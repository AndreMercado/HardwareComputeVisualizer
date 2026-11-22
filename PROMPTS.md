# Prompts

Two sets of prompts relate to this project: the ones used to build it with Claude Code, and the ones the app's AI agent uses at runtime.

## 1. Build prompts (typed to Claude Code, in order)

Source: session transcripts in `~/.claude/projects/C--Users-andre-Desktop-HardwareComputeVisualizer/`. Image-only messages are omitted.

### Session 1 - building the app (2026-10-28)
1. "add themain parts of this oprject work on itseit i up asmuch aprts dont worry aobu tdlets just makeit work the baisc sutff"
2. "can you run it"
3. "can you add some visual detials such youknow of gaming software hwo its linees steelrezor black gaign aesthing can you dhat keep onworkng to the tohers parts" (with screenshot)
4. "keep working it yes" (with screenshot)
5. "AI Benchmark Mode:" (followed by pasted SRS text, with screenshot)

### Session 2 - class diagram and cleanup (2026-10-29)
1. "You need to have like code that create a class diagram that updates as you make new sutff hadd that to the code"
2. "just add the claude code hook"
3. "clean up the code"

Result: `gen_diagram.py` plus a `PostToolUse` hook in `.claude/settings.json` that regenerates `CLASS_DIAGRAM.md` after every edit.

### Session 3 - running it and questions (2026-10-29)
Launched the app, located the class diagram, and asked about the prompts, `.py` files, telemetry, working tests, the 3D workload, and the benchmark sequence.

### Session 4 - stretch goals (2026-11-11)
1. "the hardwarecompute folder keep working on teh project in there"

Result: storage added as a fourth part (disk active time, read/write MB/s) with a Disk I/O test, an ML workload (PyTorch training on GPU or CPU), and every run recorded to `runs/` so it can be compared with the previous run and replayed. `README.md` written.

### Session 5 - finishing the "not built yet" list (2026-11-18)
1. "for the hardware tester can you finsih that"

Result: remote machines over SSH (UC-2: `remote.py` + `remote_agent.py`, key login only, 10 s watchdog, agent removed on disconnect), motherboard/board as a fifth part (PCIe link load and traffic from NVML) with a PCIe bus test, CPU power from the Windows RAPL counter, CPU / chipset temperature from LibreHardwareMonitor when it runs, and AMD / Intel GPU load and memory from the Windows GPU counters. Workloads now time themselves with the monotonic clock.

### Session 6 - the remaining features (2026-11-18)
1. "dont do thelinux right now just finish all the features"

