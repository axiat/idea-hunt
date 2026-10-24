#!/usr/bin/env python3
"""Offline task facts only. Default preview; apply requires owner, CAS and evidence.

Cooperative writers share 状态.json.task-update.lock. This lock cannot serialize
an external writer that ignores it. No transport, source use or work resumption.
"""
import argparse
import copy
import datetime
import fcntl
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
_SPEC = importlib.util.spec_from_file_location("idea_hunt_readonly_gate", Path(__file__).with_name("任务校验.py"))
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)
COMMON = {"event_id", "type", "task_id", "agent_id", "stage", "occurred_at"}
EVIDENCE_FIELDS = {"evidence", "evidence_ref"}
FIELDS = {"register": {"task_ref"}, "input_observed": {"status"},
          "start_observed": {"runtime_id", "entry_receipt_ref", "entry_receipt"},
          "output_observed": {"output_delivery"}, "complete": set(),
          "terminal_observed": {"status", "failure_id", "runtime_id", "stop_receipt_ref"},
          "cancel_unstarted": {"unstarted_receipt_ref"}}
TERMINAL = {"completed", "failed", "stopped"}
OUTPUT_FIELDS = {"material_ref", "version", "formed_at", "delivered_at", "archived_at", "read_at",
                 "reader_id", "read_version", "read_material_ref", "message_ref", "read_message_ref",
                 "retained_at", "received_at", "producer_receipt_ref", "read_receipt_ref"}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def validate_event(event):
    gate.obj(event, "event")
    kind = event.get("type")
    gate.require(type(kind) is str and kind in FIELDS, "event_type", "unsupported event")
    gate.require(set(event) <= COMMON | EVIDENCE_FIELDS | FIELDS[kind] and COMMON <= set(event), "event_fields", "unsupported or missing event fields")
    gate.require(len(set(event) & EVIDENCE_FIELDS) == 1, "event_evidence", "exactly one evidence or evidence_ref required")
    if kind == "start_observed":
        gate.require(len(set(event) & {"entry_receipt", "entry_receipt_ref"}) == 1,
                     "entry_receipt_invalid", "exactly one entry_receipt or entry_receipt_ref required")
    if kind == "cancel_unstarted":
        gate.require("unstarted_receipt_ref" in event, "unstarted_receipt_invalid", "non-start receipt required")
    for field in ("event_id", "task_id", "agent_id", "stage"):
        gate.text(event.get(field), field)
    gate.timestamp(event.get("occurred_at"), "occurred_at")
    return kind


def administrative_preservation(event):
    return event["type"] == "cancel_unstarted" or (
        event["type"] == "terminal_observed" and event.get("status") == "stopped")


def legacy_output(record, output, observation):
    """Match a first structured declaration to the known legacy completed original."""
    gate.require(all(k in record for k in ("material_ref", "formed_at", "delivered_at")),
                 "legacy_output_unknown", "completed legacy output contract is incomplete", "hold")
    prior = gate.obj(record["material_ref"], "legacy.material_ref")
    gate.require(prior.get("sha256") == output["material_ref"].get("sha256") and
                 observation.get("legacy_material_path") == observation.get("output_material_path") and
                 observation.get("legacy_material_path") is not None,
                 "output_conflict", "completed legacy material cannot be replaced")
    for field in ("version", "material_version", "output_version"):
        if field in record:
            gate.require(record[field] == output["version"], "output_conflict", "legacy version differs")
    for field in gate.FACTS:
        if field in record:
            gate.require(field in output and gate.timestamp(record[field], field) == gate.timestamp(output[field], field),
                         "output_conflict", "legacy fact differs: " + field)
    if "read_at" in record:
        gate.require(record.get("reader_id") == output.get("reader_id"), "output_conflict", "legacy reader differs")
        if "read_version" in record:
            gate.require(record["read_version"] == output.get("read_version"), "output_conflict", "legacy read version differs")
        if "read_material_ref" in record:
            gate.require(record["read_material_ref"] == output.get("read_material_ref"), "output_conflict", "legacy read original differs")


def output_identity(output):
    return (output.get("version"), output.get("material_ref"), output.get("message_ref"))


