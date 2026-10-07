# ModPort Agent Rules

## No additional hash verification

The user prohibits additional SHA-1, SHA-256, checksum and fingerprint checks unless explicitly requested. Do not add such checks in scripts, harnesses or delegated work. Do not compare source_fingerprint, candidate or rubric metadata in the gameplay harness or abort gameplay/evidence writing because the author's contract omits host-owned fields. Copy required provenance from the supplied source commit and rubric; host-owned artifact bookkeeping does not authorize another gate. Remove existing agent-added identity/checksum gates when repairing a harness, preserving its gameplay assertions and historical evidence.

## Current workflow (v42)

New Runs use the latest workflow in the current workspace. Only the single old
Run explicitly selected and carried by the host is supported for its actual
continuation, using its frozen source workflow version. A version number alone
does not establish continuation eligibility. Other old Runs are retired. Preserve
the original deadline,
cumulative assignment usage, historical evidence and frozen inputs. An upgrade
does not grant more budget. Preserved framework source archives are not
migration instances.

The source behavior path is `behavior_extract → behavior_review →
behavior_freeze`. The user has confirmed that the original mod works. Read source
code and documentation only, and create `behavior_requirements` with stable
requirement IDs, assertions, source anchors and explicit uncertainties. Record
`source_assumption: user_confirmed_functional` and
`verification_basis: source_reading`. This describes inferred behavior; it is
not a source runtime success claim. Do not generate, execute or repair source
test harnesses, even to resolve an ambiguous behavior. Record the ambiguity.

Target contract design independently creates executable target cases and maps
them to the frozen behavior requirement IDs. Target case IDs are independent;
source test IDs, harness code and source runtime evidence do not need to be
ported. `target_contract_freeze` checks retained behavior coverage and freezes
the target contract and mapping. In target behavior verification, only fresh
execution of the target cases can pass them. Skipped, missing or unexecuted
cases remain unverified and never count as passing. A deferred target test scope
also remains `acceptance_status: unverified`.

Use a reusable GameTest server runner for each locked Forge/NeoForge version
when behavior is suitable for server-side execution. Batch client cases in a
session, reset state explicitly before each case, and restart only when a case
needs isolation. Record a shared runner launch failure once; remaining cases
are unexecuted, not passed. Keep target execution in the credential-free
sandbox and keep delivered artifacts immutable during artifact verification.
Do not claim a speed improvement until real target execution is compared.
The runtime target contract keeps `behaviors` and `test_evidence` at the JSON
root after freezing; the host archive wrapper is a separate file. Native
GameTest XML is bound by the host to `build/test-results/<selected-task>/`.
Reuse host launch, report and session support across mods and target versions;
keep game API differences in locked-version adapters and mod assertions in cases.

`research_cleanup` starts after `source` and runs read-only alongside early
research and preparation. It supplies a source-bound navigation index to
planning. `migration_inventory` joins that result with `early_compile` and
`behavior_freeze`. Treat missing or incomplete cleanup output as diagnostic
context; it does not create an approval gate or authorize automatic rework.

For full validation after `development_integrate`, run `code_cleanup`, then `target_contract_freeze`,
then `target_build`. The same order applies after `target_repair_integrate` and
`target_revise`. Cleanup
may simplify the merged candidate within its assignment, but must preserve
behavior, tests, contracts and acceptance checks. A cleanup failure remains a
diagnostic and does not gate verification. Only an explicit
`request_rework` starts author work; reviewer-requested rework of `code_cleanup`
is followed by a fresh `target_build`.

## Final cleanup and coder readability (v37)

For full migrations, successful execution of every frozen target case and a
finished independent review schedule `final_cleanup` once. The host checkpoints
its integration, then rebuilds the candidate, obtains a fresh independent review,
and reruns all frozen target cases before delivery. Cleanup does not redesign
contracts, remove assertions, or substitute old results. Diagnostic review
findings remain diagnostic; missing execution evidence and unresolved cleanup
cannot settle as success. Artifact-only verification never modifies the product.

All coder, repair, coder rework and native goal turns receive the bundled
`code-simplifier` readability instructions. Preserve behavior and public APIs,
follow repository and locked Java/NeoForge conventions, simplify local control
flow, and keep contracts and assertions intact. Host-owned skill files remain
available through the Run artifact tool even after conversation compression.

The deployment uses `dispatcher-sdk==0.7.1`, Kernel schema 5, Orchestrator
schema 4 and protocol 2. Handler budgets come from the public SDK
`HandlerContext.budget`; do not infer execution authority from a lease expiry.

## Historical assignment-count supervision (v30)

