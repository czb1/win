"""Finite observed ingress states; no engine or damage simulation."""
import unittest

from test_agent import payload, setup_case, unit
from test_robot_assault import controlled_robot
from test_robot_ingress import decide
from test_robot_survival_memory import record_move
from agent.commands import command
from agent.model import pos
from agent.robot_assault import RobotAssaultMemory, enemy_base


def gap_case(round_no, positions):
    # Relevant geometry from the supplied r71-r80 logs. The unbuilt corner
    # (28,7) leads to the free inner stance (29,8).
    data = payload(round_no, [unit(10013, "station", 9, 22),
                             unit(10010, "worker", 29, 6)])
    data["mapInfo"].update(width=41, height=32)
    data["teamEnemy"]["roles"] = [unit(20013, "station", 30, 10, health=1500),
                                   unit(20042, "rocket", 32, 10, health=1000)]
    walls = [(28, 12), (29, 12), (31, 12), (32, 12), (33, 12), (32, 7),
             (33, 11), (31, 7), (33, 10), (30, 7), (33, 9), (29, 7),
             (33, 8), (33, 7), (28, 8), (28, 9), (28, 10), (28, 11)]
    data["teamEnemy"]["roles"] += [unit(41000 + i, "wall", *p, health=1000)
                                    for i, p in enumerate(walls)]
    data["teamOur"]["summonRobotList"] = [
        controlled_robot(uid, *p) for uid, p in zip((30035, 30036, 30037), positions)]
    return data


