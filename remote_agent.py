"""Remote agent (SRS UC-2). remote.py copies this file, workloads.py, nvgpu.py and
pynvml.py into a fresh temp folder on a Linux machine and starts it over SSH.

It reads the machine's hardware (read-only: /proc, /sys, NVML), runs the
workloads, and talks to the app with one JSON object per line:

  app -> agent (stdin):   {"cmd": "watch", "gpu": 0, "disk": "nvme0n1"}   start sending telemetry
                          {"cmd": "unwatch"}
                          {"cmd": "start", "script": "cpu2d", "seconds": 30}
                          {"cmd": "stop"}    {"cmd": "ping"}    {"cmd": "quit"}
  agent -> app (stdout):  {"hello": {hardware}}    {"tel": {sample}, "info": {...}}
                          {"sources": {...}}       {"wl": "<workload output line>"}
                          {"ended": exit code}     {"error": "..."}

Safety (FR-22, FR-23): with no message from the app for 10 s it stops any
workload and exits; on exit it deletes its own folder. Standard library only,
Python 3.8+.
"""
import glob
import json
import os
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import nvgpu  # noqa: E402

WATCHDOG_S = 10
POLL_HZ = 20
_out = threading.Lock()
_state = {"contact": time.monotonic(), "proc": None, "sampler": None, "gpu": 0, "closing": False}


