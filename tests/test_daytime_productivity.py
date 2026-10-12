"""Daytime production survives optional defence, traffic and scout planning."""
import unittest
from unittest.mock import patch

from test_agent import payload, unit, setup_case
import test_wall_watch
from agent.brain import Agent
from agent.combat import clear_gunner_route
from agent.commands import command
from agent.config import Config
from agent.daytime import finish_daytime_work
from agent.economy import workers, dusk_resources, refresh_stone_reserves
from agent.economy_plan import reserve_income_worker
from agent.intelligence import Memory
from agent.mining import mine
from agent.scouting import ScoutMemory, prepare_night_scout
from agent.wall_watch import prepare_watch
from agent.worker_jobs import resume_daytime_jobs, save_daytime_jobs
from agent.wall_batches import continue_batch


def income_case(tick=0, backpack=()):
    data = payload(650 + tick, [unit(13, 'station', 5, 11, level=3),
        unit(1, 'worker', 4, 5, health=500, backpack=list(backpack)),
        unit(2, 'worker', 8, 8, health=500),
        unit(11, 'pioneer', 4, 11, health=500),
        unit(20, 'rocket', 4, 12, level=3),
        unit(21, 'rocket', 5, 12, level=3),
        unit(22, 'rocket', 4, 10, level=3)])
    data['mapInfo']['zones'] = [
        dict(neutralType='copper', pos=dict(x=5, y=5), remain=10),
        dict(neutralType='stone', pos=dict(x=9, y=9), remain=10),
        dict(neutralType='vendor', pos=dict(x=6, y=6)),
        dict(neutralType='weaponShop', pos=dict(x=8, y=7))]
    data['vendorShopList'] = [dict(name='copper', price=10), dict(name='stone', price=2)]
    data['weaponShopList'] = [dict(name='WallFixer', price=15),
                            dict(name='WallUpgradeVoucher1', price=20)]
    return data


def income_setup(data):
    return setup_case(data, layout_mode='explicit',
        weapon_cells=[[4, 12], [5, 12], [4, 10]], wall_cells=[[7, 8], [7, 9]])