class RobotYieldProgressTests(unittest.TestCase):
    def waiting_yield(self):
        data = gap_case(77, ((31, 8), (29, 8), (26, 8)))
        memory = RobotAssaultMemory(day=1, base_id=20013, base_pos=(30, 10),
                                   approach_best={30037: 3}, approach_stalls={30037: 3})
        _, ledger, memory = decide(data, memory, entry=(28, 7))
        self.assertEqual(ledger.commands["30036"], command("move", (30, 8)))
        self.assertEqual(ledger.notes[30037]["reason"], "robot_wait_assault_lane")
        self.assertEqual(memory.approach_stalls[30037], 3)
        return data, memory

    def observe(self, data, memory):
        turn, _, _, _ = setup_case(data, layout_mode="explicit")
        memory.observe(turn, enemy_base(turn))
        return turn

    def test_backtracking_follower_enters_after_observed_yields(self):
        memory = None
        # These are explicit observations, not predicted move outcomes. The
        # last four states assert the desired recovery after the supplied r77.
        frames = [
            (71, ((28, 6), (27, 5), (27, 7))),
            (72, ((28, 7), (26, 6), (26, 8))),
            (73, ((29, 8), (26, 6), (26, 8))),
            (74, ((30, 8), (26, 6), (26, 8))),
            (75, ((30, 8), (27, 6), (27, 7))),
            (76, ((30, 8), (28, 7), (26, 8))),
            (77, ((31, 8), (29, 8), (26, 8))),
            (78, ((31, 8), (30, 8), (26, 8))),
            (79, ((31, 8), (30, 8), (27, 7))),
            (80, ((31, 8), (30, 8), (28, 7))),
            (81, ((31, 8), (30, 8), (29, 8))),
        ]
        for round_no, positions in frames:
            with self.subTest(round_no=round_no):
                _, ledger, memory = decide(gap_case(round_no, positions), memory, entry=(28, 7))
                if round_no in (72, 73, 76, 77):
                    self.assertEqual(ledger.notes[30037]["reason"], "robot_wait_assault_lane")
                    self.assertNotIn("30037", ledger.commands)
                if round_no in (78, 79, 80):
                    destination = {78: (27, 7), 79: (28, 7), 80: (29, 8)}[round_no]
                    self.assertEqual(ledger.commands["30037"], command("move", destination))
                    self.assertEqual(ledger.notes[30037]["reason"], "robot_approach_base")
                if round_no == 81:
                    for uid in (30035, 30036, 30037):
                        self.assertEqual(ledger.commands[str(uid)]["action"], "attack")
                        self.assertEqual(ledger.notes[uid]["conditions"]["target_id"], 20013)

    def test_confirmed_yield_rebases_only_its_waiters_once(self):
        for result in (True, False, None):
            with self.subTest(result=result):
                data, memory = self.waiting_yield()
                data["roundNo"] = 78
                data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 30, "y": 8}
                if result is not None:
                    data["lastRoundRoleActionResults"] = {"30036": result}
                memory.approach_stalls[30035] = 2
                memory.failed_steps[30037, (27, 9)] = 82
                memory.failed_base_lanes[(28, 8)] = 82
                self.observe(data, memory)
                self.assertNotIn(30037, memory.approach_stalls)
                self.assertEqual(memory.approach_best[30037], 4)
                self.assertEqual(memory.approach_stalls[30035], 2)
                self.assertIn((27, 9), memory.avoided_steps(30037))
                self.assertIn((28, 8), memory.failed_base_lanes)
                self.assertFalse(memory.yield_waiters)
                # Re-reading this observation cannot erase a newer failure.
                memory.approach_stalls[30037] = 2
                self.observe(data, memory)
                self.assertEqual(memory.approach_stalls[30037], 2)

    def test_failed_or_reoccupied_yield_does_not_reset_progress(self):
        for change in ("legal_no_move", "illegal_no_move", "missing_no_move",
                       "different_destination", "old_cell_reoccupied"):
            with self.subTest(change=change):
                data, memory = self.waiting_yield()
                data["roundNo"] = 78
                if change != "missing_no_move":
                    data["lastRoundRoleActionResults"] = {"30036": change != "illegal_no_move"}
                if change == "different_destination":
                    data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 29, "y": 9}
                elif change == "old_cell_reoccupied":
                    data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 30, "y": 8}
                    data["teamOur"]["roles"].append(unit(10012, "worker", 29, 8))
                self.observe(data, memory)
                self.assertEqual(memory.approach_stalls[30037], 3)
                self.assertEqual(memory.approach_best[30037], 3)
                self.assertFalse(memory.yield_waiters)

    def test_skipped_round_or_lost_mover_cannot_confirm_a_yield(self):
        for change in ("skipped_round", "lost_mover"):
            with self.subTest(change=change):
                data, memory = self.waiting_yield()
                data["roundNo"] = 79 if change == "skipped_round" else 78
                data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 30, "y": 8}
                if change == "lost_mover":
                    data["teamOur"]["summonRobotList"].pop(1)
                self.observe(data, memory)
                self.assertEqual(memory.approach_stalls[30037], 3)
                self.assertFalse(memory.yield_waiters)

    def test_changed_day_base_or_lost_waiter_discards_yield_history(self):
        for change in ("day", "base", "lost_waiter"):
            with self.subTest(change=change):
                data, memory = self.waiting_yield()
                data["roundNo"] = 200 if change == "day" else 78
                data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 30, "y": 8}
                if change == "base":
                    data["teamEnemy"]["roles"][0]["pos"] = {"x": 20, "y": 10}
                elif change == "lost_waiter":
                    data["teamOur"]["summonRobotList"].pop()
                self.observe(data, memory)
                self.assertFalse(memory.yield_waiters)
                self.assertNotIn(30037, memory.approach_stalls)

    def test_replacing_the_move_discards_its_yield_waiters(self):
        data, memory = self.waiting_yield()
        turn = self.observe(data, memory)
        self.assertTrue(memory.yield_waiters)
        memory.remember(turn, turn.summon_robots[1], "move", (29, 9))
        data["roundNo"] += 1
        data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 29, "y": 9}
        self.observe(data, memory)
        self.assertFalse(memory.yield_waiters)
        self.assertEqual(memory.approach_stalls[30037], 3)

    def test_observed_yield_recovery_is_symmetric_and_not_id_ordered(self):
        for flip_x, flip_y in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(flip_x=flip_x, flip_y=flip_y):
                def reflected(point, station=False):
                    x, y = point
                    return (40 - x - int(station) if flip_x else x,
                            31 - y + int(station) if flip_y else y)

                data = gap_case(77, ((31, 8), (29, 8), (26, 8)))
                for key in ("teamOur", "teamEnemy"):
                    for role in data[key]["roles"]:
                        x, y = reflected(pos(role["pos"]), role["roleType"] == "station")
                        role["pos"] = {"x": x, "y": y}
                robots = data["teamOur"]["summonRobotList"]
                for uid, robot in zip((33039, 33038, 33035), robots):
                    x, y = reflected(pos(robot["pos"]))
                    robot.update(id=uid, pos={"x": x, "y": y})
                memory = RobotAssaultMemory(day=1, base_id=20013,
                    base_pos=reflected((30, 10), True),
                    approach_best={33035: 3}, approach_stalls={33035: 3})
                _, ledger, memory = decide(data, memory, entry=reflected((28, 7)))
                self.assertEqual(ledger.notes[33038]["reason"], "robot_make_room_inside")
                data["roundNo"] += 1
                x, y = pos(ledger.commands["33038"]["targetPos"][0])
                robots[1]["pos"] = {"x": x, "y": y}
                turn, ledger, memory = decide(data, memory, entry=reflected((28, 7)))
                self.assertEqual(ledger.commands["33035"]["action"], "move")
                self.assertEqual(ledger.notes[33035]["reason"], "robot_approach_base")
                self.assertNotIn(pos(ledger.commands["33035"]["targetPos"][0]), turn.blocked)
                self.assertIsNone(memory.focus_target_id)

    def test_new_backtracking_after_a_yield_still_triggers_clearance(self):
        data, memory = self.waiting_yield()
        data["roundNo"] += 1
        data["teamOur"]["summonRobotList"][1]["pos"] = {"x": 30, "y": 8}
        self.observe(data, memory)
        for destination in ((27, 7), (26, 8), (27, 7), (26, 8)):
            record_move(data, memory, 30037, destination)
            data["roundNo"] += 1
            data["teamOur"]["summonRobotList"][2]["pos"] = {
                "x": destination[0], "y": destination[1]}
            self.observe(data, memory)
        _, ledger, _ = decide(data, memory, entry=(28, 7))
        self.assertEqual(ledger.commands["30037"], command("attack", (28, 8)))
        self.assertEqual(ledger.notes[30037]["conditions"]["assault_choice"], "shared_clearance")


if __name__ == "__main__":
    unittest.main()
