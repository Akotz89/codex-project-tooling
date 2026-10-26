from datetime import datetime, timezone
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from threading import Thread
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

import project_coordinator.runtime as runtime
from project_coordinator.http_api import handler, serve
from project_coordinator.runtime import InvalidEvent, apply_event, build_graph, digest, materialize_bundle, project_graph, read_model, task_bundle
from test_runtime import h


def reference(root, ref_id, kind, path, *, required=True, dynamic=False):
    file = root / path
    return {"id": ref_id, "kind": kind, "path": path,
            "sha256": digest(file) if file.is_file() and not dynamic else None,
            "revision": "abc123" if not dynamic else None,
            "updated_at": datetime.now(timezone.utc).isoformat() if not dynamic else None,
            "required": required, "dynamic": dynamic, "depends_on": [],
            "dependency_status": "dynamic" if dynamic else "known"}


def add(h, task_id, *, worker="worker-1", dependencies=(), references=(), linear_ref="linear:test-1"):
    return h.call("add", task={"id": task_id, "worker": worker, "linear_ref": linear_ref,
                                "beads_ref": "global:bead-1", "prompt": f"Build {task_id}",
                                "source_digest": "source-1", "scope_digest": "scope-1",
                                "dependencies": list(dependencies), "writer_paths": [],
                                "references": list(references)})


def test_task_bundle_closes_transitive_dependencies_and_marks_stale_refs(h):
    for path in ("src.py", "schema.json", "test.py", "changed.py"):
        (h.root / path).write_text(path)
    add(h, "base", references=[reference(h.root, "src", "source", "src.py")])
    add(h, "middle", dependencies=["base"], references=[reference(h.root, "schema", "schema", "schema.json")])
    add(h, "top", dependencies=["middle"], references=[
        reference(h.root, "test", "test", "test.py"),
        {"id": "gone", "kind": "artifact", "path": "gone.json", "sha256": "0" * 64,
         "revision": "r1", "updated_at": None, "required": True, "dynamic": False,
         "depends_on": [], "dependency_status": "known"},
        {"id": "moving", "kind": "source", "path": "moving.py", "sha256": None,
         "revision": None, "updated_at": None, "required": False, "dynamic": True,
         "depends_on": [], "dependency_status": "dynamic"},
        reference(h.root, "changed", "config", "changed.py")])
    (h.root / "changed.py").write_text("new bytes")
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "top"})
    assert [task["id"] for task in bundle["tasks"]] == ["base", "middle", "top"]
    assert {ref["id"]: ref["status"] for ref in bundle["references"]} == {
        "src": "current", "schema": "current", "test": "current", "gone": "missing",
        "moving": "dynamic", "changed": "changed"}
    assert bundle["freshness"]["required_gaps"] == 2
    assert {edge["type"] for edge in bundle["graph"]["edges"]} >= {"depends_on", "references_source", "references_schema", "references_test"}
    assert any(node.get("status") == "dynamic" for node in bundle["graph"]["nodes"])


def test_null_hash_nondynamic_references_are_unavailable_even_when_files_exist(h):
    (h.root / "present.py").write_text("present")
    add(h, "null-hashes", references=[
        {"id": "present", "kind": "source", "path": "present.py", "sha256": None,
         "revision": None, "updated_at": None, "required": True, "dynamic": False,
         "depends_on": [], "dependency_status": "unknown"},
        {"id": "missing", "kind": "source", "path": "missing.py", "sha256": None,
         "revision": None, "updated_at": None, "required": True, "dynamic": False,
         "depends_on": [], "dependency_status": "unknown"},
        {"id": "dynamic", "kind": "source", "path": "generated.py", "sha256": None,
         "revision": None, "updated_at": None, "required": False, "dynamic": True,
         "depends_on": [], "dependency_status": "dynamic"},
    ])
    bundle = h.call("bundle", task_id="null-hashes")
    assert {ref["id"]: ref["status"] for ref in bundle["references"]} == {
        "present": "unavailable", "missing": "unavailable", "dynamic": "dynamic"}


def test_add_defaults_omitted_dynamic_dependency_status_to_dynamic(h):
    add(h, "dynamic-default", references=[{
        "id": "generated", "kind": "source", "path": "generated.py", "sha256": None,
        "revision": None, "updated_at": None, "required": False, "dynamic": True,
    }])
    bundle = h.call("bundle", task_id="dynamic-default")
    assert bundle["references"][0]["dependency_status"] == "dynamic"
    assert {edge["status"] for edge in bundle["unknown_edges"]} == {"dynamic"}


