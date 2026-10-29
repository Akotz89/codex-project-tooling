import copy
from test_project_policy import POLICY
import hashlib
from pathlib import Path
import tempfile
import unittest

from linear_intake import intake, read_json


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "accepted.md").write_text("accepted owner intent")
        self.release = {
            "version": 1, "accepted": True, "project_id": "sample-project",
            "root": str(self.root), "issue": "APP-42",
            "linear_ref": "https://linear.app/sample-team/issue/APP-42",
            "work_order": "Implement the accepted release scope only.",
            "checkpoint_id": "checkpoint", "task_id": "first-loop",
            "worker": "builder", "dispatch_id": "dispatch",
            "worker_thread_id": "actual-thread", "writer_paths": ["harness"],
            "source_files": [{"path": "accepted.md", "sha256": hashlib.sha256(
                (self.root / "accepted.md").read_bytes()).hexdigest()}],
        }
        self.task = {"id": "first-loop", "status": "ACKNOWLEDGED", "lease": True,
                     "worker": "builder", "dispatch_id": "dispatch", "dependencies": ["prior"],
                     "dispatch_thread_id": "actual-thread", "writer_paths": [str(self.root / "harness")],
                     "linear_ref": self.release["linear_ref"]}
        self.summary = {"action": "OK", "checkpoint_id": "checkpoint", "project": {
            "project_id": "sample-project", "root": str(self.root), "workers": {"builder": "actual-thread"}},
            "recovery_required": False, "pending_checkpoint": False,
            "uncertain_held_dispatches": 0,
            "tasks": [self.task, {"id": "prior", "status": "COMPLETE"}],
            "bounds": {"truncated_fields": [], "offset": 0,
                "next_offset": None, "total_tasks": 2}}

    def test_allocated_work_becomes_upstream_input_without_side_effects(self):
        before = copy.deepcopy((self.release, self.summary))
        result = intake(self.release, self.summary, POLICY)
        self.assertTrue(result["ready"])
        self.assertEqual(result["workflow"], POLICY["workflows"]["implementation"]["workflow"])
        self.assertEqual(result["inputs"]["work"], self.release["work_order"])
        self.assertEqual(before, (self.release, self.summary))

    def test_incomplete_and_unknown_dependencies_hold(self):
        for status in ["UNKNOWN", "FAILED", "ACKNOWLEDGED"]:
            self.summary["tasks"][1]["status"] = status
            self.assertFalse(intake(self.release, self.summary, POLICY)["ready"])
        self.summary["tasks"].pop()
        self.summary["bounds"]["total_tasks"] = 1
        self.assertFalse(intake(self.release, self.summary, POLICY)["ready"])

    def test_unacknowledged_or_wrong_owner_holds(self):
        for key, value in [("status", "PREPARED"), ("lease", False),
                           ("worker", "reviewer"), ("dispatch_id", "other")]:
            task = copy.deepcopy(self.task)
            self.task[key] = value
            self.assertFalse(intake(self.release, self.summary, POLICY)["ready"])
            self.task.clear()
            self.task.update(task)

    def test_unaccepted_wrong_project_or_checkpoint_refused(self):
        for key, value in [("accepted", False), ("project_id", "other"),
                           ("checkpoint_id", "old"), ("linear_ref", "https://example.com")]:
            release = dict(self.release, **{key: value})
            with self.assertRaises(ValueError):
                intake(release, self.summary, POLICY)

    def test_issue_and_workspace_are_bound_to_caller_policy(self):
        with self.assertRaises(ValueError):
            intake(dict(self.release, issue="OTHER-9"), self.summary, POLICY)
        with self.assertRaises(ValueError):
            intake(dict(self.release, linear_ref="https://linear.app/other-team/issue/APP-42"),
                   self.summary, POLICY)

    def test_incomplete_snapshot_refused(self):
        for key, value in [("pending_checkpoint", True),
                           ("bounds", {"truncated_fields": ["tasks.0.dependencies"]})]:
            with self.assertRaises(ValueError):
                intake(self.release, dict(self.summary, **{key: value}), POLICY)

    def test_recovery_and_uncertain_ownership_hold_admission(self):
        self.summary["recovery_required"] = True
        self.summary["uncertain_held_dispatches"] = 1
        result = intake(self.release, self.summary, POLICY)
        self.assertFalse(result["ready"])
        self.assertIn("reconciliation", result["reason"])
        self.summary["recovery_required"] = False
        with self.assertRaises(ValueError):
            intake(self.release, self.summary, POLICY)

    def test_read_only_task_with_empty_writer_scope_is_supported(self):
        self.release["writer_paths"] = []
        self.task["writer_paths"] = []
        result = intake(self.release, self.summary, POLICY)
        self.assertTrue(result["ready"])
        self.assertEqual(result["writer_paths"], [])

    def test_policy_can_require_a_writer_scope(self):
        self.release["writer_paths"] = []
        self.task["writer_paths"] = []
        with self.assertRaises(ValueError):
            intake(self.release, self.summary, {**POLICY, "require_writer_paths": True})

    def test_duplicate_task_identity_refused(self):
        self.summary["tasks"].append(dict(self.task))
        self.summary["bounds"]["total_tasks"] = 3
        with self.assertRaises(ValueError):
            intake(self.release, self.summary, POLICY)

    def test_real_linear_slug_is_preserved(self):
        ref = self.release["linear_ref"] + "/replicate-the-release-loop"
        self.release["linear_ref"] = ref
        self.task["linear_ref"] = ref
        self.assertTrue(intake(self.release, self.summary, POLICY)["ready"])

    def test_source_drift_and_escape_refused(self):
        (self.root / "accepted.md").write_text("changed")
        with self.assertRaises(ValueError):
            intake(self.release, self.summary, POLICY)
        self.release["source_files"][0]["path"] = "../../etc/passwd"
        with self.assertRaises((ValueError, FileNotFoundError)):
            intake(self.release, self.summary, POLICY)

    def test_paginated_or_malformed_count_refused(self):
        for field, value in [("offset", 1), ("next_offset", 2), ("total_tasks", 3)]:
            snapshot = copy.deepcopy(self.summary)
            snapshot["bounds"][field] = value
            with self.assertRaises(ValueError):
                intake(self.release, snapshot, POLICY)

    def test_retired_thread_cannot_use_current_role(self):
        self.summary["project"]["workers"]["builder"] = "replacement-thread"
        self.assertFalse(intake(self.release, self.summary, POLICY)["ready"])

    def test_dispatch_thread_mismatch_holds(self):
        self.task["dispatch_thread_id"] = "retired-thread"
        self.assertFalse(intake(self.release, self.summary, POLICY)["ready"])

    def test_missing_or_noncanonical_identity_refused(self):
        for key, value in [("dispatch_id", ""), ("worker_thread_id", None),
                           ("checkpoint_id", " old "), ("worker", "extra")]:
            with self.assertRaises(ValueError):
                intake(dict(self.release, **{key: value}), self.summary, POLICY)

    def test_malformed_bounds_refused(self):
        with self.assertRaises(ValueError):
            intake(self.release, dict(self.summary, bounds=[]), POLICY)

    def test_writer_scope_drift_or_root_escape_refused(self):
        for paths in [["other"], ["../escape"], ["."]]:
            with self.assertRaises(ValueError):
                intake(dict(self.release, writer_paths=paths), self.summary, POLICY)

    def test_duplicate_source_pin_refused(self):
        self.release["source_files"] *= 2
        with self.assertRaises(ValueError):
            intake(self.release, self.summary, POLICY)

    def test_bounded_json_input(self):
        path = self.root / "large.json"
        path.write_bytes(b" " * (1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            read_json(path)

    def test_duplicate_json_keys_and_nonstandard_constants_are_refused(self):
        path = self.root / "malformed.json"
        for raw in (b'{"task_id":"one","task_id":"two"}', b'{"owner":NaN}'):
            path.write_bytes(raw)
            with self.assertRaises(ValueError):
                read_json(path)


if __name__ == "__main__":
    unittest.main()
