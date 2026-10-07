/* Local HTTP fixtures and the real renderer; no GitHub requests or mutations. */
const test = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {spawn} = require('node:child_process');
const {safeLink} = require('../../src/modport/desktop_static/wiki_contributions.js');
const staticRoot = path.resolve(__dirname, '../../src/modport/desktop_static');
const wiki = 'https://github.com/FlightDan/modport-wiki-for-agents';

test('contribution links accept only the device page and canonical wiki PR destinations', () => {
    assert.equal(safeLink('https://github.com/login/device', 'login'), 'https://github.com/login/device');
    assert.equal(safeLink(`${wiki}/pull/12`, 'pr'), `${wiki}/pull/12`);
    assert.equal(safeLink(wiki, 'wiki'), wiki);
    for (const url of ['javascript:alert(1)', 'http://github.com/login/device', 'https://github.com.evil.example/login/device',
        'https://evil.example/login/device', 'https://user@github.com/login/device', 'https://github.com:8443/login/device',
        'https://github.com/login/device?redirect=evil', 'https://github.com/login/device#fragment']) assert.equal(safeLink(url, 'login'), null, url);
    for (const url of [`${wiki}/pull/0`, `${wiki}/pull/1/files`, `${wiki}/issues/1`, `${wiki}/pull/1?x=1`,
        'https://github.com/other/repo/pull/1', 'https://github.com/FlightDan/ModPort/pull/1']) assert.equal(safeLink(url, 'pr'), null, url);
});

const browserCache = process.env.PLAYWRIGHT_BROWSERS_PATH || path.join(os.homedir(), '.cache/ms-playwright');
const cached = fs.existsSync(browserCache) ? fs.readdirSync(browserCache).filter(name => /^chromium-\d+$/.test(name))
    .flatMap(name => ['chrome-linux64/chrome', 'chrome-linux/chrome'].map(binary => path.join(browserCache, name, binary))) : [];
const chromium = [process.env.MODPORT_TEST_CHROMIUM, ...cached, '/usr/bin/chromium', '/usr/bin/google-chrome'].filter(Boolean).find(file => fs.existsSync(file));

