"""One per-turn ledger owns role locks, gold and destination reservations."""
from collections import Counter
from .model import WEAPONS, HEROES, ORES, dump, distance, pos

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
        self.upgrade_claims = set()
        self.repair_claims = set()
        self.mine_claims = {}
        self.return_targets = {}

    def add(self, uid, cmd):
        unit = self.turn.units.get(uid)
        if not unit or uid in self.used:
            return False
        action = cmd.get("action")
        targets = cmd.get("targetPos", [])
        try:
            points = [pos(p) for p in targets]
        except (TypeError, ValueError, KeyError):
            return False
        if any(not self.turn.inside(p) for p in points):
            return False
        target = points[0] if points else None
        name = cmd.get("name")
        cost = 0
        controller = None
        if action == "attack":
            try:
                controller = self.turn.units[int(cmd.get("controllerId", ""))]
            except (ValueError, KeyError, TypeError):
                return False
            expected = 1 if unit.kind == "railgun" else unit.level
            if (self.turn.is_day or unit.kind not in WEAPONS or unit.cooldown > 0
                    or controller.kind not in HEROES or controller.id in self.used
                    or not self.turn.adjacent(controller.pos, unit.pos)
                    or len(points) != expected or unit.attack_range <= 0
                    or any(distance(unit.pos, p) > unit.attack_range for p in points)):
                return False
            if unit.kind == "gatling":
                vectors = [(p[0]-unit.pos[0], p[1]-unit.pos[1]) for p in points]
                if any(a[0]*b[0]+a[1]*b[1] < 0 for a in vectors for b in vectors):
                    return False
        else:
            if unit.kind not in HEROES:
                return False
            needs_pos = action in ("move", "collect", "build", "remove", "summonTreasure")
            if needs_pos and len(points) != 1:
                return False
            if action == "move":
                if (not self.turn.adjacent(unit.pos, target) or target in self.turn.blocked
                        or target in self.reserved):
                    return False
            elif action == "build":
                if (not self.turn.is_day or unit.kind != "worker"
                        or not self.turn.adjacent(unit.pos, target)
                        or target in self.turn.blocked or target in self.reserved):
                    return False
                if name in WEAPONS:
                    if target not in self.tower_cells or len(self.turn.weapons) + self.new_towers >= 3:
                        return False
                    cost = self.cfg.weapon_cost
                elif name == "wall":
                    if target not in self.wall_cells or unit.inventory["stone"] < self.cfg.wall_stones:
                        return False
                else:
                    return False
            elif action == "collect":
                if (unit.kind != "worker" or not unit.space
                        or self.turn.zones.get(target) not in ORES
                        or not self.turn.adjacent(unit.pos, target)):
                    return False
            elif action in ("sell", "buy"):
                num = cmd.get("num", 1)
                if type(num) is not int or num <= 0 or not isinstance(name, str):
                    return False
                zone = "vendor" if action == "sell" else "weaponShop"
                if not any(k == zone and self.turn.adjacent(unit.pos, p) for p, k in self.turn.zones.items()):
                    return False
                if action == "sell":
                    if name not in ORES or unit.inventory[name] < num or name not in self.turn.prices:
                        return False
                else:
                    if name not in self.turn.shop or self.turn.shop[name] < 0 or unit.space < num:
                        return False
                    cost = self.turn.shop[name] * num
            elif action == "acceptTask":
                if (unit.kind != "pioneer" or self.turn.phase_task or not any(
                        t.get("isValid", False) and int(t.get("coldDownRounds", 0)) == 0
                        and any(self.turn.adjacent(unit.pos, p) for p in self.turn.task_cells(t))
                        for t in self.turn.tasks)):
                    return False
            elif action == "submitAnswer":
                if unit.kind != "pioneer" or not self.turn.phase_task or not isinstance(cmd.get("taskAnswer"), str):
                    return False
            elif action == "summonTreasure":
                items = cmd.get("item")
                if (unit.kind != "pioneer" or not self.turn.adjacent(unit.pos, target)
                        or not isinstance(items, list) or not all(isinstance(x, str) for x in items)
                        or Counter(items) - unit.inventory):
                    return False
            elif action == "use":
                if name not in unit.inventory:
                    return False
                if name in ("Bomb", "DizzyWeapon", "WallFixer") or "UpgradeVoucher" in str(name):
                    if len(points) != 1:
                        return False
                    if name not in ("Bomb", "DizzyWeapon"):
                        building = next((u for u in self.turn.ours if target == u.pos), None)
                        if not building or not any(self.turn.adjacent(unit.pos, p) for p in building.cells):
                            return False
                        if name == "WallFixer" and (building.kind != "wall" or building.id in self.repair_claims):
                            return False
                        if "UpgradeVoucher" in name:
                            kinds = WEAPONS if name.startswith("Weapon") else ("station",) if name.startswith("Station") else ("wall",)
                            if (building.kind not in kinds or not name.endswith(str(building.level))
                                    or building.level >= 3 or building.id in self.upgrade_claims):
                                return False
            elif action == "drop":
                if name not in unit.inventory:
                    return False
            elif action == "remove":
                if unit.kind != "worker" or not self.turn.adjacent(unit.pos, target) or not any(
                        u.kind == "wall" and u.pos == target for u in (*self.turn.ours, *self.turn.enemies)):
                    return False
            else:
                return False
        if cost > self.gold:
            return False
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
        self.commands[str(uid)] = cmd
        return True

    def response(self, prompt="", execute=""):
        return {"roleCommandMap": self.commands.copy(), "prompt": prompt, "executeCmd": execute}
