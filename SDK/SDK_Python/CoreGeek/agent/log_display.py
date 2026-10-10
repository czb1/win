"""Compact records for every log sink and export.

This module is embedded in the standalone extractor by sync_log_crypto.py.
"""

TASK_CATEGORIES = {"evolution", "long_context", "reasoning"}
TASK_ACTIONS = {"acceptTask", "submitAnswer", "summonTreasure"}
TASK_PAYLOAD_EVENTS = {"sent_prompt", "sent_executeCmd", "received_llmResp", "received_lastCmdResult"}
HIDDEN_FIELDS = {"run", "request_id", "level", "logger", "timestamp", "source", "session",
                 "record_id", "sequence", "schema_version", "game_time"}


def is_task(record):
    return record.get("category") in TASK_CATEGORIES or str(record.get("event", "")).startswith("task_")


def task_category(record):
    category = record.get("category")
    if category in TASK_CATEGORIES:
        return category
    if str(record.get("event", "")).startswith("task_"):
        return "evolution"
    return None


def task_related(record):
    if task_category(record) or record.get("event") in TASK_PAYLOAD_EVENTS:
        return True
    data = record.get("data")
    if not isinstance(data, dict):
        return False
    if record.get("event") == "unit_decision" and data.get("role_type") == "pioneer":
        return True
    commands = data.get("commands", data.get("roleCommandMap", {}))
    if isinstance(commands, dict) and any(isinstance(command, dict) and command.get("action") in TASK_ACTIONS
                                          for command in commands.values()):
        return True
    action = data.get("command")
    if isinstance(action, dict) and action.get("action") in TASK_ACTIONS:
        return True
    if record.get("event") == "turn_response" and (data.get("prompt_chars") or data.get("execute_chars")):
        return True
    if record.get("event") == "previous_feedback":
        if record.get("task_id") or data.get("lastSummonTreasureResult") not in (None, "", 0):
            return True
        actions = data.get("actions")
        if isinstance(actions, list) and any(isinstance(item, dict) and isinstance(item.get("command"), dict)
                                            and item["command"].get("action") in TASK_ACTIONS for item in actions):
            return True
    return False


def compact_record(record, keep_session_data=False):
    """Keep useful fields without altering the source or application payloads."""
    result = {key: value for key, value in record.items()
              if key not in HIDDEN_FIELDS and value is not None and value != ""}
    if not task_related(record):
        result.pop("task_id", None)
        result.pop("task_type", None)
    for key in ("task_id", "task_type"):
        if isinstance(result.get(key), str) and not result[key].strip():
            result.pop(key)
    if result.get("message") == result.get("event"):
        result.pop("message", None)
    data = result.get("data")
    if isinstance(data, dict) and record.get("event") == "session_started" and not keep_session_data:
        # Writers retain configuration once per scene for the extractor's meta.json.
        data = {key: data[key] for key in ("version", "reason") if data.get(key) not in (None, "")}
        result["data"] = data
    if isinstance(data, dict) and record.get("event") == "previous_feedback":
        data = {key: value for key, value in data.items() if key != "previous_request_id"}
        result["data"] = data
    if isinstance(data, dict) and record.get("event") in ("unit_decision", "task_state"):
        data = {key: value for key, value in data.items() if value is not None and value not in ({}, [])}
        result["data"] = data
    if data in ({}, []):
        result.pop("data", None)
    return result
