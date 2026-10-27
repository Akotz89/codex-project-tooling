import copy
from datetime import datetime, timedelta, timezone
import gc
import json
import subprocess
import sys

import pytest

from project_coordinator.beads import check_beads, read_status
from project_coordinator.runtime import InvalidEvent, digest
from test_runtime import h, project_policy


@pytest.fixture
def guard(h, tmp_path, monkeypatch):
    directory = tmp_path / ".beads"
    directory.mkdir(exist_ok=True)
    binary = tmp_path / "bd"
    binary.write_text("#!/bin/sh\nexit 99\n")
    binary.chmod(0o700)
    h.add()
    calls = []
    record = {"id": "bead-1", "external_ref": "linear:test-1", "status": "in_progress",
              "assignee": "codex:root:A", "owner": "not-the-assignee", "description": "DO_NOT_EMIT",
              "heartbeat_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
              "lease_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(), "revision": "1"}
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps([copy.deepcopy(record)]), "")
    monkeypatch.setattr("project_coordinator.beads.subprocess.run", run)
    return {"beads_dir": str(directory), "assignee": "codex:root:A", "executable": str(binary)}, record, calls


def event(h, op="prepare", **changes):
    return {**h.binding, "op": op, "task_id": "A", "event_id": "guard-event",
            "source_digest": "source-1", "scope_digest": "scope-1", **changes}


def test_guard_uses_supported_readonly_global_lookup_without_graph_writes(h, guard):
    options, record, calls = guard
    checkpoint = h.call("summary")["checkpoint_id"]
    gc.collect()  # Finish fixture writer checkpoints before asserting read-only bytes.
    before = digest(h.db)
    result = check_beads(h.db, event(h), **options)
    assert result["action"] == "VERIFIED" and result["readonly"] is True
    assert result["beads_id"] == "bead-1" and result["assignee"] == options["assignee"]
    assert result["coordinator_checkpoint_id"] == checkpoint
    assert digest(h.db) == before
    argv, kwargs = calls[0]
    assert argv == [options["executable"], "--readonly", "--global", "show", "bead-1", "--json"]
    assert kwargs["env"]["BEADS_DIR"] == options["beads_dir"]
    assert kwargs["env"]["BEADS_DOLT_SHARED_SERVER"] == "1"
    assert kwargs["check"] is False and kwargs["timeout"] == 30
    assert "DO_NOT_EMIT" not in json.dumps(result) and "owner" not in result


@pytest.mark.parametrize("field,value,message", [
    ("id", "wrong", "identity"), ("status", "closed", "in_progress"),
    ("status", "open", "in_progress"), ("assignee", None, "assignee"),
    ("assignee", "different-worker", "assignee"),
    ("external_ref", "linear:another", "Linear reference"),
])
def test_guard_refuses_conflicting_or_unclaimed_records_before_mutation(h, guard, field, value, message):
    options, record, calls = guard
    record[field] = value
    expected_status = h.call("summary")["tasks"][0]["status"]
    gc.collect()
    before = digest(h.db)
    with pytest.raises(InvalidEvent, match=message):
        check_beads(h.db, event(h), **options)
    assert digest(h.db) == before and h.call("summary")["tasks"][0]["status"] == expected_status


@pytest.mark.parametrize("payload", ["not JSON", "{}", "[]", '[{}, {}]', '[null]'])
def test_guard_refuses_unusable_json(h, guard, monkeypatch, payload):
    options, _, _ = guard
    monkeypatch.setattr("project_coordinator.beads.subprocess.run",
                        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, payload, "secret stderr"))
    with pytest.raises(InvalidEvent) as exc:
        check_beads(h.db, event(h), **options)
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("failure", ["exit", "timeout", "missing"])
def test_guard_backend_failure_is_explicit_and_sanitized(h, guard, monkeypatch, failure):
    options, _, _ = guard
    def run(argv, **kwargs):
        if failure == "exit":
            return subprocess.CompletedProcess(argv, 1, "secret stdout", "secret stderr")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30, output="secret stdout")
        raise FileNotFoundError("secret path")
    monkeypatch.setattr("project_coordinator.beads.subprocess.run", run)
    with pytest.raises(InvalidEvent) as exc:
        check_beads(h.db, event(h), **options)
    assert "secret" not in str(exc.value)


