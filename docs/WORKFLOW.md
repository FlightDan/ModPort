## 当前基线测试矩阵与必测行为验证

当前工作流为 v42。每次新建 Run 使用当前工作区最新工作流，并独立解析和冻结模型配置；修改模型不改变工作流版本。当前只支持宿主明确选定并携带的一个旧 Run 实际续接，沿用它冻结的来源工作流版本、原截止时间和累计 assignment 使用量；历史版本和证据保留，不构成其他旧 Run 的兼容承诺，也不能仅按版本号推定续接资格。

### 按实质进展监管 agent

当前工作流取消 agent 独立的两小时等单次硬时限，包括审查者请求的嵌套返工；SDK 命令和内部模型调用共用原 Run 总截止时间并保留结算余量。新工作流要求有限总时限，不为无限预算虚构一个任意大的超时。确定性工具的技术超时仍保留。

驱动每十分钟根据 SDK 当前 attempt 及有界源码、harness、配置、目标文档和验证结果观察进展。直接比较内容，不增加哈希或 fingerprint 校验。连续三个窗口没有观测到有效进展时，派发独立 supervisor，使用隔离工作区避免被被监管者的写锁阻塞；对活动工作区只读，开发 coder 的明确小问题可按下述 v40 规则在副本中修复。单纯心跳、日志变长或重复相同失败不算进展。supervisor 的 continue 保留同一运行任务；terminate 只请求 SDK 取消指定 execution。决定到达后若工作已恢复则丢弃过时终止决定。SDK 负责撤销执行权和进程清理，清理或 Effect 不确定不能伪装成已终止、已恢复。

原 max_rework_rounds 不再截断修复；原总截止时间、剩余 assignment 预算仍有效，supervisor 也计入该预算。无可用监管额度、非法或失败监管报告只保留诊断，不自动终止被监管任务。观察基线和审查状态由 SDK application_state 持久化，内容存于两个有界轮换侧文件，驱动重启不重置计数或预算。这是本指南旧返工轮数规则的明确例外，不改变历史 Run 冻结输入。

### 独立 watchdog 与主管恢复

新提交和显式升级的 Run 冻结 `watchdog_policy`，默认十分钟未观测到模型或工具会话响应便请求主管诊断。真实模型事件、工具执行进展进入 SDK activity；心跳不算响应，明确的工具等待可单独标注，缺失采样仍为 unknown。连续三个十分钟窗口没有实质进展也请求同一恢复主管，防止反复输出掩盖停滞。

桌面启动独立的 driver 和 watchdog 持久服务。watchdog 使用 SDK 的持久通知投递，将事件保存后再确认收件；驱动退出时它仍然存活。只在 PID、进程出生标识和命名空间都匹配且确认进程已退出时恢复同一驱动。正常成功和用户取消不会重启。暂时无法完成的 SDK 清理保留具体原因并继续等待，不作为不可恢复结论。

主管读取原始错误和宿主证据，选择 continue、repair_resume、wait 或 stop。已失败的任务不能仅靠 continue 恢复；repair_resume 必须带具体修复或诊断指令。小修复限于宿主隔离副本，更广的上游修改使用真实 request_rework，并消费返回结果。宿主重新核对目标身份和新进展；取消通过公共 SDK，清理已确认后才派发新尝试。源码修复仍经过后续编译和目标验证，不修改交付产品或冻结要求。

只允许原预算耗尽或已有充分证据的不可恢复原因停止恢复。非法或缺失报告没有停止权限。Run 已失败时通过 SDK 原 Run 的恢复 generation 先派主管，保留原截止时间、累计 assignment、SDK 历史和原始输入；不新建重试 Run，也不自动加预算。升级部署通过显式 continuation 冻结新实现。该流程由持久服务承担，Codex 无须留在对话中每三十分钟检查。

### 驱动崩溃后的应用收尾（v42）

可选 watchdog 观测遇到 SQLite BUSY/LOCKED 时保存原始错误，按失败返回后的冷却时间重试，不让观测写锁竞争终止迁移驱动。SDK 初始化时观测存储不可用则明确记录需要重新打开 Runtime，当前会话不冒充已恢复监测。其他数据库错误、身份和鉴权错误仍保留原异常路径。

驱动启动和显式 recovery 在 SDK reap/sync 后核对当前执行的 attempt、fence、宿主任务和进程身份。只有旧驱动在可核实的 PID 命名空间中确已退出，且 SDK 持久证据确认对应工作进程树已清理，才通过公共 SDK 将未完成 Effect 转入 recovery_required；不会直接重跑作者。没有 Effect 的中断执行记录为 worker_interrupted。缺失证据、活进程、命名空间不明和旧 attempt 都保持未知，不推断为死亡。

watchdog 主管仍须明确授权 repair_resume。取消授权与旧工作进程树的独立清理证据分别保留；SDK 取消日志中的 unknown 不被改写。满足精确身份和独立清理证明时，只生成失败、外部结果未知、部分产物未验收的应用收尾记录，再通过公共 SDK 结算，后续修复沿用原预算。若崩溃只留下收尾说明，后续更充分的清理证明可完成同一收尾，不伪造原日志。用户取消不触发恢复。若中断的是当前 watchdog 主管本身，宿主仅在同样的精确停止证明下取消并结算该诊断任务，记录失败且不接受任何主管决定，再交给已有的 60 秒诊断重试流程；原截止时间和累计分配数不重置，不能借此重跑业务作者。

桌面分别呈现 SDK 调度状态和已观察到的工作进程状态。宿主复制 SDK 公共 `runtime.observation_storage` 绑定供只读查询；`WorkAvailabilityReport.kernel_source` 是路径，不能充当观测存储 source ID。SDK running 不代表工作进程仍存活；实际观测为 alive 或 exited 才显示对应状态，缺失、不可访问、命名空间不明或过期的进程身份显示 unknown。观察子查询有短时限，但 SDK 准入和控制调用仍沿用 SDK 时序，不承诺绝对亚秒状态响应或五秒恢复期限。当前验证覆盖 Linux 上的恢复与进程观测路径；原生 Windows 进程存活和目标 MOD 行为未验证，此状态观察不证明目标行为验收。

### 诊断时修复明确的小问题（v40）

恢复 planner 和进度 supervisor 检查开发 coder 时，可以直接修改宿主提供的隔离源码副本，修复根因已确定、范围很小的问题。只支持已有普通 UTF-8 源码和配置文件；新增、删除、二进制或不安全改动保留为诊断。不得修改活动 coder 工作区、来源合同、交付产物、凭据和冻结输入。诊断 agent 仍不能运行项目程序或测试，后续目标验证照常执行。

