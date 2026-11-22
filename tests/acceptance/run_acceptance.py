"""Acceptance test run with screenshots (Windows).

    python tests/acceptance/run_acceptance.py [AT-3 AT-6 ...]     # default: all

Opens the real desktop app and goes through the acceptance test cases of
docs/ACCEPTANCE_TESTS.md. Each step does what the user's click does - it calls the same
handler the button or the diagram is wired to (App.start_test, App.stop_test, App.open_compare,
...) - waits for the app's real reaction, and captures the window and every pop-up it opens
(Win32 PrintWindow, so other windows on top don't matter). Real workloads run on the real
hardware; nothing is simulated.

Output: docs/acceptance/img/AT-*.png and tests/work/acceptance/results.json (what each step
observed: status line, summary, log, pop-up text). Runs and settings go to a separate data
folder (tests/work/acceptance/data), not the user's.

Needs: Windows, the app's requirements. AT-5 needs Ollama with a tool-calling model; AT-9 needs
VS Code with the HCV Hot Spots extension, open on examples/demo_project.
"""
import ctypes
import ctypes.wintypes as wt
import glob
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
WORK = os.path.join(ROOT, "tests", "work", "acceptance")
DATA = os.path.join(WORK, "data")
IMG = os.path.join(ROOT, "docs", "acceptance", "img")
os.makedirs(DATA, exist_ok=True)
os.makedirs(IMG, exist_ok=True)
os.environ["HCV_DATA_DIR"] = DATA  # before importing the app: runs/ and settings go here
sys.path.insert(0, ROOT)

import tkinter as tk  # noqa: E402

from PIL import Image  # noqa: E402

import main  # noqa: E402

user32, gdi32, dwmapi = ctypes.windll.user32, ctypes.windll.gdi32, ctypes.windll.dwmapi
WM_COMMAND, IDOK, IDCANCEL = 0x0111, 1, 2
RESULTS = []


# ---------------------------------------------------------------- screenshots
class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def window_rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def visible_rect(hwnd):
    """The window without Windows 11's invisible resize border."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) == 0:
        return r.left, r.top, r.right, r.bottom
    return window_rect(hwnd)


def capture(hwnd, name):
    """PNG of one window (PrintWindow: works even if the window is covered)."""
    l, t, r, b = window_rect(hwnd)
    w, h = r - l, b - t
    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    user32.PrintWindow(hwnd, mdc, 2)  # PW_RENDERFULLCONTENT
    bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mdc)
    user32.ReleaseDC(hwnd, hdc)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    vl, vt, vr, vb = visible_rect(hwnd)
    img = img.crop((vl - l, vt - t, vl - l + (vr - vl), vt - t + (vb - vt)))
    path = os.path.join(IMG, name + ".png")
    img.save(path)
    return os.path.relpath(path, ROOT).replace("\\", "/")


def grab_screen_area(hwnd, name):
    """Screen pixels of a window that is in front (for VS Code, which PrintWindow may not draw)."""
    from PIL import ImageGrab
    img = ImageGrab.grab(bbox=visible_rect(hwnd), all_screens=True)
    path = os.path.join(IMG, name + ".png")
    img.save(path)
    return os.path.relpath(path, ROOT).replace("\\", "/")


def windows(pid=None):
    """[(hwnd, title, class)] of visible top-level windows (of this process, by default)."""
    out = []
    pid = os.getpid() if pid is None else pid

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if pid and p.value != pid:
            return True
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        out.append((hwnd, title.value, cls.value))
        return True
    user32.EnumWindows(cb, 0)
    return out


def dialog_text(hwnd):
    """The message of a standard Windows message box (its static text controls)."""
    texts = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(child, _):
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(child, cls, 64)
        if cls.value == "Static":
            t = ctypes.create_unicode_buffer(2048)
            user32.GetWindowTextW(child, t, 2048)
            if t.value.strip():
                texts.append(t.value)
        return True
    user32.EnumChildWindows(hwnd, cb, 0)
    return "\n".join(texts)


def wait_dialog(title, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for hwnd, t, cls in windows():
            if t == title and cls == "#32770":
                time.sleep(0.6)  # let it finish drawing
                return hwnd
        time.sleep(0.1)
    raise TimeoutError(f"no pop-up '{title}' within {timeout} s")


def answer(hwnd, ok=True):
    """Clicks the pop-up's OK (or Cancel) button."""
    want = ("OK", "&OK", "Yes", "&Yes") if ok else ("Cancel", "&Cancel", "No", "&No")
    btn = user32.FindWindowExW(hwnd, 0, "Button", None)
    first = btn
    while btn:
        t = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(btn, t, 64)
        if t.value in want:
            break
        btn = user32.FindWindowExW(hwnd, btn, "Button", None)
    user32.SendMessageW(btn or first, 0x00F5, 0, 0)  # BM_CLICK


