from dataclasses import dataclass
from collections import Counter

WEAPONS = ("gatling", "railgun", "rocket")
ROBOTS = ("smallRobot", "middleRobot", "largeRobot", "bossRobot")
ROBOT_POWER = {"smallRobot": 5, "middleRobot": 10, "largeRobot": 20, "bossRobot": 40}
SUMMON_ORDERS = ("SmallRobotSummonOrder", "MiddleRobotSummonOrder",
                 "LargeRobotSummonOrder", "BossRobotSummonOrder")
HEROES = ("worker", "pioneer")
CHARACTERS = (*HEROES, "imp")
ORES = ("stone", "iron", "copper")
# Daily tick: recall operators and stop wall construction at the same time.
DEFENCE_RETURN_TICK = 67
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
    abnormal_state: str = ""
    is_driving: bool = False

    @classmethod
    def load(cls, r):
        robot = r["roleType"] in ROBOTS
        return cls(int(r["id"]), pos(r["pos"]), r["roleType"], int(r["health"]),
                   max(1, min(3, int(r.get("level") or 1))),
                   int(r.get("attackRange") or (3 if robot else 0)),
                   int(r.get("attackPower") or ROBOT_POWER.get(r["roleType"], 0)),
                   int(r.get("cooldown") or 0), int(r.get("backPackCapability") or 0),
                   tuple(r.get("backpack") or ()), r.get("targetTeam", ""),
                   r.get("abnormalState", ""), r.get("isDriving") is True)

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
        self.projectile_origin = cfg.projectile_origin
        self.projectile_characters_block = cfg.projectile_characters_block
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
        self.mine_remain = {pos(z["pos"]): z.get("remain") for z in m.get("zones", [])
                            if z["neutralType"] in ORES}
        self.ours = tuple(Unit.load(r) for r in team.get("roles", []) if int(r["health"]) > 0)
        self.enemies = tuple(Unit.load(r) for r in data.get("teamEnemy", {}).get("roles", []) if int(r["health"]) > 0)
        self.robots = tuple(Unit.load(r) for r in data.get("robot", {}).get("roles", []) if int(r["health"]) > 0)
        # Only this authoritative list grants control. targetTeam describes a
        # destination, not ownership; robot.roles may also repeat an ally.
        self.summon_robots = tuple(Unit.load(r) for r in team.get("summonRobotList", [])
                                   if r.get("roleType") in ROBOTS and int(r["health"]) > 0)
        self.summon_robot_ids = {r.id for r in self.summon_robots}
        self.heroes = sorted((r for r in self.ours if r.kind in HEROES), key=lambda r: r.id)
        self.characters = sorted((r for r in self.ours if r.kind in CHARACTERS), key=lambda r: r.id)
        self.imps = [r for r in self.characters if r.kind == "imp"]
        self.workers = [r for r in self.heroes if r.kind == "worker"]
        self.pioneer = next((r for r in self.heroes if r.kind == "pioneer"), None)
        self.weapons = sorted((r for r in self.ours if r.kind in WEAPONS), key=lambda r: r.id)
        self.station = next((r for r in self.ours if r.kind == "station"), None)
        self.units = {r.id: r for r in (*self.ours, *self.summon_robots)}
        self.tasks = team.get("playerTasks", [])
        self.phase_task = str(data.get("phaseTask") or "")
        self.prices = {r["name"]: int(r["price"]) for r in data.get("vendorShopList", [])}
        self.shop = {r["name"]: int(r["price"]) for r in data.get("weaponShopList", [])}
        self.blocked = {p for p, k in self.zones.items() if k != "land"}
        for r in (*self.ours, *self.enemies, *self.robots, *self.summon_robots):
            self.blocked.update(r.cells)
        # Task points may be missing from zones in minimal requests.
        self.blocked.update(pos(t["taskPosition"]) for t in self.tasks)

    def inside(self, p):
        return 0 <= p[0] < self.width and 0 <= p[1] < self.height

    def adjacent(self, a, b):
        return a != b and distance(a, b) <= 1

    def base_distance(self, p):
        return min(distance(p, q) for q in self.station.cells) if self.station else 999

    def summon_position_legal(self, p):
        """Static v2 summon restrictions; dynamic occupants may shift spawning.

        The two construction rings follow the repository's inferred demo
        geometry: the rulebook provides only a picture, no zone coordinates.
        """
        if not self.inside(p):
            return False
        bases = [u for u in (*self.ours, *self.enemies) if u.kind == "station"]
        if any(min(distance(p, cell) for cell in base.cells) <= 2 for base in bases):
            return False
        kind = self.zones.get(p)
        if kind and kind != "land" and kind not in ORES:
            return False
        if any(p in u.cells for u in (*self.ours, *self.enemies)
               if u.kind in (*WEAPONS, "wall", "station")):
            return False
        return True

    def mine_half(self, p):
        """Signed side of the bottom-left/top-right diagonal; zero is ambiguous."""
        return p[1] * (self.width - 1) - p[0] * (self.height - 1)

    def enemy_mine(self, p):
        """Live ore in the enemy base corner, outside the central 20% bands."""
        if (not self.station or not self.inside(p) or self.zones.get(p) not in ORES
                or self.mine_remain.get(p) is not None and self.mine_remain[p] <= 0):
            return False
        home = sum(self.mine_half(cell) for cell in self.station.cells)
        if home * self.mine_half(p) >= 0:
            return False
        enemy = next((u for u in self.enemies if u.kind == "station"), None)
        base = enemy or self.station
        # Use the 2x2 footprint centre, not the team label or imp position.
        dx = 2 * base.pos[0] + 1 - (self.width - 1)
        dy = 2 * base.pos[1] - 1 - (self.height - 1)
        if enemy is None:
            # Compatibility with incomplete fixtures: v2 bases occupy opposite
            # corners, so infer the enemy corner from our actual base.
            dx, dy = -dx, -dy
        if dx < 0 < dy:
            return 5 * p[0] <= 2 * (self.width - 1) and 5 * p[1] >= 3 * (self.height - 1)
        if dy < 0 < dx:
            return 5 * p[0] >= 3 * (self.width - 1) and 5 * p[1] <= 2 * (self.height - 1)
        return False

    def threatens_us(self, robot):
        if robot.id in self.summon_robot_ids:
            return False
        # The protocol field is authoritative; spawn side and current distance
        # must never override an explicit destination team.
        if robot.target_team in ("challenger", "defender"):
            return robot.target_team == self.team
        other = next((u for u in self.enemies if u.kind == "station"), None)
        if self.station and other:
            return self.base_distance(robot.pos) <= min(distance(robot.pos, p) for p in other.cells)
        # Compatibility with incomplete offline/older payloads.
        return True

    def task_cells(self, task):
        anchor = pos(task["taskPosition"])
        kind = self.zones.get(anchor)
        if kind and kind.startswith(self.team + "TaskPoint"):
            return {p for p, k in self.zones.items() if k == kind}
        return {anchor}
