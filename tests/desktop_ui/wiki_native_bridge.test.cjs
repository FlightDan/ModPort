/* Execute the production native request bridge without starting Electron. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {EventEmitter} = require('node:events');

test('native Wiki routes forward canonical draft UUIDs and bounded edited JSON', async () => {
    const source = fs.readFileSync(path.resolve(__dirname, '../../desktop/main.cjs'), 'utf8');
    const bridge = source.slice(source.indexOf('function validRequest('), source.indexOf('async function startService('));
    const forwarded = [];
    const http = {request(options, callback) {
        const request = new EventEmitter();
        request.setTimeout = () => {};
        request.destroy = error => request.emit('error', error);
        request.end = body => {
            forwarded.push({options, body});
            const response = new EventEmitter(); response.statusCode = 200;
            callback(response); response.emit('data', Buffer.from('{"status":"saved"}')); response.emit('end');
        };
        return request;
    }};
    const context = {http, Buffer, port: 1234, token: 'host-owned-test-credential',
        languageSettings: {language: 'en'}, t: value => value};
    vm.createContext(context);
    vm.runInContext(bridge, context);
    const uuid = 'e59148c0-74b8-49da-98f0-437112b8e679';
    const route = '/api/wiki/contributions/' + uuid;
    await context.apiRequest({method: 'GET', path: route});
    await context.apiRequest({method: 'POST', path: route, body: {content: 'x'.repeat(70000)}});
    await context.apiRequest({method: 'POST', path: route + '/submit', body: {expected_login: 'contributor'}});
    await context.apiRequest({method: 'POST', path: '/api/wiki/update', body: {}});
    assert.equal(forwarded[0].options.path, route);
    assert.equal(forwarded[0].options.headers.Authorization, 'Bearer host-owned-test-credential');
    assert.equal(JSON.parse(forwarded[1].body).content.length, 70000);
    for (const invalid of [route + '?token=value', '/api/wiki/contributions/../private', '/api/local-source']) {
        await assert.rejects(context.apiRequest({method: 'GET', path: invalid}), /unsupportedRequest/);
    }
    await assert.rejects(context.apiRequest({method: 'POST', path: '/api/setup', body: {value: 'x'.repeat(70000)}}), /requestTooLarge/);
    await assert.rejects(context.apiRequest({method: 'POST', path: route, body: {content: 'x'.repeat(2 * 1024 * 1024)}}), /requestTooLarge/);
    assert.equal(forwarded.length, 4);
});