def test_legacy_task_is_unindexed_until_references_are_cas_attached_to_existing_dispatch(h):
    (h.root / "source.py").write_text("pinned source")
    h.add("legacy")
    dispatch_id = h.issue("legacy")
    before = h.call("bundle", task_id="legacy")
    task_before = before["tasks"][-1]
    assert task_before["source_coverage"] == "unindexed"
    assert not before["freshness"]["current"]
    assert any(edge["status"] == "unindexed" and edge["required"] for edge in before["unknown_edges"])
    assert before["reference_state"]["revision"] is None

    # Persist a pre-feature checkpoint shape, then use the supported CLI refresh path.
    with sqlite3.connect(h.db, check_same_thread=False) as conn:
        graph = build_graph(SqliteSaver(conn))
        config = {"configurable": {"thread_id": "project-coordinator-v1"}}
        old_model = copy.deepcopy(graph.get_state(config).values["model"])
        for field in ("references", "reference_revision", "reference_digest"):
            old_model["tasks"]["legacy"].pop(field, None)
        graph.update_state(config, {"model": old_model}, as_node="readiness")
    persisted, _ = read_model(h.db, h.binding)
    assert "references" not in persisted["tasks"]["legacy"]
    old_bundle = task_bundle(persisted, "legacy")
    assert old_bundle["tasks"][-1]["source_coverage"] == "unindexed"
    assert not old_bundle["freshness"]["current"]

    ref = reference(h.root, "source", "source", "source.py")
    event = {**h.binding, "op": "refresh_references", "event_id": "attach-source-index",
             "task_id": "legacy", "source_digest": task_before["source_digest"],
             "scope_digest": task_before["scope_digest"],
             "expected_reference_revision": before["reference_state"]["revision"],
             "expected_reference_digest": before["reference_state"]["digest"],
             "reference_revision": "source-index-r1", "references": [ref]}
    input_path = h.root / "refresh-references.json"
    input_path.write_text(json.dumps(event))
    command = [sys.executable, "-m", "project_coordinator.cli", "--db", str(h.db),
               "refresh-references", "--input", str(input_path)]
    result = subprocess.run(command, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["action"] == "REFERENCES_REFRESHED"
    replay = subprocess.run(command, text=True, capture_output=True)
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["action"] == "REPLAY"
    after = h.call("bundle", task_id="legacy")
    attached = after["tasks"][-1]
    assert attached["source_coverage"] == "declared"
    assert after["freshness"]["current"] and after["references"][0]["status"] == "current"
    assert after["reference_state"]["revision"] == "source-index-r1"
    assert attached["status"] == task_before["status"]
    assert attached.get("dispatch_id") == dispatch_id
    assert attached.get("beads_claim") == task_before.get("beads_claim")
    stale = {**event, "event_id": "stale-source-index", "reference_revision": "source-index-r2"}
    with pytest.raises(InvalidEvent, match="compare-and-swap mismatch"):
        apply_event(h.db, stale)


def test_materialization_skips_optional_unknown_edges_and_rejects_required_unknown_edges(h):
    (h.root / "optional.py").write_text("optional")
    (h.root / "dynamic.py").write_text("generated")
    optional_unknown = {**reference(h.root, "optional", "source", "optional.py", required=False),
                        "dependency_status": "unknown"}
    optional_dynamic = {**reference(h.root, "dynamic", "source", "dynamic.py", required=False, dynamic=True),
                        "dependency_status": "dynamic"}
    add(h, "optional-unknown", references=[optional_unknown, optional_dynamic])
    optional_bundle = h.call("bundle", task_id="optional-unknown")
    assert optional_bundle["freshness"]["current"]
    assert optional_bundle["freshness"]["required_unknown_edges"] == 0
    assert {edge["status"] for edge in optional_bundle["unknown_edges"]} == {"unknown", "dynamic"}
    materialized = materialize_bundle(str(h.root), optional_bundle, "optional-out")
    assert materialized["written"] == []
    assert {item["status"] for item in materialized["skipped"]} == {"unknown_dependency", "dynamic"}
    assert not (h.root / "optional-out").exists()

    required_unknown = {**reference(h.root, "required", "source", "optional.py"),
                        "dependency_status": "unknown"}
    add(h, "required-unknown", references=[required_unknown])
    required_bundle = h.call("bundle", task_id="required-unknown")
    assert not required_bundle["freshness"]["current"]
    with pytest.raises(InvalidEvent, match="required unknown"):
        materialize_bundle(str(h.root), required_bundle, "required-out")
    assert not (h.root / "required-out").exists()

    (h.root / "current.py").write_text("current")
    current_input = reference(h.root, "consumer-current-input", "source", "current.py")
    add(h, "required-prerequisite", references=[required_unknown])
    add(h, "required-consumer", dependencies=["required-prerequisite"], references=[current_input])
    transitive_bundle = h.call("bundle", task_id="required-consumer")
    assert [task["id"] for task in transitive_bundle["tasks"]] == ["required-prerequisite", "required-consumer"]
    assert transitive_bundle["freshness"]["required_gaps"] == 0
    assert transitive_bundle["freshness"]["required_unknown_edges"] == 1
    reference_statuses = {ref["id"]: ref["status"] for ref in transitive_bundle["references"]}
    assert reference_statuses["consumer-current-input"] == "current"
    with pytest.raises(InvalidEvent, match="required unknown"):
        materialize_bundle(str(h.root), transitive_bundle, "required-transitive-out")
    assert not (h.root / "required-transitive-out").exists()


def test_declared_reference_graph_resolves_transitive_file_closure(h):
    for filename in ("consumer.py", "compiler.py", "schema.json"):
        (h.root / filename).write_text(filename)
    add(h, "closure", references=[
        {**reference(h.root, "consumer", "source", "consumer.py"),
         "depends_on": [{"id": "compiler", "type": "imports"}], "dependency_status": "known"},
        {**reference(h.root, "compiler", "source", "compiler.py"),
         "depends_on": [{"id": "schema", "type": "schema_ref"}], "dependency_status": "known"},
        reference(h.root, "schema", "schema", "schema.json"),
    ])
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "closure"})
    assert bundle["freshness"]["current"]
    assert not bundle["unknown_edges"]
    adjacency = {}
    for edge in bundle["graph"]["edges"]:
        adjacency.setdefault(edge["from"], set()).add(edge["to"])
    stack = ["source:closure:consumer"]
    reached = set()
    while stack:
        current = stack.pop()
        if current in reached:
            continue
        reached.add(current)
        stack.extend(adjacency.get(current, ()))
    reached_paths = {node.split(":", 2)[2] for node in reached if node.startswith(("source:", "schema:", "test:"))}
    assert reached_paths == {"consumer", "compiler", "schema"}


