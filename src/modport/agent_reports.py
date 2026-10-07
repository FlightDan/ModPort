"""Free-form agent reports with a small, explicit host decision boundary."""
import json
import re


def review_decision(text):
    """Read a routing decision, retaining the report verbatim for other agents.

    Old JSON reports remain readable. New reports may use prose and a final
    MODPORT_DECISION line, or a fenced JSON control block for state updates.
    We never infer approval from words appearing in the report's discussion.
    """
    value = None
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        blocks = re.findall(r'^```json\s*\n(.*?)\n```\s*$', text, re.M | re.S)
        if blocks:
            try:
                value = json.loads(blocks[-1])
            except ValueError:
                pass
    if not isinstance(value, dict):
        value = {}
    marker = re.search(r'(?:^|\n)MODPORT_DECISION:\s*(approved|rejected)\s*\Z', text)
    if marker:
        if value.get('verdict') not in (None, marker[1]):
            raise ValueError('conflicting review routing decisions')
        value['verdict'] = marker[1]
    if value.get('verdict') not in ('approved', 'rejected'):
        raise ValueError('review needs an explicit approved/rejected routing decision')
    value['raw_report'] = text
    return value


def report_findings(value):
    """Keep report content available to consumers that collect feedback lists."""
    findings = value.get('findings')
    if not findings and value.get('verdict') == 'approved':
        return []
    if isinstance(findings, list) and findings:
        return [row if isinstance(row, dict) else {'report': row} for row in findings]
    return [{'report': findings if findings else value['raw_report']}]
