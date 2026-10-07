## Workflow v17 diagnostics without business gates

The current gate policy is `disabled`. Report shape, task ownership, approval,
self-checks, test outcomes, behavior coverage and knowledge-gap disposition do
not block downstream execution. A settled stage forwards its original result
and available artifacts without automatic retry, replan or mandatory handoff.
Only an explicit `request_rework` call requests upstream revision.

Retain original failed results and raw author reports. Normalized task settings
are an execution projection; they do not attest that the report is complete or
approved. Missing fields, reordered dependencies and discarded nonproject path
declarations are recorded as diagnostics. Partial patches may be exported and
integrated. Baseline project edits are recorded explicitly and cannot represent
unmodified original behavior.

Checks and independent reviews retain their observed outcomes. An execution can
finish and deliver available artifacts with `acceptance_status: unverified` even
when diagnostics remain. Never rewrite a failed check or fabricate a passed
review, behavior witness or acceptance result. Host-owned identity/reference
binding, workspace containment and project-execution sandbox constraints still
apply. This policy adds no source, candidate, rubric or workspace fingerprint
checks.
Frozen v12–v16 records are preserved; only a new explicitly linked v17 segment
uses this policy. All older business gate requirements below are historical.

### Two-turn assignment evidence

Definitions with `agent_dialogue_policy.version = 1` record a planning reply
and an execution reply in the same managed agent session (OpenCode for
workflow v24 and later Runs; archived Codex conversations remain historical). The plan is
Markdown saved under the execution's `dialogue/` directory; it is not a review
decision, test result, or acceptance witness. Both submitted prompts, the final
raw reply, the second-turn JSON Schema when applicable, and session identity
are retained separately. The two turns share one assignment and deadline.

The host writes the final reply to the existing report path. Characterization
test declarations arrive as `{test_id, declaration}` rows and are mapped to
the existing `test_evidence` object; duplicates remain diagnostic instead of
silently overwriting another declaration. Skill envelopes materialize only
the host-declared output files. Nullable supervisor slots are translated to
the existing optional-field representation before interpretation. These
transformations retain the raw reply and do not certify its contents.

Frozen definitions without the dialogue policy retain their prior protocol.

## Workflow v16 diagnostic handoffs (historical)

The v16 gate policy is `downstream_toolcall`: reports and check failures never
implicitly authorize an upstream assignment. The SDK retains each original
failed result. `gate_diagnostics` records the downstream consumer and whether
it forwarded the diagnostic or consumed a successful tool-requested result.
The existing MCP request ledger binds every rework to its live caller, target,
request ID, budget, original assignment and returned evidence. Ordinary report
prose has no rework routing authority.

`gate_handoff` writes an execution-specific Markdown report without a machine
verdict. It may consume partial work or explicitly request an upstream revision
or a recheck. The host resumes from the actual settled SDK tool task; it does
not rewrite the failed attempt. Unrepaired diagnostics remain unmet acceptance
evidence even when downstream work proceeds.

Planning normally has two Markdown revisions (draft and improvement), followed
by task organization and dispatch. Additional corrections require a tool call.
Research scope is the producer's current unresolved applicable subset and
research kinds. Omitted dispositions remain unresolved. Compatible duplicate
verification requirements are merged; ID collisions across parents receive
stable host IDs and retain `source_gap_id` and per-contribution provenance.
Frozen contracts, source/manifest identity, authentic execution and final
acceptance requirements are unchanged. The v15 and older sections below describe
historical records; their automatic repair rules do not apply to v16.

## Workflow v15 fixed planning artifacts

V15 performs draft, improvement, major-omission correction, task organization and dispatch exactly once each.
The two corrections return complete Markdown, with no verdict or replan control. The host seals each revision
in its own execution directory, records parent SHA-256 and generation, and persists the current reference.
Synthesis and dispatch bind the final Markdown reference to immutable execution settings. Dispatch prose is
context and cannot modify the validated DAG. All model assignments and summaries use gpt-5.6-luna/high.
Historical sections below describe earlier formats; they do not authorize v15 planning back edges.

# ModPort Evidence Protocol

The run's shared `acceptance-rubric.json` is the source of detailed acceptance
criteria. This document defines the evidence shape and authentication rules;
it intentionally does not duplicate the full rubric.

## Provenance

