import * as vscode from 'vscode';
import * as path from 'path';
import * as fs from 'fs';
import * as crypto from 'crypto';
import { ChildProcessWithoutNullStreams, spawn } from 'child_process';
import { EngineRequestMap, EngineResultMap, ProgressEvent } from './types';

interface ResponseMessage { id?: number; result?: unknown; error?: { message?: string }; method?: string; params?: unknown; }
type Pending = { resolve: (value: unknown) => void; reject: (reason: Error) => void };

export class EngineClient implements vscode.Disposable {
  private process: ChildProcessWithoutNullStreams | undefined;
  private buffer = '';
  private sequence = 0;
  private readonly pending = new Map<number, Pending>();
  private readonly disposables: vscode.Disposable[] = [];
  private progressHandler: ((event: ProgressEvent) => void) | undefined;
  private errorHandler: ((error: Error) => void) | undefined;
  private root: string | undefined;
  private processRoot: string | undefined;
  private startPromise: Promise<void> | undefined;

  constructor(private readonly context: vscode.ExtensionContext, private readonly output: vscode.OutputChannel) {}

  setProgressHandler(handler: (event: ProgressEvent) => void): void { this.progressHandler = handler; }
  getProgressHandler(): ((event: ProgressEvent) => void) | undefined { return this.progressHandler; }
  setErrorHandler(handler: (error: Error) => void): void { this.errorHandler = handler; }
  getRoot(): string | undefined { return this.root; }
  setRoot(root: string | undefined): void { this.root = root; }

  async request<K extends keyof EngineRequestMap>(method: K, params: EngineRequestMap[K], token?: vscode.CancellationToken): Promise<EngineResultMap[K]> {
    await this.ensureStarted();
    if (!this.process || !this.process.stdin.writable) throw new Error('Architecture engine is not running.');
    const id = ++this.sequence;
    const payload = JSON.stringify({ id, method, params });
    const result = new Promise<unknown>((resolve, reject) => this.pending.set(id, { resolve, reject }));
    const cancellation = token?.onCancellationRequested(() => this.cancelRequest(id));
    try {
      this.process.stdin.write(`${payload}\n`);
      return await result as EngineResultMap[K];
    } finally { cancellation?.dispose(); }
  }

  async index(root: string, excludes: string[], token?: vscode.CancellationToken): Promise<EngineResultMap['index']> {
    this.root = root;
    return this.request('index', { root, excludes }, token);
  }

  async ensureStarted(): Promise<void> {
    if (this.process && !this.process.killed && this.processRoot === this.root) return;
    if (this.process && this.processRoot !== this.root) this.stopProcess(new Error('Workspace root changed.'), false);
    if (this.startPromise) return this.startPromise;
    this.startPromise = this.startInternal();
    try { await this.startPromise; } finally { this.startPromise = undefined; }
  }

  private async startInternal(): Promise<void> {
    if (!vscode.workspace.isTrusted) throw new Error('Architecture Explorer requires a trusted file workspace.');
    if (!this.root) {
      const folders = vscode.workspace.workspaceFolders ?? [];
      if (folders.length === 1 && folders[0].uri.scheme === 'file') this.root = folders[0].uri.fsPath;
    }
    const extensionRoot = this.context.extensionPath;
    const backend = path.join(extensionRoot, 'backend', 'run.py');
    if (!fs.existsSync(backend)) throw new Error(`Bundled analysis engine is missing: ${backend}`);
    await vscode.workspace.fs.createDirectory(this.context.globalStorageUri);
    const cacheRoot = this.context.globalStorageUri.fsPath;
    const dbName = `architecture-${crypto.createHash('sha256').update(this.root ?? this.workspaceHint()).digest('hex').slice(0, 24)}.db`;
    const db = path.join(cacheRoot, dbName);
    const configured = vscode.workspace.getConfiguration('architectureExplorer').get<string>('pythonPath', '');
    const python = configured.trim() || (process.platform === 'win32' ? 'python' : 'python3');
    this.output.appendLine(`Starting analysis engine: ${python} -u ${backend}`);
    const child = spawn(python, ['-u', backend, '--db', db], { shell: false, windowsHide: true, cwd: extensionRoot });
    this.process = child;
    this.processRoot = this.root;
    this.buffer = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => this.consume(chunk));
    child.stderr.on('data', (chunk: string) => this.output.append(chunk));
    child.on('error', (error) => { if (this.process === child) this.fail(error); });
    child.on('exit', (code, signal) => {
      if (this.process !== child) return;
      this.process = undefined;
      this.processRoot = undefined;
      if (code !== 0 || signal) this.fail(new Error(`Analysis engine exited with code ${code ?? 'unknown'}${signal ? ` (${signal})` : ''}.`));
      else this.fail(new Error('Analysis engine exited unexpectedly.'));
    });
    this.disposables.push({ dispose: () => child.kill() });
  }

  private cancelRequest(id: number): void {
    const pending = this.pending.get(id);
    if (!pending) return;
    this.stopProcess(new Error('Request cancelled.'));
  }

  private stopProcess(reason: Error, notify = true): void {
    const child = this.process;
    this.process = undefined;
    this.processRoot = undefined;
    this.buffer = '';
    if (child && !child.killed) child.kill();
    if (notify) this.fail(reason);
    else {
      for (const pending of this.pending.values()) pending.reject(reason);
      this.pending.clear();
    }
  }

  private workspaceHint(): string {
    return vscode.workspace.workspaceFolders?.map(folder => folder.uri.fsPath).join('|') ?? 'no-workspace';
  }

  private consume(chunk: string): void {
    this.buffer += chunk;
    let newline = this.buffer.indexOf('\n');
    while (newline >= 0) {
      const line = this.buffer.slice(0, newline).trim();
      this.buffer = this.buffer.slice(newline + 1);
      newline = this.buffer.indexOf('\n');
      if (!line) continue;
      let message: ResponseMessage;
      try { message = JSON.parse(line) as ResponseMessage; }
      catch (error) { this.output.appendLine(`Invalid engine response: ${String(error)}`); continue; }
      if (message.method === 'progress') { this.progressHandler?.(message.params as ProgressEvent); continue; }
      if (typeof message.id !== 'number') continue;
      const pending = this.pending.get(message.id);
      if (!pending) continue;
      this.pending.delete(message.id);
      if (message.error) pending.reject(new Error(message.error.message || 'Analysis engine request failed.'));
      else pending.resolve(message.result);
    }
  }

  private fail(error: Error): void {
    this.output.appendLine(error.message);
    this.errorHandler?.(error);
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }

  dispose(): void {
    const child = this.process;
    this.process = undefined;
    this.processRoot = undefined;
    if (child && !child.killed) child.kill();
    for (const disposable of this.disposables) disposable.dispose();
    this.disposables.length = 0;
    this.fail(new Error('Architecture engine stopped.'));
  }
}