def capture_popup(hwnd, name):
    """A message box: from the screen (PrintWindow leaves its inside blank). It is modal and in front."""
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.5)
    return grab_screen_area(hwnd, name)


# ---------------------------------------------------------------- driving the app
class Driver:
    """Runs the scenarios on a worker thread; UI calls are handed to the Tk thread."""

    def __init__(self, app, root):
        self.app, self.root = app, root
        self.calls = queue.Queue()
        root.after(20, self._pump)

    def _pump(self):
        while True:
            try:
                fn, box, done = self.calls.get_nowait()
            except queue.Empty:
                break
            try:
                box.append(fn())
            except Exception as e:  # report in the worker
                box.append(e)
            done.set()
        self.root.after(20, self._pump)

    def ui(self, fn, wait=True, timeout=180):
        box, done = [], threading.Event()
        t0 = time.monotonic()
        self.calls.put((fn, box, done))
        if not wait:
            return done
        if not done.wait(timeout):
            raise TimeoutError(f"the window didn't answer in {timeout} s")
        self.slowest = max(getattr(self, "slowest", 0.0), time.monotonic() - t0)  # reported per case
        if isinstance(box[0], Exception):
            raise box[0]
        return box[0]

    # what the app shows
    def hwnd(self):
        return int(self.ui(lambda: self.root.wm_frame()), 16)

    def shot(self, name):
        self.ui(lambda: self.root.update_idletasks())
        time.sleep(0.4)
        return self.capture_window(self.root, name)

    def capture_window(self, win, name):
        """PrintWindow on the Tk thread itself: called from another thread it stalls Tk's timers
        (every capture froze the frame loop for ~3 s, which piled up into minutes)."""
        return self.ui(lambda: capture(int(win.wm_frame(), 16), name))

    def status(self):
        return self.ui(lambda: self.app.status.cget("text"))

    def summary(self):
        return self.ui(lambda: self.app.summary.get("1.0", "end").strip())

    def log(self):
        return self.ui(lambda: self.app.log.get("1.0", "end").strip())

    def running(self):
        return self.ui(lambda: self.app.running)

    def show_bottlenecks(self):
        """Scrolls the summary box to the bottleneck finder's part (the box is short)."""
        def do():
            idx = self.app.summary.search("Bottlenecks", "1.0", stopindex="end")
            if idx:
                self.app.summary.yview(f"{idx} - 6 lines")
        self.ui(do)

    def wait(self, cond, timeout, every=0.5):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(every)
        return False

    # what the user does
    def click_part(self, key, value=None):
        def do():
            if value is None:
                self.app._on_diagram_click(key)
            else:
                self.app.combos[key].set_value(value)
                self.app._select(key, value)
        self.ui(do)
        time.sleep(0.3)

    def pick_test(self, key):
        self.ui(lambda: self.app.test_var.set(key))
        time.sleep(0.3)

    def set_program(self, command, folder=""):
        self.ui(lambda: (self.app.program_var.set(command), self.app.folder_var.set(folder), self.app._show_plan()))

    def start(self):
        return self.ui(self.app.start_test, wait=False)  # may open a modal pop-up

    def stop(self):
        self.ui(lambda: self.app.stop_test("stopped by user"))

    def finish_run(self, timeout):
        if not self.wait(lambda: not self.running(), timeout):
            raise TimeoutError("the test didn't end in time")
        time.sleep(1.0)


