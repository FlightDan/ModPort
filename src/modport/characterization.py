"""Functional characterization contracts and their review gate.

The contract is deliberately about observable behavior rather than a
particular mod or loader.  It can be produced from structured observations or
from a source-artifact inventory and reviewed by an independent actor.  The
host keeps schema and review identity separate from the mutable checkout so a
normal contract revision does not become a stale-hash failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, is_dataclass, asdict
from enum import Enum
import hashlib
import json
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping


CONTRACT_SCHEMA_VERSION = 1
FUNCTIONAL_CONTRACT_SCHEMA_VERSION = CONTRACT_SCHEMA_VERSION
REVIEW_SCHEMA_VERSION = 1


class ContractError(ValueError):
    """Base error for invalid characterization contracts."""


class ContractReviewError(ContractError):
    """Raised when a contract has not passed an independent review gate."""


class FrozenContractError(ContractError):
    """Raised when frozen behavior is changed, weakened, or waived."""


@dataclass(frozen=True, slots=True)
class SourceAnchor:
    """A line range in the immutable original source tree.

    Authors may provide only ``path`` and line bounds.  The verifier resolves
    the byte hashes and source commit before it publishes a passing receipt.
    """

    path: str
    start_line: int
    end_line: int
    source_commit: str | None = None
    source_file_sha256: str | None = None
    range_sha256: str | None = None

    def __post_init__(self) -> None:
        path = _non_empty(self.path, "source_anchor.path")
        parts = path.replace("\\", "/").split("/")
        if (path.startswith("/") or "\\" in path
                or any(part in {"", ".", "..", ".git"} for part in parts)):
            raise ValueError("source_anchor.path must be a contained original-source path")
        if (not isinstance(self.start_line, int) or isinstance(self.start_line, bool)
                or self.start_line < 1):
            raise ValueError("source_anchor.start_line must be a positive integer")
        if (not isinstance(self.end_line, int) or isinstance(self.end_line, bool)
                or self.end_line < self.start_line):
            raise ValueError("source_anchor.end_line must be an integer at or after start_line")
        object.__setattr__(self, "path", path)
        if self.source_commit is not None:
            commit = _non_empty(self.source_commit, "source_anchor.source_commit").lower()
            if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
                raise ValueError("source_anchor.source_commit must be a full Git object ID")
            object.__setattr__(self, "source_commit", commit)
        for name in ("source_file_sha256", "range_sha256"):
            value = getattr(self, name)
            if value is not None:
                value = _non_empty(value, f"source_anchor.{name}").lower()
                if not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise ValueError(f"source_anchor.{name} must be a SHA-256 digest")
                object.__setattr__(self, name, value)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
        }
        for name in ("source_commit", "source_file_sha256", "range_sha256"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SourceAnchor":
        if not isinstance(value, Mapping):
            raise TypeError("assertion source_anchor must be an object")
        return cls(path=value.get("path"), start_line=value.get("start_line"),
                   end_line=value.get("end_line"), source_commit=value.get("source_commit"),
                   source_file_sha256=value.get("source_file_sha256"),
                   range_sha256=value.get("range_sha256"))


@dataclass(frozen=True, slots=True)
class AssertionContract:
    """One falsifiable assertion, tied to original source and result IDs."""

    assertion_id: str
    text: str
    source_anchor: SourceAnchor | Mapping[str, Any]
    test_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "assertion_id", _non_empty(self.assertion_id, "assertion_id"))
        object.__setattr__(self, "text", _non_empty(self.text, "assertion.text"))
        anchor = self.source_anchor
        if not isinstance(anchor, SourceAnchor):
            anchor = SourceAnchor.from_mapping(anchor)
        object.__setattr__(self, "source_anchor", anchor)
        test_ids = _strings(self.test_ids, "assertion.test_ids")
        if not test_ids:
            raise ValueError("each assertion requires at least one test ID")
        object.__setattr__(self, "test_ids", test_ids)

    def to_dict(self) -> dict[str, Any]:
        return {"assertion_id": self.assertion_id, "text": self.text,
                "source_anchor": self.source_anchor.to_dict(), "test_ids": list(self.test_ids)}

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AssertionContract":
        if not isinstance(value, Mapping):
            raise TypeError("assertion_contracts items must be objects")
        return cls(assertion_id=value.get("assertion_id"), text=value.get("text"),
                   source_anchor=value.get("source_anchor"),
                   test_ids=_sequence(value.get("test_ids")))


def _non_empty(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    result = value.strip()
    if not result:
        raise ValueError(f"{name} must be non-empty")
    return result


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value.strip()


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise TypeError(f"{name} must be a string sequence")
    values = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
    result = tuple(_non_empty(item, name) for item in values)
    if len(result) != len(set(result)):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _sequence(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _freeze(value: Any) -> Any:
    """Detach nested metadata so a frozen dataclass is actually immutable."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _json_value(value: Any, *, path: str = "value") -> Any:
    """Return detached JSON data and reject executable/custom values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError(f"{path} contains a non-finite number")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value, path=path)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} has a non-string key")
            result[key] = _json_value(item, path=f"{path}.{key}")
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item, path=f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, (set, frozenset)):
        values = [_json_value(item, path=f"{path}[]") for item in value]
        return sorted(values, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _json_value(to_dict(), path=path)
    if is_dataclass(value):
        return _json_value(asdict(value), path=path)
    raise TypeError(f"{path} must contain JSON data; got {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize contract data deterministically for persistence and hashes."""

    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BehaviorEntry:
    """One observable behavior required by a functional contract."""

    entry_id: str
    behavior_source: str
    preconditions: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    assertions: tuple[str, ...] = ("observable behavior is preserved",)
    side: str = "both"
    test_mapping: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = CONTRACT_SCHEMA_VERSION
    assertion_contracts: tuple[AssertionContract, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != CONTRACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported contract schema_version {self.schema_version}")
        object.__setattr__(self, "entry_id", _non_empty(self.entry_id, "entry_id"))
        object.__setattr__(self, "behavior_source", _non_empty(self.behavior_source, "behavior_source"))
        for name in ("preconditions", "operations", "assertions", "test_mapping", "evidence_refs"):
            object.__setattr__(self, name, _strings(getattr(self, name), name))
        object.__setattr__(self, "side", _non_empty(self.side, "side").lower())
        if self.side not in {"client", "server", "both", "shared"}:
            raise ValueError("side must be client, server, both, or shared")
        if not self.assertions:
            raise ValueError("each behavior entry requires at least one assertion")
        assertion_contracts = tuple(
            item if isinstance(item, AssertionContract) else AssertionContract.from_mapping(item)
            for item in self.assertion_contracts
        )
        assertion_ids = [item.assertion_id for item in assertion_contracts]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise ValueError("assertion_contracts must have unique assertion_id values")
        if assertion_contracts and set(self.assertions) != {item.text for item in assertion_contracts}:
            raise ValueError("assertions must exactly match assertion_contracts text")
        object.__setattr__(self, "assertion_contracts", assertion_contracts)
        object.__setattr__(self, "metadata", _freeze(_json_value(self.metadata, path="metadata")))

    @property
    def id(self) -> str:
        return self.entry_id

    @property
    def source(self) -> str:
        return self.behavior_source

    @property
    def actions(self) -> tuple[str, ...]:
        return self.operations

    @property
    def client_server_side(self) -> str:
        return self.side

    @property
    def executable_tests(self) -> tuple[str, ...]:
        return self.test_mapping

    @property
    def source_evidence(self) -> str:
        """Compatibility spelling used by the on-disk Operations schema."""

        return self.behavior_source

    @property
    def action(self) -> tuple[str, ...]:
        """Compatibility spelling for the behavior's observable actions."""

        return self.operations

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "entry_id": self.entry_id,
            "id": self.entry_id,
            "behavior_source": self.behavior_source,
            "source_evidence": self.behavior_source,
            "preconditions": list(self.preconditions),
            "operations": list(self.operations),
            "action": list(self.operations),
            "assertions": list(self.assertions),
            "side": self.side,
            "test_mapping": list(self.test_mapping),
            "evidence_refs": list(self.evidence_refs),
            "metadata": _json_value(self.metadata, path="metadata"),
        }
        if self.assertion_contracts:
            result["assertion_contracts"] = [item.to_dict() for item in self.assertion_contracts]
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, default_id: str = "") -> "BehaviorEntry":
        if not isinstance(value, Mapping):
            raise TypeError("behavior entry must be a mapping")
        entry_id = value.get("entry_id", value.get("id", value.get("name", default_id)))
        source = value.get(
            "behavior_source",
            value.get("source_evidence", value.get("source", value.get("behavior"))),
        )
        if source is None:
            raise ValueError("behavior entry requires behavior_source or source")
        operations = value.get(
            "operations",
            value.get("action", value.get("actions", value.get("operation", ()))),
        )
        assertions = value.get("assertions", value.get("expected", ()))
        if assertions == ():
            assertions = ("observable behavior is preserved",)
        test_mapping = value.get("test_mapping", value.get("executable_tests", value.get("tests", ())))
        if isinstance(test_mapping, str):
            test_mapping = (test_mapping,)
        evidence_refs = value.get("evidence_refs", value.get("evidence", ()))
        if isinstance(evidence_refs, str):
            evidence_refs = (evidence_refs,)
        assertion_contracts = value.get("assertion_contracts", ())
        if not isinstance(assertion_contracts, (list, tuple)):
            raise TypeError("assertion_contracts must be an array")
        if assertion_contracts:
            typed_assertions = tuple(AssertionContract.from_mapping(item) for item in assertion_contracts)
            if "assertions" in value:
                supplied_assertions = _sequence(value.get("assertions"))
                if tuple(str(item) for item in supplied_assertions) != tuple(item.text for item in typed_assertions):
                    raise ValueError("assertions must exactly match assertion_contracts in order")
            assertions = tuple(item.text for item in typed_assertions)
        else:
            typed_assertions = ()
        return cls(
            schema_version=int(value.get("schema_version", CONTRACT_SCHEMA_VERSION)),
            entry_id=str(entry_id),
            behavior_source=str(source),
            preconditions=_sequence(value.get("preconditions", value.get("conditions", ()))),
            operations=_sequence(operations),
            assertions=_sequence(assertions),
            side=str(value.get("side", value.get("client_server_side", "both"))),
            test_mapping=_sequence(test_mapping),
            evidence_refs=_sequence(evidence_refs),
            metadata=value.get("metadata", {}),
            assertion_contracts=typed_assertions,
        )

    from_dict = from_mapping


