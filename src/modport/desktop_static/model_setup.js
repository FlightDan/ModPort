/* Translate the guided setup into the existing private settings API. */
(function () {
    'use strict';
    const clone = value => JSON.parse(JSON.stringify(value));
    const providerId = /^[a-z][a-z0-9_-]{0,47}$/;
    const modelId = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/;

    function reference(selection) {
        const value = selection?.model || '';
        const separator = value.indexOf('/');
        return separator < 0 ? {provider: 'openai', model: value}
            : {provider: value.slice(0, separator), model: value.slice(separator + 1)};
    }

    function blankProvider(id, name) {
        return {id, name, api_type: 'openai-compatible', base_url: '', api_key: '', models: []};
    }

    function modelChoice(provider, selection) {
        const id = reference(selection).model;
        const model = provider.models.find(item => item.id === id)
            || {id, context_window: '', max_output_tokens: '', reasoning_efforts: []};
        return {model: clone(model), reasoning_effort: selection?.reasoning_effort || model.reasoning_efforts[0] || 'none'};
    }

    function create(providers, policy) {
        const difficultSelection = policy.stages?.migration_plan || policy.roles?.planner || policy.default;
        const routineSelection = policy.stages?.coder || policy.roles?.coder || policy.default;
        const difficultReference = reference(difficultSelection);
        const routineReference = reference(routineSelection);
        const primary = clone(providers.find(item => item.id === difficultReference.provider)
            || blankProvider(difficultReference.provider, 'Primary API'));
        const existingSecondary = routineReference.provider !== primary.id
            ? providers.find(item => item.id === routineReference.provider) : null;
        const used = new Set([...providers.map(item => item.id), primary.id]);
        let index = 1;
        while (used.has(`custom${index}`)) index++;
        const secondary = existingSecondary ? clone(existingSecondary)
            : blankProvider(`custom${index}`, 'Second API');
        const separateConnection = Boolean(existingSecondary);
        const difficult = modelChoice(primary, difficultSelection);
        const routine = modelChoice(separateConnection ? secondary : primary, routineSelection);
        return {primary, secondary, difficult, routine, separateConnection,
            sameModel: !separateConnection && difficult.model.id === routine.model.id
                && difficult.reasoning_effort === routine.reasoning_effort};
    }

    function validateProvider(provider, label) {
        if (typeof provider.id !== 'string' || !providerId.test(provider.id)) throw new Error(`${label}: enter a valid connection ID.`);
        if (typeof provider.name !== 'string' || !provider.name.trim()) throw new Error(`${label}: enter a connection name.`);
        if (typeof provider.base_url !== 'string' || !provider.base_url.trim()) throw new Error(`${label}: enter an API URL.`);
    }

    function validateChoice(choice, label) {
        const model = choice.model;
        if (typeof model.id !== 'string' || !model.id.trim()) throw new Error(`${label}: enter a model name.`);
        if (!modelId.test(model.id.trim())) throw new Error(`${label}: enter a valid model name.`);
        if (!Number.isInteger(model.context_window) || model.context_window <= 0 || model.context_window > 100000000) {
            throw new Error(`${label}: context window must be a positive whole number at most 100000000.`);
        }
        if (!Number.isInteger(model.max_output_tokens) || model.max_output_tokens <= 0 || model.max_output_tokens > model.context_window) {
            throw new Error(`${label}: maximum output must be positive and no greater than the context window.`);
        }
        if (!Array.isArray(model.reasoning_efforts)) throw new Error(`${label}: enter supported reasoning efforts.`);
        const efforts = model.reasoning_efforts.length ? model.reasoning_efforts : ['none'];
        if (!efforts.includes(choice.reasoning_effort)) throw new Error(`${label}: selected reasoning effort is not supported by this model.`);
    }

    function upsertModel(models, model) {
        const normalized = {...clone(model), id: model.id.trim()};
        const index = models.findIndex(item => item.id === normalized.id);
        if (index < 0) models.push(normalized);
        else models[index] = normalized;
    }

    function apply(draft, providers, policy, {validate = true} = {}) {
        const value = clone(draft);
        const result = clone(providers);
        const config = clone(policy);
        config.roles ||= {};
        config.stages ||= {};
        const routineProvider = value.sameModel || !value.separateConnection ? value.primary : value.secondary;
        const routineChoice = value.sameModel ? value.difficult : value.routine;
        // Advanced editing accepts incomplete drafts; the save path validates them.
        if (validate) {
            validateProvider(value.primary, 'First API');
            validateChoice(value.difficult, 'Difficult-task model');
            validateChoice(routineChoice, 'Routine-task model');
            if (routineProvider !== value.primary) {
                validateProvider(routineProvider, 'Second API');
                if (routineProvider.id === value.primary.id) throw new Error('Second API must use a different connection ID.');
            }
        }
        if (!value.sameModel && routineProvider === value.primary
                && value.difficult.model.id.trim() === routineChoice.model.id.trim()) {
            const details = model => JSON.stringify([model?.context_window, model?.max_output_tokens,
                [...(model?.reasoning_efforts || [])].sort()]);
            if (details(value.difficult.model) !== details(routineChoice.model)) {
                const previous = value.primary.models.find(model => model.id === value.difficult.model.id.trim());
                if (!validate && previous && details(routineChoice.model) === details(previous)) {
                    routineChoice.model = clone(value.difficult.model);
                } else if (!validate && previous && details(value.difficult.model) === details(previous)) {
                    value.difficult.model = clone(routineChoice.model);
                } else {
                    throw new Error('Models with the same name on one API must have matching details. Choose the same model or use different names.');
                }
            }
        }
        function upsertProvider(provider) {
            const index = result.findIndex(item => item.id === provider.id);
            const previous = index < 0 ? {} : result[index];
            const merged = {...previous, ...provider, models: clone(previous.models || [])};
            for (const model of provider.models || []) upsertModel(merged.models, model);
            if (index < 0) result.push(merged);
            else result[index] = merged;
            return merged;
        }
        const primary = upsertProvider(value.primary);
        upsertModel(primary.models, value.difficult.model);
        const routine = routineProvider === value.primary ? primary : upsertProvider(routineProvider);
        if (!value.sameModel) upsertModel(routine.models, routineChoice.model);
        const difficultSelection = {model: `${primary.id}/${value.difficult.model.id.trim()}`, reasoning_effort: value.difficult.reasoning_effort};
        const routineSelection = {model: `${routine.id}/${routineChoice.model.id.trim()}`, reasoning_effort: routineChoice.reasoning_effort};
        for (const role of ['planner', 'contract_review']) config.roles[role] = {...config.roles[role], ...difficultSelection};
        for (const role of ['coder', 'supervisor', 'summary']) config.roles[role] = {...config.roles[role], ...routineSelection};
        config.default = {...config.default, ...routineSelection};
        return {providers: result.map(({saved, api_key_configured, ...provider}) => provider), model_config: config};
    }

    const api = {create, apply};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof window !== 'undefined') window.ModPortModelSetup = api;
})();
