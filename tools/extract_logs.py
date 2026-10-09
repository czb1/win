#!/usr/bin/env python3
"""Extract a complete issue window and tasks.txt from one downloaded match .log file.

Standalone script: Python 3.11+ standard library only; no other scripts needed.
"""
import argparse
import base64
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import re
import sys

PREFIX = "FWLOG "
TASK_CATEGORIES = {"evolution", "long_context", "reasoning"}


@dataclass
class ReadReport:
    records: int = 0
    ignored_lines: int = 0
    invalid_lines: int = 0
    duplicate_records: int = 0
    incomplete_records: int = 0
    problems: list = field(default_factory=list)
    sequences: dict = field(default_factory=dict)

    def problem(self, line, reason):
        if len(self.problems) < 100:
            self.problems.append({"line": line, "reason": reason})

    def summary(self):
        gaps = []
        for run, numbers in self.sequences.items():
            ordered = sorted(numbers)
            gaps.extend({"run": run, "from": a + 1, "to": b - 1}
                        for a, b in zip(ordered, ordered[1:]) if b > a + 1)
        return {"records": self.records, "ignored_lines": self.ignored_lines,
                "invalid_lines": self.invalid_lines, "duplicate_records": self.duplicate_records,
                "incomplete_records": self.incomplete_records, "sequence_gaps": gaps[:100],
                "problems": self.problems,
                "note": "Gaps describe this input; platform truncation and manually cut excerpts may cause them."}


def events(path, report=None, warn=True):
    report = report if report is not None else ReadReport()
    pending, emitted = OrderedDict(), OrderedDict()
    pending_bytes = 0
    decoder = json.JSONDecoder()
    with Path(path).open("rb") as source:
        prefix = source.read(4)
    encoding = "utf-16" if prefix.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    with Path(path).open(encoding=encoding, errors="replace") as source:
        for line_no, line in enumerate(source, 1):
            line = re.sub(r"\x1b\[[0-9;]*[mK]", "", line).strip()
            if not line:
                continue
            if line.startswith("{"):
                raw = line
            elif PREFIX in line:
                raw = line.split(PREFIX, 1)[1]
            else:
                report.ignored_lines += 1
                if Path(path).suffix == ".jsonl":
                    report.invalid_lines += 1
                    report.problem(line_no, "invalid JSONL line")
                    if warn and report.invalid_lines <= 5:
                        print(f"跳过无效日志：{path}:{line_no}", file=sys.stderr)
                continue
            try:
                record, end = decoder.raw_decode(raw)
                if raw[end:].strip() or not isinstance(record, dict):
                    raise ValueError("expected one JSON object")
                fragment = record.get("fragment")
                if fragment is not None:
                    if not isinstance(fragment, dict):
                        raise ValueError("invalid fragment")
                    index, total = fragment["index"], fragment["total"]
                    if type(index) is not int or type(total) is not int or not 0 <= index < total <= 4096:
                        raise ValueError("invalid fragment count")
                    if fragment.get("encoding") != "base64":
                        raise ValueError("unsupported fragment encoding")
                    identity = (record.get("run"), record["record_id"])
                    if not isinstance(identity[1], str):
                        raise ValueError("missing fragment identity")
                    part = base64.b64decode(fragment["content"], validate=True)
                    state = pending.setdefault(identity, {"parts": {}, "total": total,
                                               "sha256": fragment["sha256"], "line": line_no})
                    if state["total"] != total or state["sha256"] != fragment["sha256"]:
                        raise ValueError("fragment metadata mismatch")
                    if index in state["parts"] and state["parts"][index] != part:
                        raise ValueError("conflicting duplicate fragment")
                    if index not in state["parts"]:
                        state["parts"][index] = part
                        pending_bytes += len(part)
                    while len(pending) > 128 or pending_bytes > 32 * 1024 * 1024:
                        _, removed = pending.popitem(last=False)
                        pending_bytes -= sum(map(len, removed["parts"].values()))
                        report.incomplete_records += 1
                        report.problem(removed["line"], "fragment buffer limit; record unavailable")
                    if identity not in pending or len(state["parts"]) != total:
                        continue
                    encoded = b"".join(state["parts"][i] for i in range(total))
                    pending_bytes -= len(encoded)
                    del pending[identity]
                    if sha256(encoded).hexdigest() != state["sha256"]:
                        raise ValueError("fragment checksum mismatch")
                    record = json.loads(encoded.decode("utf-8"))
                    if not isinstance(record, dict) or (record.get("run"), record.get("record_id")) != identity:
                        raise ValueError("restored record identity mismatch")
                for key in ("run", "request_id", "session", "team", "record_id", "task_id", "category", "event"):
                    if record.get(key) is not None and type(record[key]) not in (str, int):
                        raise ValueError("invalid scalar identity: " + key)
            except (ValueError, KeyError, TypeError, UnicodeError) as error:
                report.invalid_lines += 1
                report.problem(line_no, str(error))
                if warn and report.invalid_lines <= 5:
                    print(f"跳过无效日志：{path}:{line_no} ({error})", file=sys.stderr)
                continue
            identity = (record.get("run"), record.get("record_id"))
            if identity[1] is not None:
                if identity in emitted:
                    report.duplicate_records += 1
                    continue
                emitted[identity] = True
                if len(emitted) > 4096:
                    emitted.popitem(last=False)
            sequence = record.get("sequence")
            if type(sequence) is int:
                report.sequences.setdefault(record.get("run"), set()).add(sequence)
            report.records += 1
            yield record
    for state in pending.values():
        report.incomplete_records += 1
        report.problem(state["line"], "missing fragments; record unavailable")
    if warn and report.incomplete_records:
        print(f"日志缺少分段：{report.incomplete_records} 条记录无法恢复", file=sys.stderr)


