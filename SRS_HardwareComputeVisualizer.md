# Software Requirements Specification
# for
# Hardware Compute Visualizer

Date: September 24, 2026

Prepared by:
Andre Mercado
Zachary Taylor

CS 4398 · Group 19

---

## Table of Contents

**I. Introduction**
1. Purpose
2. Scope
3. Intended Audience
4. Definitions, Acronyms, Abbreviations
5. References
6. Document Conventions

**II. Overall Description**
1. Product Perspective
2. Product Functions
3. User Characteristics
4. Operating Environment
5. Design and Implementation Constraints
6. Assumptions and Dependencies

**III. Functional Requirements**
1. Summary
2. System Architecture
3. Benchmark Sequence
4. Workload / Component Matrix
5. Component Selection
   - UC-1: Detect Hardware at Startup
   - UC-2: Connect to a Remote Machine
   - UC-3: Select a Component
   - UC-4: Deselect / Reselect a Component
6. Benchmark Execution
   - UC-5: Run a 2D Workload Benchmark
   - UC-6: Run a 3D Workload Benchmark
   - UC-7: Run an AI Benchmark Mode Task
   - UC-8: Stop a Running Test
   - UC-9: Test Completes
7. Telemetry and Visualization
   - UC-10: Live Diagram Animation from Telemetry
   - UC-11: Live Stat Readout
8. Error Handling and Safety
   - UC-12: Telemetry Polling Failure
   - UC-13: Benchmark Fails to Start
   - UC-14: Thermal Safety Abort
9. External Interface Requirements
10. Interface Wireframe

**IV. Non-Functional Requirements**
1. Performance
2. Reliability
3. Robustness
4. Maintainability
5. Security
6. Safety
7. Usability

**V. Requirements Traceability**

**VI. Appendix — Roadmap**
1. MVP Scope
2. Stretch Goals
3. Ultimate Goal (Beyond Stretch)

---

## I. Introduction

### 1. Purpose
This document specifies the requirements for the Hardware Compute Visualizer, a desktop application that gives the user a live, visual, truthful picture of what their PC hardware (CPU, GPU, and RAM, and later motherboard/chipset and storage) is doing while it executes real workloads. It is intended for both the users and the developers of the system.

### 2. Scope
The system shall:

- Display a visual diagram of the PC's hardware components as selectable parts.
- Allow the user to pick a target machine — the local PC, or a cloud machine the user rents, reached over SSH — then a component and a workload (2D or 3D) to run against it.
- Execute the selected workload for real on the selected hardware — never a simulated or scripted result.
- Poll live hardware telemetry and animate the diagram in real time based on that telemetry.
- Display live stats for the running workload: utilization, memory bandwidth, and temperature at minimum; frame time for 3D workloads; power draw and FLOPS where the hardware and workload make them available.
- Provide an "AI Benchmark Mode" in which a locally run AI agent performs an open-ended task and the user observes real, unscripted hardware behavior.

Out of scope: driver or firmware modification, overclocking, changing power states, clock speeds, or fan curves, any network dependency when testing the local PC, and renting, starting, stopping, or paying for cloud machines (the user manages these through their provider). A fixed AI/ML workload as a third test type is a stretch goal (see VI.2), not part of the MVP.

### 3. Intended Audience
This document is intended for the developers building the application and for anyone evaluating what the finished product does. Developers should read Parts III and IV closely; evaluators can rely on Part II for a summary of what the app does.

### 4. Definitions, Acronyms, Abbreviations