def test_graph_and_bundle_projection_leave_model_unchanged(h):
    (h.root / "ref.py").write_text("legacy")
    add(h, "pure", references=[reference(h.root, "ref", "source", "ref.py")])
    model, _ = read_model(h.db, h.binding)
    model["tasks"]["pure"]["references"][0].pop("depends_on")
    model["tasks"]["pure"]["references"][0].pop("dependency_status")
    before = json.loads(json.dumps(model))
    project_graph(model)
    task_bundle(model, "pure")
    assert model == before


def test_graph_projects_latest_report_and_points_to_paged_timeline(h):
    add(h, "reported")
    model, _ = read_model(h.db, h.binding)
    model["reports"] = [{"task_id": "reported", "event_id": f"report-{index}", "kind": "progress",
                         "recorded_at": f"2026-10-26T00:00:{index:02d}Z", "revision": index + 1,
                         "source": f"source:{index}", "evidence": [f"evidence:{index}"]}
                        for index in range(40)]
    graph = project_graph(model)
    evidence = [node for node in graph["nodes"] if node["kind"] == "evidence"]
    assert [node["event_id"] for node in evidence] == ["report-39"]
    assert graph["report_history"]["reported"] == {"total": 40, "latest_revision": 40}
    assert "paged summary report timeline" in graph["report_timeline"]
    latest = evidence[0]
    assert latest["source"] == "source:39" and latest["evidence"] == ["evidence:39"]
    item = next(node for node in graph["nodes"] if node["id"] == "work-item:reported")
    role = next(node for node in graph["nodes"] if node["id"] == "role:worker-1")
    assert item["reference"] == "linear:test-1"
    assert role["thread_id"] == "thread1"
    assert {("task:reported", "work-item:reported", "tracks"),
            ("task:reported", "role:worker-1", "assigned_to")} <= {
                (edge["from"], edge["to"], edge["type"]) for edge in graph["edges"]}


def test_complete_task_bundle_has_no_arbitrary_reference_or_closure_ceiling(h):
    refs = [{"id": f"r{i}", "kind": "source", "path": f"missing/{i}.py", "sha256": None,
             "revision": None, "updated_at": None, "required": False, "dynamic": False,
             "depends_on": [], "dependency_status": "known"} for i in range(520)]
    add(h, "many-refs", references=refs)
    model, _ = read_model(h.db, h.binding)
    task = model["tasks"]["many-refs"]
    for index in range(130):
        task_id = f"closure-{index}"
        model["tasks"][task_id] = {**task, "id": task_id,
                                   "dependencies": [f"closure-{index - 1}"] if index else [],
                                   "references": [], "artifacts": [], "writer_claims": [], "writer_paths": []}
    bundle = task_bundle(model, "closure-129")
    assert len(bundle["tasks"]) == 130
    assert len(bundle["references"]) == 0
    reference_bundle = task_bundle(model, "many-refs")
    assert len(reference_bundle["references"]) == 520


def test_reference_hash_streams_file_larger_than_prior_fixed_ceiling(h):
    source = h.root / "large-source.bin"
    with source.open("wb") as stream:
        stream.truncate(32 * 1024 * 1024 + 1)
    add(h, "large-source", references=[reference(h.root, "large", "source", "large-source.bin")])
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "large-source"})
    assert bundle["references"][0]["status"] == "current"


def test_bundle_role_binding_and_repo_relative_path_validation(h):
    (h.root / "source.py").write_text("ok")
    add(h, "owned", references=[reference(h.root, "source", "source", "source.py")])
    assert apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "owned",
                              "role": "worker-1", "thread_id": "thread1"})["task_id"] == "owned"
    with pytest.raises(InvalidEvent, match="caller binding"):
        apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "owned",
                           "role": "worker-1", "thread_id": "wrong"})
    for path in ("../outside", str(h.root / "source.py")):
        bad = {"id": "bad", "kind": "source", "path": path, "sha256": "0" * 64,
               "revision": None, "updated_at": None, "required": True, "dynamic": False}
        with pytest.raises(InvalidEvent, match="repository-relative"):
            add(h, f"bad-{len(path)}", references=[bad])


def test_bundle_refuses_symlink_path_escape(h):
    outside = h.root.parent / "outside-source.py"
    outside.write_text("outside")
    (h.root / "escape.py").symlink_to(outside)
    ref = {"id": "escape", "kind": "source", "path": "escape.py", "sha256": digest(outside),
           "revision": None, "updated_at": None, "required": True, "dynamic": False,
           "depends_on": [], "dependency_status": "known"}
    add(h, "escape", references=[ref])
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "escape"})
    assert bundle["references"][0]["status"] == "unavailable"


def test_artifact_reference_does_not_canonicalize_away_an_internal_symlink(h):
    target = h.root / "actual.txt"
    target.write_text("artifact bytes")
    (h.root / "alias.txt").symlink_to(target)
    add(h, "artifact-symlink")
    model, _ = read_model(h.db, h.binding)
    model["tasks"]["artifact-symlink"]["artifacts"] = [{"path": "alias.txt", "sha256": digest(target)}]
    artifact = next(ref for ref in task_bundle(model, "artifact-symlink")["references"]
                    if ref["kind"] == "artifact")
    assert artifact["status"] == "unavailable"


def test_materialization_copies_verified_refs_and_refuses_changed_required(h):
    (h.root / "src" / "pkg").mkdir(parents=True)
    source = h.root / "src" / "pkg" / "module.py"
    source.write_text("verified bytes")
    add(h, "materialize", references=[reference(h.root, "module", "source", "src/pkg/module.py")])
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "materialize"})
    result = materialize_bundle(str(h.root), bundle, "work/materialized")
    copied = h.root / "work/materialized/src/pkg/module.py"
    assert copied.read_bytes() == source.read_bytes()
    assert result["written"] == [{"path": "src/pkg/module.py", "sha256": digest(source)}]
    source.write_text("changed")
    stale = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "materialize"})
    with pytest.raises(InvalidEvent, match="required reference is changed"):
        materialize_bundle(str(h.root), stale, "work/other")
    with pytest.raises(InvalidEvent, match="repository-relative"):
        materialize_bundle(str(h.root), stale, "../outside")


