"""Read-only hardware detection + telemetry poller for this PC (Windows).

Sources: psutil (CPU, RAM, disk bytes), NVML through nvgpu.py (NVIDIA GPUs and
their PCIe link), Windows performance counters (disk active time, CPU package
power from RAPL, AMD / Intel GPU load and memory), DXGI (to find non-NVIDIA
GPUs) and, when it is running, LibreHardwareMonitor (CPU / chipset / non-NVIDIA
GPU temperatures - it needs administrator rights, this app does not).

Every metric is either a real sample or None ("unavailable"). Nothing here
writes to hardware settings.
"""
import collections
import ctypes
import json
import os
import platform
import re
import subprocess
import threading
import time
import uuid
from ctypes import wintypes

import psutil

import nvgpu

POLL_HZ = 20
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RAPL_POWER = "\\Energy Meter(RAPL_Package0_PKG)\\Power"  # mW, readable without admin
LHM_NAMESPACES = ("root/LibreHardwareMonitor", "root/OpenHardwareMonitor")
NVIDIA, MICROSOFT = 0x10DE, 0x1414


# ---------------------------------------------------------------- performance counters
class _Value(ctypes.Structure):
    _fields_ = [("status", ctypes.c_ulong), ("value", ctypes.c_double)]


class _Item(ctypes.Structure):
    _fields_ = [("name", ctypes.c_wchar_p), ("value", _Value)]


class Pdh:
    """One Windows performance-counter query (PDH). Counters are added by their
    English path; collect() once, then read each one. Read-only."""
    FMT = 0x200 | 0x8000  # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
    MORE_DATA = 0x800007D2

    def __init__(self):
        self._pdh = pdh = ctypes.WinDLL("pdh")
        pdh.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                                    ctypes.POINTER(_Value)]
        pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_ulong
        pdh.PdhGetFormattedCounterArrayW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                     ctypes.POINTER(ctypes.c_ulong),
                                                     ctypes.POINTER(ctypes.c_ulong), ctypes.c_void_p]
        pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        self._query = ctypes.c_void_p()
        if pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)):
            raise OSError("PdhOpenQuery failed")

    def add(self, path):
        """Counter handle, or None if this PC has no such counter."""
        c = ctypes.c_void_p()
        return None if self._pdh.PdhAddEnglishCounterW(self._query, path, None, ctypes.byref(c)) else c

    def collect(self):
        return self._pdh.PdhCollectQueryData(self._query) == 0

    def value(self, counter):
        v = _Value()
        if self._pdh.PdhGetFormattedCounterValue(counter, self.FMT, None, ctypes.byref(v)) or v.status:
            return None
        return v.value

    def array(self, counter):
        """{instance name: value} of a wildcard (*) counter."""
        size, count = ctypes.c_ulong(0), ctypes.c_ulong(0)
        rc = self._pdh.PdhGetFormattedCounterArrayW(counter, self.FMT, ctypes.byref(size),
                                                    ctypes.byref(count), None)
        if rc != self.MORE_DATA or not size.value:
            return {}
        buf = ctypes.create_string_buffer(size.value)
        if self._pdh.PdhGetFormattedCounterArrayW(counter, self.FMT, ctypes.byref(size),
                                                  ctypes.byref(count), buf):
            return {}
        items = ctypes.cast(buf, ctypes.POINTER(_Item))
        return {items[i].name: items[i].value.value for i in range(count.value)
                if items[i].value.status in (0, 1)}  # PDH_CSTATUS_VALID_DATA / NEW_DATA

    def close(self):
        self._pdh.PdhCloseQuery(self._query)


# ---------------------------------------------------------------- non-NVIDIA GPUs (DXGI)
class _Luid(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]


class _AdapterDesc1(ctypes.Structure):
    _fields_ = [("description", ctypes.c_wchar * 128), ("vendor", ctypes.c_uint),
                ("device", ctypes.c_uint), ("subsys", ctypes.c_uint), ("revision", ctypes.c_uint),
                ("vram", ctypes.c_size_t), ("sysmem", ctypes.c_size_t), ("shared", ctypes.c_size_t),
                ("luid", _Luid), ("flags", ctypes.c_uint)]


