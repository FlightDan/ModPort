# ModPort desktop application interface

The desktop application presents a project form, execution settings, and an
execution view organized as Preparation, Implementation, and Testing. These
are presentation groups over the current workflow; a migration instance is
one execution of that workflow. Workflow, SDK, model policy, and application
versions remain independent.

The native window communicates with a loopback-only application service. The
service authenticates every API request with an application-owned credential;
the renderer does not receive shell or unrestricted filesystem access.

## API contract

JSON requests use the following endpoints. Failures return a non-2xx status
and an object containing `error` (a user-readable message).

`Accept-Language` selects `zh-CN` or `en` for application-authored display
messages. Missing/unsupported languages fall back to English; Chinese regional
tags map to simplified Chinese. The locale is scoped to each request, never to
the stored Run. Keys, state values, user/model content and raw diagnostics remain
unchanged. Task items include `stage_id` and `label_is_stage` so only generated
stage labels are translated; authored task titles retain their original text.

The trusted Electron preload exposes `getLanguage()` and `setLanguage(locale)`
as promises returning `en` or `zh-CN`. The main process persists the selection
in its user-data `ui-settings.json`, uses it for native dialogs, and attaches
the request header. The renderer never receives a preference file path or
direct filesystem access.

- `GET /api/bootstrap`: `workflow_version`, `platform`, `defaults`,
  `model_config`, `roles` (`id`, `label`, `group`), `environment` (`ready`,
  `checks`), and `recent_runs`.
- `POST /api/repository`: `{repository, revision?}`; returns `branches`,
  `tags`, `default_revision`, `detected` version fields, and `warnings`.
- `POST /api/local-source`: native-main-process-only `{path}` after folder selection;
  returns an opaque `token`, `display_path`, `name`, `detected`, `warnings`, and
  `git` (`available`, `is_repository`, `root`, `branch`, `dirty`, `has_commits`,
  `can_branch`, `reason`). The renderer cannot choose arbitrary paths.
- `POST /api/runs`: project/version fields using the MigrationRequest names,
  plus `project_name`, `max_seconds`, `max_tokens`, `model_config`, and
  optional `max_parallel_coders`; returns `{id, project_name, status}`.
  Local requests use `source_mode: "local"`, `local_source_token`, and
  `local_workspace_mode: "git_worktree" | "copy" | "direct"`. Git mode requires
  `local_branch_name`; direct mode requires `direct_workspace_confirmed: true`.
  Remote requests reject all local selection/mode fields.
- `GET /api/runs/{id}`: `id`, `project_name`, `status`, `elapsed_seconds`,
  `budget` (`max_seconds`, `max_tokens`, `used_tokens`,
  `token_usage_complete`), `stages`, `messages`, `supervisor`, and `notice`.
  Local instances also expose `workspace` with `mode`, `path`, `original_path`
  and optional `branch`. Native `openWorkspace(instanceId)` resolves this
  authenticated status and opens the registered directory; it accepts no path.
- `POST /api/runs/{id}/chat`: `{message}`; returns an accepted message record.
- `POST /api/runs/{id}/cancel`: `{confirmed: true}`; requests supported
  cancellation of this exact instance.
- `POST /api/setup`: `{action}`; performs an explicitly selected managed
  environment preparation action and returns its result.

### Wiki drafts and GitHub

These endpoints are authenticated host actions. They are not agent tools.

- `GET /api/wiki/contributions`: `{drafts}` with compact draft previews;
  bodies and file payloads are loaded through the individual UUID endpoint.
- `GET /api/wiki/contributions/{uuid}`: a draft with `id`, `title`, `body`,
  JSON `content`, portable `files`, `status` and optional `pr_url`.
- `POST /api/wiki/contributions/{uuid}`: any subset of `title`, `body` and
  `content`; returns the saved draft. UUIDs are canonical lowercase UUIDs
  with hyphens. Origins and submission receipts are private host state.
- `POST /api/wiki/export`: `{instance_id}`; exports optional findings from
  the registered Run and returns draft previews and diagnostics.
- `POST /api/wiki/update`: `{revision?}`; downloads a research cache for
  later Runs and returns its revision and status. Existing Runs keep their
  saved material. Download failures retain the previous cache.
- `GET /api/github/status`: CLI availability, authentication state, current
  account, repository and credential-storage notice. No token is returned.
- `POST /api/github/login`: `{}`; starts GitHub CLI browser login.
- `GET /api/github/login`: current `state`, optional `verification_url`,
  `user_code`, `login` and redacted `error`. States are `idle`, `starting`,
  `waiting`, `authenticated`, `failed`, `expired` and `cancelled`.
- `POST /api/github/login/cancel`: `{}`; cancels that sign-in operation.
- `POST /api/wiki/contributions/{uuid}/submit`: `{expected_login}`; submits
  the reviewed draft using the displayed account and returns submission
  status and a Draft PR URL. Failures keep drafts and private replay receipts.

The preload's `openContributionLink(url)` permits only the official GitHub
device page, the configured Wiki repository and its canonical PR URLs. It
rejects arbitrary hosts, embedded credentials, ports, queries and fragments.
See [knowledge integration](WIKI_KNOWLEDGE.md) for tested scope and remaining
live-authentication and native-platform limits.

`stages` always contains `preparation`, `implementation`, and `testing`, with
`label`, `state`, and `items`. Each item has `id`, `label`, `state`, `detail`,
an optional raw `error_code`,
`active_agents`, `active_subagents`, and optional `counts` (`completed`,
`total`, `label`). Item states are `pending`, `queued`, `running`, `waiting`,
`completed`, `failed`, and `cancelled`. Counts come from observed tasks/cases,
not fabricated percentages. Unknown subagent counts are `null`.

Messages have `id`, `role` (`user` or `supervisor`), `content`, and `state`
(`queued`, `running`, `completed`, or `failed`). `supervisor.busy` indicates
an outstanding conversational assignment. User chat is advisory and reads
current instance evidence; lifecycle commands use explicit controls.

## Display rules

Every visible subtask occupies its own row, with its own active agent and
subagent counts. Pending and queued work that has not started is hidden.
Completed items collapse into an expandable summary. Space-constrained active
items collapse behind a labelled count and expandable control. Failed or
attention-required items stay visible. The Supervisor conversation remains
available while execution continues. The UI uses real service responses and
never substitutes demonstration execution state for missing backend data.

Stage columns group tasks and do not determine the Run lifecycle. A column with
active tasks keeps its active status and also shows a failed-task count. If it
has failed tasks but no active work, it displays “Has failed tasks” rather than
“Failed”. Individual failures retain their red status, task ID, details and any
reported error code. Only the top-level Run status indicates that the instance
itself failed or stopped; no task outcome or scheduler state is rewritten.
