"""Read-only source preparation and mod-specific triage agents."""
from dataclasses import dataclass
from pathlib import Path
import json
import re

from .contracts import OperationResult
from .analysis_contract import (ANALYSIS_SCHEMA_VERSION, CANDIDATE_STATUSES, GAP_KINDS,
                                GAP_STATUSES, VERIFICATION_STAGES, HOST_GAP_FIELDS, encoded_gap_id)
from .evidence import atomic_json, file_digest, read_json, verified_path
from .project_gaps import gap_id, license_header_only, project_gap_rows
from .skill_tools.scan import knowledge_entries
from .business_policy import business_gates_disabled


def _verified_scan_path(root, ref, *, require_digest=False):
    path = verified_path(root, ref)
    expected = ref.get("sha256") if isinstance(ref, dict) else None
    if expected is None and not require_digest:
        return path
    if (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)
            or file_digest(path) != expected):
        raise ValueError("mod scan artifact digest mismatch")
    return path


def _scan_observation(scan):
    """Return host-observed completion state and concrete diagnostics."""
    diagnostics = []
    complete = scan.get("scan_complete") is True
    if not complete:
        diagnostics.append("mod scan did not record complete coverage")
    skills = scan.get("skills")
    if not isinstance(skills, dict):
        return False, [*diagnostics, "mod scan skills are unavailable"]
    for skill, value in skills.items():
        report = value.get("report") if isinstance(value, dict) else None
        if not isinstance(report, dict) or report.get("scan_complete") is not True:
            complete = False
            diagnostics.append(f"{skill}: scan incomplete or unavailable")
    return complete, diagnostics


def _finding_evidence(finding):
    path = finding.get("path", "unknown")
    line = finding.get("line", "?")
    column = finding.get("column", "?")
    location = f"{path}:{line}:{column}"
    rule = finding.get("rule_id", "unknown-rule")
    match = finding.get("match")
    detail = f"host scanner candidate from rule {rule}"
    if isinstance(match, str) and match:
        detail += f": {match[:500]}"
    return [location, detail]


def _merge_host_scan_facts(data, scan):
    """Keep every host scanner candidate while allowing agent classification."""
    rows = data.get("candidates")
    if not isinstance(rows, list):
        return data
    seen = {(row.get("skill"), row.get("index")) for row in rows if isinstance(row, dict)}
    preserved = []
    candidate_count = 0
    for skill, value in scan.get("skills", {}).items():
        report = value.get("report", {}) if isinstance(value, dict) else {}
        findings = report.get("findings", []) if isinstance(report, dict) else []
        findings = findings if isinstance(findings, list) else []
        candidate_count += len(findings)
        for index, finding in enumerate(findings):
            if (skill, index) in seen or not isinstance(finding, dict):
                continue
            rows.append({"skill": skill, "index": index, "status": "unknown",
                         "evidence": _finding_evidence(finding), "host_finding": finding})
            preserved.append({"skill": skill, "index": index})
    data["host_scan"] = {"scan_complete": scan.get("scan_complete") is True,
                         "preserved_candidates": preserved,
                         "candidate_count": candidate_count}
    return data


def _files(root, excluded):
    result = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if (relative == ".git" or relative.startswith(".git/")
                or any(relative == name or relative.startswith(name + "/") for name in excluded)):
            continue
        if path.is_symlink():
            raise ValueError("source analysis does not support symlinks")
        if path.is_file():
            result[relative] = file_digest(path)
    return result


def validate_preparation(data, baseline, request):
    if not isinstance(data, dict):
        raise ValueError("preparation: expected JSON object")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("preparation requires schema_version 1")
    evidence_rows = data.get("version_evidence")
    if not isinstance(evidence_rows, list):
        raise ValueError("preparation.version_evidence: expected array of objects")
    for index, evidence in enumerate(evidence_rows):
        if not isinstance(evidence, dict):
            raise ValueError(f"preparation.version_evidence[{index}]: expected object")
    for field in ("source_loader_version", "source_java"):
        value = data.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9][0-9A-Za-z._-]*", value):
            raise ValueError(f"exact {field} missing")
        if request.get(field) is not None and request[field] != value:
            raise ValueError(f"{field} conflicts with requested source")
        supported = False
        for evidence in evidence_rows:
            if evidence.get("field") != field or evidence.get("value") != value:
                continue
            relative = evidence.get("path")
            line = evidence.get("line")
            if (not isinstance(relative, str) or Path(relative).is_absolute()
                    or ".." in Path(relative).parts or relative.startswith(".modport/")
                    or type(line) is not int or line < 1):
                raise ValueError("invalid source version evidence")
            path = baseline / relative
            if path.resolve() != path.absolute() or not path.is_file():
                raise ValueError("version evidence path unavailable")
            lines = path.read_text(encoding="utf-8").splitlines()
            if line <= len(lines) and re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", lines[line - 1]):
                supported = True
        if not supported:
            raise ValueError(f"{field} has no matching fixed-source line")
    return data


