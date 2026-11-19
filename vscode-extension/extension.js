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
let types = null;        // decoration types, made once
let current = null;      // {path, data, mtime} - the profile on screen
let proc = null;         // the profiler process we started
let viaVisualizer = false;
let timer = null;
let output, statusItem;

// ------------------------------------------------------------------ helpers
const norm = (p) => path.resolve(p).toLowerCase();

function cfg(key) {
  return vscode.workspace.getConfiguration("hcv").get(key);
}

function visualizerFolder() {
  const set = cfg("visualizerFolder");
  if (set) return set;
  try {
    return fs.readFileSync(path.join(__dirname, "hcv-path.txt"), "utf8").trim();
  } catch (e) {
    return "";
  }
}

function profileFor(file) {
  return path.join(path.dirname(file), ".hcv", "profile.json");
}

function readProfile(p) {
  try {
    const st = fs.statSync(p);
    if (current && current.path === p && current.mtime === st.mtimeMs) return current;
    const data = JSON.parse(fs.readFileSync(p, "utf8"));
    current = { path: p, data, mtime: st.mtimeMs };
    return current;
  } catch (e) {
    return null; // not there yet, or caught mid-write: try again on the next tick
  }
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
    label: vscode.window.createTextEditorDecorationType({}),
  };
}

// ------------------------------------------------------------------ hot spots
// Lines of one file -> blocks: neighbouring lines (at most one line apart) of the same
// function are one block. Biggest first.
function hotBlocks(data, file) {
  const mine = (data.lines || []).filter((l) => norm(l.file) === norm(file) && l.share >= 0.003)
    .sort((a, b) => a.line - b.line);
  const blocks = [];
  for (const l of mine) {
    const b = blocks[blocks.length - 1];
    if (b && l.line - b.end <= 2 && l.func === b.func) {
      b.end = l.line;
      b.lines.push(l);
    } else {
      blocks.push({ start: l.line, end: l.line, func: l.func, lines: [l] });
    }
  }
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
  return blocks.filter((b) => b.share >= cfg("minShare")).sort((a, b) => b.seconds - a.seconds)
    .slice(0, cfg("maxBoxes"));
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

// ------------------------------------------------------------------ drawing
function clear(editor) {
  if (!types) return;
  for (const t of [...types.ranks, types.memory]) for (const k in t) editor.setDecorations(t[k], []);
  editor.setDecorations(types.label, []);
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
  const prof = readProfile(profileFor(file));
  if (!types) types = makeTypes();
  clear(editor);
  if (!prof) return;
  const data = prof.data;
  const doc = editor.document;
  const labels = [];
  const live = data.running ? "LIVE · " : "";

  hotBlocks(data, file).forEach((b, i) => {
    const t = types.ranks[Math.min(i, types.ranks.length - 1)];
    const r = boxRanges(b.start, b.end, doc);
    for (const k of ["single", "top", "mid", "bot"]) editor.setDecorations(t[k], r[k]);
    const n = b.end - b.start + 1;
    const hw = hardwareNote(b, data);
    const text = `  ◀ #${i + 1} · ${live}${(100 * b.share).toFixed(0)}% of the run time · ${fmtSeconds(b.seconds)}` +
      ` · ${n} line${n > 1 ? "s" : ""}${hw ? " · " + hw : ""}`;
    const hover = new vscode.MarkdownString(
      `**Hot spot #${i + 1}** - lines ${b.start}-${b.end} in \`${b.func}\`\n\n` +
      `${(100 * b.share).toFixed(1)}% of the time spent in your code (${fmtSeconds(b.seconds)})` +
      (hw ? `\n\nWhile it ran: ${hw}` : "") + "\n\n| line | time | share |\n|---|---|---|\n" +
      b.lines.map((l) => `| ${l.line} | ${fmtSeconds(l.seconds)} | ${(100 * l.share).toFixed(1)}% |`).join("\n"));
    labels.push({ range: doc.lineAt(b.start - 1).range, hoverMessage: hover,
      renderOptions: { after: { contentText: text, color: RANK_COLORS[Math.min(i, RANK_COLORS.length - 1)],
        fontWeight: "bold", margin: "0 0 0 2em" } } });
  });

  const mem = (data.memory || []).filter((m) => norm(m.file) === norm(file) && m.bytes >= 1e6).slice(0, 3);
  const memRanges = { single: [], top: [], mid: [], bot: [] };
  for (const m of mem) {
    memRanges.single.push(doc.lineAt(Math.min(m.line - 1, doc.lineCount - 1)).range);
    labels.push({ range: doc.lineAt(Math.min(m.line - 1, doc.lineCount - 1)).range,
      hoverMessage: new vscode.MarkdownString(`Memory: the objects made on line ${m.line} held up to ` +
        `**${fmtBytes(m.bytes)}** at once (checked once a second, so short peaks can be missed).`),
      renderOptions: { after: { contentText: `  📦 up to ${fmtBytes(m.bytes)} held`, color: MEMORY_COLOR,
        margin: "0 0 0 2em" } } });
  }
  for (const k in memRanges) editor.setDecorations(types.memory[k], memRanges[k]);
  editor.setDecorations(types.label, labels);
}

function redrawAll() {
  for (const e of vscode.window.visibleTextEditors) decorate(e);
  updateStatus();
}

function updateStatus() {
  const e = vscode.window.activeTextEditor;
  if (!statusItem) return;
  if (proc || viaVisualizer) {
    statusItem.text = "$(sync~spin) HCV profiling…";
    statusItem.tooltip = "Click to stop";
    statusItem.command = "hcv.stop";
    statusItem.show();
    return;
  }
  const prof = e && e.document.languageId === "python" ? readProfile(profileFor(e.document.fileName)) : null;
  const top = prof ? hotBlocks(prof.data, e.document.fileName)[0] : null;
  if (top) {
    statusItem.text = `$(flame) #1: lines ${top.start}-${top.end} · ${(100 * top.share).toFixed(0)}%`;
    statusItem.tooltip = "Go to a hot spot";
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
    if (viaVisualizer && prof && !prof.data.running && prof.data.result) finished(prof.data);
  }, 700);
}

