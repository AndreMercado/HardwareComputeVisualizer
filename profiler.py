"""Line profiler for the user's own Python code (drives the VS Code extension's boxes).

    python profiler.py [--out PROFILE.json] [--memory] [--interval MS] script.py [script args...]

Runs script.py as __main__ and, every few ms, notes which line of *your* code is
running: the innermost frame in a file of your project, so time spent inside numpy,
PyTorch or the standard library is charged to the line of yours that called it.
Each sample is weighted by the time since the previous one (wall-clock time).
At the same moments it records what the hardware was doing: how many cores *this
program* kept busy (100 = one core; other programs don't count), the whole CPU, and
the GPU - so a hot spot can say "one core busy, GPU idle while here".

The result is written to PROFILE.json (default: .hcv/profile.json next to the
script) every second while the script runs - the editor updates live - and once
more at the end. --memory also records, for each line, the most memory its
allocations held at once (tracemalloc, checked every second; makes the script
slower, so times are less exact).
Only the main thread is profiled; child processes are not.

Accuracy: a sampler inside the program can only look when Python lets other threads
run, so time inside long C calls (sort, regex, json...) lands on the next line, and
short functions on their `def` line. When py-spy is installed (pip install py-spy) it
is used for the final numbers instead: it samples from outside the program, sees every
line exactly, plus all threads and child processes. The in-process sampler still draws
the live boxes while the program runs and records the hardware readings.
"""
import argparse
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_PARTS = ("site-packages", "dist-packages", os.sep + "lib" + os.sep + "python")


def _norm(path):
    return os.path.normcase(os.path.realpath(path))


