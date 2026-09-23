"""Unreachable defences and safe collection without a same-day sale."""
import copy
import unittest

from test_agent import unit, setup_case
from test_mining_runs import mining_case
from agent.brain import Agent
from agent.combat import assignments
from agent.config import Config
from agent.commands import command
from agent.intelligence import Memory
from agent.mining import earn, spare_mine
from agent.model import distance, neighbours


def blocked_tower_case(second_tower=True):
    p = mining_case(rno=1, workers=[unit(1, "worker", 5, 5), unit(2, "worker", 5, 7)])
    p["teamOur"]["roles"] += [unit(13, "station", 3, 10), unit(20, "rocket", 12, 12)]
    p["teamOur"]["roles"] += [unit(100+i, "wall", x, y)
                               for i, (x, y) in enumerate(neighbours((12, 12)))]
    if second_tower:
        p["teamOur"]["roles"].append(unit(21, "rocket", 9, 5))
    return p


def late_case(rno=60, inventory=(), ore=(6, 5)):
    p = mining_case(rno, inventory, zones=[("iron", *ore), ("vendor", 30, 5)])
    p["mapInfo"].update(width=41, height=32)
    p["teamOur"]["roles"].append(unit(20, "rocket", 4, 8, level=3))
    return p


class UnreachableDefenceTests(unittest.TestCase):
    def test_only_reachable_towers_get_operators(self):
        t, _, nav, ledger = setup_case(blocked_tower_case())
        pairs = assignments(t, nav, ledger)
        self.assertEqual([w.id for _, w in pairs], [21])

    def test_unreachable_assignment_does_not_idle_adjacent_miner(self):
        result = Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(blocked_tower_case())
        self.assertEqual(result["roleCommandMap"]["1"], command("collect", (6, 5)))
        self.assertIn("2", result["roleCommandMap"])

    def test_all_guns_blocked_still_allows_mining_with_base_return(self):
        p = blocked_tower_case(second_tower=False)
        t, _, nav, ledger = setup_case(p)
        self.assertEqual(assignments(t, nav, ledger), [])
        result = Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(p)
        self.assertEqual(result["roleCommandMap"]["1"], command("collect", (6, 5)))

    def test_fixed_assignment_is_released_when_wall_blocks_the_gun(self):
        t, _, nav, ledger = setup_case(blocked_tower_case())
        pairs = assignments(t, nav, ledger, fixed={1: 20})
        self.assertEqual([w.id for _, w in pairs], [21])


