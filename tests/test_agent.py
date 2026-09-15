import copy
import http.client
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from time import monotonic
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "SDK/SDK_Python/CoreGeek"))
from agent.brain import Agent
from agent.config import Config
from agent.model import Turn, distance
from agent.commands import Ledger, command
from agent.navigation import Navigator, layout, DeadlineExceeded
from agent.combat import defend, assignments, select_targets, line_cells, emergency_items
from agent.intelligence import Memory, Intelligence, parse_object
from agent.economy import worker, wall_keeps_access
from agent.server import Server


def unit(uid, kind, x, y, **kw):
    return {"id": uid, "roleType": kind, "pos": {"x": x, "y": y}, "health": 200,
            "backpack": [], "backPackCapability": 100, "attackRange": 8,
            "attackPower": 40, "level": 1, **kw}


def payload(round_no=1, roles=None):
    return {"roundNo": round_no, "mapInfo": {"width": 15, "height": 15, "zones": []},
            "teamOur": {"type": "challenger", "teamId": "test", "goldNum": 75,
                        "roles": roles if roles is not None else [unit(13,"station",3,11), unit(10,"worker",2,10), unit(11,"pioneer",6,10), unit(12,"worker",2,8)],
                        "playerTasks": []},
            "teamEnemy": {"roles": []}, "robot": {"roles": []}, "phaseTask": "",
            "worldNews": {}, "llmResp": "", "lastCmdResult": "", "errors": []}


def setup_case(data, **kw):
    cfg = Config(**kw)
    turn = Turn(data, cfg)
    nav = Navigator(turn, monotonic()+3)
    sites, walls = layout(turn, cfg)
    return turn, cfg, nav, Ledger(turn, cfg, sites, walls)


