"""Shared-crew protocol, geometry, handoff and lifecycle regressions."""
import unittest
from test_agent import payload, unit, setup_case
from agent.brain import Agent
from agent.commands import command
from agent.combat import crew_plan, crew_groups, defend, prepare_relief, relief_excluded
from agent.config import Config
from agent.intelligence import Memory
from agent.model import Turn, neighbours
from agent.navigation import layout


def scenario(rno=80):
    p = payload(rno, [unit(1, "worker", 4, 5), unit(2, "worker", 1, 5),
                      unit(3, "pioneer", 1, 1), unit(13, "station", 2, 8)] +
                [unit(20+i, "rocket", 5, y) for i, y in enumerate((4, 5, 6))])
    p["robot"]["roles"] = [unit(90, "largeRobot", 9, 5, health=500,
                                  attackRange=1, targetTeam="challenger")]
    return p


def shot():
    return {"action": "attack", "controllerId": "1", "targetPos": [{"x": 9, "y": 5}]}


def plan(p):
    t, cfg, nav, ledger = setup_case(p, layout_mode="explicit")
    pairs, posts = crew_plan(t, nav, ledger)
    return t, nav, ledger, pairs, {uid: p for uid, (p, _) in posts.items()}


class SharedCommandsTests(unittest.TestCase):
    def test_one_worker_fires_three_unique_towers(self):
        t, n, ledger, pairs, posts = plan(scenario())
        self.assertEqual({h.id for h, _ in pairs}, {1})
        self.assertEqual({w.id for _, w in pairs}, {20, 21, 22})
        defend(t, n, ledger, pairs, posts)
        self.assertEqual(set(ledger.commands), {"20", "21", "22"})
        self.assertEqual({c["controllerId"] for c in ledger.commands.values()}, {"1"})
        self.assertFalse(ledger.add(20, shot()))

    def test_personal_actions_and_control_conflict_in_both_orders(self):
        for action in (command("move", (3, 5)), command("use", name="Medicine"),
                       command("collect", (3, 4))):
            for attack_first in (False, True):
                with self.subTest(action=action, attack_first=attack_first):
                    p = scenario()
                    p["teamOur"]["roles"][0]["backpack"] = ["Medicine"]
                    p["mapInfo"]["zones"] = [{"neutralType": "copper", "pos": {"x": 3, "y": 4}}]
                    _, _, _, ledger = setup_case(p)
                    if attack_first:
                        self.assertTrue(ledger.add(20, shot()))
                        self.assertFalse(ledger.add(1, action))
                    else:
                        self.assertTrue(ledger.add(1, action))
                        self.assertFalse(ledger.add(20, shot()))

    def test_single_tower_compatibility(self):
        _, _, _, ledger = setup_case(scenario(), shared_operators=False)
        self.assertTrue(ledger.add(20, shot()))
        self.assertFalse(ledger.add(21, shot()))

    def test_mixed_cooldown_range_and_marginal_damage(self):
        p = scenario()
        p["teamOur"]["roles"][-1]["cooldown"] = 2
        p["teamOur"]["roles"][-2]["attackRange"] = 1
        t, n, ledger, pairs, posts = plan(p)
        defend(t, n, ledger, pairs, posts)
        self.assertEqual(set(ledger.commands), {"20"})
        p = scenario()
        p["robot"]["roles"][0]["health"] = 20
        t, n, ledger, pairs, posts = plan(p)
        defend(t, n, ledger, pairs, posts)
        self.assertEqual(sum(c["action"] == "attack" for c in ledger.commands.values()), 1)

    def test_travelling_shared_worker_only_moves_once(self):
        p = scenario(60)
        p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"] if r["id"] not in (2, 3)]
        p["teamOur"]["roles"][0]["pos"] = {"x": 0, "y": 5}
        t, n, ledger, pairs, posts = plan(p)
        self.assertEqual(len(pairs), 3)
        defend(t, n, ledger, pairs, posts)
        self.assertEqual(set(ledger.commands), {"1"})
        self.assertEqual(ledger.commands["1"]["action"], "move")


