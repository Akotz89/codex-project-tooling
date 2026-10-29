#!/usr/bin/env python3
"""Identity of the exact project executables exercised by runtime review."""
import hashlib
import argparse
import json
from pathlib import Path
import subprocess

from project_policy import validate_policy

SHARED_FILES = ("linear_intake.py", "acceptance.py", "candidate_identity.py", "project_policy.py",
                "post_builder.py", "prepare_runtime.py")


def harness_digest(root):
    value = hashlib.sha256()
    for name in SHARED_FILES:
        path = Path(root) / "harness" / name
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise ValueError(f"missing or unsafe shared harness executable: {name}")
        value.update(name.encode() + b"\0")
        with path.open("rb") as stream:
            value.update(hashlib.file_digest(stream, "sha256").digest())
    return value.hexdigest()


def identity(root, *, harness_root=None, policy=None, allow_unapproved_control=False):
    value = hashlib.sha256()
    names = ("linear_intake.py", "acceptance.py", "candidate_identity.py", "project_policy.py")
    source = Path(root)
    if harness_root is not None:
        if policy is None:
            raise ValueError("shared harness identity requires a project policy")
        policy = validate_policy(policy)
        if not policy.get("shared_harness", False):
            raise ValueError("external harness identity requires shared harness policy")
        source = Path(harness_root)
        names = SHARED_FILES
        head = subprocess.check_output(["git", "-C", str(Path(root).resolve(strict=True)),
                                        "rev-parse", "HEAD"], text=True, timeout=15).strip()
        value.update(b"shared-harness-v1\0" + head.encode() + b"\0")
        value.update(json.dumps(policy, sort_keys=True, separators=(",", ":"),
                                ensure_ascii=True).encode() + b"\0")
    shared_digest = hashlib.sha256()
    for name in names:
        path = source / "harness" / name
        if not path.is_file() or path.is_symlink() or not path.stat().st_size:
            raise ValueError(f"missing or unsafe runtime executable: {name}")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").digest()
        entry = name.encode() + b"\0" + digest
        value.update(entry)
        shared_digest.update(entry)
    if (harness_root is not None and policy.get("shared_harness", False)
            and not allow_unapproved_control and shared_digest.hexdigest() != policy.get("shared_harness_sha256")):
        raise ValueError("shared harness differs from the approved policy pin")
    return value.hexdigest()


def bound_identity(root, policy):
    if policy.get("shared_harness", False):
        source = Path(__file__).resolve().parent.parent
        return identity(root, harness_root=source, policy=policy)
    return identity(root)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--control-unpinned", action="store_true", help="identity of intentionally modified negative control only")
    args = parser.parse_args()
    if (args.project_root is None) != (args.policy is None):
        parser.error("--project-root and --policy must be supplied together")
    if args.control_unpinned and args.project_root is None:
        parser.error("--control-unpinned requires --project-root and --policy")
    if args.project_root is None:
        print(identity(Path(__file__).resolve().parent.parent))
    else:
        from project_policy import load_policy
        policy = load_policy(args.policy)
        if args.control_unpinned and not policy.get("shared_harness", False):
            parser.error("--control-unpinned requires shared harness policy")
        print(identity(args.project_root, harness_root=Path(__file__).resolve().parent.parent,
                       policy=policy, allow_unapproved_control=args.control_unpinned))
