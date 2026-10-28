#!/usr/bin/env python3
"""Offline stage contracts: scientific unknowns are not action permissions."""
import copy
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("stage_fixture", Path(__file__).with_name("任务校验测试.py"))
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
gate = fixtures.gate


class StageContractTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Fixture()
        self.addCleanup(self.f.close)
        self.work = {
            "schema_version": 1,
            "decision_question": "Does the conditional local hypothesis merit an independent early selection?",
            "premises": {
                "required_facts": ["The fixed local input is already delivered and permitted."],
                "allowed_assumptions": ["The proposed interface is a stated hypothesis, not an observed capability."],
                "deferred_unknowns": ["Novelty", "Historical consumer correspondence", "Robot task benefit"],
            },
            "outcome_routes": {
                "supported": "Preserve the conditional seed for independent early selection.",
                "refuted": "Record the reason for zero seeds in this version.",
                "unknown": "Deliver the conditional claim and name the evidence needed for the next stage.",
            },
            "deliverable": {"format": "markdown", "version": "v1", "reader_id": "group-leader"},
        }
        self.f.task["task_scope"]["allowed_actions"] = ["read_local", "generate_local", "preserve_local"]
        self.f.task["task_scope"]["limits"] = {"generate_local": 2, "preserve_local": 1}
        self.set_work()

    def set_work(self):
        ref = self.f.put("阶段合同.json", {"scientific_work": self.work})
        self.f.task["scientific_work_ref"] = ref
        self.f.task["input_refs"] = [self.f.input, ref]

    def test_local_unknowns_do_not_consume_source_permissions(self):
        self.f.control["source_ledger_ref"] = {"path": "unavailable-source-ledger.json", "sha256": "0" * 64}
        self.f.control["permissions"]["denied_actions"] = ["source_get", "source_query", "read_cache"]
        self.f.check()
        self.f.record.update(status="accepted", delivered_at=fixtures.TIME, accepted_at=fixtures.TIME)
        result = self.f.check("entry")
        self.assertTrue(result["receipt"]["ok"])
        self.assertFalse((self.f.run / "unavailable-source-ledger.json").exists())

    def test_provenance_input_does_not_implicitly_request_transport(self):
        history = self.f.put("来源出处.json", {"uri": "https://source.invalid/paper", "transport_state": "unknown"})
        self.f.task["input_refs"].append(history)
        self.f.check()
        self.assertNotIn("contract_refs", self.f.task)
        self.f.task["contract_refs"] = [history]
        self.f.write()
        code, result = gate.check_task(self.f.run, self.f.path, "dispatch")
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])

    def test_new_transport_still_holds_on_unknown_shared_balance(self):
        f = fixtures.Fixture()
        self.addCleanup(f.close)
        f.source()
        f.account["shared_required"] = True
        f.write()
        code, result = gate.check_task(f.run, f.path, "dispatch")
        self.assertEqual(code, 2, result)
        self.assertEqual(result["decision"], "hold")

    def test_conditional_hypothesis_does_not_override_pause_or_denial(self):
        self.f.control["paused"] = True
        self.f.check(expected=1, diagnostic="run_paused")
        self.f.control["paused"] = False
        self.f.control["permissions"]["denied_actions"] = ["generate_local"]
        self.f.write()
        code, result = gate.check_task(self.f.run, self.f.path, "dispatch")
        self.assertNotEqual(code, 0)
        self.assertFalse(result["ok"])

    def test_different_questions_can_reuse_the_same_permitted_input(self):
        for question in (
            "Which state information changes the action choice?",
            "Which training supervision changes the learned response?",
            "Which evaluation distinguishes the two conditional mechanisms?",
        ):
            with self.subTest(question=question):
                self.work["decision_question"] = question
                self.set_work()
                self.f.check()

    def test_inventory_separates_run_control_from_recorded_progress(self):
        self.f.write()
        code, result = gate.inspect_run(self.f.run, limit=1)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["control"]["status"], "running")
        self.assertEqual(result["tasks"][0]["work_kind"], "scientific")
        self.assertIsNone(result["tasks"][0]["recorded_facts"]["started_at"])
        self.assertEqual(result["progress"]["scope"], "returned_tasks_only")
        self.assertEqual(result["progress"]["scientific_started_records"], 0)
        self.assertEqual(result["progress"]["physical_activity"], "not_checked")
        self.assertFalse(result["action_permission"])

    def test_inventory_reports_facts_without_claiming_physical_liveness(self):
        self.f.record.update(status="started", delivered_at=fixtures.TIME,
                             accepted_at=fixtures.TIME, started_at=fixtures.TIME)
        self.f.write()
        code, result = gate.inspect_run(self.f.run, limit=1)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["progress"]["scientific_started_records"], 1)
        self.assertEqual(result["progress"]["scientific_output_records"], 0)
        self.assertEqual(result["tasks"][0]["recorded_facts"]["started_at"], fixtures.TIME)
        self.assertEqual(result["progress"]["physical_activity"], "not_checked")

    def test_inventory_effective_pause_matches_gate(self):
        for kind in ("root", "control", "optimization", "draining"):
            with self.subTest(kind=kind):
                self.f.root["status"] = "running"
                self.f.root["harness_optimization_pause"] = {}
                self.f.control["paused"] = False
                if kind == "root":
                    self.f.root["status"] = "paused_for_maintenance"
                elif kind == "control":
                    self.f.control["paused"] = True
                elif kind == "optimization":
                    self.f.root["harness_optimization_pause"] = {
                        "new_scientific_tasks_and_source_requests_paused": True}
                else:
                    self.f.root["status"] = "draining_existing_tasks"
                self.f.write()
                code, result = gate.inspect_run(self.f.run, limit=1)
                self.assertEqual(code, 0, result)
                self.assertTrue(result["control"]["effective_paused"])
                self.assertTrue(result["control"]["pause_reasons"])
                code, checked = gate.check_task(self.f.run, self.f.path, "dispatch")
                self.assertEqual(code, 1, checked)
                self.assertEqual(checked["diagnostics"][-1]["code"], "run_paused")

    def test_basic_timezone_offsets_keep_the_same_instant(self):
        for compact, extended in (
            ("2026-10-24T15:40:32+0800", "2026-10-24T15:40:32+08:00"),
            ("2026-10-24T15:40:32.123456-0530", "2026-10-24T15:40:32.123456-05:30"),
            ("2026-10-24T15:40:32+0000", "2026-10-24T15:40:32Z"),
        ):
            with self.subTest(timestamp=compact):
                self.assertEqual(gate.timestamp(compact, "observed_at"), gate.timestamp(extended, "observed_at"))

    def test_basic_timezone_support_keeps_invalid_and_naive_times_rejected(self):
        for value in ("2026-10-24T15:40:32+2400", "2026-10-24T15:40:32+0860", "2026-10-24T15:40:32+1260",
                      "2026-10-24T15:40:32-0099", "2026-10-24T15:40:32", "not-a-time+0800"):
            with self.subTest(timestamp=value):
                with self.assertRaises(gate.GateError):
                    gate.timestamp(value, "observed_at")

    def test_started_entry_accepts_preserved_basic_timezone_facts(self):
        basic = "2026-10-14T18:00:00+0800"
        self.f.record.update(status="started", delivered_at=basic, accepted_at=basic,
                             started_at=fixtures.TIME, runtime_id="agent1", runtime_state="unknown")
        self.f.check("entry")
        self.assertEqual(self.f.record["delivered_at"], basic)
        self.assertEqual(self.f.record["accepted_at"], basic)
        self.assertEqual(self.f.record["runtime_state"], "unknown")
        self.assertNotIn("stop_receipt_ref", self.f.record)

    def test_native_followup_is_a_readonly_original_actor_hint(self):
        self.f.record.update(status="started", delivered_at=fixtures.TIME,
                             accepted_at=fixtures.TIME, started_at=fixtures.TIME)
        self.f.write()
        root = self.f.run / "状态.json"
        before = root.read_bytes()
        code, result = gate.inspect_run(self.f.run, task="t1")
        self.assertEqual(code, 0, result)
        hint = result["tasks"][0]["native_context_followup"]
        self.assertEqual(hint["tool"], "SendMessage")
        self.assertEqual(hint["to"], self.f.task["agent_id"])
        self.assertEqual(hint["task_id"], "t1")
        self.assertEqual(hint["purpose"], "identity_and_checkpoint_only")
        self.assertEqual(hint["native_route"], "not_checked")
        self.assertFalse(hint["action_permission"])
        self.assertFalse(hint["repeat_without_new_evidence"])
        self.assertTrue(Path(hint["reference"]).is_file())
        self.assertEqual(root.read_bytes(), before)

    def test_native_followup_never_truncates_or_invents_a_target(self):
        for actor in (None, "", "a" * 385, "agent name", "agent\nother"):
            with self.subTest(actor=actor):
                self.assertIsNone(gate.native_context_followup("t1", {"agent_id": actor, "status": "started"}))
        for status in ("registered", "completed", "unknown"):
            with self.subTest(status=status):
                self.assertIsNone(gate.native_context_followup("t1", {"agent_id": "agent1", "status": status}))

    def test_same_started_task_continues_without_invented_stop(self):
        self.f.record.update(status="started", delivered_at=fixtures.TIME,
                             accepted_at=fixtures.TIME, started_at=fixtures.TIME,
                             runtime_id="agent1", runtime_state="unknown",
                             action_used={"generate_local": 1})
        self.f.record["runtime_incident_ref"] = self.f.put("原生中断.json", {
            "task_id": "t1", "agent_id": "agent1", "failure_id": "api-error-1",
            "native_error": "synthetic stream error", "logical_terminal_observed": False})
        result = self.f.check("entry")
        self.assertEqual(result["receipt"]["task_id"], "t1")
        self.assertEqual(self.f.record["status"], "started")
        self.assertEqual(self.f.record["started_at"], fixtures.TIME)
        self.assertEqual(self.f.record["runtime_state"], "unknown")
        self.assertNotIn("stop_receipt_ref", self.f.record)
        self.assertEqual(self.f.control["recoveries"], {})
        self.assertEqual(self.f.record["action_used"], {"generate_local": 1})

    def test_original_context_reentry_keeps_pause_refusal_and_limits(self):
        self.f.record.update(status="started", delivered_at=fixtures.TIME,
                             accepted_at=fixtures.TIME, started_at=fixtures.TIME)
        self.f.control["paused"] = True
        self.f.check("entry", expected=1, diagnostic="run_paused")
        self.f.control["paused"] = False
        self.f.record["refusal_actions"] = ["generate_local"]
        self.f.check("entry", expected=1, diagnostic="permission_denied")
        self.f.record.pop("refusal_actions")
        self.f.record["action_used"] = {"generate_local": 2}
        self.f.check("entry", expected=1, diagnostic="scope_exhausted")

    def test_original_context_reentry_cannot_revive_logical_terminal(self):
        self.f.record.update(delivered_at=fixtures.TIME, accepted_at=fixtures.TIME,
                             started_at=fixtures.TIME, failure_id="f1")
        for status in ("failed", "stopped"):
            with self.subTest(status=status):
                self.f.record["status"] = status
                self.f.check("entry", expected=1, diagnostic="task_not_delivered")

    def test_unrelated_actor_failure_does_not_pause_independent_science(self):
        prior = copy.deepcopy(self.f.task)
        prior.update(task_id="other-task", agent_id="other-agent")
        row = {"ref": self.f.put("其他组任务.json", prior), "agent_id": "other-agent",
               "stage": prior["stage"], "status": "started", "runtime_state": "unknown"}
        self.f.control["tasks"]["other-task"] = row
        self.f.check()
        row.update(status="failed", failure_id="other-api-error")
        self.f.check()
        self.assertFalse(self.f.control["paused"])
        self.assertNotIn("stop_receipt_ref", row)

    def test_new_task_stays_blocked_while_original_context_can_be_contacted(self):
        prior = copy.deepcopy(self.f.task)
        prior["task_id"] = "old-task"
        row = {"ref": self.f.put("原任务.json", prior), "agent_id": prior["agent_id"],
               "stage": prior["stage"], "status": "started", "runtime_state": "unknown"}
        self.f.control["tasks"]["old-task"] = row
        result = self.f.check(expected=1, diagnostic="agent_busy")
        blocker = result["execution_blocker"]
        self.assertEqual(blocker["task_id"], "old-task")
        self.assertEqual(blocker["native_context_followup"]["to"], prior["agent_id"])
        self.assertFalse(blocker["native_context_followup"]["action_permission"])
        row.update(status="failed", failure_id="api-error")
        result = self.f.check(expected=2, diagnostic="stop_evidence_missing")
        self.assertTrue(result["execution_blocker"]["unknown_is_not_evidence"])
        self.assertFalse(result["execution_blocker"]["native_context_followup"]["action_permission"])

    def test_execution_blocker_does_not_echo_reference_annotations(self):
        prior = copy.deepcopy(self.f.task)
        prior["task_id"] = "old-task"
        ref = self.f.put("原任务.json", prior)
        ref["note"] = "synthetic annotation " * 50000
        row = {"ref": ref, "agent_id": prior["agent_id"], "stage": prior["stage"], "status": "started"}
        self.f.control["tasks"]["old-task"] = row
        for status, expected, diagnostic in (("started", 1, "agent_busy"), ("failed", 2, "stop_evidence_missing")):
            with self.subTest(status=status):
                row["status"] = status
                result = self.f.check(expected=expected, diagnostic=diagnostic)
                self.assertEqual(set(result["execution_blocker"]["task_ref"]), {"path", "sha256"})
                self.assertLess(len(fixtures.json.dumps(result)), 4096)

    def test_new_output_without_plan_still_stays_in_its_task_directory(self):
        import copy
        import hashlib
        writer_spec = importlib.util.spec_from_file_location(
            "stage_writer", Path(__file__).resolve().parents[1] / "脚本" / "任务变更.py")
        writer = importlib.util.module_from_spec(writer_spec)
        writer_spec.loader.exec_module(writer)
        self.f.root["total_leader"] = "owner"
        self.f.record.update(status="started", delivered_at=fixtures.TIME,
                             accepted_at=fixtures.TIME, started_at=fixtures.TIME)
        foreign = self.f.run / "小组" / "其他组" / "稿件.md"
        foreign.parent.mkdir()
        foreign.write_text("# Synthetic foreign output\n", encoding="utf-8")
        output = {"material_ref": self.f.ref(foreign), "version": "v1", "formed_at": fixtures.TIME}
        self.f.write()
        event = {"event_id": "foreign-output", "type": "output_observed", "task_id": "t1",
                 "agent_id": "agent1", "stage": self.f.task["stage"], "occurred_at": fixtures.TIME,
                 "output_delivery": output}
        event["evidence"] = {"kind": "message", "id": "synthetic-output-observation", "observer_id": "owner",
                             "observed_at": fixtures.TIME, "event": copy.deepcopy(event)}
        root_path = self.f.run / "状态.json"
        before = root_path.read_bytes()
        code, result = writer.update_tasks(self.f.run, "owner", hashlib.sha256(before).hexdigest(), [event])
        self.assertEqual(code, 1, result)
        self.assertIn("output_escape", [d["code"] for d in result["diagnostics"]])
        self.assertEqual(root_path.read_bytes(), before)
        # Explicit checkpoint preserves the already-recorded historical reference.
        self.f.record["output_delivery"] = output
        self.f.check("checkpoint")

    def test_absolute_output_directory_accepts_relative_target_through_run_alias(self):
        self.f.task["output_dir"] = str((self.f.run / "小组" / "一组").resolve())
        self.work["deliverable"]["delivery_plan"] = {"mode": "direct", "target": "小组/一组/科学稿.md"}
        self.set_work()
        self.f.write()
        alias = self.f.run.parent / "运行别名"
        alias.symlink_to(self.f.run, target_is_directory=True)
        for run in (self.f.run, self.f.run.resolve(), alias):
            with self.subTest(run=str(run)):
                code, result = gate.check_task(run, self.f.path, "dispatch")
                self.assertEqual(code, 0, result)

    def test_new_tool_norms_have_fixed_archive_paths(self):
        sources = gate.Checker.norm_sources()
        skill = Path(fixtures.SCRIPT).resolve().parents[1]
        for name in ("参考/任务接口.md", "脚本/任务变更.py", "测试/任务变更_测试.py",
                     "脚本/消息归档.py", "测试/消息归档测试.py"):
            self.assertIn(str(skill / name), sources)
        self.assertNotIn(str(self.f.run / "来源账.json"), sources)
        self.assertNotIn(str(self.f.run / "小组/一组/科学稿.md"), sources)

    def test_delegated_admission_rejects_cancelled_or_busy_archive_actor(self):
        import copy
        for condition, diagnostic in (("cancelled", "task_cancelled"), ("busy", "agent_busy")):
            with self.subTest(condition=condition):
                f = fixtures.Fixture()
                self.addCleanup(f.close)
                helper = fixtures.GateTests()
                helper.f = f
                helper.delivery_plan(delegated=True)
                row = f.control["tasks"]["archive1"]
                if condition == "cancelled":
                    # Any cancellation marker makes this proposed route unavailable.
                    row["unstarted_cancellation"] = {"cancelled_at": fixtures.TIME}
                else:
                    previous = copy.deepcopy(f.task)
                    previous.pop("scientific_work_ref")
                    previous.update(task_id="archive-busy", agent_id="leader", input_refs=[f.input])
                    f.control["tasks"]["archive-busy"] = {
                        "ref": f.put("忙碌任务.json", previous), "agent_id": "leader",
                        "stage": previous["stage"], "status": "started", "delivered_at": fixtures.TIME,
                        "accepted_at": fixtures.TIME, "started_at": fixtures.TIME}
                f.check(expected=1, diagnostic=diagnostic)

    def cancellation_fixture(self):
        spec = importlib.util.spec_from_file_location(
            "stage_cancellation_fixture", Path(__file__).with_name("任务变更_测试.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        helper = module.MutationTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        return helper

    def test_cancellation_epoch_cache_retains_final_root_recheck(self):
        import json
        from unittest import mock
        helper = self.cancellation_fixture()
        helper.update([helper.cancellation_event()])
        row = helper.root()["harness_control"]["tasks"]["t1"]
        checker = gate.Checker(helper.f.run, helper.f.path, "checkpoint")
        with mock.patch.object(checker, "read", wraps=checker.read) as read:
            for _ in range(32):
                checker.unstarted_cancelled("t1", row, "main")
        reads = [call for call in read.call_args_list
                 if Path(call.args[0]).resolve() == checker.root_path]
        self.assertEqual(len(reads), 1)
        checker.recheck()
        changed = helper.root()
        changed["goal_met"] = True
        helper.root_path.write_text(json.dumps(changed))
        with self.assertRaises(gate.GateError) as error:
            checker.recheck()
        self.assertEqual(error.exception.code, "file_changed")

    def test_cancellation_locator_is_opaque_but_bare_error_is_not_evidence(self):
        import json
        helper = self.cancellation_fixture()
        event = helper.cancellation_event()
        receipt = json.loads((helper.f.run / event["unstarted_receipt_ref"]["path"]).read_bytes())
        receipt["observation"]["id"] = "tool-confirmed-FailedJSON-preparation-not-dispatched"
        helper.update([helper.cancellation_receipt(event, receipt)], apply=False)
        for label in ("No task found", "Failed JSON"):
            with self.subTest(label=label):
                receipt["observation"]["id"] = label
                helper.update([helper.cancellation_receipt(event, receipt)], apply=False,
                              expected=1, diagnostic="unstarted_receipt_invalid")


if __name__ == "__main__":
    unittest.main(verbosity=2)
