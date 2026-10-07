const test = require('node:test');
const assert = require('node:assert/strict');
require('../../src/modport/desktop_static/i18n.js').setLocale('zh-CN');
const {partitionItems, selectionFor, changeSelection, number, duration} = require('../../src/modport/desktop_static/app.js');

test('unstarted work is hidden; attention stays visible and completed work collapses', () => {
    const items = [
        {id: 'pending', state: 'pending'}, {id: 'queued', state: 'queued'},
        {id: 'failed', state: 'failed'}, {id: 'wait', state: 'waiting'},
        {id: 'done', state: 'completed'},
        ...Array.from({length: 6}, (_, i) => ({id: `running-${i}`, state: 'running', active_agents: i + 1})),
    ];
    const result = partitionItems(items);
    assert.deepEqual(result.attention.map(item => item.id), ['failed', 'wait']);
    assert.equal(result.active.length, 4);
    assert.equal(result.overflow.length, 2);
    assert.deepEqual(result.completed.map(item => item.id), ['done']);
    assert(!Object.values(result).flat().some(item => ['pending', 'queued'].includes(item.id)));
});

test('role and stage edits preserve independent defaults, stages, and fallbacks', () => {
    const config = {default: {model: 'default', reasoning_effort: 'max'}, roles: {coder: {model: 'author', reasoning_effort: 'high', fallback: {model: 'provider/fallback', reasoning_effort: 'low'}}}, stages: {untouched: {model: 'stage', reasoning_effort: 'medium'}}};
    const originalDefault = JSON.stringify(config.default);
    const originalStages = JSON.stringify(config.stages);
    changeSelection(config, {id: 'coder', target: 'role'}, 'reasoning_effort', 'medium');
    assert.equal(JSON.stringify(config.default), originalDefault);
    assert.equal(JSON.stringify(config.stages), originalStages);
    assert.equal(config.roles.coder.model, 'author');
    assert.equal(config.roles.coder.fallback.model, 'provider/fallback');
    const stage = {id: 'stage:agent_rework', target: 'stage', stage: 'agent_rework', role: 'coder'};
    assert.equal(selectionFor(config, stage).model, 'author');
    changeSelection(config, stage, 'model', 'new-model');
    assert.equal(config.roles.coder.model, 'author');
    assert.equal(config.stages.agent_rework.fallback.model, 'provider/fallback');
    assert.equal(config.stages.untouched.model, 'stage');
});

test('unknown metrics remain unknown and zero usage remains zero', () => {
    for (const value of [null, undefined, NaN, -1, '0']) {assert.equal(number(value), '未知'); assert.equal(duration(value), '未知');}
    assert.equal(number(0), '0');
    assert.equal(duration(3661), '01:01:01');
});

test('subagent follows current coder settings until explicitly overridden', () => {
    const config = {default: {model: 'ordinary', reasoning_effort: 'low'}, roles: {coder: {model: 'coder-a', reasoning_effort: 'high'}}, stages: {}};
    const row = {id: 'subagent', target: 'role', selection: {model: 'stale-bootstrap', reasoning_effort: 'max'}};
    assert.equal(selectionFor(config, row).model, 'coder-a');
    config.roles.coder.model = 'coder-b';
    assert.equal(selectionFor(config, row).model, 'coder-b');
    changeSelection(config, row, 'model', 'child-model');
    assert.equal(selectionFor(config, row).model, 'child-model');
    assert.equal(config.roles.subagent.reasoning_effort, 'high');
    delete config.roles.subagent;
    assert.equal(selectionFor(config, row).model, 'coder-b');
});
