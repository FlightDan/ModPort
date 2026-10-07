"""Versioned deterministic preparation: facts precede agent planning."""

from .workspace import project_path
from dataclasses import replace
import json
from pathlib import Path
import subprocess

from .evidence import verified_path, file_digest


def _trusted_path(root, ref):
    path = verified_path(root, ref)
    if not isinstance(ref.get('sha256'), str) or file_digest(path) != ref['sha256']:
        raise ValueError('deterministic evidence digest mismatch')
    return path


def _store(command, name, value):
    from .development import _artifact
    return _artifact(command, name, (json.dumps(value, sort_keys=True,
        ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8'))


def _record(command, alias):
    ref = command.artifact_refs.get(alias)
    if ref is None:
        return None
    path = _trusted_path(Path(command.run_dir), ref)
    if path.stat().st_size > 12 * 1024 * 1024:
        raise ValueError('deterministic record exceeds size limit')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('deterministic record must be an object')
    return value


class BuildPrepareHandler:
    """Add only missing, authenticated MDK files; preserve custom builds."""

    def __call__(self, command):
        from . import handlers
        from .development import _artifact, _clean, _head, _git
        from .build_preparation import (
            draft_build_preparation, apply_build_preparation, revert_build_preparation,
        )
        root = handlers._run_root(command)
        work = project_path(root, 'worktree')
        refs, applied, committed, plan, manifest = {}, None, None, None, None
        try:
            _clean(command, work)
            before = _head(command, work)
            manifest = _record(command, 'locked_manifest')
            if manifest is None:
                return handlers._result(command, 'failed', error_code='build_manifest_missing',
                    detail='Locked MDK input is unavailable',
                    outputs={'product_state': 'unavailable', 'acceptance_status': 'unverified'})
            plan = draft_build_preparation(root, work, manifest)
            refs['build_preparation'] = _store(command, 'build-preparation.json', plan.to_dict())
            refs['build_preparation_draft'] = _artifact(command, 'build-preparation-draft.txt', plan.patch.encode())
            if not plan.supported:
                semantic_merge = all(diagnostic.startswith((
                    'custom or multi-project Gradle layout is unsupported:',
                    'existing configuration differs from locked MDK and will not be overwritten:',
                )) for diagnostic in plan.diagnostics)
                diagnostic_only = command.options.get('workflow_version', 0) >= 26 and semantic_merge
                return handlers._result(command, 'completed' if diagnostic_only else 'failed',
                    error_code=None if diagnostic_only else 'build_merge_unsupported',
                    detail=('Custom build requires semantic merging; original files preserved'
                            if semantic_merge else 'Locked MDK input is unsupported or incomplete'),
                    outputs={'product_state': 'unavailable', 'candidate_commit': before,
                             'preparation_status': ('manual_merge_required' if semantic_merge
                                                    else 'locked_mdk_unavailable'),
                             'diagnostic_code': 'build_merge_unsupported',
                             'diagnostics': list(plan.diagnostics), 'artifact_refs': refs,
                             'acceptance_status': 'unverified'})
            applied = apply_build_preparation(root, work, manifest, plan)
            if applied.changed_paths:
                _git(command, work, 'add', '--', *applied.changed_paths)
                patch = _git(command, work, 'diff', '--cached', '--binary', '--full-index', before,
                             '--', *applied.changed_paths).stdout
                refs['build_preparation_patch'] = _artifact(command, 'build-preparation.patch', patch.encode())
                _git(command, work, 'commit', '--no-gpg-sign', '-m', 'Add authenticated missing MDK configuration')
                committed = _head(command, work)
            _clean(command, work)
            after = _head(command, work)
            refs['build_preparation_result'] = _store(command, 'build-preparation-result.json', {
                'before_commit': before, 'after_commit': after, 'result': applied.to_dict(),
                'acceptance_evidence': False})
            return handlers._result(command, 'completed', outputs={
                'product_state': 'produced' if applied.changed_paths else 'empty_valid',
                'candidate_commit': after, 'artifact_refs': refs, 'acceptance_status': 'unverified'})
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
            recovery = None
            if applied is not None and applied.changed_paths:
                try:
                    current = _head(command, work)
                    if current != before:
                        if current != committed:
                            raise ValueError('candidate moved outside the build preparation transaction')
                        _git(command, work, 'update-ref', 'HEAD', before, current)
                    # A path-only reset removes just our staged additions;
                    # revert then verifies their bytes before removing them.
                    _git(command, work, 'reset', before, '--', *applied.changed_paths)
                    revert_build_preparation(root, work, manifest, plan)
                    _clean(command, work)
                    recovery = {'status': 'restored', 'candidate_commit': before}
                except (OSError, ValueError, subprocess.TimeoutExpired) as rollback_error:
                    recovery = {'status': 'recovery_required', 'detail': str(rollback_error)}
                refs['build_preparation_recovery'] = _store(command, 'build-recovery.json', recovery)
            return handlers._result(command, 'failed', error_code='build_preparation_integrity',
                detail=str(error), outputs={'artifact_refs': refs, 'recovery': recovery,
                                           'product_state': 'unavailable', 'acceptance_status': 'unverified'})


class CodemodHandler:
    """Apply only the explicitly enabled, exact-version mechanical rules."""

    def __call__(self, command):
        from . import handlers
        from .codemod import (VersionIdentity, plan_codemod, apply_codemod,
                              eventbus_import_rules, CodemodConflictError)
        from .development import _artifact, _clean, _head, _git
        from .skill_runtime import resolve_skill_inputs
        root = handlers._run_root(command)
        work = project_path(root, 'worktree')
        prepared_refs = {}
        applied = None
        before = None
        committed = None
        try:
            _clean(command, work)
            before = _head(command, work)
            scan = _record(command, 'mod_scan_report')
            if scan is None:
                return handlers._result(command, 'failed', error_code='codemod_scan_missing',
                    detail='No authenticated pre-transform scan is available',
                    outputs={'product_state': 'unavailable', 'acceptance_status': 'unverified'})
            if scan.get('candidate_identity') != {'kind': 'git_commit', 'value': before}:
                raise ValueError('scan candidate differs from codemod input')
            if command.options.get('workflow_version', 0) >= 20:
                from .dependency_symbols import index_frozen_dependencies
                symbols = index_frozen_dependencies(root, command.artifact_refs)
                prepared_refs['dependency_symbols'] = _store(command, 'dependency-symbols.json', symbols)
            try:
                identity = resolve_skill_inputs(command)['identities']['platform']
            except (OSError, ValueError, TypeError, KeyError) as error:
                record = {'schema_version': 1, 'product_state': 'unavailable',
                          'before_commit': before, 'after_commit': before, 'diagnostics': [str(error)],
                          'acceptance_evidence': False}
                return handlers._result(command, 'failed', error_code='codemod_identity_missing',
                    detail=str(error), outputs={'product_state': 'unavailable',
                    'artifact_refs': {'codemod': _store(command, 'codemod.json', record)}})
            rules = eventbus_import_rules(mode='transform')
            if command.options.get('workflow_version', 0) >= 20:
                from .codemod import audited_import_rules
                rules = audited_import_rules(mode='transform')
            plan = plan_codemod(work,
                source_identity=VersionIdentity(**identity['source']),
                target_identity=VersionIdentity(**identity['target']),
                rules=rules)
            # Persist the full plan and reversible patch before changing any file.
            plan_ref = _store(command, 'codemod-plan.json', plan.to_dict())
            patch_ref = _artifact(command, 'codemod.patch', plan.patch.encode('utf-8'))
            prepared_refs.update(codemod_plan=plan_ref, codemod_patch=patch_ref)
            applied = apply_codemod(work, plan)
            if applied.changed_paths:
                _git(command, work, 'add', '--', *applied.changed_paths)
                _git(command, work, 'commit', '--no-gpg-sign', '-m', 'Apply verified mechanical migration rules')
                committed = _head(command, work)
            _clean(command, work)
            after = _head(command, work)
            applicable = [rule.rule_id for rule in rules
                          if rule.source_identity == plan.source_identity
                          and rule.target_identity == plan.target_identity]
            product_state = ('produced' if applied.applied_changes else
                             'empty_valid' if applicable else 'unavailable')
            record = {'schema_version': 1, 'before_commit': before, 'after_commit': after,
                      'rules_sha256': plan.rules_sha256, 'input_sha256': plan.input_sha256,
                      'result': applied.to_dict(), 'plan_ref': plan_ref, 'patch_ref': patch_ref,
                      'applicable_rules': applicable,
                      'product_state': product_state,
                      'acceptance_evidence': False}
            return handlers._result(command, 'completed' if applicable else 'failed',
                error_code=None if applicable else 'codemod_rules_unavailable', outputs={
                'product_state': record['product_state'], 'candidate_commit': after,
                'applied_changes': applied.applied_changes,
                'artifact_refs': {**prepared_refs, 'codemod': _store(command, 'codemod.json', record),
                                  'codemod_plan': plan_ref, 'codemod_patch': patch_ref},
                'acceptance_status': 'unverified'})
        except CodemodConflictError as error:
            # Planning conflicts do not change the candidate and are semantic
            # work for the downstream agent, not an automatic repair trigger.
            _clean(command, work)
            record = {'schema_version': 1, 'before_commit': before, 'after_commit': before,
                      'product_state': 'unavailable', 'diagnostics': [str(error)]}
            return handlers._result(command, 'failed', error_code='codemod_conflict', detail=str(error),
                outputs={'artifact_refs': {**prepared_refs, 'codemod': _store(command, 'codemod.json', record)},
                         'product_state': 'unavailable', 'acceptance_status': 'unverified'})
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
            recovery = None
            if applied is not None and applied.changed_paths:
                try:
                    # Restore only files this invocation changed, never an
                    # entire checkout or another author's untracked output.
                    current = _head(command, work)
                    if current != before:
                        if current != committed:
                            raise ValueError('candidate moved outside this codemod transaction')
                        _git(command, work, 'update-ref', 'HEAD', before, current)
                    _git(command, work, 'restore', '--source=' + before,
                         '--staged', '--worktree', '--', *applied.changed_paths)
                    _clean(command, work)
                    recovery = {'status': 'restored', 'candidate_commit': before}
                except (OSError, ValueError, subprocess.TimeoutExpired) as rollback_error:
                    recovery = {'status': 'recovery_required', 'detail': str(rollback_error),
                                'candidate_commit': before, 'paths': list(applied.changed_paths)}
                prepared_refs['codemod_recovery'] = _store(command, 'codemod-recovery.json', recovery)
            return handlers._result(command, 'failed', error_code='codemod_input_invalid', detail=str(error),
                outputs={'artifact_refs': prepared_refs, 'product_state': 'unavailable',
                         'recovery': recovery,
                         'acceptance_status': 'unverified'})


class EarlyCompileHandler:
    """Compile the target without running datagen, tests, or project scripts on host."""

    def __call__(self, command):
        from . import handlers
        from .development import _clean, _head
        root = handlers._run_root(command)
        work = project_path(root, 'worktree')
        try:
            _clean(command, work)
            before = _head(command, work)
            codemod = _record(command, 'codemod')
            if codemod is None:
                return handlers._result(command, 'failed', error_code='codemod_evidence_missing',
                    outputs={'process_executed': False, 'product_state': 'unavailable'},
                    detail='No authenticated codemod candidate is available')
            if command.options.get('workflow_version', 0) >= 20:
                preparation = _record(command, 'build_preparation_result')
                if preparation is not None:
                    if (preparation.get('before_commit') != codemod.get('after_commit')
                            or preparation.get('after_commit') != before):
                        raise ValueError('build preparation does not connect codemod and compile candidates')
                elif codemod.get('after_commit') != before:
                    raise ValueError('early compile candidate differs from codemod output')
            elif codemod.get('after_commit') != before:
                raise ValueError('early compile candidate differs from codemod output')
        except (OSError, ValueError) as error:
            return handlers._result(command, 'failed', detail=str(error),
                                    error_code='candidate_identity_mismatch')
        # A plugin name (including one in a comment) is not target provenance.
        from .build_preparation import authenticate_target_config, UnsupportedBuildLayout
        marker, configuration_diagnostics, manifest = None, [], None
        try:
            manifest = _record(command, 'locked_manifest')
            if manifest is not None:
                marker = authenticate_target_config(work, manifest, root / 'toolchains/mdk')
            else:
                configuration_diagnostics.append('locked manifest unavailable')
        except UnsupportedBuildLayout as error:
            configuration_diagnostics.extend(error.diagnostics)
        except (OSError, ValueError, TypeError, KeyError) as error:
            return handlers._result(command, 'failed', detail=str(error),
                                    error_code='candidate_identity_mismatch')
        if marker is not None:
            try:
                result = handlers.GradleHandler(baseline=False,
                    tasks=tuple('-P' + key + '=' + value
                        for key, value in marker.required_gradle_properties) + ('compileJava',),
                    name='early-compile')(command)
            except (OSError, ValueError, RuntimeError, TypeError, subprocess.TimeoutExpired) as error:
                result = handlers._result(command, 'failed', error_code='candidate_identity_mismatch',
                    detail='Compile handler failed before authenticated settlement: ' + str(error),
                    outputs={'process_executed': None})
        elif command.options.get('workflow_version', 0) >= 20 and manifest is not None:
            from .source_compile_probe import compile_source_probe
            try:
                result = compile_source_probe(command, manifest)
            except (OSError, ValueError, RuntimeError, TypeError, subprocess.TimeoutExpired) as error:
                result = handlers._result(command, 'failed', error_code='candidate_identity_mismatch',
                    detail='Source compile probe could not settle safely: ' + str(error),
                    outputs={'process_executed': None})
        else:
            result = handlers._result(command, 'failed', error_code='target_build_configuration_missing',
                detail='Target build configuration is not recognized; preserve project customizations '
                       'and merge the locked MDK before target compilation.',
                outputs={'process_executed': False, 'product_state': 'unavailable',
                         'diagnostics': configuration_diagnostics,
                         'acceptance_status': 'unverified'})
        outputs = dict(result.outputs)
        from .development import _artifact
        frozen_refs = {}
        try:
            for index, (name, ref) in enumerate(outputs.get('artifact_refs', {}).items()):
                path = _trusted_path(root, ref)
                if path.stat().st_size > 32 * 1024 * 1024:
                    result = replace(result, status='failed', error_code='compile_evidence_too_large',
                                     detail='Compile evidence exceeds the bounded capture limit')
                    continue
                frozen_refs[name] = _artifact(command, f'compile-evidence-{index}.log', path.read_bytes())
        except (OSError, ValueError, TypeError, KeyError) as error:
            result = replace(result, status='failed', error_code='candidate_identity_mismatch',
                             detail='Compile evidence could not be authenticated: ' + str(error))
        outputs['artifact_refs'] = frozen_refs
        record = {'schema_version': 1, 'candidate_commit': before,
                  'tasks': ['compileJava'], 'process_executed': outputs.get('process_executed', False),
                  'status': result.status, 'error_code': result.error_code,
                  'diagnostic_code': outputs.get('diagnostic_code'),
                  'probe_returncode': outputs.get('probe_returncode'),
                  'artifact_refs': outputs.get('artifact_refs', {}),
                  'target_configuration': marker.to_dict() if marker else None,
                  'compile_scope': outputs.get('compile_scope',
                      'authenticated_project_configuration' if marker else 'unavailable'),
                  'project_build_verified': False,
                  'project_compile_verified': bool(marker) and result.status == 'completed',
                  'acceptance_evidence': False}
        try:
            _clean(command, work)
            if _head(command, work) != before:
                raise ValueError('compile candidate changed')
            if marker is not None:
                authenticate_target_config(work, manifest, root / 'toolchains/mdk', expected_marker=marker)
        except (OSError, ValueError) as error:
            record['candidate_unchanged'] = False
            result = replace(result, status='failed', error_code='candidate_identity_mismatch',
                             detail=str(error))
        else:
            record['candidate_unchanged'] = True
        record.update(status=result.status, error_code=result.error_code)
        outputs['candidate_commit'] = before
        outputs.setdefault('artifact_refs', {})['early_compile'] = _store(
            command, 'early-compile.json', record)
        return replace(result, outputs=outputs)


class DeterministicInventoryHandler:
    """Keep scanner evidence even when later model triage is empty or malformed."""

    def __call__(self, command):
        from . import handlers
        from .development import _head, _clean
        from .repair_inventory import collect_inventory, MAX_LOG_BYTES
        root = handlers._run_root(command)
        diagnostics, missing, log_texts, retained = [], [], {}, {}
        try:
            _clean(command, project_path(root, 'worktree'))
            candidate = _head(command, project_path(root, 'worktree'))
            scan_ref = command.artifact_refs.get('mod_scan_report')
            if scan_ref:
                scan_path = _trusted_path(root, scan_ref)
                scan = json.loads(scan_path.read_text(encoding='utf-8'))
                retained['mod_scan_report'] = scan_ref
                if scan.get('scan_complete') is not True:
                    diagnostics.append('version-specific scan incomplete')
            else:
                scan = None
                missing.append('mod_scan_report')
            compile_result = command.upstream_results.get('early_compile', {})
            compile_outputs = compile_result.get('outputs', {})
            preparation_outputs = command.upstream_results.get('build_prepare', {}).get('outputs', {})
            if preparation_outputs.get('preparation_status') == 'manual_merge_required':
                diagnostics.append('build preparation requires semantic merge; original project build is unverified')
            compile_record = _record(command, 'early_compile')
            compiled_candidate = compile_record.get('candidate_commit') if compile_record else None
            if compile_outputs.get('candidate_commit') and not compile_record:
                raise ValueError('early compile output lacks authenticated evidence')
            if compiled_candidate and candidate != compiled_candidate:
                raise ValueError('inventory candidate differs from early compile')
            for name, ref in (compile_record or {}).get('artifact_refs', {}).items():
                path = _trusted_path(root, ref)
                retained[name] = ref
                if name.startswith('gradle_log:') or name == 'execution_log':
                    if path.stat().st_size > MAX_LOG_BYTES:
                        missing.append('compile log exceeds inventory limit')
                    else:
                        log_texts[name] = path.read_text(encoding='utf-8', errors='replace')
            if not (compile_record or {}).get('process_executed'):
                missing.append('target compilation not executed')
            elif (compile_record or {}).get('compile_scope') == 'provisional_mdk_source_compile':
                missing.append('only provisional MDK source compilation executed; project build remains unverified')
                code = (compile_record or {}).get('diagnostic_code')
                if code:
                    diagnostics.append('provisional source compile diagnostic: ' + str(code))
            inventory = collect_inventory(project_path(root, 'worktree'), candidate_id=candidate,
                execution_id=command.command_id, log_texts=log_texts or None,
                missing_inputs=missing)
            _clean(command, project_path(root, 'worktree'))
            if _head(command, project_path(root, 'worktree')) != candidate:
                raise ValueError('inventory candidate changed during collection')
            inventory.update(source_scan_ref=scan_ref, source_scan_complete=(
                scan.get('scan_complete', False) if scan else False),
                diagnostic_refs=retained, diagnostics=diagnostics,
                product_state='produced', acceptance_evidence=False)
            if command.options.get('workflow_version', 0) >= 20:
                from .dependency_symbols import index_frozen_dependencies
                symbols = _record(command, 'dependency_symbols')
                expected_seed = command.artifact_refs.get('dependency_repository', {}).get('sha256')
                if symbols is not None and symbols.get('dependency_manifest_sha256') != expected_seed:
                    raise ValueError('dependency symbol index differs from frozen seed')
                if symbols is None:
                    symbols = index_frozen_dependencies(root, command.artifact_refs)
                    symbol_ref = _store(command, 'dependency-symbols.json', symbols)
                else:
                    symbol_ref = command.artifact_refs['dependency_symbols']
                inventory['dependency_symbols_ref'] = symbol_ref
                inventory['dependency_symbol_coverage'] = {
                    'scope': symbols['scope'], 'artifacts_indexed': len(symbols['artifacts']),
                    'effective_classpath_verified': False, 'diagnostics': symbols['diagnostics']}
                retained['dependency_symbols'] = symbol_ref
            ref = _store(command, 'migration-inventory.json', inventory)
            return handlers._result(command, 'completed', outputs={
                'candidate_commit': candidate, 'product_state': 'produced',
                'diagnostics': diagnostics + missing,
                'artifact_refs': {**retained, 'migration_inventory': ref},
                'acceptance_status': 'unverified'})
        except (OSError, ValueError, TypeError, KeyError) as error:
            return handlers._result(command, 'failed', detail=str(error),
                                    error_code='candidate_identity_mismatch')


class VersionedInventoryHandler:
    def __init__(self, legacy):
        self.legacy = legacy

    def __call__(self, command):
        if command.options.get('workflow_version', 0) >= 19:
            return DeterministicInventoryHandler()(command)
        return self.legacy(command)
