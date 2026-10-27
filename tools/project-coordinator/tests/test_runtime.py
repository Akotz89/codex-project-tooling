from datetime import datetime, timedelta, timezone
import json
import multiprocessing
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from project_coordinator.runtime import InvalidEvent, apply_event, build_graph, digest


# Sanitized actual MCP response, source SHA-256 545b9b1f1e14bdbffd2cbeeb2bb63906248573da7c905e6d4ae4f1d7184d7aff.
ACTUAL_NATIVE_MCP_TEXT = r'''{"timedOut":false,"wake":{"reason":"turnCompleted","turnId":"01a17c95-144d-7f42-aacd-3abff2f7e42e","threadId":"01a17c95-1205-74d2-be32-7072d35e10d7","hostId":"local"},"polls":[{"schemaVersion":1,"cursor":"4d063448-70c1-4546-b542-9f7dc4841b97:1","revision":1,"changed":true,"thread":{"id":"01a17c95-1205-74d2-be32-7072d35e10d7","hostId":"local","status":{"type":"idle"}},"latestTurn":{"id":"01a17c95-144d-7f42-aacd-3abff2f7e42e","status":"completed","error":null,"startedAt":1793091507,"completedAt":1793091738,"durationMs":231591},"latestAssistantMessageId":"msg_0b36d520b8303534016ae06899bf6c8197b53299cefae2a9a3","latestAssistantMessage":{"id":"msg_0b36d520b8303534016ae06899bf6c8197b53299cefae2a9a3","turnId":"01a17c95-144d-7f42-aacd-3abff2f7e42e","phase":"final_answer","text":"Published the 13 frozen documents to codex/cpi388-level-1-19-benchmark-20261027. Commit 3b6375b3278a774c65780cfba071a14b9cd45695 has the expected parent b4a4dbc076ddaf6214c789f2f2b2d0a5e496dc88. The remote readback matched all 13 paths, sizes, and SHA-256 hashes; the isolated checkout is clean. All 26 links to package companions resolve. Twelve frozen references to paths outside the package are absent from the canonical root. None targets a listed companion; I left the document bytes unchanged. The staged diff also retained existing trailing-whitespace warnings. Requested settings were Luna/high; the host reported Luna as the active model, while observed reasoning effort was unavailable. Skipped PR creation and runtime/gameplay checks. Risk: those 12 out-of-package references remain unresolved."},"latestToolMarkerId":"exec-3b2772db-8c08-42cd-b5d3-7dd60a0e26c8","latestToolMarker":{"id":"exec-3b2772db-8c08-42cd-b5d3-7dd60a0e26c8","turnId":"01a17c95-144d-7f42-aacd-3abff2f7e42e","type":"commandExecution","name":"commandExecution","status":"completed"}}]}'''


