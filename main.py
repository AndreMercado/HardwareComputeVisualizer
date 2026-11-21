"""Hardware Compute Visualizer.

Pick a machine (this PC, or a remote Linux machine over SSH), a part (RAM / CPU /
GPU / board / disk) and a test, press Start: a real workload runs in its own
process, sensors are polled read-only at 20 Hz, and the diagram + readout follow
the real numbers. Every run is recorded to runs/ so it can be compared with the
previous one, compared side by side with any other run (another machine, or the
same program before and after a change) and replayed. YOUR CODE runs the user's
own program and analysis.py points out where it wastes the hardware.
AI CONTROL lets another program (an AI) drive the window through control.py
while you watch. See SRS_HardwareComputeVisualizer.md.
"""
import collections
import datetime
import getpass
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog
from tkinter import messagebox as _messagebox

import psutil

import agent
import analysis
import control
import remote
import telemetry

BG = "#070707"      # near-black base
PANEL = "#101010"
LINE = "#222222"    # hairlines / grid
FG = "#e8e8e8"
MUTED = "#6b6b6b"
GREY = "#2a2a2a"
FONT = "Bahnschrift"  # Windows' condensed techy UI face; Tk falls back if missing
ACCENT = "#44d62c"    # brand green (title, Start)
REPLAY_COLOR = "#ffb400"  # everything that marks "this is a recording, not live"
AI_COLOR = "#c77dff"      # everything an AI does through AI CONTROL
EDITOR_COLOR = "#4fc1ff"  # what VS Code asked for (editor link) and the links back into the code
SEGMENTS = 22         # lit segments in each part's meter
CANVAS_W, CANVAS_H = 840, 270
HISTORY = 150         # points in each part's scrolling graph (one per 3 frames)

TEST_SECONDS = 30
THERMAL_LIMIT = {"cpu": 95, "gpu": 90}  # FR-19 defaults (GPU: lower of this and vendor slowdown)
STALE_S = 1.0  # a workload number older than this is shown as unavailable


def lighten(hex_color, amount=0.35):
    """Lighten a #rrggbb color toward white by amount (0-1)."""
    return mix(hex_color, "#ffffff", amount)


def mix(c1, c2, t):
    """Blend #rrggbb c1 -> c2 by t (0-1)."""
    a, b = c1.lstrip("#"), c2.lstrip("#")
    out = []
    for i in (0, 2, 4):
        x, y = int(a[i:i + 2], 16), int(b[i:i + 2], 16)
        out.append(int(x + (y - x) * max(0.0, min(1.0, t))))
    return "#{:02x}{:02x}{:02x}".format(*out)


def chamfer(canvas, x0, y0, x1, y1, c=14, **kwargs):
    """Rectangle with two cut corners (top-left, bottom-right), gaming-panel style."""
    points = [x0 + c, y0, x1, y0, x1, y1 - c, x1 - c, y1, x0, y1, x0, y0 + c]
    return canvas.create_polygon(points, **kwargs)


# Parts shown in the diagram, left to right. To add a part: add an entry here, give it
# telemetry keys in STATS and an "<part>_util" sample - the diagram and the search boxes
# lay themselves out from this table (NFR-5).
# Neon versions of the SRS colors (CPU blue, GPU green, RAM yellow) + storage magenta, board teal.
# Laid out the way data flows: RAM - CPU - GPU (in its PCIe slot) - board/chipset - disk.
PARTS = {
    "ram": {"label": "RAM", "color": "#ffb400"},
    "cpu": {"label": "CPU", "color": "#00a8ff"},
    "gpu": {"label": "GPU", "color": "#44d62c"},
    "board": {"label": "BOARD", "color": "#1de9b6"},
    "disk": {"label": "DISK", "color": "#ff4d9d"},
}
ORDER = list(PARTS)
SLOT_MARGIN, SLOT_GAP, SLOT_TOP, SLOT_BOTTOM = 18, 28, 28, 208
_slot_w = (CANVAS_W - 2 * SLOT_MARGIN - SLOT_GAP * (len(ORDER) - 1)) / len(ORDER)
for _i, _key in enumerate(ORDER):
    _x = SLOT_MARGIN + _i * (_slot_w + SLOT_GAP)
    PARTS[_key]["rect"] = (round(_x), SLOT_TOP, round(_x + _slot_w), SLOT_BOTTOM)
# neighbours are joined directly; the others are buses routed under the boxes
LINKS = [("ram", "cpu"), ("cpu", "gpu"), ("gpu", "board"), ("board", "disk"), ("cpu", "board")]

# workload (UI choice) -> {part: script}; which parts each script drives
TESTS = {
    "2d": {"label": "2D workload", "short": "2D", "targets": {"cpu": "cpu2d", "ram": "ram"}},
    "3d": {"label": "3D workload", "short": "3D", "targets": {"gpu": "gpu3d"}},
    "ml": {"label": "ML workload", "short": "ML", "targets": {"gpu": "mlgpu", "cpu": "mlcpu"}},
    "io": {"label": "Disk I/O workload", "short": "DISK I/O", "targets": {"disk": "disk"}},
    "bus": {"label": "PCIe bus workload", "short": "PCIe", "targets": {"board": "pcie"}},
    # the agent may touch any part, so every part is involved; SRS default limit is 10 min
    "ai": {"label": "AI benchmark", "short": "AI BENCHMARK", "targets": {p: "agent" for p in ORDER},
           "seconds": 600, "local_only": True},
    # the user's own program (SRS ultimate goal): runs until it exits, at most 10 min
    "code": {"label": "Your program", "short": "YOUR CODE", "targets": {p: "usercode" for p in ORDER},
             "seconds": 600},
}
OBSERVED = {"cpu2d": ("gpu", "ram"), "ram": ("cpu",), "gpu3d": ("cpu", "ram", "board"),
            "mlgpu": ("cpu", "ram", "board"), "mlcpu": ("ram",), "disk": ("cpu", "ram"),
            "pcie": ("ram", "cpu", "gpu"), "agent": tuple(ORDER), "usercode": tuple(ORDER)}
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = control.DATA_DIR  # this folder, or the user's HCV folder when the app runs from the VS Code extension
AI_DIR = os.path.join(DATA, "ai_workspace")
RUNS_DIR = os.path.join(DATA, "runs")  # one JSON file per finished run (record / compare / replay)
REMOTE_FILE = os.path.join(DATA, "remote_target.json")  # last remote host/user/port/key *path* - no secrets

# key, label, unit, part (or "wl" = measured by the workload), default source
# (the poller names a different source where it has one: remote machine, AMD/Intel GPU...)
WL_SRC = "workload-measured"
STATS = [
    ("cpu_util", "CPU utilization", "%", "cpu", "psutil"),
    ("cpu_core_max", "Busiest CPU core", "%", "cpu", "psutil"),
    ("cpu_freq", "CPU frequency", "MHz", "cpu", "psutil"),
    ("cpu_temp", "CPU temperature", "°C", "cpu", "needs LHM + admin"),
    ("cpu_power", "CPU power draw", "W", "cpu", "Windows RAPL counter"),
    ("ram_util", "RAM in use", "%", "ram", "psutil"),
    ("ram_used", "RAM used", "GB", "ram", "psutil"),
    ("gpu_util", "GPU utilization", "%", "gpu", "NVML"),
    ("gpu_vram", "GPU VRAM used", "GB", "gpu", "NVML"),
    ("gpu_temp", "GPU temperature", "°C", "gpu", "NVML"),
    ("gpu_power", "GPU power draw", "W", "gpu", "NVML"),
    ("board_util", "PCIe link load", "%", "board", "NVML"),
    ("pcie_rx", "Bus to GPU", "MB/s", "board", "NVML"),
    ("pcie_tx", "Bus from GPU", "MB/s", "board", "NVML"),
    ("board_temp", "Chipset temp", "°C", "board", "needs LHM + admin"),
    ("disk_util", "Disk active time", "%", "disk", "Windows perf counter"),
    ("disk_read", "Disk read", "MB/s", "disk", "psutil"),
    ("disk_write", "Disk write", "MB/s", "disk", "psutil"),
    ("gbps", "Memory bandwidth", "GB/s", "wl", WL_SRC),
    ("pcie_gbps", "Bus transfer", "GB/s", "wl", WL_SRC),
    ("io_mbps", "Disk throughput", "MB/s", "wl", WL_SRC),
    ("frame_ms", "Frame time", "ms", "wl", WL_SRC),
    ("gflops", "Compute", "GFLOPS", "wl", WL_SRC + " (approx)"),
    ("sps", "ML training speed", "samples/s", "wl", WL_SRC),
    ("loss", "ML training loss", "", "wl", WL_SRC),
]
WL_KEYS = [s[0] for s in STATS if s[3] == "wl"]
OPTIONAL = {"cpu_temp", "cpu_power", "board_temp"}  # often not exposed: never "telemetry lost" on their own
FMT = {"cpu_freq": "{:.0f}", "cpu_temp": "{:.0f}", "gpu_temp": "{:.0f}", "gpu_util": "{:.0f}",
       "cpu_util": "{:.1f}", "ram_util": "{:.1f}", "gpu_power": "{:.1f}", "disk_util": "{:.1f}",
       "disk_read": "{:.0f}", "disk_write": "{:.0f}", "io_mbps": "{:.0f}", "sps": "{:.0f}",
       "loss": "{:.3f}", "cpu_power": "{:.1f}", "board_util": "{:.1f}", "pcie_rx": "{:.0f}",
       "pcie_tx": "{:.0f}", "board_temp": "{:.0f}", "cpu_core_max": "{:.0f}"}
LOWER_IS_BETTER = {"frame_ms", "loss"}  # for the comparison with the previous run


class _Dialogs:
    """Stands in for tkinter.messagebox. While an AI controls the app nobody may be at the
    keyboard to click a pop-up, so notices go to the log (and back to the AI), and a
    "continue?" question is answered by the AI's own `confirm` flag on START."""

    def __init__(self):
        self.app = None
        self.confirm = False
        self.notes = []  # what the dialogs said during the current AI action

    def _ai(self):
        return self.app is not None and (self.app.control is not None or self.app.editor_busy)

    def _note(self, kind, title, msg):
        self.notes.append({"kind": kind, "title": title, "text": msg})
        if self.app.editor_busy:
            self.app._log(f"💻 [{title}] {msg}", "editor")
        else:
            self.app._log(f"🤖 [{title}] {msg}", "ai")

    def showerror(self, title, msg, **kw):
        return self._note("error", title, msg) if self._ai() else _messagebox.showerror(title, msg, **kw)

    def showwarning(self, title, msg, **kw):
        return self._note("warning", title, msg) if self._ai() else _messagebox.showwarning(title, msg, **kw)

    def showinfo(self, title, msg, **kw):
        return self._note("info", title, msg) if self._ai() else _messagebox.showinfo(title, msg, **kw)

    def askokcancel(self, title, msg, **kw):
        if not self._ai():
            return _messagebox.askokcancel(title, msg, **kw)
        self._note("question", title, msg)
        return self.confirm


messagebox = _Dialogs()


class Replay:
    """Stands in for telemetry.Poller while a recorded run is played back:
    `latest` is the recorded sample for the current playback time."""

    def __init__(self, samples):
        self.samples = samples
        self.i = 0
        self.t0 = time.monotonic()

    def start(self):
        self.t0 = time.monotonic()

    def stop(self):
        pass

    @property
    def latest(self):
        elapsed = time.monotonic() - self.t0
        while self.i + 1 < len(self.samples) and self.samples[self.i + 1]["t"] <= elapsed:
            self.i += 1
        return self.samples[self.i]


