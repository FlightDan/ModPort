"""Integrity-checked Gradle Wrapper distribution archives shared by Runs.

Only the original ZIP is stored here. Each caller receives a private copy in
its own writable ``GRADLE_USER_HOME``; Gradle extracts it there as usual.
Archives are accepted only when their bytes match a checksum declared by the
Wrapper properties or fetched from Gradle for an exact official URL.
"""
from __future__ import annotations

from dataclasses import dataclass
from . import platform_files as fcntl
import hashlib
from http.client import HTTPException
import ipaddress
import json
from .platform_files import file_os as os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import time
from typing import Any
from .platform_files import metadata_is_host_owned
import urllib.error
import urllib.parse
import urllib.request

from .environment_build_cache import (
    _assert_cache_chain,
    _assert_cache_ownership,
    _fsync_directory,
    _json_bytes,
    _read_regular,
    _safe_path,
    _sha256_file,
    _store_lock,
    _write_manifest,
)


_SCHEMA = "gradle-wrapper-distribution-v1"
_CACHE_DIRECTORY = "wrapper-distributions-v1"
_CHECKSUM = re.compile(r"[0-9a-f]{64}\Z")
_DIST_FILENAME = re.compile(r"gradle-[0-9]+(?:\.[0-9]+){1,2}-(?:bin|all)\.zip\Z")
_PROPERTY_KEYS = {
    "distributionUrl", "distributionSha256Sum", "distributionBase",
    "distributionPath", "zipStoreBase", "zipStorePath",
}
_MAX_PROPERTIES_BYTES = 128 * 1024
_MAX_CHECKSUM_BYTES = 1024
_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024
_OFFICIAL_HOSTS = {"services.gradle.org", "downloads.gradle.org"}


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Gradle Wrapper cache deadline expired")
    return remaining


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None:
        _remaining_seconds(deadline)


@dataclass(frozen=True, slots=True)
class WrapperDistribution:
    url: str
    filename: str
    zip_relative_path: str
    distribution_relative_path: str
    marker_relative_path: str
    configured_sha256: str | None


def _property_value(raw: str) -> str:
    """Decode the escapes used by Java Properties for one value."""
    result: list[str] = []
    index = 0
    while index < len(raw):
        char = raw[index]
        index += 1
        if char != "\\":
            result.append(char)
            continue
        if index >= len(raw):
            continue
        escaped = raw[index]
        index += 1
        simple = {"t": "\t", "n": "\n", "r": "\r", "f": "\f"}
        if escaped in simple:
            result.append(simple[escaped])
        elif escaped == "u":
            digits = raw[index:index + 4]
            if len(digits) != 4 or not re.fullmatch(r"[0-9a-fA-F]{4}", digits):
                raise ValueError("invalid Unicode escape in Wrapper properties")
            result.append(chr(int(digits, 16)))
            index += 4
        else:
            # Java Properties removes the escape slash for separators and
            # unknown escaped characters, including the colon in https\://.
            result.append(escaped)
    return "".join(result).strip()


def _logical_property_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for physical in text.splitlines():
        line = physical.lstrip(" \t\f")
        if pending:
            pending += line
        else:
            pending = line
        slash_count = len(pending) - len(pending.rstrip("\\"))
        if slash_count % 2:
            pending = pending[:-1]
            continue
        lines.append(pending)
        pending = ""
    if pending:
        lines.append(pending)
    return lines


def _wrapper_properties(path: Path) -> dict[str, str]:
    path = _safe_path(Path(path))
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_PROPERTIES_BYTES:
            raise ValueError("Wrapper properties must be a bounded regular file")
        data = os.read(descriptor, _MAX_PROPERTIES_BYTES + 1)
        if len(data) > _MAX_PROPERTIES_BYTES:
            raise ValueError("Wrapper properties exceed the size limit")
    finally:
        os.close(descriptor)
    text = data.decode("latin-1")
    values: dict[str, str] = {}
    for line in _logical_property_lines(text):
        stripped = line.lstrip(" \t\f")
        if not stripped or stripped.startswith(("#", "!")):
            continue
        match = re.match(r"([^\s:=]+)(?:\s*[=:]\s*|\s+)(.*)\Z", stripped)
        if match and match.group(1) in _PROPERTY_KEYS:
            values[match.group(1)] = _property_value(match.group(2))
    return values


def _valid_distribution_url(value: str) -> tuple[urllib.parse.SplitResult, str, bool]:
    if (not isinstance(value, str) or not value or len(value) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in value)
            or "%" in value or "\\" in value):
        raise ValueError("unsafe Gradle distribution URL")
    try:
        value.encode("ascii")
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("unsafe Gradle distribution URL") from exc
    host = parsed.hostname.lower() if parsed.hostname else ""
    if (parsed.scheme != "https" or not host or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or port not in (None, 443) or "@" in parsed.netloc
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or host.startswith(".") or host.endswith(".") or ".." in host
            or host == "localhost" or host.endswith((".localhost", ".local"))):
        raise ValueError("unsafe Gradle distribution URL")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP-literal Gradle distribution URLs are forbidden")
    if not host or "." not in host:
        raise ValueError("Gradle distribution URL must use a public DNS name")
    parts = parsed.path.split("/")
    if (len(parts) != 3 or parts[0] != "" or parts[1] != "distributions"
            or not _DIST_FILENAME.fullmatch(parts[2])):
        raise ValueError("Gradle distribution path must be a versioned distribution ZIP")
    official = (
        host == "services.gradle.org"
        and value == f"https://services.gradle.org{parsed.path}"
    )
    return parsed, parts[2], official


