"""End-of-day resource conversion must be useful, legal and operator-safe."""
import copy
import unittest

from test_agent import payload, setup_case, unit
from agent.brain import Agent
from agent.commands import command
from agent.config import Config
from agent.economy import dusk_resources, supplies, use_inventory
from agent.intelligence import Memory


class DuskResourceTests(unittest.TestCase):
    def case(self, tick=60, gold=600):
        p = payload(tick, [unit(1, "worker", 2, 2, health=220),
                           unit(13, "station", 0, 2, health=1500),
                           unit(20, "rocket", 3, 3, level=2),
                           unit(30, "wall", 3, 2, health=1000)])
        p["teamOur"]["goldNum"] = gold
        p["mapInfo"]["zones"] = [{"neutralType": "weaponShop", "pos": {"x": 2, "y": 1}}]
        p["weaponShopList"] = [{"name": n, "price": price} for n, price in (
            ("WeaponUpgradeVoucher2", 150), ("WallUpgradeVoucher1", 20),
            ("WallUpgradeVoucher2", 30), ("StationUpgradeVoucher1", 100),
            ("StationUpgradeVoucher2", 150))]
        return p

    def setup(self, p, **kw):
        return setup_case(p, layout_mode="explicit", loadout=["rocket"],
                          weapon_cells=[[3, 3]], wall_cells=[[3, 2]], **kw)

    def test_healthy_walls_can_reach_three_before_healthy_base(self):
        for level in (1, 2):
            with self.subTest(level=level):
                p = self.case(40)
                p["teamOur"]["roles"][2]["level"] = 3
                p["teamOur"]["roles"][3]["level"] = level
                t, c, n, l = self.setup(p)
                plan = supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)
                self.assertEqual(plan[0], f"WallUpgradeVoucher{level}")

    def test_critical_base_does_not_bypass_wall_purchase_gate(self):
        p = self.case()
        p["teamOur"]["roles"][1]["health"] = 300
        t, c, n, l = self.setup(p)
        self.assertEqual(supplies(t, c, Memory(), n, l, t.workers[0])[0], "WeaponUpgradeVoucher2")
        # Already-owned healing is still usable without buying another item.
        p["teamOur"]["roles"][0]["backpack"] = ["StationUpgradeVoucher1", "WeaponUpgradeVoucher2"]
        t, c, n, l = self.setup(p)
        self.assertTrue(use_inventory(t, n, l, t.workers[0], mem=Memory()))
        self.assertEqual(l.commands["1"]["name"], "StationUpgradeVoucher1")

    def test_critical_base_waits_for_wall_repair_and_upgrade(self):
        p = self.case(gold=100)
        p["teamOur"]["roles"][1]["health"] = 300
        p["teamOur"]["roles"][3]["level"] = 2
        t, c, n, l = self.setup(p)
        plan = supplies(t, c, Memory(), n, l, t.workers[0])
        self.assertEqual(plan[0], "WallUpgradeVoucher2")

        # After the wall reaches level three, the same emergency budget can
        # be converted into the base upgrade and restore it to full health.
        p["teamOur"]["roles"][3]["level"] = 3
        p["teamOur"]["goldNum"] = 100
        t, c, n, l = self.setup(p)
        self.assertEqual(supplies(t, c, Memory(), n, l, t.workers[0])[0],
                         "StationUpgradeVoucher1")

    def test_sixty_spends_affordable_wall_money_when_weapon_is_too_expensive(self):
        p = self.case(gold=30)
        t, c, n, l = self.setup(p, economy_rounds=69)
        dusk_resources(t, c, Memory(), n, l, [(3, 3)])
        self.assertEqual(l.commands["1"], command("buy", name="WallUpgradeVoucher1", num=1))

    def test_wall_batch_policy_starts_on_day_four(self):
        # The new late-cash sweep must not change the first three days. On
        # day 3 a wall purchase remains the established single-voucher path;
        # day 4 may batch only when the complete route fits before night.
        for round_no, expected_max in ((320, 1), (450, 2)):
            p = self.case(round_no, gold=600)
            p["teamOur"]["roles"].append(unit(31, "wall", 4, 2, health=1000))
            t, c, n, l = self.setup(p)
            plan = supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)
            self.assertLessEqual(plan[2], expected_max)

    def test_sweep_is_day_local_and_leaves_earlier_turns_alone(self):
        for round_no, origin, active in ((59, 0, False), (60, 0, True), (70, 0, False),
                                         (189, 0, False), (190, 0, True),
                                         (60, 1, False), (61, 1, True)):
            with self.subTest(round_no=round_no, origin=origin):
                t, c, n, l = self.setup(self.case(round_no), round_origin=origin)
                dusk_resources(t, c, Memory(), n, l, [(3, 3)])
                self.assertEqual(bool(l.commands), active)

    def test_sixty_five_uses_nearby_voucher_before_stale_delivery_target(self):
        p = self.case(65)
        p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher1"]
        p["teamOur"]["roles"].append(unit(31, "wall", 5, 2, health=1000))
        t, c, n, l = self.setup(p)
        l.operator_posts[1] = (2, 2)
        mem = Memory(upgrade_targets={1: (5, 2)}, sale_workers={1})
        dusk_resources(t, c, mem, n, l, [(3, 3)])
        self.assertEqual(l.commands["1"], command("use", (3, 2), name="WallUpgradeVoucher1"))

    def test_delivery_beats_sale_recovery_and_more_shopping(self):
        p = self.case(65)
        p["teamOur"]["roles"][0]["backpack"] = ["WeaponUpgradeVoucher2", "copper"]
        p["mapInfo"]["zones"].append({"neutralType": "vendor", "pos": {"x": 1, "y": 3}})
        p["vendorShopList"] = [{"name": "copper", "price": 5}]
        agent = Agent(Config(layout_mode="explicit", loadout=["rocket"], llm_enabled=False))
        result = agent.decide(p)
        self.assertEqual(result["roleCommandMap"]["1"], command("use", (3, 3), name="WeaponUpgradeVoucher2"))
        self.assertEqual(result["prompt"], "")
        self.assertEqual(agent.decide(copy.deepcopy(p)), result)

    def test_carried_voucher_can_take_short_detour_before_return(self):
        p = self.case(65)
        p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher1"]
        p["teamOur"]["roles"][3]["pos"] = {"x": 4, "y": 2}
        t, c, n, l = self.setup(p)
        l.operator_posts[1] = (2, 2)
        dusk_resources(t, c, Memory(), n, l, [(3, 3)])
        self.assertEqual(l.commands["1"]["action"], "move")

    def test_last_turn_does_not_delay_operator_for_a_voucher(self):
        p = self.case(69)
        p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher1"]
        t, c, n, l = self.setup(p)
        l.operator_posts[1] = (2, 3)
        dusk_resources(t, c, Memory(), n, l, [(3, 3)])
        self.assertFalse(l.commands)
        self.assertFalse(use_inventory(t, n, l, t.workers[0], mem=Memory()))

    def test_shopping_checks_assigned_post_not_nearest_weapon(self):
        t, c, n, l = self.setup(self.case())
        l.operator_posts[1] = (14, 14)
        self.assertIsNone(supplies(t, c, Memory(), n, l, t.workers[0], bulk=True))

    def test_day_four_maxed_weapons_can_spend_wall_cash_without_post_return(self):
        p = self.case(450, gold=120)
        p["teamOur"]["roles"][2]["level"] = 3
        p["teamOur"]["roles"][3]["level"] = 2
        t, c, n, l = self.setup(p)
        l.operator_posts[1] = (14, 14)
        plan = supplies(t, c, Memory(), n, l, t.workers[0], bulk=True)
        self.assertIsNotNone(plan)
        self.assertEqual(plan[0], "WallUpgradeVoucher2")

    def test_final_turn_can_use_adjacent_voucher_without_leaving_post(self):
        p = self.case(69)
        p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher1"]
        t, c, n, l = self.setup(p)
        l.operator_posts[1] = (2, 2)
        dusk_resources(t, c, Memory(), n, l, [(3, 3)])
        self.assertEqual(l.commands["1"], command("use", (3, 2), name="WallUpgradeVoucher1"))

    def test_nearby_wall_is_considered_when_first_wall_is_too_far(self):
        p = self.case(65, gold=20)
        p["teamOur"]["roles"].append(unit(31, "wall", 14, 14, health=1000))
        t, c, n, l = self.setup(p)
        self.assertEqual(supplies(t, c, Memory(), n, l, t.workers[0])[0], "WallUpgradeVoucher1")

    def test_build_budget_full_bag_missing_shop_and_failures_are_respected(self):
        for blocked in ("reserve", "full", "shop", "failure"):
            with self.subTest(blocked=blocked):
                p = self.case(gold=25)
                if blocked == "full":
                    p["teamOur"]["roles"][0].update(backpack=["copper"], backPackCapability=1)
                if blocked == "shop":
                    p["mapInfo"]["zones"] = []
                t, c, n, l = self.setup(p)
                mem = Memory(buy_failures={(1, "WallUpgradeVoucher1"): 70}) if blocked == "failure" else Memory()
                # Missing-gun reserve uses the configured loadout count.
                if blocked == "reserve":
                    c.loadout.append("rocket")
                dusk_resources(t, c, mem, n, l, [(3, 3), (4, 4)] if blocked == "reserve" else [(3, 3)])
                self.assertFalse(l.commands)

    def test_active_pioneer_task_is_not_interrupted(self):
        p = self.case(65)
        p["teamOur"]["roles"][0].update(roleType="pioneer", backpack=["WeaponUpgradeVoucher2"])
        p["phaseTask"] = "active"
        t, c, n, l = self.setup(p)
        dusk_resources(t, c, Memory(), n, l, [(3, 3)])
        self.assertFalse(l.commands)

    def test_two_carriers_do_not_claim_one_upgrade(self):
        p = self.case(65)
        p["teamOur"]["roles"][0]["backpack"] = ["WallUpgradeVoucher1"]
        p["teamOur"]["roles"].append(unit(2, "worker", 4, 2, backpack=["WallUpgradeVoucher1"]))
        t, c, n, l = self.setup(p)
        dusk_resources(t, c, Memory(), n, l, [(3, 3)])
        uses = [cmd for cmd in l.commands.values() if cmd["action"] == "use"]
        self.assertEqual(len(uses), 1)

    def test_ten_turn_replay_spends_cash_and_delivers_all_purchases(self):
        # Keep the shop, all upgrade targets and operator post adjacent so
        # route length cannot hide a failure to spend/use. No model required.
        for mirror in (False, True):
            p = self.case()
            if mirror:
                for r in p["teamOur"]["roles"]:
                    r["pos"]["x"] = 14 - r["pos"]["x"] - (1 if r["roleType"] == "station" else 0)
                p["mapInfo"]["zones"][0]["pos"]["x"] = 12
            cfg = Config(layout_mode="explicit", loadout=["rocket"], llm_enabled=False)
            agent = Agent(cfg)
            for tick in range(60, 70):
                p["roundNo"] = tick
                t, _, _, ledger = setup_case(p, layout_mode="explicit", loadout=["rocket"])
                response = agent.decide(p)
                for uid, cmd in response["roleCommandMap"].items():
                    self.assertTrue(ledger.add(int(uid), cmd), (tick, cmd))
                    actor = next(r for r in p["teamOur"]["roles"] if str(r["id"]) == uid)
                    if cmd["action"] == "buy":
                        p["teamOur"]["goldNum"] -= t.shop[cmd["name"]] * cmd["num"]
                        actor["backpack"] += [cmd["name"]] * cmd["num"]
                    elif cmd["action"] == "use":
                        actor["backpack"].remove(cmd["name"])
                        target = next(r for r in p["teamOur"]["roles"] if r["pos"] == cmd["targetPos"][0])
                        target["level"] += 1
                        target["health"] = 1500
                    elif cmd["action"] == "move":
                        actor["pos"] = cmd["targetPos"][0]
                    else:
                        self.fail(cmd)
                p["lastRoundRoleActionResults"] = {uid: True for uid in response["roleCommandMap"]}
            self.assertEqual(p["teamOur"]["roles"][2]["level"], 3)
            self.assertEqual(p["teamOur"]["roles"][3]["level"], 3)
            self.assertEqual(p["teamOur"]["roles"][0]["backpack"], [])
            self.assertEqual(p["teamOur"]["roles"][1]["level"], 3)
            self.assertEqual(p["teamOur"]["goldNum"], 150)
