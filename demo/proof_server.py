"""Proof server: serves the demo page plus minimal real API endpoints."""
import json
from http.server import SimpleHTTPRequestHandler, HTTPServer

class H(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/greeting":
            body = json.dumps({"greeting": "Hello from the proof server"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
