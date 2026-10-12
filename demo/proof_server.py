"""Proof server: serves the demo page plus minimal real API endpoints."""
import json
from http.server import SimpleHTTPRequestHandler, HTTPServer

# Issue #142: remembers the headers of the most recent /api/echo-headers
# request so a fixture page can later read back what the seed request sent.
LAST_ECHO_HEADERS = {}


def _json(handler, payload, status=200):
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if self.path == "/api/greeting":
            _json(self, {"greeting": "Hello from the proof server"})
        elif path == "/api/echo-headers":
            global LAST_ECHO_HEADERS
            LAST_ECHO_HEADERS = dict(self.headers)
            _json(self, LAST_ECHO_HEADERS)
        elif path == "/api/last-headers":
            _json(self, LAST_ECHO_HEADERS)
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/signup":
            ln = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(ln) or b"{}")
            body = json.dumps({"ok": True, "name": payload.get("name")}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def log_message(self, *a):
        pass

HTTPServer(("127.0.0.1", 8933), H).serve_forever()
