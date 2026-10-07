"""Native AppContainer execution for untrusted project code; no host fallback.

An execution receives a fresh package SID, private Windows profile, explicit
filesystem grants and an atomic kill-on-close Job. Filesystem rights are the
intersection of the caller's token and the AppContainer's grants. Input mounts
use native paths on Windows: AppContainer does not emulate Linux bind mounts.

Network policies are explicit. ``internet-and-private`` enables outbound
Internet and private-network capabilities, without a loopback exemption;
``none`` provides no network capabilities. Host credentials are never passed.

https://learn.microsoft.com/windows/win32/secauthz/implementing-an-appcontainer
https://learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-setentriesinaclw
https://learn.microsoft.com/windows/win32/api/aclapi/nf-aclapi-setsecurityinfo
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .platform_files import assert_no_reparse, pinned_windows_path, atomic_write
from .platform_runtime import ProcessCapture, capture_started_process
from .windows_process import DWORD, HANDLE, BOOL, kernel32, launch, require_windows, win_error


READ_EXECUTE = 0x1200A9
MODIFY = 0x1301BF  # No WRITE_DAC or WRITE_OWNER.
MAX_GRANTED_OBJECTS = 100_000
NETWORK_POLICIES = frozenset({"none", "internet-and-private"})
_RESERVED_ENVIRONMENT = frozenset({
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "GRADLE_USER_HOME", "JAVA_HOME",
})


class SandboxUnavailableError(RuntimeError):
    """AppContainer or its required grants could not be established."""


class SandboxCleanupError(RuntimeError):
    """Process or AppContainer cleanup remains unconfirmed."""


@dataclass(frozen=True)
class SandboxMount:
    source: Path
    target: str
    readonly: bool = True
    credential_filtered: bool = False

    def __post_init__(self):
        object.__setattr__(self, "source", Path(self.source))
        if not self.source.is_absolute() or ".." in self.source.parts:
            raise ValueError("sandbox mount source must be an absolute contained path")
        if (not isinstance(self.target, str) or not re.fullmatch(r"/[a-z][a-z0-9-]*", self.target)
                or type(self.readonly) is not bool or type(self.credential_filtered) is not bool):
            raise ValueError("invalid sandbox mount alias or access mode")


@dataclass(frozen=True)
class WindowsSandboxSpec:
    argv: Sequence[str]
    cwd: Path
    private_root: Path
    mounts: Sequence[SandboxMount]
    environment: Mapping[str, str] = field(default_factory=dict)
    network_policy: str = "internet-and-private"
    java_home: Path | None = None
    memory_limit_bytes: int | None = None

    def __post_init__(self):
        object.__setattr__(self, "argv", tuple(os.fspath(item) for item in self.argv))
        object.__setattr__(self, "cwd", Path(self.cwd))
        object.__setattr__(self, "private_root", Path(self.private_root))
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "environment", dict(self.environment))
        if (not self.argv or any("\x00" in item for item in self.argv)
                or not self.cwd.is_absolute() or not self.private_root.is_absolute()):
            raise ValueError("sandbox requires argv and absolute native directories")
        if self.network_policy not in NETWORK_POLICIES:
            raise ValueError("unsupported native sandbox network policy")
        aliases = [mount.target for mount in self.mounts]
        if len(aliases) != len(set(aliases)):
            raise ValueError("duplicate sandbox mount alias")
        for key, value in self.environment.items():
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                    or key.upper() in _RESERVED_ENVIRONMENT or not isinstance(value, str)
                    or "\x00" in value or "\n" in value):
                raise ValueError("invalid sandbox environment override")
        if not any(self.cwd == mount.source or self.cwd.is_relative_to(mount.source)
                   for mount in self.mounts):
            raise ValueError("sandbox working directory is outside its explicit grants")
        # Broader grants must not defeat a read-only child's security promise.
        for left in self.mounts:
            if self.private_root == left.source or self.private_root.is_relative_to(left.source):
                raise ValueError("host sandbox records must be outside project grants")
            for right in self.mounts:
                if (left is not right and left.source != right.source
                        and right.source.is_relative_to(left.source) and not left.readonly
                        and right.readonly):
                    raise ValueError("writable sandbox mount contains a read-only mount")
                if left.source == right.source and left.readonly != right.readonly:
                    raise ValueError("conflicting sandbox access modes")


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("sid", ctypes.c_void_p), ("attributes", DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("app_sid", ctypes.c_void_p),
                ("capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                ("count", DWORD), ("reserved", DWORD)]


class TRUSTEE(ctypes.Structure):
    _fields_ = [("multiple", ctypes.c_void_p), ("operation", ctypes.c_int),
                ("form", ctypes.c_int), ("type", ctypes.c_int), ("name", ctypes.c_void_p)]


class EXPLICIT_ACCESS(ctypes.Structure):
    _fields_ = [("permissions", DWORD), ("mode", ctypes.c_int),
                ("inheritance", DWORD), ("trustee", TRUSTEE)]


class _SecurityAPI:
    def __init__(self):
        require_windows()
        self.kernel = kernel32()
        self.user = ctypes.WinDLL("userenv", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        pointer = ctypes.c_void_p
        signatures = [
            (self.user, "CreateAppContainerProfile", [ctypes.c_wchar_p, ctypes.c_wchar_p,
                ctypes.c_wchar_p, pointer, DWORD, pointer], ctypes.c_int32),
            (self.user, "DeleteAppContainerProfile", [ctypes.c_wchar_p], ctypes.c_int32),
            (self.user, "GetAppContainerFolderPath", [ctypes.c_wchar_p, pointer], ctypes.c_int32),
            (self.advapi, "ConvertSidToStringSidW", [pointer, pointer], BOOL),
            (self.advapi, "ConvertStringSidToSidW", [ctypes.c_wchar_p, pointer], BOOL),
            (self.advapi, "FreeSid", [pointer], pointer),
            (self.advapi, "GetSecurityInfo", [HANDLE, ctypes.c_int, DWORD,
                pointer, pointer, pointer, pointer, pointer], DWORD),
            (self.advapi, "SetSecurityInfo", [HANDLE, ctypes.c_int, DWORD,
                pointer, pointer, pointer, pointer], DWORD),
            (self.advapi, "SetEntriesInAclW", [DWORD, pointer, pointer, pointer], DWORD),
            (self.ole, "CoTaskMemFree", [pointer], None),
        ]
        for dll, name, arguments, result in signatures:
            function = getattr(dll, name)
            function.argtypes, function.restype = arguments, result
        self.kernel.CreateMutexW.argtypes = [pointer, BOOL, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = HANDLE
        self.kernel.ReleaseMutex.argtypes = [HANDLE]
        self.kernel.ReleaseMutex.restype = BOOL

    @contextmanager
    def acl_mutex(self):
        # Serialize DACL read/merge/write among ModPort host processes. A fresh
        # execution SID permits independent concurrent Jobs without snapshot
        # restoration overwriting another execution's ACEs.
        mutex = self.kernel.CreateMutexW(None, False, "Local\\ModPortAppContainerACL-v1")
        if not mutex:
            raise win_error("CreateMutexW(ACL)")
        owned = False
        try:
            outcome = self.kernel.WaitForSingleObject(mutex, 30_000)
            if outcome not in (0, 0x80):  # An abandoned mutex grants ownership.
                raise SandboxUnavailableError("AppContainer ACL coordination timed out")
            owned = True
            yield
        finally:
            if owned:
                self.kernel.ReleaseMutex(mutex)
            self.kernel.CloseHandle(mutex)

    def sid(self, text: str):
        pointer = ctypes.c_void_p()
        if not self.advapi.ConvertStringSidToSidW(text, ctypes.byref(pointer)):
            raise win_error("ConvertStringSidToSidW")
        return pointer

    def change_grant(self, handle, sid, rights: int, *, remove: bool, directory: bool):
        descriptor, dacl, updated = (ctypes.c_void_p() for _ in range(3))
        try:
            code = self.advapi.GetSecurityInfo(handle, 1, 4, None, None,
                                              ctypes.byref(dacl), None, ctypes.byref(descriptor))
            if code:
                raise win_error("GetSecurityInfo(DACL)", code)
            if not dacl.value:
                raise SandboxUnavailableError("null filesystem DACL cannot establish sandbox isolation")
            trustee = TRUSTEE(None, 0, 0, 5, sid)  # TRUSTEE_IS_SID / WELL_KNOWN_GROUP
            entry = EXPLICIT_ACCESS(rights, 4 if remove else 1,
                                    3 if directory and not remove else 0, trustee)
            code = self.advapi.SetEntriesInAclW(1, ctypes.byref(entry), dacl, ctypes.byref(updated))
            if code:
                raise win_error("SetEntriesInAclW", code)
            code = self.advapi.SetSecurityInfo(handle, 1, 4, None, None, updated, None)
            if code:
                raise win_error("SetSecurityInfo(DACL)", code)
        finally:
            for pointer in (updated, descriptor):
                if pointer.value:
                    self.kernel.LocalFree(pointer)


class _AppContainer:
    def __init__(self, api: _SecurityAPI, network: str):
        self.api = api
        self.name = "modport." + uuid4().hex
        self.package_sid = ctypes.c_void_p()
        self.capability_sids = []
        self.profile: Path | None = None
        self.created = False
        try:
            names = [] if network == "none" else ["S-1-15-3-1", "S-1-15-3-3"]
            for value in names:
                self.capability_sids.append(api.sid(value))
            self.capabilities = (SID_AND_ATTRIBUTES * len(names))(
                *(SID_AND_ATTRIBUTES(sid, 4) for sid in self.capability_sids))
            result = api.user.CreateAppContainerProfile(self.name, self.name,
                "ModPort isolated project command", self.capabilities, len(names),
                ctypes.byref(self.package_sid))
            if result < 0:
                raise SandboxUnavailableError(f"CreateAppContainerProfile failed: HRESULT {result & 0xFFFFFFFF:#x}")
            self.created = True
            sid_text, folder = ctypes.c_wchar_p(), ctypes.c_wchar_p()
            try:
                if not api.advapi.ConvertSidToStringSidW(self.package_sid, ctypes.byref(sid_text)):
                    raise win_error("ConvertSidToStringSidW")
                result = api.user.GetAppContainerFolderPath(sid_text, ctypes.byref(folder))
                if result < 0:
                    raise SandboxUnavailableError("GetAppContainerFolderPath failed")
                self.profile = Path(folder.value)
            finally:
                if sid_text:
                    api.kernel.LocalFree(sid_text)
                if folder:
                    api.ole.CoTaskMemFree(folder)
            self.security = SECURITY_CAPABILITIES(self.package_sid, self.capabilities,
                                                 len(names), 0)
        except BaseException:
            self.close()
            raise

    def close(self):
        error = None
        if self.created:
            result = self.api.user.DeleteAppContainerProfile(self.name)
            if result < 0:
                error = SandboxCleanupError(
                    f"AppContainer profile cleanup failed for {self.name}: HRESULT {result & 0xFFFFFFFF:#x}")
            else:
                self.created = False
        if self.package_sid.value:
            self.api.advapi.FreeSid(self.package_sid)
            self.package_sid = ctypes.c_void_p()
        for sid in self.capability_sids:
            self.api.kernel.LocalFree(sid)
        self.capability_sids = []
        if error:
            raise error


def translate_sandbox_paths(argv: Sequence[str], mounts: Sequence[SandboxMount]) -> list[str]:
    """Translate virtual path tokens to the explicit native grant paths.