class Harness:
    def __init__(self, root, *, project_id="P-test", workers=None):
        self.root = root
        self.db = root / "state.sqlite"
        self.binding = {"project_id": project_id, "root": str(root)}
        self.workers = workers or {f"worker-{i}": f"thread{i}" for i in range(1, 5)}
        directory = root / ".beads"
        directory.mkdir()
        now = datetime.now(timezone.utc)
        (directory / "record.json").write_text(json.dumps({
            "id": "bead-1", "external_ref": "linear:test-1", "status": "in_progress", "assignee": "codex:root:A",
            "heartbeat_at": (now - timedelta(seconds=1)).isoformat(),
            "lease_expires_at": (now + timedelta(hours=1)).isoformat(), "revision": "1"}))
        binary = root / "fake-bd"
        binary.write_text("#!/usr/bin/env python3\nimport os,pathlib\n"
                          "print('[' + (pathlib.Path(os.environ['BEADS_DIR'])/'record.json').read_text() + ']')\n")
        binary.chmod(0o700)
        self.guard_context = {"beads_dir": str(directory), "assignee": "codex:root:A", "executable": str(binary)}
        self.call("init", project={**self.binding, "linear_ref": "linear:test-1", "beads_ref": "global:bead-1",
                                  "coordinator_thread_id": "root", "workers": self.workers})
        for role in self.workers:
            self.call("observe", worker=role, state="IDLE", evidence="actual observation fixture")

    def call(self, op, **fields):
        return apply_event(self.db, {**self.binding, "op": op, "event_id": str(uuid.uuid4()), **fields},
                           guard_context=self.guard_context if op in {"prepare", "issue"} else None)

    def add(self, name="A", worker="worker-1", paths=None, dependencies=None):
        return self.call("add", task={"id": name, "worker": worker, "linear_ref": "linear:test-1", "beads_ref": "global:bead-1",
                                     "prompt": "fixture", "source_digest": "source-1", "scope_digest": "scope-1",
                                     "writer_paths": paths or [], "dependencies": dependencies or []})

    def prepare(self, name="A", **fields):
        return self.call("prepare", task_id=name, source_digest="source-1", scope_digest="scope-1", **fields)

    def issue(self, name="A"):
        prepared = self.prepare(name)
        dispatch = prepared["dispatch_id"]
        response = self.call("issue", task_id=name, dispatch_id=dispatch)
        assert response["native_request"]["arguments"]["threadId"] == self.task(name)["dispatch_thread_id"]
        return dispatch

    def task(self, name="A"):
        return next(t for t in self.call("status")["tasks"] if t["id"] == name)

    def file(self, name, content):
        path = self.root / name
        path.write_text(json.dumps(content))
        return {"path": name, "sha256": digest(path), "size_bytes": path.stat().st_size}

    def result(self, dispatch, name="A", **changes):
        task = self.task(name)
        thread_id = task.get("dispatch_thread_id") or self.workers[task["worker"]]
        artifact = self.file(f"{name}-output.json", {"actual": "output"})
        native = self.file(f"{name}-native.json", {"threadId": thread_id,
                                                    "dispatch_id": dispatch, "terminal": "fixture final"})
        result = {"thread_id": thread_id, "dispatch_id": dispatch,
                  "source_digest": "source-1", "scope_digest": "scope-1", "terminal": True, "quiescent": True,
                  "child_exit_code": 0, "acceptance": True, "artifacts": [artifact], "native_evidence": native, **changes}
        receipt = self.file(f"{name}-result.json", result)
        return self.call("result", task_id=name, dispatch_id=dispatch, receipt=receipt)

    def native_result(self, dispatch, name="A", *, raw=None, include_outcome=True, **changes):
        task = self.task(name)
        raw = raw or {"content": [{"type": "text", "text": ACTUAL_NATIVE_MCP_TEXT}], "isError": False}
        try:
            payload = json.loads(raw["content"][0]["text"])
        except (KeyError, IndexError, TypeError, ValueError):
            payload = json.loads(ACTUAL_NATIVE_MCP_TEXT)
        if not isinstance(payload, dict):
            payload = json.loads(ACTUAL_NATIVE_MCP_TEXT)
        wake = payload["wake"]
        turn = payload["polls"][0]["latestTurn"]
        child_thread_id = wake["threadId"]
        turn_id = wake["turnId"]
        artifact = self.file(f"{name}-native-output.json", {"actual": "native output"})
        native = self.file(f"{name}-native-result.json", raw)
        result = {"thread_id": task["dispatch_thread_id"], "dispatch_id": dispatch,
                  "source_digest": "source-1", "scope_digest": "scope-1", "artifacts": [artifact],
                  "native_evidence": native,
                  "caller_association": {"source": "graph_dispatch_and_native_launch",
                      "caller_thread_id": "root", "task_id": name, "dispatch_id": dispatch,
                      "child_thread_id": child_thread_id, "turn_id": turn_id},
                  "caller_acceptance": {"caller_thread_id": "root", "accepted": True}}
        if include_outcome:
            result["native_outcome"] = {"kind": "collaboration_turn", "child_thread_id": child_thread_id,
                "turn_id": turn_id, "status": turn["status"], "error": turn.get("error")}
        result.update(changes)
        receipt = self.file(f"{name}-native-result-receipt.json", result)
        return self.call("result", task_id=name, dispatch_id=dispatch, receipt=receipt)


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def project_policy(h, *, registrations=(), legacy=()):
    return {"schema_version": 1, "project_id": h.binding["project_id"], "root": str(h.root),
            "worker_registrations": [{"worker": role, "thread_id": thread} for role, thread in registrations],
            "legacy_leaseless_beads": [{"beads_id": bead_id, "assignee": actor}
                                       for bead_id, actor in legacy]}


def test_second_project_uses_its_non_wow_role_map_and_dynamic_count(tmp_path):
    workers = {"implementer": "build-thread", "review_qa": "review-thread", "release-manager": "release-thread"}
    harness = Harness(tmp_path, project_id="P-second", workers=workers)
    for role in workers:
        harness.add(role, role)
        dispatch = harness.issue(role)
        assert harness.result(dispatch, role)["action"] == "COMPLETE"
    summary = harness.call("summary")
    assert summary["project"]["workers"] == workers
    assert set(summary["workers"]) == set(workers)
    assert {task["worker"] for task in summary["tasks"]} == set(workers)
    assert summary["bounds"]["worker_role_count"] == 3
    assert summary["bounds"]["omitted_worker_roles"] == 0


