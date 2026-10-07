"""Private desktop provider settings and future-launch environment overlays."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from .model_policy import load_model_config, validate_model_config
from .platform_files import (FileLock, assert_host_owned, atomic_write,
                             make_private_directory, safe_open)

PROVIDER_CONFIG_ENV = 'MODPORT_DESKTOP_PROVIDER_CONFIG'
PROVIDER_KEY_PREFIX = 'MODPORT_PROVIDER_KEY_'
MAX_SETTINGS_BYTES = 49152
_PROVIDER_ID = re.compile(r'[a-z][a-z0-9_-]{0,47}\Z')
_MODEL_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z')
REASONING_EFFORTS = frozenset({'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'})
_KEY_ENV = re.compile(PROVIDER_KEY_PREFIX + r'(?:[0-9A-F]{2}){1,48}\Z')


def provider_key_environment(provider_id):
    if not isinstance(provider_id, str) or not _PROVIDER_ID.fullmatch(provider_id):
        raise ValueError('Invalid provider ID')
    return {'openai': 'OPENAI_API_KEY', 'bailian': 'BAILIAN_API_KEY'}.get(
        provider_id, PROVIDER_KEY_PREFIX + provider_id.encode('ascii').hex().upper())


def provider_environment_key(key):
    return key == PROVIDER_CONFIG_ENV or bool(_KEY_ENV.fullmatch(key))


def _literal(value, label, maximum):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or '{' in value or '}' in value):
        raise ValueError('Invalid ' + label)
    return value.strip()


def validate_providers(value, *, allow_keys=False):
    """Validate literals before OpenCode can interpret configuration templates."""
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError('Configure between 1 and 16 providers')
    providers, identities = [], set()
    fields = {'id', 'name', 'api_type', 'base_url', 'models'}
    for item in value:
        if not isinstance(item, dict) or not fields <= set(item) or set(item) - fields - ({'api_key'} if allow_keys else set()):
            raise ValueError('Invalid provider settings fields')
        identifier = _literal(item['id'], 'provider ID', 48)
        if not _PROVIDER_ID.fullmatch(identifier) or identifier in identities:
            raise ValueError('Provider IDs must be unique lowercase identifiers')
        identities.add(identifier)
        name = _literal(item['name'], 'provider name', 120)
        api_type = item['api_type']
        if api_type not in ('openai', 'openai-compatible'):
            raise ValueError('Unsupported provider API type')
        endpoint = _literal(item['base_url'], 'provider URL', 2000)
        try:
            parsed = urlsplit(endpoint)
            parsed.port
        except ValueError as error:
            raise ValueError('Invalid provider URL') from error
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(char.isspace() for char in endpoint)):
            raise ValueError('Provider URL must be HTTP(S) without credentials, query or fragment')
        models, model_ids = [], set()
        if not isinstance(item['models'], list) or not 1 <= len(item['models']) <= 64:
            raise ValueError('Configure between 1 and 64 models per provider')
        for model in item['models']:
            if not isinstance(model, dict) or set(model) != {'id', 'context_window', 'max_output_tokens', 'reasoning_efforts'}:
                raise ValueError('Invalid model settings fields')
            model_id = _literal(model['id'], 'model ID', 128)
            if not _MODEL_ID.fullmatch(model_id) or model_id in model_ids:
                raise ValueError('Model IDs must be unique literal identifiers within a provider')
            model_ids.add(model_id)
            context, output = model['context_window'], model['max_output_tokens']
            if type(context) is not int or not 1 <= context <= 100_000_000 or type(output) is not int or not 1 <= output <= context:
                raise ValueError('Model token limits must be positive integers with output at most context')
            efforts = model['reasoning_efforts']
            if (not isinstance(efforts, list) or len(efforts) > 16
                    or any(not isinstance(effort, str) or effort not in REASONING_EFFORTS for effort in efforts)
                    or len(set(efforts)) != len(efforts)):
                raise ValueError('Invalid model reasoning efforts')
            models.append({'id': model_id, 'context_window': context,
                           'max_output_tokens': output, 'reasoning_efforts': list(efforts)})
        provider = {'id': identifier, 'name': name, 'api_type': api_type,
                    'base_url': endpoint.rstrip('/'), 'models': models}
        if 'api_key' in item:
            key = item['api_key']
            if not isinstance(key, str) or len(key) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in key) or '{' in key or '}' in key:
                raise ValueError('Invalid provider API key')
            if key.strip():
                provider['api_key'] = key.strip()
        providers.append(provider)
    return providers


def _validate_policy(value):
    policy = validate_model_config(value)
    selections = [policy['default'], *policy['roles'].values(), *policy['stages'].values()]
    for selection in selections:
        for item in (selection, selection.get('fallback')):
            if item is None:
                continue
            model = _literal(item['model'], 'selected model', 177)
            if not _MODEL_ID.fullmatch(model) and not (
                    '/' in model and _PROVIDER_ID.fullmatch(model.split('/', 1)[0])
                    and _MODEL_ID.fullmatch(model.split('/', 1)[1])):
                raise ValueError('Invalid selected model')
            if item['reasoning_effort'] not in REASONING_EFFORTS:
                raise ValueError('Invalid selected reasoning effort')
    if len(policy['stages']) > 128:
        raise ValueError('Too many stage model overrides')
    if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,95}', stage) for stage in policy['stages']):
        raise ValueError('Invalid stage model override identifier')
    return policy


def _validate_selections(policy, providers):
    catalog = {provider['id']: {model['id']: set(model['reasoning_efforts'] or ['none'])
                               for model in provider['models']} for provider in providers}
    for selection in [policy['default'], *policy['roles'].values(), *policy['stages'].values()]:
        for item in (selection, selection.get('fallback')):
            if item is None:
                continue
            provider, separator, model = item['model'].partition('/')
            if not separator:
                provider, model = 'openai', provider
            efforts = catalog.get(provider, {}).get(model)
            if efforts is None:
                raise ValueError(f'选择的模型 {provider}/{model} 不在已配置的模型列表中，请添加该模型或修改角色选择。')
            if item['reasoning_effort'] not in efforts:
                supported = '、'.join(sorted(efforts))
                raise ValueError(f"模型 {provider}/{model} 不支持推理强度 {item['reasoning_effort']}，可选值为：{supported}。")


def _defaults():
    from .opencode_provider import _PROXY_MODELS, managed_provider_config
    catalog = managed_provider_config()
    catalog.setdefault('openai', {'name': 'OpenAI', 'options': {'baseURL': 'https://api.openai.com/v1'}, 'models': _PROXY_MODELS})
    providers = []
    for identifier, settings in catalog.items():
        models = [{'id': model_id, 'context_window': model['limit']['context'],
                   'max_output_tokens': model['limit']['output'],
                   'reasoning_efforts': list(model.get('variants', {}))}
                  for model_id, model in settings.get('models', {}).items()]
        providers.append({'id': identifier, 'name': settings.get('name', identifier.title()),
                          'api_type': 'openai-compatible' if settings.get('npm') == '@ai-sdk/openai-compatible' else 'openai',
                          'base_url': settings['options']['baseURL'], 'models': models})
    return {'providers': validate_providers(providers), 'model_config': _validate_policy(load_model_config())}


class ModelSettingsStore:
    def __init__(self, root):
        self.directory = make_private_directory(Path(root).expanduser().absolute() / 'model-settings')
        if os.name != 'nt' and self.directory.stat().st_mode & 0o077:
            raise ValueError('Model settings directory must be private to its owner')
        self.path = self.directory / 'settings.json'
        self._lock = threading.RLock()

    def _load(self):
        assert_host_owned(self.directory)
        try:
            descriptor = safe_open(self.directory, self.path.name)
        except FileNotFoundError:
            return _defaults()
        with os.fdopen(descriptor, 'rb') as stream:
            assert_host_owned(self.path)
            raw = stream.read(MAX_SETTINGS_BYTES + 1)
        if len(raw) > MAX_SETTINGS_BYTES:
            raise ValueError('Model settings exceed their size limit')
        try:
            value = json.loads(raw)
        except ValueError as error:
            raise ValueError('Invalid saved model settings') from error
        if not isinstance(value, dict) or set(value) != {'providers', 'model_config'}:
            raise ValueError('Invalid saved model settings fields')
        return {'providers': validate_providers(value['providers'], allow_keys=True),
                'model_config': _validate_policy(value['model_config'])}

    @staticmethod
    def _public(value):
        providers = []
        for provider in value['providers']:
            row = {key: item for key, item in provider.items() if key != 'api_key'}
            row['api_key_configured'] = bool(provider.get('api_key') or os.environ.get(provider_key_environment(provider['id'])))
            providers.append(row)
        return {'providers': providers, 'model_config': value['model_config']}

    def read(self):
        with self._lock:
            return self._public(self._load())

    def save(self, body):
        if not isinstance(body, dict) or set(body) != {'providers', 'model_config'}:
            raise ValueError('Invalid model settings fields')
        providers = validate_providers(body['providers'], allow_keys=True)
        policy = _validate_policy(body['model_config'])
        _validate_selections(policy, providers)
        with self._lock, FileLock(self.directory / 'settings.lock'):
            previous = {item['id']: item for item in self._load()['providers']}
            for item in providers:
                if 'api_key' not in item and previous.get(item['id'], {}).get('api_key'):
                    item['api_key'] = previous[item['id']]['api_key']
            value = {'providers': providers, 'model_config': policy}
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
            if len(encoded) > MAX_SETTINGS_BYTES:
                raise ValueError('Model settings exceed their size limit')
            atomic_write(self.path, encoded, mode=0o600)
            assert_host_owned(self.path)
            return self._public(value)

    def runtime_environment(self):
        """An isolated overlay; never mutate global or active-Run environments."""
        with self._lock:
            if not self.path.exists() and not self.path.is_symlink():
                return {}
            settings = self._load()
            providers, environment = [], {}
            for item in settings['providers']:
                providers.append({key: value for key, value in item.items() if key != 'api_key'})
                if item.get('api_key'):
                    environment[provider_key_environment(item['id'])] = item['api_key']
            environment[PROVIDER_CONFIG_ENV] = json.dumps(providers, ensure_ascii=False, allow_nan=False)
            return environment
