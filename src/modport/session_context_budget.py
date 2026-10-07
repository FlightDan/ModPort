"""Host preflight for another turn in an existing OpenCode session."""
from __future__ import annotations

import json
from typing import Any, Mapping


class SessionContextBudgetError(RuntimeError):
    pass


def context_budget(compression: Mapping[str, Any]) -> dict[str, int]:
    """Keep the selected model's frozen prompt reserves, not a later catalog value."""
    keys = ('context_window', 'input_token_budget', 'output_token_reserve',
            'tool_token_reserve')
    values = {key: compression.get(key) for key in keys}
    if any(type(value) is not int or value <= 0 for value in values.values()):
        raise SessionContextBudgetError('selected model context budget is unavailable')
    if sum((values['input_token_budget'], values['output_token_reserve'],
            values['tool_token_reserve'])) > values['context_window']:
        raise SessionContextBudgetError('selected model context reserves exceed its window')
    return values


def prior_context_usage(*, tokens: Mapping[str, Any] | None,
                        prior_prompt: str, response: Any) -> dict[str, Any]:
    """Use final-step context usage; fall back to an upper byte bound if absent."""
    if (isinstance(tokens, Mapping)
            and type(tokens.get('input')) is int and tokens['input'] > 0
            and type(tokens.get('output')) is int and tokens['output'] >= 0):
        cache = tokens.get('cache')
        cached = (sum(value for value in cache.values()
                      if type(value) is int and value >= 0)
                  if isinstance(cache, Mapping) else 0)
        generated = sum(value for key in ('output', 'reasoning')
                        if type(value := tokens.get(key)) is int and value >= 0)
        return {'tokens': tokens['input'] + cached + generated,
                'source': 'final_assistant_usage'}
    encoded = json.dumps(response, ensure_ascii=False, default=str,
                         separators=(',', ':')).encode('utf-8')
    return {'tokens': len(prior_prompt.encode('utf-8')) + len(encoded),
            'source': 'transcript_byte_upper_bound'}


def require_next_turn_capacity(budget: Mapping[str, Any], usage: Mapping[str, Any],
                               next_prompt: str,
                               output_format: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ceiling = context_budget(budget)['input_token_budget']
    prior = usage.get('tokens')
    if type(prior) is not int or prior < 0:
        raise SessionContextBudgetError('prior session context usage is unavailable')
    # One UTF-8 byte per token remains conservative without a verified tokenizer.
    prompt_tokens = len(next_prompt.encode('utf-8'))
    if output_format is not None:
        prompt_tokens += len(json.dumps(output_format, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8'))
    safety = max(256, budget['context_window'] // 100)
    required = prior + prompt_tokens + safety
    result = {'prior_tokens': prior, 'prior_source': usage.get('source'),
              'next_prompt_tokens_upper_bound': prompt_tokens,
              'safety_tokens': safety, 'required_tokens': required,
              'input_token_budget': ceiling}
    if required > ceiling:
        raise SessionContextBudgetError(
            f'OpenCode session context preflight exceeded input budget: '
            f'required={required}, available={ceiling}, source={usage.get("source")}')
    return result