def test_materialization_streams_and_rolls_back_if_source_grows_after_preflight(h, monkeypatch):
    source = h.root / "growing.txt"
    source.write_text("12345")
    add(h, "growing", references=[reference(h.root, "growing", "source", "growing.txt")])
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "growing"})
    original_open = runtime.open_reference_file
    calls = 0
    def grow_before_copy(root, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            source.write_text("123456")
        return original_open(root, value)
    monkeypatch.setattr(runtime, "open_reference_file", grow_before_copy)
    with pytest.raises(InvalidEvent, match="source changed during materialization"):
        materialize_bundle(str(h.root), bundle, "work/growth")
    assert not (h.root / "work" / "growth" / "growing.txt").exists()
    assert not (h.root / "work" / "growth").exists()


def test_materialization_refuses_existing_symlink_destination_component(h):
    source = h.root / "source.txt"
    source.write_text("source")
    add(h, "destination-symlink", references=[reference(h.root, "source", "source", "source.txt")])
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "destination-symlink"})
    outside = h.root.parent / "materialize-outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("preserve")
    (h.root / "work").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError):
        materialize_bundle(str(h.root), bundle, "work/destination")
    assert sentinel.read_text() == "preserve"
    assert not (outside / "destination" / "source.txt").exists()


def test_materialization_cleanup_uses_pinned_destination_fds_after_symlink_swap(h, monkeypatch):
    (h.root / "pkg").mkdir()
    first = h.root / "pkg" / "first.py"
    second = h.root / "pkg" / "second.py"
    first.write_text("first")
    second.write_text("second")
    add(h, "destination-race", references=[reference(h.root, "first", "source", "pkg/first.py"),
                                             reference(h.root, "second", "source", "pkg/second.py")])
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "destination-race"})
    outside = h.root.parent / "materialize-race-outside"
    outside.mkdir()
    sentinel = outside / "first.py"
    sentinel.write_text("outside keep")
    destination = h.root / "work" / "safe"
    package_dir = destination / "pkg"
    original_open = runtime.open_reference_file
    calls = 0

    def swap_destination_before_second_copy(root, value):
        nonlocal calls
        calls += 1
        if calls == 4:
            package_dir.rename(destination / "pkg-held")
            package_dir.symlink_to(outside, target_is_directory=True)
            second.write_text("changed after preflight")
        return original_open(root, value)

    monkeypatch.setattr(runtime, "open_reference_file", swap_destination_before_second_copy)
    # Materialize beneath a nested component so the fd walk pins `pkg` before the swap.
    with pytest.raises(InvalidEvent, match="source changed during materialization"):
        materialize_bundle(str(h.root), bundle, "work/safe")
    assert sentinel.read_text() == "outside keep"
    assert not (outside / "second.py").exists()
    assert list((destination / "pkg-held").iterdir()) == []


def test_materialization_rolls_back_only_new_outputs_after_copy_drift(h, monkeypatch):
    (h.root / "a.py").write_text("first")
    (h.root / "b.py").write_text("second")
    add(h, "rollback", references=[reference(h.root, "a", "source", "a.py"),
                                      reference(h.root, "b", "source", "b.py")])
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "rollback"})
    destination = h.root / "work" / "rollback"
    destination.mkdir(parents=True)
    keep = destination / "keep.txt"
    keep.write_text("pre-existing")
    original_open = runtime.open_reference_file
    calls = 0

    def drift_on_second_source(root, path):
        nonlocal calls
        calls += 1
        if calls == 4:
            (h.root / "b.py").write_text("drifted")
        return original_open(root, path)

    monkeypatch.setattr(runtime, "open_reference_file", drift_on_second_source)
    with pytest.raises(InvalidEvent, match="source changed during materialization"):
        materialize_bundle(str(h.root), bundle, "work/rollback")
    assert keep.read_text() == "pre-existing"
    assert not (destination / "a.py").exists()
    assert not (destination / "b.py").exists()
    assert list(destination.iterdir()) == [keep]


def test_revisions_filter_unchanged_and_irrelevant_task_wakes(h):
    add(h, "A", worker="worker-1")
    add(h, "B", worker="worker-2")
    initial = h.call("summary")["revision"]
    h.prepare("A")
    relevant = h.call("updates", role="worker-1", thread_id="thread1", after_revision=initial)
    irrelevant = h.call("updates", role="worker-2", thread_id="thread2", after_revision=initial)
    assert relevant["changed"] and [u["task_id"] for u in relevant["updates"]] == ["A"]
    assert not irrelevant["changed"] and irrelevant["updates"] == []
    repeated = h.call("updates", role="worker-1", thread_id="thread1", after_revision=relevant["revision"])
    assert not repeated["changed"] and repeated["updates"] == []
    with pytest.raises(InvalidEvent, match="thread binding"):
        h.call("updates", role="worker-1", thread_id="thread2", after_revision=0)


def test_coordinator_task_report_is_not_duplicated_as_task_state(h):
    add(h, "A", worker="worker-1", linear_ref="CPI-415")
    before = h.call("summary")["revision"]
    apply_event(h.db, {**h.binding, "op": "report", "event_id": "worker-progress", "task_id": "A",
                       "reporter": "worker-1", "reporter_thread_id": "thread1", "kind": "progress",
                       "occurred_at": datetime.now(timezone.utc).isoformat(), "state": "working",
                       "detail": "Observed current progress", "source": "source:test",
                       "evidence": ["evidence:test"], "claim_id": "global:bead-1"})
    updates = h.call("updates", role="coordinator", thread_id="root", after_revision=before)
    assert updates["updates"] == [{"event_id": "worker-progress", "task_id": "A", "role": "worker-1",
                                   "kind": "progress", "revision": before + 1,
                                   "recorded_at": updates["updates"][0]["recorded_at"]}]


