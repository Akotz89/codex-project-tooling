import sys
from pathlib import Path

import pytest

from project_coordinator import cli


def test_provider_load_is_serve_only_and_root_confined(monkeypatch, tmp_path):
    root = tmp_path / "project"; root.mkdir()
    provider = root / "provider.py"
    provider.write_text("class P:\n def read_policy(self): pass\n def support(self): pass\n def compare_and_swap(self, revision, selection): pass\ndef create_provider(project_root):\n return P()\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text(provider.read_text(), encoding="utf-8")
    called = {}
    monkeypatch.setattr(sys, "argv", ["project-coordinator", "--db", "unused", "serve", "--project-id", "P", "--root", str(root), "--routing-policy-provider", str(provider)])
    monkeypatch.setattr("project_coordinator.http_api.serve", lambda *a, **kw: called.update(kw) or 0)
    assert cli.main() == 0 and called["routing_provider"] is not None
    monkeypatch.setattr(sys, "argv", ["project-coordinator", "--db", "unused", "status", "--routing-policy-provider", str(provider)])
    assert cli.main() == 2
    monkeypatch.setattr(sys, "argv", ["project-coordinator", "--db", "unused", "serve", "--project-id", "P", "--root", str(root), "--routing-policy-provider", str(outside)])
    assert cli.main() == 2
