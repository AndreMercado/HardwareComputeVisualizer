"""Runs profiler.py on every test project the way the VS Code extension does and checks the
profile. usage: python run_tests.py [old|new] [builtin|auto] [name...]
  old = how the extension ran it before (cwd = script folder, no --root)
  new = like VS Code's normal run (cwd = workspace folder, --root workspace)"""
import json, os, shutil, subprocess, sys, time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
HCV = os.path.dirname(HERE)  # the repo
PY = sys.executable
sys.path.insert(0, HERE)
from projects import P

mode = sys.argv[1] if len(sys.argv) > 1 else "new"
engine = sys.argv[2] if len(sys.argv) > 2 else "builtin"
only = sys.argv[3:]
WS = os.path.join(HERE, "work", "ws")


def build(name, spec):
    ws = os.path.join(WS, spec.get("ws_name", name))
    shutil.rmtree(ws, ignore_errors=True)
    for rel, src in spec["files"].items():
        p = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(src)
    return ws


def norm(p):
    return os.path.normcase(os.path.realpath(p))


def check(name, spec):
    ws = build(name, spec)
    entry = os.path.join(ws, spec["entry"])
    out = os.path.join(os.path.dirname(entry), ".hcv", "profile.json")
    args = [PY, os.path.join(HCV, "profiler.py"), "--out", out, "--memory", "--engine", engine]
    if mode == "new":
        args += ["--root", ws]
    args.append(entry)
    cwd = ws if mode == "new" else os.path.dirname(entry)
    t = time.time()
    try:
        r = subprocess.run(args, cwd=cwd, input=spec.get("stdin", ""), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=spec.get("timeout", 90))
        log = r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return False, f"TIMEOUT {e}", 0
    took = time.time() - t
    fails = []
    try:
        data = json.load(open(out, encoding="utf-8"))
    except Exception as e:
        return False, f"no profile ({e})\n{log[-800:]}", took
    if data.get("running"):
        fails.append("profile still says running")
    by_file = defaultdict(float)
    for l in data["lines"]:
        by_file[norm(l["file"])] += l["share"]
    rel = {os.path.relpath(f, norm(ws)): round(s, 2) for f, s in by_file.items()}
    for f in by_file:
        if os.path.dirname(f) == norm(HCV):  # the visualizer's own modules (profiler.py, analysis.py...)
            fails.append(f"visualizer file counted: {f}")
    want_crash = spec.get("crash")
    if want_crash:
        c = data.get("crash") or {}
        if not c or norm(c["file"]) != norm(os.path.join(ws, want_crash[0])) or c["line"] != want_crash[1]:
            fails.append(f"crash expected {want_crash}, got {c}")
    elif data.get("result") != "finished":
        fails.append(f"result {data.get('result')!r}")
    hot = spec.get("hot") or (spec.get("hot_pyspy") if data.get("engine") == "py-spy" else None)
    if hot:
        share = by_file.get(norm(os.path.join(ws, hot[0])), 0)
        if share < 0.5:
            fails.append(f"{hot[0]} has {share:.0%} of the time (want >=50%); by file {rel}")
    for bad in spec.get("not_files", ()):
        if any(os.path.relpath(f, norm(ws)).startswith(bad) for f in by_file):
            fails.append(f"{bad} counted as user code: {rel}")
    if spec.get("memory"):
        mem = {norm(m["file"]) for m in data.get("memory", []) if m["bytes"] >= 1e6}
        if norm(os.path.join(ws, spec["memory"])) not in mem:
            fails.append(f"no memory box in {spec['memory']}: {data.get('memory', [])[:3]}")
    detail = "; ".join(fails) if fails else f"{data.get('engine')} {data.get('result')} {rel}"
    if fails:
        detail += "\n      log: " + log[-600:].replace("\n", "\n      ")
    return not fails, detail, took


ok_all = True
for name, spec in P.items():
    if only and name not in only:
        continue
    ok, detail, took = check(name, spec)
    ok_all &= ok
    print(f"{'PASS' if ok else 'FAIL'} {name:18s} {took:5.1f}s  {detail}", flush=True)
print("ALL PASS" if ok_all else "SOME FAILED")
