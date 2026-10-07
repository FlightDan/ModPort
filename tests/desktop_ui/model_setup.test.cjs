const test = require('node:test');
const assert = require('node:assert/strict');
const {create, apply} = require('../../src/modport/desktop_static/model_setup.js');

const model = (id, efforts = ['high', 'low']) => ({id, context_window: 64000, max_output_tokens: 4000, reasoning_efforts: efforts});
const provider = (id, models) => ({id, name: id, api_type: 'openai', base_url: 'https://example.invalid/v1', models, saved: true, api_key_configured: true});
function fixture() {
    return {providers: [provider('openai', [model('difficult'), model('routine'), model('keep')])],
        policy: {default: {model: 'routine', reasoning_effort: 'low'}, roles: {
            planner: {model: 'difficult', reasoning_effort: 'high'}, coder: {model: 'routine', reasoning_effort: 'low'}}, stages: {}}};
}

test('shared API maps two models to roles and keeps other catalog entries', () => {
    const {providers, policy} = fixture();
    const draft = create(providers, policy);
    assert.equal(draft.primary.id, 'openai');
    assert.equal(draft.separateConnection, false);
    assert.equal(draft.sameModel, false);
    const saved = apply(draft, providers, policy);
    for (const role of ['planner', 'contract_review']) assert.deepEqual(saved.model_config.roles[role], {model: 'openai/difficult', reasoning_effort: 'high'});
    for (const role of ['coder', 'supervisor', 'summary']) assert.deepEqual(saved.model_config.roles[role], {model: 'openai/routine', reasoning_effort: 'low'});
    assert.deepEqual(saved.model_config.default, saved.model_config.roles.coder);
    assert.equal(saved.providers.length, 1);
    assert.deepEqual(saved.providers[0].models.map(item => item.id), ['difficult', 'routine', 'keep']);
    assert(!('saved' in saved.providers[0]));
    assert(!('api_key_configured' in saved.providers[0]));
});

test('second API starts blank without copying credentials and remains independent', () => {
    const {providers, policy} = fixture();
    providers[0].api_key = 'private-first-key';
    providers.push(provider('custom1', [model('other')]));
    const draft = create(providers, policy);
    assert.equal(draft.secondary.id, 'custom2');
    assert.equal(draft.secondary.api_key, '');
    assert.equal(draft.secondary.base_url, '');
    assert.equal(draft.secondary.models.length, 0);
    draft.separateConnection = true;
    draft.secondary.base_url = 'https://second.invalid/v1';
    draft.secondary.api_key = 'private-second-key';
    const saved = apply(draft, providers, policy);
    assert.equal(saved.model_config.roles.coder.model, 'custom2/routine');
    assert.equal(saved.model_config.roles.planner.model, 'openai/difficult');
    assert.equal(saved.providers.find(item => item.id === 'openai').api_key, 'private-first-key');
    assert.equal(saved.providers.find(item => item.id === 'custom2').api_key, 'private-second-key');
});

test('existing separate connection and effective stage selections initialize the draft', () => {
    const {providers, policy} = fixture();
    providers.push(provider('custom1', [model('other')]));
    policy.stages.coder = {model: 'custom1/other', reasoning_effort: 'high'};
    const draft = create(providers, policy);
    assert.equal(draft.separateConnection, true);
    assert.equal(draft.secondary.id, 'custom1');
    assert.equal(draft.routine.model.id, 'other');
    assert.equal(draft.routine.reasoning_effort, 'high');
});

test('same model uses difficult connection and details once despite a blank second API', () => {
    const {providers, policy} = fixture();
    const draft = create(providers, policy);
    draft.sameModel = true;
    draft.separateConnection = true;
    draft.routine.model.context_window = -1;
    draft.difficult.model.context_window = 128000;
    const saved = apply(draft, providers, policy);
    assert.equal(saved.providers.length, 1);
    assert.equal(saved.model_config.roles.coder.model, 'openai/difficult');
    assert.equal(saved.model_config.roles.coder.reasoning_effort, 'high');
    assert.equal(saved.providers[0].models.filter(item => item.id === 'difficult').length, 1);
    assert.equal(saved.providers[0].models.find(item => item.id === 'difficult').context_window, 128000);
});

