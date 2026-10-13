"""Generic solver regressions: changed task semantics, real execution and feedback.

Replaces integration assumptions about injected parsers, automatic deployment
repair and transform-only replies. Independent utility tests remain elsewhere.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import ast
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest

from agent.intelligence import Memory, Intelligence, sandbox_result
from agent.task_history import remember, references, solution_record, solution_hints
from agent.task_prompt import render_prompt
from agent.task_runtime import runtime_code, runtime_result
from agent.task_sop import answer_contract, answer_error
from agent.task_skills import promote
from test_agent import setup_case
from test_evolution import task_payload
from test_evolution_v2 import step
from test_task_completion import run_sandbox


def execute(code, directory, contract=None, **kwargs):
    result = subprocess.run([sys.executable, '-c', runtime_code(code, directory, contract=contract, **kwargs)],
                            text=True, capture_output=True, timeout=12, cwd=directory)
    raw = f'[exitCode:{result.returncode}]\n{result.stdout}{result.stderr}'
    clean, report = runtime_result(raw)
    return raw, sandbox_result(clean), report


def checker_task(root, command='python3 verify.py', extra=''):
    root = Path(root)
    (root / 'input.json').write_text('[2,5,8]')
    checker = root / 'verify.py'
    checker.write_text("import json\nfrom pathlib import Path\n"
                       "assert json.loads(Path('result.json').read_text()) == {'sum': 13}\n"
                       "print('TOKEN: fixture-confirmed')\n" + extra)
    doc = root / 'question.md'
    doc.write_text('# Mixed input task\n选取 input.json 中大于2的值求和，写 result.json。\n'
                   f'运行验证 `{command}`，提交 TOKEN。\n## 提交格式\n```json\n{{"token":"example"}}\n```\n')
    documents = [{'kind': 'discover', 'path': str(doc), 'resolved_path': str(doc), 'output': doc.read_text()}]
    return documents, answer_contract(documents)


@contextmanager
def retry_api():
    class Handler(BaseHTTPRequestHandler):
        count = 0
        def log_message(self, *args):
            pass
        def do_GET(self):
            Handler.count += 1
            status = 401 if Handler.count == 1 or self.path == '/other' else 200
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'{"records": [3, 7], "total": 2}')
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class GenericSolverTests(unittest.TestCase):
    def test_final_text_reaches_submission_without_another_json_encoding(self):
        answers = [
            {'errors': {'monitor': 44, 'gateway': 44, 'logistics': 15},
             'peak_hour': '13时', 'peak_count': 13},
            [0, False, '中文'],
            'a real JSON string value',
        ]
        texts = [json.dumps(value, ensure_ascii=False) for value in answers]
        texts += ['普通文本答案', 'line one\nline two']
        with tempfile.TemporaryDirectory() as root:
            for text in texts:
                with self.subTest(text=text):
                    code = 'task_result(' + repr(text) + ')'
                    raw, result, _ = execute(code, root)
                    self.assertEqual(result[2], text, raw)
                    mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                                 pending=('cmd', 1), running_python=code)
                    _, _, ledger = step(mem, 2, lastCmdResult=raw)
                    self.assertEqual(ledger.commands['11']['taskAnswer'], text)

    def test_non_text_task_result_reports_protocol_error_without_submitting(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ('{}', '[]', '0', 'None', 'False'):
                with self.subTest(value=value):
                    raw, result, _ = execute('task_result(' + value + ')', root)
                    self.assertNotEqual(result[0], 'ok', raw)
                    self.assertIsNone(result[2])
                    self.assertIn('task_result 需要最终提交文本', raw)

    def test_log_filename_does_not_impose_fault_record_schema(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, 'data.log').write_text('alice,8\nbob,3\n')
            for expression, expected in [("sum(int(x[1]) for x in rows)", 11),
                                          ("[x[0] for x in rows if int(x[1]) > 5]", ['alice'])]:
                code = "rows=[line.strip().split(',') for line in open('data.log')]\n" + f'import json\ntask_result(json.dumps({expression}))'
                raw, result, report = execute(code, root)
                self.assertEqual(result[0], 'ok', raw)
                self.assertEqual(json.loads(result[2]), expected)
                self.assertNotIn('logs', report)

    def test_function_names_and_return_shapes_do_not_change_execution(self):
        with tempfile.TemporaryDirectory() as root:
            for value in ('None', 'False', '0', '[]', "{'custom': 2}"):
                raw, result, _ = execute(f'def parse_line(line): return {value}\nimport json\ntask_result(json.dumps(parse_line("x")))', root)
                self.assertEqual(result[0], 'ok', raw)
                self.assertEqual(json.loads(result[2]), ast.literal_eval(value))

    def test_zero_and_empty_results_are_valid_after_successful_execution(self):
        for value in ('0', '[]', '{"count":0}'):
            with tempfile.TemporaryDirectory() as root:
                raw, _, _ = execute('import json\ntask_result(json.dumps(' + value + '))', root)
                mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                             pending=('cmd', 1), running_python='import json\ntask_result(json.dumps(' + value + '))')
                _, _, ledger = step(mem, 2, lastCmdResult=raw)
                self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), json.loads(value))

    def test_unhandled_failure_never_submits_even_after_result_marker(self):
        with tempfile.TemporaryDirectory() as root:
            for code in ('task_result(str(0))\nraise ValueError("missing input")',
                         "print('FINAL_ANSWER\\n0')\nopen('missing.log')"):
                raw, result, _ = execute(code, root)
                self.assertNotEqual(result[0], 'ok')
                mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                             pending=('cmd', 1), running_python=code)
                _, _, ledger = step(mem, 2, lastCmdResult=raw)
                self.assertFalse(ledger.commands)
                self.assertTrue(mem.input_blocked)

    def test_caught_optional_parse_error_is_not_automatically_business_failure(self):
        with tempfile.TemporaryDirectory() as root:
            code = 'values=[]\nfor x in ["header", "4"]:\n try: values.append(int(x))\n except ValueError: continue\ntask_result(str(sum(values)))'
            raw, result, _ = execute(code, root)
            self.assertEqual(result[0], 'ok', raw)
            self.assertEqual(result[2], '4')

    def test_round_and_history_functions_are_never_injected(self):
        with tempfile.TemporaryDirectory() as root:
            raw, result, _ = execute("import json\ntask_result(json.dumps([round(2.5), 'old' in globals()]))", root,
                                     rounding='half_up', log_task=True, parser='def old(): return 9')
            self.assertEqual(json.loads(result[2]), [2, False], raw)

    def test_full_repair_can_change_signature_and_caller_in_one_reply(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('task', 1))
        code = 'def parse_line(line, factor): return int(line)*factor\ntask_result(str(parse_line("3", 4)))'
        _, command, _ = step(mem, 2, llmResp='PYTHON\n' + code)
        with tempfile.TemporaryDirectory() as root:
            raw = run_sandbox({'executeCmd': command}, root)
        _, _, ledger = step(mem, 3, lastCmdResult=raw)
        self.assertEqual(ledger.commands['11']['taskAnswer'], '12')

    def test_checker_renamed_and_full_program_submits_in_same_execution_cycle(self):
        with tempfile.TemporaryDirectory() as root:
            docs, contract = checker_task(root)
            original = Path(root, 'verify.py').read_bytes()
            self.assertEqual(contract['checker_command'], 'python3 verify.py')
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True, documents=docs,
                         contract=contract, pending=('task', 1))
            code = "import json\nfrom pathlib import Path\nrows=json.loads(Path('input.json').read_text())\nPath('result.json').write_text(json.dumps({'sum':sum(x for x in rows if x>2)}))"
            _, command, _ = step(mem, 2, llmResp='PYTHON\n' + code)
            raw = run_sandbox({'executeCmd': command}, root)
            _, _, ledger = step(mem, 3, lastCmdResult=raw)
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), {'token': 'fixture-confirmed'})
            self.assertEqual(Path(root, 'verify.py').read_bytes(), original)
            self.assertEqual(mem.submitted_method['python'], code)

    def test_checker_failure_duplicate_token_and_wrong_artifact_block_submission(self):
        with tempfile.TemporaryDirectory() as root:
            for extra, value in [('', 10), ('raise SystemExit(1)', 13), ("print('TOKEN: another')", 13)]:
                _, contract = checker_task(root, extra=extra)
                raw, result, report = execute(f"open('result.json','w').write('{{\"sum\":{value}}}')\nimport json\ntask_result(json.dumps({{'token':'invented'}}))", root, contract)
                self.assertNotEqual(result[0], 'ok', raw)
                self.assertFalse(report['checker']['passed'])

    def test_direct_and_printed_guessed_token_do_not_bypass_checker(self):
        with tempfile.TemporaryDirectory() as root:
            docs, contract = checker_task(root)
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                         documents=docs, contract=contract, pending=('task', 1))
            _, _, ledger = step(mem, 2, llmResp='ANSWER\n{"token":"invented"}')
            self.assertFalse(ledger.commands)
            raw, result, report = execute("print('FINAL_ANSWER\\n{\"token\":\"invented\"}')", root, contract)
            self.assertIsNone(result[2], raw)
            self.assertFalse(report['checker']['passed'])

    def test_new_program_invalidates_previous_verified_result(self):
        with tempfile.TemporaryDirectory() as root:
            docs, contract = checker_task(root)
            raw, result, report = execute("open('result.json','w').write('{\"sum\":13}')", root, contract)
            self.assertTrue(report['checker']['passed'], raw)
            raw, result, report = execute("open('result.json','w').write('{\"sum\":0}')", root, contract)
            self.assertFalse(report['checker']['passed'])
            self.assertIsNone(result[2])

    def test_ambiguous_checker_needs_explicit_documented_selection(self):
        with tempfile.TemporaryDirectory() as root:
            docs, contract = checker_task(root)
            contract['checker_command'] = None
            code = "open('result.json','w').write('{\"sum\":13}')\ntask_check('python3 verify.py')"
            raw, result, report = execute(code, root, contract)
            self.assertTrue(report['checker']['passed'], raw)
            raw, result, report = execute("task_check('true')", root, contract)
            self.assertNotEqual(result[0], 'ok', raw)

    def test_schema_example_is_not_closed_keys_or_nullability(self):
        docs = [{'kind': 'discover', 'path': '/q.md', 'output': '## 提交格式\n```json\n{"a":1}\n```'}]
        contract = answer_contract(docs)
        for answer in ('{"new_key":2}', '{"a":null}', '[]'):
            self.assertEqual(answer_error(answer, contract), '')
        for answer in ('not json', '{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}'):
            self.assertTrue(answer_error(answer, contract))
        self.assertNotIn('input_kind', contract)

    def test_transport_retry_recovers_only_same_url(self):
        with tempfile.TemporaryDirectory() as root, retry_api() as base:
            code = f'''import json
from urllib.request import urlopen
try: urlopen({base!r}+'/data')
except Exception: pass
value=json.load(urlopen({base!r}+'/data'))
task_result(json.dumps(sum(value['records'])))'''
            raw, result, report = execute(code, root)
            self.assertEqual(result[2], '10', raw)
            self.assertFalse(report['http_errors'])
            code = code.replace("+'/data')\nexcept", "+'/other')\nexcept")
            raw, result, report = execute(code, root)
            self.assertIsNone(result[2], raw)
            self.assertTrue(report['http_errors'])

    def test_budget_uses_actions_not_seven_round_business_deadline(self):
        mem = Memory(task_text='query', bootstrap_done=True)
        for rounds, actions in [(2, ['ANSWER']), (3, ['ANSWER', 'PYTHON']),
                               (5, ['ANSWER', 'PYTHON', 'READ', 'LIST'])]:
            context = json.loads(render_prompt(mem, rounds, []).split('上下文：\n')[1])
            self.assertEqual(context['allowedActions'], actions)
        mem.task_started = 1
        mem.task_timeout = 3
        mem.pending = ('task', 1)
        _, command, _ = step(mem, 2, llmResp='PYTHON\ntask_result(str(7))')
        self.assertTrue(command)
        with tempfile.TemporaryDirectory() as root:
            raw = run_sandbox({'executeCmd': command}, root)
        _, _, ledger = step(mem, 3, lastCmdResult=raw)
        self.assertEqual(ledger.commands['11']['taskAnswer'], '7')

    def test_expired_result_and_task_change_cannot_submit_old_answer(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('cmd', 1))
        _, _, ledger = step(mem, 3, lastCmdResult='[exitCode:0]\nFINAL_ANSWER\n7')
        self.assertFalse(ledger.commands)
        mem.pending = ('cmd', 3)
        payload = task_payload(4, 'different task')
        payload['lastCmdResult'] = '[exitCode:0]\nFINAL_ANSWER\n7'
        turn, cfg, _, ledger = setup_case(payload)
        mem.observe(turn, cfg)
        self.assertIsNone(mem.answer)

    def test_history_saves_question_and_program_with_current_parameter_warning(self):
        mem = Memory(task_text='old question', task_point=(6, 5), answer_python="api_key='old-secret'\ntask_result(3)")
        record = solution_record(mem)
        record['rounds'] = 4
        promote(mem.skills, record)
        hints = solution_hints(mem, [])
        self.assertEqual(hints[0]['question'], 'old question')
        self.assertIn('task_result', hints[0]['python'])
        self.assertNotIn('old-secret', json.dumps(hints))
        self.assertIn('重新读取', hints[0]['scope'])
        with tempfile.TemporaryDirectory() as root:
            raw, result, _ = execute("import json\ntask_result(json.dumps('api_key' in globals()))", root)
            self.assertEqual(result[2], 'false', raw)

    def test_explicit_previous_question_precedes_same_shape_heuristic(self):
        mem = Memory(task_text='new', task_point=(1, 2))
        for source, point in [('/other.md', (1, 2)), ('/explicit.md', (3, 4))]:
            mem.skills.append({'id': source, 'source': source, 'point': point, 'python': 'task_result(1)',
                               'question': source, 'evidence': 'observed', 'scope': 'reference'})
        hints = solution_hints(mem, [{'source': '/explicit.md'}])
        self.assertEqual(hints[0]['question'], '/explicit.md')

    def test_prompt_retains_old_failure_pair_and_latest_full_code(self):
        mem = Memory(task_text='query')
        mem.exploration.append({'status': 'failed', 'python': 'old code', 'sandbox': 'specific old error'})
        mem.exploration.append({'status': 'ok', 'python': 'new code', 'sandbox': 'new result'})
        mem.last_attempt = dict(mem.exploration[-1])
        prompt = render_prompt(mem, 8, [])
        self.assertIn('specific old error', prompt)
        self.assertIn('old code', prompt)
        self.assertEqual(prompt.count('new code'), 1)
        self.assertNotIn('只修失败解析函数', prompt)
        self.assertNotIn('use_skill', prompt)

    def test_truncated_document_exposes_exact_reread_origin(self):
        mem = Memory(task_text='query', documents=[{'path': '/large.md', 'start': 6000, 'output': 'x'*24000}])
        context = json.loads(render_prompt(mem, 8, []).split('上下文：\n')[1])
        self.assertTrue(context['documents'][0]['truncated'])
        self.assertEqual(context['documents'][0]['reread'], {'path': '/large.md', 'start': 6000})

    def test_read_preserves_latest_program_and_failure_for_repair(self):
        mem = Memory(task_text='query')
        mem.exploration.append({'status':'failed','python':'failing complete program','sandbox':'ValueError: original'})
        mem.last_attempt = {'tool':'read','status':'ok','sandbox':'supplement'}
        context=json.loads(render_prompt(mem,8,[]).split('上下文：\n')[1])
        self.assertEqual(context['lastAttempt']['python'],'failing complete program')
        self.assertEqual(context['lastToolResult']['sandbox'],'supplement')

    def test_checker_mutation_stays_blocked_on_next_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            _, contract = checker_task(root)
            code = "open('verify.py','w').write(\"print('TOKEN: forged')\")"
            raw,result,report=execute(code,root,contract)
            self.assertNotEqual(result[0],'ok',raw)
            self.assertIn('checker source changed',report['checker']['feedback'])
            contract['checker_files']=report['checker_files']
            raw,result,report=execute('pass',root,contract)
            self.assertIsNone(result[2],raw)
            self.assertFalse(report['checker']['passed'])

    def test_failed_page_not_cleared_by_success_of_different_page(self):
        from test_task_runtime import heritage_api, install_transport_double
        with heritage_api() as (base,_), tempfile.TemporaryDirectory() as root:
            install_transport_double(root)
            code=f'''import requests
requests.get({base!r}+'/search',params={{'location':'北京','page':2}})
requests.get({base!r}+'/search',params={{'location':'北京','page':1}},headers={{'Authorization':'Bearer fixture-key'}})
task_result(str(0))'''
            raw,result,report=execute(code,root)
            self.assertIsNone(result[2],raw)
            self.assertTrue(report['http_errors'])

    def test_direct_question_extracts_checker_requirement_without_file_bootstrap(self):
        payload=task_payload(2,'运行 checker `python3 verify.py`，提交 TOKEN。')
        turn,cfg,_,ledger=setup_case(payload)
        mem=Memory()
        mem.observe(turn,cfg)
        self.assertTrue(mem.contract['requires_checker'])
        self.assertEqual(mem.contract['checker_command'],'python3 verify.py')
