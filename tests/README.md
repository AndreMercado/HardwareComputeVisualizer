# Tests

    python tests/run_all.py            # everything except the desktop-app test
    python tests/run_all.py --quick    # a few projects of each kind
    python tests/run_all.py --app      # also the yellow ▶ through the real desktop app (its window opens)

Needs Node.js for the extension tests. Test projects are written to `tests/work/` (git-ignored).
Windows-first: process trees are stopped with `taskkill`.

| File | What it tests |
|---|---|
| `projects.py` | 20 test projects: flat folder, package tree, `src/` layout, relative imports, `__main__.py`, data paths relative to the project, name clashes with the visualizer's modules, venv inside the project, spaces and accents in folder names, crash deep in a package, multiprocessing, threads, script 7 folders deep, monorepo, `input()`, `__file__`, many lines, a visualizer-linked run, PyTorch on the GPU (needs CUDA), lines under each other |
| `run_tests.py` | `profiler.py` on every project the way the extension runs it: which file got the time, crash line, memory, nothing from venvs or the visualizer counted |
| `e2e.js` | the real `vscode-extension/extension.js` against a stand-in for VS Code (Node): presses the yellow ▶, the flame and the eye-menu items, waits for the run, opens every file and checks the boxes, memory highlights, faint lines, lenses, hovers, run report, "where in the code" and one box for lines under each other; `viaApp` runs through the desktop app |
| `vis_test.py` | the command the extension sends to the desktop app, run the way the app's YOUR CODE runs it |
| `build_only.py` | writes the projects to `tests/work/` for `e2e.js` and `vis_test.py` |
| `run_all.py` | runs all of the above and prints a summary; exit code 0 = all passed |