- Record the source repository commit used for the migration.
- Record every `test_evidence` mapping from an acceptance test identifier to
  its output, artifact, and authenticated provenance. A mapping without the
  referenced evidence is incomplete.
- Preserve the frozen contract, its selected version fields, and parent evidence
  references. A log line alone is never an evidence artifact.

## Behavior requirements

- `behavior_requirements` are extracted from source code and documentation by
  reading only. Record `source_assumption: user_confirmed_functional` and
  `verification_basis: source_reading`; these fields are assumptions and
  provenance, not a claim that source behavior passed at runtime.
- Give each requirement a stable behavior ID, observable assertions, source
  anchors (file and relevant location), and explicit uncertainties. Keep
  uncertain details visible instead of resolving them by running source code.
- Do not generate, execute or repair a source test harness. Do not require
  source runtime witnesses, source test IDs, or source pass results.
- Preserve already frozen v33 contracts, tests and runtime evidence as
  historical records. Do not rewrite them to fit the v34 requirement format.

## Target contract and runtime evidence

- Design an executable target contract independently from the source reading.
  Assign target case IDs and map each target case and assertion to one or more
  frozen behavior requirement IDs. There is no requirement to port source test
  IDs, paths, harness code or runtime evidence.
- The runtime `.modport/functional-contract.json` retains its authored top-level
  `behaviors` and `test_evidence` schema after freeze. Host provenance is stored
  separately in the archived `target-contract-lock.json`; runtime readers do not
  read that wrapper. The runtime and archived contract bodies describe the same
  frozen declarations.
- Freeze retained behavior coverage and this mapping in the target contract.
  For any target behavior acceptance claim, every required case must have fresh
  target execution evidence and passing assertions. Skipped, missing,
  unimplemented or unexecuted cases remain unverified and cannot count as pass.
- Target evidence identifies the target case and mapped behavior requirement,
  the actual target command/runner, execution context, output and observed
  assertion result. Bind host-owned execution identity and artifact references
  in the host; do not make the harness calculate identity or fingerprints.
- Use the official GameTest server runner for suitable server-side behavior,
  matched to the locked Forge/NeoForge version. Batch client cases in a session,
  reset relevant state explicitly before each case, and restart only where
  isolation requires it. If a shared runner fails to launch, record that launch
  failure once and mark remaining cases unexecuted, never passed.
- The host binds each selected native GameTest task to XML under
  `build/test-results/<selected-task>/`, including task aliases. It configures
  the official reporter and consumes exact class/name identities from that same
  task directory. Do not infer passes from arbitrary XML paths or another task.
  The reusable runner owns launch/report/session mechanics; locked-version
  adapters own game APIs, and individual mods own their behavior assertions.
- Keep target execution within the credential-free sandbox. Artifact
  verification may compile and run its isolated harness, but must not edit the
  delivered JAR or product source.

## Workflow v12 input preparation and planning references

Workflow v12 planning and agent stage handlers share one input-preparation clock per command. The
default ceiling is 300 seconds and `MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS`
may lower it but may not exceed 300. Run deadline exhaustion is also a failure.
Model, native-goal and summary execution is enclosed by `model_work`; its
elapsed time is recorded as `excluded_model_seconds` and is excluded from this
host preparation ceiling. Host reference checks, context archiving, manifest
construction, manifest writes and terminal progress writes remain preparation
work. A main-process worker may use its own `SIGALRM`; a thread worker uses
cooperative checkpoints, cannot interrupt a blocked host call, and preserves
an existing external handler/timer.

The host records schema_version=1 progress at
`artifacts/executions/<command_id>/input-preparation.json`. The record includes
the command identity, status, current phase, timeout, host and excluded model
seconds, interrupt mode, per-phase timings, counters and update time. A
timeout, invalid configuration, or failed progress audit blocks the command;
an incomplete manifest or context is never accepted as a planning input.

Planning and repair inputs are passed by authenticated reference in workflow
v12. The complete input binding or repair context is archived once under
`artifacts/planning-context/<sha256>.json`. A context binding has exactly
`source_ref`, an empty `json_pointer`, and `referenced_artifacts`; the source
reference carries `metadata.document_kind=modport-planning-context-v1`, and the
artifact list is the transitive closure needed to copy the context during a
retry or child Run. The archive is content addressed and its bytes are checked
before use.

