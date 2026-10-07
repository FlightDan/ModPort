'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const {pathToFileURL} = require('node:url');
const {LanguageSettings, normalizeLanguage, preferredSystemLanguage, translate} = require('../../desktop/i18n.cjs');

function directory(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'modport-language-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  return root;
}

test('native locale follows OS once, unsupported falls back and explicit setting survives reopening', t => {
  const root = directory(t);
  assert.equal(normalizeLanguage('zh-Hant-TW'), 'zh-CN');
  assert.equal(normalizeLanguage('fr-FR'), 'en');
  assert.equal(preferredSystemLanguage(['fr-FR', 'zh-Hans-CN', 'en-US']), 'zh-CN');
  assert.equal(preferredSystemLanguage(['fr-FR', 'en-US', 'zh-CN']), 'en');
  assert.equal(preferredSystemLanguage(['fr-FR', 'ja-JP']), 'en');
  const settings = new LanguageSettings(root, ['zh-HK']);
  assert.equal(settings.language, 'zh-CN');
  assert.equal(fs.existsSync(settings.path), false);
  assert.equal(settings.set('en'), 'en');
  assert.equal(new LanguageSettings(root, ['zh-CN']).language, 'en');
  assert.equal(settings.set('zh-CN'), 'zh-CN');
  assert.equal(new LanguageSettings(root, ['en']).language, 'zh-CN');
  assert.throws(() => settings.set('zh-HK'), /不支持/);
  assert.equal(JSON.parse(fs.readFileSync(settings.path)).language, 'zh-CN');
  assert.equal(fs.existsSync(path.join(root, 'instances')), false);
  assert.equal(translate('en', 'openWorkspace', {detail: '原文 {literal}'}), 'Could not open the migration workspace: 原文 {literal}');
});

test('native saved selection does not follow a symbolic link', t => {
  const root = directory(t);
  const outside = path.join(root, 'outside.json');
  fs.writeFileSync(outside, '{"language":"zh-CN"}');
  try {fs.symlinkSync(outside, path.join(root, 'ui-settings.json'));}
  catch (error) {if (error.code === 'EPERM') return t.skip('Host cannot create symbolic links'); throw error;}
  const settings = new LanguageSettings(root, ['en']);
  assert.equal(settings.language, 'en');
  assert.throws(() => settings.set('zh-CN'), /regular file/);
  assert.equal(fs.readFileSync(outside, 'utf8'), '{"language":"zh-CN"}');
});

test('valid JSON with an invalid settings shape falls back to OS language', t => {
  const root = directory(t);
  for (const saved of [null, [], 'en', 1, {language: 'fr-FR'}]) {
    fs.writeFileSync(path.join(root, 'ui-settings.json'), JSON.stringify(saved));
    assert.equal(new LanguageSettings(root, ['zh-CN']).language, 'zh-CN');
  }
});

async function loadMain(t, root, {applicationLocale = 'en-US', preferredLanguages = ['zh-HK']} = {}) {
  const handles = new Map(), headers = [], dialogs = [];
  const desktop = path.resolve(__dirname, '../../desktop');
  const pageUrl = pathToFileURL(path.resolve(desktop, '../src/modport/desktop_static/index.html')).href;
  const app = new EventEmitter();
  Object.assign(app, {getLocale: () => applicationLocale,
    getPreferredSystemLanguages: () => preferredLanguages, getPath: () => root,
    requestSingleInstanceLock: () => true, quit() {}, exit() {}, whenReady: () => Promise.resolve()});
  let window;
  class Window extends EventEmitter {
    constructor() {
      super(); window = this;
      this.webContents = new EventEmitter();
      this.webContents.mainFrame = {url: pageUrl};
      this.webContents.setWindowOpenHandler = () => {};
    }
    loadFile() {return Promise.resolve();}
  }
  const session = {defaultSession: {setPermissionRequestHandler() {}, setPermissionCheckHandler() {}}};
  const electron = {app, BrowserWindow: Window, session,
    ipcMain: {handle: (name, handler) => handles.set(name, handler)},
    shell: {openPath: async () => ''},
    dialog: {showErrorBox: (...args) => dialogs.push(args), showOpenDialog: async (_window, options) => {dialogs.push(options); return {canceled: true};}}};
  const service = new EventEmitter();
  service.stdout = new PassThrough(); service.stderr = new PassThrough();
  service.stdin = {write() {setImmediate(() => service.stdout.write('{"port":23456}\n'));}, end() {}};
  service.kill = () => {};
  t.after(() => {service.stdout.destroy(); service.stderr.destroy();});
  const http = {request(options, callback) {
    headers.push(options.headers);
    const request = new EventEmitter(); request.setTimeout = () => {};
    request.end = () => {
      const response = new EventEmitter(); response.statusCode = 200;
      callback(response);
      response.emit('data', Buffer.from('{"workflow_version":37}')); response.emit('end');
    };
    return request;
  }};
  vm.runInNewContext(fs.readFileSync(path.join(desktop, 'main.cjs'), 'utf8'), {
    __dirname: desktop, process: {env: {}, platform: process.platform},
    setTimeout, clearTimeout, console,
    require(name) {
      if (name === 'electron') return electron;
      if (name === 'node:child_process') return {spawn: () => service};
      if (name === 'node:http') return http;
      if (name === path.join(desktop, 'updates.cjs')) return {UpdateChecker: class {
        subscribe() {} getStatus() {return {status: 'idle'};}
        check() {return Promise.resolve(this.getStatus());} dispose() {}
      }};
      if (name === './i18n.cjs') return {LanguageSettings, translate};
      return require(name);
    }, Buffer,
  }, {filename: path.join(desktop, 'main.cjs')});
  for (let count = 0; count < 20 && !handles.has('modport:get-language'); count++) await new Promise(resolve => setImmediate(resolve));
  assert.ok(handles.has('modport:get-language'), JSON.stringify(dialogs));
  return {handles, headers, dialogs, event: {sender: window.webContents, senderFrame: window.webContents.mainFrame}};
}

