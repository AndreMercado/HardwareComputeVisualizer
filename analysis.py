"""Bottleneck finder (SRS appendix VI.3, the "ultimate goal").

Reads the samples of a recorded run and says where the code made poor use of
the hardware, and where the hardware itself was the limit: one core maxed while
the others idle (serial code), the GPU waiting on the CPU, the disk or the PCIe
bus saturated, RAM full, or nothing busy at all (waiting).

Only recorded samples are used (FR-11). Readings are averaged over a short
window first: a single 50 ms sample of one core is too coarse to judge from.
"""

WINDOW_S = 1.0     # judge each moment by the average of the last second
MIN_SHARE = 0.10   # report a pattern only if it held for at least 10% of the run
MIN_SPAN_S = 1.0   # ...and list only the stretches that lasted at least a second

# key: (title, is it a problem?, what to do about it)
PATTERNS = {
    "serial": ("One CPU core maxed while the others sat idle - serial code", True,
               "Spread the work over the cores: multiprocessing, vectorised numpy / native "
               "libraries, or threads in code that releases the GIL."),
    "gpu_starved": ("GPU waiting on the CPU - it was idle while the CPU was busy", True,
                    "Feed the GPU faster: bigger batches, load data in worker processes, keep data "
                    "on the GPU, avoid per-step syncs such as .item() or printing tensors."),
    "pcie": ("CPU <-> GPU transfers kept the PCIe bus busy", True,
             "Copy data to the GPU once and keep it there; use pinned memory and fewer, bigger copies."),
    "disk": ("The disk was busy all the time - waiting on storage", True,
             "Read less or cache it in RAM, read in big sequential blocks, or use a faster drive."),
    "ram": ("RAM almost full - the system may be swapping to disk", True,
            "Use less memory at once (stream or chunk the data, smaller batches) or add RAM."),
    "idle": ("Nothing was busy - the code was waiting (sleep, network, locks, input)", True,
             "Look for waits: network calls, sleeps, lock contention, or work done one request at a time."),
    "gpu_bound": ("GPU fully busy - the GPU is the limit (good use)", False,
                  "Faster only with a faster GPU or less GPU work (lower precision, smaller model)."),
    "cpu_bound": ("All CPU cores busy - the CPU is the limit (good, parallel use)", False,
                  "Faster only with a faster CPU or less work (better algorithm, moving work to the GPU)."),
}


# readings of the GPU and its PCIe link: what other programs do there must not count for a run that
# doesn't use the GPU (otherwise another app's GPU burst reads as "GPU waiting on the CPU")
GPU_KEYS = ("gpu_util", "gpu_memctl", "gpu_vram", "board_util", "pcie_rx", "pcie_tx")


def without_gpu(samples):
    """The samples with the GPU / PCIe readings removed."""
    return [{k: v for k, v in r.items() if k not in GPU_KEYS} for r in samples]


def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else None


def _windowed(samples):
    """Each sample replaced by the average of the readings in the WINDOW_S before it."""
    keys = ("cpu_util", "cpu_core_max", "gpu_util", "disk_util", "board_util", "ram_util")
    out, start = [], 0
    for i, s in enumerate(samples):
        while samples[start]["t"] < s["t"] - WINDOW_S:
            start += 1
        win = samples[start:i + 1]
        w = {"t": s["t"]}
        for k in keys:
            w[k] = _avg(x.get(k) for x in win)
        out.append(w)
    return out


