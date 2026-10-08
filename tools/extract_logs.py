#!/usr/bin/env python3
"""Extract a complete issue window and tasks.txt from one downloaded match txt."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from log_records import events, is_task, payload_key, ReadReport, scope


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
    parser.add_argument("log", type=Path, help="Downloaded txt or legacy events.jsonl")
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
