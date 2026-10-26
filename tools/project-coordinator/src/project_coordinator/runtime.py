"""Deterministic LangGraph transitions guarded by one filesystem lock per DB."""
from __future__ import annotations

import copy
from contextlib import closing
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
import sqlite3
from typing import TypedDict
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver

STREAM_CHUNK_BYTES = 1024 * 1024


class State(TypedDict, total=False):
    model: dict
    event: dict
    event_hash: str
    event_hash_event_id: str
    event_received_at: str
    event_received_at_event_id: str
    response: dict
    project_policy: dict


class InvalidEvent(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidEvent(message)


def string(value):
    return isinstance(value, str) and bool(value)


def worker_role(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is not None


def native_thread_id(value):
    return string(value) and value == value.strip() and "\0" not in value


def redact_report_text(value):
    value = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|(?:basic|bearer)\s+[^\s,;&}]+|[^\s,;&}]+)", r"\1[redacted]", value)
    value = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[redacted]", value)
    value = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key)([\"']?\s*[=:]\s*)([\"'])(.*?)\3",
                   lambda match: f"{match[1]}{match[2]}{match[3]}[redacted]{match[3]}", value, flags=re.DOTALL)
    value = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)(\s*[=:]\s*)[^\s,;&}]+", r"\1\2[redacted]", value)
    return re.sub(r"(://)[^/@\s]+@", r"\1[redacted]@", value)


def redact_report_event(event):
    event = copy.deepcopy(event)
    for field in ("state", "detail", "source"):
        event[field] = redact_report_text(event[field])
    event["evidence"] = [redact_report_text(link) for link in event["evidence"]]
    return event


def redact_project_report_event(event):
    event = copy.deepcopy(event)
    for field in ("state", "detail", "work_item", "source"):
        event[field] = redact_report_text(event[field])
    event["evidence"] = [redact_report_text(link) for link in event["evidence"]]
    return event


