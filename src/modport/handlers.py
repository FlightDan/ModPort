"""Operations-owned handlers for the generic mod migration workflow."""

from __future__ import annotations

from .workspace import project_path, project_relative, workspace_spec, is_project_workspace
import base64
from dataclasses import dataclass, replace
from hashlib import sha256
from http.client import HTTPException
import json
from .platform_files import file_os as os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlsplit
from xml.etree import ElementTree

from .contracts import OperationInput, OperationResult
from .contract_inputs import validate_baseline_gradle_tasks
from .telemetry import ProcessAudit, probe_process, redact
from .evidence import current_lock_fds, file_digest
from .token_budget import TokenBudgetExceeded
from .opencode_runtime import OpenCodeCleanupError
from .prompt_compressor import (PromptCompressor, PromptCompressionError,
                                compressed_history_for_plan)
from .input_preparation import (prepare_inputs, preparation_step, preparation_checkpoint,
                                preparation_phase, model_work)
from .prompts import STAGE_PROMPTS, build_prompt

from .sdk_compat import sdk_release

from .manifest import canonical_json, select_neoforge_candidate, validate_manifest
from .models import Budget, LockedManifest, MigrationRequest, NeoForgeVersionCandidate
from .characterization import (AssertionContract, CharacterizationContract,
                               FrozenContract, ReviewRecord, SourceAnchor,
                               canonical_json as contract_canonical_json, freeze_contract)
from .workflow import DEFAULT_AGENT_MODEL, DEFAULT_REASONING_EFFORT, WORKFLOW_VERSION, agent_model_policy
from .author_contracts import characterization_evidence_schema, target_build_requirements
from .review_contracts import REJECTED_FINDINGS_PROMPT, validate_review_findings
from .business_policy import business_gates_disabled, compile_package_scope


MAVEN_METADATA = "https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml"
MAVEN_METADATA_MIRROR = "https://mirrors4.qlu.edu.cn/bmclapi/net/neoforged/neoforge/maven-metadata.xml"
MAX_NEOFORGE_METADATA_BYTES = 4 * 1024 * 1024
MIN_CLIENT_LAUNCH_SECONDS = 120.0
NEOFORGE_METADATA_CACHE_SCHEMA = "modport.neoforge-metadata-cache.v1"
NEOFORGE_UNPINNED_METADATA_CACHE_SCHEMA = "modport.neoforge-unpinned-metadata-cache.v1"
NEOFORGE_UNPINNED_METADATA_TTL_SECONDS = 24 * 60 * 60
MDK_REPOSITORY_TEMPLATE = "https://github.com/NeoForgeMDKs/MDK-{minecraft}-ModDevGradle.git"
FORGE_BASELINE_INIT = """// ModPort cold-cache compatibility overlay for legacy ForgeGradle.\nallprojects {\n    repositories {\n        maven { url = uri('https://maven.minecraftforge.net/') }\n        maven { url = uri('https://libraries.minecraft.net/') }\n        mavenCentral()\n    }\n}\n"""


def _result(command: OperationInput, status: str, *, outputs: Mapping[str, Any] | None = None, detail: str = "", error_code: str | None = None) -> OperationResult:
    return OperationResult(
        status=status,
        run_id=command.run_id,
        task_id=command.task_id,
        stage_id=command.stage_id,
        command_id=command.command_id,
        outputs=dict(outputs or {}),
        detail=detail,
        error_code=error_code,
    )


def _unverified_result(command: OperationInput, *, status: str = "completed",
                       outputs: Mapping[str, Any] | None = None,
                       diagnostics: Sequence[str] = (), detail: str = "observations recorded",
                       error_code: str | None = None) -> OperationResult:
    """Add v17 acceptance metadata without changing the observed result state."""
    merged = dict(outputs or {})
    existing = merged.get("business_diagnostics", ())
    merged["business_diagnostics"] = [
        *(existing if isinstance(existing, list) else ()),
        *(str(item) for item in diagnostics if str(item)),
    ]
    merged["acceptance_status"] = "unverified"
    return _result(command, status, outputs=merged, detail=detail, error_code=error_code)


def _wrapper_cache_diagnostic(command: OperationInput, value: Any) -> dict[str, Any]:
    """Keep the Wrapper cache optimization and its outputs within v26+."""
    if command.options.get("workflow_version", 0) < 26:
        return {}
    return {"wrapper_distribution_cache": value}


def _run_root(command: OperationInput) -> Path:
    if not command.run_dir:
        raise ValueError("migration stage requires run_dir")
    root = Path(command.run_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(exist_ok=True)
    (root / "artifacts").mkdir(exist_ok=True)
    return root


def _fresh_baseline_report(command: OperationInput, root: Path) -> Path:
    """Read the exact verifier report selected for this review or freeze."""
    if command.options.get("workflow_version", 0) < 26:
        return root / "artifacts" / "baseline-contract-tests.json"
    from .evidence import verified_path
    result = command.upstream_results.get("contract_verify", {})
    refs = result.get("outputs", {}).get("artifact_refs", {}) if isinstance(result, Mapping) else {}
    ref = refs.get("baseline_contract_tests_candidate") if isinstance(refs, Mapping) else None
    if not isinstance(ref, Mapping):
        raise ValueError("fresh contract verifier has no sealed baseline report")
    expected = (Path("artifacts") / "executions" / str(result.get("command_id"))
                / "baseline-contract-tests.json").as_posix()
    if ref.get("path") != expected:
        raise ValueError("fresh baseline report belongs to another verifier")
    path = verified_path(root, ref)
    if file_digest(path) != ref.get("sha256"):
        raise ValueError("fresh baseline report digest changed")
    return path


def _remaining_timeout(command: OperationInput, maximum: float) -> float:
    from .execution_budget import remaining_timeout
    return remaining_timeout(command, maximum)


def _request(command: OperationInput) -> dict[str, Any]:
    raw = command.payload.get("request", command.payload)
    if not isinstance(raw, Mapping):
        raise TypeError("command request must be an object")
    return dict(raw)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(value) + "\n", encoding="utf-8")


def _reuse_cached_compressed_prompt(
    compression_path: Path,
    source_prompt_path: Path,
    compressed_prompt_path: Path,
    full_prompt: str,
    model: str,
    root: Path,
) -> tuple[str, dict[str, Any]] | None:
    """Return a previously completed compression only when every digest agrees.

    A worker can be interrupted after prompt compression but before its result
    is acknowledged by the SDK.  Reusing that host-owned result avoids paying
    for the same summary calls again, while the digest checks make stale or
    partial files a cache miss.  Callers still re-authenticate all artifact
    refs after this function returns.
    """
    paths = (compression_path, source_prompt_path, compressed_prompt_path)
    if any(path.is_symlink() or not path.is_file() for path in paths):
        return None
    try:
        metadata = json.loads(compression_path.read_text(encoding="utf-8"))
        source_bytes = source_prompt_path.read_bytes()
        compressed_bytes = compressed_prompt_path.read_bytes()
    except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("compressed") is not True:
        return None
    if metadata.get("model") != model:
        return None
    expected_source = full_prompt.encode("utf-8")
    source_digest = sha256(source_bytes).hexdigest()
    compressed_digest = sha256(compressed_bytes).hexdigest()
    if source_bytes != expected_source:
        return None
    if metadata.get("original_sha256") != sha256(expected_source).hexdigest():
        return None
    if metadata.get("original_bytes") != len(expected_source):
        return None
    try:
        source_relative = project_relative(root, source_prompt_path).as_posix()
        compressed_relative = project_relative(root, compressed_prompt_path).as_posix()
    except ValueError:
        return None
    if metadata.get("source_prompt_path") != source_relative:
        return None
    if metadata.get("compressed_prompt_path") != compressed_relative:
        return None
    if metadata.get("final_sha256") != compressed_digest:
        return None
    if metadata.get("final_bytes") != len(compressed_bytes) or not compressed_bytes.strip():
        return None
    try:
        return compressed_bytes.decode("utf-8"), metadata
    except UnicodeDecodeError:
        return None


def _recovery_prompt_cache_paths(root: Path, command: OperationInput) -> tuple[Path, Path, Path, str] | None:
    """Validate the host-recorded source of an interrupted prompt replay."""
    raw = command.payload.get("prompt_reuse")
    if not isinstance(raw, Mapping) or command.stage_id != "contract_diagnose":
        return None
    execution_id = raw.get("execution_id")
    if (not isinstance(execution_id, str) or not execution_id.strip()
            or execution_id == command.command_id):
        return None
    base = root / "artifacts" / "executions" / execution_id
    source = base / "prompt-compression" / "source.txt"
    metadata = base / "prompt-compression.json"
    compressed = base / "prompt-compression" / "compressed.txt"
    expected = {
        "source": source,
        "metadata": metadata,
        "compressed": compressed,
    }
    for key, path in expected.items():
        try:
            relative = project_relative(root, path).as_posix()
        except ValueError:
            return None
        if raw.get(key) != relative or path.is_symlink() or not path.is_file():
            return None
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root.resolve()):
            return None
        expected_digest = raw.get(key + "_sha256")
        if not isinstance(expected_digest, str) or expected_digest != file_digest(path):
            return None
    return source, metadata, compressed, execution_id


def _read_artifact_ref(root: Path, command: OperationInput, artifact_id: str) -> tuple[Path, dict[str, Any]]:
    return _resolve_artifact_ref(root, command.artifact_refs.get(artifact_id), artifact_id)


def _resolve_artifact_ref(root: Path, raw_ref: Any, artifact_id: str) -> tuple[Path, dict[str, Any]]:
    """Resolve one reference without copying its potentially large operation."""
    preparation_checkpoint(count='reference_checks')
    if not isinstance(raw_ref, Mapping):
        raise ValueError(f"required artifact ref is missing: {artifact_id}")
    relative = Path(str(raw_ref.get("path", "")))
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError(f"artifact ref {artifact_id!r} path must be relative and contained")
    path = project_path(root, relative)
    if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise ValueError(f"artifact ref {artifact_id!r} does not resolve to a regular run artifact")
    return path, dict(raw_ref)


@preparation_step('reference_validation')
def _acceptance_rubric_for(command: OperationInput, root: Path) -> dict[str, Any]:
    seen = set()
    for artifact_id in command.artifact_refs:
        ref = command.artifact_refs[artifact_id]
        path = ref.get('path') if isinstance(ref, Mapping) else None
        if isinstance(path, str) and path in seen:
            preparation_checkpoint(count='duplicate_reference_checks_avoided')
            continue
        _read_artifact_ref(root, command, artifact_id)
        if isinstance(path, str):
            seen.add(path)
    path, ref = _read_artifact_ref(root, command, "acceptance_rubric")
    rubric = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rubric, dict):
        raise ValueError("acceptance rubric must be a JSON object")
    if (not isinstance(rubric.get("rubric_id"), str) or not rubric["rubric_id"].strip()
            or type(rubric.get("rubric_version")) is not int):
        raise ValueError("acceptance rubric identity or version is invalid")
    return rubric


