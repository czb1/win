#!/usr/bin/env python3
"""Stream/filter events.jsonl; --list provides a day/phase/task directory."""
import argparse
from collections import Counter
import json
import sys
from log_records import events, ReadReport, LogDecryptor, LogCryptoError, load_log_key
from extract_logs import compact_record


def readable(record):
    view = compact_record(record)
    parts = []
    if view.get("game_time"):
        parts.append(view["game_time"])
    elif view.get("day") is not None:
        phase = {"day": "白天", "night": "黑夜"}.get(view.get("phase"), "")
        parts.append(f"第{view['day']}天{phase}")
    if view.get("round") is not None:
        parts.append(f"round={view['round']}")
    if view.get("category"):
        parts.append(f"[{view['category']}]")
    for key in ("team", "unit_id", "task_id", "task_type"):
        if key in view:
            parts.append(f"{key}={view[key]}")
    for key in ("event", "message"):
        if key in view:
            parts.append(str(view[key]))
    if "data" in view:
        parts.append(json.dumps(view["data"], ensure_ascii=False, separators=(",", ":")))
    return " ".join(parts)


def matches(record, args):
    for key in ("day", "phase", "category", "task_id", "session", "team", "level", "event"):
        value = getattr(args, key)
        if value is not None and record.get(key) != value:
            return False
    if args.unit_id is not None and str(record.get("unit_id")) != args.unit_id:
        return False
    number = record.get("round")
    if args.from_round is not None and (type(number) is not int or number < args.from_round):
        return False
    if args.to_round is not None and (type(number) is not int or number > args.to_round):
        return False
    return args.contains is None or args.contains in record.get("message", "")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", help="Path to events.jsonl")
    parser.add_argument("--private-key", action="append", default=[], help="Local FWLOG private key JSON; repeat for rotated keys")
    parser.add_argument("--day", type=int)
    parser.add_argument("--phase", choices=("day", "night"))
    parser.add_argument("--category")
    parser.add_argument("--event")
    parser.add_argument("--unit-id")
    parser.add_argument("--from-round", type=int)
    parser.add_argument("--to-round", type=int)
    for key in ("task-id", "session", "team", "contains"):
        parser.add_argument("--" + key)
    parser.add_argument("--level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"))
    parser.add_argument("--list", action="store_true", help="List matching day/phase/task groups and counts")
    parser.add_argument("--json", action="store_true", help="Output JSON Lines for further processing")
    parser.add_argument("--limit", type=int, default=0, help="Maximum displayed events; 0 means all")
    args = parser.parse_args(argv)
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    groups = Counter()
    count = 0
    decryptor = LogDecryptor(load_log_key(path, private=True) for path in args.private_key)
    report = ReadReport()
    for record in events(args.log, report, decryptor=decryptor):
        if not matches(record, args):
            continue
        if args.list:
            groups[tuple(record.get(k) for k in ("day", "phase", "team", "session", "category", "task_id"))] += 1
            continue
        if args.json:
            print(json.dumps(compact_record(record), ensure_ascii=False, separators=(",", ":")))
        else:
            print(readable(record))
            if record.get("exception"):
                print(record["exception"])
        count += 1
        if args.limit and count >= args.limit:
            break
    if args.list:
        print("天数\t昼夜\t队伍\t场次\t类别\t任务编号\t日志数")
        for key, total in sorted(groups.items(), key=lambda item: (item[0][0] or 0, item[0][1] == "night", *(str(v or "") for v in item[0][2:]))):
            print("\t".join(str(v if v is not None else "-") for v in (*key, total)))
    return 2 if report.crypto_errors else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (LogCryptoError, OSError) as error:
        print(f"无法处理日志：{error}", file=sys.stderr)
        sys.exit(2)

