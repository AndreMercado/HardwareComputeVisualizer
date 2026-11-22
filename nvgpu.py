"""NVIDIA GPU and PCIe bus readings through NVML (read-only).

Used by telemetry.py and the profiler; needs only the standard library and pynvml
(nvidia-ml-py).
"""
import threading
import time

try:
    import pynvml
    pynvml.nvmlInit()
    OK, ERROR = True, None
except Exception as e:  # no NVIDIA driver, or pynvml missing
    pynvml = None
    OK, ERROR = False, str(e) or type(e).__name__

# Usable bandwidth of one PCIe lane in one direction (GB/s), by link generation.
LANE_GBPS = {1: 0.25, 2: 0.5, 3: 0.985, 4: 1.969, 5: 3.938, 6: 7.563}
KEYS = ("gpu_util", "gpu_memctl", "gpu_vram", "gpu_vram_total", "gpu_temp", "gpu_power",
        "gpu_slowdown", "pcie_rx", "pcie_tx", "board_util")


def _s(v):
    return v.decode() if isinstance(v, bytes) else v


def names():
    """Names of the NVIDIA GPUs, in NVML order (the order CUDA uses with
    CUDA_DEVICE_ORDER=PCI_BUS_ID)."""
    if not OK:
        return []
    try:
        return [_s(pynvml.nvmlDeviceGetName(pynvml.nvmlDeviceGetHandleByIndex(i)))
                for i in range(pynvml.nvmlDeviceGetCount())]
    except Exception:
        return []


class Gpu:
    """One NVIDIA GPU. sample() returns the KEYS (None = unavailable).

    PCIe traffic is read on its own thread: each NVML throughput read waits
    ~20 ms while the driver measures, which would slow the main poll."""

    def __init__(self, index):
        self.handle = pynvml.nvmlDeviceGetHandleByIndex(index)
        self.slowdown = self._nv(pynvml.nvmlDeviceGetTemperatureThreshold,
                                 pynvml.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN)
        self.pcie = (None, None)  # (to GPU, from GPU) in MB/s
        self.link = None          # (generation, width, max width)
        self._stop = threading.Event()
        self._thread = None

    def _nv(self, fn, *args):
        try:
            return fn(self.handle, *args)
        except Exception:
            return None

    def start(self):
        self._stop.clear()
        if not (self._thread and self._thread.is_alive()):
            self._thread = threading.Thread(target=self._pcie_loop, daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()

    def _pcie_loop(self):
        while not self._stop.is_set():
            rx = self._nv(pynvml.nvmlDeviceGetPcieThroughput, pynvml.NVML_PCIE_UTIL_RX_BYTES)
            tx = self._nv(pynvml.nvmlDeviceGetPcieThroughput, pynvml.NVML_PCIE_UTIL_TX_BYTES)
            # NVML reports KB/s (1 KB = 1024 bytes); RX = received by the GPU
            self.pcie = (None if rx is None else rx * 1024 / 1e6,
                         None if tx is None else tx * 1024 / 1e6)
            if rx is None and tx is None:
                time.sleep(0.5)  # not supported on this GPU: don't spin

    def link_text(self):
        if not self.link:
            return None
        gen, width, max_width = self.link
        txt = f"Gen{gen} x{width}"
        return txt + (f" of x{max_width}" if max_width and max_width > width else "")

    def sample(self):
        s = dict.fromkeys(KEYS)
        u = self._nv(pynvml.nvmlDeviceGetUtilizationRates)
        if u is not None:
            s["gpu_util"], s["gpu_memctl"] = u.gpu, u.memory
        m = self._nv(pynvml.nvmlDeviceGetMemoryInfo)
        if m is not None:
            s["gpu_vram"], s["gpu_vram_total"] = m.used / 2**30, m.total / 2**30
        s["gpu_temp"] = self._nv(pynvml.nvmlDeviceGetTemperature, pynvml.NVML_TEMPERATURE_GPU)
        p = self._nv(pynvml.nvmlDeviceGetPowerUsage)
        s["gpu_power"] = p / 1000 if p is not None else None
        s["gpu_slowdown"] = self.slowdown

        s["pcie_rx"], s["pcie_tx"] = self.pcie
        # board load = busier direction of the GPU's link, as a share of what the link can carry.
        # Capacity uses the highest generation the link can run at: GPUs drop to Gen1 when idle.
        gen = self._nv(pynvml.nvmlDeviceGetMaxPcieLinkGeneration)
        width = self._nv(pynvml.nvmlDeviceGetCurrPcieLinkWidth)
        if gen and width:
            cur_gen = self._nv(pynvml.nvmlDeviceGetCurrPcieLinkGeneration) or gen
            self.link = (cur_gen, width, self._nv(pynvml.nvmlDeviceGetMaxPcieLinkWidth))
            if None not in self.pcie and gen in LANE_GBPS:
                cap_mbs = LANE_GBPS[gen] * width * 1000
                s["board_util"] = min(100.0, 100.0 * max(self.pcie) / cap_mbs)
        return s
