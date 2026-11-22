// Builds docs/presentation/HCV_Final_Presentation.pptx from the docs, diagrams and acceptance screenshots.
//   NODE_PATH=<folder with pptxgenjs, sharp, react, react-dom, react-icons> node docs/presentation/build_deck.js
// Diagrams: python docs/render_diagrams.py; screenshots + results: python tests/acceptance/run_acceptance.py
const path = require("path");
const fs = require("fs");
const pptxgen = require("pptxgenjs");
const sharp = require("sharp");
const React = require("react");
const ReactDOMServer = require("react-dom/server");
const fa = require("react-icons/fa");

const ROOT = path.resolve(__dirname, "..", "..");
const IMG = path.join(ROOT, "docs", "img");
const AT = path.join(ROOT, "docs", "acceptance", "img");
const OUT = path.join(__dirname, "HCV_Final_Presentation.pptx");
const APPLY_THEME = process.env.APPLY_THEME;  // the pptx skill's scripts/apply_theme.js
const results = JSON.parse(fs.readFileSync(path.join(ROOT, "tests", "work", "acceptance", "results.json"), "utf8"));
const atResult = (id) => results.find((r) => r.at === id);

const THEME = {
  name: "HCV Neon",
  headFontFace: "Bahnschrift SemiBold",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "070707", lt1: "FFFFFF", dk2: "151515", lt2: "A3A3A3",
    accent1: "44D62C", accent2: "00A8FF", accent3: "FFB400", accent4: "1DE9B6", accent5: "FF4D9D", accent6: "C77DFF",
    hlink: "4FC1FF", folHlink: "C77DFF",
  },
};
const HEX = THEME.colors;
const W = 13.333, H = 7.5, M = 0.6;

async function icon(Comp, hex, px = 256) {
  const svg = ReactDOMServer.renderToStaticMarkup(React.createElement(Comp, { color: "#" + hex, size: px }));
  const buf = await sharp(Buffer.from(svg)).resize(px, px).png().toBuffer();
  return "image/png;base64," + buf.toString("base64");
}

async function fit(file, maxW, maxH) {
  const m = await sharp(file).metadata();
  const s = Math.min(maxW / m.width, maxH / m.height);
  return { w: m.width * s, h: m.height * s };
}