@dataclass(frozen=True, slots=True)
class CharacterizationContract:
    """Versioned collection of observable behavior entries."""

    contract_id: str = "modport.functional-contract.v1"
    entries: tuple[BehaviorEntry, ...] = ()
    source_fingerprint: str | None = None
    generator_id: str | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTRACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported contract schema_version {self.schema_version}")
        object.__setattr__(self, "contract_id", _non_empty(self.contract_id, "contract_id"))
        normalized: list[BehaviorEntry] = []
        for entry in self.entries:
            normalized.append(entry if isinstance(entry, BehaviorEntry) else BehaviorEntry.from_mapping(entry))
        ids = [entry.entry_id for entry in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("contract entries must have unique entry_id values")
        object.__setattr__(self, "entries", tuple(normalized))
        if self.source_fingerprint is not None:
            object.__setattr__(self, "source_fingerprint", _non_empty(self.source_fingerprint, "source_fingerprint"))
        if self.generator_id is not None:
            object.__setattr__(self, "generator_id", _non_empty(self.generator_id, "generator_id"))
        object.__setattr__(self, "evidence", _freeze(_json_value(self.evidence, path="evidence")))
        object.__setattr__(self, "metadata", _freeze(_json_value(self.metadata, path="metadata")))

    @property
    def behavior_entries(self) -> tuple[BehaviorEntry, ...]:
        return self.entries

    @property
    def entry_ids(self) -> tuple[str, ...]:
        return tuple(entry.entry_id for entry in self.entries)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "contract_id": self.contract_id,
            "entries": [entry.to_dict() for entry in self.entries],
            # ``behaviors`` is the stable file-facing spelling used by the
            # Operations handlers; ``entries`` remains the typed API name.
            "behaviors": [entry.to_dict() for entry in self.entries],
            "evidence": _json_value(self.evidence, path="evidence"),
            "metadata": _json_value(self.metadata, path="metadata"),
        }
        if self.source_fingerprint is not None:
            result["source_fingerprint"] = self.source_fingerprint
        if self.generator_id is not None:
            result["generator_id"] = self.generator_id
        return result

    def canonical_json(self) -> str:
        return canonical_json(self.to_dict())

    def sha256(self) -> str:
        return _digest(self.to_dict())

    @property
    def digest(self) -> str:
        return self.sha256()

    @property
    def contract_hash(self) -> str:
        return self.sha256()

    def freeze(self, review: "ReviewRecord") -> "FrozenContract":
        return freeze_contract(self, review)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CharacterizationContract":
        if not isinstance(value, Mapping):
            raise TypeError("contract must be a mapping")
        entries = value.get("entries")
        behaviors = value.get("behaviors")
        if entries is not None and behaviors is not None:
            if canonical_json(entries) != canonical_json(behaviors):
                raise ValueError("contract entries and behaviors must be canonically identical")
        selected = entries if entries is not None else (behaviors if behaviors is not None else ())
        return cls(
            schema_version=int(value.get("schema_version", CONTRACT_SCHEMA_VERSION)),
            contract_id=str(value.get("contract_id", value.get("id", "modport.functional-contract.v1"))),
            entries=tuple(selected),
            source_fingerprint=value.get("source_fingerprint"),
            generator_id=value.get("generator_id"),
            evidence=value.get("evidence", {}),
            metadata=value.get("metadata", {}),
        )


