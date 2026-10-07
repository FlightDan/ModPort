from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from modport.dependency_symbols import class_declarations, index_jar, index_frozen_dependencies


class DependencySymbolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_real_class_declarations_and_digest_are_read_without_execution(self):
        javac = shutil.which('javac')
        self.assertIsNotNone(javac, 'supported verification environment requires javac')
        source = self.root / 'Declaration.java'
        source.write_text('public class Declaration<T> { public int value; '
                          'public String call(int x) { return "x"; } }')
        compiled = subprocess.run([javac, '-proc:none', str(source)],
                                  capture_output=True, text=True, timeout=30)
        self.assertEqual(0, compiled.returncode, compiled.stderr)
        jar = self.root / 'artifact.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.write(self.root / 'Declaration.class', 'Declaration.class')
        digest = sha256(jar.read_bytes()).hexdigest()
        index = index_jar(jar, digest)
        row = index['classes'][0]
        self.assertEqual('Declaration', row['name'])
        self.assertEqual('java/lang/Object', row['superclass'])
        self.assertEqual('I', row['fields'][0]['descriptor'])
        self.assertIn('(I)Ljava/lang/String;', [item['descriptor'] for item in row['methods']])
        self.assertTrue(index['declarations_complete'])
        self.assertFalse(index['effective_classpath_verified'])
        with self.assertRaises(ValueError):
            index_jar(jar, '0' * 64)

    def test_missing_seed_is_uncovered_not_valid_zero_classpath(self):
        result = index_frozen_dependencies(self.root, {})
        self.assertEqual({}, result['artifacts'])
        self.assertTrue(result['diagnostics'])
        self.assertFalse(result['effective_classpath_verified'])

    def test_malformed_and_symlinked_inputs_are_not_accepted(self):
        with self.assertRaises(ValueError):
            class_declarations(b'\xca\xfe\xba\xbe')
        jar = self.root / 'artifact.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('Bad.class', b'bad')
        result = index_jar(jar, sha256(jar.read_bytes()).hexdigest())
        self.assertFalse(result['declarations_complete'])
        self.assertTrue(result['diagnostics'])
        linked = self.root / 'linked.jar'
        linked.symlink_to(jar)
        with self.assertRaises(ValueError):
            index_jar(linked, sha256(jar.read_bytes()).hexdigest())

    def test_expansion_limit_reports_incomplete(self):
        jar = self.root / 'artifact.jar'
        with zipfile.ZipFile(jar, 'w') as archive:
            archive.writestr('TooLarge.class', b'x' * 100)
        with patch('modport.dependency_symbols.MAX_CLASS_BYTES', 50):
            result = index_jar(jar, sha256(jar.read_bytes()).hexdigest())
        self.assertFalse(result['declarations_complete'])
        self.assertEqual([], result['classes'])

    def test_reused_long_constants_are_bounded_before_result_expansion(self):
        def utf8(text):
            data = text.encode()
            return b'\x01' + struct.pack('>H', len(data)) + data
        pool = (utf8('Example') + b'\x07\x00\x01' + utf8('field')
                + utf8('L' + 'x' * 1024 + ';') + utf8('java/lang/Object') + b'\x07\x00\x05')
        data = (struct.pack('>IHHH', 0xCAFEBABE, 0, 61, 7) + pool
                + struct.pack('>HHHHH', 0x21, 2, 6, 0, 100)
                + struct.pack('>HHHH', 1, 3, 4, 0) * 100 + b'\x00\x00\x00\x00')
        with self.assertRaisesRegex(ValueError, 'declaration text limit'):
            class_declarations(data)
