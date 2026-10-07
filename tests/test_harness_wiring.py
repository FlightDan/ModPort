from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest

from modport.harness_wiring import (
    CONVENTIONAL_SOURCES_INIT, characterization_init_scripts, ensure_characterization_init,
)


class HarnessWiringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_conventional_sources_receive_persistent_wiring_without_project_edits(self):
        source = self.root / '.modport/harness/example/Client.java'
        source.parent.mkdir(parents=True)
        source.write_text('package example; class Client {}')
        build = self.root / 'build.gradle'
        build.write_text('// original project')
        script = ensure_characterization_init(self.root)
        self.assertEqual(script, self.root / '.modport/characterization.init.gradle')
        first = script.read_bytes()
        self.assertEqual(script, ensure_characterization_init(self.root))
        self.assertEqual(first, script.read_bytes())
        self.assertEqual('// original project', build.read_text())

    def test_authored_launcher_and_nonstandard_layout_are_preserved(self):
        self.assertIsNone(ensure_characterization_init(self.root))
        script = self.root / '.modport/characterization.init.gradle'
        script.parent.mkdir()
        script.write_text('// specialized target launcher')
        self.assertEqual(script, ensure_characterization_init(self.root))
        self.assertEqual('// specialized target launcher', script.read_text())

    def test_harness_symlink_cannot_write_wiring_into_another_workspace(self):
        other = self.root / 'other'
        other.mkdir()
        (self.root / '.modport').symlink_to(other, target_is_directory=True)
        with self.assertRaises(ValueError):
            ensure_characterization_init(self.root)
        self.assertEqual([], list(other.iterdir()))

    def test_symlinked_source_is_rejected_before_wiring(self):
        directory = self.root / '.modport/harness'
        directory.mkdir(parents=True)
        (directory / 'Source.java').symlink_to(self.root / 'outside.java')
        with self.assertRaises(ValueError):
            ensure_characterization_init(self.root)
        self.assertFalse((directory.parent / 'characterization.init.gradle').exists())

    def _authored_conventional_harness(self):
        source = self.root / '.modport/harness/Smoke.java'
        source.parent.mkdir(parents=True)
        source.write_text('class Smoke {}')
        authored = self.root / '.modport/characterization.init.gradle'
        authored.write_text('// authored launch flags, but no source registration\n')
        return authored

    def test_v21_supplements_incomplete_authored_wiring_without_overwriting_it(self):
        authored = self._authored_conventional_harness()
        original = authored.read_bytes()
        scripts = characterization_init_scripts(self.root, workflow_version=21)
        self.assertEqual((authored, authored.parent / 'characterization-sources.init.gradle'), scripts)
        self.assertEqual(CONVENTIONAL_SOURCES_INIT, scripts[1].read_text())
        self.assertEqual(original, authored.read_bytes())
        self.assertEqual(scripts, characterization_init_scripts(self.root, workflow_version=21))

    def test_older_workflows_preserve_authored_wiring_semantics(self):
        authored = self._authored_conventional_harness()
        for version in (0, 16, 17, 20):
            self.assertEqual((authored,), characterization_init_scripts(self.root, workflow_version=version))
        self.assertFalse((authored.parent / 'characterization-sources.init.gradle').exists())

    def test_host_directory_keeps_authored_candidate_clean_and_supports_concurrency(self):
        from concurrent.futures import ThreadPoolExecutor
        authored = self._authored_conventional_harness()
        support = self.root / 'host-support'
        def prepare(_):
            return characterization_init_scripts(self.root, workflow_version=21,
                                                  supplement_directory=support)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(prepare, range(8)))
        self.assertTrue(all(value == results[0] for value in results))
        self.assertEqual(support, results[0][1].parent)
        self.assertFalse((authored.parent / 'characterization-sources.init.gradle').exists())

    def test_v21_default_wiring_does_not_need_a_supplement(self):
        authored = self._authored_conventional_harness()
        authored.unlink()
        self.assertEqual((authored,), characterization_init_scripts(self.root, workflow_version=21))

    def test_v21_nonstandard_authored_layout_remains_unchanged(self):
        self.assertEqual((), characterization_init_scripts(self.root, workflow_version=21))
        authored = self.root / '.modport/characterization.init.gradle'
        authored.parent.mkdir()
        authored.write_text('// nonstandard launcher')
        self.assertEqual((authored,), characterization_init_scripts(self.root, workflow_version=21))

    def test_v21_authored_script_does_not_bypass_source_symlink_checks(self):
        authored = self._authored_conventional_harness()
        (authored.parent / 'harness/Other.java').symlink_to(self.root / 'external.java')
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            characterization_init_scripts(self.root, workflow_version=21)

    def test_v21_supplement_conflict_and_symlink_are_not_overwritten(self):
        authored = self._authored_conventional_harness()
        supplement = authored.parent / 'characterization-sources.init.gradle'
        supplement.write_text('// user data')
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            characterization_init_scripts(self.root, workflow_version=21)
        self.assertEqual('// user data', supplement.read_text())
        supplement.unlink()
        supplement.symlink_to(self.root / 'external.gradle')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            characterization_init_scripts(self.root, workflow_version=21)

    def test_real_gradle_compiles_sources_with_an_incomplete_authored_script(self):
        gradle = os.environ.get('MODPORT_TEST_GRADLE') or shutil.which('gradle')
        if not gradle:
            self.skipTest('set MODPORT_TEST_GRADLE to a local Gradle executable')
        authored = self._authored_conventional_harness()
        authored.write_text("gradle.beforeProject { p -> p.ext.set('authoredFlag', 'preserved') }\n")
        (self.root / 'settings.gradle').write_text("rootProject.name='authored-harness-fixture'\n")
        (self.root / 'build.gradle').write_text(
            "plugins { id 'java' }\n"
            "tasks.register('runClient', JavaExec) {\n"
            " classpath=sourceSets.main.runtimeClasspath; mainClass='Smoke'\n"
            " systemProperty 'authored.flag', authoredFlag\n}\n")
        (authored.parent / 'harness/Smoke.java').write_text('''import java.nio.file.*;
public class Smoke {
 public static void main(String[] args) throws Exception {
  if (!"preserved".equals(System.getProperty("authored.flag"))) throw new AssertionError("authored");
  Files.writeString(Path.of("ran.txt"), "executed");
 }
}''')
        scripts = characterization_init_scripts(self.root, workflow_version=21)
        args = [gradle, '--offline', '--no-daemon', '-g', str(self.root / 'gradle-cache')]
        for script in scripts:
            args.extend(['--init-script', str(script)])
        result = subprocess.run([*args, 'runClient'], cwd=self.root, capture_output=True,
                                text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertTrue((self.root / 'build/classes/java/main/Smoke.class').is_file())
        self.assertEqual('executed', (self.root / 'ran.txt').read_text())

    def test_real_gradle_compiles_and_runs_harness_with_flags_and_isolated_directory(self):
        gradle = os.environ.get('MODPORT_TEST_GRADLE') or shutil.which('gradle')
        if not gradle:
            self.skipTest('set MODPORT_TEST_GRADLE to a local Gradle executable')
        (self.root / 'settings.gradle').write_text("rootProject.name='harness-fixture'\n")
        (self.root / 'build.gradle').write_text(
            "plugins { id 'java' }\n"
            "tasks.register('runClient', JavaExec) {\n"
            " classpath=sourceSets.main.runtimeClasspath; mainClass='Smoke'\n}\n")
        source = self.root / '.modport/harness/Smoke.java'
        source.parent.mkdir(parents=True)
        source.write_text('''import java.nio.file.*;
public class Smoke {
 public static void main(String[] args) throws Exception {
  if (!Boolean.getBoolean("modport.characterization")) throw new AssertionError("disabled");
  Path root=Path.of(System.getProperty("modport.projectRoot"));
  if (!Files.exists(root.resolve(".modport/harness/Smoke.java"))) throw new AssertionError("root");
  if (!Path.of("").toAbsolutePath().equals(root.resolve(".modport/run-characterization")))
   throw new AssertionError("working directory");
  Files.writeString(Path.of("ran.txt"), "executed");
 }
}''')
        script = ensure_characterization_init(self.root)
        result = subprocess.run([gradle, '--offline', '--no-daemon', '-g',
            str(self.root / 'gradle-cache'), '--init-script', str(script), 'runClient'],
            cwd=self.root, capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertEqual('executed', (self.root / '.modport/run-characterization/ran.txt').read_text())

    def test_real_gradle_registers_root_harness_in_client_subproject_only(self):
        self._real_gradle_subproject(root_java=False)

    def test_real_gradle_java_aggregate_root_does_not_compile_child_client_harness(self):
        self._real_gradle_subproject(root_java=True)

    def _real_gradle_subproject(self, *, root_java):
        gradle = os.environ.get('MODPORT_TEST_GRADLE') or shutil.which('gradle')
        if not gradle:
            self.skipTest('set MODPORT_TEST_GRADLE to a local Gradle executable')
        authored = self._authored_conventional_harness()
        (self.root / 'settings.gradle').write_text("rootProject.name='multi-harness'\ninclude 'mod', 'lib'\n")
        (self.root / 'build.gradle').write_text("plugins { id 'java' }\n" if root_java else '// root has no java plugin\n')
        (self.root / 'mod').mkdir()
        (self.root / 'lib').mkdir()
        (self.root / 'lib/build.gradle').write_text("plugins { id 'java' }\n")
        (self.root / 'mod/build.gradle').write_text(
            "plugins { id 'java' }\n"
            "tasks.register('runClient', JavaExec) {\n"
            " classpath=sourceSets.main.runtimeClasspath; mainClass='Smoke'\n}\n")
        (authored.parent / 'harness/Smoke.java').write_text('''import java.nio.file.*;
public class Smoke {
 public static void main(String[] args) throws Exception {
  Files.writeString(Path.of("ran.txt"), "executed");
 }
}''')
        scripts = characterization_init_scripts(self.root, workflow_version=21,
                                                supplement_directory=self.root / 'host-support')
        args = [gradle, '--offline', '--no-daemon', '-g', str(self.root / 'gradle-cache')]
        for script in scripts:
            args.extend(['--init-script', str(script)])
        tasks = [':mod:runClient', ':lib:classes', *([':classes'] if root_java else [])]
        result = subprocess.run([*args, *tasks], cwd=self.root,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stdout[-4000:] + result.stderr[-2000:])
        self.assertTrue((self.root / 'mod/build/classes/java/main/Smoke.class').is_file())
        self.assertFalse((self.root / 'build/classes/java/main/Smoke.class').exists())
        self.assertFalse((self.root / 'lib/build/classes/java/main/Smoke.class').exists())
        self.assertEqual('executed', (self.root / 'mod/ran.txt').read_text())
