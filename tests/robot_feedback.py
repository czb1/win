"""Explicit prior rejection fixtures for testing assault fallback behavior."""
from test_agent import setup_case
from agent.model import distance
from agent.robot_assault import RobotAssaultMemory, enemy_base, _target_points


def reject_base_aims(data, memory=None, ids=None):
    """Record one failed attack per robot per turn, without simulating damage."""
    memory = memory if memory is not None else RobotAssaultMemory()
    turn, _, _, _ = setup_case(data)
    base = enemy_base(turn)
    memory.observe(turn, base)
    aims = {r.id: [p for p in _target_points(r.pos, base)
                   if distance(r.pos, p) <= r.attack_range]
            for r in turn.summon_robots if ids is None or r.id in ids}
    for index in range(max(map(len, aims.values()), default=0)):
        results = {}
        for robot in turn.summon_robots:
            points = aims.get(robot.id, [])
            if index < len(points):
                memory.remember(turn, robot, "attack", points[index], base.id)
                results[str(robot.id)] = False
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = results
        turn, _, _, _ = setup_case(data)
        memory.observe(turn, enemy_base(turn))
    data["lastRoundRoleActionResults"] = {}
    return memory
