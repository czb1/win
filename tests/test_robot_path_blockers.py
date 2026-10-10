"""Focused observed-state checks for base-directed robot obstacle clearing."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import assault_payload, controlled_robot
from agent.commands import command
from agent.brain import Agent
from agent.config import Config
from agent.model import distance, pos
from agent.robot_assault import RobotAssaultMemory, act_robots


def decide(data, memory=None):
    memory = memory if memory is not None else RobotAssaultMemory()
    turn, _, nav, ledger = setup_case(data, layout_mode="explicit")
    act_robots(turn, memory, nav, ledger)
    return ledger, memory


class RobotPathBlockerTests(unittest.TestCase):
    def test_each_enemy_building_or_character_on_the_ray_is_attacked_first(self):
        for kind in ("gatling", "railgun", "rocket", "wall", "worker", "pioneer", "imp"):
            with self.subTest(kind=kind):
                data = assault_payload(robot=controlled_robot(x=7))
                data["teamEnemy"]["roles"].append(unit(50, kind, 8, 4))
                ledger, _ = decide(data)
                self.assertEqual(ledger.commands["30000"], command("attack", (8, 4)))
                self.assertEqual(ledger.notes[30000]["conditions"]["target_id"], 50)

    def test_nearest_blocker_precedes_a_weaker_wall_and_the_base(self):
        data = assault_payload(robot=controlled_robot(x=6))
        data["teamEnemy"]["roles"] += [unit(50, "rocket", 7, 4, health=1000),
                                       unit(51, "wall", 8, 4, health=1)]
        ledger, _ = decide(data)
        self.assertEqual(ledger.commands["30000"], command("attack", (7, 4)))

    def test_clear_worker_wall_tower_then_advance_and_attack_base(self):
        data = assault_payload(robot=controlled_robot(x=6))
        data["teamEnemy"]["roles"] += [unit(50, "worker", 7, 4),
                                       unit(51, "wall", 8, 4), unit(52, "rocket", 9, 4)]
        memory = RobotAssaultMemory()
        for uid, point in ((50, (7, 4)), (51, (8, 4)), (52, (9, 4))):
            ledger, _ = decide(data, memory)
            self.assertEqual(ledger.commands["30000"], command("attack", point))
            next(r for r in data["teamEnemy"]["roles"] if r["id"] == uid)["health"] = 0
            data["roundNo"] += 1
        ledger, _ = decide(data, memory)
        action = ledger.commands["30000"]
        self.assertEqual(action["action"], "move")
        data["teamOur"]["summonRobotList"][0]["pos"] = action["targetPos"][0]
        data["roundNo"] += 1
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"]["action"], "attack")
        self.assertEqual(ledger.notes[30000]["conditions"]["target_id"], 99)
        self.assertEqual(memory.objectives[30000].phase, "base")

    def test_expected_lethal_damage_does_not_skip_a_live_blocker(self):
        data = assault_payload(robot=controlled_robot(x=7))
        data["teamEnemy"]["roles"].append(unit(50, "wall", 8, 4, health=1))
        ledger, memory = decide(data)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": True}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"], command("attack", (8, 4)))

    def test_enemies_off_the_ray_do_not_redirect_the_assault(self):
        data = assault_payload(robot=controlled_robot(x=7))
        data["teamEnemy"]["roles"] += [unit(50, "worker", 7, 5),
                                       unit(51, "wall", 8, 6, health=1), unit(52, "rocket", 6, 4)]
        ledger, _ = decide(data)
        self.assertEqual(ledger.commands["30000"], command("attack", (10, 4)))

    def test_blocker_order_survives_both_axis_reflections(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = assault_payload(robot=controlled_robot(x=7))
                data["teamEnemy"]["roles"].append(unit(50, "rocket", 8, 4))
                expected = (14 - 8 if flip_x else 8, 14 - 4 if flip_y else 4)
                for roles in (data["teamOur"]["roles"], data["teamEnemy"]["roles"],
                              data["teamOur"]["summonRobotList"]):
                    for r in roles:
                        x, y = r["pos"]["x"], r["pos"]["y"]
                        r["pos"] = {"x": 14 - x - (1 if r["roleType"] == "station" else 0) if flip_x else x,
                                    "y": 14 - y + (1 if r["roleType"] == "station" else 0) if flip_y else y}
                ledger, _ = decide(data)
                self.assertEqual(ledger.commands["30000"], command("attack", expected))

    def test_friendly_neutral_and_public_robots_are_bypassed_not_attacked(self):
        for obstruction in ("own_wall", "own_worker", "own_robot", "public_robot", "npc", "mine"):
            with self.subTest(obstruction=obstruction):
                data = assault_payload(robot=controlled_robot(x=7))
                if obstruction.startswith("own_") and obstruction != "own_robot":
                    data["teamOur"]["roles"].append(unit(50, obstruction[4:], 8, 4))
                elif obstruction == "own_robot":
                    data["teamOur"]["summonRobotList"].append(controlled_robot(30001, 8, 4))
                elif obstruction == "public_robot":
                    data["robot"]["roles"].append(controlled_robot(31000, 8, 4))
                else:
                    data["mapInfo"]["zones"] = [{"neutralType": "vendor" if obstruction == "npc" else "stone",
                                                 "pos": {"x": 8, "y": 4}}]
                ledger, _ = decide(data)
                self.assertEqual(ledger.commands["30000"]["action"], "move")
                self.assertNotEqual(pos(ledger.commands["30000"]["targetPos"][0]), (8, 4))
                self.assertNotIn("31000", ledger.commands)

    def test_out_of_range_blocker_is_approached_one_free_step(self):
        data = assault_payload(robot=controlled_robot(x=3, y=4))
        data["teamEnemy"]["roles"].append(unit(50, "rocket", 8, 4))
        ledger, _ = decide(data)
        action = ledger.commands["30000"]
        self.assertEqual(action["action"], "move")
        destination = pos(action["targetPos"][0])
        self.assertEqual(distance((3, 4), destination), 1)
        self.assertLess(distance(destination, (8, 4)), 5)


class RobotObstacleMemoryTests(unittest.TestCase):
    def hidden_weapon(self):
        data = assault_payload(round_no=65)
        data["teamOur"]["summonRobotList"] = []
        data["teamEnemy"]["roles"].append(unit(50, "rocket", 8, 4))
        _, memory = decide(data)
        data["roundNo"] = 71
        data["teamOur"]["summonRobotList"] = [controlled_robot(x=6)]
        data["teamEnemy"]["roles"] = data["teamEnemy"]["roles"][:1]
        return data, memory

    def test_previously_seen_hidden_weapon_can_be_cleared(self):
        data, memory = self.hidden_weapon()
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"], command("attack", (8, 4)))
        self.assertEqual(ledger.notes[30000]["conditions"]["target_source"], "last_seen_building")

    def test_agent_retains_hidden_weapons_from_an_earlier_daytime_request(self):
        data = assault_payload(round_no=65)
        data["teamOur"]["summonRobotList"] = []
        data["teamEnemy"]["roles"].append(unit(50, "rocket", 8, 4))
        agent = Agent(Config(layout_mode="explicit", llm_enabled=False))
        agent.decide(data)
        data["roundNo"] = 71
        data["teamOur"]["summonRobotList"] = [controlled_robot(x=6)]
        data["teamEnemy"]["roles"] = data["teamEnemy"]["roles"][:1]
        response = agent.decide(data)
        self.assertEqual(response["roleCommandMap"]["30000"], command("attack", (8, 4)))

    def test_repeated_invalid_hidden_target_is_bypassed_until_seen_again(self):
        data, memory = self.hidden_weapon()
        decide(data, memory)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": False}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"]["action"], "move")
        data["teamOur"]["summonRobotList"][0]["pos"] = ledger.commands["30000"]["targetPos"][0]
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": True}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"], command("attack", (8, 4)))
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": False}
        ledger, _ = decide(data, memory)
        self.assertIn(50, memory.rejected_buildings)
        self.assertIn(50, memory.buildings)  # Failure is not proof of death.
        self.assertEqual(ledger.commands["30000"]["action"], "move")
        self.assertEqual(ledger.notes[30000]["conditions"]["target_id"], 99)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {}
        data["teamEnemy"]["roles"].append(unit(50, "rocket", 8, 4, health=120))
        ledger, _ = decide(data, memory)
        self.assertNotIn(50, memory.rejected_buildings)
        # The old rejected stance remains excluded, but the visible target
        # can again be cleared from a new firing position.
        self.assertEqual(ledger.notes[30000]["conditions"]["target_id"], 50)

    def test_reported_death_or_absence_in_shared_vision_removes_cached_weapon(self):
        for evidence in ("death", "vision"):
            with self.subTest(evidence=evidence):
                data, memory = self.hidden_weapon()
                if evidence == "death":
                    data["teamEnemy"]["roles"].append(unit(50, "rocket", 8, 4, health=0))
                else:
                    data["teamOur"]["roles"].append(unit(51, "worker", 7, 5))
                ledger, _ = decide(data, memory)
                self.assertNotIn(50, memory.buildings)
                self.assertEqual(ledger.commands["30000"]["action"], "move")
                self.assertEqual(ledger.notes[30000]["conditions"]["target_id"], 99)

    def test_unseen_characters_are_not_used_as_current_obstacles(self):
        data = assault_payload(round_no=65)
        data["teamOur"]["summonRobotList"] = []
        data["teamEnemy"]["roles"].append(unit(50, "worker", 8, 4))
        _, memory = decide(data)
        data["roundNo"] = 71
        data["teamOur"]["summonRobotList"] = [controlled_robot(x=7)]
        data["teamEnemy"]["roles"] = data["teamEnemy"]["roles"][:1]
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"], command("attack", (10, 4)))

    def test_reported_positions_avoid_shooting_through_towers_and_allies(self):
        # Only relevant coordinates from the uploaded logs, not a full replay.
        data = payload(66, [unit(20013, "station", 32, 11)])
        data["mapInfo"].update(width=41, height=32)
        data["teamOur"]["type"] = "defender"
        data["teamEnemy"]["roles"] = [unit(10013, "station", 9, 22),
            unit(10040, "rocket", 8, 20), unit(10041, "rocket", 8, 22), unit(10042, "rocket", 9, 20)]
        _, memory = decide(data)
        data["roundNo"] = 71
        data["teamEnemy"]["roles"] = data["teamEnemy"]["roles"][:1]
        data["teamOur"]["summonRobotList"] = [controlled_robot(31035 + i, 6, 18 + i,
                                                    targetTeam="challenger") for i in range(3)]
        data["robot"]["roles"] = copy.deepcopy(data["teamOur"]["summonRobotList"])
        ledger, _ = decide(data, memory)
        # The first two rays also touch adjacent friendly robots. They must
        # move clear; the third robot can directly shoot the remembered tower.
        self.assertEqual(ledger.commands["31035"]["action"], "move")
        self.assertEqual(ledger.commands["31036"]["action"], "move")
        self.assertEqual(ledger.commands["31037"], command("attack", (8, 22)))
        for i in range(2):
            action = ledger.commands[str(31035 + i)]
            self.assertEqual(distance(pos(action["targetPos"][0]), (6, 18 + i)), 1)
            self.assertNotIn("controllerId", action)


class RobotActionFeedbackTests(unittest.TestCase):
    def test_false_attack_feedback_repositions_for_string_and_integer_ids(self):
        for key in ("30000", 30000):
            with self.subTest(key=key):
                data = assault_payload(robot=controlled_robot(x=7))
                first, memory = decide(data)
                self.assertEqual(first.commands["30000"], command("attack", (10, 4)))
                data["roundNo"] += 1
                data["lastRoundRoleActionResults"] = {key: False}
                ledger, _ = decide(data, memory)
                action = ledger.commands["30000"]
                self.assertEqual(action["action"], "move")
                self.assertEqual(distance(pos(action["targetPos"][0]), (7, 4)), 1)
                data["roundNo"] += 1
                data["teamOur"]["summonRobotList"][0]["pos"] = action["targetPos"][0]
                data["lastRoundRoleActionResults"] = {key: True}
                ledger, _ = decide(data, memory)
                self.assertEqual(ledger.commands["30000"]["action"], "attack")

    def test_missing_true_or_uncorrelated_feedback_does_not_invent_a_failure(self):
        for result, gap in ((None, 1), (True, 1), (False, 2)):
            with self.subTest(result=result, gap=gap):
                data = assault_payload(robot=controlled_robot(x=7))
                _, memory = decide(data)
                data["roundNo"] += gap
                data["lastRoundRoleActionResults"] = {} if result is None else {"30000": result}
                ledger, _ = decide(data, memory)
                self.assertEqual(ledger.commands["30000"], command("attack", (10, 4)))

    def test_a_collided_robot_move_uses_a_different_next_step(self):
        data = assault_payload(robot=controlled_robot(x=3, y=4))
        first, memory = decide(data)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": True}
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"]["action"], "move")
        self.assertNotEqual(ledger.commands["30000"], first.commands["30000"])

    def test_new_day_drops_old_robot_attack_failures(self):
        data = assault_payload(robot=controlled_robot(x=7))
        _, memory = decide(data)
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": False}
        decide(data, memory)
        data["roundNo"] = 200
        ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"], command("attack", (10, 4)))


if __name__ == "__main__":
    unittest.main()
