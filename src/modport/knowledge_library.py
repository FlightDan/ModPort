"""Version-pair knowledge projections; never stores project execution state."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

ENTRY_FIELDS = {'id', 'category', 'summary', 'applicability', 'migration', 'compat',
                'verification', 'evidence'}


def project_entries(entries):
    """Validate a portable, explicitly reviewed generic supplement."""
    if not isinstance(entries, list) or not entries:
        raise ValueError('generic_knowledge_entries must contain entries')
    result, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - ENTRY_FIELDS:
            raise ValueError('knowledge entry contains unsupported/project fields')
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', entry.get('id', '')):
            raise ValueError('knowledge entry requires stable id')
        if entry['id'] in seen:
            raise ValueError('duplicate knowledge entry id')
        seen.add(entry['id'])
        for field in ENTRY_FIELDS - {'evidence'}:
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f'knowledge entry requires {field}')
        evidence = entry.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            raise ValueError('knowledge entry requires sources')
        for source in evidence:
            if not isinstance(source, dict) or set(source) != {'source', 'locator', 'supports'}:
                raise ValueError('knowledge source requires source/locator/supports only')
            if not all(isinstance(value, str) and value.strip() for value in source.values()):
                raise ValueError('invalid knowledge source')
            url = urlparse(source['source'])
            if url.scheme not in {'https', 'http'} or not url.netloc:
                raise ValueError('knowledge source must be portable HTTP(S) URL')
        result.append(json.loads(json.dumps(entry)))
    return result


def revision_identity(manifest):
    return {key: manifest[key] for key in ('skill_id', 'source', 'target')} | {
        'revision': manifest.get('knowledge_revision') or manifest['bundle_sha256']}
