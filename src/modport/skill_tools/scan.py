#!/usr/bin/env python3
"""Read-only platform or Java migration candidate scanner (Python 3.10+)."""
import argparse
from bisect import bisect_right
from collections import Counter
from fnmatch import fnmatchcase
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse

# Scan the original mod sources only.  ``.modport`` contains generated
# characterization/harness code, and generated source/resource trees and
# build outputs are products of the toolchain rather than migration input.
DEFAULT_EXCLUDES = {'.git', '.hg', '.svn', '.gradle', '.idea', '.venv', 'venv',
                    'node_modules', 'build', 'out', 'target', 'generated',
                    'generated-sources', 'generated-resources', '.modport',
                    'harness', 'harnesses', '.harness', '__pycache__'}
FLAGS = {name: getattr(re, name) for name in ('MULTILINE', 'DOTALL', 'IGNORECASE', 'ASCII')}
VERSION_KEYS = ('minecraft', 'loader', 'loader_version')

# Names below are conventional generated outputs.  They are excluded when
# they occur at a project/output root, while the same name remains scannable as
# a package below a language source root such as src/main/java.  ``.modport``
# and tool metadata remain excluded at every depth.
_OUTPUT_DIRS = {'build', 'out', 'target'}
_GENERATED_DIRS = {'generated', 'generated-sources', 'generated-resources'}
_HARNESS_DIRS = {'harness', 'harnesses', '.harness'}
_SOURCE_ROOT_NAMES = {'java', 'kotlin', 'groovy', 'scala', 'resources'}
_ALWAYS_EXCLUDED = DEFAULT_EXCLUDES - _OUTPUT_DIRS - _GENERATED_DIRS - _HARNESS_DIRS


def require(condition, message):
    if not condition:
        raise ValueError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _under_source_root(parts):
    """Whether a directory is below a conventional source tree."""
    # ``parts`` includes the directory being considered.  Look for a source
    # marker before it, then a language/resource root after that marker.  This
    # keeps packages such as src/main/java/example/build and
    # src/main/java/example/harness visible without opening src/generated.
    for index, part in enumerate(parts[:-1]):
        if part == 'src' and any(value in _SOURCE_ROOT_NAMES for value in parts[index + 1:-1]):
            return True
    return False


def _exclude_directory(relative, excluded, custom_excluded=()):
    """Apply directory exclusions without hiding source packages by name."""
    parts = Path(relative).parts
    if not parts:
        return False
    name = parts[-1]
    if name not in excluded:
        return False
    # Explicit command-line exclusions retain their exact, global meaning.
    if name in set(custom_excluded):
        return True
    if name in _ALWAYS_EXCLUDED:
        return True
    if name in _OUTPUT_DIRS or name in _GENERATED_DIRS or name in _HARNESS_DIRS:
        return not _under_source_root(parts)
    return True


def strings(value, label, allow_empty=False):
    require(isinstance(value, list) and (allow_empty or bool(value))
            and all(nonempty(item) for item in value), f'{label}: expected string array')


def knowledge_entries(data):
    """Expose stable identities without rewriting legacy knowledge files."""
    entries = [dict(item) for group in ('rules', 'manual_checks', 'knowledge_entries')
               for item in data.get(group, [])]
    for gap in data.get('known_gaps', []):
        if isinstance(gap, dict):
            entries.append(dict(gap))
            continue
        topics = [topic for topic in ('menu', 'input', 'chat')
                  if topic in gap.lower()]
        if len(topics) > 1:
            entries.extend({'id': 'gap.' + topic, 'summary': gap, 'category': topic,
                            'status': 'unknown'} for topic in topics)
        else:
            entries.append({'id': 'gap.' + hashlib.sha256(gap.encode()).hexdigest()[:16],
                            'summary': gap, 'status': 'unknown'})
    return list({entry['id']: entry for entry in entries}.values())


