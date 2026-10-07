"""Keep stale generated package files out of source builds."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class CleanPackageBuild(build_py):
    def run(self):
        # setuptools reuses build/lib between invocations.  A module removed
        # from src can otherwise survive there and enter a later wheel.
        project_root = Path(__file__).resolve().parent
        build_output = project_root / "build" / "lib"
        configured = Path(self.build_lib)
        if not configured.is_absolute():
            configured = Path.cwd() / configured
        if configured.resolve() != build_output:
            raise RuntimeError("refusing to clean a ModPort build output outside build/lib")
        if (build_output.parent.is_symlink() or build_output.is_symlink()):
            raise RuntimeError("refusing to clean through a symlinked build directory")
        package_output = build_output / "modport"
        if package_output.is_symlink():
            raise RuntimeError("refusing to clean a symlinked ModPort build output")
        if package_output.exists():
            if not package_output.is_dir():
                raise RuntimeError("ModPort build output is not a directory")
            shutil.rmtree(package_output)
        super().run()


setup(cmdclass={"build_py": CleanPackageBuild})
