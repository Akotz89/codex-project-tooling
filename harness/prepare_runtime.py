#!/usr/bin/env python3
"""Materialize a calibrated target from real Root admission; starts no jobs."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import shutil

from candidate_identity import identity
from project_policy import load_policy, validate_policy
from linear_intake import intake, read_json
from post_builder import check_view


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def prepare(root, release_path, summary_path, output, policy):
    policy = validate_policy(policy)
    release, summary = read_json(release_path), read_json(summary_path)
    check_view(release, root, policy)
    if not intake(release, summary, policy)["ready"]:
        raise ValueError("Root admission is not ready")
    baseline = identity(root)
    original = (root / "harness/linear_intake.py").read_text()
    start = '    if (task.get("status") != "ACKNOWLEDGED"'
    end = '        return {"ready": False, "reason": "task has no acknowledged matching ownership"}'
    if original.count(start) != 1 or original.count(end) != 1:
        raise ValueError("ownership mutation anchor changed; review the control")
    begin, finish = original.index(start), original.index(end)
    defective = original[:begin] + '    if False:  # deliberate control: ownership enforcement removed\n' + original[finish:]
    output.mkdir(parents=True, exist_ok=False)
    negative = output / "negative"
    (negative / "harness").mkdir(parents=True)
    for name in ("acceptance.py", "candidate_identity.py", "project_policy.py"):
        shutil.copyfile(root / "harness" / name, negative / "harness" / name)
    (negative / "harness/linear_intake.py").write_text(defective)
    defective_identity = identity(negative)
    if defective_identity == baseline:
        raise AssertionError("mutation changed no executable identity")
    write_json(output / "policy.json", policy)
    write_json(output / "release.json", release)
    write_json(output / "summary.json", summary)
    for task in summary["tasks"]:
        if task["id"] == release["task_id"]:
            task["lease"] = False
    write_json(output / "missing-lease.json", summary)

    def command(*args):
        return shlex.join([str(item) for item in args])

    def scenario(target, candidate):
        cli = target / "harness/linear_intake.py"
        positive_argv = ("python", cli, output / "release.json", output / "summary.json", "--policy", output / "policy.json")
        missing_argv = ("python", cli, output / "release.json", output / "missing-lease.json", "--policy", output / "policy.json")
        positive, missing = command(*positive_argv), command(*missing_argv)
        required_inputs = []
        for path in dict.fromkeys(item for item in (*positive_argv[2:], *missing_argv[2:])
                                  if isinstance(item, Path)):
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            required_inputs.append({"path": str(path.resolve(strict=True)), "sha256": digest})
        return {"assertions": [
            {"id": "allocated-work", "description": f"Execute {positive}. Require exit 0 and JSON ready=true with exact issue/task/dispatch/worker_thread_id from release.json. These are the real Root-captured inputs, not a fabricated allocation."},
            {"id": "missing-ownership", "description": f"Execute {missing}. Require exit 3, ready=false and matching-ownership refusal. missing-lease.json is an explicitly altered negative INPUT derived from the real snapshot, not a live graph change. Admission of this input is a product failure."},
            {"id": "acceptance-contract", "description": f"Independently inspect {target / 'harness/acceptance.py'} against the accepted work order, then exercise at least one accepted and one stale/wrong-run/unresolved-finding fixture through that exact CLI, retaining actual invocation/output. Fixtures test the real acceptance parser; they are never evidence that native workflows ran."}
        ], "environment": {"ownership": "external", "setup": "", "start": "", "teardown": "",
             "candidate_command": command("python", target / "harness/candidate_identity.py")},
             "candidate": candidate,
             "context": {"required_inputs": required_inputs, "target_root": str(target), "evidence_boundary": "Local executable integration only; no product deployment or end-user acceptance; source is shared, not a hidden holdout."}}

    write_json(output / "baseline.json", scenario(root, baseline))
    negative_scenario = scenario(negative, defective_identity)
    # This control changes intake ownership only; don't conflate another parser's availability.
    negative_scenario["assertions"] = negative_scenario["assertions"][:2]
    write_json(output / "negative.json", negative_scenario)
    manifest = {"baseline": "project-integration-baseline", "cases": [
        {"id": "project-integration-baseline", "scenario": "baseline.json", "candidate": baseline, "expected_verdict": "verified"},
        {"id": "ownership-bypass-control", "scenario": "negative.json", "candidate": defective_identity, "expected_verdict": "failed"}]}
    write_json(output / "manifest.json", manifest)
    write_json(output / "identity-control.json", {"scenario": str(output / "baseline.json"),
        "requested_candidate": "wrong-" + baseline, "actual_candidate": baseline,
        "required_verdict": "inconclusive"})
    return {"baseline": baseline, "negative": defective_identity, "manifest": str(output / "manifest.json")}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", type=Path)
    parser.add_argument("summary", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(Path(__file__).resolve().parent.parent, args.release.resolve(),
                                 args.summary.resolve(), args.output.resolve(), load_policy(args.policy)), sort_keys=True))
    except (ValueError, KeyError, TypeError, OSError, RecursionError) as error:
        parser.exit(2, f"runtime preparation held: {error}\n")
