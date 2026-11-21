"""Visualizer mode: the extension's command string, run the way the visualizer's YOUR CODE runs it."""
import json, os, subprocess, sys
HCV = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
P = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "work", "e2e_projects.json"), encoding="utf-8"))
ok = True
for name in ("cwd_data", "src_layout", "spaces_unicode", "relative_imports"):
    p = P[name]
    ws, entry = os.path.normpath(p["ws"]), os.path.normpath(p["entry"])
    out = os.path.join(os.path.dirname(entry), ".hcv", "profile.json")
    q = lambda a: f'"{a}"'
    command = f'"{sys.executable}" ' + " ".join(map(q, [os.path.join(HCV, "profiler.py"), "--out", out, "--root", ws, "--cwd", ws, entry]))
    r = subprocess.run([sys.executable, os.path.join(HCV, "workloads.py"), "usercode", "60", command],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    d = json.load(open(out, encoding="utf-8"))
    files = sorted({os.path.relpath(l["file"], ws) for l in d["lines"] if l["share"] > 0.3})
    good = d["result"] == "finished" and d["lines"] and os.path.normcase(os.path.join(ws, p["hot"][0])) in {os.path.normcase(l["file"]) for l in d["lines"] if l["share"] > 0.3}
    ok &= bool(good)
    print("PASS" if good else "FAIL", name, d["result"], files, "" if good else r.stdout[-600:])
print("ALL PASS" if ok else "SOME FAILED")