def test_absent_database_and_non_dispatch_command_never_create_state(h, guard, tmp_path):
    options, _, calls = guard
    missing = tmp_path / "not-created" / "state.sqlite"
    with pytest.raises(InvalidEvent, match="init required"):
        check_beads(missing, event(h), **options)
    assert not missing.parent.exists() and not calls
    with pytest.raises(InvalidEvent, match="prepare and issue"):
        check_beads(h.db, event(h, "summary"), **options)
    assert not calls


def test_guard_finds_task_beyond_first_page(h, guard):
    options, _, _ = guard
    for index in range(105):
        h.add(f"0-{index:03}", "worker-2")
    assert check_beads(h.db, event(h), **options)["beads_id"] == "bead-1"


def test_guard_ignores_unrelated_writer_paths_truncation(h, guard):
    options, _, calls = guard
    h.add("B", paths=[f"src/file-{index}.py" for index in range(143)])
    snapshot = h.call("summary")
    task = next(task for task in snapshot["tasks"] if task["id"] == "B")
    assert len(task["writer_paths"]) == 32
    assert "tasks.1.writer_paths" in snapshot["bounds"]["truncated_fields"]
    before = digest(h.db)
    assert check_beads(h.db, event(h, task_id="B"), **options)["beads_id"] == "bead-1"
    assert len(calls) == 2
    assert digest(h.db) == before


@pytest.mark.parametrize("field", ["id", "beads_ref", "linear_ref"])
def test_truncated_binding_identity_refuses_before_beads_or_graph_write(h, guard, monkeypatch, field):
    from project_coordinator import apply_event

    options, _, calls = guard
    def truncated_status(db, request):
        snapshot = read_status(db, request)
        snapshot["bounds"]["truncated_fields"].append(f"tasks.0.{field}")
        return snapshot

    monkeypatch.setattr("project_coordinator.beads.read_status", truncated_status)
    before = digest(h.db)
    with pytest.raises(InvalidEvent, match="task binding is truncated"):
        apply_event(h.db, event(h), guard_context=options)
    assert not calls
    assert digest(h.db) == before


@pytest.mark.parametrize(("task_id", "beads_ref", "linear_ref", "message"), [
    ("missing", "global:bead-1", "linear:test-1", "unknown task_id"),
    ("B", "global:other", "linear:test-1", "identity mismatch"),
    ("B", "global:bead-1", "linear:other", "Linear reference mismatch"),
])
def test_binding_identity_mismatch_refuses_before_acceptance_or_graph_write(
    h, guard, task_id, beads_ref, linear_ref, message
):
    from project_coordinator import apply_event

    options, _, calls = guard
    if task_id != "missing":
        h.call("add", task={"id": task_id, "worker": "worker-1", "linear_ref": linear_ref,
                             "beads_ref": beads_ref, "prompt": "fixture", "source_digest": "source-1",
                             "scope_digest": "scope-1", "writer_paths": [], "dependencies": []})
    before = digest(h.db)
    with pytest.raises(InvalidEvent, match=message):
        apply_event(h.db, event(h, task_id=task_id), guard_context=options)
    assert digest(h.db) == before
    assert len(calls) == (0 if task_id == "missing" else 1)


def test_cli_prepare_and_issue_check_actual_executable_each_time(h, tmp_path):
    directory = tmp_path / ".beads"
    directory.mkdir(exist_ok=True)
    record = directory / "record.json"
    log = directory / "calls.jsonl"
    record.write_text(json.dumps({"id": "bead-1", "external_ref": "linear:test-1", "status": "in_progress", "assignee": "codex:root:A",
                                 "heartbeat_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                                 "lease_expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}))
    binary = tmp_path / "fake-bd"
    binary.write_text("#!/usr/bin/env python3\n" +
                      "import json,os,pathlib,sys\n" +
                      "p=pathlib.Path(os.environ['BEADS_DIR'])\n" +
                      "with (p/'calls.jsonl').open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')\n" +
                      "print(json.dumps([json.loads((p/'record.json').read_text())]))\n")
    binary.chmod(0o700)
    h.add()
    def call(op, body, flags=True):
        path = tmp_path / (op + ".json")
        path.write_text(json.dumps({**h.binding, **body}))
        argv = [sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), op, "--input", str(path)]
        if flags:
            argv += ["--beads-dir", str(directory), "--beads-assignee", "codex:root:A", "--bd-executable", str(binary)]
        return subprocess.run(argv, capture_output=True, text=True)
    prepared = call("prepare", {"event_id": "prepare", "task_id": "A", "source_digest": "source-1", "scope_digest": "scope-1"})
    assert prepared.returncode == 0, prepared.stderr
    response = json.loads(prepared.stdout)
    dispatch = response["dispatch_id"]
    assert response["action"] == "PREPARED" and response["beads_preflight"]["action"] == "VERIFIED"
    issue = {"event_id": "issue", "task_id": "A", "dispatch_id": dispatch}
    # A claim transferred after prepare must prevent issue before ISSUING is stored.
    data = json.loads(record.read_text()); data["assignee"] = "somebody-else"; record.write_text(json.dumps(data))
    before = digest(h.db)
    rejected = call("issue", issue)
    assert rejected.returncode == 2 and "assignee" in rejected.stderr
    assert digest(h.db) == before and h.call("summary")["tasks"][0]["status"] == "PREPARED"
    data["assignee"] = "codex:root:A"; record.write_text(json.dumps(data))
    issued = call("issue", issue)
    assert issued.returncode == 0, issued.stderr
    result = json.loads(issued.stdout)
    assert result["action"] == "ISSUING" and result["native_request"]["arguments"]["threadId"] == "thread1"
    assert result["beads_preflight"]["action"] == "VERIFIED"
    assert log.read_text().count('"--readonly"') == 5
    summary = call("summary", {}, flags=False)
    assert summary.returncode == 0 and "beads_preflight" not in json.loads(summary.stdout)


