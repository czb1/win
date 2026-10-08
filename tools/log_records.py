"""Read FWLOG txt or legacy JSONL, restoring verified chunks in bounded memory."""
import base64
from collections import OrderedDict
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
