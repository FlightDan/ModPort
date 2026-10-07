import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from modport.platform_files import UnsafePathError, assert_no_reparse, safe_open
from modport.windows_sandbox import (
    SandboxMount, WindowsSandboxSpec, run_windows_sandbox, translate_sandbox_paths,
    _grant_objects,
)


class SandboxSpecTests(unittest.TestCase):
    def test_narrow_output_grant_preserves_readonly_product_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            output = workspace / "build"
            output.mkdir(parents=True)
            source = workspace / "product.java"
            source.write_text("immutable input")
            result = output / "result.txt"
            result.write_text("mutable output")
            spec = WindowsSandboxSpec([sys.executable], workspace, root / "host-records",
                [SandboxMount(output, "/output", False),
                 SandboxMount(workspace, "/workspace", True)])
            inventory = dict(_grant_objects(spec))
            self.assertTrue(inventory[workspace])
            self.assertTrue(inventory[source])
            self.assertFalse(inventory[output])
            self.assertFalse(inventory[result])

    def test_conflicting_readonly_grants_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "read-only"):
                WindowsSandboxSpec([sys.executable], root, root.parent / "private-host-records",
                    [SandboxMount(root, "/workspace", False),
                     SandboxMount(root / "artifact", "/artifact", True)])

    def test_environment_cannot_replace_profile_or_inject_credentials_via_reserved_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "environment"):
                WindowsSandboxSpec([sys.executable], root, root.parent / "private-host-records",
                    [SandboxMount(root, "/workspace")], environment={"HOME": "/host/home"})
            with self.assertRaisesRegex(ValueError, "network"):
                WindowsSandboxSpec([sys.executable], root, root.parent / "private-host-records",
                    [SandboxMount(root, "/workspace")], network_policy="share-host-all")

    @unittest.skipIf(os.name == "nt", "non-Windows fail-closed behavior")
    def test_no_native_host_shell_fallback_on_linux(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = WindowsSandboxSpec([sys.executable, "-c", "raise SystemExit(99)"],
                root, root.parent / "private-host-records", [SandboxMount(root, "/workspace")])
            with self.assertRaisesRegex(RuntimeError, "native Windows"):
                run_windows_sandbox(spec, timeout=1)

    def test_virtual_argument_paths_translate_without_rewriting_shell_language(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mounts = [SandboxMount(root, "/workspace")]
            result = translate_sandbox_paths(["/workspace/gradlew.bat", "-Pdir=/workspace/out",
                                              "/workspace-other"], mounts)
            self.assertEqual(result[0], str(root) + "/gradlew.bat")
            self.assertEqual(result[2], "/workspace-other")


@unittest.skipUnless(os.name == "nt", "requires native Windows AppContainer/Job/NTFS APIs")
class NativeWindowsSecurityTests(unittest.TestCase):
    def _spec(self, root, program, *, timeout=10):
        workspace = root / "workspace"
        workspace.mkdir()
        readonly = root / "readonly"
        readonly.mkdir()
        (readonly / "data").write_text("original")
        # The interpreter needs its standard library and DLL tree. These are
        # explicit read-only grants, not a grant to the host user profile.
        locations = {Path(sys.base_prefix), Path(sys.executable).parent}
        mounts = [SandboxMount(workspace, "/workspace", False),
                  SandboxMount(readonly, "/readonly", True)]
        for index, path in enumerate(sorted(locations, key=str)):
            mounts.append(SandboxMount(path, "/python" + ("-" + str(index) if index else ""), True))
        spec = WindowsSandboxSpec([sys.executable, "-I", "-S", "-c", program],
                                  workspace, root / "host-records", mounts, network_policy="none")
        return run_windows_sandbox(spec, timeout=timeout)

    def test_real_token_allows_workspace_write_but_denies_host_read_and_readonly_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "host-secret"
            secret.write_text("do not expose")
            program = (
                "import ctypes,os,pathlib\n"
                "k=ctypes.WinDLL('kernel32',use_last_error=True); "
                "a=ctypes.WinDLL('advapi32',use_last_error=True)\n"
                "k.GetCurrentProcess.restype=ctypes.c_void_p\n"
                "a.OpenProcessToken.argtypes=[ctypes.c_void_p,ctypes.c_uint32,ctypes.c_void_p]\n"
                "a.GetTokenInformation.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_void_p]\n"
                "token=ctypes.c_void_p(); assert a.OpenProcessToken(k.GetCurrentProcess(),8,ctypes.byref(token))\n"
                "value=ctypes.c_uint32(); length=ctypes.c_uint32(); "
                "assert a.GetTokenInformation(token,29,ctypes.byref(value),4,ctypes.byref(length)); assert value.value==1\n"
                "pathlib.Path('written').write_text('allowed')\n"
                "def denied(path,write=False):\n"
                " try:\n"
                "  p=pathlib.Path(path); p.write_text('bad') if write else p.read_text()\n"
                " except PermissionError: return\n"
                " raise AssertionError('sandbox permission boundary failed')\n"
                f"denied({str(secret)!r})\n"
                f"denied({str(root / 'readonly' / 'data')!r},True)\n"
                "assert 'MODPORT_SECURITY_TEST_SECRET' not in os.environ\n"
                "print('permission-boundary-enforced')\n"
            )
            previous = os.environ.get("MODPORT_SECURITY_TEST_SECRET")
            os.environ["MODPORT_SECURITY_TEST_SECRET"] = "test-only-secret"
            try:
                result = self._spec(root, program)
            finally:
                if previous is None:
                    os.environ.pop("MODPORT_SECURITY_TEST_SECRET", None)
                else:
                    os.environ["MODPORT_SECURITY_TEST_SECRET"] = previous
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            self.assertIn(b"permission-boundary-enforced", result.stdout)
            self.assertEqual((root / "workspace" / "written").read_text(), "allowed")
            self.assertEqual((root / "readonly" / "data").read_text(), "original")
            record = json.loads((root / "host-records" / "sandbox-lifecycle.json").read_text())
            self.assertEqual(record["state"], "closed")
            self.assertTrue(record["process_cleanup_confirmed"])
            self.assertTrue(record["permissions_cleanup_confirmed"])

    def test_junction_cannot_redirect_anchored_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "secret").write_text("private")
            junction = root / "junction"
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            try:
                with self.assertRaises(UnsafePathError):
                    assert_no_reparse(junction)
                with self.assertRaises(UnsafePathError):
                    safe_open(root, "junction/secret")
            finally:
                junction.rmdir()


if __name__ == "__main__":
    unittest.main()
