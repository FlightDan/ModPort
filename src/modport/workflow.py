"""Versioned application routing, independent of SDK execution contracts."""
from dataclasses import dataclass
from .contracts import FORMAT_VERSION
from .evidence import digest
from .models import MigrationRequest

WORKFLOW_VERSION = 40
DEFAULT_AGENT_MODEL = "gpt-6-luna"
DEFAULT_REASONING_EFFORT = "max"
PLANNER_MODEL = "gpt-6.1-sol"
PREVIOUS_PLANNER_MODEL = "gpt-6-sol"
PLANNER_REASONING_EFFORT = "high"
PLANNER_STAGES = frozenset({"migration_plan", "contract_repair_plan", "target_repair_plan"})
CODER_REVIVAL_STAGE = "coder_revival_plan"
V25_PLANNER_STAGES = PLANNER_STAGES | {CODER_REVIVAL_STAGE}
V31_PLANNER_STAGES = V25_PLANNER_STAGES | {"contract_review"}
LEGACY_AGENT_MODEL = "gpt-5.6-luna"
LEGACY_REASONING_EFFORT = "high"


def agent_model_policy(version, stage=None, model_policy=None):
    """Keep the model policy of frozen workflows while selecting new defaults."""
    if model_policy is not None:
        from .model_policy import resolve_model
        return resolve_model(model_policy, stage)
    if (version is None or type(version) is int and version >= 33) and stage == "supervisor":
        return PLANNER_MODEL, PLANNER_REASONING_EFFORT
    if (version is None or type(version) is int and version >= 31) and stage in V31_PLANNER_STAGES:
        return PLANNER_MODEL, PLANNER_REASONING_EFFORT
    if (version is None or type(version) is int and version >= 25) and stage in V25_PLANNER_STAGES:
        return PREVIOUS_PLANNER_MODEL, PLANNER_REASONING_EFFORT
    if (version is None or type(version) is int and version >= 23) and stage in PLANNER_STAGES:
        return PREVIOUS_PLANNER_MODEL, PLANNER_REASONING_EFFORT
    return ((DEFAULT_AGENT_MODEL, DEFAULT_REASONING_EFFORT)
            if version is None or type(version) is int and version >= 22
            else (LEGACY_AGENT_MODEL, LEGACY_REASONING_EFFORT))

PLANNING_STAGES = ("migration_inventory", "migration_plan", "migration_tasks", "parallel_review")
REPAIR_PLANNING_STAGES = tuple(
    scope + suffix for scope in ("contract", "target")
    for suffix in ("_diagnose", "_repair_plan", "_repair_tasks", "_repair_review")
)
# Business output repair and no-progress escalation are frozen with each Run.
# These are not SDK transport retries and do not extend a Run's resource budget.
REPAIR_POLICY = {"format_retries_per_stage": 2, "stagnant_failures_before_diagnosis": 2,
                 "stagnant_failures_before_stop": 3,
                 # Draft, improvement, and major omissions are fixed Markdown revisions.
                 "planning_rounds": 3}

MAIN_STAGES = (
    "source", "background", "preparation", "project_init", "environment", "baseline_build",
    "skill_lookup", "skill_publish", "mod_scan", "mod_analysis", "contract_draft", "contract_verify",
    "contract_review", "contract_freeze", *PLANNING_STAGES, "implementation", "development_integrate",
    "target_build", "code_review", "test_design", "test_review", "test_execute", "acceptance_preflight", "acceptance_build",
    "client_smoke", "gap_review", "delivery",
)
RESEARCH_STAGES = ("platform_diff", "java_diff", "platform_skill_review", "java_skill_review", "gap_research")
REWORK_STAGES = ("contract_revise", "target_revise", *REPAIR_PLANNING_STAGES, "development_prepare")
SUPPORT_STAGES = ("gap_plan", "gap_plan_review", "research_review", "admin_review", "knowledge_publish")
SUPERVISOR_STAGE = "supervisor"
STAGE_IDS = MAIN_STAGES + RESEARCH_STAGES + REWORK_STAGES + (
    "coder", "agent_rework", "gate_handoff", "goal_prepare", "development_prepare_integrate", "contract_repair_integrate", "target_repair_integrate", "contract_restore"
) + SUPPORT_STAGES + (SUPERVISOR_STAGE,)
AGENT_STAGES = frozenset({"background", "preparation", "project_init", "mod_analysis",
    "contract_draft", "contract_review", *PLANNING_STAGES, "code_review", "gap_review", "test_design", "test_review", "coder",
    *RESEARCH_STAGES, *REWORK_STAGES, *SUPPORT_STAGES[:-1], "goal_prepare", "agent_rework", "gate_handoff",
    CODER_REVIVAL_STAGE}) - {
        "contract_revise", "target_revise", "development_prepare"}