The current failure stays inline in `repair_context` and in the protected
planning prompt, including its stage, execution identity, status, error code
and detail. Historical `prior_attempts`, `prior_findings`, `failure_input`,
`upstream_results` and `parent_context` are stored in a single history source
with per-field history pointers and are retrieved only when relevant. Preceding planning
rounds are also referenced by their authenticated stage artifacts. The host
retains the original JSON and all referenced evidence; a summary, model echo or
Markdown projection cannot replace it. Every v12 planning output exposes the
manifest and the referenced-artifact closure so retry copying does not lose a
nested source file or rewrite historical bytes. Older workflow versions keep
their frozen inline handoff format.

## Workflow v12 coder validation and scoped regression

Every immediate development task and execution group carries its frozen
`validation_checks`. Workflow v12 defaults `validation_kind` to `regression`.
The union of `gradle_regression` checks must cover every exact task acceptance
criterion. A regression check uses fully qualified direct Gradle Test task
paths such as `:test` or `:module:test` and explicit relative JUnit XML files
under a `build/` directory. The host runs a fresh candidate snapshot with old
reports and caches disabled, installs a host listener, and records a fresh
nonce-bound execution record. The listener must observe each requested Test
task as executed with actual nonzero cases, zero failures and zero skips, and
each declared report must belong to that task's JUnit output directory. Exit
status, aggregate tasks, NO-SOURCE, stale XML and declared totals alone do not
prove a regression. Other structural checks are supplementary.

A purely structural task may use `validation_kind=structural` only with a
nonempty `structural_reason` that was independently reviewed. Structural and
regression tasks cannot be mixed in one execution group; grouping preserves the
source checks, kind and reason and cannot drop an acceptance criterion.

The independent `goal_prepare` round creates one native goal per execution
group from four authenticated planning references. Before a v12 coder patch is
exported, the host requires a host-reserved acceptance report containing the
complete acceptance mapping and a `self_check` object with exactly
`state="passed"`, unique `reviewed_paths` covering every changed owned file,
unique `checks` covering every frozen host check ID, and a nonempty `summary`.
The host independently checks the candidate snapshot, commit, ownership,
frozen checks, self-check and report bytes. Native-goal completion, a patch or
a coder's claim cannot release the patch; failed checks return to the same
goal with feedback. A successful goal remains subject to integration,
independent review and final acceptance. The exported `goal_host_validation`
record also exposes every explicit host-proof file reference (including
manifests, listener records, logs and JUnit reports) as an artifact reference,
so retry and child Run copying retains the evidence bytes alongside the
normalized proof.

After a successful `code_review`, v12 starts a new regression generation. The
host partitions every frozen behavior ID into disjoint `scope-XXX` records,
with no more scopes than `max_parallel_coders`, and assigns any
`test_execute` verification obligations to a concrete scope. Each scope has a
separate `test_design.g<generation>.scope-XXX` assignment, clone and cache;
the author writes only `.modport/independent-tests/` and does not run project
code. Its `test_execute.g<generation>.scope-XXX` runs the declared direct Test
tasks in a credential-free sandbox, using fresh outputs and the host listener.
Scoped suites must cover exactly their assigned behavior IDs, produce actual
passing JUnit cases without skipped tests, and retain task/report/nonce and
workspace identity in the result.

The deterministic `test_execute.g<generation>.join` operation accepts only
completed design and execution branches from the same Run and generation. It
rechecks each branch's contract/rubric, command identity, workspace, fresh
result and exact scope coverage; the aggregate `covered_behavior_ids` must
equal the frozen contract set and `uncovered_behavior_ids` must be empty. The
join is the authenticated aggregate evidence and must complete before
acceptance stages are scheduled. A failed or cancelled branch cannot join;
running siblings settle before the host routes the failure for repair.

## Client and gate evidence

- Use `static_client` only for behavior that cannot be automatically observed;
  record the specific reason and the manual observation required.
- A `static_client` exception never replaces the `client_smoke` gate. The
  frozen contract must declare executable startup coverage with runtime
  `test_evidence` whose executor is `client_smoke`; target client verification
  obtains fresh evidence under the current execution. Preserve the declared
  operations/assertions and authenticate actual witness sites, nonce and the
  captured log. Mere lifecycle status does not satisfy the mapping.
- Acceptance is the conjunction of the applicable rubric gates and verified
  evidence. Build/test output, dispatcher logs, and agent reports are useful
  context but do not by themselves pass a gate.


## Research, development and independent-test records

