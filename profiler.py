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
"""
import argparse
import json
import os
import runpy
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
                "gpu_used": self.hw_peak_gpu >= 30, "result": self.result, "lines": lines[:500],
                "updated": time.time()}
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
        finally:
            self.done.set()
            self.write(running=False)
        return code


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out")
    ap.add_argument("--memory", action="store_true")
    ap.add_argument("--interval", type=float, default=5.0, help="ms between samples (default 5)")
    ap.add_argument("script")
    ap.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if not os.path.isfile(a.script):
        sys.exit(f"no such file: {a.script}")
    out = a.out or os.path.join(os.path.dirname(os.path.realpath(a.script)), ".hcv", "profile.json")
    prof = Profiler(a.script, os.path.realpath(out), a.interval, a.memory)
    print(f"[hcv] profiling {a.script} -> {out}", file=sys.stderr, flush=True)
    sys.exit(prof.run(a.args))


if __name__ == "__main__":
    main()
