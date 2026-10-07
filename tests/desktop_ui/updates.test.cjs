'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {pathToFileURL} = require('node:url');
const {UpdateChecker, compareStableVersions, validatedReleaseUrl, RELEASE_ENDPOINT,
  MAX_RESPONSE_BYTES} = require('../../desktop/updates.cjs');

const released = tag => ({tag_name: tag, html_url: `https://github.com/FlightDan/ModPort/releases/tag/${tag}`,
  draft: false, prerelease: false});

function transport({status = 200, body = released('v0.2.0'), offline = false, silent = false,
  abort = false, rawBody} = {}) {
  const calls = [];
  const request = (url, options, callback) => {
    const outgoing = new EventEmitter();
    outgoing.destroyed = false;
    outgoing.destroy = () => {outgoing.destroyed = true;};
    calls.push({url, options, outgoing});
    outgoing.end = () => {
      if (silent) return;
      setImmediate(() => {
        if (outgoing.destroyed) return;
        if (offline) {outgoing.emit('error', new Error('ENOTFOUND')); return;}
        const response = new EventEmitter();
        response.statusCode = status;
        response.destroy = () => {response.destroyed = true;};
        callback(response);
        if (response.destroyed) return;
        if (abort) {response.emit('aborted'); return;}
        response.emit('data', rawBody ?? Buffer.from(JSON.stringify(body)));
        response.emit('end');
      });
    };
    return outgoing;
  };
  return {request, calls};
}

test('stable release comparison uses semantic precedence and rejects malformed/prerelease tags', () => {
  assert.equal(compareStableVersions('v0.10.0', '0.9.9'), 1);
  assert.equal(compareStableVersions('0.2.0', 'v0.2.0+desktop.1'), 0);
  assert.equal(compareStableVersions('0.2.0', '0.2.1'), -1);
  assert.equal(compareStableVersions('2.0.0', '1.99.99'), 1);
  for (const invalid of ['v0.2', '0.02.0', '0.2.0-rc.1', '0.2.0junk', 'v0.2.0\n', null]) {
    assert.throws(() => compareStableVersions(invalid, '0.1.0'), /invalid_version/);
  }
});

test('download URL is restricted to the exact stable release of the trusted repository', () => {
  assert.equal(validatedReleaseUrl(released('v0.2.0').html_url, 'v0.2.0'), released('v0.2.0').html_url);
  for (const url of [
    'http://github.com/FlightDan/ModPort/releases/tag/v0.2.0',
    'https://github.com.evil.example/FlightDan/ModPort/releases/tag/v0.2.0',
    'https://github.com@evil.example/FlightDan/ModPort/releases/tag/v0.2.0',
    'https://user:secret@github.com/FlightDan/ModPort/releases/tag/v0.2.0',
    'https://github.com/FlightDan/Other/releases/tag/v0.2.0',
    'https://github.com/FlightDan/ModPort/releases/tag/v0.2.0?redirect=evil',
    'https://github.com/FlightDan/ModPort/releases/tag/v0.2.0#evil',
    'https://github.com/FlightDan/ModPort/releases/tag/v0.2.1',
    'https://github.com/FlightDan/ModPort/releases/tag/%76%30.2.0',
    'javascript:alert(1)',
  ]) assert.equal(validatedReleaseUrl(url, 'v0.2.0'), null, url);
});

test('check publishes available/current states, bounds requests, uses no credentials and deduplicates concurrency', async () => {
  const native = transport();
  const checker = new UpdateChecker({currentVersion: '0.1.0', request: native.request,
    now: () => '2026-10-06T10:00:00.000Z'});
  const observed = [];
  const unsubscribe = checker.subscribe(state => observed.push(state));
  assert.equal(checker.getStatus().status, 'idle');
  const first = checker.check(), second = checker.check();
  assert.equal(first, second);
  assert.equal(checker.getStatus().status, 'checking');
  assert.equal((await first).status, 'available');
  assert.equal(native.calls.length, 1);
  assert.equal(native.calls[0].url, RELEASE_ENDPOINT);
  assert.deepEqual(Object.keys(native.calls[0].options.headers).sort(), ['Accept', 'User-Agent', 'X-GitHub-Api-Version']);
  assert.equal(checker.getDownloadUrl(), released('v0.2.0').html_url);
  assert.deepEqual(observed.map(state => state.status), ['checking', 'available']);
  assert.equal(checker.getStatus().checkedAt, '2026-10-06T10:00:00.000Z');
  checker.getStatus().releaseUrl = 'https://evil.example';
  assert.equal(checker.getDownloadUrl(), released('v0.2.0').html_url);
  unsubscribe();
  await checker.check();
  assert.equal(observed.length, 2);
  for (const version of ['0.2.0', '0.3.0']) {
    const latest = new UpdateChecker({currentVersion: version, request: transport().request});
    assert.equal((await latest.check()).status, 'current');
    assert.equal(latest.getDownloadUrl(), null);
  }
});

