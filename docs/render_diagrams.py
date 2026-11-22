"""Renders the Mermaid diagrams in the docs to PNG images in docs/img/.

    python docs/render_diagrams.py

Needs the Mermaid CLI (`mmdc`): on PATH, in the MMDC environment variable, or it falls back to
`npx -y @mermaid-js/mermaid-cli`. It drives an installed Chrome or Edge (no browser download).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

DOCS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DOCS)
IMG = os.path.join(DOCS, "img")

# (markdown file, names for its mermaid blocks in order)
SOURCES = [
    (os.path.join(DOCS, "STATECHARTS.md"),
     ["statechart_1_test_session", "statechart_2_target_machine", "statechart_3_ai_agent",
      "statechart_4_your_code_runner"]),
    (os.path.join(DOCS, "ARCHITECTURE.md"), ["architecture_processes", "class_diagram_design"]),
    (os.path.join(ROOT, "CLASS_DIAGRAM.md"), ["class_diagram_full"]),
]
BROWSERS = [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            "/usr/bin/google-chrome", "/usr/bin/chromium"]


def mmdc():
    exe = os.environ.get("MMDC") or shutil.which("mmdc")
    if exe:
        return [exe]
    return [shutil.which("npx") or "npx", "-y", "@mermaid-js/mermaid-cli"]


def blocks(path):
    with open(path, encoding="utf-8") as f:
        return re.findall(r"```mermaid\n(.*?)```", f.read(), re.S)


def main():
    os.makedirs(IMG, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="hcv-mmd-")
    browser = next((b for b in BROWSERS if os.path.isfile(b)), None)
    pcfg = os.path.join(tmp, "puppeteer.json")
    with open(pcfg, "w") as f:
        json.dump({"executablePath": browser, "headless": "new"} if browser else {"headless": "new"}, f)
    cfg = os.path.join(tmp, "mermaid.json")
    with open(cfg, "w") as f:
        json.dump({"theme": "default", "themeVariables": {"fontFamily": "Segoe UI, Arial, sans-serif"},
                   "class": {"hideEmptyMembersBox": True}, "maxTextSize": 200000}, f)
    ok = True
    for md, names in SOURCES:
        found = blocks(md)
        if len(found) != len(names):
            print(f"{os.path.basename(md)}: expected {len(names)} diagrams, found {len(found)}")
            ok = False
        for name, src in zip(names, found):
            mmd = os.path.join(tmp, name + ".mmd")
            with open(mmd, "w", encoding="utf-8") as f:
                f.write(src)
            out = os.path.join(IMG, name + ".png")
            width = "3600" if name == "class_diagram_full" else "2000"
            r = subprocess.run(mmdc() + ["-i", mmd, "-o", out, "-p", pcfg, "-c", cfg, "-b", "white",
                                         "-w", width, "-s", "2"],
                               capture_output=True, text=True, shell=os.name == "nt")
            if r.returncode != 0 or not os.path.isfile(out):
                ok = False
                print(f"FAILED {name}: {(r.stderr or r.stdout).strip()[-800:]}")
            else:
                print(f"ok  docs/img/{name}.png")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
