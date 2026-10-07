"""OS file coordination and anchored access without following link redirects.

Windows paths are pinned with non-delete-shared handles and checked for every
reparse point. This rejects junctions as well as symbolic links. Linux uses
directory descriptors and O_NOFOLLOW. These helpers do not weaken checks to a
resolve-then-open operation on Windows.

https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-createfilew
https://learn.microsoft.com/windows/win32/api/fileapi/nf-fileapi-lockfileex
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Iterator


LOCK_SH, LOCK_EX, LOCK_NB, LOCK_UN = 1, 2, 4, 8


class UnsafePathError(ValueError):
    """The path contains an unsupported alias, link or reparse point."""


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("internal", ctypes.c_size_t), ("internal_high", ctypes.c_size_t),
                ("offset", ctypes.c_uint32), ("offset_high", ctypes.c_uint32),
                ("event", ctypes.c_void_p)]


class _FILE_INFORMATION(ctypes.Structure):
    _fields_ = [("attributes", ctypes.c_uint32), ("times", ctypes.c_uint32 * 6),
                ("volume", ctypes.c_uint32), ("size_high", ctypes.c_uint32),
                ("size_low", ctypes.c_uint32), ("links", ctypes.c_uint32),
                ("index_high", ctypes.c_uint32), ("index_low", ctypes.c_uint32)]


def flock(fd: int, operation: int) -> None:
    """Advisory lock; Windows uses one reserved byte and no descriptor cache.

Windows callers acquire once, then unlock or close. Reentrant acquisition and
lock upgrading are intentionally not emulated: caching fd/inode ownership is
unsafe because close/reopen can reuse both identities.
"""
    if os.name != "nt":
        import fcntl
        fcntl.flock(fd, operation)
        return
    from .windows_process import kernel32, win_error
    import msvcrt
    api = kernel32()
    handle = msvcrt.get_osfhandle(fd)
    offset = _OVERLAPPED()
    offset.offset, offset.offset_high = 0xFFFFFFFE, 0x7FFFFFFF
    requested = operation & (LOCK_SH | LOCK_EX | LOCK_UN)
    if requested not in (LOCK_SH, LOCK_EX, LOCK_UN):
        raise ValueError("invalid file lock operation")
    if requested == LOCK_UN:
        if not api.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(offset)):
            code = ctypes.get_last_error()
            if code != 158:  # ERROR_NOT_LOCKED
                raise win_error("UnlockFileEx", code)
        return
    while True:
        flags = (2 if requested == LOCK_EX else 0) | 1
        if api.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(offset)):
            return
        code = ctypes.get_last_error()
        if code not in (33, 158):
            raise win_error("LockFileEx", code)
        if operation & LOCK_NB:
            raise BlockingIOError(errno.EAGAIN, "file lock is held by another process")
        time.sleep(0.05)


def _absolute(path: Path | str) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise UnsafePathError("anchored access requires an absolute path without '..'")
    if os.name == "nt":
        raw = str(path)
        if raw.startswith(("\\\\", "\\?", "\\.")) or any(":" in part for part in path.parts[1:]):
            raise UnsafePathError("network, device and alternate-stream paths are unsupported")
    return path


@contextmanager
def pinned_windows_path(path: Path | str, *, access: int = 0x80,
                        create: int = 3, share_delete: bool = False) -> Iterator[int]:
    """Hold every path component against replacement; return the final handle.