test('production startup uses preferred system languages, falls back to English and keeps saved choice', async t => {
  const chineseRoot = directory(t);
  const chinese = await loadMain(t, chineseRoot, {
    applicationLocale: 'en-US', preferredLanguages: ['fr-FR', 'zh-Hans-CN', 'en-US'],
  });
  assert.equal(chinese.handles.get('modport:get-language')(chinese.event), 'zh-CN');
  assert.equal(chinese.handles.get('modport:set-language')(chinese.event, 'en'), 'en');

  const reopened = await loadMain(t, chineseRoot, {
    applicationLocale: 'zh-CN', preferredLanguages: ['zh-HK'],
  });
  assert.equal(reopened.handles.get('modport:get-language')(reopened.event), 'en');

  const englishRoot = directory(t);
  const english = await loadMain(t, englishRoot, {
    applicationLocale: 'zh-CN', preferredLanguages: ['fr-FR', 'en-US', 'zh-Hans-CN'],
  });
  assert.equal(english.handles.get('modport:get-language')(english.event), 'en');

  const unsupportedRoot = directory(t);
  const unsupported = await loadMain(t, unsupportedRoot, {
    applicationLocale: 'zh-CN', preferredLanguages: ['fr-FR', 'ja-JP'],
  });
  assert.equal(unsupported.handles.get('modport:get-language')(unsupported.event), 'en');
});

test('production native IPC persists language, rejects foreign frames and attaches current Accept-Language', async t => {
  const root = directory(t);
  const {handles, headers, dialogs, event} = await loadMain(t, root);
  assert.equal(handles.get('modport:get-language')(event), 'zh-CN');
  assert.equal(handles.get('modport:set-language')(event, 'en'), 'en');
  const untrusted = {...event, senderFrame: {url: event.senderFrame.url}};
  assert.throws(() => handles.get('modport:set-language')(untrusted, 'zh-CN'), /Untrusted/);
  assert.throws(() => handles.get('modport:get-language')({...event, sender: {}}), /Untrusted/);
  await handles.get('modport:request')(event, {method: 'GET', path: '/api/bootstrap'});
  assert.equal(headers.at(-1)['Accept-Language'], 'en');
  assert.match(headers.at(-1).Authorization, /^Bearer [a-f0-9]{64}$/);
  handles.get('modport:set-language')(event, 'zh-CN');
  await handles.get('modport:request')(event, {method: 'GET', path: '/api/bootstrap'});
  assert.equal(headers.at(-1)['Accept-Language'], 'zh-CN');
  assert.equal((await handles.get('modport:select-source-directory')(event)).cancelled, true);
  assert.equal(dialogs.at(-1).title, '选择本地 Mod 源码文件夹');
  assert.equal(new LanguageSettings(root, ['en']).language, 'zh-CN');
  await assert.rejects(handles.get('modport:request')(event, {method: 'POST', path: '/api/local-source', body: {path: '/tmp'}}), /不支持/);
});

test('production preload exposes only explicit language operations', () => {
  let api; const calls = [];
  vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../../desktop/preload.cjs'), 'utf8'), {
    require: () => ({contextBridge: {exposeInMainWorld: (_name, value) => {api = value;}},
      ipcRenderer: {invoke: (...args) => {calls.push(args); return Promise.resolve('en');}}}),
  });
  assert.ok(Object.isFrozen(api));
  api.getLanguage(); api.setLanguage('zh-CN');
  assert.deepEqual(calls, [['modport:get-language'], ['modport:set-language', 'zh-CN']]);
  assert.equal(typeof api.openWorkspace, 'function');
});