def test_registration_policy_from_another_project_is_rejected(tmp_path):
    from project_coordinator.runtime import InvalidEvent
    first_root = tmp_path / "first"
    first_root.mkdir()
    first = Harness(first_root, project_id="P-first", workers={"builder": "builder-thread"})
    second_root = tmp_path / "second"
    second_root.mkdir()
    second = Harness(second_root, project_id="P-second", workers={"reviewer": "review-thread"})
    policy = project_policy(first, registrations=[("new_role", "new-thread")])
    event = {**second.binding, "op": "register", "event_id": "cross-project-register", "worker": "new_role",
             "new_thread_id": "new-thread", "expected_workers": second.workers, "evidence": "fixture"}
    before = second.db.read_bytes()
    with pytest.raises(InvalidEvent, match="project policy identity mismatch"):
        apply_event(second.db, event, project_policy=policy)
    assert second.db.read_bytes() == before


def test_project_policy_symlink_loop_resolve_is_controlled(monkeypatch, tmp_path):
    from project_coordinator.runtime import validate_project_policy

    harness = Harness(tmp_path)
    policy = project_policy(harness)

    def symlink_loop(self, *, strict=False):
        raise RuntimeError("Symlink loop from platform path resolver")

    monkeypatch.setattr(Path, "resolve", symlink_loop)
    with pytest.raises(InvalidEvent) as exc:
        validate_project_policy(policy, harness.binding)
    assert str(exc.value) == "project policy root is unavailable"


def test_dependency_join(h):
    h.add()
    h.add("B", "worker-2")
    h.add("C", "worker-3", dependencies=["A", "B"])
    assert "dependency:A" in str(h.prepare("C"))
    h.result(h.issue())
    assert "dependency:B" in str(h.prepare("C"))
    h.result(h.issue("B"), "B")
    assert h.prepare("C")["action"] == "PREPARED"
    assert h.prepare("C")["action"] == "RECONCILE"


def test_failed_join_branch(h):
    h.add()
    h.add("C", "worker-2", dependencies=["A", "missing"])
    h.result(h.issue(), child_exit_code=1)
    response = h.prepare("C")
    assert "dependency:A:FAILED" in response["reasons"]
    assert "dependency:missing:MISSING" in response["reasons"]


@pytest.mark.parametrize("changes,state,lease", [
    ({"child_exit_code": 1, "wrapper_exit_code": 0, "quiescent": False}, "FAILED", True),
    ({"terminal": False, "child_exit_code": None, "wrapper_exit_code": 0}, "UNKNOWN", True),
    ({"acceptance": False, "wrapper_exit_code": 0}, "FAILED", False),
    ({"quiescent": False}, "FAILED", True),
    ({"child_exit_code": False}, "FAILED", False),
    ({"artifacts": []}, "FAILED", False),
])
def test_outcomes(h, changes, state, lease):
    h.add()
    result = h.result(h.issue(), **changes)
    assert result["action"] == state
    assert result["lease"] is lease


def native_payload(**changes):
    payload = json.loads(ACTUAL_NATIVE_MCP_TEXT)
    for key, value in changes.items():
        if key == "thread_status":
            payload["polls"][0]["thread"]["status"]["type"] = value
        elif key == "turn_status":
            payload["polls"][0]["latestTurn"]["status"] = value
        elif key == "turn_error":
            payload["polls"][0]["latestTurn"]["error"] = value
        elif key == "wake_thread_id":
            payload["wake"]["threadId"] = value
        elif key == "poll_turn_id":
            payload["polls"][0]["latestTurn"]["id"] = value
        elif key == "without_error":
            payload["polls"][0]["latestTurn"].pop("error", None)
        else:
            payload[key] = value
    return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload)}]}


def ambiguous_native_payload():
    payload = json.loads(ACTUAL_NATIVE_MCP_TEXT)
    payload["polls"].append(payload["polls"][0].copy())
    return {"isError": False, "content": [{"type": "text", "text": json.dumps(payload)}]}


def test_actual_nested_native_mcp_completion_is_caller_bound_and_reusable(h):
    h.add()
    dispatch = h.issue()
    raw = {"isError": False, "content": [{"type": "text", "text": ACTUAL_NATIVE_MCP_TEXT}]}
    payload = json.loads(ACTUAL_NATIVE_MCP_TEXT)
    assert "caller_thread_id" not in payload and "task_id" not in payload and "dispatch_id" not in payload
    assert "exit_code" not in ACTUAL_NATIVE_MCP_TEXT and "child_exit_code" not in ACTUAL_NATIVE_MCP_TEXT
    assert h.native_result(dispatch, raw=raw)["action"] == "COMPLETE"
    assert h.prepare()["action"] == "REUSE"