class Profiler:
    def __init__(self, script, out, interval_ms=5, memory=False):
        self.script = os.path.realpath(script)
        self.root = _norm(os.path.dirname(self.script))
        self.out = out
        self.interval = interval_ms / 1000.0
        self.memory = memory
        self.lines = {}        # (file, line) -> [seconds, func, {hw sums}, hw samples]
        self.hw = {}           # latest hardware reading
        self.hw_peak_gpu = 0.0
        self.t0 = time.perf_counter()
        self.sampled = 0.0
        self.done = threading.Event()
        self.result = None     # how the script ended
        self.exit_code = None
        self.crash = None      # {file, line, error}: where the script failed, in the user's code
        self.finalizing = False  # True when py-spy will write the final numbers after this process
        self._mine = {}        # filename -> is it one of the user's files?
        self.main_ident = threading.main_thread().ident
        self.cores = os.cpu_count() or 1
        self.mem_peak = {}     # (file, line) -> most bytes held at once by that line's allocations

    def _is_mine(self, filename):
        hit = self._mine.get(filename)
        if hit is None:
            p = _norm(filename) if filename and not filename.startswith("<") else ""
            hit = bool(p) and p.startswith(self.root) and not any(s in p for s in SKIP_PARTS) \
                and not p.startswith(_norm(HERE) + os.sep + "profiler.py")
            self._mine[filename] = hit
        return hit

    # ---------------------------------------------------------------- sampling
    def _sample_loop(self):
        last = time.perf_counter()
        while not self.done.is_set():
            time.sleep(self.interval)
            now = time.perf_counter()
            dt, last = now - last, now
            frame = sys._current_frames().get(self.main_ident)
            while frame is not None and not self._is_mine(frame.f_code.co_filename):
                frame = frame.f_back
            self.sampled += dt
            if frame is None:
                continue  # interpreter start-up / shutdown, outside the user's code
            key = (frame.f_code.co_filename, frame.f_lineno)
            rec = self.lines.get(key)
            if rec is None:
                rec = self.lines[key] = [0.0, frame.f_code.co_name,
                                         {"proc": 0.0, "cpu": 0.0, "core_max": 0.0, "gpu": 0.0}, 0]
            rec[0] += dt
            hw = self.hw
            if hw:
                for k in rec[2]:
                    v = hw.get(k)
                    if v is not None:
                        rec[2][k] += v * dt
                rec[3] += dt

    def _hardware_loop(self):
        try:
            import psutil
        except ImportError:
            return
        gpu = None
        try:
            sys.path.insert(0, HERE)
            import nvgpu
            if nvgpu.OK and nvgpu.names():
                gpu = nvgpu.pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            gpu = None
        me = psutil.Process()
        kids = {}

        def own_cpu():
            """CPU used by this program and its child processes, 100 = one core."""
            total = me.cpu_percent(None)
            try:
                for c in me.children(recursive=True):
                    p = kids.setdefault(c.pid, c)
                    total += p.cpu_percent(None)
            except psutil.Error:
                pass
            return total

        psutil.cpu_percent(None, percpu=True)
        own_cpu()
        while not self.done.is_set():
            time.sleep(0.1)
            cores = psutil.cpu_percent(None, percpu=True)
            reading = {"proc": own_cpu(), "cpu": sum(cores) / len(cores) if cores else None,
                       "core_max": max(cores) if cores else None, "gpu": None}
            if gpu is not None:
                try:
                    reading["gpu"] = float(nvgpu.pynvml.nvmlDeviceGetUtilizationRates(gpu).gpu)
                    self.hw_peak_gpu = max(self.hw_peak_gpu, reading["gpu"])
                except Exception:
                    pass
            self.hw = reading

    def _writer_loop(self):
        while not self.done.wait(1.0):
            if self.memory:
                self._memory_check()
            self.write(running=True)

    # ---------------------------------------------------------------- output
    def write(self, running):
        if not running and self.finalizing:
            running = True  # the parent (py-spy) writes the final, exact numbers next
        elapsed = time.perf_counter() - self.t0
        total = sum(r[0] for r in list(self.lines.values())) or 1e-9
        lines = []
        for (fname, line), (secs, func, hw, hw_t) in sorted(list(self.lines.items()), key=lambda kv: -kv[1][0]):
            item = {"file": os.path.realpath(fname), "line": line, "func": func, "seconds": round(secs, 4),
                    "share": round(secs / total, 5)}
            if hw_t > 0:
                item["hw"] = {k: (round(v / hw_t, 1) if v else 0.0) for k, v in hw.items()}
            lines.append(item)
        data = {"version": 1, "script": self.script, "running": running, "elapsed_s": round(elapsed, 2),
                "profiled_s": round(total, 3), "interval_ms": self.interval * 1000, "cores": self.cores,
                "gpu_used": self.hw_peak_gpu >= 30, "result": self.result, "exit_code": self.exit_code,
                "engine": "builtin", "crash": self.crash, "lines": lines[:500], "updated": time.time()}
        if self.memory:
            if not running:
                self._memory_check()
            top = sorted(self.mem_peak.items(), key=lambda kv: -kv[1])[:50]
            data["memory"] = [{"file": os.path.realpath(f), "line": ln, "bytes": b} for (f, ln), b in top]
        os.makedirs(os.path.dirname(self.out), exist_ok=True)
        tmp = self.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        for _ in range(20):  # the editor may be reading the old file: retry briefly
            try:
                os.replace(tmp, self.out)
                break
            except PermissionError:
                time.sleep(0.05)

    def _memory_check(self):
        import tracemalloc
        if not tracemalloc.is_tracing():
            return
        for stat in tracemalloc.take_snapshot().statistics("lineno"):
            fr = stat.traceback[0]
            if self._is_mine(fr.filename):
                key = (fr.filename, fr.lineno)
                if stat.size > self.mem_peak.get(key, 0):
                    self.mem_peak[key] = stat.size

    # ---------------------------------------------------------------- run
    def run(self, argv):
        if self.memory:
            import tracemalloc
            tracemalloc.start(1)
        for target in (self._sample_loop, self._hardware_loop, self._writer_loop):
            threading.Thread(target=target, daemon=True).start()
        sys.argv = [self.script] + argv
        sys.path.insert(0, os.path.dirname(self.script))
        code = 0
        try:
            runpy.run_path(self.script, run_name="__main__")
            self.result = "finished"
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
            self.result = f"exited with code {code}"
        except KeyboardInterrupt:
            code, self.result = 130, "stopped"
        except BaseException as e:
            traceback.print_exc()
            code, self.result = 1, f"crashed: {type(e).__name__}: {e}"
            mine = [fr for fr in traceback.extract_tb(e.__traceback__) if self._is_mine(fr.filename)]
            if mine:
                self.crash = {"file": os.path.realpath(mine[-1].filename), "line": mine[-1].lineno,
                              "error": f"{type(e).__name__}: {e}"[:300]}
        finally:
            self.exit_code = code
            self.done.set()
            self.write(running=False)
        return code


# ---------------------------------------------------------------- py-spy (exact numbers)
PYSPY_RATE = 200  # samples per second per thread
_FRAME = re.compile(r"^(.*) \((.+):(\d+)\)$")


def find_pyspy():
    exe = shutil.which("py-spy")
    if exe:
        return exe
    for d in (os.path.dirname(sys.executable), os.path.join(os.path.dirname(sys.executable), "Scripts")):
        for name in ("py-spy.exe", "py-spy"):
            if os.path.isfile(os.path.join(d, name)):
                return os.path.join(d, name)
    return None