def scope(record):
    return record.get("run"), record.get("session"), record.get("team")


def payload_key(record):
    data = record.get("data")
    if isinstance(data, dict) and isinstance(data.get("payload_id"), str):
        return (*scope(record), record.get("category"), data["payload_id"])
    return None


def is_task(record):
    return record.get("category") in TASK_CATEGORIES or str(record.get("event", "")).startswith("task_")


def anchors(record, args):
    number = record.get("round")
    if type(number) is not int:
        return False
    for name in ("session", "team", "run", "day", "phase", "task_id"):
        expected = getattr(args, name)
        if expected is not None and record.get(name) != expected:
            return False
    if args.from_round is not None and number < args.from_round:
        return False
    if args.to_round is not None and number > args.to_round:
        return False
    if args.unit_id is not None and str(record.get("unit_id")) != args.unit_id:
        return False
    return True


def dumps(record):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="Downloaded .log file or legacy events.jsonl")
    parser.add_argument("--out", type=Path, default=Path("issue"))
    parser.add_argument("--from-round", type=int)
    parser.add_argument("--to-round", type=int)
    parser.add_argument("--context", type=int, default=5, help="Neighboring game rounds")
    parser.add_argument("--day", type=int)
    parser.add_argument("--phase", choices=("day", "night"))
    for name in ("session", "team", "run", "task-id", "unit-id"):
        parser.add_argument("--" + name)
    parser.add_argument("--split", action="store_true", help="Also write snapshots, decisions, feedback and errors JSONL")
    parser.add_argument("--list", action="store_true", help="List sessions and tasks without writing files")
    args = parser.parse_args(argv)
    if args.context < 0 or (args.from_round is not None and args.to_round is not None and args.from_round > args.to_round):
        parser.error("invalid round range or negative context")
    report = ReadReport()
    bounds, definitions, metadata, groups = {}, {}, {}, Counter()
    for record in events(args.log, report):
        identity = scope(record)
        number = record.get("round")
        groups[(identity, record.get("task_id"))] += 1
        if record.get("event") == "session_started":
            metadata[identity] = record
        key = payload_key(record)
        if key and "content" in record.get("data", {}):
            definitions.setdefault(key, record.get("record_id"))
        if anchors(record, args):
            lower, upper = bounds.get(identity, (number, number))
            bounds[identity] = min(lower, number), max(upper, number)
    if args.list:
        print("运行\t场次\t队伍\t任务编号\t记录数")
        for (identity, task), total in sorted(groups.items(), key=lambda row: str(row[0])):
            print("\t".join(str(value or "-") for value in (*identity, task, total)))
        return 0
    if not bounds:
        print("没有匹配的回合；使用 --list 检查场次与任务编号。", file=sys.stderr)
        return 1
    outputs = {"issue.txt", "tasks.txt", "meta.json", ".records.tmp", "turns.jsonl", "decisions.jsonl", "feedback.jsonl", "errors.jsonl"}
    if args.log.resolve() in {(args.out / name).resolve() for name in outputs}:
        parser.error("output would overwrite input log")
    args.out.mkdir(parents=True, exist_ok=True)
    requested, available, task_requests = set(), set(), set()
    selected_count = 0
    unfinished = set()
    temporary = args.out / ".records.tmp"
    try:
        with temporary.open("w", encoding="utf-8") as target:
            for record in events(args.log, warn=False):
                identity, number = scope(record), record.get("round")
                interval = bounds.get(identity)
                if interval is None or type(number) is not int or not interval[0] - args.context <= number <= interval[1] + args.context:
                    continue
                if record.get("event") == "session_started":
                    continue
                target.write(dumps(record) + "\n")
                selected_count += 1
                key = payload_key(record)
                if key:
                    requested.add(key)
                    if "content" in record.get("data", {}):
                        available.add(key)
                request = (*identity, record.get("request_id"))
                if record.get("event") == "turn_started":
                    unfinished.add(request)
                elif record.get("event") == "turn_response":
                    unfinished.discard(request)
                if is_task(record) and (args.task_id is None or record.get("task_id") == args.task_id):
                    task_requests.add(request)
        missing = requested - available
        absent = [list(key) for key in missing if key not in definitions]
        meta = {"source": str(args.log), "filters": {name: getattr(args, name) for name in
                ("from_round", "to_round", "context", "team", "session", "run", "day", "phase", "task_id", "unit_id")},
                "windows": [{"run": key[0], "session": key[1], "team": key[2],
                             "from_round": value[0] - args.context, "to_round": value[1] + args.context}
                            for key, value in bounds.items()],
                "selected_records": selected_count, "read_report": report.summary(),
                "missing_payloads": absent, "requests_without_response": [list(key) for key in unfinished],
                "missing_session_metadata": [list(key) for key in bounds if key not in metadata]}
        files = {"issue": (args.out / "issue.txt").open("w", encoding="utf-8"),
                 "tasks": (args.out / "tasks.txt").open("w", encoding="utf-8")}
        if args.split:
            files.update({name: (args.out / (name + ".jsonl")).open("w", encoding="utf-8")
                          for name in ("turns", "decisions", "feedback", "errors")})
        counts = Counter()
        def write(record):
            encoded = dumps(record)
            files["issue"].write("FWLOG " + encoded + "\n")
            request = (*scope(record), record.get("request_id"))
            task = is_task(record) and (args.task_id is None or record.get("task_id") == args.task_id)
            task = task or record.get("event") in ("session_started", "log_integrity")
            task = task or record.get("included_as") == "payload_dependency" and is_task(record)
            task = task or request in task_requests and (record.get("event") in ("previous_feedback", "turn_response")
                     or record.get("event") == "unit_decision" and isinstance(record.get("data"), dict) and record["data"].get("role_type") == "pioneer"
                     or record.get("level") in ("WARNING", "ERROR", "CRITICAL"))
            if task:
                files["tasks"].write("FWLOG " + encoded + "\n")
                counts["task_records"] += 1
            for name, category in (("turns", "snapshot"), ("decisions", "decision"), ("feedback", "feedback")):
                if name in files and record.get("category") == category:
                    files[name].write(encoded + "\n")
            if "errors" in files and (record.get("level") in ("WARNING", "ERROR", "CRITICAL")
                                       or record.get("event") in ("judger_errors", "log_integrity")):
                files["errors"].write(encoded + "\n")
        try:
            write({"schema_version": 2, "event": "log_integrity", "category": "runtime",
                   "level": "WARNING" if report.invalid_lines or report.incomplete_records or absent or unfinished else "INFO",
                   "data": meta})
            for identity in bounds:
                if identity in metadata:
                    write({**metadata[identity], "included_as": "session_metadata"})
            supplied = set()
            for record in events(args.log, warn=False):
                key = payload_key(record)
                if key in missing and key not in supplied and "content" in record.get("data", {}):
                    write({**record, "included_as": "payload_dependency"})
                    supplied.add(key)
            with temporary.open(encoding="utf-8") as selected:
                for line in selected:
                    write(json.loads(line))
        finally:
            for target in files.values():
                target.close()
        meta.update(counts)
        (args.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        temporary.unlink(missing_ok=True)
    print(f"已导出 {selected_count} 条区间记录、{counts['task_records']} 条任务相关记录：{args.out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as error:
        print(f"无法处理日志：{error}", file=sys.stderr)
        sys.exit(1)
