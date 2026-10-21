import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from project_coordinator.runtime import InvalidEvent, apply_event, build_graph
from test_runtime import Harness


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def checkpoint(h):
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        snapshot = graph.get_state({"configurable": {"thread_id": "project-coordinator-v1"}})
        return snapshot.config["configurable"]["checkpoint_id"], copy.deepcopy(snapshot.values["model"])


def retirement_event(h, task_id="A", **changes):
    task = checkpoint(h)[1]["tasks"][task_id]
    return {**h.binding, "op": "retire", "event_id": str(uuid.uuid4()), "task_id": task_id,
            "source_digest": task["source_digest"], "scope_digest": task["scope_digest"],
            "quiescent": True, "evidence": "work/run/retirement-evidence.json", **changes}


@pytest.mark.parametrize("dependency,expected", [([], "READY"), (["pending"], "WAIT")])
def test_retire_only_never_dispatched_queued_tasks(h, dependency, expected):
    h.add(dependencies=dependency)
    before = checkpoint(h)[1]["tasks"]["A"]
    assert before["status"] == expected and before["lease"] is False
    assert "dispatch_id" not in before and "dispatch_thread_id" not in before

    event = retirement_event(h)
    response = apply_event(h.db, event)

    after = checkpoint(h)[1]["tasks"]["A"]
    assert response["action"] == "RETIRED" and "native_request" not in response
    assert after["status"] == "RETIRED" and after["lease"] is False
    assert "dispatch_id" not in after and "dispatch_thread_id" not in after
    assert after["retirement"] == {
        "event_id": event["event_id"],
        "source_digest": before["source_digest"], "scope_digest": before["scope_digest"],
        "quiescent": True, "evidence": "work/run/retirement-evidence.json"}
    for field in ("id", "worker", "linear_ref", "beads_ref", "source_digest", "scope_digest",
                  "dependencies", "writer_paths", "writer_claims", "prompt_sha256"):
        assert after[field] == before[field]


@pytest.mark.parametrize("protected", ["PREPARED", "ISSUING", "UNKNOWN", "ACKNOWLEDGED", "FAILED", "COMPLETE"])
def test_retire_rejects_prepared_issued_or_terminal_tasks_without_checkpoint_change(h, protected):
    h.add()
    if protected == "PREPARED":
        h.prepare()
    elif protected == "ISSUING":
        dispatch = h.prepare()["dispatch_id"]
        response = h.call("issue", task_id="A", dispatch_id=dispatch)
        assert response["action"] == "ISSUING"
    else:
        dispatch = h.issue()
        if protected == "UNKNOWN":
            h.call("recover")
        elif protected == "ACKNOWLEDGED":
            receipt = h.file("ack.json", {"threadId": "thread1"})
            h.call("ack", task_id="A", dispatch_id=dispatch, receipt=receipt)
        elif protected == "FAILED":
            assert h.result(dispatch, acceptance=False)["action"] == "FAILED"
        elif protected == "COMPLETE":
            assert h.result(dispatch)["action"] == "COMPLETE"
    assert checkpoint(h)[1]["tasks"]["A"]["status"] == protected
    before = checkpoint(h)

    with pytest.raises(InvalidEvent, match="never-dispatched"):
        apply_event(h.db, retirement_event(h))

    assert checkpoint(h) == before


@pytest.mark.parametrize("changes,remove,error", [
    ({"project_id": "foreign"}, (), "project_id"),
    ({"root": "/foreign/root"}, (), "root"),
    ({"task_id": "missing"}, (), "unknown task_id"),
    ({"source_digest": "other"}, (), "source/scope identity"),
    ({"scope_digest": "other"}, (), "source/scope identity"),
    ({"quiescent": False}, (), "quiescence"),
    ({"evidence": ""}, (), "evidence"),
    ({}, ("source_digest",), "source_digest"),
    ({}, ("scope_digest",), "scope_digest"),
    ({}, ("quiescent",), "quiescence"),
    ({}, ("evidence",), "evidence"),
])
def test_retire_rejects_wrong_binding_identity_or_evidence(h, changes, remove, error):
    h.add()
    event = retirement_event(h)
    event.update(changes)
    for field in remove:
        event.pop(field)
    before = checkpoint(h)

    with pytest.raises(InvalidEvent, match=error):
        apply_event(h.db, event)

    assert checkpoint(h) == before


@pytest.mark.parametrize("evidence", [" ", "\t\n"])
def test_retire_rejects_whitespace_only_evidence_without_checkpoint_mutation(h, evidence):
    h.add()
    event = retirement_event(h, evidence=evidence)
    before = checkpoint(h)

    with pytest.raises(InvalidEvent, match="evidence"):
        apply_event(h.db, event)

    assert checkpoint(h) == before
    schema = json.loads((Path(__file__).resolve().parents[1] / "schema.json").read_text())
    retire = next(
        rule["then"] for rule in schema["allOf"]
        if rule.get("if", {}).get("properties", {}).get("op", {}).get("const") == "retire"
    )
    pattern = retire["properties"]["evidence"]["pattern"]
    assert re.search(pattern, evidence) is None
    assert re.search(pattern, "work/run/retirement-evidence.json")


