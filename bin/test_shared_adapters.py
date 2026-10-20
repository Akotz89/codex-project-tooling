"""Installed compatibility boundaries against disposable, independently bound projects."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


HOME = Path(__file__).resolve().parent.parent


class InstalledAdapterTests(unittest.TestCase):
    def setUp(self):
        scratch = HOME / "work/cpi410-correction-1"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix="installed adapters ", dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = self.base / "shared factory source"
        for directory in ("runtime", "template"):
            shutil.copytree(HOME / directory, self.source / directory,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "work"))
        (self.source / "bin").mkdir()
        for name in ("install.py", "audit.py"):
            shutil.copy2(HOME / "bin" / name, self.source / "bin" / name)
        self.initialize(self.source)
        self.projects = []
        for name in ("first project", "second project"):
            root = self.base / name
            root.mkdir()
            (root / "app.txt").write_text(name + "\n", encoding="utf-8")
            self.initialize(root)
            store = root / "_bmad/custom/ticketing-store-config.toml"
            store.parent.mkdir(parents=True)
            store.write_text(f'[tickets]\nstore="linear"\nproject="{name}"\n', encoding="utf-8")
            (root / ".factory").mkdir()
            (root / ".factory/project-context.md").write_text(name + " context\n", encoding="utf-8")
            result = self.python(self.source, "import install; from pathlib import Path; "
                                 "install.sync(Path(__import__('sys').argv[1]))", str(root),
                                 extra_path=self.source / "bin")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.projects.append(root)

    def command(self, cwd, *args, extra_path=None):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("CPI404_USAGE_LEDGER", None)
        if extra_path:
            env["PYTHONPATH"] = str(extra_path)
        return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=30)

    def python(self, cwd, code, *args, extra_path=None):
        return self.command(cwd, sys.executable, "-B", "-c", code, *args, extra_path=extra_path)

    def initialize(self, root):
        for args in (("init", "-q"), ("config", "user.name", "Fixture"),
                     ("config", "user.email", "fixture@example.invalid"),
                     ("add", "."), ("commit", "-qm", "fixture")):
            result = self.command(root, "git", *args)
            self.assertEqual(result.returncode, 0, result.stderr)

    def config(self, root, value):
        (root / "harness/harness.config.json").write_text(json.dumps(value), encoding="utf-8")

    def ci(self, root, *args):
        return self.command(root, sys.executable, "-B", "harness/ci.py", *args)

    def snapshot(self):
        return {p.relative_to(self.source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.source.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.source).parts}

    def test_projects_keep_their_own_config_cwd_and_data_without_source_writes(self):
        before = self.snapshot()
        for number, root in enumerate(self.projects, 1):
            with self.subTest(root=root.name):
                self.config(root, {"static": [sys.executable, "-c",
                    "from pathlib import Path; assert Path('app.txt').read_text().strip() == " + repr(root.name)],
                    "unit": [sys.executable, "-c", f"print('{number} passed')"],
                    "unit_count_pattern": r"(\d+) passed"})
                result = self.ci(root)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"UNIT_PASSED tests={number}", result.stdout)
                self.assertIn("CHECKS_OK mode=ordinary", result.stdout)
                result = self.python(root, "from factory import usage_collector as u; "
                    "from pathlib import Path; import json; "
                    "print(json.dumps({'root': str(u.ROOT), 'ledger': str(u.LEDGER)}))")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {"root": str(root),
                    "ledger": str(root / "work/factory-usage/usage.jsonl")})
        self.assertEqual(self.snapshot(), before)

    def test_installed_ci_reports_configuration_failed_and_zero_test_checks(self):
        root = self.projects[0]
        cases = (({}, "NO_CHECKS", "GATE_FAILED: configuration"),
                 ({"static": [sys.executable, "-c", "raise SystemExit(9)"]}, "GATE_FAILED: static", ""),
                 ({"unit": [sys.executable, "-c", "print('0 passed')"],
                   "unit_count_pattern": r"(\d+) passed"}, "UNIT_ERROR:", "GATE_FAILED: unit"),
                 ({"static": f'"{sys.executable}" -c "import no_such_factory_module"'},
                  "ModuleNotFoundError", "GATE_FAILED: static"))
        for config, marker, other in cases:
            with self.subTest(marker=marker):
                self.config(root, config)
                result = self.ci(root)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(marker, result.stdout)
                self.assertIn(other, result.stdout)
                self.assertNotIn("CHECKS_OK", result.stdout)

    def test_installed_helper_timeout_executes_real_subprocess(self):
        root = self.projects[0]
        self.config(root, {})
        result = self.python(root, "from harness import ci; import sys; "
            "rc, out = ci.run('static', [sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.05); "
            "assert rc == 124, (rc, out); assert 'TIMEOUT after 0.05s' in out; print(out)")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("TIMEOUT after 0.05s", result.stdout)

    def test_installed_usage_script_resolves_receipts_and_owned_ledger_in_each_project(self):
        before = self.snapshot()
        for root in self.projects:
            native = root / "native.json"
            native.write_text(json.dumps({"thread_id": "fixture"}))
            receipt = root / "receipt.json"
            receipt.write_text(json.dumps({"thread_id": "fixture", "native_evidence": {
                "path": "native.json", "sha256": hashlib.sha256(native.read_bytes()).hexdigest()}}))
            event = root / "event.json"
            event.write_text(json.dumps({"task_id": root.name, "receipt": {
                "path": "receipt.json", "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}}))
            result = self.command(root, sys.executable, "-B", "factory/usage_collector.py",
                                  "--event", "event.json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout), {"collected": 0})
            self.assertTrue((root / "work/factory-usage/usage.jsonl.lock").is_file())
        self.assertEqual(self.snapshot(), before)

    def test_qualified_and_bare_imports_share_patchable_module_globals(self):
        root = self.projects[0]
        result = self.python(root, """
