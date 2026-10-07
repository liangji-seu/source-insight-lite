// Run inside the real VS Code extension test host with work/vllm as the workspace.
const vscode = require('vscode');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

exports.run = async function () {
  const checks = {};
  const reportPath = path.resolve(__dirname, '../docs/vscode-validation.json');
  const started = Date.now();
  let timer;
  try {
    const extension = vscode.extensions.getExtension('local-dev.source-architecture-explorer');
    assert.ok(extension, 'Extension manifest loaded');
    const api = await extension.activate();
    await api.whenReady();
    checks.activation = extension.isActive;
    const commands = await vscode.commands.getCommands(true);
    for (const command of ['index', 'reindex', 'showModuleGraph', 'showClassGraph', 'openSource']) {
      assert.ok(commands.includes(`architectureExplorer.${command}`));
    }
    checks.commandsRegistered = true;
    if (!api.getState().summary) {
      let ticks = 0;
      timer = setInterval(() => ticks++, 100);
      await vscode.commands.executeCommand('architectureExplorer.index');
      clearInterval(timer);
      assert.ok(ticks > 0, 'Host yielded during parsing');
      checks.indexCommand = true;
    }
    const client = api.getClient();
    const classes = await client.request('symbols', {kind: 'class', query: 'Scheduler', limit: 1000});
    const scheduler = classes.find(s => s.qualifiedName.endsWith('v1.core.sched.scheduler.Scheduler'));
    assert.ok(scheduler, 'Real vLLM Scheduler found');
    await vscode.commands.executeCommand('architectureExplorer.openSource', scheduler.id);
    const active = vscode.window.activeTextEditor;
    assert.ok(active);
    assert.equal(path.resolve(active.document.uri.fsPath).toLowerCase(), path.resolve(scheduler.file).toLowerCase());
    assert.equal(active.selection.active.line, scheduler.startLine - 1);
    checks.openSourceAtDefinition = true;
    const classGraph = await client.request('classGraph', {file: scheduler.file, depth: 1, limit: 80});
    assert.ok(classGraph.nodes.some(s => s.id === scheduler.id));
    assert.ok(classGraph.edges.some(e => e.type === 'calls' && e.targetId));
    checks.fileClassCallGraph = true;
    await vscode.commands.executeCommand('architectureExplorer.showClassGraph');
    const classRendered = await api.whenGraphRendered(15000);
    assert.ok(!classRendered.error, classRendered.error);
    assert.ok(classRendered.nodes >= 1);
    assert.equal(path.resolve(api.getCurrentFile()).toLowerCase(), path.resolve(scheduler.file).toLowerCase());
    checks.currentFileRetainedWithGraph = true;
    checks.classGraphRendered = classRendered;
    await vscode.commands.executeCommand('architectureExplorer.showModuleGraph', scheduler.moduleId);
    const moduleRendered = await api.whenGraphRendered(15000);
    assert.ok(!moduleRendered.error, moduleRendered.error);
    assert.ok(moduleRendered.nodes > 1 && moduleRendered.edges > 0);
    checks.moduleGraphRendered = moduleRendered;
    const report = {passed: true, vscode: vscode.version, elapsedMs: Date.now() - started, checks,
      limitations: ['Real extension command and Webview rendering test; not screenshot or mouse interaction verification.']};
    fs.writeFileSync(reportPath, JSON.stringify(report, null, 2));
    console.log('ARCHITECTURE_SMOKE_PASS', JSON.stringify(report));
  } catch (error) {
    fs.writeFileSync(reportPath, JSON.stringify({passed: false, checks, error: String(error), stack: error.stack}, null, 2));
    throw error;
  } finally { clearInterval(timer); }
};
