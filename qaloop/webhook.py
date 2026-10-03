"""Webhook receiver: GitHub PR events -> queue. Also a local /enqueue endpoint.

Run: qaloop webhook --port 8090
GitHub webhook URL: http://<host>:8090/webhook/github  (content type: application/json)
Secret: QALOOP_WEBHOOK_SECRET (HMAC SHA-256 over the raw body).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import queue as q


def _verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret:
        return True  # no secret configured -> accept (local dev only)
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest("sha256=" + digest, header)


def _pr_action_allowed(action: str, payload: dict) -> bool:
    """Should a pull_request webhook action enqueue a verification job?"""
    if action not in ("opened", "synchronize", "reopened", "labeled"):
        return False
    if action == "labeled" and (payload.get("label") or {}).get("name") != "needs-qa":
        return False
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "qaloop-webhook/0.1"

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # quieter logs
        print(f"[webhook] {' '.join(str(a) for a in args)}")

    def do_GET(self):  # noqa: N802
        if urlparse(self.path).path == "/health":
            self._send(200, {"ok": True, "pending": len(q.list_jobs("pending", 1))})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if path == "/webhook/github":
            self._handle_github(body)
        elif path == "/enqueue":
            self._handle_enqueue(body)
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def _handle_github(self, body: bytes) -> None:
        secret = os.environ.get("QALOOP_WEBHOOK_SECRET", "")
        if not _verify_signature(secret, body, self.headers.get("X-Hub-Signature-256")):
            self._send(401, {"ok": False, "error": "bad signature"})
            return
        event = self.headers.get("X-GitHub-Event", "")
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return
        if event == "ping":
            self._send(200, {"ok": True, "msg": "pong"})
            return
        if event == "pull_request":
            action = payload.get("action")
            if not _pr_action_allowed(action, payload):
                self._send(200, {"ok": True, "msg": f"ignored action {action}"})
                return
            repo = (payload.get("repository") or {}).get("full_name", "")
            pr = (payload.get("pull_request") or {}).get("number")
            sha = (payload.get("pull_request") or {}).get("head", {}).get("sha", "")
            job_id = q.enqueue("verify-repo", {
                "repo": repo.split("/")[-1], "repo_full": repo,
                "pr": pr, "sha": sha, "source": "github-webhook",
            })
            self._send(200, {"ok": True, "job_id": job_id})
            return
        self._send(200, {"ok": True, "msg": f"ignored event {event}"})

    def _handle_enqueue(self, body: bytes) -> None:
        token = os.environ.get("QALOOP_ENQUEUE_TOKEN", "")
        if token and self.headers.get("X-QALoop-Token") != token:
            self._send(401, {"ok": False, "error": "bad token"})
            return
        try:
            payload = json.loads(body or b"{}")
        except Exception:
            self._send(400, {"ok": False, "error": "bad json"})
            return
        kind = payload.pop("kind", "verify-flow")
        job_id = q.enqueue(kind, payload)
        self._send(200, {"ok": True, "job_id": job_id})


def serve(port: int = 8090, host: str = "127.0.0.1") -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"webhook listening on http://{host}:{port} "
          f"(github -> /webhook/github, manual -> /enqueue)")
    server.serve_forever()


def serve_in_thread(port: int = 8090, host: str = "127.0.0.1") -> threading.Thread:
    t = threading.Thread(target=serve, kwargs={"port": port, "host": host},
                         daemon=True)
    t.start()
    return t
