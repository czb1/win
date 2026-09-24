"""Wall HP from the task book 4.5.1 attribute table, confirmed by its image."""
from dataclasses import dataclass
from .model import distance

# Level 1/2/3: 1000/1500/2000. Keep every night WallFixer entry point in sync.
WALL_MAX_HEALTH = (1000, 1500, 2000)
ROBOT_POWER = {"smallRobot": 5, "middleRobot": 10, "largeRobot": 20, "bossRobot": 40}


def active_hostiles(turn):
    return [r for r in turn.robots if turn.threatens_us(r) and r.abnormal_state != "dizzy"]


@dataclass(frozen=True)
class RepairRisk:
    maximum: int
    threshold: int
    recent_damage: int
    nearby_damage: int
    nearby: int
    reason: str
    needed: bool
    emergency: bool


def repair_risk(turn, wall, mem=None):
    maximum = WALL_MAX_HEALTH[wall.level - 1]
    hostile = active_hostiles(turn)
    nearby = [r for r in hostile if distance(r.pos, wall.pos) <= (r.attack_range or 3)]
    recent = mem.wall_watch.recent_damage(wall, turn.round) if mem else 0
    # Range overlap is a pressure indicator, not proof every robot hits this wall.
    potential = sum(r.power or ROBOT_POWER.get(r.kind, 0) for r in nearby)
    reason = "base_15pct"
    if nearby and (len(nearby) >= 3 and potential * 20 >= maximum or recent * 20 >= maximum):
        reason = "focus_25pct"
    elif nearby and turn.day >= 8 and max(potential, recent) * 50 >= maximum:
        reason = "late_25pct"
    threshold = maximum * (15 if reason == "base_15pct" else 25) // 100
    # Only observed wall damage can lift the threshold beyond 25%. Require a
    # current attacker so stale damage or a cleared wave cannot waste a pack.
    emergency = bool(nearby and recent and wall.health <= (recent * 5 + 3) // 4)
    if emergency:
        threshold = max(threshold, min(maximum * 35 // 100, (recent * 5 + 3) // 4))
        reason = "observed_burst"
    needed = bool(wall.kind == "wall" and 0 < wall.health < maximum and hostile
                  and (wall.health < threshold or emergency))
    if not hostile:
        reason = "no_active_hostiles"
    return RepairRisk(maximum, threshold, recent, potential, len(nearby), reason, needed, emergency)


def needs_night_repair(wall, turn=None, mem=None):
    if turn is not None:
        return repair_risk(turn, wall, mem).needed
    return (wall.kind == "wall" and 0 < wall.health
            and wall.health * 100 < 15 * WALL_MAX_HEALTH[wall.level - 1])
