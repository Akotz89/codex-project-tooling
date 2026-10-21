#!/usr/bin/env python3
"""Translate an explicitly allocated Linear work order; never allocate or dispatch."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlparse


def read_json(path):
    path = Path(path)
    with path.open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("input exceeds 1 MiB")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("input must be an object")
    return value


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def intake(release, summary):
    for key in ("task_id", "dispatch_id", "worker_thread_id", "checkpoint_id"):
        value = release[key]
        if not isinstance(value, str) or not value or value != value.strip() or "\0" in value:
            raise ValueError(f"invalid identity: {key}")
    if release["worker"] not in {"core1", "core2", "core3", "core4"}:
        raise ValueError("invalid core role")
    root = Path(release["root"]).resolve(strict=True)
    if (release.get("version") != 1 or release.get("accepted") is not True
            or release.get("project_id") != "P-CPI-10"
            or summary["project"]["project_id"] != release["project_id"]
            or Path(summary["project"]["root"]).resolve() != root):
        raise ValueError("accepted project binding is required")
    issue = release["issue"]
    if not isinstance(issue, str) or not issue.startswith("CPI-") or not issue[4:].isdigit():
        raise ValueError("invalid Linear issue")
    ref = urlparse(release["linear_ref"])
    if (ref.scheme != "https" or ref.netloc != "linear.app"
            or ref.path.strip("/").split("/")[:3] != ["codex-azyrra", "issue", issue]):
        raise ValueError("Linear reference does not match issue")
    if not isinstance(release["work_order"], str) or not release["work_order"].strip():
        raise ValueError("work order is empty")
    if release["checkpoint_id"] != summary["checkpoint_id"]:
        raise ValueError("coordination snapshot has drifted")
    bounds = summary["bounds"]
    if not isinstance(bounds, dict):
        raise ValueError("invalid coordination bounds")
    if (summary.get("action") != "OK" or summary.get("pending_checkpoint")
            or bounds.get("truncated_fields") or bounds["offset"] != 0
            or bounds["next_offset"] is not None
            or bounds["total_tasks"] != len(summary["tasks"])):
        raise ValueError("coordination snapshot is incomplete")
    tasks = {task["id"]: task for task in summary["tasks"]}
    if len(tasks) != len(summary["tasks"]):
        raise ValueError("duplicate tasks in coordination snapshot")
    task = tasks.get(release["task_id"])
    if task is None:
        raise ValueError("allocated task is absent from snapshot")
    if (task.get("status") != "ACKNOWLEDGED" or task.get("lease") is not True
            or task.get("worker") != release["worker"]
            or task.get("dispatch_id") != release["dispatch_id"]
            or task.get("dispatch_thread_id") != release["worker_thread_id"]
            or summary["project"]["workers"].get(release["worker"]) != release["worker_thread_id"]):
        return {"ready": False, "reason": "task has no acknowledged matching ownership"}
    claims = sorted(str((root / path).resolve()) for path in release["writer_paths"])
    if (not claims or claims != sorted(task["writer_paths"])
            or any(not Path(path).is_relative_to(root) or Path(path) == root for path in claims)):
        raise ValueError("writer scope does not match allocated task")
    if task["linear_ref"].rstrip("/") != release["linear_ref"]:
        raise ValueError("task belongs to another Linear issue")
    missing = [key for key in task["dependencies"]
               if key not in tasks or tasks[key]["status"] != "COMPLETE"]
    if missing:
        return {"ready": False, "reason": "uncompleted dependencies", "dependencies": missing}
    if not release["source_files"]:
        raise ValueError("source pins are empty")
    seen = set()
    for pin in release["source_files"]:
        path = (root / pin["path"]).resolve(strict=True)
        if path in seen:
            raise ValueError("duplicate source pin")
        seen.add(path)
        if not path.is_relative_to(root) or not path.is_file() or digest(path) != pin["sha256"]:
            raise ValueError("source pin drift or escape")
    return {"ready": True, "project_id": release["project_id"], "issue": issue,
            "task_id": task["id"], "dispatch_id": task["dispatch_id"],
            "worker": task["worker"], "workflow": "archon-implement",
            "worker_thread_id": release["worker_thread_id"], "writer_paths": claims,
            "inputs": {"work": release["work_order"]},
            "publication": "none", "checkpoint_id": summary["checkpoint_id"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", nargs="?", type=Path)
    parser.add_argument("summary", nargs="?", type=Path)
    parser.add_argument("--identity", action="store_true")
    args = parser.parse_args()
    try:
        if args.identity:
            print(digest(__file__))
            return 0
        if args.release is None or args.summary is None:
            parser.error("release and summary are required")
        result = intake(read_json(args.release), read_json(args.summary))
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ready"] else 3
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(json.dumps({"ready": False, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
