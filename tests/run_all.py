"""Runs every test: the unit tests (tests/unit), the profiler and the VS Code extension. From the repo:

    python tests/run_all.py            everything except the desktop-app test
    python tests/run_all.py --quick    a few projects of each kind
    python tests/run_all.py --app      also: the yellow button through the real desktop app (its window opens)

Needs Node.js for the extension tests (the extension runs against a stand-in for VS Code: the real
extension.js, real Python and profiler.py). py-spy is used when installed. Test projects are written
to tests/work/ (git-ignored). Exit code 0 = all passed."""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
WORK = os.path.join(HERE, "work")
PY = sys.executable
QUICK = ["flat", "package", "src_layout", "relative_imports", "crash_nested", "adjacent", "visualizer_linked"]


def run(title, cmd, env=None):
    print(f"\n=== {title}", flush=True)
    t = time.time()
    r = subprocess.run(cmd, cwd=HERE, env={**os.environ, "HCV_PYTHON": PY, **(env or {})},
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    for line in r.stdout.splitlines():
        if line.startswith(("PASS", "FAIL", "ALL PASS", "SOME FAILED", "Ran ", "OK", "ERROR:")) or "EXCEPTION" in line:
            print("  " + line[:220])
    ok = r.returncode == 0 and "SOME FAILED" not in r.stdout and "\nFAIL " not in "\n" + r.stdout
    print(f"  -> {'ok' if ok else 'FAILED'} ({time.time() - t:.0f} s)", flush=True)
    return ok


def has_pyspy():
    if shutil.which("py-spy"):
        return True
    d = os.path.dirname(PY)
    return any(os.path.isfile(os.path.join(d, sub, n)) for sub in ("", "Scripts") for n in ("py-spy.exe", "py-spy"))


def venv_project():
    """A project with its own venv (no psutil in it): the Python extension's interpreter must be used,
    and its package only exists in the venv."""
    venv = os.path.join(WORK, "venvproj")
    site = os.path.join(venv, ".venv", "Lib", "site-packages", "vpkg")
    if not os.path.exists(os.path.join(venv, ".venv", "pyvenv.cfg")):
        subprocess.run([PY, "-m", "venv", "--without-pip", os.path.join(venv, ".venv")], check=True)
        os.makedirs(site, exist_ok=True)
        with open(os.path.join(site, "__init__.py"), "w") as f:
            f.write("def heavy():\n    import time\n    t = time.perf_counter()\n"
                    "    while time.perf_counter() - t < 1.2:\n        sum(i * i for i in range(2000))\n")
        with open(os.path.join(venv, "main.py"), "w") as f:
            f.write("import sys, vpkg\nprint('exe', sys.executable)\ndef go():\n    vpkg.heavy()\ngo()\n")
        open(os.path.join(venv, "requirements.txt"), "w").close()
    vpy = os.path.join(venv, ".venv", "Scripts", "python.exe") if os.name == "nt" else os.path.join(venv, ".venv", "bin", "python")
    with open(os.path.join(WORK, "venv_projects.json"), "w") as f:
        json.dump({"venv_real": {"ws": venv, "entry": os.path.join(venv, "main.py"),
                                 "py": [os.path.join(venv, "main.py"), os.path.join(site, "__init__.py")],
                                 "hot": ["main.py", "go"], "not_files": [".venv"]}}, f)
    return vpy


def demo_project():
    demo = os.path.join(REPO, "examples", "demo_project")
    files = ("main.py", "app/loader.py", "app/compute.py", "app/network.py", "app/report.py")
    with open(os.path.join(WORK, "demo_projects.json"), "w") as f:
        json.dump({"demo_project": {"ws": demo, "entry": os.path.join(demo, "main.py"),
                                    "py": [os.path.join(demo, x) for x in files],
                                    "hot": ["app/compute.py", "totals_slow"], "memory": "app/loader.py", "spread": True}}, f)


def close_app():
    """Closes the desktop app the --app test started (found through app.json)."""
    if os.name == "nt":
        path = os.path.join(os.environ.get("LOCALAPPDATA", ""), "HCV", "app.json")
    else:
        path = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "hcv", "app.json")
    try:
        with open(path, encoding="utf-8") as f:
            pid = json.load(f)["pid"]
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.kill(pid, 15)
    except (OSError, ValueError, KeyError):
        pass


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    quick = "--quick" in sys.argv
    pick = QUICK if quick else []
    os.makedirs(WORK, exist_ok=True)
    node = shutil.which("node")
    results = {}

    def build():
        subprocess.run([PY, os.path.join(HERE, "build_only.py")], check=True, stdout=subprocess.DEVNULL)

    results["unit tests"] = run("unit tests (model + controller classes)", [PY, os.path.join("unit", "run.py")])
    build()
    results["profiler, built-in sampler"] = run("profiler, built-in sampler", [PY, "run_tests.py", "new", "builtin", *pick])
    if has_pyspy():
        results["profiler, py-spy"] = run("profiler, py-spy", [PY, "run_tests.py", "new", "auto", *pick])
    results["visualizer command"] = run("visualizer YOUR CODE command", [PY, "vis_test.py"])
    if not node:
        print("\nNode.js not found: the VS Code extension tests are skipped.")
    else:
        build()
        results["extension: yellow run"] = run("extension: yellow button", [node, "e2e.js", "runAll", *pick])
        results["extension: flame"] = run("extension: flame button", [node, "e2e.js", "profile", *pick])
        vpy = venv_project()
        results["extension: project venv"] = run("extension: project venv", [node, "e2e.js", "runAll"],
                                                 {"PROJECTS": "venv_projects.json", "VENV_PY": vpy})
        demo_project()
        results["extension: demo project"] = run("extension: examples/demo_project", [node, "e2e.js", "runAll"],
                                                 {"PROJECTS": "demo_projects.json"})
        if "--app" in sys.argv:
            build()
            try:
                results["extension + desktop app"] = run("extension: yellow button through the desktop app",
                                                         [node, "e2e.js", "viaApp", "visualizer_linked", "package"])
            finally:
                close_app()
    print("\n=== summary")
    for k, ok in results.items():
        print(f"  {'ok    ' if ok else 'FAILED'}  {k}")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
