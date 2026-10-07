/* eslint-disable no-console */
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const Module = require('module');

const project = path.resolve(__dirname, '..');
const extensionDir = path.join(project, 'extension');
const workDir = path.join(path.dirname(project), '..', 'work', 'frontend-fixture');
const resultPath = path.join(project, 'docs', 'frontend-validation.json');
fs.rmSync(workDir, { recursive: true, force: true });
fs.mkdirSync(workDir, { recursive: true });
const rootA = path.join(workDir, 'root-a');
const rootB = path.join(workDir, 'root-b');
fs.mkdirSync(rootA, { recursive: true });
fs.mkdirSync(rootB, { recursive: true });
fs.writeFileSync(path.join(rootA, 'scheduler.py'), [
  'class Scheduler:',
  '    """Schedules work."""',
  '    def run(self, item: str) -> str:',
  '        return item',
  '',
  'def make_scheduler() -> Scheduler:',
  '    return Scheduler()',
  '',
].join('\n'));
fs.writeFileSync(path.join(rootA, 'caller.py'), [
  'from scheduler import Scheduler',
  '',
  'def call(scheduler: Scheduler):',
  '    return scheduler.run("ok")',
  '',
].join('\n'));
fs.writeFileSync(path.join(rootB, 'other.py'), 'class Other:\n    pass\n');
let classFileForRuntime = '';

class EventEmitter {
  constructor() { this.listeners = new Set(); }
  event(listener) { this.listeners.add(listener); return { dispose: () => this.listeners.delete(listener) }; }
  fire(value) { for (const listener of this.listeners) listener(value); }
  dispose() { this.listeners.clear(); }
}
class Uri {
  constructor(fsPath) { this.fsPath = fsPath; this.scheme = 'file'; }
  static file(value) { return new Uri(path.resolve(value)); }
  static joinPath(base, ...parts) { return new Uri(path.join(base.fsPath, ...parts)); }
}
class WebviewMock {
  constructor() { this.cspSource = 'vscode-resource://mock'; this.messages = []; this.listeners = []; this.html = ''; }
  asWebviewUri(uri) { return uri; }
  onDidReceiveMessage(listener) { this.listeners.push(listener); return { dispose() {} }; }
  async receive(message) { for (const listener of this.listeners) await listener(message); }
  async postMessage(message) { this.messages.push(message); return true; }
}
class PanelMock {
  constructor() { this.webview = new WebviewMock(); this.title = ''; this.disposed = []; }
  reveal() {}
  onDidDispose(listener) { this.disposed.push(listener); return { dispose() {} }; }
}
class TreeItem { constructor(label, collapsibleState) { this.label = label; this.collapsibleState = collapsibleState; } }
class ThemeIcon { constructor(id) { this.id = id; } }
const panels = [];
const vscodeMock = {
  Uri,
  EventEmitter,
  TreeItem,
  ThemeIcon,
  TreeItemCollapsibleState: { None: 0, Collapsed: 1 },
  ViewColumn: { Beside: 2, One: 1 },
  ProgressLocation: { Notification: 15 },
  workspace: {
    isTrusted: true,
    workspaceFolders: [{ uri: Uri.file(rootA), name: 'root-a' }],
    getConfiguration: () => ({ get: (_key, fallback) => fallback }),
    fs: { createDirectory: async () => {} },
    openTextDocument: async uri => ({ uri, lineCount: 10, lineAt: () => ({ text: '' }) }),
  },
  window: {
    createWebviewPanel: () => { const panel = new PanelMock(); panels.push(panel); return panel; },
    showTextDocument: async document => ({ document, selection: null, revealRange() {} }),
    showInformationMessage: async () => undefined,
    showErrorMessage: async message => { throw new Error(message); },
  },
  env: { clipboard: { writeText: async () => {} } },
};
const originalLoad = Module._load;
Module._load = function patchedLoad(request, parent, isMain) {
  if (request === 'vscode') return vscodeMock;
  return originalLoad.call(this, request, parent, isMain);
};

