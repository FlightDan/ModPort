"""Read-only preflight checks through the SDK's public identity API."""

from pathlib import Path
from typing import Any, Mapping

import dispatcher_sdk


SDK_VERSION = "0.7.1"

# These are the exact writable storage contracts for the pinned deployment.
# Runtime identity may still describe older stores for diagnostics, but those
# observations must never be promoted to execution authority here.
_WRITABLE_STORAGE_SCHEMAS = {
    "kernel": ("kernel", 5),
    "orchestrator": ("orchestrator", 4),
}


class SDKCompatibilityError(ValueError):
    """The imported SDK or existing storage cannot support this deployment."""


def _identity(**options: Any) -> dict[str, Any]:
    try:
        identify = dispatcher_sdk.runtime_identity
    except AttributeError as error:
        raise SDKCompatibilityError(
            f"ModPort requires dispatcher-sdk {SDK_VERSION} with runtime_identity"
        ) from error
    return identify(**options).to_dict()


def _validate_release(report: Mapping[str, Any]) -> str:
    module = report["module"]
    source = module["source_version"]
    distribution = module["distribution_version"]
    if (source != SDK_VERSION
            or distribution not in (None, SDK_VERSION)
            or module["version_agreement"] == "mismatch"
            or module["distribution_record"] == "mismatch"):
        raise SDKCompatibilityError(
            f"ModPort requires dispatcher-sdk {SDK_VERSION}; "
            f"source={source!r}, distribution={distribution!r}, "
            f"version_agreement={module['version_agreement']}, "
            f"distribution_record={module['distribution_record']}. "
            "Install matching SDK source and distribution before opening writers."
        )
    return SDK_VERSION


def sdk_release() -> str:
    """Validate imported source and available distribution evidence, then return its release.

    An uninstalled source checkout is supported; a known RECORD or version
    mismatch is not. Call at a deployment boundary, not per handler fingerprint.
    """
    return _validate_release(_identity())


def sdk_module_identity() -> dict[str, Any]:
    """Bind a helper process to the same validated imported SDK source."""
    report = _identity()
    _validate_release(report)
    return dict(report["module"])


def inspect_runtime(root: str | Path, handlers: Mapping[Any, Any] | None = None) -> dict[str, Any]:
    """Report public SDK identity and separate storage snapshots without writers.

    Preserve unsupported verdicts for diagnostics. Supplying handlers additionally
    checks unfinished command bindings. Neither snapshots nor verdicts authorize
    execution or replace the SDK writer's own checks.
    """
    root = Path(root)
    return _identity(component_paths={
        "kernel": root / "kernel.sqlite3",
        "orchestrator": root / "orchestrator.sqlite3",
    }, handlers=handlers)


def require_compatible_storage(
    root: str | Path, handlers: Mapping[Any, Any] | None = None,
) -> dict[str, Any]:
    """Reject incompatible existing stores before constructing any SDK writer.

    Missing stores may be initialized normally. Unknown observations fail closed.
    No store is created, repaired, upgraded, or removed by this function.
    """
    report = inspect_runtime(root, handlers)
    _validate_release(report)
    failures = []
    observed_names = [storage.get("name") for storage in report.get("storages", ())]
    if (report.get("complete") is not True
            or len(observed_names) != len(_WRITABLE_STORAGE_SCHEMAS)
            or set(observed_names) != set(_WRITABLE_STORAGE_SCHEMAS)):
        failures.append(
            "runtime identity did not return one complete observation for each "
            f"required storage; observed={observed_names!r}, "
            f"complete={report.get('complete')!r}"
        )
    for storage in report["storages"]:
        if storage["status"] == "missing" and storage["exists"] is False:
            continue
        storage_name = storage["name"]
        expected = _WRITABLE_STORAGE_SCHEMAS.get(storage_name)
        component, expected_schema = expected or (storage_name, None)
        if (expected is None
                or storage["status"] != "recognized"
                or storage["schemas"].get(component) != expected_schema
                or storage["execute"]["status"] != "supported"
                or (handlers is not None and storage["resume"]["status"] != "supported")):
            verdict = storage["resume"] if handlers is not None else storage["execute"]
            reasons = storage["facts"].get("issues") or storage["reasons"] or verdict["reasons"]
            failures.append(
                f"{storage['path']}: {storage['status']}; "
                f"{component} schema={storage['schemas'].get(component)!r} "
                f"(expected {expected_schema!r}); {reasons}"
            )
    if failures:
        raise SDKCompatibilityError(
            "SDK storage preflight rejected existing storage: " + "; ".join(failures)
            + ". Back up the complete run before any repair or copy upgrade. "
            "This deployment opens only Kernel schema 5 and Orchestrator schema 4. "
            "Older Run stores remain historical evidence. ModPort does not upgrade "
            "or activate them automatically."
        )
    return report
