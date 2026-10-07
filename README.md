# ModPort

[English](README.en.md) | [简体中文](README.md)

ModPort 帮助把 Forge mod 迁移到 NeoForge，组织源码阅读、迁移规划、代码修改、目标构建与测试、独立审查及运行证据。工作流的目标是保留用户要求的行为；一次运行结束本身并不能证明这些行为已经验收。

Copyright © 2026 [FlightDan](https://github.com/FlightDan/)。采用 AGPL-3.0-only。

`1.0.0` 包含 workflow 40。Linux/Windows 桌面包、Python wheel 与源码归档见
[Releases](https://github.com/FlightDan/ModPort/releases/tag/v1.0.0)。桌面包携带
Python、OpenCode 和 Dispatcher SDK；Git、适合项目的 JDK，以及研究贡献
所需的 GitHub CLI 需要另行安装。

## 环境要求

- Python 3.10 或更新版本。
- 单独安装 `dispatcher-sdk==0.7.1`。该版本已发布 wheel 和 source distribution，可从[官方 v0.7.1 release](https://github.com/FlightDan/dispatcher-sdk/releases/tag/v0.7.1)取得；本项目不通过 PyPI 分发 SDK。完整工作区将原始 release assets 放在 `build/sdk-release/0.7.1/`，并将 sdist 解压到 `dispatcher-sdk/`。
- Git、适合源和目标版本的 JDK 与构建工具、所需 Minecraft/加载器依赖的访问权限，以及已配置模型服务的凭据。
- 命令行 agent 运行时使用 OpenCode 1.18.32。Linux 上的项目构建通过 bubblewrap 执行；桌面运行时也可使用 systemd 监督长时间任务。平台信息和当前验证边界见[桌面版说明](docs/DESKTOP.md)。

## 从源码安装

创建 Python 环境，先安装单独取得的 SDK，再安装当前 ModPort 源码。若完整工作区包含 release assets，可直接安装本地 wheel；否则从[官方 release](https://github.com/FlightDan/dispatcher-sdk/releases/tag/v0.7.1)下载 wheel，或安装从 sdist 解出的源码目录：

```sh
python3 -m venv .venv

.venv/bin/python -m pip install --no-deps https://github.com/FlightDan/dispatcher-sdk/releases/download/v0.7.1/dispatcher_sdk-0.7.1-py3-none-any.whl

# 或安装从官方 sdist 解出的源码。
# .venv/bin/python -m pip install --no-deps ./dispatcher-sdk

.venv/bin/python -m pip install --no-deps -e .
.venv/bin/modport --help
```

SDK wheel 或源码目录必须标明版本 `0.7.1`。`--no-deps` 可避免 pip 尝试从包索引获取 SDK；运行此命令前必须已经安装了匹配版本。ModPort 的独立源码包和 wheel 不包含 SDK；桌面包组装时会携带 `dispatcher-sdk/` 中的对应 SDK 源码和许可文件。Linux 下有限范围的 SDK 集成检查已通过，包括导入与 `pip check`、14 项 SDK 兼容性测试，以及重新打开执行与 ACK/围栏、提交间隙回执恢复和进程监督器取消/恢复路径检查。完整真实 Hyperbox 验收和原生 Windows 验收仍未完成；这些检查不代表完整迁移验收通过。源码安装也需要本项目 `pyproject.toml` 声明的构建工具；离线安装时请先准备这些工具。目录约定见[文档索引](docs/README.md)。

命令行使用前安装固定版本 OpenCode，并通过其认证流程配置所选模型服务：

```sh
npm install -g opencode-ai@1.18.32
opencode --version
```

也可通过 `MODPORT_OPENCODE_BIN` 指向该版本的可执行文件。ModPort 不读取
Codex 登录数据。Linux 客户端测试还需要 Xvfb；具体 JDK 和构建依赖由锁定
的目标版本决定。

## 启动迁移

提供源码仓库、修订版本，以及 mod 实际使用的 Minecraft、加载器和 Java 源/目标版本。以下版本参数仅作占位示例，请替换成项目和构建环境支持的准确版本。

```sh
.venv/bin/modport run \
  --mod-id examplemod \
  --source-repository "$SOURCE_REPOSITORY_URL" \
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

`--max-seconds`、`--max-agent-assignments` 和 `--max-parallel-coders` 可调整运行限额。`--validation-scope compile_package` 会延期运行时和游戏行为测试，因此验收状态保持为 `unverified`。默认范围为 `full`。

每次运行都会记录迁移输入、工作流定义和所选模型配置。新运行使用当前安装的 ModPort 源码所包含的工作流。可使用 `status`、`resume`、`cancel` 和 `recover` 命令，结合运行目录和 ID 查看或管理已有运行；`modport <command> --help` 显示当前参数。由其他工作流或 SDK 版本创建的旧运行可能与当前安装版本不兼容。

## 数据目录

默认情况下，ModPort 把应用数据保存在当前用户的数据目录：

- Linux：`$XDG_DATA_HOME/modport`（当 `XDG_DATA_HOME` 为绝对路径时），否则为 `~/.local/share/modport`。
- Windows：`%LOCALAPPDATA%\ModPort`；未设置 `LOCALAPPDATA` 时使用 `%USERPROFILE%\AppData\Local\ModPort`。
- macOS：`~/Library/Application Support/ModPort`。

设置 `MODPORT_DATA_ROOT` 可指定其他应用数据目录。运行默认保存在其 `runs/` 子目录；也可用 `MODPORT_OUTPUT_ROOT` 单独覆盖运行目录。可复用迁移 skill 默认保存在 `migration-skills/`，可用 `MODPORT_SKILL_STORE` 更改。归档运行产物默认保存在 `archives/`，可用 `MODPORT_ARCHIVE_ROOT` 更改。请把这些目录保存在私有位置，因为其中可能包含源码、迁移改动、日志和证据。

桌面版使用 Electron 的用户数据目录，`MODPORT_DESKTOP_DATA_ROOT` 可覆盖它；
新实例会保存所选 skill 库和驱动路径配置，恢复时继续使用这些选择。归档
释放仍要求归档目录处于独立文件系统；需要启用释放时显式配置
`MODPORT_ARCHIVE_ROOT`，默认位置不保证满足这个条件。旧本机数据已移入
忽略的 `legacy/`，不会自动导入新运行。

## 模型设置

模型选择与工作流版本相互独立。可查看生效设置，也可按角色修改模型，无需编辑 Python 文件：

```sh
.venv/bin/modport models show
.venv/bin/modport models set --role planner --model PROVIDER/MODEL --reasoning-effort EFFORT
.venv/bin/modport models show --config /path/to/modport-models.json
```

支持的角色有 `default`、`planner`、`coder`、`supervisor`、`contract_review` 和 `summary`。JSON 配置中的阶段项目可以覆盖角色设置。配置读取顺序为 `MODPORT_MODEL_CONFIG`、当前目录中的 `modport-models.json`，最后回退到包内默认值。提交运行时会冻结所选配置；后续修改只影响新运行。请在运行环境中配置模型服务，并避免把凭据写入源码仓库或迁移输入。

## 行为验证

ModPort 从源码和文档记录行为需求，然后独立设计目标测试并把测试结果映射回这些需求。来源测试 harness 不作为目标验收证据。每项必需的目标用例都必须实际执行并通过，才能报告目标行为已验收。跳过、缺失、延期或失败的用例会使验收保持未验证或标记失败。构建成功或 SDK 执行状态本身不能证明行为已经验收。

[工作流说明](docs/WORKFLOW.md)介绍迁移阶段和证据模型。

## 桌面应用

命令行和桌面界面支持简体中文、英文。桌面右上角可切换并记住语言；
命令行使用 `modport --lang zh-CN --help` 或 `modport --lang en --help`。
首次使用跟随系统语言，其他语言回退英文。语言设置不改变迁移工作流。

桌面应用提供项目设置、模型配置、执行状态和 Supervisor 对话。运行迁移仍需 Git、合适的 JDK、模型访问配置和平台构建支持。当前桌面行为与限制见[桌面版说明](docs/DESKTOP.md)。原生 Windows 启动、恢复、沙箱权限及文件锁行为目前仍未验证。完整迁移验收仍未验证。

## 社区 Wiki 研究与贡献

CLI 的 `modport wiki` 提供 `update`、`import-pack`、`build-pack`、`drafts`、`export` 和 `export-draft` 命令，可更新本地 Wiki（供之后新建的迁移实例使用）、导入或构建研究包、查看草稿并导出 Run 研究结果或草稿文件。具体参数见 `modport wiki --help`。桌面“研究贡献”界面支持审阅和编辑本地草稿、浏览器登录 GitHub、提交 Draft PR，并更新本地 Wiki；提交需要安装 GitHub CLI（`gh`）。真实浏览器授权、所有者账号 Draft PR 提交与重复提交返回同一 PR 已验证，首个 [research-v0.1.0 研究包](https://github.com/FlightDan/modport-wiki-for-agents/releases/tag/research-v0.1.0)已发布并验证匿名下载和离线导入。真实模型研究也已通过正式 SDK/MCP 路径读取该研究包的缓存、保留引用并自动导出可编辑贡献草稿。这次仅执行文档研究，完整迁移验收、外部贡献者的 fork 路径和 Windows 原生行为仍未验证；这项集成包含在 ModPort 1.0.0 中。Wiki 资料是可选研究上下文，不能单独证明某个项目已通过验收。

详见[Wiki 知识与集成说明](docs/WIKI_KNOWLEDGE.md)。

## 只读进度页面

可选的 `web` 命令会提供带密码保护的只读页面，查看已保存的运行。若只需本机访问，请绑定 loopback：

```sh
.venv/bin/modport web --host 127.0.0.1 \
  --password-file /path/to/password-file
```

页面不会启动、取消或重试迁移，也不会调用模型。默认 HTTP 传输不加密，请勿向不可信网络开放；需要远程访问时，请使用可信的反向代理或 SSH 隧道。

## 更多文档

- [工作流说明](docs/WORKFLOW.md)：迁移阶段、运行输入与恢复语义。
- [Agent 规则](docs/AGENT_RULES.md)：迁移参与者的工作约束。
- [运行证据协议](docs/EVIDENCE_PROTOCOL.md)：证据结构与验收边界。
- [桌面版说明](docs/DESKTOP.md)和[桌面版 API](docs/DESKTOP_API.md)：桌面应用行为与本地接口。
- [驱动租约](docs/DRIVER_LEASE.md)：持续运行时的驱动所有权与恢复。
- [发行说明](docs/RELEASING.md)：发布准备与打包边界。

## 许可证与迁移产物

ModPort 使用 **AGPL-3.0-only** 许可证；[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 列出第三方组件及其适用条款。详见[许可证全文](LICENSE)和[归属说明](ATTRIBUTION.md)。

ModPort 的许可证不会自动适用于迁移生成或修改的 mod 及其他文件。它们的许可条件取决于相应源码、依赖和项目许可证。归属说明为自愿性质；如希望注明工具，可写：**“本项目使用 ModPort 完成迁移”**。

贡献条款见[CONTRIBUTING.md](CONTRIBUTING.md)，私密报告安全问题的说明见[SECURITY.md](SECURITY.md)。