@pytest.mark.parametrize("field,value", [
    ("child_thread_id", "tampered-child"),
    ("turn_id", "tampered-turn"),
    ("status", "failed"),
    ("error", "tampered-error"),
])
def test_native_typed_outcome_must_match_unchanged_actual_mcp_evidence(h, field, value):
    h.add()
    dispatch = h.issue()
    raw = {"isError": False, "content": [{"type": "text", "text": ACTUAL_NATIVE_MCP_TEXT}]}
    payload = json.loads(ACTUAL_NATIVE_MCP_TEXT)
    wake = payload["wake"]
    turn = payload["polls"][0]["latestTurn"]
    outcome = {"kind": "collaboration_turn", "child_thread_id": wake["threadId"],
               "turn_id": wake["turnId"], "status": turn["status"], "error": turn["error"]}
    outcome[field] = value
    association = {"source": "graph_dispatch_and_native_launch", "caller_thread_id": "root",
                   "task_id": "A", "dispatch_id": dispatch,
                   "child_thread_id": outcome["child_thread_id"], "turn_id": outcome["turn_id"]}

    with pytest.raises(InvalidEvent):
        h.native_result(dispatch, raw=raw, native_outcome=outcome, caller_association=association)

    assert raw["content"][0]["text"] == ACTUAL_NATIVE_MCP_TEXT
    assert h.task()["status"] != "COMPLETE" and h.task()["lease"] is True


@pytest.mark.parametrize("raw", [
    {"isError": False, "content": [{"type": "text", "text": "{"}]},
    {"isError": True, "content": [{"type": "text", "text": ACTUAL_NATIVE_MCP_TEXT}]},
    {"isError": False, "content": [{"type": "text", "text": "null"}]},
    native_payload(timedOut=True),
    native_payload(turn_status="in_progress"),
    native_payload(without_error=True),
    native_payload(wake_thread_id="different-child"),
    native_payload(poll_turn_id="different-turn"),
    ambiguous_native_payload(),
    {"isError": False, "content": [{"type": "text", "text": ACTUAL_NATIVE_MCP_TEXT},
                                     {"type": "text", "text": ACTUAL_NATIVE_MCP_TEXT}]},
])
def test_native_malformed_absent_error_unknown_or_mismatched_raw_never_completes(h, raw):
    h.add()
    dispatch = h.issue()
    with pytest.raises(InvalidEvent):
        h.native_result(dispatch, raw=raw)
    assert h.task()["status"] in {"ISSUING", "UNKNOWN"} and h.task()["lease"] is True


def test_native_turn_error_fails_but_releases_only_when_quiescent(h):
    h.add()
    result = h.native_result(h.issue(), raw=native_payload(turn_error="worker failed"))
    assert result["action"] == "FAILED" and result["lease"] is False


def test_native_absent_outcome_never_completes(h):
    h.add()
    with pytest.raises(InvalidEvent):
        h.native_result(h.issue(), include_outcome=False)
    assert h.task()["status"] == "UNKNOWN" and h.task()["lease"] is True


def test_native_nonquiescent_turn_retains_lease(h):
    h.add()
    result = h.native_result(h.issue(), raw=native_payload(thread_status="running"))
    assert result["action"] == "FAILED" and result["lease"] is True


@pytest.mark.parametrize("changes", [
    {"caller_association": {"source": "graph_dispatch_and_native_launch", "caller_thread_id": "wrong",
        "task_id": "A", "dispatch_id": "dispatch", "child_thread_id": "child", "turn_id": "turn"}},
    {"caller_association": {"source": "graph_dispatch_and_native_launch", "caller_thread_id": "root",
        "task_id": "wrong-task", "dispatch_id": "dispatch", "child_thread_id": "child", "turn_id": "turn"}},
    {"caller_association": {"source": "graph_dispatch_and_native_launch", "caller_thread_id": "root",
        "task_id": "A", "dispatch_id": "wrong-dispatch", "child_thread_id": "child", "turn_id": "turn"}},
    {"caller_association": {"source": "graph_dispatch_and_native_launch", "caller_thread_id": "root",
        "task_id": "A", "dispatch_id": "dispatch", "child_thread_id": "wrong-child", "turn_id": "turn"}},
    {"caller_association": {"source": "graph_dispatch_and_native_launch", "caller_thread_id": "root",
        "task_id": "A", "dispatch_id": "dispatch", "child_thread_id": "child", "turn_id": "wrong-turn"}},
    {"child_exit_code": 0},
])
def test_native_wrong_caller_task_dispatch_child_or_turn_association_is_rejected(h, changes):
    h.add()
    dispatch = h.issue()
    with pytest.raises(InvalidEvent):
        h.native_result(dispatch, **changes)
    assert h.task()["status"] in {"ISSUING", "UNKNOWN"} and h.task()["lease"] is True