def test_cli_partial_guard_flags_are_rejected_without_creating_database(tmp_path):
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"project_id": "P", "root": str(tmp_path)}))
    missing = tmp_path / "missing" / "state.sqlite"
    result = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(missing),
                             "summary", "--input", str(path), "--beads-dir", str(tmp_path)],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "together" in result.stderr
    assert not missing.parent.exists()


@pytest.mark.parametrize("changes", [
    {"lease_expires_at": "2000-01-01T00:00:00Z"},
    {"heartbeat_at": "2099-01-01T00:00:00Z"},
    {"heartbeat_at": None}, {"lease_expires_at": None},
    {"heartbeat_at": "invalid"}, {"lease_expires_at": "invalid"},
    {"heartbeat_at": "2026-10-20"}, {"lease_expires_at": "2026-10-20T12:00:00"},
    {"heartbeat_at": 123}, {"lease_expires_at": {}},
    {"heartbeat_at": None, "lease_expires_at": None},
])
def test_api_stale_or_malformed_lease_cannot_checkpoint(h, guard, changes):
    options, record, _ = guard
    record.update(changes)
    checkpoint = h.call("summary")["checkpoint_id"]
    gc.collect()
    before = digest(h.db)
    with pytest.raises(InvalidEvent, match="Beads"):
        from project_coordinator import apply_event
        apply_event(h.db, event(h), guard_context=options)
    assert digest(h.db) == before
    summary = h.call("summary")
    assert summary["checkpoint_id"] == checkpoint
    assert summary["tasks"][0]["lease"] is False


def test_lease_expiring_during_native_lookup_is_rejected_at_equal_now(h, guard, monkeypatch):
    from project_coordinator import apply_event
    options, record, _ = guard
    expiry = datetime(2026, 10, 20, 17, 0, tzinfo=timezone.utc)
    record.update(heartbeat_at=(expiry - timedelta(minutes=1)).isoformat(), lease_expires_at=expiry.isoformat())
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expiry
    monkeypatch.setattr("project_coordinator.beads.datetime", Clock)
    checkpoint = h.call("summary")["checkpoint_id"]
    with pytest.raises(InvalidEvent, match="expired"):
        apply_event(h.db, event(h), guard_context=options)
    assert h.call("summary")["checkpoint_id"] == checkpoint


def test_fresh_timezone_offsets_report_normalized_observed_renewal(h, guard):
    options, record, calls = guard
    zone = timezone(timedelta(hours=-5))
    heartbeat = datetime.now(zone) - timedelta(seconds=1)
    expiry = heartbeat + timedelta(hours=1)
    record.update(heartbeat_at=heartbeat.isoformat(), lease_expires_at=expiry.isoformat())
    result = check_beads(h.db, event(h), **options)
    assert len(calls) == 2
    assert result["lease"]["mode"] == "LEASED"
    assert result["lease"]["heartbeat_at"] == heartbeat.astimezone(timezone.utc).isoformat()
    assert result["lease"]["lease_expires_at"] == expiry.astimezone(timezone.utc).isoformat()
    assert heartbeat <= datetime.fromisoformat(result["lease"]["observed_at"]) < expiry


