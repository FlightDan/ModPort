const test = require('node:test');
const assert = require('node:assert/strict');
const {create, apply} = require('../../src/modport/desktop_static/fallback_setup.js');

const model = (id, efforts = ['high', 'low'], context = 64000, output = 4000) => ({
    id, context_window: context, max_output_tokens: output, reasoning_efforts: efforts,
});
const provider = (id, models, extra = {}) => ({
    id, name: id, api_type: 'openai-compatible', base_url: `https://${id}.example/v1`,
    models, saved: true, api_key_configured: true, ...extra,
});
function fixture() {
    return {
        providers: [provider('openai', [model('primary'), model('backup'), model('keep')], {api_key: 'primary-secret'})],
        policy: {
            default: {model: 'primary', reasoning_effort: 'high'},
            roles: {
                planner: {model: 'openai/primary', reasoning_effort: 'high'},
                coder: {model: 'primary', reasoning_effort: 'low'},
                subagent: {model: 'openai/backup', reasoning_effort: 'high'},
            },
            stages: {contract_review: {model: 'openai/primary', reasoning_effort: 'high'}},
        },
    };
}
function choice(modelId = 'backup', effort = 'low', efforts = ['high', 'low']) {
    return {model: model(modelId, efforts), reasoning_effort: effort};
}
function newProviderDraft(providers, chosen = choice()) {
    return {...create(providers, {}), mode: 'shared', choice: chosen,
        provider: {...create(providers, {}).provider, name: 'Backup API', base_url: 'https://backup.example/v1'}};
}

test('create starts in preserve mode with a blank, credential-free, unused connection', () => {
    const {providers, policy} = fixture();
    providers.push(provider('custom1', [model('other')], {api_key: 'other-secret'}));
    const draft = create(providers, policy);
    assert.equal(draft.mode, 'preserve');
    assert.equal(draft.providerId, '');
    assert.equal(draft.provider.id, 'custom2');
    assert.equal(draft.provider.api_key, '');
    assert.equal(draft.provider.base_url, '');
    assert.deepEqual(draft.choice, {model: {id: '', context_window: '', max_output_tokens: '', reasoning_efforts: []}, reasoning_effort: 'none'});
});

test('preserve clones inputs and keeps heterogeneous fallbacks while stripping UI metadata', () => {
    const {providers, policy} = fixture();
    policy.default.fallback = {model: 'custom1/a', reasoning_effort: 'low'};
    policy.roles.planner.fallback = {model: 'openai/keep', reasoning_effort: 'high'};
    const original = JSON.stringify({providers, policy});
    const saved = apply(create(providers, policy), providers, policy);
    assert.deepEqual(saved.model_config, policy);
    assert.equal(JSON.stringify({providers, policy}), original);
    assert.notEqual(saved.model_config, policy);
    assert.notEqual(saved.providers[0].models, providers[0].models);
    assert(!('saved' in saved.providers[0]));
    assert(!('api_key_configured' in saved.providers[0]));
});

test('off removes fallbacks from default, every role including subagent, and every stage', () => {
    const {providers, policy} = fixture();
    policy.default.fallback = {model: 'openai/backup', reasoning_effort: 'low'};
    for (const selection of [...Object.values(policy.roles), ...Object.values(policy.stages)]) {
        selection.fallback = {model: 'openai/backup', reasoning_effort: 'low'};
    }
    const saved = apply({...create(providers, policy), mode: 'off'}, providers, policy);
    for (const selection of [saved.model_config.default, ...Object.values(saved.model_config.roles), ...Object.values(saved.model_config.stages)]) {
        assert.equal(Object.hasOwn(selection, 'fallback'), false);
    }
});

test('shared existing-provider fallback replaces all explicit selections and canonicalizes primary comparison', () => {
    const {providers, policy} = fixture();
    policy.default.fallback = {model: 'old/model', reasoning_effort: 'minimal'};
    const draft = {...create(providers, policy), mode: 'shared', providerId: 'openai', choice: choice('backup', 'low')};
    const saved = apply(draft, providers, policy);
    const expected = {model: 'openai/backup', reasoning_effort: 'low'};
    for (const selection of [...Object.values(saved.model_config.roles), ...Object.values(saved.model_config.stages)]) {
        assert.deepEqual(selection.fallback, expected);
    }
    assert.deepEqual(saved.model_config.default.fallback, expected);
    assert.equal(saved.providers[0].models.filter(item => item.id === 'backup').length, 1);
    assert.deepEqual(saved.providers[0].models.find(item => item.id === 'backup'), model('backup'));
});

