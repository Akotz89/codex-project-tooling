"""Committed candidate preparation and runtime binding regressions."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/factory"))
from runtime_host import RuntimeHost, request


APP = r'''from http.server import BaseHTTPRequestHandler, HTTPServer
import os
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200); self.end_headers()
        value = os.environ['FACTORY_RUNTIME_CANDIDATE'] if self.path == '/identity' else 'VALUE'
        self.wfile.write(value.encode())
HTTPServer(('127.0.0.1', int(os.environ['FACTORY_RUNTIME_PORT'])), Handler).serve_forever()
'''


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="resource test ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "delivery"
        (self.repo / "factory").mkdir(parents=True)
        for name in ("runtime_resource.py", "runtime_host.py", "runtime_process.py"):
            shutil.copy2(ROOT / f"template/factory/{name}", self.repo / f"factory/{name}")
        (self.repo / "app.py").write_text(APP.replace("VALUE", "first"), encoding="utf-8")
        self.git("init")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Runtime Test")
        self.git("add", "app.py", "factory/runtime_resource.py", "factory/runtime_host.py",
                 "factory/runtime_process.py")
        self.git("commit", "-m", "first candidate")
        self.resource = self.base / "candidate"

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True,
                              text=True).stdout.strip()

    def prepare(self, revision, destination=None):
        return subprocess.run([sys.executable, str(self.repo / "factory/runtime_resource.py"),
                               "prepare", "--destination", str(destination or self.resource),
                               "--expected-revision", revision], cwd=self.repo,
                              capture_output=True, text=True, timeout=30)

    def config(self):
        path = self.base / "runtime.json"
        path.write_text(json.dumps({
            "version": 1,
            "roots": {"candidate": {"path": str(self.resource),
                                      "binding": ".factory-resource.json"}},
            "include": ["app.py"], "shape": "http",
            "command": [sys.executable, "app.py"], "timeout_s": 2,
            "health_path": "/health", "identity_path": "/identity",
        }))
        return path

    def start(self, connection, slot):
        connection_file = self.base / "connection.json"
        connection_file.write_text(json.dumps(connection))
        result = subprocess.run([sys.executable, str(self.repo / "factory/runtime_resource.py"),
                                 "start", "--slot", slot, "--root", "candidate",
                                 "--connection-file", str(connection_file)], cwd=self.repo,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_changed_committed_revision_is_materialized_and_runs(self):
        first = self.git("rev-parse", "HEAD")
        with RuntimeHost(self.config()) as host:
            self.assertEqual(self.prepare(first).returncode, 0)
            first_item = self.start(host.connection, "first")
            with urlopen(first_item["target"] + "/health", timeout=2) as response:
                self.assertEqual(response.read().decode(), "first")
            self.assertEqual(first_item["source_revision"], first)
            self.assertNotEqual(first_item["input_digest"], first_item["resource_digest"])

            (self.repo / "app.py").write_text(APP.replace("VALUE", "second"), encoding="utf-8")
            self.git("add", "app.py")
            self.git("commit", "-m", "second candidate")
            second = self.git("rev-parse", "HEAD")
            self.assertEqual(self.prepare(second).returncode, 0)
            with self.assertRaises(Exception):
                request("start", {"slot": "stale", "root": "candidate",
                                  "expected_revision": first}, host.connection)
            item = self.start(host.connection, "second")
            with urlopen(item["target"] + "/health", timeout=2) as response:
                self.assertEqual(response.read().decode(), "second")
            self.assertNotEqual(first, item["source_revision"])
            self.assertNotEqual(first_item["input_digest"], item["input_digest"])
            self.assertNotEqual(first_item["resource_digest"], item["resource_digest"])

    def test_stale_checkout_and_unowned_destination_are_refused(self):
        revision = self.git("rev-parse", "HEAD")
        self.assertNotEqual(self.prepare("0" * 40).returncode, 0)
        unowned = self.base / "unowned"
        unowned.mkdir()
        sentinel = unowned / "keep.txt"
        sentinel.write_text("keep")
        self.assertNotEqual(self.prepare(revision, unowned).returncode, 0)
        self.assertEqual(sentinel.read_text(), "keep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
