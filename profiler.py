"""Line profiler for the user's own Python code (drives the VS Code extension's boxes).

    python profiler.py [--out PROFILE.json] [--memory] [--interval MS] [--root PROJECT]
                       [--cwd FOLDER] script.py [script args...]

"Your code" is every file in the project folder (--root; default: the nearest folder above
the script with .git, pyproject.toml, requirements.txt, ...), minus virtual environments,
site-packages and the Python install. A file of a package that uses relative imports (or a
package's __main__.py) runs like `python -m pkg.module`.

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
SKIP_DIRS = {"site-packages", "dist-packages", ".tox", ".nox", "__pypackages__", "node_modules"}
OWN_FILES = ("profiler.py", "nvgpu.py", "analysis.py")
# per-line hardware averages: name in the profile -> key of a hardware sample
LINE_HW = {"proc": "proc", "cpu": "cpu_util", "core_max": "cpu_core_max", "gpu": "gpu_util",
           "ram": "ram_util", "disk": "disk_util", "board": "board_util", "vram": "gpu_vram"}
HW_EVERY_S = 0.1     # hardware sample interval
BUCKET_S = 0.1       # timeline resolution: which line ran most in each 100 ms
MAX_SAMPLES = 36000  # one hour of hardware samples / timeline buckets  # the profiler's own code never counts as the user's
# files or folders that mark the top folder of a project
ROOT_MARKERS = (".git", ".hg", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
                "tox.ini", ".vscode", ".idea", "environment.yml", "poetry.lock", "uv.lock")


def _norm(path):
    return os.path.normcase(os.path.realpath(path))


def num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


GPU_KEYS = ("gpu_util", "gpu_memctl", "gpu_vram", "board_util", "pcie_rx", "pcie_tx")


def _without_gpu(samples):
    """Samples with the GPU / PCIe readings removed: when this program never used the GPU, what other
    programs did on it must not turn into "GPU waiting on the CPU" or "PCIe busy"."""
    return [{k: v for k, v in r.items() if k not in GPU_KEYS} for r in samples]


def _private_module(name):
    """One of the visualizer's modules, loaded from its file under a private name: putting this
    folder on sys.path would let the visualizer's modules (analysis.py, control.py...) hide the user's."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_hcv_" + name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _python_dirs():
    """Folders of the Python installation (standard library, installed packages): never the user's."""
    import sysconfig
    dirs = {sys.prefix, sys.base_prefix, sys.exec_prefix, getattr(sys, "base_exec_prefix", sys.prefix)}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            dirs.add(sysconfig.get_paths()[key])
        except (KeyError, OSError):
            pass
    return tuple(_norm(d) + os.sep for d in dirs if d)


def project_root(script):
    """The project a script belongs to: the nearest folder above it with a project marker
    (.git, pyproject.toml, requirements.txt, .vscode...). Never the home folder or a drive's
    top; without a marker, the script's own folder."""
    start = os.path.dirname(os.path.realpath(script))
    home = _norm(os.path.expanduser("~"))
    d = start
    while True:
        parent = os.path.dirname(d)
        if parent == d or _norm(d) == home:
            break
        if any(os.path.exists(os.path.join(d, m)) for m in ROOT_MARKERS):
            return d
        d = parent
    return start


def module_name(script):
    """'pkg.sub.mod' when the script is part of a package and must be run as `python -m` (it
    uses relative imports, or is a package's __main__.py); else None. Also returns the folder
    that holds the top package (what `python -m` puts first on sys.path)."""
    folder, base = os.path.split(os.path.realpath(script))
    if not os.path.isfile(os.path.join(folder, "__init__.py")):
        return None, None
    if base != "__main__.py":
        try:
            with open(script, encoding="utf-8", errors="replace") as f:
                if not re.search(r"^\s*from\s+\.", f.read(), re.M):
                    return None, None
        except OSError:
            return None, None
    parts = [] if base == "__main__.py" else [os.path.splitext(base)[0]]
    while os.path.isfile(os.path.join(folder, "__init__.py")):
        folder, pkg = os.path.split(folder)
        parts.insert(0, pkg)
    if not all(p.isidentifier() for p in parts):
        return None, None
    return ".".join(parts), folder