def test_cross_role_project_report_reaches_only_assigned_task_role(h):
    add(h, "A", worker="worker-1", linear_ref="CPI-415")
    add(h, "B", worker="worker-2")
    before = h.call("summary")["revision"]
    event = {**h.binding, "op": "project_report", "event_id": "sm-update",
             "work_item": "https://linear.app/codex-azyrra/issue/CPI-415/task-title",
             "reporter": "worker-2", "reporter_thread_id": "thread2", "kind": "progress",
             "occurred_at": datetime.now(timezone.utc).isoformat(), "state": "updated",
             "detail": "Relevant cross-role status", "source": "source:test", "evidence": ["evidence:test"]}
    apply_event(h.db, event)
    received = h.call("updates", role="worker-1", thread_id="thread1", after_revision=before)
    ignored = h.call("updates", role="worker-2", thread_id="thread2", after_revision=before)
    assert received["changed"] and received["updates"][0]["event_id"] == "sm-update"
    assert not ignored["changed"]
    assert not h.call("updates", role="worker-1", thread_id="thread1", after_revision=received["revision"])["changed"]


def test_root_directs_latest_updates_only_to_registered_project_roles(h):
    add(h, "A", worker="worker-1", linear_ref="CPI-415")
    add(h, "B", worker="worker-2", linear_ref="CPI-416")
    policy = {"schema_version": 1, "project_id": h.binding["project_id"], "root": str(h.root),
              "worker_registrations": [], "legacy_leaseless_beads": [],
              "project_report_registrations": [
                  {"role": "product_owner", "thread_id": "po-thread"},
                  {"role": "scrum_master", "thread_id": "sm-thread"}]}
    cursor = h.call("summary")["revision"]
    common = {**h.binding, "op": "project_report", "work_item": "https://linear.app/codex-azyrra/issue/CPI-415/task-title",
              "reporter": "coordinator", "reporter_thread_id": "root", "kind": "result",
              "occurred_at": datetime.now(timezone.utc).isoformat(), "state": "current",
              "detail": "Latest accepted outcome", "source": "linear:CPI-415", "evidence": ["evidence:latest"],
              "recipients": ["product_owner", "scrum_master"]}
    apply_event(h.db, {**common, "event_id": "root-directed-latest"}, project_policy=policy)
    po_model, _ = read_model(h.db, h.binding)
    from project_coordinator.runtime import read_updates
    po = read_updates(po_model, {"role": "product_owner", "thread_id": "po-thread", "after_revision": cursor},
                      project_policy=policy)
    sm = read_updates(po_model, {"role": "scrum_master", "thread_id": "sm-thread", "after_revision": cursor},
                      project_policy=policy)
    assert [item["event_id"] for item in po["updates"]] == ["root-directed-latest"]
    assert [item["event_id"] for item in sm["updates"]] == ["root-directed-latest"]
    assert not read_updates(po_model, {"role": "product_owner", "thread_id": "po-thread",
                                       "after_revision": po["revision"]}, project_policy=policy)["changed"]
    bundle = apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "A",
                                "role": "product_owner", "thread_id": "po-thread"}, project_policy=policy)
    assert bundle["directed_reports"][0]["event_id"] == "root-directed-latest"
    with pytest.raises(InvalidEvent, match="caller binding"):
        apply_event(h.db, {**h.binding, "op": "bundle", "task_id": "A",
                           "role": "product_owner", "thread_id": "wrong"}, project_policy=policy)
    with pytest.raises(InvalidEvent, match="worker recipient must own"):
        apply_event(h.db, {**common, "event_id": "root-directed-wrong-worker", "recipients": ["worker-2"]},
                    project_policy=policy)
    add(h, "C", worker="worker-2", linear_ref="CPI-415")
    with pytest.raises(InvalidEvent, match="exactly one bound task"):
        apply_event(h.db, {**common, "event_id": "root-directed-ambiguous", "work_item": "CPI-415"},
                    project_policy=policy)
    po_token, sm_token = "product-owner-token-long-enough-123", "scrum-master-token-long-enough-123"
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(
        str(h.db), h.binding["project_id"], h.binding["root"], "owner-token-long-enough-123456",
        {"product_owner": po_token, "scrum_master": sm_token}, policy))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        response = json.loads(urlopen(Request(
            f"{base}/api/updates?role=product_owner&thread_id=po-thread&after_revision={cursor}",
            headers={"Authorization": f"Bearer {po_token}"})).read())
        assert [item["event_id"] for item in response["updates"]] == ["root-directed-latest"]
        with pytest.raises(HTTPError) as wrong_thread:
            urlopen(Request(f"{base}/api/updates?role=product_owner&thread_id=sm-thread&after_revision={cursor}",
                            headers={"Authorization": f"Bearer {po_token}"}))
        assert wrong_thread.value.code == 400
        role_bundle = json.loads(urlopen(Request(
            f"{base}/api/tasks/A/bundle?role=product_owner&thread_id=po-thread",
            headers={"Authorization": f"Bearer {po_token}"})).read())
        assert role_bundle["directed_reports"][0]["event_id"] == "root-directed-latest"
        with pytest.raises(HTTPError) as unrelated_task:
            urlopen(Request(f"{base}/api/tasks/B/bundle?role=product_owner&thread_id=po-thread",
                            headers={"Authorization": f"Bearer {po_token}"}))
        assert unrelated_task.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_same_team_different_linear_issue_does_not_wake_other_task(h):
    add(h, "A", worker="worker-1", linear_ref="CPI-415")
    add(h, "B", worker="worker-2", linear_ref="CPI-416")
    before = h.call("summary")["revision"]
    event = {**h.binding, "op": "project_report", "event_id": "other-issue",
             "work_item": "https://linear.app/codex-azyrra/issue/CPI-416/other-task",
             "reporter": "worker-1", "reporter_thread_id": "thread1", "kind": "progress",
             "occurred_at": datetime.now(timezone.utc).isoformat(), "state": "updated",
             "detail": "Different task", "source": "linear:CPI-416", "evidence": ["evidence:test"]}
    apply_event(h.db, event)
    assert not h.call("updates", role="worker-1", thread_id="thread1", after_revision=before)["changed"]
    updates = h.call("updates", role="worker-2", thread_id="thread2", after_revision=before)
    assert updates["changed"] and updates["updates"][0]["task_id"] == "B"