def _com_call(obj, index, *args, argtypes=()):
    """Call method number `index` of a COM object's vtable."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    return fn(obj, *args)


def dxgi_adapters():
    """Every hardware graphics adapter Windows knows: [{'name', 'vendor', 'luid', 'vram'}].
    'luid' is the id the GPU performance counters use."""
    out = []
    try:
        iid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID("770aae78-f26f-4dba-a829-253c83d1b387").bytes_le)
        factory = ctypes.c_void_p()
        if ctypes.WinDLL("dxgi").CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) or not factory:
            return out
        i = 0
        while True:
            adapter = ctypes.c_void_p()
            # IDXGIFactory1::EnumAdapters1 is vtable slot 12; fails with NOT_FOUND after the last one
            if _com_call(factory, 12, i, ctypes.byref(adapter),
                         argtypes=(ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))):
                break
            desc = _AdapterDesc1()
            # IDXGIAdapter1::GetDesc1 is slot 10, Release is slot 2
            ok = _com_call(adapter, 10, ctypes.byref(desc), argtypes=(ctypes.POINTER(_AdapterDesc1),)) == 0
            _com_call(adapter, 2)
            if ok and not desc.flags & 2 and desc.vendor != MICROSOFT:  # skip software adapters
                out.append({"name": desc.description.strip(), "vendor": desc.vendor,
                            "luid": f"luid_0x{desc.luid.high & 0xFFFFFFFF:08X}_0x{desc.luid.low:08X}",
                            "vram": desc.vram})
            i += 1
        _com_call(factory, 2)
    except (OSError, AttributeError, ValueError):
        pass
    return out


class _PdhGpu:
    """Load and memory of any GPU from the counters Task Manager uses
    (GPU Engine / GPU Adapter Memory). Read-only."""
    _ENGINE = re.compile(r"(luid_0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+)_phys_(\d+)_eng_(\d+)")

    def __init__(self, pdh, luid, vram):
        self.luid, self.vram = luid.upper(), vram
        self.pdh = pdh
        self.engines = pdh.add("\\GPU Engine(*)\\Utilization Percentage")
        self.memory = pdh.add("\\GPU Adapter Memory(*)\\Dedicated Usage")

    def sample(self):
        s = {}
        if self.engines:
            busy = {}  # engine -> summed over the processes using it
            for name, v in self.pdh.array(self.engines).items():
                m = self._ENGINE.search(name)
                if m and m.group(1).upper() == self.luid:
                    busy[m.group(2, 3)] = busy.get(m.group(2, 3), 0.0) + v
            # like Task Manager: the GPU is as busy as its busiest engine
            s["gpu_util"] = min(100.0, max(busy.values())) if busy else 0.0
        if self.memory:
            used = [v for n, v in self.pdh.array(self.memory).items() if n.upper().startswith(self.luid)]
            if used:
                s["gpu_vram"] = sum(used) / 2**30
                s["gpu_vram_total"] = self.vram / 2**30 if self.vram else None
        return s


# ---------------------------------------------------------------- LibreHardwareMonitor
LHM_URL = "http://127.0.0.1:8085/data.json"  # LHM's own web server (Options > Remote Web Server)
# LHM sensor ids start with the hardware kind; mapped to the names pick_lhm() / board_temp() use
_LHM_KINDS = (("/intelcpu/", "Cpu"), ("/amdcpu/", "Cpu"), ("/gpu-nvidia/", "GpuNvidia"), ("/gpu-amd/", "GpuAmd"),
              ("/gpu-intel/", "GpuIntel"), ("/lpc/", "SuperIO"), ("/motherboard", "Motherboard"))
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def lhm_web_sensors(data):
    """Flatten LHM's data.json tree into [{'hw', 'kind', 'name', 'type', 'value'}]
    (temperatures and power only). Values arrive as text like "40.0 °C"."""
    out = []

    def walk(node, hw):
        if node.get("HardwareId"):
            hw = node.get("Text", "")
        sid, stype = node.get("SensorId"), node.get("Type")
        if sid and stype in ("Temperature", "Power"):
            m = _NUMBER.search(str(node.get("Value", "")))
            kind = next((k for prefix, k in _LHM_KINDS if sid.startswith(prefix)), "")
            if m:
                out.append({"hw": hw, "kind": kind, "name": node.get("Text", ""), "type": stype,
                            "value": float(m.group(0).replace(",", "."))})
        for child in node.get("Children") or []:
            walk(child, hw)
    walk(data, "")
    return out


def _lhm_web_up(timeout=0.5):
    import urllib.request
    try:
        with urllib.request.urlopen(LHM_URL, timeout=timeout) as r:
            return r.status == 200
    except (OSError, ValueError):
        return False


class _Lhm:
    """Temperatures/power from LibreHardwareMonitor, read ~4x per second. LHM itself
    must run (as administrator); this only reads what it publishes - from its local
    web server (LHM 0.9.6+), or from WMI through a PowerShell helper (older LHM,
    OpenHardwareMonitor)."""
    _PS = r"""