function stopWatching() {
  if (timer) clearInterval(timer);
  timer = null;
}

function finished(data) {
  viaVisualizer = false;
  proc = null;
  setTimeout(stopWatching, 1500);
  redrawAll();
  const e = vscode.window.activeTextEditor;
  const top = e ? hotBlocks(data, e.document.fileName)[0] : null;
  output.appendLine(`[hcv] ${data.result || "done"} - ${fmtSeconds(data.elapsed_s || 0)}`);
  if (top) {
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
  if (proc || viaVisualizer) {
    vscode.window.showWarningMessage("Already profiling - stop it first.");
    return null;
  }
  if (e.document.isUntitled || e.document.isDirty) await e.document.save();
  const folder = visualizerFolder();
  const profiler = path.join(folder, "profiler.py");
  if (!fs.existsSync(profiler)) {
    vscode.window.showErrorMessage(`profiler.py not found in "${folder}". Set hcv.visualizerFolder.`);
    return null;
  }
  const file = e.document.fileName;
  const out = profileFor(file);
  try { fs.unlinkSync(out); } catch (err) { /* no old profile */ }
  current = null;
  return { file, out, profiler, folder };
}

async function profile(memory) {
  const p = await prepare();
  if (!p) return;
  const args = [p.profiler, "--out", p.out].concat(memory ? ["--memory"] : []).concat([p.file]);
  output.clear();
  output.show(true);
  output.appendLine(`[hcv] ${cfg("pythonPath")} ${args.map((a) => `"${a}"`).join(" ")}`);
  proc = cp.spawn(cfg("pythonPath"), args, { cwd: path.dirname(p.file) });
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

// The Hardware Visualizer's AI CONTROL API (control.py): the visualizer runs the profiler as
// "YOUR CODE", so the diagram shows the hardware while the boxes fill in here.
function callVisualizer(folder, action, body) {
  return new Promise((resolve) => {
    let token;
    try {
      token = fs.readFileSync(path.join(folder, "control_token.txt"), "utf8").trim();
    } catch (e) {
      resolve({ code: 0, body: { error: "Open the Hardware Visualizer and press AI CONTROL first." } });
      return;
    }
    const data = JSON.stringify(body || {});
    const req = http.request({ host: "127.0.0.1", port: 8770, path: "/" + action, method: "POST",
      headers: { "X-HCV-Token": token, "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) } },
    (res) => {
      let text = "";
      res.on("data", (c) => (text += c));
      res.on("end", () => {
        try { resolve({ code: res.statusCode, body: JSON.parse(text) }); } catch (e) { resolve({ code: res.statusCode, body: {} }); }
      });
    });
    req.on("error", () => resolve({ code: 0, body: { error: "The Hardware Visualizer isn't answering - is it open with AI CONTROL on?" } }));
    req.write(data);
    req.end();
  });
}

async function profileInVisualizer() {
  const p = await prepare();
  if (!p) return;
  const py = cfg("pythonPath");
  const command = `"${py === "python" ? "python" : py}" "${p.profiler}" --out "${p.out}" "${p.file}"`;
  for (const [action, body] of [["select", { part: "cpu" }], ["program", { command }]]) {
    const r = await callVisualizer(p.folder, action, body);
    if (r.code !== 200) {
      vscode.window.showErrorMessage(`Visualizer: ${r.body.error || "HTTP " + r.code}`);
      return;
    }
  }
  let r = await callVisualizer(p.folder, "start", {});
  if (r.code !== 200 && r.body.question) {
    const go = await vscode.window.showWarningMessage(r.body.question, { modal: true }, "Continue");
    if (go !== "Continue") return;
    r = await callVisualizer(p.folder, "start", { confirm: true });
  }
  if (r.code !== 200) {
    vscode.window.showErrorMessage(`Visualizer: ${r.body.error || "HTTP " + r.code}`);
    return;
  }
  viaVisualizer = true;
  output.appendLine(`[hcv] running in the Hardware Visualizer: ${command}`);
  startWatching(p.file);
  updateStatus();
}

async function stop() {
  if (proc) proc.kill();
  if (viaVisualizer) {
    await callVisualizer(visualizerFolder(), "stop", {});
    viaVisualizer = false;
  }
  updateStatus();
}

async function gotoHotspot() {
  const e = vscode.window.activeTextEditor;
  const prof = e ? readProfile(profileFor(e.document.fileName)) : null;
  const blocks = prof ? hotBlocks(prof.data, e.document.fileName) : [];
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

// ------------------------------------------------------------------ activation
function activate(context) {
  output = vscode.window.createOutputChannel("HCV Hot Spots");
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
  context.subscriptions.push(output, statusItem,
    vscode.commands.registerCommand("hcv.profile", () => profile(false)),
    vscode.commands.registerCommand("hcv.profileMemory", () => profile(true)),
    vscode.commands.registerCommand("hcv.profileInVisualizer", profileInVisualizer),
    vscode.commands.registerCommand("hcv.hotspots", gotoHotspot),
    vscode.commands.registerCommand("hcv.stop", stop),
    vscode.commands.registerCommand("hcv.clear", () => {
      for (const e of vscode.window.visibleTextEditors) {
        clear(e);
        try { fs.unlinkSync(profileFor(e.document.fileName)); } catch (err) { /* none */ }
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
  const seen = {};
  const follow = setInterval(() => {
    for (const e of vscode.window.visibleTextEditors) {
      if (e.document.languageId !== "python" || e.document.isUntitled || e.document.isDirty) continue;
      const p = profileFor(e.document.fileName);
      let m = 0;
      try { m = fs.statSync(p).mtimeMs; } catch (err) { m = 0; }
      if (seen[p] !== m) {
        seen[p] = m;
        decorate(e);
        updateStatus();
      }
    }
  }, 1000);
  context.subscriptions.push({ dispose: () => clearInterval(follow) });
  redrawAll();
}

function deactivate() {
  if (proc) proc.kill();
  stopWatching();
}

module.exports = { activate, deactivate };