test('missing releases, offline, redirects, rate limits and invalid payloads remain nonfatal', async () => {
  const cases = [
    [{status: 404}, 'no_releases'], [{offline: true}, 'offline'],
    [{abort: true}, 'offline'], [{status: 302}, 'http_error'],
    [{status: 403}, 'rate_limited'], [{status: 429}, 'rate_limited'],
    [{rawBody: Buffer.from('not json')}, 'invalid_release'],
    [{body: {}}, 'invalid_release'],
    [{body: {...released('v0.2.0'), html_url: 'https://evil.example'}}, 'invalid_release'],
    [{body: {...released('v0.2.0'), prerelease: true}}, 'invalid_release'],
    [{body: {...released('v0.2.0'), draft: true}}, 'invalid_release'],
    [{body: released('v0.2.0-rc.1')}, 'invalid_release'],
    [{rawBody: Buffer.alloc(MAX_RESPONSE_BYTES + 1)}, 'response_too_large'],
  ];
  for (const [options, code] of cases) {
    const native = transport(options);
    const checker = new UpdateChecker({currentVersion: '0.1.0', request: native.request});
    const state = await checker.check();
    assert.equal(state.status, 'error', code);
    assert.equal(state.errorCode, code);
    assert.equal(state.releaseUrl, null);
    assert.equal(checker.getDownloadUrl(), null);
    assert.equal(native.calls.length, 1);
  }
});

test('total deadline ends a silent request and closing cancels without waiting', async () => {
  const native = transport({silent: true});
  const checker = new UpdateChecker({currentVersion: '0.1.0', request: native.request, timeoutMs: 5});
  const [state] = await Promise.all([checker.check(), new Promise(resolve => setTimeout(resolve, 20))]);
  assert.equal(state.errorCode, 'timeout');
  assert.equal(native.calls[0].outgoing.destroyed, true);
  const closing = transport({silent: true});
  const cancelChecker = new UpdateChecker({currentVersion: '0.1.0', request: closing.request});
  const pending = cancelChecker.check();
  await new Promise(resolve => setImmediate(resolve));
  cancelChecker.dispose();
  await pending;
  assert.equal(closing.calls[0].outgoing.destroyed, true);
  await cancelChecker.check();
  assert.equal(closing.calls.length, 1);
  const immediate = transport();
  const immediatelyClosed = new UpdateChecker({currentVersion: '0.1.0', request: immediate.request});
  const immediatePending = immediatelyClosed.check();
  immediatelyClosed.dispose();
  await immediatePending;
  assert.equal(immediate.calls.length, 0);
});

