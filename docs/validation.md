# 验证记录

验证日期：2026-10-07，Windows 11。本记录区分后端源码分析、VS Code API 集成和实际图形交互，不将它们视为同一项验证。

## 环境

- Node.js 24.13.0，npm 11.6.2。
- Python 3.14.6（Anaconda），分析后端没有第三方 Python 依赖。
- Git 2.53.0.windows.1。
- 本机安装的 VS Code 1.139.1 启动测试时被更新锁阻止；原始错误为 `Code is currently being updated`。未修改现有安装或解除更新锁。

## 真实 vLLM 源码

来源为官方 `vllm-project/vllm` 仓库，commit `5281e49908960f490c8faa789eee221d4ab5001d`。源码位于本次工作目录的 `work/vllm`，不包含在插件安装包中。

结果来自 `scripts/validate_vllm.py`，详细证据见 [vllm-validation.json](vllm-validation.json)。

| 项目 | 结果 |
|---|---:|
| 索引 Python 文件 | 4,962 |
| 符号 | 121,860 |
| 关系记录 | 478,216 |
| 解析错误 | 0 |
| 引擎报告的扫描与分析时间（写库前） | 41.787 秒 |
| 完整脚本时间（含写库和验收查询） | 47.756 秒 |
| 验收断言 | 12 / 12 通过 |

通过的断言：找到 Scheduler；列出成员、方法和定义文件；Scheduler 到 KVCacheManager 的成员类型依赖；继承图保留焦点；schedule 调用图；allocate_slots 目标解析；其定义文件及行号；KVCacheManager 搜索；光标上下文；独立进程进度通知；EngineCore 搜索；Executor 搜索。

关系总数包含未知目标、推断关系、导入、继承等，不能理解为“478,216 条确定的调用边”。`owns` 仅表示成员引用的类依赖，内部字段和方法的包含关系由 `parentId` / `children` 表达。测试证明了上述具体链路，不能证明整个项目所有调用都被正确解析。

## 后端回归

`python -m unittest discover -s backend/tests -v`：4 个测试通过，覆盖多个断言，包括：

- 持久化、模块归属、成员和上下文位置；
- 语法错误隔离、嵌套函数成员范围；
- 别名调用、相对导入、注解成员与推断状态；
- 同名定义的歧义保留、未知限定名称不错误匹配；
- 相对路径排除规则、空关系过滤和图上限端点完整性。

## 前端与安装包

TypeScript strict 编译通过；VSIX 打包通过，包含 Python 后端、编译后的插件和本地 Cytoscape 资源。

`node tests/frontend-smoke.cjs` 通过，详见 [frontend-validation.json](frontend-validation.json)：真实 Python 子进程的索引、查询、检查器和图协议、双工作区数据库隔离与持久化、取消和恢复通过。模拟 Webview 的 Inspect / Open Source / Calls 消息、默认深度 1、Focus Selected 重新查询及内联 JavaScript 语法检查通过。这些模拟测试不证明 Chromium 中的实际图形渲染。

真实 VS Code 宿主测试**未完成**：本机版本被更新锁阻止；随后使用官方 `@vscode/test-electron` 获取独立 VS Code 1.141.0，下载发生超时/连接中断。默认超时和延长超时两轮均失败，因此没有将命令、Webview 渲染或源码跳转的实际 VS Code 交互记录为通过。测试程序保存在 `tests/vscode-smoke.cjs`，待环境恢复后可直接继续。

## 仍需人工/平台验证

- 图的鼠标交互、长时间使用体验、深浅主题的可读性。
- macOS 实机运行。
- SGLang 专项验收；Python 通用解析能力并不等于已完成该仓库的验收。
- Python 动态行为、复杂重导出、跨分支类型流等仍需更深入的语义分析。

本轮没有安装 vLLM 或运行其模型服务，没有联网 AI 请求，也没有发布到扩展市场。

## v0.3 简化阅读流程（2026-10-07）

本次将主要流程改为：选择源码目录 → 显式解析 → 文件树与当前文件结构 → 模块关系 / 类关系。类关系支持以当前文件的全部类为起点，汇总其他类对方法的调用和继承关系，选中类显示属性与方法参数概览。

后端新增 `moduleGraph` 和 `classGraph`，复用现有 SQLite 索引。新增聚合、包含层级、文件多类、反向调用、孤立类、截断和路径过滤断言；5 个后端测试通过。模块关系查询计划确认使用 source/target 关系索引。

使用现有 vLLM 数据库（未重新扫描）测得：模块图一层约 154 ms、两层约 150 ms；类图一层约 364 ms、两层约 373 ms。上述是此机器上的单次查询测量，不包含界面布局时间，不作为普遍性能保证。

本节更新优先于上面的旧版界面操作描述。类图中的调用是静态关系汇总，推断和无法确定的目标仍会标记；不保证覆盖动态运行时的全部对象调用。

v0.3 前端回归通过：目录选择/解析操作、开始主面板、当前文件结构、类图按文件查询、模块图、incoming 方法标签、选中类自动概览、过期响应丢弃、合成包禁用源码与图内联脚本 VM 执行。详见 `frontend-validation.json`。TypeScript 编译和 v0.3.0 VSIX 打包通过。

v0.3 真实宿主复测仍未通过启动阶段：本机 `vscode-updating` 更新锁等待 31 秒后仍未释放，VS Code 报“Code is currently being updated”。未终止或绕过更新进程。因而真实窗口截图、鼠标操作和 Chromium 中的图渲染仍未验证；VM 模拟验证不等同于这项验收。
