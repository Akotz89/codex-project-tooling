#!/usr/bin/env python3
"""Root's explicit output-candidate view; never rewrite the input allocation."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from candidate_identity import bound_identity
from project_policy import load_policy, validate_policy
from linear_intake import digest, intake, read_json

ALLOCATION = ("project_id", "issue", "task_id", "dispatch_id", "worker", "worker_thread_id",
              "source_digest", "scope_digest", "work_order", "work_order_sha256", "writer_paths",
              "candidate_root", "candidate_commit", "candidate_branch", "allowed_changes")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], timeout=15).decode().strip()


def tracked(root):
    return [name for name in git(root, "ls-files", "-z").split("\0") if name]


def source_path(root, name, *, allow_missing=False):
    if (not isinstance(name, str) or not name or name != name.strip()
            or "\0" in name or "\\" in name):
        raise ValueError("invalid source path")
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise ValueError("source path must be relative and contained")
    root = Path(root).resolve(strict=True)
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("source symlinks are not accepted")
    resolved = path.resolve(strict=not allow_missing)
    if not resolved.is_relative_to(root):
        raise ValueError("source path escaped its root")
    return path, resolved


def allowed_changes(initial):
    values = initial.get("allowed_changes")
    if not isinstance(values, list) or not values:
        raise ValueError("explicit relative change scope is required")
    paths = set()
    for value in values:
        if (not isinstance(value, str) or not value or value != value.strip()
                or "\0" in value or "\\" in value):
            raise ValueError("invalid allowed change path")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            raise ValueError("allowed change paths must be relative and contained")
        paths.add(path.as_posix())
    if len(paths) != len(values):
        raise ValueError("allowed change paths must be unique")
    return paths


def changed_paths(candidate, base):
    git(candidate, "merge-base", "--is-ancestor", base, "HEAD")
    return [name for name in git(candidate, "diff", "--name-only", "-z", "--no-renames",
                                 base, "HEAD").split("\0") if name]


def validate_builder(builder, expected_run_id, candidate, initial, policy):
    if not isinstance(builder, dict) or not isinstance(expected_run_id, str) or not expected_run_id:
        raise ValueError("actual terminal green builder identity is required")
    terminal = builder.get("terminal_record")
    if not isinstance(terminal, dict):
        raise ValueError("actual terminal green builder identity is required")
    returns = terminal.get("returns")
    if not isinstance(returns, dict):
        raise ValueError("actual terminal green builder identity is required")
    value = returns.get("value")
    baseline = builder.get("checkout_baseline")
    if not isinstance(value, dict) or not isinstance(baseline, dict):
        raise ValueError("actual terminal green builder identity is required")
    worktree = baseline.get("worktree")
    if not isinstance(worktree, dict):
        raise ValueError("actual terminal green builder identity is required")
    if (builder.get("id") != expected_run_id
            or builder.get("workflow_name") != policy["workflows"]["implementation"]["workflow"]
            or builder.get("status") != "completed" or builder.get("outcome") != "succeeded"
            or terminal.get("run_id") != expected_run_id or terminal.get("status") != "completed"
            or terminal.get("outcome") != "succeeded" or returns.get("availability") != "available"
            or returns.get("node_id") != policy["workflows"]["implementation"]["node"]
            or value.get("done") is not True or value.get("green") is not True
            or value.get("red_cause") != ""
            or Path(builder.get("working_path", "")).resolve(strict=True) != candidate
            or baseline.get("kind") != "git" or worktree.get("status") != "clean"
            or baseline.get("commit") != initial["candidate_commit"]):
        raise ValueError("actual terminal green builder identity is required")
    return builder


def check_view(view, candidate, policy):
    policy = validate_policy(policy)
    if (view["project_id"] != policy["project_id"] or view["issue"] != policy["acceptance_issue"]
            or view["worker"] not in policy["worker_roles"]):
        raise ValueError("output allocation does not match project policy")
    proof = view["post_builder"]
    root = Path(view["root"]).resolve(strict=True)
    candidate = Path(candidate).resolve(strict=True)
    initial_path = Path(proof["input_release_path"])
    if not initial_path.is_absolute():
        raise ValueError("original allocation path must be absolute")
    initial_path = source_path(root, initial_path.relative_to(root).as_posix())[1]
    if digest(initial_path) != proof["input_release_sha256"]:
        raise ValueError("original allocation changed")
    initial = read_json(initial_path)
    if any(view[key] != initial[key] for key in ALLOCATION):
        raise ValueError("allocation/work order ancestry changed")
    if digest(Path(initial["work_order"])) != initial["work_order_sha256"]:
        raise ValueError("accepted work order changed")
    allowed = allowed_changes(initial)
    changes = changed_paths(candidate, initial["candidate_commit"])
    recorded = proof.get("changed_paths")
    if (not changes or not isinstance(recorded, list) or set(changes) != set(recorded)
            or not set(changes).issubset(allowed)):
        raise ValueError("candidate delta differs from the accepted change scope")
    branch = git(candidate, "symbolic-ref", "--short", "HEAD")
    if (Path(initial["candidate_root"]).resolve(strict=True) != candidate
            or not initial["candidate_branch"].startswith("codex/")
            or git(candidate, "rev-parse", "HEAD") != proof["output_commit"]
            or bound_identity(candidate, policy) != proof["output_identity"]
            or branch != initial["candidate_branch"]
            or git(candidate, "status", "--porcelain")):
        raise ValueError("output HEAD/base/branch or clean source has drifted")
    builder_path = Path(proof["builder_record_path"])
    if not builder_path.is_absolute():
        raise ValueError("builder record path must be absolute")
    builder_path = source_path(root, builder_path.relative_to(root).as_posix())[1]
    if digest(builder_path) != proof["builder_record_sha256"]:
        raise ValueError("terminal builder record changed")
    validate_builder(read_json(builder_path), proof.get("builder_run_id"), candidate, initial, policy)
    if digest(Path(initial["work_order"])) != initial["work_order_sha256"]:
        raise ValueError("accepted work order changed")
    pins = {}
    for row in view["source_files"]:
        path = source_path(root, row["path"])[1]
        pins[path] = row
    required = {source_path(candidate, name)[1] for name in tracked(candidate)}
    if not required.issubset(pins) or (not policy.get("shared_harness", False)
                                       and candidate / "harness/acceptance.py" not in required):
        raise ValueError("output tracked source pins are incomplete")
    for path, row in pins.items():
        if not path.is_file() or digest(path) != row["sha256"]:
            raise ValueError("output source pin changed or escaped")
    return initial


def output_view(initial_path, builder_path, summary_path, expected_run_id, policy):
    policy = validate_policy(policy)
    initial_path = Path(initial_path).resolve(strict=True)
    initial = read_json(initial_path)
    root = Path(initial["root"]).resolve(strict=True)
    if not initial_path.is_relative_to(root):
        raise ValueError("original allocation is outside the project root")
    candidate = Path(initial["candidate_root"]).resolve(strict=True)
    if not candidate.is_relative_to(root) or not initial["candidate_branch"].startswith("codex/"):
        raise ValueError("candidate branch/root is outside the owned project")
    allowed = allowed_changes(initial)
    head = git(candidate, "rev-parse", "HEAD")
    changes = changed_paths(candidate, initial["candidate_commit"])
    if not changes or not set(changes).issubset(allowed):
        raise ValueError("builder delta is empty or outside the accepted change set")
    builder_path = Path(builder_path).resolve(strict=True)
    if not builder_path.is_relative_to(root):
        raise ValueError("builder record is outside the project root")
    builder = validate_builder(read_json(builder_path), expected_run_id, candidate, initial, policy)
    pins = {}
    for row in initial["source_files"]:
        lexical, path = source_path(root, row["path"], allow_missing=True)
        relative = lexical.relative_to(candidate).as_posix() if lexical.is_relative_to(candidate) else None
        if not lexical.exists():
            if relative in allowed:
                continue
            raise ValueError("pinned input source is missing")
        if relative not in allowed and digest(path) != row["sha256"]:
            raise ValueError("input/governance source changed outside builder scope")
        pins[path] = {"path": path.relative_to(root).as_posix(), "sha256": digest(path)}
    for name in tracked(candidate):
        _, path = source_path(candidate, name)
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("unsafe output source")
        pins[path] = {"path": path.relative_to(root).as_posix(), "sha256": digest(path)}
    summary = read_json(summary_path)
    view = dict(initial, checkpoint_id=summary["checkpoint_id"], source_files=list(pins.values()),
                post_builder={"input_release_path": str(initial_path),
                    "input_release_sha256": digest(initial_path), "output_commit": head,
                    "output_identity": bound_identity(candidate, policy), "changed_paths": changes,
                    "builder_run_id": expected_run_id, "builder_record_path": str(builder_path),
                    "builder_record_sha256": digest(builder_path),
                    "output_source_digest": hashlib.sha256(json.dumps(list(pins.values()), sort_keys=True).encode()).hexdigest()})
    check_view(view, candidate, policy)
    if not intake(view, summary, policy)["ready"]:
        raise ValueError("same original allocation no longer owns the output")
    return view


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("initial_release", type=Path)
    parser.add_argument("builder_get", type=Path)
    parser.add_argument("fresh_summary", type=Path)
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--policy", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(output_view(args.initial_release, args.builder_get, args.fresh_summary,
                                     args.expected_run_id, load_policy(args.policy)), indent=2))
    except (ValueError, KeyError, TypeError, OSError, RecursionError, subprocess.SubprocessError) as error:
        parser.exit(2, f"output candidate held: {error}\n")