REVIEW_STAGES = frozenset({"contract_review", "code_review", "test_review", "gap_review", "platform_skill_review", "java_skill_review", "gap_plan_review", "research_review", "admin_review"})
DEPENDENCIES = {stage: (() if i == 0 else (MAIN_STAGES[i - 1],))
                for i, stage in enumerate(MAIN_STAGES)}
# Source/target versions must be known before research. Everything after that
# waits only for the inputs it consumes, not its position in MAIN_STAGES.
DEPENDENCIES.update(
    environment=("preparation",),
    project_init=("environment",),
    baseline_build=("environment",),
    skill_lookup=("environment",),
    mod_scan=("skill_publish",),
    mod_analysis=("mod_scan", "project_init"),
    contract_draft=("baseline_build", "project_init"),
    migration_inventory=("mod_analysis", "contract_freeze"),
)
EARLY_STAGES = MAIN_STAGES[:MAIN_STAGES.index("migration_inventory")]
DEPENDENCIES.update(contract_revise=("baseline_build",), target_revise=("migration_plan",),
                    platform_diff=("skill_lookup",), java_diff=("skill_lookup",),
                    platform_skill_review=("platform_diff",), java_skill_review=("java_diff",),
                    coder=("implementation",), gap_research=("mod_analysis",))
DEPENDENCIES.update({stage: () for stage in SUPPORT_STAGES})
DEPENDENCIES[CODER_REVIVAL_STAGE] = ()
DEPENDENCIES[SUPERVISOR_STAGE] = ()
DEPENDENCIES["development_prepare"] = ("migration_tasks",)
DEPENDENCIES["development_prepare_integrate"] = ("development_prepare",)
DEPENDENCIES["contract_restore"] = ("baseline_build", "project_init")
DEPENDENCIES["goal_prepare"] = ()
DEPENDENCIES["agent_rework"] = ()
DEPENDENCIES["gate_handoff"] = ()
for scope in ("contract", "target"):
    DEPENDENCIES[scope + "_diagnose"] = (("baseline_build",) if scope == "contract" else ("migration_plan",))
    DEPENDENCIES[scope + "_repair_plan"] = (scope + "_diagnose",)
    DEPENDENCIES[scope + "_repair_tasks"] = (scope + "_repair_plan",)
    DEPENDENCIES[scope + "_repair_review"] = (scope + "_repair_tasks",)
    DEPENDENCIES[scope + "_repair_integrate"] = (scope + "_revise",)
NEXT_STAGE = dict(zip(MAIN_STAGES, MAIN_STAGES[1:]))
NEXT_STAGE.update(contract_revise="contract_verify", target_revise="target_build", gap_research="mod_analysis")
NEXT_STAGE.update(contract_repair_integrate="contract_verify", target_repair_integrate="target_build")
NEXT_STAGE["development_prepare"] = "parallel_review"
NEXT_STAGE["development_prepare_integrate"] = "parallel_review"
NEXT_STAGE["contract_restore"] = "contract_verify"
for scope in ("contract", "target"):
    NEXT_STAGE.update({scope + "_diagnose": scope + "_repair_plan",
                       scope + "_repair_plan": scope + "_repair_tasks",
                       scope + "_repair_tasks": scope + "_repair_review",
                       scope + "_repair_review": scope + "_revise"})
