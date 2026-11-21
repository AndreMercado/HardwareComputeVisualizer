"""Test projects for the HCV profiler: many folder layouts. Each project:
files {relpath: source}, entry (relpath run by the button), expectations."""
import textwrap

BURN = """
def _burn(seconds):
    import time
    t = time.perf_counter()
    n = 0
    while time.perf_counter() - t < seconds:
        n += sum(i * i for i in range(2000))
    return n
"""


def d(s):
    return textwrap.dedent(s).lstrip("\n")


P = {}

# 1. flat folder: script + sibling modules
P["flat"] = dict(files={
    "main.py": d("""
        import utils, helpers
        def main():
            helpers.quick()
            utils.slow()
        main()
        """),
    "utils.py": BURN + d("""
        def slow():
            return _burn(1.2)
        """),
    "helpers.py": d("""
        def quick():
            return sum(range(1000))
        """),
}, entry="main.py", hot=("utils.py", "slow"))

# 2. package tree, entry at the top, hot spot two levels down, memory in another subpackage
P["package"] = dict(files={
    "run.py": d("""
        from app.core.engine import crunch
        from app.io.loader import load
        data = load()
        crunch()
        print("rows", len(data))
        """),
    "app/__init__.py": "",
    "app/core/__init__.py": "",
    "app/core/engine.py": BURN + d("""
        def crunch():
            return _burn(1.2)
        """),
    "app/io/__init__.py": "",
    "app/io/loader.py": d("""
        import time
        def load():
            rows = [bytes(1000) for _ in range(60000)]
            time.sleep(0.4)
            return rows
        """),
}, entry="run.py", hot=("app/core/engine.py", "crunch"), memory="app/io/loader.py")

# 3. src layout: script in scripts/, library in src/ (outside the script's folder)
P["src_layout"] = dict(files={
    "pyproject.toml": '[project]\nname = "mylib"\nversion = "0.1"\n',
    "src/mylib/__init__.py": "",
    "src/mylib/algo.py": BURN + d("""
        def solve():
            return _burn(1.2)
        """),
    "scripts/bench.py": d("""
        import os, sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
        from mylib.algo import solve
        solve()
        """),
}, entry="scripts/bench.py", hot=("src/mylib/algo.py", "solve"))

# 4. file inside a package that uses relative imports (python -m style)
P["relative_imports"] = dict(files={
    "pkg/__init__.py": "",
    "pkg/core.py": BURN + d("""
        def work():
            return _burn(1.2)
        """),
    "pkg/cli.py": d("""
        from .core import work
        if __name__ == "__main__":
            work()
        """),
}, entry="pkg/cli.py", hot=("pkg/core.py", "work"))

# 5. paths relative to the project folder (VS Code runs from the workspace folder)
P["cwd_data"] = dict(files={
    "data/numbers.txt": "\n".join(str(i) for i in range(1000)),
    "tools/process.py": BURN + d("""
        with open("data/numbers.txt") as f:
            nums = [int(x) for x in f]
        _burn(1.0)
        print(sum(nums))
        """),
}, entry="tools/process.py", hot=("tools/process.py", "_burn"))

# 6. user modules named like the visualizer's own files
P["name_clash"] = dict(files={
    "main.py": d("""
        import time
        time.sleep(0.3)   # the visualizer's hardware thread has started by now
        from analysis import summarize
        from control import steer
        import telemetry
        steer(); telemetry.ping(); summarize()
        """),
    "analysis.py": BURN + d("""
        def summarize():
            return _burn(1.2)
        """),
    "control.py": "def steer():\n    return 1\n",
    "telemetry.py": "def ping():\n    return 2\n",
}, entry="main.py", hot=("analysis.py", "summarize"))

# 7. virtualenv inside the project: library time counts for the user's calling line
P["venv_inside"] = dict(files={
    ".venv/Lib/site-packages/fakepkg/__init__.py": BURN + d("""
        def heavy():
            return _burn(1.2)
        """),
    ".venv/pyvenv.cfg": "home = x\n",
    "main.py": d("""
        import os, sys
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv", "Lib", "site-packages"))
        import fakepkg
        def go():
            fakepkg.heavy()
        go()
        """),
}, entry="main.py", hot=("main.py", "go"), not_files=(".venv",))

# 8. spaces and non-ASCII in folder names
P["spaces_unicode"] = dict(files={
    "sub dir ü/run me.py": d("""
        import helper_mod
        helper_mod.spin()
        """),
    "sub dir ü/helper_mod.py": BURN + d("""
        def spin():
            return _burn(1.2)
        """),
}, entry="sub dir ü/run me.py", hot=("sub dir ü/helper_mod.py", "spin"), ws_name="My Proj ü")

# 9. crash deep inside the package: red box in that module
P["crash_nested"] = dict(files={
    "main.py": "from app.svc import handle\nhandle()\n",
    "app/__init__.py": "",
    "app/svc.py": "from app.db import fetch\ndef handle():\n    return fetch('missing')\n",
    "app/db.py": "TABLE = {}\ndef fetch(key):\n    return TABLE[key]\n",
}, entry="main.py", crash=("app/db.py", 3))

