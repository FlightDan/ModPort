/* Local drafts and browser sign-in. Authentication remains in the host service. */
(function (root) {
    'use strict';
    const wikiPath = '/FlightDan/modport-wiki-for-agents';
    const instances = new WeakMap();
    function safeLink(value, kind) {
        if (typeof value !== 'string') return null;
        try {
            const url = new URL(value);
            if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password || url.search || url.hash) return null;
            if (kind === 'login' && url.pathname === '/login/device') return url.href;
            if (kind === 'pr' && new RegExp(`^${wikiPath}/pull/[1-9][0-9]*$`).test(url.pathname)) return url.href;
            if (kind === 'wiki' && url.pathname === wikiPath) return url.href;
        } catch { /* Invalid destinations remain non-interactive. */ }
        return null;
    }
    function initialize(document = root.document) {
        if (!document?.getElementById('wiki-contributions-dialog')) return null;
        if (instances.has(document)) return instances.get(document);
        const i18n = root.ModPortI18n;
        const t = (...args) => i18n?.t(...args) || args[0];
        const $ = id => document.getElementById(id);
        const dialog = $('wiki-contributions-dialog');
        const accountDiagnostic = document.createElement('p');
        accountDiagnostic.id = 'contribution-account-diagnostic';
        accountDiagnostic.className = 'field-hint';
        accountDiagnostic.setAttribute('role', 'status');
        accountDiagnostic.setAttribute('aria-live', 'polite');
        $('contribution-account-status').insertAdjacentElement('afterend', accountDiagnostic);
        const cache = new Map();
        let drafts = [], selected = null, github = {}, login = {}, busy = '', loginBusy = false;
        let pollTimer, openEpoch = 0, selectionEpoch = 0, loginEpoch = 0, message = '', opener;
        const bind = (node, render) => i18n ? i18n.bindText(node, render) : node.textContent = render();
        const text = (node, value) => i18n ? i18n.setText(node, value) : node.textContent = value;
        async function request(method, path, body) {
            let result;
            if (root.modport?.request) result = await root.modport.request({method, path, body});
            else {
                const response = await root.fetch(path, {method, headers: {'Content-Type': 'application/json', 'Accept-Language': i18n?.locale || 'en'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
                try { result = await response.json(); } catch { throw new Error(t('服务返回了无法读取的响应 ({0})', response.status)); }
                if (!response.ok) throw new Error(result?.error || t('请求失败 ({0})', response.status));
            }
            if (!result || typeof result !== 'object') throw new Error(t('本机服务没有返回有效数据。'));
            // Account status can describe an unavailable account or failed login
            // while local drafts remain available. HTTP and mutation failures
            // still reject through the transport or the ordinary error path.
            const accountStatus = method === 'GET' && path === '/api/github/status'
                && typeof result.available === 'boolean' && typeof result.authenticated === 'boolean';
            const loginStatus = method === 'GET' && path === '/api/github/login'
                && ['idle', 'starting', 'waiting', 'authenticated', 'failed', 'expired', 'cancelled'].includes(result.state);
            if (result.error && !accountStatus && !loginStatus) throw new Error(result.error);
            return result;
        }
        function error(value) {
            const node = $('wiki-contributions-error');
            text(node, value ? value.message || String(value) : '');
            node.hidden = !value;
            if (value && dialog.open) node.focus();
        }
        function account() { return typeof github.login === 'string' ? github.login : github.login?.login || ''; }
        function current() { return selected ? cache.get(selected) : null; }
        function setLink(node, value, kind) {
            const destination = safeLink(value, kind);
            node.hidden = !destination;
            if (destination) node.setAttribute('href', destination); else node.removeAttribute('href');
        }
        function renderList() {
            const list = $('contribution-draft-list');
            list.replaceChildren();
            $('contribution-empty').hidden = drafts.length !== 0;
            for (const draft of drafts) {
                const button = document.createElement('button');
                button.type = 'button'; button.className = 'contribution-draft';
                button.setAttribute('aria-pressed', String(selected === draft.id));
                button.disabled = Boolean(busy);
                const title = document.createElement('strong');
                text(title, cache.get(draft.id)?.title ?? draft.title ?? draft.id);
                const status = document.createElement('small');
                const statusValue = cache.get(draft.id)?.status || draft.status;
                bind(status, () => t(statusValue === 'submitted' || draft.pr_url ? '已提交 Draft PR' : '草稿'));
                button.append(title, status);
                button.addEventListener('click', () => select(draft.id, true));
                list.append(button);
            }
        }
        function render() {
            const draft = current(), waiting = ['starting', 'waiting'].includes(login.state);
            $('contribution-editor').hidden = !draft;
            for (const id of ['contribution-title', 'contribution-content', 'contribution-body']) $(id).disabled = Boolean(busy) || Boolean(draft?.pr_url);
            $('contribution-refresh').disabled = Boolean(busy);
            $('contribution-wiki-update').disabled = Boolean(busy);
            $('contribution-save').disabled = !draft || !draft.dirty || Boolean(busy) || Boolean(draft.pr_url);
            $('contribution-submit').disabled = !draft || Boolean(busy) || !github.authenticated || !account() || Boolean(draft.pr_url);
            $('contribution-editor').setAttribute('aria-busy', String(Boolean(busy)));
            $('contribution-login').disabled = loginBusy || waiting || github.available === false || Boolean(busy);
            $('contribution-login').hidden = Boolean(github.authenticated);
            $('contribution-login-cancel').disabled = loginBusy;
            $('contribution-device-login').hidden = !waiting;
            text($('contribution-device-code'), login.user_code || '');
            $('contribution-device-code').hidden = !login.user_code;
            setLink($('contribution-device-link'), login.verification_url || login.verification_uri, 'login');
            setLink($('contribution-pr-link'), draft?.pr_url, 'pr');
            bind($('wiki-contributions-status'), () => t(message));
            bind($('contribution-account-status'), () => github.available === false ? t('请安装 GitHub CLI（gh），然后重新打开此窗口。安装说明：cli.github.com。') : github.authenticated ? t('已登录：{0}', account()) : t('提交前请在浏览器中登录 GitHub。'));
            const diagnostics = [github.error, ['failed', 'expired'].includes(login.state) ? login.error : null].filter(value => typeof value === 'string' && value);
            text(accountDiagnostic, [...new Set(diagnostics)].join('\n'));
            accountDiagnostic.hidden = diagnostics.length === 0;
            accountDiagnostic.setAttribute('aria-label', t('GitHub 账号'));
            bind($('contribution-login-status'), () => t(login.state === 'starting' ? '正在启动登录…' : '等待浏览器授权…'));
            bind($('contribution-submit-account'), () => github.authenticated ? t('Draft PR 将以 GitHub 账号 {0} 提交。', account()) : t('登录后可提交 Draft PR。'));
            bind($('contribution-save-state'), () => draft?.pr_url ? t('已提交 Draft PR') : draft?.dirty ? t('有未保存的修改。关闭窗口后，修改会保留在本次应用会话中。') : draft ? t('草稿已保存到本机。') : '');
            bind($('contribution-wiki-update'), () => t(busy === 'updating' ? '正在更新 Wiki…' : '更新本地 Wiki'));
            bind($('contribution-save'), () => t(busy === 'saving' ? '正在保存…' : '保存草稿'));
            bind($('contribution-submit'), () => t(busy === 'submitting' ? '正在提交 Draft PR…' : '提交 Draft PR'));
            bind($('contribution-login'), () => t(loginBusy || login.state === 'starting' ? '正在启动登录…' : '在浏览器中登录 GitHub'));
        }
        async function select(id, focus = false) {
            if (busy) return;
            const epoch = ++selectionEpoch;
            try {
                if (!cache.has(id)) {
                    const result = await request('GET', `/api/wiki/contributions/${encodeURIComponent(id)}`);
                    cache.set(id, {...(result.draft || result), dirty: false});
                }
                if (epoch !== selectionEpoch) return;
                selected = id;
                const draft = current();
                $('contribution-title').value = draft.title || '';
                $('contribution-body').value = draft.body || '';
                $('contribution-content').value = draft.content || '';
                error(null); renderList(); render();
                if (focus && dialog.open) $('contribution-title').focus();
            } catch (failure) { error(failure); }
        }
        function stopPolling() { root.clearTimeout(pollTimer); pollTimer = undefined; }
        function schedulePoll() {
            stopPolling();
            if (!dialog.open || !['starting', 'waiting'].includes(login.state)) return;
            const epoch = openEpoch, authEpoch = loginEpoch;
            pollTimer = root.setTimeout(async () => {
                try {
                    const result = await request('GET', '/api/github/login');
                    if (epoch !== openEpoch || authEpoch !== loginEpoch || !dialog.open) return;
                    login = result;
                    if (['authenticated', 'succeeded'].includes(login.state)) github = await request('GET', '/api/github/status');
                    if (epoch !== openEpoch || authEpoch !== loginEpoch || !dialog.open) return;
                    if (['failed', 'expired'].includes(login.state)) error(login.error || login.message || t('GitHub 登录失败，请重试。'));
                    render(); schedulePoll();
                } catch (failure) { if (epoch === openEpoch && authEpoch === loginEpoch && dialog.open) error(failure); }
            }, 1500);
        }
        async function refresh() {
            const epoch = openEpoch;
            try {
                const authEpoch = loginEpoch;
                const [list, status, loginStatus] = await Promise.all([request('GET', '/api/wiki/contributions'), request('GET', '/api/github/status'), request('GET', '/api/github/login')]);
                if (epoch !== openEpoch || !dialog.open) return;
                drafts = Array.isArray(list.drafts) ? list.drafts : [];
                github = status;
                if (authEpoch === loginEpoch) login = loginStatus;
                if (status.authenticated) login = {state: 'authenticated'};
                if (!selected && drafts.length) await select(drafts[0].id);
                renderList(); render(); schedulePoll();
            } catch (failure) { if (epoch === openEpoch && dialog.open) error(failure); }
        }
        async function open(runId) {
            if (dialog.open) { if (!runId) return; }
            else { opener = document.activeElement; dialog.showModal(); }
            const epoch = ++openEpoch;
            message = runId ? '正在导出研究草稿…' : '正在读取研究草稿…'; error(null); render();
            try {
                let exportedId;
                if (runId) {const exported = await request('POST', '/api/wiki/export', {instance_id: runId}); exportedId = exported.drafts?.[0]?.id;}
                if (epoch !== openEpoch || !dialog.open) return;
                await refresh();
                if (epoch !== openEpoch || !dialog.open) return;
                if (exportedId && exportedId !== selected && !current()?.dirty) await select(exportedId);
                message = ''; render();
            } catch (failure) { if (epoch === openEpoch && dialog.open) {message = ''; render(); error(failure);} }
        }
        async function saveDraft(draft) {
            const result = await request('POST', `/api/wiki/contributions/${encodeURIComponent(draft.id)}`, {title: draft.title, body: draft.body, content: draft.content});
            cache.set(draft.id, {...draft, ...(result.draft || result), dirty: false});
        }
        async function perform(kind) {
            const draft = current();
            if (!draft || busy || draft.pr_url) return;
            if (!$('contribution-editor').reportValidity()) return;
            if (kind === 'submitting' && (!github.authenticated || !account())) return;
            busy = kind; error(null); renderList(); render();
            try {
                if (draft.dirty) await saveDraft(draft);
                if (kind === 'submitting') {
                    const result = await request('POST', `/api/wiki/contributions/${encodeURIComponent(draft.id)}/submit`, {expected_login: account()});
                    cache.set(draft.id, {...current(), ...result, status: 'submitted', dirty: false});
                    message = 'Draft PR 已提交，等待维护者审阅。';
                } else message = '草稿已保存到本机。';
            } catch (failure) { error(failure); }
            finally { busy = ''; renderList(); render(); }
        }
        for (const [id, key] of [['contribution-title', 'title'], ['contribution-body', 'body'], ['contribution-content', 'content']]) {
            $(id).addEventListener('input', () => {const draft = current(); if (!draft || busy) return; draft[key] = $(id).value; draft.dirty = true; message = ''; render();});
        }
        $('wiki-contributions-button').addEventListener('click', () => open());
        $('wiki-contributions-close').addEventListener('click', () => dialog.close());
        dialog.addEventListener('close', () => {if (dialog.open) return; ++openEpoch; ++selectionEpoch; stopPolling(); if (opener?.isConnected) opener.focus();});
        $('contribution-refresh').addEventListener('click', () => {error(null); refresh();});
        $('contribution-wiki-update').addEventListener('click', async () => {
            if (busy) return;
            busy = 'updating'; message = ''; error(null); renderList(); render();
            try {
                await request('POST', '/api/wiki/update', {});
                message = '本地 Wiki 已更新，之后新建的迁移实例将使用最新内容。';
            } catch (failure) { error(failure); }
            finally {busy = ''; renderList(); render();}
        });
        $('contribution-save').addEventListener('click', () => perform('saving'));
        $('contribution-editor').addEventListener('submit', event => {event.preventDefault(); perform('submitting');});
        $('contribution-login').addEventListener('click', async () => {
            if (loginBusy) return;
            loginBusy = true; ++loginEpoch; error(null); render();
            try {
                login = await request('POST', '/api/github/login');
                if (['authenticated', 'succeeded'].includes(login.state)) github = await request('GET', '/api/github/status');
                if (['failed', 'expired'].includes(login.state)) error(login.error || login.message || t('GitHub 登录失败，请重试。'));
            } catch (failure) { error(failure); }
            finally {loginBusy = false; render(); schedulePoll();}
        });
        $('contribution-login-cancel').addEventListener('click', async () => {
            if (loginBusy) return;
            loginBusy = true; ++loginEpoch; stopPolling(); render();
            try {login = await request('POST', '/api/github/login/cancel');}
            catch (failure) {error(failure);}
            finally {loginBusy = false; render(); schedulePoll();}
        });
        for (const [id, kind] of [['contribution-device-link', 'login'], ['contribution-pr-link', 'pr']]) {
            $(id).addEventListener('click', async event => {
                const url = safeLink($(id).getAttribute('href'), kind);
                if (!url) {event.preventDefault(); return;}
                if (root.modport?.openContributionLink) {
                    event.preventDefault();
                    try {await root.modport.openContributionLink(url);} catch (failure) {error(failure);}
                }
            });
        }
        root.addEventListener('modport:contributions-open', event => open(event.detail?.runId));
        const api = {open, refresh};
        instances.set(document, api); render();
        return api;
    }
    const api = {initialize, safeLink};
    if (typeof module === 'object' && module.exports) module.exports = api;
    else {
        root.ModPortContributions = api;
        if (root.document?.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', () => initialize(), {once: true});
        else initialize();
    }
}(globalThis));
