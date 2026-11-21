// HCV Hot Spots: runs a Python file under profiler.py and draws boxes around the blocks
// of code that take the most time (and, optionally, memory). The profiler rewrites
// .hcv/profile.json next to the file every second, so the boxes update live while the
// program runs, and come back whenever the file is opened again.
const vscode = require("vscode");
const cp = require("child_process");
const fs = require("fs");
const path = require("path");
const http = require("http");

const RANK_COLORS = ["#ff4d4d", "#ff9f1a", "#ffd60a", "#3ddc97", "#4dd2ff", "#c77dff"];
const MEMORY_COLOR = "#4da6ff";
const MEMORY_RANKS = ["#4da6ff", "#6fb8ff", "#8fc8ff", "#b0d8ff", "#cfe7ff"];  // #1 memory = strongest blue
let types = null;        // decoration types, made once
let current = null;      // {path, data, mtime} - the profile read last
const cache = {};        // profile path -> {path, data, mtime}
let proc = null;         // the profiler process we started
let viaVisualizer = false;
let runTerminal = null;  // the terminal of a yellow-button run (a normal run, profiled)
let timer = null;
let output, statusItem;

// ------------------------------------------------------------------ helpers
const norm = (p) => path.resolve(p).toLowerCase();

function cfg(key) {
  return vscode.workspace.getConfiguration("hcv").get(key);
}

// The Hardware Compute Visualizer app's folder - only needed for "Profile in the Hardware Visualizer"
// (its control_token.txt). hcv-path.txt is written by the old install.ps1.
function visualizerFolder() {
  const set = cfg("visualizerFolder");
  if (set) return set;
  try {
    return fs.readFileSync(path.join(__dirname, "hcv-path.txt"), "utf8").trim();
  } catch (e) {
    return "";
  }
}

// The profiler shipped inside the extension (python/profiler.py + analysis.py + nvgpu.py), so the
// extension works on its own; a visualizer folder with a profiler.py is the fallback.
function profilerPath() {
  for (const sub of ["app", "python"]) {
    const bundled = path.join(__dirname, sub, "profiler.py");
    if (fs.existsSync(bundled)) return bundled;
  }
  const bundled = path.join(__dirname, "app", "profiler.py");
  const vf = visualizerFolder();
  return vf ? path.join(vf, "profiler.py") : bundled;
}

// Where a run of this file writes its profile.
function profileFor(file) {
  return path.join(path.dirname(file), ".hcv", "profile.json");
}

function readProfile(p) {
  try {
    const st = fs.statSync(p);
    const hit = cache[p];
    if (hit && hit.mtime === st.mtimeMs) return (current = hit);
    const data = JSON.parse(fs.readFileSync(p, "utf8"));
    // the files this profile has something to show in (hot lines, memory, crash)
    const files = new Set([...(data.lines || []), ...(data.memory || []), ...(data.crash ? [data.crash] : [])]
      .map((l) => norm(l.file)));
    current = cache[p] = { path: p, data, mtime: st.mtimeMs, files };
    knownProfiles.add(p);
    return current;
  } catch (e) {
    return null; // not there yet, or caught mid-write: try again on the next tick
  }
}

// Every profile in the open folders (found at start, then kept up to date by a file watcher), so
// a module shows its boxes wherever the script that imported it lives (src/, a sibling folder...).
const knownProfiles = new Set();

// The profile to show for a file: the newest one that has something in this file - its own folder's,
// one in a folder above it, or any in the workspace. Else its own folder's (empty while a run starts).
function profileShowing(file) {
  const f = norm(file);
  const candidates = new Set([profileFor(file), ...knownProfiles]);
  let dir = path.dirname(path.dirname(file));
  for (let i = 0; i < 30; i++) {
    candidates.add(path.join(dir, ".hcv", "profile.json"));
    const up = path.dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  let best = null;
  for (const p of candidates) {
    const prof = readProfile(p);
    if (prof && prof.files.has(f) && (!best || prof.mtime > best.mtime)) best = prof;
  }
  return best || readProfile(profileFor(file));
}

function profileMtime(file) {
  const prof = profileShowing(file);
  return prof ? `${prof.path}|${prof.mtime}` : "";
}

// The folder VS Code's own "Run Python File" runs a file from: the workspace folder (or the
// file's folder with python.terminal.executeInFileDir). Paths like open("data/x.csv") need it.
function runFolder(file) {
  const ws = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(file));
  const inFileDir = vscode.workspace.getConfiguration("python.terminal", vscode.Uri.file(file)).get("executeInFileDir");
  return !ws || inFileDir ? path.dirname(file) : ws.uri.fsPath;
}

// The project's top folder, whose files count as "your code": the workspace folder. Outside a
// workspace the profiler finds it itself (.git, pyproject.toml, ... above the file).
function projectRoot(file) {
  const ws = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(file));
  return ws ? ws.uri.fsPath : null;
}

// The Python to run: hcv.pythonPath if set, else the interpreter picked in the Python extension
// (the project's venv), else "python".
async function pythonFor(file) {
  const set = cfg("pythonPath");
  if (set && set !== "python") return set;
  const uri = vscode.Uri.file(file);
  try {
    const ext = vscode.extensions.getExtension("ms-python.python");
    if (ext) {
      if (!ext.isActive) await ext.activate();
      const envs = ext.exports && ext.exports.environments;
      if (envs && envs.getActiveEnvironmentPath) {
        const env = envs.getActiveEnvironmentPath(uri);
        const resolved = envs.resolveEnvironment ? await envs.resolveEnvironment(env) : null;
        const exe = (resolved && resolved.executable && resolved.executable.uri && resolved.executable.uri.fsPath) || env.path;
        if (exe && fs.existsSync(exe)) return exe;
      }
    }
  } catch (e) { /* no Python extension, or an older one */ }
  try {
    const ws = vscode.workspace.getWorkspaceFolder(uri);
    const exe = await vscode.commands.executeCommand("python.interpreterPath", ws ? { workspaceFolder: ws.uri.fsPath } : undefined);
    if (exe && fs.existsSync(exe)) return exe;
  } catch (e) { /* command not there */ }
  return "python";
}

function makeTypes() {
  const box = (color, style) => {
    const mk = (width) => vscode.window.createTextEditorDecorationType({
      isWholeLine: true, borderStyle: style, borderColor: color, borderWidth: width,
      overviewRulerColor: color, overviewRulerLane: vscode.OverviewRulerLane.Right,
    });
    return { single: mk("2px"), top: mk("2px 2px 0 2px"), mid: mk("0 2px 0 2px"), bot: mk("0 2px 2px 2px") };
  };
  return {
    ranks: RANK_COLORS.map((c) => box(c, "solid")),
    memory: box(MEMORY_COLOR, "dashed"),
    memRanks: MEMORY_RANKS.map((c, i) => {
      const t = box(c, "dashed");
      // a blue wash under the lines that hold the most memory: the strongest for #1
      t.wash = vscode.window.createTextEditorDecorationType({ isWholeLine: true,
        backgroundColor: `rgba(77, 166, 255, ${(0.16 - i * 0.03).toFixed(2)})` });
      return t;
    }),
    memLabel: vscode.window.createTextEditorDecorationType({}),
    faint: box("rgba(138, 143, 152, 0.55)", "dashed"),
    faintWarn: box("rgba(255, 159, 26, 0.6)", "dashed"),
    crash: vscode.window.createTextEditorDecorationType({
      isWholeLine: true, borderStyle: "dotted", borderColor: "#ff4d4d", borderWidth: "2px",
      backgroundColor: "rgba(255, 77, 77, 0.10)", overviewRulerColor: "#ff4d4d",
      overviewRulerLane: vscode.OverviewRulerLane.Full }),
    label: vscode.window.createTextEditorDecorationType({}),
  };
}

// ------------------------------------------------------------------ hot spots
// Is line n (1-based) only a blank line or a comment?
function isFiller(doc, n) {
  if (!doc || n < 1 || n > doc.lineCount) return false;
  const t = doc.lineAt(n - 1).text.trim();
  return !t || t.startsWith("#");
}

// Two timed lines go in one box when they sit under each other: next to each other, with only blank
// or comment lines between them, or one untimed line apart in the same function.
function joinable(doc, end, next, sameFunc) {
  if (next - end <= 1) return true;
  if (sameFunc && next - end <= 2) return true;
  if (!doc) return false;
  for (let n = end + 1; n < next; n++) if (!isFiller(doc, n)) return false;
  return true;
}

