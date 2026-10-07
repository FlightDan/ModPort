'use strict';
const {app, BrowserWindow, ipcMain, dialog, session, shell} = require('electron');
const {spawn} = require('node:child_process');
const {randomBytes} = require('node:crypto');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');
const readline = require('node:readline');
const {pathToFileURL} = require('node:url');
const {LanguageSettings, translate} = require('./i18n.cjs');
const {UpdateChecker} = require(path.join(__dirname, 'updates.cjs'));
let languageSettings;
let updateChecker;
const t = (key, values) => translate(languageSettings?.language || 'en', key, values);

let window, service, port;
let pendingRequests = 0, quitRequested = false, serviceExited = false, serviceClosing = false;
const token = randomBytes(32).toString('hex');
const root = path.resolve(__dirname, '..', '..');
const packaged = fs.existsSync(path.join(root, 'runtime', 'modport'));
const source = packaged ? path.join(root, 'runtime') : path.resolve(__dirname, '..', 'src');
const staticPage = path.join(source, 'modport', 'desktop_static', 'index.html');
const pageUrl = pathToFileURL(staticPage).href;
if (process.env.MODPORT_DESKTOP_DATA_ROOT) {
  if (!path.isAbsolute(process.env.MODPORT_DESKTOP_DATA_ROOT)) throw new Error('Desktop data root must be absolute');
  app.setPath('userData', process.env.MODPORT_DESKTOP_DATA_ROOT);
}

function validRequest(input) {
  if (!input || !['GET', 'POST'].includes(input.method) || typeof input.path !== 'string') return false;
  const route = input.path;
  return input.method === 'GET'
    ? /^\/api\/(bootstrap|model-settings|runs\/[A-Za-z0-9_-]{1,96}|github\/(status|login)|wiki\/contributions(?:\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})?)$/.test(route)
    : /^\/api\/(repository|runs|setup|model-settings|runs\/[A-Za-z0-9_-]{1,96}\/(chat|cancel)|github\/login(?:\/cancel)?|wiki\/(export|update|contributions\/[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}(?:\/submit)?))$/.test(route);
}

