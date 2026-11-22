"""Writes docs/ACCEPTANCE_TESTS.md from the test case definitions below and the observed
results of the last run (tests/work/acceptance/results.json, written by run_acceptance.py).

    python tests/acceptance/write_doc.py
"""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RESULTS = os.path.join(ROOT, "tests", "work", "acceptance", "results.json")
OUT = os.path.join(ROOT, "docs", "ACCEPTANCE_TESTS.md")

# id: (title, covers, preconditions, steps, expected)
CASES = {
    "AT-1": ("Start the app: hardware detected, live readings", "UC-1, UC-10, UC-11",
             "Windows PC; LibreHardwareMonitor running (for CPU / chipset temperature).",
             ["Start the app (`python main.py`)."],
             "The window opens; RAM, CPU, GPU, BOARD and DISK show the detected parts; the parts and the "
             "readout show live idle readings; nothing is selected."),
    "AT-2": ("Select a component, then another", "UC-3, UC-4",
             "App open, no test running.",
             ["Click GPU in the diagram.", "Click GPU again.", "Click CPU."],
             "The clicked part is outlined and named in the status line; clicking the selected part again "
             "changes nothing; selecting another part moves the selection."),
    "AT-3": ("2D workload on the CPU runs to completion", "UC-5, UC-9, UC-10, UC-11",
             "App open.",
             ["Click CPU.", "Choose 2D.", "Press START.", "Wait for the end (30 s)."],
             "Every core works (per-core bars all tall), the diagram and readout follow the real readings at "
             "about 60 FPS and 20 Hz; after 30 s the status says `completed` and the summary shows min / avg / peak "
             "per metric, the bottleneck finder's result, and that the run was recorded."),
    "AT-4": ("3D workload on the GPU", "UC-6",
             "NVIDIA GPU; moderngl installed.",
             ["Click GPU.", "Choose 3D.", "Press START.", "Wait for the end."],
             "The GPU is near 100 %, frame time is measured, the CPU and board are shown as working alongside; "
             "the run completes with a summary."),
    "AT-5": ("AI Benchmark Mode", "UC-7",
             "Ollama running with a tool-calling model.",
             ["Click GPU.", "Choose AI BENCHMARK, task \"Crunch big matrices\".", "Press START.", "Wait for the end."],
             "The log shows the agent's own steps (tool calls, results, any blocked actions); the run ends as "
             "`completed` only if the model called finish, otherwise as stopped / time limit; the summary counts "
             "the tool calls and blocked actions."),
    "AT-6": ("Stop a running test", "UC-8",
             "App open.",
             ["Click RAM, choose 2D, press START.", "After about 8 s press STOP."],
             "The workload ends at once, the status says `stopped by user`, START is enabled again, and the "
             "partial run is summarized and recorded."),
    "AT-7": ("Invalid start requests are refused", "UC-13",
             "App just opened (nothing selected).",
             ["Press START with no part selected.", "Click CPU, choose YOUR CODE, leave PROGRAM empty, press START.",
              "Type a program that doesn't exist (`C:\\no\\such\\program.exe`), press START."],
             "Each request shows a pop-up that says what is wrong; nothing is shown as running and no telemetry "
             "stays on screen."),
    "AT-8": ("YOUR CODE: your own program and its bottlenecks", "Ultimate goal (SRS VI.3)",
             "`examples/demo_project` and its requirements (numpy) installed.",
             ["Click CPU.", "Choose YOUR CODE.", "PROGRAM: `examples/demo_project/main.py`.", "Press START."],
             "The program runs with its project's Python and folder (shown under PROGRAM); its output appears in "
             "the log intact; when it exits the summary shows the exit code, every reading and the bottlenecks."),
    "AT-9": ("Hot spots in VS Code", "Ultimate goal (SRS VI.3)",
             "VS Code with the HCV Hot Spots extension, open on `examples/demo_project`; its eye menu (right-click the yellow ▶) on Everything, the default - the speed boxes are hidden in the Memory-only views.",
             ["Profile the demo project (the yellow ▶ in VS Code; the test runs `profiler.py` the same way).",
              "Open `app/compute.py` and `app/loader.py`."],
             "The slowest lines are boxed and ranked (#1, #2 …) with their share of the run; lines below each "
             "other form one box; the profile also lists the bottlenecks."),
    "AT-10": ("Compare two runs and replay a recording", "Stretch goals (SRS VI.2)",
              "At least two recorded runs (from the cases above).",
              ["Press COMPARE.", "Close it; REPLAY the 2D CPU run of AT-3."],
              "COMPARE shows two runs side by side with every average and its change, and a metric over time; "
              "REPLAY plays the recording through the diagram with an amber `REPLAY (recorded)` clock and ends "
              "with `Replay ended`."),
    "AT-11": ("Other parts: RAM bandwidth, disk I/O, PCIe bus, ML", "UC-5, UC-9; stretch goals",
              "NVIDIA GPU with PyTorch (ML, PCIe).",
              ["RAM + 2D, full 30 s.", "DISK + DISK I/O.", "BOARD + PCIe.", "GPU + ML. START each and wait for the end."],
              "Each test completes with its own measured result (GB/s, MB/s, PCIe GB/s, samples/s and GFLOPS); "
              "the RAM summary is compared with the previous (stopped) RAM run of AT-6."),
}
ORDER = list(CASES)


