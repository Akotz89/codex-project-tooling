from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from project_coordinator.http_api import handler
from langgraph.checkpoint.sqlite import SqliteSaver

from project_coordinator.runtime import InvalidEvent, apply_event, build_graph, redact_report_text
from test_runtime import Harness


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def report(h, event_id, *, occurred_at=None, state="working", **changes):
    return {**h.binding, "op": "report", "event_id": event_id, "task_id": "A", "reporter": "worker-1", "reporter_thread_id": h.workers["worker-1"],
            "kind": "progress", "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
            "state": state, "detail": f"Observed {state}", "source": "https://example.test/task", "evidence": ["https://example.test/evidence"],
            "claim_id": "global:bead-1", **changes}


def project_report(h, event_id, *, reporter="worker-1", occurred_at=None, **changes):
    return {**h.binding, "op": "project_report", "event_id": event_id,
            "work_item": "https://linear.example/issue/REAL-123", "reporter": reporter,
            "reporter_thread_id": h.workers.get(reporter, "unregistered-thread"), "kind": "progress",
            "occurred_at": occurred_at or datetime.now(timezone.utc).isoformat(),
            "state": "in progress", "detail": "Observed project work", "source": "https://linear.example/issue/REAL-123",
            "evidence": ["https://evidence.example/actual-result"], **changes}