// Groups items with a .line (sorted) into runs of lines under each other.
function groupLines(items, doc, sameFunc) {
  const groups = [];
  for (const it of items.slice().sort((a, b) => a.line - b.line)) {
    const g = groups[groups.length - 1];
    if (g && joinable(doc, g.end, it.line, sameFunc && sameFunc(g.items[g.items.length - 1], it))) {
      g.end = it.line;
      g.items.push(it);
    } else {
      groups.push({ start: it.line, end: it.line, items: [it] });
    }
  }
  return groups;
}

// Every block of timed lines in one file, biggest first.
function allBlocks(data, file, doc) {
  const mine = (data.lines || []).filter((l) => norm(l.file) === norm(file) && l.share >= 0.003);
  // "same function" = same enclosing def in the code (a generator expression counts for its function)
  const sameDef = doc ? (a, b) => enclosingDef(doc, a.line - 1) === enclosingDef(doc, b.line - 1) : (a, b) => a.func === b.func;
  const blocks = groupLines(mine, doc, sameDef).map((g) => {
    const funcs = [...new Set(g.items.map((l) => l.func))];
    const named = funcs.filter((f) => !f.startsWith("<"));  // <genexpr>, <listcomp>: say the real function
    return { start: g.start, end: g.end, lines: g.items, func: (named.length ? named : funcs).join(", ") };
  });
  for (const b of blocks) {
    b.seconds = b.lines.reduce((s, l) => s + l.seconds, 0);
    b.share = b.lines.reduce((s, l) => s + l.share, 0);
    const withHw = b.lines.filter((l) => l.hw);
    const w = withHw.reduce((s, l) => s + l.seconds, 0);
    if (w > 0) {
      b.hw = {};
      for (const k of ["proc", "cpu", "core_max", "gpu"]) {
        b.hw[k] = withHw.reduce((s, l) => s + (l.hw[k] || 0) * l.seconds, 0) / w;
      }
    }
  }
  return blocks.sort((a, b) => b.seconds - a.seconds);
}

// The boxed blocks: the biggest ones over hcv.minShare.
function hotBlocks(data, file, doc) {
  return allBlocks(data, file, doc).filter((b) => b.share >= cfg("minShare")).slice(0, cfg("maxBoxes"));
}

// What the hardware was doing while this block ran. "proc" is the CPU this program itself
// used (100 = one core), so other programs on the PC don't blur it.
function hardwareNote(b, data) {
  const h = b.hw;
  if (!h) return "";
  const cores = data.cores || 1;
  const parts = [];
  const gpuBusy = data.gpu_used ? h.gpu : 0;
  if (h.proc === undefined) {  // profiles made before "proc" existed
    parts.push(`CPU ${h.cpu.toFixed(0)}%`);
  } else if (h.proc < 25 && gpuBusy < 20) {
    return "waiting - CPU idle (sleep, disk, network)";
  } else if (h.proc < 150) {
    parts.push(cores > 1 ? `1 of ${cores} cores busy - single-threaded` : "CPU busy");
  } else {
    parts.push(`${(h.proc / 100).toFixed(1)} of ${cores} cores busy`);
  }
  if (data.gpu_used) {
    if (h.gpu >= 90) parts.push("GPU fully busy");
    else if (h.gpu < 40) parts.push(`GPU idle ${(100 - h.gpu).toFixed(0)}% of the time`);
    else parts.push(`GPU ${h.gpu.toFixed(0)}%`);
  }
  return parts.join(", ");
}

function fmtSeconds(s) {
  return s >= 10 ? `${s.toFixed(0)} s` : s >= 1 ? `${s.toFixed(1)} s` : `${(s * 1000).toFixed(0)} ms`;
}

function fmtBytes(b) {
  return b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : b >= 1e6 ? `${(b / 1e6).toFixed(0)} MB` : `${(b / 1e3).toFixed(0)} KB`;
}

// ------------------------------------------------------------------ everything else the run recorded
// Bottleneck patterns (the visualizer's analysis.py), found in the hardware samples and tied by the
// profiler to the lines that were running while they held.
const PATTERN_SHORT = {
  serial: "serial code - 1 core maxed", gpu_starved: "GPU waiting on the CPU", pcie: "PCIe transfers busy",
  disk: "disk busy", ram: "RAM almost full", idle: "waiting (sleep / network / locks)",
  gpu_bound: "GPU fully busy (good)", cpu_bound: "all cores busy (good)",
};
const PATTERN_ADVICE = {
  serial: "Spread the work over the cores: multiprocessing, vectorised numpy / native libraries, or threads in code that releases the GIL.",
  gpu_starved: "Feed the GPU faster: bigger batches, load data in worker processes, keep data on the GPU, avoid per-step syncs such as .item().",
  pcie: "Copy data to the GPU once and keep it there; use pinned memory and fewer, bigger copies.",
  disk: "Read less or cache it in RAM, read in big sequential blocks, or use a faster drive.",
  ram: "Use less memory at once (stream or chunk the data, smaller batches) or add RAM.",
  idle: "Look for waits: network calls, sleeps, lock contention, or work done one request at a time.",
  gpu_bound: "Faster only with a faster GPU or less GPU work.", cpu_bound: "Faster only with a faster CPU or less work.",
};
const PROBLEMS = new Set(["serial", "gpu_starved", "pcie", "disk", "ram", "idle"]);
const WARN_COLOR = "#ff9f1a";
const FAINT_COLOR = "#8a8f98";

// the visualizer's sensors are better (LHM temperatures, disk counters): its patterns win when linked
const lineStates = (l) => l.vis_states || l.states || {};

// problem patterns that held for at least half of this line's time, most first
function lineProblems(l) {
  return Object.entries(lineStates(l)).filter(([k, v]) => PROBLEMS.has(k) && v >= 0.5)
    .sort((a, b) => b[1] - a[1]).map(([k]) => k);
}

function blockProblems(b) {
  const t = {};
  for (const l of b.lines) for (const [k, v] of Object.entries(lineStates(l))) {
    if (PROBLEMS.has(k)) t[k] = (t[k] || 0) + v * l.seconds;
  }
  return Object.entries(t).filter(([, v]) => v >= 0.5 * b.seconds).sort((a, b2) => b2[1] - a[1]).map(([k]) => k);
}

const num = (v) => typeof v === "number" && isFinite(v);

// One line's hardware, as short "name value" parts: this program's own CPU, the machine's GPU / RAM /
// disk (profiler), then the visualizer's sensors when this run was recorded there.
function hwParts(l, data) {
  const out = [];
  // a line that ran only in a child process (multiprocessing) has no readings of its own: the whole
  // run's averages stand in, and say so
  const own = !!l.hw;
  const a = data.averages || {};
  const h = l.hw || { proc: a.proc, gpu: a.gpu_util, vram: a.gpu_vram, ram: a.ram_util, disk: a.disk_util, board: a.board_util };
  const v = l.vis_hw || {};
  if (num(h.proc)) out.push(`${(h.proc / 100).toFixed(1)} of ${data.cores || "?"} cores busy` + (own ? "" : " (whole run - this line ran in another process)"));
  if (num(h.gpu) && data.gpu_used) out.push(`GPU ${h.gpu.toFixed(0)}%`);
  if (num(h.vram) && data.gpu_used) out.push(`VRAM ${h.vram.toFixed(1)} GB`);
  if (num(h.ram)) out.push(`RAM ${h.ram.toFixed(0)}%`);
  const disk = num(v.disk_util) ? v.disk_util : h.disk;
  if (num(disk) && disk >= 1) out.push(`disk ${disk.toFixed(0)}% busy`);
  if (num(v.cpu_temp)) out.push(`CPU ${v.cpu_temp.toFixed(0)} °C`);
  if (num(v.cpu_power)) out.push(`CPU ${v.cpu_power.toFixed(0)} W`);
  if (num(v.gpu_temp)) out.push(`GPU ${v.gpu_temp.toFixed(0)} °C`);
  if (num(v.gpu_power)) out.push(`GPU ${v.gpu_power.toFixed(0)} W`);
  if (data.gpu_used && num(v.pcie_rx) && num(v.pcie_tx) && (v.pcie_rx + v.pcie_tx) >= 50) out.push(`PCIe ${v.pcie_rx.toFixed(0)}↓ ${v.pcie_tx.toFixed(0)}↑ MB/s`);
  const board = !data.gpu_used ? null : num(v.board_util) ? v.board_util : h.board;
  if (num(board) && board >= 5) out.push(`PCIe link ${board.toFixed(0)}% busy`);
  return out;
}

function adviceFor(data, key) {
  const f = [...((data.visualizer || {}).findings || []), ...(data.findings || [])].find((x) => x.key === key);
  return (f && f.advice) || PATTERN_ADVICE[key] || "";
}

