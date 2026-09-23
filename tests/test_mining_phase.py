"""Day-local, inclusive worker mining policy and independent courier budgets."""
import unittest

from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import supplies, workers
from agent.intelligence import Memory


class MiningPhaseTests(unittest.TestCase):
    def case(self, round_no):
        p = payload(round_no, [
            unit(1, "worker", 5, 5, health=50,
                 backpack=["Medicine", "WallFixer", "WallUpgradeVoucher1", "stone", "copper"]),
            unit(2, "worker", 5, 7, backpack=["WeaponUpgradeVoucher1", "copper"]),
            unit(13, "station", 1, 6, health=1500),
            unit(20, "rocket", 4, 7),
            unit(30, "wall", 7, 5, health=300),
        ])
        p["teamOur"]["goldNum"] = 600
        p["mapInfo"]["zones"] = [
            {"neutralType": kind, "pos": {"x": x, "y": y}}
            for kind, x, y in (("copper", 6, 6), ("vendor", 4, 5), ("weaponShop", 6, 5))]
        p["vendorShopList"] = [{"name": "copper", "price": 5}]
        p["weaponShopList"] = [{"name": name, "price": 20} for name in
                               ("Medicine", "WallUpgradeVoucher1", "WeaponUpgradeVoucher1")]
        return p

    def config(self, **kw):
        return Config(layout_mode="explicit", weapon_cells=[[4, 7]],
                      wall_cells=[[7, 5], [7, 6]], loadout=["rocket"],
                      llm_enabled=False, **kw)

    def test_both_workers_only_collect_through_40_every_day_and_origin(self):
        for origin in (0, 1):
            for day in (1, 4):
                for tick in (0, 5, 39, 40):
                    with self.subTest(origin=origin, day=day, tick=tick):
                        p = self.case(origin + (day - 1) * 130 + tick)
                        commands = Agent(self.config(round_origin=origin)).decide(p)["roleCommandMap"]
                        self.assertEqual(commands, {"1": command("collect", (6, 6)),
                                                    "2": command("collect", (6, 6))})

    def test_early_dispatch_ignores_old_sale_supply_and_repair_assignments(self):
        p = self.case(40)
        t, c, n, l = setup_case(p)
        mem = Memory(preparation_tick=0, preparation_workers={1, 2},
                     sold_workers={1, 2}, sale_workers={1, 2}, supply_worker=1,
                     wall_repair_worker=2, wall_repair_delivering=True,
                     wall_rebuild_levels={(7, 5): 3})
        workers(t, c, mem, n, l, [(4, 7)], [(7, 5), (7, 6)])
        self.assertEqual(l.commands, {"1": command("collect", (6, 6)),
                                     "2": command("collect", (6, 6))})

    def test_full_or_missing_mine_never_falls_through_to_other_jobs(self):
        for blocked in ("full", "missing"):
            with self.subTest(blocked=blocked):
                p = self.case(39)
                for hero in p["teamOur"]["roles"][:2]:
                    if blocked == "full":
                        hero["backPackCapability"] = len(hero["backpack"])
                if blocked == "missing":
                    p["mapInfo"]["zones"] = [z for z in p["mapInfo"]["zones"]
                                               if z["neutralType"] != "copper"]
                agent = Agent(self.config())
                agent.decide(p)
                mem = next(iter(agent.sessions.values()))
                mem.return_targets[1] = 20
                mem.return_posts[1] = (4, 6)
                mem.recovery.active[1] = ((1, "use", (7, 5)), 48)
                p["roundNo"] = 40
                result = agent.decide(p)
                self.assertFalse(result["roleCommandMap"])
                self.assertFalse(mem.return_targets)

    def test_early_travel_goes_to_mine_without_vendor_or_return_deadline(self):
        p = self.case(40)
        p["mapInfo"]["zones"] = [{"neutralType": "copper", "pos": {"x": 10, "y": 6}}]
        agent = Agent(self.config())
        response = agent.decide(p)
        self.assertEqual({c["action"] for c in response["roleCommandMap"].values()}, {"move"})
        self.assertEqual(len(response["roleCommandMap"]), 2)
        self.assertEqual(set(next(iter(agent.sessions.values())).mine_targets.values()), {(10, 6)})

    def test_daylight_41_resumes_sales_including_next_day(self):
        for day in (1, 4):
            p = self.case((day - 1) * 130 + 40)
            p["teamOur"]["roles"][0].update(health=220, backpack=["copper"])
            agent = Agent(self.config())
            self.assertEqual(agent.decide(p)["roleCommandMap"]["1"], command("collect", (6, 6)))
            p["roundNo"] += 1
            self.assertEqual(agent.decide(p)["roleCommandMap"]["1"],
                             command("sell", name="copper", num=1))


class CourierBudgetTests(unittest.TestCase):
    def case(self, carrier):
        p = payload(445, [unit(1, "worker", 2, 2, health=220),
                          unit(2, "worker", 12, 13, health=220),
                          unit(13, "station", 0, 2, health=1500, level=3),
                          unit(20, "rocket", 3, 3, level=3),
                          unit(30, "wall", 13, 13, health=1000, level=2),
                          unit(31, "wall", 3, 2, health=1000, level=2)])
        p["teamOur"]["roles"][carrier - 1]["backpack"] = ["WallUpgradeVoucher2"]
        p["teamOur"]["goldNum"] = 600
        p["mapInfo"]["zones"] = [{"neutralType": "weaponShop", "pos": {"x": 2, "y": 1}}]
        p["weaponShopList"] = [{"name": "WallUpgradeVoucher2", "price": 30}]
        return p

    def test_other_carrier_delivery_does_not_block_affordable_parallel_purchase(self):
        t, c, n, l = setup_case(self.case(2), layout_mode="explicit", loadout=["rocket"])
        plan = supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)
        # Team inventory still removes the other courier's paid wall from
        # the purchase; only the local unpaid wall needs another voucher.
        self.assertEqual(plan, ("WallUpgradeVoucher2", (0, None), 1))

    def test_own_paid_delivery_still_counts_against_purchase_deadline(self):
        t, c, n, l = setup_case(self.case(1), layout_mode="explicit", loadout=["rocket"])
        self.assertIsNone(supplies(t, c, Memory(), n, l, t.workers[0], bulk=True))

    def test_weapon_and_station_gates_remain_in_force(self):
        p = self.case(2)
        p["teamOur"]["roles"][1]["backpack"] = []
        p["teamOur"]["roles"][2]["level"] = 1
        p["teamOur"]["roles"][3]["level"] = 2
        p["teamOur"]["goldNum"] = 120
        p["weaponShopList"] += [{"name": "WeaponUpgradeVoucher2", "price": 150},
                                 {"name": "StationUpgradeVoucher1", "price": 100}]
        t, c, n, l = setup_case(p, layout_mode="explicit", loadout=["rocket"])
        self.assertIsNone(supplies(t, c, Memory(), n, l, t.workers[0], bulk=True))