def step(at, title):
    def wrap(fn):
        fn.at, fn.title = at, title
        return fn
    return wrap


DRIVER = None


def record(at, title, **observed):
    if DRIVER is not None:  # how long the window took to answer the slowest request in this case
        observed["slowest_ui_reply_s"] = round(getattr(DRIVER, "slowest", 0.0), 2)
        DRIVER.slowest = 0.0
    RESULTS.append({"at": at, "title": title, **observed})
    print(f"  {at}: {title} - " + ", ".join(f"{k}" for k in observed), flush=True)


# ---------------------------------------------------------------- the acceptance test cases
@step("AT-1", "Start the app: hardware detected, live idle readings")
def at1(d):
    time.sleep(4)
    hw = d.ui(lambda: {k: d.app.options[k] for k in main.ORDER})
    record("AT-1", at1.title, screenshot=d.shot("AT-1_start_idle"), detected=hw, status=d.status())


@step("AT-2", "Select a component, then another (UC-3, UC-4)")
def at3(d):
    d.click_part("gpu")
    s1 = d.shot("AT-2a_gpu_selected")
    st1 = d.status()
    d.click_part("gpu")  # reselect: nothing changes
    st_same = d.status()
    d.click_part("cpu")
    record("AT-2", at3.title, screenshot=[s1, d.shot("AT-2b_cpu_selected")], status_gpu=st1,
           status_after_reselect=st_same, status_cpu=d.status())


@step("AT-7", "Invalid start requests are refused with a message (UC-13)")
def at8(d):
    shots, texts = [], []
    d.start()  # nothing selected yet (runs right after AT-1)
    h = wait_dialog("Can't start test")
    texts.append(dialog_text(h))
    shots.append(capture_popup(h, "AT-7a_no_part_popup"))
    answer(h)
    time.sleep(0.5)
    shots.append(d.shot("AT-7b_no_part_status"))
    # YOUR CODE with nothing typed
    d.click_part("cpu")
    d.pick_test("code")
    d.set_program("")
    d.start()
    h = wait_dialog("Can't start test")
    texts.append(dialog_text(h))
    shots.append(capture_popup(h, "AT-7c_no_command_popup"))
    answer(h)
    # a program that doesn't exist: the runner reports it, the app says it failed to start
    d.set_program(r"C:\no\such\program.exe")
    d.start()
    h = wait_dialog("Can't start test", timeout=30)
    texts.append(dialog_text(h))
    shots.append(capture_popup(h, "AT-7d_cannot_start_popup"))
    answer(h)
    time.sleep(0.5)
    shots.append(d.shot("AT-7e_after_failed_start"))
    record("AT-7", at8.title, screenshot=shots, popups=texts, status=d.status(), running=d.running())


@step("AT-3", "2D workload on the CPU runs to completion (UC-5, UC-9, UC-10, UC-11)")
def at4(d):
    d.click_part("cpu")
    d.pick_test("2d")
    s0 = d.shot("AT-3a_cpu_2d_ready")
    d.start()
    time.sleep(12)
    s1 = d.shot("AT-3b_cpu_2d_running")
    clock = d.ui(lambda: d.app.clock.cget("text"))
    d.finish_run(60)
    d.show_bottlenecks()
    record("AT-3", at4.title, screenshot=[s0, s1, d.shot("AT-3c_cpu_2d_summary")], clock_while_running=clock,
           status=d.status(), summary=d.summary())


@step("AT-4", "3D workload on the GPU (UC-6)")
def at5(d):
    d.click_part("gpu")
    d.pick_test("3d")
    d.start()
    time.sleep(12)
    s1 = d.shot("AT-4a_gpu_3d_running")
    d.finish_run(60)
    record("AT-4", at5.title, screenshot=[s1, d.shot("AT-4b_gpu_3d_summary")], status=d.status(), summary=d.summary())


