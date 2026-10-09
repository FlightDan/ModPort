"""Target preflight consumes actual version aliases without changing the lock."""
import unittest

from modport.evidence import atomic_json
from modport.handlers import AcceptancePreflightHandler
from modport.models import LockedManifest, MigrationRequest
import test_independent_source_reading as freeze_fixture


class CurrentTargetPreflightTests(unittest.TestCase):
    def setUp(self):
        fixture = freeze_fixture.IndependentSourceReadingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        request = MigrationRequest('fixture', 'https://example.invalid/fixture.git',
            '1.21', '26.1', source_loader='neoforge', target_loader_version='26.1.0.19-beta')
        manifest = LockedManifest(request, '26.1.0.19-beta',
            source_commit='host-provided-source', minecraft_version='26.1', java_version='25',
            java_toolchain={'executable': 'jdk/bin/java'})
        atomic_json(self.root / 'artifacts/locked-manifest.json', manifest.to_dict())
        atomic_json(self.root / 'artifacts/source.json', {'source_commit': manifest.source_commit})
        java = self.root / 'toolchains/gradle-cache/jdk/bin/java'
        java.parent.mkdir(parents=True)
        java.write_text('host-provided executable placeholder; never executed\n')
        (self.root / 'worktree/build.gradle').write_text(
            'java.toolchain.languageVersion = JavaLanguageVersion.of(25)\n'
            'neoForge { version = project.neo_version }\n')
        self.command = fixture.command('acceptance_preflight')
        self.properties = self.root / 'worktree/gradle.properties'

    def test_existing_mc_version_alias_passes_real_preflight(self):
        self.properties.write_text('mc_version = 26.1\nneo_version = 26.1.0.19-beta\n')
        result = AcceptancePreflightHandler()(self.command)
        self.assertEqual('completed', result.status, result.detail)

    def test_alias_does_not_hide_wrong_or_conflicting_locked_version(self):
        for text in ['mc_version=26.1.1\nneo_version=26.1.0.19-beta\n',
                     'minecraft_version=26.1.1\nmc_version=26.1\nneo_version=26.1.0.19-beta\n']:
            with self.subTest(text=text):
                self.properties.write_text(text)
                result = AcceptancePreflightHandler()(self.command)
                self.assertEqual('target_toolchain_mismatch', result.error_code)


if __name__ == '__main__':
    unittest.main()
