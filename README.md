# 源码架构探索器

用于阅读大型 Python 源码库的 VS Code 插件。中文界面，按“选择源码目录 → 解析 → 阅读文件 → 查看关系图”的流程使用。

## 使用方式

1. 打开一个新的 VS Code 窗口，点击左侧活动栏中的“源码架构”图标。
2. 在开始页面点击“选择源码目录”，选择要阅读的项目文件夹。目录会作为当前窗口的工作区打开。
3. 点击“解析”，等待完成。插件只分析源码，不运行目标项目。
4. 在左侧“文件”中打开源码。下方“当前文件结构”随文件切换，列出类、成员属性和成员方法；点击条目定位源码。
5. 点击“模块关系”查看模块之间的包含、导入和调用；点击“类关系”直接查看当前文件中的类、接口概览，以及其他类对它的方法调用和继承关系。

关系图在右侧独立页面中打开。选择类节点后直接查看属性、方法参数与返回类型；箭头和方法名说明谁调用谁。需要深入时才打开源码，或以该节点为中心继续探索。默认显示附近一层，避免整仓库的关系挤成一团。没有关系时会显示说明；图达到数量上限时会提示。

再次打开已经解析过的工作区会读取本地缓存。源码修改后点击“重新解析”更新。选择目录不会自动开始耗时解析。

## 安装

需要 VS Code 和 Python 3.10+。建议使用 Python 3.12 或更新版本，以支持较新的 Python 语法。

在 VS Code 扩展面板右上角菜单中选择“从 VSIX 安装”，选择随附的 `source-architecture-explorer-0.3.0.vsix`。安装更新后重新加载 VS Code 窗口。

若找不到 Python，在设置中搜索 `architectureExplorer.pythonPath`，填写本机 Python 可执行文件路径。后端仅使用 Python 标准库，不需要安装目标项目的依赖。阅读 vLLM 不需要 GPU、CUDA 或 PyTorch。

## 分析范围

- 当前支持本地 Python 源码；文件树也可打开其他类型文件，但不会为其生成 Python 类结构。
- 解析模块、类、方法、函数、常量、成员及定义位置。
- 类关系汇总类之间的方法调用和继承，支持从当前文件的全部类开始；模块关系汇总包含、导入和调用。包层级来自模块命名空间，不代表自动推断的业务子系统。
- 不确定的静态推断会保留标记；运行时动态派发等无法完整确定，不会通过全局同名猜测连接调用目标。
- 默认排除版本库、虚拟环境、构建产物与依赖目录。缓存保存在插件存储目录，不写入被阅读的源码。
- Windows 优先验证；路径和进程接口采用跨平台实现，macOS 尚未实机验收。

## 开发和测试

`extension/` 是 TypeScript 插件，`backend/` 是独立 Python 分析引擎，二者通过标准输入输出的 JSON 行协议通信。协议见 `docs/protocol.md`。

```powershell
cd extension
npm.cmd install
npm.cmd run build
```

用 VS Code 打开 `extension/`，按 F5 启动开发宿主。在开发宿主进入“源码架构”，选择待阅读目录，然后点击解析。macOS 使用 `npm` 和 `python3`。

在工程根目录执行：

```powershell
python -m unittest discover -s backend/tests -v
node tests/frontend-smoke.cjs
```

前端 smoke 使用真实 Python 子进程及模拟 VS Code/Webview；不等同于实际鼠标与图形渲染验收。真实扩展宿主测试入口为 `tests/vscode-smoke.cjs`。具体证据与尚未完成的检查见 `docs/validation.md`。

真实 vLLM 后端验收：

```powershell
python scripts/validate_vllm.py C:/repos/vllm --db ./vllm.sqlite --report ./vllm-validation.json
```

打包在 `extension/` 执行 `npm.cmd run package`。后端源码会随插件打包，无需单独启动服务。

## 当前限制

当前重新解析会完整重建索引；增量更新、C/C++/CUDA 分析、复杂 Python 动态行为和逻辑架构自动聚类尚未实现。没有自动上传源码或内置付费 AI 请求。原始设计材料保存在 `docs/product-spec.txt`，实际界面以此简化流程为准。

