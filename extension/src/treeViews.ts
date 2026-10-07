import * as path from 'path';
import * as vscode from 'vscode';
import { EngineClient } from './engineClient';
import { InspectResult, SymbolRecord, Summary } from './types';

export class ExplorerState implements vscode.Disposable {
  private readonly emitter = new vscode.EventEmitter<void>();
  readonly onDidChange = this.emitter.event;
  summary: Summary | null = null;
  symbols: SymbolRecord[] = [];
  busy = false;
  progress = '';
  error = '';
  private readonly childrenCache = new Map<string, SymbolRecord[]>();
  constructor(readonly client: EngineClient) {}
  notify(): void { this.emitter.fire(); }
  setSummary(summary: Summary): void { this.summary = summary; this.emitter.fire(); }
  async reload(): Promise<void> {
    this.busy = true; this.error = ''; this.emitter.fire();
    try {
      this.summary = await this.client.request('summary', {});
      if (this.summary?.root) this.client.setRoot(this.summary.root);
      this.symbols = [];
      this.childrenCache.clear();
      if (this.summary) for (const kind of ['module', 'class'] as const) {
        for (let offset = 0;; offset += 10000) {
          const page = await this.client.request('symbols', { kind, limit: 10000, offset });
          this.symbols.push(...page);
          if (page.length < 10000) break;
        }
      }
    } catch (error) { this.error = error instanceof Error ? error.message : String(error); }
    finally { this.busy = false; this.emitter.fire(); }
  }
  async loadChildren(parentId: string): Promise<SymbolRecord[]> {
    const cached = this.childrenCache.get(parentId); if (cached) return cached;
    const children: SymbolRecord[] = [];
    for (let offset = 0;; offset += 10000) {
      const page = await this.client.request('symbols', { parentId, limit: 10000, offset });
      children.push(...page); if (page.length < 10000) break;
    }
    this.childrenCache.set(parentId, children); return children;
  }
  dispose(): void { this.emitter.dispose(); }
}

