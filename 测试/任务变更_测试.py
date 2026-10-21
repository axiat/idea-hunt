#!/usr/bin/env python3
"""Task mutation regressions; every writable run is a TemporaryDirectory."""
import ast
import copy
import hashlib
import importlib.util
import json
import subprocess
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parents[1]
SCRIPT = BASE / "脚本" / "任务变更.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mutation = load("task_mutation", SCRIPT)
fixtures = load("task_fixtures", BASE / "测试" / "任务校验测试.py")
TIME = fixtures.TIME
LATER = "2026-10-14T10:00:30+00:00"


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Fixture()
        self.addCleanup(self.f.close)
        self.f.root["total_leader"] = "main"
        self.f.root.update(goal_met=False, history=[{"preserved": True}])
        self.f.control.update(source_ledger_ref={"path": "not-used", "sha256": "0" * 64},
                              miscellaneous={"unknown": [1, 2, 3]})
        # No task source actions are requested; unrelated source state is never rewritten.
        self.f.write()
        self.counter = 0

    @property
    def root_path(self):
        return self.f.run / "状态.json"

    def root(self):
        return json.loads(self.root_path.read_bytes())

    def digest(self):
        return hashlib.sha256(self.root_path.read_bytes()).hexdigest()

    def event(self, kind="input_observed", **fields):
        self.counter += 1
        event = {"event_id": "e%d" % self.counter, "type": kind, "task_id": "t1", "agent_id": "agent1",
                 "stage": "problem-family", "occurred_at": TIME}
        if kind == "input_observed":
            event["status"] = "delivered"
        event.update(fields)
        output = event.get("output_delivery", {})
        observer = output.get("reader_id", "observer") if "read_at" in output else "observer"
        return self.evidence(event, observer=observer)

    def evidence(self, event, observer="observer"):
        event = copy.deepcopy(event)
        event.pop("evidence_ref", None)
        ref = self.f.put("证据/%s.json" % event["event_id"], {"event": event, "observer_id": observer})
        event["evidence_ref"] = ref
        return event

    def update(self, events, apply=True, expected=0, diagnostic=None, owner="main", digest=None):
        before = self.root_path.read_bytes()
        code, result = mutation.update_tasks(self.f.run, owner, digest or self.digest(), events, apply=apply)
        self.assertEqual(code, expected, result)
        if diagnostic:
            self.assertEqual(result["diagnostics"][0]["code"], diagnostic, result)
        if not apply or code:
            self.assertEqual(self.root_path.read_bytes(), before)
        return result

    def start(self):
        self.update([self.event(), self.event(status="accepted")])
        receipt = {"task_id": "t1", "agent_id": "agent1", "stage": "problem-family", "phase": "entry",
                   "decision": "allow", "ok": True, "epoch": 1, "root_sha256": self.digest(),
                   "task_sha256": self.root()["harness_control"]["tasks"]["t1"]["ref"]["sha256"], "checked_at": TIME}
        ref = self.f.put("启动入口收据.json", receipt)
        event = self.event("start_observed", runtime_id="runtime1", entry_receipt_ref=ref)
        self.update([event])
        return event

    def output(self, version="v1", read=True, at=TIME):
        path = self.f.run / "小组" / "一组" / ("科学稿_%s.md" % version)
        path.write_text("# 判断\n当前零种子，接口未知。\n", encoding="utf-8")
        ref = self.f.ref(path)
        output = {"material_ref": ref, "version": version, "formed_at": at, "delivered_at": at, "archived_at": at}
        if read:
            output.update(read_at=at, reader_id="leader", read_version=version, read_material_ref=ref)
        return output

    def adopt_science(self):
        work = {"schema_version": 1, "decision_question": "证据是否支持候选？",
                "premises": {"required_facts": [], "allowed_assumptions": ["接口可用"], "deferred_unknowns": []},
                "outcome_routes": {"supported": "继续", "refuted": "收缩", "unknown": "暂停"},
                "deliverable": {"format": "markdown", "version": "v1", "reader_id": "leader"}}
        ref = self.f.put("科学合同.json", {"scientific_work": work, "seeds": []})
        self.f.task["input_refs"].append(ref)
        self.f.task["scientific_work_ref"] = ref
        self.f.task["task_scope"]["allowed_actions"] += ["generate_local", "preserve_local"]
        self.f.write()

    def cancellation_event(self, status="stopped"):
        root = self.root()
        row = root["harness_control"]["tasks"]["t1"]
        report = {"ok": False, "decision": "fail", "phase": "dispatch", "task_id": "t1",
                  "diagnostics": [{"level": "error", "code": "source_cost", "detail": "synthetic rejected dispatch"}]}
        row.update(status=status, actor_started_or_delivered=False, source_actions_executed=0,
                   action_used={"read_local": 0}, dispatch_check_report=report, dispatch_checked_at_utc=TIME,
                   material_ref=copy.deepcopy(row["ref"]), formed_at=TIME)
        self.root_path.write_text(json.dumps(root))
        task = json.loads((self.f.run / row["ref"]["path"]).read_bytes())
        receipt = {"schema_version": 1, "run_id": self.f.run.name, "task_id": "t1",
                   "task_sha256": row["ref"]["sha256"], "epoch": task["epoch"], "agent_id": "agent1",
                   "stage": task["stage"], "status": "not_started", "success": True,
                   "cancelled_at": LATER, "confirmed_by": "main", "dispatch_check_report": copy.deepcopy(report),
                   "delivery_enqueued": False, "delivered": False, "accepted": False, "started": False,
                   "pending_delivery": False, "source_actions_executed": 0,
                   "observation": {"kind": "tool", "id": "synthetic-owner-non-start-observation",
                                   "observer_id": "main", "observed_at": LATER}}
        ref = self.f.put("未启动收据.json", receipt)
        return self.inline_evidence(self.event("cancel_unstarted", occurred_at=LATER, unstarted_receipt_ref=ref),
                                    observer="main")

    def cancellation_receipt(self, event, receipt):
        changed = copy.deepcopy(event)
        changed["unstarted_receipt_ref"] = self.f.put("错误未启动收据.json", receipt)
        return self.inline_evidence(changed, observer="main")

    def test_default_preview_creates_no_files(self):
        event = self.event()
        before = {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()}
        result = self.update([event], apply=False)
        self.assertEqual(result["mode"], "preview")
        self.assertFalse(result["applied"])
        after = {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.f.run / "状态.json.task-update.lock").exists())
        code, result = mutation.update_tasks(self.f.run, "main", self.digest(), [event])
        self.assertEqual(code, 0, result)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()})

    def test_apply_cas_and_other_root_values_preserved(self):
        before = self.root()
        digest = self.digest()
        result = self.update([self.event()])
        after = self.root()
        before["harness_control"].pop("tasks")
        after["harness_control"].pop("tasks")
        self.assertEqual(before, after)
        self.assertEqual(result["result_root_sha256"], self.digest())
        self.update([self.event(status="accepted")], digest=digest, expected=1, diagnostic="cas_conflict")
        self.assertFalse(result["action_permission"])

    def test_replay_exact_no_write_and_conflict_rejected(self):
        event = self.event()
        self.update([event])
        original = self.root_path.read_bytes()
        result = self.update([event])
        self.assertFalse(result["changed"])
        self.assertEqual(self.root_path.read_bytes(), original)
        other = self.evidence(dict(event, occurred_at=LATER))
        self.update([other], expected=1, diagnostic="event_conflict")

    def test_owner_and_ambiguous_owner(self):
        event = self.event()
        self.update([event], owner="member", expected=1, diagnostic="owner_mismatch")
        root = self.root()
        root["harness_control"]["owner"] = "other"
        self.root_path.write_text(json.dumps(root))
        self.update([event], expected=1, diagnostic="owner_mismatch")

    def test_route_and_evidence_binding(self):
        self.update([self.event(agent_id="wrong")], expected=1, diagnostic="route_mismatch")
        self.update([self.event(stage="wrong")], expected=1, diagnostic="route_mismatch")
        event = self.event()
        event["occurred_at"] = LATER
        self.update([event], expected=1, diagnostic="evidence_mismatch")
        event = self.event()
        event["evidence_ref"]["sha256"] = "0" * 64
        self.update([event], expected=1, diagnostic="hash_mismatch")

    def test_batch_bounds_duplicate_and_field_whitelist(self):
        event = self.event()
        for fields in ({"epoch": 2}, {"paused": False}, {"goal_met": True}, {"permissions": {}},
                       {"recoveries": {}}, {"action_used": {}}, {"source_ledger_ref": {}}, {"patch": {}}):
            self.update([self.evidence(dict(event, **fields))], expected=1, diagnostic="event_fields")
        self.update([], expected=1, diagnostic="event_batch")
        self.update([event] * 21, expected=1, diagnostic="event_batch")
        self.update([event, event], expected=1, diagnostic="event_duplicate")
        self.update([self.event("patch")], expected=1, diagnostic="event_type")

    def test_register_then_input_batch_and_register_not_dispatch(self):
        root = self.root()
        root["status"] = "paused_by_user"
        root["harness_control"]["paused"] = True
        ref = root["harness_control"]["tasks"].pop("t1")["ref"]
        self.root_path.write_text(json.dumps(root))
        self.update([self.event("register", task_ref=ref), self.event()])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "delivered")
        self.assertTrue(self.root()["harness_control"]["paused"])
        self.assertEqual(mutation.gate.check_task(self.f.run, self.f.path, "entry")[0], 1)

    def test_registration_cannot_modify_existing_or_recover(self):
        ref = self.root()["harness_control"]["tasks"]["t1"]["ref"]
        self.update([self.event("register", task_ref=ref)], expected=1, diagnostic="task_registered")
        root = self.root()
        root["harness_control"]["tasks"] = {}
        self.f.task["recovery_of"] = {"task_id": "old", "failure_id": "f"}
        ref = self.f.put("任务.json", self.f.task)
        self.root_path.write_text(json.dumps(root))
        self.update([self.event("register", task_ref=ref)], expected=1, diagnostic="recovery_unsupported")

    def test_input_order_and_actual_times(self):
        self.update([self.event(status="accepted")], expected=1, diagnostic="event_order")
        self.update([self.event()])
        self.update([self.event(status="accepted", occurred_at="2026-10-14T09:00:00Z")], expected=1, diagnostic="event_order")
        self.update([self.event(status="accepted", occurred_at=LATER)])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["delivered_at"], TIME)
        self.assertEqual(row["accepted_at"], LATER)
        self.assertNotIn("material_ref", row)
        self.assertNotIn("read_at", row)
        self.assertIn("recorded_at", next(iter(row["lifecycle_events"].values())))

    def test_real_entry_start_and_stale_entry(self):
        self.start()
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["runtime_state"], "running")
        self.assertEqual(row["started_at"], TIME)
        self.assertEqual(row["status"], "started")

    def test_start_requires_real_entry_not_self_report(self):
        self.update([self.event()])
        receipt = {"task_id": "t1", "agent_id": "agent1", "stage": "problem-family", "phase": "entry", "decision": "allow",
                   "ok": True, "epoch": 1, "root_sha256": "invalid-hash", "task_sha256": self.f.ref(self.f.path)["sha256"], "checked_at": TIME}
        event = self.event("start_observed", runtime_id="runtime1", entry_receipt_ref=self.f.put("入口.json", receipt))
        self.update([event], expected=1, diagnostic="entry_receipt_invalid")
        root = self.root()
        root["status"] = "paused_by_user"
        self.root_path.write_text(json.dumps(root))
        receipt["root_sha256"] = self.digest()
        event = self.event("start_observed", runtime_id="runtime1", entry_receipt_ref=self.f.put("入口.json", receipt))
        self.update([event], expected=1, diagnostic="entry_rejected")

    def test_failure_is_not_runtime_stop_and_terminal_cannot_revive(self):
        self.start()
        self.update([self.event("terminal_observed", status="failed", failure_id="f1")])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["runtime_state"], "running")
        self.assertNotIn("stop_receipt_ref", row)
        self.update([self.event()], expected=1, diagnostic="task_terminal")
        self.update([self.event("complete")], expected=1, diagnostic="task_terminal")
        self.update([self.event("terminal_observed", status="failed", failure_id="f2", runtime_id="runtime1")],
                    expected=1, diagnostic="task_terminal")

    def stop_event(self, **changes):
        receipt = {"task_id": "t1", "agent_id": "agent1", "runtime_id": "runtime1", "status": "stopped",
                   "success": True, "stopped_at": TIME}
        receipt.update(changes)
        return self.event("terminal_observed", status="stopped", failure_id="f1", runtime_id="runtime1",
                          stop_receipt_ref=self.f.put("停止.json", receipt))

    def test_real_stop_and_no_task_found_rejection(self):
        self.start()
        for changes in ({"message": "No task found"}, {"success": False}, {"runtime_id": "wrong"},
                        {"agent_id": "wrong"}, {"task_id": "wrong"}, {"status": "failed"}, {"success": 1}):
            self.update([self.stop_event(**changes)], expected=1, diagnostic="stop_receipt_invalid")
        self.update([self.event("terminal_observed", status="failed", failure_id="f1")])
        self.update([self.stop_event()])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertEqual(row["status"], "failed")

    def test_scientific_complete_requires_delivery_and_actual_read(self):
        self.adopt_science()
        self.start()
        self.update([self.event("complete")], expected=1, diagnostic="delivery_fact_missing")
        output = self.output(read=False)
        self.update([self.event("output_observed", output_delivery=output)])
        self.update([self.event("complete")], expected=1, diagnostic="scientific_delivery_missing")
        output = self.output()
        self.update([self.event("output_observed", output_delivery=output), self.event("complete")])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "completed")
        self.assertFalse(self.root()["goal_met"])

    def test_generic_complete_cannot_relabel_input_as_output(self):
        self.start()
        self.update([self.event("complete")], expected=1, diagnostic="delivery_fact_missing")
        output = self.output(read=False)
        self.update([self.event("output_observed", output_delivery=output), self.event("complete")])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "completed")
        self.assertNotIn("read_at", output)  # Actual reading is required only by explicit scientific contracts.

    def test_output_new_hash_cannot_inherit_read(self):
        self.start()
        old = self.output()
        self.update([self.event("output_observed", output_delivery=old)])
        new = self.output("v2")
        new["read_material_ref"] = old["material_ref"]
        self.update([self.event("output_observed", output_delivery=new)], expected=1, diagnostic="output_read_mismatch")
        new["read_material_ref"] = new["material_ref"]
        self.update([self.event("output_observed", output_delivery=new)], expected=1, diagnostic="output_read_stale")
        new["read_at"] = LATER
        self.update([self.event("output_observed", output_delivery=new, occurred_at=LATER)])

    def test_output_append_only_and_late_preservation(self):
        self.start()
        output = self.output(read=False)
        self.update([self.event("output_observed", output_delivery=output)])
        removed = dict(output)
        removed.pop("delivered_at")
        self.update([self.event("output_observed", output_delivery=removed)], expected=1, diagnostic="output_conflict")
        self.update([self.event("terminal_observed", status="failed", failure_id="f1")])
        output = self.output()
        self.update([self.event("output_observed", output_delivery=output)])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "failed")

    def test_completed_artifact_is_not_replaced(self):
        self.start()
        self.update([self.event("output_observed", output_delivery=self.output()), self.event("complete")])
        self.update([self.event("output_observed", output_delivery=self.output("v2", at=LATER), occurred_at=LATER)],
                    expected=1, diagnostic="task_terminal")

    def test_atomic_replace_failure_retains_original(self):
        event = self.event()
        with mock.patch.object(mutation.os, "replace", side_effect=OSError("synthetic replace failure")):
            self.update([event], expected=1, diagnostic="invalid_data")
        self.assertEqual(list(self.f.run.glob(".task-update-*")), [])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "registered")

    def test_fsync_failure_retains_original(self):
        event = self.event()
        with mock.patch.object(mutation.os, "fsync", side_effect=OSError("synthetic fsync failure")):
            self.update([event], expected=1, diagnostic="invalid_data")
        self.assertEqual(list(self.f.run.glob(".task-update-*")), [])

    def test_commit_cas_race_and_evidence_race(self):
        event = self.event()
        original_prepare = mutation.prepare
        before = self.root_path.read_bytes()
        def changed_root(*args):
            result = original_prepare(*args)
            self.root_path.write_bytes(before + b" ")
            return result
        with mock.patch.object(mutation, "prepare", changed_root):
            code, result = mutation.update_tasks(self.f.run, "main", self.digest(), [event], apply=True)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][0]["code"], "cas_conflict")
        self.assertEqual(self.root_path.read_bytes(), before + b" ")
        self.root_path.write_bytes(before)
        evidence = self.f.run / event["evidence_ref"]["path"]
        def changed_evidence(*args):
            result = original_prepare(*args)
            evidence.write_bytes(evidence.read_bytes() + b" ")
            return result
        with mock.patch.object(mutation, "prepare", changed_evidence):
            self.update([event], expected=1, diagnostic="file_changed")

    def test_cooperative_concurrent_cas_one_writer_wins(self):
        digest = self.digest()
        events = [self.event(), self.event()]
        commands = []
        for index, event in enumerate(events):
            ref = self.f.put("批次%d.json" % index, {"events": [event]})
            commands.append([sys.executable, "-I", "-B", str(SCRIPT), "apply", "--run", str(self.f.run),
                             "--owner", "main", "--expected-root-sha", digest, "--events", str(self.f.run / ref["path"])])
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for command in commands]
        results = [process.communicate(timeout=15) for process in processes]
        self.assertEqual(sorted(p.returncode for p in processes), [0, 1], results)
        failures = [json.loads(out) for p, (out, _) in zip(processes, results) if p.returncode]
        self.assertEqual(failures[0]["diagnostics"][0]["code"], "cas_conflict")
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(len(row["lifecycle_events"]), 1)
        inode = (self.f.run / "状态.json.task-update.lock").stat().st_ino
        self.update([self.event(status="accepted")])
        self.assertEqual((self.f.run / "状态.json.task-update.lock").stat().st_ino, inode)

    def test_output_read_requires_recipient_observation(self):
        self.start()
        event = self.event("output_observed", output_delivery=self.output())
        event = self.evidence(event, observer="administrative-owner")
        self.update([event], expected=1, diagnostic="read_observer_mismatch")

    def test_directory_fsync_failure_reports_committed_state(self):
        event = self.event()
        original = mutation.os.fsync
        calls = []
        def fail_directory(fd):
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("synthetic directory fsync failure")
            return original(fd)
        with mock.patch.object(mutation.os, "fsync", fail_directory):
            code, result = mutation.update_tasks(self.f.run, "main", self.digest(), [event], apply=True)
        self.assertEqual(code, 1, result)
        self.assertTrue(result["applied"])
        self.assertEqual(result["diagnostics"][0]["code"], "commit_durability_unknown")
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "delivered")

    def test_existing_manual_facts_also_enforce_order(self):
        root = self.root()
        row = root["harness_control"]["tasks"]["t1"]
        row.update(status="started", delivered_at=TIME, started_at=LATER, runtime_id="runtime1")
        self.root_path.write_text(json.dumps(root))
        self.update([self.event("complete")], expected=1, diagnostic="event_order")

    def inline_evidence(self, event, kind="message", observer="observer", observed_at=None):
        event = copy.deepcopy(event)
        event.pop("evidence_ref", None)
        event.pop("evidence", None)
        event["evidence"] = {"kind": kind, "id": "message-or-tool-" + event["event_id"], "observer_id": observer,
                             "observed_at": observed_at or event["occurred_at"], "event": copy.deepcopy(event)}
        return event

    def test_inline_message_ack_requires_no_new_evidence_files(self):
        delivered = self.inline_evidence(self.event())
        accepted = self.inline_evidence(self.event(status="accepted"), observed_at=LATER)
        before = {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()}
        self.update([delivered, accepted], apply=False)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()})
        self.update([delivered, accepted])
        after = {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()}
        changed = {p for p in before if before[p] != after[p]}
        self.assertEqual(changed, {str(self.root_path)})
        self.assertEqual(set(after) - set(before), {str(self.f.run / "状态.json.task-update.lock")})
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["lifecycle_events"][accepted["event_id"]]["event"]["evidence"]["observed_at"], LATER)
        self.assertEqual(row["accepted_at"], TIME)
        self.assertNotIn("read_at", row)
        self.update([delivered, accepted])  # Exact replay keeps original observed and recorded facts.

    def test_inline_tool_evidence_and_embedded_entry_receipt(self):
        self.update([self.inline_evidence(self.event(), kind="tool"), self.inline_evidence(self.event(status="accepted"), kind="tool")])
        receipt = {"task_id": "t1", "agent_id": "agent1", "stage": "problem-family", "phase": "entry", "decision": "allow",
                   "ok": True, "epoch": 1, "root_sha256": self.digest(), "task_sha256": self.f.ref(self.f.path)["sha256"], "checked_at": TIME}
        event = self.inline_evidence(self.event("start_observed", runtime_id="runtime1", entry_receipt=receipt), kind="tool")
        self.update([event])
        self.update([event])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["entry_receipt"], receipt)
        self.assertNotIn("entry_receipt_ref", row)

    def test_historical_entry_root_survives_unrelated_root_update(self):
        self.update([self.event(), self.event(status="accepted")])
        receipt = {"task_id": "t1", "agent_id": "agent1", "stage": "problem-family", "phase": "entry", "decision": "allow",
                   "ok": True, "epoch": 1, "root_sha256": self.digest(), "task_sha256": self.f.ref(self.f.path)["sha256"], "checked_at": TIME}
        ref = self.f.put("历史入口.json", receipt)
        root = self.root()
        root["harness_control"]["unrelated_observation"] = {"task": "other", "message": "preserved"}
        self.root_path.write_text(json.dumps(root))
        self.assertNotEqual(receipt["root_sha256"], self.digest())
        self.update([self.inline_evidence(self.event("start_observed", runtime_id="runtime1", entry_receipt_ref=ref), kind="tool")])
        self.assertEqual(self.root()["harness_control"]["unrelated_observation"], {"task": "other", "message": "preserved"})

    def test_historical_entry_rejects_changed_epoch_task_permissions(self):
        self.update([self.event(), self.event(status="accepted")])
        receipt = {"task_id": "t1", "agent_id": "agent1", "stage": "problem-family", "phase": "entry", "decision": "allow",
                   "ok": True, "epoch": 1, "root_sha256": self.digest(), "task_sha256": self.f.ref(self.f.path)["sha256"], "checked_at": TIME}
        for field, value in (("epoch", 2), ("task_sha256", "0" * 64), ("agent_id", "wrong"), ("task_id", "wrong")):
            event = self.inline_evidence(self.event("start_observed", runtime_id="runtime1", entry_receipt=dict(receipt, **{field: value})))
            self.update([event], expected=1, diagnostic="entry_receipt_invalid")
        root = self.root()
        root["harness_control"]["permissions"]["denied_actions"].append("read_local")
        self.root_path.write_text(json.dumps(root))
        event = self.inline_evidence(self.event("start_observed", runtime_id="runtime1", entry_receipt=receipt))
        self.update([event], expected=1, diagnostic="entry_rejected")
        root["harness_control"]["permissions"]["denied_actions"] = []
        root["harness_control"]["epoch"] = 2
        self.root_path.write_text(json.dumps(root))
        self.update([event], expected=1, diagnostic="epoch_stale")

    def test_inline_evidence_binding_and_observation_time(self):
        event = self.inline_evidence(self.event())
        event["evidence"]["event"]["agent_id"] = "wrong"
        self.update([event], expected=1, diagnostic="evidence_mismatch")
        event = self.inline_evidence(self.event())
        event["evidence"]["observed_at"] = "2026-10-14T09:59:59Z"
        self.update([event], expected=1, diagnostic="event_order")
        for field in ("id", "observer_id", "observed_at"):
            event = self.inline_evidence(self.event())
            del event["evidence"][field]
            self.update([event], expected=1, diagnostic="event_evidence")
        event = self.inline_evidence(self.event())
        event["evidence"]["kind"] = "unlocatable"
        self.update([event], expected=1, diagnostic="event_evidence")

    def test_evidence_and_entry_alternatives_are_exclusive(self):
        event = self.event()
        inline = self.inline_evidence(event)
        inline["evidence_ref"] = event["evidence_ref"]
        self.update([inline], expected=1, diagnostic="event_evidence")
        event.pop("evidence_ref")
        self.update([event], expected=1, diagnostic="event_evidence")
        self.update([self.event()])
        receipt = {"task_id": "t1", "agent_id": "agent1", "stage": "problem-family", "phase": "entry", "decision": "allow",
                   "ok": True, "epoch": 1, "root_sha256": self.digest(), "task_sha256": self.f.ref(self.f.path)["sha256"], "checked_at": TIME}
        event = self.inline_evidence(self.event("start_observed", runtime_id="runtime1", entry_receipt=receipt,
                                               entry_receipt_ref=self.f.put("入口重复.json", receipt)))
        self.update([event], expected=1, diagnostic="entry_receipt_invalid")

    def test_actual_api_entry_receipt_to_start_without_snapshot_assembly(self):
        self.update([self.inline_evidence(self.event()), self.inline_evidence(self.event(status="accepted"))])
        code, result = mutation.gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 0, result)
        receipt = result["receipt"]
        root = self.root()
        root["harness_control"]["unrelated_observation"] = "does not invalidate the prior receipt"
        self.root_path.write_text(json.dumps(root))
        self.assertNotEqual(receipt["root_sha256"], self.digest())
        event = self.event("start_observed", runtime_id="actual-api-fixture-runtime", entry_receipt=receipt,
                           occurred_at=receipt["checked_at"])
        self.update([self.inline_evidence(event, kind="tool")])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["entry_receipt"], receipt)
        self.assertEqual(row["status"], "started")
        self.assertEqual(row["started_at"], event["occurred_at"])

    def test_actual_cli_entry_receipt_to_start_without_snapshot_assembly(self):
        self.update([self.inline_evidence(self.event()), self.inline_evidence(self.event(status="accepted"))])
        checked = subprocess.run([sys.executable, "-I", "-B", str(BASE / "脚本" / "任务校验.py"), "check",
                                  "--run", str(self.f.run), "--task", str(self.f.path), "--phase", "entry"], capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stdout)
        receipt = json.loads(checked.stdout)["receipt"]
        event = self.event("start_observed", runtime_id="actual-cli-fixture-runtime", entry_receipt=receipt,
                           occurred_at=receipt["checked_at"])
        event = self.inline_evidence(event, kind="tool")
        batch = self.f.put("实际校验串接事件.json", {"events": [event]})
        applied = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "apply", "--run", str(self.f.run),
                                  "--owner", "main", "--expected-root-sha", self.digest(),
                                  "--events", str(self.f.run / batch["path"])], capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stdout)
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["entry_receipt"], receipt)
        self.assertEqual(row["status"], "started")

    def source_start_event(self):
        self.f.source()
        self.f.write()
        self.update([self.inline_evidence(self.event()), self.inline_evidence(self.event(status="accepted"))])
        code, result = mutation.gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 0, result)
        receipt = result["receipt"]
        event = self.inline_evidence(self.event("start_observed", runtime_id="source-runtime", entry_receipt=receipt,
                                                 occurred_at=receipt["checked_at"]), kind="tool")
        root = self.root()
        ledger_ref = root["harness_control"]["source_ledger_ref"]
        return event, self.f.run / ledger_ref["path"]

    def test_nested_entry_source_ledger_change_is_rejected(self):
        event, ledger = self.source_start_event()
        original = mutation.gate.Checker.check
        before = self.root_path.read_bytes()
        def changed_after_entry(checker):
            result = original(checker)
            if checker.phase == "entry":
                ledger.write_bytes(ledger.read_bytes() + b" ")
            return result
        with mock.patch.object(mutation.gate.Checker, "check", changed_after_entry):
            self.update([event], expected=1, diagnostic="file_changed")
        self.assertEqual(before, self.root_path.read_bytes())
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "accepted")

    def test_nested_entry_source_dependency_is_rechecked_before_commit(self):
        event, ledger = self.source_start_event()
        contract_ref = json.loads(ledger.read_bytes())["contracts"]["c1"]
        contract = self.f.run / contract_ref["path"]
        original = mutation.prepare
        before = self.root_path.read_bytes()
        def changed_after_prepare(*args):
            result = original(*args)
            self.assertIn(contract.resolve(), result[0].snapshots)
            contract.write_bytes(contract.read_bytes() + b" ")
            return result
        with mock.patch.object(mutation, "prepare", changed_after_prepare):
            self.update([event], expected=1, diagnostic="file_changed")
        self.assertEqual(before, self.root_path.read_bytes())
        self.assertEqual(list(self.f.run.glob(".task-update-*")), [])

    def test_output_checkpoint_preserves_refs_and_known_missing_sections(self):
        self.f.record["checkpoint"]["missing_sections"] = ["外部接口未知"]
        self.f.write()
        self.start()
        output = self.output(read=False)
        event = self.event("output_observed", output_delivery=output)
        self.update([event])
        self.update([self.event("output_observed", output_delivery=output)])
        self.update([event])
        checkpoint = self.root()["harness_control"]["tasks"]["t1"]["checkpoint"]
        self.assertEqual(checkpoint["saved_refs"], [self.f.input, output["material_ref"]])
        self.assertEqual(checkpoint["missing_sections"], ["外部接口未知"])

    def test_full_cli_lifecycle_then_paused_checkpoint(self):
        self.adopt_science()
        root = self.root()
        ref = root["harness_control"]["tasks"].pop("t1")["ref"]
        self.root_path.write_text(json.dumps(root))

        def mutate(event):
            batch = self.f.put("CLI事件_%s.json" % event["event_id"], {"events": [event]})
            command = [sys.executable, "-I", "-B", str(SCRIPT), "apply", "--run", str(self.f.run), "--owner", "main",
                       "--expected-root-sha", self.digest(), "--events", str(self.f.run / batch["path"])]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout)
            return json.loads(result.stdout)

        def check(phase):
            command = [sys.executable, "-I", "-B", str(BASE / "脚本" / "任务校验.py"), "check",
                       "--run", str(self.f.run), "--task", str(self.f.path), "--phase", phase]
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout)
            return json.loads(result.stdout)

        mutate(self.inline_evidence(self.event("register", task_ref=ref)))
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["checkpoint"], {"saved_refs": [], "missing_sections": []})
        check("dispatch")
        mutate(self.inline_evidence(self.event(status="delivered")))
        mutate(self.inline_evidence(self.event(status="accepted")))
        receipt = check("entry")["receipt"]
        when = receipt["checked_at"]
        mutate(self.inline_evidence(self.event("start_observed", runtime_id="full-cli-runtime", entry_receipt=receipt,
                                               occurred_at=when), kind="tool"))
        output = self.output(at=when)
        mutate(self.inline_evidence(self.event("output_observed", output_delivery=output, occurred_at=when), observer="leader"))
        mutate(self.inline_evidence(self.event("complete", occurred_at=when)))
        result = subprocess.run([sys.executable, "-I", "-B", str(BASE / "脚本" / "任务校验.py"), "inspect",
                                 "--run", str(self.f.run), "--task", "t1"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["tasks"][0]["status"], "completed")
        self.assertFalse(summary["action_permission"])
        # Simulate a new user pause in this temporary run; task records need no manual repair.
        root = self.root()
        root["status"] = "paused_by_user"
        root["harness_control"]["paused"] = True
        self.root_path.write_text(json.dumps(root))
        result = check("checkpoint")
        self.assertIn("preserve_only", [d["code"] for d in result["diagnostics"]])
        checkpoint = self.root()["harness_control"]["tasks"]["t1"]["checkpoint"]
        self.assertEqual(checkpoint, {"saved_refs": [output["material_ref"]], "missing_sections": []})

    def test_paused_late_stop_without_registered_start_preserves_real_receipt(self):
        self.update([self.inline_evidence(self.event()), self.inline_evidence(self.event(status="accepted"))])
        code, result = mutation.gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 0, result)
        when = result["receipt"]["checked_at"]
        # The synthetic runtime was started after this real entry, but start was not recorded.
        root = self.root()
        root["status"] = "paused_by_user"
        root["harness_control"]["paused"] = True
        self.root_path.write_text(json.dumps(root))
        late_start = self.inline_evidence(self.event("start_observed", runtime_id="late-runtime", entry_receipt=result["receipt"],
                                                    occurred_at=when), kind="tool")
        self.update([late_start], expected=1, diagnostic="entry_rejected")
        failed = self.inline_evidence(self.event("terminal_observed", status="failed", failure_id="late-failure", occurred_at=when))
        self.update([failed])
        before_row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertNotIn("runtime_id", before_row)
        receipt = {"task_id": "t1", "agent_id": "agent1", "runtime_id": "late-runtime", "status": "stopped",
                   "success": True, "stopped_at": when}
        for changes in ({"message": "No task found"}, {"runtime_id": "wrong-runtime"}, {"task_id": "wrong-task"},
                        {"agent_id": "wrong-agent"}, {"success": False}):
            ref = self.f.put("迟报停止.json", dict(receipt, **changes))
            event = self.inline_evidence(self.event("terminal_observed", status="stopped", failure_id="late-failure",
                runtime_id="late-runtime", stop_receipt_ref=ref, occurred_at=when), kind="tool")
            self.update([event], expected=1, diagnostic="stop_receipt_invalid")
        ref = self.f.put("迟报停止.json", receipt)
        event = self.inline_evidence(self.event("terminal_observed", status="stopped", failure_id="late-failure",
            runtime_id="late-runtime", stop_receipt_ref=ref, occurred_at=when), kind="tool")
        self.update([event])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["runtime_id"], "late-runtime")
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertEqual(row["status"], "failed")
        self.assertNotIn("started_at", row)
        self.assertEqual(row["failed_at"], before_row["failed_at"])
        self.assertEqual(row["lifecycle_events"][failed["event_id"]], before_row["lifecycle_events"][failed["event_id"]])
        self.assertTrue(self.root()["harness_control"]["paused"])
        code, result = mutation.gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "run_paused")
        self.assertEqual(mutation.gate.check_task(self.f.run, self.f.path, "checkpoint")[0], 0)

    def test_late_stop_still_rejects_different_registered_runtime(self):
        self.start()
        receipt = {"task_id": "t1", "agent_id": "agent1", "runtime_id": "other-runtime", "status": "stopped",
                   "success": True, "stopped_at": TIME}
        event = self.event("terminal_observed", status="stopped", failure_id="f1", runtime_id="other-runtime",
                           stop_receipt_ref=self.f.put("其他运行时停止.json", receipt))
        self.update([event], expected=1, diagnostic="route_mismatch")

    def test_overwritten_saved_original_is_rejected_before_commit(self):
        self.f.record["checkpoint"]["missing_sections"] = ["仍缺外部接口"]
        self.f.write()
        self.start()
        old = self.output(read=False)
        self.update([self.event("output_observed", output_delivery=old)])
        path = self.f.run / old["material_ref"]["path"]
        original = path.read_bytes()
        path.write_text("# 被原路径覆写的新稿\n", encoding="utf-8")
        new = dict(old, version="v2", material_ref=self.f.ref(path))
        event = self.event("output_observed", output_delivery=new, occurred_at=LATER)
        self.update([event], apply=False, expected=1, diagnostic="hash_mismatch")
        self.update([event], expected=1, diagnostic="hash_mismatch")
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["checkpoint"]["saved_refs"], [self.f.input, old["material_ref"]])
        self.assertEqual(list(self.f.run.glob(".task-update-*")), [])
        # Preserve the old original and observe the new version at its own path.
        path.write_bytes(original)
        new = self.output("v2", read=False, at=LATER)
        self.update([self.event("output_observed", output_delivery=new, occurred_at=LATER)])
        self.update([self.event("complete", occurred_at=LATER)])
        root = self.root()
        root["status"] = "paused_by_user"
        root["harness_control"]["paused"] = True
        self.root_path.write_text(json.dumps(root))
        code, result = mutation.gate.check_task(self.f.run, self.f.path, "checkpoint")
        self.assertEqual(code, 0, result)
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["checkpoint"]["saved_refs"],
                         [self.f.input, old["material_ref"], new["material_ref"]])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["checkpoint"]["missing_sections"], ["仍缺外部接口"])

    def test_complete_then_real_stop_preserves_logical_completion(self):
        self.start()
        self.update([self.event("output_observed", output_delivery=self.output()), self.event("complete")])
        before = self.root()["harness_control"]["tasks"]["t1"]
        stopped = self.stop_event()
        stopped.pop("failure_id")
        self.update([self.evidence(stopped)])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertEqual(row["completed_at"], before["completed_at"])
        self.assertEqual(row["output_delivery"], before["output_delivery"])
        self.assertNotIn("failure_id", row)
        self.update([self.event()], expected=1, diagnostic="task_terminal")
        self.assertEqual(mutation.gate.check_task(self.f.run, self.f.path, "checkpoint")[0], 0)

    def test_failed_then_real_stop_preserves_failure_and_restrictions(self):
        self.f.record["refusal_actions"] = ["source_get"]
        self.f.write()
        self.start()
        failed = self.event("terminal_observed", status="failed", failure_id="f1")
        self.update([failed])
        before = self.root()
        self.update([self.stop_event()])
        after = self.root()
        row = after["harness_control"]["tasks"]["t1"]
        prior = before["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["runtime_state"], "stopped")
        for field in ("failure_id", "failed_at", "refusal_actions", "started_at"):
            self.assertEqual(row[field], prior[field])
        self.assertEqual(row["lifecycle_events"][failed["event_id"]], prior["lifecycle_events"][failed["event_id"]])
        for field in ("paused", "epoch", "permissions"):
            self.assertEqual(after["harness_control"][field], before["harness_control"][field])
        self.update([self.event()], expected=1, diagnostic="task_terminal")

    def test_normal_stop_needs_no_failure_identity(self):
        self.start()
        event = self.stop_event()
        event.pop("failure_id")
        event = self.evidence(event)
        self.update([event])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertNotIn("failure_id", row)
        self.assertNotIn("failed_at", row)

    def test_stop_omitted_failure_keeps_existing_identity_and_rejects_conflict(self):
        self.start()
        self.update([self.event("terminal_observed", status="failed", failure_id="f1")])
        event = self.stop_event()
        event["failure_id"] = "wrong-failure"
        self.update([self.evidence(event)], expected=1, diagnostic="event_conflict")
        event.pop("failure_id")
        self.update([self.evidence(event)])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertEqual(row["failure_id"], "f1")
        self.assertEqual(row["failed_at"], TIME)

    def test_failure_event_still_requires_failure_identity(self):
        event = self.event("terminal_observed", status="failed")
        self.update([event], expected=1, diagnostic="type")

    def timed_stop_event(self, stopped_at, observed_at, name="真实时序停止.json"):
        receipt = {"task_id": "t1", "agent_id": "agent1", "runtime_id": "runtime1", "status": "stopped",
                   "success": True, "stopped_at": stopped_at}
        event = self.event("terminal_observed", status="stopped", runtime_id="runtime1", occurred_at=stopped_at,
                           stop_receipt_ref=self.f.put(name, receipt))
        return self.inline_evidence(event, kind="tool", observed_at=observed_at)

    def test_late_earlier_stop_preserves_later_scientific_read_and_completion(self):
        self.adopt_science()
        self.start()
        output = self.output()
        output["read_at"] = LATER
        self.update([self.event("output_observed", output_delivery=output, occurred_at=LATER), self.event("complete", occurred_at=LATER)])
        stop_time = "2026-10-14T10:00:10+00:00"
        observed_time = "2026-10-14T10:00:40+00:00"
        event = self.timed_stop_event(stop_time, observed_time)
        self.update([event])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertEqual(row["stopped_at"], stop_time)
        self.assertEqual(row["completed_at"], LATER)
        self.assertEqual(row["output_delivery"]["read_at"], LATER)
        self.assertEqual(row["lifecycle_events"][event["event_id"]]["event"]["evidence"]["observed_at"], observed_time)
        self.assertNotIn("failure_id", row)
        self.assertFalse(self.update([event])["changed"])
        conflict = self.timed_stop_event("2026-10-14T10:00:11+00:00", observed_time, name="冲突停止时序.json")
        self.update([conflict], expected=1, diagnostic="stop_receipt_invalid")

    def test_late_earlier_stop_preserves_later_failure_fact(self):
        self.start()
        self.update([self.event("terminal_observed", status="failed", failure_id="f1", occurred_at=LATER)])
        event = self.timed_stop_event("2026-10-14T10:00:10+00:00", "2026-10-14T10:00:40+00:00")
        self.update([event])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertEqual(row["failure_id"], "f1")
        self.assertEqual(row["failed_at"], LATER)
        self.assertEqual(row["stopped_at"], "2026-10-14T10:00:10+00:00")

    def test_runtime_stop_before_known_start_is_rejected(self):
        self.start()
        event = self.timed_stop_event("2026-10-14T09:59:59+00:00", "2026-10-14T10:00:40+00:00")
        self.update([event], expected=1, diagnostic="stop_receipt_invalid")

    def historical_cross_directory_output(self, *, registered=True, legacy=False):
        self.adopt_science()
        self.start()
        material = self.f.run / "小组" / "历史归档" / "科学稿.md"
        material.parent.mkdir(parents=True, exist_ok=True)
        material.write_text("# 历史判断\n合成原件。\n", encoding="utf-8")
        output = {"material_ref": self.f.ref(material), "version": "v1", "formed_at": TIME,
                  "delivered_at": TIME, "archived_at": TIME}
        if registered:
            root = self.root()
            row = root["harness_control"]["tasks"]["t1"]
            row["checkpoint"]["saved_refs"].append(output["material_ref"])
            if legacy:
                row.update(status="completed", material_ref=output["material_ref"], formed_at=TIME,
                           delivered_at=TIME, completed_at=TIME, version="v1")
            else:
                row["output_delivery"] = copy.deepcopy(output)
            self.root_path.write_text(json.dumps(root))
        return output

    def test_historical_cross_directory_same_original_can_append_actual_read(self):
        output = self.historical_cross_directory_output()
        original = self.root()["harness_control"]["tasks"]["t1"]["output_delivery"]
        read = dict(output, read_at=LATER, reader_id="leader", read_version="v1", read_material_ref=output["material_ref"])
        self.update([self.event("output_observed", output_delivery=read, occurred_at=LATER)])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual({k: row["output_delivery"][k] for k in original}, original)
        self.assertEqual(row["output_delivery"]["read_at"], LATER)
        self.update([self.event("complete", occurred_at=LATER)])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "completed")

    def test_historical_cross_directory_legacy_original_can_append_actual_read(self):
        output = self.historical_cross_directory_output(legacy=True)
        original = self.root()["harness_control"]["tasks"]["t1"]
        read = dict(output, read_at=LATER, reader_id="leader", read_version="v1", read_material_ref=output["material_ref"])
        self.update([self.event("output_observed", output_delivery=read, occurred_at=LATER)])
        row = self.root()["harness_control"]["tasks"]["t1"]
        for field in ("status", "material_ref", "version", "formed_at", "delivered_at", "completed_at", "checkpoint"):
            self.assertEqual(row[field], original[field])
        self.assertEqual(row["output_delivery"], read)

    def test_new_cross_directory_output_remains_rejected(self):
        output = self.historical_cross_directory_output(registered=False)
        self.update([self.event("output_observed", output_delivery=output)], expected=1, diagnostic="output_escape")
        self.assertNotIn("output_delivery", self.root()["harness_control"]["tasks"]["t1"])

    def test_historical_cross_directory_changed_original_or_version_is_not_preserved(self):
        output = self.historical_cross_directory_output()
        other = self.f.run / "小组" / "历史归档" / "另一原件.md"
        other.write_text("# 另一判断\n合成新版。\n", encoding="utf-8")
        changed = dict(output, material_ref=self.f.ref(other))
        self.update([self.event("output_observed", output_delivery=changed)], expected=1, diagnostic="output_escape")
        self.update([self.event("output_observed", output_delivery=dict(output, version="v2"))], expected=1, diagnostic="output_version")
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["output_delivery"], output)

    def test_historical_original_cannot_hide_cross_directory_replacement_in_one_batch(self):
        output = self.historical_cross_directory_output()
        other = self.f.run / "小组" / "历史归档" / "临时换稿.md"
        other.write_text("# 换稿\n合成内容。\n", encoding="utf-8")
        changed = dict(output, material_ref=self.f.ref(other))
        self.update([self.event("output_observed", output_delivery=changed),
                     self.event("output_observed", output_delivery=output)], expected=1, diagnostic="output_escape")
        in_directory = self.output(read=False)
        self.update([self.event("output_observed", output_delivery=in_directory),
                     self.event("output_observed", output_delivery=output)], expected=1, diagnostic="output_escape")
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["output_delivery"], output)

    def test_historical_original_completion_still_requires_actual_read_and_reader(self):
        output = self.historical_cross_directory_output()
        self.update([self.event("complete", occurred_at=LATER)], expected=1, diagnostic="scientific_delivery_missing")
        wrong = dict(output, read_at=LATER, reader_id="other", read_version="v1", read_material_ref=output["material_ref"])
        self.update([self.event("output_observed", output_delivery=wrong, occurred_at=LATER)], expected=1, diagnostic="output_reader")
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "started")

    def legacy_completed(self):
        output = self.output(read=False)
        root = self.root()
        row = root["harness_control"]["tasks"]["t1"]
        row.update(status="completed", material_ref=output["material_ref"], formed_at=TIME, delivered_at=TIME,
                   completed_at=TIME, version="v1", checkpoint={"saved_refs": [output["material_ref"]], "missing_sections": ["旧未知保留"]})
        self.root_path.write_text(json.dumps(root))
        return output

    def test_audit_legacy_completed_cannot_declare_another_current_original(self):
        legacy = self.legacy_completed()
        other = self.output("v2", read=False)
        self.update([self.event("output_observed", output_delivery=other)], expected=1, diagnostic="output_conflict")
        changed = dict(legacy, version="v2")
        self.update([self.event("output_observed", output_delivery=changed)], expected=1, diagnostic="output_conflict")
        changed = dict(legacy, formed_at="2026-10-14T09:59:00Z")
        self.update([self.event("output_observed", output_delivery=changed)], expected=1, diagnostic="output_conflict")
        self.assertNotIn("output_delivery", self.root()["harness_control"]["tasks"]["t1"])

    def test_audit_legacy_same_original_can_append_actual_read(self):
        legacy = self.legacy_completed()
        before = self.root()["harness_control"]["tasks"]["t1"]
        output = dict(legacy, read_at=LATER, reader_id="leader", read_version="v1", read_material_ref=legacy["material_ref"])
        self.update([self.event("output_observed", output_delivery=output, occurred_at=LATER)])
        after = self.root()["harness_control"]["tasks"]["t1"]
        for field in ("status", "material_ref", "formed_at", "delivered_at", "completed_at", "version", "checkpoint"):
            self.assertEqual(after[field], before[field])
        self.assertEqual(after["output_delivery"], output)

    def test_audit_legacy_unknown_contract_holds_without_guessing_saved_ref(self):
        output = self.output(read=False)
        root = self.root()
        row = root["harness_control"]["tasks"]["t1"]
        row.update(status="completed", checkpoint={"saved_refs": [output["material_ref"]], "missing_sections": []})
        self.root_path.write_text(json.dumps(root))
        self.update([self.event("output_observed", output_delivery=output)], expected=2, diagnostic="legacy_output_unknown")

    def test_audit_delivered_only_cannot_enter_or_start_even_in_reducer(self):
        self.update([self.event()])
        code, result = mutation.gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 1, result)
        self.assertNotIn("receipt", result)
        self.assertEqual(result["diagnostics"][-1]["code"], "acceptance_fact_missing")
        receipt = {"task_id": "t1", "agent_id": "agent1", "stage": "problem-family", "phase": "entry", "decision": "allow",
                   "ok": True, "epoch": 1, "root_sha256": self.digest(), "task_sha256": self.f.ref(self.f.path)["sha256"], "checked_at": TIME}
        event = self.event("start_observed", runtime_id="r1", entry_receipt=receipt)
        self.update([event], expected=1, diagnostic="entry_rejected")
        root = self.root()
        with self.assertRaises(mutation.gate.GateError) as caught:
            mutation.reduce_events(root, [event], {event["event_id"]: {}}, TIME)
        self.assertEqual(caught.exception.code, "acceptance_fact_missing")

    def binding_start_event(self):
        link = self.f.run / "输入别名.json"
        alternate = self.f.run / "同字节输入.json"
        alternate.write_bytes((self.f.run / self.f.input["path"]).read_bytes())
        link.symlink_to(self.f.run / self.f.input["path"])
        self.f.task["input_refs"] = [{"path": link.name, "sha256": self.f.input["sha256"]}]
        self.f.write()
        self.update([self.event(), self.event(status="accepted")])
        code, result = mutation.gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 0, result)
        receipt = result["receipt"]
        event = self.inline_evidence(self.event("start_observed", runtime_id="binding-runtime", entry_receipt=receipt,
                                               occurred_at=receipt["checked_at"]), kind="tool")
        return event, link, alternate

    def test_audit_fixed_link_writer_entry_is_usable(self):
        event, _, _ = self.binding_start_event()
        self.update([event])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "started")

    def test_audit_nested_entry_link_binding_is_merged(self):
        event, link, alternate = self.binding_start_event()
        original = mutation.gate.Checker.check
        def swapped(checker):
            result = original(checker)
            if checker.phase == "entry":
                link.unlink(); link.symlink_to(alternate)
            return result
        with mock.patch.object(mutation.gate.Checker, "check", swapped):
            self.update([event], expected=1, diagnostic="path_changed")

    def test_audit_link_retarget_after_prepare_is_rejected(self):
        event, link, alternate = self.binding_start_event()
        original = mutation.prepare
        def swapped(*args):
            result = original(*args)
            link.unlink(); link.symlink_to(alternate)
            return result
        with mock.patch.object(mutation, "prepare", swapped):
            self.update([event], expected=1, diagnostic="path_changed")
        self.assertEqual(list(self.f.run.glob(".task-update-*")), [])

    def test_audit_link_retarget_before_replace_is_rejected(self):
        event, link, alternate = self.binding_start_event()
        original = mutation.os.fsync
        changed = []
        def swapped(fd):
            result = original(fd)
            if not changed:
                link.unlink(); link.symlink_to(alternate)
                changed.append(True)
            return result
        with mock.patch.object(mutation.os, "fsync", swapped):
            self.update([event], expected=1, diagnostic="path_changed")
        self.assertEqual(list(self.f.run.glob(".task-update-*")), [])

    def test_audit_writer_keeps_raw_run_alias_through_commit(self):
        event = self.event()
        alias = Path(self.f.temp.name) / "运行别名"
        other = Path(self.f.temp.name) / "另一位置"
        other.mkdir()
        alias.symlink_to(self.f.run, target_is_directory=True)
        before = self.root_path.read_bytes()
        original = mutation.prepare
        def swapped(*args):
            self.assertEqual(Path(args[0]), alias)
            result = original(*args)
            alias.unlink(); alias.symlink_to(other, target_is_directory=True)
            return result
        with mock.patch.object(mutation, "prepare", swapped):
            code, result = mutation.update_tasks(alias, "main", self.digest(), [event], apply=True)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][0]["code"], "path_changed")
        self.assertEqual(self.root_path.read_bytes(), before)
        self.assertEqual(list(self.f.run.glob(".task-update-*")), [])

    def test_audit_incomplete_existing_structured_output_holds(self):
        output = self.legacy_completed()
        root = self.root()
        root["harness_control"]["tasks"]["t1"]["output_delivery"] = {}
        self.root_path.write_text(json.dumps(root))
        self.update([self.event("output_observed", output_delivery=output)], expected=2, diagnostic="output_contract_unknown")

    def test_audit_nested_only_source_alias_binding_is_merged(self):
        event, ledger = self.source_start_event()
        link = self.f.run / "来源账别名.json"
        alternate = self.f.run / "同字节来源账.json"
        alternate.write_bytes(ledger.read_bytes())
        link.symlink_to(ledger)
        root = self.root()
        root["harness_control"]["source_ledger_ref"] = {"path": link.name, "sha256": self.f.ref(ledger)["sha256"]}
        self.root_path.write_text(json.dumps(root))
        original = mutation.gate.Checker.check
        def swapped(checker):
            result = original(checker)
            if checker.phase == "entry":
                link.unlink(); link.symlink_to(alternate)
            return result
        with mock.patch.object(mutation.gate.Checker, "check", swapped):
            self.update([event], expected=1, diagnostic="path_changed")

    def initial_target_fixture(self):
        self.update([self.inline_evidence(self.event()), self.inline_evidence(self.event(status="accepted"))])
        code, result = mutation.gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 0, result)
        receipt = result["receipt"]
        event = self.inline_evidence(self.event("start_observed", runtime_id="pinned-target-runtime", entry_receipt=receipt,
                                               occurred_at=receipt["checked_at"]), kind="tool")
        other = Path(self.f.temp.name) / "另一父目录" / self.f.run.name
        other.parent.mkdir()
        shutil.copytree(self.f.run, other, symlinks=True)
        alias = Path(self.f.temp.name) / "首次运行入口"
        alias.symlink_to(self.f.run, target_is_directory=True)
        self.assertEqual((other / "状态.json").read_bytes(), self.root_path.read_bytes())
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "accepted")
        return event, alias, other

    def test_initial_target_fork_before_prepare_rejects_preview_and_apply(self):
        event, alias, other = self.initial_target_fixture()
        first_bytes = self.root_path.read_bytes()
        second_bytes = (other / "状态.json").read_bytes()
        original = mutation.prepare
        prepared_targets = []
        def switched_after_initial_resolution(*args):
            self.assertEqual(Path(args[0]), alias)
            alias.unlink(); alias.symlink_to(other, target_is_directory=True)
            result = original(*args)
            # B is independently valid, with the same accepted root bytes and native entry receipt.
            self.assertEqual(result[0].run, other.resolve())
            self.assertEqual(result[2]["harness_control"]["tasks"]["t1"]["status"], "started")
            prepared_targets.append(result[0].run)
            return result
        for apply in (False, True):
            with self.subTest(apply=apply):
                alias.unlink(); alias.symlink_to(self.f.run, target_is_directory=True)
                with mock.patch.object(mutation, "prepare", switched_after_initial_resolution):
                    code, result = mutation.update_tasks(alias, "main", self.digest(), [event], apply=apply)
                self.assertEqual(code, 1, result)
                self.assertEqual(result["diagnostics"][0]["code"], "path_changed")
                self.assertEqual(self.root_path.read_bytes(), first_bytes)
                self.assertEqual((other / "状态.json").read_bytes(), second_bytes)
                self.assertEqual(list(self.f.run.glob(".task-update-*")), [])
                self.assertEqual(list(other.glob(".task-update-*")), [])
        self.assertEqual(len(prepared_targets), 2)

    def test_initial_target_fixed_run_link_preview_and_apply_remain_valid(self):
        event, alias, other = self.initial_target_fixture()
        first_bytes = self.root_path.read_bytes()
        second_bytes = (other / "状态.json").read_bytes()
        code, result = mutation.update_tasks(alias, "main", self.digest(), [event])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["mode"], "preview")
        self.assertFalse(result["applied"])
        self.assertEqual(self.root_path.read_bytes(), first_bytes)
        self.assertEqual((other / "状态.json").read_bytes(), second_bytes)
        code, result = mutation.update_tasks(alias, "main", self.digest(), [event], apply=True)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["applied"])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "started")
        self.assertEqual((other / "状态.json").read_bytes(), second_bytes)
        self.assertEqual(alias.resolve(), self.f.run.resolve())

    def test_root_leaf_link_rejects_preview_changed_apply_and_replay(self):
        event = self.event()
        target = self.f.run / "根原件.json"
        for replay in (False, True):
            if replay:
                self.update([event])
            self.root_path.rename(target)
            self.root_path.symlink_to(target.name)
            before = target.read_bytes()
            for apply in (False, True):
                with self.subTest(replay=replay, apply=apply):
                    code, result = mutation.update_tasks(self.f.run, "main", self.digest(), [event], apply=apply)
                    self.assertEqual(code, 1, result)
                    self.assertEqual(result["diagnostics"][0]["code"], "root_symlink")
                    self.assertEqual(target.read_bytes(), before)
                    self.assertTrue(self.root_path.is_symlink())
                    self.assertEqual(list(self.f.run.glob(".task-update-*")), [])
            self.root_path.unlink(); target.rename(self.root_path)

    def test_directory_alias_keeps_preview_apply_and_replay_valid(self):
        event = self.event()
        alias = Path(self.f.temp.name) / "正常运行入口"
        alias.symlink_to(self.f.run, target_is_directory=True)
        before = self.root_path.read_bytes()
        for apply, changed in ((False, True), (True, True), (True, False)):
            code, result = mutation.update_tasks(alias, "main", self.digest(), [event], apply=apply)
            self.assertEqual(code, 0, result)
            self.assertEqual(result["changed"], changed)
            self.assertEqual(result["applied"], apply and changed)
            if not apply:
                self.assertEqual(self.root_path.read_bytes(), before)
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "delivered")

    def test_evidence_loop_at_recheck_is_structured_and_root_unchanged(self):
        event = self.event()
        link = self.f.run / "循环证据.json"
        link.symlink_to(event["evidence_ref"]["path"])
        event["evidence_ref"] = {"path": link.name, "sha256": event["evidence_ref"]["sha256"]}
        before = self.root_path.read_bytes()
        original = mutation.gate.Checker.recheck
        def changed(checker, *args, **kwargs):
            link.unlink(); link.symlink_to(link.name)
            return original(checker, *args, **kwargs)
        with mock.patch.object(mutation.gate.Checker, "recheck", changed):
            code, result = mutation.update_tasks(self.f.run, "main", self.digest(), [event], apply=True)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][0]["code"], "path_invalid")
        self.assertEqual(self.root_path.read_bytes(), before)
        ref = self.f.put("循环事件.json", {"events": [event]})
        proc = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "apply", "--run", str(self.f.run),
                               "--owner", "main", "--expected-root-sha", self.digest(), "--events",
                               str(self.f.run / ref["path"])], capture_output=True)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(json.loads(proc.stdout)["diagnostics"][0]["code"], "path_invalid")
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(self.root_path.read_bytes(), before)
        self.assertEqual(list(self.f.run.glob(".task-update-*")), [])

    def test_run_entry_loop_is_structured_before_lock(self):
        event = self.event()
        alias = Path(self.f.temp.name) / "循环运行入口"
        alias.symlink_to(alias.name)
        before = self.root_path.read_bytes()
        for apply in (False, True):
            code, result = mutation.update_tasks(alias, "main", self.digest(), [event], apply=apply)
            self.assertEqual(code, 1, result)
            self.assertEqual(result["diagnostics"][0]["code"], "path_invalid")
            self.assertEqual(self.root_path.read_bytes(), before)

    def test_legacy_stopped_can_append_missing_real_runtime_receipt(self):
        root = self.root()
        root["harness_control"]["tasks"]["t1"].update(status="stopped", stopped_at_utc=TIME,
                                                      stop_reason="synthetic legacy administrative label")
        self.root_path.write_text(json.dumps(root))
        event = self.stop_event()
        event.pop("failure_id")
        self.update([self.evidence(event)])
        row = self.root()["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["runtime_id"], "runtime1")
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertEqual(row["stopped_at_utc"], TIME)
        self.assertNotIn("failure_id", row)
        self.assertNotIn("started_at", row)
        self.update([self.evidence(event)])

    def test_old_epoch_real_stop_preserves_science_with_changed_historical_norm(self):
        self.adopt_science()
        self.start()
        output = self.output()
        self.update([self.event("output_observed", output_delivery=output)])
        self.update([self.event("terminal_observed", status="failed", failure_id="f1")])
        root = self.root()
        root["status"] = "paused_by_user"
        root["harness_control"].update(epoch=2, paused=True)
        self.root_path.write_text(json.dumps(root))
        before = self.root()
        (self.f.run / self.f.role["path"]).write_text('{"role":"updated norm"}')
        (self.f.run / self.f.task["scientific_work_ref"]["path"]).write_text('{"scientific_work":"updated norm"}')
        self.update([self.stop_event()])
        after = self.root()
        row = after["harness_control"]["tasks"]["t1"]
        prior = before["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["failure_id"], "f1")
        self.assertEqual(row["output_delivery"], output)
        self.assertEqual(row["checkpoint"], prior["checkpoint"])
        self.assertEqual(row["ref"], prior["ref"])
        self.assertEqual(row["runtime_state"], "stopped")
        self.assertTrue(after["harness_control"]["paused"])
        self.assertEqual(after["harness_control"]["epoch"], 2)
        self.assertEqual(after["harness_control"]["source_ledger_ref"], before["harness_control"]["source_ledger_ref"])
        for kind, fields in (("input_observed", {}), ("complete", {}),
                             ("terminal_observed", {"status": "failed", "failure_id": "f1"})):
            self.update([self.event(kind, **fields)], expected=1, diagnostic="epoch_stale")

    def test_old_epoch_real_stop_checks_runtime_owner_cas_and_preserved_outputs(self):
        self.start()
        root = self.root()
        root["harness_control"]["epoch"] = 2
        self.root_path.write_text(json.dumps(root))
        self.update([self.stop_event(runtime_id="other")], expected=1, diagnostic="stop_receipt_invalid")
        event = self.stop_event()
        event["runtime_id"] = "other"
        self.update([self.evidence(event)], expected=1, diagnostic="route_mismatch")
        self.update([self.stop_event()], owner="other", expected=1, diagnostic="owner_mismatch")
        self.update([self.stop_event()], digest="0" * 64, expected=1, diagnostic="cas_conflict")
        self.update([self.stop_event(message="No task found")], expected=1, diagnostic="stop_receipt_invalid")
        self.update([self.stop_event()])
        root = self.root()
        root["harness_control"]["tasks"]["t1"]["stop_receipt_ref"] = self.f.put("既存停止.json", {
            "task_id": "t1", "agent_id": "agent1", "runtime_id": "runtime1", "status": "stopped",
            "success": True, "stopped_at": TIME})
        self.root_path.write_text(json.dumps(root))
        self.update([self.stop_event()], expected=1, diagnostic="event_conflict")

    def test_future_epoch_real_stop_and_changed_envelope_are_rejected(self):
        self.f.task["epoch"] = 2
        self.f.write()
        self.update([self.stop_event()], expected=1, diagnostic="epoch_stale")
        self.f.task["epoch"] = 1
        self.f.write()
        self.f.path.write_bytes(self.f.path.read_bytes() + b" ")
        self.update([self.stop_event()], expected=1, diagnostic="hash_mismatch")

    def test_old_epoch_stop_does_not_resolve_failed_logical_task(self):
        self.start()
        self.update([self.event("terminal_observed", status="failed", failure_id="f1")])
        root = self.root()
        root["harness_control"]["epoch"] = 2
        self.root_path.write_text(json.dumps(root))
        self.update([self.stop_event()])
        root = self.root()
        self.assertEqual(root["harness_control"]["tasks"]["t1"]["status"], "failed")
        self.assertEqual(root["harness_control"]["tasks"]["t1"]["failure_id"], "f1")
        task = copy.deepcopy(self.f.task)
        task.update(task_id="next", epoch=2)
        ref = self.f.put("停止后新任务.json", task)
        root["harness_control"]["tasks"]["next"] = {"ref": ref, "agent_id": "agent1", "stage": task["stage"], "status": "registered"}
        self.root_path.write_text(json.dumps(root))
        code, result = mutation.gate.check_task(self.f.run, ref["path"], "dispatch")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "recovery_missing")

    def test_cancel_unstarted_preview_and_apply_preserve_original_state(self):
        event = self.cancellation_event()
        before = self.root()
        files = {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()}
        result = self.update([event], apply=False)
        self.assertFalse(result["action_permission"])
        self.assertEqual(files, {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()})
        self.update([event])
        after = self.root()
        row = after["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "stopped")
        self.assertEqual(row["unstarted_cancellation"], {"receipt_ref": event["unstarted_receipt_ref"], "cancelled_at": LATER})
        self.assertNotIn("runtime_id", row)
        self.assertNotIn("runtime_state", row)
        self.assertNotIn("stop_receipt_ref", row)
        after["harness_control"]["tasks"]["t1"].pop("unstarted_cancellation")
        after["harness_control"]["tasks"]["t1"].pop("lifecycle_events")
        self.assertEqual(after, before)

    def test_cancel_unstarted_registered_label_is_not_a_receipt(self):
        event = self.inline_evidence(self.event("cancel_unstarted", unstarted_receipt_ref=self.f.input), observer="main")
        self.update([event], expected=1, diagnostic="unstarted_receipt_invalid")
        event = self.cancellation_event(status="registered")
        self.update([event])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "registered")
        self.assertEqual(mutation.gate.check_task(self.f.run, self.f.path, "dispatch")[1]["diagnostics"][-1]["code"], "task_cancelled")

    def test_cancel_unstarted_replay_is_idempotent_and_marker_is_immutable(self):
        event = self.cancellation_event()
        self.update([event])
        original = self.root_path.read_bytes()
        self.update([event])
        self.assertEqual(original, self.root_path.read_bytes())
        changed = copy.deepcopy(event)
        changed["event_id"] = "another-cancellation"
        changed["occurred_at"] = "2026-10-14T10:00:31+00:00"
        self.update([self.inline_evidence(changed, observer="main")], expected=1, diagnostic="event_conflict")

    def test_cancel_unstarted_old_epoch_and_old_norm_are_administrative_only(self):
        self.f.control["epoch"] = 2
        self.f.control["paused"] = True
        self.f.root["status"] = "paused_by_user"
        self.f.record["checkpoint"]["saved_refs"] = []
        self.f.task["scientific_work_ref"] = self.f.input
        self.f.write()
        event = self.cancellation_event()
        before = self.root()
        (self.f.run / self.f.role["path"]).write_text('{"role":"new norm"}')
        (self.f.run / self.f.input["path"]).write_text('{"input":"new norm"}')
        self.update([event])
        after = self.root()
        self.assertEqual(after["harness_control"]["epoch"], 2)
        self.assertTrue(after["harness_control"]["paused"])
        self.assertEqual(after["harness_control"]["tasks"]["t1"]["ref"], before["harness_control"]["tasks"]["t1"]["ref"])
        self.assertEqual(json.loads(self.f.path.read_bytes())["epoch"], 1)
        entry = self.inline_evidence(self.event(occurred_at=LATER), observer="main")
        self.update([entry], expected=1, diagnostic="epoch_stale")

    def test_cancel_unstarted_receipt_bindings_are_exact(self):
        event = self.cancellation_event()
        receipt = json.loads((self.f.run / event["unstarted_receipt_ref"]["path"]).read_bytes())
        for key, value in (("task_id", "wrong"), ("agent_id", "wrong"), ("stage", "wrong"),
                           ("run_id", "wrong"), ("task_sha256", "0" * 64), ("epoch", 2),
                           ("confirmed_by", "observer"), ("status", "stopped"), ("success", 1),
                           ("delivery_enqueued", True), ("delivered", True), ("accepted", True),
                           ("started", True), ("pending_delivery", True), ("source_actions_executed", False)):
            with self.subTest(key=key):
                bad = dict(receipt, **{key: value})
                self.update([self.cancellation_receipt(event, bad)], expected=1, diagnostic="unstarted_receipt_invalid")
        for key in ("pending_delivery", "observation", "dispatch_check_report"):
            bad = copy.deepcopy(receipt)
            bad.pop(key)
            self.update([self.cancellation_receipt(event, bad)], expected=1, diagnostic="unstarted_receipt_invalid")

    def test_cancel_unstarted_rejects_known_execution_and_output_conflicts(self):
        event = self.cancellation_event()
        original = self.root()
        changes = ({"delivery_enqueued_at": TIME}, {"delivered_at": TIME}, {"accepted_at": TIME},
                   {"delivery_enqueued": True}, {"delivered": True}, {"accepted": True}, {"started": True},
                   {"pending_delivery": True}, {"delivery_enqueued": 0},
                   {"started_at": TIME}, {"runtime_id": "runtime1"}, {"runtime_state": "running"},
                   {"entry_receipt": {}}, {"stop_receipt_ref": self.f.input}, {"output_delivery": {}},
                   {"actor_started_or_delivered": True}, {"source_actions_executed": 1},
                   {"action_used": {"read_local": 1}}, {"failure_id": "f1"},
                   {"material_ref": self.f.input}, {"lifecycle_events": {"old": {"event": {"type": "input_observed"}}}})
        for fields in changes:
            with self.subTest(fields=fields):
                root = copy.deepcopy(original)
                root["harness_control"]["tasks"]["t1"].update(fields)
                self.root_path.write_text(json.dumps(root))
                self.update([event], expected=1, diagnostic="unstarted_conflict")

    def test_cancel_unstarted_requires_failed_dispatch_and_real_owner_observation(self):
        event = self.cancellation_event()
        receipt = json.loads((self.f.run / event["unstarted_receipt_ref"]["path"]).read_bytes())
        bad = copy.deepcopy(receipt)
        bad["dispatch_check_report"]["decision"] = "allow"
        self.update([self.cancellation_receipt(event, bad)], expected=1, diagnostic="unstarted_receipt_invalid")
        bad = copy.deepcopy(receipt)
        bad["observation"]["id"] = "No task found"
        self.update([self.cancellation_receipt(event, bad)], expected=1, diagnostic="unstarted_receipt_invalid")
        bad = copy.deepcopy(receipt)
        bad["observation"]["observer_id"] = "other"
        self.update([self.cancellation_receipt(event, bad)], expected=1, diagnostic="unstarted_receipt_invalid")
        bad = copy.deepcopy(receipt)
        bad["observation"]["observed_at"] = TIME
        self.update([self.cancellation_receipt(event, bad)], expected=1, diagnostic="event_order")
        self.update([self.inline_evidence(event)], expected=1, diagnostic="unstarted_observer_mismatch")
        self.update([event], owner="other", expected=1, diagnostic="owner_mismatch")

    def test_cancel_unstarted_cas_receipt_and_saved_material_are_rechecked(self):
        event = self.cancellation_event()
        self.update([event], digest="0" * 64, expected=1, diagnostic="cas_conflict")
        original = mutation.gate.Checker.recheck
        proof = self.f.run / event["unstarted_receipt_ref"]["path"]
        def changed(checker, *args, **kwargs):
            proof.write_bytes(proof.read_bytes() + b" ")
            return original(checker, *args, **kwargs)
        with mock.patch.object(mutation.gate.Checker, "recheck", changed):
            self.update([event], apply=False, expected=1, diagnostic="file_changed")
        event = self.cancellation_event()
        (self.f.run / self.f.input["path"]).write_bytes(b"overwritten saved material")
        self.update([event], expected=1, diagnostic="hash_mismatch")

    def test_cancel_unstarted_does_not_revive_task_and_allows_checked_role_reuse(self):
        event = self.cancellation_event()
        self.update([event])
        self.update([self.event(occurred_at=LATER)], expected=1, diagnostic="task_cancelled")
        for phase in ("dispatch", "entry"):
            code, result = mutation.gate.check_task(self.f.run, self.f.path, phase)
            self.assertEqual(code, 1, result)
            self.assertEqual(result["diagnostics"][-1]["code"], "task_cancelled")
        self.assertEqual(mutation.gate.check_task(self.f.run, self.f.path, "checkpoint")[0], 0)
        task = copy.deepcopy(self.f.task)
        task["task_id"] = "next"
        ref = self.f.put("后续任务.json", task)
        root = self.root()
        root["harness_control"]["tasks"]["next"] = {"ref": ref, "agent_id": "agent1", "stage": task["stage"], "status": "registered"}
        self.root_path.write_text(json.dumps(root))
        code, result = mutation.gate.check_task(self.f.run, ref["path"], "dispatch")
        self.assertEqual(code, 0, result)
        root["harness_control"]["tasks"]["t1"]["refusal_actions"] = ["read_local"]
        self.root_path.write_text(json.dumps(root))
        code, result = mutation.gate.check_task(self.f.run, ref["path"], "dispatch")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "permission_denied")

    def test_cancel_unstarted_future_epoch_and_event_fields_are_rejected(self):
        self.f.control["epoch"] = 2
        self.f.task["epoch"] = 3
        self.f.write()
        event = self.cancellation_event()
        self.update([event], expected=1, diagnostic="epoch_stale")
        bad = dict(event, runtime_id="invented")
        self.update([self.inline_evidence(bad, observer="main")], expected=1, diagnostic="event_fields")

    def test_cancel_unstarted_cli_uses_existing_preview_and_apply(self):
        event = self.cancellation_event()
        ref = self.f.put("撤销事件.json", {"events": [event]})
        args = [sys.executable, "-I", "-B", str(SCRIPT), "--run", str(self.f.run), "--owner", "main",
                "--expected-root-sha", self.digest(), "--events", str(self.f.run / ref["path"])]
        before = self.root_path.read_bytes()
        preview = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stdout)
        self.assertEqual(json.loads(preview.stdout)["mode"], "preview")
        self.assertEqual(before, self.root_path.read_bytes())
        applied = subprocess.run(args[:4] + ["apply"] + args[4:], capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stdout)
        self.assertTrue(json.loads(applied.stdout)["applied"])
        self.assertEqual(self.root()["harness_control"]["tasks"]["t1"]["status"], "stopped")

    def test_pure_reducer_does_not_mutate_input(self):
        root = self.root()
        event = self.event()
        original = copy.deepcopy(root)
        result, changed = mutation.reduce_events(root, [event], {event["event_id"]: {}}, TIME)
        self.assertEqual(root, original)
        self.assertEqual(changed, ["t1"])
        self.assertEqual(result["harness_control"]["tasks"]["t1"]["status"], "delivered")

    def test_cli_default_is_preview(self):
        event = self.event()
        ref = self.f.put("事件.json", {"events": [event]})
        before = self.root_path.read_bytes()
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "--run", str(self.f.run), "--owner", "main",
                                 "--expected-root-sha", self.digest(), "--events", str(self.f.run / ref["path"])], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["mode"], "preview")
        self.assertEqual(before, self.root_path.read_bytes())
        self.assertFalse((self.f.run / "状态.json.task-update.lock").exists())

    def test_readonly_gate_has_no_dependency_on_writer(self):
        source = ast.parse((BASE / "脚本" / "任务校验.py").read_text())
        gate_imports = {alias.name for node in ast.walk(source) if isinstance(node, ast.Import) for alias in node.names}
        gate_imports.update(node.module or "" for node in ast.walk(source) if isinstance(node, ast.ImportFrom))
        self.assertFalse(any("任务变更" in name.split(".") for name in gate_imports))
        tree = ast.parse(SCRIPT.read_text())
        imports = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
        self.assertFalse(imports & {"requests", "urllib", "socket", "subprocess"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