// Markdown for one line: time, hardware while it ran, bottlenecks with what to do.
function lineMarkdown(l, data) {
  const parts = show("hardware") ? hwParts(l, data) : [];
  let md = `**Line ${l.line}** in \`${l.func}\` · ${fmtSeconds(l.seconds)} · ${(100 * l.share).toFixed(1)}% of the run time`;
  if (parts.length) md += `\n\nWhile it ran: ${parts.join(" · ")}`;
  const st = !show("bottlenecks") ? [] : Object.entries(lineStates(l)).filter(([, v]) => v >= 0.2).sort((a, b) => b[1] - a[1]);
  if (st.length) {
    md += "\n\n" + st.map(([k, v]) => `${PROBLEMS.has(k) ? "⚠" : "✓"} **${PATTERN_SHORT[k] || k}** - ${(100 * v).toFixed(0)}% of this line's time` +
      (PROBLEMS.has(k) ? `  \n   _${adviceFor(data, k)}_` : "")).join("\n\n");
  }
  return md;
}

// Time per function in one file (only functions of this file that ran).
function funcTotals(data, file) {
  const f = norm(file);
  const by = {};
  for (const l of data.lines || []) {
    if (norm(l.file) !== f) continue;
    const t = by[l.func] = by[l.func] || { func: l.func, seconds: 0, share: 0, first: l.line, last: l.line, lines: 0 };
    t.seconds += l.seconds;
    t.share += l.share;
    t.first = Math.min(t.first, l.line);
    t.last = Math.max(t.last, l.line);
    t.lines++;
  }
  return Object.values(by).sort((a, b) => b.seconds - a.seconds);
}

const ENGINE_TEXT = { "py-spy": "py-spy (exact)", builtin: "built-in sampler (≈)" };

// One line about the run: how it ended, how long, how measured, the main bottleneck.
function runSummary(data) {
  const parts = [data.running ? "running" : (data.result || "done"), fmtSeconds(data.elapsed_s || 0),
    ENGINE_TEXT[data.engine] || data.engine || ""];
  if (data.threads > 1) parts.push(`${data.threads} threads/processes`);
  const fs2 = (data.visualizer && data.visualizer.findings) || data.findings || [];
  const top = fs2.find((f) => f.problem);
  if (top) parts.push(`⚠ ${PATTERN_SHORT[top.key] || top.title} ${(100 * top.share).toFixed(0)}% of the run`);
  if (data.visualizer) parts.push("+ Hardware Visualizer sensors");
  if (data.hw_missing) parts.push("no hardware readings: " + data.hw_missing);
  return parts.filter(Boolean).join(" · ");
}

// ------------------------------------------------------------------ what to show
// Each layer can be switched on and off from the eye dropdown next to the yellow ▶, the right-click
// menu in the code, or "HCV: Choose what to show". Remembered across sessions.
const LAYERS = {
  speed: { label: "Speed - boxes around the slowest code", icon: "flame" },
  lines: { label: "Speed of every other line (faint)", icon: "list-flat" },
  memory: { label: "Memory - where it is held, biggest first", icon: "database" },
  functions: { label: "Totals above each function + run summary", icon: "symbol-method" },
  bottlenecks: { label: "Bottlenecks (⚠ serial code, waiting, disk...)", icon: "warning" },
  hardware: { label: "Hardware in the labels (cores, GPU, °C, W)", icon: "circuit-board" },
  crash: { label: "Crash line", icon: "error" },
};
const PRESETS = {
  everything: { label: "Everything", on: Object.keys(LAYERS) },
  speed: { label: "Speed only - where the time goes", on: ["speed", "lines", "functions", "crash"] },
  memory: { label: "Memory only - where the memory goes", on: ["memory", "functions", "crash"] },
  speedMemory: { label: "Speed + memory, nothing else", on: ["speed", "memory", "crash"] },
};
let extContext = null;

function show(layer) {
  const v = extContext ? extContext.globalState.get("hcv.show." + layer) : undefined;
  return v === undefined ? true : v;
}

async function setLayer(layer, on) {
  if (extContext) await extContext.globalState.update("hcv.show." + layer, on);
  await vscode.commands.executeCommand("setContext", "hcv.show." + layer, on);
}

async function applyLayers(on) {
  for (const k of Object.keys(LAYERS)) await setLayer(k, on.includes(k));
  redrawAll();
}

async function chooseLayers() {
  const items = [
    ...Object.entries(PRESETS).map(([k, pr]) => ({ label: `$(layers) ${pr.label}`, preset: k, alwaysShow: true })),
    { label: "", kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1 },
    ...Object.entries(LAYERS).map(([k, l]) => ({ label: `$(${l.icon}) ${l.label}`, layer: k, picked: show(k) })),
  ];
  const qp = vscode.window.createQuickPick();
  qp.title = "HCV: what to show in the code";
  qp.placeholder = "Tick what to see (Enter to apply), or pick a preset";
  qp.canSelectMany = true;
  qp.items = items.filter((i) => i.layer || i.preset);
  qp.selectedItems = qp.items.filter((i) => i.picked);
  return new Promise((resolve) => {
    qp.onDidAccept(async () => {
      const preset = qp.selectedItems.find((i) => i.preset && !i.picked);
      const on = preset ? PRESETS[preset.preset].on : qp.selectedItems.filter((i) => i.layer).map((i) => i.layer);
      qp.hide();
      await applyLayers(on);
      resolve(on);
    });
    qp.onDidHide(() => { qp.dispose(); resolve(null); });
    qp.show();
  });
}

// "Where is it?": the slowest lines and the lines holding the most memory, in every file of the run
async function whereIsIt() {
  const e = vscode.window.activeTextEditor;
  const prof = e ? profileShowing(e.document.fileName) : null;
  if (!prof) {
    vscode.window.showInformationMessage("No run yet - press the yellow ▶ first.");
    return;
  }
  const data = prof.data;
  const root = data.root || path.dirname(data.script || "");
  const rel = (f) => path.relative(root, f) || path.basename(f);
  const sep = (label) => ({ label, kind: vscode.QuickPickItemKind ? vscode.QuickPickItemKind.Separator : -1 });
  const items = [sep("Speed - where the time goes")];
  for (const l of (data.lines || []).slice(0, 25)) {
    items.push({ label: `$(flame) ${(100 * l.share).toFixed(1)}%  ${fmtSeconds(l.seconds)}`,
      description: `${rel(l.file)}:${l.line}`, detail: `${l.func}${show("hardware") ? " · " + hwParts(l, data).join(" · ") : ""}`,
      file: l.file, line: l.line });
  }
  const mem = (data.memory || []).filter((m) => m.bytes >= 1e5).slice(0, 25);
  if (mem.length) {
    items.push(sep("Memory - where it is held"));
    const top = mem[0].bytes;
    for (const m of mem) {
      items.push({ label: `$(database) ${fmtBytes(m.bytes)}`, description: `${rel(m.file)}:${m.line}`,
        detail: `${(100 * m.bytes / top).toFixed(0)}% of the biggest`, file: m.file, line: m.line });
    }
  } else if (!data.memory) {
    items.push(sep("Memory: not measured - the yellow ▶ measures it"));
  }
  const pick = await vscode.window.showQuickPick(items.filter((i) => i.file || i.kind !== undefined),
    { title: "HCV: where in the code", matchOnDescription: true, matchOnDetail: true });
  if (!pick || !pick.file) return;
  const ed = await vscode.window.showTextDocument(vscode.Uri.file(pick.file));
  const pos = new vscode.Position(pick.line - 1, 0);
  ed.selection = new vscode.Selection(pos, pos);
  ed.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
}

// Lines holding the most memory in one file, biggest first (the yellow ▶ measures memory)
function memoryLines(data, file) {
  const minBytes = cfg("memoryMinBytes") || 1e6;
  return (data.memory || []).filter((m) => norm(m.file) === norm(file) && m.bytes >= minBytes)
    .sort((a, b) => b.bytes - a.bytes).slice(0, cfg("maxMemoryBoxes") || 5);
}

// Memory lines under each other as one block, biggest (sum) first
function memoryBlocks(data, file, doc) {
  const minBytes = cfg("memoryMinBytes") || 1e6;
  const mine = (data.memory || []).filter((m) => norm(m.file) === norm(file) && m.bytes >= 1e5 && m.line <= doc.lineCount);
  return groupLines(mine, doc, null).map((g) => ({ ...g, bytes: g.items.reduce((a, m) => a + m.bytes, 0) }))
    .filter((g) => g.bytes >= minBytes).sort((a, b) => b.bytes - a.bytes).slice(0, cfg("maxMemoryBoxes") || 5);
}