@dataclass(frozen=True, slots=True)
class ReviewRecord:
    """An independent review decision for one contract version.

    ``contract_sha256`` remains an optional, host-generated diagnostic field
    for readers of older run records.  Review validity is determined by the
    review status, identities and schema rather than by an agent-supplied
    content digest.
    """

    reviewer_id: str
    contract_sha256: str | None = None
    status: str = "approved"
    generator_id: str | None = None
    review_id: str = ""
    notes: str = ""
    evidence_refs: tuple[str, ...] = ()
    schema_version: int = REVIEW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != REVIEW_SCHEMA_VERSION:
            raise ValueError(f"unsupported review schema_version {self.schema_version}")
        object.__setattr__(self, "reviewer_id", _non_empty(self.reviewer_id, "reviewer_id"))
        if self.contract_sha256 is not None:
            value = _text(self.contract_sha256, "contract_sha256")
            object.__setattr__(self, "contract_sha256", value or None)
        status = _non_empty(self.status, "status").lower()
        if status not in {"approved", "rejected", "changes_requested"}:
            raise ValueError("review status must be approved, rejected, or changes_requested")
        object.__setattr__(self, "status", status)
        if self.generator_id is not None:
            object.__setattr__(self, "generator_id", _non_empty(self.generator_id, "generator_id"))
        if self.generator_id is not None and self.generator_id == self.reviewer_id:
            raise ContractReviewError("reviewer must be independent from contract generator")
        object.__setattr__(self, "review_id", _text(self.review_id, "review_id"))
        object.__setattr__(self, "notes", _text(self.notes, "notes"))
        object.__setattr__(self, "evidence_refs", _strings(self.evidence_refs, "evidence_refs"))

    @property
    def approved(self) -> bool:
        return self.status == "approved"

    @property
    def independent(self) -> bool:
        return self.generator_id is None or self.generator_id != self.reviewer_id

    @property
    def contract_hash(self) -> str:
        return self.contract_sha256

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema_version": self.schema_version,
            "review_id": self.review_id,
            "reviewer_id": self.reviewer_id,
            "generator_id": self.generator_id,
            "status": self.status,
            "verdict": self.status,
            "approved": self.approved,
            "independent": self.independent,
            "notes": self.notes,
            "evidence_refs": list(self.evidence_refs),
        }
        if self.contract_sha256 is not None:
            result["contract_sha256"] = self.contract_sha256
            # Compatibility spelling for existing consumers.  It is optional
            # metadata and is never required to accept a review.
            result["candidate_sha256"] = self.contract_sha256
        return result


