"""Guidance for agent-readable review reasoning, not a report wire schema."""

REJECTED_FINDINGS_PROMPT = ('Explain why you reject the work, cite relevant evidence or locations, '
    'and describe what would resolve the issues. Write naturally; no findings field schema is required.')


def validate_review_findings(document, *, strict=True):
    """Compatibility hook: reasoning is assessed by downstream agents."""
    return None
