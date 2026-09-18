"""Real task wording plus independent HTTP/checker fixtures, no live LLM/API."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import shlex
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from test_agent import setup_case
from test_evolution import task_payload
from test_task_completion import deployment, run_sandbox
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Intelligence, Memory, sandbox_result
from agent.task_runtime import runtime_code, runtime_result
from agent.task_sop import answer_contract, answer_error, engineering_code

FIXTURES = Path(__file__).with_name('fixtures') / 'evolution'


@contextmanager
def heritage_api():
    """Synthetic records deliberately differ from any official answer."""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            address = urlsplit(self.path)
            params = parse_qs(address.query)
            calls.append((address.path, params, self.headers.get('Authorization')))
            if address.path == '/empty':
                status, result = 200, {'items': [], 'total': 0}
            elif address.path == '/invalid':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'not JSON')
                return
            elif address.path == '/semantic-error':
                status, result = 200, {'status': 'error', 'message': 'backend unavailable'}
            elif self.headers.get('Authorization') != 'Bearer fixture-key':
                status, result = 401, {'message': "Missing 'Authorization' header. Expected format: Bearer <api_key>"}
            elif not params.get('location'):
                status, result = 400, {'message': 'Missing required parameter: location'}
            else:
                page = int(params.get('page', ['1'])[0])
                rows = [{'name': '乙', 'year': 200, 'type': '宫殿', 'level': '世界遗产'},
                        {'name': '丙', 'year': 300, 'type': '园林', 'level': '地方'},
                        {'name': '甲', 'year': 100, 'type': '园林', 'level': '世界遗产'}]
                status, result = 200, {'items': rows[(page - 1) * 2:page * 2],
                                       'total': 3, 'next_page': 2 if page == 1 else None}
            body = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


# An independent requests-shaped transport double uses the stdlib HTTP client.
# It exercises the optional requests hooks without installing host dependencies.
REQUESTS_DOUBLE = '''
import http.client
import json as _json
from types import SimpleNamespace
from urllib.parse import urlsplit, urlencode
class Response:
    def __init__(self, status, text, url):
        self.status_code, self.text, self.url = status, text, url
    def json(self):
        return _json.loads(self.text)
class Session:
    def request(self, method, url, params=None, headers=None, timeout=None):
        parsed = urlsplit(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
        path = parsed.path + ('?' + urlencode(params) if params else '')
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = Response(response.status, response.read().decode(), url)
        connection.close()
        return result
sessions = SimpleNamespace(Session=Session)
models = SimpleNamespace(Response=Response)
exceptions = SimpleNamespace(RequestException=OSError)
def get(url, **kwargs):
    return sessions.Session().request('GET', url, **kwargs)
'''


def install_transport_double(directory):
    Path(directory, 'requests.py').write_text(REQUESTS_DOUBLE, encoding='utf-8')


def execute(code, directory, task_directory=None):
    return run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(runtime_code(code, task_directory))}, directory)


class TaskRuntimeTests(unittest.TestCase):
    def test_log_submission_forms_and_indented_fences_define_contracts(self):
        for name, expected in [('alpha', {'token': 'xxx'}), ('beijing', {'city': '北京', 'total_count': 0,
                    'world_heritage_count': 0, 'types': ['a', 'b'], 'oldest_era': 'c'})]:
            contract = answer_contract([{'kind': 'discover', 'resolved_path': '/tmp/task/task.md',
                                        'output': (FIXTURES / (name + '_task.md')).read_text()}])
            self.assertEqual(contract['example'], expected)
            if name == 'beijing':
                self.assertTrue(answer_error('{"oldest_era":null}', contract))
                self.assertTrue(answer_error('{"total_count":"0"}', contract))
                self.assertFalse(answer_error('{"city":"北京"}', contract))

    def test_declared_shell_checker_recovers_missing_interpreter_without_modification(self):
        for first in ('#!/unavailable/bin/bash\n', '#!/bin/bash\r\n', '#!/unavailable/env bash\n'):
            with self.subTest(first=first), tempfile.TemporaryDirectory() as directory:
                workspace, _, token = deployment(directory, 'alpha')
                # Keep the independently authored checker, invoke it through a shell.
                original = (workspace / 'check').read_text().split('\n', 1)[1]
                script = first + 'exec ' + shlex.quote(sys.executable) + " - <<'CHECKER'\n" + original + 'CHECKER\n'
                (workspace / 'check').write_bytes(script.encode())
                raw = run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(engineering_code(str(workspace)))}, directory)
                self.assertEqual(json.loads(sandbox_result(raw)[2]), {'token': token})
                self.assertEqual((workspace / 'check').read_bytes(), script.encode())

    def test_unknown_interpreter_and_failing_shell_never_produce_token(self):
        for script in ('#!/missing/python\nprint("TOKEN: fake")\n',
                       '#!/missing/bash\nprintf "TOKEN: fake\\n"\nexit 1\n'):
            with self.subTest(script=script), tempfile.TemporaryDirectory() as directory:
                workspace, _, _ = deployment(directory, 'alpha')
                (workspace / 'check').write_text(script)
                raw = run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(engineering_code(str(workspace)))}, directory)
                self.assertTrue(raw.startswith('[exitCode:1]'))
                self.assertIsNone(sandbox_result(raw)[2])

    def test_independent_checker_can_finish_after_unrelated_model_http_failure(self):
        p = task_payload(5, 'repair')
        p['lastCmdResult'] = '[exitCode:0]\nFINAL_ANSWER\n{"token":"checked"}'
        mem = Memory(task_text='repair', task_started=1, bootstrap_done=True, query_blocked=True,
                     pending=('cmd', 4), running_tool={'kind':'check'},
                     contract={'kind':'check_token', 'example':{'token':'xxx'}})
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        Intelligence(t, cfg, mem).task(ledger)
        self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), {'token':'checked'})
        self.assertFalse(mem.query_blocked)

    def test_model_execution_rebinds_cwd_each_time(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory, 'current')
            current.mkdir()
            (current / 'API_DOCS.md').write_text('CURRENT')
            Path(directory, 'API_DOCS.md').write_text('WRONG')
            first = execute("import os; print(open('API_DOCS.md').read()); os.chdir('/')", directory, str(current))
            second = execute("print(open('API_DOCS.md').read())", directory, str(current))
            for raw in (first, second):
                self.assertIn('CURRENT', raw)
                self.assertNotIn('WRONG', raw)

    def test_caught_requests_errors_cannot_yield_zero_answers(self):
        with heritage_api() as (url, _), tempfile.TemporaryDirectory() as directory:
            install_transport_double(directory)
            for path, headers in [('/search', {}), ('/search', {'Authorization': 'Bearer fixture-key'}),
                                  ('/invalid', {}), ('/semantic-error', {})]:
                with self.subTest(path=path, headers=headers):
                    code = f'''import requests, json
try:
    response = requests.get({url + path!r}, headers={headers!r})
    data = response.json()
except ValueError:
    data = {{}}
print('FINAL_ANSWER')
print(json.dumps({{'total_count': len(data.get('items', []))}}))
'''
                    raw, report = runtime_result(execute(code, directory))
                    self.assertTrue(report['http_errors'])
                    self.assertTrue(raw.startswith('[exitCode:1]'))
                    self.assertIsNone(sandbox_result(raw)[2])

    def test_successful_first_page_does_not_hide_failed_later_page(self):
        with heritage_api() as (url, _), tempfile.TemporaryDirectory() as directory:
            install_transport_double(directory)
            code = f'''import requests
requests.get({url + '/search'!r}, params={{'location': '北京'}}, headers={{'Authorization': 'Bearer fixture-key'}}).json()
requests.get({url + '/search'!r}, params={{'page': 2}})
print('FINAL_ANSWER')
print('{{"total_count":2}}')
'''
            raw, report = runtime_result(execute(code, directory))
            self.assertEqual(report['http_successes'], 1)
            self.assertTrue(report['http_errors'])
            self.assertIsNone(sandbox_result(raw)[2])

    def test_urllib_failure_is_detected_even_when_caught(self):
        with heritage_api() as (url, _), tempfile.TemporaryDirectory() as directory:
            code = f'''from urllib.request import urlopen
try:
    urlopen({url + '/search'!r})
except Exception:
    pass
print('FINAL_ANSWER')
print('{{"total_count":0}}')
'''
            raw, report = runtime_result(execute(code, directory))
            self.assertIn('401', report['http_errors'][0]['error'])
            self.assertIsNone(sandbox_result(raw)[2])

    def test_real_empty_response_is_not_rejected_as_a_default(self):
        with heritage_api() as (url, _), tempfile.TemporaryDirectory() as directory:
            code = f'''import json
from urllib.request import urlopen
data = json.load(urlopen({url + '/empty'!r}))
print('FINAL_ANSWER')
print(json.dumps({{'total_count': len(data['items'])}}))
'''
            raw, report = runtime_result(execute(code, directory))
            self.assertFalse(report['http_errors'])
            self.assertEqual(json.loads(sandbox_result(raw)[2]), {'total_count': 0})

    def test_failed_query_blocks_model_and_unqueried_python_until_recovery(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     pending=('cmd', 2), running_python='query()')
        p = task_payload(3, 'query')
        p['lastCmdResult'] = '[exitCode:1]\nTASK_RUNTIME {"http_errors":[{"error":"HTTP 401"}]}\nFINAL_ANSWER\n0'
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        self.assertTrue(mem.query_blocked)
        mem.answer = '0'
        Intelligence(t, cfg, mem).task(ledger)
        self.assertFalse(ledger.commands)
        p.update(roundNo=4, lastCmdResult='[exitCode:0]\nTASK_RUNTIME {"http_errors":[],"http_successes":0}\nFINAL_ANSWER\n0')
        mem.pending = ('cmd', 3)
        t, cfg, _, _ = setup_case(p)
        mem.observe(t, cfg)
        self.assertIsNone(mem.answer)
        self.assertTrue(mem.query_blocked)
        p.update(roundNo=5, lastCmdResult='[exitCode:0]\nTASK_RUNTIME {"http_errors":[],"http_successes":1}\nFINAL_ANSWER\n3')
        mem.pending = ('cmd', 4)
        t, cfg, _, _ = setup_case(p)
        mem.observe(t, cfg)
        self.assertFalse(mem.query_blocked)
        self.assertEqual(mem.answer, '3')
        mem.query_blocked = True
        p.update(roundNo=6, phaseTask='next query')
        t, cfg, _, _ = setup_case(p)
        mem.observe(t, cfg)
        self.assertFalse(mem.query_blocked)

    def test_near_deadline_diagnostic_python_does_not_waste_format_retry(self):
        p = task_payload(13, 'query')
        p['llmResp'] = 'PYTHON\nprint("diagnostic")'
        mem = Memory(task_text='query', task_started=1, task_timeout=15,
                     bootstrap_done=True, pending=('task', 12))
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        _, cmd = Intelligence(t, cfg, mem).task(ledger)
        self.assertTrue(cmd)
        self.assertFalse(ledger.commands)
        self.assertEqual(mem.task_failures, 0)

    def test_log_api_task_prefetches_docs_and_recovers_401_400_before_deadline(self):
        with heritage_api() as (url, calls), tempfile.TemporaryDirectory() as directory:
            install_transport_double(directory)
            task_dir = Path(directory, 'selfEvolutionTask/current')
            task_dir.mkdir(parents=True)
            (task_dir / 'task_1_beijing.md').write_text((FIXTURES / 'beijing_task.md').read_text())
            (task_dir / 'API_DOCS.md').write_text('FIXTURE API: ' + url + '/search; X-API-Key; city; default page size 2')
            agent = Agent(Config(layout_mode='explicit'))
            p = task_payload(1)
            p['teamOur']['playerTasks'][0]['timeoutRounds'] = 15
            agent.decide(p)
            p.update(roundNo=2, phaseTask='请阅读task_1_beijing.md，获取任务信息')
            response = agent.decide(p)
            p.update(roundNo=3, lastCmdResult=run_sandbox(response, directory))
            response = agent.decide(p)
            self.assertFalse(response['prompt'])
            self.assertIn('API_DOCS.md', response['executeCmd'])
            p.update(roundNo=4, lastCmdResult=run_sandbox(response, directory))
            response = agent.decide(p)
            self.assertIn('FIXTURE API', response['prompt'])
            self.assertIn('oldest_era', response['prompt'])
            for round_no, headers in [(5, {'X-API-Key': 'fixture-key'}),
                                       (7, {'Authorization': 'Bearer fixture-key'})]:
                code = f'''import requests, json
r = requests.get({url + '/search'!r}, params={{'city':'北京'}}, headers={headers!r})
data = r.json()
print('FINAL_ANSWER')
print(json.dumps({{'total_count':len(data.get('items', []))}}))
'''
                p.update(roundNo=round_no, llmResp='PYTHON\n' + code, lastCmdResult='')
                response = agent.decide(p)
                p.update(roundNo=round_no + 1, llmResp='', lastCmdResult=run_sandbox(response, directory))
                response = agent.decide(p)
                self.assertNotIn('11', response['roleCommandMap'])
                self.assertIn('query_failed', response['prompt'])
                self.assertIn('Authorization' if round_no == 5 else 'location', response['prompt'])
            code = f'''import requests, json
page, rows = 1, []
while page:
    data = requests.get({url + '/search'!r}, params={{'location':'北京', 'page':page}},
                        headers={{'Authorization':'Bearer fixture-key'}}).json()
    rows.extend(data['items'])
    page = data['next_page']
assert len(rows) == data['total']
answer = {{'city':'北京', 'total_count':len(rows),
          'world_heritage_count':sum(x['level'] == '世界遗产' for x in rows),
          'types':sorted({{x['type'] for x in rows}}), 'oldest_era':min(rows, key=lambda x:x['year'])['name']}}
print('FINAL_ANSWER')
print(json.dumps(answer, ensure_ascii=False))
'''
            p.update(roundNo=9, llmResp='PYTHON\n' + code, lastCmdResult='')
            response = agent.decide(p)
            p.update(roundNo=10, llmResp='', lastCmdResult=run_sandbox(response, directory))
            response = agent.decide(p)
            answer = json.loads(response['roleCommandMap']['11']['taskAnswer'])
            self.assertEqual(answer, {'city':'北京', 'total_count':3, 'world_heritage_count':2,
                                      'types':['园林', '宫殿'], 'oldest_era':'甲'})
            self.assertEqual([c[1].get('page') for c in calls[-2:]], [['1'], ['2']])
            p.update(roundNo=11, phaseTask='', lastCmdResult='', lastRoundRoleActionResults={'11':True})
            p['teamOur']['playerTasks'][0]['isValid'] = False
            agent.decide(p)
            mem = next(iter(agent.sessions.values()))
            outcome = mem.task_outcomes[-1]
            self.assertTrue(outcome['completionObserved'])
            self.assertEqual(outcome['llmCalls'], 3)
            self.assertEqual(outcome['sandboxCalls'], 5)
            self.assertEqual(outcome['submissions'], 1)
            self.assertEqual(outcome['rounds'], 10)
