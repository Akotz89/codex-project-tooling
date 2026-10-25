"""Deterministic LangGraph transitions guarded by one filesystem lock per DB."""
from __future__ import annotations

import copy
from contextlib import closing
import fcntl
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import TypedDict
import uuid
from datetime import datetime, timedelta, timezone

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver


class State(TypedDict, total=False):
    model: dict
    event: dict
    event_hash: str
    event_hash_event_id: str
    event_received_at: str
    event_received_at_event_id: str
    response: dict
    project_policy: dict


class InvalidEvent(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidEvent(message)


def string(value):
    return isinstance(value, str) and bool(value)


def worker_role(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is not None


def native_thread_id(value):
    return string(value) and value == value.strip() and "\0" not in value


def redact_report_text(value):
    value = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|(?:basic|bearer)\s+[^\s,;&}]+|[^\s,;&}]+)", r"\1[redacted]", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", value)
    value = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key)([\"']?\s*[=:]\s*)([\"'])(.*?)\3",
                   lambda match: f"{match[1]}{match[2]}{match[3]}[redacted]{match[3]}", value, flags=re.DOTALL)
    value = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)[^\s,;&}]+", r"\1\2[redacted]", value)
    return re.sub(r"(://)[^/@\s]+@", r"\1[redacted]@", value)


def redact_report_event(event):
    event = copy.deepcopy(event)
    for field in ("state", "detail", "source"):
        event[field] = redact_report_text(event[field])
    event["evidence"] = [redact_report_text(link) for link in event["evidence"]]
    return event