def preserves_existing_output(checker, prior, output):
    """Recognize only an original already held by the committed root snapshot."""
    if type(prior) is not dict or type(output) is not dict:
        return False
    previous = prior.get("output_delivery")
    if type(previous) is dict:
        return output_identity(previous) == output_identity(output)
    if "output_delivery" in prior or prior.get("status") != "completed":
        return False
    if not all(k in prior for k in ("material_ref", "formed_at", "delivered_at")):
        return False
    checker.facts(prior)
    observation = {"legacy_material_path": str(checker.ref(prior["material_ref"])),
                   "output_material_path": str(checker.ref(output["material_ref"]))}
    legacy_output(prior, output, observation)
    return True


def reduce_events(root, events, observations, recorded_at):
    """Pure reducer. All file observations are supplied by the offline adapter."""
    gate.timestamp(recorded_at, "recorded_at")
    result = copy.deepcopy(root)
    tasks = result["harness_control"]["tasks"]
    changed = []
    for event in events:
        kind = validate_event(event)
        tid = event["task_id"]
        row = tasks.get(tid)
        if row is not None:
            gate.obj(row, "task record")
            for field in ("agent_id", "stage"):
                gate.require(row.get(field) == event[field], "route_mismatch", field)
            prior = row.get("lifecycle_events", {}).get(event["event_id"])
            if prior is not None:
                gate.require(prior.get("event") == event, "event_conflict", event["event_id"])
                continue
        # Event ids are unique across this root, not only within one task.
        gate.require(not any(event["event_id"] in r.get("lifecycle_events", {}) for r in tasks.values()),
                     "event_conflict", "event id already belongs to another task")
        observation = observations[event["event_id"]]
        when = gate.timestamp(event["occurred_at"], "occurred_at")
        if kind == "register":
            gate.require(row is None, "task_registered", tid)
            task = observation["task"]
            row = {"ref": copy.deepcopy(event["task_ref"]), "agent_id": event["agent_id"],
                   "stage": event["stage"], "status": "registered", "registered_at": event["occurred_at"],
                   "checkpoint": {"saved_refs": [], "missing_sections": []}}
            tasks[tid] = row
        else:
            gate.require(row is not None, "task_unregistered", tid)
            gate.require("unstarted_cancellation" not in row, "task_cancelled", tid)
            terminal = row.get("status") in TERMINAL
            # Late archival/output facts and a real stop may preserve a terminal task.
            gate.require(not terminal or kind == "output_observed" or
                         (kind == "terminal_observed" and event.get("status") == "stopped" and row.get("status") in {"failed", "completed", "stopped"}) or
                         (kind == "cancel_unstarted" and row.get("status") == "stopped"),
                         "task_terminal", tid)
            runtime_stop = kind == "terminal_observed" and event.get("status") == "stopped"
            if runtime_stop:
                if "started_at" in row:
                    gate.require(when >= gate.timestamp(row["started_at"], "started_at"),
                                 "event_order", "runtime stop precedes known start")
            else:
                for value in row.get("lifecycle_events", {}).values():
                    gate.require(when >= gate.timestamp(value["event"]["occurred_at"], "prior.occurred_at"),
                                 "event_order", "event precedes recorded lifecycle fact")
                for field in ("delivered_at", "accepted_at", "started_at", "completed_at", "failed_at", "stopped_at"):
                    if field in row:
                        gate.require(when >= gate.timestamp(row[field], field), "event_order", "event precedes " + field)
            if kind == "complete" and "output_delivery" in row:
                gate.require(all(when >= gate.timestamp(row["output_delivery"][k], k)
                                 for k in gate.FACTS if k in row["output_delivery"]),
                             "event_order", "completion precedes output facts")
            if kind == "input_observed":
                state = event.get("status")
                gate.require(state in {"delivered", "accepted"}, "input_status", "delivered or accepted required")
                if state == "delivered":
                    gate.require(row["status"] == "registered" and "delivered_at" not in row,
                                 "event_order", "delivery needs registered task")
                    row.update(status="delivered", delivered_at=event["occurred_at"])
                else:
                    gate.require(row["status"] == "delivered" and "delivered_at" in row,
                                 "event_order", "acceptance needs actual delivery")
                    gate.require(when >= gate.timestamp(row["delivered_at"], "delivered_at"), "event_order", "acceptance precedes delivery")
                    row.update(status="accepted", accepted_at=event["occurred_at"])
            elif kind == "start_observed":
                gate.require(row["status"] in {"delivered", "accepted"}, "event_order", "start needs input delivery")
                gate.require("accepted_at" in row, "acceptance_fact_missing", "start needs actual acceptance")
                accepted = gate.timestamp(row["accepted_at"], "accepted_at")
                gate.require(accepted >= gate.timestamp(row["delivered_at"], "delivered_at") and when >= accepted,
                             "event_order", "start must follow actual delivery and acceptance")
                row.update(status="started", started_at=event["occurred_at"], runtime_id=event["runtime_id"], runtime_state="running")
                key = "entry_receipt" if "entry_receipt" in event else "entry_receipt_ref"
                row[key] = copy.deepcopy(event[key])
            elif kind == "output_observed":
                gate.require(row["status"] in {"started", "completed", "failed", "stopped"}, "event_order", "output needs actual start")
                output = copy.deepcopy(event["output_delivery"])
                gate.require(set(output) <= OUTPUT_FIELDS, "output_fields", "unsupported output fields")
                previous = row.get("output_delivery")
                if row["status"] == "completed" and previous is None:
                    legacy_output(row, output, observation)
                if previous is not None:
                    gate.require(type(previous) is dict and all(k in previous for k in ("version", "formed_at")) and
                                 (("material_ref" in previous) != ("message_ref" in previous)),
                                 "output_contract_unknown", "existing structured output contract is incomplete", "hold")
                    same = output_identity(previous) == output_identity(output)
                    if same:
                        gate.require(all(k in output and output[k] == v for k, v in previous.items()),
                                     "output_conflict", "same artifact facts are append-only")
                    elif "read_at" in previous and "read_at" in output:
                        gate.require(gate.timestamp(output["read_at"], "read_at") > gate.timestamp(previous["read_at"], "prior.read_at"),
                                     "output_read_stale", "changed artifact needs a new actual read")
                row["output_delivery"] = output
                checkpoint = gate.obj(row.setdefault("checkpoint", {"saved_refs": [], "missing_sections": []}), "checkpoint")
                saved = checkpoint.get("saved_refs")
                gate.require(type(saved) is list, "type", "checkpoint.saved_refs must be an array")
                gate.strings(checkpoint.get("missing_sections"), "checkpoint.missing_sections")
                if "material_ref" in output and output["material_ref"] not in saved:
                    saved.append(copy.deepcopy(output["material_ref"]))
                if "message_ref" in output:
                    messages = checkpoint.setdefault("saved_messages", [])
                    gate.require(type(messages) is list, "type", "saved_messages must be an array")
                    if output["message_ref"] not in messages:
                        messages.append(copy.deepcopy(output["message_ref"]))
                # A completed scientific artifact is immutable; late missing receipt facts may be appended.
                if row["status"] == "completed" and previous:
                    gate.require(output_identity(previous) == output_identity(output),
                                 "task_terminal", "completed output artifact cannot be replaced")
            elif kind == "complete":
                gate.require(row["status"] == "started", "event_order", "completion needs actual start")
                row.update(status="completed", completed_at=event["occurred_at"])
            elif kind == "cancel_unstarted":
                gate.require(row.get("status") in {"registered", "stopped"} and "failure_id" not in row,
                             "unstarted_conflict", "cancellation cannot erase a failure or known work")
                forbidden = {"delivery_enqueued_at", "delivered_at", "accepted_at", "started_at", "runtime_id",
                             "entry_receipt", "entry_receipt_ref", "stop_receipt_ref", "output_delivery"}
                gate.require(not forbidden.intersection(row) and row.get("runtime_state") in (None, "unknown"),
                             "unstarted_conflict", "known delivery, execution or output fact exists")
                receipt = gate.obj(observation.get("unstarted_receipt"), "unstarted_receipt")
                gate.require(gate.timestamp(receipt.get("cancelled_at"), "cancelled_at") == when,
                             "event_order", "cancellation receipt time differs")
                row["unstarted_cancellation"] = {"receipt_ref": copy.deepcopy(event["unstarted_receipt_ref"]),
                                                "cancelled_at": event["occurred_at"]}
            elif kind == "terminal_observed":
                state = event.get("status")
                gate.require(state in {"failed", "stopped"}, "terminal_status", "failed or stopped required")
                if state == "failed" or "failure_id" in event:
                    gate.text(event.get("failure_id"), "failure_id")
                if state == "failed":
                    gate.require("runtime_id" not in event and "stop_receipt_ref" not in event,
                                 "event_fields", "failure does not assert runtime stop")
                    row.update(status="failed", failure_id=event["failure_id"], failed_at=event["occurred_at"])
                else:
                    gate.text(event.get("runtime_id"), "runtime_id")
                    gate.require("runtime_id" not in row or row["runtime_id"] == event["runtime_id"], "route_mismatch", "runtime_id")
                    if row.get("failure_id") is not None and "failure_id" in event:
                        gate.require(row["failure_id"] == event["failure_id"], "event_conflict", "failure identity")
                    if row.get("runtime_state") == "stopped":
                        gate.require(gate.timestamp(row.get("stopped_at", event["occurred_at"]), "stopped_at") == when and
                                     row.get("stop_receipt_ref") == event["stop_receipt_ref"],
                                     "event_conflict", "runtime stop facts are already recorded")
                    if row["status"] not in {"completed", "failed"}:
                        row["status"] = "stopped"
                        if "failure_id" in event:
                            row["failure_id"] = event["failure_id"]
                    row.update(stopped_at=row.get("stopped_at", event["occurred_at"]), runtime_id=event["runtime_id"], runtime_state="stopped",
                               stop_receipt_ref=copy.deepcopy(event["stop_receipt_ref"]))
        row.setdefault("lifecycle_events", {})[event["event_id"]] = {"event": copy.deepcopy(event), "recorded_at": recorded_at}
        changed.append(tid)
    return result, sorted(set(changed))


