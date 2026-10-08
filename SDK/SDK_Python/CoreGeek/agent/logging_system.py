"""Turn-scoped diagnostics shared by HTTP, callback and offline replay."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
import sys

_context = ContextVar("game_log_context", default=None)


@contextmanager
def turn_context(turn):
    token = _context.set(dict(round=turn.round, day=turn.day,
                              phase="day" if turn.is_day else "night",
                              phase_round=turn.tick + 1 if turn.is_day else turn.tick - 69,
                              team=turn.key[0], side=turn.team))
    try:
        yield
    finally:
        _context.reset(token)


def update_context(**values):
    current = _context.get()
    if current is not None:
        _context.set({**current, **values})


class ContextFilter(logging.Filter):
    def filter(self, record):
        context = _context.get() or {}
        for key in ("round", "day", "phase", "phase_round", "team", "side", "session", "task_id", "task_type"):
            if not hasattr(record, key):
                setattr(record, key, context.get(key))
        message = record.getMessage()
        if not hasattr(record, "category"):
            record.category = ("long_context" if message.startswith("[TREASURE_TRACE]") else
                               "evolution" if re.match(r"(?:round=\S+\s+)?task_\w+", message) else "general")
        if record.category == "long_context":
            record.task_id = f"{record.session}/long-context" if record.session else None
            record.task_type = "长上下文类"
        if not hasattr(record, "event"):
            match = re.search(r"event=([\w]+)", message) if record.category == "long_context" else re.search(r"\b(task_\w+)", message)
            record.event = match.group(1) if match else "diagnostic"
        record.game_time = (f"第{record.day}天{'白天' if record.phase == 'day' else '黑夜'}"
                            f"第{record.phase_round}回合" if record.day is not None else "系统")
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        result = {key: getattr(record, key, None) for key in
                  ("round", "day", "phase", "phase_round", "team", "side", "session", "category", "event", "task_id", "task_type")}
        result.update(timestamp=datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                      level=record.levelname, logger=record.name, message=record.getMessage())
        if record.exc_info:
            result["exception"] = self.formatException(record.exc_info)
        return json.dumps(result, ensure_ascii=False)


def configure_logging(level="INFO", log_dir=None):
    """Keep stderr readable; optionally persist text and queryable JSON Lines."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "game_log_handler", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(getattr(logging, level))
    text_format = logging.Formatter(
        "%(asctime)s %(levelname)s [%(game_time)s] team=%(team)s session=%(session)s "
        "category=%(category)s task_id=%(task_id)s %(name)s %(message)s")
    handlers = [(logging.StreamHandler(sys.stderr), text_format)]
    if log_dir:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handlers += [(logging.FileHandler(directory / "game.log", encoding="utf-8"), text_format),
                     (logging.FileHandler(directory / "events.jsonl", encoding="utf-8"), JsonFormatter())]
    for handler, formatter in handlers:
        handler.game_log_handler = True
        handler.addFilter(ContextFilter())
        handler.setFormatter(formatter)
        root.addHandler(handler)
