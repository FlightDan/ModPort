# ModPort

[![界面语言：简体中文 / English](https://img.shields.io/badge/语言-简体中文%20%2F%20English-blue)](#如何使用)
[![桌面下载：Windows](https://img.shields.io/badge/桌面-Windows-0078D4)](https://github.com/FlightDan/ModPort/releases)
[![桌面下载：Linux](https://img.shields.io/badge/桌面-Linux-FCC624?logo=linux&logoColor=black)](https://github.com/FlightDan/ModPort/releases)
[![许可证：AGPL-3.0-only](https://img.shields.io/badge/License-AGPL--3.0--only-green)](LICENSE)

[简体中文](README.md) | [English](README.en.md)

**跨版本维护太累？想把精力留给一个主分支？试试 ModPort。**

Minecraft 更新了，你的 Mod 又要迁移了。研究 API 变化、修改代码、处理构建错误、检查游戏行为……这些重复又费心的工作，交给 ModPort 帮你推进，把精力留给真正想做的功能。

ModPort 是一个由 AI Agent 驱动的 Minecraft Mod 迁移工具，帮助你将 Forge Mod 迁移到 NeoForge。围绕迁移的完整过程，我们深入设计了一套**细粒度拆分、并行执行、分层治理**的工作流，让自动迁移更省心，也让模型预算花得更值。

- **能同时做的，就同时推进。** 将迁移拆成职责清晰的任务，让多个 Agent 并行研究和修改不同部分，再按依赖关系衔接、集成与验证，减少串行等待。
- **不同的工作，用合适的模型。** 规划和复杂决策可以交给更强的模型，范围明确的执行任务可以使用更经济的模型。模型与思考强度按角色、阶段配置，把预算用在关键处，减少不必要的模型开销。
- **从任务分派，到进度监督。** 基于 Dispatcher SDK 协调任务依赖、执行预算与恢复，配合独立审查和监督 Agent，减少人工盯守、传递上下文和处理异常的负担。

**少一点版本维护，多一点创作时间。**

[下载 ModPort](https://github.com/FlightDan/ModPort/releases) · [查看迁移案例](#实际迁移案例) · [使用文档](docs/README.md) · [反馈与建议](https://github.com/FlightDan/ModPort/issues)

## 如何使用

### 桌面版：下载，配置，开始迁移

Windows 和 Linux 用户可以直接从 **[GitHub Releases](https://github.com/FlightDan/ModPort/releases)** 下载对应的桌面包。

| 平台 | 启动方式 |
| --- | --- |
| Windows | 解压到本地目录，运行 `ModPort.exe` |
| Linux | 解压到本地目录，运行 `./ModPort` |

桌面包已包含 Python、OpenCode 和 Dispatcher SDK，无需另行安装这些运行时。你还需要准备 **Git、适合项目版本的 JDK，以及可用的模型服务**。Linux 项目执行需要 bubblewrap，客户端测试还需要 Xvfb；环境检查会提示缺少的依赖。

1. **配置模型。** 打开右上角的“配置模型”，填写 API 地址、密钥和模型。可以分别指定困难任务与常规编码使用的模型，也可以共用一个模型。
2. **选择项目。** 填写源码仓库与分支、标签或提交，也可以选择本地源码目录。确认源版本与目标版本，选择开发工作区。
3. **设置预算并开始迁移。** 在执行页查看任务进度、模型用量和 Supervisor 对话；通过“打开工作目录”查看源码改动与构建结果。

界面支持简体中文和英文，右上角可以随时切换。桌面版通过 Linux 的 systemd 或 Windows 的任务计划程序托管迁移；后台任务成功启动后，关闭窗口也不会取消迁移，重新打开即可继续查看。相关环境要求见[桌面版说明](docs/DESKTOP.md)。

平台依赖、后台运行及当前验证范围见[桌面版说明](docs/DESKTOP.md)。Windows 已提供下载包，原生启动、恢复与沙箱行为仍待完整验证；macOS 测试列在下方 Roadmap。

### CLI：从终端开始

CLI 适合习惯终端、远程开发或脚本调用的用户，同样支持中英文。先安装 **Python 3.10+、Node.js/npm、Git 和适合项目的 JDK**，然后按平台安装。

Dispatcher SDK `0.7.1` 从其官方 Release 单独安装；下面的命令不依赖 PyPI 提供 SDK。

<details>
<summary><strong>Linux 安装命令</strong></summary>

```bash
git clone https://github.com/FlightDan/ModPort.git
cd ModPort
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip "setuptools>=77" wheel
python -m pip install --no-deps https://github.com/FlightDan/dispatcher-sdk/releases/download/v0.7.1/dispatcher_sdk-0.7.1-py3-none-any.whl
python -m pip install --no-deps -e .
python -m pip check

npm install -g opencode-ai@1.18.32
opencode auth login
modport --lang zh-CN --help
```

Linux 项目执行还需要安装 bubblewrap；需要客户端测试时安装 Xvfb。具体安装方式取决于你的发行版。

</details>

<details>
<summary><strong>Windows 安装命令（PowerShell）</strong></summary>

```powershell
git clone https://github.com/FlightDan/ModPort.git
cd ModPort
py -3 -m venv .venv

.\.venv\Scripts\python.exe -m pip install --upgrade pip "setuptools>=77" wheel
.\.venv\Scripts\python.exe -m pip install --no-deps https://github.com/FlightDan/dispatcher-sdk/releases/download/v0.7.1/dispatcher_sdk-0.7.1-py3-none-any.whl
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
.\.venv\Scripts\python.exe -m pip check

npm install -g opencode-ai@1.18.32
opencode auth login
.\.venv\Scripts\modport.exe --lang zh-CN --help
```

下文的 `modport` 命令在 Windows 上使用 `.\.venv\Scripts\modport.exe` 调用。多行 Bash 示例需改为单行，或使用 PowerShell 的反引号续行。

</details>

通过 OpenCode 配置所选模型服务后，查看并调整 ModPort 的模型设置：

```bash
modport models show
modport models set --role planner --model PROVIDER/MODEL --reasoning-effort EFFORT
modport models set --role coder --model PROVIDER/MODEL --reasoning-effort EFFORT
```

将 `PROVIDER/MODEL` 和 `EFFORT` 替换成服务支持的模型标识与思考强度。模型配置在启动迁移时保存，之后修改设置会用于新迁移。

**命令行界面预览**（当前中文帮助输出）：

```text
$ modport --lang zh-CN status --help
用法： modport status [-h] [--lang {en,zh-CN}] --run-dir RUN_DIR --run-id
                      RUN_ID [--detail | --task-id TASK_ID]

选项：
  -h, --help         显示此帮助信息并退出
  --lang {en,zh-CN}  界面语言（en 或 zh-CN）；手动选择会被记住
  --run-dir RUN_DIR
  --run-id RUN_ID
  --detail           读取完整 Run 快照；可能需要有界的数据库备份
  --task-id TASK_ID  读取一个任务最近的 SDK 尝试，不展开整个 Run
```

<details>
<summary><strong>启动一次迁移</strong></summary>

下面是 Bash 参数模板。将仓库地址、修订和版本占位符替换成你的项目实际值：

```bash
modport run \
  --mod-id examplemod \
  --source-repository https://github.com/YOUR_NAME/YOUR_MOD.git \
  --source-revision COMMIT_OR_TAG \
  --source-minecraft SOURCE_MINECRAFT_VERSION \
  --source-loader forge \
  --source-loader-version SOURCE_FORGE_VERSION \
  --source-java SOURCE_JAVA_VERSION \
  --target-minecraft TARGET_MINECRAFT_VERSION \
  --target-loader neoforge \
  --target-loader-version TARGET_NEOFORGE_VERSION \
  --target-java TARGET_JAVA_VERSION
```

使用 `--max-seconds` 设置总时限、`--max-agent-assignments` 设置 Agent 分派上限、`--max-parallel-coders` 设置并行编码任务数。默认验证范围为 `full`；选择 `--validation-scope compile_package` 只做编译打包，游戏行为仍为未验证。

使用启动时返回的运行目录和 ID 查看状态：

```bash
modport status --run-dir /path/to/run --run-id RUN_ID
modport status --run-dir /path/to/run --run-id RUN_ID --detail
```

`resume`、`cancel`、`recover` 用于继续、取消和恢复运行，具体参数见 `modport <command> --help`。需要更换界面语言时，使用 `--lang en` 或 `--lang zh-CN`。

</details>

## 实际迁移案例

### ScalingHealth → NeoForge

**[ScalingHealth-NeoForge](https://github.com/ModPortMC/ScalingHealth-NeoForge)** 是使用 ModPort 完成迁移的实际案例。你可以直接查看迁移后的源码，了解一个真实 Mod 迁移到 NeoForge 后的项目形态。

👉 **[查看 ScalingHealth 迁移成果](https://github.com/ModPortMC/ScalingHealth-NeoForge)**

也欢迎分享你使用 ModPort 完成的迁移，让这里出现更多熟悉的 Mod。

## 并行工作流，分层治理

迁移涉及版本研究、依赖适配、代码修改和行为验证，任务之间既有独立工作，也有先后依赖。ModPort 将这些关系组织成可执行的工作流，让 Agent 在各自明确的任务范围内推进工作。

底层由 **[Dispatcher SDK](https://github.com/FlightDan/dispatcher-sdk)** 管理执行与调度，ModPort 负责迁移流程、任务上下文与产物交接，OpenCode 承载具体 Agent 任务。每个编码任务由自己的 Agent 承担，并可在权限和预算范围内使用子 Agent。

| 分工 | 做什么 |
| --- | --- |
| 研究与规划 | 阅读源码与版本资料，梳理行为需求，把迁移拆成带依赖关系的任务 |
| 并行编码 | 在各自工作区完成修改，交付可集成的改动与任务结果 |
| 集成与验证 | 合并改动、整理代码、构建项目，并执行目标版本测试 |
| 独立审查与监督 | 检查改动、调查失败原因与停滞，按工作流发起修复或恢复 |

这套设计希望同时解决三件事：**缩短串行等待、减少人工接管、控制模型成本。** 你可以为规划、编码、监督、审查和摘要配置不同模型与思考强度，在复杂决策上投入更多能力，让范围明确的工作由经济模型承担。实际费用取决于项目复杂度、模型价格与修复次数。

预算、进度与恢复记录贯穿整个执行过程。后台监督会关注模型响应和实质进展，异常进入诊断与恢复流程，恢复仍遵守原有时限和累计预算。详细机制见[工作流说明](docs/WORKFLOW.md)。

## 还有这些用得上的功能

- **本地源码与独立工作区。** 可以从远程仓库或本地目录开始，选择 Git worktree、目录副本或明确确认后直接在原目录开发。本地源码同样会发送给所配置的模型服务。
- **迁移知识复用。** 使用迁移规则、skill 和社区 Wiki 研究包，为新任务提供版本资料；也可以审阅研究草稿，贡献回社区。详见[迁移知识库与贡献](docs/WIKI_KNOWLEDGE.md)。
- **冲突修复与异常恢复。** 集成遇到实际冲突时，在隔离工作区交给 Agent 修复，再回到原集成阶段合并。驱动或主管意外退出后，恢复流程保留原时限与累计预算；执行页分别显示任务调度状态和实际进程状态。
- **保留构建与测试结果。** 将目标测试映射回源码行为需求，区分构建成功和行为验证通过。必需用例须实际执行并通过；跳过或缺失的用例不会算作通过。
- **只读进度页面。** 使用 `modport web` 查看已保存的运行，方便远程了解进度。配置方式见下方说明。

<details>
<summary><strong>数据位置与进度页面</strong></summary>

CLI 默认把数据存放在当前用户的应用数据目录：

| 平台 | 默认目录 |
| --- | --- |
| Linux | `$XDG_DATA_HOME/modport`（变量为绝对路径时），否则 `~/.local/share/modport` |
| Windows | `%LOCALAPPDATA%\ModPort`，未设置时使用 `%USERPROFILE%\AppData\Local\ModPort` |
| macOS | `~/Library/Application Support/ModPort`（平台测试待完成） |

`MODPORT_DATA_ROOT` 可以覆盖应用数据目录；`MODPORT_OUTPUT_ROOT`、`MODPORT_SKILL_STORE` 和 `MODPORT_ARCHIVE_ROOT` 分别覆盖运行、迁移 skill 和归档目录。桌面版使用 Electron 用户数据目录，可用 `MODPORT_DESKTOP_DATA_ROOT` 覆盖。

运行数据可能包含源码、日志与迁移结果，请保存在私有目录。需要归档后释放空间时，归档目录须配置在独立文件系统；默认位置不保证满足该条件。

只读页面绑定本机地址的示例：

```bash
modport web --host 127.0.0.1 --password-file /path/to/password-file
```

页面不会启动、取消或重试迁移，也不会调用模型。远程访问请使用可信的反向代理或 SSH 隧道，避免直接将未加密的 HTTP 服务暴露到不可信网络。

</details>

进一步了解：[桌面版说明](docs/DESKTOP.md) · [工作流](docs/WORKFLOW.md) · [Agent 规则](docs/AGENT_RULES.md) · [运行证据](docs/EVIDENCE_PROTOCOL.md) · [完整文档索引](docs/README.md)

## Roadmap

- [ ] **macOS 测试**：验证安装、启动、迁移执行与恢复流程。
- [ ] **更全面的自动冲突修复**：覆盖更多复杂冲突场景，进一步减少迁移中的人工介入。

如果你也是 Mod 开发者，有希望支持的版本、迁移场景或新功能，欢迎通过 **[GitHub Issues](https://github.com/FlightDan/ModPort/issues)** 联系我，一起把 ModPort 做得更好。也欢迎直接提交 PR，参与代码、文档与迁移知识库的建设。

贡献方式见[贡献指南](CONTRIBUTING.md)；私密报告安全问题请参考[安全说明](SECURITY.md)。

## 许可证

Copyright © 2026 [FlightDan](https://github.com/FlightDan/)。ModPort 使用 **[AGPL-3.0-only](LICENSE)** 许可证。第三方组件及其许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

ModPort 的许可证不会自动适用于迁移生成或修改的 Mod 及其他文件；它们的许可条件取决于相应源码、依赖和项目许可证。详见[归属说明](ATTRIBUTION.md)。

如果愿意，也欢迎在迁移后的项目中写上一句：**“本项目使用 ModPort 完成迁移。”**