def first_lines(text, n):
    return "\n".join(text.splitlines()[:n])


def bottleneck_part(summary):
    i = summary.find("Bottlenecks")
    return summary[i:].split("\n\nRecorded")[0].strip() if i >= 0 else ""


def observed(r):
    """Short observed result, from what the run recorded."""
    out = []
    if "status" in r and r["status"]:
        out.append(f"Status line: `{r['status']}`")
    if r.get("detected"):
        out.append("Detected: " + "; ".join(f"{k.upper()} {', '.join(v)}" for k, v in r["detected"].items()))
    if r.get("clock_while_running"):
        out.append(f"Clock while running: `{r['clock_while_running'].strip()}`")
    if "status_after_reselect" in r:
        out.append(f"After GPU: `{r['status_gpu']}`; clicking GPU again: `{r['status_after_reselect']}`; "
                   f"after CPU: `{r['status_cpu']}`")
    if r.get("popups"):
        out.append("Pop-ups said: " + " / ".join(f"\"{p}\"" for p in r["popups"]))
    if "running" in r:
        out.append(f"Shown as running afterwards: {'yes' if r['running'] else 'no'}")
    if r.get("stop_took_s") is not None:
        out.append(f"STOP took {r['stop_took_s']} s to end the run")
    if r.get("compare_title"):
        out.append(f"COMPARE window opened (\"{r['compare_title']}\"); replayed `{r['replay_file']}`; clock during "
                   f"replay: `{r['clock_during_replay'].strip()}`")
    if r.get("top_lines"):
        out.append("Profiler result: " + r.get("result", "?") + "; top lines: " +
                   ", ".join(f"`{f}:{ln}` {100 * sh:.0f} %" for f, ln, sh in r["top_lines"]) +
                   "; findings: " + (", ".join(r.get("findings") or []) or "none"))
    if r.get("slowest_ui_reply_s") is not None:
        out.append(f"The window answered every request within {r['slowest_ui_reply_s']} s")
    return out