def test_updates_prefer_latest_issue_outcome_and_suppress_duplicate_relays(h):
    add(h, "A", worker="worker-1", linear_ref="CPI-415")
    add(h, "B", worker="worker-2")
    before = h.call("summary")["revision"]
    url = "https://linear.app/codex-azyrra/issue/CPI-415/task-title"
    for event_id, reporter, kind, state, detail in (
        ("old-sm-blocker", "worker-2", "blocker", "blocked", "intermediate failing cursor 111"),
        ("latest-po-outcome", "worker-2", "result", "accepted", "terminal cursor 119, 13 of 13 passing"),
    ):
        apply_event(h.db, {**h.binding, "op": "project_report", "event_id": event_id, "work_item": url,
                           "reporter": reporter, "reporter_thread_id": "thread2", "kind": kind,
                           "occurred_at": datetime.now(timezone.utc).isoformat(), "state": state,
                           "detail": detail, "source": "linear:CPI-415", "evidence": ["evidence:test"]})
    owner = h.call("updates", role="coordinator", thread_id="root", after_revision=before)
    participant = h.call("updates", role="worker-1", thread_id="thread1", after_revision=before)
    assert [item["event_id"] for item in owner["updates"]] == ["latest-po-outcome"]
    assert [item["event_id"] for item in participant["updates"]] == ["latest-po-outcome"]
    assert not h.call("updates", role="coordinator", thread_id="root", after_revision=owner["revision"])["changed"]


def test_worker_receives_current_terminal_task_state_not_old_blocker(h):
    add(h, "A", worker="worker-1", linear_ref="CPI-415")
    old = {**h.binding, "op": "project_report", "event_id": "obsolete-blocker",
           "work_item": "https://linear.app/codex-azyrra/issue/CPI-415/task-title",
           "reporter": "worker-2", "reporter_thread_id": "thread2", "kind": "blocker",
           "occurred_at": datetime.now(timezone.utc).isoformat(), "state": "blocked",
           "detail": "intermediate failure cursor 111", "source": "linear:CPI-415", "evidence": ["evidence:test"]}
    apply_event(h.db, old)
    blocker_revision = h.call("summary")["revision"]
    record_path = h.root / ".beads" / "record.json"
    claim = json.loads(record_path.read_text())
    claim["external_ref"] = "CPI-415"
    record_path.write_text(json.dumps(claim))
    dispatch = h.issue("A")
    h.result(dispatch, "A")
    update = h.call("updates", role="worker-1", thread_id="thread1", after_revision=blocker_revision)
    assert update["changed"]
    assert update["updates"] == [{"event_id": update["updates"][0]["event_id"], "task_id": "A",
                                  "role": "worker-1", "kind": "task_state", "status": "COMPLETE",
                                  "reasons": [],
                                  "revision": update["revision"]}]
    assert update["updates"][0]["event_id"] != "obsolete-blocker"


def test_assignment_update_is_delivered_once_to_the_assigned_role(h):
    cursor = h.call("summary")["revision"]
    add(h, "assigned", worker="worker-1")
    update = h.call("updates", role="worker-1", thread_id="thread1", after_revision=cursor)
    assert update["changed"] and update["updates"][0]["task_id"] == "assigned"
    assert update["updates"][0]["kind"] == "task_state"
    assert not h.call("updates", role="worker-1", thread_id="thread1", after_revision=update["revision"])["changed"]


def test_readiness_change_stamps_dependent_task_for_its_role_update(h):
    add(h, "prerequisite", worker="worker-1")
    add(h, "dependent", worker="worker-2", dependencies=["prerequisite"])
    cursor = h.call("summary")["revision"]
    dispatch = h.issue("prerequisite")
    result = h.result(dispatch, "prerequisite")
    model, _ = read_model(h.db, h.binding)
    dependent = model["tasks"]["dependent"]
    result_event_id = next(event_id for event_id, info in model["events"].items()
                           if info.get("task_id") == "prerequisite" and info.get("revision") == result["revision"])
    assert dependent["status"] == "READY"
    assert dependent["revision"] == result["revision"]
    assert dependent["last_event_id"] == result_event_id

    update = h.call("updates", role="worker-2", thread_id="thread2", after_revision=cursor)
    task_update = next(item for item in update["updates"] if item["task_id"] == "dependent")
    assert update["changed"]
    assert task_update["event_id"] == result_event_id
    assert task_update["status"] == "READY" and task_update["revision"] == result["revision"]