function contextFor(cache) {
  return { extensionPath: extensionDir, globalStorageUri: Uri.file(cache), subscriptions: [] };
}
function assertInlineScriptsCompile(html, label) {
  const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/gi)].map(match => match[1]);
  assert(scripts.length > 0, `${label} has no inline script`);
  for (const script of scripts) new vm.Script(script, { filename: `${label}.inline.js` });
  return scripts.length;
}

function executeGraphWebview(html) {
  const elements = new Map(['selected', 'depth', 'back', 'open', 'focus', 'overviewBody', 'status', 'cy', 'unresolvedBody'].map(id => [id, { id, textContent: '', innerHTML: '', disabled: false, value: '1', onclick: null, onchange: null }]));
  const listeners = {};
  const posted = [];
  let cytoscapeInstance;
  const document = { getElementById: id => elements.get(id) || { id, textContent: '', innerHTML: '', disabled: false }, querySelectorAll: () => [] };
  const window = { addEventListener: (type, listener) => { listeners[type] = listener; }, cytoscape: options => {
    const nodeIds = new Set(options.elements.nodes.map(node => node.data.id));
    const instance = { on: (_event, _selector, callback) => { instance.nodeTap = callback; }, destroy: () => {}, nodes: () => ({ unselect: () => {} }), $id: id => ({ length: nodeIds.has(id) ? 1 : 0, select: () => {} }) };
    cytoscapeInstance = instance;
    return instance;
  } };
  const vscode = { postMessage: message => { posted.push(message); } };
  const sandbox = { document, window, getComputedStyle: () => ({ getPropertyValue: name => name.includes('description') ? '#888' : '#eee' }), acquireVsCodeApi: () => vscode, console, setTimeout, clearTimeout };
  const script = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/gi)].map(match => match[1]).find(value => value.includes('const vscode=acquireVsCodeApi'));
  assert(script, 'graph inline script should be present');
  vm.runInNewContext(script, sandbox, { filename: 'graph-runtime.inline.js' });
  const graph = { nodes: [{ id: 'class-a', name: 'Selected', kind: 'class', file: classFileForRuntime, details: {} }, { id: 'class-b', name: 'Caller', kind: 'class', file: classFileForRuntime, details: {} }], edges: [{ sourceId: 'class-b', targetId: 'class-a', type: 'calls', label: 'run', status: 'resolved' }], truncated: false };
  listeners.message({ data: { command: 'graph', graph, focusId: 'class-a' } });
  assert(posted.some(message => message.command === 'inspect' && message.id === 'class-a'), 'initial selected class should request overview by id');
  assert.strictEqual(elements.get('selected').textContent, 'Selected', 'graph selection should use node label');
  assert(cytoscapeInstance && cytoscapeInstance.nodeTap, 'cytoscape node handler should be registered');
  cytoscapeInstance.nodeTap({ target: { id: () => 'class-b' } });
  assert(posted.some(message => message.command === 'inspect' && message.id === 'class-b'), 'selecting another node should request its overview');
  return posted;
}