def _gradle_url_hash(url: str) -> str:
    """Match Gradle PathAssembler's MD5-as-base36 distribution directory."""
    value = int.from_bytes(hashlib.md5(url.encode("utf-8")).digest(), "big")
    if value == 0:
        return "0"
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = digits[remainder] + result
    return result


def load_wrapper_distribution(properties_path: Path) -> WrapperDistribution | None:
    """Read the supported Gradle Wrapper settings without following links.

    Custom distribution bases and paths remain Gradle's responsibility. The
    cache only seeds the default private GRADLE_USER_HOME ``wrapper/dists``.
    """
    values = _wrapper_properties(properties_path)
    url = values.get("distributionUrl")
    if not url:
        return None
    _, filename, _ = _valid_distribution_url(url)
    if (values.get("distributionBase", "GRADLE_USER_HOME") != "GRADLE_USER_HOME"
            or values.get("zipStoreBase", "GRADLE_USER_HOME") != "GRADLE_USER_HOME"
            or values.get("distributionPath", "wrapper/dists") != "wrapper/dists"
            or values.get("zipStorePath", "wrapper/dists") != "wrapper/dists"):
        return None
    checksum = values.get("distributionSha256Sum")
    if checksum is not None:
        checksum = checksum.lower()
        if not _CHECKSUM.fullmatch(checksum):
            return None
    return _distribution_layout(url, filename, checksum)


def _distribution_layout(url: str, filename: str,
                         checksum: str | None) -> WrapperDistribution:
    dist_root = Path(filename[:-4])
    wrapper_root = Path("wrapper") / "dists" / dist_root / _gradle_url_hash(url)
    return WrapperDistribution(
        url=url,
        filename=filename,
        zip_relative_path=(wrapper_root / filename).as_posix(),
        distribution_relative_path=wrapper_root.as_posix(),
        marker_relative_path=(wrapper_root / f"{filename}.ok").as_posix(),
        configured_sha256=checksum,
    )


def _is_official_checksum_url(distribution_url: str) -> bool:
    parsed, _, official = _valid_distribution_url(distribution_url)
    return official and parsed.netloc == "services.gradle.org"


def _expected_github_release_path(expected_path: str) -> str | None:
    filename = expected_path.rsplit("/", 1)[-1]
    distribution_filename = filename.removesuffix(".sha256")
    if not _DIST_FILENAME.fullmatch(distribution_filename):
        return None
    suffix = "-bin.zip" if distribution_filename.endswith("-bin.zip") else "-all.zip"
    version = distribution_filename[len("gradle-"):-len(suffix)]
    return (
        f"/gradle/gradle-distributions/releases/download/v{version}/"
        f"{filename}"
    )


def _redirect_is_safe(
    source_url: str,
    destination_url: str,
    *,
    expected_path: str,
) -> bool:
    try:
        source = urllib.parse.urlsplit(source_url)
        destination = urllib.parse.urlsplit(destination_url)
        source_host = (source.hostname or "").lower()
        target_host = (destination.hostname or "").lower()
        target_port = destination.port
    except ValueError:
        return False
    if (destination.scheme != "https" or destination.username is not None
            or destination.password is not None or destination.fragment
            or target_port not in (None, 443) or "@" in destination.netloc
            or not re.fullmatch(r"[a-z0-9.-]+", target_host)
            or target_host == "localhost" or target_host.endswith((".localhost", ".local"))):
        return False
    try:
        ipaddress.ip_address(target_host)
    except ValueError:
        pass
    else:
        return False

    github_path = _expected_github_release_path(expected_path)
    if github_path is None:
        return False
    if source_host in _OFFICIAL_HOSTS:
        if target_host in _OFFICIAL_HOSTS:
            return destination.path == expected_path and not destination.query
        return (target_host == "github.com" and destination.path == github_path
                and not destination.query)
    if source_host == "github.com":
        return (target_host == "release-assets.githubusercontent.com"
                and re.fullmatch(
                    r"/github-production-release-asset/[0-9]+/[0-9a-fA-F-]{36}",
                    destination.path) is not None
                and 0 < len(destination.query) <= 4096)
    if source_host == "release-assets.githubusercontent.com":
        # GitHub may rotate a signed asset URL once. Keep it on the exact
        # asset host and require the same opaque release-asset path.
        return (target_host == source_host and destination.path == source.path
                and 0 < len(destination.query) <= 4096)
    return False


