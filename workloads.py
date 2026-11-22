"""Real benchmark workloads. Each runs as its own process:

    python workloads.py <cpu2d|ram|gpu3d|mlgpu|mlcpu|pcie> <seconds>
    python workloads.py disk <seconds> <folder on the drive to test>
    python workloads.py usercode <seconds> <command line of the user's own program>

and prints one JSON object per ~0.25 s on stdout with the numbers it measured
itself (frame_ms, gflops, gbps, frames, io_mbps, sps, loss, pcie_gbps). On failure
prints {"error": "..."} and exits non-zero. The UI kills the process tree to stop it.

Runs on Windows (this PC) and on Linux (a remote machine, through remote_agent.py).
"""
import json
import multiprocessing as mp
import sys
import time

REPORT_EVERY = 0.25


def emit(**kw):
    print(json.dumps(kw), flush=True)


# ---------------------------------------------------------------- CPU 2D
W, H, MAXIT = 160, 120, 200


def _render_frame():
    """CPU-rendered 2D drawing: one Mandelbrot frame into a bytearray.
    Returns iterations done."""
    buf = bytearray(W * H)
    total = 0
    for py in range(H):
        ci = (py / H) * 2.4 - 1.2
        for px in range(W):
            cr = (px / W) * 3.5 - 2.5
            zr = zi = 0.0
            n = 0
            while n < MAXIT and zr * zr + zi * zi <= 4.0:
                zr, zi = zr * zr - zi * zi + cr, 2.0 * zr * zi + ci
                n += 1
            buf[py * W + px] = n & 0xFF
            total += n
    return total


def _cpu_worker(q, deadline):
    while time.monotonic() < deadline:
        t0 = time.perf_counter()
        its = _render_frame()
        q.put((time.perf_counter() - t0, its))


def run_cpu2d(seconds):
    import os
    q = mp.Queue()
    n = os.cpu_count() or 2
    deadline = time.monotonic() + seconds
    procs = [mp.Process(target=_cpu_worker, args=(q, deadline), daemon=True) for _ in range(n)]
    for p in procs:
        p.start()
    frames, its_acc, ft_acc = 0, 0, []
    t_rep = time.monotonic()
    while time.monotonic() < deadline:
        try:
            ft, its = q.get(timeout=0.1)
            frames += 1
            its_acc += its
            ft_acc.append(ft)
        except Exception:
            pass
        now = time.monotonic()
        if now - t_rep >= REPORT_EVERY and ft_acc:
            dt = now - t_rep
            # ~7 flops per Mandelbrot iteration (4 mul, 3 add/sub)
            emit(frames=len(ft_acc), frame_ms=1000 * sum(ft_acc) / len(ft_acc),
                 gflops=its_acc * 7 / dt / 1e9)
            ft_acc, its_acc, t_rep = [], 0, now
    for p in procs:
        p.terminate()


# ---------------------------------------------------------------- RAM
def run_ram(seconds):
    import threading
    size = 256 * 1024 * 1024
    nthreads = 4
    moved = [0] * nthreads
    stop = time.monotonic() + seconds

    def work(i):
        src = bytearray(size)
        dst = bytearray(size)
        mv_s, mv_d = memoryview(src), memoryview(dst)
        while time.monotonic() < stop:
            mv_d[:] = mv_s  # memcpy: reads size, writes size
            moved[i] += 2 * size

    ts = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(nthreads)]
    for t in ts:
        t.start()
    last, t_last = 0, time.monotonic()
    while time.monotonic() < stop:
        time.sleep(REPORT_EVERY)
        now, tot = time.monotonic(), sum(moved)
        emit(gbps=(tot - last) / (now - t_last) / 2**30)
        last, t_last = tot, now


# ---------------------------------------------------------------- GPU 3D
VERT = """
#version 330
in vec2 pos;
void main() { gl_Position = vec4(pos, 0.0, 1.0); }
"""
FRAG = """
#version 330
uniform float time;
uniform vec2 res;
out vec4 color;
float map(vec3 p) {
    p.xz = mod(p.xz + 2.0, 4.0) - 2.0;
    float s = length(p - vec3(0.0, 0.0, 0.0)) - 1.0;
    float b = length(max(abs(p - vec3(0.0, -1.2, 0.0)) - vec3(1.6, 0.1, 1.6), 0.0));
    float disp = 0.0, amp = 0.05, f = 8.0;
    for (int o = 0; o < 8; o++) {  // layered surface detail
        disp += amp * sin(f * p.x + time) * sin(f * p.y) * sin(f * p.z);
        amp *= 0.5; f *= 2.0;
    }
    return min(s + disp, b);
}
void main() {
    vec3 total = vec3(0.0);
    for (int a = 0; a < 4; a++) {  // 4x supersampling
        vec2 off = vec2(float(a & 1), float(a >> 1)) * 0.5 - 0.25;
        vec2 uv = (gl_FragCoord.xy + off - 0.5 * res) / res.y;
        // fly down the gap between two rows of spheres (x = 2), never inside one
        vec3 ro = vec3(2.0, 0.5, time * 1.5);
        vec3 rd = normalize(vec3(uv, 1.5));
        float t = 0.0;
        vec3 acc = vec3(0.0);
        for (int i = 0; i < 160; i++) {
            float d = map(ro + rd * t);
            acc += vec3(0.02, 0.03, 0.05) / (1.0 + d * d * 40.0);
            if (d < 0.001 || t > 40.0) break;
            t += d;
        }
        total += acc + 0.3 * vec3(1.0 / (1.0 + t));
    }
    color = vec4(total / 4.0, 1.0);
}
"""


