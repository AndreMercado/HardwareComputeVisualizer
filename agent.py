"""AI Benchmark Mode agent (SRS UC-7, FR-7..9).

    python agent.py <workdir> <model> <goal_id> <seconds>

A local Ollama model (127.0.0.1 only) is given a goal and a few tools and works
out for itself what to do - nothing is scripted. Any Python it writes runs via
sandbox_run.py (no network, no programs, files only in <workdir>). One JSON
line per event goes to stdout for the UI:
    {"ev": "say"|"tool"|"result"|"blocked"|"done"|"timeout", "text": ...}
    {"error": "..."}    (then exit code 2)
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
# first installed one wins; both are non-"thinking" models that support tool calls
MODELS = ["llama3.2:3b", "qwen2.5:7b"]
MAX_STEPS = 30
RUN_TIMEOUT = 120  # seconds per run_python call
HERE = os.path.dirname(os.path.abspath(__file__))

GOALS = {
    "classifier": ("Train a tiny image classifier",
                   "Train a small neural-network image classifier. Generate a synthetic image dataset "
                   "yourself (for example 10,000 28x28 images of simple shapes or patterns in 4 classes), "
                   "train a small CNN with PyTorch on the GPU for several epochs, then report the test "
                   "accuracy and the training time."),
    "render": ("Render a scene",
               "Render a 3D scene. Using numpy only (no other graphics libraries), ray-trace a scene with "
               "several shaded spheres and a floor at 640x480 and save it as image.ppm in your working "
               "folder. Report how long the render took."),
    "matmul": ("Crunch big matrices",
               "Compare CPU and GPU speed. Time large matrix multiplications (for example 4096x4096 "
               "float32) with numpy on the CPU and with PyTorch on the GPU, repeating enough times to get "
               "stable numbers, then report the speedup."),
}

SYSTEM = (
    "You are an autonomous agent working on a task on this Windows PC. You act only through the tools "
    "provided. You have a private working folder; run_python executes Python 3.13 code inside it. "
    "Installed: numpy and PyTorch (CUDA GPU available via torch.cuda). There is no internet, no shell and "
    "no access outside the working folder; if something is refused, adapt. Work in small steps, run code, "
    "read the output, fix errors. Keep each run under about 90 seconds. Output only appears if the code "
    "calls print() - a bare expression on the last line shows nothing. When the task is done, call finish with a 2-3 sentence summary of what you did and the numbers "
    "you measured."
)

TOOLS = [
    {"type": "function", "function": {
        "name": "run_python", "description": "Run Python code in the working folder and return its output.",
        "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write a text file in the working folder.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                        "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a text file from the working folder.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "list_files", "description": "List files in the working folder.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "finish", "description": "Declare the task finished and give a short summary.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                       "required": ["summary"]}}},
]


def emit(ev=None, **kw):
    d = {"ev": ev, **kw} if ev else kw
    print(json.dumps(d), flush=True)


def ollama_models():
    """Installed model names, or None if Ollama isn't reachable."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None


def pick_model(installed):
    for m in MODELS:
        if m in installed:
            return m
    return None


def chat(model, messages, timeout):
    body = json.dumps({"model": model, "messages": messages, "tools": TOOLS, "stream": False,
                       "keep_alive": "10m",
                       "options": {"num_ctx": 8192, "num_predict": 1500, "temperature": 0.3}}).encode()
    req = urllib.request.Request(OLLAMA + "/api/chat", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)["message"]


class Blocked(Exception):
    pass


def safe_path(workdir, rel):
    p = os.path.realpath(os.path.join(workdir, rel))
    if p != workdir and not p.startswith(workdir + os.sep):
        raise Blocked(f"path outside the working folder: {rel}")
    return p


def tail(s, n=2500):
    return s if len(s) <= n else "...(cut)...\n" + s[-n:]