// ------------------------------------------------------------------ drawing
function clear(editor) {
  if (!types) return;
  for (const t of [...types.ranks, types.memory, ...types.memRanks, types.faint, types.faintWarn]) for (const k in t) editor.setDecorations(t[k], []);
  editor.setDecorations(types.crash, []);
  editor.setDecorations(types.label, []);
  editor.setDecorations(types.memLabel, []);
}

function boxRanges(start, end, doc) {
  const line = (n) => doc.lineAt(Math.min(n - 1, doc.lineCount - 1)).range;
  if (start === end) return { single: [line(start)], top: [], mid: [], bot: [] };
  const mid = [];
  for (let n = start + 1; n < end; n++) mid.push(line(n));
  return { single: [], top: [line(start)], mid, bot: [line(end)] };
}

function decorate(editor) {
  if (!editor || editor.document.languageId !== "python" || editor.document.isUntitled) return;
  const file = editor.document.fileName;
  const prof = profileShowing(file);
  if (!types) types = makeTypes();
  clear(editor);
  if (!prof) return;
  const data = prof.data;
  const doc = editor.document;
  const labels = [];
  const live = data.running ? "LIVE · " : "";
  const approx = !data.running && data.engine !== "py-spy" ? "≈" : "";
  const how = data.engine === "py-spy"
    ? "Measured with py-spy (exact line times" + (data.threads > 1 ? `; ${data.threads} threads/processes, their times add up` : "") + ")."
    : data.running ? "Live estimate - the exact numbers replace it when the program ends (if py-spy is installed)."
      : "Built-in sampler: approximate - time inside long C calls (sort, regex, json...) can land on the next line. " +
        "`pip install py-spy` for exact numbers.";

  const every = allBlocks(data, file, doc);
  const blocks = show("speed") ? every.filter((b) => b.share >= cfg("minShare")).slice(0, cfg("maxBoxes")) : [];
  const boxed = new Set();
  blocks.forEach((b, i) => {
    for (let n = b.start; n <= b.end; n++) boxed.add(n);
    const t = types.ranks[Math.min(i, types.ranks.length - 1)];
    const r = boxRanges(b.start, b.end, doc);
    for (const k of ["single", "top", "mid", "bot"]) editor.setDecorations(t[k], r[k]);
    const n = b.end - b.start + 1;
    const imports = b.lines.every((l) => /^\s*(import|from)\s/.test(doc.lineAt(Math.min(l.line - 1, doc.lineCount - 1)).text));
    const hw = imports ? "one-time import cost" : show("hardware") ? hardwareNote(b, data) : "";
    const isAsync = b.lines.some((l) => /asyncio\.run\(|\.run_until_complete\(/.test(doc.lineAt(Math.min(l.line - 1, doc.lineCount - 1)).text));
    const probs = imports || !show("bottlenecks") ? [] : blockProblems(b);
    const text = `  ◀ #${i + 1} · ${live}${approx}${(100 * b.share).toFixed(0)}% of the run time · ${fmtSeconds(b.seconds)}` +
      ` · ${n} line${n > 1 ? "s" : ""}${hw ? " · " + hw : ""}${probs.length ? " · ⚠ " + PATTERN_SHORT[probs[0]] : ""}`;
    const hover = new vscode.MarkdownString(
      `**Hot spot #${i + 1}** - lines ${b.start}-${b.end} in \`${b.func}\`\n\n` +
      `${(100 * b.share).toFixed(1)}% of the time spent in your code (${fmtSeconds(b.seconds)})` +
      (hw ? `\n\nWhile it ran: ${hw}` : "") +
      (isAsync ? "\n\nThis is the event loop: time spent *awaiting* inside coroutines (sleeps, network) is counted " +
        "here, because a waiting coroutine isn't running anywhere." : "") +
      (probs.length ? "\n\n" + probs.map((k) => `⚠ **${PATTERN_SHORT[k]}** while these lines ran  \n_${adviceFor(data, k)}_`).join("\n\n") : "") +
      `\n\n_${how}_` + "\n\n| line | time | share | while it ran |\n|---|---|---|---|\n" +
      b.lines.map((l) => `| ${l.line} | ${fmtSeconds(l.seconds)} | ${(100 * l.share).toFixed(1)}% | ` +
        `${[...hwParts(l, data), ...lineProblems(l).map((k) => "⚠ " + PATTERN_SHORT[k])].join(" · ")} |`).join("\n") +
      "\n\n[Open the run report](command:hcv.report)");
    hover.isTrusted = { enabledCommands: ["hcv.report"] };
    labels.push({ range: doc.lineAt(b.start - 1).range, hoverMessage: hover,
      renderOptions: { after: { contentText: text, color: RANK_COLORS[Math.min(i, RANK_COLORS.length - 1)],
        fontWeight: "bold", margin: "0 0 0 2em" } } });
  });

  // memory: the lines whose objects held the most at once, ranked like the speed boxes (#1 = deepest blue)
  const mem = show("memory") ? memoryBlocks(data, file, doc) : [];
  const memLabels = [];
  const runTop = Math.max(1, ...(data.memory || []).map((m) => m.bytes));
  const memRank = (data.memory || []).slice().sort((a, b) => b.bytes - a.bytes);
  const washes = types.memRanks.map(() => []);
  const memBoxes = types.memRanks.map(() => ({ single: [], top: [], mid: [], bot: [] }));
  mem.forEach((mb, i) => {
    const ri = Math.min(i, types.memRanks.length - 1);
    const r = boxRanges(mb.start, mb.end, doc);
    for (const k in r) memBoxes[ri][k].push(...r[k]);
    for (let n = mb.start; n <= mb.end; n++) washes[ri].push(doc.lineAt(Math.min(n - 1, doc.lineCount - 1)).range);
    const overall = memRank.indexOf(mb.items.reduce((a, b) => (b.bytes > a.bytes ? b : a))) + 1;
    const n = mb.end - mb.start + 1;
    const timed = (data.lines || []).filter((l) => norm(l.file) === norm(file) && l.line >= mb.start && l.line <= mb.end);
    const tSec = timed.reduce((s2, l) => s2 + l.seconds, 0);
    const tShare = timed.reduce((s2, l) => s2 + l.share, 0);
    const hover = new vscode.MarkdownString(`**Memory #${i + 1}** in this file - ${n > 1 ? `lines ${mb.start}-${mb.end}` : `line ${mb.start}`}` +
      ` (biggest line #${overall} in the whole run)\n\n` +
      `The objects made here held up to **${fmtBytes(mb.bytes)}** at once ` +
      `(${(100 * mb.bytes / runTop).toFixed(0)}% of the biggest line in the run).` +
      (n > 1 ? "\n\n| line | held |\n|---|---|\n" + mb.items.map((m) => `| ${m.line} | ${fmtBytes(m.bytes)} |`).join("\n") : "") +
      (timed.length ? `\n\nSpeed: ${fmtSeconds(tSec)} · ${(100 * tShare).toFixed(1)}% of the run time` : "") +
      "\n\n_Measured with tracemalloc, checked once a second: short peaks can be missed._" +
      "\n\n[Where in the code (all files)](command:hcv.where) · [Open the run report](command:hcv.report)");
    hover.isTrusted = { enabledCommands: ["hcv.report", "hcv.where"] };
    memLabels.push({ range: doc.lineAt(Math.min(mb.start - 1, doc.lineCount - 1)).range, hoverMessage: hover, renderOptions: { after: {
      contentText: `  📦 #${i + 1} memory · ${fmtBytes(mb.bytes)} held` + (n > 1 ? ` · ${n} lines` : "") +
        (overall !== i + 1 ? ` (#${overall} in the run)` : ""),
      color: MEMORY_RANKS[ri], fontWeight: i === 0 ? "bold" : "normal", margin: "0 0 0 2em" } } });
  });
  types.memRanks.forEach((t, i) => {
    for (const k of ["single", "top", "mid", "bot"]) editor.setDecorations(t[k], memBoxes[i][k]);
    editor.setDecorations(t.wash, washes[i]);
  });
  editor.setDecorations(types.memLabel, memLabels);

  // every other line that took time: a faint "share · time" (orange with ⚠ when a bottleneck held there)
  // every other block that took time: a faint "share · time"; lines under each other get one faint
  // dashed box (orange with ⚠ where a bottleneck held)
  if (show("lines") && cfg("showAllLines") !== false) {
    const minShare = cfg("lineMinShare") || 0.005;
    const faintRanges = { single: [], top: [], mid: [], bot: [] };
    const warnRanges = { single: [], top: [], mid: [], bot: [] };
    const rest = every.filter((b) => !blocks.includes(b) && b.share >= minShare && b.end <= doc.lineCount).slice(0, 200);
    for (const b of rest) {
      const probs = show("bottlenecks") ? blockProblems(b) : [];
      const n = b.end - b.start + 1;
      if (n > 1) {
        const r = boxRanges(b.start, b.end, doc);
        const into = probs.length ? warnRanges : faintRanges;
        for (const k in r) into[k].push(...r[k]);
      }
      const md = n > 1
        ? `**Lines ${b.start}-${b.end}** in \`${b.func}\` · ${fmtSeconds(b.seconds)} · ${(100 * b.share).toFixed(1)}% of the run time\n\n` +
          b.lines.map((l) => lineMarkdown(l, data)).join("\n\n---\n\n")
        : lineMarkdown(b.lines[0], data);
      const hover = new vscode.MarkdownString(md + "\n\n[Open the run report](command:hcv.report)");
      hover.isTrusted = { enabledCommands: ["hcv.report"] };
      labels.push({ range: doc.lineAt(b.start - 1).range, hoverMessage: hover,
        renderOptions: { after: { contentText: `  ${(100 * b.share).toFixed(1)}% · ${fmtSeconds(b.seconds)}` +
          (n > 1 ? ` · ${n} lines` : "") + (probs.length ? ` · ⚠ ${PATTERN_SHORT[probs[0]]}` : ""),
          color: probs.length ? WARN_COLOR : FAINT_COLOR, fontStyle: "italic", margin: "0 0 0 2em" } } });
    }
    for (const k in faintRanges) editor.setDecorations(types.faint[k], faintRanges[k]);
    for (const k in warnRanges) editor.setDecorations(types.faintWarn[k], warnRanges[k]);
  }

  const crash = show("crash") ? data.crash : null;
  if (crash && norm(crash.file) === norm(file)) {
    const r = doc.lineAt(Math.min(crash.line - 1, doc.lineCount - 1)).range;
    editor.setDecorations(types.crash, [r]);
    labels.push({ range: r, hoverMessage: new vscode.MarkdownString(`**The program crashed here**\n\n\`${crash.error}\``),
      renderOptions: { after: { contentText: `  💥 crashed here: ${crash.error.slice(0, 120)}`, color: "#ff4d4d",
        fontWeight: "bold", margin: "0 0 0 2em" } } });
  }
  editor.setDecorations(types.label, labels);
}

function redrawAll() {
  for (const e of vscode.window.visibleTextEditors) decorate(e);
  updateStatus();
  lensChanged.fire();
}

function updateStatus() {
  const e = vscode.window.activeTextEditor;
  if (!statusItem) return;
  statusItem.color = undefined;
  if (proc || viaVisualizer || runTerminal) {
    statusItem.text = runTerminal ? "$(sync~spin) HCV run + profile…" : "$(sync~spin) HCV profiling…";
    statusItem.color = runTerminal ? "#ffd60a" : undefined;
    statusItem.tooltip = "Click to stop";
    statusItem.command = "hcv.stop";
    statusItem.show();
    return;
  }
  const prof = e && e.document.languageId === "python" ? profileShowing(e.document.fileName) : null;
  const top = prof ? hotBlocks(prof.data, e.document.fileName, e.document)[0] : null;
  if (top) {
    statusItem.text = `$(flame) #1: lines ${top.start}-${top.end} · ${(100 * top.share).toFixed(0)}%`;
    statusItem.tooltip = `${runSummary(prof.data)}\nClick: go to a hot spot`;
    statusItem.command = "hcv.hotspots";
    statusItem.show();
  } else {
    statusItem.hide();
  }
}

// ------------------------------------------------------------------ running
function startWatching(file) {
  stopWatching();
  timer = setInterval(() => {
    const prof = readProfile(profileFor(file));
    redrawAll();
    if ((viaVisualizer || runTerminal) && prof && !prof.data.running && prof.data.result) finished(prof.data);
    else if (runTerminal && runTerminal.exitStatus) {  // Python exited without a final profile
      runTerminal = null;
      stopWatchingSoon();
      updateStatus();
    }
  }, 700);
}

function stopWatching() {
  if (timer) clearInterval(timer);
  timer = null;
}

// a last redraw or two after a run ends, then stop - unless a new run has started its own watch
function stopWatchingSoon() {
  const t = timer;
  setTimeout(() => { if (timer === t) stopWatching(); }, 1500);
}

// psutil (hardware readings), py-spy (exact line times), nvidia-ml-py (NVIDIA GPU): optional Python
// packages. One click puts them into the Python that runs the code (pip in a terminal you can watch).
const TOOLS = ["psutil", "py-spy", "nvidia-ml-py", "moderngl"];  // moderngl: the app's 3D and PCIe tests
let toolsOffered = false;

async function installTools(python) {
  const e = vscode.window.activeTextEditor;
  const py = python || await pythonFor(e ? e.document.fileName : (vscode.workspace.workspaceFolders || [{ uri: { fsPath: "." } }])[0].uri.fsPath);
  const term = vscode.window.createTerminal({ name: "HCV: install tools", shellPath: py,
    shellArgs: ["-m", "pip", "install", "--upgrade", ...TOOLS], iconPath: new vscode.ThemeIcon("package") });
  term.show();
  output.appendLine(`[hcv] installing ${TOOLS.join(", ")} into ${py}`);
}

function offerTools(data) {
  if (toolsOffered) return;
  const missing = [];
  if (data.hw_missing) missing.push("psutil (hardware readings)");
  if (data.engine && data.engine !== "py-spy") missing.push("py-spy (exact line times)");
  if (!missing.length) return;
  toolsOffered = true;
  const py = (data.hw_missing || "").match(/\(([^)]+)\)/);
  vscode.window.showInformationMessage(`HCV: for full results install ${missing.join(" and ")} into this Python.`,
    "Install", "Not now").then((pick) => { if (pick === "Install") installTools(py ? py[1] : undefined); });
}

function finished(data) {
  offerTools(data);
  viaVisualizer = false;
  runTerminal = null;  // the terminal stays open with the program's output
  proc = null;
  stopWatchingSoon();
  redrawAll();
  const e = vscode.window.activeTextEditor;
  const top = e ? hotBlocks(data, e.document.fileName, e.document)[0] : null;
  output.appendLine(`[hcv] ${data.result || "done"} - ${fmtSeconds(data.elapsed_s || 0)}`);
  if (data.crash) {
    const needsInput = /^EOFError/.test(data.crash.error) ? " - it reads input(): use the yellow ▶ (runs in a terminal)" : "";
    vscode.window.showErrorMessage(`The program crashed at line ${data.crash.line}: ${data.crash.error}${needsInput}`, "Go to line")
      .then(async (go) => {
        if (!go) return;
        const ed = await vscode.window.showTextDocument(vscode.Uri.file(data.crash.file));
        const pos = new vscode.Position(data.crash.line - 1, 0);
        ed.selection = new vscode.Selection(pos, pos);
        ed.revealRange(new vscode.Range(pos, pos), vscode.TextEditorRevealType.InCenter);
      });
  } else if (top) {
    vscode.window.showInformationMessage(
      `Hot spot #1: lines ${top.start}-${top.end} take ${(100 * top.share).toFixed(0)}% of the run time.`);
  }
}

async function prepare() {
  const e = vscode.window.activeTextEditor;
  if (!e || e.document.languageId !== "python") {
    vscode.window.showWarningMessage("Open a Python file first.");
    return null;
  }
  if (proc || viaVisualizer || runTerminal) {
    vscode.window.showWarningMessage("Already profiling - stop it first.");
    return null;
  }
  if (!vscode.workspace.isTrusted) {
    vscode.window.showWarningMessage("Profiling runs this file's code - trust the folder first (Restricted Mode).");
    return null;
  }
  if (e.document.isUntitled || e.document.isDirty) await e.document.save();
  const folder = visualizerFolder();
  const profiler = profilerPath();
  if (!fs.existsSync(profiler)) {
    vscode.window.showErrorMessage(`The profiler is missing (${profiler}) - reinstall the HCV Hot Spots extension.`);
    return null;
  }
  const file = e.document.fileName;
  const out = profileFor(file);
  try { fs.unlinkSync(out); } catch (err) { /* no old profile */ }
  current = null;
  const python = await pythonFor(file);
  const cwd = runFolder(file);
  const root = projectRoot(file);
  // --root: every file of the project counts as your code (src/, packages, sibling folders)
  const base = [profiler, "--out", out].concat(root ? ["--root", root] : []);
  return { file, out, profiler, folder, python, cwd, root, base };
}

// prepare() waits (saving, asking the Python extension): one start at a time
let starting = false;
async function begin() {
  if (starting) return null;
  starting = true;
  try { return await prepare(); } finally { starting = false; }
}

const quoteArgs = (args) => args.map((a) => `"${a}"`).join(" ");

async function profile(memory) {
  const p = await begin();
  if (!p) return;
  const args = p.base.concat(memory ? ["--memory"] : []).concat([p.file]);
  output.clear();
  output.show(true);
  output.appendLine(`[hcv] (in ${p.cwd}) "${p.python}" ${quoteArgs(args)}`);
  // no terminal here: input() gets end-of-file at once instead of waiting forever (the yellow
  // button runs in a terminal, where input() works)
  proc = cp.spawn(p.python, args, { cwd: p.cwd, stdio: ["ignore", "pipe", "pipe"] });
  proc.stdout.on("data", (d) => output.append(d.toString()));
  proc.stderr.on("data", (d) => output.append(d.toString()));
  proc.on("error", (err) => {
    output.appendLine(`[hcv] can't start Python: ${err.message}`);
    proc = null;
    updateStatus();
  });
  proc.on("exit", () => {
    const prof = readProfile(p.out);
    proc = null;
    if (prof) finished(prof.data);
    else { stopWatching(); updateStatus(); }
  });
  startWatching(p.file);
  updateStatus();
}

// Yellow button: a normal run in a terminal (the program's own output, input() works), with the
// profiler and its memory test around it, so every colour is drawn: time boxes, memory boxes,
// crash line. Python is the terminal's own process, so no shell quoting is involved.
async function runAll() {
  if (viaApp()) return profileInVisualizer(true);
  const p = await begin();
  if (!p) return;
  const args = p.base.concat(["--memory", p.file]);
  output.appendLine(`[hcv] run + profile in terminal (in ${p.cwd}): "${p.python}" ${quoteArgs(args)}`);
  const term = vscode.window.createTerminal({
    name: `HCV Run: ${path.basename(p.file)}`, shellPath: p.python, shellArgs: args,
    cwd: p.cwd, iconPath: new vscode.ThemeIcon("play"),
    color: new vscode.ThemeColor("terminal.ansiYellow"),
  });
  runTerminal = term;
  term.show(true);
  startWatching(p.file);
  updateStatus();
}

// The Hardware Visualizer's AI CONTROL API (control.py): the visualizer runs the profiler as
// "YOUR CODE", so the diagram shows the hardware while the boxes fill in here.
// ------------------------------------------------------------------ the Hardware Visualizer app
// The desktop app publishes its editor link in app.json (port + token) in the user's HCV folder while
// it is open; the extension carries a copy of the app (app/main.py) and starts it when needed.
const os = require("os");

function hcvUserDir() {
  return process.platform === "win32"
    ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "HCV")
    : path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share"), "hcv");
}

