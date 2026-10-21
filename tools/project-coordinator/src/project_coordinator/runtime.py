"""Deterministic LangGraph transitions guarded by one filesystem lock per DB."""
from __future__ import annotations

import copy
from contextlib import closing
import fcntl
import hashlib
import json
import re
from pathlib import Path
import sqlite3
from typing import TypedDict
import uuid

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver


class State(TypedDict, total=False):
    model: dict
    event: dict
    response: dict


class InvalidEvent(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise InvalidEvent(message)


def string(value):
    return isinstance(value, str) and bool(value)


def worker_role(value):
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is not None


def validate_event(event):
    require(isinstance(event, dict), "event must be an object")
    op = event.get("op")
    require(isinstance(op, str) and op in {"init", "add", "observe", "prepare", "issue", "ack", "result", "reconcile", "status", "recover", "rebind", "summary"}, "invalid event op")
    if op not in {"status", "recover", "summary"}:
        require(string(event.get("event_id")), "event_id required")
    if op == "init":
        project = event.get("project")
        require(isinstance(project, dict), "project must be an object")
        for field in ("root", "project_id", "linear_ref", "beads_ref", "coordinator_thread_id"):
            require(string(project.get(field)), f"project.{field} required")
        workers = project.get("workers")
        require(isinstance(workers, dict) and bool(workers) and all(worker_role(role) for role in workers), "workers must bind nonempty safe role names")
        require(all(string(value) for value in workers.values()), "worker thread IDs must be strings")
    else:
        require(string(event.get("project_id")) and string(event.get("root")), "project_id and root required")
    if op == "add":
        task = event.get("task")
        require(isinstance(task, dict), "task must be an object")
        fields = {"id", "linear_ref", "beads_ref", "worker", "prompt", "source_digest", "scope_digest", "dependencies", "writer_paths"}
        require(set(task) == fields, "task requires exactly the documented fields; reserved/unknown fields forbidden")
        for field in fields - {"dependencies", "writer_paths"}:
            require(string(task[field]), f"task.{field} required")
        require(worker_role(task["worker"]), "invalid worker role")
        for field in ("dependencies", "writer_paths"):
            require(isinstance(task[field], list) and all(string(value) for value in task[field]), f"task.{field} must be a list of nonempty strings")
    if op in {"observe", "rebind"}:
        require(worker_role(event.get("worker")), "invalid worker role")
    if op == "summary":
        require(type(event.get("offset", 0)) is int and event.get("offset", 0) >= 0, "invalid status offset")
        require(type(event.get("limit", 50)) is int and 1 <= event.get("limit", 50) <= 100, "invalid status limit")
    if op == "rebind":
        for field in ("worker", "expected_old_thread_id", "new_thread_id", "evidence"):
            require(string(event.get(field)), f"{field} required")
    if op == "observe":
        require(string(event.get("worker")), "worker required")
        require(event.get("state") in ("IDLE", "BUSY", "UNKNOWN"), "invalid worker state")
        require(string(event.get("evidence")), "observation evidence reference required")
    if op in {"prepare", "issue", "ack", "result", "reconcile"}:
        require(string(event.get("task_id")), "task_id required")
    if op in {"issue", "ack", "result", "reconcile"}:
        require(string(event.get("dispatch_id")), "dispatch_id required")
    if op == "prepare":
        require(string(event.get("source_digest")) and string(event.get("scope_digest")), "source_digest and scope_digest required")
    if op in {"ack", "result"}:
        validate_file(event.get("receipt"))


def validate_file(value):
    require(isinstance(value, dict) and string(value.get("path")), "receipt path required")
    sha = value.get("sha256")
    require(isinstance(sha, str) and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha), "receipt sha256 required")


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


def resolved_claims(model, task):
    root = model["project"]["root"]
    return sorted(set(canonical(root, path) for path in task.get("writer_claims", task["writer_paths"])))