Supervisors investigate raw errors, inputs, tool results, source snapshots and
actual goal preparation, dispatch and downstream consumption. Timeout and exit
codes alone are not diagnoses. They may edit host-provided `goals/*.md` directly.
The host retains original and revised evidence and binds revisions to the exact
task and development plan for subsequent matching dispatches. Never overwrite
an active attempt's input or sealed history. Verify actual consumption and fresh
results; publication alone is not proof of repair. Preserve user requirements,
credential-free execution, SDK ownership, and the same Run budget/deadline.
Reports remain free-form; supervision does not introduce business gates. Every
five business agent assignments triggers asynchronous supervision; supervisors
also consume the assignment budget.
This internal scheduling does not change Codex's 30-minute monitoring interval.


## OpenCode tool boundaries

Use `modport_sandbox_read_run_artifact` to read host-owned files under the
current Run's `artifacts/` directory, including task instructions, inputs and
rules. Supply the path from the host context; request bounded slices with
`offset` and `limit` for long files. OpenCode's file tools operate within the
assigned workspace and cannot edit host evidence. Run project commands only
through `modport_sandbox_run_project_command`, whose `/workspace` mount does
not contain host artifact paths.

## Coder dependency revival

When dependency B settles for coder A, the host and SDK persist and dispatch a
`coder_revival_plan` request. Diagnose the cause from raw execution output, task inputs,
dependency results, candidate code and available environment evidence. A timeout,
nonzero exit or exited process is a symptom, not a recovery decision. State
what failed, why the evidence supports that cause, and whether the coder can
make progress after a concrete correction.

The host dispatches a new coder execution only after an explicit planner
decision and after it checks the original Run/task identity, stopped state,
remaining budget/deadline and deduplication record. Those limits are shared
with the original work and are never reset. There is no fixed per-task attempt
cap. The planner actively repairs confirmed small errors in its supplied isolated
copies and passes broader corrections to a resumed coder. `stop` is permitted
only when the authoritative host context confirms exhaustion of the original
Run deadline or assignment budget, or concrete evidence conclusively establishes
an unrecoverable error. Uncertainty, not knowing the fix yet, missing tools in
the current session, conflicts, repeated failures, or incomplete information do
not alone justify stopping. Continue diagnosis and use a concrete corrective or
diagnostic `resume`, or wait for a named real prerequisite. A stop decision must
cite the exhausted host limit or explain the conclusive irrecoverability evidence
and why no permitted repair or investigation can change the outcome.
Use `stop` with extreme caution: when all unfinished tasks are stopped or blocked by
stopped tasks and no coder is active, the host settles the whole Run as failed.
It is not a pause or a request for another repair agent.
Do not request a blind retry, treat failure status as authorization, or infer
resource exhaustion without matching system evidence. This route handles only
dependency-based coder recovery. Reviewer and other upstream business repairs
continue to require an explicit `request_rework` call.

For `dependency_patch_conflict` before agent startup, the recovery planner can
return `resume` with a concrete reconciliation instruction. The existing host
then materializes conflicting prerequisite files in a fresh isolated coder
workspace. The coder reconciles its writable target contract and harness while
preserving every prerequisite's intended changes, frozen behavior requirements,
assertion IDs and expected values, retained target case IDs, and real execution
bindings. Original patches and host evidence remain unchanged. Restrictions on
the planner's diagnostic `.modport` copy do not prohibit this coder handoff.
The planner has no generic `request_rework` tool; its explicit `resume` decision
is the repair request for this route. Both dialogue turns describe this capability.
This planner repair capability retains the original Run limits.

An explicit continuation of a Run stopped on a pre-agent dependency conflict
requests a new planner decision in the successor. It preserves the predecessor's
stop, patches, decisions and cumulative usage; it does not dispatch a coder until
the new planner chooses `resume`. Ordinary ticks and supervisor restarts do not
override `stop`. This route does not reopen groups containing an author terminated
by progress supervision or groups stopped for other errors.

## Diagnostic source corrections (v40)

When investigating a development coder, the recovery planner and progress
supervisor may fix confirmed, small source errors in the host-provided isolated
`source/task-N/` copies. Keep uncertain or broader work with the coder. Explain
the cause and edits in the existing report. Only existing regular UTF-8 source
and configuration files are supported; new/deleted files remain diagnostic.
Never edit active workspaces, source contracts, delivered artifacts, `.modport`
evidence, credentials or frozen inputs. Project execution stays disabled for
these diagnostic assignments; ordinary target verification remains required.

The host captures before/after contents and binds each correction to its task and
development plan. Future coder executions receive applicable corrections after
their export baseline is recorded, so their candidate patch includes the fixes.
The coder sees application receipts and handles only remaining work; conflicting
corrections stay unapplied and are supplied as concrete repair context. Already
applied corrections are not applied twice. Final integration can consume pending
supervisor corrections when their saved contents still match, without changing
active commands or overwriting newer work. Corrections arriving after integration
dispatch can enter the following isolated code cleanup and its exported patch.
Later corrections are explicitly recorded as deferred until an eligible matching
boundary. A published correction is not proof of consumption or verification;
corrections neither block current execution nor trigger a restart. Original
budget, deadline, explicit revival/rework decisions and cancellation rules stay
in force. No additional hash or fingerprint checks are introduced.

