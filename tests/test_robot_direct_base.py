"""Bounded base probes and clearance priority, using observed turn feedback."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_agent import setup_case, unit
from test_robot_assault import assault_payload, controlled_robot
from agent.commands import command
from agent.config import Config
from agent.model import distance, pos
from agent.robot_assault import RobotAssaultMemory, act_robots, enemy_base, _firing_point, _occupants


def blocked_case(kind="rocket"):
    data = assault_payload(robot=controlled_robot(x=7))
    data["teamEnemy"]["roles"].append(unit(50, kind, 8, 4, health=1000))
    return data


def decide(data, memory=None, **config):
    memory = memory if memory is not None else RobotAssaultMemory()
    turn, _, nav, ledger = setup_case(data, layout_mode="explicit", **config)
    act_robots(turn, memory, nav, ledger)
    return turn, ledger, memory


def advance(data, result=None, damage=0):
    data["roundNo"] += 1
    data["lastRoundRoleActionResults"] = {} if result is None else {"30000": result}
    data["teamEnemy"]["roles"][0]["health"] -= damage


def reject_base_aims(data, memory=None):
    """Observe explicit failures for every in-range aim before testing fallback."""
    turn, ledger, memory = decide(data, memory)
    base = enemy_base(turn)
    limit = len(base.cells)
    for _ in range(limit):
        if ledger.notes[30000]["conditions"]["target_id"] != base.id:
            break
        advance(data, False)
        turn, ledger, memory = decide(data, memory)
    return turn, ledger, memory


class DirectBaseTests(unittest.TestCase):
    def test_uncertain_building_and_character_rays_are_tried_before_movement(self):
        for kind in ("wall", "rocket", "gatling", "railgun", "worker", "pioneer", "imp"):
            with self.subTest(kind=kind):
                data = blocked_case(kind)
                original = copy.deepcopy(data)
                turn, ledger, memory = decide(data)
                self.assertEqual(ledger.commands["30000"], command("attack", (10, 4)))
                self.assertTrue(ledger.notes[30000]["conditions"]["uncertain_base_ray"])
                self.assertIsNone(_firing_point((7, 4), turn.summon_robots[0],
                                               enemy_base(turn), memory, _occupants(turn, memory)))
                self.assertEqual(data, original)

    def test_false_feedback_changes_aim_then_clears_before_an_available_detour(self):
        data = blocked_case()
        _, first, memory = decide(data)
        advance(data, False)
        _, second, memory = decide(data, memory)
        self.assertNotEqual(first.commands, second.commands)
        self.assertEqual(second.commands["30000"], command("attack", (10, 3)))
        advance(data, False)
        _, third, memory = decide(data, memory)
        self.assertEqual(third.commands["30000"], command("attack", (8, 4)))
        self.assertEqual(memory.focus_target_id, 50)
        advance(data, True)
        _, fourth, _ = decide(data, memory)
        self.assertEqual(fourth.commands["30000"], third.commands["30000"])

    def test_true_without_hp_progress_and_missing_feedback_are_bounded_separately(self):
        for result, damage in ((True, 0), (None, 0), (None, 1)):
            with self.subTest(result=result, damage=damage):
                data = blocked_case()
                _, _, memory = decide(data)
                for _ in range(4):  # Two visible base cells, two observations each.
                    advance(data, result, damage)
                    _, ledger, _ = decide(data, memory)
                self.assertEqual(ledger.commands["30000"], command("attack", (8, 4)))
                self.assertFalse(memory.failed_shots)  # No invented platform rejection.
                self.assertEqual(sum(p.paused for p in memory.base_shots.values()), 2)
                if result is None:
                    self.assertFalse(memory.accepted_shots)

    def test_aggregate_hp_progress_keeps_the_accepted_aim_without_damage_attribution(self):
        data = blocked_case()
        _, first, memory = decide(data)
        for _ in range(4):
            advance(data, True, damage=1)
            _, ledger, _ = decide(data, memory)
            self.assertEqual(ledger.commands["30000"], first.commands["30000"])
            self.assertFalse(any(p.paused for p in memory.base_shots.values()))

    def test_probe_window_is_configurable_and_rejects_invalid_values(self):
        data = blocked_case()
        _, first, memory = decide(data, robot_base_probe_rounds=1)
        advance(data, True)
        _, second, _ = decide(data, memory, robot_base_probe_rounds=1)
        self.assertNotEqual(first.commands, second.commands)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (0, -1, True, 1.5, "2"):
                path.write_text(json.dumps({"robot_base_probe_rounds": value}))
                with self.assertRaises(ValueError):
                    Config.load(path)
            path.write_text('{"robot_base_probe_rounds": 3}')
            self.assertEqual(Config.load(path).robot_base_probe_rounds, 3)

    def test_cleared_blocker_restores_base_shots_but_hp_changes_do_not_reset_failures(self):
        data = blocked_case("wall")
        _, _, memory = reject_base_aims(data)
        advance(data, True)
        data["teamEnemy"]["roles"][1]["health"] -= 40
        _, ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"], command("attack", (8, 4)))
        advance(data, True)
        data["teamEnemy"]["roles"][1]["health"] = 0
        _, ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"], command("attack", (10, 4)))
        self.assertFalse(memory.failed_shots)

    def test_paused_aims_restore_after_relevant_occupancy_changes(self):
        data = blocked_case("worker")
        _, _, memory = decide(data)
        for _ in range(4):
            advance(data, True)
            decide(data, memory)
        data["teamEnemy"]["roles"][1]["pos"] = {"x": 5, "y": 8}
        advance(data, True)
        _, ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands["30000"], command("attack", (10, 4)))
        self.assertFalse(any(p.paused for p in memory.base_shots.values()))

    def test_unattackable_obstacles_are_never_cleared_or_entered(self):
        for kind in ("ally", "public", "npc"):
            with self.subTest(kind=kind):
                data = assault_payload(robot=controlled_robot(x=7))
                if kind == "ally":
                    data["teamOur"]["roles"].append(unit(50, "worker", 8, 4))
                elif kind == "public":
                    data["robot"]["roles"] = [controlled_robot(31000, 8, 4)]
                else:
                    data["mapInfo"]["zones"] = [{"neutralType": "vendor", "pos": {"x": 8, "y": 4}}]
                turn, ledger, _ = reject_base_aims(data)
                self.assertEqual(ledger.commands["30000"]["action"], "move")
                self.assertNotIn(pos(ledger.commands["30000"]["targetPos"][0]), turn.blocked)
                self.assertNotIn("31000", ledger.commands)

    def test_base_attack_precedes_cooperative_yield(self):
        from test_robot_ingress import corner_aisle_case, frontage_case
        for data in (corner_aisle_case(), frontage_case()):
            with self.subTest(round=data["roundNo"]):
                turn, ledger, _ = decide(data)
                base = enemy_base(turn)
                for robot in turn.summon_robots:
                    if min(distance(robot.pos, p) for p in base.cells) <= robot.attack_range:
                        self.assertEqual(ledger.commands[str(robot.id)]["action"], "attack")
                        self.assertEqual(ledger.notes[robot.id]["conditions"]["target_id"], base.id)

    def test_day_dizzy_dead_and_missing_base_do_not_probe(self):
        for state in ("day", "dizzy", "dead", "missing"):
            data = blocked_case()
            if state == "day":
                data["roundNo"] = 69
            elif state == "dizzy":
                data["teamOur"]["summonRobotList"][0]["abnormalState"] = "dizzy"
            elif state == "dead":
                data["teamOur"]["summonRobotList"][0]["health"] = 0
            else:
                data["teamEnemy"]["roles"] = []
            _, ledger, _ = decide(data)
            self.assertFalse(ledger.commands, state)

    def test_stale_feedback_does_not_pause_or_reject_an_aim(self):
        data = blocked_case()
        _, first, memory = decide(data)
        data["roundNo"] += 2
        data["lastRoundRoleActionResults"] = {"30000": False}
        _, ledger, _ = decide(data, memory)
        self.assertEqual(ledger.commands, first.commands)
        self.assertFalse(memory.base_shots)
        self.assertFalse(memory.failed_shots)

    def test_probe_state_clears_on_day_base_or_robot_change(self):
        for change in ("day", "base", "dead"):
            data = blocked_case()
            _, _, memory = decide(data)
            advance(data, True)
            decide(data, memory, robot_base_probe_rounds=1)
            self.assertTrue(memory.base_shots)
            data["lastRoundRoleActionResults"] = {}
            if change == "day":
                data["roundNo"] = 130
            elif change == "base":
                data["teamEnemy"]["roles"][0]["id"] = 98
            else:
                data["teamOur"]["summonRobotList"] = []
            decide(data, memory)
            self.assertFalse(memory.base_shots)


if __name__ == "__main__":
    unittest.main()
