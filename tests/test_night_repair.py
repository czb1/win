"""Observed construction phases and a real non-operator repair courier."""
from dataclasses import replace
import unittest
from test_agent import payload, setup_case, unit
from agent.commands import command
from agent.economy import (development_stage, choose_repairer, night_repair, prepare_night_stock,
                           supplies, use_inventory, spare_fixer)
from agent.intelligence import Memory


class NightRepairTests(unittest.TestCase):
    def case(self, day=4, night=False, backpack=(), walls=(1, 3), gun_level=3):
        p = payload((day-1)*130 + (70 if night else 40), [
            unit(1, 'worker', 6, 8, backpack=list(backpack)),
            unit(2, 'worker', 2, 8), unit(3, 'pioneer', 3, 7),
            unit(13, 'station', 3, 9, health=300),
            unit(20, 'rocket', 2, 9, level=gun_level),
            *[unit(30+i, 'wall', 7, 8+i, level=level, health=200)
              for i, level in enumerate(walls)]])
        p['teamOur']['goldNum'] = 1000
        p['mapInfo']['zones'] = [{'neutralType':'weaponShop','pos':{'x':5,'y':7}}]
        p['weaponShopList'] = [{'name':name,'price':10} for name in
            ['WallFixer','WallUpgradeVoucher1','WallUpgradeVoucher2',
             'WeaponUpgradeVoucher1','WeaponUpgradeVoucher2','StationUpgradeVoucher1','StationUpgradeVoucher2']]
        return setup_case(p, layout_mode='explicit', weapon_cells=[[2,9]], wall_cells=[[7,8],[7,9]])

    def test_towers_before_initial_walls(self):
        t,c,n,l = self.case(walls=())
        l.tower_cells.add((2,10))
        self.assertEqual(development_stage(t,c,Memory(),l)[0], 'towers')

    def test_initial_walls_before_gun_upgrades_even_with_carried_voucher(self):
        t,c,n,l = self.case(walls=(1,), gun_level=1, backpack=['WeaponUpgradeVoucher1'])
        m=Memory()
        self.assertEqual(development_stage(t,c,m,l)[0], 'initial_walls')
        self.assertFalse(use_inventory(t,n,l,t.workers[0],mem=m))

    def test_guns_before_later_holes(self):
        t,c,n,l = self.case(walls=(1,), gun_level=2)
        self.assertEqual(development_stage(t,c,Memory(initial_walls_complete=True),l)[0], 'guns')

    def test_holes_before_wall_upgrades(self):
        t,c,n,l = self.case(walls=(1,))
        self.assertEqual(development_stage(t,c,Memory(initial_walls_complete=True),l)[0], 'gaps')

    def test_rebuilt_wall_target_uses_edge_neighbours_and_stays_fixed(self):
        t,c,n,l = self.case(walls=(2,))
        m=Memory(initial_walls_complete=True,wall_levels={(7,9):3})
        development_stage(t,c,m,l)
        self.assertNotIn((7,9),m.rebuild_levels)
        t.ours += (replace(t.ours[-1], id=31, pos=(7,9), level=1),)
        t.ours = tuple(replace(w,level=3) if w.id==30 else w for w in t.ours)
        del l.development
        self.assertEqual(development_stage(t,c,m,l)[0], 'catchup')
        self.assertEqual(m.rebuild_levels[(7,9)],3)
        t.ours = tuple(replace(w,level=2) if w.id==30 else w for w in t.ours)
        del l.development
        development_stage(t,c,m,l)
        self.assertEqual(m.rebuild_levels[(7,9)],3)

    def test_wall_upgrade_precedes_wounded_base(self):
        t,c,n,l = self.case(backpack=['WallFixer'])
        m=Memory()
        plan=supplies(t,c,m,n,l,t.workers[0],bulk=True)
        self.assertEqual(plan[0],'WallUpgradeVoucher1')
        self.assertEqual(development_stage(t,c,m,l)[0],'walls')

    def test_base_only_after_walls_level_three(self):
        t,c,n,l = self.case(walls=(3,3),backpack=['WallFixer','WallFixer','WallFixer'])
        self.assertEqual(development_stage(t,c,Memory(),l)[0],'base')

    def test_healthy_walls_still_get_one_night_package(self):
        t,c,n,l=self.case()
        t.ours=tuple(replace(w,health=2000) if w.kind=='wall' else w for w in t.ours)
        plan=supplies(t,c,Memory(),n,l,t.workers[0],stock_only=True)
        self.assertEqual((plan[0],plan[2]),('WallFixer',1))

    def test_only_night_package_is_not_spent_in_daylight(self):
        t,c,n,l=self.case(backpack=['WallFixer'])
        self.assertFalse(spare_fixer(t,Memory(stock_carrier_id=1),t.workers[0]))

    def test_packages_on_gunner_do_not_cover_healer_stock(self):
        t,c,n,l=self.case()
        t.heroes = tuple(replace(h,backpack=('WallFixer',)) if h.id==2 else h for h in t.heroes)
        from agent.economy import night_stock_missing
        self.assertTrue(night_stock_missing(t,Memory(stock_carrier_id=1)))

    def test_pending_stock_purchase_does_not_reserve_gold_twice(self):
        t,c,n,l=self.case(walls=(3,3),gun_level=1)
        m=Memory(initial_walls_complete=True)
        # The stock courier already reserved 10; another 10 still buys a voucher.
        l.gold=10
        l.purchases.add('WallFixer')
        plan=supplies(t,c,m,n,l,t.workers[0])
        self.assertEqual(plan[0],'WeaponUpgradeVoucher1')

    def test_fourth_night_selects_non_operator(self):
        t,c,n,l=self.case(night=True,backpack=['WallFixer'])
        m=Memory(gunner_id=2,gunner_post=(2,8))
        h=choose_repairer(t,m,[(t.workers[1],t.weapons[0])])
        self.assertEqual(h.id,1)
        night_repair(t,c,m,n,l,h)
        self.assertEqual(l.commands['1'],command('use',(7,8),name='WallFixer'))
        self.assertNotIn(2,l.used)

    def test_third_night_does_not_assign_repair_role(self):
        t,c,n,l=self.case(day=3,night=True)
        m=Memory()
        choose_repairer(t,m,[(t.workers[1],t.weapons[0])])
        self.assertIsNone(m.repairer_id)

    def test_healer_without_packages_waits_instead_of_mining(self):
        t,c,n,l=self.case(night=True)
        night_repair(t,c,Memory(gunner_post=(2,8)),n,l,t.workers[0])
        self.assertIn(1,l.used)
        self.assertNotIn(l.commands.get('1',{}).get('action'),('collect','buy'))

    def test_active_pioneer_task_is_not_reassigned(self):
        t,c,n,l=self.case(night=True)
        t.phase_task='active'
        self.assertIsNone(choose_repairer(t,Memory(),[(h,t.weapons[0]) for h in t.workers]))

    def test_replacement_operator_is_excluded_from_healing(self):
        t,c,n,l=self.case(night=True)
        m=Memory(repairer_id=1)
        h=choose_repairer(t,m,[(t.workers[0],t.weapons[0])])
        self.assertEqual(h.id,2)

    def test_distant_healer_walks_before_using_package(self):
        t,c,n,l=self.case(night=True,backpack=['WallFixer'])
        h=replace(t.workers[0],pos=(4,7))
        t.blocked=(t.blocked-{t.workers[0].pos})|{h.pos}
        t.units[h.id]=h
        night_repair(t,c,Memory(gunner_post=(2,8)),n,l,h)
        self.assertEqual(l.commands['1']['action'],'move')

    def test_brain_switches_from_mining_to_repair_on_fourth_night(self):
        from agent.brain import Agent
        for day in (3,4):
            t,c,n,l=self.case(day=day,night=True,backpack=['WallFixer'])
            p=t.raw
            p['mapInfo']['zones'].append({'neutralType':'copper','pos':{'x':6,'y':9}})
            # No local fixer use on night 3: keep the damaged wall one step away.
            p['teamOur']['roles'][0]['pos']={'x':5,'y':8}
            response=Agent(c).decide(p)['roleCommandMap']
            if day==4:
                self.assertEqual(response['1']['action'],'move')
                self.assertIn(response['1']['targetPos'], [[{'x':6,'y':8}], [{'x':6,'y':7}]])
            else:
                self.assertEqual(response['1']['action'],'collect')

if __name__=='__main__':
    unittest.main()