@pytest.mark.parametrize("bead_id,actor", [
    ("global-legacy-a", "researcher-thread"),
    ("global-legacy-b", "legacy-actor-b"),
])
@pytest.mark.parametrize("fields_present", [False, True])
def test_only_exact_researcher_tuples_allow_explicit_leaseless_mode(h, guard, bead_id, actor, fields_present):
    from project_coordinator import apply_event
    options, record, _ = guard
    options["assignee"] = actor
    record.update(id=bead_id, assignee=actor)
    for field in ("heartbeat_at", "lease_expires_at"):
        if fields_present:
            record[field] = None
        else:
            record.pop(field)
    h.call("add", task={"id": "legacy", "worker": "worker-1", "linear_ref": "linear:test-1", "beads_ref": bead_id,
                        "prompt": "legacy fixture", "source_digest": "source-1", "scope_digest": "scope-1",
                        "dependencies": [], "writer_paths": []})
    policy = project_policy(h, legacy=[(bead_id, actor)])
    with pytest.raises(InvalidEvent, match="lacks heartbeat"):
        check_beads(h.db, event(h, task_id="legacy"), **options)
    response = apply_event(h.db, event(h, task_id="legacy"), guard_context=options, project_policy=policy)
    assert response["action"] == "PREPARED"
    assert response["beads_preflight"]["lease"]["mode"] == "LEGACY_LEASELESS"
    assert record.get("heartbeat_at") is None and record.get("lease_expires_at") is None


@pytest.mark.parametrize("bead_id,actor,changes", [
    ("global-legacy-a", "different-actor", {}),
    ("global-legacy-b", "researcher-thread", {}),
    ("global-project-7", "actor-unlisted", {}),
    ("global-legacy-a", "researcher-thread", {"heartbeat_at": "invalid"}),
    ("global-legacy-a", "researcher-thread",
     {"heartbeat_at": "2000-01-01T00:00:00Z", "lease_expires_at": "2000-01-01T01:00:00Z"}),
])
def test_legacy_never_downgrades_other_actors_or_bad_dates(h, guard, bead_id, actor, changes):
    options, record, _ = guard
    options["assignee"] = actor
    record.update({"id": bead_id, "assignee": actor, "heartbeat_at": None, "lease_expires_at": None, **changes})
    h.call("add", task={"id": "legacy", "worker": "worker-1", "linear_ref": "linear:test-1", "beads_ref": bead_id,
                        "prompt": "legacy fixture", "source_digest": "source-1", "scope_digest": "scope-1",
                        "dependencies": [], "writer_paths": []})
    policy = project_policy(h, legacy=[("global-legacy-a", "researcher-thread"),
                                       ("global-legacy-b", "legacy-actor-b")])
    with pytest.raises(InvalidEvent):
        check_beads(h.db, event(h, task_id="legacy"), project_policy=policy, **options)
    assert next(t for t in h.call("summary")["tasks"] if t["id"] == "legacy")["lease"] is False


@pytest.mark.parametrize("changes", [
    {"revision": "2"}, {"revision": None}, {"id": "other"}, {"status": "closed"},
    {"assignee": "new-owner"}, {"external_ref": "other-linear"},
    {"heartbeat_at": "2000-01-01T00:00:00Z"},
])
def test_native_transfer_or_revision_change_between_reads_never_checkpoints(h, guard, monkeypatch, changes):
    from project_coordinator import apply_event
    options, record, calls = guard
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        changed = {**record, **changes} if len(calls) == 2 else record
        return subprocess.CompletedProcess(argv, 0, json.dumps([changed]), "")
    monkeypatch.setattr("project_coordinator.beads.subprocess.run", run)
    checkpoint = h.call("summary")["checkpoint_id"]
    with pytest.raises(InvalidEvent):
        apply_event(h.db, event(h), guard_context=options)
    assert len(calls) == 2 and h.call("summary")["checkpoint_id"] == checkpoint