class SharedCrewTests(unittest.TestCase):
    def test_spare_worker_yields_a_blocked_return_route(self):
        p = scenario(68)
        p["robot"]["roles"] = []
        p["teamOur"]["roles"][0]["pos"] = {"x": 2, "y": 5}
        p["teamOur"]["roles"][1]["pos"] = {"x": 3, "y": 5}
        p["teamOur"]["roles"] += [unit(100+y, "wall", 3, y) for y in range(15) if y != 5]
        t, _, n, ledger = setup_case(p, layout_mode="explicit")
        pairs = [(t.workers[0], w) for w in t.weapons]
        self.assertIsNone(n.search(t.workers[0], {(4, 5)}))
        defend(t, n, ledger, pairs, {1: (4, 5)})
        self.assertEqual(ledger.commands["2"]["action"], "move")
        p["teamOur"]["roles"][1]["pos"] = ledger.commands["2"]["targetPos"][0]
        t, _, n, _ = setup_case(p, layout_mode="explicit")
        self.assertIsNotNone(n.search(t.workers[0], {(4, 5)}))

    def test_healing_worker_does_not_count_as_ready_task_cover(self):
        p = scenario()
        p["teamOur"]["roles"] = [r for r in p["teamOur"]["roles"] if r["id"] != 2]
        p["teamOur"]["roles"][0].update(health=60, backpack=["Medicine"])
        p["robot"]["roles"][0]["pos"] = {"x": 11, "y": 5}
        p["phaseTask"] = "求解测试题"
        agent = Agent(Config(layout_mode="explicit"))
        t = Turn(p, agent.cfg)
        mem = Memory(day=1, task_started=75, task_timeout=15)
        agent.sessions[(*t.key, t.station.pos)] = mem
        commands = agent.decide(p)["roleCommandMap"]
        self.assertEqual(commands["1"]["name"], "Medicine")
        self.assertTrue(mem.stop_reason)
        self.assertFalse(any(c["action"] == "attack" and c["controllerId"] == "1"
                             for c in commands.values()))

    def test_fire_mining_and_task_progress_together(self):
        p = scenario()
        p["robot"]["roles"][0]["pos"] = {"x": 11, "y": 5}
        p["teamOur"]["roles"][1]["pos"] = {"x": 3, "y": 5}
        p["mapInfo"]["zones"] = [{"neutralType": "copper", "pos": {"x": 3, "y": 6}}]
        p["vendorShopList"] = [{"name": "copper", "price": 5}]
        p["phaseTask"] = "求解测试题"
        agent = Agent(Config(layout_mode="explicit"))
        t = Turn(p, agent.cfg)
        mem = Memory(day=1, task_started=75, task_timeout=15)
        agent.sessions[(*t.key, t.station.pos)] = mem
        result = agent.decide(p)
        commands = result["roleCommandMap"]
        self.assertEqual({commands[str(w)]["controllerId"] for w in (20, 21, 22)}, {"1"})
        self.assertEqual(commands["2"]["action"], "collect")
        self.assertTrue(result["prompt"] or result["executeCmd"])
        self.assertFalse(mem.stop_reason)

    def test_workers_preferred_and_split_layout_falls_back(self):
        p = scenario(60)
        _, _, _, pairs, posts = plan(p)
        self.assertEqual(len(posts), 1)
        self.assertTrue(all(h.kind == "worker" for h, _ in pairs))
        for role, point in zip(p["teamOur"]["roles"][-3:], ((5, 3), (5, 5), (12, 12))):
            role["pos"] = dict(zip(("x", "y"), point))
        t, _, _, pairs, posts = plan(p)
        self.assertEqual(len(pairs), 3)
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(t.adjacent(posts[h.id], w.pos) for h, w in pairs))

    def test_death_and_blocked_post_replan(self):
        p = scenario()
        p["teamOur"]["roles"][0]["health"] = 0
        _, _, _, pairs, _ = plan(p)
        self.assertNotIn(1, {h.id for h, _ in pairs})
        self.assertEqual(len(pairs), 3)
        p = scenario(60)
        p["teamOur"]["roles"][0]["pos"] = {"x": 2, "y": 4}
        p["teamOur"]["roles"].append(unit(80, "wall", 4, 5))
        _, _, _, pairs, posts = plan(p)
        self.assertEqual(len(pairs), 3)
        self.assertNotIn((4, 5), posts.values())

    def test_cooldown_does_not_release_guard_for_upgrade(self):
        p = scenario()
        for r in p["teamOur"]["roles"][-3:]:
            r["cooldown"] = 2
        p["teamOur"]["roles"][0]["backpack"] = ["WeaponUpgradeVoucher1"]
        agent = Agent(Config(llm_enabled=False, layout_mode="explicit"))
        t = Turn(p, agent.cfg)
        mem = Memory(day=1, crew={1: ((4, 5), (20, 21, 22))})
        agent.sessions[(*t.key, t.station.pos)] = mem
        self.assertNotIn("1", agent.decide(p)["roleCommandMap"])
        self.assertEqual(set(mem.crew), {1})

    def test_incumbent_fires_while_relief_travels(self):
        p = scenario()
        p["teamOur"]["roles"][0]["health"] = 120
        t, n, ledger, pairs, posts = plan(p)
        self.assertEqual({h.id for h, _ in pairs}, {1})
        mem = Memory()
        prepare_relief(t, n, ledger, pairs, posts, mem)
        defend(t, n, ledger, pairs, posts)
        self.assertEqual(ledger.commands["2"]["action"], "move")
        self.assertEqual(sum(c["action"] == "attack" for c in ledger.commands.values()), 3)
        self.assertFalse(mem.relief[1][2])

    def test_staged_handoff(self):
        p = scenario()
        p["teamOur"]["roles"][0]["health"] = 120
        p["teamOur"]["roles"][1]["pos"] = {"x": 3, "y": 5}
        t, n, ledger, pairs, posts = plan(p)
        mem = Memory()
        prepare_relief(t, n, ledger, pairs, posts, mem)
        self.assertEqual(ledger.commands["1"]["action"], "move")
        self.assertEqual(relief_excluded(t, mem), {1})
        p["teamOur"]["roles"][0]["pos"] = ledger.commands["1"]["targetPos"][0]
        t, _, n, ledger = setup_case(p, layout_mode="explicit")
        pairs, _ = crew_plan(t, n, ledger, excluded=relief_excluded(t, mem))
        self.assertEqual({h.id for h, _ in pairs}, {2})

    def test_first_night_keeps_safe_task_with_ready_worker(self):
        p = scenario(69)
        p["robot"]["roles"] = []
        p["phaseTask"] = "求解测试题"
        agent = Agent(Config(layout_mode="explicit"))
        t = Turn(p, agent.cfg)
        mem = Memory(day=1, task_started=65, task_timeout=15)
        agent.sessions[(*t.key, t.station.pos)] = mem
        result = agent.decide(p)
        self.assertNotEqual(mem.stop_reason, "first_wave_deadline")
        self.assertNotIn(3, mem.return_targets)
        self.assertTrue(result["prompt"] or result["executeCmd"])


