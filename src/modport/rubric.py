"""One immutable acceptance rubric shared by all contract agents and gates."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from .manifest import canonical_json
from .author_contracts import characterization_evidence_schema


RUBRIC_ID = "modport.acceptance-rubric"
RUBRIC_VERSION = 4


def acceptance_rubric(*, workflow_version: int = 0) -> dict[str, Any]:
    """Return the canonical, self-identifying contract acceptance rubric."""

    payload: dict[str, Any] = {
        "schema_version": 1,
        "rubric_id": RUBRIC_ID,
        "rubric_version": RUBRIC_VERSION,
        "roles": ["generator", "reviewer", "reviser", "deterministic-verifier"],
        "rules": [
            {
                "id": "runtime-behavior-requires-runtime-evidence",
                "severity": "blocking",
                "applies_to": ["generator", "reviewer", "reviser", "deterministic-verifier"],
                "statement": "A runtime behavior must be exercised through an executable runtime harness. Source substring matching, source parsing, raw JSON inspection, or hard-coded facts are not behavioral evidence.",
            },
            {
                "id": "static-client-evidence-is-explicit",
                "severity": "blocking",
                "applies_to": ["generator", "reviewer", "reviser", "deterministic-verifier"],
                "statement": "Static evidence is allowed only for client-only visual behavior that cannot be automated. It must state why, record observations, and map to the mandatory client_smoke acceptance gate.",
            },
            {
                "id": "evidence-is-source-bound",
                "severity": "blocking",
                "applies_to": ["generator", "reviewer", "reviser", "deterministic-verifier"],
                "statement": "Every runtime evidence record must identify the mod source version, the current execution nonce, concrete operation witnesses, and the captured process log. File hashes are not required. Merely copying verifier environment values without runtime witnesses is rejected.",
            },
            {
                "id": "tests-are-executable-and-falsifiable",
                "severity": "blocking",
                "applies_to": ["generator", "reviewer", "reviser", "deterministic-verifier"],
                "statement": "Declared tasks must execute against the original baseline and fail when the asserted behavior is broken. NO-SOURCE, reporting-only tasks, and circular tests are rejected.",
            },
            {
                "id": "mapping-is-exact",
                "severity": "blocking",
                "applies_to": ["generator", "reviewer", "reviser", "deterministic-verifier"],
                "statement": "Every behavior test_mapping id must be globally unique across behaviors, occur exactly once in test_evidence and point to one distinct freshly generated declared evidence file. Two behaviors must not share a test id.",
            },
            {
                "id": "contract-cannot-be-weakened",
                "severity": "blocking",
                "applies_to": ["reviewer", "reviser"],
                "statement": "A failed test or rejected review must be fixed without deleting discovered behavior, weakening assertions, inventing evidence, or granting a waiver.",
            },
        ],
        "test_evidence_schema": characterization_evidence_schema(),
    }
    if workflow_version >= 34:
        payload["rubric_version"] = 5
        payload["test_evidence_schema"] = characterization_evidence_schema(workflow_version=workflow_version)
        for rule in payload["rules"]:
            if rule["id"] == "tests-are-executable-and-falsifiable":
                rule["statement"] = ("Declared target tasks must exercise the source-derived behavior requirements "
                    "and fail when an assertion is violated. Source functionality is user-confirmed; "
                    "source runtime tests are not required. Missing, skipped, NO-SOURCE or reporting-only "
                    "target cases do not establish runtime acceptance.")
    payload["rubric_sha256"] = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return payload


__all__ = ["RUBRIC_ID", "RUBRIC_VERSION", "acceptance_rubric"]