def test_coordinator_and_registered_project_roles_keep_separate_five_worker_map(tmp_path):
    h = Harness(tmp_path, workers={f"worker-{i}": f"thread{i}" for i in range(1, 6)})
    h.add()
    original_workers = json.dumps(h.call("summary")["project"]["workers"], sort_keys=True, separators=(",", ":")).encode()
    role_threads = {"product_owner": "actual-product-owner-thread", "scrum_master": "actual-scrum-master-thread"}
    policy = {"schema_version": 1, "project_id": h.binding["project_id"], "root": str(h.root),
              "worker_registrations": [],
              "project_report_registrations": [{"role": role, "thread_id": thread} for role, thread in role_threads.items()],
              "legacy_leaseless_beads": []}
    observations = [project_report(h, "root-observation", reporter="coordinator", reporter_thread_id="root"),
                    *(project_report(h, f"{role}-observation", reporter=role, reporter_thread_id=thread)
                      for role, thread in role_threads.items()),
                    *(project_report(h, f"{role}-observation", reporter=role, reporter_thread_id=thread)
                      for role, thread in h.workers.items())]
    observations[0]["detail"] = "Authorization: Bearer project-secret-marker"
    path = tmp_path / "project-report.json"
    event = observations[1]
    path.write_text(json.dumps({k: v for k, v in event.items() if k != "op"}))
    policy_path = tmp_path / "project-policy.json"
    policy_path.write_text(json.dumps(policy))
    result = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), "project-report", "--input", str(path), "--project-policy", str(policy_path)],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout)["action"] == "PROJECT_REPORTED"
    root_path = tmp_path / "root-report.json"
    root_path.write_text(json.dumps({k: v for k, v in observations[0].items() if k != "op"}))
    root_result = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), "project-report", "--input", str(root_path)],
                                 capture_output=True, text=True, check=True)
    assert json.loads(root_result.stdout)["action"] == "PROJECT_REPORTED"
    assert apply_event(h.db, observations[0])["action"] == "REPLAY"
    for observation in observations[1:]:
        role_policy = policy if observation["reporter"] in role_threads else None
        action = apply_event(h.db, observation, project_policy=role_policy)["action"]
        assert action == ("REPLAY" if observation["event_id"] == event["event_id"] else "PROJECT_REPORTED")
        assert apply_event(h.db, observation, project_policy=role_policy)["action"] == "REPLAY"
    with pytest.raises(InvalidEvent, match="conflicting payload"):
        apply_event(h.db, {**observations[1], "state": "changed"}, project_policy=policy)
    with pytest.raises(InvalidEvent, match="coordinator reporter thread binding mismatch"):
        apply_event(h.db, {**observations[0], "event_id": "root-wrong-thread", "reporter_thread_id": "spoofed-root"})
    with pytest.raises(InvalidEvent, match="coordinator reporter thread binding mismatch"):
        apply_event(h.db, {**observations[0], "event_id": "root-spoof", "reporter_thread_id": role_threads["product_owner"]})
    with pytest.raises(InvalidEvent, match="thread binding mismatch|not registered"):
        apply_event(h.db, {**observations[1], "event_id": "po-wrong-thread", "reporter_thread_id": "wrong-thread"}, project_policy=policy)
    with pytest.raises(InvalidEvent, match="project worker reporter thread binding mismatch"):
        apply_event(h.db, {**observations[3], "event_id": "worker-wrong-thread", "reporter_thread_id": "wrong-thread"})
    with pytest.raises(InvalidEvent, match="1-16 bounded evidence"):
        apply_event(h.db, project_report(h, "no-evidence", evidence=[]))
    apply_event(h.db, {**observations[1], "event_id": "older-project-observation",
                       "occurred_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}, project_policy=policy)
    summary = apply_event(h.db, {**h.binding, "op": "summary"})
    project_reports = {r["event_id"]: r for r in summary["project_reports"]}
    assert project_reports["product_owner-observation"]["work_item"] == event["work_item"]
    assert project_reports["product_owner-observation"]["reporter"] == "product_owner"
    assert project_reports["product_owner-observation"]["stale"] is False
    assert project_reports["older-project-observation"]["stale"] is True
    assert "project-secret-marker" not in json.dumps(summary)
    assert summary["project"]["workers"] == h.workers
    assert json.dumps(summary["project"]["workers"], sort_keys=True, separators=(",", ":")).encode() == original_workers
    assert "product_owner" not in summary["project"]["workers"] and "scrum_master" not in summary["project"]["workers"]
    assert {report["reporter"] for report in summary["project_reports"]} == {"coordinator", *role_threads, *h.workers}
    assert summary["reports"] == []
    assert summary["tasks"][0].get("latest_report") is None
    assert summary["tasks"][0]["status"] == "READY"
    assert summary["bounds"]["total_project_reports"] == 9


def test_project_report_api_paginates_and_old_checkpoint_defaults_to_empty(h):
    h.add()
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    for index in range(3):
        role = f"role_{index + 1}"
        policy = {"schema_version": 1, "project_id": h.binding["project_id"], "root": str(h.root),
                  "worker_registrations": [],
                  "project_report_registrations": [{"role": role, "thread_id": f"project-thread-{index + 1}"}],
                  "legacy_leaseless_beads": []}
        apply_event(h.db, project_report(h, f"project-page-{index}", reporter=role,
                                         reporter_thread_id=f"project-thread-{index + 1}",
                                         occurred_at=(base + timedelta(minutes=index)).isoformat()), project_policy=policy)
    page = apply_event(h.db, {**h.binding, "op": "summary", "project_report_limit": 2})
    assert [r["event_id"] for r in page["project_reports"]] == ["project-page-2", "project-page-1"]
    older = apply_event(h.db, {**h.binding, "op": "summary", "project_report_offset": 2, "project_report_limit": 2})
    assert [r["event_id"] for r in older["project_reports"]] == ["project-page-0"]
    assert older["project_reports"][0]["stale"] is False
    assert older["bounds"]["previous_project_report_offset"] == 0
    # A pre-feature checkpoint has no project_reports key; summary remains query-only and compatible.
    with sqlite3.connect(h.db) as conn:
        saver = SqliteSaver(conn)
        graph = build_graph(saver)
        config = {"configurable": {"thread_id": "project-coordinator-v1"}}
        state = graph.get_state(config)
        values = dict(state.values)
        values["model"].pop("project_reports", None)
        graph.update_state(config, {"model": values["model"]})
    old = apply_event(h.db, {**h.binding, "op": "summary"})
    assert old["project_reports"] == []
    assert old["bounds"]["total_project_reports"] == 0


def test_report_cli_retry_conflict_late_order_and_restart(tmp_path):
    h = Harness(tmp_path)
    h.add()
    newest = datetime.now(timezone.utc)
    events = [report(h, "report-new", occurred_at=newest.isoformat(), state="progressed", detail='progress {"api_key":"should-not-leak"}'),
              report(h, "report-late", occurred_at=(newest - timedelta(minutes=5)).isoformat(), state="older")]
    for event in events:
        input_path = tmp_path / f"{event['event_id']}.json"
        input_path.write_text(json.dumps(event))
        result = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), "report", "--input", str(input_path)],
                                capture_output=True, text=True, check=True)
        assert json.loads(result.stdout)["action"] == "REPORTED"
    replay = apply_event(h.db, events[0])
    assert replay["action"] == "REPLAY"
    with pytest.raises(InvalidEvent, match="conflicting payload"):
        apply_event(h.db, {**events[0], "state": "conflict"})
    summary = apply_event(h.db, {**h.binding, "op": "summary", "limit": 50})
    assert [r["event_id"] for r in summary["reports"]] == ["report-new", "report-late"]
    assert summary["tasks"][0]["latest_report"]["event_id"] == "report-new"
    assert summary["reports"][0]["stale"] is False and summary["reports"][1]["stale"] is True
    assert "should-not-leak" not in json.dumps(summary) and "[redacted]" in json.dumps(summary)
    assert summary["reports"][0]["recorded_at"] and summary["observed_at"]
    reopened = apply_event(h.db, {**h.binding, "op": "summary"})
    assert reopened["reports"] == summary["reports"]
    assert "prompt" not in json.dumps(reopened) and "native_request" not in reopened