def _trusted_final_url(response_url: str, expected_path: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(response_url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if (parsed.scheme != "https" or parsed.username is not None
            or parsed.password is not None or parsed.fragment or port not in (None, 443)
            or "@" in parsed.netloc):
        return False
    if host in _OFFICIAL_HOSTS:
        return parsed.path == expected_path and not parsed.query
    if host == "github.com":
        return parsed.path == _expected_github_release_path(expected_path) and not parsed.query
    return (host == "release-assets.githubusercontent.com"
            and re.fullmatch(
                r"/github-production-release-asset/[0-9]+/[0-9a-fA-F-]{36}",
                parsed.path) is not None
            and 0 < len(parsed.query) <= 4096)


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, expected_path: str, deadline: float):
        super().__init__()
        self.expected_path = expected_path
        self.deadline = deadline
        self.max_redirections = 3
        self._redirect_count = 0

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if not location:
            return None
        newurl = urllib.parse.urljoin(req.full_url, location)
        self._redirect_count += 1
        if self._redirect_count > self.max_redirections:
            fp.close()
            raise urllib.error.URLError("Gradle distribution exceeded the redirect limit")
        if not _redirect_is_safe(
            req.full_url, newurl,
                expected_path=self.expected_path):
            fp.close()
            raise urllib.error.URLError("Gradle distribution redirected to an unsafe URL")
        # The standard handler drains the entire redirect body before opening
        # the next URL. Close it instead: an unbounded body could defeat the
        # cache deadline before the next request starts.
        fp.close()
        request = urllib.request.Request(
            newurl, headers={"User-Agent": "ModPort Gradle wrapper cache"},
            origin_req_host=req.origin_req_host, unverifiable=True)
        return self.parent.open(
            request, timeout=min(10.0, _remaining_seconds(self.deadline)))

    http_error_301 = http_error_302
    http_error_303 = http_error_302
    http_error_307 = http_error_302
    http_error_308 = http_error_302


def _open_https_response(
    url: str,
    *,
    expected_path: str,
    timeout: float,
):
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(_SafeRedirectHandler(
        expected_path=expected_path, deadline=deadline))
    request = urllib.request.Request(url, headers={"User-Agent": "ModPort Gradle wrapper cache"})
    return opener.open(request, timeout=min(10.0, _remaining_seconds(deadline)))


def _read_response_chunk(response: Any, size: int, deadline: float) -> bytes:
    """Read one transport chunk without renewing the overall fetch budget."""
    remaining = _remaining_seconds(deadline)
    # urllib's initial timeout is per socket operation. Shorten it before
    # every subsequent read so a late stalled chunk cannot reuse the full
    # transfer budget. A test response may have no underlying socket.
    socket = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if socket is not None:
        socket.settimeout(min(10.0, remaining))
    reader = getattr(response, "read1", None) or response.read
    block = reader(size)
    _remaining_seconds(deadline)
    return block


def _published_sha256(url: str, timeout: float | None = None, *,
                      deadline: float | None = None) -> tuple[str, str]:
    if not _is_official_checksum_url(url):
        raise ValueError("Gradle-published checksum is available only for the exact official URL")
    path = urllib.parse.urlsplit(url).path + ".sha256"
    checksum_urls = (
        url + ".sha256",
        "https://downloads.gradle.org" + path,
    )
    if deadline is None:
        if timeout is None:
            raise TypeError("timeout or deadline is required")
        deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    for checksum_url in dict.fromkeys(checksum_urls):
        try:
            with _open_https_response(
                    checksum_url, expected_path=path,
                    timeout=_remaining_seconds(deadline)) as response:
                final = urllib.parse.urlsplit(response.geturl())
                if (getattr(response, "status", 200) != 200
                        or not _trusted_final_url(response.geturl(), path)
                        or final.scheme != "https"
                        or (final.hostname or "").lower() not in {
                            *_OFFICIAL_HOSTS, "github.com", "release-assets.githubusercontent.com"}
                        or final.username is not None or final.password is not None):
                    raise ValueError("Gradle checksum response used an unexpected host")
                checksum_body = bytearray()
                while len(checksum_body) <= _MAX_CHECKSUM_BYTES:
                    block = _read_response_chunk(
                        response, _MAX_CHECKSUM_BYTES + 1 - len(checksum_body),
                        deadline)
                    if not block:
                        break
                    checksum_body.extend(block)
                data = bytes(checksum_body)
            if len(data) > _MAX_CHECKSUM_BYTES:
                raise ValueError("Gradle checksum response exceeds the size limit")
            try:
                text = data.decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise ValueError("Gradle checksum response is not ASCII") from exc
            match = re.fullmatch(r"([0-9a-fA-F]{64})(?:\s+[^\s]+)?", text)
            if match is None:
                raise ValueError("Gradle checksum response is malformed")
            return match.group(1).lower(), checksum_url
        except TimeoutError as exc:
            _remaining_seconds(deadline)
            last_error = exc
        except (OSError, ValueError, HTTPException, urllib.error.URLError) as exc:
            last_error = exc
    raise ValueError("Gradle-published checksum was unavailable from official hosts") from last_error


def _entry_key(url: str) -> str:
    return hashlib.sha256(_json_bytes({"kind": _SCHEMA, "distribution_url": url})).hexdigest()


def _entry_manifest(
    url: str,
    sha256sum: str,
    size: int,
    checksum_kind: str,
    checksum_url: str | None = None,
) -> dict[str, Any]:
    checksum: dict[str, str] = {"kind": checksum_kind, "sha256": sha256sum}
    if checksum_kind == "gradle_published":
        checksum["url"] = checksum_url or url + ".sha256"
    payload: dict[str, Any] = {
        "schema_version": 1,
        "kind": _SCHEMA,
        "cache_key": _entry_key(url),
        "distribution_url": url,
        "distribution_sha256": sha256sum,
        "distribution_size": size,
        "checksum": checksum,
    }
    payload["manifest_sha256"] = hashlib.sha256(_json_bytes(payload)).hexdigest()
    return payload


def _validate_entry(entry: Path, url: str, root: Path, *,
                    deadline: float | None = None) -> dict[str, Any]:
    _check_deadline(deadline)
    entry = _safe_path(entry)
    if not entry.is_dir() or entry.is_symlink():
        raise ValueError("Wrapper distribution cache entry is not a directory")
    _assert_cache_chain(root, entry)
    names: set[str] = set()
    for path in entry.iterdir():
        _check_deadline(deadline)
        if len(names) >= 2:
            raise ValueError("Wrapper distribution cache contains unmanifested entries")
        names.add(path.name)
    if names != {"distribution.zip", "manifest.json"}:
        raise ValueError("Wrapper distribution cache contains unmanifested entries")
    archive = entry / "distribution.zip"
    manifest_path = entry / "manifest.json"
    _assert_cache_ownership(archive)
    _assert_cache_ownership(manifest_path)
    manifest = json.loads(_read_regular(
        manifest_path, max_bytes=16 * 1024, deadline=deadline))
    expected_fields = {
        "schema_version", "kind", "cache_key", "distribution_url",
        "distribution_sha256", "distribution_size", "checksum", "manifest_sha256",
    }
    if (not isinstance(manifest, dict) or set(manifest) != expected_fields
            or manifest["schema_version"] != 1 or manifest["kind"] != _SCHEMA
            or manifest["cache_key"] != _entry_key(url)
            or manifest["distribution_url"] != url):
        raise ValueError("Wrapper distribution cache identity mismatch")
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    _check_deadline(deadline)
    if (not isinstance(manifest["manifest_sha256"], str)
            or hashlib.sha256(_json_bytes(payload)).hexdigest() != manifest["manifest_sha256"]):
        raise ValueError("Wrapper distribution cache manifest digest mismatch")
    _check_deadline(deadline)
    digest = manifest["distribution_sha256"]
    checksum = manifest["checksum"]
    if not isinstance(digest, str) or not _CHECKSUM.fullmatch(digest):
        raise ValueError("Wrapper distribution cache checksum is invalid")
    if (not isinstance(checksum, dict) or checksum.get("sha256") != digest
            or checksum.get("kind") not in {"wrapper_properties", "gradle_published"}):
        raise ValueError("Wrapper distribution cache checksum provenance is invalid")
    if checksum["kind"] == "gradle_published":
        allowed_checksum_urls = {
            url + ".sha256",
            "https://downloads.gradle.org" + urllib.parse.urlsplit(url).path + ".sha256",
        }
        if (set(checksum) != {"kind", "sha256", "url"}
                or checksum["url"] not in allowed_checksum_urls
                or not _is_official_checksum_url(url)):
            raise ValueError("Wrapper cache has invalid Gradle checksum provenance")
    elif set(checksum) != {"kind", "sha256"}:
        raise ValueError("Wrapper cache has invalid Wrapper checksum provenance")
    actual_digest, actual_size = _sha256_file(
        archive, max_bytes=_MAX_ARCHIVE_BYTES, deadline=deadline)
    if (actual_digest != digest or actual_size != manifest["distribution_size"]
            or actual_size <= 0 or actual_size > _MAX_ARCHIVE_BYTES):
        raise ValueError("Wrapper distribution archive failed its trusted checksum")
    if not zipfile_is_valid(archive, _valid_distribution_url(url)[1], deadline=deadline):
        raise ValueError("Wrapper distribution archive is not a valid ZIP")
    _check_deadline(deadline)
    return manifest


def zipfile_is_valid(path: Path, expected_filename: str | None = None, *,
                     deadline: float | None = None) -> bool:
    import zipfile
    _check_deadline(deadline)
    filename = expected_filename or path.name
    match = _DIST_FILENAME.fullmatch(filename)
    if match is None:
        # Temporary download names are validated against their owning
        # distribution before this function is called.
        return True
    expected_root = filename[:-len("-bin.zip")] if filename.endswith("-bin.zip") else filename[:-len("-all.zip")]
    seen: set[str] = set()
    try:
        with path.open("rb") as archive:
            endrec = zipfile._EndRecData(archive)
            _check_deadline(deadline)
            if endrec is None:
                return False
            if (endrec[zipfile._ECD_DISK_NUMBER] != 0
                    or endrec[zipfile._ECD_DISK_START] != 0
                    or endrec[zipfile._ECD_ENTRIES_THIS_DISK]
                    != endrec[zipfile._ECD_ENTRIES_TOTAL]):
                return False
            count = endrec[zipfile._ECD_ENTRIES_TOTAL]
            directory_size = endrec[zipfile._ECD_SIZE]
            directory_offset = endrec[zipfile._ECD_OFFSET]
            eocd_offset = endrec[zipfile._ECD_LOCATION]
            # ZipFile accepts prepended self-extracting data by shifting the
            # recorded central-directory offset. Keep that behavior while
            # parsing each bounded record ourselves instead of asking ZipFile
            # to read and parse the complete directory in one operation.
            concat = eocd_offset - directory_size - directory_offset
            start = directory_offset + concat
            end = start + directory_size
            archive_size = os.fstat(archive.fileno()).st_size
            if (concat < 0 or start < 0 or end > eocd_offset
                    or end > archive_size):
                return False
            archive.seek(start)
            for _ in range(count):
                _check_deadline(deadline)
                header = archive.read(zipfile.sizeCentralDir)
                _check_deadline(deadline)
                if len(header) != zipfile.sizeCentralDir:
                    return False
                fields = struct.unpack(zipfile.structCentralDir, header)
                if fields[zipfile._CD_SIGNATURE] != zipfile.stringCentralDir:
                    return False
                name_length = fields[zipfile._CD_FILENAME_LENGTH]
                extra_length = fields[zipfile._CD_EXTRA_FIELD_LENGTH]
                comment_length = fields[zipfile._CD_COMMENT_LENGTH]
                raw_name_bytes = archive.read(name_length)
                _check_deadline(deadline)
                extra = archive.read(extra_length)
                _check_deadline(deadline)
                comment = archive.read(comment_length)
                _check_deadline(deadline)
                if (len(raw_name_bytes) != name_length or len(extra) != extra_length
                        or len(comment) != comment_length
                        or archive.tell() > end):
                    return False
                encoding = "utf-8" if fields[zipfile._CD_FLAG_BITS] & zipfile._MASK_UTF_FILENAME else "cp437"
                raw_name = raw_name_bytes.decode(encoding)
                if not raw_name or "\x00" in raw_name:
                    return False
                if fields[zipfile._CD_DISK_NUMBER_START] != 0:
                    return False
                if (not raw_name or "\x00" in raw_name
                        or raw_name.startswith("/") or "\\" in raw_name):
                    return False
                components = raw_name.rstrip("/").split("/")
                if (not components or any(part in {"", ".", ".."} for part in components)
                        or ":" in components[0] or components[0] != expected_root):
                    return False
                normalized = "/".join(components)
                if normalized in seen:
                    return False
                seen.add(normalized)
                mode = fields[zipfile._CD_EXTERNAL_FILE_ATTRIBUTES] >> 16
                file_type = stat.S_IFMT(mode)
                if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                    return False
                if raw_name.endswith("/") != (file_type == stat.S_IFDIR):
                    return False
            if archive.tell() != end:
                return False
            _check_deadline(deadline)
    except TimeoutError:
        raise
    except (OSError, ValueError, zipfile.BadZipFile, struct.error,
            UnicodeDecodeError, IndexError):
        return False
    return bool(seen)


def _write_download(url: str, destination: Path, *, timeout: float | None = None,
                    deadline: float | None = None) -> tuple[str, int]:
    expected_path = urllib.parse.urlsplit(url).path
    if deadline is None:
        if timeout is None:
            raise TypeError("timeout or deadline is required")
        deadline = time.monotonic() + timeout
    _check_deadline(deadline)
    digest = hashlib.sha256()
    total = 0
    with _open_https_response(
            url, expected_path=expected_path,
            timeout=_remaining_seconds(deadline)) as response:
        final = urllib.parse.urlsplit(response.geturl())
        if (getattr(response, "status", 200) != 200 or final.scheme != "https"
                or not _trusted_final_url(response.geturl(), expected_path)
                or final.username is not None or final.password is not None):
            raise ValueError("Gradle distribution response used an unexpected URL")
        fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            while True:
                _check_deadline(deadline)
                block = _read_response_chunk(
                    response, min(_COPY_CHUNK, _MAX_ARCHIVE_BYTES + 1 - total),
                    deadline)
                if not block:
                    break
                total += len(block)
                if total > _MAX_ARCHIVE_BYTES:
                    raise ValueError("Gradle distribution exceeds the size limit")
                digest.update(block)
                output.write(block)
                _check_deadline(deadline)
            output.flush()
            os.fsync(output.fileno())
            _check_deadline(deadline)
    if total <= 0:
        raise ValueError("Gradle distribution download is empty")
    os.chmod(destination, 0o444)
    _check_deadline(deadline)
    return digest.hexdigest(), total


def _copy_verified(source: Path, destination: Path, expected_sha256: str, *,
                   deadline: float | None = None) -> None:
    _check_deadline(deadline)
    source = _safe_path(source)
    destination = _safe_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(destination.parent)
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > _MAX_ARCHIVE_BYTES:
            raise ValueError("Wrapper distribution source is not a bounded regular file")
        existing = None
        try:
            existing = destination.lstat()
        except FileNotFoundError:
            pass
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise UnsafeWrapperDistributionPath(
                "Wrapper distribution destination is a symlink or special file")
        if existing is not None:
            if _sha256_file(destination, max_bytes=_MAX_ARCHIVE_BYTES,
                             deadline=deadline)[0] != expected_sha256:
                raise UnsafeWrapperDistributionPath(
                    "existing phase Wrapper archive failed its trusted checksum")
            return
        temporary_fd, temporary_name = tempfile.mkstemp(prefix=".wrapper-distribution-", dir=destination.parent)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        linked_destination = False
        try:
            with os.fdopen(fd, "rb", closefd=False) as input_stream, os.fdopen(temporary_fd, "wb") as output:
                while True:
                    _check_deadline(deadline)
                    block = input_stream.read(min(_COPY_CHUNK, _MAX_ARCHIVE_BYTES + 1 - size))
                    _check_deadline(deadline)
                    if not block:
                        break
                    size += len(block)
                    if size > _MAX_ARCHIVE_BYTES:
                        raise ValueError("Wrapper distribution exceeds the size limit")
                    digest.update(block)
                    output.write(block)
                    _check_deadline(deadline)
                output.flush()
                os.fsync(output.fileno())
                _check_deadline(deadline)
            after = os.fstat(fd)
            if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                    or size != before.st_size):
                raise ValueError("Wrapper distribution changed while copying")
            if digest.hexdigest() != expected_sha256:
                raise ValueError("Wrapper distribution failed its trusted checksum before materialization")
            if not zipfile_is_valid(temporary, destination.name, deadline=deadline):
                raise ValueError("Wrapper distribution is not a valid ZIP")
            os.chmod(temporary, 0o600)
            _check_deadline(deadline)
            try:
                os.link(temporary, destination, follow_symlinks=False)
                linked_destination = True
            except FileExistsError:
                if _sha256_file(destination, max_bytes=_MAX_ARCHIVE_BYTES,
                                deadline=deadline)[0] != expected_sha256:
                    raise UnsafeWrapperDistributionPath(
                        "existing phase Wrapper archive failed its trusted checksum")
            _check_deadline(deadline)
            _fsync_directory(destination.parent)
            _check_deadline(deadline)
        except TimeoutError:
            if linked_destination:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
            raise
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