export class WorkspaceFileProvider implements vscode.TreeDataProvider<FileItem>, vscode.Disposable {
  private readonly emitter = new vscode.EventEmitter<FileItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;
  constructor(private readonly root: () => vscode.Uri | undefined) {}
  refresh(): void { this.emitter.fire(undefined); }
  getTreeItem(item: FileItem): vscode.TreeItem { return item; }
  async getChildren(element?: FileItem): Promise<FileItem[]> {
    const folder = element?.uri ?? this.root(); if (!folder) return [];
    const entries = await vscode.workspace.fs.readDirectory(folder);
    return entries.filter(([name]) => !['.git', '.venv', 'node_modules', '__pycache__', 'build', 'dist'].includes(name)).sort((a, b) => (a[1] === b[1] ? a[0].localeCompare(b[0]) : a[1] === vscode.FileType.Directory ? -1 : 1)).map(([name, type]) => new FileItem(vscode.Uri.joinPath(folder, name), type));
  }
  dispose(): void { this.emitter.dispose(); }
}
export class FileItem extends vscode.TreeItem {
  constructor(readonly uri: vscode.Uri, type: vscode.FileType) {
    super(path.basename(uri.fsPath), type === vscode.FileType.Directory ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    this.contextValue = type === vscode.FileType.Directory ? 'folder' : 'file';
    this.iconPath = new vscode.ThemeIcon(type === vscode.FileType.Directory ? 'folder' : 'file');
    if (type !== vscode.FileType.Directory) this.command = { command: 'vscode.open', title: '打开文件', arguments: [uri] };
  }
}

interface StructureNode { label: string; symbol?: SymbolRecord; children: StructureNode[]; kind: 'file' | 'group' | 'class' | 'method' | 'member' | 'function' | 'info'; }
export class CurrentStructureProvider implements vscode.TreeDataProvider<StructureItem>, vscode.Disposable {
  private readonly emitter = new vscode.EventEmitter<StructureItem | undefined>();
  readonly onDidChangeTreeData = this.emitter.event;
  private file: vscode.Uri | undefined;
  private nodes: StructureNode[] = [];
  constructor(private readonly state: ExplorerState, private readonly openSource: (symbol: SymbolRecord) => Promise<void>) { state.onDidChange(() => this.refresh()); }
  async setEditor(editor: vscode.TextEditor | undefined): Promise<void> {
    this.file = editor?.document.uri;
    this.nodes = [];
    if (this.file && this.file.scheme === 'file') {
      const classes = stateSymbols(this.state, this.file.fsPath, 'class');
      const modules = stateSymbols(this.state, this.file.fsPath, 'module');
      const groups: StructureNode[] = [];
      for (const cls of classes) groups.push({ label: cls.name, kind: 'class', symbol: cls, children: [] });
      if (groups.length) this.nodes = groups;
      else if (modules.length) this.nodes = modules.map(module => ({ label: module.name, kind: 'file', symbol: module, children: [] }));
    }
    this.emitter.fire(undefined);
  }
  refresh(): void { if (this.file) void this.setEditor({ document: { uri: this.file } } as vscode.TextEditor); else this.emitter.fire(undefined); }
  getTreeItem(item: StructureItem): vscode.TreeItem { return item; }
  async getChildren(element?: StructureItem): Promise<StructureItem[]> {
    if (!element) {
      if (!this.file) return [new StructureItem({ label: '打开源码文件后显示结构', kind: 'info', children: [] }, this.openSource)];
      if (!this.state.summary) return [new StructureItem({ label: '解析完成后显示结构', kind: 'info', children: [] }, this.openSource)];
      if (!this.file.fsPath.toLocaleLowerCase().endsWith('.py')) return [new StructureItem({ label: '当前文件不是 Python 源码', kind: 'info', children: [] }, this.openSource)];
      if (!this.nodes.length) return [new StructureItem({ label: '当前文件没有可显示的类或函数', kind: 'info', children: [] }, this.openSource)];
      return this.nodes.map(node => new StructureItem(node, this.openSource));
    }
    const node = element.node;
    if (node.symbol && !node.children.length && (node.kind === 'class' || node.kind === 'file')) {
      const children = await this.state.loadChildren(node.symbol.id);
      const members = children.filter(item => ['member', 'constant'].includes(item.kind));
      const methods = children.filter(item => ['method', 'function'].includes(item.kind));
      node.children = [];
      if (members.length) node.children.push({ label: this.nodes.some(item => item.kind === 'class') ? '成员属性' : '模块变量', kind: 'group', children: members.map(item => ({ label: item.name, kind: 'member', symbol: item, children: [] })) });
      const classMethods = methods.filter(item => item.kind === 'method');
      const functions = methods.filter(item => item.kind === 'function');
      if (classMethods.length) node.children.push({ label: '成员方法', kind: 'group', children: classMethods.map(item => ({ label: item.name, kind: 'method', symbol: item, children: [] })) });
      if (functions.length) node.children.push({ label: '函数', kind: 'group', children: functions.map(item => ({ label: item.name, kind: 'function', symbol: item, children: [] })) });
    }
    return node.children.map(child => new StructureItem(child, this.openSource));
  }
  dispose(): void { this.emitter.dispose(); }
}
function stateSymbols(state: ExplorerState, file: string, kind: string): SymbolRecord[] { const target = normalizePath(file); return state.symbols.filter(symbol => symbol.kind === kind && normalizePath(symbol.file) === target); }
function normalizePath(value: string): string { const normalized = path.resolve(value).replace(/\\/g, '/'); return process.platform === 'win32' ? normalized.toLocaleLowerCase() : normalized; }
export class StructureItem extends vscode.TreeItem {
  constructor(readonly node: StructureNode, private readonly openSource: (symbol: SymbolRecord) => Promise<void>) {
    const expandable = node.children.length > 0 || !!node.symbol && (node.kind === 'class' || node.kind === 'file');
    super(node.label, expandable ? vscode.TreeItemCollapsibleState.Collapsed : vscode.TreeItemCollapsibleState.None);
    this.contextValue = node.kind; this.iconPath = new vscode.ThemeIcon(node.kind === 'group' ? 'symbol-field' : node.kind === 'class' ? 'symbol-class' : node.kind === 'method' ? 'symbol-method' : 'symbol-field');
    if (node.symbol) {
      this.command = { command: 'architectureExplorer.openSourceSymbol', title: '查看源码', arguments: [node.symbol.id] };
      const details = node.symbol.details || {};
      this.description = node.symbol.kind === 'method' || node.symbol.kind === 'function' ? `(${parameterText(details.parameters)})${details.returnAnnotation ? ` → ${String(details.returnAnnotation)}` : ''}` : String(details.inferredType || '');
      this.tooltip = `${node.symbol.qualifiedName}\n${this.description || '查看源码'}`;
    }
  }
}

function parameterText(value: unknown): string { return Array.isArray(value) ? value.map(item => typeof item === 'string' ? item : `${(item as { name?: string }).name || ''}${(item as { annotation?: string }).annotation ? `: ${(item as { annotation: string }).annotation}` : ''}`).filter(Boolean).join(', ') : typeof value === 'string' ? value : ''; }

export async function validateAndInspect(client: EngineClient, id: string): Promise<InspectResult> {
  if (!id || id.length > 512) throw new Error('Invalid symbol id.');
  const result = await client.request('inspect', { id });
  if (!result.symbol.id || result.symbol.id !== id) throw new Error('The engine returned a different symbol id.');
  return result;
}
