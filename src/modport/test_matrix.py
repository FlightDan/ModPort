"""Pure validation and projection helpers for migration test selection."""
from copy import deepcopy
from collections.abc import Mapping
from typing import Any


_DECISIONS = frozenset({"keep", "merge", "drop", "repair", "source_defect"})
_UNBOUND_CATEGORIES = frozenset({"test_infrastructure", "assertion_invalid"})


def matrix_protocol() -> dict[str, Any]:
    """Return the shared producer and assessor schema for test matrices."""
    text = {"type": "string", "minLength": 1, "pattern": r"\S"}
    text_list = {"type": "array", "items": {"type": "string"}}
    note_list = {"type": "array", "items": text}
    id_list = {"type": "array", "uniqueItems": True, "items": text}
    case_schema = {
        "type": "object",
        "required": ["test_id", "behavior_id", "entry_point", "action", "conditions", "assertion_ids"],
        "properties": {
            "test_id": text,
            "behavior_id": text,
            "entry_point": text,
            "action": text,
            "conditions": text_list,
            "assertion_ids": id_list,
        },
        "additionalProperties": True,
    }
    assessment_schema = {
        "type": "object",
        "required": ["schema_version", "decisions"],
        "properties": {
            "schema_version": {"const": 1},
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["test_id", "decision", "reason"],
                    "properties": {
                        "test_id": text,
                        "decision": {"enum": sorted(_DECISIONS)},
                        "reason": text,
                        "replacement_test_ids": id_list,
                        "evidence": text_list,
                        "defect_assertion_ids": id_list,
                    },
                    "additionalProperties": True,
                },
            },
        },
        "additionalProperties": True,
    }
    return {
        "schema_version": 1,
        "matrix_schema": {
            "type": "object",
            "required": ["schema_version", "cases"],
            "properties": {
                "schema_version": {"const": 1},
                "cases": {"type": "array", "items": case_schema},
                "exploration_notes": note_list,
            },
            "additionalProperties": True,
        },
        "assessment_schema": assessment_schema,
        "guidance": [
            "Find likely test projects from source layout, build files and commands, test tasks, actions, events, resources, state transitions, and lifecycle entry points; do not assume tests live at the repository root.",
            "Cover important risks and operation sequences, and use property-style cases where they expose invariants across inputs or state. Record the actual entry point, action, conditions, and observable assertion for each case.",
            "Each selected case must own or reconstruct its required setup and teardown. Keep an operation sequence in one traceable case, or declare explicit fixture dependencies so dropping or merging another test cannot remove required state preparation. Declare setup prerequisites as Gradle task dependencies of the selected test task, not as unrelated baseline tasks.",
            "Record each contract test id once with its owning behavior, real entry point, action, relevant conditions, and linked assertion ids. Preserve useful discovery notes as extra case fields or optional top-level exploration_notes.",
            "Run the original mod's declared test path before assessing migration cases. Record only observed case results; a missing result, compile failure, or infrastructure error does not establish an original-mod defect.",
            "For each case choose keep, merge, drop, repair, or source_defect and explain the decision. Supply evidence for source_defect decisions; the host accepts those only when a fresh bound baseline case result supports them. The observed test may pass while demonstrating a defect or fail while exposing one; explain the concrete comparison to the expected behavior.",
            "For source_defect, list the non-empty defect_assertion_ids subset demonstrated by this case. A single linked assertion may be inferred; for a case with multiple assertions, identify defective assertions explicitly or keep the case with a diagnostic. Preserve other assertions as migration coverage gaps when no selected test covers them.",
            "Keep and repair cases remain executable. A repair decision is a diagnostic pending explicit rework and fresh assessment; it does not schedule repair automatically. Merge and drop cases are excluded, while assertions without selected tests remain explicit coverage gaps. A source defect is recorded with original assertion and source-anchor evidence and excluded from migration tests.",
        ],
    }