test('contribution dialog edits, retains drafts, signs in, submits safely and translates with real DOM', {skip: !chromium, timeout: 60000}, async () => {
    const draftId = 'e59148c0-74b8-49da-98f0-437112b8e679';
    let draft = {id: draftId, title: '本地研究 <img src=x onerror=alert(1)>', body: 'Review evidence', content: '{"conclusion":"Before"}', status: 'draft'};
    let authenticated = false, available = true, login = {state: 'failed', error: 'Previous GitHub sign-in failed'}, failSave = false, submitResolve, updateResolve, failUpdate = true;
    const requests = [];
    const server = http.createServer(async (req, res) => {
        try {
            const chunks = []; for await (const chunk of req) chunks.push(chunk);
            const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
            if (req.url.startsWith('/api/')) {
                requests.push({method: req.method, path: req.url, body, locale: req.headers['accept-language']});
                res.setHeader('Content-Type', 'application/json');
                const send = value => res.end(JSON.stringify(value));
                if (req.url === '/api/bootstrap') return send({workflow_version:'fixture-current',platform:'fixture',defaults:{max_seconds:7200,max_tokens:10000},model_config:{default:{model:'fixture',reasoning_effort:'high'},roles:{},stages:{}},roles:[],environment:{ready:true,checks:[]},recent_runs:[]});
                if (req.url === '/api/runs/fixture-run') return send({id:'fixture-run',project_name:'Fixture research Run',status:'succeeded',budget:{max_seconds:7200,max_tokens:10000},stages:{},messages:[],supervisor:{busy:false}});
                if (req.url === '/api/wiki/update') {
                    if (failUpdate) {res.statusCode=503; return send({error:'Wiki download unavailable'});}
                    await new Promise(resolve=>{updateResolve=resolve;});
                    return send({updated:true});
                }
                if (req.url === '/api/wiki/contributions') return send({drafts: [draft]});
                if (req.url === `/api/wiki/contributions/${draftId}`) {
                    if (req.method === 'POST') {
                        if (failSave) {res.statusCode = 400; return send({error: 'Research JSON is invalid <script>unsafe()</script>'});}
                        draft = {...draft, ...body};
                    }
                    return send(draft);
                }
                if (req.url.endsWith('/submit')) {
                    await new Promise(resolve => {submitResolve = resolve;});
                    draft = {...draft, status: 'submitted', pr_url: `${wiki}/pull/42`};
                    return send({status: 'submitted', pr_url: draft.pr_url});
                }
                if (req.url === '/api/github/status') return send({available, authenticated, login: authenticated ? 'test-contributor' : null, ...(!authenticated ? {error: available ? 'Sign in to GitHub in this app before submitting' : 'GitHub CLI (gh) is not installed'} : {})});
                if (req.url === '/api/github/login/cancel') {login = {state: 'cancelled'}; return send(login);}
                if (req.url === '/api/github/login') {
                    if (req.method === 'POST') login = {state: 'waiting', verification_url: 'https://github.com/login/device', user_code: 'ABCD-1234'};
                    return send(login);
                }
                if (req.url === '/api/wiki/export') return send({drafts: [draft]});
                res.statusCode = 404; return send({error: 'Unknown fixture route'});
            }
            const filename = req.url === '/' ? 'index.html' : req.url.slice(1);
            if (!['index.html', 'style.css', 'i18n.js', 'en.js', 'wiki_contributions.js', 'app.js', 'model_setup.js', 'fallback_setup.js'].includes(filename)) {res.statusCode = 404; return res.end();}
            res.setHeader('Content-Type', filename.endsWith('.js') ? 'text/javascript' : filename.endsWith('.css') ? 'text/css' : 'text/html');
            let content = fs.readFileSync(path.join(staticRoot, filename), 'utf8');
            if (filename === 'index.html') {
                content = content.replace('</head>', '<script>localStorage.setItem("modport.language","en"); window.openedLinks=[]; window.modport={openContributionLink:async url=>window.openedLinks.push(url)};</script></head>');
            }
            res.end(content);
        } catch (error) {res.statusCode = 500; res.end(JSON.stringify({error: error.message}));}
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'modport-contributions-'));
    const browser = spawn(chromium, ['--headless', '--no-sandbox', '--disable-dev-shm-usage', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--window-size=1440,1000', 'about:blank'], {stdio: ['ignore', 'ignore', 'pipe']});
    let socket;
    try {
        const address = await new Promise((resolve, reject) => {
            let stderr = ''; const timeout = setTimeout(() => reject(new Error(`Chromium startup timeout: ${stderr.slice(-500)}`)), 10000);
            browser.stderr.on('data', data => {stderr += data; const match = stderr.match(/DevTools listening on (ws:\/\/[^\s]+)/); if (match) {clearTimeout(timeout); resolve(match[1]);}});
            browser.once('error', error => {clearTimeout(timeout); reject(error);});
            browser.once('exit', code => {clearTimeout(timeout); reject(new Error(`Chromium exited: ${code}; ${stderr.slice(-500)}`));});
        });
        const origin = new URL(address); origin.protocol = 'http:';
        let page;
        const pageDeadline = Date.now() + 3000;
        while (!page && Date.now() < pageDeadline) {
            const targets = await (await fetch(`${origin.origin}/json/list`)).json();
            page = targets.find(target => target.type === 'page');
            if (!page) await new Promise(resolve => setTimeout(resolve, 60));
        }
        assert.ok(page, 'Chromium did not expose a page target within three seconds');
        socket = new WebSocket(page.webSocketDebuggerUrl);
        await new Promise((resolve, reject) => {socket.addEventListener('open', resolve, {once: true}); socket.addEventListener('error', reject, {once: true});});
        const pending = new Map(), pageErrors = []; let sequence = 0;
        socket.addEventListener('message', ({data}) => {
            const result = JSON.parse(data);
            if (result.method === 'Runtime.exceptionThrown') pageErrors.push(result.params.exceptionDetails);
            if (pending.has(result.id)) {const {resolve, reject} = pending.get(result.id); pending.delete(result.id); result.error ? reject(new Error(result.error.message)) : resolve(result.result);}
        });
        const command = (method, params = {}) => new Promise((resolve, reject) => {const id = ++sequence; pending.set(id, {resolve, reject}); socket.send(JSON.stringify({id, method, params}));});
        const evaluate = async expression => {const result = await command('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true}); if (result.exceptionDetails) throw new Error(result.exceptionDetails.text); return result.result.value;};
        const waitFor = async expression => {const until = Date.now() + 5000; while (Date.now() < until) {if (await evaluate(expression)) return; await new Promise(resolve => setTimeout(resolve, 40));} throw new Error(`Timed out: ${expression}; ${JSON.stringify(await evaluate('({error:document.getElementById("wiki-contributions-error").textContent,status:document.getElementById("wiki-contributions-status").textContent,open:document.getElementById("wiki-contributions-dialog").open})'))}`);};
        const click = id => evaluate(`document.getElementById(${JSON.stringify(id)}).click()`);
        const edit = (id, value) => evaluate(`(() => {const field=document.getElementById(${JSON.stringify(id)}); field.value=${JSON.stringify(value)}; field.dispatchEvent(new Event('input',{bubbles:true}));})()`);
        await command('Runtime.enable');
        await command('Page.navigate', {url: `http://127.0.0.1:${server.address().port}/#run`});
        await waitFor('Boolean(window.ModPortContributions) && !document.getElementById("project-next").disabled');
        assert.equal(await evaluate('document.getElementById("run-contributions-button").disabled'), true);
        await evaluate('ModPortI18n.apply(document)');
        assert.equal(await evaluate('ModPortContributions.initialize()===ModPortContributions.initialize()'), true);
        await click('wiki-contributions-button');
        await waitFor('document.getElementById("contribution-title").value.includes("本地研究")');
        assert.equal(await evaluate('document.getElementById("wiki-contributions-dialog").open'), true);
        assert.equal(await evaluate('document.getElementById("contribution-submit").disabled'), true);
        assert.match(await evaluate('document.getElementById("contribution-account-diagnostic").textContent'), /Sign in to GitHub in this app/);
        assert.match(await evaluate('document.getElementById("contribution-account-diagnostic").textContent'), /Previous GitHub sign-in failed/);
        assert.equal(await evaluate('document.getElementById("wiki-contributions-error").hidden'), true);
        assert.equal(await evaluate('document.querySelectorAll("#contribution-draft-list img").length'), 0);
        assert.equal(await evaluate('document.getElementById("wiki-contributions-heading").textContent'), 'Research contributions');
        await edit('contribution-title', 'Edited research');
        await edit('contribution-content', '{"conclusion":"Reviewed <script>unsafe()</script>"}');
        await edit('contribution-body', 'Reviewed evidence <b>literal</b>');
        await click('contribution-wiki-update');
        await waitFor('document.getElementById("wiki-contributions-error").textContent==="Wiki download unavailable"');
        assert.equal(await evaluate('document.getElementById("contribution-wiki-update").disabled'), false);
        assert.equal(await evaluate('document.getElementById("contribution-title").value'), 'Edited research');
        failUpdate = false;
        await click('contribution-wiki-update');
        await waitFor('document.getElementById("contribution-wiki-update").disabled');
        assert.equal(await evaluate('document.getElementById("contribution-wiki-update").textContent'), 'Updating Wiki…');
        const updateDeadline=Date.now()+5000;
        while (!updateResolve && Date.now()<updateDeadline) await new Promise(resolve=>setTimeout(resolve,30));
        assert.ok(updateResolve); updateResolve();
        await waitFor('!document.getElementById("contribution-wiki-update").disabled');
        assert.match(await evaluate('document.getElementById("wiki-contributions-status").textContent'), /New migration instances/);
        assert.equal(await evaluate('document.getElementById("contribution-content").value'), '{"conclusion":"Reviewed <script>unsafe()</script>"}');
        assert.deepEqual(requests.findLast(item=>item.path==='/api/wiki/update').body, {});
        await click('contribution-refresh');
        await waitFor('!document.getElementById("contribution-refresh").disabled');
        assert.equal(await evaluate('document.getElementById("contribution-title").value'), 'Edited research');
        await evaluate('ModPortI18n.setLocale("zh-CN"); ModPortI18n.apply(document)');
        assert.equal(await evaluate('document.getElementById("wiki-contributions-heading").textContent'), '研究贡献');
        assert.equal(await evaluate('document.getElementById("contribution-title").value'), 'Edited research');
        assert.match(await evaluate('document.getElementById("contribution-save-state").textContent'), /未保存/);
        await click('wiki-contributions-close'); await click('wiki-contributions-button');
        await waitFor('document.getElementById("wiki-contributions-status").textContent===""');
        assert.equal(await evaluate('document.getElementById("contribution-content").value'), '{"conclusion":"Reviewed <script>unsafe()</script>"}');
        failSave = true;
        await click('contribution-save');
        await waitFor('!document.getElementById("wiki-contributions-error").hidden');
        assert.equal(await evaluate('document.activeElement.id'), 'wiki-contributions-error');
        assert.match(await evaluate('document.getElementById("wiki-contributions-error").textContent'), /<script>/);
        assert.equal(await evaluate('document.querySelectorAll("#wiki-contributions-error script").length'), 0);
        assert.equal(await evaluate('document.getElementById("contribution-save").disabled'), false);
        failSave = false;
        await click('contribution-save');
        await waitFor('document.getElementById("contribution-save").disabled && document.getElementById("contribution-editor").getAttribute("aria-busy")==="false"');
        assert.equal(draft.title, 'Edited research'); assert.match(draft.content, /Reviewed/); assert.match(draft.body, /literal/);
        assert.equal(requests.findLast(item => item.path === `/api/wiki/contributions/${draftId}` && item.method === 'POST').locale, 'zh-CN');
        await click('contribution-login');
        await waitFor('!document.getElementById("contribution-device-login").hidden');
        assert.equal(await evaluate('document.getElementById("contribution-device-code").textContent'), 'ABCD-1234');
        await click('contribution-device-link');
        assert.deepEqual(await evaluate('openedLinks'), ['https://github.com/login/device']);
        await click('contribution-login-cancel');
        await waitFor('document.getElementById("contribution-device-login").hidden');
        assert.equal(login.state, 'cancelled');
        await click('contribution-login');
        await waitFor('!document.getElementById("contribution-device-login").hidden');
        await click('wiki-contributions-close');
        const before = requests.filter(item => item.path === '/api/github/login' && item.method === 'GET').length;
        await new Promise(resolve => setTimeout(resolve, 1750));
        assert.equal(requests.filter(item => item.path === '/api/github/login' && item.method === 'GET').length, before, 'closed dialog must stop sign-in polling');
        await click('wiki-contributions-button');
        await waitFor('!document.getElementById("contribution-device-login").hidden');
        authenticated = true; login = {state: 'authenticated'};
        await waitFor('!document.getElementById("contribution-submit").disabled');
        assert.match(await evaluate('document.getElementById("contribution-submit-account").textContent'), /test-contributor/);
        await edit('contribution-content', '{"conclusion":"Ready to submit"}');
        await click('contribution-submit');
        await waitFor('document.getElementById("contribution-editor").getAttribute("aria-busy")==="true"');
        assert.equal(await evaluate('document.getElementById("contribution-title").disabled'), true);
        assert.equal(await evaluate('document.getElementById("contribution-save").disabled'), true);
        const submitDeadline = Date.now() + 5000;
        while (!submitResolve && Date.now() < submitDeadline) await new Promise(resolve => setTimeout(resolve, 30));
        assert.ok(submitResolve);
        assert.equal(draft.content, '{"conclusion":"Ready to submit"}');
        assert.deepEqual(requests.findLast(item => item.path.endsWith('/submit')).body, {expected_login: 'test-contributor'});
        submitResolve();
        await waitFor('!document.getElementById("contribution-pr-link").hidden');
        await click('contribution-pr-link');
        assert.equal((await evaluate('openedLinks')).at(-1), `${wiki}/pull/42`);
        assert.equal(await evaluate('document.getElementById("contribution-submit").disabled'), true);
        await command('Emulation.setDeviceMetricsOverride', {width: 390, height: 780, deviceScaleFactor: 1, mobile: false});
        assert.equal(await evaluate('document.getElementById("wiki-contributions-dialog").scrollWidth<=document.getElementById("wiki-contributions-dialog").clientWidth'), true);
        assert.equal(await evaluate('document.documentElement.scrollWidth<=innerWidth'), true);
        await click('wiki-contributions-close');
        available = false; authenticated = false;
        await evaluate('location.hash="run=fixture-run"');
        await waitFor('document.getElementById("run-heading").textContent==="Fixture research Run"');
        assert.equal(await evaluate('document.getElementById("run-contributions-button").disabled'), false);
        await click('run-contributions-button');
        await waitFor('document.getElementById("contribution-account-status").textContent.includes("GitHub CLI")');
        assert.deepEqual(requests.findLast(item => item.path === '/api/wiki/export').body, {instance_id: 'fixture-run'});
        assert.equal(await evaluate('document.getElementById("contribution-login").disabled'), true);
        assert.match(await evaluate('document.getElementById("contribution-account-diagnostic").textContent'), /GitHub CLI.*not installed/);
        assert.equal(await evaluate('document.getElementById("wiki-contributions-error").hidden'), true);
        await evaluate('window.modport.request=async () => ({error:"Native service error"})');
        await click('contribution-refresh');
        await waitFor('document.getElementById("wiki-contributions-error").textContent==="Native service error"');
        assert.deepEqual(pageErrors, []);
    } finally {
        if (submitResolve) submitResolve();
        if (updateResolve) updateResolve();
        socket?.close();
        browser.kill('SIGTERM');
        await new Promise(resolve => {if (browser.exitCode !== null) resolve(); else {const timeout = setTimeout(() => {browser.kill('SIGKILL'); resolve();}, 2000); browser.once('exit', () => {clearTimeout(timeout); resolve();});}});
        server.closeAllConnections();
        await new Promise(resolve => server.close(resolve));
        fs.rmSync(profile, {recursive: true, force: true});
    }
});
