/* Authored UI messages only. User content, identifiers and service logs are data. */
(function (root) {
    'use strict';
    const english = typeof module === 'object' && module.exports ? require('./en.js') : root.ModPortEnglish;
    const preferenceKey = 'modport.language';
    const bindings = new Map();
    const normalize = value => /^zh(?:[-_.@]|$)/i.test(String(value || '').trim()) ? 'zh-CN' : 'en';
    let locale = normalize(typeof navigator === 'object' ? navigator.language : 'en');
    try { locale = normalize(root.localStorage?.getItem(preferenceKey) || locale); } catch { /* Storage may be disabled. */ }
    function t(message, ...values) {
        if (message === undefined || message === null) return '';
        const source = String(message);
        const translated = locale === 'zh-CN' ? source : Object.hasOwn(english, source) ? english[source] : source;
        return translated.replace(/\{(\d+)\}/g, (match, index) => index < values.length ? String(values[index]) : match);
    }
    function setLocale(value, persist = false) {
        locale = normalize(value);
        if (persist) {
            try { root.localStorage?.setItem(preferenceKey, locale); } catch { /* The selection still works for this session. */ }
        }
        return locale;
    }
    function apply(document) {
        document.documentElement.lang = locale;
        for (const node of document.querySelectorAll('[data-i18n]')) node.textContent = t(node.dataset.i18n);
        for (const attribute of ['aria-label', 'placeholder', 'title']) {
            for (const node of document.querySelectorAll(`[data-i18n-${attribute}]`)) node.setAttribute(attribute, t(node.getAttribute(`data-i18n-${attribute}`)));
        }
        for (const [node, render] of bindings) {
            if (node.isConnected) node.textContent = render();
            else bindings.delete(node);
        }
    }
    function setText(node, value) {
        bindings.delete(node);
        node.textContent = value;
    }
    function bindText(node, render) {
        bindings.set(node, render);
        node.textContent = render();
    }
    const api = {normalize, t, setLocale, apply, setText, bindText, get locale() {return locale;}};
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.ModPortI18n = api;
}(globalThis));