def observe(root, events, checker, root_sha, owner, original_tasks):
    observations = {}
    control = root["harness_control"]
    for event in events:
        kind = validate_event(event)
        if "evidence_ref" in event:
            evidence = checker.ref(event["evidence_ref"], True)
        else:
            evidence = gate.obj(event["evidence"], "evidence")
            gate.require(set(evidence) == {"kind", "id", "observer_id", "observed_at", "event"},
                         "event_evidence", "inline evidence fields")
            gate.require(evidence["kind"] in {"message", "tool"}, "event_evidence", "message or tool evidence required")
            gate.text(evidence.get("id"), "evidence.id")
        gate.text(evidence.get("observer_id"), "evidence.observer_id")
        if "observed_at" in evidence:
            gate.require(gate.timestamp(evidence["observed_at"], "evidence.observed_at") >=
                         gate.timestamp(event["occurred_at"], "occurred_at"), "event_order", "observation precedes occurrence")
        gate.require(evidence.get("event") == {k: v for k, v in event.items() if k not in EVIDENCE_FIELDS},
                     "evidence_mismatch", "evidence must bind the complete observed event")
        tid = event["task_id"]
        row = control["tasks"].get(tid)
        if row is not None and "checkpoint" in row:
            checkpoint = gate.obj(row["checkpoint"], "checkpoint")
            checker.refs(checkpoint.get("saved_refs"), "checkpoint.saved_refs")
            for original in checkpoint.get("saved_messages", []):
                checker.message_ref(original)
        ref = event.get("task_ref") if kind == "register" else (row or {}).get("ref")
        task = checker.ref(ref, True)
        gate.require(task.get("schema_version") == 1 and type(task["schema_version"]) is int,
                     "task_schema", "v1 task required")
        mapping = checker.protocol_mapping(task, row or {}) if not administrative_preservation(event) else None
        if administrative_preservation(event):
            gate.require(task.get("run_id") == checker.run.name, "run_id", "task run differs")
            gate.number(task.get("epoch"), "task.epoch")
            gate.require(0 < task["epoch"] <= control["epoch"], "epoch_stale", "administrative preservation requires an existing task epoch")
        else:
            gate.require(task.get("run_id") == checker.run.name and (task.get("epoch") == control.get("epoch") or mapping is not None),
                         "epoch_stale", "task run/epoch differs")
        for field in ("task_id", "agent_id", "stage"):
            gate.require(task.get(field) == event.get(field), "route_mismatch", field)
        if administrative_preservation(event):
            gate.require(row is not None, "task_unregistered", tid)
            for field in ("agent_id", "stage"):
                gate.require(row.get(field) == event[field], "route_mismatch", field)
            observation = {"task": task}
            if kind == "cancel_unstarted":
                gate.require(evidence["observer_id"] == owner, "unstarted_observer_mismatch", "root owner must observe cancellation")
                marker = {"receipt_ref": event["unstarted_receipt_ref"], "cancelled_at": event["occurred_at"]}
                gate.require("unstarted_cancellation" not in row or row["unstarted_cancellation"] == marker,
                             "event_conflict", "cancellation facts are append-only")
                receipt = checker.unstarted_cancelled(tid, dict(row, unstarted_cancellation=marker), owner)
                gate.require(gate.timestamp(receipt["cancelled_at"], "cancelled_at") == gate.timestamp(event["occurred_at"], "occurred_at"),
                             "event_order", "cancellation receipt time differs")
                observation["unstarted_receipt"] = receipt
            else:
                gate.text(event.get("runtime_id"), "runtime_id")
                gate.require("runtime_id" not in row or row["runtime_id"] == event["runtime_id"], "route_mismatch", "runtime_id")
                checker.stopped(tid, dict(row, runtime_id=event["runtime_id"], runtime_state="stopped",
                                         stop_receipt_ref=event.get("stop_receipt_ref")))
                receipt = checker.ref(event["stop_receipt_ref"], True)
                gate.require(gate.timestamp(receipt["stopped_at"], "stopped_at") == gate.timestamp(event["occurred_at"], "occurred_at"),
                             "event_order", "stopped_at differs from observed time")
            observations[event["event_id"]] = observation
            continue
        checker.ref(task.get("role_ref"))
        checker.refs(task.get("input_refs"), "input_refs")
        checker.output(task.get("output_dir"))
        for original in task.get("message_input_refs", []):
            checker.message_ref(original)
        if kind == "register":
            gate.require("recovery_of" not in task, "recovery_unsupported", "recovery routing requires owner review outside this tool")
            scope = gate.obj(task.get("task_scope"), "task_scope")
            gate.require(set(scope) == {"allowed_actions", "allowed_stages", "limits"}, "scope_unknown_fields", "task_scope fields")
            actions = gate.strings(scope["allowed_actions"], "allowed_actions")
            gate.require(bool(actions) and set(actions) <= gate.ACTIONS, "action_unsupported", "task actions")
            gate.require(task["stage"] in gate.strings(scope["allowed_stages"], "allowed_stages"), "scope_stage", task["stage"])
            for action, limit in gate.obj(scope["limits"], "limits").items():
                gate.require(action in actions, "scope_limit_action", action)
                gate.number(limit, action)
            permissions = gate.obj(control.get("permissions"), "permissions")
            gate.require(set(actions) <= set(permissions["allowed_actions"]) and not set(actions) & set(permissions["denied_actions"]),
                         "permission_denied", "registration cannot elevate permissions")
        elif row is not None:
            for field in ("agent_id", "stage"):
                gate.require(row.get(field) == event[field], "route_mismatch", field)
        replay = row is not None and row.get("lifecycle_events", {}).get(event["event_id"], {}).get("event") == event
        if kind == "start_observed" and not replay:
            gate.text(event.get("runtime_id"), "runtime_id")
            receipt = (checker.ref(event["entry_receipt_ref"], True) if "entry_receipt_ref" in event
                       else gate.obj(event["entry_receipt"], "entry_receipt"))
            expected = {"task_id": tid, "agent_id": task["agent_id"], "stage": task["stage"], "phase": "entry",
                        "decision": "allow", "ok": True, "epoch": control["epoch"], "task_sha256": ref["sha256"]}
            gate.require(all(type(receipt.get(k)) is type(v) and receipt[k] == v for k, v in expected.items()),
                         "entry_receipt_invalid", "entry receipt binding differs")
            gate.require(type(receipt.get("root_sha256")) is str and
                         gate.re.fullmatch(r"[0-9a-f]{64}", receipt["root_sha256"]),
                         "entry_receipt_invalid", "historical root snapshot hash required")
            gate.require(gate.timestamp(receipt.get("checked_at"), "checked_at") <= gate.timestamp(event["occurred_at"], "occurred_at"),
                         "event_order", "entry check follows reported start")
            entry = gate.Checker(checker.raw_run, ref["path"], "entry")
            try:
                entry.check()
            except gate.GateError as error:
                raise gate.GateError("entry_rejected", error.code + ": " + error.detail)
            checker.merge(entry)
        if kind == "output_observed":
            output = gate.obj(event.get("output_delivery"), "output_delivery")
            checker.delivery_facts(output)
            if "read_at" in output:
                gate.require(evidence["observer_id"] == output["reader_id"], "read_observer_mismatch",
                             "actual reader must supply the output read receipt")
            when = gate.timestamp(event["occurred_at"], "occurred_at")
            gate.require(all(gate.timestamp(output[k], k) <= when for k in gate.FACTS if k in output),
                         "event_order", "output facts follow observation")
        observation = {"task": task}
        if kind == "output_observed" and row is not None and row.get("status") == "completed" and "output_delivery" not in row:
            gate.require(all(k in row for k in ("material_ref", "formed_at", "delivered_at")),
                         "legacy_output_unknown", "completed legacy output contract is incomplete", "hold")
            checker.facts(row)
            observation["legacy_material_path"] = str(checker.ref(row["material_ref"]))
            observation["output_material_path"] = str(checker.ref(event["output_delivery"]["material_ref"]))
        if kind == "output_observed":
            preserve = preserves_existing_output(checker, original_tasks.get(tid), event["output_delivery"])
            preserve = preserve and preserves_existing_output(checker, row, event["output_delivery"])
            checker.scientific(task, dict(row, output_delivery=event["output_delivery"]), preserve_existing=preserve)
        observations[event["event_id"]] = observation
    return observations


