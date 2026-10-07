"""Host-integrated cleanup stages for research material and migration code."""
from __future__ import annotations

from .workspace import project_path, is_project_workspace
from contextlib import ExitStack
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Mapping

from . import handlers
from .contracts import OperationInput, OperationResult
from .evidence import atomic_json, file_digest, verified_path, workspace_lock
from .manifest import canonical_json


def _artifact_ref(root: Path, path: Path, media_type: str,
                  metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if (path.is_symlink() or not path.is_file() or path.resolve() != path.absolute()
            or not path.resolve().is_relative_to(root.resolve())):
        raise ValueError("cleanup artifact is not a contained regular file")
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": file_digest(path),
        "media_type": media_type,
        "metadata": dict(metadata or {}),
    }


def _ensure_run_directory(root: Path, directory: Path) -> Path:
    """Create a directory one component at a time without following symlinks."""
    root = root.resolve(strict=True)
    try:
        relative = directory.relative_to(root)
    except ValueError as exc:
        raise ValueError("cleanup write directory escapes the Run") from exc
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("cleanup write directory has an unsafe path")

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("cleanup write directory traverses a symlink")
        if not current.exists():
            current.mkdir()
        if (current.is_symlink() or not current.is_dir()
                or not current.resolve(strict=True).is_relative_to(root)):
            raise ValueError("cleanup write directory is outside the Run or not a directory")
    return current


def _execution_directory(root: Path, command: OperationInput) -> Path:
    execution_id = command.command_id
    if (not isinstance(execution_id, str) or not execution_id
            or execution_id in {".", ".."} or "/" in execution_id or "\\" in execution_id):
        raise ValueError("cleanup execution ID is unsafe")
    return root / "artifacts" / "executions" / execution_id


