"""Immutable value objects used by ModPort.

The module deliberately contains no filesystem, network, or workflow code.  A
manifest is a value that can be serialized and hashed; resolving one is left
to the workflow layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping


def _freeze(value: Any) -> Any:
    """Recursively turn common containers into immutable equivalents."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Budget:
    """Optional resource limits for one migration.

    ``None`` means that a limit is not imposed by the request.  Limits are
    deliberately represented as non-negative integers so that a request can be
    validated before any workflow is started.
    """

    max_seconds: int | None = 43_200
    max_agent_assignments: int | None = 40
    max_rework_rounds: int = 10
    execution_max_attempts: int = 3
    max_tokens: int | None = None

    def validate(self) -> None:
        for name in (
            "max_seconds",
            "max_agent_assignments",
            "max_tokens",
        ):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"{name} must be a non-negative integer or None")
            if value is not None and value < 0:
                raise ValueError(f"{name} must be a non-negative integer or None")
        if isinstance(self.max_rework_rounds, bool) or not isinstance(self.max_rework_rounds, int):
            raise ValueError("max_rework_rounds must be a non-negative finite integer")
        if self.max_rework_rounds < 0:
            raise ValueError("max_rework_rounds must be a non-negative finite integer")
        if isinstance(self.execution_max_attempts, bool) or not isinstance(
            self.execution_max_attempts, int
        ):
            raise ValueError("execution_max_attempts must be a positive finite integer")
        if self.execution_max_attempts < 1:
            raise ValueError("execution_max_attempts must be a positive finite integer")

    def to_dict(self) -> dict[str, int | None]:
        self.validate()
        return {
            "max_seconds": self.max_seconds,
            "max_agent_assignments": self.max_agent_assignments,
            "max_rework_rounds": self.max_rework_rounds,
            "execution_max_attempts": self.execution_max_attempts,
            "max_tokens": self.max_tokens,
        }


_BUDGET_FIELDS = frozenset(
    {"max_seconds", "max_agent_assignments", "max_rework_rounds", "execution_max_attempts", "max_tokens"}
)


def _budget_from_mapping(value: Mapping[str, Any]) -> Budget:
    """Decode only the current SDK budget shape; legacy fields are rejected."""

    unknown = set(value) - _BUDGET_FIELDS
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"unsupported budget field(s): {names}")
    budget = Budget(**dict(value))
    budget.validate()
    return budget


