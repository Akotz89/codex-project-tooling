"""Loopback bearer API for owner status reads and bound participant event writes."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .runtime import InvalidEvent, apply_event, read_declared_reference, read_model, task_bundle, read_updates, worker_role, validate_project_policy
from .routing_policy import valid_policy, valid_support, valid_write


PAGE = Path(__file__).with_name("status.html")


def handler(db: str, project_id: str, root: str, token: str, participant_tokens=None, project_policy=None, routing_provider=None):
    participant_tokens = participant_tokens or {}

    def token_matches(candidate, expected):
        return isinstance(candidate, str) and candidate.isascii() and hmac.compare_digest(candidate, expected)

    def participant_role(auth):
        if not auth.startswith("Bearer "):
            return None
        candidate = auth[7:]
        for role, secret in participant_tokens.items():
            if token_matches(candidate, secret):
                return role
        return None

    def registered_thread_id(model, role):
        if role == "coordinator":
            return model["project"]["coordinator_thread_id"]
        worker_thread = model["project"]["workers"].get(role)
        if worker_thread is not None:
            return worker_thread
        if project_policy is not None:
            policy = validate_project_policy(project_policy, model["project"])
            return next((item["thread_id"] for item in policy.get("project_report_registrations", [])
                         if item["role"] == role), None)
        return None

    class APIHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Request lines may contain attacker-supplied query values.
            pass

        def send_body(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def send_attachment(self, filename, stream, size):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename, safe='')}")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
            self.end_headers()
            while chunk := stream.read(1024 * 1024):
                self.wfile.write(chunk)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/" and not parsed.query:
                self.send_body(200, "text/html; charset=utf-8", PAGE.read_bytes())
                return
            auth = self.headers.get("Authorization", "")
            actor = participant_role(auth)
            owner = token_matches(auth, f"Bearer {token}")
            known = parsed.path in {"/api/status", "/api/graph", "/api/updates", "/api/routing-policy"} or parsed.path.startswith("/api/tasks/")
            if not known:
                self.send_body(404, "application/json", b'{"error":"unauthorized or unknown route"}')
                return
            participant_route = parsed.path == "/api/updates" or parsed.path.startswith("/api/tasks/")
            if not owner and not (actor and participant_route):
                self.send_body(401, "application/json", b'{"error":"unauthorized or unknown route"}')
                return
            try:
                if parsed.path == "/api/routing-policy":
                    if not owner or routing_provider is None:
                        self.send_body(404 if routing_provider is None else 401, "application/json", b'{"error":"unavailable"}')
                        return
                    support = routing_provider.support()
                    policy = routing_provider.read_policy()
                    if not valid_policy(policy) or not valid_support(support):
                        raise ValueError("routing provider returned an invalid typed result")
                    result = {"policy": policy, "support": support,
                              "revision": policy.get("revision") if isinstance(policy, dict) else None}
                    self.send_body(200, "application/json; charset=utf-8", json.dumps(result, separators=(",", ":")).encode())
                    return
                query = parse_qs(parsed.query, strict_parsing=True) if parsed.query else {}
                allowed = {"offset", "limit", "report_offset", "report_limit", "project_report_offset", "project_report_limit", "role", "thread_id", "after_revision"}
                if set(query) - allowed or any(len(v) != 1 for v in query.values()):
                    raise ValueError("invalid query")
                if parsed.path == "/api/updates":
                    role = query.get("role", [actor or "coordinator"])[0]
                    if actor and role != actor:
                        raise InvalidEvent("participant role does not match token")
                    model, _ = read_model(db, {"project_id": project_id, "root": root})
                    bound_thread = registered_thread_id(model, role)
                    thread_id = query.get("thread_id", [bound_thread])[0]
                    if not thread_id:
                        raise InvalidEvent("unbound update role")
                    if thread_id != bound_thread:
                        raise InvalidEvent("update thread binding mismatch")
                    result = read_updates(model, {"role": role, "thread_id": thread_id,
                                                  "after_revision": int(query.get("after_revision", ["0"])[0])},
                                         project_policy=project_policy)
                    self.send_body(200, "application/json; charset=utf-8", json.dumps(result, separators=(",", ":")).encode())
                    return
                if parsed.path.startswith("/api/tasks/"):
                    parts = [unquote(part) for part in parsed.path.split("/")]
                    role = query.get("role", [actor])[0]
                    if actor and role != actor:
                        raise InvalidEvent("participant role does not match token")
                    model, checkpoint_id = read_model(db, {"project_id": project_id, "root": root})
                    if len(parts) == 5 and parts[1] == "api" and parts[2] == "tasks" and parts[4] == "bundle" and parts[3]:
                        bound_thread = registered_thread_id(model, role) if role else None
                        thread_id = query.get("thread_id", [bound_thread])[0]
                        if role and thread_id != bound_thread:
                            raise InvalidEvent("task bundle thread binding mismatch")
                        result = task_bundle(model, parts[3], role=role, thread_id=thread_id,
                                             project_policy=project_policy)
                        result["checkpoint_id"] = checkpoint_id
                        self.send_body(200, "application/json; charset=utf-8", json.dumps(result, separators=(",", ":")).encode())
                        return
                    if (len(parts) == 7 and parts[1] == "api" and parts[2] == "tasks"
                            and parts[4] == "references" and all(parts[i] for i in (3, 5, 6))):
                        bound_thread = registered_thread_id(model, role) if role else None
                        thread_id = query.get("thread_id", [bound_thread])[0]
                        if role and thread_id != bound_thread:
                            raise InvalidEvent("task bundle thread binding mismatch")
                        bundle = task_bundle(model, parts[3], role=role, thread_id=thread_id,
                                             project_policy=project_policy)
                        ref = next((r for r in bundle["references"]
                                    if r["task_id"] == parts[5] and r["id"] == parts[6]), None)
                        if ref is None:
                            raise InvalidEvent("reference is not declared in the requested task closure")
                        stream, size = read_declared_reference(root, ref)
                        try:
                            self.send_attachment(Path(ref["path"]).name, stream, size)
                        finally:
                            stream.close()
                        return
                    raise ValueError("invalid task bundle or reference route")
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                report_offset = int(query.get("report_offset", ["0"])[0])
                report_limit = int(query.get("report_limit", ["50"])[0])
                project_report_offset = int(query.get("project_report_offset", ["0"])[0])
                project_report_limit = int(query.get("project_report_limit", ["50"])[0])
                if parsed.path == "/api/graph":
                    if offset < 0 or not 1 <= limit <= 100:
                        raise ValueError("invalid graph page")
                    model, checkpoint_id = read_model(db, {"project_id": project_id, "root": root})
                    tasks = sorted(model["tasks"].values(), key=lambda t: (not t.get("lease", False), t["id"]))
                    selected = tasks[offset:offset + limit]
                    from .runtime import project_graph
                    view = {"action": "OK", "checkpoint_id": checkpoint_id, "revision": model.get("revision", 0),
                            "graph": project_graph(model, [task["id"] for task in selected]),
                            "bounds": {"offset": offset, "limit": limit, "total_tasks": len(tasks),
                                       "next_offset": offset + limit if offset + limit < len(tasks) else None}}
                else:
                    view = apply_event(db, {"op": "summary", "project_id": project_id, "root": root,
                                        "offset": offset, "limit": limit,
                                        "report_offset": report_offset, "report_limit": report_limit,
                                        "project_report_offset": project_report_offset,
                                        "project_report_limit": project_report_limit})
                self.send_body(200, "application/json; charset=utf-8", json.dumps(view, separators=(",", ":")).encode())
            except (InvalidEvent, OSError, ValueError, KeyError, TypeError):
                self.send_body(400, "application/json", b'{"error":"invalid status request"}')

        def do_POST(self):
            route = urlsplit(self.path).path
            if route == "/api/routing-policy":
                auth = self.headers.get("Authorization", "")
                if not auth.startswith("Bearer ") or not token_matches(auth[7:], token):
                    self.send_body(401, "application/json", b'{"error":"unauthorized"}')
                    return
                if routing_provider is None:
                    self.send_body(404, "application/json", b'{"error":"unavailable"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 8192:
                        raise ValueError("invalid body length")
                    body = json.loads(self.rfile.read(length), object_pairs_hook=_unique_object)
                    if not isinstance(body, dict) or set(body) != {"revision", "model", "effort"}:
                        raise ValueError("invalid policy request")
                    if (not isinstance(body["revision"], str) or not body["revision"]
                            or not isinstance(body["model"], str) or not body["model"]
                            or not isinstance(body["effort"], str) or not body["effort"]
                            or max(map(len, body.values())) > 256):
                        raise ValueError("invalid policy request values")
                    support = routing_provider.support()
                    if not valid_support(support):
                        raise ValueError("routing provider returned invalid support state")
                    if not support.get("combined_supported"):
                        self.send_body(409, "application/json", json.dumps({"state":"unsupported", "support":support}).encode())
                        return
                    result = routing_provider.compare_and_swap(body["revision"], {"model":body["model"], "effort":body["effort"]})
                    if not valid_write(result):
                        raise ValueError("routing provider returned invalid write state")
                    self.send_body(200 if result["state"] == "saved" else 409, "application/json", json.dumps(result, separators=(",", ":")).encode())
                except (InvalidEvent, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                    self.send_body(400, "application/json", b'{"error":"invalid policy request"}')
                return
            if route not in {"/api/participant/report", "/api/participant/project-report"}:
                self.send_response(405)
                self.send_header("Allow", "GET")
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b'{"error":"unsupported write route"}')
                return
            role = participant_role(self.headers.get("Authorization", ""))
            if not role:
                self.send_body(401, "application/json", b'{"error":"unauthorized"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 32768:
                    raise ValueError("invalid body length")
                body = json.loads(self.rfile.read(length), object_pairs_hook=_unique_object)
                fields = ({"event_id", "task_id", "claim_id", "dispatch_id", "source_digest", "kind", "occurred_at", "state", "detail", "source", "evidence"}
                          if route.endswith("/report") and not route.endswith("/project-report")
                          else {"event_id", "work_item", "kind", "occurred_at", "state", "detail", "source", "evidence"})
                if route.endswith("/project-report"):
                    fields.add("recipients") if isinstance(body, dict) and "recipients" in body else None
                if not isinstance(body, dict) or set(body) != fields:
                    raise ValueError("invalid participant report fields")
                model, _ = read_model(db, {"project_id": project_id, "root": root})
                if route.endswith("/project-report"):
                    thread_id = registered_thread_id(model, role)
                    if not thread_id:
                        raise InvalidEvent("project reporter is not registered")
                    event = {**body, "op": "project_report", "project_id": project_id, "root": root,
                             "reporter": role, "reporter_thread_id": thread_id}
                    result = apply_event(db, event, project_policy=project_policy)
                else:
                    task = model["tasks"].get(body["task_id"])
                    if not task or task["worker"] != role:
                        raise InvalidEvent("participant does not own task")
                    if body["claim_id"] != task["beads_ref"] or body["dispatch_id"] != task.get("dispatch_id") or not body["dispatch_id"]:
                        raise InvalidEvent("task claim or dispatch identity mismatch")
                    if body["source_digest"] != task["source_digest"]:
                        raise InvalidEvent("task source identity mismatch")
                    event = {**body, "op": "report", "project_id": project_id, "root": root,
                             "reporter": role, "reporter_thread_id": model["project"]["workers"][role]}
                    result = apply_event(db, event)
                self.send_body(200, "application/json; charset=utf-8", json.dumps(result, separators=(",", ":")).encode())
            except (InvalidEvent, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.send_body(400, "application/json", b'{"error":"invalid participant report"}')

        def do_method_not_allowed(self):
            path = urlsplit(self.path).path
            allow = "POST" if path in {"/api/participant/report", "/api/participant/project-report"} else "GET" if path.startswith("/api/") else "GET, POST"
            self.send_response(405)
            self.send_header("Allow", allow)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b'{"error":"method not allowed"}')

        do_PUT = do_method_not_allowed
        do_PATCH = do_method_not_allowed
        do_DELETE = do_method_not_allowed
        do_OPTIONS = do_method_not_allowed

    return APIHandler


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def serve(db: str, project_id: str, root: str, host: str, port: int, token: str | None = None, participant_tokens=None, project_policy=None, routing_provider=None):
    token = token or os.environ.get("PROJECT_COORDINATOR_TOKEN")
    if (not isinstance(token, str) or len(token) < 24 or not token.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in token)):
        raise InvalidEvent("set PROJECT_COORDINATOR_TOKEN to a bearer token of at least 24 characters")
    if not 0 <= port <= 65535:
        raise InvalidEvent("port must be between 0 and 65535")
    if not Path(db).is_file():
        raise InvalidEvent("coordinator database does not exist")
    if participant_tokens is None and os.environ.get("PROJECT_COORDINATOR_PARTICIPANT_TOKENS"):
        try:
            participant_tokens = json.loads(os.environ["PROJECT_COORDINATOR_PARTICIPANT_TOKENS"])
        except json.JSONDecodeError as exc:
            raise InvalidEvent("participant token map must be JSON") from exc
    participant_tokens = participant_tokens or {}
    if (not isinstance(participant_tokens, dict)
            or any(not worker_role(k) or not isinstance(v, str) or len(v) < 24 or not v.isascii()
                   or any(ord(char) < 33 or ord(char) > 126 for char in v)
                   for k, v in participant_tokens.items())
            or len(set(participant_tokens.values())) != len(participant_tokens)):
        raise InvalidEvent("participant tokens require role keys and bearer tokens of at least 24 characters")
    if any(token.isascii() and hmac.compare_digest(token, secret) for secret in participant_tokens.values()):
        raise InvalidEvent("owner and participant bearer tokens must be distinct")
    if participant_tokens:
        model, _ = read_model(db, {"project_id": project_id, "root": root})
        allowed_roles = set(model["project"]["workers"]) | {"coordinator"}
        if project_policy is not None:
            policy = validate_project_policy(project_policy, model["project"])
            allowed_roles.update(item["role"] for item in policy.get("project_report_registrations", []))
        if set(participant_tokens) - allowed_roles:
            raise InvalidEvent("participant tokens include an unregistered role")
    server = ThreadingHTTPServer((host, port), handler(db, project_id, root, token, participant_tokens, project_policy, routing_provider))
    try:
        print(f"owner status API listening on http://{host}:{server.server_port}/", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
    return 0