def main():
    with open(RESULTS, encoding="utf-8") as f:
        results = {r["at"]: r for r in json.load(f)}
    passed = sum(1 for a in ORDER if a in results and "error" not in results[a])
    L = ["# Acceptance test cases", "",
         "Each case is a use case (or goal) of the SRS v1.1 run on the real app, on real hardware - nothing is",
         "simulated. The cases were executed by `tests/acceptance/run_acceptance.py`, which opens the app, calls",
         "the same handlers the buttons and the diagram are wired to, waits for the app's real reaction, and",
         "captures the window and every pop-up (Win32 `PrintWindow`; pop-ups from the screen). The observed",
         "results below are what that run recorded (`tests/work/acceptance/results.json`); this page is written",
         "from them by `tests/acceptance/write_doc.py`. Each case can also be done by hand with the steps given.", "",
         f"**Result of the last run: {passed} of {len(ORDER)} passed.** Machine: "
         + (", ".join(f"{', '.join(v)}" for v in results.get("AT-1", {}).get("detected", {}).values()) or "?") + ".", "",
         "Not covered here, and why: **UC-12** (telemetry lost) and **UC-14** (thermal abort) can't be triggered "
         "safely on real hardware - pulling a sensor or overheating a part - so they are verified by the unit tests "
         "(`tests/unit/test_controller.py`, `SafetyTest`). **UC-2** (remote machine) was withdrawn in SRS v1.1.", "",
         "**Observations from the run.** While the 2D test keeps every CPU core at 100 % (AT-3), the window itself "
         "gets less CPU: the clock read 40-57 FPS and 16-20 sensor samples per second instead of 60 / 20. The "
         "10 Hz minimum of NFR-1 holds (the clock was recorded in AT-3 only; three runs gave 57 / 20, 49 / 17 and 40 / 16). "
         "Three defects were found by these runs and fixed, each with a regression test: doubled backslashes in an "
         "error pop-up (AT-7), garbled non-ASCII program output (AT-8), and a CPU-only test reported as "
         "\"GPU waiting on the CPU\" when another program used the GPU (AT-3). The driver itself had a defect: "
         "taking `PrintWindow` screenshots from a worker thread stalled Tk's timers for seconds at a time; it now "
         "captures on the window's own thread (every request answered in under 0.2 s).", "",
         "| Case | Covers | Title | Result |", "|---|---|---|---|"]
    for a in ORDER:
        t, cov = CASES[a][0], CASES[a][1]
        r = results.get(a)
        res = "not run" if r is None else ("**fail**" if "error" in r else "pass")
        L.append(f"| [{a}](#{a.lower()}) | {cov} | {t} | {res} |")
    L.append("")
    for a in ORDER:
        title, cov, pre, steps, expected = CASES[a]
        r = results.get(a, {})
        L += ["---", "", f"## {a}", "", f"### {title}", "",
              f"**Covers:** {cov}  ", f"**Preconditions:** {pre}", "", "**Steps**", ""]
        L += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
        L += ["", f"**Expected:** {expected}", ""]
        if not r:
            L += ["**Observed:** not run.", ""]
            continue
        if "error" in r:
            L += [f"**Observed: FAILED** - {r['error']}", ""]
        else:
            L += ["**Observed:** pass.", ""] + [f"- {o}" for o in observed(r)] + [""]
        summ = r.get("summary") or ""
        if summ:
            L += ["Summary (first lines, and the bottleneck finder's part):", "", "```", first_lines(summ, 3)]
            b = bottleneck_part(summ)
            if b:
                L += ["...", b]
            L += ["```", ""]
        for name, d in (r.get("results") or {}).items():
            L += [f"`{name}`: {d['status']}", "", "```", first_lines(d["summary"], 1)]
            m = re.findall(r"^(?:Memory bandwidth|Disk throughput|Bus transfer|ML training speed|Compute|Frame time).*$",
                           d["summary"], re.M)
            L += m + [bottleneck_part(d["summary"]), "```", ""]
        shots = r.get("screenshot") or []
        shots = [shots] if isinstance(shots, str) else shots
        if shots:
            L += ["**Screenshots**", ""]
            for s in shots:
                rel = os.path.relpath(os.path.join(ROOT, s), os.path.dirname(OUT)).replace("\\", "/")
                L += [f"![{os.path.basename(s)}]({rel})", "", f"*{os.path.basename(s)}*", ""]
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")
    print(f"wrote {os.path.relpath(OUT, ROOT)}: {passed}/{len(ORDER)} passed")


if __name__ == "__main__":
    main()
