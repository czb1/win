"""Two-task learning/reuse and evidence isolation with independent data."""
from copy import deepcopy
import ast
import json
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from urllib.parse import parse_qs, urlsplit
import tempfile
import unittest

from test_agent import setup_case
from test_evolution import task_payload
from test_task_completion import run_sandbox
from test_task_runtime import heritage_api, install_transport_double
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Intelligence, Memory
from agent.model import Turn
from agent.task_skills import bind_recipe, compatible, learned_method, recipe_proposal


FILE_RECIPE = {
    'parameters': {'source': 'str', 'city': 'str'},
    'python': """import json
from pathlib import Path
records = json.loads(Path(PARAMS['source']).read_text())
rows = records[PARAMS['city']]
assert isinstance(rows, list) and all(type(x) is int for x in rows)
print('FINAL_ANSWER')
print(json.dumps({'city': PARAMS['city'], 'sum': sum(rows)}, ensure_ascii=False))
"""}
CONTRACT = {'example': {'city': 'example', 'sum': 0}}


def step(mem, round_no, description='query', **updates):
    payload = task_payload(round_no, description)
    payload.update(updates)
    turn, cfg, _, ledger = setup_case(payload)
    mem.observe(turn, cfg)
    prompt, code = Intelligence(turn, cfg, mem).task(ledger)
    return prompt, code, ledger