const alive = (pid) => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === "EPERM"; } };

// The running app's link, or null.
function appInfo() {
  try {
    const info = JSON.parse(fs.readFileSync(path.join(hcvUserDir(), "app.json"), "utf8"));
    return info.port && info.token && alive(info.pid) ? info : null;
  } catch (e) {
    return null;
  }
}

// Which app to start: a folder set in hcv.visualizerFolder (your own copy of the repo), else the copy
// that ships with the extension.
function appFolder() {
  const vf = visualizerFolder();
  if (vf && fs.existsSync(path.join(vf, "main.py"))) return vf;
  const bundled = path.join(__dirname, "app");
  return fs.existsSync(path.join(bundled, "main.py")) ? bundled : null;
}

function callApp(info, action, body) {
  return new Promise((resolve) => {
    const data = JSON.stringify(body || {});
    const req = http.request({ host: "127.0.0.1", port: info.port, path: "/" + action, method: "POST", timeout: 35000,
      headers: { "X-HCV-Token": info.token, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } },
    (res) => {
      let text = "";
      res.on("data", (c) => (text += c));
      res.on("end", () => {
        try { resolve({ code: res.statusCode, body: JSON.parse(text) }); } catch (e) { resolve({ code: res.statusCode, body: {} }); }
      });
    });
    req.on("error", () => resolve({ code: 0, body: { error: "The Hardware Visualizer isn't answering." } }));
    req.on("timeout", () => req.destroy());
    req.write(data);
    req.end();
  });
}

