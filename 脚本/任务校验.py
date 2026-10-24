#!/usr/bin/env python3
"""Read-only, offline task gate. Python 3.9; run with python3 -I.

State: RUN/状态.json, harness_control v1. Task records require only
ref/agent_id/stage/status. References use path (relative to RUN or absolute)
and lowercase sha256. Scope: allowed_actions/allowed_stages/limits.

Sources: source_ledger_ref -> v1 {contracts: {id: ref}, accounts: {id: ...}}.
Each account owns exact URIs (including known aliases), cumulative limits,
used/remaining, constraints, history_refs, and explicit safety/cache facts.
Contracts are immutable; used_floor prevents known historical use resets.
Legacy new_budget/contracts/archive is read without guessing unknown facts.
Exit 0 = allow, 1 = fail, 2 = hold (missing/unsupported evidence).
No dispatch, writes, requests, imports from RUN, or scientific judgement.
"""
import argparse
import datetime
import errno
import hashlib
import io
import json
import math
import os
import stat
import uuid
import re
import sys
import zipfile
import zlib
from pathlib import Path

ACTIONS = {"read_local", "preserve_local", "generate_local", "read_cache",
           "source_get", "source_query"}
COUNTERS = {"initial_get", "normal_location", "query"}
STATUSES = {"registered", "delivered", "accepted", "started", "completed",
            "failed", "stopped"}
FACTS = ("formed_at", "delivered_at", "archived_at", "retained_at", "received_at", "read_at")
PHASES = {"dispatch", "entry", "checkpoint"}
INSPECT_BYTES = 12288


class GateError(Exception):
    def __init__(self, code, detail, level="error"):
        self.code, self.detail, self.level = code, detail, level


def require(test, code, detail, level="error"):
    if not test:
        raise GateError(code, detail, level)


def obj(value, label):
    require(type(value) is dict, "type", label + " must be an object")
    return value


def text(value, label):
    require(type(value) is str and bool(value.strip()), "type", label + " must be nonempty text")
    return value


def strings(value, label):
    require(type(value) is list and all(type(v) is str and v.strip() for v in value),
            "type", label + " must be a string array")
    require(len(set(value)) == len(value), "duplicate", label)
    return value


def number(value, label):
    require(type(value) is int and value >= 0, "type", label + " must be a nonnegative integer")
    return value


def flag(value, label):
    require(type(value) is bool, "type", label + " must be boolean")
    return value


def counts(value, label):
    require(value is not None, "budget_unknown", label + " is missing", "hold")
    obj(value, label)
    require(set(value) == COUNTERS, "budget_unknown", label + " requires all three counters", "hold")
    return {k: number(v, label + "." + k) for k, v in value.items()}


def constraints(value, label):
    obj(value, label)
    require(set(value) == {"max_bytes", "timeout_seconds", "retries"}, "constraints_unknown", label, "hold")
    for k, v in value.items():
        number(v, label + "." + k)
    require(value["max_bytes"] > 0 and value["timeout_seconds"] > 0, "constraints_unknown", label, "hold")
    return value


def timestamp(value, label):
    text(value, label)
    try:
        t = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(t.tzinfo is not None, "timestamp", label + " needs timezone")
        return t
    except ValueError:
        raise GateError("timestamp", label + " invalid ISO timestamp")


def finished_time(receipt):
    values = [timestamp(receipt[k], k) for k in ("finished_at", "finished_at_utc") if k in receipt]
    require(bool(values), "timestamp", "finished_at or finished_at_utc required")
    require(all(v == values[0] for v in values), "timestamp_conflict", "terminal timestamps conflict")
    return values[0]


def strict_pairs(pairs):
    result = {}
    for k, v in pairs:
        require(k not in result, "duplicate_json_key", k)
        result[k] = v
    return result


def reject_constant(value):
    raise GateError("json_number", "nonfinite JSON number: " + value)


def parse(data, label):
    try:
        value = json.loads(data, object_pairs_hook=strict_pairs, parse_constant=reject_constant)
        # Python also accepts finite-looking exponent strings overflowing to inf.
        def finite(v):
            if type(v) is float:
                require(math.isfinite(v), "json_number", label)
            elif type(v) is dict:
                for x in v.values():
                    finite(x)
            elif type(v) is list:
                for x in v:
                    finite(x)
        finite(value)
        return obj(value, label)
    except (ValueError, UnicodeError, RecursionError) as e:
        raise GateError("json_invalid", label + ": " + str(e))


def effective_pause(root, control):
    """Return the dispatch/entry pause reasons, also used by the bounded inventory."""
    status = root.get("status")
    require(type(status) is str, "type", "root.status must be text")
    paused = status.startswith("paused") or status in {"stopped", "suspended", "halted"}
    require(paused or status in {"running", "active", "draining_existing_tasks"}, "run_status_unknown", status, "hold")
    reasons = ["status:" + status] if paused or status == "draining_existing_tasks" else []
    optimization = obj(root.get("harness_optimization_pause", {}), "harness_optimization_pause")
    if flag(optimization.get("new_scientific_tasks_and_source_requests_paused", False), "optimization pause"):
        reasons.append("harness_optimization_pause")
    if flag(control.get("paused", False), "control.paused"):
        reasons.append("harness_control.paused")
    return reasons


def resolve_path(path, *, strict=False):
    try:
        # Strict resolution detects loops even when non-strict resolve tolerates them.
        try:
            return path.resolve(strict=True)
        except FileNotFoundError:
            if strict:
                raise
            return path.resolve(strict=False)
    except (RuntimeError, OSError) as error:
        if isinstance(error, RuntimeError) or error.errno == errno.ELOOP:
            raise GateError("path_invalid", str(error))
        raise