class UnsafeWrapperDistributionPath(ValueError):
    """A Wrapper-owned path contains a link, special file, or bad archive."""


def _scan_regular_tree(directory: Path, *, deadline: float | None = None) -> None:
    _check_deadline(deadline)
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise UnsafeWrapperDistributionPath("Gradle Wrapper installation path is unsafe")
    for current, directories, files in os.walk(directory, followlinks=False):
        _check_deadline(deadline)
        current_path = Path(current)
        for name in directories:
            _check_deadline(deadline)
            node = current_path / name
            if not stat.S_ISDIR(node.lstat().st_mode):
                raise UnsafeWrapperDistributionPath(
                    "Gradle Wrapper installation contains a symlink or special file")
        for name in files:
            _check_deadline(deadline)
            node = current_path / name
            if not stat.S_ISREG(node.lstat().st_mode):
                raise UnsafeWrapperDistributionPath(
                    "Gradle Wrapper installation contains a symlink or special file")


def _complete_installation(gradle_home: Path,
                           distribution: WrapperDistribution, *,
                           deadline: float | None = None) -> bool:
    _check_deadline(deadline)
    try:
        wrapper_root = _safe_path(gradle_home / distribution.distribution_relative_path)
    except ValueError as exc:
        raise UnsafeWrapperDistributionPath("Gradle Wrapper installation path is unsafe") from exc
    try:
        root_info = wrapper_root.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(root_info.st_mode):
        raise UnsafeWrapperDistributionPath("Gradle Wrapper cache path is not a directory")
    for child in wrapper_root.iterdir():
        _check_deadline(deadline)
        mode = child.lstat().st_mode
        if child.name.endswith((".ok", ".lck", ".zip")):
            if not stat.S_ISREG(mode):
                raise UnsafeWrapperDistributionPath(
                    "Gradle Wrapper cache path contains a symlink or special file")
        elif not stat.S_ISDIR(mode):
            raise UnsafeWrapperDistributionPath(
                "Gradle Wrapper cache path contains a symlink or special file")

    marker = wrapper_root / Path(distribution.marker_relative_path).name
    try:
        if not stat.S_ISREG(marker.lstat().st_mode):
            raise UnsafeWrapperDistributionPath("Gradle Wrapper marker is unsafe")
    except FileNotFoundError:
        return False

    root_name = distribution.filename[:-len("-bin.zip")] if distribution.filename.endswith(
        "-bin.zip") else distribution.filename[:-len("-all.zip")]
    installation = wrapper_root / root_name
    try:
        if not stat.S_ISDIR(installation.lstat().st_mode):
            raise UnsafeWrapperDistributionPath("Gradle Wrapper installation is unsafe")
    except FileNotFoundError as exc:
        raise UnsafeWrapperDistributionPath(
            "Gradle Wrapper marker exists without an installation") from exc
    _scan_regular_tree(installation, deadline=deadline)
    version = root_name.removeprefix("gradle-")
    for path in (installation / "bin" / "gradle",
                 installation / "lib" / f"gradle-launcher-{version}.jar"):
        _check_deadline(deadline)
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise UnsafeWrapperDistributionPath("Gradle Wrapper installation is incomplete")
        except FileNotFoundError as exc:
            raise UnsafeWrapperDistributionPath(
                "Gradle Wrapper marker exists without a complete installation") from exc
    return True