@dataclass(frozen=True, slots=True)
class FrozenContract:
    """A reviewed contract with a stable schema and independent approval.

    The optional ``frozen_sha256`` identifies the bytes written by the host;
    it is not a prohibition on later run-local edits or repair iterations.
    """

    contract: CharacterizationContract
    review: ReviewRecord
    frozen_sha256: str = ""
    schema_version: int = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CONTRACT_SCHEMA_VERSION:
            raise ValueError(f"unsupported frozen contract schema_version {self.schema_version}")
        if not isinstance(self.contract, CharacterizationContract):
            raise TypeError("frozen contract requires a CharacterizationContract")
        if not isinstance(self.review, ReviewRecord):
            raise TypeError("frozen contract requires a ReviewRecord")
        if not self.review.approved or not self.review.independent:
            raise ContractReviewError("only an approved independent review can freeze a contract")
        supplied = self.frozen_sha256 or self.contract.sha256()
        object.__setattr__(self, "frozen_sha256", supplied)

    @property
    def contract_hash(self) -> str:
        return self.frozen_sha256

    @property
    def freeze_hash(self) -> str:
        return self.frozen_sha256

    @property
    def contract_sha256(self) -> str:
        return self.frozen_sha256

    @property
    def digest(self) -> str:
        return self.frozen_sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "contract": self.contract.to_dict(),
            "review": self.review.to_dict(),
            "frozen_sha256": self.frozen_sha256,
        }

    def verify(self) -> bool:
        """Verify the semantic review gate without comparing checkout bytes."""

        if not self.review.approved or not self.review.independent:
            raise FrozenContractError("frozen contract review is no longer valid")
        return True

    def assert_unchanged(self, candidate: CharacterizationContract | Mapping[str, Any]) -> bool:
        """Preserve behavior requirements while allowing metadata to evolve.

        Compare the declared requirements themselves. Formatting, evidence
        annotations and optional digests are not part of this check.
        """

        self.verify()
        if not isinstance(candidate, CharacterizationContract):
            try:
                candidate = CharacterizationContract.from_mapping(candidate)
            except (TypeError, ValueError) as exc:
                raise FrozenContractError("frozen functional contract cannot be changed or weakened") from exc
        entries = {entry.entry_id: entry for entry in candidate.entries}
        for original in self.contract.entries:
            updated = entries.get(original.entry_id)
            if (updated is None or any(getattr(original, field) != getattr(updated, field)
                    for field in ("preconditions", "operations", "side"))
                    or not set(original.assertions).issubset(updated.assertions)
                    or not set(original.test_mapping).issubset(updated.test_mapping)):
                raise FrozenContractError(f"frozen behavior requirements were weakened: {original.entry_id}")
            if original.assertion_contracts:
                current_assertions = {item.assertion_id: item for item in updated.assertion_contracts}
                for assertion in original.assertion_contracts:
                    current = current_assertions.get(assertion.assertion_id)
                    if current is None or current.to_dict() != assertion.to_dict():
                        raise FrozenContractError(
                            f"frozen assertion identity or source anchor changed: {assertion.assertion_id}"
                        )
        return True

    # Explicitly named aliases make it difficult for a caller to mistake this
    # gate for an advisory review.
    assert_not_weakened = assert_unchanged
    validate_candidate = assert_unchanged

    def enforce(self, candidate: CharacterizationContract | Mapping[str, Any] | None = None, *, waiver: Any = None) -> bool:
        if waiver is not None:
            raise FrozenContractError("frozen functional contract does not permit waivers or exemptions")
        self.verify()
        if candidate is not None:
            return self.assert_unchanged(candidate)
        return True

    def waive(self, *args: Any, **kwargs: Any) -> None:
        raise FrozenContractError("frozen functional contract does not permit waivers or exemptions")

    allow_waiver = waive
    add_exemption = waive
    apply_waiver = waive
    with_waiver = waive
    exempt = waive