@step("AT-6", "Stop a running test (UC-8)")
def at7(d):
    d.click_part("ram")
    d.pick_test("2d")
    d.start()
    time.sleep(8)
    s1 = d.shot("AT-6a_ram_running")
    t0 = time.monotonic()
    d.stop()
    stopped_in = time.monotonic() - t0
    time.sleep(1)
    d.show_bottlenecks()
    record("AT-6", at7.title, screenshot=[s1, d.shot("AT-6b_ram_stopped_summary")], stop_took_s=round(stopped_in, 2),
           status=d.status(), running=d.running(), summary=d.summary())


@step("AT-11", "Other parts: RAM bandwidth (full run, compared with AT-6), disk I/O, PCIe bus, ML on the GPU")
def at12(d):
    shots, summaries = [], {}
    for part, test, name, wait_s in (("ram", "2d", "ram", 30), ("disk", "io", "disk", 30),
                                      ("board", "bus", "pcie", 30), ("gpu", "ml", "ml_gpu", 30)):
        d.click_part(part)
        d.pick_test(test)
        d.start()
        time.sleep(12)
        shots.append(d.shot(f"AT-11_{name}_running"))
        d.finish_run(90)
        shots.append(d.shot(f"AT-11_{name}_summary"))
        summaries[name] = {"status": d.status(), "summary": d.summary()}
    record("AT-11", at12.title, screenshot=shots, results=summaries)


@step("AT-8", "YOUR CODE: run your own program and find its bottlenecks (ultimate goal)")
def at9(d):
    demo = os.path.join(ROOT, "examples", "demo_project", "main.py")
    d.click_part("cpu")
    d.pick_test("code")
    d.set_program(f'"{demo}"')
    s0 = d.shot("AT-8a_your_code_ready")
    d.start()
    time.sleep(6)
    s1 = d.shot("AT-8b_your_code_running")
    d.finish_run(620)
    d.show_bottlenecks()
    record("AT-8", at9.title, screenshot=[s0, s1, d.shot("AT-8c_your_code_summary")], status=d.status(),
           summary=d.summary(), log=d.log()[-3000:])


@step("AT-10", "Compare two runs and replay a recording (stretch goals)")
def at11(d):
    d.ui(d.app.open_compare)
    time.sleep(1.5)
    top = d.ui(lambda: [w for w in d.root.winfo_children() if isinstance(w, tk.Toplevel)][-1])
    s1 = d.capture_window(top, "AT-10a_compare_window")
    title = d.ui(lambda: top.title())
    d.ui(top.destroy)
    runs = sorted(glob.glob(os.path.join(DATA, "runs", "*-2d-cpu.json")))
    d.ui(lambda: d.app.start_replay(runs[-1]))
    time.sleep(8)
    s2 = d.shot("AT-10b_replay_running")
    clock = d.ui(lambda: d.app.clock.cget("text"))
    d.finish_run(60)
    record("AT-10", at11.title, screenshot=[s1, s2, d.shot("AT-10c_replay_ended")], compare_title=title,
           replay_file=os.path.basename(runs[-1]), clock_during_replay=clock, status=d.status())


@step("AT-5", "AI Benchmark Mode: a local AI model does a task by itself (UC-7)")
def at6(d):
    d.click_part("gpu")
    d.pick_test("ai")
    d.ui(lambda: d.app.goal_var.set(next(t for t in d.app.goal_ids if "matri" in t.lower())))
    s0 = d.shot("AT-5a_ai_ready")
    d.start()
    time.sleep(45)
    s1 = d.shot("AT-5b_ai_working")
    d.finish_run(660)
    record("AT-5", at6.title, screenshot=[s0, s1, d.shot("AT-5c_ai_summary")], status=d.status(),
           summary=d.summary(), log=d.log()[-4000:])


