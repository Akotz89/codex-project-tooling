import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from linear_intake import digest
from post_builder import check_view, output_view
from prepare_runtime import prepare


class OutputCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.candidate = self.root / "candidate"
        (self.candidate / "harness").mkdir(parents=True)
        self.work = self.root / "WORK-ORDER.md"
        self.work.write_text("Explicit unit fixture; not native execution evidence.")
        for name in ("linear_intake.py", "candidate_identity.py"):
            shutil.copyfile(Path(__file__).parent / name, self.candidate / "harness" / name)
        self.git("init", "-b", "codex/test")
        self.git("add", ".")
        self.commit()
        self.base = self.git("rev-parse", "HEAD")
        self.git("branch", "main")
        self.release = {"version": 1, "accepted": True, "project_id": "P-CPI-10",
            "root": str(self.root), "issue": "CPI-402", "linear_ref": "https://linear.app/codex-azyrra/issue/CPI-402",
            "work_order": str(self.work), "work_order_sha256": digest(self.work),
            "checkpoint_id": "before", "task_id": "task", "dispatch_id": "dispatch", "worker": "core2",
            "worker_thread_id": "thread", "source_digest": "immutable-source", "scope_digest": "immutable-scope",
            "writer_paths": [str(self.candidate / "harness")], "candidate_root": str(self.candidate),
            "candidate_commit": self.base, "candidate_branch": "codex/test",
            "allowed_changes": ["harness/acceptance.py", "harness/test_acceptance.py"],
            "source_files": [{"path": "WORK-ORDER.md", "sha256": digest(self.work)}]}
        self.initial = self.root / "initial.json"
        self.initial.write_text(json.dumps(self.release))
        self.initial_bytes = self.initial.read_bytes()
        self.summary = self.root / "summary.json"
        self.summary.write_text(json.dumps({"action": "OK", "checkpoint_id": "after", "pending_checkpoint": False,
            "project": {"root": str(self.root), "project_id": "P-CPI-10", "workers": {"core2": "thread"}},
            "bounds": {"offset": 0, "next_offset": None, "total_tasks": 1, "truncated_fields": []},
            "tasks": [{"id": "task", "lease": True, "status": "ACKNOWLEDGED", "worker": "core2",
                "dispatch_id": "dispatch", "dispatch_thread_id": "thread", "dependencies": [],
                "linear_ref": self.release["linear_ref"], "writer_paths": self.release["writer_paths"]}]}))
        self.builder = self.root / "builder.json"
        self.builder.write_text(json.dumps({"fixture": "unit data, not a live native run", "id": "run",
            "workflow_name": "archon-implement", "status": "completed", "outcome": "succeeded",
            "working_path": str(self.candidate), "checkout_baseline": {"kind": "git", "commit": self.base,
                "worktree": {"status": "clean"}}, "terminal_record": {"run_id": "run", "status": "completed",
                "outcome": "succeeded", "returns": {"availability": "available", "node_id": "implement",
                    "value": {"done": True, "green": True, "red_cause": ""}}}}))
        (self.candidate / "harness/acceptance.py").write_text("# deliberately identified unit fixture\n")
        (self.candidate / "harness/test_acceptance.py").write_text("# unit fixture\n")
        self.git("add", ".")
        self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", "-C", str(self.candidate), *args], stderr=subprocess.DEVNULL).decode().strip()

    def commit(self):
        self.git("-c", "commit.gpgsign=false", "-c", "user.name=Fixture", "-c", "user.email=fixture@local.invalid",
                 "-c", "core.hooksPath=/dev/null", "commit", "-m", "fixture")

    def view(self):
        return output_view(self.initial, self.builder, self.summary, "run")

    def test_new_sources_are_pinned_without_rewriting_input_allocation(self):
        view = self.view()
        self.assertEqual(self.initial.read_bytes(), self.initial_bytes)
        self.assertEqual(view["source_digest"], "immutable-source")
        self.assertEqual(view["checkpoint_id"], "after")
        self.assertIn("candidate/harness/acceptance.py", [row["path"] for row in view["source_files"]])
        check_view(view, self.candidate)
        view_path = self.root / "output-view.json"
        view_path.write_text(json.dumps(view))
        controls = prepare(self.candidate, view_path, self.summary, self.root / "controls")
        self.assertEqual(controls["baseline"], view["post_builder"]["output_identity"])

    def test_unrelated_path_changed_is_refused(self):
        (self.candidate / "governance.md").write_text("unexpected")
        self.git("add", ".")
        self.commit()
        with self.assertRaises(ValueError): self.view()

    def test_allowed_committed_edit_gets_new_pin_and_preserves_input(self):
        first = self.view()
        (self.candidate / "harness/acceptance.py").write_text("# changed allowed unit fixture\n")
        self.git("add", ".")
        self.commit()
        second = self.view()
        self.assertNotEqual(first["post_builder"]["output_identity"], second["post_builder"]["output_identity"])
        self.assertEqual(self.initial.read_bytes(), self.initial_bytes)

    def test_advanced_main_is_not_a_valid_builder_output(self):
        self.git("branch", "-f", "main", "HEAD")
        with self.assertRaises(ValueError): self.view()

    def test_changed_work_order_refused(self):
        self.work.write_text("changed")
        with self.assertRaises(ValueError): self.view()

    def test_stale_head_and_missing_output_pins_refused(self):
        view = self.view()
        missing = copy.deepcopy(view)
        missing["source_files"] = missing["source_files"][:1]
        with self.assertRaises(ValueError): check_view(missing, self.candidate)
        (self.candidate / "harness/test_acceptance.py").write_text("changed committed output")
        self.git("add", ".")
        self.commit()
        with self.assertRaises(ValueError): check_view(view, self.candidate)

    def test_ownership_changed_refused(self):
        summary = json.loads(self.summary.read_text())
        summary["tasks"][0]["dispatch_thread_id"] = "different"
        self.summary.write_text(json.dumps(summary))
        with self.assertRaises(ValueError): self.view()

    def test_wrong_terminal_builder_refused(self):
        with self.assertRaises(ValueError): output_view(self.initial, self.builder, self.summary, "other")


if __name__ == "__main__":
    unittest.main()