def _observation_entries(observations: Any) -> tuple[BehaviorEntry, ...]:
    if observations is None:
        return ()
    if isinstance(observations, CharacterizationContract):
        return observations.entries
    if isinstance(observations, Mapping):
        if any(key in observations for key in ("entries", "behaviors", "features")):
            values = observations.get("entries", observations.get("behaviors", observations.get("features", ())))
            if isinstance(values, Mapping):
                values = [
                    {"entry_id": str(key), "behavior_source": str(key), "metadata": value}
                    for key, value in values.items()
                ]
        else:
            values = (observations,)
    elif isinstance(observations, (str, Path)):
        path = Path(observations)
        if path.exists():
            paths = [path] if path.is_file() else sorted(item for item in path.rglob("*") if item.is_file())
            result = []
            for item in paths:
                relative = str(item.relative_to(path.parent if path.is_file() else path))
                result.append(
                    BehaviorEntry(
                        entry_id=f"artifact:{relative}",
                        behavior_source=relative,
                        operations=("observe source artifact",),
                        assertions=("source artifact is represented in characterization evidence",),
                        evidence_refs=(relative,),
                    )
                )
            return tuple(result)
        return (
            BehaviorEntry(
                entry_id="observation:source",
                behavior_source=str(observations),
                operations=("observe declared source",),
            ),
        )
    else:
        values = observations
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise TypeError("observations must be a mapping, path, or iterable of mappings")
    result = []
    for index, value in enumerate(values):
        if isinstance(value, BehaviorEntry):
            result.append(value)
        elif isinstance(value, Mapping):
            result.append(BehaviorEntry.from_mapping(value, default_id=f"behavior:{index}"))
        else:
            result.append(
                BehaviorEntry(
                    entry_id=f"behavior:{index}",
                    behavior_source=str(value),
                    operations=("observe declared behavior",),
                )
            )
    return tuple(result)