test('new backup connection rebases a colliding custom ID and receives no other provider credentials', () => {
    const {providers, policy} = fixture();
    const draft = newProviderDraft(providers);
    // Simulate guided primary setup registering custom1 after this draft was created.
    providers.push(provider('custom1', [model('primary')], {api_key: 'guided-primary-secret'}));
    const saved = apply(draft, providers, policy);
    const backup = saved.providers.find(item => item.id === 'custom2');
    assert.ok(backup);
    assert.equal(backup.api_key, '');
    assert.equal(saved.providers.find(item => item.id === 'custom1').api_key, 'guided-primary-secret');
    assert.equal(saved.model_config.default.fallback.model, 'custom2/backup');
});

test('existing-provider selections use final catalog details instead of stale draft specs', () => {
    const {providers, policy} = fixture();
    const stale = choice('backup', 'low', ['invalid']);
    stale.model.context_window = 0;
    stale.model.max_output_tokens = 0;
    const draft = {...create(providers, policy), mode: 'shared', providerId: 'openai', choice: stale};
    const saved = apply(draft, providers, policy);
    assert.deepEqual(saved.providers[0].models.find(item => item.id === 'backup'), model('backup'));
    assert.deepEqual(saved.model_config.default.fallback, {model: 'openai/backup', reasoning_effort: 'low'});
    assert.deepEqual(providers[0].models.find(item => item.id === 'backup'), model('backup'));
    const unsupported = {...draft, choice: {...draft.choice, reasoning_effort: 'ultra'}};
    assert.throws(() => apply(unsupported, providers, policy), /Fallback reasoning effort is not supported by this model\./);
});

test('existing-provider selection fails if the model was removed from the final catalog', () => {
    const {providers, policy} = fixture();
    const draft = {...create(providers, policy), mode: 'shared', providerId: 'openai', choice: choice('backup', 'low')};
    const finalProviders = providers.map(item => ({...item,
        models: item.models.filter(modelItem => modelItem.id !== 'backup')}));
    assert.throws(() => apply(draft, finalProviders, policy),
        /Selected fallback model was not found in the provider catalog\./);
    assert.equal(finalProviders[0].models.some(item => item.id === 'backup'), false);
});

test('new-provider registration rejects a conflicting model already in its draft catalog', () => {
    const {providers, policy} = fixture();
    const draft = newProviderDraft(providers);
    draft.provider.models.push(model('backup', ['high'], 32000, 2000));
    assert.throws(() => apply(draft, providers, policy),
        /Fallback model conflicts with an existing model on this API\./);
});

test('a fallback identical to a selection primary by model and effort is removed for that selection', () => {
    const {providers, policy} = fixture();
    policy.default = {model: 'backup', reasoning_effort: 'low', fallback: {model: 'old/model', reasoning_effort: 'high'}};
    policy.roles.planner = {model: 'openai/backup', reasoning_effort: 'high'};
    const draft = {...create(providers, policy), mode: 'shared', providerId: 'openai', choice: choice('backup', 'low')};
    const saved = apply(draft, providers, policy);
    assert.equal(Object.hasOwn(saved.model_config.default, 'fallback'), false);
    assert.deepEqual(saved.model_config.roles.planner.fallback, {model: 'openai/backup', reasoning_effort: 'low'});
});

test('shared mode leaves all caller inputs untouched', () => {
    const {providers, policy} = fixture();
    const draft = newProviderDraft(providers);
    const before = JSON.stringify({providers, policy, draft});
    const saved = apply(draft, providers, policy);
    assert.equal(JSON.stringify({providers, policy, draft}), before);
    saved.providers[0].models[0].id = 'changed';
    saved.model_config.roles.planner.model = 'changed';
    assert.equal(providers[0].models[0].id, 'primary');
    assert.equal(policy.roles.planner.model, 'openai/primary');
});

test('required provider, model limits, and reasoning support are validated', () => {
    const {providers, policy} = fixture();
    const cases = [
        [draft => { draft.provider.name = ''; }, /Fallback connection name is required/],
        [draft => { draft.provider.base_url = ''; }, /Fallback API URL is required/],
        [draft => { draft.choice.model.id = ''; }, /Fallback model name is required/],
        [draft => { draft.choice.model.context_window = 0; }, /Fallback context window must be/],
        [draft => { draft.choice.model.max_output_tokens = 64001; }, /Fallback maximum output must be/],
        [draft => { draft.choice.reasoning_effort = 'ultra'; }, /Fallback reasoning effort is not supported/],
    ];
    for (const [change, message] of cases) {
        const draft = newProviderDraft(providers);
        change(draft);
        assert.throws(() => apply(draft, providers, policy), message);
    }
});
