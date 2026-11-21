"""Builds every test project into ws_e2e/ and prints {name: {ws, entry, ...}} as JSON."""
import json, os, shutil, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from projects import P
WS = os.path.join(HERE, "work", "ws_e2e")
out = {}
for name, spec in P.items():
    ws = os.path.join(WS, spec.get("ws_name", name))
    shutil.rmtree(ws, ignore_errors=True)
    for rel, src in spec["files"].items():
        p = os.path.join(ws, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w", encoding="utf-8").write(src)
    out[name] = dict(ws=ws, entry=os.path.join(ws, spec["entry"]),
                     py=[os.path.join(ws, r) for r in spec["files"] if r.endswith(".py")],
                     **{k: spec[k] for k in ("hot", "hot_pyspy", "memory", "crash", "stdin", "not_files", "faint", "link_vis", "gpu", "adjacent") if k in spec})
json.dump(out, open(os.path.join(HERE, "work", "e2e_projects.json"), "w", encoding="utf-8"), indent=1)
print(len(out), "projects built")
