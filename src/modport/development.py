"""Host-owned isolated development plans, coder deltas and atomic integration."""
from __future__ import annotations

from .workspace import project_path, is_project_workspace
from .author_contracts import acceptance_report_prompt

from dataclasses import replace
from hashlib import sha1, sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import time
from typing import Mapping

from .contracts import OperationInput, OperationResult
from . import handlers
from .business_policy import business_gates_disabled
from .workflow import agent_model_policy


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


class DependencyPatchConflict(ValueError):
    """A safe dependency merge needs semantic work, not isolation recovery."""

    def __init__(self, task_id, paths, patch_path, output):
        self.task_id = task_id
        self.paths = paths
        self.patch_path = patch_path
        self.output = output
        super().__init__(f"dependency {task_id} patch conflict in {', '.join(paths)}: {output[-1000:]}")


def _path(value, *, shared=False):
    if (not isinstance(value, str) or not value or "\\" in value
            or any(ord(c) < 32 for c in value) or "\ufffd" in value):
        raise ValueError("unsafe owned path")
    path = PurePosixPath(value)
    parts = value.rstrip("/").split("/")
    if (path.is_absolute() or any(p in ("", ".", "..", ".git") for p in parts)
            or ":" in parts[0]):
        raise ValueError("owned paths must be contained relative paths")
    if not shared and parts[0] == ".modport" and (len(parts) < 2 or parts[1] != "tests"):
        raise ValueError("only .modport/tests may be owned by a coder")
    return "/".join(parts)


def _contains(prefix, path):
    return path == prefix or path.startswith(prefix + "/")


def validate_plan(plan, *, allow_contract=False, allow_preparation=False,
                  workflow_version=16, model_policy=None):
    """Normalize a non-overlapping task DAG into stable topological order."""
    if business_gates_disabled({'workflow_version': workflow_version}):
        from .execution_plan import normalize_execution_plan
        return normalize_execution_plan(plan, workflow_version=workflow_version,
                                        model_policy=model_policy)
    if not isinstance(plan, Mapping) or type(plan.get("schema_version")) is not int or plan["schema_version"] != 1:
        raise ValueError("development plan requires schema_version 1")
    base = plan.get("base_commit")
    if not isinstance(base, str) or not _SHA.fullmatch(base):
        raise ValueError("base_commit must be an exact 40-character commit")
    shared = plan.get("shared_paths")
    raw_tasks = plan.get("tasks")
    if not isinstance(shared, list) or not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("plan requires shared_paths and nonempty tasks arrays")
    shared = [_path(p, shared=True) for p in shared]
    tasks = {}
    ownership = [(p, "shared") for p in shared]
    for raw in raw_tasks:
        if not isinstance(raw, Mapping):
            raise ValueError("task must be an object")
        task = dict(raw)
        identifier = task.get("id")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in tasks:
            raise ValueError("invalid or duplicate development task id")
        if not isinstance(task.get("objective"), str) or not task["objective"].strip():
            raise ValueError("task objective must be nonempty")
        for key in ("dependencies", "owned_paths", "acceptance"):
            values = task.get(key)
            if (not isinstance(values, list) or (key != "dependencies" and not values)
                    or any(not isinstance(v, str) or not v.strip() for v in values)
                    or len(set(values)) != len(values)):
                raise ValueError(f"invalid task {key}")
        blocked_by_gaps = task.get("blocked_by_gaps", [])
        if (
            not isinstance(blocked_by_gaps, list)
            or any(not isinstance(value, str) or not value.strip() for value in blocked_by_gaps)
            or len(set(blocked_by_gaps)) != len(blocked_by_gaps)
        ):
            raise ValueError("invalid task blocked_by_gaps")
        task["blocked_by_gaps"] = list(blocked_by_gaps)
        task["owned_paths"] = [_path(p, shared=allow_contract or (allow_preparation and task.get("kind") == "prepare")) for p in task["owned_paths"]]
        if allow_contract:
            _contract_paths(task["owned_paths"])
        if allow_preparation and task.get("kind") == "prepare":
            _prepare_paths(task["owned_paths"])
        for path in task["owned_paths"]:
            for previous, owner in ownership:
                if _contains(previous, path) or _contains(path, previous):
                    raise ValueError(f"overlapping ownership: {identifier}:{path} and {owner}:{previous}")
            ownership.append((path, identifier))
        complexity = task.get("complexity")
        if complexity not in ("simple", "complex"):
            raise ValueError("task complexity must be simple or complex")
        if type(workflow_version) is not int or workflow_version < 1:
            raise ValueError("workflow_version must be a positive integer")
        if workflow_version >= 15:
            expected_model, expected_effort = agent_model_policy(workflow_version, "coder", model_policy)
        elif complexity == "simple":
            expected_model, expected_effort = "gpt-5.6-luna", "max"
        else:
            expected_model, expected_effort = "gpt-6-astra", "medium"
        task.setdefault("model", expected_model)
        if not isinstance(task["model"], str) or task["model"] != expected_model:
            raise ValueError("task model is outside the development policy")
        task.setdefault("reasoning_effort", expected_effort)
        if task["reasoning_effort"] != expected_effort:
            raise ValueError("reasoning effort differs from the development model policy")
        tasks[identifier] = task
    for task in tasks.values():
        if any(d not in tasks or d == task["id"] for d in task["dependencies"]):
            raise ValueError("unknown or self dependency")
    ordered = []
    done = set()
    while len(done) < len(tasks):
        ready = [t for t in tasks.values() if t["id"] not in done and set(t["dependencies"]) <= done]
        if not ready:
            raise ValueError("development task dependency cycle")
        for task in ready:
            ordered.append(task)
            done.add(task["id"])
    return {"schema_version": 1, "base_commit": base, "shared_paths": shared, "tasks": ordered}


def _git(command, workspace, *args, check=True):
    root = Path(command.run_dir)
    result = handlers._exec(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
         "-c", "core.quotePath=true", "-c", "user.name=ModPort", "-c", "user.email=modport@localhost", *args],
        cwd=workspace, log=root / "logs" / f"development-{command.command_id}.log",
        timeout=handlers._remaining_timeout(command, 120),
        env={"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1"})
    if check and result.returncode:
        raise ValueError(f"git {args[0]} failed: {result.stdout[-1000:]}")
    return result


def _clean(command, workspace, *, include_ignored=False):
    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if include_ignored:
        args.append("--ignored")
    if _git(command, workspace, *args).stdout:
        raise ValueError("development workspace is dirty (including untracked files)")


def _head(command, workspace):
    value = _git(command, workspace, "rev-parse", "HEAD").stdout.strip()
    if not _SHA.fullmatch(value):
        raise ValueError("invalid HEAD")
    return value


def _artifact(command, name, data, metadata=None):
    from .execution_budget import require_remaining_time
    require_remaining_time(command)
    root = Path(command.run_dir)
    path = root / "artifacts" / "executions" / command.command_id / name
    path.parent.mkdir(parents=True, exist_ok=True)
    require_remaining_time(command)
    if path.exists() or path.is_symlink():
        raise ValueError("development artifact already exists")
    path.write_bytes(data)
    require_remaining_time(command)
    digest = sha256(data).hexdigest()
    require_remaining_time(command)
    return {"path": path.relative_to(root).as_posix(), "sha256": digest,
            "metadata": metadata or {}}


