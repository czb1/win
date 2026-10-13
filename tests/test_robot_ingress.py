"""Finite frontage and observation checks for cooperative base ingress."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import controlled_robot
from agent.commands import command
from agent.model import distance, pos
from agent.projectiles import line_cells
from agent.robot_assault import RobotAssaultMemory, act_robots


def frontage_case(round_no=102):
    # Local geometry from the supplied round-102 snapshot: the worker at
    # (32,8) has died, but the lead at (31,8) still seals the inner aisle.
    data = payload(round_no, [unit(10013, "station", 9, 22)])
    data["mapInfo"].update(width=41, height=32)
    data["teamEnemy"]["roles"] = [
        unit(20013, "station", 30, 10, health=420),
        unit(20011, "pioneer", 32, 10),
        unit(20040, "rocket", 32, 9),
        unit(20041, "rocket", 32, 11),
        unit(20042, "rocket", 31, 11),
    ]
    walls = [(28, 9), (28, 8), (28, 7), (29, 7), (30, 7),
             (28, 10), (28, 11), (28, 12), (29, 12), (30, 12),
             (31, 12), (31, 7), (32, 7), (32, 12), (33, 12), (33, 9)]
    data["teamEnemy"]["roles"] += [
        unit(41000 + i, "wall", *point, health=1000)
        for i, point in enumerate(walls)
    ]
    data["teamOur"]["summonRobotList"] = [
        controlled_robot(30035, 31, 8), controlled_robot(30036, 33, 8),
        controlled_robot(30037, 34, 6), controlled_robot(30038, 33, 7),
    ]
    return data


def decide(data, memory=None, reserved=(), entry=None):
    memory = memory if memory is not None else RobotAssaultMemory()
    turn, _, nav, ledger = setup_case(data, layout_mode="explicit")
    ledger.reserved.update(reserved)
    ledger.robot_assault_entry = entry
    act_robots(turn, memory, nav, ledger)
    return turn, ledger, memory


def corner_aisle_case(blocker_kind='worker'):
    # Variant of r78 with an opaque worker in place of the north-row turret:
    # keep exercising cooperative yields after turrets became penetrable.
    data = payload(78, [unit(10013, "station", 9, 22),
                        unit(10010, "worker", 27, 11)])
    data["mapInfo"].update(width=41, height=32)
    data["teamEnemy"]["roles"] = [
        unit(20013, "station", 30, 10, health=1420),
        unit(20041, blocker_kind, 30, 11, health=500),
        unit(20011, "pioneer", 31, 11),
    ]
    walls = [(x, y) for x in range(28, 34) for y in range(7, 13)
             if (x in (28, 33) or y in (7, 12)) and (x, y) not in {(28, 12), (30, 12)}]
    data["teamEnemy"]["roles"] += [
        unit(41000 + i, "wall", *point, health=1000)
        for i, point in enumerate(walls)
    ]
    data["teamOur"]["summonRobotList"] = [
        controlled_robot(30035, 29, 8), controlled_robot(30036, 28, 12),
        controlled_robot(30037, 29, 10),
    ]
    return data


class RobotIngressTests(unittest.TestCase):
    def test_original_corner_turret_no_longer_forces_cooperative_yield(self):
        _, ledger, _ = decide(corner_aisle_case(blocker_kind='rocket'), entry=(28, 12))
        for uid in (30035, 30037):
            self.assertEqual(ledger.commands[str(uid)]['action'], 'attack')
            self.assertEqual(ledger.notes[uid]['conditions']['target_id'], 20013)
        self.assertEqual(ledger.commands['30036'], command('move', (29, 11)))
        self.assertEqual(ledger.notes[30036]['conditions']['target_id'], 20013)

    def test_corner_shooter_steps_sideways_to_release_the_aisle(self):
        for entry in (None, (28, 12)):
            with self.subTest(entry=entry):
                data = corner_aisle_case()
                original = copy.deepcopy(data)
                turn, ledger, _ = decide(data, entry=entry)
                self.assertEqual(ledger.commands["30035"], command("move", (30, 8)))
                self.assertEqual(ledger.commands["30037"], command("attack", (30, 10)))
                self.assertEqual(ledger.notes[30036]["reason"], "robot_wait_assault_lane")
                self.assertNotIn("30036", ledger.commands)
                self.assertEqual(data, original)
                self.assertIn((29, 8), turn.blocked)
                self.assertIn((30, 8), ledger.reserved)

    def test_opening_the_waiting_route_precedes_unrelated_deeper_moves(self):
        data = corner_aisle_case()
        data["roundNo"] = 79
        data["teamOur"]["summonRobotList"][0]["pos"] = {"x": 30, "y": 8}
        _, ledger, _ = decide(data, entry=(28, 12))
        self.assertEqual(ledger.commands["30037"], command("move", (29, 9)))
        self.assertEqual(ledger.commands["30035"], command("attack", (30, 9)))
        self.assertEqual(ledger.notes[30036]["reason"], "robot_wait_assault_lane")

    def test_observed_clear_aisle_lets_the_last_robot_approach_the_base(self):
        data = corner_aisle_case()
        data["teamOur"]["summonRobotList"][0]["pos"] = {"x": 30, "y": 8}
        data["teamOur"]["summonRobotList"][2]["pos"] = {"x": 29, "y": 9}
        _, ledger, _ = decide(data, entry=(28, 12))
        self.assertEqual(ledger.commands["30036"], command("move", (29, 11)))
        for uid in (30035, 30037):
            self.assertEqual(ledger.commands[str(uid)]["action"], "attack")
        data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 29, "y": 11}
        _, ledger, _ = decide(data, entry=(28, 12))
        for uid in (30035, 30036, 30037):
            self.assertEqual(ledger.commands[str(uid)]["action"], "attack")
            self.assertEqual(ledger.notes[uid]["conditions"]["target_id"], 20013)

    def test_corner_yield_does_not_use_a_reserved_or_occupied_destination(self):
        data = corner_aisle_case()
        _, ledger, _ = decide(data, reserved={(30, 8)}, entry=(28, 12))
        self.assertEqual(ledger.commands["30035"]["action"], "attack")
        data["teamOur"]["roles"].append(unit(10012, "worker", 30, 8))
        _, ledger, _ = decide(data, entry=(28, 12))
        self.assertEqual(ledger.commands["30035"]["action"], "attack")

    def test_failed_corner_yield_does_not_admit_the_waiting_robot(self):
        data = corner_aisle_case()
        _, first, memory = decide(data, entry=(28, 12))
        self.assertEqual(first.commands["30035"], command("move", (30, 8)))
        data["roundNo"] += 1
        # A legal command alone is not evidence that its move succeeded.
        data["lastRoundRoleActionResults"] = {"30035": True}
        turn, ledger, memory = decide(data, memory, entry=(28, 12))
        self.assertIn((30, 8), memory.avoided_steps(30035))
        self.assertIn((29, 8), turn.blocked)
        self.assertEqual(ledger.commands["30035"]["action"], "attack")
        self.assertNotEqual(ledger.commands.get("30036"), command("move", (29, 11)))

    def test_corner_yield_is_symmetric_and_independent_of_robot_ids(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = corner_aisle_case()
                data["teamOur"]["summonRobotList"][0]["id"] = 30039
                for roles in (data["teamOur"]["roles"], data["teamEnemy"]["roles"],
                              data["teamOur"]["summonRobotList"]):
                    for role in roles:
                        x, y = pos(role["pos"])
                        station = role["roleType"] == "station"
                        role["pos"] = {"x": 40 - x - int(station) if flip_x else x,
                                       "y": 31 - y + int(station) if flip_y else y}
                expected = (40 - 30 if flip_x else 30, 31 - 8 if flip_y else 8)
                _, ledger, _ = decide(data)
                self.assertEqual(ledger.commands["30039"], command("move", expected))

    def test_lead_clears_inner_aisle_after_leaving_the_entry(self):
        for entry in (None, (33, 7)):
            for opposite_gap in (False, True):
                with self.subTest(entry=entry, opposite_gap=opposite_gap):
                    data = frontage_case()
                    if opposite_gap:
                        data["teamEnemy"]["roles"] = [r for r in data["teamEnemy"]["roles"]
                                                        if r["id"] != 41000]
                    _, ledger, _ = decide(data, entry=entry)
                    self.assertEqual(ledger.commands["30035"], command("move", (30, 8)))
                    self.assertEqual(ledger.notes[30035]["reason"], "robot_make_room_inside")

    def test_clearing_keeps_current_occupancy_and_input_intact(self):
        data = frontage_case()
        original = copy.deepcopy(data)
        turn, ledger, memory = decide(data)
        self.assertEqual(data, original)
        self.assertIn((31, 8), turn.blocked)
        self.assertIn((30, 8), ledger.reserved)
        self.assertEqual(memory.pending[30035][2:4], ("move", (30, 8)))
        for action in ledger.commands.values():
            if action["action"] == "move":
                self.assertNotEqual(pos(action["targetPos"][0]), (31, 8))
        self.assertEqual(ledger.notes[30036]["reason"], "robot_attack_base")

    def test_lead_continues_deeper_when_the_queue_still_needs_space(self):
        data = frontage_case(103)
        data["teamOur"]["summonRobotList"][0]["pos"] = {"x": 30, "y": 8}
        _, ledger, _ = decide(data)
        self.assertEqual(ledger.commands["30035"]["action"], "move")
        step = pos(ledger.commands["30035"]["targetPos"][0])
        self.assertIn(step, {(29, 8), (29, 9)})
        self.assertEqual(distance((30, 8), step), 1)

    def test_enemy_worker_is_not_speculatively_removed_for_ingress(self):
        data = frontage_case(90)
        data["teamEnemy"]["roles"].append(unit(20010, "worker", 32, 8))
        _, ledger, _ = decide(data)
        self.assertEqual(ledger.commands["30035"], command("attack", (31, 9)))
        self.assertEqual(ledger.notes[30036]["conditions"]["target_id"], 20010)

    def test_single_robot_keeps_attacking(self):
        data = frontage_case()
        data["teamOur"]["summonRobotList"] = data["teamOur"]["summonRobotList"][:1]
        _, ledger, _ = decide(data)
        self.assertEqual(ledger.commands["30035"], command("attack", (31, 9)))

    def test_all_four_clear_shooters_keep_their_positions(self):
        data = frontage_case()
        for robot, point in zip(data["teamOur"]["summonRobotList"],
                                ((29, 10), (29, 9), (30, 8), (31, 8))):
            robot["pos"] = {"x": point[0], "y": point[1]}
        _, ledger, _ = decide(data)
        self.assertEqual(len(ledger.commands), 4)
        for uid in (30035, 30036, 30037, 30038):
            self.assertEqual(ledger.commands[str(uid)]["action"], "attack")
            self.assertEqual(ledger.notes[uid]["conditions"]["target_id"], 20013)

    def test_failed_yield_uses_observed_coordinates_and_avoids_retry(self):
        data = frontage_case()
        _, first, memory = decide(data)
        self.assertEqual(first.commands["30035"], command("move", (30, 8)))
        data["roundNo"] += 1
        data["lastRoundRoleActionResults"] = {"30035": False}
        turn, ledger, memory = decide(data, memory)
        self.assertIn((30, 8), memory.avoided_steps(30035))
        self.assertIn((31, 8), turn.blocked)
        self.assertEqual(ledger.commands["30035"]["action"], "attack")
        for action in ledger.commands.values():
            if action["action"] == "move":
                self.assertNotEqual(pos(action["targetPos"][0]), (31, 8))

    def test_reserved_or_known_occupied_destination_is_not_used(self):
        data = frontage_case()
        _, ledger, _ = decide(data, reserved={(30, 8)})
        self.assertEqual(ledger.commands["30035"]["action"], "attack")
        data["teamOur"]["roles"].append(unit(10010, "worker", 30, 8))
        _, ledger, _ = decide(data)
        self.assertEqual(ledger.commands["30035"]["action"], "attack")

    def test_dizzy_or_public_robot_cannot_be_a_cooperative_mover(self):
        for public in (False, True):
            with self.subTest(public=public):
                data = frontage_case()
                robot = data["teamOur"]["summonRobotList"][0]
                if public:
                    data["teamOur"]["summonRobotList"].remove(robot)
                    data["robot"]["roles"].append(robot)
                else:
                    robot["abnormalState"] = "dizzy"
                _, ledger, _ = decide(data)
                self.assertNotIn("30035", ledger.commands)
                self.assertFalse(any(n["reason"] == "robot_make_room_inside"
                                     for n in ledger.notes.values()))

    def test_clearing_preserves_another_robots_established_shot(self):
        data = frontage_case()
        data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 29, "y": 8}
        _, ledger, _ = decide(data)
        # The corner shooter now yields; the nonmoving lead's ray remains
        # protected, rather than requiring a particular robot to be the mover.
        self.assertEqual(ledger.notes[30036]["reason"], "robot_make_room_inside")
        action = ledger.commands["30035"]
        self.assertEqual(action["action"], "attack")
        ray = line_cells((31, 8), pos(action["targetPos"][0]))
        for uid, action in ledger.commands.items():
            if uid != "30035" and action["action"] == "move":
                self.assertNotIn(pos(action["targetPos"][0]), ray)

    def test_leading_position_is_used_instead_of_robot_id_order(self):
        data = frontage_case()
        data["teamOur"]["summonRobotList"][0]["id"] = 30039
        _, ledger, _ = decide(data)
        self.assertEqual(ledger.commands["30039"], command("move", (30, 8)))

    def test_reflected_frontage_uses_the_same_cooperative_rule(self):
        for flip_x, flip_y in ((True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                data = frontage_case()
                for roles in (data["teamOur"]["roles"], data["teamEnemy"]["roles"],
                              data["teamOur"]["summonRobotList"]):
                    for role in roles:
                        x, y = pos(role["pos"])
                        station = role["roleType"] == "station"
                        role["pos"] = {"x": 40 - x - int(station) if flip_x else x,
                                       "y": 31 - y + int(station) if flip_y else y}
                _, ledger, _ = decide(data)
                expected = (40 - 30 if flip_x else 30, 31 - 8 if flip_y else 8)
                self.assertEqual(ledger.commands["30035"], command("move", expected))

    def test_daytime_does_not_rearrange_robots(self):
        _, ledger, _ = decide(frontage_case(69))
        self.assertFalse(ledger.commands)


if __name__ == "__main__":
    unittest.main()