@pytest.mark.parametrize("acceptance", [None, {"caller_thread_id": "root", "accepted": False},
                                         {"caller_thread_id": "other", "accepted": True}])
def test_native_missing_false_or_wrong_caller_acceptance_never_completes(h, acceptance):
    h.add()
    result = h.native_result(h.issue(), caller_acceptance=acceptance)
    assert result["action"] == "FAILED" and result["lease"] is False


@pytest.mark.parametrize("changes", [
    {"artifacts": []},
    {"artifacts": [{"path": "missing", "sha256": "0" * 64, "size_bytes": 1}]},
    {"artifacts": [{"path": "A-native-output.json", "sha256": "0" * 64, "size_bytes": 18}]},
    {"native_evidence": {"path": "missing", "sha256": "0" * 64}},
])
def test_native_missing_or_drifted_evidence_and_artifacts_never_complete(h, changes):
    h.add()
    dispatch = h.issue()
    if changes.get("native_evidence", {}).get("path") == "missing":
        with pytest.raises(FileNotFoundError):
            h.native_result(dispatch, **changes)
    else:
        result = h.native_result(dispatch, **changes)
        assert result["action"] == "FAILED"


def test_native_wrong_artifact_size_never_completes(h):
    h.add()
    dispatch = h.issue()
    artifact = h.file("A-native-output.json", {"actual": "native output"})
    artifact["size_bytes"] += 1
    result = h.native_result(dispatch, artifacts=[artifact])
    assert result["action"] == "FAILED"


def test_native_raw_evidence_hash_drift_never_completes(h):
    h.add()
    evidence = h.file("A-native-result.json", {"fixture": "drift"})
    evidence["sha256"] = "0" * 64
    with pytest.raises(InvalidEvent, match="native evidence drift"):
        h.native_result(h.issue(), native_evidence=evidence)


def test_completed_reuse_and_changed_identity(h):
    h.add()
    dispatch = h.issue()
    assert h.result(dispatch)["action"] == "COMPLETE"
    assert h.prepare()["action"] == "REUSE"
    with pytest.raises(InvalidEvent, match="identity mismatch"):
        h.call("prepare", task_id="A", source_digest="source-2", scope_digest="scope-1")
    with pytest.raises(InvalidEvent, match="task already exists"):
        h.add()
    (h.root / "A-output.json").write_text("mutated")
    with pytest.raises(InvalidEvent, match="artifact/receipt drift"):
        h.prepare()


def test_mutation_refuses_completion(h):
    h.add()
    dispatch = h.issue()
    assert h.result(dispatch, artifacts=[{"path": "missing", "sha256": "0" * 64, "size_bytes": 1}])["action"] == "FAILED"


def test_wrong_size_refuses_completion(h):
    h.add()
    dispatch = h.issue()
    artifact = h.file("out", "fixture")
    artifact["size_bytes"] += 1
    assert h.result(dispatch, artifacts=[artifact])["action"] == "FAILED"


def test_ambiguous_recovery_ack_and_no_reissue(h):
    h.add(paths=["owned"])
    dispatch = h.issue()
    assert h.call("recover")["tasks"][0]["status"] == "UNKNOWN"
    assert "native_request" not in h.call("issue", task_id="A", dispatch_id=dispatch)
    assert h.task()["lease"]
    ack = h.file("ack.json", {"content": [{"type": "text", "text": '{"threadId":"thread1"}'}]})
    assert h.call("ack", task_id="A", dispatch_id=dispatch, receipt=ack)["action"] == "OK"
    assert h.task()["status"] == "ACKNOWLEDGED"
    assert h.result(dispatch)["action"] == "COMPLETE"


def test_parent_alias_overlap_and_unknown_lease(h):
    (h.root / "owned").mkdir()
    (h.root / "alias").symlink_to(h.root / "owned", target_is_directory=True)
    h.add(paths=["owned"])
    h.issue()
    h.add("B", "worker-2", paths=["alias/child"])
    assert "writer-lease:A" in str(h.prepare("B"))
    assert h.task()["status"] == "UNKNOWN"
    assert "writer-lease:A" in str(h.prepare("B"))