def _candidate_capture_receipt(root, execution_id, record):
    """Persist candidate capture outcome separately from model and SDK results."""
    from .evidence import atomic_json
    root = Path(root).resolve()
    directory = root / 'artifacts' / 'executions' / execution_id
    if directory.is_symlink() or directory.resolve() != directory.absolute():
        raise ValueError('candidate capture receipt path is unsafe')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'candidate-capture.json'
    envelope = {'record': record, 'sha256': sha256(json.dumps(
        record, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    if len(json.dumps(envelope, sort_keys=True, ensure_ascii=False).encode('utf-8')) > 8 * 1024 * 1024:
        raise ValueError('candidate capture receipt exceeds the 8 MiB limit')
    if path.exists() or path.is_symlink():
        if (path.is_symlink() or not path.is_file()
                or path.stat().st_size > 8 * 1024 * 1024
                or json.loads(path.read_text()) != envelope):
            raise ValueError('existing candidate capture receipt differs')
    else:
        atomic_json(path, envelope)
    data = path.read_bytes()
    return {'path': path.relative_to(root).as_posix(), 'sha256': sha256(data).hexdigest(),
            'media_type': 'application/json', 'metadata': {
                'execution_id': execution_id, 'status': record.get('status')}}


def _verified(command, ref):
    from .execution_budget import require_remaining_time
    require_remaining_time(command)
    path = handlers._resolve_artifact_ref(Path(command.run_dir), ref, 'development_ref')[0]
    expected = ref.get('sha256') if isinstance(ref, Mapping) else None
    require_remaining_time(command)
    data = path.read_bytes()
    require_remaining_time(command)
    if (not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected)
            or sha256(data).hexdigest() != expected):
        raise ValueError('development artifact digest mismatch')
    require_remaining_time(command)
    return path


def _plan(command):
    ref = command.artifact_refs.get("development_plan")
    if not isinstance(ref, Mapping):
        raise ValueError("frozen development_plan ref is required")
    frozen = json.loads(_verified(command, ref).read_text())
    # The host's scheduled DAG may incorporate an explicit author's revision.
    # Keep the old report as evidence, but execute the same plan as the scheduler.
    scheduled = command.payload.get('execution_development_plan')
    if business_gates_disabled(command) and isinstance(scheduled, Mapping):
        if scheduled.get('base_commit') != command.payload.get('development_base'):
            raise ValueError('scheduled development plan base mismatch')
        frozen = scheduled
    return validate_plan(frozen,
                         allow_contract=command.payload.get("goal_scope") == "contract",
                         allow_preparation=command.payload.get('development_kind') == 'preparation',
                         workflow_version=command.options.get("workflow_version", 16),
                         model_policy=command.options.get('model_policy'))


def _base(command):
    base = command.payload.get("development_base")
    if not isinstance(base, str) or not _SHA.fullmatch(base):
        raise ValueError("development_base must be an exact commit")
    generation = command.payload.get("development_generation")
    if type(generation) is not int or generation < 1:
        raise ValueError("development_generation must be positive")
    return base, generation


def development_workspace(generation, task_id, payload):
    relative = f'workspaces/development/g{generation}/{task_id}'
    epoch = payload.get('development_workspace_epoch')
    if epoch is not None:
        if not isinstance(epoch, str) or not re.fullmatch(r'[0-9a-f]{16}', epoch):
            raise ValueError('invalid continued development workspace identity')
        relative += '-segment-' + epoch
    return relative


def _task(plan, identifier):
    for task in plan["tasks"]:
        if task["id"] == identifier:
            return task
    raise ValueError("task not present in frozen plan")


def _dependencies(plan, task):
    required = set(task["dependencies"])
    previous = None
    while previous != required:
        previous = set(required)
        for item in plan["tasks"]:
            if item["id"] in required:
                required.update(item["dependencies"])
    return [item for item in plan["tasks"] if item["id"] in required]


def _paths(command, workspace, start, end):
    # Disable rename detection so both old and new names are independently checked.
    output = _git(command, workspace, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "--no-renames", "-z", start, end, "--").stdout
    return sorted(set(p for p in output.split("\0") if p))


def _prepare_paths(paths):
    protected = ('.modport/functional-contract.json', '.modport/contract-review.json',
                 '.modport/code-review.json', '.modport/evidence')
    for path in paths:
        _path(path, shared=True)
        if any(_contains(path, item) or _contains(item, path) for item in protected):
            raise ValueError('preparation cannot own frozen contracts, reviews, or evidence')


def _contract_paths(paths):
    for path in paths:
        _path(path, shared=True)
        if not path.startswith('.modport/') or _contains(path, '.modport/goal-reports') or _contains('.modport/goal-reports', path):
            raise ValueError('contract coder ownership must stay within .modport outside host goal reports')


def _owned(paths, task, *, allow_contract=False, gates_disabled=False):
    if allow_contract and not gates_disabled:
        _contract_paths(paths)
    if task.get('kind') == 'prepare' and not gates_disabled:
        _prepare_paths(paths)
    for path in paths:
        _path(path, shared=True if gates_disabled else allow_contract or task.get('kind') == 'prepare')
        if not gates_disabled and not any(_contains(prefix, path) for prefix in task["owned_paths"]):
            raise ValueError(f"coder {task['id']} modified unowned path {path}")


def _check_ref(command, ref, task, base, generation):
    path = _verified(command, ref)
    metadata = ref.get("metadata", {})
    if (metadata.get("task_id") != task["id"] or metadata.get("base") != base
            or metadata.get("generation") != generation):
        raise ValueError("coder patch identity/base/generation mismatch")
    paths = metadata.get("paths")
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise ValueError("coder patch requires paths metadata")
    _owned(paths, task, allow_contract=command.payload.get('goal_scope') == 'contract',
           gates_disabled=business_gates_disabled(command))
    return path


def _apply(command, workspace, path, task, *, conflict_handoff=None, resolvable_paths=None):
    start = _head(command, workspace)
    if path.stat().st_size:
        applied = _git(command, workspace, "apply", "--index", "--3way", "--", str(path), check=False)
        changed = _git(command, workspace, "diff", "--cached", "--name-only", "--no-renames", "-z", "--").stdout
        _owned([p for p in changed.split("\0") if p], task,
               allow_contract=command.payload.get('goal_scope') == 'contract',
               gates_disabled=business_gates_disabled(command))
        if business_gates_disabled(command):
            _assert_regular_workspace(workspace)
            for entry in _git(command, workspace, 'ls-files', '--stage', '-z').stdout.split('\0'):
                if entry and entry.split(' ', 1)[0] not in {'100644', '100755'}:
                    raise ValueError('integrated patch contains a symlink or nonregular Git entry')
        if applied.returncode:
            paths = [p for p in _git(command, workspace, 'diff', '--name-only',
                '--diff-filter=U', '-z', '--').stdout.split('\0') if p]
            if not paths:
                raise ValueError(f'git apply failed: {applied.stdout[-1000:]}')
            conflict = DependencyPatchConflict(task['id'], paths,
                str(path.relative_to(Path(command.run_dir))), applied.stdout)
            if (conflict_handoff is None or not business_gates_disabled(command)
                    or (resolvable_paths is not None and not set(paths) <= resolvable_paths)):
                raise conflict
            # A planner-approved successor receives the actual conflicting
            # files and archived patches. Freeze the materialized merge as its
            # baseline; resolving it is part of the assigned integration work.
            _git(command, workspace, 'add', '--all', '--', *paths)
            conflict_handoff.append({'task_id': task['id'], 'paths': paths,
                'patch_path': conflict.patch_path, 'detail': conflict.output})
        _git(command, workspace, "commit", "--no-gpg-sign", "-m", f"Integrate development task {task['id']}")
    _clean(command, workspace)
    return start


class ImplementationHandler:
    def __call__(self, command):
        root = handlers._run_root(command)
        workspace = project_path(root, "worktree")
        try:
            _clean(command, workspace)
            from .planning import approved_development_plan
            plan = approved_development_plan(command)
            base = _head(command, workspace)
            if plan["base_commit"] != base:
                raise ValueError("development plan base differs from actual HEAD")
            ref = command.artifact_refs["development_plan"]
            return handlers._result(command, "completed", outputs={"development_tasks": plan["tasks"], "tasks": plan["tasks"], "development_base": base, "artifact_refs": {"development_plan": ref}})
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            return handlers._result(command, "failed", detail=str(exc), error_code="development_plan_invalid")


def _candidate(command, workspace, start, task):
    """Check all commits at the native goal acceptance boundary."""
    advisory = business_gates_disabled(command)
    if not advisory:
        _clean(command, workspace, include_ignored=True)
    head = _head(command, workspace)
    if advisory:
        _assert_regular_workspace(workspace)
        for entry in _git(command, workspace, 'ls-tree', '-r', '-z', head).stdout.split('\0'):
            if entry:
                description, name = entry.split('\t', 1)
                mode, kind, _ = description.split()
                if kind != 'blob' or mode not in {'100644', '100755'}:
                    raise ValueError('candidate contains a symlink or nonregular Git entry: ' + name)
    _git(command, workspace, 'merge-base', '--is-ancestor', start, head)
    contract = command.payload.get('goal_scope') == 'contract'
    commits = _git(command, workspace, 'rev-list', f'{start}..{head}').stdout.splitlines()
    for commit in commits:
        changes = _git(command, workspace, 'diff-tree', '--root', '-m', '--no-commit-id',
                       '--name-only', '--no-renames', '-r', '-z', commit).stdout
        _owned([p for p in changes.split('\0') if p], task, allow_contract=contract,
               gates_disabled=advisory)
    paths = _paths(command, workspace, start, head)
    _owned(paths, task, allow_contract=contract, gates_disabled=advisory)
    return head, paths


def _assert_regular_workspace(workspace):
    """Contain host reads without requiring every partial edit to be committed."""
    workspace = Path(workspace).absolute()
    if workspace.resolve() != workspace:
        raise ValueError('candidate workspace traverses a symlink')
    for folder, directories, names in os.walk(workspace, followlinks=False):
        directories[:] = [name for name in directories
                          if not (Path(folder) == workspace and name == '.git')]
        for name in [*directories, *names]:
            path = Path(folder) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError('candidate contains a symlink or special file: '
                                 + path.relative_to(workspace).as_posix())


def _assert_committed_files(command, workspace, head, *, repository=None):
    """Compare actual bytes with Git objects without trusting index/stat flags."""
    expected = {}
    for entry in _git(command, repository or workspace, 'ls-tree', '-r', '-z', head).stdout.split('\0'):
        if entry:
            description, name = entry.split('\t', 1)
            mode, kind, identity = description.split()
            if kind != 'blob' or mode not in ('100644', '100755'):
                raise ValueError('native candidate requires regular committed files: ' + name)
            expected[name] = (mode, identity)
    observed = set()
    for folder, directories, names in os.walk(workspace, followlinks=False):
        directories[:] = [name for name in directories if not (Path(folder) == workspace and name == '.git')]
        for name in directories:
            if (Path(folder) / name).is_symlink():
                raise ValueError('native candidate contains a directory symlink')
        for name in names:
            handlers._remaining_timeout(command, 120)
            path = Path(folder) / name
            relative = path.relative_to(workspace).as_posix()
            attributes = path.lstat()
            if relative not in expected or not stat.S_ISREG(attributes.st_mode):
                raise ValueError('native candidate differs from committed files: ' + relative)
            mode, identity = expected[relative]
            actual_mode = '100755' if attributes.st_mode & 0o111 else '100644'
            digest = sha1(f'blob {attributes.st_size}\0'.encode())
            with path.open('rb') as source:
                while chunk := source.read(1024 * 1024):
                    handlers._remaining_timeout(command, 120)
                    digest.update(chunk)
            if mode != actual_mode or digest.hexdigest() != identity:
                raise ValueError('native candidate bytes or mode differ from committed HEAD: ' + relative)
            observed.add(relative)
    if observed != set(expected):
        raise ValueError('native candidate omits committed files')


def _coder_setup(command, workspace, identity, *, start=None):
    """Bind an explicitly resumable native session to its host-created checkout."""
    path = Path(command.run_dir) / 'artifacts' / 'executions' / command.command_id / 'coder-setup.json'
    if path.resolve() != path.absolute():
        raise ValueError('unsafe coder setup path')
    if start is not None:
        record = {**identity, 'start_commit': start,
                  'start_tree': _git(command, workspace, 'rev-parse', start + '^{tree}').stdout.strip()}
        data = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
        _artifact(command, 'coder-setup.json', json.dumps({
            'record': record, 'sha256': sha256(data).hexdigest()}, sort_keys=True).encode())
        return start
    if not path.is_file():
        raise ValueError('native coder resume requires its execution setup record')
    envelope = json.loads(path.read_text())
    if not isinstance(envelope, dict):
        raise ValueError('invalid coder setup envelope')
    record = envelope.get('record')
    if not isinstance(record, dict):
        raise ValueError('invalid coder setup record')
    data = json.dumps(record, sort_keys=True, separators=(',', ':')).encode()
    if envelope.get('sha256') != sha256(data).hexdigest():
        raise ValueError('coder setup digest mismatch')
    if {key: value for key, value in record.items() if key not in ('start_commit', 'start_tree')} != identity:
        raise ValueError('coder resume differs from its original execution setup')
    start = record.get('start_commit')
    if not isinstance(start, str) or not _SHA.fullmatch(start):
        raise ValueError('invalid coder resume start commit')
    tree = _git(command, workspace, 'rev-parse', start + '^{tree}').stdout.strip()
    if tree != record.get('start_tree'):
        raise ValueError('coder resume start tree mismatch')
    _git(command, workspace, 'merge-base', '--is-ancestor', identity['base_commit'], start)
    _git(command, workspace, 'merge-base', '--is-ancestor', start, _head(command, workspace))
    for name in ('coder.patch', 'goal-acceptance-report.json', 'goal-host-validation.json'):
        if (path.parent / name).exists() or (path.parent / name).is_symlink():
            raise ValueError('coder export already started; native resume cannot duplicate publication')
    return start


def coder_runtime_goal(goal: Mapping, *, advisory: bool) -> Mapping:
    """Derive the exact native goal used by both coder launch and recovery."""
    if not advisory:
        return goal
    return {**goal, 'source_objective': goal['objective'],
            'objective': ('Edit the assigned project files and finish with a concise handoff. '
                          'The host will collect and commit the filesystem changes after you stop; '
                          'do not wait for builds or host verification. Source objective: '
                          + goal['objective'])}


class CoderHandler:
    def __call__(self, command):
        root = handlers._run_root(command)
        advisory = business_gates_disabled(command)
        business_diagnostics = []
        execution_started = time.monotonic()
        collection_seconds = 0.0
        collection_calls = 0
        settlement_context = None
        settlement_entered = False
        publication_context = None
        publication_entered = False
        split_settlement = False

        def collect_candidate(*args, **kwargs):
            nonlocal collection_seconds, collection_calls
            from .host_candidate import collect_host_candidate
            started = time.monotonic()
            collection_calls += 1
            try:
                return collect_host_candidate(*args, **kwargs)
            finally:
                collection_seconds += time.monotonic() - started

        def settlement_checkpoint():
            from .execution_budget import require_remaining_time
            return require_remaining_time(settlement_command)

        def enter_publication_phase():
            nonlocal settlement_context, settlement_entered
            nonlocal publication_context, publication_entered
            if not split_settlement:
                return
            settlement_checkpoint()
            if settlement_context is not None and settlement_entered:
                settlement_context.__exit__(None, None, None)
                settlement_entered = False
            from .execution_budget import publication_phase
            publication_context = publication_phase(settlement_command)
            publication_context.__enter__()
            publication_entered = True
            settlement_checkpoint()
        dependency_conflicts = []
        diagnostic_repair_receipts = []
        try:
            plan = _plan(command)
            base, generation = _base(command)
            supplied = command.payload.get("development_task", {})
            if not isinstance(supplied, Mapping):
                raise ValueError("development_task must be an object")
            task = _task(plan, supplied.get("id"))
            if supplied != task and not advisory:
                raise ValueError("coder task differs from frozen plan")
            if command.artifact_refs["development_plan"].get("metadata", {}).get("development_base") != base:
                raise ValueError("development base differs from frozen plan ref")
            goal = None
            if 'coder_goal' in command.artifact_refs:
                from .goal_planning import validate_goal
                context = command.payload.get('planning_context')
                if not isinstance(context, Mapping):
                    if not advisory:
                        raise ValueError('coder goal needs four authenticated planning references')
                    business_diagnostics.append('planning context was unavailable; coder received the normalized task')
                    context = {}
                elif not advisory and len(context) != 4:
                    raise ValueError('coder goal needs four authenticated planning references')
                path = _verified(command, command.artifact_refs['coder_goal'])
                if path.resolve() != path.absolute():
                    raise ValueError('coder goal context containment mismatch')
                for name, ref in context.items():
                    try:
                        path = _verified(command, ref)
                    except (OSError, ValueError, TypeError, KeyError) as exc:
                        if not advisory:
                            raise
                        business_diagnostics.append(f'planning context {name} was unavailable: {exc}')
                        continue
                    if path.resolve() != path.absolute():
                        if not advisory:
                            raise ValueError('coder goal context containment mismatch')
                        business_diagnostics.append(f'planning context {name} failed containment validation')
                supplied_goal = json.loads(_verified(command, command.artifact_refs['coder_goal']).read_text())
                goal = validate_goal(supplied_goal, task, context,
                                     require_double_check=command.options.get('workflow_version', 0) >= 12,
                                     gates_disabled=advisory)
                if (command.options.get('workflow_version', 0) >= 26
                        and isinstance(supplied_goal.get('objective'), str)
                        and supplied_goal['objective'].strip()):
                    # Normalization owns executable settings, not the prepared
                    # context. Older workflows retain their frozen behavior.
                    goal['objective'] = supplied_goal['objective']
                if not advisory and goal['acceptance_report'] != f".modport/goal-reports/{task['id']}.json":
                    raise ValueError('native coder goal must use its host-reserved acceptance report')
            elif advisory:
                from .goal_planning import validate_goal
                context = command.payload.get('planning_context', {})
                goal = validate_goal({}, task, context, gates_disabled=True)
                business_diagnostics.append('coder goal artifact was unavailable; host used the normalized task directly')
            elif command.options.get('workflow_version', 0) >= 11:
                raise ValueError('native coder goal is required by workflow 11')
            if goal is not None and command.options.get('workflow_version', 0) >= 26:
                from .supervised_goals import apply_to_goal
                goal = apply_to_goal(command, goal, task)
            relative = development_workspace(generation, task['id'], command.payload)
            if command.options.get("workspace") != relative:
                raise ValueError("coder workspace differs from its isolated task path")
            workspace = root / relative
            resume = command.options.get('native_goal_resume', False)
            if type(resume) is not bool or (resume and goal is None):
                raise ValueError('explicit native coder resume requires a goal and boolean option')
            if workspace.is_symlink() or workspace.resolve() != workspace.absolute():
                raise ValueError('coder workspace must be contained without symlinks')
            if resume and not workspace.is_dir():
                raise ValueError('native coder resume requires its existing workspace')
            if not resume and workspace.exists():
                raise ValueError('coder workspace must be new and contained')
            source_relative = _path(command.payload.get('development_source_workspace', 'worktree'), shared=True)
            source = project_path(root, source_relative)
            if source.resolve() != source.absolute() or (not resume and not source.is_dir()):
                raise ValueError('development source workspace must be contained and regular')
            dependencies = _dependencies(plan, task)
            refs = command.payload.get("dependency_patches", [])
            if not isinstance(refs, list):
                raise ValueError("dependency patches must include transitive dependencies exactly once")
            if not advisory and len(refs) != len(dependencies):
                raise ValueError("dependency patches must include transitive dependencies exactly once")
            dependency_patches = []
            indexed_refs = {}
            duplicate_ids = set()
            for ref in refs:
                metadata = ref.get('metadata', {}) if isinstance(ref, Mapping) else {}
                identifier = metadata.get('task_id') if isinstance(metadata, Mapping) else None
                if not isinstance(identifier, str):
                    if not advisory:
                        raise ValueError('dependency patch requires task identity')
                    business_diagnostics.append('dependency patch without task identity was not applied')
                    continue
                if identifier in indexed_refs:
                    duplicate_ids.add(identifier)
                indexed_refs[identifier] = ref
            if duplicate_ids and not advisory:
                raise ValueError('duplicate dependency patch identity')
            for dep in dependencies:
                try:
                    if dep['id'] in duplicate_ids:
                        raise ValueError('duplicate dependency patch identity')
                    ref = indexed_refs.get(dep['id'])
                    if ref is None:
                        raise ValueError('dependency patch missing for task ' + dep['id'])
                    dependency_patches.append((dep, ref, _check_ref(command, ref, dep, base, generation)))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    if not advisory:
                        raise
                    business_diagnostics.append(f"dependency {dep['id']} patch unavailable: {exc}")
            if advisory and len(dependency_patches) != len(dependencies):
                business_diagnostics.append('one or more dependency patches were not applied')
            identity = {'schema_version': 1, 'run_id': command.run_id, 'command_id': command.command_id,
                        'task_id': command.task_id, 'stage_id': command.stage_id,
                        'task': task, 'goal': goal, 'goal_ref': command.artifact_refs.get('coder_goal'),
                        'planning_context': command.payload.get('planning_context'),
                        'plan_ref': command.artifact_refs['development_plan'],
                        'plan_sha256': sha256(_verified(command, command.artifact_refs['development_plan']).read_bytes()).hexdigest(),
                        'dependency_refs': [ref for _, ref, _ in dependency_patches],
                        'dependency_sha256': [sha256(path.read_bytes()).hexdigest()
                                              for _, _, path in dependency_patches],
                        'base_commit': base, 'generation': generation, 'workspace': relative,
                        'source_workspace': source_relative, 'goal_scope': command.payload.get('goal_scope'),
                        'goal_generation': command.payload.get('goal_generation')}
            if command.options.get('workflow_version', 0) >= 40:
                identity['diagnostic_repair_refs'] = command.payload.get('diagnostic_repair_refs', [])
            repair_record = root / 'artifacts' / 'executions' / command.command_id / 'diagnostic-repair-application.json'
            if resume:
                start = _coder_setup(command, workspace, identity)
                if command.options.get('workflow_version', 0) >= 40 and command.payload.get('diagnostic_repair_refs'):
                    if repair_record.resolve() != repair_record.absolute() or not repair_record.is_file():
                        raise ValueError('native coder resume requires its original diagnostic repair receipt')
                    diagnostic_repair_receipts = json.loads(repair_record.read_text(encoding='utf-8'))
                conflict_record = root / 'artifacts' / 'executions' / command.command_id / 'dependency-conflict-baseline.json'
                if conflict_record.is_symlink() or conflict_record.resolve() != conflict_record.absolute():
                    raise ValueError('unsafe dependency conflict baseline record')
                if conflict_record.is_file():
                    dependency_conflicts = json.loads(conflict_record.read_text())
            else:
                workspace.parent.mkdir(parents=True, exist_ok=True)
                _git(command, root, 'clone', '--no-hardlinks', '--no-checkout', '--', str(source), str(workspace))
                _git(command, workspace, 'checkout', '--detach', base)
                dependency_conflicts = []
                revival = command.payload.get('coder_revival')
                approved_revival = (advisory and isinstance(revival, Mapping)
                                    and bool(revival.get('request_id')))
                for dep, _, path in dependency_patches:
                    _apply(command, workspace, path, dep,
                        conflict_handoff=dependency_conflicts if approved_revival else None)
                if dependency_conflicts:
                    _artifact(command, 'dependency-conflict-baseline.json',
                              json.dumps(dependency_conflicts, ensure_ascii=False).encode())
                start = _head(command, workspace)
                partial_ref = command.payload.get('recovered_partial_patch')
                if partial_ref is not None:
                    if not advisory:
                        raise ValueError('partial recovery requires advisory workflow')
                    partial_path = _check_ref(command, partial_ref, task, base, generation)
                    _apply(command, workspace, partial_path, task)
                    business_diagnostics.append(
                        'continued from authenticated stopped-coder partial patch '
                        + partial_ref['sha256'])
                if command.options.get('workflow_version', 0) >= 40 and command.payload.get('diagnostic_repair_refs'):
                    from .diagnostic_repairs import apply
                    from .evidence import atomic_json
                    # Keep start before these edits: the exported coder patch
                    # must include the repair as well as the coder's work.
                    diagnostic_repair_receipts = apply(
                        command, workspace, command.payload['diagnostic_repair_refs'])
                    if repair_record.resolve() != repair_record.absolute() or repair_record.exists():
                        raise ValueError('unsafe or existing diagnostic repair receipt')
                    atomic_json(repair_record, diagnostic_repair_receipts)
                if goal is not None:
                    _coder_setup(command, workspace, identity, start=start)
            if advisory:
                prompt = ("Implement the assigned development work by editing the project files directly. "
                          "Do not stage or commit changes: after you stop, the host collects the safe filesystem "
                          "delta and creates the candidate commit. Do not run project code, builds, or tests, and "
                          "do not wait for host verification. Treat ownership, dependencies, acceptance criteria, "
                          "and validation checks as advisory context. Finish with a concise handoff describing "
                          "useful edits and remaining blockers. Task: " + json.dumps(task, ensure_ascii=False))
                if diagnostic_repair_receipts:
                    prompt += ('\nThe host has processed small corrections from a diagnostic agent. '
                               'Applied/already_applied entries are already present: preserve them and '
                               'do only the remaining task work, without reimplementing those fixes. '
                               'Conflict/invalid entries were NOT applied; inspect the supplied repair '
                               'reference and current code, resolve only if justified, and report unresolved '
                               'issues. These edits are not verification or acceptance. Host receipts: '
                               + json.dumps(diagnostic_repair_receipts, ensure_ascii=False))
                if dependency_conflicts:
                    prompt += ('\nDependency patches have overlapping edits. The host preserved the '
                               'materialized conflict files in your baseline after the recovery planner '
                               'explicitly resumed this task. Resolve these conflicts using the current '
                               'files, original patches and planner instruction; preserve the intended '
                               'changes from each prerequisite. Reconcile the writable target contract '
                               'and harness in this isolated workspace when needed, retaining frozen '
                               'behavior requirements, assertion IDs and expected values, target case IDs '
                               'and actual executable bindings. Resolve textual implementation-note '
                               'overlaps without dropping either contribution or restoring placeholders. '
                               'The planner has already authorized this reconciliation; do not stop '
                               'merely to request the same permission again. Original patches and host '
                               'evidence remain reference material. Conflict evidence: '
                               + json.dumps(dependency_conflicts, ensure_ascii=False))
                    business_diagnostics.extend(dependency_conflicts)
                if command.payload.get('recovered_partial_patch') is not None:
                    prompt += ('\nThe host has already applied the authenticated partial edits from your '
                               'interrupted predecessor. Review the current files and finish the remaining '
                               'work; preserve or correct those edits before handing off.')
                prior_request = command.payload.get('recovered_rework_request')
                if isinstance(prior_request, Mapping):
                    prompt += ('\nA previous explicit request_rework targeting this task failed before '
                               'verification completed. Address its exact reported defect as part of this '
                               'continued assignment. Request and outcome: '
                               + json.dumps(prior_request, ensure_ascii=False))
                revival = command.payload.get('coder_revival')
                if isinstance(revival, Mapping):
                    prompt += ('\nThe SDK dispatched this new attempt after a dependency-aware planner decision. '
                               'Keep the original task objective and use the latest dependency inputs. '
                               'Investigate the raw failure, inputs, code and environment; an exit, timeout '
                               'or failed check is a symptom, not a root cause. Planner continuation: '
                               + json.dumps(revival, ensure_ascii=False))
            else:
                prompt = ("Implement only this frozen development task and commit all changes. "
                          "Do not run project code. Modify only the explicitly owned paths. "
                          "Do not rewrite history. Dependencies are already applied. Task: "
                          + json.dumps(task, ensure_ascii=False))
            delegated_model, delegated_effort = (
                agent_model_policy(command.options.get('workflow_version'), "coder",
                                   command.options.get('model_policy'))
                if command.options.get('workflow_version', 0) >= 15
                else (task['model'], task['reasoning_effort']))
            delegated = replace(command, options={**command.options, "model": delegated_model,
                "reasoning_effort": delegated_effort,
                **({"host_collect_candidate": True} if advisory else {})})
            settlement_command = delegated
            model_command = delegated
            accepted_head = None
            accepted_projection = None
            accepted_report_data = None
            accepted_evidence = None
            host_candidate = None
            if goal is not None:
                from .goal_validation import validate_goal_candidate, contained_file, _check_snapshot
                from .execution_budget import (
                    current_deadline_budget, reserve_settlement, settlement_phase,
                )
                split_settlement = command.options.get('workflow_version', 0) >= 17
                # v17 separates model, capture, and receipt. Frozen v11-v16
                # retain one shared correction deadline and never receive the
                # newer host-only settlement window.
                active_budget = current_deadline_budget(settlement_command)
                native_seconds = handlers._remaining_timeout(
                    settlement_command, 7200)
                if not split_settlement:
                    model_deadline = time.time() + native_seconds
                    settlement_budget = None
                    host_deadline = None
                elif active_budget is None:
                    # Direct callers have no SDK receipt window, but still
                    # retain the v17 model/capture boundary used by run_goal.
                    capture_reserve = min(180.0, native_seconds * 0.2)
                    host_deadline = time.time() + native_seconds
                    model_deadline = host_deadline - capture_reserve
                    settlement_budget = None
                else:
                    available = max(0.0, active_budget.remaining_work())
                    capture_reserve = min(180.0, available * 0.2)
                    settlement_budget = reserve_settlement(
                        settlement_command, capture_reserve)
                    model_deadline = settlement_budget.model_deadline
                    host_deadline = settlement_budget.capture_deadline
                model_options = dict(settlement_command.options)
                if split_settlement:
                    model_options['model_deadline_epoch'] = model_deadline
                    model_options['host_settlement_deadline_epoch'] = host_deadline
                else:
                    model_options['deadline_epoch'] = model_deadline
                model_command = replace(settlement_command, options=model_options)
                if advisory:
                    prompt += ('\nThe host records one completion observation for this native goal. '
                               'Read the available authenticated planning references. The report path and '
                               'checks are optional diagnostics; missing or failed observations do not reopen '
                               'the goal. Advisory goal: ' + json.dumps(goal, ensure_ascii=False))
                    prompt += ('\nWhen useful, write a free-form handoff report at ' + goal['acceptance_report'] +
                               '. Explain changes, self-review, evidence and remaining blockers. Keep it '
                               'untracked and never commit it.')
                else:
                    prompt += ('\nThe host starts this native goal and independently verifies each completion. '
                               'Keep the acceptance report untracked; never commit it. '
                               'Read all four authenticated planning references and preserve source objectives, '
                               'acceptance, ownership and dependencies. Frozen goal: ' + json.dumps(goal, ensure_ascii=False))
                    prompt += ('\nWrite a free-form handoff report at ' + goal['acceptance_report'] +
                               '. Explain changes, self-review, evidence and remaining blockers for the next agent. '
                               'There is no JSON schema or required report field. Keep the report untracked. '
                               'The host runs the reviewed checks in its project execution sandbox; failed checks '
                               'return to this OpenCode session for correction. Your report does not substitute for those checks.')
                    prompt += acceptance_report_prompt(
                        goal, double_check=command.options.get('workflow_version', 0) >= 12)

                def validate():
                    nonlocal accepted_head, accepted_projection, accepted_report_data, accepted_evidence, host_candidate
                    accepted_head = None
                    accepted_projection = None
                    accepted_report_data = None
                    accepted_evidence = None
                    verdict = {'accepted': False, 'failures': [], 'evidence': {}}
                    report = None
                    report_data = None
                    phase = None
                    phase_entered = False
                    try:
                        if split_settlement:
                            phase = settlement_phase(settlement_command)
                            phase.__enter__()
                            phase_entered = True
                        try:
                            report = contained_file(workspace, goal['acceptance_report'])
                            if _git(settlement_command, workspace, 'ls-files', '--', goal['acceptance_report']).stdout:
                                raise ValueError('host goal report must never be committed or staged')
                            report_data = report.read_bytes()
                            report.unlink()
                        except (OSError, ValueError, TypeError, KeyError) as exc:
                            if not advisory:
                                raise
                            verdict['failures'].append('acceptance report: ' + str(exc))
                        if advisory:
                            host_candidate = collect_candidate(
                                settlement_command, workspace, start, task['id'],
                                report_paths=(goal['acceptance_report'],))
                            checked_head = host_candidate.head
                            checked_paths = host_candidate.paths
                            checked_projection = host_candidate.record['safe_projection_sha256']
                        else:
                            checked_head, checked_paths = _candidate(settlement_command, workspace, start, task)
                            checked_projection = None
                            _assert_committed_files(settlement_command, workspace, checked_head)
                        snapshot_source = workspace
                        if advisory:
                            from .host_candidate import materialize_host_candidate_view
                            snapshot_source = materialize_host_candidate_view(
                                settlement_command, host_candidate,
                                report_paths=(goal['acceptance_report'],))
                        snapshot, snapshot_evidence = _check_snapshot(settlement_command, snapshot_source)
                        # Validate an immutable host copy whose actual bytes match
                        # this exact commit, even if the live checkout later changes.
                        if not advisory:
                            _assert_committed_files(settlement_command, snapshot, checked_head, repository=workspace)
                        if report_data is not None:
                            snapshot_report = snapshot / goal['acceptance_report']
                            snapshot_report.parent.mkdir(parents=True, exist_ok=True)
                            snapshot_report.write_bytes(report_data)
                        validation_command = replace(settlement_command, payload={**settlement_command.payload,
                            'candidate_changed_paths': checked_paths})
                        observed = validate_goal_candidate(validation_command, snapshot, goal)
                        observed['failures'] = verdict['failures'] + observed.get('failures', [])
                        observed['accepted'] = not observed['failures'] and observed.get('accepted', False)
                        verdict = observed
                        if advisory:
                            final_candidate = collect_candidate(
                                settlement_command, workspace, start, task['id'],
                                report_paths=(goal['acceptance_report'],),
                                cached_candidate=host_candidate)
                            final_head = final_candidate.head
                            host_candidate = final_candidate
                        else:
                            final_head, _ = _candidate(settlement_command, workspace, start, task)
                            _assert_committed_files(settlement_command, workspace, checked_head)
                        if final_head != checked_head:
                            raise ValueError('candidate HEAD changed during host validation')
                        if (advisory and final_candidate.record['safe_projection_sha256']
                                != checked_projection):
                            raise ValueError('candidate safe projection changed during host validation')
                        verdict['evidence']['candidate'] = {'head': checked_head,
                            **({'safe_projection_sha256': checked_projection}
                               if checked_projection else {}), **snapshot_evidence}
                        if verdict['accepted'] or advisory:
                            accepted_head = checked_head
                            accepted_projection = checked_projection
                            accepted_report_data = report_data
                            accepted_evidence = verdict['evidence']
                    except TimeoutError:
                        raise
                    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                        verdict['accepted'] = False
                        verdict['failures'].append(str(exc))
                    finally:
                        try:
                            if report_data is not None and report is not None:
                                settlement_checkpoint()
                                report.write_bytes(report_data)
                                settlement_checkpoint()
                        finally:
                            if phase_entered:
                                phase.__exit__(None, None, None)
                    return verdict

                runtime_goal = coder_runtime_goal(goal, advisory=advisory)
                if advisory:
                    cleanup_binding = {
                        'run_id': command.run_id, 'command_id': command.command_id,
                        'task_id': command.task_id, 'development_task_id': task['id'],
                        'workspace': relative, 'workspace_path': str(workspace),
                        'base_commit': base, 'generation': generation,
                        'start_commit': start,
                        'start_tree': _git(command, workspace, 'rev-parse',
                                           start + '^{tree}').stdout.strip(),
                    }
                    model_command = replace(model_command, payload={
                        **model_command.payload,
                        'opencode_cleanup_binding': cleanup_binding,
                    })
                result = handlers.CodexStageHandler(
                    prompt, native_goal=runtime_goal, goal_validator=validate)(model_command)
            else:
                result = handlers.CodexStageHandler(prompt)(delegated)
            revision = (goal or {}).get('supervised_goal_revision')
            if isinstance(revision, dict):
                result = replace(result, outputs={**result.outputs, 'artifact_refs': {
                    **result.outputs.get('artifact_refs', {}),
                    'supervised_goal_application': revision['receipt_ref']}})
            if goal is not None and split_settlement:
                from .execution_budget import settlement_phase
                settlement_context = settlement_phase(settlement_command)
                settlement_context.__enter__()
                settlement_entered = True
            settlement_checkpoint()
            if advisory and isinstance(result.outputs.get('business_diagnostics'), list):
                business_diagnostics.extend(result.outputs['business_diagnostics'])
            if result.status != "completed" and not advisory:
                return result
            if result.status != 'completed':
                business_diagnostics.append(result.detail or result.error_code or 'coder execution did not complete')
            if advisory:
                native_runtime = result.outputs.get('native_goal')
                unsafe_runtime = (isinstance(native_runtime, Mapping) and (
                    native_runtime.get('producer_stopped') is False
                    or (native_runtime.get('owned_pid') is not None
                        and native_runtime.get('producer_stopped') is not True)
                    or native_runtime.get('error') in {
                        'native_goal_session_already_owned',
                        'native_goal_previous_process_alive'}))
                if unsafe_runtime:
                    business_diagnostics.append(
                        'host candidate collection skipped because the producer is not proven stopped')
                    capture_ref = _candidate_capture_receipt(root, command.command_id, {
                        'schema': 'modport.candidate-capture.v1', 'status': 'not_captured',
                        'reason': 'opencode_process_tree_cleanup_unconfirmed',
                        'source_command_id': command.command_id,
                        'workspace': relative, 'base_commit': base,
                        'generation': generation, 'start_commit': start,
                        'start_tree': _git(command, workspace, 'rev-parse',
                                           start + '^{tree}').stdout.strip(),
                    })
                    return handlers._result(command, result.status, outputs={**result.outputs,
                        'development_task_id': task['id'],
                        'business_diagnostics': business_diagnostics,
                        'acceptance_status': 'unverified',
                        'artifact_refs': {**result.outputs.get('artifact_refs', {}),
                                          'candidate_capture': capture_ref}},
                        detail=result.detail, error_code=result.error_code)
                try:
                    host_candidate = collect_candidate(
                        command, workspace, start, task['id'],
                        report_paths=((goal['acceptance_report'],) if goal is not None else ()),
                        cached_candidate=host_candidate)
                    settlement_checkpoint()
                    if (accepted_head is not None and
                            (host_candidate.head != accepted_head
                             or host_candidate.record['safe_projection_sha256']
                             != accepted_projection)):
                        business_diagnostics.append(
                            'host candidate changed after the recorded validation observation; '
                            'the final collected identity is exported as unverified')
                except (OSError, ValueError, TypeError, subprocess.SubprocessError) as exc:
                    business_diagnostics.append('host candidate collection unavailable: ' + str(exc))
                    capture_ref = _candidate_capture_receipt(root, command.command_id, {
                        'schema': 'modport.candidate-capture.v1', 'status': 'not_captured',
                        'reason': 'host_candidate_collection_failed',
                        'detail': str(exc), 'source_command_id': command.command_id,
                        'workspace': relative, 'base_commit': base,
                        'generation': generation, 'start_commit': start,
                        'start_tree': _git(command, workspace, 'rev-parse',
                                           start + '^{tree}').stdout.strip(),
                    })
                    return handlers._result(command, result.status, outputs={**result.outputs,
                        'development_task_id': task['id'],
                        'business_diagnostics': business_diagnostics,
                        'acceptance_status': 'unverified',
                        'artifact_refs': {**result.outputs.get('artifact_refs', {}),
                                          'candidate_capture': capture_ref}},
                        detail=result.detail, error_code=result.error_code)
            report_ref = None
            validation_ref = None
            validation_refs = {}
            report = None
            if goal is not None:
                if not advisory and result.outputs.get('native_goal', {}).get('host_accepted') is not True:
                    raise ValueError('native goal lacks host acceptance')
                if not advisory and accepted_head is None:
                    raise ValueError('native goal lacks an independently verified candidate HEAD')
                try:
                    settlement_checkpoint()
                    report = contained_file(workspace, goal['acceptance_report'])
                    settlement_checkpoint()
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    if not advisory:
                        raise
                    business_diagnostics.append('acceptance report unavailable: ' + str(exc))
                if report is not None:
                    report.unlink()
                    settlement_checkpoint()
            export_command = command if advisory else settlement_command if goal is not None else command
            if advisory:
                head, paths = host_candidate.head, host_candidate.paths
            else:
                head, paths = _candidate(export_command, workspace, start, task)
                settlement_checkpoint()
            if goal is not None and not advisory:
                _assert_committed_files(export_command, workspace, accepted_head)
                settlement_checkpoint()
                if head != accepted_head:
                    raise ValueError('candidate HEAD changed after host acceptance')
            # Candidate identity and all live-workspace reads are now frozen.
            # Host publication stops before the final SDK receipt/effect tail;
            # neither phase can restore model or capture time.
            enter_publication_phase()
            if accepted_report_data is not None:
                report_ref = _artifact(command, 'goal-acceptance-report.json', accepted_report_data)
            if (goal is not None and command.options.get('workflow_version', 0) >= 12
                    and accepted_evidence is not None):
                from .goal_validation import export_goal_evidence
                settlement_checkpoint()
                accepted_evidence, validation_refs = export_goal_evidence(root, accepted_evidence)
                settlement_checkpoint()
                validation_ref = _artifact(command, 'goal-host-validation.json',
                                           json.dumps(accepted_evidence, sort_keys=True).encode())
            destination = root / "artifacts" / "executions" / command.command_id / "coder.patch"
            settlement_checkpoint()
            destination.parent.mkdir(parents=True, exist_ok=True)
            settlement_checkpoint()
            host_candidate_ref = None
            if advisory:
                from .host_candidate import export_host_candidate
                patch_data = export_host_candidate(export_command, host_candidate, destination)
                settlement_checkpoint()
                candidate_data = (json.dumps(host_candidate.record, sort_keys=True) + '\n').encode()
                candidate_path = destination.parent / 'host-candidate.json'
                if candidate_path.exists() or candidate_path.is_symlink():
                    if (candidate_path.is_symlink() or not candidate_path.is_file()
                            or candidate_path.read_bytes() != candidate_data):
                        raise ValueError('existing host candidate record differs from collected candidate')
                    settlement_checkpoint()
                    host_candidate_ref = {'path': candidate_path.relative_to(root).as_posix(),
                        'sha256': sha256(candidate_data).hexdigest(), 'metadata': {}}
                    settlement_checkpoint()
                else:
                    host_candidate_ref = _artifact(command, 'host-candidate.json', candidate_data)
                if host_candidate.record['excluded_count']:
                    business_diagnostics.append(
                        f"host candidate excluded {host_candidate.record['excluded_count']} report, credential, "
                        "cache, build, or unsafe entries")
            else:
                if destination.exists() or destination.is_symlink():
                    raise ValueError("coder patch already exists")
                _git(export_command, workspace, "diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--no-renames", f"--output={destination}", start, head, "--")
                settlement_checkpoint()
                patch_data = destination.read_bytes()
                settlement_checkpoint()
            metadata = {"task_id": task["id"], "base": base, "start": start,
                        "head": head, "paths": paths, "generation": generation}
            patch_sha256 = sha256(patch_data).hexdigest()
            settlement_checkpoint()
            ref = {"path": destination.relative_to(root).as_posix(), "sha256": patch_sha256, "metadata": metadata}
            capture_ref = _candidate_capture_receipt(root, command.command_id, {
                'schema': 'modport.candidate-capture.v1', 'status': 'captured',
                'source_command_id': command.command_id, 'workspace': relative,
                'base_commit': base, 'generation': generation, 'start_commit': start,
                'start_tree': _git(export_command, workspace, 'rev-parse',
                                   start + '^{tree}').stdout.strip(),
                'head': head, 'paths': paths, 'patch_ref': ref,
                **({'safe_projection_sha256': host_candidate.record['safe_projection_sha256']}
                   if host_candidate is not None else {}),
            })
            settlement_checkpoint()
            return handlers._result(command, result.status if advisory else "completed", outputs={**result.outputs,
                "development_task_id": task["id"], **metadata,
                "dependency_patch_tasks": [dep['id'] for dep, _, _ in dependency_patches],
                "execution_timing": {
                    "total_seconds": time.monotonic() - execution_started,
                    "host_candidate_seconds": collection_seconds,
                    "host_candidate_calls": collection_calls,
                },
                "business_diagnostics": business_diagnostics,
                "dependency_conflicts": dependency_conflicts,
                **({'diagnostic_repair_receipts': diagnostic_repair_receipts,
                    'diagnostic_repair_refs': command.payload.get('diagnostic_repair_refs', [])}
                   if command.options.get('workflow_version', 0) >= 40 else {}),
                **({"acceptance_status": "unverified"} if advisory else {}),
                "artifact_refs": {**result.outputs.get("artifact_refs", {}),
                    **({'goal_acceptance_report': report_ref} if report_ref else {}),
                    **({'goal_host_validation': validation_ref} if validation_ref else {}),
                    **validation_refs,
                    **({'host_candidate': host_candidate_ref} if host_candidate_ref else {}),
                    'candidate_capture': capture_ref,
                    "coder_patch": ref}},
                detail=result.detail if advisory else "",
                error_code=result.error_code if advisory else None)
        except DependencyPatchConflict as exc:
            record = {'task_id': task['id'], 'dependency_task_id': exc.task_id,
                      'paths': exc.paths, 'patch_path': exc.patch_path,
                      'detail': exc.output, 'agent_started': False}
            ref = _artifact(command, 'dependency-conflict.json',
                            (json.dumps(record, ensure_ascii=False) + '\n').encode())
            return handlers._result(command, 'blocked', detail=str(exc),
                error_code='dependency_patch_conflict', outputs={
                    'development_task_id': task['id'], 'agent_started': False,
                    'acceptance_status': 'unverified', 'artifact_refs': {
                        **{name: value for name, value in command.artifact_refs.items()
                           if name not in {'coder_patch', 'host_candidate', 'candidate_capture'}},
                        'dependency_conflict': ref,
                        **{f'dependency:{dep["id"]}': ref
                           for dep, ref, _ in dependency_patches}}})
        except TimeoutError as exc:
            return handlers._result(command, "blocked", detail=str(exc),
                                    error_code="coder_settlement_timeout")
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            return handlers._result(command, "blocked", detail=str(exc), error_code="coder_isolation_violation")
        finally:
            if settlement_context is not None and settlement_entered:
                settlement_context.__exit__(None, None, None)
            if publication_context is not None and publication_entered:
                publication_context.__exit__(None, None, None)


class DevelopmentIntegrateHandler:
    def __call__(self, command):
        root = handlers._run_root(command)
        workspace = project_path(root, _path(command.options.get('workspace', 'worktree'), shared=True))
        advisory = business_gates_disabled(command)
        business_diagnostics = []
        start = None
        try:
            if workspace.resolve() != workspace.absolute():
                raise ValueError('integration workspace must not traverse symlinks')
            plan = _plan(command)
            base, generation = _base(command)
            _clean(command, workspace)
            if _head(command, workspace) != base:
                raise ValueError("integration HEAD differs from development_base")
            if command.artifact_refs["development_plan"].get("metadata", {}).get("development_base") != base:
                raise ValueError("integration base differs from frozen plan")
            results = command.payload.get("development_results")
            if not isinstance(results, list):
                raise ValueError("integration requires every coder result")
            if not advisory and len(results) != len(plan["tasks"]):
                raise ValueError("integration requires every coder result")
            indexed = {}
            resolver_outputs = {}
            for result in results:
                carried = command.payload.get('carried_development_results', {})
                inherited = (advisory and isinstance(result, Mapping)
                    and isinstance(carried, Mapping)
                    and carried.get(result.get('command_id')) == sha256(json.dumps(
                        result, sort_keys=True, separators=(',', ':')).encode()).hexdigest())
                if (not isinstance(result, Mapping) or result.get("stage_id") != "coder"
                        or (result.get("run_id") != command.run_id and not inherited)):
                    if advisory:
                        business_diagnostics.append('ignored malformed or foreign coder result')
                        continue
                    raise ValueError("only completed coder results from this Run can be integrated")
                if result.get('status') != 'completed' and not advisory:
                    raise ValueError("only completed coder results from this Run can be integrated")
                outputs = result.get("outputs", {})
                identifier = outputs.get("development_task_id")
                if identifier in indexed:
                    if advisory:
                        business_diagnostics.append(f'ignored duplicate coder result: {identifier}')
                        continue
                    raise ValueError("duplicate coder result")
                patch_ref = outputs.get("artifact_refs", {}).get("coder_patch")
                if identifier not in {task['id'] for task in plan['tasks']} or not isinstance(patch_ref, Mapping):
                    if advisory:
                        business_diagnostics.append(f'coder result has no usable patch: {identifier}')
                        continue
                    raise ValueError("only completed coder results from this Run can be integrated")
                indexed[identifier] = patch_ref
                if result.get('status') == 'completed':
                    resolver_outputs[identifier] = outputs
                if advisory and isinstance(outputs.get('business_diagnostics'), list):
                    business_diagnostics.extend({
                        'task_id': identifier, 'diagnostic': diagnostic}
                        for diagnostic in outputs['business_diagnostics'])
                if result.get('status') != 'completed':
                    business_diagnostics.append(f'using partial patch from coder result: {identifier}')
            if not advisory and set(indexed) != {t["id"] for t in plan["tasks"]}:
                raise ValueError("coder result task set differs from plan")
            patches = []
            for task in plan['tasks']:
                if task['id'] not in indexed:
                    if advisory:
                        business_diagnostics.append(f"no patch available for task: {task['id']}")
                        continue
                    raise ValueError("coder result task set differs from plan")
                try:
                    patches.append((task, _check_ref(command, indexed[task['id']], task, base, generation)))
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    if not advisory:
                        raise
                    business_diagnostics.append(f"patch unavailable for task {task['id']}: {exc}")
            start = base
            integration_conflicts = []
            for position, (task, path) in enumerate(patches):
                resolvable_paths = set()
                for resolver, _ in patches[position + 1:]:
                    outputs = resolver_outputs.get(resolver['id'], {})
                    changed_paths = set(indexed[resolver['id']].get('metadata', {}).get('paths', ()))
                    for conflict in outputs.get('dependency_conflicts', ()):
                        if isinstance(conflict, Mapping) and conflict.get('task_id') == task['id']:
                            resolvable_paths.update(set(conflict.get('paths', ())) & changed_paths)
                _apply(command, workspace, path, task,
                    conflict_handoff=integration_conflicts if advisory else None,
                    resolvable_paths=resolvable_paths)
            for conflict in integration_conflicts:
                for relative in conflict['paths']:
                    target = workspace / relative
                    if target.is_file() and re.search(rb'(?m)^(<<<<<<< |>>>>>>> )', target.read_bytes()):
                        raise ValueError(f'unresolved dependency conflict in {relative}')
            business_diagnostics.extend(integration_conflicts)
            repair_receipts = []
            if command.options.get('workflow_version', 0) >= 40:
                from .diagnostic_repairs import apply
                # Repairs already exported by an included coder are part of
                # its patch, including any later edits it made to that file.
                # Reapplying the earlier snapshot here could undo those edits.
                included_repairs = []
                included_task_ids = {task['id'] for task, _ in patches}
                for result in results:
                    outputs = result.get('outputs', {})
                    if outputs.get('development_task_id') not in included_task_ids:
                        continue
                    for receipt in outputs.get('diagnostic_repair_receipts', ()):
                        if receipt.get('status') in {'applied', 'already_applied'}:
                            included_repairs.append(receipt.get('repair_ref'))
                pending = [ref for ref in command.payload.get('diagnostic_repair_refs', ())
                           if ref not in included_repairs]
                repair_command = replace(command, payload={**command.payload,
                    'diagnostic_repair_targets': [
                        {'task_id': task['id'], 'plan_ref': command.artifact_refs['development_plan']}
                        for task in plan['tasks']]})
                repair_receipts = apply(repair_command, workspace, pending)
                changed = sorted({path for receipt in repair_receipts
                                  if receipt['status'] == 'applied' for path in receipt['paths']})
                if changed:
                    _git(command, workspace, 'add', '--', *changed)
                    _git(command, workspace, 'commit', '--no-gpg-sign', '-m',
                         'Apply isolated diagnostic corrections')
                business_diagnostics.extend(receipt for receipt in repair_receipts
                                            if receipt['status'] in {'conflict', 'invalid'})
            head = _head(command, workspace)
            integrated_task_ids = [task['id'] for task, _ in patches]
            ref = _artifact(command, "development-integration.json", (json.dumps({
                "base": base, "head": head, "generation": generation,
                "tasks": integrated_task_ids, "available_task_results": list(indexed),
                "business_diagnostics": business_diagnostics}, sort_keys=True) + "\n").encode())
            return handlers._result(command, "completed", outputs={"development_base": base,
                "head": head, "integrated_task_ids": integrated_task_ids,
                "diagnostic_repair_receipts": repair_receipts,
                **({'diagnostic_repair_context': {
                    'plan_ref': command.artifact_refs['development_plan'],
                    'task_ids': [task['id'] for task in plan['tasks']],
                    'included_refs': [*included_repairs, *[
                        receipt['repair_ref'] for receipt in repair_receipts
                        if receipt['status'] in {'applied', 'already_applied'}]]}}
                   if command.options.get('workflow_version', 0) >= 40 else {}),
                "business_diagnostics": business_diagnostics,
                "artifact_refs": {"development_integration": ref}})
        except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
            if start is not None:
                from .workspace import workspace_spec
                binding = workspace_spec(root)
                if binding and binding['mode'] == 'direct' and workspace == project_path(root):
                    return handlers._result(command, 'failed',
                        detail=f'{exc}; direct workspace retains partial edits for inspection',
                        error_code='integration_conflict', outputs={'partial_edits_preserved': True})
                rollback = _git(command, workspace, "reset", "--hard", start, check=False)
                if rollback.returncode:
                    return handlers._result(command, "blocked", detail=f"{exc}; rollback failed", error_code="integration_rollback_failed")
            return handlers._result(command, "failed", detail=str(exc), error_code="integration_conflict")


def build_development_registry():
    return {"implementation": ImplementationHandler(), "coder": CoderHandler(),
            "development_integrate": DevelopmentIntegrateHandler()}
