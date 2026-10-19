"""Shared binding controls use local Git roots and no provider/network."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from install import configure
import consumer

class ProjectBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="binding-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.a = self.project("a", "project-a")
        self.b = self.project("b", "project-b")
        self.guard = patch.dict(consumer.MANIFEST, {"project_binding_required": True})
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def project(self, name, address):
        root = self.base / name
        root.mkdir()
        consumer.checked(["git", "init", "-q"], root)
        (root / "_bmad/custom").mkdir(parents=True)
        (root / "_bmad/custom/ticketing-store-config.toml").write_text('[tickets]\nstore="linear"\nproject="' + address + '"\n')
        (root / ".factory").mkdir()
        (root / ".factory/project-context.md").write_text("Context for " + name)
        return root

    def test_distinct_roots_keep_local_address_and_context(self):
        for root in (self.a, self.b):
            configure(root, {"source": "exact-source", "revision": "a" * 40})
        a, b = (consumer.read_settings(root)["project_binding"] for root in (self.a, self.b))
        self.assertNotEqual(a["root"], b["root"])
        self.assertNotEqual(a["project"], b["project"])
        self.assertNotEqual(a["context_sha256"], b["context_sha256"])

    def test_copied_binding_refuses_cross_project_without_rewriting(self):
        configure(self.a, {"source": "exact-source", "revision": "a" * 40})
        wrong = (self.a / consumer.SETTINGS).read_bytes()
        (self.b / consumer.SETTINGS).write_bytes(wrong)
        with self.assertRaisesRegex(ValueError, "binding drift"):
            consumer.read_settings(self.b)
        self.assertEqual((self.b / consumer.SETTINGS).read_bytes(), wrong)

    def test_address_and_context_drift_refuse(self):
        for relative in ("_bmad/custom/ticketing-store-config.toml", ".factory/project-context.md"):
            with self.subTest(path=relative):
                configure(self.a, {"source": "exact-source", "revision": "a" * 40})
                path = self.a / relative
                old = path.read_text()
                path.write_text(old.replace("project-a", "wrong-project") if relative.endswith("toml") else "changed context")
                with self.assertRaisesRegex(ValueError, "binding drift"):
                    consumer.read_settings(self.a)
                path.write_text(old)

    def test_missing_settings_and_missing_context_refuse(self):
        with self.assertRaisesRegex(ValueError, "Integration pin required"):
            consumer.read_settings(self.a)
        (self.a / ".factory/project-context.md").unlink()
        with self.assertRaisesRegex(ValueError, "context.md is required"):
            configure(self.a, {})

    def test_wrong_native_revision_refuses_before_source_access(self):
        with self.assertRaisesRegex(ValueError, "exact immutable native source pin"):
            consumer.verify_source({"revision": "b" * 40, "source": "/not-accessed"})

if __name__ == "__main__":
    unittest.main()