def _test_evidence_declarations(
    contract: Mapping[str, Any], rubric: Mapping[str, Any], *,
    workflow_version: int = 0, gradle_tasks: Sequence[str] | None = None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(contract, Mapping):
        raise ValueError('functional contract must be a JSON object')
    if workflow_version < 34 and (
        contract.get("rubric_id") != rubric.get("rubric_id")
        or contract.get("rubric_version") != rubric.get("rubric_version")
    ):
        raise ValueError("functional contract is not bound to the active acceptance rubric version")
    behaviors = contract.get("behaviors", contract.get("entries", ()))
    if not isinstance(behaviors, list) or not behaviors:
        raise ValueError("functional contract requires behaviors")
    owners: dict[str, str] = {}
    for behavior in behaviors:
        if not isinstance(behavior, Mapping):
            raise ValueError("functional contract behavior must be an object")
        side = str(behavior.get("side", "")).lower()
        mappings = behavior.get("test_mapping", ())
        if not isinstance(mappings, list) or not mappings:
            raise ValueError("each behavior requires a non-empty test_mapping array")
        for raw_test_id in mappings:
            test_id = str(raw_test_id).strip()
            if not test_id or test_id in owners:
                raise ValueError("test_mapping ids must be non-empty and globally unique")
            owners[test_id] = side
    declarations = contract.get("test_evidence")
    evidence_files = contract.get("baseline_evidence_files")
    if not isinstance(declarations, Mapping) or set(declarations) != set(owners):
        raise ValueError("test_evidence must map every behavior test id exactly once")
    if (
        not isinstance(evidence_files, list)
        or not evidence_files
        or any(not isinstance(value, str) for value in evidence_files)
        or len(evidence_files) != len(set(evidence_files))
    ):
        raise ValueError("baseline_evidence_files must be a non-empty unique array")
    schema = rubric.get("test_evidence_schema", {})
    required = set(schema.get("required_fields", ()))
    runtime_executors = set(schema.get("runtime_executors", ()))
    normalized: dict[str, dict[str, Any]] = {}
    paths: list[str] = []
    result_identities: set[tuple[str, str, str]] = set()
    declared_task_paths = ({":" + task.lstrip(":") for task in gradle_tasks}
                           if gradle_tasks is not None else None)
    for test_id, raw in declarations.items():
        if not isinstance(test_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]+", test_id):
            raise ValueError("test_evidence ids must be safe non-empty identifiers")
        if not isinstance(raw, Mapping) or not required.issubset(raw):
            raise ValueError(f"test_evidence {test_id!r} does not satisfy the rubric schema")
        item = dict(raw)
        path = item.get("path")
        kind = item.get("evidence_kind")
        executor = item.get("executor")
        operations = item.get("runtime_operations")
        if not isinstance(path, str) or path not in evidence_files:
            raise ValueError(f"test_evidence {test_id!r} does not reference a declared evidence file")
        if (
            not path.startswith(".modport/evidence/")
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or "\\" in path
        ):
            raise ValueError(
                f"test_evidence {test_id!r}.path={path!r} must be a relative file path "
                f"under .modport/evidence/ (use .modport/evidence/{test_id}.json); "
                "update baseline_evidence_files, test_evidence and the harness evidence "
                "producer together. Build outputs belong in build/; evidence records do not."
            )
        if not isinstance(operations, list) or not operations or any(not isinstance(value, str) or not value.strip() for value in operations):
            raise ValueError(f"test_evidence {test_id!r} requires concrete runtime_operations")
        if kind == "runtime":
            if executor not in runtime_executors:
                raise ValueError(f"runtime evidence {test_id!r} has an unsupported executor")
            test_sources = item.get("test_source_files")
            if (
                not isinstance(test_sources, list)
                or not test_sources
                or len(test_sources) != len(set(test_sources))
                or any(not isinstance(value, str) or not value.strip() for value in test_sources)
            ):
                raise ValueError(
                    f"runtime evidence {test_id!r} requires unique test_source_files"
                )
            if workflow_version >= 29:
                identity = item.get('result_identity')
                native = workflow_version >= 34 and executor == 'gametest'
                if ((executor != 'junit' and not native) or not isinstance(identity, Mapping)
                        or identity.get('kind') != 'junit_xml'
                        or set(identity) != {'kind', 'gradle_task', 'classname', 'name'}):
                    raise ValueError(
                        f"runtime evidence {test_id!r} requires an exact JUnit XML result_identity"
                    )
                from .runtime_result_identity import validate_runtime_result_identity
                try:
                    identity_key = validate_runtime_result_identity(
                        identity, native=native, gradle_tasks=declared_task_paths)
                except ValueError as exc:
                    raise ValueError(f"runtime evidence {test_id!r}: {exc}") from exc
                if identity_key in result_identities:
                    raise ValueError("JUnit result_identity values must be unique across test IDs")
                result_identities.add(identity_key)
        elif kind == "static_client":
            if workflow_version >= 29:
                raise ValueError(
                    f"static client evidence {test_id!r} has no actual runtime test-result identity"
                )
            gates = item.get("acceptance_gates")
            if owners[test_id] != "client" or executor != schema.get("static_executor"):
                raise ValueError(f"static evidence {test_id!r} is permitted only for client-only behavior")
            if not isinstance(item.get("static_reason"), str) or not item["static_reason"].strip():
                raise ValueError(f"static client evidence {test_id!r} requires a reason")
            if not isinstance(gates, list) or "client_smoke" not in gates:
                raise ValueError(f"static client evidence {test_id!r} must bind client_smoke")
        else:
            raise ValueError(f"test_evidence {test_id!r} has an unsupported evidence_kind")
        paths.append(path)
        normalized[str(test_id)] = item
    if len(paths) != len(set(paths)) or set(paths) != set(evidence_files):
        raise ValueError("every test id must bind one distinct declared evidence file")
    return normalized


def _runtime_executor_provenance(
    worktree: Path,
    declarations: Mapping[str, Mapping[str, Any]],
    rubric: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Hash executable test sources before untrusted build logic is run."""

    schema = rubric.get("test_evidence_schema", {})
    from .runtime_result_identity import RUNTIME_TEST_SOURCE_SUFFIXES
    suffixes = set(schema.get("runtime_test_source_suffixes", ())) | RUNTIME_TEST_SOURCE_SUFFIXES
    provenance: dict[str, dict[str, Any]] = {}
    from .retry_policy import is_harness_source
    worktree_root = worktree.resolve()
    for test_id, declaration in declarations.items():
        if declaration.get("evidence_kind") != "runtime":
            continue
        files: dict[str, str] = {}
        for raw_relative in declaration.get("test_source_files", ()):
            relative = Path(str(raw_relative))
            source = worktree / relative
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or relative.parts[0] != ".modport"
                or not is_harness_source(relative.as_posix())
                or relative.suffix not in suffixes
                or source.is_symlink()
                or not source.is_file()
                or not source.resolve().is_relative_to(worktree_root)
                or source.resolve() != source.absolute()
            ):
                raise ValueError(
                    f"runtime evidence {test_id!r} has an unsafe or non-executable test source"
                )
            files[relative.as_posix()] = sha256(source.read_bytes()).hexdigest()
        fingerprint_payload = {"schema_version": 1, "files": dict(sorted(files.items()))}
        provenance[test_id] = {
            "executor_fingerprint": sha256(
                canonical_json(fingerprint_payload).encode("utf-8")
            ).hexdigest(),
            "test_source_files": dict(sorted(files.items())),
        }
    return provenance


def _validate_evidence_record(
    *,
    test_id: str,
    declaration: Mapping[str, Any],
    record: Mapping[str, Any],
    source_commit: str,
    execution_nonce: str,
    executor_fingerprint: str | None,
    workflow_version: int = 0,
) -> str | None:
    schema = characterization_evidence_schema(workflow_version=workflow_version)
    required = set(schema['static_client_required_record_fields'] if declaration.get('evidence_kind') == 'static_client'
                   else schema['runtime_required_record_fields'])
    if not (required - {"runtime_witnesses"}).issubset(record):
        missing = sorted((required - {"runtime_witnesses"}) - set(record))
        raise ValueError(f"evidence record {test_id!r} is missing runtime provenance fields: {missing}")
    if record.get("test_id") != test_id or record.get("status") != schema['record_shapes']['status']['const']:
        raise ValueError(f"evidence record {test_id!r} is not a passing record for its mapping")
    for key in ("evidence_kind", "executor", "runtime_operations"):
        if canonical_json(record.get(key)) != canonical_json(declaration.get(key)):
            raise ValueError(f"evidence record {test_id!r} disagrees with its {key} declaration")
    if workflow_version < 34 and record.get("source_fingerprint") != source_commit:
        raise ValueError(f"evidence record {test_id!r} is not source-bound")
    if record.get("execution_nonce") != execution_nonce:
        raise ValueError(f"evidence record {test_id!r} is stale or not verifier-bound")
    for key in ("execution_inputs", "runtime_operations", "observations"):
        value = record.get(key)
        shape = schema['record_shapes'][key]
        choices = shape.get('oneOf', [shape])
        containers = tuple({'array': list, 'object': dict}[choice['type']] for choice in choices)
        if not isinstance(value, containers) or not value:
            raise ValueError(f"evidence record {test_id!r} requires non-empty {key}")
    if declaration.get("evidence_kind") == "static_client":
        if record.get("static_reason") != declaration.get("static_reason"):
            raise ValueError(f"static client evidence {test_id!r} reason mismatch")
        gates = record.get("acceptance_gates")
        if not isinstance(gates, list) or "client_smoke" not in gates:
            raise ValueError(f"static client evidence {test_id!r} lacks client_smoke binding")
        return None
    operations = declaration.get("runtime_operations")
    witnesses = record.get("runtime_witnesses")
    if not isinstance(operations, list) or not isinstance(witnesses, list) or len(witnesses) != len(operations):
        raise ValueError(f"runtime evidence record {test_id!r} lacks operation-level witnesses")
    event_indexes: list[int] = []
    for operation, witness in zip(operations, witnesses, strict=True):
        if not isinstance(witness, Mapping):
            raise ValueError(f"runtime evidence record {test_id!r} has an invalid witness")
        if witness.get("operation") != operation:
            raise ValueError(f"runtime evidence record {test_id!r} witness operation mismatch")
        event_index = witness.get("event_index")
        invocation = witness.get("invocation")
        observations = witness.get("observations")
        if (
            not isinstance(event_index, int)
            or isinstance(event_index, bool)
            or event_index < 0
            or not isinstance(invocation, str)
            or not invocation.strip()
            or not isinstance(observations, (list, dict))
            or not observations
            or witness.get("execution_nonce") != execution_nonce
        ):
            raise ValueError(f"runtime evidence record {test_id!r} has an incomplete witness")
        event_indexes.append(event_index)
    if event_indexes != list(range(len(operations))):
        raise ValueError(f"runtime evidence record {test_id!r} witness sequence is invalid")
    return f"MODPORT_RUNTIME_WITNESS {execution_nonce} {test_id}"


def _verify_locked_artifacts(
    root: Path,
    expected: Mapping[str, Any] | None = None,
    *,
    rubric: Mapping[str, Any] | None = None,
    contract_ref: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Validate the inputs needed by a post-freeze gate.

    A lock records useful provenance, but it is not a lease on the mutable
    checkout.  Keep schema, source/version, toolchain and evidence checks;
    tolerate missing or outdated content identities so a later stage can use
    the current files in an isolated run.
    """

    manifest_path = root / "artifacts" / "locked-manifest.json"
    contract_path = root / "artifacts" / "functional-contract.lock.json"
    if contract_ref is not None:
        contract_path, _ = _resolve_artifact_ref(
            root, contract_ref, "functional_contract_lock")
    source_path = root / "artifacts" / "source.json"
    for path in (manifest_path, contract_path, source_path):
        if not path.is_file():
            raise ValueError(f"required locked artifact is missing: {path.name}")

    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = LockedManifest.from_mapping(manifest_data)
    # ``manifest_sha256`` is optional diagnostic metadata.  Calling
    # ``validate_manifest`` would turn it into a hard content lock, so validate
    # the manifest's structural fields directly.
    manifest.validate()
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if manifest.source_commit != source.get("source_commit"):
        raise ValueError("locked manifest source commit does not match source evidence")
    java_relative = manifest.java_toolchain.get("executable")
    if not java_relative:
        raise ValueError("locked manifest lacks a Java toolchain")
    java_cache = (root / "toolchains" / "gradle-cache").resolve()
    java_executable = (java_cache / java_relative).resolve()
    if not java_executable.is_relative_to(java_cache) or not java_executable.is_file() or java_executable.is_symlink():
        raise ValueError("locked Java executable is unavailable")

    if rubric is None:
        rubric_path = root / "artifacts" / "acceptance-rubric.json"
        if not rubric_path.is_file():
            raise ValueError("immutable acceptance rubric is missing")
        rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
        if not isinstance(rubric, Mapping):
            raise ValueError("acceptance rubric must be an object")
        if (not isinstance(rubric.get("rubric_id"), str)
                or type(rubric.get("rubric_version")) is not int):
            raise ValueError("acceptance rubric identity or version is invalid")

    lock_data = json.loads(contract_path.read_text(encoding="utf-8"))
    if contract_ref is not None:
        # Current workflows publish a host-owned observation, whose review uses
        # verdict/findings and whose baseline result is an OperationResult. It is
        # not the retired approved-lock schema consumed by ReviewRecord below.
        # Required execution outcomes are assessed by the verification workflow.
        if lock_data.get("verification_basis") == "source_reading":
            from .behavior_requirements import read_requirements
            from .target_contract import validate_target_contract
            requirements = read_requirements(root, lock_data["behavior_requirements_ref"])
            validate_target_contract(requirements, lock_data.get("contract", {}))
            return {"manifest_sha256": str(manifest.manifest_sha256 or ""),
                    "contract_lock_sha256": str(contract_ref.get("sha256", ""))}
        contract = CharacterizationContract.from_mapping(lock_data.get("contract", {}))
        if not contract.entries:
            raise ValueError("frozen functional contract has no behavior entries")
        return {
            "manifest_sha256": str(manifest.manifest_sha256 or ""),
            "contract_lock_sha256": str(contract_ref.get("sha256", "")),
        }
    lock_payload = {key: value for key, value in lock_data.items() if key != "lock_sha256"}
    calculated_lock_hash = sha256(canonical_json(lock_payload).encode("utf-8")).hexdigest()
    contract = CharacterizationContract.from_mapping(lock_data.get("contract", {}))
    if not contract.entries:
        raise ValueError("frozen functional contract has no behavior entries")
    review_data = lock_data.get("review", {})
    review = ReviewRecord(
        reviewer_id=str(review_data.get("reviewer_id", "")),
        generator_id=review_data.get("generator_id"),
        contract_sha256=str(review_data.get("contract_sha256", "")),
        status=str(review_data.get("status", "")),
        review_id=str(review_data.get("review_id", "")),
        notes=str(review_data.get("notes", "")),
        evidence_refs=tuple(str(item) for item in review_data.get("evidence_refs", ())),
        schema_version=int(review_data.get("schema_version", 1)),
    )
    frozen = FrozenContract(
        contract=contract,
        review=review,
        frozen_sha256=str(lock_data.get("frozen_sha256", "")),
        schema_version=int(lock_data.get("schema_version", 1)),
    )
    frozen.verify()
    if contract.source_fingerprint != manifest.source_commit:
        raise ValueError("functional contract is not bound to the manifest source commit")
    baseline = lock_data.get("baseline_verification")
    if not isinstance(baseline, Mapping) or baseline.get("exit_code") != 0:
        raise ValueError("frozen contract lacks successful baseline verification")
    lock_rubric = lock_data.get("acceptance_rubric")
    if not isinstance(lock_rubric, Mapping) or any(
        lock_rubric.get(key) != rubric.get(key)
        for key in ("rubric_id", "rubric_version")
    ):
        raise ValueError("frozen contract is not bound to the active acceptance rubric")
    evidence_contract = {
        **dict(lock_data.get("contract", {})),
        "rubric_id": rubric.get("rubric_id"),
        "rubric_version": rubric.get("rubric_version"),
        "rubric_sha256": rubric.get("rubric_sha256"),
        "test_evidence": lock_data.get("test_evidence"),
        "baseline_evidence_files": lock_data.get("baseline_evidence_files"),
    }
    declarations = _test_evidence_declarations(evidence_contract, rubric)
    mapped_tests = {mapping for entry in contract.entries for mapping in entry.test_mapping}
    evidence_paths = {test_id: item["path"] for test_id, item in declarations.items()}
    if set(evidence_paths) != mapped_tests or any(
        value not in baseline.get("evidence_files", {}) for value in evidence_paths.values()
    ):
        raise ValueError("frozen test mappings are not bound to baseline evidence")
    evidence_root = root / "baseline" / ".modport" / "evidence"
    if evidence_root.is_symlink() or evidence_root.resolve() != evidence_root.absolute():
        raise ValueError("baseline evidence directory is unsafe")
    for relative, digest in baseline.get("evidence_files", {}).items():
        path = (root / "baseline" / relative).resolve()
        if not path.is_relative_to(evidence_root.resolve()) or not path.is_file() or path.is_symlink():
            raise ValueError(f"frozen baseline evidence checksum mismatch: {relative}")
    anchors = {
        "manifest_sha256": str(manifest.manifest_sha256 or ""),
        "contract_lock_sha256": calculated_lock_hash,
    }
    for key, digest in manifest.checksums.items():
        if key.startswith("mdk:"):
            actual = root / "toolchains" / "mdk" / key.removeprefix("mdk:")
            if not actual.is_file() or actual.is_symlink():
                raise ValueError(f"locked MDK file checksum mismatch: {key}")
        elif key == "operations:forge_baseline_init":
            continue
        elif key == "neoforge_maven_metadata_sha256":
            actual = root / "toolchains" / "neoforge-maven-metadata.xml"
            if not actual.is_file() or actual.is_symlink():
                raise ValueError("NeoForge metadata checksum mismatch")
    return anchors


def _exec(args: Sequence[str], *, cwd: Path, log: Path, timeout: float | None = None, env: Mapping[str, str] | None = None, combine_output: bool = True, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    if not args or any(not isinstance(item, str) or "\x00" in item for item in args):
        raise ValueError("invalid subprocess argument")
    from .telemetry import run_process
    from .workspace import workspace_context
    # Standalone host entrypoints may execute outside operation_context.
    run_root = log.parent.parent if log.parent.name == 'logs' else None
    if run_root is not None:
        with workspace_context(run_root):
            return run_process(args, cwd=cwd, log=log, timeout=timeout, env=env,
                               pass_fds=current_lock_fds(), combine_output=combine_output, input_text=input_text)
    return run_process(args, cwd=cwd, log=log, timeout=timeout, env=env,
                       pass_fds=current_lock_fds(), combine_output=combine_output, input_text=input_text)


def _bounded_entry_probe_log(command: OperationInput, log: Path) -> dict[str, Any]:
    """Archive a bounded excerpt of the redacted process log after a timeout."""

    root = Path(command.run_dir)
    if log.parent.is_symlink() or log.parent.resolve() != (root / "logs").absolute():
        raise ValueError("entry probe log directory is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(log, flags)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("entry probe log is not a regular file")
        limit = 1024 * 1024
        if info.st_size <= limit:
            excerpt = stream.read(limit + 1)
            truncated = len(excerpt) > limit
            excerpt = excerpt[:limit]
        else:
            half = limit // 2
            beginning = stream.read(half).decode("utf-8", errors="ignore").encode("utf-8")
            stream.seek(-half, os.SEEK_END)
            ending = stream.read(half).decode("utf-8", errors="ignore").encode("utf-8")
            excerpt = beginning + b"\n[MODPORT LOG MIDDLE OMITTED]\n" + ending
            truncated = True
    from .development import _artifact
    return _artifact(command, "harness-entry-probe-timeout.log", excerpt,
                     metadata={"raw_log": project_relative(root, log).as_posix(),
                               "raw_log_bytes": info.st_size,
                               "truncated": truncated,
                               "capture": "prefix_and_suffix" if truncated else "complete"})


def _agent_model_policy(command: OperationInput) -> tuple[str, str]:
    """Resolve a Run agent model while retaining frozen pre-v15 commands."""
    version = command.options.get("workflow_version")
    if type(version) is int and version >= 15:
        return agent_model_policy(version, command.stage_id, command.options.get("model_policy"))
    legacy_model, legacy_effort = agent_model_policy(version)
    return (
        str(command.options.get("model") or legacy_model),
        str(command.options.get("reasoning_effort") or legacy_effort),
    )


def preflight_opencode_host(root: Path, *, model: str = DEFAULT_AGENT_MODEL,
                           reasoning_effort: str = DEFAULT_REASONING_EFFORT,
                           timeout: float = 60.0) -> dict[str, Any]:
    """Verify the real OpenCode/model path before charging an assignment."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.resolve() != root.absolute():
        raise RuntimeError("OpenCode host preflight requires a regular Run directory")
    root = root.resolve()
    header_path = root / 'run.json'
    if header_path.is_file():
        header = json.loads(header_path.read_text(encoding='utf-8'))
        deadline = header.get('deadline_epoch')
        if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
            timeout = min(timeout, deadline - time.time())
            if timeout <= 0:
                raise TimeoutError('original Run deadline is exhausted before host preflight')
    log_path = root / "artifacts" / "host-preflight" / "opencode.log"
    prompt = (
        "Do not use tools or read files. Return exactly one JSON object and no other text: "
        '{"modport_preflight":"ok"}'
    )
    try:
        from .opencode_agent import run_agent
        completed = run_agent(prompt=prompt, cwd=root, log=log_path,
                              model=model, variant=reasoning_effort, timeout=timeout,
                              read_only=True, no_tools=True, token_budget_root=root)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"OpenCode host preflight timed out; inspect process audit log {log_path}"
        ) from exc
    except OpenCodeCleanupError as exc:
        from .evidence import atomic_json
        diagnostic = {key: exc.cleanup_diagnostic.get(key) for key in (
            'classification', 'returncode', 'target_pid', 'target_birth',
            'error_type', 'leader_exited', 'process_group_gone',
            'group_exit_wait_seconds', 'process_group_observation',
            'collection_errors', 'cleanup_reason', 'host_requested_signal')
            if key in exc.cleanup_diagnostic}
        diagnostic['cleanup_confirmed'] = False
        path = root / 'artifacts' / 'host-preflight' / 'opencode-cleanup.json'
        atomic_json(path, diagnostic)
        raise RuntimeError(
            f"OpenCode host preflight cleanup is unconfirmed; inspect {path}"
        ) from None
    except (OSError, TimeoutError, RuntimeError) as exc:
        raise RuntimeError(
            f"OpenCode host preflight failed: {exc}; inspect process audit log {log_path}"
        ) from exc
    if completed.returncode:
        raise RuntimeError(
            f"OpenCode host preflight process failed (exit_code={completed.returncode}); "
            f"inspect process audit log {log_path}"
        )
    from .telemetry import public_last_message
    message = public_last_message(completed.stdout)
    try:
        value = json.loads(message)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"OpenCode host preflight returned non-JSON output; inspect process audit log {log_path}"
        ) from exc
    if not isinstance(value, Mapping) or value.get("modport_preflight") != "ok":
        raise RuntimeError(
            f"OpenCode host preflight returned an unexpected response; inspect process audit log {log_path}"
        )
    result = {
        "schema_version": 1,
        "status": "passed",
        "model": model,
        "tools": "disabled",
        "session": "isolated",
        "exit_code": completed.returncode,
        "log": project_relative(root, log_path).as_posix(),
        "log_sha256": sha256(log_path.read_bytes()).hexdigest(),
        "checked_at": time.time(),
    }
    _write_json(root / "artifacts" / "host-preflight" / "result.json", result)
    return result


# Keep the Python entry point for frozen callers; it no longer starts Codex.
preflight_codex_host = preflight_opencode_host


def _sandboxed_build_command(
    root: Path,
    worktree: Path,
    args: Sequence[str],
    *,
    cache_name: str = "gradle-cache",
    java_home: Path | None = None,
    environment: Mapping[str, str] | None = None,
    readonly_workspace: bool = False,
    gradle_ro_cache: Path | None = None,
    operation: OperationInput | None = None,
    wrapper_cache_info: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
    writable_workspace_paths: Sequence[str] = (),
) -> list[str]:
    """Run untrusted build logic without host credentials or unrelated files."""

    from .local_workspace_sandbox import prepare_external_build
    external = prepare_external_build(root, worktree, args, cache_name=cache_name,
        java_home=java_home, environment=environment,
        readonly_workspace=readonly_workspace, gradle_ro_cache=gradle_ro_cache,
        operation=operation, wrapper_cache_info=wrapper_cache_info,
        timeout_seconds=timeout_seconds, writable_workspace_paths=writable_workspace_paths)
    if external is not None:
        return external
    if os.name == 'nt':
        from .windows_build import build_command
        return build_command(root, worktree, args, cache_name=cache_name,
            java_home=java_home, environment=environment,
            readonly_workspace=readonly_workspace, gradle_ro_cache=gradle_ro_cache,
            operation=operation, timeout_seconds=timeout_seconds,
            writable_workspace_paths=writable_workspace_paths)
    if shutil.which("bwrap") is None:
        raise RuntimeError("bubblewrap is required for untrusted Gradle execution")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", cache_name):
        raise ValueError("invalid isolated Gradle cache name")
    cache = root / "toolchains" / cache_name
    cache.mkdir(parents=True, exist_ok=True)
    if (cache / "init.gradle").exists() or (cache / "init.gradle.kts").exists() or (cache / "init.d").exists():
        raise RuntimeError("isolated Gradle cache contains an untrusted automatic init script")
    if (operation is not None
            and operation.options.get("workflow_version", 0) >= 26
            and tuple(args[:2]) == ("bash", "/workspace/gradlew")):
        cache_info = _seed_gradle_wrapper_distribution(
            operation, root, worktree, cache_name)
        if wrapper_cache_info is not None:
            wrapper_cache_info.clear()
            wrapper_cache_info.update(cache_info)
    from .dependency_build import dependency_mounts
    seed_mounts, args = dependency_mounts(root, args)
    command = ["bwrap", "--die-with-parent", "--new-session", "--unshare-pid"]
    for path in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(path).exists():
            command.extend(["--ro-bind", path, path])
    command.extend(["--dir", "/etc"])
    public_etc = [
        "/etc/alternatives", "/etc/ssl/certs", "/etc/ssl/openssl.cnf", "/etc/fonts", "/etc/X11",
        "/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/passwd", "/etc/group",
        "/etc/localtime",
    ]
    public_etc.extend(str(path) for path in Path("/etc").glob("java-*-openjdk"))
    for path in public_etc:
        if Path(path).exists():
            command.extend(["--ro-bind", path, path])
    path_value = "/usr/local/bin:/usr/bin:/bin"
    if java_home is not None:
        resolved_java_home = java_home.resolve()
        if not (resolved_java_home / "bin" / "java").is_file():
            raise RuntimeError("locked Java home is invalid")
        command.extend(["--ro-bind", str(resolved_java_home), "/java-home"])
        path_value = "/java-home/bin:" + path_value
    command.extend([
        "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp", "--dir", "/tmp/home",
        "--ro-bind" if readonly_workspace else "--bind", str(worktree), "/workspace",
        "--bind", str(cache), "/gradle-cache",
        "--clearenv", "--setenv", "PATH", path_value,
        "--setenv", "HOME", "/tmp/home", "--setenv", "GRADLE_USER_HOME", "/gradle-cache",
        "--setenv", "LANG", "C.UTF-8",
    ])
    if gradle_ro_cache is not None:
        shared = Path(gradle_ro_cache)
        if (not shared.is_absolute() or shared.is_symlink() or not shared.is_dir()
                or not (shared / "modules-2").is_dir()):
            raise RuntimeError("verified read-only Gradle cache is unavailable")
        command.extend([
            "--ro-bind", str(shared), "/gradle-ro-cache",
            "--setenv", "GRADLE_RO_DEP_CACHE", "/gradle-ro-cache",
        ])
    support = root / "artifacts" / "harness-support"
    if support.is_dir():
        if support.is_symlink() or support.resolve() != support.absolute():
            raise RuntimeError("unsafe client harness support directory")
        command.extend(["--ro-bind", str(support), "/modport-support"])
    wiring_support = root / 'artifacts' / 'harness-wiring'
    if wiring_support.exists():
        if (not wiring_support.is_dir() or wiring_support.is_symlink()
                or wiring_support.resolve() != wiring_support.absolute()
                or any(path.is_symlink() for path in wiring_support.rglob('*'))):
            raise RuntimeError('unsafe characterization wiring support directory')
        command.extend(['--ro-bind', str(wiring_support), '/modport-wiring'])
    artifact_runtime = operation.options.get('artifact_runtime_directory') if operation is not None else None
    if artifact_runtime is not None:
        directory = Path(artifact_runtime)
        if (not directory.is_relative_to(root / 'artifacts' / 'artifact-runtime')
                or directory.resolve() != directory.absolute()
                or not (directory / 'classes').is_dir()
                or any(path.is_symlink() for path in directory.rglob('*'))):
            raise RuntimeError('unsafe delivered artifact runtime directory')
        command.extend(['--ro-bind', str(directory), '/modport-artifact'])
        artifact_script = Path(operation.options['artifact_init_script'])
        if (artifact_script.parent != root / 'artifacts' / 'harness-wiring'
                or artifact_script.is_symlink() or not artifact_script.is_file()):
            raise RuntimeError('unsafe delivered artifact Gradle wiring')
        command.extend(['--setenv', 'MODPORT_ARTIFACT_INIT_SCRIPT',
                        '/modport-wiring/' + artifact_script.name])
    if java_home is not None:
        command.extend(["--setenv", "JAVA_HOME", "/java-home"])
    reserved = {"PATH", "HOME", "GRADLE_USER_HOME", "GRADLE_RO_DEP_CACHE", "JAVA_HOME"}
    for key, value in sorted(dict(environment or {}).items()):
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
            or key in reserved
            or not isinstance(value, str)
            or "\x00" in value
            or "\n" in value
        ):
            raise ValueError("invalid sandbox environment override")
        command.extend(["--setenv", key, value])
    git_pointer = worktree / ".git"
    repository = root / "repository.git"
    if git_pointer.is_file() and repository.is_dir():
        pointer = git_pointer.read_text(encoding="utf-8").strip()
        marker = "/worktrees/"
        if pointer.startswith("gitdir: ") and marker in pointer:
            admin_name = pointer.rsplit(marker, 1)[1]
            if admin_name and "/" not in admin_name and "\\" not in admin_name:
                command.extend([
                    "--ro-bind", str(repository), "/repository.git",
                    "--setenv", "GIT_DIR", f"/repository.git/worktrees/{admin_name}",
                    "--setenv", "GIT_WORK_TREE", "/workspace",
                ])
    command.extend(seed_mounts)
    command.extend(["--chdir", "/workspace", *args])
    return command


def _seed_gradle_wrapper_distribution(
    operation: OperationInput,
    root: Path,
    worktree: Path,
    cache_name: str,
) -> dict[str, Any]:
    """Seed the private phase home from a checksum-verified host archive."""
    return _seed_gradle_wrapper_from_request(
        _request(operation), root, worktree, cache_name,
        timeout_seconds=300, deadline_operation=operation,
    )


def _seed_gradle_wrapper_from_request(
    request: Mapping[str, Any], root: Path, worktree: Path,
    cache_name: str, *, timeout_seconds: float,
    deadline_operation: OperationInput | None = None,
) -> dict[str, Any]:
    """Share the same verified Wrapper seed with formal and MCP build gates."""
    diagnostic: dict[str, Any] = {"state": "disabled", "reason": "no_persistent_cache"}
    distribution = None
    gradle_home = root / "toolchains" / cache_name
    try:
        build_cache = _host_environment_build_cache(request)
        if build_cache is None:
            return diagnostic
        from .environment_wrapper_cache import (
            EnvironmentWrapperDistributionCache,
            load_wrapper_distribution,
        )
        distribution = load_wrapper_distribution(
            worktree / "gradle" / "wrapper" / "gradle-wrapper.properties")
        if distribution is None:
            return {"state": "bypassed", "reason": "unsupported_wrapper_layout"}
        version = distribution.filename[len("gradle-"):-len("-bin.zip")]
        if distribution.filename.endswith("-all.zip"):
            version = distribution.filename[len("gradle-"):-len("-all.zip")]
        timeout = (_remaining_timeout(deadline_operation, timeout_seconds)
                   if deadline_operation is not None else timeout_seconds)
        result = EnvironmentWrapperDistributionCache(build_cache.root).seed(
            distribution,
            gradle_home,
            timeout_seconds=timeout,
        )
        if result is None:
            return {"state": "miss", "reason": "trusted_distribution_unavailable",
                    "gradle_version": version}
        return {**result, "gradle_version": version}
    except TimeoutError:
        return {"state": "miss", "reason": "deadline_or_download_timeout"}
    except (OSError, ValueError, HTTPException, URLError) as exc:
        from .environment_wrapper_cache import UnsafeWrapperDistributionPath
        if isinstance(exc, UnsafeWrapperDistributionPath):
            raise RuntimeError("unsafe Gradle Wrapper installation path") from exc
        # A persistent cache is an optimization. Invalid host cache data is
        # never materialized; the normal Wrapper path remains usable.
        return {"state": "miss", "reason": "cache_or_distribution_validation_failed"}


def _locked_java_home(root: Path) -> Path:
    data = json.loads((root / "artifacts" / "locked-manifest.json").read_text(encoding="utf-8"))
    manifest = LockedManifest.from_mapping(data)
    relative = manifest.java_toolchain.get("executable", "")
    cache = (root / "toolchains" / "gradle-cache").resolve()
    executable = (cache / relative).resolve()
    if not relative or not executable.is_relative_to(cache) or not executable.is_file():
        raise RuntimeError("locked Java executable escapes its verified cache")
    return executable.parent.parent


def _client_launch_arguments(root: Path, command: OperationInput, workload: Sequence[str], *, timeout: float) -> list[str]:
    """Use only the Run's frozen host support, inside the build sandbox."""
    from .client_harness import client_harness_support_files
    expected = client_harness_support_files(workflow_version=command.options.get("workflow_version", 0))
    directory = root / "artifacts" / "harness-support"
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("client support contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != set(expected):
        raise ValueError("client support file set differs from the frozen bundle")
    for relative in expected:
        _, ref = _read_artifact_ref(root, command, "harness_support:" + relative)
        candidate = directory / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("client support file is unavailable")
    return ["python3", "-I", "/modport-support/launch.py", "--timeout", str(timeout), "--", *workload]


def _forge_baseline_init(root: Path, *, cache_name: str) -> str:
    path = root / "toolchains" / cache_name / "modport-forge-baseline.init.gradle"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        path.unlink()
    path.write_text(FORGE_BASELINE_INIT, encoding="utf-8")
    return "/gradle-cache/modport-forge-baseline.init.gradle"


def _baseline_changes_are_isolated(worktree: Path) -> tuple[bool, list[str]]:
    status = probe_process(pass_fds=current_lock_fds(), args=
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=worktree,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    if status.returncode:
        return False, ["git status failed", status.stdout]
    paths = [line[3:] for line in status.stdout.splitlines() if len(line) >= 4]
    modport = worktree / ".modport"
    safe_tree = not modport.is_symlink() and (not modport.exists() or (modport.is_dir() and modport.resolve() == modport.absolute()))
    if safe_tree and modport.exists():
        safe_tree = not any(path.is_symlink() for path in modport.rglob("*"))
    allowed = safe_tree and all(path == ".modport" or path.startswith(".modport/") for path in paths)
    return allowed, paths


def _snapshot_stage_output(
    root: Path,
    worktree: Path,
    command: OperationInput,
    relative: str,
) -> tuple[str, dict[str, Any]]:
    """Copy one stage output to a run-local artifact for later consumers.

    The copy is scoped to this command, so its location is a task identity;
    the output bytes are not used to decide whether another stage is stale.
    """

    relative_path = Path(relative)
    if relative_path.is_absolute() or not relative_path.parts or ".." in relative_path.parts:
        raise ValueError("stage output path must be relative and contained")
    source = worktree / relative_path
    if source.is_symlink() or source.resolve() != source.absolute() or not source.is_file():
        raise ValueError("stage output must be a contained regular file")
    data = source.read_bytes()
    artifact_relative = Path("artifacts") / "stage-outputs" / command.stage_id / command.command_id / relative_path
    artifact = root / artifact_relative
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if artifact.is_symlink() or (artifact.exists() and not artifact.is_file()):
        raise ValueError("stage output artifact path is unsafe")
    artifact.write_bytes(data)
    artifact_id = f"stage_output:{command.stage_id}:{relative}"
    return artifact_id, {
        "path": artifact_relative.as_posix(),
        "sha256": sha256(data).hexdigest(),
        "media_type": "application/json" if source.suffix == ".json" else "text/plain",
        "metadata": {"source_path": f"{worktree.name}/{relative}"},
    }


class ValidateInputHandler:
    """Resolve the immutable ref and create baseline + migration worktrees."""

    def __call__(self, command: OperationInput) -> OperationResult:
        root = _run_root(command)
        try:
            rubric = _acceptance_rubric_for(command, root)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _result(command, "failed", detail=f"acceptance rubric is invalid: {exc}", error_code="acceptance_rubric_invalid")
        request = _request(command)
        repository = str(request.get("source_repository", "")).strip()
        revision = str(request.get("source_revision", "HEAD")).strip()
        try:
            from .handoff_runtime import source_handoff
            handoff = source_handoff(root, command)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return _result(command, "failed", detail=f"artifact handoff is invalid: {exc}",
                           error_code="command_artifact_invalid")
        clone_source = repository if handoff is None else str(
            root / "artifacts" / "handoff" / handoff["repository_bundle"]["path"])
        if not repository:
            return _result(command, "failed", detail="source_repository is required", error_code="invalid_request")
        parsed = urlsplit(repository)
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            return _result(command, "failed", detail="source_repository must not embed credentials, query strings, or fragments", error_code="unsafe_repository_url")
        git_home = root / "toolchains" / "git-home"
        git_home.mkdir(parents=True, exist_ok=True)
        git_env = {
            "HOME": str(git_home), "XDG_CONFIG_HOME": str(git_home),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
        }
        bare = root / "repository.git"
        if not bare.exists():
            completed = _exec(["git", "clone", "--bare", clone_source, str(bare)], cwd=root, log=root / "logs" / "source-clone.log", timeout=_remaining_timeout(command, 900), env=git_env)
            if completed.returncode:
                return _result(command, "failed", detail="source clone failed", error_code="source_clone_failed")
        else:
            origin = probe_process(pass_fds=current_lock_fds(), args=["git", "--git-dir", str(bare), "config", "--get", "remote.origin.url"], env={**os.environ, **git_env}, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if origin.returncode or origin.stdout.strip() != clone_source:
                return _result(command, "failed", detail="existing bare repository does not match the request", error_code="source_repository_mismatch")
        if request.get("source_snapshot") is True:
            # The host captured current filesystem bytes. Do not re-apply the
            # author's checkout/encoding transforms when materializing them.
            from .platform_files import atomic_write
            information = bare / "info"
            information.mkdir(exist_ok=True)
            atomic_write(information / "attributes", b"* -text -filter -ident -working-tree-encoding\n")
        resolved = _exec(["git", "--git-dir", str(bare), "rev-parse", f"{revision}^{{commit}}"], cwd=root, log=root / "logs" / "source-resolve.log", timeout=_remaining_timeout(command, 60), env=git_env)
        if resolved.returncode:
            return _result(command, "failed", detail=f"cannot resolve source revision {revision}", error_code="source_ref_invalid")
        commit = resolved.stdout.strip().splitlines()[-1]
        # Some released source trees assume an annotated tag exists.  Add a
        # clone-local provenance tag without changing the immutable commit.
        described = probe_process(pass_fds=current_lock_fds(), args=["git", "--git-dir", str(bare), "describe", "--tags", "--long", commit], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        overlay: dict[str, Any] = {}
        if described.returncode:
            tag = "0.0.0-modport-baseline"
            tagged = _exec(["git", "--git-dir", str(bare), "-c", "user.name=ModPort", "-c", "user.email=modport@localhost", "tag", "-a", tag, "-m", "ModPort baseline provenance tag", commit], cwd=root, log=root / "logs" / "baseline-tag.log", timeout=60)
            if tagged.returncode == 0:
                overlay = {"kind": "local_git_tag", "tag": tag, "reason": "source has no describe-compatible release tag"}
        for name, branch in (("baseline", None), ("worktree", f"modport-{command.run_id}")):
            checkout_commit = (handoff["source"]["target_commit"]
                               if handoff is not None and name == "worktree" else commit)
            target = project_path(root, name)
            if name == 'worktree' and workspace_spec(root):
                from .workspace import initialize_engine_repository
                initialize_engine_repository(root, commit)
            if target.exists():
                current = _exec(["git", "-C", str(target), "rev-parse", "HEAD"],
                    cwd=root, log=root / 'logs' / f'check-{name}.log', timeout=60, env=git_env)
                if current.returncode or current.stdout.strip() != checkout_commit:
                    return _result(command, "failed", detail=f"existing {name} does not match immutable source commit", error_code="worktree_mismatch")
                continue
            args = ["git", "--git-dir", str(bare), "worktree", "add"]
            if branch:
                args.extend(["-b", branch])
            else:
                args.append("--detach")
            args.extend([str(target), checkout_commit])
            completed = _exec(args, cwd=root, log=root / "logs" / f"create-{name}.log", timeout=120)
            if completed.returncode:
                return _result(command, "failed", detail=f"could not create {name}", error_code="worktree_failed")
        evidence = {"source_repository": repository, "requested_revision": revision, "source_commit": commit, "baseline_overlay": overlay}
        if request.get("source_snapshot") is True:
            evidence["source_snapshot"] = True
        if handoff is not None:
            evidence["artifact_handoff"] = {
                "source_run_id": handoff["source"]["run_id"],
                "target_commit": handoff["source"]["target_commit"],
                "acceptance_status": "unverified",
                "scheduler_history_imported": False,
            }
        source_path = root / "artifacts" / "source.json"
        _write_json(source_path, evidence)
        _write_json(root / "request.json", request)
        return _result(
            command,
            "completed",
            outputs={
                **evidence,
                "artifact_refs": {
                    "source_evidence": {
                        "path": "artifacts/source.json",
                        "sha256": sha256(source_path.read_bytes()).hexdigest(),
                        "media_type": "application/json",
                    }
                },
            },
            detail=f"resolved source commit {commit}",
        )


def resolve_neoforge_versions(xml: bytes, minecraft_version: str) -> list[NeoForgeVersionCandidate]:
    root = ElementTree.fromstring(xml)
    candidates: list[NeoForgeVersionCandidate] = []
    parts = minecraft_version.split(".")
    coordinate = ".".join(parts[1:]) if len(parts) >= 3 and parts[0] == "1" else minecraft_version
    prefix = coordinate + "."
    for node in root.findall("./versioning/versions/version"):
        version = (node.text or "").strip()
        if not version.startswith(prefix):
            continue
        lowered = version.lower()
        channel = "beta" if "beta" in lowered else ("stable" if not re.search(r"-(alpha|snapshot|rc|pr)", lowered) else "other")
        candidates.append(NeoForgeVersionCandidate(version, channel, minecraft_version))
    return candidates


def _handoff_artifact_path(root: Path, command: OperationInput, handoff: Mapping[str, Any],
                           source_path: str) -> Path:
    """Resolve one artifact only when both the handoff manifest and its task ref agree."""
    rows = [item for item in handoff.get("artifacts", ())
            if isinstance(item, Mapping) and item.get("source_path") == source_path]
    if len(rows) != 1:
        raise ValueError(f"authenticated handoff does not select {source_path}")
    item = rows[0]
    maximum_size = (1024 * 1024 if source_path == "artifacts/locked-manifest.json"
                    else MAX_NEOFORGE_METADATA_BYTES)
    if (isinstance(item.get("size"), bool) or not isinstance(item.get("size"), int)
            or not 0 < item["size"] <= maximum_size):
        raise ValueError(f"authenticated handoff artifact size is invalid: {source_path}")
    artifact_id = "handoff:" + source_path
    reference = command.artifact_refs.get(artifact_id)
    if not isinstance(reference, Mapping):
        raise ValueError(f"authenticated handoff reference is missing: {source_path}")
    metadata = reference.get("metadata")
    if (reference.get("sha256") != item.get("sha256")
            or not isinstance(metadata, Mapping)
            or metadata.get("source_path") != source_path):
        raise ValueError(f"authenticated handoff reference disagrees with its manifest: {source_path}")
    expected = Path("artifacts/handoff") / str(item.get("path", ""))
    if reference.get("path") != expected.as_posix():
        raise ValueError(f"authenticated handoff reference path is invalid: {source_path}")
    from .evidence import verified_path
    path = verified_path(root, reference)
    if path != root / expected or file_digest(path) != item.get("sha256"):
        raise ValueError(f"authenticated handoff artifact checksum mismatch: {source_path}")
    return path


def _neoforge_metadata_from_handoff(root: Path, command: OperationInput,
                                    request: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """Use pinned metadata only when an authenticated old lock binds this exact request."""
    from .handoff_runtime import source_handoff

    handoff = source_handoff(root, command)
    if handoff is None:
        raise ValueError("there is no authenticated artifact-only handoff")

    source_path, _ = _read_artifact_ref(root, command, "source_evidence")
    source = json.loads(source_path.read_text(encoding="utf-8"))
    handoff_source = handoff["source"]
    if (source.get("source_repository") != request.get("source_repository")
            or source.get("source_commit") != handoff_source.get("source_commit")
            or source.get("requested_revision") != handoff_source.get("source_commit")):
        raise ValueError("current source evidence does not match the authenticated handoff")
    source_provenance = source.get("artifact_handoff")
    if (not isinstance(source_provenance, Mapping)
            or source_provenance.get("source_run_id") != handoff_source.get("run_id")
            or source_provenance.get("target_commit") != handoff_source.get("target_commit")):
        raise ValueError("current source evidence does not identify the authenticated handoff")

    lock_source_path = "artifacts/locked-manifest.json"
    metadata_source_path = "toolchains/neoforge-maven-metadata.xml"
    lock_path = _handoff_artifact_path(root, command, handoff, lock_source_path)
    metadata_path = _handoff_artifact_path(root, command, handoff, metadata_source_path)
    lock_data = json.loads(lock_path.read_text(encoding="utf-8"))
    if not isinstance(lock_data, Mapping):
        raise ValueError("authenticated old locked manifest is not an object")
    declared_digest = lock_data.get("manifest_sha256")
    unsigned_lock = {key: value for key, value in lock_data.items() if key != "manifest_sha256"}
    calculated_digest = sha256(canonical_json(unsigned_lock).encode("utf-8")).hexdigest()
    if not isinstance(declared_digest, str) or declared_digest != calculated_digest:
        raise ValueError("authenticated old locked manifest self-checksum is invalid")
    old_manifest = LockedManifest.from_mapping(lock_data)
    old_manifest.validate()

    current_request = MigrationRequest.from_mapping(request).to_dict()
    old_request = old_manifest.request.to_dict()
    identity_fields = (
        "mod_id", "source_repository", "source_minecraft", "target_minecraft",
        "source_loader", "source_loader_version", "target_loader", "target_loader_version",
        "source_java", "target_java", "source_revision", "mdk_revision",
    )
    for field in identity_fields:
        if old_request.get(field) != current_request.get(field):
            raise ValueError(f"old locked manifest request differs from this handoff: {field}")

    source_commit = handoff_source.get("source_commit")
    minecraft = str(request.get("target_minecraft", ""))
    exact_version = request.get("target_loader_version")
    target_java = request.get("target_java")
    expected_mdk_repository = MDK_REPOSITORY_TEMPLATE.format(minecraft=minecraft)
    if (old_manifest.source_commit != source_commit
            or old_manifest.request.source_revision != source_commit):
        raise ValueError("old locked manifest is not bound to the handoff source commit")
    if (not isinstance(exact_version, str) or not exact_version.strip()
            or old_manifest.neoforge_version != exact_version
            or old_manifest.request.target_loader_version != exact_version):
        raise ValueError("old locked manifest does not bind an exact requested NeoForge version")
    if (old_manifest.minecraft_version != minecraft
            or old_manifest.request.target_minecraft != minecraft):
        raise ValueError("old locked manifest Minecraft version differs from this handoff")
    if (not isinstance(target_java, str) or not target_java.strip()
            or old_manifest.java_version != target_java
            or old_manifest.request.target_java != target_java):
        raise ValueError("old locked manifest Java version differs from this handoff")
    if (old_manifest.mdk_repository != expected_mdk_repository
            or not old_manifest.mdk_commit
            or (request.get("mdk_revision")
                and old_manifest.mdk_commit != request.get("mdk_revision"))):
        raise ValueError("old locked manifest MDK provenance differs from this handoff")

    metadata = metadata_path.read_bytes()
    if len(metadata) > MAX_NEOFORGE_METADATA_BYTES:
        raise ValueError("authenticated NeoForge metadata XML exceeds its size limit")
    metadata_digest = sha256(metadata).hexdigest()
    if old_manifest.checksums.get("neoforge_maven_metadata_sha256") != metadata_digest:
        raise ValueError("old locked manifest does not authenticate the NeoForge metadata XML")
    exact_candidates = [candidate for candidate in resolve_neoforge_versions(metadata, minecraft)
                        if candidate.version == exact_version]
    if len(exact_candidates) != 1:
        raise ValueError("authenticated metadata XML does not contain one exact requested NeoForge version")
    if exact_candidates[0].channel != old_manifest.neoforge_channel:
        raise ValueError("old locked manifest channel differs from its metadata XML")
    provenance = {
        "kind": "authenticated_artifact_only_handoff",
        "source_run_id": handoff_source["run_id"],
        "handoff_manifest_sha256": handoff.get("manifest_sha256"),
        "locked_manifest_source_path": lock_source_path,
        "locked_manifest_sha256": file_digest(lock_path),
        "locked_manifest_digest": declared_digest,
        "metadata_source_path": metadata_source_path,
        "metadata_sha256": metadata_digest,
    }
    return metadata, provenance


def _pinned_neoforge_candidate(metadata: bytes, request: Mapping[str, Any]) -> NeoForgeVersionCandidate:
    minecraft = str(request.get("target_minecraft", ""))
    version = request.get("target_loader_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("exact target NeoForge version is not pinned")
    candidates = [candidate for candidate in resolve_neoforge_versions(metadata, minecraft)
                  if candidate.version == version]
    if len(candidates) != 1:
        raise ValueError("metadata XML does not contain exactly one requested NeoForge version")
    return select_neoforge_candidate(candidates, minecraft_version=minecraft)


def _neoforge_metadata_cache_location(request: Mapping[str, Any]):
    dependency_store = request.get("dependency_cache")
    if dependency_store is None:
        return None
    dependency_path = Path(str(dependency_store))
    if (not dependency_path.is_absolute()
            or ".." in dependency_path.parts):
        raise ValueError("dependency_cache must be absolute and contained before deriving environment cache")
    pinned = bool(request.get("target_loader_version"))
    identity = {
        "schema": (NEOFORGE_METADATA_CACHE_SCHEMA if pinned
                   else NEOFORGE_UNPINNED_METADATA_CACHE_SCHEMA),
        "target_loader": str(request.get("target_loader", "")),
        "target_minecraft": str(request.get("target_minecraft", "")),
        "target_loader_version": str(request.get("target_loader_version") or ""),
    }
    if (identity["target_loader"] != "neoforge"
            or not identity["target_minecraft"]):
        return None
    key = sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    cache_root = Path(os.path.abspath(dependency_path)).parent / "environment-cache"
    directory = "neoforge-metadata-v1" if pinned else "neoforge-unpinned-metadata-v1"
    cache_entry = cache_root / directory / f"{key}.json"
    return cache_root, cache_entry, key, identity


def _metadata_cache_candidate(metadata: bytes, identity: Mapping[str, Any]) -> NeoForgeVersionCandidate:
    if identity.get("target_loader_version"):
        return _pinned_neoforge_candidate(metadata, identity)
    candidates = resolve_neoforge_versions(metadata, str(identity.get("target_minecraft", "")))
    return select_neoforge_candidate(candidates, minecraft_version=identity["target_minecraft"])


def _validate_environment_cache_directory(path: Path) -> None:
    from .dependency_cache import _path as safe_path
    safe_path(path)
    try:
        info = os.stat(path, follow_symlinks=False)
    except FileNotFoundError:
        return
    from .platform_files import metadata_is_host_owned
    if not stat.S_ISDIR(info.st_mode) or not metadata_is_host_owned(info):
        raise ValueError("environment metadata cache directory is not host-owned and private")


def _validate_environment_cache_lock(lock_path: Path) -> None:
    from .dependency_cache import _path as safe_path
    safe_path(lock_path)
    info = os.stat(lock_path, follow_symlinks=False)
    from .platform_files import metadata_is_host_owned
    if not stat.S_ISREG(info.st_mode) or not metadata_is_host_owned(info):
        raise ValueError("environment metadata cache lock is not host-owned and private")


def _validate_environment_cache_source(source: Any) -> dict[str, Any]:
    if not isinstance(source, Mapping):
        raise ValueError("environment metadata cache provenance is missing")
    result = dict(source)
    kind = result.get("kind")
    if kind in {"official_maven_metadata", "qlu_maven_mirror"}:
        expected_url = MAVEN_METADATA if kind == "official_maven_metadata" else MAVEN_METADATA_MIRROR
        verified_at = result.get("verified_at_epoch")
        if (set(result) != {"kind", "source_url", "verified_at_epoch", "metadata_sha256"}
                or result.get("source_url") != expected_url
                or isinstance(verified_at, bool)
                or not isinstance(verified_at, (int, float))
                or not 0 < verified_at <= time.time() + 300):
            raise ValueError("environment metadata cache source provenance is incomplete or not allowlisted")
    elif kind == "authenticated_artifact_only_handoff":
        from .dependency_cache import _digest as valid_digest
        expected_fields = {
            "kind", "source_run_id", "handoff_manifest_sha256",
            "locked_manifest_source_path", "locked_manifest_sha256",
            "locked_manifest_digest", "metadata_source_path", "metadata_sha256",
        }
        if (set(result) != expected_fields
                or not isinstance(result.get("source_run_id"), str)
                or not result["source_run_id"]
                or result.get("locked_manifest_source_path") != "artifacts/locked-manifest.json"
                or result.get("metadata_source_path") != "toolchains/neoforge-maven-metadata.xml"):
            raise ValueError("environment metadata cache handoff provenance is incomplete")
        for field in ("handoff_manifest_sha256", "locked_manifest_sha256",
                      "locked_manifest_digest", "metadata_sha256"):
            valid_digest(result.get(field))
    else:
        raise ValueError("environment metadata cache source kind is unsupported")
    return result


def _load_environment_metadata_cache_locked(
    cache_root: Path,
    cache_entry: Path,
    key: str,
    identity: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any]] | None:
    from .dependency_cache import _parent_fd, _path as safe_path
    _validate_environment_cache_directory(cache_root)
    _validate_environment_cache_directory(cache_entry.parent)
    safe_path(cache_entry)
    try:
        with _parent_fd(cache_entry) as parent:
            fd = os.open(cache_entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        from .platform_files import metadata_is_host_owned
        if (not stat.S_ISREG(info.st_mode) or not metadata_is_host_owned(info)
                or info.st_size > MAX_NEOFORGE_METADATA_BYTES * 2):
            raise ValueError("environment metadata cache entry is unsafe or exceeds its size limit")
        raw = stream.read(MAX_NEOFORGE_METADATA_BYTES * 2 + 1)
        if len(raw) > MAX_NEOFORGE_METADATA_BYTES * 2:
            raise ValueError("environment metadata cache entry exceeds its size limit")

    def unique_object(pairs):
        result = {}
        for name, value in pairs:
            if name in result:
                raise ValueError("environment metadata cache record has duplicate keys")
            result[name] = value
        return result

    record = json.loads(raw, object_pairs_hook=unique_object)
    unpinned = not identity.get("target_loader_version")
    expected_fields = {
        "schema", "cache_key", "target_loader", "target_minecraft", "target_loader_version",
        "metadata_base64", "metadata_sha256", "resolved_channel", "source",
        "created_at_epoch", "record_sha256",
    }
    if unpinned:
        expected_fields.add("resolved_version")
    if not isinstance(record, Mapping) or set(record) != expected_fields:
        raise ValueError("environment metadata cache record has an unsupported shape")
    payload = {field: value for field, value in record.items() if field != "record_sha256"}
    expected_record_sha = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    if (record.get("schema") != identity.get("schema")
            or record.get("cache_key") != key
            or record.get("target_loader") != identity.get("target_loader")
            or record.get("target_minecraft") != identity.get("target_minecraft")
            or record.get("target_loader_version") != identity.get("target_loader_version")
            or record.get("record_sha256") != expected_record_sha):
        raise ValueError("environment metadata cache record identity or checksum is invalid")
    created_at = record.get("created_at_epoch")
    if (isinstance(created_at, bool) or not isinstance(created_at, (int, float))
            or not 0 < created_at <= time.time() + 300):
        raise ValueError("environment metadata cache timestamp is invalid")
    try:
        metadata = base64.b64decode(record["metadata_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("environment metadata cache XML encoding is invalid") from exc
    if (len(metadata) > MAX_NEOFORGE_METADATA_BYTES
            or sha256(metadata).hexdigest() != record.get("metadata_sha256")):
        raise ValueError("environment metadata cache XML checksum or size is invalid")
    candidate = _metadata_cache_candidate(metadata, identity)
    if (candidate.channel != record.get("resolved_channel")
            or (unpinned and candidate.version != record.get("resolved_version"))):
        raise ValueError("environment metadata cache selection differs from its XML")
    source = _validate_environment_cache_source(record.get("source"))
    if source.get("metadata_sha256") != record.get("metadata_sha256"):
        raise ValueError("environment metadata cache source digest differs from cached XML")
    if unpinned and time.time() - created_at >= NEOFORGE_UNPINNED_METADATA_TTL_SECONDS:
        return None
    provenance = {
        "kind": "persistent_environment_cache",
        "cache_state": "hit",
        "cache_key": key,
        "cache_path": cache_entry.relative_to(cache_root).as_posix(),
        "metadata_sha256": record["metadata_sha256"],
        "resolved_channel": candidate.channel,
        **({"resolved_version": candidate.version} if unpinned else {}),
        "source": source,
        "created_at_epoch": created_at,
    }
    return metadata, provenance


def _read_environment_metadata_cache(
    cache_root: Path,
    cache_entry: Path,
    key: str,
    identity: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any]] | None:
    from .dependency_cache import _lock as cache_lock
    lock_root = cache_root / ".locks"
    lock_name = f"neoforge-metadata-{key}.lock"
    with cache_lock(lock_root, lock_name):
        _validate_environment_cache_directory(cache_root)
        _validate_environment_cache_directory(lock_root)
        _validate_environment_cache_lock(lock_root / lock_name)
        return _load_environment_metadata_cache_locked(cache_root, cache_entry, key, identity)


def _publish_environment_metadata_cache(
    cache_root: Path,
    cache_entry: Path,
    key: str,
    identity: Mapping[str, Any],
    metadata: bytes,
    source: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    from .dependency_cache import _atomic as cache_atomic, _lock as cache_lock
    if len(metadata) > MAX_NEOFORGE_METADATA_BYTES:
        raise ValueError("environment metadata XML exceeds its size limit")
    candidate = _metadata_cache_candidate(metadata, identity)
    verified_source = _validate_environment_cache_source(source)
    metadata_digest = sha256(metadata).hexdigest()
    if verified_source.get("metadata_sha256") != metadata_digest:
        raise ValueError("environment metadata cache provenance digest differs from XML")
    record_payload = {
        "schema": identity["schema"],
        "cache_key": key,
        "target_loader": identity["target_loader"],
        "target_minecraft": identity["target_minecraft"],
        "target_loader_version": identity["target_loader_version"],
        "metadata_base64": base64.b64encode(metadata).decode("ascii"),
        "metadata_sha256": metadata_digest,
        "resolved_channel": candidate.channel,
        "source": verified_source,
        "created_at_epoch": time.time(),
    }
    if not identity["target_loader_version"]:
        record_payload["resolved_version"] = candidate.version
    record = {
        **record_payload,
        "record_sha256": sha256(canonical_json(record_payload).encode("utf-8")).hexdigest(),
    }
    lock_root = cache_root / ".locks"
    lock_name = f"neoforge-metadata-{key}.lock"
    with cache_lock(lock_root, lock_name):
        _validate_environment_cache_directory(cache_root)
        _validate_environment_cache_directory(lock_root)
        _validate_environment_cache_lock(lock_root / lock_name)
        existing = _load_environment_metadata_cache_locked(cache_root, cache_entry, key, identity)
        if existing is not None:
            return existing
        _validate_environment_cache_directory(cache_entry.parent)
        cache_entry.parent.mkdir(mode=0o755, exist_ok=True)
        _validate_environment_cache_directory(cache_entry.parent)
        cache_atomic(cache_entry, (canonical_json(record) + "\n").encode("utf-8"))
        published = _load_environment_metadata_cache_locked(cache_root, cache_entry, key, identity)
        if published is None:
            raise ValueError("environment metadata cache publication did not persist")
        metadata_out, provenance = published
        return metadata_out, {**provenance, "cache_state": "stored"}


def _metadata_failure_label(error: Exception) -> str:
    status = getattr(error, "code", None)
    if type(status) is int:
        return f"HTTP {status}"
    if isinstance(error, ValueError):
        return str(error)
    return type(error).__name__


def _current_handoff_metadata_if_present(
    root: Path,
    command: OperationInput,
    request: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any]] | None:
    refs = command.artifact_refs
    lock_ref = "handoff:artifacts/locked-manifest.json" in refs
    metadata_ref = "handoff:toolchains/neoforge-maven-metadata.xml" in refs
    if not lock_ref and not metadata_ref:
        return None
    if not isinstance(refs.get("artifact_handoff"), Mapping):
        raise ValueError("metadata handoff artifacts have no authenticated handoff manifest reference")
    if not lock_ref or not metadata_ref:
        raise ValueError("authenticated metadata handoff is incomplete: locked manifest and XML are both required")
    try:
        metadata, provenance = _neoforge_metadata_from_handoff(root, command, request)
        _pinned_neoforge_candidate(metadata, request)
        return metadata, provenance
    except Exception as exc:
        detail = _metadata_failure_label(exc)
        raise ValueError(f"authenticated metadata handoff was rejected: {detail}") from exc


def _cache_authenticated_handoff_metadata(
    cache_location: tuple[Path, Path, str, Mapping[str, Any]],
    metadata: bytes,
    handoff_provenance: Mapping[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    cache_metadata, cache_provenance = _publish_environment_metadata_cache(
        *cache_location, metadata, handoff_provenance,
    )
    handoff_digest = sha256(metadata).hexdigest()
    cache_digest = sha256(cache_metadata).hexdigest()
    provenance = {
        **dict(handoff_provenance),
        "cache_key": cache_provenance["cache_key"],
        "cache_path": cache_provenance["cache_path"],
    }
    if cache_digest == handoff_digest:
        provenance["cache_state"] = cache_provenance.get("cache_state", "hit")
    else:
        provenance["cache_state"] = "conflict"
        provenance["cache_conflict"] = {
            "cached_metadata_sha256": cache_digest,
            "handoff_metadata_sha256": handoff_digest,
            "cached_source": cache_provenance["source"],
        }
    return metadata, provenance


def _fetch_neoforge_metadata(url: str, source_kind: str, command: OperationInput,
                             request: Mapping[str, Any]):
    with urlopen(url, timeout=_remaining_timeout(command, 60)) as response:
        geturl = getattr(response, "geturl", None)
        final_url = geturl() if callable(geturl) else url
        if final_url != url:
            raise ValueError("NeoForge metadata request redirected outside its allowlisted URL")
        metadata = response.read(MAX_NEOFORGE_METADATA_BYTES + 1)
    if len(metadata) > MAX_NEOFORGE_METADATA_BYTES:
        raise ValueError("NeoForge metadata XML exceeds its size limit")
    if request.get("target_loader_version"):
        candidate = _pinned_neoforge_candidate(metadata, request)
    else:
        minecraft = str(request.get("target_minecraft", ""))
        candidate = select_neoforge_candidate(
            resolve_neoforge_versions(metadata, minecraft),
            minecraft_version=minecraft,
        )
    provenance = {
        "kind": source_kind,
        "source_url": url,
        "verified_at_epoch": time.time(),
    }
    return metadata, candidate, provenance


def _resolve_neoforge_metadata(root: Path, command: OperationInput,
                               request: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
    exact_version = request.get("target_loader_version")
    handoff_metadata = (
        _current_handoff_metadata_if_present(root, command, request)
        if exact_version else None
    )
    cache_location = _neoforge_metadata_cache_location(request)
    if cache_location is not None:
        cache_root, cache_entry, key, identity = cache_location
        cached = _read_environment_metadata_cache(cache_root, cache_entry, key, identity)
        if exact_version and handoff_metadata is not None:
            return _cache_authenticated_handoff_metadata(
                cache_location, handoff_metadata[0], handoff_metadata[1],
            )
        if cached is not None:
            return cached

    if handoff_metadata is not None:
        metadata, provenance = handoff_metadata
        if cache_location is None:
            return metadata, {**provenance, "cache_state": "disabled"}
        return _cache_authenticated_handoff_metadata(cache_location, metadata, provenance)

    def fetch_verified_metadata():
        network_errors: list[str] = []
        for url, kind in ((MAVEN_METADATA, "official_maven_metadata"),
                          (MAVEN_METADATA_MIRROR, "qlu_maven_mirror")):
            try:
                metadata, _, provenance = _fetch_neoforge_metadata(url, kind, command, request)
            except Exception as exc:
                network_errors.append(f"{kind}: {_metadata_failure_label(exc)}")
                continue
            provenance["metadata_sha256"] = sha256(metadata).hexdigest()
            if cache_location is None:
                return metadata, {**provenance, "cache_state": "disabled"}
            return _publish_environment_metadata_cache(*cache_location, metadata, provenance)
        suffix = "; ".join(network_errors) if network_errors else "no eligible source"
        if exact_version:
            raise ValueError(f"no verified pinned NeoForge metadata source succeeded ({suffix})")
        raise ValueError(f"no unpinned NeoForge metadata source succeeded ({suffix})")

    if cache_location is None:
        return fetch_verified_metadata()
    from .dependency_cache import _lock as cache_lock
    lock_root = cache_root / ".locks"
    lock_name = f"neoforge-metadata-fetch-{key}.lock"
    with cache_lock(lock_root, lock_name, timeout_seconds=_remaining_timeout(command, 130)):
        _validate_environment_cache_directory(cache_root)
        _validate_environment_cache_directory(lock_root)
        _validate_environment_cache_lock(lock_root / lock_name)
        # Other Runs can fill this entry while we wait for the fetch lock.
        cached = _read_environment_metadata_cache(cache_root, cache_entry, key, identity)
        if cached is not None:
            return cached
        return fetch_verified_metadata()


def _host_environment_build_cache(request: Mapping[str, Any]):
    dependency_store = request.get("dependency_cache")
    if dependency_store is None:
        return None
    dependency_path = Path(str(dependency_store))
    if not dependency_path.is_absolute() or ".." in dependency_path.parts:
        raise ValueError("dependency_cache must be an absolute path without traversal")
    from .environment_build_cache import EnvironmentBuildCache
    return EnvironmentBuildCache(Path(os.path.abspath(dependency_path)).parent / "environment-cache")


def _contract_asset_cache_context(command: OperationInput, root: Path,
                                  cache_name: str, *, baseline: bool):
    """Locate a private Gradle asset home using an authenticated Mojang root."""
    if command.options.get("workflow_version", 0) < 26:
        return None
    request = _request(command)
    build_cache = _host_environment_build_cache(request)
    if build_cache is None:
        return None
    manifest = LockedManifest.from_mapping(json.loads(
        (root / "artifacts" / "locked-manifest.json").read_text(encoding="utf-8")))
    manifest.validate()
    if not all((manifest.mdk_repository, manifest.mdk_commit,
                manifest.minecraft_version, manifest.gradle_version)):
        return None
    from .environment_neoform_cache import EnvironmentNeoFormCache
    launcher = EnvironmentNeoFormCache(build_cache.root).verified_launcher_manifest(
        mdk_repository=manifest.mdk_repository,
        mdk_commit=manifest.mdk_commit,
        minecraft_version=manifest.minecraft_version,
        neoforge_version=manifest.neoforge_version,
        gradle_version=manifest.gradle_version,
    )
    if launcher is None:
        return None
    gradle_home = root / "toolchains" / cache_name
    if baseline:
        minecraft = request["source_minecraft"]
        forge_cache = gradle_home / "caches" / "forge_gradle"
        version_path = (forge_cache / "minecraft_repo" / "versions" /
                        minecraft / "version.json")
        assets_root = forge_cache / "assets"
    else:
        minecraft = manifest.minecraft_version
        runtime = root / "toolchains" / "gradle-cache" / "caches" / "neoformruntime"
        version_path = runtime / "artifacts" / f"minecraft_{minecraft}_version_manifest.json"
        assets_root = gradle_home / "caches" / "neoformruntime" / "assets"
    if version_path.is_symlink():
        raise ValueError("private Minecraft version manifest is a symlink")
    if not version_path.exists():
        return None
    from .environment_asset_cache import EnvironmentAssetCache
    cache = EnvironmentAssetCache(build_cache.root)
    return cache, {"assets_root": assets_root,
                   "minecraft_version": minecraft,
                   "authenticated_launcher_manifest": launcher,
                   "version_manifest_path": version_path}


def _hash_sandbox_java(gradle_home: Path, relative: Path) -> str:
    """Hash a sandbox-created Java launcher without following links or blocking on FIFOs."""
    if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) < 4
            or relative.parts[0] != "jdks"
            or relative.parts[-2:] != ("bin", "java.exe" if os.name == 'nt' else "java")):
        raise ValueError("invalid Java launcher path")
    max_bytes = 64 * 1024 * 1024
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    directory = os.open(gradle_home, flags | os.O_DIRECTORY)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative.name, flags | os.O_NONBLOCK, dir_fd=directory)
        try:
            before = os.fstat(descriptor)
            from .platform_files import metadata_is_host_owned
            if (not stat.S_ISREG(before.st_mode) or not metadata_is_host_owned(before)
                    or before.st_size <= 0 or before.st_size > max_bytes):
                raise ValueError("Java launcher is not a bounded host-owned regular file")
            digest = sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError("Java launcher exceeds the size limit")
                digest.update(chunk)
            after = os.fstat(descriptor)
            current = os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if total != before.st_size or identity(before) != identity(after) or identity(before) != identity(current):
                raise ValueError("Java launcher changed while being hashed")
            return digest.hexdigest()
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


class LockEnvironmentHandler:
    def __call__(self, command: OperationInput) -> OperationResult:
        root = _run_root(command)
        request = _request(command)
        minecraft = str(request.get("target_minecraft", ""))
        try:
            metadata, metadata_provenance = _resolve_neoforge_metadata(root, command, request)
        except Exception as exc:
            return _result(command, "failed", detail=f"NeoForge resolution failed: {exc}", error_code="version_resolution_failed")
        try:
            candidates = resolve_neoforge_versions(metadata, minecraft)
            if request.get("target_loader_version"):
                candidates = [candidate for candidate in candidates if candidate.version == request["target_loader_version"]]
            selected = select_neoforge_candidate(candidates, minecraft_version=minecraft)
        except Exception as exc:
            return _result(command, "failed", detail=f"NeoForge resolution failed: {exc}", error_code="version_resolution_failed")
        source = json.loads((root / "artifacts" / "source.json").read_text(encoding="utf-8"))
        metadata_path = root / "toolchains" / "neoforge-maven-metadata.xml"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_bytes(metadata)
        mdk_repository = MDK_REPOSITORY_TEMPLATE.format(minecraft=minecraft)
        mdk = root / "toolchains" / "mdk"
        try:
            build_cache = _host_environment_build_cache(request)
        except (OSError, ValueError) as exc:
            return _result(command, "failed", detail=f"environment build cache is invalid: {exc}",
                           error_code="environment_cache_invalid")
        mdk_seed = None
        if build_cache is not None and request.get("mdk_revision"):
            try:
                mdk_seed = build_cache.try_materialize_mdk(
                    mdk_repository, request["mdk_revision"], mdk)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return _result(command, "failed", detail=f"verified MDK cache is invalid: {exc}",
                               error_code="environment_cache_invalid")
        mdk_cache_hit = mdk_seed is not None
        git_home = root / "toolchains" / "git-home"
        git_home.mkdir(parents=True, exist_ok=True)
        git_env = {"HOME": str(git_home), "XDG_CONFIG_HOME": str(git_home), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false"}
        if not mdk.exists():
            mdk.parent.mkdir(parents=True, exist_ok=True)
            cloned = _exec(["git", "clone", "--depth", "1", mdk_repository, str(mdk)], cwd=root, log=root / "logs" / "mdk-clone.log", timeout=_remaining_timeout(command, 600), env=git_env)
            if cloned.returncode:
                return _result(command, "failed", detail="official ModDevGradle MDK clone failed", error_code="mdk_clone_failed")
        if request.get("mdk_revision") and mdk_seed is None:
            revision = request["mdk_revision"]
            fetched = _exec(["git", "fetch", "--depth", "1", "origin", revision], cwd=mdk,
                log=root / "logs" / "mdk-fetch.log", timeout=_remaining_timeout(command, 600), env=git_env)
            if fetched.returncode:
                return _result(command, "failed", detail="fixed MDK commit fetch failed", error_code="mdk_ref_invalid")
            checked = _exec(["git", "checkout", "--detach", revision], cwd=mdk,
                log=root / "logs" / "mdk-checkout.log", timeout=60, env=git_env)
            if checked.returncode:
                return _result(command, "failed", detail="fixed MDK commit checkout failed", error_code="mdk_ref_invalid")
        origin = probe_process(pass_fds=current_lock_fds(), args=["git", "-C", str(mdk), "config", "--get", "remote.origin.url"], env={**os.environ, **git_env}, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        cleanliness = probe_process(pass_fds=current_lock_fds(), args=["git", "-C", str(mdk), "status", "--porcelain", "--untracked-files=no"], env={**os.environ, **git_env}, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if origin.returncode or origin.stdout.strip() != mdk_repository or cleanliness.returncode or cleanliness.stdout.strip():
            return _result(command, "failed", detail="official MDK checkout provenance or cleanliness check failed", error_code="mdk_provenance_invalid")
        resolved_mdk = _exec(["git", "rev-parse", "HEAD"], cwd=mdk, log=root / "logs" / "mdk-resolve.log", timeout=60)
        if resolved_mdk.returncode:
            return _result(command, "failed", detail="official MDK commit resolution failed", error_code="mdk_ref_invalid")
        mdk_commit = resolved_mdk.stdout.strip().splitlines()[-1]
        checksums = {"neoforge_maven_metadata_sha256": sha256(metadata).hexdigest()}
        checksums["operations:forge_baseline_init"] = sha256(FORGE_BASELINE_INIT.encode("utf-8")).hexdigest()
        mdk_paths = ("build.gradle", "settings.gradle", "gradle.properties", "gradle/wrapper/gradle-wrapper.properties")
        if command.options.get('workflow_version', 0) >= 19:
            mdk_paths += ('gradlew', 'gradlew.bat', 'gradle/wrapper/gradle-wrapper.jar',
                          'src/main/templates/META-INF/neoforge.mods.toml',
                          'src/main/resources/META-INF/neoforge.mods.toml')
        for relative in mdk_paths:
            path = mdk / relative
            if path.exists():
                checksums[f"mdk:{relative}"] = sha256(path.read_bytes()).hexdigest()
        gradle_version = None
        wrapper_properties = mdk / "gradle" / "wrapper" / "gradle-wrapper.properties"
        if wrapper_properties.exists():
            match = re.search(r"gradle-([0-9.]+)-(?:bin|all)\.zip", wrapper_properties.read_text(encoding="utf-8"))
            gradle_version = match.group(1) if match else None
        build_text = (mdk / "build.gradle").read_text(encoding="utf-8")
        java_match = re.search(r"JavaLanguageVersion\.of\((\d+)\)", build_text)
        if java_match is None:
            return _result(command, "failed", detail="official MDK does not declare a Java toolchain", error_code="java_toolchain_unlocked")
        required_java = java_match.group(1)
        if request.get("target_java") and str(request["target_java"]) != required_java:
            return _result(command, "blocked", detail="requested Java conflicts with fixed MDK", error_code="java_toolchain_mismatch")
        if request.get("mdk_revision") and mdk_commit != request["mdk_revision"]:
            return _result(command, "blocked", detail="MDK commit differs from request", error_code="mdk_ref_invalid")
        if build_cache is not None and request.get("mdk_revision") and mdk_seed is None:
            try:
                mdk_seed = build_cache.publish_mdk_bundle(mdk, mdk_repository, mdk_commit)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return _result(command, "failed", detail=f"verified MDK cache publication failed: {exc}",
                               error_code="environment_cache_invalid")
        gradle_snapshot = None
        gradle_ro_cache = None
        toolchain_cache = None
        toolchain_seed = None
        neoform_cache = None
        neoform_seed = None
        if build_cache is not None and request.get("mdk_revision") and gradle_version:
            try:
                snapshots = build_cache.compatible_gradle_snapshots(
                    gradle_version, mdk_repository=mdk_repository,
                    mdk_commit=mdk_commit, neoforge_version=selected.version)
                if snapshots:
                    gradle_snapshot = snapshots[0]
                    gradle_ro_cache = build_cache.read_only_gradle_cache(
                        gradle_snapshot["snapshot_key"], gradle_version,
                        mdk_repository=mdk_repository, mdk_commit=mdk_commit,
                        neoforge_version=selected.version)
            except (OSError, ValueError) as exc:
                return _result(command, "failed", detail=f"verified Gradle cache is invalid: {exc}",
                               error_code="environment_cache_invalid")
            from .environment_toolchain_cache import EnvironmentToolchainCache
            toolchain_cache = EnvironmentToolchainCache(build_cache.root)
            try:
                toolchain_seed = toolchain_cache.try_materialize_toolchains(
                    root, mdk_repository=mdk_repository, mdk_commit=mdk_commit,
                    neoforge_version=selected.version, gradle_version=gradle_version,
                    java_version=required_java,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return _result(command, "failed", detail=f"verified Wrapper/JDK cache is invalid: {exc}",
                               error_code="environment_cache_invalid")
            if command.options.get("workflow_version", 0) >= 25:
                from .environment_neoform_cache import EnvironmentNeoFormCache
                neoform_cache = EnvironmentNeoFormCache(build_cache.root)
                try:
                    neoform_seed = neoform_cache.try_materialize(
                        root / "toolchains" / "gradle-cache",
                        mdk_repository=mdk_repository, mdk_commit=mdk_commit,
                        minecraft_version=minecraft, neoforge_version=selected.version,
                        gradle_version=gradle_version,
                    )
                except (OSError, ValueError) as exc:
                    return _result(command, "failed", detail=f"verified NeoForm cache is invalid: {exc}",
                                   error_code="environment_cache_invalid")
        wrapper_cache_info: dict[str, Any] = {}
        try:
            bootstrap_task_args = ["bash", "/workspace/gradlew", "--no-daemon",
                                   f"-Pneo_version={selected.version}"]
            if neoform_seed is not None:
                from .environment_neoform_cache import LAUNCHER_MANIFEST_PROPERTY
                bootstrap_task_args.append(LAUNCHER_MANIFEST_PROPERTY)
                if gradle_ro_cache is not None and toolchain_seed is not None:
                    bootstrap_task_args.append("--offline")
            bootstrap_task_args.append("compileJava")
            bootstrap_args = _sandboxed_build_command(
                root, mdk, bootstrap_task_args,
                gradle_ro_cache=gradle_ro_cache,
                operation=command,
                wrapper_cache_info=wrapper_cache_info,
            )
            bootstrap = _exec(bootstrap_args, cwd=root, log=root / "logs" / "mdk-java-toolchain.log", timeout=_remaining_timeout(command, 3600))
        except (RuntimeError, TimeoutError, subprocess.TimeoutExpired) as exc:
            return _result(command, "failed",
                           outputs=_wrapper_cache_diagnostic(command, wrapper_cache_info),
                           detail=f"Java {required_java} toolchain bootstrap failed: {exc}",
                           error_code="java_toolchain_failed")
        if bootstrap.returncode:
            from .dependency_build import gradle_failure_kind
            kind = gradle_failure_kind(bootstrap.stdout)
            return _result(command, "failed", outputs={
                "log": "logs/mdk-java-toolchain.log",
                **_wrapper_cache_diagnostic(command, wrapper_cache_info),
            }, detail=f"official MDK did not build with Java {required_java}",
                           error_code=kind if kind != "gradle_failed" else "java_toolchain_failed")
        java_executables = sorted((root / "toolchains" / "gradle-cache" / "jdks").glob(
            "**/bin/java.exe" if os.name == 'nt' else "**/bin/java"))
        verified_java: Path | None = None
        version_output = ""
        verification_log = None
        gradle_home = root / "toolchains" / "gradle-cache"
        for index, executable in enumerate(java_executables):
            # The MDK build can write this tree. Never execute a discovered JDK
            # on the host, even for a version check: a build plugin could have
            # replaced bin/java with an arbitrary program. Keep verification
            # inside the same credential-free build sandbox.
            try:
                relative = executable.relative_to(gradle_home)
                unsafe_path = (any(part.is_symlink() for part in (gradle_home, *executable.parents)
                                   if part == gradle_home or gradle_home in part.parents)
                               or not stat.S_ISREG(executable.lstat().st_mode))
            except (OSError, ValueError):
                continue
            if unsafe_path:
                continue
            probe_log = root / "logs" / f"mdk-java-version-{index}.log"
            try:
                probe_args = _sandboxed_build_command(
                    root, mdk, [f"/gradle-cache/{relative.as_posix()}", "-version"],
                    gradle_ro_cache=gradle_ro_cache,
                    operation=command,
                )
                probe = _exec(
                    probe_args, cwd=root,
                    log=probe_log,
                    timeout=_remaining_timeout(command, 30),
                )
            except TimeoutError as exc:
                return _result(command, "failed", detail=f"Java version probe deadline exhausted: {exc}",
                               error_code="java_toolchain_failed")
            except subprocess.TimeoutExpired:
                return _result(command, "failed", outputs={"log": str(project_relative(root, probe_log))},
                               detail="Java version probe timed out", error_code="java_toolchain_failed")
            except (OSError, RuntimeError):
                continue
            if probe.returncode == 0 and re.search(rf'\bversion "{re.escape(required_java)}(?:[.\"]|\b)', probe.stdout):
                verified_java, version_output, verification_log = executable, probe.stdout.strip(), str(project_relative(root, probe_log))
                break
        if verified_java is None:
            return _result(command, "failed", detail=f"Java {required_java} executable was not verified after MDK build", error_code="java_toolchain_unverified")
        try:
            java_digest = _hash_sandbox_java(gradle_home, verified_java.relative_to(gradle_home))
        except (OSError, ValueError) as exc:
            return _result(command, "failed", outputs={"log": verification_log},
                           detail=f"Java launcher changed after sandbox verification: {exc}",
                           error_code="java_toolchain_unverified")
        published_gradle_snapshot = None
        if (build_cache is not None and request.get("mdk_revision")
                and gradle_version and gradle_snapshot is None):
            try:
                published_gradle_snapshot = build_cache.publish_gradle_modules(
                    root / "toolchains" / "gradle-cache" / "caches" / "modules-2",
                    gradle_version, mdk_repository=mdk_repository,
                    mdk_commit=mdk_commit, neoforge_version=selected.version,
                    mdk_bootstrap_succeeded=True)
            except (OSError, ValueError) as exc:
                return _result(command, "failed", detail=f"verified Gradle cache publication failed: {exc}",
                               error_code="environment_cache_invalid")
        published_toolchains = None
        if toolchain_cache is not None and toolchain_seed is None:
            try:
                published_toolchains = toolchain_cache.publish_bootstrap_toolchains(
                    root, mdk_bootstrap_succeeded=True,
                    mdk_repository=mdk_repository, mdk_commit=mdk_commit,
                    neoforge_version=selected.version, gradle_version=gradle_version,
                    java_version=required_java, verified_java=verified_java,
                    verified_java_sha256=java_digest,
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return _result(command, "failed", detail=f"verified Wrapper/JDK cache publication failed: {exc}",
                               error_code="environment_cache_invalid")
        published_neoform = None
        neoform_publish_error = None
        if neoform_cache is not None and neoform_seed is None:
            try:
                published_neoform = neoform_cache.publish_bootstrap(
                    gradle_home / "caches" / "neoformruntime",
                    mdk_repository=mdk_repository, mdk_commit=mdk_commit,
                    minecraft_version=minecraft, neoforge_version=selected.version,
                    gradle_version=gradle_version, mdk_bootstrap_succeeded=True,
                )
            except (OSError, ValueError) as exc:
                # A completed official MDK build remains valid if this optional
                # derived seed cannot be published. Retain the diagnostic.
                neoform_publish_error = str(exc)
        manifest = LockedManifest(
            request=MigrationRequest.from_mapping(request),
            neoforge_version=selected.version, neoforge_channel=selected.channel,
            minecraft_version=minecraft, source_commit=source["source_commit"], java_version=required_java,
            java_toolchain={
                "executable": str(verified_java.relative_to(root / "toolchains" / "gradle-cache")),
                "java_sha256": java_digest,
                "version_output": version_output,
                "verification_log": verification_log,
            },
            gradle_version=gradle_version, mdk_repository=mdk_repository, mdk_commit=mdk_commit,
            sdk_version=sdk_release(), workflow_version=WORKFLOW_VERSION, checksums=checksums,
        )
        payload = manifest.to_dict()
        digest = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        payload["manifest_sha256"] = digest
        manifest_path = root / "artifacts" / "locked-manifest.json"
        _write_json(manifest_path, payload)
        return _result(
            command,
            "completed",
            outputs={
                "neoforge_version": selected.version,
                "manifest_sha256": digest,
                "locked_artifacts": {"manifest_sha256": digest},
                "metadata_resolution": metadata_provenance,
                "build_cache": {
                    "mdk_bundle": None if mdk_seed is None else {
                        "cache_key": mdk_seed["cache_key"],
                        "bundle_sha256": mdk_seed["bundle_sha256"],
                        "state": "hit" if mdk_cache_hit else "stored",
                    },
                    "gradle_input": None if gradle_snapshot is None else {
                        "snapshot_key": gradle_snapshot["snapshot_key"],
                        "content_sha256": gradle_snapshot["content_sha256"],
                    },
                    "gradle_published": None if published_gradle_snapshot is None else {
                        "snapshot_key": published_gradle_snapshot["snapshot_key"],
                        "content_sha256": published_gradle_snapshot["content_sha256"],
                    },
                    "toolchains_input": None if toolchain_seed is None else {
                        "cache_key": toolchain_seed["cache_key"],
                        "java_sha256": toolchain_seed["identity"]["java_sha256"],
                        "java_reused": java_digest == toolchain_seed["identity"]["java_sha256"],
                    },
                    "toolchains_published": None if published_toolchains is None else {
                        "cache_key": published_toolchains["cache_key"],
                        "java_sha256": published_toolchains["identity"]["java_sha256"],
                    },
                    "neoform_input": None if neoform_seed is None else {
                        "snapshot_key": neoform_seed["snapshot_key"],
                        "content_sha256": neoform_seed["content_sha256"],
                    },
                    "neoform_published": None if published_neoform is None else {
                        "snapshot_key": published_neoform["snapshot_key"],
                        "content_sha256": published_neoform["content_sha256"],
                    },
                    "neoform_publish_error": neoform_publish_error,
                    **({"wrapper_distribution": wrapper_cache_info}
                       if command.options.get("workflow_version", 0) >= 26 else {}),
                },
                "artifact_refs": {
                    "locked_manifest": {
                        "path": "artifacts/locked-manifest.json",
                        "sha256": sha256(manifest_path.read_bytes()).hexdigest(),
                        "media_type": "application/json",
                    }
                },
            },
            detail=f"locked NeoForge {selected.version} with verified Java {required_java}",
        )


class GradleHandler:
    def __init__(self, *, baseline: bool, tasks: Sequence[str], name: str, cache_name: str | None = None) -> None:
        self.baseline = baseline
        self.tasks = tuple(tasks)
        self.name = name
        self.cache_name = cache_name or ("baseline-gradle-cache" if baseline else "target-gradle-cache")

    def __call__(self, command: OperationInput) -> OperationResult:
        root = _run_root(command)
        gates_disabled = business_gates_disabled(command)
        scope_version = command.options.get("workflow_version", 0)
        defer_runtime_checks = (compile_package_scope(command)
                                and (not self.baseline or scope_version >= 28))
        requested_tasks = tuple(task for task in self.tasks
                                if not (defer_runtime_checks and task == "runGameTestServer")
                                and not (defer_runtime_checks and scope_version >= 28
                                         and task == "runData"))
        excluded_tasks = (("check", "test") if self.baseline and scope_version >= 31
                          else ("check", "test", "runGameTestServer")) if defer_runtime_checks else ()
        scope_outputs = ({"gradle_tasks": list(requested_tasks),
                          "excluded_gradle_tasks": list(excluded_tasks)}
                         if defer_runtime_checks else {})
        worktree = project_path(root, "baseline" if self.baseline else "worktree")
        wrapper = worktree / "gradlew"
        if not wrapper.exists():
            return _result(command, "failed", detail="Gradle wrapper is missing", error_code="gradle_wrapper_missing")
        wrapper_cache_info: dict[str, Any] = {}
        try:
            gradle = ["bash", "/workspace/gradlew", "--no-daemon"]
            if self.baseline:
                gradle.extend(["--init-script", _forge_baseline_init(root, cache_name=self.cache_name)])
            gradle.extend(requested_tasks)
            for task in excluded_tasks:
                gradle.extend(["-x", task])
            java_home = None if self.baseline else _locked_java_home(root)
            args = _sandboxed_build_command(
                root, worktree, gradle, cache_name=self.cache_name,
                java_home=java_home, operation=command,
                wrapper_cache_info=wrapper_cache_info)
            completed = _exec(args, cwd=root, log=root / "logs" / f"{self.name}.log", timeout=_remaining_timeout(command, 7200))
        except TimeoutError:
            return _result(command, "failed",
                           outputs={**scope_outputs, **_wrapper_cache_diagnostic(command, wrapper_cache_info)},
                           detail="run wall-clock budget exhausted", error_code="budget_exhausted")
        except RuntimeError as exc:
            return _result(command, "failed",
                           outputs={**scope_outputs, **_wrapper_cache_diagnostic(command, wrapper_cache_info)},
                           detail=str(exc), error_code="build_sandbox_unavailable")
        except subprocess.TimeoutExpired:
            return _result(command, "failed", outputs={**_file_log_outputs(root, f'logs/{self.name}.log'),
                **_wrapper_cache_diagnostic(command, wrapper_cache_info),
                **scope_outputs,
                **({'process_executed': True} if command.options.get('workflow_version', 0) >= 18 else {})},
                           detail=f"{self.name} timed out", error_code="gradle_timeout")
        if completed.returncode:
            signature = sha256(completed.stdout[-8000:].encode("utf-8", errors="replace")).hexdigest()[:16]
            log_path = root / "logs" / f"{self.name}.log"
            outputs: dict[str, Any] = {
                "failure_signature": signature,
                "log": f"logs/{self.name}.log",
                **_wrapper_cache_diagnostic(command, wrapper_cache_info),
                **scope_outputs,
                **({'process_executed': True} if command.options.get('workflow_version', 0) >= 18 else {}),
            }
            if log_path.is_file():
                outputs["artifact_refs"] = {
                    f"gradle_log:{command.stage_id}": {
                        "path": f"logs/{self.name}.log",
                        "sha256": sha256(log_path.read_bytes()).hexdigest(),
                        "media_type": "text/plain",
                    }
                }
            from .dependency_build import gradle_failure_kind
            if gates_disabled:
                error_code = gradle_failure_kind(completed.stdout)
                return _unverified_result(command, status="failed", outputs=outputs,
                    diagnostics=[f"{self.name} exited with {completed.returncode}"],
                    detail=f"{self.name} executed; failure retained as an observation",
                    error_code=error_code)
            return _result(command, "failed", outputs=outputs, detail=f"{self.name} failed",
                           error_code=gradle_failure_kind(completed.stdout))
        log_path = root / "logs" / f"{self.name}.log"
        outputs: dict[str, Any] = {
            "log": f"logs/{self.name}.log",
            **_wrapper_cache_diagnostic(command, wrapper_cache_info),
            **scope_outputs,
        }
        if command.options.get('workflow_version', 0) >= 18:
            outputs['process_executed'] = True
        if log_path.is_file():
            outputs["artifact_refs"] = {
                f"gradle_log:{command.stage_id}": {
                    "path": f"logs/{self.name}.log",
                    "sha256": sha256(log_path.read_bytes()).hexdigest(),
                    "media_type": "text/plain",
                }
            }
        return _result(command, "completed", outputs=outputs, detail=f"{self.name} passed")


def _file_log_outputs(root, relative):
    """Keep a partial process log addressable when execution fails."""
    path = root / relative
    if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root.resolve()):
        return {'log': relative, 'artifact_refs': {'execution_log': {
            'path': relative, 'sha256': file_digest(path), 'media_type': 'text/plain'}}}
    return {}


def _target_package_receipt(command: OperationInput) -> tuple[dict[str, Any], dict[str, Any]]:
    """Seal regular target JARs and their committed build candidate."""
    root = _run_root(command)
    worktree = project_path(root, "worktree")
    libs = worktree / "build" / "libs"
    artifacts = []
    try:
        candidates = sorted(libs.glob("*.jar"))
    except OSError:
        candidates = []
    for path in candidates:
        try:
            if (path.is_symlink() or not path.is_file()
                    or path.resolve(strict=True) != path.absolute()
                    or not path.resolve(strict=True).is_relative_to(worktree.resolve(strict=True))):
                continue
            size = path.stat().st_size
            if size <= 0:
                continue
            artifacts.append({"path": project_relative(root, path).as_posix(),
                              "sha256": file_digest(path), "size": size})
        except (OSError, ValueError):
            continue
    target_commit = None
    target_clean = None
    preserved_reviewer_reports = []
    if command.options.get("workflow_version", 0) >= 28 and compile_package_scope(command):
        head = _exec(["git", "rev-parse", "HEAD"], cwd=worktree,
                     log=root / "logs" / "target-build-git-head.log",
                     timeout=_remaining_timeout(command, 60))
        status = _exec(["git", "status", "--porcelain", "--untracked-files=all"],
                       cwd=worktree, log=root / "logs" / "target-build-git-status.log",
                       timeout=_remaining_timeout(command, 60))
        target_commit = head.stdout.strip() if head.returncode == 0 else None
        if not isinstance(target_commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", target_commit):
            target_commit = None
        dirty_rows = status.stdout.splitlines()
        # During a reviewer-requested cleanup revision, the reviewer continues
        # to own its untracked report while the fresh target build runs. Only
        # that exact host-declared report may be absent from the source-tree
        # cleanliness check; tracked edits and all other files remain dirty.
        report_path = worktree / ".modport" / "code-review.json"
        if (command.options.get("workflow_version", 0) >= 30
                and isinstance(command.payload.get("reviewer_rework"), Mapping)
                and command.payload.get("reviewer_report_paths") == [".modport/code-review.json"]
                and not report_path.is_symlink() and report_path.is_file()
                and report_path.resolve() == report_path.absolute()
                and report_path.resolve().is_relative_to(worktree.resolve())):
            marker = "?? .modport/code-review.json"
            if marker in dirty_rows:
                dirty_rows.remove(marker)
                preserved_reviewer_reports.append(".modport/code-review.json")
        target_clean = status.returncode == 0 and not dirty_rows
    receipt = {
        "schema_version": 1,
        "status": ("passed" if artifacts and
                   (target_clean is None or target_commit is not None and target_clean)
                   else "failed"),
        "run_id": command.run_id,
        "build_execution_id": command.command_id,
        "artifacts": artifacts,
    }
    if target_clean is not None:
        receipt.update(target_commit=target_commit, target_clean=target_clean)
    if preserved_reviewer_reports:
        receipt["preserved_reviewer_reports"] = preserved_reviewer_reports
    receipt_path = (root / "artifacts" / "executions" / command.command_id
                    / "target-package-receipt.json")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(receipt_path, receipt)
    ref = {"path": project_relative(root, receipt_path).as_posix(),
           "sha256": file_digest(receipt_path), "media_type": "application/json"}
    return receipt, ref


def _agent_log_outputs(root, log_relative):
    outputs = {"log": log_relative, **_file_log_outputs(root, log_relative)}
    prompt = root / (log_relative + '.stdin.txt')
    if prompt.is_file() and not prompt.is_symlink():
        outputs.setdefault('artifact_refs', {})['agent_prompt'] = {
            'path': project_relative(root, prompt).as_posix(),
            'sha256': sha256(prompt.read_bytes()).hexdigest(), 'media_type': 'text/plain'}
    return outputs


def _prior_characterization_receipts(
    command: OperationInput, root: Path, *,
    additional: Mapping[str, Any] | None = None,
    limit: int = 500,
) -> tuple[dict[str, dict[str, str]], bool]:
    """Host-select bounded, referenced receipts for exact invocation-time checks."""
    refs = command.artifact_refs
    candidates: dict[str, tuple[float, dict[str, str]]] = {}
    total_bytes = 0
    incomplete = False
    max_file_bytes = 4 * 1024 * 1024
    max_total_bytes = 32 * 1024 * 1024
    if not isinstance(refs, Mapping):
        return {}, False
    from .evidence import verified_path
    input_rows = [
        (str(alias), refs.get(alias)) for alias in sorted(refs)
        if 'characterization_verification:' in str(alias)
        and not str(alias).endswith('characterization_verification_latest')
    ]
    if isinstance(additional, Mapping):
        input_rows.extend((f'prior_case_receipts:{test_id}', reference)
                          for test_id, reference in sorted(additional.items(), key=lambda item: str(item[0])))
    selected = 0
    for alias, reference in input_rows:
        if not isinstance(reference, Mapping):
            continue
        selected += 1
        if selected > limit:
            incomplete = True
            break
        try:
            path = verified_path(root, reference)
            size = path.stat().st_size
            if path.is_symlink() or size > max_file_bytes:
                incomplete = True
                continue
            total_bytes += size
            if total_bytes > max_total_bytes:
                incomplete = True
                break
            raw = path.read_bytes()
            if not isinstance(reference.get('sha256'), str) or sha256(raw).hexdigest() != reference['sha256']:
                continue
            receipt = json.loads(raw.decode('utf-8'))
            if not isinstance(receipt, Mapping):
                continue
            recorded_digest = receipt.get('receipt_sha256')
            unsigned = dict(receipt)
            unsigned.pop('receipt_sha256', None)
            actual_digest = sha256(json.dumps(
                unsigned, ensure_ascii=False, sort_keys=True, separators=(',', ':')
            ).encode('utf-8')).hexdigest()
            if (receipt.get('receipt_type') != 'modport.characterization_verification.v1'
                    or not isinstance(recorded_digest, str)
                    or recorded_digest != actual_digest
                    or receipt.get('scope') != 'selected'
                    or not isinstance(receipt.get('test_ids'), list)
                    or len(receipt['test_ids']) != 1
                    or not isinstance(receipt['test_ids'][0], str)):
                continue
            test_id = receipt['test_ids'][0]
            before = receipt.get('candidate_before')
            after = receipt.get('candidate_after')
            results = receipt.get('test_results')
            bindings = receipt.get('case_bindings')
            is_passing = (
                receipt.get('outcome') == 'passed'
                and receipt.get('category') == 'none'
                and receipt.get('case_execution_evidence') is True
                and receipt.get('candidate_unchanged') is True
                and isinstance(before, Mapping) and isinstance(after, Mapping)
                and before.get('candidate_id') == after.get('candidate_id')
                and isinstance(results, list) and len(results) == 1
                and isinstance(results[0], Mapping)
                and results[0].get('test_id') == test_id
                and results[0].get('outcome') == 'passed'
                and isinstance(bindings, list) and len(bindings) == 1
                and isinstance(bindings[0], Mapping)
                and isinstance(bindings[0].get('case_identity'), str)
            )
            if not is_passing:
                continue
            finished = receipt.get('finished_at')
            rank = float(finished) if isinstance(finished, (int, float)) else 0.0
            current = candidates.get(test_id)
            if current is None or rank > current[0]:
                candidates[test_id] = (rank, {
                    'path': project_relative(root, path).as_posix(),
                    'sha256': str(reference['sha256']),
                })
        except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError):
            continue
    return {test_id: ref for test_id, (_, ref) in candidates.items()}, incomplete


@dataclass
class CodexStageHandler:
    prompt: str
    baseline: bool = False
    read_only: bool = False
    required_paths: tuple[str, ...] = ()
    native_goal: Mapping[str, Any] | None = None
    goal_validator: Callable[[], dict[str, Any]] | None = None
    reuse_recovery_prompt: bool = True

    def _required_paths(self, command: OperationInput) -> tuple[str, ...]:
        paths = self.required_paths
        if (command.options.get("workflow_version", 0) >= 31 and self.baseline
                and command.stage_id in {"contract_draft", "contract_revise"}):
            paths = tuple(dict.fromkeys((*paths, ".modport/test-matrix.json")))
        return paths

    def __call__(self, command: OperationInput) -> OperationResult:
        """Retain previous required outputs until this assignment is accepted."""
        outcome = None
        rejected_refs = {}
        try:
            outcome = self._execute(command)
        finally:
            if (self._required_paths(command) and not business_gates_disabled(command)
                    and (outcome is None or outcome.status != "completed")):
                root = _run_root(command)
                previous = root / "artifacts" / "executions" / command.command_id / "previous-outputs"
                started = previous / ".replacement-started"
                if started.is_file():
                    from .kernel_runtime import operation_workspace
                    workspace = operation_workspace(root, command) or project_path(root, "baseline" if self.baseline else "worktree")
                    for relative in self._required_paths(command):
                        target = workspace / relative
                        if not target.parent.resolve().is_relative_to(workspace.resolve()):
                            continue  # Never follow an agent-created path outside its workspace.
                        if target.is_symlink():
                            target.unlink()
                        if target.is_dir():
                            # A malformed directory output is still a business
                            # rejection. Move it aside before restoring a file.
                            import tempfile
                            rejected_parent = previous.parent / 'rejected-outputs'
                            rejected_parent.mkdir(parents=True, exist_ok=True)
                            retained = Path(tempfile.mkdtemp(prefix='directory-', dir=rejected_parent))
                            target.rename(retained / 'contents')
                            description = retained / 'shape.json'
                            _write_json(description, {'required_path': relative, 'actual_type': 'directory',
                                                      'retained_path': project_relative(root, retained / 'contents').as_posix()})
                            rejected_refs['rejected_output:' + relative] = {
                                'path': project_relative(root, description).as_posix(),
                                'sha256': file_digest(description), 'media_type': 'application/json'}
                        if target.is_file():
                            rejected = previous.parent / "rejected-outputs" / relative
                            rejected.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copyfile(target, rejected)
                            rejected_refs['rejected_output:' + relative] = {
                                'path': project_relative(root, rejected).as_posix(),
                                'sha256': file_digest(rejected), 'media_type': 'application/octet-stream'}
                        backup = previous / relative
                        if backup.is_file() and not backup.is_symlink():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            temporary = target.with_name(target.name + ".modport-restore")
                            if temporary.is_symlink():
                                temporary.unlink()
                            shutil.copy2(backup, temporary)
                            temporary.replace(target)
                        else:
                            target.unlink(missing_ok=True)
        if rejected_refs:
            outcome = replace(outcome, outputs={**outcome.outputs, 'artifact_refs': {
                **outcome.outputs.get('artifact_refs', {}), **rejected_refs}})
        return outcome

    @prepare_inputs
    def _execute(self, command: OperationInput) -> OperationResult:
        root = _run_root(command)
        from .wiki_knowledge import prepare_for_command
        wiki_refs = prepare_for_command(command)
        gates_disabled = business_gates_disabled(command)
        workflow_version = command.options.get("workflow_version", 0)
        business_diagnostics: list[str] = []
        try:
            rubric = _acceptance_rubric_for(command, root)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if not gates_disabled:
                return _result(command, "blocked", detail=f"acceptance rubric is invalid: {exc}", error_code="acceptance_rubric_invalid")
            from .rubric import acceptance_rubric
            rubric = acceptance_rubric()
            business_diagnostics.append(f"acceptance rubric unavailable: {exc}")
        request = _request(command)
        assignment = int(command.options.get("agent_assignment", command.attempt))
        from .kernel_runtime import operation_workspace
        worktree = operation_workspace(root, command) or project_path(root, "baseline" if self.baseline else "worktree")
        frozen = root / "artifacts" / "functional-contract.lock.json"
        if (frozen.exists() or gates_disabled
                and isinstance(command.artifact_refs.get("functional_contract_lock"), Mapping)):
            try:
                _verify_locked_artifacts(
                    root,
                    command.payload.get("locked_artifacts"),
                    rubric=rubric,
                    contract_ref=(command.artifact_refs.get("functional_contract_lock")
                                  if gates_disabled else None),
                )
            except (TypeError, ValueError, KeyError) as exc:
                if not gates_disabled:
                    return _result(command, "blocked", detail=f"locked artifacts are invalid: {exc}", error_code="locked_artifact_invalid")
                business_diagnostics.append(f"locked artifact observation before execution: {exc}")
        output = root / "logs" / f"agent-{command.task_id}-{assignment}.txt"
        log_relative = f"logs/agent-{command.task_id}-{assignment}.log"
        try:
            rule_refs = {key: _read_artifact_ref(root, command, key)[1]
                         for key in ("agent_rules", "evidence_protocol")}
        except (OSError, ValueError, TypeError) as exc:
            return _result(command, "blocked", detail=str(exc), error_code="agent_rules_invalid")
        for relative in self._required_paths(command):
            target = worktree / relative
            if target.is_symlink() or not target.resolve().is_relative_to(worktree.resolve()):
                return _result(command, "blocked", detail="unsafe required output path", error_code="agent_output_invalid")
        if self.native_goal is not None and (
            not isinstance(self.native_goal.get("objective"), str)
            or not self.native_goal["objective"].strip()
            or not callable(self.goal_validator)
        ):
            return _result(command, "blocked", detail="native goal requires an objective and host validator",
                           error_code="native_goal_invalid")
        compression_path = root / "artifacts" / "executions" / command.command_id / "prompt-compression.json"
        compression_dir = compression_path.parent / "prompt-compression"
        source_prompt_path = compression_dir / "source.txt"
        compressed_prompt_path = compression_dir / "compressed.txt"
        active_model, active_effort = _agent_model_policy(command)
        from .report_dialogue import (dialogue_enabled, prepare_dialogue, phase_command,
                                      materialize_report, dialogue_artifacts)
        from .agent_dialogue import AgentDialogueError
        dialogue = (prepare_dialogue(command, root, self.prompt, self._required_paths(command))
                    if dialogue_enabled(command) else None)
        prompt_cache = (_recovery_prompt_cache_paths(root, command)
                        if self.reuse_recovery_prompt and dialogue is None else None)
        if prompt_cache is None:
            full_prompt = build_prompt(dialogue['execution_task'], phase_command(command, 'execute'),
                                       root, rule_refs, rubric) if dialogue else build_prompt(
                                           self.prompt, command, root, rule_refs, rubric)
        else:
            full_prompt = prompt_cache[0].read_text(encoding="utf-8")
        compression_outputs = {
            "prompt_compression": str(project_relative(root, compression_path)),
        }
        last_message_available = False
        native_goal_metadata: Mapping[str, Any] = {}

        def capture_native_goal_metadata(value: Any) -> None:
            nonlocal native_goal_metadata
            if not isinstance(value, Mapping):
                if self.native_goal is None:
                    return
                value = {}
            native_goal_metadata = value
            compression_outputs['native_goal'] = dict(value)
            refs = compression_outputs.setdefault('artifact_refs', {})
            state_relative = value.get('state_path')
            if not isinstance(state_relative, str) and self.native_goal is not None:
                identity = sha256(str(command.command_id).encode()).hexdigest()[:24]
                state_relative = f'artifacts/native-goals/{identity}/state.json'
            if isinstance(state_relative, str):
                state_path = root / state_relative
                if (state_path.is_file() and not state_path.is_symlink()
                        and state_path.resolve() == state_path.absolute()
                        and state_path.resolve().is_relative_to(root.resolve())):
                    refs['native_goal_state'] = {
                        'path': project_relative(root, state_path).as_posix(),
                        'sha256': file_digest(state_path), 'media_type': 'application/json',
                    }
            result_ref = value.get('model_result_ref')
            if not isinstance(result_ref, Mapping) and self.native_goal is not None:
                identity = sha256(str(command.command_id).encode()).hexdigest()[:24]
                model_result_path = root / 'artifacts' / 'native-goals' / identity / 'model-result.json'
                if model_result_path.is_file() and not model_result_path.is_symlink():
                    result_ref = {
                        'path': project_relative(root, model_result_path).as_posix(),
                        'sha256': file_digest(model_result_path),
                    }
            if isinstance(result_ref, Mapping):
                try:
                    from .evidence import verified_path
                    result_path = verified_path(root, result_ref)
                    if result_ref.get('sha256') != file_digest(result_path):
                        return
                    refs['native_goal_model_result'] = {
                        'path': project_relative(root, result_path).as_posix(),
                        'sha256': file_digest(result_path), 'media_type': 'application/json',
                    }
                except (OSError, TypeError, ValueError, KeyError):
                    pass
            events_relative = value.get('events_path')
            if not isinstance(events_relative, str) and self.native_goal is not None:
                identity = sha256(str(command.command_id).encode()).hexdigest()[:24]
                events_relative = f'artifacts/native-goals/{identity}/events.jsonl'
            if isinstance(events_relative, str):
                events_path = root / events_relative
                if (events_path.is_file() and not events_path.is_symlink()
                        and events_path.resolve() == events_path.absolute()
                        and events_path.resolve().is_relative_to(root.resolve())):
                    refs['native_goal_events'] = {
                        'path': project_relative(root, events_path).as_posix(),
                        'sha256': file_digest(events_path), 'media_type': 'application/x-ndjson',
                    }

        def _with_compression(outputs: Mapping[str, Any]) -> dict[str, Any]:
            merged = dict(outputs)
            merged.update({key: value for key, value in compression_outputs.items()
                           if key != "artifact_refs"})
            refs = dict(merged.get("artifact_refs", {}))
            refs.update(compression_outputs.get("artifact_refs", {}))
            shell_dir = root / "artifacts" / "executions" / command.command_id / "opencode-shell"
            if (shell_dir.is_dir() and not shell_dir.is_symlink()
                    and shell_dir.resolve().is_relative_to(root.resolve())):
                for artifact in sorted(shell_dir.glob("*.json")):
                    if (artifact.name in {"session.json", "latest-receipt.json"} or artifact.is_symlink()
                            or not artifact.is_file()
                            or not artifact.resolve().is_relative_to(shell_dir.resolve())):
                        continue
                    refs["project_command:" + artifact.stem] = {
                        "path": project_relative(root, artifact).as_posix(),
                        "sha256": file_digest(artifact),
                        "media_type": "application/json",
                    }
            characterization_dir = (root / 'artifacts' / 'executions'
                                    / command.command_id / 'opencode-characterization')
            if (characterization_dir.is_dir() and not characterization_dir.is_symlink()
                    and characterization_dir.resolve().is_relative_to(root.resolve())):
                for artifact in sorted(characterization_dir.glob('*.json')):
                    if (artifact.name in {'session.json', 'latest-receipt.json', 'contract-input.json'}
                            or artifact.is_symlink() or not artifact.is_file()
                            or not artifact.resolve().is_relative_to(characterization_dir.resolve())):
                        continue
                    try:
                        receipt = json.loads(artifact.read_text(encoding='utf-8'))
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        continue
                    if (not isinstance(receipt, Mapping)
                            or receipt.get('receipt_type') != 'modport.characterization_verification.v1'):
                        continue
                    refs['characterization_verification:' + artifact.stem] = {
                        'path': project_relative(root, artifact).as_posix(),
                        'sha256': file_digest(artifact), 'media_type': 'application/json',
                    }
                latest_receipt = characterization_dir / 'latest-receipt.json'
                if latest_receipt.is_file() and not latest_receipt.is_symlink():
                    refs['characterization_verification_latest'] = {
                        'path': project_relative(root, latest_receipt).as_posix(),
                        'sha256': file_digest(latest_receipt), 'media_type': 'application/json',
                    }
            if dialogue is not None:
                refs.update(dialogue_artifacts(root, dialogue))
            author_contract = root / "artifacts" / "executions" / command.command_id / "characterization-author-contract.json"
            if author_contract.is_file() and not author_contract.is_symlink():
                refs["characterization_author_contract"] = {
                    "path": project_relative(root, author_contract).as_posix(),
                    "sha256": file_digest(author_contract), "media_type": "application/json"}
            if last_message_available:
                merged['last_message'] = project_relative(root, output).as_posix()
                refs['agent_last_message'] = {
                    'path': project_relative(root, output).as_posix(),
                    'sha256': file_digest(output), 'media_type': 'text/plain'}
            if refs:
                merged["artifact_refs"] = refs
            return merged

        try:
            compression_dir.mkdir(parents=True, exist_ok=True)
            # Preserve the complete prompt before any model-written summary.
            # This is the host-owned source of truth when compression fails.
            source_prompt_path.write_text(full_prompt, encoding="utf-8")
            compression_outputs["prompt_compression_source"] = str(project_relative(root, source_prompt_path))
            compression_outputs["artifact_refs"] = {
                "prompt_compression_source": {
                    "path": project_relative(root, source_prompt_path).as_posix(),
                    "sha256": sha256(source_prompt_path.read_bytes()).hexdigest(),
                    "media_type": "text/plain",
                }
            }
        except OSError as exc:
            return _result(
                command,
                "blocked",
                outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                detail=f"prompt compression input could not be archived: {exc}",
                error_code="prompt_compression_audit_failed",
            )
        try:
            cached = None
            reused_from = None
            if prompt_cache is not None:
                cached = _reuse_cached_compressed_prompt(
                    prompt_cache[1],
                    prompt_cache[0],
                    prompt_cache[2],
                    full_prompt,
                    active_model,
                    root,
                )
                if cached is None:
                    raise PromptCompressionError("recovery prompt cache failed validation")
                reused_from = prompt_cache[3]
            if cached is None:
                cached = _reuse_cached_compressed_prompt(
                    compression_path,
                    source_prompt_path,
                    compressed_prompt_path,
                    full_prompt,
                    active_model,
                    root,
                )
            if cached is None:
                summary_model, summary_effort = agent_model_policy(
                    command.options.get("workflow_version"), "prompt_summary", command.options.get("model_policy"))
                compressed = PromptCompressor.from_environment(
                    summary_model=summary_model, summary_reasoning=summary_effort).compress(
                    full_prompt,
                    model=active_model,
                    command=command,
                    root=root,
                    worktree=worktree,
                )
                compressed_prompt_path.write_text(compressed.text, encoding="utf-8")
                metadata = dict(compressed.metadata)
                metadata.update({
                    "source_prompt_path": str(project_relative(root, source_prompt_path)),
                    "compressed_prompt_path": str(project_relative(root, compressed_prompt_path)),
                })
                _write_json(compression_path, metadata)
                compressed_text = compressed.text
            else:
                compressed_text, metadata = cached
                compressed_prompt_path.write_text(compressed_text, encoding="utf-8")
                metadata = dict(metadata)
                metadata.update({
                    "source_prompt_path": str(project_relative(root, source_prompt_path)),
                    "compressed_prompt_path": str(project_relative(root, compressed_prompt_path)),
                })
                if reused_from is not None:
                    metadata["reused_from_execution_id"] = reused_from
                _write_json(compression_path, metadata)
                if reused_from is not None:
                    compression_outputs["prompt_compression_reused_from"] = reused_from
            compression_outputs["prompt_compression_output"] = str(project_relative(root, compressed_prompt_path))
            compression_outputs["artifact_refs"] = {
                "prompt_compression_source": {
                    "path": project_relative(root, source_prompt_path).as_posix(),
                    "sha256": sha256(source_prompt_path.read_bytes()).hexdigest(),
                    "media_type": "text/plain",
                },
                "prompt_compression": {
                    "path": project_relative(root, compression_path).as_posix(),
                    "sha256": sha256(compression_path.read_bytes()).hexdigest(),
                    "media_type": "application/json",
                },
                "prompt_compression_output": {
                    "path": project_relative(root, compressed_prompt_path).as_posix(),
                    "sha256": sha256(compressed_prompt_path.read_bytes()).hexdigest(),
                    "media_type": "text/plain",
                },
            }
            if (dialogue is not None and command.options.get('workflow_version', 0) >= 25
                    and metadata.get('compressed')):
                planning_history = compressed_history_for_plan(full_prompt, compressed_text)
                planning_history_path = compression_dir / 'plan-history.txt'
                if (planning_history_path.exists()
                        and planning_history_path.read_text(encoding='utf-8') != planning_history):
                    raise PromptCompressionError('saved planning history differs from compressed prompt')
                planning_history_path.write_text(planning_history, encoding='utf-8')
                compression_outputs['artifact_refs']['prompt_compression_plan_history'] = {
                    'path': project_relative(root, planning_history_path).as_posix(),
                    'sha256': sha256(planning_history_path.read_bytes()).hexdigest(),
                    'media_type': 'text/plain',
                }
        except PromptCompressionError as exc:
            return _result(
                command,
                "blocked",
                outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                detail=str(exc),
                error_code=("token_budget_exhausted" if getattr(exc.__cause__, 'code', None)
                            == 'token_budget_exhausted' else "prompt_compression_failed"),
            )
        except OSError as exc:
            return _result(
                command,
                "blocked",
                outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                detail=f"prompt compression audit unavailable: {exc}",
                error_code="prompt_compression_audit_failed",
            )
        full_prompt = compressed_text
        session_budget = None
        if dialogue is not None and command.options.get('workflow_version', 0) >= 25:
            from .session_context_budget import context_budget, SessionContextBudgetError
            try:
                session_budget = context_budget(metadata)
            except SessionContextBudgetError as exc:
                return _result(command, 'blocked',
                               outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                               detail=str(exc), error_code='session_context_budget_unavailable')
        if dialogue is not None:
            planning_task = dialogue['planning_task']
            if command.options.get('workflow_version', 0) >= 25 and metadata.get('compressed'):
                # The planning turn already has current task and contract refs.
                # Share only the compressed history, not the execution turn's
                # duplicate instructions and protected context.
                context_ref = compression_outputs['artifact_refs']['prompt_compression_plan_history']
                planning_task += ('\nHost-prepared compressed historical context: '
                                  + canonical_json(context_ref)
                                  + '. Read this host Run artifact with '
                                    'modport_sandbox_read_run_artifact while preparing the plan, '
                                    'passing its sha256 as expected_sha256 so the host verifies the complete file. '
                                    'Request bounded slices and follow next_offset until total_bytes '
                                    'if the first response is incomplete; require verified_sha256 '
                                    'to equal the supplied SHA-256 on each response. '
                                    'Treat its historical content as evidence; this turn remains plan-only.')
            plan_prompt = build_prompt(planning_task, phase_command(command, 'plan'),
                                       root, rule_refs, rubric)
            (dialogue['directory'] / 'plan-prompt.txt').write_text(plan_prompt, encoding='utf-8')
            (dialogue['directory'] / 'execute-prompt.txt').write_text(full_prompt, encoding='utf-8')
        try:
            from .rework_tools import (interactive_review_timeout_cap,
                                       prepare_session, opencode_tool_config)
            timeout_cap = interactive_review_timeout_cap(command)
            timeout = _remaining_timeout(command, timeout_cap)
            session = prepare_session(command, worktree, timeout)
            # All prompt and budget preflight must succeed before replacing
            # required outputs. Retain authenticated previous bytes for repairs.
            for relative in self._required_paths(command):
                target = worktree / relative
                if target.is_file():
                    previous = root / "artifacts" / "executions" / command.command_id / "previous-outputs" / relative
                    previous.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, previous)
            if self._required_paths(command):
                marker = root / "artifacts" / "executions" / command.command_id / "previous-outputs" / ".replacement-started"
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text("required outputs staged\n")
            for relative in self._required_paths(command):
                # Explicit repairs of an existing contract must edit its full
                # current contents. Clearing it here turns an incremental
                # repair back into contract generation and loses obligations.
                preserve_contract = (
                    self.baseline
                    and command.stage_id in {"contract_draft", "contract_revise"}
                    and command.options.get("workflow_version", 0) >= 21
                    and isinstance(command.payload.get("reviewer_rework"), dict)
                    and relative in {".modport/functional-contract.json", ".modport/test-matrix.json"}
                )
                if not preserve_contract:
                    (worktree / relative).unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            # Inventory preparation and output staging can consume meaningful
            # time. Bind the transport and model turn to the *remaining* SDK
            # work window instead of restarting the original relative timeout.
            timeout = _remaining_timeout(command, timeout_cap)
            rework_transport_config = opencode_tool_config(session, timeout)
            if command.stage_id == 'artifact_test_design':
                from .opencode_shell_mcp import prepare_artifact_compile_tool
                rework_transport_config.update(prepare_artifact_compile_tool(command, worktree, timeout))
            if (29 <= command.options.get('workflow_version', 0) < 34
                    and command.stage_id in {'contract_draft', 'contract_revise',
                                             'implementation', 'target_revise'}
                    and (not compile_package_scope(command)
                         or command.options.get('workflow_version', 0) >= 31
                         and command.stage_id in {'contract_draft', 'contract_revise'})):
                try:
                    from .opencode_shell_mcp import prepare_characterization_tool
                    from .harness_wiring import characterization_init_scripts
                    source_record = json.loads(
                        (root / 'artifacts' / 'source.json').read_text(encoding='utf-8'))
                    source_commit = str(source_record.get('source_commit', ''))
                    authoring_contract = command.stage_id in {'contract_draft', 'contract_revise'}
                    if authoring_contract:
                        contract_document: dict[str, Any] = {}
                        contract_path = project_relative(root, worktree) / '.modport' / 'functional-contract.json'
                        contract_digest = None
                        test_cases: dict[str, dict[str, Any]] = {}
                        full_tasks: list[str] = []
                    else:
                        locked_ref = command.artifact_refs.get('functional_contract_lock')
                        locked_path = (_resolve_artifact_ref(root, locked_ref, 'functional_contract_lock')[0]
                                       if isinstance(locked_ref, Mapping)
                                       else root / 'artifacts' / 'functional-contract.lock.json')
                        locked = json.loads(locked_path.read_text(encoding='utf-8'))
                        contract_document = {
                            **locked['contract'],
                            'test_evidence': locked['test_evidence'],
                            'baseline_gradle_tasks': locked['baseline_gradle_tasks'],
                        }
                        contract_input = (root / 'artifacts' / 'executions' /
                                          command.command_id / 'opencode-characterization' /
                                          'contract-input.json')
                        _write_json(contract_input, contract_document)
                        contract_path = project_relative(root, contract_input)
                        contract_digest = file_digest(contract_input)
                        assertion_ids_by_test: dict[str, list[str]] = {}
                        for behavior in contract_document.get('behaviors', contract_document.get('entries', ())):
                            if not isinstance(behavior, Mapping):
                                continue
                            for assertion in behavior.get('assertion_contracts', ()):
                                if not isinstance(assertion, Mapping):
                                    continue
                                for test_id in assertion.get('test_ids', ()):
                                    assertion_ids_by_test.setdefault(str(test_id), []).append(
                                        str(assertion.get('assertion_id', '')))
                        test_cases = {
                            str(test_id): {
                                'executor': declaration.get('executor', 'junit'),
                                'assertion_ids': sorted(set(assertion_ids_by_test.get(str(test_id), ()))),
                                'test_source_files': declaration.get('test_source_files', ()),
                                'result_identity': declaration.get('result_identity'),
                            }
                            for test_id, declaration in contract_document.get('test_evidence', {}).items()
                            if isinstance(declaration, Mapping)
                        }
                        full_tasks = list(contract_document.get('baseline_gradle_tasks', ()))
                    wiring_directory = root / 'artifacts' / 'harness-wiring'
                    init_scripts = []
                    for init_script in characterization_init_scripts(
                            worktree, workflow_version=29,
                            supplement_directory=wiring_directory):
                        host_owned = init_script.parent == wiring_directory
                        base = wiring_directory if host_owned else worktree
                        init_scripts.append({
                            'path': init_script.relative_to(base).as_posix(),
                            'sha256': file_digest(init_script),
                            'origin': 'wiring' if host_owned else 'workspace',
                        })
                    if not authoring_contract and workflow_version >= 31:
                        from .test_selection_execution import build_selected_test_execution
                        selected = build_selected_test_execution(contract_document, list(test_cases))
                        full_tasks = list(selected.gradle_tasks)
                    reviewed = False
                    if not authoring_contract:
                        review_data = locked.get('review', {})
                        reviewed = not _assertion_review_diagnostics(contract_document, review_data)
                    supplied_prior = command.artifact_refs.get('prior_case_receipts', {})
                    prior_receipts, prior_receipts_incomplete = _prior_characterization_receipts(
                        command, root,
                        additional=supplied_prior if isinstance(supplied_prior, Mapping) else None,
                    )
                    if prior_receipts_incomplete:
                        compression_outputs['prior_characterization_receipts_incomplete'] = (
                            'host receipt selection reached its 500-reference or 32 MiB input cap; '
                            'unselected prior cases will be re-executed or remain unverified'
                        )
                    characterization_config = prepare_characterization_tool(
                        root, worktree, str(command.command_id),
                        source_commit=source_commit,
                        contract_sha256=contract_digest,
                        contract_path=contract_path.as_posix(),
                        test_cases=test_cases,
                        init_scripts=init_scripts,
                        full_tasks=full_tasks,
                        timeout=timeout,
                        assertions_reviewed=reviewed,
                        prior_case_receipts=prior_receipts,
                        dynamic_contract=authoring_contract,
                        read_only=self.read_only,
                    )
                    rework_transport_config.update(characterization_config)
                    compression_outputs['characterization_tool'] = 'modport_characterization.verify_characterization'
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    compression_outputs['characterization_tool_setup_error'] = (
                        f'{type(exc).__name__}: {str(exc)[:1000]}')
            if self.native_goal is None:
                from .opencode_agent import run_agent
                with model_work('coding_model'):
                    completed = run_agent(prompt=full_prompt, cwd=worktree,
                        log=root / log_relative, model=active_model, variant=active_effort,
                        timeout=_remaining_timeout(command, timeout_cap),
                        read_only=self.read_only, mcp=rework_transport_config,
                        allow_project_commands=not bool(command.payload.get('diagnostic_repair_manifest')),
                        model_policy=command.options.get('model_policy'),
                        run_root=root, command_id=command.command_id,
                        planning_prompt=plan_prompt if dialogue is not None else None,
                        plan_path=dialogue['plan_path'] if dialogue is not None else None,
                        schema_path=dialogue['schema_path'] if dialogue is not None else None,
                        session_context_budget=session_budget)
                    if dialogue is not None:
                        metadata = completed.dialogue_metadata
                        compression_outputs['agent_dialogue'] = metadata
                        compression_outputs['artifact_refs'].update(dialogue_artifacts(root, dialogue, metadata))
            else:
                from .goal_runtime import run_goal
                goal_command = replace(command, options={**command.options, "goal_read_only": self.read_only})
                log = root / log_relative
                log.parent.mkdir(parents=True, exist_ok=True)
                Path(str(log) + ".stdin.txt").write_text(full_prompt, encoding="utf-8")
                with model_work('native_coder_goal'):
                    dialogue_args = ({'planning_prompt': plan_prompt, 'plan_path': dialogue['plan_path'],
                                      'session_context_budget': session_budget,
                                      'on_report': lambda text: business_diagnostics.extend(
                                          materialize_report(dialogue, worktree, text))}
                                     if dialogue is not None else {})
                    native_transport_args = ({'transport_args': rework_transport_config}
                                             if rework_transport_config else {})
                    completed = run_goal(command=goal_command, root=root, worktree=worktree,
                        prompt=full_prompt, objective=self.native_goal["objective"],
                        validate=self.goal_validator,
                        timeout=_remaining_timeout(command, timeout_cap), **dialogue_args,
                        **native_transport_args)
                log.write_text(completed.stdout + "\n", encoding="utf-8")
                compression_outputs["native_goal"] = dict(completed.metadata)
                capture_native_goal_metadata(completed.metadata)
                if dialogue is not None:
                    metadata = {key: completed.metadata[key] for key in
                        ('thread_id', 'dialogue_phase', 'planning_turns', 'turns', 'status', 'deadline_epoch')
                        if key in completed.metadata}
                    compression_outputs['agent_dialogue'] = metadata
                    compression_outputs['artifact_refs'].update(dialogue_artifacts(root, dialogue, metadata))
        except AgentDialogueError as exc:
            metadata = getattr(exc, 'metadata', None)
            capture_native_goal_metadata(metadata)
            if dialogue is not None:
                compression_outputs['artifact_refs'].update(dialogue_artifacts(root, dialogue, metadata))
            return _result(command, 'failed', outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                           detail=str(exc), error_code='agent_dialogue_failed')
        except TimeoutError as exc:
            capture_native_goal_metadata(getattr(exc, 'metadata', None))
            if dialogue is not None:
                compression_outputs['artifact_refs'].update(dialogue_artifacts(root, dialogue, getattr(exc, 'metadata', None)))
            detail = ("reviewer rework assignment deadline exhausted"
                      if command.options.get('workflow_version', 0) >= 26
                      and isinstance(command.payload.get('reviewer_rework'), dict)
                      else "run wall-clock budget exhausted")
            return _result(command, "failed", outputs=_with_compression(_agent_log_outputs(root, log_relative)), detail=detail, error_code="budget_exhausted")
        except TokenBudgetExceeded as exc:
            capture_native_goal_metadata(getattr(exc, 'metadata', None))
            return _result(command, "failed", outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                           detail=str(exc), error_code=exc.code)
        except subprocess.TimeoutExpired as exc:
            capture_native_goal_metadata(getattr(exc, 'metadata', None))
            if dialogue is not None:
                compression_outputs['artifact_refs'].update(dialogue_artifacts(root, dialogue, getattr(exc, 'metadata', None)))
            return _result(command, "failed", outputs=_with_compression(_agent_log_outputs(root, log_relative)), detail="coding agent timed out", error_code="agent_timeout")
        except OSError as exc:
            capture_native_goal_metadata(getattr(exc, 'metadata', None))
            if dialogue is not None:
                compression_outputs['artifact_refs'].update(dialogue_artifacts(root, dialogue, getattr(exc, 'metadata', None)))
            return _result(command, "failed", outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                           detail=f"agent launch failed (errno={exc.errno}); inspect process audit",
                           error_code="agent_launch_failed")
        except OpenCodeCleanupError as exc:
            capture_native_goal_metadata(getattr(exc, 'metadata', None))
            from .evidence import atomic_json
            diagnostic = {key: exc.cleanup_diagnostic.get(key) for key in (
                'classification', 'returncode', 'target_pid', 'target_birth',
                'error_type', 'leader_exited', 'process_group_gone',
                'group_exit_wait_seconds', 'process_group_observation',
                'collection_errors', 'cleanup_reason', 'host_requested_signal')
                if key in exc.cleanup_diagnostic}
            diagnostic['cleanup_confirmed'] = False
            path = (root / 'artifacts' / 'executions' / command.command_id /
                    'opencode-cleanup.json')
            atomic_json(path, diagnostic)
            compression_outputs['artifact_refs']['opencode_cleanup'] = {
                'path': project_relative(root, path).as_posix(),
                'sha256': file_digest(path), 'media_type': 'application/json'}
            if dialogue is not None:
                compression_outputs['artifact_refs'].update(dialogue_artifacts(
                    root, dialogue, getattr(exc, 'metadata', None)))
            return _result(command, 'failed',
                           outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                           detail='OpenCode cleanup is unconfirmed',
                           error_code='opencode_cleanup_unconfirmed')
        except RuntimeError as exc:
            from .telemetry import redact
            capture_native_goal_metadata(getattr(exc, 'metadata', None))
            if dialogue is not None:
                compression_outputs['artifact_refs'].update(dialogue_artifacts(
                    root, dialogue, getattr(exc, 'metadata', None)))
            return _result(command, "failed", outputs=_with_compression(_agent_log_outputs(root, log_relative)),
                           detail=f"OpenCode agent failed: {redact(str(exc))}",
                           error_code="agent_runtime_failed")
        from .rework_tools import review_rework_observation
        try:
            rework_observation = review_rework_observation(command, completed.stdout)
            if rework_observation is not None:
                from .evidence import atomic_json
                observation_path = root / 'artifacts' / 'executions' / command.command_id / 'review-rework-observation.json'
                atomic_json(observation_path, rework_observation)
                compression_outputs['review_rework_observation'] = rework_observation
                compression_outputs['artifact_refs']['review_rework_observation'] = {
                    'path': project_relative(root, observation_path).as_posix(),
                    'sha256': file_digest(observation_path), 'media_type': 'application/json'}
                business_diagnostics.append(rework_observation['detail'])
        except (OSError, ValueError, TypeError, KeyError) as exc:
            business_diagnostics.append('rework result observation unavailable: ' + str(exc))
        from .telemetry import public_last_message
        output.write_text(public_last_message(completed.stdout) + "\n", encoding="utf-8")
        last_message_available = True
        if dialogue is not None:
            business_diagnostics.extend(materialize_report(dialogue, worktree, output.read_text(encoding='utf-8')))
        if self.baseline:
            isolated, changed = _baseline_changes_are_isolated(worktree)
            if not isolated:
                if not gates_disabled:
                    return _result(command, "blocked", outputs={**_with_compression(_agent_log_outputs(root, log_relative)), "baseline_changes": changed}, detail="baseline stage wrote outside its assigned .modport output directory", error_code="baseline_write_scope_invalid")
                business_diagnostics.append("baseline stage changed paths outside its assigned .modport output directory: " + ", ".join(changed))
        if (frozen.exists() or gates_disabled
                and isinstance(command.artifact_refs.get("functional_contract_lock"), Mapping)):
            try:
                _verify_locked_artifacts(
                    root,
                    command.payload.get("locked_artifacts"),
                    rubric=rubric,
                    contract_ref=(command.artifact_refs.get("functional_contract_lock")
                                  if gates_disabled else None),
                )
            except (TypeError, ValueError, KeyError) as exc:
                if not gates_disabled:
                    return _result(command, "blocked", outputs=_with_compression(_agent_log_outputs(root, log_relative)), detail=f"locked artifacts are invalid after agent execution: {exc}", error_code="locked_artifact_invalid")
                business_diagnostics.append(f"locked artifact observation after execution: {exc}")
        if completed.returncode:
            return _result(command, "failed", outputs=_with_compression(_agent_log_outputs(root, log_relative)), detail="coding agent failed", error_code="agent_failed")
        if self.native_goal is not None and not completed.metadata.get("host_accepted"):
            if not gates_disabled:
                return _result(command, "failed", outputs=_with_compression(_agent_log_outputs(root, log_relative)), detail="coding agent failed", error_code="agent_failed")
            business_diagnostics.append("native goal validator did not accept the observed result")
        missing: list[str] = []
        for relative in self._required_paths(command):
            candidate = worktree / relative
            try:
                valid = (
                    not candidate.is_symlink()
                    and candidate.is_file()
                    and candidate.stat().st_size > 0
                    and candidate.resolve().is_relative_to(worktree.resolve())
                )
            except OSError:
                valid = False
            if not valid:
                missing.append(relative)
        if missing:
            if not gates_disabled:
                return _result(
                    command,
                    "failed",
                    outputs={**_with_compression(_agent_log_outputs(root, log_relative)), "missing_required_paths": missing},
                    detail="coding agent did not produce required stage outputs",
                    error_code="agent_output_missing",
                )
            business_diagnostics.append("requested stage outputs missing: " + ", ".join(missing))
        artifact_refs: dict[str, Mapping[str, Any]] = dict(_agent_log_outputs(root, log_relative).get('artifact_refs', {}))
        artifact_refs.update(compression_outputs.get("artifact_refs", {}))
        artifact_refs.update(wiki_refs)
        for relative in self._required_paths(command):
            candidate = worktree / relative
            if candidate.is_file() and not candidate.is_symlink():
                artifact_id, ref = _snapshot_stage_output(root, worktree, command, relative)
                artifact_refs[artifact_id] = ref
        if self.baseline and command.stage_id in {"contract_draft", "contract_revise"}:
            from .harness_snapshot import capture_harness
            try:
                artifact_refs.update(capture_harness(command))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                if not gates_disabled:
                    return _result(command, "blocked", outputs=_with_compression(_agent_log_outputs(root, log_relative)), detail=f"cannot authenticate harness source snapshot: {exc}",
                        error_code="harness_snapshot_invalid")
                business_diagnostics.append(f"harness source snapshot unavailable: {exc}")
        if output.is_file() and not output.is_symlink():
            artifact_refs[f"agent_log:{command.stage_id}"] = {
                "path": project_relative(root, output).as_posix(),
                "sha256": sha256(output.read_bytes()).hexdigest(),
                "media_type": "text/plain",
            }
        outputs: dict[str, Any] = {
            "agent_assignment": assignment,
            "log": log_relative,
            "last_message": str(project_relative(root, output)),
            "prompt_compression": str(project_relative(root, compression_path)),
            "prompt_compression_source": str(project_relative(root, source_prompt_path)),
            "prompt_compression_output": str(project_relative(root, compressed_prompt_path)),
        }
        if command.stage_id in {'background', 'gap_research'}:
            from .wiki_knowledge import export_findings
            drafts, diagnostics = export_findings(command, workspace=worktree)
            outputs.update(wiki_contribution_drafts=drafts,
                           wiki_contribution_diagnostics=diagnostics)
        if (self.baseline and command.stage_id in {'contract_draft', 'contract_revise'}
                and isinstance(command.payload.get('reviewer_rework'), Mapping)):
            from .repair_context import (inherited_harness_candidate_mode,
                                         observe_candidate, observe_source_head)
            if inherited_harness_candidate_mode(command, worktree):
                outputs['after_head'] = observe_source_head(worktree)
                outputs['after_candidate_id'] = observe_candidate(
                    worktree, inherited_harness=True)
        if missing:
            outputs["missing_required_paths"] = missing
        if artifact_refs:
            outputs["artifact_refs"] = artifact_refs
        if business_diagnostics:
            outputs.update(acceptance_status="unverified",
                           business_diagnostics=business_diagnostics)
        return _result(command, "completed", outputs=_with_compression(outputs), detail="coding agent completed")


class SupervisorHandler:
    """Investigate execution and publish goal revisions; retain frozen old policy."""

    def __call__(self, command: OperationInput) -> OperationResult:
        if 'watchdog_incident' in command.payload:
            from .watchdog_supervisor import invoke_watchdog_supervisor
            def stage_handler(prompt):
                return lambda prepared: CodexStageHandler(prompt, read_only=not bool(
                    prepared.payload.get('diagnostic_repair_manifest')))(prepared)
            return invoke_watchdog_supervisor(command, stage_handler)
        if 'desktop_chat' in command.payload:
            from .desktop_supervisor import invoke_chat
            return invoke_chat(command)
        if 'progress_supervision' in command.payload:
            from .progress_supervisor import invoke_progress_supervisor
            def stage_handler(prompt):
                return lambda prepared: CodexStageHandler(prompt, read_only=not bool(
                    prepared.payload.get('diagnostic_repair_manifest')))(prepared)
            return invoke_progress_supervisor(command, stage_handler)

        from .supervision import (SUPERVISOR_PROMPT, DEEP_SUPERVISOR_PROMPT,
                                  deep_supervision, validate_supervisor_decision)

        if deep_supervision(command):
            from .supervised_goals import prepare, collect
            try:
                prepared = prepare(command)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                return _result(command, 'failed', detail=str(exc),
                               error_code='supervision_input_invalid')
            prompt = (DEEP_SUPERVISOR_PROMPT + '\nHost goal/source manifest: '
                      + json.dumps(prepared.payload['supervised_goal_manifest_ref'], ensure_ascii=False))
            result = CodexStageHandler(prompt, read_only=False)(prepared)
            return collect(prepared, result)

        packet = command.payload.get("supervision_packet")
        if not isinstance(packet, dict):
            if not business_gates_disabled(command):
                return _result(command, "blocked", detail="supervision evidence packet is missing",
                               error_code="supervision_input_invalid")
            packet = {}
        allowed_stages = command.payload.get("supervisor_allowed_stages", [])
        allowed_profiles = command.payload.get("supervisor_allowed_profiles", [])
        allowed_tasks = command.payload.get("supervisor_allowed_task_ids", [])
        prompt = (SUPERVISOR_PROMPT + "\nHost evidence packet: "
                  + json.dumps(packet, ensure_ascii=False, sort_keys=True)
                  + "\nAllowed agent stages: " + json.dumps(allowed_stages)
                  + "\nAllowed profiles: " + json.dumps(allowed_profiles)
                  + "\nAllowed task IDs: " + json.dumps(allowed_tasks))
        result = CodexStageHandler(prompt, read_only=True)(command)
        if result.status != "completed":
            return result
        try:
            path = Path(result.outputs["last_message"])
            if not path.is_absolute():
                path = _run_root(command) / path
            document = json.loads(path.read_text(encoding="utf-8"))
            from .report_dialogue import dialogue_enabled, supervisor_document
            if dialogue_enabled(command):
                document = supervisor_document(document)
            decision = validate_supervisor_decision(
                document,
                evidence_execution_ids=packet.get("evidence_execution_ids", []),
                allowed_stages=allowed_stages,
                allowed_profiles=allowed_profiles,
                allowed_task_ids=allowed_tasks,
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            if business_gates_disabled(command):
                raw_report = ""
                try:
                    path = Path(result.outputs.get("last_message", ""))
                    path = path if path.is_absolute() else _run_root(command) / path
                    if path.is_file() and not path.is_symlink():
                        raw_report = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
                return _unverified_result(command, outputs={**result.outputs,
                    "raw_report": raw_report, "observed_supervisor_decision": None},
                    diagnostics=[str(exc)],
                    detail="supervisor observations retained without schema-gated routing")
            return _result(command, "failed", outputs=result.outputs,
                           detail=str(exc), error_code="supervision_output_invalid")
        return _result(command, "completed", outputs={**result.outputs,
            "supervisor_decision": decision, "supervision_window": packet.get("window_end")},
            detail="supervisor decision validated")


class _CharacterizationShapeError(ValueError):
    def __init__(self, detail: str, code: str) -> None:
        super().__init__(detail)
        self.code = code


def _validated_characterization_contract(
    contract: Mapping[str, Any], *, workflow_version: int = 0,
) -> CharacterizationContract:
    behaviors = contract.get("behaviors", contract.get("entries", []))
    if not isinstance(behaviors, list) or not behaviors:
        raise _CharacterizationShapeError("functional contract has no behaviors", "contract_empty")
    required = {"id", "source_evidence", "preconditions", "action", "assertions", "side", "test_mapping"}
    if any(not isinstance(item, dict) or not required.issubset(item) for item in behaviors):
        raise _CharacterizationShapeError("functional contract behavior schema is incomplete", "contract_invalid")
    try:
        typed = CharacterizationContract.from_mapping(contract)
    except (TypeError, ValueError) as exc:
        raise _CharacterizationShapeError(f"functional contract is invalid: {exc}", "contract_invalid") from exc
    if not typed.entries:
        raise _CharacterizationShapeError("functional contract has no typed behavior entries", "contract_empty")
    if any(not entry.preconditions or not entry.operations or not entry.assertions or not entry.test_mapping for entry in typed.entries):
        raise _CharacterizationShapeError("every behavior requires preconditions, actions, assertions, and executable test mapping", "contract_weak")
    if workflow_version >= 29:
        assertion_ids: set[str] = set()
        for entry in typed.entries:
            if len(entry.assertion_contracts) != len(entry.assertions):
                raise _CharacterizationShapeError(
                    f"behavior {entry.entry_id!r} requires a source-anchored assertion record for every assertion",
                    "assertion_invalid",
                )
            mapped: set[str] = set()
            for assertion in entry.assertion_contracts:
                if assertion.assertion_id in assertion_ids:
                    raise _CharacterizationShapeError(
                        f"assertion_id {assertion.assertion_id!r} must be globally unique",
                        "assertion_invalid",
                    )
                assertion_ids.add(assertion.assertion_id)
                mapped.update(assertion.test_ids)
            if mapped != set(entry.test_mapping):
                raise _CharacterizationShapeError(
                    f"behavior {entry.entry_id!r} assertion test IDs must exactly equal test_mapping",
                    "assertion_invalid",
                )
    return typed


def _resolve_assertion_source_anchors(
    contract: Mapping[str, Any], source_root: Path, source_commit: str,
) -> dict[str, Any]:
    """Resolve author line ranges to byte identities in the original checkout."""
    from .characterization import canonical_json

    source_root = Path(source_root)
    if (source_root.is_symlink() or not source_root.is_dir()
            or source_root.resolve() != source_root.absolute()):
        raise _CharacterizationShapeError(
            "original source checkout is missing or unsafe", "assertion_invalid",
        )
    git_env = {
        "PATH": "/usr/bin:/bin", "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    try:
        head = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-C", str(source_root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env,
            timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _CharacterizationShapeError(
            f"original source commit cannot be verified: {exc}", "assertion_invalid",
        ) from exc
    if (head.returncode or head.stdout.decode("ascii", errors="ignore").strip()
            != source_commit):
        raise _CharacterizationShapeError(
            "original source checkout HEAD does not match source_commit",
            "assertion_invalid",
        )
    typed = CharacterizationContract.from_mapping(contract)
    result = dict(contract)
    behaviors = []
    for entry in typed.entries:
        behavior = entry.to_dict()
        resolved = []
        for assertion in entry.assertion_contracts:
            anchor = assertion.source_anchor
            if anchor.source_commit is not None and anchor.source_commit != source_commit:
                raise _CharacterizationShapeError(
                    f"assertion {assertion.assertion_id!r} source anchor names a different original commit",
                    "assertion_invalid",
                )
            source_path = source_root.joinpath(*anchor.path.split("/"))
            if (not source_path.is_relative_to(source_root)
                    or source_path.resolve() != source_path.absolute()
                    or not source_path.is_file()):
                raise _CharacterizationShapeError(
                    f"assertion {assertion.assertion_id!r} source anchor is missing or unsafe: {anchor.path}",
                    "assertion_invalid",
                )
            for parent in source_path.parents:
                if parent == source_root.parent:
                    break
                if parent.is_symlink():
                    raise _CharacterizationShapeError(
                        f"assertion {assertion.assertion_id!r} source anchor crosses a symbolic link",
                        "assertion_invalid",
                    )
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(source_path, flags)
                try:
                    before = os.fstat(descriptor)
                    if not stat.S_ISREG(before.st_mode) or before.st_size > 16 * 1024 * 1024:
                        raise ValueError("source anchor is not a bounded regular file")
                    chunks = []
                    while block := os.read(descriptor, 1024 * 1024):
                        chunks.append(block)
                    after = os.fstat(descriptor)
                    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                              value.st_mtime_ns, value.st_ctime_ns)
                    if identity(before) != identity(after):
                        raise ValueError("source anchor changed while it was read")
                    data = b"".join(chunks)
                finally:
                    os.close(descriptor)
            except OSError as exc:
                raise _CharacterizationShapeError(
                    f"assertion {assertion.assertion_id!r} source anchor could not be read: {exc}",
                    "assertion_invalid",
                ) from exc
            try:
                lines = data.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError as exc:
                raise _CharacterizationShapeError(
                    f"assertion {assertion.assertion_id!r} source anchor is not UTF-8",
                    "assertion_invalid",
                ) from exc
            if anchor.end_line > len(lines):
                raise _CharacterizationShapeError(
                    f"assertion {assertion.assertion_id!r} source anchor exceeds the original file",
                    "assertion_invalid",
                )
            selected = "".join(lines[anchor.start_line - 1:anchor.end_line]).encode("utf-8")
            source_file_sha256 = sha256(data).hexdigest()
            range_sha256 = sha256(selected).hexdigest()
            if (anchor.source_file_sha256 is not None
                    and anchor.source_file_sha256 != source_file_sha256):
                raise _CharacterizationShapeError(
                    f"assertion {assertion.assertion_id!r} source file digest does not match",
                    "assertion_invalid",
                )
            if anchor.range_sha256 is not None and anchor.range_sha256 != range_sha256:
                raise _CharacterizationShapeError(
                    f"assertion {assertion.assertion_id!r} source range digest does not match",
                    "assertion_invalid",
                )
            resolved_anchor = SourceAnchor(
                path=anchor.path, start_line=anchor.start_line, end_line=anchor.end_line,
                source_commit=source_commit, source_file_sha256=source_file_sha256,
                range_sha256=range_sha256,
            )
            resolved.append(AssertionContract(
                assertion.assertion_id, assertion.text, resolved_anchor, assertion.test_ids,
            ).to_dict())
        behavior["assertion_contracts"] = resolved
        behaviors.append(behavior)
    result["behaviors"] = behaviors
    result.pop("entries", None)
    return result


def _assertion_review_diagnostics(
    contract: Mapping[str, Any], review: Mapping[str, Any],
) -> list[str]:
    """Require independent semantic support for each exact original-source range."""
    expected: dict[str, Mapping[str, Any]] = {}
    for behavior in contract.get("behaviors", contract.get("entries", ())):
        if not isinstance(behavior, Mapping):
            continue
        for assertion in behavior.get("assertion_contracts", ()):
            if isinstance(assertion, Mapping) and isinstance(assertion.get("assertion_id"), str):
                expected[assertion["assertion_id"]] = assertion
    rows = review.get("assertion_reviews") if isinstance(review, Mapping) else None
    if not isinstance(rows, list):
        return ["independent review lacks per-assertion semantic source assessments"]
    observed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            return ["independent review has a malformed assertion assessment"]
        assertion_id = row.get("assertion_id")
        if not isinstance(assertion_id, str) or assertion_id in observed:
            return ["independent review has missing or duplicate assertion IDs"]
        observed[assertion_id] = row
    diagnostics: list[str] = []
    if set(observed) != set(expected):
        diagnostics.append("independent semantic review does not cover the exact assertion ID set")
    for assertion_id, assertion in expected.items():
        row = observed.get(assertion_id)
        if row is None:
            continue
        anchor = assertion.get("source_anchor")
        reviewed_anchor = row.get("source_anchor")
        if (not isinstance(anchor, Mapping) or not isinstance(reviewed_anchor, Mapping)
                or any(reviewed_anchor.get(field) != anchor.get(field)
                       for field in ("path", "start_line", "end_line"))):
            diagnostics.append(f"assertion {assertion_id!r} review is not bound to its exact source range")
        if row.get("status") != "supported":
            diagnostics.append(f"assertion {assertion_id!r} is unsupported, ambiguous, or unreviewed")
        reasoning = row.get("reasoning")
        if not isinstance(reasoning, str) or not reasoning.strip():
            diagnostics.append(f"assertion {assertion_id!r} has no semantic comparison rationale")
    return diagnostics


def _collect_v29_case_results(
    *, root: Path, workspace: Path, command: OperationInput,
    contract: Mapping[str, Any], declarations: Mapping[str, Mapping[str, Any]],
    executor_provenance: Mapping[str, Mapping[str, Any]], source_commit: str,
    candidate_before: Mapping[str, Any] | None, execution_nonce: str,
    contract_valid: bool, reviewed_target: bool, wiring_refs: Mapping[str, Mapping[str, Any]],
    phase: str,
) -> tuple[dict[str, Any], dict[str, Any], list[str], dict[str, Any], bool | None,
           Mapping[str, Any] | None]:
    """Bind assertions to fresh exact XML results; v34 records source provenance."""
    from .opencode_shell_mcp import _read_junit_result, _workspace_candidate_identity

    errors: list[str] = []
    target_only = command.options.get('workflow_version', 0) >= 34
    candidate_after: Mapping[str, Any] | None = None
    unchanged: bool | None = None
    if not target_only:
        try:
            candidate_after = _workspace_candidate_identity(workspace)
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            errors.append(f"candidate identity could not be revalidated: {exc}")
        unchanged = (
            isinstance(candidate_before, Mapping) and isinstance(candidate_after, Mapping)
            and candidate_before.get("candidate_id") == candidate_after.get("candidate_id")
        )
        if not unchanged:
            errors.append("candidate identity changed or could not be revalidated during characterization")

    anchors_by_test: dict[str, list[dict[str, Any]]] = {}
    assertions: dict[str, Mapping[str, Any]] = {}
    for behavior in contract.get("behaviors", contract.get("entries", ())):
        if not isinstance(behavior, Mapping):
            continue
        for assertion in behavior.get("assertion_contracts", ()):
            if not isinstance(assertion, Mapping):
                continue
            assertion_id = assertion.get("assertion_id")
            test_ids = assertion.get("test_ids", ())
            if not isinstance(assertion_id, str) or not isinstance(test_ids, list):
                continue
            assertions[assertion_id] = assertion
            anchor = assertion.get("source_anchor")
            source_row = {
                "assertion_id": assertion_id, "text": assertion.get("text"),
                "source_anchor": dict(anchor) if isinstance(anchor, Mapping) else None,
            }
            for test_id in test_ids:
                anchors_by_test.setdefault(str(test_id), []).append(source_row)
    wiring_identity = [
        {"path": ref.get("path"), "sha256": ref.get("sha256")}
        for alias, ref in wiring_refs.items()
        if alias == f"{phase}_harness_wiring" or alias.startswith(f"{phase}_harness_wiring:")
    ]
    candidate_id = candidate_before.get("candidate_id") if isinstance(candidate_before, Mapping) else None
    case_results: dict[str, dict[str, Any]] = {}
    result_refs: dict[str, Any] = {}
    for test_id, declaration in declarations.items():
        identity = declaration.get("result_identity")
        result: dict[str, Any] | None = None
        result_error = None
        if isinstance(identity, Mapping):
            try:
                result = _read_junit_result(workspace, test_id, identity, require_isolated=False,
                    include_xml_digest=command.options.get('workflow_version', 0) < 35)
            except (OSError, ValueError, TypeError) as exc:
                result_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
        else:
            result_error = "exact JUnit result identity is absent"
        xml_ref = None
        if result is not None:
            relative_xml = result.get("xml_path")
            try:
                if not isinstance(relative_xml, str):
                    raise ValueError("JUnit result path is missing")
                _, xml_ref = _snapshot_stage_output(root, workspace, command, relative_xml)
                result_refs[f"{phase}_junit_result:{test_id}"] = xml_ref
            except (OSError, ValueError) as exc:
                result_error = f"JUnit result snapshot failed: {exc}"
                xml_ref = None
        if result_error:
            errors.append(f"{test_id}: {result_error}")
        source_rows = sorted(anchors_by_test.get(test_id, ()),
                             key=lambda item: str(item.get("assertion_id")))
        identity_fields = {}
        if not target_only:
            identity_material = {
                "candidate_id": candidate_id,
                "source_commit": source_commit,
                "assertions": source_rows,
                "declaration": declaration,
                "test_source": executor_provenance.get(test_id, {}),
                "harness_wiring": wiring_identity,
            }
            identity_fields = {
                "case_identity": sha256(canonical_json(identity_material).encode("utf-8")).hexdigest(),
                "case_identity_inputs": identity_material,
                "candidate_id": candidate_id,
            }
        actual = result.get("outcome") if result is not None else None
        is_bound = bool(contract_valid and (target_only or unchanged) and xml_ref is not None)
        if not contract_valid:
            category = "assertion_invalid"
        elif result_error or result is None or xml_ref is None:
            category = "test_infrastructure"
        elif not target_only and not unchanged:
            category = "unknown"
        elif actual == "failed":
            category = "mod_behavior" if reviewed_target else "unknown"
        elif actual in {"passed", "skipped"}:
            category = "none"
        else:
            category = "test_infrastructure"
        case_results[test_id] = {
            "test_id": test_id,
            "status": actual if is_bound else "unverified",
            "test_outcome": actual or "missing",
            "category": category,
            "result_identity": dict(identity) if isinstance(identity, Mapping) else None,
            "test_result": result,
            "xml_artifact_ref": xml_ref,
            "source_commit": source_commit,
            "candidate_unchanged": unchanged,
            "execution_nonce": execution_nonce,
            **identity_fields,
        }
    assertion_results: dict[str, dict[str, Any]] = {}
    for assertion_id, assertion in assertions.items():
        test_ids = list(assertion.get("test_ids", ()))
        linked = {test_id: case_results[test_id] for test_id in test_ids if test_id in case_results}
        if not contract_valid:
            status, category = "unverified", "assertion_invalid"
        elif set(linked) != set(test_ids):
            status, category = "unverified", "test_infrastructure"
        elif not target_only and not unchanged:
            status, category = "unverified", "unknown"
        elif all(row.get("status") == "passed" for row in linked.values()):
            status, category = "passed", "none"
        elif any(row.get("test_outcome") == "failed" for row in linked.values()):
            status = "failed"
            category = "mod_behavior" if reviewed_target else "unknown"
        else:
            status, category = "unverified", "test_infrastructure"
        assertion_results[assertion_id] = {
            "assertion_id": assertion_id,
            "test_ids": test_ids,
            "status": status,
            "category": category,
            "source_anchor": assertion.get("source_anchor"),
            **({'source_anchors': assertion.get('source_anchors', [])} if target_only else {}),
            "test_results": {
                test_id: {
                    "result_identity": linked[test_id].get("result_identity"),
                    "status": linked[test_id].get("status"),
                    "test_outcome": linked[test_id].get("test_outcome"),
                    **({"case_identity": linked[test_id].get("case_identity"),
                        "candidate_id": linked[test_id].get("candidate_id")} if not target_only else {}),
                    "execution_nonce": linked[test_id].get("execution_nonce"),
                    "xml_artifact_ref": linked[test_id].get("xml_artifact_ref"),
                    "test_result": linked[test_id].get("test_result"),
                } for test_id in test_ids if test_id in linked
            },
        }
    return case_results, assertion_results, errors, result_refs, unchanged, candidate_after


class FreezeContractHandler:
    def __call__(self, command: OperationInput) -> OperationResult:
        if business_gates_disabled(command):
            return self._freeze_unverified(command)
        root = _run_root(command)
        try:
            rubric = _acceptance_rubric_for(command, root)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            return _result(command, "failed", detail=f"acceptance rubric is invalid: {exc}", error_code="acceptance_rubric_invalid")
        source = root / "baseline" / ".modport" / "functional-contract.json"
        review = root / "baseline" / ".modport" / "contract-review.json"
        baseline_evidence = root / "artifacts" / "baseline-contract-tests.json"
        if not source.exists() or not review.exists() or not baseline_evidence.exists():
            return _result(command, "failed", detail="candidate contract or independent review is missing", error_code="contract_missing")
        contract = json.loads(source.read_text(encoding="utf-8"))
        verdict = json.loads(review.read_text(encoding="utf-8"))
        executed = json.loads(baseline_evidence.read_text(encoding="utf-8"))
        source_evidence = json.loads((root / "artifacts" / "source.json").read_text(encoding="utf-8"))
        if not isinstance(contract, dict) or not isinstance(source_evidence, Mapping):
            return _result(command, "failed", detail="functional contract or source evidence is invalid", error_code="contract_invalid")
        # Fill provenance owned by the host.  Agents describe behavior; they
        # do not need to calculate source/rubric digests in their JSON.
        contract = dict(contract)
        contract.setdefault("source_fingerprint", source_evidence.get("source_commit"))
        contract.setdefault("rubric_id", rubric.get("rubric_id"))
        contract.setdefault("rubric_version", rubric.get("rubric_version"))
        contract.setdefault("rubric_sha256", rubric.get("rubric_sha256"))
        # This is a host-generated report field, never a value the review
        # agent has to calculate or that later stages use as a checkout lock.
        candidate_file_sha256 = sha256(source.read_bytes()).hexdigest()
        if (
            executed.get("exit_code") != 0
        ):
            return _result(command, "failed", detail="deterministic baseline contract tests did not pass for this candidate", error_code="baseline_contract_failed")
        try:
            _test_evidence_declarations(
                contract, rubric, workflow_version=command.options.get('workflow_version', 0),
                gradle_tasks=contract.get('baseline_gradle_tasks'),
            )
        except (TypeError, ValueError) as exc:
            return _result(command, "failed", detail=f"functional contract evidence is invalid: {exc}", error_code="contract_invalid")
        if (
            verdict.get("rubric_id") != rubric.get("rubric_id")
            or verdict.get("rubric_version") != rubric.get("rubric_version")
        ):
            return _result(command, "failed", detail="independent review is not bound to the active rubric", error_code="contract_review_stale")
        try:
            typed_contract = _validated_characterization_contract(
                contract, workflow_version=command.options.get('workflow_version', 0),
            )
        except _CharacterizationShapeError as exc:
            return _result(command, "failed", detail=str(exc), error_code=exc.code)
        if not executed.get("evidence_files"):
            return _result(command, "failed", detail="baseline contract has no freshly generated executable evidence", error_code="baseline_contract_failed")
        if typed_contract.source_fingerprint != source_evidence.get("source_commit"):
            return _result(command, "failed", detail="functional contract is not bound to the immutable source commit", error_code="contract_source_mismatch")
        if command.options.get('workflow_version', 0) >= 29:
            try:
                contract = _resolve_assertion_source_anchors(
                    contract, root / 'baseline', str(source_evidence.get('source_commit', '')),
                )
                typed_contract = _validated_characterization_contract(
                    contract, workflow_version=command.options.get('workflow_version', 0),
                )
                assertion_review_errors = _assertion_review_diagnostics(contract, verdict)
                if verdict.get('verdict') != 'approved' or assertion_review_errors:
                    raise ValueError('; '.join(assertion_review_errors)
                                     or 'independent semantic assertion review is not approved')
                assertions = {
                    item.assertion_id: item for entry in typed_contract.entries
                    for item in entry.assertion_contracts
                }
                observed = executed.get('assertion_results')
                if not isinstance(observed, Mapping) or set(observed) != set(assertions):
                    raise ValueError('baseline receipt lacks one result for every frozen assertion')
                for assertion_id, assertion in assertions.items():
                    item = observed[assertion_id]
                    if (not isinstance(item, Mapping) or item.get('status') != 'passed'
                            or item.get('test_ids') != list(assertion.test_ids)
                            or any(test_id not in item.get('test_results', {})
                                   for test_id in assertion.test_ids)):
                        raise ValueError(f'baseline assertion {assertion_id!r} lacks exact passing test identities')
            except (_CharacterizationShapeError, OSError, TypeError, ValueError, KeyError) as exc:
                return _result(command, 'failed', detail=f'v29 assertion evidence is invalid: {exc}',
                               error_code='assertion_invalid')
        if verdict.get("verdict") != "approved":
            return _result(command, "failed", detail="independent contract review did not approve the contract", error_code="contract_unapproved")
        reviewer_id = str(verdict.get("reviewer_id", "")).strip()
        generator_id = typed_contract.generator_id
        if not reviewer_id or not generator_id or reviewer_id == generator_id:
            return _result(command, "failed", detail="contract generator and reviewer identities must be distinct", error_code="review_not_independent")
        typed_review = ReviewRecord(
            reviewer_id=reviewer_id,
            generator_id=generator_id,
            contract_sha256=typed_contract.sha256(),
            status="approved",
            review_id=str(verdict.get("review_id", "")),
            notes=str(verdict.get("notes", "")),
            evidence_refs=tuple(str(item) for item in verdict.get("baseline_commands", ())),
        )
        frozen = freeze_contract(typed_contract, typed_review)
        payload = frozen.to_dict()
        payload["candidate_file_sha256"] = candidate_file_sha256
        payload["baseline_verification"] = executed
        payload["baseline_gradle_tasks"] = contract.get("baseline_gradle_tasks", [])
        payload["baseline_evidence_files"] = contract.get("baseline_evidence_files", [])
        payload["test_evidence"] = contract.get("test_evidence", {})
        payload["acceptance_rubric"] = {
            "rubric_id": rubric["rubric_id"],
            "rubric_version": rubric["rubric_version"],
        }
        payload["lock_sha256"] = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        target = root / "artifacts" / "functional-contract.lock.json"
        _write_json(target, payload)
        manifest_data = json.loads((root / "artifacts" / "locked-manifest.json").read_text(encoding="utf-8"))
        anchors = {
            "manifest_sha256": str(manifest_data.get("manifest_sha256", "")),
            "contract_lock_sha256": payload["lock_sha256"],
        }
        refs = {
            "functional_contract_lock": {
                "path": "artifacts/functional-contract.lock.json",
                "sha256": sha256(target.read_bytes()).hexdigest(),
                "media_type": "application/json",
            },
            "baseline_contract_tests": {
                "path": "artifacts/baseline-contract-tests.json",
                "sha256": sha256(baseline_evidence.read_bytes()).hexdigest(),
                "media_type": "application/json",
            },
            "contract_review": {
                "path": "baseline/.modport/contract-review.json",
                "sha256": sha256(review.read_bytes()).hexdigest(),
                "media_type": "application/json",
            },
        }
        return _result(command, "completed", outputs={"contract_sha256": payload["lock_sha256"], "behavior_count": len(typed_contract.entries), "locked_artifacts": anchors, "artifact_refs": refs}, detail="functional contract frozen")

    def _freeze_unverified(self, command: OperationInput) -> OperationResult:
        root = _run_root(command)
        diagnostics = []

        def read_mapping(path: Path, label: str) -> dict[str, Any]:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
                raise ValueError("expected object")
            except (OSError, ValueError, TypeError) as exc:
                diagnostics.append(f"{label}: {exc}")
                return {}

        source_path = root / "baseline" / ".modport" / "functional-contract.json"
        review_path = root / "baseline" / ".modport" / "contract-review.json"
        evidence_path = root / "artifacts" / "baseline-contract-tests.json"
        contract = read_mapping(source_path, "functional contract observation")
        identity_only = (28 <= command.options.get('workflow_version', 0) < 31
                         and compile_package_scope(command))
        review = ({} if identity_only else read_mapping(review_path, "contract review observation"))
        evidence = ({} if command.options.get('workflow_version', 0) >= 26
                    else read_mapping(evidence_path, "baseline characterization observation"))
        if command.options.get('workflow_version', 0) >= 26 and not identity_only:
            # A fixed workspace file can predate a failed fresh reviewer.
            # Bind the observation to that review's sealed decision instead.
            from .evidence import verified_path
            fresh_review = command.upstream_results.get('contract_review', {})
            review_ref = (fresh_review.get('outputs', {}).get('artifact_refs', {})
                          .get('contract_review') if isinstance(fresh_review, Mapping) else None)
            try:
                if not isinstance(review_ref, Mapping):
                    raise ValueError('fresh independent review has no sealed decision')
                decision_path = verified_path(root, review_ref)
                if review_ref.get('sha256') != file_digest(decision_path):
                    raise ValueError('fresh review decision digest does not match file')
                decision = json.loads(decision_path.read_text(encoding='utf-8'))
                if (not isinstance(decision, dict) or decision != review
                        or decision.get('review_id') != fresh_review.get('command_id')):
                    raise ValueError('review observation does not match fresh reviewer execution')
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                review = {}
                diagnostics.append(f'fresh contract review unavailable: {exc}')
        current = command.upstream_results.get('contract_verify', {})
        if command.options.get('workflow_version', 0) >= 18:
            # A failed fresh verifier must not inherit an older fixed-name report.
            evidence = {key: current[key] for key in ('command_id', 'status', 'error_code', 'detail', 'outputs')
                        if key in current}
        source = read_mapping(root / "artifacts" / "source.json", "source record observation")
        try:
            rubric = _acceptance_rubric_for(command, root)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            from .rubric import acceptance_rubric
            rubric = acceptance_rubric()
            diagnostics.append(f"acceptance rubric observation: {exc}")
        candidate_sha = file_digest(source_path) if source_path.is_file() and not source_path.is_symlink() else None
        v29_observation: dict[str, Any] = {}
        if command.options.get('workflow_version', 0) >= 29:
            source_commit = source.get('source_commit')
            resolved_contract: Mapping[str, Any] | None = None
            try:
                if not isinstance(source_commit, str) or not source_commit:
                    raise ValueError('source commit identity is unavailable')
                resolved_contract = _resolve_assertion_source_anchors(
                    contract, root / 'baseline', source_commit,
                )
                _validated_characterization_contract(resolved_contract, workflow_version=29)
                source_status = 'resolved'
            except (_CharacterizationShapeError, OSError, TypeError, ValueError,
                    KeyError, UnicodeError) as exc:
                source_status = 'invalid_or_missing'
                diagnostics.append(f'v29 assertion source anchors are unverified: {exc}')
            semantic_errors = _assertion_review_diagnostics(contract, review)
            if review.get('verdict') != 'approved':
                semantic_errors.insert(0, 'independent semantic contract review is not approved')
            if semantic_errors:
                diagnostics.extend('v29 assertion review: ' + item for item in semantic_errors)
            fresh_report: Mapping[str, Any] = {}
            if not identity_only:
                try:
                    report_path = _fresh_baseline_report(command, root)
                    loaded_report = json.loads(report_path.read_text(encoding='utf-8'))
                    if not isinstance(loaded_report, Mapping):
                        raise ValueError('fresh baseline report is not an object')
                    fresh_report = loaded_report
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    diagnostics.append(f'v29 baseline case receipts are unavailable: {exc}')
            observed_assertions = fresh_report.get('assertion_results', {})
            case_result_errors: list[str] = []
            candidate_before = fresh_report.get('candidate_before', {})
            expected_candidate_id = (candidate_before.get('candidate_id')
                                     if isinstance(candidate_before, Mapping) else None)
            expected_assertions: dict[str, AssertionContract] = {}
            if resolved_contract is not None:
                try:
                    typed = CharacterizationContract.from_mapping(resolved_contract)
                    expected_assertions = {
                        item.assertion_id: item for entry in typed.entries
                        for item in entry.assertion_contracts
                    }
                except (TypeError, ValueError) as exc:
                    case_result_errors.append(f'validated assertion set is malformed: {exc}')
            if not identity_only and resolved_contract is not None:
                if (not isinstance(observed_assertions, Mapping)
                        or set(observed_assertions) != set(expected_assertions)):
                    case_result_errors.append('fresh verifier receipt does not cover the exact assertion ID set')
                else:
                    declarations = contract.get('test_evidence', {})
                    for assertion_id, assertion in expected_assertions.items():
                        result = observed_assertions.get(assertion_id)
                        result_tests = result.get('test_results') if isinstance(result, Mapping) else None
                        if (not isinstance(result, Mapping)
                                or result.get('test_ids') != list(assertion.test_ids)
                                or result.get('status') != 'passed'
                                or not isinstance(result_tests, Mapping)
                                or set(result_tests) != set(assertion.test_ids)):
                            case_result_errors.append(
                                f'assertion {assertion_id!r} lacks exact passing runtime test results')
                            continue
                        for test_id in assertion.test_ids:
                            test_result = result_tests.get(test_id)
                            expected_identity = (declarations.get(test_id, {}).get('result_identity')
                                                 if isinstance(declarations, Mapping)
                                                 and isinstance(declarations.get(test_id), Mapping) else None)
                            xml_ref = test_result.get('xml_artifact_ref') if isinstance(test_result, Mapping) else None
                            if (not isinstance(test_result, Mapping)
                                    or test_result.get('test_outcome') != 'passed'
                                    or test_result.get('result_identity') != expected_identity
                                    or test_result.get('candidate_id') != expected_candidate_id
                                    or not isinstance(test_result.get('case_identity'), str)
                                    or not isinstance(test_result.get('execution_nonce'), str)
                                    or test_result.get('execution_nonce') != fresh_report.get('execution_nonce')
                                    or not isinstance(xml_ref, Mapping)):
                                case_result_errors.append(
                                    f'assertion {assertion_id!r} test {test_id!r} lacks exact current receipt identity')
                                continue
                            try:
                                from .evidence import verified_path
                                artifact = verified_path(root, xml_ref)
                                if file_digest(artifact) != xml_ref.get('sha256'):
                                    raise ValueError('JUnit XML artifact digest mismatch')
                            except (OSError, ValueError, TypeError, KeyError) as exc:
                                case_result_errors.append(
                                    f'assertion {assertion_id!r} test {test_id!r} XML artifact is invalid: {exc}')
            if case_result_errors:
                diagnostics.extend('v29 case evidence: ' + item for item in case_result_errors)
            if identity_only:
                diagnostics.append('behavior acceptance remains unverified under compile/package scope')
            v29_observation = {
                'source_anchor_status': source_status,
                'semantic_review_status': ('approved' if not semantic_errors else 'unverified'),
                'case_results': fresh_report.get('case_results', {}) if isinstance(fresh_report, Mapping) else {},
                'assertion_results': observed_assertions if isinstance(observed_assertions, Mapping) else {},
                'case_result_diagnostics': case_result_errors,
                'verification_binding': ('source_identity_only' if identity_only
                                         else fresh_report.get('verification_binding')),
                'acceptance_status': 'unverified',
            }
        payload = {
            "schema_version": 1,
            "contract": contract,
            "review": review,
            "candidate_file_sha256": candidate_sha,
            "baseline_verification": evidence,
            "baseline_gradle_tasks": contract.get("baseline_gradle_tasks", []),
            "baseline_evidence_files": contract.get("baseline_evidence_files", []),
            "test_evidence": contract.get("test_evidence", {}),
            "source_fingerprint": source.get("source_commit"),
            "acceptance_rubric": {key: rubric.get(key) for key in ("rubric_id", "rubric_version")},
            "acceptance_status": "unverified",
            "business_diagnostics": diagnostics,
            **({'v29_assertion_observation': v29_observation}
               if command.options.get('workflow_version', 0) >= 29 else {}),
        }
        if (command.options.get('workflow_version', 0) >= 25
                and 'inherited_harness' in command.artifact_refs):
            from .repair_context import observe_candidate, observe_source_head
            identity = _inherited_contract_identity_observation(command)
            verifier_outputs = current.get('outputs', {}) if isinstance(current, Mapping) else {}
            verifier_outputs = verifier_outputs if isinstance(verifier_outputs, Mapping) else {}
            verifier_identity = verifier_outputs.get('inherited_contract_identity')
            verifier_identity = verifier_identity if isinstance(verifier_identity, Mapping) else {}
            workspace = root / 'baseline'
            freeze_candidate_id = observe_candidate(workspace, inherited_harness=True)
            source_head = observe_source_head(workspace)
            verifier_candidate_id = verifier_outputs.get('verification_candidate_id')
            identity_fields = (
                'selected_contract_id', 'candidate_contract_id',
                'selected_behavior_ids', 'candidate_behavior_ids',
                'selected_source_commit', 'current_source_commit',
                'candidate_source_fingerprint', 'selected_artifact_sha256',
                'candidate_sha256',
            )
            matched = (
                identity.get('status') == verifier_identity.get('status') == 'preserved'
                and all(identity.get(key) == verifier_identity.get(key)
                        for key in identity_fields)
                and identity.get('selected_contract_id') == identity.get('candidate_contract_id')
                and identity.get('selected_behavior_ids') == identity.get('candidate_behavior_ids')
                and identity.get('candidate_sha256') == candidate_sha
                and source_head == identity.get('selected_source_commit')
                and verifier_outputs.get('verification_binding') == (
                    'source_identity_only' if identity_only else 'host_observed_harness_candidate')
                and isinstance(verifier_candidate_id, str) and bool(verifier_candidate_id)
                and freeze_candidate_id == verifier_candidate_id
            )
            payload['inherited_harness_binding'] = {
                'status': 'matched' if matched else 'unverified',
                'freeze_identity': identity,
                'verifier_execution_id': current.get('command_id') if isinstance(current, Mapping) else None,
                'verifier_status': current.get('status') if isinstance(current, Mapping) else None,
                'verifier_identity_status': verifier_identity.get('status'),
                'verifier_candidate_id': verifier_candidate_id,
                'freeze_candidate_id': freeze_candidate_id,
                'source_head': source_head,
            }
            if not matched:
                diagnostics.append('inherited harness candidate or selected identity changed after verification')
        if command.options.get('workflow_version', 0) >= 31:
            from .test_matrix_runtime import frozen_selection, selected_assertion_review
            payload['source_contract'] = contract
            payload['source_review'] = review
            try:
                selection = frozen_selection(command)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                diagnostics.append('baseline test selection unavailable: ' + str(exc))
                payload['test_selection'] = {'status': 'unverified', 'diagnostics': [str(exc)]}
            else:
                payload['test_selection'] = selection
                payload['source_defects'] = selection['source_defects']
                payload['uncovered_assertion_ids'] = selection['uncovered_assertion_ids']
                diagnostics.extend(selection['diagnostics'])
                contract = selection['migration_contract']
                payload['review'] = selected_assertion_review(contract, review)
                payload.update(contract=contract,
                    baseline_gradle_tasks=contract.get('baseline_gradle_tasks', []),
                    baseline_evidence_files=contract.get('baseline_evidence_files', []),
                    test_evidence=contract.get('test_evidence', {}))
        payload["lock_sha256"] = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        target = (root / "artifacts" / "executions" / command.command_id
                  / "functional-contract-observation.json")
        if target.exists():
            diagnostics.append("existing functional contract observation retained")
            stored = read_mapping(target, "functional contract observation artifact")
        else:
            _write_json(target, payload)
            stored = payload
        stored_lock_sha = stored.get("lock_sha256") if isinstance(stored, Mapping) else None
        if not isinstance(stored_lock_sha, str):
            stored_lock_sha = sha256(canonical_json({key: value for key, value in stored.items()
                if key != "lock_sha256"}).encode("utf-8")).hexdigest()
        refs = {}
        if command.options.get('workflow_version', 0) >= 18 and (not contract or candidate_sha is None):
            return _unverified_result(command, status='failed', outputs={'artifact_refs': {
                'functional_contract_observation': {'path': project_relative(root, target).as_posix(),
                    'sha256': file_digest(target), 'media_type': 'application/json'}}},
                diagnostics=diagnostics + ['No authentic contract input is available; no lock reference published.'],
                detail='functional contract remains unavailable', error_code='contract_missing')
        if target.is_file() and not target.is_symlink():
            refs["functional_contract_lock"] = {"path": project_relative(root, target).as_posix(),
                "sha256": file_digest(target), "media_type": "application/json"}
        for alias, path in (("baseline_contract_tests", evidence_path), ("contract_review", review_path)):
            if command.options.get('workflow_version', 0) >= 18 and alias == 'baseline_contract_tests':
                fresh_refs = current.get('outputs', {}).get('artifact_refs', {})
                fresh = fresh_refs.get('baseline_contract_tests_candidate'
                    if command.options.get('workflow_version', 0) >= 26 else alias)
                if isinstance(fresh, dict):
                    if command.options.get('workflow_version', 0) >= 26:
                        try:
                            _fresh_baseline_report(command, root)
                        except (OSError, ValueError, TypeError) as exc:
                            diagnostics.append(f'fresh baseline report unavailable: {exc}')
                        else:
                            refs[alias] = fresh
                    else:
                        refs[alias] = fresh
                continue
            if (alias == 'contract_review' and command.options.get('workflow_version', 0) >= 26
                    and not review):
                continue
            if path.is_file() and not path.is_symlink():
                refs[alias] = {"path": project_relative(root, path).as_posix(),
                               "sha256": file_digest(path), "media_type": "application/json"}
        return _unverified_result(command, outputs={"contract_sha256": stored_lock_sha,
            "behavior_count": len(contract.get("behaviors", contract.get("entries", [])))
                if isinstance(contract.get("behaviors", contract.get("entries", [])), list) else 0,
            "locked_artifacts": {"contract_lock_sha256": stored_lock_sha},
            "artifact_refs": refs,
            **({'inherited_harness_binding': stored['inherited_harness_binding']}
               if isinstance(stored, Mapping) and 'inherited_harness_binding' in stored else {})},
            diagnostics=diagnostics,
            detail="available functional contract inputs frozen with unverified acceptance")


def _inherited_contract_identity_observation(command: OperationInput) -> dict[str, Any]:
    """Compare a fresh candidate with the selected handoff identities, not its bytes."""
    from .evidence import verified_path

    root = Path(command.run_dir)
    selected_ref = command.artifact_refs.get(
        'inherited_harness:.modport/functional-contract.json')
    candidate_path = root / 'baseline' / '.modport' / 'functional-contract.json'
    try:
        if not isinstance(selected_ref, Mapping):
            raise ValueError('selected contract reference is missing')
        selected_path = verified_path(root, selected_ref)
        selected_digest = selected_ref.get('sha256')
        if (not isinstance(selected_digest, str)
                or file_digest(selected_path) != selected_digest):
            raise ValueError('selected contract reference digest differs from its artifact')
        if (candidate_path.is_symlink() or not candidate_path.is_file()
                or candidate_path.resolve() != candidate_path.absolute()):
            raise ValueError('candidate contract is missing or unsafe')
        selected = json.loads(selected_path.read_text(encoding='utf-8'))
        candidate = json.loads(candidate_path.read_text(encoding='utf-8'))
        manifest_ref = command.artifact_refs['inherited_harness']
        if not isinstance(manifest_ref, Mapping):
            raise ValueError('inherited harness manifest reference is malformed')
        manifest_path = verified_path(root, manifest_ref)
        if file_digest(manifest_path) != manifest_ref.get('sha256'):
            raise ValueError('inherited harness manifest digest differs from its reference')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        source = json.loads((root / 'artifacts' / 'source.json').read_text(encoding='utf-8'))
        if not isinstance(selected, dict) or not isinstance(candidate, dict):
            raise ValueError('selected and candidate contracts must be JSON objects')
        if not isinstance(manifest, dict) or not isinstance(source, dict):
            raise ValueError('inherited manifest and source record must be JSON objects')
        files = manifest.get('files')
        if not isinstance(files, list) or not any(
                isinstance(item, dict)
                and item.get('path') == '.modport/functional-contract.json'
                and isinstance(item.get('ref'), dict)
                and item['ref'].get('path') == selected_ref.get('path')
                and item['ref'].get('sha256') == selected_digest
                for item in files):
            raise ValueError('selected contract reference is absent from inherited manifest')

        selected_contract = CharacterizationContract.from_mapping(selected)
        candidate_contract = CharacterizationContract.from_mapping(candidate)
        selected_id, selected_ids = selected_contract.contract_id, sorted(selected_contract.entry_ids)
        candidate_id, candidate_ids = candidate_contract.contract_id, sorted(candidate_contract.entry_ids)
        source_commit = source.get('source_commit')
        selected_source = manifest.get('source_commit')
        candidate_source = candidate_contract.source_fingerprint
        missing = sorted(set(selected_ids) - set(candidate_ids))
        added = sorted(set(candidate_ids) - set(selected_ids))
        status = ('preserved' if selected_id == candidate_id and not missing
                  and selected_source == source_commit == candidate_source
                  else 'changed')
        return {
            'status': status,
            'selected_contract_id': selected_id,
            'candidate_contract_id': candidate_id,
            'selected_behavior_ids': selected_ids,
            'candidate_behavior_ids': candidate_ids,
            'missing_behavior_ids': missing,
            'added_behavior_ids': added,
            'selected_source_commit': selected_source,
            'current_source_commit': source_commit,
            'candidate_source_fingerprint': candidate_source,
            'selected_artifact_sha256': selected_digest,
            'candidate_sha256': file_digest(candidate_path),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        return {'status': 'unavailable', 'diagnostic': str(exc)}


class BaselineContractVerificationHandler:
    """Execute Agent-declared Gradle characterization tasks deterministically."""

    def __init__(self, *, baseline: bool = True, cache_name: str | None = None,
                 require_client_evidence: bool = False) -> None:
        self.baseline = baseline
        self.cache_name = cache_name or ("baseline-contract-gradle-cache" if baseline else "target-contract-gradle-cache")
        self.require_client_evidence = require_client_evidence

    def __call__(self, command: OperationInput) -> OperationResult:
        if self.baseline and command.options.get('workflow_version', 0) >= 34:
            return _result(command, 'failed', error_code='source_harness_disabled_by_policy',
                detail='Source behavior is established by code reading; source runtime execution is disabled.',
                outputs={'process_executed': False, 'source_runtime_tested': False})
        if (command.options.get('workflow_version', 0) >= 29
                and (not self.baseline or command.options.get('workflow_version', 0) < 31)
                and compile_package_scope(command)):
            from .repair_context import observe_candidate
            root = _run_root(command)
            workspace = project_path(root, 'baseline' if self.baseline else 'worktree')
            candidate_id = observe_candidate(workspace)
            diagnostics = ['behavior runtime characterization was deferred for compile/package scope']
            if self.baseline:
                try:
                    contract = json.loads((workspace / '.modport' / 'functional-contract.json').read_text(encoding='utf-8'))
                    source = json.loads((root / 'artifacts' / 'source.json').read_text(encoding='utf-8'))
                    _validated_characterization_contract(contract, workflow_version=29)
                    _resolve_assertion_source_anchors(
                        contract, workspace, str(source.get('source_commit', '')),
                    )
                except (_CharacterizationShapeError, OSError, TypeError, ValueError,
                        KeyError, UnicodeError, json.JSONDecodeError) as exc:
                    diagnostics.append(f'v29 assertion source anchors are unavailable or invalid: {exc}')
            return _unverified_result(command, outputs={
                'process_executed': False,
                'verification_scope': 'source_identity_only',
                'verification_binding': 'source_identity_only',
                'verification_candidate_id': candidate_id,
                'behavior_tests_status': 'deferred_by_user',
                'acceptance_status': 'unverified',
                'assertion_source_diagnostics': diagnostics[1:],
            }, diagnostics=diagnostics,
                detail='contract identity observed; behavior verification deferred')
        if (self.baseline and 28 <= command.options.get("workflow_version", 0) < 31
                and compile_package_scope(command)):
            from .repair_context import observe_candidate
            root = _run_root(command)
            inherited = "inherited_harness" in command.artifact_refs
            candidate_id = observe_candidate(root / "baseline",
                                             inherited_harness=inherited)
            observation = (_inherited_contract_identity_observation(command)
                           if inherited else None)
            if inherited and (candidate_id is None or observation["status"] != "preserved"):
                return _unverified_result(command, status="blocked", outputs={
                    "process_executed": False,
                    "verification_scope": "source_identity_only",
                    "verification_candidate_id": candidate_id,
                    "inherited_contract_identity": observation,
                }, diagnostics=["inherited source or behavior identity is unavailable or changed"],
                    detail="inherited harness identity cannot be bound to the selected source",
                    error_code="inherited_harness_identity_invalid")
            return _unverified_result(command, outputs={
                "process_executed": False,
                "verification_scope": "source_identity_only",
                "verification_binding": "source_identity_only",
                "verification_candidate_id": candidate_id,
                "behavior_tests_status": "deferred_by_user",
                **({"inherited_contract_identity": observation} if observation is not None else {}),
            }, diagnostics=(["inherited contract identity: " + observation["status"]]
                            if observation is not None and observation["status"] != "preserved" else []),
                detail="baseline behavior tests deferred; source contract identity observed")
        from .repair_context import (enabled, observe_candidate, attach_inventory,
                                     inherited_harness_candidate_mode)
        if enabled(command) and self.baseline:
            workspace = _run_root(command) / 'baseline'
            inherited_harness = inherited_harness_candidate_mode(command, workspace)
            before = observe_candidate(workspace, inherited_harness=inherited_harness)
            result = self._execute(command)
            result = attach_inventory(command, result, workspace, before_candidate=before,
                                      bind_verification=True,
                                      inherited_harness=inherited_harness)
        else:
            result = self._execute(command)
        if (self.baseline and command.options.get('workflow_version', 0) >= 25
                and 'inherited_harness' in command.artifact_refs):
            observation = _inherited_contract_identity_observation(command)
            outputs = dict(result.outputs)
            outputs['inherited_contract_identity'] = observation
            if observation['status'] != 'preserved':
                diagnostics = list(outputs.get('business_diagnostics', []))
                diagnostics.append('inherited contract identity observation: '
                                   + observation['status'])
                outputs['business_diagnostics'] = diagnostics
                outputs['acceptance_status'] = 'unverified'
            return replace(result, outputs=outputs)
        return result

    def _execute(self, command: OperationInput) -> OperationResult:
        root = _run_root(command)
        gates_disabled = business_gates_disabled(command)
        workflow_version = command.options.get("workflow_version", 0)
        v29_contract_valid = True
        business_diagnostics: list[str] = []
        phase = "baseline" if self.baseline else "target"
        worktree = project_path(root, "baseline" if self.baseline else "worktree")
        log_path = root / "logs" / f"{phase}-contract-tests.log"
        report_path = root / "artifacts" / f"{phase}-contract-tests.json"
        frozen_review: Mapping[str, Any] = {}
        target_selection = None
        target_source_contract: Mapping[str, Any] = {}
        if command.options.get("workflow_version", 0) >= 26:
            execution_dir = root / "artifacts" / "executions" / command.command_id
            execution_dir.mkdir(parents=True, exist_ok=True)
            log_path = execution_dir / f"{phase}-contract-tests.log"
            report_path = execution_dir / f"{phase}-contract-tests.json"
        try:
            rubric = _acceptance_rubric_for(command, root)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            if not gates_disabled:
                return _result(command, "failed", detail=f"acceptance rubric is invalid: {exc}", error_code="acceptance_rubric_invalid")
            from .rubric import acceptance_rubric
            rubric = acceptance_rubric()
            business_diagnostics.append(f"acceptance rubric observation: {exc}")
        source = root / "baseline" / ".modport" / "functional-contract.json"
        try:
            if self.baseline:
                contract = json.loads(source.read_text(encoding="utf-8"))
                candidate = None
            else:
                try:
                    _verify_locked_artifacts(root, command.payload.get("locked_artifacts"), rubric=rubric,
                        contract_ref=(command.artifact_refs.get("functional_contract_lock")
                                      if gates_disabled else None))
                except (OSError, TypeError, ValueError, KeyError) as exc:
                    if not gates_disabled:
                        raise
                    business_diagnostics.append(f"locked artifact observation: {exc}")
                lock_ref = (command.artifact_refs.get("functional_contract_lock")
                            if gates_disabled else None)
                lock_path = (_resolve_artifact_ref(root, lock_ref, "functional_contract_lock")[0]
                             if isinstance(lock_ref, Mapping)
                             else root / "artifacts" / "functional-contract.lock.json")
                locked = json.loads(lock_path.read_text(encoding="utf-8"))
                frozen_review = (locked.get("review", {})
                                 if isinstance(locked.get("review", {}), Mapping) else {})
                contract = {
                    **locked["contract"], **locked["acceptance_rubric"],
                    **{key: locked[key] for key in ("baseline_gradle_tasks", "baseline_evidence_files", "test_evidence")},
                }
                candidate = None
        except (OSError, TypeError, ValueError, KeyError) as exc:
            return _result(command, "failed", detail=f"characterization input is invalid: {exc}", error_code="contract_missing" if self.baseline else "locked_artifact_invalid")
        if not isinstance(contract, dict):
            return _result(command, "failed", detail="functional contract must be a JSON object", error_code="contract_invalid")
        if self.baseline and command.options.get("workflow_version", 0) >= 13:
            try:
                source_record = json.loads((root / "artifacts" / "source.json").read_text(encoding="utf-8"))
                contract = dict(contract)
                contract.setdefault("source_fingerprint", source_record["source_commit"])
                for key in ("rubric_id", "rubric_version", "rubric_sha256"):
                    contract.setdefault(key, rubric.get(key))
                _validated_characterization_contract(contract, workflow_version=workflow_version)
                v29_contract_valid = workflow_version >= 29
            except _CharacterizationShapeError as exc:
                if workflow_version >= 29:
                    v29_contract_valid = False
                if not gates_disabled:
                    return _result(command, "failed", detail=str(exc), error_code=exc.code)
                business_diagnostics.append(str(exc))
            except (OSError, TypeError, ValueError, KeyError) as exc:
                if workflow_version >= 29:
                    v29_contract_valid = False
                if not gates_disabled:
                    return _result(command, "failed", detail=f"functional contract input is invalid: {exc}", error_code="contract_invalid")
                business_diagnostics.append(f"functional contract observation: {exc}")
        try:
            tasks = validate_baseline_gradle_tasks(contract.get("baseline_gradle_tasks"))
        except ValueError as exc:
            return _result(command, "failed", detail=str(exc), error_code=f"{phase}_tasks_invalid")
        evidence_files = contract.get("baseline_evidence_files")
        if not isinstance(evidence_files, list) or not evidence_files:
            if workflow_version >= 29:
                v29_contract_valid = False
            if not gates_disabled:
                return _result(command, "failed", detail="contract requires non-empty baseline_evidence_files", error_code=f"{phase}_evidence_invalid")
            evidence_files = []
            business_diagnostics.append("contract has no usable baseline_evidence_files")
        try:
            test_evidence = _test_evidence_declarations(
                contract, rubric, workflow_version=workflow_version, gradle_tasks=tasks,
            )
        except (TypeError, ValueError) as exc:
            if workflow_version >= 29:
                v29_contract_valid = False
            if not gates_disabled:
                return _result(command, "failed", detail=f"contract evidence schema is invalid: {exc}", error_code=f"{phase}_evidence_invalid")
            test_evidence = {}
            business_diagnostics.append(f"contract evidence observation: {exc}")
        if not self.baseline and workflow_version >= 31:
            if not test_evidence and business_diagnostics:
                return _unverified_result(command, status='failed',
                    detail='selected migration test declarations are invalid: '
                           + business_diagnostics[-1],
                    error_code='target_evidence_invalid',
                    outputs={'process_executed': False}, diagnostics=business_diagnostics)
            try:
                from .test_selection_execution import build_selected_test_execution
                target_selection = build_selected_test_execution(contract, list(test_evidence),
                    workflow_version=workflow_version)
                tasks = list(target_selection.gradle_tasks)
                target_source_contract = locked.get('source_contract', {})
            except (ValueError, TypeError, KeyError) as exc:
                return _unverified_result(command, status='failed',
                    detail='selected migration tests cannot execute: ' + str(exc),
                    error_code='target_selection_invalid',
                    outputs={'process_executed': False}, diagnostics=[str(exc)])
        client_tests = {test_id for test_id, item in test_evidence.items()
                        if item.get("evidence_kind") == "runtime" and item.get("executor") == "client_smoke"}
        if workflow_version >= 31:
            client_tests.update(
                test_id for behavior in contract.get("behaviors", [])
                if isinstance(behavior, Mapping) and behavior.get("side") in {"client", "both"}
                for test_id in behavior.get("test_mapping", [])
                if test_id in test_evidence
                and test_evidence[test_id].get("evidence_kind") == "runtime"
                and test_evidence[test_id].get("executor") == "junit"
            )
        if (self.require_client_evidence and not client_tests
                and (self.baseline or not compile_package_scope(command))):
            if not gates_disabled:
                return _result(command, "failed", detail="client gate requires a frozen executable client startup mapping",
                    error_code="client_mapping_missing")
            business_diagnostics.append("no executable client startup mapping was observed")
        source_record = json.loads((root / "artifacts" / "source.json").read_text(encoding="utf-8"))
        source_commit = str(source_record.get("source_commit", ""))
        if 29 <= workflow_version < 34:
            previously_valid = v29_contract_valid
            try:
                _validated_characterization_contract(contract, workflow_version=workflow_version)
                contract = _resolve_assertion_source_anchors(
                    contract, root / 'baseline', source_commit,
                )
                v29_contract_valid = previously_valid
            except _CharacterizationShapeError as exc:
                v29_contract_valid = False
                business_diagnostics.append(f"assertion validation observation: {exc}")
            except (OSError, TypeError, ValueError, KeyError, UnicodeError) as exc:
                v29_contract_valid = False
                business_diagnostics.append(f"assertion source anchor observation: {exc}")
        try:
            executor_provenance = _runtime_executor_provenance(
                worktree, test_evidence, rubric
            )
        except (OSError, TypeError, ValueError) as exc:
            if not gates_disabled:
                return _result(
                    command,
                    "failed",
                    detail=f"runtime test source provenance is invalid: {exc}",
                    error_code=f"{phase}_evidence_invalid",
                )
            executor_provenance = {}
            business_diagnostics.append(f"runtime test source provenance observation: {exc}")
        audit_refs: dict[str, Mapping[str, Any]] = {}
        try:
            for test_id, provenance in executor_provenance.items():
                for relative in provenance["test_source_files"]:
                    _, ref = _snapshot_stage_output(root, worktree, command, relative)
                    audit_refs[f"{phase}_executor_source:{test_id}:{relative}"] = ref
        except (OSError, ValueError) as exc:
            return _result(command, "failed", detail=str(exc), error_code=f"{phase}_evidence_invalid")
        evidence_root = worktree / ".modport" / "evidence"
        if evidence_root.is_symlink() or evidence_root.resolve() != evidence_root.absolute():
            return _result(command, "failed", detail="baseline evidence directory must not be a symlink", error_code=f"{phase}_evidence_invalid")
        resolved_evidence: dict[str, Path] = {}
        for test_id, declaration in test_evidence.items():
            relative = declaration["path"]
            path = worktree / relative
            if path.resolve() != path.absolute() or not path.resolve().is_relative_to(evidence_root.resolve()):
                return _result(command, "failed", detail="baseline evidence path escapes its directory", error_code=f"{phase}_evidence_invalid")
            if path.exists():
                path.unlink()
            resolved_evidence[test_id] = path
        timed_out = False
        environment = None
        workload_budget: dict[str, Any] | None = None
        wrapper_cache_diagnostics: list[dict[str, Any]] = []
        asset_cache_context = None
        asset_cache_diagnostics: dict[str, Any] = {"state": "not_configured"}
        v29_candidate_before: Mapping[str, Any] | None = None
        v29_junit_removed: list[str] = []
        v29_identity_error: str | None = None
        task_graph_output = ""

        def harvest_asset_cache() -> None:
            nonlocal asset_cache_context
            if asset_cache_diagnostics.get("state") == "skipped":
                return
            try:
                # A cold ForgeGradle home may create its version manifest only
                # during the full build. Capture verified partial downloads in
                # that case so the next Run can use them.
                if asset_cache_context is None:
                    asset_cache_context = _contract_asset_cache_context(
                        command, root, cache_name, baseline=self.baseline)
                if asset_cache_context is None:
                    return
                asset_cache, asset_paths = asset_cache_context
                harvest_diagnostics = asset_cache.harvest(**asset_paths)
                asset_cache_diagnostics["harvest"] = harvest_diagnostics
                asset_cache_diagnostics["state"] = (
                    "harvested" if harvest_diagnostics.get("index_verified")
                    else "index_unavailable")
            except TimeoutError:
                # Another Run may be publishing a large asset corpus. Cache
                # contention cannot change the completed Gradle result.
                asset_cache_diagnostics["state"] = "busy"
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                # Publication cannot replace the actual characterization
                # result, including a result that exhausted its deadline.
                asset_cache_diagnostics["harvest_error"] = type(exc).__name__

        try:
            cache_name = self.cache_name
            gradle = ["bash", "/workspace/gradlew", "--no-daemon"]
            if workflow_version < 34:
                gradle.append('--rerun-tasks')
            if self.baseline:
                gradle.extend(["--init-script", _forge_baseline_init(root, cache_name=cache_name)])
            from .harness_wiring import characterization_init_scripts
            wiring_directory = root / 'artifacts' / 'harness-wiring'
            for index, characterization_init in enumerate(characterization_init_scripts(
                    worktree, workflow_version=command.options.get('workflow_version', 0),
                    supplement_directory=wiring_directory)):
                host_owned = characterization_init.parent == wiring_directory
                origin = root if host_owned else worktree
                relative = characterization_init.relative_to(origin).as_posix()
                _, wiring_ref = _snapshot_stage_output(root, origin, command,
                    relative)
                alias = f'{phase}_harness_wiring' if index == 0 else f'{phase}_harness_wiring:{index}'
                audit_refs[alias] = wiring_ref
                sandbox_path = ('/modport-wiring/' + characterization_init.name if host_owned
                                else '/workspace/' + relative)
                gradle.extend(["--init-script", sandbox_path])
            if target_selection is not None:
                selector = wiring_directory / (command.command_id + '-selected-tests.init.gradle')
                selector.parent.mkdir(parents=True, exist_ok=True)
                selector.write_text(target_selection.gradle_init_script, encoding='utf-8')
                gradle.extend(['--init-script', '/modport-wiring/' + selector.name])
                _, selector_ref = _snapshot_stage_output(root, root, command,
                    project_relative(root, selector).as_posix())
                audit_refs['target_test_selection_wiring'] = selector_ref
            artifact_init = command.options.get('artifact_init_script')
            if artifact_init is not None:
                script = Path(artifact_init)
                if script.parent != wiring_directory or script.is_symlink():
                    raise ValueError('unsafe delivered artifact Gradle wiring')
                gradle.extend(['--init-script', '/modport-wiring/' + script.name])
                _, artifact_wiring_ref = _snapshot_stage_output(root, root, command,
                    project_relative(root, script).as_posix())
                audit_refs['artifact_test_wiring'] = artifact_wiring_ref
            gradle.extend(tasks)
            if command.options.get('workflow_version', 0) >= 20:
                # Resolve the exact task graph before paying for a game/test
                # launch. Configuration remains untrusted project execution
                # and therefore uses the same credential-free sandbox.
                probe_wrapper_cache_info: dict[str, Any] = {}
                probe_args = _sandboxed_build_command(
                    root, worktree, [*gradle, '--dry-run'], cache_name=cache_name,
                    java_home=None if self.baseline else _locked_java_home(root),
                    operation=command,
                    wrapper_cache_info=probe_wrapper_cache_info,
                    environment=target_selection.environment if target_selection is not None else None)
                wrapper_cache_diagnostics.append(probe_wrapper_cache_info)
                probe_log = root / 'logs' / f'{phase}-entry-probe-{command.command_id}.log'
                try:
                    if command.options.get('workflow_version', 0) >= 25:
                        # Leave time for redacted diagnostic capture and SDK settlement
                        # when the Run or execution budget is tighter than this probe.
                        allowance = _remaining_timeout(command, 125)
                        if allowance <= 5:
                            return _result(command, 'failed', error_code='budget_exhausted',
                                detail='No execution time remains for the task-entry probe and its diagnostic capture.',
                                outputs={'process_executed': False, 'harness_executed': False,
                                         'entry_probe_executed': False,
                                         'acceptance_status': 'unverified',
                                         'artifact_refs': audit_refs})
                        probe_timeout = min(120, allowance - 5)
                    else:
                        probe_timeout = _remaining_timeout(command, 120)
                    probe = _exec(probe_args, cwd=root, log=probe_log,
                                  timeout=probe_timeout)
                except subprocess.TimeoutExpired:
                    outputs: dict[str, Any] = {
                        'process_executed': True, 'harness_executed': False,
                        'entry_probe_executed': True, 'acceptance_status': 'unverified',
                        **_wrapper_cache_diagnostic(command, wrapper_cache_diagnostics),
                        'artifact_refs': audit_refs,
                    }
                    if command.options.get('workflow_version', 0) >= 25:
                        try:
                            audit_refs[f'{phase}_entry_probe'] = _bounded_entry_probe_log(command, probe_log)
                            outputs['probe_log'] = project_relative(root, probe_log).as_posix()
                        except (OSError, TimeoutError, ValueError) as error:
                            outputs['probe_log_capture_error'] = type(error).__name__
                            if (not probe_log.parent.is_symlink()
                                    and probe_log.parent.resolve() == (root / 'logs').absolute()
                                    and probe_log.is_file() and not probe_log.is_symlink()):
                                outputs['probe_log'] = project_relative(root, probe_log).as_posix()
                    return _result(command, 'failed', error_code='harness_entry_probe_timeout',
                        detail='The bounded task-entry probe timed out; the full harness was not launched.',
                        outputs=outputs)
                from .development import _artifact
                audit_refs[f'{phase}_entry_probe'] = _artifact(
                    command, 'harness-entry-probe.log', probe.stdout.encode('utf-8'))
                if probe.returncode:
                    return _result(command, 'failed', error_code='harness_entry_probe_failed',
                        detail='Task-entry resolution failed; the full harness was not launched.',
                        outputs={'process_executed': True, 'harness_executed': False,
                                 'entry_probe_executed': True,
                                 'probe_returncode': probe.returncode,
                                 **_wrapper_cache_diagnostic(command, wrapper_cache_diagnostics),
                                 'acceptance_status': 'unverified', 'artifact_refs': audit_refs})
                task_graph_output = probe.stdout
            if command.options.get("workflow_version", 0) >= 26:
                try:
                    # The entry probe has exited; no Gradle process owns this
                    # private cache until the full build starts below.
                    asset_cache_context = _contract_asset_cache_context(
                        command, root, cache_name, baseline=self.baseline)
                    if asset_cache_context is not None:
                        asset_cache, asset_paths = asset_cache_context
                        try:
                            seed_diagnostics = asset_cache.seed(**asset_paths)
                        except TimeoutError:
                            # A busy shared store is a cache miss, not a Run
                            # deadline failure; Gradle can fetch missing data.
                            asset_cache_diagnostics = {"state": "busy"}
                        else:
                            asset_cache_diagnostics = {
                                "state": ("seeded" if seed_diagnostics.get("index_verified")
                                          else "index_unavailable"),
                                "seed": seed_diagnostics,
                            }
                except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                    # Shared assets only accelerate Gradle. An unavailable seed
                    # must not prevent the actual harness from running, and it
                    # must not be retried when harvesting this execution.
                    asset_cache_context = None
                    asset_cache_diagnostics = {
                        "state": "skipped", "reason": "cache_unavailable",
                        "error": type(exc).__name__, "detail": redact(str(exc)),
                    }
            execution_nonce = sha256(
                f"{command.command_id}:{time.time_ns()}:{os.getpid()}".encode("utf-8")
            ).hexdigest()
            timeout = _remaining_timeout(command, 900 if command.stage_id == "client_smoke" else 7200)
            from .client_harness import requires_client_display
            client_launch = bool(client_tests) or requires_client_display(
                contract, tasks, task_graph_output,
            )
            launcher_timeout = max(0.01, timeout - 5) if client_launch else None
            workload_budget = {"outer_seconds": timeout, "launcher_seconds": launcher_timeout}
            if command.options.get("workflow_version", 0) >= 26:
                from .execution_budget import current_deadline_budget
                active_budget = current_deadline_budget(command)
                if active_budget is not None:
                    workload_budget.update({
                        "run_deadline_epoch": active_budget.run_deadline,
                        "stage_deadline_epoch": active_budget.stage_deadline,
                        "work_deadline_epoch": active_budget.work_deadline,
                        "settlement_reserve_seconds": active_budget.settlement_reserve,
                        "nominal_deadline_source": "run" if (active_budget.run_deadline is not None
                            and active_budget.run_deadline <= active_budget.stage_deadline) else "sdk_stage",
                    })
                if client_launch and launcher_timeout < MIN_CLIENT_LAUNCH_SECONDS:
                    return _result(command, "failed", error_code="budget_exhausted",
                        detail="No meaningful client workload window remains after the task-entry probe.",
                        outputs={"process_executed": True, "harness_executed": False,
                                 "entry_probe_executed": True, "acceptance_status": "unverified",
                                 **_wrapper_cache_diagnostic(command, wrapper_cache_diagnostics),
                                 "workload_budget": workload_budget, "artifact_refs": audit_refs})
            if client_launch:
                gradle = _client_launch_arguments(root, command, gradle, timeout=launcher_timeout)
            if workflow_version >= 29:
                try:
                    from .opencode_shell_mcp import (_clear_junit_results,
                                                     _workspace_candidate_identity)
                    v29_junit_removed = _clear_junit_results(
                        worktree, validated_tasks=tasks,
                    )
                    if workflow_version < 34:
                        v29_candidate_before = _workspace_candidate_identity(worktree)
                except (OSError, RuntimeError, ValueError, TypeError) as exc:
                    label = 'candidate or test-result identity' if workflow_version < 34 else 'fresh test results'
                    v29_identity_error = f"{label} unavailable: {exc}"
            build_wrapper_cache_info: dict[str, Any] = {}
            args = _sandboxed_build_command(
                root,
                worktree,
                gradle,
                cache_name=cache_name,
                java_home=None if self.baseline else _locked_java_home(root),
                operation=command,
                wrapper_cache_info=build_wrapper_cache_info,
                environment={
                    **(target_selection.environment if target_selection is not None else {}),
                    "MODPORT_EXECUTION_ID": command.command_id,
                    "MODPORT_EVIDENCE_NONCE": execution_nonce,
                    "MODPORT_EXECUTOR_FINGERPRINTS": canonical_json(
                        {
                            test_id: item["executor_fingerprint"]
                            for test_id, item in executor_provenance.items()
                        }
                    ),
                },
            )
            wrapper_cache_diagnostics.append(build_wrapper_cache_info)
            completed = _exec(args, cwd=root, log=log_path, timeout=timeout)
        except TimeoutError:
            return _result(command, "failed", detail="run wall-clock budget exhausted", error_code="budget_exhausted")
        except ValueError as exc:
            return _result(command, "blocked", detail=str(exc), error_code="harness_support_invalid")
        except RuntimeError as exc:
            return _result(command, "failed", detail=str(exc), error_code="build_sandbox_unavailable")
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            captured = "".join(value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
                               for value in (exc.stdout, exc.stderr))
            completed = subprocess.CompletedProcess(args, 124, captured)
        if command.options.get("workflow_version", 0) >= 26:
            harvest_asset_cache()
        file_hashes: dict[str, str] = {}
        record_summaries: dict[str, Mapping[str, Any]] = {}
        record_errors: list[str] = []
        excluded_executions = []
        if target_selection is not None:
            try:
                from .test_matrix_runtime import executed_excluded_cases
                excluded_executions = executed_excluded_cases(worktree, target_source_contract,
                    list(target_selection.test_ids))
                if excluded_executions:
                    record_errors.append('target executed cases excluded from the migration suite: '
                                         + ', '.join(row['test_id'] for row in excluded_executions))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                record_errors.append('target selection result audit failed: ' + str(exc))
        v29_case_results: dict[str, Any] = {}
        v29_assertion_results: dict[str, Any] = {}
        v29_candidate_after: Mapping[str, Any] | None = None
        v29_candidate_unchanged = None if workflow_version >= 34 else False
        if workflow_version >= 29 and v29_identity_error:
            v29_contract_valid = False
            record_errors.append(v29_identity_error)
        from .client_harness import parse_environment_report
        try:
            environment = parse_environment_report(completed.stdout, execution_id=command.command_id)
        except ValueError as exc:
            record_errors.append(f"client environment diagnostics invalid: {exc}")
        try:
            # The harness runs in the isolated VM and the host controls its
            # writable mounts.  Recomputing a whole source-tree digest after
            # execution only turns normal concurrent progress into a failure;
            # the actual runtime records and JUnit/process results below are
            # the acceptance checks.
            _runtime_executor_provenance(worktree, test_evidence, rubric)
        except (OSError, TypeError, ValueError) as exc:
            record_errors.append(f"runtime test source revalidation failed: {exc}")
        if not self.baseline:
            try:
                _verify_locked_artifacts(root, command.payload.get("locked_artifacts"), rubric=rubric,
                    contract_ref=(command.artifact_refs.get("functional_contract_lock")
                                  if gates_disabled else None))
            except (OSError, TypeError, ValueError, KeyError) as exc:
                record_errors.append(f"target verification inputs are invalid: {exc}")
        if workflow_version >= 29:
            if not v29_contract_valid:
                record_errors.append('v29 source-anchor or assertion identity is invalid')
            reviewed_target = (
                not self.baseline
                and (workflow_version >= 31 or frozen_review.get('verdict') == 'approved')
                and (workflow_version >= 34 or not _assertion_review_diagnostics(contract, frozen_review))
            )
            (v29_case_results, v29_assertion_results, junit_errors, junit_refs,
             v29_candidate_unchanged, v29_candidate_after) = _collect_v29_case_results(
                root=root, workspace=worktree, command=command, contract=contract,
                declarations=test_evidence, executor_provenance=executor_provenance,
                source_commit=source_commit, candidate_before=v29_candidate_before,
                execution_nonce=execution_nonce, contract_valid=v29_contract_valid,
                reviewed_target=reviewed_target, wiring_refs=audit_refs, phase=phase,
            )
            record_errors.extend(junit_errors)
            audit_refs.update(junit_refs)
        output_lines = {line.strip() for line in completed.stdout.splitlines()}
        for test_id, path in resolved_evidence.items():
            evidence_root_safe = (
                not evidence_root.is_symlink()
                and evidence_root.is_dir()
                and evidence_root.resolve() == evidence_root.absolute()
            )
            path_safe = (
                evidence_root_safe
                and not path.is_symlink()
                and path.is_file()
                and path.resolve().is_relative_to(evidence_root.resolve())
                and path.resolve() == path.absolute()
            )
            if not path_safe:
                record_errors.append(f"missing fresh evidence: {test_id}")
                continue
            relative = path.relative_to(worktree).as_posix()
            try:
                _, ref = _snapshot_stage_output(root, worktree, command, relative)
                audit_refs[f"{phase}_runtime_evidence:{test_id}"] = ref
                file_hashes[relative] = ref["sha256"]
                record = json.loads((root / ref["path"]).read_text(encoding="utf-8"))
                if not isinstance(record, Mapping):
                    raise ValueError("record is not an object")
                expected_marker = _validate_evidence_record(
                    test_id=test_id,
                    declaration=test_evidence[test_id],
                    record=record,
                    source_commit=source_commit,
                    execution_nonce=execution_nonce,
                    executor_fingerprint=executor_provenance.get(test_id, {}).get(
                        "executor_fingerprint"
                    ),
                    workflow_version=workflow_version,
                )
                if expected_marker is not None and not any(
                        line == expected_marker or
                        (line.startswith(expected_marker + " ") and len(line.split()) == 4)
                        for line in output_lines):
                    raise ValueError(
                        f"runtime evidence record {test_id!r} has no matching process-log witness"
                    )
                record_summaries[test_id] = {
                    "path": relative,
                    "sha256": file_hashes[relative],
                    "evidence_kind": record["evidence_kind"],
                    "executor": record["executor"],
                    "runtime_operations": record["runtime_operations"],
                    "executor_fingerprint": record.get("executor_fingerprint"),
                    "runtime_witness_count": len(record.get("runtime_witnesses", ())),
                }
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                record_errors.append(f"{test_id}: {exc}")
        evidence = {
            "source_commit": source_commit,
            "rubric_sha256": rubric["rubric_sha256"],
            "execution_nonce": execution_nonce,
            "executor_provenance": executor_provenance,
            "tasks": tasks,
            "exit_code": completed.returncode,
            "log_sha256": sha256(log_path.read_bytes()).hexdigest(),
            "evidence_files": file_hashes,
            "evidence_records": record_summaries,
            "record_errors": record_errors,
            **({'selected_test_ids': list(target_selection.test_ids),
                'executed_excluded_cases': excluded_executions} if target_selection is not None else {}),
            **({
                'case_results': v29_case_results,
                'assertion_results': v29_assertion_results,
                'candidate_before': v29_candidate_before,
                'candidate_after': v29_candidate_after,
                'candidate_unchanged': v29_candidate_unchanged,
                'verification_binding': 'host_observed_case_execution',
                'acceptance_status': 'unverified',
                'failure_category': (
                    'assertion_invalid' if any(row.get('category') == 'assertion_invalid'
                                               for row in v29_assertion_results.values())
                    else 'test_infrastructure' if any(row.get('category') == 'test_infrastructure'
                                                      for row in v29_assertion_results.values())
                    else 'mod_behavior' if any(row.get('category') == 'mod_behavior'
                                               for row in v29_assertion_results.values())
                    else 'unknown' if any(row.get('category') == 'unknown'
                                          for row in v29_assertion_results.values())
                    else 'none'
                ),
            } if workflow_version >= 29 else {}),
            "client_environment": environment,
            **({"asset_cache": asset_cache_diagnostics}
               if command.options.get("workflow_version", 0) >= 26 else {}),
            **({"workload_budget": workload_budget} if command.options.get("workflow_version", 0) >= 26 else {}),
        }
        selected = {':' + task.lstrip(':') for task in tasks}
        no_source_tasks = sorted({match.group(1) for match in re.finditer(
            r'(?m)^> Task (:[A-Za-z0-9_:.\-]+) NO-SOURCE\s*$', completed.stdout)
            if match.group(1) in selected})
        no_source = bool(no_source_tasks)
        evidence['no_source_tasks'] = no_source_tasks
        from .artifact_verification_policy import required_behavior_policy, assess_required_cases
        if required_behavior_policy(command):
            assessment = assess_required_cases(contract, {'outputs': {
                **evidence, 'process_executed': True,
            }})
            evidence['required_behavior_assessment'] = assessment
            record_errors.extend(assessment['gaps'])
        failed = bool(completed.returncode or no_source or record_errors or len(file_hashes) != len(resolved_evidence))
        if failed:
            from .diagnostics import classify_characterization_failure
            log_ref = {"path": project_relative(root, log_path).as_posix(),
                       "sha256": evidence["log_sha256"], "media_type": "text/plain",
                       "metadata": {"execution_id": command.command_id}}
            evidence["diagnostics"] = classify_characterization_failure(completed.stdout,
                exit_code=completed.returncode, timed_out=timed_out, phase=phase,
                record_errors=record_errors, evidence_records=record_summaries,
                executor_provenance=executor_provenance,
                execution_id=command.command_id, missing_tests=sorted(set(resolved_evidence) - set(record_summaries)),
                environment=environment,
                raw_log_refs=[log_ref],
                workload_budget=workload_budget if command.options.get("workflow_version", 0) >= 26 else None)
            timed_out = evidence["diagnostics"]["timed_out"]
            diagnostic_path = root / "artifacts" / "executions" / command.command_id / "characterization-diagnostic.json"
            _write_json(diagnostic_path, evidence["diagnostics"])
            audit_refs[phase + "_characterization_diagnostic"] = {
                "path": project_relative(root, diagnostic_path).as_posix(),
                "sha256": sha256(diagnostic_path.read_bytes()).hexdigest(), "media_type": "application/json"}
        _write_json(report_path, evidence)
        result_outputs = {
            **({'process_executed': True} if command.options.get('workflow_version', 0) >= 18 else {}),
            **evidence,
            **_wrapper_cache_diagnostic(command, wrapper_cache_diagnostics),
            "artifact_refs": {
                **audit_refs,
                f"{phase}_contract_tests_log": {
                    "path": project_relative(root, log_path).as_posix(),
                    "sha256": sha256(log_path.read_bytes()).hexdigest(),
                    "media_type": "text/plain",
                },
                f"{phase}_contract_tests_candidate": {
                    "path": project_relative(root, report_path).as_posix(),
                    "sha256": sha256(report_path.read_bytes()).hexdigest(),
                    "media_type": "application/json",
                }
            },
        }
        if business_diagnostics:
            result_outputs["business_diagnostics"] = business_diagnostics
        if failed:
            diagnosis = evidence["diagnostics"]
            if gates_disabled:
                error_code = f"{phase}_contract_timeout" if timed_out else f"{phase}_contract_failed"
                return _unverified_result(command, status="failed", outputs=result_outputs,
                    diagnostics=[f"{phase} characterization tests observed failure: {diagnosis['category']}/{diagnosis['error_code']}"],
                    detail=f"{phase} characterization tests executed; adverse observations retained",
                    error_code=error_code)
            return _result(command, "failed", outputs=result_outputs,
                detail=f"{phase} characterization tests failed: {diagnosis['category']}/{diagnosis['error_code']}",
                error_code=f"{phase}_contract_timeout" if timed_out else f"{phase}_contract_failed")
        return _result(
            command,
            "completed",
            outputs=result_outputs,
            detail=f"{phase} characterization tests passed",
        )

class DeliveryHandler:
    def __call__(self, command: OperationInput) -> OperationResult:
        if business_gates_disabled(command):
            return self._deliver_unverified(command)
        root = _run_root(command)
        worktree = project_path(root, "worktree")
        try:
            rubric = _acceptance_rubric_for(command, root)
            locked = _verify_locked_artifacts(
                root,
                command.payload.get("locked_artifacts"),
                rubric=rubric,
            )
        except (TypeError, ValueError, KeyError) as exc:
            return _result(command, "failed", detail=f"locked artifact verification failed: {exc}", error_code="locked_artifact_invalid")
        status = _exec(["git", "status", "--porcelain", "--untracked-files=all"], cwd=worktree, log=root / "logs" / "delivery-git-status.log", timeout=_remaining_timeout(command, 60))
        if status.returncode or status.stdout.strip():
            return _result(command, "failed", outputs={"tracked_changes": status.stdout.splitlines()}, detail="migration worktree has uncommitted tracked changes", error_code="worktree_dirty")
        source = json.loads((root / "artifacts" / "source.json").read_text(encoding="utf-8"))
        baseline_head = _exec(
            ["git", "rev-parse", "HEAD"],
            cwd=root / "baseline",
            log=root / "logs" / "delivery-baseline-head.log",
            timeout=_remaining_timeout(command, 60),
        )
        if (
            baseline_head.returncode
            or baseline_head.stdout.strip() != source["source_commit"]
        ):
            return _result(command, "failed", detail="baseline checkout no longer matches the immutable source commit", error_code="source_history_invalid")
        ancestor = _exec(["git", "merge-base", "--is-ancestor", source["source_commit"], "HEAD"], cwd=worktree, log=root / "logs" / "delivery-git-ancestor.log", timeout=_remaining_timeout(command, 60))
        if ancestor.returncode:
            return _result(command, "failed", detail="immutable source commit is not an ancestor of the delivered HEAD", error_code="source_history_invalid")
        commits = _exec(["git", "rev-list", "--count", f"{source['source_commit']}..HEAD"], cwd=worktree, log=root / "logs" / "delivery-git-history.log", timeout=_remaining_timeout(command, 60))
        if commits.returncode or int((commits.stdout.strip() or "0").splitlines()[-1]) < 1:
            return _result(command, "failed", detail="migration produced no committed source changes", error_code="migration_commit_missing")
        jar_files = sorted((project_path(root, "worktree") / "build" / "libs").glob("*.jar"))
        if not jar_files:
            return _result(command, "failed", detail="no deliverable jar found", error_code="jar_missing")
        report = {
            "status": "succeeded", "run_id": command.run_id,
            "jars": [{"path": str(project_relative(root, path)), "sha256": sha256(path.read_bytes()).hexdigest(), "size": path.stat().st_size} for path in jar_files],
            **locked,
            "manifest": "artifacts/locked-manifest.json", "events": "orchestrator.sqlite3",
        }
        agent_evidence = worktree / ".modport"
        if agent_evidence.exists():
            destination = root / "artifacts" / "agent-evidence"
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(agent_evidence, destination)
        _write_json(root / "artifacts" / "delivery-report.json", report)
        return _result(command, "completed", outputs=report, detail="migration artifacts delivered")

    def _deliver_unverified(self, command: OperationInput) -> OperationResult:
        """Export whatever v17 produced without converting observations into refusal."""
        root = _run_root(command)
        worktree = project_path(root, "worktree")
        diagnostics: list[str] = []
        locked: dict[str, Any] = {}
        try:
            rubric = _acceptance_rubric_for(command, root)
            locked = _verify_locked_artifacts(
                root, command.payload.get("locked_artifacts"), rubric=rubric,
                contract_ref=command.artifact_refs.get("functional_contract_lock"))
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            diagnostics.append(f"locked artifact observation: {exc}")

        status = _exec(["git", "status", "--porcelain", "--untracked-files=all"],
                       cwd=worktree, log=root / "logs" / "delivery-git-status.log",
                       timeout=_remaining_timeout(command, 60))
        tracked_changes = status.stdout.splitlines()
        if status.returncode:
            diagnostics.append(f"git status exited with {status.returncode}")
        if tracked_changes:
            diagnostics.append("migration worktree has uncommitted changes")
        target_commit = None
        target_clean = None
        if command.options.get("workflow_version", 0) >= 28 and compile_package_scope(command):
            target_head = _exec(["git", "rev-parse", "HEAD"], cwd=worktree,
                log=root / "logs" / "delivery-target-head.log",
                timeout=_remaining_timeout(command, 60))
            target_commit = target_head.stdout.strip() if target_head.returncode == 0 else None
            if not isinstance(target_commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", target_commit):
                target_commit = None
            # Agent review reports under .modport/ may remain untracked after
            # the build. The build receipt itself requires a clean candidate.
            project_changes = [row for row in tracked_changes
                               if not row.startswith("?? .modport/")]
            target_clean = status.returncode == 0 and not project_changes

        source: Mapping[str, Any] = {}
        try:
            loaded = json.loads((root / "artifacts" / "source.json").read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                source = loaded
            else:
                diagnostics.append("source record is not an object")
        except (OSError, ValueError, TypeError) as exc:
            diagnostics.append(f"source record unavailable: {exc}")
        source_commit = source.get("source_commit")
        baseline_head_valid = False
        source_ancestry_valid = False
        migration_commit_valid = False
        if isinstance(source_commit, str) and source_commit:
            baseline_head = _exec(["git", "rev-parse", "HEAD"], cwd=root / "baseline",
                log=root / "logs" / "delivery-baseline-head.log", timeout=_remaining_timeout(command, 60))
            if baseline_head.returncode or baseline_head.stdout.strip() != source_commit:
                diagnostics.append("baseline checkout does not match the recorded source commit")
            else:
                baseline_head_valid = True
            ancestor = _exec(["git", "merge-base", "--is-ancestor", source_commit, "HEAD"],
                cwd=worktree, log=root / "logs" / "delivery-git-ancestor.log",
                timeout=_remaining_timeout(command, 60))
            if ancestor.returncode:
                diagnostics.append("recorded source commit is not an ancestor of delivered HEAD")
            else:
                source_ancestry_valid = True
            commits = _exec(["git", "rev-list", "--count", f"{source_commit}..HEAD"],
                cwd=worktree, log=root / "logs" / "delivery-git-history.log",
                timeout=_remaining_timeout(command, 60))
            try:
                count = int((commits.stdout.strip() or "0").splitlines()[-1])
            except ValueError:
                count = 0
            if commits.returncode or count < 1:
                diagnostics.append("migration produced no committed source changes")
            else:
                migration_commit_valid = True

        jar_files = sorted((worktree / "build" / "libs").glob("*.jar"))
        if not jar_files:
            diagnostics.append("no deliverable jar found")
        jars = [{"path": str(project_relative(root, path)),
                 "sha256": sha256(path.read_bytes()).hexdigest(),
                 "size": path.stat().st_size} for path in jar_files
                if path.is_file() and not path.is_symlink()]
        required_checks: dict[str, Any] = {}
        if compile_package_scope(command):
            source_tests_deferred = (28 <= command.options.get("workflow_version", 0) < 31
                                     or command.options.get("workflow_version", 0) >= 34)
            if not source_tests_deferred:
                baseline_result = command.upstream_results.get("contract_verify", {})
                baseline_outputs = (baseline_result.get("outputs", {})
                                   if isinstance(baseline_result, Mapping) else {})
                expected_evidence = (baseline_outputs.get("executor_provenance", {})
                                     if isinstance(baseline_outputs, Mapping) else {})
                observed_evidence = (baseline_outputs.get("evidence_records", {})
                                     if isinstance(baseline_outputs, Mapping) else {})
                record_errors = (baseline_outputs.get("record_errors", [])
                                 if isinstance(baseline_outputs, Mapping) else [])
                no_source_tasks = (baseline_outputs.get("no_source_tasks", [])
                                   if isinstance(baseline_outputs, Mapping) else [])
                expected_ids = set(expected_evidence) if isinstance(expected_evidence, Mapping) else set()
                observed_ids = set(observed_evidence) if isinstance(observed_evidence, Mapping) else set()
                baseline_ok = bool(
                    isinstance(baseline_result, Mapping)
                    and baseline_result.get("status") == "completed"
                    and isinstance(baseline_outputs, Mapping)
                    and baseline_outputs.get("process_executed") is True
                    and baseline_outputs.get("exit_code") == 0
                    and expected_ids
                    and observed_ids == expected_ids
                    and not record_errors
                    and not no_source_tasks
                )
                required_checks["source_baseline_behavior_tests"] = {
                    "status": "passed" if baseline_ok else "failed",
                    "execution_id": baseline_result.get("command_id")
                        if isinstance(baseline_result, Mapping) else None,
                    "expected_behavior_tests": len(expected_ids),
                    "verified_behavior_tests": len(observed_ids),
                }
                if command.options.get('workflow_version', 0) >= 31:
                    from .test_matrix_runtime import baseline_selection_summary
                    try:
                        from .evidence import verified_path
                        ref = command.artifact_refs['functional_contract_lock']
                        path = verified_path(root, ref)
                        if file_digest(path) != ref.get('sha256'):
                            raise ValueError('selected contract digest changed')
                        selected_summary = baseline_selection_summary(json.loads(path.read_text(encoding='utf-8')))
                        selected_summary['execution_id'] = baseline_result.get('command_id')
                        required_checks['source_baseline_behavior_tests'] = selected_summary
                        baseline_ok = selected_summary['status'] == 'passed'
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        baseline_ok = False
                        required_checks['source_baseline_behavior_tests']['status'] = 'unverified'
                        diagnostics.append('selected baseline result unavailable: ' + str(exc))
                if not baseline_ok:
                    diagnostics.append(
                        "fresh original Forge baseline evidence does not prove every selected migration test passed"
                    )

            target_build = command.upstream_results.get("target_build", {})
            target_outputs = target_build.get("outputs", {}) if isinstance(target_build, Mapping) else {}
            build_tasks = target_outputs.get("build_gradle_tasks", []) if isinstance(target_outputs, Mapping) else []
            compile_ok = bool(
                isinstance(target_build, Mapping)
                and target_build.get("status") == "completed"
                and isinstance(target_outputs, Mapping)
                and target_outputs.get("build_status") == "completed"
                and target_outputs.get("build_executed") is True
                and isinstance(build_tasks, list)
                and "build" in build_tasks
            )
            required_checks["target_compile"] = {
                "status": "passed" if compile_ok else "failed",
                "build_execution_id": target_build.get("command_id") if isinstance(target_build, Mapping) else None,
            }
            if not compile_ok:
                diagnostics.append("fresh target build result does not prove an executed successful compile/package task")

            receipt_ok = False
            receipt_ref = (target_outputs.get("artifact_refs", {}).get("target_package_receipt")
                           if isinstance(target_outputs, Mapping)
                           and isinstance(target_outputs.get("artifact_refs"), Mapping) else None)
            receipt_value = target_outputs.get("package_receipt") if isinstance(target_outputs, Mapping) else None
            try:
                from .evidence import verified_path
                if not isinstance(receipt_ref, Mapping):
                    raise ValueError("package receipt reference is missing")
                receipt_path = verified_path(root, receipt_ref)
                stored_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if stored_receipt != receipt_value or not isinstance(stored_receipt, Mapping):
                    raise ValueError("package receipt does not match the sealed artifact")
                if (stored_receipt.get("status") != "passed"
                        or stored_receipt.get("build_execution_id") != target_build.get("command_id")):
                    raise ValueError("package receipt is not bound to the successful target build")
                if command.options.get("workflow_version", 0) >= 28:
                    candidate_id = target_outputs.get("verification_candidate_id")
                    if (target_commit is None or stored_receipt.get("target_commit") != target_commit
                            or stored_receipt.get("target_clean") is not True
                            or target_clean is not True
                            or not baseline_head_valid or not source_ancestry_valid
                            or not migration_commit_valid
                            or not isinstance(candidate_id, str) or not candidate_id
                            or target_outputs.get("verification_binding") != "host_observed_clean_candidate"):
                        raise ValueError("package is not bound to a clean committed migration candidate")
                artifacts = stored_receipt.get("artifacts")
                if not isinstance(artifacts, list) or not artifacts:
                    raise ValueError("package receipt contains no JARs")
                verified_artifacts = []
                for artifact in artifacts:
                    if not isinstance(artifact, Mapping):
                        continue
                    relative = Path(str(artifact.get("path", "")))
                    path = project_path(root, relative)
                    if (relative.is_absolute() or ".." in relative.parts
                            or relative.parts[:3] != ("worktree", "build", "libs")
                            or path.is_symlink() or not path.is_file()
                            or path.resolve(strict=True) != path.absolute()
                            or file_digest(path) != artifact.get("sha256")
                            or path.stat().st_size != artifact.get("size")):
                        continue
                    verified_artifacts.append(dict(artifact))
                receipt_ok = bool(verified_artifacts)
            except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                diagnostics.append(f"target package receipt verification failed: {exc}")
            required_checks["target_package"] = {
                "status": "passed" if receipt_ok else "failed",
                "verified_artifact_count": len(verified_artifacts) if receipt_ok else 0,
                "receipt_sha256": receipt_ref.get("sha256") if isinstance(receipt_ref, Mapping) else None,
                **({"target_commit": target_commit,
                    "target_clean": target_clean,
                    "build_candidate_id": target_outputs.get("verification_candidate_id")}
                   if command.options.get("workflow_version", 0) >= 28 else {}),
            }
            if not receipt_ok and not any("target package receipt verification failed" in item for item in diagnostics):
                diagnostics.append("fresh target package receipt does not match a regular delivered JAR")
        report: dict[str, Any] = {
            "status": "unverified", "acceptance_status": "unverified",
            "run_id": command.run_id, "jars": jars, "tracked_changes": tracked_changes,
            "business_diagnostics": diagnostics, **locked,
            "manifest": "artifacts/locked-manifest.json", "events": "orchestrator.sqlite3",
        }
        if compile_package_scope(command):
            report["required_checks"] = required_checks
            report["deferred_checks"] = [
                *(["source_baseline_behavior_tests"]
                  if 28 <= command.options.get("workflow_version", 0) < 31 else []),
                "target_gametests", "independent_behavior_tests",
                "client_smoke", "full_mod_behavior_equivalence",
            ]
        execution_dir = root / "artifacts" / "executions" / command.command_id
        report_path = execution_dir / "delivery-report.json"
        evidence_source = worktree / ".modport"
        evidence_destination = execution_dir / "agent-evidence"
        if evidence_source.is_dir() and not evidence_source.is_symlink():
            if evidence_destination.exists():
                diagnostics.append("existing delivery evidence snapshot retained")
            else:
                shutil.copytree(evidence_source, evidence_destination)
                report["agent_evidence"] = project_relative(root, evidence_destination).as_posix()
        _write_json(report_path, report)
        report["artifact_refs"] = {"delivery_report": {
            "path": project_relative(root, report_path).as_posix(),
            "sha256": file_digest(report_path), "media_type": "application/json"}}
        required_check_failures = [name for name, record in required_checks.items()
                                   if record.get("status") != "passed"]
        return _unverified_result(command,
            status="failed" if required_check_failures else "completed",
            outputs=report,
            diagnostics=(["required compile/package checks failed: " + ", ".join(required_check_failures)]
                         if required_check_failures else []),
            detail=("migration artifacts exported; required compile/package checks failed"
                    if required_check_failures else
                    "migration artifacts exported with compile/package evidence and unverified behavior acceptance"),
            error_code="target_validation_requirements_missing" if required_check_failures else None)


class ClientSmokeHandler(BaselineContractVerificationHandler):
    """Require fresh executable client evidence under the frozen contract."""

    def __init__(self) -> None:
        super().__init__(baseline=False, cache_name="acceptance-client-gradle-cache",
                         require_client_evidence=True)


class AcceptancePreflightHandler:

    def __call__(self, command: OperationInput) -> OperationResult:
        result = self._check(command)
        from .repair_context import enabled, attach_inventory
        if enabled(command):
            return attach_inventory(command, result, project_path(_run_root(command), 'worktree'))
        return result

    def _check(self, command: OperationInput) -> OperationResult:
        root = _run_root(command)
        worktree = project_path(root, "worktree")
        forbidden: list[str] = []
        forbidden_patterns = ("net.minecraftforge",)
        from .repair_context import enabled
        if enabled(command):
            from .repair_inventory import collect_inventory
            inventory = collect_inventory(worktree, execution_id=command.command_id)
            forbidden = sorted({loc['path'] for issue in inventory['issues']
                                if issue['rule_id'] == 'forge-package-reference'
                                for loc in issue['locations']})
        for path in (() if enabled(command) else worktree.rglob("*")):
            relative = path.relative_to(worktree)
            if not path.is_file() or any(part in {".git", ".gradle", ".modport", "build", "run", "runs"} for part in relative.parts):
                continue
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="replace").lower()
            if any(pattern in text for pattern in forbidden_patterns):
                forbidden.append(str(path.relative_to(worktree)))
        if forbidden:
            if business_gates_disabled(command):
                return _unverified_result(command, status="failed", outputs={"forbidden_imports": forbidden},
                    diagnostics=["Forge imports remain"],
                    detail="target preflight observations recorded", error_code="forbidden_dependency")
            return _result(command, "failed", outputs={"forbidden_imports": forbidden}, detail="Forge imports remain", error_code="forbidden_dependency")
        try:
            rubric = _acceptance_rubric_for(command, root)
            _verify_locked_artifacts(
                root,
                command.payload.get("locked_artifacts"),
                rubric=rubric,
                contract_ref=(command.artifact_refs.get("functional_contract_lock")
                              if business_gates_disabled(command) else None),
            )
        except (TypeError, ValueError, KeyError) as exc:
            if business_gates_disabled(command):
                return _unverified_result(command, status="failed",
                    diagnostics=[f"locked artifact observation: {exc}"],
                    detail="target preflight observations recorded", error_code="locked_artifact_invalid")
            return _result(command, "failed", detail=f"locked artifact verification failed: {exc}", error_code="locked_artifact_invalid")
        manifest_path = root / "artifacts" / "locked-manifest.json"
        manifest = LockedManifest.from_mapping(json.loads(manifest_path.read_text(encoding="utf-8"))) if manifest_path.is_file() else None
        if manifest is None:
            if business_gates_disabled(command):
                return _unverified_result(command, status="failed", diagnostics=["locked manifest is missing"],
                    detail="target preflight observations recorded", error_code="locked_artifact_invalid")
            return _result(command, "failed", detail="locked manifest is missing", error_code="locked_artifact_invalid")
        requirements = target_build_requirements(manifest)
        properties = worktree / requirements['properties_file']
        build_file = worktree / requirements['build_file']
        if not properties.is_file() or not build_file.is_file():
            if business_gates_disabled(command):
                return _unverified_result(command, status="failed", diagnostics=["target Gradle configuration is missing"],
                    detail="target preflight observations recorded", error_code="target_toolchain_mismatch")
            return _result(command, "failed", detail="target Gradle configuration is missing", error_code="target_toolchain_mismatch")
        property_text = properties.read_text(encoding="utf-8", errors="replace")
        build_text = build_file.read_text(encoding="utf-8", errors="replace")
        required_properties = requirements['properties']
        for key, value in required_properties.items():
            aliases = requirements.get('property_aliases', {}).get(key, [key])
            observed = [match.group(1).strip() for alias in aliases
                        for match in re.finditer(
                            rf"(?m)^\s*{re.escape(alias)}\s*=\s*([^\r\n]*)$", property_text)]
            if not observed or any(actual != value for actual in observed):
                if business_gates_disabled(command):
                    return _unverified_result(command, status="failed", diagnostics=[f"target {key} does not match locked manifest"],
                        detail="target preflight observations recorded", error_code="target_toolchain_mismatch")
                return _result(command, "failed", detail=f"target {key} does not match locked manifest", error_code="target_toolchain_mismatch")
        if re.search(rf"JavaLanguageVersion\s*\.\s*of\s*\(\s*{re.escape(requirements['java_version'])}\s*\)", build_text) is None:
            if business_gates_disabled(command):
                return _unverified_result(command, status="failed", diagnostics=["target Java toolchain does not match locked manifest"],
                    detail="target preflight observations recorded", error_code="target_toolchain_mismatch")
            return _result(command, "failed", detail="target Java toolchain does not match locked manifest", error_code="target_toolchain_mismatch")
        return _result(command, "completed", detail="target acceptance preflight passed")


@dataclass
class ArtifactVerifiedHandler:
    """Fail closed before an Operation consumes a changed context artifact."""

    handler: Callable[[OperationInput], OperationResult]

    def __call__(self, command: OperationInput) -> OperationResult:
        from .workflow import SOURCE_HARNESS_STAGES
        if command.options.get('workflow_version', 0) >= 34 and command.stage_id in SOURCE_HARNESS_STAGES:
            return _result(command, 'failed', error_code='source_harness_disabled_by_policy',
                detail='The current workflow reads source behavior and executes only target tests.',
                outputs={'process_executed': False, 'source_runtime_tested': False})
        root = _run_root(command)
        try:
            for artifact_id in command.artifact_refs:
                _read_artifact_ref(root, command, artifact_id)
        except (OSError, TypeError, ValueError) as exc:
            return _result(
                command,
                "failed",
                detail=f"command artifact verification failed: {exc}",
                error_code="command_artifact_invalid",
            )
        return self.handler(command)


@dataclass
class ReviewHandler:
    """One independent review, with no repair or hidden retry."""

    baseline: bool

    def __call__(self, command: OperationInput) -> OperationResult:
        result = self._review(command)
        if not self.baseline or command.options.get('workflow_version', 0) < 31:
            return result
        from .rework_tools import refresh_review_command
        from .test_matrix_runtime import seal_assessment
        command = refresh_review_command(command)
        try:
            ref = seal_assessment(command, result)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            return _unverified_result(command, status=result.status, outputs=result.outputs,
                detail=result.detail, error_code=result.error_code,
                diagnostics=['baseline test selection unavailable: ' + str(exc)])
        return replace(result, outputs={**result.outputs, 'artifact_refs': {
            **result.outputs.get('artifact_refs', {}), 'baseline_test_selection': ref}})

    def _review(self, command: OperationInput) -> OperationResult:
        root = _run_root(command)
        gates_disabled = business_gates_disabled(command)
        business_diagnostics: list[str] = []
        assess_baseline = self.baseline and command.options.get("workflow_version", 0) >= 31
        worktree = project_path(root, "baseline" if self.baseline else "worktree")
        relative = ".modport/contract-review.json" if self.baseline else ".modport/code-review.json"
        review_path = worktree / relative
        try:
            if (worktree.resolve() != worktree.absolute()
                    or review_path.parent.resolve() != review_path.parent.absolute()):
                raise ValueError("review workspace cannot contain parent symlinks")
        except (OSError, ValueError) as exc:
            return _result(command, "blocked", detail=str(exc),
                           error_code="review_workspace_invalid")
        review_path.unlink(missing_ok=True)
        try:
            rubric = _acceptance_rubric_for(command, root)
            if self.baseline:
                evidence = json.loads(_fresh_baseline_report(command, root).read_text())
                if evidence.get("exit_code") != 0 and not assess_baseline:
                    raise ValueError("baseline evidence is not a passing result for the active contract")
        except (OSError, ValueError, TypeError) as exc:
            if not gates_disabled:
                return _result(command, "failed", detail=str(exc), error_code="review_input_invalid")
            from .rubric import acceptance_rubric
            rubric = acceptance_rubric()
            business_diagnostics.append(str(exc))
        stage = "contract_review" if self.baseline else "code_review"
        review_prompt = STAGE_PROMPTS[stage]
        if self.baseline and command.options.get('workflow_version', 0) >= 29:
            review_prompt += (
                '\nWorkflow v29 semantic assertion review: inspect the exact original-source range '
                'named by each assertion, including neighboring statements needed to understand its '
                'meaning. Compare the assertion text with the value, units, comparison direction, '
                'state transition and conditions expressed by that source. A matching path and hash '
                'only identifies bytes; it does not show that the assertion interprets them correctly. '
                'For every assertion, include one assertion_reviews record with assertion_id, '
                'source_anchor {path,start_line,end_line}, status (supported/unsupported/ambiguous), '
                'and a concrete reasoning string describing what the source says and why the assertion '
                'matches or does not match. Do not approve any unsupported or ambiguous assertion. '
                'Return valid JSON containing verdict and the complete assertion_reviews array, then '
                'finish with the matching standalone MODPORT_DECISION line. Runtime results are reviewed '
                'separately; do not infer a pass from source review.'
            )
        agent = CodexStageHandler(review_prompt,
                                  baseline=self.baseline, required_paths=(relative, ".modport/test-assessment.json")
                                  if assess_baseline else (relative,))
        result = agent(command)
        from .rework_tools import refresh_review_command
        command = refresh_review_command(command)
        # Exclude the sole authorized review output before restoring the
        # baseline review file.  The isolated VM owns the workspace; content
        # changes are not treated as an external tamper signal.
        data = review_path.read_bytes() if review_path.is_file() and not review_path.is_symlink() else None
        review_path.unlink(missing_ok=True)
        try:
            if worktree.resolve() != worktree.absolute() or review_path.parent.resolve() != review_path.parent.absolute():
                raise ValueError("review workspace cannot contain parent symlinks")
        except (OSError, ValueError) as exc:
            return _result(command, "blocked", outputs=result.outputs,
                           detail=str(exc), error_code="review_workspace_invalid")
        if self.baseline and data is not None:
            review_path.write_bytes(data)
        if result.status != "completed":
            return result
        try:
            from .agent_reports import review_decision, report_findings
            verdict = review_decision(data.decode("utf-8") if data is not None else "")
            expected_id = "contract-review-agent" if self.baseline else "code-review-agent"
            verdict.update(reviewer_id=expected_id, review_id=command.command_id,
                           rubric_id=rubric['rubric_id'], rubric_version=rubric['rubric_version'])
            findings = report_findings(verdict)
            verdict['findings'] = findings
            verification = 'contract_verify' if self.baseline else 'target_build'
            fresh_result = command.upstream_results.get(verification, {})
            if verdict['verdict'] == 'approved':
                from .rework_tools import responses
                revisions = [response for response in responses(command)
                             if any(update.get('stage') in {'coder', 'contract_draft'}
                                    and update.get('result', {}).get('status') == 'completed'
                                    for update in response.get('updates', []))]
                if revisions and not any(update.get('stage') == verification
                        and (assess_baseline
                             or update.get('result', {}).get('status') == 'completed')
                        for update in revisions[-1].get('updates', [])):
                    raise ValueError('reviewed revision is missing fresh ' + verification)
            if (not assess_baseline and verdict['verdict'] == 'approved' and fresh_result.get('status')
                    and fresh_result['status'] != 'completed'):
                raise ValueError('reviewed revision has no passing ' + verification)
            if self.baseline:
                if verdict['verdict'] == 'approved':
                    fresh = json.loads(_fresh_baseline_report(command, root).read_text())
                    if fresh.get('exit_code') != 0 and not assess_baseline:
                        raise ValueError('revised contract has no passing baseline verification')
                contract = json.loads((worktree / ".modport/functional-contract.json").read_text())
                if contract.get("generator_id") == expected_id:
                    raise ValueError("reviewer must differ from generator")
                if command.options.get('workflow_version', 0) >= 29:
                    assertion_diagnostics = _assertion_review_diagnostics(contract, verdict)
                    if assertion_diagnostics:
                        if not gates_disabled:
                            raise ValueError('; '.join(assertion_diagnostics))
                        business_diagnostics.extend(assertion_diagnostics)
        except (ValueError, TypeError, OSError) as exc:
            if not gates_disabled:
                return _result(command, "failed", outputs=result.outputs, detail=str(exc), error_code="review_invalid")
            raw_report = data.decode("utf-8", errors="replace") if data is not None else ""
            return _unverified_result(command, outputs={**result.outputs,
                "raw_report": raw_report, "observed_verdict": None},
                diagnostics=[*business_diagnostics, str(exc)],
                detail="independent review report recorded without a validated decision")
        outputs = dict(result.outputs)
        refs = dict(outputs.get("artifact_refs", {}))
        snapshot = next((ref for key, ref in refs.items() if key.endswith(relative)), None)
        if snapshot is None:
            if not gates_disabled:
                return _result(command, "failed", outputs=result.outputs, detail="review snapshot missing", error_code="review_invalid")
            business_diagnostics.append("review snapshot missing")
        # Keep the author's original snapshot and store host routing metadata
        # separately, so downstream approval consumers need no report parser.
        from .evidence import atomic_json, file_digest
        decision_path = root / 'artifacts' / 'executions' / command.command_id / (stage + '-decision.json')
        atomic_json(decision_path, verdict)
        refs[stage] = {'path': project_relative(root, decision_path).as_posix(),
                       'sha256': file_digest(decision_path)}
        if self.baseline:
            atomic_json(review_path, verdict)
        outputs.update(verdict=verdict["verdict"], prior_findings=findings, artifact_refs=refs)
        if gates_disabled and (business_diagnostics or verdict["verdict"] != "approved"):
            rejected = verdict["verdict"] != "approved"
            return _unverified_result(command, outputs=outputs,
                status="failed" if rejected else "completed",
                diagnostics=[*business_diagnostics,
                    *( ["independent review observed verdict=" + verdict["verdict"]]
                       if verdict["verdict"] != "approved" else [])],
                detail="independent review observations recorded",
                error_code=(stage + "_rejected") if rejected else None)
        return _result(command, "completed", outputs=outputs, detail="independent review recorded")


def _require_code_review(root: Path, command: OperationInput) -> None:
    path, _ = _read_artifact_ref(root, command, "code_review")
    review = json.loads(path.read_text())
    rubric = _acceptance_rubric_for(command, root)
    if (review.get("verdict") != "approved" or review.get("reviewer_id") != "code-review-agent"
            or any(review.get(key) != rubric[key] for key in ("rubric_id", "rubric_version"))):
        raise ValueError("approved independent review is invalid")


@dataclass
class ApprovedCandidateHandler:
    handler: Callable[[OperationInput], OperationResult]

    def __call__(self, command: OperationInput) -> OperationResult:
        if business_gates_disabled(command):
            return self.handler(command)
        root = _run_root(command)
        try:
            _require_code_review(root, command)
        except (OSError, ValueError, TypeError) as exc:
            return _result(command, "failed", detail=str(exc), error_code="code_review_stale")
        result = self.handler(command)
        if result.status == "completed":
            try:
                _require_code_review(root, command)
            except (OSError, ValueError, TypeError) as exc:
                return _result(command, "failed", outputs=result.outputs, detail=str(exc), error_code="code_review_stale")
        return result


@dataclass
class BuildAndBehaviorHandler:
    """One build and one behavior verification; the application owns retries."""

    build: GradleHandler
    verification: BaselineContractVerificationHandler

    def __call__(self, command: OperationInput) -> OperationResult:
        from .repair_context import (enabled, observe_candidate, attach_inventory,
                                     inherited_harness_candidate_mode)
        if enabled(command):
            workspace = project_path(_run_root(command), 'baseline' if self.build.baseline else 'worktree')
            inherited_harness = inherited_harness_candidate_mode(command, workspace)
            before = observe_candidate(workspace, inherited_harness=inherited_harness)
            result = self._execute(command)
            return attach_inventory(command, result, workspace, before_candidate=before,
                                    bind_verification=True,
                                    inherited_harness=inherited_harness)
        return self._execute(command)

    def _execute(self, command: OperationInput) -> OperationResult:
        built = self.build(command)
        if compile_package_scope(command):
            outputs = {
                "build_status": built.status,
                "build_error_code": built.error_code,
                "build_executed": built.outputs.get("process_executed", False),
                "build_gradle_tasks": built.outputs.get("gradle_tasks", []),
                "excluded_gradle_tasks": built.outputs.get("excluded_gradle_tasks", []),
                "build_detail": built.detail,
                "verification_status": "deferred_by_user",
                "verification_executed": False,
                "acceptance_status": "unverified",
                "artifact_refs": dict(built.outputs.get("artifact_refs", {})),
            }
            if built.status != "completed":
                return _unverified_result(command, status=built.status, outputs=outputs,
                    diagnostics=[f"target compile/package command observed {built.status}: {built.detail}",
                                 "runtime and game behavior verification was deferred by user"],
                    detail="target compile/package did not complete; behavior verification deferred",
                    error_code=built.error_code)
            receipt, receipt_ref = _target_package_receipt(command)
            outputs["package_receipt"] = receipt
            outputs["artifact_refs"]["target_package_receipt"] = receipt_ref
            compile_passed = (built.outputs.get("process_executed") is True
                              and "build" in built.outputs.get("gradle_tasks", []))
            outputs["required_checks"] = {
                "target_compile": {"status": "passed" if compile_passed else "failed",
                                   "build_execution_id": command.command_id},
                "target_package": {"status": receipt["status"],
                                   "artifact_count": len(receipt["artifacts"]),
                                   "receipt_sha256": receipt_ref["sha256"]},
            }
            if not compile_passed or receipt["status"] != "passed":
                return _unverified_result(command, status="failed", outputs=outputs,
                    diagnostics=["required target compile or package evidence is incomplete",
                                 "runtime and game behavior verification was deferred by user"],
                    detail="target compile/package acceptance evidence is incomplete",
                    error_code="target_package_missing" if receipt["status"] != "passed"
                    else "target_compile_unverified")
            return _unverified_result(command, status="completed", outputs=outputs,
                diagnostics=["runtime and game behavior verification was deferred by user"],
                detail="target compile and package completed; behavior verification deferred")
        if built.status != "completed" and not business_gates_disabled(command):
            return built
        verified = self.verification(command)
        outputs = {**built.outputs, **verified.outputs}
        outputs["artifact_refs"] = {**built.outputs.get("artifact_refs", {}),
                                    **verified.outputs.get("artifact_refs", {})}
        outputs.update(build_status=built.status, build_error_code=built.error_code,
                       verification_status=verified.status,
                       verification_error_code=verified.error_code)
        if command.options.get('workflow_version', 0) >= 18:
            outputs.update(build_executed=built.outputs.get('process_executed', False),
                build_gradle_tasks=built.outputs.get('gradle_tasks', []),
                verification_executed=verified.outputs.get('process_executed', False),
                build_detail=built.detail, verification_detail=verified.detail,
                build_failure_signature=built.outputs.get('failure_signature'),
                verification_failure_signature=(verified.outputs.get('failure_signature')
                    or verified.outputs.get('diagnostics', {}).get('failure_signature')))
        if business_gates_disabled(command) and (built.status != "completed" or verified.status != "completed"):
            failed = verified if verified.status != "completed" else built
            return _unverified_result(command, status=failed.status, outputs=outputs, diagnostics=[
                f"build observed {built.status}: {built.detail}",
                f"behavior verification observed {verified.status}: {verified.detail}",
            ], detail=("build and behavior checks returned; per-check execution observations retained"
                       if command.options.get('workflow_version', 0) >= 18 else
                       "build and behavior verification both executed; adverse observations retained"),
                error_code=failed.error_code)
        return _result(command, verified.status, outputs=outputs,
                       detail=verified.detail, error_code=verified.error_code)


def build_registry() -> dict[str, Callable[[OperationInput], OperationResult]]:
    handlers: dict[str, Callable[[OperationInput], OperationResult]] = {
        "source": ValidateInputHandler(),
        "environment": LockEnvironmentHandler(),
        "baseline_build": GradleHandler(baseline=True, tasks=("clean", "build"), name="baseline-build"),
        "contract_draft": CodexStageHandler(STAGE_PROMPTS["contract_draft"], baseline=True, required_paths=(".modport/functional-contract.json",)),
        "contract_verify": BaselineContractVerificationHandler(require_client_evidence=True),
        "contract_review": ReviewHandler(baseline=True),
        "contract_freeze": FreezeContractHandler(),
        "contract_revise": CodexStageHandler(STAGE_PROMPTS["contract_revise"], baseline=True, required_paths=(".modport/functional-contract.json",)),
        "migration_plan": CodexStageHandler(STAGE_PROMPTS["migration_plan"], required_paths=(".modport/dependency-plan.md", ".modport/migration-plan.md", ".modport/development-plan.json")),
        "implementation": CodexStageHandler(STAGE_PROMPTS["implementation"]),
        "target_revise": CodexStageHandler(STAGE_PROMPTS["target_revise"]),
        "target_build": BuildAndBehaviorHandler(GradleHandler(baseline=False, tasks=("clean", "runData", "build", "runGameTestServer"), name="target-build"), BaselineContractVerificationHandler(baseline=False)),
        "code_review": ReviewHandler(baseline=False),
        "acceptance_preflight": ApprovedCandidateHandler(AcceptancePreflightHandler()),
        "acceptance_build": ApprovedCandidateHandler(BuildAndBehaviorHandler(GradleHandler(baseline=False, tasks=("clean", "runData", "build", "runGameTestServer"), name="acceptance-build", cache_name="acceptance-gradle-cache"), BaselineContractVerificationHandler(baseline=False, cache_name="acceptance-contract-gradle-cache"))),
        "client_smoke": ApprovedCandidateHandler(ClientSmokeHandler()),
        "delivery": ApprovedCandidateHandler(DeliveryHandler()),
        "supervisor": SupervisorHandler(),
    }
    from .analysis_stages import build_analysis_registry
    from .skill_runtime import build_skill_registry
    from .development import build_development_registry
    from .independent_tests import build_test_registry
    from .gap_research import GapResearchHandler, ResearchReviewHandler
    from .gap_planning import GapPlanHandler, GapPlanReviewHandler
    from .gap_review import GapApprovedDeliveryHandler, GapReviewHandler
    from .planning import build_planning_registry
    from .goal_planning import GoalPreparationHandler
    from .repair_execution import RepairPrepareHandler, RepairIntegrateHandler
    from .harness_snapshot import RestoreHarnessHandler
    handlers.update(build_analysis_registry())
    handlers.update(build_skill_registry())
    handlers.update(build_development_registry())
    handlers.update(build_planning_registry())
    from .deterministic_stages import BuildPrepareHandler, CodemodHandler, EarlyCompileHandler, VersionedInventoryHandler
    handlers['build_prepare'] = BuildPrepareHandler()
    handlers['codemod'] = CodemodHandler()
    handlers['early_compile'] = EarlyCompileHandler()
    handlers['migration_inventory'] = VersionedInventoryHandler(handlers['migration_inventory'])
    handlers["contract_restore"] = RestoreHarnessHandler()
    handlers["goal_prepare"] = GoalPreparationHandler()
    from .revival_planning import CoderRevivalPlannerHandler
    handlers['coder_revival_plan'] = CoderRevivalPlannerHandler()
    handlers["contract_revise"] = RepairPrepareHandler()
    handlers["target_revise"] = RepairPrepareHandler()
    handlers["contract_repair_integrate"] = RepairIntegrateHandler()
    handlers["target_repair_integrate"] = RepairIntegrateHandler()
    handlers.update({stage: ApprovedCandidateHandler(handler) for stage, handler in build_test_registry().items()})
    handlers["gap_research"] = GapResearchHandler()
    handlers["research_review"] = ResearchReviewHandler()
    handlers["admin_review"] = ResearchReviewHandler("admin_review")
    handlers["gap_plan"] = GapPlanHandler()
    handlers["gap_plan_review"] = GapPlanReviewHandler()
    handlers["gap_review"] = ApprovedCandidateHandler(GapReviewHandler())
    handlers["delivery"] = ApprovedCandidateHandler(GapApprovedDeliveryHandler(DeliveryHandler()))
    from .rework_effects import CoderReworkHandler, AuthorReworkHandler
    from .gate_policy import GateHandoffHandler
    handlers['agent_rework'] = CoderReworkHandler()
    handlers['gate_handoff'] = GateHandoffHandler()
    from .cleanup import CodeCleanupHandler, FinalCleanupHandler, ResearchCleanupHandler
    handlers["research_cleanup"] = ResearchCleanupHandler()
    handlers["code_cleanup"] = CodeCleanupHandler()
    handlers["final_cleanup"] = FinalCleanupHandler()
    from .artifact_verification import (ArtifactTestDesignHandler,
        ArtifactTestExecuteHandler, ArtifactTestReportHandler)
    handlers['artifact_test_design'] = ArtifactTestDesignHandler()
    handlers['artifact_test_execute'] = ArtifactTestExecuteHandler()
    handlers['artifact_test_report'] = ArtifactTestReportHandler()
    from .behavior_requirements import BehaviorExtractHandler, BehaviorReviewHandler, BehaviorFreezeHandler
    from .target_contract import TargetContractFreezeHandler
    handlers['behavior_extract'] = BehaviorExtractHandler()
    handlers['behavior_review'] = BehaviorReviewHandler()
    handlers['behavior_freeze'] = BehaviorFreezeHandler()
    handlers['target_contract_freeze'] = TargetContractFreezeHandler()
    return {"modport." + stage: ArtifactVerifiedHandler(AuthorReworkHandler(handler))
            for stage, handler in handlers.items()}
