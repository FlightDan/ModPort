"""Host-selected provider endpoints for managed OpenCode, without credentials."""
from __future__ import annotations

import os
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from urllib.parse import urlsplit


# Alibaba publishes these limits for deepseek-v4.1-flash. Preserve reasoning
# between tool calls using OpenCode's supported interleaved response field.
# https://www.alibabacloud.com/help/en/model-studio/deepseek-v4-1-flash
_BAILIAN_MODELS = {
    'deepseek-v4.1-flash': {
        'name': 'DeepSeek V4.1 Flash',
        'limit': {'context': 1_000_000, 'input': 1_000_000, 'output': 393_216},
        'reasoning': True,
        'tool_call': True,
        'interleaved': {'field': 'reasoning_content'},
        'variants': {'max': {'reasoningEffort': 'max'}},
    },
}


# The managed proxy serves these model IDs even when OpenCode's downloaded
# provider catalog has not caught up. Pin the limits used by the host prompt
# budget so the model remains discoverable in GET /provider.
_PROXY_MODELS = {
    'gpt-6-astra': {
        'name': 'GPT-6 Astra',
        'limit': {'context': 1_050_000, 'input': 922_000, 'output': 128_000},
        'reasoning': True,
        'variants': {'high': {'reasoningEffort': 'high'}},
    },
    'gpt-6-luna': {
        'name': 'GPT-6 Luna',
        'limit': {'context': 1_050_000, 'input': 922_000, 'output': 128_000},
        'reasoning': True,
        'variants': {'max': {'reasoningEffort': 'max'}},
    },
    'gpt-6-sol': {
        'name': 'GPT-6 Sol',
        'limit': {'context': 1_050_000, 'input': 922_000, 'output': 128_000},
        'reasoning': True,
        'variants': {'high': {'reasoningEffort': 'high'}},
    },
    'gpt-6.1-sol': {
        'name': 'GPT-6.1 Sol',
        'limit': {'context': 1_050_000, 'input': 922_000, 'output': 128_000},
        'reasoning': True,
        'variants': {'high': {'reasoningEffort': 'high'}},
    },
}


def _endpoint(variable: str) -> str | None:
    raw = os.environ.get(variable, '').strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError(f'{variable} must be an HTTP(S) endpoint without credentials or query')
    return raw.rstrip('/')


def openai_provider_config() -> dict:
    """Return a host-selected endpoint without embedding provider credentials."""
    endpoint = _endpoint('OPENAI_BASE_URL')
    if endpoint is None:
        return {}
    return {'openai': {
        'options': {'baseURL': endpoint},
        'models': deepcopy(_PROXY_MODELS),
    }}


def bailian_provider_config() -> dict:
    """Use the private host environment for the Bailian Coding Plan key."""
    endpoint = _endpoint('BAILIAN_BASE_URL')
    if endpoint is None:
        return {}
    return {'bailian': {
        'npm': '@ai-sdk/openai-compatible',
        'name': 'Alibaba Bailian',
        'env': ['BAILIAN_API_KEY'],
        'options': {'baseURL': endpoint, 'apiKey': '{env:BAILIAN_API_KEY}'},
        'models': deepcopy(_BAILIAN_MODELS),
    }}


