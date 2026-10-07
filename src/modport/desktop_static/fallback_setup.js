/* Add one shared provider fallback to the existing private settings policy. */
(function () {
    'use strict';

    const providerIdPattern = /^[a-z][a-z0-9_-]{0,47}$/;
    const modelIdPattern = /^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$/;
    const reasoningEfforts = new Set(['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']);
    const clone = value => JSON.parse(JSON.stringify(value));
    const isRecord = value => value !== null && typeof value === 'object' && !Array.isArray(value);

    function blankModel() {
        return {id: '', context_window: '', max_output_tokens: '', reasoning_efforts: []};
    }

    function nextCustomId(providers) {
        const used = new Set(providers.map(provider => provider?.id));
        let index = 1;
        while (used.has(`custom${index}`)) index++;
        return `custom${index}`;
    }

    function create(providers, _policy) {
        if (!Array.isArray(providers)) throw new Error('Provider settings must be a list.');
        return {
            mode: 'preserve',
            providerId: '',
            provider: {
                id: nextCustomId(providers), name: 'Fallback API',
                api_type: 'openai-compatible', base_url: '', api_key: '', models: [],
            },
            choice: {model: blankModel(), reasoning_effort: 'none'},
        };
    }

    function stripUiMetadata(provider) {
        const {saved, api_key_configured, ...result} = provider;
        return clone(result);
    }

    function cleanProviders(providers) {
        if (!Array.isArray(providers)) throw new Error('Provider settings must be a list.');
        return providers.map(stripUiMetadata);
    }

    function validateProvider(provider, providers) {
        if (!isRecord(provider) || typeof provider.id !== 'string'
                || !providerIdPattern.test(provider.id)) {
            throw new Error('Fallback connection ID is invalid.');
        }
        if (providers.some(item => item?.id === provider.id)) {
            throw new Error('Fallback connection ID is already in use.');
        }
        if (typeof provider.name !== 'string' || !provider.name.trim() || provider.name.length > 120) {
            throw new Error('Fallback connection name is required and must be at most 120 characters.');
        }
        if (!['openai', 'openai-compatible'].includes(provider.api_type)) {
            throw new Error('Fallback API type is unsupported.');
        }
        if (typeof provider.base_url !== 'string' || !provider.base_url.trim()
                || provider.base_url.length > 2000 || /\s/.test(provider.base_url)) {
            throw new Error('Fallback API URL is required and must be a valid HTTP(S) URL.');
        }
        let endpoint;
        try { endpoint = new URL(provider.base_url); }
        catch { throw new Error('Fallback API URL is required and must be a valid HTTP(S) URL.'); }
        if (!['http:', 'https:'].includes(endpoint.protocol) || !endpoint.hostname
                || endpoint.username || endpoint.password || endpoint.search || endpoint.hash) {
            throw new Error('Fallback API URL must be HTTP(S) without credentials, query, or fragment.');
        }
        if (provider.api_key !== undefined && (typeof provider.api_key !== 'string'
                || provider.api_key.length > 4096
                || /[\x00-\x1f\x7f{}]/.test(provider.api_key))) {
            throw new Error('Fallback API key is invalid.');
        }
        if (!Array.isArray(provider.models) || provider.models.length > 64) {
            throw new Error('Fallback provider has an invalid model catalog.');
        }
    }

    function validateChoice(choice) {
        if (!isRecord(choice) || !isRecord(choice.model)) {
            throw new Error('Fallback model details are required.');
        }
        const model = choice.model;
        if (typeof model.id !== 'string' || !model.id.trim()) {
            throw new Error('Fallback model name is required.');
        }
        if (!modelIdPattern.test(model.id.trim())) {
            throw new Error('Fallback model name is invalid.');
        }
        if (!Number.isInteger(model.context_window) || model.context_window < 1
                || model.context_window > 100000000) {
            throw new Error('Fallback context window must be a positive whole number at most 100000000.');
        }
        if (!Number.isInteger(model.max_output_tokens) || model.max_output_tokens < 1
                || model.max_output_tokens > model.context_window) {
            throw new Error('Fallback maximum output must be positive and no greater than context window.');
        }
        if (!Array.isArray(model.reasoning_efforts) || model.reasoning_efforts.length > 16
                || model.reasoning_efforts.some(effort => typeof effort !== 'string'
                    || !reasoningEfforts.has(effort))
                || new Set(model.reasoning_efforts).size !== model.reasoning_efforts.length) {
            throw new Error('Fallback model reasoning efforts are invalid.');
        }
        const supported = model.reasoning_efforts.length ? model.reasoning_efforts : ['none'];
        if (typeof choice.reasoning_effort !== 'string'
                || !reasoningEfforts.has(choice.reasoning_effort)
                || !supported.includes(choice.reasoning_effort)) {
            throw new Error('Fallback reasoning effort is not supported by this model.');
        }
    }

    function validateCatalogEffort(effort, model) {
        const supported = model.reasoning_efforts.length ? model.reasoning_efforts : ['none'];
        if (typeof effort !== 'string' || !reasoningEfforts.has(effort)
                || !supported.includes(effort)) {
            throw new Error('Fallback reasoning effort is not supported by this model.');
        }
    }

    function modelDetails(model) {
        return JSON.stringify([
            model.context_window,
            model.max_output_tokens,
            [...model.reasoning_efforts].sort(),
        ]);
    }

    function explicitSelections(config) {
        if (!isRecord(config) || !isRecord(config.default)
                || (config.roles !== undefined && !isRecord(config.roles))
                || (config.stages !== undefined && !isRecord(config.stages))) {
            throw new Error('Model settings contain an invalid selection map.');
        }
        config.roles ||= {};
        config.stages ||= {};
        const selections = [config.default, ...Object.values(config.roles), ...Object.values(config.stages)];
        if (selections.some(selection => !isRecord(selection))) {
            throw new Error('Model settings contain an invalid selection.');
        }
        return selections;
    }

    function canonicalModel(model) {
        if (typeof model !== 'string') return '';
        const separator = model.indexOf('/');
        return separator < 0 ? `openai/${model}` : model;
    }

    function apply(draft, providers, policy, {validate = true} = {}) {
        if (!isRecord(draft) || !['preserve', 'off', 'shared'].includes(draft.mode)) {
            throw new Error('Fallback mode is invalid.');
        }
        const resultProviders = cleanProviders(providers);
        const config = clone(policy);

        if (draft.mode === 'preserve') {
            return {providers: resultProviders, model_config: config};
        }

        const selections = explicitSelections(config);
        if (draft.mode === 'off') {
            for (const selection of selections) delete selection.fallback;
            return {providers: resultProviders, model_config: config};
        }

        let provider;
        let selectedModel;
        if (draft.providerId) {
            if (typeof draft.providerId !== 'string' || !providerIdPattern.test(draft.providerId)) {
                throw new Error('Selected fallback provider ID is invalid.');
            }
            provider = resultProviders.find(item => item.id === draft.providerId);
            if (!provider) throw new Error('Selected fallback provider was not found in settings.');
            if (!isRecord(draft.choice) || !isRecord(draft.choice.model)
                    || typeof draft.choice.model.id !== 'string' || !draft.choice.model.id.trim()) {
                throw new Error('Fallback model name is required.');
            }
            const modelId = draft.choice.model.id.trim();
            if (validate && !modelIdPattern.test(modelId)) {
                throw new Error('Fallback model name is invalid.');
            }
            selectedModel = provider.models?.find(item => item?.id === modelId);
            if (!selectedModel) {
                throw new Error('Selected fallback model was not found in the provider catalog.');
            }
            if (validate) validateCatalogEffort(draft.choice.reasoning_effort, selectedModel);
        } else {
            if (!isRecord(draft.provider)) throw new Error('Fallback provider details are required.');
            provider = stripUiMetadata(draft.provider);
            // The guided primary setup can reserve the same customN ID after this
            // draft was created. Rebase at apply time so it can never replace it.
            provider.id = nextCustomId(resultProviders);
            if (validate) validateProvider(provider, resultProviders);
            if (resultProviders.length >= 16 && validate) {
                throw new Error('Fallback provider catalog has reached the 16-provider limit.');
            }
            resultProviders.push(provider);
            if (validate) validateChoice(draft.choice);
            if (!isRecord(draft.choice) || !isRecord(draft.choice.model)
                    || typeof draft.choice.model.id !== 'string') {
                throw new Error('Fallback model details are required.');
            }
            selectedModel = {
                id: draft.choice.model.id.trim(),
                context_window: draft.choice.model.context_window,
                max_output_tokens: draft.choice.model.max_output_tokens,
                reasoning_efforts: clone(draft.choice.model.reasoning_efforts),
            };
        }

        if (!draft.providerId) {
            if (!Array.isArray(provider.models)) provider.models = [];
            const registered = provider.models.find(item => item?.id === selectedModel.id);
            if (registered && modelDetails(registered) !== modelDetails(selectedModel)) {
                throw new Error('Fallback model conflicts with an existing model on this API.');
            }
            if (!registered) {
                if (provider.models.length >= 64 && validate) {
                    throw new Error('Fallback provider has reached the 64-model limit.');
                }
                provider.models.push(selectedModel);
            }
        }

        const modelReference = `${provider.id}/${selectedModel.id}`;
        for (const selection of selections) {
            const primaryReference = canonicalModel(selection.model);
            if (primaryReference === modelReference
                    && selection.reasoning_effort === draft.choice.reasoning_effort) {
                delete selection.fallback;
            } else {
                selection.fallback = {
                    model: modelReference,
                    reasoning_effort: draft.choice.reasoning_effort,
                };
            }
        }
        return {providers: resultProviders, model_config: config};
    }

    const api = {create, apply};
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (typeof window !== 'undefined') window.ModPortFallbackSetup = api;
})();