def test_busy_worker_and_same_worker_lease(h):
    h.add()
    h.call("observe", worker="worker-1", state="BUSY", evidence="observed running")
    assert h.prepare()["action"] == "WAIT"
    h.call("observe", worker="worker-1", state="IDLE", evidence="observed idle")
    h.prepare()
    h.add("B")
    assert "worker-lease:A" in str(h.prepare("B"))


def concurrent_prepare(db, binding, name, queue, guard_context):
    queue.put(apply_event(db, {**binding, "op": "prepare", "event_id": f"prepare-{name}", "task_id": name,
                             "source_digest": "source-1", "scope_digest": "scope-1"}, guard_context=guard_context)["action"])


def test_two_controllers_one_claim(h):
    h.add(paths=["owned"])
    h.add("B", "worker-2", paths=["owned"])
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    children = [ctx.Process(target=concurrent_prepare, args=(h.db, h.binding, name, queue, h.guard_context)) for name in ("A", "B")]
    for child in children:
        child.start()
    for child in children:
        child.join(20)
        assert child.exitcode == 0
    assert sorted(queue.get(timeout=2) for _ in children) == ["PREPARED", "WAIT"]


def test_idempotent_and_conflicting_replay(h):
    event = {**h.binding, "op": "observe", "event_id": "same", "worker": "worker-1", "state": "IDLE", "evidence": "fixture"}
    apply_event(h.db, event)
    assert apply_event(h.db, event)["action"] == "REPLAY"
    with pytest.raises(InvalidEvent, match="conflicting"):
        apply_event(h.db, {**event, "state": "BUSY"})
    assert h.call("status")["workers"]["worker-1"]["state"] == "IDLE"
    h.add()
    dispatch = h.prepare()["dispatch_id"]
    issue = {**h.binding, "op": "issue", "event_id": "issue-once", "task_id": "A", "dispatch_id": dispatch}
    assert "native_request" in apply_event(h.db, issue, guard_context=h.guard_context)
    assert "native_request" not in apply_event(h.db, issue)


def test_wrong_project_root_target_and_dispatch(h):
    with pytest.raises(InvalidEvent, match="project_id"):
        h.call("status", project_id="different")
    with pytest.raises(InvalidEvent, match="root"):
        h.call("status", root=str(h.root.parent))
    h.add()
    dispatch = h.issue()
    ack = h.file("ack.json", {"threadId": "thread2"})
    with pytest.raises(InvalidEvent, match="target"):
        h.call("ack", task_id="A", dispatch_id=dispatch, receipt=ack)
    with pytest.raises(InvalidEvent, match="dispatch"):
        h.call("issue", task_id="A", dispatch_id="other")
    assert h.result(dispatch)["action"] == "COMPLETE"


def test_checkpoint_pending_resume_suppresses_issue(h):
    h.add()
    dispatch = h.prepare()["dispatch_id"]
    # Simulate death after checkpointed issue input, before transition completed.
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {"configurable": {"thread_id": "project-coordinator-v1"}}
        event = {**h.binding, "op": "issue", "event_id": "crashed-issue", "task_id": "A", "dispatch_id": dispatch}
        stream = graph.stream({"event": event}, config, stream_mode="checkpoints", durability="sync")
        next(stream)
        stream.close()
    recovered = h.call("recover")
    assert recovered["tasks"][0]["status"] == "UNKNOWN"
    assert recovered["tasks"][0]["lease"]
    assert "native_request" not in h.call("issue", task_id="A", dispatch_id=dispatch)


def test_receipt_drift_and_inconclusive_absence(h):
    h.add()
    dispatch = h.issue()
    with pytest.raises(InvalidEvent, match="quiescence"):
        h.call("reconcile", task_id="A", dispatch_id=dispatch, evidence="bounded page found no match")
    assert h.task()["lease"]
    receipt = h.file("ack.json", {"threadId": "thread1"})
    (h.root / "ack.json").write_text("{}")
    with pytest.raises(InvalidEvent, match="receipt hash"):
        h.call("ack", task_id="A", dispatch_id=dispatch, receipt=receipt)
    assert h.call("reconcile", task_id="A", dispatch_id=dispatch, quiescent=True, evidence="verified no live work/pending dispatch")["lease"] is False


@pytest.mark.parametrize("changed", ["A-result.json", "A-native.json"])
def test_completed_receipt_and_native_drift(h, changed):
    h.add()
    h.result(h.issue())
    (h.root / changed).write_text("tampered")
    with pytest.raises(InvalidEvent, match="receipt drift"):
        h.prepare()
    h.add("B", "worker-2", dependencies=["A"])
    assert "dependency:A:ARTIFACT_DRIFT" in str(h.prepare("B"))