def test_report_api_paginates_newest_first_and_marks_global_latest(h):
    h.add()
    newest = datetime.now(timezone.utc) - timedelta(minutes=3)
    for index in range(3):
        apply_event(h.db, report(h, f"page-{index}", occurred_at=(newest + timedelta(minutes=index)).isoformat()))
    first = apply_event(h.db, {**h.binding, "op": "summary", "report_limit": 2})
    assert [r["event_id"] for r in first["reports"]] == ["page-2", "page-1"]
    assert first["bounds"]["next_report_offset"] == 2
    assert first["bounds"]["previous_report_offset"] is None
    older = apply_event(h.db, {**h.binding, "op": "summary", "report_offset": 2, "report_limit": 2})
    assert [r["event_id"] for r in older["reports"]] == ["page-0"]
    assert older["bounds"]["next_report_offset"] is None
    assert older["bounds"]["previous_report_offset"] == 0
    assert older["tasks"][0]["latest_report"]["event_id"] == "page-2"
    assert older["reports"][0]["stale"] is True

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(str(h.db), h.binding["project_id"], h.binding["root"], "x" * 32))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = Request(f"http://127.0.0.1:{server.server_port}/api/status?report_offset=2&report_limit=2",
                          headers={"Authorization": "Bearer " + "x" * 32})
        api_page = json.loads(urlopen(request).read())
        assert [r["event_id"] for r in api_page["reports"]] == ["page-0"]
        assert api_page["bounds"]["omitted_reports"] == 2
        html = urlopen(f"http://127.0.0.1:{server.server_port}/").read().decode()
        assert "Older reports" in html and "Newer reports" in html and "omitted_reports" in html
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_report_secrets_are_redacted_before_checkpoint_and_wal(h):
    h.add()
    markers = [b"synthetic-secret-value", b"two word secret", b"synthetic-token-marker"]
    event = report(h, "redaction-checkpoint", state='password="two word secret"',
                   detail="Authorization: Basic synthetic-secret-value", source='https://example.test/?token="synthetic-token-marker"')
    observer = sqlite3.connect(h.db)
    observer.execute("PRAGMA journal_mode=WAL")
    observer.execute("BEGIN")
    observer.execute("SELECT count(*) FROM checkpoints").fetchone()
    try:
        response = apply_event(h.db, event)
        assert response["action"] == "REPORTED"
        wal = Path(str(h.db) + "-wal")
        assert wal.is_file()
        assert all(marker not in wal.read_bytes() for marker in markers)
    finally:
        observer.rollback()
        observer.close()
    summary = json.dumps(apply_event(h.db, {**h.binding, "op": "summary"})).encode()
    assert all(marker not in summary for marker in markers)
    for path in (h.db, Path(str(h.db) + "-wal")):
        if path.exists():
            raw = path.read_bytes()
            assert all(marker not in raw for marker in markers), path.name
    assert "synthetic-secret-value" not in redact_report_text("Authorization: Basic synthetic-secret-value")
    assert "two word secret" not in redact_report_text('password="two word secret"')
    assert apply_event(h.db, event)["action"] == "REPLAY"
    with pytest.raises(InvalidEvent, match="conflicting payload"):
        apply_event(h.db, {**event, "detail": "changed"})