async function main() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_WIDE";
  pres.title = "Hardware Compute Visualizer - final presentation";
  pres.author = "Andre Mercado, Zachary Taylor";
  pres.subject = "CS 4398 Group 19";
  pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
  const C = pres.SchemeColor;

  // ---------------------------------------------------------------- layouts
  const footer = [
    { text: { text: "Hardware Compute Visualizer  ·  CS 4398 Group 19", options: {
      x: M, y: H - 0.42, w: 6, h: 0.3, fontSize: 10, color: C.background2, margin: 0 } } },
  ];
  const slideNum = { x: W - M - 0.6, y: H - 0.42, w: 0.6, h: 0.3, fontSize: 10, color: C.background2, align: "right", margin: 0 };
  pres.defineSlideMaster({
    title: "HCV Title", background: { color: C.text1 },
    objects: [
      { placeholder: { options: { name: "title", type: "title", x: M, y: 1.7, w: 6.4, h: 1.9, fontSize: 44, bold: true, align: "left",
        color: C.background1, valign: "top", margin: 0 }, text: "" } },
      { placeholder: { options: { name: "body", type: "body", x: M, y: 3.75, w: 6.0, h: 1.2, fontSize: 18,
        color: C.background2, valign: "top", margin: 0 }, text: "" } },
    ],
  });
  pres.defineSlideMaster({
    title: "HCV Section", background: { color: C.text1 },
    objects: [
      { placeholder: { options: { name: "title", type: "title", x: M, y: 2.9, w: W - 2 * M, h: 1.0, fontSize: 40, bold: true,
        color: C.background1, margin: 0 }, text: "" } },
      { placeholder: { options: { name: "body", type: "body", x: M, y: 3.95, w: W - 2 * M, h: 0.8, fontSize: 18,
        color: C.background2, margin: 0 }, text: "" } },
      ...footer,
    ],
    slideNumber: slideNum,
  });
  pres.defineSlideMaster({
    title: "HCV Content", background: { color: C.text1 },
    objects: [
      { placeholder: { options: { name: "title", type: "title", x: M, y: 0.35, w: W - 2 * M, h: 0.8, fontSize: 32, bold: true,
        color: C.background1, valign: "middle", margin: 0 }, text: "" } },
      ...footer,
    ],
    slideNumber: slideNum,
  });

  // ---------------------------------------------------------------- helpers
  const PANEL = { color: C.text2 };
  function card(slide, x, y, w, h, line, name) {
    slide.addShape(pres.shapes.SNIP_2_DIAG_RECTANGLE, { x, y, w, h, fill: PANEL, line: { color: line, width: 1.25 },
      rectRadius: 0.12, objectName: name });
  }
  function text(slide, t, o) {
    slide.addText(t, { isTextBox: true, margin: 0, fontSize: 14, color: C.background1, valign: "top", ...o });
  }
  async function picture(slide, file, x, y, maxW, maxH, alt, align = "center") {
    const s = await fit(file, maxW, maxH);
    const px = align === "left" ? x : align === "right" ? x + maxW - s.w : x + (maxW - s.w) / 2;
    slide.addImage({ path: file, x: px, y, w: s.w, h: s.h, altText: alt, objectName: alt.slice(0, 40) });
    return { x: px, y, w: s.w, h: s.h };
  }
  function caption(slide, t, x, y, w) {
    text(slide, t, { x, y, w, h: 0.3, fontSize: 11, color: C.background2, italic: true });
  }
  const content = (sec) => pres.addSlide({ masterName: "HCV Content", sectionTitle: sec });

  const ic = {
    eye: await icon(fa.FaEye, HEX.accent1), chip: await icon(fa.FaMicrochip, HEX.accent2),
    bullseye: await icon(fa.FaBullseye, HEX.accent3), layers: await icon(fa.FaLayerGroup, HEX.accent1),
    plug: await icon(fa.FaPlug, HEX.accent2), table: await icon(fa.FaTable, HEX.accent3),
    puzzle: await icon(fa.FaPuzzlePiece, HEX.accent4), shield: await icon(fa.FaShieldAlt, HEX.accent5),
    life: await icon(fa.FaLifeRing, HEX.accent6), bug: await icon(fa.FaBug, HEX.accent5),
    check: await icon(fa.FaCheckCircle, HEX.accent1), robot: await icon(fa.FaRobot, HEX.accent6),
    file: await icon(fa.FaFileAlt, HEX.accent3), branch: await icon(fa.FaCodeBranch, HEX.accent4),
    vial: await icon(fa.FaVial, HEX.accent2),
  };

  // ================================================================ 1 title
  pres.addSection({ title: "Introduction" });
  let s = pres.addSlide({ masterName: "HCV Title", sectionTitle: "Introduction" });
  s.addText("Hardware Compute Visualizer", { placeholder: "title" });
  s.addText("Watch a PC's hardware do real work, and find where your code wastes it\n\nAndre Mercado  ·  Zachary Taylor\nCS 4398  ·  Group 19",
    { placeholder: "body" });
  await picture(s, path.join(AT, "AT-3b_cpu_2d_running.png"), 7.4, 0.5, 5.35, 6.5, "App running a 2D CPU test", "right");
  s.addNotes("Hardware Compute Visualizer: a desktop app that runs a real workload on one part of the PC and shows, live, what that part is doing, from real sensors. The ultimate goal: run your own program and see where it wastes the hardware.");

  // ================================================================ 2 purpose
  s = content("Introduction");
  s.addText("Utilization percentages hide what the hardware is doing", { placeholder: "title" });
  const rows = [
    [ic.eye, "The problem", "A CPU at 30 % can be one core maxed and the rest idle. A GPU at 10 % can be waiting on the CPU. The usual numbers don't say."],
    [ic.chip, "What HCV does", "Pick a part (RAM, CPU, GPU, board, disk) and a test. A real workload runs, sensors are read 20 times a second, and the diagram follows the real readings at 60 FPS."],
    [ic.bullseye, "Ultimate goal", "Run your own program: HCV says where it wastes the hardware, and the VS Code extension boxes the slow lines."],
  ];
  rows.forEach(([img, head, body], i) => {
    const y = 1.5 + i * 1.75;
    s.addShape(pres.shapes.OVAL, { x: M, y, w: 0.8, h: 0.8, fill: PANEL, line: { color: C.text2 }, objectName: `icon circle ${i}` });
    s.addImage({ data: img, x: M + 0.18, y: y + 0.18, w: 0.44, h: 0.44, altText: head });
    text(s, head, { x: M + 1.1, y: y - 0.02, w: 5.3, h: 0.4, fontSize: 20, bold: true, fontFace: THEME.headFontFace });
    text(s, body, { x: M + 1.1, y: y + 0.42, w: 5.3, h: 1.1, fontSize: 15, color: C.background2 });
  });
  await picture(s, path.join(AT, "AT-8c_your_code_summary.png"), 7.3, 1.35, 5.4, 5.45, "YOUR CODE run summary with bottlenecks", "right");
  s.addNotes("Why: utilization percentages are averages; they hide serial code, a starved GPU, a saturated bus. HCV shows each part, per core, with real readings - never simulated.");

  // ================================================================ 3 requirements
  s = content("Introduction");
  s.addText("Requirements first: SRS with 13 use cases, all built", { placeholder: "title" });
  const stats = [["13", "use cases", "UC-1 … UC-14 with alternate paths (UC-2 withdrawn in v1.1)", C.accent1], ["19", "functional reqs", "FR-1 … FR-19, traced to code and tests", C.accent2],
    ["6 / 6", "MVP items", "diagram, 20 Hz telemetry, 2D + 3D tests, 60 FPS, stats, AI mode", C.accent3],
    ["4 / 4", "stretch goals", "board + disk parts, ML test, record / replay, compare", C.accent4]];
  stats.forEach(([n, l, d, col], i) => {
    const x = M + i * 3.07;
    card(s, x, 1.55, 2.85, 2.55, col, `stat ${l}`);
    text(s, n, { x: x + 0.25, y: 1.75, w: 2.4, h: 1.0, fontSize: 54, bold: true, color: col, fontFace: THEME.headFontFace });
    text(s, l, { x: x + 0.25, y: 2.75, w: 2.4, h: 0.4, fontSize: 18, bold: true });
    text(s, d, { x: x + 0.25, y: 3.18, w: 2.45, h: 0.8, fontSize: 13, color: C.background2 });
  });
  card(s, M, 4.45, W - 2 * M, 2.25, C.accent6, "ultimate goal card");
  s.addImage({ data: ic.bullseye, x: M + 0.35, y: 4.75, w: 0.5, h: 0.5, altText: "goal" });
  text(s, "Beyond stretch - the ultimate goal is built too", { x: M + 1.1, y: 4.72, w: 10, h: 0.45, fontSize: 20, bold: true, fontFace: THEME.headFontFace });
  text(s, [
    { text: "YOUR CODE runs the user's own program and the bottleneck finder names what limited it", options: { bullet: true, breakLine: true } },
    { text: "COMPARE the same program before and after a change, every metric with its change", options: { bullet: true, breakLine: true } },
    { text: "VS Code \"Hot Spots\" extension: the slowest lines boxed, with the hardware state while each ran", options: { bullet: true } },
  ], { x: M + 1.1, y: 5.25, w: 11, h: 1.3, fontSize: 15, color: C.background2, paraSpaceAfter: 4 });
  s.addNotes("Process started with ProjectInfo.md and the SRS: use cases with main and alternate paths, functional requirements, NFRs, and a traceability table. SRS v1.1 withdrew UC-2 (measuring a remote machine): it was built, then removed to keep the project on one machine. Everything else in MVP and stretch is built and tested.");

  // ================================================================ design section
  pres.addSection({ title: "Design" });
  s = pres.addSlide({ masterName: "HCV Section", sectionTitle: "Design" });
  s.addText("Design", { placeholder: "title" });
  s.addText("Architecture, class diagram, statecharts, algorithms", { placeholder: "body" });

  // ================================================================ 4 architecture
  s = content("Design");
  s.addText("Every workload runs in its own process", { placeholder: "title" });
  await picture(s, path.join(IMG, "architecture_processes.png"), M, 1.35, 7.9, 5.4, "Process architecture diagram", "left");
  text(s, [
    { text: "Why separate processes", options: { bold: true, fontSize: 18, breakLine: true } },
    { text: "A workload that hangs or uses every core can't freeze the window", options: { bullet: true, breakLine: true } },
    { text: "STOP always works: the process tree is terminated, then killed after 2 s", options: { bullet: true, breakLine: true } },
    { text: "Same contract everywhere: one JSON object per line", options: { bullet: true, breakLine: true } },
    { text: "Talks to", options: { bold: true, fontSize: 18, breakLine: true } },
    { text: "LibreHardwareMonitor and Ollama on 127.0.0.1", options: { bullet: true, breakLine: true } },
    { text: "VS Code / an AI through a token-protected local API", options: { bullet: true } },
  ], { x: 8.9, y: 1.5, w: 3.85, h: 5.2, fontSize: 14, color: C.background2, paraSpaceAfter: 6 });
  s.addNotes("main.py is the window. Workloads, the AI agent and its sandbox, and the profiler are separate processes talking JSON lines. The poller is a thread at 20 Hz.");

  // ================================================================ 5 class diagram
  s = content("Design");
  s.addText("Class diagram: Model separate from View + Controller", { placeholder: "title" });
  await picture(s, path.join(IMG, "class_diagram_design.png"), M, 1.3, 9.0, 5.5, "UML class diagram, design view", "left");
  const layers = [["Model", "telemetry, workloads, analysis, agent, profiler - no UI imports", C.accent1],
    ["Controller", "App.start_test, stop_test, _tick_running, safety rules; ControlServer API", C.accent2],
    ["View", "App drawing (60 FPS), CompareWindow, SearchCombo, dialogs", C.accent3]];
  layers.forEach(([n, d, col], i) => {
    const y = 1.4 + i * 1.45;
    card(s, 9.85, y, 2.9, 1.25, col, `layer ${n}`);
    text(s, n, { x: 10.05, y: y + 0.12, w: 2.6, h: 0.35, fontSize: 16, bold: true, color: col });
    text(s, d, { x: 10.05, y: y + 0.48, w: 2.6, h: 0.75, fontSize: 11, color: C.background2 });
  });
  text(s, "Interface: TelemetrySource (Poller = live sensors, Replay = a recording). Full diagram: CLASS_DIAGRAM.md, regenerated by a hook after every edit.",
    { x: 9.85, y: 5.85, w: 2.9, h: 1.0, fontSize: 11, color: C.background2, italic: true });
  s.addNotes("Model never imports the UI, so each Model class is unit tested on its own. App mixes View and Controller (Tkinter style) - the honest weak spot; next refactor: move the controller methods into a TestSession class. TelemetrySource lets a replay reuse all the drawing code.");

  // ================================================================ 6 principles
  s = content("Design");
  s.addText("Design principles applied", { placeholder: "title" });
  const pr = [
    [ic.layers, "Separation of concerns", "Model modules never import the UI; workloads are separate programs"],
    [ic.plug, "Program to an interface", "TelemetrySource: live sensors and replays share one drawing and readout path"],
    [ic.table, "Data-driven, open/closed", "Parts, tests, readouts, bottleneck rules are tables: a new part is one entry"],
    [ic.puzzle, "Adapter", "_Dialogs replaces messagebox: under AI control pop-ups go to the log"],
    [ic.shield, "Least privilege", "No admin, read-only sensors, sandboxed AI code, localhost API with a token, no outside network"],
    [ic.life, "Fail safe, truthful", "Missing = \"unavailable\", never 0; stale data expires in 1 s; replays are marked amber"],
  ];
  pr.forEach(([img, head, body], i) => {
    const x = M + (i % 3) * 4.1, y = 1.5 + Math.floor(i / 3) * 2.65;
    card(s, x, y, 3.85, 2.4, C.text2, `principle ${head}`);
    s.addImage({ data: img, x: x + 0.3, y: y + 0.3, w: 0.5, h: 0.5, altText: head });
    text(s, head, { x: x + 0.3, y: y + 0.95, w: 3.3, h: 0.4, fontSize: 18, bold: true });
    text(s, body, { x: x + 0.3, y: y + 1.38, w: 3.3, h: 0.95, fontSize: 13, color: C.background2 });
  });
  s.addNotes("Each principle points at real code: PARTS/TESTS/STATS tables in main.py, analysis.PATTERNS, _Dialogs, sandbox_run.py, control.py token.");

  // ================================================================ 7 statechart test session
  s = content("Design");
  s.addText("Statechart 1: a test session (main.App)", { placeholder: "title" });
  await picture(s, path.join(IMG, "statechart_1_test_session.png"), M, 1.25, 4.3, 5.75, "Statechart of a test session", "left");
  text(s, "Seven ways a test can end", { x: 5.4, y: 1.4, w: 7.3, h: 0.45, fontSize: 20, bold: true, fontFace: THEME.headFontFace });
  s.addTable([
    [{ text: "Event", options: { bold: true, color: C.accent1 } }, { text: "Result in the summary", options: { bold: true, color: C.accent1 } }],
    ["benchmark ran its full time / agent called finish", "completed"],
    ["AI or YOUR CODE reached its limit", "time limit reached"],
    ["YOUR CODE ended by itself", "program exited with code N"],
    ["STOP pressed (killed if it ignores terminate)", "stopped by user (force-stopped)"],
    ["CPU ≥ 95 °C / GPU ≥ min(90, slowdown)", "aborted — thermal limit + warning"],
    ["all target metrics unavailable ≥ 1 s", "stopped — CPU telemetry lost"],
    ["no data ever arrived", "\"failed to start\" pop-up, no summary"],
  ], { x: 5.4, y: 1.95, w: 7.33, colW: [3.9, 3.43], fontSize: 13, color: C.background1, fill: PANEL,
    border: { type: "solid", pt: 0.5, color: "2A2A2A" }, rowH: 0.42, margin: [0.04, 0.1, 0.04, 0.1] });
  text(s, "Every transition is a method: start_test, _tick_running, _check_thermal, _update_readout, stop_test. Each one is exercised by test_controller.py.",
    { x: 5.4, y: 5.55, w: 7.3, h: 0.8, fontSize: 13, color: C.background2, italic: true });
  s.addNotes("States: Idle, Validating (with the 'no CPU temperature - continue?' question), Running (waiting for data, measuring), Stopping (terminate, force kill, summarize), Replaying. Exit checks run every frame in _tick_running.");

  // ================================================================ 8 statecharts 2-4
  s = content("Design");
  s.addText("Statecharts 2 and 3: the AI agent and the YOUR CODE runner", { placeholder: "title" });
  const sc = [["statechart_2_ai_agent.png", "2  AI agent: at most 30 steps, 4 nudges, and the deadline"],
    ["statechart_3_your_code_runner.png", "3  YOUR CODE runner: exits, or is killed as a tree at the limit"]];
  for (let i = 0; i < 2; i++) {
    const x = M + i * 6.15;
    await picture(s, path.join(IMG, sc[i][0]), x, 1.3, 5.9, 5.05, sc[i][1]);
    caption(s, sc[i][1], x, 6.5, 5.9);
  }
  s.addNotes("Agent: finish = done; 5 replies in a row without a tool call = stopped; step 30 or the deadline = stopped / timeout; the model is always unloaded. YOUR CODE: the program exits by itself, or at the time limit taskkill /T ends it and every process it started.");

  // ================================================================ 9 algorithm
  s = content("Design");
  s.addText("Critical algorithm: the bottleneck finder (analysis.py)", { placeholder: "title" });
  const steps = [["1", "GPU used?", "busy ≥ 30 % once, or VRAM grew ≥ 0.1 GB"], ["2", "Smooth", "each sample = average of the last 1 s (sliding window, O(n))"],
    ["3", "Classify", "each moment into patterns by thresholds"], ["4", "Integrate", "seconds per pattern, and its stretches"],
    ["5", "Report", "≥ 10 % of the run, stretches ≥ 1 s, problems first"]];
  steps.forEach(([n, h, d], i) => {
    const x = M + i * 2.46;
    card(s, x, 1.45, 2.3, 1.85, C.accent1, `step ${n}`);
    text(s, n, { x: x + 0.2, y: 1.55, w: 0.6, h: 0.6, fontSize: 30, bold: true, color: C.accent1, fontFace: THEME.headFontFace });
    text(s, h, { x: x + 0.75, y: 1.65, w: 1.45, h: 0.4, fontSize: 17, bold: true });
    text(s, d, { x: x + 0.2, y: 2.2, w: 1.95, h: 1.0, fontSize: 12, color: C.background2 });
  });
  s.addTable([
    [{ text: "Pattern", options: { bold: true, color: C.accent1 } }, { text: "Rule on the 1-s averages", options: { bold: true, color: C.accent1 } }, { text: "Advice", options: { bold: true, color: C.accent1 } }],
    ["serial", "> 2 cores, busiest core ≥ 70 %, CPU ≤ ~1.6-3 cores of work, GPU not maxed", "spread work over the cores"],
    ["gpu_starved", "run uses the GPU, GPU < 40 %, a core or the CPU ≥ 80 %", "feed the GPU faster"],
    ["pcie / disk / ram", "PCIe ≥ 60 % / disk ≥ 90 % / RAM ≥ 92 %", "fewer copies / cache / less memory"],
    ["idle", "busiest of core, GPU, disk < 20 % and CPU < 15 %", "look for waits"],
    ["cpu_bound / gpu_bound", "CPU ≥ 85 % / GPU ≥ 90 %", "good use: the hardware is the limit"],
  ], { x: M, y: 3.6, w: W - 2 * M, colW: [2.3, 6.4, 3.43], fontSize: 13, color: C.background1, fill: PANEL,
    border: { type: "solid", pt: 0.5, color: "2A2A2A" }, rowH: 0.4, margin: [0.04, 0.1, 0.04, 0.1] });
  caption(s, "Written from scratch. 24 unit tests check every threshold boundary. The profiler reuses steps 2-3 per line of code.", M, 6.6, 11);
  s.addNotes("Example: 16 cores, one at 100 %, the CPU at 6 % = one core of work -> serial. The window smooths because the OS moves a busy thread between cores. O(n) for n samples; a 30 s test is 600 samples.");

  // ================================================================ process section
  pres.addSection({ title: "Process and verification" });
  s = pres.addSlide({ masterName: "HCV Section", sectionTitle: "Process and verification" });
  s.addText("Process and verification", { placeholder: "title" });
  s.addText("How the agent was guided, and how the result was checked", { placeholder: "body" });

  // ================================================================ 10 agentic process
  s = content("Process and verification");
  s.addText("A disciplined agentic process with Claude Code", { placeholder: "title" });
  const proc = [["Requirements first", "ProjectInfo.md and the SRS (use cases, FRs, NFRs, traceability) written before code"],
    ["One goal per session", "each session points the agent at one SRS goal or roadmap item"],
    ["Context given", "SRS + source in the repo, screenshots of the running app, test output"],
    ["Self-checking", "the agent runs the app and the tests itself; it can drive the real app over MCP"],
    ["Commit when verified", "result looked at, then committed; bugs get a test first"]];
  proc.forEach(([h, d], i) => {
    const y = 1.45 + i * 1.05;
    s.addShape(pres.shapes.OVAL, { x: M, y, w: 0.7, h: 0.7, fill: PANEL, line: { color: C.accent1, width: 1.25 }, objectName: `process ${i + 1}` });
    text(s, String(i + 1), { x: M, y: y + 0.08, w: 0.7, h: 0.55, fontSize: 22, bold: true, color: C.accent1, align: "center" });
    text(s, h, { x: M + 0.95, y: y + 0.02, w: 5.6, h: 0.36, fontSize: 17, bold: true });
    text(s, d, { x: M + 0.95, y: y + 0.38, w: 5.6, h: 0.6, fontSize: 13, color: C.background2 });
  });
  card(s, 7.4, 1.45, 5.33, 5.15, C.accent6, "context card");
  s.addImage({ data: ic.robot, x: 7.7, y: 1.7, w: 0.5, h: 0.5, altText: "agent" });
  text(s, "Kept in sync automatically", { x: 8.35, y: 1.75, w: 4.2, h: 0.4, fontSize: 18, bold: true });
  text(s, [
    { text: "Hook in .claude/settings.json: after every Edit/Write → python gen_diagram.py, so CLASS_DIAGRAM.md always matches the code", options: { bullet: true, breakLine: true } },
    { text: ".mcp.json: the agent drives the real app (AI CONTROL) to check its own work", options: { bullet: true, breakLine: true } },
    { text: "PROMPTS.md: every build prompt, session by session, and the AI agent's own prompts", options: { bullet: true, breakLine: true } },
    { text: "Feature branches merged with merge commits (Network slide)", options: { bullet: true } },
  ], { x: 7.7, y: 2.45, w: 4.8, h: 4.0, fontSize: 14, color: C.background2, paraSpaceAfter: 8 });
  s.addNotes("The prompts are in PROMPTS.md. Context: the SRS, ProjectInfo, the auto-generated class diagram, screenshots, and test output. The agent verifies its own work by running the app and the test suites before anything is committed.");

  // ================================================================ 11 V&V
  s = content("Process and verification");
  s.addText("Verification and validation at four levels", { placeholder: "title" });
  const lv = [["Acceptance", "11 test cases on the real app, screenshots of every window and pop-up", C.accent6, "AT-1 … AT-11"],
    ["System", "real VS Code extension + profiler + app (Node stand-in for VS Code)", C.accent3, "e2e.js"],
    ["Integration", "profiler and YOUR CODE on 20 real projects, two samplers", C.accent2, "20 projects"],
    ["Unit", "Model + Controller classes, fakes for hardware and Ollama", C.accent1, "146 cases"]];
  lv.forEach(([n, d, col, k], i) => {
    const wdt = 3.4 + i * 0.9, x = M + (6.6 - wdt) / 2, y = 1.45 + i * 1.3;
    card(s, x, y, wdt, 1.12, col, `level ${n}`);
    text(s, n, { x: x + 0.2, y: y + 0.1, w: wdt - 0.4, h: 0.38, fontSize: 17, bold: true, color: col });
    text(s, k, { x: x + 0.2, y: y + 0.1, w: wdt - 0.4, h: 0.38, fontSize: 13, align: "right", color: C.background2 });
    text(s, d, { x: x + 0.2, y: y + 0.52, w: wdt - 0.4, h: 0.55, fontSize: 11.5, color: C.background2 });
  });
  s.addChart(pres.charts.BAR, [{ name: "Unit test coverage", labels: ["analysis", "agent", "control", "telemetry", "workloads", "main"],
    values: [99, 84, 82, 49, 40, 24] }], {
    x: 7.5, y: 1.4, w: 5.25, h: 4.6, barDir: "bar", chartColors: [HEX.accent1], showValue: true, dataLabelPosition: "outEnd",
    dataLabelColor: HEX.lt1, dataLabelFontSize: 11, dataLabelFontFace: "+mn-lt", dataLabelFormatCode: "0\"%\"",
    showTitle: true, title: "Statement + branch coverage, unit tests (%)", titleColor: HEX.lt1, titleFontSize: 13, titleFontFace: "+mn-lt",
    catAxisLabelColor: HEX.lt2, catAxisLabelFontFace: "+mn-lt", catAxisLabelFontSize: 12, catAxisOrientation: "maxMin",
    valAxisHidden: true, valAxisMaxVal: 110, valGridLine: { style: "none" }, catGridLine: { style: "none" }, showLegend: false,
    plotArea: { fill: { color: HEX.dk1 } },
  });
  caption(s, "main.py is mostly drawing code; its controller methods are covered. python tests/unit/run.py --coverage", 7.5, 6.15, 5.3);
  s.addNotes("Unit tests cover thresholds, the agent loop with a scripted model, the sandbox, the control API and the App controller logic on a stand-in object. The integration and system tests run real programs. Acceptance tests run each use case on the real app.");

  // ================================================================ 12 defects found
  s = content("Process and verification");
  s.addText("Testing found four real defects before the demo", { placeholder: "title" });
  const bugs = [
    ["Regression: YOUR CODE didn't start on Windows", "Found by the full test suite (vis_test.py). Cause: two new lines placed between \"if win:\" and its \"else:\", so Windows got \"sh -c\".",
      "Fix 348ec68 + regression test", C.accent5],
    ["CPU-only test reported \"GPU waiting on the CPU\"", "Found by acceptance test AT-3. Cause: another program's GPU burst made the finder think the test used the GPU.",
      "Fix: tests without the GPU are judged without GPU readings + tests", C.accent6],
    ["Program output garbled: \"·\" showed as \"Â·\"", "Found by acceptance test AT-8 (log screenshot). Cause: the runner decoded the program's UTF-8 output as cp1252.",
      "Fix: read UTF-8, set PYTHONIOENCODING + regression test", C.accent3],
    ["Error pop-up showed doubled backslashes", "Found by acceptance test AT-7 (pop-up screenshot: C:\\\\no\\\\such). Cause: the message used repr() of the command.",
      "Fix: plain text + regression test", C.accent4],
  ];
  bugs.forEach(([h, a, c, col], i) => {
    const y = 1.35 + i * 1.32;
    card(s, M, y, W - 2 * M, 1.18, col, `defect ${i + 1}`);
    s.addImage({ data: ic.bug, x: M + 0.3, y: y + 0.2, w: 0.4, h: 0.4, altText: "defect" });
    text(s, h, { x: M + 0.95, y: y + 0.13, w: 10.9, h: 0.38, fontSize: 17, bold: true, fontFace: THEME.headFontFace });
    text(s, a, { x: M + 0.95, y: y + 0.53, w: 7.95, h: 0.6, fontSize: 12.5, color: C.background2 });
    text(s, c, { x: M + 9.1, y: y + 0.53, w: 2.85, h: 0.6, fontSize: 12.5, bold: true, color: col });
  });
  s.addNotes("The first was a real regression from the previous session: the test suite caught it, and a unit test now guards it. The other three were visible only on screen - in a summary, the log and a pop-up - which is why the acceptance run captures every window and pop-up. The test driver had its own defect too: screenshots taken from a worker thread stalled Tk's timers; it now captures on the window's own thread.");
  // ================================================================ acceptance section
  pres.addSection({ title: "Acceptance tests" });
  s = pres.addSlide({ masterName: "HCV Section", sectionTitle: "Acceptance tests" });
  s.addText("Acceptance tests", { placeholder: "title" });
  s.addText("The SRS use cases on the real app - real workloads, real sensors", { placeholder: "body" });

  // ================================================================ 13 AT table
  s = content("Acceptance tests");
  s.addText("11 acceptance test cases, traced to the use cases", { placeholder: "title" });
  const ats = [["AT-1", "UC-1, UC-10", "Start: hardware detected, live readings"], ["AT-2", "UC-3, UC-4", "Select / reselect a part"],
    ["AT-3", "UC-5, 9, 10, 11", "2D on the CPU to completion"], ["AT-4", "UC-6", "3D on the GPU"],
    ["AT-5", "UC-7", "AI Benchmark Mode"], ["AT-6", "UC-8", "STOP a running test"],
    ["AT-7", "UC-13", "Invalid starts refused with a message"], ["AT-8", "ultimate goal", "YOUR CODE + bottlenecks"],
    ["AT-9", "ultimate goal", "Hot spots in VS Code"], ["AT-10", "stretch", "COMPARE and REPLAY"], ["AT-11", "stretch", "RAM, disk, PCIe, ML tests"]];
  const head = (t) => ({ text: t, options: { bold: true, color: C.accent1 } });
  const res = (id) => { const r = atResult(id); return r && !r.error ? { text: "pass", options: { color: C.accent1, bold: true } } : { text: r ? "fail" : "not run", options: { color: C.accent5, bold: true } }; };
  const half = (list) => [[head("Test"), head("Covers"), head("What"), head("Result")], ...list.map(([a, u, w]) => [a, u, w, res(a)])];
  s.addTable(half(ats.slice(0, 6)), { x: M, y: 1.45, w: 5.95, colW: [0.8, 1.5, 2.85, 0.8], fontSize: 12.5, color: C.background1, fill: PANEL,
    border: { type: "solid", pt: 0.5, color: "2A2A2A" }, rowH: 0.55, margin: [0.04, 0.08, 0.04, 0.08] });
  s.addTable(half(ats.slice(6)), { x: 6.78, y: 1.45, w: 5.95, colW: [0.8, 1.5, 2.85, 0.8], fontSize: 12.5, color: C.background1, fill: PANEL,
    border: { type: "solid", pt: 0.5, color: "2A2A2A" }, rowH: 0.55, margin: [0.04, 0.08, 0.04, 0.08] });
  caption(s, "UC-12 (telemetry lost) and UC-14 (thermal abort) can't be triggered safely on real hardware: verified by unit tests (SafetyTest). UC-2 withdrawn. Steps and observed results: docs/ACCEPTANCE_TESTS.md", M, 5.65, W - 2 * M);
  s.addNotes("Each acceptance test lists preconditions, steps, expected and observed results in docs/ACCEPTANCE_TESTS.md, with screenshots of every window and pop-up. They were executed by tests/acceptance/run_acceptance.py, which presses the same handlers the buttons call.");

  // ================================================================ 14 AT-4
  s = content("Acceptance tests");
  s.addText("AT-3: a 2D test on the CPU, from START to summary", { placeholder: "title" });
  for (const [i, f, cap] of [[0, "AT-3a_cpu_2d_ready.png", "1  CPU selected, 2D chosen"], [1, "AT-3b_cpu_2d_running.png", "2  running: every core busy, live numbers"],
    [2, "AT-3c_cpu_2d_summary.png", "3  completed: summary, saved, compared"]]) {
    const x = M + i * 4.1;
    await picture(s, path.join(AT, f), x, 1.3, 3.85, 5.1, cap);
    caption(s, cap, x, 6.5, 3.85);
  }
  s.addNotes("Real Mandelbrot frames on every core for 30 s. The clock shows the live drawing and sensor rate. The summary shows min/avg/peak per metric, the change from the previous run, and the bottleneck finder's result (all cores busy = good parallel use).");

  // ================================================================ 15 AT-8
  s = content("Acceptance tests");
  s.addText("AT-7: invalid requests are refused, nothing starts", { placeholder: "title" });
  const pops = [["AT-7a_no_part_popup.png", "START with no part selected"], ["AT-7c_no_command_popup.png", "YOUR CODE with no command"],
    ["AT-7d_cannot_start_popup.png", "a program that doesn't exist"]];
  for (let i = 0; i < 3; i++) {
    const y = 1.45 + i * 1.75;
    await picture(s, path.join(AT, pops[i][0]), M, y, 5.6, 1.5, pops[i][1], "left");
    caption(s, pops[i][1], 6.4, y + 0.55, 2.4);
  }
  await picture(s, path.join(AT, "AT-7e_after_failed_start.png"), 8.6, 1.35, 4.15, 5.4, "Window after the failed start", "right");
  s.addNotes("UC-13: each refusal is a pop-up plus the status line; nothing is shown as running and no telemetry stays on screen. These same refusals are unit tested in StartValidationTest.");

  // ================================================================ 16 AT-9 / AT-10
  s = content("Acceptance tests");
  s.addText("AT-8 and AT-9: your own code, in the app and in VS Code", { placeholder: "title" });
  await picture(s, path.join(AT, "AT-8c_your_code_summary.png"), M, 1.3, 4.6, 5.1, "YOUR CODE summary");
  caption(s, "AT-8  YOUR CODE: the demo project ran; summary with bottlenecks", M, 6.5, 4.6);
  if (fs.existsSync(path.join(AT, "AT-9a_vscode_compute.png"))) {
    await picture(s, path.join(AT, "AT-9a_vscode_compute.png"), 5.45, 1.3, 7.28, 5.1, "VS Code with hot-spot boxes");
    caption(s, "AT-9  VS Code: the slowest lines boxed and ranked, with the hardware while each ran", 5.45, 6.5, 7.28);
  }
  s.addNotes("The same demo project: in the desktop app YOUR CODE gives the hardware view and bottlenecks; the extension's profiler puts ranked boxes on the slow lines.");

  // ================================================================ 17 AT-2 / AT-6
  s = content("Acceptance tests");
  s.addText("AT-5 and AT-11: the AI agent, and the other parts", { placeholder: "title" });
  await picture(s, path.join(AT, "AT-5b_ai_working.png"), M, 1.3, 3.9, 5.1, "AI agent working");
  caption(s, "AT-5  the local model writes and runs code itself", M, 6.5, 3.9);
  await picture(s, path.join(AT, "AT-11_disk_running.png"), 4.7, 1.3, 3.9, 5.1, "Disk I/O test running");
  caption(s, "AT-11  disk I/O: 1 GB file, cache bypassed", 4.7, 6.5, 3.9);
  await picture(s, path.join(AT, "AT-11_ml_gpu_running.png"), 8.8, 1.3, 3.93, 5.1, "ML training on the GPU");
  caption(s, "AT-11  ML: a small network trains on the GPU", 8.8, 6.5, 3.93);
  s.addNotes("AI Benchmark: a local Ollama model chooses its own steps; its code runs in the sandbox, and the summary counts its tool calls and blocked actions. AT-11 runs the remaining tests: RAM bandwidth (compared with the stopped run of AT-6), disk I/O, the PCIe bus and ML training.");

  // ================================================================ 18 AT-11
  s = content("Acceptance tests");
  s.addText("AT-10: every run is recorded, compared and replayed", { placeholder: "title" });
  await picture(s, path.join(AT, "AT-10a_compare_window.png"), M, 1.3, 7.2, 5.1, "Compare window");
  caption(s, "COMPARE: two runs side by side, every average with its change", M, 6.5, 7.2);
  await picture(s, path.join(AT, "AT-10b_replay_running.png"), 8.0, 1.3, 4.73, 5.1, "Replay");
  caption(s, "REPLAY: amber clock, never passes as live", 8.0, 6.5, 4.73);
  s.addNotes("Runs are JSON files in runs/. Replay plays the recorded samples through the same diagram, clearly marked as a recording; no safety checks run on a replay.");

  // ================================================================ 19 git network
  pres.addSection({ title: "Conclusion" });
  s = content("Conclusion");
  s.addText("Git repository network: branches and merges", { placeholder: "title" });
  const net = path.join(__dirname, "git_network.png");
  if (fs.existsSync(net)) await picture(s, net, M, 1.3, W - 2 * M, 5.2, "Git network graph");
  caption(s, "Drawn from the repository history (docs/presentation/git_graph.py). Same graph: github.com/AndreMercado/HardwareComputeVisualizer → Insights → Network", M, 6.6, W - 2 * M);
  s.addNotes("Feature branches unit-tests, design-docs, acceptance-tests, remove-remote, acceptance-docs and presentation, each merged into main with a merge commit.");

  // ================================================================ 20 conclusion
  s = content("Conclusion");
  s.addText("Done, verified, and what comes next", { placeholder: "title" });
  const cols = [[ic.check, "Done", C.accent1, ["All use cases of SRS v1.1, MVP, stretch and the ultimate goal", "Real workloads, real sensors, nothing simulated", "Desktop app + VS Code extension"]],
    [ic.vial, "Verified", C.accent2, ["146 unit tests, 20 integration projects, system tests", "11 acceptance tests with screenshots", "4 defects found and fixed by testing"]],
    [ic.branch, "Next", C.accent3, ["Split App: move the controller into a TestSession class", "Linux / macOS desktop (sensors differ)", "Import runs from other PCs to compare machines"]]];
  cols.forEach(([img, h, col, items], i) => {
    const x = M + i * 4.1;
    card(s, x, 1.5, 3.85, 4.9, col, `conclusion ${h}`);
    s.addImage({ data: img, x: x + 0.3, y: 1.8, w: 0.55, h: 0.55, altText: h });
    text(s, h, { x: x + 1.0, y: 1.83, w: 2.6, h: 0.5, fontSize: 24, bold: true, color: col, fontFace: THEME.headFontFace });
    text(s, items.map((t, j) => ({ text: t, options: { bullet: true, breakLine: j < items.length - 1 } })),
      { x: x + 0.3, y: 2.65, w: 3.3, h: 3.5, fontSize: 15, color: C.background2, paraSpaceAfter: 10 });
  });
  s.addNotes("Questions welcome: the generated code (analysis.py, telemetry.Poller, App._tick_running) and the tests (tests/unit) are good places to look.");

  await pres.writeFile({ fileName: OUT });
  if (APPLY_THEME) {
    const { applyTheme } = require(APPLY_THEME);
    await applyTheme(OUT, THEME);
  }
  console.log("wrote " + OUT);
}

main().catch((e) => { console.error(e); process.exit(1); });