function apiRequest(input, nativeFolderSelection = false) {
  const selectedFolder = nativeFolderSelection && input?.method === 'POST' && input.path === '/api/local-source';
  if (!selectedFolder && !validRequest(input)) return Promise.reject(new Error(t('unsupportedRequest')));
  const body = input.body === undefined ? null : JSON.stringify(input.body);
  const bodyLimit = input.path.startsWith('/api/wiki/contributions/') ? 2 * 1024 * 1024 : 65536;
  if (body !== null && Buffer.byteLength(body) > bodyLimit) return Promise.reject(new Error(t('requestTooLarge')));
  return new Promise((resolve, reject) => {
    const request = http.request({hostname: '127.0.0.1', port, path: input.path,
      method: input.method, headers: {Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json', 'Accept-Language': languageSettings?.language || 'en', ...(body === null ? {} : {'Content-Length': Buffer.byteLength(body)})}}, response => {
      let size = 0, chunks = [];
      response.on('data', chunk => {
        size += chunk.length;
        if (size > 4 * 1024 * 1024) {request.destroy(new Error(t('responseTooLarge'))); return;}
        chunks.push(chunk);
      });
      response.on('end', () => {
        try {
          const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          if (response.statusCode >= 400) reject(new Error(data.error || t('requestFailed', {status: response.statusCode})));
          else resolve(data);
        } catch (error) {reject(error);}
      });
    });
    request.setTimeout(120000, () => request.destroy(new Error(t('requestTimeout'))));
    request.on('error', reject);
    request.end(body);
  });
}

async function startService() {
  const runtimePython = process.platform === 'win32'
    ? path.join(root, 'python', 'python.exe') : path.join(root, 'python', 'bin', 'python3');
  const python = packaged ? runtimePython : process.env.MODPORT_DESKTOP_PYTHON || path.resolve(__dirname, '..', '.venv', 'bin', 'python');
  // Source roots are explicit so packaged execution cannot load another installed ModPort.
  const code = `import sys; sys.path.insert(0, ${JSON.stringify(source)}); from modport.desktop_service import main; raise SystemExit(main())`;
  const environment = {...process.env};
  if (packaged) {
    const inheritedPath = Object.keys(environment).find(key => key.toUpperCase() === 'PATH');
    const searchPath = inheritedPath ? environment[inheritedPath] : '';
    for (const key of Object.keys(environment)) if (key.toUpperCase() === 'PATH') delete environment[key];
    environment.PATH = path.join(root, 'tools') + path.delimiter + searchPath;
    environment.MODPORT_OPENCODE_BIN = path.join(root, 'tools', process.platform === 'win32' ? 'opencode.exe' : 'opencode');
  }
  service = spawn(python, ['-I', '-c', code, '--data-root', app.getPath('userData'), '--token-stdin'], {
    cwd: packaged ? root : path.resolve(__dirname, '..'), env: environment,
    windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']
  });
  service.stdin.write(token + '\n');
  service.once('exit', () => {serviceExited = true; if (quitRequested) app.quit();});
  let stderr = '';
  service.stderr.on('data', data => {stderr = (stderr + data.toString()).slice(-8192);});
  return new Promise((resolve, reject) => {
    const lines = readline.createInterface({input: service.stdout});
    const timer = setTimeout(() => {service.kill(); reject(new Error(t('serviceNotStarted', {detail: stderr})));}, 30000);
    service.once('error', error => {clearTimeout(timer); reject(error);});
    service.once('exit', code => {clearTimeout(timer); reject(new Error(t('serviceExited', {code, detail: stderr})));});
    lines.on('line', line => {
      try {
        const ready = JSON.parse(line);
        if (Number.isInteger(ready.port) && ready.port > 0 && ready.port <= 65535) {
          port = ready.port; clearTimeout(timer); lines.close(); resolve();
        }
      } catch (_) { /* Diagnostics do not masquerade as readiness. */ }
    });
  });
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => {if (window) {if (window.isMinimized()) window.restore(); window.show(); window.focus();}});
  app.whenReady().then(async () => {
    languageSettings = new LanguageSettings(app.getPath('userData'), app.getPreferredSystemLanguages());
    await startService();
    session.defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
    session.defaultSession.setPermissionCheckHandler(() => false);
    window = new BrowserWindow({width: 1440, height: 960, minWidth: 720, minHeight: 540,
      title: 'ModPort', backgroundColor: '#0a0e13', show: false, autoHideMenuBar: true,
      webPreferences: {preload: path.join(__dirname, 'preload.cjs'), nodeIntegration: false,
        contextIsolation: true, sandbox: true, webSecurity: true, webviewTag: false}});
    window.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
    window.webContents.on('will-navigate', (event, url) => {if (url.split('#')[0] !== pageUrl) event.preventDefault();});
    window.webContents.on('will-attach-webview', event => event.preventDefault());
    const assertTrustedFrame = event => {
      if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame ||
          event.senderFrame.url.split('#')[0] !== pageUrl) throw new Error(t('untrustedFrame'));
    };
    updateChecker = new UpdateChecker({currentVersion: require(path.join(__dirname, 'package.json')).version});
    updateChecker.subscribe(status => {
      if (!window.isDestroyed() && !window.webContents.isDestroyed()) window.webContents.send('modport:update-status', status);
    });
    ipcMain.handle('modport:get-update-status', event => {assertTrustedFrame(event); return updateChecker.getStatus();});
    ipcMain.handle('modport:check-for-updates', event => {assertTrustedFrame(event); return updateChecker.check();});
    ipcMain.handle('modport:open-update-download', async event => {
      assertTrustedFrame(event);
      const releaseUrl = updateChecker.getDownloadUrl();
      if (!releaseUrl) return {opened: false};
      await shell.openExternal(releaseUrl);
      return {opened: true};
    });
    ipcMain.handle('modport:open-contribution-link', async (event, value) => {
      assertTrustedFrame(event);
      const url = new URL(value);
      const wikiPath = '/FlightDan/modport-wiki-for-agents';
      if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port ||
          url.username || url.password || url.search || url.hash ||
          !(url.pathname === '/login/device' || url.pathname === wikiPath ||
            new RegExp('^' + wikiPath + '/pull/[1-9][0-9]*$').test(url.pathname))) {
        throw new Error(t('unsupportedRequest'));
      }
      await shell.openExternal(url.href);
      return {opened: true};
    });
    ipcMain.handle('modport:get-language', event => {assertTrustedFrame(event); return languageSettings.language;});
    ipcMain.handle('modport:set-language', (event, language) => {assertTrustedFrame(event); return languageSettings.set(language);});
    ipcMain.handle('modport:request', async (event, input) => {
      if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame ||
          event.senderFrame.url.split('#')[0] !== pageUrl) throw new Error(t('untrustedFrame'));
      pendingRequests += 1;
      try {return await apiRequest(input);}
      finally {
        pendingRequests -= 1;
        if (quitRequested && pendingRequests === 0) app.quit();
      }
    });
    ipcMain.handle('modport:select-source-directory', async event => {
      if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame ||
          event.senderFrame.url.split('#')[0] !== pageUrl) throw new Error(t('untrustedFrame'));
      pendingRequests += 1;
      try {
        const result = await dialog.showOpenDialog(window, {title: t('chooseSource'), properties: ['openDirectory']});
        if (result.canceled || result.filePaths.length !== 1) return {cancelled: true};
        return await apiRequest({method: 'POST', path: '/api/local-source', body: {path: result.filePaths[0]}}, true);
      } finally {
        pendingRequests -= 1;
        if (quitRequested && pendingRequests === 0) app.quit();
      }
    });
    ipcMain.handle('modport:open-workspace', async (event, instanceId) => {
      if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame ||
          event.senderFrame.url.split('#')[0] !== pageUrl) throw new Error(t('untrustedFrame'));
      if (typeof instanceId !== 'string' || !/^[A-Za-z0-9_-]{1,96}$/.test(instanceId)) throw new Error(t('invalidInstance'));
      pendingRequests += 1;
      try {
        const run = await apiRequest({method: 'GET', path: `/api/runs/${encodeURIComponent(instanceId)}`});
        const workspacePath = run?.workspace?.path;
        if (typeof workspacePath !== 'string' || !path.isAbsolute(workspacePath)) throw new Error(t('absoluteWorkspace'));
        let stat;
        try {stat = await fs.promises.stat(workspacePath);} catch {throw new Error(t('unavailableWorkspace'));}
        if (!stat.isDirectory()) throw new Error(t('directoryWorkspace'));
        const openError = await shell.openPath(workspacePath);
        if (openError) throw new Error(t('openWorkspace', {detail: openError}));
        return {opened: true};
      } finally {
        pendingRequests -= 1;
        if (quitRequested && pendingRequests === 0) app.quit();
      }
    });
    window.once('ready-to-show', () => {window.maximize(); window.show();});
    await window.loadFile(staticPage);
    void updateChecker.check();
    if (process.env.MODPORT_DESKTOP_SMOKE === '1') {
      const result = await window.webContents.executeJavaScript(`(async () => {
        const deadline = Date.now() + 10000;
        while (document.getElementById('project-next')?.disabled &&
            document.getElementById('feedback')?.classList.contains('error') !== true && Date.now() < deadline) {
          await new Promise(resolve => setTimeout(resolve, 50));
        }
        const bootstrap = await window.modport.request({method: 'GET', path: '/api/bootstrap'});
        return {workflow_version: bootstrap.workflow_version, platform: bootstrap.platform,
          native_language: await window.modport.getLanguage(), renderer_language: document.documentElement.lang,
          update_status: await window.modport.getUpdateStatus(),
          fallback_editor_ready: Boolean(document.getElementById('fallback-settings') && window.ModPortFallbackSetup),
          renderer_heading: document.getElementById('project-heading')?.textContent,
          renderer_ready: document.getElementById('project-next')?.disabled === false,
          renderer_error: document.getElementById('feedback')?.classList.contains('error') === true
            ? document.getElementById('feedback').textContent : null};
      })()`);
      console.log(JSON.stringify({desktop_smoke: result}));
      app.quit();
    }
  }).catch(error => {dialog.showErrorBox(t('startupTitle'), error.message); app.exit(1);});
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', event => {
    updateChecker?.dispose();
    // Let an accepted submission finish registering its durable driver. The
    // window can close immediately; no partial submission is killed on quit.
    if (pendingRequests > 0) {
      event.preventDefault(); quitRequested = true;
      if (window && !window.isDestroyed()) window.hide();
      return;
    }
    if (service && !serviceExited) {
      event.preventDefault(); quitRequested = true;
      if (window && !window.isDestroyed()) window.hide();
      if (!serviceClosing) {serviceClosing = true; service.stdin.end();}
    }
  });
}