class SearchCombo:
    """Type to filter, click a result to pick it. The result list floats over
    the layout (child of `overlay`) so it never pushes widgets around."""

    def __init__(self, parent, overlay, key, options, color, on_pick):
        self.key = key
        self.options = options
        self.on_pick = on_pick
        self.color = color
        self.hover_color = lighten(color, 0.35)
        self._focused = False
        self._no_match = False

        self.frame = tk.Frame(parent, bg=BG)
        self.var = tk.StringVar()
        self.entry = tk.Entry(
            self.frame, textvariable=self.var, font=(FONT, 10), width=12,
            bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
            highlightthickness=2, highlightbackground=color, highlightcolor=color,
        )
        self.entry.pack(fill="x", ipady=6)
        self.listbox = tk.Listbox(
            overlay, height=3, font=(FONT, 10), bg=PANEL, fg=FG, relief="flat",
            selectbackground=color, activestyle="none", highlightthickness=0, exportselection=False,
        )
        self.listbox.bind("<<ListboxSelect>>", self._on_select)

        self.enabled = bool(options)
        if self.enabled:
            self.entry.bind("<FocusIn>", self._on_focus_in)
            self.entry.bind("<FocusOut>", self._on_focus_out)
            self.entry.bind("<KeyRelease>", self._filter)
            self.entry.bind("<Enter>", lambda e: self._set_border(self.hover_color))
            self.entry.bind("<Leave>", lambda e: None if self._focused else self._set_border(color))
            self.clear()
        else:
            self.var.set("not detected")
            self.entry.config(state="disabled", fg=MUTED, disabledforeground=MUTED,
                              disabledbackground=PANEL, highlightbackground=GREY)

    def _set_border(self, c):
        self.entry.config(highlightbackground=c, highlightcolor=c)

    def _on_focus_in(self, event=None):
        self._focused = True
        self._set_border(self.hover_color)
        if self._placeholder:
            self.entry.delete(0, "end")
            self.entry.config(fg=FG)
            self._placeholder = False
        self._show_results(self.options)

    def _on_focus_out(self, event=None):
        self._focused = False
        self._set_border(self.color)
        self.entry.after(150, self._maybe_hide)
        if not self.var.get():
            self.clear()

    def _maybe_hide(self):
        if self.entry.focus_get() not in (self.entry, self.listbox):
            self._hide_results()

    def _filter(self, event=None):
        q = self.var.get().strip().lower()
        matches = [o for o in self.options if q in o.lower()] if q else self.options
        self._show_results(matches)

    def _show_results(self, matches):
        self._no_match = not matches
        self.listbox.delete(0, "end")
        for m in (matches or ["No matches"]):
            self.listbox.insert("end", m)
        self.listbox.config(height=min(3, max(1, len(matches))))
        self.listbox.place(in_=self.entry, x=0, rely=1.0, y=2, relwidth=1.0)
        self.listbox.lift()

    def _hide_results(self):
        self.listbox.place_forget()

    def _on_select(self, event=None):
        sel = self.listbox.curselection()
        if not sel or self._no_match:
            return
        value = self.listbox.get(sel[0])
        self.set_value(value)
        self._hide_results()
        self.entry.master.focus_set()
        self.on_pick(self.key, value)

    def set_value(self, value):
        self._placeholder = False
        self.entry.delete(0, "end")
        self.entry.insert(0, value)
        self.entry.config(fg=FG)

    def clear(self):
        self.entry.delete(0, "end")
        self.entry.insert(0, f"Search {self.key.upper()}...")
        self.entry.config(fg=MUTED)
        self._placeholder = True
        self._hide_results()