| Term | Definition |
|---|---|
| Component / Part | A piece of PC hardware represented in the diagram: CPU, GPU, RAM (MVP), and later motherboard/chipset and storage. |
| System RAM | Main memory installed on the motherboard, shared by the CPU and (for transfers) the GPU. Shown as the "RAM" part. |
| VRAM | Dedicated memory on the graphics card. Reported as part of the GPU, not as the RAM part. |
| Telemetry | Real-time hardware metrics read from the system: utilization, temperature, power draw, clock speed, memory usage, etc. |
| Workload | A benchmark test executed against a selected component. MVP workloads are a 2D workload and a 3D workload. |
| AI Benchmark Mode | A mode where a locally run AI agent attempts an open-ended task on the hardware instead of a fixed benchmark. |
| AI Agent | The program that drives AI Benchmark Mode: a locally hosted language model that plans and executes a task using a restricted set of tools. |
| FLOPS | Floating Point Operations Per Second. Calculated by the workload itself (operations performed ÷ elapsed time); it is not read from a sensor. |
| Frame Time | The time taken to render a single frame, measured by the 3D workload's own render loop. |
| Memory Bandwidth | Bytes moved to/from memory per second. Measured by the workload (bytes transferred ÷ elapsed time) when OS/hardware counters are unavailable. |
| Latency (telemetry) | Time from a hardware sample being taken to that sample being reflected on screen. |
| MVP | Minimum Viable Product — the committed feature set for the first working version. |
| Polling Rate | The frequency at which telemetry is read from hardware, measured in Hz. |
| NVML | NVIDIA Management Library, the vendor interface used to read NVIDIA GPU telemetry. |
| Target Machine | The computer whose hardware is being tested and shown: the local PC (default) or a remote cloud machine. |
| Remote Target | A cloud machine the user rents from a provider (e.g., RunPod, AWS EC2, Lambda Labs) and connects to over SSH. |
| Remote Agent | A small script the app copies to a remote target. It runs the workloads and reads telemetry there, and streams results back over SSH. |
| SSH | Secure Shell, the encrypted protocol used to log in to and communicate with a remote target. |

### 5. References
- IEEE Std 830-1998, *IEEE Recommended Practice for Software Requirements Specifications* (outline followed by this document).
- ISO/IEC/IEEE 29148:2018, *Systems and software engineering — Life cycle processes — Requirements engineering* (successor to IEEE 830).

### 6. Document Conventions
This document follows the outline format of IEEE Std 830. Functional requirements are written as use cases with a name, priority, trigger, precondition, basic path, and requirement. Each requirement carries an ID (FR-n for functional, NFR-n for non-functional) so it can be traced to a verification method in Part V. "Shall" marks a mandatory requirement; "should" marks a recommendation.

---

## II. Overall Description

### 1. Product Perspective
The Hardware Compute Visualizer is a standalone desktop application (built in Python with Tkinter) that combines a visual hardware diagram, a real-time telemetry pipeline, and a benchmark runner. A normal system monitor only reports a changing number; this application ties that number to a visible part of the PC shown doing the work. Tests can run on the local PC or on a cloud machine the user rents, so the app can also show hardware the user does not own.

### 2. Product Functions
- Connect to a target machine — the local PC by default, or a rented cloud machine over SSH — and detect its CPU, GPU, and RAM.
- Render a diagram of the PC with CPU, GPU, and RAM (MVP) as selectable, distinctly colored parts.
- Let the user select a component by clicking it in the diagram or through its search box.
- Let the user pick a 2D or 3D workload, or AI Benchmark Mode, for the selected component.
- Run that workload for real on the actual hardware, and let the user stop it at any time.
- Poll telemetry for the active component(s), including components they depend on (e.g., RAM feeding the GPU), at 10 Hz or greater with under 200 ms latency.
- Animate the diagram (brightness/motion) at 60 FPS, driven by that live telemetry.
- Display a live stat readout appropriate to the running test and a summary when the test ends.
- Abort any test automatically if a component exceeds a safe temperature.

### 3. User Characteristics
The intended user is a PC enthusiast, hobbyist builder, or student who wants to see what their hardware is doing under load. Basic familiarity with PC components and desktop applications is expected; systems-programming or benchmarking expertise is not required.

