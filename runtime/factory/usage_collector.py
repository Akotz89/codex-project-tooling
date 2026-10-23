#!/usr/bin/env python3
"""Collect exact, explicitly identified Codex usage events after a result receipt."""
from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
if os.name == "posix":
    import fcntl
import re
import sys
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = PROJECT_ROOT
LEDGER = Path(os.environ.get("CPI404_USAGE_LEDGER", str(ROOT / "work/factory-usage/usage.jsonl")))
SESSIONS = Path.home() / ".codex/sessions"
ARCHIVED = Path.home() / ".codex/archived_sessions"
EVENT_TYPE = re.compile(rb'"type"\s*:\s*"(session_meta|turn_context|token_usage_record)"')
COUNTERS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
SHORT_CONTEXT_LIMIT = 272_000


def read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_bytes().splitlines():
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                out.append(row)
        except (ValueError, UnicodeDecodeError):
            continue
    return out


def append_rows(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size:
        existing = path.read_bytes()
        if not existing.endswith(b"\n"):
            last = existing.rfind(b"\n")
            with path.open("r+b") as repair:
                repair.truncate(last + 1 if last >= 0 else 0)
    with path.open("ab") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())


def api_price_proxy(tokens: dict, rates: dict, short_limit: int = SHORT_CONTEXT_LIMIT,
                    long_rates: dict | None = None) -> dict:
    keys = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
    if any(not isinstance(tokens.get(k), int) or isinstance(tokens.get(k), bool) or tokens[k] < 0 for k in ("input_tokens", "cached_input_tokens", "output_tokens")):
        return {"estimate": None, "low": None, "high": None, "basis": "required counters missing"}
    i, c, o = tokens["input_tokens"], tokens["cached_input_tokens"], tokens["output_tokens"]
    w = tokens.get("cache_write_input_tokens")
    if c > i or (w is not None and (not isinstance(w, int) or isinstance(w, bool) or w < 0 or c + w > i)):
        return {"estimate": None, "low": None, "high": None, "basis": "input counter partition invalid"}
    is_long = i > short_limit
    r = long_rates if is_long else rates
    context_band = "long" if is_long else "short"
    if not isinstance(r, dict) or any(k not in r for k in ("input_uncached", "input_cached", "cache_write", "output")):
        return {"estimate": None, "low": None, "high": None,
                "basis": f"{context_band}-context published rates unavailable", "context_band": context_band}
    base = o * r["output"] + c * r["input_cached"]
    if w is None:
        remaining = i - c
        low = (base + remaining * min(r["input_uncached"], r["cache_write"])) / 1_000_000
        high = (base + remaining * max(r["input_uncached"], r["cache_write"])) / 1_000_000
        point = (base + remaining * r["input_uncached"]) / 1_000_000
        return {"estimate": point, "low": low, "high": high,
                "basis": f"conditional Standard {context_band}-context API list-rate reference; assumes zero cache-write, range allows all remaining input to be cache-write",
                "context_band": context_band}
    ordinary = i - c - w
    value = (base + ordinary * r["input_uncached"] + w * r["cache_write"]) / 1_000_000
    return {"estimate": value, "low": value, "high": value,
            "basis": f"conditional Standard {context_band}-context partitioned API list-rate reference",
            "context_band": context_band}


def expected_cost_per_accepted(attempt_cost: float | None, acceptance_probability: float | None) -> float | None:
    """Conditional expected proxy; probability must come from a matching accepted-work cohort."""
    if attempt_cost is None or acceptance_probability is None or not 0 < acceptance_probability <= 1:
        return None
    return attempt_cost / acceptance_probability


