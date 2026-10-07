"""Stage-specific final report contracts for the v17 agent dialogue.

The schemas describe the document which an existing consumer already reads.
They are presentation contracts only: callers must retain malformed output as a
diagnostic under workflow v17 rather than turning schema conformance into a
business gate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .author_contracts import characterization_evidence_schema


# Keep the wire contract structural.  Empty or incomplete report content is a
# diagnostic under workflow v17, so the output schema must not make the model
# invent evidence merely to satisfy transport validation.
TEXT = {"type": "string"}


def _array(items: Mapping[str, Any], *, nonempty: bool = False) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "array", "items": dict(items)}
    # ``nonempty`` documents the legacy consumer's semantic expectation.  It
    # deliberately does not become a structured-output gate in workflow v17.
    return value


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    """Build the strict object form accepted by structured-output servers."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(properties),
        "additionalProperties": False,
    }


def _strings(*, nonempty: bool = False) -> dict[str, Any]:
    return _array(TEXT, nonempty=nonempty)


def _enum(*values: str) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


EVIDENCE_SOURCE_SCHEMA = _object({
    "source": TEXT,
    "locator": TEXT,
    "supports": TEXT,
})

COMMON_REVIEW_SCHEMA = _object({
    "verdict": _enum("approved", "rejected"),
    "findings": _strings(),
    "report": TEXT,
})

V31_CONTRACT_REVIEW_SCHEMA = _object({
    **COMMON_REVIEW_SCHEMA["properties"],
    "assertion_reviews": _array(_object({
        "assertion_id": TEXT,
        "source_anchor": _object({
            "path": TEXT,
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        }),
        "status": _enum("supported", "unsupported", "ambiguous"),
        "reasoning": TEXT,
    })),
})

PREPARATION_SCHEMA = _object({
    "schema_version": {"type": "integer", "enum": [1]},
    "source_loader_version": TEXT,
    "source_java": TEXT,
    "version_evidence": _array(_object({
        "field": _enum("source_loader_version", "source_java"),
        "path": TEXT,
        "line": {"type": "integer", "minimum": 1},
        "value": TEXT,
    }), nonempty=True),
    "unresolved_dependencies": _strings(),
})

USAGE_LOCATION_SCHEMA = _object({
    "path": TEXT,
    "line": {"type": "integer", "minimum": 1},
    # Use an empty string when the source line has no useful symbol binding.
    "symbol": {"anyOf": [TEXT, {"type": "null"}]},
})

MOD_ANALYSIS_SCHEMA = _object({
    "schema_version": {"type": "integer", "enum": [2]},
    "candidates": _array(_object({
        "skill": TEXT,
        "index": {"type": "integer", "minimum": 0},
        "status": _enum("import_only", "confirmed", "candidate", "false_positive", "unknown"),
        "evidence": _strings(nonempty=True),
    })),
    "gap_assessments": _array(_object({
        "entry_id": TEXT,
        "gap_id": TEXT,
        "skill": TEXT,
        "index": {"type": "integer", "minimum": 0},
        "applicable": {"type": "boolean"},
        "status": _enum("resolved", "not_applicable", "unresolved"),
        "kind": _enum("knowledge", "verification"),
        "question": TEXT,
        "existing_answer": TEXT,
        "missing_information": TEXT,
        "usage_locations": _array(USAGE_LOCATION_SCHEMA),
        "resolution_stage": TEXT,
        "closure_criteria": _strings(),
        "evidence": _strings(nonempty=True),
        "affected_tasks": _strings(),
        "new_entry": {"type": "boolean"},
        "applicability_disputed": {"type": "boolean"},
        "checked_scope": _strings(),
    })),
    "manual_checks": _strings(),
    "behavior_verification": _strings(),
})

def _check_schema(check_type: str, **fields: Mapping[str, Any]) -> dict[str, Any]:
    return _object({"id": TEXT, "type": _enum(check_type),
                    "acceptance": _strings(), **fields})


CHECK_SCHEMA = {"anyOf": [
    *[_check_schema(check_type, path=TEXT) for check_type in
      ("file_exists", "json_valid", "python_syntax", "contract_schema")],
    _check_schema("gradle_tasks", tasks=_strings()),
    _check_schema("gradle_regression", tasks=_strings(), reports=_strings()),
]}

