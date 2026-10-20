"""Local boundary tests. Fake CLI records argv and never launches a provider."""
from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from install import (POLICY_POINTER, HOME, TEMPLATE, configure, install_policy_pointer,
                     install_source, sync)
import consumer


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return sys.modules[name]


FAKE_CLI = r'''
import json, os, re, sys
from pathlib import Path
args = sys.argv[1:]
source = Path(__file__).resolve().parents[3]
with open(os.environ["FACTORY_TEST_TRACE"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
if "--help" in args:
    print("workflow " + args[1] + " --workflow-source --input --adopt --detach --json --events --comment --reason")
    raise SystemExit(0)
if args[:2] == ["workflow", "list"]:
    names = [p.stem for p in (source / ".archon/workflows/sdlc").rglob("*.yaml")]
    print(json.dumps({"workflows": [{"name": n} for n in names], "errors": []}, indent=2))
elif args[:2] == ["validate", "workflows"]:
    name = args[2]
    files = list((source / ".archon/workflows/sdlc").rglob(name + ".yaml"))
    valid = bool(files) and (files[0].parent / "commands/check.md").is_file()
    print(json.dumps({"results": [{"workflowName": name, "valid": valid}], "summary": {"errors": 0 if valid else 1}}))
    raise SystemExit(0 if valid else 1)
elif args[:2] == ["trigger", "whoami"]:
    print('{"level":30,"msg":"db.sqlite_schema_initialized"}')
    print(json.dumps({"runAsUserId": "user-123", "cliIdentity": "fixture"}, indent=2))
elif args[:1] == ["trigger"] or args[:2] == ["workflow", "wake"]:
    print("{}")
else:
    if os.environ.get("FACTORY_RUNTIME_URL"):
        from urllib.request import Request, urlopen
        req = Request(os.environ["FACTORY_RUNTIME_URL"] + "/start",
                      data=json.dumps({"slot":"baseline", "root":"candidate"}).encode(),
                      headers={"Authorization":"Bearer " + os.environ["FACTORY_RUNTIME_TOKEN"]})
        with urlopen(req) as response:
            Path(os.environ["FACTORY_TEST_HOST_RESULT"]).write_bytes(response.read())
    status = os.environ.get("FACTORY_TEST_STATUS", "paused")
    print(json.dumps({"id": "run-123", "status": status, "source": str(source),
                      "working_path": "candidate checkout", "argv": args}, indent=2))
    raise SystemExit(int(os.environ.get("FACTORY_TEST_EXIT", "0")))
'''