Research references are stored in `artifacts/skill-references.json`. Each
`SkillReference` fixes kind, skill_id, exact source/target identities, bundle
metadata, coverage and independent approval. When both kinds are present, links
bind the platform revision to the Java identity and version pair.
The host validates contained artifact paths, and consumers may verify supplied
file digests when artifact bytes are required; it does not require a separate
content-addressed object copy for every reference. Candidate bundle scripts are
data; only the packaged trusted scanner runs.

In workflow v7, development-plan.json is produced only after four authenticated
planning rounds and independent grouping approval. Implementation rechecks the
current approved planning inputs, source commit and task DAG; an old on-disk plan
or ancestor commit is insufficient. The plan does not require an agent-supplied
whole-workspace digest. See the planning chain contract below.
Coder patch references include task_id, development generation, base, start, head,
owned changed paths and plan identity metadata. Each patch is the task's own
start..head delta. Dependency deltas are consumed once in frozen topological order. A terminal
SDK attempt is not a successful join unless its business result is completed.
Rejected or cancelled assignments remain evidence in their original generation.

Independent test design produces `independent_test_suite`,
`independent_test_snapshot` and source references. The snapshot binds the
approved contract, rubric, suite files and protected paths; any file digests are
  artifact metadata for those declared inputs. Execution mounts product and suite
read-only, gives conventional output roots separate writable mounts, disables
task/build-cache reuse and deletes declared old XML outputs before running.
`independent_test_result` binds the suite to the host command, its exit status and
newly observed JUnit reports. Covered and uncovered behavior IDs remain explicit;
this suite never replaces full frozen characterization verification or client
gates. XML and task success do not independently prove assertion quality; the test
author must supply real behavioral assertions under the independent role contract.

Audit is supplementary provenance, not acceptance evidence. Every process
invocation has a distinct UUID and durable public logs. SDK events are copied via
public APIs into ModPort's own audit database, with replay deduplication. Input,
cached input, output and optional reasoning-output token observations retain their
provider granularity. Cached input and reasoning output are subsets, not extra
tokens. Unknown counts remain null; different aggregate granularities are not
added together. Private reasoning is omitted and credentials are redacted before
persistence. An unfinished invocation is interrupted_or_unknown, not a proved
completion. Explicit CLI control requests are also recorded.

## Planning and repair artifacts (v7-v12)

The authoritative aliases are migration_inventory, migration_plan, migration_tasks
and parallel_review. The first three rounds produce free-form reports. The host
stores each report verbatim in a raw_report wrapper and supplies it directly to
the next agent. Missing JSON punctuation, omitted fields, alternate organization,
or an omitted manifest/hash echo never reject a report. Run, generation and
producer routing metadata come from the host, not model output. The independent
reviewer owns completeness, consistency, evidence assessment and task coverage.

At the actual scheduling boundary the reviewer supplies the executable DAG or a
replan decision. The host checks the machine parameters it consumes: task IDs,
paths, dependencies, check commands and deferred execution stages. Narrative
reasoning and prior reports do not need exact source-field copies. Historical
structured planning artifacts remain readable as context. No extra model call
is added to repair report formatting.

Coder context preparation also produces free-form text. Reviewed execution
settings are attached by the host rather than retyped by the context author.
The coder's handoff report is retained as text for independent review; its JSON
shape, criterion echoes and self_check fields do not control acceptance. Actual
host-executed checks still determine whether the candidate passes those checks.

Research reports at .modport/gap-research/report.md and gap-plan proposals are
forwarded intact to their independent reviewers. Saved research sources are
collected directly from the sources directory. Review reasoning is free-form;
only workflow routing and explicit state updates require machine parameters.
A review can finish with MODPORT_DECISION: approved or rejected, or use a fenced
JSON control block for gap resolutions and task updates. The host supplies
reviewer, rubric and submission metadata. Reports are not rejected for missing
findings fields, IDs or hashes.

The development_prepare record binds planning_generation, the original and new
commits, task_objectives artifact identity, completed_task_ids, changed_paths,
structural checks and patch reference metadata. The host checks per-commit ownership,
the actual before..after diff and the current prepared checkout. Fourth-round approval requires
an independent preparation_assessment that explains whether the actual shared
interfaces still conform to the strategy. The record bridges the original planning
snapshot to development_base without treating the mutable candidate as a frozen
workspace digest.
Completed preparation, pending coder tasks and reviewed downstream obligations
are disjoint and jointly complete. Downstream closure criteria stay pending in
project_verification_gaps and final gap_review must prove them. Current tasks
carry targeted validation_checks frozen through review and goal preparation.

