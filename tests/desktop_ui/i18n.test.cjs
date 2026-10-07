const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const i18n = require('../../src/modport/desktop_static/i18n.js');
const english = require('../../src/modport/desktop_static/en.js');

test('language aliases, unsupported fallback and interpolation preserve raw values', () => {
    for (const locale of ['zh', 'zh_CN.UTF-8', 'zh-Hans', 'zh-TW']) assert.equal(i18n.normalize(locale), 'zh-CN');
    for (const locale of ['en-US', 'fr-FR', 'C', undefined]) assert.equal(i18n.normalize(locale), 'en');
    i18n.setLocale('en');
    assert.equal(i18n.t('实例目录：{0}', '/项目/$&/{1}'), 'Instance folder: /项目/$&/{1}');
    assert.equal(i18n.t('unknown raw diagnostic'), 'unknown raw diagnostic');
    i18n.setLocale('zh-CN');
    assert.equal(i18n.t('实例目录：{0}', '/项目'), '实例目录：/项目');
});

test('every marked renderer message has an English translation with matching placeholders', () => {
    const root = path.resolve(__dirname, '../../src/modport/desktop_static');
    const html = fs.readFileSync(path.join(root, 'index.html'), 'utf8');
    const app = fs.readFileSync(path.join(root, 'app.js'), 'utf8');
    const messages = [...html.matchAll(/data-i18n(?:-aria-label|-placeholder|-title)?="([^"]*)"/g)].map(m => m[1].replaceAll('&quot;', '"').replaceAll('&amp;', '&'));
    messages.push(...[...app.matchAll(/\bt\((['"])([^'"\n]*[\u4e00-\u9fff][^'"\n]*)\1/g)].map(m => m[2]));
    for (const message of messages) assert(Object.hasOwn(english, message), `Missing English message: ${message}`);
    for (const [key,value] of Object.entries(english)) {
        const placeholders = text => [...text.matchAll(/\{\d+\}/g)].map(m => m[0]).sort();
        assert.deepEqual(placeholders(value), placeholders(key), `Placeholder mismatch: ${key}`);
    }
});
