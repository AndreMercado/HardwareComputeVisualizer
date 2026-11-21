// End-to-end test of the HCV Hot Spots extension without the VS Code window: a mock `vscode`
// module, the real extension.js, real Python + profiler.py runs. For every test project it
// presses a button (yellow run / flame), waits for the run to end, opens every .py file of the
// project and checks which boxes are drawn where.
//   node e2e.js [runAll|profile|visualizerless] [project...]
const Module = require("module");
const fs = require("fs");
const path = require("path");
const cp = require("child_process");

const REPO = path.resolve(__dirname, "..");
const EXT = process.env.EXT || path.join(REPO, "vscode-extension", "extension.js");
const PY = process.env.HCV_PYTHON || "python";
const projects = JSON.parse(fs.readFileSync(path.resolve(__dirname, "work", process.env.PROJECTS || "e2e_projects.json"), "utf8"));
const mode = process.argv[2] || "runAll";
const only = process.argv.slice(3);
const norm = (p) => path.resolve(p).toLowerCase();

// ------------------------------------------------------------------ mock vscode
const RANKS = ["#ff4d4d", "#ff9f1a", "#ffd60a", "#3ddc97", "#4dd2ff", "#c77dff"];
const state = {
  context: {}, globalState: {}, quickPick: null,
  statusVisibleSpin: () => !!(state.status && state.status.visible && /spin/.test(state.status.text)),
  workspaceFolders: [], commands: {}, events: {}, messages: [], terminals: [], config: {},
  pythonExtPath: null, trusted: true,
};
const on = (name) => (cb) => { (state.events[name] = state.events[name] || []).push(cb); return { dispose() {} }; };
const fire = (name, arg) => (state.events[name] || []).forEach((cb) => cb(arg));
let typeId = 0;
const killTree = (p) => { try { cp.execSync(`taskkill /T /F /PID ${p.pid}`, { stdio: "ignore" }); } catch (e) { /* gone */ } };
class Range { constructor(l) { this.line = l; } }
function makeEditor(file) {
  const text = fs.readFileSync(file, "utf8").split(/\r?\n/);
  const doc = {
    fileName: path.resolve(file), languageId: "python", isUntitled: false, isDirty: false,
    lineCount: text.length, uri: { fsPath: file },
    lineAt: (n) => ({ text: text[n] || "", range: new Range(n) }), save: async () => true,
  };
  return { document: doc, decos: new Map(), setDecorations(t, r) { this.decos.set(t, r); },
    selection: null, revealRange() {} };
}
const vscode = {
  OverviewRulerLane: { Right: 4, Full: 7 }, StatusBarAlignment: { Left: 1 }, QuickPickItemKind: { Separator: -1, Default: 0 },
  TextEditorRevealType: { InCenter: 2 },
  MarkdownString: class { constructor(v) { this.value = v; } },
  ThemeIcon: class { constructor(id) { this.id = id; } },
  ThemeColor: class { constructor(id) { this.id = id; } },
  Position: class { constructor(l, c) { this.line = l; this.character = c; } },
  Selection: class { constructor(a) { this.anchor = a; } },
  Range: class { constructor(a, b) { this.start = a; this.end = b; } },
  Uri: { file: (p) => ({ fsPath: path.resolve(p) }) },
  EventEmitter: class { constructor() { this.subs = []; this.event = (cb) => { this.subs.push(cb); return { dispose() {} }; }; }
    fire(x) { this.subs.forEach((cb) => cb(x)); } dispose() {} },
  CodeLens: class { constructor(range, command) { this.range = range; this.command = command; } },
  languages: { registerCodeLensProvider: (sel, prov) => { state.lensProvider = prov; return { dispose() {} }; } },
  window: {
    visibleTextEditors: [], activeTextEditor: null,
    createTextEditorDecorationType: (o) => ({ id: ++typeId, opts: o, dispose() {} }),
    createOutputChannel: () => ({ text: "", append(t) { this.text += t; }, appendLine(t) { this.text += t + "\n"; },
      clear() { this.text = ""; }, show() {}, dispose() {} }),
    createStatusBarItem: () => (state.status = { show() { this.visible = true; }, hide() { this.visible = false; }, dispose() {} }),
    showWarningMessage: async (m) => { state.messages.push("WARN " + m); },
    showErrorMessage: async (m) => { state.messages.push("ERROR " + m); },
    showInformationMessage: async (m) => { state.messages.push("INFO " + m); },
    setStatusBarMessage: () => ({ dispose() {} }),
    showQuickPick: async (items) => { state.lastPick = items; return items.find((i) => i.file); },
    showTextDocument: async (uri) => { state.opened = uri.fsPath; return makeEditor(uri.fsPath); },
    createQuickPick: () => {
      const qp = { items: [], selectedItems: [], accept: [], hideCbs: [],
        onDidAccept(cb) { this.accept.push(cb); }, onDidHide(cb) { this.hideCbs.push(cb); },
        show() { state.quickPick = this; }, hide() { this.hideCbs.forEach((cb) => cb()); }, dispose() {} };
      return qp;
    },
    onDidChangeVisibleTextEditors: on("visible"), onDidChangeActiveTextEditor: on("active"),
    onDidCloseTerminal: on("closeTerminal"),
    createTerminal(opts) {  // a real process standing in for the terminal
      const t = { opts, exitStatus: undefined, output: "", shown: false, show() { this.shown = true; },
        sendText(s) { if (s === "\u0003") this.proc.kill(); }, dispose() { this.proc.kill(); } };
      t.proc = cp.spawn(opts.shellPath, opts.shellArgs, { cwd: opts.cwd, env: { ...process.env, PYTHONIOENCODING: "utf-8" } });
      t.proc.stdout.on("data", (d) => (t.output += d));
      t.proc.stderr.on("data", (d) => (t.output += d));
      t.proc.on("error", (e) => { t.output += "SPAWN ERROR " + e.message; t.exitStatus = { code: -1 }; });
      t.proc.on("exit", (code) => { t.exitStatus = { code }; });
      if (state.stdin) t.proc.stdin.write(state.stdin);
      t.proc.stdin.end();
      state.terminals.push(t);
      return t;
    },
  },
  workspace: {
    get isTrusted() { return state.trusted; },
    getConfiguration: (section) => ({ get: (k) => {
      const key = section + "." + k;
      if (key in state.config) return state.config[key];
      return { "hcv.pythonPath": "python", "hcv.maxBoxes": 5, "hcv.minShare": 0.02, "hcv.visualizerFolder": process.env.VISFOLDER ?? REPO }[key];
    } }),
    getWorkspaceFolder: (uri) => state.workspaceFolders.find((w) => norm(uri.fsPath).startsWith(norm(w) + path.sep))
      ? { uri: { fsPath: state.workspaceFolders.find((w) => norm(uri.fsPath).startsWith(norm(w) + path.sep)) } } : undefined,
    findFiles: async () => {
      const found = [];
      const walk = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) {
        const p = path.join(d, e.name);
        if (e.isDirectory()) walk(p); else if (e.name === "profile.json" && path.basename(d) === ".hcv") found.push({ fsPath: p });
      } };
      state.workspaceFolders.forEach(walk);
      return found;
    },
    createFileSystemWatcher: () => ({ onDidCreate: on("fsCreate"), onDidChange: on("fsChange"), onDidDelete: on("fsDelete"), dispose() {} }),
    onDidChangeTextDocument: on("change"), onDidSaveTextDocument: on("save"),
  },
  commands: {
    registerCommand: (n, f) => { state.commands[n] = f; return { dispose() {} }; },
    executeCommand: async (n, ...a) => {
      if (n === "python.interpreterPath") return state.pythonExtPath;
      if (n === "setContext") state.context[a[0]] = a[1];
      if (state.commands[n]) return state.commands[n](...a);
    },
  },
  extensions: { getExtension: (id) => id === "ms-python.python" && state.pythonExtPath ? {
    isActive: true, exports: { environments: {
      getActiveEnvironmentPath: () => ({ path: state.pythonExtPath }),
      resolveEnvironment: async (e) => ({ executable: { uri: { fsPath: e.path } } }) } } } : undefined },
};
const load = Module._load;
Module._load = function (req, ...rest) { return req === "vscode" ? vscode : load.call(this, req, ...rest); };