Repair aliases are contract_diagnose/contract_repair_plan/contract_repair_tasks/contract_repair_review and
the corresponding target aliases. Revise authenticates this chain and checks
actual modified paths. A missing or malformed contract draft remains diagnosable;
readable obligations are retained and fixed source/rubric identity stays enforced.
This leniency does not relax frozen-contract validation. New candidates require
fresh runtime validation and independent review; an authenticated plan is not
proof that its implementation preserves behavior.

v8 repair handoffs additionally carry a host-owned repair_context, identical
across diagnosis, solution, task generation and revise. It includes the original
failure execution/result, request, upstream results, prior findings, parent Run
context and flat prior-attempt history. Repair evidence is copied into contained
content-addressed repair-evidence files so later log reuse cannot rewrite history.
Each repair document binds failure_execution_id, original input references and
the same candidate commit. The host attaches the context rather than trusting an
agent to summarize or reproduce it. The third round emits consistency_review;
conflicts or incomplete task/strategy/issue mappings are retryable output errors.
The host builds repair_work_package with one authenticated context binding, all
three round documents and each task's selected issues/strategies. Revise rechecks this package
against the original chain before exposing it to the coder. Ordinary migration
planning remains free of whole-workspace fingerprint requirements. These checks
establish handoff consistency, not correctness of the model's diagnosis or fix.
Repairable development failures restart migration_inventory with repair_scope
`migration`: the same WHY/HOW/package contract precedes the existing independent
parallel review and DAG scheduling. Individual coders receive the complete
package reference and retrieve relevant evidence from its authenticated closure.
Parent packets include original stage results even for older Runs that
predate repair_context, with explicit child-local evidence mappings.
Review/preparation-requested replans preserve separate host-owned repair_feedback
with frozen results and execution-scoped rework_evidence aliases. Later planning
rounds and coders receive this feedback without changing the original failure
context or replacing earlier document bindings.

Workflow v11 introduced goal_prepare per execution group after independent review,
for ordinary development and both repair scopes. Workflow v12 additionally
requires the sealed coder goal to bind validation kind and host checks. The sealed coder_goal binds
task ownership, dependencies, source acceptance and four planning references.
Native goal evidence records thread identity, status, validation feedback,
turns and observed token/time usage. Only native completion plus host acceptance
releases a coder patch; ordinary turn completion cannot release it. Failed checks
reactivate the same native goal with feedback. These checks establish declared
task properties; independent semantic review and final acceptance still apply.
Goal generation and coder launch each charge one host assignment; native turns
remain inside that coder assignment and the existing deadline/cancellation.
Repair preparation seals an isolated source snapshot and rebased reviewed plan.
Integration verifies both references and unchanged source files before publishing
only owned changes. Publication supports rollback of caught errors, not atomic
multi-file recovery after host failure. Old execution evidence remains intact.
Task-contract changes after gap review require fresh planning and goals; prior
patches remain evidence and are never silently reused with changed boundaries.
Prompt regions bind lengths and digests. Only the historical region can shrink;
current requirements, source acceptance and protected identities remain exact.

## Client lifecycle diagnostics and partial results

Host support files are stored as harness_support refs in each new Run. launch.py
and preflight.py execute inside the build sandbox, using the same environment as
the workload. MODPORT_CLIENT_PREFLIGHT contains an execution-bound environment
report with acceptance_evidence=false. Java, DISPLAY/GLX, game-directory and audio
observations describe capabilities; they do not prove Minecraft startup or sound
correctness. The launcher manages only its own process groups and records bounded
preflight/workload failure or cancellation. The host still enforces a hard deadline.

MODPORT_CLIENT_STATE lines contain schema_version=1, kind=client_diagnostic,
execution_id, increasing sequence, elapsed_ms, stage_elapsed_ms, stage, screen,
overlay, world_present, player_present, last_test, error_code and detail.
The supported stages are startup, resource_loading, onboarding, title,
world_creation, world_ready, characterization, resource_reload and finished.
The host checks identity/order but marks milestones as untrusted diagnostics.
A fabricated world_ready line cannot authenticate a test or close an obligation.