// The running app (started if needed). Returns its link info, or null after saying why.
let appStarting = null;
async function ensureApp(quiet) {
  let info = appInfo();
  if (info && (await callApp(info, "hello", {})).code === 200) return info;
  if (appStarting) return appStarting;
  appStarting = (async () => {
    const folder = appFolder();
    if (!folder) {
      vscode.window.showErrorMessage("The Hardware Visualizer app wasn't found - reinstall HCV Hot Spots, or set hcv.visualizerFolder.");
      return null;
    }
    const e = vscode.window.activeTextEditor;
    const py = await pythonFor(e ? e.document.fileName : path.join(folder, "main.py"));
    const dir = hcvUserDir();
    fs.mkdirSync(dir, { recursive: true });
    const log = path.join(dir, "app.log");
    const fd = fs.openSync(log, "w");
    let exited = null;
    const child = cp.spawn(py, [path.join(folder, "main.py")], {
      cwd: folder, detached: true, windowsHide: true, stdio: ["ignore", fd, fd],
      env: { ...process.env, HCV_DATA_DIR: folder.startsWith(__dirname) ? dir : (process.env.HCV_DATA_DIR || "") },
    });
    child.on("exit", (code) => { exited = code; });
    child.on("error", (err) => { exited = -1; fs.appendFileSync(log, String(err)); });
    child.unref();
    fs.closeSync(fd);
    output.appendLine(`[hcv] starting the Hardware Visualizer: "${py}" "${path.join(folder, "main.py")}"`);
    if (!quiet) vscode.window.setStatusBarMessage("$(sync~spin) Starting the Hardware Visualizer…", 8000);
    for (let i = 0; i < 120; i++) {  // up to 30 s: it detects the hardware first
      await new Promise((r) => setTimeout(r, 250));
      info = appInfo();
      if (info && info.pid === child.pid && (await callApp(info, "hello", {})).code === 200) return info;
      if (exited !== null) break;
    }
    let tail = "";
    try { tail = fs.readFileSync(log, "utf8").trim().split(/\r?\n/).slice(-3).join(" | "); } catch (err) { /* no log */ }
    const missing = /No module named '([\w.]+)'/.exec(tail);
    if (missing) {
      const pick = await vscode.window.showErrorMessage(`The Hardware Visualizer needs the Python package "${missing[1]}" ` +
        `(in ${py}).`, "Install the tools");
      if (pick) installTools(py);
    } else {
      vscode.window.showErrorMessage(`The Hardware Visualizer didn't start${exited !== null ? ` (exit ${exited})` : ""}: ${tail || "see " + log}`);
    }
    return null;
  })();
  try { return await appStarting; } finally { appStarting = null; }
}

async function openVisualizer() {
  const before = appInfo();
  const info = await ensureApp();
  if (info) vscode.window.showInformationMessage(before ? "The Hardware Visualizer is already open - it's connected." :
    "The Hardware Visualizer is open and connected. Turn on 'Watch in the Hardware Visualizer' (eye menu) to run your code there.");
}

// Runs the file through the app (YOUR CODE): the diagram shows the hardware live while the boxes fill
// in here; when it ends the app adds its sensors and bottlenecks to the profile.
async function profileInVisualizer(memory = true) {
  const info = await ensureApp();
  if (!info) return;
  const p = await begin();
  if (!p) return;
  // the app starts programs from the home folder: --cwd puts it where VS Code would run it
  const command = `"${p.python}" ${quoteArgs(p.base.concat(memory ? ["--memory"] : []).concat(["--cwd", p.cwd, p.file]))}`;
  for (const [action, body] of [["select", { part: "cpu" }], ["program", { command }]]) {
    const r = await callApp(info, action, body);
    if (r.code !== 200) {
      vscode.window.showErrorMessage(`Hardware Visualizer: ${r.body.error || "HTTP " + r.code}`);
      return;
    }
  }
  let r = await callApp(info, "start", {});
  if (r.code !== 200 && r.body.question) {
    const go = await vscode.window.showWarningMessage(r.body.question, { modal: true }, "Continue");
    if (go !== "Continue") return;
    r = await callApp(info, "start", { confirm: true });
  }
  if (r.code !== 200) {
    vscode.window.showErrorMessage(`Hardware Visualizer: ${r.body.error || "HTTP " + r.code}`);
    return;
  }
  viaVisualizer = info;
  output.appendLine(`[hcv] running in the Hardware Visualizer: ${command}`);
  startWatching(p.file);
  updateStatus();
}