import sys
from pathlib import Path
from unittest.mock import patch
from factory import usage_collector as qualified
sys.path.insert(0, str(Path.cwd() / 'factory'))
import usage_collector, consumer, jev_router, runtime_host
from factory import consumer as qualified_consumer, jev_router as qualified_router, runtime_host as qualified_host
assert qualified is usage_collector
assert consumer is qualified_consumer
assert jev_router is qualified_router
assert runtime_host is qualified_host
with patch.object(usage_collector, 'LEDGER', Path('owned-ledger')):
    assert qualified.LEDGER == Path('owned-ledger')
with patch.object(consumer, 'checked', return_value=str(Path.cwd())) as checked:
    assert qualified_consumer.project_root() == Path.cwd()
    checked.assert_called_once()
print('IDENTITY_OK')
""")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IDENTITY_OK", result.stdout)

    def test_installed_test_adapters_preserve_discovery_without_source_writes(self):
        before = self.snapshot()
        result = self.command(self.projects[0], sys.executable, "-B", "-m", "unittest",
                              "discover", "-s", "factory/tests", "-v")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("test_dispatch_shape.", result.stderr)
        self.assertIn("test_usage_collector.", result.stderr)
        self.assertRegex(result.stderr, r"\nRan [1-9]\d* tests in ")
        self.assertRegex(result.stderr, r"\nOK(?: \(skipped=1\))?\n$")
        if "skipped=1" in result.stderr:
            self.assertIn("Native Core wrapper belongs to its project; shared runtime does not install a coordinator",
                          result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_installed_helper_and_test_scripts_preserve_direct_execution(self):
        before = self.snapshot()
        root = self.projects[0]
        helper = self.command(root, sys.executable, "-B", "harness/appproc.py")
        self.assertEqual(helper.returncode, 0, helper.stdout + helper.stderr)
        for script in ("factory/tests/test_dispatch_shape.py", "factory/tests/test_usage_collector.py"):
            with self.subTest(script=script):
                result = self.command(root, sys.executable, "-B", script)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertRegex(result.stderr, r"\nRan [1-9]\d* tests in ")
                self.assertRegex(result.stderr, r"\nOK(?: \(skipped=1\))?\n$")
        self.assertEqual(self.snapshot(), before)

    def test_linked_worktree_keeps_local_cwd_config_and_canonical_project_binding(self):
        before = self.snapshot()
        canonical = self.projects[0]
        linked = self.base / "linked project worktree"
        created = self.command(canonical, "git", "worktree", "add", "-q", "-b", "linked-fixture", str(linked))
        self.assertEqual(created.returncode, 0, created.stdout + created.stderr)
        self.addCleanup(self.command, canonical, "git", "worktree", "remove", "--force", str(linked))
        installed = self.python(self.source, "import install; from pathlib import Path; "
                                "install.sync(Path(__import__('sys').argv[1]))", str(linked),
                                extra_path=self.source / "bin")
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        self.sentinel(canonical)
        self.config(linked, {"static": [sys.executable, "-c",
            "from pathlib import Path; assert Path.cwd() == Path(" + repr(str(linked)) + "); "
            "Path('LINKED_CHECK').touch()"]})
        checked = self.ci(linked)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("CHECKS_OK mode=ordinary", checked.stdout)
        self.assertTrue((linked / "LINKED_CHECK").is_file())
        self.assertFalse((canonical / "LINKED_CHECK").exists())
        self.assertFalse((canonical / "DISPATCHED").exists())
        binding = self.python(linked, "from factory import consumer; import json; "
            "from pathlib import Path; print(json.dumps(consumer.project_binding(Path.cwd())))")
        self.assertEqual(binding.returncode, 0, binding.stdout + binding.stderr)
        self.assertEqual(json.loads(binding.stdout)["root"], str(canonical))
        self.assertEqual(json.loads(binding.stdout)["project"], canonical.name)
        self.assertEqual(self.snapshot(), before)

    def test_installed_application_helper_uses_project_cwd_and_reports_import_failure(self):
        root = self.projects[0]
        result = self.python(root, """
