# Tests

    python tests/run_all.py            # everything except the desktop-app test
    python tests/run_all.py --quick    # a few projects of each kind
    python tests/run_all.py --app      # also the yellow ▶ through the real desktop app (its window opens)

    python tests/unit/run.py              # unit tests only (~20 s)
    python tests/unit/run.py --coverage   # with statement + branch coverage (pip install coverage)

Needs Node.js for the extension tests. Test projects are written to `tests/work/` (git-ignored).
Windows-first: process trees are stopped with `taskkill`.

There are three levels:

1. **Unit tests** (`tests/unit/`, Python `unittest`): one class or function at a time, on the Model
   and Controller classes. Hardware, Ollama and SSH are replaced by fakes where needed.
2. **Integration tests** (`run_tests.py`, `vis_test.py`): the profiler and YOUR CODE on 20 real
   test projects.
3. **System tests** (`e2e.js`): the real VS Code extension, the profiler and (with `--app`) the
   desktop app together.

Acceptance tests (the SRS use cases, run by hand on the real app with screenshots) are in
`docs/ACCEPTANCE_TESTS.md`.

## Unit tests

| File | Class under test | What it checks |
|---|---|---|
| `test_analysis.py` | `analysis` (Model) | each bottleneck pattern and its threshold, the 1 s averaging window, the 10 % minimum share, ordering (problems first), report text |
| `test_telemetry.py` | `telemetry.Poller`, LHM parsing (Model) | LibreHardwareMonitor tree parsing (comma decimals, units, kinds), sensor choice, chipset sensor never guessed, RAPL energy-weighted watts, the live Poller's rate (≥ 15 samples/s) and value ranges |
| `test_workloads.py` | `workloads` (Model) | deterministic 2D frame, every UI test has a runner, JSON output contract, error = one line + exit 2; YOUR CODE: folder, Python choice, exit code, heartbeat, time limit kills the child processes too, and the regression test for the Windows launch bug |
| `test_agent.py` | `agent`, `sandbox_run` (Model) | path escape blocked, file tools, model choice, the agent loop with a scripted model (finish, nudges, step limit, time limit, blocked paths, tool errors); the sandbox blocks network, programs, and reads / writes / deletes / listings outside the working folder |
| `test_remote.py` | `remote.RemoteTarget`, `RemoteProc`, `RemotePoller` (Model) | SSH options (key only), error messages, connect failures (no Python, old Python, odd folder), message dispatch, lost connection fails the run, stop confirmed vs forced, stale telemetry = unavailable |
| `test_control.py` | `control.ControlServer` (Controller, external API) | token required, unknown actions, JSON body checks, requests reach the UI thread, `wait`, localhost only, the editor link's smaller action set |
| `test_profiler.py` | `profiler` helpers (Model) | project folder detection, when to run as `python -m`, GPU readings removed for programs that never used the GPU |
| `test_controller.py` | `main.App` (Controller) | START validation (every refusal of UC-13), launching the workload and poller, how a finished run is classified (UC-9, UC-7), thermal abort (UC-14), telemetry lost (UC-12), STOP and force-stop (UC-8, FR-10), agent event routing, saving runs and finding the previous one, pop-ups under AI control, replay timing |

Coverage (statement + branch, `--coverage`): `analysis` 99 %, `agent` 84 %, `control` 82 %,
`remote` 77 %, `telemetry` 49 % (the rest reads Windows / NVIDIA hardware), `main` 23 % (most of
`main.py` draws the window; the controller methods above are covered). `sandbox_run.py` shows
0 % because its audit hook also blocks coverage from saving its data; it is checked from outside
by the 9 sandbox tests in `test_agent.py`.

## Integration and system tests

| File | What it tests |
|---|---|
| `projects.py` | 20 test projects: flat folder, package tree, `src/` layout, relative imports, `__main__.py`, data paths relative to the project, name clashes with the visualizer's modules, venv inside the project, spaces and accents in folder names, crash deep in a package, multiprocessing, threads, script 7 folders deep, monorepo, `input()`, `__file__`, many lines, a visualizer-linked run, PyTorch on the GPU (needs CUDA), lines under each other |
| `run_tests.py` | `profiler.py` on every project the way the extension runs it: which file got the time, crash line, memory, nothing from venvs or the visualizer counted |
| `e2e.js` | the real `vscode-extension/extension.js` against a stand-in for VS Code (Node): presses the yellow ▶, the flame and the eye-menu items, waits for the run, opens every file and checks the boxes, memory highlights, faint lines, lenses, hovers, run report, "where in the code" and one box for lines under each other; `viaApp` runs through the desktop app |
| `vis_test.py` | the command the extension sends to the desktop app, run the way the app's YOUR CODE runs it |
| `build_only.py` | writes the projects to `tests/work/` for `e2e.js` and `vis_test.py` |
| `run_all.py` | runs all of the above and prints a summary; exit code 0 = all passed |