### 4. Operating Environment
- **OS:** Windows 10 or 11, 64-bit.
- **Runtime:** Python 3.11 or later.
- **Hardware:** the CPU, GPU, and RAM installed in the machine running the app. Minimum 4 CPU cores and 8 GB of RAM; a GPU supporting OpenGL 3.3 or later for the 3D workload.
- **GPU vendors:** NVIDIA GPUs are fully supported in the MVP (via NVML). AMD and Intel GPUs are best-effort; any metric that cannot be read shall show "unavailable" (UC-12).
- **Privileges:** runs as a standard user. Some sensors (notably CPU temperature and power) are only exposed on Windows through a helper that requires administrator rights; without it those metrics show "unavailable".
- **Remote target (optional):** Linux cloud machine (Ubuntu 22.04+) with an NVIDIA GPU, Python 3.11+, and SSH key access, from any provider (e.g., RunPod, AWS EC2, Lambda Labs).
- **Network:** None when testing the local PC (including AI Benchmark Mode); a stable internet connection when testing a remote target.

### 5. Design and Implementation Constraints
- Telemetry access is strictly read-only. The application shall never modify driver settings, clock speeds, fan curves, or power states.
- When testing the local PC, the core benchmark/visual loop, including AI Benchmark Mode, shall function with no network connection. Remote mode needs a network connection only between the app and the cloud machine.
- Diagram animation shall sustain 60 FPS during an active test.
- Telemetry for MVP components shall be polled at 10 Hz or greater with under 200 ms latency on the local PC, and under 500 ms on a cloud machine to allow for network delay.
- Telemetry polling and workloads shall run off the UI thread so they cannot stall rendering.
- Tkinter's Canvas is not designed for 60 FPS animation. If profiling shows it cannot hold 60 FPS, the diagram renderer may move to a faster toolkit (e.g., pygame or PySide6) without changing these requirements.

### 6. Assumptions and Dependencies
- The OS and vendor libraries expose utilization and memory counters for the installed hardware without kernel drivers.
- NVIDIA telemetry depends on an installed NVIDIA driver that ships NVML.
- AI Benchmark Mode depends on Ollama with the Llama 3.2 3B model installed locally. The model is downloaded once during setup; no network connection is needed after that.
- Cloud machines are assumed to run Linux (Ubuntu 22.04 or later) with Python 3.11+ and the NVIDIA driver installed, and to allow SSH key login. Because they are virtual machines, CPU temperature and power are usually hidden and will show "unavailable". The user creates, starts, stops, and pays for the cloud machine through their provider (e.g., RunPod, AWS EC2, Lambda Labs).
- The 3D workload depends on a working OpenGL 3.3 driver; on a headless cloud machine it renders off-screen through EGL.
- The user does not run other heavy workloads during a test; the app reports what the hardware is doing, which includes other programs' load.

---

## III. Functional Requirements

### 1. Summary
The user picks a hardware part, picks a workload, and watches a real-time, truthful visualization of that hardware doing the work. Two actors drive the app: the **User** (selecting parts, starting and stopping tests) and the **AI Agent** (acting inside AI Benchmark Mode once the user starts it).

Tools used:
- Python as the primary implementation language.
- Tkinter for the user interface.
- Paramiko (SSH) for connecting to rented cloud machines.
- `psutil` for CPU and RAM utilization and memory usage.
- `pynvml` (NVML) for NVIDIA GPU utilization, VRAM, temperature, and power.
- LibreHardwareMonitor (optional, requires administrator) for CPU temperature and power.
- Real, runnable 2D/3D workloads written for this project, which also report their own frame time, FLOPS, and bytes moved.
- Ollama running Llama 3.2 3B (4-bit quantized, about 2 GB) as the AI agent's local model.

### 2. System Architecture
When the user starts a test, the UI layer sends start(test, part) to the **Benchmark Runner** and start(parts) to the **Telemetry Poller**, telling it which component(s) to watch. The runner executes the workload on the real hardware. On a cloud machine, the runner and poller run inside a small remote agent on that machine and send results back over SSH. The poller reads sensors at 10 Hz or greater (read-only) and passes cleaned values to the **Diagram Renderer / Stat Readout**, which draws at 60 FPS. When the test stops or completes, the runner reports completion and the poller stops.