def validate(data, expected):
    require(isinstance(data, dict), 'rules must be a JSON object')
    require(type(data.get('schema_version')) is int and data['schema_version'] == 1,
            'schema_version must be 1')
    for side in ('source', 'target'):
        version = data.get(side)
        keys = {'java'} if set(expected[side]) == {'java'} else set(VERSION_KEYS)
        require(isinstance(version, dict) and set(version) == keys
                and all(nonempty(version[k]) for k in keys), f'invalid {side} version')
        require(version == expected[side], f'{side} version mismatch: rules={version}, requested={expected[side]}')
    gaps = data.get('known_gaps')
    require(isinstance(gaps, list) and all(nonempty(gap) or
            (isinstance(gap, dict) and nonempty(gap.get('id')) and nonempty(gap.get('summary')))
            for gap in gaps), 'known_gaps: expected strings or stable id/summary objects')
    require(isinstance(data.get('knowledge_entries', []), list) and all(
            isinstance(entry, dict) and nonempty(entry.get('id'))
            for entry in data.get('knowledge_entries', [])), 'invalid knowledge_entries')
    seen, compiled = set(), []
    for group in ('rules', 'manual_checks'):
        require(isinstance(data.get(group), list), f'{group} must be an array')
        for item in data[group]:
            require(isinstance(item, dict), f'{group} entry must be an object')
            for key in ('id', 'category', 'summary', 'recommendation', 'verification'):
                require(nonempty(item.get(key)), f'{group}: missing {key}')
            label = item['id']
            require(label not in seen, f'duplicate rule id: {label}')
            seen.add(label)
            evidence = item.get('evidence')
            require(isinstance(evidence, list) and evidence, f'{label}: missing evidence')
            for entry in evidence:
                require(isinstance(entry, dict) and all(nonempty(entry.get(k)) for k in
                        ('source', 'locator', 'supports')), f'{label}: invalid evidence')
                location = entry['source']
                url = urlparse(location)
                require(Path(location).is_absolute() or (url.scheme in ('http', 'https') and url.netloc),
                        f'{label}: evidence source must be an absolute path or HTTP(S) URL')
            if group == 'manual_checks':
                require(not any(k in item for k in ('pattern', 'flags', 'files', 'examples')),
                        f'{label}: manual check must not contain regex fields')
                continue
            strings(item.get('files'), f'{label}.files')
            require(nonempty(item.get('pattern')), f'{label}: missing pattern')
            flags = item.get('flags', [])
            strings(flags, f'{label}.flags', allow_empty=True)
            require(all(f in FLAGS for f in flags), f'{label}: unsupported regex flag')
            mask = 0
            for flag in flags:
                mask |= FLAGS[flag]
            try:
                pattern = re.compile(item['pattern'], mask)
            except re.error as exc:
                raise ValueError(f'{label}: invalid regex: {exc}') from exc
            examples = item.get('examples')
            require(isinstance(examples, dict), f'{label}: missing examples')
            for kind in ('match', 'no_match'):
                strings(examples.get(kind), f'{label}.examples.{kind}')
                for example in examples[kind]:
                    found = False
                    for match in pattern.finditer(example):
                        require(match.start() != match.end(), f'{label}: zero-width match')
                        found = True
                    require(found == (kind == 'match'), f'{label}: failed {kind} example')
            require(pattern.search('') is None, f'{label}: pattern matches empty input')
            compiled.append((item, pattern))
    return compiled