const viaApp = () => !!(extContext && extContext.globalState.get("hcv.viaApp"));

async function toggleViaApp() {
  const on = !viaApp();
  if (extContext) await extContext.globalState.update("hcv.viaApp", on);
  await vscode.commands.executeCommand("setContext", "hcv.viaApp", on);
  if (on) ensureApp();
}

async function stop() {
  if (proc) proc.kill();
  if (runTerminal) {
    const term = runTerminal;
    term.sendText("\u0003", false);  // Ctrl+C: the profiler saves the boxes as "stopped"
    setTimeout(() => { if (runTerminal === term) { term.dispose(); runTerminal = null; updateStatus(); } }, 4000);
  }
  if (viaVisualizer) {
    const info = viaVisualizer === true ? appInfo() : viaVisualizer;
    if (info) await callApp(info, "stop", {});
    viaVisualizer = false;
  }
  updateStatus();
}

async function gotoHotspot() {
  const e = vscode.window.activeTextEditor;
  const prof = e ? profileShowing(e.document.fileName) : null;
  const blocks = prof ? hotBlocks(prof.data, e.document.fileName, e.document) : [];
  if (!blocks.length) {
    vscode.window.showInformationMessage("No hot spots for this file yet - run HCV: Profile this file.");
    return;
  }
  const pick = await vscode.window.showQuickPick(blocks.map((b, i) => ({
    label: `#${i + 1}  lines ${b.start}-${b.end}  ${(100 * b.share).toFixed(0)}%`,
    description: `${b.func} · ${fmtSeconds(b.seconds)}`, detail: hardwareNote(b, prof.data), block: b })));
  if (pick) {
    const range = new vscode.Range(pick.block.start - 1, 0, pick.block.end - 1, 0);
    e.selection = new vscode.Selection(range.start, range.start);
    e.revealRange(range, vscode.TextEditorRevealType.InCenter);
  }
}

// ------------------------------------------------------------------ code lenses
// The def a line belongs to (by indentation, so lambdas, comprehensions and generator expressions
// count for the function they are written in); -1 at module level.
function enclosingDef(doc, n) {
  const indent = (t) => t.match(/^\s*/)[0].length;
  let want = indent(doc.lineAt(Math.min(n, doc.lineCount - 1)).text);
  for (let i = Math.min(n, doc.lineCount - 1); i >= 0; i--) {
    const t = doc.lineAt(i).text;
    if (!t.trim() || t.trim().startsWith("#")) continue;
    const m = /^(\s*)(async\s+)?def\s+\w+/.exec(t);
    if (m && (m[1].length < want || i === n)) return i;
    if (indent(t) < want) want = indent(t);
    if (want === 0 && !m) return -1;
  }
  return -1;
}

// Time per def in this document: {def (0-based line), seconds, share, lines}
function defTotals(data, doc) {
  const f = norm(doc.fileName);
  const by = {};
  for (const l of data.lines || []) {
    if (norm(l.file) !== f || l.line > doc.lineCount) continue;
    const d = enclosingDef(doc, l.line - 1);
    if (d < 0) continue;
    const t = by[d] = by[d] || { def: d, seconds: 0, share: 0, lines: [] };
    t.seconds += l.seconds;
    t.share += l.share;
    t.lines.push(l);
  }
  return Object.values(by).sort((a, b) => b.seconds - a.seconds);
}

// Above each function that took time: its total. On line 1 of a file with a profile: the run summary.
const lensChanged = new vscode.EventEmitter();
const lensProvider = {
  onDidChangeCodeLenses: lensChanged.event,
  provideCodeLenses(doc) {
    if (cfg("codeLens") === false || doc.isUntitled || !show("functions")) return [];
    const prof = profileShowing(doc.fileName);
    if (!prof || !prof.files.has(norm(doc.fileName))) return [];
    const data = prof.data;
    const lenses = [new vscode.CodeLens(new vscode.Range(0, 0, 0, 0), {
      title: `$(flame) HCV run: ${runSummary(data)}`, command: "hcv.report", arguments: [prof.path],
      tooltip: "Open the run report: bottlenecks, hardware, every hot line" })];
    // memory held per function (biggest line of each def), when memory is shown
    const memByDef = {};
    if (show("memory")) {
      for (const m of data.memory || []) {
        if (norm(m.file) !== norm(doc.fileName) || m.line > doc.lineCount || m.bytes < 1e5) continue;
        const d = enclosingDef(doc, m.line - 1);
        if (d >= 0) memByDef[d] = (memByDef[d] || 0) + m.bytes;
      }
    }
    const defs = defTotals(data, doc);
    for (const d of Object.keys(memByDef).map(Number)) {
      if (!defs.some((t) => t.def === d)) defs.push({ def: d, seconds: 0, share: 0, lines: [] });
    }
    for (const t of defs) {
      const memText = memByDef[t.def] ? `📦 ${fmtBytes(memByDef[t.def])} held` : "";
      const speedText = show("speed") && t.share >= 0.01
        ? `🔥 ${(100 * t.share).toFixed(0)}% of the run · ${fmtSeconds(t.seconds)} · ${t.lines.length} line${t.lines.length > 1 ? "s" : ""}` : "";
      if (!speedText && !memText) continue;
      const probs = show("bottlenecks") && t.lines.length ? blockProblems(t) : [];
      lenses.push(new vscode.CodeLens(new vscode.Range(t.def, 0, t.def, 0), {
        title: [speedText, memText].filter(Boolean).join(" · ") + (probs.length ? ` · ⚠ ${PATTERN_SHORT[probs[0]]}` : ""),
        command: "hcv.where", tooltip: "Where in the code: the slowest lines and the biggest memory, all files" }));
    }
    return lenses;
  },
};

