"""Port-gated demo service (issue #188).

Readiness is signalled ONLY by the TCP port opening: the process
sleeps ~1.5s BEFORE binding its port, so a services wait of
{port: 8942} genuinely gates the boot - nothing listens at all until
the delay elapses. Serves the repo statically (like proof_server)
plus /api/port-status.
"""
import json
import time
from http.server import SimpleHTTPRequestHandler, HTTPServer


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
        if path == "/api/port-status":
            _json(self, {"status": "port-gated service answered"})
        else:
            super().do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("portsvc: booting, port not open yet", flush=True)
    time.sleep(1.5)
    server = HTTPServer(("127.0.0.1", 8942), H)
    print("portsvc: port 8942 open", flush=True)
    server.serve_forever()