``access`` and ``create`` are Win32 access/disposition constants. Ancestors
are opened with FILE_READ_ATTRIBUTES. Omitting FILE_SHARE_DELETE prevents a
validated component being renamed or replaced while these handles are held.
"""
    from .windows_process import kernel32, INVALID_HANDLE_VALUE, win_error
    api = kernel32()
    path = _absolute(path)
    handles = []
    parts = [Path(path.anchor)]
    for component in path.parts[1:]:
        parts.append(parts[-1] / component)
    try:
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            handle = api.CreateFileW(str(part), access if final else 0x80,
                                     1 | 2 | (4 if final and share_delete else 0),
                                     None, create if final else 3,
                                     0x02000000 | 0x00200000, None)
            if handle == INVALID_HANDLE_VALUE:
                raise win_error("CreateFileW(anchored path)")
            handles.append(handle)
            information = _FILE_INFORMATION()
            if not api.GetFileInformationByHandle(handle, ctypes.byref(information)):
                raise win_error("GetFileInformationByHandle")
            if information.attributes & 0x400:
                raise UnsafePathError("reparse points are not allowed")
            if not final and not information.attributes & 0x10:
                raise UnsafePathError("path ancestor is not a directory")
        yield handles[-1]
    finally:
        for handle in reversed(handles):
            api.CloseHandle(handle)


def _open_posix_directory(path: Path | str) -> int:
    path = _absolute(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def assert_no_reparse(path: Path | str) -> None:
    """Validate all existing components; do not use this as a future open gate."""
    path = _absolute(path)
    if os.name == "nt":
        with pinned_windows_path(path):
            return
    if path == Path(path.anchor):
        descriptor = _open_posix_directory(path)
    else:
        parent = _open_posix_directory(path.parent)
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent)
        finally:
            os.close(parent)
    os.close(descriptor)


def _relative(relative: Path | str) -> Path:
    value = Path(relative)
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise UnsafePathError("file path must be a nonempty contained relative path")
    if os.name == "nt" and (value.drive or any(":" in part for part in value.parts)):
        raise UnsafePathError("alternate-stream or drive-relative path")
    return value


def safe_open(root: Path | str, relative: Path | str, flags: int = os.O_RDONLY,
              mode: int = 0o600, *, share_delete: bool = False,
              allow_readonly_hardlinks: bool = False) -> int:
    """Open a regular file inside an existing root without traversing links."""
    root, relative = _absolute(root), _relative(relative)
    if allow_readonly_hardlinks and flags & (os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_CREAT):
        raise UnsafePathError("hard-link aliases are permitted only for explicit read-only access")
    if os.name == "nt":
        from .windows_process import kernel32, check
        import msvcrt
        access = 0x80000000 if not flags & os.O_WRONLY else 0
        if flags & (os.O_WRONLY | os.O_RDWR):
            access |= 0x40000000
        create = 1 if flags & os.O_EXCL else (4 if flags & os.O_CREAT else 3)
        with pinned_windows_path(root / relative, access=access, create=create,
                                 share_delete=share_delete) as handle:
            info = _FILE_INFORMATION()
            check(kernel32().GetFileInformationByHandle(handle, ctypes.byref(info)),
                  "GetFileInformationByHandle")
            if info.attributes & 0x10 or (info.links != 1 and not allow_readonly_hardlinks):
                raise UnsafePathError("expected a regular file without hard-link aliases")
            duplicate = ctypes.c_void_p()
            api = kernel32()
            current = api.GetCurrentProcess()
            check(api.DuplicateHandle(current, handle, current, ctypes.byref(duplicate),
                                      0, False, 2), "DuplicateHandle(file)")
            try:
                descriptor = msvcrt.open_osfhandle(duplicate.value, flags | os.O_BINARY)
            except BaseException:
                api.CloseHandle(duplicate)
                raise
        if flags & os.O_TRUNC:
            os.ftruncate(descriptor, 0)
        return descriptor
    directory = _open_posix_directory(root / relative.parent)
    try:
        descriptor = os.open(relative.name, (flags & ~os.O_TRUNC) | os.O_NOFOLLOW | os.O_CLOEXEC,
                             mode, dir_fd=directory)
    finally:
        os.close(directory)
    details = os.fstat(descriptor)
    if not stat.S_ISREG(details.st_mode) or (details.st_nlink != 1 and not allow_readonly_hardlinks):
        os.close(descriptor)
        raise UnsafePathError("expected a regular file without hard-link aliases")
    if flags & os.O_TRUNC:
        os.ftruncate(descriptor, 0)
    return descriptor


class FileLock:
    """An advisory process lock backed by an existing, stable lock filename."""
    def __init__(self, path: Path | str, *, blocking: bool = True):
        self.path = _absolute(path)
        self.blocking = blocking
        self.fd: int | None = None

    def __enter__(self):
        self.fd = safe_open(self.path.parent, self.path.name, os.O_CREAT | os.O_RDWR)
        try:
            flock(self.fd, LOCK_EX | (0 if self.blocking else LOCK_NB))
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            try:
                flock(self.fd, LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def atomic_write(path: Path | str, data: bytes, mode: int = 0o600) -> None:
    """Publish bytes in a contained directory; reject redirected destinations."""
    path = _absolute(path)
    temporary = "." + path.name + "." + secrets.token_hex(8)
    if os.name == "nt":
        with pinned_windows_path(path.parent):
            descriptor = safe_open(path.parent, temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                if path.exists() or path.is_symlink():
                    assert_no_reparse(path)
                file_os.replace(path.parent / temporary, path)
            finally:
                try:
                    (path.parent / temporary).unlink()
                except FileNotFoundError:
                    pass
        return
    parent = _open_posix_directory(path.parent)
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             mode, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafePathError("atomic destination is not a unique regular file")
        except FileNotFoundError:
            pass
        os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


class NativeDirectory:
    """Pinned Windows directory used by the scoped openat compatibility facade."""
    def __init__(self, path: Path):
        self.path = _absolute(path)
        self._context = pinned_windows_path(self.path, access=0x80 | 0x20000)
        self.handle = self._context.__enter__()
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self._context.__exit__(None, None, None)


def _security_functions():
    from .windows_process import kernel32, HANDLE, DWORD, BOOL
    api = kernel32()
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    signatures = {
        "OpenProcessToken": ([HANDLE, DWORD, pointer], BOOL),
        "GetTokenInformation": ([HANDLE, ctypes.c_int, pointer, DWORD, pointer], BOOL),
        "ConvertSidToStringSidW": ([pointer, pointer], BOOL),
        "GetSecurityInfo": ([HANDLE, ctypes.c_int, DWORD, pointer, pointer, pointer, pointer, pointer], DWORD),
        "GetAce": ([pointer, DWORD, pointer], BOOL),
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": ([ctypes.c_wchar_p, DWORD, pointer, pointer], BOOL),
        "GetSecurityDescriptorDacl": ([pointer, pointer, pointer, pointer], BOOL),
        "SetSecurityInfo": ([HANDLE, ctypes.c_int, DWORD, pointer, pointer, pointer, pointer], DWORD),
    }
    for name, (arguments, result) in signatures.items():
        getattr(advapi, name).argtypes, getattr(advapi, name).restype = arguments, result
    return api, advapi


def _sid_string(api, advapi, sid) -> str:
    from .windows_process import check
    value = ctypes.c_wchar_p()
    check(advapi.ConvertSidToStringSidW(sid, ctypes.byref(value)), "ConvertSidToStringSidW")
    try:
        return value.value
    finally:
        api.LocalFree(value)


def host_user_key() -> str:
    """Stable current-user identity for host-local coordination filenames."""
    if os.name != "nt":
        return str(os.getuid())
    from .windows_process import check, HANDLE, DWORD
    api, advapi = _security_functions()
    token = HANDLE()
    check(advapi.OpenProcessToken(api.GetCurrentProcess(), 8, ctypes.byref(token)), "OpenProcessToken")
    try:
        size = DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        check(advapi.GetTokenInformation(token, 1, data, size, ctypes.byref(size)), "GetTokenInformation(user)")
        sid = ctypes.c_void_p.from_buffer(data).value
        return _sid_string(api, advapi, sid)
    finally:
        api.CloseHandle(token)


def windows_handle_is_host_owned(handle) -> bool:
    """Reject null/foreign-owned DACLs and write grants to untrusted principals.