// ------------------------------------------------------------------ run report
// Everything the run recorded, in one Markdown page next to the profile (.hcv/report.md), with
// every line a link into the code.
function reportMarkdown(prof) {
  const data = prof.data;
  const dir = path.dirname(prof.path);
  const link = (file, line) => {
    const rel = path.relative(dir, file).replace(/\\/g, "/");
    return `[${path.basename(file)}:${line}](<${rel}#L${line}>)`;
  };
  const pct = (v) => `${(100 * v).toFixed(1)}%`;
  const out = [];
  out.push(`# HCV run report: ${path.basename(data.script || "")}`, "");
  out.push(`**${runSummary(data)}**`, "");
  out.push(`- Script: ${link(data.script, 1)}`, `- Project folder: \`${data.root || "?"}\``,
    `- Started: ${data.started_epoch ? new Date(data.started_epoch * 1000).toLocaleString() : "?"}`,
    `- Exit code: ${data.exit_code ?? "?"} · CPU threads on this PC: ${data.cores || "?"}`);
  if (data.hw_missing) out.push(`- ⚠ No hardware readings: ${data.hw_missing}`);
  if (data.crash) out.push(`- 💥 **Crashed** at ${link(data.crash.file, data.crash.line)}: \`${data.crash.error}\``);
  out.push("");

  const findingsBlock = (title, list) => {
    if (!list || !list.length) return;
    out.push(`## ${title}`, "");
    for (const f of list) {
      out.push(`### ${f.problem ? "⚠" : "✓"} ${f.title} - ${pct(f.share)} of the run (${fmtSeconds(f.seconds || 0)})`, "");
      out.push(`${f.advice}`, "");
      if ((f.spans || []).length) out.push(`When: ${f.spans.slice(0, 6).map(([a, b]) => `${a}-${b} s`).join(", ")}`, "");
      if ((f.lines || []).length) out.push("Lines running while it held: " + f.lines.map((l) => `${link(l.file, l.line)} (${fmtSeconds(l.seconds)})`).join(", "), "");
    }
  };
  findingsBlock("Bottlenecks (this run's hardware samples)", data.findings);
  if (data.visualizer) findingsBlock(`Bottlenecks (Hardware Visualizer recording ${data.visualizer.run || ""})`, data.visualizer.findings);
  if (!(data.findings || []).length && !(data.visualizer && data.visualizer.findings.length)) {
    out.push("## Bottlenecks", "", data.findings_error ? `Not analysed: ${data.findings_error}` : "None found - no single pattern held for 10% of the run.", "");
  }

  const avgRows = [];
  const a = data.averages || {};
  const va = (data.visualizer && data.visualizer.averages) || {};
  const row = (name, v, unit, digits = 0) => { if (num(v)) avgRows.push(`| ${name} | ${v.toFixed(digits)} ${unit} |`); };
  row("This program's CPU (100 = one core)", a.proc, "%");
  row("Whole CPU", va.cpu_util ?? a.cpu_util, "%");
  row("Busiest core", va.cpu_core_max ?? a.cpu_core_max, "%");
  row("CPU temperature", va.cpu_temp, "°C");
  row("CPU power", va.cpu_power, "W");
  row("CPU clock", va.cpu_freq, "MHz");
  row("RAM in use", va.ram_util ?? a.ram_util, "%");
  row("GPU", va.gpu_util ?? a.gpu_util, "%");
  row("VRAM", va.gpu_vram ?? a.gpu_vram, "GB", 1);
  row("GPU temperature", va.gpu_temp ?? a.gpu_temp, "°C");
  row("GPU power", va.gpu_power ?? a.gpu_power, "W");
  row("PCIe to GPU", va.pcie_rx ?? a.pcie_rx, "MB/s");
  row("PCIe from GPU", va.pcie_tx ?? a.pcie_tx, "MB/s");
  row("PCIe link busy", va.board_util ?? a.board_util, "%");
  row("Chipset temperature", va.board_temp, "°C");
  row("Disk busy", va.disk_util ?? a.disk_util, "%");
  row("Disk read", va.disk_read ?? a.disk_read, "MB/s", 1);
  row("Disk write", va.disk_write ?? a.disk_write, "MB/s", 1);
  if (avgRows.length) {
    out.push("## Hardware during the run (averages)", "", "| reading | average |", "|---|---|", ...avgRows, "");
    if (data.visualizer) out.push(`_Sensors from the Hardware Visualizer recording (${data.visualizer.machine || ""}, ${data.visualizer.part_name || ""})._`, "");
  }

  const files = {};
  for (const l of data.lines || []) (files[l.file] = files[l.file] || []).push(l);
  out.push("## Time per function", "", "| function | file | time | share |", "|---|---|---|---|");
  const funcs = [];
  for (const f of Object.keys(files)) for (const t of funcTotals(data, f)) funcs.push({ ...t, file: f });
  funcs.sort((x, y) => y.seconds - x.seconds);
  for (const t of funcs.slice(0, 40)) out.push(`| \`${t.func}\` | ${link(t.file, t.first)} | ${fmtSeconds(t.seconds)} | ${pct(t.share)} |`);
  out.push("");

  out.push("## Hot lines", "", "| line | function | time | share | while it ran |", "|---|---|---|---|---|");
  for (const l of (data.lines || []).slice(0, 60)) {
    const extra = [...hwParts(l, data), ...lineProblems(l).map((k) => "⚠ " + PATTERN_SHORT[k])].join(" · ");
    out.push(`| ${link(l.file, l.line)} | \`${l.func}\` | ${fmtSeconds(l.seconds)} | ${pct(l.share)} | ${extra} |`);
  }
  out.push("");
  if ((data.memory || []).length) {
    out.push("## Memory (most held at once, per line)", "", "| line | held |", "|---|---|");
    for (const m of data.memory.slice(0, 30)) out.push(`| ${link(m.file, m.line)} | ${fmtBytes(m.bytes)} |`);
    out.push("");
  }
  out.push(`_Measured with ${ENGINE_TEXT[data.engine] || data.engine}; ${(data.samples || []).length} hardware samples, ` +
    `${(data.timeline || []).length} timeline steps of 0.1 s. Profile: \`${prof.path}\`._`);
  return out.join("\n");
}

async function openReport(profilePath) {
  let prof = profilePath ? readProfile(profilePath) : null;
  if (!prof) {
    const e = vscode.window.activeTextEditor;
    prof = e ? profileShowing(e.document.fileName) : null;
  }
  if (!prof) {
    vscode.window.showInformationMessage("No run for this file yet - press the yellow ▶ first.");
    return;
  }
  const file = path.join(path.dirname(prof.path), "report.md");
  fs.writeFileSync(file, reportMarkdown(prof), "utf8");
  await vscode.commands.executeCommand("markdown.showPreviewToSide", vscode.Uri.file(file));
}

// ------------------------------------------------------------------ activation
function activate(context) {
  extContext = context;
  vscode.commands.executeCommand("setContext", "hcv.viaApp", viaApp());
  for (const k of Object.keys(LAYERS)) vscode.commands.executeCommand("setContext", "hcv.show." + k, show(k));
  output = vscode.window.createOutputChannel("HCV Hot Spots");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  context.subscriptions.push(output, statusItem,
    vscode.commands.registerCommand("hcv.runAll", runAll),
    vscode.commands.registerCommand("hcv.report", openReport),
    vscode.commands.registerCommand("hcv.chooseView", chooseLayers),
    vscode.commands.registerCommand("hcv.installTools", () => installTools()),
    vscode.commands.registerCommand("hcv.openVisualizer", openVisualizer),
    vscode.commands.registerCommand("hcv.toggle.viaApp", toggleViaApp),
    vscode.commands.registerCommand("hcv.where", whereIsIt),
    ...Object.keys(LAYERS).map((k) => vscode.commands.registerCommand(`hcv.toggle.${k}`, async () => {
      await setLayer(k, !show(k));
      redrawAll();
    })),
    ...Object.keys(PRESETS).map((k) => vscode.commands.registerCommand(`hcv.preset.${k}`, () => applyLayers(PRESETS[k].on))),
    vscode.languages.registerCodeLensProvider({ language: "python" }, lensProvider),
    vscode.window.onDidCloseTerminal((t) => {
      if (t !== runTerminal) return;
      runTerminal = null;  // closed before the profile said it was done (killed, or Python missing)
      if (t.exitStatus && t.exitStatus.code) output.appendLine(`[hcv] run terminal closed (exit code ${t.exitStatus.code})`);
      stopWatchingSoon();
      redrawAll();
    }),
    vscode.commands.registerCommand("hcv.profile", () => profile(false)),
    vscode.commands.registerCommand("hcv.profileMemory", () => profile(true)),
    vscode.commands.registerCommand("hcv.profileInVisualizer", profileInVisualizer),
    vscode.commands.registerCommand("hcv.hotspots", gotoHotspot),
    vscode.commands.registerCommand("hcv.stop", stop),
    vscode.commands.registerCommand("hcv.clear", () => {
      for (const e of vscode.window.visibleTextEditors) {
        clear(e);
        const shown = profileShowing(e.document.fileName);
        try { fs.unlinkSync(shown ? shown.path : profileFor(e.document.fileName)); } catch (err) { /* none */ }
      }
      current = null;
      updateStatus();
    }),
    vscode.window.onDidChangeVisibleTextEditors(redrawAll),
    vscode.window.onDidChangeActiveTextEditor(updateStatus),
    vscode.workspace.onDidChangeTextDocument((ev) => {
      // line numbers shift when the file is edited: redraw from the saved profile on save instead
      if (ev.contentChanges.length) for (const e of vscode.window.visibleTextEditors) if (e.document === ev.document) clear(e);
    }),
    vscode.workspace.onDidSaveTextDocument(redrawAll));
  // runs started elsewhere (a terminal, the visualizer) update the profile too: redraw when it changes
  // profiles anywhere in the open folders: a module shows boxes from a run started elsewhere
  const PATTERN = "**/.hcv/profile.json";
  vscode.workspace.findFiles(PATTERN, "**/node_modules/**", 500).then((uris) => {
    for (const u of uris) knownProfiles.add(u.fsPath);
    redrawAll();
  }, () => { /* no folder open */ });
  const watcher = vscode.workspace.createFileSystemWatcher(PATTERN);
  watcher.onDidCreate((u) => knownProfiles.add(u.fsPath));
  watcher.onDidChange((u) => knownProfiles.add(u.fsPath));
  watcher.onDidDelete((u) => { knownProfiles.delete(u.fsPath); delete cache[u.fsPath]; });
  context.subscriptions.push(watcher);
  const seen = {};
  const follow = setInterval(() => {
    for (const e of vscode.window.visibleTextEditors) {
      if (e.document.languageId !== "python" || e.document.isUntitled || e.document.isDirty) continue;
      const p = e.document.fileName;
      const m = profileMtime(p);
      if (seen[p] !== m) {
        seen[p] = m;
        decorate(e);
        updateStatus();
        lensChanged.fire();
      }
    }
  }, 1000);
  context.subscriptions.push({ dispose: () => clearInterval(follow) });
  redrawAll();
}

function deactivate() {
  if (proc) proc.kill();
  if (runTerminal) runTerminal.dispose();
  stopWatching();
}

module.exports = { activate, deactivate };