class DaytimeProductivityTests(unittest.TestCase):
    def gate_case(self, tick=38):
        data = payload(650 + tick, [unit(13, 'station', 9, 22),
            unit(10010, 'worker', 11, 20, health=500, backpack=['WallFixer'] * 2),
            unit(10012, 'worker', 6, 22, health=500),
            unit(10011, 'pioneer', 6, 18, health=500),
            unit(20, 'rocket', 8, 23, level=3),
            unit(21, 'rocket', 9, 23, level=3),
            unit(22, 'rocket', 8, 21, level=3)])
        data['mapInfo'].update(width=41, height=32)
        walls = [[7, y] for y in range(32) if y != 21]
        data['teamOur']['roles'] += [unit(100 + i, 'wall', *p) for i, p in enumerate(walls)]
        data['mapInfo']['zones'] = [dict(neutralType='copper', pos=dict(x=4, y=22), remain=10),
                                  dict(neutralType='vendor', pos=dict(x=10, y=19))]
        data['vendorShopList'] = [dict(name='copper', price=10)]
        cfg = Config(llm_enabled=False, layout_mode='explicit',
            weapon_cells=[[8, 23], [9, 23], [8, 21]], wall_cells=walls)
        return data, cfg

    def test_only_watch_return_does_not_reserve_idle_gunner_corridor(self):
        for tick in (38, 47):
            with self.subTest(tick=tick):
                data, cfg = self.gate_case(tick)
                agent = Agent(cfg)
                with patch('agent.brain.prepare_watch', return_value=(True, 0)), \
                        patch('agent.brain.clear_gunner_route', wraps=clear_gunner_route) as clear:
                    result = agent.decide(data)['roleCommandMap']
                mem = next(iter(agent.sessions.values()))
                self.assertEqual(mem.gunner_id, 10011)
                self.assertEqual(mem.gunner_post, (8, 22))
                clear.assert_not_called()
                self.assertIn('10012', result)
                self.assertIn(result['10012']['action'], ('move', 'collect', 'sell'))

    def test_actual_gunner_recall_still_clears_its_corridor(self):
        data, cfg = self.gate_case(tick=67)
        with patch('agent.brain.clear_gunner_route', wraps=clear_gunner_route) as clear:
            Agent(cfg).decide(data)
        clear.assert_called_once()

    def test_late_wall_phase_keeps_one_worker_earning_all_day(self):
        for tick in (0, 35, 50):
            with self.subTest(tick=tick):
                turn, cfg, nav, ledger = income_setup(income_case(tick))
                mem = Memory(wall_watch_id=2, preparation_workers={1, 2})
                workers(turn, cfg, mem, nav, ledger, list(ledger.tower_cells), list(ledger.wall_cells))
                self.assertEqual(ledger.income_worker_id, 1)
                self.assertNotIn(1, mem.preparation_workers)
                self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))
                self.assertFalse(ledger.work_jobs[1]['want_stone'])

    def test_income_job_continues_past_optional_preparation_cutoff(self):
        turn, cfg, nav, ledger = income_setup(income_case(50))
        job = dict(kind='mine', target=(5, 5), ore='copper', want_stone=False,
                   stockpile=False, deadline=35, stone_goal=None)
        mem = Memory(wall_watch_id=2, preparation_tick=0, daytime_jobs={1: job})
        reserve_income_worker(turn, mem, nav, ledger)
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells, set())
        self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))
        self.assertEqual(ledger.work_jobs[1]['deadline'], 70)

    def test_old_optional_material_job_releases_to_income_in_same_turn(self):
        turn, cfg, nav, ledger = income_setup(income_case())
        mem = Memory(wall_watch_id=2, preparation_workers={1}, supply_worker=1,
            daytime_jobs={1: dict(kind='mine', target=(9, 9), ore='stone', want_stone=True,
                                 stockpile=False, deadline=None, stone_goal=2)})
        reserve_income_worker(turn, mem, nav, ledger)
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells, set())
        self.assertNotIn(1, mem.daytime_jobs)
        workers(turn, cfg, mem, nav, ledger, list(ledger.tower_cells), list(ledger.wall_cells))
        save_daytime_jobs(turn, mem, ledger)
        self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))
        self.assertFalse(mem.daytime_jobs[1]['want_stone'])

    def test_dusk_purchase_cannot_take_reserved_income_worker(self):
        data = income_case(50)
        data['teamOur']['goldNum'] = 300
        data['teamOur']['roles'] += [unit(31, 'wall', 7, 8)]
        turn, cfg, nav, ledger = income_setup(data)
        mem = Memory(wall_watch_id=2)
        reserve_income_worker(turn, mem, nav, ledger)
        dusk_resources(turn, cfg, mem, nav, ledger, ledger.tower_cells)
        self.assertNotIn(1, ledger.used)
        workers(turn, cfg, mem, nav, ledger, list(ledger.tower_cells), list(ledger.wall_cells))
        self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))

    def test_existing_wall_face_batch_releases_reserved_income_worker(self):
        for phase in ('build', 'maintain', 'economy'):
            with self.subTest(phase=phase):
                turn, cfg, nav, ledger = income_setup(income_case())
                job = dict(kind='mine', target=(9, 9), ore='stone', want_stone=True,
                           stockpile=False, deadline=None, stone_goal=2)
                mem = Memory(wall_watch_id=2, daytime_jobs={1: job}, wall_batches={
                    1: dict(sites=((7, 8), (7, 9)), phase=phase, day=6)})
                reserve_income_worker(turn, mem, nav, ledger)
                self.assertNotIn(1, mem.wall_batches)
                resume_daytime_jobs(turn, cfg, mem, nav, ledger,
                                    ledger.tower_cells, ledger.wall_cells, set())
                workers(turn, cfg, mem, nav, ledger, list(ledger.tower_cells), list(ledger.wall_cells))
                self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))

    def test_face_batch_continuation_cannot_bypass_income_allocation(self):
        turn, cfg, nav, ledger = income_setup(income_case())
        ledger.income_worker_id = 1
        mem = Memory(wall_batches={1: dict(sites=((7, 8), (7, 9)), phase='build', day=6)})
        self.assertFalse(continue_batch(turn, cfg, mem, nav, ledger, turn.workers[0],
                                        ledger.tower_cells, ledger.wall_cells))
        self.assertNotIn(1, ledger.used)
        self.assertNotIn(1, mem.wall_batches)

    def test_optional_blueprint_does_not_reserve_income_workers_sale_stone(self):
        turn, cfg, nav, ledger = income_setup(income_case(backpack=['stone'] * 3))
        mem = Memory(wall_watch_id=2)
        reserve_income_worker(turn, mem, nav, ledger)
        refresh_stone_reserves(turn, cfg, mem, ledger)
        self.assertEqual(mem.stone_reserves[1], 0)

    def test_paid_courier_and_recalled_worker_are_not_selected_for_income(self):
        for paid, excluded in ((['WallUpgradeVoucher1'], set()), ([], {1})):
            turn, _, nav, ledger = income_setup(income_case(backpack=paid))
            mem = Memory(income_worker_id=1, wall_watch_id=2)
            reserve_income_worker(turn, mem, nav, ledger, excluded)
            self.assertEqual(ledger.income_worker_id, 2)

    def test_carried_ore_keeps_its_sale_job_when_no_mines_remain(self):
        data = income_case(50, backpack=['copper'] * 40)
        data['mapInfo']['zones'] = [z for z in data['mapInfo']['zones']
                                  if z['neutralType'] in ('vendor', 'weaponShop')]
        turn, cfg, nav, ledger = income_setup(data)
        mem = Memory(wall_watch_id=2)
        workers(turn, cfg, mem, nav, ledger, list(ledger.tower_cells), list(ledger.wall_cells))
        self.assertEqual(ledger.income_worker_id, 1)
        self.assertEqual(ledger.work_jobs[1]['kind'], 'sell')
        # Supply a new observed state at the submitted, legal vendor approach.
        data['roundNo'] += 1
        data['teamOur']['roles'][1]['pos'] = ledger.commands['1']['targetPos'][0]
        turn, cfg, nav, ledger = income_setup(data)
        workers(turn, cfg, mem, nav, ledger, list(ledger.tower_cells), list(ledger.wall_cells))
        self.assertEqual(ledger.commands['1'], dict(action='sell', name='copper', num=40))

    def test_breach_repair_uses_other_worker_before_preempting_income(self):
        data = income_case(40)
        data['teamOur']['roles'][2]['backpack'] = ['stone']
        turn, cfg, nav, ledger = income_setup(data)
        job = dict(kind='mine', target=(5, 5), ore='copper', want_stone=False,
                   stockpile=False, deadline=35, stone_goal=None)
        mem = Memory(wall_watch_id=2, daytime_jobs={1: job}, wall_rebuild_levels={(7, 8): 2})
        reserve_income_worker(turn, mem, nav, ledger)
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells, set())
        self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))
        self.assertEqual(ledger.commands['2'], command('build', (7, 8), name='wall'))

    def test_daytime_scout_cannot_take_last_income_worker_or_arrive_after_dark(self):
        data = income_case(50)
        data['teamEnemy']['roles'] = [unit(99, 'station', 12, 2)]
        turn, _, nav, ledger = income_setup(data)
        ledger.income_worker_id = 1
        memory = ScoutMemory(worker_id=1)
        self.assertIsNone(prepare_night_scout(turn, memory, nav, ledger, excluded={2}))
        self.assertIsNone(memory.worker_id)
        ledger.income_worker_id = None
        # The route is feasible spatially but cannot produce any daylight sight.
        with patch('agent.scouting._routes', return_value=[((10, 2), (23, (5, 4)), 5, 4)]):
            self.assertIsNone(prepare_night_scout(turn, memory, nav, ledger, excluded={2}))
        self.assertFalse(ledger.used)
        with patch('agent.scouting._routes', return_value=[((10, 2), (19, (5, 4)), 5, 4)]):
            self.assertEqual(prepare_night_scout(turn, memory, nav, ledger, excluded={2}).id, 1)
        self.assertEqual(ledger.commands['1'], command('move', (5, 4)))

    def test_breach_can_borrow_income_worker_when_other_worker_cannot_act(self):
        data = income_case(40, backpack=['stone'])
        data['teamOur']['roles'][1]['pos'] = dict(x=6, y=8)
        data['teamOur']['roles'][2]['health'] = 100
        turn, cfg, nav, ledger = income_setup(data)
        job = dict(kind='mine', target=(5, 5), ore='copper', want_stone=False,
                   stockpile=False, deadline=35, stone_goal=None)
        mem = Memory(wall_watch_id=2, daytime_jobs={1: job}, wall_rebuild_levels={(7, 8): 2})
        reserve_income_worker(turn, mem, nav, ledger)
        self.assertEqual(ledger.income_worker_id, 1)
        resume_daytime_jobs(turn, cfg, mem, nav, ledger, ledger.tower_cells, ledger.wall_cells, set())
        self.assertEqual(ledger.commands['1'], command('build', (7, 8), name='wall'))
        self.assertIsNone(ledger.income_worker_id)
        self.assertEqual(mem.wall_repair_worker, 1)

    def watch_setup(self, tick=47, gold=0, distant_shop=False):
        data = test_wall_watch.WallWatchTests().case(day=6, tick=tick, packs=2, damaged=False)
        data['robot']['roles'] = []
        data['teamOur']['goldNum'] = gold
        for role in data['teamOur']['roles']:
            if role['roleType'] == 'rocket':
                role['level'] = 3
        data['mapInfo']['zones'].append(dict(neutralType='copper', pos=dict(x=6, y=9), remain=10))
        data['vendorShopList'] = [dict(name='copper', price=5)]
        if distant_shop:
            data['mapInfo'].update(width=41, height=32)
            # Clear the physical gate so this check isolates procurement cost.
            data['teamOur']['roles'][1]['pos'] = dict(x=0, y=0)
            data['mapInfo']['zones'] = [z for z in data['mapInfo']['zones']
                                      if z['pos'] != dict(x=6, y=9)]
            next(z for z in data['mapInfo']['zones'] if z['neutralType'] == 'weaponShop')['pos'] = dict(x=26, y=4)
            return setup_case(data, layout_mode='explicit',
                weapon_cells=[[r['pos']['x'], r['pos']['y']] for r in data['teamOur']['roles']
                              if r['roleType'] == 'rocket'],
                wall_cells=[[r['pos']['x'], r['pos']['y']] for r in data['teamOur']['roles']
                            if r['roleType'] == 'wall'])
        return setup_case(data)

    def test_scout_uses_feasible_post_when_preferred_post_misses_deadline(self):
        data = income_case(50)
        data['teamEnemy']['roles'] = [unit(99, 'station', 12, 2)]
        turn, _, nav, ledger = income_setup(data)
        routes = [((11, 2), (23, (5, 4)), 6, 4), ((10, 2), (19, (5, 4)), 5, 4)]
        memory = ScoutMemory()
        with patch('agent.scouting._routes', return_value=routes):
            scout = prepare_night_scout(turn, memory, nav, ledger, excluded={2})
        self.assertEqual(scout.id, 1)
        self.assertEqual(memory.post, (10, 2))
        self.assertEqual(ledger.notes[1]['conditions']['route_steps'], 19)

    def test_unfunded_partial_stock_releases_with_twenty_three_turns_left(self):
        turn, cfg, nav, ledger = self.watch_setup()
        mem = Memory(wall_watch_id=2)
        self.assertEqual(prepare_watch(turn, cfg, mem, nav, ledger, turn.workers[1], ledger.wall_cells), (False, 0))
        self.assertNotIn(2, ledger.daytime_waits)

    def test_unfunded_far_shop_does_not_set_the_recall_deadline(self):
        turn, cfg, nav, ledger = self.watch_setup(tick=20, distant_shop=True)
        mem = Memory(wall_watch_id=2)
        self.assertEqual(prepare_watch(turn, cfg, mem, nav, ledger, turn.workers[1], ledger.wall_cells), (False, 0))

    def test_distant_single_pack_is_deferred_without_reserving_optional_stock(self):
        turn, cfg, nav, ledger = self.watch_setup(tick=0, gold=26, distant_shop=True)
        # The official repair-pack price leaves only one pack affordable.
        turn.shop['WallFixer'] = 15
        mem = Memory(wall_watch_id=2)
        self.assertIsNotNone(nav.approach(turn.workers[1], [(26, 4)]))
        self.assertEqual(prepare_watch(turn, cfg, mem, nav, ledger, turn.workers[1], ledger.wall_cells), (False, 15))
        self.assertNotIn(2, ledger.used)
        self.assertEqual(ledger.watch_pack_slots[2], 1)

    def test_stale_watch_ready_can_collect_locally_but_final_recall_still_waits(self):
        for tick, productive in ((47, True), (69, False)):
            turn, cfg, nav, ledger = self.watch_setup(tick=tick)
            mem = Memory(wall_watch_id=2)
            ledger.daytime_waits[2] = 'watch_ready'
            finish_daytime_work(turn, cfg, mem, nav, ledger, {2})
            self.assertEqual(ledger.commands.get('2'), command('collect', (6, 9)) if productive else None)

    def test_blocked_return_allows_in_place_collection_for_ordinary_worker(self):
        turn, cfg, nav, ledger = income_setup(income_case(40))
        ledger.operator_posts[1] = (12, 12)
        ledger.reserved.add((12, 12))
        finish_daytime_work(turn, cfg, Memory(), nav, ledger, set())
        self.assertEqual(ledger.commands['1'], command('collect', (5, 5)))

    def test_stone_choice_compares_delivery_instead_of_only_outbound_walk(self):
        data = payload(650, [unit(13, 'station', 2, 5), unit(1, 'worker', 18, 4)])
        data['mapInfo'].update(width=41, height=32)
        data['mapInfo']['zones'] = [dict(neutralType='stone', pos=dict(x=x, y=4), remain=10)
                                  for x in (21, 5)]
        turn, cfg, nav, ledger = setup_case(data, layout_mode='explicit', wall_cells=[[2, 2]])
        self.assertTrue(mine(turn, cfg, Memory(), nav, ledger, turn.workers[0],
                             want_stone=True, stone_goal=1, stone_sites=[(2, 2)]))
        # The nearby mine sends the load away from the delivery point. The
        # farther outbound mine lies on the useful trip toward construction.
        self.assertEqual(ledger.mine_claims[1], (5, 4))
        self.assertGreater(ledger.notes[1]['conditions']['construction_steps'], 0)