### 3. Benchmark Sequence
1. User selects a part, picks a test, and presses Start.
2. UI calls start(test, part) on the Runner and start(parts) on the Poller.
3. Runner launches the real workload on the hardware.
4. Loop, every ≤100 ms: Poller reads sensors → cleans raw readings → sends values to UI → screen updates.
5. On Stop (UC-8), completion (UC-9), or thermal abort (UC-14): Runner ends the workload and reports done → UI tells the Poller to stop → UI shows the result summary.

### 4. Workload / Component Matrix
Each workload is offered only for the components it meaningfully exercises. Components marked "observed" animate because the workload depends on them but are not the target.

| Workload | CPU | GPU | RAM |
|---|---|---|---|
| 2D workload | Target (CPU-rendered 2D drawing / image processing) | Observed | Observed |
| 3D workload | Observed (draw-call submission) | Target | Observed (texture/buffer uploads) |
| Memory workload (part of 2D test when RAM is selected) | Observed | — | Target (large buffer copy, measures bandwidth) |
| AI Benchmark Mode | Any the agent uses | Any the agent uses | Any the agent uses |

### 5. Component Selection

#### UC-1: Detect Hardware at Startup
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | Application launches, or the user changes the target machine (UC-2) |
| Precondition | None |
| Basic Path | 1. The application queries the target machine's OS and vendor libraries for its CPU, GPU(s), and RAM — directly on the local PC, or through the remote agent on a cloud machine.<br>2. Detected device names populate each part's search options.<br>3. The diagram renders each detected part as selectable.<br>4. Any part that cannot be detected is shown greyed out with "not detected". |
| Alternate Path | a. No GPU is detected: the GPU part is greyed out as "not detected" and the 3D workload is disabled.<br>b. NVML is missing: the GPU name is taken from the operating system and its GPU metrics show "unavailable". |
| Postcondition | Every detected part is shown as selectable and its search box lists the real device name. |
| Requirement | **FR-1:** The application shall list only hardware actually detected on the machine. If more than one GPU is present, each shall be selectable separately. |

#### UC-2: Connect to a Remote Machine
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | User chooses "Remote machine" as the target and enters the host address, username, and SSH key file |
| Precondition | The user has rented and started a cloud machine that accepts SSH key login |
| Basic Path | 1. User selects "Remote machine" and enters the host, username, and key file.<br>2. The application connects over SSH using the key.<br>3. The application checks that Python and the NVIDIA driver are present, copies the remote agent into a working folder, and starts it.<br>4. The remote agent reports the machine's hardware (UC-1).<br>5. The diagram shows the remote hardware, and the status line reads "Connected: HOST". |
| Alternate Path | a. Login fails: an error explains why, and the target stays on the local PC.<br>b. Python or the NVIDIA driver is missing: an error lists what is missing, and the target stays on the local PC.<br>c. The connection drops during a test: all remote metrics show "unavailable" (UC-12), and the remote agent stops the workload on its own after 10 seconds without contact.<br>d. The user switches back to the local PC: any remote test is stopped (UC-8), and the remote agent is stopped and removed. |
| Postcondition | The diagram shows the remote machine's hardware, and tests run on that machine. |
| Requirement | **FR-20:** Remote connections shall use SSH with key authentication only; the application shall never store passwords or cloud-account credentials. **FR-21:** The application shall never create, start, stop, or resize cloud machines; the user manages them, and their cost, through the provider. **FR-22:** The remote agent shall stop any running workload if it loses contact with the application for 10 seconds. **FR-23:** The remote agent shall be read-only toward hardware settings, exactly as on the local PC, and shall be removed from the machine when the user disconnects. |

#### UC-3: Select a Component
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | User clicks a part in the diagram, or types into a part's search box |
| Precondition | UC-1 has completed and at least one part is selectable |
| Basic Path | 1. User clicks a part in the diagram, **or** types into / clicks that part's search box and clicks a matching result.<br>2. The diagram highlights the selected part and the status text updates.<br>3. Any previously selected part is cleared. |
| Alternate Path | a. The search text matches nothing: the list shows "No matches" and the current selection is unchanged. |
| Postcondition | Exactly one part is selected, highlighted, and named in the status line. |
| Requirement | **FR-2:** Only one component may be selected at a time in the MVP. **FR-3:** The highlight shall update within one UI frame (≤17 ms) of the click. |