def test_public_api_cannot_bypass_guard_and_checks_under_existing_lock(h, guard):
    import fcntl
    from project_coordinator import apply_event
    options, _, calls = guard
    checkpoint = h.call("summary")["checkpoint_id"]
    with pytest.raises(InvalidEvent, match="requires explicit Beads guard context"):
        apply_event(h.db, event(h))
    assert not calls and h.call("summary")["checkpoint_id"] == checkpoint
    original_run = subprocess.run
    def run(argv, **kwargs):
        with open(str(h.db) + ".lock", "a+b") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original_run(argv, **kwargs)
    # The fixture's fake lookup remains the source; this wrapper checks serialization.
    from unittest.mock import patch
    with patch("project_coordinator.beads.subprocess.run", run):
        prepared = apply_event(h.db, event(h), guard_context=options)
    issue = event(h, "issue", event_id="issue", dispatch_id=prepared["dispatch_id"])
    checkpoint = h.call("summary")["checkpoint_id"]
    with pytest.raises(InvalidEvent, match="requires explicit Beads guard context"):
        apply_event(h.db, issue)
    assert h.call("summary")["checkpoint_id"] == checkpoint
    assert h.call("summary")["tasks"][0]["status"] == "PREPARED"


def test_old_issued_replay_reconcile_and_completed_reuse_need_no_guard(h, monkeypatch):
    from project_coordinator import apply_event
    h.add()
    prepared = h.prepare()
    issue = event(h, "issue", event_id="historical-issue", dispatch_id=prepared["dispatch_id"])
    assert "native_request" in apply_event(h.db, issue, guard_context=h.guard_context)
    def forbidden(*args, **kwargs):
        raise AssertionError("history must not require Beads or resend")
    monkeypatch.setattr("project_coordinator.beads.subprocess.run", forbidden)
    assert apply_event(h.db, issue)["action"] == "REPLAY"
    assert apply_event(h.db, event(h, event_id="old-prepare"))["action"] == "RECONCILE"
    assert apply_event(h.db, {**issue, "event_id": "old-issue"})["action"] == "RECONCILE"
    assert h.call("recover")["tasks"][0]["lease"] is True
    assert h.result(prepared["dispatch_id"])["action"] == "COMPLETE"
    assert apply_event(h.db, event(h, event_id="completed-prepare"))["action"] == "REUSE"
    assert "native_request" not in apply_event(h.db, {**issue, "event_id": "completed-issue"})


@pytest.mark.parametrize("op", ["prepare", "issue"])
def test_cli_omitted_guard_refuses_new_dispatch_without_checkpoint(h, tmp_path, op):
    h.add()
    body = event(h)
    if op == "issue":
        body = event(h, "issue", dispatch_id=h.prepare()["dispatch_id"])
    path = tmp_path / "event.json"
    path.write_text(json.dumps(body))
    checkpoint = h.call("summary")["checkpoint_id"]
    result = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), op,
                             "--input", str(path)], capture_output=True, text=True)
    assert result.returncode == 2 and result.stdout == ""
    assert "requires explicit Beads guard context" in result.stderr
    assert h.call("summary")["checkpoint_id"] == checkpoint


@pytest.mark.parametrize("op", ["prepare", "issue"])
@pytest.mark.parametrize("invalid_lease", ["expired", "missing"])
def test_cli_actual_fake_bd_refuses_invalid_lease_before_checkpoint(h, tmp_path, op, invalid_lease):
    h.add()
    body = event(h)
    if op == "issue":
        body = event(h, "issue", dispatch_id=h.prepare()["dispatch_id"])
    record_path = tmp_path / ".beads" / "record.json"
    record = json.loads(record_path.read_text())
    if invalid_lease == "expired":
        record["lease_expires_at"] = "2000-01-01T00:00:00Z"
    else:
        record.pop("heartbeat_at")
        record.pop("lease_expires_at")
    record_path.write_text(json.dumps(record))
    path = tmp_path / "event.json"
    path.write_text(json.dumps(body))
    checkpoint = h.call("summary")["checkpoint_id"]
    result = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), op,
                             "--input", str(path), "--beads-dir", h.guard_context["beads_dir"],
                             "--beads-assignee", h.guard_context["assignee"],
                             "--bd-executable", h.guard_context["executable"]], capture_output=True, text=True)
    assert result.returncode == 2 and result.stdout == "" and "Beads" in result.stderr
    assert h.call("summary")["checkpoint_id"] == checkpoint
    assert h.call("summary")["tasks"][0]["status"] == ("PREPARED" if op == "issue" else "READY")


