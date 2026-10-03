"""Scripted fake OpenAI-compatible chat server for proving qaloop perform.

Serves POST /chat/completions with a canned action sequence. The only
mocked part is the model; the browser loop, actions, guards, transcript,
and reports are all real.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT = [
    {"action": "click", "target": {"role": "button", "name": "Load greeting"}},
    {"action": "fill", "target": {"role": "textbox", "name": "Name"}, "text": "Maya"},
    {"action": "fill", "target": {"role": "textbox", "name": "Email"}, "text": "maya@example.com"},
    {"action": "click", "target": {"role": "button", "name": "Sign up"}},
    {"action": "wait", "target": {"text": "Welcome, Maya"}, "state": "visible"},
    {"action": "done", "summary": "Loaded the mocked greeting, filled the signup form as Maya (maya@example.com), submitted, and confirmed 'Welcome, Maya!' is visible."},
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