class Profiler:
    def __init__(self, script, out, interval_ms=5, memory=False, root=None):
        self.script = os.path.realpath(script)
        self.root = _norm(root or project_root(self.script))
        self.python_dirs = _python_dirs()
        self.venvs = {}        # folder -> is it a virtual environment (has pyvenv.cfg)?
        self._write_lock = threading.Lock()
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
        self.started_epoch = time.time()  # wall clock at t0: lines up with the visualizer's recording
        self.samples = []      # hardware samples {t, cpu_util, cpu_core_max, ram_util, disk_util, gpu_util...}
        self.timeline = []     # [t, key index]: the line that ran most in each BUCKET_S
        self.tl_keys = []      # key index -> (file, line)
        self._tl_index = {}
        self._bucket = (0.0, {})  # (start, {key: seconds}) of the bucket being filled
        self.hw_ready = threading.Event()  # set once the hardware thread has its sensors open
        self.gpu_mine = None   # did this program (or its children) open the GPU? None = NVML can't say
        self.thread_hw = {}    # (file, line) -> [{hw sums}, {hw seconds}] for lines seen in other threads
        self.own_threads = set()  # the profiler's own threads: never the user's code
        self.hw_missing = None    # why there are no hardware readings, if there aren't

    def _is_mine(self, filename):
        hit = self._mine.get(filename)
        if hit is None:
            p = _norm(filename) if filename and not filename.startswith("<") else ""
            hit = bool(p) and (p == self.root or p.startswith(self.root.rstrip(os.sep) + os.sep)) \
                and not any(s in p for s in SKIP_PARTS) \
                and not any(p.startswith(d) for d in self.python_dirs) \
                and p not in {_norm(os.path.join(HERE, f)) for f in OWN_FILES} \
                and not self._in_venv(p)
            self._mine[filename] = hit
        return hit

    def _in_venv(self, p):
        """Inside a virtual environment or a tool folder within the project (.venv, venv, .tox...)?"""
        rel = os.path.relpath(os.path.dirname(p), self.root)
        if rel == ".":
            return False
        d = self.root
        for part in rel.split(os.sep):
            if part in SKIP_DIRS:
                return True
            d = os.path.join(d, part)
            hit = self.venvs.get(d)
            if hit is None:
                hit = self.venvs[d] = os.path.isfile(os.path.join(d, "pyvenv.cfg")) or \
                    os.path.isfile(os.path.join(d, "conda-meta", "history"))
            if hit:
                return True
        return False

    # ---------------------------------------------------------------- sampling
    def _sample_loop(self):
        self.own_threads.add(threading.get_ident())
        last = time.perf_counter()
        while not self.done.is_set():
            time.sleep(self.interval)
            now = time.perf_counter()
            dt, last = now - last, now
            frames = sys._current_frames()
            hw = self.hw
            if hw:  # worker threads: what the hardware did while their lines ran (py-spy times them)
                for ident, fr in frames.items():
                    if ident == self.main_ident or ident in self.own_threads:
                        continue
                    while fr is not None and not self._is_mine(fr.f_code.co_filename):
                        fr = fr.f_back
                    if fr is None:
                        continue
                    rec = self.thread_hw.setdefault((fr.f_code.co_filename, fr.f_lineno),
                                                    [dict.fromkeys(LINE_HW, 0.0), dict.fromkeys(LINE_HW, 0.0)])
                    for k, src in LINE_HW.items():
                        v = hw.get(src)
                        if v is not None:
                            rec[0][k] += v * dt
                            rec[1][k] += dt
            frame = frames.get(self.main_ident)
            while frame is not None and not self._is_mine(frame.f_code.co_filename):
                frame = frame.f_back
            self.sampled += dt
            if frame is None:
                continue  # interpreter start-up / shutdown, outside the user's code
            key = (frame.f_code.co_filename, frame.f_lineno)
            rec = self.lines.get(key)
            if rec is None:
                rec = self.lines[key] = [0.0, frame.f_code.co_name, dict.fromkeys(LINE_HW, 0.0),
                                         dict.fromkeys(LINE_HW, 0.0)]
            rec[0] += dt
            hw = self.hw
            if hw:
                for k, src in LINE_HW.items():
                    v = hw.get(src)
                    if v is not None:
                        rec[2][k] += v * dt
                        rec[3][k] += dt
            self._to_timeline(now - self.t0, key, dt)

    def _to_timeline(self, t, key, dt):
        """Adds dt of `key` to the current bucket; a full bucket goes to the timeline as its top line."""
        start, counts = self._bucket
        if t - start >= BUCKET_S:
            if counts:
                top = max(counts, key=counts.get)
                idx = self._tl_index.get(top)
                if idx is None:
                    idx = self._tl_index[top] = len(self.tl_keys)
                    self.tl_keys.append(top)
                if len(self.timeline) < MAX_SAMPLES:
                    self.timeline.append([round(start, 2), idx])
            start, counts = start + BUCKET_S * int((t - start) / BUCKET_S), {}
            self._bucket = (start, counts)
        if key is not None:
            counts[key] = counts.get(key, 0.0) + dt

    def _hardware_loop(self):
        """Every HW_EVERY_S: what the machine was doing (the readings the visualizer's bottleneck
        finder uses), as self.hw for the line running now, and in self.samples."""
        self.own_threads.add(threading.get_ident())
        try:
            import psutil
        except ImportError:
            self.hw_missing = f"psutil isn't installed in this Python ({sys.executable}): pip install psutil"
            self.hw_ready.set()
            return
        gpu = None
        try:
            nvgpu = _private_module("nvgpu")
            if nvgpu.OK and nvgpu.names():
                gpu = nvgpu.Gpu(0)
                gpu.start()
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

        def disk_counters():
            try:
                return psutil.disk_io_counters()
            except Exception:
                return None

        psutil.cpu_percent(None, percpu=True)
        own_cpu()
        last_disk, last_t = disk_counters(), time.perf_counter()
        self.hw_ready.set()
        while not self.done.is_set():
            time.sleep(HW_EVERY_S)
            now = time.perf_counter()
            cores = psutil.cpu_percent(None, percpu=True)
            row = {"t": round(now - self.t0, 3), "proc": round(own_cpu(), 1),
                   "cpu_util": round(sum(cores) / len(cores), 1) if cores else None,
                   "cpu_core_max": max(cores) if cores else None}
            try:
                vm = psutil.virtual_memory()
                row["ram_util"], row["ram_used"] = vm.percent, round(vm.used / 2**30, 3)
            except Exception:
                pass
            disk = disk_counters()
            dt = now - last_t
            if disk is not None and last_disk is not None and dt > 0:
                row["disk_read"] = round((disk.read_bytes - last_disk.read_bytes) / dt / 1e6, 2)
                row["disk_write"] = round((disk.write_bytes - last_disk.write_bytes) / dt / 1e6, 2)
                if getattr(disk, "busy_time", None) is not None:  # Linux
                    busy_ms = disk.busy_time - last_disk.busy_time
                else:  # Windows: time spent reading + writing
                    busy_ms = (disk.read_time + disk.write_time) - (last_disk.read_time + last_disk.write_time)
                row["disk_util"] = round(max(0.0, min(100.0, busy_ms / 10.0 / dt)), 1)
            last_disk, last_t = disk, now
            if gpu is not None and len(self.samples) % 10 == 0 and not self.gpu_mine:
                self.gpu_mine = self._uses_gpu(gpu, nvgpu.pynvml, me, kids)
            if gpu is not None:
                try:
                    for k, v in gpu.sample().items():
                        if v is not None:
                            row[k] = round(v, 3) if isinstance(v, float) else v
                    if row.get("gpu_util") is not None:
                        self.hw_peak_gpu = max(self.hw_peak_gpu, row["gpu_util"])
                except Exception:
                    pass
            self.hw = row
            if len(self.samples) < MAX_SAMPLES:
                self.samples.append(row)
        if gpu is not None:
            gpu.stop()

    def _uses_gpu(self, gpu, pynvml, me, kids):
        """Is this program among the GPU's processes? (Other programs - the desktop, a browser - keep
        the GPU partly busy; that must not read as this program's GPU work.)"""
        pids = set()
        for fn in ("nvmlDeviceGetComputeRunningProcesses", "nvmlDeviceGetGraphicsRunningProcesses"):
            try:
                pids |= {p.pid for p in getattr(pynvml, fn)(gpu.handle)}
            except Exception:
                pass
        if not pids:
            return self.gpu_mine  # not supported here: undecided
        return bool(pids & ({me.pid} | set(kids)))

    def gpu_used(self):
        """This program used the GPU: NVML lists it, else (NVML can't list processes) its memory or
        load rose clearly above where it started."""
        if self.gpu_mine is not None:
            return self.gpu_mine
        g = [r["gpu_util"] for r in self.samples if num(r.get("gpu_util"))]
        v = [r["gpu_vram"] for r in self.samples if num(r.get("gpu_vram"))]
        base = sorted(g[:10])[len(g[:10]) // 2] if g else 0
        return bool((v and max(v) - v[0] >= 0.15) or (g and max(g) - base >= 40))

    def _writer_loop(self):
        self.own_threads.add(threading.get_ident())
        while not self.done.wait(1.0):
            with self._write_lock:
                if self.done.is_set():
                    break
                if self.memory:
                    self._memory_check()
                self._write(running=True)

    # ---------------------------------------------------------------- output
    def write(self, running):
        # the writer thread and the end of the run both write: one at a time, and never an old
        # "running" snapshot over the final file
        with self._write_lock:
            if running and self.done.is_set():
                return
            self._write(running)

    def _write(self, running):
        final = not running
        if not running and self.finalizing:
            running = True  # the parent (py-spy) writes the final, exact numbers next
        elapsed = time.perf_counter() - self.t0
        total = sum(r[0] for r in list(self.lines.values())) or 1e-9
        lines = []
        for (fname, line), (secs, func, hw, hw_t) in sorted(list(self.lines.items()), key=lambda kv: -kv[1][0]):
            item = {"file": os.path.realpath(fname), "line": line, "func": func, "seconds": round(secs, 4),
                    "share": round(secs / total, 5)}
            avg = {k: round(hw[k] / hw_t[k], 1) for k in hw if hw_t[k] > 0}
            if avg:
                item["hw"] = avg
            lines.append(item)
        data = {"version": 1, "script": self.script, "running": running, "elapsed_s": round(elapsed, 2),
                "profiled_s": round(total, 3), "interval_ms": self.interval * 1000, "cores": self.cores,
                "gpu_used": self.gpu_used(), "result": self.result, "exit_code": self.exit_code,
                "engine": "builtin", "crash": self.crash, "lines": lines[:500], "updated": time.time(),
                "started_epoch": self.started_epoch, "root": self.root, "hw_missing": self.hw_missing}
        if final:
            # the whole recording, and what it means: bottlenecks found in the hardware samples,
            # tied to the lines that were running while each one held
            self._to_timeline(elapsed + BUCKET_S, None, 0.0)
            data["timeline"] = self.timeline
            data["timeline_keys"] = [[os.path.realpath(f), ln] for f, ln in self.tl_keys]
            data["samples"] = self.samples
            data["thread_hw"] = [{"file": os.path.realpath(f), "line": ln,
                                  "hw": {k: round(sums[k] / secs[k], 1) for k in sums if secs[k] > 0}}
                                 for (f, ln), (sums, secs) in self.thread_hw.items() if any(secs.values())]
            try:
                link_findings(data, self.cores)
            except Exception as e:  # never lose the profile over the extras
                data["findings_error"] = f"{type(e).__name__}: {e}"
        if self.memory:
            if not running:
                self._memory_check()
            top = sorted(self.mem_peak.items(), key=lambda kv: -kv[1])[:50]
            data["memory"] = [{"file": os.path.realpath(f), "line": ln, "bytes": b} for (f, ln), b in top]
        make_out_dir(self.out)
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
        # sensors first: opened while the user's code runs, they compete with it for the GIL and a
        # short program can end before the first reading
        threading.Thread(target=self._hardware_loop, daemon=True).start()
        self.hw_ready.wait(3.0)
        for target in (self._sample_loop, self._writer_loop):
            threading.Thread(target=target, daemon=True).start()
        sys.argv = [self.script] + argv
        # like `python script.py`: the script's folder first on sys.path, in place of this one's;
        # a file of a package that needs it runs like `python -m pkg.mod`
        mod, top = module_name(self.script)
        sys.path[0] = top if mod else os.path.dirname(self.script)
        code = 0
        try:
            if mod:
                print(f"[hcv] {os.path.basename(self.script)} is part of a package: running it as "
                      f"python -m {mod}", file=sys.stderr, flush=True)
                runpy.run_module(mod, run_name="__main__", alter_sys=True)
            else:
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


# ---------------------------------------------------------------- linking hardware to lines
def _line_at(data):
    """t (seconds since the profile's start) -> (file, line) that was running, from the timeline."""
    import bisect
    tl = data.get("timeline") or []
    keys = data.get("timeline_keys") or []
    starts = [b[0] for b in tl]

    def at(t):
        i = bisect.bisect_right(starts, t) - 1
        if i < 0 or t - starts[i] > BUCKET_S * 1.5:
            return None
        f, ln = keys[tl[i][1]]
        return (f, ln)
    return at


def _states_by_line(data, samples, cores, t_offset=0.0):
    """{(file, line): {pattern: seconds}} - which bottleneck patterns held while each line ran.
    `samples` t are seconds since their own start; t_offset moves them onto the profile's clock."""
    analysis = _private_module("analysis")
    samples = [r for r in samples if isinstance(r.get("t"), (int, float))]
    if len(samples) < 4:
        return {}, analysis
    gpu_vals = [r["gpu_util"] for r in samples if isinstance(r.get("gpu_util"), (int, float))]
    vram = [r["gpu_vram"] for r in samples if isinstance(r.get("gpu_vram"), (int, float))]
    uses_gpu = bool(gpu_vals) and (max(gpu_vals) >= 30 or (len(vram) > 1 and max(vram) - vram[0] >= 0.1))
    at = _line_at(data)
    out = {}
    win = analysis._windowed(samples)
    for a, b in zip(win, win[1:]):
        key = at(a["t"] + t_offset)
        if key is None:
            continue
        for k in analysis._states(a, uses_gpu, cores):
            d = out.setdefault(key, {})
            d[k] = d.get(k, 0.0) + (b["t"] - a["t"])
    return out, analysis


def _attach(data, findings, by_line, field):
    """Puts per-line pattern shares on data['lines'][i][field] and the top lines on each finding."""
    secs = {(os.path.normcase(l["file"]), l["line"]): l for l in data.get("lines", [])}
    for (f, ln), states in by_line.items():
        item = secs.get((os.path.normcase(f), ln))
        if item is None:
            continue
        own = max(item["seconds"], sum(states.values()) / max(1, len(states)), 1e-9)
        item[field] = {k: round(min(1.0, v / own), 2) for k, v in states.items()}
    for fd in findings:
        tops = sorted(((v.get(fd["key"], 0.0), f, ln) for (f, ln), v in by_line.items()), reverse=True)
        fd["lines"] = [{"file": f, "line": ln, "seconds": round(v, 2)} for v, f, ln in tops[:8] if v > 0]


def link_findings(data, cores):
    """The visualizer's bottleneck finder on this run's own hardware samples; each finding gets
    the lines that ran while it held, each line the patterns that held while it ran."""
    samples = data.get("samples") or []
    if not data.get("gpu_used"):
        samples = _without_gpu(samples)
    by_line, analysis = _states_by_line(data, samples, cores)
    findings = analysis.analyze(samples, cores)
    _attach(data, findings, by_line, "states")
    data["findings"] = findings
    data["averages"] = _averages(samples)


def _averages(samples):
    keys = {}
    for r in samples:
        for k, v in r.items():
            if k != "t" and isinstance(v, (int, float)) and not isinstance(v, bool):
                keys.setdefault(k, []).append(v)
    return {k: round(sum(v) / len(v), 3) for k, v in keys.items()}


# sensors only the visualizer reads (LibreHardwareMonitor, Windows counters): shown per line
VIS_LINE_KEYS = ("cpu_temp", "cpu_power", "gpu_temp", "gpu_power", "board_temp", "disk_util", "disk_read",
                 "disk_write", "pcie_rx", "pcie_tx", "board_util", "ram_util", "cpu_freq")


def link_visualizer_run(profile_path, run, run_name=""):
    """Called by the visualizer when a YOUR CODE run of the profiler ends: its recording (more sensors:
    CPU temperature and power, disk, PCIe...) and its bottleneck findings are tied to the lines that
    were running, and saved into the profile so the editor shows them."""
    with open(profile_path, encoding="utf-8") as f:
        data = json.load(f)
    t_offset = (run.get("started_epoch") or 0) - (data.get("started_epoch") or 0)
    if not run.get("started_epoch") or not data.get("timeline"):
        return False
    samples = run.get("samples") or []
    cores = run.get("threads") or data.get("cores")
    findings = run.get("findings")
    if not data.get("gpu_used"):  # the profile knows this program never touched the GPU
        samples, findings = _without_gpu(samples), None
    by_line, analysis = _states_by_line(data, samples, cores, t_offset)
    if findings is None:
        findings = analysis.analyze(samples, cores)
    findings = [dict(fd) for fd in findings]
    _attach(data, findings, by_line, "vis_states")
    # per-line averages of the visualizer's sensors
    at = _line_at(data)
    sums = {}
    for r in samples:
        if not isinstance(r.get("t"), (int, float)):
            continue
        key = at(r["t"] + t_offset)
        if key is None:
            continue
        acc = sums.setdefault((os.path.normcase(key[0]), key[1]), {})
        for k in VIS_LINE_KEYS:
            v = r.get(k)
            if isinstance(v, (int, float)):
                s, n = acc.get(k, (0.0, 0))
                acc[k] = (s + v, n + 1)
    for item in data.get("lines", []):
        acc = sums.get((os.path.normcase(item["file"]), item["line"]))
        if acc:
            item["vis_hw"] = {k: round(s / n, 2) for k, (s, n) in acc.items()}
    data["visualizer"] = {"run": run_name, "machine": run.get("machine"), "when": run.get("when"),
                          "part_name": run.get("part_name"), "duration": run.get("duration"),
                          "result": run.get("result"), "averages": run.get("averages") or {},
                          "findings": findings, "t_offset": round(t_offset, 3)}
    tmp = profile_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, profile_path)
    return True