def test_participant_api_accepts_only_bound_own_report_and_retries_idempotently(h):
    h.add()
    dispatch = h.issue()
    role_token = "p" * 32
    owner_token = "o" * 32
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(str(h.db), h.binding["project_id"], h.binding["root"], owner_token,
                                                            {"worker-1": role_token}))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    body = {"event_id": "participant-progress", "task_id": "A", "claim_id": "global:bead-1",
            "dispatch_id": dispatch, "source_digest": "source-1", "kind": "progress",
            "occurred_at": datetime.now(timezone.utc).isoformat(), "state": "running",
            "detail": "Verified progress", "source": "source:test", "evidence": ["evidence:test"]}
    try:
        bundle = json.loads(urlopen(Request(base + "/api/tasks/A/bundle?role=worker-1",
                                                  headers={"Authorization": f"Bearer {role_token}"})).read())
        assert bundle["task_id"] == "A"
        explicitly_bound = json.loads(urlopen(Request(base + "/api/tasks/A/bundle?role=worker-1&thread_id=thread1",
                                                           headers={"Authorization": f"Bearer {role_token}"})).read())
        assert explicitly_bound["task_id"] == "A"
        with pytest.raises(HTTPError) as wrong_thread:
            urlopen(Request(base + "/api/tasks/A/bundle?role=worker-1&thread_id=thread2",
                            headers={"Authorization": f"Bearer {role_token}"}))
        assert wrong_thread.value.code == 400
        for path in ("/api/status", "/api/graph"):
            with pytest.raises(HTTPError) as denied:
                urlopen(Request(base + path, headers={"Authorization": f"Bearer {role_token}"}))
            assert denied.value.code == 401
        request = Request(base + "/api/participant/report", data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": f"Bearer {role_token}", "Content-Type": "application/json"})
        posted = json.loads(urlopen(request).read())
        assert posted["action"] == "REPORTED"
        retried = json.loads(urlopen(request).read())
        assert retried["action"] == "REPLAY"
        forged = {**body, "event_id": "forged", "claim_id": "global:other"}
        bad_request = Request(base + "/api/participant/report", data=json.dumps(forged).encode(), method="POST",
                              headers={"Authorization": f"Bearer {role_token}", "Content-Type": "application/json"})
        with pytest.raises(HTTPError) as rejected:
            urlopen(bad_request)
        assert rejected.value.code == 400
        unauthenticated = Request(base + "/api/participant/report", data=json.dumps(body).encode(), method="POST")
        with pytest.raises(HTTPError) as rejected:
            urlopen(unauthenticated)
        assert rejected.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_declared_local_reference_route_is_bound_authenticated_and_plain_text(h):
    content = "verified local evidence\n"
    (h.root / "evidence.txt").write_text(content)
    add(h, "A", references=[reference(h.root, "proof", "evidence", "evidence.txt")])
    role_token = "local-reference-participant-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(str(h.db), h.binding["project_id"], h.binding["root"],
                                                             "owner-token-that-is-long-enough", {"worker-1": role_token}))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}/api/tasks/A/references/A/proof"
    headers = {"Authorization": f"Bearer {role_token}"}
    try:
        response = urlopen(Request(base, headers=headers))
        assert response.read().decode() == content
        assert response.headers["Content-Type"].startswith("text/plain")
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(base.replace("/proof", "/undeclared"), headers=headers))
        assert denied.value.code == 400
        (h.root / "evidence.txt").write_text("changed after declaration")
        with pytest.raises(HTTPError) as changed:
            urlopen(Request(base, headers=headers))
        assert changed.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_task_bundle_artifact_drilldown_has_authenticated_download(h):
    h.add()
    h.result(h.issue())
    bundle = h.call("bundle", task_id="A")
    artifact = next(ref for ref in bundle["references"] if ref["path"] == "A-output.json")
    assert artifact["kind"] == "artifact" and artifact["status"] == "current"
    token = "artifact-download-participant-token"
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(str(h.db), h.binding["project_id"], h.binding["root"],
                                                             "owner-artifact-token-long-enough", {"worker-1": token}))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/tasks/A/references/A/{artifact['id']}"
    try:
        response = urlopen(Request(url, headers={"Authorization": f"Bearer {token}"}))
        assert json.loads(response.read()) == {"actual": "output"}
        assert response.headers["Content-Type"].startswith("text/plain")
        assert response.headers["Content-Disposition"].startswith("attachment;")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_registered_project_role_can_only_post_project_observations(h):
    original_workers = dict(h.workers)
    policy = {"schema_version": 1, "project_id": h.binding["project_id"], "root": str(h.root),
              "worker_registrations": [], "legacy_leaseless_beads": [],
              "project_report_registrations": [{"role": "product_owner", "thread_id": "po-thread"}]}
    role_token = "p" * 32
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(str(h.db), h.binding["project_id"], h.binding["root"], "o" * 32,
                                                            {"product_owner": role_token}, policy))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    body = {"event_id": "po-report", "work_item": "https://linear.example/issue/CPI-415",
            "kind": "progress", "occurred_at": datetime.now(timezone.utc).isoformat(),
            "state": "reviewed", "detail": "Accepted scope is reflected", "source": "linear:CPI-415",
            "evidence": ["evidence:scope"]}
    try:
        request = Request(base + "/api/participant/project-report", data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": f"Bearer {role_token}", "Content-Type": "application/json"})
        posted = json.loads(urlopen(request).read())
        assert posted["action"] == "PROJECT_REPORTED"
        rows = h.call("summary")["project_reports"]
        assert rows[0]["reporter"] == "product_owner" and rows[0]["reporter_thread_id"] == "po-thread"
        assert h.workers == original_workers
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(base + "/api/tasks/A/bundle?role=product_owner",
                           headers={"Authorization": f"Bearer {role_token}"}))
        assert denied.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_participant_api_writes_only_with_post(h):
    h.add()
    dispatch = h.issue()
    role_token = "r" * 32
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(str(h.db), h.binding["project_id"], h.binding["root"], "o" * 32,
                                                            {"worker-1": role_token}))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/participant/report"
    data = json.dumps({"event_id": "method-boundary", "task_id": "A", "claim_id": "global:bead-1",
                       "dispatch_id": dispatch, "source_digest": "source-1", "kind": "ack",
                       "occurred_at": datetime.now(timezone.utc).isoformat(), "state": "acknowledged",
                       "detail": "Worker acknowledgement", "source": "source:test", "evidence": ["evidence:test"]}).encode()
    try:
        for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
            request = Request(url, data=data, method=method, headers={"Authorization": f"Bearer {role_token}"})
            with pytest.raises(HTTPError) as rejected:
                urlopen(request)
            assert rejected.value.code == 405
            assert rejected.value.headers["Allow"] == "POST"
        status_post = Request(f"http://127.0.0.1:{server.server_port}/api/status", data=b"{}", method="POST",
                              headers={"Authorization": "Bearer " + "o" * 32})
        with pytest.raises(HTTPError) as rejected:
            urlopen(status_post)
        assert rejected.value.code == 405 and rejected.value.headers["Allow"] == "GET"
        assert h.call("summary")["bounds"]["total_reports"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_owner_bearer_cannot_be_reused_as_participant_credential(h):
    owner = "shared-owner-participant-token-123456"
    with pytest.raises(InvalidEvent, match="tokens must be distinct"):
        serve(str(h.db), h.binding["project_id"], h.binding["root"], "127.0.0.1", 0,
              token=owner, participant_tokens={"worker-1": owner})


def test_non_ascii_authorization_and_configured_tokens_are_rejected_safely(h):
    owner = "safe-owner-token-that-is-long-enough"
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(str(h.db), h.binding["project_id"], h.binding["root"], owner))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as rejected:
            urlopen(Request(f"http://127.0.0.1:{server.server_port}/api/status",
                            headers={"Authorization": "Bearer café"}))
        assert rejected.value.code == 401
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    with pytest.raises(InvalidEvent, match="participant tokens require"):
        serve(str(h.db), h.binding["project_id"], h.binding["root"], "127.0.0.1", 0,
              token=owner, participant_tokens={"worker-1": "participant-token-café-long-enough"})


