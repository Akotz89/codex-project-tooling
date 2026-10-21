#!/usr/bin/env python3
"""Validate retained native factory evidence without dispatching or publishing."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from candidate_identity import identity


WORKFLOWS = {
    "implementation": ("archon-implement", "implement"),
    "review": ("archon-review", "publish"),
    "runtime_suite": ("archon-verify-runtime-suite", "compare"),
    "wrong_identity": ("archon-verify-runtime", "gate-verified"),
}
NATIVE_OUTCOMES = ("succeeded", "failed")
SUITE_VERDICTS = ("passed", "failed", "inconclusive")
IMPLEMENTATION_RED_CAUSES = ("", "introduced", "inherited", "environment", "incomplete")
REVIEW_ACTIONS = ("none", "correct", "replan")
JSON_LIMIT = 1024 * 1024
REPORT_LIMIT = 4 * JSON_LIMIT


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    require(isinstance(value, str) and bool(value) and value == value.strip()
            and "\0" not in value, "expected a nonempty canonical string")
    return value


def native_text(value):
    require(isinstance(value, str) and bool(value.strip()), "expected a nonblank native string")
    return value


def obj(value):
    require(isinstance(value, dict), "expected a JSON object")
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def read_file(path, limit, expected_hash=None):
    with path.open("rb") as stream:
        require(stream.seek(0, 2) <= limit, f"file exceeds {limit} bytes: {path}")
        stream.seek(0)
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
        require(expected_hash is None or actual == expected_hash, f"SHA-256 mismatch: {path}")
        stream.seek(0)
        data = stream.read(limit + 1)
    require(0 < len(data) <= limit, f"empty or oversized file: {path}")
    require(hashlib.sha256(data).hexdigest() == actual, f"file changed while reading: {path}")
    return data


def read_json(path, expected_hash=None):
    return obj(json.loads(read_file(path, JSON_LIMIT, expected_hash),
                          object_pairs_hook=unique_object, parse_constant=invalid_constant))


def source_path(pin, directory):
    obj(pin)
    path = (directory / text(pin["path"])).resolve(strict=True)
    require(path.is_file(), f"not a retained file: {path}")
    require(re.fullmatch(r"[0-9a-f]{64}", text(pin["sha256"])) is not None,
            "invalid SHA-256 pin")
    return path


def pinned(pin, directory):
    text(obj(pin)["run_id"])
    return source_path(pin, directory)


def absolute(value):
    path = Path(text(value))
    require(path.is_absolute(), "an absolute path is required")
    return path


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, timeout=15)
    require(result.returncode == 0, "candidate Git inspection failed")
    return result.stdout.strip()


def native_record(name, pin, directory, root):
    path = pinned(pin, directory)
    run = read_json(path, pin["sha256"])
    workflow, node = WORKFLOWS[name]
    require(run["id"] == pin["run_id"], f"{name}: wrong run ID")
    require(run["workflow_name"] == workflow, f"{name}: wrong workflow")
    require(absolute(run["working_path"]).resolve(strict=True) == root,
            f"{name}: stale working path")
    require(run["status"] == "completed", f"{name}: lifecycle is not completed")
    require(run["outcome"] in NATIVE_OUTCOMES, f"{name}: missing authored outcome")
    terminal = obj(run["terminal_record"])
    require(terminal["run_id"] == run["id"] and terminal["status"] == run["status"]
            and terminal["outcome"] == run["outcome"], f"{name}: terminal record disagrees")
    selected = obj(terminal["returns"])
    require(selected["availability"] == "available" and selected["node_id"] == node,
            f"{name}: selected structured return unavailable or wrong node")
    nodes = terminal["nodes"]
    require(isinstance(nodes, list), f"{name}: missing terminal nodes")
    selected_nodes = [obj(entry) for entry in nodes if obj(entry).get("node_id") == node]
    require(len(selected_nodes) == 1 and selected_nodes[0].get("state") == "completed",
            f"{name}: selected terminal node is not completed")
    return run, obj(selected["value"])


def certified_report(run, value, pin, directory, head):
    pointer = obj(value["report"])
    require(pointer.get("type") == "archon_artifact" and pointer.get("run_id") == run["id"]
            and pointer.get("path") == "review/report.md", "review: wrong certified report pointer")
    require(pin["run_id"] == run["id"], "review: retained report belongs to another run")
    run_id = text(run["id"])
    require(Path(run_id).name == run_id and run_id not in (".", ".."), "unsafe run ID")
    output = absolute(run["output_root"])
    artifact_root = output / "artifacts" / "runs" / run_id
    declared_root = absolute(obj(run["terminal_record"]["artifacts"])["root"])
    require(declared_root == artifact_root, "review: artifact root disagrees with output root")
    report = artifact_root / pointer["path"]
    require(all(not parent.is_symlink() for parent in (report, *report.parents)),
            "review: symlink in certified artifact path")
    require(report.resolve(strict=True).is_relative_to(artifact_root.resolve(strict=True)),
            "review: certified report escaped artifact root")
    retained = pinned(pin, directory)
    require(retained == report.resolve(strict=True), "review: retained report is not certified artifact")
    body = read_file(report, REPORT_LIMIT, pin["sha256"]).decode("utf-8")
    require(body.strip(), "review: empty report")
    stamps = re.findall(r"^\s*(?:\*\*)?Reviewed head SHA:(?:\*\*)?\s*`?([0-9a-f]+)`?\s*$",
                        body, flags=re.MULTILINE)
    require(stamps and all(stamp == head for stamp in stamps), "review: missing or stale head stamp")


def boolean_fields(value, fields, label):
    for field in fields:
        require(type(value[field]) is bool, f"{label}: {field} must be boolean")


def contained_file(directory, raw):
    path = (directory / native_text(raw)).resolve(strict=True)
    require(path.is_relative_to(directory) and path.is_file() and path.stat().st_size > 0,
            "runtime: missing, empty or escaped evidence")
    return path


def file_identity(path):
    with path.open("rb") as stream:
        sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": str(path), "sha256": sha256}


def runtime_evidence(value, required_ids=None, negative_failure=None, refusal=False):
    directory = absolute(value["directory"])
    require(directory.resolve(strict=True) == directory, "runtime: noncanonical attempt directory")
    probe = contained_file(directory, "target.txt")
    require(read_file(probe, JSON_LIMIT).decode("utf-8").strip() == value["candidate"],
            "runtime: target probe disagrees with selected return")
    evidence = [file_identity(probe)]
    if refusal:
        # Native check-evidence returns identity mismatch before reading the report.
        return evidence
    report_path = contained_file(directory, text(value["report_path"]))
    report = read_json(report_path)
    require(isinstance(report["candidate"], str) and report["candidate"].strip() == value["candidate"],
            "runtime: report candidate disagrees")
    assertions = report["assertions"]
    require(isinstance(assertions, list) and bool(assertions), "runtime: missing assertions")
    seen = set()
    outcomes = []
    for raw in assertions:
        assertion = obj(raw)
        key = native_text(assertion["id"])
        require(key in required_ids, "runtime: foreign assertion ID")
        require(key not in seen, "runtime: duplicate assertion ID")
        seen.add(key)
        require(assertion["outcome"] in ("passed", "failed") and "expected" in assertion
                and "observed" in assertion, "runtime: unavailable or malformed assertion")
        native_text(assertion["reason"])
        evidence.append(file_identity(contained_file(directory, assertion["evidence_path"])))
        outcomes.append(assertion["outcome"])
    require(seen == set(required_ids), "runtime: missing required assertion coverage")
    if negative_failure is not None:
        failed = [entry for entry in assertions if entry["outcome"] == "failed"]
        require(len(failed) == 1 and all(json.dumps(failed[0].get(key), sort_keys=True, allow_nan=False)
                                          == json.dumps(negative_failure[key], sort_keys=True, allow_nan=False)
                                      for key in ("id", "expected", "observed", "reason")),
                "runtime: deliberate fault did not fail the Root-calibrated ownership assessment")
    require(value["verdict"] == ("failed" if "failed" in outcomes else "verified"),
            "runtime: report assertions disagree with selected verdict")
    evidence.append(file_identity(report_path))
    return evidence


def native_inputs(run, bundle, directory, root, role, field):
    metadata = obj(run.get("metadata", {}))
    inputs = obj(metadata.get("inputs_values", metadata.get("inputs", {})))
    if field in inputs:
        return inputs, "native metadata inputs"
    pin = bundle.get(role + "_launch_receipt")
    require(pin is not None, f"{role}: native requested input absent; Root-pinned launch receipt required")
    receipt = read_json(pinned(pin, directory), pin["sha256"])
    require(pin["run_id"] == receipt["run_id"] == run["id"]
            and receipt["workflow_name"] == run["workflow_name"]
            and absolute(receipt["working_path"]).resolve(strict=True) == root,
            f"{role}: launch receipt binding disagrees")
    return obj(receipt["inputs"]), "Root-pinned launch receipt (integrity only)"


def frozen_contract(bundle, directory, run, root):
    contract = obj(bundle["runtime_contract"])
    pin = obj(contract["manifest"])
    manifest_path = source_path(pin, directory)
    manifest = read_json(manifest_path, pin["sha256"])
    inputs, input_evidence = native_inputs(run, bundle, directory, root, "runtime_suite", "manifest")
    require((root / text(inputs["manifest"])).resolve(strict=True) == manifest_path,
            "runtime_suite: native manifest input differs from frozen manifest")
    input_pins = contract["inputs"]
    require(isinstance(input_pins, list) and bool(input_pins), "runtime_suite: missing frozen scenario inputs")
    input_paths = {}
    for source in input_pins:
        path = source_path(source, directory)
        require(path not in input_paths, "runtime_suite: duplicate frozen scenario input")
        input_paths[path] = source["sha256"]
        read_file(path, JSON_LIMIT, source["sha256"])
    cases = manifest["cases"]
    require(isinstance(cases, list) and len(cases) == 2, "runtime_suite: frozen manifest needs two controls")
    scenario_pins = contract["scenarios"]
    require(isinstance(scenario_pins, list) and len(scenario_pins) == len(cases),
            "runtime_suite: missing or extra frozen scenario pins")
    scenarios = {}
    for source in scenario_pins:
        path = source_path(source, directory)
        require(path not in scenarios, "runtime_suite: duplicate frozen scenario pin")
        scenarios[path] = read_json(path, source["sha256"])
    required_ids = {}
    required_inputs = {}
    seen = set()
    for raw in cases:
        case = obj(raw)
        key = text(case["id"])
        require(key not in seen, "runtime_suite: duplicate frozen case")
        seen.add(key)
        text(case["candidate"])
        path = (manifest_path.parent / text(case["scenario"])).resolve(strict=True)
        require(path in scenarios, "runtime_suite: case has no frozen scenario pin")
        scenario = scenarios[path]
        declarations = obj(scenario["context"])["required_inputs"]
        require(isinstance(declarations, list) and bool(declarations),
                "runtime_suite: missing required scenario inputs")
        declared_paths = set()
        for source in declarations:
            absolute(obj(source)["path"])
            dependency = source_path(source, path.parent)
            require(dependency not in declared_paths, "runtime_suite: duplicate required scenario input")
            declared_paths.add(dependency)
            read_file(dependency, JSON_LIMIT, source["sha256"])
            require(dependency not in required_inputs or required_inputs[dependency] == source["sha256"],
                    "runtime_suite: conflicting required scenario input")
            required_inputs[dependency] = source["sha256"]
        require(scenario["candidate"] == case["candidate"], "runtime_suite: scenario candidate drift")
        environment = obj(scenario["environment"])
        require(environment["ownership"] == "external", "runtime_suite: scenario ownership drift")
        native_text(environment["candidate_command"])
        assertions = scenario["assertions"]
        require(isinstance(assertions, list) and bool(assertions), "runtime_suite: missing scenario assertions")
        ids = [native_text(obj(entry)["id"]) for entry in assertions]
        require(len(set(ids)) == len(ids), "runtime_suite: duplicate scenario assertion ID")
        required_ids[key] = ids
        case["scenario"] = str(path)
    require({Path(case["scenario"]) for case in cases} == set(scenarios),
            "runtime_suite: substituted scenario pins")
    require(input_paths == required_inputs, "runtime_suite: frozen input pins differ from required scenario inputs")
    controls = bundle["controls"]
    baseline = [case for case in cases if case["id"] == manifest["baseline"]]
    require(len(baseline) == 1 and baseline[0]["candidate"] == controls["baseline"]
            and baseline[0]["expected_verdict"] == "verified", "runtime_suite: frozen baseline drift")
    negative = [case for case in cases if case["id"] != manifest["baseline"]]
    require(len(negative) == 1 and negative[0]["candidate"] == controls["negative"]
            and negative[0]["expected_verdict"] == "failed", "runtime_suite: frozen negative control drift")
    failure = obj(contract["negative_failure"])
    require(native_text(failure["id"]) in required_ids[negative[0]["id"]]
            and "expected" in failure and "observed" in failure,
            "runtime_suite: missing Root-calibrated ownership assertion")
    native_text(failure["reason"])
    return cases, required_ids, failure, input_evidence


def runtime_suite(run, value, bundle, directory, root, failures):
    cases, required_ids, negative_failure, input_evidence = frozen_contract(bundle, directory, run, root)
    controls = bundle["controls"]
    boolean_fields(value, ("expectations_passed", "all_cases_executed", "baseline_verified"), "runtime_suite")
    require(value["verdict"] in SUITE_VERDICTS, "runtime_suite: invalid verdict")
    rows = value["cases"]
    require(isinstance(rows, list) and len(rows) == 2, "runtime_suite: missing or extra cases")
    seen = set()
    evidence = []
    for index, raw in enumerate(rows):
        row = obj(raw)
        case_id = text(row["id"])
        require(case_id not in seen, "runtime_suite: duplicate case")
        seen.add(case_id)
        expected_case = cases[index]
        require(all(row.get(key) == expected_case[key]
                    for key in ("id", "candidate", "scenario", "expected_verdict")),
                "runtime_suite: case differs from frozen manifest")
        expected = expected_case["expected_verdict"]
        boolean_fields(row, ("executed", "expectation_met"), "runtime_suite case")
        actual = obj(row["runtime"])
        boolean_fields(actual, ("verified",), "runtime_suite runtime")
        require(row["executed"] is True and row["comparison"] not in ("unavailable", "wrong_identity", "escaped")
                and actual.get("archon_failed") is not True, f"runtime_suite: unavailable, wrong identity or escaped case {case_id}")
        require(row["candidate_observed"] == row["candidate"] == actual["candidate"],
                f"runtime_suite: wrong observed identity {case_id}")
        verdict = row["underlying_verdict"]
        require(verdict in ("verified", "failed") and actual["verdict"] == verdict
                and actual["verified"] is (verdict == "verified"), "runtime_suite: inconsistent runtime verdict")
        evidence.extend(runtime_evidence(actual, required_ids[case_id],
                                       negative_failure if expected == "failed" else None))
        if verdict == "failed" and expected == "verified":
            failures.append({"record": "runtime_suite", "case": case_id, "reason": "baseline runtime failed"})
        matched = verdict == expected
        require(row["expectation_met"] is matched and row["comparison"] == ("matched" if matched else "unexpected_failure"),
                "runtime_suite: inconsistent comparison")
        attribution = obj(row["attribution"])
        require(attribution == {"run_id": run["id"], "fan_out_node": "cases", "ordinal": index,
                                "return_node": "gate-verified", "agent_node": "verify-loop.verify"},
                "runtime_suite: wrong case attribution")
    baseline = next(row for row in rows if row["candidate"] == controls["baseline"])
    require(value["baseline_verification"] == baseline, "runtime_suite: contradictory baseline row")
    passed = all(row["expectation_met"] for row in rows)
    require(value["expectations_passed"] is passed and value["all_cases_executed"] is True
            and value["baseline_verified"] is baseline["expectation_met"]
            and value["verdict"] == ("passed" if passed else "failed")
            and run["outcome"] == ("succeeded" if passed else "failed"),
            "runtime_suite: contradictory aggregate")
    return evidence, input_evidence


def requested_identity(run, bundle, directory, root):
    inputs, evidence = native_inputs(run, bundle, directory, root, "wrong_identity", "candidate")
    return text(inputs["candidate"]), evidence


def accept(bundle, directory, failures=None):
    if failures is None:
        failures = []
    obj(bundle)
    require(type(bundle["version"]) is int and bundle["version"] == 1, "unsupported bundle version")
    candidate = obj(bundle["candidate"])
    root = absolute(candidate["root"]).resolve(strict=True)
    require(root.is_dir() and git(root, "rev-parse", "--show-toplevel") == str(root), "candidate is not repository root")
    head = text(candidate["git_head"])
    require(git(root, "rev-parse", "HEAD") == head, "candidate HEAD has drifted")
    require(not git(root, "status", "--porcelain", "--untracked-files=normal"),
            "candidate checkout is dirty; review cannot certify current files")
    executable = identity(root)
    require(candidate["executable_identity"] == executable, "candidate executable identity has drifted")
    allocation = obj(bundle["allocation"])
    require(allocation["issue"] == "CPI-402" and allocation["project_id"] == "P-CPI-10", "wrong allocation project or issue")
    for key in ("task_id", "dispatch_id", "worker", "worker_thread_id"):
        text(allocation[key])
    controls = obj(bundle["controls"])
    require(text(controls["baseline"]) == executable and text(controls["negative"]) != executable
            and text(controls["wrong_requested_identity"]) != executable, "invalid frozen control identities")
    require(re.fullmatch(r"[0-9a-f]{64}", controls["negative"]) is not None,
            "deliberate defect control must identify an exact executable")
    pins = obj(bundle["records"])
    require(set(pins) == set(WORKFLOWS), "exactly four native records required")
    require(len({text(obj(pin)["run_id"]) for pin in pins.values()}) == 4, "native run IDs must be distinct")
    implementation, result = native_record("implementation", pins["implementation"], directory, root)
    boolean_fields(result, ("done", "green"), "implementation")
    require(result["red_cause"] in IMPLEMENTATION_RED_CAUSES, "implementation: invalid red cause")
    if (implementation["outcome"] != "succeeded" or result["done"] is not True
            or result["green"] is not True or result["red_cause"] != ""):
        failures.append({"record": "implementation", "reason": "builder did not author a completed green result",
                         "red_cause": result["red_cause"], "summary": str(result.get("summary", ""))[:2000]})
    review, result = native_record("review", pins["review"], directory, root)
    baseline = obj(review["checkout_baseline"])
    require(baseline["kind"] == "git" and baseline["commit"] == head
            and obj(baseline["worktree"])["status"] == "clean", "review: dirty, unknown or stale checkout baseline")
    certified_report(review, result, obj(bundle["review_report"]), directory, head)
    boolean_fields(result, ("ready",), "review")
    require(result["action"] in REVIEW_ACTIONS, "review: invalid action")
    if review["outcome"] != "succeeded" or result["ready"] is not True or result["action"] != "none":
        failures.append({"record": "review", "reason": "review has unresolved findings", "action": result["action"],
                         "findings_summary": str(result.get("findings_summary", ""))[:2000],
                         "report": bundle["review_report"]})
    suite, result = native_record("runtime_suite", pins["runtime_suite"], directory, root)
    runtime_files, suite_input = runtime_suite(suite, result, bundle, directory, root, failures)
    wrong, result = native_record("wrong_identity", pins["wrong_identity"], directory, root)
    requested, input_evidence = requested_identity(wrong, bundle, directory, root)
    require(requested == controls["wrong_requested_identity"] and requested != executable,
            "wrong_identity: run did not request frozen wrong identity")
    require(wrong["outcome"] == "failed" and result["verified"] is False
            and result["verdict"] == "inconclusive" and result["candidate"] == executable,
            "wrong_identity: expected independent refusal not observed")
    runtime_files.extend(runtime_evidence(result, refusal=True))
    return {"accepted": not failures, "action": "correct" if failures else "none",
            "allocation": allocation, "candidate": candidate, "records": pins,
            "review_report": bundle["review_report"], "feedback": failures,
            "runtime_evidence": runtime_files,
            "wrong_identity_input_evidence": input_evidence,
            "runtime_suite_input_evidence": suite_input,
            "runtime_contract": bundle["runtime_contract"],
            "boundary": "Hashes prove retained integrity, not authenticity or fresh live admission; no issue completion."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", nargs="?", type=Path)
    parser.add_argument("--identity", action="store_true")
    args = parser.parse_args()
    failures = []
    try:
        if args.identity:
            with Path(__file__).open("rb") as stream:
                print(hashlib.file_digest(stream, "sha256").hexdigest())
            return 0
        require(args.bundle is not None, "BUNDLE.json is required")
        path = args.bundle.resolve(strict=True)
        result = accept(read_json(path), path.parent, failures)
        code = 0 if result["accepted"] else 3
    except (ValueError, KeyError, TypeError, OSError, UnicodeError, RecursionError,
            subprocess.SubprocessError) as error:
        failures.append({"reason": str(error)[:2000]})
        result = {"accepted": False, "action": "hold", "feedback": failures}
        code = 2
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
