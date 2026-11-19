"""MCP server: lets an AI assistant (Claude Code, Claude Desktop, any MCP client) drive
the Hardware Compute Visualizer through its AI CONTROL API (control.py).

The app must be open with AI CONTROL switched on. Registered for Claude Code in
.mcp.json; for other clients run:  python mcp_server.py   (stdio, JSON-RPC 2.0).
Standard library only.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import control  # noqa: E402

BASE = f"http://127.0.0.1:{control.PORT}"


def _tool(name, desc, props=None, required=()):
    return {"name": name, "description": desc,
            "inputSchema": {"type": "object", "properties": props or {}, "required": list(required)}}


S = {"type": "string"}
TOOLS = {
    "get_state": (_tool("get_state", "What the app shows now: target machine, detected parts, selected part, "
                                     "chosen test, whether a test is running, live readings and the status line."),
                  "GET", "state"),
    "select_part": (_tool("select_part", "Click a part in the diagram: ram, cpu, gpu, board or disk. Optional "
                                         "name picks one of several (e.g. a disk or a GPU) by part of its name.",
                          {"part": S, "name": S}, ["part"]), "POST", "select"),
    "choose_test": (_tool("choose_test", "Choose the test: 2d (CPU/RAM), 3d (GPU), ml (GPU/CPU), io (disk), "
                                         "bus (board PCIe), ai (AI benchmark), code (your own program).",
                          {"test": S}, ["test"]), "POST", "test"),
    "set_program": (_tool("set_program", "For the YOUR CODE test: the command line that starts the program "
                                         "(a .py file runs with Python).", {"command": S}, ["command"]),
                    "POST", "program"),
    "set_ai_task": (_tool("set_ai_task", "For the AI BENCHMARK test: classifier, render or matmul.",
                          {"task": S}, ["task"]), "POST", "ai_task"),
    "start_test": (_tool("start_test", "Press START. If the app asks a question (e.g. no temperature sensor, so "
                                       "no thermal safety stop) the reply says so; call again with confirm=true "
                                       "only if going ahead is reasonable.",
                         {"confirm": {"type": "boolean"}}), "POST", "start"),
    "stop_test": (_tool("stop_test", "Press STOP."), "POST", "stop"),
    "wait_for_test": (_tool("wait_for_test", "Wait until the running test ends (default up to 600 s), then "
                                             "return the final state.", {"timeout": {"type": "number"}}),
                      "POST", "wait"),
    "get_summary": (_tool("get_summary", "The last run's summary: min/avg/peak of every reading, comparison "
                                         "with the previous run, and the bottlenecks found."), "GET", "summary"),
    "list_runs": (_tool("list_runs", "Recorded runs, newest first, with their results and bottlenecks."),
                  "GET", "runs"),
    "compare_runs": (_tool("compare_runs", "Open the COMPARE window with two recorded runs (file names from "
                                           "list_runs; A = before, B = after).", {"a": S, "b": S}),
                     "POST", "compare"),
    "replay_run": (_tool("replay_run", "Replay a recorded run in the diagram.", {"run": S}, ["run"]),
                   "POST", "replay"),
    "connect_remote": (_tool("connect_remote", "Make a remote Linux machine the target over SSH (key login).",
                             {"host": S, "user": S, "key": S, "port": {"type": "integer"}},
                             ["host", "user", "key"]), "POST", "remote"),
    "use_this_pc": (_tool("use_this_pc", "Make this PC the target again."), "POST", "local"),
}


def call_app(method, action, args):
    try:
        with open(control.TOKEN_FILE, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError:
        return True, {"error": "AI control is off: open the app and press AI CONTROL "
                               "(or start it with: python main.py --ai-control)."}
    data = json.dumps(args).encode() if method == "POST" else None
    req = urllib.request.Request(f"{BASE}/{action}", data=data, method=method,
                                 headers={"X-HCV-Token": token, "Content-Type": "application/json"})
    timeout = float(args.get("timeout") or 600) + 30 if action == "wait" else 60
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return False, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return True, json.load(e)
        except ValueError:
            return True, {"error": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return True, {"error": f"the app isn't answering ({e}). Is it open with AI CONTROL on?"}


def handle(msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion") or "2024-11-05"
        return {"protocolVersion": version, "capabilities": {"tools": {}},
                "serverInfo": {"name": "hardware-visualizer", "version": "1.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": [t[0] for t in TOOLS.values()]}
    if method == "tools/call":
        params = msg.get("params") or {}
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOLS:
            return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
        _, http_method, action = TOOLS[name]
        is_error, body = call_app(http_method, action, args)
        return {"content": [{"type": "text", "text": json.dumps(body, indent=1, ensure_ascii=False)}],
                "isError": is_error}
    if mid is not None:
        raise KeyError(method)
    return None  # a notification: no answer


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        try:
            result = handle(msg)
            if mid is None:
                continue
            out = {"jsonrpc": "2.0", "id": mid, "result": result}
        except KeyError as e:
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {e}"}}
        except Exception as e:
            out = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
