#!/usr/bin/env python3
"""Exact-leaf archival regressions. Every writable file is a temporary fixture."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
BASE = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archive = load("message_archive", BASE / "脚本" / "消息归档.py")
fixtures = load("message_archive_fixtures", BASE / "测试" / "任务校验测试.py")
TIME = fixtures.TIME
UUID = "11111111-1111-4111-8111-111111111111"


class MessageArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.Fixture()
        self.addCleanup(self.f.close)
        self.helper = fixtures.GateTests()
        self.helper.f = self.f
        self.work = self.helper.delivery_plan(delegated=True)
        self.f.root["total_leader"] = "main"
        for row in self.f.control["tasks"].values():
            row.update(status="started", delivered_at=TIME, accepted_at=TIME, started_at=TIME,
                       runtime_id="runtime-" + row["agent_id"], runtime_state="running")
        self.f.write()
        self.target = self.f.run / "小组" / "一组" / "科学稿.md"
        self.log = Path(self.f.temp.name) / "原始消息.jsonl"
        self.payload = "﻿# 科学原文\r\n同版判断。\n\n"
        self.record = {"uuid": UUID, "type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "不得提取"}, {"type": "text", "text": self.payload}]}}
        self.source = self.save_log()

    def save_log(self, record=None, prefix=b"", suffix=b""):
        raw = json.dumps(record or self.record, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n"
        self.log.write_bytes(prefix + raw + suffix)
        payload = self.payload.encode("utf-8")
        return {"path": str(self.log.resolve()), "line": 1 + prefix.count(b"\n"), "record_uuid": UUID,
                "pointer": "/message/content/1/text", "raw_line_sha256": hashlib.sha256(raw).hexdigest(),
                "payload_bytes": len(payload), "payload_sha256": hashlib.sha256(payload).hexdigest(),
                "utf8_start": 0, "utf8_end": len(payload), "task_id": "t1", "author_id": "agent1", "version": "v1"}

    def root_sha(self):
        return hashlib.sha256((self.f.run / "状态.json").read_bytes()).hexdigest()

    def call(self, source=None, apply=False, actor="leader", expected=0, diagnostic=None, root_sha=None):
        before = (self.f.run / "状态.json").read_bytes()
        code, result = archive.archive_message(self.f.run, self.f.path, actor, root_sha or self.root_sha(),
                                               self.source if source is None else source, apply)
        self.assertEqual(code, expected, result)
        if diagnostic:
            self.assertEqual(result["diagnostics"][0]["code"], diagnostic, result)
        self.assertEqual((self.f.run / "状态.json").read_bytes(), before)
        self.assertEqual(list(self.target.parent.glob("消息归档_*.tmp")), [])
        return result

    def test_preview_is_readonly_and_apply_preserves_exact_utf8(self):
        before = {str(p): p.read_bytes() for p in Path(self.f.temp.name).rglob("*") if p.is_file()}
        preview = self.call()
        self.assertFalse(preview["applied"])
        self.assertEqual(before, {str(p): p.read_bytes() for p in Path(self.f.temp.name).rglob("*") if p.is_file()})
        result = self.call(apply=True)
        self.assertTrue(result["applied"])
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))
        self.assertFalse(result["author_authenticated"])
        self.assertNotIn("read_at", result)
        self.assertNotIn("completed", result)
        repeated = self.call(apply=True)
        self.assertFalse(repeated["applied"])
        self.assertTrue(repeated["already_present"])

    def test_sendmessage_leaf_uses_exact_recipient_tool_and_id(self):
        self.record["message"]["content"][1] = {"type": "tool_use", "name": "SendMessage", "id": "tool-1",
                                                  "input": {"to": "leader", "message": self.payload}}
        self.source = self.save_log()
        self.source.update(pointer="/message/content/1/input/message", tool_use_id="tool-1")
        self.call(apply=True)
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))
        self.call(dict(self.source, tool_use_id="wrong"), expected=1, diagnostic="message_tool_id")
        self.record["message"]["content"][1]["input"]["to"] = "main"
        self.source = self.save_log()
        self.source.update(pointer="/message/content/1/input/message", tool_use_id="tool-1")
        self.call(expected=1, diagnostic="message_recipient")

    def test_noncanonical_or_null_uuid_and_wrong_record_are_rejected(self):
        for value in ("00000000-0000-0000-0000-000000000000", "11111111111141118111111111111111", "22222222-2222-4222-8222-222222222222"):
            self.call(dict(self.source, record_uuid=value), expected=1, diagnostic="message_uuid")
        self.call(dict(self.source, record_uuid=None), expected=1, diagnostic="type")

    def test_only_supported_full_assistant_leaves_are_accepted(self):
        for pointer in ("/message", "/message/content/0/thinking", "/message/content/1/text/nested", "/message/content/01/text"):
            self.call(dict(self.source, pointer=pointer), expected=1, diagnostic="message_pointer")
        for record_type, role in (("user", "assistant"), ("assistant", "user")):
            record = copy.deepcopy(self.record)
            record["type"], record["message"]["role"] = record_type, role
            self.call(self.save_log(record), expected=1, diagnostic="message_role")
        self.record["message"]["content"][1] = {"type": "tool_result", "text": self.payload}
        self.call(self.save_log(), expected=1, diagnostic="message_block")

    def test_api_error_placeholder_is_not_a_scientific_original(self):
        self.payload = "API Error: HTTP 422 unprocessable entity"
        self.record["isApiErrorMessage"] = True
        self.record["message"]["content"][1]["text"] = self.payload
        self.source = self.save_log()
        self.call(apply=True, expected=1, diagnostic="message_api_error")
        self.assertFalse(self.target.exists())

    def test_source_parent_symlink_switch_is_rejected(self):
        parent = self.log.parent / "转录目录"
        parent.mkdir()
        self.log = parent / self.log.name
        self.source = self.save_log()
        other = parent.with_name("另一转录目录")
        other.mkdir()
        (other / self.log.name).write_bytes(self.log.read_bytes())
        original = archive.payload_leaf
        def switch(raw, source, reader):
            value = original(raw, source, reader)
            parent.rename(parent.with_name("保留转录目录"))
            parent.symlink_to(other, target_is_directory=True)
            return value
        with mock.patch.object(archive, "payload_leaf", switch):
            self.call(apply=True, expected=1, diagnostic="path_changed")
        self.assertFalse(self.target.exists())

    def test_ranges_bytes_hashes_and_declarations_are_exact(self):
        for field, value, code in (("utf8_start", 1, "message_range"), ("utf8_end", 1, "message_range"),
                                   ("payload_bytes", 1, "message_payload_hash"), ("payload_sha256", "0" * 64, "message_payload_hash"),
                                   ("raw_line_sha256", "0" * 64, "message_line_hash"), ("task_id", "other", "message_declaration"),
                                   ("author_id", "other", "message_declaration"), ("version", "v2", "message_declaration")):
            self.call(dict(self.source, **{field: value}), expected=1, diagnostic=code)
        self.call(dict(self.source, arbitrary_pointer="/other"), expected=1, diagnostic="message_source_fields")

    def test_partial_record_and_line_selection_do_not_search(self):
        self.source = self.save_log(prefix=b"unparsed private preceding record\n", suffix=b"{unparsed later record\n")
        self.call(apply=True)
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))
        self.source["line"] += 1
        self.call(expected=1, diagnostic="message_line_hash")
        self.source = self.save_log()
        self.log.write_bytes(self.log.read_bytes()[:-1])
        self.call(expected=2, diagnostic="message_line_incomplete")

    def test_log_append_during_archive_is_allowed(self):
        original = archive.payload_leaf
        def append(raw, source, reader):
            value = original(raw, source, reader)
            with self.log.open("ab") as stream:
                stream.write(b"additional unparsed record\n")
            return value
        with mock.patch.object(archive, "payload_leaf", append):
            self.call(apply=True)
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))

    def test_selected_line_replacement_and_file_replacement_are_rejected(self):
        original = archive.payload_leaf
        def change(raw, source, reader):
            value = original(raw, source, reader)
            self.log.write_bytes(raw.replace(b"same-never-occurs", b"other") + b"append\n")
            return value
        with mock.patch.object(archive, "payload_leaf", change):
            self.call()  # Append-only is intentionally accepted.
        def replace(raw, source, reader):
            value = original(raw, source, reader)
            alternate = self.log.with_name("替换消息.jsonl")
            alternate.write_bytes(raw)
            os.replace(str(alternate), str(self.log))
            return value
        with mock.patch.object(archive, "payload_leaf", replace):
            self.call(expected=1, diagnostic="message_source_changed")
        self.source = self.save_log()
        def rewrite(raw, source, reader):
            value = original(raw, source, reader)
            self.log.write_bytes(raw.replace(b"11111111", b"22222222", 1))
            return value
        with mock.patch.object(archive, "payload_leaf", rewrite):
            self.call(expected=1, diagnostic="message_source_changed")
        self.assertFalse(self.target.exists())

    def test_multiline_prefetch_cannot_hide_same_inode_record_rewrite(self):
        self.source = self.save_log(suffix=b"later unparsed line\n")
        original = archive.payload_leaf
        before = self.log.stat()
        def rewrite(raw, source, reader):
            value = original(raw, source, reader)
            replacement = raw.replace(b"11111111", b"22222222", 1)
            self.assertEqual(len(replacement), len(raw))
            with self.log.open("r+b") as stream:
                stream.write(replacement)
                stream.flush()
                os.fsync(stream.fileno())
            after = self.log.stat()
            self.assertEqual((before.st_dev, before.st_ino, before.st_size),
                             (after.st_dev, after.st_ino, after.st_size))
            return value
        with mock.patch.object(archive, "payload_leaf", rewrite):
            self.call(apply=True, expected=1, diagnostic="message_source_changed")
        self.assertFalse(self.target.exists())

    def test_fresh_line_rereads_use_blocks_for_large_prefixes(self):
        self.source = self.save_log(prefix=b"prefix line\n" * 10000, suffix=b"later line\n")
        with mock.patch.object(archive.os, "pread", wraps=archive.os.pread) as reads:
            self.call()
        self.assertGreater(reads.call_count, 1)
        self.assertLess(reads.call_count, 100)

    def test_selected_record_can_cross_positional_read_blocks(self):
        self.payload = "# 大段原件\n" + "原文" * 30000 + "\n"
        self.record["message"]["content"][1]["text"] = self.payload
        self.source = self.save_log(prefix=b"preceding line\n", suffix=b"following line\n")
        self.call(apply=True)
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))

    def test_existing_different_original_and_symlink_are_not_overwritten(self):
        self.target.write_bytes(b"original immutable bytes\n")
        self.call(apply=True, expected=1, diagnostic="archive_conflict")
        self.assertEqual(self.target.read_bytes(), b"original immutable bytes\n")
        self.target.unlink()
        outside = Path(self.f.temp.name) / "其他原件.md"
        outside.write_bytes(self.payload.encode("utf-8"))
        self.target.symlink_to(outside)
        self.call(apply=True, expected=1, diagnostic="output_escape")
        self.assertEqual(outside.read_bytes(), self.payload.encode("utf-8"))

    def test_actor_quota_permission_and_actual_start_required(self):
        self.call(actor="main", expected=1, diagnostic="archive_actor")
        row = self.f.control["tasks"]["archive1"]
        row["status"] = "registered"
        self.f.write()
        self.call(expected=2, diagnostic="archive_task_not_started")
        row["status"] = "started"
        row["refusal_actions"] = ["preserve_local"]
        self.f.write()
        self.call(expected=1, diagnostic="permission_denied")
        row.pop("refusal_actions")
        self.f.write()
        with mock.patch.object(archive.os, "access", return_value=False):
            self.call(expected=1, diagnostic="filesystem_permission")
        self.call(root_sha="0" * 64, expected=1, diagnostic="cas_conflict")
        self.assertFalse(self.target.exists())

    def test_source_record_duplicates_must_match_the_registered_envelope(self):
        for field, value in (("output_dir", "小组/另一组"),
                             ("task_scope", {"allowed_actions": ["preserve_local"], "allowed_stages": ["problem-family"], "limits": {}})):
            self.f.record[field] = value
            self.f.write()
            for apply in (False, True):
                self.call(apply=apply, expected=1, diagnostic="record_mismatch")
                self.assertFalse(self.target.exists())
            self.f.record.pop(field)

    def test_legacy_fileheld_does_not_automatically_gain_archive_permission(self):
        self.work["deliverable"].pop("delivery_plan")
        self.helper.replace_delivery_work(self.work)
        self.f.write()
        self.call(expected=2, diagnostic="archive_plan_missing")
        self.assertFalse(self.target.exists())

    def test_parent_switch_and_late_permission_change_refuse_commit(self):
        original = archive.os.link
        def revoke(*args, **kwargs):
            original(*args, **kwargs)
            self.f.control["permissions"]["denied_actions"] = ["preserve_local"]
            self.f.put("状态.json", self.f.root)
        before = self.root_sha()
        with mock.patch.object(archive.os, "link", revoke):
            code, result = archive.archive_message(self.f.run, self.f.path, "leader", before, self.source, True)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][0]["code"], "cas_conflict")
        self.assertFalse(result["applied"])
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob("消息归档_*.tmp")), [])

    def test_fixed_source_path_rejects_aliases(self):
        alias = self.log.with_name("消息别名.jsonl")
        alias.symlink_to(self.log)
        self.call(dict(self.source, path=str(alias)), expected=1, diagnostic="message_source_path")

    def test_direct_plan_can_preserve_its_own_exact_message(self):
        self.work["deliverable"]["delivery_plan"] = {"mode": "direct", "target": "小组/一组/科学稿.md"}
        self.helper.replace_delivery_work(self.work)
        self.f.task["task_scope"]["allowed_actions"] = ["read_local", "generate_local", "preserve_local"]
        self.f.write()
        self.call(apply=True, actor="agent1")
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))

    def test_direct_preservation_does_not_consume_new_generation_allowance(self):
        self.work["deliverable"]["delivery_plan"] = {"mode": "direct", "target": "小组/一组/科学稿.md"}
        self.helper.replace_delivery_work(self.work)
        self.f.task["task_scope"]["allowed_actions"] = ["read_local", "generate_local", "preserve_local"]
        self.f.task["task_scope"]["limits"] = {"generate_local": 1, "preserve_local": 1}
        self.f.record["action_used"] = {"generate_local": 1}
        self.f.write()
        self.call(apply=True, actor="agent1")
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))

    def test_preservation_still_rejects_exhausted_read_or_archive_allowance(self):
        row = self.f.control["tasks"]["archive1"]
        task = json.loads((self.f.run / row["ref"]["path"]).read_bytes())
        for action in ("read_local", "preserve_local"):
            task["task_scope"]["limits"] = {action: 1}
            row["ref"] = self.f.put("归档任务.json", task)
            row["action_used"] = {action: 1}
            self.f.write()
            self.call(apply=True, expected=1, diagnostic="scope_exhausted")
            self.assertFalse(self.target.exists())

    def test_direct_preservation_does_not_reauthorize_revoked_generation(self):
        self.work["deliverable"]["delivery_plan"] = {"mode": "direct", "target": "小组/一组/科学稿.md"}
        self.helper.replace_delivery_work(self.work)
        self.f.task["task_scope"]["allowed_actions"] = ["read_local", "generate_local", "preserve_local"]
        self.f.control["permissions"]["denied_actions"] = ["generate_local"]
        self.f.root["status"] = "paused_after_generation"
        self.f.write()
        self.call(apply=True, actor="agent1")
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))

    def test_explicit_author_refusals_cannot_be_repeated_by_delegate(self):
        for action in ("read_local", "preserve_local"):
            self.f.record["refusal_actions"] = [action]
            self.f.write()
            self.call(apply=True, expected=1, diagnostic="permission_denied")
            self.assertFalse(self.target.exists())

    def test_author_historical_refusal_survives_new_archive_actor(self):
        prior = copy.deepcopy(self.f.task)
        prior["task_id"] = "previous-author"
        self.f.control["tasks"][prior["task_id"]] = {
            "ref": self.f.put("历史作者任务.json", prior), "agent_id": prior["agent_id"], "stage": prior["stage"],
            "status": "stopped", "refusal_actions": ["preserve_local"]}
        self.f.write()
        self.call(apply=True, expected=1, diagnostic="permission_denied")
        self.assertFalse(self.target.exists())

    def test_message_only_scope_is_distinct_from_an_explicit_refusal(self):
        self.assertEqual(self.f.task["task_scope"]["allowed_actions"], ["read_local"])
        self.assertNotIn("refusal_actions", self.f.record)
        self.call(apply=True)
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))

    def test_target_parent_switch_is_detected_before_writing(self):
        original = archive.payload_leaf
        group = self.target.parent
        moved = group.with_name("保留原组")
        other = group.with_name("另一组")
        other.mkdir()
        def switch(raw, source, reader):
            value = original(raw, source, reader)
            group.rename(moved)
            group.symlink_to(other, target_is_directory=True)
            return value
        with mock.patch.object(archive, "payload_leaf", switch):
            self.call(apply=True, expected=1, diagnostic="path_changed")
        self.assertFalse((moved / self.target.name).exists())
        self.assertFalse((other / self.target.name).exists())

    def test_late_real_permission_check_rolls_back_own_new_file(self):
        original = archive.os.link
        revoked = [False]
        def link(*args, **kwargs):
            original(*args, **kwargs)
            revoked[0] = True
        access = archive.os.access
        with mock.patch.object(archive.os, "link", link), mock.patch.object(
                archive.os, "access", side_effect=lambda *a, **kw: False if revoked[0] else access(*a, **kw)):
            result = self.call(apply=True, expected=1, diagnostic="filesystem_permission")
        self.assertFalse(result["applied"])
        self.assertFalse(self.target.exists())

    def test_racing_creator_is_idempotent_only_for_same_bytes(self):
        original = archive.os.link
        def equal(*args, **kwargs):
            self.target.write_bytes(self.payload.encode("utf-8"))
            return original(*args, **kwargs)
        with mock.patch.object(archive.os, "link", equal):
            result = self.call(apply=True)
        self.assertFalse(result["applied"])
        self.assertTrue(result["already_present"])
        self.target.unlink()
        def different(*args, **kwargs):
            self.target.write_bytes(b"other creator original\n")
            return original(*args, **kwargs)
        with mock.patch.object(archive.os, "link", different):
            self.call(apply=True, expected=1, diagnostic="archive_conflict")
        self.assertEqual(self.target.read_bytes(), b"other creator original\n")

    def test_post_write_original_change_is_reported_without_deleting_external_edit(self):
        original = archive.os.fsync
        changed = [False]
        def change(fd):
            original(fd)
            if self.target.exists() and not changed[0]:
                changed[0] = True
                self.target.write_bytes(b"external changed original\n")
        with mock.patch.object(archive.os, "fsync", change):
            result = self.call(apply=True, expected=1, diagnostic="file_changed")
        self.assertTrue(result["applied"])
        self.assertEqual(self.target.read_bytes(), b"external changed original\n")

    def test_nonregular_source_and_existing_target_cannot_block(self):
        fifo = self.log.with_name("非普通消息.jsonl")
        os.mkfifo(str(fifo))
        self.call(dict(self.source, path=str(fifo.resolve())), expected=1, diagnostic="message_source_type")
        os.mkfifo(str(self.target))
        self.call(apply=True, expected=1, diagnostic="output_type")

    def test_python39_offline_surface_and_paused_preservation(self):
        script = BASE / "脚本" / "消息归档.py"
        tree = ast.parse(script.read_text(), feature_version=(3, 9))
        imported = {node.names[0].name for node in ast.walk(tree) if isinstance(node, ast.Import)}
        self.assertFalse(imported & {"socket", "requests", "urllib", "subprocess"})
        self.f.root["status"] = "paused_for_harness_optimization"
        self.f.control["paused"] = True
        self.f.write()
        self.call(apply=True)
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))

    def test_cli_locator_fifo_and_device_are_rejected_without_blocking(self):
        fifo = self.f.run / "定位管道.json"
        os.mkfifo(str(fifo))
        command = [sys.executable, "-I", "-B", str(BASE / "脚本" / "消息归档.py"), "--run", str(self.f.run),
                   "--task", str(self.f.path), "--actor", "leader", "--expected-root-sha", self.root_sha(), "--source"]
        for path in (fifo, Path("/dev/null"), self.f.run):
            result = subprocess.run(command + [str(path)], capture_output=True, text=True, timeout=2)
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            self.assertEqual(json.loads(result.stdout)["diagnostics"][0]["code"], "message_locator_type")
            self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.target.exists())

    def test_locator_requires_utf8_json_and_bounded_ordinary_file(self):
        path = self.f.run / "错误编码定位.json"
        path.write_bytes(json.dumps(self.source).encode("utf-16"))
        with self.assertRaises(archive.gate.GateError) as caught:
            archive.read_locator(path)
        self.assertEqual(caught.exception.code, "json_invalid")
        path.write_bytes(b" " * 16385)
        with self.assertRaises(archive.gate.GateError) as caught:
            archive.read_locator(path)
        self.assertEqual(caught.exception.code, "message_source_fields")

    def test_cli_invalid_locator_returns_json_without_traceback(self):
        spec = self.f.put("错误叶定位.json", dict(self.source, pointer="/message/content/0/thinking"))
        command = [sys.executable, "-I", "-B", str(BASE / "脚本" / "消息归档.py"), "apply", "--run", str(self.f.run),
                   "--task", str(self.f.path), "--actor", "leader", "--expected-root-sha", self.root_sha(),
                   "--source", str(self.f.run / spec["path"])]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)["diagnostics"][0]["code"], "message_pointer")
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.target.exists())

    def test_cli_preview_apply_and_lifecycle_end_to_end(self):
        spec = self.f.put("精确叶定位.json", self.source)
        command = [sys.executable, "-I", "-B", str(BASE / "脚本" / "消息归档.py"), "--run", str(self.f.run),
                   "--task", str(self.f.path), "--actor", "leader", "--expected-root-sha", self.root_sha(),
                   "--source", str(self.f.run / spec["path"])]
        preview = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(preview.returncode, 0, preview.stderr + preview.stdout)
        self.assertFalse(self.target.exists())
        applied = subprocess.run(command[:4] + ["apply"] + command[4:], capture_output=True, text=True)
        self.assertEqual(applied.returncode, 0, applied.stderr + applied.stdout)
        response = json.loads(applied.stdout)
        self.assertEqual(self.target.read_bytes(), self.payload.encode("utf-8"))
        writer = load("message_archive_writer", BASE / "脚本" / "任务变更.py")
        material = response["material_ref"]
        output = {"material_ref": material, "version": "v1", "formed_at": TIME, "delivered_at": TIME, "archived_at": TIME}
        def event(kind, event_id):
            value = {"event_id": event_id, "type": kind, "task_id": "t1", "agent_id": "agent1",
                     "stage": "problem-family", "occurred_at": TIME}
            if kind == "output_observed":
                value["output_delivery"] = dict(output)
            value["evidence"] = {"kind": "message", "id": "observed-" + event_id, "observer_id": "leader",
                                 "observed_at": TIME, "event": dict(value)}
            return value
        code, result = writer.update_tasks(self.f.run, "main", self.root_sha(), [event("output_observed", "e1")], True)
        self.assertEqual(code, 0, result)
        code, result = writer.update_tasks(self.f.run, "main", self.root_sha(), [event("complete", "e2")])
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][0]["code"], "scientific_delivery_missing")
        output.update(read_at=TIME, reader_id="leader", read_version="v1", read_material_ref=material)
        code, result = writer.update_tasks(self.f.run, "main", self.root_sha(),
                                           [event("output_observed", "e3"), event("complete", "e4")], True)
        self.assertEqual(code, 0, result)
        root = json.loads((self.f.run / "状态.json").read_bytes())
        self.assertEqual(root["harness_control"]["tasks"]["t1"]["status"], "completed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
