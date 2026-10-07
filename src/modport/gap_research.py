"""One bounded, read-only research assignment; the host owns the retry loop."""
import json
from pathlib import Path

from .agent_reports import review_decision, report_findings

from .evidence import read_json, verified_path
from .analysis_contract import GENERIC_ENTRIES_SCHEMA
from .project_gaps import gap_id, license_header_only, merge_reviewed_requirements
from .business_policy import business_gates_disabled


RESEARCH_DIRECTORY = ".modport/gap-research"
RESEARCH_REPORT = RESEARCH_DIRECTORY + "/report.md"


def _generic_entries(entries, kinds):
    from .knowledge_library import project_entries
    if entries == []:  # Older reports used an empty array for no supplement.
        return {}
    if not isinstance(entries, dict) or any(kind not in ("platform", "java") or kind not in kinds for kind in entries):
        raise ValueError("generic knowledge entries require authorized platform/java mappings")
    return {kind: project_entries(rows) if rows else [] for kind, rows in entries.items()}


def _research_identity(row):
    if not isinstance(row, dict):
        raise ValueError("invalid research gap identity")
    if row.get("entry_id") is not None:
        return gap_id(row)
    if isinstance(row.get("gap_id"), str) and row["gap_id"].strip():
        return row["gap_id"]
    if isinstance(row.get("skill"), str) and type(row.get("index")) is int:
        return row["skill"], row["index"]
    raise ValueError("invalid research gap identity")


def _knowledge_gap_outputs(payload):
    """Return usable analysis outputs; persisted null/legacy shapes mean no context."""
    context = payload.get("knowledge_gap_context")
    if not isinstance(context, dict):
        return {}
    result = context.get("result")
    if not isinstance(result, dict):
        return {}
    outputs = result.get("outputs")
    return outputs if isinstance(outputs, dict) else {}


def _eligible_research_gaps(rows, kinds, *, allow_legacy=False):
    """Apply the producer's current research scope to a gap collection."""
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("project research gaps must be an array of objects")
    return [row for row in rows
            if ((row.get("kind") in kinds or row.get("skill") in kinds)
                or allow_legacy and row.get("kind") is None and row.get("skill") is None)
            and row.get("project_status", row.get(
                "status", "unresolved" if allow_legacy else None)) == "unresolved"
            and row.get("applicable", True if allow_legacy else None) is True
            and not license_header_only(row)]


def validate_research(data, blockers, workspace):
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("gap research requires schema_version 1")
    expected = {_research_identity(row) for row in blockers}
    rows = data.get("gap_findings")
    if not expected or not isinstance(rows, list):
        raise ValueError("gap research requires the current knowledge blockers")
    seen = set()
    for row in rows:
        identity = _research_identity(row)
        if identity not in expected or identity in seen:
            raise ValueError("duplicate or unknown research gap")
        seen.add(identity)
        if row.get("status") not in ("evidence_added", "unresolved"):
            raise ValueError("research status must be evidence_added or unresolved")
        evidence = row.get("evidence")
        if (not isinstance(evidence, list) or not evidence
                or any(not isinstance(item, str) or not item.strip() for item in evidence)):
            raise ValueError("research requires concrete evidence or a missing-evidence explanation")
    if seen != expected:
        raise ValueError("research omitted knowledge gaps")
    sources = data.get("sources")
    if not isinstance(sources, list):
        raise ValueError("research sources must be an array")
    paths = []
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("invalid research source")
        relative = source.get("path")
        if (not isinstance(relative, str) or not relative.startswith(RESEARCH_DIRECTORY + "/sources/")
                or any(part in ("", ".", "..") for part in relative.split("/")) or "\\" in relative):
            raise ValueError("research snapshots must be contained in gap-research/sources")
        path = workspace / relative
        if path.resolve() != path.absolute() or not path.is_file() or relative in paths:
            raise ValueError("missing, unsafe or duplicate research snapshot")
        if not isinstance(source.get("origin"), str) or not source["origin"].strip():
            raise ValueError("research source origin is required")
        paths.append(relative)
    return paths


