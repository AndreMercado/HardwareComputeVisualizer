# System statechart diagrams

Three statecharts describe the behavior of the Hardware Compute Visualizer. Each one is taken
from the code, and every transition names the method or event that causes it. Rendered images
are in `docs/img/` (`statechart_*.png`); the Mermaid source below is what they are drawn from
(`python docs/render_diagrams.py`).

| # | Statechart | Object | Code |
|---|---|---|---|
| 1 | Test session | `main.App` (the controller) | `start_test`, `_tick_running`, `_check_thermal`, `_update_readout`, `stop_test`, `start_replay` |
| 2 | AI Benchmark agent | `agent.main` (its own process) | the step loop in `agent.py` |
| 3 | Workload process: YOUR CODE runner | `workloads.run_usercode` (its own process) | `run_usercode`, `kill` |

---

## 1. Test session (`main.App`)

The app is always in exactly one of **Idle**, **Running a test** or **Replaying**. While a test
runs, every frame (60 per second) runs `_tick_running`, which checks the exit conditions in the
order shown. A test can end in several ways; each gives a result text in the summary and
in the saved run.

```mermaid
stateDiagram-v2
    direction TB
    [*] --> Idle : app starts / detect_hardware()

    state Idle {
        [*] --> Watching
        Watching : idle readings at 4 Hz (_tick_idle)
        Watching --> Watching : click part / pick test [_select, _sync_controls]
    }

    Idle --> Validating : START [start_test]
    state Validating {
        [*] --> CheckRequest
        CheckRequest : part chosen? test fits part? local-only? NVIDIA for board/ML? command typed? Ollama + model?
        CheckRequest --> AskNoSensor : CPU/GPU temperature unreadable
        AskNoSensor : "No CPU temperature - Continue?"
    }
    CheckRequest --> Idle : invalid / _fail() dialog (UC-13)
    AskNoSensor --> Idle : Cancel
    AskNoSensor --> Running : OK
    CheckRequest --> Running : valid / Popen(workload), Poller.start()

    state Running {
        [*] --> WaitingForData
        WaitingForData --> Measuring : first sample
        Measuring --> Measuring : tick [new sample] / record row, update readout, check thermal
    }

    Running --> Stopping : STOP [stop_test("stopped by user")] (UC-8)
    Running --> Stopping : time limit (AI 600 s, YOUR CODE 600 s + 5 s)
    Running --> Stopping : process exited [poll() != None] / classify result (UC-9)
    Running --> Stopping : temp >= limit / "aborted" (UC-14)
    Running --> Stopping : target metrics lost 1 s (UC-12)

    state Stopping {
        [*] --> KillTree
        KillTree : terminate process tree, wait 2 s
        KillTree --> ForceKill : still alive
        ForceKill : kill(), result += "(force-stopped)" (FR-10)
        KillTree --> Summarize : all ended
        ForceKill --> Summarize
        Summarize : poller.stop(), bottleneck finder, save run to runs/, compare with previous run
    }
    Stopping --> Idle : summary shown
    Stopping --> Idle : "failed to start" (no data ever arrived) / error dialog, no summary (UC-13)

    Idle --> Replaying : REPLAY [file valid] / Replay(samples)
    state Replaying {
        [*] --> Playing
        Playing : recorded sample for the playback time, amber clock "REPLAY (recorded)", no safety checks
    }
    Replaying --> Idle : end of recording / STOP
```

**Result texts** of a finished test (from `_tick_running` and `stop_test`):