# 10. multiprocessing pool from a package (Windows spawn re-imports the main script)
P["multiprocessing"] = dict(files={
    "main.py": d("""
        from concurrent.futures import ProcessPoolExecutor
        from work import chunk
        def main():
            with ProcessPoolExecutor(4) as ex:
                print(sum(ex.map(chunk, range(4))))
        if __name__ == "__main__":
            main()
        """),
    "work.py": BURN + d("""
        def chunk(i):
            return _burn(1.0)
        """),
}, entry="main.py", hot_pyspy=("work.py", "chunk"), timeout=120)

# 11. package with __main__.py (python -m tool)
P["dunder_main"] = dict(files={
    "tool/__init__.py": "",
    "tool/impl.py": BURN + d("""
        def go():
            return _burn(1.2)
        """),
    "tool/__main__.py": "from .impl import go\ngo()\n",
}, entry="tool/__main__.py", hot=("tool/impl.py", "go"))

# 12. script deep in the tree, library at the top
P["deep_script"] = dict(files={
    "lib/__init__.py": "",
    "lib/mathx.py": BURN + d("""
        def heavy():
            return _burn(1.2)
        """),
    "a/b/c/d/e/f/g/run.py": d("""
        import os, sys
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *[".."] * 7)))
        from lib.mathx import heavy
        heavy()
        """),
}, entry="a/b/c/d/e/f/g/run.py", hot=("lib/mathx.py", "heavy"))

# 13. monorepo: workspace holds several projects; script uses a sibling project's package
P["monorepo"] = dict(files={
    "shared/common/__init__.py": "",
    "shared/common/text.py": BURN + d("""
        def tokenize():
            return _burn(1.2)
        """),
    "services/api/server.py": d("""
        import os, sys
        sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "shared")))
        from common.text import tokenize
        tokenize()
        """),
}, entry="services/api/server.py", hot=("shared/common/text.py", "tokenize"))

# 14. threads: worker threads do the work (py-spy sees them; built-in sees main waiting)
P["threads"] = dict(files={
    "main.py": d("""
        import threading
        from jobs import job
        ts = [threading.Thread(target=job) for _ in range(3)]
        for t in ts: t.start()
        for t in ts: t.join()
        """),
    "jobs.py": BURN + d("""
        def job():
            return _burn(1.0)
        """),
}, entry="main.py", hot_pyspy=("jobs.py", "job"))

# 15. script reads input() - a normal run in a terminal; here stdin is piped
P["stdin_input"] = dict(files={
    "ask.py": BURN + d("""
        name = input("name? ")
        _burn(0.8)
        print("hi", name)
        """),
}, entry="ask.py", stdin="andre\n", hot=("ask.py", "_burn"))

# 16. script uses __file__ and sys.argv[0] to find its own folder
P["dunder_file"] = dict(files={
    "cfg/settings.json": '{"n": 3}',
    "app/main.py": BURN + d("""
        import json, os, sys
        here = os.path.dirname(os.path.abspath(__file__))
        assert os.path.abspath(sys.argv[0]) == os.path.abspath(__file__), (sys.argv[0], __file__)
        cfg = json.load(open(os.path.join(here, "..", "cfg", "settings.json")))
        assert __name__ == "__main__"
        _burn(0.8)
        """),
}, entry="app/main.py", hot=("app/main.py", "_burn"))

# 17. time spread over many functions: top 5 boxed, the rest get faint marks
P["many_lines"] = dict(files={
    "main.py": "import time\n" + "\n".join(
        f"def step{i}():\n    t = time.perf_counter()\n    n = 0\n    while time.perf_counter() - t < {0.05 + 0.03 * i:.2f}:\n"
        f"        n += sum(j * j for j in range(2000))\n    return n\n" for i in range(12)) +
        "\n" + "\n".join(f"step{i}()" for i in range(12)) + "\n",
}, entry="main.py", hot=("main.py", "step11"), faint=True)

# 18. run recorded by the Hardware Visualizer too: its sensors land on the lines
P["visualizer_linked"] = dict(files={
    "job.py": BURN + d("""
        def crunch():
            return _burn(1.5)
        crunch()
        """),
}, entry="job.py", hot=("job.py", "_burn"), link_vis=True)

# 19. real GPU work (PyTorch CUDA): GPU readings and patterns must stay
P["gpu_torch"] = dict(files={
    "train.py": d("""
        import time, torch
        x = torch.randn(4096, 4096, device="cuda")
        t = time.perf_counter()
        while time.perf_counter() - t < 2.0:
            y = x @ x
            torch.cuda.synchronize()
        print(float(y[0, 0]))
        """),
}, entry="train.py", hot=("train.py", "<module>"), gpu=True, timeout=120)

# 20. lines under each other become one box (speed: loop + genexpr; memory: with a comment between)
P["adjacent"] = dict(files={
    "work.py": d("""
        import time
        def work():
            t = time.perf_counter()
            n = 0
            while time.perf_counter() - t < 1.2:
                n += sum(i * i for i in range(2000))
                n += sum(i for i in range(3000))
            data = [bytes(1000) for _ in range(20000)]
            more = [bytes(1000) for _ in range(20000)]
            # a comment between still counts as "under each other"
            extra = [bytes(500) for _ in range(20000)]
            return n, data, more, extra


        result = work()
        time.sleep(1.5)  # the program keeps using the data
        """),
}, entry="work.py", hot=("work.py", "work"), adjacent=True)