$ns = '%s'; $app = %d; $hw = @{}
while ($true) {
  if (-not (Get-Process -Id $app -ErrorAction SilentlyContinue)) { exit }
  try {
    foreach ($h in Get-CimInstance -Namespace $ns -ClassName Hardware -ErrorAction Stop) {
      $hw[$h.Identifier] = @($h.Name, [string]$h.HardwareType) }
    $out = @(Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop |
      Where-Object { $_.SensorType -eq 'Temperature' -or $_.SensorType -eq 'Power' } |
      ForEach-Object { $p = $hw[$_.Parent]; if (-not $p) { $p = @('', '') }
        [pscustomobject]@{ hw = $p[0]; kind = $p[1]; name = $_.Name; type = [string]$_.SensorType;
                           value = [double]$_.Value } })
    [Console]::Out.WriteLine((ConvertTo-Json -Compress -Depth 2 -InputObject $out))
  } catch { [Console]::Out.WriteLine('[]') }
  Start-Sleep -Milliseconds 300
}
"""
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls, source):
        with cls._lock:
            if cls._instance is None or cls._instance.source != source or not cls._instance.alive():
                cls._instance = cls(source)
            return cls._instance

    def __init__(self, source):
        self.source = source  # "web" or a WMI namespace
        self.sensors, self.t = [], 0.0
        self.proc = None
        if source == "web":
            threading.Thread(target=self._poll_web, daemon=True).start()
        else:
            self.proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", self._PS % (source, os.getpid())],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True,
                creationflags=NO_WINDOW)
            threading.Thread(target=self._read_wmi, daemon=True).start()

    def alive(self):
        return self.proc.poll() is None if self.proc else True

    def _poll_web(self):
        import urllib.request
        while True:
            try:
                with urllib.request.urlopen(LHM_URL, timeout=2) as r:
                    self.sensors = lhm_web_sensors(json.loads(r.read().decode("utf-8", "replace")))
                self.t = time.monotonic()
            except (OSError, ValueError):
                pass  # LHM closed or busy: readings() goes empty after 3 s
            time.sleep(0.25)

    def _read_wmi(self):
        for line in self.proc.stdout:
            try:
                data = json.loads(line)
            except ValueError:
                continue
            self.sensors = [data] if isinstance(data, dict) else data
            self.t = time.monotonic()

    def readings(self):
        """Current sensors, or [] if nothing new arrived for 3 s (LHM closed)."""
        return self.sensors if time.monotonic() - self.t < 3.0 else []


def pick_lhm(sensors, kinds, hw_name=None):
    """{'temp': °C, 'power': W} for the LHM hardware of one of `kinds`
    (optionally whose name matches hw_name). Missing readings are None."""
    preferred = {"Temperature": ("CPU Package", "Core (Tctl/Tdie)", "Core (Tdie)", "Core (Tctl)",
                                 "GPU Core", "GPU Hot Spot", "Chipset", "PCH", "Core Average", "Core Max"),
                 "Power": ("CPU Package", "Package", "GPU Package", "GPU Power", "GPU Core")}
    out = {"temp": None, "power": None}
    pool = [s for s in sensors if any(str(s.get("kind", "")).lower().startswith(k.lower()) for k in kinds)]
    if hw_name:
        n = hw_name.lower()
        pool = [s for s in pool if str(s.get("hw", "")).lower() in n or n in str(s.get("hw", "")).lower()]
    for key, stype in (("temp", "Temperature"), ("power", "Power")):
        cands = [s for s in pool if s.get("type") == stype and isinstance(s.get("value"), (int, float))]
        for want in preferred[stype]:
            hit = [s for s in cands if want.lower() in str(s.get("name", "")).lower()]
            if hit:
                out[key] = float(hit[0]["value"])
                break
    return out


def board_temp(sensors):
    """Chipset temperature from LHM's motherboard / Super I/O sensors, if it has one."""
    for s in sensors:
        if str(s.get("kind")) in ("SuperIO", "Motherboard") and s.get("type") == "Temperature":
            name = str(s.get("name", "")).lower()
            if ("chipset" in name or "pch" in name) and isinstance(s.get("value"), (int, float)):
                return float(s["value"])
    return None