Current user, SYSTEM and Administrators are the trusted host principals. Unix
mode bits and st_uid are never treated as Windows permission evidence.
Unknown write ACE types fail closed.
"""
    api, advapi = _security_functions()
    owner, dacl, descriptor = (ctypes.c_void_p() for _ in range(3))
    code = advapi.GetSecurityInfo(handle, 1, 1 | 4, ctypes.byref(owner), None,
                                 ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if code:
        from .windows_process import win_error
        raise win_error("GetSecurityInfo(host ownership)", code)
    try:
        trusted = {host_user_key(), "S-1-5-18", "S-1-5-32-544"}
        if not owner or not dacl or _sid_string(api, advapi, owner) not in trusted:
            return False
        count = ctypes.c_uint16.from_address(dacl.value + 4).value
        for index in range(count):
            ace = ctypes.c_void_p()
            if not advapi.GetAce(dacl, index, ctypes.byref(ace)):
                return False
            ace_type = ctypes.c_uint8.from_address(ace.value).value
            flags = ctypes.c_uint8.from_address(ace.value + 1).value
            if flags & 8 or ace_type == 1:  # INHERIT_ONLY or access denied
                continue
            if ace_type != 0:
                return False
            mask = ctypes.c_uint32.from_address(ace.value + 4).value
            write_rights = 0x40000000 | 0x10000000 | 0x000D0116
            if mask & write_rights and _sid_string(api, advapi, ace.value + 8) not in trusted:
                return False
        return True
    finally:
        if descriptor:
            api.LocalFree(descriptor)


def assert_host_owned(path: Path | str) -> None:
    path = _absolute(path)
    if os.name == "nt":
        with pinned_windows_path(path, access=0x80 | 0x20000) as handle:
            if not windows_handle_is_host_owned(handle):
                raise UnsafePathError("path is not protected by a host-owned Windows DACL")
        return
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise UnsafePathError("path is not private to the host user")


def make_private_directory(path: Path | str) -> Path:
    path = _absolute(path)
    if os.name == "nt":
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current = current / component
            try:
                with pinned_windows_path(current):
                    pass
            except FileNotFoundError:
                try:
                    file_os.mkdir(current, mode=0o700)
                except FileExistsError:
                    # Another creator may have won; the next pin verifies it.
                    with pinned_windows_path(current):
                        pass
    else:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    assert_no_reparse(path)
    if os.name != "nt":
        assert_host_owned(path)
        return path
    from .windows_process import check, BOOL, win_error
    api, advapi = _security_functions()
    descriptor, dacl = ctypes.c_void_p(), ctypes.c_void_p()
    sddl = f"D:P(A;OICI;FA;;;{host_user_key()})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
    check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1,
          ctypes.byref(descriptor), None), "ConvertStringSecurityDescriptorToSecurityDescriptorW")
    try:
        present, defaulted = BOOL(), BOOL()
        check(advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
              ctypes.byref(dacl), ctypes.byref(defaulted)), "GetSecurityDescriptorDacl")
        with pinned_windows_path(path, access=0x40000 | 0x20000 | 0x80) as handle:
            code = advapi.SetSecurityInfo(handle, 1, 4 | 0x80000000, None, None, dacl, None)
            if code:
                raise win_error("SetSecurityInfo(private directory)", code)
    finally:
        api.LocalFree(descriptor)
    assert_host_owned(path)
    return path


class _NativeStat:
    def __init__(self, info, owned: bool):
        self._info, self.native_host_owned = info, owned

    def __getattr__(self, name):
        return getattr(self._info, name)


def metadata_is_host_owned(info, *, private_mask: int = 0o022) -> bool:
    if os.name == "nt":
        return bool(getattr(info, "native_host_owned", False))
    return info.st_uid == os.geteuid() and not info.st_mode & private_mask


class _FileOS:
    """Scoped filesystem adapter; never modifies the real os module globally."""
    _WINDOWS_FLAGS = {"O_DIRECTORY": 0x10000000, "O_NOFOLLOW": 0x20000000,
                      "O_CLOEXEC": 0x40000000, "O_NONBLOCK": 0}

    def __getattr__(self, name):
        if os.name == "nt" and name in self._WINDOWS_FLAGS:
            return self._WINDOWS_FLAGS[name]
        return getattr(os, name)

    def _path(self, path, directory=None):
        if directory is None:
            return Path(path).absolute()
        if not isinstance(directory, NativeDirectory) or directory.closed:
            raise ValueError("dir_fd must be a live pinned Windows directory")
        return directory.path / _relative(path)

    def open(self, path, flags, mode=0o777, *, dir_fd=None, allow_readonly_hardlinks=False):
        if os.name != "nt":
            return os.open(path, flags, mode, dir_fd=dir_fd)
        path = self._path(path, dir_fd)
        if (flags & self._WINDOWS_FLAGS["O_DIRECTORY"]
                or (not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT) and path.is_dir())):
            return NativeDirectory(path)
        flags &= ~(0x10000000 | 0x20000000 | 0x40000000)
        return safe_open(path.parent, path.name, flags, mode, share_delete=True,
                         allow_readonly_hardlinks=allow_readonly_hardlinks)

    def close(self, descriptor):
        if isinstance(descriptor, NativeDirectory):
            return descriptor.close()
        return os.close(descriptor)

    def set_inheritable(self, descriptor, value):
        if isinstance(descriptor, NativeDirectory):
            if value:
                raise ValueError("pinned directories are never inherited")
            return
        return os.set_inheritable(descriptor, value)

    def fstat(self, descriptor):
        if os.name != "nt":
            return os.fstat(descriptor)
        import msvcrt
        if isinstance(descriptor, NativeDirectory):
            return _NativeStat(descriptor.path.stat(), windows_handle_is_host_owned(descriptor.handle))
        return _NativeStat(os.fstat(descriptor),
                           windows_handle_is_host_owned(msvcrt.get_osfhandle(descriptor)))

    def fsync(self, descriptor):
        if isinstance(descriptor, NativeDirectory):
            # Native publication uses flushed files and MoveFileEx WRITE_THROUGH.
            # Windows does not expose POSIX directory-fsync semantics.
            return
        return os.fsync(descriptor)

    def listdir(self, path="."):
        return os.listdir(path.path if isinstance(path, NativeDirectory) else path)

    def scandir(self, path="."):
        return os.scandir(path.path if isinstance(path, NativeDirectory) else path)

    def dup(self, descriptor):
        if isinstance(descriptor, NativeDirectory):
            return NativeDirectory(descriptor.path)
        return os.dup(descriptor)

    def mkdir(self, path, mode=0o777, *, dir_fd=None):
        if os.name != "nt":
            return os.mkdir(path, mode, dir_fd=dir_fd)
        destination = self._path(path, dir_fd)
        with pinned_windows_path(destination.parent):
            return os.mkdir(destination, mode)

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        if os.name != "nt":
            return os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        value = self._path(path, dir_fd)
        with pinned_windows_path(value, access=0x80 | 0x20000) as handle:
            return _NativeStat(value.stat(), windows_handle_is_host_owned(handle))

    def unlink(self, path, *, dir_fd=None):
        if os.name != "nt":
            return os.unlink(path, dir_fd=dir_fd)
        destination = self._path(path, dir_fd)
        with pinned_windows_path(destination.parent):
            return os.unlink(destination)

    def _move(self, source, destination, source_fd, destination_fd, replace):
        if os.name != "nt":
            function = os.replace if replace else os.rename
            return function(source, destination, src_dir_fd=source_fd, dst_dir_fd=destination_fd)
        from .windows_process import kernel32, check, DWORD, BOOL
        source, destination = self._path(source, source_fd), self._path(destination, destination_fd)
        api = kernel32()
        api.MoveFileExW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, DWORD]
        api.MoveFileExW.restype = BOOL
        with pinned_windows_path(source.parent), pinned_windows_path(destination.parent):
            check(api.MoveFileExW(str(source), str(destination), (1 if replace else 0) | 8),
                  "MoveFileExW(write through)")

    def rename(self, source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        return self._move(source, destination, src_dir_fd, dst_dir_fd, False)

    def replace(self, source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        return self._move(source, destination, src_dir_fd, dst_dir_fd, True)

    def link(self, source, destination, *, src_dir_fd=None, dst_dir_fd=None,
             follow_symlinks=True):
        if os.name != "nt":
            return os.link(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd,
                           follow_symlinks=follow_symlinks)
        source, destination = self._path(source, src_dir_fd), self._path(destination, dst_dir_fd)
        from .windows_process import kernel32, check, BOOL
        api = kernel32()
        api.CreateHardLinkW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p]
        api.CreateHardLinkW.restype = BOOL
        with pinned_windows_path(source, share_delete=True), pinned_windows_path(destination.parent):
            check(api.CreateHardLinkW(str(destination), str(source), None), "CreateHardLinkW")

    def fchmod(self, descriptor, mode):
        if os.name != "nt":
            return os.fchmod(descriptor, mode)
        # Windows chmod is a DOS read-only attribute, not a DACL. Handle-based
        # FileBasicInfo preserves the other attributes without a path race.
        from .windows_process import kernel32, BOOL, DWORD, check
        import msvcrt

        class BasicInfo(ctypes.Structure):
            _fields_ = [("times", ctypes.c_int64 * 4), ("attributes", DWORD)]

        api = kernel32()
        api.GetFileInformationByHandleEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                    ctypes.c_void_p, DWORD]
        api.GetFileInformationByHandleEx.restype = BOOL
        api.SetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                                   ctypes.c_void_p, DWORD]
        api.SetFileInformationByHandle.restype = BOOL
        handle = msvcrt.get_osfhandle(descriptor)
        info = BasicInfo()
        check(api.GetFileInformationByHandleEx(handle, 0, ctypes.byref(info), ctypes.sizeof(info)),
              "GetFileInformationByHandleEx(FileBasicInfo)")
        info.attributes = ((info.attributes & ~1) if mode & 0o200 else info.attributes | 1)
        check(api.SetFileInformationByHandle(handle, 0, ctypes.byref(info), ctypes.sizeof(info)),
              "SetFileInformationByHandle(FileBasicInfo)")


file_os = _FileOS()