The version-specific startup adapter only advances recognized screens through
normal APIs. Unknown screens are terminal diagnostics. The generated harness
must create an isolated world, observe actual world/player state, run assertions
and await resource reload. Screenshots, when available, are separate artifacts;
an unresponsive client relies on host timeout/log collection.

Failure paths retain raw logs, valid incremental evidence and record errors.
Only evidence meeting the full source/executor/nonce/witness protocol contributes
to authenticated_test_ids. Partial success never passes the complete gate or
becomes fresh evidence for a later execution. Diagnostic records include category,
error_code, first_failed_task, last_milestone, execution identity, test-source
references, missing tests and raw_log_refs. Source commits and execution identities
identify the relevant inputs; mutable workspace changes are not a diagnostic failure.
Unknown errors remain unknown.
An OpenAL warning is recorded separately and is not automatically a startup cause.

The host compares failure signatures, observed milestones and new authenticated
test IDs within a repair chain. Workspace or artifact metadata churn alone does not
demonstrate progress.
Repeated same failures without progress escalate diagnosis at 2 and stop at 3;
normal waits and cancellation are not repair-triggering stagnation. Diagnostic
progress and acceptance remain separate even when lifecycle state advances.

## Child retry and authenticated harness restoration

Explicit same-workspace continuation uses a new SDK segment while retaining the
logical migration identity. The old header and attempts stay historical; only
new commands bind to the current deployment. Contract planning may reuse the
unchanged diagnosis packet, but later plans and contract approvals are discarded.
Carried outputs are archived before dispatch. Neither segment continuation nor
research-review retry certifies unresolved project behavior or knowledge.

Baseline author results archive harness source bytes before publishing their
references. Historical manifests and repair inputs use these independent
copies, so a later author deleting or replacing a required output cannot erase
earlier evidence. This archive does not certify the current workspace or replace
fresh contract verification.

An explicit retry accepts a settled failed/cancelled v2 parent. The child freezes
its parent association, declared budget overrides, before/after values and reason.
Only Budget fields can differ from the source-bound parent request. No parent
header, deployment or evidence is rewritten and no child is created automatically.

For v34, source contracts and source harnesses in a parent package remain
historical evidence and do not become execution inputs or acceptance evidence.
The child re-extracts `behavior_requirements` from source code and documentation,
then designs its target contract and mapping independently. If a target harness
is carried forward, the child still requires fresh target execution; parent
successes and partial evidence never substitute for it.

These protocols and fixture tests do not establish real Minecraft acceptance.
Actual startup, world behavior, onboarding variants, unknown screens, unavailable
display/audio, reload failure, timeout and cancellation require real sandbox runs.
The previously cancelled Run remains cancelled.

## Project research and independent alternative evidence

Strict analysis schema_version=2 gap rows carry entry_id, the exact supplied catalog gap_id,
kind, applicable, status, question, existing_answer, missing_information,
usage_locations, evidence, affected_tasks, closure_criteria and resolution_stage.
Each applicable row cites a real baseline-relative file and positive one-based
line; an optional symbol must occur on that line. New entries use new_entry=true.
Non-applicable rows require nonempty checked_scope, absence evidence and no
conflicting use. Disputed applicability remains unresolved pending independent
review. Legacy scanner indexes are compatibility references, not stable identity.
The legacy v12 kind:entry_id form remains compatible where unambiguous; current
catalogs qualify skill and may publish an encoded collision-safe ID. Copy the
supplied identity rather than reconstructing or shortening it.

Research reports record gap_findings, actual saved source snapshots with origin,
and optional portable generic_knowledge_entries grouped by platform/java. Source
bytes and claims remain distinct. research_review independently assesses these
claims before mod_analysis reassessment. An approved research_review may retain
findings only when every finding explicitly has severity=info and blocking=false;
these are informational context and do not reopen or block the review. A
blocking, non-info, missing, or non-boolean classification requires rejection;
unknown findings fail closed. Knowledge review cannot pass execution obligations.
The host separately records per-kind research usage and project research/verification
gaps; writing an artifacts projection cannot change state.

Research/admin review verification_requirements may reference a host project research
identity or existing gap_obligations identity through research_gap_id. Each gap_id
must be nonblank and unique in the review. due_stage is target_build, test_execute,
or client_smoke; if resolution_stage is supplied it must equal due_stage. An
existing obligation's stage and research binding cannot change. The host merges
additional closure criteria without replacing original task scope, source evidence,
usage locations or provenance; model-provided extra metadata cannot replace host
fields. Each accepted contribution records its review execution. New checks remain
pending, and later analysis retains reviewed additions. Applicability can expand
when analysis discovers usage, but cannot silently remove an existing obligation.
Unknown parent references, conflicting stages and malformed batches fail before
publishing any verification requirement updates.