### Current candidate integration (v41)

An earlier Git base or artifact digest records provenance; it is not a lock on
the current workspace. Preserve legal candidate edits before merging coder
patches. Use isolated Git merges, retain all original task contributions, and
send actual `integration_merge_required` conflicts to a fresh coder through the
public SDK under the original Run deadline and remaining assignment budget.
Archive the conflicting snapshot, paths, original command and patch references.
The repair coder edits only project files in its isolated copy and cannot execute
project code, builds or tests. Require its actual returned patch, then dispatch
the original integration stage again to apply the resolved complete delta to the
current candidate. New user changes can require another actual merge. Driver
restart retains the persisted repair task without duplicate dispatch or charges.
This route covers development, development-preparation, target-repair and
contract-repair integration. Do not discard user files or advance failed repair,
missing patches or unsuccessful integration into cleanup, contract freeze or build.
Keep concrete errors; a diagnostic report does not complete an integration.

An explicit successor archives the predecessor's integration repair state and
rebinds the remaining integration to fresh SDK execution authority. Reuse a
completed repair patch instead of charging a second coder assignment. Same-Run
driver restart retains its already dispatched task. Snapshot inputs ignored by
Git are merged with their current contents; a later user deletion is an explicit
delete/modify conflict materialized only in the isolated merge workspace.

A nested coder's explicit rework writes into that exact host-bound caller
workspace. Keep the original prerequisite patch in the group; the caller exports
its merged result. Supervisor copies remain diagnostic and cannot impersonate
this destination. Missing files and invalid task/path bindings retain their raw
errors. No content checksum or frozen HEAD equality authorizes a merge.

An explicit upgrade from the host-selected carried Run chooses the
current workflow only for subsequent execution, preserving executed history,
original deadlines and cumulative usage. Carry a disabled or durably suppressed
watchdog forward; upgrading must not undo an explicit watchdog stop.

## Interrupted execution and observation recovery (v42)

Optional watchdog observation handles only SQLite BUSY/LOCKED contention by
retaining raw errors and retrying after a bounded cooldown measured from the
failed call's return. Already installed watches remain installed. Runtime
initialization contention explicitly requires the next public Runtime reopen;
do not claim that observation recovered in the current session. Permanent
database, identity and authentication errors retain their original error paths.
Missing observation is unknown, not progress or proof of a stopped process.

After SDK reap/sync, startup and explicit recovery bind the current attempt,
fence, task, generation and process identity. The registered old driver must be
independently proved dead in a verifiable PID namespace, and durable SDK evidence
must confirm its exact worker tree was cleaned. Worker or supervisor exit alone
does not prove descendant cleanup. Park an uncertain Effect through public SDK
recovery; never replay an author from a crash symptom. A stopped execution with
no Effect is recorded as `worker_interrupted`. Preserve logical artifact Run
provenance separately from current physical SDK Run membership.

A valid supervisor `repair_resume` decision is still required for business repair.
Exact SDK cancellation authority and independent stopped-tree proof are separate
from SDK cleanup status; never rewrite cleanup `unknown` as confirmed. Mechanical
application settlement records failure, unknown external results and unaccepted
partial artifacts. If a crash left only a settlement note, stronger later cleanup
proof may complete that same settlement. User cancellation does not authorize
recovery. All subsequent work retains the original deadline and cumulative budget.

For an interrupted active watchdog supervisor, mechanical cancellation and failed
diagnostic settlement require that same exact stopped proof and active episode
binding. Accept no fabricated supervisor decision. The existing 60-second diagnosis
retry policy owns the next supervisor assignment; this does not restart a business
author or reset limits. Healthy supervisors, another episode or a stopped watchdog
do not authorize this route.

Desktop process liveness is separate from SDK scheduling state. Use the driver's
copied public `runtime.observation_storage` binding for read-only observation;
`WorkAvailabilityReport.kernel_source` is a path, not a storage source ID. A
running SDK task can have an exited worker. Missing, inaccessible, stale or
namespace-uncertain process identities remain unknown. These observations are
not target acceptance evidence. Short observation limits do not impose an
absolute subsecond status response or five-second SDK recovery deadline.


## Current planner handoff

Describe each file or related issue group with its source location, problem,
intended code/API change, affected callers and verification. Associate inventory
issue IDs with tasks and explain unresolved items. Include one JSON code block
with `development_plan.tasks` in the final Markdown plan; each task describes
`id`, `objective`, `owned_paths`, `dependencies`, `issue_ids`, `acceptance` and
optional `validation_checks`. These fields communicate executable work, not an
approval gate. Read the authenticated original planner report and input index
forwarded with the task. Rework uses the current candidate and a newly bound
execution plan; earlier plans remain historical evidence. Coverage diagnostics
do not discard an otherwise usable independent test snapshot. Explicit
`request_rework` remains required for reviewer-directed upstream repairs; coder
dependency revival uses the separate planner decision described above.