def test_report_state_limit_and_bounded_latest_projection(h):
    h.add()
    accepted = report(h, "report-state-boundary", state="x" * 512)
    assert apply_event(h.db, accepted)["action"] == "REPORTED"
    summary = apply_event(h.db, {**h.binding, "op": "summary"})
    latest_state = summary["tasks"][0]["latest_report"]["state"]
    assert len(latest_state) <= 512

    before = h.db.read_bytes()
    with pytest.raises(InvalidEvent, match="512 characters"):
        apply_event(h.db, report(h, "report-state-too-long", state="x" * 513))
    assert h.db.read_bytes() == before
    assert apply_event(h.db, {**h.binding, "op": "summary"})["tasks"][0]["latest_report"]["event_id"] == accepted["event_id"]


def test_report_rejects_utc_conversion_overflow_before_checkpoint(h):
    h.add()
    before = h.db.read_bytes()
    invalid = report(h, "report-utc-overflow", occurred_at="0001-01-01T00:00:00+14:00")
    with pytest.raises(InvalidEvent, match="invalid report occurred_at"):
        apply_event(h.db, invalid)
    assert h.db.read_bytes() == before
    summary = apply_event(h.db, {**h.binding, "op": "summary"})
    assert summary["bounds"]["total_reports"] == 0


def test_report_rejects_overlong_timestamp_before_checkpoint(h):
    h.add()
    before = h.db.read_bytes()
    overlong = "2026-10-25T00:00:00." + "1" * 100_000 + "Z"
    with pytest.raises(InvalidEvent, match="64 characters"):
        apply_event(h.db, report(h, "report-overlong-timestamp", occurred_at=overlong))
    assert h.db.read_bytes() == before
    assert apply_event(h.db, {**h.binding, "op": "summary"})["bounds"]["total_reports"] == 0


def test_report_canonicalizes_utc_timestamp_and_accepts_small_future_skew(h):
    h.add()
    occurred_utc = datetime.now(timezone.utc) + timedelta(minutes=2)
    offset = timezone(timedelta(hours=-5))
    raw_timestamp = occurred_utc.astimezone(offset).isoformat()
    event = report(h, "report-canonical-time", occurred_at=raw_timestamp)
    assert apply_event(h.db, event)["action"] == "REPORTED"

    expected = occurred_utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    summary = apply_event(h.db, {**h.binding, "op": "summary"})
    stored = summary["reports"][0]["occurred_at"]
    assert stored == expected
    assert len(stored) <= 64 and stored.endswith("Z")
    assert summary["tasks"][0]["latest_report"]["occurred_at"] == expected
    assert apply_event(h.db, event)["action"] == "REPLAY"

    summary_path = h.root / "canonical-summary.json"
    summary_path.write_text(json.dumps({**h.binding, "op": "summary"}))
    restarted = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db),
                                "summary", "--input", str(summary_path)],
                               capture_output=True, text=True, check=True)
    assert json.loads(restarted.stdout)["reports"][0]["occurred_at"] == expected
    with pytest.raises(InvalidEvent, match="conflicting payload"):
        apply_event(h.db, {**event, "occurred_at": expected})


