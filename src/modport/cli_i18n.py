"""Small localization support for the human-facing command line interface."""
from __future__ import annotations

import importlib.resources
import json
import locale
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .user_paths import data_root


SUPPORTED_LANGUAGES = ("en", "zh-CN")

# English is the source catalog and the fallback for untranslated content.
ENGLISH = {
    "program.description": "Forge to NeoForge migration on Dispatcher SDK",
    "help.lang": "interface language (en or zh-CN); a manual choice is remembered",
    "help.models": "show or edit model settings independently of workflow versions",
    "help.models.show": "show effective model settings",
    "help.models.set": "save a model and reasoning effort for one role",
    "help.models.config_show": "model JSON file (default: modport-models.json in the current directory)",
    "help.models.config_set": "model JSON file to update (default: modport-models.json in the current directory)",
    "help.web": "serve a password-protected read-only LAN progress page",
    "help.storage_maintain": "plan or apply verified cold retention without opening SDK databases",
    "help.storage_apply": "archive verified eligible artifacts and release working copies",
    "help.handoff_create": "package selected evidence and Git commits without scheduler history",
    "help.handoff_include": "explicit source-relative evidence file; repeat for each file",
    "help.handoff_head": "exclude uncommitted target worktree changes and package only HEAD",
    "help.run_model_config": "model JSON file to freeze for this Run",
    "help.run_handoff": "verified artifact-only handoff package for a fresh Run",
    "help.run_verify_artifact": "run fresh contract and behavior verification against the handed-off target artifact",
    "help.run_inherit_harness": "restore handoff harness sources before fresh verification (requires --handoff)",
    "help.dependency_cache": "verified Maven seed store (default: dependency-cache beside output root)",
    "help.no_dependency_cache": "start without a shared dependency seed",
    "help.validation_scope": "compile_package defers runtime/game behavior tests and keeps acceptance unverified",
    "help.requirements": "additional migration and acceptance scope, frozen with the Run",
    "help.skill_generate": "generate or reuse an independently approved exact-version skill",
    "help.recover_native_goals": "resume interrupted coder goals in their existing native threads",
    "help.status_detail": "read the full Run snapshot; may require a bounded database backup",
    "help.status_task": "read one task's latest SDK attempt without expanding the Run",
    "help.recover_cleanup": "reconcile one settled OpenCode process tree without rerunning its author",
    "help.cleanup_command_id": "exact settled coder execution ID with unconfirmed cleanup",
    "help.reopen": "formally reopen one settled failed stage in the same SDK Run",
    "help.reopen_verification": "reopen a reconciled interrupted verification under the current host deployment",
    "help.research_import": "import administrator evidence, independently review and resume affected tasks",
    "help.audit_memory": "memory limit for the separate report export process",
    "help.audit_compact_prepare": "prepare a verified audit backup and compaction plan without changing the database",
    "help.audit_compact_apply": "verify and apply a prepared audit compaction plan to the same database inode",
    "help.sdk_inspect": "inspect imported SDK and both stores without opening writers",
    "help.dependency_cache_command": "fetch or verify pinned host-managed Maven dependencies",
    "help.cache_fetch": "fetch HTTPS bytes or import a verified local download",
    "help.cache_coordinate": "group:artifact:version",
    "help.cache_url": "source provenance URL",
    "help.cache_sha256": "expected artifact SHA-256",
    "help.cache_file": "import a local download instead of requesting URL",
    "help.cache_pom_url": "original POM URL; requires --pom-sha256",
    "help.cache_no_transitive": "explicitly publish a bare JAR with a generated minimal POM",
    "help.cache_pom_file": "original POM file for --file imports",
    "help.cache_verify": "verify every cached byte and print its manifest",
    "help.continue": "retain settled work and continue in a new SDK segment",
    "help.continue_start_stage": "explicitly reopen the selected target verification tail",
    "help.continue_seconds": "explicit continuation time allowance; v19 defaults to the frozen deadline",
    "help.continue_upgrade": "create an explicitly linked segment with the installed workflow definition",
    "help.continue_model_config": "explicit model settings for the new segment; omit to retain frozen settings",
    "help.continue_assignments": "add assignments to the carried budget while keeping its original deadline",
    "help.continue_progress": "continue for 8h; extend only on observed progress within a 12h cap",
    "help.retry_dependency_cache": "explicit verified dependency store for the new Run",
    "help.argparse_help": "show this help message and exit",
    "help.argparse_usage": "usage:",
    "help.argparse_positionals": "positional arguments:",
    "help.argparse_options": "options:",
    "help.argparse_optional_arguments": "optional arguments:",
    "error.prefix": "modport",
    "error.verify_requires_handoff": "run --verify-artifact requires --handoff",
    "error.verify_no_harness": "run --verify-artifact uses a fresh contract and cannot inherit a harness",
    "error.inherit_requires_handoff": "run --inherit-harness requires --handoff",
    "error.platform_requires_versions": "platform generation requires source and target Minecraft versions",
    "error.pom_file_requires_file": "--pom-file requires --file",
    "error.keyboard_interrupt": "host stopped; inspect the v2 Run before resume/recover",
}

