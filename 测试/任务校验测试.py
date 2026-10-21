#!/usr/bin/env python3
"""Synthetic offline fixtures only. python3 -I 任务校验测试.py [--example]."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
import shutil
from unittest import mock
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[1] / "脚本" / "任务校验.py"
spec = importlib.util.spec_from_file_location("task_gate", SCRIPT)
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
ZERO = {"initial_get": 0, "normal_location": 0, "query": 0}
LIMIT = {"initial_get": 1, "normal_location": 0, "query": 0}
TIME = "2026-10-14T10:00:00+00:00"


class Fixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory(prefix="task-gate-")
        self.run = Path(self.temp.name) / "合成运行"
        self.run.mkdir()
        (self.run / "小组" / "一组").mkdir(parents=True)
        (self.run / "检索支持").mkdir()
        self.role = self.put("角色.json", {"role": "synthetic"})
        self.input = self.put("输入.json", {"allowed_actions": ["read_local"]})
        self.task = {"schema_version": 1, "run_id": self.run.name, "epoch": 1,
                     "task_id": "t1", "stage": "problem-family", "agent_id": "agent1",
                     "role_ref": self.role, "input_refs": [self.input],
                     "task_scope": {"allowed_actions": ["read_local"], "allowed_stages": ["problem-family"], "limits": {}},
                     "output_dir": "小组/一组"}
        self.record = {"agent_id": "agent1", "stage": "problem-family", "status": "registered",
                       "checkpoint": {"saved_refs": [self.input], "missing_sections": []}}
        self.control = {"schema_version": 1, "epoch": 1, "paused": False,
                        "permissions": {"allowed_actions": sorted(gate.ACTIONS), "denied_actions": []},
                        "tasks": {"t1": self.record}, "recoveries": {}}
        self.root = {"run_id": self.run.name, "status": "running", "harness_control": self.control}
        self.path = self.run / "任务.json"
        self.contract = None

    def close(self):
        self.temp.cleanup()

    def put(self, name, value):
        p = self.run / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.ref(p)

    def ref(self, p):
        p = Path(p)
        return {"path": str(p.relative_to(self.run)), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}

    def write(self):
        if self.contract is not None:
            cref = self.put("合同.json", self.contract)
            self.task["contract_refs"] = [cref]
            self.ledger["contracts"][self.contract["contract_id"]] = cref
            self.control["source_ledger_ref"] = self.put("来源账.json", self.ledger)
        self.record["ref"] = self.put("任务.json", self.task)
        self.put("状态.json", self.root)

    def delivery(self, status="started"):
        self.record.update(status=status, material_ref=self.input, formed_at=TIME, delivered_at=TIME, started_at=TIME)
        if status in {"accepted", "started", "completed"}:
            self.record["accepted_at"] = TIME
        if status == "accepted":
            self.record.update(read_at=TIME, reader_id="reader1")

    def source(self, cache=False):
        constraints = {"max_bytes": 8388608, "timeout_seconds": 30, "retries": 0}
        self.task["task_scope"].update(allowed_actions=["read_cache" if cache else "source_get"],
                                      limits={} if cache else {"source_get": 1})
        self.contract = {"schema_version": 1, "contract_id": "c1", "account_id": "a1",
                         "uri": "https://source.invalid/a", "owner": "agent1", "limits": copy.deepcopy(LIMIT),
                         "used_floor": copy.deepcopy(ZERO), "constraints": constraints,
                         "operation": "cache" if cache else "request", "costs": copy.deepcopy(ZERO if cache else LIMIT)}
        self.account = {"uris": [self.contract["uri"], "https://alias.invalid/a"], "owner": "agent1",
                        "limits": copy.deepcopy(LIMIT), "used": copy.deepcopy(ZERO), "remaining": copy.deepcopy(LIMIT),
                        "constraints": copy.deepcopy(constraints), "history_refs": [], "inflight": [],
                        "protected": False, "permission_denied": False, "stopped": False, "shared_required": False,
                        "cache": {"status": "available", "ref": self.input} if cache else {"status": "absent"}}
        self.ledger = {"schema_version": 1, "contracts": {}, "accounts": {"a1": self.account}}

    def history(self, **changes):
        old = {"account_id": "a1", "uris": list(self.account["uris"]), "used": copy.deepcopy(self.account["used"]),
               "limits": copy.deepcopy(self.account["limits"]), "constraints": copy.deepcopy(self.account["constraints"]), "cache": copy.deepcopy(self.account["cache"]),
               "inflight": [], "protected": False, "permission_denied": False, "stopped": False}
        old.update(changes)
        ref = self.put("旧账户.json", old)
        self.account["history_refs"] = [ref]
        return ref

    def recovery(self):
        old = copy.deepcopy(self.task)
        old.update(task_id="old", agent_id="old-agent")
        self.stop = {"task_id": "old", "agent_id": "old-agent", "runtime_id": "runtime-old",
                     "status": "stopped", "success": True, "stopped_at": TIME}
        self.prior = {"ref": self.put("旧任务.json", old), "agent_id": "old-agent", "stage": old["stage"],
                      "status": "failed", "failure_id": "f1", "runtime_state": "stopped", "runtime_id": "runtime-old",
                      "stop_receipt_ref": self.put("停止收据.json", self.stop)}
        self.control["tasks"]["old"] = self.prior
        self.task["recovery_of"] = {"task_id": "old", "failure_id": "f1"}
        self.control["recoveries"]["old/f1"] = {"task_id": "old", "failure_id": "f1", "replacement_task_id": "t1"}

    def check(self, phase="dispatch", expected=0, diagnostic=None, cli=False, rewrite=True):
        if rewrite:
            self.write()
        before = {str(p): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        args = ["check", "--run", str(self.run), "--task", str(self.path), "--phase", phase]
        if cli:
            result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT)] + args, capture_output=True, text=True)
            code, output = result.returncode, result.stdout
        else:
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = gate.main(args)
            output = stream.getvalue()
        response = json.loads(output)
        assert code == expected, (phase, expected, code, response)
        assert response["decision"] == {0: "allow", 1: "fail", 2: "hold"}[expected], response
        if diagnostic:
            assert diagnostic in [d["code"] for d in response["diagnostics"]], response
        after = {str(p): p.read_bytes() for p in self.run.rglob("*") if p.is_file()}
        assert before == after, "validator mutated fixture"
        return response


class GateTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)

    def test_cli_three_phases_exitcodes(self):
        for phase in ("dispatch", "entry", "checkpoint"):
            self.f.record["status"] = "registered"
            if phase == "entry":
                self.f.delivery()
            self.f.check(phase, cli=True)
            self.f.write()
            self.f.path.write_text("{invalid")
            self.f.check(phase, 1, "json_invalid", cli=True, rewrite=False)
            self.f.write()
            self.f.path.unlink()
            self.f.check(phase, 2, "file_missing", cli=True, rewrite=False)
        print("CLI dispatch/entry/checkpoint: allow=0 malformed=1 missing-evidence=2; read-only verified")

    def test_pause_and_late_entry(self):
        for kind in ("root", "optimization", "control"):
            self.f.root.update(status="running", harness_optimization_pause={})
            self.f.control["paused"] = False
            if kind == "root":
                self.f.root["status"] = "paused_for_harness_optimization"
            elif kind == "optimization":
                self.f.root["harness_optimization_pause"]["new_scientific_tasks_and_source_requests_paused"] = True
            else:
                self.f.control["paused"] = True
            self.f.check(expected=1, diagnostic="run_paused")
            self.f.delivery()
            self.f.check("entry", 1, "run_paused")
            self.f.check("checkpoint", 0, "preserve_only")
            self.f.record["status"] = "registered"

    def test_epoch_run_id_and_healthy_reuse(self):
        for phase in ("dispatch", "entry", "checkpoint"):
            self.f.task["epoch"] = 0
            self.f.check(phase, 1, "epoch_stale")
        self.f.task["epoch"] = 1
        self.f.root.pop("run_id")
        self.f.check()
        old = copy.deepcopy(self.f.task)
        old["task_id"] = "previous"
        old["task_scope"]["limits"] = {"read_local": 1}
        row = {"ref": self.f.put("已完成任务.json", old), "agent_id": "agent1", "stage": old["stage"], "status": "completed",
               "formed_at": TIME, "delivered_at": TIME, "material_ref": self.f.input}
        self.f.control["tasks"]["previous"] = row
        self.f.check()
        row["status"] = "started"
        self.f.check(expected=1, diagnostic="agent_busy")
        row["status"] = "completed"
        self.f.task["run_id"] = "other"
        self.f.check(expected=1, diagnostic="run_id")

    def test_replay_and_started_continuation(self):
        for status in ("delivered", "accepted", "started", "completed"):
            self.f.delivery(status)
            self.f.check(expected=1, diagnostic="task_replay")
        self.f.delivery()
        self.f.check("entry")
        self.f.check("entry")
        self.assertEqual(self.f.record["status"], "started")
        self.f.record["status"] = "registered"
        self.f.check(expected=1, diagnostic="task_replay")

    def test_route_scope_optional_duplicate(self):
        for field in ("agent_id", "stage"):
            value = self.f.record[field]
            self.f.record[field] = "other"
            self.f.check(expected=1, diagnostic="route_mismatch")
            self.f.record[field] = value
        for field in ("epoch", "role_ref", "task_scope", "input_refs", "output_dir"):
            self.f.record[field] = "wrong"
            self.f.check(expected=1, diagnostic="record_mismatch")
            del self.f.record[field]
        self.f.task["task_scope"]["allowed_stages"] = ["seeds"]
        self.f.check(expected=1, diagnostic="scope_stage")

    def test_hash_and_input_paths(self):
        for filename in ("角色.json", "输入.json", "任务.json"):
            self.f.write()
            target = self.f.run / filename
            original = target.read_bytes()
            target.write_bytes(original + b" ")
            self.f.check(expected=1, diagnostic="hash_mismatch", rewrite=False)
            target.write_bytes(original)
        self.f.task["input_refs"] = [{"path": ["wrong-type"], "sha256": "0" * 64}]
        self.f.check(expected=1, diagnostic="type")
        self.f.task["input_refs"] = [{"path": "missing", "sha256": "0" * 64}]
        self.f.check(expected=2, diagnostic="file_missing")
        self.f.task["input_refs"] = [{"path": "输入.json", "sha256": "wrong"}]
        self.f.check(expected=1, diagnostic="hash_format")

    def test_output_paths_and_symlinks(self):
        for path in ("../outside", "小组/../../outside", "状态.json", "小组/../"):
            self.f.task["output_dir"] = path
            self.f.check(expected=1, diagnostic="output_escape")
        outside = self.f.run.parent / "外部"
        outside.mkdir()
        link = self.f.run / "小组" / "链接"
        link.symlink_to(outside, target_is_directory=True)
        self.f.task["output_dir"] = "小组/链接/new"
        self.f.check(expected=1, diagnostic="output_escape")
        self.f.task["output_dir"] = "检索支持/new"
        self.f.check()
        (self.f.run / "小组" / "一组").rmdir()
        link.unlink()
        (self.f.run / "小组").rmdir()
        (self.f.run / "小组").symlink_to(outside, target_is_directory=True)
        self.f.task["output_dir"] = "小组/new"
        self.f.check(expected=1, diagnostic="output_escape")

    def test_safe_types_and_json(self):
        for field, value in (("epoch", True), ("epoch", "1"), ("schema_version", True), ("input_refs", {}), ("recovery_of", [])):
            old = self.f.task.get(field)
            self.f.task[field] = value
            self.f.check(expected=2 if field == "schema_version" else 1)
            if old is None:
                del self.f.task[field]
            else:
                self.f.task[field] = old
        for raw in ('{"schema_version":1,"schema_version":1}', '{"x":NaN}', '{"x":1e999}', '[]'):
            self.f.write()
            self.f.path.write_text(raw)
            self.f.check(expected=1, rewrite=False)
        self.f.control["paused"] = "false"
        self.f.check(expected=1, diagnostic="type")

    def test_permissions_and_refusals(self):
        self.f.control["permissions"]["denied_actions"] = ["read_local"]
        self.f.check(expected=1, diagnostic="permission_denied")
        self.f.control["permissions"]["denied_actions"] = []
        self.f.control["restriction_refs"] = [self.f.put("拒绝.json", {"denied_actions": ["read_local"]})]
        self.f.check(expected=1, diagnostic="permission_denied")
        self.f.control["restriction_refs"] = []
        self.f.record["refusal_actions"] = ["read_local"]
        self.f.check(expected=1, diagnostic="permission_denied")
        self.f.record["refusal_actions"] = []
        self.f.task["task_scope"]["allowed_actions"] = ["ssh"]
        self.f.control["permissions"]["allowed_actions"].append("ssh")
        self.f.check(expected=2, diagnostic="action_unsupported")

    def test_delivery_facts(self):
        self.f.delivery("delivered")
        self.f.check("entry", 1, "acceptance_fact_missing")
        self.assertNotIn("read_at", self.f.record)
        self.assertNotIn("archived_at", self.f.record)
        self.f.record["status"] = "accepted"
        self.f.check("entry", 1, "acceptance_fact_missing")
        self.f.record["accepted_at"] = TIME
        self.f.check("entry")  # Administrative acceptance does not assert scientific output reading.
        self.f.record.update(read_at=TIME, reader_id="reader")
        self.f.check("entry")
        self.f.record.pop("delivered_at")
        self.f.check("entry", 1, "delivery_fact_missing")
        self.f.record["delivered_at"] = "2026-10-14T09:00:00Z"
        self.f.check("entry", 1, "fact_order")

    def test_completed_legacy_delivery_evidence_is_required(self):
        self.f.record["status"] = "completed"
        self.f.check("checkpoint", 1, "delivery_fact_missing")
        self.f.record["delivered_at"] = TIME
        self.f.check("checkpoint", 1, "delivery_fact_missing")
        self.f.delivery("completed")
        self.f.check("checkpoint")
        self.assertNotIn("read_at", self.f.record)
        self.assertNotIn("reader_id", self.f.record)

    def test_resolved_failure_rejects_completed_replacement_without_delivery(self):
        f = self.f
        f.task["agent_id"] = f.record["agent_id"] = "old-agent"
        f.recovery()
        f.delivery("completed")
        replacement = f.record
        self.next_task("next", "old-agent")
        f.check()  # The delivered legacy replacement resolves the stopped ancestor.
        saved = copy.deepcopy(replacement)
        for key in ("formed_at", "material_ref", "delivered_at", "started_at", "accepted_at"):
            replacement.pop(key, None)
        f.check(expected=2, diagnostic="delivery_fact_missing")
        replacement["delivered_at"] = TIME
        f.check(expected=2, diagnostic="delivery_fact_missing")
        replacement.update(saved)
        f.check()

    def test_partial_checkpoint(self):
        self.f.root["status"] = "paused"
        self.f.record["checkpoint"]["missing_sections"] = ["实读ACK", "来源终态"]
        self.f.check("checkpoint", 0, "checkpoint_incomplete")
        self.f.record.pop("checkpoint")
        self.f.check("checkpoint", 1, "type")

    def test_recovery_once_real_stop(self):
        self.f.recovery()
        self.f.check()
        self.f.control["recoveries"]["old/f1"]["replacement_task_id"] = "old"
        self.f.check(expected=1, diagnostic="recovery_replay")
        self.f.control["recoveries"]["old/f1"]["replacement_task_id"] = "t1"
        self.f.prior["runtime_state"] = "unknown"
        self.f.check(expected=1, diagnostic="runtime_not_stopped")
        self.f.prior["runtime_state"] = "stopped"
        for rec in ({"message": "No task found"}, {"status": "failed", "success": False}, dict(self.f.stop, runtime_id="wrong"), dict(self.f.stop, success=1), dict(self.f.stop, message="No task found")):
            self.f.prior["stop_receipt_ref"] = self.f.put("停止收据.json", rec)
            self.f.check(expected=1, diagnostic="stop_receipt_invalid")
        self.f.prior["stop_receipt_ref"] = self.f.put("停止收据.json", self.f.stop)
        other = copy.deepcopy(self.f.task)
        other.update(task_id="other", agent_id="agent2")
        self.f.control["tasks"]["other"] = {"ref": self.f.put("另一恢复.json", other), "agent_id": "agent2", "stage": other["stage"], "status": "registered"}
        self.f.check(expected=1, diagnostic="recovery_replay")

    def test_recovery_retains_refusal(self):
        self.f.recovery()
        self.f.prior["refusal_actions"] = ["read_local"]
        self.f.check(expected=1, diagnostic="permission_denied")

    def test_source_owner_inflight_stop_exhaustion(self):
        self.f.source()
        self.f.check()
        self.f.account["owner"] = self.f.contract["owner"] = "other"
        self.f.check(expected=1, diagnostic="source_owner")
        self.f.account["owner"] = self.f.contract["owner"] = "agent1"
        self.f.account["inflight"] = ["request1"]
        self.f.check(expected=1, diagnostic="source_inflight")
        self.f.account["inflight"] = []
        for field in ("protected", "permission_denied", "stopped"):
            self.f.account[field] = True
            self.f.check(expected=1, diagnostic="source_" + field)
            self.f.account[field] = False
        self.f.account.update(used=copy.deepcopy(LIMIT), remaining=copy.deepcopy(ZERO))
        self.f.check(expected=1, diagnostic="source_exhausted")
        self.f.check("checkpoint", 0, "source_exhausted")
        self.f.account["remaining"] = copy.deepcopy(LIMIT)
        self.f.check(expected=1, diagnostic="budget_mismatch")

    def test_shared_unknown_and_independent_account(self):
        self.f.source()
        self.f.account["shared_required"] = True
        self.f.check(expected=2, diagnostic="shared_budget_unknown")
        self.f.account["shared_budget"] = {"limits": LIMIT, "remaining": LIMIT, "account_ids": ["a1"]}
        self.f.check(expected=2, diagnostic="budget_unknown")
        self.f.account["shared_budget"]["used"] = copy.deepcopy(ZERO)
        self.f.check()
        self.f.account["shared_budget"]["account_ids"] = ["a1", "a2"]
        self.f.check(expected=2, diagnostic="shared_pool_unknown")
        self.f.account["shared_required"] = False
        self.f.ledger.update(cumulative_get=10000, cumulative_query=10000)
        other = copy.deepcopy(self.f.account)
        other.update(uris=["https://other.invalid/b"], used=copy.deepcopy(LIMIT), remaining=copy.deepcopy(ZERO))
        self.f.ledger["accounts"]["a2"] = other
        self.f.check()

    def test_alias_new_task_owner_epoch_do_not_reset(self):
        self.f.source()
        self.f.contract["uri"] = self.f.account["uris"][1]
        self.f.account.update(used=copy.deepcopy(LIMIT), remaining=copy.deepcopy(ZERO))
        self.f.contract["contract_id"] = "new-contract"
        self.f.task["epoch"] = self.f.control["epoch"] = 2
        self.f.account["owner"] = self.f.contract["owner"] = self.f.task["agent_id"] = self.f.record["agent_id"] = "new-owner"
        self.f.check(expected=1, diagnostic="source_exhausted")
        self.f.ledger["accounts"]["new"] = copy.deepcopy(self.f.account)
        self.f.check(expected=1, diagnostic="alias_account_conflict")

    def test_cache_and_safety_unknown(self):
        self.f.source(cache=True)
        self.f.check()
        self.f.account["cache"]["ref"] = dict(self.f.input, sha256="0" * 64)
        self.f.check(expected=1, diagnostic="hash_mismatch")
        self.f.account["cache"] = {"status": "unknown"}
        self.f.check(expected=2, diagnostic="cache_unavailable")
        self.f.account["cache"] = {"status": "available", "ref": self.f.input}
        self.f.account.pop("protected")
        self.f.check(expected=2, diagnostic="source_fact_unknown")

    def test_history_preserves_usage_stop_alias_inflight(self):
        self.f.source()
        self.f.history(used=copy.deepcopy(LIMIT))
        self.f.check(expected=1, diagnostic="usage_reset")
        for field in ("protected", "permission_denied", "stopped"):
            self.f.history(**{field: True})
            self.f.check(expected=1, diagnostic="restriction_reset")
        self.f.history(inflight=["request-old"])
        self.f.check(expected=2, diagnostic="inflight_history_lost")
        self.f.account["terminal_receipt_refs"] = {"request-old": self.f.put("终态.json", {"request_id": "request-old", "terminal": True, "finished_at": TIME})}
        self.f.check()
        self.f.history(uris=["https://forgotten.invalid/a"])
        self.f.check(expected=1, diagnostic="alias_history_lost")

    def test_contract_revision_preserves_used(self):
        self.f.source()
        self.f.contract["uri"] = "https://unknown.invalid/a"
        self.f.check(expected=1, diagnostic="alias_account_conflict")
        self.f.contract["uri"] = self.f.account["uris"][0]
        old = copy.deepcopy(self.f.contract)
        old["used_floor"] = copy.deepcopy(LIMIT)
        oldref = self.f.put("旧合同.json", old)
        self.f.account["used"] = copy.deepcopy(LIMIT)
        self.f.account["limits"]["initial_get"] = 2
        self.f.account["remaining"]["initial_get"] = 1
        self.f.contract["limits"]["initial_get"] = 2
        self.f.contract["used_floor"] = copy.deepcopy(LIMIT)
        previous = self.f.history(used=copy.deepcopy(LIMIT), limits=copy.deepcopy(LIMIT))
        self.f.contract.update(revision_of=oldref, previous_account_ref=previous)
        self.f.contract["authorization_ref"] = self.f.put("授权.json", {"authorized": True, "contract_id": "c1", "account_id": "a1",
            "previous_sha256": oldref["sha256"], "limits": self.f.contract["limits"], "constraints": self.f.contract["constraints"],
            "authorized_by": "actual-user", "at": TIME})
        self.f.check()
        self.f.contract["used_floor"] = copy.deepcopy(ZERO)
        self.f.check(expected=1, diagnostic="usage_reset")
        self.f.contract["used_floor"] = copy.deepcopy(LIMIT)
        self.f.contract.pop("previous_account_ref")
        self.f.check(expected=2, diagnostic="revision_required")
        self.f.contract["previous_account_ref"] = previous
        self.f.contract["authorization_ref"] = self.f.put("授权.json", {"authorized": False,
            "limits": self.f.contract["limits"], "constraints": self.f.contract["constraints"]})
        self.f.check(expected=1, diagnostic="revision_unauthorized")

    def test_legacy_adapter_hold_without_guessing(self):
        self.f.task["task_scope"].update(allowed_actions=["source_get"], limits={"source_get": 1})
        self.f.task["contract_refs"] = [self.f.put("旧合同.json", {"task": "legacy", "owner": "agent1"})]
        c = {"task": "legacy", "url": "https://old.invalid/a", "owner": "agent1", "max_initial_GET": 1,
             "legacy_initial_used": 0, "initial_GET_used_this_run": 1, "remaining_initial_GET": 0, "shared_required": True}
        archive = self.f.put("旧合同档案.json", {"contracts": [c]})
        self.f.control["source_ledger_ref"] = self.f.put("旧来源账.json", {"legacy_totals": {"GET": 107, "query": 19},
            "new_budget": {"query_requests": 19, "contracts_archive": archive,
                           "contracts": [{"task": "legacy", "state": "finished", "initial_GET_used_this_run": 1}]}})
        response = self.f.check(expected=2, diagnostic="legacy_unresolved")
        mapped = json.loads(next(x["detail"] for x in response["diagnostics"] if x["code"] == "legacy_exact_fields"))
        self.assertEqual((mapped[0]["used_this_run"], mapped[0]["limit"]), (1, 1))

    def test_source_typed_limits_and_history_cache(self):
        self.f.source()
        self.f.contract["limits"]["initial_get"] = True
        self.f.check(expected=1, diagnostic="type")
        self.f.contract["limits"]["initial_get"] = 1
        self.f.contract["constraints"]["retries"] = False
        self.f.check(expected=1, diagnostic="type")
        self.f.contract["constraints"]["retries"] = 0
        self.f.history(cache={"status": "available", "ref": self.f.input})
        self.f.check(expected=1, diagnostic="cache_history_lost")
        self.f.history(limits={"initial_get": 0, "normal_location": 0, "query": 0})
        self.f.check(expected=2, diagnostic="revision_required")

    def test_duplicate_contract_reservations_and_scope_limit(self):
        self.f.source()
        self.f.task["task_scope"]["limits"]["source_get"] = 0
        self.f.check(expected=1, diagnostic="scope_exhausted")
        self.f.task["task_scope"]["limits"]["source_get"] = 1
        self.f.write()
        self.f.task["contract_refs"] *= 2
        self.f.record["ref"] = self.f.put("任务.json", self.f.task)
        self.f.put("状态.json", self.f.root)
        self.f.check(expected=1, diagnostic="contract_duplicate", rewrite=False)
        # Two independent contract IDs under one canonical account share remaining.
        self.f.task["contract_refs"] = [self.f.task["contract_refs"][0]]
        other = copy.deepcopy(self.f.contract)
        other["contract_id"] = "c2"
        ref = self.f.put("合同二.json", other)
        self.f.ledger["contracts"]["c2"] = ref
        self.f.control["source_ledger_ref"] = self.f.put("来源账.json", self.f.ledger)
        self.f.task["contract_refs"].append(ref)
        self.f.record["ref"] = self.f.put("任务.json", self.f.task)
        self.f.put("状态.json", self.f.root)
        self.f.check(expected=1, diagnostic="source_exhausted", rewrite=False)

    def test_independent_v1_uri_with_immutable_legacy_index(self):
        self.f.source()
        old = {"contracts": [{"task": "old", "url": "https://other.invalid/old",
                              "max_initial_GET": 1, "initial_GET_used_this_run": 1,
                              "shared_required": True, "cache_check": "unknown", "stop_check": "unknown"}]}
        self.f.ledger["legacy_refs"] = [self.f.put("保留原账.json", old)]
        self.f.check()  # Unrelated task-scoped unknown shared condition cannot become global.
        old["contracts"][0]["aliases"] = [self.f.contract["uri"]]
        self.f.ledger["legacy_refs"] = [self.f.put("保留原账.json", old)]
        self.f.check(expected=2, diagnostic="legacy_resource_unmapped")
        old["contracts"][0].pop("aliases")
        old["contracts"][0]["url"] = self.f.contract["uri"]
        self.f.ledger["legacy_refs"] = [self.f.put("保留原账.json", old)]
        self.f.check(expected=2, diagnostic="legacy_resource_unmapped")
        old["contracts"][0]["url"] = "https://other.invalid/old"
        old["shared_required"] = True
        self.f.ledger["legacy_refs"] = [self.f.put("保留原账.json", old)]
        self.f.check(expected=2, diagnostic="legacy_shared_unmapped")

    def test_external_workspace_refs_are_supported(self):
        workspace = Path(self.f.temp.name) / "工作区"
        workspace.mkdir()
        for name in ("组长.md", "AGENTS.md", "判定标准.md"):
            p = workspace / name
            p.write_text("synthetic local instruction")
            ref = {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            if name == "组长.md":
                self.f.task["role_ref"] = ref
            else:
                self.f.task["input_refs"].append(ref)
        self.f.check()

    def next_task(self, task_id="next", agent_id="agent2", recovery=None):
        f = self.f
        f.record["ref"] = f.put(f.task["task_id"] + "_存档任务.json", f.task)
        next_task = copy.deepcopy(f.task)
        next_task.update(task_id=task_id, agent_id=agent_id)
        next_task.pop("recovery_of", None)
        if recovery:
            next_task["recovery_of"] = recovery
        f.task = next_task
        f.record = {"agent_id": agent_id, "stage": next_task["stage"], "status": "registered",
                    "checkpoint": {"saved_refs": [], "missing_sections": []}}
        f.control["tasks"][task_id] = f.record

    def test_regression_ancestor_refusal_survives_second_replacement(self):
        f = self.f
        f.recovery()
        f.prior["refusal_actions"] = ["read_local"]
        f.check(expected=1, diagnostic="permission_denied")
        f.record.update(status="failed", failure_id="f2", runtime_state="stopped", runtime_id="runtime-t1")
        f.record["stop_receipt_ref"] = f.put("停止一.json", {"task_id": "t1", "agent_id": "agent1",
            "runtime_id": "runtime-t1", "status": "stopped", "success": True, "stopped_at": TIME})
        self.next_task("t2", "agent2", {"task_id": "t1", "failure_id": "f2"})
        f.control["recoveries"]["t1/f2"] = {"task_id": "t1", "failure_id": "f2", "replacement_task_id": "t2"}
        f.check(expected=1, diagnostic="permission_denied")

    def test_regression_resolved_failure_allows_normal_role_reuse(self):
        f = self.f
        f.task["agent_id"] = f.record["agent_id"] = "old-agent"
        f.recovery()
        f.delivery("completed")
        self.next_task("next", "old-agent")
        f.check()

    def test_regression_revision_keeps_old_canonical_contract(self):
        f = self.f
        f.source()
        old = copy.deepcopy(f.contract)
        old["used_floor"] = copy.deepcopy(LIMIT)
        oldref = f.put("旧合同保留.json", old)
        f.ledger["contracts"]["c1"] = oldref
        f.contract.update(contract_id="c2", used_floor=copy.deepcopy(LIMIT), revision_of=oldref)
        f.account["used"] = copy.deepcopy(LIMIT)
        previous = f.history()
        f.account["limits"]["initial_get"] = f.contract["limits"]["initial_get"] = 2
        f.account["remaining"]["initial_get"] = 1
        f.contract["previous_account_ref"] = previous
        f.contract["authorization_ref"] = f.put("改订授权.json", {"authorized": True, "contract_id": "c2", "account_id": "a1",
            "previous_sha256": oldref["sha256"], "limits": f.contract["limits"], "constraints": f.contract["constraints"],
            "authorized_by": "actual-user", "at": TIME})
        f.check()
        # Old contract selected for execution still fails against current conditions.
        f.write()
        f.task["contract_refs"] = [oldref]
        f.record["ref"] = f.put("任务.json", f.task)
        f.put("状态.json", f.root)
        f.check(expected=2, rewrite=False)

    def test_regression_stop_receipt_failure_variants(self):
        f = self.f
        f.recovery()
        for message in ("No-task-found", "NO_TASK_FOUND", "No task found", "NoTaskFound", "failed-JSON"):
            with self.subTest(message=message):
                f.prior["stop_receipt_ref"] = f.put("停止收据.json", dict(f.stop, message=message))
                f.check(expected=1, diagnostic="stop_receipt_invalid")

    def test_regression_zero_local_limit_and_unknown_scope(self):
        self.f.task["task_scope"]["limits"]["read_local"] = 0
        self.f.check(expected=1, diagnostic="scope_exhausted")
        self.f.task["task_scope"]["limits"] = {}
        self.f.task["task_scope"]["max_tasks"] = 0
        self.f.check(expected=2, diagnostic="scope_unknown_fields")

    def mapped_legacy(self):
        f = self.f
        f.source()
        f.account["limits"]["initial_get"] = f.contract["limits"]["initial_get"] = 2
        f.account["used"] = f.contract["used_floor"] = copy.deepcopy(LIMIT)
        f.account["remaining"] = copy.deepcopy(LIMIT)
        raw = {"contracts": [{"task": "old", "url": f.contract["uri"], "aliases": [f.account["uris"][1]],
            "max_initial_GET": 2, "initial_GET_used_this_run": 1, "legacy_initial_used": 0,
            "max_normal_Location": 0, "normal_Location_used_this_run": 0, "query_cost": 0,
            "max_bytes": 8388608, "timeout_seconds": 30, "retry": 0,
            "protected": False, "permission_denied": False, "stopped": False, "shared_required": False,
            "cache": {"status": "absent"}}], "inflight": []}
        legacy_ref = f.put("保留旧账.json", raw)
        mapping = {"schema_version": 1, "legacy_ref": legacy_ref, "account_id": "a1", "contract_ids": ["old"],
            "uris": list(f.account["uris"]), "used_basis": "disjoint", "used": copy.deepcopy(LIMIT),
            "limits": copy.deepcopy(f.account["limits"]), "constraints": copy.deepcopy(f.account["constraints"]),
            "facts": {k: copy.deepcopy(f.account[k]) for k in ("protected", "permission_denied", "stopped", "shared_required", "inflight", "cache")},
            "confirmed_by": "agent1", "confirmed_at": TIME}
        return raw, mapping

    def write_mapping(self, raw, mapping):
        f = self.f
        mapping["legacy_ref"] = f.put("保留旧账.json", raw)
        f.ledger["legacy_refs"] = [mapping["legacy_ref"]]
        f.ledger["legacy_mappings"] = [f.put("旧账映射.json", mapping)]

    def test_regression_complete_legacy_mapping_preserves_raw_ledger(self):
        raw, mapping = self.mapped_legacy()
        self.write_mapping(raw, mapping)
        self.f.check(diagnostic="legacy_resource_mapped", cli=True)
        self.assertEqual(self.f.account["used"]["initial_get"], 1)
        self.assertEqual(self.f.account["remaining"]["initial_get"], 1)

    def test_regression_legacy_mapping_missing_evidence_and_usage_reset(self):
        raw, mapping = self.mapped_legacy()
        self.write_mapping(raw, mapping)
        self.f.ledger["legacy_mappings"] = []
        self.f.check(expected=2, diagnostic="legacy_resource_unmapped")
        mapping["used"]["initial_get"] = 0
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_usage_lost")
        mapping["used"]["initial_get"] = 1
        self.f.account["used"]["initial_get"] = 0
        self.f.account["remaining"]["initial_get"] = 2
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_usage_lost")

    def test_regression_legacy_mapping_alias_stop_and_unknown_facts(self):
        raw, mapping = self.mapped_legacy()
        mapping["uris"] = [self.f.contract["uri"]]
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_alias_omitted")
        mapping["uris"] = list(self.f.account["uris"])
        raw["contracts"][0]["protected"] = True
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_restriction_lost")
        raw["contracts"][0]["protected"] = False
        mapping["facts"]["stopped"] = "unknown"
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_facts_unknown")

    def test_regression_legacy_mapping_inflight_and_shared_hold(self):
        raw, mapping = self.mapped_legacy()
        raw["inflight"] = [self.f.contract["uri"]]
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_inflight_lost")
        raw["inflight"] = []
        raw["contracts"][0]["shared_required"] = True
        mapping["facts"]["shared_required"] = self.f.account["shared_required"] = True
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_shared_unmapped")

    def test_recorded_local_counts_and_checkpoint_preservation(self):
        self.f.task["task_scope"]["limits"]["read_local"] = 2
        self.f.record["action_used"] = {"read_local": 1}
        self.f.check(diagnostic="local_counts_record_only")
        self.f.record["action_used"]["read_local"] = 2
        self.f.check(expected=1, diagnostic="scope_exhausted")
        self.f.check("checkpoint", diagnostic="scope_exhausted")
        self.f.record["action_used"]["read_local"] = True
        self.f.check(expected=1, diagnostic="type")

    def test_source_costs_respect_recorded_task_use(self):
        f = self.f
        f.source()
        f.task["task_scope"]["limits"]["source_get"] = 2
        f.record["action_used"] = {"source_get": 1}
        f.contract["costs"]["initial_get"] = 2
        f.contract["limits"]["initial_get"] = f.account["limits"]["initial_get"] = 3
        f.account["remaining"]["initial_get"] = 3
        f.check(expected=1, diagnostic="scope_exhausted")

    def test_legacy_receipt_protection_and_known_shared_budget_retained(self):
        raw, mapping = self.mapped_legacy()
        raw["contracts"][0]["receipt"] = self.f.put("旧请求收据.json", {"protection_detected": True, "http_status": 403})
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_restriction_lost")
        raw["contracts"][0].pop("receipt")
        raw["contracts"][0]["shared_required"] = True
        raw["contracts"][0]["shared_budget"] = {"limits": copy.deepcopy(LIMIT), "used": copy.deepcopy(LIMIT)}
        mapping["facts"]["shared_required"] = self.f.account["shared_required"] = True
        self.f.account["shared_budget"] = {"limits": copy.deepcopy(LIMIT), "used": copy.deepcopy(ZERO),
                                           "remaining": copy.deepcopy(LIMIT), "account_ids": ["a1"]}
        self.write_mapping(raw, mapping)
        self.f.check(expected=2, diagnostic="legacy_shared_unmapped")

    def completed_replacement_with_refusal(self, hops=1):
        f = self.f
        f.recovery()
        f.prior["refusal_actions"] = ["read_local"]
        f.task["task_scope"]["allowed_actions"] = ["generate_local"]
        f.check()  # Replacement may perform this explicitly allowed action.
        if hops > 1:
            f.record.update(status="failed", failure_id="f2", runtime_state="stopped", runtime_id="runtime-t1")
            f.record["stop_receipt_ref"] = f.put("停止中间任务.json", {"task_id": "t1", "agent_id": "agent1",
                "runtime_id": "runtime-t1", "status": "stopped", "success": True, "stopped_at": TIME})
            self.next_task("t2", "agent2", {"task_id": "t1", "failure_id": "f2"})
            f.control["recoveries"]["t1/f2"] = {"task_id": "t1", "failure_id": "f2", "replacement_task_id": "t2"}
            f.check()
        f.delivery("completed")
        self.next_task("healthy", f.task["agent_id"])

    def test_regression_completed_role_retains_recovery_ancestor_refusal(self):
        self.completed_replacement_with_refusal()
        self.f.task["task_scope"]["allowed_actions"] = ["read_local"]
        self.f.check(expected=1, diagnostic="permission_denied")

    def test_regression_multihop_healthy_tasks_keep_only_real_refusals(self):
        self.completed_replacement_with_refusal(hops=2)
        f = self.f
        f.task["task_scope"]["limits"] = {"generate_local": 2}
        f.check(diagnostic="local_counts_record_only")  # Resolved failure does not become recovery_missing.
        f.delivery("completed")
        self.next_task("healthy-again", "agent2")
        f.task["task_scope"]["limits"] = {"generate_local": 3}
        f.task["output_dir"] = "检索支持/new"
        f.check()  # Old per-task counts/directories do not become lifetime restrictions.
        f.task["task_scope"]["allowed_actions"] = ["read_local"]
        f.task["task_scope"]["limits"] = {}
        f.check(expected=1, diagnostic="permission_denied")

    def test_request_zero_cost_and_query_zero_cost_are_rejected(self):
        f = self.f
        f.source()
        f.contract["costs"] = copy.deepcopy(ZERO)
        f.check(expected=1, diagnostic="source_cost")  # Existing all-zero guard.
        f.contract["costs"] = copy.deepcopy(LIMIT)
        f.task["task_scope"].update(allowed_actions=["source_get", "source_query"], limits={"source_get": 1, "source_query": 1})
        f.check(expected=1, diagnostic="source_cost")

    def test_http_get_cannot_use_query_only_cost_to_bypass_exhaustion(self):
        f = self.f
        f.source()
        f.task["task_scope"].update(allowed_actions=["source_get", "source_query"], limits={"source_get": 1, "source_query": 1})
        f.contract["limits"]["query"] = f.account["limits"]["query"] = f.account["remaining"]["query"] = 1
        f.account["used"]["initial_get"] = 1
        f.account["remaining"]["initial_get"] = 0
        f.contract["costs"] = {"initial_get": 0, "normal_location": 0, "query": 1}
        f.check(expected=1, diagnostic="source_cost")

    def test_normal_location_and_pure_query_costs_are_accounted(self):
        f = self.f
        f.source()
        f.contract["limits"]["normal_location"] = f.account["limits"]["normal_location"] = f.account["remaining"]["normal_location"] = 1
        f.contract["costs"] = {"initial_get": 0, "normal_location": 1, "query": 0}
        f.check()  # A follow-up HTTP GET consumes its Location budget and source_get allowance.
        f.task["task_scope"].update(allowed_actions=["source_query"], limits={"source_query": 1})
        f.contract["limits"]["query"] = f.account["limits"]["query"] = f.account["remaining"]["query"] = 1
        f.contract["costs"] = {"initial_get": 0, "normal_location": 0, "query": 1}
        f.check()  # A query-only tool call is not declared as an HTTP GET.

    def test_cache_zero_cost_cannot_authorize_source_request_actions(self):
        f = self.f
        f.source(cache=True)
        f.check()
        f.task["task_scope"]["allowed_actions"].append("source_get")
        f.task["task_scope"]["limits"]["source_get"] = 1
        f.check(expected=1, diagnostic="cache_scope")
        f.task["task_scope"]["allowed_actions"] = ["read_cache"]
        f.task["task_scope"]["limits"] = {}
        f.contract["costs"]["initial_get"] = 1
        f.check(expected=1, diagnostic="cache_scope")

    def scientific(self):
        work = {"schema_version": 1, "decision_question": "候选是否依赖未确认的控制接口？",
                "premises": {"required_facts": [], "allowed_assumptions": ["条件接口可用"], "deferred_unknowns": ["接口型号"]},
                "outcome_routes": {"supported": "继续集中核验", "refuted": "收缩问题", "unknown": "保留条件结论"},
                "deliverable": {"format": "markdown", "version": "v1", "reader_id": "leader"}}
        ref = self.f.put("科学输入.json", {"scientific_work": work, "seeds": []})
        self.f.task["input_refs"].append(ref)
        self.f.task["scientific_work_ref"] = ref
        self.f.task["task_scope"]["allowed_actions"] = ["read_local", "generate_local", "preserve_local"]
        return work

    def scientific_output(self):
        p = self.f.run / "小组" / "一组" / "科学稿.md"
        p.write_text("# 条件判断\n尚未确认接口，当前零种子。\n", encoding="utf-8")
        ref = self.f.ref(p)
        return {"material_ref": ref, "version": "v1", "formed_at": TIME, "delivered_at": TIME,
                "archived_at": TIME, "read_at": TIME, "reader_id": "leader", "read_version": "v1", "read_material_ref": ref}

    def test_administrative_ack_without_output(self):
        for status in ("delivered", "accepted", "started"):
            self.f.record.update(status=status, delivered_at=TIME)
            if status in {"accepted", "started"}:
                self.f.record["accepted_at"] = TIME
            if status == "started":
                self.f.record["started_at"] = TIME
            self.f.check("entry", 1, "acceptance_fact_missing") if status == "delivered" else self.f.check("entry")
        self.f.record.clear()
        self.f.record.update(agent_id="agent1", stage="problem-family", status="accepted", delivery_enqueued_at=TIME,
                             checkpoint={"saved_refs": [], "missing_sections": []})
        self.f.check("entry", 1, "delivery_fact_missing")

    def test_draining_only_checkpoint_and_unknown_hold(self):
        self.f.root["status"] = "draining_existing_tasks"
        self.f.check(expected=1, diagnostic="run_paused")
        self.f.delivery()
        self.f.check("entry", 1, "run_paused")
        self.f.check("checkpoint", 0, "preserve_only")
        self.f.root["status"] = "future_unsupported_status"
        self.f.check("checkpoint", 2, "run_status_unknown")

    def test_public_api_paths_invalid_phase_no_stdout(self):
        self.f.write()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            for run, task in ((self.f.run, self.f.path), (str(self.f.run), str(self.f.path))):
                code, result = gate.check_task(run, task, "dispatch")
                self.assertEqual(code, 0, result)
            for phase in ("future", None, [], 1):
                code, result = gate.check_task(self.f.run, self.f.path, phase)
                self.assertEqual(code, 1, result)
                self.assertEqual(result["diagnostics"][0]["code"], "phase_invalid")
        self.assertEqual(stream.getvalue(), "")

    def test_successful_entry_receipt_uses_verified_snapshot(self):
        self.f.delivery()
        self.f.write()
        code, result = gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 0, result)
        receipt = result["receipt"]
        self.assertEqual(receipt["task_id"], "t1")
        self.assertEqual(receipt["agent_id"], "agent1")
        self.assertEqual(receipt["stage"], "problem-family")
        self.assertEqual(receipt["epoch"], 1)
        self.assertEqual(receipt["phase"], "entry")
        self.assertEqual(receipt["decision"], "allow")
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["root_sha256"], self.f.ref(self.f.run / "状态.json")["sha256"])
        self.assertEqual(receipt["task_sha256"], self.f.ref(self.f.path)["sha256"])
        self.assertIsNotNone(gate.timestamp(receipt["checked_at"], "checked_at").tzinfo)
        code, result = gate.check_task(self.f.run, self.f.path, "dispatch")
        self.assertEqual(code, 1, result)
        self.assertNotIn("receipt", result)

    def test_changed_entry_snapshot_never_emits_receipt(self):
        self.f.delivery()
        self.f.write()
        original = gate.Checker.read
        def changed(checker, path):
            data = original(checker, path)
            if Path(path).resolve() == self.f.path.resolve():
                root = self.f.run / "状态.json"
                root.write_bytes(root.read_bytes() + b" ")
            return data
        with mock.patch.object(gate.Checker, "read", changed):
            code, result = gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "file_changed")
        self.assertNotIn("receipt", result)

    def test_finished_time_alias_timezone_and_conflicts(self):
        self.f.source()
        self.f.history(inflight=["r1"])
        base = {"request_id": "r1", "terminal": True}
        for values, code, diag in (({"finished_at": TIME}, 0, None),
                                   ({"finished_at_utc": TIME}, 0, None),
                                   ({"finished_at": TIME, "finished_at_utc": "2026-10-14T18:00:00+08:00"}, 0, None),
                                   ({"finished_at": TIME, "finished_at_utc": "2026-10-14T18:00:01+08:00"}, 1, "timestamp_conflict"),
                                   ({"finished_at_utc": "2026-10-14T10:00:00"}, 1, "timestamp"),
                                   ({}, 1, "timestamp")):
            self.f.account["terminal_receipt_refs"] = {"r1": self.f.put("终态.json", dict(base, **values))}
            self.f.check(expected=code, diagnostic=diag)

    def test_scientific_opt_in_zero_seed_and_legacy(self):
        ref = self.f.put("已有科学输入.json", {"scientific_work": {"legacy": True}})
        self.f.task["input_refs"].append(ref)
        self.f.check()  # Existing field alone never adopts the new contract.
        self.scientific()
        self.f.check()
        self.f.record.update(status="accepted", delivered_at=TIME, accepted_at=TIME)
        self.f.check("entry")
        self.f.record["status"] = "completed"
        self.f.check("checkpoint", 1, "delivery_fact_missing")
        self.f.record["output_delivery"] = self.scientific_output()
        self.f.check("checkpoint")
        self.f.task["scientific_work_ref"] = ref
        self.f.check("checkpoint", 1, "scientific_schema")
        self.f.task["scientific_work_ref"] = self.f.role
        self.f.check("checkpoint", 1, "scientific_input_ref")

    def test_scientific_read_binding_and_late_delivery(self):
        self.scientific()
        self.f.record.update(status="started", delivered_at=TIME, started_at=TIME)
        output = self.scientific_output()
        for key, value, code in (("version", "v2", "output_read_mismatch"),
                                  ("read_version", "v0", "output_read_mismatch"),
                                  ("reader_id", "administrative-ack", "output_reader"),
                                  ("read_material_ref", self.f.input, "output_read_mismatch")):
            self.f.record["output_delivery"] = dict(output, **{key: value})
            self.f.check("checkpoint", 1, code)
        self.f.record["output_delivery"] = dict(output)
        self.f.record["output_delivery"].pop("read_at")
        self.f.check("checkpoint")
        self.f.record["status"] = "completed"
        self.f.check("checkpoint", 1, "scientific_delivery_missing")
        self.f.record["output_delivery"] = output
        self.f.check("checkpoint")
        material = self.f.run / output["material_ref"]["path"]
        self.f.write()
        material.write_text("换稿", encoding="utf-8")
        self.f.check("checkpoint", 1, "hash_mismatch", rewrite=False)

    def delivery_plan(self, delegated=False):
        work = self.scientific()
        plan = {"mode": "direct", "target": "小组/一组/科学稿.md"}
        if delegated:
            plan.update(mode="delegated-archive", archive_task_id="archive1", archive_agent_id="leader")
            self.f.task["task_scope"]["allowed_actions"] = ["read_local"]
            archive = copy.deepcopy(self.f.task)
            archive.pop("scientific_work_ref")
            archive.update(task_id="archive1", agent_id="leader", input_refs=[self.f.input])
            archive["task_scope"]["allowed_actions"] = ["read_local", "preserve_local"]
            self.f.control["tasks"]["archive1"] = {
                "ref": self.f.put("归档任务.json", archive), "agent_id": "leader", "stage": archive["stage"], "status": "registered"}
        work["deliverable"]["delivery_plan"] = plan
        ref = self.f.put("科学输入.json", {"scientific_work": work, "seeds": []})
        self.f.task["input_refs"][-1] = ref
        self.f.task["scientific_work_ref"] = ref
        return work

    def replace_delivery_work(self, work):
        ref = self.f.put("科学输入.json", {"scientific_work": work, "seeds": []})
        self.f.task["input_refs"][-1] = ref
        self.f.task["scientific_work_ref"] = ref

    def test_scientific_implicit_direct_requires_real_local_actions(self):
        self.scientific()
        self.f.task["task_scope"]["allowed_actions"] = ["read_local"]
        self.f.check(expected=2, diagnostic="scientific_delivery_permission", cli=True)
        self.f.record.update(status="accepted", delivered_at=TIME, accepted_at=TIME)
        self.f.check("entry", 2, "scientific_delivery_permission")
        self.f.check("checkpoint")  # Old formed/read facts remain preservable without new admission.
        self.f.task["task_scope"]["allowed_actions"].extend(["generate_local", "preserve_local"])
        self.f.control["permissions"]["denied_actions"] = ["preserve_local"]
        self.f.check("entry", 1, "permission_denied")

    def test_scientific_direct_plan_and_cli(self):
        self.delivery_plan()
        self.f.check(cli=True)
        self.f.record.update(status="accepted", delivered_at=TIME, accepted_at=TIME)
        self.f.check("entry", cli=True)
        self.f.record.update(status="completed", output_delivery=self.scientific_output())
        self.f.check("checkpoint")

    def test_scientific_delegated_archive_has_independent_authority(self):
        self.delivery_plan(delegated=True)
        self.f.control["permissions"]["denied_actions"] = ["generate_local"]
        self.f.check(cli=True)
        self.f.record.update(status="accepted", delivered_at=TIME, accepted_at=TIME)
        self.f.check("entry")
        self.f.control["permissions"]["denied_actions"].append("preserve_local")
        self.f.check("entry", 1, "permission_denied")

    def test_scientific_archive_task_actions_quota_and_refusals(self):
        self.delivery_plan(delegated=True)
        row = self.f.control["tasks"]["archive1"]
        archive = json.loads((self.f.run / row["ref"]["path"]).read_bytes())
        archive["task_scope"]["allowed_actions"] = ["read_local"]
        row["ref"] = self.f.put("归档任务.json", archive)
        self.f.check(expected=2, diagnostic="scientific_delivery_permission")
        archive["task_scope"]["allowed_actions"].append("preserve_local")
        archive["task_scope"]["limits"] = {"preserve_local": 1}
        row["ref"] = self.f.put("归档任务.json", archive)
        row["action_used"] = {"preserve_local": 1}
        self.f.check(expected=1, diagnostic="scope_exhausted")
        row["action_used"] = {}
        row["refusal_actions"] = ["preserve_local"]
        self.f.check(expected=1, diagnostic="permission_denied")
        row.pop("refusal_actions")
        self.f.control["restriction_refs"] = [self.f.put("归档限制.json", {"denied_actions": ["preserve_local"]})]
        self.f.check(expected=1, diagnostic="permission_denied")

    def test_scientific_archive_owner_and_registered_task_binding(self):
        work = self.delivery_plan(delegated=True)
        row = self.f.control["tasks"]["archive1"]
        row["status"] = "completed"
        self.f.check(expected=1, diagnostic="archive_task_terminal")
        row["status"] = "registered"
        row["agent_id"] = "other"
        self.f.check(expected=1, diagnostic="route_mismatch")
        row["agent_id"] = "leader"
        work["deliverable"]["delivery_plan"]["archive_agent_id"] = "other"
        self.replace_delivery_work(work)
        self.f.check(expected=1, diagnostic="archive_owner")

    def test_scientific_archive_cross_group_and_epoch_rejected(self):
        self.delivery_plan(delegated=True)
        row = self.f.control["tasks"]["archive1"]
        archive = json.loads((self.f.run / row["ref"]["path"]).read_bytes())
        archive["output_dir"] = "小组/另一组"
        row["ref"] = self.f.put("归档任务.json", archive)
        self.f.check(expected=1, diagnostic="archive_owner")
        archive["output_dir"] = self.f.task["output_dir"]
        archive["epoch"] = 2
        row["ref"] = self.f.put("归档任务.json", archive)
        self.f.check(expected=1, diagnostic="epoch_stale")

    def test_scientific_target_and_material_must_match_this_task(self):
        work = self.delivery_plan()
        for target in ("小组/另一组/科学稿.md", "../科学稿.md", str(Path(self.f.temp.name) / "科学稿.md")):
            work["deliverable"]["delivery_plan"]["target"] = target
            self.replace_delivery_work(work)
            self.f.check(expected=1, diagnostic="output_escape")
        work["deliverable"]["delivery_plan"]["target"] = "小组/一组/约定原件.md"
        self.replace_delivery_work(work)
        self.f.record.update(status="completed", delivered_at=TIME, output_delivery=self.scientific_output())
        self.f.check("checkpoint", 1, "output_target")
        outside = self.f.run / "小组" / "另一组" / "科学稿.md"
        outside.parent.mkdir()
        outside.write_text("# 另一组原件\n", encoding="utf-8")
        ref = self.f.ref(outside)
        self.f.record["output_delivery"].update(material_ref=ref, read_material_ref=ref)
        self.f.check("checkpoint", 1, "output_escape")

    def test_scientific_target_parent_symlink_cannot_change_group(self):
        work = self.delivery_plan()
        other = self.f.run / "小组" / "另一组"
        other.mkdir()
        link = self.f.run / "小组" / "一组" / "别名"
        link.symlink_to(other, target_is_directory=True)
        work["deliverable"]["delivery_plan"]["target"] = "小组/一组/别名/科学稿.md"
        self.replace_delivery_work(work)
        self.f.check(expected=1, diagnostic="output_escape")

    def test_scientific_checkpoint_does_not_require_new_archive_admission(self):
        self.delivery_plan(delegated=True)
        self.f.record.update(status="completed", delivered_at=TIME, output_delivery=self.scientific_output())
        self.f.control["tasks"]["archive1"]["status"] = "stopped"
        self.f.control["permissions"]["denied_actions"] = ["preserve_local"]
        self.f.check("checkpoint")

    def test_scientific_delegated_retains_prior_agent_and_recovery_refusals(self):
        self.delivery_plan(delegated=True)
        archive_row = self.f.control["tasks"]["archive1"]
        archive = json.loads((self.f.run / archive_row["ref"]["path"]).read_bytes())
        prior = copy.deepcopy(archive)
        prior.update(task_id="prior-archive", agent_id="old-owner")
        stop = {"task_id": prior["task_id"], "agent_id": prior["agent_id"], "runtime_id": "old-runtime",
                "status": "stopped", "success": True, "stopped_at": TIME}
        self.f.control["tasks"][prior["task_id"]] = {
            "ref": self.f.put("旧归档任务.json", prior), "agent_id": prior["agent_id"], "stage": prior["stage"],
            "status": "failed", "failure_id": "archive-failure", "runtime_id": "old-runtime", "runtime_state": "stopped",
            "stop_receipt_ref": self.f.put("旧归档停止.json", stop), "refusal_actions": ["preserve_local"]}
        archive["recovery_of"] = {"task_id": prior["task_id"], "failure_id": "archive-failure"}
        archive_row["ref"] = self.f.put("归档任务.json", archive)
        self.f.control["recoveries"]["prior-archive/archive-failure"] = {
            "task_id": prior["task_id"], "failure_id": "archive-failure", "replacement_task_id": "archive1"}
        self.f.check(expected=1, diagnostic="permission_denied")
        self.f.control["tasks"][prior["task_id"]]["refusal_actions"] = []
        self.f.check()
        archive.pop("recovery_of")
        archive_row["ref"] = self.f.put("归档任务.json", archive)
        # The separate healthy-reuse case needs a matching stop and no unresolved failure.
        prior["agent_id"] = stop["agent_id"] = "leader"
        self.f.control["recoveries"].clear()
        previous_row = self.f.control["tasks"][prior["task_id"]]
        previous_row.pop("failure_id")
        previous_row.update(ref=self.f.put("旧归档任务.json", prior), agent_id="leader", status="stopped",
                            stop_receipt_ref=self.f.put("旧归档停止.json", stop), refusal_actions=["preserve_local"])
        self.f.check(expected=1, diagnostic="permission_denied")

    def test_scientific_plan_boolean_self_delegation_and_unknown_fields_rejected(self):
        work = self.delivery_plan()
        work["deliverable"]["delivery_plan"] = True
        self.replace_delivery_work(work)
        self.f.check(expected=1, diagnostic="type")
        work["deliverable"]["delivery_plan"] = {"mode": "direct", "target": "小组/一组/科学稿.md", "authorized": True}
        self.replace_delivery_work(work)
        self.f.check(expected=1, diagnostic="delivery_plan_fields")
        work["deliverable"]["delivery_plan"] = {"mode": "delegated-archive", "target": "小组/一组/科学稿.md",
                                                "archive_task_id": "t1", "archive_agent_id": "leader"}
        self.replace_delivery_work(work)
        self.f.check(expected=1, diagnostic="archive_owner")

    def inspect_tasks(self, count=35):
        for i in range(count):
            task = copy.deepcopy(self.f.task)
            tid = "t%03d" % i
            task.update(task_id=tid, agent_id="agent%d" % (i % 2))
            row = copy.deepcopy(self.f.record)
            row.update(ref=self.f.put("任务/%s.json" % tid, task), agent_id=task["agent_id"],
                       checkpoint={"missing_sections": ["长中文" * 10000] * 30})
            self.f.control["tasks"][tid] = row
        self.f.root["huge_history"] = "累计账正文不可输出" * 10000
        self.f.write()

    def test_inspect_bounded_pagination_filter_and_cli(self):
        self.inspect_tasks()
        seen = []
        cursor = None
        while True:
            code, result = gate.inspect_run(self.f.run, limit=20, cursor=cursor)
            self.assertEqual(code, 0, result)
            self.assertFalse(result["action_permission"])
            self.assertEqual(result["total"], 36)
            encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.assertLessEqual(len(encoded) + 1, 12288)
            self.assertNotIn("累计账正文", encoded.decode())
            seen.extend(row["task_id"] for row in result["tasks"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(seen, sorted(self.f.control["tasks"]))
        code, result = gate.inspect_run(self.f.run, status="registered", agent="agent0", limit=100)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["total"], 18)
        code, result = gate.inspect_run(self.f.run, task=self.f.path)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["total"], 1)
        for limit in (0, 101, True):
            self.assertEqual(gate.inspect_run(self.f.run, limit=limit)[0], 1)
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "inspect", "--run", str(self.f.run)], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertLessEqual(len(result.stdout), 12288)

    def test_inspect_snapshot_and_target_changes(self):
        self.inspect_tasks()
        _, result = gate.inspect_run(self.f.run, limit=1)
        cursor = result["next_cursor"]
        self.f.root["status"] = "paused_by_user"
        self.f.write()
        code, result = gate.inspect_run(self.f.run, limit=1, cursor=cursor)
        self.assertEqual(code, 1)
        self.assertEqual(result["diagnostics"][0]["code"], "inspect_snapshot_changed")
        self.f.path.write_bytes(self.f.path.read_bytes() + b" ")
        code, result = gate.inspect_run(self.f.run, task="t1")
        self.assertEqual(code, 1)
        self.assertEqual(result["diagnostics"][0]["code"], "hash_mismatch")
        self.f.write()
        original = gate.Checker.ref
        def changed(checker, ref, as_json=False):
            result = original(checker, ref, as_json)
            p = checker.run / "状态.json"
            p.write_bytes(p.read_bytes() + b" ")
            return result
        with mock.patch.object(gate.Checker, "ref", changed):
            code, result = gate.inspect_run(self.f.run, task="t1")
        self.assertEqual(code, 1)
        self.assertEqual(result["diagnostics"][0]["code"], "file_changed")

    def test_six_get_terminal_structure_offline(self):
        self.f.source()
        limit = {"initial_get": 6, "normal_location": 0, "query": 0}
        self.f.contract["limits"] = copy.deepcopy(limit)
        self.f.account.update(limits=copy.deepcopy(limit), used=copy.deepcopy(limit), remaining=copy.deepcopy(ZERO))
        self.f.history(inflight=["get%d" % i for i in range(6)])
        self.f.account["terminal_receipt_refs"] = {
            "get%d" % i: self.f.put("收据/GET%d.json" % i, {"request_id": "get%d" % i,
                "terminal": True, "finished_at_utc": TIME, "status": "failed", "source_denial": False}) for i in range(6)}
        self.f.root["status"] = "paused_by_user"
        self.f.check("checkpoint", 0, "source_exhausted")
        del self.f.account["terminal_receipt_refs"]["get4"]
        self.f.check("checkpoint", 2, "inflight_history_lost")

    def old_same_agent(self, status="stopped"):
        old = copy.deepcopy(self.f.task)
        old["task_id"] = "prior"
        row = {"ref": self.f.put("先前任务.json", old), "agent_id": "agent1", "stage": old["stage"], "status": status}
        self.f.control["tasks"]["prior"] = row
        return row

    def stop_prior(self, row):
        receipt = {"task_id": "prior", "agent_id": "agent1", "runtime_id": "prior-runtime", "status": "stopped",
                   "success": True, "stopped_at": TIME}
        row.update(runtime_id="prior-runtime", runtime_state="stopped", started_at=TIME, stopped_at=TIME,
                   stop_receipt_ref=self.f.put("普通复用停止.json", receipt))
        return receipt

    def test_audit_reuse_independently_requires_real_stop(self):
        row = self.old_same_agent()
        self.f.check(expected=2, diagnostic="stop_evidence_missing")
        row.update(runtime_state="running", runtime_id="still-running")
        self.f.check(expected=1, diagnostic="runtime_not_stopped")
        row["status"] = "failed"
        row["runtime_state"] = "stopped"
        self.f.check(expected=2, diagnostic="stop_evidence_missing")
        receipt = self.stop_prior(row)
        self.f.check()
        row["stop_receipt_ref"] = self.f.put("普通复用停止.json", dict(receipt, success=False))
        self.f.check(expected=1, diagnostic="stop_receipt_invalid")
        row["stop_receipt_ref"] = self.f.put("普通复用停止.json", dict(receipt, runtime_id="wrong"))
        self.f.check(expected=1, diagnostic="stop_receipt_invalid")
        row["stop_receipt_ref"] = self.f.put("普通复用停止.json", dict(receipt, stopped_at="2026-10-14T09:59:59Z"))
        self.f.check(expected=1, diagnostic="stop_receipt_invalid")

    def test_audit_reuse_completion_facts_and_checkpoint_preservation(self):
        row = self.old_same_agent("completed")
        self.f.check(expected=2, diagnostic="delivery_fact_missing")
        self.f.check("checkpoint")  # The new reuse gate does not block preservation.
        row.update(formed_at=TIME, delivered_at=TIME, material_ref=self.f.input)
        self.f.check()
        row["status"] = "stopped"
        row.pop("material_ref")
        row["runtime_state"] = "running"
        self.f.check("checkpoint")

    def test_audit_reuse_explicit_science_cannot_use_bare_completion(self):
        self.scientific()
        row = self.old_same_agent("completed")
        row.update(formed_at=TIME, delivered_at=TIME, material_ref=self.f.input)
        self.f.check(expected=2, diagnostic="scientific_delivery_missing")
        row["output_delivery"] = self.scientific_output()
        self.f.check()

    def test_audit_acceptance_required_before_entry_receipt(self):
        self.f.record.update(status="delivered", delivered_at=TIME)
        for status in ("delivered", "accepted"):
            self.f.record["status"] = status
            result = self.f.check("entry", 1, "acceptance_fact_missing")
            self.assertNotIn("receipt", result)
        self.f.record["accepted_at"] = TIME
        self.f.check("entry")
        self.f.record.update(status="started", started_at="2026-10-14T09:59:59Z")
        self.f.check("entry", 1, "fact_order")
        self.f.record.pop("accepted_at")
        self.f.check("checkpoint")  # Historical unknown acceptance is not backfilled.

    def link_input(self, parent=False):
        a, b = self.f.run / "目标甲", self.f.run / "目标乙"
        a.mkdir(); b.mkdir()
        for folder in (a, b):
            (folder / "输入.json").write_bytes((self.f.run / self.f.input["path"]).read_bytes())
        link = self.f.run / "原引用"
        link.symlink_to(a if parent else a / "输入.json", target_is_directory=parent)
        ref = {"path": "原引用/输入.json" if parent else "原引用", "sha256": self.f.input["sha256"]}
        self.f.task["input_refs"] = [ref]
        return link, b if parent else b / "输入.json"

    def test_audit_fixed_links_and_changed_file_parent_bindings(self):
        link, target = self.link_input()
        self.f.check()
        self.f.write()
        original = gate.Checker.ref
        def swapped(checker, ref, *args, **kwargs):
            result = original(checker, ref, *args, **kwargs)
            if ref.get("path") == "原引用":
                link.unlink(); link.symlink_to(target)
            return result
        with mock.patch.object(gate.Checker, "ref", swapped):
            self.f.check(expected=1, diagnostic="path_changed", rewrite=False)

    def test_audit_parent_link_change_at_final_gate_recheck(self):
        link, target = self.link_input(parent=True)
        self.f.write()
        original = gate.Checker.recheck
        def swapped(checker, *args, **kwargs):
            link.unlink(); link.symlink_to(target, target_is_directory=True)
            return original(checker, *args, **kwargs)
        with mock.patch.object(gate.Checker, "recheck", swapped):
            self.f.check(expected=1, diagnostic="path_changed", rewrite=False)

    def test_audit_raw_run_and_task_entry_bindings(self):
        self.f.write()
        run_link = Path(self.f.temp.name) / "运行入口"
        task_link = self.f.run / "任务入口.json"
        run_link.symlink_to(self.f.run, target_is_directory=True)
        task_link.symlink_to(self.f.path)
        self.assertEqual(gate.check_task(run_link, task_link, "dispatch")[0], 0)
        clone = Path(self.f.temp.name) / "另一运行"
        shutil.copytree(self.f.run, clone, symlinks=True)
        original = gate.Checker.recheck
        def moved_run(checker, *args, **kwargs):
            run_link.unlink(); run_link.symlink_to(clone, target_is_directory=True)
            return original(checker, *args, **kwargs)
        with mock.patch.object(gate.Checker, "recheck", moved_run):
            code, result = gate.check_task(run_link, task_link, "dispatch")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "path_changed")
        run_link.unlink(); run_link.symlink_to(self.f.run, target_is_directory=True)
        alternate = self.f.run / "同内容任务.json"
        alternate.write_bytes(self.f.path.read_bytes())
        def moved_task(checker, *args, **kwargs):
            task_link.unlink(); task_link.symlink_to(alternate)
            return original(checker, *args, **kwargs)
        with mock.patch.object(gate.Checker, "recheck", moved_task):
            code, result = gate.check_task(run_link, task_link, "dispatch")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "path_changed")

    def test_audit_inspect_rechecks_task_link_binding(self):
        self.f.write()
        link = self.f.run / "任务别名.json"
        link.symlink_to(self.f.path)
        alternate = self.f.run / "相同任务.json"
        alternate.write_bytes(self.f.path.read_bytes())
        self.f.record["ref"] = {"path": link.name, "sha256": self.f.ref(self.f.path)["sha256"]}
        self.f.put("状态.json", self.f.root)
        original = gate.Checker.recheck
        def swapped(checker, *args, **kwargs):
            link.unlink(); link.symlink_to(alternate)
            return original(checker, *args, **kwargs)
        with mock.patch.object(gate.Checker, "recheck", swapped):
            code, result = gate.inspect_run(self.f.run)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][0]["code"], "path_changed")

    def archive_fixture(self, corrupt=None, *, compression=zipfile.ZIP_STORED):
        source = str(SCRIPT.parents[1] / "SKILL.md")
        payload = b"historical normative bytes, never executed"
        member = "skill/SKILL.md"
        row = {"source_path": source, "archive_path": member, "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        manifest = {"schema_version": 1, "purpose": "synthetic read-only archive", "files": [row]}
        extras = {}
        if corrupt:
            corrupt(manifest, extras)
        path = self.f.run / "历史规范.zip"
        with zipfile.ZipFile(path, "w", compression=compression) as archive:
            archive.writestr(member, payload)
            archive.writestr("快照清单.json", json.dumps(manifest, ensure_ascii=False))
            for name, value in extras.items():
                archive.writestr(name, value)
        norm = {"path": source, "sha256": hashlib.sha256(payload).hexdigest()}
        self.f.task["input_refs"].append(norm)
        return self.f.ref(path), norm

    def archive_check(self, ref, expected=0, code=None, phase="checkpoint"):
        self.f.write()
        before = {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()}
        actual, result = gate.check_task(self.f.run, self.f.path, phase, checkpoint_archive_ref=ref)
        self.assertEqual(actual, expected, result)
        if code:
            self.assertEqual(result["diagnostics"][-1]["code"], code, result)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()})
        return result

    def test_archive_explicit_checkpoint_only_and_exact_norm_bytes(self):
        ref, norm = self.archive_fixture()
        self.f.check("checkpoint", 1, "hash_mismatch")
        result = self.archive_check(ref)
        self.assertEqual(result["checkpoint_archive"][0]["source_path"], norm["path"])
        self.assertEqual(result["checkpoint_archive"][0]["sha256"], norm["sha256"])
        for phase in ("dispatch", "entry"):
            self.archive_check(ref, 1, "archive_phase", phase)
        self.archive_check(dict(ref, sha256="0" * 64), 1, "hash_mismatch")
        self.f.write()
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "check", "--run", str(self.f.run),
                                 "--task", str(self.f.path), "--phase", "checkpoint", "--checkpoint-archive", ref["path"],
                                 "--checkpoint-archive-sha256", ref["sha256"]], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "check", "--run", str(self.f.run),
                                 "--task", str(self.f.path), "--phase", "checkpoint", "--checkpoint-archive", ref["path"]], capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout)

    def test_archive_rejects_manifest_corruption_and_unsafe_members(self):
        cases = [
            (lambda m, e: m["files"][0].update(sha256="0" * 64), "archive_member_hash"),
            (lambda m, e: m["files"][0].update(bytes=999), "archive_member_hash"),
            (lambda m, e: m["files"].append(dict(m["files"][0])), "archive_manifest"),
            (lambda m, e: e.update({"../escape": b"x"}), "archive_member_path"),
            (lambda m, e: m["files"][0].update(source_path="/unknown/norm.md"), "archive_source"),
            (lambda m, e: m.update(schema_version=True), "archive_manifest")]
        for corrupt, code in cases:
            self.f.task["input_refs"] = [self.f.input]
            ref, _ = self.archive_fixture(corrupt)
            self.archive_check(ref, 1, code)

    def test_archive_cannot_fallback_for_science_or_other_refs_or_alias(self):
        ref, norm = self.archive_fixture()
        self.f.task["scientific_work_ref"] = norm
        self.archive_check(ref, 1, "hash_mismatch")
        self.f.task.pop("scientific_work_ref")
        self.f.control["restriction_refs"] = [norm]
        self.archive_check(ref, 1, "hash_mismatch")
        self.f.control["restriction_refs"] = []
        alias = self.f.run / "规范别名.md"
        alias.symlink_to(norm["path"])
        self.f.task["input_refs"] = [self.f.input, dict(norm, path=str(alias))]
        self.archive_check(ref, 1, "archive_norm_unknown")

    def test_archive_change_during_check_is_rejected(self):
        ref, norm = self.archive_fixture()
        self.f.write()
        original = gate.Checker.ref
        path = self.f.run / ref["path"]
        def changed(checker, value, *args, **kwargs):
            result = original(checker, value, *args, **kwargs)
            if value == norm:
                path.write_bytes(path.read_bytes() + b" ")
            return result
        with mock.patch.object(gate.Checker, "ref", changed):
            code, result = gate.check_task(self.f.run, self.f.path, "checkpoint", checkpoint_archive_ref=ref)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "file_changed")

    def test_audit_precise_normal_reuse_cli_and_acceptance(self):
        row = self.old_same_agent("stopped")
        self.stop_prior(row)
        self.f.check(cli=True)
        self.f.record.update(status="accepted", delivered_at=TIME, accepted_at=TIME)
        self.f.check("entry", cli=True)

    def test_archive_wrong_zip_limits_and_binding_change(self):
        ref, _ = self.archive_fixture()
        path = self.f.run / ref["path"]
        path.write_bytes(b"not a zip")
        self.archive_check(self.f.ref(path), 1, "archive_invalid")
        self.f.task["input_refs"] = [self.f.input]
        ref, _ = self.archive_fixture(lambda m, e: e.update({"large": b"x" * 1048577}))
        self.archive_check(ref, 1, "archive_size")
        self.f.task["input_refs"] = [self.f.input]
        ref, _ = self.archive_fixture()
        alias = self.f.run / "归档别名.zip"
        copy = self.f.run / "同字节归档.zip"
        copy.write_bytes((self.f.run / ref["path"]).read_bytes())
        alias.symlink_to(self.f.run / ref["path"])
        ref = dict(ref, path=str(alias))
        self.f.write()
        original = gate.Checker.recheck
        def swapped(checker, *args, **kwargs):
            alias.unlink(); alias.symlink_to(copy)
            return original(checker, *args, **kwargs)
        with mock.patch.object(gate.Checker, "recheck", swapped):
            code, result = gate.check_task(self.f.run, self.f.path, "checkpoint", checkpoint_archive_ref=ref)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "path_changed")

    def corrupt_deflate_archive(self):
        ref, _ = self.archive_fixture(compression=zipfile.ZIP_DEFLATED)
        data = bytearray((self.f.run / ref["path"]).read_bytes())
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("skill/SKILL.md")
            self.assertEqual(info.compress_type, zipfile.ZIP_DEFLATED)
            offset = info.header_offset
            filename_bytes = int.from_bytes(data[offset + 26:offset + 28], "little")
            extra_bytes = int.from_bytes(data[offset + 28:offset + 30], "little")
            payload_offset = offset + 30 + filename_bytes + extra_bytes
            self.assertGreaterEqual(info.compress_size, 8)
        # Only the temporary copy's compressed payload changes; central directory and manifest remain intact.
        data[payload_offset:payload_offset + 8] = b"\x07" * 8  # Reserved DEFLATE block type.
        path = self.f.run / "损坏压缩流.zip"
        path.write_bytes(data)
        with zipfile.ZipFile(path) as archive:
            json.loads(archive.read("快照清单.json"))
            with self.assertRaises(gate.zlib.error):
                archive.read("skill/SKILL.md")
        return self.f.ref(path)

    def test_archive_deflate_corruption_api_returns_json_failure(self):
        ref = self.corrupt_deflate_archive()
        self.f.write()
        before = {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()}
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code, result = gate.check_task(self.f.run, self.f.path, "checkpoint", checkpoint_archive_ref=ref)
        self.assertEqual(code, 1, result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["decision"], "fail")
        self.assertEqual(result["diagnostics"][-1]["code"], "archive_invalid")
        self.assertEqual(stream.getvalue(), "")
        self.assertNotIn("Traceback", json.dumps(result))
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()})

    def test_archive_deflate_corruption_cli_returns_json_without_traceback(self):
        ref = self.corrupt_deflate_archive()
        self.f.write()
        before = {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()}
        result = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "check", "--run", str(self.f.run),
                                 "--task", str(self.f.path), "--phase", "checkpoint", "--checkpoint-archive", ref["path"],
                                 "--checkpoint-archive-sha256", ref["sha256"]], capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        response = json.loads(result.stdout)
        self.assertFalse(response["ok"])
        self.assertEqual(response["decision"], "fail")
        self.assertEqual(response["diagnostics"][-1]["code"], "archive_invalid")
        self.assertNotIn(b"Traceback", result.stdout + result.stderr)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.f.run.rglob("*") if p.is_file()})

    def loop_input(self):
        self.f.record.update(status="accepted", delivered_at=TIME, accepted_at=TIME)
        link = self.f.run / "循环输入.json"
        link.symlink_to(self.f.input["path"])
        self.f.task["input_refs"].append({"path": link.name, "sha256": self.f.input["sha256"]})
        self.f.write()
        return link

    def test_path_loop_at_initial_bind_api_and_cli_is_structured(self):
        link = self.loop_input()
        link.unlink(); link.symlink_to(link.name)
        before = (self.f.run / "状态.json").read_bytes()
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code, result = gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "path_invalid")
        self.assertNotIn("receipt", result)
        self.assertEqual(stream.getvalue(), "")
        proc = subprocess.run([sys.executable, "-I", "-B", str(SCRIPT), "check", "--run", str(self.f.run),
                               "--task", str(self.f.path), "--phase", "entry"], capture_output=True)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertEqual(json.loads(proc.stdout)["diagnostics"][-1]["code"], "path_invalid")
        self.assertEqual(proc.stderr, b"")
        self.assertEqual((self.f.run / "状态.json").read_bytes(), before)

    def test_path_loop_at_final_recheck_has_no_receipt_or_root_change(self):
        link = self.loop_input()
        before = (self.f.run / "状态.json").read_bytes()
        original = gate.Checker.recheck
        def changed(checker, *args, **kwargs):
            link.unlink(); link.symlink_to(link.name)
            return original(checker, *args, **kwargs)
        with mock.patch.object(gate.Checker, "recheck", changed):
            code, result = gate.check_task(self.f.run, self.f.path, "entry")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["diagnostics"][-1]["code"], "path_invalid")
        self.assertNotIn("receipt", result)
        self.assertEqual((self.f.run / "状态.json").read_bytes(), before)

    def test_no_network_agent_or_write(self):
        tree = ast.parse(SCRIPT.read_text())
        imported = {n.names[0].name for n in ast.walk(tree) if isinstance(n, ast.Import)}
        imported.update(n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
        self.assertEqual(imported, {"argparse", "datetime", "errno", "hashlib", "io", "json", "math", "re", "sys", "zipfile", "zlib", "pathlib"})
        forbidden = {"write_bytes", "write_text", "mkdir", "unlink", "rename", "system", "exec", "eval", "spawn"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden)
        self.f.check(cli=True)


def example():
    f = Fixture()
    try:
        f.source()
        f.recovery()
        f.write()
        # Include every referenced file so this is a complete, reproducible JSON example.
        files = {str(p.relative_to(f.run)): json.loads(p.read_bytes())
                 for p in sorted(f.run.rglob("*.json"))}
        f.check()  # The emitted dispatch example itself must pass the gate.
        print(json.dumps({"run_directory": f.run.name, "files": files}, ensure_ascii=False, indent=2))
    finally:
        f.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["--example"]:
        example()
    else:
        unittest.main(verbosity=2)
