"""Independent offset API and three-question replay with no model replies."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import shlex
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from test_agent import setup_case
from test_evolution import task_payload
from test_task_completion import run_sandbox
from agent.intelligence import Intelligence, Memory, sandbox_result
from agent.task_query import query_code, query_config, reference_paths
from agent.task_runtime import runtime_result
from agent.task_sop import answer_contract


@contextmanager
def api(mode='ok'):
    calls = []
    state = {'key': 'first-key'}
    datasets = {
        '北京': [('明清', '建筑'), ('旧石器时代', '遗址'), ('清', '园林')],
        '南京': [('明', '陵墓'), ('未知年代' if mode == 'unknown_era' else '六朝', '遗址')],
        '成都': [('汉', '遗址'), ('商', '遗址'), ('唐', '宗教'), ('宋', '园林')],
        '空城': [],
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            params = parse_qs(urlsplit(self.path).query)
            calls.append((params, self.headers.get('Authorization')))
            if self.headers.get('Authorization') != 'Bearer ' + state['key']:
                status, value = 401, {'message': "Missing 'Authorization'; Expected Authorization: Bearer <api_key>"}
            elif 'location' not in params:
                status, value = 400, {'message': 'Missing required parameter: location'}
            else:
                city = params['location'][0]
                offset = int(params.get('offset', ['0'])[0])
                actual = 0 if mode == 'repeated' else offset
                records = [{'id': str(i), 'name': city + str(i), 'era': era,
                            'type': kind, 'protected_level': '世界遗产' if i % 2 else '全国重点'}
                           for i, (era, kind) in enumerate(datasets[city])]
                batch = records[actual:actual + 2]  # Server clamps requested limit.
                if mode == 'duplicate' and offset:
                    batch[0]['id'] = '0'
                if mode == 'missing_field' and batch:
                    del batch[0]['protected_level']
                if mode == 'empty_page' and offset:
                    batch = []
                status, value = 200, {'code': 200, 'data': {'records': batch, 'pagination': {
                    'total_count': len(records) + (1 if mode == 'changed_total' and offset else 0),
                    'offset': actual, 'limit': 2}}}
                if mode == 'bad_shape':
                    value['data'] = 'not records'
                if mode == 'later_error' and offset:
                    status, value = 500, {'message': 'failure'}
            self.send_response(status)
            self.end_headers()
            self.wfile.write(json.dumps(value, ensure_ascii=False).encode())

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port), calls, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def documents(base, city='北京', key='first-key'):
    text = Path(__file__).with_name('fixtures').joinpath('evolution/beijing_task.md').read_text()
    text = text.replace('http://localhost:8899', base).replace('北京', city)
    return [{'kind': 'discover', 'resolved_path': '/current/task.md', 'output': text},
            {'kind': 'read', 'path': 'API_DOCS.md', 'resolved_path': '/current/API_DOCS.md',
             'output': f'curl -H "X-API-Key: {key}" "{base}/search?city=北京"'}]


class QueryTests(unittest.TestCase):
    def execute_query(self, base, city='北京'):
        docs = documents(base, city)
        config = query_config(docs, answer_contract(docs))
        with tempfile.TemporaryDirectory() as directory:
            output = run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(query_code(config))}, directory)
        return sandbox_result(runtime_result(output)[0]), output

    def test_negotiation_and_server_clamped_offset_pagination(self):
        with api() as (base, calls, _):
            (status, _, answer), output = self.execute_query(base)
        self.assertEqual(status, 'ok', output)
        self.assertEqual(json.loads(answer), {'city': '北京', 'total_count': 3,
            'world_heritage_count': 1, 'types': ['园林', '建筑', '遗址'], 'oldest_era': '北京1'})
        self.assertEqual(len(calls), 4)  # 401, 400, then two successful pages.
        self.assertEqual(calls[-1][0]['offset'], ['2'])
        self.assertNotIn('first-key', output)

    def test_failed_or_incomplete_query_never_prints_answer(self):
        for mode in ('repeated', 'duplicate', 'changed_total', 'missing_field',
                     'bad_shape', 'later_error', 'empty_page'):
            with self.subTest(mode=mode), api(mode) as (base, _, _):
                (status, _, answer), output = self.execute_query(base)
                self.assertNotEqual(status, 'ok', output)
                self.assertIsNone(answer)
                self.assertNotIn('FINAL_ANSWER', output)

    def test_empty_and_unknown_chronology_submit_only_proven_fields(self):
        for city, mode, total in (('空城', 'ok', 0), ('南京', 'unknown_era', 2)):
            with self.subTest(city=city), api(mode) as (base, _, _):
                (status, _, answer), output = self.execute_query(base, city)
                self.assertEqual(status, 'ok', output)
                value = json.loads(answer)
                self.assertEqual(value['total_count'], total)
                self.assertNotIn('oldest_era', value)

    def test_three_tasks_reread_rotating_key_without_llm_or_prior_completion(self):
        with api() as (base, calls, state), tempfile.TemporaryDirectory() as directory:
            mem = Memory()
            for index, city in enumerate(('北京', '南京', '成都')):
                state['key'] = 'current-key-' + str(index)
                docs = documents(base, city, state['key'])
                text = docs[0]['output']
                if index:
                    text = text.replace('`API_DOCS.md`', '前题文档') + '\nAPI 与前面完全相同\n'
                task = Path(directory, f'task_{index}.md')
                task.write_text(text)
                Path(directory, 'API_DOCS.md').write_text(docs[1]['output'])
                description = '请阅读' + str(task)
                previous = ''
                for offset in range(4):
                    p = task_payload(index * 10 + offset + 1, description)
                    p['teamOur']['playerTasks'][0]['timeoutRounds'] = 10
                    p['lastCmdResult'] = previous
                    turn, cfg, _, ledger = setup_case(p)
                    mem.observe(turn, cfg)
                    prompt, command = Intelligence(turn, cfg, mem).task(ledger)
                    self.assertFalse(prompt, prompt)
                    if command:
                        previous = run_sandbox({'executeCmd': command}, directory)
                    else:
                        answer = json.loads(ledger.commands['11']['taskAnswer'])
                        self.assertEqual(answer['city'], city)
                        self.assertEqual(answer['total_count'], (3, 2, 4)[index])
                        self.assertEqual(answer['oldest_era'], city + '1')
                self.assertFalse(mem.skills)  # No completion signal; reread still works.
            self.assertEqual({auth for _, auth in calls if auth},
                             {'Bearer current-key-0', 'Bearer current-key-1', 'Bearer current-key-2'})

    def test_reference_reuse_requires_same_point_directory_and_explicit_continuity(self):
        docs = documents('http://localhost:8899')
        docs[0]['output'] += '\nAPI 与前面完全相同\n'
        knowledge = [{'point': (1, 2), **docs[1]}]
        self.assertEqual(reference_paths(docs[:1], knowledge, (1, 2)), ['/current/API_DOCS.md'])
        self.assertFalse(reference_paths(docs[:1], knowledge, (2, 2)))
        docs[0]['resolved_path'] = '/other/task.md'
        self.assertFalse(reference_paths(docs[:1], knowledge, (1, 2)))
        docs[0]['resolved_path'] = '/current/task.md'
        docs[0]['output'] = 'unrelated question'
        self.assertFalse(reference_paths(docs[:1], knowledge, (1, 2)))

    def test_unrecognized_or_changed_service_uses_model_fallback(self):
        docs = documents('http://localhost:8899')
        self.assertIsNone(query_config(docs, {'example': {'token': ''}}))
        docs[1]['output'] = docs[1]['output'].replace(':8899', ':9900')
        self.assertIsNone(query_config(docs, answer_contract(docs)))

    def test_query_failure_falls_back_once_without_submitting(self):
        docs = documents('http://localhost:8899')
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     query_sop_attempted=True, pending=('cmd', 2),
                     running_tool={'kind': 'query'}, documents=docs, contract=answer_contract(docs))
        p = task_payload(3, 'query')
        p['lastCmdResult'] = '[exitCode:1]\nQUERY_DIAGNOSTIC invalid pagination'
        turn, cfg, _, ledger = setup_case(p)
        mem.observe(turn, cfg)
        prompt, command = Intelligence(turn, cfg, mem).task(ledger)
        self.assertTrue(prompt)
        self.assertFalse(command)
        self.assertFalse(ledger.commands)
        self.assertIn('invalid pagination', prompt)


if __name__ == '__main__':
    unittest.main()