def event_fingerprint(event):
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def reference_set_digest(references):
    return hashlib.sha256(json.dumps(references, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_project_policy(policy, binding):
    fields = {"schema_version", "project_id", "root", "worker_registrations", "legacy_leaseless_beads"}
    require(isinstance(policy, dict) and frozenset(policy) in {frozenset(fields), frozenset(fields | {"project_report_registrations"})},
            "project policy requires exactly the documented fields")
    require(type(policy["schema_version"]) is int and policy["schema_version"] == 1,
            "unsupported project policy schema")
    require(string(policy["project_id"]) and policy["project_id"] == binding["project_id"],
            "project policy identity mismatch")
    require(string(policy["root"]) and Path(policy["root"]).is_absolute(),
            "project policy root must be absolute")
    try:
        require(Path(policy["root"]).resolve(strict=True) == Path(binding["root"]).resolve(strict=True),
                "project policy root mismatch")
    except (OSError, RuntimeError) as exc:
        raise InvalidEvent("project policy root is unavailable") from exc
    registrations = policy["worker_registrations"]
    require(isinstance(registrations, list), "worker_registrations must be a list")
    roles, thread_ids = set(), set()
    for item in registrations:
        require(isinstance(item, dict) and set(item) == {"worker", "thread_id"},
                "worker registration policy requires worker and thread_id")
        role, thread_id = item["worker"], item["thread_id"]
        require(worker_role(role) and native_thread_id(thread_id),
                "invalid worker registration policy")
        require(role not in roles and thread_id not in thread_ids, "duplicate worker registration policy")
        roles.add(role)
        thread_ids.add(thread_id)
    project_registrations = policy.get("project_report_registrations", [])
    require(isinstance(project_registrations, list), "project_report_registrations must be a list")
    project_roles, project_thread_ids = set(), set()
    for item in project_registrations:
        require(isinstance(item, dict) and set(item) == {"role", "thread_id"},
                "project report registration requires role and thread_id")
        role, thread_id = item["role"], item["thread_id"]
        require(worker_role(role) and role != "coordinator" and native_thread_id(thread_id),
                "invalid project report registration")
        require(role not in project_roles and thread_id not in project_thread_ids,
                "duplicate project report registration")
        project_roles.add(role)
        project_thread_ids.add(thread_id)
    require(project_roles.isdisjoint(roles) and project_thread_ids.isdisjoint(thread_ids),
            "project report registrations must be separate from worker registrations")
    legacy = policy["legacy_leaseless_beads"]
    require(isinstance(legacy, list), "legacy_leaseless_beads must be a list")
    pairs = set()
    for item in legacy:
        require(isinstance(item, dict) and set(item) == {"beads_id", "assignee"},
                "legacy Beads policy requires beads_id and assignee")
        bead_id, assignee = item["beads_id"], item["assignee"]
        require(isinstance(bead_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", bead_id),
                "invalid legacy Beads ID")
        require(native_thread_id(assignee),
                "invalid legacy Beads assignee")
        require((bead_id, assignee) not in pairs, "duplicate legacy Beads policy")
        pairs.add((bead_id, assignee))
    return policy


def validate_event(event, *, received_at=None):
    require(isinstance(event, dict), "event must be an object")
    op = event.get("op")
    require(isinstance(op, str) and op in {"init", "add", "refresh_references", "observe", "prepare", "issue", "ack", "result", "reconcile", "retire", "report", "project_report", "status", "recover", "rebind", "register", "summary", "bundle", "updates"}, "invalid event op")
    if op not in {"status", "recover", "summary", "bundle", "updates"}:
        require(string(event.get("event_id")), "event_id required")
    if op == "init":
        project = event.get("project")
        require(isinstance(project, dict), "project must be an object")
        for field in ("root", "project_id", "linear_ref", "beads_ref", "coordinator_thread_id"):
            require(string(project.get(field)), f"project.{field} required")
        workers = project.get("workers")
        require(isinstance(workers, dict) and bool(workers) and all(worker_role(role) for role in workers),
                "workers must bind nonempty safe role names")
        require(all(native_thread_id(value) for value in workers.values()), "worker thread IDs must be nonempty exact strings")
    else:
        require(string(event.get("project_id")) and string(event.get("root")), "project_id and root required")
    if op == "add":
        task = event.get("task")
        require(isinstance(task, dict), "task must be an object")
        fields = {"id", "linear_ref", "beads_ref", "worker", "prompt", "source_digest", "scope_digest", "dependencies", "writer_paths"}
        require(frozenset(task) in {frozenset(fields), frozenset(fields | {"references"})}, "task requires exactly the documented fields plus optional references; unknown fields forbidden")
        for field in fields - {"dependencies", "writer_paths"}:
            require(string(task[field]), f"task.{field} required")
        require(worker_role(task["worker"]), "invalid worker role")
        for field in ("dependencies", "writer_paths"):
            require(isinstance(task[field], list) and all(string(value) for value in task[field]), f"task.{field} must be a list of nonempty strings")
        validate_references(task.get("references", []))
    if op == "refresh_references":
        fields = {"op", "event_id", "project_id", "root", "task_id", "source_digest", "scope_digest",
                  "expected_reference_revision", "expected_reference_digest", "reference_revision", "references"}
        require(set(event) == fields, "refresh_references requires exactly the documented fields")
        for field in ("task_id", "source_digest", "scope_digest", "reference_revision"):
            require(string(event[field]), f"{field} required")
        require(event["expected_reference_revision"] is None or string(event["expected_reference_revision"]),
                "expected reference revision must be null or nonempty")
        require(isinstance(event["expected_reference_digest"], str)
                and re.fullmatch(r"[0-9a-f]{64}", event["expected_reference_digest"]),
                "expected reference digest must be lowercase SHA-256")
        validate_references(event["references"])
    if op == "summary":
        require(type(event.get("offset", 0)) is int and event.get("offset", 0) >= 0, "invalid status offset")
        require(type(event.get("limit", 50)) is int and 1 <= event.get("limit", 50) <= 100, "invalid status limit")
        require(type(event.get("report_offset", 0)) is int and event.get("report_offset", 0) >= 0, "invalid report offset")
        require(type(event.get("report_limit", 50)) is int and 1 <= event.get("report_limit", 50) <= 100, "invalid report limit")
        require(type(event.get("project_report_offset", 0)) is int and event.get("project_report_offset", 0) >= 0, "invalid project report offset")
        require(type(event.get("project_report_limit", 50)) is int and 1 <= event.get("project_report_limit", 50) <= 100, "invalid project report limit")
    if op == "bundle":
        require(string(event.get("task_id")), "task_id required")
        require("role" not in event or worker_role(event["role"]), "invalid bundle role")
        require("thread_id" not in event or native_thread_id(event["thread_id"]), "invalid bundle thread_id")
    if op == "updates":
        require(worker_role(event.get("role")) and native_thread_id(event.get("thread_id")), "updates require a bound role and thread_id")
        require(type(event.get("after_revision", 0)) is int and event.get("after_revision", 0) >= 0, "invalid updates revision")
    canonical_occurred_at = None
    if op == "report":
        fields = {"op", "event_id", "project_id", "root", "task_id", "reporter", "reporter_thread_id", "kind", "occurred_at", "state", "detail", "source", "evidence", "claim_id", "dispatch_id", "source_digest"}
        require(set(event) <= fields and fields - {"claim_id", "dispatch_id", "source_digest"} <= set(event), "report requires only the documented fields")
        require(worker_role(event["reporter"]), "invalid report reporter")
        require(native_thread_id(event["reporter_thread_id"]), "invalid report reporter thread")
        require(event["kind"] in {"ack", "lifecycle", "progress", "blocker", "handoff", "result"}, "invalid report kind")
        require(string(event["task_id"]) and string(event["state"]) and len(event["state"]) <= 512,
                "report task_id and state required; state must be at most 512 characters")
        require(string(event["detail"]) and len(event["detail"]) <= 2000, "report detail must be 1-2000 characters")
        require(string(event["source"]) and len(event["source"]) <= 512, "report source link must be bounded")
        require(isinstance(event["evidence"], list) and 1 <= len(event["evidence"]) <= 16 and all(string(v) and len(v) <= 512 for v in event["evidence"]), "report evidence must be 1-16 bounded links")
        require(string(event["occurred_at"]) and len(event["occurred_at"]) <= 64,
                "report occurred_at must be at most 64 characters")
        try:
            occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
            require(occurred.tzinfo is not None, "report occurred_at requires timezone")
            occurred_utc = occurred.astimezone(timezone.utc)
        except (TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise InvalidEvent("invalid report occurred_at") from exc
        if received_at is None:
            received_at = datetime.now(timezone.utc)
        elif isinstance(received_at, str):
            received_at = datetime.fromisoformat(received_at)
        require(isinstance(received_at, datetime) and received_at.tzinfo is not None,
                "report receipt time must be timezone-aware")
        require(occurred_utc <= received_at.astimezone(timezone.utc) + timedelta(minutes=5),
                "report occurred_at exceeds five-minute future tolerance")
        canonical_occurred_at = occurred_utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
        for optional in ("claim_id", "dispatch_id"):
            require(optional not in event or string(event[optional]), f"{optional} must be nonempty when present")
        require("source_digest" not in event or string(event["source_digest"]), "source_digest must be nonempty when present")
    if op == "project_report":
        fields = {"op", "event_id", "project_id", "root", "work_item", "reporter", "reporter_thread_id", "kind", "occurred_at", "state", "detail", "source", "evidence"}
        require(frozenset(event) in {frozenset(fields), frozenset(fields | {"recipients"})},
                "project_report requires exactly the documented fields")
        require(worker_role(event["reporter"]) and native_thread_id(event["reporter_thread_id"]), "invalid project reporter identity")
        require(event["kind"] in {"lifecycle", "progress", "blocker", "handoff", "result"}, "invalid report kind")
        require(string(event["work_item"]) and len(event["work_item"]) <= 512, "project report requires a bounded work-item reference")
        require(string(event["state"]) and len(event["state"]) <= 512, "project report state must be at most 512 characters")
        require(string(event["detail"]) and len(event["detail"]) <= 2000, "project report detail must be 1-2000 characters")
        require(string(event["source"]) and len(event["source"]) <= 512, "project report source link must be bounded")
        require(isinstance(event["evidence"], list) and 1 <= len(event["evidence"]) <= 16 and all(string(v) and len(v) <= 512 for v in event["evidence"]), "project report requires 1-16 bounded evidence links")
        recipients = event.get("recipients", [])
        require(isinstance(recipients, list) and all(worker_role(role) for role in recipients)
                and len(set(recipients)) == len(recipients) and event["reporter"] not in recipients,
                "project report recipients must be unique other registered role names")
        require(not recipients or event["reporter"] == "coordinator",
                "only the coordinator may direct project reports")
        require(string(event["occurred_at"]) and len(event["occurred_at"]) <= 64, "project report occurred_at must be at most 64 characters")
        try:
            occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
            require(occurred.tzinfo is not None, "project report occurred_at requires timezone")
            occurred_utc = occurred.astimezone(timezone.utc)
        except (TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise InvalidEvent("invalid project report occurred_at") from exc
        if received_at is None:
            received_at = datetime.now(timezone.utc)
        elif isinstance(received_at, str):
            received_at = datetime.fromisoformat(received_at)
        require(isinstance(received_at, datetime) and received_at.tzinfo is not None, "project report receipt time must be timezone-aware")
        require(occurred_utc <= received_at.astimezone(timezone.utc) + timedelta(minutes=5), "project report occurred_at exceeds five-minute future tolerance")
        canonical_project_report_at = occurred_utc.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if op != "project_report":
        canonical_project_report_at = None
    if op == "register":
        require(set(event) == {"op", "event_id", "project_id", "root", "worker", "new_thread_id", "expected_workers", "evidence"}, "register requires exactly the documented fields")
        require(worker_role(event.get("worker")), "register requires a valid worker role")
        require(native_thread_id(event.get("new_thread_id")),
                "register requires a worker thread ID")
        workers = event.get("expected_workers")
        require(isinstance(workers, dict) and bool(workers) and all(worker_role(role) for role in workers)
                and all(native_thread_id(value) for value in workers.values()), "expected_workers must bind safe worker roles")
        require(string(event.get("evidence")) and event["evidence"].strip(), "registration evidence reference required")
    if op == "rebind":
        require(worker_role(event.get("worker")), "invalid worker role")
        for field in ("worker", "expected_old_thread_id", "new_thread_id", "evidence"):
            require(string(event.get(field)), f"{field} required")
    if op == "observe":
        require(worker_role(event.get("worker")), "invalid worker role")
        require(event.get("state") in ("IDLE", "BUSY", "UNKNOWN"), "invalid worker state")
        require(string(event.get("evidence")), "observation evidence reference required")
    if op in {"prepare", "issue", "ack", "result", "reconcile"}:
        require(string(event.get("task_id")), "task_id required")
    if op == "retire":
        require(string(event.get("task_id")), "task_id required")
        require(string(event.get("source_digest")) and string(event.get("scope_digest")), "source_digest and scope_digest required")
        require(event.get("quiescent") is True, "retire requires explicit quiescence confirmation")
        require(string(event.get("evidence")) and event["evidence"].strip(), "retirement evidence reference required")
    if op in {"issue", "ack", "result", "reconcile"}:
        require(string(event.get("dispatch_id")), "dispatch_id required")
    if op == "prepare":
        require(string(event.get("source_digest")) and string(event.get("scope_digest")), "source_digest and scope_digest required")
    if op in {"ack", "result"}:
        validate_file(event.get("receipt"))
    return canonical_project_report_at if op == "project_report" else canonical_occurred_at


def validate_file(value):
    require(isinstance(value, dict) and string(value.get("path")), "receipt path required")
    sha = value.get("sha256")
    require(isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), "receipt sha256 required")


def validate_references(references):
    require(isinstance(references, list), "task.references must be a list")
    ids = set()
    for ref in references:
        fields = {"id", "kind", "path", "sha256", "revision", "updated_at", "required", "dynamic"}
        require(isinstance(ref, dict) and frozenset(ref) in {frozenset(fields), frozenset(fields | {"depends_on", "dependency_status"})}, "each reference requires the documented fields")
        require(string(ref["id"]) and ref["id"] not in ids, "reference ids must be nonempty and unique per task")
        ids.add(ref["id"])
        require(ref["kind"] in {"source", "schema", "config", "test", "artifact", "evidence", "cleanup"}, "invalid reference kind")
        require(string(ref["path"]), "reference path required")
        path = Path(ref["path"])
        require(not path.is_absolute() and ".." not in path.parts, "reference path must be repository-relative")
        sha = ref["sha256"]
        require(sha is None or isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha), "reference sha256 must be null or lowercase SHA-256")
        require(ref["revision"] is None or string(ref["revision"]), "reference revision must be null or nonempty")
        stamp = ref["updated_at"]
        require(stamp is None or string(stamp), "reference updated_at must be null or nonempty")
        if stamp is not None:
            try:
                require(datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is not None, "reference updated_at requires timezone")
            except (TypeError, ValueError) as exc:
                raise InvalidEvent("invalid reference updated_at") from exc
        require(type(ref["required"]) is bool and type(ref["dynamic"]) is bool, "reference required and dynamic flags must be booleans")
        dependencies = ref.get("depends_on", [])
        require(isinstance(dependencies, list), "reference depends_on must be a list")
        targets = set()
        for dependency in dependencies:
            require(isinstance(dependency, dict) and set(dependency) == {"id", "type"}, "reference dependency requires id and type")
            require(string(dependency["id"]) and dependency["id"] != ref["id"] and dependency["id"] not in targets,
                    "reference dependency ids must be unique and non-self-referential")
            require(dependency["type"] in {"imports", "schema_ref", "configures", "tests", "uses", "requires"},
                    "invalid reference dependency type")
            targets.add(dependency["id"])
        dependency_status = ref.get("dependency_status", "dynamic" if ref["dynamic"] else "unknown")
        require(dependency_status in {"known", "unknown", "dynamic", "unavailable"}, "invalid reference dependency_status")
        require(not ref.get("dynamic") or dependency_status == "dynamic", "dynamic references must mark dynamic dependencies")


def reference_status(root, ref):
    if ref["dynamic"]:
        return "dynamic"
    if not ref["sha256"]:
        return "unavailable"
    try:
        _, sha256, _ = hash_reference(root, ref["path"])
        return "current" if sha256 == ref["sha256"] else "changed"
    except (OSError, RuntimeError, InvalidEvent):
        return "unavailable"


def safe_reference_path(root, value):
    base = Path(root).resolve(strict=True)
    relative = Path(value)
    require(not relative.is_absolute() and ".." not in relative.parts, "reference path must be repository-relative")
    probe = base
    for part in relative.parts:
        if part == ".":
            continue
        probe = probe / part
        require(not probe.is_symlink(), "reference path contains a symlink")
    return Path(canonical(str(base), value))


def open_reference_file(root, value):
    """Open a repo-relative regular file without following any symlink component."""
    base = Path(root).resolve(strict=True)
    relative = Path(value)
    require(not relative.is_absolute() and ".." not in relative.parts,
            "reference path must be repository-relative")
    parts = [part for part in relative.parts if part not in {"", "."}]
    require(parts, "reference path must name a file")
    if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY")):
        raise InvalidEvent("safe no-follow reference opening is unavailable on this platform")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    directory_flags = flags | os.O_DIRECTORY
    parent = os.open(base, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=parent)
            os.close(parent)
            parent = child
        descriptor = os.open(parts[-1], flags, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        metadata = os.fstat(descriptor)
        require(stat.S_ISREG(metadata.st_mode), "reference is not a regular file")
        return os.fdopen(descriptor, "rb"), metadata
    except BaseException:
        os.close(descriptor)
        raise


def directory_flags():
    if not all(hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY")):
        raise InvalidEvent("safe no-follow directory access is unavailable on this platform")
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_DIRECTORY | os.O_NOFOLLOW


def open_destination_directory(root_fd, components, created_dirs):
    """Walk or create a destination directory chain relative to pinned directory FDs."""
    flags = directory_flags()
    parent = os.dup(root_fd)
    try:
        for component in components:
            require(component not in {"", ".", ".."} and "/" not in component,
                    "unsafe materialization destination component")
            try:
                child = os.open(component, flags, dir_fd=parent)
            except FileNotFoundError:
                os.mkdir(component, mode=0o777, dir_fd=parent)
                child = os.open(component, flags, dir_fd=parent)
                metadata = os.fstat(child)
                created_dirs.append((os.dup(parent), component, metadata.st_dev, metadata.st_ino))
            os.close(parent)
            parent = child
        return parent
    except BaseException:
        os.close(parent)
        raise


def hash_reference(root, value):
    hasher = hashlib.sha256()
    size = 0
    with open_reference_file(root, value)[0] as stream:
        while chunk := stream.read(STREAM_CHUNK_BYTES):
            size += len(chunk)
            hasher.update(chunk)
    return None, hasher.hexdigest(), size


def artifact_reference(root, artifact):
    try:
        base = Path(root).resolve(strict=True)
        raw = Path(artifact["path"])
        relative = raw.relative_to(base) if raw.is_absolute() else raw
        path = safe_reference_path(str(base), relative.as_posix()).relative_to(base).as_posix()
        return {"id": path, "kind": "artifact", "path": path,
                "sha256": artifact["sha256"], "revision": None, "updated_at": None,
                "required": True, "dynamic": False, "depends_on": [], "dependency_status": "known"}
    except (InvalidEvent, KeyError, ValueError, OSError):
        return None


def reference_inventory(root, tasks):
    references = []
    for task in tasks:
        require(isinstance(task, dict) and string(task.get("id")), "bundle task identity is required")
        task_references = task.get("references", [])
        task_artifacts = task.get("artifacts", [])
        require(isinstance(task_references, list) and isinstance(task_artifacts, list),
                "bundle references and artifacts must be lists")
        references.extend((task["id"], "reference", ref) for ref in task_references)
        for artifact in task_artifacts:
            ref = artifact_reference(root, artifact)
            if ref is not None:
                references.append((task["id"], "artifact", ref))
    return references


def reference_status_projection(root, tasks):
    references = reference_inventory(root, tasks)
    statuses = {}
    for task_id, category, ref in references:
        key = (task_id, category, ref["id"])
        if ref.get("dynamic"):
            statuses[key] = {"status": "dynamic"}
            continue
        if not ref.get("sha256"):
            statuses[key] = {"status": "unavailable"}
            continue
        try:
            _, actual_hash, _ = hash_reference(root, ref["path"])
        except FileNotFoundError:
            statuses[key] = {"status": "missing" if ref.get("sha256") else "unavailable"}
            continue
        except (OSError, RuntimeError, InvalidEvent):
            statuses[key] = {"status": "unavailable", "status_reason": "unsafe_or_unavailable_path"}
            continue
        statuses[key] = {"status": "current" if actual_hash == ref.get("sha256") else "changed"}
    return statuses


def preflight_materialization(root, references):
    for ref in references:
        if ref["status"] != "current":
            continue
        _, sha256, _ = hash_reference(root, ref["path"])
        require(sha256 == ref.get("sha256"), f"source changed since bundle was read: {ref['path']}")


def read_declared_reference(root, ref):
    snapshot = tempfile.SpooledTemporaryFile(max_size=8 * STREAM_CHUNK_BYTES, mode="w+b")
    hasher = hashlib.sha256()
    size = 0
    try:
        with open_reference_file(root, ref["path"])[0] as stream:
            while chunk := stream.read(STREAM_CHUNK_BYTES):
                hasher.update(chunk)
                size += len(chunk)
                snapshot.write(chunk)
        require(hasher.hexdigest() == ref.get("sha256"), "reference changed since it was declared")
        snapshot.seek(0)
        return snapshot, size
    except BaseException:
        snapshot.close()
        raise


def reject_cycles(tasks):
    visited, active = set(), set()

    def visit(task_id):
        require(task_id not in active, f"dependency cycle involving {task_id}")
        if task_id in visited or task_id not in tasks:
            return
        active.add(task_id)
        for dependency in tasks[task_id]["dependencies"]:
            visit(dependency)
        active.remove(task_id)
        visited.add(task_id)

    for task_id in tasks:
        visit(task_id)


def reject_reference_cycles(references):
    by_id = {ref["id"]: ref for ref in references}
    visited, active = set(), set()

    def visit(ref_id):
        require(ref_id not in active, f"reference dependency cycle involving {ref_id}")
        if ref_id in visited or ref_id not in by_id:
            return
        active.add(ref_id)
        for dependency in by_id[ref_id].get("depends_on", []):
            visit(dependency["id"])
        active.remove(ref_id)
        visited.add(ref_id)

    for ref_id in by_id:
        visit(ref_id)


def resolved_claims(model, task):
    root = model["project"]["root"]
    return sorted(set(canonical(root, path) for path in task.get("writer_claims", task["writer_paths"])))

def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(root, value):
    require(string(value), "path must be a nonempty string")
    base = Path(root).resolve(strict=True)
    candidate = Path(value)
    try:
        path = (candidate if candidate.is_absolute() else base / candidate).resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise InvalidEvent("path is unavailable or contains a symlink loop") from exc
    require(path.is_relative_to(base), "path escapes project root")
    return str(path)


def artifacts_valid(model, artifacts):
    if not artifacts:
        return False
    try:
        return all(
            isinstance(a, dict)
            and type(a["size_bytes"]) is int
            and Path(canonical(model["project"]["root"], a["path"])).stat().st_size == a["size_bytes"]
            and len(a["sha256"]) == 64
            and digest(canonical(model["project"]["root"], a["path"])) == a["sha256"]
            for a in artifacts
        )
    except (OSError, KeyError, TypeError, ValueError):
        return False


def dispatch_thread(model, task):
    return task.get("dispatch_thread_id", model["project"]["workers"][task["worker"]])


def project_graph(model, task_ids=None, *, reference_statuses=None):
    selected = set(task_ids if task_ids is not None else model["tasks"])
    pending = list(selected)
    while pending:
        current = pending.pop()
        task = model["tasks"].get(current)
        if task is None:
            continue
        for dependency in task.get("dependencies", []):
            if dependency in model["tasks"] and dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    selected_tasks = [model["tasks"][task_id] for task_id in selected if task_id in model["tasks"]]
    if reference_statuses is None:
        reference_statuses = reference_status_projection(model["project"]["root"], selected_tasks)
    report_counts = {}
    latest_reports = {}
    for report in model.get("reports", []):
        task_id = report.get("task_id")
        if task_id not in selected:
            continue
        report_counts[task_id] = report_counts.get(task_id, 0) + 1
        prior = latest_reports.get(task_id)
        if prior is None or (report.get("revision", 0), report.get("recorded_at", "")) > (
                prior.get("revision", 0), prior.get("recorded_at", "")):
            latest_reports[task_id] = report
    project_id = model["project"]["project_id"]
    nodes = [{"id": f"project:{project_id}", "kind": "project", "label": project_id}]
    edges = []
    for task_id in sorted(selected):
        task = model["tasks"].get(task_id)
        if not task:
            continue
        task_node = f"task:{task_id}"
        nodes.append({"id": task_node, "kind": "task", "label": task_id, "status": task["status"],
                      "role": task["worker"], "revision": task.get("revision", 0),
                      "source_coverage": "declared" if task.get("references") else "unindexed"})
        edges.append({"from": f"project:{project_id}", "to": task_node, "type": "contains"})
        work_item_node = f"work-item:{task_id}"
        nodes.append({"id": work_item_node, "kind": "work_item", "label": task.get("linear_ref", "unknown"),
                      "reference": task.get("linear_ref", "unknown")})
        edges.append({"from": task_node, "to": work_item_node, "type": "tracks"})
        worker_role = task["worker"]
        worker_node = f"role:{worker_role}"
        nodes.append({"id": worker_node, "kind": "role", "label": worker_role, "role": worker_role,
                      "thread_id": model["project"]["workers"].get(worker_role)})
        edges.append({"from": task_node, "to": worker_node, "type": "assigned_to"})
        claim_node = f"claim:{task_id}"
        nodes.append({"id": claim_node, "kind": "claim", "label": task.get("beads_ref", "unknown")})
        edges.append({"from": task_node, "to": claim_node, "type": "claimed_by"})
        if task.get("dispatch_id"):
            dispatch_node = f"dispatch:{task['dispatch_id']}"
            nodes.append({"id": dispatch_node, "kind": "dispatch", "label": task["dispatch_id"],
                          "status": task["status"],
                          "thread_id": task.get("dispatch_thread_id") or model["project"]["workers"].get(worker_role)})
            edges.append({"from": task_node, "to": dispatch_node, "type": "dispatched_as"})
        for dep in task.get("dependencies", []):
            edges.append({"from": task_node, "to": f"task:{dep}", "type": "depends_on"})
            if dep not in model["tasks"]:
                nodes.append({"id": f"task:{dep}", "kind": "task", "label": dep, "status": "missing"})
        for ref in task.get("references", []):
            dependencies = ref.get("depends_on", [])
            dependency_status = ref.get("dependency_status", "dynamic" if ref["dynamic"] else "unknown")
            ref_node = f"{ref['kind']}:{task_id}:{ref['id']}"
            state = reference_statuses.get((task_id, "reference", ref["id"]), {"status": "unavailable"})
            nodes.append({"id": ref_node, "kind": ref["kind"], "label": ref["path"],
                          **state,
                          "sha256": ref["sha256"], "revision": ref["revision"], "updated_at": ref["updated_at"],
                          "dependency_status": dependency_status})
            edges.append({"from": task_node, "to": ref_node, "type": f"references_{ref['kind']}"})
        reference_ids = {ref["id"] for ref in task.get("references", [])}
        for ref in task.get("references", []):
            ref_node = f"{ref['kind']}:{task_id}:{ref['id']}"
            for dependency in ref.get("depends_on", []):
                target = next((item for item in task.get("references", []) if item["id"] == dependency["id"]), None)
                target_node = (f"{target['kind']}:{task_id}:{target['id']}" if target
                               else f"missing-reference:{task_id}:{dependency['id']}")
                if not target:
                    nodes.append({"id": target_node, "kind": "dependency", "label": dependency["id"], "status": "missing"})
                edges.append({"from": ref_node, "to": target_node, "type": dependency["type"],
                              "status": "known" if dependency["id"] in reference_ids else "missing"})
            dependency_status = ref.get("dependency_status", "dynamic" if ref["dynamic"] else "unknown")
            if dependency_status != "known":
                unknown_node = f"{dependency_status}-dependency:{task_id}:{ref['id']}"
                nodes.append({"id": unknown_node, "kind": "dependency", "label": dependency_status,
                              "status": dependency_status})
                edges.append({"from": ref_node, "to": unknown_node, "type": f"{dependency_status}_dependency",
                              "status": dependency_status})
        for artifact in task.get("artifacts", []):
            artifact_node = f"artifact:{task_id}:{artifact['path']}"
            ref = artifact_reference(model["project"]["root"], artifact)
            state = reference_statuses.get((task_id, "artifact", ref["id"]), {"status": "unavailable"}) if ref else {"status": "unavailable"}
            nodes.append({"id": artifact_node, "kind": "artifact", "label": artifact["path"], **state,
                          "sha256": artifact.get("sha256")})
            edges.append({"from": task_node, "to": artifact_node, "type": "produced"})
        report = latest_reports.get(task_id)
        if report:
            report_node = f"evidence:{report['event_id']}"
            nodes.append({"id": report_node, "kind": "evidence", "label": report["kind"],
                          "event_id": report["event_id"], "recorded_at": report["recorded_at"],
                          "revision": report.get("revision", 0), "source": report.get("source"),
                          "evidence": report.get("evidence", [])})
            edges.append({"from": task_node, "to": report_node, "type": "latest_evidence"})
    unique = {node["id"]: node for node in nodes}
    report_history = {task_id: {"total": report_counts[task_id],
                                "latest_revision": latest_reports[task_id].get("revision", 0)}
                      for task_id in sorted(report_counts)}
    return {"nodes": list(unique.values()), "edges": edges,
            "report_history": report_history,
            "report_timeline": "Use the paged summary report timeline for complete history."}


def task_bundle(model, task_id, *, role=None, thread_id=None, project_policy=None):
    require(task_id in model["tasks"], "unknown task_id")
    require((role is None) == (thread_id is None), "role and thread_id must be supplied together")
    if role is not None:
        if role in model["project"]["workers"]:
            require(model["project"]["workers"].get(role) == thread_id, "task bundle caller binding mismatch")
            require(model["tasks"][task_id]["worker"] == role, "task bundle role does not own the assigned task")
        else:
            policy = validate_project_policy(project_policy, model["project"]) if project_policy is not None else None
            require(policy is not None and any(item["role"] == role and item["thread_id"] == thread_id
                                               for item in policy.get("project_report_registrations", [])),
                    "task bundle caller binding mismatch")
            require(any(role in report.get("recipients", [])
                        and work_item_matches(report.get("work_item"), model["tasks"][task_id])
                        for report in model.get("project_reports", [])),
                    "task bundle is not directed to this project role")
    ordered, active = [], set()

    def visit(current):
        if current in active:
            raise InvalidEvent(f"dependency cycle involving {current}")
        if current in ordered:
            return
        task = model["tasks"].get(current)
        if task is None:
            return
        active.add(current)
        for dependency in task.get("dependencies", []):
            visit(dependency)
        active.remove(current)
        ordered.append(current)

    visit(task_id)
    closure_tasks = [model["tasks"][item] for item in ordered]
    reference_statuses = reference_status_projection(model["project"]["root"], closure_tasks)
    missing = sorted({dep for current in ordered for dep in model["tasks"][current].get("dependencies", []) if dep not in model["tasks"]})
    references = [{**ref, "task_id": current,
                   **reference_statuses.get((current, "reference", ref["id"]), {"status": "unavailable"})}
                  for current in ordered for ref in model["tasks"][current].get("references", [])]
    root = Path(model["project"]["root"]).resolve(strict=True)
    for current in ordered:
        for artifact in model["tasks"][current].get("artifacts", []):
            artifact_ref = artifact_reference(str(root), artifact)
            if artifact_ref is None:
                artifact_ref = {"id": f"unavailable-artifact-{len(references)}", "kind": "artifact",
                                "path": Path(str(artifact.get("path", "unknown"))).name,
                                "sha256": artifact.get("sha256"), "revision": None, "updated_at": None,
                                "required": True, "dynamic": False, "depends_on": [],
                                "dependency_status": "unavailable", "task_id": current, "status": "unavailable"}
            else:
                artifact_ref["task_id"] = current
                artifact_ref.update(reference_statuses.get((current, "artifact", artifact_ref["id"]), {"status": "unavailable"}))
            references.append(artifact_ref)
    required_gaps = [ref for ref in references if ref["required"] and ref["status"] != "current"]
    unknown_edges = []
    for current in ordered:
        refs = model["tasks"][current].get("references", [])
        if not refs:
            unknown_edges.append({"task_id": current, "status": "unindexed", "required": True})
        known_ids = {ref["id"] for ref in refs}
        for ref in refs:
            edge_state = ref.get("dependency_status", "dynamic" if ref.get("dynamic") else "unknown")
            if edge_state != "known":
                unknown_edges.append({"task_id": current, "reference_id": ref["id"], "status": edge_state,
                                      "required": ref["required"]})
            unknown_edges.extend({"task_id": current, "reference_id": ref["id"], "dependency_id": dep["id"],
                                  "status": "missing", "required": ref["required"]}
                                 for dep in ref.get("depends_on", []) if dep["id"] not in known_ids)
    task_views = [copy.deepcopy(model["tasks"][item]) for item in ordered]
    for task_view in task_views:
        task_view["source_coverage"] = "declared" if task_view.get("references") else "unindexed"
    required_unknown_edges = [edge for edge in unknown_edges if edge.get("required", True)]
    directed_reports = []
    recipient_role = role if role is not None else model["tasks"][task_id]["worker"]
    if recipient_role is not None:
        latest = None
        for report in model.get("project_reports", []):
            if (recipient_role in report.get("recipients", [])
                    and work_item_matches(report.get("work_item"), model["tasks"][task_id])
                    and (latest is None or report.get("revision", 0) > latest.get("revision", 0))):
                latest = report
        if latest is not None:
            directed_reports = [copy.deepcopy(latest)]
    return {"action": "OK", "revision": model.get("revision", 0), "task_id": task_id,
            "tasks": task_views,
            "reference_state": {"revision": model["tasks"][task_id].get("reference_revision"),
                                "digest": reference_set_digest(model["tasks"][task_id].get("references", []))},
            "directed_reports": directed_reports,
            "missing_dependencies": missing, "references": references, "unknown_edges": unknown_edges,
            "graph": project_graph(model, ordered, reference_statuses=reference_statuses),
            "freshness": {"observed_at": datetime.now(timezone.utc).isoformat(),
                          "required_gaps": len(required_gaps), "unknown_edges": len(unknown_edges),
                          "required_unknown_edges": len(required_unknown_edges),
                          "current": not missing and not required_gaps and not required_unknown_edges}}


def work_item_matches(work_item, task):
    """Match a report's issue link against its assigned task's bound identities."""
    def tokens(value):
        if not isinstance(value, str):
            return set()
        normalized = unquote(value).strip().casefold().rstrip("/")
        result = {normalized}
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            path_parts = [unquote(part).casefold() for part in parsed.path.split("/") if part]
            for index, part in enumerate(path_parts[:-1]):
                if part in {"issue", "issues"}:
                    result.add(f"issue:{path_parts[index + 1]}")
        elif normalized.startswith("linear:"):
            issue_id = normalized.partition(":")[2]
            if re.fullmatch(r"[a-z][a-z0-9]*-[0-9]+", issue_id):
                result.add(f"issue:{issue_id}")
        elif re.fullmatch(r"[a-z][a-z0-9]*-[0-9]+", normalized):
            result.add(f"issue:{normalized}")
        return result

    report_tokens = tokens(work_item)
    candidates = {task.get(key) for key in ("id", "linear_ref", "beads_ref", "dispatch_id")}
    return any(report_tokens & tokens(candidate) for candidate in candidates if isinstance(candidate, str))


def materialize_bundle(root, bundle, destination):
    base = Path(root).resolve(strict=True)
    output = Path(destination)
    require(not output.is_absolute() and ".." not in output.parts, "materialization destination must be repository-relative")
    required_unknown = [edge for edge in bundle.get("unknown_edges", []) if edge.get("required", True)]
    require(not required_unknown and not bundle.get("missing_dependencies"),
            "task bundle has required unknown or missing dependency edges")
    preflight_materialization(root, bundle.get("references", []))
    output_parts = tuple(part for part in output.parts if part not in {"", "."})
    target_root = base.joinpath(*output_parts)
    invalid = [ref for ref in bundle["references"] if ref["required"] and ref["status"] != "current"]
    require(not invalid, f"required reference is {invalid[0]['status']}: {invalid[0]['path']}" if invalid else "")
    written, skipped, created_files, created_dirs = [], [], [], []
    root_fd = os.open(base, directory_flags())
    try:
        for ref in bundle["references"]:
            uncertain_dependency = next((edge for edge in bundle.get("unknown_edges", [])
                                          if edge.get("task_id") == ref.get("task_id")
                                          and edge.get("reference_id") == ref.get("id")
                                          and not edge.get("required", True)), None)
            if uncertain_dependency:
                skipped.append({"path": ref["path"],
                                "status": "dynamic" if uncertain_dependency["status"] == "dynamic" else "unknown_dependency"})
                continue
            if ref["status"] != "current":
                skipped.append({"path": ref["path"], "status": ref["status"]})
                continue
            reference_path = Path(ref["path"])
            require(not reference_path.is_absolute() and ".." not in reference_path.parts,
                    "reference path must be repository-relative")
            reference_parts = tuple(part for part in reference_path.parts if part not in {"", "."})
            require(reference_parts, "reference path must name a file")
            target_parent_fd = open_destination_directory(root_fd, output_parts + reference_parts[:-1], created_dirs)
            target_name = reference_parts[-1]
            try:
                try:
                    target_fd = os.open(target_name, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
                                        dir_fd=target_parent_fd)
                except FileNotFoundError:
                    src, _ = open_reference_file(str(base), ref["path"])
                    try:
                        target_fd = os.open(target_name,
                                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW,
                                            0o666, dir_fd=target_parent_fd)
                    except BaseException:
                        src.close()
                        raise
                    metadata = os.fstat(target_fd)
                    created_files.append((os.dup(target_parent_fd), target_name, metadata.st_dev, metadata.st_ino))
                    hasher = hashlib.sha256()
                    with src, os.fdopen(target_fd, "wb") as dst:
                        while chunk := src.read(STREAM_CHUNK_BYTES):
                            hasher.update(chunk)
                            dst.write(chunk)
                    require(hasher.hexdigest() == ref["sha256"],
                            f"source changed during materialization: {ref['path']}")
                else:
                    target_stream = None
                    try:
                        if not stat.S_ISREG(os.fstat(target_fd).st_mode):
                            raise InvalidEvent(f"materialization target is not a regular file: {ref['path']}")
                        target_stream = os.fdopen(target_fd, "rb")
                        with target_stream as existing:
                            hasher = hashlib.sha256()
                            while chunk := existing.read(STREAM_CHUNK_BYTES):
                                hasher.update(chunk)
                        require(hasher.hexdigest() == ref["sha256"],
                                f"materialization target already differs: {ref['path']}")
                    except BaseException:
                        if target_stream is not None and not target_stream.closed:
                            target_stream.close()
                        elif target_stream is None:
                            os.close(target_fd)
                        raise
            finally:
                os.close(target_parent_fd)
            written.append({"path": ref["path"], "sha256": ref["sha256"]})
    except BaseException:
        for parent_fd, name, device, inode in reversed(created_files):
            try:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (metadata.st_dev, metadata.st_ino) == (device, inode):
                    os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
        for parent_fd, name, device, inode in reversed(created_dirs):
            try:
                metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode) and (metadata.st_dev, metadata.st_ino) == (device, inode):
                    os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        os.close(root_fd)
        for parent_fd, *_ in created_files + created_dirs:
            os.close(parent_fd)
    return {"destination": str(target_root), "written": written, "skipped": skipped}


def completion_valid(model, task):
    if not artifacts_valid(model, task.get("artifacts")):
        return False
    try:
        result = receipt_file(model, {"receipt": task["result_receipt"]})
        if result.get("thread_id") != dispatch_thread(model, task):
            return False
        native = result["native_evidence"]
        if digest(canonical(model["project"]["root"], native["path"])) != native["sha256"]:
            return False
        if task.get("ack"):
            receipt_file(model, {"receipt": task["ack"]})
        return True
    except (OSError, KeyError, TypeError, ValueError):
        return False


def readiness(model, task):
    reasons = []
    try:
        claims = resolved_claims(model, task)
        if claims != task["writer_paths"]:
            reasons.append(f"claim-identity-changed:{task['id']}:replace task identity after reconciliation")
    except (OSError, ValueError, RuntimeError):
        claims = task["writer_paths"]
        reasons.append(f"claim-unresolvable:{task['id']}")
    for dep in task["dependencies"]:
        prerequisite = model["tasks"].get(dep)
        if not prerequisite or prerequisite["status"] != "COMPLETE":
            reasons.append(f"dependency:{dep}:{prerequisite['status'] if prerequisite else 'MISSING'}")
        elif not completion_valid(model, prerequisite):
            reasons.append(f"dependency:{dep}:ARTIFACT_DRIFT")
    if model["workers"].get(task["worker"], {}).get("state") != "IDLE":
        reasons.append(f"worker:{task['worker']}:not-observed-idle")
    for tid, other in model["tasks"].items():
        if tid == task["id"] or not other.get("lease"):
            continue
        if other["worker"] == task["worker"]:
            reasons.append(f"worker-lease:{tid}:{other['status']}")
        occupied_claims = set(other["writer_paths"])
        try:
            occupied_claims.update(resolved_claims(model, other))
            occupied_claims.update(canonical(model["project"]["root"], path) for path in other["writer_paths"])
        except (OSError, ValueError, RuntimeError):
            if claims:
                reasons.append(f"writer-lease:{tid}:claim-unresolvable:{other['status']}")
        for path in set(claims) | set(task["writer_paths"]):
            for occupied in occupied_claims:
                if Path(path).is_relative_to(occupied) or Path(occupied).is_relative_to(path):
                    reasons.append(f"writer-lease:{tid}:{occupied}:{other['status']}")
    return sorted(set(reasons))


def normalize_native(raw):
    """Accept actual MCP content text or structured payload, without invented IDs."""
    require(isinstance(raw, dict), "native receipt must be an object")
    require(raw.get("isError") is not True, "native receipt reports isError")
    if "threadId" in raw:
        require(string(raw["threadId"]), "native threadId must be a string")
        return raw
    structured = raw.get("structuredContent")
    require(structured is None or isinstance(structured, dict), "native structuredContent must be an object")
    if isinstance(structured, dict) and "threadId" in structured:
        require(string(structured["threadId"]), "native threadId must be a string")
        return structured
    content = raw.get("content", [])
    require(isinstance(content, list), "native content must be a list")
    for item in content:
        require(isinstance(item, dict), "native content item must be an object")
        if item.get("type") == "text":
            require(isinstance(item.get("text"), str), "native content text must be a string")
            try:
                payload = json.loads(item["text"])
            except (ValueError, KeyError):
                continue
            if isinstance(payload, dict) and "threadId" in payload:
                require(string(payload["threadId"]), "native threadId must be a string")
                return payload
    raise InvalidEvent("native receipt has no supported threadId payload")


def receipt_file(model, event):
    receipt = event["receipt"]
    validate_file(receipt)
    path = canonical(model["project"]["root"], receipt["path"])
    require(digest(path) == receipt["sha256"], "receipt hash mismatch")
    with open(path) as stream:
        value = json.load(stream)
    require(isinstance(value, dict), "receipt JSON must be an object")
    return value


def transition(state):
    model = copy.deepcopy(state.get("model", {}))
    event = state["event"]
    received_at = (state.get("event_received_at")
                   if state.get("event_received_at_event_id") == event.get("event_id")
                   else None)
    canonical_report_at = validate_event(event, received_at=received_at)
    op = event["op"]
    response = {"action": "OK"}
    project_policy = state.get("project_policy")
    if op not in {"status", "recover", "summary"}:
        require(isinstance(event.get("event_id"), str) and event["event_id"], "event_id required")
        event_hash = (state.get("event_hash")
                      if state.get("event_hash_event_id") == event["event_id"]
                      else event_fingerprint(event))
        prior = model.get("events", {}).get(event["event_id"])
        if prior:
            require(prior["sha256"] == event_hash, "event_id reused with conflicting payload")
            return {"model": model, "response": {"action": "REPLAY", "original_action": prior["action"]}}
    if op == "init":
        project = copy.deepcopy(event["project"])
        project["root"] = str(Path(project["root"]).resolve(strict=True))
        for field in ("project_id", "linear_ref", "beads_ref", "coordinator_thread_id"):
            require(isinstance(project.get(field), str) and project[field], f"project.{field} required")
        require(isinstance(project["workers"], dict) and bool(project["workers"])
                and all(worker_role(role) for role in project["workers"]),
                "workers must bind nonempty safe role names")
        ids = list(project["workers"].values()) + [project["coordinator_thread_id"]]
        require(all(native_thread_id(value) for value in ids) and len(set(ids)) == len(ids),
                "worker and coordinator thread IDs must be nonempty and distinct")
        if model:
            require(model["project"] == project, "database already bound to a different project")
        else:
            model = {"project": project, "tasks": {}, "workers": {}, "events": {}, "revision": 0}
    else:
        require(model, "init required")
        require(event.get("project_id") == model["project"]["project_id"], "project_id binding mismatch")
        require(str(Path(event.get("root", "")).resolve()) == model["project"]["root"], "root binding mismatch")
        # An earlier ISSUING transition may have exposed a tool request. Never repeat it.
        for task in model["tasks"].values():
            if op not in {"rebind", "register", "report", "project_report", "refresh_references"} and task["status"] == "ISSUING":
                task["status"] = "UNKNOWN"
                task["reason"] = "native send uncertain; reconcile retained receipts"
        if op == "add":
            task = copy.deepcopy(event["task"])
            for field in ("id", "linear_ref", "beads_ref", "worker", "prompt", "source_digest", "scope_digest"):
                require(isinstance(task.get(field), str) and task[field], f"task.{field} required")
            require(task["worker"] in model["project"]["workers"], "unbound worker")
            require(isinstance(task.get("dependencies"), list), "dependencies list required")
            require(task["id"] not in task["dependencies"], "self dependency")
            require(isinstance(task.get("writer_paths"), list), "writer_paths list required; [] for read-only")
            task["writer_claims"] = task["writer_paths"][:]
            task["writer_paths"] = resolved_claims(model, task)
            task.setdefault("references", [])
            for ref in task["references"]:
                ref.setdefault("depends_on", [])
                ref.setdefault("dependency_status", "dynamic" if ref["dynamic"] else "unknown")
            reject_reference_cycles(task["references"])
            known_ids = {ref["id"] for ref in task["references"]}
            require(all(dep["id"] in known_ids for ref in task["references"] if ref["dependency_status"] == "known"
                        for dep in ref["depends_on"]), "known reference dependencies must be present in the task")
            require(task["id"] not in model["tasks"], "task already exists; use prepare for reuse")
            task["prompt_sha256"] = hashlib.sha256(task["prompt"].encode()).hexdigest()
            task.update(status="WAIT", lease=False)
            model["tasks"][task["id"]] = task
            reject_cycles(model["tasks"])
        elif op == "refresh_references":
            require(event["task_id"] in model["tasks"], "reference refresh task is not bound to this project")
            task = model["tasks"][event["task_id"]]
            require(task["source_digest"] == event["source_digest"]
                    and task["scope_digest"] == event["scope_digest"],
                    "reference refresh task source or scope digest mismatch")
            existing_refs = copy.deepcopy(task.get("references", []))
            previous_revision = task.get("reference_revision")
            previous_digest = reference_set_digest(existing_refs)
            require(task.get("reference_digest", previous_digest) == previous_digest,
                    "stored reference index digest is inconsistent")
            require(event["expected_reference_revision"] == previous_revision
                    and event["expected_reference_digest"] == previous_digest,
                    "reference refresh compare-and-swap mismatch")
            require(event["reference_revision"] != previous_revision,
                    "reference revision must advance")
            new_refs = copy.deepcopy(event["references"])
            for ref in new_refs:
                ref.setdefault("depends_on", [])
                ref.setdefault("dependency_status", "dynamic" if ref["dynamic"] else "unknown")
            reject_reference_cycles(new_refs)
            known_ids = {ref["id"] for ref in new_refs}
            require(all(dep["id"] in known_ids for ref in new_refs if ref["dependency_status"] == "known"
                        for dep in ref["depends_on"]),
                    "known reference dependencies must be present in the task")
            new_digest = reference_set_digest(new_refs)
            task["references"] = new_refs
            task["reference_revision"] = event["reference_revision"]
            task["reference_digest"] = new_digest
            response = {"action": "REFERENCES_REFRESHED", "task_id": event["task_id"],
                        "reference_revision": event["reference_revision"], "reference_digest": new_digest}
        elif op == "report":
            require(event["task_id"] in model["tasks"], "report task is not bound to this project")
            task = model["tasks"][event["task_id"]]
            require(event["reporter"] == task["worker"], "reporter does not own the bound task")
            require(event["reporter_thread_id"] == model["project"]["workers"][event["reporter"]], "reporter thread binding mismatch")
            require(event["claim_id"] == task["beads_ref"], "report claim binding mismatch")
            if "source_digest" in event:
                require(event["source_digest"] == task["source_digest"], "report source identity mismatch")
            if "dispatch_id" in event:
                require(event["dispatch_id"] == task.get("dispatch_id"), "report dispatch binding mismatch")
            recorded = datetime.now(timezone.utc).isoformat()
            report = {k: copy.deepcopy(event[k]) for k in ("event_id", "task_id", "reporter", "reporter_thread_id", "kind", "occurred_at", "state", "detail", "source", "evidence", "claim_id", "dispatch_id") if k in event}
            for field in ("state", "detail", "source"):
                report[field] = redact_report_text(report[field])
            report["evidence"] = [redact_report_text(link) for link in report["evidence"]]
            report["recorded_at"] = recorded
            model.setdefault("reports", []).append(report)
            response = {"action": "REPORTED", "event_id": event["event_id"], "recorded_at": recorded}
        elif op == "project_report":
            if event["reporter"] == "coordinator":
                require(event["reporter_thread_id"] == model["project"]["coordinator_thread_id"], "coordinator reporter thread binding mismatch")
            elif event["reporter"] in model["project"]["workers"]:
                require(event["reporter_thread_id"] == model["project"]["workers"][event["reporter"]], "project worker reporter thread binding mismatch")
            else:
                require(event["reporter"] not in model["project"]["workers"]
                        and event["reporter_thread_id"] not in [model["project"]["coordinator_thread_id"], *model["project"]["workers"].values()],
                        "project report registrations must be separate from coordinator and task workers")
                policy = validate_project_policy(state.get("project_policy"), event) if state.get("project_policy") is not None else None
                require(policy is not None and any(item["role"] == event["reporter"] and item["thread_id"] == event["reporter_thread_id"]
                                                   for item in policy.get("project_report_registrations", [])),
                        "project reporter is not registered")
            recipients = event.get("recipients", [])
            if recipients:
                policy = validate_project_policy(state.get("project_policy"), event) if state.get("project_policy") is not None else None
                tasks = [task for task in model["tasks"].values()
                         if work_item_matches(event["work_item"], task)]
                require(len(tasks) == 1, "directed project report must resolve to exactly one bound task")
                task = tasks[0]
                for recipient in recipients:
                    if recipient in model["project"]["workers"]:
                        require(task["worker"] == recipient,
                                "worker recipient must own the directed task")
                    else:
                        require(policy is not None and any(item["role"] == recipient for item in
                                                           policy.get("project_report_registrations", [])),
                                "directed recipient is not registered")
            report = redact_project_report_event(event)
            report["occurred_at"] = canonical_report_at
            report["recorded_at"] = datetime.now(timezone.utc).isoformat()
            model.setdefault("project_reports", []).append(report)
            response = {"action": "PROJECT_REPORTED", "event_id": event["event_id"], "recorded_at": report["recorded_at"]}
        elif op == "register":
            policy = validate_project_policy(project_policy, event)
            require(any(item["worker"] == event["worker"] and item["thread_id"] == event["new_thread_id"]
                        for item in policy["worker_registrations"]),
                    "registration is not authorized by project policy")
            bindings = model["project"]["workers"]
            require(event["worker"] not in bindings and event["worker"] not in model["workers"],
                    "worker already bound or observed")
            require(bindings == event["expected_workers"], "expected worker bindings mismatch")
            require(event["new_thread_id"] not in [*bindings.values(), model["project"]["coordinator_thread_id"]], "registered thread must be distinct")
            before = json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
            bindings[event["worker"]] = event["new_thread_id"]
            model["workers"][event["worker"]] = {"state": "UNKNOWN", "evidence": event["evidence"]}
            protected = copy.deepcopy(model)
            del protected["project"]["workers"][event["worker"]]
            del protected["workers"][event["worker"]]
            after = json.dumps(protected, sort_keys=True, separators=(",", ":")).encode()
            require(before == after, "registration changed protected coordinator state")
            response = {"action": "REGISTERED", "worker": event["worker"], "new_thread_id": event["new_thread_id"],
                        "preservation": {"before_sha256": hashlib.sha256(before).hexdigest(),
                                         "after_sha256": hashlib.sha256(after).hexdigest(),
                                         "task_count": len(protected["tasks"]),
                                         "worker_binding_count": len(protected["project"]["workers"]),
                                         "worker_observation_count": len(protected["workers"]),
                                         "event_count": len(protected["events"])}}
        elif op == "rebind":
            worker = event["worker"]
            bindings = model["project"]["workers"]
            require(worker in bindings, "unbound worker")
            require(bindings[worker] == event["expected_old_thread_id"], "expected old thread binding mismatch")
            require(event["new_thread_id"] not in [*bindings.values(), model["project"]["coordinator_thread_id"]], "replacement thread must be distinct")
            require(model["workers"].get(worker, {}).get("state") != "BUSY", "worker observed busy; reconcile first")
            require(not any(t["worker"] == worker and (t.get("lease") or t["status"] in {"PREPARED", "ISSUING", "UNKNOWN", "ACKNOWLEDGED"}) for t in model["tasks"].values()), "worker has held or uncertain dispatch; reconcile first")
            # Freeze legacy receipts to their original target before changing the role.
            for task in model["tasks"].values():
                if task["worker"] == worker and task.get("dispatch_id"):
                    task.setdefault("dispatch_thread_id", bindings[worker])
            bindings[worker] = event["new_thread_id"]
            model["workers"][worker] = {"state": "UNKNOWN", "evidence": event["evidence"]}
            model.setdefault("rebindings", []).append({k: event[k] for k in ("event_id", "worker", "expected_old_thread_id", "new_thread_id", "evidence")})
            response = {"action": "REBOUND", "worker": worker, "old_thread_id": event["expected_old_thread_id"], "new_thread_id": event["new_thread_id"]}
        elif op == "observe":
            worker = event["worker"]
            require(worker in model["project"]["workers"], "unbound worker")
            require(event["state"] in {"IDLE", "BUSY", "UNKNOWN"}, "invalid worker state")
            require(isinstance(event.get("evidence"), str) and event["evidence"], "observation evidence reference required")
            model["workers"][worker] = {"state": event["state"], "evidence": event["evidence"]}
        elif op == "retire":
            require(event.get("task_id") in model["tasks"], "unknown task_id")
            task = model["tasks"][event["task_id"]]
            require(event["source_digest"] == task["source_digest"] and event["scope_digest"] == task["scope_digest"], "source/scope identity mismatch")
            require(task["status"] in {"WAIT", "READY"} and task.get("dispatch_id") is None
                    and task.get("dispatch_thread_id") is None and task.get("lease") is False,
                    "retire requires a never-dispatched WAIT/READY task without a lease")
            task.update(status="RETIRED", lease=False, retirement={
                "event_id": event["event_id"], "source_digest": event["source_digest"],
                "scope_digest": event["scope_digest"], "quiescent": True, "evidence": event["evidence"]})
            response = {"action": "RETIRED", "task_id": task["id"], "status": task["status"], "lease": False}
        elif op in {"prepare", "issue", "ack", "result", "reconcile"}:
            require(event.get("task_id") in model["tasks"], "unknown task_id")
            task = model["tasks"][event["task_id"]]
            if op in {"prepare", "issue"}:
                require(task["status"] != "RETIRED", "retired task cannot be prepared or issued; use a new task identity")
            if op == "prepare":
                require(event.get("source_digest") == task["source_digest"] and event.get("scope_digest") == task["scope_digest"], "source/scope identity mismatch; no reuse or dispatch")
                if task["status"] == "COMPLETE":
                    require(completion_valid(model, task), "completed artifact/receipt drift; immutable completion requires a new authorized task identity and replacement dependents")
                    response = {"action": "REUSE", "task_id": task["id"], "dispatch_id": task["dispatch_id"]}
                elif task.get("dispatch_id"):
                    response = {"action": "RECONCILE", "task_id": task["id"], "status": task["status"]}
                else:
                    reasons = readiness(model, task)
                    if reasons:
                        response = {"action": "WAIT", "reasons": reasons}
                    else:
                        task.update(status="PREPARED", lease=True, dispatch_id=str(uuid.uuid4()),
                                    dispatch_thread_id=model["project"]["workers"][task["worker"]])
                        response = {"action": "PREPARED", "task_id": task["id"], "dispatch_id": task["dispatch_id"]}
            else:
                require(event.get("dispatch_id") == task.get("dispatch_id") and task.get("dispatch_id"), "dispatch binding mismatch")
                if op == "issue":
                    if task["status"] == "PREPARED":
                        reasons = readiness(model, task)
                        if reasons:
                            response = {"action": "WAIT", "reasons": reasons}
                        else:
                            task["status"] = "ISSUING"
                            model["workers"][task["worker"]] = {"state": "BUSY", "evidence": f"dispatch:{task['dispatch_id']}"}
                            response = {"action": "ISSUING", "task_id": task["id"], "dispatch_id": task["dispatch_id"]}
                    else:
                        response = {"action": "RECONCILE", "status": task["status"]}
                elif op == "ack":
                    task.setdefault("dispatch_thread_id", model["project"]["workers"][task["worker"]])
                    require(task["status"] in {"UNKNOWN", "ACKNOWLEDGED"}, "ack requires issued dispatch")
                    native = normalize_native(receipt_file(model, event))
                    require(native["threadId"] == dispatch_thread(model, task), "native target mismatch")
                    task.update(status="ACKNOWLEDGED", ack=event["receipt"])
                elif op == "result":
                    task.setdefault("dispatch_thread_id", model["project"]["workers"][task["worker"]])
                    require(task.get("lease") is True, "dispatch lease already released; late result rejected")
                    require(task["status"] in {"UNKNOWN", "ACKNOWLEDGED", "FAILED"}, "result requires issued dispatch")
                    result = receipt_file(model, event)
                    require(result.get("thread_id") == dispatch_thread(model, task), "result target mismatch")
                    require(result.get("dispatch_id") == task["dispatch_id"], "result dispatch mismatch")
                    require(result.get("source_digest") == task["source_digest"] and result.get("scope_digest") == task["scope_digest"], "result source/scope identity mismatch")
                    require(isinstance(result.get("native_evidence"), dict), "native_evidence file/hash required")
                    validate_file(result["native_evidence"])
                    native_path = canonical(model["project"]["root"], result["native_evidence"]["path"])
                    require(digest(native_path) == result["native_evidence"]["sha256"], "native evidence drift")
                    with open(native_path) as stream:
                        native_text = stream.read()
                    require(task["dispatch_id"] in native_text, "native evidence lacks dispatch marker")
                    require(dispatch_thread(model, task) in native_text, "native evidence lacks target thread")
                    terminal = result.get("terminal") is True
                    quiescent = terminal and result.get("quiescent") is True
                    success = (quiescent and type(result.get("child_exit_code")) is int
                               and result["child_exit_code"] == 0 and result.get("acceptance") is True
                               and artifacts_valid(model, result.get("artifacts")))
                    task.update(status="COMPLETE" if success else "FAILED" if terminal else "UNKNOWN",
                                lease=not (success or quiescent), result_receipt=event["receipt"])
                    if success:
                        task["artifacts"] = result["artifacts"]
                    response = {"action": task["status"], "lease": task["lease"]}
                else:
                    require(event.get("quiescent") is True and isinstance(event.get("evidence"), str) and event["evidence"], "reconcile requires quiescence evidence")
                    require(task["status"] in {"UNKNOWN", "FAILED", "PREPARED", "ACKNOWLEDGED"}, "cannot abandon immutable complete task")
                    task.update(status="FAILED", lease=False, reason=event["evidence"])
                    response = {"action": "FAILED", "lease": False}
        elif op not in {"status", "recover", "summary"}:
            raise InvalidEvent(f"unknown op: {op}")
    if op not in {"status", "recover", "summary", "bundle", "updates"}:
        revision = model.get("revision", 0) + 1
        model["revision"] = revision
        task_id = event.get("task_id") or (event.get("task") or {}).get("id")
        task = model.get("tasks", {}).get(task_id)
        actor_role = event.get("reporter") or event.get("worker") or "coordinator"
        if task is not None and response["action"] not in {"RECONCILE", "REUSE"}:
            task["revision"] = revision
            task["last_event_role"] = actor_role
            task["last_event_id"] = event["event_id"]
        if op == "report" and model.get("reports"):
            model["reports"][-1]["revision"] = revision
        if op == "project_report" and model.get("project_reports"):
            model["project_reports"][-1]["revision"] = revision
        model["events"][event["event_id"]] = {"sha256": event_hash, "action": response["action"],
                                               "revision": revision, "task_id": task_id,
                                               "role": actor_role}
        response["revision"] = revision
    return {"model": model, "response": response}


def compute_readiness(state):
    model = copy.deepcopy(state["model"])
    if state.get("event", {}).get("op") == "register":
        return {"model": model}
    event = state.get("event", {})
    event_id = event.get("event_id")
    revision = model.get("revision", 0)
    actor_role = event.get("reporter") or event.get("worker") or "coordinator"
    for task in model["tasks"].values():
        if task["status"] in {"WAIT", "READY"}:
            previous_status = task["status"]
            previous_reasons = task.get("reasons", [])
            reasons = readiness(model, task)
            status = "WAIT" if reasons else "READY"
            task["reasons"] = reasons
            task["status"] = status
            if event_id and (status != previous_status or reasons != previous_reasons):
                task["revision"] = revision
                task["last_event_role"] = actor_role
                task["last_event_id"] = event_id
    return {"model": model}


def build_graph(saver):
    builder = StateGraph(State)
    builder.add_node("transition", transition)
    builder.add_node("readiness", compute_readiness)
    builder.add_edge(START, "transition")
    builder.add_edge("transition", "readiness")
    builder.add_edge("readiness", END)
    return builder.compile(checkpointer=saver)


def apply_event(db, event, *, guard_context=None, project_policy=None):
    """Commit all checkpoints synchronously before returning any native intent.

    Safe to kill the caller after this returns: subsequent calls never return the
    same native request. New reservations/issue intents require native Beads
    guard_context; historical replay/reuse/reconciliation does not. No promise
    of exactly-once delivery across MCP transport.
    """
    event_received_at = datetime.now(timezone.utc).isoformat()
    canonical_occurred_at = validate_event(event, received_at=event_received_at)
    event_hash = event_fingerprint(event)
    persisted_event = redact_report_event(event) if event["op"] == "report" else redact_project_report_event(event) if event["op"] == "project_report" else event
    if event["op"] == "report":
        persisted_event["occurred_at"] = canonical_occurred_at
    if event["op"] == "project_report":
        persisted_event["occurred_at"] = canonical_occurred_at
    if project_policy is not None:
        binding = event["project"] if event["op"] == "init" else event
        validate_project_policy(project_policy, binding)
    if event["op"] == "register":
        validate_project_policy(project_policy, event)
    if guard_context is not None:
        require(event["op"] in {"prepare", "issue"}, "Beads guard supports prepare and issue only")
        require(isinstance(guard_context, dict) and set(guard_context) == {"beads_dir", "assignee", "executable"},
                "Beads guard context requires exactly beads_dir, assignee and executable")
    db = Path(db).resolve()
    if event["op"] == "summary":
        return read_status(db, event)
    if event["op"] in {"bundle", "updates"}:
        model, checkpoint_id = read_model(db, event)
        if event["op"] == "bundle":
            result = task_bundle(model, event["task_id"], role=event.get("role"), thread_id=event.get("thread_id"),
                                 project_policy=project_policy)
        else:
            result = read_updates(model, event, project_policy=project_policy)
        result["checkpoint_id"] = checkpoint_id
        return result
    db.parent.mkdir(parents=True, exist_ok=True)
    with open(str(db) + ".lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with sqlite3.connect(db, check_same_thread=False) as conn:
            conn.execute("PRAGMA synchronous=FULL")
            saver = SqliteSaver(conn)
            graph = build_graph(saver)
            config = {"configurable": {"thread_id": "project-coordinator-v1"}}
            snapshot = graph.get_state(config)
            resumed = bool(snapshot.next)
            require(not (resumed and event["op"] in {"rebind", "register", "prepare", "issue"}), "pending checkpoint; recover and reconcile before prepare/issue/rebind/register")
            if resumed:
                graph.invoke(None, config, durability="sync")
            # Validate before recording input: an invalid event cannot poison recovery.
            before = graph.get_state(config).values
            candidate = transition({"model": before.get("model", {}), "event": persisted_event, "event_hash": event_hash,
                                    "event_hash_event_id": event.get("event_id", ""),
                                    "event_received_at": event_received_at,
                                    "event_received_at_event_id": event.get("event_id", ""),
                                    "project_policy": project_policy})
            compute_readiness({**candidate, "event": event})
            guard = None
            inputs = {"event": persisted_event, "event_hash": event_hash,
                      "event_hash_event_id": event.get("event_id", ""),
                      "event_received_at": event_received_at,
                      "event_received_at_event_id": event.get("event_id", "")}
            if event["op"] in {"register", "project_report"}:
                inputs["project_policy"] = project_policy
            if event["op"] in {"prepare", "issue"} and candidate["response"]["action"] != "REPLAY":
                task = before["model"]["tasks"][event["task_id"]]
                # Readiness may change externally between preview and commit; gate the stable dispatch state.
                if (event["op"] == "prepare" and not task.get("dispatch_id")) or (event["op"] == "issue" and task["status"] == "PREPARED"):
                    require(guard_context is not None, "new prepare/issue requires explicit Beads guard context")
                    from .beads import check_beads
                    guard = check_beads(db, event, project_policy=project_policy, **guard_context)
                    claim = {field: guard[field] for field in ("beads_id", "assignee", "linear_ref")}
                    if event["op"] == "issue":
                        require(task.get("beads_claim") == claim,
                                "prepared Beads claim missing or changed; explicit Root reconciliation required")
                    else:
                        # The input checkpoint must retain the pin even if preparation is interrupted.
                        inputs["model"] = copy.deepcopy(before["model"])
                        inputs["model"]["tasks"][event["task_id"]]["beads_claim"] = claim
            state = graph.invoke(inputs, config, durability="sync")
            conn.commit()
            response = copy.deepcopy(state["response"])
            if guard is not None:
                response["beads_preflight"] = guard
            response["checkpoint_id"] = graph.get_state(config).config["configurable"]["checkpoint_id"]
            if event["op"] == "issue" and response["action"] == "ISSUING":
                task = state["model"]["tasks"][event["task_id"]]
                response["native_request"] = {
                    "tool": "mcp__codex_app__send_message_to_thread",
                    "arguments": {"threadId": dispatch_thread(state["model"], task),
                                  "prompt": f"[dispatch_id:{task['dispatch_id']}]\n{task['prompt']}"},
                }
            if event["op"] in {"status", "recover"}:
                response["project"] = state["model"]["project"]
                response["workers"] = state["model"]["workers"]
                response["tasks"] = [{k: v for k, v in task.items() if k not in {"prompt"}}
                                     for task in state["model"]["tasks"].values()]
                response["resumed_checkpoint"] = resumed
            return response


def compact_status(state, event, checkpoint_id=None, pending=()):
    """Bounded projection; never exposes stored events, prompts or native arguments."""
    model = state["model"]
    tasks = sorted(model["tasks"].values(), key=lambda t: (not t.get("lease", False), t["id"]))
    offset, limit = event.get("offset", 0), event.get("limit", 50)
    truncated = set()

    def bounded(value, path):
        if isinstance(value, str) and len(value) > 512:
            truncated.add(path)
            return value[:512]
        if isinstance(value, list):
            if len(value) > 32:
                truncated.add(path)
            return [bounded(v, path) for v in value[:32]]
        if isinstance(value, dict):
            return {k: bounded(v, f"{path}.{k}") for k, v in value.items()}
        return value

    fields = {"id", "worker", "status", "lease", "dispatch_id", "dispatch_thread_id", "linear_ref", "beads_ref", "dependencies", "writer_paths", "reasons"}
    page = []
    for task in tasks[offset:offset + limit]:
        item = {k: v for k, v in task.items() if k in fields}
        if task["status"] == "ISSUING":
            item["status"] = "UNKNOWN"
        page.append(bounded(item, f"tasks.{offset + len(page)}"))
    roles = {role for role in model["project"]["workers"] if worker_role(role)}
    all_roles = set(model["project"]["workers"]) | set(model["workers"])
    if all_roles - roles:
        truncated.add("workers.extra_roles")
    project = {k: model["project"][k] for k in ("project_id", "root", "linear_ref", "beads_ref", "coordinator_thread_id")}
    project["workers"] = {k: model["project"]["workers"][k] for k in sorted(roles)}
    uncertain = sum(bool(t.get("lease")) and t["status"] in {"ISSUING", "UNKNOWN", "FAILED"} for t in tasks)
    reports = sorted(model.get("reports", []), key=lambda r: (datetime.fromisoformat(r["occurred_at"].replace("Z", "+00:00")).astimezone(timezone.utc), r["event_id"]))
    latest_report_by_task = {}
    for report in reports:
        latest_report_by_task[report["task_id"]] = report
    report_offset, report_limit = event.get("report_offset", 0), event.get("report_limit", 50)
    report_end = max(0, len(reports) - report_offset)
    report_start = max(0, report_end - report_limit)
    report_page = list(reversed(reports[report_start:report_end]))
    bounded_reports = [bounded({**{k: v for k, v in report.items() if k in {"event_id", "task_id", "reporter", "reporter_thread_id", "kind", "occurred_at", "recorded_at", "state", "detail", "source", "evidence", "claim_id", "dispatch_id"}}, "stale": latest_report_by_task[report["task_id"]]["event_id"] != report["event_id"]}, "reports") for report in report_page]
    project_reports = sorted(model.get("project_reports", []), key=lambda r: (datetime.fromisoformat(r["occurred_at"].replace("Z", "+00:00")).astimezone(timezone.utc), r["event_id"]))
    latest_project_report = {(r["reporter"], r["work_item"]): r for r in project_reports}
    project_report_offset, project_report_limit = event.get("project_report_offset", 0), event.get("project_report_limit", 50)
    project_report_end = max(0, len(project_reports) - project_report_offset)
    project_report_start = max(0, project_report_end - project_report_limit)
    project_report_page = list(reversed(project_reports[project_report_start:project_report_end]))
    bounded_project_reports = [bounded({**{k: r[k] for k in ("event_id", "work_item", "reporter", "reporter_thread_id", "kind", "occurred_at", "recorded_at", "state", "detail", "source", "evidence")}, "stale": latest_project_report[(r["reporter"], r["work_item"])]["event_id"] != r["event_id"]}, "project_reports") for r in project_report_page]
    for task in page:
        latest = latest_report_by_task.get(task["id"])
        if latest:
            task["latest_report"] = bounded(
                {**{k: latest[k] for k in ("event_id", "reporter", "kind", "occurred_at", "recorded_at", "state")}, "stale": False},
                f"tasks.{task['id']}.latest_report")
    return {"action": "OK", "checkpoint_id": checkpoint_id, "revision": model.get("revision", 0),
            "project": bounded(project, "project"),
            "workers": bounded({k: {"state": model["workers"][k]["state"]}
                                 for k in sorted(roles) if k in model["workers"]}, "workers"),
            "tasks": page, "reports": bounded_reports, "project_reports": bounded_project_reports, "observed_at": datetime.now(timezone.utc).isoformat(),
            "resumed_checkpoint": False, "recovery_required": bool(pending) or bool(uncertain),
            "pending_checkpoint": bool(pending), "uncertain_held_dispatches": uncertain,
            "bounds": {"offset": offset, "limit": limit, "total_tasks": len(tasks),
                       "next_offset": offset + limit if offset + limit < len(tasks) else None,
                       "report_offset": report_offset, "report_limit": report_limit,
                       "total_reports": len(reports), "omitted_reports": max(0, len(reports) - len(bounded_reports)),
                       "next_report_offset": report_offset + report_limit if report_offset + report_limit < len(reports) else None,
                       "previous_report_offset": max(0, report_offset - report_limit) if report_offset > 0 else None,
                       "project_report_offset": project_report_offset, "project_report_limit": project_report_limit,
                       "total_project_reports": len(project_reports), "omitted_project_reports": max(0, len(project_reports) - len(bounded_project_reports)),
                       "next_project_report_offset": project_report_offset + project_report_limit if project_report_offset + project_report_limit < len(project_reports) else None,
                       "previous_project_report_offset": max(0, project_report_offset - project_report_limit) if project_report_offset > 0 else None,
                       "string_chars": 512, "list_items": 32, "worker_role_count": len(all_roles),
                       "omitted_worker_roles": len(all_roles - roles), "truncated_fields": sorted(truncated)}}


def read_status(db, event):
    # Skip saver.setup: its journal/schema initialization is a write operation.
    require(db.is_file(), "init required")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, check_same_thread=False)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        saver = SqliteSaver(conn)
        # Setup normally detects this legacy schema, but a summary must never migrate it.
        saver._has_task_path = any(row[1] == "task_path" for row in conn.execute("PRAGMA table_info(writes)"))
        saver.is_setup = True
        graph = build_graph(saver)
        snapshot = graph.get_state({"configurable": {"thread_id": "project-coordinator-v1"}})
        state = snapshot.values
        require(bool(state.get("model")), "init required")
        model = state["model"]
        require(event["project_id"] == model["project"]["project_id"], "project_id binding mismatch")
        require(str(Path(event["root"]).resolve()) == model["project"]["root"], "root binding mismatch")
        # Compute a view only: no checkpoint resume, reconciliation or dispatch.
        view = transition({"model": model, "event": event})
        view.update(compute_readiness(view))
        checkpoint = snapshot.config["configurable"]["checkpoint_id"]
        return compact_status(view, event, checkpoint, snapshot.next)


def read_model(db, event):
    db = Path(db).resolve()
    require(db.is_file(), "init required")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, check_same_thread=False)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        saver = SqliteSaver(conn)
        saver._has_task_path = any(row[1] == "task_path" for row in conn.execute("PRAGMA table_info(writes)"))
        saver.is_setup = True
        snapshot = build_graph(saver).get_state({"configurable": {"thread_id": "project-coordinator-v1"}})
        model = snapshot.values.get("model")
        require(bool(model), "init required")
        require(event["project_id"] == model["project"]["project_id"], "project_id binding mismatch")
        require(str(Path(event["root"]).resolve()) == model["project"]["root"], "root binding mismatch")
        return model, snapshot.config["configurable"]["checkpoint_id"]


def read_updates(model, event, project_policy=None):
    role, thread_id = event["role"], event["thread_id"]
    after = event.get("after_revision", 0)
    require(after <= model.get("revision", 0), "updates revision is ahead of coordinator state")
    if role == "coordinator":
        require(thread_id == model["project"]["coordinator_thread_id"], "coordinator thread binding mismatch")
        reports_by_task = {}
        project_only = {}
        for report in [*model.get("reports", []), *model.get("project_reports", [])]:
            revision = report.get("revision", 0)
            if revision <= after:
                continue
            task = (model["tasks"].get(report.get("task_id")) if "task_id" in report else
                    next((t for t in model["tasks"].values() if work_item_matches(report.get("work_item"), t)), None))
            if task:
                if task.get("status") in {"COMPLETE", "RETIRED"} and task.get("revision", 0) > revision:
                    continue
                current = reports_by_task.get(task["id"])
                if current is None or revision > current.get("revision", 0):
                    reports_by_task[task["id"]] = report
            else:
                key = report.get("work_item")
                current = project_only.get(key)
                if current is None or revision > current.get("revision", 0):
                    project_only[key] = report
        updates = [{"event_id": r["event_id"], "task_id": task_id, "role": r["reporter"],
                    "kind": r["kind"], "revision": r.get("revision", 0), "recorded_at": r["recorded_at"]}
                   for task_id, r in reports_by_task.items()]
        updates.extend({"event_id": r["event_id"], "task_id": r["work_item"], "role": r["reporter"],
                        "kind": r["kind"], "revision": r.get("revision", 0), "recorded_at": r["recorded_at"]}
                       for r in project_only.values())
        for task in model["tasks"].values():
            if (task.get("revision", 0) > after and task.get("last_event_role") != role
                    and task["id"] not in reports_by_task):
                event_id = task.get("last_event_id") or next((eid for eid, info in model.get("events", {}).items()
                                 if info.get("task_id") == task["id"] and info.get("revision") == task["revision"]), None)
                updates.append({"event_id": event_id, "task_id": task["id"],
                                "role": task.get("last_event_role", "coordinator"), "kind": "task_state",
                                "status": task["status"], "reasons": task.get("reasons", []),
                                "revision": task["revision"]})
    elif role in model["project"]["workers"]:
        require(model["project"]["workers"].get(role) == thread_id, "worker thread binding mismatch")
        updates = []
        for task_id, task in model["tasks"].items():
            if task["worker"] == role and task.get("revision", 0) > after and task.get("last_event_role") != role:
                event_id = task.get("last_event_id") or next((eid for eid, info in model.get("events", {}).items()
                                 if info.get("task_id") == task_id and info.get("revision") == task["revision"]), None)
                updates.append({"event_id": event_id, "task_id": task_id, "role": role, "status": task["status"],
                                "reasons": task.get("reasons", []), "revision": task["revision"], "kind": "task_state"})
        relevant = {}
        for report in model.get("project_reports", []):
            task = next((t for t in model["tasks"].values() if work_item_matches(report.get("work_item"), t)), None)
            revision = report.get("revision", 0)
            if (revision <= after or not task or task["worker"] != role or report["reporter"] == role
                    or (task.get("status") in {"COMPLETE", "RETIRED"} and task.get("revision", 0) > revision)):
                continue
            current = relevant.get(task["id"])
            if current is None or revision > current.get("revision", 0):
                relevant[task["id"]] = report
        for task_id, report in relevant.items():
            updates.append({"event_id": report["event_id"], "task_id": task_id,
                            "role": report["reporter"], "kind": report["kind"],
                            "revision": report["revision"], "recorded_at": report["recorded_at"]})
    else:
        policy = validate_project_policy(project_policy, model["project"]) if project_policy is not None else None
        require(policy is not None and any(item["role"] == role and item["thread_id"] == thread_id
                                           for item in policy.get("project_report_registrations", [])),
                "project role thread binding mismatch")
        reports_by_task = {}
        for report in model.get("project_reports", []):
            revision = report.get("revision", 0)
            if revision <= after or role not in report.get("recipients", []):
                continue
            task = next((task for task in model["tasks"].values()
                         if work_item_matches(report.get("work_item"), task)), None)
            if task is None or (task.get("status") in {"COMPLETE", "RETIRED"}
                                and task.get("revision", 0) > revision):
                continue
            previous = reports_by_task.get(task["id"])
            if previous is None or revision > previous.get("revision", 0):
                reports_by_task[task["id"]] = report
        updates = [{"event_id": report["event_id"], "task_id": task_id,
                    "role": report["reporter"], "kind": report["kind"],
                    "revision": report.get("revision", 0), "recorded_at": report["recorded_at"]}
                   for task_id, report in reports_by_task.items()]
    updates.sort(key=lambda item: item["revision"])
    return {"action": "OK", "role": role, "after_revision": after,
            "revision": model.get("revision", 0), "changed": bool(updates), "updates": updates}