_ARGPARSE_ERRORS = {
    "unrecognized arguments:": "无法识别的参数：",
    "the following arguments are required:": "以下参数为必填项：",
    "invalid choice:": "无效选项：",
    "(choose from ": "（可选值：",
    "expected one argument": "需要提供一个参数",
    "expected at least one argument": "至少需要提供一个参数",
    "expected at most one argument": "最多只允许提供一个参数",
    "expected an argument": "需要提供参数",
    "not allowed with argument": "不能与参数同时使用",
    "one of the arguments ": "以下参数之一 ",
    " is required": "为必填项",
    "ambiguous option:": "无法确定的选项：",
    "conflicting option string:": "选项冲突：",
    "cannot have multiple subparser arguments": "不能重复指定子命令参数",
    "limit must be non-negative or 'none'": "上限必须为非负数或 'none'",
    "invalid int value": "无效整数值",
    "invalid float value": "无效浮点数值",
    "invalid choice": "无效选项",
}


def canonical_cli_language(value: str) -> str:
    """Normalize common language tags while leaving unsupported values intact."""
    tag = value.strip().split(".", 1)[0].split("@", 1)[0].replace("_", "-").lower()
    if tag == "zh" or tag.startswith("zh-"):
        return "zh-CN"
    if tag.startswith("chinese"):
        # Windows locale names and BCP 47 Chinese tags use the same available catalog.
        return "zh-CN"
    if tag == "en" or tag.startswith("en-") or tag.startswith("english"):
        return "en"
    return value


def _supported_language(value: str | None) -> str | None:
    if value is None:
        return None
    canonical = canonical_cli_language(value)
    return canonical if canonical in SUPPORTED_LANGUAGES else None


def system_language() -> str:
    """Select Chinese or English from the process/system locale."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANGUAGE", "LANG"):
        value = os.environ.get(name)
        if value:
            # The first defined locale source has precedence even when unsupported.
            # For example, LC_ALL=C must not inherit Chinese from LANG.
            candidate = value.split(":", 1)[0] if name == "LANGUAGE" else value
            return _supported_language(candidate) or "en"
    try:
        message_locale = locale.getlocale(locale.LC_MESSAGES)[0]
    except (AttributeError, TypeError, ValueError):
        message_locale = None
    try:
        process_locale = locale.getlocale()[0]
    except (TypeError, ValueError):
        process_locale = None
    for candidate in (message_locale, process_locale):
        language = _supported_language(candidate)
        if language:
            return language
    return "en"


def preference_path() -> Path:
    """Return a per-user preference location without creating it."""
    return data_root() / "cli-language.json"


def load_preference(path: Path | None = None) -> str | None:
    try:
        value = json.loads((path or preference_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    return _supported_language(value.get("language")) if isinstance(value.get("language"), str) else None


def save_preference(language: str, path: Path | None = None) -> None:
    """Remember an explicit supported CLI language, outside every Run directory."""
    selected = _supported_language(language)
    if selected is None:
        return
    target = path or preference_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"language": selected}, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    except OSError:
        # A preference is helpful, but must not prevent help or another command.
        return


def language_from_arguments(argv: list[str] | tuple[str, ...] | None) -> str | None:
    """Find the last valid --lang value without consuming or validating argv."""
    if argv is None:
        return None
    found = None
    index = 0
    while index < len(argv):
        item = argv[index]
        value = None
        if item == "--lang" and index + 1 < len(argv) and not argv[index + 1].startswith("-"):
            value = argv[index + 1]
            index += 1
        elif item.startswith("--lang="):
            value = item.partition("=")[2]
        if value:
            found = _supported_language(value)
        index += 1
    return found


def resolve_language(explicit: str | None = None) -> str:
    """Resolve an explicit choice, a saved manual choice, then the system locale."""
    return _supported_language(explicit) or load_preference() or system_language()


def translate(key: str, language: str = "en", **values: Any) -> str:
    english = ENGLISH.get(key, key)
    selected = _supported_language(language) or "en"
    translated = english
    if selected != "en":
        translated = _chinese_catalog().get(key, english)
    try:
        return translated.format(**values) if values else translated
    except (KeyError, ValueError):
        return translated


@lru_cache(maxsize=1)
def _chinese_catalog() -> dict[str, str]:
    try:
        resource = importlib.resources.files("modport").joinpath("locales", "cli_zh-CN.json")
        catalog = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, ModuleNotFoundError, ValueError, TypeError):
        return {}
    return catalog if isinstance(catalog, dict) else {}


def translate_argparse_error(message: str, language: str) -> str:
    if _supported_language(language) != "zh-CN":
        return message
    for source, target in _ARGPARSE_ERRORS.items():
        message = message.replace(source, target)
    if message.endswith(")") and "（可选值：" in message:
        message = message[:-1] + "）"
    return message


def translate_argparse_help(message: str, language: str) -> str:
    if _supported_language(language) != "zh-CN":
        return message
    for key in ("help.argparse_help", "help.argparse_usage", "help.argparse_positionals",
                "help.argparse_options", "help.argparse_optional_arguments"):
        english = ENGLISH[key]
        message = message.replace(english, translate(key, language))
    return message