@step("AT-9", "Hot spots in VS Code: slow lines of your code boxed in the editor")
def vscode_layers():
    """The HCV extension's eye-menu choices, as VS Code saved them (None if unknown)."""
    import shutil
    import sqlite3
    import tempfile
    src = os.path.join(os.environ.get("APPDATA", ""), "Code", "User", "globalStorage", "state.vscdb")
    if not os.path.isfile(src):
        return None
    tmp = os.path.join(tempfile.mkdtemp(prefix="hcv-vsstate-"), "state.vscdb")
    shutil.copy(src, tmp)  # VS Code keeps the file open
    with sqlite3.connect(tmp) as con:
        row = con.execute("select value from ItemTable where key = 'hcv.hcv-hotspots'").fetchone()
    return json.loads(row[0]) if row else {}


def at10(d):
    layers = vscode_layers()
    if layers is not None and layers.get("hcv.show.speed") is False:
        raise RuntimeError("the speed boxes are switched off in VS Code (eye menu on a Memory-only view): "
                           "right-click the yellow run button and choose Everything, then re-run AT-9")
    demo = os.path.join(ROOT, "examples", "demo_project")
    out = os.path.join(demo, ".hcv", "profile.json")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "profiler.py"), "--out", out, "--root", demo, "--cwd", demo,
                        os.path.join(demo, "main.py")], capture_output=True, text=True, timeout=300)
    with open(out, encoding="utf-8") as f:
        prof = json.load(f)
    shots = []
    for rel, name in (("app/compute.py", "AT-9a_vscode_compute"), ("app/loader.py", "AT-9b_vscode_loader")):
        subprocess.run(["code", "-r", "-g", os.path.join(demo, *rel.split("/")) + ":1"], shell=True)
        time.sleep(5)
        vs = [h for h, t, c in windows(pid=0) if "Visual Studio Code" in t and os.path.basename(rel) in t]
        if vs:
            user32.ShowWindow(vs[0], 9)
            user32.SetForegroundWindow(vs[0])
            time.sleep(1.5)
            shots.append(grab_screen_area(vs[0], name))
    top = sorted(prof["lines"], key=lambda l: -l["share"])[:5]
    record("AT-9", at10.title, screenshot=shots, profiler_exit=r.returncode, result=prof.get("result"),
           top_lines=[(os.path.relpath(l["file"], demo), l["line"], round(l["share"], 3)) for l in top],
           findings=[f["key"] for f in prof.get("findings", [])])


ORDER = [at1, at8, at3, at4, at5, at7, at12, at9, at11, at6, at10]


def save_results():
    """results.json: this run's cases replace the same cases of earlier runs (a partial re-run keeps the rest)."""
    path = os.path.join(WORK, "results.json")
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = []
    ran = {r["at"] for r in RESULTS}
    order = [fn.at for fn in ORDER]
    merged = sorted([r for r in old if r["at"] not in ran] + RESULTS,
                    key=lambda r: order.index(r["at"]) if r["at"] in order else 99)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=1, ensure_ascii=False)


def scenario(d, wanted):
    try:
        for fn in ORDER:
            if wanted and fn.at not in wanted:
                continue
            print(f"{fn.at} {fn.title}", flush=True)
            try:
                fn(d)
            except Exception as e:
                RESULTS.append({"at": fn.at, "title": fn.title, "error": f"{type(e).__name__}: {e}",
                                "trace": traceback.format_exc()})
                print(f"  {fn.at} FAILED: {e}", flush=True)
                try:
                    d.shot(f"{fn.at}_error")
                    if d.running():
                        d.stop()
                except Exception:
                    pass
            save_results()

    finally:
        d.ui(d.app._on_close, wait=False)


def run():
    wanted = set(sys.argv[1:])
    main._fine_timer(True)
    root = tk.Tk()
    app = main.App(root)
    global DRIVER
    d = DRIVER = Driver(app, root)
    threading.Thread(target=scenario, args=(d, wanted), daemon=True).start()
    try:
        root.mainloop()
    finally:
        main._fine_timer(False)
    errors = [r for r in RESULTS if "error" in r]
    print(f"\n{len(RESULTS) - len(errors)} acceptance tests ran, {len(errors)} failed. "
          f"Results: tests/work/acceptance/results.json, screenshots: docs/acceptance/img/")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(run())
