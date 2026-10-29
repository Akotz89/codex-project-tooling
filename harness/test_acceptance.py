"""Parser fixtures only: none of these documents are native run evidence."""
import copy
from test_project_policy import POLICY
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from candidate_identity import identity, bound_identity, harness_digest
from acceptance import (NATIVE_OUTCOMES, SUITE_VERDICTS,
                        IMPLEMENTATION_RED_CAUSES, REVIEW_ACTIONS)


HERE = Path(__file__).resolve().parent
CLI = HERE / "acceptance.py"


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="acceptance-fixtures-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "candidate"
        (self.root / "harness").mkdir(parents=True)
        shared_policy = getattr(self, "shared_policy", None)
        if shared_policy:
            (self.root / "harness/project-policy.json").write_text(json.dumps(shared_policy))
        else:
            for name in ("acceptance.py", "linear_intake.py", "candidate_identity.py", "project_policy.py"):
                shutil.copyfile(HERE / name, self.root / "harness" / name)
        (self.root / "fixture-target.txt").write_text("Explicit parser fixture target\n")
        for args in (["init", "-q"], ["add", "harness", "fixture-target.txt"],
                     ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                      "commit", "-qm", "Explicit parser fixture"]):
            subprocess.run(["git", "-C", str(self.root), *args], check=True,
                           capture_output=True, timeout=10)
        self.head = subprocess.check_output(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"], text=True).strip()
        self.baseline = bound_identity(self.root, shared_policy) if shared_policy else identity(self.root)
        self.negative = "b" * 64 if self.baseline != "b" * 64 else "c" * 64
        self.wrong = "d" * 64
        self.bundle = {
            "version": 1,
            "candidate": {"root": str(self.root), "git_head": self.head,
                          "executable_identity": self.baseline},
            "allocation": {"issue": "APP-42", "project_id": "sample-project",
                           "task_id": "fixture-task", "dispatch_id": "fixture-dispatch",
                           "worker": "builder", "worker_thread_id": "fixture-thread"},
            "controls": {"baseline": self.baseline, "negative": self.negative,
                         "wrong_requested_identity": self.wrong},
            "records": {},
        }
        allocation_inputs = {key: self.bundle["allocation"][key]
                             for key in ("task_id", "dispatch_id", "worker", "worker_thread_id")}
        self.make_contract()
        self.values = {
            "implementation": {"done": True, "green": True, "red_cause": "", "summary": "Fixture"},
            "review": {"ready": True, "action": "none", "findings_summary": "Fixture clean",
                       "report": {"type": "archon_artifact", "run_id": "fixture-review",
                                  "path": "review/report.md"}},
            "runtime_suite": self.suite(),
            "wrong_identity": self.runtime(self.baseline, "inconclusive"),
        }
        workflows = {purpose: (entry["workflow"], entry["node"])
                     for purpose, entry in POLICY["workflows"].items()}
        self.documents = {}
        for role, (workflow, node) in workflows.items():
            run_id = "fixture-" + role.replace("_", "-")
            if role == "review":
                run_id = "fixture-review"
            outcome = "failed" if role == "wrong_identity" else "succeeded"
            artifact_root = self.directory / "fixture-output" / "artifacts" / "runs" / run_id
            artifact_root.mkdir(parents=True)
            self.documents[role] = {
                "fixture_notice": "Explicit parser fixture; never native run evidence",
                "id": run_id, "workflow_name": workflow, "working_path": str(self.root),
                "status": "completed", "outcome": outcome,
                "checkout_baseline": {"kind": "git", "commit": self.head, "tree": self.head,
                                      "sampledAt": "2026-10-18T00:00:00Z",
                                      "worktree": {"status": "clean"}},
                "output_root": str(self.directory / "fixture-output"),
                "metadata": {"inputs": {"candidate": self.wrong}} if role == "wrong_identity" else {},
                "terminal_record": {"run_id": run_id, "status": "completed", "outcome": outcome,
                                    "error": None, "first_failed_node": None,
                                    "nodes": [{"node_id": node, "state": "completed"}],
                                    "returns": {"availability": "available", "node_id": node,
                                                "value": self.values[role]},
                                    "artifacts": {"root": str(artifact_root), "files": [], "limitations": []}},
            }
        self.documents["implementation"]["metadata"] = {"inputs_values": allocation_inputs.copy()}
        self.documents["review"]["metadata"] = {"inputs_values": allocation_inputs.copy()}
        self.documents["wrong_identity"]["metadata"]["inputs"].update(allocation_inputs)
        self.documents["runtime_suite"]["metadata"] = {
            "inputs_values": {"manifest": str(self.manifest), **allocation_inputs}}
        self.report = (self.directory / "fixture-output/artifacts/runs/fixture-review/review/report.md")
        self.report.parent.mkdir()
        self.report.write_text(f"**Reviewed head SHA:** {self.head}\n\nExplicit parser fixture: no findings.\n")
        self.bundle["review_report"] = self.pin(self.report, "fixture-review")
        self.documents["review"]["terminal_record"]["artifacts"]["files"] = [
            {"path": "review/report.md", "size": self.report.stat().st_size}]
        self.save_records()

    def make_contract(self):
        self.required = {self.baseline: ["allocated-work", "missing-ownership", "acceptance-contract"],
                         self.negative: ["allocated-work", "missing-ownership"]}
        self.input_file = self.directory / "scenario-input.fixture.json"
        self.input_file.write_text(json.dumps({"notice": "Explicit parser fixture input"}))
        self.second_input = self.directory / "second-input.fixture.json"
        self.second_input.write_text('{"notice": "Second required parser fixture input"}')
        self.scenarios = {}
        for candidate, ids in self.required.items():
            path = self.directory / (candidate + ".scenario.json")
            path.write_text(json.dumps({
                "assertions": [{"id": aid, "description": f"Explicit parser fixture using {self.input_file}"} for aid in ids],
                "environment": {"ownership": "external", "candidate_command": "fixture identity"},
                "candidate": candidate,
                "context": {"required_inputs": [self.source_pin(self.input_file),
                                                self.source_pin(self.second_input)]}}))
            self.scenarios[candidate] = path
        self.manifest = self.directory / "manifest.fixture.json"
        self.manifest.write_text(json.dumps({"baseline": "baseline", "cases": [
            {"id": name, "candidate": candidate, "scenario": self.scenarios[candidate].name,
             "expected_verdict": verdict}
            for name, candidate, verdict in [("baseline", self.baseline, "verified"),
                                              ("ownership-bypass", self.negative, "failed")]]}))
        self.bundle["runtime_contract"] = {
            "manifest": self.source_pin(self.manifest),
            "inputs": [self.source_pin(self.input_file), self.source_pin(self.second_input)],
            "scenarios": [self.source_pin(path) for path in self.scenarios.values()],
            "negative_failure": {"id": "missing-ownership", "expected": True, "observed": False,
                                 "reason": "Explicit fixture"}}

    def source_pin(self, path):
        return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def runtime(self, candidate, verdict):
        directory = self.directory / ("fixture-attempt-" + candidate + "-" + verdict)
        directory.mkdir(exist_ok=True)
        (directory / "target.txt").write_text(candidate + "\n")
        report_path = directory / "report.json"
        if verdict != "inconclusive":
            (directory / "observation.txt").write_text("Explicit parser fixture observation\n")
            report_path.write_text(json.dumps({
                "candidate": candidate,
                "assertions": [{"id": aid, "outcome": "failed" if verdict == "failed" and aid == "missing-ownership" else "passed",
                                "expected": True, "observed": verdict != "failed" or aid != "missing-ownership",
                                "reason": "Explicit fixture", "evidence_path": "observation.txt"}
                               for aid in self.required[candidate]]}))
        return {"verified": verdict == "verified", "verdict": verdict, "candidate": candidate,
                "checkout": self.head, "directory": str(directory),
                "report_path": str(report_path), "summary": "Fixture"}

    def suite(self):
        rows = []
        for ordinal, (name, candidate, verdict) in enumerate([
                ("baseline", self.baseline, "verified"), ("ownership-bypass", self.negative, "failed")]):
            rows.append({"id": name, "candidate": candidate, "scenario": str(self.scenarios[candidate]),
                         "expected_verdict": verdict, "executed": True,
                         "candidate_observed": candidate, "underlying_verdict": verdict,
                         "comparison": "matched", "expectation_met": True,
                         "runtime": self.runtime(candidate, verdict),
                         "attribution": {"run_id": "fixture-runtime-suite", "fan_out_node": "cases",
                                         "ordinal": ordinal, "return_node": "gate-verified",
                                         "agent_node": "verify-loop.verify"}})
        return {"expectations_passed": True, "verdict": "passed", "all_cases_executed": True,
                "baseline_verified": True, "baseline_verification": copy.deepcopy(rows[0]),
                "cases": rows, "summary": "Explicit fixture suite"}

    def pin(self, path, run_id):
        return {"path": str(path.relative_to(self.directory)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "run_id": run_id}

    def save_records(self):
        for role, document in self.documents.items():
            path = self.directory / (role + ".fixture.json")
            path.write_text(json.dumps(document))
            self.bundle["records"][role] = self.pin(path, document["id"])

    def run_cli(self, expected, policy=POLICY):
        path = self.directory / "bundle.fixture.json"
        path.write_text(json.dumps(self.bundle))
        policy_path = self.directory / "policy.fixture.json"
        policy_path.write_text(json.dumps(policy))
        process = subprocess.run([sys.executable, str(CLI), str(path), "--policy", str(policy_path)],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        self.assertNotIn("Traceback", process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["accepted"], expected == 0)
        if expected:
            self.assertEqual(result["action"], "hold" if expected == 2 else "correct")
        return result

    def test_accepts_four_native_shapes_and_echoes_allocation(self):
        result = self.run_cli(0)
        self.assertEqual(result["allocation"], self.bundle["allocation"])
        self.assertEqual(result["candidate"], self.bundle["candidate"])

    def test_allocation_and_each_workflow_are_bound_to_caller_policy(self):
        for field in ("project_id", "issue", "worker", "task_id", "dispatch_id", "worker_thread_id"):
            original = self.bundle["allocation"][field]
            self.bundle["allocation"][field] = "foreign"
            self.run_cli(2)
            self.bundle["allocation"][field] = original
        for purpose, record in self.documents.items():
            for target, field in ((record, "workflow_name"),
                                  (record["terminal_record"]["returns"], "node_id")):
                original = target[field]
                target[field] = "foreign"
                self.save_records()
                self.run_cli(2)
                target[field] = original
        self.save_records()
        self.run_cli(0)

    def test_alternate_caller_workflow_and_node_identities_are_supported(self):
        policy = copy.deepcopy(POLICY)
        for purpose, record in self.documents.items():
            workflow = "caller-" + purpose.replace("_", "-")
            node = "caller-node-" + purpose.replace("_", "-")
            policy["workflows"][purpose] = {"workflow": workflow, "node": node}
            record["workflow_name"] = workflow
            record["terminal_record"]["returns"]["node_id"] = node
            record["terminal_record"]["nodes"][0]["node_id"] = node
        self.save_records()
        self.run_cli(0, policy)

    def test_identity_is_executable_sha_without_bundle(self):
        process = subprocess.run([sys.executable, str(CLI), "--identity"],
                                 capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(process.stdout.strip(), hashlib.sha256(CLI.read_bytes()).hexdigest())

    def test_candidate_head_or_executable_drift_holds(self):
        for key, wrong in [("git_head", "0" * 40), ("executable_identity", "0" * 64)]:
            with self.subTest(key=key):
                original = self.bundle["candidate"][key]
                self.bundle["candidate"][key] = wrong
                self.run_cli(2)
                self.bundle["candidate"][key] = original

    def test_duplicate_run_ids_hold(self):
        doc = self.documents["wrong_identity"]
        doc["id"] = self.documents["review"]["id"]
        doc["terminal_record"]["run_id"] = doc["id"]
        self.save_records()
        self.run_cli(2)

    def test_review_findings_require_correction(self):
        self.values["review"].update(ready=False, action="correct", findings_summary="Fixture blocking defect")
        self.documents["review"]["outcome"] = "failed"
        self.documents["review"]["terminal_record"]["outcome"] = "failed"
        self.save_records()
        self.run_cli(3)

    def test_record_missing_empty_malformed_or_hash_drift_holds(self):
        path = self.directory / "implementation.fixture.json"
        original = path.read_bytes()
        for data in (b"", b"{", original + b" "):
            with self.subTest(data=data[:10]):
                path.write_bytes(data)
                self.run_cli(2)
        path.unlink()
        self.run_cli(2)

    def test_unavailable_or_truncated_return_holds(self):
        for availability in ("unavailable", "truncated"):
            with self.subTest(availability=availability):
                self.documents["implementation"]["terminal_record"]["returns"]["availability"] = availability
                self.save_records()
                self.run_cli(2)

    def test_stale_or_dirty_review_baseline_holds(self):
        baseline = self.documents["review"]["checkout_baseline"]
        baseline["commit"] = "0" * 40
        self.save_records()
        self.run_cli(2)
        baseline["commit"] = self.head
        baseline["worktree"]["status"] = "dirty"
        self.save_records()
        self.run_cli(2)

    def test_wrong_record_run_or_return_node_holds(self):
        terminal = self.documents["implementation"]["terminal_record"]
        terminal["run_id"] = "fixture-wrong-run"
        self.save_records()
        self.run_cli(2)
        terminal["run_id"] = self.documents["implementation"]["id"]
        terminal["returns"]["node_id"] = "fixture-not-implement"
        self.save_records()
        self.run_cli(2)

    def test_unavailable_runtime_case_holds(self):
        suite = self.values["runtime_suite"]
        suite.update(expectations_passed=False, verdict="inconclusive", all_cases_executed=False)
        suite["cases"][1].update(executed=False, underlying_verdict="inconclusive",
                                 comparison="unavailable", expectation_met=False)
        suite["cases"][1]["runtime"] = self.runtime(self.negative, "inconclusive")
        self.save_records()
        self.run_cli(2)

    def test_escaped_defect_holds(self):
        suite = self.values["runtime_suite"]
        suite.update(expectations_passed=False, verdict="failed")
        suite["cases"][1].update(underlying_verdict="verified", comparison="escaped", expectation_met=False)
        suite["cases"][1]["runtime"] = self.runtime(self.negative, "verified")
        self.save_records()
        self.run_cli(2)

    def test_wrong_identity_refusal_cannot_have_requested_target(self):
        self.documents["wrong_identity"]["metadata"]["inputs"]["candidate"] = self.baseline
        self.save_records()
        self.run_cli(2)

    def test_typed_native_input_takes_precedence(self):
        inputs = {key: self.bundle["allocation"][key]
                  for key in ("task_id", "dispatch_id", "worker", "worker_thread_id")}
        self.documents["wrong_identity"]["metadata"]["inputs_values"] = {
            "candidate": self.baseline, **inputs}
        self.save_records()
        self.run_cli(2)

    def test_missing_wrong_request_requires_pinned_launch_receipt(self):
        self.documents["wrong_identity"]["metadata"] = {}
        self.save_records()
        self.run_cli(2)
        receipt = self.directory / "launch.fixture.json"
        allocation_inputs = {key: self.bundle["allocation"][key]
                             for key in ("task_id", "dispatch_id", "worker", "worker_thread_id")}
        receipt.write_text(json.dumps({"run_id": "fixture-wrong-identity",
                                      "workflow_name": POLICY["workflows"]["wrong_identity"]["workflow"],
                                      "working_path": str(self.root),
                                      "inputs": {"candidate": self.wrong, **allocation_inputs}}))
        self.bundle["wrong_identity_launch_receipt"] = self.pin(receipt, "fixture-wrong-identity")
        self.run_cli(0)

    def test_missing_runtime_case_holds(self):
        self.values["runtime_suite"]["cases"].pop()
        self.save_records()
        self.run_cli(2)

    def test_success_flags_cannot_hide_wrong_runtime_identity(self):
        self.values["runtime_suite"]["cases"][1]["candidate_observed"] = self.baseline
        self.save_records()
        self.run_cli(2)

    def test_record_working_path_must_match_target(self):
        self.documents["review"]["working_path"] = str(self.directory / "alternate")
        self.save_records()
        self.run_cli(2)

    def test_implementation_measured_failure_requires_correction(self):
        self.values["implementation"].update(green=False, red_cause="introduced")
        self.documents["implementation"]["outcome"] = "failed"
        self.documents["implementation"]["terminal_record"]["outcome"] = "failed"
        self.save_records()
        self.run_cli(3)

    def test_nonempty_report_must_stamp_exact_reviewed_head(self):
        self.report.write_text("Explicit parser fixture: reviewed something else.\n")
        self.bundle["review_report"] = self.pin(self.report, "fixture-review")
        self.run_cli(2)

    def fail_baseline(self):
        suite = self.values["runtime_suite"]
        suite.update(expectations_passed=False, verdict="failed", baseline_verified=False)
        row = suite["cases"][0]
        row.update(underlying_verdict="failed", comparison="unexpected_failure", expectation_met=False)
        row["runtime"] = self.runtime(self.baseline, "failed")
        suite["baseline_verification"] = copy.deepcopy(row)
        self.documents["runtime_suite"]["outcome"] = "failed"
        self.documents["runtime_suite"]["terminal_record"]["outcome"] = "failed"
        self.save_records()

    def test_baseline_measured_failure_requires_correction(self):
        self.fail_baseline()
        result = self.run_cli(3)
        self.assertIn("runtime_suite", json.dumps(result))

    def test_every_role_rejects_lifecycle_and_terminal_disagreement(self):
        original = copy.deepcopy(self.documents)
        for role in self.documents:
            for field, value in (("status", "failed"), ("run_id", "fixture-other"), ("outcome", None)):
                with self.subTest(role=role, field=field):
                    self.documents = copy.deepcopy(original)
                    self.documents[role]["terminal_record"][field] = value
                    self.save_records()
                    self.run_cli(2)
            self.documents = copy.deepcopy(original)
            self.documents[role]["status"] = "running"
            self.save_records()
            self.run_cli(2)
        self.documents = original

    def test_review_artifact_missing_empty_hash_drift_or_wrong_run_holds(self):
        original = self.report.read_bytes()
        for data in (b"", original + b"changed"):
            with self.subTest(data=data[:10]):
                self.report.write_bytes(data)
                self.run_cli(2)
        self.report.unlink()
        self.run_cli(2)
        self.report.write_bytes(original)
        self.values["review"]["report"]["run_id"] = "fixture-unrelated-review"
        self.save_records()
        self.run_cli(2)

    def test_nonobject_native_structures_hold_without_tracebacks(self):
        original = copy.deepcopy(self.documents)
        for field in ("terminal_record", "checkout_baseline", "metadata"):
            role = "wrong_identity" if field == "metadata" else "review"
            for wrong in (None, [], "fixture malformed object"):
                with self.subTest(field=field, wrong=wrong):
                    self.documents = copy.deepcopy(original)
                    self.documents[role][field] = wrong
                    self.save_records()
                    self.run_cli(2)
        self.documents = original

    def test_wrong_identity_actual_outcome_or_verdict_cannot_be_green(self):
        original = copy.deepcopy(self.documents["wrong_identity"])
        for field, value in (("candidate", self.negative), ("verified", True), ("verdict", "verified")):
            with self.subTest(field=field):
                self.documents["wrong_identity"] = copy.deepcopy(original)
                self.documents["wrong_identity"]["terminal_record"]["returns"]["value"][field] = value
                self.save_records()
                self.run_cli(2)
        self.documents["wrong_identity"] = copy.deepcopy(original)
        self.documents["wrong_identity"]["outcome"] = "succeeded"
        self.documents["wrong_identity"]["terminal_record"]["outcome"] = "succeeded"
        self.save_records()
        self.run_cli(2)

    def test_malformed_or_oversized_bundle_holds(self):
        path = self.directory / "malformed-bundle.fixture.json"
        for raw in ("null", "[]", '{"version":1,"version":1}', '{"version":NaN}',
                    json.dumps({"fixture_padding": "x" * (1024 * 1024)})):
            with self.subTest(size=len(raw)):
                path.write_text(raw)
                policy_path = self.directory / "policy.fixture.json"
                policy_path.write_text(json.dumps(POLICY))
                process = subprocess.run([sys.executable, str(CLI), str(path), "--policy", str(policy_path)],
                                         capture_output=True, text=True, timeout=10)
                self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
                self.assertEqual(json.loads(process.stdout)["action"], "hold")
                self.assertNotIn("Traceback", process.stderr)

    def test_acceptance_preserves_source_and_evidence_bytes(self):
        paths = [self.directory / pin["path"] for pin in self.bundle["records"].values()]
        paths += [self.report, self.root / "harness/acceptance.py", self.root / "harness/linear_intake.py"]
        before = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        self.run_cli(0)
        self.assertEqual(before, {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})

    def test_dirty_target_files_cannot_claim_clean_reviewed_head(self):
        (self.root / "fixture-target.txt").write_text("Explicit parser fixture dirty target\n")
        self.assertEqual(identity(self.root), self.baseline)
        self.run_cli(2)

    def test_selected_terminal_node_must_have_completed(self):
        for state in ("running", "failed"):
            with self.subTest(state=state):
                self.documents["implementation"]["terminal_record"]["nodes"][0]["state"] = state
                self.save_records()
                self.run_cli(2)

    def test_nested_runtime_cannot_contradict_case_measurement(self):
        original = copy.deepcopy(self.documents["runtime_suite"])
        for field, value in (("verified", False), ("candidate", self.negative)):
            with self.subTest(field=field):
                self.documents["runtime_suite"] = copy.deepcopy(original)
                self.documents["runtime_suite"]["terminal_record"]["returns"]["value"]["cases"][0]["runtime"][field] = value
                self.save_records()
                self.run_cli(2)

    def test_missing_runtime_report_or_assertion_evidence_holds(self):
        runtime = self.values["runtime_suite"]["cases"][0]["runtime"]
        report = Path(runtime["report_path"])
        original = report.read_bytes()
        report.unlink()
        self.run_cli(2)
        report.write_bytes(original)
        (Path(runtime["directory"]) / "observation.txt").unlink()
        self.run_cli(2)

    def test_wrong_runtime_probe_holds(self):
        runtime = self.values["runtime_suite"]["cases"][0]["runtime"]
        (Path(runtime["directory"]) / "target.txt").write_text(self.negative)
        self.run_cli(2)

    def test_review_symlink_escape_holds(self):
        outside = self.directory / "outside-report.fixture.md"
        outside.write_bytes(self.report.read_bytes())
        self.report.unlink()
        self.report.symlink_to(outside)
        self.run_cli(2)

    def test_certified_review_run_directory_escape_holds(self):
        self.run_cli(0)
        run_root = self.report.parent.parent
        outside = self.directory / "outside-certified-run.fixture"
        run_root.rename(outside)
        run_root.symlink_to(outside, target_is_directory=True)
        result = self.run_cli(2)
        self.assertIn("symlink in certified artifact path", result["feedback"][-1]["reason"])

    def test_runtime_contained_links_match_native_and_escapes_hold(self):
        check_hash = "2d42982260dd84110deed4b89d5ee8609f443f0b9b649d1ab327ffce0ee10c46"
        for index, row in enumerate(self.values["runtime_suite"]["cases"]):
            path, data = self.report_data(index)
            env = {"INPUTS_START_OK": "true", "INPUTS_IDENTITY_OK": "true",
                   "INPUTS_DIRECTORY": row["runtime"]["directory"],
                   "INPUTS_EXPECTED_CANDIDATE": row["candidate"], "INPUTS_REPORT_PATH": str(path),
                   "INPUTS_REQUIRED_IDS": json.dumps(self.required[row["candidate"]])}
            directory = path.parent
            stored = directory / "stored"
            stored.mkdir()
            for filename in ("target.txt", "observation.txt", path.name):
                source = directory / filename
                source.rename(stored / filename)
                source.symlink_to(stored / filename)
            self.assertEqual(self.native_script("verify-runtime/scripts/check-evidence.py", check_hash, env)["status"],
                             row["underlying_verdict"])
            self.run_cli(0)
            observation = directory / "observation.txt"
            observation.unlink()
            for target, contents in ((self.directory / "escaped.fixture", "outside"),
                                     (stored / "empty.fixture", ""), (stored / "missing.fixture", None)):
                if contents is not None:
                    target.write_text(contents)
                observation.symlink_to(target)
                self.assertEqual(self.native_script("verify-runtime/scripts/check-evidence.py", check_hash, env)["status"],
                                 "malformed")
                self.run_cli(2)
                observation.unlink()
            observation.symlink_to(stored / "observation.txt")
        probe = Path(self.values["wrong_identity"]["directory"]) / "target.txt"
        stored = probe.with_name("stored-target.txt")
        probe.rename(stored)
        probe.symlink_to(stored)
        self.run_cli(0)

    def test_required_inputs_exact_membership_and_declarations(self):
        original_pins = copy.deepcopy(self.bundle["runtime_contract"]["inputs"])
        unrelated = self.source_pin(self.report)
        for pins in ([original_pins[0]], [unrelated], [*original_pins, unrelated],
                     [original_pins[0], unrelated]):
            with self.subTest(pins=pins):
                self.bundle["runtime_contract"]["inputs"] = pins
                self.run_cli(2)
        self.bundle["runtime_contract"]["inputs"] = original_pins
        self.second_input.unlink()
        self.bundle["runtime_contract"]["inputs"] = [original_pins[0], unrelated]
        self.run_cli(2)
        self.second_input.write_text('{"notice": "Second required parser fixture input"}')
        self.bundle["runtime_contract"]["inputs"] = original_pins
        scenario = self.scenarios[self.baseline]
        original = json.loads(scenario.read_text())
        declarations = original["context"]["required_inputs"]
        for invalid in (None, [], [*declarations, declarations[0]],
                        [dict(declarations[0], sha256="0" * 64), declarations[1]],
                        [dict(declarations[0], path=str(self.directory / "absent"))]):
            data = copy.deepcopy(original)
            if invalid is None:
                del data["context"]["required_inputs"]
            else:
                data["context"]["required_inputs"] = invalid
            scenario.write_text(json.dumps(data))
            self.bundle["runtime_contract"]["scenarios"][0] = self.source_pin(scenario)
            self.run_cli(2)
        scenario.write_text(json.dumps(original))
        self.bundle["runtime_contract"]["scenarios"][0] = self.source_pin(scenario)
        self.run_cli(0)

    def report_data(self, index):
        path = Path(self.values["runtime_suite"]["cases"][index]["runtime"]["report_path"])
        return path, json.loads(path.read_text())

    def test_done_false_alone_requires_builder_correction(self):
        self.values["implementation"]["done"] = False
        self.save_records()
        result = self.run_cli(3)
        self.assertEqual([item["record"] for item in result["feedback"]], ["implementation"])

    def test_missing_or_foreign_assertions_hold_in_each_case(self):
        for index in (0, 1):
            path, original = self.report_data(index)
            for mutation in ("missing", "foreign", "extra", "duplicate"):
                with self.subTest(index=index, mutation=mutation):
                    data = copy.deepcopy(original)
                    if mutation == "missing":
                        data["assertions"].pop()
                    elif mutation == "foreign":
                        data["assertions"][0]["id"] = "unrelated-smoke"
                    else:
                        data["assertions"].append(copy.deepcopy(data["assertions"][0]))
                        if mutation == "extra":
                            data["assertions"][-1]["id"] = "extra-smoke"
                    path.write_text(json.dumps(data))
                    self.run_cli(2)
            path.write_text(json.dumps(original))

    def test_substituted_case_scenario_and_assertions_hold(self):
        suite = self.values["runtime_suite"]
        for index, row in enumerate(suite["cases"]):
            row.update(id=f"unrelated-{index}", scenario="unrelated-smoke.json")
            path, data = self.report_data(index)
            for assertion in data["assertions"]:
                assertion.update(id="unrelated-smoke", reason="Ownership was never exercised")
            path.write_text(json.dumps(data))
        suite["baseline_verification"] = copy.deepcopy(suite["cases"][0])
        self.save_records()
        self.run_cli(2)

    def test_frozen_contract_pins_and_native_manifest_input_are_required(self):
        original = copy.deepcopy(self.bundle)
        del self.bundle["runtime_contract"]
        self.run_cli(2)
        self.bundle = copy.deepcopy(original)
        for pin in [self.bundle["runtime_contract"]["manifest"],
                    *self.bundle["runtime_contract"]["scenarios"],
                    *self.bundle["runtime_contract"]["inputs"]]:
            path = Path(pin["path"])
            before = path.read_bytes()
            path.write_bytes(before + b" ")
            self.run_cli(2)
            path.write_bytes(before)
        self.documents["runtime_suite"]["metadata"]["inputs_values"]["manifest"] = str(self.report)
        self.save_records()
        self.run_cli(2)

    def test_frozen_input_pins_cannot_be_omitted_or_duplicated(self):
        original = self.bundle["runtime_contract"]["inputs"]
        for pins in ([], [*original, *original]):
            self.bundle["runtime_contract"]["inputs"] = pins
            self.run_cli(2)
        self.bundle["runtime_contract"]["inputs"] = original
        self.run_cli(0)

    def test_native_suite_input_receipt_fallback_is_run_bound(self):
        self.documents["runtime_suite"]["metadata"] = {}
        self.save_records()
        self.run_cli(2)
        receipt = self.directory / "suite-launch.fixture.json"
        allocation_inputs = {key: self.bundle["allocation"][key]
                             for key in ("task_id", "dispatch_id", "worker", "worker_thread_id")}
        data = {"run_id": "fixture-runtime-suite", "workflow_name": POLICY["workflows"]["runtime_suite"]["workflow"],
                "working_path": str(self.root),
                "inputs": {"manifest": str(self.manifest), **allocation_inputs}}
        receipt.write_text(json.dumps(data))
        self.bundle["runtime_suite_launch_receipt"] = self.pin(receipt, data["run_id"])
        self.run_cli(0)
        data["inputs"]["worker_thread_id"] = "another-thread"
        receipt.write_text(json.dumps(data))
        self.bundle["runtime_suite_launch_receipt"] = self.pin(receipt, data["run_id"])
        self.run_cli(2)
        data["inputs"]["worker_thread_id"] = allocation_inputs["worker_thread_id"]
        data["run_id"] = "another-run"
        receipt.write_text(json.dumps(data))
        self.bundle["runtime_suite_launch_receipt"] = self.pin(receipt, data["run_id"])
        self.run_cli(2)

    def test_unrelated_failed_assertion_or_failure_assessment_holds(self):
        path, original = self.report_data(1)
        for mutation in ("other-failure", "expected", "observed", "reason"):
            with self.subTest(mutation=mutation):
                data = copy.deepcopy(original)
                if mutation == "other-failure":
                    data["assertions"][0]["outcome"] = "failed"
                    data["assertions"][1]["outcome"] = "passed"
                else:
                    data["assertions"][1][mutation] = "Unrelated failure assessment"
                path.write_text(json.dumps(data))
                self.run_cli(2)
        path.write_text(json.dumps(original))

    def test_native_literal_strings_remain_valid_in_both_cases(self):
        for index in (0, 1):
            for field in ("id", "reason", "evidence_path"):
                with self.subTest(index=index, field=field):
                    path, data = self.report_data(index)
                    original = copy.deepcopy(data)
                    assertion = data["assertions"][0]
                    before = assertion[field]
                    assertion[field] = " " + before + " "
                    scenario = self.scenarios[self.values["runtime_suite"]["cases"][index]["candidate"]]
                    scenario_bytes = scenario.read_bytes()
                    if field == "id":
                        source = json.loads(scenario_bytes)
                        source["assertions"][0]["id"] = assertion[field]
                        scenario.write_text(json.dumps(source))
                        self.bundle["runtime_contract"]["scenarios"][index] = self.source_pin(scenario)
                    elif field == "evidence_path":
                        (path.parent / assertion[field]).write_text("Literal filename fixture")
                    path.write_text(json.dumps(data))
                    self.run_cli(0)
                    path.write_text(json.dumps(original))
                    scenario.write_bytes(scenario_bytes)
                    self.bundle["runtime_contract"]["scenarios"][index] = self.source_pin(scenario)

    def test_nonblank_reason_and_literal_path_still_required(self):
        path, original = self.report_data(0)
        for field in ("id", "reason", "evidence_path"):
            for invalid in ("", " \n", None, 7):
                with self.subTest(field=field, invalid=invalid):
                    data = copy.deepcopy(original)
                    data["assertions"][0][field] = invalid
                    path.write_text(json.dumps(data))
                    self.run_cli(2)
        path.write_text(json.dumps(original))

    def native_source(self, relative, expected_hash):
        revision = json.loads((HERE.parent / "template/factory/pack.json").read_text())["integration_revision_required"]
        source = os.environ.get("ARCHON_SOURCE")
        self.assertTrue(source, "ARCHON_SOURCE must name the installed pinned source for conformance")
        observed = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True).strip()
        self.assertEqual(observed, revision)
        path = Path(source) / ".archon/workflows/sdlc" / relative
        self.assertTrue(path.is_file(), "Installed pinned Archon source required for native conformance")
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected_hash)
        return path

    def native_script(self, relative, expected_hash, inputs):
        path = self.native_source(relative, expected_hash)
        result = subprocess.run([sys.executable, str(path)],
                                env={**os.environ, **inputs}, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_pinned_native_workflow_selection_contract(self):
        owners = {
            "implementation": ("implement", "17810bc52ce79726fa51d19cc9382d772a550cd7a197f0632f683f7b9c3c2aec"),
            "review": ("review", "e387f5855fe9ea4ce10b1b06b86688ed65b4367fbd3d7f2e7517bd943654cc2c"),
            "runtime_suite": ("verify-runtime-suite", "65189bc5c02d232470d9373c6ff840155c6fe24153081aa61c83ceadc9654df5"),
            "wrong_identity": ("verify-runtime", "ed716e0a233a384f54a06bad07cd34bbe6da382e100c5e1c5d88f3e1122dcb39"),
        }
        for role, (folder, digest) in owners.items():
            workflow, node = {"implementation": ("archon-implement", "implement"),
                              "review": ("archon-review", "publish"),
                              "runtime_suite": ("archon-verify-runtime-suite", "compare"),
                              "wrong_identity": ("archon-verify-runtime", "gate-verified")}[role]
            path = self.native_source(f"{folder}/archon-{folder}.yaml", digest)
            declarations = dict(line.split(": ", 1) for line in path.read_text().splitlines()
                                if line.startswith(("name: ", "returns: ")))
            self.assertEqual((declarations["name"], declarations["returns"]), (workflow, node))

    def test_complete_native_enum_domains_agree(self):
        owners = [
            (NATIVE_OUTCOMES, "../../../packages/workflows/src/schemas/workflow-run.ts",
             "70033e373ed4173e3b6f028ee929d11bbdf108f618787fd164de76e5913e53d2",
             r"workflowRunOutcomeSchema = z\.enum\(\[([^\]]+)\]\)"),
            (SUITE_VERDICTS, "verify-runtime-suite/archon-verify-runtime-suite.yaml",
             "65189bc5c02d232470d9373c6ff840155c6fe24153081aa61c83ceadc9654df5",
             r"verdict: \{ type: string, enum: \[([^\]]+)\]"),
            (IMPLEMENTATION_RED_CAUSES, "implement/archon-implement.yaml",
             "17810bc52ce79726fa51d19cc9382d772a550cd7a197f0632f683f7b9c3c2aec",
             r"red_cause:\s+type: string\s+enum: \[([^\]]+)\]"),
            (REVIEW_ACTIONS, "review/archon-review.yaml",
             "e387f5855fe9ea4ce10b1b06b86688ed65b4367fbd3d7f2e7517bd943654cc2c",
             r"action:\s+type: string\s+enum: \[([^\]]+)\]"),
        ]
        for local, owner, digest, pattern in owners:
            with self.subTest(owner=owner):
                source = self.native_source(owner, digest).read_text()
                matches = re.findall(pattern, source)
                self.assertEqual(len(matches), 1)
                domain = {token.strip().strip("\"'") for token in matches[0].split(",")}
                self.assertEqual(set(local), domain)
                self.assertEqual(len(local), len(domain))

    def test_prepared_dependencies_follow_command_arguments(self):
        import test_runtime_preparation
        from prepare_runtime import prepare

        fixture = test_runtime_preparation.RuntimePreparationTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        prepare(fixture.root, fixture.release, fixture.summary, fixture.output, POLICY)
        expected = [self.source_pin(fixture.output / filename)
                    for filename in ("release.json", "summary.json", "policy.json", "missing-lease.json")]
        for filename, target in (("baseline.json", fixture.root),
                                 ("negative.json", fixture.output / "negative")):
            scenario = json.loads((fixture.output / filename).read_text())
            self.assertEqual(scenario["context"]["required_inputs"], expected)
            for index, snapshot in enumerate(("summary.json", "missing-lease.json")):
                argv = ["python", str(target / "harness/linear_intake.py"),
                        str(fixture.output / "release.json"), str(fixture.output / snapshot),
                        "--policy", str(fixture.output / "policy.json")]
                self.assertIn(shlex.join(argv), scenario["assertions"][index]["description"])
            native = self.native_script("verify-runtime/scripts/read-scenario.py",
                                       "788353f076f5dd73544088200e1f0996d142eb93583bbaa31478ec22b7c161de",
                                       {"INPUTS_SCENARIO": str(fixture.output / filename)})
            self.assertEqual(native["assertion_ids"], [item["id"] for item in scenario["assertions"]])

    def test_pinned_native_scenario_checker_and_compare_conformance(self):
        read_hash = "788353f076f5dd73544088200e1f0996d142eb93583bbaa31478ec22b7c161de"
        check_hash = "2d42982260dd84110deed4b89d5ee8609f443f0b9b649d1ab327ffce0ee10c46"
        compare_hash = "9416a46448a386ae87d1ef2b949cc4777e047dcec6dee4a031c538e09333fbb1"
        rows = self.values["runtime_suite"]["cases"]
        for index, row in enumerate(rows):
            scenario_path = Path(row["scenario"])
            scenario_data = json.loads(scenario_path.read_text())
            scenario_data["assertions"][0]["id"] = " allocated-work "
            scenario_path.write_text(json.dumps(scenario_data))
            self.required[row["candidate"]][0] = " allocated-work "
            self.bundle["runtime_contract"]["scenarios"][index] = self.source_pin(scenario_path)
            path, data = self.report_data(index)
            data["assertions"][0]["id"] = " allocated-work "
            path.write_text(json.dumps(data))
            scenario = self.native_script("verify-runtime/scripts/read-scenario.py", read_hash,
                                          {"INPUTS_SCENARIO": row["scenario"]})
            self.assertEqual(scenario["assertion_ids"], self.required[row["candidate"]])
            path, original = self.report_data(index)
            env = {"INPUTS_START_OK": "true", "INPUTS_IDENTITY_OK": "true",
                   "INPUTS_DIRECTORY": row["runtime"]["directory"],
                   "INPUTS_EXPECTED_CANDIDATE": row["candidate"], "INPUTS_REPORT_PATH": str(path),
                   "INPUTS_REQUIRED_IDS": json.dumps(scenario["assertion_ids"])}
            check = lambda: self.native_script("verify-runtime/scripts/check-evidence.py", check_hash, env)
            self.assertEqual(check()["status"], row["underlying_verdict"])
            for field in ("reason", "evidence_path"):
                data = copy.deepcopy(original)
                entry = data["assertions"][0]
                entry[field] = " " + entry[field] + " "
                if field == "evidence_path":
                    (path.parent / entry[field]).write_text("Literal native conformance fixture")
                path.write_text(json.dumps(data))
                self.assertEqual(check()["status"], row["underlying_verdict"])
                self.run_cli(0)
            for mutation in ("missing", "foreign"):
                data = copy.deepcopy(original)
                if mutation == "missing":
                    data["assertions"].pop()
                else:
                    data["assertions"][0]["id"] = "foreign"
                path.write_text(json.dumps(data))
                self.assertEqual(check()["status"], "malformed")
                self.run_cli(2)
            path.write_text(json.dumps(original))
        manifest = json.loads(self.manifest.read_text())
        for case in manifest["cases"]:
            case["scenario"] = str(self.directory / case["scenario"])
        native = self.native_script("verify-runtime-suite/scripts/compare.py", compare_hash,
                                   {"INPUTS_MANIFEST": json.dumps(manifest),
                                    "INPUTS_RESULTS": json.dumps([row["runtime"] for row in rows]),
                                    "WORKFLOW_ID": "fixture-runtime-suite"})
        self.assertEqual(native, self.values["runtime_suite"] | {"summary": native["summary"]})
        self.documents["runtime_suite"]["terminal_record"]["returns"]["value"] = native
        self.save_records()
        self.run_cli(0)

    def test_frozen_cases_and_scenario_identity_cannot_be_substituted(self):
        original = copy.deepcopy(self.values["runtime_suite"])
        for field, wrong in (("id", "unrelated"), ("scenario", "unrelated.json"),
                             ("candidate", self.negative), ("expected_verdict", "failed")):
            with self.subTest(field=field):
                self.values["runtime_suite"] = copy.deepcopy(original)
                self.documents["runtime_suite"]["terminal_record"]["returns"]["value"] = self.values["runtime_suite"]
                self.values["runtime_suite"]["cases"][0][field] = wrong
                self.values["runtime_suite"]["baseline_verification"] = copy.deepcopy(self.values["runtime_suite"]["cases"][0])
                self.save_records()
                self.run_cli(2)
        self.values["runtime_suite"] = original
        self.documents["runtime_suite"]["terminal_record"]["returns"]["value"] = original
        scenario = self.scenarios[self.baseline]
        data = json.loads(scenario.read_text())
        data["candidate"] = self.negative
        scenario.write_text(json.dumps(data))
        self.bundle["runtime_contract"]["scenarios"][0] = self.source_pin(scenario)
        self.save_records()
        self.run_cli(2)

    def test_success_flags_cannot_forge_complete_case_count(self):
        original = copy.deepcopy(self.values["runtime_suite"]["cases"])
        for rows in (original[:1], [*original, copy.deepcopy(original[0])],
                     [original[0], copy.deepcopy(original[0])]):
            self.values["runtime_suite"]["cases"] = rows
            self.save_records()
            self.run_cli(2)

    def test_duplicate_manifest_cases_hold(self):
        data = json.loads(self.manifest.read_text())
        data["cases"][1]["id"] = data["cases"][0]["id"]
        self.manifest.write_text(json.dumps(data))
        self.bundle["runtime_contract"]["manifest"] = self.source_pin(self.manifest)
        self.run_cli(2)

    def test_legitimate_native_extra_metadata_is_preserved(self):
        self.documents["runtime_suite"]["metadata"]["additional_native_metadata"] = {"source": "fixture"}
        for row in self.values["runtime_suite"]["cases"]:
            row["runtime"]["additional_native_metadata"] = {"trace": [1, 2]}
        self.values["runtime_suite"]["baseline_verification"] = copy.deepcopy(self.values["runtime_suite"]["cases"][0])
        self.save_records()
        result = self.run_cli(0)
        self.assertEqual(result["records"], self.bundle["records"])
        self.assertEqual(result["runtime_contract"], self.bundle["runtime_contract"])

    def test_frozen_scenario_pins_must_be_exact_and_unique(self):
        pins = self.bundle["runtime_contract"]["scenarios"]
        original = copy.deepcopy(pins)
        for wrong in ([], pins[:1], [pins[0], pins[0]], [*pins, pins[0]]):
            self.bundle["runtime_contract"]["scenarios"] = wrong
            self.run_cli(2)
        self.bundle["runtime_contract"]["scenarios"] = original
        self.run_cli(0)

    def test_ownership_assessment_keeps_literal_reason_and_json_types(self):
        path, data = self.report_data(1)
        assertion = data["assertions"][1]
        assertion["reason"] = " Explicit ownership assessment\n"
        self.bundle["runtime_contract"]["negative_failure"]["reason"] = assertion["reason"]
        path.write_text(json.dumps(data))
        self.run_cli(0)
        assertion["observed"] = 0
        path.write_text(json.dumps(data))
        self.run_cli(2)

    def test_measured_failures_survive_aggregate_and_later_record_holds(self):
        self.values["implementation"]["done"] = False
        self.fail_baseline()
        original = copy.deepcopy(self.documents)
        for role, mutate in (("runtime_suite", "aggregate"), ("wrong_identity", "lifecycle")):
            with self.subTest(role=role):
                self.documents = copy.deepcopy(original)
                if mutate == "aggregate":
                    self.documents[role]["terminal_record"]["returns"]["value"]["expectations_passed"] = True
                else:
                    self.documents[role]["status"] = "running"
                self.save_records()
                result = self.run_cli(2)
                self.assertEqual({entry["record"] for entry in result["feedback"][:-1]},
                                 {"implementation", "runtime_suite"})

    def test_measured_failures_survive_later_hold(self):
        self.values["implementation"]["done"] = False
        self.values["review"].update(ready=False, action="correct", findings_summary="Fixture blocker")
        self.fail_baseline()
        self.save_records()
        result = self.run_cli(3)
        expected = copy.deepcopy(result["feedback"])
        wrong_probe = Path(self.values["wrong_identity"]["directory"]) / "target.txt"
        wrong_probe.unlink()
        result = self.run_cli(2)
        self.assertEqual(result["feedback"][:-1], expected)
        self.assertEqual({entry["record"] for entry in expected},
                         {"implementation", "review", "runtime_suite"})
        self.assertIn("target.txt", result["feedback"][-1]["reason"])


class SharedAcceptanceTests(unittest.TestCase):
    def test_acceptance_binds_shared_harness_and_policy_without_project_copies(self):
        fixture = AcceptanceTests()
        fixture.shared_policy = {**POLICY, "shared_harness": True,
                                 "shared_harness_sha256": harness_digest(HERE.parent)}
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.assertFalse((fixture.root / "harness/acceptance.py").exists())
        fixture.run_cli(0, policy=fixture.shared_policy)
        changed = {**fixture.shared_policy, "require_writer_paths": True}
        fixture.run_cli(2, policy=changed)


if __name__ == "__main__":
    unittest.main()
