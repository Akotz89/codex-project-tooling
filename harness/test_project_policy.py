"""Neutral caller policy fixtures and validation controls."""
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from project_policy import load_policy, validate_policy


POLICY = {
    "project_id": "sample-project",
    "worker_roles": ["builder", "reviewer"],
    "linear_workspace": "sample-team",
    "acceptance_issue": "APP-42",
    "workflows": {
        "implementation": {"workflow": "sample-build", "node": "build-result"},
        "review": {"workflow": "sample-review", "node": "review-result"},
        "runtime_suite": {"workflow": "sample-runtime-suite", "node": "suite-result"},
        "wrong_identity": {"workflow": "sample-runtime", "node": "runtime-result"},
    },
}


class PolicyTests(unittest.TestCase):
    def test_valid_policy_preserves_caller_values(self):
        self.assertEqual(validate_policy(copy.deepcopy(POLICY)), POLICY)

    def test_missing_malformed_and_ambiguous_policy_refused(self):
        invalid = [None, [], {}, {**POLICY, "extra": True}, {**POLICY, "require_writer_paths": "yes"},
                   {**POLICY, "shared_harness": "yes"}, {**POLICY, "shared_harness": True},
                   {**POLICY, "shared_harness": True, "shared_harness_sha256": 5}]
        invalid += [{key: value for key, value in POLICY.items() if key != missing}
                    for missing in POLICY]
        for field, values in {
                "project_id": ["", " x ", None],
                "worker_roles": [[], ["builder", "builder"], [None], ["../worker"], ["Worker"],
                                 ["a" * 65], "builder"],
                "linear_workspace": ["", "../team", "team/issue"],
                "acceptance_issue": ["", "APP", "APP-42/other"],
                "workflows": [{}, [], {**POLICY["workflows"], "extra": {}}]}.items():
            invalid.extend({**POLICY, field: value} for value in values)
        for purpose in POLICY["workflows"]:
            for entry in ({}, {"workflow": "x"}, {"workflow": "x", "node": " "},
                          {"workflow": "x", "node": "n", "extra": True}):
                invalid.append({**POLICY, "workflows": {**POLICY["workflows"], purpose: entry}})
        duplicate = copy.deepcopy(POLICY)
        duplicate["workflows"]["review"]["workflow"] = duplicate["workflows"]["implementation"]["workflow"]
        invalid.append(duplicate)
        for policy in invalid:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                validate_policy(policy)

    def test_file_loader_rejects_duplicate_keys_nonfinite_and_oversized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(POLICY))
            self.assertEqual(load_policy(path), POLICY)
            for body in ('{"project_id":"a","project_id":"b"}', '{"project_id":NaN}',
                         ' ' * (1024 * 1024 + 1), '{'):
                path.write_text(body)
                with self.assertRaises(ValueError):
                    load_policy(path)

    def test_public_apis_require_and_validate_policy_before_other_inputs(self):
        from acceptance import accept
        from linear_intake import intake
        from post_builder import check_view, output_view
        from prepare_runtime import prepare
        calls = [(intake, ({}, {})), (accept, ({}, Path("."))),
                 (check_view, ({}, Path("."))),
                 (output_view, (None, None, None, "run")),
                 (prepare, (None, None, None, None))]
        for function, args in calls:
            with self.subTest(api=function.__name__):
                with self.assertRaises(TypeError):
                    function(*args)
                for policy in (None, {}, {**POLICY, "project_id": ""}):
                    with self.assertRaises(ValueError):
                        function(*args, policy)

    def test_clis_refuse_missing_and_malformed_policy(self):
        here = Path(__file__).resolve().parent
        commands = [("linear_intake.py", ["release.json", "summary.json"]),
                    ("acceptance.py", ["bundle.json"]),
                    ("post_builder.py", ["release.json", "builder.json", "summary.json", "--expected-run-id", "run"]),
                    ("prepare_runtime.py", ["release.json", "summary.json", "controls"])]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text("{}")
            for script, args in commands:
                for flags in ([], ["--policy", str(path)]):
                    with self.subTest(script=script, flags=flags):
                        result = subprocess.run([sys.executable, str(here / script), *args, *flags],
                                                cwd=directory, capture_output=True, text=True, timeout=10)
                        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                        self.assertNotIn("Traceback", result.stdout + result.stderr)
                        self.assertIn("policy", result.stdout + result.stderr)
            self.assertFalse((Path(directory) / "controls").exists())
