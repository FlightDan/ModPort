"""Native OpenCode helpers within their parent ModPort assignment scope."""
from collections.abc import Mapping
from copy import deepcopy

from .model_policy import resolve_model_selection
from .opencode_runtime import resolve_model_id


SUBAGENT_NAMES = ("general", "explore")
TASK_PERMISSION = {"*": "deny", **{name: "allow" for name in SUBAGENT_NAMES}}


def subagent_profiles(*, model_policy: Mapping | None, model: str, variant: str,
                      permissions: Mapping, inherit_coder: bool = False) -> dict[str, dict]:
    """Freeze the coder selection and explicitly carry host restrictions.

    OpenCode copies only selected parent session rules into native children;
    per-message tool restrictions alone do not restrict a child's tools.
    """
    selection = (resolve_model_selection(model_policy, "subagent")
                 if model_policy is not None else
                 {"model": model, "reasoning_effort": variant})
    child_permissions = deepcopy(dict(permissions))
    child_permissions.update({
        "bash": "deny", "shell": "deny", "terminal": "deny",
        "task": "deny", "question": "deny", "modport_rework_*": "deny",
    })
    # A coder's helpers follow its effective model, including authorized
    # fallback within the same session. Other parents use the frozen coder role.
    inherit = inherit_coder and not (model_policy and model_policy.get("roles", {}).get("subagent"))
    model_settings = ({} if inherit else
                      {"model": resolve_model_id(selection["model"]),
                       "variant": selection["reasoning_effort"]})
    profiles = {name: {"mode": "subagent", **model_settings,
                       "permission": deepcopy(child_permissions)} for name in SUBAGENT_NAMES}
    profiles["explore"]["permission"].update({
        "edit": "deny", "modport_sandbox_run_project_command": "deny",
    })
    return profiles