// ------------------------------------------------------------------ helpers
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const ext = require(EXT);
const ctx = { subscriptions: [], globalState: {
  get: (k) => state.globalState[k], update: async (k, v) => { state.globalState[k] = v; } } };
ext.activate(ctx);

function boxesIn(editor) {
  // labels: contentText of the label decoration type; ranks: which rank colours have ranges
  const out = { labels: [], memory: 0, crash: null };
  for (const [t, ranges] of editor.decos) {
    if (!ranges.length) continue;
    for (const r of ranges) {
      const a = r.renderOptions && r.renderOptions.after;
      if (a) out.labels.push({ line: r.range.line + 1, text: a.contentText, hover: r.hoverMessage ? r.hoverMessage.value : "" });
    }
  }
  for (const l of out.labels) {
    if (l.text.includes("📦")) out.memory++;
    if (l.text.includes("💥")) out.crash = l.line;
  }
  return out;
}

async function waitDone(entry, timeoutMs) {
  const prof = path.join(path.dirname(entry), ".hcv", "profile.json");
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    await sleep(300);
    try {
      const d = JSON.parse(fs.readFileSync(prof, "utf8"));
      if (!d.running && d.result) { await sleep(1800); return d; }  // the extension's watcher notices
    } catch (e) { /* not yet */ }
  }
  return null;
}