WORK = HOME / "work/cpi410/tests"
WORK.mkdir(parents=True, exist_ok=True)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="factory consumer ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = self.base / "application with spaces"
        self.app.mkdir()
        self.git(self.app, "init", "-q")
        self.git(self.app, "config", "user.name", "Fixture")
        self.git(self.app, "config", "user.email", "fixture@example.invalid")
        (self.app / "app.txt").write_text("original app\n")
        self.git(self.app, "add", ".")
        self.git(self.app, "commit", "-qm", "fixture")
        self.git(self.app, "remote", "add", "origin", "https://github.com/owner/repo.git")
        self.origin = self.base / "complete producer source"
        self.origin.mkdir()
        self.git(self.origin, "init", "-q")
        self.git(self.origin, "config", "user.name", "Fixture")
        self.git(self.origin, "config", "user.email", "fixture@example.invalid")
        for rel, body in {consumer.ENTRY: FAKE_CLI, "package.json": "{}", "bun.lock": "{}",
                          ".gitignore": "node_modules/\n.archon/ignored.yaml\n"}.items():
            path = self.origin / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        for name in [*consumer.MANIFEST["entries"], "archon-future-queue"]:
            folder = self.origin / consumer.MANIFEST["source_directory"] / name
            (folder / "commands").mkdir(parents=True)
            (folder / (name + ".yaml")).write_text(f"name: {name}\nnodes:\n  - id: check\n    command: check\n")
            (folder / "commands/check.md").write_text("fixture only\n")
        self.git(self.origin, "add", ".")
        self.git(self.origin, "commit", "-qm", "producer fixture")
        self.revision = self.git(self.origin, "rev-parse", "HEAD").strip()
        self.source = self.base / self.revision
        self.git(self.base, "clone", "-q", "--no-hardlinks", str(self.origin), str(self.source))
        (self.source / "node_modules").mkdir()
        self.settings = {"source": str(self.source), "revision": self.revision, "bun": sys.executable}
        # Bind the real consumer guards to this disposable fake source, without a
        # production bypass or mutation of the pinned source under test.
        self.pin_patch = patch.dict(consumer.MANIFEST, {"integration_revision_required": self.revision})
        self.pin_patch.start()
        self.addCleanup(self.pin_patch.stop)
        (self.app / "_bmad/custom").mkdir(parents=True)
        (self.app / "_bmad/custom/ticketing-store-config.toml").write_text('[tickets]\nstore="linear"\nproject="fixture-project"\n')
        (self.app / ".factory").mkdir()
        (self.app / ".factory/project-context.md").write_text("Fixture context\n")
        self.settings["project_binding"] = consumer.project_binding(self.app)
        self.fixture_factory = self.base / "factory-installation"
        shutil.copytree(HOME / "bin", self.fixture_factory / "bin", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(TEMPLATE, self.fixture_factory / "template", ignore=shutil.ignore_patterns("__pycache__", "work"))
        (self.fixture_factory / "template/factory/pack.json").write_text(json.dumps(consumer.MANIFEST))
        shutil.copytree(HOME / "runtime", self.fixture_factory / "runtime", ignore=shutil.ignore_patterns("__pycache__"))
        # This disposable copied runtime uses an offline protocol fixture, never a paid API.
        fixture_router = self.fixture_factory / "runtime/factory/jev_router.py"
        with fixture_router.open("a") as stream:
            stream.write('\ndef http_choice(payload, credential, deadline):\n'
                         '    keys = list(payload["questions"]["worker_pair"]["criteria"])\n'
                         '    choice = next(k for k in keys if k.startswith("pair_"))\n'
                         '    return {"answers": {"worker_pair": {"type": "choice", "choice": choice, "confidence": .7,\n'
                         '        "probabilities": {k: .8 if k == choice else .2 / (len(keys)-1) for k in keys}}}}\n')
        self.git(self.fixture_factory, "init", "-q")
        self.git(self.fixture_factory, "config", "user.name", "Fixture")
        self.git(self.fixture_factory, "config", "user.email", "fixture@example.invalid")
        self.git(self.fixture_factory, "add", "bin", "template", "runtime")
        self.git(self.fixture_factory, "commit", "-qm", "factory fixture")
        self.trace = self.base / "argv.jsonl"
        self.env = patch.dict(os.environ, {"FACTORY_TEST_TRACE": str(self.trace),
                         "FACTORY_AGENT_CMD": "provider-must-never-run", "FACTORY_AUTONOMY": "4",
                         "TYPESAFE_API_KEY": "offline-consumer-fixture"})
        self.env.start()
        self.addCleanup(self.env.stop)
        configure(self.app, self.settings)

    def git(self, cwd, *args):
        result = consumer.execute(["git", *args], cwd)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def command(self, *args, env=None):
        return subprocess.run([sys.executable, str(self.fixture_factory / "bin/factory.py"), *args], cwd=self.app,
                              env={**os.environ, **(env or {})}, capture_output=True, text=True,
                              encoding="utf-8", timeout=60)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []


class ConsumerTests(Fixture):
    def test_real_worker_command_refuses_source_drift_before_human_override(self):
        connection = "46edcd0d-a394-4516-a68e-214dc61ef7a0"
        captured = time.time()
        request = {"schema": "CPI414PROVIDERWORKERV1", "caller_id": "archon:fixture",
            "task_id": "worker:parser", "project_binding": consumer.project_binding(self.app),
            "task": "Bounded parser repair with regression assertions.", "remaining_ms": 2000,
            "ordinary": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium"},
            "catalog": {"same_account": True, "identity": connection, "fetched_at": captured,
                        "models": [{"model": "gpt-6.1-sol", "supportedReasoningEfforts": ["medium"]}]},
            "catalog_provenance": {"source": "codex_app_server_same_connection",
                                   "connection_id": connection, "captured_at": captured},
            "explicit_choice": {"model": "gpt-6.1-sol", "effort": "medium",
                "source": "direct_human_instruction", "instruction_quote": "Use Sol6.1 medium.",
                "instruction_reference": {"kind": "chat_turn", "chat_id": "human:fixture", "turn_id": "human:1"}}}
        request["human_attribution_verification"] = {
            "checked": True, "checked_by": "native:actual-caller", "checked_at": "2026-10-20T12:00:00-05:00"}
        args = [sys.executable, "-B", str(self.fixture_factory / "bin/factory.py"), "route-worker",
                "--caller-id", "archon:fixture", "--work-dir", "work/worker"]
        result = subprocess.run(args, input=json.dumps(request), cwd=self.app,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        prepared = json.loads(result.stdout)
        self.assertEqual(prepared["status"], "confirmed_ordinary")
        self.assertEqual(json.loads(Path(prepared["receipt_path"]).read_text()), prepared)
        (self.source / "package.json").write_text("source drift")
        result = subprocess.run(args, input=json.dumps(request), cwd=self.app,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        refused = json.loads(result.stdout)
        self.assertEqual(refused["status"], "refused")
        self.assertNotIn("model", refused)

    def test_actual_native_hook_rechecks_configured_source_before_allow(self):
        self.native_hook_source_boundary("spawn_agent")

    def test_actual_flat_native_hook_rewrites_and_denies_wrong_locator(self):
        self.native_hook_source_boundary("collaborationspawn_agent")

    def native_hook_source_boundary(self, tool_name):
        session_id = "01a15564-fd5f-7550-aadb-de6160ef7b5d"
        caller = "native:" + session_id
        body = "Perform the bounded parser task and verify its assertions."
        request = {"schema": "CPI411SUBAGENTV1", "caller_id": caller, "task_id": "worker:native",
            "project_binding": consumer.project_binding(self.app),
            "task": "Repair the known parser defect in isolated source. Verify parser assertions.",
            "remaining_ms": 2000,
            "ordinary": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium"},
            "native_dispatch": {"task_name": "bounded_parser", "message": body},
            "capability_provenance": {"source": "current_runtime_tool_schema",
                "tool": "collaboration.spawn_agent", "caller_id": caller, "captured_at": time.time(),
                "models": [{"model": "gpt-6.1-sol", "reasoning_efforts": ["medium"]}]},
            "explicit_choice": {"model": "gpt-6.1-sol", "effort": "medium",
                "source": "direct_human_instruction", "instruction_quote": "Use Sol6.1 medium.",
                "instruction_reference": {"kind": "chat_turn", "chat_id": "human:fixture", "turn_id": "human:1"}}}
        prepare_args = [sys.executable, "-B", str(self.fixture_factory / "bin/factory.py"),
                        "route-subagent", "--caller-id", caller, "--work-dir", "work/native"]
        hook_args = [sys.executable, "-B",
                     str(self.fixture_factory / "bin/factory.py"), "native-hook"]

        def prepare():
            result = subprocess.run(prepare_args, input=json.dumps(request), cwd=self.app,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        def event(receipt, tool_id):
            relative = Path(receipt["receipt_path"]).relative_to(self.app)
            return {"hook_event_name": "PreToolUse", "cwd": str(self.app),
                    "session_id": session_id, "turn_id": "turn:1", "tool_use_id": tool_id,
                    "tool_name": tool_name, "tool_input": {"task_name": "bounded_parser",
                        "message": body + "\n[CPI414 preparation=" + str(relative) + "]",
                        "model": "gpt-6-luna", "reasoning_effort": "max", "fork_turns": "all"}}

        def hook(receipt, tool_id, wrong_locator=False):
            payload = event(receipt, tool_id)
            if wrong_locator:
                payload["tool_input"]["message"] = payload["tool_input"]["message"].replace(
                    Path(receipt["receipt_path"]).name, "jev-00000000000000000000000000000000.json")
            result = subprocess.run(hook_args, input=json.dumps(payload), cwd=self.app,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["hookSpecificOutput"]

        positive = prepare()
        wrong = hook(positive, "tool:wrong-locator", wrong_locator=True)
        self.assertEqual(wrong["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", wrong)
        self.assertFalse(Path(positive["receipt_path"]).with_suffix(".consume.json").exists())
        allowed = hook(positive, "tool:valid")
        self.assertEqual(allowed["permissionDecision"], "allow")
        self.assertEqual(allowed["updatedInput"]["model"], "gpt-6.1-sol")
        self.assertEqual(allowed["updatedInput"]["reasoning_effort"], "medium")
        self.assertEqual(allowed["updatedInput"]["fork_turns"], "none")
        self.assertEqual(positive["receipt"]["attempts"], 1)
        self.assertEqual(allowed["updatedInput"]["task_name"], "bounded_parser")
        self.assertEqual(allowed["updatedInput"]["message"], event(positive, "tool:valid")["tool_input"]["message"])

        unused = prepare()
        self.assertEqual(unused["source_binding"],
                         {"revision": self.settings["revision"], "source": self.settings["source"]})
        (self.source / "package.json").write_text("source drift")
        refused = hook(unused, "tool:source-drift")
        self.assertEqual(refused["permissionDecision"], "deny")
        self.assertNotIn("updatedInput", refused)
        self.assertFalse(Path(unused["receipt_path"]).with_suffix(".consume.json").exists())

    def test_factory_state_labels_default_only_for_declaring_workflows(self):
        expected = consumer.MANIFEST["default_inputs"]["archon-triage"]["state_labels"]
        for workflow in ("archon-triage", "archon-ship", "archon-lifecycle"):
            with self.subTest(workflow=workflow):
                result = self.command("run", workflow, "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = json.loads(result.stdout)["argv"]
                self.assertEqual(argv.count("--input"), 1)
                self.assertEqual(argv.count("state_labels=" + expected), 1)
                self.assertFalse(any(arg.startswith(("publish=", "publish_holds="))
                                     for arg in argv))
        result = self.command("run", "archon-backlog", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(arg.startswith("state_labels=") for arg in json.loads(result.stdout)["argv"]))

    def test_explicit_state_labels_override_defaults_in_both_flag_forms(self):
        for input_args in (("--input", "state_labels={}"),
                           ("--input=state_labels={}",)):
            with self.subTest(input_args=input_args):
                result = self.command("run", "archon-ship", *input_args, "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = json.loads(result.stdout)["argv"]
                values = [argv[index + 1] for index, arg in enumerate(argv[:-1])
                          if arg == "--input"]
                values += [arg.removeprefix("--input=") for arg in argv
                           if arg.startswith("--input=")]
                self.assertEqual(values, ["state_labels={}"])

    def test_defaults_do_not_enter_messages_or_native_continuations(self):
        result = self.command("run", "archon-ship", "--", "--input", "state_labels={}")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)["argv"]
        self.assertEqual(argv[argv.index("--") + 1:], ["--input", "state_labels={}"])
        self.assertIn("state_labels=" + consumer.MANIFEST["default_inputs"]["archon-ship"]["state_labels"], argv)
        result = self.command("run", "archon-ship", "--resume", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)["argv"]
        self.assertFalse(any(arg.startswith("state_labels=") for arg in argv))
        self.assertNotIn("--workflow-source", argv)

    def trigger_binding(self):
        return json.loads((self.app / ".factory/trigger.json").read_text())["binding"]

    def test_scheduled_defaults_and_explicit_empty_mapping(self):
        schedule = self.app / ".factory/schedule.json"
        schedule.write_text(json.dumps({"workflow": "archon-lifecycle", "inputs": {}}))
        result = self.command("schedule", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = consumer.MANIFEST["default_inputs"]["archon-lifecycle"]["state_labels"]
        binding = self.trigger_binding()
        self.assertEqual(binding["launch"]["inputs"]["state_labels"], expected)
        self.assertEqual(binding["launch"]["workflowName"], "archon-lifecycle")
        self.assertEqual(binding["launch"]["cwd"], str(self.app))
        self.assertEqual(binding["runAsUserId"], "user-123")
        self.assertEqual(binding["resource"], "github:owner/repo:archon-lifecycle")
        self.assertEqual(binding["capacity"], 1)
        self.assertEqual(binding["overlap"], "skip")
        schedule.write_text(json.dumps({"workflow": "archon-lifecycle",
                                        "inputs": {"state_labels": {}}}))
        result = self.command("schedule", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trigger_binding()["launch"]["inputs"]["state_labels"], "{}")

    def test_parallel_laps_get_their_own_slot_with_that_capacity(self):
        schedule = self.app / ".factory/schedule.json"
        schedule.write_text(json.dumps({"workflow": "archon-lifecycle", "inputs": {}, "parallel": 3}))
        result = self.command("schedule", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        binding = self.trigger_binding()
        self.assertEqual(binding["capacity"], 3)
        # A slot's capacity is fixed once used, so the key names it.
        self.assertEqual(binding["resource"], "github:owner/repo:archon-lifecycle:x3")
        for bad in (0, 9, "3", True, 2.5):
            with self.subTest(parallel=bad):
                schedule.write_text(json.dumps({"workflow": "archon-lifecycle", "inputs": {},
                                                "parallel": bad}))
                result = self.command("schedule", "install")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("parallel must be", result.stderr)

    def test_unknown_commands_fail_without_a_native_launch(self):
        for args in (("tick",), ("level", "4"), ("accept", "gh:pr:1")):
            with self.subTest(args=args):
                result = self.command(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unknown command", result.stderr)
        result = self.command("run", "merge", "gh:pr:1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("absent from the pinned SDLC source", result.stderr)
        self.assertFalse(any(row[:2] == ["workflow", "run"] for row in self.calls()))

    def test_schedule_install_writes_the_binding_but_never_registers_a_timer_unasked(self):
        (self.app / ".factory/schedule.json").write_text(
            json.dumps({"workflow": "archon-lifecycle", "inputs": {}}))
        result = self.command("schedule", "install", "--interval", "600")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Run to enable:", result.stdout)
        config = json.loads((self.app / ".factory/trigger.json").read_text())
        self.assertEqual(config["schedule"]["intervalSeconds"], 600)
        # Only reads reach the native CLI: discovery and whoami, never a timer or a fire.
        commands = [row[:2] for row in self.calls()]
        self.assertIn(["trigger", "whoami"], commands)
        self.assertNotIn(["trigger", "fire"], commands)
        self.assertNotIn(["trigger", "schedule"], commands)

    def test_schedule_fire_fires_then_wakes_and_honors_stop(self):
        (self.app / ".factory/schedule.json").write_text(
            json.dumps({"workflow": "archon-lifecycle", "inputs": {}}))
        self.assertEqual(self.command("schedule", "install").returncode, 0)
        before = len(self.calls())
        result = self.command("schedule", "fire")
        self.assertEqual(result.returncode, 0, result.stderr)
        fired = self.calls()[before:]
        self.assertEqual(fired[0][:3], ["trigger", "fire", "--config"])
        self.assertEqual(Path(fired[0][3]), self.app / ".factory/trigger.json")
        self.assertEqual(fired[1][:2], ["workflow", "wake"])
        self.assertEqual(len(fired), 2)
        before = len(self.calls())
        self.assertEqual(self.command("schedule", "fire").returncode, 0)
        self.assertEqual([row[:2] for row in self.calls()[before:]],
                         [["trigger", "fire"], ["workflow", "wake"]])
        self.assertEqual(self.command("halt").returncode, 0)
        before = len(self.calls())
        result = self.command("schedule", "fire")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Local STOP is set", result.stderr)
        self.assertEqual(self.calls()[before:], [])

    def test_pause_stops_scheduled_launches_but_keeps_waking_active_runs(self):
        # Seen live: disabling the timer to queue an explicit lap stranded a lap already paused
        # on CI, because the same tick that fires launches also wakes durable waits.
        (self.app / ".factory/schedule.json").write_text(
            json.dumps({"workflow": "archon-lifecycle", "inputs": {}}))
        self.assertEqual(self.command("schedule", "install").returncode, 0)
        self.assertEqual(self.command("pause").returncode, 0)
        before = len(self.calls())
        result = self.command("schedule", "fire")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Scheduled launches paused", result.stderr)
        self.assertEqual([row[:2] for row in self.calls()[before:]],
                         [["workflow", "wake"]])
        # An explicit run is the operator's own launch and is not paused.
        before = len(self.calls())
        self.command("run", "archon-lifecycle", "--input", "target=7")
        self.assertIn(["workflow", "run"], [row[:2] for row in self.calls()[before:]])
        self.assertEqual(self.command("unpause").returncode, 0)
        before = len(self.calls())
        self.assertEqual(self.command("schedule", "fire").returncode, 0)
        self.assertEqual([row[:2] for row in self.calls()[before:]],
                         [["trigger", "fire"], ["workflow", "wake"]])

    def test_windows_timer_is_a_hidden_logged_on_task_without_a_console(self):
        import xml.etree.ElementTree as ET
        with patch.object(consumer.sys, "platform", "win32"), \
             patch.object(consumer, "shared_root", return_value=self.app):
            plan = consumer.timer_commands(self.app, 600)
        (task_file, body), = plan["task_xml"].items()
        self.assertEqual(task_file, self.app / ".factory/schedule-task.xml")
        self.assertEqual(plan["install"][0][:2], ["schtasks", "/Create"])
        self.assertEqual(plan["install"][0][plan["install"][0].index("/XML") + 1], str(task_file))
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        task = ET.fromstring(body.split("\n", 1)[1])
        command = task.find("t:Actions/t:Exec/t:Command", ns).text
        if Path(sys.executable).with_name("pythonw.exe").exists():
            self.assertTrue(command.lower().endswith("pythonw.exe"), command)
        self.assertEqual(task.find("t:Actions/t:Exec/t:Arguments", ns).text,
                         "factory\\consumer.py schedule fire")
        self.assertEqual(task.find("t:Actions/t:Exec/t:WorkingDirectory", ns).text, str(self.app))
        self.assertEqual(task.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text,
                         "PT10M")
        self.assertEqual(task.find("t:Settings/t:Hidden", ns).text, "true")
        self.assertEqual(task.find("t:Settings/t:MultipleInstancesPolicy", ns).text, "Parallel")
        self.assertEqual(task.find("t:Principals/t:Principal/t:LogonType", ns).text,
                         "InteractiveToken")

    def test_laps_start_from_the_locally_known_default_branch(self):
        # Seen live: with no base known, each launch ran ls-remote; a failed one left the
        # run pending with its slot held for hours.
        schedule = self.app / ".factory/schedule.json"
        schedule.write_text(json.dumps({"workflow": "archon-lifecycle", "inputs": {}}))
        result = self.command("schedule", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trigger_binding()["launch"]["isolation"], {"kind": "default"})
        self.assertIn("every launch asks the remote", result.stdout)
        head = self.git(self.app, "rev-parse", "HEAD").strip()
        self.git(self.app, "update-ref", "refs/remotes/origin/trunk", head)
        self.git(self.app, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        result = self.command("schedule", "install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trigger_binding()["launch"]["isolation"],
                         {"kind": "worktree", "baseOverride": "trunk"})
        self.assertIn("Laps start from trunk.", result.stdout)
        schedule.write_text(json.dumps({"workflow": "archon-lifecycle", "inputs": {},
                                        "base_branch": "release/2"}))
        self.assertEqual(self.command("schedule", "install").returncode, 0)
        self.assertEqual(self.trigger_binding()["launch"]["isolation"]["baseOverride"], "release/2")
        schedule.write_text(json.dumps({"workflow": "archon-lifecycle", "inputs": {},
                                        "base_branch": "bad name"}))
        self.assertNotEqual(self.command("schedule", "install").returncode, 0)

    def test_single_flight_admits_one_holder_and_frees_on_exit(self):
        lock = self.app / ".factory/fire.lock"
        with consumer.single_flight(lock) as first:
            self.assertTrue(first)
            with consumer.single_flight(lock) as second:
                self.assertFalse(second)
        with consumer.single_flight(lock) as again:
            self.assertTrue(again)

    def test_a_tick_that_finds_a_launch_in_progress_skips_it_but_still_wakes(self):
        (self.app / ".factory/schedule.json").write_text(
            json.dumps({"workflow": "archon-lifecycle", "inputs": {}}))
        self.assertEqual(self.command("schedule", "install").returncode, 0)
        before = len(self.calls())
        with consumer.single_flight(self.app / ".factory/fire.lock") as held:
            self.assertTrue(held)
            result = self.command("schedule", "fire")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Another tick is still launching", result.stderr)
        self.assertEqual([row[:2] for row in self.calls()[before:]],
                         [["workflow", "wake"]])

    def test_a_windowless_tick_logs_its_output_and_opens_no_console(self):
        (self.app / ".factory").mkdir(exist_ok=True)
        (self.app / ".factory/trigger.json").write_text("{}")
        calls = []

        def fake_execute(argv, cwd, **kwargs):
            calls.append((argv, kwargs))
            kwargs["output"].write("native output\n")
            return subprocess.CompletedProcess(argv, 0)
        with patch.object(consumer, "windowless", return_value=True), \
             patch.object(consumer, "read_settings", return_value={}), \
             patch.object(consumer, "verify_source", return_value=self.app), \
             patch.object(consumer, "cli", return_value=["archon"]), \
             patch.object(consumer, "shared_root", return_value=self.app), \
             patch.object(consumer, "execute", side_effect=fake_execute):
            self.assertEqual(consumer.schedule(self.app, ["fire"]), 0)
        self.assertEqual([argv[1:3] for argv, _ in calls],
                         [["trigger", "fire"], ["workflow", "wake"]])
        log = (self.app / ".factory/schedule.log").read_text(encoding="utf-8")
        self.assertIn("=== schedule fire", log)
        self.assertEqual(log.count("native output"), 2)

    @unittest.skipUnless(sys.platform == "win32", "Windows console creation flags")
    def test_children_get_no_console_window_only_when_nothing_shows_their_output(self):
        seen = []
        with patch.object(consumer.subprocess, "run",
                          side_effect=lambda argv, **kw: seen.append(kw) or None):
            consumer.execute(["git"], self.app)
            consumer.execute(["git"], self.app, capture=False)
            with patch.object(consumer, "windowless", return_value=True):
                consumer.execute(["git"], self.app, capture=False)
        flags = [kw.get("creationflags") for kw in seen]
        self.assertEqual(flags, [subprocess.CREATE_NO_WINDOW, None, subprocess.CREATE_NO_WINDOW])

    def test_declared_required_checks_reach_merging_workflows_only(self):
        harness = self.app / "harness/harness.config.json"
        harness.parent.mkdir(parents=True, exist_ok=True)
        harness.write_text(json.dumps({"unit": "", "required_checks": "tests"}))
        for workflow in ("archon-lifecycle", "archon-merge-queue"):
            with self.subTest(workflow=workflow):
                result = self.command("run", workflow, "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["argv"].count("required_checks=tests"), 1)
        result = self.command("run", "archon-ship", "--json")
        self.assertFalse(any(arg.startswith("required_checks=")
                             for arg in json.loads(result.stdout)["argv"]))
        result = self.command("run", "archon-lifecycle", "--input", "required_checks=none", "--json")
        argv = json.loads(result.stdout)["argv"]
        self.assertEqual([a for a in argv if a.startswith("required_checks=")], ["required_checks=none"])
        (self.app / ".factory/schedule.json").write_text(
            json.dumps({"workflow": "archon-lifecycle", "inputs": {}}))
        self.assertEqual(self.command("schedule", "install").returncode, 0)
        self.assertEqual(self.trigger_binding()["launch"]["inputs"]["required_checks"], "tests")
        harness.write_text(json.dumps({"unit": "", "required_checks": ""}))
        result = self.command("run", "archon-lifecycle", "--json")
        self.assertFalse(any(arg.startswith("required_checks=")
                             for arg in json.loads(result.stdout)["argv"]))

    def test_declared_protected_paths_reach_merging_workflows_only(self):
        harness = self.app / "harness/harness.config.json"
        harness.parent.mkdir(parents=True, exist_ok=True)
        paths = "MISSION.md,harness/**"
        # PowerShell's ConvertTo-Json writes a byte-order mark; it must not hide the policy.
        harness.write_text("﻿" + json.dumps(
            {"unit": "", "required_checks": "tests", "protected_paths": paths}), encoding="utf-8")
        for workflow in ("archon-lifecycle", "archon-merge-queue"):
            with self.subTest(workflow=workflow):
                result = self.command("run", workflow, "--json")
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = json.loads(result.stdout)["argv"]
                self.assertEqual(argv.count(f"protected_paths={paths}"), 1)
                self.assertEqual(argv.count("required_checks=tests"), 1)
        result = self.command("run", "archon-ship", "--json")
        self.assertFalse(any(arg.startswith("protected_paths=")
                             for arg in json.loads(result.stdout)["argv"]))
        result = self.command("run", "archon-lifecycle", "--input", "protected_paths=none", "--json")
        argv = json.loads(result.stdout)["argv"]
        self.assertEqual([a for a in argv if a.startswith("protected_paths=")], ["protected_paths=none"])
        (self.app / ".factory/schedule.json").write_text(
            json.dumps({"workflow": "archon-lifecycle", "inputs": {}}))
        self.assertEqual(self.command("schedule", "install").returncode, 0)
        self.assertEqual(self.trigger_binding()["launch"]["inputs"]["protected_paths"], paths)
        harness.write_text(json.dumps({"unit": "", "protected_paths": " "}))
        result = self.command("run", "archon-lifecycle", "--json")
        self.assertFalse(any(arg.startswith("protected_paths=")
                             for arg in json.loads(result.stdout)["argv"]))

    def test_runtime_host_detach_and_resume_refused_before_native_launch(self):
        for args in [("--detach",), ("--detach=true",), ("-d",), ("--resume",)]:
            result = self.command("run", "archon-ship", "--runtime-host", "missing.json", *args)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ownership contract", result.stderr)
        self.assertEqual(self.calls(), [])
        result = self.command("run", "archon-ship", "--detach", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--detach", json.loads(result.stdout)["argv"])

    def test_foreground_runtime_host_wraps_direct_and_scheduled_native_runs_and_cleans(self):
        from test_runtime_host import TARGET
        (self.app / "app.py").write_text(TARGET)
        config = self.app / "runtime.json"
        config.write_text(json.dumps({"version": 1, "roots": {"candidate": str(self.app)},
            "include": ["app.py"], "shape": "http", "command": [sys.executable, "app.py"],
            "health_path": "/health", "identity_path": "/identity", "timeout_s": 5}))
        output = self.base / "host-result.json"
        for rc in (0, 23):
            result = self.command("run", "archon-ship", "--runtime-host", str(config), "--json",
                env={"FACTORY_TEST_HOST_RESULT": str(output), "FACTORY_TEST_EXIT": str(rc)})
            self.assertEqual(result.returncode, rc, result.stderr)
            argv = json.loads(result.stdout)["argv"]
            self.assertNotIn("--runtime-host", argv)
            item = json.loads(output.read_text())
            self.assertFalse(Path(item["snapshot"]).parent.exists())
        self.assertEqual(len([row for row in self.calls() if row[:2] == ["workflow", "run"]]), 2)

    def test_scheduled_runtime_host_refused_because_a_trigger_cannot_wrap_it(self):
        schedule = self.app / ".factory/schedule.json"
        for host in ("runtime.json", "", None, False, 0, [], {}):
            with self.subTest(runtime_host=host):
                schedule.write_text(json.dumps({"workflow": "archon-ship", "inputs": {},
                                                "runtime_host": host}))
                result = self.command("schedule", "install")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("runtime_host.py serve", result.stderr)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.app / ".factory/trigger.json").exists())

    def test_pinned_run_ignores_local_conflict_and_provider_override(self):
        local = self.app / ".archon/workflows/archon-merge-queue.yaml"
        local.parent.mkdir(parents=True)
        local.write_text("name: archon-merge-queue\nTHIS MUST NOT RUN\n")
        direct_inputs = [
            'prs=["https://github.com/owner/repo/pull/12"]',
            "mode=preview",
            'evidence=[{"path":"proof.json","sha256":"abc123"}]',
            "merge_method=merge",
        ]
        result = self.command("run", "archon-merge-queue", "--json", *[
            value for item in direct_inputs for value in ("--input", item)
        ], "--detach")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)["argv"]
        self.assertEqual(argv[argv.index("--workflow-source") + 1], str(self.source))
        self.assertEqual(argv[argv.index("--cwd") + 1], str(self.app))
        for item in direct_inputs:
            self.assertEqual(argv.count(item), 1)
        self.assertNotIn("--no-worktree", argv)
        self.assertNotIn("provider-must-never-run", self.trace.read_text())

    def test_reliability_inputs_pass_through_scheduled_run(self):
        scheduled_inputs = {
            "target": "https://github.com/owner/repo/issues/12",
            "publish": "false",
            "merge_method": "merge",
            "merge_mode": "preview",
            "discovery_publication": "preview",
            "validation_scope": "packages/api",
            "validation_context": "ubuntu-postgres-16",
            "scenario": "runtime.json",
            "holdout": "holdout.json",
            "deploy": "",
            "health": "",
            "identity": "",
        }
        schedule = self.app / ".factory/schedule.json"
        schedule.write_text(json.dumps({"workflow": "archon-lifecycle", "inputs": scheduled_inputs}))
        scheduled = self.command("schedule", "install")
        self.assertEqual(scheduled.returncode, 0, scheduled.stderr)
        inputs = self.trigger_binding()["launch"]["inputs"]
        for key, value in scheduled_inputs.items():
            self.assertEqual(inputs[key], value)

    def test_future_source_workflow_needs_no_alias(self):
        result = self.command("run", "archon-future-queue", "--input", "candidate=pr:1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_shared_workflow_fails_closed(self):
        result = self.command("run", "archon-ambient-only")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no fallback", result.stderr)
        self.assertFalse(any(row[:2] == ["workflow", "run"] for row in self.calls()))

    def test_native_controls_keep_args_and_exit(self):
        for action, args in [("get", ["run-123", "--events"]), ("status", ["--all"]),
                             ("approve", ["run-123", "--comment", "accepted scope"]),
                             ("reject", ["run-123", "--reason", "changed head"]),
                             ("respond", ["run-123", "revise", "keep this text"]),
                             ("cancel", ["run-123"]), ("resume", ["run-123", "--detach"])]:
            with self.subTest(action=action):
                result = self.command(action, *args, "--json", env={"FACTORY_TEST_EXIT": "7"})
                self.assertEqual(result.returncode, 7, result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["id"], "run-123")
                self.assertEqual(payload["status"], "paused")
                self.assertEqual(payload["argv"][:2], ["workflow", action])
                self.assertEqual(payload["argv"][4:4 + len(args)], args)
                self.assertNotIn("--workflow-source", payload["argv"])
        self.assertFalse((self.app / ".factory/runs").exists())

    def test_native_failed_and_unknown_are_not_reinterpreted(self):
        for status in ("running", "failed", "paused", "future-status"):
            result = self.command("get", "run-123", "--json", env={"FACTORY_TEST_STATUS": status})
            self.assertEqual(json.loads(result.stdout)["status"], status)

    def test_adopt_and_resume_have_native_source_semantics(self):
        result = self.command("run", "archon-deliver", "--adopt", "producer-run", "--input", "work=fix.md")
        argv = json.loads(result.stdout)["argv"]
        self.assertIn("producer-run", argv)
        self.assertIn("--workflow-source", argv)
        result = self.command("run", "archon-deliver", "--resume")
        self.assertNotIn("--workflow-source", json.loads(result.stdout)["argv"])

    def test_source_overrides_refused(self):
        for flag in ("--workflow-source=evil", "--cwd=evil", "--workflow-source", "--cwd"):
            self.assertNotEqual(self.command("run", "archon-ship", flag, "evil").returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_separator_cannot_turn_source_flags_into_message_text(self):
        result = self.command("run", "archon-ship", "--", "--resume", "literal message")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)["argv"]
        self.assertLess(argv.index("--cwd"), argv.index("--"))
        self.assertLess(argv.index("--workflow-source"), argv.index("--"))
        self.assertEqual(argv[argv.index("--") + 1:], ["--resume", "literal message"])

    def test_failed_native_launch_is_not_retried_or_promoted(self):
        result = self.command("run", "archon-ship", "--json",
                              env={"FACTORY_TEST_STATUS": "failed", "FACTORY_TEST_EXIT": "23"})
        self.assertEqual(result.returncode, 23)
        self.assertEqual(json.loads(result.stdout)["status"], "failed")
        runs = [args for args in self.calls() if args[:2] == ["workflow", "run"]]
        self.assertEqual(len(runs), 1)
        self.assertFalse((self.app / ".factory/runs").exists())

    def test_stop_does_not_guess_or_cancel_active_runs(self):
        self.assertEqual(self.command("halt").returncode, 0)
        self.assertNotEqual(self.command("run", "archon-ship").returncode, 0)
        self.assertEqual(self.command("status", "--json").returncode, 0)
        self.assertEqual(self.command("cancel", "run-123").returncode, 0)
        self.assertEqual(self.command("unhalt").returncode, 0)
        self.assertFalse(any(row[:2] == ["workflow", "run"] for row in self.calls()))

    def test_sha_dirty_and_ignored_authoring_drift_refused(self):
        with self.assertRaisesRegex(ValueError, "SHA mismatch|exact immutable native source pin"):
            other = self.base / ("a" * 40)
            other.mkdir()
            self.git(self.base, "clone", "-q", str(self.origin), str(other))
            consumer.verify_source({**self.settings, "source": str(other), "revision": "a" * 40})
        (other / "node_modules").mkdir()
        with patch.dict(consumer.MANIFEST, {"integration_revision_required": "a" * 40}):
            with self.assertRaisesRegex(ValueError, "Source SHA mismatch"):
                consumer.verify_source({**self.settings, "source": str(other), "revision": "a" * 40})
        command = next(self.source.rglob("check.md"))
        command.write_text("modified")
        with self.assertRaisesRegex(ValueError, "changes"):
            consumer.verify_source(self.settings)
        self.git(self.source, "restore", ".")
        (self.source / ".archon/ignored.yaml").write_text("name: archon-ship")
        with self.assertRaisesRegex(ValueError, "Untracked authoring"):
            consumer.verify_source(self.settings)

    def test_doctor_missing_workflow_or_command_is_not_ready(self):
        self.assertEqual(consumer.doctor(self.settings)["revision"], self.revision)
        with patch.object(consumer, "discover", return_value={}):
            with self.assertRaisesRegex(ValueError, "missing shared"):
                consumer.doctor(self.settings)
        with patch.object(consumer, "native_json", return_value={"results": [{"valid": False}]}):
            with self.assertRaisesRegex(ValueError, "validation failed"):
                consumer.validate(self.settings, self.source, "archon-ship")

    def test_capability_probe_refuses_unsupported_cli(self):
        with patch.object(consumer, "checked", return_value="no capabilities"), \
             patch.object(consumer, "verify_source", return_value=self.source):
            with self.assertRaisesRegex(ValueError, "missing workflow"):
                consumer.doctor(self.settings)

    def test_source_index_refuses_missing_command_include_and_conflict(self):
        path = next(self.source.rglob("archon-ship.yaml"))
        body = path.read_text()
        command = path.parent / "commands/check.md"
        command.unlink()
        with self.assertRaisesRegex(ValueError, "Missing source-owned command"):
            consumer.source_workflows(self.source)
        command.write_text("fixture")
        path.write_text(body + "    include: archon-only-in-global-config\n")
        with self.assertRaisesRegex(ValueError, "Include .* missing"):
            consumer.source_workflows(self.source)
        path.write_text(body)
        conflict = self.source / ".archon/workflows/conflict.yaml"
        conflict.write_text("name: archon-ship\n")
        with self.assertRaisesRegex(ValueError, "Conflicting workflow"):
            consumer.source_workflows(self.source)

    def test_pretty_json_and_malformed_response(self):
        self.assertTrue(consumer.native_json(self.settings, self.source, ["workflow", "list"], self.source)["workflows"])
        with patch.object(consumer, "checked", return_value='{}\n{}'):
            with self.assertRaises(ValueError):
                consumer.native_json(self.settings, self.source, [], self.source)

    def test_worktree_uses_shared_settings_and_its_own_target(self):
        worktree = self.base / "application worktree"
        self.git(self.app, "worktree", "add", "-b", "fixture-branch", str(worktree))
        self.assertEqual(consumer.read_settings(worktree), self.settings)
        result = subprocess.run([sys.executable, str(self.fixture_factory / "bin/factory.py"), "run", "archon-ship", "--json"],
                                cwd=worktree, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(result.stdout)["argv"]
        self.assertEqual(argv[argv.index("--cwd") + 1], str(worktree))
        self.assertEqual(argv[argv.index("--workflow-source") + 1], str(self.source))


GATE_SCRIPT = r'''
import json, os, sys
from pathlib import Path
state = Path.cwd() / ".factory"
names = ("INPUTS_CLAIM_HOURS", "INPUTS_ENDED_RUNS")
seen = {name: os.getenv(name) for name in names}
seen["cwd"] = os.getcwd()
(state / "test-seen.json").write_text(json.dumps(seen))
if (state / "test-fail").exists():
    sys.exit("gh: could not resolve host")
print((state / "test-board.json").read_text())
'''


class LaunchGateTests(unittest.TestCase):
    """The launch gate decides from the board and the run list; nothing here launches."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=WORK, prefix="factory gate ")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.app = self.base / "app"
        (self.app / ".factory").mkdir(parents=True)
        self.source = self.base / "source"
        gate = self.source / consumer.MANIFEST["source_directory"] / consumer.MANIFEST["launch_gates"]["archon-loop"]
        gate.parent.mkdir(parents=True)
        # The gate runs under settings["bun"]; here that is Python, so the fake is Python.
        gate.write_text(GATE_SCRIPT, encoding="utf-8")
        self.settings = {"bun": sys.executable}
        (self.app / ".factory/trigger.json").write_text(json.dumps({"binding": {"launch": {
            "workflowName": "archon-loop", "inputs": {"claim_hours": "3"}}}}))
        self.home = self.base / "archon home"
        (self.home / "logs").mkdir(parents=True)
        self.seen = self.app / ".factory/test-seen.json"
        self.runs = []
        self.abandoned = []
        self.lines = []
        env = patch.dict(os.environ, {"ARCHON_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for target, value in (("shared_root", lambda root: self.app),
                              ("native_json", lambda *a: {"runs": self.runs})):
            patcher = patch.object(consumer, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        real_execute = consumer.execute

        def execute(argv, cwd, **kwargs):
            if "abandon" in argv:
                self.abandoned.append(argv[argv.index("abandon") + 1])
                return subprocess.CompletedProcess(argv, 0, "", "")
            return real_execute(argv, cwd, **kwargs)
        patcher = patch.object(consumer, "execute", side_effect=execute)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(consumer, "cli", return_value=["archon"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def decide(self, board, schedule=None):
        (self.app / ".factory/test-board.json").write_text(json.dumps(
            {"workable": [], "claims": [], "ended": [], "waiting": [], **board}))
        if schedule is not None:
            (self.app / ".factory/schedule.json").write_text(json.dumps(schedule))
        return consumer.launch_decision(self.app, self.settings, self.source, self.lines.append)

    def recent_launch(self):
        (self.app / ".factory/gate.json").write_text(json.dumps({"last_launch": time.time()}))

    def test_launches_when_work_is_workable_and_no_lap_is_still_choosing(self):
        # A lap holding a claim is not choosing: it does not hold back the next launch.
        self.runs = [{"id": "a", "workflow_name": "archon-loop", "status": "running"}]
        board = {"workable": [{"kind": "issue", "number": 4}, {"kind": "issue", "number": 5}],
                 "claims": [{"kind": "issue", "number": 2, "run": "a"}]}
        self.assertTrue(self.decide(board))
        self.assertIn("workable #4, #5; no lap still choosing", self.lines[-1])

    def test_no_launch_when_every_slot_is_busy(self):
        (self.app / ".factory/trigger.json").write_text(json.dumps({"binding": {"capacity": 2, "launch": {
            "workflowName": "archon-loop", "inputs": {}}}}))
        self.runs = [{"id": r, "workflow_name": "archon-loop", "status": "running"} for r in ("a", "b")]
        board = {"workable": [{"kind": "issue", "number": 4}],
                 "claims": [{"kind": "issue", "number": 2, "run": "a"},
                            {"kind": "issue", "number": 3, "run": "b"}]}
        self.assertFalse(self.decide(board))
        self.assertIn("all 2 slot(s) busy", self.lines[-1])

    def test_one_pick_at_a_time(self):
        # Seen live: two laps choosing at once both picked the lowest item and the later one
        # did nothing. While a lap is still choosing, the next waits for the next tick.
        self.recent_launch()
        self.runs = [{"id": "a", "workflow_name": "archon-loop", "status": "running"}]
        self.assertFalse(self.decide({"workable": [{"kind": "issue", "number": 4},
                                                   {"kind": "issue", "number": 5}]}))
        self.assertIn("1 lap(s) active, 1 still choosing", self.lines[-1])
        seen = json.loads(self.seen.read_text())
        self.assertEqual(seen["INPUTS_CLAIM_HOURS"], "3")
        self.assertEqual(Path(seen["cwd"]).resolve(), self.app.resolve())

    def test_skips_when_every_workable_item_already_has_a_lap_choosing(self):
        # One workable item and one lap that has not claimed yet: a second lap would
        # race it for the same item and do nothing.
        self.recent_launch()
        self.runs = [{"id": "a", "workflow_name": "archon-loop", "status": "running"},
                     {"id": "b", "workflow_name": "archon-loop", "status": "running"},
                     {"id": "m", "workflow_name": "archon-manager", "status": "running"}]
        board = {"workable": [{"kind": "issue", "number": 4}],
                 "claims": [{"kind": "issue", "number": 2, "run": "b"}]}
        self.assertFalse(self.decide(board))
        self.assertIn("No launch", self.lines[-1])
        self.assertIn("2 lap(s) active, 1 still choosing", self.lines[-1])

    def test_an_empty_lap_on_an_unchanged_board_is_not_repeated(self):
        # Seen live: the gate counted items workable that pick stayed clear of (they touch
        # what live claims change); every tick bought another empty pick.
        board = {"workable": [{"kind": "issue", "number": 7}],
                 "claims": [{"kind": "issue", "number": 2, "run": "busy"}]}
        self.runs = [{"id": "busy", "workflow_name": "archon-loop", "status": "running"}]
        self.assertTrue(self.decide(board))
        launched = json.loads((self.app / ".factory/gate.json").read_text())["last_launch"]
        stamp = lambda t: time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))
        self.runs.append({"id": "empty", "workflow_name": "archon-loop", "status": "completed",
                          "started_at": stamp(launched + 5), "completed_at": stamp(launched + 70)})
        self.assertFalse(self.decide(board))
        self.assertIn("nothing to start on this same board", self.lines[-1])
        # Seen live: laps that failed in seconds on a used-up usage limit held the factory
        # idle for hours. A failed lap says nothing about the board.
        self.runs[-1]["status"] = "failed"
        self.assertTrue(self.decide(board))
        # The board changes (that lap ended and released its claim): the next lap may find something.
        board["claims"] = []
        self.runs[0]["status"] = "completed"
        self.assertTrue(self.decide(board))

    def test_a_drained_board_launches_only_once_per_idle_period(self):
        self.recent_launch()
        self.assertFalse(self.decide({}))
        (self.app / ".factory/gate.json").write_text(json.dumps({"last_launch": time.time() - 3700}))
        self.assertTrue(self.decide({}))
        self.assertIn("bookkeeping", self.lines[-1])
        self.assertFalse(self.decide({}))
        (self.app / ".factory/gate.json").write_text(json.dumps({"last_launch": 0}))
        self.assertFalse(self.decide({}, schedule={"idle_pick_minutes": 0}))

    def test_an_unreadable_board_launches_as_before_the_gate(self):
        self.recent_launch()
        (self.app / ".factory/test-fail").write_text("1")
        self.assertTrue(self.decide({}))
        self.assertIn("could not read the board", self.lines[-1])

    def test_a_workflow_without_a_gate_always_launches(self):
        (self.app / ".factory/trigger.json").write_text(json.dumps({"binding": {"launch": {
            "workflowName": "archon-lifecycle", "inputs": {}}}}))
        self.assertTrue(self.decide({}))
        self.assertFalse(self.seen.exists())

    def test_runs_that_never_started_are_abandoned_and_ended_runs_named(self):
        self.recent_launch()
        stuck = self.home / "logs/trigger-run-ghost.log"
        stuck.write_text('{"level":40}\nError: Failed to fetch base branch from \'origin\'\n')
        old = time.time() - 900
        os.utime(stuck, (old, old))
        fresh = self.home / "logs/trigger-run-new.log"
        fresh.write_text("Error: still writing\n")
        self.runs = [{"id": "ghost", "workflow_name": "archon-loop", "status": "pending"},
                     {"id": "new", "workflow_name": "archon-loop", "status": "pending"},
                     {"id": "done", "workflow_name": "archon-loop", "status": "completed"},
                     {"id": "killed", "workflow_name": "archon-loop", "status": "failed"}]
        board = {"workable": [{"kind": "issue", "number": 9}],
                 "ended": [{"kind": "issue", "number": 9, "run": "killed", "url": "u"}]}
        # The ghost no longer counts as a lap choosing work; the fresh pending one does.
        self.assertFalse(self.decide(board))
        self.assertEqual(self.abandoned, ["ghost"])
        self.assertEqual(json.loads(self.seen.read_text())["INPUTS_ENDED_RUNS"], "done,killed")
        self.assertTrue(any("Released the claim run killed left on #9" in l for l in self.lines))
        self.runs = [r for r in self.runs if r["id"] != "new"]
        self.assertTrue(self.decide(board))


class SourceConformanceTests(unittest.TestCase):
    @patch.object(sys, "dont_write_bytecode", True)
    def test_supplied_pinned_source_accepts_factory_state_labels(self):
        supplied = os.environ.get("FACTORY_ARCHON_CONFORMANCE_SOURCE")
        if not supplied:
            self.skipTest("set FACTORY_ARCHON_CONFORMANCE_SOURCE to a complete pinned checkout")
        source = Path(supplied).resolve()
        revision = consumer.MANIFEST["integration_revision_required"]
        settings = {"source": str(source), "revision": revision}
        self.assertEqual(consumer.checked(["git", "rev-parse", "HEAD"], source).strip(), revision)
        consumer.verify_source(settings)
        self.addCleanup(consumer.verify_source, settings)

        bun = Path(os.environ.get("FACTORY_ARCHON_CONFORMANCE_BUN", ""))
        self.assertTrue(bun.is_absolute() and bun.is_file(),
                        "set FACTORY_ARCHON_CONFORMANCE_BUN to the configured exact Bun executable")
        version = subprocess.run([str(bun), "--version"], capture_output=True, text=True,
                                 timeout=10, check=True)
        self.assertEqual(version.stdout.strip(), "1.4.2")

        defaults = consumer.MANIFEST["default_inputs"]
        workflows = ("archon-triage", "archon-ship", "archon-lifecycle")
        self.assertEqual(set(defaults), set(workflows))
        mappings = [json.loads(defaults[name]["state_labels"]) for name in workflows]
        self.assertTrue(all(mapping == mappings[0] for mapping in mappings))

        directory = source / consumer.MANIFEST["source_directory"]
        lifecycle = load("pinned_select_target", directory / "lifecycle/scripts/select-target.py")
        self.assertEqual(set(mappings[0]), lifecycle.STATES)
        for mapping in mappings:
            with patch.dict(os.environ, {"INPUTS_STATE_LABELS": json.dumps(mapping)}):
                labels, blocked_label, complete = lifecycle.state_labels()
            self.assertTrue(complete)
            self.assertEqual(labels, {label.casefold() for label in mapping.values()})
            self.assertEqual(blocked_label, mapping["BLOCKED"].casefold())

        report = {"type": "archon_artifact", "run_id": "factory-conformance-non-live", "path": "triage.md"}
        env = {key: value for key, value in os.environ.items() if not key.startswith("INPUTS_")}
        env.update({
            "INPUTS_CONTRACT": "READY", "INPUTS_ROUTE": "deliver",
            "INPUTS_COMPLEXITY": "small", "INPUTS_DESIGN_FIRST": "false",
            "INPUTS_PUBLISH": "false", "INPUTS_STATE_LABELS": json.dumps(mappings[0]),
            "INPUTS_SUMMARY": "Factory state-label conformance",
            "INPUTS_BLOCKED_REASON": "", "INPUTS_AREA_LABELS": "[]",
            "INPUTS_ITEM": json.dumps({"repo": {"host": "", "path": ""}, "number": 0}),
            "INPUTS_PROPOSED_EDITS": json.dumps({"title": "", "body": ""}),
            "INPUTS_BLOCKED_BY": "[]", "INPUTS_REPORT": json.dumps(report),
        })

        def verdict(**inputs):
            return subprocess.run(
                [str(bun), str(directory / "triage/scripts/verdict.ts")], cwd=HOME,
                env={**env, **{"INPUTS_" + key: value for key, value in inputs.items()}},
                capture_output=True, text=True, timeout=30)

        cases = {
            "READY": {},
            "DESIGN_FIRST": {"ROUTE": "plan", "DESIGN_FIRST": "true"},
            "NEEDS_CONTRACT_WORK": {
                "CONTRACT": "NEEDS_CONTRACT_WORK", "ROUTE": "no_action",
                "PROPOSED_EDITS": json.dumps({"title": "Complete contract", "body": "Acceptance"})},
            "BLOCKED": {"CONTRACT": "BLOCKED", "ROUTE": "no_action",
                        "BLOCKED_REASON": "External decision",
                        "BLOCKED_BY": json.dumps(["https://example.invalid/decisions/1"])},
            "NO_ACTION": {"CONTRACT": "NO_ACTION", "ROUTE": "no_action"},
        }
        self.assertEqual(set(cases), set(mappings[0]))
        custom_mapping = {state: "factory-" + state.lower() for state in mappings[0]}
        for mapping in (mappings[0], custom_mapping):
            for state, inputs in cases.items():
                with self.subTest(state=state, mapping=mapping):
                    result = verdict(STATE_LABELS=json.dumps(mapping), **inputs)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    output = json.loads(result.stdout)
                    self.assertEqual(output["labels"], [mapping[state]])
                    self.assertEqual(output["contract"], inputs.get("CONTRACT", "READY"))
                    self.assertEqual(output["route"], inputs.get("ROUTE", "deliver"))
                    self.assertEqual(output["design_first"], state == "DESIGN_FIRST")
                    self.assertEqual(output["ready"], state in {"READY", "DESIGN_FIRST"})
                    self.assertIs(output["published"], False)
                    self.assertEqual(output["skipped_labels"], [])
                    self.assertEqual(output["report"], report)

        refusals = (
            ({"STATE_LABELS": "{"}, "state_labels must be a JSON object"),
            ({"STATE_LABELS": "[]"}, "state_labels must be a JSON object"),
            ({"STATE_LABELS": '{"UNKNOWN":"unknown"}'}, "unsupported state: UNKNOWN"),
            ({"STATE_LABELS": '{"READY":"same","BLOCKED":"SAME"}'},
             "must not map multiple states to the same label"),
            ({"AREA_LABELS": '["area","AREA"]'}, "area_labels must not contain duplicates"),
            ({"AREA_LABELS": json.dumps([mappings[0]["BLOCKED"].upper()])},
             "area_labels may not name a pack label or a caller-owned state label"),
            ({"STATE_LABELS": json.dumps(custom_mapping),
              "AREA_LABELS": json.dumps([custom_mapping["BLOCKED"].upper()])},
             "area_labels may not name a pack label or a caller-owned state label"),
        )
        for inputs, message in refusals:
            with self.subTest(refusal=inputs):
                result = verdict(**inputs)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn(message, result.stderr)

        for name, folder in (("archon-triage", "triage"), ("archon-ship", "ship"),
                             ("archon-lifecycle", "lifecycle")):
            lines = (directory / folder / f"{name}.yaml").read_text(encoding="utf-8").splitlines()
            start = lines.index("inputs:") + 1
            input_lines = next((lines[start:index] for index in range(start, len(lines))
                                if lines[index] and not lines[index].startswith(" ")), lines[start:])
            declared = {line[2:-1] for line in input_lines
                        if line.startswith("  ") and not line.startswith("    ") and line.endswith(":" )}
            self.assertIn("state_labels", declared)

        policy_labels = {}
        for line in (TEMPLATE / "factory/WORKFLOW_POLICY.md").read_text(encoding="utf-8").splitlines():
            if line.startswith("| `"):
                state, label = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
                policy_labels[state] = label
        self.assertEqual(policy_labels, mappings[0])
        consumer.verify_source(settings)


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        scratch = HOME / "work/cpi410-correction-3"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch, prefix="configure ")
        self.addCleanup(self.temp.cleanup)
        self.app = Path(self.temp.name) / "project with spaces"
        self.app.mkdir()
        consumer.checked(["git", "init", "-q"], self.app)
        store = self.app / "_bmad/custom/ticketing-store-config.toml"
        store.parent.mkdir(parents=True)
        store.write_text('[tickets]\nstore="linear"\nproject="configure-project"\n')
        (self.app / ".factory").mkdir()
        (self.app / ".factory/project-context.md").write_text("Configure context\n")
        self.path = self.app / consumer.SETTINGS
        self.settings = {"source": "new source", "revision": "a" * 40, "bun": "new bun"}
        self.router = {"key_file": "private/key reference", "jev_model": "jev-latest"}
        self.previous = {"source": "old source", "revision": "b" * 40, "bun": "old bun",
                         "project_binding": consumer.project_binding(self.app),
                         "jev_router": self.router, "unmanaged": "must not carry over"}

    def write_previous(self):
        self.path.write_text(json.dumps(self.previous))

    def test_configure_preserves_only_project_router_options_and_refreshes_binding(self):
        self.write_previous()
        (self.app / ".factory/project-context.md").write_text("Updated context\n")
        original = dict(self.settings)
        for _ in range(2):
            configure(self.app, self.settings)
            self.assertEqual(consumer.read_settings(self.app), {
                **self.settings, "project_binding": consumer.project_binding(self.app),
                "jev_router": self.router})
        self.assertEqual(self.settings, original)

    def test_explicit_router_settings_replace_prior_options_including_empty_options(self):
        for router in ({"key_file": "new reference", "jev_model": "new-model"},
                       {"jev_model": "new-model"}, {}):
            with self.subTest(router=router):
                self.write_previous()
                settings = {**self.settings, "jev_router": router,
                            "project_binding": {"root": "stale caller binding"}}
                configure(self.app, settings)
                self.assertEqual(consumer.read_settings(self.app), {
                    **self.settings, "jev_router": router,
                    "project_binding": consumer.project_binding(self.app)})
                self.assertEqual(settings["project_binding"], {"root": "stale caller binding"})

    def test_fresh_configuration_and_configuration_without_router_options(self):
        for previous in (None, {"project_binding": consumer.project_binding(self.app)}):
            with self.subTest(previous=previous):
                self.path.unlink(missing_ok=True)
                if previous is not None:
                    self.path.write_text(json.dumps(previous))
                configure(self.app, self.settings)
                self.assertEqual(consumer.read_settings(self.app), {
                    **self.settings, "project_binding": consumer.project_binding(self.app)})

    def test_configure_does_not_borrow_foreign_or_unbound_router_options(self):
        binding = consumer.project_binding(self.app)
        for field in ("root", "tracker", "project"):
            with self.subTest(field=field):
                self.previous["project_binding"] = {**binding, field: "another identity"}
                self.write_previous()
                configure(self.app, self.settings)
                self.assertEqual(consumer.read_settings(self.app), {
                    **self.settings, "project_binding": binding})
        for unbound in (None, {}, "not a binding"):
            with self.subTest(binding=unbound):
                self.previous["project_binding"] = unbound
                self.write_previous()
                configure(self.app, self.settings)
                self.assertNotIn("jev_router", consumer.read_settings(self.app))

    def test_copied_project_settings_cannot_supply_router_options(self):
        other = Path(self.temp.name) / "other project"
        other.mkdir()
        consumer.checked(["git", "init", "-q"], other)
        shutil.copytree(self.app / "_bmad", other / "_bmad")
        shutil.copytree(self.app / ".factory", other / ".factory")
        configure(other, {**self.settings, "jev_router": self.router})
        self.path.write_bytes((other / consumer.SETTINGS).read_bytes())
        configure(self.app, self.settings)
        self.assertEqual(consumer.read_settings(self.app), {
            **self.settings, "project_binding": consumer.project_binding(self.app)})
        self.assertEqual(consumer.read_settings(other)["jev_router"], self.router)

    def test_linked_worktree_preserves_canonical_project_options(self):
        self.write_previous()
        consumer.checked(["git", "-c", "user.name=Fixture", "-c",
                          "user.email=fixture@example.invalid", "commit", "--allow-empty",
                          "-qm", "fixture"], self.app)
        worktree = Path(self.temp.name) / "linked worktree"
        consumer.checked(["git", "worktree", "add", "-q", "-b", "linked", str(worktree)], self.app)
        configure(worktree, self.settings)
        self.assertEqual(consumer.read_settings(worktree), {
            **self.settings, "project_binding": consumer.project_binding(self.app),
            "jev_router": self.router})
        self.assertFalse((worktree / consumer.SETTINGS).exists())

    def test_configure_refuses_malformed_existing_settings_without_overwriting(self):
        for raw in ("{broken", "[]", "null", *(
                json.dumps({**self.previous, "jev_router": value})
                for value in (None, [], "invalid", {"key_file": 42}, {"jev_model": []},
                              {"jev_model": "invalid model"}, {"unsupported": "option"}))):
            with self.subTest(raw=raw):
                self.path.write_text(raw)
                with self.assertRaises(ValueError):
                    configure(self.app, self.settings)
                self.assertEqual(self.path.read_text(), raw)
                self.assertFalse(self.path.with_suffix(".tmp").exists())

    def test_configure_refuses_malformed_explicit_options_without_overwriting(self):
        self.write_previous()
        before = self.path.read_bytes()
        for router in (None, [], "invalid", {"key_file": False}, {"jev_model": None}):
            with self.subTest(router=router):
                with self.assertRaises(ValueError):
                    configure(self.app, {**self.settings, "jev_router": router})
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.path.with_suffix(".tmp").exists())


class InstallTests(Fixture):
    def test_repeated_init_preserves_project_router_options(self):
        factory = load("factory_router_init_test", HOME / "bin/factory.py")
        router = {"key_file": "private/missing-reference", "jev_model": "jev-latest"}
        (self.app / consumer.SETTINGS).unlink()
        with patch.object(sys, "argv", ["factory.py", "init"]), \
             patch.object(factory.consumer, "project_root", return_value=self.app), \
             patch.object(factory, "install_source", return_value=self.settings), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(factory.main(), 0)
            self.assertEqual(consumer.read_settings(self.app), self.settings)
            configure(self.app, {**self.settings, "jev_router": router})
            for _ in range(2):
                self.assertEqual(factory.main(), 0)
                self.assertEqual(consumer.read_settings(self.app)["jev_router"], router)

    def test_policy_pointer_preserves_existing_bytes_and_is_idempotent(self):
        agents = self.app / "engineering.md"
        original = b"# Existing guidance\r\n\r\nKeep this byte-for-byte."
        agents.write_bytes(original)
        preserved_time = 946684800_000_000_000
        os.utime(agents, ns=(preserved_time, preserved_time))
        with contextlib.redirect_stdout(io.StringIO()):
            sync(self.app)
        installed = agents.read_bytes()
        self.assertTrue(installed.startswith(original))
        self.assertEqual(installed.count(b"factory/WORKFLOW_POLICY.md"), 1)
        self.assertEqual(agents.stat().st_mtime_ns, preserved_time)
        with contextlib.redirect_stdout(io.StringIO()):
            sync(self.app)
        self.assertEqual(agents.read_bytes(), installed)
        self.assertTrue((self.app / "factory/WORKFLOW_POLICY.md").is_file())

    def test_policy_pointer_failed_temp_write_preserves_original_and_cleans_temp(self):
        agents = self.app / "engineering.md"
        original = b"# Existing guidance\n"
        agents.write_bytes(original)
        with patch("install.os.fsync", side_effect=OSError("interrupted write")), \
             self.assertRaisesRegex(OSError, "interrupted write"):
            install_policy_pointer(self.app, False)
        self.assertEqual(agents.read_bytes(), original)
        self.assertEqual(list(self.app.glob(".engineering.md.*.tmp")), [])

    def test_policy_pointer_failed_replace_preserves_original_and_cleans_temp(self):
        agents = self.app / "engineering.md"
        original = b"# Existing guidance\n"
        agents.write_bytes(original)
        with patch("install.os.replace", side_effect=OSError("interrupted replace")), \
             self.assertRaisesRegex(OSError, "interrupted replace"):
            install_policy_pointer(self.app, False)
        self.assertEqual(agents.read_bytes(), original)
        self.assertEqual(list(self.app.glob(".engineering.md.*.tmp")), [])

    def test_policy_pointer_honors_dry_run_and_creates_missing_file(self):
        agents = self.app / "engineering.md"
        with contextlib.redirect_stdout(io.StringIO()) as output:
            sync(self.app, True)
        self.assertFalse(agents.exists())
        self.assertIn("engineering.md factory policy pointer", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            sync(self.app)
        self.assertEqual(agents.read_bytes(), POLICY_POINTER)

    def test_policy_pointer_goes_to_engineering_md_and_leaves_agents_md_alone(self):
        agents = self.app / "AGENTS.md"
        original = b"# Native agent guidance" + bytes([10])
        agents.write_bytes(original)
        with contextlib.redirect_stdout(io.StringIO()):
            sync(self.app)
        self.assertEqual(agents.read_bytes(), original)
        self.assertEqual((self.app / "engineering.md").read_bytes(), POLICY_POINTER)

    def test_policy_pointer_refuses_escaping_symlink(self):
        agents = self.app / "engineering.md"
        outside = self.base / "outside-engineering.md"
        outside.write_text("outside")
        try:
            agents.symlink_to(outside)
        except OSError:
            real_resolve = Path.resolve

            def escaping_resolve(path, *args, **kwargs):
                return outside if path == agents else real_resolve(path, *args, **kwargs)
            with patch.object(Path, "resolve", escaping_resolve), \
                 self.assertRaisesRegex(ValueError, "outside application"):
                install_policy_pointer(self.app, False)
            self.assertEqual(outside.read_text(), "outside")
            return
        with self.assertRaisesRegex(ValueError, "outside application"):
            install_policy_pointer(self.app, False)
        self.assertEqual(outside.read_text(), "outside")

    def test_upgrade_preserves_personal_files_and_backs_up_changed_factory_files(self):
        originals = {"harness/harness.config.json": b'{"agent":{"cmd":"do not run"}}',
                     "harness/END-TO-END.md": b"custom journey\r\n", ".factory/holdout/HOLDOUT.md": b"private scenario\n",
                     ".archon/config.yaml": b"defaultAssistant: custom\r\n", "app.txt": b"original app\n"}
        for rel, data in originals.items():
            path = self.app / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        changed = self.app / "factory/RUNTIME_HOST.md"
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_bytes(b"locally edited\r\n")
        provider = self.base / "home/.archon/config.yaml"
        provider.parent.mkdir(parents=True)
        provider.write_bytes(b"defaultAssistant: custom\r\nprivate: preserved\n")
        provider_before = provider.read_bytes()
        with patch.dict(os.environ, {"HOME": str(self.base / "home"), "USERPROFILE": str(self.base / "home")}), contextlib.redirect_stdout(io.StringIO()):
            sync(self.app)
            consumer.doctor(self.settings)
        self.assertEqual(provider.read_bytes(), provider_before)
        for rel, data in originals.items():
            self.assertEqual((self.app / rel).read_bytes(), data, rel)
        self.assertEqual(changed.read_bytes(), (TEMPLATE / "factory/RUNTIME_HOST.md").read_bytes())
        self.assertEqual((self.app / ".factory/backup/factory/RUNTIME_HOST.md").read_bytes(),
                         b"locally edited\r\n")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            sync(self.app)
        self.assertNotIn("install ", out.getvalue())
        self.assertNotIn("preserve ", out.getvalue())

    def test_scaffold_leaves_integration_unconfigured(self):
        (self.app / consumer.SETTINGS).unlink()
        result = self.command("init", "--scaffold-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(list((self.app / ".archon").rglob("*.yaml")))
        result = self.command("doctor")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Integration pin required", result.stderr)

    def test_default_init_installs_manifest_revision(self):
        factory = load("factory_init_test", HOME / "bin/factory.py")
        settings = {"source": "immutable", "revision": consumer.MANIFEST["integration_revision_required"]}
        with patch.object(sys, "argv", ["factory.py", "init"]), \
             patch.object(factory.consumer, "project_root", return_value=self.app), \
             patch.object(factory, "sync") as sync_mock, \
             patch.object(factory, "install_source", return_value=settings) as install_mock, \
             patch.object(factory, "configure") as configure_mock, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(factory.main(), 0)
        sync_mock.assert_called_once_with(self.app)
        install_mock.assert_called_once_with(
            consumer.MANIFEST["repository"], consumer.MANIFEST["integration_revision_required"],
            Path.home() / ".cache/factory/archon", "bun")
        configure_mock.assert_called_once_with(self.app, settings)

    def test_installer_clones_full_source_and_never_repoints_cache(self):
        cache = self.base / "cache with spaces"
        # Only Bun's package install boundary is faked; clone, SHA checks, native CLI
        # discovery and command validation run as real subprocesses.
        original = consumer.checked
        def checked(argv, cwd, timeout=180):
            if argv[1:3] == ["install", "--frozen-lockfile"]:
                (cwd / "node_modules").mkdir()
                return "installed"
            return original(argv, cwd, timeout)
        with patch.object(consumer, "checked", side_effect=checked):
            settings = install_source(str(self.origin), self.revision, cache, sys.executable)
        self.assertEqual(settings["source"], str(cache / self.revision))
        self.assertTrue((Path(settings["source"]) / "package.json").is_file())
        tracked = Path(settings["source"]) / "package.json"
        tracked.write_text("dirty")
        with self.assertRaisesRegex(ValueError, "dirty"):
            install_source(str(self.origin), self.revision, cache, sys.executable)
        self.assertEqual(tracked.read_text(), "dirty")

    def test_dry_upgrade_and_repeat_backup_preserve_bytes(self):
        path = self.app / "factory/RUNTIME_HOST.md"
        path.parent.mkdir()
        path.write_bytes(b"custom notes\r\n")
        with contextlib.redirect_stdout(io.StringIO()):
            sync(self.app, True)
        self.assertEqual(path.read_bytes(), b"custom notes\r\n")
        self.assertFalse((self.app / ".factory/backup").exists())
        with contextlib.redirect_stdout(io.StringIO()):
            sync(self.app)
            path.write_bytes(b"second custom version")
            sync(self.app)
        base = self.app / ".factory/backup/factory/RUNTIME_HOST.md"
        self.assertEqual(base.read_bytes(), b"custom notes\r\n")
        self.assertEqual(base.with_suffix(".md.1").read_bytes(), b"second custom version")


class NativeAssessmentPackagingTests(unittest.TestCase):
    def test_provider_command_example_has_real_shell_continuation(self):
        text = (TEMPLATE / "factory/PROVIDER_WORKERS.md").read_text()
        command = text.split("~~~sh", 1)[1].split("~~~", 1)[0].strip()
        lines = command.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].endswith(chr(92)))
        self.assertTrue(lines[1].lstrip().startswith("--work-dir "))
        self.assertNotIn(chr(92) + "n", command)

    def test_shared_question_asset_and_legacy_paths_use_maintained_policy(self):
        router = load('jev_router', TEMPLATE / 'factory/jev_router.py')
        bridge = load('jev_prepared_bridge', TEMPLATE / 'factory/jev_prepared_bridge.py')
        self.assertEqual(set(router.QUESTIONS), {'worker_pair'})
        self.assertEqual(set(router.QUESTIONS['worker_pair']['criteria']), {'insufficient_information', 'uncertain'})
        self.assertFalse(router.allowed_pair('gpt-6-luna', 'ultra'))
        self.assertFalse(router.allowed_pair('gpt-6.1-sol', 'xhigh'))
        self.assertTrue(router.allowed_pair('gpt-6-luna', 'max'))
        self.assertTrue(router.allowed_pair('gpt-6.1-sol', 'low'))
        self.assertIs(bridge.choose, router.choose)
        for name in ['jev_router.py', 'jev_prepared_bridge.py']:
            adapter = (TEMPLATE / 'factory' / name).read_text()
            self.assertIn(".development(globals(), 'factory/" + name + "'", adapter)
            self.assertNotIn('def choose(', adapter)


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=WORK, prefix="ordinary harness ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        consumer.checked(["git", "init", "-q"], self.root)
        (self.root / "_bmad/custom").mkdir(parents=True)
        (self.root / "_bmad/custom/ticketing-store-config.toml").write_text('[tickets]\nstore="linear"\nproject="harness-test"\n')
        (self.root / ".factory").mkdir()
        (self.root / ".factory/project-context.md").write_text("Harness test context")
        with contextlib.redirect_stdout(io.StringIO()):
            sync(self.root)
        self.config = self.root / "harness/harness.config.json"

    def run_checks(self, cfg):
        self.config.write_text(json.dumps(cfg))
        return subprocess.run([sys.executable, "harness/ci.py"], cwd=self.root,
                              capture_output=True, text=True, timeout=30)

    def test_static_unit_only_and_provider_sentinel_never_executes(self):
        sentinel = self.root / "provider.py"
        sentinel.write_text('from pathlib import Path\nPath("PROVIDER_EXECUTED").touch()\n')
        result = self.run_checks({"static": [sys.executable, "-c", "pass"],
                    "unit": [sys.executable, "-c", "print('3 passed')"], "unit_count_pattern": r"(\d+) passed",
                    "agent": {"cmd": f'"{sys.executable}" provider.py'}, "driver": "missing"})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("UNIT_PASSED tests=3", result.stdout)
        self.assertIn("CHECKS_OK mode=ordinary", result.stdout)
        self.assertNotIn("E2E_PASSED", result.stdout)
        self.assertFalse((self.root / "PROVIDER_EXECUTED").exists())

    def test_no_checks_zero_tests_and_quoted_failure_are_red(self):
        for cfg in ({}, {"unit": [sys.executable, "-c", "print('0 passed')"], "unit_count_pattern": r"(\d+) passed"},
                    {"static": f'"{sys.executable}" -c "import no_such_factory_module"'}):
            result = self.run_checks(cfg)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("CHECKS_OK", result.stdout)

    def test_windows_pathext_resolution(self):
        if os.name != "nt":
            self.skipTest("Windows launcher fixture")
        shim = self.root / "factory-test-shim.cmd"
        shim.write_text("@echo off\r\necho SHIM_OK\r\n")
        with patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"]}):
            self.assertEqual(consumer.execute(["factory-test-shim"], self.root).stdout.strip(), "SHIM_OK")

    def test_application_helper_quoted_import_can_fail(self):
        appproc = load("appproc_test", TEMPLATE / "harness/appproc.py")
        appproc.ROOT = self.root
        cfg = {"driver": "library", "library": {"import_check":
               f'"{sys.executable}" -c "import no_such_factory_module"'}}
        with self.assertRaises(appproc.AppDidNotStart):
            with appproc.make_driver(cfg):
                pass

    def test_mutation_unique_anchor_path_escape_and_noop(self):
        runner = load("mutation_test", TEMPLATE / "harness/mutations/run.py")
        target = self.root / "app.txt"
        target.write_text("value = 1\nvalue = 1\n")
        mutation = {"file": "app.txt", "find": "value = 1", "replace": "value = 2"}
        self.assertFalse(runner.apply(self.root, mutation)[0])
        target.write_text("value = 1\n")
        self.assertTrue(runner.apply(self.root, mutation)[0])
        self.assertEqual(target.read_text(), "value = 2\n")
        self.assertFalse(runner.apply(self.root, {**mutation, "file": "../outside"})[0])
        self.assertFalse(runner.apply(self.root, {**mutation, "find": "", "replace": ""})[0])

    def test_mutation_outcomes_require_real_verdict(self):
        runner = load("mutation_verdict_test", TEMPLATE / "harness/mutations/run.py")
        for rc, log, expected in [(0, "", "INCONCLUSIVE"), (0, "CHECKS_OK mode=ordinary", "ESCAPED"),
                 (1, "GATE_FAILED: unit", "CAUGHT"), (124, "GATE_FAILED: unit", "INCONCLUSIVE"),
                 (1, "GATE_FAILED: e2e", "INCONCLUSIVE"),
                 (1, "E2E_FAIL assertion\nGATE_FAILED: e2e", "CAUGHT"),
                 (1, "could not run missing\nGATE_FAILED: unit", "INCONCLUSIVE"),
                 (1, "ModuleNotFoundError: missing\nGATE_FAILED: unit", "INCONCLUSIVE")]:
            self.assertEqual(runner.classify(rc, log).outcome, expected)

    def test_runtime_mutation_scoring_requires_bound_typed_return(self):
        runner = load("runtime_mutation_verdict_test", TEMPLATE / "harness/mutations/run.py")
        result = {"verified": False, "verdict": "failed", "candidate": "attempt-id",
                  "checkout": "", "summary": "observed failure"}
        self.assertEqual(runner.classify_runtime(result, "attempt-id").outcome, "CAUGHT")
        self.assertEqual(runner.classify_runtime({**result, "verified": True, "verdict": "verified"},
                                               "attempt-id").outcome, "ESCAPED")
        for invalid in (None, {}, {**result, "verified": True}, {**result, "candidate": "old"},
                        {**result, "summary": ""}, {**result, "verdict": "inconclusive"},
                        {"exit_code": 0, "stdout": "[PASS]"}):
            self.assertEqual(runner.classify_runtime(invalid, "attempt-id").outcome, "INCONCLUSIVE")


# Public admission and native interception run with the principal consumer checks.
from test_route_subagent import RoutingTests, WorkerAdmissionTests

if __name__ == "__main__":
    unittest.main(verbosity=2)
