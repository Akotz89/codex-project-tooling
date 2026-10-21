"""Read-only Beads ownership and live lease observation for new dispatch."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess

from .runtime import InvalidEvent, read_status, require, validate_project_policy


def task_binding(db, event):
    """Read compact pages without creating a database or resuming checkpoints."""
    checkpoint = None
    offset = 0
    while True:
        snapshot = read_status(Path(db).resolve(), {
            "op": "summary", "project_id": event["project_id"], "root": event["root"],
            "offset": offset, "limit": 100,
        })
        if checkpoint is None:
            checkpoint = snapshot["checkpoint_id"]
        require(snapshot["checkpoint_id"] == checkpoint, "coordinator changed during Beads preflight; retry")
        require(not snapshot["pending_checkpoint"], "pending checkpoint; recover before Beads preflight")
        for index, task in enumerate(snapshot["tasks"], offset):
            if task["id"] == event["task_id"]:
                prefix = f"tasks.{index}."
                require(not any(field.startswith(prefix) for field in snapshot["bounds"]["truncated_fields"]),
                        "task binding is truncated; Beads preflight cannot verify it")
                return task, checkpoint
        offset = snapshot["bounds"]["next_offset"]
        require(offset is not None, "task absent from coordinator")


def check_beads(db, event, *, beads_dir, assignee, executable, project_policy=None):
    """Observe a claimed global execution record; never claim or synchronize it."""
    require(event["op"] in {"prepare", "issue"}, "Beads guard supports prepare and issue only")
    directory = Path(beads_dir)
    binary = Path(executable)
    require(directory.is_absolute() and directory.is_dir(), "Beads directory must be an existing absolute directory")
    require(binary.is_absolute() and binary.is_file() and os.access(binary, os.X_OK),
            "bd executable must be an executable absolute file")
    require(isinstance(assignee, str) and bool(assignee) and assignee == assignee.strip() and "\0" not in assignee,
            "Beads assignee must be a nonempty exact actor")
    task, checkpoint = task_binding(db, event)
    policy = validate_project_policy(project_policy, event) if project_policy is not None else None
    legacy_leaseless = {
        (item["beads_id"], item["assignee"])
        for item in (policy or {}).get("legacy_leaseless_beads", [])
    }
    reference = task["beads_ref"]
    bead_id = reference.removeprefix("global:")
    require(reference.startswith(("global:", "global-")) and
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", bead_id) is not None,
            "task Beads reference must identify one global record")
    env = {**os.environ, "BEADS_DIR": str(directory.resolve()), "BEADS_DOLT_SHARED_SERVER": "1"}
    records = []
    for _ in range(2):
        try:
            result = subprocess.run([str(binary), "--readonly", "--global", "show", bead_id, "--json"],
                                    env=env, text=True, capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise InvalidEvent("read-only Beads lookup unavailable") from exc
        require(result.returncode == 0, f"read-only Beads lookup failed (exit {result.returncode})")
        try:
            payload = json.loads(result.stdout)
        except (ValueError, TypeError) as exc:
            raise InvalidEvent("read-only Beads lookup returned invalid JSON") from exc
        require(isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict),
                "Beads lookup must return exactly one record")
        record = payload[0]
        require(record.get("id") == bead_id, "Beads record identity mismatch")
        require(record.get("external_ref") == task["linear_ref"], "Beads Linear reference mismatch")
        require(record.get("status") == "in_progress", "Beads record is not in_progress")
        require(record.get("assignee") == assignee, "Beads execution assignee mismatch or unclaimed")
        records.append(record)
    fields = ("id", "external_ref", "status", "assignee", "revision", "heartbeat_at", "lease_expires_at")
    require(all((field in records[0], records[0].get(field)) ==
                (field in records[1], records[1].get(field)) for field in fields),
            "Beads claim changed during observation; reconcile before dispatch")
    observed = datetime.now(timezone.utc)
    heartbeat, expiry = record.get("heartbeat_at"), record.get("lease_expires_at")
    if heartbeat is None and expiry is None:
        require((bead_id, assignee) in legacy_leaseless,
                "Beads claim lacks heartbeat and lease expiry; exact owner must renew externally")
        lease = {"mode": "LEGACY_LEASELESS", "heartbeat_at": None, "lease_expires_at": None}
    else:
        heartbeat = lease_date(heartbeat, "heartbeat_at")
        expiry = lease_date(expiry, "lease_expires_at")
        require(heartbeat <= observed < expiry and heartbeat < expiry,
                "Beads lease expired or heartbeat is future/invalid; exact owner must renew externally")
        lease = {"mode": "LEASED", "heartbeat_at": heartbeat.isoformat(), "lease_expires_at": expiry.isoformat()}
    return {"action": "VERIFIED", "beads_id": bead_id, "assignee": assignee,
            "linear_ref": task["linear_ref"], "coordinator_checkpoint_id": checkpoint,
            "readonly": True, "lease": {**lease, "observed_at": observed.isoformat()}}


def lease_date(value, field):
    try:
        require(isinstance(value, str), f"Beads {field} must be a timezone-aware datetime")
        parsed = datetime.fromisoformat(value)
        require(parsed.tzinfo is not None and parsed.utcoffset() is not None,
                f"Beads {field} must be a timezone-aware datetime")
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise InvalidEvent(f"Beads {field} must be a timezone-aware datetime") from exc
