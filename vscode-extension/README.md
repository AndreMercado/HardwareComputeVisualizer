# HCV Hot Spots (VS Code extension)

Run a Python file and see, in the editor, which code takes the most time: the slowest blocks
get coloured boxes (#1 red, #2 orange, #3 yellow, ...) with a label such as

    ◀ #1 · 60% of the run time · 4.5 s · 2 lines · 1 CPU core maxed, the rest idle

The boxes fill in **live** while the program runs (the profiler saves its results every second)
and come back whenever you open the file again, until the next run or **HCV: Clear the boxes**.
Hover a label for the line-by-line breakdown.

## Commands (yellow ▶ and flame buttons in the editor title, right-click menu, or Ctrl+Shift+P)

| Command | What it does |
|---|---|
| **Yellow ▶** HCV: Run + color everything | A normal run in a terminal (your program's output there, `input()` works) with the profiler and its memory test around it: time boxes, blue memory boxes and the crash line all drawn. Stop: the yellow status-bar item or Ctrl+C in the terminal. |
| HCV: Profile this file | Runs the file under `profiler.py` and boxes the hot spots. |
| HCV: Profile this file with memory | Also boxes (blue, dashed) the lines whose objects held the most memory. Slower. |
| HCV: Run in the Hardware Visualizer | Runs it as YOUR CODE in the desktop app (started and connected by itself), so the diagram shows the hardware while the boxes fill in. |
| HCV: Go to a hot spot | Jumps to one of the boxed blocks. |
| HCV: Stop profiling / Clear the boxes | |

## Choose what to show (eye button)

Next to the yellow ▶ is an **eye** dropdown (also: right-click in the code > *HCV: What to show*).
Tick on and off what the code shows - remembered across sessions:

| Layer | Shows |
|---|---|
| Speed | boxes around the slowest code (#1 red, #2 orange, ...) |
| Speed of every other line | the faint `2.1% · 80 ms` marks |
| Memory | the lines holding the most memory, ranked like speed: `📦 #1 memory · 63 MB held`, a blue wash (deepest for #1) and a dashed box; hover says its rank in the whole run and that line's speed. Needs a yellow ▶ run (it measures memory). |
| Totals above each function | `🔥 34% of the run · 1.2 s` and `📦 78 MB held` above each `def`, the run summary on line 1 |
| Bottlenecks | the `⚠ serial code / waiting / disk...` notes |
| Hardware in the labels | cores busy, GPU, °C, W in labels and hovers |
| Crash line | the 💥 box |

Presets: **Everything**, **Speed only**, **Memory only**, **Speed + memory**. **HCV: Choose what to
show…** opens the same as a checklist. **HCV: Where in the code?** lists the slowest lines and the
lines holding the most memory across every file of the run - pick one to jump there.

(VS Code doesn't let extensions add a right-click menu to toolbar buttons, so the menu is the eye
button beside ▶ and the right-click menu inside the code.)

## What you see in the files

Everything one run records is linked to the lines that were running:

| Where | What |
|---|---|
| **Boxes** (#1 red, #2 orange, #3 yellow...) | the slowest blocks: share of the run, time, how many cores *this program* kept busy, GPU (only if this program used it), and `⚠ <bottleneck>` when one held there. Hover: per-line table with the hardware while each line ran, the bottleneck and what to do about it. |
| **Faint `2.1% · 80 ms`** after other lines | every other line that took ≥ 0.5% (`hcv.lineMinShare`); orange with `⚠` where a bottleneck held. Hover for the details. Off with `hcv.showAllLines`. |
| **Lens above each `def`** | `🔥 34% of the run · 1.2 s · 3 lines · ⚠ serial code` - the function's total (comprehensions and lambdas count for the function they're written in). |
| **Lens on line 1** | the run: how it ended, time, engine, threads, main bottleneck. Click: the run report. |
| **Blue dashed boxes / 💥** | memory held per line (yellow ▶ / memory run) / the crash line. |
| **Run report** (`HCV: Open the run report`, the lens, or a hover link) | `.hcv/report.md` beside the profile: bottlenecks with the lines running while they held, hardware averages, time per function, the 60 hottest lines, memory - every line a link into the code. |

**Bottlenecks** are the Hardware Visualizer's own finder (`analysis.py`: serial code, GPU waiting on
the CPU, PCIe busy, disk busy, RAM full, waiting; plus good use: all cores / GPU fully busy) run on
the profiler's hardware samples (every 0.1 s: each core, RAM, disk, and with an NVIDIA GPU its load,
VRAM, temperature, power and PCIe traffic). A 0.1 s timeline of which line was running ties each
moment to a line. The GPU only counts when **this program** used it (NVML lists its process, or its
VRAM / load clearly rose), so a browser or the desktop using the GPU doesn't read as "GPU waiting".

**With the Hardware Visualizer** - the desktop app ships inside the extension and connects by itself:

- **HCV: Open the Hardware Visualizer** (eye menu) starts it with your Python. No settings, no AI CONTROL:
  while it is open it publishes a local link (`%LOCALAPPDATA%\HCV\app.json`: port + token, this PC only)
  and the extension finds it.
- **Watch in the Hardware Visualizer** (eye menu, a tick) - the yellow ▶ then runs your file through the
  app (starting it if needed): the diagram shows the hardware live while the boxes fill in here.
- When the run ends the app links its recording into the profile, so lines also show the sensors only it
  reads - CPU temperature and power (LibreHardwareMonitor), chipset temperature, disk counters - and its
  findings. Its summary lists your slowest lines; **click one to jump to it in VS Code**.
- The app keeps its runs in `%LOCALAPPDATA%\HCV\runs` when started from the extension (from a copy of
  the repo: the repo's `runs/` as before). Missing Python packages: the extension offers **Install the
  tools** (psutil, py-spy, nvidia-ml-py, moderngl).

## How the time is counted

The profiler notes which line *of your project* is running, many times a second. Time inside
libraries (numpy, PyTorch, the standard library) counts for the line of yours that called them,
so `y = model(x)` gets the time the model took. Lines under each other form one box: directly below
one another, with only blank or comment lines between, or one untimed line apart in the same function
(a generator expression counts for its function). The same goes for the faint marks (one dashed grey
box) and the memory highlights (one blue block). "What the hardware was doing" is how many cores your program kept busy and the GPU load,
measured while those lines ran.

- **Exact numbers: install py-spy** (`pip install py-spy`). It samples from outside the program, so
  long C calls (sort, regex, json, ...) are charged to the right line, and threads and child
  processes (multiprocessing) are included - their times add up. Tested on 12 kinds of programs:
  91-105 % of the real time on the right line.
- **Without py-spy** the built-in sampler is used and labels start with `≈`: it can only look when
  Python switches threads, so time inside long C calls can land on the next line and short
  functions on their `def` line. The live boxes during a run always use it; with py-spy the exact
  numbers replace them when the program ends.
- **Imports** that take long are labelled "one-time import cost".
- **Crashes:** the line where your code failed gets a dotted red box with the error.
- **Modules:** opening any module of the project that the run used shows its boxes too, wherever it
  lives (a package, `src/`, a sibling folder): the newest profile in the workspace with lines in it is used.
- **asyncio:** time spent awaiting (sleeps, network) is counted on the `asyncio.run(...)` line,
  because a waiting coroutine isn't running anywhere; the hover says so.
- **Restricted Mode** (untrusted folder): saved boxes are shown, but profiling (which runs the code)
  needs a trusted folder.

## Projects with many files and folders

A run works like VS Code's own *Run Python File*:

- **Python:** the interpreter picked in the Python extension (your project's venv), unless
  `hcv.pythonPath` is set to something other than `python`.
- **Working folder:** the workspace folder, so `open("data/input.csv")` finds the same files
  (the file's own folder when `python.terminal.executeInFileDir` is on).
- **Your code** = every file in the workspace folder (`--root`), except virtual environments
  (any folder with `pyvenv.cfg`, conda envs), `site-packages`, `.tox` / `.nox` and the Python install
  itself: time inside those counts for your line that called them. Without a workspace (a loose
  file, or `profiler.py` from a terminal) the project is the nearest folder above the file with
  `.git`, `pyproject.toml`, `setup.py`, `requirements.txt`, `.vscode`, ... - or the file's own folder.
- **Packages:** a file that uses relative imports (`from .core import x`), or a package's
  `__main__.py`, is run like `python -m pkg.module`, so it works instead of failing with
  "attempted relative import with no known parent package".
- The script's folder is first on `sys.path`, as with `python script.py`; the visualizer's own
  modules never hide yours (a `analysis.py` or `control.py` of yours is the one imported).
- Each run writes `.hcv/profile.json` next to the script it ran (one per folder: running another
  script in the same folder replaces the boxes). `.hcv/` gets its own `.gitignore`.

Tested on 16 layouts (flat folder, package tree, `src/` layout, relative imports, `__main__.py`,
data paths relative to the project, name clashes, venv in the project, spaces and accents in
folder names, crash deep in a package, multiprocessing, threads, script 7 folders deep, monorepo,
`input()`, `__file__`) with both engines.

## Install

**From the file:** in VS Code open Extensions (Ctrl+Shift+X) > `···` menu > **Install from VSIX…** and pick
`hcv-hotspots-0.2.0.vsix` (or run `code --install-extension hcv-hotspots-0.2.0.vsix`). That's all: the
profiler ships inside the extension. Open a `.py` file and press the yellow ▶.

**Optional Python tools** - the first run offers to install them with one click (or run
**HCV: Install the optional Python tools**): `psutil` (hardware readings), `py-spy` (exact line times),
`nvidia-ml-py` (NVIDIA GPU readings). They go into the Python that runs your code (the interpreter picked
in the Python extension).

**Build it yourself** (from this repo): `powershell -ExecutionPolicy Bypass -File vscode-extension/build.ps1`
makes `vscode-extension/dist/hcv-hotspots-<version>.vsix`; `install.ps1` builds and installs it.

Settings: `hcv.pythonPath`, `hcv.maxBoxes`, `hcv.minShare`, `hcv.showAllLines`, `hcv.lineMinShare`,
`hcv.codeLens`, `hcv.maxMemoryBoxes`, `hcv.memoryMinBytes`, and `hcv.visualizerFolder` (your own copy of
the app to start instead of the bundled one - normally empty).