#### UC-4: Deselect / Reselect a Component
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | User selects a different component while one is already selected |
| Precondition | A component is currently selected |
| Basic Path | 1. User selects a new component.<br>2. If a test is running, it is stopped (UC-8).<br>3. The previous component's highlight and search box are cleared.<br>4. The new component is highlighted. |
| Alternate Path | a. The user reselects the part that is already selected: nothing changes and any running test continues. |
| Postcondition | Only the new part is selected and no test is running. |
| Requirement | **FR-4:** Any running test shall be stopped before the selection changes, so no test is left running against an unselected part. |

### 6. Benchmark Execution

#### UC-5: Run a 2D Workload Benchmark
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | User selects the 2D workload and presses Start |
| Precondition | A component is selected and the 2D workload is valid for it (III.4) |
| Basic Path | 1. User selects the 2D workload.<br>2. User presses Start.<br>3. The application launches the real 2D workload on that hardware.<br>4. Telemetry polling starts for the target and observed components.<br>5. The diagram animates and the readout updates in real time. |
| Alternate Path | a. The workload fails to launch: continue at UC-13.<br>b. A telemetry read fails during the test: continue at UC-12.<br>c. The user presses Stop: continue at UC-8. |
| Postcondition | The 2D workload is running and live telemetry is shown for the target and observed parts. |
| Requirement | **FR-5:** The workload shall actually execute on the hardware; no simulated or pre-recorded data shall be shown. |

#### UC-6: Run a 3D Workload Benchmark
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | User selects the 3D workload and presses Start |
| Precondition | The GPU is selected and supports OpenGL 3.3 or later |
| Basic Path | 1. User selects the 3D workload.<br>2. User presses Start.<br>3. The application launches a real 3D rendering workload.<br>4. Telemetry polling starts for the GPU, plus CPU and RAM as observed parts.<br>5. The GPU (and observed parts) animate; the readout shows frame time, utilization, temperature, and power draw. |
| Alternate Path | a. The GPU does not support OpenGL 3.3: continue at UC-13.<br>b. A component reaches its thermal limit: continue at UC-14. |
| Postcondition | The 3D workload is running and frame time is shown in the readout. |
| Requirement | **FR-6:** Frame time shall be shown in the readout for this test, measured by the workload's render loop. |

#### UC-7: Run an AI Benchmark Mode Task
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | User selects AI Benchmark Mode and presses Start |
| Precondition | A component is selected, and Ollama is running with the Llama 3.2 3B model installed |
| Basic Path | 1. User selects AI Benchmark Mode and picks a task goal (e.g., "train a tiny image classifier", "render a scene").<br>2. User presses Start.<br>3. The application launches the local AI agent inside a sandbox.<br>4. The agent plans and carries out the task using only its allowed tools.<br>5. Telemetry polling starts for every component the agent's work touches.<br>6. The diagram and readout show real, unscripted hardware behavior until the task ends, times out, or the user stops it. |
| Alternate Path | a. Ollama is not running or the model is not installed: an error explains how to start Ollama or install the model, and the app returns to the selection state (UC-13).<br>b. The task reaches its time limit: the agent is stopped (UC-8) and the summary is marked "time limit reached".<br>c. The agent attempts a blocked action (network access, a file outside its folder): the action is refused and logged, and the task continues. |
| Postcondition | The agent is running inside its sandbox and live telemetry is shown for every part it uses. |
| Requirement | **FR-7:** The agent's actions shall not be predetermined; hardware behavior is observed, not scripted. **FR-8:** The agent shall run entirely on the local machine: its model is served by Ollama on localhost, and no request leaves the machine. **FR-9:** Code the agent runs shall be sandboxed: no administrator rights, no network, file access limited to a dedicated working folder, only approved tools (e.g., run Python in that folder), and a time limit (default 10 minutes). |

