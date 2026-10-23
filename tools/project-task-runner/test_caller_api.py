"""Focused tests for consuming generic task-runner promotion receipts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load_module("project_task_runner_for_receipt_test", ROOT / "runner.py")
caller_api = load_module("project_task_runner_caller_api_test", ROOT / "caller_api.py")


class PromotionReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(mode=0o700)
        self.promoted = self.artifacts / "runtime/source-map.json"
        self.promoted.parent.mkdir(mode=0o700)
        self.promoted_bytes = b'{"schema":"runtime-map-v1"}\n'
        self.promoted.write_bytes(self.promoted_bytes)
        self.promoted.chmod(0o600)
        self.manifest = self.artifacts / "manifest.json"
        self.manifest_bytes = json.dumps({
            "complete": True,
            "files": [{
                "path": "runtime/source-map.json",
                "bytes": len(self.promoted_bytes),
                "sha256": hashlib.sha256(self.promoted_bytes).hexdigest(),
            }],
        }, sort_keys=True, separators=(",", ":")).encode()
        self.manifest.write_bytes(self.manifest_bytes)
        self.manifest.chmod(0o600)
        self.outcome = self.root / "outcome.json"
        self._write_outcome()

    def tearDown(self):
        self.temp.cleanup()

    def _write_outcome(self, *, state="complete", scratch_cleanup="complete",
                       retained_scratch=None, manifest_path=None):
        value = {
            "state": state,
            "scratch_cleanup": scratch_cleanup,
            "retained_scratch": retained_scratch or {
                "logical_bytes": 0, "allocated_bytes": 0, "inodes": 0,
            },
            "artifacts": {"manifest": {
                "path": str(manifest_path or self.manifest),
                "sha256": hashlib.sha256(self.manifest_bytes).hexdigest(),
            }},
        }
        self.outcome.write_text(json.dumps(value), encoding="utf-8")
        self.outcome.chmod(0o600)

    def _consume(self, **overrides):
        args = {
            "outcome_path": self.outcome,
            "promoted_path": self.promoted,
            "expected_artifacts": self.artifacts,
            "expected_relative": "runtime/source-map.json",
            "private_directory": runner.private_dir,
            "read_private": runner.read_bytes,
            "digest": runner.digest,
        }
        args.update(overrides)
        return caller_api.consume_runtime_promotion(**args)

    def test_valid_receipt_returns_exact_promoted_bytes(self):
        receipt = self._consume()

        self.assertTrue(receipt["complete"])
        self.assertEqual(receipt["promoted_bytes"], self.promoted_bytes)
        self.assertEqual(receipt["promoted_sha256"], runner.digest(self.promoted_bytes))
        self.assertEqual(receipt["manifest"]["sha256"], runner.digest(self.manifest_bytes))
        self.assertEqual(receipt["scratch_cleanup"], "complete")

    def test_preterminal_consumer_returns_only_manifest_pinned_bytes(self):
        receipt = caller_api.consume_promoted_files(
            manifest_reference={"path": str(self.manifest),
                                "sha256": runner.digest(self.manifest_bytes)},
            expected_artifacts=self.artifacts,
            promoted_files={"runtime/source-map.json": self.promoted},
            private_directory=runner.private_dir, read_private=runner.read_bytes,
            digest=runner.digest)
        self.assertEqual(receipt["files"]["runtime/source-map.json"]["bytes"],
                         self.promoted_bytes)
        self.assertEqual(receipt["manifest_bytes"], self.manifest_bytes)

    def test_preterminal_consumer_refuses_unpinned_bytes(self):
        self.promoted.write_bytes(b"substituted bytes")
        self.promoted.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "promoted_file_receipt"):
            caller_api.consume_promoted_files(
                manifest_reference={"path": str(self.manifest),
                                    "sha256": runner.digest(self.manifest_bytes)},
                expected_artifacts=self.artifacts,
                promoted_files={"runtime/source-map.json": self.promoted},
                private_directory=runner.private_dir, read_private=runner.read_bytes,
                digest=runner.digest)

    def test_all_receipt_file_reads_use_the_injected_private_reader(self):
        read_paths = []
        private_read = runner.read_bytes

        def tracked_private_read(path):
            read_paths.append(Path(path))
            return private_read(path)

        receipt = self._consume(read_private=tracked_private_read)

        self.assertEqual(receipt["promoted_bytes"], self.promoted_bytes)
        self.assertEqual(read_paths, [self.outcome, self.manifest, self.promoted])

    def test_failed_terminal_receipt_is_rejected_before_manifest_read(self):
        self._write_outcome(state="failed")
        reads = []

        def tracked_read(path):
            reads.append(Path(path))
            return runner.read_bytes(path)

        with self.assertRaisesRegex(ValueError, "terminal_outcome_incomplete"):
            self._consume(read_private=tracked_read)

        self.assertEqual(reads, [self.outcome])

    def test_malformed_manifest_json_shape_fails_closed(self):
        self.manifest_bytes = b"[]"
        self.manifest.write_bytes(self.manifest_bytes)
        self._write_outcome()

        with self.assertRaisesRegex(ValueError, "promotion_manifest_incomplete"):
            self._consume()

    def test_manifest_reference_must_match_expected_path(self):
        alternate = self.root / "other-manifest.json"
        self._write_outcome(manifest_path=alternate)

        with self.assertRaisesRegex(ValueError, "promotion_manifest_path"):
            self._consume()

    def test_manifest_digest_must_match_terminal_reference(self):
        outcome = json.loads(self.outcome.read_text(encoding="utf-8"))
        outcome["artifacts"]["manifest"]["sha256"] = "0" * 64
        self.outcome.write_text(json.dumps(outcome), encoding="utf-8")
        self.outcome.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "promotion_manifest_hash"):
            self._consume()

    def test_changed_promoted_bytes_do_not_match_receipt(self):
        self.promoted.write_bytes(b"substituted bytes")
        self.promoted.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "promoted_file_receipt"):
            self._consume()

    def test_noncanonical_artifact_directory_is_rejected(self):
        alias = self.root / "artifact-alias"
        alias.symlink_to(self.artifacts, target_is_directory=True)

        with self.assertRaises(runner.Refused):
            self._consume(expected_artifacts=alias)

    def test_relative_path_traversal_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "promoted_file_path"):
            self._consume(expected_relative="../source-map.json")

    def test_pinned_loader_refuses_changed_bytes_before_execution(self):
        marker = self.root / "loader-marker"
        source = self.root / "pinned.py"
        source.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
                          encoding="utf-8")
        with self.assertRaisesRegex(caller_api.Refused, "source_pin_before_execute"):
            caller_api.load_pinned_module("must_not_run", source, "0" * 64)
        self.assertFalse(marker.exists())

    def test_nested_pinned_import_executes_explicit_bytes_without_global_patch(self):
        nested = self.root / "nested.py"
        nested_raw = b"VALUE = 'verified'\n"
        nested.write_bytes(nested_raw)
        outer_raw = ("import importlib.util\n"
                     f"spec = importlib.util.spec_from_file_location('nested_fixture', {str(nested)!r})\n"
                     "module = importlib.util.module_from_spec(spec)\n"
                     "spec.loader.exec_module(module)\n"
                     "VALUE = module.VALUE\n").encode()
        original = importlib.util.spec_from_file_location

        module = caller_api.execute_verified_source(
            "outer_fixture", self.root / "outer.py", outer_raw,
            source_bytes={nested: nested_raw}, trusted_root=self.root)

        self.assertEqual(module.VALUE, "verified")
        self.assertIs(importlib.util.spec_from_file_location, original)

    def test_nested_unpinned_import_under_trusted_root_is_refused(self):
        unpinned = self.root / "unpinned.py"
        unpinned.write_text("VALUE = 'untrusted'\n", encoding="utf-8")
        outer_raw = ("import importlib.util\n"
                     f"spec = importlib.util.spec_from_file_location('unpinned_fixture', {str(unpinned)!r})\n").encode()
        with self.assertRaisesRegex(caller_api.Refused, "unpinned_executable_source"):
            caller_api.execute_verified_source("outer_unpinned", self.root / "outer.py", outer_raw,
                                               source_bytes={}, trusted_root=self.root)

    def test_unpinned_importlib_machinery_loader_is_refused_before_side_effects(self):
        unpinned = self.root / "unpinned.py"
        marker = self.root / "unpinned-marker"
        unpinned.write_text(f"open({str(marker)!r}, 'w').write('executed')\n", encoding="utf-8")
        outer_raw = ("from importlib.machinery import SourceFileLoader\n"
                     f"SourceFileLoader('unpinned_fixture', {str(unpinned)!r}).load_module()\n").encode()
        original = importlib.util.spec_from_file_location

        with self.assertRaisesRegex(caller_api.Refused, "unpinned_importlib_submodule"):
            caller_api.execute_verified_source("outer_machinery", self.root / "outer.py", outer_raw,
                                               source_bytes={}, trusted_root=self.root)

        self.assertFalse(marker.exists())
        self.assertIs(importlib.util.spec_from_file_location, original)

    def test_importlib_attribute_cannot_reach_unpinned_machinery_loader(self):
        unpinned = self.root / "unpinned.py"
        marker = self.root / "unpinned-marker"
        unpinned.write_text(f"open({str(marker)!r}, 'w').write('executed')\n", encoding="utf-8")
        outer_raw = ("import importlib\n"
                     f"importlib.machinery.SourceFileLoader('unpinned_fixture', {str(unpinned)!r}).load_module()\n").encode()

        with self.assertRaisesRegex(caller_api.Refused, "unpinned_importlib_submodule"):
            caller_api.execute_verified_source("outer_machinery_attribute", self.root / "outer.py",
                                               outer_raw, source_bytes={}, trusted_root=self.root)

        self.assertFalse(marker.exists())

    def test_sys_module_registry_cannot_reach_unpinned_machinery_loader(self):
        unpinned = self.root / "unpinned.py"
        marker = self.root / "unpinned-marker"
        unpinned.write_text(f"open({str(marker)!r}, 'w').write('executed')\n", encoding="utf-8")
        outer_raw = ("import sys\n"
                     f"sys.modules['importlib.machinery'].SourceFileLoader('unpinned_fixture', {str(unpinned)!r}).load_module()\n").encode()
        original_modules = sys.modules
        original_machinery = sys.modules["importlib.machinery"]

        with self.assertRaisesRegex(caller_api.Refused, "unpinned_sys_module_registry"):
            caller_api.execute_verified_source("outer_sys_registry", self.root / "outer.py",
                                               outer_raw, source_bytes={}, trusted_root=self.root)

        self.assertFalse(marker.exists())
        self.assertIs(sys.modules, original_modules)
        self.assertIs(sys.modules["importlib.machinery"], original_machinery)


if __name__ == "__main__":
    unittest.main()