These rules describe current v42 agents and the explicitly supported continuation. In
the current route, exact-version scans and mechanical transforms can run
immediately after source acquisition. A provisional MDK source compile is diagnostic only: it does
not certify the original custom build or its dependency classpath. Keep omitted inventory issues
explicitly unresolved, and distinguish cached checks from fresh execution. Host-owned inventory and
early target compilation precede one implementation plan. Treat raw scanner findings and compilation
logs as evidence; do not discard them because an agent report is empty. Do not redo already applied
mechanical edits. Unknown API behavior still requires semantic work and independent verification.
Business diagnostics never authorize automatic rework; use explicit
`request_rework` when needed. Coder dependency revival is the separate planner
decision defined above. Resource pressure preserves an explicitly requested rework as a planned SDK task.
The host dispatches the same task when capacity returns within the original deadline;
do not submit duplicate requests while waiting. Queue expiry/cancellation remains a recorded failure.
The supported execution SDK is the installed `dispatcher-sdk==0.7.1`
distribution or the explicitly selected checkout of that version. Use the
actual imported source; do not assume a machine-specific checkout location.

## Two-turn report dialogue (policy version 1)

When the frozen input contains `agent_dialogue_policy` version 1, each assignment
receives two user messages in the same OpenCode conversation. The first asks only
for a Markdown task plan. Inspect the supplied inputs to plan the work, return
the plan, and finish that turn. The host saves it as `审阅计划.md` for reviews or
`任务计划.md` for other assignments, outside the candidate workspace.

The second message asks you to execute that plan and supplies the output
contract. JSON assignments use a host-provided JSON Schema for the final reply;
Markdown assignments return the actual report. The host saves the reply at the
existing consumer path. Target test and supporting files may be produced during
execution; source test harnesses are never generated, executed or repaired in
the current workflow. A plan is not a review verdict or completed work.

Both turns share one assignment and its original deadline. A missing or
malformed business report remains diagnostic; it does not add a correction
round or reinstate an approval gate. Upstream rework may be requested during
execution using the existing tools. Frozen inputs without this policy retain
their earlier conversation protocol. Host health probes and prompt compression
are not report assignments.

## Diagnostic repair loop

Use the file-backed repair inventory and routing context returned by
`list_rework_targets`. Inspect exact file, line, symbol and evidence locations;
include issue IDs, expected behavior and verification steps in repair instructions.
Routes and ownership are advisory. For defects spanning modules, select existing
authors, sequence provider/interface changes before callers, and identify an
integration author. Broad scope alone is not a reason to abandon actionable work.
For unmatched issues, explicitly choose an available author or explain why none
can address the defect. Never fabricate a contract lock to bypass missing inputs;
repair the authentic producer evidence or its reference.

After each explicit request, read the host repair feedback and refreshed artifacts.
Distinguish the author handoff, build result, behavior check and verified candidate.
A failed host build is an executed failure; missing test inputs may mean behavior
tests never ran. Missing candidate binding is unknown, not success. New commits,
changed files and SDK completion do not prove progress. A compiler error disappearing
from another failed log does not prove repair. Compiler truncation and unknown or
changed execution scope limit log comparisons. Complete, comparable source scans
may independently establish source issue removal even when logs are incomplete.
Incomplete evidence cannot establish that its omitted issue was resolved. Diagnostic progress cannot
grant acceptance or trigger automatic retry. If you make no rework request, give
a concrete reason tied to the defect, author availability, budget, missing input
or repeated failure without a new repair hypothesis.

## Diagnostic business policy

Use available reports and partial results without waiting for approval, report
schemas, ownership declarations, self-checks, test passes, behavior coverage or
closed knowledge gaps. These are diagnostic information, not admission checks.
Continue the planned stages once their executions settle. Do not start format
correction or replan automatically from diagnostic outcomes. For coder
dependencies, use the separate `coder_revival_plan` decision described above;
other upstream work requires an explicit `request_rework` call.

When an agent finds concrete defects in upstream work that require correction,
it should call `list_rework_targets` and `request_rework`, inspect the returned
result and updated artifacts, and continue its assignment. Writing a request in
report prose does not invoke the author. A planning-only first turn must not
start rework. If there are no callable upstream targets, the prompt must say so
and the agent must preserve the unresolved failure without claiming a repair.

For coders, finish the assigned code edits and return a handoff describing
changes and unresolved checks. The host collects project files and creates the
candidate commit after the coder stops; the coder does not need Git write
permission. Complete the coding goal once that handoff is ready, even when
host-owned build or runtime checks have not yet run. Do not wait for the host
to integrate or verify the same still-running coding assignment. A real blocked
result remains blocked, and its useful project changes are retained.

