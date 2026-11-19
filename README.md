# HardwareComputeVisualizer

A desktop app that shows a PC's hardware actually doing work. Pick a machine, a part and a
test, press Start: a real workload runs on that part, sensors are read 20 times a second, and
the diagram and numbers follow the real readings. Nothing on screen is simulated.

Background and requirements: `ProjectInfo.md`, `SRS_HardwareComputeVisualizer.md`.

## Run it

Windows 10/11, Python 3.11+ (built on 3.13). Runs as a standard user.

```
pip install -r requirements.txt
python main.py
```

Optional extras:
- **PyTorch with CUDA** - needed for the ML test and for the AI benchmark's GPU tasks; makes the
  PCIe test measure the link's full speed (without it, the PCIe test uses OpenGL and moves less).
- **Ollama** running locally with a model that can call tools - needed for the AI benchmark. It uses
  `llama3.2:3b` or `qwen2.5:7b` if installed, otherwise the smallest installed tool-calling model.
- **LibreHardwareMonitor** running as administrator - the only way Windows gives out CPU and
  chipset temperatures (and AMD / Intel GPU temperatures). The app reads what LHM publishes; it
  never needs admin itself. Without LHM these show "unavailable" and there is no CPU thermal stop
  (the app warns before a CPU test). If LHM is started while the app is open, the next CPU test
  finds it. Set it up once with

  ```
  powershell -ExecutionPolicy Bypass -File setup_lhm.ps1
  ```

  It downloads LHM v0.9.6 (hash-checked), starts it hidden in the tray at every logon with admin
  rights (one UAC prompt), and blocks its port 8085 from other machines. The app reads LHM's local
  web server (`http://127.0.0.1:8085/data.json`); older LHM / OpenHardwareMonitor versions are
  read through WMI instead. On the first start LHM may ask to install its PawnIO driver - that is
  what lets it read the CPU. Some boards' sensor chips don't name a chipset sensor; then chipset
  temperature stays "unavailable" rather than guessing which unnamed sensor it is.

## Using it

1. Choose the target machine: **THIS PC** (default) or **REMOTE MACHINE…** (see below).
2. Click a part in the diagram (or search for it in the box under it).
3. Pick a test.
4. Press START. STOP ends it early; otherwise it ends by itself.

| Test | Works on | What really runs |
|---|---|---|
| 2D | CPU, RAM | CPU: Mandelbrot frames drawn on every core. RAM: large memory copies (measures GB/s). |
| 3D | GPU | A ray-marched 3D scene rendered at 1080p with OpenGL (measures frame time). |
| ML | GPU, CPU | Trains a small neural network with PyTorch (measures samples/s and GFLOPS, counted from a real training step; the loss really falls). GPU needs NVIDIA. |
| DISK I/O | DISK | Writes then reads a 1 GB temporary file, bypassing the file cache (measures MB/s). The file deletes itself, even if the app is killed. |
| PCIe | BOARD | Moves 256 MB blocks system RAM -> GPU -> system RAM across the motherboard's PCIe link (measures GB/s). |
| AI BENCHMARK | any | A local AI model is given a task and decides by itself what code to run, inside a sandbox (10 min limit). This PC only. |
| YOUR CODE | any | Your own program: type its command line in PROGRAM (or pick a `.py` / `.exe` with …). Runs until it exits (10 min limit); its output shows in the log. On a remote machine the command runs in your home folder there. |

Tests last 30 seconds. Parts the test doesn't target but that work alongside it (for example
the CPU feeding the GPU, or the board's PCIe link during a 3D test) are shown too.

The clock shows the drawing rate and sensor rate while a test runs (`61 FPS · 20 Hz`).
The CPU box has one small bar per core: a single tall bar among short ones is serial code, all
tall is code that uses every core.

### Where the code wastes the hardware (bottleneck finder)

Every run's summary ends with what `analysis.py` found in the recorded samples, with the share
of the run and the longest stretches:

| Finding | Means |
|---|---|
| One CPU core maxed while the others sat idle | serial code - spread it over the cores |
| GPU waiting on the CPU | the GPU is idle while the CPU prepares work - feed it faster |
| CPU <-> GPU transfers kept the PCIe bus busy | too much copying between RAM and the GPU |
| The disk was busy all the time | waiting on storage |
| RAM almost full | swapping risk |
| Nothing was busy | the code was waiting (sleep, network, locks) |
| GPU fully busy / all CPU cores busy | good use: the hardware itself is the limit |

Run YOUR CODE on two machines (one can be remote), or before and after a change, then COMPARE.

### The parts and where their numbers come from

| Part | Readings | Source on Windows | Source on a remote Linux machine |
|---|---|---|---|
| RAM | in use, GB used | psutil | /proc/meminfo |
| CPU | utilization, each core, frequency | psutil | /proc/stat, cpufreq |
| CPU | power draw | Windows RAPL counter (no admin needed) | RAPL in /sys (usually root-only, so "unavailable") |
| CPU | temperature | LibreHardwareMonitor | hwmon (k10temp / coretemp) |
| GPU | utilization, VRAM, temperature, power | NVML (NVIDIA); Windows GPU counters - the ones Task Manager uses - for AMD / Intel, with temperature and power from LHM | NVML |
| BOARD | PCIe link load, MB/s to and from the GPU, link width | NVML (the NVIDIA GPU's PCIe link) | NVML |
| BOARD | chipset temperature | LibreHardwareMonitor | hwmon (Super I/O chip), if exposed |
| DISK | active time, read / write MB/s | Windows perf counter, psutil | /proc/diskstats |

"PCIe link load" is the busier direction of the GPU's link as a share of what the link can
carry (its top generation x its current width). The source column shows the link, e.g.
`Gen4 x8 of x16` = the card sits in a x16 slot but runs at x8.

### Remote machine (SSH)

REMOTE MACHINE… asks for a host, user, port and SSH key file. The machine must run Linux with
Python 3.8+ and accept SSH key login (for example a rented cloud GPU machine); an NVIDIA driver
is needed for GPU and board readings.

- Uses Windows' own OpenSSH client (`ssh.exe`) with key login only: no password is ever asked
  for or stored. `remote_target.json` remembers the host, user, port and key file *path*.
- On first contact the host's key is remembered in `~/.ssh/known_hosts`; if it changes later,
  the connection is refused.
- The app copies a small agent (`remote_agent.py`, `workloads.py`, `nvgpu.py`, `pynvml.py`) to a
  new temp folder there and starts it. Nothing is installed. The status line reads
  `Connected: HOST`, and the diagram shows that machine's hardware.
- The tests run on the remote machine (3D and PCIe need `moderngl` there, ML needs PyTorch;
  missing ones fail with a message saying so). The AI benchmark stays on this PC.
- If the app stops talking to it for 10 seconds (network down, app closed), the agent stops the
  test and exits by itself. On exit it deletes its folder. THIS PC switches back and removes it.
- The app never creates, starts, stops or pays for cloud machines.

### Which lines of your code take the time (VS Code extension)

`profiler.py` runs a Python file and notes, every 5 ms, which line *of your code* is running
(time inside numpy, PyTorch or the standard library counts for your line that called it), along
with what the hardware was doing. The **HCV Hot Spots** extension for VS Code turns that into
coloured boxes around the slowest blocks of code - #1 red, #2 orange, #3 yellow - each labelled
like

    ◀ #1 · 75% of the run time · 4.9 s · 1 line · 1 of 20 cores busy - single-threaded

The boxes fill in live while the program runs and stay until the next run. Optionally, blue dashed
boxes mark the lines whose objects held the most memory. Install it with

    powershell -ExecutionPolicy Bypass -File vscode-extension/install.ps1

then reload VS Code, open a `.py` file and press the flame button in the editor title (or
right-click > HCV: ...). "Profile in the Hardware Visualizer" runs it as YOUR CODE in this app
(AI CONTROL on), so you watch the hardware while the boxes fill in. Try it on
`examples/hotspots.py`. Details: `vscode-extension/README.md`. Without VS Code:
`python profiler.py your_script.py` writes `.hcv/profile.json` next to the script.

### AI control: let an AI drive the app while you watch

Press **AI CONTROL** (top right) or start with `python main.py --ai-control`. Another program -
for example Claude - can then pick parts and tests, press START / STOP, wait for results, read
summaries and open COMPARE. You see everything it does: a purple banner says what it is doing,
the button it presses flashes, every action is logged in purple, and each action waits a moment
before happening so you can follow it. Press AI CONTROL again to take control back.

- **Claude Code:** the project's `.mcp.json` registers `mcp_server.py`. Open `claude` in this folder,
  approve the `hardware-visualizer` server once, then ask e.g. *"run the 3D test on the GPU and tell
  me the frame time"* or *"run my script train.py and tell me where it wastes the hardware"*.
- **Other MCP clients** (Claude Desktop...): add a server with command `python` and argument
  `<this folder>\mcp_server.py`.
- **Anything else:** plain HTTP on `127.0.0.1:8770` (endpoints listed at the top of `control.py`),
  with the header `X-HCV-Token: <contents of control_token.txt>`.

Safety: the API listens on this PC only and refuses requests without the token, which is new each
time control is switched on, so a web page can't drive the app. While an AI is in control, pop-up
questions don't block the window: they go to the log, and a "continue?" question (such as "no CPU
temperature, so no thermal stop") is answered only if the AI explicitly confirms.

### Recorded runs: compare and replay

Every finished run is saved as one file in `runs/`, with the machine it ran on and what the
bottleneck finder said.

- **Previous run** - the summary of a run shows the previous run of the same test (or the same
  program) on the same part of the same machine next to it, with the change in percent.
- **COMPARE** - any two runs side by side: details, every average with the change from A to B
  (better / worse for the test's own results), one metric over time for both runs, and each run's
  bottlenecks. It opens on the newest run and the one before it of the same test or program.
- **REPLAY** - pick a run to watch it again. The clock and status line turn amber and say it is a
  recording; a replay never passes as live data.

## Files

| File | What it is |
|---|---|
| `main.py` | The window: target machine, diagram, controls, live numbers, summary, recording and replay. |
| `telemetry.py` | Finds this PC's hardware and reads its sensors (read-only). |
| `nvgpu.py` | NVIDIA GPU and PCIe link readings (NVML); used on this PC and by the remote agent. |
| `workloads.py` | The tests. Each runs in its own process (Windows or Linux). |
| `remote.py` | SSH connection to a remote machine: copies and drives the agent. |
| `control.py` | AI CONTROL: the local, token-protected API another program uses to drive the window. |
| `mcp_server.py` | MCP server that gives AI assistants the app's controls as tools (registered in `.mcp.json`). |
| `profiler.py` | Line profiler for your own Python code; feeds the VS Code extension. |
| `vscode-extension/` | HCV Hot Spots: boxes the slowest code in VS Code, live. |
| `analysis.py` | Bottleneck finder: reads a run's samples and says where the hardware sat unused. |
| `remote_agent.py` | Runs on the remote machine: reads its sensors, runs the tests, stops itself if contact is lost. |
| `agent.py` | The AI benchmark: talks to the local Ollama model and runs its tool calls. |
| `sandbox_run.py` | Runs code the AI wrote with no network, no programs, files only in its folder. |
| `gen_diagram.py` | Regenerates `CLASS_DIAGRAM.md` from the code. |
| `PROMPTS.md` | Prompts used to build the app and the ones the AI benchmark uses. |

## Limits

- CPU and chipset temperatures on Windows exist only through LibreHardwareMonitor (see above).
  The ACPI "thermal zone" Windows offers without admin is a fixed placeholder on most desktops, so
  it is not used.
- Board readings need an NVIDIA GPU: NVML is the only standard-user source of PCIe traffic.
- On a PC with several GPUs, the 3D test renders on the GPU Windows picks for OpenGL; the ML and
  PCIe tests use the chosen NVIDIA GPU.
- Windows' per-core counters are coarse at 20 Hz and Windows moves busy threads between cores,
  so the finder judges one-second averages, not single samples.
- While the app is open it asks Windows for a 1 ms timer (as games do) so it can draw at 60 FPS;
  this is undone when it closes.
- The remote side is Linux only. The SRS names Paramiko for SSH; the app uses the OpenSSH client
  that ships with Windows instead, so nothing extra is installed.