def quota_interval(before: dict, after: dict) -> dict:
    """Describe a shared account snapshot change without allocating it to a task/model."""
    common = {"from": before.get("captured_at"), "to": after.get("captured_at"),
              "attributed_to_model_or_task": None, "classification": "shared_confounded_unallocated"}
    if not before.get("captured_at") or not after.get("captured_at"):
        return {**common, "used_percent_delta": None, "status": "unknown_missing_timestamp"}
    try:
        before_time = datetime.fromisoformat(before["captured_at"].replace("Z", "+00:00"))
        after_time = datetime.fromisoformat(after["captured_at"].replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return {**common, "used_percent_delta": None, "status": "unknown_invalid_timestamp"}
    if before_time.tzinfo is None or after_time.tzinfo is None or after_time <= before_time:
        return {**common, "used_percent_delta": None, "status": "unknown_nonchronological_timestamp"}
    before_reset, after_reset = before.get("reset_timestamp_unix"), after.get("reset_timestamp_unix")
    before_window, after_window = before.get("window_id"), after.get("window_id")
    if (before_reset is None or after_reset is None or before_window is None or after_window is None
            or before.get("limit_id") is None or after.get("limit_id") is None
            or before.get("window_duration_mins") is None or after.get("window_duration_mins") is None):
        return {**common, "used_percent_delta": None, "status": "unknown_missing_window_or_reset_id"}
    if (before_reset != after_reset or before_window != after_window
            or before.get("limit_id") != after.get("limit_id")
            or before.get("window_duration_mins") != after.get("window_duration_mins")):
        return {**common, "used_percent_delta": None, "status": "window_or_reset_changed"}
    a, b = before.get("used_percent"), after.get("used_percent")
    if not isinstance(a, (int, float)) or isinstance(a, bool) or not isinstance(b, (int, float)) or isinstance(b, bool):
        return {**common, "used_percent_delta": None, "status": "unknown_missing_counter"}
    if not 0 <= a <= 100 or not 0 <= b <= 100:
        return {**common, "used_percent_delta": None, "status": "unknown_counter_out_of_bounds"}
    if b < a:
        return {**common, "used_percent_delta": None, "status": "counter_decreased_or_reset"}
    return {**common, "used_percent_delta": b - a, "status": "same_window_shared_delta_unallocated"}


def estimate_groups(records: list[dict], baseline: dict) -> list[dict]:
    rates = baseline.get("pricing", {}).get("rates", {})
    sources = {}
    for row in records:
        if row.get("type") == "source":
            sources[row.get("source_id")] = row
    streamed = {(r.get("source_id"), r.get("turn_id")) for r in records if r.get("type") == "usage"}
    groups: dict[tuple, dict] = {}
    for row in records:
        if row.get("type") not in ("usage", "reported_snapshot"):
            continue
        if row.get("type") == "reported_snapshot" and (row.get("source_id"), row.get("turn_id")) in streamed:
            continue
        key = (row.get("source_id"), row.get("turn_id"), row.get("model"), row.get("effort"))
        group = groups.setdefault(key, {"source_id": key[0], "turn_id": key[1], "model": key[2], "effort": key[3],
                                        "response_count": 0, "tokens": {k: 0 for k in COUNTERS},
                                        "price_proxy_usd": 0.0, "price_complete": True,
                                        "price_context_bands": set(),
                                        "workflow_accepted": None,
                                        "retry_count": row.get("retry_count"), "rework_count": row.get("rework_count"),
                                        "coordinator_review_overhead": row.get("coordinator_review_overhead")})
        group["response_count"] += 1
        for name in COUNTERS:
            value = row.get("tokens", {}).get(name)
            if not isinstance(value, int) or isinstance(value, bool):
                group["tokens"][name] = None
            elif group["tokens"][name] is not None:
                group["tokens"][name] += value
        model_rates = rates.get(row.get("model"), {})
        tokens = row.get("tokens", {})
        if (row.get("type") == "reported_snapshot" and
                isinstance(tokens.get("input_tokens"), int) and tokens["input_tokens"] > SHORT_CONTEXT_LIMIT):
            # A cumulative aggregate may span many requests; its total cannot select a per-request context band.
            cost = {"estimate": None, "low": None, "high": None,
                    "basis": "aggregate snapshot exceeds short threshold; per-request context bands unknown",
                    "context_band": "unknown aggregate"}
        else:
            cost = api_price_proxy(tokens, model_rates,
                                   long_rates=model_rates.get("long_context"))
        if cost.get("context_band"):
            group["price_context_bands"].add(cost["context_band"])
        if cost["estimate"] is None:
            group["price_complete"] = False
            group.setdefault("price_basis_components", set()).add(cost["basis"])
        else:
            group["price_proxy_usd"] += cost["estimate"]
            group.setdefault("price_low_usd", 0.0); group["price_low_usd"] += cost["low"]
            group.setdefault("price_high_usd", 0.0); group["price_high_usd"] += cost["high"]
            group.setdefault("price_basis_components", set()).add(cost["basis"])
    result = []
    for group in groups.values():
        if not group["price_complete"]:
            group["price_proxy_usd"] = None
            group["price_low_usd"] = None
            group["price_high_usd"] = None
            reasons = "; ".join(sorted(group.get("price_basis_components", set())))
            group["price_basis"] = f"partial: {reasons or 'at least one response lacks applicable counters or pricing'}"
        else:
            bands = ", ".join(sorted(group.pop("price_context_bands"))) or "unknown"
            components = "; ".join(sorted(group.get("price_basis_components", set())))
            group["price_basis"] = f"per-response context band(s): {bands}; {components}"
        group.pop("price_context_bands", None)
        group.pop("price_basis_components", None)
        source = sources.get(group["source_id"], {})
        outcome = source.get("outcome")
        accepted = outcome.get("whole_workflow_accepted") if isinstance(outcome, dict) else None
        group["workflow_accepted"] = accepted if isinstance(accepted, bool) else None
        group["bounded_deliverable_accepted"] = outcome.get("bounded_deliverable_accepted") if isinstance(outcome, dict) else None
        group["native_workflow_succeeded"] = outcome.get("native_workflow_succeeded") if isinstance(outcome, dict) else None
        group["product_integration_accepted"] = outcome.get("product_integration_accepted") if isinstance(outcome, dict) else None
        group["artifact_accepted"] = outcome.get("artifact_accepted") if isinstance(outcome, dict) else None
        group["observed_worker_turn_cost_proxy_usd"] = group["price_proxy_usd"]
        bounded_accepted = group["bounded_deliverable_accepted"]
        group["observed_accepted_worker_turn_cost_proxy_usd"] = group["price_proxy_usd"] if bounded_accepted is True else None
        group["accepted_task_cost_proxy_usd"] = None
        group["accepted_task_cost_reason"] = "not inferred: worker-turn coverage, retries/rework, and coordinator/reviewer costs are not fully attributed"
        group["expected_cost_per_accepted_task_equation"] = "attempt_cost / probability_of_acceptance; probability unavailable for this cohort"
        group["expected_cost_per_accepted_task_usd"] = None
        group["quota_estimate"] = None
        group["cost_basis"] = "conditional Standard API list-rate reference by per-response context band; actual service tier/endpoint unobserved; not ChatGPT billing or quota"
        result.append(group)
    by_source: dict[str, list[dict]] = {}
    for group in result:
        by_source.setdefault(group["source_id"], []).append(group)
    for turns in by_source.values():
        complete = all(t["price_proxy_usd"] is not None for t in turns)
        total = sum(t["price_proxy_usd"] for t in turns) if complete else None
        for turn in turns:
            turn["observed_task_turn_count"] = len(turns)
            turn["observed_task_cost_proxy_usd"] = total
            turn["observed_task_cost_coverage"] = "all observed native turns; retry/rework/review completeness unknown"
    return result


def _hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _path_from(root: Path, value: str) -> Path:
    p = Path(value)
    if not p.is_absolute():
        p = ROOT / p
    resolved = p.resolve(strict=True)
    if root not in resolved.parents:
        raise ValueError("receipt path outside permitted root")
    return resolved


def find_rollout(thread_id: str, created_at: str | None) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", thread_id):
        return None
    candidates = []
    if created_at:
        try:
            day = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(timezone.utc)
            dated = SESSIONS / day.strftime("%Y/%m/%d")
            candidates.extend(dated.glob(f"rollout-*-{thread_id}.jsonl"))
        except ValueError:
            pass
    # The identifier is exact; only filenames are inspected, never neighboring session contents.
    candidates.extend(ARCHIVED.glob(f"rollout-*-{thread_id}.jsonl"))
    exact = [p.resolve() for p in candidates if p.is_file()]
    return exact[0] if len(exact) == 1 else None


def verify_result(event_path: Path) -> tuple[dict, dict]:
    event = json.loads(event_path.read_text())
    desc = event.get("receipt")
    if not isinstance(desc, dict) or not isinstance(desc.get("path"), str):
        raise ValueError("result receipt path missing")
    receipt_path = _path_from(ROOT, desc["path"])
    if _hash(receipt_path) != desc.get("sha256"):
        raise ValueError("result receipt hash mismatch")
    receipt = json.loads(receipt_path.read_text())
    if event.get("dispatch_id") is not None and receipt.get("dispatch_id") != event["dispatch_id"]:
        raise ValueError("result dispatch binding mismatch")
    native = receipt.get("native_evidence")
    if not isinstance(native, dict) or not isinstance(native.get("path"), str):
        raise ValueError("native evidence path missing")
    native_path = _path_from(ROOT, native["path"])
    if _hash(native_path) != native.get("sha256"):
        raise ValueError("native evidence hash mismatch")
    native_data = json.loads(native_path.read_text())
    native_thread_id = native_data.get("thread_id") or native_data.get("threadId")
    receipt_thread_id = receipt.get("thread_id")
    if native_thread_id is not None and receipt_thread_id is not None and native_thread_id != receipt_thread_id:
        raise ValueError("receipt/native thread ID mismatch")
    thread_id = native_thread_id or receipt_thread_id
    if not isinstance(thread_id, str):
        raise ValueError("native thread ID missing")
    return event, {"receipt": receipt, "native": native_data, "thread_id": thread_id}


def verify_parent_evidence(source: dict) -> None:
    binding = source.get("parent_evidence")
    if binding is None:
        return
    if not isinstance(binding, dict) or not isinstance(binding.get("path"), str) or not isinstance(binding.get("sha256"), str):
        raise ValueError("parent evidence binding requires path and sha256")
    receipt_path = _path_from(ROOT, binding["path"])
    if _hash(receipt_path) != binding["sha256"]:
        raise ValueError("parent evidence hash mismatch")
    receipt = json.loads(receipt_path.read_text())
    worker = receipt.get("worker", {})
    bound_thread = worker.get("thread_id") if isinstance(worker, dict) else None
    if bound_thread is not None and bound_thread != source.get("thread_id"):
        raise ValueError("parent evidence thread ID mismatch")


def collect_rollout(source: dict, ledger: Path) -> int:
    if source.get("source_id") != source.get("thread_id"):
        raise ValueError("source ID must equal the exact thread ID")
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.with_suffix(ledger.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _collect_rollout_locked(source, ledger)


def _collect_rollout_locked(source: dict, ledger: Path) -> int:
    registered = [r for r in read_records(ledger)
                  if r.get("type") == "source" and r.get("source_id") == source.get("source_id")]
    registered_mode = registered[-1].get("collection_mode") if registered else None
    requested_mode = source.get("collection_mode")
    if registered_mode == "future_only" and requested_mode != "future_only":
        raise ValueError("registered future-only source cannot be downgraded to historical collection")
    if requested_mode == "future_only" and registered_mode not in (None, "future_only"):
        raise ValueError("historical source cannot be changed to future-only collection")
    if requested_mode == "future_only":
        return _collect_future_rollout_locked(source, ledger)
    path = Path(source["path"]).resolve(strict=True)
    thread_id = source["thread_id"]
    session_id = source.get("session_id") or thread_id
    records = read_records(ledger)
    cursors = [r for r in records if r.get("type") == "cursor" and r.get("source_id") == source["source_id"]]
    cursor = cursors[-1] if cursors else {"offset": 0, "contexts": {}}
    if cursor.get("session_id") not in (None, session_id):
        raise ValueError("registered session ID changed for source thread")
    offset = cursor["offset"] if path.stat().st_size >= cursor["offset"] else 0
    if source.get("verify_session_meta") and not cursor.get("thread_meta_verified"):
        offset = 0
    contexts = dict(cursor.get("contexts", {})) if offset else {}
    thread_meta_verified = bool(cursor.get("thread_meta_verified", offset)) if offset else False
    seen = {(r.get("turn_id"), r.get("response_id")) for r in records if r.get("type") == "usage" and r.get("source_id") == source["source_id"]}
    rows = []
    end = offset
    with path.open("rb") as f:
        f.seek(offset)
        while True:
            start = f.tell()
            raw = f.readline()
            if not raw:
                end = f.tell()
                break
            if raw[-1:] != b"\n":
                end = start
                break
            match = EVENT_TYPE.search(raw)
            if not match:
                end = f.tell()
                continue
            try:
                obj = json.loads(raw)
                payload = obj.get("payload", {})
                if obj.get("type") == "session_meta":
                    if payload.get("id") != thread_id:
                        raise ValueError("session metadata ID does not match native thread")
                    thread_meta_verified = True
                elif obj.get("type") == "turn_context":
                    tid = payload.get("turn_id")
                    if isinstance(tid, str):
                        contexts[tid] = {"model": payload.get("model"), "effort": payload.get("effort")}
                elif obj.get("type") == "token_usage_record":
                    if not thread_meta_verified:
                        raise ValueError("usage event precedes verified native thread metadata")
                    if payload.get("thread_id") != thread_id or payload.get("session_id") != session_id:
                        raise ValueError("usage event thread/session mismatch")
                    tid, rid = payload.get("turn_id"), payload.get("response_id")
                    usage = payload.get("usage")
                    ctx = contexts.get(tid)
                    if not isinstance(tid, str) or not isinstance(rid, str) or not isinstance(usage, dict) or not ctx:
                        raise ValueError("usage event lacks response, turn, counters, or model context")
                    if (tid, rid) not in seen:
                        tokens = {k: usage.get(k) for k in COUNTERS}
                        if any(v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0) for v in tokens.values()):
                            raise ValueError("invalid token counter")
                        rows.append({"type": "usage", "source_id": source["source_id"], "thread_id": thread_id,
                                     "session_id": session_id,
                                     "turn_id": tid, "response_id": rid, "timestamp": obj.get("timestamp"),
                                     "model": ctx["model"], "effort": ctx["effort"], "tokens": tokens,
                                     "task_id": source.get("task_id"), "handoff_task_id": source.get("handoff_task_id"),
                                     "task_class": source.get("task_class"),
                                     "difficulty_signals": source.get("difficulty_signals"), "route": source.get("route", "unknown"),
                                     "outcome": source.get("outcome"), "elapsed_seconds": source.get("elapsed_seconds"),
                                     "retry_count": source.get("retry_count"), "rework_count": source.get("rework_count"),
                                     "coordinator_review_overhead": source.get("coordinator_review_overhead"),
                                     "evidence": source.get("evidence")})
                        seen.add((tid, rid))
            except (ValueError, UnicodeDecodeError):
                raise
            end = f.tell()
    if rows or end != cursor.get("offset", 0) or (thread_meta_verified and not cursor.get("thread_meta_verified")):
        rows.append({"type": "cursor", "source_id": source["source_id"], "thread_id": thread_id,
                     "session_id": session_id, "thread_meta_verified": thread_meta_verified,
                     "path": str(path), "offset": end, "contexts": contexts})
        append_rows(ledger, rows)
    return len([r for r in rows if r.get("type") == "usage"])


def _future_header(path: Path, thread_id: str, session_id: str) -> tuple[os.stat_result, bytes]:
    """Read only the bounded first session_meta record for an explicit future-only source."""
    stat = path.stat()
    with path.open("rb") as f:
        header = f.readline(65537)
        if len(header) > 65536 or not header.endswith(b"\n"):
            raise ValueError("future source lacks a complete bounded session metadata header")
    try:
        obj = json.loads(header)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("future source session metadata is invalid") from exc
    payload = obj.get("payload", {})
    if obj.get("type") != "session_meta" or payload.get("id") != thread_id:
        raise ValueError("future source session metadata thread mismatch")
    if payload.get("session_id") != session_id:
        raise ValueError("future source session metadata session mismatch")
    return stat, header


def _collect_future_rollout_locked(source: dict, ledger: Path) -> int:
    path = Path(source["path"]).resolve(strict=True)
    thread_id, session_id = source["thread_id"], source["session_id"]
    stat = path.stat()
    records = read_records(ledger)
    cursors = [r for r in records if r.get("type") == "future_cursor" and r.get("source_id") == source["source_id"]]
    if cursors:
        prior = cursors[-1]
        if stat.st_size < prior.get("offset", 0):
            raise ValueError("future source truncated; refusing historical rebaseline")
        if stat.st_dev != prior.get("device") or stat.st_ino != prior.get("inode"):
            raise ValueError("future source identity changed; refusing historical rebaseline")
    elif any(r.get("type") == "source" and r.get("source_id") == source["source_id"] for r in records):
        raise ValueError("future source cursor missing; refusing historical rebaseline")
    stat, header = _future_header(path, thread_id, session_id)
    if not cursors:
        # Registration is a forward-only baseline. Never parse bytes before this offset.
        baseline = source.get("baseline", {})
        if (baseline.get("device") != stat.st_dev or baseline.get("inode") != stat.st_ino
                or baseline.get("header_sha256") != hashlib.sha256(header).hexdigest()):
            raise ValueError("future source no longer matches observed rollout identity")
        with path.open("rb") as f:
            f.seek(max(0, stat.st_size - 1))
            ends_newline = stat.st_size == 0 or f.read(1) == b"\n"
        cursor = {"type": "future_cursor", "source_id": source["source_id"],
                  "thread_id": thread_id, "session_id": session_id, "path": str(path),
                  "offset": stat.st_size, "device": stat.st_dev, "inode": stat.st_ino,
                  "header_sha256": hashlib.sha256(header).hexdigest(), "contexts": {},
                  "discard_partial_until_newline": not ends_newline,
                  "baseline_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                  "unverified_timestamp_events": 0}
        append_rows(ledger, [source, cursor])
        return 0

    cursor = cursors[-1]
    if (cursor.get("path") != str(path) or cursor.get("device") != stat.st_dev
            or cursor.get("inode") != stat.st_ino
            or cursor.get("header_sha256") != hashlib.sha256(header).hexdigest()):
        raise ValueError("future source identity changed; refusing historical rebaseline")
    offset = cursor.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or stat.st_size < offset:
        raise ValueError("future source truncated; refusing historical rebaseline")
    # shortcut: same-inode truncation followed by regrowth beyond this offset cannot be detected without reading retained tail/history bytes.
    contexts = dict(cursor.get("contexts", {}))
    discard_partial = bool(cursor.get("discard_partial_until_newline"))
    unverified_timestamp_events = cursor.get("unverified_timestamp_events", 0)
    baseline_time = datetime.fromisoformat(cursor["baseline_at_utc"].replace("Z", "+00:00"))
    seen = {(r.get("turn_id"), r.get("response_id")) for r in records
            if r.get("type") == "usage" and r.get("source_id") == source["source_id"]}
    rows = []
    end = offset
    with path.open("rb") as f:
        f.seek(offset)
        if discard_partial:
            tail = f.readline()
            if not tail or not tail.endswith(b"\n"):
                end = f.tell()
                cursor = dict(cursor, offset=end)
                append_rows(ledger, [cursor])
                return 0
            end = f.tell()
            discard_partial = False
        while True:
            start = f.tell()
            raw = f.readline()
            if not raw:
                end = f.tell()
                break
            if not raw.endswith(b"\n"):
                end = start
                break
            if not EVENT_TYPE.search(raw):
                end = f.tell()
                continue
            obj = json.loads(raw)
            payload = obj.get("payload", {})
            kind = obj.get("type")
            if kind == "session_meta":
                if payload.get("id") != thread_id or payload.get("session_id") != session_id:
                    raise ValueError("future source session identity changed")
            elif kind == "turn_context":
                timestamp = obj.get("timestamp")
                try:
                    observed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else None
                except ValueError:
                    observed_time = None
                if observed_time is None or observed_time.tzinfo is None or observed_time <= baseline_time:
                    unverified_timestamp_events += 1
                    end = f.tell()
                    continue
                tid = payload.get("turn_id")
                if isinstance(tid, str):
                    contexts[tid] = {"model": payload.get("model") if isinstance(payload.get("model"), str) else None,
                                     "effort": payload.get("effort") if isinstance(payload.get("effort"), str) else None}
            elif kind == "token_usage_record":
                if payload.get("thread_id") != thread_id or payload.get("session_id") != session_id:
                    raise ValueError("future usage event thread/session mismatch")
                timestamp = obj.get("timestamp")
                try:
                    observed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00")) if isinstance(timestamp, str) else None
                except ValueError:
                    observed_time = None
                if observed_time is None or observed_time.tzinfo is None or observed_time <= baseline_time:
                    unverified_timestamp_events += 1
                    end = f.tell()
                    continue
                tid, rid = payload.get("turn_id"), payload.get("response_id")
                usage = payload.get("usage")
                if not isinstance(tid, str) or not isinstance(rid, str) or not isinstance(usage, dict):
                    raise ValueError("future usage event lacks response, turn, or counters")
                if (tid, rid) not in seen:
                    tokens = {k: usage.get(k) for k in COUNTERS}
                    if any(v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 0) for v in tokens.values()):
                        raise ValueError("invalid future token counter")
                    ctx = contexts.get(tid, {})
                    rows.append({"type": "usage", "source_id": source["source_id"],
                                 "thread_id": thread_id, "session_id": session_id,
                                 "turn_id": tid, "response_id": rid, "timestamp": obj.get("timestamp"),
                                 "model": ctx.get("model"), "effort": ctx.get("effort"),
                                 "context_status": "observed" if ctx.get("model") and ctx.get("effort") else "unknown_missing_turn_context",
                                 "tokens": tokens, "task_id": None,
                                 "task_class": "coordinator-overhead", "route": source.get("route", "unknown"),
                                 "collection_mode": "future_only", "historical_totals_imported": False})
                    seen.add((tid, rid))
            end = f.tell()
    updated = dict(cursor, offset=end, contexts=contexts,
                   discard_partial_until_newline=discard_partial,
                   unverified_timestamp_events=unverified_timestamp_events)
    rows.append(updated)
    append_rows(ledger, rows)
    return len([r for r in rows if r.get("type") == "usage"])


def collect_future_source_file(source_path: Path, ledger: Path) -> int:
    source = json.loads(source_path.read_text())
    if not isinstance(source, dict) or source.get("type") != "source" or source.get("collection_mode") != "future_only":
        raise ValueError("future source manifest must declare future_only")
    thread_id, session_id = source.get("thread_id"), source.get("session_id")
    if not isinstance(thread_id, str) or source.get("source_id") != thread_id or not isinstance(session_id, str):
        raise ValueError("future source requires exact thread and session IDs")
    path = Path(source.get("path", "")).resolve(strict=True)
    allowed = (SESSIONS.resolve(), ARCHIVED.resolve())
    if not any(base == path or base in path.parents for base in allowed) or not path.name.endswith(f"-{thread_id}.jsonl"):
        raise ValueError("future source must be the exact rollout filename for its thread ID")
    baseline = source.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("future source requires a bounded observed file identity")
    source["path"] = str(path)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.with_suffix(ledger.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing = [r for r in read_records(ledger) if r.get("type") == "source" and r.get("source_id") == thread_id]
        if existing and existing[-1] != source:
            raise ValueError("future source conflicts with existing source registration")
        return _collect_future_rollout_locked(source, ledger)


def collect_event(event_path: Path, ledger: Path) -> int:
    event, proof = verify_result(event_path)
    task_id = event.get("task_id")
    if not isinstance(task_id, str):
        raise ValueError("task ID missing")
    thread_id = proof["thread_id"]
    source_id = thread_id
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.with_suffix(ledger.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = read_records(ledger)
        sources = [r for r in rows if r.get("type") == "source" and r.get("source_id") == source_id]
        rollout = Path(sources[-1]["path"]).resolve(strict=True) if sources else find_rollout(
            thread_id, proof["native"].get("created_at") or proof["native"].get("timestamp"))
        if rollout is None:
            return 0
        if not sources:
            source = {"type": "source", "source_id": source_id, "task_id": task_id, "thread_id": thread_id,
                      "path": str(rollout), "handoff_task_id": task_id, "task_class": proof["receipt"].get("task_class"),
                      "difficulty_signals": proof["receipt"].get("difficulty_signals"), "route": proof["receipt"].get("route", "unknown"),
                      "outcome": {"whole_workflow_accepted": proof["receipt"].get("whole_workflow_accepted"),
                                  "bounded_deliverable_accepted": (proof["receipt"].get("bounded_deliverable_accepted")
                                      if isinstance(proof["receipt"].get("bounded_deliverable_accepted"), bool)
                                      else proof["receipt"].get("acceptance") if isinstance(proof["receipt"].get("acceptance"), bool) else None),
                                  "native_workflow_succeeded": proof["receipt"].get("native_workflow_succeeded"),
                                  "factory_workflow_succeeded": proof["receipt"].get("factory_workflow_succeeded"),
                                  "product_integration_accepted": proof["receipt"].get("product_integration_accepted"),
                                  "artifact_accepted": proof["receipt"].get("artifact_accepted"),
                                  "gameplay_accepted": proof["receipt"].get("gameplay_accepted"),
                                  "terminal": proof["receipt"].get("terminal"), "quiescent": proof["receipt"].get("quiescent"),
                                  "child_exit_code": proof["receipt"].get("child_exit_code")},
                      "elapsed_seconds": proof["receipt"].get("elapsed_seconds"),
                      "retry_count": proof["receipt"].get("retry_count"), "rework_count": proof["receipt"].get("rework_count"),
                      "coordinator_review_overhead": proof["receipt"].get("coordinator_review_overhead"), "evidence": desc_ref(event)}
            append_rows(ledger, [source])
        else:
            source = sources[-1]
            if source.get("path") != str(rollout):
                raise ValueError("source path changed for registered task/thread")
            if source.get("collection_mode") == "future_only":
                # A long-lived source may observe many task results; don't bind one task's outcome to its future usage.
                return _collect_rollout_locked(source, ledger)
            if source.get("task_id") != task_id:
                raise ValueError("task provenance conflicts with registered native thread")
            outcome = source.get("outcome")
            if not isinstance(outcome, dict):
                outcome = {}
            changed = False
            updates = {"whole_workflow_accepted": proof["receipt"].get("whole_workflow_accepted"),
                       "bounded_deliverable_accepted": (proof["receipt"].get("bounded_deliverable_accepted")
                           if isinstance(proof["receipt"].get("bounded_deliverable_accepted"), bool)
                           else proof["receipt"].get("acceptance") if isinstance(proof["receipt"].get("acceptance"), bool) else None),
                       "native_workflow_succeeded": proof["receipt"].get("native_workflow_succeeded"),
                       "factory_workflow_succeeded": proof["receipt"].get("factory_workflow_succeeded"),
                       "product_integration_accepted": proof["receipt"].get("product_integration_accepted"),
                       "artifact_accepted": proof["receipt"].get("artifact_accepted"),
                       "gameplay_accepted": proof["receipt"].get("gameplay_accepted"),
                       "terminal": proof["receipt"].get("terminal"), "quiescent": proof["receipt"].get("quiescent"),
                       "child_exit_code": proof["receipt"].get("child_exit_code")}
            for key in ("whole_workflow_accepted", "bounded_deliverable_accepted", "native_workflow_succeeded",
                        "factory_workflow_succeeded", "product_integration_accepted", "artifact_accepted",
                        "gameplay_accepted", "terminal", "quiescent", "child_exit_code"):
                value = updates[key]
                if value is not None and outcome.get(key) != value:
                    outcome[key] = value; changed = True
            if changed:
                source = dict(source, outcome=outcome)
                append_rows(ledger, [source])
        return _collect_rollout_locked(source, ledger)


def collect_source_file(source_path: Path, ledger: Path) -> int:
    source = json.loads(source_path.read_text())
    if not isinstance(source, dict) or source.get("type") != "source":
        raise ValueError("source file must contain one source object")
    if source.get("collection_mode") == "future_only":
        return collect_future_source_file(source_path, ledger)
    if not isinstance(source.get("source_id"), str) or not isinstance(source.get("thread_id"), str):
        raise ValueError("source identity required")
    if source.get("session_id") is not None and not isinstance(source.get("session_id"), str):
        raise ValueError("source session ID must be a string")
    if source["source_id"] != source["thread_id"]:
        raise ValueError("source ID must equal the exact thread ID")
    verify_parent_evidence(source)
    resolved = Path(source.get("path", "")).resolve(strict=True)
    allowed = (Path.home() / ".codex/sessions").resolve(), (Path.home() / ".codex/archived_sessions").resolve()
    if not any(base == resolved or base in resolved.parents for base in allowed) or not resolved.name.endswith(f"-{source['thread_id']}.jsonl"):
        raise ValueError("source must be the exact rollout filename for its thread ID")
    source["path"] = str(resolved)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.with_suffix(ledger.suffix + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        records = read_records(ledger)
        existing = [r for r in records if r.get("type") == "source" and r.get("source_id") == source["source_id"]]
        if existing:
            old = existing[-1]
            old_mode = old.get("collection_mode")
            if old_mode == "future_only" and source.get("collection_mode") != "future_only":
                raise ValueError("registered future-only source cannot be downgraded to historical collection")
            if old_mode != source.get("collection_mode") and (old_mode is not None or source.get("collection_mode") is not None):
                raise ValueError("source collection mode conflicts with existing registration")
            if (old.get("path") != source["path"] or old.get("thread_id") != source["thread_id"] or
                    old.get("task_id") != source.get("task_id") or
                    old.get("session_id") not in (None, source.get("session_id"))):
                raise ValueError("source identity conflicts with existing registration")
            if old != source:
                append_rows(ledger, [source])
        else:
            append_rows(ledger, [source])
        if source.get("reported_tokens") and not any(r.get("type") == "reported_snapshot" and r.get("source_id") == source["source_id"] for r in records):
            append_rows(ledger, [{"type": "reported_snapshot", "source_id": source["source_id"],
                "thread_id": source["thread_id"], "turn_id": source.get("reported_turn_id"),
                "model": source.get("model"), "effort": source.get("effort"),
                "tokens": source["reported_tokens"], "evidence": source.get("evidence"),
                "note": "aggregate receipt fallback; prefer native response stream when present"}])
        return _collect_rollout_locked(source, ledger)


def desc_ref(event: dict) -> dict:
    desc = event.get("receipt", {})
    return {"result_receipt": desc.get("path"), "sha256": desc.get("sha256")}



class NativeAuditRefusal(ValueError):
    """Only stable, locally selected reason codes cross the audit boundary."""


def _audit_require(condition, reason):
    if not condition:
        raise NativeAuditRefusal(reason)


def _audit_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf8")).hexdigest()


def _audit_json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            _audit_require(key not in result, "duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique)


def _audit_stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _audit_private_json(root, locator):
    from .jev_prepared_bridge import private_json, MAX_CONTEXT
    path, _ = private_json(root, locator)
    before = path.stat()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _audit_require(_audit_stamp(before) == _audit_stamp(opened)
                       and stat.S_ISREG(opened.st_mode) and not opened.st_mode & 0o077
                       and opened.st_nlink == 1 and opened.st_uid == os.getuid()
                       and opened.st_size <= MAX_CONTEXT, "private_evidence_changed")
        raw = stream.read(opened.st_size + 1)
        _audit_require(_audit_stamp(opened) == _audit_stamp(os.fstat(stream.fileno()))
                       and len(raw) == opened.st_size, "private_evidence_changed")
    data = _audit_json(raw)
    _audit_require(isinstance(data, dict), "private_evidence_invalid")
    return path, data, raw


def _audit_owned(path, base):
    path = Path(path)
    _audit_require(path.is_absolute() and path.is_relative_to(base), "unsafe_source_path")
    _audit_require(path.resolve(strict=True) == path, "unsafe_source_path")
    for component in (path, *path.parents):
        _audit_require(not component.is_symlink(), "unsafe_source_path")
        if component == base:
            break
    info = path.stat()
    _audit_require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                   and info.st_uid == os.getuid() and not info.st_mode & 0o022,
                   "unsafe_source_file")
    return info


def _audit_original_records(root, thread_id, call_id):
    """Decode bounded envelopes for identity; examine only requested arguments/results."""
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").absolute()
    database = home / "state_5.sqlite"
    _audit_owned(database, home)
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute("SELECT rollout_path, cwd FROM threads WHERE id = ?", (thread_id,)).fetchall()
    _audit_require(len(rows) == 1, "thread_resolution_unverified")
    rollout, cwd = rows[0]
    _audit_require(isinstance(cwd, str) and Path(cwd).is_absolute()
                   and Path(cwd).resolve() == root, "thread_cwd_mismatch")
    _audit_require(isinstance(rollout, str), "unsafe_source_path")
    path = Path(rollout)
    bases = [home / "sessions", home / "archived_sessions"]
    bases = [base for base in bases if path.is_relative_to(base)]
    _audit_require(len(bases) == 1, "unsafe_source_path")
    before = _audit_owned(path, bases[0])
    _audit_require(path.name.startswith("rollout-") and path.name.endswith("-" + thread_id + ".jsonl"),
                   "source_filename_mismatch")
    calls, outputs = [], []
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        _audit_require(_audit_stamp(opened) == _audit_stamp(before), "source_identity_changed")
        header = stream.readline(min(opened.st_size, 1048577))
        _audit_require(len(header) <= 1048576 and header.endswith(b"\n"), "session_metadata_invalid")
        meta = _audit_json(header)
        payload = meta.get("payload", {})
        _audit_require(meta.get("type") == "session_meta" and isinstance(payload, dict)
                       and payload.get("id") == thread_id
                       and payload.get("session_id", thread_id) == thread_id
                       and isinstance(payload.get("cwd"), str) and Path(payload["cwd"]).is_absolute()
                       and Path(payload["cwd"]).resolve() == root, "session_metadata_mismatch")
        remaining = opened.st_size - len(header)
        position = 1
        while remaining > 0:
            raw = stream.readline(min(remaining, 1048577))
            remaining -= len(raw)
            position += 1
            _audit_require(bool(raw), "source_identity_changed")
            _audit_require(len(raw) <= 1048576 and raw.endswith(b"\n"), "source_record_invalid")
            # Decode envelope identity even when JSON escapes the call ID. Never parse
            # unrelated argument/output strings or retain unrelated envelopes.
            row = _audit_json(raw)
            _audit_require(isinstance(row, dict), "source_record_invalid")
            data = row.get("payload", {})
            if not isinstance(data, dict) or data.get("call_id") != call_id:
                continue
            if row.get("type") != "response_item":
                continue
            record = (row, data, hashlib.sha256(raw).hexdigest(), position)
            if data.get("type") == "function_call":
                calls.append(record)
            elif data.get("type") == "function_call_output":
                outputs.append(record)
            else:
                raise NativeAuditRefusal("requested_record_invalid")
        after = os.fstat(stream.fileno())
        _audit_require(_audit_stamp(opened) == _audit_stamp(after)
                       and _audit_stamp(path.stat()) == _audit_stamp(opened), "source_identity_changed")
    _audit_require(len(calls) == 1, "call_missing_or_duplicate")
    _audit_require(len(outputs) <= 1, "result_duplicate")
    if outputs:
        _audit_require(outputs[0][3] > calls[0][3], "result_precedes_call")
    identity = {"source_path_sha256": hashlib.sha256(str(path).encode()).hexdigest(),
                "session_meta_sha256": hashlib.sha256(header).hexdigest(),
                "call_record_sha256": calls[0][2],
                "result_record_sha256": outputs[0][2] if outputs else None}
    return calls[0], outputs[0] if outputs else None, identity


def _audit_opaque_message(message):
    """Recognize the stored token shape only; this is not decryption or authentication."""
    if not isinstance(message, str) or not re.fullmatch(r"gAAAA[A-Za-z0-9_-]+={0,2}", message):
        return False
    try:
        raw = base64.b64decode(message, altchars=b"-_", validate=True)
        return len(raw) >= 73 and raw[0] == 128 and (len(raw) - 57) % 16 == 0
    except ValueError:
        return False


def _audit_opaque_hook_binding(root, path, prepared, thread_id, call_id, actual, handle):
    from .jev_prepared_bridge import _identifier
    from .native_hook import native_caller_mapping
    try:
        consume_path, consume, consume_raw = _audit_private_json(root, str(path.with_suffix(".consume.json")))
        result_path, post, post_raw = _audit_private_json(root, str(path.with_suffix(".result.json")))
    except FileNotFoundError:
        raise NativeAuditRefusal("hook_evidence_missing") from None
    identity = consume.get("identity")
    _audit_require(isinstance(identity, dict) and set(identity) == {"session_id", "turn_id", "tool_use_id"}
                   and identity.get("session_id") == thread_id and identity.get("tool_use_id") == call_id
                   and _identifier(identity.get("turn_id")) and post.get("identity") == identity,
                   "hook_identity_mismatch")
    pair = prepared.get("spawn_agent")
    declared = prepared.get("native_dispatch")
    arguments_hash = hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
    message_hash = hashlib.sha256(actual["message"].encode()).hexdigest()
    _audit_require(consume.get("schema") == "CPI414NATIVECONSUMPTIONV2"
                   and post.get("schema") == "CPI414NATIVERESULTV2"
                   and consume.get("status") == "pending_native_result"
                   and post.get("status") == "native_handle_observed"
                   and consume.get("preparation_id") == prepared.get("preparation_id") == post.get("preparation_id")
                   and consume.get("task_id") == prepared.get("task_id")
                   and consume.get("project_binding") == prepared.get("project_binding")
                   and consume.get("prepared_caller_id") == prepared.get("caller_id")
                   and consume.get("caller_mapping") == native_caller_mapping(prepared, thread_id)
                   and consume.get("native_transport") == prepared.get("native_transport")
                   and consume.get("native_transport") is not None
                   and consume.get("declared_task") == declared == post.get("declared_task")
                   and isinstance(declared, dict)
                   and consume.get("declared_message_sha256") == declared.get("message_sha256")
                   and consume.get("prepared_pair") == pair
                   and consume.get("requested_pair") == pair
                   and consume.get("plaintext_attestation") == post.get("plaintext_attestation") == "unavailable"
                   and consume.get("observed_message_sha256") == message_hash == post.get("observed_post_message_sha256")
                   and consume.get("observed_pre_arguments_sha256") == arguments_hash
                   and consume.get("applied_arguments_sha256") == arguments_hash == post.get("observed_post_arguments_sha256")
                   and isinstance(handle, str) and post.get("native_handle") == handle,
                   "hook_binding_mismatch")
    return {"consume_sha256": hashlib.sha256(consume_raw).hexdigest(), "result_sha256": hashlib.sha256(post_raw).hexdigest(),
            "canonical_arguments_sha256": arguments_hash, "observed_message_sha256": message_hash}


def audit_native_call(root: Path, preparation: str, thread_id: str, call_id: str) -> dict:
    """Retrospective original-call evidence only. Never reserve, persist, retry or dispatch."""
    from .consumer import shared_root
    from .native_hook import ALLOWED_ARGS, TOOLS, REFUSAL_REASONS, native_result_handle, validate
    result = {"schema": "CPI414NATIVECALLAUDITV1", "status": "unverified", "reason": None,
              "thread_id": None, "call_id": None, "source": None,
              "preparation_sha256": None, "expected_arguments_sha256": None,
              "actual_arguments_sha256": None, "original_arguments_sha256": None,
              "actual_hook_arguments_sha256": None, "expected_message_sha256": None,
              "actual_message_sha256": None, "arguments_match": None, "mismatched_fields": [],
              "observable_arguments_match": None, "plaintext_attestation": "unverified",
              "message_representation": "unverified", "hook_binding": None,
              "fresh_at_call": None, "replay_authorized": False,
              "native_result": "unverified", "refusal_reason": None,
              "reported_handle_sha256": None, "terminal_result": "unverified",
              "caller_acceptance": "not_performed", "observed_model": None,
              "observed_effort": None, "worker_usage": None}
    try:
        _audit_require(os.name == "posix", "unsupported_platform")
        _audit_require(isinstance(thread_id, str) and str(uuid.UUID(thread_id)) == thread_id,
                       "thread_identity_invalid")
        _audit_require(isinstance(call_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call_id),
                       "call_identity_invalid")
        result.update(thread_id=thread_id, call_id=call_id)
        root = shared_root(Path(root).resolve(strict=True))
        path, prepared, preparation_raw = _audit_private_json(root, preparation)
        result["preparation_sha256"] = hashlib.sha256(preparation_raw).hexdigest()
        expected = prepared.get("native_request")
        _audit_require(isinstance(expected, dict) and set(expected) == ALLOWED_ARGS,
                       "preparation_packet_invalid")
        # Reuse the hook's full private/project/source/caller/carrier policy without TTL replay.
        event = {"cwd": str(root), "session_id": thread_id, "turn_id": "audit-turn",
                 "tool_use_id": call_id, "tool_name": "collaboration.spawn_agent", "tool_input": expected}
        try:
            validated = validate(event, root, fresh=False)
        except Exception:
            raise NativeAuditRefusal("current_binding_unverified") from None
        _audit_require(validated[3] == path, "preparation_locator_mismatch")
        call, output, identity = _audit_original_records(root, thread_id, call_id)
        result["source"] = identity
        data = call[1]
        _audit_require(data.get("name") in TOOLS, "call_tool_mismatch")
        actual = _audit_json(data["arguments"]) if isinstance(data.get("arguments"), str) else data.get("arguments")
        _audit_require(isinstance(actual, dict) and not set(actual) - ALLOWED_ARGS
                       and isinstance(actual.get("message"), str), "call_arguments_invalid")
        result["original_arguments_sha256"] = (hashlib.sha256(data["arguments"].encode()).hexdigest()
            if isinstance(data.get("arguments"), str) else None)
        result["actual_hook_arguments_sha256"] = hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
        opaque = _audit_opaque_message(actual["message"]) and actual["message"] != expected["message"]
        compared_fields = ALLOWED_ARGS - {"message"} if opaque else ALLOWED_ARGS
        result.update(expected_arguments_sha256=_audit_hash(expected), actual_arguments_sha256=_audit_hash(actual),
                      expected_message_sha256=hashlib.sha256(expected["message"].encode()).hexdigest(),
                      actual_message_sha256=hashlib.sha256(actual["message"].encode()).hexdigest(),
                      mismatched_fields=sorted(key for key in compared_fields if actual.get(key) != expected.get(key)),
                      plaintext_attestation="unavailable" if opaque else "exact" if actual["message"] == expected["message"] else "mismatch",
                      message_representation="opaque_token" if opaque else "plaintext")
        result["observable_arguments_match"] = not result["mismatched_fields"] and set(actual) == set(expected)
        result["arguments_match"] = None if opaque else result["observable_arguments_match"]
        if not result["observable_arguments_match"]:
            result.update(status="argument_mismatch", reason="exact_consumption_refused")
        timestamp = datetime.fromisoformat(call[0]["timestamp"].replace("Z", "+00:00"))
        _audit_require(timestamp.tzinfo is not None, "call_timestamp_invalid")
        captured, expires = prepared.get("capability_captured_at"), prepared.get("capability_expires_at")
        _audit_require(isinstance(captured, (float, int)) and not isinstance(captured, bool)
                       and expires == captured + 60, "preparation_timestamp_invalid")
        result["fresh_at_call"] = 0 <= timestamp.timestamp() - captured <= 60
        handle = None
        if output is None:
            result["native_result"] = "missing_result"
        else:
            response = output[1].get("output")
            _audit_require(isinstance(response, str), "result_invalid")
            try:
                decoded = _audit_json(response)
            except json.JSONDecodeError:
                decoded = None
            handle = native_result_handle(decoded)
            if handle:
                result.update(native_result="reported_handle", reported_handle_sha256=hashlib.sha256(handle.encode()).hexdigest())
            elif decoded is None:
                codes = REFUSAL_REASONS + ["native_dispatch_already_reserved", "native_receipt_missing",
                                          "native_private_io_refused", "native_validation_refused"]
                pattern = "(?:" + "|".join(codes) + ")"
                reason = re.fullmatch(pattern, response.strip()) or re.search(
                    r"Native worker (?:preparation|policy input|command) refused[: ]+(" + pattern + r")\b", response)
                _audit_require(reason is not None, "result_unrecognized")
                result.update(native_result="native_refused", refusal_reason=reason.group(1) if reason.lastindex else reason.group(0))
            else:
                raise NativeAuditRefusal("result_unrecognized")
        if not result["observable_arguments_match"]:
            result.update(status="argument_mismatch", reason="exact_consumption_refused")
        elif result["native_result"] == "reported_handle":
            if opaque:
                result["hook_binding"] = _audit_opaque_hook_binding(root, path, prepared, thread_id, call_id, actual, handle)
            result.update(status="matching_reported_launch", reason=None)
        else:
            result.update(status="matching_without_launch", reason=result["native_result"])
    except NativeAuditRefusal as error:
        if result["status"] != "argument_mismatch":
            result.update(status="unverified", reason=str(error))
    except Exception:
        if result["status"] != "argument_mismatch":
            result.update(status="unverified", reason="evidence_validation_refused")
    return result


def audit_native_cli(root: Path, args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="project-factory audit-native-call",
        description="Audit one original native call read-only; never authorizes replay or caller acceptance.")
    parser.add_argument("--preparation", required=True)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument("--call-id", required=True)
    options = parser.parse_args(args)
    result = audit_native_call(root, options.preparation, options.thread_id, options.call_id)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "matching_reported_launch" else 1


def main() -> int:
    if sys.argv[1:2] == ["audit-native-call"]:
        from .consumer import project_root
        return audit_native_cli(project_root(), sys.argv[2:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--event")
    parser.add_argument("--source")
    parser.add_argument("--future-source")
    parser.add_argument("--ledger", default=str(LEDGER))
    args = parser.parse_args()
    if sum(bool(x) for x in (args.event, args.source, args.future_source)) != 1:
        parser.error("provide exactly one of --event, --source, or --future-source")
    try:
        if args.event:
            count = collect_event(Path(args.event), Path(args.ledger))
        elif args.source:
            count = collect_source_file(Path(args.source), Path(args.ledger))
        else:
            count = collect_future_source_file(Path(args.future_source), Path(args.ledger))
        print(json.dumps({"collected": count}))
        return 0
    except Exception as exc:
        print(f"usage collector skipped ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