async function run() {
  const { EngineClient } = require(path.join(extensionDir, 'dist', 'engineClient.js'));
  const { InspectorPanel, GraphViewProvider } = require(path.join(extensionDir, 'dist', 'webviews.js'));
  const { CurrentStructureProvider } = require(path.join(extensionDir, 'dist', 'treeViews.js'));
  const { LandingProvider } = require(path.join(extensionDir, 'dist', 'landing.js'));
  const cache = path.join(workDir, 'cache');
  fs.mkdirSync(cache, { recursive: true });
  const output = { append() {}, appendLine() {} };
  const client = new EngineClient(contextFor(cache), output);
  const real = { protocol: {}, cancellation: {}, persistence: {} };
  const indexed = await client.index(rootA, []);
  assert(indexed.symbols >= 3, 'fixture should produce multiple symbols');
  real.protocol.index = { files: indexed.files, symbols: indexed.symbols };
  const scheduler = (await client.request('symbols', { query: 'Scheduler', limit: 20 })).find(symbol => symbol.kind === 'class');
  assert(scheduler, 'Scheduler query should return a class');
  const inspected = await client.request('inspect', { id: scheduler.id });
  assert.strictEqual(inspected.symbol.id, scheduler.id);
  const graph = await client.request('graph', { id: scheduler.id, types: ['owns', 'calls'], direction: 'both', depth: 1 });
  assert(graph.nodes.some(node => node.id === scheduler.id), 'graph must include focus node');
  real.protocol.queryInspectGraph = { query: true, inspect: true, graph: true };

  const second = await client.index(rootB, []);
  assert.strictEqual(path.resolve(second.root), path.resolve(rootB));
  const secondSummary = await client.request('summary', {});
  assert.strictEqual(path.resolve(secondSummary.root), path.resolve(rootB));
  const dbs = fs.readdirSync(cache).filter(name => name.endsWith('.db'));
  assert(dbs.length >= 2, 'each indexed root should have its own database');
  real.persistence = { rootSwitch: true, databaseCount: dbs.length };

  const cancelClient = new EngineClient(contextFor(path.join(workDir, 'cancel-cache')), output);
  const token = { onCancellationRequested(listener) { this.listener = listener; return { dispose() {} }; } };
  const pending = cancelClient.index(rootA, [], token);
  setTimeout(() => token.listener?.(), 1);
  let cancelled = false;
  try { await pending; } catch (error) { cancelled = /cancel/i.test(String(error)); }
  assert(cancelled, 'cancelled index should reject promptly');
  cancelClient.dispose();
  real.cancellation = { requestRejected: true, processKilled: true };
  client.dispose();

  const inspectorCalls = [];
  let sourceOpens = 0;
  const inspectClient = {
    getRoot: () => rootA,
    request: async (method, params) => {
      if (method === 'inspect') return inspected;
      if (method === 'explainContext') return { context: '{"symbol":"Scheduler"}' };
      throw new Error(`unexpected inspector method ${method}`);
    },
  };
  const inspector = new InspectorPanel(Uri.file(extensionDir), inspectClient, async () => { sourceOpens += 1; }, output, async (command, id) => { inspectorCalls.push({ command, id }); });
  await inspector.show(scheduler.id);
  const panel = panels.at(-1);
  const inspectorScriptCount = assertInlineScriptsCompile(panel.webview.html, 'inspector');
  assert(panel.webview.html.includes('查看源码'), 'inspector should expose Chinese source action');
  assert(panel.webview.html.includes('var(--vscode-editor-background)'), 'inspector should use VS Code theme variables');
  await panel.webview.receive({ command: 'inspect', id: scheduler.id });
  assert.strictEqual(sourceOpens, 0, 'Inspect must not open source');
  await panel.webview.receive({ command: 'openSource', id: scheduler.id });
  assert.strictEqual(sourceOpens, 1, 'Open Source should invoke source callback');
  await panel.webview.receive({ command: 'calls', id: scheduler.id });
  assert.deepStrictEqual(inspectorCalls.at(-1), { command: 'architectureExplorer.showCalls', id: scheduler.id });
  const mockGraphClientCalls = [];
  const classId = scheduler.id;
  const classFile = path.join(rootA, 'scheduler.py');
  classFileForRuntime = classFile;
  const classInspect = {
    symbol: { ...scheduler, file: classFile, kind: 'class', name: 'Scheduler', qualifiedName: 'scheduler.Scheduler' },
    children: [
      { id: 'member-items', kind: 'member', name: 'items', file: classFile, qualifiedName: 'scheduler.Scheduler.items', details: { inferredType: 'list[str]' } },
      { id: 'method-run', kind: 'method', name: 'run', file: classFile, qualifiedName: 'scheduler.Scheduler.run', details: { parameters: [{ name: 'item', annotation: 'str' }], returnAnnotation: 'str' } },
    ], outgoing: [], incoming: [{ sourceId: 'caller.call', targetId: classId, type: 'calls', status: 'resolved', label: 'run' }], related: [],
  };
  const graphClient = {
    getRoot: () => rootA,
    request: async (method, params) => {
      mockGraphClientCalls.push({ method, params });
      if (method === 'graph') return { nodes: [{ id: params.id, kind: 'class', name: 'Scheduler', file: classFile, details: {} }], edges: [], truncated: false };
      if (method === 'classGraph') return { nodes: [
        { id: classId, kind: 'class', name: 'Scheduler', file: classFile, details: {} },
        { id: 'caller.call', kind: 'class', name: 'Caller', file: path.join(rootA, 'caller.py'), details: {} },
      ], edges: [{ sourceId: 'caller.call', targetId: classId, type: 'calls', status: 'resolved', label: 'run' }], truncated: true };
      if (method === 'moduleGraph') return { nodes: [{ id: params.id, kind: 'module', name: 'scheduler', file: classFile, details: {} }], edges: [], truncated: false };
      if (method === 'inspect') {
        if (params.id === classId) await new Promise(resolve => setTimeout(resolve, 15));
        if (params.id === 'class-b') await new Promise(resolve => setTimeout(resolve, 1));
        return params.id === classId ? classInspect : { ...classInspect, symbol: { ...classInspect.symbol, id: params.id, name: 'Caller' }, children: [] };
      }
      throw new Error(`unexpected graph method ${method}`);
    },
  };
  const classGraph = new GraphViewProvider(Uri.file(extensionDir), graphClient, 'class', async () => {}, async () => {}, output);
  await classGraph.focus(classId, ['inherits', 'calls'], 'both', classFile);
  const classPanel = panels.at(-1);
  assert.notStrictEqual(classPanel, panel, 'class graph should use a separate editor panel');
  await classPanel.webview.receive({ command: 'ready' });
  const classGraphRequests = mockGraphClientCalls.filter(call => call.method === 'classGraph');
  assert(classGraphRequests.some(call => call.params.file === classFile), 'file class graph should query by file');
  assert(classGraphRequests.every(call => call.params.limit === 80), 'graph should use bounded default limit');
  const classGraphMessage = classPanel.webview.messages.find(message => message.command === 'graph');
  assert(classGraphMessage.graph.edges.some(edge => edge.label === 'run'), 'incoming class call label should remain visible in graph payload');
  await classPanel.webview.receive({ command: 'inspect', id: classId });
  assert(classPanel.webview.messages.some(message => message.command === 'inspectResult' && message.inspect.children.some(child => child.name === 'run')), 'selected class should receive member and method overview');
  classPanel.webview.messages.length = 0;
  const graphListener = classPanel.webview.listeners.at(-1);
  await Promise.all([graphListener({ command: 'inspect', id: classId }), graphListener({ command: 'inspect', id: 'class-b' })]);
  const staleResults = classPanel.webview.messages.filter(message => message.command === 'inspectResult');
  assert.strictEqual(staleResults.length, 1, 'stale class overview response should be ignored');
  assert.strictEqual(staleResults[0].inspect.symbol.id, 'class-b', 'latest selected class overview should win');
  assert(classPanel.webview.html.includes('接口概览'), 'class graph should expose compact overview');
  assert(classPanel.webview.html.includes('以此为中心'), 'graph should expose focus action');
  assert(classPanel.webview.html.includes('graphRendered'), 'graph should report render counts');
  assert(classPanel.webview.html.includes('runtimeError'), 'graph should report runtime errors');
  assert(classPanel.webview.html.includes('var(--vscode-editor-background)'), 'graph should use VS Code theme variables');
  assert(!classPanel.webview.html.includes('id="direction"'), 'graph should not expose an inactive direction control');
  assert(!classPanel.webview.html.includes('查看概览'), 'class selection should load overview automatically');
  assert(classPanel.webview.html.includes('g.truncated'), 'graph should communicate truncated results');
  executeGraphWebview(classPanel.webview.html);
  const moduleGraph = new GraphViewProvider(Uri.file(extensionDir), graphClient, 'module', async () => {}, async () => {}, output);
  await moduleGraph.focus('scheduler-module', ['imports', 'calls'], 'both');
  const modulePanel = panels.at(-1);
  await modulePanel.webview.receive({ command: 'ready' });
  assert(mockGraphClientCalls.some(call => call.method === 'moduleGraph' && call.params.id === 'scheduler-module'), 'module graph should use moduleGraph');
  assert(!modulePanel.webview.html.includes('接口概览'), 'module graph should not show class overview');
  const graphScriptCount = assertInlineScriptsCompile(classPanel.webview.html, 'class-graph');
  assert(classPanel.webview.html.includes('isPackage'), 'synthetic package source action should be guarded');
  assert(classPanel.webview.html.includes('target-arrow-shape'), 'graph should show directional arrows');
  const graphRequests = mockGraphClientCalls.filter(call => call.method === 'classGraph' || call.method === 'moduleGraph');
  let landingChoose = 0;
  let landingIndex = 0;
  const landingContext = { globalState: { get: () => rootA, update: async () => {} } };
  const landingState = { summary: null, busy: false, progress: '', error: '', onDidChange: () => ({ dispose() {} }) };
  const landing = new LandingProvider(landingContext, landingState, async () => { landingChoose += 1; }, async () => { landingIndex += 1; }, async () => {}, async () => {}, async () => {});
  const landingView = { webview: new WebviewMock() };
  landing.resolveWebviewView(landingView);
  await new Promise(resolve => setImmediate(resolve));
  assert(panels.at(-1)?.webview.html.includes('开始阅读源码'), 'pending workspace should open main landing panel');
  assert(landingView.webview.html.includes('选择其他目录') || landingView.webview.html.includes('解析'), 'sidebar landing should expose workspace action');
  await landingView.webview.receive({ command: 'chooseFolder' });
  await landingView.webview.receive({ command: 'index' });
  assert.strictEqual(landingChoose, 1, 'landing choose action should dispatch');
  assert.strictEqual(landingIndex, 1, 'landing parse action should dispatch');
  const structureState = { summary: { root: rootA, files: 2, symbols: 4, errors: [] }, symbols: [{ id: 'scheduler', kind: 'class', name: 'Scheduler', file: rootA.toUpperCase().replace(/\\/g, '/') + '/SCHEDULER.PY', qualifiedName: 'scheduler.Scheduler', details: {} }, { id: 'module-scheduler', kind: 'module', name: 'scheduler', file: classFile, qualifiedName: 'scheduler', details: {} }], onDidChange: () => ({ dispose() {} }), loadChildren: async id => id === 'scheduler' ? classInspect.children : [] };
  const structure = new CurrentStructureProvider(structureState, async () => {});
  await structure.setEditor({ document: { uri: Uri.file(classFile.toUpperCase()) } });
  const structureRoots = await structure.getChildren();
  assert.strictEqual(structureRoots[0].label, 'Scheduler', 'current structure should match Windows paths case-insensitively');
  const structureChildren = await structure.getChildren(structureRoots[0]);
  const structureLabels = structureChildren.flatMap(group => group.node.children.map(child => child.label));
  assert(structureLabels.includes('run'), 'current structure should show method names');
  assert(structureChildren.some(group => group.node.children.some(child => String(child.symbol?.details?.parameters?.[0]?.annotation).includes('str'))), 'current structure should show method parameter signatures');
  const outputData = {
    actual: real,
    mockedWebview: { inspectorScriptCount, graphScriptCount, inspectorActions: inspectorCalls, sourceOpenCount: sourceOpens, graphRequests: graphRequests.length, landingMainPanel: true, landingActions: { choose: landingChoose, index: landingIndex }, structureSignature: true, classOverview: true, moduleGraph: true, graphInlineRuntime: true, staleInspectGuard: true, incomingCallLabel: 'run' },
    note: 'Python protocol/process checks are real. VS Code panel and webview checks use a minimal mock; no graphical rendering claim is made.',
  };
  fs.writeFileSync(resultPath, `${JSON.stringify(outputData, null, 2)}\n`);
  console.log(JSON.stringify(outputData, null, 2));
}

run().catch(error => { console.error(error.stack || error); process.exitCode = 1; });
