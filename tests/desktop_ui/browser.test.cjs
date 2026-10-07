/* A local fixture service exercises the real renderer and HTTP boundary.
   This verifies interface behavior; it does not establish migration acceptance. */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');
const os = require('node:os');
const staticRoot = path.resolve(__dirname, '../../src/modport/desktop_static');
const browserCache = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(os.homedir(), '.cache', 'ms-playwright');
const cachedBrowsers = fs.existsSync(browserCache)
    ? fs.readdirSync(browserCache).filter(name => /^chromium-\d+$/.test(name))
        .flatMap(name => ['chrome-linux64/chrome', 'chrome-linux/chrome'].map(binary => path.join(browserCache, name, binary)))
    : [];
const candidates = [process.env.MODPORT_TEST_CHROMIUM, ...cachedBrowsers, '/usr/bin/chromium', '/usr/bin/google-chrome'].filter(Boolean);
const chromium = candidates.find(candidate => fs.existsSync(candidate));

test('desktop renderer: repository → config → run, task visibility, chat, cancel and reconnect', {skip: !chromium, timeout: 45000}, async () => {
    const modelConfig = {default: {model: 'test-default', reasoning_effort: 'max'}, roles: {planner: {model: 'test-planner', reasoning_effort: 'high', fallback: {model: 'test/fallback', reasoning_effort: 'low'}}, coder: {model: 'test-author', reasoning_effort: 'high'}, supervisor: {model: 'test-supervisor', reasoning_effort: 'max'}}, stages: {code_review: {model: 'test-stage-review', reasoning_effort: 'medium'}}};
    let modelSettings = {providers: [], model_config: modelConfig};
    const requests = []; let offline = false; let cancelled = false; let showWorkspace = true; let runNotice = null; const messages = [];
    const runRecords = new Map([['browser-test', {workspace: {mode: 'copy', path: '/fixture/workspaces/browser-test'}}]]); let runSequence = 0; let runFetchSequence = 0;
    const stageItems = [{id: 'pending-test', label: '尚未开始的测试', state: 'pending', active_agents: 0, active_subagents: null}, {id: 'done-test', label: '已完成测试', state: 'completed', active_agents: 0, active_subagents: 0}, {id: 'failed-test', label: '必须可见的失败', state: 'failed', detail: '<script>this is plain text</script>', active_agents: 0, active_subagents: null}, ...Array.from({length: 6}, (_, i) => ({id: `active-${i}`, label: `独立任务 ${i}`, state: 'running', active_agents: i + 1, active_subagents: i}))];
    const server = http.createServer(async (req, res) => {
        const chunks = []; for await (const chunk of req) chunks.push(chunk);
        const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
        requests.push({method: req.method, path: req.url, body});
        if (req.url.startsWith('/api/')) {
            res.setHeader('Content-Type', 'application/json');
            const send = data => res.end(JSON.stringify(data));
            if (req.url === '/api/bootstrap') return send({workflow_version: 'test-current', platform: 'test', defaults: {max_seconds: 7200, max_tokens: 10000}, model_config: modelConfig, roles: [{id: 'default', label: '默认', group: 'author', target: 'default'}, {id: 'planner', label: '规划', group: 'author', target: 'role'}, {id: 'coder', label: '编写', group: 'author', target: 'role'}, {id: 'supervisor', label: '监督', group: 'review', target: 'role'}, {id: 'stage:code_review', label: '代码审查', group: 'review', target: 'stage', stage: 'code_review'}], environment: {ready: true, checks: [{id: 'python', label: 'Python', ready: true, detail: 'test environment'}]}, recent_runs: [{id: 'browser-test', project_name: '浏览器测试实例', status: 'running'}]});
            if (req.url === '/api/model-settings') {
                if (req.method === 'POST') modelSettings = {model_config: body.model_config, providers: body.providers.map(({api_key, ...provider}) => ({...provider, api_key_configured: Boolean(api_key)}))};
                return send(modelSettings);
            }
            if (req.url === '/api/repository') return send({branches: ['main', 'release'], tags: ['test-tag'], default_revision: 'main', detected: {source_minecraft: '1.20.1', source_loader: 'forge', source_loader_version: '47.test'}, warnings: ['测试仓库版本需确认']});
            if (req.url === '/api/runs') {
                const id = `browser-test-${++runSequence}`;
                const workspace = {mode: body.local_workspace_mode || 'copy', path: `/fixture/workspaces/${id}`, original_path: '/fixture/闭源 Mod', ...(body.local_branch_name ? {branch: body.local_branch_name} : {})};
                runRecords.set(id, {body, workspace});
                return send({id, project_name: body.project_name, status: 'running'});
            }
            if (req.url.endsWith('/chat')) {messages.push({id: 'message-test', role: 'user', content: body.message, state: 'queued'}); return send(messages.at(-1));}
            if (req.url.endsWith('/cancel')) {cancelled = body.confirmed; return send({status: 'cancelling'});}
            const runMatch = req.url.match(/^\/api\/runs\/([^/]+)$/);
            if (runMatch) {
                if (offline) {res.statusCode = 503; return send({error: '测试连接中断'});}
                const id = decodeURIComponent(runMatch[1]); const record = runRecords.get(id);
                if (!record) {res.statusCode = 404; return send({error: 'unknown test run'});}
                return send({id, project_name: id === 'browser-test' ? '已有实例恢复测试' : '浏览器测试实例', status: cancelled ? 'cancelled' : 'running', workspace: showWorkspace ? record.workspace : null, elapsed_seconds: 61 + ++runFetchSequence, budget: {max_seconds: 7200, max_tokens: 10000, used_tokens: null, token_usage_complete: false}, stages: {preparation: {state: 'failed', items: [{id: 'codemod', label: '应用迁移规则', state: 'failed', detail: '', error_code: 'codemod_rules_unavailable', active_agents: 0, active_subagents: null}, {id: 'source', label: '读取源码', state: 'completed', active_agents: 0, active_subagents: 0}]}, implementation: {state: 'running', items: stageItems}, testing: {state: 'pending', items: []}}, messages, supervisor: {busy: false}, notice: runNotice});
            }
            res.statusCode = 404; return send({error: 'test route not found'});
        }
        const name = req.url === '/' ? 'index.html' : req.url.slice(1).split('?')[0];
        if (!['index.html', 'app.js', 'style.css', 'en.js', 'i18n.js', 'model_setup.js', 'fallback_setup.js', 'wiki_contributions.js'].includes(name)) {res.statusCode = 404; return res.end();}
        res.setHeader('Content-Type', name.endsWith('.js') ? 'text/javascript' : name.endsWith('.css') ? 'text/css' : 'text/html');
        res.end(fs.readFileSync(path.join(staticRoot, name)));
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'modport-ui-browser-'));
    const browser = spawn(chromium, ['--headless', '--no-sandbox', '--disable-dev-shm-usage', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--window-size=1440,1000', 'about:blank'], {stdio: ['ignore', 'ignore', 'pipe']});
    let socket;
    try {
        const address = await new Promise((resolve, reject) => {
            let stderr = ''; const timer = setTimeout(() => reject(new Error(`Chromium startup timeout: ${stderr.slice(-1000)}`)), 12000);
            browser.stderr.on('data', chunk => {stderr += chunk; const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (match) {clearTimeout(timer); resolve(match[1]);}});
            browser.once('error', error => {clearTimeout(timer); reject(new Error(`Chromium could not start: ${error.message}; ${stderr.slice(-1000)}`));});
            browser.once('exit', code => {clearTimeout(timer); reject(new Error(`Chromium exited: ${code}; ${stderr.slice(-1000)}`));});
        });
        const debugOrigin = new URL(address); debugOrigin.protocol = 'http:';
        let pageTarget;
        const pageDeadline = Date.now() + 8000;
        while (!pageTarget && Date.now() < pageDeadline) {
            const targets = await (await fetch(`${debugOrigin.origin}/json/list`)).json();
            pageTarget = targets.find(target => target.type === 'page');
            if (!pageTarget) await new Promise(resolve => setTimeout(resolve, 50));
        }
        assert(pageTarget, 'Chromium did not expose a page target');
        socket = new WebSocket(pageTarget.webSocketDebuggerUrl);
        await new Promise((resolve, reject) => {socket.addEventListener('open', resolve, {once: true}); socket.addEventListener('error', reject, {once: true});});
        let counter = 0; const pending = new Map(); const pageErrors = [];
        socket.addEventListener('message', ({data}) => {const response = JSON.parse(data); if (response.method === 'Runtime.exceptionThrown') pageErrors.push(response.params.exceptionDetails); if (response.id && pending.has(response.id)) {const {resolve, reject} = pending.get(response.id); pending.delete(response.id); response.error ? reject(new Error(response.error.message)) : resolve(response.result);}});
        function command(method, params = {}) {return new Promise((resolve, reject) => {const id = ++counter; pending.set(id, {resolve, reject}); socket.send(JSON.stringify({id, method, params}));});}
        async function evaluate(expression) {const result = await command('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true}); if (result.exceptionDetails) throw new Error(result.exceptionDetails.text); return result.result.value;}
        async function waitFor(expression) {const deadline = Date.now() + 5000; while (Date.now() < deadline) {if (await evaluate(expression)) return; await new Promise(resolve => setTimeout(resolve, 50));} throw new Error(`Timed out: ${expression}`);}
        async function clickVisible(selector) {
            const target = await evaluate(`(() => {
                const element = document.querySelector(${JSON.stringify(selector)});
                if (!element?.checkVisibility()) throw new Error('Control is hidden: ' + ${JSON.stringify(selector)});
                element.scrollIntoView({block: 'nearest'});
                const rect = element.getBoundingClientRect();
                const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
                if (x < 0 || x >= innerWidth || y < 0 || y >= innerHeight || !element.contains(document.elementFromPoint(x, y)))
                    throw new Error('Control is outside viewport or covered: ' + ${JSON.stringify(selector)});
                return {x, y};
            })()`);
            await command('Input.dispatchMouseEvent', {type: 'mousePressed', button: 'left', clickCount: 1, ...target});
            await command('Input.dispatchMouseEvent', {type: 'mouseReleased', button: 'left', clickCount: 1, ...target});
        }
        async function refreshRun() {
            const elapsedBefore = await evaluate("document.getElementById('elapsed-time').textContent");
            await clickVisible('#run-refresh');
            await waitFor(`document.getElementById('elapsed-time').textContent !== ${JSON.stringify(elapsedBefore)}`);
        }
        async function overview(open) {
            if (await evaluate("document.getElementById('run-overview').open") !== open)
                await clickVisible('#run-overview > summary');
            await waitFor(`document.getElementById('run-overview').open === ${open} && document.getElementById('supervisor-panel').checkVisibility() === ${!open}`);
        }
        async function chatBounds(width, height) {
            const bounds = await evaluate(`(() => {
                const rect = selector => {const r=document.querySelector(selector).getBoundingClientRect(); return {top:r.top,bottom:r.bottom,left:r.left,right:r.right,height:r.height};};
                return {messages:rect('#messages'), composer:rect('#chat-form'), input:rect('#chat-message'), send:rect('#chat-send'), footer:rect('.app-footer'),
                    scrollWidth:document.documentElement.scrollWidth, viewportWidth:innerWidth, viewportHeight:innerHeight,
                    outerScrollHeight:document.documentElement.scrollHeight,
                    logScrollable:getComputedStyle(document.getElementById('messages')).overflowY,
                    composerScrollable:document.getElementById('chat-form').scrollHeight > document.getElementById('chat-form').clientHeight + 1};
            })()`);
            assert(bounds.messages.height >= 80, `usable message height at ${width}x${height}: ${JSON.stringify(bounds)}`);
            assert(bounds.messages.bottom <= bounds.composer.top + 1, `messages above composer at ${width}x${height}`);
            assert(bounds.composer.bottom <= bounds.footer.top + 1, `footer does not overlap composer at ${width}x${height}`);
            assert(bounds.send.top >= bounds.composer.top && bounds.send.bottom <= bounds.composer.bottom + 1, `send stays inside composer at ${width}x${height}`);
            assert(bounds.footer.bottom <= bounds.viewportHeight + 1, `footer fits viewport at ${width}x${height}`);
            assert(bounds.input.height >= 40, `usable composer at ${width}x${height}`);
            assert(bounds.scrollWidth <= bounds.viewportWidth, `horizontal overflow at ${width}x${height}`);
            assert(bounds.outerScrollHeight <= bounds.viewportHeight + 1, `execution page has no outer vertical scroll at ${width}x${height}`);
            assert(['auto', 'scroll'].includes(bounds.logScrollable), 'messages own their scroll');
            assert.equal(bounds.composerScrollable, false, 'composer does not need a second scroll');
        }
        async function chooseLocalSource(fixture) {
            await evaluate(`window.localFixture = ${JSON.stringify(fixture)}; document.getElementById('choose-local-source').click()`);
            await waitFor(`document.getElementById('local-source-token').value === ${JSON.stringify(fixture.token)}`);
        }
        await command('Runtime.enable');
        await command('Page.enable');
        await command('Page.addScriptToEvaluateOnNewDocument', {source: "Object.defineProperty(navigator, 'language', {value:'zh-CN'})"});
        async function screenshot(name) {
            if (!process.env.DESKTOP_UI_SCREENSHOT_ROOT) return;
            fs.mkdirSync(process.env.DESKTOP_UI_SCREENSHOT_ROOT, {recursive: true});
            const shot = await command('Page.captureScreenshot', {format: 'png'});
            fs.writeFileSync(path.join(process.env.DESKTOP_UI_SCREENSHOT_ROOT, `${name}.png`), Buffer.from(shot.data, 'base64'));
        }
        await command('Page.addScriptToEvaluateOnNewDocument', {source: `
            window.updateFixture={status:'available', currentVersion:'0.1.0', latestVersion:'0.1.1', releaseUrl:'https://github.com/FlightDan/ModPort/releases/tag/v0.1.1', checkedAt:'2026-10-06T12:00:00Z'};
            window.modport={getUpdateStatus:async()=>window.updateFixture,
                onUpdateStatus: callback=>{window.updateListener=callback; return ()=>{};},
                checkForUpdates:async()=>{await new Promise(resolve=>setTimeout(resolve,150)); return window.updateFixture;},
                openUpdateDownload:async()=>{window.downloadOpened=true; return {opened:true};}};
        `});
        await command('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
        await command('Page.navigate', {url: `http://127.0.0.1:${server.address().port}/`});
        await waitFor("document.getElementById('project-next')?.disabled === false");
        await evaluate("document.getElementById('language-select').value='zh-CN'; document.getElementById('language-select').dispatchEvent(new Event('change', {bubbles:true}))");
        await waitFor("!document.getElementById('language-select').disabled && document.documentElement.lang === 'zh-CN'");
        assert.match(await evaluate("document.getElementById('updates-label').textContent"), /0.1.1/,
            JSON.stringify(await evaluate("({bridge:typeof window.modport, fixture:window.updateFixture, status:document.getElementById('updates-status').textContent, feedback:document.getElementById('feedback').textContent})")));
        await clickVisible('#updates-button');
        await waitFor("document.getElementById('updates-dialog').open");
        assert.match(await evaluate("document.getElementById('updates-current').textContent"), /0.1.0/);
        await clickVisible('#updates-download');
        assert.equal(await evaluate('window.downloadOpened'), true);
        await evaluate("window.updateFixture={...window.updateFixture,status:'error',errorCode:'offline'}; document.getElementById('updates-check').click()");
        await waitFor("document.getElementById('updates-check').disabled");
        await waitFor("document.getElementById('updates-status').textContent.includes('暂时无法')");
        assert.equal(await evaluate("document.getElementById('updates-download').hidden"), true);
        await evaluate("window.updateFixture={...window.updateFixture,status:'current',errorCode:null}; document.getElementById('updates-check').click()");
        await waitFor("document.getElementById('updates-status').textContent.includes('最新正式版')");
        await evaluate("window.updateFixture={...window.updateFixture,status:'available'}; window.updateListener(window.updateFixture)");
        await screenshot('updates');
        await clickVisible('[data-close-dialog="updates-dialog"]');
        // Navigation is available before a project is complete and does not
        // manufacture a Run or pretend that execution data already exists.
        assert.equal(await evaluate("[...document.querySelectorAll('.steps button')].every(button => !button.disabled)"), true);
        await evaluate("document.querySelector('.steps [data-screen=\"settings\"]').click()");
        await waitFor("!document.getElementById('settings-screen').hidden");
        await evaluate("document.getElementById('settings-form').requestSubmit()");
        await waitFor("!document.getElementById('project-screen').hidden");
        assert.equal(requests.filter(req => req.method === 'POST' && req.path === '/api/runs').length, 0);
        await evaluate("document.querySelector('.steps [data-screen=\"run\"]').click()");
        await waitFor("!document.getElementById('run-screen').hidden");
        assert.equal(await evaluate("document.getElementById('run-id').textContent"), '');
        assert.equal(await evaluate("document.getElementById('run-heading').textContent"), '尚未创建迁移实例');
        assert.equal(await evaluate("document.getElementById('run-status').textContent"), '未开始');
        assert.equal(await evaluate("document.getElementById('sync-state').textContent"), '尚无执行数据');
        assert.deepEqual(await evaluate("['cancel-button','run-refresh','chat-send','chat-message'].map(id => document.getElementById(id).disabled)"), [true,true,true,true]);
        assert.equal(await evaluate("document.querySelectorAll('[data-task-id]').length"), 0);
        assert.equal(requests.some(req => /^\/api\/runs\//.test(req.path)), false);
        await evaluate("document.querySelector('.steps [data-screen=\"project\"]').click()");
        await waitFor("!document.getElementById('project-screen').hidden");
        // The first request for a real Run may fail after viewing the empty
        // execution page. Refresh must remain available so the user can recover.
        offline = true;
        await evaluate("document.querySelector('#recent-runs .recent-item').click()");
        await waitFor("document.getElementById('sync-state').textContent.includes('连接中断')");
        assert.equal(await evaluate("document.getElementById('run-id').textContent"), 'browser-test');
        assert.equal(await evaluate("document.getElementById('run-refresh').disabled"), false);
        assert.equal(await evaluate("document.getElementById('cancel-button').disabled && document.getElementById('chat-send').disabled"), true);
        offline = false;
        await evaluate("document.getElementById('run-refresh').click()");
        await waitFor("document.getElementById('run-heading').textContent === '已有实例恢复测试' && document.getElementById('sync-state').textContent === '已同步'");
        assert.equal(await evaluate("document.getElementById('run-refresh').disabled"), false);
        assert.equal(requests.filter(req => req.method === 'POST' && req.path === '/api/runs').length, 0);
        await evaluate("document.querySelector('.steps [data-screen=\"project\"]').click()");
        await waitFor("!document.getElementById('project-screen').hidden");
        await screenshot('project');
        assert.deepEqual(await evaluate("['source-loader','target-loader'].map(id => [...document.getElementById(id).options].some(option => option.value === 'fabric'))"), [true, true]);
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        assert.equal(await evaluate("document.getElementById('project-name').value"), '');
        await evaluate("document.getElementById('advanced-model-settings').open=true");
        await waitFor("!document.getElementById('save-model-settings').hidden");
        await evaluate(`document.getElementById('add-provider').click();
            for (const [id,value] of Object.entries({'provider-0-name':'本机连接','provider-0-url':'http://127.0.0.1:9999/v1','provider-0-key':'test-ui-key','provider-0-model-0-id':'test-model','provider-0-model-0-context':'64000','provider-0-model-0-output':'4000','provider-0-model-0-efforts':'low, high'})) {
                const input=document.getElementById(id);input.value=value;input.dispatchEvent(new Event('input'));
            }`);
        await screenshot('model-settings');
        for (const [width,height] of [[768,900],[375,812]]) {
            await command('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:false});
            assert.equal(await evaluate("document.getElementById('model-settings-dialog').scrollWidth <= document.getElementById('model-settings-dialog').clientWidth"), true);
        }
        await command('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
        await evaluate("document.getElementById('model-settings-form').requestSubmit()");
        await waitFor("!document.getElementById('model-settings-dialog').open && document.getElementById('provider-settings').children.length === 0");
        const saved = requests.find(req => req.method === 'POST' && req.path === '/api/model-settings').body;
        assert.equal(saved.providers[0].models[0].context_window,64000);
        assert.equal(saved.providers[0].models[0].max_output_tokens,4000);
        assert.deepEqual(saved.providers[0].models[0].reasoning_efforts,['low','high']);
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        assert.equal(await evaluate("document.getElementById('provider-0-key').value"),'');
        assert.match(await evaluate("document.getElementById('provider-0-key').placeholder"),/已配置/);
        const settingsBeforeCancel = JSON.parse(JSON.stringify(modelSettings));
        const savesBeforeCancel = requests.filter(req => req.method === 'POST' && req.path === '/api/model-settings').length;
        await evaluate("document.getElementById('setup-primary-url').value='https://discarded.example/v1'; document.getElementById('setup-primary-url').dispatchEvent(new Event('input')); document.querySelector('[data-close-dialog=\"model-settings-dialog\"]').click()");
        await waitFor("!document.getElementById('model-settings-dialog').open && document.getElementById('provider-settings').children.length === 0");
        assert.deepEqual(modelSettings, settingsBeforeCancel);
        assert.equal(requests.filter(req => req.method === 'POST' && req.path === '/api/model-settings').length, savesBeforeCancel);
        assert.equal(await evaluate("document.getElementById('group-author-model').value"), '');
        assert.match(await evaluate("document.getElementById('group-author-model').placeholder"), /多个设置/);
        await evaluate("document.getElementById('project-name').value='真实表单测试'; document.getElementById('repository').value='https://github.com/test/mod'; document.getElementById('identify-button').click()");
        await waitFor("document.getElementById('source-minecraft').value === '1.20.1'");
        assert.equal(await evaluate("document.getElementById('revision').value"), 'main');
        const dirtySource = {token: 'local-dirty-token', display_path: '/fixture/闭源 Mod', name: '闭源 Mod', detected: {source_minecraft: '1.20.1', source_loader: 'fabric', source_loader_version: '0.16.0'}, warnings: [], git: {available: true, is_repository: true, root: '/fixture/闭源 Mod', branch: 'work', dirty: true, has_commits: true, can_branch: true, reason: null}};
        const cleanSource = {...dirtySource, token: 'local-clean-token', git: {...dirtySource.git, branch: 'main', dirty: false}};
        const noGitSource = {...dirtySource, token: 'local-no-git-token', git: {available: false, is_repository: false, root: null, branch: null, dirty: false, has_commits: false, can_branch: false, reason: '沒有安裝 Git'}};
        await evaluate(`window.openedWorkspaceIds=[]; window.localFixture=${JSON.stringify(dirtySource)};
            window.modport = {...window.modport, selectSourceDirectory: async () => window.localFixture, openWorkspace: async id => {window.openedWorkspaceIds.push(id); return {opened:true};}};
            const radio=document.querySelector('input[name="source_mode"][value="local"]');radio.checked=true;radio.dispatchEvent(new Event('change'));`);
        await chooseLocalSource(dirtySource);
        assert.equal(await evaluate("!document.getElementById('project-screen').hidden && !document.getElementById('local-workspace-settings').hidden && document.getElementById('local-workspace-settings').closest('#project-form') !== null && document.getElementById('local-workspace-settings').getBoundingClientRect().height > 0"), true);
        assert.deepEqual(await evaluate("[...document.querySelectorAll('input[name=local_workspace_mode]')].map(input => input.value)"), ['git_worktree','copy','direct']);
        assert.equal(await evaluate("document.getElementById('repository').disabled && document.getElementById('revision').disabled"),true);
        assert.equal(await evaluate("document.getElementById('source-loader').value"),'fabric');
        assert.equal(await evaluate("document.getElementById('local-git-status').textContent.includes('有未提交修改')"), true);
        await evaluate("document.getElementById('local-workspace-branch').value='stale-branch'; document.getElementById('direct-workspace-confirmed').checked=true");
        await chooseLocalSource(cleanSource);
        assert.equal(await evaluate("document.getElementById('local-git-status').textContent.includes('工作区干净')"), true);
        assert.equal(await evaluate("document.getElementById('local-workspace-branch').value === '' && !document.getElementById('direct-workspace-confirmed').checked"), true);
        await chooseLocalSource(noGitSource);
        assert.match(await evaluate("document.getElementById('local-git-status').textContent"), /沒有安裝 Git/);
        await chooseLocalSource(dirtySource);
        await screenshot('local-source');

        await evaluate("document.getElementById('target-minecraft').value='1.21.1'; document.getElementById('target-loader-version').value='21.1.test'; document.getElementById('project-form').requestSubmit()");
        await waitFor("!document.getElementById('settings-screen').hidden");
        await screenshot('settings');
        assert.equal(await evaluate("document.querySelector('input[name=\"local_workspace_mode\"][value=\"git_worktree\"]').checked"), true);
        await evaluate("document.getElementById('settings-form').requestSubmit()");
        assert.equal(await evaluate("!document.getElementById('workspace-branch-error').hidden && document.getElementById('workspace-branch-error').textContent.includes('请输入新分支名称')"), true);
        assert.equal(requests.filter(req => req.method === 'POST' && req.path === '/api/runs').length, 0);
        await evaluate("document.getElementById('local-workspace-branch').value='bad name'; document.getElementById('local-workspace-branch').dispatchEvent(new Event('input')); document.getElementById('settings-form').requestSubmit()");
        assert.equal(await evaluate("!document.getElementById('workspace-branch-error').hidden && document.getElementById('workspace-branch-error').textContent.includes('格式无效')"), true);
        await evaluate("document.querySelector('input[name=\"local_workspace_mode\"][value=\"copy\"]').click(); document.querySelector('button[data-screen=\"project\"]').click(); document.querySelector('input[name=\"source_mode\"][value=\"remote\"]').click(); document.querySelector('input[name=\"source_mode\"][value=\"local\"]').click(); document.getElementById('project-form').requestSubmit()");
        await waitFor("!document.getElementById('settings-screen').hidden");
        assert.equal(await evaluate("document.querySelector('input[name=\"local_workspace_mode\"][value=\"copy\"]').checked && document.getElementById('workspace-summary').textContent.includes('复制所选源码')"), true);
        await evaluate("document.querySelector('input[name=\"local_workspace_mode\"][value=\"git_worktree\"]').click(); document.getElementById('local-workspace-branch').value='release/ui-test'; document.getElementById('local-workspace-branch').dispatchEvent(new Event('input'))");
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        await evaluate("document.getElementById('advanced-model-settings').open=true");
        await waitFor("!document.getElementById('save-model-settings').hidden");
        await evaluate("document.getElementById('role-planner-reasoning_effort').closest('details').open=true; document.getElementById('role-planner-reasoning_effort').focus(); document.getElementById('role-planner-reasoning_effort').value='medium'; document.getElementById('model-settings-form').requestSubmit()");
        await waitFor("!document.getElementById('model-settings-dialog').open && document.getElementById('provider-settings').children.length === 0");
        await evaluate("document.getElementById('settings-form').requestSubmit()");
        await waitFor("document.getElementById('run-heading').textContent === '浏览器测试实例'");
        const launchedGit = requests.find(req => req.method === 'POST' && req.path === '/api/runs').body;
        assert.equal(launchedGit.model_config.roles.planner.reasoning_effort, 'medium');
        assert.deepEqual(launchedGit.model_config.default, modelConfig.default);
        assert.deepEqual(launchedGit.model_config.stages, modelConfig.stages);
        assert.deepEqual(launchedGit.model_config.roles.planner.fallback, modelConfig.roles.planner.fallback);
        assert.equal(launchedGit.source_mode, 'local'); assert.equal(launchedGit.local_source_token, 'local-dirty-token');
        assert.equal(launchedGit.local_workspace_mode, 'git_worktree'); assert.equal(launchedGit.local_branch_name, 'release/ui-test');
        assert.equal(launchedGit.direct_workspace_confirmed, false);
        assert.equal(launchedGit.source_repository, undefined); assert.equal(launchedGit.source_revision, undefined);
        assert(!JSON.stringify(launchedGit).includes('/fixture/闭源')); assert.equal(launchedGit.max_seconds, 7200);
        await overview(true);
        assert.equal(await evaluate("document.getElementById('run-workspace').checkVisibility() && document.getElementById('run-workspace-path').textContent.includes('/fixture/workspaces/browser-test-1') && document.getElementById('run-workspace-branch').textContent.includes('release/ui-test')"), true);
        await clickVisible('#open-workspace');
        await waitFor("window.openedWorkspaceIds.length === 1");
        assert.equal(await evaluate("window.openedWorkspaceIds[0]"), 'browser-test-1');
        await overview(false);

        await evaluate("document.getElementById('new-project').click()");
        await waitFor("!document.getElementById('project-screen').hidden");
        await evaluate("document.getElementById('local-workspace-branch').value='stale-branch'; document.getElementById('direct-workspace-confirmed').checked=true");
        await chooseLocalSource({...noGitSource, token: 'local-copy-token'});
        assert.equal(await evaluate("document.getElementById('local-workspace-branch').value === '' && !document.getElementById('direct-workspace-confirmed').checked"), true);
        await evaluate("document.getElementById('project-form').requestSubmit()");
        await waitFor("!document.getElementById('settings-screen').hidden");
        assert.equal(await evaluate("!document.getElementById('workspace-git-reason').hidden && document.querySelector('input[name=\"local_workspace_mode\"][value=\"git_worktree\"]').disabled && document.querySelector('input[name=\"local_workspace_mode\"][value=\"copy\"]').checked"), true);
        await evaluate("document.getElementById('settings-form').requestSubmit()");
        await waitFor("document.getElementById('run-id').textContent === 'browser-test-2'");
        const launchedCopy = requests.filter(req => req.method === 'POST' && req.path === '/api/runs')[1].body;
        assert.equal(launchedCopy.local_source_token, 'local-copy-token');
        assert.equal(launchedCopy.local_workspace_mode, 'copy');
        assert.equal(launchedCopy.local_branch_name, undefined);
        assert.equal(launchedCopy.direct_workspace_confirmed, false);
        assert(!JSON.stringify(launchedCopy).includes('/fixture/闭源'));

        await evaluate("document.getElementById('new-project').click()");
        await waitFor("!document.getElementById('project-screen').hidden");
        await chooseLocalSource({...noGitSource, token: 'local-direct-token'});
        await evaluate("document.getElementById('project-form').requestSubmit()");
        await waitFor("!document.getElementById('settings-screen').hidden");
        await evaluate("document.querySelector('input[name=\"local_workspace_mode\"][value=\"direct\"]').click()");
        assert.equal(await evaluate("document.getElementById('workspace-direct-warning').textContent.includes('直接修改所选目录，不新建分支、不复制备份；运行期间请勿同时编辑')"), true);
        await evaluate("document.getElementById('settings-form').requestSubmit()");
        assert.equal(await evaluate("!document.getElementById('workspace-direct-error').hidden && document.getElementById('workspace-direct-error').textContent.includes('需要先确认')"), true);
        assert.equal(requests.filter(req => req.method === 'POST' && req.path === '/api/runs').length, 2);
        await evaluate("document.getElementById('direct-workspace-confirmed').click(); document.getElementById('settings-form').requestSubmit()");
        await waitFor("document.getElementById('run-id').textContent === 'browser-test-3'");
        const launchedDirect = requests.filter(req => req.method === 'POST' && req.path === '/api/runs')[2].body;
        assert.equal(launchedDirect.local_source_token, 'local-direct-token');
        assert.equal(launchedDirect.local_workspace_mode, 'direct');
        assert.equal(launchedDirect.local_branch_name, undefined);
        assert.equal(launchedDirect.direct_workspace_confirmed, true);
        assert(!JSON.stringify(launchedDirect).includes('/fixture/闭源'));
        assert.equal(await evaluate("document.getElementById('run-overview').open"), false, 'overview starts collapsed');
        assert.equal(await evaluate("document.getElementById('supervisor-panel').checkVisibility()"), true);
        assert.equal(await evaluate("document.getElementById('stage-board').checkVisibility()"), false);
        await evaluate("document.getElementById('chat-message').value='保留草稿'; document.getElementById('chat-message').dispatchEvent(new Event('input', {bubbles:true}))");
        await overview(true);
        assert.equal(await evaluate("document.getElementById('chat-message').checkVisibility()"), false);
        assert.equal(await evaluate("document.querySelectorAll('[data-task-id=\"pending-test\"]').length"), 0);
        assert.equal(await evaluate("document.querySelector('[data-task-id=\"failed-test\"]').getBoundingClientRect().height > 0"), true);
        assert.equal(await evaluate("document.querySelector('[data-task-id=\"done-test\"]').closest('details').open"), false);
        assert.equal(await evaluate("document.querySelector('[data-task-id=\"active-0\"] .task-agents').textContent"), 'agents 1subagents 0');
        assert.equal(await evaluate("document.querySelector('[data-task-id=\"active-5\"]').closest('details').open"), false);
        assert.equal(await evaluate("document.getElementById('used-tokens').textContent"), '未知');
        assert.equal(await evaluate("document.querySelector('[data-task-id=\"failed-test\"]').querySelector('script')"), null);
        assert.equal(await evaluate("document.getElementById('run-status').textContent"), '运行中');
        assert.equal(await evaluate("document.querySelectorAll('.stage-header .badge')[0].textContent"), '有失败项');
        assert.equal(await evaluate("document.querySelectorAll('.stage-header .badge')[1].textContent"), '运行中');
        assert.equal(await evaluate("document.querySelectorAll('.stage-failure-count')[0].textContent"), '1 项失败');
        assert.equal(await evaluate("document.querySelector('[data-task-id=codemod] .badge').textContent"), '失败');
        assert.equal(await evaluate("document.querySelector('[data-task-id=codemod] .task-identity').textContent"), '任务 ID：codemod');
        assert.match(await evaluate("document.querySelector('[data-task-id=codemod] .task-error-code').textContent"), /codemod_rules_unavailable/);
        assert.equal(await evaluate("document.querySelector('[data-task-id=done-test]').checkVisibility()"), false);
        await clickVisible('#stage-board details:has([data-task-id="done-test"]) > summary');
        assert.equal(await evaluate("document.querySelector('[data-task-id=done-test]').checkVisibility()"), true);
        await clickVisible('#stage-board details:has([data-task-id="done-test"]) > summary');
        await clickVisible('[data-task-id="codemod"]');
        assert.equal(await evaluate("document.querySelector('[data-task-id=codemod]').checkVisibility()"), true);
        await clickVisible('#return-supervisor-chat');
        await waitFor("!document.getElementById('run-overview').open && document.activeElement.id === 'chat-message'");
        assert.equal(await evaluate("document.getElementById('chat-message').value"), '保留草稿');
        await overview(true);
        await clickVisible('#open-supervisor-chat');
        await waitFor("!document.getElementById('run-overview').open && document.activeElement.id === 'chat-message'");
        assert.equal(await evaluate("document.getElementById('chat-message').value"), '保留草稿');
        // Enough fixture conversation to exercise the message scroller; no model is called.
        messages.push(...Array.from({length: 40}, (_, i) => ({id:`history-${i}`,role:'supervisor',content:`已有监督记录 ${i}\n这是一条多行记录。`,state:'delivered'})));
        for (const [width, height] of [[1440,900], [1366,768], [1024,600], [768,900], [375,812]]) {
            await command('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:false});
            await refreshRun();
            await waitFor("document.getElementById('messages').textContent.includes('已有监督记录 39')");
            assert.equal(await evaluate("document.getElementById('run-overview').open"), false, 'refresh preserves collapsed overview');
            assert.equal(await evaluate("document.getElementById('chat-message').value"), '保留草稿', 'refresh preserves chat draft');
            await chatBounds(width, height);
            assert.equal(await evaluate("document.getElementById('messages').scrollHeight > document.getElementById('messages').clientHeight"), true, 'message history scrolls');
            await screenshot(`chat-${width}x${height}`);
            await overview(true);
            await refreshRun();
            await waitFor("document.getElementById('sync-state').textContent === '已同步'");
            assert.equal(await evaluate("document.getElementById('run-overview').open"), true, 'refresh preserves expanded overview');
            const layout = await evaluate(`(() => {
                const content=document.getElementById('run-overview'), r=content.getBoundingClientRect(), footer=document.querySelector('.app-footer').getBoundingClientRect();
                return {top:r.top,bottom:r.bottom,height:r.height,footerTop:footer.top,overflow:getComputedStyle(content).overflowY,
                    boardVisible:document.getElementById('stage-board').checkVisibility(),chatVisible:document.getElementById('supervisor-panel').checkVisibility(),
                    stageScrollers:[...document.querySelectorAll('.stage-body')].filter(e => e.scrollHeight > e.clientHeight + 1 && ['auto','scroll'].includes(getComputedStyle(e).overflowY)).length,
                    horizontalOverflow:document.documentElement.scrollWidth > innerWidth};
            })()`);
            assert(layout.height >= 80 && layout.bottom <= layout.footerTop + 1, `overview fits at ${width}x${height}: ${JSON.stringify(layout)}`);
            assert(['auto','scroll'].includes(layout.overflow), 'overview content owns its scroll');
            assert.equal(layout.stageScrollers, 0, 'overview has no nested stage scrollers');
            assert.equal(layout.boardVisible, true);
            assert.equal(layout.chatVisible, false);
            assert.equal(layout.horizontalOverflow, false);
            await screenshot(`overview-${width}x${height}`);
            await clickVisible('#open-supervisor-chat');
            await waitFor("!document.getElementById('run-overview').open && document.activeElement.id === 'chat-message'");
        }
        // Updating conversation while hidden must retain the reader's position,
        // while readers already at the end follow new messages when they return.
        async function scrollMessages(position) {
            return evaluate(`new Promise(resolve => {
                const log=document.getElementById('messages');
                const next=${position};
                if (Math.abs(log.scrollTop - next) < 1) return resolve(log.scrollTop);
                log.addEventListener('scroll', () => requestAnimationFrame(() => resolve(log.scrollTop)), {once:true});
                log.scrollTop=next;
            })`);
        }
        const readingTop = await scrollMessages('(log.scrollHeight - log.clientHeight) / 2');
        assert(readingTop > 50, 'long fixture has a middle reading position');
        await overview(true);
        messages.push({id:'hidden-reading-update',role:'supervisor',content:'隐藏对话期间的新记录\n阅读位置应保持。',state:'delivered'});
        await refreshRun();
        assert.equal(await evaluate("document.getElementById('supervisor-panel').checkVisibility()"), false);
        assert.equal(await evaluate("document.getElementById('messages').textContent.includes('隐藏对话期间的新记录')"), true, 'refresh updates the hidden conversation');
        await clickVisible('#open-supervisor-chat');
        await waitFor(`!document.getElementById('run-overview').open && Math.abs(document.getElementById('messages').scrollTop - ${readingTop}) <= 1`);
        assert.equal(await evaluate("document.activeElement.id"), 'chat-message');
        assert.equal(await evaluate("document.getElementById('chat-message').value"), '保留草稿');
        await screenshot('chat-reading-preserved');
        const previousEnd = await scrollMessages('log.scrollHeight - log.clientHeight');
        await overview(true);
        messages.push({id:'hidden-follow-update',role:'supervisor',content:'最新监督记录，应跟随至末尾。\n新的记录第二行。\n新的记录第三行。',state:'delivered'});
        await refreshRun();
        assert.equal(await evaluate("document.getElementById('messages').textContent.includes('最新监督记录，应跟随至末尾。')"), true);
        await clickVisible('#open-supervisor-chat');
        try {
            await waitFor("!document.getElementById('run-overview').open && Math.abs(document.getElementById('messages').scrollHeight - document.getElementById('messages').clientHeight - document.getElementById('messages').scrollTop) <= 1");
        } catch (error) {
            const scrollState = await evaluate(`(() => {
                const log=document.getElementById('messages');
                return {overviewOpen:document.getElementById('run-overview').open,visible:log.checkVisibility(),top:log.scrollTop,height:log.clientHeight,scrollHeight:log.scrollHeight,bottomGap:log.scrollHeight-log.clientHeight-log.scrollTop};
            })()`);
            await screenshot('chat-follow-latest-failure');
            throw new Error(`${error.message}; scroll state: ${JSON.stringify(scrollState)}`);
        }
        assert((await evaluate("document.getElementById('messages').scrollTop")) > previousEnd, 'following reader advances to new conversation end');
        assert.equal(await evaluate("document.getElementById('chat-message').value"), '保留草稿');
        await screenshot('chat-follow-latest');
        // Optional overview panels never take height from the conversation.
        for (const [workspace, notice] of [[false,null], [true,'实例诊断仍待确认。']]) {
            showWorkspace = workspace; runNotice = notice;
            await refreshRun();
            await waitFor(`document.getElementById('run-workspace').hidden === ${!workspace} && document.getElementById('run-notice').hidden === ${!notice}`);
            await chatBounds(375, 812);
            await overview(true);
            assert.equal(await evaluate("document.getElementById('run-workspace').checkVisibility()"), workspace);
            assert.equal(await evaluate("document.getElementById('run-notice').checkVisibility()"), Boolean(notice));
            await overview(false);
        }
        await evaluate("document.getElementById('chat-message').value='测试监督对话'; document.getElementById('chat-form').requestSubmit()");
        await waitFor("document.getElementById('messages').textContent.includes('测试监督对话')");
        assert.equal(requests.find(req => req.path.endsWith('/chat')).body.message, '测试监督对话');
        await evaluate("document.getElementById('cancel-button').click()");
        assert.equal(await evaluate("document.getElementById('cancel-dialog').open"), true);
        assert.equal(requests.some(req => req.path.endsWith('/cancel')), false);
        await evaluate("document.querySelector('[data-close-dialog=\"cancel-dialog\"]').click()");
        offline = true;
        await evaluate("document.getElementById('run-refresh').click()");
        await waitFor("document.getElementById('sync-state').textContent.includes('连接中断')");
        assert.match(await evaluate("document.getElementById('run-heading').textContent"), /浏览器测试实例/);
        offline = false;
        await evaluate("document.getElementById('run-refresh').click()");
        await waitFor("document.getElementById('sync-state').textContent === '已同步'");
        for (const [width, height] of [[1024, 768], [768, 900], [375, 812]]) {
            await command('Emulation.setDeviceMetricsOverride', {width, height, deviceScaleFactor: 1, mobile: false});
            assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true, `horizontal overflow at ${width}px`);
        }
        await command('Emulation.setEmulatedMedia', {features: [{name: 'prefers-reduced-motion', value: 'reduce'}]});
        assert.equal(await evaluate("getComputedStyle(document.getElementById('chat-send')).transitionDuration"), '0s');
        await evaluate("document.getElementById('cancel-button').click(); document.getElementById('confirm-cancel').click()");
        await waitFor("document.getElementById('run-status').textContent === '已取消'");
        assert.deepEqual(requests.find(req => req.path.endsWith('/cancel')).body, {confirmed: true});
        // Save the two intended task usages through the actual guided form.
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        assert.equal(await evaluate("document.getElementById('advanced-model-settings').open"), false);
        async function fillGuided(values) {
            await evaluate(`for (const [id,value] of Object.entries(${JSON.stringify(values)})) {const input=document.getElementById(id);input.value=value;input.dispatchEvent(new Event('input'));}`);
        }
        await fillGuided({'setup-primary-url':'https://primary.example/v1','setup-primary-key':'guided-primary-key'});
        await evaluate("document.getElementById('model-setup-next').click()");
        await waitFor("Boolean(document.getElementById('setup-difficult-id'))");
        await fillGuided({'setup-difficult-id':'reasoning-model','setup-difficult-reasoning':'high','setup-difficult-context':'128000','setup-difficult-output':'16000','setup-difficult-efforts':'low, high'});
        await evaluate("document.getElementById('model-setup-next').click()");
        await waitFor("Boolean(document.getElementById('setup-routine-id'))");
        assert.equal(await evaluate("document.getElementById('setup-secondary-url')"), null);
        await fillGuided({'setup-routine-id':'coding-model','setup-routine-reasoning':'low','setup-routine-context':'64000','setup-routine-output':'8000','setup-routine-efforts':'low, high'});
        await evaluate("document.getElementById('model-settings-form').requestSubmit()");
        await waitFor("!document.getElementById('model-settings-dialog').open && document.getElementById('provider-settings').children.length === 0");
        const sharedSettings = requests.filter(req => req.method === 'POST' && req.path === '/api/model-settings').at(-1).body;
        const primary = sharedSettings.providers.find(provider => provider.base_url === 'https://primary.example/v1');
        assert(primary);
        assert.equal(primary.api_key, 'guided-primary-key');
        assert.deepEqual(primary.models.map(model => model.id), ['reasoning-model','coding-model']);
        assert.deepEqual(primary.models[0], {id:'reasoning-model',context_window:128000,max_output_tokens:16000,reasoning_efforts:['low','high']});
        for (const role of ['planner','contract_review']) assert.equal(sharedSettings.model_config.roles[role].model, `${primary.id}/reasoning-model`);
        for (const role of ['coder','supervisor','summary']) assert.deepEqual(sharedSettings.model_config.roles[role], {model:`${primary.id}/coding-model`,reasoning_effort:'low'});
        assert.deepEqual(sharedSettings.model_config.default, {model:`${primary.id}/coding-model`,reasoning_effort:'low'});
        assert.deepEqual(sharedSettings.model_config.stages, modelConfig.stages);
        assert.deepEqual(sharedSettings.model_config.roles.planner.fallback, modelConfig.roles.planner.fallback);
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        assert.equal(await evaluate("document.getElementById('setup-primary-key').value"), '');
        assert.match(await evaluate("document.getElementById('setup-primary-key').placeholder"), /已配置/);
        await evaluate("document.querySelector('[data-model-step=\"2\"]').click(); document.querySelectorAll('.setup-model-actions button')[1].click()");
        await waitFor("Boolean(document.getElementById('setup-secondary-url'))");
        await fillGuided({'setup-secondary-url':'https://secondary.example/v1','setup-secondary-key':'guided-secondary-key'});
        await evaluate("document.querySelector('.setup-model-actions button').click()");
        assert.equal(await evaluate("document.querySelector('.setup-model-actions button').getAttribute('aria-pressed')"), 'true');
        assert.equal(await evaluate("document.getElementById('setup-routine-id')"), null);
        assert.equal(await evaluate("document.getElementById('setup-secondary-url')"), null);
        await evaluate("document.querySelector('.setup-model-actions button').click()");
        assert.equal(await evaluate("document.getElementById('setup-routine-id').value"), 'coding-model');
        await evaluate("document.querySelectorAll('.setup-model-actions button')[1].click()");
        assert.equal(await evaluate("document.getElementById('setup-secondary-url').value"), 'https://secondary.example/v1');
        await evaluate("document.getElementById('model-settings-form').requestSubmit()");
        await waitFor("!document.getElementById('model-settings-dialog').open && document.getElementById('provider-settings').children.length === 0");
        const separateSettings = requests.filter(req => req.method === 'POST' && req.path === '/api/model-settings').at(-1).body;
        const secondary = separateSettings.providers.find(provider => provider.base_url === 'https://secondary.example/v1');
        assert(secondary);
        assert.equal(secondary.api_key, 'guided-secondary-key');
        assert.equal(separateSettings.model_config.roles.planner.model, `${primary.id}/reasoning-model`);
        assert.equal(separateSettings.model_config.roles.coder.model, `${secondary.id}/coding-model`);
        assert.equal(separateSettings.model_config.default.model, `${secondary.id}/coding-model`);
        const savesBeforeGuidedCancel = requests.filter(req => req.method === 'POST' && req.path === '/api/model-settings').length;
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        await fillGuided({'setup-primary-url':'https://discarded.example/v1'});
        await evaluate("document.querySelector('[data-model-step=\"2\"]').click(); document.querySelector('.setup-model-actions button').click(); document.querySelector('[data-model-step=\"1\"]').click()");
        await fillGuided({'setup-difficult-context':'0'});
        await evaluate("document.getElementById('advanced-model-settings').open=true");
        await waitFor("document.getElementById('advanced-model-settings').open && document.getElementById('model-setup-content').hidden");
        const primaryIndex = separateSettings.providers.findIndex(provider => provider.id === primary.id);
        assert.equal(await evaluate(`document.getElementById('provider-${primaryIndex}-url').value`), 'https://discarded.example/v1');
        assert.equal(await evaluate(`document.getElementById('provider-${primaryIndex}-model-0-context').value`), '0');
        assert.equal(await evaluate("document.getElementById('model-settings-error').hidden"), true);
        await evaluate("document.querySelector('[data-close-dialog=\"model-settings-dialog\"]').click()");
        await waitFor("!document.getElementById('model-settings-dialog').open && document.getElementById('provider-settings').children.length === 0");
        assert.equal(requests.filter(req => req.method === 'POST' && req.path === '/api/model-settings').length, savesBeforeGuidedCancel);
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        assert.equal(await evaluate("document.getElementById('setup-primary-url').value"), 'https://primary.example/v1');
        await evaluate("document.querySelector('[data-model-step=\"2\"]').click()");
        assert.equal(await evaluate("document.getElementById('setup-secondary-url').value"), 'https://secondary.example/v1');
        assert.equal(await evaluate("document.querySelector('.setup-model-actions button').getAttribute('aria-pressed')"), 'false');
        await evaluate("document.querySelector('[data-close-dialog=\"model-settings-dialog\"]').click()");
        // The optional fallback editor uses the same persisted settings request.
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        await evaluate("document.getElementById('advanced-model-settings').open=true; document.getElementById('fallback-settings').open=true");
        await waitFor("!document.getElementById('save-model-settings').hidden");
        await evaluate("document.getElementById('fallback-mode').value='shared'; document.getElementById('fallback-mode').dispatchEvent(new Event('input'))");
        await fillGuided({'fallback-connection-url':'https://backup.example/v1', 'fallback-connection-key':'private-fallback-test',
            'fallback-model-id':'backup-model', 'fallback-model-context':'32000', 'fallback-model-output':'4000', 'fallback-model-reasoning':'none'});
        for (const [width,height] of [[768,900],[375,812]]) {
            await command('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:false});
            assert.equal(await evaluate("document.getElementById('model-settings-dialog').scrollWidth <= document.getElementById('model-settings-dialog').clientWidth"), true);
        }
        await screenshot('fallback-settings');
        await command('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
        await evaluate("document.getElementById('model-settings-form').requestSubmit()");
        await waitFor("!document.getElementById('model-settings-dialog').open && document.getElementById('provider-settings').children.length === 0");
        const fallbackSaved = requests.filter(req => req.method === 'POST' && req.path === '/api/model-settings').at(-1).body;
        const backup = fallbackSaved.providers.find(provider => provider.base_url === 'https://backup.example/v1');
        assert(backup);
        assert.equal(backup.api_key, 'private-fallback-test');
        for (const selection of [fallbackSaved.model_config.default, ...Object.values(fallbackSaved.model_config.roles), ...Object.values(fallbackSaved.model_config.stages)]) {
            assert.deepEqual(selection.fallback, {model: `${backup.id}/backup-model`, reasoning_effort: 'none'});
        }
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        assert.equal(await evaluate("document.getElementById('fallback-mode').value"), 'preserve');
        assert.match(await evaluate("document.getElementById('fallback-settings-content').textContent"), /backup-model/);
        await evaluate("document.getElementById('fallback-mode').value='off'; document.getElementById('fallback-mode').dispatchEvent(new Event('input')); document.querySelector('[data-close-dialog=\"model-settings-dialog\"]').click()");
        await waitFor("!document.getElementById('model-settings-dialog').open && document.getElementById('provider-settings').children.length === 0");
        assert(modelSettings.model_config.default.fallback, 'cancel does not persist fallback removal');
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        await evaluate("document.getElementById('advanced-model-settings').open=true; document.getElementById('fallback-mode').value='off'; document.getElementById('fallback-mode').dispatchEvent(new Event('input'))");
        await waitFor("!document.getElementById('save-model-settings').hidden");
        await evaluate("document.getElementById('model-settings-form').requestSubmit()");
        await waitFor("!document.getElementById('model-settings-dialog').open");
        for (const selection of [modelSettings.model_config.default, ...Object.values(modelSettings.model_config.roles), ...Object.values(modelSettings.model_config.stages)]) assert(!selection.fallback);
        // Updating only fallback can save at step 0 without remapping primary roles.
        const primaryBeforeFallbackOnly = modelSettings.model_config.roles.planner.model;
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        await evaluate("document.getElementById('fallback-settings').open=true; document.getElementById('fallback-mode').value='shared'; document.getElementById('fallback-mode').dispatchEvent(new Event('input'))");
        await evaluate(`document.getElementById('fallback-provider').value=${JSON.stringify(backup.id)}; document.getElementById('fallback-provider').dispatchEvent(new Event('input'))`);
        assert.equal(await evaluate("document.getElementById('save-model-settings').hidden"), false);
        assert.equal(await evaluate("document.getElementById('fallback-catalog-model').value"), 'backup-model');
        await command('Emulation.setDeviceMetricsOverride', {width:375,height:812,deviceScaleFactor:1,mobile:false});
        await evaluate("document.getElementById('fallback-settings').scrollIntoView({block:'start'})");
        await screenshot('fallback-existing-narrow');
        assert.equal(await evaluate("document.getElementById('model-settings-dialog').scrollWidth <= document.getElementById('model-settings-dialog').clientWidth"), true);
        await command('Emulation.setDeviceMetricsOverride', {width:1440,height:1000,deviceScaleFactor:1,mobile:false});
        await evaluate("document.getElementById('fallback-settings').scrollIntoView({block:'start'})");
        await screenshot('fallback-existing');
        await evaluate("document.getElementById('model-settings-form').requestSubmit()");
        await waitFor("!document.getElementById('model-settings-dialog').open");
        assert.equal(modelSettings.model_config.roles.planner.model, primaryBeforeFallbackOnly);
        assert.deepEqual(modelSettings.model_config.default.fallback, {model:`${backup.id}/backup-model`, reasoning_effort:'none'});
        assert.deepEqual(pageErrors, []);
    } finally {
        socket?.close(); browser.kill('SIGTERM');
        await new Promise(resolve => {if (browser.pid === undefined || browser.exitCode !== null) return resolve(); browser.once('exit', resolve); setTimeout(() => {browser.kill('SIGKILL'); resolve();}, 2000).unref();});
        server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
        fs.rmSync(profile, {recursive: true, force: true});
    }
});
