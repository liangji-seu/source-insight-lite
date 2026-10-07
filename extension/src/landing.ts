import * as crypto from 'crypto';
import * as vscode from 'vscode';
import { ExplorerState } from './treeViews';

export class LandingProvider implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private editorPanel: vscode.WebviewPanel | undefined;
  private editorOpened = false;
  private readonly nonce = crypto.randomBytes(16).toString('base64url');
  constructor(private readonly context: vscode.ExtensionContext, private readonly state: ExplorerState, private readonly chooseFolder: () => Promise<void>, private readonly index: () => Promise<void>, private readonly moduleGraph: () => Promise<void>, private readonly classGraph: () => Promise<void>, private readonly openFile: () => Promise<void>) { state.onDidChange(() => this.update()); }
  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true };
    view.webview.html = this.render(view.webview);
    view.webview.onDidReceiveMessage(message => { void this.handleMessage(message); });
    if (this.shouldOpenEditor()) void this.openEditor();
  }
  private shouldOpenEditor(): boolean {
    const root = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const pending = this.context.globalState.get<string>('architectureExplorer.pendingHome');
    return !root || (!!pending && normalize(root) === normalize(pending));
  }
  private async handleMessage(message: { command?: string }): Promise<void> {
    try {
      if (message.command === 'chooseFolder') await this.chooseFolder();
      else if (message.command === 'index') await this.index();
      else if (message.command === 'moduleGraph') await this.moduleGraph();
      else if (message.command === 'classGraph') await this.classGraph();
      else if (message.command === 'openFile') await this.openFile();
    } catch (error) { void vscode.window.showErrorMessage(error instanceof Error ? error.message : String(error)); }
  }
  private update(): void { if (this.view) this.view.webview.html = this.render(this.view.webview); if (this.editorPanel) this.editorPanel.webview.html = this.render(this.editorPanel.webview); }
  private async openEditor(): Promise<void> {
    if (this.editorOpened && this.editorPanel) { this.editorPanel.reveal(vscode.ViewColumn.One); return; }
    this.editorOpened = true;
    if (vscode.workspace.workspaceFolders?.[0]) await this.context.globalState.update('architectureExplorer.pendingHome', undefined);
    this.editorPanel = vscode.window.createWebviewPanel('architectureExplorer.welcome', '源码架构', vscode.ViewColumn.One, { enableScripts: true, retainContextWhenHidden: true });
    this.editorPanel.onDidDispose(() => { this.editorPanel = undefined; });
    this.editorPanel.webview.html = this.render(this.editorPanel.webview);
    this.editorPanel.webview.onDidReceiveMessage(message => { void this.handleMessage(message); });
  }
  private render(webview: vscode.Webview): string {
    const n = this.nonce; const summary = this.state.summary; const root = summary?.root || vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
    const status = this.state.busy ? `正在解析… ${this.state.progress}` : summary ? `已解析 ${summary.files} 个文件，${summary.symbols} 个符号` : '选择源码目录后开始解析';
    const error = this.state.error ? `<p class="warning">${escape(this.state.error)}</p>` : '';
    const body = !root ? `<h1>开始阅读源码</h1><p>先选择一个源码目录，扩展会在当前窗口打开它。</p><button data-command="chooseFolder" class="primary">选择源码目录</button>` : !summary ? `<h1>开始阅读源码</h1><p class="path">${escape(root)}</p>${error}<p>准备好后，点击解析建立索引。</p><button data-command="chooseFolder">选择其他目录</button><button data-command="index" class="primary" ${this.state.busy ? 'disabled' : ''}>${this.state.busy ? '正在解析…' : '解析'}</button>` : `<h1>开始阅读源码</h1><p class="path">${escape(root)}</p><p class="status">${escape(status)}</p>${error}${summary.errors.length ? `<p class="warning">有 ${summary.errors.length} 个文件需要检查。</p>` : ''}<button data-command="openFile">打开文件开始阅读</button><button data-command="index" ${this.state.busy ? 'disabled' : ''}>重新解析</button><div class="graphActions"><button data-command="moduleGraph">模块关系</button><button data-command="classGraph">类关系</button></div><p class="hint">打开源码文件后，左侧“当前文件结构”会跟随光标所在文件更新。</p>`;
    return `<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource} 'unsafe-inline'; script-src 'nonce-${n}';"><style>${landingCss()}</style></head><body>${body}<script nonce="${n}">const vscode=acquireVsCodeApi();document.querySelectorAll('[data-command]').forEach(button=>button.onclick=()=>vscode.postMessage({command:button.dataset.command}));</script></body></html>`;
  }
  dispose(): void { this.view = undefined; this.editorPanel?.dispose(); this.editorPanel = undefined; }
}
function escape(value: string): string { return value.replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch] || ch)); }
function normalize(value: string): string { return pathNormalize(value); }
function pathNormalize(value: string): string { const normalized = value.replace(/\\/g, '/').replace(/\/$/, ''); return process.platform === 'win32' ? normalized.toLocaleLowerCase() : normalized; }
function landingCss(): string { return `html,body{height:100%;background:var(--vscode-editor-background);color:var(--vscode-editor-foreground)}body{font:13px var(--vscode-font-family);padding:24px;box-sizing:border-box}h1{font-size:20px;margin:0 0 10px}.path{font-family:var(--vscode-editor-font-family);color:var(--vscode-descriptionForeground);word-break:break-all}.status,.hint{color:var(--vscode-descriptionForeground)}.warning{color:var(--vscode-editorWarning-foreground)}button{margin:8px 6px 8px 0;border:1px solid var(--vscode-button-border,var(--vscode-contrastBorder));background:var(--vscode-button-secondaryBackground);color:var(--vscode-button-secondaryForeground);padding:7px 12px;cursor:pointer}.primary{background:var(--vscode-button-background);color:var(--vscode-button-foreground)}.graphActions{margin-top:10px}`; }