@pytest.mark.parametrize("op", ["prepare", "issue"])
def test_api_pending_checkpoint_never_resumes_before_guard(h, op, monkeypatch):
    from project_coordinator import apply_event
    from test_control import pending
    h.add()
    body = event(h)
    if op == "issue":
        body = event(h, "issue", dispatch_id=h.prepare()["dispatch_id"])
    pending(h, {"op": "observe", "event_id": "pending", "worker": "worker-2", "state": "IDLE", "evidence": "fixture"})
    before = h.call("summary")
    assert before["pending_checkpoint"]
    def forbidden(*args, **kwargs):
        raise AssertionError("pending input must not reach Beads")
    monkeypatch.setattr("project_coordinator.beads.subprocess.run", forbidden)
    with pytest.raises(InvalidEvent, match="pending checkpoint"):
        apply_event(h.db, body, guard_context=h.guard_context)
    after = h.call("summary")
    assert after["pending_checkpoint"] and after["checkpoint_id"] == before["checkpoint_id"]
    h.call("recover")
    assert not h.call("summary")["pending_checkpoint"]


def test_cli_non_dispatch_guard_flags_are_rejected(h, tmp_path):
    path = tmp_path / "event.json"
    path.write_text(json.dumps(h.binding))
    checkpoint = h.call("summary")["checkpoint_id"]
    result = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), "summary",
                             "--input", str(path), "--beads-dir", h.guard_context["beads_dir"],
                             "--beads-assignee", h.guard_context["assignee"],
                             "--bd-executable", h.guard_context["executable"]], capture_output=True, text=True)
    assert result.returncode == 2 and "prepare and issue only" in result.stderr
    assert h.call("summary")["checkpoint_id"] == checkpoint


def test_wait_preview_cannot_become_unguarded_dispatch_during_graph_commit(h, monkeypatch):
    from project_coordinator import apply_event
    from project_coordinator import runtime
    h.add()
    original = runtime.readiness
    calls = []
    def changing_readiness(model, task):
        calls.append(task["id"])
        if len(calls) == 1:
            return ["synthetic-readiness-race"]
        return original(model, task)
    monkeypatch.setattr(runtime, "readiness", changing_readiness)
    checkpoint = h.call("summary")["checkpoint_id"]
    calls.clear()
    with pytest.raises(InvalidEvent, match="requires explicit Beads guard context"):
        apply_event(h.db, event(h))
    assert len(calls) >= 2
    task = h.call("summary")["tasks"][0]
    assert task["lease"] is False and "dispatch_id" not in task
    assert h.call("summary")["checkpoint_id"] == checkpoint
    calls.clear()
    result = apply_event(h.db, event(h), guard_context=h.guard_context)
    assert result["action"] == "PREPARED" and result["beads_preflight"]["action"] == "VERIFIED"


def test_native_heartbeat_age_has_no_arbitrary_threshold(h, guard):
    options, record, _ = guard
    record["heartbeat_at"] = "2000-01-01T00:00:00Z"
    assert check_beads(h.db, event(h), **options)["lease"]["mode"] == "LEASED"


@pytest.mark.parametrize("context", [{}, {"beads_dir": "/tmp"},
    {"beads_dir": "/tmp", "assignee": "A", "executable": "/tmp/bd", "heartbeat_at": "caller-assertion"}])
def test_api_incomplete_or_asserted_guard_context_is_rejected_without_checkpoint(h, context):
    from project_coordinator import apply_event
    h.add()
    checkpoint = h.call("summary")["checkpoint_id"]
    with pytest.raises(InvalidEvent, match="requires exactly"):
        apply_event(h.db, event(h), guard_context=context)
    assert h.call("summary")["checkpoint_id"] == checkpoint


