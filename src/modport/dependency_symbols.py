"""Read bounded JVM declaration indexes without loading or executing classes.

Format reference: https://docs.oracle.com/javase/specs/jvms/se25/html/jvms-4.html
This is not a verifier, effective-classpath resolver, or compatibility proof.
"""

from hashlib import sha256
import io
from pathlib import Path
import zipfile


MAX_JAR_BYTES = 64 * 1024 * 1024
MAX_CLASS_BYTES = 2 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_CLASSES = 4096
MAX_MEMBERS = 40000
MAX_DECLARATION_TEXT_BYTES = 512 * 1024
MAX_INDEX_BYTES = 4 * 1024 * 1024


def class_declarations(data):
    if len(data) > MAX_CLASS_BYTES:
        raise ValueError('class exceeds byte limit')
    cursor, declaration_bytes = 0, 0

    def take(size):
        nonlocal cursor
        if size < 0 or cursor + size > len(data):
            raise ValueError('truncated class structure')
        value = data[cursor:cursor + size]
        cursor += size
        return value

    def number(size=2):
        return int.from_bytes(take(size), 'big')

    if number(4) != 0xCAFEBABE:
        raise ValueError('not a JVM class')
    minor, major = number(), number()
    if not 45 <= major <= 69:
        raise ValueError('unsupported class version')
    pool = [None] * number()
    index = 1
    while index < len(pool):
        tag = number(1)
        if tag == 1:
            # Modified UTF-8 permits NUL and UTF-16 surrogate encodings.
            raw = take(number()).replace(b'\xc0\x80', b'\x00')
            pool[index] = ('text', raw.decode('utf-8', errors='surrogatepass'))
        elif tag in (7, 8, 16, 19, 20):
            pool[index] = (tag, number())
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            take(4)
        elif tag in (5, 6):
            take(8)
            index += 1
        elif tag == 15:
            take(3)
        else:
            raise ValueError('unknown constant pool tag')
        index += 1

    def text(index):
        nonlocal declaration_bytes
        if not 0 < index < len(pool) or not pool[index] or pool[index][0] != 'text':
            raise ValueError('invalid UTF-8 constant reference')
        # Charge each reference, not just each pool entry: one long constant
        # reused by thousands of members must not expand into gigabytes.
        declaration_bytes += 12 * len(pool[index][1]) + 128
        if declaration_bytes > MAX_DECLARATION_TEXT_BYTES:
            raise ValueError('declaration text limit reached')
        # Normalize paired surrogates to ordinary Unicode for JSON consumers.
        return pool[index][1].encode('utf-16', 'surrogatepass').decode('utf-16')

    def class_name(index):
        if index == 0:
            return None
        if not 0 < index < len(pool) or not pool[index] or pool[index][0] != 7:
            raise ValueError('invalid class constant reference')
        return text(pool[index][1])

    def attributes():
        signature = None
        for _ in range(number()):
            name, size = text(number()), number(4)
            if name == 'Signature' and size == 2:
                signature = text(number())
            else:
                take(size)
        return signature

    access, name, superclass = number(), class_name(number()), class_name(number())
    if name is None:
        raise ValueError('class declaration lacks its own name')
    interfaces = [class_name(number()) for _ in range(number())]
    groups = {}
    for group in ('fields', 'methods'):
        members = []
        count = number()
        if count > MAX_MEMBERS:
            raise ValueError('class exceeds member limit')
        for _ in range(count):
            flags, member_name, descriptor = number(), text(number()), text(number())
            signature = attributes()
            members.append({'name': member_name, 'descriptor': descriptor,
                            'signature': signature, 'access_flags': flags})
        groups[group] = members
    signature = attributes()
    if cursor != len(data):
        raise ValueError('unexpected bytes after class structure')
    return {'name': name, 'superclass': superclass, 'interfaces': interfaces,
            'major_version': major, 'minor_version': minor, 'access_flags': access,
            'signature': signature, **groups}