// the run-summary lens of the entry file mentions why hardware readings are missing
function sum0(data) {
  const ed = makeEditor(data.script);
  return state.lensProvider.provideCodeLenses(ed.document).some((l) => /no hardware readings/.test(l.command.title));
}

async function runProject(name, p) {
  state.workspaceFolders = [p.ws];
  state.stdin = p.stdin || "";
  state.messages = [];
  const entryEd = makeEditor(p.entry);
  vscode.window.activeTextEditor = entryEd;
  vscode.window.visibleTextEditors = [entryEd];
  const nTerm = state.terminals.length;
  if (process.env.DBG) console.log('DBG before', name, JSON.stringify(state.status), state.terminals.map((t) => t.exitStatus));
  await state.commands[mode === "profile" ? "hcv.profile" : "hcv.runAll"]();
  const fails = [];
  if (state.messages.some((m) => m.startsWith("ERROR") || m.startsWith("WARN"))) return [`not started: ${state.messages}`];
  let data = await waitDone(p.entry, mode === "viaApp" ? 150000 : 120000);
  if (mode === "viaApp" && data) {  // the app adds its recording when its run ends
    for (let i = 0; i < 60 && data && !data.visualizer; i++) {
      await sleep(500);
      try { data = JSON.parse(fs.readFileSync(path.join(path.dirname(p.entry), ".hcv", "profile.json"), "utf8")); } catch (e) { /* mid-write */ }
    }
    if (!data.visualizer) fails.push("the app didn't link its recording into the profile");
    else {
      await sleep(1500);
      const info = JSON.parse(fs.readFileSync(path.join(process.env.LOCALAPPDATA, "HCV", "app.json"), "utf8"));
      const sum = await new Promise((res) => {
        const req = require("http").request({ host: "127.0.0.1", port: info.port, path: "/summary", method: "POST",
          headers: { "X-HCV-Token": info.token, "Content-Type": "application/json", "Content-Length": 2 } }, (r) => {
          let t = ""; r.on("data", (c) => (t += c)); r.on("end", () => res(t)); });
        req.on("error", (e) => res("ERR " + e.message)); req.write("{}"); req.end();
      });
      if (!/Where in your code/.test(sum)) fails.push("app summary has no code links: " + sum.slice(-300));
      const runs = fs.readdirSync(path.join(info.data, "runs"));
      if (!runs.length) fails.push("no run saved in the app's data folder");
      state.appInfo = info;
    }
  }
  if (!data) {
    const t = state.terminals[nTerm];
    return [`no finished profile; terminal: ${t ? t.output.slice(-500) : "none"}; msgs ${state.messages}`];
  }
  if (mode === "runAll") {
    const t = state.terminals[nTerm];
    if (!t) fails.push("no terminal made");
    else {
      if (norm(t.opts.cwd) !== norm(p.ws)) fails.push(`terminal cwd ${t.opts.cwd}, want workspace ${p.ws}`);
      if (!t.opts.shellArgs.includes("--root") || !t.opts.shellArgs.includes("--memory")) fails.push(`args ${t.opts.shellArgs}`);
      if (state.pythonExtPath && norm(t.opts.shellPath) !== norm(state.pythonExtPath)) fails.push(`python ${t.opts.shellPath}`);
    }
  }
  if (p.link_vis) {  // what the visualizer does when its YOUR CODE run of the profiler ends
    const prof = path.join(path.dirname(p.entry), ".hcv", "profile.json");
    const script = `import json,sys; sys.path.insert(0, ${JSON.stringify(REPO)}); import profiler
d = json.load(open(sys.argv[1], encoding="utf-8"))
n = int(d["elapsed_s"] / 0.05)
samples = [{"t": i * 0.05, "cpu_util": 6.0, "cpu_core_max": 95.0, "cpu_cores": [0] * 20, "cpu_temp": 71.5, "cpu_power": 48.0,
            "gpu_util": 2, "gpu_temp": 44, "gpu_power": 30.0, "ram_util": 40.0, "disk_util": 1.0, "board_util": 0.2} for i in range(n)]
run = {"started_epoch": d["started_epoch"], "samples": samples, "threads": 20, "machine": "TestPC", "part_name": "CPU",
       "averages": {"cpu_temp": 71.5, "cpu_power": 48.0, "cpu_util": 6.0}}
print(profiler.link_visualizer_run(sys.argv[1], run, "run-test.json"))`;
    const r = cp.spawnSync(PY, ["-c", script, prof], { encoding: "utf8" });
    if (!/True/.test(r.stdout)) fails.push("visualizer link failed: " + r.stdout + r.stderr);
    await sleep(1500);
  }
  // open every .py file of the project (all at once, like split editors)
  const eds = p.py.map(makeEditor);
  vscode.window.visibleTextEditors = eds;
  vscode.window.activeTextEditor = eds[0];
  fire("visible", eds);
  const by = {};
  for (const e of eds) by[path.relative(p.ws, e.document.fileName).replace(/\\/g, "/")] = boxesIn(e);
  const share = (labels) => Math.max(0, ...labels.filter((l) => l.text.includes("◀ #")).map((l) => +(/(\d+)% of the run/.exec(l.text) || [0, 0])[1]));
  if (mode === "profile" && p.stdin) {
    if (!(data.crash && /EOFError/.test(data.crash.error))) fails.push(`input() without terminal: ${data.result}`);
    if (!state.messages.some((m) => m.includes("yellow"))) fails.push(`no hint to the yellow button: ${state.messages}`);
    return fails.length ? fails : `EOFError box + hint: ${state.messages.find((m) => m.includes("yellow"))}`;
  }
  const hot = p.hot || (data.engine === "py-spy" ? p.hot_pyspy : null);
  if (hot && !(by[hot[0]] && share(by[hot[0]].labels) >= (p.faint || p.spread ? 10 : 40))) fails.push(`no big box in ${hot[0]}: ${JSON.stringify(by)}`);
  if (p.memory && mode === "runAll" && !(by[p.memory] && by[p.memory].memory)) fails.push(`no memory box in ${p.memory}`);
  if (p.crash && !(by[p.crash[0]] && by[p.crash[0]].crash === p.crash[1])) fails.push(`crash box not at ${p.crash}: ${JSON.stringify(by)}`);
  for (const bad of p.not_files || []) for (const f in by) if (f.startsWith(bad) && by[f].labels.length) fails.push(`boxes in ${f}`);
  // --- everything else the run recorded, shown in the files
  const extra = [];
  if (data.hw_missing) { if (!sum0(data)) fails.push("hw_missing not in the summary lens"); else extra.push("hw-missing-hint✓"); }
  else if ((data.elapsed_s || 0) > 1 && (!data.samples || data.samples.length < 1)) fails.push(`no hardware samples (${(data.samples || []).length})`);
  if ((data.elapsed_s || 0) > 1 && !(data.timeline || []).length) fails.push("no timeline");
  if (data.findings_error) fails.push("findings error " + data.findings_error);
  if (hot) {
    const ed = eds.find((e) => norm(e.document.fileName) === norm(path.join(p.ws, hot[0])));
    const lenses = ed ? state.lensProvider.provideCodeLenses(ed.document) : [];
    const sum = lenses.find((l) => l.command.title.includes("HCV run:"));
    const fn = lenses.filter((l) => l.command.title.startsWith("🔥"));
    if (!sum) fails.push("no run-summary lens");
    if (!fn.length && hot[1] !== "<module>") fails.push(`no function lens in ${hot[0]}: ${lenses.map((l) => l.command.title)}`);
    else if (fn.length) extra.push(`lens@${fn[0].range.start ?? fn[0].range.line}`);
    const big = (by[hot[0]] || { labels: [] }).labels.find((l) => l.text.includes("◀ #1"));
    if (big && !/While it ran|while it ran/.test(big.hover)) fails.push("hover has no hardware");
    if (big && !data.hw_missing && !/cores busy/.test(big.hover)) fails.push(`hover has no per-line cores: ${big.hover.slice(-300)}`);
    vscode.window.activeTextEditor = ed;
    await state.commands["hcv.report"]();
    const rp = path.join(path.dirname(p.entry), ".hcv", "report.md");
    const rep = fs.existsSync(rp) ? fs.readFileSync(rp, "utf8") : "";
    const rel = path.relative(path.dirname(rp), path.join(p.ws, hot[0])).replace(/\\/g, "/");
    for (const sec of ["## Hot lines", "## Time per function", ...(data.hw_missing ? ["No hardware readings"] : ["## Hardware during the run"])]) if (!rep.includes(sec)) fails.push(`report lacks ${sec}`);
    if (!rep.includes(`(<${rel}#L`)) fails.push(`report has no link to ${rel}`);
    else extra.push("report✓");
  }
  if (p.link_vis) {
    const hl = (by[p.hot[0]] || { labels: [] }).labels.find((l) => l.text.includes("◀ #1"));
    if (!hl || !/CPU 72 °C/.test(hl.hover) || !/CPU 48 W/.test(hl.hover)) fails.push(`no visualizer sensors in hover: ${hl && hl.hover.slice(-400)}`);
    const rep = fs.readFileSync(path.join(path.dirname(p.entry), ".hcv", "report.md"), "utf8");
    if (!rep.includes("CPU temperature | 72 °C") || !rep.includes("Hardware Visualizer recording")) fails.push("report lacks visualizer sensors/findings");
    else extra.push("vis✓");
  }
  if (p.memory && mode === "runAll") {
    const memRel = p.memory;
    const labelsNow = () => {
      fire("visible", eds);
      const o = {};
      for (const e of eds) o[path.relative(p.ws, e.document.fileName).replace(/\\/g, "/")] = boxesIn(e);
      return o;
    };
    // 1. preset "memory only": no speed boxes, ranked memory highlight, memory in the lens
    await state.commands["hcv.preset.memory"]();
    let o = labelsNow();
    const speedLeft = Object.values(o).some((b) => b.labels.some((l) => l.text.includes("◀ #")));
    const memTop = (o[memRel] || { labels: [] }).labels.find((l) => l.text.includes("📦 #1 memory"));
    if (speedLeft) fails.push("memory-only still shows speed boxes");
    if (!memTop) fails.push(`memory-only: no '📦 #1 memory' in ${memRel}: ${JSON.stringify(o[memRel])}`);
    if (state.context["hcv.show.speed"] !== false || state.context["hcv.show.memory"] !== true) fails.push(`context keys ${JSON.stringify(state.context)}`);
    const memEd = eds.find((e) => norm(e.document.fileName) === norm(path.join(p.ws, memRel)));
    const ml = state.lensProvider.provideCodeLenses(memEd.document).map((l) => l.command.title);
    if (!ml.some((t) => t.includes("📦") && t.includes("held"))) fails.push(`no memory lens: ${ml}`);
    // 2. toggle speed back on from the menu: both
    await state.commands["hcv.toggle.speed"]();
    o = labelsNow();
    if (!Object.values(o).some((b) => b.labels.some((l) => l.text.includes("◀ #")))) fails.push("speed toggle didn't bring boxes back");
    // 3. the picker: untick memory, keep speed
    const pr = state.commands["hcv.chooseView"]();
    const qp = state.quickPick;
    qp.selectedItems = qp.items.filter((i) => i.layer === "speed" || i.layer === "lines");
    for (const cb of qp.accept) await cb();
    await pr;
    o = labelsNow();
    if (Object.values(o).some((b) => b.labels.some((l) => l.text.includes("📦")))) fails.push("picker: memory still shown");
    if (state.context["hcv.show.hardware"] !== false) fails.push("picker: hardware not switched off");
    const big2 = Object.values(o).flatMap((b) => b.labels).find((l) => l.text.includes("◀ #1"));
    if (big2 && /cores busy/.test(big2.text)) fails.push("hardware off, but label still has cores");
    // 4. where is it: speed + memory entries across files, jumps to the file
    await state.commands["hcv.preset.everything"]();
    await state.commands["hcv.where"]();
    const lp = state.lastPick || [];
    if (!lp.some((i) => /\$\(flame\)/.test(i.label || "")) || !lp.some((i) => /\$\(database\)/.test(i.label || ""))) fails.push(`where-list lacks speed or memory: ${lp.map((i) => i.label)}`);
    if (!state.opened) fails.push("where-list didn't open a file");
    labelsNow();
    if (!fails.length) extra.push("layers✓ where✓");
  }
  if (p.adjacent) {
    const ed = eds[0];
    const ranges = (color, width) => [...ed.decos].filter(([t]) => t.opts && t.opts.borderColor === color && t.opts.borderWidth === width).flatMap(([, r]) => r);
    // the speed box (any rank) that holds the loop's lines 6-7
    const loopColor = RANKS.find((c) => ranges(c, "2px 2px 0 2px").some((r) => r.line + 1 <= 6) || ranges(c, "2px").some((r) => r.line + 1 === 6 || r.line + 1 === 7)) || RANKS[0];
    const top = ranges(loopColor, "2px 2px 0 2px").filter((r) => r.line + 1 <= 7), bot = ranges(loopColor, "0 2px 2px 2px").filter((r) => r.line + 1 <= 12),
      single = ranges(loopColor, "2px").filter((r) => r.line + 1 <= 12);
    const b = by["work.py"].labels;
    const speedBoxes = b.filter((l) => l.text.includes("◀ #"));
    if (!(top.length === 1 && bot.length === 1 && single.length === 0)) fails.push(`#1 box not one multi-line box: top ${top.map((r) => r.line + 1)} bot ${bot.map((r) => r.line + 1)} single ${single.map((r) => r.line + 1)}`);
    else if (!(top[0].line + 1 <= 6 && bot[0].line + 1 >= 7)) fails.push(`#1 box ${top[0].line + 1}-${bot[0].line + 1} does not cover genexpr+loop 6-7`);
    const memL = mode !== "runAll" ? [{ text: "3 lines" }] : b.filter((l) => l.text.includes("📦"));
    if (memL.length !== 1 || !/3 lines|4 lines/.test(memL[0].text)) fails.push(`memory not one block: ${memL.map((l) => l.line + ":" + l.text)}`);
    const mtop = ranges("#4da6ff", "2px 2px 0 2px"), mbot = ranges("#4da6ff", "0 2px 2px 2px");
    if (mode === "runAll" && !(mtop.length === 1 && mbot.length === 1 && mtop[0].line + 1 === 8 && mbot[0].line + 1 === 11)) fails.push(`memory box not 8-11: ${mtop.map((r) => r.line + 1)}-${mbot.map((r) => r.line + 1)}`);
    if (!fails.length) extra.push(`one-box✓ speed ${top[0].line + 1}-${bot[0].line + 1} memory ${mtop.map((r) => r.line + 1)}-${mbot.map((r) => r.line + 1)} labels ${speedBoxes.map((l) => l.line)}`);
  }
  const faint = Object.values(by).reduce((n, b) => n + b.labels.filter((l) => !l.text.includes("◀") && /% · /.test(l.text)).length, 0);
  const warns = Object.values(by).reduce((n, b) => n + b.labels.filter((l) => l.text.includes("⚠")).length, 0);
  if (p.gpu && !data.gpu_used) fails.push("GPU work not detected (gpu_used false)");
  if (!p.gpu && data.gpu_used) fails.push("CPU-only program marked as using the GPU");
  if (!p.gpu && Object.values(by).some((b) => b.labels.some((l) => /GPU waiting|PCIe transfers/.test(l.text)))) fails.push("GPU bottleneck shown for CPU-only code");
  if (p.faint && faint < 3) fails.push(`only ${faint} faint line marks`);
  extra.push(`faint=${faint} ⚠=${warns} findings=${(data.findings || []).map((f) => f.key).join("/") || "-"}`);
  const summary = Object.entries(by).filter(([, b]) => b.labels.length)
    .map(([f, b]) => `${f}:${b.labels.filter((l) => !/^  \d/.test(l.text)).map((l) => (l.text.match(/#\d+|📦|💥/) || ["?"])[0] + "@" + l.line).join(",")}`).join("  ") + "  " + extra.join(" ");
  // the extension must have cleared its "running" state
  await sleep(200);
  try { await state.commands["hcv.runAll"].call(null); } catch (e) { /* */ }
  // ^ a second press must start again (not "Already profiling"); stop it at once
  if (state.messages.some((m) => m.includes("Already profiling"))) fails.push("still 'Already profiling' after the run ended");
  await state.commands["hcv.stop"]();
  await sleep(500);
  await sleep(1500);
  return fails.length ? fails : summary;
}

(async () => {
  state.pythonExtPath = process.env.VENV_PY || PY;
  if (mode === "viaApp") await state.commands["hcv.toggle.viaApp"]();
  let allOk = true;
  for (const [name, p] of Object.entries(projects)) {
    if (only.length && !only.includes(name)) continue;
    const t0 = Date.now();
    let r;
    try { r = await runProject(name, p); } catch (e) { r = ["EXCEPTION " + e.stack]; }
    const ok = !Array.isArray(r);
    allOk = allOk && ok;
    console.log(`${ok ? "PASS" : "FAIL"} ${name.padEnd(18)} ${((Date.now() - t0) / 1000).toFixed(1)}s  ${ok ? r : r.join("; ")}`);
  }
  console.log(allOk ? "ALL PASS" : "SOME FAILED");
  ext.deactivate();
  process.exit(allOk ? 0 : 1);
})();
