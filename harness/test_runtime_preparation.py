import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from candidate_identity import identity
from prepare_runtime import prepare
import test_linear_intake


class RuntimePreparationTests(unittest.TestCase):
    def setUp(self):
        # These tests isolate materialization; test_post_builder exercises the real snapshot boundary.
        checked_view = patch("prepare_runtime.check_view")
        checked_view.start()
        self.addCleanup(checked_view.stop)
        self.fixture = test_linear_intake.IntakeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.temp.cleanup)
        self.root = self.fixture.root
        (self.root / "harness").mkdir()
        here = Path(__file__).resolve().parent
        for name in ("linear_intake.py", "candidate_identity.py"):
            shutil.copyfile(here / name, self.root / "harness" / name)
        # Parser/preparation fixture only; never a native builder result.
        (self.root / "harness/acceptance.py").write_text("# explicit unit-test fixture\n")
        self.release = self.root / "release.json"
        self.summary = self.root / "summary.json"
        self.release.write_text(json.dumps(self.fixture.release))
        self.summary.write_text(json.dumps(self.fixture.summary))
        self.output = self.root / "controls"

    def test_real_cli_mutation_is_detectable_and_original_unchanged(self):
        original = identity(self.root)
        result = prepare(self.root, self.release, self.summary, self.output)
        self.assertEqual(result["baseline"], original)
        self.assertEqual(identity(self.root), original)
        self.assertNotEqual(result["negative"], original)
        for target, snapshot, expected in [
                (self.root, "summary.json", 0),
                (self.root, "missing-lease.json", 3),
                (self.output / "negative", "missing-lease.json", 0)]:
            observed = subprocess.run([sys.executable, str(target / "harness/linear_intake.py"),
                str(self.output / "release.json"), str(self.output / snapshot)],
                capture_output=True, text=True, timeout=10)
            self.assertEqual(observed.returncode, expected, observed.stderr)
            self.assertEqual(json.loads(observed.stdout)["ready"], expected == 0)
        manifest = json.loads((self.output / "manifest.json").read_text())
        self.assertEqual([x["expected_verdict"] for x in manifest["cases"]], ["verified", "failed"])

    def test_missing_real_implementation_refuses_before_output(self):
        (self.root / "harness/acceptance.py").unlink()
        with self.assertRaises(ValueError):
            prepare(self.root, self.release, self.summary, self.output)
        self.assertFalse(self.output.exists())

    def test_unready_ownership_refuses_without_materializing(self):
        self.fixture.task["lease"] = False
        self.summary.write_text(json.dumps(self.fixture.summary))
        with self.assertRaises(ValueError):
            prepare(self.root, self.release, self.summary, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved(self):
        self.output.mkdir()
        sentinel = self.output / "historical.json"
        sentinel.write_text("old evidence")
        with self.assertRaises(FileExistsError):
            prepare(self.root, self.release, self.summary, self.output)
        self.assertEqual(sentinel.read_text(), "old evidence")


if __name__ == "__main__":
    unittest.main()