def profile_of_command(command):
    """The profile a visualizer YOUR CODE command writes, if it runs this profiler; else None."""
    m = re.search(r'profiler\.py"?\s.*?--out"?\s+"([^"]+)"', command or "") or \
        re.search(r'profiler\.py"?\s.*?--out\s+(\S+)', command or "")
    return m.group(1) if m else None


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


def make_out_dir(out):
    """The profile's folder; a .hcv folder gets a .gitignore so results never land in git."""
    folder = os.path.dirname(out)
    os.makedirs(folder, exist_ok=True)
    ignore = os.path.join(folder, ".gitignore")
    if os.path.basename(folder) == ".hcv" and not os.path.exists(ignore):
        try:
            with open(ignore, "w") as f:
                f.write("# HCV Hot Spots profiles\n*\n")
        except OSError:
            pass


def pyspy_lines(raw_path, script, root=None):
    """py-spy's collapsed stacks -> {(file, line): [seconds, func]} for the user's innermost frame
    of every sample (all threads and processes; waiting time included)."""
    judge = Profiler(script, "", 5, root=root)  # only for its _is_mine()
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
    child = [sys.executable, os.path.abspath(__file__), "--child", "--out", out, "--interval", str(a.interval),
             "--root", a.root]
    child += (["--memory"] if a.memory else []) + [a.script] + a.args
    cmd = [pyspy, "record", "--rate", str(PYSPY_RATE), "--idle", "--threads", "--subprocesses",
           "--full-filenames", "--format", "raw", "-o", raw, "--"] + child
    make_out_dir(out)
    try:
        rc = subprocess.call(cmd)
    except KeyboardInterrupt:  # Ctrl+C reaches the program too: it saves "stopped"; finish up below
        rc = 130
    live = None
    try:
        with open(out, encoding="utf-8") as f:
            live = json.load(f)
        exact, n_threads = pyspy_lines(raw, a.script, a.root)
    except (OSError, ValueError) as e:
        print(f"[hcv] py-spy result unusable ({e}); keeping the built-in numbers", file=sys.stderr)
        exact, n_threads = {}, 1
        if live is None:
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
        hw_line = {(os.path.normcase(l["file"]), l["line"]): l.get("hw")
                   for l in (live.get("thread_hw") or []) + live["lines"] if l.get("hw")}
        st_line = {(os.path.normcase(l["file"]), l["line"]): l.get("states") for l in live["lines"] if l.get("states")}
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
            st = st_line.get((os.path.normcase(fname), line))
            if st:
                item["states"] = st
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
    ap.add_argument("--root", help="top folder of your project: its files count as your code "
                    "(default: the nearest folder above the script with .git, pyproject.toml, ...)")
    ap.add_argument("--cwd", help="working folder for the script (default: the current one)")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("script")
    ap.add_argument("args", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    if not os.path.isfile(a.script):
        sys.exit(f"no such file: {a.script}")
    a.script = os.path.realpath(a.script)
    if a.out:
        a.out = os.path.realpath(a.out)
    if a.cwd:
        os.chdir(a.cwd)
    a.root = os.path.realpath(a.root) if a.root else project_root(a.script)
    out = os.path.realpath(a.out or os.path.join(os.path.dirname(a.script), ".hcv", "profile.json"))
    pyspy = find_pyspy() if a.engine != "builtin" and not a.child else None
    if a.engine == "py-spy" and not pyspy:
        sys.exit("py-spy isn't installed: pip install py-spy")
    if pyspy:
        print(f"[hcv] profiling {a.script} with py-spy -> {out}", file=sys.stderr, flush=True)
        sys.exit(run_with_pyspy(pyspy, a, out))
    prof = Profiler(a.script, out, a.interval, a.memory, root=a.root)
    prof.finalizing = a.child
    if not a.child:
        print(f"[hcv] profiling {a.script} -> {out} (built-in sampler; pip install py-spy for exact numbers)",
              file=sys.stderr, flush=True)
    sys.exit(prof.run(a.args))


if __name__ == "__main__":
    main()
