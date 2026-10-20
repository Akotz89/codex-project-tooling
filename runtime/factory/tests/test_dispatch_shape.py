from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = PROJECT_ROOT
TASK_WORK = ROOT / "work" / "factory-tests"


def setUpModule():
    TASK_WORK.mkdir(parents=True, exist_ok=True)


sys.path.insert(0, str(ROOT / "factory"))
import consumer  # noqa: E402
import jev_prepared_bridge as bridge  # noqa: E402
import jev_router  # noqa: E402


class DispatchArgsTests(unittest.TestCase):
    def test_generated_config_is_translated_and_literal_tail_is_untouched(self):
        args, requested, generated = jev_router.strip_opt_in([
            "archon-implement", "--jev-route", "--jev-generated-config", "work/run/config.json",
            "--no-worktree", "--", "--config", "literal worker text",
        ])
        self.assertTrue(requested)
        self.assertEqual(generated, "work/run/config.json")
        self.assertEqual(args, [
            "archon-implement", "--config", generated, "--no-worktree",
            "--", "--config", "literal worker text",
        ])
        resolved_args, resolved = jev_router.resolve_generated_config(args, generated, ROOT)
        self.assertEqual(resolved, str((ROOT / generated).resolve()))
        self.assertEqual(resolved_args[2], resolved)
        self.assertFalse(jev_router.explicit_choice(resolved_args, resolved))

    def test_human_config_and_explicit_model_choice_remain_explicit(self):
        self.assertTrue(jev_router.explicit_choice(["archon-implement", "--config", "human.json"]))
        self.assertTrue(jev_router.explicit_choice(
            ["archon-implement", "--config", "/run/generated.json", "--model", "gpt-6-luna"],
            "/run/generated.json",
        ))
        self.assertTrue(jev_router.explicit_choice([
            "archon-implement", "--config", "human.json", "--", "--model", "literal"
        ]))

    def test_generated_config_option_requires_unambiguous_opt_in(self):
        invalid = [
            ["archon-implement", "--jev-generated-config", "config.json"],
            ["archon-implement", "--jev-route", "--jev-generated-config"],
            ["archon-implement", "--jev-route", "--jev-generated-config", "a.json",
             "--jev-generated-config=b.json"],
            ["archon-implement", "--jev-route", "--config", "human.json",
             "--jev-generated-config", "generated.json"],
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                jev_router.strip_opt_in(args)
        # The option after `--` is user message text, not wrapper syntax.
        args, requested, generated = jev_router.strip_opt_in([
            "archon-implement", "--jev-route", "--", "--jev-generated-config", "literal"
        ])
        self.assertTrue(requested)
        self.assertIsNone(generated)
        self.assertEqual(args[-2:], ["--jev-generated-config", "literal"])

    def test_configured_bun_directory_is_added_once_to_runtime_path(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-path-", dir=TASK_WORK) as scratch:
            bindir = Path(scratch) / "bin"
            bindir.mkdir()
            bun = bindir / "bun"
            bun.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            bun.chmod(0o755)
            original = {"PATH": "/usr/bin:/bin", "KEEP": "yes"}
            result = consumer.with_runtime_path(original, {"bun": str(bun)})
            self.assertEqual(result["PATH"].split(os.pathsep).count(str(bindir)), 1)
            self.assertEqual(result["PATH"].split(os.pathsep)[0], str(bindir))
            self.assertEqual(original["PATH"], "/usr/bin:/bin")
            self.assertEqual(result["KEEP"], "yes")
            self.assertEqual(consumer.with_runtime_path(result, {"bun": str(bun)}), result)

    def test_configured_bun_precedes_ambient_even_when_already_later_in_path(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-path-order-", dir=TASK_WORK) as scratch:
            root = Path(scratch)
            ambient, configured = root / "ambient", root / "configured"
            ambient.mkdir(); configured.mkdir()
            for directory in (ambient, configured):
                executable = directory / "bun"
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o755)
            alias = root / "configured-alias"
            alias.symlink_to(configured, target_is_directory=True)
            path = os.pathsep.join([str(ambient), str(configured), str(configured / ".." / "configured"), str(alias), "/bin"])
            original = {"PATH": path, "KEEP": "yes"}
            result = consumer.with_runtime_path(original, {"bun": str(configured / "bun")})
            self.assertEqual(result["PATH"].split(os.pathsep), [str(configured), str(ambient), "/bin"])
            self.assertEqual(consumer.shutil.which("bun", path=result["PATH"]), str(configured / "bun"))
            self.assertEqual(consumer.with_runtime_path(result, {"bun": str(configured / "bun")}), result)
            self.assertEqual(original["PATH"], path)
            self.assertEqual(result["KEEP"], "yes")

    def test_invoke_passes_generated_provenance_and_bun_path_but_not_for_human_config(self):
        with tempfile.TemporaryDirectory(prefix="dispatch-invoke-", dir=TASK_WORK) as scratch:
            root = Path(scratch)
            source = root / "source"
            (source / "packages/cli/src/commands").mkdir(parents=True)
            (source / "packages/cli/src/commands/jev-preflight.ts").write_text("", encoding="utf-8")
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")
            bindir = root / "runtime-bin"
            bindir.mkdir()
            bun = bindir / "bun"
            bun.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            bun.chmod(0o755)
            settings = {"bun": str(bun), "jev_router": {}}
            captured = []

            def execute(argv, cwd, **kwargs):
                captured.append((argv, kwargs.get("env", {})))
                return subprocess.CompletedProcess(argv, 0, "", "")

            with patch.dict(os.environ, {"PATH": "/usr/bin", "ARCHON_JEV_GENERATED_CONFIG": "stale"}, clear=True), \
                    patch.object(consumer, "shared_root", return_value=root), \
                    patch.object(consumer, "read_settings", return_value=settings), \
                    patch.object(consumer, "verify_source", return_value=source), \
                    patch.object(consumer, "discover", return_value={"archon-implement": source}), \
                    patch.object(consumer, "validate"), \
                    patch.object(consumer, "execute", side_effect=execute):
                consumer.invoke(root, "run", [
                    "archon-implement", "--jev-route", "--jev-generated-config", str(config), "--", "task"
                ])
            argv, env = captured[-1]
            self.assertIn("--config", argv)
            self.assertIn(str(config), argv)
            self.assertEqual(env["ARCHON_JEV_GENERATED_CONFIG"], str(config.resolve()))
            self.assertIn(str(bindir), env["PATH"].split(os.pathsep))
            self.assertIn("ARCHON_JEV_BRIDGE", env)

            captured.clear()
            with patch.dict(os.environ, {"PATH": "/usr/bin", "ARCHON_JEV_GENERATED_CONFIG": "stale"}, clear=True), \
                    patch.object(consumer, "shared_root", return_value=root), \
                    patch.object(consumer, "read_settings", return_value=settings), \
                    patch.object(consumer, "verify_source", return_value=source), \
                    patch.object(consumer, "discover", return_value={"archon-implement": source}), \
                    patch.object(consumer, "validate"), \
                    patch.object(consumer, "execute", side_effect=execute):
                consumer.invoke(root, "run", [
                    "archon-implement", "--jev-route", "--config", str(config), "--", "task"
                ])
            _, env = captured[-1]
            self.assertNotIn("ARCHON_JEV_BRIDGE", env)
            self.assertNotIn("ARCHON_JEV_GENERATED_CONFIG", env)


class TypedAssessmentBridgeTests(unittest.TestCase):
    def capability_provenance(self):
        return {
            "source": "current_runtime_tool_schema",
            "tool": "collaboration.spawn_agent",
            "models": [
                {"model": "gpt-6-luna", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
                {"model": "gpt-6.1-sol", "reasoning_efforts": ["low", "medium", "high", "xhigh", "ultra"]},
            ],
        }

    def answer(self, confidence=1.0):
        values = {
            "task_kind": "known_pattern_implementation",
            "reasoning_demand": "bounded_local",
            "verification_difficulty": "integration_checks",
            "material_consequence": "bounded_reversible",
        }
        return {"answers": {
            name: {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
                "probabilities": {
                    option: 1.0 if option == choice else 0.0
                    for option in question["criteria"]
                },
            }
            for name, choice in values.items()
            for question in [jev_router.QUESTIONS[name]]
        }}

    def test_valid_typed_assessment_is_consumed_and_returns_prepared_selection(self):
        request = {
            "task": "bounded offline test task",
            "ordinary": {"provider": "codex", "model": "gpt-6-luna", "effort": "low"},
            "capability_provenance": self.capability_provenance(),
            "remaining_ms": 4000,
        }
        old_reader = bridge.read_credential
        bridge.read_credential = lambda *_: "offline-test-token"
        try:
            with tempfile.TemporaryDirectory(prefix="dispatch-bridge-", dir=TASK_WORK) as root:
                seen = []
                result = bridge.route(
                    request,
                    root,
                    transport=lambda payload, _credential, _deadline: seen.append(payload) or self.answer(),
                )
                self.assertEqual(result["status"], "selected")
                self.assertEqual((result["model"], result["effort"]), ("gpt-6-luna", "medium"))
                self.assertEqual(result["receipt"]["status"], "choice_proposed")
                self.assertEqual(result["receipt"]["schema"], "CPI236TASKASSESSMENTV1")
                self.assertEqual(result["spawn_agent"], {
                    "model": "gpt-6-luna", "reasoning_effort": "medium", "fork_turns": "none"
                })
                self.assertEqual(result["dispatch_receipt"]["status"], "selected")
                self.assertEqual(result["dispatch_receipt"]["tool"], "collaboration.spawn_agent")
                self.assertEqual(result["dispatch_receipt"]["dispatch"], "not_invoked")
                self.assertEqual(result["receipt"]["capability_provenance"], "caller_supplied_tool_metadata")
                self.assertEqual(result["receipt"]["capability_tool"], "collaboration.spawn_agent")
                self.assertEqual({(row["model"], row["effort"]) for row in seen[0]["state"]["allowed_catalog"]}, {
                    ("gpt-6-luna", effort) for effort in ["low", "medium", "high", "xhigh", "max"]
                } | {
                    ("gpt-6.1-sol", effort) for effort in ["low", "medium", "high"]
                })
                self.assertNotIn("same_account", seen[0]["state"])
                self.assertNotIn("identity", seen[0]["state"])
        finally:
            bridge.read_credential = old_reader

    def test_uncertain_typed_assessment_returns_ordinary_fallback_receipt(self):
        request = {
            "task": "bounded offline test task",
            "ordinary": {"provider": "codex", "model": "gpt-6-luna", "effort": "medium"},
            "capability_provenance": self.capability_provenance(),
            "remaining_ms": 4000,
        }
        old_reader = bridge.read_credential
        bridge.read_credential = lambda *_: "offline-test-token"
        try:
            with tempfile.TemporaryDirectory(prefix="dispatch-fallback-", dir=TASK_WORK) as root:
                result = bridge.route(
                    request,
                    root,
                    transport=lambda payload, _credential, _deadline: self.answer(confidence=0.6),
                )
                self.assertEqual(result["status"], "fallback")
                self.assertEqual(result["receipt"]["status"], "fallback")
                self.assertEqual(result["receipt"]["reason"], "assessment_uncertain")
                self.assertEqual(result["receipt"]["ordinary_worker"], request["ordinary"])
                self.assertEqual(result["spawn_agent"], {
                    "model": "gpt-6-luna", "reasoning_effort": "medium", "fork_turns": "none"
                })
                self.assertEqual(result["dispatch_receipt"]["status"], "ordinary_fallback")
                self.assertEqual(result["dispatch_receipt"]["reason"], "assessment_uncertain")
        finally:
            bridge.read_credential = old_reader

    def test_missing_jev_credential_emits_validated_ordinary_spawn_fallback(self):
        request = {
            "task": "bounded offline test task",
            "ordinary": {"provider": "codex", "model": "gpt-6-luna", "effort": "medium"},
            "capability_provenance": self.capability_provenance(),
            "remaining_ms": 4000,
        }
        old_reader = bridge.read_credential
        bridge.read_credential = lambda *_: None
        try:
            with tempfile.TemporaryDirectory(prefix="dispatch-missing-credential-", dir=TASK_WORK) as root:
                result = bridge.route(request, root, transport=lambda *_: self.fail("transport must not run"))
            self.assertEqual(result["status"], "fallback")
            self.assertEqual(result["receipt"]["reason"], "unavailable")
            self.assertEqual(result["spawn_agent"], {
                "model": "gpt-6-luna", "reasoning_effort": "medium", "fork_turns": "none"
            })
            self.assertEqual(result["dispatch_receipt"]["status"], "ordinary_fallback")
        finally:
            bridge.read_credential = old_reader

    def test_forbidden_ordinary_pair_is_refused_without_spawn_request(self):
        request = {
            "task": "bounded offline test task",
            "ordinary": {"provider": "codex", "model": "gpt-5.6-sol", "effort": "medium"},
            "capability_provenance": self.capability_provenance(),
            "remaining_ms": 4000,
        }
        with tempfile.TemporaryDirectory(prefix="dispatch-forbidden-", dir=TASK_WORK) as root:
            result = bridge.route(request, root, transport=lambda *_: self.answer())
        self.assertEqual(result["status"], "refused")
        self.assertNotIn("spawn_agent", result)
        self.assertEqual(result["receipt"]["reason"], "worker_model_policy")

    def test_spawn_arguments_require_actual_tool_provenance_and_model_effort_policy(self):
        capability_pairs = [
            {"model": "gpt-6-luna", "effort": effort}
            for effort in ["low", "medium", "high", "xhigh", "max"]
        ]
        capability_pairs += [
            {"model": "gpt-6.1-sol", "effort": effort}
            for effort in ["low", "medium", "high"]
        ]
        receipt = {
            "capability_provenance": "caller_supplied_tool_metadata",
            "capability_tool": "collaboration.spawn_agent",
            "spawn_capability_pairs": capability_pairs,
        }
        self.assertEqual(bridge.spawn_agent_request({"model": "gpt-6-luna", "effort": "xhigh"}, receipt), {
            "model": "gpt-6-luna", "reasoning_effort": "xhigh", "fork_turns": "none"
        })
        self.assertEqual(bridge.spawn_agent_request({"model": "gpt-6-luna", "effort": "max"}, receipt)["reasoning_effort"], "max")
        self.assertIsNone(bridge.spawn_agent_request({"model": "gpt-6.1-sol", "effort": "xhigh"}, receipt))
        self.assertIsNone(bridge.spawn_agent_request({"model": "gpt-6-luna", "effort": "ultra"}, receipt))
        self.assertIsNone(bridge.spawn_agent_request(
            {"model": "gpt-6-luna", "effort": "medium"}, {"catalog_pairs": capability_pairs}
        ))

    def test_explicit_choice_bypasses_jev_but_must_match_runtime_tool_capability(self):
        request = {
            "explicit_choice": {"model": "gpt-6-luna", "effort": "max"},
            "capability_provenance": self.capability_provenance(),
        }
        old_reader = bridge.read_credential
        bridge.read_credential = lambda *_: self.fail("explicit choice must not read Jev credentials")
        try:
            with tempfile.TemporaryDirectory(prefix="dispatch-explicit-", dir=TASK_WORK) as root:
                result = bridge.route(request, root, transport=lambda *_: self.fail("explicit choice must not call Jev"))
        finally:
            bridge.read_credential = old_reader
        self.assertEqual(result["status"], "explicit")
        self.assertEqual(result["spawn_agent"]["reasoning_effort"], "max")
        self.assertEqual(result["dispatch_receipt"]["dispatch"], "not_invoked")

        request["explicit_choice"] = {"model": "gpt-6.1-sol", "effort": "xhigh"}
        with tempfile.TemporaryDirectory(prefix="dispatch-explicit-refused-", dir=TASK_WORK) as root:
            refused = bridge.route(request, root)
        self.assertEqual(refused["status"], "refused")
        self.assertNotIn("spawn_agent", refused)

    def test_wrong_tool_provenance_falls_back_without_args(self):
        request = {
            "task": "bounded offline test task",
            "ordinary": {"provider": "codex", "model": "gpt-6-luna", "effort": "medium"},
            "capability_provenance": {"tool": "not-the-runtime-tool", "models": [
                {"model": "gpt-6-luna", "reasoning_efforts": ["medium"]}
            ]},
            "remaining_ms": 4000,
        }
        old_reader = bridge.read_credential
        bridge.read_credential = lambda *_: "offline-test-token"
        try:
            with tempfile.TemporaryDirectory(prefix="dispatch-capability-fallback-", dir=TASK_WORK) as root:
                result = bridge.route(request, root, transport=lambda *_: self.fail("invalid provenance must not invoke Jev"))
            self.assertEqual(result["status"], "fallback")
            self.assertNotIn("spawn_agent", result)
        finally:
            bridge.read_credential = old_reader

    def test_legacy_native_catalog_keeps_selection_but_never_impersonates_tool_metadata(self):
        import time
        request = {
            "task": "bounded offline native preflight task",
            "ordinary": {"provider": "codex", "model": "gpt-6-luna", "effort": "low"},
            "catalog": {
                "same_account": True,
                "identity": "offline-test-catalog",
                "fetched_at": time.time(),
                "models": [{"model": "gpt-6-luna", "supportedReasoningEfforts": ["low", "medium"]}],
            },
            "remaining_ms": 4000,
        }
        old_reader = bridge.read_credential
        bridge.read_credential = lambda *_: "offline-test-token"
        try:
            with tempfile.TemporaryDirectory(prefix="dispatch-legacy-catalog-", dir=TASK_WORK) as root:
                result = bridge.route(request, root, transport=lambda *_: self.answer())
                self.assertEqual(result["status"], "selected")
                self.assertEqual((result["model"], result["effort"]), ("gpt-6-luna", "medium"))
                self.assertNotIn("spawn_agent", result)

                for catalog in [
                    {**request["catalog"], "same_account": False},
                    {**request["catalog"], "fetched_at": time.time() - 61},
                ]:
                    invalid = {**request, "catalog": catalog}
                    refused = bridge.route(invalid, root, transport=lambda *_: self.fail("invalid catalog must not reach Jev"))
                    self.assertEqual(refused["status"], "fallback")
                    self.assertNotIn("spawn_agent", refused)
        finally:
            bridge.read_credential = old_reader

    def test_capability_source_must_be_current_runtime_schema_before_jev(self):
        for source in (None, "stale_cache"):
            with self.subTest(source=source):
                capability = self.capability_provenance()
                if source is None:
                    capability.pop("source")
                else:
                    capability["source"] = source
                request = {
                    "task": "bounded offline test task",
                    "ordinary": {"provider": "codex", "model": "gpt-6-luna", "effort": "medium"},
                    "capability_provenance": capability,
                    "remaining_ms": 4000,
                }
                old_reader = bridge.read_credential
                bridge.read_credential = lambda *_: "offline-test-token"
                try:
                    with tempfile.TemporaryDirectory(prefix="dispatch-stale-capability-", dir=TASK_WORK) as root:
                        result = bridge.route(
                            request, root,
                            transport=lambda *_: self.fail("non-current capability must not invoke Jev"),
                        )
                    self.assertEqual(result["status"], "fallback")
                    self.assertNotIn("spawn_agent", result)
                finally:
                    bridge.read_credential = old_reader


if __name__ == "__main__":
    unittest.main()