REPAIR_ROUTE = {
    "mod_analysis": "mod_analysis",
    "gap_research": "gap_research",
    **{s: "contract_diagnose" for s in ("contract_draft", "contract_verify", "contract_review", "contract_revise")},
    **{s: "target_diagnose" for s in ("target_build", "code_review", "test_design", "test_review", "test_execute",
        "acceptance_preflight", "acceptance_build", "client_smoke", "gap_review", "delivery", "target_revise")},
    **{stage: stage for stage in (*PLANNING_STAGES, *REPAIR_PLANNING_STAGES)},
    "development_prepare": "migration_plan", "implementation": "migration_tasks",
    "development_prepare_integrate": "migration_inventory",
    "development_integrate": "migration_inventory", "coder": "migration_inventory",
    "goal_prepare": "migration_tasks", "contract_repair_integrate": "contract_diagnose",
    "target_repair_integrate": "target_diagnose",
}
FATAL_ERRORS = frozenset({
    "command_artifact_invalid", "acceptance_rubric_invalid", "locked_artifact_invalid",
    "budget_exhausted", "build_sandbox_unavailable",
    "dependency_rate_limited", "dependency_resolution_failed",
    "source_history_invalid", "contract_source_mismatch", "planning_artifact_invalid",
})

# These tuples remain the frozen v18 routing vocabulary. New Runs explicitly
# carry their main/early routes so upgrades cannot reinterpret older inputs.
LEGACY_STAGE_IDS = STAGE_IDS
NEW_STAGES = ("skill_resolve", "build_prepare", "codemod", "early_compile")
STAGE_IDS = LEGACY_STAGE_IDS + NEW_STAGES
DEPENDENCIES.update(skill_resolve=("environment",),
                    build_prepare=("skill_resolve", "project_init"),
                    codemod=("mod_scan", "project_init"), early_compile=("codemod",))
V19_EARLY_STAGES = (
    "source", "background", "preparation", "environment", "project_init",
    "skill_resolve", "build_prepare", "mod_scan", "codemod", "early_compile", "baseline_build",
    "contract_draft", "contract_verify", "contract_review", "contract_freeze",
)
V19_MAIN_STAGES = V19_EARLY_STAGES + (
    "migration_inventory", "migration_plan", "implementation", "development_integrate",
    "target_build", "code_review", "test_design", "test_review", "test_execute",
    "acceptance_preflight", "acceptance_build", "client_smoke", "gap_review", "delivery",
)

# Scan and transform the authenticated source before spending time on narrative
# preparation or bootstrapping a compiler. Keep the v19 tuples frozen above.
V20_EARLY_STAGES = (
    "source", "skill_resolve", "mod_scan", "codemod", "background", "preparation",
    "environment", "build_prepare", "early_compile", "project_init", "baseline_build",
    "contract_draft", "contract_verify", "contract_review", "contract_freeze",
)
V20_MAIN_STAGES = V20_EARLY_STAGES + V19_MAIN_STAGES[len(V19_EARLY_STAGES):]

# Keep the immediate predecessor's serialized routing frozen.  v30 gets an
# additive route below; existing v29 definitions must still serialize exactly
# as they did before the new stages were introduced.
V29_STAGE_IDS = STAGE_IDS
V29_MAIN_STAGES = MAIN_STAGES
V29_EARLY_STAGES = EARLY_STAGES
V29_NEXT_STAGE = dict(NEXT_STAGE)
V29_DEPENDENCIES = {stage: tuple(dependencies)
                    for stage, dependencies in DEPENDENCIES.items()}

CLEANUP_STAGES = ("research_cleanup", "code_cleanup")
V30_EARLY_STAGES = (V20_EARLY_STAGES[0], "research_cleanup", *V20_EARLY_STAGES[1:])
V30_MAIN_STAGES = (
    *V20_MAIN_STAGES[:V20_MAIN_STAGES.index("target_build")],
    "code_cleanup",
    *V20_MAIN_STAGES[V20_MAIN_STAGES.index("target_build"):],
)
V30_STAGE_IDS = (*V29_STAGE_IDS, *CLEANUP_STAGES)
V30_DEPENDENCIES = {
    **V29_DEPENDENCIES,
    "research_cleanup": ("source",),
    "code_cleanup": ("development_integrate",),
    "migration_inventory": ("early_compile", "contract_freeze", "research_cleanup"),
}
V30_NEXT_STAGE = {
    **V29_NEXT_STAGE,
    "development_integrate": "code_cleanup",
    "code_cleanup": "target_build",
    "target_repair_integrate": "code_cleanup",
    "target_revise": "code_cleanup",
}

