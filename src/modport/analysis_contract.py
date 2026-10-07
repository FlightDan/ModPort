"""Shared, effect-free vocabulary for mod analysis outputs and assignments."""

CANDIDATE_STATUSES = ("import_only", "confirmed", "candidate", "false_positive", "unknown")
GAP_STATUSES = ("resolved", "not_applicable", "unresolved")
ANALYSIS_SCHEMA_VERSION = 2
GAP_KINDS = ("knowledge", "verification")
VERIFICATION_STAGES = ("target_build", "test_execute", "client_smoke")

# Agent analysis cannot manufacture host decisions or their provenance.
HOST_GAP_FIELDS = frozenset({"project_status", "verification_status", "review_execution_id",
    "resolution", "attempted_alternatives", "requirement_reviews", "research_gap_id",
    "source_gap_id", "verified_execution_id", "producer_execution_id", "review_id", "reviewer_id"})

GAP_IDENTITY_SCHEMA = "Use the exact current gap_id for every stable project gap; skill/index alone is permitted only for legacy rows without gap_id or entry_id."

GENERIC_ENTRIES_SCHEMA = ('generic_knowledge_entries (approved_generic_knowledge_entries for reviewers) '
    'is an optional mapping of authorized platform/java keys to entry arrays. Each entry has exactly '
    'id, category, summary, applicability, migration, compat, verification, evidence; every field except '
    'evidence is a nonblank string. id matches [a-z0-9][a-z0-9._-]{0,127} and is unique within its skill. '
    'evidence is a nonempty array of objects with exactly source, locator, supports: nonblank strings; '
    'source is a portable HTTP(S) URL. Do not include project status or other project-specific fields.')


def encoded_gap_id(kind, skill, entry_id):
    """An injective tuple namespace disjoint from historical kind:entry IDs."""
    import base64
    import json
    payload = json.dumps([kind, skill, entry_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "gap:v1:" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
