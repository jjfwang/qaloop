"""Scripted fake model for the upload proof (issue #4).

Serves POST /chat/completions with a canned sequence: upload the fixture
through the file input, wait for the file name to appear in the page, then
done. Fixture path comes from argv[1]. The only mocked part is the model;
the browser loop, actions, guards, transcript, and reports are all real.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

FIXTURE = sys.argv[1]
NAME = os.path.basename(FIXTURE)
SCRIPT = [
    {"action": "upload", "target": {"css": "#resume"}, "path": FIXTURE},
    {"action": "wait", "target": {"text": NAME}, "state": "visible"},
    {"action": "done",
     "summary": f"Uploaded {NAME} through the Resume file input and confirmed its name is shown on the page."},
]
state = {"n": 0}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0))
        self.rfile.read(ln)
        i = min(state["n"], len(SCRIPT) - 1)
        state["n"] += 1
        body = json.dumps({
            "choices": [{"message": {"role": "assistant",
                                     "content": json.dumps(SCRIPT[i])}}],
            "usage": {"prompt_tokens": 800, "completion_tokens": 60},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


HTTPServer(("127.0.0.1", 8932), H).serve_forever()
