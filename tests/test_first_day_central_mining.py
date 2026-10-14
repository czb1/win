"""Opening mining stays near the vendor without changing the departure chain."""
import unittest

from test_agent import setup_case, unit
from test_first_day_offense import case, cfg
from agent.brain import Agent
from agent.commands import command
from agent.intelligence import Memory
from agent.mining import mine, spare_mine
from agent.model import neighbours, pos
from agent.worker_jobs import resume_daytime_jobs


OUTER = (7, 17)
CENTRAL = (13, 21)
VENDOR = (15, 20)
MODES = ((mine, {}), (mine, {'stockpile': True}), (spare_mine, {}))


def central_case(tick=1):
    data = case(tick, gold=0)
    data['teamOur']['roles'][1]['pos'] = {'x': 10, 'y': 19}
    data['mapInfo']['zones'] = [
        {'neutralType': kind, 'pos': {'x': point[0], 'y': point[1]}, 'remain': 10}
        for kind, point in (('iron', OUTER), ('iron', CENTRAL), ('vendor', VENDOR))]
    return data


def setup(data):
    towers = [list(pos(r['pos'])) for r in data['teamOur']['roles'] if r['roleType'] == 'rocket']
    return setup_case(data, llm_enabled=False, layout_mode='explicit',
                      weapon_cells=towers, wall_cells=[])