Only argument values are translated. Shell-language rewriting is unsafe and
is deliberately left to platform-specific command producers.
"""
    translated = []
    for argument in argv:
        value = str(argument)
        for mount in sorted(mounts, key=lambda item: len(item.target), reverse=True):
            pattern = re.escape(mount.target) + r"(?=/|$)"
            value = re.sub(pattern, lambda _: str(mount.source), value)
        translated.append(value)
    return translated


def _walk_grant_tree(root: Path):
    """Inventory without following Windows junctions or other link aliases."""
    pending = [root]
    count = 0
    while pending:
        path = pending.pop()
        assert_no_reparse(path)
        details = path.stat(follow_symlinks=False)
        directory = path.is_dir()
        if not directory and details.st_nlink != 1:
            raise SandboxUnavailableError("sandbox mount contains a hard-linked file")
        count += 1
        if count > MAX_GRANTED_OBJECTS:
            raise SandboxUnavailableError("sandbox mount exceeds bounded filesystem grant inventory")
        yield path
        if directory:
            with os.scandir(path) as entries:
                children = [Path(entry.path) for entry in entries]
            pending.extend(children)


def _grant_objects(spec: WindowsSandboxSpec) -> list[tuple[Path, bool]]:
    objects: dict[Path, bool] = {}
    # Broad read-only inputs may have explicitly named writable output trees.
    # Validate policy in the spec, then let the most-specific declaration win.
    for mount in sorted(spec.mounts, key=lambda item: len(item.source.parts)):
        assert_no_reparse(mount.source)
        if mount.credential_filtered:
            from .local_workspace_sandbox import workspace_inventory
            _, excluded = workspace_inventory(mount.source)
            if excluded:
                raise SandboxUnavailableError('credential-filtered workspace contains excluded entries')
        for path in _walk_grant_tree(mount.source):
            objects[path] = mount.readonly
            if len(objects) > MAX_GRANTED_OBJECTS:
                raise SandboxUnavailableError("sandbox exceeds bounded filesystem grant inventory")
    return sorted(objects.items(), key=lambda item: len(item[0].parts))


def _duplicate_grant_handle(api, path: Path, *, protect_root: bool):
    with pinned_windows_path(path, access=0x20000 | 0x40000 | 0x80,
                             share_delete=not protect_root) as handle:
        duplicate = HANDLE()
        current = api.kernel.GetCurrentProcess()
        if not api.kernel.DuplicateHandle(current, handle, current, ctypes.byref(duplicate),
                                           0, False, 2):
            raise win_error("DuplicateHandle(ACL)")
        return duplicate.value


def run_windows_sandbox(spec: WindowsSandboxSpec, *, timeout: float,
                        max_output_bytes: int = 1024 * 1024,
                        on_chunk=None) -> ProcessCapture:
    """Run the native command; revoke grants only after confirmed Job cleanup."""
    require_windows()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("sandbox requires a positive finite deadline")
    api = _SecurityAPI()
    spec.private_root.mkdir(parents=True, exist_ok=True)
    assert_no_reparse(spec.private_root)
    objects = _grant_objects(spec)
    container = _AppContainer(api, spec.network_policy)
    granted = []
    cleanup_errors = []
    process = None
    record = {"schema_version": 1, "profile_name": container.name,
              "network_policy": spec.network_policy,
              "mounts": [{"source": str(mount.source), "readonly": mount.readonly}
                         for mount in spec.mounts],
              "state": "preparing", "process_cleanup_confirmed": False,
              "permissions_cleanup_confirmed": False}

    def publish():
        atomic_write(spec.private_root / "sandbox-lifecycle.json",
                     (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))

    try:
        publish()
        with ExitStack() as paths:
            try:
                with api.acl_mutex():
                    roots = {mount.source for mount in spec.mounts}
                    for path, readonly in objects:
                        handle = _duplicate_grant_handle(api, path, protect_root=path in roots)
                        paths.callback(api.kernel.CloseHandle, handle)
                        directory = path.is_dir()
                        # Register before mutation so a partial grant failure
                        # still attempts targeted SID revocation.
                        granted.append((handle, directory))
                        api.change_grant(handle, container.package_sid,
                                         READ_EXECUTE if readonly else MODIFY,
                                         remove=False, directory=directory)
                profile = container.profile
                temp = profile / "Temp"
                temp.mkdir(exist_ok=True)
                home = profile / "Home"
                home.mkdir(exist_ok=True)
                gradle = next((mount.source for mount in spec.mounts if mount.target == "/gradle-cache"),
                              profile / "Gradle")
                if gradle == profile / "Gradle":
                    gradle.mkdir(exist_ok=True)
                system = Path(os.environ.get("SystemRoot", "C:\\Windows"))
                environment = {
                    "SystemRoot": str(system), "WINDIR": str(system),
                    "COMSPEC": str(system / "System32" / "cmd.exe"),
                    "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(profile),
                    "LOCALAPPDATA": str(profile), "TEMP": str(temp), "TMP": str(temp),
                    "GRADLE_USER_HOME": str(gradle), "LANG": "C.UTF-8",
                    "PATH": str(system / "System32"),
                }
                if spec.java_home is not None:
                    java = Path(spec.java_home)
                    if not any(mount.readonly and (java == mount.source or java.is_relative_to(mount.source))
                               for mount in spec.mounts):
                        raise ValueError("Java home must have an explicit read-only sandbox grant")
                    environment["JAVA_HOME"] = str(java)
                    environment["PATH"] = str(java / "bin") + os.pathsep + environment["PATH"]
                git_home = spec.environment.get('MODPORT_GIT_HOME')
                if git_home:
                    git_home = Path(git_home)
                    if not any(mount.target == '/git-home' and mount.readonly
                               and git_home.is_relative_to(mount.source) for mount in spec.mounts):
                        raise ValueError('Git executable directory needs an explicit read-only runtime grant')
                    environment['PATH'] = str(git_home) + os.pathsep + environment['PATH']
                environment.update(spec.environment)
                argv = translate_sandbox_paths(spec.argv, spec.mounts)
                if not Path(argv[0]).is_absolute():
                    raise ValueError("native sandbox executable must be absolute")
                with open(os.devnull, "rb") as stdin:
                    process = launch(argv, cwd=spec.cwd, environment=environment,
                                     stdin=stdin, security_capabilities=container.security,
                                     memory_limit_bytes=spec.memory_limit_bytes)
                record.update(state="running", pid=process.pid, birth=process.birth)
                publish()
                return capture_started_process(process, timeout=timeout,
                    max_output_bytes=max_output_bytes, on_chunk=on_chunk)
            finally:
                if process is not None and not process.cleanup_confirmed:
                    try:
                        process.close()
                    except Exception as exc:
                        cleanup_errors.append(str(exc))
                if process is None or process.cleanup_confirmed:
                    record["process_cleanup_confirmed"] = True
                    with api.acl_mutex():
                        # Parents first: remove inheritable grants before
                        # revoking explicit child ACEs. Existing handles remain
                        # valid even if the build deleted/renamed a child.
                        for handle, directory in granted:
                            try:
                                api.change_grant(handle, container.package_sid, 0,
                                                 remove=True, directory=directory)
                            except Exception as exc:
                                cleanup_errors.append(str(exc))
                        # Cover newly created files and protected child DACLs.
                        try:
                            for mount in spec.mounts:
                                for path in _walk_grant_tree(mount.source):
                                    with pinned_windows_path(path, access=0x20000 | 0x40000 | 0x80,
                                                             share_delete=True) as handle:
                                        api.change_grant(handle, container.package_sid, 0,
                                                         remove=True, directory=path.is_dir())
                        except Exception as exc:
                            cleanup_errors.append(str(exc))
                    record["permissions_cleanup_confirmed"] = not cleanup_errors
                else:
                    cleanup_errors.append("process cleanup unconfirmed; AppContainer grants retained")
    finally:
        if process is None or process.cleanup_confirmed:
            try:
                container.close()
            except Exception as exc:
                cleanup_errors.append(str(exc))
        record["state"] = "cleanup_pending" if cleanup_errors else "closed"
        if cleanup_errors:
            record["cleanup_errors"] = cleanup_errors
        publish()
        if cleanup_errors:
            raise SandboxCleanupError("; ".join(cleanup_errors))


def main(argv: Sequence[str] | None = None) -> int:
    """Host launcher entrypoint, suitable for the normal audited subprocess path."""
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    arguments = parser.parse_args(argv)
    from .platform_files import safe_open
    path = arguments.spec.absolute()
    descriptor = safe_open(path.parent, path.name)
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        value = json.load(source)
    mounts = [SandboxMount(Path(item["source"]), item["target"], item["readonly"],
                           item.get("credential_filtered", False))
              for item in value.pop("mounts")]
    if value.get("java_home"):
        value["java_home"] = Path(value["java_home"])
    spec = WindowsSandboxSpec(mounts=mounts, **value)

    def output(name, data):
        stream = sys.stdout.buffer if name == "stdout" else sys.stderr.buffer
        stream.write(data)
        stream.flush()

    try:
        result = run_windows_sandbox(spec, timeout=arguments.timeout, on_chunk=output)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"native sandbox unavailable: {exc}", file=sys.stderr)
        return 69
    return 124 if result.timed_out else result.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
