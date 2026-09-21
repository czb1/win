"""Economy deadlines, shared budgets, delivery and first-night readiness."""
import sys
import unittest

from test_agent import ROOT, payload, unit, setup_case
from agent.brain import Agent
from agent.config import Config
from agent.commands import command
from agent.economy import earn, supplies, workers
from agent.economy_plan import planned_weapons, preparation_start
from agent.intelligence import Memory

sys.path.insert(0, str(ROOT / "tools"))
from day_economy_benchmark import simulate
from run_checks import replay_test


class DayEconomyTests(unittest.TestCase):
    def case(self, rno=1, gold=75):
        p = payload(rno, [unit(1, "worker", 8, 7, health=220), unit(2, "worker", 8, 9, health=220),
                          unit(13, "station", 3, 11, health=1500)])
        p["teamOur"]["goldNum"] = gold
        p["mapInfo"]["zones"] = [{"neutralType": k, "pos": {"x": x, "y": y}} for k, x, y in
                                   (("copper", 9, 8), ("vendor", 10, 8), ("weaponShop", 10, 5), ("stone", 7, 6))]
        p["vendorShopList"] = [{"name": "copper", "price": 10}, {"name": "stone", "price": 1}]
        p["weaponShopList"] = [{"name": n, "price": v} for n, v in
                                  (("Medicine", 10), ("WeaponUpgradeVoucher1", 100),
                                   ("WeaponUpgradeVoucher2", 150), ("StationUpgradeVoucher1", 100))]
        return p

    def test_initial_gold_is_not_spent_before_farming(self):
        r = Agent(Config(llm_enabled=False)).decide(self.case())
        self.assertEqual([c["action"] for c in r["roleCommandMap"].values()], ["collect", "collect"])

    def test_no_permanent_builder_when_nothing_needs_work(self):
        p = self.case(45)
        p["teamOur"]["roles"] += [unit(20+i, "rocket", 5, 8+i, level=3) for i in range(3)]
        p["teamOur"]["roles"][2]["level"] = 3
        r = Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(p)
        self.assertEqual([c["action"] for c in r["roleCommandMap"].values()], ["collect", "collect"])

    def test_shop_batch_reserves_the_cost_of_unbuilt_weapons(self):
        p = self.case(41, 374)
        p["teamOur"]["roles"][0]["pos"] = {"x": 9, "y": 5}
        t, cfg, nav, ledger = setup_case(p, layout_mode="explicit", weapon_cells=[[5, 5], [5, 7], [5, 9]])
        mem = Memory()
        planned = planned_weapons(t, cfg, mem, [(5, 5), (5, 7), (5, 9)])
        plan = supplies(t, cfg, mem, nav, ledger, t.workers[0], 75, planned=planned, bulk=True)
        self.assertEqual((plan[0], plan[2]), ("WeaponUpgradeVoucher1", 2))
        workers(t, cfg, mem, nav, ledger, [(5, 5), (5, 7), (5, 9)], [])
        purchases = [c for c in ledger.commands.values() if c["action"] == "buy"]
        self.assertEqual(purchases, [{"action": "buy", "name": "WeaponUpgradeVoucher1", "num": 2}])
        self.assertGreaterEqual(ledger.gold, 75-cfg.weapon_cost)

    def test_carried_vouchers_prevent_overbuying(self):
        p = self.case(41, 400)
        p["teamOur"]["roles"] += [unit(20+i, "rocket", 5, 7+i) for i in range(3)]
        p["teamOur"]["roles"][1]["backpack"] = ["WeaponUpgradeVoucher1"] * 2
        t, cfg, nav, ledger = setup_case(p)
        plan = supplies(t, cfg, Memory(), nav, ledger, t.workers[0], bulk=True)
        self.assertEqual((plan[0], plan[2]), ("WeaponUpgradeVoucher1", 1))

    def test_next_level_can_share_the_same_shop_trip(self):
        p = self.case(41, 450)
        p["teamOur"]["roles"] += [unit(20+i, "rocket", 5, 7+i) for i in range(3)]
        p["teamOur"]["roles"][0]["backpack"] = ["WeaponUpgradeVoucher1"] * 3
        t, cfg, nav, ledger = setup_case(p)
        plan = supplies(t, cfg, Memory(), nav, ledger, t.workers[0], bulk=True)
        self.assertEqual((plan[0], plan[2]), ("WeaponUpgradeVoucher2", 3))

    def test_no_speculative_medicine_when_healthy(self):
        p = self.case(41, 10)
        p["teamOur"]["roles"] += [unit(20, "rocket", 5, 7)]
        t, cfg, nav, ledger = setup_case(p)
        self.assertIsNone(supplies(t, cfg, Memory(), nav, ledger, t.workers[0], bulk=True))

    def test_urgent_healing_is_not_delayed_to_round_40(self):
        p = self.case(5, 20)
        p["teamOur"]["roles"][0].update(health=50, pos={"x": 9, "y": 5})
        result = Agent(Config(llm_enabled=False)).decide(p)
        self.assertEqual(result["roleCommandMap"]["1"], command("buy", name="Medicine", num=1))

    def test_partial_ore_load_is_sold_at_preparation_deadline(self):
        p = self.case(40)
        p["teamOur"]["roles"][0].update(pos={"x": 9, "y": 7}, backpack=["copper"] * 3)
        t, cfg, nav, ledger = setup_case(p)
        earn(t, cfg, Memory(), nav, ledger, t.workers[0], deadline=40)
        self.assertEqual(ledger.commands["1"], command("sell", name="copper", num=3))

    def test_sale_trip_is_not_cancelled_when_a_mine_respawns(self):
        p = self.case(41)
        p["teamOur"]["roles"][0]["backpack"] = ["copper"]
        t, cfg, nav, ledger = setup_case(p)
        earn(t, cfg, Memory(sale_workers={1}), nav, ledger, t.workers[0], deadline=40)
        self.assertEqual(ledger.commands["1"]["action"], "move")

    def test_unreachable_shop_does_not_stop_income(self):
        p = self.case(41, 400)
        p["mapInfo"]["zones"] = [z for z in p["mapInfo"]["zones"] if z["neutralType"] != "weaponShop"]
        r = Agent(Config(layout_mode="explicit", llm_enabled=False)).decide(p)
        self.assertTrue(all(c["action"] == "collect" for c in r["roleCommandMap"].values()))

    def test_day_transition_resets_the_farming_window(self):
        p = self.case(131)
        t, cfg, nav, _ = setup_case(p)
        mem = Memory(day=1, preparation_tick=5, preparation_workers={1, 2})
        mem.observe(t, cfg)
        self.assertFalse(mem.preparation_workers)
        self.assertGreater(preparation_start(t, cfg, mem, nav, t.workers, []), 5)

    def test_single_worker_cannot_assume_parallel_construction(self):
        p = self.case()
        p["mapInfo"]["zones"][2]["pos"] = {"x": 14, "y": 0}
        t, cfg, nav, _ = setup_case(p)
        sites = [(5, 7), (5, 9), (5, 11)]
        parallel = preparation_start(t, cfg, Memory(), nav, t.workers, sites)
        solo = preparation_start(t, cfg, Memory(), nav, t.workers[:1], sites)
        self.assertLess(solo, parallel)