EXECUTION_TASK_SCHEMA = _object({
    "id": TEXT,
    "kind": _enum("prepare", "coder", "deferred"),
    "objective": TEXT,
    "dependencies": _strings(),
    "owned_paths": _strings(),
    "acceptance": _strings(),
    "validation_checks": _array(CHECK_SCHEMA),
    "blocked_by_gaps": _strings(),
    "complexity": _enum("simple", "complex"),
})

EXECUTION_PLAN_SCHEMA = _object({
    "tasks": _array(EXECUTION_TASK_SCHEMA, nonempty=True),
})

INDEPENDENT_TEST_SCHEMA = _object({
    "id": TEXT,
    "behavior_ids": _strings(nonempty=True),
    "sources": _strings(nonempty=True),
    "task": TEXT,
    "report_paths": _strings(nonempty=True),
})

STATIC_TEST_EVIDENCE_SCHEMA = _object({
    "behavior_id": TEXT,
    "reason": TEXT,
    "evidence": _strings(nonempty=True),
    "acceptance_gates": {
        "type": "array", "items": {"type": "string", "enum": ["client_smoke"]},
        "minItems": 1, "maxItems": 1,
    },
})

def _independent_test_suite_schema(command: Any) -> dict[str, Any]:
    scope = getattr(command, "payload", {}).get("regression_scope")
    static_only = (isinstance(scope, Mapping)
                   and scope.get("runtime_behavior_ids") == []
                   and bool(scope.get("static_behavior_ids")))
    if static_only:
        return _object({
            "schema_version": {"type": "integer", "enum": [1]},
            "tests": {"type": "array", "items": INDEPENDENT_TEST_SCHEMA, "maxItems": 0},
            "static_behavior_ids": _strings(nonempty=True),
            "static_evidence": _array(STATIC_TEST_EVIDENCE_SCHEMA, nonempty=True),
        })
    return _object({
        "schema_version": {"type": "integer", "enum": [1]},
        "init_script": TEXT,
        "tests": _array(INDEPENDENT_TEST_SCHEMA, nonempty=True),
        "static_behavior_ids": _strings(),
        "static_evidence": _array(STATIC_TEST_EVIDENCE_SCHEMA),
    })


# The exported default is the ordinary runtime-suite shape. ``report_contract``
# specializes it for a host-declared static-only scope.
INDEPENDENT_TEST_SUITE_SCHEMA = _independent_test_suite_schema(
    type("_DefaultCommand", (), {"payload": {}})())


_CHARACTERIZATION_PROTOCOL = characterization_evidence_schema(workflow_version=31)

BEHAVIOR_SCHEMA = _object({
    "id": TEXT,
    "source_evidence": TEXT,
    "preconditions": _strings(),
    "action": _strings(),
    "assertions": _strings(),
    "side": _enum("client", "server", "both", "shared"),
    "test_mapping": _strings(),
    "assertion_contracts": _array(_CHARACTERIZATION_PROTOCOL["assertion_contract_schema"]),
})

_EVIDENCE_DECLARATION_COMMON = {
    "path": TEXT,
    "runtime_operations": _strings(),
}

RUNTIME_EVIDENCE_DECLARATION_SCHEMA = _object({
    **_EVIDENCE_DECLARATION_COMMON,
    "evidence_kind": _enum("runtime"),
    "executor": _CHARACTERIZATION_PROTOCOL["declaration_schemas"]["runtime"]["properties"]["executor"],
    "test_source_files": _strings(),
    "result_identity": _CHARACTERIZATION_PROTOCOL["result_identity_schema"],
})

CHARACTERIZATION_SCHEMA = _object({
    "schema_version": {"type": "integer", "enum": [1]},
    "contract_id": TEXT,
    "generator_id": TEXT,
    "baseline_gradle_tasks": _strings(),
    "baseline_evidence_files": _strings(),
    # JSON object keys cannot be data-dependent in a strict output schema.
    # The dialogue materializer converts this finite wire array to the legacy
    # ``test_evidence[test_id] = declaration`` mapping.
    "test_evidence": _array(_object({
        "test_id": TEXT,
        "declaration": RUNTIME_EVIDENCE_DECLARATION_SCHEMA,
    })),
    "behaviors": _array(BEHAVIOR_SCHEMA),
})