def scan(spec):
    data, root = spec['data'], Path(spec['root'])
    compiled = validate(data, spec['expected'])
    excluded = set(spec['excluded'])
    custom_excluded = set(spec.get('custom_excluded', ()))
    report = {'schema_version': 1, 'source': data['source'], 'target': data['target'],
              'root': str(root), 'rules_sha256': spec['rules_sha256'], 'scan_complete': True,
              'knowledge_entries': knowledge_entries(data),
              'known_gaps': data['known_gaps'], 'manual_checks': data['manual_checks'],
              'rules': data['rules'], 'findings': [], 'scanned_files': [], 'skipped': [],
              'scope': {'excluded_directory_names': sorted(excluded),
                        'files_without_applicable_rules': 0, 'excluded_directories': 0,
                        'max_file_bytes': spec['max_file_bytes'], 'max_findings': spec['max_findings']}}

    def skip(path, reason):
        report['scan_complete'] = False
        report['skipped'].append({'path': str(path), 'reason': reason})

    def walk_error(error):
        skip(error.filename or str(root), f'directory traversal failed: {error}')

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        kept = []
        for name in sorted(dirs):
            path = Path(directory) / name
            relative_dir = path.relative_to(root).as_posix()
            if _exclude_directory(relative_dir, excluded, custom_excluded):
                report['scope']['excluded_directories'] += 1
            elif path.is_symlink():
                skip(path.relative_to(root).as_posix(), 'symlink directory not followed')
            else:
                kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            applicable = [(rule, regex) for rule, regex in compiled
                          if any(fnmatchcase(relative, glob) for glob in rule['files'])]
            if not applicable:
                report['scope']['files_without_applicable_rules'] += 1
                continue
            if path.is_symlink() or not path.is_file():
                skip(relative, 'symlink or non-regular file not read')
                continue
            try:
                with path.open('rb') as handle:
                    raw = handle.read(spec['max_file_bytes'] + 1)
                if len(raw) > spec['max_file_bytes']:
                    skip(relative, 'file size limit exceeded')
                    continue
                if b'\x00' in raw:
                    skip(relative, 'binary/NUL content')
                    continue
                content = raw.decode('utf-8-sig')
            except (OSError, UnicodeError) as exc:
                skip(relative, f'cannot read UTF-8 text: {exc}')
                continue
            report['scanned_files'].append({'path': relative, 'sha256': hashlib.sha256(raw).hexdigest()})
            starts = [0] + [m.end() for m in re.finditer('\n', content)]
            for rule, regex in applicable:
                for match in regex.finditer(content):
                    require(match.start() != match.end(), f"{rule['id']}: zero-width match in {relative}")
                    if len(report['findings']) >= spec['max_findings']:
                        skip(relative, 'finding limit exceeded; remaining scan omitted')
                        return report
                    line = bisect_right(starts, match.start())
                    end_line = bisect_right(starts, match.end() - 1)
                    report['findings'].append({'rule_id': rule['id'], 'status': 'candidate', 'path': relative,
                        'line': line, 'column': match.start() - starts[line - 1] + 1,
                        'end_line': end_line, 'match': match.group()[:500],
                        'match_truncated': len(match.group()) > 500})
    if compiled and not report['scanned_files']:
        skip('.', 'no files scanned for the declared rule scope; check source root and globs')
    return report