# ---------------------------------------------------------------- detection
# One PowerShell call: CPU and board names, LHM's WMI namespace if it runs, and the
# physical disks with their drive letters.
_DETECT_PS = (
    "$d = @(Get-PhysicalDisk | ForEach-Object { $n = [int]$_.DeviceId; [pscustomobject]@{"
    " id = $n; name = $_.FriendlyName; media = [string]$_.MediaType; size = [double]$_.Size;"
    " letters = (@(Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue |"
    " Where-Object DriveLetter | ForEach-Object { [string]$_.DriveLetter }) -join '') } });"
    " $b = Get-CimInstance Win32_BaseBoard | Select-Object -First 1;"
    " $l = $null; foreach ($ns in '" + "','".join(LHM_NAMESPACES) + "') {"
    " if (Get-CimInstance -Namespace $ns -ClassName Hardware -ErrorAction SilentlyContinue |"
    " Select-Object -First 1) { $l = $ns; break } };"
    " [pscustomobject]@{ cpu = (Get-CimInstance Win32_Processor | Select-Object -First 1).Name;"
    " board = ('' + $b.Manufacturer + ' ' + $b.Product).Trim(); lhm = $l;"
    " disks = $d } | ConvertTo-Json -Depth 4 -Compress"
)
_LHM_PROBE_PS = ("foreach ($ns in '" + "','".join(LHM_NAMESPACES) + "') {"
                 " if (Get-CimInstance -Namespace $ns -ClassName Hardware -ErrorAction SilentlyContinue |"
                 " Select-Object -First 1) { $ns; break } }")
LHM = None  # "web" (LHM's web server), a WMI namespace (older LHM / OpenHardwareMonitor), or None


def _disk_label(d):
    size = d["size"] / 1e9
    size_s = f"{size / 1000:.0f} TB" if size >= 950 else f"{size:.0f} GB"
    kind = d["media"] if d["media"] in ("SSD", "HDD") else "disk"
    drives = " ".join(f"{c}:" for c in d["letters"]) or "no drive letter"
    return f"{d['name']} ({drives}) - {size_s} {kind}"


def probe_lhm():
    """Look again for a running LibreHardwareMonitor (it may have been started after the app)."""
    global LHM
    if _lhm_web_up():
        LHM = "web"
        return LHM
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _LHM_PROBE_PS], capture_output=True,
                             text=True, timeout=10, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        out = ""
    LHM = out or None
    return LHM


def rapl_available():
    try:
        p = Pdh()
    except OSError:
        return False
    try:
        return p.add(RAPL_POWER) is not None
    finally:
        p.close()


