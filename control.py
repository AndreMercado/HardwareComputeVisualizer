"""AI control of the running app: a small JSON API on 127.0.0.1 only.

Switched on with the AI CONTROL button (or `python main.py --ai-control`).
Every request must carry the header `X-HCV-Token: <token>`; the token is
written to control_token.txt in the data folder when control starts (a new
one each time).

The same server also runs as the **editor link** (always on while the app is open,
on a free port): the VS Code extension finds it through `app.json` in the user's
HCV folder (%LOCALAPPDATA%/HCV on Windows) - port, token, app folder - so it
connects without any settings. Editor requests are done at once, without the AI's
banners and pauses, and only a few actions are allowed (EDITOR_ACTIONS). A web page can't send that header to another site, so a
browser can't drive the app; local programs that can read the file can.

Requests are handed to the app's UI thread, which shows each action (banner,
flashing button, log line) and waits a moment before doing it, so a person
can watch the AI work. Endpoints (all JSON):

    GET  /state                  what is on screen: machine, parts, test, running, readings, status
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
"""
import json
import os
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def user_dir():
    """The user's own HCV folder (not inside the app): %LOCALAPPDATA%/HCV, ~/.local/share/hcv."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return os.path.join(base, "HCV")
    return os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "hcv")


def data_dir():
    """Where runs, the AI workspace and tokens go: HCV_DATA_DIR if set (the VS Code extension sets it
    when it starts the app it carries), else this folder (a copy of the repo) when it can be written."""
    d = os.environ.get("HCV_DATA_DIR")
    if not d:
        d = HERE if os.access(HERE, os.W_OK) and ".vscode" not in HERE.split(os.sep) else user_dir()
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = data_dir()
TOKEN_FILE = os.path.join(DATA_DIR, "control_token.txt")
DISCOVERY_FILE = os.path.join(user_dir(), "app.json")  # how the VS Code extension finds the running app
PORT = 8770
ACTIONS = {"state", "summary", "runs", "select", "test", "program", "ai_task", "start", "stop", "wait",
           "replay", "compare"}
# what the editor link may do: run a program and watch it, nothing else
EDITOR_ACTIONS = {"state", "summary", "select", "test", "program", "start", "stop", "wait", "hello"}
APP_VERSION = "0.2.0"


class ControlServer:
    """Runs the HTTP server on a thread; requests become (action, args, reply queue)
    items in `self.requests`, which the app drains on its UI thread."""

    def __init__(self, port=PORT, token_file=TOKEN_FILE, actions=ACTIONS):
        """port=0 picks a free port; token_file=None keeps the token in memory (the editor link
        publishes it in app.json instead)."""
        self.token = secrets.token_hex(16)
        self.requests = queue.Queue()
        self.port = port
        self.token_file = token_file
        self.actions = actions
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
                if action not in server.actions:
                    return self._reply(404, {"error": f"unknown action {action!r}", "actions": sorted(server.actions)})
                if action == "hello":  # the editor checking the app is there
                    return self._reply(200, {"app": "Hardware Compute Visualizer", "version": APP_VERSION,
                                             "pid": os.getpid(), "folder": HERE})
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
        self.port = self.httpd.server_address[1]
        if token_file:
            with open(token_file, "w", encoding="utf-8") as f:
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
        if self.token_file:
            try:
                os.remove(self.token_file)
            except OSError:
                pass


def publish_link(server):
    """Tells editors where the running app's link is: app.json in the user's HCV folder."""
    info = {"app": "Hardware Compute Visualizer", "version": APP_VERSION, "pid": os.getpid(),
            "port": server.port, "token": server.token, "folder": HERE, "data": DATA_DIR,
            "python": sys.executable, "started": time.time()}
    os.makedirs(os.path.dirname(DISCOVERY_FILE), exist_ok=True)
    tmp = DISCOVERY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(info, f)
    os.replace(tmp, DISCOVERY_FILE)
    return info


def unpublish_link():
    """Removes app.json - only if it is still this app's (a second app may have taken over)."""
    try:
        with open(DISCOVERY_FILE, encoding="utf-8") as f:
            if json.load(f).get("pid") != os.getpid():
                return
        os.remove(DISCOVERY_FILE)
    except (OSError, ValueError):
        pass