class GapResearchHandler:
    def __call__(self, command):
        from .handlers import CodexStageHandler, _result, _snapshot_stage_output, _unverified_result
        from .prompts import STAGE_PROMPTS
        root = Path(command.run_dir)
        workspace = root / "baseline"
        result = None
        business_diagnostics = []
        try:
            try:
                context = _knowledge_gap_outputs(command.payload)
                blockers = command.payload.get("project_research_gaps", context.get("unresolved_relevant_gaps", []))
                kinds = command.payload.get("research_kinds", ["knowledge"])
                if not isinstance(kinds, list) or not kinds or any(kind not in ("knowledge", "verification", "platform", "java") for kind in kinds):
                    raise ValueError("invalid research_kinds")
                blockers = _eligible_research_gaps(blockers, kinds)
                if not blockers:
                    raise ValueError("research requires current unresolved knowledge gaps")
            except (ValueError, TypeError, KeyError) as exc:
                if not business_gates_disabled(command):
                    raise
                business_diagnostics.append(str(exc))
                raw_kinds = command.payload.get("research_kinds", ["knowledge"])
                kinds = [kind for kind in raw_kinds
                         if kind in ("knowledge", "verification", "platform", "java")] if isinstance(raw_kinds, list) else ["knowledge"]
                blockers = []
            result = CodexStageHandler(STAGE_PROMPTS["gap_research"], baseline=True,
                                      required_paths=(RESEARCH_REPORT,))(command)
            if result.status != "completed":
                return result
            # Research is read by another agent, not deserialized as an API call.
            # Preserve every source file without requiring a model-written catalog.
            paths = []
            sources = workspace / RESEARCH_DIRECTORY / "sources"
            if sources.is_dir():
                for path in sorted(sources.rglob("*")):
                    if path.is_file():
                        relative = path.relative_to(workspace).as_posix()
                        verified_path(workspace, {"path": relative})
                        paths.append(relative)
            outputs = dict(result.outputs)
            refs = dict(outputs.get("artifact_refs", {}))
            for relative in (RESEARCH_REPORT, *paths):
                candidate = workspace / relative
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                key, ref = _snapshot_stage_output(root, workspace, command, relative)
                refs[key] = ref
                if relative == RESEARCH_REPORT:
                    refs["gap_research"] = ref
            outputs.update(artifact_refs=refs, research_kinds=list(kinds))
            if business_diagnostics:
                return _unverified_result(command, outputs=outputs,
                    diagnostics=business_diagnostics,
                    detail="research evidence retained without prerequisite gating")
            return _result(command, "completed", outputs=outputs,
                           detail="research evidence recorded; analysis must reassess, not assume closure")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if business_gates_disabled(command) and result is not None and result.status == "completed":
                outputs = dict(result.outputs)
                report = workspace / RESEARCH_REPORT
                if report.is_file() and not report.is_symlink():
                    outputs["raw_report"] = report.read_text(encoding="utf-8", errors="replace")
                return _unverified_result(command, outputs=outputs,
                    diagnostics=[*business_diagnostics, str(exc)],
                    detail="research output retained without report gating")
            return _result(command, "failed", outputs=dict(result.outputs) if result else {}, detail=str(exc), error_code="gap_research_invalid")


