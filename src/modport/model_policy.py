"""Independent model settings, resolved at submission and frozen with each Run."""

import json
import os
from pathlib import Path
from collections.abc import Mapping


DEFAULT_MODEL_CONFIG_PATH = "modport-models.json"
ROLE_STAGES = {
    "planner": frozenset({"migration_plan", "contract_repair_plan", "target_repair_plan", "coder_revival_plan", "behavior_extract"}),
    "coder": frozenset({"coder", "agent_rework", "contract_draft", "artifact_test_design", "test_design", "code_cleanup", "final_cleanup"}),
    "supervisor": frozenset({"supervisor"}),
    "contract_review": frozenset({"contract_review", "behavior_review"}),
    "summary": frozenset({"prompt_summary"}),
    "subagent": frozenset(),
}


def _selection(value, *, allow_fallback=True):
    required = {"model", "reasoning_effort"}
    allowed = required | ({"fallback"} if allow_fallback else set())
    if (not isinstance(value, Mapping) or not required <= set(value)
            or set(value) - allowed):
        raise ValueError("model selection requires model and reasoning_effort")
    result = {}
    for key in ("model", "reasoning_effort"):
        text = value[key]
        if not isinstance(text, str) or not text.strip() or any(ord(char) < 32 for char in text):
            raise ValueError(f"invalid model selection {key}")
        result[key] = text.strip()
    if "fallback" in value:
        fallback = _selection(value["fallback"], allow_fallback=False)
        provider, separator, model = fallback["model"].partition("/")
        if not separator or not provider or not model:
            raise ValueError("fallback model requires an explicit provider/model ID")
        if (fallback["model"] == result["model"]
                and fallback["reasoning_effort"] == result["reasoning_effort"]):
            raise ValueError("fallback must select a different model or reasoning effort")
        result["fallback"] = fallback
    return result


def validate_model_config(value):
    if not isinstance(value, Mapping) or set(value) - {"default", "roles", "stages"}:
        raise ValueError("model configuration requires default with optional roles and stages")
    default = _selection(value.get("default"))
    roles, stages = value.get("roles", {}), value.get("stages", {})
    if not isinstance(roles, Mapping) or set(roles) - set(ROLE_STAGES):
        raise ValueError("unknown model configuration role")
    if not isinstance(stages, Mapping) or any(not isinstance(key, str) or not key.strip() for key in stages):
        raise ValueError("invalid model configuration stage")
    return {"default": default,
            "roles": {key: _selection(item) for key, item in roles.items()},
            "stages": {key: _selection(item) for key, item in stages.items()}}


def load_model_config(path=None):
    """Read an explicit file, configured default, or packaged defaults."""
    if path is None:
        configured = os.environ.get("MODPORT_MODEL_CONFIG")
        local = Path(DEFAULT_MODEL_CONFIG_PATH)
        path = configured or (local if local.is_file() else Path(__file__).parent / "rules" / "models.json")
    return validate_model_config(json.loads(Path(path).expanduser().read_text(encoding="utf-8")))


def resolve_model_selection(config, stage=None):
    """Select frozen provider/model settings, including an optional fallback."""
    config = validate_model_config(config)
    if stage == "subagent":
        return config["roles"].get("subagent") or resolve_model_selection(config, "coder")
    selection = config["stages"].get(stage)
    if selection is None:
        role = next((role for role, stages in ROLE_STAGES.items() if stage in stages), None)
        selection = config["roles"].get(role, config["default"])
    return selection


def resolve_model(config, stage=None):
    """Select a stage override, its role, or the default from frozen settings."""
    selection = resolve_model_selection(config, stage)
    return selection["model"], selection["reasoning_effort"]


def update_model_config(config, role, model, reasoning_effort):
    config = validate_model_config(config)
    selection = _selection({"model": model, "reasoning_effort": reasoning_effort})
    if role == "default":
        config["default"] = selection
    elif role in ROLE_STAGES:
        config["roles"][role] = selection
    else:
        raise ValueError("unknown model configuration role")
    return config


def save_model_config(path, config):
    from .evidence import atomic_json
    atomic_json(Path(path).expanduser(), validate_model_config(config))