#### UC-8: Stop a Running Test
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | User presses Stop, selects another component, or closes the app |
| Precondition | A test or AI task is running |
| Basic Path | 1. The application signals the runner to end the workload.<br>2. The workload process ends.<br>3. Telemetry polling stops and the diagram returns to idle.<br>4. A partial summary is shown, marked "stopped by user". |
| Alternate Path | a. The workload does not release the hardware within 2 seconds: its process is force-terminated and the summary notes "force-stopped".<br>b. The user closes the app: the test is stopped without a summary and the app exits. |
| Postcondition | No workload process is running, the diagram is idle, and a partial summary is shown. |
| Requirement | **FR-10:** A stopped workload shall release the CPU/GPU within 2 seconds; if it does not, its process shall be force-terminated. |

#### UC-9: Test Completes
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | The workload or AI task finishes on its own |
| Precondition | A test or AI task was running |
| Basic Path | 1. The runner reports completion.<br>2. Telemetry polling stops.<br>3. The diagram returns to idle.<br>4. A summary shows duration and the minimum, average, and peak of each metric recorded. |
| Alternate Path | a. A metric had no valid samples during the run (e.g., it was unavailable the whole time): the summary shows "no data" for that metric. |
| Postcondition | The diagram is idle and the summary of the completed run is shown. |
| Requirement | **FR-11:** The summary shall be computed only from samples actually recorded during the run. |

### 7. Telemetry and Visualization

#### UC-10: Live Diagram Animation from Telemetry
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | A test or AI task is running |
| Precondition | Telemetry polling is active for the component(s) involved |
| Basic Path | 1. Telemetry is polled at 10 Hz or greater.<br>2. The diagram updates brightness/motion for the target part based on real utilization.<br>3. Observed parts (e.g., RAM feeding the GPU) animate too. |
| Alternate Path | a. Telemetry for one part fails: that part follows UC-12 while the others keep animating.<br>b. Rendering falls behind: the renderer skips frames rather than delaying telemetry, so latency stays under 200 ms. |
| Postcondition | The diagram reflects the latest telemetry sample within 200 ms. |
| Requirement | **FR-12:** Animation shall run at 60 FPS. **FR-13:** Latency from a hardware sample to its visual reflection shall be under 200 ms. **FR-14:** Between samples, the animation may interpolate toward the latest sample but shall never display a value that was not bounded by real samples. |

#### UC-11: Live Stat Readout
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | A test or AI task is running |
| Precondition | Telemetry polling is active |
| Basic Path | 1. The application reads the current telemetry values.<br>2. The readout shows utilization, memory bandwidth, and temperature at minimum, plus frame time (3D), and power draw / FLOPS when available.<br>3. Values refresh on every poll for the duration of the test. |
| Alternate Path | a. A metric is not supported on this hardware or needs administrator rights: it shows "unavailable" with the reason. |
| Postcondition | The readout shows the latest sample for each available metric. |
| Requirement | **FR-15:** Numbers in the readout shall be the latest raw sample — not cached, averaged, or smoothed. (Smoothing is allowed only in the animation, per FR-14.) **FR-16:** Every metric shall show its unit and its source (sensor or workload-measured). |

### 8. Error Handling and Safety

#### UC-12: Telemetry Polling Failure
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | A telemetry source becomes unreadable (sensor failure, permissions issue, unsupported hardware) |
| Precondition | A test is running or about to run |
| Basic Path | 1. The application tries to read telemetry for the active component.<br>2. The read fails or takes longer than 100 ms.<br>3. The application shows a message naming the component and metric that are unavailable.<br>4. That part stops animating; its affected numbers show "unavailable". |
| Alternate Path | a. The sensor recovers: the part resumes animating and the message clears.<br>b. All telemetry for the target part fails: the test is stopped (UC-8), since it can no longer be shown truthfully. |
| Postcondition | The app is still running; failed metrics show "unavailable" and all other parts keep updating. |
| Requirement | **FR-17:** A telemetry failure shall not crash the application or affect components still reading correctly. |