class SharedLayoutTests(unittest.TestCase):
    def test_main_build_zones_and_crew_coverage_four_corners(self):
        for station in ((3, 11), (10, 4), (3, 4), (10, 11)):
            p = payload(roles=[unit(13, "station", *station)] +
                        [unit(i+1, "worker", 0, 6+i) for i in range(3)])
            cfg = Config()
            initial = Turn(p, cfg)
            towers, walls = layout(initial, cfg)
            self.assertEqual(len(towers), 3)
            self.assertEqual(len(walls), 12)
            self.assertTrue(all(initial.base_distance(q) == 1 for q in towers))
            self.assertTrue(all(initial.base_distance(q) == 2 for q in walls))
            self.assertEqual((towers, walls), layout(initial, Config(shared_operators=False)))
            p["teamOur"]["roles"] += [unit(20+i, "rocket", *q) for i, q in enumerate(towers)]
            p["teamOur"]["roles"] += [unit(100+i, "wall", *q) for i, q in enumerate(walls)]
            t, _, n, ledger = setup_case(p)
            pairs, posts = crew_plan(t, n, ledger, walls)
            self.assertEqual({w.id for _, w in pairs}, {20, 21, 22})
            for hero, tower in pairs:
                self.assertTrue(t.adjacent(posts[hero.id][0], tower.pos))
                self.assertIsNotNone(n.search(hero, {posts[hero.id][0]}))
            self.assertIsNotNone(n.approach(t.workers[0], t.station.cells))

    def test_match_log_coordinates_restore_main_positions(self):
        p = payload(roles=[unit(13, "station", 9, 22), unit(1, "worker", 8, 22,
                           backpack=["stone"])])
        p["mapInfo"].update(width=41, height=32)
        t, cfg, n, ledger = setup_case(p)
        towers, walls = layout(t, cfg)
        self.assertEqual(towers, [(11, 22), (11, 23), (11, 20)])
        self.assertEqual(set(walls[:6]), {(12, y) for y in range(19, 25)})
        self.assertTrue({(12, 20), (12, 21), (12, 22)}.isdisjoint(towers))
        self.assertTrue({(13, y) for y in range(19, 25)}.isdisjoint(walls))
        # Put the builder in range so rejection is caused by the zone whitelist.
        p["teamOur"]["roles"][1]["pos"] = {"x": 11, "y": 21}
        _, _, _, ledger = setup_case(p)
        self.assertFalse(ledger.add(1, command("build", (12, 21), name="rocket")))
        p["teamOur"]["roles"][1]["pos"] = {"x": 12, "y": 21}
        _, _, _, ledger = setup_case(p)
        self.assertFalse(ledger.add(1, command("build", (13, 21), name="wall")))

    def test_old_and_explicit_layouts_are_preserved(self):
        p = scenario(1)
        p["teamOur"]["roles"] = p["teamOur"]["roles"][:4]
        old = layout(Turn(p, Config()), Config(shared_operators=False))
        p["teamOur"]["roles"] += [unit(20+i, "rocket", *q) for i, q in enumerate(old[0])]
        self.assertEqual(layout(Turn(p, Config()), Config()), old)
        cfg = Config(layout_mode="explicit", weapon_cells=[[2, 2]], wall_cells=[[3, 3]])
        self.assertEqual(layout(Turn(payload(), cfg), cfg), ([(2, 2)], [(3, 3)]))


