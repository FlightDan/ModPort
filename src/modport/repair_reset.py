"""Invalidate active repair projections while retaining execution evidence."""
from .workflow import MAIN_STAGES


def require_settled_review_rework(app):
    if any(row.get("state") in {"pending", "running"}
           for row in app.get("review_rework", {}).get("requests", {}).values()):
        raise ValueError("settle reviewer rework before restarting contract repair")


def contract_repair_tail(*, include_diagnosis=False):
    stages = {"contract_repair_plan", "contract_repair_tasks", "contract_repair_review",
              "contract_revise", "contract_repair_integrate", "contract_verify",
              "contract_review", "contract_freeze"}
    stages.update(MAIN_STAGES[MAIN_STAGES.index("migration_inventory"):])
    if include_diagnosis:
        stages.add("contract_diagnose")
    return stages


def invalidate_contract_tail(app, *, include_diagnosis=False):
    """Clear stage and scoped task projections, never history or spent budgets."""
    stages = contract_repair_tail(include_diagnosis=include_diagnosis)
    effective = app.setdefault("effective", {})
    removed = {key for key, value in effective.items()
               if key in stages or value.get("stage_id") in stages}
    for key in removed:
        effective.pop(key)
    for field in ("early_failures", "format_retries"):
        projection = app.setdefault(field, {})
        for key in tuple(projection):
            if key in removed or key.split(".", 1)[0] in stages:
                projection.pop(key)
    return sorted(removed)
