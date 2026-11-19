# Hardware Compute Visualizer

## Why We Picked This
GPUs, CPUs, and the rest of a PC run huge amounts of work every second, but that work
is invisible. Utilization percentages don't show what's actually happening inside the
machine. We want to build something that makes a PC's hardware visible and watchable
while it's under load  a project that combines systems programming, graphics, and
benchmarking, which is why it interests both of us.

## What We Want To Build
A desktop app with a visual diagram of the whole PC  CPU, GPU, motherboard, RAM, and
storage  as selectable parts. Pick a part, run a benchmark test against it, and watch
that part responds in real time, driven by actual hardware telemetry.

## How It Works
1. Pick a component CPU, GPU, RAM, motherboardchipset, or storage.
2. Pick a test to run against it a 2D workload, a 3D workload, or an AIML workload.
3. The test runs for real on that hardware.
4. The diagram animates that part (and any parts it talks to e.g. GPU pulling from
   RAM)  brightnessmotion driven by real utilization, not scripted.
5. Live stats show alongside FLOPS, memory bandwidth, temperature, power draw, frame
   time (whatever's relevant to the test).

AI benchmark mode instead of a canned test, let an AI agent try to complete a
task using the hardware (e.g. train a tiny model, render a scene) and watch how each
part behaves under an unscripted, real workload instead of a synthetic benchmark.

## What We're Committing To Build (MVP)
- Visual diagram of at least CPU + GPU + RAM as selectable parts
- Real telemetry polling for those 3 parts (10Hz+, under 200ms lag)
- 2 benchmark tests one 2D workload, one 3D workload, each a real workload (not
  simulated)
- Diagram animates the active part(s) at 60 FPS based on real utilization
- Basic stat readout per test utilization, memory bandwidth, temperature
- AI benchmark mode AI agent runs an open-ended task, we watch hardware behavior

## Stretch Goals (if time allows)
- Add motherboardchipset + storage as visualized parts
- AIML workload as a third test type
- Session recordreplay of a benchmark run
- Compare two runs (or two machines) side by side

## Ultimate Goal (beyond stretch)
Run the same piece of software across different hardware (different CPUs, GPUs,
machines) and use the visualizer to see how well the code actually uses each one.
Where one machine's GPU sits idle while the CPU maxes out, where a routine is
serial when the hardware could run it in parallel, where code becomes the
bottleneck instead of the hardware  surface those spots directly, instead of just
showing a utilization percentage. The end goal is finding where code should be
changed to make better use of whatever hardware it's running on, backed by real
telemetry from real runs, not static analysis alone.

## Constraints
- Telemetry is read-only never touch driver settings, clocks, or power states
- Runs fully offline, no network dependency for the core benchmarkvisual loop

## Done Looks Like
Pick a part, pick a test, watch a real-time, truthful picture of that part of your PC
actually doing the work.