def detect_hardware():
    """{'machine', 'cpu', 'board', 'ram', 'gpus': [names], 'gpu_info': [{'kind': 'nvml', 'index'} |
    {'kind': 'pdh', 'luid', 'vram'}], 'disks': [{'id', 'name', 'media', 'size', 'letters', 'label'}],
    'cpu_temp': bool, 'cpu_power': bool}."""
    global LHM
    cpu, board = platform.processor() or None, None
    disks = []
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _DETECT_PS], capture_output=True,
                             text=True, timeout=15, creationflags=NO_WINDOW).stdout.strip()
        info = json.loads(out)
        cpu = (info.get("cpu") or "").strip() or cpu
        board = (info.get("board") or "").strip() or None
        LHM = "web" if _lhm_web_up() else (info.get("lhm") or None)
        found = info.get("disks") or []
        for d in ([found] if isinstance(found, dict) else found):
            d = {"id": int(d["id"]), "name": str(d.get("name") or f"Disk {d['id']}").strip(),
                 "media": str(d.get("media") or ""), "size": float(d.get("size") or 0),
                 "letters": sorted(str(d.get("letters") or "").upper())}
            d["label"] = _disk_label(d)
            disks.append(d)
    except Exception:
        pass
    if not disks:  # PowerShell unavailable: fall back to what psutil can see
        try:
            names = sorted(psutil.disk_io_counters(perdisk=True))
        except Exception:
            names = []
        sys_letter = os.environ.get("SystemDrive", "C:")[0].upper()
        for n in names:
            if n.startswith("PhysicalDrive") and n[13:].isdigit():
                i = int(n[13:])
                disks.append({"id": i, "name": n, "media": "", "size": 0.0,
                              "letters": [sys_letter] if i == 0 else [],
                              "label": f"{n} ({sys_letter}:)" if i == 0 else n})
    disks.sort(key=lambda d: d["id"])
    if cpu:
        cpu = f"{cpu} ({psutil.cpu_count(logical=False)}C/{psutil.cpu_count()}T)"

    # NVIDIA GPUs through NVML; every other GPU through the Windows GPU counters
    gpus = nvgpu.names()
    gpu_info = [{"kind": "nvml", "index": i} for i in range(len(gpus))]
    force_pdh = os.environ.get("HCV_FORCE_PDH_GPU") == "1"  # debug: read NVIDIA GPUs the AMD/Intel way
    for a in dxgi_adapters():
        if a["vendor"] == NVIDIA and gpus and not force_pdh:
            continue
        gpus.append(a["name"] + (" (Windows counters)" if force_pdh and a["vendor"] == NVIDIA else ""))
        gpu_info.append({"kind": "pdh", "luid": a["luid"], "vram": a["vram"]})

    ram = f"{psutil.virtual_memory().total / 2**30:.0f} GB RAM"
    return {"machine": platform.node() or "this PC", "cpu": cpu, "threads": psutil.cpu_count() or 1,
            "board": board, "ram": ram,
            "gpus": gpus, "gpu_info": gpu_info, "disks": disks,
            "cpu_temp": LHM is not None, "cpu_power": rapl_available()}