def _validate_project_gap(row, baseline):
    gap_id(row)
    for field in ("entry_id", "gap_id", "question", "existing_answer", "missing_information"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise ValueError(f"project gap requires concrete {field}")
    locations = row.get("usage_locations")
    if not isinstance(locations, list) or (row["applicable"] and not locations):
        raise ValueError("applicable project gap requires usage_locations")
    for location in locations:
        if not isinstance(location, dict):
            raise ValueError("invalid usage location")
        relative, line = location.get("path"), location.get("line")
        if (not isinstance(relative, str) or not relative or Path(relative).is_absolute()
                or any(part in ("", ".", "..") for part in relative.split("/")) or "\\" in relative
                or type(line) is not int or line < 1 or baseline is None):
            raise ValueError("invalid usage location path or line")
        path = baseline / relative
        if path.resolve() != path.absolute() or not path.is_file():
            raise ValueError("usage location file unavailable")
        lines = path.read_text(encoding="utf-8").splitlines()
        if line > len(lines):
            raise ValueError("usage location line unavailable")
        symbol = location.get("symbol")
        if symbol is not None and (not isinstance(symbol, str) or not symbol.strip() or symbol not in lines[line - 1]):
            raise ValueError("usage location symbol not present on referenced line")
    if row["status"] == "not_applicable":
        scope = row.get("checked_scope")
        if not isinstance(scope, list) or not scope or any(not isinstance(item, str) or not item.strip() for item in scope):
            raise ValueError("not_applicable requires checked_scope")
        if locations or row.get("used") is True or row.get("usage_status") == "used":
            raise ValueError("not_applicable conflicts with used evidence")
    if row.get("applicability_disputed") and row["status"] != "unresolved":
        raise ValueError("disputed applicability requires independent review")


def analysis_catalog(scan, historical_rows=()):
    """Publish the same namespaced scanner identities used by validation."""
    entries = {}
    for skill, value in scan["skills"].items():
        for index, item in enumerate(value["report"]["known_gaps"]):
            for entry in knowledge_entries({"known_gaps": [item]}):
                entries[(skill, entry["id"])] = index
        for entry in value["report"].get("knowledge_entries", []):
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                entries.setdefault((skill, entry["id"]), None)
    historical = {row.get("gap_id"): (row.get("kind"), row.get("skill"), row.get("entry_id"))
                  for row in historical_rows if isinstance(row, dict) and row.get("gap_id")}
    catalog = []
    for (skill, entry), index in sorted(entries.items()):
        identities = []
        for kind in GAP_KINDS:
            identity = kind + ":" + skill + ":" + entry
            if identity in historical and historical[identity] != (kind, skill, entry):
                identity = encoded_gap_id(kind, skill, entry)
            identities.append(identity)
        catalog.append({"skill": skill, "index": index, "entry_id": entry, "gap_ids": identities})
    return catalog



def validate_analysis(data, scan, baseline=None, *, strict=False):
    if not isinstance(data, dict):
        raise ValueError("analysis: expected JSON object")
    if type(data.get("schema_version")) is not int or data["schema_version"] != ANALYSIS_SCHEMA_VERSION:
        raise ValueError(f"analysis.schema_version: expected integer {ANALYSIS_SCHEMA_VERSION}")
    blockers = []
    for section, source_key in (("candidates", "findings"), ("gap_assessments", "known_gaps")):
        rows = data.get(section)
        if not isinstance(rows, list):
            raise ValueError(f"analysis.{section}: expected array")
        expected = {(kind, index) for kind, value in scan["skills"].items()
                    for index, _ in enumerate(value["report"][source_key])}
        entry_indexes = {}
        if section == "gap_assessments":
            for skill_name, value in scan["skills"].items():
                for source_index, item in enumerate(value["report"][source_key]):
                    entries = knowledge_entries({"known_gaps": [item]}) if strict else [item]
                    for entry in entries:
                        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                            entry_indexes[(skill_name, entry["id"])] = source_index
        if strict and section == "gap_assessments":
            expected = set(entry_indexes)
        required = set(expected)
        optional_entries = {(skill_name, entry["id"]) for skill_name, value in scan["skills"].items()
                            for entry in value["report"].get("knowledge_entries", [])
                            if isinstance(entry, dict) and isinstance(entry.get("id"), str)}
        seen = set()
        seen_gap_ids = set()
        seen_gap_kinds = set()
        for position, row in enumerate(rows):
            location = f"analysis.{section}[{position}]"
            if not isinstance(row, dict):
                raise ValueError(f"{location}: expected object, got {type(row).__name__}")
            skill, index = row.get("skill"), row.get("index")
            if section == "gap_assessments":
                forbidden = HOST_GAP_FIELDS.intersection(row)
                if forbidden:
                    raise ValueError(f"{location}: host-owned fields forbidden: {sorted(forbidden)}")
                entry = row.get("entry_id")
                matches = [key for key in entry_indexes.keys() | optional_entries if key[1] == entry]
                if skill is None and len(matches) == 1:
                    skill = matches[0][0]
                key = (skill, entry)
                if key in entry_indexes:
                    source_index = entry_indexes[key]
                    if index is not None and index != source_index:
                        raise ValueError("entry_id conflicts with legacy scan identity")
                    index = source_index
                elif strict and isinstance(entry, str) and (key in optional_entries or row.get("new_entry") is True):
                    skill = skill or "project"
                    index = index if type(index) is int else 0
                    expected.add((skill, entry))
                if strict and len({name for name, _ in matches}) > 1:
                    if row.get("gap_id") not in (f"{row.get('kind')}:{skill}:{entry}",
                                                  encoded_gap_id(row.get("kind"), skill, entry)):
                        raise ValueError("colliding catalog entry requires gap_id=kind:skill:entry_id")
            if not isinstance(skill, str) or type(index) is not int:
                raise ValueError(f"{location}: expected skill string and index integer; got skill={skill!r}, index={index!r}")
            identity = (skill, row.get("entry_id")) if strict and section == "gap_assessments" else (skill, index)
            location += f" (skill={skill!r}, index={index})"
            strict_gap = strict and section == "gap_assessments"
            if identity not in expected or (identity in seen and not strict_gap):
                raise ValueError(f"{location}: duplicate or unknown scan identity")
            if strict_gap:
                semantic_identity = (skill, row.get("entry_id"), row.get("kind"))
                if semantic_identity in seen_gap_kinds:
                    raise ValueError(f"{location}: duplicate gap kind for catalog entry")
                seen_gap_kinds.add(semantic_identity)
                stable = gap_id(row)
                if stable in seen_gap_ids:
                    raise ValueError(f"{location}: duplicate gap_id")
                seen_gap_ids.add(stable)
            evidence = row.get("evidence")
            if not isinstance(evidence, list) or not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence):
                raise ValueError(f"{location}.evidence: expected nonempty array of nonblank strings")
            statuses = GAP_STATUSES if section == "gap_assessments" else CANDIDATE_STATUSES
            if row.get("status") not in statuses:
                raise ValueError(f"{location}.status: invalid {'gap status' if section == 'gap_assessments' else 'candidate classification'} {row.get('status')!r}; allowed: {', '.join(statuses)}")
            if section == "gap_assessments":
                if type(row.get("applicable")) is not bool:
                    raise ValueError(f"{location}.applicable: expected boolean, got {row.get('applicable')!r}")
                if row["applicable"] and row["status"] == "not_applicable":
                    raise ValueError(f"{location}: applicable gap cannot be dismissed as not applicable")
                if not row["applicable"] and row["status"] != "not_applicable":
                    raise ValueError(f"{location}: non-applicable gap requires not_applicable status and evidence")
                if row.get("kind") not in GAP_KINDS:
                    raise ValueError(f"{location}.kind: expected knowledge or verification")
                allowed_stages = ("mod_analysis",) if row["kind"] == "knowledge" else VERIFICATION_STAGES
                if row.get("resolution_stage") not in allowed_stages:
                    raise ValueError(f"{location}.resolution_stage: expected one of {allowed_stages}")
                for field in ("closure_criteria", "affected_tasks"):
                    values = row.get(field)
                    if (not isinstance(values, list) or (row["applicable"] and not values)
                            or any(not isinstance(value, str) or not value.strip() for value in values)):
                        raise ValueError(f"{location}.{field}: expected string array, nonempty for applicable gaps")
                if strict:
                    _validate_project_gap(row, baseline)
                    row.setdefault("skill", skill)
                    row.setdefault("index", index)
                if row["applicable"] and row["status"] == "unresolved" and not license_header_only(row):
                    if row["kind"] == "knowledge":
                        blockers.append(row)
            seen.add(identity)
        if not required.issubset(seen):
            raise ValueError(f"analysis.{section}: omitted scan identities {sorted(required - seen)!r}")
    return blockers