Planning has four assignments: draft, improve once, organize tasks and dispatch.
Plain text and incomplete task settings are usable; preserve the original report.
Contract repair tasks may change project and build files. Preserve failures as
failures and record changes to the baseline rather than claiming it is pristine.
Available partial patches may be integrated with conflicts reported as diagnostics.
Checks and independent reviews still run. Delivery may publish available artifacts
with `acceptance_status: unverified`; execution completion never certifies behavior.

SDK identity, immutable inputs, leases, cancellation, budget, deadline, memory
admission, workspace containment and credential-free project execution remain
operational requirements. Business approval and retry rules are not additional
active gates.

## Downstream tool-call policy

- Every failed check or rejected report is diagnostic input for downstream work.
  It never starts an author retry, plan restart, format-correction assignment or
  native-goal correction turn automatically.
- A downstream agent may use adequate partial output. If it needs another
  agent to revise work, it must call `request_rework(target_agent, instructions)`.
  A report saying rejected, replan or please fix is not a tool request.
- `gate_handoff` consumes diagnostics when a stage cannot continue normally.
  It writes a short prose handoff, may request specific upstream revisions or a
  fresh deterministic check, and does not edit project code. No verdict JSON,
  full catalog disposition list or approval token is required for this handoff.
- Planning runs draft, one improvement, organization and dispatch. Extra plan
  revisions require a downstream tool request. All planning consumers can ask
  their actual upstream authors for a correction through the same tools.
- Research reviews assess only the submitted research scope and kinds. Missing
  dispositions stay unresolved. Future verification belongs to its owning
  stage; it is not a prerequisite for handing off current research or plans.
  The host merges compatible repeated verification requirements and assigns
  stable IDs for different parents, retaining every original contribution.
- Preserve raw failed results, source/contract/manifest identity, sandbox
  execution and budgets. Forwarding a diagnostic does not make its check pass
  or satisfy final delivery. Independent peers are not cancelled for another
  peer's business failure. A completed handoff without a tool request does not
  authorize rework; final delivery remains incomplete without actual evidence.

## Scope and stage contract

- Work only in the explicitly authorized checkout. Do not read or write an
  unrelated checkout, credential store, or external project.
- Agents do not execute project code. Project execution belongs to the
  credential-free sandbox gate, which records the evidence described in
  `EVIDENCE_PROTOCOL.md`.
- Keep stage prompts short and return the requested structured result. The
  host owns stage transitions, budgets, leases, and retries.
- The host locks source and environment, builds the baseline, generates and
  executes characterization tests, obtains independent contract review, then
  freezes the contract. It then runs the fixed five-assignment planning pipeline, with
  shared preparation and a fresh parallel review, isolated coders and host
  integration, target verification, independent code review, parallel scoped
  regression design/execution and a deterministic join, acceptance and client
  gates, and local delivery. Ready stages follow their declared inputs; after
  environment, project initialization, skill lookup and baseline build can
  overlap.
- Read the shared `acceptance-rubric.json` referenced by the run instead of
  copying the rubric into a prompt or another document.
- Host input preparation has one shared 300-second ceiling per command. The
  ceiling excludes model, native-goal and summary execution, but includes
  reference validation, context/archive work, manifest I/O and terminal
  progress auditing. `MODPORT_INPUT_PREPARATION_TIMEOUT_SECONDS` may lower the
  ceiling but may not raise it; a Run deadline can end preparation earlier.

## Invariants

- Preserve the frozen behavior contract and discovery behavior. A migration
  must not silently remove or reinterpret existing behavior.
- Independent review produces a report only. Reviewers do not edit source
  files or repair their own findings.
- Logs, console output, and a successful process exit do not constitute
  acceptance. Acceptance requires the applicable gates and authenticated
  evidence.
- The business budget is counted by the application host. Agents must not implement an
  unbounded self-loop or silently create extra rework rounds. Business
  rework and technical execution attempts remain separate host counters.
- Preserve source commit, manifest integrity, parent evidence provenance, and
  the exact witness protocol when handing work between stages. Artifact refs may
  carry SHA-256 metadata, while mutable agent workspaces remain associated by
  contained paths and execution identity rather than a whole-tree digest.
- Pass planning and repair context by authenticated reference. Keep the current
  failure visible in the active repair prompt, retrieve related history on
  demand, and preserve the content-addressed context archive and its artifact
  references. A summary or model echo never replaces original evidence.
- Acceptance claims require fresh host evidence. Regression checks use actual
  test execution and explicit reports; structural observations do not substitute
  for behavior tests.
- A skill gap is missing knowledge or verification evidence, not proof that a
  migration is impossible. Analysis schema v2 separates concrete prerequisite
  knowledge (bounded gap_research, independent research_review, then fresh analysis) from verification duties
  assigned to target_build, test_execute or client_smoke. Keep deferred duties
  unresolved and implement their closure_criteria in the appropriate stage.
  Do not demand future target or acceptance evidence before implementation.