def test_retirement_is_replayed_exactly_and_keeps_dependents_blocked(h):
    h.add()
    h.add("dependent", "worker-2", dependencies=["A"])
    event = retirement_event(h)
    first = apply_event(h.db, event)
    assert first["action"] == "RETIRED"
    assert apply_event(h.db, event)["action"] == "REPLAY"
    with pytest.raises(InvalidEvent, match="conflicting"):
        apply_event(h.db, {**event, "evidence": "different evidence"})
    blocked = h.prepare("dependent")
    assert blocked["action"] == "WAIT" and "dependency:A:RETIRED" in blocked["reasons"]

    with pytest.raises(InvalidEvent, match="retired task"):
        h.prepare("A")
    with pytest.raises(InvalidEvent, match="retired task"):
        h.call("issue", task_id="A", dispatch_id="not-a-real-dispatch")
    with pytest.raises(InvalidEvent, match="task already exists"):
        h.add()
    beads_dir = Path(h.guard_context["beads_dir"])
    original_record = json.loads((beads_dir / "record.json").read_text())
    replacement_record = {**original_record, "id": "test-1"}
    (beads_dir / "test-1.json").write_text(json.dumps(replacement_record))
    Path(h.guard_context["executable"]).write_text(
        "#!/usr/bin/env python3\nimport json,pathlib,sys\n"
        "bead_id=sys.argv[-2]\n"
        "record=pathlib.Path(__import__('os').environ['BEADS_DIR']) / "
        "('record.json' if bead_id == 'bead-1' else bead_id + '.json')\n"
        "print('[' + record.read_text() + ']')\n"
    )
    Path(h.guard_context["executable"]).chmod(0o700)
    h.call("add", task={"id": "replacement", "worker": "worker-1", "linear_ref": "linear:test-1",
                         "beads_ref": "global:test-1", "prompt": "replacement", "source_digest": "source-2",
                         "scope_digest": "scope-2", "writer_paths": [], "dependencies": []})
    assert h.call("prepare", task_id="replacement", source_digest="source-2", scope_digest="scope-2")["action"] == "PREPARED"


def test_retiring_queued_task_preserves_unrelated_held_dispatch(h):
    h.add("queued", "worker-2")
    h.add("held", "worker-1", paths=["held-path"])
    dispatch = h.issue("held")
    h.call("recover")
    before = checkpoint(h)[1]
    assert before["tasks"]["held"]["status"] == "UNKNOWN"
    assert before["tasks"]["held"]["lease"] is True
    assert before["workers"]["worker-1"]["state"] == "BUSY"

    response = apply_event(h.db, retirement_event(h, "queued"))
    after = checkpoint(h)[1]
    held = after["tasks"]["held"]
    assert response["action"] == "RETIRED" and "native_request" not in response
    assert held["status"] == "UNKNOWN" and held["lease"] is True
    assert held["dispatch_id"] == dispatch and held["dispatch_thread_id"] == "thread1"
    assert after["workers"]["worker-1"] == before["workers"]["worker-1"]
    assert "native_request" not in h.call("issue", task_id="held", dispatch_id=dispatch)


def test_cli_retire_replay_and_conflict_after_process_restart(h, tmp_path):
    h.add()
    event = retirement_event(h)
    input_path = tmp_path / "retire.json"
    input_path.write_text(json.dumps({k: v for k, v in event.items() if k != "op"}))
    source = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(source), env.get("PYTHONPATH", "")]))
    command = [sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db), "retire", "--input", str(input_path)]

    first = subprocess.run(command, cwd=source.parent.parent, env=env, capture_output=True, text=True)
    assert first.returncode == 0 and json.loads(first.stdout)["action"] == "RETIRED"
    assert "native_request" not in json.loads(first.stdout)
    replay = subprocess.run(command, cwd=source.parent.parent, env=env, capture_output=True, text=True)
    assert replay.returncode == 0 and json.loads(replay.stdout)["action"] == "REPLAY"
    assert "native_request" not in json.loads(replay.stdout)

    recover_command = [sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db),
                       "recover", "--input", str(input_path)]
    recovered = subprocess.run(recover_command, cwd=source.parent.parent, env=env, capture_output=True, text=True)
    assert recovered.returncode == 0
    retired = next(task for task in json.loads(recovered.stdout)["tasks"] if task["id"] == "A")
    assert retired["status"] == "RETIRED"
    assert retired["source_digest"] == event["source_digest"] and retired["scope_digest"] == event["scope_digest"]
    assert retired["retirement"]["event_id"] == event["event_id"]
    assert retired["retirement"]["evidence"] == event["evidence"]

    changed = {k: v for k, v in event.items() if k != "op"}
    changed["evidence"] = "work/run/conflicting-evidence.json"
    input_path.write_text(json.dumps(changed))
    conflict = subprocess.run(command, cwd=source.parent.parent, env=env, capture_output=True, text=True)
    assert conflict.returncode == 2 and conflict.stdout == ""
    assert "conflicting" in json.loads(conflict.stderr)["error"]
    retired = checkpoint(h)[1]["tasks"]["A"]
    assert retired["status"] == "RETIRED"
    assert retired["source_digest"] == event["source_digest"] and retired["scope_digest"] == event["scope_digest"]
    assert retired["retirement"]["evidence"] == event["evidence"]