def _phase_home_state(gradle_home: Path, distribution: WrapperDistribution,
                      expected_sha256: str, *,
                      deadline: float | None = None) -> str:
    _check_deadline(deadline)
    try:
        zip_path = _safe_path(gradle_home / distribution.zip_relative_path)
    except ValueError as exc:
        raise UnsafeWrapperDistributionPath("Gradle Wrapper ZIP path is unsafe") from exc
    installed = _complete_installation(gradle_home, distribution, deadline=deadline)
    try:
        info = zip_path.lstat()
    except FileNotFoundError:
        info = None
    if info is not None:
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeWrapperDistributionPath(
                "Gradle Wrapper ZIP path contains a symlink or special file")
        digest, _ = _sha256_file(
            zip_path, max_bytes=_MAX_ARCHIVE_BYTES, deadline=deadline)
        if digest != expected_sha256:
            raise UnsafeWrapperDistributionPath(
                "existing phase Wrapper archive failed its trusted checksum")
        if not zipfile_is_valid(zip_path, distribution.filename, deadline=deadline):
            raise UnsafeWrapperDistributionPath("existing phase Wrapper archive is unsafe")
        return "already_seeded"
    if installed:
        return "already_installed"
    _check_deadline(deadline)
    return "missing"


def _distribution_lock(root: Path, key: str, timeout: float, *,
                       deadline: float | None = None):
    """Serialize cold fetches for one distribution across concurrent Runs."""
    from contextlib import contextmanager

    @contextmanager
    def locked():
        if deadline is not None:
            _remaining_seconds(deadline)
        root_path = _safe_path(root)
        root_path.mkdir(parents=True, exist_ok=True)
        _assert_cache_chain(root_path, root_path)
        lock_path = _safe_path(root_path / f".wrapper-distribution-{key}.fetch.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not metadata_is_host_owned(info):
                raise ValueError("Wrapper distribution fetch lock is not private")
            lock_deadline = deadline if deadline is not None else time.monotonic() + timeout
            _remaining_seconds(lock_deadline)
            while True:
                try:
                    _remaining_seconds(lock_deadline)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = lock_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Wrapper distribution fetch lock timed out")
                    time.sleep(min(0.05, remaining))
            _remaining_seconds(lock_deadline)
            yield
        finally:
            os.close(fd)
    return locked()


def _phase_lock(gradle_home: Path, key: str, timeout: float, *,
                deadline: float | None = None):
    """Serialize idempotent seed checks and writes within one phase home."""
    from contextlib import contextmanager

    @contextmanager
    def locked():
        if deadline is not None:
            _remaining_seconds(deadline)
        home = _safe_path(Path(gradle_home))
        home.mkdir(parents=True, exist_ok=True)
        _safe_path(home)
        lock_path = _safe_path(home / f".modport-wrapper-{key}.lock")
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not metadata_is_host_owned(info):
                raise UnsafeWrapperDistributionPath("phase Wrapper seed lock is unsafe")
            lock_deadline = deadline if deadline is not None else time.monotonic() + timeout
            _remaining_seconds(lock_deadline)
            while True:
                try:
                    _remaining_seconds(lock_deadline)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    remaining = lock_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("phase Wrapper seed lock timed out")
                    time.sleep(min(0.05, remaining))
            _remaining_seconds(lock_deadline)
            yield home
        finally:
            os.close(fd)
    return locked()


class EnvironmentWrapperDistributionCache:
    """Host-owned cache for checksum-verified Gradle Wrapper ZIP archives."""

    def __init__(self, root: Path):
        self.root = _safe_path(Path(root))

    def _entry(self, url: str) -> Path:
        return self.root / _CACHE_DIRECTORY / _entry_key(url)

    def _materialize(self, distribution: WrapperDistribution,
                     gradle_home: Path, manifest: dict[str, Any], *,
                     state: str, deadline: float) -> dict[str, Any]:
        key = manifest["cache_key"]
        try:
            _remaining_seconds(deadline)
            gradle_home = _safe_path(Path(gradle_home))
            with _phase_lock(gradle_home, key, _remaining_seconds(deadline),
                             deadline=deadline) as home:
                local_state = _phase_home_state(
                    home, distribution, manifest["distribution_sha256"],
                    deadline=deadline)
                if local_state == "missing":
                    _copy_verified(
                        self._entry(distribution.url) / "distribution.zip",
                        home / distribution.zip_relative_path,
                        manifest["distribution_sha256"],
                        deadline=deadline,
                    )
            _remaining_seconds(deadline)
        except UnsafeWrapperDistributionPath:
            raise
        except TimeoutError:
            raise
        except (OSError, ValueError) as exc:
            raise UnsafeWrapperDistributionPath(
                "phase Wrapper distribution path could not be validated") from exc
        return {
            "state": state,
            "cache_key": key,
            "distribution_sha256": manifest["distribution_sha256"],
            "checksum_source": manifest["checksum"]["kind"],
            "materialization": local_state,
        }

    def seed(
        self,
        distribution: WrapperDistribution,
        gradle_home: Path,
        *,
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any] | None:
        """Verify or fetch, atomically publish, then privately materialize a ZIP.

        A missing checksum on a non-official URL is a cache miss. A cached
        entry is always checked against its persisted external checksum
        provenance before it is copied into the caller's Gradle home.
        """
        if timeout_seconds <= 0:
            raise TimeoutError("no time remains to seed the Gradle Wrapper distribution")
        deadline = time.monotonic() + timeout_seconds
        _remaining_seconds(deadline)
        parsed, filename, official = _valid_distribution_url(distribution.url)
        if not official:
            # Project-controlled mirrors and custom distribution URLs remain
            # inside the existing sandboxed Wrapper network path.
            return None
        if filename != distribution.filename:
            raise ValueError("Wrapper properties filename does not match its distribution URL")
        if distribution != _distribution_layout(
                distribution.url, filename, distribution.configured_sha256):
            raise ValueError("invalid Wrapper distribution relative path")
        _remaining_seconds(deadline)
        self.root = _safe_path(self.root)
        _remaining_seconds(deadline)
        key = _entry_key(distribution.url)
        destination = self._entry(distribution.url)
        manifest: dict[str, Any] | None = None
        cache_state = "hit"
        with _distribution_lock(self.root, key, _remaining_seconds(deadline),
                                deadline=deadline):
            _remaining_seconds(deadline)
            if destination.exists() or destination.is_symlink():
                manifest = _validate_entry(
                    destination, distribution.url, self.root, deadline=deadline)
                expected = distribution.configured_sha256
                if expected is not None and expected != manifest["distribution_sha256"]:
                    return None
            else:
                configured = distribution.configured_sha256
                checksum_url = None
                checksum_source = "wrapper_properties" if configured is not None else None
                if configured is None:
                    try:
                        configured, checksum_url = _published_sha256(
                            distribution.url, deadline=deadline)
                    except TimeoutError:
                        _remaining_seconds(deadline)
                        return None
                    except (OSError, ValueError, HTTPException, urllib.error.URLError):
                        return None
                    checksum_source = "gradle_published"
                if not _CHECKSUM.fullmatch(configured):
                    return None

                cache_root = self.root / _CACHE_DIRECTORY
                _remaining_seconds(deadline)
                cache_root.mkdir(parents=True, exist_ok=True)
                _assert_cache_chain(self.root, cache_root)
                temporary = Path(tempfile.mkdtemp(prefix=".pending-wrapper-", dir=cache_root))
                try:
                    _remaining_seconds(deadline)
                    archive = temporary / "distribution.zip"
                    distribution_urls = (
                        distribution.url,
                        "https://downloads.gradle.org" + parsed.path,
                    )
                    downloaded_size = None
                    for index, download_url in enumerate(dict.fromkeys(distribution_urls)):
                        candidate = temporary / f"candidate-{index}.zip"
                        try:
                            actual, size = _write_download(
                                download_url, candidate,
                                deadline=deadline)
                        except TimeoutError:
                            _remaining_seconds(deadline)
                            continue
                        except (OSError, ValueError, HTTPException, urllib.error.URLError):
                            continue
                        if (actual == configured
                                and zipfile_is_valid(
                                    candidate, distribution.filename, deadline=deadline)):
                            _remaining_seconds(deadline)
                            os.rename(candidate, archive)
                            _remaining_seconds(deadline)
                            downloaded_size = size
                            break
                    if downloaded_size is None:
                        return None
                    manifest = _entry_manifest(
                        distribution.url, configured, downloaded_size,
                        checksum_source or "", checksum_url)
                    _write_manifest(temporary / "manifest.json", manifest)
                    _remaining_seconds(deadline)
                    os.chmod(temporary, 0o555)
                    _remaining_seconds(deadline)
                    with _store_lock(self.root,
                                     f".wrapper-distribution-{manifest['cache_key']}.lock",
                                     timeout_seconds=_remaining_seconds(deadline),
                                     deadline=deadline):
                        _remaining_seconds(deadline)
                        if destination.exists() or destination.is_symlink():
                            existing = _validate_entry(
                                destination, distribution.url, self.root,
                                deadline=deadline)
                            if existing["distribution_sha256"] != configured:
                                raise ValueError("immutable Wrapper distribution cache conflict")
                        else:
                            os.rename(temporary, destination)
                            _remaining_seconds(deadline)
                            _fsync_directory(cache_root)
                            _remaining_seconds(deadline)
                    manifest = _validate_entry(
                        destination, distribution.url, self.root, deadline=deadline)
                    cache_state = "stored"
                finally:
                    if temporary.exists():
                        os.chmod(temporary, 0o700)
                        shutil.rmtree(temporary)

        if manifest is None:
            return None
        return self._materialize(
            distribution, gradle_home, manifest,
            state=cache_state, deadline=deadline)