test('applying setup preserves explicit overrides, fallbacks, and every input', () => {
    const {providers, policy} = fixture();
    const fallback = {model: 'openai/keep', reasoning_effort: 'low'};
    policy.default.fallback = fallback;
    policy.roles.planner.fallback = fallback;
    policy.roles.coder.fallback = fallback;
    policy.roles.subagent = {model: 'openai/keep', reasoning_effort: 'high'};
    policy.stages.code_review = {model: 'openai/keep', reasoning_effort: 'low'};
    const initial = JSON.stringify({providers, policy});
    const draft = create(providers, policy);
    draft.difficult.model.id = 'new-difficult';
    const before = JSON.stringify(draft);
    const saved = apply(draft, providers, policy);
    assert.equal(JSON.stringify(draft), before);
    assert.equal(JSON.stringify({providers, policy}), initial);
    assert.deepEqual(saved.model_config.stages, policy.stages);
    assert.deepEqual(saved.model_config.roles.subagent, policy.roles.subagent);
    assert.deepEqual(saved.model_config.roles.planner.fallback, fallback);
    assert.deepEqual(saved.model_config.roles.coder.fallback, fallback);
    assert.deepEqual(saved.model_config.default.fallback, fallback);
    saved.providers[0].models[0].id = 'mutated-result';
    assert.equal(providers[0].models[0].id, 'difficult');
});

test('invalid model fields fail and an empty effort list supports none', () => {
    const {providers, policy} = fixture();
    const changes = [
        [draft => draft.difficult.model.id = '', /model name/],
        [draft => draft.difficult.model.context_window = 0, /context window/],
        [draft => draft.routine.model.max_output_tokens = 64001, /maximum output/],
        [draft => draft.routine.reasoning_effort = 'max', /not supported/],
        [draft => draft.primary.name = '', /connection name/],
    ];
    for (const [change, message] of changes) {
        const draft = create(providers, policy);
        change(draft);
        assert.throws(() => apply(draft, providers, policy), message);
    }
    const draft = create(providers, policy);
    draft.routine.model.reasoning_efforts = [];
    draft.routine.reasoning_effort = 'none';
    assert.equal(apply(draft, providers, policy).model_config.roles.coder.reasoning_effort, 'none');
});

test('same catalog model can use distinct efforts but cannot silently replace conflicting details', () => {
    const {providers, policy} = fixture();
    policy.roles.coder.model = 'difficult';
    const draft = create(providers, policy);
    assert.equal(draft.sameModel, false);
    const saved = apply(draft, providers, policy);
    assert.equal(saved.model_config.roles.planner.reasoning_effort, 'high');
    assert.equal(saved.model_config.roles.coder.reasoning_effort, 'low');
    assert.equal(saved.providers[0].models.filter(item => item.id === 'difficult').length, 1);
    draft.difficult.model.context_window = 128000;
    assert.throws(() => apply(draft, providers, policy), /matching details/);
});

// The advanced editor must remain reachable while a guided field is incomplete.
test('advanced projection transfers incomplete draft fields without allowing normal save', () => {
    const {providers, policy} = fixture();
    const draft = create(providers, policy);
    draft.primary.base_url = 'https://edited.example/v1';
    draft.difficult.model.context_window = '';
    assert.throws(() => apply(draft, providers, policy), /context window/);
    const projected = apply(draft, providers, policy, {validate: false});
    assert.equal(projected.providers[0].base_url, 'https://edited.example/v1');
    assert.equal(projected.providers[0].models.find(model => model.id === 'difficult').context_window, '');
});

test('advanced projection preserves edits to a shared model with different efforts', () => {
    const {providers, policy} = fixture();
    policy.roles.coder = {model: 'difficult', reasoning_effort: 'low'};
    for (const slot of ['difficult', 'routine']) {
        const draft = create(providers, policy);
        assert.equal(draft.sameModel, false);
        draft[slot].model.context_window = 128000;
        const projected = apply(draft, providers, policy, {validate: false});
        assert.equal(projected.providers[0].models.find(model => model.id === 'difficult').context_window, 128000);
        assert.equal(projected.model_config.roles.coder.reasoning_effort, 'low');
        assert.equal(projected.model_config.roles.planner.reasoning_effort, 'high');
    }
    const conflict = create(providers, policy);
    conflict.difficult.model.context_window = 128000;
    conflict.routine.model.context_window = 256000;
    assert.throws(() => apply(conflict, providers, policy, {validate: false}), /matching details/);
});
