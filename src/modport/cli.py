"""CLI for fresh ModPort v2 Runs and explicit recovery."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4
from .models import Budget, MigrationRequest
from .model_policy import (
    DEFAULT_MODEL_CONFIG_PATH, load_model_config, save_model_config, update_model_config,
)
from .operations import MigrationOperations
from .workflow import compile_migration_workflow
from .user_paths import archives_root, configured_path, runs_root, skill_store
from . import cli_i18n


def optional_limit(value):
    if value.lower() == "none":
        return None
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("limit must be non-negative or 'none'")
    return number


class LocalizedArgumentParser(argparse.ArgumentParser):
    """Argument parser that localizes its own display without global gettext state."""

    def __init__(self, *args, language="en", manual_language=None, **kwargs):
        self.language = language
        self.manual_language = manual_language
        super().__init__(*args, **kwargs)

    def format_help(self):
        return cli_i18n.translate_argparse_help(super().format_help(), self.language)

    def format_usage(self):
        return cli_i18n.translate_argparse_help(super().format_usage(), self.language)

    def error(self, message):
        self.print_usage(sys.stderr)
        message = cli_i18n.translate_argparse_error(message, self.language)
        label = "错误：" if self.language == "zh-CN" else "error:"
        self.exit(2, f"{self.prog}: {label} {message}\n")

    def exit(self, status=0, message=None):
        if status == 0 and self.manual_language:
            cli_i18n.save_preference(self.manual_language)
        super().exit(status, message)


def _subparser(collection, name, *, language, manual_language, **kwargs):
    result = collection.add_parser(name, language=language, manual_language=manual_language, **kwargs)
    result.add_argument("--lang", type=cli_i18n.canonical_cli_language,
                        choices=cli_i18n.SUPPORTED_LANGUAGES, default=argparse.SUPPRESS,
                        help=cli_i18n.translate("help.lang", language))
    return result


def parser(language=None, manual_language=None):
    language = cli_i18n.resolve_language(language)
    output_default = str(runs_root())
    skill_default = str(skill_store())
    path_argument = lambda value: str(configured_path(value))
    result = LocalizedArgumentParser(
        prog="modport", description=cli_i18n.translate("program.description", language),
        language=language, manual_language=manual_language)
    result.add_argument("--lang", type=cli_i18n.canonical_cli_language,
                        choices=cli_i18n.SUPPORTED_LANGUAGES, default=argparse.SUPPRESS,
                        help=cli_i18n.translate("help.lang", language))
    sub = result.add_subparsers(dest="command", required=True)
    wiki = _subparser(sub, "wiki", language=language, manual_language=manual_language,
                     help="迁移研究库与本地贡献草稿" if language == "zh-CN" else "Migration research and local contribution drafts")
    wiki_sub = wiki.add_subparsers(dest="wiki_command", required=True)
    update = _subparser(wiki_sub, "update", language=language, manual_language=manual_language)
    update.add_argument("--revision")
    importing = _subparser(wiki_sub, "import-pack", language=language, manual_language=manual_language)
    importing.add_argument("--file", required=True)
    packing = _subparser(wiki_sub, "build-pack", language=language, manual_language=manual_language)
    packing.add_argument("--library", required=True)
    packing.add_argument("--output", required=True)
    packing.add_argument("--revision", required=True)
    packing.add_argument("--index-output")
    _subparser(wiki_sub, "drafts", language=language, manual_language=manual_language)
    exporting = _subparser(wiki_sub, "export", language=language, manual_language=manual_language)
    exporting.add_argument("--run-dir", required=True)
    file_export = _subparser(wiki_sub, "export-draft", language=language, manual_language=manual_language)
    file_export.add_argument("--id", required=True)
    file_export.add_argument("--output", required=True)
    models = _subparser(sub, "models", language=language, manual_language=manual_language,
                        help=cli_i18n.translate("help.models", language))
    model_sub = models.add_subparsers(dest="models_command", required=True)
    model_show = _subparser(model_sub, "show", language=language, manual_language=manual_language,
                            help=cli_i18n.translate("help.models.show", language))
    model_show.add_argument("--config", help=cli_i18n.translate("help.models.config_show", language))
    model_set = _subparser(model_sub, "set", language=language, manual_language=manual_language,
                           help=cli_i18n.translate("help.models.set", language))
    model_set.add_argument("--role", required=True,
                           choices=("default", "planner", "coder", "supervisor", "contract_review", "summary", "subagent"))
    model_set.add_argument("--model", required=True)
    model_set.add_argument("--reasoning-effort", required=True)
    model_set.add_argument("--config", help=cli_i18n.translate("help.models.config_set", language))
    web = _subparser(sub, "web", language=language, manual_language=manual_language,
                     help=cli_i18n.translate("help.web", language))
    web.add_argument("--runs-root", type=path_argument, default=output_default)
    web.add_argument("--host", default="0.0.0.0")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--password-file", required=True)
    storage = _subparser(sub, "storage-maintain", language=language, manual_language=manual_language,
                         help=cli_i18n.translate("help.storage_maintain", language))
    storage.add_argument("--run-dir", required=True)
    storage.add_argument("--archive-root", type=path_argument, default=str(archives_root()))
    storage.add_argument("--apply", action="store_true", help=cli_i18n.translate("help.storage_apply", language))
    handoff = _subparser(sub, "handoff-create", language=language, manual_language=manual_language,
                         help=cli_i18n.translate("help.handoff_create", language))
    handoff.add_argument("--source-run-dir", required=True)
    handoff.add_argument("--output-dir", required=True)
    handoff.add_argument("--include", action="append", default=[],
                         help=cli_i18n.translate("help.handoff_include", language))
    handoff.add_argument("--committed-head-only", action="store_true",
                         help=cli_i18n.translate("help.handoff_head", language))
    for name in ("run", "compile"):
        item = _subparser(sub, name, language=language, manual_language=manual_language)
        if name == "run":
            item.add_argument("--model-config", help=cli_i18n.translate("help.run_model_config", language))
            item.add_argument("--handoff", help=cli_i18n.translate("help.run_handoff", language))
            item.add_argument("--verify-artifact", action="store_true",
                              help=cli_i18n.translate("help.run_verify_artifact", language))
            item.add_argument("--inherit-harness", action="store_true",
                              help=cli_i18n.translate("help.run_inherit_harness", language))
        item.add_argument("--mod-id", required=True)
        item.add_argument("--source-repository", required=True)
        item.add_argument("--source-revision", required=True)
        item.add_argument("--source-minecraft", required=True)
        item.add_argument("--target-minecraft", required=True)
        item.add_argument("--source-loader", default="forge")
        item.add_argument("--target-loader", default="neoforge")
        item.add_argument("--output-root", type=path_argument, default=output_default)
        item.add_argument("--dependency-cache", type=path_argument, default=os.environ.get("MODPORT_DEPENDENCY_CACHE"),
                          help=cli_i18n.translate("help.dependency_cache", language))
        item.add_argument("--no-dependency-cache", action="store_true",
                          help=cli_i18n.translate("help.no_dependency_cache", language))
        for field in ("source-loader-version", "target-loader-version", "source-java", "target-java",
                      "mdk-revision", "platform-skill-revision", "java-skill-revision"):
            item.add_argument("--" + field)
        item.add_argument("--skill-store", type=path_argument, default=skill_default)
        item.add_argument("--no-wiki", action="store_true")
        item.add_argument("--wiki-revision")
        item.add_argument("--max-parallel-coders", type=int, default=3)
        item.add_argument("--validation-scope", choices=("full", "compile_package"), default="full",
                          help=cli_i18n.translate("help.validation_scope", language))
        item.add_argument("--requirements", help=cli_i18n.translate("help.requirements", language))
        item.add_argument("--admin-wait-seconds", type=optional_limit, default=None)
        item.add_argument("--max-seconds", type=optional_limit, default=43200)
        item.add_argument("--max-agent-assignments", type=optional_limit, default=40)
        item.add_argument("--max-rework-rounds", type=int, default=10)
        item.add_argument("--execution-max-attempts", type=int, default=3)
    skill = _subparser(sub, "skill-generate", language=language, manual_language=manual_language,
                       help=cli_i18n.translate("help.skill_generate", language))
    skill.add_argument("--kind", choices=("platform", "java"), required=True)
    for field in ("source-minecraft", "target-minecraft", "source-loader-version", "target-loader-version",
                  "source-java", "target-java", "platform-skill-revision", "java-skill-revision"):
        skill.add_argument("--" + field)
    skill.add_argument("--skill-store", type=path_argument, default=skill_default)
    skill.add_argument("--no-wiki", action="store_true")
    skill.add_argument("--wiki-revision")
    skill.add_argument("--source-loader", default="forge")
    skill.add_argument("--target-loader", default="neoforge")
    skill.add_argument("--output-root", type=path_argument, default=output_default)
    skill.add_argument("--max-seconds", type=optional_limit, default=43200)
    skill.add_argument("--max-agent-assignments", type=optional_limit, default=4)
    skill.add_argument("--execution-max-attempts", type=int, default=3)
    for name in ("status", "resume", "drive", "cancel", "recover"):
        item = _subparser(sub, name, language=language, manual_language=manual_language)
        item.add_argument("--run-dir", required=True)
        item.add_argument("--run-id", required=True)
        if name == "recover":
            recovery = item.add_mutually_exclusive_group()
            recovery.add_argument("--cancel-interrupted-research", action="store_true")
            recovery.add_argument("--resume-native-goals", action="store_true",
                                  help=cli_i18n.translate("help.recover_native_goals", language))
        elif name == "status":
            detail = item.add_mutually_exclusive_group()
            detail.add_argument("--detail", action="store_true",
                                help=cli_i18n.translate("help.status_detail", language))
            detail.add_argument("--task-id",
                                help=cli_i18n.translate("help.status_task", language))
    cleanup = _subparser(sub, "recover-cleanup", language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.recover_cleanup", language))
    cleanup.add_argument("--run-dir", required=True)
    cleanup.add_argument("--run-id", required=True)
    cleanup.add_argument("--command-id", required=True,
                         help=cli_i18n.translate("help.cleanup_command_id", language))
    reopen = _subparser(sub, "reopen", language=language, manual_language=manual_language,
                        help=cli_i18n.translate("help.reopen", language))
    reopen.add_argument("--run-dir", required=True)
    reopen.add_argument("--run-id", required=True)
    reopen.add_argument("--reason", required=True)
    reopen.add_argument("--stage", choices=("contract_diagnose",), default="contract_diagnose")
    reopen.add_argument("--additional-seconds", type=optional_limit, default=86400)
    reopen.add_argument("--max-agent-assignments", type=optional_limit, default=80)
    verify_reopen = _subparser(sub, 'reopen-verification', language=language,
        manual_language=manual_language, help=cli_i18n.translate("help.reopen_verification", language))
    verify_reopen.add_argument('--run-dir', required=True)
    verify_reopen.add_argument('--run-id', required=True)
    verify_reopen.add_argument('--reason', required=True)
    research_import = _subparser(sub, "research-import", language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.research_import", language))
    research_import.add_argument("--run-dir", required=True)
    research_import.add_argument("--run-id", required=True)
    research_import.add_argument("--submission", required=True)
    audit = _subparser(sub, "audit", language=language, manual_language=manual_language)
    audit.add_argument("--run-dir", required=True)
    audit.add_argument("--output-dir")
    audit.add_argument("--memory-mib", type=int, default=768,
                       help=cli_i18n.translate("help.audit_memory", language))
    audit_prepare = _subparser(sub,
        "audit-compact-prepare",
        language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.audit_compact_prepare", language))
    audit_prepare.add_argument("--run-dir", required=True)
    audit_prepare.add_argument("--output-dir")
    audit_apply = _subparser(sub,
        "audit-compact-apply",
        language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.audit_compact_apply", language))
    audit_apply.add_argument("--run-dir", required=True)
    audit_apply.add_argument("--manifest", required=True)
    sdk_inspect = _subparser(sub, "sdk-inspect", language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.sdk_inspect", language))
    sdk_inspect.add_argument("--run-dir", required=True)
    cache = _subparser(sub, "dependency-cache", language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.dependency_cache_command", language))
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    fetch = _subparser(cache_sub, "fetch", language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.cache_fetch", language))
    fetch.add_argument("--store", required=True)
    fetch.add_argument("--coordinate", required=True, help=cli_i18n.translate("help.cache_coordinate", language))
    fetch.add_argument("--url", required=True, help=cli_i18n.translate("help.cache_url", language))
    fetch.add_argument("--sha256", required=True, help=cli_i18n.translate("help.cache_sha256", language))
    fetch.add_argument("--file", help=cli_i18n.translate("help.cache_file", language))
    pom = fetch.add_mutually_exclusive_group(required=True)
    pom.add_argument("--pom-url", help=cli_i18n.translate("help.cache_pom_url", language))
    pom.add_argument("--no-transitive-dependencies", action="store_true",
                     help=cli_i18n.translate("help.cache_no_transitive", language))
    fetch.add_argument("--pom-sha256")
    fetch.add_argument("--pom-file", help=cli_i18n.translate("help.cache_pom_file", language))
    cache_verify = _subparser(cache_sub, "verify", language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.cache_verify", language))
    cache_verify.add_argument("--store", required=True)
    retry = _subparser(sub, "retry", language=language, manual_language=manual_language)
    retry.add_argument("--parent-run-dir", required=True)
    retry.add_argument("--parent-run-id", required=True)
    retry.add_argument("--run-dir", required=True)
    retry.add_argument("--run-id")
    for name in ("max-seconds", "max-agent-assignments"):
        retry.add_argument("--" + name, type=optional_limit, default=argparse.SUPPRESS)
    for name in ("max-rework-rounds", "execution-max-attempts"):
        retry.add_argument("--" + name, type=int, default=argparse.SUPPRESS)
    retry.add_argument("--budget-reason")
    retry.add_argument("--inherit-harness", action="store_true")
    retry.add_argument("--dependency-cache", help=cli_i18n.translate("help.retry_dependency_cache", language))
    continuation = _subparser(sub, "continue", language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.continue", language))
    continuation.add_argument("--run-dir", required=True)
    continuation.add_argument("--run-id", required=True)
    continuation.add_argument("--next-run-id", required=True)
    continuation.add_argument("--reason", required=True)
    continuation.add_argument("--start-stage", choices=("target_contract_freeze", "artifact_test_design"),
                              help=cli_i18n.translate("help.continue_start_stage", language))
    continuation.add_argument("--additional-seconds", type=optional_limit, default=argparse.SUPPRESS,
                              help=cli_i18n.translate("help.continue_seconds", language))
    continuation.add_argument("--upgrade-workflow", action="store_true",
                              help=cli_i18n.translate("help.continue_upgrade", language))
    continuation.add_argument("--model-config", help=cli_i18n.translate("help.continue_model_config", language))
    continuation.add_argument("--additional-agent-assignments", type=int, default=0,
                              help=cli_i18n.translate("help.continue_assignments", language))
    progress = _subparser(sub, "continue-progress", language=language, manual_language=manual_language,
        help=cli_i18n.translate("help.continue_progress", language))
    for name in ("run-dir", "run-id", "next-run-id", "reason"):
        progress.add_argument("--" + name, required=True)
    progress.add_argument("--initial-seconds", type=int, default=28800)
    progress.add_argument("--maximum-seconds", type=int, default=43200)
    return result


def _request(args):
    return MigrationRequest(mod_id=args.mod_id, source_repository=args.source_repository,
        source_revision=args.source_revision, source_minecraft=args.source_minecraft,
        target_minecraft=args.target_minecraft, source_loader=args.source_loader, target_loader=args.target_loader,
        output_root=args.output_root,
        wiki_enabled=not args.no_wiki, wiki_revision=args.wiki_revision,
        dependency_cache=None if args.no_dependency_cache else str(Path(
            args.dependency_cache or (Path(args.output_root).absolute().parent / "dependency-cache")).absolute()),
        **{field: getattr(args, field) for field in ("source_loader_version", "target_loader_version",
            "source_java", "target_java", "mdk_revision", "skill_store", "platform_skill_revision",
            "java_skill_revision", "max_parallel_coders", "admin_wait_seconds", "validation_scope", "requirements")}, budget=Budget(max_seconds=args.max_seconds,
            max_agent_assignments=args.max_agent_assignments, max_rework_rounds=args.max_rework_rounds,
            execution_max_attempts=args.execution_max_attempts),
        workflow_mode=("artifact_verification" if getattr(args, "verify_artifact", False)
                       else "migration"))


def _task_summary(snapshot):
    """Report latest SDK execution and business outcomes without conflating them."""
    def mapping(value):
        return value if isinstance(value, dict) else {}

    def text(value, limit=1000):
        return value[:limit] if isinstance(value, str) else None

    tasks = mapping(mapping(snapshot).get("tasks"))
    rows = []
    execution_counts = {}
    business_counts = {}
    for task_id, task in tasks.items():
        attempts = mapping(task).get("attempts") or []
        last = mapping(attempts[-1]) if isinstance(attempts, list) and attempts else {}
        payload = mapping(mapping(last.get("command")).get("payload"))
        value = mapping(mapping(last.get("result")).get("value"))
        execution_state = text(last.get("state")) or "unknown"
        business_status = text(value.get("status")) or "unknown"
        execution_counts[execution_state] = execution_counts.get(execution_state, 0) + 1
        business_counts[business_status] = business_counts.get(business_status, 0) + 1
        if len(rows) >= 100:
            continue
        rows.append({
            "task_id": text(task_id),
            "stage_id": text(payload.get("stage_id")) or text(value.get("stage_id")),
            "execution_state": execution_state,
            "business_status": business_status,
            "error_code": text(value.get("error_code")),
            "detail": text(value.get("detail")),
            "review_verdict": text(mapping(value.get("outputs")).get("verdict")),
        })
    return {"tasks": rows, "omitted_tasks": max(0, len(tasks) - len(rows)),
            "execution_state_counts": execution_counts, "business_status_counts": business_counts}


def main(argv=None):
    command_line = list(sys.argv[1:] if argv is None else argv)
    manual_language = cli_i18n.language_from_arguments(command_line)
    language = cli_i18n.resolve_language(manual_language)
    args = parser(language, manual_language).parse_args(command_line)
    if manual_language:
        cli_i18n.save_preference(manual_language)
    operations = MigrationOperations()
    try:
        if args.command == "wiki":
            from .wiki_knowledge import refresh_cache, build_pack, import_pack, export_run_findings
            from .wiki_contributions import ContributionStore
            from .user_paths import data_root
            if args.wiki_command == "update":
                outcome = refresh_cache(revision=args.revision)
            elif args.wiki_command == "build-pack":
                outcome = build_pack(args.library, args.output, revision=args.revision, index_output=args.index_output)
            elif args.wiki_command == "import-pack":
                outcome = import_pack(args.file)
            elif args.wiki_command == "export":
                outcome = export_run_findings(Path(args.run_dir))
            elif args.wiki_command == "export-draft":
                outcome = ContributionStore(data_root()).export(args.id, args.output)
            else:
                outcome = {'drafts': ContributionStore(data_root()).list_drafts()}
            print(json.dumps(outcome, ensure_ascii=False, indent=2))
            return 0
        if args.command == "models":
            if args.models_command == "set":
                path = Path(args.config or DEFAULT_MODEL_CONFIG_PATH).expanduser()
                config = load_model_config(path if path.is_file() else Path(__file__).parent / "rules" / "models.json")
                config = update_model_config(config, args.role, args.model, args.reasoning_effort)
                save_model_config(path, config)
            else:
                config = load_model_config(args.config)
            print(json.dumps(config, ensure_ascii=False, indent=2))
            return 0
        if args.command == "web":
            from .web import serve
            return serve(args.runs_root, args.host, args.port, args.password_file)
        if args.command == "dependency-cache":
            from .dependency_cache import fetch_artifact, publish_artifact, verify_store
            if args.cache_command == "verify":
                result = verify_store(Path(args.store))
            else:
                kwargs = {"pom_url": args.pom_url, "pom_sha256": args.pom_sha256,
                          "no_transitive_dependencies": args.no_transitive_dependencies}
                if args.file:
                    result = publish_artifact(Path(args.store), args.coordinate, Path(args.file),
                        args.sha256, args.url,
                        pom_source=Path(args.pom_file) if args.pom_file else None, **kwargs)
                else:
                    if args.pom_file:
                        raise ValueError(cli_i18n.translate("error.pom_file_requires_file", language))
                    result = fetch_artifact(Path(args.store), args.coordinate, args.url, args.sha256, **kwargs)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "sdk-inspect":
            from .sdk_compat import inspect_runtime
            print(json.dumps(inspect_runtime(args.run_dir), ensure_ascii=False, indent=2))
            return 0
        if args.command == "recover-cleanup":
            from dispatcher_sdk.orchestrator import Orchestrator
            from .opencode_recovery import reconcile_opencode_cleanup
            from .sdk_compat import require_compatible_storage
            from .snapshot_storage import snapshot_databases
            root = Path(args.run_dir).resolve()
            operations._header(root, args.run_id)
            require_compatible_storage(root)
            with snapshot_databases((root / "orchestrator.sqlite3",),
                                    prefix="modport-cleanup-read-") as snapshot_root:
                sdk = Orchestrator(snapshot_root / "orchestrator.sqlite3", None,
                                   clock=operations.clock)
                try:
                    outcome = reconcile_opencode_cleanup(
                        root, sdk=sdk, run_id=args.run_id,
                        command_id=args.command_id)
                finally:
                    sdk.close()
            print(json.dumps(outcome, ensure_ascii=False, sort_keys=True, indent=2))
            return 0 if outcome["cleanup_confirmed"] else 2
        if args.command == "audit":
            from .audit_export import export_isolated
            print(json.dumps({k: str(v) for k, v in export_isolated(
                args.run_dir, args.output_dir, memory_mib=args.memory_mib).items()}, indent=2))
            return 0
        if args.command == "audit-compact-prepare":
            from .audit_maintenance import prepare_compaction
            manifest_path = prepare_compaction(args.run_dir, args.output_dir)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            root = Path(args.run_dir).absolute()
            print(json.dumps({
                "status": "prepared",
                "manifest_path": str(manifest_path),
                "backup_path": str(root / manifest["backup"]["path"]),
                "updates_path": str(root / manifest["updates"]["path"]),
                "source": manifest["source"],
                "result": manifest["result"],
                "stats": manifest["stats"],
            }, ensure_ascii=False, indent=2))
            return 0
        if args.command == "audit-compact-apply":
            from .audit_maintenance import apply_compaction
            print(json.dumps(
                apply_compaction(args.run_dir, args.manifest),
                ensure_ascii=False, indent=2,
            ))
            return 0
        if args.command == "compile":
            print(json.dumps(compile_migration_workflow(_request(args)).to_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "handoff-create":
            from .artifact_handoff import prepare_handoff
            manifest = prepare_handoff(args.source_run_dir, args.output_dir, args.include,
                                       committed_head_only=args.committed_head_only)
            print(json.dumps(manifest, ensure_ascii=False, indent=2))
            return 0
        if args.command == "skill-generate":
            request = MigrationRequest(mod_id=f"{args.kind}-skill", source_repository="skill://standalone",
                source_minecraft=args.source_minecraft or "0", target_minecraft=args.target_minecraft or "0",
                source_loader=args.source_loader, target_loader=args.target_loader,
                source_loader_version=args.source_loader_version, target_loader_version=args.target_loader_version,
                source_java=args.source_java, target_java=args.target_java, skill_store=args.skill_store,
                platform_skill_revision=args.platform_skill_revision, java_skill_revision=args.java_skill_revision,
                workflow_mode="skill_generation", skill_kind=args.kind, output_root=args.output_root,
                wiki_enabled=not args.no_wiki, wiki_revision=args.wiki_revision,
                budget=Budget(max_seconds=args.max_seconds, max_agent_assignments=args.max_agent_assignments,
                              max_rework_rounds=0, execution_max_attempts=args.execution_max_attempts))
            if args.kind == "platform" and not (args.source_minecraft and args.target_minecraft):
                raise ValueError(cli_i18n.translate("error.platform_requires_versions", language))
            from .skill_runtime import resolve_skill_inputs
            from .contracts import OperationInput
            resolve_skill_inputs(OperationInput("validation", "validation", "skill_lookup", "validation",
                                 str(Path(args.output_root).resolve()), payload={"request": request.to_dict()}))
            root = Path(args.output_root).resolve() / f"{args.kind}-skill-{uuid4().hex[:12]}"
            run = operations.run(request, run_dir=root)
        elif args.command == "storage-maintain":
            from .storage_lifecycle import retention_checkpoint
            report = retention_checkpoint(args.run_dir, archive_root=args.archive_root, apply=args.apply)
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
            return 1 if report.get("errors") else 0
        elif args.command == "run":
            if args.verify_artifact and not args.handoff:
                raise ValueError(cli_i18n.translate("error.verify_requires_handoff", language))
            if args.verify_artifact and args.inherit_harness:
                raise ValueError(cli_i18n.translate("error.verify_no_harness", language))
            if args.inherit_harness and not args.handoff:
                raise ValueError(cli_i18n.translate("error.inherit_requires_handoff", language))
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            root = Path(args.output_root).resolve() / f"{args.mod_id}-{stamp}-{uuid4().hex[:8]}"
            model_options = {"model_policy": load_model_config(args.model_config)} if args.model_config else {}
            if args.handoff:
                run = operations.execute(operations.submit(
                    _request(args), run_dir=root, artifact_handoff=args.handoff,
                    inherit_harness=args.inherit_harness, **model_options))
            else:
                run = operations.run(_request(args), run_dir=root, **model_options)
        elif args.command == "retry":
            parent = operations.load_retry_parent(args.parent_run_dir, args.parent_run_id)
            from .retry_policy import BUDGET_FIELDS
            overrides = {name: getattr(args, name) for name in BUDGET_FIELDS if hasattr(args, name)}
            run = operations.retry(parent, run_dir=args.run_dir, run_id=args.run_id,
                                   budget_overrides=overrides, budget_reason=args.budget_reason,
                                   inherit_harness=args.inherit_harness, dependency_cache=args.dependency_cache)
        elif args.command == "continue-progress":
            from .progress_runner import continue_with_progress
            run = continue_with_progress(operations, args.run_dir, args.run_id,
                next_run_id=args.next_run_id, reason=args.reason,
                initial_seconds=args.initial_seconds, maximum_seconds=args.maximum_seconds)
        elif args.command == "continue":
            from .continuation import continue_from_planner
            model_options = {"model_policy": load_model_config(args.model_config)} if args.model_config else {}
            run = operations.execute(continue_from_planner(operations, args.run_dir, args.run_id,
                next_run_id=args.next_run_id, reason=args.reason,
                **({'start_stage': args.start_stage} if args.start_stage else {}),
                **({'additional_seconds': args.additional_seconds} if hasattr(args, 'additional_seconds') else {}),
                additional_agent_assignments=args.additional_agent_assignments,
                upgrade_workflow=args.upgrade_workflow, **model_options))
        elif args.command == "research-import":
            run = operations.execute(operations.import_research(args.run_dir, args.run_id, args.submission))
        elif args.command == "recover":
            run = operations.recover(args.run_dir, args.run_id, cancel_interrupted_research=args.cancel_interrupted_research,
                                     resume_native_goals=args.resume_native_goals)
        elif args.command == "reopen":
            run = operations.reopen(args.run_dir, args.run_id, stage=args.stage, reason=args.reason,
                                     additional_seconds=args.additional_seconds,
                                     max_agent_assignments=args.max_agent_assignments)
            run = operations.execute(run)
        elif args.command in {"resume", "drive"}:
            run = operations.resume(args.run_dir, args.run_id)
        elif args.command == 'reopen-verification':
            from .verification_recovery import reopen_verification
            run = operations.execute(reopen_verification(
                operations, args.run_dir, args.run_id, reason=args.reason))
        elif args.command == "status":
            if args.detail:
                run = operations.status(args.run_dir, args.run_id, detail=True)
            elif args.task_id is not None:
                run = operations.status(args.run_dir, args.run_id, task_id=args.task_id)
            else:
                run = operations.status(args.run_dir, args.run_id)
        else:
            run = getattr(operations, args.command)(args.run_dir, args.run_id)
        from .runner import read_driver_health
        try:
            import time
            from .run_monitor import _runner_health, driver_namespace_match, process_alive
            driver_snapshot = read_driver_health(run.run_dir)
            namespace_match = driver_namespace_match(driver_snapshot)
            alive = bool(driver_snapshot and namespace_match is not False and process_alive(
                driver_snapshot.get('pid'), driver_snapshot.get('birth')))
            driver_health = _runner_health(driver_alive=alive, run_state=run.status,
                                          now=time.time(), snapshot=driver_snapshot,
                                          namespace_match=namespace_match)
            driver_health['snapshot'] = driver_snapshot
        except (OSError, ValueError, RuntimeError) as error:
            driver_health = {'status': 'unknown', 'detail': str(error)}
        if args.command == "status" and not args.detail:
            summary = run.snapshot.get("status_summary", {})
            overall = summary.get("overall_status", run.status)
            if (run.status == "running"
                    and driver_health["status"] in {"interrupted", "unresponsive"}):
                overall = "execution_interrupted"
            report = {
                "run_id": run.run_id, "run_dir": str(run.run_dir),
                "status": run.status, "overall_status": overall,
                "runner_health": driver_health,
                "acceptance_status": "unknown",
                "execution": {
                    "state": run.status,
                    "revision": summary.get("revision"),
                    "generation": summary.get("generation"),
                    "task_count": summary.get("task_count"),
                    "source": summary.get("source"),
                    "observed_at": summary.get("observed_at"),
                    "stale": summary.get("stale", True),
                },
                "availability": summary.get("availability"),
                "current_wait": summary.get("current_wait"),
                "last_code_change": summary.get("last_code_change"),
                "last_successful_authenticated_verification": summary.get(
                    "last_successful_authenticated_verification"),
                "active_tasks": summary.get("active_tasks", []),
                "requested_task": summary.get("requested_task"),
                "recovery": summary.get("recovery"),
            }
        else:
            report = {"run_id": run.run_id, "run_dir": str(run.run_dir), "status": run.status,
                      "overall_status": ('execution_interrupted' if run.status == 'running'
                          and driver_health['status'] in {'interrupted', 'unresponsive'} else run.status),
                      "execution": {'state': run.status},
                      "runner_health": driver_health,
                      "acceptance_status": (run.snapshot.get('application_state') or {}).get(
                          'acceptance_status', 'unverified'),
                      "snapshot": run.snapshot, "task_summary": _task_summary(run.snapshot)}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        if args.command == 'drive' and run.status != 'succeeded':
            # A settled failure or an open recovery wait needs reconciliation,
            # not a service restart loop or another author assignment.
            return 78
        return 0 if run.status == "succeeded" or args.command == "status" else (2 if run.status in {"running", "waiting"} else 1)
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        prefix = cli_i18n.translate("error.prefix", language)
        print(f"{prefix}: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        prefix = cli_i18n.translate("error.prefix", language)
        message = cli_i18n.translate("error.keyboard_interrupt", language)
        print(f"{prefix}: {message}", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
