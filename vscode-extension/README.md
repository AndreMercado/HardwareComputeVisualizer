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

Every 5 ms the profiler notes which line *of your project* is running. Time inside libraries
(numpy, PyTorch, the standard library) counts for the line of yours that called them, so
`y = model(x)` gets the time the model took. Lines next to each other in the same function form
one block. "What the hardware was doing" is CPU, busiest core and GPU, sampled 10x a second
while those lines ran. Only the main thread is profiled; child processes are not.

## Install

    powershell -ExecutionPolicy Bypass -File vscode-extension/install.ps1

then reload VS Code. Settings: `hcv.pythonPath`, `hcv.visualizerFolder`, `hcv.maxBoxes`, `hcv.minShare`.
