import * as path from 'path';
import * as vscode from 'vscode';
import { EngineClient } from './engineClient';
import { LandingProvider } from './landing';
import { Direction, InspectResult, RelationshipType, SymbolRecord } from './types';
import { CurrentStructureProvider, ExplorerState, WorkspaceFileProvider, validateAndInspect } from './treeViews';
import { GraphRenderStatus, GraphViewProvider } from './webviews';

export interface ExtensionApi {
  getClient: () => EngineClient;
  getState: () => ExplorerState;
  whenReady: () => Promise<void>;
  whenGraphRendered: (timeoutMs?: number) => Promise<GraphRenderStatus>;
  getCurrentFile: () => string | undefined;
}
let api: ExtensionApi | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<ExtensionApi> {
  const output = vscode.window.createOutputChannel('源码架构');
  const client = new EngineClient(context, output);
  const state = new ExplorerState(client);
  const currentRoot = (): vscode.Uri | undefined => vscode.workspace.workspaceFolders?.find(folder => folder.uri.scheme === 'file')?.uri;
  const openSource = async (inspect: InspectResult): Promise<void> => openSymbolSource(client, inspect);
  const moduleGraph = new GraphViewProvider(context.extensionUri, client, 'module', async inspect => openSource(inspect), async inspect => openSource(inspect), output);
  const classGraph = new GraphViewProvider(context.extensionUri, client, 'class', async inspect => openSource(inspect), async inspect => openSource(inspect), output);
  let activeGraph = classGraph;
  const files = new WorkspaceFileProvider(currentRoot);
  const structure = new CurrentStructureProvider(state, async symbol => { await openSymbolSource(client, await validateAndInspect(client, symbol.id)); });
  let lastCodeEditor: vscode.TextEditor | undefined;
  const updateEditor = async (editor: vscode.TextEditor | undefined): Promise<void> => { if (editor?.document.uri.scheme === 'file') { lastCodeEditor = editor; await structure.setEditor(editor); } };

  client.setProgressHandler(progress => { state.progress = `${progress.completed}/${progress.total}${progress.file ? ` · ${path.basename(progress.file)}` : ''}`; state.notify(); });
  client.setErrorHandler(error => { state.error = friendlyError(error); state.notify(); void vscode.window.showErrorMessage(state.error); });
  context.subscriptions.push(output, client, state, files, structure);
  context.subscriptions.push(vscode.window.registerTreeDataProvider('architectureExplorer.files', files));
  context.subscriptions.push(vscode.window.registerTreeDataProvider('architectureExplorer.currentStructure', structure));

  const chooseFolder = async (): Promise<void> => {
    const picked = await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false, canSelectMany: false, openLabel: '选择源码目录' });
    const uri = picked?.[0]; if (!uri) return;
    await context.globalState.update('architectureExplorer.pendingHome', uri.fsPath);
    await vscode.commands.executeCommand('vscode.openFolder', uri, { forceNewWindow: false });
  };
  const reloadExisting = async (): Promise<void> => {
    const root = currentRoot(); if (!root || !vscode.workspace.isTrusted) return;
    client.setRoot(root.fsPath); await state.reload(); await structure.setEditor(lastCodeEditor ?? vscode.window.activeTextEditor);
  };
  const indexWorkspace = async (): Promise<void> => {
    if (state.busy) return;
    if (!vscode.workspace.isTrusted) { void vscode.window.showErrorMessage('请先信任当前工作区，再解析源码。'); return; }
    const root = currentRoot(); if (!root) { await chooseFolder(); return; }
    const excludes = vscode.workspace.getConfiguration('architectureExplorer').get<string[]>('excludes', []);
    await vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title: '正在解析源码', cancellable: true }, async (progress, token) => {
      state.busy = true; state.error = ''; client.setRoot(root.fsPath);
      const previous = client.getProgressHandler();
      client.setProgressHandler(event => { state.progress = `${event.completed}/${event.total}`; progress.report({ message: state.progress }); state.notify(); });
      try { const result = await client.index(root.fsPath, excludes, token); state.setSummary(result); await state.reload(); void vscode.window.showInformationMessage(`解析完成：${result.files} 个文件。`); }
      catch (error) { state.error = friendlyError(error); void vscode.window.showErrorMessage(`解析失败：${state.error}`); }
      finally { if (previous) client.setProgressHandler(previous); state.busy = false; state.progress = ''; state.notify(); files.refresh(); }
    });
  };
  const activeEditor = (): vscode.TextEditor | undefined => lastCodeEditor ?? vscode.window.activeTextEditor;
  const currentContext = async (): Promise<SymbolRecord | undefined> => {
    const editor = activeEditor(); if (!editor || !state.summary) return undefined;
    return (await client.request('context', { file: editor.document.uri.fsPath, line: editor.selection.active.line + 1 })) ?? undefined;
  };
  const chooseSymbol = async (symbols: SymbolRecord[], placeHolder: string): Promise<SymbolRecord | undefined> => {
    const pick = await vscode.window.showQuickPick(symbols.map(symbol => ({ label: symbol.name, description: symbol.qualifiedName, symbol })), { placeHolder });
    return pick?.symbol;
  };
  const chooseModule = async (): Promise<SymbolRecord | undefined> => {
    const contextSymbol = await currentContext();
    if (contextSymbol?.moduleId) return state.symbols.find(symbol => symbol.id === contextSymbol.moduleId);
    const editor = activeEditor(); const local = editor ? state.symbols.filter(symbol => symbol.kind === 'module' && sameFile(symbol.file, editor.document.uri.fsPath)) : [];
    return chooseSymbol(local.length ? local : state.symbols.filter(symbol => symbol.kind === 'module'), '选择模块');
  };
  const chooseClass = async (): Promise<SymbolRecord | undefined> => {
    const contextSymbol = await currentContext();
    const contextClass = contextSymbol?.kind === 'class' ? contextSymbol : contextSymbol?.parentId ? state.symbols.find(symbol => symbol.id === contextSymbol.parentId && symbol.kind === 'class') : undefined;
    if (contextClass) return contextClass;
    const editor = activeEditor(); const local = editor ? state.symbols.filter(symbol => symbol.kind === 'class' && sameFile(symbol.file, editor.document.uri.fsPath)) : [];
    if (editor && !local.length) { void vscode.window.showInformationMessage('当前文件未定义类；可以从工作区打开模块关系。'); return undefined; }
    return chooseSymbol(local.length ? local : state.symbols.filter(symbol => symbol.kind === 'class'), '选择类');
  };
  const showModuleGraph = async (id?: string): Promise<void> => { const symbol = id ? state.symbols.find(item => item.id === id && item.kind === 'module') : await chooseModule(); if (symbol) { activeGraph = moduleGraph; await moduleGraph.focus(symbol.id, ['imports', 'calls'], 'both'); } };
  const showClassGraph = async (id?: string): Promise<void> => {
    if (id) { const symbol = state.symbols.find(item => item.id === id && item.kind === 'class'); if (symbol) { activeGraph = classGraph; await classGraph.focus(symbol.id, ['inherits', 'calls'], 'both'); } return; }
    const editor = activeEditor();
    const local = editor ? state.symbols.filter(symbol => symbol.kind === 'class' && sameFile(symbol.file, editor.document.uri.fsPath)) : [];
    if (local.length && editor) { activeGraph = classGraph; await classGraph.focus(local[0].id, ['inherits', 'calls'], 'both', editor.document.uri.fsPath); return; }
    const symbol = await chooseClass(); if (symbol) { activeGraph = classGraph; await classGraph.focus(symbol.id, ['inherits', 'calls'], 'both'); }
  };
  const openFile = async (): Promise<void> => { const picked = await vscode.window.showOpenDialog({ canSelectFiles: true, canSelectFolders: false, canSelectMany: false, openLabel: '打开源码文件' }); if (picked?.[0]) await vscode.commands.executeCommand('vscode.open', picked[0]); };
  const openSourceCommand = async (id: string): Promise<void> => { const inspect = await validateAndInspect(client, id); await openSource(inspect); };
  const safe = (action: (...args: any[]) => Promise<void>) => async (...args: any[]): Promise<void> => { try { await action(...args); } catch (error) { const message = friendlyError(error); state.error = message; state.notify(); void vscode.window.showErrorMessage(message); } };
  const landing = new LandingProvider(context, state, chooseFolder, indexWorkspace, showModuleGraph, showClassGraph, openFile);
  context.subscriptions.push(landing, vscode.window.registerWebviewViewProvider('architectureExplorer.workspace', landing, { webviewOptions: { retainContextWhenHidden: true } }));
  lastCodeEditor = vscode.window.activeTextEditor;
  void structure.setEditor(lastCodeEditor);
  context.subscriptions.push(vscode.window.onDidChangeActiveTextEditor(editor => { void updateEditor(editor); }));
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => { files.refresh(); client.setRoot(currentRoot()?.fsPath); state.summary = null; state.symbols = []; state.error = ''; state.notify(); void reloadExisting(); }));
  void reloadExisting();

  context.subscriptions.push(
    vscode.commands.registerCommand('architectureExplorer.index', safe(indexWorkspace)),
    vscode.commands.registerCommand('architectureExplorer.reindex', safe(indexWorkspace)),
    vscode.commands.registerCommand('architectureExplorer.openArchitecture', safe(showClassGraph)),
    vscode.commands.registerCommand('architectureExplorer.inspectCurrent', safe(async () => { const symbol = await currentContext(); if (symbol) await openSourceCommand(symbol.id); })),
    vscode.commands.registerCommand('architectureExplorer.inspectSymbol', safe(openSourceCommand)),
    vscode.commands.registerCommand('architectureExplorer.openSource', safe(openSourceCommand)),
    vscode.commands.registerCommand('architectureExplorer.openSourceSymbol', safe(openSourceCommand)),
    vscode.commands.registerCommand('architectureExplorer.showCalls', safe(showClassGraph)),
    vscode.commands.registerCommand('architectureExplorer.showCallers', safe(showClassGraph)),
    vscode.commands.registerCommand('architectureExplorer.showInheritance', safe(showClassGraph)),
    vscode.commands.registerCommand('architectureExplorer.showDependencies', safe(showModuleGraph)),
    vscode.commands.registerCommand('architectureExplorer.showModuleGraph', safe(showModuleGraph)),
    vscode.commands.registerCommand('architectureExplorer.showClassGraph', safe(showClassGraph)),
    vscode.commands.registerCommand('architectureExplorer.searchSymbol', safe(async () => { const symbol = await chooseSymbol(state.symbols, '选择类或模块'); if (symbol) await openSourceCommand(symbol.id); })),
    vscode.commands.registerCommand('architectureExplorer.copyAIContext', safe(async () => {})),
  );
  api = { getClient: () => client, getState: () => state, whenReady: async () => { await reloadExisting(); }, whenGraphRendered: (timeoutMs?: number) => activeGraph.whenGraphRendered(timeoutMs), getCurrentFile: () => activeEditor()?.document.uri.fsPath };
  return api;
}
export function getExtensionApi(): ExtensionApi | undefined { return api; }