def test_real_process_kill_after_durable_issue(h):
    h.add()
    dispatch = h.prepare()["dispatch_id"]
    event = {**h.binding, "op": "issue", "event_id": "killed-issue", "task_id": "A", "dispatch_id": dispatch}
    code = """import json,sys,time
from pathlib import Path
from project_coordinator import apply_event
result=apply_event(sys.argv[1],json.loads(sys.argv[2]),guard_context=json.loads(sys.argv[4]))
Path(sys.argv[3]).write_text(json.dumps(result))
time.sleep(30)
"""
    output = h.root / "durable-issue.json"
    child = subprocess.Popen([sys.executable, "-c", code, str(h.db), json.dumps(event), str(output), json.dumps(h.guard_context)])
    import time
    try:
        deadline = time.monotonic() + 15
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert output.exists()
        assert "native_request" in json.loads(output.read_text())
    finally:
        child.kill()
        child.wait(timeout=5)
    recovered = h.call("recover")
    assert recovered["tasks"][0]["status"] == "UNKNOWN"
    assert "native_request" not in apply_event(h.db, event)


@pytest.mark.parametrize("change", [
    {"dependencies": [{}]}, {"dependencies": [1]}, {"dependencies": "A"},
    {"dispatch_id": "injected"}, {"lease": True}, {"writer_paths": [None]},
])
def test_malformed_task_rejected_before_checkpoint(h, change):
    task = {"id": "A", "worker": "worker-1", "linear_ref": "linear:test-1", "beads_ref": "global:test-1", "prompt": "fixture",
            "source_digest": "source-1", "scope_digest": "scope-1", "dependencies": [], "writer_paths": [], **change}
    with pytest.raises(InvalidEvent):
        h.call("add", task=task)
    assert h.call("recover")["tasks"] == []
    h.add()
    assert h.prepare()["action"] == "PREPARED"


def test_dependency_cycle_rejected_before_checkpoint(h):
    h.add(dependencies=["B"])
    with pytest.raises(InvalidEvent, match="cycle"):
        h.add("B", "worker-2", dependencies=["A"])
    assert [task["id"] for task in h.call("recover")["tasks"]] == ["A"]


def test_late_result_cannot_reacquire_released_lease(h):
    h.add(paths=["owned"])
    dispatch = h.issue()
    h.call("reconcile", task_id="A", dispatch_id=dispatch, quiescent=True, evidence="verified task and pending transport stopped")
    h.add("B", "worker-2", paths=["owned"])
    assert h.prepare("B")["action"] == "PREPARED"
    with pytest.raises(InvalidEvent, match="lease already released"):
        h.result(dispatch, terminal=False, quiescent=False)
    tasks = h.call("recover")["tasks"]
    assert [task["id"] for task in tasks if task["lease"]] == ["B"]
    assert h.task()["status"] == "FAILED"


def test_acknowledged_can_abandon_verified_quiescent_work(h):
    h.add()
    dispatch = h.issue()
    ack = h.file("ack.json", {"threadId": "thread1"})
    h.call("ack", task_id="A", dispatch_id=dispatch, receipt=ack)
    assert h.task()["status"] == "ACKNOWLEDGED"
    result = h.call("reconcile", task_id="A", dispatch_id=dispatch, quiescent=True, evidence="actual terminal stopped worker receipt")
    assert result["action"] == "FAILED" and result["lease"] is False


def test_pending_checkpoint_requires_explicit_recover_before_fresh_issue(h):
    h.add()
    dispatch = h.prepare()["dispatch_id"]
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {"configurable": {"thread_id": "project-coordinator-v1"}}
        event = {**h.binding, "op": "observe", "event_id": "pending-observation", "worker": "worker-2", "state": "IDLE", "evidence": "fixture"}
        stream = graph.stream({"event": event}, config, stream_mode="checkpoints", durability="sync")
        next(stream)
        stream.close()
    with pytest.raises(InvalidEvent, match="pending checkpoint"):
        h.call("issue", task_id="A", dispatch_id=dispatch)
    assert h.call("summary")["pending_checkpoint"]
    h.call("recover")
    response = h.call("issue", task_id="A", dispatch_id=dispatch)
    assert response["native_request"]["arguments"]["threadId"] == "thread1"
    assert "native_request" not in h.call("issue", task_id="A", dispatch_id=dispatch)


def test_symlink_change_before_prepare_blocks_changed_identity(h):
    (h.root / "target").mkdir()
    h.add(paths=["future"])
    (h.root / "future").symlink_to(h.root / "target", target_is_directory=True)
    assert "claim-identity-changed:A" in str(h.prepare())
    h.add("B", "worker-2", paths=["target"])
    assert h.prepare("B")["action"] == "PREPARED"
    assert h.task()["lease"] is False