async function loadNativeMain(t) {
  const desktop = path.resolve(__dirname, '../../desktop');
  const pageUrl = pathToFileURL(path.resolve(desktop, '../src/modport/desktop_static/index.html')).href;
  const handles = new Map(), opened = [], notifications = [], errors = [];
  const native = transport({body: released('v1.0.1')});
  let checker, window;
  const app = new EventEmitter();
  Object.assign(app, {getPath: () => '/tmp/modport-update-ipc-test', getPreferredSystemLanguages: () => ['en'],
    requestSingleInstanceLock: () => true, whenReady: () => Promise.resolve(), quit() {}, exit() {}});
  class Window extends EventEmitter {
    constructor() {
      super(); window = this;
      this.webContents = new EventEmitter();
      Object.assign(this.webContents, {mainFrame: {url: pageUrl}, setWindowOpenHandler() {},
        isDestroyed: () => false, send: (...args) => notifications.push(args)});
    }
    isDestroyed() {return false;}
    hide() {}
    loadFile() {return Promise.resolve();}
  }
  const service = new EventEmitter();
  service.stdout = new PassThrough(); service.stderr = new PassThrough();
  service.stdin = {write() {setImmediate(() => service.stdout.write('{"port":23456}\n'));}, end() {}};
  service.kill = () => {};
  t.after(() => {checker?.dispose(); service.stdout.destroy(); service.stderr.destroy();});
  vm.runInNewContext(fs.readFileSync(path.join(desktop, 'main.cjs'), 'utf8'), {
    __dirname: desktop, process: {env: {}, platform: process.platform}, Buffer, setTimeout, clearTimeout, console,
    require(name) {
      if (name === 'electron') return {app, BrowserWindow: Window,
        session: {defaultSession: {setPermissionRequestHandler() {}, setPermissionCheckHandler() {}}},
        ipcMain: {handle: (channel, handler) => handles.set(channel, handler)},
        shell: {openExternal: async url => opened.push(url)}, dialog: {showErrorBox: (...args) => errors.push(args)}};
      if (name === 'node:child_process') return {spawn: () => service};
      if (name === './i18n.cjs') return {LanguageSettings: class {constructor() {this.language = 'en';}}, translate: (_locale, key) => key};
      if (name === path.join(desktop, 'updates.cjs')) return {UpdateChecker: class extends UpdateChecker {
        constructor(options) {super({...options, request: native.request}); checker = this;}
      }};
      return require(name);
    },
  }, {filename: path.join(desktop, 'main.cjs')});
  for (let count = 0; count < 20 && !handles.has('modport:get-update-status'); count++) await new Promise(resolve => setImmediate(resolve));
  assert.ok(handles.has('modport:get-update-status'), JSON.stringify(errors));
  return {handles, opened, notifications, native, checker, app,
    event: {sender: window.webContents, senderFrame: window.webContents.mainFrame}};
}

test('real native startup, trusted IPC and preload keep release opening under host control', async t => {
  const {handles, opened, notifications, native, checker, app, event} = await loadNativeMain(t);
  const get = handles.get('modport:get-update-status'), check = handles.get('modport:check-for-updates');
  assert.equal(get(event).currentVersion, require('../../desktop/package.json').version);
  await check(event);
  assert.equal(native.calls.length, 1, 'startup and simultaneous manual check share a request');
  assert.equal(get(event).status, 'available');
  assert.deepEqual(notifications.map(call => call[1].status), ['checking', 'available']);
  assert.throws(() => get({...event, sender: {}}), /untrustedFrame/);
  assert.throws(() => check({...event, senderFrame: {url: event.senderFrame.url}}), /untrustedFrame/);
  const open = handles.get('modport:open-update-download');
  await assert.rejects(open({...event, sender: {}}), /untrustedFrame/);
  await open(event, 'https://evil.example');
  assert.deepEqual(opened, [released('v1.0.1').html_url]);
  app.emit('before-quit', {preventDefault() {}});
  assert.equal(checker.disposed, true);

  let api;
  const calls = [], ipc = new EventEmitter();
  ipc.invoke = (...args) => {calls.push(args); return Promise.resolve();};
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../../desktop/preload.cjs'), 'utf8'), {
    require: () => ({contextBridge: {exposeInMainWorld: (_name, value) => {api = value;}}, ipcRenderer: ipc}),
  });
  api.getUpdateStatus(); api.checkForUpdates(); api.openUpdateDownload('https://evil.example');
  assert.deepEqual(calls, [['modport:get-update-status'], ['modport:check-for-updates'], ['modport:open-update-download']]);
  const statuses = [];
  const unsubscribe = api.onUpdateStatus(state => statuses.push(state));
  ipc.emit('modport:update-status', {sender: 'internal'}, {status: 'available'});
  unsubscribe();
  ipc.emit('modport:update-status', {}, {status: 'current'});
  assert.deepEqual(statuses, [{status: 'available'}]);
  assert.throws(() => api.onUpdateStatus(null), /callback/);
});
