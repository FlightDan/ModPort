"""Deterministic serialization, validation, and NeoForge selection helpers."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import LockedManifest, MigrationRequest, NeoForgeVersionCandidate


def _json_value(value: Any) -> Any:
    """Convert supported value objects to JSON-compatible immutable data."""

    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_value(value.to_dict())
    if is_dataclass(value):
        return _json_value(asdict(value))
    raise TypeError(f"unsupported value for canonical JSON: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Return stable compact JSON suitable for hashing and lock files."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256(value: Any) -> str:
    """Hash a value's canonical JSON representation with SHA-256."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def manifest_sha256(manifest: LockedManifest) -> str:
    """Hash manifest content, deliberately omitting its optional hash field."""

    if not isinstance(manifest, LockedManifest):
        raise TypeError("manifest_sha256 expects a LockedManifest")
    return sha256(manifest.to_dict(include_hash=False))


def validate_migration_request(request: MigrationRequest) -> None:
    """Validate a migration request, raising ``ValueError`` on bad input."""

    if not isinstance(request, MigrationRequest):
        raise TypeError("request must be a MigrationRequest")
    request.validate()


def validate_manifest(manifest: LockedManifest) -> None:
    """Validate resolved version inputs; optional digests are descriptive metadata."""

    if not isinstance(manifest, LockedManifest):
        raise TypeError("manifest must be a LockedManifest")
    manifest.validate()


_VERSION_PART = re.compile(r"\d+(?:\.\d+)*")


def _version_key(version: str) -> tuple[tuple[int, ...], tuple[int, ...], str]:
    """Sort Minecraft/NeoForge versions without depending on packaging."""

    text = version.strip().lower()
    numbers = _VERSION_PART.search(text)
    numeric = tuple(int(part) for part in numbers.group(0).split(".")) if numbers else ()
    suffix_numbers = tuple(int(part) for part in re.findall(r"\d+", text[numbers.end() :] if numbers else ""))
    # The final text tie-breaker makes selection deterministic for unusual
    # metadata while keeping normal semver ordering intuitive.
    return numeric, suffix_numbers, text


def _candidate(value: Any) -> NeoForgeVersionCandidate:
    if isinstance(value, NeoForgeVersionCandidate):
        value.validate()
        return value
    if isinstance(value, str):
        return NeoForgeVersionCandidate(version=value)
    if isinstance(value, Mapping):
        version = value.get("version", value.get("name"))
        if version is None:
            raise ValueError("candidate mapping requires version or name")
        channel = value.get("channel", value.get("type", value.get("release_type", "stable")))
        minecraft = value.get("minecraft_version", value.get("minecraft"))
        metadata = {
            key: item
            for key, item in value.items()
            if key not in {"version", "name", "channel", "type", "release_type", "minecraft_version", "minecraft"}
        }
        result = NeoForgeVersionCandidate(str(version), str(channel), minecraft, metadata)
        result.validate()
        return result
    raise TypeError(f"unsupported NeoForge candidate: {type(value).__name__}")


def _channel_rank(channel: str) -> int:
    normalized = channel.strip().lower()
    if normalized in {"stable", "release", "released", "final"}:
        return 2
    if normalized in {"beta", "preview"}:
        return 1
    return 0


def select_neoforge_candidate(
    candidates: Iterable[NeoForgeVersionCandidate | Mapping[str, Any] | str],
    *,
    minecraft_version: str | None = None,
) -> NeoForgeVersionCandidate:
    """Select the highest stable NeoForge candidate, or highest beta.

    The function is pure: it neither mutates the iterable nor performs I/O.
    Candidates for a requested Minecraft version are filtered before channel
    preference is applied.  Unsupported channels are ignored, and an empty
    eligible set raises ``ValueError``.
    """

    normalized = [_candidate(value) for value in candidates]
    if minecraft_version is not None:
        normalized = [
            item
            for item in normalized
            if item.minecraft_version in {None, minecraft_version}
        ]
    eligible = [item for item in normalized if _channel_rank(item.channel) > 0]
    if not eligible:
        raise ValueError("no stable or beta NeoForge candidate is available")
    preferred_rank = max(_channel_rank(item.channel) for item in eligible)
    preferred = [item for item in eligible if _channel_rank(item.channel) == preferred_rank]
    return max(preferred, key=lambda item: _version_key(item.version))


def select_neoforge_version(
    candidates: Iterable[NeoForgeVersionCandidate | Mapping[str, Any] | str],
    *,
    minecraft_version: str | None = None,
) -> str:
    """Return the selected NeoForge version string."""

    return select_neoforge_candidate(candidates, minecraft_version=minecraft_version).version


# Readable aliases for callers that prefer imperative naming.
choose_neoforge_candidate = select_neoforge_candidate
choose_neoforge_version = select_neoforge_version