VERIFICATION_REQUIREMENT_SCHEMA = _object({
    "gap_id": TEXT,
    "research_gap_id": TEXT,
    "due_stage": _enum("target_build", "test_execute", "client_smoke"),
    "closure_criteria": _strings(nonempty=True),
})

RESEARCH_DISPOSITION_SCHEMA = _object({
    "gap_id": TEXT,
    "project_status": _enum("resolved", "not_applicable", "unresolved"),
    "evidence_artifact_ids": _strings(nonempty=True),
})

KNOWLEDGE_ENTRY_SCHEMA = _object({
    "id": TEXT,
    "category": TEXT,
    "summary": TEXT,
    "applicability": TEXT,
    "migration": TEXT,
    "compat": TEXT,
    "verification": TEXT,
    "evidence": _array(EVIDENCE_SOURCE_SCHEMA, nonempty=True),
})


def _research_review_schema(command: Any) -> dict[str, Any]:
    raw_kinds = getattr(command, "payload", {}).get("research_kinds", ["platform", "java"])
    kinds = [kind for kind in ("platform", "java")
             if not isinstance(raw_kinds, Sequence) or isinstance(raw_kinds, str) or kind in raw_kinds]
    entries = _object({kind: _array(KNOWLEDGE_ENTRY_SCHEMA) for kind in kinds})
    return _object({
        "verdict": _enum("approved", "rejected"),
        "findings": _strings(),
        "report": TEXT,
        "approved_generic_knowledge_entries": entries,
        "approved_gap_resolutions": _array(RESEARCH_DISPOSITION_SCHEMA),
        "verification_requirements": _array(VERIFICATION_REQUIREMENT_SCHEMA),
    })


GAP_ALTERNATIVE_REQUIREMENT_SCHEMA = _object({
    "id": TEXT,
    "closure_criteria": _strings(nonempty=True),
    "resolution_stage": _enum("target_build", "test_execute", "client_smoke"),
})

GAP_ALTERNATIVE_SCHEMA = _object({
    "gap_id": TEXT,
    "action": _enum("existing_answer", "compatibility_layer", "alternative_api",
                    "implementation_change", "wait_admin"),
    "alternative_id": TEXT,
    "rationale": TEXT,
    "evidence_artifact_ids": _strings(),
    "affected_tasks": _strings(),
    "verification_requirements": _array(GAP_ALTERNATIVE_REQUIREMENT_SCHEMA),
})

GAP_PLAN_REVIEW_SCHEMA = _object({
    "verdict": _enum("approved", "rejected"),
    "findings": _strings(),
    "report": TEXT,
    "approved_gap_resolutions": _array(GAP_ALTERNATIVE_SCHEMA),
    # Existing consumers accept partial updates. A complete normalized task is
    # also a valid update and gives structured-output servers a finite shape.
    "approved_task_updates": _array(EXECUTION_TASK_SCHEMA),
})

GAP_RESOLUTION_SCHEMA = _object({
    "gap_id": TEXT,
    "status": _enum("resolved", "not_applicable", "unresolved", "bypassed"),
    "evidence": _strings(nonempty=True),
    "artifact_ids": _strings(),
})

GAP_REVIEW_SCHEMA = _object({
    "verdict": _enum("approved", "rejected"),
    "findings": _strings(),
    "report": TEXT,
    "gap_resolutions": _array(GAP_RESOLUTION_SCHEMA),
})

_SUPERVISOR_COMMON = {
    "schema_version": {"type": "integer", "enum": [1]},
    "reason": TEXT,
    "evidence_execution_ids": _strings(nonempty=True),
    "process_improvements": _strings(),
}

SUPERVISOR_SCHEMA = _object({
    **_SUPERVISOR_COMMON,
    "decision": _enum("continue", "targeted_fix", "replan", "pause"),
    # Strict structured outputs require every property.  The dialogue
    # materializer removes null members, and removes the empty intervention,
    # before passing the document to the existing conditional validator.
    "intervention": _object({
        "prompt": {"type": ["string", "null"]},
        "task_ids": {"anyOf": [_strings(), {"type": "null"}]},
        "stage": {"type": ["string", "null"]},
        "profile": {"type": ["string", "null"]},
    }),
})

