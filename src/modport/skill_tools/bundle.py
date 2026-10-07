"""Validate, seal and publish portable migration skills without external libraries."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from ..platform_files import file_os as os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def checksum(value):
    return sha256(canonical(value).encode('utf-8')).hexdigest()


def read(path):
    if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise ValueError('skill metadata must be a contained regular file')
    return json.loads(path.read_text(encoding='utf-8'))


def payload_files(root):
    files = {}
    for directory, names, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        names[:] = sorted(name for name in names if name != '__pycache__')
        for name in names:
            if (parent / name).is_symlink():
                raise ValueError('skill directory links are not permitted')
        for name in sorted(filenames):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or not path.is_file():
                raise ValueError('skill payload must contain regular files only')
            if relative in {'manifest.json', 'review.json'} or name.endswith('.pyc'):
                continue
            files[relative] = sha256(path.read_bytes()).hexdigest()
    return files


def build_manifest(root):
    root = Path(root).resolve()
    metadata = read(root / 'metadata.json')
    if metadata.get('schema_version') != 1 or metadata.get('kind') not in {'platform', 'java'}:
        raise ValueError('unsupported skill metadata')
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,95}', metadata.get('skill_id', '')):
        raise ValueError('invalid skill id')
    keys = {'java'} if metadata['kind'] == 'java' else {'minecraft', 'loader', 'loader_version'}
    for side in ('source', 'target'):
        value = metadata.get(side)
        if not isinstance(value, dict) or set(value) != keys or any(
                not isinstance(v, str) or not v.strip() for v in value.values()):
            raise ValueError('skill needs exact source and target versions')
    rules = read(root / 'rules.json')
    if (rules.get('schema_version') != 1
            or not isinstance(rules.get('known_gaps'), list)
            or not isinstance(rules.get('rules'), list)
            or not isinstance(rules.get('manual_checks'), list)):
        raise ValueError('unsupported skill rules schema')
    if any(rules.get(side) != metadata[side] for side in ('source', 'target')):
        raise ValueError('rules and metadata version mismatch')
    coverage = read(root / 'coverage.json')
    areas = coverage.get('areas') if isinstance(coverage, dict) else None
    if not isinstance(areas, list) or not areas:
        raise ValueError('skill requires a domain coverage matrix')
    rule_ids = {r['id'] for group in ('rules', 'manual_checks') for r in rules[group]}
    area_ids = set()
    for area in areas:
        if (not isinstance(area, dict) or not area.get('id') or area['id'] in area_ids
                or area.get('status') not in {'verified', 'manual', 'gap', 'verified-no-change'}
                or not isinstance(area.get('rule_ids'), list)
                or not set(area['rule_ids']).issubset(rule_ids)):
            raise ValueError('invalid coverage entry or missing rule reference')
        area_ids.add(area['id'])
    files = payload_files(root)
    for required in ('SKILL.md', 'rules.json', 'coverage.json', 'evidence.json', 'scripts/scan.py'):
        if required not in files:
            raise ValueError(f'skill is missing {required}')
    text = (root / 'SKILL.md').read_text(encoding='utf-8')
    if not text.startswith('---\n') or 'name:' not in text or 'description:' not in text:
        raise ValueError('SKILL.md requires skill frontmatter')
    generator = metadata.get('generator', {})
    body = {'schema_version': 1, 'kind': metadata['kind'], 'skill_id': metadata['skill_id'],
            'source': metadata['source'], 'target': metadata['target'],
            'generator_id': metadata.get('generator_id', metadata['kind'] + '-diff-agent'),
            'generator': generator, 'files': files,
            'requires_java': metadata.get('requires_java'),
            'knowledge_revision': metadata.get('knowledge_revision'),
            'coverage': {'domains': len(areas), 'rules': len(rules['rules']),
                         'manual_checks': len(rules['manual_checks']), 'known_gaps': rules['known_gaps']}}
    return {**body, 'bundle_sha256': checksum(body)}


def validate_examples(root):
    root = Path(root).resolve()
    metadata = read(root / 'metadata.json')
    # Execute only this trusted scanner, not code supplied by a candidate skill.
    try:
        from ..telemetry import probe_process
    except ImportError:  # Preserve the standalone bundle CLI.
        probe_process = subprocess.run
    process = probe_process(
        [sys.executable, '-I', str(Path(__file__).with_name('scan.py')),
         '--_validate-bundle', str(root)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, timeout=60)
    if process.returncode:
        raise ValueError(process.stderr.strip() or 'rule examples failed')


def _review_is_approved(review, manifest):
    """Return whether a review is usable for the current payload.

    The review is a workflow hint, not a content lock.  A skill agent can
    continue a run and update its payload without first rewriting every old
    review/manifest digest.  The current manifest is still calculated from
    the payload, but an old or absent ``bundle_sha256`` in review.json is
    deliberately ignored.
    """
    return (isinstance(review, dict) and review.get('verdict') == 'approved'
            and bool(review.get('reviewer_id'))
            and review.get('reviewer_id') != manifest.get('generator_id'))


def inspect(root, *, approved=False, validate=True):
    """Quickly inspect a reusable skill without requiring a prior seal.

    ``manifest.json`` and the review digest are derived metadata.  They are
    read when present so callers can report their state, but stale metadata
    does not make an otherwise complete, version-applicable skill unusable.
    Structural validation and the trusted rule-example check remain required;
    candidate ``scripts/scan.py`` is only checked as an entrypoint and is
    never executed here.
    """
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError('skill root must be a real directory without symlink ancestors')

    # build_manifest validates the payload and its version/schema fields.  It
    # intentionally computes a fresh digest rather than trusting manifest.
    manifest = build_manifest(root)
    if validate:
        validate_examples(root)

    scanner = root / 'scripts' / 'scan.py'
    if (scanner.is_symlink() or not scanner.is_file()
            or scanner.resolve() != scanner.absolute() or scanner.stat().st_size == 0):
        raise ValueError('skill is missing a usable scripts/scan.py entrypoint')

    manifest_path = root / 'manifest.json'
    if manifest_path.is_symlink():
        raise ValueError('skill manifest must be a contained regular file')
    disk_manifest = None
    if manifest_path.exists():
        try:
            disk_manifest = read(manifest_path)
        except (OSError, ValueError, TypeError, UnicodeError):
            # A partially written derived seal is equivalent to no seal.  The
            # payload was already validated above, so keep inspecting it.
            disk_manifest = None
    review_path = root / 'review.json'
    if review_path.is_symlink():
        raise ValueError('skill review must be a contained regular file')
    review = None
    if review_path.exists():
        try:
            review = read(review_path)
        except (OSError, ValueError, TypeError, UnicodeError):
            # Generation can continue and the review stage can replace a
            # malformed prior review.  Approved inspection below still fails
            # because review will be None.
            review = None
    if approved and not _review_is_approved(review, manifest):
        raise ValueError('skill lacks an independent approval')
    return {
        'manifest': manifest,
        'manifest_present': disk_manifest is not None,
        'manifest_matches': disk_manifest == manifest,
        'review': review,
        'review_present': review is not None,
        'review_matches': (isinstance(review, dict)
                           and review.get('bundle_sha256') == manifest['bundle_sha256']),
        'scanner': str(scanner),
    }


def verify(root, *, approved=False):
    """Validate a skill and return a manifest computed from its payload.

    Existing callers use this as the old verification API.  Keep that API,
    but make the computed manifest authoritative so an unfinished seal or a
    stale review digest does not force a complete regeneration.
    """
    return inspect(root, approved=approved)['manifest']


def _write_manifest(path, manifest):
    path.write_text(canonical(manifest) + '\n', encoding='utf-8')


def _copy_bundle(root, destination, manifest):
    """Copy a payload into a new directory and refresh derived manifest data."""
    for relative in manifest['files']:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, target)
    # Always write the current manifest into a newly materialized bundle.  A
    # stale source manifest is harmless and is not propagated to the cache.
    _write_manifest(destination / 'manifest.json', manifest)
    review = root / 'review.json'
    if not review.is_file() or review.is_symlink():
        raise ValueError('skill lacks review.json')
    target = destination / 'review.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(review, target)


def pair_index_path(store, kind, identity):
    """Index keys organize exact versions; their digests are not trust gates."""
    if kind not in {'platform', 'java'}:
        raise ValueError('invalid knowledge kind')
    return (Path(store).absolute() / '_pairs' / kind /
            (checksum(identity['source']) + '--' + checksum(identity['target'])) / 'index.json')


def indexed_paths(store, kind, identity):
    """Return this pair's paths, or None for an unindexed legacy store."""
    store = Path(store).absolute()
    index = pair_index_path(store, kind, identity)
    if not index.exists():
        return None
    data = read(index)
    if (data.get('kind') != kind or any(data.get(side) != identity[side]
                                       for side in ('source', 'target'))):
        raise ValueError('knowledge pair index identity mismatch')
    entries = data.get('revisions')
    if not isinstance(entries, list):
        raise ValueError('invalid knowledge pair revisions')
    paths = []
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('path'), str)
                or not isinstance(entry.get('revision'), str) or not entry['revision']):
            raise ValueError('invalid knowledge revision path')
        relative = Path(entry['path'])
        path = store / relative
        if (relative.is_absolute() or '..' in relative.parts or not relative.parts
                or path.resolve() != path.absolute()):
            raise ValueError('unsafe knowledge revision path')
        paths.append(path)
    return paths