def prepare(run, owner, expected_root_sha, events, recorded_at):
    checker = gate.Checker(run, "状态.json", "checkpoint")
    path = checker.root_path
    gate.require(not (checker.raw_run / "状态.json").is_symlink(), "root_symlink", "root state must be a regular file")
    original = checker.read(checker.raw_run / "状态.json")
    gate.require(sha(original) == expected_root_sha, "cas_conflict", "root hash changed")
    root = gate.parse(original, "root")
    gate.require(root.get("run_id", checker.run.name) == checker.run.name, "run_id", "run directory mismatch")
    gate.text(owner, "owner")
    gate.require(root.get("total_leader") == owner, "owner_mismatch", "only the existing root owner may update task facts")
    control = gate.obj(root.get("harness_control"), "harness_control")
    gate.require(control.get("schema_version") == 1 and type(control["schema_version"]) is int, "control_schema", "v1 required")
    gate.number(control.get("epoch"), "epoch")
    gate.require(control["epoch"] > 0, "epoch", "positive epoch required")
    if "owner" in control:
        gate.require(control["owner"] == owner, "owner_mismatch", "ambiguous root owner")
    gate.obj(control.get("tasks"), "tasks")
    gate.require(type(events) is list and 1 <= len(events) <= 20, "event_batch", "1..20 events required")
    gate.require(len(json.dumps(events, ensure_ascii=False).encode("utf-8")) <= 131072, "event_batch", "events exceed 128 KiB")
    ids = [gate.text(e.get("event_id"), "event_id") for e in events if type(e) is dict]
    gate.require(len(ids) == len(events) and len(set(ids)) == len(ids), "event_duplicate", "unique event ids per batch required")
    observations = {}
    result = root
    changed = []
    for event in events:
        current = observe(result, [event], checker, expected_root_sha, owner, control["tasks"])
        observations.update(current)
        result, touched = reduce_events(result, [event], current, recorded_at)
        changed.extend(touched)
    changed = sorted(set(changed))
    for tid in changed:
        row = result["harness_control"]["tasks"][tid]
        checker.facts(row)
        if "checkpoint" in row:
            checker.refs(gate.obj(row["checkpoint"], "checkpoint").get("saved_refs"), "checkpoint.saved_refs")
            for saved_message in row["checkpoint"].get("saved_messages", []):
                checker.message_ref(saved_message)
        task_events = [e for e in events if e["task_id"] == tid]
        if any(not administrative_preservation(e) for e in task_events):
            preserve = preserves_existing_output(checker, control["tasks"].get(tid), row.get("output_delivery"))
            checker.scientific(observations[task_events[0]["event_id"]]["task"], row, preserve_existing=preserve)
    checker.recheck()
    return checker, original, result, changed