def _write_markdown(root: Path, command: OperationInput, content: str) -> Path:
    directory = _ensure_run_directory(root, _execution_directory(root, command))
    target = directory / "research-cleanup-index.md"
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValueError("research index artifact path is unsafe")
    data = content.encode("utf-8")
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError("existing research index differs from this execution")
        return target
    descriptor, temporary = tempfile.mkstemp(prefix=".modport-cleanup-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def _source_material_refs(root: Path, command: OperationInput) -> tuple[dict, list[str]]:
    materials: dict[str, dict[str, str]] = {}
    diagnostics: list[str] = []
    for alias, raw in sorted(command.artifact_refs.items()):
        if not isinstance(alias, str) or not isinstance(raw, Mapping):
            continue
        try:
            path = verified_path(root, raw)
            materials[alias] = {
                "path": path.relative_to(root).as_posix(),
                "sha256": file_digest(path),
            }
        except (OSError, TypeError, ValueError) as exc:
            diagnostics.append(f"material reference {alias} was not indexed: {exc}")
    return materials, diagnostics


def _last_message(root: Path, outputs: Mapping[str, Any]) -> str | None:
    refs = outputs.get("artifact_refs", {})
    raw = refs.get("agent_last_message") if isinstance(refs, Mapping) else None
    if isinstance(raw, Mapping):
        return verified_path(root, raw).read_text(encoding="utf-8")
    relative = outputs.get("last_message")
    if not isinstance(relative, str):
        return None
    try:
        return verified_path(root, {"path": relative}).read_text(encoding="utf-8")
    except (OSError, TypeError, ValueError):
        return None


class ResearchCleanupHandler:
    """Archive a concise, source-bound index without modifying source files."""

    def __call__(self, command: OperationInput) -> OperationResult:
        root = Path(command.run_dir).resolve()
        result: OperationResult | None = None
        try:
            from .development import _head

            baseline = root / "baseline"
            if (baseline.is_symlink() or baseline.resolve() != baseline.absolute()
                    or not baseline.is_dir() or not baseline.resolve().is_relative_to(root)):
                raise ValueError("research cleanup baseline is missing or unsafe")
            source_commit = _head(command, baseline)
            materials, diagnostics = _source_material_refs(root, command)
            from .prompts import STAGE_PROMPTS

            result = handlers.CodexStageHandler(
                STAGE_PROMPTS["research_cleanup"], baseline=True, read_only=True,
            )(replace(command, options={
                key: value for key, value in command.options.items() if key != "workspace"
            }))
            if result.status != "completed":
                return result

            outputs = dict(result.outputs)
            refs = dict(outputs.get("artifact_refs", {}))
            try:
                index = _last_message(root, outputs)
                if index is None or not index.strip():
                    diagnostics.append("final Markdown index was unavailable or empty")
                else:
                    path = _write_markdown(root, command, index)
                    refs["research_cleanup"] = _artifact_ref(root, path, "text/markdown", {
                        "execution_id": command.command_id,
                        "source_workspace": "baseline",
                        "source_commit": source_commit,
                        "material_refs": materials,
                    })
            except (OSError, UnicodeError, TypeError, ValueError) as exc:
                diagnostics.append(f"final Markdown index could not be archived: {exc}")
            outputs.update(artifact_refs=refs, source_commit=source_commit,
                           indexed_material_refs=materials)
            if diagnostics:
                return handlers._unverified_result(
                    command, status=result.status, outputs=outputs, diagnostics=diagnostics,
                    detail="read-only research indexing completed with advisory gaps")
            return handlers._result(
                command, "completed", outputs=outputs,
                detail="read-only source and material index recorded")
        except (OSError, UnicodeError, TypeError, ValueError, KeyError,
                subprocess.SubprocessError) as exc:
            if result is not None and result.status == "completed":
                return handlers._unverified_result(
                    command, status=result.status, outputs=result.outputs,
                    diagnostics=[str(exc)],
                    detail="read-only research indexing completed with advisory gaps")
            return handlers._result(
                command, "failed", outputs={} if result is None else dict(result.outputs),
                detail=str(exc), error_code="cleanup_integrity")


def _workspace_paths(root: Path, command: OperationInput) -> tuple[Path, Path]:
    execution_dir = _execution_directory(root, command)
    identity = sha256(command.command_id.encode("utf-8")).hexdigest()
    parent = _ensure_run_directory(root, root / "workspaces" / "code-cleanup")
    workspace = parent / identity
    execution_dir = _ensure_run_directory(root, execution_dir)
    setup_path = execution_dir / "code-cleanup-setup.json"
    return workspace, setup_path


def _load_or_create_setup(root: Path, command: OperationInput, source_candidate: str,
                          workspace: Path, setup_path: Path) -> None:
    safe_parent = _ensure_run_directory(root, setup_path.parent)
    if setup_path.parent != safe_parent:
        raise ValueError("code cleanup setup parent changed during validation")
    relative = workspace.relative_to(root).as_posix()
    if setup_path.exists() or setup_path.is_symlink():
        if (setup_path.is_symlink() or not setup_path.is_file()
                or setup_path.resolve() != setup_path.absolute()):
            raise ValueError("code cleanup setup record is unsafe")
        envelope = json.loads(setup_path.read_text(encoding="utf-8"))
        record = envelope.get("record") if isinstance(envelope, Mapping) else None
        if not isinstance(record, Mapping):
            raise ValueError("code cleanup setup record is invalid")
        expected = sha256(canonical_json(dict(record)).encode("utf-8")).hexdigest()
        if (envelope.get("sha256") != expected or record.get("schema_version") != 1
                or record.get("execution_id") != command.command_id
                or record.get("source_candidate") != source_candidate
                or record.get("workspace") != relative):
            raise ValueError("code cleanup setup identity differs from this operation")
        return
    record = {"schema_version": 1, "execution_id": command.command_id,
              "source_candidate": source_candidate, "workspace": relative}
    atomic_json(setup_path, {
        "record": record,
        "sha256": sha256(canonical_json(record).encode("utf-8")).hexdigest(),
    })


def _candidate_record(root: Path, command: OperationInput, *, before: str,
                      collected: str | None, after: str | None,
                      paths: list[str], report_ref: Mapping[str, Any] | None,
                      workspace: Path, status: str) -> dict:
    record = {
        "schema_version": 1,
        "execution_id": command.command_id,
        "source_candidate": before,
        "collected_candidate": collected,
        "integrated_candidate": after,
        "paths": list(paths),
        "report_ref": dict(report_ref) if report_ref is not None else None,
        "workspace": workspace.relative_to(root).as_posix(),
        "status": status,
    }
    path = root / "artifacts" / "executions" / command.command_id / "code-cleanup-candidate.json"
    safe_parent = _ensure_run_directory(root, path.parent)
    if path.parent != safe_parent:
        raise ValueError("cleanup candidate record parent changed during validation")
    data = (canonical_json(record) + "\n").encode("utf-8")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise ValueError("existing cleanup candidate record differs from this execution")
    else:
        atomic_json(path, record)
    return _artifact_ref(root, path, "application/json", {
        "execution_id": command.command_id,
        "source_candidate": before,
        "collected_candidate": collected,
        "integrated_candidate": after,
    })


def _report_ref(root: Path, outputs: Mapping[str, Any], *, before: str,
                after: str | None) -> dict | None:
    refs = outputs.get("artifact_refs", {})
    if not isinstance(refs, Mapping):
        return None
    raw = refs.get("agent_last_message")
    if not isinstance(raw, Mapping):
        raw = next((ref for ref in refs.values() if isinstance(ref, Mapping)
                    and ref.get("path") == outputs.get("last_message")), None)
    if not isinstance(raw, Mapping):
        return None
    path = verified_path(root, raw)
    if not path.read_text(encoding="utf-8").strip():
        return None
    return {
        **dict(raw),
        "media_type": "text/markdown",
        "metadata": {**dict(raw.get("metadata", {})),
                      "candidate_before": before,
                      "candidate_after": after},
    }


def _diagnostic_repair_application(root, command, workspace, *, resumed):
    """Retain private-clone application receipts without resetting resumed edits."""
    from .diagnostic_repairs import apply

    refs = command.payload.get('diagnostic_repair_refs', [])
    targets = command.payload.get('diagnostic_repair_targets', [])
    identity = {'run_id': command.run_id, 'execution_id': command.command_id,
                'workspace': workspace.relative_to(root).as_posix(),
                'repair_refs': refs, 'targets': targets}
    path = _execution_directory(root, command) / 'diagnostic-repair-application.json'
    diagnostics = []
    if resumed:
        try:
            if (path.is_symlink() or path.resolve() != path.absolute() or not path.is_file()):
                raise ValueError('original cleanup diagnostic repair receipt is missing or unsafe')
            record = json.loads(path.read_text(encoding='utf-8'))
            if (not isinstance(record, Mapping) or record.get('identity') != identity
                    or not isinstance(record.get('receipts'), list)):
                raise ValueError('cleanup diagnostic repair receipt differs from its original execution')
            return record['receipts'], diagnostics
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            diagnostics.append(str(exc) + '; existing clone edits were preserved without reapplication')
            return [{'repair_ref': ref, 'status': 'unknown', 'paths': [], 'files': [],
                     'workspace_scope': 'cleanup_clone', 'detail': diagnostics[-1]}
                    for ref in refs], diagnostics
    receipts = apply(command, workspace, refs)
    for receipt in receipts:
        receipt['workspace_scope'] = 'cleanup_clone'
    try:
        if path.exists() or path.is_symlink() or path.resolve() != path.absolute():
            raise ValueError('cleanup diagnostic repair receipt path is unsafe or already exists')
        atomic_json(path, {'identity': identity, 'receipts': receipts})
    except (OSError, ValueError, TypeError) as exc:
        diagnostics.append('cleanup diagnostic repair receipts could not be archived: ' + str(exc))
    return receipts, diagnostics


class CodeCleanupHandler:
    """Edit an isolated candidate, then host-collect and apply its safe delta."""

    prompt_stage = 'code_cleanup'

    def prepare_integration(self, command, root, **record):
        pass

    def prepare_source(self, command, source):
        from .rework_coder import _review_outputs
        return _review_outputs(command, source)

    def check_source(self, command, source):
        from .development import _clean
        _clean(command, source)

    def prepare_workspace(self, command, source, workspace):
        pass

    def apply_integration(self, command, source, patch_path, changed_paths):
        from .development import _apply
        _apply(command, source, patch_path, {'id': 'code-cleanup', 'owned_paths': changed_paths})

    def rollback_integration(self, command, source, before, changed_paths):
        from .development import _git
        _git(command, source, 'reset', '--hard', before)

    def __call__(self, command: OperationInput) -> OperationResult:
        root = Path(command.run_dir).resolve()
        from .development import _apply, _clean, _git, _head
        from .host_candidate import collect_host_candidate, export_host_candidate
        from .rework_coder import _restore_review_outputs, _review_outputs
        from .execution_budget import (publication_phase, reserve_settlement,
                                      settlement_phase)

        source = project_path(root, "worktree")
        workspace: Path | None = None
        report_paths: list[str] = []
        retained: dict[str, tuple[bytes, int]] = {}
        agent_result: OperationResult | None = None
        before: str | None = None
        collected: str | None = None
        after: str | None = None
        changed_paths: list[str] = []
        report_ref: dict | None = None
        patch_ref: dict | None = None
        candidate_ref: dict | None = None
        outcome: OperationResult | None = None
        failure: tuple[str, str] | None = None
        diagnostics: list[str] = []
        repair_enabled = (command.stage_id == 'code_cleanup'
                          and command.options.get('workflow_version', 0) >= 40)
        repair_refs = command.payload.get('diagnostic_repair_refs', []) if repair_enabled else []
        repair_receipts = []
        integration_started = False
        phase = ExitStack()

        def rollback(*, check_clean: bool) -> None:
            nonlocal after, failure
            try:
                self.rollback_integration(command, source, before, changed_paths)
                after = _head(command, source)
                if after != before:
                    raise ValueError("git reset did not restore the starting candidate")
                if check_clean:
                    self.check_source(command, source)
            except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as exc:
                failure = ("cleanup_integrity", (failure[1] + "; " if failure else "")
                           + "cleanup integration rollback failed: " + str(exc))
                try:
                    after = _head(command, source)
                except (OSError, ValueError, TimeoutError, subprocess.SubprocessError) as identity_error:
                    failure = (failure[0], failure[1] + "; actual candidate HEAD unavailable: "
                               + str(identity_error))

        try:
            available = handlers._remaining_timeout(command, 7200)
            capture_reserve = min(180.0, available * 0.2)
            allocation = reserve_settlement(command, capture_reserve)
            model_deadline = (allocation.model_deadline if allocation is not None
                              else time.time() + available - capture_reserve)
        except TimeoutError as exc:
            return handlers._result(command, "failed", detail=str(exc), error_code="budget_exhausted",
                outputs=({'diagnostic_repair_refs': repair_refs, 'diagnostic_repair_receipts': [],
                          'diagnostic_repair_integration_status': 'not_integrated'} if repair_enabled else {}))
        except (OSError, TypeError, ValueError) as exc:
            return handlers._result(command, "failed", detail=str(exc), error_code="cleanup_integrity",
                outputs=({'diagnostic_repair_refs': repair_refs, 'diagnostic_repair_receipts': [],
                          'diagnostic_repair_integration_status': 'not_integrated'} if repair_enabled else {}))

        try:
            if (source.is_symlink() or source.resolve() != source.absolute()
                    or not source.is_dir() or not is_project_workspace(root, source)):
                raise ValueError("integrated migration worktree is missing or unsafe")

            # Preserve only explicitly declared reviewer reports. Other dirty
            # files are an integrity failure and never enter the cleanup clone.
            report_paths, retained = self.prepare_source(command, source)
            self.check_source(command, source)
            before = _head(command, source)
            workspace, setup_path = _workspace_paths(root, command)
            _load_or_create_setup(root, command, before, workspace, setup_path)
            resumed = workspace.exists() or workspace.is_symlink()
            if resumed:
                if (workspace.is_symlink() or not workspace.is_dir()
                        or workspace.resolve() != workspace.absolute()
                        or not workspace.resolve().is_relative_to(root)):
                    raise ValueError("existing code cleanup workspace is unsafe")
                git_metadata = workspace / ".git"
                if git_metadata.is_symlink() or not git_metadata.is_dir():
                    raise ValueError("existing code cleanup workspace has no safe Git metadata")
                workspace_head = _head(command, workspace)
                ancestry = _git(command, workspace, "merge-base", "--is-ancestor",
                                before, workspace_head, check=False)
                if ancestry.returncode:
                    raise ValueError("existing cleanup workspace is not based on the integrated candidate")
            else:
                _git(command, root, "clone", "--no-hardlinks", "--no-checkout", "--",
                     str(source), str(workspace))
                _git(command, workspace, "checkout", "--detach", before)
                if _head(command, workspace) != before:
                    raise ValueError("cleanup clone does not match the integrated candidate")

            self.prepare_workspace(command, source, workspace)
            if repair_enabled and repair_refs:
                repair_receipts, repair_diagnostics = _diagnostic_repair_application(
                    root, command, workspace, resumed=resumed)
                diagnostics.extend(repair_diagnostics)
            from .prompts import STAGE_PROMPTS

            prompt = STAGE_PROMPTS[self.prompt_stage]
            if repair_receipts:
                prompt += ('\n\nThe host processed isolated diagnostic corrections in this private cleanup clone. '
                           'Applied/already_applied entries are already present: preserve them and do only '
                           'the remaining cleanup work, without repeating those fixes. Conflict entries '
                           'were not applied. Invalid/unknown entries require inspecting their receipt details, '
                           'repair references and current code before repeating any change. Resolve only '
                           'confirmed small issues and report unresolved conflicts. These receipts describe '
                           'clone edits; the host integrates the final cleanup candidate afterward. Receipts: '
                           + json.dumps(repair_receipts, ensure_ascii=False))
            reviewer_rework = command.payload.get("reviewer_rework")
            if isinstance(reviewer_rework, Mapping):
                instructions = reviewer_rework.get("instructions")
                if not isinstance(instructions, str) or not instructions.strip():
                    raise ValueError("reviewer cleanup instructions are missing")
                prompt += (
                    "\n\nReviewer-requested cleanup revision. Address these instructions "
                    "on this candidate while preserving frozen behaviors and assertions:\n"
                    + instructions
                )
            delegated = replace(command, options={
                **command.options,
                "workspace": workspace.relative_to(root).as_posix(),
                "model_deadline_epoch": model_deadline,
            })
            agent_result = handlers.CodexStageHandler(prompt)(delegated)
            phase.enter_context(settlement_phase(command))

            try:
                report_ref = _report_ref(root, agent_result.outputs, before=before, after=None)
                if report_ref is None:
                    diagnostics.append("final cleanup report was unavailable or empty")
            except (OSError, UnicodeError, TypeError, ValueError, KeyError) as exc:
                diagnostics.append("cleanup report unavailable: " + str(exc))

            if agent_result.status != "completed":
                outputs = dict(agent_result.outputs)
                outputs.update(candidate_before=before,
                               cleanup_workspace=workspace.relative_to(root).as_posix())
                outcome = handlers._result(
                    command, agent_result.status, outputs=outputs,
                    detail=agent_result.detail, error_code=agent_result.error_code)
            else:
                candidate = collect_host_candidate(
                    command, workspace, before, "code-cleanup", report_paths=report_paths)
                collected = candidate.head
                changed_paths = candidate.paths
                execution_dir = _ensure_run_directory(
                    root, _execution_directory(root, command))
                patch_path = execution_dir / "code-cleanup.patch"
                export_host_candidate(command, candidate, patch_path)
                patch_ref = _artifact_ref(root, patch_path, "application/octet-stream", {
                    "execution_id": command.command_id,
                    "candidate_before": before,
                    "candidate_collected": collected,
                    "paths": changed_paths,
                })
                if _head(command, source) != before:
                    raise ValueError("integrated migration candidate changed during cleanup")
                self.prepare_integration(command, root, before=before, collected=collected,
                    changed_paths=changed_paths, patch_ref=patch_ref, report_ref=report_ref,
                    workspace=workspace.relative_to(root).as_posix(), agent_result=agent_result.to_dict())
                integration_started = True
                self.apply_integration(command, source, patch_path, changed_paths)
                after = _head(command, source)
                if after == before and collected != before:
                    raise ValueError("host integration did not advance the cleanup candidate")

                outputs = dict(agent_result.outputs)
                refs = dict(outputs.get("artifact_refs", {}))
                if report_ref is not None:
                    refs["code_cleanup_report"] = report_ref
                refs["code_cleanup_patch"] = patch_ref
                outputs.update(artifact_refs=refs, candidate_before=before,
                               candidate_collected=collected, candidate_after=after,
                               changed_paths=changed_paths,
                               cleanup_workspace=workspace.relative_to(root).as_posix())
                outcome = handlers._result(
                    command, "completed", outputs=outputs,
                    detail="cleanup patch was host-collected and integrated")
        except TimeoutError as exc:
            failure = ("cleanup_integrity", str(exc))
        except (OSError, UnicodeError, TypeError, ValueError, KeyError,
                subprocess.SubprocessError) as exc:
            failure = ("cleanup_integrity", str(exc))
        except Exception as exc:
            failure = ("cleanup_integrity", f"{type(exc).__name__}: {exc}")
        finally:
            phase.close()
            with publication_phase(command):
                if integration_started and failure is not None and before is not None:
                    rollback(check_clean=True)
                if retained:
                    try:
                        _restore_review_outputs(source, retained)
                    except (OSError, ValueError) as exc:
                        failure = ("cleanup_integrity",
                                   (failure[1] + "; " if failure else "")
                                   + "reviewer output could not be restored: " + str(exc))
                        if integration_started and before is not None and after != before:
                            rollback(check_clean=False)

        outputs = dict(outcome.outputs if outcome is not None else
                       agent_result.outputs if agent_result is not None else {})
        refs = dict(outputs.get("artifact_refs", {}))
        if report_ref is not None:
            report_ref = {**report_ref, "metadata": {
                **report_ref.get("metadata", {}), "candidate_after": after}}
            refs["code_cleanup_report"] = report_ref
        if patch_ref is not None:
            refs["code_cleanup_patch"] = patch_ref
        if before is not None:
            try:
                with publication_phase(command):
                    candidate_ref = _candidate_record(
                        root, command, before=before, collected=collected, after=after,
                        paths=changed_paths, report_ref=report_ref,
                        workspace=workspace or source,
                        status="failed" if failure else "integrated" if after else
                            outcome.status if outcome is not None else "failed")
                refs["code_cleanup_candidate"] = candidate_ref
            except (OSError, UnicodeError, TypeError, ValueError, TimeoutError) as exc:
                diagnostics.append("cleanup candidate record unavailable: " + str(exc))
            outputs["candidate_before"] = before
        if collected is not None:
            outputs["candidate_collected"] = collected
        if after is not None:
            outputs["candidate_after"] = after
        if changed_paths:
            outputs["changed_paths"] = changed_paths
        if workspace is not None:
            outputs["cleanup_workspace"] = workspace.relative_to(root).as_posix()
        outputs["artifact_refs"] = refs
        outputs["acceptance_status"] = "unverified"
        if repair_enabled:
            outputs.update(diagnostic_repair_refs=repair_refs,
                           diagnostic_repair_receipts=repair_receipts,
                           diagnostic_repair_integration_status=(
                               'integrated' if failure is None and outcome is not None
                               and outcome.status == 'completed' and after is not None else 'not_integrated'))
        if diagnostics:
            outputs["business_diagnostics"] = [
                *outputs.get("business_diagnostics", []), *diagnostics]
        if failure is not None:
            return handlers._result(command, "failed", outputs=outputs,
                                    detail=failure[1], error_code=failure[0])
        if outcome is None:
            return handlers._result(
                command, "failed", outputs=outputs,
                detail="cleanup handler produced no terminal result",
                error_code="cleanup_integrity")
        return replace(outcome, outputs=outputs)


class FinalCleanupHandler(CodeCleanupHandler):
    """Once-per-Run cleanup with a durable patch publication checkpoint."""

    prompt_stage = 'final_cleanup'

    @staticmethod
    def _preserved_paths(command):
        from .development import _path
        paths = set()
        if isinstance(command.artifact_refs.get('functional_contract_lock'), Mapping):
            paths.add('.modport/functional-contract.json')
        for ref in command.artifact_refs.values():
            if not isinstance(ref, Mapping):
                continue
            metadata = ref.get('metadata', {})
            source = metadata.get('source_path') if isinstance(metadata, Mapping) else None
            if isinstance(source, str) and source.startswith('worktree/.modport/'):
                paths.add(_path(source[len('worktree/'):], shared=True))
        for relative in command.payload.get('reviewer_report_paths', []):
            paths.add(_path(relative, shared=True))
        return paths

    def prepare_source(self, command, source):
        # Runtime snapshots and normalized declarations stay in the live tree.
        # Collection excludes them, rather than temporarily deleting evidence.
        return sorted(self._preserved_paths(command)), {}

    def check_source(self, command, source):
        from .development import _git
        if _git(command, source, 'diff', '--cached', '--name-only', '--').stdout:
            raise ValueError('final cleanup cannot consume unrelated staged changes')
        preserved = self._preserved_paths(command)
        records = _git(command, source, 'status', '--porcelain=v1', '-z',
                       '--untracked-files=all').stdout.split('\0')
        for record in records:
            if not record:
                continue
            relative = record[3:]
            if record[:2].strip() not in {'M', 'D', '??'} or relative not in preserved:
                raise ValueError('final cleanup has unrelated pending changes: ' + relative)
        for relative in preserved:
            target = source / relative
            if (target.is_symlink() or target.resolve() != target.absolute()
                    or (target.exists() and not target.is_file())):
                raise ValueError('preserved host output is not a contained regular file: ' + relative)

    def prepare_workspace(self, command, source, workspace):
        # Supply the host-normalized contract to the isolated reader while keeping
        # generated runtime evidence exclusively in the original Run workspace.
        relative = '.modport/functional-contract.json'
        if relative not in self._preserved_paths(command):
            return
        current = source / relative
        destination = workspace / relative
        if destination.is_symlink() or destination.resolve() != destination.absolute():
            raise ValueError('cleanup declaration copy path is unsafe')
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(current.read_bytes())
        os.chmod(destination, current.stat().st_mode & 0o777)

    def apply_integration(self, command, source, patch_path, changed_paths):
        from .development import _git, _owned, _assert_regular_workspace
        self.check_source(command, source)
        if patch_path.stat().st_size:
            _git(command, source, 'apply', '--index', '--3way', '--', str(patch_path))
            changed = _git(command, source, 'diff', '--cached', '--name-only',
                           '--no-renames', '-z', '--').stdout
            _owned([name for name in changed.split('\0') if name],
                   {'id': 'code-cleanup', 'owned_paths': changed_paths}, gates_disabled=True)
            _assert_regular_workspace(source)
            _git(command, source, 'commit', '--no-gpg-sign', '-m',
                 'Integrate development task code-cleanup', '--only', '--', *changed_paths)
        self.check_source(command, source)

    def rollback_integration(self, command, source, before, changed_paths):
        from .development import _git, _head
        if _head(command, source) != before:
            _git(command, source, 'reset', '--soft', before)
        elif not _git(command, source, 'diff', '--cached', '--name-only', '--').stdout:
            # Pre-integration validation may fail before this patch touches the
            # index. Keep any newly arrived user edits in place in that case.
            return
        if changed_paths:
            # Restore only this published patch's files. Host-normalized tracked
            # declarations and generated evidence never enter this path list.
            _git(command, source, 'restore', '--source=' + before, '--staged',
                 '--worktree', '--', *changed_paths)

    @staticmethod
    def _checkpoint_path(root):
        directory = _ensure_run_directory(root, root / 'artifacts' / 'final-cleanup')
        path = directory / 'checkpoint.json'
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('final cleanup checkpoint path is unsafe')
        return path

    def prepare_integration(self, command, root, **record):
        # Frozen tests and their declaration are never rewritten by final cleanup.
        ref = command.artifact_refs.get('functional_contract_lock')
        if not isinstance(ref, Mapping):
            raise ValueError('final cleanup requires the frozen target contract')
        lock = json.loads(verified_path(root, ref).read_text(encoding='utf-8'))
        contract = lock['contract']
        protected = {'.modport/functional-contract.json'}
        for declaration in contract.get('test_evidence', {}).values():
            protected.update(declaration.get('test_source_files', []))
        changed = record['changed_paths']
        forbidden = [path for path in changed if path in protected
                     or path.startswith('.modport/')]
        if forbidden:
            raise ValueError('final cleanup changed frozen tests or evidence: ' + ', '.join(forbidden))
        atomic_json(self._checkpoint_path(root), {'phase': 'prepared',
            'execution_id': command.command_id, **record})

    @staticmethod
    def _rebind(command, result):
        return replace(result, run_id=command.run_id, task_id=command.task_id,
                       stage_id=command.stage_id, command_id=command.command_id)

    def _recover_prepared(self, command, root, record):
        from .development import _apply, _clean, _git, _head
        source = project_path(root, 'worktree')
        if (source.is_symlink() or source.resolve() != source.absolute()
                or not source.is_dir() or not is_project_workspace(root, source)):
            raise ValueError('final cleanup recovery worktree is unsafe')
        before = record['before']
        patch = verified_path(root, record['patch_ref'])
        expected_delta = patch.read_bytes()

        def delta(end):
            return _git(command, source, 'diff', '--binary', '--full-index',
                '--no-ext-diff', '--no-textconv', '--no-renames', before, end, '--').stdout.encode('utf-8')

        current = _head(command, source)
        if current == before:
            dirty = _git(command, source, 'status', '--porcelain', '-z', '--untracked-files=all').stdout
            if dirty:
                entries = [entry for entry in dirty.split('\0') if entry]
                preserved = self._preserved_paths(command)
                entries = [entry for entry in entries if entry[3:] not in preserved]
                # Only a partially applied host patch is eligible for rollback.
                # Unexpected files remain intact and require explicit recovery.
                staged = _git(command, source, 'diff', '--cached', '--binary', '--full-index',
                    '--no-ext-diff', '--no-textconv', '--no-renames', before, '--').stdout.encode('utf-8')
                unstaged = [name for name in _git(command, source, 'diff', '--name-only', '--').stdout.splitlines()
                            if name not in preserved]
                if (entries and staged != expected_delta or unstaged
                        or any(entry[:2] == '??' or entry[3:] not in record['changed_paths'] for entry in entries)):
                    raise ValueError('final cleanup integration has unrelated pending changes')
                if entries:
                    self.rollback_integration(command, source, before, record['changed_paths'])
            self.check_source(command, source)
            self.apply_integration(command, source, patch, record['changed_paths'])
            current = _head(command, source)
        else:
            parent = _git(command, source, 'rev-parse', 'HEAD^').stdout.strip()
            subject = _git(command, source, 'log', '-1', '--format=%s').stdout.strip()
            if (parent != before or subject != 'Integrate development task code-cleanup'
                    or delta(current) != expected_delta):
                raise ValueError('final cleanup recovery cannot identify its integration commit')
            self.check_source(command, source)
        agent = OperationResult.from_dict(record['agent_result'])
        refs = dict(agent.outputs.get('artifact_refs', {}))
        refs['code_cleanup_patch'] = record['patch_ref']
        if isinstance(record.get('report_ref'), Mapping):
            refs['code_cleanup_report'] = record['report_ref']
        outputs = {**agent.outputs, 'artifact_refs': refs, 'candidate_before': before,
            'candidate_collected': record['collected'], 'candidate_after': current,
            'changed_paths': record['changed_paths'], 'cleanup_workspace': record['workspace'],
            'acceptance_status': 'unverified', 'cleanup_recovered': True}
        return handlers._result(command, 'completed', outputs=outputs,
            detail='final cleanup integration recovered from its published patch')

    def __call__(self, command):
        if command.options.get('validation_policy', {}).get('scope') == 'artifact_verification':
            return handlers._result(command, 'failed', error_code='artifact_product_immutable',
                detail='final cleanup cannot modify an artifact verification product')
        root = Path(command.run_dir).resolve()
        try:
            path = self._checkpoint_path(root)
            with workspace_lock(root / '.locks' / 'final-cleanup', blocking=False):
                return self._execute_checkpoint(command, root, path)
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            return handlers._result(command, 'failed', error_code='cleanup_recovery_required',
                detail=str(exc), outputs={'acceptance_status': 'unverified'})

    def _execute_checkpoint(self, command, root, path):
        try:
            checkpoint = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            if checkpoint.get('phase') == 'settled':
                result = self._rebind(command, OperationResult.from_dict(checkpoint['result']))
                return replace(result, outputs={**result.outputs, 'cleanup_replayed': True})
            if checkpoint.get('phase') == 'prepared':
                result = self._recover_prepared(command, root, checkpoint)
            else:
                result = super().__call__(command)
            atomic_json(path, {'phase': 'settled', 'execution_id': command.command_id,
                'result': result.to_dict()})
            return result
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
            return handlers._result(command, 'failed', error_code='cleanup_recovery_required',
                detail=str(exc), outputs={'acceptance_status': 'unverified'})