def _states(w, uses_gpu, cores):
    """Which patterns hold at this moment."""
    cpu, core = w["cpu_util"], w["cpu_core_max"]
    gpu, disk, bus, ram = w["gpu_util"], w["disk_util"], w["board_util"], w["ram_util"]
    found = set()
    # one core (nearly) maxed while the CPU as a whole does only a core or two of work - the
    # second core allows for the OS and this app's own drawing. The OS moves a busy thread
    # between cores, so the busiest core reads ~70-90 %, not 100 %, over a second.
    # Not when the GPU is maxed: then that core is the thread feeding (and waiting on) the GPU.
    limit = max(1.6, min(3.0, 0.2 * cores))  # cores' worth of work still counted as "one thread"
    one_core = (cpu is not None and core is not None and cores > 2 and core >= 70
                and cpu / 100.0 * cores <= limit)
    if one_core and not (gpu is not None and gpu >= 90):
        found.add("serial")
    if cpu is not None and cpu >= 85:
        found.add("cpu_bound")
    if gpu is not None and gpu >= 90:
        found.add("gpu_bound")
    if (uses_gpu and gpu is not None and gpu < 40 and
            ((core is not None and core >= 80) or (cpu is not None and cpu >= 80))):
        found.add("gpu_starved")
    if bus is not None and bus >= 60:
        found.add("pcie")
    if disk is not None and disk >= 90:
        found.add("disk")
    if ram is not None and ram >= 92:
        found.add("ram")
    busy = [v for v in (core, gpu, disk) if v is not None]
    if busy and max(busy) < 20 and (cpu is None or cpu < 15):
        found.add("idle")
    return found


def analyze(samples, cores=None):
    """[{'key', 'title', 'problem', 'advice', 'share', 'seconds', 'spans': [(t0, t1)]}],
    most time first. `samples` are the rows of a recorded run (each has 't' in seconds)."""
    samples = [s for s in samples if isinstance(s.get("t"), (int, float))]
    if len(samples) < 4:
        return []
    if not cores:
        cores = max((len(s["cpu_cores"]) for s in samples if isinstance(s.get("cpu_cores"), list)), default=0)
    gpu_vals = [s["gpu_util"] for s in samples if isinstance(s.get("gpu_util"), (int, float))]
    vram = [s["gpu_vram"] for s in samples if isinstance(s.get("gpu_vram"), (int, float))]
    # the run used the GPU: it got busy at some point, or the code put data on it
    uses_gpu = bool(gpu_vals) and (max(gpu_vals) >= 30 or (len(vram) > 1 and max(vram) - vram[0] >= 0.1))
    win = _windowed(samples)
    total = samples[-1]["t"] - samples[0]["t"]
    if total <= 0:
        return []
    time_in, spans, open_at = {}, {}, {}
    for a, b in zip(win, win[1:]):
        dt = b["t"] - a["t"]
        now = _states(a, uses_gpu, cores)
        for k in now:
            time_in[k] = time_in.get(k, 0.0) + dt
            open_at.setdefault(k, a["t"])
        for k in list(open_at):
            if k not in now:
                spans.setdefault(k, []).append((open_at.pop(k), a["t"]))
    for k, t0 in open_at.items():
        spans.setdefault(k, []).append((t0, win[-1]["t"]))
    out = []
    for k, secs in time_in.items():
        share = secs / total
        if share < MIN_SHARE:
            continue
        title, problem, advice = PATTERNS[k]
        out.append({"key": k, "title": title, "problem": problem, "advice": advice,
                    "share": share, "seconds": secs,
                    "spans": [(round(a, 1), round(b, 1)) for a, b in spans.get(k, []) if b - a >= MIN_SPAN_S]})
    out.sort(key=lambda f: (not f["problem"], -f["share"]))
    return out


def spans_text(spans, limit=3):
    if not spans:
        return ""
    parts = [f"{a:.0f}-{b:.0f} s" for a, b in sorted(spans, key=lambda s: s[0] - s[1])[:limit]]
    more = len(spans) - limit
    return ", ".join(parts) + (f" (+{more} more)" if more > 0 else "")


def report(findings):
    """Plain-text lines for the run summary."""
    if not findings:
        return ["Bottlenecks: none found - no single pattern held for 10% of the run."]
    lines = ["Bottlenecks (share of the run, longest stretches):"]
    for f in findings:
        mark = "!" if f["problem"] else "ok"
        where = spans_text(f["spans"])
        lines.append(f"  {mark:>2} {100 * f['share']:3.0f}%  {f['title']}" + (f"  [{where}]" if where else ""))
        lines.append(f"          -> {f['advice']}")
    return lines
