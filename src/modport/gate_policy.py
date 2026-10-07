"""Diagnostic handoffs: only an agent tool request authorizes upstream rework.

Raw SDK results remain failures when a check fails.  This module changes routing,
not evidence or acceptance.  A downstream consumer may use the available output
or request a revision; a verdict in a report never dispatches an author.
"""
from pathlib import Path

from .contracts import OperationInput, OperationResult, json_copy
from .evidence import digest, verified_path


GATE_POLICY = {"mode": "downstream_toolcall", "automatic_rework": False}
_DONE = frozenset({"succeeded", "failed", "cancelled", "timed_out", "dead"})


def downstream_toolcall(header):
    definition = header.get("definition", {})
    # This policy is part of the frozen v16 contract. Later workflows must not
    # accidentally inherit its mandatory consumer merely by keeping the field.
    return (definition.get("workflow_version", 0) == 16
            and definition.get("gate_policy", {}).get("mode") == "downstream_toolcall")


def passed(result):
    from .workflow import REVIEW_STAGES
    return (result.get("status") == "completed"
            and (result.get("stage_id") not in REVIEW_STAGES
                 or result.get("outputs", {}).get("verdict") == "approved"))


def forwarded(app, result):
    return result.get("command_id") in app.get("diagnostic_forwarded", [])


def handoff_task(task_id):
    return task_id.startswith("gate-handoff.")


def diagnostic_context(app):
    """Pass bounded diagnostic pointers rather than recursively nesting handoffs."""
    return [{"stage": row["stage"], "execution_id": row["execution_id"],
             "state": row["state"], "status": row["result"].get("status"),
             "error_code": row["result"].get("error_code"),
             "detail": row["result"].get("detail", ""),
             "artifact_refs": row["result"].get("outputs", {}).get("artifact_refs", {})}
            for row in app.get("gate_diagnostics", {}).values()]


def available_refs(root, refs):
    """A diagnostic consumer may inspect valid evidence despite a broken input.

    Unavailable references remain described in the original diagnostic result;
    they are never presented to a worker as verified evidence.
    """
    available, unavailable = {}, {}
    for name, ref in refs.items():
        try:
            try:
                verified_path(Path(root), ref)
            except (OSError, ValueError):
                candidate = Path(root) / ref["path"]
                if candidate.exists() or candidate.is_symlink():
                    raise
                from .artifact_retention import inspect_archived_artifact
                cold = inspect_archived_artifact(root, ref['path'])
                if cold is None or (ref.get('sha256') and ref['sha256'] != cold['sha256']):
                    raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            unavailable[name] = {"diagnostic": str(error), "available": False}
        else:
            available[name] = ref
    return available, unavailable