# Fresh artifact verification reuses source characterization while entering its
# own target-artifact test stages. It has no migration-plan or coder route.
ARTIFACT_VERIFICATION_STAGES = (
    "source", "environment", "baseline_build", "contract_draft", "contract_verify",
    "contract_review", "contract_freeze", "artifact_test_design",
    "artifact_test_execute", "artifact_test_report",
)
ARTIFACT_VERIFICATION_DEPENDENCIES = {
    stage: (() if index == 0 else (ARTIFACT_VERIFICATION_STAGES[index - 1],))
    for index, stage in enumerate(ARTIFACT_VERIFICATION_STAGES)
}
ARTIFACT_VERIFICATION_NEXT_STAGE = dict(zip(
    ARTIFACT_VERIFICATION_STAGES, ARTIFACT_VERIFICATION_STAGES[1:]))
ARTIFACT_VERIFICATION_AGENT_STAGES = frozenset({
    "contract_draft", "contract_review", "artifact_test_design",
})

# Runtime projections use the newest stage vocabulary. Definitions choose
# V29_* or V30_* explicitly below, so this does not reinterpret frozen v29
# route data.
MAIN_STAGES = V30_MAIN_STAGES
EARLY_STAGES = V30_EARLY_STAGES
NEXT_STAGE = V30_NEXT_STAGE
DEPENDENCIES = {**V30_DEPENDENCIES, **{
    stage: ARTIFACT_VERIFICATION_DEPENDENCIES[stage]
    for stage in ("artifact_test_design", "artifact_test_execute", "artifact_test_report")
}}
STAGE_IDS = V30_STAGE_IDS
AGENT_STAGES = AGENT_STAGES | frozenset(CLEANUP_STAGES)
STAGE_IDS = (*STAGE_IDS, "artifact_test_design", "artifact_test_execute", "artifact_test_report")
AGENT_STAGES = AGENT_STAGES | ARTIFACT_VERIFICATION_AGENT_STAGES

# Source behavior is read from code. Executable declarations belong solely to
# the target, and are frozen after its author has produced the real adapters.
BEHAVIOR_STAGES = ('behavior_extract', 'behavior_review', 'behavior_freeze')
SOURCE_HARNESS_STAGES = frozenset({
    'baseline_build', 'contract_draft', 'contract_verify', 'contract_review',
    'contract_freeze', 'contract_revise', 'contract_restore',
    'contract_diagnose', 'contract_repair_plan', 'contract_repair_tasks',
    'contract_repair_review', 'contract_repair_integrate',
})
V34_ARTIFACT_STAGES = ('source', 'environment', *BEHAVIOR_STAGES,
    'artifact_test_design', 'target_contract_freeze',
    'artifact_test_execute', 'artifact_test_report')
STAGE_IDS = (*STAGE_IDS, *BEHAVIOR_STAGES, 'target_contract_freeze', 'final_cleanup')
AGENT_STAGES = AGENT_STAGES | {'behavior_extract', 'behavior_review', 'final_cleanup'}
DEPENDENCIES = {**DEPENDENCIES, 'behavior_extract': ('source',),
    'behavior_review': ('behavior_extract',),
    'behavior_freeze': ('behavior_review',),
    'target_contract_freeze': ('artifact_test_design',)}


def _exact_scan_versions(request):
    # Omitted selectors still need source preparation / target resolution. Do
    # not guess versions just to take the fast path; resolve_skill_inputs also
    # authenticates the exact values and approved package before scanning.
    return all(request.get(key) not in (None, "") for key in (
        "source_minecraft", "source_loader_version", "source_java",
        "target_minecraft", "target_loader_version", "target_java"))


