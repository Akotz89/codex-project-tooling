"""Focused positive and tampering checks for project model policy."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "bin"))
import test_route_subagent as route_tests
import jev_prepared_bridge as bridge


class ProjectModelPolicyTests(unittest.TestCase):
    def setUp(self):
        self.routing = route_tests.WorkerAdmissionTests("runTest")
        self.routing.setUp()
        self.addCleanup(self.routing.doCleanups)

    def test_sol_only_policy_keeps_sol_medium_selectable(self):
        routing = self.routing
        settings = json.loads((routing.a / route_tests.consumer.SETTINGS).read_text())
        settings["jev_router"]["allowed_models"] = ["gpt-6.1-sol"]
        (routing.a / route_tests.consumer.SETTINGS).write_text(json.dumps(settings))
        seen = []
        code, result, _ = route_tests.RoutingTests.command(routing,
            copy.deepcopy(routing.request),
            transport=lambda payload, *_: seen.append(payload) or routing.answer(
                payload, model="gpt-6.1-sol", effort="medium"),
            work="work/policy-sol-only",
        )
        self.assertEqual(code, 0)
        self.assertEqual(result["spawn_agent"], {
            "model": "gpt-6.1-sol", "reasoning_effort": "medium", "fork_turns": "none"})
        self.assertTrue(result["receipt"]["assessment_capability_pairs"])
        self.assertTrue(all(row["model"] == "gpt-6.1-sol"
                            for row in result["receipt"]["assessment_capability_pairs"]))
        self.assertIn("gpt-6.1-sol", " ".join(seen[0]["questions"]["worker_pair"]["criteria"].values()))

    def test_multiple_allowed_models_preserve_each_and_exclude_others(self):
        routing = self.routing
        settings = json.loads((routing.a / route_tests.consumer.SETTINGS).read_text())
        settings["jev_router"]["allowed_models"] = ["gpt-6-luna", "gpt-6.1-sol"]
        (routing.a / route_tests.consumer.SETTINGS).write_text(json.dumps(settings))
        seen = []
        code, result, _ = route_tests.RoutingTests.command(routing,
            copy.deepcopy(routing.request),
            transport=lambda payload, *_: seen.append(payload) or routing.answer(
                payload, model="gpt-6.1-sol", effort="medium"),
            work="work/policy-multiple-models",
        )
        self.assertEqual(code, 0)
        models = {row["model"] for row in result["receipt"]["assessment_capability_pairs"]}
        self.assertEqual(models, {"gpt-6-luna", "gpt-6.1-sol"})
        options = " ".join(seen[0]["questions"]["worker_pair"]["criteria"].values())
        self.assertIn("gpt-6-luna", options)
        self.assertIn("gpt-6.1-sol", options)
        self.assertNotIn("gpt-5.6-sol", options)

    def test_valid_policy_with_empty_capability_intersection_refuses_before_assessment(self):
        routing = self.routing
        settings = json.loads((routing.a / route_tests.consumer.SETTINGS).read_text())
        settings["jev_router"]["allowed_models"] = ["gpt-6-luna"]
        (routing.a / route_tests.consumer.SETTINGS).write_text(json.dumps(settings))
        request = copy.deepcopy(routing.request)
        request["capability_provenance"]["models"] = [
            {"model": "gpt-6.1-sol", "reasoning_efforts": ["medium"]}]
        called = []
        with patch.object(bridge, "read_credential", side_effect=AssertionError("credential accessed")):
            code, result, _ = route_tests.RoutingTests.command(routing,
                request,
                transport=lambda *args: called.append(args) or self.fail("assessment called"),
                work="work/policy-empty-intersection",
            )
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "refused")
        self.assertNotIn("spawn_agent", result)
        self.assertNotIn("spawn_agent_request", result["receipt"])
        self.assertEqual(called, [])

    def test_native_hook_rejects_either_tampered_project_policy_copy(self):
        routing = self.routing
        for location in ("top-level", "nested-receipt"):
            with self.subTest(location=location):
                prepared, event = routing.prepared_native(
                    project_policy=["gpt-6-luna"], work="work/policy-tamper-" + location)
                tampered = copy.deepcopy(prepared)
                if location == "top-level":
                    tampered["project_model_policy"] = {"allowed_models": ["gpt-6.1-sol"]}
                else:
                    tampered["receipt"]["project_model_policy"] = {
                        "allowed_models": ["gpt-6.1-sol"]}
                Path(prepared["receipt_path"]).write_text(json.dumps(tampered))
                routing.assert_denied(routing.hook(event))


if __name__ == "__main__":
    unittest.main(verbosity=2)