def test_prepared_claim_rejects_new_actor_context_and_allows_same_actor_renewal(h):
    from project_coordinator import apply_event
    from test_control import model
    h.add()
    prepare = event(h, event_id="pinned-prepare")
    prepared = apply_event(h.db, prepare, guard_context=h.guard_context)
    expected = {"beads_id": "bead-1", "assignee": "codex:root:A", "linear_ref": "linear:test-1"}
    before = copy.deepcopy(model(h))
    assert before["tasks"]["A"]["beads_claim"] == expected
    assert before["tasks"]["A"]["lease"] is True
    checkpoint = h.call("summary")["checkpoint_id"]
    record_path = h.root / ".beads" / "record.json"
    record = json.loads(record_path.read_text())
    record.update(assignee="codex:root:B", revision="2")
    record_path.write_text(json.dumps(record))
    issue = event(h, "issue", event_id="pinned-issue", dispatch_id=prepared["dispatch_id"])
    with pytest.raises(InvalidEvent, match="prepared Beads claim missing or changed"):
        apply_event(h.db, issue, guard_context={**h.guard_context, "assignee": "codex:root:B"})
    assert model(h) == before
    assert h.call("summary")["checkpoint_id"] == checkpoint
    assert apply_event(h.db, prepare)["action"] == "REPLAY"
    assert model(h)["tasks"]["A"]["beads_claim"] == expected
    assert apply_event(h.db, {**prepare, "event_id": "B-cannot-reprepare"},
                       guard_context={**h.guard_context, "assignee": "codex:root:B"})["action"] == "RECONCILE"
    assert model(h)["tasks"]["A"] == before["tasks"]["A"]
    now = datetime.now(timezone.utc)
    record.update(assignee="codex:root:A", revision="3", heartbeat_at=(now - timedelta(seconds=1)).isoformat(),
                  lease_expires_at=(now + timedelta(hours=2)).isoformat())
    record_path.write_text(json.dumps(record))
    issued = apply_event(h.db, issue, guard_context=h.guard_context)
    assert issued["action"] == "ISSUING" and "native_request" in issued
    assert model(h)["tasks"]["A"]["beads_claim"] == expected
    assert model(h)["events"][prepare["event_id"]] == before["events"][prepare["event_id"]]


def test_wait_observation_cannot_authorize_its_actor_to_issue_another_actors_reservation(h):
    from project_coordinator import apply_event
    from test_control import model
    h.add()
    h.call("observe", worker="worker-1", state="BUSY", evidence="fixture busy")
    record_path = h.root / ".beads" / "record.json"
    record = json.loads(record_path.read_text())
    record.update(assignee="codex:root:B", revision="2")
    record_path.write_text(json.dumps(record))
    waiting = apply_event(h.db, event(h, event_id="waiting-B"),
                          guard_context={**h.guard_context, "assignee": "codex:root:B"})
    assert waiting["action"] == "WAIT"
    task = model(h)["tasks"]["A"]
    assert not task["lease"] and "dispatch_id" not in task
    record.update(assignee="codex:root:A", revision="3")
    record_path.write_text(json.dumps(record))
    h.call("observe", worker="worker-1", state="IDLE", evidence="fixture idle")
    prepared = apply_event(h.db, event(h, event_id="reserved-A"), guard_context=h.guard_context)
    assert model(h)["tasks"]["A"]["beads_claim"]["assignee"] == "codex:root:A"
    before = copy.deepcopy(model(h))
    checkpoint = h.call("summary")["checkpoint_id"]
    record.update(assignee="codex:root:B", revision="4")
    record_path.write_text(json.dumps(record))
    with pytest.raises(InvalidEvent, match="prepared Beads claim missing or changed"):
        apply_event(h.db, event(h, "issue", event_id="B-cannot-issue", dispatch_id=prepared["dispatch_id"]),
                    guard_context={**h.guard_context, "assignee": "codex:root:B"})
    assert model(h) == before and h.call("summary")["checkpoint_id"] == checkpoint


def test_old_prepared_without_claim_proof_holds_lease_and_fails_closed(h):
    import sqlite3
    from langgraph.checkpoint.sqlite import SqliteSaver
    from project_coordinator import apply_event
    from project_coordinator.runtime import build_graph
    from test_control import model
    h.add()
    prepared = h.prepare()
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {"configurable": {"thread_id": "project-coordinator-v1"}}
        legacy = copy.deepcopy(graph.get_state(config).values["model"])
        legacy["tasks"]["A"].pop("beads_claim")
        graph.update_state(config, {"model": legacy}, as_node="readiness")
    before = copy.deepcopy(model(h))
    checkpoint = h.call("summary")["checkpoint_id"]
    with pytest.raises(InvalidEvent, match="explicit Root reconciliation required"):
        apply_event(h.db, event(h, "issue", dispatch_id=prepared["dispatch_id"]), guard_context=h.guard_context)
    assert model(h) == before and h.call("summary")["checkpoint_id"] == checkpoint
    assert before["tasks"]["A"]["lease"] is True and before["tasks"]["A"]["status"] == "PREPARED"
    assert h.prepare()["action"] == "RECONCILE"
    h.call("recover")
    task = model(h)["tasks"]["A"]
    assert "beads_claim" not in task and task["lease"] is True and task["status"] == "PREPARED"