Result: YOUR CODE test (runs the user's own program locally or remotely), `analysis.py` bottleneck finder (serial code, GPU starved, PCIe / disk / RAM bound, idle) in every summary, COMPARE window for any two runs, per-core bars in the CPU box, GFLOPS for the ML test, a live FPS / sensor-rate readout with a 1 ms timer for real 60 FPS, and a fix for the 3D test (its camera passed through a sphere, making early frames fake-fast).

### Session 7 - AI control (2026-11-18)
1. "can you set it up where an ai can controll this and i can see it being controlled"

Result: AI CONTROL button and `--ai-control` flag, `control.py` (local token-protected JSON API; every AI action shown in a purple banner, flashed and logged before it happens), `mcp_server.py` + `.mcp.json` so Claude can drive the app as MCP tools.

### Session 8 - hot spots in the editor (2026-11-18)
1. "...i want it where when it runs the code i know the parts of the code that are taking up the space running time ... make it visual studio extension ... add squares blocks of code this spend the most time here ... can you do that"

Result: `profiler.py` (sampling line profiler: time per line of the user's code, what the program's own CPU use and the GPU were at those moments, optional memory per line; results saved every second) and the `vscode-extension/` HCV Hot Spots extension that draws ranked boxes around the slowest blocks, live, and can run the file inside the visualizer. Example: `examples/hotspots.py`.

### Session 9 - testing the profiler on many programs (2026-11-18)
1. "merge it and test it with many different python code files to see if it can be improved"

Result: merged to main; 12 test programs (C calls holding the GIL, threads, multiprocessing, asyncio, recursion, classes, multi-file, GPU, file I/O, crash) with measured true times showed the in-process sampler misplaces time in C calls and short functions, so `profiler.py` now uses py-spy for the final numbers when installed (91-105 % of the true time on the right line), keeping the built-in sampler for live boxes and hardware readings. The extension finds profiles for imported modules, marks approximate numbers, shows crash lines and import costs, and works in Restricted Mode.

### Session 10 - yellow run button, many projects, one-click install (2026-11-21, typos fixed in the quotes)
1. "hardware project add a special run button in the vs code extension make it yellow which when i hit that does a normal run with the extra test and adds all the coloring in the vs code file"
2. "can you do a bunch of testing with many code files projects so that it works like how does it work in folder system of projects"
3. "did you link all the info we gather and put it in the extension to see in the files in vs code"
4. "on the yellow run button i want to be able to right click it ... it allows me to pick which data i want to see and the highlight part is memory speed and where in the code it is"
5. "when doing lines and boxes make it where if they're below each other make it one big box"
6. "how to make it one easy extension to use ... where i just hit install and it's there"
7. "the other desktop application in python and this hcv hotspot how should we connect them" / "make it where they connect a lot easier and it's easier to package, other people have to use this"
8. "hardware compute visualizer program are there any other things we should do to make it more friendly" / "1, 2, 3, 6 first do this"
9. "add all the commits"

Result: the yellow ▶ (run in a terminal + profile + boxes), the eye menu (choose speed / memory / "where in the code"), adjacent lines merged into one box, the extension packaged as a `.vsix` that carries its own copy of the app, an automatic editor link between the extension and the desktop app (`app.json` + token), 20 test projects (`tests/projects.py`) run through the real extension (`tests/e2e.js`), the multi-file demo project, live idle readings, guided steps and "why is this reading missing" fixes in the app. Commits `ff534b6` … `e3b432f`.

### Session 11 - verification and the course deliverables (2026-11-21 / 22, typos fixed in the quotes)
1. "is the hardwarecompute fully added to the git" / "what about github" / "would you say the project is fully done"
2. "yes" (fix the bug the test run found and push)
3. "open it up the vs code so i can test it" / "i hit run and got terminal process"
4. (pasted the final presentation assignment and its deliverables list) / "yes do all of it"

Result: running the full test suite before answering "is it done" showed one failing group; the cause was a regression in `e3b432f` (YOUR CODE started with `sh -c` on Windows when no folder was given), fixed in `348ec68` with a regression test. The "terminal process" message was the demo program's `ModuleNotFoundError: numpy` under a VS Code interpreter without numpy (not an app bug). Then, each on its own branch merged into `main`: unit tests for the Model and Controller classes (`tests/unit/`, 161 cases, coverage report), statecharts and the architecture / algorithms document with UML diagrams (`docs/`), the acceptance test run with screenshots (`tests/acceptance/`, `docs/ACCEPTANCE_TESTS.md`), and the presentation.

## Development process and the context given to the agent

The app was built with Claude Code, one goal per session. The steps each session followed:

1. **Requirements before code.** `ProjectInfo.md` (purpose, MVP, stretch goals, constraints) and the
   SRS (`SRS_HardwareComputeVisualizer.md`: 14 use cases with main and alternate paths, FR-1 … FR-23,
   NFRs, traceability table) were written first. Sessions point the agent at them ("AI Benchmark Mode:"
   followed by the SRS section; "finish the not built yet list" = the SRS roadmap).
2. **Context given to the agent:** the SRS and `ProjectInfo.md` in the repo; the source itself; the
   class diagram `CLASS_DIAGRAM.md`, kept in sync with the code by a Claude Code hook
   (`.claude/settings.json`: after every Edit/Write, `python gen_diagram.py`); screenshots of the running
   app when something looked wrong; `.mcp.json`, which lets the agent drive the real app (AI CONTROL)
   to check its own work; test output.
3. **Small steps, each checked.** The agent implements one goal, runs the app or the tests, the result
   is looked at (often from a screenshot), and only then committed. Bugs found get a test first
   (example: the regression test in `tests/unit/test_workloads.py` for `348ec68`).
4. **Verification and validation at four levels:** unit tests (Model / Controller classes), integration
   tests (profiler on 20 projects), system tests (the real VS Code extension + profiler + app), and
   acceptance tests (the SRS use cases on the real app, with screenshots). See `tests/README.md` and
   `docs/ACCEPTANCE_TESTS.md`.
5. **Version control:** feature branches (`unit-tests`, `design-docs`, `acceptance-tests`,
   `presentation`) merged into `main` with merge commits, pushed to GitHub.

## 2. AI Benchmark Mode prompts (`agent.py`)

### Message sequence (`agent.py:main`)
1. **System:** `SYSTEM` (rules, tools, "call finish with a summary when done").
2. **User:** `Task: <goal text>` (one of the three `GOALS`).
3. **Assistant:** model reply, usually a tool call such as `run_python(code)`.
4. **Tool:** result as `exit code N / stdout / stderr` (cut to 2500 chars). Back to step 3.

The loop runs up to 30 steps or until the time limit (10 min default). It ends when the model calls `finish(summary)` (result: completed), replies without a tool call five times in a row, or hits the step or time limit (result: agent stopped without finishing / time limit reached).

### System prompt (`SYSTEM`)
> You are an autonomous agent working on a task on this Windows PC. You act only through the tools provided. You have a private working folder; run_python executes Python 3.13 code inside it. Installed: numpy and PyTorch (CUDA GPU available via torch.cuda). There is no internet, no shell and no access outside the working folder; if something is refused, adapt. Work in small steps, run code, read the output, fix errors. Keep each run under about 90 seconds. Output only appears if the code calls print() - a bare expression on the last line shows nothing. When the task is done, call finish with a 2-3 sentence summary of what you did and the numbers you measured.

### Goals (`GOALS`)
| Id | Title | Task text |
|---|---|---|
| `classifier` | Train a tiny image classifier | Train a small neural-network image classifier. Generate a synthetic image dataset yourself (for example 10,000 28x28 images of simple shapes or patterns in 4 classes), train a small CNN with PyTorch on the GPU for several epochs, then report the test accuracy and the training time. |
| `render` | Render a scene | Render a 3D scene. Using numpy only (no other graphics libraries), ray-trace a scene with several shaded spheres and a floor at 640x480 and save it as image.ppm in your working folder. Report how long the render took. |
| `matmul` | Crunch big matrices | Compare CPU and GPU speed. Time large matrix multiplications (for example 4096x4096 float32) with numpy on the CPU and with PyTorch on the GPU, repeating enough times to get stable numbers, then report the speedup. |

To add a goal, add one entry to `GOALS`: `"id": ("Short title", "Full task text")`.

### Tools offered to the model
`run_python`, `write_file`, `read_file`, `list_files`, `finish`.

### Built-in extra messages
| When | Text |
|---|---|
| Model replies with no tool call (up to 4 times in a row, the 5th stops the run) | `You replied without calling a tool. Call run_python with the next code to run, or call finish if the task is done.` (user message) |
| Code ran but printed nothing | `(The code printed nothing. Use print() to see values.)` appended to the tool result |
| A run exceeds its time limit | `TIMEOUT: the run was stopped after N s. Use less work per run.` |
| Sandbox refuses an action | `BLOCKED: <reason>` |

### Model settings
Models tried in order: `llama3.2:3b`, `qwen2.5:7b`; if neither is installed, the smallest installed model that Ollama says can call tools. For "thinking" models, thinking is turned off (`think: false`) so each step is a quick tool call. `num_ctx` 8192, `num_predict` 1500, `temperature` 0.3.

## 3. Where else to look
- Benchmark sequence (start -> poll -> render -> stop): `SRS_HardwareComputeVisualizer.md` section 3; page 9 of `Group19_SRS_HardwareComputeVisualizer.pdf`.
- Past AI runs (code the model wrote, one `step_N.py` per step): `ai_workspace/run-*/`. The model's chat messages are not saved.
