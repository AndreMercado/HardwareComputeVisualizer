"""AI control of the running app: a small JSON API on 127.0.0.1 only.

Switched on with the AI CONTROL button (or `python main.py --ai-control`).
Every request must carry the header `X-HCV-Token: <token>`; the token is
written to control_token.txt next to this file when control starts (a new
one each time). A web page can't send that header to another site, so a
browser can't drive the app; local programs that can read the file can.

Requests are handed to the app's UI thread, which shows each action (banner,
flashing button, log line) and waits a moment before doing it, so a person
can watch the AI work. Endpoints (all JSON):

    GET  /state                  what is on screen: target, parts, test, running, readings, status
    GET  /summary                the last run's summary text
    GET  /runs                   recorded runs (newest first)
    POST /select   {"part", "name"?}         click a part (and pick one by name, e.g. a disk)
    POST /test     {"test"}                  choose a test: 2d 3d ml io bus ai code (or its button text)
    POST /program  {"command"}               YOUR CODE: the command line to run
    POST /ai_task  {"task"}                  AI BENCHMARK: classifier | render | matmul (or its title)
    POST /start    {"confirm"?: bool}        press START (confirm=true answers "continue?" questions)
    POST /stop                               press STOP
    POST /wait     {"timeout"?: s}           block until the running test ends (max 900 s)
    POST /replay   {"run"}                   replay a recorded run (file name from /runs)
    POST /compare  {"a"?, "b"?}              open COMPARE with these two runs
    POST /remote   {"host", "user", "key", "port"?}   connect to a remote machine (SSH key)
    POST /local                              back to this PC
"""
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "control_token.txt")
PORT = 8770
ACTIONS = {"state", "summary", "runs", "select", "test", "program", "ai_task", "start", "stop", "wait",
           "replay", "compare", "remote", "local"}


class ControlServer:
    """Runs the HTTP server on a thread; requests become (action, args, reply queue)
    items in `self.requests`, which the app drains on its UI thread."""

    def __init__(self, port=PORT):
        self.token = secrets.token_hex(16)
        self.requests = queue.Queue()
        self.port = port
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keep the console quiet
                pass

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                if not secrets.compare_digest(self.headers.get("X-HCV-Token", ""), server.token):
                    return self._reply(403, {"error": "missing or wrong X-HCV-Token (see control_token.txt)"})
                action = self.path.strip("/").split("?")[0]
                if action not in ACTIONS:
                    return self._reply(404, {"error": f"unknown action {action!r}", "actions": sorted(ACTIONS)})
                args = {}
                if method == "POST":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        args = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    except ValueError:
                        return self._reply(400, {"error": "body must be JSON"})
                    if not isinstance(args, dict):
                        return self._reply(400, {"error": "body must be a JSON object"})
                if action == "wait":  # waits here, off the UI thread
                    return self._reply(200, server.wait(args))
                reply = queue.Queue()
                server.requests.put((action, args, reply))
                try:
                    code, body = reply.get(timeout=30)
                except queue.Empty:
                    code, body = 504, {"error": "the app didn't answer in 30 s"}
                self._reply(code, body)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(self.token)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def wait(self, args):
        """Block until the test ends; asks the UI thread for the state twice a second."""
        timeout = min(900.0, float(args.get("timeout") or 600))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            reply = queue.Queue()
            self.requests.put(("state", {"quiet": True}, reply))
            try:
                _, state = reply.get(timeout=10)
            except queue.Empty:
                return {"error": "the app stopped answering"}
            if not state.get("running"):
                return {"finished": True, "state": state}
            time.sleep(0.5)
        return {"finished": False, "error": f"still running after {timeout:.0f} s"}

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        try:
            os.remove(TOKEN_FILE)
        except OSError:
            pass