def managed_provider_config() -> dict:
    """Expose both authorized providers to a single managed assignment."""
    from .desktop_model_settings import (MAX_SETTINGS_BYTES, PROVIDER_CONFIG_ENV,
                                         provider_key_environment, validate_providers)
    configured = os.environ.get(PROVIDER_CONFIG_ENV)
    if configured is not None:
        if len(configured.encode('utf-8')) > MAX_SETTINGS_BYTES:
            raise ValueError('Desktop provider configuration exceeds its size limit')
        try:
            providers = validate_providers(json.loads(configured))
        except (TypeError, ValueError) as error:
            raise ValueError('Invalid desktop provider configuration') from error
        result = {}
        for provider in providers:
            credential = provider_key_environment(provider['id'])
            options = {'baseURL': provider['base_url']}
            # An absent key leaves local/no-auth endpoints and existing OpenCode
            # OAuth logins available; never manufacture placeholder credentials.
            if os.environ.get(credential):
                options['apiKey'] = '{env:' + credential + '}'
            elif provider['api_type'] == 'openai' and provider['id'] != 'openai':
                # The OpenAI SDK falls back to OPENAI_API_KEY only when its
                # apiKey option is absent. A custom endpoint must never inherit
                # another provider's credential; an explicit empty key blocks
                # that fallback without inventing authentication.
                options['apiKey'] = ''
            models = {}
            for model in provider['models']:
                efforts = model['reasoning_efforts']
                row = {'name': model['id'], 'limit': {
                    'context': model['context_window'],
                    'input': max(1, model['context_window'] - model['max_output_tokens']),
                    'output': model['max_output_tokens']},
                    'reasoning': bool(efforts), 'tool_call': True,
                    'variants': {effort: {'reasoningEffort': effort}
                                 for effort in efforts} if efforts else {'none': {}}}
                known = _BAILIAN_MODELS.get(model['id'], {})
                if provider['api_type'] == 'openai-compatible' and 'interleaved' in known:
                    row['interleaved'] = deepcopy(known['interleaved'])
                models[model['id']] = row
            result[provider['id']] = {
                'npm': '@ai-sdk/openai' if provider['api_type'] == 'openai' else '@ai-sdk/openai-compatible',
                'name': provider['name'], 'env': [credential],
                'options': options, 'models': models}
        return result
    return {**openai_provider_config(), **bailian_provider_config()}


def provider_failure_kind(error) -> str | None:
    """Classify definitive provider unavailability, never generic retryability.

    The caller owns once-only fallback, response/tool settlement, durable model
    selection and the original assignment deadline. Local HTTP failures,
    cancellation, transport disconnects and ambiguous timeouts are excluded.
    A model response's ``isRetryable`` alone is insufficient authorization.
    """
    from .opencode_runtime import OpenCodeError, OpenCodeResponseError

    if type(error) is OpenCodeError:
        # These are the exact discovery failures from require_model(), before
        # any model turn is dispatched. Unsupported variants are configuration
        # errors and intentionally retain their original failure.
        message = str(error)
        if re.fullmatch(r"OpenCode provider '[^']+' is not connected", message):
            return 'provider_not_connected'
        if re.fullmatch(r'OpenCode model [^\s]+ is unavailable', message):
            return 'model_unavailable'
        return None
    if isinstance(error, OpenCodeResponseError):
        error = error.error
    if not isinstance(error, Mapping):
        return None
    name = error.get('name')
    data = error.get('data')
    if not isinstance(data, Mapping):
        return None
    if name == 'ProviderAuthError':
        return 'provider_authentication'
    if name != 'APIError':
        return None
    body = data.get('responseBody')
    if isinstance(body, str) and len(body) <= 65_536:
        try:
            body = json.loads(body)
        except ValueError:
            body = None
    body_error = body.get('error', body) if isinstance(body, Mapping) else None
    code = body_error.get('code') if isinstance(body_error, Mapping) else None
    code = code.casefold() if isinstance(code, str) else ''
    if code in {'context_length_exceeded', 'invalid_prompt', 'invalid_request_error',
                'content_filter', 'content_policy_violation'}:
        return None
    if code in {'invalid_api_key', 'invalidapikey', 'apikeyinvalid'}:
        return 'provider_authentication'
    if code in {'insufficient_quota', 'insufficientbalance', 'quota_exceeded',
                'usage_not_included'}:
        return 'provider_quota'
    if code in {'model_not_found', 'modelnotfound', 'model_not_available'}:
        return 'model_unavailable'
    if code in {'rate_limit_exceeded', 'ratelimitexceeded', 'throttling'}:
        return 'provider_rate_limit'
    if code in {'server_is_overloaded', 'server_error', 'service_unavailable'}:
        return 'provider_unavailable'
    status = data.get('statusCode')
    if type(status) is not int:
        return None
    if status in {401, 403}:
        return 'provider_authentication'
    if status == 404:
        return 'model_unavailable'
    if status == 429:
        return 'provider_rate_limit'
    if status in {500, 502, 503, 504}:
        return 'provider_unavailable'
    return None