def stage_routes(header):
    """Use frozen routes, with exact legacy fallbacks for older definitions."""
    definition = header.get("definition", header)
    version = definition.get("workflow_version")
    v30_migration = (type(version) is int and version >= 30
                     and definition.get("workflow_type") == "modport.migration")
    main_default = V30_MAIN_STAGES if v30_migration else V29_MAIN_STAGES
    early_default = V30_EARLY_STAGES if v30_migration else V29_EARLY_STAGES
    next_default = V30_NEXT_STAGE if v30_migration else V29_NEXT_STAGE
    dependencies_default = V30_DEPENDENCIES if v30_migration else V29_DEPENDENCIES
    return (tuple(definition.get("main_stages", main_default)),
            tuple(definition.get("early_stages", early_default)),
            definition.get("next_stage", next_default),
            {row["stage_id"]: tuple(row["depends_on"])
             for row in definition.get("stages", [])} or dependencies_default)


def agent_stage(header, stage):
    definition = header.get("definition", header)
    for row in definition.get("stages", []):
        if row["stage_id"] == stage:
            return bool(row["agent"])
    return stage in AGENT_STAGES

@dataclass(frozen=True)
class WorkflowDefinition:
    request: dict
    version: int = WORKFLOW_VERSION

    def to_dict(self):
        if (self.request.get("workflow_mode") == "artifact_verification"
                and self.version < 31):
            raise ValueError("artifact_verification requires workflow version 31 or later")
        model, effort = agent_model_policy(self.version)
        planner_model = PLANNER_MODEL if self.version >= 31 else PREVIOUS_PLANNER_MODEL
        v30_migration = (self.version >= 30
                         and self.request.get("workflow_mode", "migration") == "migration")
        base_dependencies = V30_DEPENDENCIES if v30_migration else V29_DEPENDENCIES
        base_next_stage = V30_NEXT_STAGE if v30_migration else V29_NEXT_STAGE
        result = {
            "format_version": FORMAT_VERSION, "workflow_type": "modport." + self.request.get("workflow_mode", "migration"), "workflow_version": self.version,
            "request": self.request,
            "stages": [{"stage_id": s, "handler_id": f"modport.{s}",
                        "depends_on": list(base_dependencies[s]), "agent": s in AGENT_STAGES} for s in LEGACY_STAGE_IDS],
            "next_stage": base_next_stage,
            "repair_route": {},
            "repair_policy": {**REPAIR_POLICY, "format_retries_per_stage": 0, "planning_rounds": 2},
            "gate_policy": {"mode": "disabled", "automatic_rework": False},
            "budget": self.request["budget"],
            "research_policy": {"new_pair_assignments": 2, "existing_pair_assignments": 1,
                                "transport_attempts": 1, "automatic_supplement_required": False},
            "planning_policy": {"steps": ["draft", "improve", "organize", "dispatch"],
                                "review_replan_allowed": False,
                                "rework_trigger": "explicit_toolcall"},
            "agent_model_policy": {"model": model,
                                   "reasoning_effort": effort,
                                   "applies_to": "all_run_agents_and_prompt_summaries"},
            "agent_dialogue_policy": {"version": 1, "turns": ["plan", "execute"]},
            "coder_validation_policy": {"self_check_required": False, "regression_required": False,
                                        "structural_tasks_require_reviewed_reason": False},
            "regression_policy": {"separate_agents": True, "parallel_scopes": True,
                                   "all_scopes_required_before_acceptance": False,
                                   "independent_test_review_required": False,
                                   "pristine_candidate_execution": True,
                                   "static_client_coverage_separate": True},
        }
        if self.version >= 23:
            result["agent_model_policy"].update(
                applies_to="default_for_run_agents_and_prompt_summaries",
                stage_overrides={stage: {"model": planner_model,
                                         "reasoning_effort": PLANNER_REASONING_EFFORT}
                                 for stage in sorted(PLANNER_STAGES)},
            )
        if self.version >= 25:
            result["agent_model_policy"]["stage_overrides"] = {
                stage: {"model": planner_model,
                        "reasoning_effort": PLANNER_REASONING_EFFORT}
                for stage in sorted(V25_PLANNER_STAGES)
            }
            result["revival_policy"] = {
                "mode": "planner_requests",
                "trigger": "dependency_settled",
                "planner_stage": CODER_REVIVAL_STAGE,
                "per_task_attempt_limit": None,
                "reset_budget": False,
                "host_owns": ["run_identity", "stopped_state", "budgets", "deduplication"],
            }
        if self.version >= 31:
            result["agent_model_policy"]["stage_overrides"] = {
                stage: {"model": PLANNER_MODEL,
                        "reasoning_effort": PLANNER_REASONING_EFFORT}
                for stage in sorted(V31_PLANNER_STAGES)
            }
        if self.version >= 26:
            result['supervision_policy'] = {
                'mode': 'causal_investigation_and_goal_revision',
                'business_assignment_interval': 5,
                'goal_consumption': 'subsequent_matching_dispatch',
                'shared_assignment_budget': True,
                'mutate_running_input': False,
            }
        if self.version >= 27:
            validation_scope = self.request.get("validation_scope", "full")
            source_tests_deferred = (28 <= self.version < 31
                                     and validation_scope == "compile_package")
            result["validation_policy"] = {
                "scope": validation_scope,
                "deferred_checks": ([
                    *(["source_baseline_behavior_tests"] if source_tests_deferred else []),
                    "target_gametests",
                    "independent_behavior_tests",
                    "client_smoke",
                    "full_mod_behavior_equivalence",
                ] if validation_scope == "compile_package" else []),
                "required_checks": (["target_compile", "target_package"]
                                    if source_tests_deferred else
                                    ["source_baseline_behavior_tests", "target_compile",
                                     "target_package"]),
                "acceptance_status": "unverified" if validation_scope == "compile_package" else "normal",
            }
        if self.version >= 29:
            result["contract_validation_policy"] = {
                "source_evidence": "per_assertion_resolved_source_anchor",
                "passing_evidence": "immutable_case_identity_and_result",
                "revalidation": "affected_cases_with_authenticated_unchanged_carry_forward",
                "host_verification_tool": True,
                "failure_categories": ["test_infrastructure", "assertion_invalid", "mod_behavior", "undetermined"],
            }
        if self.version >= 24:
            # The agent transport is part of the frozen execution contract.
            # Provider credentials are supplied separately by OpenCode and
            # are never embedded in a Run definition.
            result["agent_backend_policy"] = {
                "backend": "opencode",
                "version": "1.18.32",
                "transport": "managed_loopback_server",
                "model_provider": "openai",
            }
        if self.version >= 38:
            result["subagent_policy"] = {
                "transport": "opencode_native_task", "agents": ["general", "explore"],
                "model_role": "subagent", "default_model_role": "coder",
                "scope": "parent_assignment", "planning_turns": False,
                "background": False,
            }
        if self.version >= 19 and self.request.get("workflow_mode", "migration") == "migration":
            dependencies = {**(V30_DEPENDENCIES if self.version >= 30 else V29_DEPENDENCIES),
                            "mod_scan": ("build_prepare",),
                            "migration_inventory": ("early_compile", "contract_freeze"),
                            "migration_plan": ("migration_inventory",),
                            "implementation": ("migration_plan",)}
            main, early = V19_MAIN_STAGES, V19_EARLY_STAGES
            if self.version >= 20:
                main, early = V20_MAIN_STAGES, V20_EARLY_STAGES
                dependencies.update(
                    skill_resolve=("source",) if _exact_scan_versions(self.request) else ("environment",),
                    mod_scan=("skill_resolve",), codemod=("mod_scan",),
                    build_prepare=("environment", "codemod"),
                    early_compile=("build_prepare", "codemod"))
                if _exact_scan_versions(self.request):
                    dependencies['background'] = ('codemod',)
            if self.version >= 30:
                main, early = V30_MAIN_STAGES, V30_EARLY_STAGES
                dependencies.update(
                    research_cleanup=("source",),
                    migration_inventory=("early_compile", "contract_freeze", "research_cleanup"),
                    code_cleanup=("development_integrate",),
                )
            if (self.version >= 27
                    and self.request.get("validation_scope", "full") == "compile_package"):
                deferred_stages = {
                    "test_design", "test_review", "test_execute",
                    "acceptance_build", "client_smoke",
                }
                if 28 <= self.version < 31:
                    deferred_stages.add("contract_review")
                    dependencies["contract_freeze"] = ("contract_verify",)
                main = tuple(stage for stage in main if stage not in deferred_stages)
                early = tuple(stage for stage in early if stage not in deferred_stages)
            result.update(main_stages=list(main),
                          early_stages=list(early),
                          next_stage={**(V30_NEXT_STAGE if self.version >= 30 else V29_NEXT_STAGE),
                                      **dict(zip(main, main[1:]))},
                          planning_policy={"steps": ["plan", "dispatch"],
                                           "review_replan_allowed": False,
                                           "rework_trigger": "explicit_toolcall"})
            stage_ids = V30_STAGE_IDS if self.version >= 30 else V29_STAGE_IDS
            result["stages"] = [{"stage_id": s, "handler_id": f"modport.{s}",
                "depends_on": list(dependencies[s]),
                "agent": s in AGENT_STAGES and s != "migration_inventory"} for s in stage_ids]
        if self.version >= 31 and self.request.get("workflow_mode") == "artifact_verification":
            result.update(
                main_stages=list(ARTIFACT_VERIFICATION_STAGES),
                early_stages=[],
                next_stage=ARTIFACT_VERIFICATION_NEXT_STAGE,
                planning_policy={
                    "steps": ["source_contract", "baseline_verification", "contract_review",
                              "contract_freeze", "artifact_test_design", "artifact_test_execute",
                              "artifact_test_report"],
                    "review_replan_allowed": False,
                    "rework_trigger": "explicit_toolcall",
                },
                validation_policy={
                    "scope": "artifact_verification",
                    "required_checks": ["source_baseline_behavior_tests",
                                        "target_artifact_harness_tests"],
                    "deferred_checks": ["target_compile", "target_package",
                                        "full_client_acceptance_coverage",
                                        "full_mod_behavior_equivalence"],
                    "acceptance_status": "unverified",
                },
                stages=[{"stage_id": stage, "handler_id": f"modport.{stage}",
                         "depends_on": list(ARTIFACT_VERIFICATION_DEPENDENCIES[stage]),
                         "agent": stage in ARTIFACT_VERIFICATION_AGENT_STAGES}
                        for stage in ARTIFACT_VERIFICATION_STAGES],
            )
            result.pop("revival_policy", None)
            result.pop("supervision_policy", None)
            if self.version >= 32:
                result['validation_policy'].update(
                    required_behavior_completion=True,
                    bounded_harness_repair=True,
                )
        if self.version >= 25 and self.request.get("workflow_mode") != "artifact_verification":
            result["stages"].append({
                "stage_id": CODER_REVIVAL_STAGE,
                "handler_id": f"modport.{CODER_REVIVAL_STAGE}",
                "depends_on": [],
                "agent": True,
            })
        if self.version >= 33:
            result['progress_supervision_policy'] = {
                'enabled': True,
                'observation_interval_seconds': 600,
                'idle_windows_before_review': 3,
                'assignment_deadline': 'original_run_deadline',
                'rework_round_limit': None,
                'termination_authority': 'supervisor',
                'shared_assignment_budget': True,
            }
            if not any(stage['stage_id'] == SUPERVISOR_STAGE for stage in result['stages']):
                result['stages'].append({'stage_id': SUPERVISOR_STAGE,
                    'handler_id': 'modport.supervisor', 'depends_on': [], 'agent': True})
        if self.version >= 26:
            for stage in result['stages']:
                if stage['stage_id'] == SUPERVISOR_STAGE:
                    stage['agent'] = True
        if self.version >= 34 and self.request.get('workflow_mode', 'migration') != 'skill_generation':
            mode = self.request.get('workflow_mode', 'migration')
            result['validation_policy'].update(
                source_assumption='user_confirmed_functional',
                source_behavior_basis='source_reading',
                source_runtime_tests=False,
                target_execution='batched_by_isolation',
            )
            result['contract_validation_policy'] = {
                'source_evidence': 'source_reading',
                'passing_evidence': 'target_runtime_case_and_assertion_results',
                'host_verification_tool': True,
                'source_harness_required': False,
            }
            if mode == 'artifact_verification':
                main = list(V34_ARTIFACT_STAGES)
                dependencies = {stage: (() if index == 0 else (main[index - 1],))
                                for index, stage in enumerate(main)}
                result.update(main_stages=main, early_stages=[],
                    next_stage=dict(zip(main, main[1:])),
                    stages=[{'stage_id': stage, 'handler_id': f'modport.{stage}',
                             'depends_on': list(dependencies[stage]),
                             'agent': stage in AGENT_STAGES} for stage in main]
                           + [row for row in result['stages'] if row['stage_id'] == SUPERVISOR_STAGE])
                result['planning_policy']['steps'] = main[2:]
                result['validation_policy']['required_checks'] = ['target_artifact_harness_tests']
            else:
                main = [stage for stage in result['main_stages'] if stage not in SOURCE_HARNESS_STAGES]
                insertion = main.index('migration_inventory')
                main[insertion:insertion] = BEHAVIOR_STAGES
                early = [stage for stage in result['early_stages'] if stage not in SOURCE_HARNESS_STAGES]
                early.extend(BEHAVIOR_STAGES)
                full = self.request.get('validation_scope', 'full') != 'compile_package'
                if full:
                    main.insert(main.index('target_build'), 'target_contract_freeze')
                dependencies = {row['stage_id']: tuple(dep for dep in row['depends_on']
                                if dep not in SOURCE_HARNESS_STAGES) for row in result['stages']}
                dependencies.update(behavior_extract=('source',),
                    behavior_review=('behavior_extract',), behavior_freeze=('behavior_review',),
                    migration_inventory=('early_compile', 'behavior_freeze', 'research_cleanup'),
                    target_contract_freeze=('code_cleanup',))
                if full:
                    dependencies['target_build'] = ('target_contract_freeze',)
                stages = [row['stage_id'] for row in result['stages']
                          if row['stage_id'] not in SOURCE_HARNESS_STAGES]
                stages.extend(BEHAVIOR_STAGES)
                if full:
                    stages.append('target_contract_freeze')
                next_stages = {key: value for key, value in result['next_stage'].items()
                               if key not in SOURCE_HARNESS_STAGES and value not in SOURCE_HARNESS_STAGES}
                next_stages.update(zip(main, main[1:]))
                result.update(main_stages=main, early_stages=early, next_stage=next_stages,
                    stages=[{'stage_id': stage, 'handler_id': f'modport.{stage}',
                             'depends_on': list(dependencies.get(stage, ())),
                             'agent': stage in AGENT_STAGES and stage != 'migration_inventory'}
                            for stage in stages])
                result['validation_policy']['required_checks'] = ['target_compile', 'target_package']
            result['validation_policy']['deferred_checks'] = [check for check in
                result['validation_policy'].get('deferred_checks', [])
                if check != 'source_baseline_behavior_tests']
        if (self.version >= 37 and self.request.get('workflow_mode', 'migration') == 'migration'
                and self.request.get('validation_scope', 'full') == 'full'):
            main = result['main_stages']
            main.insert(main.index('delivery'), 'final_cleanup')
            result['next_stage'].update(gap_review='final_cleanup', final_cleanup='target_build')
            result['stages'].append({'stage_id': 'final_cleanup',
                'handler_id': 'modport.final_cleanup', 'depends_on': ['gap_review'], 'agent': True})
            result['final_cleanup_policy'] = {'enabled': True, 'once_per_run': True,
                'entry': 'initial_target_acceptance', 'revalidation': 'all_frozen_target_tests',
                'independent_review': True, 'redesign_tests': False}
        return result

    def sha256(self):
        return digest(self.to_dict())


def compile_migration_workflow(request: MigrationRequest, *, version: int | None = None) -> WorkflowDefinition:
    request.validate()
    if version is not None and (type(version) is not int or version < 1):
        raise ValueError("workflow version must be a positive integer")
    selected_version = WORKFLOW_VERSION if version is None else version
    if selected_version >= 33 and request.budget.max_seconds is None:
        raise ValueError("Progress-supervised Runs require a finite overall max_seconds budget; "
                         "agent assignments share that original deadline")
    return WorkflowDefinition(request.to_dict(), version=selected_version)