def test_browser_renders_project_report_source_and_evidence_links():
    page = Path(runtime.__file__).with_name("status.html").read_text()
    assert "function appendLinks(card,refs,taskId=null)" in page
    assert "appendLinks(card,[r.source,...(r.evidence||[])],r.task_id)" in page
    assert "const projectSection=document.createElement('section')" in page
    assert "Download declared local evidence" in page and "URL.createObjectURL(blob)" in page
    assert "Local evidence not linked (no declared task reference)" in page
    append_links = page.split("function appendLinks", 1)[1].split("\nfunction drawGraph", 1)[0]
    assert r"if(/^https?:\/\//i.test(ref))u=new URL(ref)" in append_links
    assert "appendLinks(card,[ref.path],taskId)" in page
    assert "setAttribute('role','img')" in page and "Dependency graph with ${nodes.length} nodes" in page


def test_browser_local_relative_path_uses_declared_download_control_not_panel_link():
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("Node.js is unavailable for the browser-helper interaction check")
    page = Path(runtime.__file__).with_name("status.html").read_text()
    helper = "function appendLinks" + page.split("function appendLinks", 1)[1].split("\nfunction drawGraph", 1)[0]
    script = helper + """
const calls=[];
const node=(tag,text)=>({tag,textContent:text,click(){this.clicked=true},replaceWith(next){const index=this.parent.children.indexOf(this);next.parent=this.parent;this.parent.children.splice(index,1,next)}});
const api=async path=>{calls.push({type:'bundle',path});return {references:[{path:'work/evidence.txt',task_id:'task-1',id:'ref-1'}]}};
const token={value:'participant-token'};
const fetch=async(path,options)=>{calls.push({type:'fetch',path,authorization:options.headers.Authorization});return {ok:true,blob:async()=>new Blob(['verified bytes'])}};
const setTimeout=()=>{};
const local={children:[],append(...items){for(const item of items)item.parent=this;this.children.push(...items)}};
const external={children:[],append(...items){for(const item of items)item.parent=this;this.children.push(...items)}};
appendLinks(local,['work/evidence.txt'],'task-1');
appendLinks(external,['https://example.test/evidence'],null);
(async()=>{const button=local.children.find(x=>x.tag==='button');await button.onclick();const downloaded=local.children.find(x=>x.tag==='a');process.stdout.write(JSON.stringify({local:local.children.filter(x=>x.tag!=='br').map(x=>({tag:x.tag,text:x.textContent,href:x.href||null,download:x.download||null,clicked:x.clicked||false})),external:external.children.filter(x=>x.tag!=='br').map(x=>({tag:x.tag,text:x.textContent,href:x.href||null})),calls}))})()
"""
    result = subprocess.run([node_bin, "-e", script], check=True, capture_output=True, text=True)
    observed = json.loads(result.stdout)
    assert len(observed["local"]) == 1
    assert observed["local"][0]["tag"] == "a" and observed["local"][0]["download"] == "evidence.txt"
    assert observed["local"][0]["clicked"] is True
    assert observed["local"][0]["href"].startswith("blob:")
    assert "127.0.0.1" not in observed["local"][0]["href"]
    assert observed["external"] == [{"tag": "a", "text": "https://example.test/evidence", "href": "https://example.test/evidence"}]
    assert observed["calls"] == [{"type": "bundle", "path": "/api/tasks/task-1/bundle"},
                                 {"type": "fetch", "path": "/api/tasks/task-1/references/task-1/ref-1",
                                  "authorization": "Bearer participant-token"}]