#### UC-13: Benchmark Fails to Start
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | User starts a workload the selected hardware cannot run, or the workload fails to launch |
| Precondition | A component and workload are selected |
| Basic Path | 1. User presses Start.<br>2. The application detects the combination is unsupported (III.4) or the workload fails to launch.<br>3. The application shows an error explaining why and returns to the selection state. |
| Alternate Path | a. The user picks a supported workload and presses Start again: continue at UC-5, UC-6, or UC-7. |
| Postcondition | The app is in the selection state and no workload is running. |
| Requirement | **FR-18:** No partial or fake telemetry or animation shall be shown after a failed start. |

#### UC-14: Thermal Safety Abort
| Field | Description |
|---|---|
| Priority | Essential |
| Trigger | A component's temperature reaches its safety limit during a test |
| Precondition | A test or AI task is running and temperature telemetry is available |
| Basic Path | 1. The poller reads a temperature at or above the limit.<br>2. The application stops the test (UC-8).<br>3. A warning names the component and the temperature reached.<br>4. The summary is marked "aborted — thermal limit". |
| Alternate Path | a. Temperature telemetry is unavailable at start: the user is warned before the test starts and may continue or cancel. |
| Postcondition | The test is stopped and a warning names the component and temperature. |
| Requirement | **FR-19:** Default limits: CPU 95 °C, GPU 90 °C (or the vendor-reported slowdown temperature if lower). The test shall be stopped within 1 second of the limit being read. If temperature is unavailable, the user shall be warned before the test starts. |

### 9. External Interface Requirements
- **User interface:** A single window with the hardware diagram on top, a search box under each part, a workload selector, Start/Stop buttons, a live stat panel, and a status line. Dark theme using the colors in III.10.
- **Hardware interfaces:** Read-only access to CPU, GPU, and RAM counters through the OS and vendor libraries (on a cloud machine, through the remote agent). No writes to any hardware register or setting.
- **Software interfaces:** `psutil` (CPU/RAM), NVML via `pynvml` (NVIDIA GPU), LibreHardwareMonitor (optional, CPU temperature/power), OpenGL 3.3 / EGL (3D workload), Paramiko (SSH to cloud machines), and Ollama's local API on localhost (AI agent).
- **Communication interfaces:** Local mode: no external network connections; the only connection is to the local Ollama service on 127.0.0.1. Remote mode: one SSH connection (TCP port 22, key authentication) to the user's cloud machine, carrying commands and telemetry.

### 10. Interface Wireframe
The picker screen shows a diagram of three clickable parts on top with a search box under each, and a status line naming the selected part. Colors: CPU = `#3b82f6`, GPU = `#22c55e`, RAM = `#eab308`, background = `#0f172a`. The selected part is drawn with a white outline. The workload selector, Start/Stop buttons, and stat panel (III.9) sit below the search boxes.

---

## IV. Non-Functional Requirements