import sys
from pathlib import Path
from harness import appproc
with appproc.make_driver({'driver': 'library', 'library': {'import_check':
    f'''"{sys.executable}" -c "from pathlib import Path; assert Path('app.txt').is_file()"'''}}):
    pass
try:
    with appproc.make_driver({'driver': 'library', 'library': {'import_check':
        f'"{sys.executable}" -c "import no_such_factory_module"'}}):
        raise AssertionError('missing import accepted')
except appproc.AppDidNotStart:
    print('IMPORT_FAILURE_REPORTED')
""")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IMPORT_FAILURE_REPORTED", result.stdout)

    def test_installed_runtime_worker_self_launches_and_refuses_old_revision(self):
        before = self.snapshot()
        for root in self.projects:
            result = self.python(root, """
import os
from pathlib import Path
import sys
from factory.runtime_process import ProcessTree
from factory.runtime_resource import delivery
tree = ProcessTree([sys.executable, '-c',
    "from pathlib import Path; Path('worker-result').write_text(Path('app.txt').read_text())"],
    Path.cwd(), dict(os.environ))
try:
    assert tree.proc.wait(timeout=10) == 0
finally:
    tree.close()
assert tree.proc.poll() is not None
assert Path('worker-result').read_text() == Path('app.txt').read_text()
try:
    delivery('0' * 40)
    raise AssertionError('old revision accepted')
except ValueError as error:
    assert 'expected revision' in str(error), error