SKILL_RULE_SCHEMA = _object({
    "id": TEXT,
    "category": TEXT,
    "summary": TEXT,
    "recommendation": TEXT,
    "verification": TEXT,
    "evidence": _array(EVIDENCE_SOURCE_SCHEMA, nonempty=True),
    "files": _strings(nonempty=True),
    "pattern": TEXT,
    "flags": _array(_enum("MULTILINE", "DOTALL", "IGNORECASE", "ASCII")),
    "examples": _object({
        "match": _strings(nonempty=True),
        "no_match": _strings(nonempty=True),
    }),
})

SKILL_MANUAL_CHECK_SCHEMA = _object({
    "id": TEXT,
    "category": TEXT,
    "summary": TEXT,
    "recommendation": TEXT,
    "verification": TEXT,
    "evidence": _array(EVIDENCE_SOURCE_SCHEMA, nonempty=True),
})


def _version_schema(kind: str) -> dict[str, Any]:
    fields = ({"java": TEXT} if kind == "java" else
              {"minecraft": TEXT, "loader": TEXT, "loader_version": TEXT})
    return _object(fields)


def _skill_generation_schema(kind: str) -> dict[str, Any]:
    version = _version_schema(kind)
    return _object({
        "outputs": _object({
            "SKILL.md": TEXT,
            "metadata.json": _object({
                "schema_version": {"type": "integer", "enum": [1]},
                "kind": {"type": "string", "enum": [kind]},
                "skill_id": TEXT,
                "source": version,
                "target": version,
                "generator_id": TEXT,
                "generator": _object({"model": TEXT, "reasoning_effort": TEXT}),
                **({"requires_java": {"anyOf": [
                    {"type": "null"}, _object({"source": TEXT, "target": TEXT})
                ]}} if kind == "platform" else {}),
            }),
            "rules.json": _object({
                "schema_version": {"type": "integer", "enum": [1]},
                "source": version,
                "target": version,
                "known_gaps": _array(_object({"id": TEXT, "summary": TEXT})),
                "rules": _array(SKILL_RULE_SCHEMA),
                "manual_checks": _array(SKILL_MANUAL_CHECK_SCHEMA),
            }),
            "coverage.json": _object({
                "areas": _array(_object({
                    "id": TEXT,
                    "status": _enum("verified", "manual", "gap", "verified-no-change"),
                    "rule_ids": _strings(),
                    "rationale": TEXT,
                    "evidence": _strings(),
                }), nonempty=True),
            }),
            "evidence.json": _object({
                "sources": _array(EVIDENCE_SOURCE_SCHEMA),
                "limitations": _strings(),
            }),
        }),
    })


_MARKDOWN_PATHS = {
    "background": ".modport/background.md",
    "project_init": ".modport/project-init.md",
    "gap_research": ".modport/gap-research/report.md",
}

_PLANNING_MARKDOWN_STAGES = {
    "migration_inventory", "migration_plan", "parallel_review",
    "contract_diagnose", "contract_repair_plan", "contract_repair_review",
    "target_diagnose", "target_repair_plan", "target_repair_review",
    "goal_prepare",
}

_PLANNING_JSON_STAGES = {
    "migration_tasks", "contract_repair_tasks", "target_repair_tasks",
}


def _required_path(required_paths: Sequence[str], suffix: str) -> str | None:
    return next((path for path in required_paths if isinstance(path, str)
                 and path.endswith(suffix)), None)


