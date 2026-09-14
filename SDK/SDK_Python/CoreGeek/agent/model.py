from dataclasses import dataclass
from collections import Counter

WEAPONS = ("gatling", "railgun", "rocket")
HEROES = ("worker", "pioneer")
ORES = ("stone", "iron", "copper")
Pos = tuple[int, int]


def pos(raw) -> Pos:
    if not isinstance(raw, dict) or any(type(raw.get(k)) is not int for k in ("x", "y")):
        raise ValueError("position must contain integer x and y")
    return raw["x"], raw["y"]


def dump(p):
    return {"x": p[0], "y": p[1]}


def distance(a, b):
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def neighbours(p):
    return [(p[0] + dx, p[1] + dy) for dx in (-1, 0, 1)
            for dy in (-1, 0, 1) if dx or dy]


@dataclass(frozen=True)
class Unit:
    id: int
    pos: Pos
    kind: str
    health: int
    level: int = 1
    attack_range: int = 0
    power: int = 0
    cooldown: int = 0
    capacity: int = 0
    backpack: tuple = ()
    target_team: str = ""

    @classmethod
    def load(cls, r):
        return cls(int(r["id"]), pos(r["pos"]), r["roleType"], int(r["health"]),
                   max(1, min(3, int(r.get("level") or 1))),
                   int(r.get("attackRange") or 0), int(r.get("attackPower") or 0),
                   int(r.get("cooldown") or 0), int(r.get("backPackCapability") or 0),
                   tuple(r.get("backpack") or ()), r.get("targetTeam", ""))

    @property
    def cells(self):
        x, y = self.pos
        return {(x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1)} if self.kind == "station" else {self.pos}

    @property
    def inventory(self):
        return Counter(self.backpack)

    @property
    def space(self):
        return max(0, self.capacity - len(self.backpack))


class Turn:
    def __init__(self, data, cfg):
        self.raw = data
        self.round = int(data["roundNo"])
        if self.round < cfg.round_origin:
            raise ValueError("roundNo precedes configured round origin")
        offset = self.round - cfg.round_origin
        self.day, self.tick = offset // 130 + 1, offset % 130
        self.is_day = self.tick < 70
        self.day_left = max(0, 70 - self.tick)
        m, team = data["mapInfo"], data["teamOur"]
        self.width, self.height = int(m["width"]), int(m["height"])
        if not (1 <= self.width <= 100 and 1 <= self.height <= 100):
            raise ValueError("unsupported map dimensions")
        self.team = team["type"]
        if self.team not in ("challenger", "defender"):
            raise ValueError("invalid team type")
        self.key = str(team.get("teamId", self.team)), self.team
        self.gold = max(0, int(team.get("goldNum", 0)))
        self.zones = {pos(z["pos"]): z["neutralType"] for z in m.get("zones", [])}
        self.ours = tuple(Unit.load(r) for r in team.get("roles", []) if int(r["health"]) > 0)
        self.enemies = tuple(Unit.load(r) for r in data.get("teamEnemy", {}).get("roles", []) if int(r["health"]) > 0)
        self.robots = tuple(Unit.load(r) for r in data.get("robot", {}).get("roles", []) if int(r["health"]) > 0)
        self.heroes = sorted((r for r in self.ours if r.kind in HEROES), key=lambda r: r.id)
        self.workers = [r for r in self.heroes if r.kind == "worker"]
        self.pioneer = next((r for r in self.heroes if r.kind == "pioneer"), None)
        self.weapons = sorted((r for r in self.ours if r.kind in WEAPONS), key=lambda r: r.id)
        self.station = next((r for r in self.ours if r.kind == "station"), None)
        self.units = {r.id: r for r in self.ours}
        self.tasks = team.get("playerTasks", [])
        self.phase_task = str(data.get("phaseTask") or "")
        self.prices = {r["name"]: int(r["price"]) for r in data.get("vendorShopList", [])}
        self.shop = {r["name"]: int(r["price"]) for r in data.get("weaponShopList", [])}
        self.blocked = {p for p, k in self.zones.items() if k != "land"}
        for r in (*self.ours, *self.enemies, *self.robots):
            self.blocked.update(r.cells)
        # Task points may be missing from zones in minimal requests.
        self.blocked.update(pos(t["taskPosition"]) for t in self.tasks)

    def inside(self, p):
        return 0 <= p[0] < self.width and 0 <= p[1] < self.height

    def adjacent(self, a, b):
        return a != b and distance(a, b) <= 1

    def base_distance(self, p):
        return min(distance(p, q) for q in self.station.cells) if self.station else 999

    def task_cells(self, task):
        anchor = pos(task["taskPosition"])
        kind = self.zones.get(anchor)
        if kind and kind.startswith(self.team + "TaskPoint"):
            return {p for p, k in self.zones.items() if k == kind}
        return {anchor}