print('WORKER_AND_REVISION_OK')
""")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("WORKER_AND_REVISION_OK", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_installed_mutations_read_local_data_and_apply_only_to_candidate(self):
        before = self.snapshot()
        for root in self.projects:
            defect = {"id": root.name, "file": "app.txt", "find": root.name, "replace": "changed"}
            (root / "harness/mutations/defects.json").write_text(json.dumps({"defects": [defect]}))
            listed = self.command(root, sys.executable, "-B", "harness/mutations/run.py", "list")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout), [defect])
            applied = self.command(root, sys.executable, "-B", "harness/mutations/run.py", "apply",
                                   root.name, "--candidate", str(root))
            self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
            self.assertTrue(json.loads(applied.stdout)["injected"])
            self.assertEqual((root / "app.txt").read_text(), "changed\n")
        self.assertEqual(self.snapshot(), before)

    def test_audit_checks_shared_mutation_body_for_execution_dependencies(self):
        args = (sys.executable, "-B", str(self.source / "bin/audit.py"), "--repo",
                str(self.source / "template"))
        clean = self.command(self.source, *args)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        mutation = self.source / "runtime/harness/mutations/run.py"
        mutation.write_text(mutation.read_text() + "\nimport subprocess\n")
        dirty = self.command(self.source, *args)
        self.assertEqual(dirty.returncode, 1, dirty.stdout + dirty.stderr)
        self.assertIn("Execution dependency in data-only helper", dirty.stdout)

    def test_development_ci_and_mutations_use_template_data_and_cwd(self):
        template = self.source / "template"
        clean = self.command(self.source, sys.executable, "-B", "template/harness/ci.py")
        self.assertEqual(clean.returncode, 1, clean.stdout + clean.stderr)
        self.assertIn("NO_CHECKS", clean.stdout)
        self.assertEqual(clean.stderr, "")
        listed = self.command(self.source, sys.executable, "-B",
                              "template/harness/mutations/run.py", "list")
        self.assertEqual(listed.returncode, 0, listed.stdout + listed.stderr)
        self.assertEqual(json.loads(listed.stdout), [])
        (template / "app.txt").write_text("template data")
        self.config(template, {"static": [sys.executable, "-c",
            "from pathlib import Path; assert Path('app.txt').read_text() == 'template data'"]})
        checked = self.command(self.source, sys.executable, "-B", "template/harness/ci.py")
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("CHECKS_OK mode=ordinary", checked.stdout)
        defect = {"id": "development", "file": "app.txt", "find": "template", "replace": "changed"}
        (template / "harness/mutations/defects.json").write_text(json.dumps({"defects": [defect]}))
        applied = self.command(self.source, sys.executable, "-B", "template/harness/mutations/run.py",
                               "apply", "development", "--candidate", str(template))
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertTrue(json.loads(applied.stdout)["injected"])
        self.assertEqual((template / "app.txt").read_text(), "changed data")
        self.assertFalse((self.source / "app.txt").exists())

    def test_development_application_drivers_use_template_cwd_and_import_root(self):
        template = self.source / "template"
        (template / "dev_app.py").write_text("from pathlib import Path; print(Path.cwd())\n")
        (template / "dev_http.py").write_text(
            "import http.server, sys\nfrom pathlib import Path\n"
            "class Handler(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200); self.end_headers()\n"
            "        self.wfile.write(str(Path.cwd()).encode())\n"
            "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()\n")
        result = self.python(self.source, r'''
from pathlib import Path
import sys
import appproc
expected = Path.cwd() / 'template'
with appproc.make_driver({'driver': 'cli', 'cli': {
    'invoke': f'"{sys.executable}" -B dev_app.py', 'smoke_contains': str(expected)}}) as app:
    rc, out, err = app.run()
    assert rc == 0 and out.strip() == str(expected), (rc, out, err)
with appproc.make_driver({'driver': 'library', 'library': {
    'import_check': f'"{sys.executable}" -B dev_app.py'}}):
    assert sys.path[0] == str(expected), sys.path[0]
    import dev_app
    assert Path(dev_app.__file__).parent == expected
app = appproc.make_driver({'driver': 'http', 'http': {
    'start': f'"{sys.executable}" -B dev_http.py {{port}}', 'health_path': '/', 'boot_timeout_s': 5}})
try:
    app.__enter__()
    import urllib.request
    with urllib.request.urlopen(app.base, timeout=5) as response:
        assert response.read().decode() == str(expected)
    child = app.proc
finally:
    app.__exit__()
assert child.poll() is not None
print('DEVELOPMENT_DRIVERS_OK')
''', extra_path=template / "harness")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DEVELOPMENT_DRIVERS_OK", result.stdout)

    def test_development_usage_resolves_relative_evidence_and_default_ledger_under_template(self):
        template = self.source / "template"
        native = template / "native.json"
        native.write_text(json.dumps({"thread_id": "fixture"}))
        receipt = template / "receipt.json"
        receipt.write_text(json.dumps({"thread_id": "fixture", "native_evidence": {
            "path": "native.json", "sha256": hashlib.sha256(native.read_bytes()).hexdigest()}}))
        event = template / "event.json"
        event.write_text(json.dumps({"task_id": "development", "receipt": {
            "path": "receipt.json", "sha256": hashlib.sha256(receipt.read_bytes()).hexdigest()}}))
        result = self.command(template, sys.executable, "-B", "factory/usage_collector.py",
                              "--event", "event.json")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout), {"collected": 0})
        self.assertTrue((template / "work/factory-usage/usage.jsonl.lock").is_file())
        self.assertFalse((self.source / "work/factory-usage").exists())
        parent = template / "parent.json"
        parent.write_text(json.dumps({"worker": {"thread_id": "fixture"}}))
        result = self.python(self.source, "import usage_collector as u; "
            "u.verify_parent_evidence({'thread_id': 'fixture', 'parent_evidence': "
            + repr({"path": "parent.json", "sha256": hashlib.sha256(parent.read_bytes()).hexdigest()})
            + "}); print('PARENT_EVIDENCE_OK')", extra_path=template / "factory")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PARENT_EVIDENCE_OK", result.stdout)

    def test_development_resource_keeps_delivering_repository_root(self):
        result = self.python(self.source, "from runtime_resource import delivery; "
            "from pathlib import Path; root, revision = delivery(); "
            "assert root == Path.cwd(); assert len(revision) == 40; print('SOURCE_ROOT_OK')",
            extra_path=self.source / "template/factory")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("SOURCE_ROOT_OK", result.stdout)

    def test_ordinary_audit_does_not_write_shared_bytecode(self):
        before = self.snapshot()
        env = dict(os.environ)
        env.pop("PYTHONDONTWRITEBYTECODE", None)
        env.pop("PYTHONPYCACHEPREFIX", None)
        result = subprocess.run([sys.executable, str(self.source / "bin/audit.py")],
            cwd=self.source, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("CONSUMER_AUDIT errors=0", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_audit_refuses_preload_graph_that_omits_real_shared_import(self):
        args = (sys.executable, "-B", "bin/audit.py")
        clean = self.command(self.source, *args)
        self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
        loader = self.source / "runtime/adapter.py"
        body = loader.read_text()
        original = '"runtime_host": ("runtime_process", "runtime_resource"),'
        self.assertIn(original, body)
        loader.write_text(body.replace(original, '"runtime_host": ("runtime_process",),'))
        drifted = self.command(self.source, *args)
        self.assertEqual(drifted.returncode, 1, drifted.stdout + drifted.stderr)
        self.assertIn("Shared import dependencies drifted: factory/runtime_host.py", drifted.stdout)

    def sentinel(self, root):
        self.config(root, {"static": [sys.executable, "-c",
            "from pathlib import Path; Path('DISPATCHED').touch()"]})

    def refused(self, root, marker):
        result = self.ci(root)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(marker, result.stderr)
        self.assertFalse((root / "DISPATCHED").exists())

    def test_missing_or_drifted_loader_refuses_before_project_command(self):
        root = self.projects[0]
        self.sentinel(root)
        loader = self.source / "runtime/adapter.py"
        original = loader.read_bytes()
        loader.unlink()
        self.refused(root, "Missing or drifted shared factory loader")
        fallback = self.command(root, sys.executable, "-B", "harness/ci.py",
                                extra_path=HOME / "template/factory")
        self.assertNotEqual(fallback.returncode, 0, fallback.stdout + fallback.stderr)
        self.assertIn("Missing or drifted shared factory loader", fallback.stderr)
        self.assertFalse((root / "DISPATCHED").exists())
        loader.write_bytes(original + b"\n# fixture drift\n")
        self.refused(root, "Missing or drifted shared factory loader")

    def test_shared_body_or_revision_drift_refuses_before_project_command(self):
        root = self.projects[0]
        self.sentinel(root)
        body = self.source / "runtime/harness/ci.py"
        original = body.read_bytes()
        body.write_bytes(original + b"\n# fixture drift\n")
        self.refused(root, "Shared factory source binding drift")
        body.write_bytes(original)
        result = self.command(self.source, "git", "commit", "--allow-empty", "-qm", "different revision")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.refused(root, "Shared factory source binding drift")

    def test_shared_asset_drift_refuses_before_project_command(self):
        root = self.projects[0]
        self.sentinel(root)
        for name in ("pack.json", "assessment_questions.json"):
            with self.subTest(asset=name):
                (root / "DISPATCHED").unlink(missing_ok=True)
                asset = self.source / "template/factory" / name
                original = asset.read_bytes()
                try:
                    data = json.loads(original)
                    data["fixture_drift"] = "post-install valid JSON"
                    asset.write_text(json.dumps(data))
                    self.refused(root, "Shared factory source binding drift")
                finally:
                    asset.write_bytes(original)
        (root / "DISPATCHED").unlink(missing_ok=True)
        control = self.ci(root)
        self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
        self.assertTrue((root / "DISPATCHED").is_file())

    def test_symlinked_shared_body_and_installed_adapter_refuse_before_project_command(self):
        root = self.projects[0]
        self.sentinel(root)
        body = self.source / "runtime/harness/ci.py"
        saved_body = self.base / "saved-ci-body"
        body.rename(saved_body)
        body.symlink_to(saved_body)
        self.refused(root, "Shared factory source symlink refused")
        body.unlink()
        saved_body.rename(body)
        adapter = root / "harness/ci.py"
        saved_adapter = root / "harness/saved-ci-adapter"
        adapter.rename(saved_adapter)
        adapter.symlink_to(saved_adapter)
        self.refused(root, "Factory adapter path refused")

    def test_unsupported_abi_and_unknown_entry_refuse_before_project_command(self):
        root = self.projects[0]
        self.sentinel(root)
        adapter = root / "harness/ci.py"
        original = adapter.read_text()
        self.assertEqual(original.count("'abi': 1,"), 1)
        adapter.write_text(original.replace("'abi': 1,", "'abi': 999,"))
        self.refused(root, "Shared factory source binding drift")
        self.assertEqual(original.count("'harness/ci.py',"), 1)
        adapter.write_text(original.replace("'harness/ci.py',", "'harness/unknown.py',"))
        self.refused(root, "Unknown factory adapter entry")

    def test_context_tracker_and_copied_adapter_refuse_before_project_command(self):
        first, second = self.projects
        self.sentinel(first)
        self.sentinel(second)
        context = first / ".factory/project-context.md"
        original = context.read_bytes()
        context.write_bytes(original + b"changed context\n")
        self.refused(first, "Factory adapter project binding drift")
        context.write_bytes(original)
        store = first / "_bmad/custom/ticketing-store-config.toml"
        original = store.read_bytes()
        store.write_text('[tickets]\nstore="linear"\nproject="different-project"\n')
        self.refused(first, "Factory adapter project binding drift")
        store.write_bytes(original)
        shutil.copyfile(first / "harness/ci.py", second / "harness/ci.py")
        self.refused(second, "Factory adapter project binding drift")

    def test_absolute_adapter_from_other_project_refuses_before_project_command(self):
        first, second = self.projects
        self.sentinel(first)
        self.sentinel(second)
        result = self.command(second, sys.executable, "-B", str(first / "harness/ci.py"))
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Factory adapter and caller must use the same project", result.stderr)
        for root in self.projects:
            self.assertFalse((root / "DISPATCHED").exists())

    def test_refused_import_does_not_publish_unvalidated_dependency_aliases(self):
        root = self.projects[0]
        self.sentinel(root)
        context = root / ".factory/project-context.md"
        context.write_bytes(context.read_bytes() + b"changed context\n")
        result = self.python(root, """
import sys
from pathlib import Path
try:
    import factory.usage_collector
    raise AssertionError('drifted usage collector imported')
except ValueError as error:
    assert 'Factory adapter project binding drift' in str(error), error
aliases = ('consumer', 'factory.consumer', 'usage_collector', 'factory.usage_collector',
           'jev_router', 'factory.jev_router', 'runtime_host', 'factory.runtime_host')
assert not any(name in sys.modules for name in aliases), [name for name in aliases if name in sys.modules]
sys.path.insert(0, str(Path.cwd() / 'factory'))
try:
    import consumer
    raise AssertionError('cached unvalidated consumer imported')
except ValueError as error:
    assert 'Factory adapter project binding drift' in str(error), error
assert not any(name in sys.modules for name in aliases), [name for name in aliases if name in sys.modules]
print('REFUSED_IMPORT_NOT_CACHED')
""")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("REFUSED_IMPORT_NOT_CACHED", result.stdout)
        self.assertFalse((root / "DISPATCHED").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