def generate_contract(
    observations: Any = None,
    *,
    contract_id: str = "modport.functional-contract.v1",
    source_fingerprint: str | None = None,
    generator_id: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> CharacterizationContract:
    """Build a generic functional contract from observations or artifacts."""

    if isinstance(observations, CharacterizationContract):
        return observations
    return CharacterizationContract(
        contract_id=contract_id,
        entries=_observation_entries(observations),
        source_fingerprint=source_fingerprint,
        generator_id=generator_id,
        evidence=evidence or {},
        metadata=metadata or {},
    )


build_contract = generate_contract
characterize = generate_contract
generate_functional_contract = generate_contract


def review_contract(
    contract: CharacterizationContract,
    reviewer_id: str,
    *,
    approved: bool = True,
    status: str | None = None,
    generator_id: str | None = None,
    review_id: str = "",
    notes: str = "",
    evidence_refs: Iterable[str] = (),
    contract_sha256: str | None = None,
) -> ReviewRecord:
    """Create an independent review record for the current contract."""

    if not isinstance(contract, CharacterizationContract):
        contract = CharacterizationContract.from_mapping(contract)
    effective_generator = generator_id if generator_id is not None else contract.generator_id
    if effective_generator is not None and effective_generator == reviewer_id:
        raise ContractReviewError("reviewer must be independent from contract generator")
    digest = contract.sha256()
    # A caller may retain an old digest for display, but it is not a review
    # precondition.  The host computes the optional diagnostic value below.
    effective_status = status if status is not None else ("approved" if approved else "rejected")
    return ReviewRecord(
        reviewer_id=reviewer_id,
        contract_sha256=digest,
        status=effective_status,
        generator_id=effective_generator,
        review_id=review_id,
        notes=notes,
        evidence_refs=tuple(evidence_refs),
    )


def freeze_contract(contract: CharacterizationContract, review: ReviewRecord) -> FrozenContract:
    """Freeze only an independently reviewed contract."""

    if not isinstance(contract, CharacterizationContract):
        contract = CharacterizationContract.from_mapping(contract)
    if not isinstance(review, ReviewRecord):
        raise TypeError("review must be a ReviewRecord")
    return FrozenContract(contract=contract, review=review, frozen_sha256=contract.sha256())


freeze_functional_contract = freeze_contract
freeze_functionality_contract = freeze_contract
review_functional_contract = review_contract
independent_review = review_contract


# Compatibility names all point at the same immutable representations; there
# is no second weaker contract format.
FunctionalContract = CharacterizationContract
FunctionContract = CharacterizationContract
Contract = CharacterizationContract
ContractEntry = BehaviorEntry
FunctionalContractEntry = BehaviorEntry
ContractItem = BehaviorEntry
IndependentReview = ReviewRecord
IndependentReviewRecord = ReviewRecord
ContractReview = ReviewRecord
FrozenFunctionalContract = FrozenContract


__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "FUNCTIONAL_CONTRACT_SCHEMA_VERSION",
    "REVIEW_SCHEMA_VERSION",
    "ContractError",
    "ContractReviewError",
    "FrozenContractError",
    "SourceAnchor",
    "AssertionContract",
    "BehaviorEntry",
    "ContractEntry",
    "FunctionalContractEntry",
    "ContractItem",
    "CharacterizationContract",
    "FunctionalContract",
    "FunctionContract",
    "Contract",
    "ReviewRecord",
    "IndependentReview",
    "IndependentReviewRecord",
    "ContractReview",
    "FrozenContract",
    "FrozenFunctionalContract",
    "canonical_json",
    "generate_contract",
    "build_contract",
    "characterize",
    "generate_functional_contract",
    "review_contract",
    "review_functional_contract",
    "independent_review",
    "freeze_contract",
    "freeze_functional_contract",
    "freeze_functionality_contract",
]