def update_tasks(run, owner, expected_root_sha, events, apply=False):
    """Preview by default. File evidence and entry are always actually re-read."""
    lock_fd = None
    temporary = None
    committed = False
    try:
        gate.require(type(apply) is bool, "apply_flag", "apply must be boolean")
        recorded_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        raw_run = Path(run).absolute()
        run = gate.resolve_path(raw_run, strict=True)
        path = run / "状态.json"
        gate.require(not path.is_symlink(), "root_symlink", "root state must be a regular file")
        root_target = gate.resolve_path(path)
        if apply:
            lock_path = run / "状态.json.task-update.lock"
            lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            gate.require(stat.S_ISREG(os.fstat(lock_fd).st_mode), "lock_type", "regular cooperative lock required")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        gate.require(gate.resolve_path(raw_run) == run, "path_changed", str(raw_run))
        checker, original, result, changed = prepare(raw_run, owner, expected_root_sha, events, recorded_at)
        gate.require(checker.run == run and checker.root_path == root_target and gate.resolve_path(path) == root_target,
                     "path_changed", "prepared run/root differs from the initial locked write target")
        checker.recheck(root_conflict=apply)
        data = json.dumps(result, ensure_ascii=False, indent=2).encode("utf-8") + b"\n" if changed else original
        if apply and changed:
            fd, temporary = tempfile.mkstemp(prefix=".task-update-", suffix=".tmp", dir=str(run))
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            checker.recheck(root_conflict=True)
            gate.require(not path.is_symlink() and path.read_bytes() == original, "cas_conflict", "root changed before commit")
            os.replace(temporary, path)
            committed = True
            temporary = None
            directory = os.open(str(run), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return 0, {"ok": True, "mode": "apply" if apply else "preview", "changed_tasks": changed,
                   "changed": bool(changed), "expected_root_sha256": expected_root_sha,
                   "result_root_sha256": sha(data), "applied": apply and bool(changed), "action_permission": False}
    except gate.GateError as e:
        return 2 if e.level == "hold" else 1, {"ok": False, "diagnostics": [{"code": e.code, "detail": e.detail, "level": e.level}]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as e:
        return 1, {"ok": False, "applied": committed, "diagnostics": [{
            "code": "commit_durability_unknown" if committed else "invalid_data",
            "detail": str(e), "level": "error"}]}
    finally:
        if temporary is not None:
            os.unlink(temporary)
        if lock_fd is not None:
            os.close(lock_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "apply"), nargs="?", default="preview")
    parser.add_argument("--run", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--expected-root-sha", required=True)
    parser.add_argument("--events", required=True)
    args = parser.parse_args(argv)
    try:
        data = gate.parse(Path(args.events).read_bytes(), "events")
        gate.require(set(data) == {"events"}, "event_fields", "events wrapper required")
        code, result = update_tasks(args.run, args.owner, args.expected_root_sha, data["events"], args.mode == "apply")
    except (OSError, gate.GateError) as e:
        code, result = 1, {"ok": False, "diagnostics": [{"code": getattr(e, "code", "invalid_data"), "detail": str(e)}]}
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
