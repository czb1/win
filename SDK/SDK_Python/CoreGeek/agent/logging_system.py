"""One machine-readable stderr stream; optional legacy files use the same records."""
import base64
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from hashlib import sha256
from itertools import count
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
from uuid import uuid4

PREFIX = "FWLOG "
SCHEMA_VERSION = 2
RUN_ID = uuid4().hex
_sequence = count(1)
_context = ContextVar("game_log_context", default=None)
_payloads = OrderedDict()
TRACE = logging.getLogger("agent.trace")
TRACE.setLevel(logging.INFO)
FIELDS = ("run", "request_id", "source", "round", "day", "phase", "phase_round",
          "team", "side", "session", "category", "event", "unit_id", "task_id", "task_type")


def bind_request(data):
    """Bind only safe scalar hints before Turn validation; do not invent game time."""
    if not isinstance(data, dict):
        return
    team = data.get("teamOur")
    values = {"round": data.get("roundNo") if type(data.get("roundNo")) is int else None}
    if isinstance(team, dict):
        values.update(team=str(team.get("teamId", ""))[:256], side=str(team.get("type", ""))[:64])
    update_context(**values)


@contextmanager
def request_context(data=None, source="callback"):
    if (_context.get() or {}).get("request_id"):
        yield
        return
    token = _context.set(dict(run=RUN_ID, request_id=uuid4().hex, source=source))
    try:
        bind_request(data)
        yield
    finally:
        _context.reset(token)


@contextmanager
def turn_context(turn):
    token = _context.set({**(_context.get() or {}), "round": turn.round, "day": turn.day,
                          "phase": "day" if turn.is_day else "night",
                          "phase_round": turn.tick + 1 if turn.is_day else turn.tick - 69,
                          "team": turn.key[0], "side": turn.team})
    try:
        yield
    finally:
        _context.reset(token)


def update_context(**values):
    current = _context.get()
    if current is not None:
        _context.set({**current, **values})


def context():
    return dict(_context.get() or {})


def logging_failure(error):
    """Best effort, non-recursive fallback: diagnostics cannot fail a turn."""
    try:
        record = {**context(), "run": RUN_ID, "schema_version": SCHEMA_VERSION,
                  "event": "logging_failed", "category": "runtime", "level": "ERROR",
                  "data": {"error": str(error)[:256], "records_may_be_missing": True}}
        sys.stderr.write(PREFIX + json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def emit_event(event, data=None, category="general", level=logging.INFO, **fields):
    try:
        TRACE.log(level, event, extra={"event": event, "category": category,
                                      "data": data if data is not None else {}, **fields})
    except Exception as error:
        logging_failure(error)


def emit_payload(event, value, category="evolution", **fields):
    """Persist full long text once per session; every use retains its reference."""
    if not value:
        return
    value = str(value)
    identity = sha256(value.encode("utf-8")).hexdigest()
    key = (context().get("session"), category, identity)
    scope = {key: fields.pop(key) for key in ("task_id", "task_type", "unit_id") if key in fields}
    data = {"payload_id": identity, "chars": len(value), **fields}
    if key not in _payloads:
        data["content"] = value
    _payloads[key] = True
    _payloads.move_to_end(key)
    while len(_payloads) > 256:
        _payloads.popitem(last=False)
    emit_event(event, data, category, **scope)


class ContextFilter(logging.Filter):
    def filter(self, record):
        current = _context.get() or {}
        for key in FIELDS:
            if not hasattr(record, key):
                setattr(record, key, current.get(key))
        if record.run is None:
            record.run = RUN_ID
        if not hasattr(record, "sequence"):
            record.sequence = next(_sequence)
            record.record_id = uuid4().hex
        message = record.getMessage()
        if record.category is None:
            record.category = ("long_context" if message.startswith("[TREASURE_TRACE]") else
                               "evolution" if re.match(r"(?:round=\S+\s+)?task_\w+", message) else "general")
        if record.category == "long_context" and record.task_id == current.get("task_id"):
            record.task_id = f"{record.session}/long-context" if record.session else None
            record.task_type = "长上下文类"
        if record.event is None:
            match = re.search(r"event=([\w]+)", message) if record.category == "long_context" else re.search(r"\b(task_\w+)", message)
            record.event = match.group(1) if match else "diagnostic"
        if not hasattr(record, "data"):
            # Promote existing JSON message bodies without changing old messages.
            match = re.search(r"\b\w+=(\{.*|\[.*)$", message, re.DOTALL)
            try:
                record.data = json.loads(match.group(1)) if match else {}
            except ValueError:
                record.data = {}
        record.game_time = (f"第{record.day}天{'白天' if record.phase == 'day' else '黑夜'}"
                            f"第{record.phase_round}回合" if record.day is not None else "系统")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {key: getattr(record, key, None) for key in FIELDS}
        result.update(schema_version=SCHEMA_VERSION, sequence=record.sequence, record_id=record.record_id,
                      timestamp=datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                      game_time=record.game_time, level=record.levelname, logger=record.name,
                      message=record.getMessage(), data=record.data)
        if record.exc_info:
            result["exception"] = self.formatException(record.exc_info)
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


class WireFormatter(JsonFormatter):
    """Physical lines stay below 8KB for normal protocol IDs; chunks are checked."""
    def format(self, record):
        raw = super().format(record)
        encoded = raw.encode("utf-8")
        if len(encoded) <= 7000:
            return PREFIX + raw
        pieces = [encoded[i:i + 4200] for i in range(0, len(encoded), 4200)]
        digest = sha256(encoded).hexdigest()
        header = {key: getattr(record, key, None) for key in FIELDS}
        header.update(schema_version=SCHEMA_VERSION, sequence=record.sequence, record_id=record.record_id)
        return "\n".join(PREFIX + json.dumps({**header, "fragment": {
            "index": index, "total": len(pieces), "sha256": digest, "encoding": "base64",
            "content": base64.b64encode(piece).decode("ascii")}}, ensure_ascii=False, separators=(",", ":"))
            for index, piece in enumerate(pieces))


class SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record):
        logging_failure(sys.exc_info()[1])


class SafeRotatingHandler(RotatingFileHandler):
    def handleError(self, record):
        logging_failure(sys.exc_info()[1])


def configure_logging(level="INFO", log_dir=None):
    """FWLOG JSON on stderr; core snapshots stay enabled at WARNING/ERROR."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "game_log_handler", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(getattr(logging, level))
    handlers = [(SafeStreamHandler(sys.stderr), WireFormatter())]
    if log_dir:
        try:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            for filename, formatter in (("game.log", WireFormatter()), ("events.jsonl", JsonFormatter())):
                handlers.append((SafeRotatingHandler(directory / filename, maxBytes=50 * 1024 * 1024,
                                                       backupCount=5, encoding="utf-8"), formatter))
        except OSError as error:
            logging_failure(error)
    for handler, formatter in handlers:
        handler.game_log_handler = True
        handler.addFilter(ContextFilter())
        handler.setFormatter(formatter)
        root.addHandler(handler)