@dataclass(frozen=True, slots=True)
class MigrationRequest:
    """A validated description of a mod migration.

    ``source_*`` and ``target_*`` are intentionally explicit: a migration can
    change both the Minecraft version and the loader version.  For a normal
    Forge-to-NeoForge migration, ``source_loader`` is ``"forge"`` and
    ``target_loader`` is ``"neoforge"``.
    """

    mod_id: str
    source_repository: str
    source_minecraft: str
    target_minecraft: str
    source_loader: str = "forge"
    target_loader: str = "neoforge"
    budget: Budget = field(default_factory=Budget)
    source_revision: str | None = None
    output_root: str | None = None
    source_loader_version: str | None = None
    target_loader_version: str | None = None
    source_java: str | None = None
    target_java: str | None = None
    mdk_revision: str | None = None
    skill_store: str | None = None
    platform_skill_revision: str | None = None
    java_skill_revision: str | None = None
    max_parallel_coders: int = 3
    workflow_mode: str = "migration"
    skill_kind: str | None = None
    admin_wait_seconds: int | None = None
    dependency_cache: str | None = None
    validation_scope: str = "full"
    requirements: str | None = None
    wiki_enabled: bool = True
    wiki_revision: str | None = None
    source_snapshot: bool = False
    local_workspace: Mapping[str, Any] | None = None

    def __post_init__(self):
        if self.local_workspace is not None:
            object.__setattr__(self, 'local_workspace', _freeze(self.local_workspace))

    # Familiar aliases keep the value object ergonomic without duplicating
    # serialized fields.  The canonical representation always uses the names
    # above.
    @property
    def from_minecraft(self) -> str:
        return self.source_minecraft

    @property
    def to_minecraft(self) -> str:
        return self.target_minecraft

    @property
    def from_loader(self) -> str:
        return self.source_loader

    @property
    def to_loader(self) -> str:
        return self.target_loader

    def validate(self) -> None:
        if not isinstance(self.mod_id, str) or not self.mod_id.strip():
            raise ValueError("mod_id must be a non-empty string")
        if not isinstance(self.source_repository, str) or not self.source_repository.strip():
            raise ValueError("source_repository must be a non-empty string")
        if type(self.source_snapshot) is not bool:
            raise ValueError("source_snapshot must be a boolean")
        if self.local_workspace is not None:
            from .workspace import validate_workspace_spec
            validate_workspace_spec(self.local_workspace)
            from urllib.parse import urlsplit
            if (self.workflow_mode != 'migration' or urlsplit(self.source_repository).scheme != 'file'
                    or (not self.source_snapshot and self.local_workspace['mode'] != 'git_worktree')):
                raise ValueError('local_workspace requires frozen local migration source')
        if self.source_snapshot:
            from urllib.parse import urlsplit
            if urlsplit(self.source_repository).scheme != "file":
                raise ValueError("source_snapshot requires a local file repository URI")
        for name in ("source_minecraft", "target_minecraft", "source_loader", "target_loader"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            if any(char.isspace() for char in value):
                raise ValueError(f"{name} must not contain whitespace")
        if self.source_revision is not None:
            if not isinstance(self.source_revision, str) or not self.source_revision.strip():
                raise ValueError("source_revision must be a non-empty string or None")
        if self.requirements is not None and (
            not isinstance(self.requirements, str) or not self.requirements.strip()
        ):
            raise ValueError("requirements must be a non-empty string or None")
        if type(self.wiki_enabled) is not bool:
            raise ValueError("wiki_enabled must be a boolean")
        if self.wiki_revision is not None:
            import re
            if (not isinstance(self.wiki_revision, str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,127}', self.wiki_revision)
                    or any(part in {'', '.', '..'} for part in self.wiki_revision.split('/'))):
                raise ValueError("wiki_revision must be a branch, tag or repository revision")
        if self.output_root is not None and (
            not isinstance(self.output_root, str) or not self.output_root.strip()
        ):
            raise ValueError("output_root must be a non-empty string or None")
        if self.dependency_cache is not None:
            from pathlib import Path
            if (not isinstance(self.dependency_cache, str)
                    or not Path(self.dependency_cache).is_absolute()
                    or ".." in Path(self.dependency_cache).parts):
                raise ValueError("dependency_cache must be an absolute path without parent traversal")
        for name in ("source_loader_version", "target_loader_version", "source_java", "target_java",
                     "mdk_revision", "skill_store", "platform_skill_revision", "java_skill_revision"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        for name in ("platform_skill_revision", "java_skill_revision", "mdk_revision"):
            value = getattr(self, name)
            if value is not None:
                import re
                pattern = r"[0-9a-f]{40}" if name == "mdk_revision" else r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
                if value.lower() in {"latest", "recommended", "current"} or not re.fullmatch(pattern, value):
                    raise ValueError(f"{name} must be an explicit revision identifier")
        if type(self.max_parallel_coders) is not int or not 1 <= self.max_parallel_coders <= 32:
            raise ValueError("max_parallel_coders must be between 1 and 32")
        if self.admin_wait_seconds is not None and (type(self.admin_wait_seconds) is not int or self.admin_wait_seconds < 0):
            raise ValueError("admin_wait_seconds must be a nonnegative integer or None")
        if self.workflow_mode not in {"migration", "skill_generation", "artifact_verification"}:
            raise ValueError("unsupported workflow_mode")
        if self.workflow_mode == "skill_generation" and self.skill_kind not in {"platform", "java"}:
            raise ValueError("standalone generation requires skill_kind platform or java")
        if (not isinstance(self.validation_scope, str)
                or self.validation_scope not in {"full", "compile_package"}):
            raise ValueError("validation_scope must be 'full' or 'compile_package'")
        if self.workflow_mode == "skill_generation" and self.validation_scope != "full":
            raise ValueError("validation_scope applies only to migration workflows")
        if self.workflow_mode == "artifact_verification" and self.validation_scope != "full":
            raise ValueError("artifact_verification requires validation_scope='full'")
        if not isinstance(self.budget, Budget):
            raise ValueError("budget must be a Budget")
        self.budget.validate()

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result: dict[str, Any] = {
            "mod_id": self.mod_id,
            "source_repository": self.source_repository,
            "source_minecraft": self.source_minecraft,
            "target_minecraft": self.target_minecraft,
            "source_loader": self.source_loader,
            "target_loader": self.target_loader,
            "budget": self.budget.to_dict(),
        }
        if self.source_revision is not None:
            result["source_revision"] = self.source_revision
        if self.output_root is not None:
            result["output_root"] = self.output_root
        if self.source_snapshot:
            result["source_snapshot"] = True
        if self.local_workspace is not None:
            result['local_workspace'] = dict(self.local_workspace)
        for name in ("source_loader_version", "target_loader_version", "source_java", "target_java",
                     "mdk_revision", "skill_store", "platform_skill_revision", "java_skill_revision",
                     "max_parallel_coders", "workflow_mode", "skill_kind", "admin_wait_seconds", "dependency_cache",
                     "validation_scope", "requirements", "wiki_enabled", "wiki_revision"):
            value = getattr(self, name)
            if value is not None and not (name == "validation_scope" and value == "full"):
                result[name] = value
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MigrationRequest":
        if not isinstance(value, Mapping):
            raise TypeError("migration request must be a mapping")
        budget = value.get("budget", {})
        if not isinstance(budget, Mapping):
            raise ValueError("migration request budget must be a mapping")
        return cls(
            mod_id=str(value.get("mod_id", "")),
            source_repository=str(value.get("source_repository", "")),
            source_minecraft=str(value.get("source_minecraft", "")),
            target_minecraft=str(value.get("target_minecraft", "")),
            source_loader=str(value.get("source_loader", "forge")),
            target_loader=str(value.get("target_loader", "neoforge")),
            budget=_budget_from_mapping(budget),
            source_revision=value.get("source_revision"),
            output_root=value.get("output_root"),
            **{name: value[name] for name in ("source_loader_version", "target_loader_version",
                "source_java", "target_java", "mdk_revision", "skill_store", "platform_skill_revision",
                "java_skill_revision", "max_parallel_coders", "workflow_mode", "skill_kind", "admin_wait_seconds",
                "dependency_cache", "validation_scope", "requirements", "source_snapshot", "local_workspace",
                "wiki_enabled", "wiki_revision") if name in value},
        )

    from_dict = from_mapping


@dataclass(frozen=True, slots=True)
class NeoForgeVersionCandidate:
    """A NeoForge version offered by a metadata source.

    ``channel`` is normally ``"stable"`` or ``"beta"``.  Other channels are
    accepted as lower-priority metadata, but selection only considers stable
    candidates and, if none exist, beta candidates.
    """

    version: str
    channel: str = "stable"
    minecraft_version: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", _freeze(self.metadata))

    def validate(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("candidate version must be a non-empty string")
        if not isinstance(self.channel, str) or not self.channel.strip():
            raise ValueError("candidate channel must be a non-empty string")
        if self.minecraft_version is not None and (
            not isinstance(self.minecraft_version, str) or not self.minecraft_version.strip()
        ):
            raise ValueError("candidate minecraft_version must be a non-empty string or None")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result: dict[str, Any] = {"version": self.version, "channel": self.channel}
        if self.minecraft_version is not None:
            result["minecraft_version"] = self.minecraft_version
        if self.metadata:
            result["metadata"] = self.metadata
        return result


@dataclass(frozen=True, slots=True)
class SkillReference:
    """Immutable identity and evidence index of one reusable skill.

    Digest fields are retained for diagnostics and compatibility with older
    Runs.  They describe the bytes observed when the reference was produced;
    a stale digest must not make an otherwise applicable isolated-run skill
    unusable.
    """

    kind: str
    skill_id: str
    source: Mapping[str, str]
    target: Mapping[str, str]
    bundle_sha256: str | None
    path: str
    files: Mapping[str, str]
    review: Mapping[str, Any]
    coverage: Mapping[str, Any] = field(default_factory=dict)
    requires_java: Mapping[str, str] | None = None
    schema_version: int = 1

    def __post_init__(self):
        for name in ("source", "target", "files", "review", "coverage"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))
        if self.requires_java is not None:
            object.__setattr__(self, "requires_java", _freeze(self.requires_java))

    def validate(self):
        import re
        from pathlib import PurePosixPath
        if type(self.schema_version) is not int or self.schema_version != 1 or self.kind not in {"java", "platform"}:
            raise ValueError("unsupported skill reference schema or kind")
        if not isinstance(self.skill_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", self.skill_id):
            raise ValueError("invalid skill identity")
        keys = {"java"} if self.kind == "java" else {"minecraft", "loader", "loader_version"}
        for identity in (self.source, self.target):
            if set(identity) != keys or any(not isinstance(value, str) or not value.strip() for value in identity.values()):
                raise ValueError("skill reference requires exact source and target identities")
        if self.bundle_sha256 is not None and not isinstance(self.bundle_sha256, str):
            raise ValueError("skill bundle digest metadata must be a string or None")
        for relative in (self.path, *self.files):
            if (not isinstance(relative, str) or not relative or PurePosixPath(relative).is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.split("/")) or "\\" in relative):
                raise ValueError("skill reference paths must be contained relative paths")
        if any(value is not None and not isinstance(value, str) for value in self.files.values()):
            raise ValueError("skill file digest metadata must be strings or None")
        if (self.review.get("verdict") != "approved" or not self.review.get("reviewer_id")):
            raise ValueError("skill reference requires independent approval")

    def to_dict(self):
        from .contracts import json_copy
        # _freeze uses mappingproxy/tuple; convert collections without effects.
        def plain(value):
            if isinstance(value, Mapping):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [plain(item) for item in value]
            return value
        self.validate()
        return json_copy({name: plain(getattr(self, name)) for name in self.__dataclass_fields__})

    @classmethod
    def from_mapping(cls, value):
        reference = cls(**dict(value))
        reference.validate()
        return reference


@dataclass(frozen=True, slots=True)
class LockedManifest:
    """An immutable, deterministic resolution of a :class:`MigrationRequest`.

    All collection fields are copied and recursively frozen at construction.
    ``manifest_sha256`` is optional provenance supplied by a persistence layer;
    :meth:`sha256` computes the digest from the manifest content itself and
    excludes this field to avoid a self-referential hash.
    """

    request: MigrationRequest
    neoforge_version: str
    source_commit: str | None = None
    neoforge_channel: str = "stable"
    minecraft_version: str | None = None
    java_version: str = "25"
    java_toolchain: Mapping[str, str] = field(default_factory=dict)
    gradle_version: str | None = None
    mdk_repository: str | None = None
    mdk_commit: str | None = None
    sdk_version: str = "0.7.1"
    workflow_version: int = 12
    checksums: Mapping[str, str] = field(default_factory=dict)
    mappings_version: str | None = None
    dependencies: tuple[Mapping[str, Any], ...] = ()
    files: tuple[str, ...] = ()
    schema_version: int = 2
    manifest_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "checksums", _freeze(self.checksums))
        object.__setattr__(self, "java_toolchain", _freeze(self.java_toolchain))
        object.__setattr__(self, "dependencies", tuple(_freeze(item) for item in self.dependencies))
        object.__setattr__(self, "files", tuple(self.files))

    def validate(self) -> None:
        if not isinstance(self.request, MigrationRequest):
            raise ValueError("request must be a MigrationRequest")
        self.request.validate()
        if not isinstance(self.neoforge_version, str) or not self.neoforge_version.strip():
            raise ValueError("neoforge_version must be a non-empty string")
        if not isinstance(self.neoforge_channel, str) or not self.neoforge_channel.strip():
            raise ValueError("neoforge_channel must be a non-empty string")
        for name in ("source_commit", "gradle_version", "mdk_repository", "mdk_commit"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        if not isinstance(self.java_version, str) or not self.java_version.strip():
            raise ValueError("java_version must be a non-empty string")
        if not isinstance(self.java_toolchain, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.java_toolchain.items()
        ):
            raise ValueError("java_toolchain must be a string mapping")
        if not isinstance(self.sdk_version, str) or not self.sdk_version.strip():
            raise ValueError("sdk_version must be a non-empty string")
        if not isinstance(self.workflow_version, int) or self.workflow_version < 1:
            raise ValueError("workflow_version must be a positive integer")
        if not isinstance(self.checksums, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.checksums.items()
        ):
            raise ValueError("checksums must be a string mapping")
        if self.minecraft_version is not None and (
            not isinstance(self.minecraft_version, str) or not self.minecraft_version.strip()
        ):
            raise ValueError("minecraft_version must be a non-empty string or None")
        if isinstance(self.schema_version, bool) or not isinstance(self.schema_version, int):
            raise ValueError("schema_version must be an integer")
        if self.schema_version != 2:
            raise ValueError("unsupported locked manifest schema_version; expected 2")
        if not isinstance(self.dependencies, tuple):
            raise ValueError("dependencies must be immutable tuple data")
        for dependency in self.dependencies:
            if not isinstance(dependency, Mapping):
                raise ValueError("each dependency must be a mapping")
        if not isinstance(self.files, tuple) or any(not isinstance(item, str) for item in self.files):
            raise ValueError("files must be an immutable tuple of strings")

    def to_dict(self, *, include_hash: bool = False) -> dict[str, Any]:
        self.validate()
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "request": self.request.to_dict(),
            "neoforge_version": self.neoforge_version,
            "neoforge_channel": self.neoforge_channel,
            "java_version": self.java_version,
            "java_toolchain": dict(self.java_toolchain),
            "sdk_version": self.sdk_version,
            "workflow_version": self.workflow_version,
            "checksums": dict(self.checksums),
            "dependencies": list(self.dependencies),
            "files": list(self.files),
        }
        for name in ("source_commit", "gradle_version", "mdk_repository", "mdk_commit"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.minecraft_version is not None:
            result["minecraft_version"] = self.minecraft_version
        if self.mappings_version is not None:
            result["mappings_version"] = self.mappings_version
        if include_hash and self.manifest_sha256 is not None:
            result["manifest_sha256"] = self.manifest_sha256
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LockedManifest":
        if not isinstance(value, Mapping):
            raise TypeError("locked manifest must be a mapping")
        schema_version = value.get("schema_version", 2)
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValueError("schema_version must be an integer")
        if schema_version != 2:
            raise ValueError(
                f"unsupported locked manifest schema_version {schema_version}; old manifests are not migrated"
            )
        if "kernel_version" in value:
            raise ValueError("kernel_version is deprecated; use sdk_version")
        if not isinstance(value.get("sdk_version"), str) or not value["sdk_version"].strip():
            raise ValueError("locked manifest must declare sdk_version; historical versions are not inferred")
        request_value = value.get("request")
        if not isinstance(request_value, Mapping):
            raise ValueError("locked manifest request must be a mapping")
        budget_value = request_value.get("budget", {})
        if not isinstance(budget_value, Mapping):
            raise ValueError("locked manifest request budget must be a mapping")
        request = MigrationRequest.from_mapping(request_value)
        return cls(
            request=request,
            neoforge_version=str(value.get("neoforge_version", "")),
            source_commit=value.get("source_commit"),
            neoforge_channel=str(value.get("neoforge_channel", "stable")),
            minecraft_version=value.get("minecraft_version"),
            java_version=str(value.get("java_version", "25")),
            java_toolchain=dict(value.get("java_toolchain", {})),
            gradle_version=value.get("gradle_version"),
            mdk_repository=value.get("mdk_repository"),
            mdk_commit=value.get("mdk_commit"),
            sdk_version=value["sdk_version"],
            workflow_version=int(value.get("workflow_version", 2)),
            checksums=dict(value.get("checksums", {})),
            mappings_version=value.get("mappings_version"),
            dependencies=tuple(value.get("dependencies", ())),
            files=tuple(value.get("files", ())),
            schema_version=schema_version,
            manifest_sha256=value.get("manifest_sha256"),
        )

    from_dict = from_mapping
