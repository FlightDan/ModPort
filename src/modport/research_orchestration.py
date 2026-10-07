"""Project gap decisions and administrator waits over public host operations."""
from .contracts import json_copy, OperationInput, OperationResult
from .research_policy import initialize_research, observe_attempts
from .project_gaps import merge_reviewed_requirements, merge_verification_obligation
from .analysis_contract import HOST_GAP_FIELDS
from .gate_policy import downstream_toolcall


def gap_identity(row):
    return row.get('gap_id') or ((row.get('kind', 'knowledge') + ':' + row['entry_id']) if row.get('entry_id') else row['skill'] + ':' + str(row.get('index')))


class ResearchOrchestration:
    @staticmethod
    def _ingest_reviewed_requirements(app, outcome, execution_id, *, normalize_duplicates=False):
        app['project_verification_gaps'] = merge_reviewed_requirements(
            app.get('project_verification_gaps', {}),
            outcome.outputs.get('verification_requirements', []), execution_id,
            app.get('project_research_gaps', {}), normalize_duplicates=normalize_duplicates)

    def _effective_deadline(self, header, app):
        # A same-Run SDK recovery records its explicitly authorized execution
        # window in application state.  It must be absolute and durable so a
        # host restart cannot silently grant another window.  Ordinary Runs
        # continue to derive their deadline from the immutable header.
        deadline = app.get('recovery_deadline_epoch', header.get('deadline_epoch'))
        if deadline is None:
            return None
        if header.get('definition', {}).get('workflow_version', 0) >= 19:
            return deadline
        paused = app.get('administrator_wait_seconds', 0)
        waiting = app.get('administrator_wait')
        if waiting:
            paused += max(0, self.clock() - waiting['started_at'])
        return deadline + paused

    @staticmethod
    def _canonical_analysis_rows(app, rows, records):
        """Keep historical host keys and publish every new spelling as an alias."""
        incoming = {}
        aliases = dict(app.get('gap_identity_aliases', {}))
        for row in rows:
            identity = gap_identity(row)
            def same_owner(old):
                return bool(old.get('entry_id')) and all(old.get(field) == row.get(field)
                    for field in ('kind', 'skill', 'entry_id'))
            matches = [key for key, old in records.items() if same_owner(old)]
            if identity in records and records[identity].get('entry_id') and row.get('entry_id') and not same_owner(records[identity]):
                raise ValueError(f'gap_id {identity} belongs to another historical skill entry; use kind:skill:entry_id')
            canonical = aliases.get(identity)
            if canonical is not None and row.get('entry_id') and not same_owner(records.get(canonical, {})):
                raise ValueError('gap identity alias belongs to another historical skill entry')
            if canonical is None and identity not in records and len(matches) == 1:
                canonical = matches[0]
                aliases[identity] = canonical
            canonical = canonical or identity
            if canonical in incoming:
                raise ValueError('analysis duplicates a historical gap identity')
            incoming[canonical] = {**json_copy(row), 'gap_id': canonical}
        app['gap_identity_aliases'] = aliases
        return incoming

    def _ingest_analysis(self, app, outputs):
        research = outputs.get('project_research_gaps', outputs.get('unresolved_relevant_gaps', []))
        verification = outputs.get('project_verification_gaps', outputs.get('deferred_verification_gaps', []))
        app['unresolved_knowledge_gaps'] = json_copy(outputs.get('unresolved_relevant_gaps', research))
        # Treat analysis as untrusted even when a caller bypasses its validator.
        research = [{key: json_copy(value) for key, value in row.items() if key not in HOST_GAP_FIELDS}
                    for row in research]
        verification = [{key: json_copy(value) for key, value in row.items() if key not in HOST_GAP_FIELDS}
                        for row in verification]
        records = app.setdefault('project_research_gaps', {})
        incoming = self._canonical_analysis_rows(app, research, records)
        if incoming != app.get('analysis_gap_rows', {}):
            app['gap_revision'] = app.get('gap_revision', 0) + 1
        analysis_rows = json_copy(incoming)
        previous = app.get('analysis_gap_rows', {})
        records = app.setdefault('project_research_gaps', {})
        for identity, row in incoming.items():
            old = records.get(identity, {})
            unchanged = previous.get(identity) == row
            same_inputs = all(previous.get(identity, old).get(key) == row.get(key) for key in
                              ('usage_locations', 'affected_tasks', 'closure_criteria', 'question'))
            row.update(gap_id=identity, project_status=(old.get('project_status', 'unresolved')
                       if unchanged else (row.get('status') if row.get('status') in {'resolved', 'not_applicable'} else 'unresolved')),
                       attempted_alternatives=old.get('attempted_alternatives', []))
            if same_inputs and old.get('project_status') in {'bypassed', 'resolved', 'not_applicable'} and old.get('review_execution_id'):
                row['project_status'] = old['project_status']
            if unchanged or (same_inputs and old.get('review_execution_id')):
                for field in ('resolution', 'review_execution_id'):
                    if field in old:
                        row[field] = old[field]
            if old.get('project_status') == 'unresolved' and not old.get('review_execution_id') and row['project_status'] != 'unresolved':
                row['project_status'] = 'unresolved'
            if old and not same_inputs:
                row['project_status'] = 'unresolved'
                row.pop('review_execution_id', None)
                row.pop('resolution', None)
            records[identity] = row
        # A valid reassessment may answer or dismiss an earlier project gap.
        for identity in set(previous) - set(incoming):
            if identity in records and records[identity].get('review_execution_id'):
                records[identity]['project_status'] = records[identity].get('project_status', 'resolved')
        app['analysis_gap_rows'] = analysis_rows
        obligations = app.setdefault('project_verification_gaps', {})
        for identity, row in self._canonical_analysis_rows(app, verification, obligations).items():
            obligations[identity] = merge_verification_obligation(obligations.get(identity), row)
        app['unresolved_knowledge_gaps'] = [dict(row) for row in records.values() if row.get('project_status') == 'unresolved']
        app['known_knowledge_gap_ids'] = sorted(set(app.get('known_knowledge_gap_ids', [])) | set(records))

    def _apply_gap_resolutions(self, app, resolutions, execution_id):
        records = app.setdefault('project_research_gaps', {})
        obligations = app.setdefault('project_verification_gaps', {})
        for resolution in resolutions:
            if 'project_status' in resolution and any(field in resolution for field in
                    ('action', 'alternative_id', 'verification_requirements')):
                raise ValueError('research dispositions cannot authorize gap-plan actions or nested verification')
        for resolution in resolutions:
            identity = app.get('gap_identity_aliases', {}).get(resolution['gap_id'], resolution['gap_id'])
            if identity not in records:
                continue
            row = records[identity]
            action = resolution.get('action')
            alternative_id = resolution.get('alternative_id')
            if alternative_id:
                attempts = row.setdefault('attempted_alternatives', [])
                if not any(item.get('alternative_id') == alternative_id for item in attempts):
                    attempts.append(json_copy(resolution))
            status = resolution.get('project_status')
            if action:
                status = 'unresolved' if action == 'wait_admin' else 'bypassed'
            if status in {'resolved', 'not_applicable', 'bypassed', 'unresolved'}:
                row.update(project_status=status, resolution=json_copy(resolution),
                           review_execution_id=execution_id)
            for index, requirement in enumerate(resolution.get('verification_requirements', [])):
                key = 'verify:' + identity + ':' + str(requirement.get('id', index))
                obligations[key] = {**json_copy(requirement), 'gap_id': key,
                    'research_gap_id': identity, 'kind': 'verification', 'project_status': 'pending',
                    'affected_tasks': resolution.get('affected_tasks', row.get('affected_tasks', [])),
                    'status': 'unresolved', 'applicable': True}
        app['approved_gap_resolutions'] = list(app.get('approved_gap_resolutions', [])) + json_copy(resolutions)

    def _support_payload(self, app):
        group = app.get('active_group') or {}
        tasks = group.get('tasks') or app.get('effective', {}).get('implementation', {}).get('outputs', {}).get('development_tasks', [])
        return {'development_tasks': tasks,
                'completed_development_results': {task['id']: group.get('results', {})['coder.g' + str(group.get('generation')) + '.' + task['id']]
                    for task in tasks if 'coder.g' + str(group.get('generation')) + '.' + task['id'] in group.get('results', {})},
                'attempted_gap_alternatives': [item for row in app.get('project_research_gaps', {}).values()
                                             for item in row.get('attempted_alternatives', [])]}

    def _schedule_support(self, snapshot, header, app, stage, *, payload=None, causation_id=None):
        operations = self._schedule(snapshot, header, app, stage, activate=False,
            dependencies=[], payload={**self._support_payload(app), **(payload or {})}, causation_id=causation_id)
        app.setdefault('support_pending', []).extend(op['task_id'] for op in operations if op['kind'] == 'dispatch')
        return operations

    def _maybe_gap_plan(self, snapshot, header, app):
        if downstream_toolcall(header):
            return []
        if app.get('support_pending') or app.get('gap_pending'):
            return []
        gaps = self._knowledge_gaps(app)
        if not gaps:
            return []
        signature = {'gap_revision': app.get('gap_revision', 0),
                     'gaps': sorted(gap_identity(row) for row in gaps),
                     'tasks': [task['id'] for task in self._support_payload(app)['development_tasks']]}
        if app.get('last_gap_plan') == signature:
            return []
        app['last_gap_plan'] = signature
        return self._schedule_support(snapshot, header, app, 'gap_plan')

    def _release_administrator_wait(self, snapshot, app):
        wait = app.pop('administrator_wait', None)
        if not wait:
            return []
        app['administrator_wait_seconds'] = app.get('administrator_wait_seconds', 0) + max(0, self.clock() - wait['started_at'])
        existing = snapshot.get('waits', {}).get(wait['wait_id'])
        return [{'kind': 'release_wait', 'wait_id': wait['wait_id']}] if existing and existing['state'] == 'open' else []

    def _park_administrator(self, snapshot, header, app, reason='research_requires_administrator'):
        if app.get('support_pending') or app.get('gap_pending'):
            return []
        # Creating a Run wait while another task can execute would stop the host.
        if any(task['attempts'][-1]['state'] not in {'succeeded', 'failed', 'cancelled', 'dead', 'timed_out'}
               for task in snapshot['tasks'].values()):
            return []
        if app.get('administrator_wait'):
            return []
        now = self.clock()
        serial = app.get('administrator_wait_count', 0) + 1
        app['administrator_wait_count'] = serial
        wait_id = 'administrator:' + str(serial)
        duration = header['request'].get('admin_wait_seconds')
        data = {'wait_id': wait_id, 'started_at': now,
                'deadline_epoch': None if duration is None else now + duration,
                'gap_ids': [gap_identity(row) for row in self._knowledge_gaps(app)], 'reason': reason}
        app['administrator_wait'] = data
        return [{'kind': 'wait', 'wait_id': wait_id, 'payload': data}]

    def _support_decision(self, snapshot, header, app):
        from .business_policy import business_gates_disabled
        advisory = business_gates_disabled(header)
        operations = []
        pending = app.setdefault('support_pending', [])
        for task_id in list(pending):
            task = snapshot['tasks'].get(task_id)
            if not task and task_id not in app.get('gate_resumptions', {}):
                continue
            attempt, resumed = self._resumed_attempt(snapshot, app, task_id,
                task['attempts'][-1] if task else {})
            if attempt['state'] not in {'succeeded', 'failed', 'cancelled', 'dead', 'timed_out'}:
                continue
            pending.remove(task_id)
            execution_id = attempt['command']['execution_id']
            if execution_id in app['processed'] and not resumed:
                continue
            if not resumed:
                app['processed'].append(execution_id)
            command = OperationInput.from_dict(attempt['command']['payload'])
            outcome = (OperationResult.from_dict(attempt['result']['value']) if attempt['state'] == 'succeeded'
                       else OperationResult('failed', command.run_id, task_id, command.stage_id, execution_id,
                                            error_code='support_' + attempt['state']))
            outcome.validate_for(command)
            app['effective'][task_id] = outcome.to_dict()
            app['history'].append({'stage': command.stage_id, 'execution_id': execution_id, 'state': outcome.status})
            stage = command.stage_id
            default_verdict = None if advisory and stage in {'admin_review', 'gap_plan_review'} else 'approved'
            approved = outcome.status == 'completed' and outcome.outputs.get('verdict', default_verdict) == 'approved'
            if advisory and (outcome.status != 'completed' or not approved):
                self._flowthrough_diagnostic(app, outcome.to_dict())
            if downstream_toolcall(header) and not approved:
                if stage == 'admin_review':
                    identity = command.payload.get('admin_submission_id')
                    imported = app.get('admin_imports', {}).get(identity)
                    if imported is not None:
                        imported['status'] = 'diagnostic'
                operations += self._repair_failure(snapshot, header, app, stage, outcome,
                    execution_id, location='support')
                continue
            stale_submission = False
            if stage == 'admin_review':
                imported = app.get('admin_imports', {}).get(command.payload['admin_submission_id'], {})
                stale_submission = (imported.get('base_gap_revision') != app.get('gap_revision', 0)
                                    or imported.get('knowledge_revisions', {}) != app.get('knowledge_revisions', {}))
                approved = approved and not stale_submission
            if stage == 'gap_plan' and outcome.status == 'completed':
                app['last_gap_plan_resolutions'] = outcome.outputs.get('resolutions', [])
                operations += self._schedule_support(snapshot, header, app, 'gap_plan_review', causation_id=execution_id)
                continue
            if stage in {'gap_plan_review', 'admin_review'} and approved:
                resolutions = outcome.outputs.get('approved_gap_resolutions', [])
                self._apply_gap_resolutions(app, resolutions, execution_id)
                group = app.get('active_group')
                updates = outcome.outputs.get('approved_task_updates', [])
                if group and group.get('kind') == 'development' and updates:
                    group['pending_task_updates'] = json_copy(updates)
                    group['update_seeds'] = sorted({name for row in resolutions for name in row.get('affected_tasks', [])})
                app['gap_failure'] = None
                self._ingest_reviewed_requirements(app, outcome, execution_id,
                    normalize_duplicates=downstream_toolcall(header))
            if stage == 'gap_plan_review' and not approved:
                for resolution in app.get('last_gap_plan_resolutions', []):
                    row = app.get('project_research_gaps', {}).get(resolution['gap_id'])
                    if row is not None:
                        row.setdefault('attempted_alternatives', []).append({**resolution, 'review_status': 'rejected',
                            'findings': outcome.outputs.get('findings', []), 'review_execution_id': execution_id})
            if stage == 'admin_review':
                identity = command.payload['admin_submission_id']
                disposition = ('approved' if approved else 'diagnostic'
                               if advisory and outcome.outputs.get('verdict') != 'rejected' else 'rejected')
                app.setdefault('admin_imports', {})[identity]['status'] = 'stale' if stale_submission else disposition
                if approved:
                    app['gap_revision'] = app.get('gap_revision', 0) + 1
                    generic = outcome.outputs.get('approved_generic_knowledge_entries', {})
                    if generic:
                        operations += self._schedule_support(snapshot, header, app, 'knowledge_publish',
                            payload={'generic_knowledge_entries': generic}, causation_id=execution_id)
            if stage == 'knowledge_publish' and approved:
                app.setdefault('knowledge_revisions', {}).update(outcome.outputs.get('knowledge_revisions', {}))
                if outcome.outputs.get('published_kinds') and not downstream_toolcall(header):
                    app['knowledge_rescan_pending'] = True
        while not pending and app.get('admin_submission_queue'):
            submission = app['admin_submission_queue'].pop(0)
            imported = app['admin_imports'][submission['admin_submission_id']]
            if (imported['base_gap_revision'] != app.get('gap_revision', 0)
                    or imported.get('knowledge_revisions', {}) != app.get('knowledge_revisions', {})):
                imported['status'] = 'stale'
                continue
            operations += self._schedule_support(snapshot, header, app, 'admin_review', payload=submission)
        return operations

    def import_research(self, run_dir, run_id, submission_path):
        """Import explicit administrator evidence through an independent SDK task.

        Editing project projections is inert. Only this action binds supplied
        evidence to the current gap/knowledge revisions and queues its review.
        """
        from pathlib import Path
        import re
        from .evidence import read_json, verified_path, atomic_json
        from .operations import MigrationRun
        from .application_state_storage import hydrate_run_snapshot, pack_application_state
        source_path = Path(submission_path).absolute()
        if source_path.resolve() != source_path or not source_path.is_file():
            raise ValueError('administrator submission must be a regular non-symlink file')
        submission = read_json(source_path)
        if not isinstance(submission, dict) or type(submission.get('schema_version')) is not int or submission['schema_version'] != 1:
            raise ValueError('administrator submission requires schema_version 1')
        identity = submission.get('submission_id')
        if not isinstance(identity, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', identity):
            raise ValueError('administrator submission_id must be a stable identifier')
        with self.session(run_dir, run_id) as (root, header, runtime, sdk):
            state = hydrate_run_snapshot(root, sdk.get_run(run_id))
            if state['state'] != 'running':
                raise ValueError('administrator import requires a nonterminal Run')
            app = json_copy(state['application_state'] or self._new_application())
            if identity in app.get('admin_imports', {}):
                return MigrationRun(run_id, root, state)
            if submission.get('run_id') != run_id or submission.get('workflow_version') != header['definition']['workflow_version']:
                raise ValueError('administrator submission belongs to a different Run or workflow')
            if submission.get('execution_version') != header['registry_revision']:
                raise ValueError('administrator submission execution version mismatch')
            if submission.get('knowledge_revisions') != app.get('knowledge_revisions', {}):
                raise ValueError('administrator submission knowledge revision mismatch')
            if type(submission.get('base_gap_revision')) is not int or submission['base_gap_revision'] != app.get('gap_revision', 0):
                raise ValueError('administrator submission gap revision mismatch')
            refs = self._refs(header, app)
            original = read_json(verified_path(root, refs['source_evidence']))
            if submission.get('source_commit') != original['source_commit']:
                raise ValueError('administrator submission source commit mismatch')
            resolutions = submission.get('gap_resolutions')
            if not isinstance(resolutions, list) or not resolutions:
                raise ValueError('administrator submission requires project gap resolutions')
            seen = set()
            for row in resolutions:
                if not isinstance(row, dict) or row.get('gap_id') not in app.get('project_research_gaps', {}) or row['gap_id'] in seen:
                    raise ValueError('administrator submission contains unknown or duplicate gap')
                seen.add(row['gap_id'])
                if row.get('project_status') not in ('resolved', 'not_applicable', 'unresolved'):
                    raise ValueError('administrator cannot certify project execution or bypass independent planning')
            source_refs = {}
            for index, row in enumerate(submission.get('sources', [])):
                if not isinstance(row, dict) or not isinstance(row.get('path'), str):
                    raise ValueError('invalid administrator source')
                relative = Path(row['path'])
                source = source_path.parent / relative
                if relative.is_absolute() or '..' in relative.parts or source.resolve() != source.absolute() or not source.is_file():
                    raise ValueError('administrator source must be contained beside its submission')
                alias = 'admin:' + identity + ':source:' + str(index)
                source_refs[alias] = (source, root / 'artifacts' / 'administrator' / identity / 'sources' / str(index))
            directory = root / 'artifacts' / 'administrator' / identity
            directory.mkdir(parents=True, exist_ok=True)
            for alias, (source, destination) in source_refs.items():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
                app.setdefault('admin_artifact_refs', {})[alias] = {'path': destination.relative_to(root).as_posix()}
            stored = directory / 'submission.json'
            atomic_json(stored, submission)
            ref = {'path': stored.relative_to(root).as_posix()}
            app.setdefault('admin_artifact_refs', {})['admin:' + identity + ':submission'] = ref
            app.setdefault('admin_imports', {})[identity] = {'status': 'pending', 'ref': ref,
                'base_gap_revision': app.get('gap_revision', 0), 'knowledge_revisions': json_copy(app.get('knowledge_revisions', {}))}
            app.setdefault('admin_submission_queue', []).append({'admin_submission_id': identity, 'admin_submission_ref': ref})
            operations = self._release_administrator_wait(state, app)
            result = sdk.apply_operations(run_id, command_id='administrator-import:' + identity,
                expected_revision=state['revision'], expected_generation=state['generation'],
                operations=operations, application_state=pack_application_state(root, app))
            result = hydrate_run_snapshot(root, result)
            self._project_gap_state(header, result)
            self._audit_action(root, run_id, 'research_import:' + identity)
            return MigrationRun(run_id, root, self.tick(sdk, header))