def event_fingerprint(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_project_policy(policy, binding):
    fields = {"schema_version", "project_id", "root", "worker_registrations", "legacy_leaseless_beads"}
    require(isinstance(policy, dict) and set(policy) == fields,
            "project policy requires exactly the documented fields")
    require(type(policy["schema_version"]) is int and policy["schema_version"] == 1,
            "unsupported project policy schema")
    require(string(policy["project_id"]) and policy["project_id"] == binding["project_id"],
            "project policy identity mismatch")
    require(string(policy["root"]) and Path(policy["root"]).is_absolute(),
            "project policy root must be absolute")
    try:
        require(Path(policy["root"]).resolve(strict=True) == Path(binding["root"]).resolve(strict=True),
                "project policy root mismatch")
    except (OSError, RuntimeError) as exc:
        raise InvalidEvent("project policy root is unavailable") from exc
    registrations = policy["worker_registrations"]
    require(isinstance(registrations, list), "worker_registrations must be a list")
    roles, thread_ids = set(), set()
    for item in registrations:
        require(isinstance(item, dict) and set(item) == {"worker", "thread_id"},
                "worker registration policy requires worker and thread_id")
        role, thread_id = item["worker"], item["thread_id"]
        require(worker_role(role) and native_thread_id(thread_id),
                "invalid worker registration policy")
        require(role not in roles and thread_id not in thread_ids, "duplicate worker registration policy")
        roles.add(role)
        thread_ids.add(thread_id)
    legacy = policy["legacy_leaseless_beads"]
    require(isinstance(legacy, list), "legacy_leaseless_beads must be a list")
    pairs = set()
    for item in legacy:
        require(isinstance(item, dict) and set(item) == {"beads_id", "assignee"},
                "legacy Beads policy requires beads_id and assignee")
        bead_id, assignee = item["beads_id"], item["assignee"]
        require(isinstance(bead_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", bead_id),
                "invalid legacy Beads ID")
        require(native_thread_id(assignee),
                "invalid legacy Beads assignee")
        require((bead_id, assignee) not in pairs, "duplicate legacy Beads policy")
        pairs.add((bead_id, assignee))
    return policy


def validate_event(event, *, received_at=None):
    require(isinstance(event, dict), "event must be an object")
    op = event.get("op")
    require(isinstance(op, str) and op in {"init", "add", "observe", "prepare", "issue", "ack", "result", "reconcile", "retire", "report", "status", "recover", "rebind", "register", "summary"}, "invalid event op")
    if op not in {"status", "recover", "summary"}:
        require(string(event.get("event_id")), "event_id required")
    if op == "init":
        project = event.get("project")
        require(isinstance(project, dict), "project must be an object")
        for field in ("root", "project_id", "linear_ref", "beads_ref", "coordinator_thread_id"):
            require(string(project.get(field)), f"project.{field} required")
        workers = project.get("workers")
        require(isinstance(workers, dict) and bool(workers) and all(worker_role(role) for role in workers),
                "workers must bind nonempty safe role names")
        require(all(native_thread_id(value) for value in workers.values()), "worker thread IDs must be nonempty exact strings")
    else:
        require(string(event.get("project_id")) and string(event.get("root")), "project_id and root required")
    if op == "add":
        task = event.get("task")
        require(isinstance(task, dict), "task must be an object")
        fields = {"id", "linear_ref", "beads_ref", "worker", "prompt", "source_digest", "scope_digest", "dependencies", "writer_paths"}
        require(set(task) == fields, "task requires exactly the documented fields; reserved/unknown fields forbidden")
        for field in fields - {"dependencies", "writer_paths"}:
            require(string(task[field]), f"task.{field} required")
        require(worker_role(task["worker"]), "invalid worker role")
        for field in ("dependencies", "writer_paths"):
            require(isinstance(task[field], list) and all(string(value) for value in task[field]), f"task.{field} must be a list of nonempty strings")
    if op == "summary":
        require(type(event.get("offset", 0)) is int and event.get("offset", 0) >= 0, "invalid status offset")
        require(type(event.get("limit", 50)) is int and 1 <= event.get("limit", 50) <= 100, "invalid status limit")
        require(type(event.get("report_offset", 0)) is int and event.get("report_offset", 0) >= 0, "invalid report offset")
        require(type(event.get("report_limit", 50)) is int and 1 <= event.get("report_limit", 50) <= 100, "invalid report limit")
    if op == "report":
        fields = {"op", "event_id", "project_id", "root", "task_id", "reporter", "reporter_thread_id", "kind", "occurred_at", "state", "detail", "source", "evidence", "claim_id", "dispatch_id"}
        require(set(event) <= fields and fields - {"dispatch_id"} <= set(event), "report requires only the documented fields")
        require(worker_role(event["reporter"]), "invalid report reporter")
        require(native_thread_id(event["reporter_thread_id"]), "invalid report reporter thread")
        require(event["kind"] in {"lifecycle", "progress", "blocker", "handoff", "result"}, "invalid report kind")
        require(string(event["task_id"]) and string(event["state"]) and len(event["state"]) <= 512,
                "report task_id and state required; state must be at most 512 characters")
        require(string(event["detail"]) and len(event["detail"]) <= 2000, "report detail must be 1-2000 characters")
        require(string(event["source"]) and len(event["source"]) <= 512, "report source link must be bounded")
        require(isinstance(event["evidence"], list) and len(event["evidence"]) <= 16 and all(string(v) and len(v) <= 512 for v in event["evidence"]), "report evidence must be at most 16 bounded links")
        require(string(event["occurred_at"]) and len(event["occurred_at"]) <= 64,
                "report occurred_at must be at most 64 characters")
        try:
            occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
            require(occurred.tzinfo is not None, "report occurred_at requires timezone")
            occurred_utc = occurred.astimezone(timezone.utc)
        except (TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise InvalidEvent("invalid report occurred_at") from exc
        if received_at is None:
            received_at = datetime.now(timezone.utc)
        elif isinstance(received_at, str):
            received_at = datetime.fromisoformat(received_at)
        require(isinstance(received_at, datetime) and received_at.tzinfo is not None,
                "report receipt time must be timezone-aware")
        require(occurred_utc <= received_at.astimezone(timezone.utc) + timedelta(minutes=5),
                "report occurred_at exceeds five-minute future tolerance")
        canonical_occurred_at = occurred_utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for optional in ("claim_id", "dispatch_id"):
            require(optional not in event or string(event[optional]), f"{optional} must be nonempty when present")
    else:
        canonical_occurred_at = None
    if op == "register":
        require(set(event) == {"op", "event_id", "project_id", "root", "worker", "new_thread_id", "expected_workers", "evidence"}, "register requires exactly the documented fields")
        require(worker_role(event.get("worker")), "register requires a valid worker role")
        require(native_thread_id(event.get("new_thread_id")),
                "register requires a worker thread ID")
        workers = event.get("expected_workers")
        require(isinstance(workers, dict) and bool(workers) and all(worker_role(role) for role in workers)
                and all(native_thread_id(value) for value in workers.values()), "expected_workers must bind safe worker roles")
        require(string(event.get("evidence")) and event["evidence"].strip(), "registration evidence reference required")
    if op == "rebind":
        require(worker_role(event.get("worker")), "invalid worker role")
        for field in ("worker", "expected_old_thread_id", "new_thread_id", "evidence"):
            require(string(event.get(field)), f"{field} required")
    if op == "observe":
        require(worker_role(event.get("worker")), "invalid worker role")
        require(event.get("state") in ("IDLE", "BUSY", "UNKNOWN"), "invalid worker state")
        require(string(event.get("evidence")), "observation evidence reference required")
    if op in {"prepare", "issue", "ack", "result", "reconcile"}:
        require(string(event.get("task_id")), "task_id required")
    if op == "retire":
        require(string(event.get("task_id")), "task_id required")
        require(string(event.get("source_digest")) and string(event.get("scope_digest")), "source_digest and scope_digest required")
        require(event.get("quiescent") is True, "retire requires explicit quiescence confirmation")
        require(string(event.get("evidence")) and event["evidence"].strip(), "retirement evidence reference required")
    if op in {"issue", "ack", "result", "reconcile"}:
        require(string(event.get("dispatch_id")), "dispatch_id required")
    if op == "prepare":
        require(string(event.get("source_digest")) and string(event.get("scope_digest")), "source_digest and scope_digest required")
    if op in {"ack", "result"}:
        validate_file(event.get("receipt"))
    return canonical_occurred_at


def validate_file(value):
    require(isinstance(value, dict) and string(value.get("path")), "receipt path required")
    sha = value.get("sha256")
    require(isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), "receipt sha256 required")


def reject_cycles(tasks):
    visited, active = set(), set()

    def visit(task_id):
        require(task_id not in active, f"dependency cycle involving {task_id}")
        if task_id in visited or task_id not in tasks:
            return
        active.add(task_id)
        for dependency in tasks[task_id]["dependencies"]:
            visit(dependency)
        active.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def resolved_claims(model, task):
    root = model["project"]["root"]
    return sorted(set(canonical(root, path) for path in task.get("writer_claims", task["writer_paths"])))

def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(root, value):
    require(string(value), "path must be a nonempty string")
    path = (Path(root) / value).resolve()
    require(path.is_relative_to(root), "path escapes project root")
    return str(path)


def artifacts_valid(model, artifacts):
    if not artifacts:
        return False
    try:
        return all(
            isinstance(a, dict)
            and type(a["size_bytes"]) is int
            and Path(canonical(model["project"]["root"], a["path"])).stat().st_size == a["size_bytes"]
            and len(a["sha256"]) == 64
            and digest(canonical(model["project"]["root"], a["path"])) == a["sha256"]
            for a in artifacts
        )
    except (OSError, KeyError, TypeError, ValueError):
        return False


def dispatch_thread(model, task):
    return task.get("dispatch_thread_id", model["project"]["workers"][task["worker"]])


def completion_valid(model, task):
    if not artifacts_valid(model, task.get("artifacts")):
        return False
    try:
        result = receipt_file(model, {"receipt": task["result_receipt"]})
        if result.get("thread_id") != dispatch_thread(model, task):
            return False
        native = result["native_evidence"]
        if digest(canonical(model["project"]["root"], native["path"])) != native["sha256"]:
            return False
        if task.get("ack"):
            receipt_file(model, {"receipt": task["ack"]})
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False


def readiness(model, task):
    reasons = []
    try:
        claims = resolved_claims(model, task)
        if claims != task["writer_paths"]:
            reasons.append(f"claim-identity-changed:{task['id']}:replace task identity after reconciliation")
    except (OSError, ValueError, RuntimeError):
        claims = task["writer_paths"]
        reasons.append(f"claim-unresolvable:{task['id']}")
    for dep in task["dependencies"]:
        prerequisite = model["tasks"].get(dep)
        if not prerequisite or prerequisite["status"] != "COMPLETE":
            reasons.append(f"dependency:{dep}:{prerequisite['status'] if prerequisite else 'MISSING'}")
        elif not completion_valid(model, prerequisite):
            reasons.append(f"dependency:{dep}:ARTIFACT_DRIFT")
    if model["workers"].get(task["worker"], {}).get("state") != "IDLE":
        reasons.append(f"worker:{task['worker']}:not-observed-idle")
    for tid, other in model["tasks"].items():
        if tid == task["id"] or not other.get("lease"):
            continue
        if other["worker"] == task["worker"]:
            reasons.append(f"worker-lease:{tid}:{other['status']}")
        occupied_claims = set(other["writer_paths"])
        try:
            occupied_claims.update(resolved_claims(model, other))
            occupied_claims.update(canonical(model["project"]["root"], path) for path in other["writer_paths"])
        except (OSError, ValueError, RuntimeError):
            if claims:
                reasons.append(f"writer-lease:{tid}:claim-unresolvable:{other['status']}")
        for path in set(claims) | set(task["writer_paths"]):
            for occupied in occupied_claims:
                if Path(path).is_relative_to(occupied) or Path(occupied).is_relative_to(path):
                    reasons.append(f"writer-lease:{tid}:{occupied}:{other['status']}")
    return sorted(set(reasons))


def normalize_native(raw):
    """Accept actual MCP content text or structured payload, without invented IDs."""
    require(isinstance(raw, dict), "native receipt must be an object")
    require(raw.get("isError") is not True, "native receipt reports isError")
    if "threadId" in raw:
        require(string(raw["threadId"]), "native threadId must be a string")
        return raw
    structured = raw.get("structuredContent")
    require(structured is None or isinstance(structured, dict), "native structuredContent must be an object")
    if isinstance(structured, dict) and "threadId" in structured:
        require(string(structured["threadId"]), "native threadId must be a string")
        return structured
    content = raw.get("content", [])
    require(isinstance(content, list), "native content must be a list")
    for item in content:
        require(isinstance(item, dict), "native content item must be an object")
        if item.get("type") == "text":
            require(isinstance(item.get("text"), str), "native content text must be a string")
            try:
                payload = json.loads(item["text"])
            except (ValueError, KeyError):
                continue
            if isinstance(payload, dict) and "threadId" in payload:
                require(string(payload["threadId"]), "native threadId must be a string")
                return payload
    raise InvalidEvent("native receipt has no supported threadId payload")


def receipt_file(model, event):
    receipt = event["receipt"]
    validate_file(receipt)
    path = canonical(model["project"]["root"], receipt["path"])
    require(digest(path) == receipt["sha256"], "receipt hash mismatch")
    with open(path) as stream:
        value = json.load(stream)
    require(isinstance(value, dict), "receipt JSON must be an object")
    return value


def transition(state):
    model = copy.deepcopy(state.get("model", {}))
    event = state["event"]
    received_at = (state.get("event_received_at")
                   if state.get("event_received_at_event_id") == event.get("event_id")
                   else None)
    validate_event(event, received_at=received_at)
    op = event["op"]
    response = {"action": "OK"}
    project_policy = state.get("project_policy")
    if op not in {"status", "recover", "summary"}:
        require(isinstance(event.get("event_id"), str) and event["event_id"], "event_id required")
        event_hash = (state.get("event_hash")
                      if state.get("event_hash_event_id") == event["event_id"]
                      else event_fingerprint(event))
        prior = model.get("events", {}).get(event["event_id"])
        if prior:
            require(prior["sha256"] == event_hash, "event_id reused with conflicting payload")
            return {"model": model, "response": {"action": "REPLAY", "original_action": prior["action"]}}
    if op == "init":
        project = copy.deepcopy(event["project"])
        project["root"] = str(Path(project["root"]).resolve(strict=True))
        for field in ("project_id", "linear_ref", "beads_ref", "coordinator_thread_id"):
            require(isinstance(project.get(field), str) and project[field], f"project.{field} required")
        require(isinstance(project["workers"], dict) and bool(project["workers"])
                and all(worker_role(role) for role in project["workers"]),
                "workers must bind nonempty safe role names")
        ids = list(project["workers"].values()) + [project["coordinator_thread_id"]]
        require(all(native_thread_id(value) for value in ids) and len(set(ids)) == len(ids),
                "worker and coordinator thread IDs must be nonempty and distinct")
        if model:
            require(model["project"] == project, "database already bound to a different project")
        else:
            model = {"project": project, "tasks": {}, "workers": {}, "events": {}}
    else:
        require(model, "init required")
        require(event.get("project_id") == model["project"]["project_id"], "project_id binding mismatch")
        require(str(Path(event.get("root", "")).resolve()) == model["project"]["root"], "root binding mismatch")
        # An earlier ISSUING transition may have exposed a tool request. Never repeat it.
        for task in model["tasks"].values():
            if op not in {"rebind", "register", "report"} and task["status"] == "ISSUING":
                task["status"] = "UNKNOWN"
                task["reason"] = "native send uncertain; reconcile retained receipts"
        if op == "add":
            task = copy.deepcopy(event["task"])
            for field in ("id", "linear_ref", "beads_ref", "worker", "prompt", "source_digest", "scope_digest"):
                require(isinstance(task.get(field), str) and task[field], f"task.{field} required")
            require(task["worker"] in model["project"]["workers"], "unbound worker")
            require(isinstance(task.get("dependencies"), list), "dependencies list required")
            require(task["id"] not in task["dependencies"], "self dependency")
            require(isinstance(task.get("writer_paths"), list), "writer_paths list required; [] for read-only")
            task["writer_claims"] = task["writer_paths"][:]
            task["writer_paths"] = resolved_claims(model, task)
            require(task["id"] not in model["tasks"], "task already exists; use prepare for reuse")
            task["prompt_sha256"] = hashlib.sha256(task["prompt"].encode()).hexdigest()
            task.update(status="WAIT", lease=False)
            model["tasks"][task["id"]] = task
            reject_cycles(model["tasks"])
        elif op == "report":
            require(event["task_id"] in model["tasks"], "report task is not bound to this project")
            task = model["tasks"][event["task_id"]]
            require(event["reporter"] == task["worker"], "reporter does not own the bound task")
            require(event["reporter_thread_id"] == model["project"]["workers"][event["reporter"]], "reporter thread binding mismatch")
            require(event["claim_id"] == task["beads_ref"], "report claim binding mismatch")
            if "dispatch_id" in event:
                require(event["dispatch_id"] == task.get("dispatch_id"), "report dispatch binding mismatch")
            recorded = datetime.now(timezone.utc).isoformat()
            report = {k: copy.deepcopy(event[k]) for k in ("event_id", "task_id", "reporter", "reporter_thread_id", "kind", "occurred_at", "state", "detail", "source", "evidence", "claim_id", "dispatch_id") if k in event}
            for field in ("state", "detail", "source"):
                report[field] = redact_report_text(report[field])
            report["evidence"] = [redact_report_text(link) for link in report["evidence"]]
            report["recorded_at"] = recorded
            model.setdefault("reports", []).append(report)
            response = {"action": "REPORTED", "event_id": event["event_id"], "recorded_at": recorded}
        elif op == "register":
            policy = validate_project_policy(project_policy, event)
            require(any(item["worker"] == event["worker"] and item["thread_id"] == event["new_thread_id"]
                        for item in policy["worker_registrations"]),
                    "registration is not authorized by project policy")
            bindings = model["project"]["workers"]
            require(event["worker"] not in bindings and event["worker"] not in model["workers"],
                    "worker already bound or observed")
            require(bindings == event["expected_workers"], "expected worker bindings mismatch")
            require(event["new_thread_id"] not in [*bindings.values(), model["project"]["coordinator_thread_id"]], "registered thread must be distinct")
            before = json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
            bindings[event["worker"]] = event["new_thread_id"]
            model["workers"][event["worker"]] = {"state": "UNKNOWN", "evidence": event["evidence"]}
            protected = copy.deepcopy(model)
            del protected["project"]["workers"][event["worker"]]
            del protected["workers"][event["worker"]]
            after = json.dumps(protected, sort_keys=True, separators=(",", ":")).encode()
            require(before == after, "registration changed protected coordinator state")
            response = {"action": "REGISTERED", "worker": event["worker"], "new_thread_id": event["new_thread_id"],
                        "preservation": {"before_sha256": hashlib.sha256(before).hexdigest(),
                                         "after_sha256": hashlib.sha256(after).hexdigest(),
                                         "task_count": len(protected["tasks"]),
                                         "worker_binding_count": len(protected["project"]["workers"]),
                                         "worker_observation_count": len(protected["workers"]),
                                         "event_count": len(protected["events"])}}
        elif op == "rebind":
            worker = event["worker"]
            bindings = model["project"]["workers"]
            require(worker in bindings, "unbound worker")
            require(bindings[worker] == event["expected_old_thread_id"], "expected old thread binding mismatch")
            require(event["new_thread_id"] not in [*bindings.values(), model["project"]["coordinator_thread_id"]], "replacement thread must be distinct")
            require(model["workers"].get(worker, {}).get("state") != "BUSY", "worker observed busy; reconcile first")
            require(not any(t["worker"] == worker and (t.get("lease") or t["status"] in {"PREPARED", "ISSUING", "UNKNOWN", "ACKNOWLEDGED"}) for t in model["tasks"].values()), "worker has held or uncertain dispatch; reconcile first")
            # Freeze legacy receipts to their original target before changing the role.
            for task in model["tasks"].values():
                if task["worker"] == worker and task.get("dispatch_id"):
                    task.setdefault("dispatch_thread_id", bindings[worker])
            bindings[worker] = event["new_thread_id"]
            model["workers"][worker] = {"state": "UNKNOWN", "evidence": event["evidence"]}
            model.setdefault("rebindings", []).append({k: event[k] for k in ("event_id", "worker", "expected_old_thread_id", "new_thread_id", "evidence")})
            response = {"action": "REBOUND", "worker": worker, "old_thread_id": event["expected_old_thread_id"], "new_thread_id": event["new_thread_id"]}
        elif op == "observe":
            worker = event["worker"]
            require(worker in model["project"]["workers"], "unbound worker")
            require(event["state"] in {"IDLE", "BUSY", "UNKNOWN"}, "invalid worker state")
            require(isinstance(event.get("evidence"), str) and event["evidence"], "observation evidence reference required")
            model["workers"][worker] = {"state": event["state"], "evidence": event["evidence"]}
        elif op == "retire":
            require(event.get("task_id") in model["tasks"], "unknown task_id")
            task = model["tasks"][event["task_id"]]
            require(event["source_digest"] == task["source_digest"] and event["scope_digest"] == task["scope_digest"], "source/scope identity mismatch")
            require(task["status"] in {"WAIT", "READY"} and task.get("dispatch_id") is None
                    and task.get("dispatch_thread_id") is None and task.get("lease") is False,
                    "retire requires a never-dispatched WAIT/READY task without a lease")
            task.update(status="RETIRED", lease=False, retirement={
                "event_id": event["event_id"], "source_digest": event["source_digest"],
                "scope_digest": event["scope_digest"], "quiescent": True, "evidence": event["evidence"]})
            response = {"action": "RETIRED", "task_id": task["id"], "status": task["status"], "lease": False}
        elif op in {"prepare", "issue", "ack", "result", "reconcile"}:
            require(event.get("task_id") in model["tasks"], "unknown task_id")
            task = model["tasks"][event["task_id"]]
            if op in {"prepare", "issue"}:
                require(task["status"] != "RETIRED", "retired task cannot be prepared or issued; use a new task identity")
            if op == "prepare":
                require(event.get("source_digest") == task["source_digest"] and event.get("scope_digest") == task["scope_digest"], "source/scope identity mismatch; no reuse or dispatch")
                if task["status"] == "COMPLETE":
                    require(completion_valid(model, task), "completed artifact/receipt drift; immutable completion requires a new authorized task identity and replacement dependents")
                    response = {"action": "REUSE", "task_id": task["id"], "dispatch_id": task["dispatch_id"]}
                elif task.get("dispatch_id"):
                    response = {"action": "RECONCILE", "task_id": task["id"], "status": task["status"]}
                else:
                    reasons = readiness(model, task)
                    if reasons:
                        response = {"action": "WAIT", "reasons": reasons}
                    else:
                        task.update(status="PREPARED", lease=True, dispatch_id=str(uuid.uuid4()),
                                    dispatch_thread_id=model["project"]["workers"][task["worker"]])
                        response = {"action": "PREPARED", "task_id": task["id"], "dispatch_id": task["dispatch_id"]}
            else:
                require(event.get("dispatch_id") == task.get("dispatch_id") and task.get("dispatch_id"), "dispatch binding mismatch")
                if op == "issue":
                    if task["status"] == "PREPARED":
                        reasons = readiness(model, task)
                        if reasons:
                            response = {"action": "WAIT", "reasons": reasons}
                        else:
                            task["status"] = "ISSUING"
                            model["workers"][task["worker"]] = {"state": "BUSY", "evidence": f"dispatch:{task['dispatch_id']}"}
                            response = {"action": "ISSUING", "task_id": task["id"], "dispatch_id": task["dispatch_id"]}
                    else:
                        response = {"action": "RECONCILE", "status": task["status"]}
                elif op == "ack":
                    task.setdefault("dispatch_thread_id", model["project"]["workers"][task["worker"]])
                    require(task["status"] in {"UNKNOWN", "ACKNOWLEDGED"}, "ack requires issued dispatch")
                    native = normalize_native(receipt_file(model, event))
                    require(native["threadId"] == dispatch_thread(model, task), "native target mismatch")
                    task.update(status="ACKNOWLEDGED", ack=event["receipt"])
                elif op == "result":
                    task.setdefault("dispatch_thread_id", model["project"]["workers"][task["worker"]])
                    require(task.get("lease") is True, "dispatch lease already released; late result rejected")
                    require(task["status"] in {"UNKNOWN", "ACKNOWLEDGED", "FAILED"}, "result requires issued dispatch")
                    result = receipt_file(model, event)
                    require(result.get("thread_id") == dispatch_thread(model, task), "result target mismatch")
                    require(result.get("dispatch_id") == task["dispatch_id"], "result dispatch mismatch")
                    require(result.get("source_digest") == task["source_digest"] and result.get("scope_digest") == task["scope_digest"], "result source/scope identity mismatch")
                    require(isinstance(result.get("native_evidence"), dict), "native_evidence file/hash required")
                    validate_file(result["native_evidence"])
                    native_path = canonical(model["project"]["root"], result["native_evidence"]["path"])
                    require(digest(native_path) == result["native_evidence"]["sha256"], "native evidence drift")
                    with open(native_path) as stream:
                        native_text = stream.read()
                    require(task["dispatch_id"] in native_text, "native evidence lacks dispatch marker")
                    require(dispatch_thread(model, task) in native_text, "native evidence lacks target thread")
                    terminal = result.get("terminal") is True
                    quiescent = terminal and result.get("quiescent") is True
                    success = (quiescent and type(result.get("child_exit_code")) is int
                               and result["child_exit_code"] == 0 and result.get("acceptance") is True
                               and artifacts_valid(model, result.get("artifacts")))
                    task.update(status="COMPLETE" if success else "FAILED" if terminal else "UNKNOWN",
                                lease=not (success or quiescent), result_receipt=event["receipt"])
                    if success:
                        task["artifacts"] = result["artifacts"]
                    response = {"action": task["status"], "lease": task["lease"]}
                else:
                    require(event.get("quiescent") is True and isinstance(event.get("evidence"), str) and event["evidence"], "reconcile requires quiescence evidence")
                    require(task["status"] in {"UNKNOWN", "FAILED", "PREPARED", "ACKNOWLEDGED"}, "cannot abandon immutable complete task")
                    task.update(status="FAILED", lease=False, reason=event["evidence"])
                    response = {"action": "FAILED", "lease": False}
        elif op not in {"status", "recover", "summary"}:
            raise InvalidEvent(f"unknown op: {op}")
    if op not in {"status", "recover", "summary"}:
        model["events"][event["event_id"]] = {"sha256": event_hash, "action": response["action"]}
    return {"model": model, "response": response}


def compute_readiness(state):
    model = copy.deepcopy(state["model"])
    if state.get("event", {}).get("op") == "register":
        return {"model": model}
    for task in model["tasks"].values():
        if task["status"] in {"WAIT", "READY"}:
            task["reasons"] = readiness(model, task)
            task["status"] = "WAIT" if task["reasons"] else "READY"
    return {"model": model}


def build_graph(saver):
    builder = StateGraph(State)
    builder.add_node("transition", transition)
    builder.add_node("readiness", compute_readiness)
    builder.add_edge(START, "transition")
    builder.add_edge("transition", "readiness")
    builder.add_edge("readiness", END)
    return builder.compile(checkpointer=saver)


def apply_event(db, event, *, guard_context=None, project_policy=None):
    """Commit all checkpoints synchronously before returning any native intent.

    Safe to kill the caller after this returns: subsequent calls never return the
    same native request. New reservations/issue intents require native Beads
    guard_context; historical replay/reuse/reconciliation does not. No promise
    of exactly-once delivery across MCP transport.
    """
    event_received_at = datetime.now(timezone.utc).isoformat()
    canonical_occurred_at = validate_event(event, received_at=event_received_at)
    event_hash = event_fingerprint(event)
    persisted_event = redact_report_event(event) if event["op"] == "report" else event
    if event["op"] == "report":
        persisted_event["occurred_at"] = canonical_occurred_at
    if project_policy is not None:
        binding = event["project"] if event["op"] == "init" else event
        validate_project_policy(project_policy, binding)
    if event["op"] == "register":
        validate_project_policy(project_policy, event)
    if guard_context is not None:
        require(event["op"] in {"prepare", "issue"}, "Beads guard supports prepare and issue only")
        require(isinstance(guard_context, dict) and set(guard_context) == {"beads_dir", "assignee", "executable"},
                "Beads guard context requires exactly beads_dir, assignee and executable")
    db = Path(db).resolve()
    if event["op"] == "summary":
        return read_status(db, event)
    db.parent.mkdir(parents=True, exist_ok=True)
    with open(str(db) + ".lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(db, check_same_thread=False) as conn:
            conn.execute("PRAGMA synchronous=FULL")
            saver = SqliteSaver(conn)
            graph = build_graph(saver)
            config = {"configurable": {"thread_id": "project-coordinator-v1"}}
            snapshot = graph.get_state(config)
            resumed = bool(snapshot.next)
            require(not (resumed and event["op"] in {"rebind", "register", "prepare", "issue"}), "pending checkpoint; recover and reconcile before prepare/issue/rebind/register")
            if resumed:
                graph.invoke(None, config, durability="sync")
            # Validate before recording input: an invalid event cannot poison recovery.
            before = graph.get_state(config).values
            candidate = transition({"model": before.get("model", {}), "event": persisted_event, "event_hash": event_hash,
                                    "event_hash_event_id": event.get("event_id", ""),
                                    "event_received_at": event_received_at,
                                    "event_received_at_event_id": event.get("event_id", ""),
                                    "project_policy": project_policy})
            compute_readiness({**candidate, "event": event})
            guard = None
            inputs = {"event": persisted_event, "event_hash": event_hash,
                      "event_hash_event_id": event.get("event_id", ""),
                      "event_received_at": event_received_at,
                      "event_received_at_event_id": event.get("event_id", "")}
            if event["op"] == "register":
                inputs["project_policy"] = project_policy
            if event["op"] in {"prepare", "issue"} and candidate["response"]["action"] != "REPLAY":
                task = before["model"]["tasks"][event["task_id"]]
                # Readiness may change externally between preview and commit; gate the stable dispatch state.
                if (event["op"] == "prepare" and not task.get("dispatch_id")) or (event["op"] == "issue" and task["status"] == "PREPARED"):
                    require(guard_context is not None, "new prepare/issue requires explicit Beads guard context")
                    from .beads import check_beads
                    guard = check_beads(db, event, project_policy=project_policy, **guard_context)
                    claim = {field: guard[field] for field in ("beads_id", "assignee", "linear_ref")}
                    if event["op"] == "issue":
                        require(task.get("beads_claim") == claim,
                                "prepared Beads claim missing or changed; explicit Root reconciliation required")
                    else:
                        # The input checkpoint must retain the pin even if preparation is interrupted.
                        inputs["model"] = copy.deepcopy(before["model"])
                        inputs["model"]["tasks"][event["task_id"]]["beads_claim"] = claim
            state = graph.invoke(inputs, config, durability="sync")
            conn.commit()
            response = copy.deepcopy(state["response"])
            if guard is not None:
                response["beads_preflight"] = guard
            response["checkpoint_id"] = graph.get_state(config).config["configurable"]["checkpoint_id"]
            if event["op"] == "issue" and response["action"] == "ISSUING":
                task = state["model"]["tasks"][event["task_id"]]
                response["native_request"] = {
                    "tool": "mcp__codex_app__send_message_to_thread",
                    "arguments": {"threadId": dispatch_thread(state["model"], task),
                                  "prompt": f"[dispatch_id:{task['dispatch_id']}]\n{task['prompt']}"},
                }
            if event["op"] in {"status", "recover"}:
                response["project"] = state["model"]["project"]
                response["workers"] = state["model"]["workers"]
                response["tasks"] = [{k: v for k, v in task.items() if k not in {"prompt"}}
                                     for task in state["model"]["tasks"].values()]
                response["resumed_checkpoint"] = resumed
            return response


def compact_status(state, event, checkpoint_id=None, pending=()):
    """Bounded projection; never exposes stored events, prompts or native arguments."""
    model = state["model"]
    tasks = sorted(model["tasks"].values(), key=lambda t: (not t.get("lease", False), t["id"]))
    offset, limit = event.get("offset", 0), event.get("limit", 50)
    truncated = set()

    def bounded(value, path):
        if isinstance(value, str) and len(value) > 512:
            truncated.add(path)
            return value[:512]
        if isinstance(value, list):
            if len(value) > 32:
                truncated.add(path)
            return [bounded(v, path) for v in value[:32]]
        if isinstance(value, dict):
            return {k: bounded(v, f"{path}.{k}") for k, v in value.items()}
        return value

    fields = {"id", "worker", "status", "lease", "dispatch_id", "dispatch_thread_id", "linear_ref", "beads_ref", "dependencies", "writer_paths", "reasons"}
    page = []
    for task in tasks[offset:offset + limit]:
        item = {k: v for k, v in task.items() if k in fields}
        if task["status"] == "ISSUING":
            item["status"] = "UNKNOWN"
        page.append(bounded(item, f"tasks.{offset + len(page)}"))
    roles = {role for role in model["project"]["workers"] if worker_role(role)}
    all_roles = set(model["project"]["workers"]) | set(model["workers"])
    if all_roles - roles:
        truncated.add("workers.extra_roles")
    project = {k: model["project"][k] for k in ("project_id", "root", "linear_ref", "beads_ref", "coordinator_thread_id")}
    project["workers"] = {k: model["project"]["workers"][k] for k in sorted(roles)}
    uncertain = sum(bool(t.get("lease")) and t["status"] in {"ISSUING", "UNKNOWN", "FAILED"} for t in tasks)
    reports = sorted(model.get("reports", []), key=lambda r: (datetime.fromisoformat(r["occurred_at"].replace("Z", "+00:00")).astimezone(timezone.utc), r["event_id"]))
    latest_report_by_task = {}
    for report in reports:
        latest_report_by_task[report["task_id"]] = report
    report_offset, report_limit = event.get("report_offset", 0), event.get("report_limit", 50)
    report_end = max(0, len(reports) - report_offset)
    report_start = max(0, report_end - report_limit)
    report_page = list(reversed(reports[report_start:report_end]))
    bounded_reports = [bounded({**{k: v for k, v in report.items() if k in {"event_id", "task_id", "reporter", "reporter_thread_id", "kind", "occurred_at", "recorded_at", "state", "detail", "source", "evidence", "claim_id", "dispatch_id"}}, "stale": latest_report_by_task[report["task_id"]]["event_id"] != report["event_id"]}, "reports") for report in report_page]
    for task in page:
        latest = latest_report_by_task.get(task["id"])
        if latest:
            task["latest_report"] = bounded(
                {**{k: latest[k] for k in ("event_id", "reporter", "kind", "occurred_at", "recorded_at", "state")}, "stale": False},
                f"tasks.{task['id']}.latest_report")
    return {"action": "OK", "checkpoint_id": checkpoint_id,
            "project": bounded(project, "project"),
            "workers": bounded({k: {"state": model["workers"][k]["state"]}
                                 for k in sorted(roles) if k in model["workers"]}, "workers"),
            "tasks": page, "reports": bounded_reports, "observed_at": datetime.now(timezone.utc).isoformat(),
            "resumed_checkpoint": False, "recovery_required": bool(pending) or bool(uncertain),
            "pending_checkpoint": bool(pending), "uncertain_held_dispatches": uncertain,
            "bounds": {"offset": offset, "limit": limit, "total_tasks": len(tasks),
                       "next_offset": offset + limit if offset + limit < len(tasks) else None,
                       "report_offset": report_offset, "report_limit": report_limit,
                       "total_reports": len(reports), "omitted_reports": max(0, len(reports) - len(bounded_reports)),
                       "next_report_offset": report_offset + report_limit if report_offset + report_limit < len(reports) else None,
                       "previous_report_offset": max(0, report_offset - report_limit) if report_offset > 0 else None,
                       "string_chars": 512, "list_items": 32, "worker_role_count": len(all_roles),
                       "omitted_worker_roles": len(all_roles - roles), "truncated_fields": sorted(truncated)}}


def read_status(db, event):
    # Skip saver.setup: its journal/schema initialization is a write operation.
    require(db.is_file(), "init required")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, check_same_thread=False)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        saver = SqliteSaver(conn)
        # Setup normally detects this legacy schema, but a summary must never migrate it.
        saver._has_task_path = any(row[1] == "task_path" for row in conn.execute("PRAGMA table_info(writes)"))
        saver.is_setup = True
        graph = build_graph(saver)
        snapshot = graph.get_state({"configurable": {"thread_id": "project-coordinator-v1"}})
        state = snapshot.values
        require(bool(state.get("model")), "init required")
        model = state["model"]
        require(event["project_id"] == model["project"]["project_id"], "project_id binding mismatch")
        require(str(Path(event["root"]).resolve()) == model["project"]["root"], "root binding mismatch")
        # Compute a view only: no checkpoint resume, reconciliation or dispatch.
        view = transition({"model": model, "event": event})
        view.update(compute_readiness(view))
        checkpoint = snapshot.config["configurable"]["checkpoint_id"]
        return compact_status(view, event, checkpoint, snapshot.next)
