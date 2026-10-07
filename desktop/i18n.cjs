'use strict';
const fs = require('node:fs');
const path = require('node:path');

const messages = Object.freeze({
  unsupportedRequest: ['Unsupported application request', '不支持的应用请求'],
  requestTooLarge: ['Request is too large', '请求过大'],
  responseTooLarge: ['Application response is too large', '应用响应过大'],
  requestFailed: ['Request failed ({status})', '请求失败（{status}）'],
  requestTimeout: ['Application request timed out', '应用请求超时'],
  serviceNotStarted: ['Application service did not start.\n{detail}', '应用服务未能启动。\n{detail}'],
  serviceExited: ['Application service exited ({code}).\n{detail}', '应用服务已退出（{code}）。\n{detail}'],
  untrustedFrame: ['Untrusted application frame', '应用页面来源不受信任'],
  chooseSource: ['Select a local Mod source folder', '选择本地 Mod 源码文件夹'],
  invalidInstance: ['Invalid migration instance ID', '迁移实例标识无效'],
  absoluteWorkspace: ['The migration instance did not provide an absolute workspace path', '迁移实例未提供绝对工作目录路径'],
  unavailableWorkspace: ['The migration workspace is unavailable', '迁移工作目录不可用'],
  directoryWorkspace: ['The migration workspace path is not a directory', '迁移工作路径不是目录'],
  openWorkspace: ['Could not open the migration workspace: {detail}', '无法打开迁移工作目录：{detail}'],
  startupTitle: ['ModPort could not start', 'ModPort 无法启动'],
  invalidLanguage: ['Unsupported interface language', '不支持的界面语言'],
  invalidSettings: ['Interface settings must be a regular file', '界面设置必须是普通文件'],
});

function supportedLanguage(value) {
  if (typeof value !== 'string') return null;
  const locale = value.trim();
  if (/^zh(?:[-_.@]|$)/i.test(locale)) return 'zh-CN';
  if (/^en(?:[-_.@]|$)/i.test(locale)) return 'en';
  return null;
}

function normalizeLanguage(value) {
  return supportedLanguage(value) || 'en';
}

function preferredSystemLanguage(values) {
  if (!Array.isArray(values)) return 'en';
  for (const value of values) {
    const language = supportedLanguage(value);
    if (language) return language;
  }
  return 'en';
}

function translate(language, key, values = {}) {
  const pair = messages[key];
  if (!pair) throw new Error(`Unknown interface message: ${key}`);
  return pair[language === 'zh-CN' ? 1 : 0].replace(/\{([a-z]+)\}/g, (_, name) => String(values[name] ?? `{${name}}`));
}

class LanguageSettings {
  constructor(directory, preferredLanguages) {
    this.directory = directory;
    this.path = path.join(directory, 'ui-settings.json');
    this.language = preferredSystemLanguage(preferredLanguages);
    try {
      const stat = fs.lstatSync(this.path);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 4096) return;
      const saved = JSON.parse(fs.readFileSync(this.path, 'utf8'));
      if (saved && typeof saved === 'object' && !Array.isArray(saved) &&
          (saved.language === 'en' || saved.language === 'zh-CN')) this.language = saved.language;
    } catch (error) {
      if (error.code !== 'ENOENT' && !(error instanceof SyntaxError)) throw error;
    }
  }

  set(language) {
    if (language !== 'en' && language !== 'zh-CN') throw new Error(translate(this.language, 'invalidLanguage'));
    fs.mkdirSync(this.directory, {recursive: true, mode: 0o700});
    try {
      const stat = fs.lstatSync(this.path);
      if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(translate(this.language, 'invalidSettings'));
    } catch (error) {if (error.code !== 'ENOENT') throw error;}
    const temporary = path.join(this.directory, `.ui-settings-${process.pid}-${Date.now()}.tmp`);
    try {
      fs.writeFileSync(temporary, JSON.stringify({language}) + '\n', {flag: 'wx', mode: 0o600});
      fs.renameSync(temporary, this.path);
    } finally {
      try {fs.unlinkSync(temporary);} catch (error) {if (error.code !== 'ENOENT') throw error;}
    }
    this.language = language;
    return language;
  }
}

module.exports = {normalizeLanguage, preferredSystemLanguage, translate, LanguageSettings};