class SharedSafetyTests(unittest.TestCase):
    def test_lethal_post_loses_priority_over_immediate_fire(self):
        from agent.combat import post_damage
        p = scenario()
        p['teamOur']['roles'][0]['health'] = 30
        p['robot']['roles'][0]['pos'] = {'x': 6, 'y': 5}
        t, _, _, pairs, posts = plan(p)
        self.assertTrue(pairs)
        for h, _ in pairs:
            self.assertLess(post_damage(t, posts[h.id]), h.health)
        self.assertNotIn(1, {h.id for h, _ in pairs})

    def test_endangered_incumbent_really_retreats(self):
        from agent.combat import post_damage
        p = scenario()
        p['teamOur']['roles'][0]['health'] = 30
        p['robot']['roles'][0]['pos'] = {'x': 6, 'y': 5}
        agent = Agent(Config(layout_mode='explicit', llm_enabled=False))
        t = Turn(p, agent.cfg)
        agent.sessions[(*t.key, t.station.pos)] = Memory(day=1, crew={1: ((4, 5), (20, 21, 22))})
        cmds = agent.decide(p)['roleCommandMap']
        self.assertEqual(cmds['1']['action'], 'move')
        point = cmds['1']['targetPos'][0]
        self.assertLess(post_damage(t, (point['x'], point['y'])), 30)
        self.assertFalse(any(c.get('controllerId') == '1' for c in cmds.values()))

    def test_relief_starts_above_old_fixed_health_threshold(self):
        p = scenario()
        p['teamOur']['roles'][0]['health'] = 190
        t, n, ledger, pairs, posts = plan(p)
        mem = Memory()
        prepare_relief(t, n, ledger, pairs, posts, mem)
        defend(t, n, ledger, pairs, posts)
        self.assertEqual(ledger.commands['2']['action'], 'move')
        self.assertEqual(sum(c['action'] == 'attack' for c in ledger.commands.values()), 3)
        self.assertEqual(mem.relief[1][0], 2)

    def test_recent_damage_can_start_relief_without_nearby_robot(self):
        p = scenario()
        p['teamOur']['roles'][0]['health'] = 190
        p['robot']['roles'][0]['pos'] = {'x': 14, 'y': 14}
        t, n, ledger, pairs, posts = plan(p)
        mem = Memory(worker_damage={1: 50})
        prepare_relief(t, n, ledger, pairs, posts, mem)
        self.assertEqual(ledger.commands['2']['action'], 'move')

    def test_already_in_range_replacement_fires_same_round(self):
        p = scenario()
        p['teamOur']['roles'] = [r for r in p['teamOur']['roles'] if r['id'] != 22]
        p['teamOur']['roles'][0]['health'] = 120
        p['teamOur']['roles'][1]['pos'] = {'x': 4, 'y': 4}
        t, _, n, ledger = setup_case(p, layout_mode='explicit')
        pairs = [(t.units[1], w) for w in t.weapons]
        posts = {1: (4, 5)}
        mem = Memory()
        prepare_relief(t, n, ledger, pairs, posts, mem)
        defend(t, n, ledger, pairs, posts)
        self.assertEqual(set(ledger.commands), {'20', '21'})
        self.assertEqual({c['controllerId'] for c in ledger.commands.values()}, {'2'})
        self.assertEqual(posts, {2: (4, 4)})

    def test_unique_post_handoff_has_two_real_movement_rounds(self):
        p = scenario()
        p['teamOur']['roles'][0]['health'] = 120
        p['teamOur']['roles'][1]['pos'] = {'x': 3, 'y': 5}
        agent = Agent(Config(layout_mode='explicit', llm_enabled=False))
        counts = []
        for rno in (80, 81, 82):
            p['roundNo'] = rno
            cmds = agent.decide(p)['roleCommandMap']
            counts.append(sum(c['action'] == 'attack' for c in cmds.values()))
            for c in cmds.values():
                if c['action'] == 'attack':
                    self.assertNotIn(c['controllerId'], cmds)
            for role in p['teamOur']['roles']:
                action = cmds.get(str(role['id']), {})
                if action.get('action') == 'move':
                    role['pos'] = action['targetPos'][0]
        self.assertEqual(counts, [0, 0, 3])
        self.assertEqual({c['controllerId'] for c in cmds.values() if c['action'] == 'attack'}, {'2'})

    def test_expired_or_blocked_handoff_releases_outgoing_worker(self):
        for blocked in (False, True):
            p = scenario()
            if blocked:
                p['teamOur']['roles'].append(unit(80, 'wall', 4, 5))
            t = Turn(p, Config())
            mem = Memory(relief={1: (2, (4, 5), True)}, relief_started={1: 80 if blocked else 50})
            self.assertEqual(relief_excluded(t, mem), set())
            self.assertFalse(mem.relief)
            self.assertFalse(mem.relief_started)

    def test_damage_memory_ignores_skipped_rounds_and_duplicate_requests(self):
        p = scenario()
        mem = Memory(day=1, last_round=79, worker_health={1: 240})
        mem.observe(Turn(p, Config()), Config())
        self.assertEqual(mem.worker_damage[1], 40)
        mem.last_round = 78
        mem.observe(Turn(p, Config()), Config())
        self.assertEqual(mem.worker_damage, {})

    def test_cooldown_keeps_healthy_incumbent_when_alternative_is_equivalent(self):
        p = scenario()
        for r in p['teamOur']['roles'][-3:]:
            r['cooldown'] = 2
        t, _, n, ledger = setup_case(p, layout_mode='explicit')
        pairs, posts = crew_plan(t, n, ledger, previous={1: ((4, 5), (20, 21, 22))})
        self.assertEqual({h.id for h, _ in pairs}, {1})
        self.assertEqual(posts[1][0], (4, 5))

    def test_equal_health_spare_keeps_economy_instead_of_pointless_rotation(self):
        p = scenario()
        t, n, ledger, pairs, posts = plan(p)
        mem = Memory()
        prepare_relief(t, n, ledger, pairs, posts, mem)
        self.assertFalse(mem.relief)
        self.assertNotIn('2', ledger.commands)

    def test_missing_attack_power_is_not_assumed_safe(self):
        from agent.combat import post_damage
        p = scenario()
        p['robot']['roles'][0].pop('attackPower')
        p['robot']['roles'][0]['pos'] = {'x': 6, 'y': 5}
        t = Turn(p, Config())
        self.assertEqual(post_damage(t, (4, 5)), 40)