class App:
    def __init__(self, root):
        self.root = root
        root.title("Hardware Compute Visualizer")
        root.configure(bg=BG)
        root.minsize(900, 760)
        w, h = 940, min(1160, root.winfo_screenheight() - 80)
        root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{max(0, (root.winfo_screenheight() - h) // 2 - 20)}")

        self.control = None      # control.ControlServer while AI CONTROL is on
        self.link = None         # the editor link (VS Code): always on, found through app.json
        self.editor_busy = False  # True while an editor request is being done
        self._ai_queue = collections.deque()
        self._ai_busy = False
        messagebox.app = self
        self.local_hw = telemetry.detect_hardware()
        self.hw = self.local_hw  # the target machine's hardware (this PC, or the remote one)
        self.remote = None       # remote.RemoteTarget while a remote machine is the target (UC-2)
        self._connecting = None  # (target, result) while a connection is being made
        self._lhm_probed = time.monotonic()  # detect_hardware() just looked for LibreHardwareMonitor
        self.options = self._options_for(self.hw)
        self.selected = None
        self.selected_value = None
        self.gpu_index = 0
        self.disk_index = 0
        self.test_key = "2d"    # test of the current / last run (the radio can change afterwards)
        self.target_name = None
        self.replay = None      # the recorded run being played back, if any
        self.record = []        # samples of the current run, saved to RUNS_DIR when it ends
        self.peak_io = 0.0
        self.lost_since = None

        self.proc = None
        self.poller = None
        self.script = None
        self.running = False
        self.wl = {}            # latest workload-reported numbers
        self.wl_time = 0.0
        self.wl_error = None
        self.wl_got_data = False
        self.samples = {}       # key -> [values] recorded this run
        self.last_t = None
        self.peak_gbps = 0.0
        self.t_start = 0.0
        self.involved = ()
        self.target = None
        self.level = {k: 0.0 for k in ORDER}
        self.msg_unavail = set()
        self.last_tick = time.monotonic()
        self.dash_off = {l: 0.0 for l in LINKS}
        self._lit = {}

        outer = tk.Frame(root, bg=BG)
        outer.pack(expand=True, fill="both", padx=24, pady=16)
        self.outer = outer

        # --- title bar ---
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", pady=(0, 6))
        tk.Label(head, text="HARDWARE COMPUTE", bg=BG, fg=FG, font=(FONT, 20, "bold")).pack(side="left")
        tk.Label(head, text=" VISUALIZER", bg=BG, fg=ACCENT, font=(FONT, 20, "bold")).pack(side="left")
        self.ai_banner = tk.Label(head, text="", bg=BG, fg=AI_COLOR, font=(FONT, 12, "bold"))
        self.ai_banner.pack(side="left", padx=(18, 0))
        self.clock = tk.Label(head, text="IDLE", bg=BG, fg=MUTED, font=("Consolas", 11))
        self.clock.pack(side="right")
        tk.Frame(outer, bg=ACCENT, height=2).pack(fill="x", pady=(0, 8))

        # --- target machine (UC-2): this PC or a remote machine over SSH ---
        trow = tk.Frame(outer, bg=BG)
        trow.pack(fill="x", pady=(0, 8))
        tk.Label(trow, text="TARGET", bg=BG, fg=MUTED, font=(FONT, 10, "bold")).pack(side="left", padx=(0, 8))
        self.target_lbl = tk.Label(trow, text="", bg=BG, fg=FG, font=(FONT, 11, "bold"))
        self.target_lbl.pack(side="left")
        btn = dict(bg=BG, fg=FG, activebackground=LINE, activeforeground=ACCENT, relief="flat",
                   highlightthickness=1, highlightbackground=LINE, font=(FONT, 10, "bold"),
                   disabledforeground="#3a3a3a", padx=10)
        self.btn_remote = tk.Button(trow, text="REMOTE MACHINE…", command=self.ask_remote, **btn)
        self.btn_remote.pack(side="right", padx=(6, 0))
        self.btn_local = tk.Button(trow, text="THIS PC", command=self.use_local, **btn)
        self.btn_local.pack(side="right")
        self.btn_ai = tk.Button(trow, text="AI CONTROL: OFF", command=self.toggle_ai_control, **btn)
        self.btn_ai.config(fg=AI_COLOR, highlightbackground=AI_COLOR, activeforeground=AI_COLOR)
        self.btn_ai.pack(side="right", padx=(0, 18))

        self.canvas = tk.Canvas(outer, width=CANVAS_W, height=CANVAS_H, bg=BG, highlightthickness=0)
        self.canvas.pack(pady=(0, 6))
        self._draw_backdrop()
        self.shapes = {}
        self.bars = {}
        self.glows = {}
        self.graphs = {}
        self.graph_box = {}
        self.history = {}
        self.frame_n = 0
        self.links = {}
        for a, b in LINKS:
            self.links[(a, b)] = self._draw_trace(a, b)
        for key in ORDER:
            self._draw_part(key)

        self.combo_row = tk.Frame(outer, bg=BG)
        self.combo_row.pack(fill="x")
        for i in range(len(ORDER)):
            self.combo_row.columnconfigure(i, weight=1, uniform="col")
        self.combos = {}
        self._build_combos()
        for key in ORDER:
            if not self.options[key]:
                self._grey_out(key)

        # --- controls ---
        ctl = tk.Frame(outer, bg=BG)
        ctl.pack(pady=(18, 6))
        self.test_var = tk.StringVar(value="2d")
        self.radios = []
        for k, t in TESTS.items():
            r = tk.Radiobutton(ctl, text=t["short"], variable=self.test_var, value=k,
                               bg=BG, fg=FG, selectcolor=BG, activebackground=BG, indicatoron=False,
                               activeforeground=ACCENT, font=(FONT, 11, "bold"), relief="flat",
                               padx=10, pady=5, highlightthickness=1, highlightbackground=LINE,
                               overrelief="flat")
            r.pack(side="left", padx=3)
            self.radios.append(r)
        self.btn_start = tk.Button(ctl, text="START", width=9, command=self.start_test,
                                   bg=ACCENT, fg="black", activebackground=lighten(ACCENT, 0.3),
                                   relief="flat", font=(FONT, 12, "bold"), disabledforeground="#1a3a14")
        self.btn_start.pack(side="left", padx=(18, 6), ipady=2)
        self.btn_stop = tk.Button(ctl, text="STOP", width=9, command=lambda: self.stop_test("stopped by user"),
                                  bg=BG, fg="#ff3b3b", activebackground="#2a0b0b", activeforeground="#ff3b3b",
                                  relief="flat", state="disabled", highlightthickness=1,
                                  highlightbackground="#ff3b3b", font=(FONT, 12, "bold"),
                                  disabledforeground="#4a2020")
        self.btn_stop.pack(side="left", padx=6, ipady=2)

        # second row: the chosen test's option (AI task / program) on the left, recorded runs on the right
        optrow = tk.Frame(outer, bg=BG)
        optrow.pack(fill="x", pady=(2, 2), padx=8)
        runbtn = dict(bg=BG, fg=REPLAY_COLOR, activebackground="#2a2108", activeforeground=REPLAY_COLOR,
                      relief="flat", highlightthickness=1, highlightbackground=REPLAY_COLOR,
                      font=(FONT, 11, "bold"), disabledforeground="#4a3f20", width=9)
        self.btn_compare = tk.Button(optrow, text="COMPARE", command=self.open_compare, **runbtn)
        self.btn_compare.pack(side="right", padx=(6, 0))
        self.btn_replay = tk.Button(optrow, text="REPLAY", command=self.start_replay, **runbtn)
        self.btn_replay.pack(side="right", padx=6)

        # AI task picker (shown while AI BENCHMARK is the chosen test)
        self.goalrow = goalrow = tk.Frame(optrow, bg=BG)
        tk.Label(goalrow, text="AI TASK", bg=BG, fg=MUTED, font=(FONT, 10, "bold")).pack(side="left", padx=(0, 8))
        self.goal_ids = {title.upper(): gid for gid, (title, _) in agent.GOALS.items()}
        self.goal_var = tk.StringVar(value=next(iter(self.goal_ids)))
        self.goal_menu = tk.OptionMenu(goalrow, self.goal_var, *self.goal_ids)
        self.goal_menu.config(bg=PANEL, fg=FG, activebackground=LINE, activeforeground=ACCENT, relief="flat",
                              highlightthickness=1, highlightbackground=LINE, font=(FONT, 10, "bold"),
                              width=28, disabledforeground="#3a3a3a", bd=0)
        self.goal_menu["menu"].config(bg=PANEL, fg=FG, activebackground=ACCENT, activeforeground="black",
                                      font=(FONT, 10), bd=0)
        self.goal_menu.pack(side="left")

        # the user's own program (shown while YOUR CODE is the chosen test)
        self.coderow = coderow = tk.Frame(optrow, bg=BG)
        tk.Label(coderow, text="PROGRAM", bg=BG, fg=MUTED, font=(FONT, 10, "bold")).pack(side="left", padx=(0, 8))
        self.program_var = tk.StringVar()
        self.program_entry = tk.Entry(coderow, textvariable=self.program_var, width=44, bg=PANEL, fg=FG,
                                      insertbackground=FG, relief="flat", font=("Consolas", 10),
                                      highlightthickness=1, highlightbackground=LINE, highlightcolor=ACCENT,
                                      disabledbackground=PANEL)
        self.program_entry.pack(side="left", ipady=4)
        self.btn_browse = tk.Button(coderow, text="…", width=3, bg=PANEL, fg=FG, relief="flat",
                                    command=self._browse_program)
        self.btn_browse.pack(side="left", padx=(4, 0))
        self.hintrow = tk.Label(optrow, text="", bg=BG, fg=MUTED, font=(FONT, 10))
        self.test_var.trace_add("write", lambda *a: self._sync_controls())

        self.status = tk.Label(outer, text="CLICK A PART OR SEARCH TO SELECT IT", font=(FONT, 11),
                               bg=BG, fg=MUTED, wraplength=820)
        self.status.pack(pady=(4, 8))

        # --- live stats ---
        grid = tk.Frame(outer, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        grid.pack(fill="x", pady=(0, 10))
        grid.columnconfigure(3, weight=1, uniform="src")
        grid.columnconfigure(7, weight=1, uniform="src")
        self.stat_val = {}
        self.stat_src = {}
        per_col = (len(STATS) + 1) // 2  # two columns of metrics, filled top to bottom
        for n, (key, label, unit, part, src) in enumerate(STATS):
            r, c0 = n % per_col, 4 * (n // per_col)
            color = PARTS[part]["color"] if part in PARTS else "#b388ff"
            tk.Frame(grid, bg=color, width=3).grid(row=r, column=c0, sticky="ns", pady=1)
            tk.Label(grid, text=label.upper(), bg=PANEL, fg=color, font=(FONT, 10, "bold"),
                     anchor="w", width=19).grid(row=r, column=c0 + 1, sticky="w", padx=(8, 2), pady=1)
            v = tk.Label(grid, text="—", bg=PANEL, fg=FG, font=("Consolas", 11, "bold"), anchor="e", width=15)
            v.grid(row=r, column=c0 + 2, sticky="e")
            s = tk.Label(grid, text=src, bg=PANEL, fg=MUTED, font=(FONT, 9), anchor="w")
            s.grid(row=r, column=c0 + 3, sticky="w", padx=(8, 4))
            self.stat_val[key], self.stat_src[key] = v, s

        tk.Label(outer, text="LOG  -  AI AGENT STEPS / YOUR PROGRAM'S OUTPUT", bg=BG, fg="#b388ff",
                 font=(FONT, 10, "bold"), anchor="w").pack(fill="x")
        self.log = tk.Text(outer, height=7, bg=PANEL, fg=FG, font=("Consolas", 9), relief="flat",
                           state="disabled", wrap="word", highlightthickness=1, highlightbackground=LINE)
        self.log.pack(fill="x", pady=(0, 8))
        for tag, color in (("say", FG), ("tool", ACCENT), ("result", MUTED), ("blocked", "#ff5c5c"),
                           ("done", "#b388ff"), ("info", MUTED), ("ai", AI_COLOR), ("editor", EDITOR_COLOR)):
            self.log.tag_config(tag, foreground=color)
        self._log("AI BENCHMARK: pick AI BENCHMARK + a task + a part, then START. The agent's steps appear here.",
                  "info")
        self._log("YOUR CODE: type the command that starts your program (or pick a .py / .exe with …), "
                  "then START. Its output appears here; the summary says where it wasted the hardware.", "info")

        tk.Label(outer, text="LAST RUN SUMMARY", bg=BG, fg=ACCENT, font=(FONT, 10, "bold"),
                 anchor="w").pack(fill="x")
        self.summary = tk.Text(outer, height=8, bg=PANEL, fg=FG, font=("Consolas", 10),
                               relief="flat", state="disabled", highlightthickness=1,
                               highlightbackground=LINE)
        self.summary.pack(fill="both", expand=True)

        self.events = queue.Queue()  # agent events from the reader thread -> UI thread
        self.ai_steps = 0
        self.ai_blocked = 0
        self.ai_done_text = None
        self.ai_stop_text = None
        self.duration = TEST_SECONDS
        self.is_ai = False
        self.is_code = False
        self.program = None     # command line of the user's program in the current / last run
        self.code_exit = None
        self.fps = 60.0
        self.sample_times = collections.deque()  # arrival times of samples in the last second
        self.core_bars, self.core_lv, self._core_col = [], [], {}
        self._build_core_strip()
        self._clear_readout()
        self._sync_controls()

        self._show_target()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.tick()

    @staticmethod
    def _options_for(hw):
        """Search options per part from a detect_hardware() / remote agent report."""
        return {
            "ram": [hw["ram"]] if hw.get("ram") else [],
            "cpu": [hw["cpu"]] if hw.get("cpu") else [],
            "gpu": list(hw.get("gpus") or []),
            "board": [hw["board"]] if hw.get("board") else [],
            "disk": [d["label"] for d in hw.get("disks") or []],
        }

    def _build_combos(self):
        for i, key in enumerate(ORDER):
            c = SearchCombo(self.combo_row, self.outer, key, self.options[key], PARTS[key]["color"],
                            self._on_combo)
            c.frame.grid(row=0, column=i, padx=6, sticky="new")
            self.combos[key] = c

    def _machine_name(self):
        name = self.hw.get("machine") or "?"
        return f"{name} ({self.remote.label})" if self.remote else name

    def _show_target(self):
        if self.remote:
            self.target_lbl.config(text=f"REMOTE  {self._machine_name()}", fg=ACCENT)
        else:
            self.target_lbl.config(text=f"THIS PC  {self._machine_name()}", fg=FG)
        busy = self.running or self._connecting is not None
        self.btn_local.config(state="normal" if self.remote and not busy else "disabled")
        self.btn_remote.config(state="disabled" if busy else "normal")

    def _apply_hardware(self):
        """The target machine changed: rebuild the part list, search boxes and diagram."""
        self.options = self._options_for(self.hw)
        if self.selected is not None:
            self.canvas.itemconfig(self.shapes[self.selected][0], width=2)
        self.selected = self.selected_value = None
        self.gpu_index = self.disk_index = 0
        for c in self.combos.values():
            c.listbox.destroy()
            c.frame.destroy()
        self.combos = {}
        self._build_combos()
        for key in ORDER:
            if self.options[key]:
                self._light_up(key)
            else:
                self._grey_out(key)
        self._clear_readout()
        self._build_core_strip()
        self._show_target()

    def _build_core_strip(self):
        """One small bar per logical CPU inside the CPU box: a single lit bar among dark
        ones is serial code, all lit is parallel code. Many cores share bars (max 64)."""
        self.canvas.delete("cores")
        n = min(int(self.hw.get("threads") or 0), 64) if self.options["cpu"] else 0
        self.core_bars, self.core_lv, self._core_col = [], [0.0] * n, {}
        if not n:
            return
        x0, y0, x1, y1 = PARTS["cpu"]["rect"]
        bx0, bx1 = x0 + 16, x1 - 16
        self.core_box = (y1 - 58, y1 - 37)  # top, bottom of the bars
        top, bot = self.core_box
        w = (bx1 - bx0) / n
        gap = 1 if w > 3 else 0
        for i in range(n):
            xa, xb = bx0 + i * w + gap, bx0 + (i + 1) * w - gap
            self.canvas.create_rectangle(xa, top, xb, bot, fill="#121a20", outline="", tags=("part_cpu", "cores"))
            self.core_bars.append(self.canvas.create_rectangle(xa, bot, xb, bot, fill=PARTS["cpu"]["color"],
                                                               outline="", tags=("part_cpu", "cores")))

    def _browse_program(self):
        path = filedialog.askopenfilename(title="Your program", filetypes=[
            ("Programs and scripts", "*.py *.exe *.bat *.cmd"), ("All files", "*.*")])
        if path:
            self.program_var.set(f'"{os.path.normpath(path)}"')

    def open_compare(self):
        if not self.running:
            CompareWindow(self.root)

    # ------------------------------------------------------------ editor link (VS Code)
    def _open_link(self):
        """The editor link: a token-protected local API on a free port, published in app.json so the
        VS Code extension connects by itself. No banners or pauses: you started it from the editor."""
        try:
            self.link = control.ControlServer(port=0, token_file=None, actions=control.EDITOR_ACTIONS)
            control.publish_link(self.link)
        except OSError as e:
            self.link = None
            self._log(f"VS Code link unavailable: {e}", "info")

    def _drain_link(self):
        while True:
            try:
                action, args, reply = self.link.requests.get_nowait()
            except queue.Empty:
                return
            if action not in ("state", "summary"):
                desc, _ = self._ai_describe(action, args)
                self._log(f"💻 VS Code → {desc}", "editor")
            messagebox.notes = []
            self.editor_busy = True
            try:
                reply.put(self._ai_do(action, args))
            except Exception as e:  # report, never kill the UI loop
                reply.put((500, {"error": f"{type(e).__name__}: {e}"}))
            finally:
                self.editor_busy = False

    def _open_in_editor(self, file, line):
        """A line of the user's code in VS Code (vscode://file/...), else the file itself."""
        uri = "vscode://file/" + os.path.abspath(file).replace("\\", "/") + f":{line}"
        try:
            if sys.platform == "win32":
                os.startfile(uri)
            else:
                subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", uri])
        except OSError as e:
            self._log(f"Can't open {file}:{line} in VS Code: {e}", "blocked")

    # ------------------------------------------------------------ AI control (control.py)
    def toggle_ai_control(self):
        if self.control:
            self.control.close()
            self.control = None
            self.btn_ai.config(text="AI CONTROL: OFF", bg=BG, fg=AI_COLOR)
            self.ai_banner.config(text="")
            self._log("🤖 AI control is off.", "ai")
            return
        try:
            self.control = control.ControlServer()
        except OSError as e:
            messagebox.showerror("AI control", f"Can't open 127.0.0.1:{control.PORT}: {e}")
            return
        self.btn_ai.config(text="AI CONTROL: ON", bg=AI_COLOR, fg="black")
        self.ai_banner.config(text=f"🤖 AI CONTROL ON")
        self._log(f"🤖 AI control is on: 127.0.0.1:{control.PORT}, token in control_token.txt. "
                  "Everything the AI does shows here in purple.", "ai")

    def _flash(self, widget, times=3):
        """Blink a widget the AI is 'pressing' so the action is easy to see."""
        try:
            normal = widget.cget("bg")
        except tk.TclError:
            return

        def step(n):
            try:
                widget.config(bg=AI_COLOR if n % 2 == 0 else normal)
            except tk.TclError:
                return
            if n < 2 * times - 1:
                self.root.after(120, step, n + 1)
        step(0)

    def _drain_control(self):
        """AI requests -> UI thread. 'state' answers at once; every other action is shown
        first (banner, log, flashing control) and done a moment later, one at a time."""
        while True:
            try:
                item = self.control.requests.get_nowait()
            except queue.Empty:
                break
            if item[0] in ("state", "summary", "runs"):
                action, args, reply = item
                reply.put(self._ai_do(action, args))
            else:
                self._ai_queue.append(item)
        if self._ai_busy or not self._ai_queue:
            return
        action, args, reply = self._ai_queue.popleft()
        desc, widget = self._ai_describe(action, args)
        self._ai_busy = True
        self.ai_banner.config(text=f"🤖 AI: {desc}")
        self._log(f"🤖 AI → {desc}", "ai")
        if widget is not None:
            self._flash(widget)

        def run():
            try:
                reply.put(self._ai_do(action, args))
            except Exception as e:  # report, never kill the UI loop
                reply.put((500, {"error": f"{type(e).__name__}: {e}"}))
            finally:
                self._ai_busy = False
                if self.control:
                    self.root.after(2500, lambda: self.control and not self._ai_busy and
                                    self.ai_banner.config(text="🤖 AI CONTROL ON"))
        self.root.after(450, run)  # long enough to see what is about to happen

    def _ai_describe(self, action, args):
        tests = {k: t["short"] for k, t in TESTS.items()}
        if action == "select":
            part = str(args.get("part", "")).lower()
            w = self.combos[part].entry if part in self.combos else None
            return f"select {part.upper()}" + (f" ({args['name']})" if args.get("name") else ""), w
        if action == "test":
            key = self._find_test(args.get("test"))
            w = self.radios[list(TESTS).index(key)] if key else None
            return f"choose test {tests.get(key, args.get('test'))}", w
        if action == "program":
            return f"program: {args.get('command', '')}", self.program_entry
        if action == "ai_task":
            return f"AI task: {args.get('task', '')}", self.goal_menu
        if action == "start":
            return "press START" + (" (and confirm)" if args.get("confirm") else ""), self.btn_start
        if action == "stop":
            return "press STOP", self.btn_stop
        if action == "replay":
            return f"replay {args.get('run', '')}", self.btn_replay
        if action == "compare":
            return "open COMPARE", self.btn_compare
        if action == "remote":
            return f"connect to {args.get('user', '')}@{args.get('host', '')}", self.btn_remote
        if action == "local":
            return "switch to THIS PC", self.btn_local
        return action, None

    @staticmethod
    def _find_test(name):
        n = str(name or "").strip().lower()
        for k, t in TESTS.items():
            if n in (k, t["short"].lower(), t["label"].lower()):
                return k
        return None

    def _ai_state(self):
        readings = {}
        for key, label, unit, part, src in STATS:
            text = self.stat_val[key].cget("text")
            if text not in ("—", ""):
                readings[label] = text
        return {
            "target": self.remote.label if self.remote else "this PC", "machine": self.hw.get("machine"),
            "parts": self.options, "selected": {"part": self.selected, "name": self.selected_value},
            "test": self.test_var.get(), "tests": {k: t["label"] for k, t in TESTS.items()},
            "ai_task": self.goal_ids.get(self.goal_var.get()), "ai_tasks": {v: k for k, v in self.goal_ids.items()},
            "program": self.program_var.get(), "running": self.running, "replay": bool(self.replay),
            "elapsed_s": round(time.monotonic() - self.t_start, 1) if self.running else None,
            "status": self.status.cget("text"), "readings": readings,
            "connecting": self._connecting is not None,
        }

    def _ai_do(self, action, args):
        """One AI action on the UI thread -> (HTTP code, body)."""
        messagebox.notes = []
        ok = lambda **kw: (200, dict(kw, status=self.status.cget("text"), messages=messagebox.notes))  # noqa: E731
        bad = lambda msg, **kw: (400, dict(kw, error=msg, status=self.status.cget("text"),  # noqa: E731
                                           messages=messagebox.notes))
        if action == "state":
            return 200, self._ai_state()
        if action == "summary":
            return 200, {"summary": self.summary.get("1.0", "end").strip()}
        if action == "runs":
            out = []
            for n in sorted((n for n in os.listdir(RUNS_DIR) if n.endswith(".json")), reverse=True) \
                    if os.path.isdir(RUNS_DIR) else []:
                try:
                    with open(os.path.join(RUNS_DIR, n), "r", encoding="utf-8") as f:
                        r = json.load(f)
                except (OSError, ValueError):
                    continue
                out.append({"run": n, "when": r.get("when"), "machine": r.get("machine"),
                            "test": r.get("test_label"), "part": r.get("part"), "program": r.get("program"),
                            "result": r.get("result"), "duration_s": r.get("duration"),
                            "bottlenecks": [f"{100 * f['share']:.0f}% {f['title']}" for f in r.get("findings") or []]})
            return 200, {"runs": out}
        busy = self.running and action not in ("stop",)
        if busy:
            return bad("a test is running - stop it or wait for it first")
        if action == "select":
            part = str(args.get("part", "")).lower()
            part = next((k for k in PARTS if part in (k, PARTS[k]["label"].lower())), None)
            if not part:
                return bad(f"part must be one of {list(PARTS)}")
            if not self.options[part]:
                return bad(f"{part} is not detected on this machine")
            name = str(args.get("name") or "").lower()
            value = next((o for o in self.options[part] if name and name in o.lower()), None)
            if name and value is None:
                return bad(f"no {part} matching {args['name']!r}", choices=self.options[part])
            if value:
                self.combos[part].set_value(value)
                self._select(part, value)
            else:
                self._on_diagram_click(part)
            return ok(selected={"part": self.selected, "name": self.selected_value})
        if action == "test":
            key = self._find_test(args.get("test"))
            if not key:
                return bad("unknown test", tests={k: t["short"] for k, t in TESTS.items()})
            self.test_var.set(key)
            return ok(test=key)
        if action == "program":
            self.test_var.set("code")
            self.program_var.set(str(args.get("command", "")))
            return ok(program=self.program_var.get())
        if action == "ai_task":
            t = str(args.get("task", "")).lower()
            hit = next((title for title, gid in self.goal_ids.items() if t in (gid, title.lower())), None)
            if not hit:
                return bad("unknown AI task", tasks=list(self.goal_ids.values()))
            self.test_var.set("ai")
            self.goal_var.set(hit)
            return ok(ai_task=self.goal_ids[hit])
        if action == "start":
            messagebox.confirm = bool(args.get("confirm"))
            try:
                self.start_test()
            finally:
                messagebox.confirm = False
            if self.running:
                return ok(running=True)
            asked = [n for n in messagebox.notes if n["kind"] == "question"]
            if asked and not args.get("confirm"):
                return bad("needs confirmation - call start again with confirm=true to go ahead",
                           question=asked[-1]["text"])
            return bad("the test did not start")
        if action == "stop":
            if not self.running:
                return bad("nothing is running")
            self.stop_test("stopped from VS Code" if self.editor_busy else "stopped by AI")
            return ok(stopped=True)
        if action == "replay":
            path = os.path.join(RUNS_DIR, os.path.basename(str(args.get("run", ""))))
            if not os.path.isfile(path):
                return bad("no such run - see /runs")
            self.start_replay(path)
            return ok(running=self.running)
        if action == "compare":
            CompareWindow(self.root, args.get("a"), args.get("b"))
            return ok(opened=True)
        if action == "remote":
            host, user, key = (str(args.get(k) or "").strip() for k in ("host", "user", "key"))
            if not host or not user or not key:
                return bad("host, user and key (path to an SSH private key) are needed")
            if self._connecting:
                return bad("already connecting")
            self._connect(remote.RemoteTarget(host, user, key, int(args.get("port") or 22)))
            return ok(connecting=True, note="poll /state until connecting is false")
        if action == "local":
            self.use_local()
            return ok(target="this PC")
        return bad("unknown action")

    # ------------------------------------------------------------ remote target (UC-2)
    def _load_remote_cfg(self):
        try:
            with open(REMOTE_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg if isinstance(cfg, dict) else {}
        except (OSError, ValueError):
            return {}

    def ask_remote(self):
        if self.running or self._connecting:
            return
        cfg = self._load_remote_cfg()
        dlg = tk.Toplevel(self.root)
        dlg.title("Remote machine")
        dlg.configure(bg=BG)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        tk.Label(dlg, text="CONNECT TO A REMOTE MACHINE", bg=BG, fg=ACCENT, font=(FONT, 13, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(14, 2))
        tk.Label(dlg, text="Linux with Python 3.8+ and SSH key login (for example a rented cloud GPU machine).\n"
                           "Key login only: no password is asked for or stored. Tests and the AI benchmark's\n"
                           "model stay yours to start and pay for; this app never creates or stops machines.",
                 bg=BG, fg=MUTED, font=(FONT, 9), justify="left").grid(row=1, column=0, columnspan=3, sticky="w",
                                                                      padx=16, pady=(0, 10))
        default_key = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")
        fields = {}
        for r, (key, label, value) in enumerate((
                ("host", "HOST", cfg.get("host", "")),
                ("user", "USER", cfg.get("user", getpass.getuser())),
                ("port", "PORT", str(cfg.get("port", 22))),
                ("key", "KEY FILE", cfg.get("key", default_key if os.path.isfile(default_key) else ""))), start=2):
            tk.Label(dlg, text=label, bg=BG, fg=FG, font=(FONT, 10, "bold")).grid(row=r, column=0, sticky="w",
                                                                                 padx=(16, 8), pady=3)
            var = tk.StringVar(value=value)
            tk.Entry(dlg, textvariable=var, width=44, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                     font=(FONT, 10), highlightthickness=1, highlightbackground=LINE,
                     highlightcolor=ACCENT).grid(row=r, column=1, sticky="we", pady=3, ipady=3)
            fields[key] = var
        tk.Button(dlg, text="…", width=3, bg=PANEL, fg=FG, relief="flat",
                  command=lambda: fields["key"].set(filedialog.askopenfilename(
                      parent=dlg, title="SSH private key", initialdir=os.path.dirname(default_key))
                      or fields["key"].get())).grid(row=5, column=2, padx=(4, 16))
        err = tk.Label(dlg, text="", bg=BG, fg="#f87171", font=(FONT, 9))
        err.grid(row=6, column=0, columnspan=3, sticky="w", padx=16)

        def go(event=None):
            host, user = fields["host"].get().strip(), fields["user"].get().strip()
            key = fields["key"].get().strip()
            try:
                port = int(fields["port"].get().strip() or 22)
            except ValueError:
                port = 0
            if not host or not user or not 0 < port < 65536 or not key:
                err.config(text="Host, user, a port number and a key file are all needed.")
                return
            try:
                with open(REMOTE_FILE, "w", encoding="utf-8") as f:
                    json.dump({"host": host, "user": user, "port": port, "key": key}, f)
            except OSError:
                pass
            dlg.destroy()
            self._connect(remote.RemoteTarget(host, user, key, port))

        bar = tk.Frame(dlg, bg=BG)
        bar.grid(row=7, column=0, columnspan=3, sticky="e", padx=16, pady=(8, 14))
        tk.Button(bar, text="CONNECT", width=10, command=go, bg=ACCENT, fg="black", relief="flat",
                  font=(FONT, 11, "bold")).pack(side="left", padx=4)
        tk.Button(bar, text="CANCEL", width=10, command=dlg.destroy, bg=BG, fg=FG, relief="flat",
                  highlightthickness=1, highlightbackground=LINE, font=(FONT, 11, "bold")).pack(side="left")
        dlg.bind("<Return>", go)
        dlg.bind("<Escape>", lambda e: dlg.destroy())
        dlg.grab_set()

    def _connect(self, target):
        """Connect on a thread (it can take seconds); _poll_connect finishes on the UI thread."""
        self._connecting = (target, None)
        self._show_target()
        self._set_status(f"Connecting to {target.host} over SSH…", ACCENT)

        def work():
            try:
                result = target.connect()
            except remote.RemoteError as e:
                result = e
            except Exception as e:  # never leave the UI waiting
                result = remote.RemoteError(f"{type(e).__name__}: {e}")
            self._connecting = (target, result)
        threading.Thread(target=work, daemon=True).start()
        self.root.after(100, self._poll_connect)

    def _poll_connect(self):
        target, result = self._connecting
        if result is None:
            self.root.after(100, self._poll_connect)
            return
        self._connecting = None
        if isinstance(result, Exception):  # UC-2 alt a/b: stay on this PC
            self._show_target()
            self._set_status(f"Not connected: {result}", "#f87171")
            messagebox.showerror("Can't connect", str(result))
            return
        if not result.get("nvml") and not messagebox.askokcancel(
                "No NVIDIA driver",
                f"{target.host} has no working NVIDIA driver ({result.get('nvml_error') or 'NVML not found'}). "
                "GPU and board readings will be unavailable there. Stay connected?"):
            target.close()
            self._show_target()
            self._set_status("Not connected: stayed on this PC.")
            return
        if self.remote:
            self.remote.close()
        self.remote, self.hw = target, result
        self._apply_hardware()
        self._set_status(f"Connected: {target.host}", ACCENT)

    def use_local(self):
        """UC-2 alt d: back to this PC - stop any remote test, stop and remove the agent."""
        if not self.remote:
            return
        if self.running:
            self.stop_test("stopped: switched to this PC")
        self.remote.close()
        self.remote, self.hw = None, self.local_hw
        self._apply_hardware()
        self._set_status("Target: this PC. The remote agent was stopped and removed.")

    def _on_remote_lost(self):
        reason = self.remote.lost_reason or f"Connection to {self.remote.host} lost."
        self.remote.close()
        self.remote, self.hw = None, self.local_hw
        self._apply_hardware()
        self._set_status(f"{reason} Back on this PC (the agent there stops its test by itself within 10 s).",
                         "#f87171")
        messagebox.showwarning("Remote machine lost", f"{reason}\n\nThe target is this PC again.")

    # ------------------------------------------------------------ diagram
    def _draw_backdrop(self):
        c = self.canvas
        for x in range(0, CANVAS_W + 1, 22):
            c.create_line(x, 0, x, CANVAS_H, fill="#0e0e0e")
        for y in range(0, CANVAS_H + 1, 22):
            c.create_line(0, y, CANVAS_W, y, fill="#0e0e0e")
        c.create_line(0, 1, CANVAS_W, 1, fill=LINE)
        c.create_line(0, CANVAS_H - 1, CANVAS_W, CANVAS_H - 1, fill=LINE)

    def _trace_points(self, a, b):
        """Polyline for the circuit trace joining part a to part b."""
        ra, rb = PARTS[a]["rect"], PARTS[b]["rect"]
        span = ORDER.index(b) - ORDER.index(a)
        if span > 1:  # bus routed under the boxes in between; longer buses run deeper
            shift = (span - 2) * 22 - 11
            xa, xb = (ra[0] + ra[2]) / 2 - shift, (rb[0] + rb[2]) / 2 + shift
            y = ra[3] + 14 + (span - 2) * 18
            return [xa, ra[3], xa, y, xa + 8, y + 8, xb - 8, y + 8, xb, y, xb, rb[3]]
        y = (ra[1] + ra[3]) / 2
        return [ra[2], y - 22, ra[2] + 14, y - 22, ra[2] + 22, y - 30, rb[0] - 22, y - 30,
                rb[0] - 14, y - 22, rb[0], y - 22]

    def _draw_trace(self, a, b):
        pts = self._trace_points(a, b)
        # dim static copper underneath, animated dashes on top
        self.canvas.create_line(*pts, fill="#161616", width=5)
        item = self.canvas.create_line(*pts, fill=GREY, width=2, dash=(8, 6))
        for x, y in (pts[0:2], pts[-2:]):
            self.canvas.create_rectangle(x - 3, y - 3, x + 3, y + 3, fill=GREY, outline="")
        return item

    def _draw_part(self, key):
        p = PARTS[key]
        x0, y0, x1, y1 = p["rect"]
        color = p["color"]
        tag = f"part_{key}"
        # glow halos (outline-only, recolored from live utilization), outermost first
        halos = []
        for pad in (7, 4):
            halos.append(chamfer(self.canvas, x0 - pad, y0 - pad, x1 + pad, y1 + pad, c=14 + pad,
                                 fill="", outline=BG, width=1, tags=tag))
        rect = chamfer(self.canvas, x0, y0, x1, y1, c=14, fill=PANEL, outline=mix(color, BG, 0.5),
                       width=2, tags=tag)
        # scanlines + top ticks for texture
        for y in range(y0 + 6, y1, 6):
            self.canvas.create_line(x0 + 3, y, x1 - 3, y, fill="#131313", tags=tag)
        for i in range(10):
            x = x0 + 24 + i * 7
            self.canvas.create_line(x, y0 + 2, x, y0 + (7 if i % 3 == 0 else 4), fill=mix(color, BG, 0.4), tags=tag)
        # corner brackets (bottom-left / top-right)
        for (cx, cy, dx, dy) in ((x0, y1, 1, -1), (x1, y0, -1, 1)):
            self.canvas.create_line(cx, cy + dy * 14, cx, cy, cx + dx * 14, cy, fill=color, width=2, tags=tag)
        text = self.canvas.create_text(x0 + 18, y0 + 28, text=p["label"], anchor="w", fill=color,
                                       font=(FONT, 24, "bold"), tags=tag)
        # scrolling history graph (behind the big number): guide lines + the trace itself
        # the CPU box keeps room under its graph for the per-core bars
        gx0, gx1, gy0, gy1 = x0 + 16, x1 - 16, y0 + 56, y1 - (64 if key == "cpu" else 46)
        for frac in (0.0, 0.5, 1.0):
            gy = gy1 - (gy1 - gy0) * frac
            self.canvas.create_line(gx0, gy, gx1, gy, fill="#1c1c1c", dash=(2, 4), tags=tag)
        graph = self.canvas.create_line(gx0, gy1, gx1, gy1, fill=mix(color, BG, 0.4), width=2, tags=tag)
        self.graph_box[key] = (gx0, gx1, gy0, gy1)
        self.history[key] = collections.deque([0.0] * HISTORY, maxlen=HISTORY)
        self.graphs[key] = graph
        sub = self.canvas.create_text((x0 + x1) / 2, (gy0 + gy1) / 2, text="", fill=FG,
                                      font=(FONT, 36, "bold"), tags=tag)
        # segmented meter
        bx0, bx1, by = x0 + 16, x1 - 16, y1 - 30
        seg_w = (bx1 - bx0) / SEGMENTS
        segs = [self.canvas.create_rectangle(bx0 + i * seg_w + 1, by, bx0 + (i + 1) * seg_w - 1, by + 9,
                                             fill="#1a1a1a", outline="", tags=tag)
                for i in range(SEGMENTS)]
        self.shapes[key] = (rect, text, sub)
        self.bars[key] = segs
        self.glows[key] = halos
        self.canvas.tag_bind(tag, "<Button-1>", lambda e, k=key: self._on_diagram_click(k))
        self.canvas.tag_bind(tag, "<Enter>", lambda e: self.canvas.config(cursor="hand2"))
        self.canvas.tag_bind(tag, "<Leave>", lambda e: self.canvas.config(cursor=""))

    def _grey_out(self, key):
        rect, text, sub = self.shapes[key]
        self.canvas.itemconfig(rect, outline=GREY, fill=PANEL, width=2)
        self.canvas.itemconfig(sub, text="NOT DETECTED", font=(FONT, 13, "bold"), fill=MUTED)
        self.canvas.itemconfig(text, fill=MUTED)
        for seg in self.bars[key]:
            self.canvas.itemconfig(seg, fill="#1a1a1a")
        self._lit[key] = 0
        for halo in self.glows[key]:
            self.canvas.itemconfig(halo, outline=BG)

    def _light_up(self, key):
        rect, text, sub = self.shapes[key]
        self.canvas.itemconfig(text, fill=PARTS[key]["color"])
        self.canvas.itemconfig(sub, text="", font=(FONT, 36, "bold"), fill=FG)

    def _on_diagram_click(self, key):
        if not self.options[key]:
            return
        value = self.selected_value if key == self.selected else self.options[key][0]
        self.combos[key].set_value(value)
        self._select(key, value)

    def _on_combo(self, key, value):
        self._select(key, value)

    def _select(self, key, value):
        if key == self.selected and value == self.selected_value:
            return  # UC-4 alt: reselect same part, nothing changes
        if self.running:
            self.stop_test("stopped: selection changed")  # FR-4
        if self.selected is not None and self.selected != key:
            self.canvas.itemconfig(self.shapes[self.selected][0], width=2)
            self.combos[self.selected].clear()
        self.selected, self.selected_value = key, value
        if key == "gpu":
            self.gpu_index = self.options["gpu"].index(value)
        elif key == "disk":
            self.disk_index = self.options["disk"].index(value)
        self.canvas.itemconfig(self.shapes[key][0], width=3)  # colour is set per frame in _animate
        self._set_status(f"Selected: {PARTS[key]['label']} — {value}")

    def _set_status(self, text, color=FG):
        self.status.config(text=text, fg=color)

    # ------------------------------------------------------------ run control
    def start_test(self):
        if self.running:
            return
        if self.selected is None:
            self._fail("Select a component first (click a part or search for it).")
            return
        test = TESTS[self.test_var.get()]
        script = test["targets"].get(self.selected)
        if script is None:
            ok = ", ".join(PARTS[p]["label"] for p in test["targets"])
            self._fail(f"The {test['label']} can't target {PARTS[self.selected]['label']}. "
                       f"It works on: {ok}.")
            return
        if test.get("local_only") and self.remote:
            self._fail(f"The {test['label']} runs on this PC only (its AI model is the local Ollama). "
                       "Switch the target to THIS PC.")
            return
        gpus = self.hw.get("gpu_info") or []
        gpu_sel = self.gpu_index if gpus else None
        if self.selected == "board":
            # the board's readings are the PCIe link of an NVIDIA GPU: use the chosen GPU if it is one
            nv = [i for i, g in enumerate(gpus) if g["kind"] == "nvml"]
            if not nv:
                self._fail("The board's readings come from the PCIe link of an NVIDIA GPU (NVML), and this "
                           "machine has none - there would be nothing to show.")
                return
            gpu_sel = gpu_sel if gpu_sel in nv else nv[0]
        gpu = gpus[gpu_sel] if gpu_sel is not None else None
        if script == "mlgpu" and (not gpu or gpu["kind"] != "nvml"):
            self._fail("The ML test on a GPU needs an NVIDIA GPU (it trains with CUDA). "
                       "Pick an NVIDIA GPU, or run the ML test on the CPU.")
            return
        # UC-14 alt a: warn before the test when the target's temperature can't be read
        if self.selected == "cpu" and not self.hw.get("cpu_temp") and not self.remote \
                and time.monotonic() - self._lhm_probed > 30:
            self._lhm_probed = time.monotonic()  # LibreHardwareMonitor may have been started after the app
            self.hw["cpu_temp"] = telemetry.probe_lhm() is not None
        if self.selected == "cpu" and not self.hw.get("cpu_temp"):
            why = ("the machine doesn't expose a CPU sensor - usual on cloud VMs" if self.remote else
                   "on Windows it needs LibreHardwareMonitor running as administrator")
            if not messagebox.askokcancel(
                    "No CPU temperature",
                    f"CPU temperature isn't readable ({why}), so there is no thermal safety abort "
                    "for the CPU. Continue?"):
                return
        if self.selected == "gpu" and gpu and gpu["kind"] == "pdh" and not telemetry.LHM and not \
                messagebox.askokcancel(
                    "No GPU temperature",
                    "This GPU's temperature isn't readable (for AMD / Intel GPUs it needs LibreHardwareMonitor "
                    "running as administrator), so there is no thermal safety abort for it. Continue?"):
            return

        is_ai, is_code = script == "agent", script == "usercode"
        command = self.program_var.get().strip() if is_code else None
        if is_code and not command:
            self._fail("Type the command that starts your program in PROGRAM (or pick a file with …)."
                       + (" On a remote machine it runs in your home folder there." if self.remote else ""))
            return
        cmd = None
        if is_ai:
            # UC-7 precondition: Ollama up on localhost with a supported model installed
            installed = agent.ollama_models()
            if installed is None:
                self._fail("Ollama isn't running. Start it (open the Ollama app, or run 'ollama serve') "
                           "and press Start again.")
                return
            model = agent.pick_model(installed)
            if model is None:
                self._fail(f"No supported model is installed. Run:  ollama pull {agent.MODELS[0]}")
                return
            workdir = os.path.join(AI_DIR, datetime.datetime.now().strftime("run-%Y%m%d-%H%M%S"))
            try:
                os.makedirs(workdir)
            except OSError as e:
                self._fail(f"Can't create the agent's working folder: {e}")
                return
            self.duration = test.get("seconds", TEST_SECONDS)
            cmd = [os.path.join(HERE, "agent.py"), workdir, model,
                   self.goal_ids[self.goal_var.get()], str(self.duration)]
        elif not self.remote:
            self.duration = test.get("seconds", TEST_SECONDS)
            cmd = [os.path.join(HERE, "workloads.py"), script, str(self.duration)]
            if script == "disk":
                folder = self._disk_test_folder()
                if folder is None:
                    self._fail("This disk has no drive letter, so there is nowhere to put the test file.")
                    return
                cmd.append(folder)
            elif is_code:
                cmd.append(command)

        self.script = script
        self.is_ai, self.is_code = is_ai, is_code
        self.program, self.code_exit = command, None
        self.test_key = self.test_var.get()
        self.target = self.selected
        self.target_name = self.selected_value
        involved = dict.fromkeys((self.target,) + OBSERVED[script])
        self.involved = tuple(p for p in involved if not (is_ai or is_code) or self.options[p])
        self.wl, self.wl_time, self.wl_error, self.wl_got_data = {}, 0.0, None, False
        self.samples = {}
        self.record = []
        self.last_t = None
        self.peak_gbps = 0.0
        self.peak_io = 0.0
        self.lost_since = None
        self.msg_unavail = set()
        self.ai_steps = self.ai_blocked = 0
        self.ai_done_text = self.ai_stop_text = None
        while not self.events.empty():
            self.events.get_nowait()
        if is_ai:
            self._log("", None, clear=True)
            self._log(f"Working folder: {workdir}", "info")
        elif is_code:
            self._log("", None, clear=True)
            where = f" on {self.remote.host} (in your home folder)" if self.remote else ""
            self._log(f"Running{where}: {command}", "info")
        self.sample_times.clear()

        disks = self.hw.get("disks") or []
        disk = disks[self.disk_index] if disks else None
        try:
            if self.remote:
                self.duration = test.get("seconds", TEST_SECONDS)
                self.proc = self.remote.start(script, self.duration, disk=disk["id"] if disk else None,
                                              command=command)
            else:
                env = None
                if gpu and gpu["kind"] == "nvml" and not (is_ai or is_code):  # the GPU being watched
                    env = dict(os.environ, CUDA_DEVICE_ORDER="PCI_BUS_ID", CUDA_VISIBLE_DEVICES=str(gpu["index"]))
                self.proc = subprocess.Popen(
                    [sys.executable] + cmd, env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError as e:
            self._fail(f"Workload failed to launch: {e}")
            return
        threading.Thread(target=self._read_proc, args=(self.proc,), daemon=True).start()

        if self.remote:
            self.poller = self.remote.poller(gpu["index"] if gpu else None, disk["id"] if disk else None)
        else:
            self.poller = telemetry.Poller(gpu, disk)
        self.poller.start()
        self.running = True
        self.t_start = time.monotonic()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._sync_controls()
        self._set_summary("")
        self._show_target()

    def _disk_test_folder(self):
        """A writable folder on the selected disk for the I/O test file (None = no drive letter)."""
        letters = self.hw["disks"][self.disk_index]["letters"]
        tmp = tempfile.gettempdir()
        if tmp[:1].upper() in letters:
            return tmp  # the system drive's root usually isn't writable without admin
        return f"{letters[0]}:\\" if letters else None

    # ------------------------------------------------------------ recorded runs
    def start_replay(self, path=None):
        """Play a recorded run back through the diagram and readout, clearly marked as a replay."""
        if self.running:
            return
        os.makedirs(RUNS_DIR, exist_ok=True)
        path = path or filedialog.askopenfilename(title="Replay a recorded run", initialdir=RUNS_DIR,
                                                  filetypes=[("Recorded run", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                run = json.load(f)
            samples = run["samples"]
            if not samples or run["part"] not in PARTS or not all("t" in s for s in samples):
                raise ValueError("no samples in it")
        except (OSError, ValueError, KeyError, TypeError) as e:
            self._fail(f"Can't replay {os.path.basename(path)}: {e}")
            return
        self.replay = run
        self.script, self.is_ai, self.is_code = run.get("script"), False, False
        self.target = run["part"]
        self.involved = tuple(p for p in run.get("involved", [run["part"]]) if p in PARTS)
        self.duration = samples[-1]["t"]
        self.wl, self.last_t, self.peak_gbps, self.peak_io = {}, None, 0.0, 0.0
        self.sample_times.clear()
        self.proc = None
        self.poller = Replay(samples)
        self.poller.start()
        self.running = True
        self.t_start = time.monotonic()
        self.btn_start.config(state="disabled")
        self.btn_stop.config(state="normal")
        self._sync_controls()
        self._set_summary(f"REPLAY - recorded {run.get('when', '?')} on {run.get('part_name', '?')}\n"
                          + run.get("summary", ""))

    def _save_run(self, result, duration, summary, findings):
        """Write this run's samples to RUNS_DIR; returns the file name (None if it couldn't be saved)."""
        if not self.record:
            return None
        now = datetime.datetime.now()
        name = f"run-{now:%Y%m%d-%H%M%S}-{self.test_key}-{self.target}.json"
        run = {"version": 2, "when": f"{now:%Y-%m-%d %H:%M:%S}", "machine": self._machine_name(),
               "test": self.test_key,
               "test_label": TESTS[self.test_key]["label"], "script": self.script,
               "part": self.target, "part_name": self.target_name, "involved": list(self.involved),
               "hardware": self.options, "result": result, "duration": round(duration, 2),
               "program": self.program, "threads": self.hw.get("threads"),
               # wall clock at t=0 of the samples: lines them up with a profile of the same run
               "started_epoch": time.time() - (time.monotonic() - self.t_start),
               "averages": {k: sum(v) / len(v) for k, v in self.samples.items() if v},
               "findings": findings, "summary": summary, "samples": self.record}
        try:
            os.makedirs(RUNS_DIR, exist_ok=True)
            with open(os.path.join(RUNS_DIR, name), "w", encoding="utf-8") as f:
                json.dump(run, f)
        except OSError:
            return None
        self._link_profile(run, name)
        return name

    def _link_profile(self, run, name):
        """YOUR CODE that ran the line profiler (VS Code: Profile in the Hardware Visualizer): this
        recording and its bottlenecks go into the profile too, so the editor shows them per line."""
        import profiler
        out = profiler.profile_of_command(self.program) if self.test_key == "code" and not self.remote else None
        if not out or not os.path.isfile(out):
            return
        try:
            if profiler.link_visualizer_run(out, run, name):
                self._log(f"Linked this recording to the code profile {out}", "info")
                self._code_links = _code_lines(out)
        except Exception as e:  # the run itself is saved either way
            self._log(f"Couldn't link the recording to {out}: {e}", "blocked")

    def _previous_run(self):
        """Newest saved run of the same test on the same part and hardware, or None."""
        try:
            names = sorted((n for n in os.listdir(RUNS_DIR)
                            if n.endswith(f"-{self.test_key}-{self.target}.json")), reverse=True)
        except OSError:
            return None
        for n in names:
            try:
                with open(os.path.join(RUNS_DIR, n), "r", encoding="utf-8") as f:
                    run = json.load(f)
            except (OSError, ValueError):
                continue
            machine = run.get("machine", self.local_hw.get("machine"))  # v1 runs were all on this PC
            if (run.get("part_name") == self.target_name and machine == self._machine_name()
                    and run.get("program") == self.program and run.get("averages")):
                return run
        return None

    def _sync_controls(self):
        """Radios lock while a test runs; the AI task menu is live only for an idle AI selection."""
        running = getattr(self, "running", False)
        for b in (self.btn_replay, self.btn_compare):
            b.config(state="disabled" if running else "normal")
        for r in self.radios:
            r.config(state="disabled" if running else "normal")
        test = self.test_var.get()
        for row in (self.goalrow, self.coderow, self.hintrow):
            row.pack_forget()
        if test == "ai":
            self.goalrow.pack(side="left")
        elif test == "code":
            self.coderow.pack(side="left")
        else:
            self.hintrow.config(text=f"{TESTS[test]['label']} - {TEST_SECONDS} s on the selected part")
            self.hintrow.pack(side="left")
        self.goal_menu.config(state="disabled" if running else "normal")
        self.program_entry.config(state="disabled" if running else "normal")
        self.btn_browse.config(state="disabled" if running or self.remote else "normal")

    def _log(self, text, tag, clear=False):
        self.log.config(state="normal")
        if clear:
            self.log.delete("1.0", "end")
        if text:
            self.log.insert("end", text + "\n", tag)
            self.log.see("end")
        self.log.config(state="disabled")

    def _drain_events(self):
        """Agent events -> log (UI thread only)."""
        while True:
            try:
                d = self.events.get_nowait()
            except queue.Empty:
                return
            ev, text = d.get("ev"), str(d.get("text", ""))
            if ev == "tool":
                self.ai_steps += 1
                self._log(f"▶ {text}", "tool")
            elif ev == "blocked":
                self.ai_blocked += 1
                self._log(f"⛔ BLOCKED: {text}", "blocked")
            elif ev == "done":
                self.ai_done_text = text
                self._log(f"✔ {text}", "done")
            elif ev == "timeout":
                self._log(f"⏱ {text}", "blocked")
            elif ev == "stopped":  # quit without calling finish: not a completed task
                self.ai_stop_text = text
                self._log(f"■ {text}", "blocked")
            elif ev == "result":
                self._log("  " + text.replace("\n", "\n  "), "result")
            elif ev == "out":
                self._log(text, "say")
            elif ev == "exit":
                self.code_exit = text
                self._log(f"■ {text}", "done")
            else:
                self._log(text, "say")

    def _read_proc(self, proc):
        for line in proc.stdout:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if "ev" in d:
                self.events.put(d)
            elif "error" in d:
                self.wl_error = d["error"]
            else:
                self.wl = d
                self.wl_time = time.monotonic()
                self.wl_got_data = True

    def _kill_tree(self):
        """Stop the workload; returns True if it had to be force-terminated (FR-10)."""
        p = self.proc
        if p is None:
            return False
        if isinstance(p, remote.RemoteProc):
            return p.terminate()
        try:
            parent = psutil.Process(p.pid)
            procs = parent.children(recursive=True) + [parent]
        except psutil.Error:
            return False
        for q in procs:
            try:
                q.terminate()
            except psutil.Error:
                pass
        _, alive = psutil.wait_procs(procs, timeout=2)
        for q in alive:
            try:
                q.kill()
            except psutil.Error:
                pass
        return bool(alive)

    def stop_test(self, result, summary=True):
        if not self.running:
            return
        self.running = False
        forced = self._kill_tree()
        if forced:
            result += " (force-stopped)"
        self.proc = None
        if self.poller:
            self.poller.stop()
        duration = time.monotonic() - self.t_start
        self.btn_start.config(state="normal")
        self.btn_stop.config(state="disabled")
        self._sync_controls()
        self._show_target()
        self._drain_events()
        if self.replay:
            self.replay = None
            self._set_status(f"Replay ended: {result}", REPLAY_COLOR)
        elif summary:
            self._show_summary(result, duration)
            self._set_status(f"Test ended: {result}")
        self._clear_readout()

    def _fail(self, msg):
        """UC-13: nothing is shown as running, no telemetry left on screen."""
        messagebox.showerror("Can't start test", msg)
        self._set_status(msg, "#f87171")

    def _on_close(self):
        if self.control:
            self.control.close()
            self.control = None
        if self.link:
            control.unpublish_link()
            self.link.close()
            self.link = None
        self.stop_test("closed", summary=False)
        if self.remote:
            self.remote.close()
        self.root.destroy()

    # ------------------------------------------------------------ per-frame loop
    def tick(self):
        now = time.monotonic()
        raw_dt = now - self.last_tick
        dt = min(0.1, raw_dt)
        self.last_tick = now
        if raw_dt > 0:
            self.fps += (1.0 / raw_dt - self.fps) * 0.05
        try:
            if self.link:
                self._drain_link()
            if self.control:
                self._drain_control()
            if self.running:
                self._tick_running(now)
            elif self.remote and not self.remote.alive:
                self._on_remote_lost()
            self._animate(dt)
            if self.running and self.replay:
                self.clock.config(text=f"▶ REPLAY (recorded)  {now - self.t_start:4.1f}s", fg=REPLAY_COLOR)
            elif self.running:
                blink = int((now - self.t_start) * 2) % 2 == 0
                while self.sample_times and now - self.sample_times[0] > 1.0:
                    self.sample_times.popleft()
                # FR-12 / NFR-1 shown live: drawing rate and sensor samples per second
                self.clock.config(text=f"{'●' if blink else '○'} LIVE  {now - self.t_start:5.1f}s   "
                                       f"{self.fps:3.0f} FPS · {len(self.sample_times):2d} Hz", fg="#ff3b3b")
            else:
                self.clock.config(text="IDLE", fg=MUTED)
        except Exception as e:  # never let the loop die (FR-17)
            self._set_status(f"Internal error: {e}", "#f87171")
        # aim at the next 1/60 s boundary (a fixed 16 ms wait drifts below 60 FPS on Windows)
        self.root.after(max(1, int(1000 * (1 / 60 - (time.monotonic() - now)))), self.tick)

    def _wl_fresh(self, now):
        return self.wl_got_data and now - self.wl_time <= STALE_S

    def _tick_running(self, now):
        elapsed = now - self.t_start
        if self.replay:
            self._tick_replay(elapsed)
            return
        p = self.proc
        self._drain_events()
        if self.is_ai and elapsed >= self.duration:
            self.stop_test("time limit reached")  # UC-7 alt b
            return
        if self.is_code and elapsed >= self.duration + 5:  # the runner stops it at the limit itself
            self.stop_test("time limit reached")
            return
        if p.poll() is not None:
            # process ended on its own: failure to start (UC-13) or completion (UC-9)
            time.sleep(0.05)  # let the reader thread drain the last lines
            self._drain_events()
            label = TESTS[self.test_key]["label"]
            if self.is_ai:
                if self.wl_error and not self.ai_steps:
                    self.stop_test("failed", summary=False)
                    self._fail(f"{label} failed to start: {self.wl_error}")
                elif self.wl_error:
                    self.stop_test(f"failed — {self.wl_error}")
                elif self.ai_done_text is None:
                    self.stop_test("time limit reached" if elapsed >= self.duration - 2 else
                                   f"agent stopped without finishing ({self.ai_stop_text or 'no reason given'})")
                else:
                    self.stop_test("completed")
            elif not self.wl_got_data:
                err = self.wl_error or f"workload exited with code {p.returncode}"
                self.stop_test("failed", summary=False)
                self._fail(f"{label} failed to start: {err}")
            elif self.wl_error:
                self.stop_test(f"failed — {self.wl_error}")
            elif self.is_code:
                self.stop_test(self.code_exit or "program ended")
            else:
                self.stop_test("completed")
            return

        where = f" on {self.remote.host}" if self.remote else ""
        if self.is_code:
            self._set_status(f"Running your program{where} — {elapsed:.0f} s (stops at {self.duration:.0f} s)")
        else:
            self._set_status(f"Running {TESTS[self.test_key]['label']} on "
                             f"{PARTS[self.target]['label']}{where} — {elapsed:.0f}/{self.duration:.0f} s")

        s = self.poller.latest
        if not s or s["t"] == self.last_t:
            return
        self.last_t = s["t"]
        self.sample_times.append(now)

        merged = dict(s)
        fresh = self._wl_fresh(now)
        if fresh:
            merged.update(self.wl)
            if "gbps" in self.wl:
                self.peak_gbps = max(self.peak_gbps, self.wl["gbps"])
        row = {"t": round(elapsed, 3)}
        for k, v in merged.items():
            if v is not None and k != "t":
                if isinstance(v, (int, float)):
                    self.samples.setdefault(k, []).append(v)
                row[k] = round(v, 4) if isinstance(v, float) else v
        self.record.append(row)

        self._update_readout(merged, fresh)
        self._check_thermal(merged)

    def _tick_replay(self, elapsed):
        """One frame of a replay: show the recorded sample for this moment. Nothing is measured,
        recorded or safety-checked - the clock and status line say it is a recording."""
        run = self.replay
        if elapsed >= self.duration + 0.3:
            self.stop_test("finished")
            return
        self._set_status(f"REPLAY (recorded {run.get('when', '?')}, not live) — "
                         f"{run.get('test_label', '?')} on {PARTS[self.target]['label']} — "
                         f"{min(elapsed, self.duration):.0f}/{self.duration:.0f} s", REPLAY_COLOR)
        s = self.poller.latest
        if s["t"] == self.last_t:
            return
        self.last_t = s["t"]
        self.sample_times.append(time.monotonic())
        self.wl = {k: s[k] for k in WL_KEYS if k in s}
        self.peak_gbps = max(self.peak_gbps, s.get("gbps") or 0.0)
        self._update_readout(s, True)

    def _src(self, key, default):
        """Where a metric comes from on the current target (the poller may name another source)."""
        p = self.poller
        src = (getattr(p, "sources", None) or {}).get(key, default)
        link = (getattr(p, "info", None) or {}).get("pcie_link")
        if key == "board_util" and link and src == default:
            src = f"{src} · {link}"
        elif self.remote and src in ("psutil", "Windows perf counter"):
            src = "remote agent"
        return src

    def _update_readout(self, m, wl_fresh):
        unavail = set()
        for key, label, unit, part, src in STATS:
            src = self._src(key, src)
            v = m.get(key)
            relevant = part == "wl" or part in self.involved
            if not relevant:
                self.stat_val[key].config(text="—", fg=MUTED)
                self.stat_src[key].config(text=src, fg=MUTED)
            elif v is None and part == "wl":
                self.stat_val[key].config(text="—", fg=MUTED)
                self.stat_src[key].config(text="not in this test", fg=MUTED)
            elif v is None:
                reason = src
                self.stat_val[key].config(text="unavailable", fg="#f87171")
                self.stat_src[key].config(text=reason, fg="#f87171")
                if part in PARTS:
                    unavail.add(key)
            else:
                self.stat_val[key].config(text=f"{FMT.get(key, '{:.2f}').format(v)} {unit}", fg=FG)
                self.stat_src[key].config(text=src, fg=MUTED)
        # UC-12: if every metric of the target part fails for a second, the run can't be shown
        # truthfully (a second, because some readings need a moment to produce their first value)
        target_keys = [k for k, _, _, pt, _ in STATS if pt == self.target and k not in OPTIONAL]
        if not self.replay and target_keys and all(m.get(k) is None for k in target_keys):
            now = time.monotonic()
            self.lost_since = self.lost_since or now
            if now - self.lost_since >= 1.0:
                self.stop_test(f"stopped — {PARTS[self.target]['label']} telemetry lost")
                return
        else:
            self.lost_since = None
        self.msg_unavail = unavail

    def _check_thermal(self, m):
        checks = [("cpu", m.get("cpu_temp"), THERMAL_LIMIT["cpu"]),
                  ("gpu", m.get("gpu_temp"), min(THERMAL_LIMIT["gpu"], m.get("gpu_slowdown") or 999))]
        for part, temp, limit in checks:
            if part in self.involved and temp is not None and temp >= limit:
                name = PARTS[part]["label"]
                self.stop_test(f"aborted — thermal limit ({name} {temp:.0f} °C)")
                messagebox.showwarning("Thermal limit",
                                       f"{name} reached {temp:.0f} °C (limit {limit:.0f} °C). Test stopped.")
                return

    def _target_levels(self):
        """Latest real utilization 0..1 per part (None = unavailable)."""
        s = self.poller.latest if self.poller else {}
        out = {}
        for part in ORDER:
            if not self.running or part not in self.involved:
                out[part] = 0.0
                continue
            if part == "ram" and self.script == "ram" and self.peak_gbps > 0 and "gbps" in self.wl:
                out[part] = min(1.0, self.wl["gbps"] / self.peak_gbps)
                continue
            v = s.get(f"{part}_util")
            if part == "disk" and v is None and s.get("disk_read") is not None:
                # no active-time counter: fall back to throughput relative to this run's peak
                mb = s["disk_read"] + (s.get("disk_write") or 0.0)
                self.peak_io = max(self.peak_io, mb, 200.0)
                v = 100.0 * mb / self.peak_io
            out[part] = None if v is None else v / 100.0
        return out

    def _animate(self, dt):
        targets = self._target_levels()
        self.frame_n += 1
        for key in ORDER:
            tgt = targets[key]
            if tgt is None:  # unavailable: stops animating
                tgt = 0.0
            # ease toward the latest real sample (never overshoots it)
            self.level[key] += (tgt - self.level[key]) * min(1.0, dt * 12)
            lv = self.level[key]
            if not self.options[key]:
                continue
            base = PARTS[key]["color"]
            rect, text, sub = self.shapes[key]
            # panel body warms with load; edge goes white when this part is selected
            self.canvas.itemconfig(rect, fill=mix(PANEL, base, lv * 0.22))
            edge = mix(mix(base, BG, 0.5), base, lv)
            self.canvas.itemconfig(rect, outline=lighten(base, 0.7) if key == self.selected else edge)
            outer_halo, inner_halo = self.glows[key]
            glow_on = lv > 0.03
            self.canvas.itemconfig(outer_halo, outline=mix(BG, base, lv * 0.30) if glow_on else BG)
            self.canvas.itemconfig(inner_halo, outline=mix(BG, base, lv * 0.65) if glow_on else BG)
            # segmented meter
            lit = round(lv * SEGMENTS)
            if lit != self._lit.get(key):
                self._lit[key] = lit
                for i, seg in enumerate(self.bars[key]):
                    c = (mix(mix(base, BG, 0.3), lighten(base, 0.4), i / SEGMENTS)) if i < lit else "#1a1a1a"
                    self.canvas.itemconfig(seg, fill=c)
            show = self.running and key in self.involved
            if show and targets[key] is not None:
                self.canvas.itemconfig(sub, text=f"{lv * 100:.0f}%", fill=lighten(base, 0.5), font=(FONT, 36, "bold"))
            elif show:
                self.canvas.itemconfig(sub, text="UNAVAILABLE", fill="#ff5c5c", font=(FONT, 13, "bold"))
            else:
                self.canvas.itemconfig(sub, text="")
            # history graph: one point per 3 frames, scrolls left; bounded by the eased real samples
            hist = self.history[key]
            if self.frame_n % 3 == 0:
                hist.append(lv)
                gx0, gx1, gy0, gy1 = self.graph_box[key]
                step = (gx1 - gx0) / (HISTORY - 1)
                pts = []
                for i, v in enumerate(hist):
                    pts += [gx0 + i * step, gy1 - (gy1 - gy0) * v]
                self.canvas.coords(self.graphs[key], *pts)
                self.canvas.itemconfig(self.graphs[key], fill=mix(mix(base, BG, 0.5), base, 0.4 + 0.6 * lv))
        # per-core strip: eased toward each core's latest real reading
        cores = None
        if self.running and "cpu" in self.involved and self.poller:
            c = (self.poller.latest or {}).get("cpu_cores")
            cores = c if isinstance(c, list) and c else None
        n = len(self.core_bars)
        lit = lighten(PARTS["cpu"]["color"], 0.3)
        for i in range(n):
            if cores:
                a, b = i * len(cores) // n, (i + 1) * len(cores) // n
                tgt = max(cores[a:b] or [cores[min(a, len(cores) - 1)]]) / 100.0
            else:
                tgt = 0.0
            self.core_lv[i] += (tgt - self.core_lv[i]) * min(1.0, dt * 12)
            top, bot = self.core_box
            h = round((bot - top) * self.core_lv[i])
            if h != self._core_col.get(i):  # bar height = that core's load
                self._core_col[i] = h
                xa, _, xb, _ = self.canvas.coords(self.core_bars[i])
                self.canvas.coords(self.core_bars[i], xa, bot - h, xb, bot)
                self.canvas.itemconfig(self.core_bars[i], fill=mix(PARTS["cpu"]["color"], lit, self.core_lv[i]))
        for (a, b), item in self.links.items():
            flow = (self.level[a] + self.level[b]) / 2
            if self.running and flow > 0.02:
                self.dash_off[(a, b)] -= dt * (20 + 160 * flow)
                trace = mix(PARTS[a]["color"], PARTS[b]["color"], 0.5)
                self.canvas.itemconfig(item, dashoffset=int(self.dash_off[(a, b)]),
                                       fill=mix(GREY, lighten(trace, 0.3), flow))
            else:
                self.canvas.itemconfig(item, fill=GREY)

    # ------------------------------------------------------------ readout / summary
    def _clear_readout(self):
        for key, _, _, _, src in STATS:
            if key in ("cpu_temp", "board_temp") and not self.remote and telemetry.LHM:
                src = "LibreHardwareMonitor"  # running: the readings come from it during a test
            self.stat_val[key].config(text="—", fg=MUTED)
            self.stat_src[key].config(text=src, fg=MUTED)

    def _set_summary(self, text):
        self.summary.config(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", text)
        # the code run's slowest lines, each a link into VS Code
        links, self._code_links = getattr(self, "_code_links", []), []
        if links:
            self.summary.insert("end", "\n\nWhere in your code (click to open in VS Code):\n")
            for i, (file, line, label) in enumerate(links):
                tag = f"code{i}"
                self.summary.insert("end", f"  {label}\n", tag)
                self.summary.tag_config(tag, foreground=EDITOR_COLOR, underline=True)
                self.summary.tag_bind(tag, "<Button-1>", lambda e, f=file, n=line: self._open_in_editor(f, n))
                self.summary.tag_bind(tag, "<Enter>", lambda e: self.summary.config(cursor="hand2"))
                self.summary.tag_bind(tag, "<Leave>", lambda e: self.summary.config(cursor=""))
        self.summary.config(state="disabled")

    def _show_summary(self, result, duration):
        lines = [f"Result: {result}   Test: {TESTS[self.test_key]['label']} on "
                 f"{PARTS[self.target]['label']}   Duration: {duration:.1f} s   Machine: {self._machine_name()}", ""]
        if self.is_ai:
            lines.insert(1, f"Agent: {self.ai_steps} tool calls, {self.ai_blocked} blocked action(s)")
            if self.ai_done_text:
                lines.insert(2, f"Agent said: {self.ai_done_text}")
        if self.is_code:
            lines.insert(1, f"Program: {self.program}")
        findings = analysis.analyze(self.record, self.hw.get("threads"))
        prev = self._previous_run()  # looked up before this run is saved
        prev_avg = prev["averages"] if prev else {}
        head = f"{'Metric':<20}{'min':>11}{'avg':>11}{'peak':>11}  {'unit':<10}"
        if prev:
            head += f"{'prev avg':>11}{'change':>9}"
        lines.append(head)
        for key, label, unit, part, _ in STATS:
            vals = self.samples.get(key)
            if part in PARTS and part not in self.involved:
                continue
            if part == "wl" and not vals:
                continue
            f = FMT.get(key, "{:.2f}").format
            if not vals:
                lines.append(f"{label:<20}{'no data':>33}")
                continue
            avg = sum(vals) / len(vals)
            line = f"{label:<20}{f(min(vals)):>11}{f(avg):>11}{f(max(vals)):>11}  {unit:<10}"
            old = prev_avg.get(key)
            if isinstance(old, (int, float)):
                line += f"{f(old):>11}"
                if old:
                    pct = 100.0 * (avg - old) / abs(old)
                    line += f"{pct:>+8.1f}%"
                    if part == "wl" and abs(pct) >= 3:  # only workload results have a "better"
                        line += "  better" if (pct < 0) == (key in LOWER_IS_BETTER) else "  worse"
            lines.append(line)
        if prev:
            lines.append(f"Compared with the previous run of this test on this part ({prev.get('when', '?')}, "
                         f"{prev.get('result', '?')}).")
        lines += [""] + analysis.report(findings)
        self._code_links = []
        saved = self._save_run(result, duration, "\n".join(lines), findings)
        lines.append("")
        lines.append(f"Recorded as runs/{saved} - REPLAY watches it again, COMPARE puts it next to another run."
                     if saved else "Not recorded (no samples were taken).")
        self._set_summary("\n".join(lines))


class CompareWindow:
    """Two recorded runs side by side (SRS stretch goal): the same test on two machines,
    or your program before and after a change. Averages with the change, one metric
    over time for both runs, and what the bottleneck finder says about each."""
    COLORS = ("#00e5ff", "#ff9f1a")  # run A, run B
    GW, GH = 900, 230

    def __init__(self, root, a=None, b=None):
        """a, b: run file names to open with (default: newest run and an earlier one like it)."""
        self.runs, labels, by_file = {}, [], {}
        try:
            names = sorted((n for n in os.listdir(RUNS_DIR) if n.endswith(".json")), reverse=True)
        except OSError:
            names = []
        for n in names:
            try:
                with open(os.path.join(RUNS_DIR, n), "r", encoding="utf-8") as f:
                    run = json.load(f)
                if not run.get("samples"):
                    continue
            except (OSError, ValueError):
                continue
            prog = run.get("program")
            what = ("program " + (prog if len(prog) <= 40 else "…" + prog[-39:]) if prog else
                    f"{run.get('test_label', '?')} on {str(run.get('part', '?')).upper()}")
            label = f"{run.get('when', '?')}   {run.get('machine', 'this PC')}   {what}   ({run.get('result', '?')})"
            while label in self.runs:
                label += " "
            self.runs[label] = run
            labels.append(label)
            by_file[n] = label
            run["_same"] = (run.get("test"), run.get("part"), run.get("program"))
        if len(labels) < 2:
            messagebox.showinfo("Compare runs", "Record at least two runs first (every finished test is recorded).")
            return
        top = self.top = tk.Toplevel(root)
        top.title("Compare runs")
        top.configure(bg=BG)
        top.geometry(f"{self.GW + 48}x860")
        tk.Label(top, text="COMPARE RUNS", bg=BG, fg=FG, font=(FONT, 16, "bold")).pack(anchor="w", padx=24,
                                                                                     pady=(14, 4))
        # B = newest run; A = the newest earlier run of the same test (or program), else the one before
        newest = self.runs[labels[0]]["_same"]
        older = next((lab for lab in labels[1:] if self.runs[lab]["_same"] == newest), labels[1])
        pick_a, pick_b = by_file.get(a, older), by_file.get(b, labels[0])
        self.vars = []
        for i, (name, default) in enumerate((("A", pick_a), ("B", pick_b))):
            row = tk.Frame(top, bg=BG)
            row.pack(fill="x", padx=24, pady=2)
            tk.Label(row, text=name, bg=BG, fg=self.COLORS[i], font=(FONT, 14, "bold"), width=2).pack(side="left")
            var = tk.StringVar(value=default)
            m = tk.OptionMenu(row, var, *labels, command=lambda *_: self.refresh())
            m.config(bg=PANEL, fg=FG, activebackground=LINE, activeforeground=self.COLORS[i], relief="flat",
                     highlightthickness=1, highlightbackground=self.COLORS[i], font=("Consolas", 9),
                     anchor="w", width=110)
            m["menu"].config(bg=PANEL, fg=FG, font=("Consolas", 9))
            m.pack(side="left", fill="x", expand=True)
            self.vars.append(var)
        mrow = tk.Frame(top, bg=BG)
        mrow.pack(fill="x", padx=24, pady=(10, 2))
        tk.Label(mrow, text="GRAPH", bg=BG, fg=MUTED, font=(FONT, 10, "bold")).pack(side="left", padx=(0, 8))
        self.metric = tk.StringVar()
        self.metric_menu = tk.OptionMenu(mrow, self.metric, "")
        self.metric_menu.config(bg=PANEL, fg=FG, activebackground=LINE, relief="flat", highlightthickness=1,
                                highlightbackground=LINE, font=(FONT, 10, "bold"), width=24)
        self.metric_menu["menu"].config(bg=PANEL, fg=FG, font=(FONT, 10))
        self.metric_menu.pack(side="left")
        self.canvas = tk.Canvas(top, width=self.GW, height=self.GH, bg=PANEL, highlightthickness=1,
                                highlightbackground=LINE)
        self.canvas.pack(padx=24, pady=(6, 8))
        box = tk.Frame(top, bg=BG)
        box.pack(fill="both", expand=True, padx=24, pady=(0, 16))
        self.text = tk.Text(box, bg=PANEL, fg=FG, font=("Consolas", 9), relief="flat", wrap="word",
                            highlightthickness=1, highlightbackground=LINE)
        bar = tk.Scrollbar(box, command=self.text.yview)
        self.text.config(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        self.text.tag_config("a", foreground=self.COLORS[0])
        self.text.tag_config("b", foreground=self.COLORS[1])
        self.text.tag_config("head", foreground=ACCENT)
        self.text.tag_config("bad", foreground="#ff6b6b")
        self.refresh()

    def _pair(self):
        return self.runs[self.vars[0].get()], self.runs[self.vars[1].get()]

    @staticmethod
    def _series(run, key):
        return [(s["t"], s[key]) for s in run["samples"] if isinstance(s.get(key), (int, float))]

    def refresh(self):
        a, b = self._pair()
        stats = [st for st in STATS if self._series(a, st[0]) or self._series(b, st[0])]
        labels = [st[1] for st in stats]
        menu = self.metric_menu["menu"]
        menu.delete(0, "end")
        for lab in labels:
            menu.add_command(label=lab, command=tk._setit(self.metric, lab, lambda *_: self.draw()))
        if self.metric.get() not in labels:
            prefer = [st[1] for st in stats if st[3] == "wl"] + [st[1] for st in stats if st[0].endswith("_util")]
            self.metric.set((prefer or labels or [""])[0])
        self.write_table(a, b, stats)
        self.draw()

    def write_table(self, a, b, stats):
        t = self.text
        t.config(state="normal")
        t.delete("1.0", "end")

        def cut(v):  # long paths: the end (file name) matters more than the start
            v = str(v)
            return v if len(v) <= 46 else "…" + v[-45:]

        def row(label, va, vb):
            t.insert("end", f"{label:<22}")
            t.insert("end", f"{cut(va):<48}", "a")
            t.insert("end", f"{cut(vb):<48}\n", "b")

        row("", "A", "B")
        for label, key in (("When", "when"), ("Machine", "machine"), ("Test", "test_label"), ("Part", "part_name"),
                           ("Program", "program"), ("Result", "result")):
            if a.get(key) or b.get(key):
                row(label, a.get(key) or "-", b.get(key) or "-")
        da, db = a.get("duration") or 0, b.get("duration") or 0
        line = f"{'Duration':<22}{da:>10.1f} s{'':36}{db:>10.1f} s"
        if da:
            line += f"   {100 * (db - da) / da:+.1f}% B vs A"
        t.insert("end", line + "\n\n")
        t.insert("end", f"{'Average':<22}{'A':>12}{'B':>12}   {'unit':<10}{'B vs A':>9}\n", "head")
        for key, label, unit, part, _ in stats:
            va, vb = (a.get("averages") or {}).get(key), (b.get("averages") or {}).get(key)
            f = FMT.get(key, "{:.2f}").format
            sa = f(va) if isinstance(va, (int, float)) else "-"
            sb = f(vb) if isinstance(vb, (int, float)) else "-"
            line = f"{label:<22}{sa:>12}{sb:>12}   {unit:<10}"
            if isinstance(va, (int, float)) and isinstance(vb, (int, float)) and va:
                pct = 100.0 * (vb - va) / abs(va)
                line += f"{pct:>+8.1f}%"
                if part == "wl" and abs(pct) >= 3:
                    line += "  better" if (pct < 0) == (key in LOWER_IS_BETTER) else "  worse"
            t.insert("end", line + "\n")
        for name, run, tag in (("A", a, "a"), ("B", b, "b")):
            findings = run.get("findings")
            if findings is None:  # runs recorded before the bottleneck finder existed
                findings = analysis.analyze(run["samples"], run.get("threads"))
            t.insert("end", f"\n{name}: ", tag)
            for i, ln in enumerate(analysis.report(findings)):
                t.insert("end", ("" if i == 0 else "   ") + ln + "\n", "bad" if ln.lstrip().startswith("!") else ())
        t.config(state="disabled")

    def draw(self):
        c = self.canvas
        c.delete("all")
        stat = next((st for st in STATS if st[1] == self.metric.get()), None)
        if not stat:
            return
        key, label, unit = stat[0], stat[1], stat[2]
        series = [self._series(r, key) for r in self._pair()]
        if not any(series):
            return
        x0, x1, y0, y1 = 56, self.GW - 16, 18, self.GH - 28
        t_max = max(s[-1][0] for s in series if s) or 1.0
        v_max = 100.0 if unit == "%" else max(v for s in series for _, v in s) * 1.1 or 1.0
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = y1 - (y1 - y0) * frac
            c.create_line(x0, y, x1, y, fill="#1c1c1c", dash=(2, 4))
            c.create_text(x0 - 6, y, text=FMT.get(key, "{:.2f}").format(v_max * frac), anchor="e", fill=MUTED,
                          font=("Consolas", 8))
        for i in range(6):
            x = x0 + (x1 - x0) * i / 5
            c.create_text(x, y1 + 12, text=f"{t_max * i / 5:.0f} s", fill=MUTED, font=("Consolas", 8))
        for s, color, name in zip(series, self.COLORS, ("A", "B")):
            if len(s) < 2:
                continue
            step = max(1, len(s) // 800)
            pts = []
            for t_, v in s[::step]:
                pts += [x0 + (x1 - x0) * t_ / t_max, y1 - (y1 - y0) * min(1.0, v / v_max)]
            c.create_line(*pts, fill=color, width=2)
        c.create_text(x0 + 6, y0, text=f"{label} ({unit})" if unit else label, anchor="nw", fill=FG,
                      font=(FONT, 10, "bold"))
        c.create_text(x1 - 60, y0, text="A", anchor="ne", fill=self.COLORS[0], font=(FONT, 11, "bold"))
        c.create_text(x1 - 30, y0, text="B", anchor="ne", fill=self.COLORS[1], font=(FONT, 11, "bold"))


def _code_lines(profile_path, n=6):
    """[(file, line, label)] for the slowest lines of a code profile, for the summary's links."""
    try:
        with open(profile_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    root = data.get("root") or os.path.dirname(data.get("script") or "")
    out = []
    for l in (data.get("lines") or [])[:n]:
        try:
            rel = os.path.relpath(l["file"], root)
        except ValueError:
            rel = l["file"]
        out.append((l["file"], l["line"], f"{rel}:{l['line']}  {l['func']}  {l['seconds']:.2f} s  "
                    f"({100 * l['share']:.0f}% of the run)"))
    return out


def _fine_timer(on):
    """Windows wakes timers every 15.6 ms by default, which caps Tk's after() loop at ~48 FPS.
    Ask for 1 ms while the app runs (what games and media players do); undone on exit."""
    try:
        import ctypes
        (ctypes.windll.winmm.timeBeginPeriod if on else ctypes.windll.winmm.timeEndPeriod)(1)
    except (AttributeError, OSError):
        pass


if __name__ == "__main__":
    _fine_timer(True)
    root = tk.Tk()
    app = App(root)
    app._open_link()
    if "--ai-control" in sys.argv:
        app.toggle_ai_control()
    try:
        root.mainloop()
    finally:
        _fine_timer(False)
