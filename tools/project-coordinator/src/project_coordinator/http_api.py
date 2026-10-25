"""Loopback-only, bearer-authenticated, query-only coordinator status API."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .runtime import InvalidEvent, apply_event


PAGE = Path(__file__).with_name("status.html")


def handler(db: str, project_id: str, root: str, token: str):
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

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == "/" and not parsed.query:
                self.send_body(200, "text/html; charset=utf-8", PAGE.read_bytes())
                return
            if parsed.path != "/api/status" or not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                self.send_body(404 if parsed.path != "/api/status" else 401, "application/json", b'{"error":"unauthorized or unknown route"}')
                return
            try:
                query = parse_qs(parsed.query, strict_parsing=True) if parsed.query else {}
                if set(query) - {"offset", "limit", "report_offset", "report_limit", "project_report_offset", "project_report_limit"} or any(len(v) != 1 for v in query.values()):
                    raise ValueError("invalid query")
                offset = int(query.get("offset", ["0"])[0])
                limit = int(query.get("limit", ["50"])[0])
                report_offset = int(query.get("report_offset", ["0"])[0])
                report_limit = int(query.get("report_limit", ["50"])[0])
                project_report_offset = int(query.get("project_report_offset", ["0"])[0])
                project_report_limit = int(query.get("project_report_limit", ["50"])[0])
                view = apply_event(db, {"op": "summary", "project_id": project_id, "root": root,
                                        "offset": offset, "limit": limit,
                                        "report_offset": report_offset, "report_limit": report_limit,
                                        "project_report_offset": project_report_offset,
                                        "project_report_limit": project_report_limit})
                self.send_body(200, "application/json; charset=utf-8", json.dumps(view, separators=(",", ":")).encode())
            except (InvalidEvent, ValueError):
                self.send_body(400, "application/json", b'{"error":"invalid status request"}')

        def do_POST(self):
            self.send_response(405)
            self.send_header("Allow", "GET")
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b'{"error":"read-only API"}')

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST
        do_OPTIONS = do_POST

    return APIHandler


def serve(db: str, project_id: str, root: str, host: str, port: int, token: str | None = None):
    token = token or os.environ.get("PROJECT_COORDINATOR_TOKEN")
    if not token or len(token) < 24:
        raise InvalidEvent("set PROJECT_COORDINATOR_TOKEN to a bearer token of at least 24 characters")
    if not 0 <= port <= 65535:
        raise InvalidEvent("port must be between 0 and 65535")
    if not Path(db).is_file():
        raise InvalidEvent("coordinator database does not exist")
    server = ThreadingHTTPServer((host, port), handler(db, project_id, root, token))
    try:
        print(f"owner status API listening on http://{host}:{server.server_port}/", flush=True)
        server.serve_forever()
    finally:
        server.server_close()
    return 0