async function openSymbolSource(client: EngineClient, inspect: InspectResult): Promise<void> {
  const symbol = inspect.symbol; if (!symbol.file || !path.isAbsolute(symbol.file)) throw new Error('源码路径无效。');
  const roots = vscode.workspace.workspaceFolders?.filter(folder => folder.uri.scheme === 'file').map(folder => path.resolve(folder.uri.fsPath)); const file = path.resolve(symbol.file);
  if (!roots?.some(root => isWithinRoot(root, file))) throw new Error('源码路径不在当前工作区内。');
  const document = await vscode.workspace.openTextDocument(vscode.Uri.file(file)); const editor = await vscode.window.showTextDocument(document, vscode.ViewColumn.One);
  const position = new vscode.Position(Math.max(0, Math.min(document.lineCount - 1, symbol.startLine - 1)), 0); editor.selection = new vscode.Selection(position, position); editor.revealRange(new vscode.Range(position, position), vscode.TextEditorRevealType.InCenter);
}
function isWithinRoot(root: string, file: string): boolean { const relative = path.relative(root, file); return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)); }
  function sameFile(a: string, b: string): boolean { const normalize = (value: string): string => { const normalized = path.resolve(value).replace(/\\/g, '/'); return process.platform === 'win32' ? normalized.toLocaleLowerCase() : normalized; }; return normalize(a) === normalize(b); }
function friendlyError(error: unknown): string { const message = error instanceof Error ? error.message : String(error); if (/trust/i.test(message)) return '请先信任当前工作区，再解析源码。'; if (/Bundled analysis engine is missing/i.test(message)) return '找不到内置分析引擎，请重新安装扩展。'; if (/spawn .*ENOENT|not found|找不到/i.test(message)) return '找不到 Python，请在设置中填写 Python 路径。'; return message; }