class SpareMiningTests(unittest.TestCase):
    def test_spare_collection_stops_at_tick_40(self):
        for tick in (39, 40, 60):
            with self.subTest(tick=tick):
                t, cfg, nav, ledger = setup_case(late_case(rno=tick))
                self.assertEqual(earn(t, cfg, Memory(), nav, ledger, t.workers[0]), tick < 40)
                if tick < 40:
                    self.assertEqual(ledger.commands["1"], command("collect", (6, 5)))
                else:
                    self.assertFalse(ledger.commands)

    def test_carried_ore_does_not_force_an_impossible_sale(self):
        t, cfg, nav, ledger = setup_case(late_case(inventory=["iron"] * 40))
        self.assertTrue(earn(t, cfg, Memory(sale_workers={1}), nav, ledger, t.workers[0]))
        self.assertEqual(ledger.commands["1"], command("move", (4, 6)))

    def test_failed_liquidation_never_falls_back_to_collecting(self):
        for tick in (39, 40, 60):
            with self.subTest(tick=tick):
                t, cfg, nav, ledger = setup_case(late_case(rno=tick, inventory=["iron"]))
                self.assertFalse(earn(t, cfg, Memory(), nav, ledger, t.workers[0],
                                      force_sale=True, allow_spare=False))
                self.assertFalse(ledger.commands)

    def test_wait_before_preparation_does_not_start_an_extra_mining_trip(self):
        t, cfg, nav, ledger = setup_case(late_case(rno=39))
        self.assertFalse(earn(t, cfg, Memory(), nav, ledger, t.workers[0], deadline=40))
        self.assertFalse(ledger.commands)

    def test_two_step_mine_is_allowed_but_long_trip_is_not(self):
        for target, expected in (((8, 5), True), ((10, 5), False)):
            with self.subTest(target=target):
                t, cfg, nav, ledger = setup_case(late_case(rno=55, ore=target))
                self.assertEqual(spare_mine(t, cfg, Memory(), nav, ledger, t.workers[0]), expected)
                if expected:
                    self.assertEqual(ledger.commands["1"]["action"], "move")

    def test_spare_collection_respects_return_margin(self):
        for rno, expected in ((62, True), (63, False)):
            with self.subTest(rno=rno):
                t, cfg, nav, ledger = setup_case(late_case(rno=rno))
                self.assertEqual(spare_mine(t, cfg, Memory(), nav, ledger, t.workers[0]), expected)

    def test_assigned_gun_controls_return_budget(self):
        t, cfg, nav, ledger = setup_case(late_case())
        ledger.operator_posts[1] = (14, 14)
        self.assertFalse(spare_mine(t, cfg, Memory(), nav, ledger, t.workers[0]))

    def test_no_spare_mining_when_full_failed_or_night(self):
        for case in ("full", "failed", "night"):
            with self.subTest(case=case):
                p = late_case(rno=70 if case == "night" else 60,
                              inventory=["iron"] * 100 if case == "full" else [])
                t, cfg, nav, ledger = setup_case(p)
                mem = Memory(collect_failures={(6, 5): 65} if case == "failed" else {})
                self.assertFalse(spare_mine(t, cfg, mem, nav, ledger, t.workers[0]))

    def test_no_collection_without_a_known_return_destination(self):
        t, cfg, nav, ledger = setup_case(mining_case())
        self.assertFalse(spare_mine(t, cfg, Memory(), nav, ledger, t.workers[0]))

    def test_spare_mining_does_not_revisit_a_cyclic_movement_target(self):
        t, cfg, nav, ledger = setup_case(late_case())
        mem = Memory()
        mem.movement.targets[1, (6, 5)] = 68
        self.assertFalse(spare_mine(t, cfg, mem, nav, ledger, t.workers[0]))

    def test_spare_return_respects_observed_blocked_cells(self):
        t, cfg, nav, ledger = setup_case(late_case())
        mem = Memory()
        mem.movement.failures = {(1, p): 68 for p in neighbours((4, 8))}
        nav.memory = mem.movement
        self.assertFalse(spare_mine(t, cfg, mem, nav, ledger, t.workers[0]))

    def test_carry_ore_home_before_night_and_sell_next_day(self):
        p = late_case(inventory=["iron"] * 2)
        cfg = Config(layout_mode="explicit", llm_enabled=False)
        agent = Agent(cfg)
        actions = []
        hero = p["teamOur"]["roles"][0]
        for rno in range(60, 70):
            p["roundNo"] = rno
            result = agent.decide(copy.deepcopy(p))
            cmd = result["roleCommandMap"].get("1", {})
            actions.append(cmd.get("action"))
            if cmd.get("action") == "collect":
                hero["backpack"].append("iron")
            elif cmd.get("action") == "move":
                hero["pos"] = cmd["targetPos"][0].copy()
            p["lastRoundRoleActionResults"] = {"1": bool(cmd)}
        self.assertNotIn("collect", actions)
        self.assertEqual(hero["backpack"], ["iron"] * 2)
        self.assertNotIn("sell", actions)
        self.assertEqual(distance((hero["pos"]["x"], hero["pos"]["y"]), (4, 8)), 1)
        # The next day's mining phase preserves the saved load even beside
        # a vendor; liquidation resumes after the inclusive mining cutoff.
        p["roundNo"] = 130
        p["mapInfo"]["zones"] = [{"neutralType": "vendor", "pos": {"x": 4, "y": 8}}]
        p["teamOur"]["roles"] = [hero]
        result = agent.decide(p)
        self.assertNotIn("1", result["roleCommandMap"])
        p["roundNo"] = 171
        result = agent.decide(p)
        self.assertEqual(result["roleCommandMap"]["1"],
                         command("sell", name="iron", num=len(hero["backpack"])))


if __name__ == "__main__":
    unittest.main()