# ---------------------------------------------------------------- poller
class Poller:
    """Samples sensors at POLL_HZ on a thread. `latest` is a dict of
    metric -> value (None = unavailable), plus 't' (monotonic sample time).
    `sources` names where each metric comes from; `info` holds text facts
    (the PCIe link) that are not numbers."""

    def __init__(self, gpu=None, disk=None):
        self.gpu_info = gpu   # one entry of detect_hardware()['gpu_info'], or None
        self.disk = disk      # one entry of detect_hardware()['disks'], or None
        self.latest = {}
        self.info = {}
        self._stop = threading.Event()
        self._thread = None
        self._nv = self._pdh_gpu = None
        self._pdh = None
        try:
            self._pdh = Pdh()
        except OSError:
            pass
        if gpu and gpu["kind"] == "nvml" and nvgpu.OK:
            try:
                self._nv = nvgpu.Gpu(gpu["index"])
            except Exception:
                self._nv = None
        elif gpu and gpu["kind"] == "pdh" and self._pdh:
            self._pdh_gpu = _PdhGpu(self._pdh, gpu["luid"], gpu["vram"])
        self._rapl = self._pdh.add(RAPL_POWER) if self._pdh else None
        self._rapl_t = None  # time of the previous collection
        self._rapl_hist = collections.deque()  # (time, interval, mW over that interval)
        self._disk_key = f"PhysicalDrive{disk['id']}" if disk else None
        self._disk_last = None  # (time, read_bytes, write_bytes) of the previous sample
        self._disk_idle = None
        if disk and self._pdh:
            # instance name is "<number> <letter>: <letter>: ..." (just the number if no letters)
            inst = " ".join([str(disk["id"])] + [f"{c}:" for c in disk["letters"]])
            self._disk_idle = self._pdh.add("\\PhysicalDisk(" + inst + ")\\% Idle Time")
        self._lhm = _Lhm.get(LHM) if LHM else None

        lhm = "LibreHardwareMonitor" if self._lhm else "needs LHM + admin"
        self.sources = {"cpu_temp": lhm, "cpu_power": "Windows RAPL counter" if self._rapl else "not exposed",
                        "board_temp": lhm, "disk_util": "Windows perf counter"}
        if self._pdh_gpu:
            self.sources.update(gpu_util="Windows GPU counters", gpu_vram="Windows GPU counters",
                                gpu_temp=lhm, gpu_power=lhm)
        if not self._nv:
            for k in ("pcie_rx", "pcie_tx"):
                self.sources[k] = "needs NVIDIA (NVML)"

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        psutil.cpu_percent(None)  # prime
        psutil.cpu_percent(None, percpu=True)
        if self._pdh:
            self._pdh.collect()  # rates need two collections
            self._rapl_t = time.monotonic()
        if self._nv:
            self._nv.start()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._nv:
            self._nv.stop()

    def sample(self):
        s = {"t": time.monotonic()}
        try:
            s["cpu_util"] = psutil.cpu_percent(None)
        except Exception:
            s["cpu_util"] = None
        try:  # each logical CPU: one maxed core among idle ones = serial code
            cores = psutil.cpu_percent(None, percpu=True)
            s["cpu_cores"] = [round(c) for c in cores]
            s["cpu_core_max"] = max(cores) if cores else None
        except Exception:
            s["cpu_cores"] = s["cpu_core_max"] = None
        try:
            f = psutil.cpu_freq()
            s["cpu_freq"] = f.current if f else None
        except Exception:
            s["cpu_freq"] = None
        try:
            vm = psutil.virtual_memory()
            s["ram_util"] = vm.percent
            s["ram_used"] = vm.used / 2**30
        except Exception:
            s["ram_util"] = s["ram_used"] = None

        collected = self._pdh.collect() if self._pdh else False
        s["cpu_power"] = self._cpu_power(s["t"]) if collected and self._rapl else None

        s.update(dict.fromkeys(nvgpu.KEYS))
        if self._nv:
            s.update(self._nv.sample())
            link = self._nv.link_text()
            if link:
                self.info["pcie_link"] = link
        elif self._pdh_gpu and collected:
            s.update(self._pdh_gpu.sample())

        sensors = self._lhm.readings() if self._lhm else []
        s["cpu_temp"] = pick_lhm(sensors, ("Cpu",))["temp"]
        s["board_temp"] = board_temp(sensors)
        if sensors and s["board_temp"] is None:
            self.sources["board_temp"] = "LHM names no chipset sensor"
        if self._pdh_gpu and self.gpu_info:
            g = pick_lhm(sensors, ("Gpu",))
            s["gpu_temp"], s["gpu_power"] = g["temp"], g["power"]

        s["disk_util"] = s["disk_read"] = s["disk_write"] = None
        if self._disk_key:
            idle = self._pdh.value(self._disk_idle) if collected and self._disk_idle else None
            if idle is not None:
                s["disk_util"] = max(0.0, min(100.0, 100.0 - idle))
            try:
                io = psutil.disk_io_counters(perdisk=True).get(self._disk_key)
            except Exception:
                io = None
            if io:
                last, self._disk_last = self._disk_last, (s["t"], io.read_bytes, io.write_bytes)
                if last and s["t"] > last[0]:
                    dt = s["t"] - last[0]
                    s["disk_read"] = max(0, io.read_bytes - last[1]) / dt / 1e6
                    s["disk_write"] = max(0, io.write_bytes - last[2]) / dt / 1e6
        return s

    def _cpu_power(self, now):
        """CPU package watts over the last ~0.5 s. Each RAPL reading is the average over the
        time since the previous collection, which can be a few ms (noisy), so readings are
        weighted by their interval: the result is energy used / time taken."""
        mw = self._pdh.value(self._rapl)
        if mw is not None and self._rapl_t is not None and now > self._rapl_t:
            self._rapl_hist.append((now, now - self._rapl_t, mw))
        self._rapl_t = now
        while self._rapl_hist and now - self._rapl_hist[0][0] > 0.5:
            self._rapl_hist.popleft()
        span = sum(dt for _, dt, _ in self._rapl_hist)
        if span < 0.15:
            return None
        return sum(dt * v for _, dt, v in self._rapl_hist) / span / 1000

    def _run(self):
        period = 1.0 / POLL_HZ
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                self.latest = self.sample()
            except Exception:
                pass  # one bad read must not end polling; stale data shows as unavailable
            time.sleep(max(0.0, period - (time.monotonic() - t0)))
        if self._pdh:
            try:
                self._pdh.close()
            except Exception:
                pass


if __name__ == "__main__":
    hw = detect_hardware()
    print(json.dumps(hw, indent=1))
    p = Poller(gpu=hw["gpu_info"][0] if hw["gpu_info"] else None, disk=hw["disks"][0] if hw["disks"] else None)
    p.start()
    time.sleep(1.0)
    print(p.latest, p.info, p.sources)
    p.stop()