def diagnostic_view(root, value):
    """Quarantine bad nested evidence pointers in the consumer's input view.

    Original SDK results stay untouched. Missing/corrupt evidence is described
    as unavailable, never offered as a source merely because it is nested in a
    diagnostic or an upstream result instead of the main artifact map.
    """
    if isinstance(value, list):
        return [diagnostic_view(root, item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: diagnostic_view(root, item) for key, item in value.items()
              if key != "artifact_refs"}
    if "artifact_refs" in value:
        refs = value["artifact_refs"]
        if isinstance(refs, dict):
            result["artifact_refs"], unavailable = available_refs(root, refs)
            if unavailable:
                result["unavailable_artifact_refs"] = {
                    **result.get("unavailable_artifact_refs", {}), **unavailable}
        else:
            result["artifact_refs"] = {}
            result["unavailable_artifact_refs"] = {"diagnostic": "artifact_refs is not an object"}
    return result


class DownstreamGateOrchestration:
    def _diagnostic_handoff(self, snapshot, header, app, stage, outcome, execution_id,
                            *, location="main"):
        records = app.setdefault("gate_diagnostics", {})
        if execution_id in records:
            return []
        task_id = "gate-handoff." + digest(execution_id)[:20]
        from .workflow import EARLY_STAGES
        group = app.get("active_group") or {}
        workspace = ("baseline" if stage in EARLY_STAGES or stage.startswith("contract_")
                     or group.get("goal_scope") == "contract" else "worktree")
        record = {"stage": stage, "task_id": outcome.task_id, "execution_id": execution_id,
                  "location": location, "result": outcome.to_dict(),
                  "consumer_task_id": task_id, "workspace": workspace, "state": "pending"}
        records[execution_id] = record
        if location != "group":
            app["effective"][stage] = outcome.to_dict()
        operations = self._schedule(snapshot, header, app, "gate_handoff", task_id=task_id,
            activate=False, dependencies=[], causation_id=execution_id,
            payload={"gate_diagnostic": json_copy(record)},
            upstream_overrides={outcome.task_id: outcome.to_dict()})
        if any(op["kind"] == "dispatch" for op in operations):
            record["state"] = "running"
        return operations

    def _gate_handoff_decision(self, snapshot, header, app):
        if not downstream_toolcall(header):
            return []
        operations = []
        for record in app.get("gate_diagnostics", {}).values():
            if record["state"] != "running":
                continue
            task = snapshot["tasks"].get(record["consumer_task_id"])
            if not task or task["attempts"][-1]["state"] not in _DONE:
                continue
            attempt = task["attempts"][-1]
            command = OperationInput.from_dict(attempt["command"]["payload"])
            if command.command_id in app["processed"]:
                continue
            app["processed"].append(command.command_id)
            if attempt["state"] == "succeeded":
                consumer = OperationResult.from_dict(attempt["result"]["value"])
                consumer.validate_for(command)
            else:
                consumer = OperationResult("blocked", command.run_id, command.task_id,
                    command.stage_id, command.command_id,
                    error_code="execution_" + attempt["state"])
            app["effective"][command.task_id] = consumer.to_dict()
            app["history"].append({"stage": command.stage_id, "task_id": command.task_id,
                "execution_id": command.command_id, "state": consumer.status})
            record["consumer_result"] = consumer.to_dict()
            if consumer.status != "completed":
                # A missing/failed handoff is not a downstream decision to use
                # the diagnostic. Preserve both results and stop this segment;
                # only an explicit continuation may create another consumer.
                record["state"] = "consumer_failed"
                return operations + self._finish(
                    app, consumer.error_code or "gate_handoff_failed")
            # Only results produced for an accepted tool request by this consumer
            # may replace the failed transition. Unrelated completions do not.
            selected = record["result"]
            requests = sorted(app.get("review_rework", {}).get("requests", {}).values(),
                              key=lambda row: row.get("sequence", 0))
            for request in requests:
                if request.get("reviewer_execution_id") != command.command_id:
                    continue
                for update in request.get("updates", []):
                    value = update["result"]
                    if (update.get("target_agent") == record["task_id"]
                            or (request.get("followup_stage") == record["stage"]
                                and update.get("stage") == record["stage"])):
                        selected = value
            record["current_result"] = json_copy(selected)
            if selected["command_id"] != record["execution_id"] and passed(selected):
                record["state"] = "repaired_by_tool"
                if record["location"] == "group":
                    group = app.get("active_group")
                    if group:
                        group["results"][record["task_id"]] = json_copy(selected)
                        group.pop("failure", None)
                else:
                    app.setdefault("gate_resumptions", {})[record["task_id"]] = json_copy(selected)
                    if record["location"] == "early":
                        app.setdefault("early_pending", []).append(record["task_id"])
                    elif record["location"] == "gap":
                        app.setdefault("gap_pending", []).append(record["task_id"])
                    elif record["location"] == "support":
                        app.setdefault("support_pending", []).append(record["task_id"])
                    else:
                        app["active_stage"] = record["task_id"]
                continue
            record["state"] = "forwarded"
            for identity in {record["execution_id"], selected["command_id"]}:
                if identity not in app.setdefault("diagnostic_forwarded", []):
                    app["diagnostic_forwarded"].append(identity)
            operations += self._forward_gate_diagnostic(snapshot, header, app, record)
        return operations

    @staticmethod
    def _resumed_attempt(snapshot, app, task_id, original):
        """Resume routing from the real tool task; never rewrite an SDK attempt."""
        resumed = app.get("gate_resumptions", {}).pop(task_id, None)
        if resumed is None:
            return original, False
        for task in snapshot["tasks"].values():
            for attempt in task["attempts"]:
                if attempt["command"]["execution_id"] == resumed["command_id"]:
                    if attempt["state"] not in _DONE:
                        raise ValueError("downstream tool result is not settled")
                    return attempt, True
        raise ValueError("downstream tool result has no SDK attempt")

    def _forward_gate_diagnostic(self, snapshot, header, app, record):
        from .workflow import EARLY_STAGES, NEXT_STAGE
        stage, location = record["stage"], record["location"]
        if location == "early":
            app.setdefault("early_failures", {}).pop(stage, None)
            if stage in EARLY_STAGES:
                return []  # The early DAG admits the diagnostic to its consumers.
            next_stage = {"platform_diff": "platform_skill_review",
                          "java_diff": "java_skill_review",
                          "gap_research": "research_review"}.get(stage)
            if stage.startswith("contract_"):
                next_stage = NEXT_STAGE.get(stage)
            if next_stage:
                return self._early_schedule(snapshot, header, app, next_stage, dependencies=[],
                    causation_id=record["execution_id"])
            return []
        if location == "gap":
            app["gap_failure"] = record["result"].get("error_code")
            # Research is not automatically repeated because a report is rejected.
            # Its unresolved questions remain available to every downstream agent.
            return self._resume_continuation_route(snapshot, header, app, record)
        if location == "support":
            return self._resume_continuation_route(snapshot, header, app, record)
        if location == "group":
            group = app.get("active_group")
            if group is None:
                return []
            # Preserve unfinished peers and original patches. The downstream
            # consumer can request scoped revisions; no group is silently passed.
            app["failed_development_group"] = json_copy(group)
            app["active_group"] = None
            if group["kind"] == "skills":
                next_stage = "skill_publish"
            elif group["kind"] == "regression":
                next_stage = "acceptance_preflight"
            else:
                next_stage = ("contract_review" if group.get("goal_scope") == "contract"
                              else "code_review")
            if app.get("early_active"):
                return self._early_schedule(snapshot, header, app, next_stage, dependencies=[],
                    causation_id=record["execution_id"])
            return self._schedule(snapshot, header, app, next_stage, dependencies=[],
                causation_id=record["execution_id"])
        next_stage = NEXT_STAGE.get(stage)
        if next_stage:
            return self._schedule(snapshot, header, app, next_stage, dependencies=[],
                causation_id=record["execution_id"])
        return self._finish(app, "delivery_incomplete" if stage == "delivery"
                            else "downstream_work_incomplete")

    def _resume_continuation_route(self, snapshot, header, app, record):
        """Resume saved control flow without referring to prior-segment SDK tasks."""
        feedback = app.get("continuation_feedback")
        resume = feedback.get("resume") if isinstance(feedback, dict) else None
        gate_failure = feedback.get("gate_failure") if isinstance(feedback, dict) else None
        if (not isinstance(resume, dict)
                or not isinstance(gate_failure, dict)
                or gate_failure.get("command_id") != record["execution_id"]
                or resume.get("location") != record["location"]):
            return []

        join_stage = resume.get("gap_join_stage")
        if isinstance(join_stage, str) and join_stage:
            app["gap_join_stage"] = join_stage
            app["gap_join_ids"] = json_copy(resume.get("gap_join_ids"))
            return []
        if resume.get("early_active") is True:
            app["early_active"] = True
            return []

        active = resume.get("active_stage")
        result = app.get("effective", {}).get(active) if isinstance(active, str) else None
        if isinstance(result, dict):
            try:
                outcome = OperationResult.from_dict(result)
            except (TypeError, ValueError):
                outcome = None
            if outcome is not None:
                if passed(result) or forwarded(app, result):
                    from .workflow import NEXT_STAGE
                    next_stage = NEXT_STAGE.get(outcome.stage_id)
                    if next_stage:
                        return self._schedule(snapshot, header, app, next_stage,
                            dependencies=[], causation_id=outcome.command_id)
                else:
                    return self._diagnostic_handoff(snapshot, header, app,
                        outcome.stage_id, outcome, outcome.command_id, location="main")
        return self._finish(app, "continuation_route_incomplete")


class GateHandoffHandler:
    """A downstream consumer, not a second validator or an automatic repairer."""
    def __call__(self, command):
        from .handlers import CodexStageHandler
        relative = ".modport/gate-handoffs/" + command.command_id + ".md"
        prompt = (
            "Consume the preceding stage's available work and diagnostic evidence. "
            "Checks are observations, not instructions to reject or replan. Decide whether "
            "the next work actually needs a correction. If so, use request_rework to ask "
            "the relevant upstream assignment for a scoped revision, or request the failed "
            "check itself to run again after the revision. Only tool calls start rework; "
            "words such as rejected or replan in your report do not. You may use adequate "
            "partial work and leave unrelated verification to its owning stage. Do not "
            "edit project code, invent missing evidence, or claim an unexecuted check passed. "
            "Write a short natural-language handoff to " + relative + " describing "
            "what can be used and what is still unknown. No JSON, disposition list, approval "
            "token or repeated catalog is required. If a prerequisite really needs repair, "
            "request it now; your report by itself will not launch another agent."
        )
        baseline = command.payload.get("gate_diagnostic", {}).get("workspace") == "baseline"
        return CodexStageHandler(prompt, baseline=baseline, required_paths=(relative,))(command)