- First filter gaps against the mod's actual source, resources, configuration
  and dependencies. Unused features/APIs/domains are not_applicable with a
  concise project-specific explanation; ignore them for research and execution.
  Do not invent relevance merely because a generic skill lists a limitation.
- The final independent gap_review must cover every original gap, authenticate
  its evidence and reject any unresolved duty. Approval binds the candidate,
  analysis and rubric. Stage success alone does not prove a gap resolved;
  do not weaken acceptance or relabel missing tests as non-applicable.

## Research, delegation and independent tests

- Background, source preparation, project initialization, platform differences,
  Java differences, each skill review and mod triage are separate assignments.
  Generic bundles preserve all domains; mod-specific findings and gaps remain
  in the mod report.
- Read the same fixed platform skill, Java skill and mod analysis as the planner,
  implementation coder and rework coder. Do not substitute another revision.
- The organizing agent preserves behavior, ownership, real dependency edges,
  acceptance criteria, validation checks and deferred obligations. Independent
  work stays in separate coder tasks. The dispatch agent explains allocation;
  only the host launches and charges coder instances from the validated DAG.
- Models and reasoning effort come from the Run's frozen model configuration,
  independently of the workflow version. Packaged defaults assign pure planner
  stages (`migration_plan`, `target_repair_plan`,
  `coder_revival_plan`) to gpt-6-astra/high; behavior review and code-author
  stages (`coder`, `agent_rework`, `behavior_extract`, `behavior_review`, `artifact_test_design`,
  `test_design`, `code_cleanup`) to gpt-6.1-sol/high; supervisor, other agents and
  prompt summaries to gpt-6-luna/max. Explicit stage overrides take priority over
  role overrides and the default. Follow the host-supplied frozen selection.
  Complexity describes task scope; it does not select another model. Do not
  spawn untracked subagents. For current executions, the managed OpenCode `task`
  tool may delegate bounded helper work to `general` or read-only `explore`.
  Helpers default to the frozen coder model and reasoning effort, with an
  optional `roles.subagent` override. Give helpers English prompts, clear scope
  and non-overlapping file ownership. They share the parent assignment's
  deadline, usage allowance and sandbox; they do not create another host DAG
  task, extend budgets or request upstream rework. The parent owns integration,
  verification and the final report. Planning-only turns and tool-free preflight
  do not expose delegation.
- After coding, preserve integration, target build, independent code review,
  independently designed/reviewed regression, clean acceptance build, client
  verification, gap closure and delivery. Failures require specific task repairs,
  bounded retries or an explicit blocked outcome, never a whole-plan restart.
- Each coder uses its assigned isolated checkout and edits only owned files; the host authenticates and commits the resulting patch.
  Do not modify shared inputs or another task's work. Failed and cancelled work
  cannot be joined; a new plan creates a new batch and preserves old evidence.
- Before handoff, coders review changed owned files and report completed checks
  and unresolved items. The host independently runs applicable checks against
  a fresh candidate snapshot; a self-report or patch does not prove completion.
- The independent test author may add executable assertions only under the
  assigned clone's .modport/independent-tests/. Preserve product files, existing
  tests and the frozen contract. Never manufacture reports or weaken assertions.
- `target_revise` reads the authenticated integration-debugging guide and its
  pinned dependencies with the current diagnosis and complete failure evidence.
  Diagnose before editing. Preserve the original failure context and acceptance
  criteria, propose targeted verification, and leave project execution to the
  host. Keep independent repair tasks separate unless concrete coupling and a
  connected dependency set justify a merge. A malformed or missing contract is
  a diagnosis input, not a reason to fabricate a replacement source identity or
  weaken behavior.
- Repair planners must start from the complete host repair_context reference in
  every round. The current failure remains active in the prompt; retrieve
  relevant original evidence, parent Run context and prior attempts on demand
  from the authenticated history source instead of echoing unrelated packets.
  The draft explains the failure, evidence and concrete strategy. Two fixed checks
  improve it and correct major omissions. Task organization preserves stop conditions
  and regression methods; dispatch carries the final Markdown and frozen task settings.
  The host binds repair_work_package to that final plan and its task synthesis.
  Classify current failures, prerequisites and downstream work explicitly.
  Preserve all source acceptance criteria. Do not merge independent tasks into
  one coder; a merge requires concrete coupling and a connected dependency set.
- goal_prepare reads the final Markdown, task synthesis, dispatch record and frozen plan, and
  returns one bounded goal per coder. The host runs a persistent goal in an isolated managed OpenCode session.
  Write the declared acceptance report without committing the host-reserved
  .modport/goal-reports file. A turn ending or a self-report is not acceptance.
  Host checks and commit ownership must pass; rejected completion returns feedback
  to the same goal. Keep final independent reviews and sandbox acceptance gates.
  Runtime criteria require targeted runtime checks, not mere file existence.
  Do not execute project code yourself; declare supported host checks instead.
