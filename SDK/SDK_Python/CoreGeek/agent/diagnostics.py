"""Observation-only traces. Never run pathfinding or modify strategy state here."""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import asdict
from functools import lru_cache, wraps
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import re

from .logging_system import context, emit_event, emit_payload, logging_failure
from .model import CHARACTERS, WEAPONS


def observational(function):
    @wraps(function)
    def safe(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as error:
            logging_failure(error)
    return safe


@lru_cache(maxsize=1)
def build_info():
    digest = sha256()
    try:
        for path in sorted(Path(__file__).parent.glob("*.py")):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        source = digest.hexdigest()
    except OSError:
        source = None
    return {"version": os.environ.get("FUTURE_WAR_VERSION"), "source_sha256": source,
            "python": platform.python_version()}


def start_session(mem, cfg, reason):
    mem.log_history = OrderedDict()
    mem.log_enemy_seen = OrderedDict()
    configuration = asdict(cfg)
    emit_event("session_started", {**build_info(), "config": configuration,
               "config_sha256": sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest(),
               "reason": reason, "time_basis": "UTC", "coordinate_origin": "bottom_left",
               "station_anchor": "top_left", "station_size": [2, 2]}, "runtime")


@observational
def observe(turn, mem):
    raw = turn.raw
    seen = getattr(mem, "log_enemy_seen", OrderedDict())
    current_ids = set()
    for role in raw.get("teamEnemy", {}).get("roles", []):
        uid = str(role["id"])
        current_ids.add(uid)
        seen[uid] = {"role": deepcopy(role), "last_seen_round": turn.round}
        seen.move_to_end(uid)
    while len(seen) > 512:
        seen.popitem(last=False)
    emit_event("turn_snapshot", {
        "mapInfo": raw["mapInfo"], "teamOur": raw["teamOur"],
        "teamEnemy": raw.get("teamEnemy"), "robot": raw.get("robot"),
        "vendorShopList": raw.get("vendorShopList"), "weaponShopList": raw.get("weaponShopList"),
        "enemy_last_seen": [value for uid, value in seen.items() if uid not in current_ids],
        "enemy_visibility": "received_only; last_seen_is_not_current_position",
        "robot_ownership": "known_ours_from_summonRobotList; other_ownership_unknown",
        "received_fields": sorted(raw)}, "snapshot")
    previous = getattr(mem, "log_history", {}).get(turn.round - 1)
    results = raw.get("lastRoundRoleActionResults")
    current = {str(role["id"]): role for role in raw["teamOur"].get("roles", [])}
    changes = []
    if previous:
        before = previous["roles"]
        for uid in sorted(before.keys() | current.keys()):
            old, new = before.get(uid), current.get(uid)
            fields = ("pos", "health", "backpack", "level", "cooldown", "isDriving")
            diff = {key: {"before": old.get(key), "after": new.get(key)}
                    for key in fields if old is not None and new is not None and old.get(key) != new.get(key)}
            if old is None or new is None:
                diff["presence"] = {"before": old is not None, "after": new is not None,
                                    "meaning": "observation_change; not_proof_of_death"}
            if diff:
                changes.append({"unit_id": uid, "fields": diff})
    commands = previous["commands"] if previous else {}
    evaluations = []
    for uid, command in commands.items():
        legality = results.get(uid, results.get(int(uid))) if isinstance(results, dict) else None
        evaluation = {"unit_id": uid, "command": command, "legality": legality,
                      "effect": "unknown"}
        role = current.get(uid)
        if command.get("action") == "move" and role:
            target = (command.get("targetPos") or [None])[0]
            evaluation["effect"] = "target_observed" if role.get("pos") == target else "target_not_observed"
            evaluation["observed_pos"] = role.get("pos")
            evaluation["cause"] = "unknown; legality_is_not_execution_success"
        evaluations.append(evaluation)
    emit_event("previous_feedback", {
        "feedback_for_round": turn.round - 1, "correlated": previous is not None,
        "previous_request_id": previous["request_id"] if previous else None,
        "lastRoundRoleActionResults": results, "lastSummonTreasureResult": raw.get("lastSummonTreasureResult"),
        "errors": raw.get("errors"), "actions": evaluations, "observed_changes": changes,
        "gold": {"before": previous["gold"] if previous else None, "after": raw["teamOur"].get("goldNum")},
        "score": {"before": previous["score"] if previous else None, "after": raw["teamOur"].get("totalScore")},
        "note": None if previous else "previous turn unavailable; do not associate older commands"}, "feedback")
    if raw.get("errors"):
        emit_event("judger_errors", {"errors": raw["errors"]}, "protocol")


@observational
def incoming_payloads(turn, mem):
    pending = mem.pending
    purpose = pending[0] if pending else None
    category = "evolution" if purpose in ("task", "cmd") else "long_context" if purpose == "news" else "recovery"
    for key in ("llmResp", "lastCmdResult"):
        value = turn.raw.get(key)
        if value:
            details = {}
            if key == "lastCmdResult":
                lines = str(value).splitlines()
                duration = re.fullmatch(r"\[durationMs:(\d+)\]", lines[1]) if len(lines) > 1 else None
                details = {"status_header": lines[0] if lines else None,
                           "duration_ms": int(duration.group(1)) if duration else None,
                           "truncated_by_judger": bool(lines and lines[-1] == "[TRUNCATED]")}
            emit_payload("received_" + key, value, category, purpose=purpose,
                         issued_round=pending[1] if pending else None,
                         correlated=bool(pending and turn.round == pending[1] + 1), **details)


@observational
def task_state(turn, mem):
    if turn.phase_task:
        emit_payload("task_description", turn.phase_task)
    news = turn.raw.get("worldNews") or {}
    for key, category in (("officialNews", "reasoning"), ("folkLegends", "long_context")):
        emit_payload("received_" + key, news.get(key), category,
                     task_id=f"{mem.log_session}/{'long-context' if category == 'long_context' else category}",
                     task_type="推理类" if category == "reasoning" else "长上下文类")
    if turn.phase_task or mem.log_task_id:
        emit_event("task_state", {"active": bool(turn.phase_task), "point": mem.task_point,
                   "started_round": mem.task_started, "timeout": mem.task_timeout,
                   "pending": mem.pending, "calls": mem.calls, "stop_reason": mem.stop_reason,
                   "answer_ready": mem.answer is not None}, "evolution")


@observational
def finish(turn, mem, ledger, response, elapsed_ms, budget_reached=False, cached=False, navigation=None):
    if ledger is not None:
        controllers = {str(cmd.get("controllerId")): uid for uid, cmd in response["roleCommandMap"].items()
                       if cmd.get("action") == "attack" and cmd.get("controllerId") is not None}
        roles = list(turn.raw["teamOur"].get("roles", []))
        roles += list(turn.raw["teamOur"].get("summonRobotList", []))
        for role in roles:
            uid, kind = str(role["id"]), role["roleType"]
            action = response["roleCommandMap"].get(uid)
            if kind in ("station", "wall") and not action:
                continue
            note = ledger.notes.get(int(uid), {})
            if action:
                reason = note.get("reason", "selected_by_strategy")
            elif uid in controllers:
                reason = "operating_weapon"
            elif int(role.get("health") or 0) <= 0:
                reason = "not_alive"
            elif kind not in CHARACTERS and kind not in WEAPONS:
                reason = "not_supported_by_current_strategy"
            elif ledger.daytime_waits.get(int(uid)):
                reason = ledger.daytime_waits[int(uid)]
            elif kind in WEAPONS:
                reason = "cooldown" if role.get("cooldown", 0) else "no_selected_attack"
            elif int(uid) in ledger.used:
                reason = note.get("reason", "strategy_hold_without_command")
            else:
                reason = "no_eligible_action"
            emit_event("unit_decision", {
                "role_type": kind, "pos": role.get("pos"), "command": action,
                "reason": reason, "selected_at": ledger.origins.get(int(uid)),
                "conditions": note.get("conditions", {}), "weapon_operated": controllers.get(uid),
                "planning": ledger.plans.get(int(uid), {}),
                "navigation": navigation.diagnostics.get(int(uid), {}) if navigation else {},
                "day_left": turn.day_left, "free_space": max(0, int(role.get("backPackCapability") or 0) - len(role.get("backpack") or [])),
                "mine_target": mem.mine_targets.get(int(uid)), "build_target": mem.build_targets.get(int(uid)),
                "sale_target": mem.sale_targets.get(int(uid)), "upgrade_target": mem.upgrade_targets.get(int(uid)),
                "return_tower": mem.return_targets.get(int(uid)), "return_post": mem.return_posts.get(int(uid)),
                "rejections": ledger.rejections.get(int(uid), {}),
                "rejected_samples": ledger.rejected_samples.get(int(uid), []),
                "budget_reached": budget_reached}, "decision", unit_id=role["id"])
    pending = mem.pending
    category = "evolution" if pending and pending[0] in ("task", "cmd") or turn.phase_task else "long_context" if pending and pending[0] == "news" else "recovery"
    emit_payload("sent_prompt", response.get("prompt"), category, issued_round=turn.round)
    emit_payload("sent_executeCmd", response.get("executeCmd"), "evolution", issued_round=turn.round)
    task_actions = {uid: cmd for uid, cmd in response["roleCommandMap"].items()
                    if cmd.get("action") in ("acceptTask", "submitAnswer", "summonTreasure")}
    if task_actions:
        emit_event("task_actions", {"commands": task_actions, "success": "not_yet_known"},
                   "long_context" if any(c["action"] == "summonTreasure" for c in task_actions.values()) else "evolution")
    emit_event("turn_response", {"roleCommandMap": response["roleCommandMap"], "latency_ms": elapsed_ms,
               "cached": cached, "budget_reached": budget_reached,
               "prompt_chars": len(response.get("prompt", "")), "execute_chars": len(response.get("executeCmd", ""))}, "response")
    history = mem.log_history
    history[turn.round] = {"round": turn.round, "request_id": context().get("request_id"),
                          "roles": {str(r["id"]): deepcopy(r) for r in turn.raw["teamOur"].get("roles", [])},
                          "commands": deepcopy(response["roleCommandMap"]),
                          "gold": turn.raw["teamOur"].get("goldNum"), "score": turn.raw["teamOur"].get("totalScore")}
    history.move_to_end(turn.round)
    while len(history) > 3:
        history.popitem(last=False)