| ID | Quality | Requirement | Verification |
|---|---|---|---|
| NFR-1 | Performance | Telemetry shall be polled at 10 Hz or greater with under 200 ms latency (under 500 ms on a cloud machine). | Log sample and draw timestamps during a 60 s run; check poll interval ≤100 ms and latency <200 ms for 95% of samples. |
| NFR-2 | Performance | The diagram shall render at 60 FPS (≤17 ms per frame) during an active test. | Log frame times during each workload; average ≥58 FPS, no frame >50 ms. |
| NFR-3 | Reliability | Displayed values shall be real and current for the full duration of the test — no stale data older than 200 ms. | Compare readout values to logged raw samples. |
| NFR-4 | Robustness | Telemetry failures and unsupported combinations shall produce a clear error, never a crash or fake data. | Unplug/deny a sensor source, run an unsupported combination; app keeps running. |
| NFR-5 | Maintainability | New components (motherboard/chipset, storage) and workload types (AI/ML) shall be addable by adding a component or workload module, without changing the diagram or selection code. | Code review; add a stub component in a test. |
| NFR-6 | Security | Telemetry access shall be read-only. The app shall never change driver settings, clocks, fan curves, or power states, and shall run as a standard user (administrator only for optional sensors). | Code review; run as standard user. |
| NFR-7 | Security | Code run by the AI agent shall have no network access, no administrator rights, and file access limited to its working folder. | Attempt a network call and an out-of-folder write from the agent; both are blocked. |
| NFR-8 | Safety | Any test shall stop within 1 s of a component reaching its thermal limit. | Lower the limit in a debug setting and confirm abort. |
| NFR-9 | Usability | From launch, a user shall be able to start a test in no more than 3 interactions: select a part, select a workload, press Start. | Walkthrough with a new user. |
| NFR-10 | Security | Remote connections shall use SSH key authentication only, and no passwords or cloud-account credentials shall be stored. | Code review; inspect saved settings files. |
| NFR-11 | Reliability | If the connection to a cloud machine is lost, the remote workload shall stop within 10 seconds so it does not keep running (and billing) unattended. | Disconnect the network mid-test; confirm the remote process ends. |

---

## V. Requirements Traceability

| Requirement | Use Case / Section | Verification |
|---|---|---|
| FR-1 | UC-1 | Run on machines with 1 and 2 GPUs; compare to Device Manager. |
| FR-2, FR-3 | UC-3 | Manual UI test; frame-time log. |
| FR-4 | UC-4 | Switch parts mid-test; confirm the workload process ends. |
| FR-5 | UC-5 | Confirm utilization rises in Task Manager during the test. |
| FR-6 | UC-6 | Readout shows frame time; matches workload log. |
| FR-7, FR-8, FR-9 | UC-7 | Run with network disabled; sandbox escape tests (NFR-7). |
| FR-10 | UC-8 | Press Stop; CPU/GPU utilization drops within 2 s. |
| FR-11 | UC-9 | Summary values match recorded sample log. |
| FR-12, FR-13, FR-14 | UC-10 | NFR-1 and NFR-2 measurements. |
| FR-15, FR-16 | UC-11 | Readout values match raw sample log. |
| FR-17 | UC-12 | Simulate sensor failure; other parts keep updating. |
| FR-18 | UC-13 | Start unsupported combination; no animation shown. |
| FR-19 | UC-14 | NFR-8 test. |
| FR-20 – FR-23 | UC-2 | Connect to a cloud machine with a key; check no credentials are saved; drop the connection mid-test (NFR-11); disconnect and confirm the agent is removed. |

---

## VI. Appendix — Roadmap

### 1. MVP Scope — Committed to Build
- Hardware detection and a diagram of CPU + GPU + RAM as clickable parts
- Real telemetry for those 3 parts (10 Hz+, under 200 ms latency)
- Two real workloads: one 2D, one 3D (never simulated), runnable on the local PC or on a rented cloud machine over SSH
- Diagram animates the active part(s) at 60 FPS from real utilization
- Live readout per test: utilization, memory bandwidth, temperature (plus frame time for 3D)
- Stop button, end-of-test summary, and thermal safety abort
- AI Benchmark Mode: a local, sandboxed AI agent runs an open-ended task while hardware behavior is observed

### 2. Stretch Goals — If Time Allows
- Add motherboard/chipset and storage as visualized parts
- Add a fixed AI/ML workload as a third test type
- Full AMD and Intel GPU telemetry support
- Record and replay a test run
- Compare two runs — or two machines — side by side

### 3. Ultimate Goal — Beyond Stretch
Run the same piece of software across different hardware (different CPUs, GPUs, machines) and use the visualizer to see how well the code actually uses each one. Where one machine's GPU sits idle while the CPU maxes out, where a routine is serial when the hardware could run it in parallel, where the code becomes the bottleneck instead of the hardware — surface those spots directly, instead of just showing a utilization percentage. The goal is finding where code should change to make better use of whatever hardware it's running on, backed by real telemetry from real runs, not static analysis alone.