def send(**kw):
    line = json.dumps(kw) + "\n"
    with _out:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except (OSError, ValueError):
            threading.Thread(target=shutdown, daemon=True).start()  # the app is gone


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- hardware
def cpu_name():
    model = None
    for line in (read("/proc/cpuinfo", "") or "").splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    cores = len({(read(p + "/topology/physical_package_id"), read(p + "/topology/core_id"))
                 for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*")}) or None
    threads = os.cpu_count()
    return f"{model or 'CPU'} ({cores}C/{threads}T)" if cores else f"{model or 'CPU'} ({threads}T)"


def disk_of(path):
    """Name of the whole disk (e.g. nvme0n1) that holds `path`, or None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    sysdev = os.path.realpath(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
    name = os.path.basename(sysdev)
    if name.startswith("dm-"):  # LVM / LUKS: follow to the partition underneath
        slaves = glob.glob(f"/sys/block/{name}/slaves/*")
        if not slaves:
            return None
        sysdev = os.path.realpath(slaves[0])
    parent = os.path.basename(os.path.dirname(sysdev))
    return parent if os.path.exists(f"/sys/block/{parent}") else os.path.basename(sysdev)


def mount_points():
    out = []
    for line in (read("/proc/mounts", "") or "").splitlines():
        parts = line.split()
        if len(parts) > 2 and parts[0].startswith("/dev/"):
            out.append(parts[1].replace("\\040", " "))
    return out


def disks():
    found = []
    mounts = mount_points()
    for dev in sorted(os.listdir("/sys/block")):
        if dev.startswith(("loop", "ram", "zram", "dm-", "sr", "md")):
            continue
        size = int(read(f"/sys/block/{dev}/size", "0") or 0) * 512
        if not size:
            continue
        model = read(f"/sys/block/{dev}/device/model") or dev
        kind = "HDD" if read(f"/sys/block/{dev}/queue/rotational") == "1" else "SSD"
        where = [m for m in mounts if disk_of(m) == dev]
        size_s = f"{size / 1e12:.0f} TB" if size >= 950e9 else f"{size / 1e9:.0f} GB"
        at = ", ".join(where[:3]) if where else "not mounted"
        found.append({"id": dev, "name": model, "letters": [], "mounts": where,
                      "label": f"{model} ({dev}: {at}) - {size_s} {kind}"})
    # the system disk first, then other mounted disks: the first entry is the default choice
    found.sort(key=lambda d: (0 if "/" in d["mounts"] else 1 if d["mounts"] else 2, d["id"]))
    return found


def hwmon_temp(want_names, want_labels):
    """Path of a temperature input, from the first hwmon driver in want_names whose label
    is in want_labels (in that order of preference), or None."""
    mons = {}
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        mons.setdefault(read(h + "/name", ""), []).append(h)
    for name in want_names:
        for h in mons.get(name, []):
            labels = {read(p, ""): p[:-len("label")] + "input" for p in glob.glob(h + "/temp*_label")}
            for want in want_labels:
                for label, path in labels.items():
                    if want.lower() in label.lower() and read(path):
                        return path
            if not want_labels and read(h + "/temp1_input"):
                return h + "/temp1_input"
    return None


def cpu_temp_path():
    return (hwmon_temp(("k10temp", "zenpower"), ("Tdie", "Tctl"))
            or hwmon_temp(("coretemp",), ("Package",))
            or hwmon_temp(("k10temp", "zenpower", "coretemp", "cpu_thermal"), ()))


def board_temp_path():
    names = [read(h + "/name", "") for h in glob.glob("/sys/class/hwmon/hwmon*")]
    superio = [n for n in names if n.startswith(("nct", "it8", "f71", "w83"))]
    return hwmon_temp(superio, ("PCH", "Chipset")) if superio else None


def rapl_path():
    for p in ("/sys/class/powercap/intel-rapl:0/energy_uj", "/sys/class/powercap/amd-rapl:0/energy_uj"):
        if read(p) is not None:  # usually root-only since 2020 (PLATYPUS)
            return p
    return None


def hardware():
    board = " ".join(x for x in (read("/sys/class/dmi/id/board_vendor"),
                                 read("/sys/class/dmi/id/board_name")) if x) or None
    total = 0
    for line in (read("/proc/meminfo", "") or "").splitlines():
        if line.startswith("MemTotal:"):
            total = int(line.split()[1]) * 1024
    gpus = nvgpu.names()
    return {"machine": socket.gethostname(), "cpu": cpu_name(), "threads": os.cpu_count() or 1,
            "board": board,
            "ram": f"{total / 2**30:.0f} GB RAM" if total else None,
            "gpus": gpus, "gpu_info": [{"kind": "nvml", "index": i} for i in range(len(gpus))],
            "disks": disks(), "cpu_temp": cpu_temp_path() is not None, "cpu_power": rapl_path() is not None,
            "python": "%d.%d" % sys.version_info[:2], "nvml": nvgpu.OK, "nvml_error": nvgpu.ERROR}


# ---------------------------------------------------------------- telemetry
class Sampler:
    def __init__(self, gpu_index, disk):
        self.disk = disk
        self.stop_ev = threading.Event()
        self.gpu = None
        if nvgpu.OK and gpu_index is not None:
            try:
                self.gpu = nvgpu.Gpu(gpu_index)
                self.gpu.start()
            except Exception:
                self.gpu = None
        self.temp, self.board_temp, self.rapl = cpu_temp_path(), board_temp_path(), rapl_path()
        self.last_cpu = self.last_disk = self.last_rapl = None
        self.last_cores = {}
        self.sources = {
            "cpu_util": "/proc/stat", "cpu_core_max": "/proc/stat", "cpu_freq": "cpufreq / cpuinfo", "ram_util": "/proc/meminfo",
            "ram_used": "/proc/meminfo", "disk_util": "/proc/diskstats", "disk_read": "/proc/diskstats",
            "disk_write": "/proc/diskstats",
            "cpu_temp": "hwmon " + read(os.path.dirname(self.temp) + "/name", "") if self.temp
            else "not exposed (VM?)",
            "cpu_power": "RAPL (powercap)" if self.rapl else "RAPL needs root here",
            "board_temp": "hwmon" if self.board_temp else "not exposed",
        }
        if not self.gpu:
            for k in ("pcie_rx", "pcie_tx"):
                self.sources[k] = "needs NVIDIA (NVML)"

    def sample(self):
        now = time.monotonic()
        s = {"cpu_util": None, "cpu_core_max": None, "cpu_cores": None, "cpu_freq": None, "cpu_temp": None, "cpu_power": None,
             "ram_util": None, "ram_used": None, "board_temp": None,
             "disk_util": None, "disk_read": None, "disk_write": None}
        cores = []
        for line in (read("/proc/stat", "") or "").splitlines():
            f = line.split()
            if not f or not f[0].startswith("cpu"):
                continue
            v = [int(x) for x in f[1:9]]
            total, idle = sum(v), v[3] + v[4]
            last = self.last_cpu if f[0] == "cpu" else self.last_cores.get(f[0])
            busy = (100.0 * (1 - (idle - last[1]) / (total - last[0]))
                    if last and total > last[0] else None)
            if f[0] == "cpu":
                s["cpu_util"], self.last_cpu = busy, (total, idle)
            else:
                self.last_cores[f[0]] = (total, idle)
                if busy is not None:
                    cores.append(max(0.0, busy))
        if cores:
            s["cpu_cores"] = [round(c) for c in cores]
            s["cpu_core_max"] = max(cores)
        mhz = [int(x) / 1000 for x in (read(p) for p in glob.glob(
            "/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")) if x and x.isdigit()]
        if not mhz:  # no cpufreq driver (some VMs): the kernel's own figure
            mhz = [float(l.split(":", 1)[1]) for l in (read("/proc/cpuinfo", "") or "").splitlines()
                   if l.startswith("cpu MHz")]
        s["cpu_freq"] = sum(mhz) / len(mhz) if mhz else None
        mem = {}
        for line in (read("/proc/meminfo", "") or "").splitlines():
            k, _, rest = line.partition(":")
            if k in ("MemTotal", "MemAvailable"):
                mem[k] = int(rest.split()[0]) * 1024
        if len(mem) == 2:
            used = mem["MemTotal"] - mem["MemAvailable"]
            s["ram_util"], s["ram_used"] = 100.0 * used / mem["MemTotal"], used / 2**30
        for key, path in (("cpu_temp", self.temp), ("board_temp", self.board_temp)):
            raw = read(path) if path else None
            s[key] = int(raw) / 1000 if raw and raw.lstrip("-").isdigit() else None
        if self.rapl:
            raw = read(self.rapl)
            if raw and raw.isdigit():
                e = int(raw)
                if self.last_rapl and e >= self.last_rapl[1] and now > self.last_rapl[0]:
                    s["cpu_power"] = (e - self.last_rapl[1]) / 1e6 / (now - self.last_rapl[0])
                self.last_rapl = (now, e)
        if self.disk:
            for line in (read("/proc/diskstats", "") or "").splitlines():
                p = line.split()
                if len(p) > 12 and p[2] == self.disk:
                    cur = (now, int(p[5]) * 512, int(p[9]) * 512, int(p[12]))
                    last, self.last_disk = self.last_disk, cur
                    if last and now > last[0]:
                        dt = now - last[0]
                        s["disk_read"] = max(0, cur[1] - last[1]) / dt / 1e6
                        s["disk_write"] = max(0, cur[2] - last[2]) / dt / 1e6
                        s["disk_util"] = min(100.0, max(0, cur[3] - last[3]) / (dt * 1000) * 100)
                    break
        s.update(dict.fromkeys(nvgpu.KEYS))
        info = {}
        if self.gpu:
            s.update(self.gpu.sample())
            if self.gpu.link_text():
                info["pcie_link"] = self.gpu.link_text()
        return s, info

    def run(self):
        send(sources=self.sources)
        period = 1.0 / POLL_HZ
        while not self.stop_ev.is_set():
            t0 = time.monotonic()
            try:
                s, info = self.sample()
                send(tel=s, info=info)
            except Exception as e:
                send(error=f"telemetry: {type(e).__name__}: {e}")
            time.sleep(max(0.0, period - (time.monotonic() - t0)))

    def stop(self):
        self.stop_ev.set()
        if self.gpu:
            self.gpu.stop()


# ---------------------------------------------------------------- workloads
def disk_folder(disk):
    """A writable folder on `disk` for the I/O test file, or None."""
    cands = [os.path.expanduser("~"), tempfile.gettempdir()] + mount_points()
    for c in cands:
        if disk_of(c) == disk and os.access(c, os.W_OK):
            return c
    return None


def start_workload(script, seconds, disk=None):
    stop_workload()
    cmd = [sys.executable, "-u", os.path.join(HERE, "workloads.py"), script, str(seconds)]
    if script == "usercode":
        cmd.append(disk)  # for this test the extra argument is the command line
    if script == "disk":
        folder = disk_folder(disk)
        if folder is None:
            send(wl=json.dumps({"error": f"{disk} has no mounted folder you can write to"}))
            send(ended=2)
            return
        cmd.append(folder)
    env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=str(_state["gpu"]))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                            text=True, env=env, start_new_session=True)
    _state["proc"] = proc

    def pump():
        for line in proc.stdout:
            if line.strip():
                send(wl=line.strip())
        send(ended=proc.wait())
    threading.Thread(target=pump, daemon=True).start()


def stop_workload():
    proc, _state["proc"] = _state["proc"], None
    if proc is None or proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(proc.pid, sig)  # the workload and every process it started
        except OSError:
            return
        try:
            proc.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def unwatch():
    s, _state["sampler"] = _state["sampler"], None
    if s:
        s.stop()


def shutdown():
    if _state["closing"]:
        return
    _state["closing"] = True
    stop_workload()
    unwatch()
    if os.path.basename(HERE).startswith("hcv-agent-"):  # FR-23: leave nothing behind
        shutil.rmtree(HERE, ignore_errors=True)
    os._exit(0)


def watchdog():
    while True:
        time.sleep(1.0)
        if time.monotonic() - _state["contact"] > WATCHDOG_S:  # FR-22
            shutdown()


def main():
    signal.signal(signal.SIGHUP, lambda *a: shutdown())
    signal.signal(signal.SIGTERM, lambda *a: shutdown())
    threading.Thread(target=watchdog, daemon=True).start()
    send(hello=hardware())
    for line in sys.stdin:
        _state["contact"] = time.monotonic()
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        cmd = msg.get("cmd")
        try:
            if cmd == "watch":
                unwatch()
                _state["gpu"] = msg.get("gpu") or 0
                s = _state["sampler"] = Sampler(msg.get("gpu"), msg.get("disk"))
                threading.Thread(target=s.run, daemon=True).start()
            elif cmd == "unwatch":
                unwatch()
            elif cmd == "start":
                start_workload(msg["script"], float(msg["seconds"]),
                               msg.get("command") if msg["script"] == "usercode" else msg.get("disk"))
            elif cmd == "stop":
                stop_workload()
            elif cmd == "quit":
                break
        except Exception as e:
            send(error=f"{cmd}: {type(e).__name__}: {e}")
    shutdown()  # stdin closed: the SSH session ended


if __name__ == "__main__":
    main()