def pyspy_lines(raw_path, script):
    """py-spy's collapsed stacks -> {(file, line): [seconds, func]} for the user's innermost frame
    of every sample (all threads and processes; waiting time included)."""
    judge = Profiler(script, "", 5)  # only for its _is_mine()
    lines, threads = {}, set()
    with open(raw_path, encoding="utf-8", errors="replace") as f:
        for row in f:
            stack, _, count = row.rstrip("\n").rpartition(" ")
            if not count.isdigit():
                continue
            frames = stack.split(";")
            threads.add(";".join(frames[:2]))
            for fr in reversed(frames):
                m = _FRAME.match(fr)
                if m and judge._is_mine(m.group(2)):
                    key = (os.path.realpath(m.group(2)), int(m.group(3)))
                    rec = lines.setdefault(key, [0.0, m.group(1).split(".")[-1]])
                    rec[0] += int(count) / PYSPY_RATE
                    break
    return lines, len(threads)


def run_with_pyspy(pyspy, a, out):
    """py-spy runs this file again as the program (--child: live boxes + hardware), then the
    exact line times from py-spy are merged with the child's hardware readings."""
    raw = out + ".pyspy.txt"
    child = [sys.executable, os.path.abspath(__file__), "--child", "--out", out, "--interval", str(a.interval)]
    child += (["--memory"] if a.memory else []) + [a.script] + a.args
    cmd = [pyspy, "record", "--rate", str(PYSPY_RATE), "--idle", "--threads", "--subprocesses",
           "--full-filenames", "--format", "raw", "-o", raw, "--"] + child
    os.makedirs(os.path.dirname(out), exist_ok=True)
    rc = subprocess.call(cmd)
    try:
        with open(out, encoding="utf-8") as f:
            live = json.load(f)
        exact, n_threads = pyspy_lines(raw, a.script)
    except (OSError, ValueError) as e:
        print(f"[hcv] py-spy result unusable ({e}); keeping the built-in numbers", file=sys.stderr)
        return rc
    finally:
        try:
            os.remove(raw)
        except OSError:
            pass
    if not exact:
        live["running"] = False
        exact_lines = live["lines"]
    else:
        # hardware per line comes from the in-process sampler: same line, else same function
        hw_line = {(os.path.normcase(l["file"]), l["line"]): l.get("hw") for l in live["lines"] if l.get("hw")}
        hw_func = {}
        for l in live["lines"]:
            if l.get("hw"):
                hw_func.setdefault((os.path.normcase(l["file"]), l["func"]), l["hw"])
        total = sum(v[0] for v in exact.values()) or 1e-9
        exact_lines = []
        for (fname, line), (secs, func) in sorted(exact.items(), key=lambda kv: -kv[1][0]):
            item = {"file": fname, "line": line, "func": func, "seconds": round(secs, 4),
                    "share": round(secs / total, 5)}
            hw = hw_line.get((os.path.normcase(fname), line)) or hw_func.get((os.path.normcase(fname), func))
            if hw:
                item["hw"] = hw
            exact_lines.append(item)
        live.update(profiled_s=round(total, 3), threads=n_threads)
    live.update(lines=exact_lines[:500], running=False, engine="py-spy" if exact else "builtin",
                updated=time.time())
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(live, f)
    os.replace(tmp, out)
    code = live.get("exit_code")
    return code if isinstance(code, int) else rc


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out")
    ap.add_argument("--memory", action="store_true")
    ap.add_argument("--interval", type=float, default=5.0, help="ms between samples (default 5)")
    ap.add_argument("--engine", choices=("auto", "py-spy", "builtin"), default="auto",
                    help="py-spy (exact, if installed) or the built-in sampler (approximate)")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("script")
    ap.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if not os.path.isfile(a.script):
        sys.exit(f"no such file: {a.script}")
    out = os.path.realpath(a.out or os.path.join(os.path.dirname(os.path.realpath(a.script)), ".hcv", "profile.json"))
    pyspy = find_pyspy() if a.engine != "builtin" and not a.child else None
    if a.engine == "py-spy" and not pyspy:
        sys.exit("py-spy isn't installed: pip install py-spy")
    if pyspy:
        print(f"[hcv] profiling {a.script} with py-spy -> {out}", file=sys.stderr, flush=True)
        sys.exit(run_with_pyspy(pyspy, a, out))
    prof = Profiler(a.script, out, a.interval, a.memory)
    prof.finalizing = a.child
    if not a.child:
        print(f"[hcv] profiling {a.script} -> {out} (built-in sampler; pip install py-spy for exact numbers)",
              file=sys.stderr, flush=True)
    sys.exit(prof.run(a.args))


if __name__ == "__main__":
    main()
