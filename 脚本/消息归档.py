#!/usr/bin/env python3
"""Preserve one explicitly observed assistant message leaf. Offline; preview by default.

The owner supplies a fixed JSONL locator and payload digest, not transcript search.
No source transport, scientific rewriting, Root writes, reading receipts or completion.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path

sys.dont_write_bytecode = True
_SPEC = importlib.util.spec_from_file_location("message_archive_gate", Path(__file__).with_name("任务校验.py"))
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)
MAX_RECORD_BYTES = 2097152
MAX_PAYLOAD_BYTES = 1048576
READ_BLOCK_BYTES = 65536
SOURCE_FIELDS = {"path", "line", "record_uuid", "pointer", "raw_line_sha256", "payload_bytes",
                 "payload_sha256", "utf8_start", "utf8_end"}
OPTIONAL_FIELDS = {"tool_use_id", "task_id", "author_id", "version"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def hash_value(value, label):
    gate.require(type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value), "hash_format", label)
    return value


def utf8_json(data, label):
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise gate.GateError("json_invalid", label + " must use UTF-8")
    return gate.parse(text, label)


def read_locator(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        gate.require(stat.S_ISREG(info.st_mode), "message_locator_type", "regular locator JSON file required")
        gate.require(info.st_size <= 16384, "message_source_fields", "locator exceeds 16 KiB")
        data = os.read(fd, 16385)
    finally:
        os.close(fd)
    gate.require(len(data) <= 16384, "message_source_fields", "locator exceeds 16 KiB")
    return utf8_json(data, "source locator")


def identity(info):
    return info.st_dev, info.st_ino


def observed_line(stream, line):
    """Read fresh positional blocks; BufferedReader.seek may reuse stale prefetched bytes."""
    offset, current, length = 0, 1, 0
    pieces = []
    while True:
        block = os.pread(stream.fileno(), READ_BLOCK_BYTES, offset)
        if not block:
            gate.require(not length, "message_line_incomplete", "specified record needs its terminating LF", "hold")
            raise gate.GateError("message_line_missing", "specified raw LF line is absent", "hold")
        offset += len(block)
        start = 0
        while start < len(block):
            newline = block.find(b"\n", start)
            end = newline + 1 if newline >= 0 else len(block)
            length += end - start
            gate.require(length <= MAX_RECORD_BYTES, "message_size", "raw line exceeds 2 MiB")
            if current == line:
                pieces.append(block[start:end])
            if newline >= 0:
                if current == line:
                    return b"".join(pieces)
                current += 1
                length = 0
            start = end


def payload_leaf(raw, source, reader):
    record = utf8_json(raw, "message record")
    expected = source["record_uuid"]
    try:
        parsed_uuid = uuid.UUID(expected)
    except ValueError:
        raise gate.GateError("message_uuid", "canonical non-null UUID required")
    gate.require(str(parsed_uuid) == expected and parsed_uuid.int != 0,
                 "message_uuid", "canonical non-null UUID required")
    gate.require(record.get("uuid") == expected, "message_uuid", "specified record UUID differs")
    message = gate.obj(record.get("message"), "message")
    gate.require(record.get("type") == "assistant" and message.get("role") == "assistant",
                 "message_role", "canonical assistant record required")
    gate.require(not gate.flag(record.get("isApiErrorMessage", False), "isApiErrorMessage"),
                 "message_api_error", "API error placeholders are not scientific originals")
    match = re.fullmatch(r"/message/content/(0|[1-9][0-9]*)/(text|input/message)", source["pointer"])
    gate.require(match is not None, "message_pointer", "one supported complete assistant leaf required")
    blocks = message.get("content")
    index = int(match.group(1))
    gate.require(type(blocks) is list and index < len(blocks), "message_pointer", "text block is absent")
    block = gate.obj(blocks[index], "message block")
    if match.group(2) == "text":
        gate.require(block.get("type") == "text" and "tool_use_id" not in source,
                     "message_block", "assistant text block required")
        value = block.get("text")
    else:
        gate.require(block.get("type") == "tool_use" and block.get("name") == "SendMessage",
                     "message_block", "canonical SendMessage block required")
        gate.text(block.get("id"), "tool_use.id")
        if "tool_use_id" in source:
            gate.require(source["tool_use_id"] == block["id"], "message_tool_id", "tool use id differs")
        inputs = gate.obj(block.get("input"), "SendMessage.input")
        gate.require(inputs.get("to") == reader, "message_recipient", "message must target the receiving group owner")
        value = inputs.get("message")
    gate.text(value, "message payload")
    gate.require("\x00" not in value, "message_payload", "Markdown text cannot contain NUL")
    payload = value.encode("utf-8")
    gate.require(0 < len(payload) <= MAX_PAYLOAD_BYTES, "message_size", "payload must fit 1 MiB")
    gate.require(source["utf8_start"] == 0 and source["utf8_end"] == len(payload),
                 "message_range", "the entire selected string leaf is required")
    gate.require(source["payload_bytes"] == len(payload) and source["payload_sha256"] == digest(payload),
                 "message_payload_hash", "payload bytes or SHA-256 differs")
    return payload


def prepare(run, task, actor, expected_root_sha, source):
    gate.obj(source, "source")
    gate.require(SOURCE_FIELDS <= set(source) and set(source) <= SOURCE_FIELDS | OPTIONAL_FIELDS,
                 "message_source_fields", "exact locator fields required")
    for field in ("raw_line_sha256", "payload_sha256"):
        hash_value(source[field], field)
    for field in ("line", "payload_bytes", "utf8_start", "utf8_end"):
        gate.number(source[field], field)
    gate.require(0 < source["line"] <= 1000000 and 0 < source["payload_bytes"] <= MAX_PAYLOAD_BYTES,
                 "message_size", "bounded raw LF line and payload required")
    gate.text(source["record_uuid"], "record_uuid")
    gate.text(source["pointer"], "pointer")
    checker = gate.Checker(run, task, "checkpoint")
    original = checker.read(checker.raw_run / "状态.json")
    gate.require(digest(original) == hash_value(expected_root_sha, "expected_root_sha"), "cas_conflict", "Root changed")
    root = gate.parse(original, "root")
    control = gate.obj(root.get("harness_control"), "harness_control")
    gate.effective_pause(root, control)  # Preservation remains possible while paused; control fields still must be valid.
    gate.require(control.get("schema_version") == 1 and type(control["schema_version"]) is int,
                 "control_schema", "harness_control v1 required")
    task = gate.parse(checker.read(checker.raw_task_path), "task")
    gate.require(task.get("schema_version") == 1 and type(task["schema_version"]) is int, "task_schema", "task v1 required")
    gate.require(root.get("run_id", checker.run.name) == task.get("run_id") == checker.run.name,
                 "run_id", "task/Root/run mismatch")
    gate.number(task.get("epoch"), "task.epoch")
    gate.require(type(control.get("epoch")) is int and control["epoch"] > 0 and task.get("epoch") == control["epoch"],
                 "epoch_stale", "task epoch differs")
    row = gate.obj(control["tasks"].get(task.get("task_id")), "scientific task record")
    gate.require(checker.ref(row.get("ref")) == checker.task_path, "task_ref_path", "scientific task differs")
    checker.task_binding(task, row)
    gate.require(row.get("status") in {"started", "completed", "failed", "stopped"},
                 "archive_source_not_started", "scientific task must already have started", "hold")
    gate.require("scientific_work_ref" in task, "archive_plan_missing", "explicit scientific delivery plan required", "hold")
    checker.ref(task.get("role_ref"), norm=True)
    checker.refs(task.get("input_refs"), "input_refs")
    checker.facts(row)
    checker.scientific(task, row)
    delivery = checker.ref(task["scientific_work_ref"], True)["scientific_work"]["deliverable"]
    gate.require("delivery_plan" in delivery, "archive_plan_missing", "legacy fileheld does not grant archival permission", "hold")
    route = checker.delivery_route(task, row, delivery, admission=True, preserving=True)
    actor_task, actor_row = route["task"], route["record"]
    gate.require(gate.text(actor, "actor") == actor_task["agent_id"], "archive_actor", "actor differs from the permitted archive task")
    gate.require(actor_row.get("status") == "started", "archive_task_not_started", "archive task needs actual start", "hold")
    delivered = gate.timestamp(actor_row.get("delivered_at"), "delivered_at")
    accepted = gate.timestamp(actor_row.get("accepted_at"), "accepted_at")
    started = gate.timestamp(actor_row.get("started_at"), "started_at")
    gate.require(delivered <= accepted <= started, "fact_order", "archive start needs delivered and accepted facts")
    gate.text(actor_row.get("runtime_id"), "runtime_id")
    for field, expected in (("task_id", task["task_id"]), ("author_id", task["agent_id"]), ("version", delivery["version"])):
        if field in source:
            gate.require(source[field] == expected, "message_declaration", field + " differs from the registered contract")
    raw_source = Path(gate.text(source["path"], "source.path"))
    gate.require(raw_source.is_absolute() and checker.bind(raw_source) == raw_source,
                 "message_source_path", "fixed canonical absolute source path required")
    checker.recheck(root_conflict=True)
    return checker, task, delivery, route, raw_source


def filesystem_permission(source, parent):
    gate.require(os.access(str(source), os.R_OK) and os.access(str(parent), os.W_OK | os.X_OK),
                 "filesystem_permission", "current source read and archive directory write access required")


def archive_message(run, task, actor, expected_root_sha, source, apply=False):
    """Archive one owner-observed leaf; declarations do not authenticate its author."""
    parent_fd = None
    temporary = None
    linked = False
    created_identity = None
    target_name = None
    payload = None
    result = None
    try:
        gate.require(type(apply) is bool, "apply_flag", "apply must be boolean")
        checker, task, delivery, route, source_path = prepare(run, task, actor, expected_root_sha, source)
        target = route["target"]
        parent = checker.bind(target.parent)
        gate.require(parent.is_dir(), "file_missing", "archive parent directory must already exist", "hold")
        filesystem_permission(source_path, parent)
        gate.require(stat.S_ISREG(source_path.stat().st_mode), "message_source_type", "regular JSONL source required")
        source_fd = os.open(str(source_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(source_fd, "rb") as stream:
            source_identity = identity(os.fstat(stream.fileno()))
            gate.require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), "message_source_type", "regular JSONL source required")
            raw = observed_line(stream, source["line"])
            gate.require(digest(raw) == source["raw_line_sha256"], "message_line_hash", "raw LF record differs")
            payload = payload_leaf(raw, source, delivery["reader_id"])
            checker.recheck(root_conflict=True)
            parent_fd = os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            parent_identity = identity(os.fstat(parent_fd))
            target_name = target.name

            def recheck():
                filesystem_permission(source_path, parent)
                checker.recheck(root_conflict=True)
                gate.require(identity(os.stat(str(source_path), follow_symlinks=False)) == source_identity
                             and identity(os.fstat(stream.fileno())) == source_identity,
                             "message_source_changed", "source file identity changed")
                gate.require(observed_line(stream, source["line"]) == raw, "message_source_changed", "specified raw LF record changed")
                gate.require(identity(os.stat(str(source_path), follow_symlinks=False)) == source_identity
                             and identity(os.fstat(stream.fileno())) == source_identity,
                             "message_source_changed", "source changed during record recheck")
                gate.require(identity(os.stat(str(parent), follow_symlinks=False)) == parent_identity,
                             "path_changed", "archive directory identity changed")
                gate.require(checker.material_target(task, delivery["delivery_plan"]["target"]) == target,
                             "path_changed", "archive target changed")

            def existing():
                fd = os.open(target_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                with os.fdopen(fd, "rb") as saved:
                    gate.require(stat.S_ISREG(os.fstat(saved.fileno()).st_mode), "output_type", "regular original required")
                    gate.require(saved.read(MAX_PAYLOAD_BYTES + 1) == payload, "archive_conflict", "existing original differs")
                    gate.require(identity(os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False))
                                 == identity(os.fstat(saved.fileno())), "file_changed", "original replaced during read")
                gate.require(checker.read(target) == payload, "archive_conflict", "original changed after read")

            recheck()
            present = os.path.lexists(str(target))
            if present:
                existing()
                recheck()
            elif apply:
                temporary = "消息归档_" + uuid.uuid4().hex + ".tmp"
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd)
                with os.fdopen(fd, "wb") as output:
                    created_identity = identity(os.fstat(output.fileno()))
                    output.write(payload)
                    output.flush()
                    os.fsync(output.fileno())
                recheck()
                try:
                    os.link(temporary, target_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                    linked = True
                except FileExistsError:
                    existing()
                recheck()
                existing()
                os.fsync(parent_fd)
                recheck()
            else:
                recheck()
            if temporary is not None:
                os.unlink(temporary, dir_fd=parent_fd)
                temporary = None
                os.fsync(parent_fd)
                recheck()
            result = {"ok": True, "mode": "apply" if apply else "preview", "applied": linked,
                      "already_present": present or (apply and not linked), "payload_bytes": len(payload),
                      "material_ref": {"path": str(target.relative_to(checker.run)), "sha256": digest(payload)}, "source": dict(source),
                      "author_authenticated": False, "action_permission": False}
            return 0, result
    except (gate.GateError, OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError) as error:
        if linked:
            try:
                info = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
                fd = os.open(target_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
                with os.fdopen(fd, "rb") as saved:
                    unchanged = identity(info) == identity(os.fstat(saved.fileno())) == created_identity and saved.read(MAX_PAYLOAD_BYTES + 1) == payload
                if unchanged and identity(os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)) == created_identity:
                    os.unlink(target_name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                    linked = False
            except OSError:
                pass
        code = error.code if isinstance(error, gate.GateError) else "invalid_data"
        level = error.level if isinstance(error, gate.GateError) else "error"
        result = {"ok": False, "applied": linked, "diagnostics": [{
            "code": code, "detail": error.detail if isinstance(error, gate.GateError) else str(error), "level": level}]}
        return 2 if level == "hold" else 1, result
    finally:
        if temporary is not None and parent_fd is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError as error:
                if result is not None:
                    result["cleanup_warning"] = str(error)
        if parent_fd is not None:
            os.close(parent_fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "apply"), nargs="?", default="preview")
    parser.add_argument("--run", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--expected-root-sha", required=True)
    parser.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    try:
        source = read_locator(args.source)
        code, result = archive_message(args.run, args.task, args.actor, args.expected_root_sha, source, args.mode == "apply")
    except (gate.GateError, OSError, ValueError, TypeError) as error:
        level = error.level if isinstance(error, gate.GateError) else "error"
        code, result = 2 if level == "hold" else 1, {"ok": False, "applied": False, "diagnostics": [{
            "code": error.code if isinstance(error, gate.GateError) else "invalid_data", "detail": str(error), "level": level}]}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return code


if __name__ == "__main__":
    sys.exit(main())
