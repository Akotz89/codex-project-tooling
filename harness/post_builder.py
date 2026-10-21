#!/usr/bin/env python3
"""Root's explicit output-candidate view; never rewrite the input allocation."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from candidate_identity import identity
from project_policy import load_policy, validate_policy
from linear_intake import digest, intake, read_json

ALLOCATION = ("project_id", "issue", "task_id", "dispatch_id", "worker", "worker_thread_id",
              "source_digest", "scope_digest", "work_order", "work_order_sha256", "writer_paths",
              "candidate_root", "candidate_commit", "candidate_branch", "allowed_changes")


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], timeout=15).decode().strip()


def tracked(root):
    return [name for name in git(root, "ls-files", "-z").split("\0") if name]


def check_view(view, candidate, policy):
    policy = validate_policy(policy)
    if (view["project_id"] != policy["project_id"] or view["issue"] != policy["acceptance_issue"]
            or view["worker"] not in policy["worker_roles"]):
        raise ValueError("output allocation does not match project policy")
    proof = view["post_builder"]
    initial_path = Path(proof["input_release_path"])
    if digest(initial_path) != proof["input_release_sha256"]:
        raise ValueError("original allocation changed")
    if digest(Path(proof["builder_record_path"])) != proof["builder_record_sha256"]:
        raise ValueError("terminal builder record changed")
    initial = read_json(initial_path)
    if any(view[key] != initial[key] for key in ALLOCATION):
        raise ValueError("allocation/work order ancestry changed")
    if digest(Path(initial["work_order"])) != initial["work_order_sha256"]:
        raise ValueError("accepted work order changed")
    if (Path(initial["candidate_root"]).resolve() != candidate
            or git(candidate, "rev-parse", "HEAD") != proof["output_commit"]
            or identity(candidate) != proof["output_identity"]
            or git(candidate, "rev-parse", "main") != initial["candidate_commit"]
            or git(candidate, "symbolic-ref", "--short", "HEAD") != initial["candidate_branch"]
            or git(candidate, "status", "--porcelain")):
        raise ValueError("output HEAD/base/branch or clean source has drifted")
    root = Path(view["root"]).resolve()
    pins = {(root / row["path"]).resolve(): row for row in view["source_files"]}
    required = {(candidate / name).resolve() for name in tracked(candidate)}
    if not required.issubset(pins) or candidate / "harness/acceptance.py" not in required:
        raise ValueError("output tracked source pins are incomplete")
    for path, row in pins.items():
        if not path.is_relative_to(root) or not path.is_file() or path.is_symlink() or digest(path) != row["sha256"]:
            raise ValueError("output source pin changed or escaped")
    return initial


def output_view(initial_path, builder_path, summary_path, expected_run_id, policy):
    policy = validate_policy(policy)
    initial = read_json(initial_path)
    candidate = Path(initial["candidate_root"]).resolve(strict=True)
    root = Path(initial["root"]).resolve(strict=True)
    if not candidate.is_relative_to(root) or not initial["candidate_branch"].startswith("codex/"):
        raise ValueError("candidate branch/root is outside the owned project")
    allowed = set(initial["allowed_changes"])
    if not allowed or any(not name.startswith("harness/") or ".." in Path(name).parts for name in allowed):
        raise ValueError("explicit harness delta scope is required")
    head = git(candidate, "rev-parse", "HEAD")
    git(candidate, "merge-base", "--is-ancestor", initial["candidate_commit"], head)
    changes = [name for name in git(candidate, "diff", "--name-only", "-z", "--no-renames",
                                   initial["candidate_commit"], head).split("\0") if name]
    if not changes or not set(changes).issubset(allowed):
        raise ValueError("builder delta is empty or outside the accepted change set")
    builder = read_json(builder_path)
    terminal = builder["terminal_record"]
    returns = terminal["returns"]
    if (not expected_run_id or builder["id"] != expected_run_id
            or builder["workflow_name"] != policy["workflows"]["implementation"]["workflow"]
            or builder["status"] != "completed" or builder["outcome"] != "succeeded"
            or terminal["run_id"] != expected_run_id or terminal["status"] != "completed"
            or terminal["outcome"] != "succeeded" or returns["availability"] != "available"
            or returns["node_id"] != policy["workflows"]["implementation"]["node"] or returns["value"].get("done") is not True
            or returns["value"].get("green") is not True
            or returns["value"].get("red_cause") != ""
            or Path(builder["working_path"]).resolve() != candidate
            or builder["checkout_baseline"]["kind"] != "git"
            or builder["checkout_baseline"]["worktree"]["status"] != "clean"
            or builder["checkout_baseline"]["commit"] != initial["candidate_commit"]):
        raise ValueError("actual terminal green builder identity is required")
    pins = {}
    for row in initial["source_files"]:
        path = (root / row["path"]).resolve(strict=True)
        relative = path.relative_to(candidate).as_posix() if path.is_relative_to(candidate) else None
        if relative not in allowed and digest(path) != row["sha256"]:
            raise ValueError("input/governance source changed outside builder scope")
        pins[path] = {"path": path.relative_to(root).as_posix(), "sha256": digest(path)}
    for name in tracked(candidate):
        path = candidate / name
        if path.is_symlink():
            raise ValueError("unsafe output source symlink")
        path = path.resolve(strict=True)
        if not path.is_file() or not path.is_relative_to(root):
            raise ValueError("unsafe output source")
        pins[path] = {"path": path.relative_to(root).as_posix(), "sha256": digest(path)}
    summary = read_json(summary_path)
    view = dict(initial, checkpoint_id=summary["checkpoint_id"], source_files=list(pins.values()),
                post_builder={"input_release_path": str(initial_path.resolve()),
                    "input_release_sha256": digest(initial_path), "output_commit": head,
                    "output_identity": identity(candidate), "changed_paths": changes,
                    "builder_run_id": expected_run_id, "builder_record_path": str(builder_path.resolve()),
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