宿主保存修改前后的内容，并绑定原任务与开发计划。新 coder 在记录导出起点后接收修复，让修复进入最终 coder 补丁；输入同时说明哪些修改已应用、哪些仍冲突，以及剩余任务。已有相同修复跳过，接收端文件已变化则整项保留为冲突，不覆盖新代码。最终集成可以消费仍未进入 coder 补丁的 supervisor 修复；若修复晚于集成派发，则在后续隔离 code cleanup 中应用并进入 cleanup 补丁。更迟且没有适用接收点的修复明确记录为 deferred。活动任务保留原命令和输入；迟到的修复留待下一个适用边界，不阻塞任务，也不自动重启作者。发布补丁不等于已消费或已验证，原总截止、预算及显式恢复/返工规则不变，不新增哈希校验。

### 当前候选集成与冲突修复（v41）

旧 Git 基线记录来源，不锁定当前工作区。宿主保留合法候选及用户修改，在隔离 Git 工作区合并原任务补丁；真实冲突记录为 `integration_merge_required`，保存冲突快照、路径、原命令和补丁引用。在已授权的集成修复范围内，宿主通过公共 SDK 持久化并派发新 coder，让其只修改隔离副本中的项目文件、保留所有原任务的意图，不运行项目程序、构建或测试。

修复 coder 必须返回实际补丁；宿主随后重新派发原集成阶段，把完整解决差异合入当时的候选。候选后来又发生变化时，仍执行真实合并并显式处理新冲突。驱动重启使用已持久化的同一修复任务，不重复派发或扣预算。该路由适用于 `development_integrate`、`development_prepare_integrate`、`target_repair_integrate` 和 `contract_repair_integrate`。失败、缺失补丁或未完成集成不会进入后续 cleanup、合同冻结或构建；保留原始错误，不用诊断报告代替集成结果。修复与重新集成共用原截止时间和剩余 assignment 预算，不新增业务审批或哈希校验。

嵌套 coder 的显式返工写入宿主绑定的调用者工作区，调用者导出合并结果；原依赖补丁仍保留。supervisor 的诊断副本不能冒充该写入目标。显式升级仅改变后续执行，保留历史命令、原截止时间和累计用量；已有禁用或持久停止的 watchdog 不因升级而重新开启。

显式续段归档旧集成修复状态，并为剩余集成绑定新的 SDK 执行身份；已完成的修复补丁直接复用，不重复派发 coder。同一 Run 的驱动重启仍沿用已有任务。修复快照中被 Git 忽略的输入按当前内容合并；用户后来删除这些文件时，删除/修改冲突只在隔离副本中物化，不把原文件恢复到用户工作区。

### 目标行为验证的必测完成要求

`artifact_verification` 将冻结目标合同中保留的每项测试和断言视为执行义务。跳过、未实现、缺失运行见证或断言失败都不能结算为 SDK `succeeded`。目标 harness 缺口可进入有界修复和新鲜目标执行；沿用 SDK 任务身份、资源准入、总 assignment/返工上限及原截止时间。无法修复、额度耗尽或到期时先保存失败报告，再结算 `failed`，不延长预算。恢复路径同样遵守此规则。

来源行为根据用户确认和只读源码分析建立，不运行原版，也不派发来源 harness 生成、执行、修复或源缺陷复核。目标 artifact verification 不能修改交付 JAR；测试和适配只针对独立 harness。所有必测目标项通过只证明该冻结验证集合完成，不能替代尚未完成的其他验收；报告保留 `acceptance_status: unverified`。目标测试修复规则不恢复旧的来源运行要求。

`research_cleanup` 只依赖 `source`，在源码获取后进入早期并行路由，与研究和
准备工作并行。它只读源码及研究材料，产生绑定 `source_commit` 的导航索引，
供规划使用。`migration_inventory` 汇合 `research_cleanup`、`early_compile`
和 `behavior_freeze` 的结果。该索引属于诊断上下文，不得认证行为、关闭缺口或
授权上游返工；缺少或失败结果不能复活业务审批门槛。

`full` 范围的常规目标路径为 `development_integrate → code_cleanup → target_contract_freeze → target_build`。
目标修复路径 `target_repair_integrate` 和 `target_revise` 也先进入
`code_cleanup`，再进入 `target_contract_freeze → target_build` 和独立代码审查。`code_cleanup` 可在其分配范围内整理
当前合并候选，但必须保留可观察行为、冻结合同、测试断言和验收义务。成功的
清理不代替目标构建、独立审查或行为验证；失败和发现均作为诊断向下游传递，
不阻止既定验证。只有显式 `request_rework` 工具调用才启动作者返工；审查者
明确要求 `code_cleanup` 返工后，须用返工后的候选新跑 `target_build`。

默认 `full` 路由保留目标执行验证。当前来源行为路径为
`behavior_extract → behavior_review → behavior_freeze`。用户已确认原版 MOD 正常工作，因此来源阶段只读
源代码和文档，不运行或生成原版测试 harness。提取器创建 `behavior_requirements`，为每项行为提供稳定
ID、断言、源码锚点和明确的不确定项，并记录 `source_assumption: user_confirmed_functional`、
`verification_basis: source_reading`。这些内容描述从源码理解的行为，不是原版运行通过证明；包括含糊行为在内，
都记录不确定性，不为消除不确定性执行原版程序。

`behavior_review` 检查需求、断言和源码锚点是否可追溯，并保留无法由源码确定的细节；
`behavior_freeze` 冻结 `behavior_requirements`。来源没有测试评估、缺陷判定、harness 修复或运行回执。

目标合同设计独立创建目标可执行用例，并将目标用例 ID 映射到冻结的行为需求 ID。无需移植原版测试 ID、
harness 或运行证据。`target_contract_freeze` 检查保留行为是否都映射到目标断言，并冻结目标合同及映射。
目标行为验证只以新鲜的目标执行结果作为通过证据；跳过、缺失或未执行的用例均未通过。任何延期目标行为测试的
范围仍须报告 `acceptance_status: unverified`。