| How it ended | Result |
|---|---|
| benchmark ran its full time | `completed` |
| AI agent called `finish` | `completed` |
| AI agent quit without `finish` | `agent stopped without finishing (<reason>)` |
| AI / YOUR CODE reached its limit | `time limit reached` |
| YOUR CODE ended by itself | `program exited with code N` |
| workload reported an error after data | `failed — <error>` |
| no data ever arrived | dialog `<test> failed to start: <error>` (no summary, nothing shown as running) |
| STOP | `stopped by user` (+ ` (force-stopped)` if it had to be killed) |
| temperature limit (CPU 95 °C; GPU the lower of 90 °C and the vendor's slowdown temperature) | `aborted — thermal limit (CPU 96 °C)` + warning dialog |
| target metrics lost ≥ 1 s | `stopped — CPU telemetry lost` |

A REPLAY file that can't be read (no samples, unknown part) is refused with a `_fail()` dialog and the app stays Idle.

---

## 2. AI Benchmark agent (`agent.py`)

The agent runs in its own process. A local Ollama model decides each step; the program only
enforces the limits (`MAX_STEPS = 30`, `MAX_NUDGES = 4`, the run's deadline) and the sandbox.
It prints one JSON event per transition, which `App._drain_events` shows in the log.

```mermaid
stateDiagram-v2
    direction TB
    [*] --> AskModel : goal + system prompt / emit say

    AskModel : chat(model, messages) with the 5 tools
    AskModel --> RunTools : reply has tool calls / nudges = 0
    AskModel --> Nudge : reply has no tool call
    Nudge : nudges += 1, add "call run_python or finish"
    Nudge --> AskModel : nudges <= 4
    Nudge --> Stopped : nudges > 4 / emit stopped

    state RunTools {
        [*] --> Tool
        Tool : emit tool, then run_python (sandbox_run.py) / write_file / read_file / list_files
        Tool --> Blocked : path outside the folder, network, program start
        Blocked : emit blocked, result "BLOCKED: ..."
        Tool --> ToolResult
        Blocked --> ToolResult
        ToolResult : emit result, add it to the conversation
    }
    RunTools --> AskModel : next step [step <= 30]
    RunTools --> Done : tool = finish / emit done(summary)
    RunTools --> Stopped : step 30 used / emit stopped "step limit reached"

    AskModel --> TimedOut : deadline passed / emit timeout
    AskModel --> Failed : Ollama stopped responding / emit error, exit 2

    Done --> [*] : unload model
    Stopped --> [*] : unload model
    TimedOut --> [*] : unload model
    Failed --> [*] : unload model
```

---

## 3. YOUR CODE runner (`workloads.run_usercode`)

```mermaid
stateDiagram-v2
    direction TB
    [*] --> Launch
    Launch : choose folder (HCV_CWD, else the script's folder) and Python (HCV_PYTHON)
    Launch --> Error : Popen fails / emit error "can't start", exit 2
    Launch --> ProgramRunning : Popen OK

    state ProgramRunning {
        [*] --> Heartbeat
        Heartbeat : emit run_s every 0.25 s
        Heartbeat --> Heartbeat : program prints a line / emit out
    }
    ProgramRunning --> Exited : program ends / emit exit "program exited with code N"
    ProgramRunning --> KillTree : run_s >= limit
    KillTree : taskkill /T /F (the program and its child processes)
    KillTree --> StoppedAtLimit : emit exit "stopped at the N s time limit"

    Exited --> [*]
    StoppedAtLimit --> [*]
    Error --> [*]
```

---

## How the statecharts were checked

The transitions above are exercised by the unit tests (`tests/unit/`):

| Statechart | Transitions tested in |
|---|---|
| 1 Test session | `test_controller.py` - `StartValidationTest` (every Validating → Idle refusal, the Cancel on AskNoSensor), `StartLaunchTest` (Validating → Running), `EndOfRunTest` (every Running → Stopping exit and its result text), `SafetyTest` (thermal, telemetry lost, replay exempt), `StopTest` (KillTree / ForceKill / Summarize) |
| 2 AI agent | `test_agent.py` - `AgentLoopTest` (Done, Nudge → Stopped, nudge reset, Blocked, tool error, step limit, time limit) |
| 3 YOUR CODE runner | `test_workloads.py` - `UserCodeTest` (Launch choices, Error, Exited, KillTree → StoppedAtLimit with child processes) |