Gap-plan authors write proposals in their preferred format. The independent
reviewer reads the original proposal and emits any approved gap resolutions and
task updates at the state mutation boundary. Rejection carries no approved
changes. The host validates affected tasks, dependency updates and verification
duties before applying them; it does not require approved fields to reproduce a
model-authored proposal byte for byte. Research cannot certify runtime acceptance.

Administrator submissions use schema_version=1, submission_id, run_id,
workflow_version, execution_version, knowledge_revisions, base_gap_revision,
source_commit, gap_resolutions, sources and generic_knowledge_entries.
Current version and revision metadata is exposed in research-budget.json;
base_gap_revision copies its gap_revision. Source identity comes from the
Run's authenticated source_evidence. Import validates the current identities,
copies contained source files and queues independent admin_review. Its
submission_id binds the review; source aliases have the form
admin:<submission_id>:source:<zero-based index>. Repeated submission IDs are
idempotent. See WORKFLOW.md for a full JSON example and CLI commands.

Portable generic entries contain only id/category/summary/applicability/
migration/compat/verification/evidence. Evidence rows contain HTTP(S) source,
locator and supports. Independently approved knowledge_publish may create a
knowledge revision; project task IDs, used/not-used conclusions and execution
success do not become generic knowledge. Root LICENSE remains the licensing
reference; historical source header differences are not a research obligation.

Administrator waiting has its own optional bound and is excluded from execution
time. Explicit recover --cancel-interrupted-research cancels incomplete research
without inventing successful evidence or refunding its consumed budget. Ordinary
Effect recovery still requires authenticated complete receipts.

All host-launched agent assignments, including skill generation and independent
skill review, use managed OpenCode HTTP sessions. Full prompts travel in the
request body, not argv or environment variables. The host does not truncate
prompts to accommodate OS argument limits. Persisted `<stage-log>.stdin.txt`
files are redacted copies. The transport does not remove model context-window
limits: model rejection remains a failure and must not be treated as completed
work.

Before an agent stage starts, ModPort resolves the selected model's active
context window from the OpenCode provider catalog. UTF-8 byte estimates are
conservative; reserved runtime space is not a measurement of server/tool
injection.
Planning prompts carry a small exact identity and a hashed execution-local
planning-input-manifest descriptor. Complete input bindings remain in that
manifest and the host-sealed output. Agents may return only used input aliases;
unknown or stale references fail validation. Reference catalogs in historical
packets become manifest JSON pointers, without deleting reasoning or acceptance.

Oversized historical material is archived and divided into ordered records with
Unicode character offsets and SHA-256 values. Every record reaches a bounded
model summary request; there is no whole-prompt head/tail omission or global line
deduplication. The rolling JSON checkpoint records objective, important_details,
completed, active, blocked, next_steps and evidence_refs. A separate recent-record
budget retains exact original text. Current task instructions and protected
contracts remain exact. Summaries are advisory, never authenticated acceptance.

The summary transport uses an isolated managed OpenCode session with all tools
disabled and the selected OpenAI-compatible provider credentials supplied by
the host environment. It has no server output-token cap: a local estimated
byte ceiling, host summary validation and timeout enforce the local limits.
Audit records actual provider usage when supplied, never credentials or private
reasoning. Provider, model and reasoning-variant mismatch fail closed.

Stage-lineage budgets persist across retries: at most 64 summary calls. Elapsed
time is recorded without a cumulative time cap. Calls have no independent total
deadline by default; the remaining host Run deadline still bounds every call.
A separate 60-second stream idle deadline starts after response headers. Configure
an optional total timeout and the idle timeout with
`MODPORT_PROMPT_SUMMARY_TOTAL_TIMEOUT_SECONDS` and
`MODPORT_PROMPT_SUMMARY_IDLE_TIMEOUT_SECONDS`. The total timeout accepts `none`
(default), `unlimited`, or positive finite seconds; idle requires positive finite
seconds. Any configured total timeout is clamped to the remaining Run deadline.
Stream activity resets only the idle deadline, never the total deadline. The
transport aborts when decoded summary output exceeds the host byte budget; this
does not impose a server-side reasoning or output-token cap.
Each rolling request carries the previous validated summary and the next records.
Empty, malformed, unknown-reference or oversized summaries fail without replacing
the previous valid checkpoint. A checkpoint is published only after the complete
reconstructed prompt fits. Full source, historical.txt, record index, transport
usage logs, partition diagnostics and final digests remain in execution artifacts.
If protected content itself exceeds capacity, diagnostics report region sizes
and largest protected line; changing a history summarizer cannot bypass it.