class FirstDayCentralMiningTests(unittest.TestCase):
    def test_blocker_crosses_its_normal_sector_to_mine_near_vendor_on_both_sides(self):
        for mirror in (False, True):
            for method, options in MODES:
                with self.subTest(mirror=mirror, method=method.__name__, options=options):
                    data = central_case()
                    if mirror:
                        data['teamOur']['type'] = 'defender'
                        for team in ('teamOur', 'teamEnemy'):
                            for role in data[team]['roles']:
                                x, y = pos(role['pos'])
                                station = role['roleType'] == 'station'
                                role['pos'] = {'x': 40 - x - int(station),
                                               'y': 31 - y + int(station)}
                                role['id'] += 10000
                        for zone in data['mapInfo']['zones']:
                            x, y = pos(zone['pos'])
                            zone['pos'] = {'x': 40 - x, 'y': 31 - y}
                    turn, config, nav, ledger = setup(data)
                    hero = turn.workers[0]
                    self.assertTrue(method(turn, config, Memory(), nav, ledger, hero, **options))
                    expected = (40 - CENTRAL[0], 31 - CENTRAL[1]) if mirror else CENTRAL
                    self.assertEqual(ledger.mine_claims[hero.id], expected)
                    self.assertEqual(ledger.commands[str(hero.id)]['action'], 'move')

    def test_other_worker_keeps_its_sector_even_when_vendor_is_on_the_other_side(self):
        data = central_case()
        data['teamOur']['roles'][1]['id'] = 12
        data['teamOur']['roles'][2]['id'] = 10
        data['mapInfo']['zones'][-1]['pos'] = {'x': 5, 'y': 17}
        for method, options in MODES:
            with self.subTest(method=method.__name__, options=options):
                turn, config, nav, ledger = setup(data)
                self.assertTrue(method(turn, config, Memory(), nav, ledger, turn.units[12], **options))
                self.assertEqual(ledger.mine_claims[12], CENTRAL)

    def test_departure_night_and_later_days_keep_normal_mining(self):
        for tick in (25, 70, 130):
            for method, options in MODES:
                if tick == 70 and method is spare_mine:
                    continue
                with self.subTest(tick=tick, method=method.__name__, options=options):
                    turn, config, nav, ledger = setup(central_case(tick))
                    self.assertTrue(method(turn, config, Memory(), nav, ledger, turn.workers[0], **options))
                    self.assertEqual(ledger.mine_claims[10], OUTER)

    def test_missing_or_inaccessible_vendor_falls_back_to_normal_sector(self):
        for blocked in (False, True):
            for method, options in MODES:
                if blocked and method is mine and not options:
                    # Ordinary same-day mining already requires a reachable sale.
                    continue
                with self.subTest(blocked=blocked, method=method.__name__, options=options):
                    data = central_case()
                    if blocked:
                        data['teamOur']['roles'] += [unit(300 + i, 'wall', *p)
                                                   for i, p in enumerate(neighbours(VENDOR))]
                    else:
                        data['mapInfo']['zones'].pop()
                    turn, config, nav, ledger = setup(data)
                    self.assertTrue(method(turn, config, Memory(), nav, ledger, turn.workers[0], **options))
                    self.assertEqual(ledger.mine_claims[10], OUTER)

    def test_sale_preference_accounts_for_obstacles_instead_of_straight_line_distance(self):
        data = central_case()
        data['teamOur']['roles'] += [unit(300 + y, 'wall', 14, y) for y in range(5, 32)]
        for method, options in MODES:
            with self.subTest(method=method.__name__, options=options):
                turn, config, nav, ledger = setup(data)
                self.assertTrue(method(turn, config, Memory(), nav, ledger, turn.workers[0], **options))
                self.assertEqual(ledger.mine_claims[10], OUTER)

    def test_exhausted_blocked_or_low_value_central_mine_does_not_force_a_trip(self):
        for unavailable in ('exhausted', 'blocked', 'low_value'):
            for method, options in MODES:
                with self.subTest(unavailable=unavailable, method=method.__name__, options=options):
                    data = central_case()
                    if unavailable == 'exhausted':
                        data['mapInfo']['zones'][1]['remain'] = 0
                    elif unavailable == 'blocked':
                        data['teamOur']['roles'] += [unit(300 + i, 'wall', *p)
                                                   for i, p in enumerate(neighbours(CENTRAL))]
                    else:
                        data['mapInfo']['zones'][1]['neutralType'] = 'copper'
                        data['vendorShopList'][-1]['price'] = 1
                    turn, config, nav, ledger = setup(data)
                    self.assertTrue(method(turn, config, Memory(), nav, ledger, turn.workers[0], **options))
                    self.assertEqual(ledger.mine_claims[10], OUTER)

    def test_productive_existing_mining_job_is_not_interrupted(self):
        for method, options in MODES:
            with self.subTest(method=method.__name__, options=options):
                turn, config, nav, ledger = setup(central_case())
                memory = Memory(mine_targets={10: OUTER})
                self.assertTrue(method(turn, config, memory, nav, ledger, turn.workers[0], **options))
                self.assertEqual(ledger.mine_claims[10], OUTER)
                memory.daytime_jobs = dict(ledger.work_jobs)
                turn, config, nav, ledger = setup(central_case(2))
                resume_daytime_jobs(turn, config, memory, nav, ledger,
                                    sorted(ledger.tower_cells), [], set())
                self.assertEqual(ledger.mine_claims[10], OUTER)

    def test_tick_24_does_not_start_a_vendor_detour_that_cannot_collect_before_departure(self):
        data = central_case(24)
        data['teamOur']['roles'][1]['pos'] = {'x': 8, 'y': 18}
        data['mapInfo']['zones'][1]['pos'] = {'x': 11, 'y': 20}
        for method, options in MODES:
            with self.subTest(method=method.__name__, options=options):
                turn, config, nav, ledger = setup(data)
                self.assertTrue(method(turn, config, Memory(), nav, ledger, turn.workers[0], **options))
                self.assertEqual(ledger.commands['10'], command('collect', OUTER))

    def test_agent_mines_centrally_then_sells_and_keeps_its_wall_assignment(self):
        data = central_case()
        agent = Agent(cfg(walls=[]))
        response = agent.decide(data)['roleCommandMap']
        memory = next(iter(agent.sessions.values()))
        self.assertEqual(memory.mine_targets[10], CENTRAL)
        self.assertEqual(response['10']['action'], 'move')
        data['roundNo'] = 24
        data['teamOur']['roles'][1]['pos'] = {'x': 12, 'y': 20}
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('collect', CENTRAL))
        data['roundNo'] = 25
        data['teamOur']['roles'][1].update(pos={'x': 14, 'y': 20}, backpack=['iron'] * 5)
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(response['10'], command('sell', name='iron', num=5))
        self.assertEqual(memory.first_day_offense['phase'], 'sell')
        data['roundNo'] = 26
        data['teamOur']['roles'][1]['backpack'] = ['BossRobotSummonOrder'] * 2
        response = agent.decide(data)['roleCommandMap']
        self.assertEqual(memory.first_day_offense['phase'], 'block')
        self.assertEqual(memory.night_scout.worker_id, 10)
        self.assertEqual(response['10']['action'], 'move')
        self.assertNotIn(10, memory.mine_targets)