def test_report_rejects_timestamps_over_five_minutes_ahead(h):
    h.add()
    too_far = (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()
    timestamps = [too_far, "9999-12-31T23:59:59Z"]
    for index, timestamp in enumerate(timestamps):
        before = h.db.read_bytes()
        with pytest.raises(InvalidEvent, match="five-minute future tolerance"):
            apply_event(h.db, report(h, f"report-future-{index}", occurred_at=timestamp))
        assert h.db.read_bytes() == before
    assert apply_event(h.db, {**h.binding, "op": "summary"})["bounds"]["total_reports"] == 0


def test_concurrent_report_retry_is_idempotent(h):
    h.add()
    event = report(h, "concurrent-report")
    barrier = Barrier(2)

    def submit():
        barrier.wait(timeout=5)
        return apply_event(h.db, event)["action"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        actions = [future.result(timeout=20) for future in (pool.submit(submit), pool.submit(submit))]
    assert sorted(actions) == ["REPLAY", "REPORTED"]
    summary = apply_event(h.db, {**h.binding, "op": "summary"})
    assert summary["bounds"]["total_reports"] == 1
    assert [item["event_id"] for item in summary["reports"]] == ["concurrent-report"]


def test_report_preserves_live_task_status_lease_and_dispatch(h):
    h.add()
    prepared = h.prepare()
    dispatch_id = prepared["dispatch_id"]
    issued = h.call("issue", task_id="A", dispatch_id=dispatch_id)
    assert issued["action"] == "ISSUING"
    config = {"configurable": {"thread_id": "project-coordinator-v1"}}

    def current_task():
        with sqlite3.connect(h.db) as conn:
            model = build_graph(SqliteSaver(conn)).get_state(config).values["model"]
            return dict(model["tasks"]["A"])

    before = current_task()
    assert before["status"] == "ISSUING" and before["lease"] is True
    response = apply_event(h.db, report(h, "live-report", state="progress", dispatch_id=dispatch_id))
    assert response["action"] == "REPORTED"
    after = current_task()
    for field in ("status", "lease", "dispatch_id", "dispatch_thread_id"):
        assert after[field] == before[field]


def test_report_requires_bound_project_owner_and_matching_claim(h):
    h.add()
    for changes, message in [({"project_id": "P-other"}, "project_id binding mismatch"),
                             ({"reporter": "worker-2"}, "does not own"),
                             ({"claim_id": "global:other"}, "claim binding mismatch"),
                             ({"occurred_at": "yesterday"}, "invalid report occurred_at")]:
        with pytest.raises(InvalidEvent, match=message):
            apply_event(h.db, report(h, f"bad-{message}", **changes))


def test_loopback_api_auth_binding_read_only_and_browser_view(h):
    h.add()
    apply_event(h.db, report(h, "api-report", state="blocked", kind="blocker",
                              detail='Authorization: Basic synthetic-api-secret; password="two word api secret"'))
    apply_event(h.db, project_report(h, "api-project-report", reporter="coordinator", reporter_thread_id="root", state="observed"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(str(h.db), h.binding["project_id"], h.binding["root"], "x" * 32))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        page = urlopen(base + "/").read().decode()
        assert "Project coordinator status" in page and "sessionStorage" not in page and "localStorage" not in page
        with pytest.raises(HTTPError) as unauth:
            urlopen(base + "/api/status")
        assert unauth.value.code == 401
        request = Request(base + "/api/status?limit=100&project_report_offset=0&project_report_limit=10", headers={"Authorization": "Bearer " + "x" * 32})
        response = json.loads(urlopen(request).read())
        assert response["project"]["project_id"] == h.binding["project_id"]
        assert response["reports"][0]["state"] == "blocked"
        assert response["reports"][0]["source"] == "https://example.test/task"
        assert response["project_reports"][0]["work_item"] == "https://linear.example/issue/REAL-123"
        assert "Project observations" in page
        body = json.dumps(response)
        assert "synthetic-api-secret" not in body and "two word api secret" not in body
        assert "prompt" not in json.dumps(response) and "native_request" not in json.dumps(response)
        write = Request(base + "/api/status", data=b"{}", method="POST", headers={"Authorization": "Bearer " + "x" * 32})
        with pytest.raises(HTTPError) as rejected:
            urlopen(write)
        assert rejected.value.code == 405
        wrong_binding = handler(str(h.db), "P-other", h.binding["root"], "x" * 32)
        other = ThreadingHTTPServer(("127.0.0.1", 0), wrong_binding)
        other_thread = Thread(target=other.serve_forever, daemon=True)
        other_thread.start()
        try:
            req = Request(f"http://127.0.0.1:{other.server_port}/api/status", headers={"Authorization": "Bearer " + "x" * 32})
            with pytest.raises(HTTPError) as mismatch:
                urlopen(req)
            assert mismatch.value.code == 400
        finally:
            other.shutdown()
            other.server_close()
            other_thread.join(timeout=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("host", ["0.0.0.0", "::1"])
def test_cli_refuses_non_supported_or_non_loopback_bindings(tmp_path, host):
    result = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(tmp_path / "missing.sqlite"),
                             "serve", "--project-id", "P-test", "--root", str(tmp_path), "--host", host],
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert "API host must be loopback" in result.stderr
