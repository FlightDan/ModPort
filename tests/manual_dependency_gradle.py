"""Offline real Gradle seed checks; run with MODPORT_TEST_GRADLE_HOME set.

Use only an installed, trusted Gradle distribution. All build logic executes
inside the credential-free sandbox with networking disabled.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

from modport.dependency_cache import publish_artifact
from modport.dependency_build import prepare_dependency_seed
from modport.handlers import _sandboxed_build_command


class OfflineGradleSeedTests(unittest.TestCase):
    def test_two_cold_runs_resolve_transitives_offline_and_cannot_write_seed(self):
        distribution = Path(os.environ["MODPORT_TEST_GRADLE_HOME"]).resolve()
        self.assertTrue((distribution / "bin/gradle").is_file())
        with tempfile.TemporaryDirectory(prefix="modport-offline-gradle-") as raw:
            base = Path(raw)
            store = base / "shared"
            jar = base / "artifact.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
            checksum = sha256(jar.read_bytes()).hexdigest()
            pom = base / "root.pom"
            pom.write_text('''<project><modelVersion>4.0.0</modelVersion>
                <groupId>seed.test</groupId><artifactId>root</artifactId><version>1.0</version>
                <dependencies><dependency><groupId>seed.test</groupId><artifactId>leaf</artifactId>
                <version>1.0</version></dependency></dependencies></project>''')
            publish_artifact(store, "seed.test:root:1.0", jar, checksum, "https://example.invalid/root.jar",
                pom_source=pom, pom_sha256=sha256(pom.read_bytes()).hexdigest(), pom_url="https://example.invalid/root.pom")
            publish_artifact(store, "seed.test:leaf:1.0", jar, checksum, "https://example.invalid/leaf.jar",
                no_transitive_dependencies=True)
            for name in ("first", "second"):
                root = base / name
                workspace = root / "worktree"
                workspace.mkdir(parents=True)
                (root / "artifacts").mkdir()
                refs = prepare_dependency_seed(store, root)
                (root / "run.json").write_text(json.dumps({"initial_refs": refs}))
                (workspace / "gradlew").write_text('exec /gradle-dist/bin/gradle "$@"\n')
                # Exercise project repositories first, then settings-only mode.
                settings = "rootProject.name='offline-seed'\n"
                repositories = "repositories { maven { url='https://www.cursemaven.com' } }\n"
                if name == "second":
                    settings += "dependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS) }\n"
                    repositories = ""
                (workspace / "settings.gradle").write_text(settings)
                (workspace / "build.gradle").write_text(repositories + '''
configurations { seed }
dependencies { seed 'seed.test:root:1.0' }
tasks.register('verifySeed') {
    doLast {
        def files = configurations.seed.resolve()
        assert files.collect { it.name }.sort() == ['leaf-1.0.jar', 'root-1.0.jar']
        def leaf = new File('/modport-dependencies/repository/seed/test/leaf/1.0/leaf-1.0.jar')
        try { leaf.append('bad'); throw new GradleException('seed was writable') }
        catch (IOException expected) { println('SEED_READ_ONLY_CONFIRMED') }
        println('TWO_TRANSITIVE_ARTIFACTS_RESOLVED_OFFLINE')
    }
}
''')
                args = _sandboxed_build_command(root, workspace,
                    ["bash", "/workspace/gradlew", "--offline", "--no-daemon", "--max-workers=1", "verifySeed"])
                args[1:1] = ["--unshare-net", "--ro-bind", str(distribution), "/gradle-dist"]
                result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        timeout=180)
                self.assertEqual(0, result.returncode, result.stdout)
                self.assertIn("TWO_TRANSITIVE_ARTIFACTS_RESOLVED_OFFLINE", result.stdout)
                self.assertIn("SEED_READ_ONLY_CONFIRMED", result.stdout)
                print(f"{distribution.name}/{name}: offline resolution and read-only seed passed")


if __name__ == "__main__":
    unittest.main()