class ModelTests(unittest.TestCase):
    def test_day_boundaries(self):
        for r, day, is_day in [(0,1,True),(69,1,True),(70,1,False),(129,1,False),(130,2,True),(1299,10,False)]:
            t = Turn(payload(r), Config())
            self.assertEqual((t.day,t.is_day),(day,is_day))

    def test_station_footprint(self):
        t = Turn(payload(), Config())
        self.assertEqual(t.station.cells,{(3,11),(4,11),(3,10),(4,10)})

    def test_dead_units_do_not_block(self):
        p = payload(roles=[unit(1,"worker",1,1),unit(2,"wall",2,2,health=0)])
        self.assertNotIn((2,2), Turn(p,Config()).blocked)

    def test_enemy_and_neutral_block(self):
        p=payload()
        p["teamEnemy"]["roles"]=[unit(99,"station",9,4)]
        p["mapInfo"]["zones"]=[{"neutralType":"stone","pos":{"x":5,"y":5}}]
        t=Turn(p,Config())
        self.assertTrue({(9,4),(10,3),(5,5)} <= t.blocked)

    def test_two_cell_task_point(self):
        p=payload()
        p["mapInfo"]["zones"]=[{"neutralType":"challengerTaskPoint2","pos":{"x":x,"y":5}} for x in (5,6)]
        t=Turn(p,Config())
        self.assertEqual(t.task_cells({"taskPosition":{"x":5,"y":5}}),{(5,5),(6,5)})

    def test_explicit_empty_layout_builds_nothing(self):
        p=payload()
        r=Agent(Config(layout_mode="explicit",llm_enabled=False)).decide(p)
        self.assertFalse(any(c["action"]=="build" for c in r["roleCommandMap"].values()))

    def test_configuration_rejects_bad_loadout(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/"bad.json"
            path.write_text('{"loadout":["laser"]}')
            with self.assertRaises(ValueError): Config.load(path)


class NavigationTests(unittest.TestCase):
    def test_start_equals_goal(self):
        t,_,n,_=setup_case(payload(roles=[unit(1,"worker",1,1)]))
        self.assertEqual(n.search(t.heroes[0],{(1,1)}),(0,None))

    def test_diagonal_corner_cut_is_legal(self):
        p=payload(roles=[unit(1,"worker",1,1),unit(2,"wall",2,1),unit(3,"wall",1,2)])
        t,_,n,_=setup_case(p)
        self.assertEqual(n.search(t.heroes[0],{(2,2)}),(1,(2,2)))

    def test_approach_not_onto_resource(self):
        p=payload(roles=[unit(1,"worker",1,1)])
        p["mapInfo"]["zones"]=[{"neutralType":"stone","pos":{"x":3,"y":3}}]
        t,_,n,_=setup_case(p)
        self.assertEqual(n.approach(t.heroes[0],[(3,3)]),(1,(2,2)))

    def test_unreachable_and_deadline(self):
        p=payload(roles=[unit(1,"worker",1,1)]+[unit(20+i,"wall",x,y) for i,(x,y) in enumerate([(0,0),(0,1),(0,2),(1,0),(1,2),(2,0),(2,1),(2,2)])])
        t,_,n,_=setup_case(p)
        self.assertIsNone(n.search(t.heroes[0],{(8,8)}))
        t,_,_,_=setup_case(payload(roles=[unit(1,"worker",1,1)]))
        with self.assertRaises(DeadlineExceeded):
            Navigator(t,0).search(t.heroes[0],{(14,14)})

    def test_same_destination_and_swaps_rejected(self):
        t,_,_,l=setup_case(payload(roles=[unit(1,"worker",1,1),unit(2,"worker",3,1)]))
        self.assertTrue(l.add(1,command("move",(2,1))))
        self.assertFalse(l.add(2,command("move",(2,1))))
        t,_,_,l=setup_case(payload(roles=[unit(1,"worker",1,1),unit(2,"worker",2,1)]))
        self.assertFalse(l.add(1,command("move",(2,1))))


class LedgerTests(unittest.TestCase):
    def test_shared_build_budget(self):
        p=payload(roles=[unit(1,"worker",1,1),unit(2,"worker",4,1)])
        p["teamOur"]["goldNum"]=25
        t,c,n,l=setup_case(p,layout_mode="explicit",weapon_cells=[[2,1],[5,1]])
        self.assertTrue(l.add(1,command("build",(2,1),name="gatling")))
        self.assertFalse(l.add(2,command("build",(5,1),name="railgun")))
        self.assertEqual(l.gold,0)

    def test_build_limit_and_night(self):
        p=payload(71,roles=[unit(1,"worker",1,1)])
        t,c,n,l=setup_case(p,layout_mode="explicit",weapon_cells=[[2,1]])
        self.assertFalse(l.add(1,command("build",(2,1),name="gatling")))
        p["roundNo"]=1
        p["teamOur"]["roles"] += [unit(i,"gatling",i,5) for i in (3,4,5)]
        t,c,n,l=setup_case(p,layout_mode="explicit",weapon_cells=[[2,1]])
        self.assertFalse(l.add(1,command("build",(2,1),name="gatling")))

    def test_no_collect_full_or_wrong_role(self):
        p=payload(roles=[unit(1,"worker",1,1,backPackCapability=1,backpack=["stone"]),unit(2,"pioneer",1,2)])
        p["mapInfo"]["zones"]=[{"neutralType":"stone","pos":{"x":2,"y":2}}]
        t,c,n,l=setup_case(p)
        self.assertFalse(l.add(1,command("collect",(2,2))))
        self.assertFalse(l.add(2,command("collect",(2,2))))

    def test_treasure_requires_exact_inventory_counts(self):
        p=payload(roles=[unit(1,"pioneer",1,1,backpack=["StarSand"])])
        t,c,n,l=setup_case(p)
        self.assertFalse(l.add(1,command("summonTreasure",(2,2),item=["StarSand","StarSand"])))
        self.assertTrue(l.add(1,command("summonTreasure",(2,2),item=["StarSand"])))

    def test_unknown_action_and_missing_target(self):
        t,c,n,l=setup_case(payload())
        self.assertFalse(l.add(10,{"action":"wait"}))
        self.assertFalse(l.add(10,{"action":"move"}))


class CombatTests(unittest.TestCase):
    def combat(self,kind="gatling",level=1,cooldown=0):
        p=payload(71,roles=[unit(1,"worker",4,6),unit(20,kind,5,5,level=level,cooldown=cooldown),unit(13,"station",2,10)])
        p["robot"]["roles"]=[unit(31,"smallRobot",8,5,health=40,targetTeam="challenger"),unit(32,"smallRobot",8,6,health=40)]
        return p

    def test_attack_key_controller_lock_and_target_count(self):
        t,c,n,l=setup_case(self.combat(level=3))
        defend(t,n,l)
        a=l.commands["20"]
        self.assertEqual(a["controllerId"],"1")
        self.assertEqual(len(a["targetPos"]),3)
        self.assertNotIn("1",l.commands)
        self.assertFalse(l.add(1,command("move",(3,6))))

    def test_cooldown_and_day_prevent_attack(self):
        for p in (self.combat("rocket",cooldown=2), {**self.combat(),"roundNo":69}):
            t,c,n,l=setup_case(p)
            defend(t,n,l)
            self.assertNotIn("20",l.commands)

    def test_invalid_cone_rejected(self):
        t,c,n,l=setup_case(self.combat(level=2))
        self.assertFalse(l.add(20,{"action":"attack","controllerId":"1","targetPos":[{"x":8,"y":5},{"x":2,"y":5}]}))

    def test_rocket_ignores_wall_railgun_does_not(self):
        p=self.combat("railgun")
        p["teamOur"]["roles"].append(unit(70,"wall",6,5))
        p["robot"]["roles"]=p["robot"]["roles"][:1]
        t,c,n,l=setup_case(p)
        self.assertEqual(select_targets(t,t.weapons[0],{},n.deadline),[])
        p["teamOur"]["roles"][1]["roleType"]="rocket"
        t,c,n,l=setup_case(p)
        self.assertEqual(len(select_targets(t,t.weapons[0],{},n.deadline)),1)

    def test_rocket_splash_and_railgun_energy(self):
        t,c,n,l=setup_case(self.combat("rocket",level=2))
        damage={}
        self.assertEqual(len(select_targets(t,t.weapons[0],damage,n.deadline)),2)
        self.assertEqual(sum(damage.values()),60)
        p=self.combat("railgun")
        p["robot"]["roles"]=[unit(31,"smallRobot",7,5,health=10),unit(32,"largeRobot",8,5,health=500)]
        t,c,n,l=setup_case(p)
        damage={}
        self.assertEqual(select_targets(t,t.weapons[0],damage,n.deadline),[(8,5)])
        self.assertEqual(sum(damage.values()),40)

    def test_unknown_range_never_assumed_infinite(self):
        p=self.combat("rocket")
        p["teamOur"]["roles"][1]["attackRange"]=0
        t,c,n,l=setup_case(p)
        defend(t,n,l)
        self.assertFalse(l.commands)

    def test_emergency_healing_excludes_operator(self):
        p=self.combat()
        p["teamOur"]["roles"][0].update(health=20,backpack=["Medicine"])
        t,c,n,l=setup_case(p)
        emergency_items(t,l)
        defend(t,n,l)
        self.assertEqual(l.commands["1"],{"action":"use","name":"Medicine"})
        self.assertNotIn("20",l.commands)


class IntelligenceTests(unittest.TestCase):
    def test_json_only(self):
        self.assertEqual(parse_object('```json\n{"answer":"x"}\n```'),{"answer":"x"})
        self.assertIsNone(parse_object("do shell work now"))
        self.assertIsNone(parse_object("[]"))

    def test_daily_quota_and_task_exemption(self):
        p=payload()
        t=Turn(p,Config())
        m=Memory(day=1,calls=3)
        i=Intelligence(t,Config(),m)
        self.assertEqual(i.request("news","test"),"")
        p["phaseTask"]="查询天气"
        t=Turn(p,Config())
        i=Intelligence(t,Config(),m)
        self.assertEqual(i.request("task","test"),"test")
        self.assertEqual(m.calls,3)
        p["roundNo"]=131
        m.observe(Turn(p,Config()),Config())
        self.assertEqual(m.calls,0)

    def test_llm_sandbox_answer_round_trip(self):
        p=payload(roles=[unit(11,"pioneer",5,5)])
        p["phaseTask"]="测试任务：返回42"
        t,c,n,l=setup_case(p)
        m=Memory()
        m.observe(t,c)
        prompt,execute=Intelligence(t,c,m).task(l)
        self.assertTrue(prompt)
        self.assertEqual(execute,"")
        p["roundNo"]=2
        p["llmResp"]=json.dumps({"python":"print(42)"})
        t,c,n,l=setup_case(p)
        m.observe(t,c)
        prompt,execute=Intelligence(t,c,m).task(l)
        self.assertIn("python3 -c",execute)
        self.assertEqual(prompt,"")
        p["roundNo"]=3
        p["lastCmdResult"]="[exitCode:0]\n42"
        t,c,n,l=setup_case(p)
        m.observe(t,c)
        prompt,execute=Intelligence(t,c,m).task(l)
        self.assertIn("42",prompt)
        p["roundNo"]=4
        p["llmResp"]='{"answer":"42"}'
        t,c,n,l=setup_case(p)
        m.observe(t,c)
        Intelligence(t,c,m).task(l)
        self.assertEqual(l.commands["11"],{"action":"submitAnswer","taskAnswer":"42"})

    def test_skipped_turn_drops_stale_answer(self):
        p=payload(3)
        p["phaseTask"]="a"
        p["llmResp"]='{"answer":"wrong task"}'
        m=Memory(task_text="a",pending=("task",1))
        m.observe(Turn(p,Config()),Config())
        self.assertIsNone(m.answer)

    def test_failed_build_backoff(self):
        p=payload(2)
        p["lastRoundRoleActionResults"]={"10":False}
        m=Memory(last_round=1,last_commands={"10":command("build",(2,2),name="wall")})
        m.observe(Turn(p,Config()),Config())
        self.assertEqual(m.build_failures[(2,2)],22)

    def test_treasure_requires_evidence(self):
        p=payload(2)
        p["weaponShopList"]=[{"name":"StarSand","price":15}]
        prediction={"treasure":{"position":[4,4],"items":["StarSand"],"startRound":10,"endRound":20,"confidence":.99}}
        p["llmResp"]=json.dumps(prediction)
        m=Memory(pending=("news",1))
        m.observe(Turn(p,Config()),Config())
        self.assertIsNone(m.treasure)
        prediction["treasure"]["evidence"]=["test clue"]
        p["llmResp"]=json.dumps(prediction)
        m.pending=("news",1)
        m.observe(Turn(p,Config()),Config())
        self.assertIsNotNone(m.treasure)

    def test_malformed_news_retries_within_quota(self):
        p=payload()
        p["worldNews"]={"folkLegends":"test clue"}
        m=Memory()
        c=Config()
        for r in (1,2,3):
            p["roundNo"]=r
            p["llmResp"]="invalid JSON"
            t=Turn(p,c)
            m.observe(t,c)
            self.assertTrue(Intelligence(t,c,m).news())
        p["roundNo"]=4
        t=Turn(p,c)
        m.observe(t,c)
        self.assertEqual(Intelligence(t,c,m).news(),"")
        self.assertEqual(m.calls,3)

    def test_task_death_clears_pending_work(self):
        p=payload(2,roles=[])
        m=Memory(task_text="prior task",pending=("task",1),python="print(1)")
        m.observe(Turn(p,Config()),Config())
        self.assertIsNone(m.pending)
        self.assertIsNone(m.python)


class IntegrationTests(unittest.TestCase):
    def test_repo_sample(self):
        data=json.loads((ROOT/"examples/request.json").read_text())
        result=Agent().decide(data)
        self.assertEqual(set(result),{"roleCommandMap","prompt","executeCmd"})
        self.assertEqual(len(result["roleCommandMap"]),3)

    def test_duplicate_requests_do_not_spend_llm_twice(self):
        p=payload()
        p["worldNews"]={"folkLegends":"a clue"}
        agent=Agent()
        first=agent.decide(p)
        first["prompt"]="mutated"
        second=agent.decide(p)
        self.assertNotEqual(second["prompt"],"mutated")
        self.assertEqual(next(iter(agent.sessions.values())).calls,1)

    def test_round_reset_and_side_switch(self):
        agent=Agent()
        agent.decide(payload(100))
        agent.decide(payload(1))
        self.assertEqual(next(iter(agent.sessions.values())).last_round,1)
        p=payload(1)
        p["teamOur"]["type"]="defender"
        agent.decide(p)
        self.assertEqual(len(agent.sessions),2)

    def test_no_action_for_dead_team(self):
        self.assertEqual(Agent().decide(payload(roles=[]))["roleCommandMap"],{})

    def test_all_rounds_protocol_and_latency(self):
        agent=Agent(Config(llm_enabled=False))
        worst=0
        p=payload()
        for r in range(1,1301):
            p["roundNo"]=r
            start=monotonic()
            response=agent.decide(p)
            worst=max(worst,monotonic()-start)
            self.assertEqual(set(response),{"roleCommandMap","prompt","executeCmd"})
            self.assertIsInstance(response["roleCommandMap"],dict)
        self.assertLess(worst,1.0)
        print(f"\n1300 static-state protocol turns: worst={worst*1000:.2f} ms (not an engine simulation)")


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=Server(("127.0.0.1",0),Config(llm_enabled=False))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self,path="/",body=None,method="POST"):
        con=http.client.HTTPConnection("127.0.0.1",self.server.server_port,timeout=3)
        con.request(method,path,body=body,headers={"Content-Type":"application/json"})
        res=con.getresponse()
        status,data=res.status,json.loads(res.read())
        con.close()
        return status,data

    def test_post_health_and_bad_path(self):
        status,data=self.request(body=json.dumps(payload()))
        self.assertEqual(status,200)
        self.assertIn("roleCommandMap",data)
        self.assertEqual(self.request("/healthz",method="GET"),(200,{"status":"ok"}))
        self.assertEqual(self.request("/missing",method="GET")[0],404)

    def test_invalid_json_and_recovery(self):
        self.assertEqual(self.request(body="{broken")[0],400)
        self.assertEqual(self.request(body=json.dumps(payload()))[0],200)

    def test_invalid_state_has_complete_fallback(self):
        status,data=self.request(body="{}")
        self.assertEqual(status,200)
        self.assertEqual(data,{"roleCommandMap":{},"prompt":"","executeCmd":""})


if __name__ == "__main__":
    unittest.main(verbosity=2)