def _gl_context():
    try:
        import moderngl
    except ImportError:
        raise RuntimeError("moderngl isn't installed on this machine (pip install moderngl)")
    try:
        return moderngl, moderngl.create_standalone_context(require=330)
    except Exception:
        if sys.platform == "win32":
            raise
        # a Linux machine with no desktop session: render through EGL instead of X11
        return moderngl, moderngl.create_standalone_context(require=330, backend="egl")


def run_gpu3d(seconds):
    moderngl, ctx = _gl_context()
    w, h = 1920, 1080
    fbo = ctx.simple_framebuffer((w, h))
    fbo.use()
    prog = ctx.program(vertex_shader=VERT, fragment_shader=FRAG)
    import array
    vbo = ctx.buffer(array.array("f", [-1, -1, 1, -1, -1, 1, 1, 1]).tobytes())
    vao = ctx.vertex_array(prog, [(vbo, "2f", "pos")])
    prog["res"].value = (w, h)
    stop = time.monotonic() + seconds
    t0 = time.monotonic()
    ft_acc, t_rep = [], time.monotonic()
    while time.monotonic() < stop:
        prog["time"].value = time.monotonic() - t0
        f0 = time.perf_counter()
        vao.render(moderngl.TRIANGLE_STRIP)
        ctx.finish()
        fbo.read(viewport=(0, 0, 1, 1))  # readback forces real GPU completion, not just submission
        ft_acc.append(time.perf_counter() - f0)
        now = time.monotonic()
        if now - t_rep >= REPORT_EVERY:
            emit(frames=len(ft_acc), frame_ms=1000 * sum(ft_acc) / len(ft_acc))
            ft_acc, t_rep = [], now


# ---------------------------------------------------------------- Disk I/O
DISK_FILE = 1 << 30   # 1 GiB test file
DISK_BLOCK = 8 << 20  # moved in 8 MiB blocks
DISK_READS_PER_WRITE = 3  # mostly reads, to keep SSD wear low


def run_disk(seconds, folder):
    """Sequential write then reads of a temp file in `folder`, bypassing the
    file cache so the drive itself does the work. The file can't outlive this
    process: on Windows it is opened delete-on-close, on Linux it is unlinked
    as soon as it is open."""
    import shutil

    if shutil.disk_usage(folder).free < 2 * DISK_FILE:
        raise RuntimeError(f"not enough free space in {folder} (needs 2 GB)")
    if sys.platform == "win32":
        _disk_windows(seconds, folder)
    else:
        _disk_linux(seconds, folder)


