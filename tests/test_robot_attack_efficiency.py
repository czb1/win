"""Fast decision/feedback checks for avoiding unnecessary assault movement."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import assault_payload, controlled_robot
from agent.commands import command
from agent.model import distance, pos
from agent.robot_assault import RobotAssaultMemory, act_robots


def decide(data, memory=None):
    memory = memory if memory is not None else RobotAssaultMemory()
    turn, _, nav, ledger = setup_case(data, layout_mode="explicit")
    act_robots(turn, memory, nav, ledger)
    return ledger, memory


def base_corner_case():
    data = payload(99, [unit(20013, "station", 32, 11)])
    data["mapInfo"].update(width=41, height=32)
    data["teamOur"]["type"] = "defender"
    data["teamEnemy"]["roles"] = [unit(10013, "station", 9, 22, health=1500)]
    data["teamOur"]["summonRobotList"] = [controlled_robot(31036, 7, 21, targetTeam="challenger")]
    return data


class RobotAttackEfficiencyTests(unittest.TestCase):
    def test_rejected_base_cell_tries_another_cell_without_moving(self):
        for key in ("30000", 30000):
            with self.subTest(key=key):
                data = assault_payload(robot=controlled_robot(x=7))
                first, memory = decide(data)
                self.assertEqual(first.commands["30000"], command("attack", (10, 4)))
                data["roundNo"] += 1
                data["lastRoundRoleActionResults"] = {key: False}
                ledger, _ = decide(data, memory)
                self.assertEqual(ledger.commands["30000"], command("attack", (10, 3)))
                self.assertEqual(data["teamOur"]["summonRobotList"][0]["pos"], {"x": 7, "y": 4})

    def test_moves_only_after_every_in_range_base_cell_is_rejected(self):
        data = assault_payload(robot=controlled_robot(x=7))
        _, memory = decide(data)
        for _ in range(2):
            data["roundNo"] += 1
            data["lastRoundRoleActionResults"] = {"30000": False}
            ledger, _ = decide(data, memory)
        action = ledger.commands["30000"]
        self.assertEqual(action["action"], "move")
        self.assertEqual(distance((7, 4), pos(action["targetPos"][0])), 1)
        self.assertEqual(len(ledger.commands), 1)

    def test_all_four_base_cells_are_considered_before_repositioning(self):
        data = base_corner_case()
        ledger, memory = decide(data)
        points = {pos(ledger.commands["31036"]["targetPos"][0])}
        for _ in range(3):
            data["roundNo"] += 1
            data["lastRoundRoleActionResults"] = {"31036": False}
            ledger, _ = decide(data, memory)
            self.assertEqual(ledger.commands["31036"]["action"], "attack")
            point = pos(ledger.commands["31036"]["targetPos"][0])
            self.assertNotIn(point, points)
            points.add(point)
        self.assertEqual(points, {(9, 21), (9, 22), (10, 21), (10, 22)})
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"31036": False}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["31036"]["action"], "move")

    def test_alternate_base_aim_preserves_range_and_ownership_under_reflection(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = base_corner_case()
                data["robot"]["roles"] = [controlled_robot(30000, 8, 22)]
                for roles in (data["teamOur"]["roles"], data["teamEnemy"]["roles"],
                              data["teamOur"]["summonRobotList"], data["robot"]["roles"]):
                    for role in roles:
                        x, y = pos(role["pos"])
                        station = role["roleType"] == "station"
                        role["pos"] = {"x": 40 - x - int(station) if flip_x else x,
                                       "y": 31 - y + int(station) if flip_y else y}
                ledger, _ = decide(data)
                action = ledger.commands["31036"]
                self.assertEqual(action["action"], "attack")
                point = pos(action["targetPos"][0])
                self.assertLessEqual(distance(pos(data["teamOur"]["summonRobotList"][0]["pos"]), point), 3)
                self.assertNotIn("30000", ledger.commands)
                self.assertEqual(ledger.notes[31036]["conditions"]["target_id"], 10013)

    def test_partial_obstruction_does_not_force_a_move_or_tower_attack(self):
        for obstruction in ("ally", "public_robot", "npc", "enemy_tower"):
            with self.subTest(obstruction=obstruction):
                data = base_corner_case()
                if obstruction == "ally":
                    data["teamOur"]["roles"].append(unit(20010, "worker", 8, 22))
                elif obstruction == "public_robot":
                    data["robot"]["roles"] = [controlled_robot(30000, 8, 22)]
                elif obstruction == "npc":
                    data["mapInfo"]["zones"] = [{"neutralType": "vendor", "pos": {"x": 8, "y": 22}}]
                else:
                    data["teamEnemy"]["roles"].append(unit(10042, "rocket", 8, 22))
                ledger, _ = decide(data)
                self.assertEqual(ledger.commands["31036"], command("attack", (9, 21)))
                self.assertEqual(ledger.notes[31036]["conditions"]["target_id"], 10013)
                self.assertNotIn("30000", ledger.commands)

    def test_successful_aim_is_retained_when_the_default_cell_becomes_clear(self):
        data = base_corner_case()
        data["teamOur"]["roles"].append(unit(20010, "worker", 8, 22))
        first, memory = decide(data)
        self.assertEqual(first.commands["31036"], command("attack", (9, 21)))
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"31036": True}
        data["teamOur"]["roles"][-1]["pos"] = {"x": 8, "y": 23}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["31036"], first.commands["31036"])
        self.assertTrue(ledger.notes[31036]["conditions"]["accepted_aim"])

    def test_reported_99_to_100_stances_can_attack_another_base_cell(self):
        # Relevant coordinates from r99/r100 snapshots, with no ordinary units.
        # Hidden occupants are unreported; this checks candidate selection,
        # not whether the engine would accept the counterfactual attack.
        data = base_corner_case()
        data["teamOur"]["roles"] = []
        data["teamOur"]["summonRobotList"] = [
            controlled_robot(31035, 7, 18, targetTeam="challenger"),
            controlled_robot(31036, 7, 19, targetTeam="challenger")]
        first, memory = decide(data)
        self.assertEqual(first.commands["31036"], command("attack", (9, 21)))
        data["roundNo"] = 100
        data["teamOur"]["summonRobotList"][0]["pos"] = {"x": 8, "y": 19}
        data["lastRoundRoleActionResults"] = {"31036": True}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["31036"], command("attack", (9, 22)))

    def test_attack_goals_include_stances_that_only_clear_an_alternate_cell(self):
        data = base_corner_case()
        data["teamOur"]["summonRobotList"][0]["pos"] = {"x": 5, "y": 21}
        data["teamOur"]["roles"].append(unit(20010, "worker", 8, 22))
        ledger, memory = decide(data)
        self.assertEqual(ledger.commands["31036"]["action"], "move")
        self.assertEqual(ledger.notes[31036]["conditions"]["route_steps"], 1)
        data["roundNo"] += 1
        data["teamOur"]["summonRobotList"][0]["pos"] = ledger.commands["31036"]["targetPos"][0]
        data["lastRoundRoleActionResults"] = {"31036": True}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["31036"]["action"], "attack")

    def test_ray_occupancy_change_clears_a_stale_failure(self):
        data = assault_payload(robot=controlled_robot(x=7))
        _, memory = decide(data)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": False}
        decide(data, memory)
        self.assertTrue(memory.rejected(30000, (7, 4), 99, (10, 4)))
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": True}
        data["teamOur"]["roles"].append(unit(50, "worker", 8, 4))
        decide(data, memory)
        self.assertFalse(memory.rejected(30000, (7, 4), 99, (10, 4)))

    def test_unchanged_ray_keeps_rejected_aim_excluded(self):
        data = assault_payload(robot=controlled_robot(x=7))
        _, memory = decide(data)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": False}
        decide(data, memory)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": True}
        ledger, _ = decide(data, memory)
        self.assertTrue(memory.rejected(30000, (7, 4), 99, (10, 4)))
        self.assertEqual(ledger.commands["30000"], command("attack", (10, 3)))

    def test_repeated_collisions_extend_avoidance_to_eight_then_sixteen_turns(self):
        data = assault_payload(robot=controlled_robot(x=3))
        first, memory = decide(data)
        point = pos(first.commands["30000"]["targetPos"][0])
        for issued, duration in ((70, 4), (75, 8), (84, 16)):
            with self.subTest(issued=issued):
                if issued != 70:
                    data["roundNo"] = issued
                    data["lastRoundRoleActionResults"] = {}
                    retry, _ = decide(data, memory)
                    self.assertEqual(retry.commands["30000"], first.commands["30000"])
                data["roundNo"] = issued + 1
                data["lastRoundRoleActionResults"] = {"30000": False}
                ledger, _ = decide(data, memory)
                self.assertEqual(memory.failed_steps[30000, point], data["roundNo"] + duration)
                self.assertNotEqual(ledger.commands["30000"], first.commands["30000"])

    def test_arriving_at_a_previously_failed_cell_clears_collision_count(self):
        data = assault_payload(robot=controlled_robot(x=3))
        first, memory = decide(data)
        point = pos(first.commands["30000"]["targetPos"][0])
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": True}
        decide(data, memory)  # Legal move that collided and remained at x=3.
        data["roundNo"] = 75
        data["lastRoundRoleActionResults"] = {}
        retry, _ = decide(data, memory)
        self.assertEqual(retry.commands["30000"], first.commands["30000"])
        data["roundNo"] += 1
        data["teamOur"]["summonRobotList"][0]["pos"] = copy.deepcopy(first.commands["30000"]["targetPos"][0])
        data["lastRoundRoleActionResults"] = {"30000": True}
        decide(data, memory)
        self.assertNotIn((30000, point), memory.step_failures)

    def test_new_day_drops_accepted_aim_and_scene_evidence(self):
        data = base_corner_case()
        data["teamOur"]["roles"].append(unit(20010, "worker", 8, 22))
        _, memory = decide(data)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"31036": True}
        decide(data, memory)
        self.assertTrue(memory.accepted_shots)
        data["roundNo"] = 200
        data["teamOur"]["roles"].pop()
        data["lastRoundRoleActionResults"] = {}
        ledger, _ = decide(data, memory)
        self.assertFalse(memory.accepted_shots)
        self.assertEqual(ledger.commands["31036"], command("attack", (9, 22)))


if __name__ == "__main__":
    unittest.main()