def test_prepare_input_checkpoint_crash_retains_claim_through_recovery(h, monkeypatch):
    from project_coordinator import apply_event, runtime
    from test_control import model
    h.add()
    original_build = runtime.build_graph
    def crashing_build(saver):
        graph = original_build(saver)
        original_invoke = graph.invoke
        def crash_after_input(inputs, config, **kwargs):
            if isinstance(inputs, dict) and inputs.get("event", {}).get("op") == "prepare":
                stream = graph.stream(inputs, config, stream_mode="checkpoints", durability="sync")
                next(stream)
                stream.close()
                raise RuntimeError("synthetic death after prepare input checkpoint")
            return original_invoke(inputs, config, **kwargs)
        monkeypatch.setattr(graph, "invoke", crash_after_input)
        return graph
    with monkeypatch.context() as patch:
        patch.setattr(runtime, "build_graph", crashing_build)
        with pytest.raises(RuntimeError, match="synthetic death"):
            apply_event(h.db, event(h), guard_context=h.guard_context)
    assert h.call("summary")["pending_checkpoint"]
    claim = {"beads_id": "bead-1", "assignee": "codex:root:A", "linear_ref": "linear:test-1"}
    with runtime.sqlite3.connect(h.db, check_same_thread=False) as conn:
        saved = runtime.SqliteSaver(conn).get_tuple({"configurable": {"thread_id": "project-coordinator-v1"}})
        staged = saved.checkpoint["channel_values"]["__start__"]
        assert staged["model"]["tasks"]["A"]["beads_claim"] == claim
        assert staged["event"] == event(h)
    recovered = h.call("recover")
    assert "native_request" not in recovered
    task = model(h)["tasks"]["A"]
    assert task["status"] == "PREPARED" and task["lease"] and task["beads_claim"] == claim
    record_path = h.root / ".beads" / "record.json"
    record = json.loads(record_path.read_text())
    record.update(assignee="codex:root:B", revision="2")
    record_path.write_text(json.dumps(record))
    before = copy.deepcopy(model(h))
    with pytest.raises(InvalidEvent, match="prepared Beads claim missing or changed"):
        apply_event(h.db, event(h, "issue", event_id="recovered-B-issue", dispatch_id=task["dispatch_id"]),
                    guard_context={**h.guard_context, "assignee": "codex:root:B"})
    assert model(h) == before



def test_registered_researcher_still_requires_fresh_guard_and_prepared_actor_pin(h, guard):
    from project_coordinator import apply_event
    from test_control import REGISTERED_THREAD, model, register
    options, record, calls = guard
    manual_before = copy.deepcopy(record)
    register(h)
    assert not calls and record == manual_before
    h.add("researcher-task", "researcher")
    h.call("observe", worker="researcher", state="IDLE", evidence="fresh registered worker observation")
    prepare = event(h, task_id="researcher-task", event_id="registered-prepare")
    checkpoint = h.call("summary")["checkpoint_id"]
    with pytest.raises(InvalidEvent, match="requires explicit Beads guard context"):
        apply_event(h.db, prepare)
    assert h.call("summary")["checkpoint_id"] == checkpoint and not calls
    expiry = record["lease_expires_at"]
    record["lease_expires_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(InvalidEvent, match="expired"):
        apply_event(h.db, prepare, guard_context=options)
    assert h.call("summary")["checkpoint_id"] == checkpoint
    record["lease_expires_at"] = expiry
    prepared = apply_event(h.db, prepare, guard_context=options)
    assert prepared["action"] == "PREPARED"
    stored = model(h)
    assert stored["tasks"]["researcher-task"]["beads_claim"]["assignee"] == options["assignee"]
    issue = event(h, "issue", task_id="researcher-task", event_id="registered-issue", dispatch_id=prepared["dispatch_id"])
    checkpoint = h.call("summary")["checkpoint_id"]
    with pytest.raises(InvalidEvent, match="requires explicit Beads guard context"):
        apply_event(h.db, issue)
    record.update(assignee="new-actor", revision="2")
    with pytest.raises(InvalidEvent, match="prepared Beads claim missing or changed"):
        apply_event(h.db, issue, guard_context={**options, "assignee": "new-actor"})
    assert h.call("summary")["checkpoint_id"] == checkpoint and model(h) == stored
    record.update(assignee=options["assignee"], revision="3")
    response = apply_event(h.db, issue, guard_context=options)
    assert response["native_request"]["arguments"]["threadId"] == REGISTERED_THREAD
    assert response["beads_preflight"]["lease"]["mode"] == "LEASED"
