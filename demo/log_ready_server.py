"""Log-gated demo service (issue #186).

Readiness is signalled ONLY by a line in stdout: the server binds its
port immediately but does not print the ready needle (or serve) until
~1.5s later, so a services wait of {log_contains: ...} genuinely gates
the boot - an http/port wait could pass earlier against a bound socket
only if it served, which this process does not do before the needle.
Serves the repo statically (like proof_server) plus /api/log-status.
"""
import json
import time
from http.server import SimpleHTTPRequestHandler, HTTPServer

NEEDLE = "LOGREADY-186 log-gated service ready"


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
        if path == "/api/log-status":
            _json(self, {"status": "log-gated service answered"})
        else:
            super().do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8941), H)
    print("logsvc: booting, not ready yet", flush=True)
    time.sleep(1.5)
    print(NEEDLE, flush=True)
    server.serve_forever()