def index_publication(store, destination, manifest):
    """Atomically append a generic revision under a per-version-pair lock."""
    from .. import platform_files as fcntl
    store, destination = Path(store).absolute(), Path(destination).absolute()
    kind = manifest['kind']
    identity = {side: manifest[side] for side in ('source', 'target')}
    index = pair_index_path(store, kind, identity)
    if index.resolve() != index.absolute():
        raise ValueError('unsafe knowledge pair index')
    index.parent.mkdir(parents=True, exist_ok=True)
    lock_path = index.with_suffix('.lock')
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = {'schema_version': 1, 'kind': kind, **identity, 'revisions': []}
        if index.exists():
            indexed_paths(store, kind, identity)
            data = read(index)
        entry = {'revision': manifest.get('knowledge_revision') or manifest['bundle_sha256'],
                 'path': destination.relative_to(store).as_posix()}
        entries = [{key: item[key] for key in ('revision', 'path')}
                   for item in data['revisions'] if item['revision'] != entry['revision']]
        entries.append(entry)
        # Reconstruct the whitelist instead of propagating unknown persisted state.
        data = {'schema_version': 1, 'kind': kind, **identity, 'revisions': entries}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', prefix='.index-',
                    dir=index.parent, delete=False) as output:
                temporary = Path(output.name)
                output.write(canonical(data) + '\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, index)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def publish(root, store):
    root, store = Path(root).absolute(), Path(store).absolute()
    # Approval is checked for its verdict/independence only; digest fields are
    # informational and may describe the previous payload revision.
    manifest = inspect(root, approved=True)['manifest']
    destination = store / '_published' / manifest['skill_id'] / manifest['bundle_sha256']
    if store.resolve() != store or destination.resolve() != destination:
        raise ValueError('publication path must not contain symlinks')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        # The content-addressed directory is only a storage convention.  An
        # existing complete package wins without re-running generation.
        current = inspect(destination, approved=True)['manifest']
        index_publication(store, destination, current)
        return destination
    temporary = Path(tempfile.mkdtemp(prefix='.publishing-', dir=destination.parent))
    try:
        _copy_bundle(root, temporary, manifest)
        inspect(temporary, approved=True)
        try:
            temporary.rename(destination)
        except OSError:
            if not destination.is_dir():
                raise
            inspect(destination, approved=True)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    index_publication(store, destination, inspect(destination, approved=True)['manifest'])
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('seal', 'verify', 'publish'))
    parser.add_argument('root', type=Path)
    parser.add_argument('--store', type=Path)
    parser.add_argument('--approved', action='store_true')
    args = parser.parse_args()
    try:
        if args.action == 'seal':
            validate_examples(args.root)
            manifest = build_manifest(args.root)
            (args.root / 'manifest.json').write_text(canonical(manifest) + '\n', encoding='utf-8')
            print(manifest['bundle_sha256'])
        elif args.action == 'verify':
            print(verify(args.root, approved=args.approved)['bundle_sha256'])
        else:
            if args.store is None:
                raise ValueError('--store is required for publication')
            print(publish(args.root, args.store))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