def test_symlink_change_after_prepare_holds_old_and_current_claims(h):
    (h.root / "old").mkdir()
    (h.root / "target").mkdir()
    alias = h.root / "alias"
    alias.symlink_to(h.root / "old", target_is_directory=True)
    h.add(paths=["alias"])
    dispatch = h.prepare()["dispatch_id"]
    alias.unlink()
    alias.symlink_to(h.root / "target", target_is_directory=True)
    response = h.call("issue", task_id="A", dispatch_id=dispatch)
    assert response["action"] == "WAIT" and "native_request" not in response
    assert h.task()["lease"]
    for name, path, worker in [("B", "old/child", "worker-2"), ("C", "target/child", "worker-3")]:
        h.add(name, worker, paths=[path])
        assert "writer-lease:A" in str(h.prepare(name))


def test_worker_availability_consumed_until_fresh_observation(h):
    h.add()
    dispatch = h.issue()
    assert h.call("status")["workers"]["worker-1"]["state"] == "BUSY"
    h.result(dispatch)
    h.add("B")
    assert "not-observed-idle" in str(h.prepare("B"))
    h.call("observe", worker="worker-1", state="IDLE", evidence="fresh native idle observation after A")
    assert h.prepare("B")["action"] == "PREPARED"


@pytest.mark.parametrize("receipt,command", [
    ([], "result"), ({"content": ["bad-item"]}, "ack"), ({"content": {}}, "ack"),
    ({"content": [{"type": "text", "text": []}]}, "ack"), ({"structuredContent": []}, "ack"),
])
def test_cli_malformed_receipt_is_bounded_json(h, receipt, command):
    h.add()
    dispatch = h.issue()
    descriptor = h.file("malformed.json", receipt)
    event = {**h.binding, "event_id": "malformed-receipt", "task_id": "A", "dispatch_id": dispatch, "receipt": descriptor}
    path = h.root / "event.json"
    path.write_text(json.dumps(event))
    process = subprocess.run([sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), command, "--input", str(path)], capture_output=True, text=True)
    assert process.returncode == 2
    assert process.stdout == "" and "Traceback" not in process.stderr
    assert len(json.loads(process.stderr)["error"]) <= 500
    assert h.call("recover")["tasks"][0]["lease"]


def test_exact_native_tool_target_and_multiline_prompt(h):
    prompt = 'Inspect only designated output.\nKeep "quoted" text and $literal intact.\nReport terminal child evidence.'
    h.call("add", task={"id": "A", "worker": "worker-1", "linear_ref": "linear:test-1", "beads_ref": "global:bead-1", "prompt": prompt,
                        "source_digest": "source-1", "scope_digest": "scope-1", "dependencies": [], "writer_paths": []})
    dispatch = h.prepare()["dispatch_id"]
    native = h.call("issue", task_id="A", dispatch_id=dispatch)["native_request"]
    assert native == {"tool": "mcp__codex_app__send_message_to_thread", "arguments": {
        "threadId": "thread1", "prompt": f"[dispatch_id:{dispatch}]\n{prompt}"}}


@pytest.mark.parametrize("invalidation", ["worker", "dependency"])
def test_readiness_invalidated_after_preparation_retains_lease(h, invalidation):
    h.add()
    h.result(h.issue())
    h.add("B", "worker-2", dependencies=["A"])
    dispatch = h.prepare("B")["dispatch_id"]
    if invalidation == "worker":
        h.call("observe", worker="worker-2", state="BUSY", evidence="fresh observed busy")
    else:
        (h.root / "A-output.json").write_text("drift")
    response = h.call("issue", task_id="B", dispatch_id=dispatch)
    assert response["action"] == "WAIT" and "native_request" not in response
    assert h.task("B")["lease"] and h.task("B")["status"] == "PREPARED"


def test_existing_v1_closed_failed_model_recovers_without_new_fields(h):
    h.add(paths=["legacy"])
    dispatch = h.issue()
    h.call("reconcile", task_id="A", dispatch_id=dispatch, quiescent=True, evidence="legacy closed receipt")
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {"configurable": {"thread_id": "project-coordinator-v1"}}
        model = graph.get_state(config).values["model"]
        model["tasks"]["A"].pop("writer_claims")
        model["tasks"]["A"].pop("prompt_sha256")
        graph.update_state(config, {"model": model}, as_node="readiness")
    for op in ("status", "recover"):
        task = h.call(op)["tasks"][0]
        assert task["status"] == "FAILED" and task["lease"] is False