class ResearchReviewHandler:
    """Independently vet submitted knowledge; never certify execution obligations."""

    def __init__(self, stage="research_review"):
        if stage not in ("research_review", "admin_review"):
            raise ValueError("invalid research review stage")
        self.stage = stage

    def _current_context(self, command, root):
        source_ref = (command.payload.get("admin_submission_ref") if self.stage == "admin_review"
                      else command.artifact_refs.get("gap_research"))
        if not isinstance(source_ref, dict):
            raise ValueError("independent research review requires submitted evidence")
        source_text = verified_path(root, source_ref).read_text(encoding="utf-8")
        try:
            source = json.loads(source_text)
        except ValueError:
            source = {}
        if not isinstance(source, dict):
            source = {}
        obligations = command.payload.get("gap_obligations", [])
        if not isinstance(obligations, list):
            raise ValueError("gap_obligations must be an array")
        obligation_map = {_research_identity(row): row for row in obligations}
        if self.stage == "admin_review":
            submitted = source.get("generic_knowledge_entries", {})
            allowed_kinds = list(submitted) if isinstance(submitted, dict) else []
        else:
            producer = command.upstream_results.get("gap_research", {})
            produced_kinds = (producer.get("outputs", {}).get("research_kinds")
                              if isinstance(producer, dict) else None)
            allowed_kinds = (produced_kinds if isinstance(produced_kinds, list)
                             else command.payload.get("research_kinds", ["platform", "java"]))
        if (not isinstance(allowed_kinds, list)
                or (self.stage != "admin_review" and not allowed_kinds)
                or any(kind not in ("knowledge", "verification", "platform", "java")
                       for kind in allowed_kinds)):
            raise ValueError("invalid reviewed research kinds")
        # ``project_research_gaps`` is the complete project catalog, while a
        # research submission is scoped to the unresolved rows selected for the
        # current gap-research pass.  Prefer that explicit scope whenever it is
        # usable and apply exactly the producer's filters.  Standalone and admin
        # reviews retain the project-catalog fallback.
        context = _knowledge_gap_outputs(command.payload)
        scoped = context.get("unresolved_relevant_gaps")
        catalog = command.payload.get("project_research_gaps", [])
        if self.stage == "research_review":
            has_scoped_rows = isinstance(scoped, list)
            rows = scoped if has_scoped_rows else catalog
            rows = _eligible_research_gaps(rows, allowed_kinds,
                                           allow_legacy=not has_scoped_rows)
        else:
            rows = scoped if isinstance(scoped, list) else catalog
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("project research gaps must be an array of objects")
        known = {_research_identity(row) for row in rows}
        requirement_known = set(known).union(obligation_map)
        return source_ref, source, known, requirement_known, obligation_map, allowed_kinds

    def __call__(self, command):
        from .handlers import CodexStageHandler, _result, _unverified_result
        root = Path(command.run_dir)
        report_path = ".modport/" + self.stage.replace("_", "-") + ".json"
        reviewer = self.stage.replace("_", "-") + "-agent"
        result = None
        business_diagnostics = []
        try:
            try:
                (source_ref, source, known, requirement_known,
                 obligation_map, allowed_kinds) = self._current_context(command, root)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                if not business_gates_disabled(command):
                    raise
                business_diagnostics.append(str(exc))
                source_ref, source, known, requirement_known, obligation_map = ({"path": "<unavailable>"}, {}, set(), set(), {})
                raw_kinds = command.payload.get("research_kinds", ["platform", "java"])
                allowed_kinds = [kind for kind in raw_kinds if kind in ("knowledge", "verification", "platform", "java")] if isinstance(raw_kinds, list) else []
            prompt = f"""You are {reviewer}, independent from the research author.
Read the submitted report at {source_ref['path']} and its source files, and inspect the current
project gaps. The report is free-form text; do not reject it for formatting, missing fields,
or JSON syntax. Independently assess the evidence, omissions and unresolved questions.
Write your reasoning freely in {report_path}. Finish with a fenced JSON control block
containing verdict=approved|rejected. Only if approving state changes, include
approved_generic_knowledge_entries (platform/java mapping), approved_gap_resolutions
(current gap identity, project_status=resolved|not_applicable|unresolved,
evidence_artifact_ids referring to supplied source aliases), and verification_requirements
(gap_id,research_gap_id,due_stage,closure_criteria). Those are machine state updates,
not report formatting. Do not echo reviewer IDs, submission IDs, schema versions or hashes.
Never claim project execution passed based on research. Explain source support, missing
coverage and required corrections in your prose. An approved research disposition must
address every supplied research gap; unresolved is allowed. Later verification requirements
cannot replace unresolved research or weaken existing obligations.
"""
            prompt += "\nPublication entry parameters (only when publishing entries): " + GENERIC_ENTRIES_SCHEMA
            if command.options.get("gate_policy") == "downstream_toolcall":
                prompt = prompt.replace(
                    "An approved research disposition must\naddress every supplied research gap; unresolved is allowed.",
                    "Address only the current research you can assess. Omitted items remain unresolved; "
                    "do not repeat the complete catalog or manufacture dispositions to meet a count.")
                prompt += ("\nDiagnostics and a rejected verdict never dispatch rework. If another "
                           "agent needs to revise its work, request it through request_rework now. "
                           "The host normalizes duplicate verification IDs and retains their sources.")
            result = CodexStageHandler(prompt, baseline=True, required_paths=(report_path,))(command)
            if result.status != "completed":
                return result
            from .rework_tools import refresh_review_command
            command = refresh_review_command(command)
            (source_ref, source, known, requirement_known,
             obligation_map, allowed_kinds) = self._current_context(command, root)
            data = review_decision((root / "baseline" / report_path).read_text(encoding="utf-8"))
            data['reviewer_id'] = reviewer
            data['review_id'] = command.command_id
            findings = report_findings(data)
            resolutions = data.get("approved_gap_resolutions", [])
            if not isinstance(resolutions, list):
                raise ValueError("approved_gap_resolutions must be an array")
            seen = set()
            for row in resolutions:
                allowed = {"gap_id", "entry_id", "kind", "skill", "index", "project_status", "evidence_artifact_ids"}
                if not isinstance(row, dict) or set(row) - allowed:
                    raise ValueError("research disposition permits only identity, project_status and evidence_artifact_ids; no actions or nested requirements")
                identity = _research_identity(row)
                if identity not in known or identity in seen:
                    raise ValueError("unknown or duplicate reviewed gap")
                seen.add(identity)
                if row.get("project_status") not in ("resolved", "not_applicable", "unresolved"):
                    raise ValueError("research review cannot certify bypass or verification")
                ids = row.get("evidence_artifact_ids")
                if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
                    raise ValueError("reviewed resolution requires source evidence references")
                for identity in ids:
                    ref = command.artifact_refs.get(identity)
                    if not isinstance(ref, dict):
                        raise ValueError("unknown reviewed source evidence")
                    verified_path(root, ref)
            tool_driven = command.options.get("gate_policy") == "downstream_toolcall"
            if (data["verdict"] == "approved" and command.options.get("workflow_version", 0) >= 13
                    and seen != known and not tool_driven):
                raise ValueError("approved review requires explicit disposition for every current research gap")
            entries = _generic_entries(data.get("approved_generic_knowledge_entries", {}), allowed_kinds)
            requirements = data.get("verification_requirements", [])
            if not isinstance(requirements, list):
                raise ValueError("verification_requirements must be an array")
            for row in requirements:
                parent = row.get('research_gap_id') if isinstance(row, dict) else None
                if not isinstance(parent, str) or parent not in requirement_known:
                    raise ValueError('reviewed verification requirement references an unknown gap')
            if data["verdict"] == "approved" and command.options.get("workflow_version", 0) >= 13:
                dispositions = {_research_identity(row): row["project_status"] for row in resolutions}
                for requirement in requirements:
                    parent = requirement["research_gap_id"]
                    if parent in known and dispositions.get(parent) != "resolved":
                        raise ValueError("verification transfer requires independently resolved knowledge disposition")
            # Validate with the same merge semantics used at ingestion. No host
            # state is changed until the whole independent review is accepted.
            merge_reviewed_requirements(obligation_map, requirements, command.command_id,
                                        {identity: {} for identity in known},
                                        normalize_duplicates=tool_driven)
            outputs = dict(result.outputs)
            outputs.update(verdict=data["verdict"], reviewer_id=reviewer, review_id=data["review_id"], prior_findings=findings,
                           approved_generic_knowledge_entries=entries if data["verdict"] == "approved" else {},
                           approved_gap_resolutions=resolutions if data["verdict"] == "approved" else [],
                           verification_requirements=requirements if data["verdict"] == "approved" else [])
            if data['verdict'] == 'approved' and entries:
                from .wiki_knowledge import export_findings
                drafts, diagnostics = export_findings(command, entries=entries)
                outputs.update(wiki_contribution_drafts=drafts,
                               wiki_contribution_diagnostics=diagnostics)
            if tool_driven:
                outputs["unaddressed_research_gaps"] = sorted(known - seen)
            if self.stage == "admin_review":
                submission_id = command.payload.get("admin_submission_id")
                if not isinstance(submission_id, str):
                    raise ValueError("admin review submission binding mismatch")
                outputs["submission_id"] = submission_id
            if business_gates_disabled(command) and (business_diagnostics or data["verdict"] != "approved"):
                rejected = data["verdict"] != "approved"
                return _unverified_result(command, outputs=outputs,
                    status="failed" if rejected else "completed",
                    diagnostics=[*business_diagnostics,
                        *(["research review observed verdict=" + data["verdict"]]
                          if data["verdict"] != "approved" else [])],
                    detail="independent research review observations recorded",
                    error_code="research_review_rejected" if rejected else None)
            return _result(command, "completed", outputs=outputs, detail="independent research evidence review recorded")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            if business_gates_disabled(command) and result is not None and result.status == "completed":
                outputs = dict(result.outputs)
                report = root / "baseline" / report_path
                if report.is_file() and not report.is_symlink():
                    outputs["raw_report"] = report.read_text(encoding="utf-8", errors="replace")
                return _unverified_result(command, outputs=outputs,
                    diagnostics=[*business_diagnostics, str(exc)],
                    detail="research review retained without schema-gated acceptance")
            return _result(command, "failed", outputs=dict(result.outputs) if result else {}, detail=str(exc), error_code="research_review_invalid")