def _disk_windows(seconds, folder):
    import ctypes
    import mmap
    import os
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    io_args = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
               ctypes.c_void_p]
    k32.WriteFile.argtypes = k32.ReadFile.argtypes = io_args
    k32.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    path = os.path.join(folder, f"hcv_disk_test_{os.getpid()}.tmp")
    # GENERIC_READ|GENERIC_WRITE, CREATE_ALWAYS,
    # NO_BUFFERING | WRITE_THROUGH | DELETE_ON_CLOSE | SEQUENTIAL_SCAN
    handle = k32.CreateFileW(path, 0xC0000000, 0, None, 2,
                             0x20000000 | 0x80000000 | 0x04000000 | 0x08000000, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(f"can't create the test file in {folder}: {ctypes.WinError(ctypes.get_last_error())}")

    mem = mmap.mmap(-1, DISK_BLOCK)       # page-aligned, as unbuffered I/O requires
    mem[:] = os.urandom(DISK_BLOCK)       # incompressible data
    buf = (ctypes.c_char * DISK_BLOCK).from_buffer(mem)
    done = wintypes.DWORD()
    stop = time.monotonic() + seconds
    moved, t_rep, phase = 0, time.monotonic(), 0
    try:
        while time.monotonic() < stop:
            writing = phase % (DISK_READS_PER_WRITE + 1) == 0
            op = k32.WriteFile if writing else k32.ReadFile
            k32.SetFilePointerEx(handle, 0, None, 0)
            for _ in range(DISK_FILE // DISK_BLOCK):
                if not op(handle, buf, DISK_BLOCK, ctypes.byref(done), None) or done.value != DISK_BLOCK:
                    raise OSError(f"disk {'write' if writing else 'read'} failed: "
                                  f"{ctypes.WinError(ctypes.get_last_error())}")
                moved += DISK_BLOCK
                now = time.monotonic()
                if now - t_rep >= REPORT_EVERY:
                    emit(io_mbps=moved / (now - t_rep) / 1e6)
                    moved, t_rep = 0, now
                if now >= stop:
                    break
            phase += 1
    finally:
        del buf
        mem.close()
        k32.CloseHandle(handle)  # deletes the file


def _disk_linux(seconds, folder):
    import mmap
    import os

    path = os.path.join(folder, f".hcv_disk_test_{os.getpid()}.tmp")
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_DIRECT, 0o600)
    except OSError as e:
        raise OSError(f"can't create the test file in {folder}: {e.strerror}"
                      + (" (this filesystem doesn't allow direct I/O)" if e.errno == 22 else ""))
    os.unlink(path)  # nothing is left in the folder; the space is freed when fd closes
    mem = mmap.mmap(-1, DISK_BLOCK)  # page-aligned, as O_DIRECT requires
    mem[:] = os.urandom(DISK_BLOCK)
    stop = time.monotonic() + seconds
    moved, t_rep, phase = 0, time.monotonic(), 0
    try:
        while time.monotonic() < stop:
            writing = phase % (DISK_READS_PER_WRITE + 1) == 0
            for n in range(DISK_FILE // DISK_BLOCK):
                off = n * DISK_BLOCK
                done = os.pwrite(fd, mem, off) if writing else os.preadv(fd, [mem], off)
                if done != DISK_BLOCK:
                    raise OSError(f"disk {'write' if writing else 'read'} stopped short")
                moved += DISK_BLOCK
                now = time.monotonic()
                if now - t_rep >= REPORT_EVERY:
                    emit(io_mbps=moved / (now - t_rep) / 1e6)
                    moved, t_rep = 0, now
                if now >= stop:
                    break
            if writing:
                os.fsync(fd)
            phase += 1
    finally:
        mem.close()
        os.close(fd)


# ---------------------------------------------------------------- PCIe bus (motherboard)
PCIE_BYTES = 256 << 20  # moved per direction per round


def run_pcie(seconds):
    """Moves data across the motherboard's PCIe link: system RAM -> GPU, then
    GPU -> system RAM, over and over. Uses PyTorch (pinned memory, CUDA) when it
    can see the GPU, otherwise OpenGL buffer uploads/readbacks (any GPU)."""
    try:
        import torch
        cuda = torch.cuda.is_available()
    except ImportError:
        cuda = False
    if cuda:
        host = torch.empty(PCIE_BYTES, dtype=torch.uint8).pin_memory()
        dev = torch.empty(PCIE_BYTES, dtype=torch.uint8, device="cuda")

        def round_trip():
            dev.copy_(host)
            host.copy_(dev)
            torch.cuda.synchronize()
    else:
        import os
        try:
            _, ctx = _gl_context()
        except RuntimeError:
            raise RuntimeError("needs PyTorch with CUDA, or moderngl, on this machine")
        data = os.urandom(PCIE_BYTES)
        buf = ctx.buffer(reserve=PCIE_BYTES)

        def round_trip():
            buf.write(data)
            ctx.finish()
            buf.read()  # blocks until the bytes are back in system RAM
    stop = time.monotonic() + seconds
    moved, t_rep = 0, time.monotonic()
    while time.monotonic() < stop:
        round_trip()
        moved += 2 * PCIE_BYTES
        now = time.monotonic()
        if now - t_rep >= REPORT_EVERY:
            emit(pcie_gbps=moved / (now - t_rep) / 1e9)
            moved, t_rep = 0, now


# ---------------------------------------------------------------- ML training
def run_ml(seconds, device):
    """Really trains a small CNN (PyTorch) on synthetic images on `device`
    ("cuda" or "cpu") and reports training speed."""
    try:
        import torch
        from torch import nn
    except ImportError:
        raise RuntimeError("PyTorch isn't installed (pip install torch)")
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("PyTorch can't see a CUDA GPU")
    dev = torch.device(device)
    torch.manual_seed(0)
    n, batch = (16384, 512) if device == "cuda" else (4096, 128)
    x = torch.randn(n, 3, 32, 32, device=dev)
    # labels follow a fixed hidden rule, so the loss really falls as it learns
    y = (x.mean(dim=(2, 3)) @ torch.randn(3, 10, device=dev)).argmax(dim=1)
    width = 128 if device == "cuda" else 32
    model = nn.Sequential(
        nn.Conv2d(3, width, 3, padding=1), nn.ReLU(),
        nn.Conv2d(width, width, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(width, 2 * width, 3, padding=1), nn.ReLU(),
        nn.Conv2d(2 * width, 2 * width, 3, padding=1), nn.ReLU(), nn.AdaptiveAvgPool2d(1),
        nn.Flatten(), nn.Linear(2 * width, 10),
    ).to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.CrossEntropyLoss()

    # count the floating-point work of one real training step (forward + backward of the
    # convolutions and matmuls; elementwise ops are left out, so it slightly undercounts)
    flops_per_sample = None
    try:
        from torch.utils.flop_counter import FlopCounterMode
        counter = FlopCounterMode(display=False)
        with counter:
            loss_fn(model(x[:batch]), y[:batch]).backward()
        model.zero_grad(set_to_none=True)
        flops_per_sample = counter.get_total_flops() / batch
    except Exception:
        pass

    stop = time.monotonic() + seconds
    seen, t_rep, i = 0, time.monotonic(), 0
    while time.monotonic() < stop:
        xb, yb = x[i:i + batch], y[i:i + batch]
        i = (i + batch) % n
        opt.zero_grad(set_to_none=True)
        loss = loss_fn(model(xb), yb)
        loss.backward()
        opt.step()
        loss_v = loss.item()  # also waits for the GPU to really finish the step
        seen += len(xb)
        now = time.monotonic()
        if now - t_rep >= REPORT_EVERY:
            sps = seen / (now - t_rep)
            if flops_per_sample:
                emit(sps=sps, loss=loss_v, gflops=sps * flops_per_sample / 1e9)
            else:
                emit(sps=sps, loss=loss_v)
            seen, t_rep = 0, now


# ---------------------------------------------------------------- the user's own program
def run_usercode(seconds, command):
    """Runs the user's own program (SRS ultimate goal: watch how real code uses the
    hardware). Its output lines are passed on as {"ev": "out"}; when it ends,
    {"ev": "exit"} says how. It is stopped at the time limit."""
    import os
    import re
    import signal
    import subprocess
    import threading

    win = sys.platform == "win32"
    cwd = os.path.expanduser("~")
    # the app works out the folder and the Python (the project's venv) and hands them over
    want_cwd = os.environ.get("HCV_CWD") or ""
    python = os.environ.get("HCV_PYTHON") or sys.executable
    if win:
        m = re.match(r'\s*(?:"([^"]+)"|(\S+))(.*)$', command)
        path = (m.group(1) or m.group(2)) if m else ""
        if os.path.isfile(path):
            cwd = os.path.dirname(os.path.abspath(path))
            if path.lower().endswith((".py", ".pyw")):
                command = f'"{python}" -u "{path}"{m.group(3)}'
        popen = dict(args=command, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        popen = dict(args=["sh", "-c", command], start_new_session=True)
    if want_cwd and os.path.isdir(want_cwd):
        cwd = want_cwd
    # read the output as UTF-8 and ask Python programs to write UTF-8: with the Windows default
    # (cp1252) on one side only, "·" or "é" arrived as "Â·" / "Ã©"
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    try:
        proc = subprocess.Popen(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, cwd=cwd, env=env, **popen)
    except OSError as e:
        raise RuntimeError(f"can't start {command}: {e.strerror or e}")  # not repr: Windows paths would show \\

    def kill():
        if proc.poll() is not None:
            return
        if win:  # the program and everything it started
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass

    if not win:  # the remote agent stops a test with SIGTERM: take the program down too
        signal.signal(signal.SIGTERM, lambda *a: (kill(), os._exit(0)))

    def pump():
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                emit(ev="out", text=line[:400])
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    t0 = time.monotonic()
    while proc.poll() is None:
        if time.monotonic() - t0 >= seconds:
            kill()
            proc.wait()
            reader.join(2)
            emit(ev="exit", text=f"stopped at the {seconds:.0f} s time limit")
            return
        emit(run_s=time.monotonic() - t0)  # heartbeat: the program is running
        time.sleep(REPORT_EVERY)
    reader.join(2)
    emit(run_s=time.monotonic() - t0)
    emit(ev="exit", text=f"program exited with code {proc.returncode}")


RUNNERS = {"cpu2d": run_cpu2d, "ram": run_ram, "gpu3d": run_gpu3d, "disk": run_disk, "pcie": run_pcie,
           "mlgpu": lambda s: run_ml(s, "cuda"), "mlcpu": lambda s: run_ml(s, "cpu"),
           "usercode": run_usercode}

if __name__ == "__main__":
    mp.freeze_support()
    name, secs = sys.argv[1], float(sys.argv[2])
    try:
        RUNNERS[name](secs, *sys.argv[3:])
    except Exception as e:
        emit(error=f"{type(e).__name__}: {e}")
        sys.exit(2)