def validate_matrix(matrix: Mapping[str, Any], contract: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and copy a matrix against contract test and assertion links."""
    index = _contract_index(contract)
    if not isinstance(matrix, Mapping):
        raise ValueError("test matrix must be an object")
    if type(matrix.get("schema_version")) is not int or matrix.get("schema_version") != 1:
        raise ValueError("test matrix schema_version must be 1")
    cases = matrix.get("cases")
    if not isinstance(cases, list):
        raise ValueError("test matrix cases must be an array")
    if "exploration_notes" in matrix:
        exploration_notes = matrix["exploration_notes"]
        if not isinstance(exploration_notes, list) or any(not _nonblank(item) for item in exploration_notes):
            raise ValueError("test matrix exploration_notes must be an array of nonblank strings")

    normalized = deepcopy(dict(matrix))
    normalized_cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, raw in enumerate(cases):
        if not isinstance(raw, Mapping):
            raise ValueError(f"test matrix case {position} must be an object")
        test_id = _required_text(raw.get("test_id"), f"case {position} test_id")
        if test_id in seen:
            raise ValueError(f"duplicate test id {test_id!r} in test matrix")
        if test_id not in index["test_owner"]:
            raise ValueError(f"unknown test id {test_id!r} in test matrix")
        behavior_id = _required_text(raw.get("behavior_id"), f"case {position} behavior_id")
        if behavior_id not in index["behavior_by_id"]:
            raise ValueError(f"unknown behavior id {behavior_id!r} for test {test_id!r}")
        if index["test_owner"][test_id] != behavior_id:
            raise ValueError(f"test {test_id!r} is linked to the wrong behavior")
        _required_text(raw.get("entry_point"), f"case {position} entry_point")
        _required_text(raw.get("action"), f"case {position} action")
        conditions = raw.get("conditions")
        if not isinstance(conditions, list) or any(not isinstance(item, str) for item in conditions):
            raise ValueError(f"case {test_id!r} conditions must be an array of strings")
        assertion_ids = raw.get("assertion_ids")
        if not isinstance(assertion_ids, list) or any(not _nonblank(item) for item in assertion_ids):
            raise ValueError(f"case {test_id!r} assertion_ids must be an array of nonblank strings")
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError(f"case {test_id!r} has duplicate assertion ids")
        expected = index["assertions_by_test"].get(test_id, [])
        if set(assertion_ids) != set(expected):
            raise ValueError(
                f"case {test_id!r} assertion_ids do not match contract assertion_contracts.test_ids"
            )
        seen.add(test_id)
        case = deepcopy(dict(raw))
        case["conditions"] = list(conditions)
        case["assertion_ids"] = list(expected)
        normalized_cases.append(case)

    missing = [test_id for test_id in index["test_owner"] if test_id not in seen]
    if missing:
        raise ValueError(f"test matrix is missing contract test ids: {', '.join(missing)}")
    normalized["schema_version"] = 1
    normalized["cases"] = normalized_cases
    return normalized


def select_migration_tests(
    contract: Mapping[str, Any],
    matrix: Mapping[str, Any],
    assessment: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a selected suite without mutating any supplied mapping."""
    if not isinstance(contract, Mapping):
        raise ValueError("contract must be an object")
    if not isinstance(baseline_report, Mapping):
        raise ValueError("baseline report must be an object")
    index = _contract_index(contract)
    normalized_matrix = validate_matrix(matrix, contract)
    matrix_ids = {case["test_id"] for case in normalized_matrix["cases"]}
    assertions_by_test = {
        case["test_id"]: set(case["assertion_ids"])
        for case in normalized_matrix["cases"]
    }
    decisions = _validate_assessment(assessment, assertions_by_test)

    excluded_cases: list[dict[str, Any]] = []
    source_defects: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    selected: set[str] = set()
    verified_source_defects: dict[str, list[str]] = {}
    case_results = baseline_report.get("case_results")
    declarations = contract.get("test_evidence")

    for case in normalized_matrix["cases"]:
        test_id = case["test_id"]
        decision = decisions.get(test_id)
        if decision is None:
            selected.add(test_id)
            diagnostics.append(f"No assessment decision for {test_id!r}; kept conservatively.")
            continue
        choice = decision["decision"]
        if choice == "keep":
            selected.add(test_id)
            continue
        if choice == "repair":
            selected.add(test_id)
            diagnostics.append(
                f"Repair was requested for {test_id!r}; kept executable pending explicit rework and fresh assessment."
            )
            continue
        if choice in {"merge", "drop"}:
            excluded_cases.append(_excluded_case(case, decision))
            continue

        defect_assertion_ids = decision.get("defect_assertion_ids")
        if defect_assertion_ids is None:
            if len(case["assertion_ids"]) == 1:
                defect_assertion_ids = list(case["assertion_ids"])
            else:
                selected.add(test_id)
                diagnostics.append(
                    f"Source-defect decision for {test_id!r} omitted defect_assertion_ids for a case with multiple assertions; kept as a migration test."
                )
                continue

        result = case_results.get(test_id) if isinstance(case_results, Mapping) else None
        declaration = declarations.get(test_id) if isinstance(declarations, Mapping) else None
        rejection = _source_defect_rejection(test_id, result, declaration, contract, baseline_report)
        if rejection is not None:
            selected.add(test_id)
            diagnostics.append(
                f"Source-defect decision for {test_id!r} was not accepted ({rejection}); kept as a migration test."
            )
            continue
        verified_source_defects[test_id] = list(defect_assertion_ids)
        excluded_cases.append(_excluded_case(case, decision))
        source_defects.append(_source_defect_record(
            test_id, case, decision, index, result, defect_assertion_ids,
        ))

    selected_test_ids = [case["test_id"] for case in normalized_matrix["cases"]
                         if case["test_id"] in selected]
    omitted_assertions: list[str] = []
    defect_assertions: set[str] = set()
    for assertion_id, assertion in index["assertion_by_id"].items():
        test_ids = assertion["test_ids"]
        if any(test_id in selected for test_id in test_ids):
            continue
        if assertion_id in {
            defect_id for defect_ids in verified_source_defects.values() for defect_id in defect_ids
        }:
            defect_assertions.add(assertion_id)
        else:
            omitted_assertions.append(assertion_id)
            diagnostics.append(
                f"Assertion {assertion_id!r} has no selected test and remains an uncovered migration obligation."
            )

    projected = deepcopy(dict(contract))
    for field in ("behaviors", "entries"):
        if field in contract:
            projected[field] = _project_behaviors(contract[field], selected)

    if isinstance(declarations, Mapping):
        projected["test_evidence"] = {
            test_id: deepcopy(value) for test_id, value in declarations.items() if test_id in selected
        }
    elif "test_evidence" in contract:
        projected["test_evidence"] = {}
    baseline_tasks = contract.get("baseline_gradle_tasks")
    if isinstance(baseline_tasks, list):
        selected_task_names = {
            _normalized_gradle_task(declaration["result_identity"].get("gradle_task"))
            for test_id, declaration in declarations.items()
            if test_id in selected and isinstance(declaration, Mapping)
            and isinstance(declaration.get("result_identity"), Mapping)
        } if isinstance(declarations, Mapping) else set()
        selected_task_names.discard(None)
        projected["baseline_gradle_tasks"] = [
            deepcopy(task) for task in baseline_tasks
            if isinstance(task, str) and _normalized_gradle_task(task) in selected_task_names
        ]
    evidence_files = contract.get("baseline_evidence_files")
    selected_paths = {
        declaration.get("path") for test_id, declaration in declarations.items()
        if test_id in selected and isinstance(declaration, Mapping)
        and isinstance(declaration.get("path"), str)
    } if isinstance(declarations, Mapping) else set()
    if isinstance(evidence_files, list):
        projected["baseline_evidence_files"] = [
            deepcopy(path) for path in evidence_files
            if isinstance(path, str) and path in selected_paths
        ]
    elif "baseline_evidence_files" in contract:
        projected["baseline_evidence_files"] = []

    if not selected_test_ids:
        diagnostics.append(
            "No migration tests remain after planner selection; the empty suite cannot establish acceptance."
        )

    return {
        "migration_contract": projected,
        "selected_test_ids": selected_test_ids,
        "excluded_cases": excluded_cases,
        "source_defects": source_defects,
        "uncovered_assertion_ids": omitted_assertions,
        "diagnostics": diagnostics,
    }


def _contract_index(contract: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(contract, Mapping):
        raise ValueError("contract must be an object")
    behaviors = contract.get("behaviors", contract.get("entries"))
    if not isinstance(behaviors, list):
        raise ValueError("contract behaviors must be an array")
    behavior_by_id: dict[str, Mapping[str, Any]] = {}
    test_owner: dict[str, str] = {}
    assertions_by_test: dict[str, list[str]] = {}
    assertion_by_id: dict[str, Mapping[str, Any]] = {}
    for position, behavior in enumerate(behaviors):
        if not isinstance(behavior, Mapping):
            raise ValueError(f"contract behavior {position} must be an object")
        behavior_id = _required_text(behavior.get("id"), f"contract behavior {position} id")
        if behavior_id in behavior_by_id:
            raise ValueError(f"duplicate contract behavior id {behavior_id!r}")
        behavior_by_id[behavior_id] = behavior
        test_mapping = behavior.get("test_mapping")
        if not isinstance(test_mapping, list) or not test_mapping:
            raise ValueError(f"contract behavior {behavior_id!r} test_mapping must be a non-empty array")
        for test_id in test_mapping:
            _required_text(test_id, f"contract behavior {behavior_id!r} test id")
            if test_id in test_owner:
                raise ValueError(f"duplicate contract test id {test_id!r}")
            test_owner[test_id] = behavior_id
            assertions_by_test[test_id] = []
        raw_assertions = behavior.get("assertion_contracts", [])
        if not isinstance(raw_assertions, list):
            raise ValueError(f"behavior {behavior_id!r} assertion_contracts must be an array")
        for position, assertion in enumerate(raw_assertions):
            if not isinstance(assertion, Mapping):
                raise ValueError(f"behavior {behavior_id!r} assertion {position} must be an object")
            assertion_id = _required_text(assertion.get("assertion_id"), "assertion_id")
            if assertion_id in assertion_by_id:
                raise ValueError(f"duplicate contract assertion id {assertion_id!r}")
            test_ids = assertion.get("test_ids")
            if not isinstance(test_ids, list) or not test_ids:
                raise ValueError(f"assertion {assertion_id!r} test_ids must be a non-empty array")
            if any(not _nonblank(test_id) for test_id in test_ids):
                raise ValueError(f"assertion {assertion_id!r} has a non-string or blank test id")
            if len(test_ids) != len(set(test_ids)):
                raise ValueError(f"assertion {assertion_id!r} has duplicate test ids")
            for test_id in test_ids:
                if test_owner.get(test_id) != behavior_id:
                    raise ValueError(
                        f"assertion {assertion_id!r} references test {test_id!r} outside its behavior"
                    )
                assertions_by_test[test_id].append(assertion_id)
            assertion_by_id[assertion_id] = assertion
    return {
        "behaviors": behaviors,
        "behavior_by_id": behavior_by_id,
        "test_owner": test_owner,
        "assertions_by_test": assertions_by_test,
        "assertion_by_id": assertion_by_id,
    }


def _validate_assessment(
    assessment: Mapping[str, Any], known_assertions_by_test: Mapping[str, set[str]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(assessment, Mapping):
        raise ValueError("test assessment must be an object")
    if type(assessment.get("schema_version")) is not int or assessment.get("schema_version") != 1:
        raise ValueError("test assessment schema_version must be 1")
    rows = assessment.get("decisions")
    if not isinstance(rows, list):
        raise ValueError("test assessment decisions must be an array")
    decisions: dict[str, dict[str, Any]] = {}
    for position, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise ValueError(f"assessment decision {position} must be an object")
        test_id = _required_text(raw.get("test_id"), f"assessment decision {position} test_id")
        if test_id not in known_assertions_by_test:
            raise ValueError(f"unknown test id {test_id!r} in assessment")
        if test_id in decisions:
            raise ValueError(f"duplicate assessment decision for test id {test_id!r}")
        decision = raw.get("decision")
        if not isinstance(decision, str) or decision not in _DECISIONS:
            raise ValueError(f"assessment decision for {test_id!r} has an unsupported decision")
        reason = _required_text(raw.get("reason"), f"assessment decision for {test_id!r} reason")
        replacements = raw.get("replacement_test_ids", [])
        if not isinstance(replacements, list) or any(not _nonblank(item) for item in replacements):
            raise ValueError(f"assessment decision for {test_id!r} replacement_test_ids must be an array of ids")
        if len(replacements) != len(set(replacements)):
            raise ValueError(f"assessment decision for {test_id!r} has duplicate replacement test ids")
        unknown_replacements = [item for item in replacements if item not in known_assertions_by_test]
        if unknown_replacements:
            raise ValueError(
                f"assessment decision for {test_id!r} references unknown replacement ids: "
                + ", ".join(unknown_replacements)
            )
        evidence = raw.get("evidence", [])
        if not isinstance(evidence, list) or any(not _nonblank(item) for item in evidence):
            raise ValueError(f"assessment decision for {test_id!r} evidence must be an array of nonblank strings")
        if decision == "source_defect" and not evidence:
            raise ValueError(f"source_defect decision for {test_id!r} requires non-empty evidence")
        defect_assertion_ids = raw.get("defect_assertion_ids")
        if defect_assertion_ids is not None:
            if decision != "source_defect":
                raise ValueError(f"defect_assertion_ids for {test_id!r} requires a source_defect decision")
            if (not isinstance(defect_assertion_ids, list) or not defect_assertion_ids
                    or any(not _nonblank(item) for item in defect_assertion_ids)):
                raise ValueError(f"source_defect decision for {test_id!r} defect_assertion_ids must be a non-empty array")
            if len(defect_assertion_ids) != len(set(defect_assertion_ids)):
                raise ValueError(f"source_defect decision for {test_id!r} has duplicate defect_assertion_ids")
            unexpected = [item for item in defect_assertion_ids
                          if item not in known_assertions_by_test[test_id]]
            if unexpected:
                raise ValueError(
                    f"source_defect decision for {test_id!r} references assertions not linked to its case: "
                    + ", ".join(unexpected)
                )
        elif decision == "source_defect" and not known_assertions_by_test[test_id]:
            raise ValueError(f"source_defect decision for {test_id!r} has no linked assertions")
        row = deepcopy(dict(raw))
        row.update(test_id=test_id, decision=decision, reason=reason,
                   replacement_test_ids=list(replacements), evidence=list(evidence))
        if defect_assertion_ids is not None:
            row["defect_assertion_ids"] = list(defect_assertion_ids)
        decisions[test_id] = row
    return decisions


def _source_defect_rejection(
    test_id: str,
    result: Any,
    declaration: Any,
    contract: Mapping[str, Any],
    baseline_report: Mapping[str, Any],
) -> str | None:
    if not isinstance(result, Mapping):
        return "no executed baseline case result exists"
    status = result.get("status")
    outcome = result.get("test_outcome")
    if (not isinstance(status, str) or status not in {"passed", "failed"}
            or not isinstance(outcome, str) or outcome not in {"passed", "failed"}
            or status != outcome):
        return "baseline status and test_outcome are absent, unexecuted, or inconsistent"
    category = result.get("category")
    if not isinstance(category, str):
        return "baseline result category is missing"
    if category in _UNBOUND_CATEGORIES:
        return f"baseline result category is {category!r}"
    if not isinstance(declaration, Mapping):
        return "contract test declaration is missing"
    expected_identity = declaration.get("result_identity")
    observed_identity = result.get("result_identity")
    if (not isinstance(expected_identity, Mapping) or not isinstance(observed_identity, Mapping)
            or dict(expected_identity) != dict(observed_identity)):
        return "baseline result_identity does not match the contract declaration"
    if result.get("candidate_unchanged") is not True:
        return "baseline candidate was not confirmed unchanged"
    source_fingerprint = contract.get("source_fingerprint")
    if _nonblank(source_fingerprint):
        if result.get("source_commit") != source_fingerprint:
            return "baseline case source_commit does not match contract source_fingerprint"
        report_commit = baseline_report.get("source_commit")
        if report_commit is not None and report_commit != source_fingerprint:
            return "baseline report source_commit does not match contract source_fingerprint"
    report_nonce = baseline_report.get("execution_nonce")
    if report_nonce is not None:
        if not _nonblank(report_nonce) or result.get("execution_nonce") != report_nonce:
            return "baseline case execution_nonce does not match the report"
    if result.get("test_id", test_id) != test_id:
        return "baseline case result is bound to a different test id"
    return None


def _excluded_case(case: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "test_id": decision["test_id"],
        "decision": decision["decision"],
        "reason": decision["reason"],
        "replacement_test_ids": deepcopy(decision.get("replacement_test_ids", [])),
        "evidence": deepcopy(decision.get("evidence", [])),
        "case": deepcopy(dict(case)),
    }


def _source_defect_record(
    test_id: str,
    case: Mapping[str, Any],
    decision: Mapping[str, Any],
    index: Mapping[str, Any],
    result: Mapping[str, Any],
    defect_assertion_ids: list[str],
) -> dict[str, Any]:
    behavior = index["behavior_by_id"][case["behavior_id"]]
    assertions = [
        deepcopy(index["assertion_by_id"][assertion_id])
        for assertion_id in case["assertion_ids"]
    ]
    source_anchors = {
        assertion["assertion_id"]: deepcopy(assertion["source_anchor"])
        for assertion in assertions if isinstance(assertion.get("source_anchor"), Mapping)
    }
    return {
        "test_id": test_id,
        "behavior_id": case["behavior_id"],
        "case": deepcopy(dict(case)),
        "original_behavior": deepcopy(dict(behavior)),
        "assertion_ids": list(case["assertion_ids"]),
        "defect_assertion_ids": list(defect_assertion_ids),
        "reason": decision["reason"],
        "planner_evidence": deepcopy(decision.get("evidence", [])),
        "original_behavior_assertions": deepcopy(behavior.get("assertions", [])),
        "original_source_evidence": deepcopy(behavior.get("source_evidence")),
        "assertions": assertions,
        "source_anchors": source_anchors,
        "case_evidence": deepcopy(dict(result)),
    }


def _project_behaviors(raw_behaviors: Any, selected: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw_behaviors, list):
        return []
    projected: list[dict[str, Any]] = []
    for raw in raw_behaviors:
        if not isinstance(raw, Mapping):
            continue
        behavior = deepcopy(dict(raw))
        test_mapping = raw.get("test_mapping", [])
        if not isinstance(test_mapping, list):
            test_mapping = []
        retained_tests = [test_id for test_id in test_mapping if test_id in selected]
        if not retained_tests:
            continue
        behavior["test_mapping"] = retained_tests
        if isinstance(raw.get("assertion_contracts"), list):
            retained_assertions: list[dict[str, Any]] = []
            for raw_assertion in raw["assertion_contracts"]:
                if not isinstance(raw_assertion, Mapping):
                    continue
                assertion = deepcopy(dict(raw_assertion))
                test_ids = assertion.get("test_ids", [])
                test_ids = [test_id for test_id in test_ids if test_id in selected] \
                    if isinstance(test_ids, list) else []
                if not test_ids:
                    continue
                assertion["test_ids"] = test_ids
                retained_assertions.append(assertion)
            behavior["assertion_contracts"] = retained_assertions
            if isinstance(raw.get("assertions"), list):
                texts = [assertion.get("text") for assertion in retained_assertions]
                if all(isinstance(value, str) for value in texts):
                    behavior["assertions"] = texts
        projected.append(behavior)
    return projected


def _required_text(value: Any, field: str) -> str:
    if not _nonblank(value):
        raise ValueError(f"{field} must be a nonblank string")
    return value


def _normalized_gradle_task(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().lstrip(":")


def _nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())
