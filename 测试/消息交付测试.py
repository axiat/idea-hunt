#!/usr/bin/env python3
"""Synthetic native-message delivery, real-session preflight and immutable evidence regressions."""
import copy
import hashlib
import importlib.util
import json
import os
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location("message_fixtures", Path(__file__).with_name("任务校验测试.py"))
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
gate = fixtures.gate
SPEC = importlib.util.spec_from_file_location("message_mutation", Path(__file__).resolve().parents[1] / "脚本/任务变更.py")
mutation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mutation)
TIME = fixtures.TIME


class MessageDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Fixture()
        self.addCleanup(self.f.close)
        self.work = {"schema_version": 2, "decision_question": "Which conditional seed merits early selection?",
                     "premises": {"required_facts": [], "allowed_assumptions": ["Conditional interface"], "deferred_unknowns": ["Novelty"]},
                     "outcome_routes": {"supported": "Select", "refuted": "Stop this version", "unknown": "Keep conditional"},
                     "deliverable": {"format": "message", "version": "v01", "reader_id": "reader",
                                     "delivery_plan": {"mode": "native-message"}}}
        self.f.task["task_scope"].update(allowed_actions=["read_local", "generate_local"], limits={"read_local": 20, "generate_local": 2})
        self.set_work()
        self.f.write()
        self.f.auto_readiness = False
        source = self.f.native("science", "agent1", "reader", "# Complete original\n\nA conditional result; zero experiments.\n")
        self.message = {"source": source, "task_id": "t1", "author_id": "agent1", "reader_id": "reader", "version": "v01"}
        producer = self.f.native("producer", "agent1", "main", {"kind": "scientific_delivery", "message_ref": self.message,
                                                                  "version": "v01", "formed_at": TIME})
        reader = self.f.native("reader", "reader", "main", {"kind": "scientific_read", "message_ref": self.message,
                                                              "version": "v01", "read_at": TIME})
        self.output = {"message_ref": self.message, "version": "v01", "formed_at": TIME,
                       "received_at": TIME, "retained_at": TIME, "read_at": TIME, "reader_id": "reader",
                       "read_version": "v01", "read_message_ref": self.message,
                       "producer_receipt_ref": producer, "read_receipt_ref": reader}
        self.f.write()

    def set_work(self):
        ref = self.f.put("消息合同.json", {"scientific_work": self.work})
        self.f.task.update(scientific_work_ref=ref, input_refs=[self.f.input, ref])

    def check(self, phase="dispatch", expected=0, diagnostic=None):
        return self.f.check(phase, expected=expected, diagnostic=diagnostic)

    def completed(self):
        self.f.record.update(status="completed", delivered_at=TIME, accepted_at=TIME, started_at=TIME,
                             output_delivery=copy.deepcopy(self.output))

    def event(self, kind, **extra):
        event = {"event_id": kind + "-native", "type": kind, "task_id": "t1", "agent_id": "agent1",
                 "stage": self.f.task["stage"], "occurred_at": TIME, **extra}
        event["evidence"] = {"kind": "message", "id": "synthetic-observation", "observer_id": "reader" if kind == "output_observed" else "main",
                             "observed_at": TIME, "event": dict(event)}
        return event

    def test_message_only_route_needs_no_markdown_or_preserve_permission(self):
        self.f.control["permissions"]["denied_actions"] = ["preserve_local"]
        self.check()
        self.completed()
        self.check("checkpoint")
        self.assertFalse(list(self.f.run.rglob("*.md")))

    def test_both_actual_native_readiness_receipts_are_required(self):
        for refs in ([], self.f.task["delivery_readiness_refs"][:1]):
            self.f.task["delivery_readiness_refs"] = refs
            self.check(expected=2, diagnostic="delivery_readiness_missing")

    def test_confirmation_cannot_be_a_boolean_or_an_ack(self):
        self.f.task["delivery_readiness_refs"] = [True, True]
        self.check(expected=1, diagnostic="type")

    def test_readiness_binds_actor_plan_version_and_current_epoch(self):
        original = copy.deepcopy(self.f.task["delivery_readiness_refs"])
        for key, wrong in (("allowed", False), ("epoch", 2), ("version", "v02"), ("reader_id", "someone-else"),
                           ("delivery_plan", {"mode": "direct", "target": "report.md"})):
            with self.subTest(key=key):
                self.f.task["delivery_readiness_refs"] = copy.deepcopy(original)
                source = original[0]
                raw = json.loads(Path(source["path"]).read_bytes())
                value = json.loads(raw["message"]["content"][0]["input"]["message"])
                value[key] = wrong
                replacement = self.f.native("bad-ready-" + key, "agent1", "main", value)
                self.f.task["delivery_readiness_refs"][0] = replacement
                self.check(expected=2, diagnostic="delivery_readiness_binding")

    def test_completion_requires_actual_full_read_and_retention(self):
        self.completed()
        for field in ("read_at", "retained_at"):
            with self.subTest(field=field):
                self.f.record["output_delivery"] = copy.deepcopy(self.output)
                del self.f.record["output_delivery"][field]
                self.check("checkpoint", expected=1, diagnostic="scientific_delivery_missing")

    def test_sender_recipient_version_fingerprint_and_whole_body(self):
        for field, wrong, code in (("author_id", "other", "message_sender"), ("reader_id", "other", "message_recipient"),
                                   ("version", "v02", "message_delivery_binding")):
            with self.subTest(field=field):
                self.completed()
                self.f.record["output_delivery"]["message_ref"][field] = wrong
                self.f.record["output_delivery"]["read_message_ref"] = self.f.record["output_delivery"]["message_ref"]
                self.check("checkpoint", expected=2 if field == "author_id" else 1,
                           diagnostic="native_message_binding_missing" if field == "author_id" else code)
        for field, wrong, code in (("payload_sha256", "0" * 64, "message_payload_hash"),
                                   ("utf8_end", 8, "message_range"), ("pointer", "/message/content/0/text", "message_pointer")):
            with self.subTest(field=field):
                self.completed()
                self.f.record["output_delivery"]["message_ref"]["source"][field] = wrong
                self.check("checkpoint", expected=1, diagnostic=code)

    def test_claimed_author_without_native_sender_is_rejected(self):
        source = self.message["source"]
        raw = json.loads(Path(source["path"]).read_bytes())
        del raw["agentId"]
        data = (json.dumps(raw) + "\n").encode()
        Path(source["path"]).write_bytes(data)
        source["raw_line_sha256"] = hashlib.sha256(data).hexdigest()
        self.completed()
        self.check("checkpoint", expected=1, diagnostic="message_sender")

    def test_native_reader_and_producer_receipts_cannot_be_substituted(self):
        self.completed()
        self.f.record["output_delivery"]["read_receipt_ref"] = self.output["producer_receipt_ref"]
        self.check("checkpoint", expected=2, diagnostic="native_message_binding_missing")
        self.completed()
        self.f.record["output_delivery"]["producer_receipt_ref"] = self.output["read_receipt_ref"]
        self.check("checkpoint", expected=2, diagnostic="native_message_binding_missing")

    def test_append_is_allowed_but_same_inode_edit_is_rejected(self):
        checker = gate.Checker(self.f.run, self.f.path, "checkpoint")
        checker.message_ref(self.message)
        p = Path(self.message["source"]["path"])
        with p.open("ab") as f:
            f.write(b"{}\n")
        checker.recheck()
        original = p.read_bytes()
        p.write_bytes(original.replace(b"Complete original", b"Modified original"))
        with self.assertRaises(gate.GateError):
            checker.recheck()

    def test_replaced_log_and_parent_link_are_rejected(self):
        checker = gate.Checker(self.f.run, self.f.path, "checkpoint")
        checker.message_ref(self.message)
        p = Path(self.message["source"]["path"])
        replacement = p.with_suffix(".replacement")
        replacement.write_bytes(p.read_bytes())
        os.replace(replacement, p)
        with self.assertRaises(gate.GateError):
            checker.recheck()

    def test_pause_and_source_protection_are_unchanged(self):
        self.f.control["paused"] = True
        self.check(expected=1, diagnostic="run_paused")
        self.completed()
        self.check("checkpoint")
        self.f.control["permissions"]["denied_actions"] = ["generate_local"]
        self.f.control["paused"] = False
        self.f.record.update(status="registered")
        self.check(expected=1, diagnostic="permission_denied")

    def test_v1_cannot_implicitly_adopt_message_delivery(self):
        self.work["schema_version"] = 1
        self.set_work()
        self.check(expected=1, diagnostic="scientific_schema")

    def test_scientific_message_inputs_are_verified(self):
        self.f.task["message_input_refs"] = [copy.deepcopy(self.message)]
        self.check()
        self.f.task["message_input_refs"][0]["source"]["payload_sha256"] = "0" * 64
        self.check(expected=1, diagnostic="message_payload_hash")

    def test_message_edit_between_prepare_and_commit_keeps_root_unchanged(self):
        self.f.record.update(status="started", delivered_at=TIME, accepted_at=TIME, started_at=TIME)
        self.f.write()
        root = self.f.run / "状态.json"
        before = root.read_bytes()
        original_recheck = mutation.gate.Checker.recheck
        changed = []
        def racing_recheck(checker, *args, **kwargs):
            original_recheck(checker, *args, **kwargs)
            if not changed:
                p = Path(self.message["source"]["path"])
                p.write_bytes(p.read_bytes().replace(b"Complete original", b"Modified original"))
                changed.append(True)
        events = [self.event("output_observed", output_delivery=self.output)]
        with mock.patch.object(mutation.gate.Checker, "recheck", racing_recheck):
            code, result = mutation.update_tasks(self.f.run, "main", hashlib.sha256(before).hexdigest(), events, apply=True)
        self.assertEqual(code, 1, result)
        self.assertEqual(root.read_bytes(), before)

    def test_sender_claim_in_an_unbound_log_is_not_native_authentication(self):
        self.f.control["native_message_bindings"].pop("agent1")
        self.check(expected=2, diagnostic="native_message_binding_missing")

    def test_protocol_archive_cannot_fallback_for_a_scientific_role(self):
        self.migration()
        self.f.write()
        checker = gate.Checker(self.f.run, self.f.path, "checkpoint")
        checker.protocol_mapping(self.f.task, self.f.record)
        bad = dict(self.f.role, sha256="0" * 64)
        with self.assertRaises(gate.GateError) as raised:
            checker.ref(bad, norm=True)
        self.assertEqual(raised.exception.code, "hash_mismatch")

    def migration(self):
        self.work["schema_version"] = 1
        self.work["deliverable"] = {"format": "markdown", "version": "v01", "reader_id": "reader"}
        self.set_work()
        norm = Path(__file__).resolve().parents[1] / "SKILL.md"
        old_bytes = b"Synthetic previous delivery protocol"
        old_ref = {"path": str(norm), "sha256": hashlib.sha256(old_bytes).hexdigest()}
        self.f.task["input_refs"].append(old_ref)
        self.f.record.update(status="started", delivered_at=TIME, accepted_at=TIME, started_at=TIME)
        self.f.write()
        original = self.f.path.read_bytes()
        self.f.control["epoch"] = 2
        archive = self.f.run / "norms.zip"
        manifest = {"schema_version": 1, "purpose": "Synthetic explicit delivery transition",
                    "files": [{"source_path": str(norm), "archive_path": "old/SKILL.md",
                               "sha256": old_ref["sha256"], "bytes": len(old_bytes)}]}
        with zipfile.ZipFile(archive, "w") as z:
            z.writestr("快照清单.json", json.dumps(manifest))
            z.writestr("old/SKILL.md", old_bytes)
        delivery = {"format": "message", "version": "v01", "reader_id": "reader", "delivery_plan": {"mode": "native-message"}}
        mapping = {"task_ref": self.f.record["ref"], "deliverable": delivery,
                   "delivery_readiness_refs": self.f.readiness(self.f.task, delivery)}
        revision = {"schema_version": 2, "run_id": self.f.run.name, "from_epoch": 1, "to_epoch": 2,
                    "norm_archive_ref": self.f.ref(archive), "norm_refs": [{"path": str(norm), "sha256": hashlib.sha256(norm.read_bytes()).hexdigest()}],
                    "task_mappings": {"t1": mapping}}
        self.f.control["delivery_protocol_ref"] = self.f.put("协议修订.json", revision)
        return original, revision

    def test_explicit_revision_preserves_envelope_and_resolves_only_delivery_norms(self):
        original, revision = self.migration()
        self.f.record["output_delivery"] = copy.deepcopy(self.output)
        self.f.record["status"] = "completed"
        self.check("checkpoint")
        self.assertEqual(self.f.path.read_bytes(), original)
        self.assertEqual(self.f.task["epoch"], 1)
        self.assertEqual(self.f.control["epoch"], 2)
        revision["task_mappings"]["t1"]["task_ref"]["sha256"] = "0" * 64
        self.f.control["delivery_protocol_ref"] = self.f.put("协议修订.json", revision)
        self.check("checkpoint", expected=1, diagnostic="protocol_task_binding")

    def test_revision_cannot_change_reader_version_or_non_delivery_claim(self):
        _, revision = self.migration()
        revision["task_mappings"]["t1"]["deliverable"]["reader_id"] = "other"
        self.f.control["delivery_protocol_ref"] = self.f.put("协议修订.json", revision)
        self.check("checkpoint", expected=1, diagnostic="protocol_delivery_binding")
        revision["task_mappings"]["t1"]["deliverable"]["reader_id"] = "reader"
        revision["task_mappings"]["t1"]["decision_question"] = "Changed science"
        self.f.control["delivery_protocol_ref"] = self.f.put("协议修订.json", revision)
        self.check("checkpoint", expected=1, diagnostic="protocol_mapping_fields")

    def test_mapped_task_output_and_completion_use_existing_thin_writer(self):
        original, revision = self.migration()
        self.f.write()
        root = self.f.run / "状态.json"
        before = root.read_bytes()
        events = [self.event("output_observed", output_delivery=self.output), self.event("complete")]
        code, result = mutation.update_tasks(self.f.run, "main", hashlib.sha256(before).hexdigest(), events, apply=True)
        self.assertEqual(code, 0, result)
        self.assertEqual(self.f.path.read_bytes(), original)
        self.assertEqual(json.loads(root.read_bytes())["harness_control"]["tasks"]["t1"]["status"], "completed")

    def test_markdown_route_also_needs_both_session_confirmations(self):
        self.work["schema_version"] = 2
        self.work["deliverable"] = {"format": "markdown", "version": "v01", "reader_id": "reader",
                                    "delivery_plan": {"mode": "direct", "target": "小组/一组/科学稿.md"}}
        self.set_work()
        self.f.task["task_scope"]["allowed_actions"].append("preserve_local")
        self.f.task["delivery_readiness_refs"] = []
        self.check(expected=2, diagnostic="delivery_readiness_missing")

    def test_end_to_end_preview_apply_complete_and_checkpoint(self):
        self.f.record.update(status="started", delivered_at=TIME, accepted_at=TIME, started_at=TIME)
        self.f.write()
        root = self.f.run / "状态.json"
        before = root.read_bytes()
        events = [self.event("output_observed", output_delivery=self.output), self.event("complete")]
        code, result = mutation.update_tasks(self.f.run, "main", hashlib.sha256(before).hexdigest(), events)
        self.assertEqual(code, 0, result)
        self.assertEqual(root.read_bytes(), before)
        code, result = mutation.update_tasks(self.f.run, "main", hashlib.sha256(before).hexdigest(), events, apply=True)
        self.assertEqual(code, 0, result)
        row = json.loads(root.read_bytes())["harness_control"]["tasks"]["t1"]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["checkpoint"]["saved_messages"], [self.message])
        self.assertFalse(list(self.f.run.rglob("*.md")))
        code, result = gate.check_task(self.f.run, self.f.path, "checkpoint")
        self.assertEqual(code, 0, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
