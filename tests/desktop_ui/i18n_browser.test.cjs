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

test('desktop languages: detection, live switching, drafts, dynamic states and reload', {skip: !chromium, timeout: 45000}, async () => {
    const modelConfig = {default: {model: 'test-default', reasoning_effort: 'max'}, roles: {planner: {model: 'test-planner', reasoning_effort: 'high', fallback: {model: 'test/fallback', reasoning_effort: 'low'}}, coder: {model: 'test-author', reasoning_effort: 'high'}, supervisor: {model: 'test-supervisor', reasoning_effort: 'max'}}, stages: {code_review: {model: 'test-stage-review', reasoning_effort: 'medium'}}};
    let modelSettings = {providers: [], model_config: modelConfig};
    const requests = []; let offline = false; let cancelled = false; const messages = [];
    const runRecords = new Map([['browser-test', {workspace: {mode: 'copy', path: '/fixture/workspaces/browser-test'}}]]); let runSequence = 0; let runFetchSequence = 0;
    const stageItems = [{id: 'pending-test', label: '尚未开始的测试', state: 'pending', active_agents: 0, active_subagents: null}, {id: 'done-test', label: '已完成测试', state: 'completed', active_agents: 0, active_subagents: 0}, {id: 'failed-test', label: '必须可见的失败', state: 'failed', detail: '<script>this is plain text</script>', active_agents: 0, active_subagents: null}, ...Array.from({length: 6}, (_, i) => ({id: `active-${i}`, label: `独立任务 ${i}`, state: 'running', active_agents: i + 1, active_subagents: i}))];
    const server = http.createServer(async (req, res) => {
        const chunks = []; for await (const chunk of req) chunks.push(chunk);
        const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
        requests.push({method: req.method, path: req.url, body, language: req.headers['accept-language']});
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
                return send({id, project_name: '浏览器测试实例', status: cancelled ? 'cancelled' : 'running', workspace: record.workspace, elapsed_seconds: 61 + ++runFetchSequence, budget: {max_seconds: 7200, max_tokens: 10000, used_tokens: null, token_usage_complete: false}, stages: {preparation: {state: 'completed', items: []}, implementation: {state: 'running', items: stageItems}, testing: {state: 'pending', items: []}}, messages, supervisor: {busy: false}, notice: null});
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
                const element=document.querySelector(${JSON.stringify(selector)});
                if (!element?.checkVisibility()) throw new Error('Control is hidden: ' + ${JSON.stringify(selector)});
                element.scrollIntoView({block:'nearest'});
                const rect=element.getBoundingClientRect(), x=rect.left+rect.width/2, y=rect.top+rect.height/2;
                if (x < 0 || x >= innerWidth || y < 0 || y >= innerHeight || !element.contains(document.elementFromPoint(x,y)))
                    throw new Error('Control is outside viewport or covered: ' + ${JSON.stringify(selector)});
                return {x,y};
            })()`);
            await command('Input.dispatchMouseEvent', {type:'mousePressed',button:'left',clickCount:1,...target});
            await command('Input.dispatchMouseEvent', {type:'mouseReleased',button:'left',clickCount:1,...target});
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
        async function chooseLocalSource(fixture) {
            await evaluate(`window.localFixture = ${JSON.stringify(fixture)}; document.getElementById('choose-local-source').click()`);
            await waitFor(`document.getElementById('local-source-token').value === ${JSON.stringify(fixture.token)}`);
        }
        await command('Runtime.enable');
        async function screenshot(name) {
            if (!process.env.DESKTOP_UI_SCREENSHOT_ROOT) return;
            fs.mkdirSync(process.env.DESKTOP_UI_SCREENSHOT_ROOT, {recursive: true});
            const shot = await command('Page.captureScreenshot', {format: 'png'});
            fs.writeFileSync(path.join(process.env.DESKTOP_UI_SCREENSHOT_ROOT, `${name}.png`), Buffer.from(shot.data, 'base64'));
        }
        await command('Emulation.setDeviceMetricsOverride', {width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false});
        await command('Page.addScriptToEvaluateOnNewDocument', {source: "Object.defineProperty(navigator, 'language', {value:'en-US'})"});
        await command('Page.navigate', {url: `http://127.0.0.1:${server.address().port}/`});
        await waitFor("document.getElementById('project-next')?.disabled === false");

        assert.equal(await evaluate("document.documentElement.lang"), 'en');
        assert.equal(await evaluate("document.getElementById('project-heading').textContent"), 'Start with a project.');
        assert.equal(await evaluate("document.getElementById('language-select').getAttribute('aria-label')"), 'Language');
        assert.equal(requests.find(req => req.path === '/api/bootstrap').language, 'en');
        await evaluate("document.getElementById('project-name').value='我的 project'; document.getElementById('max-hours').value='3.5'");
        async function language(value) {
            await evaluate(`document.getElementById('language-select').value=${JSON.stringify(value)}; document.getElementById('language-select').dispatchEvent(new Event('change'))`);
            await waitFor(`document.documentElement.lang === ${JSON.stringify(value)} && !document.getElementById('language-select').disabled`);
        }
        await language('zh-CN');
        assert.equal(await evaluate("document.getElementById('project-heading').textContent"), '从项目开始。');
        assert.equal(await evaluate("document.getElementById('project-name').value"), '我的 project');
        assert.equal(await evaluate("document.getElementById('max-hours').value"), '3.5');
        assert.equal(requests.filter(req => req.path === '/api/bootstrap').at(-1).language, 'zh-CN');
        await evaluate(`window.modport={selectSourceDirectory:async()=>({token:'local-token',name:'用户名字',display_path:'/source',detected:{source_minecraft:'1.20.1'},git:{available:false,reason:'未检测到可用的 Git。',reason_translations:{en:'Git was not found.', 'zh-CN':'未检测到可用的 Git。'}},warnings:['用户原文'],warnings_translations:{en:['English warning','用户原文'],'zh-CN':['中文提示','用户原文']}})};
            const radio=document.querySelector('input[name="source_mode"][value="local"]');radio.checked=true;radio.dispatchEvent(new Event('change'));document.getElementById('choose-local-source').click()`);
        await waitFor("document.getElementById('local-source-token').value === 'local-token'");
        await language('en');
        assert.match(await evaluate("document.getElementById('local-git-status').textContent"), /Git was not found/);
        assert.equal(await evaluate("document.getElementById('repository-warnings').textContent"), 'English warning\n用户原文');
        assert.doesNotMatch(await evaluate("document.getElementById('feedback').textContent"), /[\u4e00-\u9fff]/);
        await language('zh-CN');
        assert.equal(await evaluate("document.getElementById('repository-warnings').textContent"), '中文提示\n用户原文');
        await command('Page.reload');
        await waitFor("document.getElementById('project-next')?.disabled === false");
        assert.equal(await evaluate("document.documentElement.lang"), 'zh-CN');
        await language('en');
        await screenshot('project-en');
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        await evaluate("document.getElementById('setup-primary-url').value='https://draft.example/v1'; document.getElementById('setup-primary-url').dispatchEvent(new Event('input')); document.getElementById('setup-primary-key').value='draft-key'; document.getElementById('setup-primary-key').dispatchEvent(new Event('input'))");
        await language('zh-CN');
        assert.equal(await evaluate("document.getElementById('model-setup-content').querySelector('h3').textContent"), '连接你的模型服务');
        assert.equal(await evaluate("document.getElementById('setup-primary-url').value"), 'https://draft.example/v1');
        assert.equal(await evaluate("document.getElementById('setup-primary-key').value"), 'draft-key');
        await language('en');
        assert.equal(await evaluate("document.getElementById('model-setup-content').querySelector('h3').textContent"), 'Connect your model service');
        assert.equal(await evaluate("document.getElementById('setup-primary-url').value"), 'https://draft.example/v1');
        await evaluate("document.getElementById('model-setup-next').click(); document.getElementById('setup-difficult-id').value='user/模型'; document.getElementById('setup-difficult-id').dispatchEvent(new Event('input')); document.getElementById('setup-difficult-context').value='64000'; document.getElementById('setup-difficult-context').dispatchEvent(new Event('input'))");
        await language('zh-CN');
        assert.equal(await evaluate("document.getElementById('setup-difficult-id').value"), 'user/模型');
        assert.equal(await evaluate("document.getElementById('setup-difficult-context').value"), '64000');
        await language('en');
        assert.equal(await evaluate("document.getElementById('setup-difficult-id').value"), 'user/模型');
        // Cancel the incomplete guided draft before testing advanced catalog drafts.
        await evaluate("document.getElementById('model-settings-dialog').close()");
        await waitFor("!document.getElementById('model-settings-dialog').open");
        assert.equal(requests.some(req => req.method === 'POST' && req.path === '/api/model-settings'), false);
        await evaluate("document.getElementById('model-settings-button').click()");
        await waitFor("document.getElementById('model-settings-dialog').open");
        assert.equal(await evaluate("document.getElementById('setup-primary-url').value"), '');
        await evaluate("document.getElementById('advanced-model-settings').open=true");
        await waitFor("!document.getElementById('save-model-settings').hidden");
        await evaluate("document.getElementById('add-provider').click(); const input=document.getElementById('provider-0-name'); input.value='中文 user content'; input.dispatchEvent(new Event('input'))");
        // Trigger from script while modal is open to cover draft preservation too.
        await language('zh-CN');
        assert.equal(await evaluate("document.getElementById('provider-0-name').value"), '中文 user content');
        await language('en');
        assert.equal(await evaluate("document.getElementById('provider-0-name').value"), '中文 user content');
        await screenshot('models-en');
        await evaluate("document.getElementById('model-settings-dialog').close()");
        await evaluate("location.hash='run=browser-test'");
        await waitFor("document.getElementById('run-heading').textContent === '浏览器测试实例'");
        assert.equal(await evaluate("document.getElementById('used-tokens').textContent"), 'Unknown');
        assert.equal(await evaluate("document.getElementById('run-status').textContent"), 'Running');
        assert.equal(await evaluate("document.getElementById('run-overview').open"), false);
        await evaluate("document.getElementById('chat-message').value='Keep 草稿'; document.getElementById('chat-message').dispatchEvent(new Event('input', {bubbles:true}))");
        await overview(true);
        await language('zh-CN');
        assert.equal(await evaluate("document.getElementById('run-overview').open"), true, 'language switch preserves expanded overview');
        assert.equal(await evaluate("document.getElementById('used-tokens').checkVisibility()"), true);
        assert.equal(await evaluate("document.getElementById('used-tokens').textContent"), '未知');
        assert.equal(await evaluate("document.getElementById('chat-message').value"), 'Keep 草稿');
        await language('en');
        assert.equal(await evaluate("document.getElementById('run-heading').textContent"), '浏览器测试实例');
        assert.equal(await evaluate("document.getElementById('run-overview').open"), true);
        await clickVisible('#return-supervisor-chat');
        await waitFor("!document.getElementById('run-overview').open && document.activeElement.id === 'chat-message'");
        assert.equal(await evaluate("document.getElementById('chat-message').value"), 'Keep 草稿');
        await language('zh-CN');
        assert.equal(await evaluate("document.getElementById('run-overview').open"), false, 'language switch preserves collapsed overview');
        assert.equal(await evaluate("document.getElementById('supervisor-panel').checkVisibility()"), true);
        assert.equal(await evaluate("document.getElementById('chat-message').value"), 'Keep 草稿');
        await language('en');
        const lastSync = await evaluate("document.getElementById('sync-time').textContent");
        offline = true;
        await evaluate("document.getElementById('run-refresh').click()");
        await waitFor("document.getElementById('sync-state').textContent.includes('Connection interrupted')");
        await language('zh-CN');
        assert.match(await evaluate("document.getElementById('sync-state').textContent"), /连接中断/);
        await language('en');
        assert.equal(await evaluate("document.getElementById('sync-time').textContent"), lastSync);
        assert.match(await evaluate("document.getElementById('sync-state').textContent"), /Connection interrupted/);
        offline = false;
        await refreshRun();
        await waitFor("document.getElementById('sync-state').textContent === 'Synced'");
        assert.equal(await evaluate("document.getElementById('run-overview').open"), false, 'successful refresh preserves collapsed overview');
        assert.equal(await evaluate("document.getElementById('chat-message').value"), 'Keep 草稿');
        await screenshot('run-en');
        for (const [width,height] of [[1440,900],[1366,768],[1024,600],[768,900],[375,812]]) {
            await command('Emulation.setDeviceMetricsOverride', {width,height,deviceScaleFactor:1,mobile:false});
            const bounds=await evaluate(`(() => {
                const messages=document.getElementById('messages').getBoundingClientRect(), composer=document.getElementById('chat-form').getBoundingClientRect(), footer=document.querySelector('.app-footer').getBoundingClientRect();
                return {messageHeight:messages.height,messageBottom:messages.bottom,composerTop:composer.top,composerBottom:composer.bottom,footerTop:footer.top,footerBottom:footer.bottom,overflow:document.documentElement.scrollWidth > innerWidth,viewportHeight:innerHeight};
            })()`);
            assert.equal(bounds.overflow, false, `English overflow at ${width}x${height}`);
            assert(bounds.messageHeight >= 80 && bounds.messageBottom <= bounds.composerTop + 1, `English vertical conversation at ${width}x${height}: ${JSON.stringify(bounds)}`);
            assert(bounds.composerBottom <= bounds.footerTop + 1 && bounds.footerBottom <= bounds.viewportHeight + 1, `English composer and footer at ${width}x${height}`);
            await screenshot(`chat-en-${width}x${height}`);
        }
        assert.deepEqual(pageErrors, []);
    } finally {
        socket?.close(); browser.kill('SIGTERM');
        await new Promise(resolve => {if (browser.exitCode !== null) return resolve(); browser.once('exit', resolve); setTimeout(() => {browser.kill('SIGKILL'); resolve();}, 2000).unref();});
        server.closeAllConnections(); await new Promise(resolve => server.close(resolve));
        fs.rmSync(profile, {recursive: true, force: true});
    }
});