对适合服务端执行的行为，按锁定的 Forge/NeoForge 版本使用可复用的官方 GameTest server runner。
例如 [Forge 1.20.1 GameTests](https://docs.minecraftforge.net/en/1.20.1/misc/gametest/) 和
[NeoForge 26.1 GameTests](https://docs.neoforged.net/docs/misc/gametest/)。
客户端用例可批量复用会话；每个用例都要明确重置相关状态，只有隔离确有需要时才重启客户端或服务端。
共享 runner 启动失败只记录一次，其余用例记录为未执行，不得计为通过。所有目标执行仍在无凭据 sandbox 中进行；
产物验证不能修改交付 JAR。当前没有真实目标执行对比，因此不要声称该路径已提升速度。

目标测试设计与执行共享冻结的目标 case ID 和 requirement-to-case 映射。target suite 的输出按目标 case ID
关联到实际 JUnit/GameTest 结果，不复用来源 test IDs。显式返工保留并更新完整的目标合同与映射；来源行为需求仍是
只读分析结果。显示环境按行为类型、显式任务和实际 Gradle 任务图配置，包含 `test` 间接依赖的 `runClient`；
证据声明失败不能取消显示包装器。以上均不替代真实目标测试结果。

## 当前工作流的执行证据

宿主明确选定的携带 Run 续接时，保留其冻结来源版本、原总截止时间、累计 assignment 使用量和原段证据；升级本身不增加预算，也不恢复其他旧 Run。公开发行包不携带私有 Run 选择。
每条 `behavior_requirements` 断言保留源文件位置、相关范围及不确定项；不生成来源测试回执或源文件摘要。目标运行证据绑定明确的目标 Gradle 任务、JUnit 类/方法或 GameTest case、映射的 behavior requirement ID 和本次执行结果。独立审查核对断言与源码含义，不计算源码、候选或 workspace 指纹。基础设施故障、目标断言失败、目标行为未执行和无法归类的结果分别报告。历史通过回执不能替代当前目标执行。

宿主在无凭据 sandbox 中运行冻结的目标用例并读取实际 JUnit/GameTest 结果。每项保留的目标断言都必须新鲜执行并通过才能计为通过；完整任务启动失败时记录一次共享 runner 故障，其余用例明确记录为未执行。普通 `runClient` 启动若没有执行冻结断言，不产生行为通过回执。

OpenCode 作者答复、补丁产物、进程树清理和 SDK 结算分别留证。清理未确认时保存恢复义务；`recover-cleanup --run-dir DIR --run-id ID --command-id ID` 只核对已结算任务和原进程身份、完成清理并写入回执，不重跑作者。候选补丁恢复在清理回执之后单独执行。默认 `status` 使用 SDK 有界摘要与可用工作检查，显示最后代码变化、最后成功认证验证和当前等待对象；单任务详情和完整快照需显式请求。摘要证据不足时显示未知，不推断通过或完成。

## Run 内监督与 Goal 文档修订

每五次业务 agent 派发，宿主通过 SDK 调度一个异步 supervisor，并计入同一分配预算和截止时间。它需读取原始错误、输入、工具往返和代码快照，检查目标、上下文、调度与下游消费链，而非仅复述失败。supervisor 可直接编辑隔离工作区的 `goals/*.md`；宿主保留原文、修订和执行者归属，以精确 task/plan 身份绑定到后续匹配的 goal_prepare/coder。运行中输入及封存历史不被改写，发布记录不等于实际消费或修复成功。报告为自由文本，不新增批准门槛。代码快照仅供调查，修改快照不会修改活动项目。

当前 coder 保留 goal_prepare 生成的具体上下文，并记录实际采用的监督修订。外层 Codex 的例行监督仍至少相隔 30 分钟。模型策略、无凭据 sandbox、SDK 权限和原预算不变。

当前确定性构建准备遇到已有自定义 Gradle 文件时，保留候选与合并草案，把需要语义合并作为诊断交给后续规划；缺失或不受支持的锁定 MDK 输入仍是准备失败。独立 MDK 源码探针只有实际产出 Java 编译器错误时，才把非零结果作为已完成的临时诊断传递；超时、构建基础设施失败、候选身份不符和证据异常仍按失败处理。临时探针无论结果如何都不认证原项目构建，最终仍需针对候选项目的正式目标构建。

Minecraft 共享资产缓存只用于加速资源获取。缓存忙时记录 `busy`，缓存内容不可用或读取失败时记录 `skipped`，继续由 Gradle 获取资源并执行原测试；失败的缓存复用不会终止测试，也不会在本次执行结束时重复尝试回收。源和目标使用各自隔离的可写 Gradle 缓存。共享缓存不提供构建或测试通过的证明，最终结果来自实际执行。

目标测试作者通过自由项目命令工具直接执行的 `runClient` 不会自动获得宿主验证器的 Gradle init 接线或结果归属；裸命令可能只停在游戏菜单。作者的临时启动不能替代宿主对冻结目标断言的执行和结果记录。来源行为提取与复核不启动任何来源程序。

OpenCode 服务清理在杀掉进程组并回收主进程后，最多等待五秒让原进程组消失，并记录等待时长。原进程组消失即可确认清理；若只剩无法由宿主回收的孤儿僵尸进程，等待后再用约 50 毫秒间隔做两次完整 `/proc` 扫描，均只发现僵尸且两次之间仍能确认原进程组身份，才确认进程组已静止，并分别记录 `process_group_gone: false` 与 `group_quiescence_reason: zombie_only`。等待结束仍有活进程或无法完整核实则保留 `opencode_cleanup_unconfirmed`，不把主进程退出当成整组清理完成。这段任务截止后清理余量不执行新的 agent 工作，也不延长 Run 截止。

规划 agent 若报 `opencode_cleanup_unconfirmed`，宿主保留日志、清理诊断和规划输入索引，但不把日志中可能已经完成的消息或空报告转成可派发的任务计划。该错误属于执行器安全边界；无法排除原进程仍在写共享候选时，停止当前 Run 的后续派发并核实残留进程，再沿受支持路径继续。普通业务报告缺字段仍按诊断处理。

## 依赖感知 coder 恢复、planner 模型与 OpenCode 后端

模型配置独立于工作流版本，当前工作流为 v42。包内默认配置将纯规划阶段
`migration_plan`、`target_repair_plan` 和 `coder_revival_plan`
设为 `gpt-6-astra/high`；行为复核与 coder 和 target harness/代码作者使用
`gpt-6.1-sol/high`；supervisor、其他 agent 和提示压缩使用 `gpt-6-luna/max`。
`modport models show` 显示有效配置；`modport models set --role planner --model gpt-6-astra
--reasoning-effort high` 默认写入当前目录的 `modport-models.json`，可用 `--config PATH`
另选文件。支持 `default`、`planner`、`coder`、`supervisor`、`summary`
角色，以及可选的 `subagent`。JSON 的 `default` 与 `roles`、`stages` 各项均为 model/reasoning_effort 对，阶段覆盖优先。

v38 开放普通执行任务和 coder 的 OpenCode 原生 `task` 工具，可调用 `general` 和只读的
`explore` 子 agent。子任务默认读取同一 Run 冻结的 coder 模型与推理强度；配置
`roles.subagent` 可独立覆盖，例如 `modport models set --role subagent --model gpt-6-luna
--reasoning-effort max`。删除该角色覆盖即恢复跟随 coder。子任务属于父 assignment，
共享原有截止时间、累计 Token 预算和宿主沙箱，不另开 Run 或增加预算。
子 agent 保留父任务的只读、敏感路径、网络和 shell 限制；返工请求由父 agent 发起。
纯规划消息与无工具预检仍不开放 `task`，实验性后台子任务仍关闭。子会话用量由事件和
公开 HTTP 子会话接口归集，预算仍采用观测用量控制，正在进行的请求可能超出额度。
开放原生派发属于执行行为变化，因此工作流升级为 v38；单独更换模型不升级工作流。

新提交读取显式 `run --model-config PATH`，否则读取 `MODPORT_MODEL_CONFIG`、当前目录配置
或包内默认配置，并冻结在 Run 的独立模型设置中。worker 和提示压缩读取同一冻结配置；
模型认证由 OpenCode 提供。模型设置不写入 WorkflowDefinition，修改模型无需提升工作流版本。
续接时 `continue --model-config PATH --additional-agent-assignments 20` 可独立更新新段模型并增加
20 次分配，省略 `--additional-seconds` 继承原截止时间；无需 `--upgrade-workflow`。
省略 `--model-config` 保留原 Run 的冻结模型，即使显式升级工作流也是如此。
不要修改正在运行的段或原证据。

### 依赖结算后的 coder revival

v39 将依赖补丁的普通 Git 合并冲突记录为 `dependency_patch_conflict`，保留冲突路径、
原始 Git 输出和依赖补丁引用。宿主通过 SDK 持久化恢复规划请求；只有 planner 明确
选择恢复，才派发到新的隔离工作区。宿主将冲突文件保存为该任务的输入基线，并将
原始补丁和规划指令传给 coder 处理，不能静默选择其中一方。最终集成重建相同的
依赖冲突基线再应用 coder 的解决补丁；仅有冲突基线而没有实际解决补丁时，仍报告集成失败。失败证据和原分配次数、总截止保持不变。
续接段继承的失败结果同样进入恢复规划，不因本段没有对应 attempt 而永久等待。
路径逃逸、符号链接和真实执行隔离错误仍停止派发，不归为可修复的合并冲突。

当 coder 任务 A 依赖的任务 B 结算后，宿主通过 SDK 持久化一个 `coder_revival_plan` 请求。
planner 收到原始 SDK/进程错误、A/B 的任务输入与依赖结果、关联候选代码和可用环境诊断，先判断失败根因，再决定是否以及如何恢复 A。超时、非零退出码和“coder 已退出”只是观察到的结果，不能单独作为重派理由；例如，内存错误必须有相符的系统证据，不能由退出码推定。

planner 的明确恢复决定才允许宿主派发新的 A 执行。宿主校验 Run 和任务身份、停止状态、原始剩余预算/截止时间及请求去重，并沿用既有预算；预算不重置，也没有固定的每任务尝试上限。planner 可以停止或要求先解决输入、代码或环境问题。SDK 结算、普通失败、报告文字或 reviewer 的拒绝本身不会重派 coder。

该路由只处理依赖结算后的 coder 恢复；reviewer 或其他 agent 要求修改上游时仍须显式调用 `request_rework`，不会被 coder revival 取代。

继承 harness 的 baseline 修复与验证由宿主绑定同一候选身份：当前 Git source HEAD 加上 `.modport/` 下非生成源码的路径和内容摘要。构建输出、Gradle 缓存及 harness 证据不参与身份；新增、暂存、重命名或删除的 harness 源码会改变身份。宿主在验证前后重新观察，只有身份相同才将结果归属该候选；未知 Git 状态、非 harness 的工作区改动（包含被 Git 忽略的非生成文件）、源码符号链接或超出交接大小限制时，身份保持未知。

### 规划与执行交接

主线为 inventory → migration_plan → 按依赖拆分的 goal_prepare/coder → 集成与构建/审查 →
独立测试设计/审查 → 宿主执行和验收。reviewer 等其他 agent 要求修改上游时需要显式 `request_rework`；
coder 的依赖结算处理遵循上面的 planner 决策流程。
planner 应写明文件与符号、问题原因、修改动作、验证方式，并关联 inventory issue ID；
遗漏项保留原因，不形成批准门槛。Markdown 报告中的唯一 JSON 任务块会进入正式任务解析，
任务 ID 和依赖边保留；多个不同任务块按歧义记录诊断，并保留原文供单任务执行。
下游同时收到原规划报告、输入索引和规范化计划的认证引用；缺失旧版分轮报告不阻断执行。
返工时计划、基线、任务与目标上下文统一绑定当前候选，原计划引用另存为历史证据。
独立测试的可用快照在业务覆盖诊断前封存，覆盖问题随快照传到执行器；路径、合同分类和候选身份
校验仍有效，测试执行失败或证据缺失保持 `acceptance_status: unverified`。

## 启动观察与有证据的技术恢复

新执行以有界进度记录区分 SDK dispatch、worker 入口、输入恢复、内存/工作区等待、业务 handler、模型调用和结算；
监控必须把 SDK lease/driver 健康与实际 handler/model 进展分别呈现。
资源准入按真实 stage 使用 worker 同一份轻/重型策略；`planned` 不占 worker 内存，已排队和运行的执行按其声明阶段保留预留。
内存与工作区等待有界并受当前 SDK/Run 截止裁剪，等待采样有上限，不关闭资源保护来换取派发。

每个 SDK task 最多允许一次启动前技术恢复，并且仅当公开 SDK 状态为 `timed_out`、匹配的 worker 阶段记录证明尚未进入 handler、
Effect 列表为空且无待恢复 Effect、部署/attempt/fence 身份相符、旧 worker PID/启动身份已确认退出、原 Run 截止仍有效时，
宿主才通过 SDK `new_attempt` 后 `dispatch` 同一任务。该动作不新增业务 assignment、不重置任务或 Run 截止；原超时及恢复依据均保留。
任何缺失/冲突/未知事实、handler 已进入、Effect 不确定、旧进程仍存活或恢复已用过一次，均进入 `recovery_required`，不自动重放。
该机制只处理执行前技术失败，不替代显式 `request_rework`，不触发重规划/同伴取消，也不宣称迁移验收通过。

## 返工资源等待

v34 交接保留原 Run 的来源测试文件和结果作为历史证据，但它们不进入新 Run 的执行合同，也不满足目标行为覆盖。
新 Run 重新从源代码和文档提取 `behavior_requirements`，再独立设计目标可执行用例及映射；目标结果必须新鲜执行。
handoff 不继承运行结果、审查决定或 SDK 历史。已有 source harness 只可作为被保存的历史文件，不生成、运行或修复它。

显式 `request_rework` 遇到内存或并发容量不足时，宿主通过 SDK `add_task` 持久化 planned 任务，暂不 dispatch。计划任务不占 SDK worker，不占运行中的内存租约；请求、任务身份、返工额度和原始截止一并保留。驱动持续观察容量，恢复后只 dispatch 同一个任务，不要求 agent 再发请求，不增加 attempt 或重复扣额度。多请求按宿主观察的到达顺序串行处理。

等待中的审查者仅让出逻辑 coder 名额，存活进程的内存仍须计入。等待与执行共同受原 Run、会话和工具响应截止约束；超时或明确的工具取消/关闭标记会通过 SDK 取消尚未执行的 planned 任务并保留原因。诊断放行模式下，调用者先结算本身不撤销已授权请求。资源等待不创建需要管理员解除的 Run wait，也不修改 SDK 数据库。

## 扫描优先与诊断覆盖

精确源/目标版本已给定时，`source → skill_resolve → mod_scan → codemod` 不等待背景说明、项目说明或完整 MDK 编译。缺少版本时先解析，禁止猜版本套规则。`build_prepare` 等待环境与机械迁移，`early_compile` 验证机械迁移到构建准备的候选身份链。

新增五条精确 import 映射，连同三条 EventBus 映射共八条；只改显式 import，不保证构造、泛型或事件语义。静态扫描只复用完整、摘要匹配的检查结果，命中与新执行分开；不缓存游戏验收或瞬态失败。

已有自定义构建仍不盲目覆盖。无法认证其目标配置时，可在独立、认证 MDK 骨架中编译常规 `src/main/java`，记录 `provisional_mdk_source_compile`。此探针不迁移自定义依赖、处理器、源集、资源或打包，不等于原项目构建通过；缺类也可能来自临时类路径。原候选不修改，源码及骨架摘要在执行后复核。

宿主索引已认证依赖种子中的 JVM 类、方法、字段和声明描述符；缺少种子、有界截断与多版本 JAR 均报告覆盖限制。这不是完整 Gradle 有效类路径或兼容证明。计划遗漏的 inventory issue 明确保留为未解决上下文，不新增自动任务、重规划或门禁。任务内相同工具输入/输出只记录重复诊断，不据此假定候选未变、取消或重启 goal。harness 的实际长运行前先在同一沙箱执行最长 120 秒的任务图短探针，探针失败明确记录完整 harness 未启动。

## 显式返工与诊断证据

当前工作流采用无业务门禁执行方式。只有 agent 的显式 `request_rework` 能派发上游
返工；扫描、路由建议和进展评估都不自动派发、重试、停止下游或授予验收。

- **可定位清单**：审查工具会话和宿主构建生成 `repair_inventory` 文件，记录稳定问题 ID、
  文件、行号、符号、原文片段和来源。扫描源码、资源、构建配置及编译日志，记录缺失输入。
  文件、字节和日志限额以及无法读取的内容明确记录；扫描零命中不等于迁移通过。
  清单模块也支持附加扫描规则，以及运行时、知识缺口和人工审查观察，统一保留来源。
- **任务路由**：`list_rework_targets` 暴露实际可调用作者的路径和依赖，并返回问题清单及
  路由文件位置。多作者匹配标明协调需求，未匹配问题明确留给审查者选择作者。审查者在
  返工意见中写问题 ID、具体位置、预期行为和验证步骤；跨模块问题按接口提供方、调用方、
  集成顺序拆分显式请求。“范围广”本身不是放弃返工的理由。
- **证据回传**：各下游阶段请求 coder 返工后，宿主继续执行对应的目标构建或合同验证。
  回传分别列出作者结果、构建状态、行为验证状态、验证执行 ID、候选版本和日志。
  只有宿主在执行前后观察到相同的干净 Git 候选时才绑定验证版本；否则明确未知。
  小型原始日志保留在执行专属目录，后一次构建覆盖固定日志名不会改变已回传证据。
  合同作者返工后执行新的合同验证并刷新合同观察引用；输入缺失时只保留失败观察，
  不发布空的合同锁引用，也不把旧验证报告当成本次结果。
- **进展评估**：比较同一运行、作者和范围的可比证据；提交号变化、文件变多、SDK 阶段
  成功均不算进展。实际检查失败转成功，或完整可比源码扫描确认旧引用消失，可成为诊断性进展。
  编译错误仅从失败日志中消失不算修复；日志记录编译器截断和实际到达的 Gradle 任务，
  执行范围未知时标为不完整。源码范围完整且可比时，可独立确认源码问题减少。
  缺失、过期或不完整证据不能证明对应问题关闭；无法判定根因相同时不宣称停滞。

完整清单留在 `artifacts/repair-diagnostics/<execution>/`，工具回传有界摘要和引用，
不在每次调度轮询中重新扫描或嵌入完整历史。缺少合同锁时应修复真实生产者证据或引用，
不得捏造空合同。所有这些诊断均标记 `acceptance_evidence: false`；执行完成仍可能为
`acceptance_status: unverified`，需要真实项目验收证据才能另行确认行为。

## 当前工作流的诊断式业务结果

当前默认流程不再用报告格式、任务路径归属、审查判决、自检、验证通过状态、
行为覆盖率或知识缺口阻止下一阶段。检查和独立审查继续执行，结果作为诊断传给下游；
不产生自动格式重试、作者返工、重新规划、同伴取消或强制 `gate_handoff`。
主动返工仍须由 agent 显式调用 `request_rework`。

规划为初稿 → 一次改进 → 任务组织 → 分发。缺少字段时宿主补齐执行标识和默认值，
保留原文；自由文本也能成为执行任务。合同修复允许修改项目和构建文件，任务无需
等待未来阶段的验收材料。重叠任务按依赖排序；已有部分 patch 可以进入集成。
修改过的 baseline 明确记录项目变更，不把它称为未修改的原版。

新段直接接续已结算阶段，失败结果保持失败。流程结束使用
`acceptance_status: unverified`，只表示执行结束，不代表迁移通过验收。
SDK 身份、租约、取消、截止时间、预算、内存限制、工作区路径安全和无凭据项目执行沙箱
仍是执行约束。其余章节描述当前执行策略；其中版本标注说明行为引入历史，不为已退役的旧 Run 提供恢复承诺。

### 两轮对话协议

新定义冻结 `agent_dialogue_policy: {version: 1, turns: [plan, execute]}`。
所有报告类 agent（包括规划、审查、测试设计、skill 生成，以及 coder 交接）
先收到只生成任务计划的消息，返回 Markdown；随后在同一个 OpenCode 会话中收到
执行消息。执行轮才提供固定 JSON Schema，或要求输出完整 Markdown 报告。
两轮不是两次独立分配，共用原分配的截止时间；不会因计划格式再增加一轮。

普通阶段使用受管理的 OpenCode 本地服务建立持久会话，两轮共用明确的会话 ID。
coder 由宿主管理持续目标，计划结束后才进入执行目标；服务重启和恢复依靠持久请求与结果身份对账。报告 JSON 的动态映射使用固定数组表示，由宿主确定性地还原为原消费格式；
skill 多文件结果由宿主按预先确定的文件列表保存。原始回复始终保留。

每次执行的 `artifacts/executions/<execution_id>/dialogue/` 保存计划、两份 prompt、
第二轮 Schema（若需要）、最终原文和会话信息；两轮日志分别保存。
执行上下文经过压缩时，第一轮规划任务说明提供仅含压缩历史片段的认证路径与 SHA；完整的当前任务、冻结约束仍由各轮正式输入提供。
规划轮将预期 SHA 交给只读 Run artifact 工具，由宿主校验完整文件，再按 UTF-8 字符边界读取该历史片段；仍只制定计划，不提前执行第二轮任务。
审查计划名为 `审阅计划.md`，其他计划名为 `任务计划.md`。
主机健康检查和内部 prompt 压缩不属于该两轮协议。

## 已验证依赖种子

宿主通过 `modport dependency-cache fetch` 显式取得并校验 Maven JAR/POM，记录精确坐标、
来源 URL、SHA-256 和大小。同坐标不可覆盖；普通依赖保留原始 POM，只有显式声明无传递依赖时
才生成最小 POM。429/503 下载重试有界，并遵守 `Retry-After`；不自动切换版本。

CLI 默认使用 output-root 同级的 dependency-cache，可显式指定或关闭。提交 Run 时冻结独立副本，
把 manifest 和 Gradle init 脚本绑定到 initial_refs；构建前验证摘要及仓库内容，只读挂载到沙箱。
所有 Gradle 门禁使用该种子，各阶段私有可写缓存继续隔离，不把运行生成物回写共享仓库。
本接口仅导入主 JAR/POM，不覆盖插件 DSL、classifier、wrapper/JDK 或 Minecraft 专用下载缓存。

明确的限流或网络传输失败以 `dependency_rate_limited` / `dependency_resolution_failed` 停止修复链，
保留失败证据；声明错误、缺失坐标或 variant 不匹配仍允许诊断修复。补齐依赖不会自动重启失败 Run。
仓库命中不等于真实迁移通过；仍需完成原有构建、测试和客户端验收。


## 审查会话内定向返工

审查 agent 通过 stdio MCP 的 `list_rework_targets` 查询当前上游作者，再调用
`request_rework(target_agent, instructions)` 要求指定作者修订。`instructions` 是原文上下文；
不要求报告字段、括号配对或 hash 回填。调用期间原审查 OpenCode 会话等待，宿主用公开 SDK 接口
派发作者任务，提交结果后把作者原文、产物路径和验证结果返回同一 tool call。审查者可以继续检查、
再次返工，最后给出审查决定。

返工读取原 goal 时使用当前子任务的 SDK 预算身份，保留原任务 ID、目标和产物来源；
原计划中的同级依赖只在新返工计划中清空，不用于重新解释原任务。读取或认证失败须原样返回。
coder 失败且没有集成候选变化时，直接返回失败，不重复构建未变化的候选；已集成的变化仍需验证。

审查发现具体缺陷需要上游修正时，应实际调用返工工具并读取返回结果，不能只在报告中写“请修复”。
两轮对话的第一轮只写计划，返工调用放在第二轮执行。没有可用目标时，提示词明确说明工具不可用，
并保留未解决的问题；不能声称已经发起或完成修复。

续接除了保留作者结果，还会继承相应的原始任务描述，供下游建立真实的 MCP 返工目标。
新续接可沿当前已封存目录及任务描述中的哈希引用恢复早期续接遗漏的作者；不会扫描历史目录猜测来源，
也不会改写旧目录。缺失、冲突或验证失败的描述保留为诊断，不作为可调用目标。

可选目标来自该审查实际收到的上游任务，包括规划作者、契约作者、对应范围的 coder、研究作者、
skill 作者及测试设计作者。每个作者的连续返工受 `max_rework_rounds` 限制，同时计入总 assignment、
时间及适用的研究预算。返工串行派发；审查等待不会占住作者的 workspace 锁。
等待中的审查者可把自己的 coder 并发名额借给被请求的作者，但仍持有实际内存租约；
准入必须同时计算这两名存活进程的内存需求。容量不足时保留 SDK planned 任务等待。
准入后若其他进程抢占空闲内存，执行器对嵌套任务最多等待 120 秒，仍受工具与 Run 时限约束。
单次 `request_rework` 最多等待 20 分钟。当前交互式审查任务可在共享 Run
截止前等待其作者及后续验证，不再受普通 agent 的两小时窗口截断；工具响应仍须在审查会话
截止前留出结算时间，作者和验证也受各自 SDK 执行窗口及同一 Run 截止约束。逾时写入取消标记并向
调用者返回明确错误。迟到的作者结果保留在宿主证据中，不会让同一次工具调用再次成功。

coder 返工从当前已合入版本启动独立的宿主目标与 OpenCode 会话，保留原任务边界，只合入本次修改差异。
来源行为要求只读提取和审查，不是运行或修复原版测试的返工对象。目标代码或目标测试合同的显式返工后，按范围重新构建并验证。
测试设计返工更新等待中审查者的 suite，后续执行使用这次修订。取消工具调用不能抹除已经完成的作者结果；
请求、子任务和返回内容均保留在 Run 的 SDK 记录与 `artifacts/rework-tools/` 中。

## 应用状态的有界持久化

Dispatcher SDK 会把每次改变的 `application_state` 整体写入历史和决定事件。ModPort 在调用
SDK 公共写接口前，先把达到 64 KiB 的顶层字段保存为按 SHA-256 寻址的确定性 gzip 工件，
SDK 只保存严格校验的引用；多个状态中内容相同的字段复用同一个工件。紧凑后的根对象若仍达到
64 KiB，也使用相同机制外置。读取、恢复、续接及 Web 投影在使用状态前验证大小、摘要、路径和
gzip 内容并还原完整对象；缺失、损坏、symlink 或超过 128 MiB 的状态会明确失败。

历史 inline 状态的保存格式不构成旧 Run 的续接支持；受支持的续接使用当前存储路径。该表示只改变
ModPort 自有持久化封装，不删除结果、诊断、返工记录或证据，也不直接修改 SDK 数据库。
历史 revision 仍引用对应工件，因此备份和归档必须同时包含 SQLite 数据库与 `audit-blobs/`；
这些工件的保留期与整个 Run 相同，不能只按当前状态做垃圾回收。

## Run 保留与冷归档

运行目录不做按名称或日期的自动删除。清理前先建立 `run_id`、`parent_run_id`、continuation
来源和恢复记录的引用图；活动 Run、当前返工链、被保留 Run 引用的祖先以及唯一验收证据必须保留。
仍被热存 Run 引用的祖先可以迁入冷归档，但引用图必须登记可解析的 archive locator；这属于
存储层迁移，不是证据销毁。只有已经被较新链替代、不再可达且确认终止的 Run 才能永久销毁。

清理采用 archive-first：把计划中的精确目录列表写入清单，在独立文件系统生成压缩归档，完成
压缩流测试、成员列表核对、SHA-256 和字节数记录后，才删除工作盘上的相同目录。恢复同样写入
临时文件，完整校验后原子发布；失败或取消必须删除临时文件。协议或返工工具测试使用小型 fixture，
不能为了测试而恢复大型历史数据库。

归档清单绑定精确路径列表、每个 Run 的 ID/header/父边、完整成员列表摘要和所有跨冷热层引用。
恢复器只接受清单中的单个 Run，拒绝绝对路径、`..`、额外根目录、链接和特殊文件；目标必须不存在，
空间必须足够，发布前对临时副本执行 `sdk-inspect`，随后 fsync 并原子改名。目录仍在但 SDK 主库
已经冷归档的 Run 必须标为不可续跑，不能只根据目录存在就报告为活动或已恢复。

宿主每次新建、续接或恢复前记录 Run 目录、SDK 数据库、外部状态 blob、审计报告和剩余空间的
字节数。异常增长先暂停新的历史写入并生成诊断，不能通过直接删除 SDK 表解决。SDK 未提供公开
的历史裁剪和 Run 销毁接口；所需接口和验收标准见 `docs/SDK_STORAGE_RETENTION_PROPOSAL.md`。


## 标准存储维护

命令和结果也使用 ModPort 的内容寻址存储：超过 64 KiB 的 JSON 子树自底向上拆分，重复的上游
结果、返工原始命令跨任务复用同一 blob。SDK 持久化小型身份字段和引用；业务处理器及决策读取前
校验并还原。小命令保持 inline；历史 inline 命令作为证据保留，不承诺旧 Run 可恢复。执行收据保留完整业务结果，Effect 返回值和
最终执行结果使用紧凑表示；恢复、续接准备包必须先验证引用再提交 SDK。单个逻辑输入/结果上限
128 MiB，不允许通过无限递归引用绕过。

物理资源检查独立于 workflow 的业务诊断策略，在打开 SDK writer 前、调度提交及处理器执行边界
运行。默认两份 SDK 数据库及 WAL/journal 合计预算 512 MiB，提前保留 64 MiB；整个 Run 在新建、
执行/续跑、续接、恢复、维护边界统计，软预算 20 GiB；文件系统至少保留 2 GiB 空闲。超限拒绝新写入，原始
失败证据不删除，诊断覆盖写入 `artifacts/storage/status.json`。这是宿主检查点限制，SDK 0.6 没有
事务级磁盘配额；并发或单次事务仍可能越过检查点阈值，不能宣称严格的逐事务硬限额。

宿主配置（正整数 MiB）：`MODPORT_SDK_STORAGE_LIMIT_MIB`、`MODPORT_SDK_STORAGE_SAFETY_MIB`、
`MODPORT_RUN_STORAGE_LIMIT_MIB`、`MODPORT_STORAGE_MIN_FREE_MIB`。安全余量不能低于 64 MiB，
空闲保留不能低于 2 GiB；禁止负数或 `None` 取消限制。

执行到终态、完成续接时自动记录 SDK 已结算段并运行保留检查。按冻结 `previous_run_id` 链保留
当前段及前两段的完整明细；更早的已结算段仅归档 `prepared.json`、`rework-sources.json`，以及已
校验的非当前审计报告代。段名或日期不能替代 SDK 结算证据。冻结头、来源代码、工作区、唯一验收
证据、`audit-blobs/` 和 SDK 数据库不属于自动删除范围。

归档根默认是当前用户 ModPort 数据目录下的 `archives/`，可用 `MODPORT_ARCHIVE_ROOT` 配置，必须在独立
文件系统。归档失败保留原件并记录 `artifacts/storage/retention-status.json`。gzip 归档经过流式
SHA-256/解压校验、fsync 和索引发布后才释放原件；旧引用由
`artifacts/storage/retention-index.json` 定位。执行明确需要旧文件时校验恢复；只读观察不得恢复
或复制冷文件。没有可信结算记录的历史段保留，并列出阻塞原因。

手动维护也使用相同实现，默认只读计划，不读取或复制 SDK 数据库：

```sh
modport storage-maintain --run-dir /absolute/run
modport storage-maintain --run-dir /absolute/run --apply --archive-root /other-disk/archive
```

既有庞大 SQL 不会因产物归档而缩小。SDK 未提供公开裁剪/压缩接口；超限的 Run 暂停新增
调度写入，不能直接删表、用旧备份替代当前原件或跳过身份/完整性校验继续运行。

### 验证超时与独立监控

标准 Java characterization harness 放在 `.modport/harness` 时，宿主在缺少作者提供的
`.modport/characterization.init.gradle` 时生成默认接线：加入 Java 主源码集，为
`runClient` / `runServer` 打开 `modport.characterization`，设置项目根和隔离运行目录。
已有初始化脚本保留原样。对同时存在的标准 `.modport/harness` Java 源码追加独立
`characterization-sources.init.gradle`，在宿主 `artifacts/harness-wiring` 目录保存并只读挂载，
只补充源码注册，不污染作者工作区或覆盖启动设置。
根项目不应用 Java 插件时，根目录的标准 harness 也会注册到拥有 runClient/runServer 的 Java 子项目。
自定义启动器仍需作者接线。任务图 dry-run 仅证明任务可解析，不证明测试类编译或入口已执行。
所有实际使用的接线脚本均纳入阶段证据；编译和真实
行为验证仍须在无凭据沙箱内运行，不能用接线成功代替验收。

宿主把内部工作时间限制在 SDK 外层超时以内，预留最多 60 秒（短任务为 20%）用于
失败回执和结果结算，不改写冻结命令。进程崩溃等仍可能需要恢复，不保证所有中断都有回执。

生产执行与续跑自动启动独立只读监控，结果位于 `artifacts/monitor/`。默认每 10 秒进行轻量
SDK 状态和驱动健康检查，用于及时发现终态、心跳失效和驱动退出；恢复等待期间也不降低此
检测频率。完整进展扫描只在首次建立基线、健康异常、SDK 恢复等待/空转、终态或明确诊断
请求时触发。需要固定诊断间隔时可显式传入 `--diagnostic-interval`（例如 1800 秒）；生产
自动启动默认关闭，也可通过 `MODPORT_MONITOR_DIAGNOSTIC_INTERVAL=1800` 显式开启。驱动退出不会终止监控，恢复驱动可重新绑定。监控状态和事件有容量上限，
不保存完整调度快照，也不自行重试或替代代理的返工决定。

历史 Run 中断的原版 `contract_verify` 证据保持冻结，不转成 v34 的来源运行义务。v34 的来源分析只读代码和文档；
被中断的目标测试则按其真实目标执行身份和公开 SDK 恢复路径处理。恢复不得改写历史证据、冻结输入或增加任务额度。


### 有界开发续跑与产物结算

`modport continue-progress --run-dir /absolute/run --run-id OLD --next-run-id NEW --reason REASON`
通过公开 SDK 续接终态 Run，默认提供 8 小时执行窗口；到期后仅在宿主记录到产品源码、构建或资源
内容变化、问题关闭或真实验证进展时，再续接一次，累计上限 12 小时。单纯 revision 增长、提交数、
报告改写和重复观察不构成延期依据。策略、观察基线、延期决定和 SDK 身份记录保存在
`artifacts/progress-continuations/NEW/`，重启不得重置额度或重复延期。
若必须替换尚未派发的续段，宿主先通过公开 SDK 原子取消其计划任务并结束旧续段，再发布
新续段；已产生全局派发意图的续段必须先独立结算，不能仅因应用状态显示“未启动”就替换。

预算中断时保留已完成的目标准备与已有补丁。已停止但未导出补丁的 coder 会先核对 SDK
终态、原工作区、模型进程及起始 Git 树，再由宿主将安全改动冻结成独立补丁；新分段工作区先
应用依赖，再应用该部分补丁。返工失败覆盖早期作者结果时，也校验并接回早期补丁供该作者继续。
此前明确发出的返工指令及具体文件位置随续段传给原作者，避免仅重做旧任务却漏掉实际缺陷。
若同一段里原作者先成功而后续显式返工失败，预算续接不能再以原成功 SDK attempt
认定返工已完成；先校验本段 SDK 补丁，保留补丁并重新排队该作者处理原指令。
补丁引用及来源写入不可变续段输入，冲突须明确报错，遗留工作区和旧回执保留为证据。
调度采用实际 DAG 的任务定义，按补丁 metadata.task_id 关联依赖，继续
校验哈希、base 和 generation；旧计划顺序不能导致上游补丁漏用。已有部分补丁保留原失败状态，
后续审查仍可通过明确 request_rework 发起返工。

产物捕获批量哈希、批量更新 Git 索引。后续结算可复用候选，但必须重新安全扫描并核对每个文件的
内容、模式和身份；发现变化立即重新捕获。同步 request_rework 时，等待的父任务释放逻辑执行槽，
真实内存准入仍然生效。coder 模型时限提前 180 秒结束，给宿主采集、导出和 SDK 回执留出时间；
在 Run 时限仍有效且部分补丁已封存时，单个 coder 的局部超时保留为诊断，其他任务继续。
这些措施不修改历史 SDK 数据库、冻结输入或验收状态。

进展诊断记录问题清单变化、宿主候选内容差异、验证和依赖失败，以及无实质进展持续时间；
固定 30 分钟诊断只是可选的诊断模式。轻量进程/SDK 状态检查和进展诊断都不能替代调度，
进程存活和 revision 增长也不能用作完成证明。该监控是宿主进程，独立于外部助手应用的定时唤醒。

## 已交付 JAR 的测试重跑

`run --verify-artifact --handoff PACKAGE` 创建独立验证 Run，按当前流程只读提取来源行为需求，独立设计目标可执行合同，
再执行冻结的目标测试。handoff 必须明确选择同一候选的 `target-package-receipt.json` 和该回执认证的 JAR，
同时保留锁定环境和版本元数据。此入口不派发迁移实现或目标构建；原 Run 的输入和证据保持冻结。

目标验证只读挂载交付 JAR，并仅运行独立 harness，不编译或修改产品源码、不生成新 JAR。目标 case ID 与来源行为
requirement ID 建立映射，但不要求沿用任何 v33 来源 test ID。测试未执行、跳过、覆盖缺口和失败都保留为未验证，
报告位于 `artifacts/artifact-verification-report.json`，不以流程结算代替行为验收。共享 GameTest runner 启动失败
只记录一次；未能运行的其余 case 保持未执行。

目标测试作者使用 `modport_artifact_compile_artifact_harness` 在无凭据沙箱中编译目标探针和
JUnit 源文件，原始编译错误直接返回作者。编译是诊断，不是审批或行为验收，也不会自动重试或
启动游戏。作者在目标 harness 的 init 配置中声明测试依赖和执行引擎；若 JUnit 测试
启动子 Gradle，必须传入宿主环境变量 `MODPORT_ARTIFACT_INIT_SCRIPT` 对应的 init script，
让子进程继续使用交付二进制。工具调用的时限包括 Wrapper 准备时间，并受原 SDK 和作者时限约束。
宿主在目标验证前提供本次冻结的目标合同和 requirement-to-case 映射，并隔离目标测试输入；
目标测试作者保持冻结行为断言，来源 harness 声明不作为目标通过依据。

artifact-only handoff 的修复导航在诊断模式分支前加入作者指令，正常、返工和恢复路径均
读取明确选中的修复 README 和源码。目标修复只用于目标版本；保留本次冻结的行为要求和映射，
补齐缺失适配而不要求复用来源测试身份。旧结果仍是历史证据。

## v35 目标测试接口修复

目标冻结结果的宿主封装仅存于独立的 `target-contract-lock.json`。harness 使用的
`.modport/functional-contract.json` 始终保持作者声明的顶层 `behaviors`、`test_evidence`，
不会在冻结后改变 JSON 层级。主机按选中的 GameTest 任务统一配置官方 XML reporter，
输出和读取均使用 `build/test-results/<selected-task>/`，任务别名也遵守相同绑定。
共享 runner 负责启动、结果归集和客户端会话复用；版本适配器负责游戏 API，单独用例
负责 MOD 的行为断言。某个锁定版本通过不代表其他版本已经完成运行验收。

失败的 artifact verification 可显式从 `target_contract_freeze` 重新执行主机修复后的
契约和目标验证，或从 `artifact_test_design` 修复 harness；只废弃所选目标阶段之后的
当前结果引用，历史执行和失败反馈保留，来源行为需求不重做。增加返工余量使用
`continue --additional-agent-assignments 50`；保留累计使用次数及原截止时间。

## v37 最终 cleanup 与 SDK 0.7.1 集成

完整迁移在全部冻结目标用例执行通过且独立审查结束后，执行一次 `final_cleanup`。
持久化 cleanup 集成检查点后，重新 `target_build → code_review → acceptance_preflight → acceptance_build → client_smoke → gap_review → delivery`；
这次验证执行全部冻结目标用例，不重设计合同，不删除断言，不复用 cleanup 前结果。
审查意见保持诊断性质，交付必须包含实际非空 JAR。artifact-only 验证不修改交付产品。

所有 coder、repair、coder rework 与 native goal 后续 turn 注入包内 `code-simplifier` 的可读性和项目规范要求，
保留行为、公开接口、冻结合同和断言。压缩后的 turn 仍携带要求，并通过 host artifact 工具读取 skill。

部署依赖 `dispatcher-sdk==0.7.1`，Kernel schema 5、Orchestrator schema 4、protocol 2。
执行预算使用公共 `HandlerContext.budget` 的观察时间与截止时间，不从 driver lease 推算。
该版本已由上游正式发布。Linux 下有限范围检查已通过，包括 SDK 导入与 `pip check`、14 项 SDK 兼容性测试，以及重新打开执行与 ACK/围栏、提交间隙回执恢复和进程监督器取消/恢复路径检查。完整真实 Hyperbox 与原生 Windows 验收仍为 `unverified`；这些检查不代表完整迁移验收通过。