class OpeningReplayTests(unittest.TestCase):
    @replay_test
    def test_first_night_has_three_upgraded_operable_weapons(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                r = simulate(Agent, Config, profile="controlled", mirror=mirror)
                self.assertEqual(r["invalid_actions"], 0)
                self.assertEqual(r["checkpoints"]["40"]["spent"], 0)
                dusk = r["checkpoints"]["69"]
                self.assertEqual(dusk["weapon_levels"], [2, 2, 2])
                self.assertEqual(dusk["shared_guns_ready"], 3)
                self.assertEqual(dusk["carried_vouchers"], 0)
                self.assertEqual(dusk["front_walls"], 6)
                self.assertGreaterEqual(r["worker_actions_before_70"]["collect"], 40)
                self.assertLess(r["worker_actions_before_70"]["move"], 75)

    @replay_test
    def test_nearby_iron_is_used_and_first_defence_still_finishes(self):
        for mirror in (False, True):
            with self.subTest(mirror=mirror):
                r = simulate(Agent, Config, profile="controlled", case="local_ore", mirror=mirror)
                self.assertEqual(r["invalid_actions"], 0)
                self.assertGreater(r["mined_before_70"].get("iron", 0), 0)
                self.assertLess(r["worker_actions_before_70"]["move"], 70)
                dusk = r["checkpoints"]["69"]
                self.assertEqual(dusk["weapon_levels"], [2, 2, 2])
                self.assertEqual(dusk["shared_guns_ready"], 3)
                self.assertEqual(dusk["front_walls"], 6)

    @replay_test
    def test_long_shop_trip_starts_early_and_finishes_before_night(self):
        r = simulate(Agent, Config, profile="controlled", case="far_shop")
        self.assertEqual(r["invalid_actions"], 0)
        self.assertLess(r["first"]["buy_WeaponUpgradeVoucher1"], 40)
        self.assertEqual(r["checkpoints"]["69"]["weapon_levels"], [1, 1, 2])
        self.assertEqual(r["checkpoints"]["69"]["shared_guns_ready"], 3)
        self.assertEqual(r["checkpoints"]["69"]["carried_vouchers"], 0)

