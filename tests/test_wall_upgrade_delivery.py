"""Strict wall stages, replacement priority and paid delivery regressions."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.config import Config
from agent.commands import command
from agent.economy import (supplies, use_inventory, workers, wall_upgrade_allowed,
                           upgrade_order, dusk_batch)
from agent.economy_plan import preparation_start
from agent.intelligence import Memory
from agent.model import Turn
from agent.recovery import Recovery


class WallUpgradeDeliveryTests(unittest.TestCase):
    def case(self, tick=40):
        p = payload(tick, [unit(1, "worker", 5, 5, health=220),
                           unit(13, "station", 1, 5, health=1500),
                           unit(20, "rocket", 4, 6, level=3),
                           unit(30, "wall", 7, 5, health=1000, level=2),
                           unit(31, "wall", 5, 4, health=1000, level=2)])
        p["teamOur"]["goldNum"] = 600
        p["mapInfo"]["zones"] = [{"neutralType": "weaponShop", "pos": {"x": 6, "y": 6}}]
        p["weaponShopList"] = [{"name": name, "price": price} for name, price in (
            ("WallUpgradeVoucher1", 20), ("WallUpgradeVoucher2", 30),
            ("StationUpgradeVoucher1", 100), ("StationUpgradeVoucher2", 150))]
        return p

    def setup(self, p, **kw):
        return setup_case(p, layout_mode="explicit", loadout=["rocket"],
                          weapon_cells=[[4, 6]], wall_cells=[[7, 5], [5, 4]], **kw)

    def test_station_purchase_requires_actual_completed_walls(self):
        for blocked in ("stock", "claimed", "price", "failure", "missing_shop_item", "unreachable"):
            with self.subTest(blocked=blocked):
                p = self.case()
                mem = Memory()
                if blocked == "stock":
                    p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher2"] * 2
                if blocked == "price":
                    p["weaponShopList"][1]["price"] = 700
                if blocked == "failure":
                    mem.buy_failures[(1, "WallUpgradeVoucher2")] = 70
                if blocked == "missing_shop_item":
                    p["weaponShopList"] = p["weaponShopList"][2:]
                if blocked == "unreachable":
                    p["mapInfo"]["zones"] += [{"neutralType": "stone", "pos": {"x": 6, "y": y}}
                                               for y in range(15) if y != 6]
                t, c, n, l = self.setup(p)
                if blocked == "claimed":
                    l.upgrade_claims.update((30, 31))
                plan = supplies(t, c, mem, n, l, t.workers[0], bulk=True)
                self.assertTrue(plan is None or not plan[0].startswith("Station"))
        p = self.case()
        for r in p["teamOur"]["roles"][3:]:
            r["level"] = 3
        t, c, n, l = self.setup(p)
        self.assertEqual(supplies(t, c, Memory(), n, l, t.workers[0])[0], "StationUpgradeVoucher1")
        self.assertIsNone(supplies(t, c, Memory(wall_rebuild_levels={(7, 4): 3}), n, l, t.workers[0]))

    def test_every_wall_reaches_two_before_normal_level_three(self):
        for tick in (40, 65):
            p = self.case(tick)
            p["teamOur"]["roles"][4]["level"] = 1
            p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher1", "WallUpgradeVoucher2"]
            t, c, n, l = self.setup(p)
            self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=Memory(upgrade_targets={1: (7, 5)})))
            self.assertEqual(l.commands["1"], command("use", (5, 4), name="WallUpgradeVoucher1"))
            p["teamOur"]["roles"][0]["backpack"] = []
            t, c, n, l = self.setup(p)
            self.assertEqual(supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)[0], "WallUpgradeVoucher1")

    def test_front_three_precedes_nearer_flank_even_at_dusk_or_with_old_target(self):
        for mirror in (False, True):
            for tick in (40, 65):
                p = self.case(tick)
                p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher2"]
                if mirror:
                    for r in p["teamOur"]["roles"]:
                        r["pos"]["x"] = 14 - r["pos"]["x"] - (1 if r["roleType"] == "station" else 0)
                    p["mapInfo"]["zones"][0]["pos"]["x"] = 8
                t, c, n, l = self.setup(p)
                front, flank = t.ours[3:]
                mem = Memory(upgrade_targets={1: flank.pos})
                self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=mem))
                self.assertEqual(mem.upgrade_targets[1], front.pos)
                self.assertIn(front.id, l.upgrade_claims)
                self.assertFalse(wall_upgrade_allowed(t, flank, mem))

    def test_prefetch_next_wall_level_from_paid_prerequisite(self):
        p = self.case()
        p["teamOur"]["roles"][3]["level"] = 1
        p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher1"]
        t, c, n, l = self.setup(p)
        plan = supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)
        self.assertEqual((plan[0], plan[2]), ("WallUpgradeVoucher2", 2))

    def test_shop_buys_both_wall_tiers_before_delivery(self):
        for rebuilding in (False, True):
            p = self.case()
            for w in p["teamOur"]["roles"][3:]:
                w["level"] = 1
            p["teamOur"]["roles"].append(unit(32, "wall", 7, 4, level=2, health=1000))
            mem = Memory(wall_rebuild_levels={(7, 5): 3, (5, 4): 3} if rebuilding else {})
            for name, count in (("WallUpgradeVoucher1", 2), ("WallUpgradeVoucher2", 3)):
                t, c, n, l = self.setup(p)
                workers(t, c, mem, n, l, [(4, 6)], [(7, 5), (5, 4), (7, 4)])
                self.assertEqual(l.commands["1"], command("buy", name=name, num=count))
                p["teamOur"]["goldNum"] -= t.shop[name] * count
                p["teamOur"]["roles"][0]["backpack"] += [name] * count
            t, c, n, l = self.setup(p)
            workers(t, c, mem, n, l, [(4, 6)], [(7, 5), (5, 4), (7, 4)])
            self.assertIn(l.commands["1"]["action"], ("use", "move"))

    def test_prefetch_respects_money_space_and_delivery_deadline(self):
        for gold, capacity, tick in ((0, 100, 40), (600, 1, 40), (600, 100, 69)):
            p = self.case(tick)
            p["teamOur"]["goldNum"] = gold
            p["teamOur"]["roles"][3]["level"] = 1
            p["teamOur"]["roles"][0].update(backpack=["WallUpgradeVoucher1"],
                                           backPackCapability=capacity)
            t, c, n, l = self.setup(p)
            workers(t, c, Memory(), n, l, [(4, 6)], [(7, 5), (5, 4)])
            self.assertNotEqual(l.commands.get("1", {}).get("action"), "buy")

    def front_case(self, tick=40, rows=range(2, 9), mirror=False):
        p = self.case(tick)
        p["teamOur"]["roles"][0].update(pos={"x": 6, "y": 2}, backpack=["WallUpgradeVoucher2"])
        p["teamOur"]["roles"] = p["teamOur"]["roles"][:3] + [
            unit(30 + y, "wall", 7, y, level=2, health=1000) for y in rows
        ] + [unit(50, "wall", 5, 2, level=2, health=1000)]
        if mirror:
            for r in p["teamOur"]["roles"]:
                r["pos"]["x"] = 14 - r["pos"]["x"] - (1 if r["roleType"] == "station" else 0)
            p["mapInfo"]["zones"][0]["pos"]["x"] = 8
        return p

    def test_front_three_expands_in_symmetric_layers_before_flanks(self):
        for rows in (range(2, 9), range(2, 8)):
            for mirror in (False, True):
                with self.subTest(rows=list(rows), mirror=mirror):
                    p = self.front_case(rows=rows, mirror=mirror)
                    midpoint = (min(rows) + max(rows)) / 2
                    for radius in sorted({abs(y - midpoint) for y in rows}):
                        t, _, _, _ = self.setup(p)
                        allowed = {w.pos[1] for w in t.ours if w.kind == "wall" and w.level == 2
                                   and wall_upgrade_allowed(t, w, Memory())}
                        self.assertEqual(allowed, {y for y in rows if abs(y - midpoint) == radius})
                        for w in p["teamOur"]["roles"][3:-1]:
                            if w["pos"]["y"] in allowed:
                                w["level"] = 3
                    t, _, _, _ = self.setup(p)
                    self.assertTrue(wall_upgrade_allowed(t, t.ours[-1], Memory()))

    def test_front_center_beats_nearer_edge_old_target_and_recovery(self):
        for mirror in (False, True):
            for tick in (40, 65, 450, 455):
                with self.subTest(mirror=mirror, tick=tick):
                    p = self.front_case(tick, mirror=mirror)
                    t, c, n, l = self.setup(p)
                    center = next(w for w in t.ours if w.id == 35)
                    edge = next(w for w in t.ours if w.id == 32)
                    mem = Memory(upgrade_targets={1: edge.pos})
                    self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=mem))
                    self.assertEqual(mem.upgrade_targets[1], center.pos)
                    self.assertIn(center.id, l.upgrade_claims)
                    self.assertIsNone(Recovery().action((1, "use", edge.pos), t, c, mem, n, l))
                    # A claimed inner wall is still level 2 until the next observation.
                    self.assertFalse(wall_upgrade_allowed(t, edge, mem))

    def test_rebuilt_front_edge_keeps_emergency_priority(self):
        t, _, n, l = self.setup(self.front_case())
        edge = next(w for w in t.ours if w.id == 32)
        mem = Memory(wall_rebuild_levels={edge.pos: 3})
        self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=mem))
        self.assertEqual(l.commands["1"], command("use", edge.pos, name="WallUpgradeVoucher2"))

    def test_late_purchase_keeps_prefetch_but_budgets_center_first(self):
        p = self.front_case(450)
        p["teamOur"]["roles"][0]["backpack"] = []
        t, c, n, l = self.setup(p)
        plan = supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)
        self.assertIsNotNone(plan)
        self.assertEqual(plan[0], "WallUpgradeVoucher2")
        self.assertGreater(plan[2], 1)
        # One remaining turn could use the adjacent edge, but not the center.
        p["roundNo"] = 459
        t, _, n, l = self.setup(p)
        batch = [(upgrade_order(t, w, Memory()), "WallUpgradeVoucher2", w.cells)
                 for w in t.ours if w.kind == "wall"]
        count, _ = dusk_batch(t, n, l, t.workers[0], batch, 8, 0, require_home=False)
        self.assertEqual(count, 0)

    def test_recovery_cannot_skip_front_wall_stage(self):
        p = self.case()
        p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher2"]
        t, c, n, l = self.setup(p)
        self.assertIsNone(Recovery().action((1, "use", (5, 4)), t, c, Memory(), n, l))

    def test_shop_carrier_delivers_before_sale_or_another_purchase(self):
        for tick in (10, 40, 59):
            p = self.case(tick)
            p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher2", "copper"]
            p["mapInfo"]["zones"] += [{"neutralType": k, "pos": {"x": x, "y": y}}
                                       for k, x, y in (("vendor", 4, 5), ("copper", 4, 4))]
            p["vendorShopList"] = [{"name": "copper", "price": 5}]
            t, c, n, l = self.setup(p)
            mem = Memory(sale_workers={1})
            workers(t, c, mem, n, l, [(4, 6)], [(7, 5), (5, 4)])
            self.assertEqual(l.commands["1"]["action"], "move")
            self.assertEqual(mem.upgrade_targets[1], (7, 5))

    def test_replacement_shopping_starts_before_farming_cutoff(self):
        p = self.case(5)
        p["teamOur"]["roles"][3]["level"] = 1
        p["mapInfo"]["zones"] += [{"neutralType": k, "pos": {"x": x, "y": y}}
                                   for k, x, y in (("vendor", 4, 5), ("copper", 4, 4))]
        p["vendorShopList"] = [{"name": "copper", "price": 5}]
        p["teamOur"]["roles"][0]["backpack"] = ["copper"]
        t, c, n, l = self.setup(p)
        workers(t, c, Memory(wall_rebuild_levels={(7, 5): 3}), n, l, [(4, 6)], [(7, 5), (5, 4)])
        self.assertEqual(l.commands["1"], command("buy", name="WallUpgradeVoucher1", num=1))

    def test_destroyed_wall_remembers_its_own_level_with_weaker_neighbours(self):
        for instant_replacement in (False, True):
            p = self.case(129)
            p["teamOur"]["roles"][3]["level"] = 3
            p["teamOur"]["roles"][4]["level"] = 1
            t, c, _, _ = self.setup(p)
            mem = Memory()
            mem.observe(t, c)
            mem.last_round = 129
            p["roundNo"] = 130
            p["teamOur"]["roles"][3].update(id=99, level=1)
            if not instant_replacement:
                replacement = p["teamOur"]["roles"].pop(3)
                mem.observe(Turn(p, c), c)
                p["roundNo"] = 131
                p["teamOur"]["roles"].append(replacement)
            mem.observe(Turn(p, c), c)
            self.assertEqual(mem.wall_rebuild_levels[(7, 5)], 3)
            p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher1"]
            t, c, n, l = self.setup(p)
            self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=mem))
            self.assertEqual(mem.upgrade_targets[1], (7, 5))

    def test_wall_only_preparation_includes_shop_and_wall_delivery(self):
        p = self.case(1)
        p["teamOur"]["roles"][1]["level"] = 3
        p["mapInfo"]["zones"][0]["pos"] = {"x": 14, "y": 14}
        t, c, n, _ = self.setup(p, economy_rounds=69)
        pending = preparation_start(t, c, Memory(), n, t.workers, [])
        for r in p["teamOur"]["roles"][3:]:
            r["level"] = 3
        t, c, n, _ = self.setup(p, economy_rounds=69)
        self.assertLess(pending, preparation_start(t, c, Memory(), n, t.workers, []))

    def test_bulk_purchase_budgets_return_from_last_wall_to_assigned_post(self):
        p = self.case(54)
        p["teamOur"]["roles"][3]["level"] = 1
        p["teamOur"]["roles"][4]["level"] = 1
        t, c, n, l = self.setup(p)
        l.operator_posts[1] = (14, 14)
        far = supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)
        l.operator_posts[1] = (5, 5)
        near = supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)
        self.assertIsNotNone(near)
        self.assertTrue(far is None or far[2] < near[2])

    def test_rebuilt_wall_buy_deliver_two_levels_before_night(self):
        for mirror in (False, True):
            p = self.case(40)
            p["teamOur"]["roles"][4]["level"] = 3
            if mirror:
                for r in p["teamOur"]["roles"]:
                    r["pos"]["x"] = 14 - r["pos"]["x"] - (1 if r["roleType"] == "station" else 0)
                p["mapInfo"]["zones"][0]["pos"]["x"] = 8
            wall_pos = [(r["pos"]["x"], r["pos"]["y"]) for r in p["teamOur"]["roles"][3:]]
            cfg = Config(layout_mode="explicit", loadout=["rocket"],
                         wall_cells=[list(x) for x in wall_pos], llm_enabled=False)
            agent = Agent(cfg)
            # Observe a level-3 wall, then a replacement ID at level 1.
            p["teamOur"]["roles"][3]["level"] = 3
            agent.decide(copy.deepcopy(p))
            p["teamOur"]["roles"][3].update(id=99, level=1)
            used = []
            for tick in range(41, 70):
                p["roundNo"] = tick
                t, _, _, ledger = setup_case(p, layout_mode="explicit", loadout=["rocket"])
                result = agent.decide(copy.deepcopy(p))["roleCommandMap"]
                for uid, cmd in result.items():
                    self.assertTrue(ledger.add(int(uid), cmd), (tick, cmd))
                    actor = next(r for r in p["teamOur"]["roles"] if str(r["id"]) == uid)
                    if cmd["action"] == "buy":
                        if cmd["name"].startswith("Station"):
                            self.assertTrue(all(r["level"] == 3 for r in p["teamOur"]["roles"] if r["roleType"] == "wall"))
                        p["teamOur"]["goldNum"] -= t.shop[cmd["name"]] * cmd["num"]
                        actor["backpack"] += [cmd["name"]] * cmd["num"]
                    elif cmd["action"] == "use":
                        actor["backpack"].remove(cmd["name"])
                        target = next(r for r in p["teamOur"]["roles"] if r["pos"] == cmd["targetPos"][0])
                        target["level"] += 1
                        used.append((cmd["name"], target["id"]))
                    elif cmd["action"] == "move":
                        actor["pos"] = cmd["targetPos"][0]
                    else:
                        self.fail(cmd)
                p["lastRoundRoleActionResults"] = {uid: True for uid in result}
            self.assertEqual(used[:2], [("WallUpgradeVoucher1", 99), ("WallUpgradeVoucher2", 99)])
            self.assertEqual(p["teamOur"]["roles"][3]["level"], 3)
            self.assertEqual(p["teamOur"]["roles"][0]["backpack"], [])
