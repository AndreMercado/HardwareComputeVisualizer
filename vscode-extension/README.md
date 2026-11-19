# HCV Hot Spots (VS Code extension)

Run a Python file and see, in the editor, which code takes the most time: the slowest blocks
get coloured boxes (#1 red, #2 orange, #3 yellow, ...) with a label such as

    ◀ #1 · 60% of the run time · 4.5 s · 2 lines · 1 CPU core maxed, the rest idle

The boxes fill in **live** while the program runs (the profiler saves its results every second)
and come back whenever you open the file again, until the next run or **HCV: Clear the boxes**.
Hover a label for the line-by-line breakdown.

## Commands (flame button in the editor title, right-click menu, or Ctrl+Shift+P)

| Command | What it does |
|---|---|
| HCV: Profile this file | Runs the file under `profiler.py` and boxes the hot spots. |
| HCV: Profile this file with memory | Also boxes (blue, dashed) the lines whose objects held the most memory. Slower. |
| HCV: Profile in the Hardware Visualizer | Runs it as YOUR CODE in the visualizer (AI CONTROL must be on), so the diagram shows the hardware while the boxes fill in. |
| HCV: Go to a hot spot | Jumps to one of the boxed blocks. |
| HCV: Stop profiling / Clear the boxes | |

## How the time is counted

The profiler notes which line *of your project* is running, many times a second. Time inside
libraries (numpy, PyTorch, the standard library) counts for the line of yours that called them,
so `y = model(x)` gets the time the model took. Lines next to each other in the same function form
one block. "What the hardware was doing" is how many cores your program kept busy and the GPU load,
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
- **Modules:** opening a module imported by the profiled script shows its boxes too.
- **asyncio:** time spent awaiting (sleeps, network) is counted on the `asyncio.run(...)` line,
  because a waiting coroutine isn't running anywhere; the hover says so.
- **Restricted Mode** (untrusted folder): saved boxes are shown, but profiling (which runs the code)
  needs a trusted folder.

## Install

    powershell -ExecutionPolicy Bypass -File vscode-extension/install.ps1

then reload VS Code. Settings: `hcv.pythonPath`, `hcv.visualizerFolder`, `hcv.maxBoxes`, `hcv.minShare`.
