"""Env-echo demo service (issue #190).

The page/API content is driven entirely by the QALOOP_DEMO_ENV_TOKEN
environment variable, which the parent shell does NOT set: the only
source is the services env: block in flows/demo-services-env.yaml,
merged into this child's environment by env.py boot_command. With no
declared env the token falls back to "unset-env-token", so the flow's
positive assertion can only pass when the declared value arrives.
Serves the repo statically (like proof_server) plus /api/env-status.
"""
import json
import os
from http.server import SimpleHTTPRequestHandler, HTTPServer

TOKEN = os.environ.get("QALOOP_DEMO_ENV_TOKEN", "unset-env-token")


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
        if path == "/api/env-status":
            _json(self, {"status": "env-declared token is " + TOKEN})
        else:
            super().do_GET()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", 8943), H)
    print("envsvc: port 8943 open, token is " + TOKEN, flush=True)
    server.serve_forever()