def run_python(workdir, step, code, deadline):
    script = os.path.join(workdir, f"step_{step}.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(code)
    tmp = os.path.join(workdir, ".tmp")
    os.makedirs(tmp, exist_ok=True)
    env = dict(os.environ, TEMP=tmp, TMP=tmp, PYTHONIOENCODING="utf-8")
    limit = max(5, min(RUN_TIMEOUT, deadline - time.time()))
    try:
        p = subprocess.run([sys.executable, os.path.join(HERE, "sandbox_run.py"), workdir, script],
                           cwd=workdir, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=limit, env=env)
    except subprocess.TimeoutExpired:
        return f"TIMEOUT: the run was stopped after {limit:.0f} s. Use less work per run."
    for line in p.stderr.splitlines():
        if line.startswith("BLOCKED:"):
            emit("blocked", text=line[8:].strip())
    hint = ""
    if p.returncode == 0 and not p.stdout.strip():
        hint = "\n(The code printed nothing. Use print() to see values.)"
    return f"exit code {p.returncode}\nstdout:\n{tail(p.stdout)}\nstderr:\n{tail(p.stderr)}{hint}"


def call_tool(workdir, step, name, args, deadline):
    if name == "run_python":
        return run_python(workdir, step, str(args.get("code", "")), deadline)
    if name == "write_file":
        p = safe_path(workdir, str(args.get("path", "")))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(str(args.get("content", "")))
        return f"wrote {args.get('path')}"
    if name == "read_file":
        with open(safe_path(workdir, str(args.get("path", ""))), "r", encoding="utf-8", errors="replace") as f:
            return tail(f.read())
    if name == "list_files":
        out = [n for n in sorted(os.listdir(workdir)) if not n.startswith(".")]
        return "\n".join(out) or "(empty)"
    return f"unknown tool: {name}"


def unload(model):
    try:
        req = urllib.request.Request(OLLAMA + "/api/generate",
                                     json.dumps({"model": model, "keep_alive": 0}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        pass


def main():
    workdir = os.path.realpath(sys.argv[1])
    model, goal_id, seconds = sys.argv[2], sys.argv[3], float(sys.argv[4])
    deadline = time.time() + seconds
    title, goal = GOALS[goal_id]
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": f"Task: {goal}"}]
    emit("say", text=f"Goal: {title}. Model: {model}.")
    nudges = 0
    try:
        for step in range(1, MAX_STEPS + 1):
            if time.time() >= deadline:
                emit("timeout", text="time limit reached")
                return
            msg = chat(model, messages, max(5, min(180, deadline - time.time())))
            messages.append(msg)
            if msg.get("content"):
                emit("say", text=msg["content"].strip())
            calls = msg.get("tool_calls") or []
            if not calls:
                nudges += 1
                if nudges > 2:
                    emit("done", text=(msg.get("content") or "agent stopped without finishing").strip())
                    return
                messages.append({"role": "user", "content": "Continue using the tools, or call finish."})
                continue
            for c in calls:
                fn = c["function"]
                name, args = fn["name"], fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if name == "finish":
                    emit("done", text=str(args.get("summary", "")).strip())
                    return
                shown = args.get("code") or args.get("path") or ""
                shown = str(shown)
                emit("tool", text=f"{name}({shown[:300]}{'...' if len(shown) > 300 else ''})")
                try:
                    result = call_tool(workdir, step, name, args, deadline)
                except Blocked as e:
                    emit("blocked", text=str(e))
                    result = f"BLOCKED: {e}"
                except Exception as e:
                    result = f"tool error: {type(e).__name__}: {e}"
                emit("result", text=tail(result, 600))
                messages.append({"role": "tool", "content": tail(result)})
        emit("done", text="step limit reached")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        if time.time() >= deadline - 5:  # the limit cut a model call short, not an Ollama failure
            emit("timeout", text="time limit reached")
            return
        emit(error=f"Ollama stopped responding ({e})")
        sys.exit(2)
    finally:
        unload(model)


if __name__ == "__main__":
    main()
