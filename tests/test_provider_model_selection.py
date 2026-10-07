"""Current frozen provider selections and definitive fallback eligibility."""
import copy
import json
import unittest
from unittest.mock import patch

from modport.model_policy import (
    resolve_model, resolve_model_selection, update_model_config, validate_model_config,
)
from modport.opencode_provider import managed_provider_config, provider_failure_kind
from modport.opencode_runtime import OpenCodeError, OpenCodeHTTPError, OpenCodeResponseError


class ProviderModelSelectionTests(unittest.TestCase):
    def setUp(self):
        self.coder = {'model': 'bailian/deepseek-v4.1-flash', 'reasoning_effort': 'max',
                      'fallback': {'model': 'openai/gpt-6-luna', 'reasoning_effort': 'max'}}
        self.policy = {'default': {'model': 'gpt-6-luna', 'reasoning_effort': 'max'},
                       'roles': {'coder': self.coder}, 'stages': {}}

    def test_frozen_selection_preserves_fallback_through_role_and_stage_resolution(self):
        frozen = validate_model_config(self.policy)
        self.assertEqual(self.coder, resolve_model_selection(frozen, 'coder'))
        self.assertEqual(('bailian/deepseek-v4.1-flash', 'max'), resolve_model(frozen, 'code_cleanup'))
        frozen['stages']['coder'] = {'model': 'openai/gpt-6.1-sol', 'reasoning_effort': 'high'}
        self.assertNotIn('fallback', resolve_model_selection(frozen, 'coder'))
        self.assertEqual(self.coder, self.policy['roles']['coder'])

    def test_fallback_is_explicit_nonrecursive_and_credentials_are_rejected(self):
        for fallback in (
            {'model': 'gpt-6-luna', 'reasoning_effort': 'max'},
            {**self.coder['fallback'], 'fallback': self.coder['fallback']},
            {**self.coder['fallback'], 'api_key': 'fixture-secret'},
            {'model': self.coder['model'], 'reasoning_effort': 'max'},
        ):
            with self.subTest(fallback=fallback):
                policy = copy.deepcopy(self.policy)
                policy['roles']['coder']['fallback'] = fallback
                with self.assertRaises(ValueError):
                    validate_model_config(policy)

    def test_explicit_role_model_change_replaces_prior_fallback(self):
        changed = update_model_config(self.policy, 'coder', 'openai/gpt-6.1-sol', 'high')
        self.assertNotIn('fallback', changed['roles']['coder'])
        self.assertIn('fallback', self.policy['roles']['coder'])

    def test_managed_config_uses_env_reference_and_keeps_provider_settings_independent(self):
        with patch.dict('os.environ', {'OPENAI_BASE_URL': 'http://proxy.invalid/v1/',
                'BAILIAN_BASE_URL': 'https://bailian.invalid/compatible-mode/v1/',
                'BAILIAN_API_KEY': 'fixture-private-bailian-key'}, clear=True):
            config = managed_provider_config()
        self.assertEqual('http://proxy.invalid/v1', config['openai']['options']['baseURL'])
        self.assertEqual('{env:BAILIAN_API_KEY}', config['bailian']['options']['apiKey'])
        self.assertNotIn('fixture-private-bailian-key', json.dumps(config))
        model = config['bailian']['models']['deepseek-v4.1-flash']
        self.assertEqual({'reasoningEffort': 'max'}, model['variants']['max'])
        self.assertEqual({'field': 'reasoning_content'}, model['interleaved'])
        self.assertEqual(1_000_000, model['limit']['context'])

    def test_provider_endpoint_cannot_embed_credentials_or_query(self):
        for endpoint in ('https://user:secret@provider.invalid/v1',
                         'https://provider.invalid/v1?token=secret',
                         'file:///provider/v1', 'https://provider.invalid/v1#secret'):
            with self.subTest(endpoint=endpoint), patch.dict('os.environ',
                    {'BAILIAN_BASE_URL': endpoint}, clear=True), self.assertRaises(ValueError):
                managed_provider_config()

    def test_provider_errors_support_definitive_status_and_stream_quota(self):
        for status in (401, 403, 404, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                error = OpenCodeResponseError({'name': 'APIError',
                    'data': {'statusCode': status, 'isRetryable': False}}, {})
                self.assertIsNotNone(provider_failure_kind(error))
        self.assertEqual('provider_authentication', provider_failure_kind(
            {'name': 'ProviderAuthError', 'data': {'providerID': 'bailian'}}))
        self.assertEqual('provider_quota', provider_failure_kind({'name': 'APIError',
            'data': {'responseBody': '{"error":{"code":"insufficient_quota"}}'}}))

    def test_business_context_and_ambiguous_transport_failures_do_not_fallback(self):
        errors = [TimeoutError('request timed out'), OpenCodeHTTPError(503, 'local server unavailable'),
                  RuntimeError('agent failed'), OpenCodeError('OpenCode variant max is unavailable'),
                  {'name': 'ContextOverflowError', 'data': {'message': 'context overflow'}},
                  {'name': 'StructuredOutputError', 'data': {'message': 'invalid schema'}},
                  {'name': 'MessageAbortedError', 'data': {'message': 'cancelled'}},
                  {'name': 'APIError', 'data': {'isRetryable': True}},
                  {'name': 'APIError', 'data': {'statusCode': 408}},
                  {'name': 'APIError', 'data': {'statusCode': 400, 'message': 'invalid reasoning effort'}},
                  {'name': 'APIError', 'data': {'statusCode': 429,
                   'responseBody': '{"error":{"code":"context_length_exceeded"}}'}}]
        for error in errors:
            with self.subTest(error=error):
                self.assertIsNone(provider_failure_kind(error))

    def test_model_discovery_failure_is_definitive_without_author_turn(self):
        self.assertEqual('provider_not_connected', provider_failure_kind(
            OpenCodeError("OpenCode provider 'bailian' is not connected")))
        self.assertEqual('model_unavailable', provider_failure_kind(
            OpenCodeError('OpenCode model bailian/deepseek-v4.1-flash is unavailable')))


if __name__ == '__main__':
    unittest.main()