class Checker:
    def __init__(self, run, task, phase, *, checkpoint_archive_ref=None):
        require(type(phase) is str and phase in PHASES, "phase_invalid", "phase must be dispatch, entry or checkpoint")
        self.phase = phase
        self.diagnostics = []
        self.task_id = None
        self.snapshots = {}  # Resolved target -> observed content hash; retained for callers.
        self.bindings = {}  # Original absolute spelling -> resolved target, including parent links.
        self.absent = set()  # Missing norm files used only by explicit archive preservation.
        self.archive = None
        self.archive_used = []
        self.message_reads = {}
        self.protocol = None
        self.protocol_norms = set()
        self.raw_run = Path(run).absolute()
        self.run = self.bind(self.raw_run)
        require(self.run.is_dir(), "file_missing", str(self.raw_run), "hold")
        self.raw_task_path = self.absolute(str(task) if isinstance(task, Path) else task)
        self.task_path = self.bind(self.raw_task_path)
        self.root_path = self.path("状态.json")
        if checkpoint_archive_ref is not None:
            require(phase == "checkpoint", "archive_phase", "historical norms are checkpoint-only")
            self.load_archive(checkpoint_archive_ref)

    def absolute(self, raw):
        raw = text(raw, "path") if not isinstance(raw, Path) else str(raw)
        require("\x00" not in raw, "path", "NUL in path")
        p = Path(raw)
        return p if p.is_absolute() else self.raw_run / p

    def bind(self, raw):
        raw = self.absolute(raw)
        target = resolve_path(raw)
        require(raw not in self.bindings or self.bindings[raw] == target, "path_changed", str(raw))
        self.bindings[raw] = target
        return target

    def path(self, raw):
        # JSON paths remain strings, even though internal read()/bind() accept Path.
        return self.bind(text(raw, "path"))

    def read(self, path):
        path = self.bind(path)
        if not path.is_file():
            self.absent.add(path)
            raise GateError("file_missing", str(path), "hold")
        require(path not in self.absent, "file_changed", str(path))
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        require(path not in self.snapshots or self.snapshots[path] == digest, "file_changed", str(path))
        self.snapshots[path] = digest
        return data

    def recheck(self, root_conflict=False):
        for source, author, reader, identity in list(self.message_reads.values()):
            self.native_message(source, author, reader, expected_identity=identity)
        for raw, target in self.bindings.items():
            require(resolve_path(raw) == target, "path_changed", str(raw))
        for path in self.absent:
            require(not path.is_file(), "file_changed", str(path))
        for path, digest in self.snapshots.items():
            require(hashlib.sha256(path.read_bytes()).hexdigest() == digest,
                    "cas_conflict" if root_conflict and path == self.root_path else "file_changed", str(path))
        for raw, target in self.bindings.items():
            require(resolve_path(raw) == target, "path_changed", str(raw))

    def merge(self, other):
        require(not (self.absent & set(other.snapshots)) and not (other.absent & set(self.snapshots)),
                "file_changed", "file presence changed between read sets")
        for key, value in other.message_reads.items():
            require(key not in self.message_reads or self.message_reads[key] == value, "message_changed", "nested message observation differs")
            self.message_reads[key] = value
        self.absent.update(other.absent)
        for raw, target in other.bindings.items():
            require(raw not in self.bindings or self.bindings[raw] == target, "path_changed", str(raw))
            self.bindings[raw] = target
        for path, digest in other.snapshots.items():
            require(path not in self.snapshots or self.snapshots[path] == digest, "file_changed", str(path))
            self.snapshots[path] = digest

    @staticmethod
    def norm_sources():
        skill = resolve_path(Path(__file__)).parents[1]
        workspace = Path.home() / "research" / "机器人选题"
        homes = {skill, Path.home() / ".agents/skills/idea-hunt", Path.home() / ".claude/skills/idea-hunt"}
        files = ("SKILL.md", "配置.yaml", "参考/任务接口.md", "脚本/任务校验.py", "脚本/任务变更.py",
                 "脚本/消息归档.py", "测试/任务校验测试.py", "测试/任务变更_测试.py", "测试/消息归档测试.py")
        return {str(home / name) for home in homes for name in files} | {
            str(workspace / name) for name in ("AGENTS.md", "判定标准.md", "评审校准.md")
        } | {str(workspace / "角色" / (name + ".md"))
             for name in ("Leader", "组长", "调研人", "初筛人", "审稿人")}

    def load_archive(self, ref):
        obj(ref, "checkpoint_archive_ref")
        require(set(ref) == {"path", "sha256"}, "archive_ref", "archive path and sha256 required")
        path = self.path(ref.get("path"))
        require(type(ref.get("sha256")) is str and re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]), "hash_format", str(path))
        require(path.is_file() and path.stat().st_size <= 8388608, "archive_size", "archive missing or exceeds 8 MiB")
        data = self.read(path)
        require(len(data) <= 8388608, "archive_size", "archive exceeds 8 MiB")
        require(hashlib.sha256(data).hexdigest() == ref["sha256"], "hash_mismatch", str(path))
        allowed = self.norm_sources()
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                infos = archive.infolist()
                names = [info.filename for info in infos]
                require(1 < len(names) <= 33 and len(names) == len(set(names)), "archive_members", "unique bounded members required")
                for info in infos:
                    name = info.filename
                    require(name and "\\" not in name and "\x00" not in name and not name.startswith("/") and
                            all(part not in {"", ".", ".."} for part in name.split("/")) and not info.is_dir() and
                            ((info.external_attr >> 16) & 0o170000) != 0o120000 and not info.flag_bits & 1,
                            "archive_member_path", name)
                    require(info.file_size <= 1048576, "archive_size", name)
                require(sum(info.file_size for info in infos) <= 8388608 and "快照清单.json" in names,
                        "archive_size", "bounded manifest archive required")
                manifest = parse(archive.read("快照清单.json"), "archive manifest")
                require(set(manifest) == {"schema_version", "purpose", "files"} and
                        type(manifest["schema_version"]) is int and manifest["schema_version"] == 1,
                        "archive_manifest", "v1 manifest required")
                text(manifest["purpose"], "archive.purpose")
                rows = manifest["files"]
                require(type(rows) is list and len(rows) == len(infos) - 1, "archive_manifest", "manifest/member count mismatch")
                sources, members, entries = set(), set(), {}
                for row in rows:
                    obj(row, "archive manifest row")
                    require(set(row) == {"source_path", "archive_path", "bytes", "sha256"}, "archive_manifest", "manifest fields")
                    source, member = text(row["source_path"], "source_path"), text(row["archive_path"], "archive_path")
                    require(Path(source).is_absolute() and source == str(Path(source)) and ".." not in Path(source).parts and source in allowed,
                            "archive_source", source)
                    require(source not in sources and member not in members and member in names and member != "快照清单.json",
                            "archive_manifest", "duplicate or missing source/member")
                    sources.add(source)
                    members.add(member)
                    size = number(row["bytes"], "archive.bytes")
                    require(type(row["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", row["sha256"]), "hash_format", member)
                    payload = archive.read(member)
                    require(len(payload) == size and hashlib.sha256(payload).hexdigest() == row["sha256"],
                            "archive_member_hash", member)
                    entries[source] = (row, payload)
                require(members | {"快照清单.json"} == set(names), "archive_manifest", "unlisted member")
                self.archive = {"ref": dict(ref), "entries": entries}
        except (zipfile.BadZipFile, zlib.error, RuntimeError, EOFError, NotImplementedError) as error:
            raise GateError("archive_invalid", str(error))

    def ref(self, ref, as_json=False, *, norm=False):
        obj(ref, "ref")
        raw = self.absolute(text(ref.get("path"), "path"))
        p = self.path(ref["path"])
        sha = ref.get("sha256")
        require(type(sha) is str and re.fullmatch(r"[0-9a-f]{64}", sha), "hash_format", str(p))
        try:
            data = self.read(p)
            require(hashlib.sha256(data).hexdigest() == sha, "hash_mismatch", str(p))
        except GateError as error:
            if not (((norm and not self.protocol_norms) or str(raw) in self.protocol_norms) and self.archive is not None and error.code in {"hash_mismatch", "file_missing"}):
                raise
            source = str(raw)  # Exact spelling, deliberately not resolve() or target-path matching.
            require(source in self.norm_sources() and source in self.archive["entries"], "archive_norm_unknown", source)
            row, data = self.archive["entries"][source]
            require(row["sha256"] == sha, "archive_ref_mismatch", source)
            receipt = {"source_path": source, "member": row["archive_path"], "sha256": sha,
                       "archive_ref": self.archive["ref"]}
            if receipt not in self.archive_used:
                self.archive_used.append(receipt)
                self.notice("checkpoint_archive_norm", source + " from " + row["archive_path"])
        return parse(data, str(p)) if as_json else p

    def refs(self, value, label):
        require(type(value) is list, "type", label + " must be an array")
        for r in value:
            self.ref(r)

    def notice(self, code, detail):
        self.diagnostics.append({"level": "warning", "code": code, "detail": detail})

    def eligibility(self, test, code, detail, level="error"):
        if not test and self.phase == "checkpoint":
            self.notice(code, detail + "; preservation only")
        else:
            require(test, code, detail, level)

    def output(self, raw):
        p = self.path(raw)
        allowed = False
        for name in ("小组", "检索支持"):
            base = resolve_path(self.run / name)
            # A symlinked archive root must itself remain inside this run.
            try:
                base.relative_to(self.run)
                p.relative_to(base)
                allowed = base != self.run
            except ValueError:
                pass
            if allowed:
                break
        require(allowed, "output_escape", str(p))
        require(not p.exists() or p.is_dir(), "output_type", str(p))

    def task_binding(self, task, record):
        """Check shared envelope/record identity without inferring execution permission."""
        obj(task, "task")
        obj(record, "task record")
        tid = text(task.get("task_id"), "task_id")
        for field in ("agent_id", "stage"):
            text(task.get(field), field)
            require(record.get(field) == task[field], "route_mismatch", field)
        require(record.get("status") in STATUSES, "task_status", tid)
        for key in task:
            if key in record and key not in {"schema_version", "status"}:
                require(record[key] == task[key], "record_mismatch", key)

    def material_target(self, task, raw):
        """Bind both the declared spelling and resolved target to this task directory."""
        self.output(task.get("output_dir"))
        base_raw = self.absolute(task["output_dir"])
        target_raw = self.absolute(raw)
        require(".." not in target_raw.parts, "output_escape", str(target_raw))
        base, target = self.path(task["output_dir"]), self.path(raw)
        # Normalize only the RUN alias, retaining lexical containment inside the declared group.
        def run_spelling(path):
            try:
                return self.run / path.relative_to(self.raw_run)
            except ValueError:
                return path
        declared_base, declared_target = run_spelling(base_raw), run_spelling(target_raw)
        try:
            try:
                declared_target.relative_to(declared_base)
            except ValueError:
                declared_target.relative_to(base)
            target.relative_to(base)
        except ValueError:
            raise GateError("output_escape", "material must remain in this task output_dir")
        require(target != base and target.suffix.lower() == ".md", "scientific_format", "Markdown target required")
        return target

    def native_message(self, source, author, reader, *, expected_identity=None):
        """Verify a complete native SendMessage leaf, including the native sender identity."""
        obj(source, "message source")
        required = {"path", "line", "record_uuid", "pointer", "raw_line_sha256", "payload_bytes",
                    "payload_sha256", "utf8_start", "utf8_end", "tool_use_id"}
        require(required <= set(source) and set(source) <= required | {"task_id", "author_id", "version"},
                "message_source_fields", "exact native locator fields required")
        path = Path(text(source["path"], "message path"))
        require(path.is_absolute() and self.bind(path) == path, "message_source_path", "canonical absolute path required")
        root = parse(self.read(self.raw_run / "状态.json"), "root")
        bindings = obj(root["harness_control"].get("native_message_bindings", {}), "native_message_bindings")
        paths = bindings.get(author)
        require(type(paths) is list and str(path) in paths, "native_message_binding_missing",
                "root owner must bind this actual actor to its observed native transcript path", "hold")
        line = number(source["line"], "line")
        require(0 < line <= 1000000, "message_size", "bounded line number required")
        fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            require(stat.S_ISREG(info.st_mode), "message_source_type", "regular native log required")
            identity = (info.st_dev, info.st_ino)
            require(expected_identity is None or identity == expected_identity, "message_changed", "native log replaced")
            offset, current, length, pieces, done = 0, 1, 0, [], False
            while True:
                block = os.pread(fd, 65536, offset)
                require(bool(block), "message_line_missing", "complete LF-terminated record required", "hold")
                offset += len(block)
                start = 0
                while start < len(block):
                    newline = block.find(b"\n", start)
                    end = newline + 1 if newline >= 0 else len(block)
                    length += end - start
                    require(length <= 2097152, "message_size", "record exceeds 2 MiB")
                    if current == line:
                        pieces.append(block[start:end])
                    if newline >= 0:
                        if current == line:
                            done = True
                            break
                        current += 1
                        length = 0
                    start = end
                if done:
                    break
            raw = b"".join(pieces)
        finally:
            os.close(fd)
        require(hashlib.sha256(raw).hexdigest() == source["raw_line_sha256"], "message_record_hash", "native record changed")
        record = parse(raw, "native message")
        identifier = text(source["record_uuid"], "record_uuid")
        require(str(uuid.UUID(identifier)) == identifier and uuid.UUID(identifier).int != 0 and
                record.get("uuid") == identifier, "message_uuid", "canonical matching non-null UUID required")
        require(record.get("agentId") == author, "message_sender", "native sender differs or is missing")
        require(record.get("type") == "assistant" and record.get("message", {}).get("role") == "assistant" and
                not flag(record.get("isApiErrorMessage", False), "isApiErrorMessage"), "message_role", "real assistant message required")
        match = re.fullmatch(r"/message/content/(0|[1-9][0-9]*)/input/message", source["pointer"])
        require(match is not None, "message_pointer", "complete SendMessage leaf required")
        blocks = record["message"]["content"]
        require(type(blocks) is list and int(match[1]) < len(blocks), "message_pointer", "tool block absent")
        block = obj(blocks[int(match[1])], "message block")
        require(block.get("type") == "tool_use" and block.get("name") == "SendMessage" and
                block.get("id") == source["tool_use_id"], "message_block", "exact SendMessage tool binding required")
        inputs = obj(block.get("input"), "SendMessage input")
        require(inputs.get("to") == reader, "message_recipient", "actual recipient differs")
        value = text(inputs.get("message"), "full message")
        payload = value.encode("utf-8")
        require("\x00" not in value and 0 < len(payload) <= 1048576, "message_size", "nonempty bounded UTF-8 body required")
        require(type(source["utf8_start"]) is int and type(source["utf8_end"]) is int and
                source["utf8_start"] == 0 and source["utf8_end"] == len(payload), "message_range", "whole body required")
        require(type(source["payload_bytes"]) is int and source["payload_bytes"] == len(payload) and
                source["payload_sha256"] == hashlib.sha256(payload).hexdigest(), "message_payload_hash", "payload fingerprint differs")
        key = (str(path), line)
        observation = (dict(source), author, reader, identity)
        require(key not in self.message_reads or self.message_reads[key] == observation, "message_changed", "message binding changed")
        self.message_reads[key] = observation
        return value

    def message_ref(self, reference):
        obj(reference, "message_ref")
        require(set(reference) == {"source", "author_id", "reader_id", "task_id", "version"},
                "message_ref_fields", "complete delivery identity required")
        for key in ("author_id", "reader_id", "task_id", "version"):
            text(reference[key], key)
        for key, field in (("author_id", "author_id"), ("task_id", "task_id"), ("version", "version")):
            if field in reference["source"]:
                require(reference["source"][field] == reference[key], "message_declaration", field)
        return self.native_message(reference["source"], reference["author_id"], reference["reader_id"])

    def protocol_mapping(self, task, record):
        root = parse(self.read(self.raw_run / "状态.json"), "root")
        control = root["harness_control"]
        if "delivery_protocol_ref" not in control:
            return None
        revision = self.ref(control["delivery_protocol_ref"], True)
        require(revision.get("schema_version") == 2 and type(revision["schema_version"]) is int and
                revision.get("run_id") == self.run.name and type(revision.get("to_epoch")) is int and revision.get("to_epoch") == control["epoch"] and
                type(revision.get("from_epoch")) is int and 0 < revision["from_epoch"] < revision["to_epoch"],
                "protocol_revision", "explicit current run/epoch transition required")
        self.protocol = revision
        for reference in revision["norm_refs"]:
            self.ref(reference)
        mapping = obj(revision["task_mappings"], "task_mappings").get(task["task_id"])
        if mapping is None:
            return None
        require(mapping.get("task_ref") == record.get("ref") and task["epoch"] == revision["from_epoch"],
                "protocol_task_binding", "mapping must bind original immutable task")
        require(set(mapping) <= {"task_ref", "deliverable", "delivery_readiness_refs"}, "protocol_mapping_fields", "delivery-only mapping")
        self.load_archive(revision["norm_archive_ref"])
        skill = str(Path(__file__).resolve().parent.parent)
        # Scientific criteria and user permissions cannot be revised through this delivery migration.
        self.protocol_norms = {str(Path(home) / name) for home in (skill, str(Path.home() / ".claude/skills/idea-hunt"), str(Path.home() / ".agents/skills/idea-hunt"))
                               for name in ("SKILL.md", "参考/任务接口.md")}
        expected_norms = {r["path"] for r in task.get("input_refs", []) if r.get("path") in self.protocol_norms}
        require(expected_norms <= {r["path"] for r in revision["norm_refs"]}, "protocol_norm_missing",
                "mapped historical delivery norms also require current exact norm references")
        return mapping

    def delivery_readiness(self, task, delivery, mapping=None):
        root = parse(self.read(self.raw_run / "状态.json"), "root")
        refs = (mapping or task).get("delivery_readiness_refs")
        require(type(refs) is list and len(refs) == 2, "delivery_readiness_missing", "author and receiving Leader must confirm a viable route", "hold")
        actors = [task["agent_id"], delivery["reader_id"]]
        require(len(set(actors)) == 2, "delivery_readiness_binding", "independent author and receiver required")
        for actor, source in zip(actors, refs):
            receipt = parse(self.native_message(source, actor, root["total_leader"]), "delivery readiness")
            expected = {"kind": "delivery_readiness", "run_id": self.run.name, "task_id": task["task_id"],
                        "epoch": root["harness_control"]["epoch"], "agent_id": actor,
                        "author_id": task["agent_id"], "reader_id": delivery["reader_id"],
                        "format": delivery["format"], "version": delivery["version"],
                        "delivery_plan": delivery.get("delivery_plan"), "allowed": True}
            require(all(type(receipt.get(k)) is type(v) and receipt[k] == v for k, v in expected.items()),
                    "delivery_readiness_binding", "real session confirmation differs or blocks delivery", "hold")
            text(receipt.get("constraints"), "session constraints summary")
            require(timestamp(receipt.get("checked_at"), "checked_at") <= datetime.datetime.now(datetime.timezone.utc),
                    "delivery_readiness_time", "future confirmation")

    def local_capability(self, task, record, control, required, *, operation_only=False):
        """Check declared local authority; real tool permission remains the caller's responsibility."""
        scope = obj(task.get("task_scope"), "task_scope")
        require(set(scope) <= {"allowed_actions", "allowed_stages", "limits"}, "scope_unknown_fields", "task_scope", "hold")
        actions = set(strings(scope.get("allowed_actions"), "allowed_actions"))
        require(actions <= ACTIONS, "action_unsupported", "local delivery task actions", "hold")
        require(task["stage"] in strings(scope.get("allowed_stages"), "allowed_stages"), "scope_stage", task["stage"])
        require(set(required) <= actions, "scientific_delivery_permission", "delivery needs " + ", ".join(sorted(required)), "hold")
        limits, used = obj(scope.get("limits"), "limits"), obj(record.get("action_used", {}), "action_used")
        for action, limit in limits.items():
            require(action in actions, "scope_limit_action", action)
            number(limit, "limit." + action)
        for action, count in used.items():
            require(action in actions, "scope_limit_action", action)
            number(count, "action_used." + action)
        for action in required:
            if action in limits:
                require(used.get(action, 0) < limits[action], "scope_exhausted", action)
        permissions = obj(control.get("permissions"), "permissions")
        allowed = set(strings(permissions.get("allowed_actions"), "allowed_actions"))
        denied = set(strings(permissions.get("denied_actions"), "denied_actions"))
        for reference in control.get("restriction_refs", []):
            denied.update(strings(self.ref(reference, True).get("denied_actions"), "restriction.denied_actions"))
        denied.update(self.inherited_refusals(task, control))
        requested = set(required) if operation_only else actions
        require(requested <= allowed and not requested & denied, "permission_denied", "delivery cannot elevate permissions")
        return denied

    def inherited_refusals(self, task, control):
        denied = set()
        ancestors = set(self.ancestors(task, control))
        for tid, prior in control["tasks"].items():
            obj(prior, "task record")
            if prior.get("agent_id") == task["agent_id"] and tid != task["task_id"]:
                historical = self.ref(prior.get("ref"), True)
                require(historical.get("task_id") == tid and historical.get("agent_id") == prior.get("agent_id")
                        and historical.get("stage") == prior.get("stage"), "route_mismatch", tid)
                ancestors.update(self.ancestors(historical, control))
        for tid, prior in control["tasks"].items():
            if prior.get("agent_id") == task["agent_id"] or tid in ancestors:
                denied.update(strings(prior.get("refusal_actions", []), "refusal_actions"))
        return denied

    def archive_actor_available(self, task, record, root):
        """Reject an impossible delivery route without reserving unrelated generation budget."""
        tid = task["task_id"]
        require("unstarted_cancellation" not in record, "task_cancelled", tid)
        control = root["harness_control"]
        ancestors = self.ancestors(task, control)
        for old_id, old in control["tasks"].items():
            if old_id == tid or old.get("agent_id") != task["agent_id"]:
                continue
            previous = self.ref(old.get("ref"), True)
            require(previous.get("task_id") == old_id and previous.get("agent_id") == old.get("agent_id")
                    and previous.get("stage") == old.get("stage"), "route_mismatch", old_id)
            if "unstarted_cancellation" in old:
                self.unstarted_cancelled(old_id, old, root.get("total_leader"))
                continue
            require(old.get("status") in {"completed", "failed", "stopped"}, "agent_busy", old_id)
            if old["status"] == "completed":
                self.facts(old)
                self.scientific(previous, old, preserve_existing=True)
            else:
                require(old.get("runtime_state") == "stopped" and old.get("runtime_id") is not None and
                        old.get("stop_receipt_ref") is not None, "stop_evidence_missing", old_id, "hold")
                self.stopped(old_id, old)
                if old.get("failure_id"):
                    require(old_id in ancestors or self.resolved_failure(old_id, control),
                            "recovery_missing", "archive actor has unresolved failed work")

    def delivery_route(self, task, record, delivery, *, admission=False, preserving=False):
        plan = delivery.get("delivery_plan")
        require(delivery.get("format") != "message" or plan is not None,
                "delivery_plan_mode", "message delivery requires an explicit native-message plan")
        if plan is None:
            if admission:
                root = parse(self.read(self.raw_run / "状态.json"), "root")
                self.local_capability(task, record, root["harness_control"], {"generate_local", "preserve_local"})
            return None
        obj(plan, "delivery_plan")
        mode = plan.get("mode")
        if delivery.get("format") == "message":
            require(plan == {"mode": "native-message"}, "delivery_plan_mode", "native-message route required")
            if admission:
                root = parse(self.read(self.raw_run / "状态.json"), "root")
                self.local_capability(task, record, root["harness_control"], {"read_local", "generate_local"})
            return {"mode": "native-message"}
        require(mode in {"direct", "delegated-archive"}, "delivery_plan_mode", "direct or delegated-archive required")
        fields = {"mode", "target"} | ({"archive_task_id", "archive_agent_id"} if mode == "delegated-archive" else set())
        require(set(plan) == fields, "delivery_plan_fields", "exact delivery plan fields required")
        target = self.material_target(task, text(plan.get("target"), "delivery_plan.target"))
        if mode == "delegated-archive":
            tid = text(plan.get("archive_task_id"), "archive_task_id")
            agent = text(plan.get("archive_agent_id"), "archive_agent_id")
            require(tid != task["task_id"], "archive_owner", "delegated archive requires another registered task")
            require(agent == delivery["reader_id"], "archive_owner", "archive actor must be the receiving group owner")
        if not admission:
            return {"mode": mode, "target": target}
        root = parse(self.read(self.raw_run / "状态.json"), "root")
        control = obj(root.get("harness_control"), "harness_control")
        actor_task, actor_record = task, record
        require("unstarted_cancellation" not in record, "task_cancelled", task["task_id"])
        if mode == "delegated-archive":
            actor_record = obj(control["tasks"].get(tid), "archive task record")
            actor_task = self.ref(actor_record.get("ref"), True)
            require(actor_task.get("schema_version") == 1 and type(actor_task["schema_version"]) is int,
                    "task_schema", "archive task v1 required")
            require(actor_task.get("task_id") == tid and actor_task.get("agent_id") == actor_record.get("agent_id") == agent
                    and actor_task.get("stage") == actor_record.get("stage"), "route_mismatch", "archive task")
            self.task_binding(actor_task, actor_record)
            number(actor_task.get("epoch"), "archive_task.epoch")
            require(actor_task.get("run_id") == self.run.name and actor_task.get("epoch") == control["epoch"],
                    "epoch_stale", "archive task")
            require(actor_record.get("status") in {"registered", "delivered", "accepted", "started"},
                    "archive_task_terminal", tid)
            require(self.absolute(actor_task.get("output_dir")) == self.absolute(task["output_dir"])
                    and self.path(actor_task["output_dir"]) == self.path(task["output_dir"]),
                    "archive_owner", "archive task must own the same declared group directory")
            self.ref(actor_task.get("role_ref"), norm=True)
            self.refs(actor_task.get("input_refs"), "archive.input_refs")
            self.facts(actor_record)
            self.archive_actor_available(actor_task, actor_record, root)
        required = {"read_local", "preserve_local"} if mode == "delegated-archive" or preserving else {"generate_local", "preserve_local"}
        if mode == "delegated-archive":
            require(not self.inherited_refusals(task, control) & required,
                    "permission_denied", "delegation cannot retry the author's refused action")
        self.local_capability(actor_task, actor_record, control, required, operation_only=preserving)
        return {"mode": mode, "target": target, "task": actor_task, "record": actor_record}

    def delivery_facts(self, delivery, label="output_delivery"):
        obj(delivery, label)
        text(delivery.get("version"), label + ".version")
        require(("material_ref" in delivery) != ("message_ref" in delivery), "output_identity", "exactly one file or message original required")
        if "message_ref" in delivery:
            self.message_ref(delivery["message_ref"])
        else:
            self.ref(delivery.get("material_ref"))
        times = {k: timestamp(delivery[k], label + "." + k) for k in FACTS if k in delivery}
        require("formed_at" in times, "delivery_fact_missing", label + ".formed_at")
        for k in ("delivered_at", "archived_at", "retained_at", "received_at", "read_at"):
            if k in times:
                require(times[k] >= times["formed_at"], "fact_order", k + " precedes formation")
        if "read_at" in times:
            received = times.get("delivered_at", times.get("received_at") if "message_ref" in delivery else None)
            require(received is not None and times["read_at"] >= received,
                    "fact_order", "output read needs prior observed full delivery")
            text(delivery.get("reader_id"), label + ".reader_id")
            original = "message_ref" if "message_ref" in delivery else "material_ref"
            require(delivery.get("read_version") == delivery["version"] and
                    delivery.get("read_" + original) == delivery[original],
                    "output_read_mismatch", "read must bind current version and complete original")
            if original == "material_ref":
                self.ref(delivery["read_material_ref"])
        return times

    def facts(self, record):
        # Flat material fields remain a legacy output receipt, never an input ACK.
        times = {k: timestamp(record[k], k) for k in FACTS if k in record}
        if "material_ref" in record or any(k in record for k in ("formed_at", "archived_at", "read_at")):
            require("formed_at" in times and "material_ref" in record,
                    "delivery_fact_missing", "legacy facts need formed_at and material_ref")
            self.ref(record["material_ref"])
            for k in ("delivered_at", "archived_at", "read_at"):
                if k in times:
                    require(times[k] >= times["formed_at"], "fact_order", k + " precedes formation")
            if "read_at" in times:
                text(record.get("reader_id"), "reader_id")
        if record["status"] in {"delivered", "accepted", "started"}:
            require("delivered_at" in times, "delivery_fact_missing", "input status needs actual delivered_at")
        if "accepted_at" in record:
            accepted = timestamp(record["accepted_at"], "accepted_at")
            require("delivered_at" in times and accepted >= times["delivered_at"], "fact_order", "acceptance precedes delivery")
        if "output_delivery" in record:
            self.delivery_facts(record["output_delivery"])
        if record["status"] == "completed":
            require("delivered_at" in times, "delivery_fact_missing", "completed needs actual input delivery")
            if "output_delivery" in record:
                require("delivered_at" in record["output_delivery"] or
                        ("message_ref" in record["output_delivery"] and "received_at" in record["output_delivery"]),
                        "delivery_fact_missing", "completed needs actual output delivery")
            else:
                require("formed_at" in times and "material_ref" in record,
                        "delivery_fact_missing", "legacy completed needs formed_at and material_ref")

    def scientific(self, task, record, *, admission=False, preserve_existing=False):
        if "scientific_work_ref" not in task:
            return  # Existing input JSON may already contain unrelated scientific_work data.
        self.protocol_mapping(task, record)
        ref = obj(task["scientific_work_ref"], "scientific_work_ref")
        require(any(r.get("path") == ref.get("path") and r.get("sha256") == ref.get("sha256")
                    for r in task["input_refs"]), "scientific_input_ref", "contract must be an existing input ref")
        work = obj(self.ref(ref, True).get("scientific_work"), "scientific_work")
        require(type(work.get("schema_version")) is int and work["schema_version"] in {1, 2},
                "scientific_schema", "scientific_work v1 or v2 required")
        text(work.get("decision_question"), "decision_question")
        premises = obj(work.get("premises"), "premises")
        require(set(premises) == {"required_facts", "allowed_assumptions", "deferred_unknowns"},
                "scientific_premises", "explicit premises required")
        for k in premises:
            strings(premises[k], "premises." + k)
        routes = obj(work.get("outcome_routes"), "outcome_routes")
        require(set(routes) == {"supported", "refuted", "unknown"}, "scientific_routes", "three outcome routes required")
        for k in routes:
            text(routes[k], "outcome_routes." + k)
        delivery = obj(work.get("deliverable"), "deliverable")
        mapping = self.protocol_mapping(task, record)
        if mapping and "deliverable" in mapping:
            revised = obj(mapping["deliverable"], "mapped deliverable")
            require(revised.get("version") == delivery.get("version") and revised.get("reader_id") == delivery.get("reader_id"),
                    "protocol_delivery_binding", "migration preserves version and actual receiving Leader")
            delivery = revised
        require(delivery.get("format") in {"markdown", "message"}, "scientific_format", "markdown or message required")
        if delivery["format"] == "message":
            require(work["schema_version"] == 2 or mapping is not None, "scientific_schema", "message delivery requires explicit v2 adoption")
            text(delivery.get("version"), "version")
            text(delivery.get("reader_id"), "reader_id")
            self.delivery_route(task, record, delivery, admission=admission)
            if admission:
                self.delivery_readiness(task, delivery, mapping)
            output = record.get("output_delivery")
            if output is not None:
                self.delivery_facts(output)
                reference = obj(output.get("message_ref"), "message_ref")
                require(reference["task_id"] == task["task_id"] and reference["author_id"] == task["agent_id"] and
                        reference["reader_id"] == delivery["reader_id"] and reference["version"] == output["version"] == delivery["version"],
                        "message_delivery_binding", "message must bind actual author, receiver, task and version")
                for key, actor, kind in (("producer_receipt_ref", task["agent_id"], "scientific_delivery"),
                                          ("read_receipt_ref", delivery["reader_id"], "scientific_read")):
                    if key == "read_receipt_ref" and "read_at" not in output:
                        continue
                    receipt = parse(self.native_message(output.get(key), actor, parse(self.read(self.raw_run / "状态.json"), "root")["total_leader"]), kind)
                    require(receipt.get("kind") == kind and receipt.get("message_ref") == reference and
                            receipt.get("version") == output["version"], "message_receipt_binding", "native full-original receipt differs")
                    clock = "formed_at" if key == "producer_receipt_ref" else "read_at"
                    require(timestamp(receipt.get(clock), clock) == timestamp(output.get(clock), clock),
                            "message_receipt_time", "native receipt time differs")
                if "read_at" in output:
                    require(output["reader_id"] == delivery["reader_id"], "output_reader", "actual reader differs")
            if record["status"] == "completed":
                require(output is not None and all(k in output for k in ("retained_at", "read_at")) and
                        ("delivered_at" in output or "received_at" in output),
                        "scientific_delivery_missing", "completed message needs delivery, retention and full read")
                self.delivery_readiness(task, delivery, mapping)
            return
        text(delivery.get("version"), "deliverable.version")
        text(delivery.get("reader_id"), "deliverable.reader_id")
        route = self.delivery_route(task, record, delivery, admission=admission)
        if admission:
            self.delivery_readiness(task, delivery, mapping)
        output = record.get("output_delivery")
        if output is not None:
            self.delivery_facts(output)
            require(output["version"] == delivery["version"], "output_version", "version differs from contract")
            material = self.ref(output["material_ref"])
            require(material.suffix.lower() == ".md", "scientific_format", "readable Markdown artifact required")
            if route is not None or admission or not preserve_existing:
                self.material_target(task, output["material_ref"]["path"])
            if route is not None:
                require(material == route["target"], "output_target", "material differs from declared delivery target")
            require(bool(self.read(material).decode("utf-8").strip()), "scientific_empty", "empty deliverable")
            if "reader_id" in output:
                require(output["reader_id"] == delivery["reader_id"], "output_reader", "wrong recipient")
        if record["status"] == "completed":
            require(output is not None and all(k in output for k in ("delivered_at", "archived_at", "read_at")),
                    "scientific_delivery_missing", "completed needs delivered, archived and actually read output")

    def recovery(self, task, control):
        rec = task.get("recovery_of")
        recoveries = obj(control["recoveries"], "recoveries")
        seen = set()
        replacements = set()
        for key, value in recoveries.items():
            obj(value, "recovery")
            old = text(value.get("task_id"), "recovery.task_id")
            fail = text(value.get("failure_id"), "recovery.failure_id")
            pair = (old, fail)
            require(pair not in seen, "recovery_replay", "one replacement per failure")
            seen.add(pair)
            require(key == old + "/" + fail, "recovery_key", key)
            target = text(value.get("replacement_task_id"), "replacement_task_id")
            require(target in control["tasks"] and old in control["tasks"], "recovery_unregistered", target)
            require(target not in replacements, "recovery_replay", "replacement binds one failure")
            replacements.add(target)
        if rec is None:
            require(not any(v["replacement_task_id"] == self.task_id for v in recoveries.values()),
                    "recovery_missing", self.task_id)
            return
        obj(rec, "recovery_of")
        old_id = text(rec.get("task_id"), "recovery_of.task_id")
        failure = text(rec.get("failure_id"), "recovery_of.failure_id")
        require(old_id != self.task_id, "recovery_self", old_id)
        prior = obj(control["tasks"].get(old_id), "prior task")
        self.ref(prior.get("ref"), True)
        require(prior.get("failure_id") == failure and prior.get("status") in {"failed", "stopped"},
                "recovery_failure", "specific recorded failure required")
        row = recoveries.get(old_id + "/" + failure, {})
        require(row.get("replacement_task_id") == self.task_id and row.get("task_id") == old_id
                and row.get("failure_id") == failure, "recovery_replay", "failure already recovered or unregistered")
        # Other envelopes cannot claim the same failure, even under another epoch.
        for tid, record in control["tasks"].items():
            if tid not in {self.task_id, old_id}:
                other = self.ref(record.get("ref"), True)
                require(other.get("recovery_of") != rec, "recovery_replay", tid)
        self.stopped(old_id, prior)

    def unstarted_cancelled(self, tid, record, owner):
        """Verify positive non-dispatch evidence, never infer it from absent runtime fields."""
        marker = obj(record.get("unstarted_cancellation"), "unstarted_cancellation")
        require(set(marker) == {"receipt_ref", "cancelled_at"}, "unstarted_receipt_invalid", "cancellation marker fields")
        ref = obj(record.get("ref"), "task.ref")
        task = self.ref(ref, True)
        receipt = self.ref(marker["receipt_ref"], True)
        expected = {"schema_version": 1, "run_id": self.run.name, "task_id": tid,
                    "task_sha256": ref["sha256"], "epoch": task.get("epoch"),
                    "agent_id": record.get("agent_id"), "stage": record.get("stage"),
                    "status": "not_started", "success": True, "confirmed_by": text(owner, "owner"),
                    "delivery_enqueued": False, "delivered": False, "accepted": False,
                    "started": False, "pending_delivery": False, "source_actions_executed": 0}
        require(set(receipt) == set(expected) | {"cancelled_at", "dispatch_check_report", "observation"},
                "unstarted_receipt_invalid", "receipt fields")
        require(all(type(receipt.get(k)) is type(v) and receipt[k] == v for k, v in expected.items()),
                "unstarted_receipt_invalid", "receipt task, owner or execution binding differs")
        require(task.get("schema_version") == 1 and type(task["schema_version"]) is int and
                task.get("run_id") == self.run.name and task.get("task_id") == tid and
                task.get("agent_id") == record.get("agent_id") and task.get("stage") == record.get("stage"),
                "route_mismatch", tid)
        number(task.get("epoch"), "task.epoch")
        if not hasattr(self, "_cancellation_epoch"):
            current = obj(parse(self.read(self.raw_run / "状态.json"), "root").get("harness_control"), "harness_control")
            self._cancellation_epoch = number(current.get("epoch"), "epoch")
        require(0 < task["epoch"] <= self._cancellation_epoch, "epoch_stale", tid)
        when = timestamp(receipt.get("cancelled_at"), "cancelled_at")
        require(when == timestamp(marker.get("cancelled_at"), "cancelled_at"), "unstarted_receipt_invalid", "cancellation time differs")
        observation = obj(receipt.get("observation"), "observation")
        require(set(observation) == {"kind", "id", "observer_id", "observed_at"} and
                observation.get("kind") in {"message", "tool"} and observation.get("observer_id") == owner,
                "unstarted_receipt_invalid", "root owner observation required")
        text(observation.get("id"), "observation.id")
        require(timestamp(observation.get("observed_at"), "observed_at") >= when,
                "event_order", "observation precedes cancellation")
        report = obj(receipt.get("dispatch_check_report"), "dispatch_check_report")
        require(report.get("ok") is False and report.get("decision") in {"fail", "hold"} and
                report.get("phase") == "dispatch" and report.get("task_id") == tid and
                type(report.get("diagnostics")) is list and any(type(d) is dict and
                    d.get("level") in {"error", "hold"} and type(d.get("code")) is str and d["code"]
                    for d in report["diagnostics"]), "unstarted_receipt_invalid", "failed task dispatch evidence required")
        require("dispatch_check_report" not in record or record["dispatch_check_report"] == report,
                "unstarted_conflict", "recorded dispatch report differs")
        if "dispatch_checked_at_utc" in record:
            require(timestamp(record["dispatch_checked_at_utc"], "dispatch_checked_at_utc") <= when,
                    "event_order", "cancellation precedes dispatch rejection")
        require(record.get("status") in {"registered", "stopped"} and "failure_id" not in record,
                "unstarted_conflict", "only never-started registered or administrative stopped tasks may be cancelled")
        forbidden = {"delivery_enqueued_at", "delivered_at", "accepted_at", "started_at", "runtime_id",
                     "entry_receipt", "entry_receipt_ref", "stop_receipt_ref", "output_delivery"}
        require(not forbidden.intersection(record) and record.get("runtime_state") in (None, "unknown"),
                "unstarted_conflict", "known delivery, execution or output fact exists")
        for key in ("actor_started_or_delivered", "delivery_enqueued", "delivered", "accepted", "started", "pending_delivery", "source_actions_executed"):
            value = 0 if key == "source_actions_executed" else False
            require(key not in record or (type(record[key]) is type(value) and record[key] == value),
                    "unstarted_conflict", key)
        for key, value in obj(record.get("action_used", {}), "action_used").items():
            require(number(value, key) == 0, "unstarted_conflict", "recorded action was executed: " + key)
        for value in obj(record.get("lifecycle_events", {}), "lifecycle_events").values():
            require(obj(value.get("event"), "lifecycle event").get("type") in {"register", "cancel_unstarted"},
                    "unstarted_conflict", "recorded lifecycle execution fact exists")
        if "material_ref" in record:
            material = obj(record["material_ref"], "material_ref")
            require(material.get("sha256") == ref["sha256"] and self.ref(material) == self.ref(ref),
                    "unstarted_conflict", "only the preparation envelope may be a legacy material")
        require(re.sub(r"[^a-z0-9]", "", observation["id"].lower()) not in {"notaskfound", "failedjson"},
                "unstarted_receipt_invalid", "a bare error label is not a positive non-dispatch observation")
        return receipt

    def stopped(self, old_id, prior):
        require(prior.get("runtime_state") == "stopped", "runtime_not_stopped", old_id)
        receipt = self.ref(prior.get("stop_receipt_ref"), True)
        for field, expected in (("task_id", old_id), ("agent_id", prior["agent_id"]),
                                ("runtime_id", text(prior.get("runtime_id"), "runtime_id")),
                                ("status", "stopped"), ("success", True)):
            require(type(receipt.get(field)) is type(expected) and receipt[field] == expected,
                    "stop_receipt_invalid", field)
        stop_time = timestamp(receipt.get("stopped_at"), "stopped_at")
        if "started_at" in prior:
            require(stop_time >= timestamp(prior["started_at"], "started_at"), "stop_receipt_invalid", "stop precedes start")
        if "stopped_at" in prior:
            require(stop_time == timestamp(prior["stopped_at"], "stopped_at"), "stop_receipt_invalid", "stopped_at conflict")
        require(not receipt.get("error") and not receipt.get("failed") and
                not re.search(r"notaskfound|failedjson", re.sub(r"[^a-z0-9]", "", json.dumps(receipt).lower())),
                "stop_receipt_invalid", "failed or missing-runtime receipt")

    def ancestors(self, task, control):
        """Validate every recovery edge and retain all ancestor refusal facts."""
        result = set()
        child = task
        while "recovery_of" in child:
            rec = obj(child["recovery_of"], "recovery_of")
            tid = text(rec.get("task_id"), "recovery_of.task_id")
            failure = text(rec.get("failure_id"), "recovery_of.failure_id")
            require(tid not in result and tid != task["task_id"], "recovery_cycle", tid)
            prior = obj(control["tasks"].get(tid), "ancestor task")
            row = control["recoveries"].get(tid + "/" + failure, {})
            require(row.get("task_id") == tid and row.get("failure_id") == failure
                    and row.get("replacement_task_id") == child["task_id"], "recovery_replay", tid)
            require(prior.get("failure_id") == failure and prior.get("status") in {"failed", "stopped"},
                    "recovery_failure", tid)
            self.stopped(tid, prior)
            parent = self.ref(prior.get("ref"), True)
            require(parent.get("task_id") == tid and parent.get("agent_id") == prior.get("agent_id")
                    and parent.get("stage") == prior.get("stage"), "route_mismatch", tid)
            result.add(tid)
            child = parent
        return result

    def resolved_failure(self, tid, control):
        """A completed, registered replacement chain resolves its logical task."""
        seen = set()
        while tid not in seen:
            seen.add(tid)
            old = control["tasks"][tid]
            if old.get("status") == "completed":
                self.facts(old)
                return True
            failure = old.get("failure_id")
            if old.get("status") not in {"failed", "stopped"} or not failure:
                return False
            row = control["recoveries"].get(tid + "/" + failure, {})
            target = row.get("replacement_task_id")
            if target not in control["tasks"]:
                return False
            self.stopped(tid, old)
            child_record = control["tasks"][target]
            child = self.ref(child_record.get("ref"), True)
            require(child.get("agent_id") == child_record.get("agent_id") and child.get("stage") == child_record.get("stage"),
                    "route_mismatch", target)
            require(child.get("task_id") == target and child.get("recovery_of") == {"task_id": tid, "failure_id": failure},
                    "recovery_failure", target)
            self.ancestors(child, control)
            tid = target
        raise GateError("recovery_cycle", tid)

    def legacy_items(self, ledger):
        """Read archived exact contracts without changing their counting scopes."""
        budget = obj(ledger.get("new_budget", ledger), "legacy budget")
        items = budget.get("contracts")
        require(type(items) is list, "legacy_schema", "contracts missing", "hold")
        if "contracts_archive" in budget:
            archive = self.ref(budget["contracts_archive"], True)
            full = {x["task"]: x for x in archive.get("contracts", [])}
            merged = []
            for x in items:
                require(x.get("task") in full, "legacy_archive", "contract not archived", "hold")
                y = dict(full[x["task"]])
                for k in ("state", "initial_GET_used_this_run", "normal_Location_used_this_run",
                          "remaining_initial_GET", "receipt"):
                    if k in x:
                        y[k] = x[k]
                merged.append(y)
            items = merged
        return items

    def legacy(self, ledger):
        """Extract only exact known fields; historical totals never become limits."""
        items = self.legacy_items(ledger)
        mapped = []
        for c in items:
            obj(c, "legacy contract")
            if all(k in c for k in ("max_initial_GET", "initial_GET_used_this_run", "legacy_initial_used")):
                mapped.append({"contract_id": c.get("task"), "uri": c.get("url"),
                               "limit": number(c["max_initial_GET"], "max_initial_GET"),
                               "used_this_run": number(c["initial_GET_used_this_run"], "initial_GET_used_this_run"),
                               "legacy_used": number(c["legacy_initial_used"], "legacy_initial_used"),
                               "remaining_declared": c.get("remaining_initial_GET")})
        self.notice("legacy_exact_fields", json.dumps(mapped, ensure_ascii=False))
        raise GateError("legacy_unresolved", "hold: canonical aliases/account history, explicit shared limits/used, "
                        "cache validity, service/protection stops and permission refusals lack structured evidence; "
                        "text cache_check/stop_check and cumulative query/GET are not authorization", "hold")

    def legacy_index(self, references, selected_uris, mappings, aid, account):
        """Independent v1 resources may coexist with immutable, unmapped old books.

        Any exact URI or known alias collision holds until that resource has a
        cumulative v1 account. Natural-language unknowns never grant a reset.
        """
        require(type(references) is list, "type", "legacy_refs must be an array")
        for ref in references:
            legacy = self.ref(ref, True)
            require(legacy.get("shared_required") is not True, "legacy_shared_unmapped",
                    "explicit ledger-wide shared condition needs v1 mapping", "hold")
            known = set()
            ownership = legacy.get("current_uri_ownership", {})
            obj(ownership, "legacy.current_uri_ownership")
            known.update(ownership)
            items = self.legacy_items(legacy)
            for c in items:
                obj(c, "legacy contract")
                uri = c.get("url", c.get("uri"))
                if type(uri) is str:
                    known.add(uri)
                for field in ("aliases", "known_aliases"):
                    if field in c:
                        known.update(strings(c[field], "legacy." + field))
            if known & selected_uris:
                self.legacy_mapping(ref, legacy, items, mappings, aid, account, selected_uris)

    def legacy_mapping(self, legacy_ref, legacy, items, references, aid, account, uris):
        """Attested exact-resource mapping; raw books and accounting scopes stay intact."""
        require(type(references) is list, "type", "legacy_mappings must be an array")
        candidates = [self.ref(r, True) for r in references]
        matches = [m for m in candidates if m.get("legacy_ref") == legacy_ref and m.get("account_id") == aid]
        require(len(matches) == 1, "legacy_resource_unmapped", "one hash-bound mapping required", "hold")
        m = matches[0]
        require(type(m.get("schema_version")) is int and m["schema_version"] == 1, "legacy_mapping_schema", aid, "hold")
        text(m.get("confirmed_by"), "mapping.confirmed_by")
        timestamp(m.get("confirmed_at"), "mapping.confirmed_at")
        require(m["confirmed_by"] == account.get("owner"), "legacy_mapping_owner", aid, "hold")
        mapped_uris = set(strings(m.get("uris"), "mapping.uris"))
        require(mapped_uris <= uris, "legacy_alias_omitted", "mapping URI not in canonical account", "hold")
        ids = strings(m.get("contract_ids"), "mapping.contract_ids")
        selected = []
        ownership = legacy.get("current_uri_ownership", {})
        for c in items:
            names = {c.get("url", c.get("uri"))}
            names.discard(None)
            for k in ("aliases", "known_aliases"):
                names.update(strings(c.get(k, []), "legacy." + k))
            names.update(u for u, row in ownership.items() if type(row) is dict and row.get("task") == c.get("task"))
            if names & uris:
                require(names <= mapped_uris and names <= uris, "legacy_alias_omitted", str(c.get("task")), "hold")
                require(c.get("task") in ids, "legacy_contract_omitted", str(c.get("task")), "hold")
                selected.append(c)
        require(bool(selected) and set(ids) == {c.get("task") for c in selected}, "legacy_contract_omitted", aid, "hold")
        basis = m.get("used_basis")
        require(basis in {"disjoint", "cumulative"}, "legacy_counter_scope_unknown", aid, "hold")
        known_used, known_limits = [], []
        for c in selected:
            fields = ("max_initial_GET", "initial_GET_used_this_run", "legacy_initial_used",
                      "max_normal_Location", "normal_Location_used_this_run", "query_cost")
            require(all(k in c for k in fields), "legacy_counter_unknown", str(c.get("task")), "hold")
            values = {k: number(c[k], "legacy." + k) for k in fields}
            initial, previous = values["initial_GET_used_this_run"], values["legacy_initial_used"]
            if basis == "cumulative":
                require(initial >= previous, "legacy_counter_scope_unknown", "cumulative excludes previous use", "hold")
                total = initial
            else:
                total = initial + previous
            location = values["normal_Location_used_this_run"]
            if previous and values["max_normal_Location"] and basis == "disjoint":
                require("legacy_normal_Location_used" in c, "legacy_counter_unknown", "old Location use missing", "hold")
                location += number(c["legacy_normal_Location_used"], "legacy Location")
            if previous and values["query_cost"] and basis == "disjoint":
                require("legacy_query_used" in c, "legacy_counter_unknown", "old query use missing", "hold")
            query = initial * values["query_cost"]
            if basis == "disjoint" and previous and values["query_cost"]:
                query += number(c["legacy_query_used"], "legacy query")
            known_used.append({"initial_get": total, "normal_location": location, "query": query})
            known_limits.append({"initial_get": values["max_initial_GET"], "normal_location": values["max_normal_Location"],
                                 "query": values["max_initial_GET"] * values["query_cost"]})
            require(all(k in c for k in ("max_bytes", "timeout_seconds", "retry")), "legacy_conditions_unknown", aid, "hold")
            transport = {"max_bytes": c["max_bytes"], "timeout_seconds": c["timeout_seconds"], "retries": c["retry"]}
            constraints(transport, "legacy.constraints")
            require(m.get("constraints") == transport, "legacy_conditions_unknown", aid, "hold")
        combine = sum if basis == "disjoint" else max
        floor = {k: combine(x[k] for x in known_used) for k in COUNTERS}
        old_limits = {k: combine(x[k] for x in known_limits) for k in COUNTERS}
        constraints(m.get("constraints"), "mapping.constraints")
        mapped_used = counts(m.get("used"), "mapping.used")
        mapped_limits = counts(m.get("limits"), "mapping.limits")
        require(all(mapped_used[k] >= floor[k] for k in COUNTERS), "legacy_usage_lost", aid, "hold")
        require(mapped_limits == old_limits, "legacy_conditions_unknown", "mapping changes old finite limits", "hold")
        require(all(account["used"][k] >= mapped_used[k] for k in COUNTERS), "legacy_usage_lost", aid, "hold")
        if account["limits"] != mapped_limits or account["constraints"] != m["constraints"]:
            snapshots = [self.ref(r, True) for r in account.get("history_refs", [])]
            require(any(x.get("limits") == mapped_limits and x.get("constraints") == m["constraints"]
                        and all(x.get("used", {}).get(k, -1) >= mapped_used[k] for k in COUNTERS) for x in snapshots),
                    "legacy_conditions_unknown", "changed conditions need preserved old account and authorized revision", "hold")
        evidence = list(selected)
        for c in selected:
            if "receipt" in c:
                receipt = self.ref(c["receipt"], True)
                evidence.append(receipt)
        facts = m.get("facts")
        require(type(facts) is dict, "legacy_facts_unknown", aid, "hold")
        for k in ("protected", "permission_denied", "stopped", "shared_required"):
            require(type(facts.get(k)) is bool, "legacy_facts_unknown", k, "hold")
            require(all(k not in c or type(c[k]) is bool for c in evidence), "legacy_facts_unknown", k, "hold")
            known_true = any(c.get(k) is True for c in evidence)
            if k == "protected":
                known_true = known_true or any(c.get("protection_detected") is True for c in evidence)
            if k == "stopped":
                stops = legacy.get("current_exact_stops", {})
                known_true = known_true or (type(stops) is dict and bool(set(stops) & mapped_uris))
                known_true = known_true or any(c.get("http_status") in {401, 403, 429} or c.get("channel_stopped") is True
                                              or c.get("stopped_exact_URI") is True for c in evidence)
            require(not known_true or facts[k], "legacy_restriction_lost", k, "hold")
            require(not facts[k] or account.get(k) is True, "legacy_restriction_lost", k, "hold")
            if k == "shared_required" and facts[k]:
                require(all(type(c.get("shared_budget")) is dict for c in selected if c.get(k) is True),
                        "legacy_shared_unmapped", "old explicit shared condition lacks limits/used", "hold")
                for c in selected:
                    if c.get(k) is True:
                        old_shared = c["shared_budget"]
                        new_shared = account.get("shared_budget", {})
                        old_l = counts(old_shared.get("limits"), "legacy.shared.limits")
                        old_u = counts(old_shared.get("used"), "legacy.shared.used")
                        require(new_shared.get("limits") == old_l and
                                all(new_shared.get("used", {}).get(key, -1) >= old_u[key] for key in COUNTERS),
                                "legacy_shared_unmapped", "old shared limits/use changed", "hold")
        inflight = strings(facts.get("inflight"), "mapping.inflight")
        old_inflight = strings(legacy.get("inflight", []), "legacy.inflight")
        old_inflight += [c.get("url", c.get("uri")) for c in selected if c.get("state") == "request_started"]
        require(set(old_inflight) & mapped_uris <= set(inflight), "legacy_inflight_lost", aid, "hold")
        require(set(inflight) <= set(account.get("inflight", [])), "legacy_inflight_lost", aid, "hold")
        cache = obj(facts.get("cache"), "mapping.cache")
        require(cache.get("status") in {"absent", "available"}, "legacy_cache_unknown", aid, "hold")
        if cache["status"] == "available":
            self.ref(cache.get("ref"))
            require(account.get("cache") == cache, "legacy_cache_lost", aid, "hold")
        for c in evidence:
            old_cache = c.get("cache")
            if type(old_cache) is dict and old_cache.get("status") == "available":
                self.ref(old_cache.get("ref"))
                require(cache == old_cache, "legacy_cache_lost", aid, "hold")
            body = c.get("body")
            if c.get("state") == "body_obtained" and type(body) is dict and "sha256" in body:
                self.ref(body)
                require(cache["status"] == "available" and cache.get("ref", {}).get("sha256") == body["sha256"],
                        "legacy_cache_lost", "known obtained body must remain reusable", "hold")
        self.notice("legacy_resource_mapped", aid + "; raw ledger hash and cumulative facts retained")

    def sources(self, task, control):
        references = task.get("contract_refs", [])
        require(type(references) is list, "type", "contract_refs must be an array")
        actions = set(task["task_scope"]["allowed_actions"])
        needed = bool(actions & {"source_get", "source_query", "read_cache"})
        require(not needed or references, "contract_missing", "source action needs registered contract", "hold")
        if not references:
            return
        ledger = self.ref(control.get("source_ledger_ref"), True)
        if "schema_version" not in ledger:
            self.legacy(ledger)
        require(type(ledger.get("schema_version")) is int and ledger["schema_version"] == 1,
                "source_schema", "unsupported source ledger", "hold")
        registry = obj(ledger.get("contracts"), "contracts")
        accounts = obj(ledger.get("accounts"), "accounts")
        uri_owner = {}
        for aid, account in accounts.items():
            obj(account, "account")
            uris = strings(account.get("uris"), "account.uris")
            require(bool(uris), "account_uri_missing", aid, "hold")
            for uri in uris:
                require(uri not in uri_owner, "alias_account_conflict", uri)
                uri_owner[uri] = aid
        canonical = {}
        for cid, ref in registry.items():
            c = self.ref(ref, True)
            require(c.get("contract_id") == cid, "contract_registry", cid)
            require(c.get("account_id") in accounts, "account_missing", cid, "hold")
            require(uri_owner.get(c.get("uri")) == c["account_id"], "alias_account_conflict", cid)
            canonical[cid] = c
        selected = set()
        reserved = {}
        for ref in references:
            c = self.ref(ref, True)
            if "schema_version" not in c:
                raise GateError("legacy_contract", "legacy contract needs explicit v1 account mapping", "hold")
            cid = text(c.get("contract_id"), "contract_id")
            require(cid not in selected, "contract_duplicate", cid)
            selected.add(cid)
            require(cid in registry and self.path(ref["path"]) == self.path(registry[cid]["path"])
                    and ref["sha256"] == registry[cid]["sha256"], "contract_registry", cid)
            aid = c["account_id"]
            a = accounts[aid]
            self.legacy_index(ledger.get("legacy_refs", []), set(a["uris"]), ledger.get("legacy_mappings", []), aid, a)
            self.account(aid, a, [x for x in canonical.values() if x["account_id"] == aid], c)
            require(type(c.get("schema_version")) is int and c["schema_version"] == 1,
                    "contract_schema", cid, "hold")
            counts(c.get("limits"), "contract.limits")
            constraints(c.get("constraints"), "contract.constraints")
            for field in ("owner", "limits", "constraints"):
                require(c.get(field) == a.get(field), "contract_account_mismatch", cid + "." + field)
            costs = counts(c.get("costs"), cid + ".costs")
            op = c.get("operation")
            require(op in {"request", "cache"}, "contract_operation", cid, "hold")
            self.eligibility(c["owner"] == task["agent_id"], "source_owner", cid)
            if op == "cache":
                require("read_cache" in actions and not (actions & {"source_get", "source_query"})
                        and all(v == 0 for v in costs.values()), "cache_scope", cid)
                self.eligibility(a["cache"]["status"] == "available", "cache_unavailable", cid, "hold")
                if a["cache"]["status"] == "available":
                    self.ref(a["cache"].get("ref"))
            else:
                http_cost = costs["initial_get"] + costs["normal_location"]
                require(http_cost > 0 or costs["query"] > 0, "source_cost", cid)
                require("source_get" not in actions or http_cost > 0, "source_cost", cid + ": HTTP GET requires GET/Location debit")
                require("source_query" not in actions or costs["query"] > 0, "source_cost", cid + ": query requires query debit")
                require(not costs["initial_get"] or "source_get" in actions, "source_scope", cid)
                require(not costs["query"] or "source_query" in actions, "source_scope", cid)
                require(not costs["normal_location"] or "source_get" in actions, "source_scope", cid)
                for action, cost in (("source_get", http_cost), ("source_query", costs["query"])):
                    if cost:
                        require(action in task["task_scope"]["limits"], "scope_limit_missing", action, "hold")
                self.eligibility(not a["inflight"], "source_inflight", aid)
                self.eligibility(a["cache"]["status"] == "absent", "cache_reuse_required", aid, "hold")
                for field in ("protected", "permission_denied", "stopped"):
                    self.eligibility(not a[field], "source_" + field, aid)
            total = reserved.setdefault(aid, {k: 0 for k in COUNTERS})
            for k in COUNTERS:
                total[k] += costs[k]
                self.eligibility(total[k] <= a["remaining"][k], "source_exhausted", aid + "." + k)
            for action in ("source_get", "source_query"):
                all_cost = sum(v["initial_get"] + v["normal_location"] if action == "source_get" else v["query"]
                               for v in reserved.values())
                if all_cost:
                    self.eligibility(all_cost <= task["task_scope"]["limits"].get(action, 0) - self.action_used.get(action, 0), "scope_exhausted", action)
            if a["shared_required"]:
                require(type(a.get("shared_budget")) is dict, "shared_budget_unknown", aid, "hold")
                shared = a["shared_budget"]
                for field in ("limits", "used", "remaining"):
                    counts(shared.get(field), "shared." + field)
                for k in COUNTERS:
                    require(shared["remaining"][k] == shared["limits"][k] - shared["used"][k], "shared_budget_mismatch", k)
                    # Only this account is supported per shared budget; unidentified pools hold.
                    require(shared.get("account_ids") == [aid], "shared_pool_unknown", aid, "hold")
                    self.eligibility(total[k] <= shared["remaining"][k], "shared_exhausted", k)

    def account(self, aid, a, contracts, current):
        text(a.get("owner"), "account.owner")
        limits, used, remaining = (counts(a.get(k), aid + "." + k) for k in ("limits", "used", "remaining"))
        for k in COUNTERS:
            require(used[k] <= limits[k] and remaining[k] == limits[k] - used[k], "budget_mismatch", aid + "." + k)
        transport = constraints(a.get("constraints"), "account.constraints")
        for k in ("protected", "permission_denied", "stopped", "shared_required"):
            require(k in a, "source_fact_unknown", aid + "." + k, "hold")
            flag(a[k], k)
        strings(a.get("inflight"), "inflight")
        cache = obj(a.get("cache"), "cache")
        require(cache.get("status") in {"absent", "available", "unknown"}, "cache_unknown", aid, "hold")
        if cache["status"] == "available":
            self.ref(cache.get("ref"))
        history = a.get("history_refs")
        require(type(history) is list, "history_unknown", aid, "hold")
        chain = []
        node = current
        seen = set()
        while True:
            digest = hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()
            require(digest not in seen, "revision_cycle", aid)
            seen.add(digest)
            chain.append(node)
            if "revision_of" not in node:
                break
            node = self.ref(node["revision_of"], True)
        for c in contracts:
            floor = counts(c.get("used_floor"), "contract.used_floor")
            require(all(used[k] >= floor[k] for k in COUNTERS), "usage_reset", aid)
        for r in history:
            old = self.ref(r, True)
            require(old.get("account_id") == aid, "history_account", aid)
            old_limits = counts(old.get("limits"), "history.limits")
            old_transport = constraints(old.get("constraints"), "history.constraints")
            if old_limits != limits or old_transport != transport:
                require(any(c.get("previous_account_ref") == r and "revision_of" in c for c in chain),
                        "revision_required", "account conditions changed without old contract authorization", "hold")
            for k, v in counts(old.get("used"), "history.used").items():
                require(used[k] >= v, "usage_reset", aid + "." + k)
            for uri in strings(old.get("uris"), "history.uris"):
                require(uri in a["uris"], "alias_history_lost", uri)
            old_cache = obj(old.get("cache"), "history.cache")
            require(old_cache.get("status") in {"absent", "available", "unknown"}, "cache_unknown", aid, "hold")
            if old_cache["status"] == "available":
                self.ref(old_cache.get("ref"))
                require(cache["status"] == "available" and cache["ref"]["sha256"] == old_cache["ref"]["sha256"],
                        "cache_history_lost", aid)
            for k in ("protected", "permission_denied", "stopped"):
                require(k in old, "history_unknown", k, "hold")
                require(not flag(old[k], k) or a[k], "restriction_reset", k)
            for inflight in strings(old.get("inflight"), "history.inflight"):
                if inflight not in a["inflight"]:
                    receipts = obj(a.get("terminal_receipt_refs", {}), "terminal_receipt_refs")
                    require(inflight in receipts, "inflight_history_lost", inflight, "hold")
                    receipt = self.ref(receipts[inflight], True)
                    require(receipt.get("request_id") == inflight and receipt.get("terminal") is True,
                            "terminal_receipt_invalid", inflight)
                    finished_time(receipt)
        for c in chain:
            require(c.get("account_id") == aid and c.get("uri") in a["uris"], "revision_account", aid)
            counts(c.get("limits"), "chain.limits")
            constraints(c.get("constraints"), "chain.constraints")
            floor = counts(c.get("used_floor"), "contract.used_floor")
            require(all(used[k] >= floor[k] for k in COUNTERS), "usage_reset", aid)
            if "revision_of" in c:
                old = self.ref(c["revision_of"], True)
                require(c.get("previous_account_ref") in history, "revision_history_missing", aid, "hold")
                require(old.get("account_id") == aid and old.get("uri") in a["uris"], "revision_account", aid)
                old_floor = counts(old.get("used_floor"), "revision.used_floor")
                require(all(used[k] >= old_floor[k] and floor[k] >= old_floor[k] for k in COUNTERS), "usage_reset", aid)
                auth = self.ref(c.get("authorization_ref"), True)
                counts(auth.get("limits"), "authorization.limits")
                constraints(auth.get("constraints"), "authorization.constraints")
                require(auth.get("authorized") is True and auth.get("contract_id") == c["contract_id"]
                        and auth.get("account_id") == aid and auth.get("previous_sha256") == c["revision_of"]["sha256"]
                        and auth.get("limits") == c.get("limits") and auth.get("constraints") == c.get("constraints"),
                        "revision_unauthorized", c["contract_id"])
                text(auth.get("authorized_by"), "authorized_by")
                timestamp(auth.get("at"), "authorization.at")
            elif c is current and (c.get("limits") != limits or c.get("constraints") != transport):
                raise GateError("revision_required", "requested old contract differs from current account conditions", "hold")

    def check(self):
        root = parse(self.read(self.raw_run / "状态.json"), "root")
        task_bytes = self.read(self.raw_task_path)
        task = parse(task_bytes, "task")
        require(type(task.get("schema_version")) is int and task["schema_version"] == 1, "task_schema", "v1 required", "hold")
        self.task_id = text(task.get("task_id"), "task_id")
        require(root.get("run_id", self.run.name) == self.run.name and task.get("run_id") == self.run.name,
                "run_id", "run_id must match directory name")
        control = obj(root.get("harness_control"), "harness_control")
        require(type(control.get("schema_version")) is int and control["schema_version"] == 1,
                "control_schema", "v1 required", "hold")
        number(control.get("epoch"), "epoch")
        require(control["epoch"] > 0, "epoch", "epoch must be positive")
        number(task.get("epoch"), "task.epoch")
        record = obj(control["tasks"].get(self.task_id), "registered task")
        mapping = self.protocol_mapping(task, record)
        require(task["epoch"] == control["epoch"] or mapping is not None, "epoch_stale", self.task_id)
        if effective_pause(root, control) and self.phase != "checkpoint":
            raise GateError("run_paused", "dispatch and entry remain paused")
        if self.phase == "checkpoint":
            self.notice("preserve_only", "checkpoint does not authorize dispatch, sources or resumed work")
        tasks = obj(control.get("tasks"), "tasks")
        record = obj(tasks.get(self.task_id), "registered task")
        require(self.ref(record.get("ref")) == self.task_path, "task_ref_path", self.task_id)
        self.task_binding(task, record)
        if "unstarted_cancellation" in record:
            self.unstarted_cancelled(self.task_id, record, root.get("total_leader"))
            require(self.phase == "checkpoint", "task_cancelled", self.task_id)
        self.ref(task.get("role_ref"), norm=True)
        inputs = task.get("input_refs")
        require(type(inputs) is list, "type", "input_refs must be an array")
        for reference in inputs:
            self.ref(reference, norm=reference != task.get("scientific_work_ref"))
        self.output(task.get("output_dir"))
        message_inputs = task.get("message_input_refs", [])
        require(type(message_inputs) is list, "type", "message_input_refs must be an array")
        for original in message_inputs:
            self.message_ref(original)
        scope = obj(task.get("task_scope"), "task_scope")
        require(set(scope) <= {"allowed_actions", "allowed_stages", "limits"}, "scope_unknown_fields",
                ", ".join(sorted(set(scope) - {"allowed_actions", "allowed_stages", "limits"})), "hold")
        actions = strings(scope.get("allowed_actions"), "scope.allowed_actions")
        require(bool(actions), "scope_empty", self.task_id)
        require(set(actions) <= ACTIONS, "action_unsupported", ",".join(sorted(set(actions) - ACTIONS)), "hold")
        require(task["stage"] in strings(scope.get("allowed_stages"), "scope.allowed_stages"), "scope_stage", task["stage"])
        limits = obj(scope.get("limits"), "scope.limits")
        for k, v in limits.items():
            require(k in actions, "scope_limit_action", k)
            number(v, "scope.limit." + k)
            self.eligibility(v > 0, "scope_exhausted", k + " has explicit zero allowance")
        used_actions = obj(record.get("action_used", {}), "action_used")
        self.action_used = used_actions
        for k, v in used_actions.items():
            require(k in actions, "scope_limit_action", k)
            number(v, "action_used." + k)
            if k in limits:
                self.eligibility(v < limits[k], "scope_exhausted", k + " recorded allowance exhausted")
        if set(limits) - {"source_get", "source_query"}:
            self.notice("local_counts_record_only", "local positive limits check recorded action_used only; no automatic action counting")
        permissions = obj(control.get("permissions"), "permissions")
        allowed = strings(permissions.get("allowed_actions"), "permissions.allowed_actions")
        denied = set(strings(permissions.get("denied_actions"), "permissions.denied_actions"))
        restrictions = control.get("restriction_refs", [])
        require(type(restrictions) is list, "type", "restriction_refs must be an array")
        recovery = task.get("recovery_of", {})
        obj(recovery, "recovery_of")
        ancestors = self.ancestors(task, control)
        permission_ancestors = set(ancestors)
        # A healthy new task retains this actual agent's previous recovery read/permission history.
        # Ordinary old task scopes and resolved failure identities are not inherited.
        historical_tasks = {}
        for tid, old in tasks.items():
            obj(old, "task record")
            if tid != self.task_id and old.get("agent_id") == task["agent_id"]:
                historical = self.ref(old.get("ref"), True)
                require(historical.get("task_id") == tid and historical.get("agent_id") == old.get("agent_id")
                        and historical.get("stage") == old.get("stage"), "route_mismatch", tid)
                historical_tasks[tid] = historical
                permission_ancestors.update(self.ancestors(historical, control))
        for r in restrictions:
            denied.update(strings(self.ref(r, True).get("denied_actions"), "restriction.denied_actions"))
        for tid, old in tasks.items():
            obj(old, "task record")
            if old.get("agent_id") == task["agent_id"] or tid in permission_ancestors:
                denied.update(strings(old.get("refusal_actions", []), "refusal_actions"))
            if tid != self.task_id and old.get("agent_id") == task["agent_id"]:
                cancelled = "unstarted_cancellation" in old
                if cancelled:
                    self.unstarted_cancelled(tid, old, root.get("total_leader"))
                if self.phase != "checkpoint" and not cancelled:
                    require(old.get("status") in {"completed", "stopped", "failed"}, "agent_busy", tid)
                    if old["status"] in {"failed", "stopped"}:
                        if old.get("runtime_state") not in {None, "unknown"}:
                            require(old["runtime_state"] == "stopped", "runtime_not_stopped", tid)
                        if (old.get("runtime_state") in {None, "unknown"} or old.get("runtime_id") is None or
                                old.get("stop_receipt_ref") is None):
                            self.execution_blocker = {
                                "task_id": tid, "agent_id": old.get("agent_id"), "task_ref": old.get("ref"),
                                "recorded_runtime_id": old.get("runtime_id"),
                                "required_evidence": ["A real stop receipt bound to the original task, agent and runtime.",
                                    "For a rejected, never-dispatched preparation only: cancel_unstarted with positive non-dispatch evidence."],
                                "unknown_is_not_evidence": True}
                            raise GateError("stop_evidence_missing", tid, "hold")
                        self.stopped(tid, old)
                    else:
                        try:
                            self.facts(old)
                            self.scientific(historical_tasks[tid], old, preserve_existing=True)
                        except GateError as error:
                            if error.code in {"delivery_fact_missing", "scientific_delivery_missing"}:
                                raise GateError(error.code, tid + ": " + error.detail, "hold")
                            raise
                if old.get("status") in {"failed", "stopped"} and old.get("failure_id"):
                    require(tid in ancestors or self.resolved_failure(tid, control),
                            "recovery_missing", "unresolved failed logical task needs registered recovery")
        require(set(actions) <= set(allowed) and not (set(actions) & denied), "permission_denied", "scope cannot elevate permissions")
        self.facts(record)
        self.scientific(task, record, admission=self.phase != "checkpoint",
                        preserve_existing=self.phase == "checkpoint")
        if self.phase == "dispatch":
            require(record["status"] == "registered" and "delivered_at" not in record, "task_replay", self.task_id)
        elif self.phase == "entry":
            require(record["status"] in {"delivered", "accepted", "started"}, "task_not_delivered", self.task_id)
            require("delivered_at" in record, "delivery_fact_missing", "entry needs actual delivery")
            require("accepted_at" in record, "acceptance_fact_missing", "entry needs actual acceptance")
            accepted = timestamp(record["accepted_at"], "accepted_at")
            require(accepted >= timestamp(record["delivered_at"], "delivered_at"), "fact_order", "acceptance precedes delivery")
            if record["status"] == "started":
                start = timestamp(record.get("started_at"), "started_at")
                require(start >= accepted, "fact_order", "start precedes acceptance")
        else:
            cp = obj(record.get("checkpoint"), "checkpoint")
            self.refs(cp.get("saved_refs"), "checkpoint.saved_refs")
            for original in cp.get("saved_messages", []):
                self.message_ref(original)
            missing = strings(cp.get("missing_sections"), "checkpoint.missing_sections")
            if missing:
                self.notice("checkpoint_incomplete", ", ".join(missing))
        self.recovery(task, control)
        self.sources(task, control)
        self.recheck()
        if self.phase == "entry":
            # Produce the receipt only after this exact read set passed its final recheck.
            self.receipt = {"task_id": self.task_id, "agent_id": task["agent_id"], "stage": task["stage"],
                            "epoch": control["epoch"], "phase": "entry", "decision": "allow", "ok": True,
                            "root_sha256": self.snapshots[self.root_path],
                            "task_sha256": self.snapshots[self.task_path],
                            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def check_task(run, task, phase, *, checkpoint_archive_ref=None):
    """Return the original CLI result without printing or mutating any file."""
    checker = None
    diagnostics = []
    try:
        require(type(phase) is str and phase in PHASES, "phase_invalid", "invalid phase")
        checker = Checker(run, task, phase, checkpoint_archive_ref=checkpoint_archive_ref)
        checker.check()
        decision, code = "allow", 0
    except GateError as e:
        decision, code = ("hold", 2) if e.level == "hold" else ("fail", 1)
        diagnostics.append({"level": e.level, "code": e.code, "detail": e.detail})
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as e:
        decision, code = "fail", 1
        diagnostics.append({"level": "error", "code": "invalid_data", "detail": str(e)})
    result = {"ok": code == 0, "decision": decision, "phase": phase,
              "diagnostics": (checker.diagnostics if checker else []) + diagnostics}
    if checker is not None:
        result["task_id"] = checker.task_id
    if checker is not None and getattr(checker, "execution_blocker", None):
        result["execution_blocker"] = checker.execution_blocker
    if checker is not None and checker.archive_used:
        result["checkpoint_archive"] = checker.archive_used
    if code == 0 and phase == "entry":
        result["receipt"] = checker.receipt
    return code, result


def inspect_run(run, task=None, status=None, agent=None, limit=20, cursor=None):
    """Bounded inventory only. Neither a gate decision nor an action permission."""
    try:
        require(type(limit) is int and 1 <= limit <= 100, "inspect_limit", "limit must be 1..100")
        c = Checker(run, "状态.json", "checkpoint")
        root = parse(c.read(c.raw_run / "状态.json"), "root")
        root_sha = c.snapshots[c.root_path]
        control = obj(root.get("harness_control"), "harness_control")
        tasks = obj(control.get("tasks"), "tasks")
        query = [str(task) if task is not None else None, status, agent, limit]
        offset = 0
        if cursor is not None:
            try:
                token = parse(bytes.fromhex(text(cursor, "cursor")), "cursor")
            except (ValueError, UnicodeError):
                raise GateError("inspect_cursor", "invalid cursor")
            require(token.get("root") == root_sha and token.get("query") == query,
                    "inspect_snapshot_changed", "cursor snapshot or filters changed")
            offset = number(token.get("offset"), "cursor.offset")
        ids = sorted(tasks)
        if task is not None:
            target = str(task)
            if target in tasks:
                ids = [target]
            else:
                target_path = c.path(target)
                ids = [tid for tid in ids if c.path(obj(tasks[tid], "task").get("ref", {}).get("path")) == target_path]
            require(bool(ids), "inspect_task_missing", "target is not registered", "hold")
        ids = [tid for tid in ids if (status is None or tasks[tid].get("status") == status)
               and (agent is None or tasks[tid].get("agent_id") == agent)]
        require(offset <= len(ids), "inspect_cursor", "offset exceeds filtered total")

        def short(value):
            if type(value) is not str:
                return value if type(value) in (int, bool) or value is None else None
            raw = value.encode("utf-8")
            return value if len(raw) <= 384 else raw[:380].decode("utf-8", "ignore") + "…"

        def brief_ref(value):
            if type(value) is not dict:
                return None
            return {k: short(value.get(k)) for k in ("path", "sha256")}

        pause_reasons = effective_pause(root, control)
        result = {"ok": True, "action_permission": False, "root_sha256": root_sha,
                  "snapshot": None, "total": len(ids), "truncated": False, "next_cursor": None,
                  "control": {"status": short(root.get("status")), "paused": short(control.get("paused")),
                              "epoch": short(control.get("epoch")), "goal_met": short(root.get("goal_met")),
                              "owner": short(root.get("total_leader")), "effective_paused": bool(pause_reasons),
                              "pause_reasons": pause_reasons}, "tasks": []}
        for tid in ids[offset:offset + limit]:
            row = obj(tasks[tid], "task record")
            envelope = c.ref(row.get("ref"), True)
            require(envelope.get("task_id") == tid and envelope.get("agent_id") == row.get("agent_id")
                    and envelope.get("stage") == row.get("stage"), "route_mismatch", tid)
            output = row.get("output_delivery", {})
            cp = row.get("checkpoint", {})
            missing = cp.get("missing_sections", []) if type(cp) is dict else []
            item = {"task_id": short(tid), "agent_id": short(row.get("agent_id")),
                    "stage": short(row.get("stage")), "status": short(row.get("status")),
                    "ref": brief_ref(row.get("ref")), "scientific_work_ref": brief_ref(envelope.get("scientific_work_ref")),
                    "output_ref": brief_ref(output.get("material_ref")) if type(output) is dict else None,
                    "output_kind": ("message" if "message_ref" in output else "file") if output else None,
                    "output_message_sha256": short(output.get("message_ref", {}).get("source", {}).get("payload_sha256")),
                    "output_version": short(output.get("version")) if type(output) is dict else None,
                    "missing_sections": [short(v) for v in missing[:4]] if type(missing) is list else [],
                    "missing_sections_total": len(missing) if type(missing) is list else None}
            scope = envelope.get("task_scope", {})
            actions = scope.get("allowed_actions", []) if type(scope) is dict else []
            source_work = type(actions) is list and any(type(a) is str and a in {"source_get", "source_query", "read_cache"} for a in actions)
            item["work_kind"] = "scientific" if envelope.get("scientific_work_ref") else "source" if source_work else "administrative_or_legacy"
            output_facts = output if type(output) is dict else {}
            marker = row.get("unstarted_cancellation", {})
            item["recorded_facts"] = {
                "delivered_at": short(row.get("delivered_at")), "accepted_at": short(row.get("accepted_at")),
                "started_at": short(row.get("started_at")), "output_formed_at": short(output_facts.get("formed_at")),
                "output_delivered_at": short(output_facts.get("delivered_at")),
                "archived_at": short(output_facts.get("archived_at")), "read_at": short(output_facts.get("read_at")),
                "runtime_state": short(row.get("runtime_state")), "runtime_id": short(row.get("runtime_id")),
                "stop_receipt_ref": brief_ref(row.get("stop_receipt_ref")),
                "unstarted_cancelled_at": short(marker.get("cancelled_at")) if type(marker) is dict else None}
            result["tasks"].append(item)
            # Reserve enough space for the continuation token and snapshot metadata.
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > INSPECT_BYTES - 2200:
                result["tasks"].pop()
                break
        science = [item for item in result["tasks"] if item["work_kind"] == "scientific"]
        result["progress"] = {"scope": "returned_tasks_only", "physical_activity": "not_checked",
                              "scientific_task_records": len(science),
                              "scientific_started_records": sum(item["recorded_facts"]["started_at"] is not None for item in science),
                              "scientific_output_records": sum(item["output_ref"] is not None for item in science)}
        advanced = offset + len(result["tasks"])
        result["truncated"] = advanced < len(ids)
        if result["truncated"]:
            require(advanced > offset, "inspect_item_size", "single summary exceeds output cap")
            result["next_cursor"] = json.dumps({"root": root_sha, "query": query, "offset": advanced},
                                                ensure_ascii=False, separators=(",", ":")).encode("utf-8").hex()
        result["snapshot"] = hashlib.sha256(json.dumps(sorted((str(p), sha) for p, sha in c.snapshots.items())).encode()).hexdigest()
        c.recheck()
        require(len(json.dumps(result, ensure_ascii=False).encode("utf-8")) + 1 <= INSPECT_BYTES,
                "inspect_output_size", "filters or cursor exceed output cap")
        return 0, result
    except GateError as e:
        code = 2 if e.level == "hold" else 1
        detail = e.detail.encode("utf-8")[:600].decode("utf-8", "ignore")
        return code, {"ok": False, "action_permission": False, "diagnostics": [{"level": e.level, "code": e.code, "detail": detail}]}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as e:
        return 1, {"ok": False, "action_permission": False, "diagnostics": [{"level": "error", "code": "invalid_data", "detail": str(e)[:200]}]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check")
    p.add_argument("--run", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--phase", choices=sorted(PHASES), required=True)
    p.add_argument("--checkpoint-archive")
    p.add_argument("--checkpoint-archive-sha256")
    p = sub.add_parser("inspect")
    p.add_argument("--run", required=True)
    p.add_argument("--task")
    p.add_argument("--status")
    p.add_argument("--agent")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--cursor")
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        if e.code:
            print(json.dumps({"ok": False, "decision": "fail", "diagnostics": [{"level": "error", "code": "cli_arguments", "detail": "invalid arguments"}]}))
        return e.code
    if args.command == "check":
        if bool(args.checkpoint_archive) != bool(args.checkpoint_archive_sha256):
            code, result = 1, {"ok": False, "decision": "fail", "phase": args.phase,
                               "diagnostics": [{"level": "error", "code": "cli_arguments", "detail": "archive path and sha256 must be paired"}]}
        else:
            archive_ref = {"path": args.checkpoint_archive, "sha256": args.checkpoint_archive_sha256} if args.checkpoint_archive else None
            code, result = check_task(args.run, args.task, args.phase, checkpoint_archive_ref=archive_ref)
    else:
        code, result = inspect_run(args.run, args.task, args.status, args.agent, args.limit, args.cursor)
    print(json.dumps(result, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