## Verified dependency repository inputs (workflow v10)

A configured shared Maven store is copied into `artifacts/dependency-repository/`
at submission. `initial_refs.dependency_repository` authenticates its manifest;
`initial_refs.dependency_repository_init` authenticates the generated Gradle init
script. Before a Gradle gate, the host verifies both frozen digests and each
manifest entry's path, size and SHA-256. The sandbox receives only a read-only
Run snapshot, never the shared store. Copies do not share writable inodes.

The manifest retains exact Maven coordinates and source URLs for JAR/POM bytes.
An original POM and its digest are required unless the operator explicitly
asserts no transitive dependencies. A caller-supplied digest needs independent
provenance; hashing an untrusted mirror download does not establish authenticity.
Later shared-store additions cannot alter an existing Run's frozen inputs.
Private Gradle caches and outputs are not promoted into the shared repository.
A cache hit establishes byte identity only, not build or behavioral acceptance.

## Bounded application-state evidence

Before a ModPort policy decision is written through the public SDK API, every
top-level application-state field at or above 64 KiB is stored as a deterministic,
content-addressed gzip object. The SDK value contains only strict schema and blob
references. An oversized compact root is externalized the same way. Readers verify
the path, compressed and raw sizes, SHA-256 digest, gzip stream, and declared field
identity before exposing the complete logical state. Missing, corrupt, symlinked,
or over-128-MiB state fails closed.

Legacy inline workflow state remains readable and is converted only by a later
decision under the current deployment. This representation does not remove results,
diagnostics, rework history, or evidence and does not edit SDK tables directly.
Every historical revision may retain blob references, so a Run backup must include
both its SDK databases and `audit-blobs/`; blob retention matches the Run lifetime.


## Workflow 13 handoff contracts

Each regression scope follows test_design -> test_review -> test_execute. The
host retains the pre-design candidate and captured suite and reconstructs review
and execution workspaces from them. Author changes to the product checkout do
not become the execution candidate. Review approval binds the design command,
candidate, suite and scope; the join validates every scoped approval. Rejected
reviews route to repair. Workflow 12 and earlier retain their frozen stage order.

Scopes record all behavior IDs plus separate runtime/static IDs. Only client-only
behaviors whose frozen mappings are all static_client with static_reason and
client_smoke bindings qualify for static coverage. All-static scopes omit runtime
tests, require independent review, and retain characterization, deferred duties
and final client_smoke gates. Static coverage is not runtime proof.

Shared author contracts define coder acceptance entries and complete runtime and
static evidence schemas, examples and semantics. Protected prompt instructions
point to the complete execution-specific template even with older rubrics.
Behavior structure is validated before v13 baseline execution. Only NO-SOURCE
for an exact declared Gradle task fails that gate. Failures retain available raw
agent output, execution logs and artifact references; reopen retains the latest
format feedback alongside the original business failure.

Current gap IDs qualify kind, skill and entry; collisions with historical owners use a gap:v1 encoded tuple. Legacy aliases require matching
ownership. Analysis authors cannot supply host status fields. Knowledge closure
or transfer to verification requires an explicit independently approved research
or admin disposition. Unreviewed gaps cannot disappear on reanalysis. Rejected
v13 review findings require reason, evidence or location, and closure_criteria.
Supervisor assignments are self-contained and do not instruct file reads.

Independent suites may use subproject tasks such as :module:independentTest, but
must redirect reports to root build/. The suite validator rejects module/build/
paths before execution; native goal host checks have separate path support.

## Runtime output ownership

`.modport/evidence/`, `.modport/run-client/`, and `.modport/run-server/` contain
regenerated runtime receipts, logs and isolated game state, not delivered
product inputs. Product snapshots omit these directories on capture and compare.
When carrying a snapshot that included them, ignore only these output entries;
preserve the original snapshot and archived execution evidence. Product source,
build configuration and the delivered artifact remain protected.