def digest(path):
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(root, value):
    require(string(value), "path must be a nonempty string")
    path = (Path(root) / value).resolve()
    require(path.is_relative_to(root), "path escapes project root")
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
    validate_event(event)
    op = event["op"]
    response = {"action": "OK"}
    if op not in {"status", "recover", "summary"}:
        require(isinstance(event.get("event_id"), str) and event["event_id"], "event_id required")
        event_hash = hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        prior = model.get("events", {}).get(event["event_id"])
        if prior:
            require(prior["sha256"] == event_hash, "event_id reused with conflicting payload")
            return {"model": model, "response": {"action": "REPLAY", "original_action": prior["action"]}}
    if op == "init":
        project = copy.deepcopy(event["project"])
        project["root"] = str(Path(project["root"]).resolve(strict=True))
        for field in ("project_id", "linear_ref", "beads_ref", "coordinator_thread_id"):
            require(isinstance(project.get(field), str) and project[field], f"project.{field} required")
        ids = list(project["workers"].values()) + [project["coordinator_thread_id"]]
        require(len(set(ids)) == len(ids), "distinct native thread IDs required")
        if model:
            require(model["project"] == project, "database already bound to a different project")
        else:
            model = {"project": project, "tasks": {}, "workers": {}, "events": {}}
    else:
        require(model, "init required")
        require(event.get("project_id") == model["project"]["project_id"], "project_id binding mismatch")
        require(str(Path(event.get("root", "")).resolve()) == model["project"]["root"], "root binding mismatch")
        # An earlier ISSUING transition may have exposed a tool request. Never repeat it.
        for task in model["tasks"].values():
            if op != "rebind" and task["status"] == "ISSUING":
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
            require(task["id"] not in model["tasks"], "task already exists; use prepare for reuse")
            task["prompt_sha256"] = hashlib.sha256(task["prompt"].encode()).hexdigest()
            task.update(status="WAIT", lease=False)
            model["tasks"][task["id"]] = task
            reject_cycles(model["tasks"])
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
        elif op in {"prepare", "issue", "ack", "result", "reconcile"}:
            require(event.get("task_id") in model["tasks"], "unknown task_id")
            task = model["tasks"][event["task_id"]]
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
    if op not in {"status", "recover", "summary"}:
        model["events"][event["event_id"]] = {"sha256": event_hash, "action": response["action"]}
    return {"model": model, "response": response}


def compute_readiness(state):
    model = copy.deepcopy(state["model"])
    for task in model["tasks"].values():
        if task["status"] in {"WAIT", "READY"}:
            task["reasons"] = readiness(model, task)
            task["status"] = "WAIT" if task["reasons"] else "READY"
    return {"model": model}


def build_graph(saver):
    builder = StateGraph(State)
    builder.add_node("transition", transition)
    builder.add_node("readiness", compute_readiness)
    builder.add_edge(START, "transition")
    builder.add_edge("transition", "readiness")
    builder.add_edge("readiness", END)
    return builder.compile(checkpointer=saver)


def apply_event(db, event):
    """Commit all checkpoints synchronously before returning any native intent.

    Safe to kill the caller after this returns: subsequent calls never return the
    same native request. No promise of exactly-once delivery across MCP transport.
    """
    validate_event(event)
    db = Path(db).resolve()
    if event["op"] == "summary":
        return read_status(db, event)
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
            require(not (resumed and event["op"] == "rebind"), "pending checkpoint; recover and reconcile before rebind")
            if resumed:
                graph.invoke(None, config, durability="sync")
            # Validate before recording input: an invalid event cannot poison recovery.
            before = graph.get_state(config).values
            candidate = transition({"model": before.get("model", {}), "event": event})
            compute_readiness(candidate)
            state = graph.invoke({"event": event}, config, durability="sync")
            conn.commit()
            response = copy.deepcopy(state["response"])
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
    return {"action": "OK", "checkpoint_id": checkpoint_id,
            "project": bounded(project, "project"),
            "workers": bounded({k: {"state": model["workers"][k]["state"]} for k in sorted(roles) if k in model["workers"]}, "workers"),
            "tasks": page, "resumed_checkpoint": False, "recovery_required": bool(pending) or bool(uncertain),
            "pending_checkpoint": bool(pending), "uncertain_held_dispatches": uncertain,
            "bounds": {"offset": offset, "limit": limit, "total_tasks": len(tasks),
                       "next_offset": offset + limit if offset + limit < len(tasks) else None,
                       "string_chars": 512, "list_items": 32, "worker_role_count": len(all_roles),
                       "omitted_worker_roles": len(all_roles - roles), "truncated_fields": sorted(truncated)}}


def read_status(db, event):
    # Skip saver.setup: its journal/schema initialization is a write operation.
    require(db.is_file(), "init required")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, check_same_thread=False)) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        saver = SqliteSaver(conn)
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