@dataclass
class AnalysisStageHandler:
    stage: str
    paths: tuple[str, ...]

    def __call__(self, command):
        from .handlers import CodexStageHandler, _result, _unverified_result
        from .prompts import STAGE_PROMPTS
        root = Path(command.run_dir)
        baseline = root / "baseline"
        result = None
        try:
            prompt = STAGE_PROMPTS[self.stage]
            if self.stage == "mod_analysis":
                scan = read_json(_verified_scan_path(root, command.artifact_refs["mod_scan_report"],
                    require_digest=business_gates_disabled(command)))
                prompt += "\nExact catalog identities (choose knowledge or verification gap_id): " + json.dumps(analysis_catalog(scan, [*command.payload.get("project_research_gaps", []),
                    *command.payload.get("gap_obligations", [])]))
            result = CodexStageHandler(prompt, baseline=True, required_paths=self.paths)(command)
            if result.status != "completed":
                return result
            outputs = dict(result.outputs)
            refs = dict(outputs.get("artifact_refs", {}))
            blockers = []
            if self.stage == "preparation":
                data = validate_preparation(read_json(baseline / self.paths[0]), baseline, command.payload["request"])
                name, key = "preparation.json", "source_preparation"
            elif self.stage == "mod_analysis":
                scan = read_json(_verified_scan_path(root, command.artifact_refs["mod_scan_report"],
                    require_digest=business_gates_disabled(command)))
                scan_complete, scan_diagnostics = _scan_observation(scan)
                try:
                    data = read_json(baseline / self.paths[0])
                    if business_gates_disabled(command):
                        data = _merge_host_scan_facts(data, scan)
                    blockers = validate_analysis(data, scan, baseline, strict=command.options.get("workflow_version", 0) >= 7)
                except ValueError as exc:
                    # Preserve authenticated output refs for a budgeted business repair.
                    # Input/evidence/I/O failures outside this block are not format retries.
                    outputs.update(format_repairable=True, validation_error=str(exc))
                    if business_gates_disabled(command):
                        path = baseline / self.paths[0]
                        if path.is_file() and not path.is_symlink():
                            outputs["raw_report"] = path.read_text(encoding="utf-8", errors="replace")
                        return _unverified_result(command, outputs=outputs,
                            diagnostics=[str(exc)],
                            detail="source analysis recorded without schema-gated acceptance")
                    return _result(command, "failed", outputs=outputs, detail=str(exc),
                                   error_code="analysis_output_invalid")
                name, key = "mod-analysis.json", "mod_analysis"
            else:
                return result
            path = root / "artifacts" / name
            atomic_json(path, data)
            refs[key] = {"path": path.relative_to(root).as_posix(), "sha256": file_digest(path)}
            deferred = [row for row in data.get("gap_assessments", [])
                        if row["applicable"] and row["status"] == "unresolved" and row["kind"] == "verification"]
            research, verification = project_gap_rows(data.get("gap_assessments", []))
            outputs.update(project_research_gaps=research, project_verification_gaps=verification,
                           artifact_refs=refs, unresolved_relevant_gaps=blockers,
                           deferred_verification_gaps=deferred, research_repairable=bool(blockers))
            if self.stage == "mod_analysis":
                outputs["scan_complete"] = scan_complete
                if business_gates_disabled(command) and not scan_complete:
                    return _unverified_result(command, outputs=outputs,
                        diagnostics=scan_diagnostics,
                        detail="source analysis recorded; scan coverage remains incomplete")
            if business_gates_disabled(command) and blockers:
                return _unverified_result(command, outputs=outputs,
                    diagnostics=["unresolved relevant knowledge gaps observed"],
                    detail="source analysis recorded; unresolved gaps remain diagnostic")
            return _result(command, "failed" if blockers else "completed", outputs=outputs,
                           detail="knowledge gaps require bounded research, not a finding of infeasibility" if blockers else "source analysis validated; verification obligations remain tracked",
                           error_code="relevant_skill_gap" if blockers else None)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            if business_gates_disabled(command) and result is not None and result.status == "completed":
                outputs = dict(result.outputs)
                path = baseline / self.paths[0]
                if path.is_file() and not path.is_symlink():
                    outputs["raw_report"] = path.read_text(encoding="utf-8", errors="replace")
                return _unverified_result(command, outputs=outputs,
                    diagnostics=[str(exc)], detail=f"{self.stage} output retained without schema-gated acceptance")
            return _result(command, "failed", outputs=dict(result.outputs) if result else {}, detail=str(exc), error_code="analysis_output_invalid")


def build_analysis_registry():
    return {stage: AnalysisStageHandler(stage, paths) for stage, paths in {
        "background": (".modport/background.md",),
        "preparation": (".modport/preparation.json",),
        "project_init": (".modport/project-init.md",),
        "mod_analysis": (".modport/mod-analysis.json", ".modport/mod-analysis.md"),
    }.items()}