- A regression check must use fully qualified direct Gradle Test
  paths and explicit JUnit XML files under `build/`. The host disables stale
  reports and build/task caches and requires observed nonzero test cases with
  zero failures, errors and skips; aggregate tasks and exit status alone are
  insufficient. The host listener records fresh nonce-bound task/report
  observations, while the frozen candidate and independent review remain the
  semantic trust boundary.
- After `code_review`, independent test-design agents own disjoint behavior
  scopes in separate clones and caches. Each scope's host `test_execute` must
  pass before the deterministic `test_execute.g<generation>.join` operation
  can aggregate evidence. The join must cover every frozen behavior exactly
  once and verify branch identity, workspace, contract/rubric and fresh result.
- baseline_gradle_tasks contains only task names, including optional project
  prefixes. Do not insert flags or init-script paths; the host adds those.
  Coders must read that package, preserve its selected strategies and report
  actual changes and pending checks. Contradictions require explicit feedback,
  not a silent replacement of the approved scope. Summaries never replace input
  evidence. Host structural checks do not prove semantic correctness.
- One repair chain counts as one business cycle. For executable envelopes and
  routing controls, the host permits at most two correction attempts per stage,
  still charging agent assignments. Free-form reports do not require JSON repair.
  The second repeated same failure without progress escalates diagnosis; the
  third stops the chain. Workspace or artifact metadata churn alone is not progress.
  No agent owns a
  retry loop, budget increase or automatic child Run. Cancellation does not
  authorize another repair.
- Public commands, outputs and observed token usage are audited. Never place
  credentials in prompts, evidence or logs; do not output private reasoning.

## Client harness and diagnostic boundaries

- Read the authenticated harness_support artifacts. launch.py and preflight.py
  run only through the credential-free host sandbox. Preserve an existing display;
  cleanup may stop only processes created by this launch. Missing audio capability
  is distinct from resource/event behavior and from real sound correctness.
- Integrate ClientDiagnostics into the generated harness and supply the current
  MODPORT_EXECUTION_ID. Report lifecycle transitions and bounded waits, including
  the last screen/overlay, world/player state, completed test and raw failure.
  The host retains its hard deadline when the client thread cannot respond.
- Minecraft1201Startup is a version-specific fragment for recognized onboarding
  and title states. Do not auto-close unknown screens or rewrite options to bypass
  them. Other locked versions need a typed adapter. Wait for a real world/player,
  run behavioral assertions, and observe resource reload completion.
- Flush genuine evidence after every completed test using the current nonce.
  Lifecycle messages and environment reports are diagnostics, never acceptance
  evidence. Failed executions may retain authenticated partial results without
  passing the gate; never reuse them as fresh results in another execution. A
  retry may carry an existing target harness as an input after path and
  regular-file checks, but source test harnesses and their results never satisfy
  current target coverage. Fresh target verification and review remain mandatory.
- client_smoke requires a frozen executable client startup mapping and fresh
  authenticated runtime witnesses. Log health strings, audio initialization and
  static_client observations do not replace this gate. Do not declare success
  from a synthetic lifecycle transcript, parser fixture or Java stub test.

## Child Run inheritance

Only the single host-selected carried Run is supported for its actual continuation
into the current workflow, retaining its frozen source workflow version. Preserve
its original deadline and cumulative assignment usage; upgrading does not extend
its budget. Public distributions contain no private Run selection. All other old
Runs are retired. Any child Run requires the applicable user
authorization and a new immutable input set. Source harnesses and source runtime
results remain historical; target cases are independently designed and freshly
verified. Do not modify frozen parent evidence or resume a cancelled parent.

## Project gaps and research handoff

- Copy the exact supplied catalog `gap_id` in analysis rows; do not reconstruct
  or shorten it. Current catalogs qualify IDs with skill and entry to prevent
  collisions. Record
  question, existing_answer, missing_information and concrete usage_locations
  (baseline-relative path, one-based line, optional symbol present on that line).
  Newly discovered project entries require `new_entry=true`.
- Non-applicability requires checked_scope and actual absence evidence; it must
  not conflict with usage locations. Mark disputed applicability unresolved and
  submit it for independent review before dismissal. Analysis status, host
  project_status and verification status are distinct fields.
- Platform and Java research budgets are separate: new knowledge gets two
  assignments including its initial diff; reused knowledge gets one supplement.
  Research review and analysis do not authorize an automatic research loop.
  Exhaustion hands control to gap_plan and independent gap_plan_review.
- Alternatives may use an existing answer, compatibility layer, alternative API
  or implementation change. Preserve frozen behavior and add explicit execution
  verification duties. Approved bypassed means a project-local workaround, not
  resolution of the generic unknown or a passed test. Do not repeat an alternative
  without new authenticated evidence. No viable alternative means wait_admin.
- Update only affected tasks and their downstream consumers. Compare explicit
  inputs, interfaces and behavior requirements; preserve completed results when
  those remain unchanged. Unrelated harness and coder work remains eligible.
