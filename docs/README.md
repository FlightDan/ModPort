# 文档索引

从项目根目录的 [README](../README.md) 开始安装与使用。

| 文档 | 用途 |
| --- | --- |
| [桌面版](DESKTOP.md) | 使用方式、依赖、数据位置和已知平台边界 |
| [桌面 API](DESKTOP_API.md) | 本机服务与界面的接口 |
| [迁移知识库与贡献](WIKI_KNOWLEDGE.md) | 准确版本查询、引用、离线研究包和 GitHub 贡献草稿 |
| [工作流](WORKFLOW.md) | 当前迁移阶段、预算、恢复与验收行为 |
| [Agent 规则](AGENT_RULES.md) | 运行时 agent 的职责及边界 |
| [证据协议](EVIDENCE_PROTOCOL.md) | 源码、工具结果和目标运行证据的接口 |
| [驱动租约](DRIVER_LEASE.md) | 同一个 Run 的单驱动约束 |
| [发布说明](RELEASING.md) | 源码、SDK 集成与桌面交付物的构建 |

## 目录约定

```text
ModPort/
├── src/modport/       Python 实现、打包规则、网页和桌面静态资源
├── tests/             Python 与界面测试、受控测试数据
├── desktop/           Electron 主进程及 preload
├── scripts/           构建与维护入口
├── docs/              当前有效文档
├── dispatcher-sdk/    从正式 v0.7.1 sdist 解出的 SDK 源码（桌面包集成输入）
├── build/sdk-release/ 原始 v0.7.1 wheel 和 sdist（本地构建输入，不随 ModPort sdist 分发）
├── build/             本机构建中间产物与运行时下载（忽略）
├── dist/              新构建的发行物（忽略）
└── legacy/            旧 Run、部署、旧文档与旧缓存（忽略，不发布）
```

根目录另保留构建配置、README、许可文件、贡献与安全说明。
日常运行数据默认保存在当前用户的数据目录，不写入源码目录；具体环境
变量覆盖见项目 README。桌面版使用 Electron 的用户数据目录，可通过
`MODPORT_DESKTOP_DATA_ROOT` 显式覆盖。

工作流、Agent 与证据协议各有一份运行时副本位于 `src/modport/rules/`，
修改时同步对应文档。`legacy/` 中的绝对路径、旧版本及旧结果仅作为历史
记录保留，不代表当前配置，也不支持直接恢复已退役 Run。历史证据和冻结
输入不进入公开仓库或发行包。
