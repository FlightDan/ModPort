/* The renderer has no shell or filesystem privileges. All state comes from the service. */
(function () {
    'use strict';
    const i18n = typeof module !== 'undefined' && module.exports ? require('./i18n.js') : window.ModPortI18n;
    const t = (message, ...values) => i18n.t(message, ...values);
    const {setText, bindText} = i18n;
    const labels = {pending: '未开始', queued: '排队中', running: '运行中', waiting: '等待处理', completed: '已完成', succeeded: '执行完成', failed: '失败', issues: '有失败项', cancelled: '已取消', cancelling: '取消处理中', planned: '准备中', unknown: '未知'};
    const groups = [{id: 'author', label: '编写与执行', code: 'AUTHOR / EXECUTION'}, {id: 'review', label: '审查与监督', code: 'REVIEW / SUPERVISION'}];
    const stageRoles = {migration_plan: 'planner', contract_repair_plan: 'planner', target_repair_plan: 'planner', coder_revival_plan: 'planner', behavior_extract: 'planner', coder: 'coder', agent_rework: 'coder', contract_draft: 'coder', artifact_test_design: 'coder', test_design: 'coder', code_cleanup: 'coder', supervisor: 'supervisor', contract_review: 'contract_review', behavior_review: 'contract_review', prompt_summary: 'summary'};
    const clone = value => JSON.parse(JSON.stringify(value));
    const knownNumber = value => typeof value === 'number' && Number.isFinite(value) && value >= 0;
    const endedRun = run => ['succeeded', 'completed', 'failed', 'cancelled'].includes(run?.status);
    const number = value => knownNumber(value) ? value.toLocaleString(i18n.locale) : t('未知');
    function duration(value) {
        if (!knownNumber(value)) return t('未知');
        const seconds = Math.floor(value);
        return `${String(Math.floor(seconds / 3600)).padStart(2, '0')}:${String(Math.floor(seconds / 60) % 60).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
    }
    function selectionFor(config, row) {
        if (row.target === 'default' || row.id === 'default') return config.default || {};
        if (row.id === 'subagent') return config.roles?.subagent || config.stages?.coder || config.roles?.coder || config.default || {};
        if (row.target === 'stage') return config.stages?.[row.stage] || config.roles?.[row.role || stageRoles[row.stage]] || row.selection || config.default || {};
        return config.roles?.[row.id] || row.selection || config.default || {};
    }
    function changeSelection(config, row, field, value) {
        const selection = {...selectionFor(config, row), [field]: value.trim()};
        if (row.target === 'default' || row.id === 'default') config.default = selection;
        else if (row.target === 'stage') (config.stages ||= {})[row.stage] = selection;
        else (config.roles ||= {})[row.id] = selection;
        return config;
    }
    function partitionItems(items, limit = 4) {
        const visible = (items || []).filter(item => !(['pending', 'queued'].includes(item.state) && !(item.active_agents > 0 || item.active_subagents > 0 || item.started_at)));
        const completed = visible.filter(item => item.state === 'completed');
        const attention = visible.filter(item => ['failed', 'cancelled', 'waiting'].includes(item.state) || item.attention_required);
        const active = visible.filter(item => item.state !== 'completed' && !attention.includes(item));
        return {attention, active: active.slice(0, limit), overflow: active.slice(limit), completed};
    }
    function stagePresentation(stage) {
        const items = stage?.items || [];
        const failed = items.filter(item => item.state === 'failed').length;
        // These columns group tasks; only the Run status describes its lifecycle.
        const active = ['running', 'waiting', 'queued'].find(value => items.some(item => item.state === value));
        return {state: active || (failed || stage?.state === 'failed' ? 'issues' : stage?.state), failed};
    }
    const helpers = {duration, number, selectionFor, changeSelection, partitionItems, stagePresentation};
    if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
    if (typeof document === 'undefined') return;
    const $ = id => document.getElementById(id);
    const state = {bootstrap: null, modelConfig: null, project: null, localSource: null, workspaceMode: 'copy', run: null, runId: null, screen: 'project', polling: null, fetching: false, fetchAgain: false, folds: new Map(), messagesKey: '', syncedAt: null, syncError: false, roleInputs: new Map(), requestEpoch: 0};
    let providerDraft = [], modelBackup = null, modelSetup = null, modelSetupStep = 0, guidedSetupDirty = false;
    const sourceDrafts = {remote: null, local: null};
    let sourceMode = 'remote';
    let fallbackSetup = null, updateStatus = null;
    let warningDisplay = null;
    function renderWarnings() {
        if (!warningDisplay) return;
        const {result, extra} = warningDisplay;
        const warnings = result.warnings_translations?.[i18n.locale] || result.warnings || [];
        const text = warnings.map(warning => typeof warning === 'string' ? warning : warning.message || warning.detail);
        text.push(...extra.map(message => t(message)));
        $('repository-warnings').hidden = !text.length;
        setText($('repository-warnings'), text.join('\n'));
    }
    function showWarnings(result, extra = []) {
        warningDisplay = {result, extra};
        renderWarnings();
    }
    function element(tag, className, content) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (content !== undefined) setText(node, content);
        return node;
    }
    function badge(status) { return element('span', `badge ${Object.hasOwn(labels, status) ? `state-${status}` : ''}`, t(labels[status] || status || labels.unknown)); }
    function feedback(message, kind = '', render = null) {
        const node = $('feedback');
        node.className = `feedback ${kind}`;
        if (render) bindText(node, render); else setText(node, message);
        node.hidden = !message;
    }
    function feedbackTranslated(render, kind = '') { feedback(render(), kind, render); }
    async function request(method, path, body) {
        let result;
        if (window.modport?.request) result = await window.modport.request({method, path, body});
        else {
            const response = await fetch(path, {method, headers: {'Content-Type': 'application/json', 'Accept-Language': i18n.locale}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
            try { result = await response.json(); } catch { throw new Error(t("服务返回了无法读取的响应 ({0})", response.status)); }
            if (!response.ok) throw new Error(result.error || t("请求失败 ({0})", response.status));
        }
        if (!result || typeof result !== 'object') throw new Error(t('本机服务没有返回有效数据。'));
        if (result.error) throw new Error(result.error);
        return result;
    }
    async function busy(button, text, action) {
        const old = [...button.childNodes];
        button.disabled = true;
        setText(button, text);
        try { return await action(); }
        catch (error) { feedback(error.message || String(error), 'error'); return undefined; }
        finally { button.replaceChildren(...old); button.disabled = false; }
    }
    function showScreen(name, focus = true) {
        state.screen = name;
        for (const key of ['project', 'settings', 'run']) $(`${key}-screen`).hidden = key !== name;
        document.querySelectorAll('.steps button').forEach(button => {
            if (button.dataset.screen === name) button.setAttribute('aria-current', 'step'); else button.removeAttribute('aria-current');
            button.disabled = false;
        });
        if (name !== 'run') clearTimeout(state.polling);
        if (name === 'run') {requestAnimationFrame(restoreChatScroll); if (state.runId) schedulePoll();}
        if (focus) $('main').focus({preventScroll: true});
    }
    function fillDefaults(defaults = {}) {
        for (const input of $('project-form').elements) if (input.name && defaults[input.name] !== undefined && !input.value) input.value = defaults[input.name] ?? '';
        if (defaults.source_loader) $('source-loader').value = defaults.source_loader;
        if (defaults.target_loader) $('target-loader').value = defaults.target_loader;
        if (knownNumber(defaults.max_seconds) && defaults.max_seconds > 0) $('max-hours').value = defaults.max_seconds / 3600;
        if (knownNumber(defaults.max_tokens) && defaults.max_tokens > 0) $('max-tokens').value = defaults.max_tokens;
    }
    function renderEnvironment(environment) {
        const ready = environment?.ready;
        $('environment-button').dataset.ready = ready === true ? 'true' : 'false';
        setText($('environment-label'), ready === true ? t('环境就绪') : t('环境需要准备'));
        const checks = $('environment-checks');
        checks.replaceChildren();
        for (const check of environment?.checks || []) {
            const row = element('section', 'environment-check');
            const header = element('header');
            header.append(element('strong', '', check.label || check.id), badge(check.ready ? 'completed' : 'waiting'));
            row.append(header, element('p', '', check.detail || t('服务未提供详细检查结果。')));
            if (check.action) {
                const button = element('button', '', check.action_label || t('准备此项'));
                button.type = 'button';
                button.addEventListener('click', () => busy(button, t('正在准备…'), async () => {
                    const result = await request('POST', '/api/setup', {action: check.action});
                    $('setup-result').hidden = false;
                    const output = result.message || result.detail || result.result;
                    setText($('setup-result'), typeof output === 'object' ? JSON.stringify(output, null, 2) : output || t('操作已完成。请查看最新环境检查。'));
                    await bootstrap(false);
                }));
                row.append(button);
            }
            checks.append(row);
        }
        if (!checks.children.length) checks.append(element('p', 'empty', t('服务尚未提供环境检查结果。')));
        $('launch-button').disabled = ready !== true;
        setText($('launch-note'), ready === true ? t('启动前请确认项目版本与执行预算。') : t('请打开运行环境，完成准备后再启动。'));
    }
    function renderRecent(runs) {
        $('recent-runs').replaceChildren();
        for (const run of runs || []) {
            if (!run.id) continue;
            const button = element('button', 'recent-item');
            button.type = 'button';
            const text = element('div');
            text.append(element('span', 'recent-name', run.project_name || run.id), element('small', '', run.id));
            button.append(text, badge(run.status));
            button.addEventListener('click', () => busy(button, t('正在打开…'), () => openRun(run.id)));
            $('recent-runs').append(button);
        }
        if (!$('recent-runs').children.length) $('recent-runs').append(element('p', 'empty', t('还没有迁移实例。创建项目后会显示在这里。')));
    }
    function makeModelInput(id, value, list, label) {
        const input = element('input');
        input.id = id; input.value = value || ''; input.setAttribute('list', list); input.setAttribute('aria-label', label); input.autocomplete = 'off'; input.required = true;
        return input;
    }
    function updateGroupDisplay(group, rows) {
        for (const field of ['model', 'reasoning_effort']) {
            const values = new Set(rows.map(row => selectionFor(state.modelConfig, row)[field] || ''));
            const input = $(`group-${group.id}-${field}`);
            input.value = values.size === 1 ? [...values][0] : '';
            input.placeholder = values.size > 1 ? t('多个设置 · 保持现有选择') : t('填写模型设置');
        }
    }
    function syncInheritedSubagentInputs() {
        if (state.modelConfig.roles?.subagent) return;
        const selection = selectionFor(state.modelConfig, {id: 'subagent', target: 'role'});
        for (const field of ['model', 'reasoning_effort']) {
            const input = state.roleInputs.get(`subagent:${field}`);
            if (input) input.value = selection[field] || '';
        }
    }
    function renderModels() {
        const rows = state.bootstrap.roles || [];
        const root = $('model-groups'); root.replaceChildren(); state.roleInputs.clear();
        const suggestions = new Set();
        const efforts = new Set();
        for (const provider of providerDraft) for (const model of provider.models) {
            if (provider.id && model.id) suggestions.add(`${provider.id}/${model.id}`);
            for (const effort of model.reasoning_efforts.length ? model.reasoning_efforts : ['none']) efforts.add(effort);
        }
        for (const row of rows) {
            const selection = selectionFor(state.modelConfig, row);
            if (selection.model) suggestions.add(selection.model);
            if (selection.reasoning_effort) efforts.add(selection.reasoning_effort);
        }
        $('models-list').replaceChildren(...[...suggestions].map(model => { const option = element('option'); option.value = model; return option; }));
        $('effort-list').replaceChildren(...[...efforts].map(effort => {const option = element('option'); option.value = effort; return option;}));
        for (const group of groups) {
            const members = rows.filter(row => row.group === group.id);
            const panel = element('section', 'panel model-group');
            const header = element('div', 'panel-heading'); header.append(element('h2', '', t(group.label)), element('span', 'small-code', group.code)); panel.append(header);
            const controls = element('div', 'group-models');
            for (const [field, title, list] of [['model', t('模型'), 'models-list'], ['reasoning_effort', t('推理强度'), 'effort-list']]) {
                const wrapper = element('div', 'field'); const id = `group-${group.id}-${field}`;
                const label = element('label', '', title); label.htmlFor = id;
                const input = makeModelInput(id, '', list, `${t(group.label)} ${title}`); input.required = false;
                input.addEventListener('change', () => {
                    if (!input.value.trim()) {updateGroupDisplay(group, members); return;}
                    for (const row of members) {
                        if (row.id === 'subagent' && !state.modelConfig.roles?.subagent) continue;
                        changeSelection(state.modelConfig, row, field, input.value);
                        state.roleInputs.get(`${row.id}:${field}`).value = input.value.trim();
                    }
                    syncInheritedSubagentInputs();
                    updateGroupDisplay(group, members);
                });
                wrapper.append(label, input); controls.append(wrapper);
            }
            controls.append(element('p', 'field-hint', t('修改后应用于本组所有角色；也可以展开逐项调整。'))); panel.append(controls);
            const disclosure = element('details', 'role-disclosure');
            disclosure.append(element('summary', '', t("单独配置角色与阶段 · {0} 项", members.length)));
            for (const row of members) {
                const role = element('div', 'role-row');
                const name = element('div', 'role-label', row.label || row.id); name.append(element('small', '', row.target === 'stage' ? row.stage : row.id)); role.append(name);
                if (row.id === 'subagent') {
                    const inherit = element('button', '', t('沿用代码编写模型'));
                    inherit.type = 'button';
                    inherit.addEventListener('click', () => {delete state.modelConfig.roles.subagent; renderModels();});
                    name.append(inherit);
                }
                for (const [field, title, list] of [['model', t('模型'), 'models-list'], ['reasoning_effort', t('推理强度'), 'effort-list']]) {
                    const input = makeModelInput(`role-${row.id.replace(/[^\w-]/g, '-')}-${field}`, selectionFor(state.modelConfig, row)[field], list, `${row.label || row.id} ${title}`);
                    state.roleInputs.set(`${row.id}:${field}`, input);
                    input.addEventListener('change', () => {
                        if (input.value.trim()) {changeSelection(state.modelConfig, row, field, input.value); syncInheritedSubagentInputs(); updateGroupDisplay(group, members);}
                        else input.value = selectionFor(state.modelConfig, row)[field] || '';
                    }); role.append(input);
                }
                disclosure.append(role);
            }
            panel.append(disclosure); root.append(panel); updateGroupDisplay(group, members);
        }
    }
    function modelSummary() {
        const policy = state.modelConfig;
        const difficult = policy?.stages?.migration_plan || policy?.roles?.planner || policy?.default;
        const routine = policy?.stages?.coder || policy?.roles?.coder || policy?.default;
        setText($('model-summary'), difficult && routine ? [t('困难任务：{0} · {1}', difficult.model, difficult.reasoning_effort), t('常规 Coder：{0} · {1}', routine.model, routine.reasoning_effort)].join('\n') : t('尚未读取模型配置'));
    }
    function modelField(root, id, labelText, value, update, options = {}) {
        const wrapper = element('div', 'field');
        const label = element('label', '', labelText); label.htmlFor = id;
        const input = element(options.choices ? 'select' : 'input'); input.id = id;
        if (options.choices) for (const [value, title] of options.choices) {const option = element('option', '', title); option.value = value; input.append(option);}
        else {input.type = options.type || 'text'; input.autocomplete = options.type === 'password' ? 'new-password' : 'off';}
        input.value = value ?? ''; input.required = options.required !== false;
        if (options.type === 'number') {input.min = '1'; input.max = '100000000'; input.step = '1';}
        if (options.placeholder) input.placeholder = options.placeholder;
        if (options.readOnly) input.readOnly = true;
        input.addEventListener('input', () => update(input.type === 'number' ? Number(input.value) : input.value));
        wrapper.append(label, input); root.append(wrapper);
        return input;
    }
    function renderProviders() {
        const root = $('provider-settings'); root.replaceChildren();
        if (!providerDraft.length) root.append(element('p', 'empty', t('添加一个 API 连接，再填写服务提供的模型名称与上下文上限。')));
        providerDraft.forEach((provider, index) => {
            const panel = element('section', 'panel provider-panel');
            const header = element('div', 'panel-heading');
            header.append(element('h3', '', t("连接 {0}", index + 1)));
            const remove = element('button', 'text-button', t('移除连接')); remove.type = 'button';
            remove.addEventListener('click', () => {providerDraft.splice(index, 1); renderProviders(); renderModels();});
            header.append(remove); panel.append(header);
            const fields = element('div', 'provider-fields');
            const prefix = `provider-${index}`;
            modelField(fields, `${prefix}-name`, t('连接名称'), provider.name, value => provider.name = value);
            modelField(fields, `${prefix}-id`, t('连接标识'), provider.id, value => provider.id = value, {readOnly: provider.saved});
            modelField(fields, `${prefix}-type`, t('API 协议'), provider.api_type, value => provider.api_type = value, {choices: [['openai', 'OpenAI Responses'], ['openai-compatible', t('OpenAI 兼容 · Chat Completions')]]});
            modelField(fields, `${prefix}-url`, t('API 地址'), provider.base_url, value => provider.base_url = value, {type: 'url', placeholder: 'https://api.example.com/v1'});
            modelField(fields, `${prefix}-key`, t('API 密钥'), provider.api_key || '', value => provider.api_key = value, {type: 'password', required: false, placeholder: provider.api_key_configured ? t('已配置 · 留空保留') : t('输入 API 密钥；本地免密服务可留空')});
            panel.append(fields);
            const models = element('div', 'provider-models');
            provider.models.forEach((model, modelIndex) => {
                const row = element('fieldset', 'provider-model');
                row.append(element('legend', '', t("模型 {0}", modelIndex + 1)));
                const fields = element('div', 'provider-fields');
                const prefix = `provider-${index}-model-${modelIndex}`;
                const id = modelField(fields, `${prefix}-id`, t('模型 ID'), model.id, value => model.id = value, {placeholder: t('服务提供的模型名称')});
                id.addEventListener('change', renderModels);
                modelField(fields, `${prefix}-context`, t('上下文窗口 · Tokens'), model.context_window, value => model.context_window = value, {type: 'number'});
                modelField(fields, `${prefix}-output`, t('最大输出 · Tokens'), model.max_output_tokens, value => model.max_output_tokens = value, {type: 'number'});
                modelField(fields, `${prefix}-efforts`, t('支持的推理强度'), model.reasoning_efforts.join(', '), value => model.reasoning_efforts = value.split(/[,，]/).map(item => item.trim()).filter(Boolean), {required: false, placeholder: t('例如 low, medium, high；不支持可留空')});
                row.append(fields);
                const actions = element('div', 'provider-model-actions');
                const remove = element('button', 'text-button', t('移除模型')); remove.type = 'button';
                remove.addEventListener('click', () => {provider.models.splice(modelIndex, 1); renderProviders(); renderModels();});
                actions.append(remove); row.append(actions); models.append(row);
            });
            const add = element('button', '', t('＋ 添加模型')); add.type = 'button';
            add.addEventListener('click', () => {provider.models.push({id: '', context_window: '', max_output_tokens: '', reasoning_efforts: []}); renderProviders(); $(`provider-${index}-model-${provider.models.length - 1}-id`).focus();});
            models.append(add); panel.append(models); root.append(panel);
        });
        renderFallback();
    }
    function modelSetupError(error) {
        const labels = {"First API": "第一组 API", "Second API": "第二组 API", "Difficult-task model": "困难任务模型", "Routine-task model": "常规 Coder 模型"};
        const messages = {"enter a valid connection ID.": "请输入有效的连接标识。", "enter a connection name.": "请输入连接名称。", "enter an API URL.": "请输入 API 地址。", "enter a model name.": "请输入模型名称。", "enter a valid model name.": "请输入有效的模型名称。", "context window must be a positive whole number at most 100000000.": "上下文窗口必须为不大于 100000000 的正整数。", "maximum output must be positive and no greater than the context window.": "最大输出必须为正整数且不大于上下文窗口。", "enter supported reasoning efforts.": "请填写支持的推理强度。", "selected reasoning effort is not supported by this model.": "所选推理强度不在该模型支持的范围内。", "Second API must use a different connection ID.": "第二组 API 必须使用不同的连接标识。", "Models with the same name on one API must have matching details. Choose the same model or use different names.": "同一 API 中同名模型的规格必须一致。请使用相同模型按钮，或填写不同模型名称。"};
        Object.assign(messages, {
            "Selected fallback model was not found in the provider catalog.": "所选备用模型已从连接中移除，请重新选择。",
            "Fallback mode is invalid.": "备用设置模式无效。",
            "Fallback connection ID is invalid.": "备用连接标识无效。",
            "Fallback connection ID is already in use.": "备用连接标识已被使用。",
            "Fallback connection name is required and must be at most 120 characters.": "备用连接名称不能为空，且不能超过 120 个字符。",
            "Fallback API type is unsupported.": "不支持此备用 API 协议。",
            "Fallback API URL is required and must be a valid HTTP(S) URL.": "请填写有效的备用 HTTP(S) API 地址。",
            "Fallback API URL must be HTTP(S) without credentials, query, or fragment.": "备用 API 地址须为 HTTP(S)，且不含凭据、查询参数或片段。",
            "Fallback API key is invalid.": "备用 API 密钥格式无效。",
            "Fallback provider has an invalid model catalog.": "备用连接的模型目录无效。",
            "Fallback provider catalog has reached the 16-provider limit.": "API 连接数量已达到 16 个的上限。",
            "Fallback provider has reached the 64-model limit.": "此 API 的模型数量已达到 64 个的上限。",
            "Fallback model details are required.": "请填写备用模型的详细规格。",
            "Fallback model name is required.": "请填写备用模型名称。",
            "Fallback model name is invalid.": "备用模型名称格式无效。",
            "Fallback context window must be a positive whole number at most 100000000.": "备用模型上下文窗口必须为不大于 100000000 的正整数。",
            "Fallback maximum output must be positive and no greater than context window.": "备用模型最大输出必须为正整数且不大于上下文窗口。",
            "Fallback model reasoning efforts are invalid.": "备用模型支持的推理强度无效。",
            "Fallback reasoning effort is not supported by this model.": "此备用模型不支持所选推理强度。",
            "Fallback model conflicts with an existing model on this API.": "备用模型规格与此 API 已有同名模型不一致。请重新选择或在高级配置中修改。",
            "Selected fallback provider ID is invalid.": "所选备用连接的标识无效。",
            "Selected fallback provider was not found in settings.": "所选备用连接已不存在，请重新选择。",
            "Fallback provider details are required.": "请填写备用 API 连接信息。",
            "Model settings contain an invalid selection map.": "模型设置中的角色或阶段配置无效。",
            "Model settings contain an invalid selection.": "模型设置包含无效的模型选择。",
            "Provider settings must be a list.": "API 连接列表格式无效。"
});
        const [label, detail] = error.message.split(": ");
        return labels[label] && messages[detail] ? `${t(labels[label])}: ${t(messages[detail])}` : t(messages[error.message] || error.message);
    }
    function commitGuidedSetup(validate = true) {
        if (!modelSetup || !guidedSetupDirty) return;
        const result = window.ModPortModelSetup.apply(modelSetup, providerDraft, state.modelConfig, {validate});
        providerDraft = result.providers.map(provider => {
            const previous = providerDraft.find(item => item.id === provider.id);
            return {...provider, saved: previous?.saved, api_key_configured: previous?.api_key_configured};
        });
        state.modelConfig = result.model_config;
        guidedSetupDirty = false;
    }
    function guidedField(root, id, title, value, update, options = {}) {
        return modelField(root, id, t(title), value, value => {update(value); guidedSetupDirty = true;}, options);
    }
    function guidedConnection(root, provider, prefix, field = guidedField) {
        const fields = element('div', 'provider-fields');
        field(fields, `${prefix}-url`, 'API 地址', provider.base_url, value => provider.base_url = value,
            {type: 'url', placeholder: 'https://api.example.com/v1'});
        field(fields, `${prefix}-key`, 'API 密钥', provider.api_key || '', value => provider.api_key = value,
            {type: 'password', required: false, placeholder: provider.api_key_configured ? t('已配置 · 留空保留') : t('输入 API 密钥；本地免密服务可留空')});
        field(fields, `${prefix}-protocol`, 'API 协议', provider.api_type, value => provider.api_type = value,
            {choices: [['openai', 'OpenAI Responses'], ['openai-compatible', t('OpenAI 兼容 · Chat Completions')]]});
        root.append(fields);
    }
    function guidedModel(root, slot, prefix, field = guidedField) {
        const fields = element('div', 'provider-fields');
        field(fields, `${prefix}-id`, '模型名称', slot.model.id, value => slot.model.id = value,
            {placeholder: t('填写 API 服务提供的模型名称')});
        field(fields, `${prefix}-reasoning`, '推理强度', slot.reasoning_effort, value => slot.reasoning_effort = value,
            {placeholder: 'high / medium / none'});
        root.append(fields);
        const details = element('details', 'model-detail-settings'); details.open = true;
        details.append(element('summary', '', t('模型细节')));
        const limits = element('div', 'provider-fields');
        field(limits, `${prefix}-context`, '上下文窗口 · Tokens', slot.model.context_window, value => slot.model.context_window = value, {type: 'number'});
        field(limits, `${prefix}-output`, '最大输出 · Tokens', slot.model.max_output_tokens, value => slot.model.max_output_tokens = value, {type: 'number'});
        field(limits, `${prefix}-efforts`, '支持的推理强度', slot.model.reasoning_efforts.join(', '), value => slot.model.reasoning_efforts = value.split(/[,，]/).map(item => item.trim()).filter(Boolean),
            {required: false, placeholder: t('例如 low, medium, high；不支持可留空')});
        details.append(limits, element('p', 'field-hint', t('请按模型服务提供的规格填写；不支持推理强度的模型使用 none。')));
        root.append(details);
    }
    function fallbackField(root, id, title, value, update, options = {}) {
        return modelField(root, id, t(title), value, update, options);
    }
    function renderFallback() {
        if (!fallbackSetup) return;
        const root = $('fallback-settings-content'); root.replaceChildren();
        const selectedProvider = providerDraft.find(item => item.id === fallbackSetup.providerId);
        const selectedModel = selectedProvider?.models.find(item => item.id === fallbackSetup.choice.model.id);
        if (selectedModel) fallbackSetup.choice.model = clone(selectedModel);
        const selections = [state.modelConfig.default, ...Object.values(state.modelConfig.roles || {}), ...Object.values(state.modelConfig.stages || {})];
        const existing = [...new Set(selections.filter(item => item?.fallback).map(item => `${item.fallback.model} · ${item.fallback.reasoning_effort}`))];
        root.append(element('p', 'field-hint', existing.length ? t('已配置备用：{0}', existing.join(' / ')) : t('尚未配置备用模型。')));
        const mode = fallbackField(root, 'fallback-mode', '备用模型设置', fallbackSetup.mode, value => {
            fallbackSetup.mode = value; renderFallback(); renderGuidedSetup(); $('fallback-mode').focus();
        }, {choices: [['preserve', t('保持现有设置')], ['shared', t('统一设置备用模型')], ['off', t('关闭所有备用模型')]]});
        mode.setAttribute('aria-describedby', 'fallback-help');
        const help = element('p', 'field-hint', t('明确的 API 认证、额度、限流或服务不可用错误可触发一次切换；普通超时或已执行工具的失败回合不会自动重放。'));
        help.id = 'fallback-help'; root.append(help);
        if (fallbackSetup.mode !== 'shared') return;
        root.append(element('p', 'field-hint', t('应用到所有角色与阶段；与主模型及推理强度相同的项不再设置备用。')));
        fallbackField(root, 'fallback-provider', '备用 API 连接', fallbackSetup.providerId, value => {
            fallbackSetup.providerId = value;
            const provider = providerDraft.find(item => item.id === value);
            const model = provider?.models[0];
            fallbackSetup.choice = {model: model ? clone(model) : {id: '', context_window: '', max_output_tokens: '', reasoning_efforts: []}, reasoning_effort: model?.reasoning_efforts[0] || 'none'};
            renderFallback(); $('fallback-provider').focus();
        }, {choices: [['', t('新增独立备用 API')], ...providerDraft.map(provider => [provider.id, provider.name || provider.id])]});
        if (!fallbackSetup.providerId) guidedConnection(root, fallbackSetup.provider, 'fallback-connection', fallbackField);
        else {
            const provider = providerDraft.find(item => item.id === fallbackSetup.providerId);
            const modelPicker = fallbackField(root, 'fallback-catalog-model', '选择已配置模型', fallbackSetup.choice.model.id, value => {
                const model = provider?.models.find(item => item.id === value);
                if (model) fallbackSetup.choice = {model: clone(model), reasoning_effort: model.reasoning_efforts[0] || 'none'};
                renderFallback(); $('fallback-catalog-model').focus();
            }, {choices: (provider?.models || []).map(model => [model.id, model.id])});
            modelPicker.disabled = !provider?.models.length;
            const efforts = fallbackSetup.choice.model.reasoning_efforts;
            fallbackField(root, 'fallback-reasoning', '备用推理强度', fallbackSetup.choice.reasoning_effort,
                value => fallbackSetup.choice.reasoning_effort = value,
                {choices: (efforts.length ? efforts : ['none']).map(effort => [effort, effort])});
            root.append(element('p', 'field-hint', t('沿用此连接的密钥与模型规格；可在高级配置中修改。')));
            return;
        }
        guidedModel(root, fallbackSetup.choice, 'fallback-model', fallbackField);
    }
    function renderUpdates() {
        const available = updateStatus?.status === 'available';
        const supported = typeof window.modport?.checkForUpdates === 'function';
        const messages = {idle: '尚未检查更新。', checking: '正在检查更新…', current: '当前已是最新正式版。', error: '暂时无法检查更新，请稍后重试。'};
        let message = available ? t('发现新版本 {0}', updateStatus.latestVersion) : t(messages[updateStatus?.status] || '尚未检查更新。');
        if (!supported) message = t('请在 ModPort 桌面窗口中检查更新。');
        if (updateStatus?.errorCode === 'no_releases') message = t('尚无可用的正式版发布，或暂时无法访问发布渠道。');
        setText($('updates-label'), available ? message : t('应用更新'));
        $('updates-button').classList.toggle('update-available', available);
        setText($('updates-current'), updateStatus?.currentVersion ? t('当前应用版本：{0}', updateStatus.currentVersion) : '');
        setText($('updates-status'), message);
        setText($('updates-settings-status'), message);
        setText($('updates-checked'), updateStatus?.checkedAt ? t('上次检查：{0}', new Date(updateStatus.checkedAt).toLocaleString(i18n.locale)) : '');
        $('updates-check').disabled = !supported || updateStatus?.status === 'checking';
        setText($('updates-check'), updateStatus?.status === 'checking' ? t('检查中…') : t('检查更新'));
        $('updates-download').hidden = !available;
    }
    async function initializeUpdates() {
        renderUpdates();
        if (!window.modport?.getUpdateStatus) return;
        let received = false;
        window.modport.onUpdateStatus?.(value => {received = true; updateStatus = value; renderUpdates();});
        try {
            const initial = await window.modport.getUpdateStatus();
            if (!received) {updateStatus = initial; renderUpdates();}
        } catch {updateStatus = {status: 'error'}; renderUpdates();}
    }
    function renderGuidedSetup() {
        if (!modelSetup) return;
        const advanced = $('advanced-model-settings').open;
        for (const node of [$('model-setup-steps'), $('model-setup-content'), document.querySelector('.model-setup-navigation')]) node.hidden = advanced;
        const root = $('model-setup-content'); root.replaceChildren();
        document.querySelectorAll('[data-model-step]').forEach(button => {
            if (Number(button.dataset.modelStep) === modelSetupStep) button.setAttribute('aria-current', 'step');
            else button.removeAttribute('aria-current');
        });
        const panel = element('section', 'guided-model-step');
        const titles = ['连接你的模型服务', '配置困难任务模型', '配置常规 Coder 模型'];
        panel.append(element('h3', '', t(titles[modelSetupStep])));
        if (modelSetupStep === 0) {
            panel.append(element('p', 'muted', t('先填写 API 地址和 Key。接下来分别配置困难任务与常规 Coder 使用的模型。')));
            guidedConnection(panel, modelSetup.primary, 'setup-primary');
        } else if (modelSetupStep === 1) {
            panel.append(element('p', 'muted', t('用于迁移规划、复杂问题分析与需求审查。建议选择推理能力较强的模型。')));
            guidedModel(panel, modelSetup.difficult, 'setup-difficult');
        } else {
            panel.append(element('p', 'muted', t('用于日常代码编写与修复，也作为其余任务的默认模型。')));
            const actions = element('div', 'setup-model-actions');
            const same = element('button', '', t(modelSetup.sameModel ? '分别配置两个模型' : '使用与困难任务相同的模型'));
            same.type = 'button'; same.setAttribute('aria-pressed', String(modelSetup.sameModel));
            same.addEventListener('click', () => {
                modelSetup.sameModel = !modelSetup.sameModel;
                if (modelSetup.sameModel) modelSetup.separateConnection = false;
                guidedSetupDirty = true; renderGuidedSetup();
            });
            actions.append(same);
            if (!modelSetup.sameModel) {
                const separate = element('button', '', t(modelSetup.separateConnection ? '共用第一个 API 连接' : '使用独立 API 连接'));
                separate.type = 'button'; separate.setAttribute('aria-pressed', String(modelSetup.separateConnection));
                separate.addEventListener('click', () => {modelSetup.separateConnection = !modelSetup.separateConnection; guidedSetupDirty = true; renderGuidedSetup();});
                actions.append(separate);
            }
            panel.append(actions);
            if (modelSetup.sameModel) {
                panel.append(element('p', 'inline-note', t('两个用途将共用困难任务的连接、模型和推理强度。')));
            } else {
                panel.append(element('p', 'field-hint', t(modelSetup.separateConnection ? '为常规 Coder 单独填写 API 地址和 Key。' : '常规 Coder 共用第一步的 API 地址和 Key。')));
                if (modelSetup.separateConnection) guidedConnection(panel, modelSetup.secondary, 'setup-secondary');
                guidedModel(panel, modelSetup.routine, 'setup-routine');
            }
        }
        root.append(panel);
        $('model-setup-back').disabled = modelSetupStep === 0;
        $('model-setup-next').hidden = modelSetupStep === 2;
        $('save-model-settings').hidden = modelSetupStep !== 2 && !advanced && fallbackSetup?.mode === 'preserve';
        const overrides = Object.keys(state.modelConfig.stages || {}).length + Number(Boolean(state.modelConfig.roles?.subagent));
        setText($('model-setup-overrides'), overrides ? t('另有 {0} 项单独设置会保留，可在高级配置中检查。', overrides) : t('两个用途分别保存；需要时可展开高级配置调整具体角色。'));
    }
    function changeModelStep(step) {
        modelSetupStep = Math.max(0, Math.min(2, step));
        if (modelSetupStep === 2) guidedSetupDirty = true;
        renderGuidedSetup();
        $('model-setup-content').scrollIntoView({block: 'nearest'});
    }
    async function openModelSettings(button) {
        if (!state.bootstrap) return;
        await busy(button, t('读取配置…'), async () => {
            const settings = await request('GET', '/api/model-settings');
            modelBackup = clone(settings.model_config);
            state.modelConfig = clone(settings.model_config);
            providerDraft = settings.providers.map(provider => ({...provider, saved: true}));
            modelSetup = window.ModPortModelSetup.create(providerDraft, state.modelConfig);
            modelSetupStep = 0; guidedSetupDirty = false;
            fallbackSetup = window.ModPortFallbackSetup.create(providerDraft, state.modelConfig);
            $('fallback-settings').open = false;
            renderFallback();
            $('advanced-model-settings').open = false;
            renderProviders(); renderModels(); renderGuidedSetup();
            $('model-settings-error').hidden = true;
            setText($('model-settings-message'), '');
            $('model-settings-dialog').showModal();
        });
    }
    async function bootstrap(initial = true) {
        const data = await request('GET', '/api/bootstrap');
        state.bootstrap = data;
        setText($('workflow-label'), `WORKFLOW ${data.workflow_version ?? t('未知')} · ${data.platform || 'LOCAL'}`);
        renderEnvironment(data.environment); renderRecent(data.recent_runs);
        if (initial) {
            fillDefaults(data.defaults);
            state.modelConfig = clone(data.model_config);
            renderModels();
            modelSummary();
            $('model-settings-button').disabled = false;
            $('project-next').disabled = false;
        }
        return data;
    }
    async function identifyRepository() {
        const repository = $('repository');
        if (!repository.reportValidity()) return;
        const revision = $('revision').value.trim();
        const requestUrl = repository.value.trim();
        const result = await request('POST', '/api/repository', {repository: requestUrl, ...(revision ? {revision} : {})});
        if (sourceMode !== 'remote' || repository.value.trim() !== requestUrl || $('revision').value.trim() !== revision) return;
        $('revisions').replaceChildren();
        for (const ref of [...(result.branches || []), ...(result.tags || [])]) {const option = element('option'); option.value = typeof ref === 'string' ? ref : ref.name; $('revisions').append(option);}
        if (!revision && result.default_revision) $('revision').value = result.default_revision;
        for (const [name, value] of Object.entries(result.detected || {})) {
            const input = $('project-form').elements.namedItem(name);
            if (input && value !== null && value !== undefined) input.value = value;
        }
        showWarnings(result);
        bindText($('repository-hint'), () => t('仓库信息已读取。请确认分支和源版本，并填写目标版本。'));
        feedbackTranslated(() => t('仓库识别完成。版本信息可以继续编辑。'));
    }
    function readProject() {
        const values = Object.fromEntries(new FormData($('project-form')));
        for (const key of Object.keys(values)) values[key] = values[key].trim();
        delete values.local_workspace_mode;
        if (!values.source_revision) delete values.source_revision;
        for (const key of ['source_loader_version', 'target_loader_version']) if (!values[key]) delete values[key];
        return values;
    }
    const workspaceLabels = {git_worktree: 'Git 新分支与独立工作树', copy: '复制所选源码', direct: '直接修改所选目录'};
    function gitStatusText(git) {
        if (!git?.available) return git?.reason_translations?.[i18n.locale] || git?.reason || t('未检测到可用的 Git。');
        if (!git.is_repository) return git.reason_translations?.[i18n.locale] || git.reason || t('所选目录不是 Git 仓库。');
        const facts = [git.branch ? t("当前分支：{0}", git.branch) : t('当前分支：未检出'), git.dirty ? t('工作区有未提交修改') : t('工作区干净'), git.has_commits ? t('已有提交') : t('尚无提交')];
        return facts.join(' · ');
    }
    function renderLocalGitStatus() {
        const node = $('local-git-status');
        const git = state.localSource?.git;
        node.hidden = !git;
        node.classList.toggle('is-dirty', git?.dirty === true);
        node.replaceChildren();
        if (!git) return;
        node.append(element('strong', '', gitStatusText(git)));
        if (!git.can_branch) node.append(element('p', '', t("无法使用 Git 分支工作树：{0} 仍可复制源码，或选择直接修改原目录。", git.reason_translations?.[i18n.locale] || git.reason || t('当前仓库不满足创建新分支的条件。'))));
    }
    function clearWorkspaceErrors() {
        for (const id of ['workspace-branch-error', 'workspace-direct-error', 'workspace-mode-error']) {
            const node = $(id); node.hidden = true; setText(node, '');
        }
        $('local-workspace-branch').removeAttribute('aria-invalid');
        $('direct-workspace-confirmed').removeAttribute('aria-invalid');
    }
    function renderWorkspaceSettings() {
        const visible = sourceMode === 'local' && Boolean(state.localSource);
        $('local-workspace-settings').hidden = !visible;
        if (!visible) {
            document.querySelectorAll('#local-workspace-settings input').forEach(input => input.disabled = true);
            return;
        }
        $('direct-workspace-confirmed').disabled = false;
        const git = state.localSource?.git;
        const canBranch = git?.can_branch === true;
        if (!canBranch && state.workspaceMode === 'git_worktree') state.workspaceMode = 'copy';
        document.querySelectorAll('input[name="local_workspace_mode"]').forEach(input => {
            input.checked = input.value === state.workspaceMode;
            input.disabled = input.value === 'git_worktree' && !canBranch;
        });
        const branchMode = state.workspaceMode === 'git_worktree';
        $('workspace-branch-field').hidden = !branchMode;
        $('local-workspace-branch').disabled = !branchMode;
        $('local-workspace-branch').setAttribute('aria-required', String(branchMode));
        $('workspace-direct-warning').hidden = state.workspaceMode !== 'direct';
        $('workspace-git-reason').hidden = canBranch;
        setText($('workspace-git-reason'), canBranch ? '' : t("Git 分支工作树不可用：{0} 可选择复制源码，或确认后直接修改原目录。", git?.reason_translations?.[i18n.locale] || git?.reason || (!git?.available ? t('未检测到可用的 Git。') : !git?.is_repository ? t('所选目录不是 Git 仓库。') : !git?.has_commits ? t('仓库还没有提交，无法创建新分支。') : t('当前仓库不满足创建新分支的条件。'))));
        bindText($('workspace-summary'), () => t("本次策略：{0}{1}", t(workspaceLabels[state.workspaceMode]), branchMode && $('local-workspace-branch').value.trim() ? t(" · 分支 {0}", $('local-workspace-branch').value.trim()) : ''));
    }
    function branchNameIsValid(name) {
        if (!name || name !== name.trim() || name.startsWith('-') || name.startsWith('/') || name.endsWith('/') || name.endsWith('.') || name.includes('..') || name.includes('//') || name.includes('@{') || name === '@' || /[\s~^:?*\[\]\\]/.test(name)) return false;
        return name.split('/').every(part => part && !part.startsWith('.') && !part.endsWith('.lock'));
    }
    function validateWorkspaceSettings() {
        clearWorkspaceErrors();
        if (sourceMode !== 'local') return true;
        if (state.workspaceMode === 'git_worktree') {
            const branch = $('local-workspace-branch').value.trim();
            if (!branch) {
                bindText($('workspace-branch-error'), () => t('请输入新分支名称。')); $('workspace-branch-error').hidden = false;
                $('local-workspace-branch').setAttribute('aria-invalid', 'true'); $('local-workspace-branch').focus(); return false;
            }
            if (!branchNameIsValid(branch)) {
                bindText($('workspace-branch-error'), () => t('分支名称格式无效。请移除空格及 Git 禁止的符号，并确认各段不以点号开头。')); $('workspace-branch-error').hidden = false;
                $('local-workspace-branch').setAttribute('aria-invalid', 'true'); $('local-workspace-branch').focus(); return false;
            }
        }
        if (state.workspaceMode === 'direct' && !$('direct-workspace-confirmed').checked) {
            bindText($('workspace-direct-error'), () => t('直接修改原目录需要先确认上方提示。')); $('workspace-direct-error').hidden = false;
            $('direct-workspace-confirmed').setAttribute('aria-invalid', 'true'); $('direct-workspace-confirmed').focus(); return false;
        }
        return true;
    }
    function switchSourceMode(mode) {
        const fields = ['source-minecraft', 'source-loader', 'source-loader-version'];
        sourceDrafts[sourceMode] = Object.fromEntries(fields.map(id => [id, $(id).value]));
        sourceMode = mode;
        document.querySelectorAll('[data-source-mode]').forEach(section => {
            const active = section.dataset.sourceMode === mode;
            section.hidden = !active;
            section.querySelectorAll('input, button').forEach(input => input.disabled = !active);
        });
        for (const id of fields) $(id).value = sourceDrafts[mode]?.[id] ?? (id === 'source-loader' ? 'forge' : '');
        $('repository-warnings').hidden = true;
        warningDisplay = null;
        state.project = null;
        renderWorkspaceSettings();
        feedback('');
    }
    async function chooseLocalSource() {
        if (!window.modport?.selectSourceDirectory) throw new Error(t('请在 ModPort 桌面窗口中选择本地文件夹。'));
        const selected = await window.modport.selectSourceDirectory();
        if (selected.cancelled || sourceMode !== 'local') return;
        if (!selected.token) throw new Error(t('本机服务没有返回有效的源码选择凭据。'));
        state.localSource = {git: selected.git || null};
        state.workspaceMode = selected.git?.can_branch === true ? 'git_worktree' : 'copy';
        $('local-workspace-branch').value = '';
        $('direct-workspace-confirmed').checked = false;
        clearWorkspaceErrors(); renderLocalGitStatus();
        $('local-source-token').value = selected.token;
        $('local-source-path').value = selected.display_path;
        if (!$('project-name').value.trim()) $('project-name').value = selected.name;
        for (const id of ['source-minecraft', 'source-loader-version']) $(id).value = '';
        $('source-loader').value = 'forge';
        for (const [name, value] of Object.entries(selected.detected || {})) {
            const input = $('project-form').elements.namedItem(name);
            if (input && value !== null && value !== undefined) input.value = value;
        }
        showWarnings(selected, selected.detected?.source_minecraft ? [] : ['未能可靠识别 Minecraft 版本，请手动填写。']);
        state.project = null;
        renderWorkspaceSettings();
        feedbackTranslated(() => t('已选择本地代码库。请确认源版本和本地工作区策略。'));
    }
    function renderTask(item) {
        const row = element('div', `task-row ${Object.hasOwn(labels, item.state) ? `state-${item.state}` : ''}`);
        row.dataset.taskId = item.id;
        const header = element('div', 'task-heading'); header.append(element('span', 'task-label', item.label || item.id), badge(item.state)); row.append(header);
        if (['failed', 'waiting', 'cancelled'].includes(item.state)) {
            row.append(element('p', 'task-identity', t('任务 ID：{0}', item.id)));
        }
        if (item.detail) row.append(element('p', 'task-detail', item.detail));
        if (item.error_code && item.error_code !== item.detail) row.append(element('p', 'task-error-code', t('错误码：{0}', item.error_code)));
        const agents = element('div', 'task-agents');
        for (const [label, value] of [['agents', item.active_agents], ['subagents', item.active_subagents]]) {const text = element('span', '', `${label} `); text.append(element('strong', '', number(value))); agents.append(text);}
        row.append(agents);
        if (item.counts) row.append(element('div', 'task-counts', `${item.counts.label || t('已完成')} ${number(item.counts.completed)} / ${number(item.counts.total)}`));
        return row;
    }
    function taskDisclosure(key, label, items) {
        const details = element('details', 'task-fold'); details.dataset.fold = key; details.open = state.folds.get(key) || false;
        const summary = element('summary', '', label); summary.id = `fold-${key}`; details.append(summary, ...items.map(renderTask));
        details.addEventListener('toggle', () => state.folds.set(key, details.open));
        return details;
    }
    function renderStages(stages) {
        const focusId = document.activeElement?.id;
        const scrolls = [...$('stage-board').children].map(stage => stage.querySelector('.stage-body')?.scrollTop || 0);
        const root = $('stage-board'); root.replaceChildren();
        const definitions = [['preparation', 'Preparation', t('准备')], ['implementation', 'Implementation', t('实施')], ['testing', 'Testing', t('测试')]];
        definitions.forEach(([key, english, chinese], index) => {
            const stage = stages?.[key]; const panel = element('section', 'stage');
            const display = stagePresentation(stage);
            const header = element('div', 'stage-header'); header.append(element('span', 'section-number', `0${index + 1}`), element('h2', '', i18n.locale === 'en' ? english : chinese), badge(display.state));
            if (display.failed) header.append(element('span', 'stage-failure-count', t('{0} 项失败', display.failed)));
            const body = element('div', 'stage-body');
            if (!stage) body.append(element('p', 'empty', t('服务尚未提供此组任务。')));
            else {
                const parts = partitionItems(stage.items);
                body.append(...parts.attention.map(renderTask), ...parts.active.map(renderTask));
                if (parts.overflow.length) body.append(taskDisclosure(`${key}-active`, t("展开另外 {0} 个进行中的任务", parts.overflow.length), parts.overflow));
                if (parts.completed.length) body.append(taskDisclosure(`${key}-completed`, t("已完成 {0} 项 · 展开查看", parts.completed.length), parts.completed));
                if (!body.children.length) body.append(element('p', 'empty', ['completed', 'succeeded', 'failed', 'cancelled'].includes(stage.state) ? t('阶段已结束。服务未提供展开项。') : t('等待任务开始。未开始的任务会暂时隐藏。')));
            }
            panel.append(header, body); root.append(panel);
            body.scrollTop = scrolls[index] || 0;
        });
        if (focusId?.startsWith('fold-')) $(focusId)?.focus({preventScroll: true});
    }
    let chatScrollTop = 0, chatFollowEnd = true;
    function rememberChatScroll() {
        const messages = $('messages');
        if ($('run-overview').open || !messages.clientHeight) return;
        chatScrollTop = messages.scrollTop;
        chatFollowEnd = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 50;
    }
    function restoreChatScroll() {
        const messages = $('messages');
        if (messages.clientHeight) messages.scrollTop = chatFollowEnd ? messages.scrollHeight : chatScrollTop;
    }
    function syncRunView() {
        // Native toggle fires after layout changes; keep the last visible scroll snapshot.
        const expanded = $('run-overview').open;
        $('supervisor-panel').hidden = expanded;
        bindText($('overview-hint'), () => $('run-overview').open
            ? t('收起总览，返回对话') : t('展开查看用量、工作区与任务'));
        $('open-supervisor-chat').setAttribute('aria-pressed', String(!expanded));
        if (!expanded) requestAnimationFrame(restoreChatScroll);
    }
    function openSupervisorChat() {
        $('run-overview').open = false;
        syncRunView();
        if (!$('chat-message').disabled) $('chat-message').focus({preventScroll: true});
        else $('supervisor-heading').focus({preventScroll: true});
    }
    $('supervisor-heading').tabIndex = -1;
    $('run-overview').addEventListener('toggle', syncRunView);
    $('open-supervisor-chat').addEventListener('click', openSupervisorChat);
    $('return-supervisor-chat').addEventListener('click', openSupervisorChat);
    $('messages').addEventListener('scroll', rememberChatScroll);
    syncRunView();
    function renderMessages(messages) {
        const key = JSON.stringify(messages || []);
        if (key === state.messagesKey) return;
        state.messagesKey = key;
        rememberChatScroll();
        const root = $('messages');
        root.replaceChildren();
        for (const message of messages || []) {
            const row = element('article', `chat-entry ${message.role === 'user' ? 'user' : ''}`);
            const header = element('header'); header.append(element('span', '', message.role === 'user' ? t('YOU / 你') : 'SUPERVISOR'), element('span', 'message-state', t(labels[message.state] || message.state || '')));
            row.append(header, element('p', '', message.content)); root.append(row);
        }
        if (!root.children.length) root.append(element('p', 'empty', t('还没有对话。可以询问进展，或补充说明。')));
        restoreChatScroll();
    }
    function renderRunWorkspace(workspace) {
        const panel = $('run-workspace');
        panel.hidden = !workspace || typeof workspace.path !== 'string' || !workspace.path;
        if (panel.hidden) return;
        bindText($('run-workspace-heading'), () => t(workspaceLabels[workspace.mode]) || workspace.mode || t('工作区'));
        bindText($('run-workspace-path'), () => t("实例目录：{0}", workspace.path));
        const original = $('run-workspace-original');
        original.hidden = !workspace.original_path || workspace.original_path === workspace.path;
        setText(original, original.hidden ? '' : t("所选源码：{0}", workspace.original_path));
        const branch = $('run-workspace-branch');
        branch.hidden = !workspace.branch;
        setText(branch, workspace.branch ? t("分支 {0}", workspace.branch) : '');
        $('open-workspace').disabled = typeof window.modport?.openWorkspace !== 'function';
    }
    function renderSync() {
        bindText($('sync-state'), () => state.syncError ? t('连接中断 · 显示上次状态') : state.syncedAt ? t('已同步') : t('正在读取'));
        bindText($('sync-time'), () => state.syncedAt ? t('更新于 {0}', new Date(state.syncedAt).toLocaleTimeString(i18n.locale)) : '');
    }
    function renderRun(run) {
        state.run = run;
        $('run-refresh').disabled = false;
        $('run-contributions-button').disabled = !state.runId;
        setText($('run-heading'), run.project_name || run.id);
        const status = badge(run.status); $('run-status').className = status.className; setText($('run-status'), status.textContent);
        setText($('run-id'), run.id);
        setText($('elapsed-time'), duration(run.elapsed_seconds));
        setText($('time-limit'), ` / ${duration(run.budget?.max_seconds)}`);
        const used = run.budget?.used_tokens;
        setText($('used-tokens'), number(used));
        setText($('token-limit'), ` / ${number(run.budget?.max_tokens)}${run.budget?.token_usage_complete === false ? knownNumber(used) ? t(' · 已知用量，统计未完整') : t(' · 用量统计未完整') : ''}`);
        renderSync();
        $('run-notice').hidden = !run.notice;
        setText($('run-notice'), run.notice || '');
        renderRunWorkspace(run.workspace);
        $('cancel-button').disabled = ['succeeded', 'completed', 'failed', 'cancelled', 'cancelling'].includes(run.status);
        renderStages(run.stages); renderMessages(run.messages);
        const ended = endedRun(run);
        setText($('supervisor-status'), ended ? t('实例已结束') : run.supervisor?.busy ? t('正在处理对话') : t('可发送消息'));
        $('chat-send').disabled = ended || run.supervisor?.busy === true;
        $('chat-message').disabled = ended;
    }
    function schedulePoll() {
        clearTimeout(state.polling);
        if (state.screen === 'run' && state.runId && !document.hidden) state.polling = setTimeout(refreshRun, 4000);
    }
    async function refreshRun() {
        if (!state.runId) return;
        if (state.fetching) {state.fetchAgain = true; return;}
        state.fetching = true; const id = state.runId; const epoch = state.requestEpoch;
        try {
            const run = await request('GET', `/api/runs/${encodeURIComponent(id)}`);
            if (state.runId === id && epoch === state.requestEpoch) {state.syncedAt = Date.now(); state.syncError = false; renderRun(run); if ($('feedback').dataset.runError === 'true') {feedback(''); delete $('feedback').dataset.runError;}}
        } catch (error) {
            if (state.runId === id && epoch === state.requestEpoch) {state.syncError = true; renderSync(); feedbackTranslated(() => t("无法更新实例：{0}。可点击刷新重试。", error.message), 'error'); $('feedback').dataset.runError = 'true';}
        } finally {
            state.fetching = false;
            if (state.fetchAgain) {state.fetchAgain = false; void refreshRun();} else schedulePoll();
        }
    }
    function renderEmptyRun() {
        clearTimeout(state.polling);
        setText($('run-heading'), t('尚未创建迁移实例'));
        setText($('run-id'), ''); setText($('run-status'), t('未开始'));
        $('run-status').className = 'badge';
        for (const id of ['elapsed-time', 'used-tokens']) setText($(id), '—');
        for (const id of ['time-limit', 'token-limit', 'sync-time']) setText($(id), '');
        setText($('sync-state'), t('尚无执行数据'));
        setText($('run-notice'), t('可以先检查执行界面；配置完成并开始迁移后，这里会显示真实任务和进度。'));
        $('run-notice').hidden = false; $('run-workspace').hidden = true;
        $('cancel-button').disabled = true; $('run-refresh').disabled = true; $('run-contributions-button').disabled = true;
        $('chat-send').disabled = true; $('chat-message').disabled = true;
        setText($('supervisor-status'), t('开始迁移后可对话'));
        $('messages').replaceChildren(element('p', 'empty', t('Supervisor 对话将在实例启动后启用。')));
        renderStages(Object.fromEntries(['preparation', 'implementation', 'testing'].map(key => [key, {state: 'pending', items: []}])));
    }
    async function openRun(id) {
        if (!id) {renderEmptyRun(); showScreen('run'); return;}
        if (state.runId !== id) {state.run = null; state.messagesKey = ''; chatScrollTop = 0; chatFollowEnd = true; state.syncedAt = null; state.syncError = false; state.folds.clear(); state.requestEpoch += 1;}
        state.runId = id;
        $('run-contributions-button').disabled = false;
        $('run-refresh').disabled = false;
        location.hash = `run=${encodeURIComponent(id)}`;
        showScreen('run'); feedback('');
        bindText($('run-heading'), () => t('正在读取实例…')); setText($('run-id'), id);
        if (!state.run) {
            $('stage-board').replaceChildren(); $('messages').replaceChildren(element('p', 'empty', t('正在读取对话…')));
            bindText($('elapsed-time'), () => t('未知')); bindText($('used-tokens'), () => t('未知')); setText($('time-limit'), ''); setText($('token-limit'), '');
            $('run-status').className = 'badge'; bindText($('run-status'), () => t('读取中')); $('run-notice').hidden = true; $('cancel-button').disabled = true; $('chat-send').disabled = true;
            bindText($('sync-state'), () => t('正在读取')); setText($('sync-time'), ''); bindText($('supervisor-status'), () => t('读取中'));
        }
        await refreshRun();
    }
    $('project-form').addEventListener('submit', event => {
        event.preventDefault();
        if (sourceMode === 'local' && !$('local-source-token').value) {feedbackTranslated(() => t('请先选择本地源码文件夹。'), 'warning'); $('choose-local-source').focus(); return;}
        state.project = readProject();
        setText($('project-summary'), `${state.project.project_name} · ${state.project.source_minecraft} ${state.project.source_loader} → ${state.project.target_minecraft} ${state.project.target_loader}`);
        renderWorkspaceSettings();
        feedback(''); location.hash = 'settings'; showScreen('settings');
    });
    $('identify-button').addEventListener('click', () => busy($('identify-button'), t('读取仓库…'), identifyRepository));
    document.querySelectorAll('input[name="source_mode"]').forEach(input => input.addEventListener('change', () => {if (input.checked) switchSourceMode(input.value);}));
    $('choose-local-source').addEventListener('click', () => busy($('choose-local-source'), t('读取本地源码…'), chooseLocalSource));
    document.querySelectorAll('input[name="local_workspace_mode"]').forEach(input => input.addEventListener('change', () => {
        if (!input.checked) return;
        state.workspaceMode = input.value;
        $('direct-workspace-confirmed').checked = false;
        clearWorkspaceErrors(); renderWorkspaceSettings();
    }));
    $('local-workspace-branch').addEventListener('input', () => {
        if (!$('workspace-branch-error').hidden) clearWorkspaceErrors();
        bindText($('workspace-summary'), () => t("本次策略：{0}{1}", t(workspaceLabels[state.workspaceMode]), $('local-workspace-branch').value.trim() ? t(" · 分支 {0}", $('local-workspace-branch').value.trim()) : ''));
    });
    $('direct-workspace-confirmed').addEventListener('change', () => {
        if ($('direct-workspace-confirmed').checked) {$('workspace-direct-error').hidden = true; $('direct-workspace-confirmed').removeAttribute('aria-invalid');}
    });
    $('settings-form').addEventListener('submit', event => {
        event.preventDefault();
        if (!$('project-form').checkValidity() || (sourceMode === 'local' && !$('local-source-token').value)) {
            location.hash = 'project'; showScreen('project');
            feedbackTranslated(() => t('启动前请补全项目名称、源码和版本信息。'), 'warning');
            $('project-form').reportValidity(); return;
        }
        state.project = readProject();
        if (!state.bootstrap?.environment?.ready) {feedbackTranslated(() => t('请先确认项目，并完成运行环境准备。'), 'warning'); return;}
        if (!validateWorkspaceSettings()) {location.hash = 'project'; showScreen('project'); document.querySelector('#local-workspace-settings [aria-invalid="true"]')?.focus(); return;}
        void busy($('launch-button'), t('正在启动…'), async () => {
            const maxSeconds = Math.ceil(Number($('max-hours').value) * 3600);
            const maxTokens = Number($('max-tokens').value);
            if (!Number.isSafeInteger(maxSeconds) || maxSeconds <= 0 || !Number.isSafeInteger(maxTokens) || maxTokens <= 0) throw new Error(t('请输入有效的总时间和 Token 限制。'));
            const workspace = state.project.source_mode === 'local' ? {
                local_workspace_mode: state.workspaceMode,
                direct_workspace_confirmed: state.workspaceMode === 'direct' && $('direct-workspace-confirmed').checked,
                ...(state.workspaceMode === 'git_worktree' ? {local_branch_name: $('local-workspace-branch').value.trim()} : {})
            } : {};
            const run = await request('POST', '/api/runs', {...state.project, ...workspace, max_seconds: maxSeconds, max_tokens: maxTokens, model_config: clone(state.modelConfig)});
            if (!run.id) throw new Error(t('服务未返回实例编号。'));
            await openRun(run.id);
        });
    });
    $('chat-form').addEventListener('submit', event => {
        event.preventDefault(); const message = $('chat-message').value.trim(); const id = state.runId;
        if (!message || !id || !state.run || endedRun(state.run) || state.run.supervisor?.busy) return;
        void busy($('chat-send'), t('发送中…'), async () => {
            const result = await request('POST', `/api/runs/${encodeURIComponent(id)}/chat`, {message});
            if (state.runId === id) {$('chat-message').value = ''; feedbackTranslated(() => t('消息已接收，由 Supervisor 处理。')); await refreshRun();}
            return result;
        }).then(() => {$('chat-send').disabled = endedRun(state.run) || state.run?.supervisor?.busy === true || !state.run;});
    });
    $('chat-message').addEventListener('keydown', event => {if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {event.preventDefault(); $('chat-form').requestSubmit();}});
    $('run-contributions-button').addEventListener('click', () => {
        if (state.runId) window.dispatchEvent(new CustomEvent('modport:contributions-open', {detail: {runId: state.runId}}));
    });
    $('cancel-button').addEventListener('click', () => {setText($('cancel-instance'), `${state.run?.project_name || ''} · ${state.runId}`); $('cancel-dialog').dataset.runId = state.runId; $('cancel-dialog').showModal();});
    $('confirm-cancel').addEventListener('click', () => busy($('confirm-cancel'), t('正在请求…'), async () => {
        const id = $('cancel-dialog').dataset.runId;
        await request('POST', `/api/runs/${encodeURIComponent(id)}/cancel`, {confirmed: true});
        $('cancel-dialog').close(); feedbackTranslated(() => t('已提交取消请求。正在等待服务返回任务与清理状态。'));
        if (state.runId === id) await refreshRun();
    }));
    $('model-setup-back').addEventListener('click', () => changeModelStep(modelSetupStep - 1));
    $('model-setup-next').addEventListener('click', () => changeModelStep(modelSetupStep + 1));
    document.querySelectorAll('[data-model-step]').forEach(button => button.addEventListener('click', () => changeModelStep(Number(button.dataset.modelStep))));
    $('advanced-model-settings').addEventListener('toggle', () => {
        if (!modelSetup) return;
        if ($('advanced-model-settings').open) {
            try {commitGuidedSetup(false); renderProviders(); renderModels();}
            catch (error) {$('advanced-model-settings').open = false; setText($('model-settings-error'), modelSetupError(error)); $('model-settings-error').hidden = false;}
        } else if (!guidedSetupDirty) modelSetup = window.ModPortModelSetup.create(providerDraft, state.modelConfig);
        renderGuidedSetup(); renderFallback();
    });
    $('model-settings-button').addEventListener('click', event => void openModelSettings(event.currentTarget));
    document.querySelectorAll('[data-open-model-settings]').forEach(button => button.addEventListener('click', () => void openModelSettings(button)));
    $('provider-settings').addEventListener('change', renderFallback);
    $('add-provider').addEventListener('click', () => {
        let index = 1;
        while (providerDraft.some(provider => provider.id === `custom${index}`)) index++;
        providerDraft.push({id: `custom${index}`, name: '', api_type: 'openai-compatible', base_url: '', models: [{id: '', context_window: '', max_output_tokens: '', reasoning_efforts: []}]});
        renderProviders(); $(`provider-${providerDraft.length - 1}-name`).focus();
    });
    $('model-settings-form').addEventListener('submit', event => {
        event.preventDefault();
        if (!$('advanced-model-settings').open && modelSetupStep < 2 && fallbackSetup?.mode === 'preserve') {changeModelStep(modelSetupStep + 1); return;}
        try {
            commitGuidedSetup();
        } catch (error) {
            setText($('model-settings-error'), modelSetupError(error)); $('model-settings-error').hidden = false; return;
        }
        // Enter can submit while a model input still owns focus, before blur/change.
        const active = document.activeElement;
        if (active?.matches('input') && event.currentTarget.contains(active)) active.dispatchEvent(new Event('change'));
        let submission;
        try {submission = window.ModPortFallbackSetup.apply(fallbackSetup, providerDraft, state.modelConfig);}
        catch (error) {
            setText($('model-settings-error'), modelSetupError(error)); $('model-settings-error').hidden = false;
            $('model-settings-error').scrollIntoView({block: 'nearest'}); return;
        }
        const button = $('save-model-settings');
        if (button.disabled) return;
        const dialog = $('model-settings-dialog');
        const controls = [...dialog.querySelectorAll('button, input, select')];
        const disabled = controls.map(control => control.disabled);
        controls.forEach(control => control.disabled = true);
        dialog.dataset.saving = 'true'; bindText(button, () => t('正在保存…'));
        $('model-settings-error').hidden = true;
        const providers = submission.providers;
        void request('POST', '/api/model-settings', {providers, model_config: submission.model_config}).then(settings => {
            state.modelConfig = clone(settings.model_config); modelBackup = null;
            modelSummary(); dialog.close(); feedbackTranslated(() => t('模型配置已保存，新实例将使用这些设置。'));
        }).catch(error => {
            setText($('model-settings-error'), error.message || t('配置保存失败，请重试。'));
            $('model-settings-error').hidden = false;
            $('model-settings-error').scrollIntoView({block: 'nearest'});
        }).finally(() => {
            controls.forEach((control, index) => control.disabled = disabled[index]);
            delete dialog.dataset.saving; bindText(button, () => t('保存配置'));
        });
    });
    $('model-settings-dialog').addEventListener('cancel', event => {
        if ($('model-settings-dialog').dataset.saving) event.preventDefault();
    });
    $('model-settings-dialog').addEventListener('close', () => {
        if ($('model-settings-dialog').open) return;
        if (modelBackup) state.modelConfig = modelBackup;
        modelBackup = null; providerDraft = [];
        modelSetup = null; fallbackSetup = null; guidedSetupDirty = false;
        $('provider-settings').replaceChildren();
        renderModels(); modelSummary();
    });
    $('updates-button').addEventListener('click', () => $('updates-dialog').showModal());
    document.querySelectorAll('[data-open-updates]').forEach(button => button.addEventListener('click', () => $('updates-dialog').showModal()));
    $('updates-check').addEventListener('click', async () => {
        updateStatus = {...updateStatus, status: 'checking'}; renderUpdates();
        try {updateStatus = await window.modport.checkForUpdates();}
        catch {updateStatus = {...updateStatus, status: 'error'};}
        renderUpdates();
    });
    $('updates-download').addEventListener('click', () => busy($('updates-download'), t('正在打开…'), async () => {
        await window.modport.openUpdateDownload();
    }));
    $('environment-button').addEventListener('click', () => $('environment-dialog').showModal());
    document.querySelectorAll('[data-close-dialog]').forEach(button => button.addEventListener('click', () => $(button.dataset.closeDialog).close()));
    document.querySelectorAll('[data-screen]').forEach(button => button.addEventListener('click', () => {
        if (button.dataset.screen === 'run') {location.hash = state.runId ? `run=${encodeURIComponent(state.runId)}` : 'run'; void openRun(state.runId);}
        else {location.hash = button.dataset.screen; showScreen(button.dataset.screen);}
    }));
    document.querySelector('.brand').addEventListener('click', () => showScreen('project'));
    $('new-project').addEventListener('click', () => {location.hash = 'project'; showScreen('project'); void bootstrap(false).catch(error => feedback(error.message, 'error'));});
    $('run-refresh').addEventListener('click', () => busy($('run-refresh'), t('读取中…'), refreshRun));
    $('open-workspace').addEventListener('click', () => busy($('open-workspace'), t('正在打开…'), async () => {
        if (!state.runId || typeof window.modport?.openWorkspace !== 'function') throw new Error(t('只能在 ModPort 桌面窗口中打开实例工作区。'));
        await window.modport.openWorkspace(state.runId);
        feedbackTranslated(() => t('已在文件管理器中打开此实例工作区。'));
    }));
    $('refresh-recent').addEventListener('click', () => busy($('refresh-recent'), t('读取中…'), () => bootstrap(!state.modelConfig)));
    $('environment-refresh').addEventListener('click', () => busy($('environment-refresh'), t('检查中…'), () => bootstrap(!state.modelConfig)));
    document.addEventListener('visibilitychange', () => {if (document.hidden) clearTimeout(state.polling); else if (state.screen === 'run') void refreshRun();});
    window.addEventListener('hashchange', () => {
        const hash = location.hash.slice(1);
        if (hash.startsWith('run=')) {const id = decodeURIComponent(hash.slice(4)); if (id && (id !== state.runId || state.screen !== 'run')) void openRun(id);}
        else if (hash === 'project') showScreen('project', false);
        else if (hash === 'settings') showScreen('settings', false);
        else if (hash === 'run' && !state.runId) {renderEmptyRun(); showScreen('run', false);}
    });
    $('language-select').addEventListener('change', async event => {
        const picker = event.currentTarget;
        const previous = i18n.locale;
        picker.disabled = true;
        try {
            const selected = window.modport?.setLanguage ? await window.modport.setLanguage(picker.value) : picker.value;
            i18n.setLocale(selected, true);
            picker.value = i18n.locale;
            i18n.apply(document);
            state.requestEpoch += 1;
            state.messagesKey = '';
            // Keep project inputs, model drafts, workspace choices and budgets intact.
            renderLocalGitStatus(); renderWorkspaceSettings(); renderWarnings();
            if (state.bootstrap) {renderModels(); modelSummary();}
            if ($('model-settings-dialog').open) {renderProviders(); renderGuidedSetup(); renderFallback();}
            renderUpdates();
            if (state.run) renderRun(state.run);
            else if (state.screen === 'run') renderEmptyRun();
            await bootstrap(!state.modelConfig);
            renderModels(); modelSummary();
            if (state.screen === 'run' && state.runId) await refreshRun();
        } catch (error) {
            // A failed service refresh must not roll back a successfully saved language.
            picker.value = i18n.locale || previous;
            feedback(error.message || String(error), 'error');
        } finally { picker.disabled = false; }
    });
    async function start() {
        try {
            if (window.modport?.getLanguage) i18n.setLocale(await window.modport.getLanguage());
            $('language-select').value = i18n.locale;
            i18n.apply(document);
            void initializeUpdates();
            await bootstrap();
            if (location.hash.startsWith('#run=')) await openRun(decodeURIComponent(location.hash.slice(5)));
            else if (location.hash === '#run') {renderEmptyRun(); showScreen('run', false);}
            else if (location.hash === '#settings') showScreen('settings', false);
            renderWorkspaceSettings();
        } catch (error) {
            bindText($('environment-label'), () => t('服务连接失败'));
            $('recent-runs').replaceChildren(element('p', 'empty', t('无法读取本机实例。请检查服务并点击刷新。')));
            feedbackTranslated(() => t("无法连接 ModPort：{0}。运行环境和实例列表中可重试。", error.message), 'error');
        }
    }
    void start();
}());
