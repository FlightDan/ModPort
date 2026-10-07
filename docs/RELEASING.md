# 发布 ModPort

## 当前发布状态

项目采用 **AGPL-3.0-only**，版权署名为 **FlightDan**。迁移成果的工具使用
说明是自愿的，示例见 [ATTRIBUTION.md](../ATTRIBUTION.md)。第三方材料见
[THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

公开源码地址为 [FlightDan/ModPort](https://github.com/FlightDan/ModPort)，默认
分支为 `V1.0.0`。Python 与桌面版本均为 `1.0.0`，对应正式版标签 `v1.0.0`；
工作流版本独立，当前内嵌 workflow 40。
`dispatcher-sdk==0.7.1` 已在[官方 release](https://github.com/FlightDan/dispatcher-sdk/releases/tag/v0.7.1)
发布 wheel 和 source distribution。本工作区将原始 release assets 放在
`build/sdk-release/0.7.1/`，并将 sdist 解压到 `dispatcher-sdk/`，保留 Apache
LICENSE 与 NOTICE。有限范围的 Linux SDK 集成检查已通过；完整真实 Hyperbox 与原生 Windows 验收仍未完成，不能报告为通过。
独立 ModPort 源码包和 wheel 不含 SDK；桌面包组装会携带对应 SDK 源码。

## 公开源码与本地数据

公开内容包括 `src/modport/`、`tests/`、`desktop/`、发布脚本、可复用文档和
许可声明。`scripts/build_source.py` 从明确列出的文件及源码目录组装临时
构建目录，再调用 setuptools；它不复制整个工作区。

运行实例、冻结部署、修复工作区、缓存、下载的可执行文件、模型配置、
凭据和历史验证记录不进入源码包。现有证据不为发布而删改。
源码包显式携带测试，`tests/` 也纳入公开版本控制范围；旧 Run、旧部署、
历史文档及本机旧缓存集中保存在被忽略的 `legacy/`。`.gitignore` 不会移除已经跟踪的文件，
不能作为 Git 历史脱敏的证明。公开现有历史前需另行检查跟踪文件和历史；
本次准备的源码包不携带 `.git/`。

唯一允许继续的旧 Run 由 host 的 `MODPORT_CARRIED_RUN_ID`，或源码工作区内
未发布的 `private-upgrade-policy.json` 中 `carried_run_id` 指定。配置在进程
启动时读取；公开安装未配置时不允许升级任何旧 Run。该设置不修改原 Run
输入、定义、期限或证据，也不允许其他旧实例恢复。

## 构建 ModPort 源码包与 wheel

从项目根目录运行，使用 Python 3.10+ 和 `setuptools>=77` 的构建环境：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install 'setuptools>=77' wheel
.venv/bin/python scripts/build_source.py
```

默认输出到 `dist/opensource/`；可用 `--destination PATH` 修改。构建不运行
迁移，不调用模型，不下载 SDK。没有网络时，预先从受信来源准备上述构建
依赖。wheel 的依赖声明为 `dispatcher-sdk==0.7.1`，源码包不冒充
包含该依赖的离线完整交付物。需要从本地 release asset 安装时，可先执行：

```sh
.venv/bin/python -m pip install --no-index --no-deps \
  build/sdk-release/0.7.1/dispatcher_sdk-0.7.1-py3-none-any.whl
```

发布前检查实际 sdist/wheel 成员、许可元数据和第三方文件；从解压后的
源码重新构建 wheel，再在干净环境中安装已确认的 SDK 和 ModPort。
至少检查实际导入路径、CLI help 及受影响的真实入口。不要用仅构建成功
替代迁移验收，不默认触发 CI 或全套测试。

## SDK 0.7.1 集成

1. 只使用官方 v0.7.1 [release assets](https://github.com/FlightDan/dispatcher-sdk/releases/tag/v0.7.1)：原始 wheel 与 sdist 保存在 `build/sdk-release/0.7.1/`，源码由该 sdist 解到项目根目录 `dispatcher-sdk/`。不要以可变分支或本地重打包内容替代发布资产；不要假设此 SDK 版本能从 PyPI 安装。
2. 保留 sdist 中的 `src/dispatcher_sdk/`、构建文件、README、LICENSE 和 NOTICE；不要把 SDK 虚拟环境、数据库、凭据或运行记录复制到该目录。`dispatcher-sdk/` 是桌面打包使用的源码依赖，不进入独立 ModPort sdist。
3. 从该目录安装或使用发布 wheel，先装 SDK 再装 ModPort。Linux 下已通过有界检查：实际导入与 `pip check`、14 项 SDK 兼容性测试，以及重新打开执行与 ACK/围栏、提交间隙回执恢复、进程监督器取消/恢复路径检查。它们不构成完整迁移验收；真实 Hyperbox 与原生 Windows 验收仍为 `unverified`。
4. 桌面包将携带同一 v0.7.1 SDK 源码及许可证。组装完成后，发行说明须如实列出完成的验证和剩余边界。

## 桌面包

组装脚本需要 Python 3.12+（使用安全 tar 解包过滤器），输入放在
`build/desktop/downloads/`。这些输入需由维护者从官方发布准备：

| 本地文件名 | 官方来源与要求 |
| --- | --- |
| `electron-linux.zip`、`electron-windows.zip` | [Electron 44.5.1](https://github.com/electron/electron/releases/tag/v44.5.1)，对应平台 x64，保留 LICENSE 和 Chromium notices |
| `opencode-linux.tar.gz`、`opencode-windows.zip`、`opencode-LICENSE.txt` | [OpenCode 1.18.32](https://github.com/anomalyco/opencode/releases/tag/v1.18.32)，对应平台 x64，附该版本许可证 |
| `python-windows.zip` | [Python 3.13.12](https://www.python.org/ftp/python/3.13.12/python-3.13.12-embed-amd64.zip)，x64 embeddable，保留 LICENSE.txt |
| `python-linux.tar.gz` | [python-build-standalone](https://github.com/astral-sh/python-build-standalone/releases)，Python 3.13.16 x64，解压后为 `python/`，保留全部运行时许可文件 |

记录实际采用的 Python 补丁版本和运行时发布来源后再公开二进制；不能仅
根据本地通用文件名断言下载内容的版本。V1.0.0 复用已准备的官方运行时：
Electron 压缩包的版本文件为 44.5.1；Linux Python 的 `patchlevel.h` 为
3.13.16；Windows Python DLL 的 FileVersion/ProductVersion 为 3.13.12。
Linux Python 的原始下载标签未保留，故仅记录上游发布来源与实际补丁版本，
不声称已恢复其确切下载 URL。Windows 版本来自文件元数据，不能替代原生执行验收。

```sh
.venv/bin/python scripts/build_desktop.py --platform linux
# 默认使用 build/sdk-release/<SDK版本>/dispatcher_sdk-<SDK版本>-py3-none-any.whl。
# 可用 --sdk-wheel PATH 显式选择另一份官方 wheel。
# SDK 源码也可不放项目根目录，显式选择已完成的源码：
.venv/bin/python scripts/build_desktop.py --platform windows --sdk-root /path/to/dispatcher-sdk
```

默认输出到 `dist/desktop/`。包中包含：

- ModPort 的 LICENSE、NOTICE、ATTRIBUTION.md 和 THIRD_PARTY_NOTICES.md。
- `licenses/` 中的 SDK、OpenCode 和 Electron 声明，以及运行时自带的其他许可文件。
- `runtime/` 中实际执行的 Python 源码；`source/modport/` 中的对应项目源码、
  测试及构建脚本；`source/dispatcher-sdk/` 中同一份 SDK 的源码和构建文件。

将同版本对应源码和二进制一起提供下载；不能仅链接会继续变化的主分支。
本脚本不收录本机历史验证报告。发行说明应列出该交付物实际完成的验证，
并明确 Linux、Windows 和目标游戏行为的边界。Windows 原生启动、恢复、
AppContainer 与真实游戏验收在本次开源准备中均未重新验证。

## 发布入口

发布使用审阅后的源码快照；此前的私有仓库历史保留为私有备份，不进入
公开仓库的 Git 历史。完成源代码/凭据检查和对应源码准备后，才上传
发行物。本地生成压缩包不代表已经公开发布，也不代表目标迁移验收通过。

## 桌面更新渠道

桌面使用公开的 `https://api.github.com/repos/FlightDan/ModPort/releases/latest`
检查正式版。发布时同步递增 `desktop/package.json` 的三段式应用版本，并使用
匹配的正式版标签（例如 `v1.0.0`，小写 `v`）；预发布不进入此渠道。应用版本独立于
工作流版本。发布页应附上对应平台的桌面包和相同版本源码；用户在发布页
手动选择下载。此功能不提供自动安装或迁移正在运行的实例。

源码归档和桌面 `resources/app/` 必须包含 `updates.cjs`，源码/运行时界面
必须包含 `fallback_setup.js`。发布前验证实际生成归档，而非仅检查工作区文件。
