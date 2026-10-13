"""Model-routed query regressions with an independent local offset API."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from test_agent import setup_case
from test_evolution import task_payload
from test_task_completion import run_sandbox
from agent.intelligence import Intelligence, Memory
from agent.task_query import reference_paths
from test_evolution_v2 import step
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


def model_program(base, city='北京', key='first-key', only_type=None, reverse=False):
    """Controlled model response, never production query logic."""
    return f'''
import json
from urllib.request import Request, urlopen
from urllib.parse import urlencode
rows = []
while True:
    params = {{'location': {city!r}, 'offset': len(rows), 'limit': 100}}
    request = Request({base!r} + '/search?' + urlencode(params),
                      headers={{'Authorization': 'Bearer ' + {key!r}}})
    with urlopen(request, timeout=3) as response:
        data = json.load(response)['data']
    rows.extend(data['records'])
    if len(rows) == data['pagination']['total_count']:
        break
    if not data['records']:
        raise ValueError('incomplete page')
only_type = {only_type!r}
selected = [r for r in rows if only_type is None or r['type'] == only_type]
result = {{'city': {city!r}, 'total_count': len(selected),
          'world_heritage_count': sum(r['protected_level'] == '世界遗产' for r in selected),
          'types': sorted({{r['type'] for r in selected}}, reverse={reverse!r})}}
task_result(result)
'''


class QueryTests(unittest.TestCase):
    def ready(self, docs, directory=None):
        if directory:
            docs = [{**d, 'resolved_path': str(Path(directory, Path(d['resolved_path']).name))}
                    for d in docs]
        return Memory(task_text='query', task_started=1, task_timeout=15,
                      bootstrap_done=True, documents=docs, contract=answer_contract(docs))

    def test_original_and_changed_rules_request_model_before_execution(self):
        for extra in ('', '\n全部统计仅限建筑类型。', '\ntypes 必须按名称降序。'):
            with self.subTest(extra=extra):
                docs = documents('http://localhost:8899')
                docs[0]['output'] += extra
                mem = self.ready(docs)
                prompt, command, ledger = step(mem, 2)
                self.assertTrue(prompt)
                self.assertIn(docs[0]['output'], json.loads(prompt.split('上下文：')[1])['documents'][0]['output'])
                self.assertFalse(command)
                self.assertFalse(ledger.commands)
                self.assertEqual(mem.pending, ('task', 2))

    def test_model_query_pagination_filter_sort_and_submission(self):
        with api() as (base, calls, _), tempfile.TemporaryDirectory() as directory:
            for only_type, reverse, total, kinds in (
                    (None, True, 3, ['遗址', '建筑', '园林']),
                    ('建筑', False, 1, ['建筑'])):
                with self.subTest(only_type=only_type):
                    docs = documents(base)
                    docs[0]['output'] += '\n按当前指定类型筛选，types 使用本题要求的次序。'
                    mem = self.ready(docs, directory)
                    step(mem, 2)
                    prompt, command, _ = step(mem, 3, llmResp='PYTHON\n' + model_program(
                        base, only_type=only_type, reverse=reverse))
                    self.assertFalse(prompt)
                    self.assertTrue(command)
                    output = run_sandbox({'executeCmd': command}, directory)
                    prompt, command, ledger = step(mem, 4, lastCmdResult=output)
                    answer = json.loads(ledger.commands['11']['taskAnswer'])
                    self.assertEqual(answer, {'city':'北京', 'total_count':total,
                        'world_heritage_count': 1 if only_type is None else 0, 'types':kinds})
                    self.assertFalse(prompt or command)
            self.assertEqual([c[0]['offset'] for c in calls], [['0'], ['2'], ['0'], ['2']])

    def test_failed_query_blocks_answer_then_successful_repair_submits(self):
        with api() as (base, _, _), tempfile.TemporaryDirectory() as directory:
            mem = self.ready(documents(base), directory)
            step(mem, 2)
            code = model_program(base, key='wrong-key')
            _, command, _ = step(mem, 3, llmResp='PYTHON\n' + code)
            raw = run_sandbox({'executeCmd':command}, directory)
            prompt, command, ledger = step(mem, 4, lastCmdResult=raw)
            self.assertTrue(mem.query_blocked)
            self.assertIn('401', prompt)
            self.assertFalse(command or ledger.commands)
            _, _, ledger = step(mem, 5, llmResp='ANSWER\n{"total_count":0}')
            self.assertFalse(ledger.commands)
            self.assertTrue(mem.query_blocked)
            _, command, _ = step(mem, 6, llmResp='PYTHON\n' + model_program(base))
            raw = run_sandbox({'executeCmd':command}, directory)
            _, _, ledger = step(mem, 7, lastCmdResult=raw)
            self.assertFalse(mem.query_blocked)
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer'])['total_count'], 3)

    def test_caught_later_page_failure_cannot_submit_default_zero(self):
        with api('later_error') as (base, _, _), tempfile.TemporaryDirectory() as directory:
            mem = self.ready(documents(base), directory)
            step(mem, 2)
            code = 'try:\n' + '\n'.join('    ' + line for line in model_program(base).splitlines())
            code += '\nexcept Exception:\n    task_result({"total_count": 0})'
            _, command, _ = step(mem, 3, llmResp='PYTHON\n' + code)
            raw = run_sandbox({'executeCmd':command}, directory)
            prompt, command, ledger = step(mem, 4, lastCmdResult=raw)
            self.assertTrue(mem.query_blocked)
            self.assertIn('500', prompt)
            self.assertFalse(command or ledger.commands)

    def test_three_tasks_reread_current_key_then_request_model(self):
        with tempfile.TemporaryDirectory() as directory:
            mem = Memory()
            for index, city in enumerate(('北京', '南京', '成都')):
                docs = documents('http://localhost:8899', city, 'current-key-' + str(index))
                text = docs[0]['output']
                if index:
                    text = text.replace('`API_DOCS.md`', '前题文档') + '\nAPI 与前面完全相同\n'
                task = Path(directory, f'task_{index}.md')
                task.write_text(text)
                Path(directory, 'API_DOCS.md').write_text(docs[1]['output'])
                previous = ''
                for offset in range(3):
                    payload = task_payload(index * 10 + offset + 1, '请阅读' + str(task))
                    payload['lastCmdResult'] = previous
                    turn, cfg, _, ledger = setup_case(payload)
                    mem.observe(turn, cfg)
                    prompt, command = Intelligence(turn, cfg, mem).task(ledger)
                    self.assertFalse(ledger.commands)
                    if offset < 2:
                        self.assertTrue(command)
                        previous = run_sandbox({'executeCmd':command}, directory)
                    else:
                        self.assertFalse(command)
                        self.assertIn('current-key-' + str(index), prompt)
                        if index:
                            self.assertNotIn('current-key-' + str(index-1), prompt)

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