def _json_size_bound(value):
    if isinstance(value, str):
        return 12 * len(value) + 2
    if isinstance(value, dict):
        return 64 + sum(_json_size_bound(key) + _json_size_bound(item) + 4
                        for key, item in value.items())
    if isinstance(value, list):
        return 64 + sum(_json_size_bound(item) + 2 for item in value)
    return 64


def index_jar(path, expected_sha256):
    path = Path(path).absolute()
    if path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ValueError('jar must be a regular non-symlink file')
    with path.open('rb') as stream:
        data = stream.read(MAX_JAR_BYTES + 1)
    if len(data) > MAX_JAR_BYTES or sha256(data).hexdigest() != expected_sha256:
        raise ValueError('jar exceeds limit or differs from locked digest')
    rows, diagnostics, expanded, members, index_bytes = [], [], 0, 0, 0
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 50000:
            raise ValueError('jar directory exceeds entry limit')
        names = set()
        for entry in entries:
            if entry.filename in names:
                raise ValueError('duplicate jar entry')
            names.add(entry.filename)
            if not entry.filename.endswith('.class'):
                continue
            if entry.filename.startswith('META-INF/versions/'):
                diagnostics.append('multi-release variants not resolved')
                continue
            if (len(rows) >= MAX_CLASSES or entry.file_size > MAX_CLASS_BYTES
                    or expanded + entry.file_size > MAX_EXPANDED_BYTES):
                diagnostics.append('class/expanded byte limit reached')
                break
            expanded += entry.file_size
            try:
                row = class_declarations(archive.read(entry))
                members += len(row['fields']) + len(row['methods'])
                if members > MAX_MEMBERS:
                    diagnostics.append('member limit reached')
                    break
                indexed = {'entry': entry.filename, **row}
                index_bytes += _json_size_bound(indexed)
                if index_bytes > MAX_INDEX_BYTES:
                    diagnostics.append('declaration output limit reached')
                    break
                rows.append(indexed)
            except (ValueError, UnicodeError) as error:
                diagnostics.append(f'{entry.filename[:256]}: {str(error)[:256]}')
                if len(diagnostics) >= 128:
                    diagnostics.append('diagnostic limit reached')
                    break
    return {'jar_sha256': expected_sha256, 'classes': rows,
            'declarations_complete': not diagnostics, 'diagnostics': sorted(set(diagnostics)),
            'effective_classpath_verified': False, 'acceptance_evidence': False}


def index_frozen_dependencies(root, refs):
    """Index only the authenticated seed, never silently promote a Gradle cache."""
    from .dependency_build import REPOSITORY
    from .dependency_cache import verify_repository
    from .evidence import verified_path, file_digest
    root = Path(root)
    ref = refs.get('dependency_repository')
    result = {'schema_version': 1, 'scope': 'frozen_dependency_seed', 'artifacts': {},
              'dependency_manifest_sha256': ref.get('sha256') if ref else None,
              'effective_classpath_verified': False, 'acceptance_evidence': False,
              'diagnostics': []}
    if ref is None:
        result['diagnostics'].append('no authenticated dependency seed; resolved compile classpath not indexed')
        return result
    path = verified_path(root, ref)
    if ref.get('path') != REPOSITORY + '/manifest.json' or file_digest(path) != ref.get('sha256'):
        raise ValueError('dependency manifest identity mismatch')
    manifest = verify_repository(root / REPOSITORY)
    size = 0
    for coordinate, record in sorted(manifest['artifacts'].items()):
        if len(result['artifacts']) >= 16:
            result['diagnostics'].append('dependency artifact limit reached')
            break
        try:
            indexed = index_jar(root / REPOSITORY / record['jar']['path'], record['jar']['sha256'])
        except (OSError, ValueError, zipfile.BadZipFile, RuntimeError) as error:
            result['diagnostics'].append(f'{coordinate}: {error}')
            continue
        size += _json_size_bound(indexed)
        if size > 8 * 1024 * 1024:
            result['diagnostics'].append('dependency index output limit reached')
            break
        result['artifacts'][coordinate] = indexed
    return result