- research-gaps.json, verification-gaps.json and research-budget.json under
  artifacts are host projections, never edit-to-resume control inputs.
  Administrators submit evidence through research-import --submission; admin_review
  independently reviews the version-bound submission. Never certify execution
  from an administrator's statement. Only independently approved portable entries
  may reach knowledge_publish; project status and results stay project-local.
- Root LICENSE governs the project. Historical source header discrepancies alone
  are not research gaps and must not cause new research or block migration.
- The host can separately bound administrator waiting via admin_wait_seconds;
  waiting is excluded from execution time. Explicit recovery cancellation of
  interrupted research preserves consumed budgets and does not authenticate
  partial outputs as successful research.


## Regression handoff contracts

Each regression scope follows test_design -> test_review -> test_execute. The
host retains the pre-design candidate and captured suite and reconstructs review
and execution workspaces from them. Author changes to the product checkout do
not become the execution candidate. Review approval binds the design command,
candidate, suite and scope; the join validates each scoped review. Rejected
reviews route to repair.

Scopes record all behavior IDs plus separate runtime/static IDs. Only client-only
behaviors whose frozen mappings are all static_client with static_reason and
client_smoke bindings qualify for static coverage. All-static scopes omit runtime
tests, require independent review, and retain characterization, deferred duties
and final client_smoke gates. Static coverage is not runtime proof.

Shared author contracts define coder acceptance entries and runtime and static
evidence schemas. Protected prompt instructions point to the complete
execution-specific template. Validate behavior structure before baseline
execution. Treat Gradle `NO-SOURCE` as a failure only when it applies to an
exact task declared for the check. Failures retain available raw agent output,
execution logs and artifact references.

Analysis authors cannot supply host status fields. Knowledge closure
or transfer to verification requires an explicit independently approved research
or admin disposition. Unreviewed gaps cannot disappear on reanalysis. Rejected
review findings require reason, evidence or location, and closure criteria.
Current progress supervisors investigate original evidence and may publish isolated
development-source corrections as described above. Historical goal-edit supervision
does not authorize writes to an active coder workspace.

Independent suites may use subproject tasks such as :module:independentTest, but
must redirect reports to root build/. The suite validator rejects module/build/
paths before execution; native goal host checks have separate path support.

## Independent watchdog recovery

New Runs and explicit workflow upgrades freeze a watchdog policy. The SDK reports
ten minutes without observed model or tool-session response; three ten-minute
windows without useful work also request diagnosis. Heartbeats are not responses,
and unavailable observations remain unknown. A separate persistent desktop
watchdog consumes durable SDK notifications and survives driver death.

Watchdog supervisors investigate the raw cause and return continue,
repair_resume, wait, or stop. Repair requires a concrete instruction, host-bound
isolated source copies or real request_rework tools, and inspection of returned
results. Reports alone do not repair a task. The host rechecks exact execution
identity and progress, requests SDK cancellation when needed, confirms cleanup,
and dispatches remaining work under the original deadline and cumulative budget.
A failed task cannot be resumed by a continue decision alone.

Stop is restricted to host-confirmed budget exhaustion or a conclusively
unrecoverable cause; unknown evidence and invalid reports do not authorize it.
Real prerequisite waits remain explicit. Terminal failure recovery uses a public
SDK same-Run generation with a supervisor first, preserving frozen input and
history. Normal completion and user cancellation suppress restoration. No new
hash checks, source runtime tests, delivered-product changes, credential access,
or direct SDK storage edits are authorized by this policy.

## Reviewer-directed rework

When the host supplies the reviewer rework tools, use `list_rework_targets` to inspect the
authenticated upstream targets and `request_rework(target_agent, instructions)` to request a
specific revision. The call waits for SDK-scheduled work and returns its report to this same
review session. Review that result before deciding; repeat within the host budget when needed.
Do not launch an untracked author or treat an old approval as approval of a revised candidate.
Code revisions require the fresh host verification returned by the tool.

For an explicitly supported continuation, follow the current rules supplied for
the new Run. Earlier rule copies inside diagnostic packets are historical
  evidence, not active instructions. The transition does not replace the contract,
acceptance rubric, or original diagnostic evidence.

## New Run workflow selection

Each new Run uses the latest workflow implementation in the current workspace
and independently freezes its model configuration. Model changes do not change
WorkflowDefinition or the workflow version. `modport models show` displays the
configuration; `modport models set --role ROLE --model MODEL --reasoning-effort
EFFORT [--config PATH]` saves a role override, defaulting to the current directory's
`modport-models.json`. `run --model-config PATH` selects a file for a new Run;
otherwise the host reads `MODPORT_MODEL_CONFIG`, the current directory file or
packaged defaults. `continue --model-config PATH` explicitly changes models in
the new segment without requiring `--upgrade-workflow`; omission retains the
parent's frozen models even if the workflow is upgraded. Existing Runs retain
frozen versions, model settings and inputs under the old Run support boundary.