class SkillTests(unittest.TestCase):
    def test_agent_learns_recipe_then_rebinds_new_city_and_source(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory, 'one.json')
            second = Path(directory, 'two.json')
            first.write_text(json.dumps({'北京': [3, 7]}))
            second.write_text(json.dumps({'上海': [100, 25, -2]}))
            agent = Agent(Config(layout_mode='explicit'))
            p = task_payload(1, 'query first city')
            agent.decide(p)
            mem = next(iter(agent.sessions.values()))
            mem.contract = deepcopy(CONTRACT)
            p.update(roundNo=2, llmResp=json.dumps({'skill': FILE_RECIPE,
                        'inputs': {'source': str(first), 'city': '北京'}}, ensure_ascii=False))
            execution = agent.decide(p)
            self.assertTrue(execution['executeCmd'])
            self.assertFalse(mem.skills)  # Not learned from syntax or sandbox success alone.
            p.update(roundNo=3, llmResp='', lastCmdResult=run_sandbox(execution, directory))
            result = agent.decide(p)
            self.assertEqual(json.loads(result['roleCommandMap']['11']['taskAnswer']), {'city': '北京', 'sum': 10})
            self.assertFalse(mem.skills)
            p.update(roundNo=4, phaseTask='', lastCmdResult='', lastRoundRoleActionResults={'11': True})
            agent.decide(p)
            self.assertEqual(len(mem.skills), 1)
            learned = mem.skills[0]
            stored = json.dumps(learned, ensure_ascii=False)
            self.assertNotIn(str(first), stored)
            self.assertNotIn('北京', stored)
            self.assertEqual(ast.dump(ast.parse(learned['recipe']['python'])),
                             ast.dump(ast.parse(FILE_RECIPE['python'])))
            first.unlink()  # Reusing the previous data/path would now fail.
            p = task_payload(5, 'query second city')
            agent.decide(p)
            mem.contract = deepcopy(CONTRACT)
            mem.pending = None
            prompt, _, _ = step(mem, 6, 'query second city')
            self.assertIn(learned['id'], prompt)
            p = task_payload(7, 'query second city')
            p['llmResp'] = json.dumps({'use_skill': learned['id'],
                                      'inputs': {'source': str(second), 'city': '上海'}}, ensure_ascii=False)
            execution = agent.decide(p)
            self.assertTrue(execution['executeCmd'])
            p.update(roundNo=8, llmResp='', lastCmdResult=run_sandbox(execution, directory))
            result = agent.decide(p)
            self.assertEqual(json.loads(result['roleCommandMap']['11']['taskAnswer']), {'city': '上海', 'sum': 123})
            p.update(roundNo=9, phaseTask='', lastCmdResult='', lastRoundRoleActionResults={'11': True})
            agent.decide(p)
            self.assertEqual(len(mem.skills), 1)
            self.assertEqual(mem.skills[0]['successes'], 2)

    def test_model_restatement_retains_method_evidence_but_not_answer(self):
        mem = Memory(task_text='query', task_point=(6, 5), task_started=1, bootstrap_done=True,
                     pending=('cmd', 1), running_python='query_current_api()', contract=deepcopy(CONTRACT))
        report = {'http_successes': 1, 'http_errors': [], 'http_calls': [{
            'method': 'GET', 'endpoint': 'http://localhost:8899/api/search',
            'auth': 'Bearer', 'parameters': ['location']}]}
        answer = '{"city":"北京","sum":999}'
        step(mem, 2, lastCmdResult='[exitCode:0]\nTASK_RUNTIME '+json.dumps(report)+'\n'+answer)
        _, _, ledger = step(mem, 3, llmResp='ANSWER\n'+answer)
        self.assertEqual(ledger.commands['11']['taskAnswer'], answer)
        self.assertIsNotNone(mem.submitted_method)
        step(mem, 4, '', lastRoundRoleActionResults={'11': True})
        self.assertEqual(len(mem.skills), 1)
        self.assertEqual(mem.skills[0]['interfaces'][0]['auth'], 'Bearer')
        serialized = json.dumps(mem.skills, ensure_ascii=False)
        self.assertNotIn('北京', serialized)
        self.assertNotIn('999', serialized)
        self.assertNotIn('query_current_api()', serialized)

    def test_unsupported_model_answer_does_not_create_skill(self):
        mem = Memory(task_text='query', task_point=(6, 5), task_started=1, bootstrap_done=True,
                     pending=('task', 1), supported_python='query()', supported_output='42')
        step(mem, 2, llmResp='ANSWER\n99')
        step(mem, 3, '', lastRoundRoleActionResults={'11': True})
        self.assertFalse(mem.skills)

    def test_failure_disables_recipe_and_revised_success_replaces_it(self):
        old = learned_method((6, 5), CONTRACT, FILE_RECIPE)
        mem = Memory(task_text='query', task_point=(6, 5), task_started=1, bootstrap_done=True,
                     pending=('task', 1), contract=deepcopy(CONTRACT), skills=[old])
        with tempfile.TemporaryDirectory() as directory:
            inputs = {'source': str(Path(directory, 'missing.json')), 'city': 'changed'}
            _, cmd, _ = step(mem, 2, llmResp=json.dumps({'use_skill': old['id'], 'inputs': inputs}))
            raw = run_sandbox({'executeCmd': cmd}, directory)
            prompt, _, ledger = step(mem, 3, lastCmdResult=raw)
            self.assertFalse(ledger.commands)
            self.assertTrue(old['disabled'])
            self.assertFalse(compatible(old, (6, 5), CONTRACT))
            self.assertNotIn(old['id'], prompt)
            # A repaired proposal must execute on fresh data before promotion.
            source = Path(directory, 'now.json')
            source.write_text(json.dumps({'changed': [6]}))
            inputs['source'] = str(source)
            _, cmd, _ = step(mem, 4, llmResp=json.dumps({'skill': FILE_RECIPE, 'inputs': inputs}))
            _, _, ledger = step(mem, 5, lastCmdResult=run_sandbox({'executeCmd': cmd}, directory))
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer'])['sum'], 6)
            step(mem, 6, '', lastRoundRoleActionResults={'11': True})
            self.assertEqual(len(mem.skills), 1)
            self.assertFalse(mem.skills[0]['disabled'])

    def test_judger_error_not_legal_action_confirms_failure(self):
        old = learned_method((6, 5), {}, FILE_RECIPE)
        mem = Memory(task_text='query', task_point=(6, 5), task_started=1,
                     active_skill=old['id'], skills=[old], submitted=(2, '42'),
                     submitted_method=old.copy())
        step(mem, 3, lastRoundRoleActionResults={'11': True},
             errors=[{'errorCode': 2, 'description': 'wrong answer'}])
        self.assertTrue(old['disabled'])
        self.assertEqual(old['successes'], 1)

    def test_all_inputs_required_and_task_shape_must_match(self):
        with self.assertRaises(ValueError):
            bind_recipe(FILE_RECIPE, {'city': 'new'}, 12000)
        old = learned_method((6, 5), CONTRACT, FILE_RECIPE)
        self.assertFalse(compatible(old, (7, 5), CONTRACT))
        self.assertFalse(compatible(old, (6, 5), {'example': {'token': 'xxx'}}))
        mem = Memory(task_text='query', task_point=(6, 5), task_started=1,
                     pending=('task', 1), skills=[old])
        _, cmd, _ = step(mem, 2, llmResp=json.dumps({'use_skill': old['id'],
                            'inputs': {'city': 'new', 'source': 'new.json'}}))
        self.assertFalse(cmd)
        self.assertIsNone(mem.recipe_candidate)

    def test_literal_inputs_credentials_and_workspace_are_not_saved_in_recipe(self):
        for code in (FILE_RECIPE['python'] + "\nprint('北京')",
                     FILE_RECIPE['python'] + "\nkey = 'Bearer secret'",
                     FILE_RECIPE['python'] + "\npath = '/tmp/old/ws_1'"):
            with self.assertRaises(ValueError):
                recipe_proposal({**FILE_RECIPE, 'python': code},
                                {'city': '北京', 'source': 'current.json'}, 12000)

    def test_runtime_observes_protocol_not_key_city_or_payload(self):
        from test_task_runtime import execute
        from agent.task_runtime import runtime_result
        with heritage_api() as (base, calls), tempfile.TemporaryDirectory() as directory:
            install_transport_double(directory)
            code = ("import requests\n"
                    f"r=requests.get({base!r}+'/search', headers={{'Authorization':'Bearer fixture-key'}}, "
                    "params={'location':'北京','page':1})\nprint(r.json())")
            _, report = runtime_result(execute(code, directory))
            self.assertEqual(report['http_calls'][0]['auth'], 'Bearer')
            self.assertEqual(report['http_calls'][0]['parameters'], ['location', 'page'])
            self.assertNotIn('fixture-key', json.dumps(report['http_calls']))
            self.assertNotIn('北京', json.dumps(report['http_calls'], ensure_ascii=False))
            self.assertEqual(calls[0][2], 'Bearer fixture-key')

    def test_http_recipe_reuses_bearer_location_and_pagination_with_new_key(self):
        calls = []
        datasets = {'北京': ('first-secret', [2, 5, 8]), '上海': ('rotated-secret', [50, 70])}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                params = parse_qs(urlsplit(self.path).query)
                city = params.get('location', [''])[0]
                key, values = datasets.get(city, ('', []))
                authorization = self.headers.get('Authorization')
                calls.append((city, authorization))
                if not key or authorization != 'Bearer ' + key:
                    status, payload = 401, {'error': 'wrong current credentials'}
                else:
                    page = int(params['page'][0])
                    status, payload = 200, {'items': values[(page-1)*2:page*2], 'total': len(values),
                                            'next_page': 2 if page == 1 and len(values) > 2 else None}
                self.send_response(status)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

        server = HTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        recipe = {'parameters': {'url': 'str', 'city': 'str', 'api_key': 'str'}, 'python': """import json
import requests
rows, page = [], 1
while page is not None:
    response = requests.get(PARAMS['url'], headers={'Authorization': 'Bearer ' + PARAMS['api_key']},
                            params={'location': PARAMS['city'], 'page': page}, timeout=8)
    assert response.status_code == 200
    data = response.json()
    rows.extend(data['items'])
    page = data['next_page']
assert len(rows) == data['total']
print('FINAL_ANSWER')
print(json.dumps({'city': PARAMS['city'], 'sum': sum(rows)}, ensure_ascii=False))
"""}
        try:
            with tempfile.TemporaryDirectory() as directory:
                install_transport_double(directory)
                mem = Memory()
                for start, city, expected in ((1, '北京', 15), (5, '上海', 120)):
                    step(mem, start)
                    mem.contract = deepcopy(CONTRACT)
                    inputs = {'url': 'http://127.0.0.1:' + str(server.server_port) + '/search',
                              'city': city, 'api_key': datasets[city][0]}
                    reply = ({'skill': recipe, 'inputs': inputs} if start == 1 else
                             {'use_skill': mem.skills[0]['id'], 'inputs': inputs})
                    _, code, _ = step(mem, start+1, llmResp=json.dumps(reply, ensure_ascii=False))
                    raw = run_sandbox({'executeCmd': code}, directory)
                    _, _, ledger = step(mem, start+2, lastCmdResult=raw)
                    self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']),
                                     {'city': city, 'sum': expected})
                    step(mem, start+3, '', lastRoundRoleActionResults={'11': True})
                self.assertEqual(calls, [('北京', 'Bearer first-secret'), ('北京', 'Bearer first-secret'),
                                         ('上海', 'Bearer rotated-secret')])
                self.assertEqual(len(mem.skills), 1)
                self.assertEqual(mem.skills[0]['successes'], 2)
                stored = json.dumps(mem.skills, ensure_ascii=False)
                for old_value in ('first-secret', 'rotated-secret', '北京', '上海'):
                    self.assertNotIn(old_value, stored)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_timeout_or_skipped_feedback_never_promotes(self):
        for round_no, errors in ((3, [{'errorCode': 1}]), (5, [])):
            method = learned_method((6, 5), CONTRACT, FILE_RECIPE)
            mem = Memory(task_text='query', task_point=(6, 5), task_started=1,
                         submitted=(2, '42'), submitted_method=method)
            step(mem, round_no, '', lastRoundRoleActionResults={'11': True}, errors=errors)
            self.assertFalse(mem.skills)


if __name__ == '__main__':
    unittest.main()
