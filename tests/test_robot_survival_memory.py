"""Finite feedback/state checks; no engine replay or inferred hidden damage."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import assault_payload, controlled_robot
from test_robot_attack_efficiency import decide
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.robot_assault import RobotAssaultMemory, enemy_base


def occupied_error(uid, point):
    return {"errorCode": 4, "description":
            f"robot {uid} wants move to ({point[0]},{point[1]}), "
            "but target is occupied (canMove=false)"}


def record_move(data, memory, uid, point):
    turn, _, _, _ = setup_case(data, layout_mode="explicit")
    memory.observe(turn, enemy_base(turn))
    robot = next(r for r in turn.summon_robots if r.id == uid)
    memory.remember(turn, robot, "move", point)


def snapshot_corner(round_no=97):
    # Only the assault-relevant fields from the uploaded snapshots. The empty
    # ordinary-unit list and two surviving owned robots are actual r97 facts.
    data = payload(round_no, [])
    data["mapInfo"].update(width=41, height=32)
    data["teamOur"].update(type="defender", teamId="4493")
    data["teamEnemy"]["roles"] = [unit(10013, "station", 9, 22, health=1430)]
    data["teamOur"]["summonRobotList"] = [
        controlled_robot(31035, 7, 17, health=790, targetTeam="challenger"),
        controlled_robot(31036, 6, 19, health=740, targetTeam="challenger")]
    data["robot"]["roles"] = copy.deepcopy(data["teamOur"]["summonRobotList"])
    return data


class RobotSurvivingSessionTests(unittest.TestCase):
    def agent_before_base_loss(self, *, dizzy=False):
        data = snapshot_corner(72)
        data["teamOur"]["roles"] = [unit(20013, "station", 32, 11)]
        data["teamEnemy"]["roles"] += [
            unit(10040, "rocket", 9, 20, health=1000),
            unit(10041, "rocket", 8, 20, health=960),
            unit(10042, "rocket", 8, 22, health=1000)]
        if dizzy:
            for robot in data["teamOur"]["summonRobotList"]:
                robot["abnormalState"] = "dizzy"
        agent = Agent(Config(layout_mode="explicit", llm_enabled=False))
        agent.decide(data)
        data["roundNo"] = 96
        data["teamOur"]["roles"][0]["health"] = 60
        data["teamEnemy"]["roles"] = data["teamEnemy"]["roles"][:1]
        response = agent.decide(data)
        return agent, data, response, next(iter(agent.sessions.values()))

    def test_base_loss_preserves_hidden_weapons_feedback_and_next_turn_cache(self):
        agent, data, before, memory = self.agent_before_base_loss()
        self.assertEqual(before["roleCommandMap"]["31036"], command("attack", (8, 20)))
        data["roundNo"] = 97
        data["teamOur"]["roles"] = []
        data["lastRoundRoleActionResults"] = {"31035": True, "31036": True}
        after = agent.decide(data)
        self.assertEqual(len(agent.sessions), 1)
        self.assertIs(next(iter(agent.sessions.values())), memory)
        self.assertEqual(set(memory.robot_assault.buildings), {10040, 10041, 10042})
        self.assertEqual(memory.robot_assault.accepted_shots[31036, (6, 19), 10041], (8, 20))
        self.assertEqual(after["roleCommandMap"]["31036"], command("attack", (8, 20)))
        self.assertEqual(agent.decide(copy.deepcopy(data)), after)
        data["roundNo"] = 98
        agent.decide(data)
        self.assertIs(next(iter(agent.sessions.values())), memory)
        self.assertEqual(len(agent.sessions), 1)

    def test_dizzy_survivors_preserve_history_without_an_issued_command(self):
        agent, data, before, memory = self.agent_before_base_loss(dizzy=True)
        self.assertEqual(before["roleCommandMap"], {})
        self.assertFalse(memory.robot_assault.pending)
        data["roundNo"] = 97
        data["teamOur"]["roles"] = []
        agent.decide(data)
        self.assertIs(next(iter(agent.sessions.values())), memory)
        self.assertIn(10042, memory.robot_assault.buildings)

    def test_unrelated_or_discontinuous_payload_does_not_inherit_assault(self):
        for change in ("round_rewind", "round_gap", "team", "side", "enemy_base", "enemy_base_position",
                       "own_base", "different_owned_robots", "public_robots_only"):
            with self.subTest(change=change):
                agent, data, _, old_memory = self.agent_before_base_loss()
                data["roundNo"] = 97
                data["teamOur"]["roles"] = []
                if change == "round_rewind":
                    data["roundNo"] = 70
                elif change == "round_gap":
                    data["roundNo"] = 99
                elif change == "team":
                    data["teamOur"]["teamId"] = "another-match"
                elif change == "side":
                    data["teamOur"]["type"] = "challenger"
                elif change == "enemy_base":
                    data["teamEnemy"]["roles"][0]["id"] = 10099
                elif change == "enemy_base_position":
                    data["teamEnemy"]["roles"][0]["pos"] = {"x": 11, "y": 22}
                elif change == "own_base":
                    data["teamOur"]["roles"] = [unit(20013, "station", 30, 11)]
                elif change == "different_owned_robots":
                    data["teamOur"]["summonRobotList"] = [controlled_robot(31099, 6, 19)]
                else:
                    data["teamOur"]["summonRobotList"] = []
                agent.decide(data)
                current = next(reversed(agent.sessions.values()))
                self.assertIsNot(current, old_memory)
                self.assertFalse(current.robot_assault.buildings)


class RobotOccupiedStepTests(unittest.TestCase):
    def test_engine_occupied_cell_is_shared_by_other_owned_robots(self):
        data = assault_payload(robot=controlled_robot(x=3))
        data["teamOur"]["summonRobotList"].append(controlled_robot(30001, 3, 5))
        memory = RobotAssaultMemory()
        record_move(data, memory, 30000, (4, 4))
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30000": False}
        data["errors"] = [occupied_error(30000, (4, 4))]
        ledger, _ = decide(data, memory)
        self.assertIn((4, 4), memory.avoided_steps(30001))
        self.assertNotEqual(ledger.commands["30001"], command("move", (4, 4)))
        # A legal-but-collided move without the explicit occupied-cell error
        # still stays local to its actor, rather than blocking everyone.
        local = RobotAssaultMemory()
        earlier = copy.deepcopy(data)
        earlier["roundNo"] -= 1
        record_move(earlier, local, 30000, (4, 4))
        data["errors"] = []
        data["lastRoundRoleActionResults"] = {"30000": True}
        ledger, _ = decide(data, local)
        self.assertFalse(local.occupied_steps)
        self.assertEqual(ledger.commands["30001"], command("move", (4, 4)))

    def test_occupied_errors_require_matching_command_round_id_point_and_legality(self):
        for mismatch in ("round", "id", "point", "code", "text", "legal", "missing_result"):
            with self.subTest(mismatch=mismatch):
                data = assault_payload(robot=controlled_robot(x=3))
                memory = RobotAssaultMemory()
                record_move(data, memory, 30000, (4, 4))
                data["roundNo"] += 2 if mismatch == "round" else 1
                data["lastRoundRoleActionResults"] = {"30000": False}
                error = occupied_error(30000, (4, 4))
                if mismatch == "id":
                    error = occupied_error(30001, (4, 4))
                elif mismatch == "point":
                    error = occupied_error(30000, (4, 5))
                elif mismatch == "code":
                    error["errorCode"] = 5
                elif mismatch == "text":
                    error["description"] = "a move failed for an unknown reason"
                elif mismatch == "legal":
                    data["lastRoundRoleActionResults"]["30000"] = True
                elif mismatch == "missing_result":
                    data["lastRoundRoleActionResults"] = {}
                data["errors"] = [error]
                decide(data, memory)
                self.assertFalse(memory.occupied_steps)

    def test_explicit_repeated_occupancy_uses_shared_four_eight_sixteen_turn_windows(self):
        data = assault_payload(robot=controlled_robot(x=3))
        memory = RobotAssaultMemory()
        for issued, duration in ((70, 4), (75, 8), (84, 16), (101, 16)):
            data["roundNo"] = issued
            data["lastRoundRoleActionResults"] = {}
            data["errors"] = []
            record_move(data, memory, 30000, (4, 4))
            data["roundNo"] += 1
            data["lastRoundRoleActionResults"] = {30000: False}
            data["errors"] = [occupied_error(30000, (4, 4))]
            decide(data, memory)
            self.assertEqual(memory.occupied_steps[(4, 4)], data["roundNo"] + duration)
            self.assertIn((4, 4), memory.avoided_steps(30001))

    def test_actual_104_105_occupancy_does_not_invent_targets_or_change_accepted_shot(self):
        data = snapshot_corner(104)
        data["teamOur"]["summonRobotList"][0]["pos"] = {"x": 8, "y": 20}
        data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 7, "y": 21}
        data["robot"]["roles"] = copy.deepcopy(data["teamOur"]["summonRobotList"])
        memory = RobotAssaultMemory()
        record_move(data, memory, 31036, (8, 21))
        turn, _, _, _ = setup_case(data, layout_mode="explicit")
        memory.remember(turn, turn.summon_robots[0], "attack", (9, 21), 10013)
        for issued, point in ((104, (8, 21)), (105, (8, 22))):
            if issued == 105:
                record_move(data, memory, 31036, point)
            data["roundNo"] = issued + 1
            data["lastRoundRoleActionResults"] = {"31035": True, "31036": False}
            data["errors"] = [occupied_error(31036, point)]
            ledger, _ = decide(data, memory)
            self.assertIn(point, memory.avoided_steps(31035))
            self.assertEqual(ledger.commands["31035"], command("attack", (9, 21)))
            self.assertNotIn(point, ledger.turn.blocked)
            self.assertFalse(memory.buildings)
            self.assertEqual(set(ledger.commands), {"31035", "31036"})
        self.assertEqual(set(memory.occupied_steps), {(8, 21), (8, 22)})

    def test_only_ordinary_shared_vision_or_arrival_clears_the_occupied_evidence(self):
        for evidence in ("robot_vision_only", "ordinary_vision", "arrival"):
            with self.subTest(evidence=evidence):
                data = assault_payload(robot=controlled_robot(x=3))
                memory = RobotAssaultMemory()
                record_move(data, memory, 30000, (4, 4))
                data["roundNo"] += 1
                data["lastRoundRoleActionResults"] = {"30000": False}
                data["errors"] = [occupied_error(30000, (4, 4))]
                decide(data, memory)
                data["roundNo"] += 1
                data["lastRoundRoleActionResults"] = {}
                data["errors"] = []
                if evidence == "ordinary_vision":
                    data["teamOur"]["roles"].append(unit(20, "worker", 4, 5))
                elif evidence == "arrival":
                    record_move(data, memory, 30000, (4, 4))
                    data["roundNo"] += 1
                    data["teamOur"]["summonRobotList"][0]["pos"] = {"x": 4, "y": 4}
                    data["lastRoundRoleActionResults"] = {"30000": True}
                decide(data, memory)
                if evidence == "robot_vision_only":
                    self.assertIn((4, 4), memory.occupied_steps)
                else:
                    self.assertNotIn((4, 4), memory.occupied_steps)
                    self.assertNotIn((4, 4), memory.occupied_failures)
                    self.assertNotIn((30000, (4, 4)), memory.failed_steps)
                    self.assertNotIn((30000, (4, 4)), memory.step_failures)

    def test_occupied_history_expires_and_clears_at_day_or_enemy_base_change(self):
        for change in ("expiry", "day", "base"):
            with self.subTest(change=change):
                data = assault_payload(robot=controlled_robot(x=3))
                memory = RobotAssaultMemory()
                record_move(data, memory, 30000, (4, 4))
                data["roundNo"] += 1
                data["lastRoundRoleActionResults"] = {"30000": False}
                data["errors"] = [occupied_error(30000, (4, 4))]
                decide(data, memory)
                data["roundNo"] = 75 if change == "expiry" else 200 if change == "day" else 72
                data["lastRoundRoleActionResults"] = {}
                data["errors"] = []
                if change == "base":
                    data["teamEnemy"]["roles"][0]["id"] = 100
                decide(data, memory)
                self.assertFalse(memory.occupied_steps)
                self.assertEqual(bool(memory.occupied_failures), change == "expiry")


if __name__ == "__main__":
    unittest.main()
