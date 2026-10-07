"""Request-local translations for desktop-owned display text only.

Catalog keys are stable original messages; user/model text and diagnostic details
must never be passed through a recursive translation of an API response.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import re

# Direct application callers retain their existing presentation. HTTP requests
# always enter language_scope, including the English fallback for missing headers.
_language = ContextVar('modport_desktop_language', default='zh-CN')


def normalize_language(value):
    return 'zh-CN' if isinstance(value, str) and re.match(r'^zh(?:[-_]|$)', value, re.I) else 'en'


def request_language(header):
    choices = []
    if isinstance(header, str):
        for index, part in enumerate(header[:1024].split(',')):
            fields = part.strip().split(';')
            locale = fields[0].strip()
            quality = 1.0
            try:
                for field in fields[1:]:
                    if field.strip().startswith('q='):
                        quality = float(field.strip()[2:])
            except ValueError:
                continue
            if not 0 < quality <= 1 or not re.fullmatch(r'[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*', locale):
                continue
            # Unsupported primary preferences fall back to English.
            choices.append((quality, -index, normalize_language(locale)))
    return max(choices)[2] if choices else 'en'


@contextmanager
def language_scope(locale):
    token = _language.set(normalize_language(locale))
    try:
        yield _language.get()
    finally:
        _language.reset(token)


# Explicit desktop strings; unknown messages pass through unchanged.
MESSAGES = {
    '子任务': 'Subagents',
    '请提供有效的 GitHub 或 Gitee HTTPS 仓库地址。': 'Provide a valid GitHub or Gitee HTTPS repository URL.',
    '仅支持不含凭据、端口或参数的 GitHub / Gitee HTTPS 仓库地址。': 'Only GitHub or Gitee HTTPS repository URLs without credentials, ports or parameters are supported.',
    '仓库地址应为 https://github.com/owner/repository 或 Gitee 同类地址。': 'Use https://github.com/owner/repository or the equivalent Gitee URL.',
    '仓库路径不正确。': 'The repository path is invalid.',
    '分支或标签名称不正确。': 'The branch or tag name is invalid.',
    '请提供分支或标签名称，不使用 Git 版本表达式。': 'Provide a branch or tag name without Git revision expressions.',
    '无法读取远程仓库：{detail}': 'Could not read the remote repository: {detail}',
    '仓库引用列表超出读取上限，请指定较小的仓库或手动填写版本。': 'The repository reference list exceeds the read limit. Select a smaller repository or enter versions manually.',
    '未读到仓库默认分支，请明确选择分支或标签。': 'The default branch could not be read. Select a branch or tag.',
    '分支或标签较多，仅展示前 1000 项；仍可直接输入名称。': 'Only the first 1,000 branches and tags are shown. You can still enter a name directly.',
    '未读取到构建版本声明。仓库可能需要认证，或使用其他目录结构；请手动确认版本。': 'Build version declarations could not be read. The repository may require authentication or use another layout. Confirm the versions manually.',
    '未能可靠识别源 Minecraft 版本，请手动填写。': 'The source Minecraft version could not be reliably detected. Enter it manually.',
    '安装 Git 并加入 PATH；仓库识别只读取代码声明。': 'Install Git and add it to PATH. Repository inspection only reads code declarations.',
    '需要 OpenCode 1.18.32，并在本机配置模型提供方凭据。': 'OpenCode 1.18.32 and locally configured model provider credentials are required.',
    '安装迁移版本需要的 JDK；具体版本在实例环境准备中解析。': 'Install the JDK required for the migration versions. The exact version is resolved during instance preparation.',
    '{path} · {version} · 需要 {required}': '{path} · {version} · requires {required}',
    '版本未知': 'version unknown',
    '项目代码隔离执行': 'Isolated project execution',
    '需要 bubblewrap；项目代码只能在无凭据沙箱中执行。': 'bubblewrap is required. Project code runs only in a sandbox without credentials.',
    'Windows AppContainer 沙箱': 'Windows AppContainer sandbox',
    '执行前会验证原生隔离和清理能力；Windows 真实主机验收尚未完成。': 'Native isolation and cleanup are checked before execution. Real Windows host acceptance is pending.',
    '持久执行监督器': 'Persistent execution supervisor',
    '独立实例目录': 'Separate instance directories',
    '准备工作目录': 'Prepare workspace',
    '默认任务': 'Default tasks', '规划': 'Planning', '代码编写': 'Coding', '监督': 'Supervision', '需求审查': 'Requirement review', '上下文整理': 'Context summarization',
    '不支持的项目参数：{fields}': 'Unsupported project parameters: {fields}',
    '项目名称须包含 1–120 个字符。': 'The project name must contain 1–120 characters.',
    '请选择远程仓库或本地代码库。': 'Select a remote repository or local source folder.',
    '本地模式使用所选目录的当前文件，不接受远程地址或分支。': 'Local mode uses the selected folder’s current files and does not accept a remote URL or revision.',
    '请通过“选择文件夹”重新选择本地代码库。': 'Use “Choose folder” to select the local source folder again.',
    '请选择新分支、复制工作区或直接开发。': 'Select a new branch, copied workspace or direct development.',
    '只有新建 Git 分支模式接受分支名称。': 'A branch name is only accepted when creating a new Git branch.',
    '请确认直接修改所选目录，不新建分支、不复制备份。': 'Confirm direct changes to the selected folder without a new branch or backup copy.',
    '直接开发确认只能用于直接开发模式。': 'Direct development confirmation is only valid for direct development mode.',
    '远程模式不接受本地目录参数。': 'Remote mode does not accept local folder parameters.',
    '{field} 必须是正整数。': '{field} must be a positive integer.',
    '并行代码任务数须为 1–16。': 'The number of parallel coding tasks must be 1–16.',
    '加载器须为 Forge、NeoForge 或 Fabric。': 'The loader must be Forge, NeoForge or Fabric.',
    '运行环境尚未就绪，请先查看运行环境。': 'The runtime environment is not ready. Check the environment first.',
    '实例 {instance_id} 已保存，但持久驱动未能启动：{detail}': 'Instance {instance_id} was saved, but its persistent driver could not start: {detail}',
    '不支持此环境准备操作。': 'This environment preparation action is not supported.',
    '工作目录不能是符号链接。': 'The workspace directory must not be a symbolic link.',
    '工作目录已准备。': 'The workspace is ready.', '环境检查已完成。': 'The environment check is complete.',
    '请求必须是 JSON 对象。': 'The request must be a JSON object.',
    '不支持的本地目录参数。': 'Unsupported local folder parameters.',
    '请求来源不正确。': 'The request origin is invalid.', '请求须使用 JSON。': 'The request must use JSON.',
    '请求大小不正确。': 'The request size is invalid.', '实例或接口不存在。': 'The instance or endpoint does not exist.',
    '本机服务无法完成此请求，请查看实例诊断。': 'The local service could not complete this request. Check the instance diagnostics.',
    'Application authentication required': 'Application authentication required',
}

# Translations for fixed English service and supervisor validation messages.
CHINESE_MESSAGES = {
    'Application authentication required': '需要应用身份认证',
    'Unsupported application request': '不支持的应用请求',
    'Unsupported repository parameters': '不支持的仓库参数',
    'Unknown application endpoint': '应用接口不存在',
    'Unsupported lifecycle request': '不支持的实例操作',
    'Incomplete request body': '请求正文不完整',
    'JSON numbers must be finite': 'JSON 数值必须有限',
    'Application response exceeds its limit': '应用响应超出大小上限',
    'Repository metadata redirects are not followed': '不允许仓库元数据请求重定向',
    'Repository metadata file exceeds the read limit': '仓库元数据文件超出读取上限',
    'systemd is required for a durable migration driver': '持久迁移驱动需要 systemd',
    'systemd persistent service supervision is available': 'systemd 持久服务监督可用',
    'Windows Task Scheduler is unavailable': 'Windows 任务计划程序不可用',
    'Windows Task Scheduler is available; native acceptance pending': 'Windows 任务计划程序可用；原生验收尚未完成',
    'This desktop package supports Windows and Linux supervision': '此桌面包支持 Windows 和 Linux 的持久监督',
}


def t(message, **values):
    catalog = MESSAGES if _language.get() == 'en' else CHINESE_MESSAGES
    return catalog.get(message, message).format_map(values) if values else catalog.get(message, message)


STAGE_LABELS = {
    'source': '源码读取', 'background': '背景研究', 'preparation': '环境准备', 'project_init': '项目初始化',
    'environment': '环境解析', 'baseline_build': '基线构建', 'skill_lookup': '查找迁移规则', 'skill_publish': '发布迁移规则',
    'skill_resolve': '解析迁移规则', 'mod_scan': '模组扫描', 'mod_analysis': '模组分析', 'codemod': '应用迁移规则',
    'contract_draft': '契约草案', 'contract_verify': '契约验证', 'contract_review': '契约审查', 'contract_freeze': '冻结契约',
    'migration_inventory': '迁移清单', 'migration_plan': '迁移规划', 'migration_tasks': '迁移任务', 'parallel_review': '并行任务审查',
    'implementation': '代码实现', 'development_integrate': '集成开发结果', 'target_build': '目标构建', 'code_review': '代码审查',
    'test_design': '测试设计', 'test_review': '测试审查', 'test_execute': '执行测试', 'acceptance_preflight': '验收准备',
    'acceptance_build': '验收构建', 'client_smoke': '客户端启动检查', 'gap_review': '差异审查', 'delivery': '交付',
    'platform_diff': '平台差异研究', 'java_diff': 'Java 差异研究', 'platform_skill_review': '平台规则审查', 'java_skill_review': 'Java 规则审查',
    'gap_research': '差异研究', 'coder': '代码编写', 'agent_rework': '代理返工', 'gate_handoff': '任务交接', 'goal_prepare': '任务目标准备',
    'development_prepare': '开发准备', 'development_prepare_integrate': '集成开发准备', 'contract_repair_integrate': '集成契约修复',
    'target_repair_integrate': '集成目标修复', 'contract_restore': '恢复契约', 'contract_revise': '修订契约', 'target_revise': '修订目标',
    'gap_plan': '差异规划', 'gap_plan_review': '差异规划审查', 'research_review': '研究审查', 'admin_review': '管理审查',
    'knowledge_publish': '发布迁移知识', 'supervisor': '监督', 'coder_revival_plan': '代码任务恢复规划',
    'behavior_extract': '提取行为需求', 'behavior_review': '行为需求审查', 'behavior_freeze': '冻结行为需求',
    'research_cleanup': '研究导航整理', 'early_compile': '早期编译诊断', 'code_cleanup': '代码整理',
    'target_contract_design': '目标契约设计', 'target_contract_review': '目标契约审查', 'target_contract_freeze': '冻结目标契约',
    'artifact_verification': '产物行为验证', 'summary': '上下文整理',
    'artifact_test_design': '产物测试设计', 'artifact_test_execute': '执行产物测试',
    'artifact_test_report': '产物测试报告', 'build_prepare': '构建准备', 'final_cleanup': '最终整理',
}
for _scope, _name in [('contract', '契约'), ('target', '目标')]:
    for _suffix, _label in [('diagnose', '诊断'), ('repair_plan', '修复规划'), ('repair_tasks', '修复任务'), ('repair_review', '修复审查')]:
        STAGE_LABELS[_scope + '_' + _suffix] = _name + _label


def stage_label(stage):
    return STAGE_LABELS.get(stage, stage.replace('_', ' ')) if _language.get() == 'zh-CN' else stage.replace('_', ' ')


def localize_run_display(value):
    """Copy only framework labels; authored titles/details and chats stay raw."""
    result = dict(value)
    result['stages'] = {
        key: {**group, 'label': ({'preparation': '准备', 'implementation': '开发', 'testing': '测试'}.get(key, group.get('label', key))
                               if _language.get() == 'zh-CN' else {'preparation': 'Preparation', 'implementation': 'Implementation', 'testing': 'Testing'}.get(key, group.get('label', key)))}
        for key, group in value.get('stages', {}).items()
    }
    for group in result['stages'].values():
        group['items'] = [
            {**item, 'label': stage_label(item['stage_id'])}
            if item.get('label_is_stage') is True and item.get('stage_id') in STAGE_LABELS
            else item
            for item in group.get('items', [])
        ]
    # Raw launch diagnostics may contain newlines and even text equal to a
    # catalog key. Preserve that block as one template argument; only the
    # separately attributed framework/source messages can be translated.
    parts = result.pop('_desktop_notice_parts', None)
    if isinstance(parts, list):
        notices = []
        for part in parts:
            text = str(part['text'])
            if part['kind'] == 'launch_error' and text.startswith('持久监督器启动失败：'):
                text = t('持久监督器启动失败：{detail}', detail=text.removeprefix('持久监督器启动失败：'))
            elif part['kind'] == 'framework':
                text = owned_display(text)
            elif part['kind'] == 'source':
                text = '\n'.join(owned_display(line) for line in text.split('\n'))
            notices.append(text)
        result['notice'] = '\n'.join(notices) or None
    elif isinstance(value.get('notice'), str):
        result['notice'] = owned_display(value['notice'])
    return result

MESSAGES.update({
    '请选择有效的本地源码目录。': 'Select a valid local source folder.',
    '本地源码目录必须是绝对路径。': 'The local source folder must use an absolute path.',
    '本地源码路径必须是目录。': 'The local source path must be a directory.',
    '本地源码路径不能包含符号链接或目录联接。': 'The local source path must not contain symbolic links or directory junctions.',
    '源码目录不能与 ModPort 应用数据目录重叠。': 'The source folder must not overlap the ModPort application data directory.',
    '请选择具体项目目录，不要选择磁盘根目录、用户目录或系统目录。': 'Select a project folder instead of a disk root, home or system directory.',
    '启动时将保存当前文件的独立快照；实际工作目录由所选迁移模式决定，不会执行源码中的脚本。': 'Startup saves a separate snapshot of the current files. The selected migration mode determines the workspace; source scripts are not executed during preparation.',
    '未找到 Git；安装 Git 后可创建源码快照和独立工作区。': 'Git was not found. Install Git to create source snapshots and separate workspaces.',
    '所选目录的 Git 元数据无效；请修复仓库或选择复制/直接模式。': 'The selected folder has invalid Git metadata. Repair the repository or select copy/direct mode.',
    '所选目录不是 Git 仓库，可选择复制或直接模式。': 'The selected folder is not a Git repository. Select copy or direct mode.',
    'Git 仓库尚无提交；请先提交源码，或选择复制/直接模式。': 'The Git repository has no commits. Commit the source first or select copy/direct mode.',
    '项目包含嵌套 Git 仓库或子模块；请整理为单一源码目录，或选择复制/直接模式。': 'The project contains nested Git repositories or submodules. Use a single source folder or select copy/direct mode.',
    'Git 工作区包含未提交修改或未跟踪文件；请先保存并提交，或选择复制/直接模式。': 'The Git workspace has uncommitted changes or untracked files. Save and commit them first, or select copy/direct mode.',
    '可创建新分支和独立 Git 工作区。': 'A new branch and separate Git workspace can be created.',
    '已保存当前文件的独立源码快照，原目录及其 Git 历史未被修改。': 'A separate snapshot of the current source files was saved. The original folder and its Git history were not changed.',
    '仅按明确文件名排除缓存、凭据和应用状态；没有检测其余文件是否含有敏感信息。': 'Caches, credentials and application state were excluded by explicit file names. Other files were not checked for sensitive content.',
    '直接修改模式将在迁移期间修改原目录文件；源码快照独立保留。': 'Direct edit mode changes the original folder during migration. The source snapshot is retained separately.',
    '已基于原仓库当前提交创建独立 Git 工作区和新分支，仅包含受版本控制的源码；原目录文件、当前分支和索引保持不变。': 'A separate Git workspace and new branch were created from the original repository’s current commit, containing only tracked source files. The original files, current branch and index are unchanged.',
    '迁移将在独立复制目录中进行；原目录保持不变。': 'Migration runs in a separate copied folder. The original folder is unchanged.',
    '创建本地源码快照需要已安装的 Git。': 'Git must be installed to create a local source snapshot.',
    '本地源码 Git 操作超过时间上限。': 'The local source Git operation timed out.',
    '本地源码 Git 操作超过时间上限或输出未能完整收集。': 'The local source Git operation timed out or its output could not be fully collected.',
    '本地源码目录层级超过检查上限。': 'The local source folder exceeds the inspection depth limit.',
    '本地源码目录超过 Git 检查上限。': 'The local source folder exceeds the Git inspection limit.',
    '源码目录的 .git 不是有效的目录或文件。': 'The source folder’s .git entry is not a valid directory or file.',
    '本地源码目录层级超过快照上限。': 'The local source folder exceeds the snapshot depth limit.',
    '本地源码快照超过时间上限，请减少不必要的大文件。': 'The local source snapshot timed out. Remove unnecessary large files.',
    '本地源码目录条目超过快照上限。': 'The local source folder exceeds the snapshot entry limit.',
    '源码文件名不能包含控制字符。': 'Source file names must not contain control characters.',
    '本地源码文件数超过快照上限。': 'The local source file count exceeds the snapshot limit.',
    '本地源码文件大小超过快照上限。': 'The local source file size exceeds the snapshot limit.',
    '本地源码快照超过时间上限。': 'The local source snapshot timed out.',
    '源码文件在复制期间发生变化，请保存文件后重新启动。': 'Source files changed while copying. Save them and start again.',
    '源码目录包含符号链接、目录联接或非常规文件，请移除后重试。': 'The source folder contains symbolic links, directory junctions or special files. Remove them and retry.',
    '源码目录中没有可用于快照的文件。': 'The source folder contains no files for a snapshot.',
    'Git 仓库对象格式不受支持。': 'The Git repository object format is unsupported.',
    '原仓库提交在准备期间发生变化，请重新启动。': 'The original repository commit changed during preparation. Start again.',
    'Git 未返回有效的源码提交标识。': 'Git did not return a valid source commit ID.',
    '源码快照的 Git 历史与父提交必须同时提供。': 'Source snapshot Git history and parent commit must be supplied together.',
    '源码快照只能保留所选原仓库的有效父提交。': 'A source snapshot can only retain a valid parent commit from the selected repository.',
    '无效的本地源码快照标识。': 'Invalid local source snapshot ID.',
    '该源码快照已经存在，不能覆盖或重新复制原目录。': 'This source snapshot already exists and cannot be overwritten or copied again.',
    '请选择独立 Git 工作区、复制目录或直接修改模式。': 'Select a separate Git workspace, copied folder or direct edit mode.',
    '只有独立 Git 工作区模式可以指定新分支。': 'A new branch can only be specified for separate Git workspace mode.',
    '请输入有效的新 Git 分支名称。': 'Enter a valid new Git branch name.',
    '迁移工作目录不能与 ModPort 应用数据目录重叠。': 'The migration workspace must not overlap the ModPort application data directory.',
    '迁移工作目录已经存在；不会覆盖，请重新选择。': 'The migration workspace already exists and will not be overwritten. Select another one.',
    '请输入明确的新分支名称，不支持分支切换表达式。': 'Enter an explicit new branch name without branch switching expressions.',
    '该 Git 分支已经存在；请输入尚未使用的新分支名称。': 'This Git branch already exists. Enter an unused branch name.',
    '无法检查新 Git 分支是否可用。': 'Could not check whether the new Git branch is available.',
    '原仓库在准备期间发生变化，请检查并重新启动。': 'The original repository changed during preparation. Check it and start again.',
    '实例不能更换已经预留的开发目录。': 'An instance cannot change its reserved development workspace.',
    '该目录与另一迁移实例的开发区重叠；请先结束该实例或选择独立开发区。': 'This folder overlaps another migration instance’s workspace. Finish that instance first or choose a separate workspace.',
    '此实例已结束，不能再调度监督对话。': 'This instance has ended and cannot schedule another supervisor conversation.',
    'Supervisor 正在处理上一条消息，请稍后再发送。': 'The supervisor is processing the previous message. Send another one later.',
    '选择的模型 {model} 不在已配置的模型列表中，请添加该模型或修改角色选择。': 'Selected model {model} is not configured. Add it or change the role selection.',
    '模型 {model} 不支持推理强度 {effort}，可选值为：{supported}。': 'Model {model} does not support reasoning effort {effort}. Available values: {supported}.',
    '版本声明文件 {path} 超出读取上限，请手动确认版本。': 'Version declaration file {path} exceeds the read limit. Confirm the versions manually.',
    '已排除 {count} 个命名文件或目录；目录内容未读取，按入口计数。': 'Excluded {count} named files or folders. Folder contents were not read; each folder counts as one entry.',
    '请选择 Git 仓库根目录：{path}': 'Select the Git repository root: {path}',
    '本地源码 Git 操作失败：{detail}': 'The local source Git operation failed: {detail}',
})
CHINESE_MESSAGES.update({
    'Invalid provider ID': '提供方标识无效', 'Invalid provider name': '提供方名称无效',
    'Invalid provider URL': '提供方地址无效', 'Invalid model ID': '模型标识无效',
    'Invalid selected model': '所选模型无效', 'Invalid selected reasoning effort': '所选推理强度无效',
    'Configure between 1 and 16 providers': '请配置 1–16 个提供方',
    'Invalid provider settings fields': '提供方设置字段无效',
    'Provider IDs must be unique lowercase identifiers': '提供方标识须唯一且使用小写字符',
    'Unsupported provider API type': '不支持的提供方 API 类型',
    'Provider URL must be HTTP(S) without credentials, query or fragment': '提供方地址须使用 HTTP(S)，且不含凭据、查询参数或片段',
    'Configure between 1 and 64 models per provider': '每个提供方请配置 1–64 个模型',
    'Invalid model settings fields': '模型设置字段无效',
    'Model IDs must be unique literal identifiers within a provider': '同一提供方的模型标识须唯一且为字面值',
    'Model token limits must be positive integers with output at most context': '模型 Token 上限须为正整数，且输出上限不能超过上下文上限',
    'Invalid model reasoning efforts': '模型推理强度配置无效', 'Invalid provider API key': '提供方 API 密钥无效',
    'Too many stage model overrides': '阶段模型覆盖设置过多',
    'Invalid stage model override identifier': '阶段模型覆盖标识无效',
    'Model settings directory must be private to its owner': '模型设置目录须仅允许所有者访问',
    'Model settings exceed their size limit': '模型设置超出大小上限',
    'Invalid saved model settings': '保存的模型设置无效', 'Invalid saved model settings fields': '保存的模型设置字段无效',
    'message must contain 1–12000 characters': '消息须包含 1–12000 个字符',
    'application state must not be a symbolic link': '应用状态文件不能是符号链接',
    'application state exceeds its size limit': '应用状态超出大小上限',
    'application root must not contain control characters': '应用数据目录不能包含控制字符',
    'application directories must not be symbolic links': '应用目录不能是符号链接',
    'unsafe migration instance directory': '迁移实例目录不安全',
})

# Only these owned templates can substitute user paths/identifiers/raw details.
_TEMPLATES = (
    ('版本声明文件 {path} 超出读取上限，请手动确认版本。', r'版本声明文件 (?P<path>.+) 超出读取上限，请手动确认版本。'),
    ('已排除 {count} 个命名文件或目录；目录内容未读取，按入口计数。', r'已排除 (?P<count>\d+) 个命名文件或目录；目录内容未读取，按入口计数。'),
    ('请选择 Git 仓库根目录：{path}', r'请选择 Git 仓库根目录：(?P<path>.+)'),
    ('本地源码 Git 操作失败：{detail}', r'本地源码 Git 操作失败：(?P<detail>[\s\S]+)'),
    ('选择的模型 {model} 不在已配置的模型列表中，请添加该模型或修改角色选择。', r'选择的模型 (?P<model>.+) 不在已配置的模型列表中，请添加该模型或修改角色选择。'),
    ('模型 {model} 不支持推理强度 {effort}，可选值为：{supported}。', r'模型 (?P<model>.+) 不支持推理强度 (?P<effort>[^，]+)，可选值为：(?P<supported>.+)。'),
)


def owned_display(message):
    """Translate a known desktop warning/reason; preserve interpolated data."""
    if not isinstance(message, str):
        return message
    if message in MESSAGES or message in CHINESE_MESSAGES:
        return t(message)
    for template, pattern in _TEMPLATES:
        match = re.fullmatch(pattern, message)
        if match:
            return t(template, **match.groupdict())
    return message


def git_reason_display(git):
    """Expose both known reason labels for live switching without reinspection."""
    message = git.get('reason', '')
    result = {**git, 'reason': owned_display(message)}
    if (isinstance(message, str) and (message in MESSAGES or message in CHINESE_MESSAGES
            or re.fullmatch(r'请选择 Git 仓库根目录：.+', message))):
        translations = {}
        for locale in ('en', 'zh-CN'):
            with language_scope(locale):
                translations[locale] = owned_display(message)
        result['reason_translations'] = translations
    return result


def warning_display(messages):
    """Known inspection warnings in both languages; raw entries stay verbatim."""
    translations = {}
    for locale in ('en', 'zh-CN'):
        with language_scope(locale):
            translations[locale] = [owned_display(message) for message in messages]
    return {'warnings': translations[_language.get()], 'warnings_translations': translations}


def validation_message(error):
    """Localize known validation text only when raised by an owned validator."""
    if not isinstance(error, (ValueError, TypeError)):
        return str(error)
    trace = error.__traceback__
    if trace is None:
        return str(error)
    while trace.tb_next:
        trace = trace.tb_next
    module = trace.tb_frame.f_globals.get('__name__')
    if module not in {'modport.desktop_service', 'modport.desktop_model_settings',
                      'modport.desktop_local_source', 'modport.desktop_state', 'modport.model_policy', 'modport.models'}:
        return str(error)
    return owned_display(str(error))

CHINESE_MESSAGES.update({
    'model selection requires model and reasoning_effort': '模型选择须包含 model 和 reasoning_effort',
    'fallback model requires an explicit provider/model ID': '备用模型须使用明确的提供方/模型标识',
    'fallback must select a different model or reasoning effort': '备用选择须使用不同的模型或推理强度',
    'model configuration requires default with optional roles and stages': '模型配置须包含 default，可选 roles 和 stages',
    'unknown model configuration role': '模型配置角色未知',
    'invalid model configuration stage': '模型配置阶段无效',
    'source_repository must be a non-empty string': '源码仓库须为非空字符串',
    'mod_id must be a non-empty string': '模组标识须为非空字符串',
})
for _field in ['source_minecraft', 'target_minecraft', 'source_loader', 'target_loader', 'source_loader_version', 'target_loader_version']:
    CHINESE_MESSAGES[_field + ' must be a non-empty string'] = _field + ' 须为非空字符串'
    CHINESE_MESSAGES[_field + ' must not contain whitespace'] = _field + ' 不能包含空白字符'
    CHINESE_MESSAGES[_field + ' must be a non-empty string or None'] = _field + ' 须为非空字符串或 None'
for _field in ['model', 'reasoning_effort']:
    CHINESE_MESSAGES['invalid model selection ' + _field] = '模型选择字段 ' + _field + ' 无效'

MESSAGES.update({
    '实例已保存；尚未收到任务状态投影，当前执行状态未知。': 'The instance was saved, but no task status projection has arrived. The current execution state is unknown.',
    '状态投影超过 30 秒未更新；这不证明进程已退出。请查看持久监督器或环境检查。': 'The status projection has not updated for over 30 seconds. This does not prove the process has exited. Check the persistent supervisor or environment.',
    '执行已完成；行为验收仍未验证。': 'Execution is complete; behavior acceptance remains unverified.',
    '未识别到源码中的 mod_id 字面量。本次暂用规范化仓库名称 {mod_id}；项目显示名称独立保存，实际源码身份仍由迁移源码读取确认。': 'No literal mod_id was detected in the source. The normalized repository name {mod_id} is used temporarily; the project display name is stored separately and migration source reading will confirm the actual source identity.',
    '本地源码快照包含 {count} 个文件，已排除 {excluded} 项。': 'The local source snapshot contains {count} files; {excluded} entries were excluded.',
    '持久监督器启动失败：{detail}': 'The persistent supervisor could not start: {detail}',
})
_TEMPLATES += (
    ('未识别到源码中的 mod_id 字面量。本次暂用规范化仓库名称 {mod_id}；项目显示名称独立保存，实际源码身份仍由迁移源码读取确认。', r'未识别到源码中的 mod_id 字面量。本次暂用规范化仓库名称 (?P<mod_id>[a-z0-9_]+)；项目显示名称独立保存，实际源码身份仍由迁移源码读取确认。'),
    ('本地源码快照包含 {count} 个文件，已排除 {excluded} 项。', r'本地源码快照包含 (?P<count>\d+) 个文件，已排除 (?P<excluded>\d+) 项。'),
    ('持久监督器启动失败：{detail}', r'持久监督器启动失败：(?P<detail>[\s\S]+)'),
)
