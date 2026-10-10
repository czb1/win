"""One per-turn ledger owns role locks, gold and destination reservations."""
from collections import Counter
import sys
from .model import WEAPONS, CHARACTERS, ORES, SUMMON_ORDERS, DEFENCE_RETURN_TICK, dump, distance, pos
from .logging_system import logging_failure

EMPTY = {"roleCommandMap": {}, "prompt": "", "executeCmd": ""}


def command(action, target=None, **kwargs):
    result = {"action": action, **kwargs}
    if target is not None:
        result["targetPos"] = [dump(target)]
    return result


class Ledger:
    def __init__(self, turn, cfg, tower_cells, wall_cells):
        self.turn, self.cfg = turn, cfg
        self.gold = turn.gold
        self.commands, self.used, self.reserved = {}, set(), set()
        self.tower_cells, self.wall_cells = set(tower_cells), set(wall_cells)
        self.new_towers = 0
        # Claims are work destinations, not occupied movement cells.
        self.build_claims = {}
        self.purchases = set()
        self.summon_pending_positions = set()
        self.summon_daily_count = 0
        # Only previously observed, stationary enemy weapons may be added by
        # robot assault memory. They remain estimates, not current visibility.
        self.robot_known_enemies = ()
        self.upgrade_claims = set()
        self.repair_claims = set()
        self.mine_claims = {}
        self.return_pairs = []
        self.operator_posts = {}
        self.weapon_diagnostics = {}
        self.operator_post_reports = {}
        # Daytime ownership is separate from issuing a command. Failed recall
        # and intentional watch standby must have different fallback policies.
        self.daytime_waits = {}
        self.watch_pack_slots = {}
        self.work_jobs = {}
        self.supply_targets = {}
        self.supply_reports = {}
        self.spending_plan = {}
        self.notes, self.plans, self.origins, self.rejections, self.rejected_samples = {}, {}, {}, {}, {}

    def explain(self, uid, reason, **conditions):
        self.notes[uid] = {"reason": reason, "conditions": conditions}

    def remember_work(self, hero, kind, target=None, **details):
        """Record accepted daytime work, never speculative role assignments."""
        if self.turn.is_day and hero.kind == "worker" and hero.id in self.used:
            self.work_jobs[hero.id] = dict(kind=kind, target=target, **details)

    def _reject(self, reason):
        try:
            self._record_rejection(reason)
        except Exception as error:
            logging_failure(error)
        return False

    def _record_rejection(self, reason):
        uid, cmd = self._attempt
        counts = self.rejections.setdefault(uid, {})
        counts[reason] = counts.get(reason, 0) + 1
        samples = self.rejected_samples.setdefault(uid, [])
        if len(samples) < 3:
            unit = self.turn.units.get(uid)
            targets = cmd.get("targetPos") or []
            target = targets[0] if isinstance(targets, (list, tuple)) and targets else None
            location = (target.get("x"), target.get("y")) if isinstance(target, dict) else None
            if location is not None and any(type(value) is not int for value in location):
                location = None
            samples.append({"reason": reason, "command": cmd.copy(), "gold_available": self.gold,
                            "day": self.turn.is_day, "actor_used": uid in self.used,
                            "actor_pos": unit.pos if unit else None, "actor_kind": unit.kind if unit else None,
                            "cooldown": unit.cooldown if unit else None,
                            "free_space": unit.space if unit else None,
                            "target_blocked": location in self.turn.blocked,
                            "target_reserved": location in self.reserved})

    def add(self, uid, cmd):
        self._attempt = uid, cmd
        gold_before = self.gold
        result = self._add(uid, cmd)
        if result:
            try:
                self._record_accepted(uid, cmd, gold_before)
            except Exception as error:
                logging_failure(error)
        return result

    def _record_accepted(self, uid, cmd, gold_before):
        unit = self.turn.units[uid]
        action = cmd["action"]
        reasons = {"move": "move_to_strategy_destination", "build": "build_selected_site",
                   "attack": "fire_selected_targets", "buy": "buy_selected_item",
                   "sell": "sell_selected_ore", "use": "use_available_item",
                   "collect": "collect_selected_mine", "acceptTask": "accept_available_task",
                   "submitAnswer": "submit_ready_answer", "summonTreasure": "attempt_treasure_opening",
                   "remove": "remove_selected_wall", "drop": "drop_selected_item",
                   "destroy": "destroy_enemy_mine", "catch": "catch_enemy_imp"}
        self.explain(uid, reasons.get(action, "selected_by_strategy"),
                     gold_before=gold_before, reserved_cost=gold_before - self.gold, gold_after=self.gold,
                     free_space=unit.space, stone_count=unit.inventory["stone"],
                     required_wall_stones=self.cfg.wall_stones, weapon_cost=self.cfg.weapon_cost,
                     attack_range=unit.attack_range, cooldown=unit.cooldown,
                     target_distances=[distance(unit.pos, pos(p)) for p in cmd.get("targetPos", [])],
                     controller_id=cmd.get("controllerId"), phase="day" if self.turn.is_day else "night")
        frame = sys._getframe(2)
        try:
            self.origins[uid] = [{"file": f.f_code.co_filename.rsplit("/", 1)[-1],
                                  "function": f.f_code.co_name, "line": f.f_lineno}
                                 for f in (frame, frame.f_back) if f is not None]
        finally:
            del frame
    def _add(self, uid, cmd):
        unit = self.turn.units.get(uid)
        if not unit or uid in self.used:
            return self._reject("actor_unavailable_or_already_used")
        action = cmd.get("action")
        targets = cmd.get("targetPos", [])
        try:
            points = [pos(p) for p in targets]
        except (TypeError, ValueError, KeyError):
            return self._reject("invalid_target_position")
        if any(not self.turn.inside(p) for p in points):
            return self._reject("target_outside_map")
        target = points[0] if points else None
        name = cmd.get("name")
        cost = 0
        controller = None
        if uid in self.turn.summon_robot_ids:
            if self.turn.is_day or unit.abnormal_state == "dizzy":
                return self._reject("robot_unavailable_by_phase_or_dizzy")
            if "controllerId" in cmd or len(points) != 1:
                return self._reject("robot_expected_one_target_without_controller")
            if action == "move":
                if (not self.turn.adjacent(unit.pos, target) or target in self.turn.blocked
                        or target in self.reserved):
                    return self._reject("move_not_adjacent_or_occupied")
            elif action == "attack":
                if (unit.attack_range <= 0 or distance(unit.pos, target) > unit.attack_range
                        or not any(target in enemy.cells and enemy.kind in (*CHARACTERS, *WEAPONS, "wall", "station")
                                   for enemy in (*self.turn.enemies, *self.robot_known_enemies))):
                    return self._reject("robot_attack_requires_enemy_in_range")
            else:
                return self._reject("robot_action_unsupported")
        elif action == "attack":
            try:
                controller = self.turn.units[int(cmd.get("controllerId", ""))]
            except (ValueError, KeyError, TypeError):
                return self._reject("invalid_controller")
            expected = 1 if unit.kind == "railgun" else unit.level
            if (self.turn.is_day or unit.kind not in WEAPONS or unit.cooldown > 0
                    or controller.kind not in CHARACTERS or controller.id in self.used
                    or not self.turn.adjacent(controller.pos, unit.pos)
                    or len(points) != expected or unit.attack_range <= 0
                    or any(distance(unit.pos, p) > unit.attack_range for p in points)):
                return self._reject("attack_preconditions")
            if unit.kind == "gatling":
                vectors = [(p[0]-unit.pos[0], p[1]-unit.pos[1]) for p in points]
                if any(a[0]*b[0]+a[1]*b[1] < 0 for a in vectors for b in vectors):
                    return self._reject("gatling_cone_exceeded")
        else:
            if unit.kind not in CHARACTERS:
                return self._reject("actor_kind_unsupported")
            if unit.kind == "imp" and action not in ("move", "destroy", "catch"):
                return self._reject("imp_action_unsupported")
            needs_pos = action in ("move", "collect", "build", "remove", "summonTreasure", "destroy", "catch")
            if needs_pos and len(points) != 1:
                return self._reject("expected_one_target")
            if action == "move":
                if (not self.turn.adjacent(unit.pos, target) or target in self.turn.blocked
                        or target in self.reserved):
                    return self._reject("move_not_adjacent_or_occupied")
            elif action == "build":
                if (not self.turn.is_day or unit.kind != "worker"
                        or not self.turn.adjacent(unit.pos, target)
                        or target in self.turn.blocked or target in self.reserved):
                    return self._reject("build_preconditions")
                if name in WEAPONS:
                    if target not in self.tower_cells or len(self.turn.weapons) + self.new_towers >= 3:
                        return self._reject("weapon_site_or_count_limit")
                    cost = self.cfg.weapon_cost
                elif name == "wall":
                    if self.turn.tick >= DEFENCE_RETURN_TICK:
                        return self._reject("wall_construction_cutoff")
                    if target not in self.wall_cells or unit.inventory["stone"] < self.cfg.wall_stones:
                        return self._reject("wall_site_or_materials")
                else:
                    return self._reject("unknown_building")
            elif action == "collect":
                if (unit.kind != "worker" or not unit.space
                        or self.turn.zones.get(target) not in ORES
                        or not self.turn.adjacent(unit.pos, target)):
                    return self._reject("collect_preconditions")
            elif action == "destroy":
                if (unit.kind != "imp" or not self.turn.adjacent(unit.pos, target)
                        or not self.turn.enemy_mine(target)):
                    return self._reject("destroy_requires_adjacent_enemy_mine")
            elif action == "catch":
                if not self.turn.adjacent(unit.pos, target) or not any(
                        u.kind == "imp" and u.pos == target for u in self.turn.enemies):
                    return self._reject("catch_requires_adjacent_enemy_imp")
            elif action in ("sell", "buy"):
                num = cmd.get("num", 1)
                if type(num) is not int or num <= 0 or not isinstance(name, str):
                    return self._reject("invalid_trade_quantity_or_name")
                zone = "vendor" if action == "sell" else "weaponShop"
                if not any(k == zone and self.turn.adjacent(unit.pos, p) for p, k in self.turn.zones.items()):
                    return self._reject("not_adjacent_to_shop")
                if action == "sell":
                    if name not in ORES or unit.inventory[name] < num or name not in self.turn.prices:
                        return self._reject("ore_inventory_or_price")
                else:
                    if name not in self.turn.shop or self.turn.shop[name] < 0 or unit.space < num:
                        return self._reject("shop_item_price_or_capacity")
                    cost = self.turn.shop[name] * num
            elif action == "acceptTask":
                if (unit.kind != "pioneer" or self.turn.phase_task or not any(
                        t.get("isValid", False) and int(t.get("coldDownRounds", 0)) == 0
                        and any(self.turn.adjacent(unit.pos, p) for p in self.turn.task_cells(t))
                        for t in self.turn.tasks)):
                    return self._reject("task_accept_preconditions")
            elif action == "submitAnswer":
                if unit.kind != "pioneer" or not self.turn.phase_task or not isinstance(cmd.get("taskAnswer"), str):
                    return self._reject("task_submit_preconditions")
            elif action == "summonTreasure":
                items = cmd.get("item")
                if (unit.kind != "pioneer" or not self.turn.adjacent(unit.pos, target)
                        or not isinstance(items, list) or not all(isinstance(x, str) for x in items)
                        or Counter(items) - unit.inventory):
                    return self._reject("treasure_preconditions")
            elif action == "use":
                if name not in unit.inventory:
                    return self._reject("item_not_in_inventory")
                if name in SUMMON_ORDERS:
                    if (len(points) != 1 or not self.turn.summon_position_legal(target)
                            or target in self.summon_pending_positions
                            or self.summon_daily_count >= 10):
                        return self._reject("summon_position_or_daily_limit")
                elif name in ("Bomb", "DizzyWeapon", "WallFixer") or "UpgradeVoucher" in str(name):
                    if len(points) != 1:
                        return self._reject("item_expected_one_target")
                    if name not in ("Bomb", "DizzyWeapon"):
                        building = next((u for u in self.turn.ours if target == u.pos), None)
                        if not building or not any(self.turn.adjacent(unit.pos, p) for p in building.cells):
                            return self._reject("building_missing_or_not_adjacent")
                        if name == "WallFixer" and (building.kind != "wall" or building.id in self.repair_claims):
                            return self._reject("wall_repair_already_claimed_or_wrong_kind")
                        if "UpgradeVoucher" in name:
                            kinds = WEAPONS if name.startswith("Weapon") else ("station",) if name.startswith("Station") else ("wall",)
                            if (building.kind not in kinds or not name.endswith(str(building.level))
                                    or building.level >= 3 or building.id in self.upgrade_claims):
                                return self._reject("upgrade_level_kind_or_claim")
            elif action == "drop":
                if name not in unit.inventory:
                    return self._reject("drop_item_missing")
            elif action == "remove":
                if unit.kind != "worker" or not self.turn.adjacent(unit.pos, target) or not any(
                        u.kind == "wall" and u.pos == target for u in (*self.turn.ours, *self.turn.enemies)):
                    return self._reject("remove_preconditions")
            else:
                return self._reject("unsupported_action")
        if cost > self.gold:
            return self._reject("insufficient_gold")
        self.gold -= cost
        self.used.add(uid)
        if controller:
            self.used.add(controller.id)
        if action in ("build", "move"):
            self.reserved.add(target)
        if action == "build" and name in WEAPONS:
            self.new_towers += 1
        if action == "buy":
            self.purchases.add(name)
        if action == "use" and "UpgradeVoucher" in str(name):
            self.upgrade_claims.add(building.id)
        if action == "use" and name == "WallFixer":
            self.repair_claims.add(building.id)
        if action == "use" and name in SUMMON_ORDERS:
            self.summon_pending_positions.add(target)
            self.summon_daily_count += 1
        self.commands[str(uid)] = cmd
        return True

    def response(self, prompt="", execute=""):
        return {"roleCommandMap": self.commands.copy(), "prompt": prompt, "executeCmd": execute}