def report_contract(command: Any, required_paths: Sequence[str] = ()) -> dict[str, Any]:
    """Return the final-response contract for one business assignment.

    ``output_path`` is workspace-relative. ``None`` means the response remains
    the handler's ``last_message`` or is a multi-document envelope handled by
    the dialogue transport.
    """
    stage = getattr(command, "stage_id", "")
    command_id = getattr(command, "command_id", "")

    if stage == 'behavior_extract':
        from .author_contracts import behavior_requirements_protocol
        return {'schema': behavior_requirements_protocol()['schema'],
                'output_path': '.modport/behavior-requirements.json',
                'instructions': ('Return only the complete source-derived behavior requirements JSON. '
                                 'Read original code without executing it or authoring source tests. '
                                 'Preserve confirmed behavior and assertion IDs.')}
    if stage == 'behavior_review':
        return {'schema': None, 'output_path': '.modport/behavior-review.md',
                'instructions': ('Return the complete source-reading review in Markdown. Findings '
                                 'are diagnostic; no approval marker or source execution is required.')}

    if stage == 'supervisor' and 'watchdog_incident' in getattr(command, 'payload', {}):
        from .watchdog_supervisor import watchdog_supervisor_schema
        return {'schema': watchdog_supervisor_schema(), 'output_path': None,
                'instructions': 'Return only the host-incident-bound watchdog decision JSON document.'}

    if stage == 'supervisor' and 'progress_supervision' in getattr(command, 'payload', {}):
        from .progress_supervisor import progress_supervisor_schema
        return {'schema': progress_supervisor_schema(), 'output_path': None,
                'instructions': 'Return only the execution-bound progress supervisor decision JSON document.'}

    if stage == 'supervisor' and getattr(command, 'options', {}).get('workflow_version', 0) >= 26:
        return {'schema': None, 'output_path': None,
                'instructions': 'Edit the supplied goal Markdown files when needed and return your causal investigation in Markdown.'}

    # Reviewer rework edits the existing full document. Publishing a fresh-draft
    # reply here would overwrite repaired metadata and source provenance,
    # whether or not this child carries the original handoff reference.
    if (stage in {"contract_draft", "contract_revise"}
            and getattr(command, "options", {}).get("workflow_version", 0) >= 21
            and isinstance(getattr(command, "payload", {}).get("reviewer_rework"), dict)):
        return {"schema": None, "output_path": None,
                "transform": "characterization_in_place",
                "instructions": ("Edit the existing .modport/functional-contract.json in place, "
                                 "preserving all existing identities, source links, evidence, "
                                 "and other fields except those the rework actually changes. "
                                 "Keep test_evidence in the file as a mapping keyed by test ID. "
                                 "Return a concise account of the edits and remaining gaps; "
                                 "the host snapshots the file itself as the contract candidate.")}

    if stage in _MARKDOWN_PATHS:
        return {"schema": None, "output_path": _MARKDOWN_PATHS[stage],
                "instructions": "Return the complete Markdown report as the final response."}
    if stage in _PLANNING_MARKDOWN_STAGES:
        return {"schema": None, "output_path": None,
                "instructions": "Return the complete Markdown planning or handoff document as the final response."}
    if stage in _PLANNING_JSON_STAGES:
        return {"schema": EXECUTION_PLAN_SCHEMA, "output_path": None,
                "instructions": "Return only the execution-task JSON document consumed by the host."}
    if stage == "preparation":
        return {"schema": PREPARATION_SCHEMA, "output_path": ".modport/preparation.json",
                "instructions": "Return only the complete preparation JSON document."}
    if stage == "mod_analysis":
        return {"schema": MOD_ANALYSIS_SCHEMA, "output_path": ".modport/mod-analysis.json",
                "instructions": "Return only the complete mod-analysis JSON document; write the companion Markdown explanation during execution."}
    if stage == "contract_draft":
        return {"schema": CHARACTERIZATION_SCHEMA,
                "output_path": ".modport/functional-contract.json",
                "transform": "characterization",
                "instructions": ("Write the executable characterization harness during execution, then return only the complete functional-contract JSON wire document. "
                                 "Include each assertion_contracts source anchor and each runtime declaration's exact JUnit result_identity from the supplied evidence protocol. "
                                 "Keep the on-disk test_evidence as a mapping keyed by test ID for in-turn verification. "
                                 "Represent test_evidence in the final response as an array of {test_id,declaration}; the host converts it to the test-ID mapping without changing values. Unimplemented cases must remain honestly skipped or unverified.")}
    if stage in {"contract_review", "code_review"}:
        path = ".modport/contract-review.json" if stage == "contract_review" else ".modport/code-review.json"
        v31_contract_review = (
            stage == "contract_review"
            and getattr(command, "options", {}).get("workflow_version", 0) >= 31
        )
        schema = V31_CONTRACT_REVIEW_SCHEMA if v31_contract_review else COMMON_REVIEW_SCHEMA
        instructions = (
            "Return only the contract review JSON document with a complete assertion_reviews array."
            if v31_contract_review else
            "Return only the common review JSON document; the host supplies identity and provenance."
        )
        return {"schema": schema, "output_path": path,
                "instructions": instructions}
    if stage == "test_design":
        return {"schema": _independent_test_suite_schema(command),
                "output_path": ".modport/independent-tests/suite.json",
                "instructions": "Return only the complete independent-test suite JSON document after writing its source files."}
    if stage == "test_review":
        return {"schema": COMMON_REVIEW_SCHEMA, "output_path": None,
                "instructions": "Return only the common review JSON document as the final response."}
    if stage in {"research_review", "admin_review"}:
        return {"schema": _research_review_schema(command),
                "output_path": ".modport/" + stage.replace("_", "-") + ".json",
                "instructions": "Return only the research disposition JSON document."}
    if stage == "gap_plan":
        # Keep the historical filename because existing artifact routing expects it.
        return {"schema": None, "output_path": ".modport/gap-plan.json",
                "instructions": "Return the complete gap proposal as Markdown; the .json suffix is a historical filename."}
    if stage == "gap_plan_review":
        return {"schema": GAP_PLAN_REVIEW_SCHEMA, "output_path": ".modport/gap-plan-review.json",
                "instructions": "Return only the independent gap-plan review JSON document."}
    if stage == "gap_review":
        return {"schema": GAP_REVIEW_SCHEMA, "output_path": ".modport/gap-review.json",
                "instructions": "Return only the final gap-resolution review JSON document."}
    if stage == "gate_handoff":
        path = ".modport/gate-handoffs/" + str(command_id) + ".md"
        return {"schema": None, "output_path": path,
                "instructions": "Return the complete downstream handoff as Markdown."}
    if stage in {"coder", "agent_rework"}:
        task = getattr(command, "payload", {}).get("development_task", {})
        task_id = task.get("id") if isinstance(task, Mapping) else None
        path = f".modport/goal-reports/{task_id}.json" if isinstance(task_id, str) and task_id else None
        return {"schema": None, "output_path": path,
                "instructions": ("Return the complete coder handoff as Markdown. The reserved .json suffix is historical; "
                                 "describe actual changes, evidence, failures, and remaining work without claiming acceptance.")}
    if stage in {"platform_diff", "java_diff"}:
        kind = "platform" if stage == "platform_diff" else "java"
        return {"schema": _skill_generation_schema(kind), "output_path": None,
                "output_paths": ("SKILL.md", "metadata.json", "rules.json",
                                 "coverage.json", "evidence.json"),
                "transform": "skill_bundle",
                "instructions": ("Return only the multi-document skill envelope. The host materializes each allowed outputs key "
                                 "at that exact workspace-relative path and restores its trusted scanner.")}
    if stage in {"platform_skill_review", "java_skill_review"}:
        return {"schema": COMMON_REVIEW_SCHEMA, "output_path": "review.json",
                "instructions": "Return only the common skill-review JSON document."}
    if stage == "supervisor":
        return {"schema": SUPERVISOR_SCHEMA, "output_path": None,
                "transform": "supervisor",
                "instructions": ("Return only the supervisor decision JSON document. Use null for unused intervention fields; "
                                 "the host removes null fields before applying the existing conditional validator.")}

    markdown = _required_path(required_paths, ".md")
    if markdown is not None:
        return {"schema": None, "output_path": markdown,
                "instructions": "Return the complete Markdown document requested by the assignment."}
    return {"schema": None, "output_path": None,
            "instructions": "Return the complete final report in the format requested by the assignment."}


__all__ = [
    "COMMON_REVIEW_SCHEMA", "V31_CONTRACT_REVIEW_SCHEMA",
    "EXECUTION_PLAN_SCHEMA", "GAP_PLAN_REVIEW_SCHEMA",
    "GAP_REVIEW_SCHEMA", "INDEPENDENT_TEST_SUITE_SCHEMA", "MOD_ANALYSIS_SCHEMA",
    "PREPARATION_SCHEMA", "CHARACTERIZATION_SCHEMA", "SUPERVISOR_SCHEMA", "report_contract",
]