def markdown(report):
    def safe(value):
        return str(value).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('`', '\\`').replace('\n', ' ')

    lines = ['# Migration scan', '', f"Scan complete within declared scope: **{report['scan_complete']}**",
             '', 'Text matches are candidates, not verified defects. Zero matches do not prove compatibility.',
             '', f"Source: {safe(report['source'])}", '', f"Target: {safe(report['target'])}",
             '', f"Rules SHA-256: `{report['rules_sha256']}`", '',
             f"Scanned files: {len(report['scanned_files'])}; findings: {len(report['findings'])}.",
             '', f"Scope: {safe(report['scope'])}", '', '## Findings', '']
    counts = Counter(item['rule_id'] for item in report['findings'])
    for rule in report['rules']:
        lines.extend([f"### {safe(rule['id'])} ({counts[rule['id']]})", '', safe(rule['summary']), '',
                      f"Suggestion: {safe(rule['recommendation'])}", '', f"Verify: {safe(rule['verification'])}", ''])
        for evidence in rule['evidence']:
            lines.append(f"- Evidence: {safe(evidence['source'])} — {safe(evidence['locator'])}; {safe(evidence['supports'])}")
        lines.append('')
        for hit in report['findings']:
            if hit['rule_id'] == rule['id']:
                lines.append(f"- {safe(hit['path'])}:{hit['line']}:{hit['column']} — `{safe(hit['match'])}`")
        lines.append('')
    lines.extend(['## Manual checks', ''])
    for check in report['manual_checks']:
        lines.extend([f"- {safe(check['id'])}: {safe(check['summary'])}",
                      f"  Suggestion: {safe(check['recommendation'])}; verify: {safe(check['verification'])}"])
        for evidence in check['evidence']:
            lines.append(f"  Evidence: {safe(evidence['source'])} — {safe(evidence['locator'])}; {safe(evidence['supports'])}")
    lines.extend(['', '## Research gaps', ''])
    lines.extend(f'- {safe(gap)}' for gap in report['known_gaps'])
    lines.extend(['', '## Skipped / incomplete coverage', ''])
    lines.extend(f"- {safe(item['path'])}: {safe(item['reason'])}" for item in report['skipped'])
    return '\n'.join(lines) + '\n'


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError('must be finite and positive')
    return number


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--_validate-bundle':
        try:
            root = Path(sys.argv[2])
            metadata = json.loads((root / 'metadata.json').read_text(encoding='utf-8'))
            data = json.loads((root / 'rules.json').read_text(encoding='utf-8'))
            validate(data, {side: metadata[side] for side in ('source', 'target')})
            return 0
        except (ValueError, TypeError, OSError, KeyError, RecursionError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if sys.argv[1:] == ['--_validate']:
        try:
            spec = json.load(sys.stdin)
            validate(spec['data'], spec['expected'])
            return 0
        except (ValueError, TypeError, OSError, KeyError, RecursionError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if sys.argv[1:] == ['--_worker']:
        try:
            print(json.dumps(scan(json.load(sys.stdin)), ensure_ascii=False))
            return 0
        except (ValueError, TypeError, OSError, RecursionError) as exc:
            print(str(exc), file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--rules', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='new directory outside source root')
    for side, default in (('source', 'forge'), ('target', 'neoforge')):
        parser.add_argument(f'--{side}-minecraft')
        parser.add_argument(f'--{side}-java')
        parser.add_argument(f'--{side}-loader', choices=('forge', 'neoforge'))
        parser.add_argument(f'--{side}-loader-version')
    parser.add_argument('--exclude-dir', action='append', default=[])
    parser.add_argument('--include-generated', action='store_true')
    parser.add_argument('--max-file-bytes', type=positive_int, default=2 * 1024 * 1024)
    parser.add_argument('--max-findings', type=positive_int, default=10000)
    parser.add_argument('--timeout', type=positive_seconds, default=60)
    args = parser.parse_args()
    try:
        root, rules_path, output = args.root.resolve(), args.rules.resolve(), args.output.resolve()
        require(root.is_dir(), 'source root is not a directory')
        require(not rules_path.is_relative_to(root), 'rules file must be outside source root')
        require(not output.is_relative_to(root), 'output directory must be outside source root')
        require(not output.exists() and not args.output.is_symlink(), 'output directory already exists')
        for name in args.exclude_dir:
            require(nonempty(name) and '/' not in name and '\\' not in name and name not in ('.', '..'),
                    '--exclude-dir accepts directory names only')
        with rules_path.open('rb') as handle:
            raw = handle.read(2 * 1024 * 1024 + 1)
        require(len(raw) <= 2 * 1024 * 1024, 'rules file exceeds 2 MiB')
        java_mode = args.source_java is not None or args.target_java is not None
        if java_mode:
            require(bool(args.source_java and args.target_java), 'both Java versions are required')
            require(not any((args.source_minecraft, args.target_minecraft,
                             args.source_loader_version, args.target_loader_version,
                             args.source_loader, args.target_loader)),
                    'Java and platform version arguments cannot be mixed')
            expected = {side: {'java': getattr(args, f'{side}_java')} for side in ('source', 'target')}
        else:
            args.source_loader = args.source_loader or 'forge'
            args.target_loader = args.target_loader or 'neoforge'
            expected = {side: {key: getattr(args, f'{side}_{key}') for key in VERSION_KEYS}
                        for side in ('source', 'target')}
            require(all(nonempty(value) for version in expected.values() for value in version.values()),
                    'both Minecraft and exact loader versions are required')
        excluded = DEFAULT_EXCLUDES - (
            {'build', 'out', 'target', 'generated', 'generated-sources', 'generated-resources'}
            if args.include_generated else set())
        spec = {'root': str(root), 'data': json.loads(raw), 'expected': expected,
                'rules_sha256': hashlib.sha256(raw).hexdigest(), 'excluded': sorted(excluded | set(args.exclude_dir)),
                'custom_excluded': sorted(set(args.exclude_dir)),
                'max_file_bytes': args.max_file_bytes, 'max_findings': args.max_findings}
        result = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--_worker'],
                                input=json.dumps(spec), text=True, encoding='utf-8', capture_output=True,
                                timeout=args.timeout, check=False)
        require(result.returncode == 0, result.stderr.strip() or f'scan worker failed: {result.returncode}')
        report = json.loads(result.stdout)
        rendered = markdown(report)
        output.mkdir(parents=True, exist_ok=False)
        (output / 'scan.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        (output / 'scan.md').write_text(rendered, encoding='utf-8')
        print(f"{len(report['findings'])} candidates; scan_complete={report['scan_complete']}; {output}")
        return 0 if report['scan_complete'] else 3
    except subprocess.TimeoutExpired:
        print(f'error: scan exceeded {args.timeout:g}s; no successful report produced', file=sys.stderr)
        return 2
    except (ValueError, TypeError, OSError, RecursionError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
