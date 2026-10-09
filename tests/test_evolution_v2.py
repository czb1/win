"""v2 transport, current-input inspection and checked file-task regressions."""
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

from test_agent import setup_case
from test_evolution import task_payload
from test_task_completion import run_sandbox
from agent.brain import Agent
from agent.config import Config
from agent.intelligence import Intelligence, Memory, sandbox_result
from agent.task_runtime import runtime_code, runtime_result
from agent.task_skills import compatible, learned_method, parser_method
from agent.task_sop import answer_contract, engineering_code


def v2_result(response, directory):
    raw = run_sandbox(response, directory)
    header, _, body = raw.partition('\n')
    return header + '\n[durationMs:12]\n' + body


def step(mem, round_no, **updates):
    payload = task_payload(round_no, mem.task_text)
    payload.update(updates)
    turn, cfg, _, ledger = setup_case(payload)
    mem.observe(turn, cfg)
    prompt, code = Intelligence(turn, cfg, mem).task(ledger)
    return prompt, code, ledger


def file_task(root):
    base = root / 'scripts'
    (base / 'test_cases').mkdir(parents=True)
    (base / 'work').mkdir()
    cases = {'cases': [{'input': [1, 3], 'expected': 4}]}
    (base / 'test_cases/s2_cases.json').write_text(json.dumps(cases))
    (base / 'work/input_s2.json').write_text('[10, 20]')
    checker = base / 'check_s2'
    checker.write_text(f'#!{sys.executable}\nfrom pathlib import Path\n'
                       "assert Path('work/result_s2.json').read_text() == '30'\n"
                       "print('TOKEN: current-fixture-token')\n")
    checker.chmod(0o755)
    document = base / 'task_s2.md'
    document.write_text('''# 文件转换
读取 `test_cases/s2_cases.json` 和 `work/input_s2.json`。
将总和写入 `work/result_s2.json`，运行 `./check_s2`，提交输出的 TOKEN。
## 提交形式
```json
{"token":"xxx"}
```
''')
    return base, document, checker


class EvolutionV2Tests(unittest.TestCase):
    def test_transport_headers_preserve_status_and_strict_final_marker(self):
        for duration in ('', '[durationMs:42]\n'):
            raw = '[exitCode:0]\n' + duration + 'TASK_RUNTIME {"http_successes":1}\nFINAL_ANSWER\n42'
            cleaned, report = runtime_result(raw)
            self.assertEqual(report['http_successes'], 1)
            self.assertEqual(sandbox_result(cleaned)[2], '42')
            self.assertEqual(sandbox_result('[exitCode:0]\n'+duration+'FINAL_ANSWER\n42')[2], '42')
        for raw in ('[TIMEOUT]\n[durationMs:15000]\nFINAL_ANSWER\n42',
                    '[exitCode:1]\n[durationMs:42]\nFINAL_ANSWER\n42',
                    '[JUDGER_ERROR]\n[durationMs:1]\nFINAL_ANSWER\n42',
                    '[exitCode:0]\n[durationMs:bad]\nFINAL_ANSWER\n42',
                    '[exitCode:0]\n[durationMs:1]\nnoise\nFINAL_ANSWER\n42',
                    '[exitCode:0]\n[durationMs:1]\nFINAL_ANSWER\n42\n[TRUNCATED]',
                    '[exitCode:0]\n[durationMs:1]\nFINAL_ANSWER\n'+'x'*65536):
            self.assertIsNone(sandbox_result(runtime_result(raw)[0])[2])

    def test_named_checker_from_current_document_runs_without_spec_and_binds_method(self):
        with tempfile.TemporaryDirectory() as directory:
            base, doc, checker = file_task(Path(directory))
            original = checker.read_bytes()
            agent = Agent(Config(layout_mode='explicit'))
            payload = task_payload(1, f'请阅读{doc}，获取任务信息')
            discovery = agent.decide(payload)
            payload.update(roundNo=2, lastCmdResult=v2_result(discovery, '/'))
            response = agent.decide(payload)
            mem = next(iter(agent.sessions.values()))
            self.assertEqual(mem.task_directory(), str(base))
            self.assertEqual(mem.contract['checker'], 'check_s2')
            self.assertFalse(mem.contract['repair_spec'])
            cases = next(f for f in mem.inputs['files'] if f['path'].startswith('test_cases/'))
            self.assertEqual(cases['type'], 'dict')
            self.assertEqual(cases['keys'], ['cases'])
            self.assertIn('expected', cases['sample'])
            self.assertIn('inputPreview', response['prompt'])
            # Recognized pair-based tasks now validate and check in one sandbox call.
            code = "def transform(records):\n    return sum(records)"
            payload.update(roundNo=3, lastCmdResult='', llmResp='PYTHON\n'+code)
            execution = agent.decide(payload)
            payload.update(roundNo=4, llmResp='', lastCmdResult=v2_result(execution, '/'))
            submitted = agent.decide(payload)
            self.assertFalse(submitted['executeCmd'])
            answer = json.loads(submitted['roleCommandMap']['11']['taskAnswer'])
            self.assertEqual(answer, {'token': 'current-fixture-token'})
            self.assertIsNotNone(mem.submitted_method)
            payload.update(roundNo=5, phaseTask='', lastCmdResult='', lastRoundRoleActionResults={'11': True})
            agent.decide(payload)
            self.assertEqual(len(mem.skills), 1)
            self.assertNotIn('current-fixture-token', json.dumps(mem.skills))
            self.assertEqual(checker.read_bytes(), original)
            self.assertFalse((base / 'spec.md').exists())

    def test_failed_or_ambiguous_checker_never_supplies_a_token(self):
        with tempfile.TemporaryDirectory() as directory:
            base, doc, checker = file_task(Path(directory))
            contract = answer_contract([{'kind':'read', 'resolved_path':str(doc), 'output':doc.read_text()}])
            self.assertEqual(contract['checker'], 'check_s2')
            for suffix in ('raise SystemExit(1)', "print('TOKEN: second')"):
                checker.write_text(f'#!{sys.executable}\nprint("TOKEN: misleading")\n'+suffix)
                raw = v2_result({'executeCmd':'python3 -c '+shlex.quote(
                    engineering_code(str(base), False, 'check_s2', False))}, directory)
                self.assertIsNone(sandbox_result(raw)[2])

    def test_zero_logs_require_matching_records_but_real_zero_is_valid(self):
        for matched in (False, True):
            with self.subTest(matched=matched), tempfile.TemporaryDirectory() as directory:
                code = ("def parse_line(system,line):\n    return "
                        + ("(system,0,False,'')" if matched else 'None')
                        + "\nparse_line('monitor','[12:00:00] INFO module=x')\n"
                          "print('FINAL_ANSWER')\nprint('{\"total_events\":0}')")
                mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                             pending=('cmd', 1), running_python=code,
                             contract={'example': {'total_events':0}, 'input_kind':'logs'})
                raw = v2_result({'executeCmd':'python3 -c '+shlex.quote(runtime_code(code))}, directory)
                _, _, ledger = step(mem, 2, lastCmdResult=raw)
                if matched:
                    self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), {'total_events':0})
                    self.assertEqual(mem.last_attempt['parseCoverage']['matched'], 1)
                else:
                    self.assertFalse(ledger.commands)
                    self.assertTrue(mem.input_blocked)
                    self.assertEqual(mem.last_attempt['parseCoverage']['unmatched'], ['[12:00:00] INFO module=x'])

    def test_unmarked_zero_restatement_cannot_bypass_coverage_check(self):
        for output in ('{"count":0}', ''):
            with self.subTest(supported=bool(output)):
                mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                             pending=('task',1), supported_python='print(0)', supported_output=output,
                             contract={'example': {'count':0}, 'input_kind':'logs'})
                _, _, ledger = step(mem, 2, llmResp='ANSWER\n{"count":0}')
                self.assertFalse(ledger.commands)
                self.assertTrue(mem.input_blocked)

    def test_declared_rounding_handles_half_minutes_without_changing_default_runtime(self):
        doc='# 事件时长\n平均时长四舍五入到整数分钟。\n## 提交形式\n```json\n{"avg_duration_minutes":0}\n```\n'
        contract=answer_contract([{'kind':'read','output':doc,'path':'task.md'}])
        self.assertEqual(contract['rounding'],'half_up')
        code="print('FINAL_ANSWER')\nprint(round(2.5))"
        with tempfile.TemporaryDirectory() as directory:
            for rounding,expected in ((contract['rounding'],'3'),(None,'2')):
                raw=v2_result({'executeCmd':'python3 -c '+shlex.quote(runtime_code(code,rounding=rounding))},directory)
                self.assertEqual(sandbox_result(runtime_result(raw)[0])[2].strip(),expected)

    def test_same_code_with_changed_comments_is_not_a_new_attempt(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True)
        for round_no in (2, 3, 4):
            mem.pending=('task',round_no-1)
            mem.python=None
            payload=task_payload(round_no, 'query')
            payload['llmResp']=f'PYTHON\n# changed comment {round_no}\nprint(42)'
            turn,cfg,_,_=setup_case(payload)
            mem.observe(turn,cfg)
        self.assertIsNone(mem.python)
        self.assertIn('同一内容已尝试两次', mem.history[-1]['error'])

    def test_same_output_from_different_code_requests_diagnosis(self):
        mem=Memory(task_text='query', task_started=1, bootstrap_done=True)
        for round_no in (2,3):
            mem.pending=('cmd',round_no-1)
            mem.running_python=f'print({round_no})'
            payload=task_payload(round_no,'query')
            payload['lastCmdResult']='[exitCode:0]\n[durationMs:13]\n{"count":0}'
            turn,cfg,_,_=setup_case(payload)
            mem.observe(turn,cfg)
        self.assertTrue(mem.diagnostic_pending)

    def test_parser_hint_reuses_method_across_shapes_without_old_data_or_aggregate(self):
        code="import re\nold_input='/tmp/day1/private.json'\n" \
             "def parse_line(system,line):\n    return (system,line,False,'')\n" \
             "def solve():\n    return old_input\nprint({'old_answer':123})"
        parser=parser_method(code)
        self.assertTrue(parser)
        first={'example':{'errors':{'monitor':0}}, 'family':'logs/task_l#.md', 'input_kind':'logs'}
        second={'example':{'total_events':0}, 'family':'logs/task_l#.md', 'input_kind':'logs'}
        skill=learned_method((6,5),first,parser=parser)
        self.assertTrue(compatible(skill,(6,5),second,for_hint=True))
        self.assertFalse(compatible(skill,(6,5),second))
        stored=json.dumps(skill)
        self.assertNotIn('private.json',stored)
        self.assertNotIn('old_answer',stored)
        self.assertNotIn('123',stored)
        self.assertNotIn('solve',stored)
        self.assertIsNone(parser_method("def parse_line(system,line):\n    return old_data[line]"))
        self.assertIsNone(parser_method("def parse_line(system,line):\n    return open('/tmp/old').read()"))

    def test_confirmed_plain_parser_is_saved_and_reused_with_fresh_files_and_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory,'logs')
            (base/'day1').mkdir(parents=True)
            (base/'day2').mkdir()
            (base/'day1/monitor_a.log').write_text('[12:00:00] ERROR module=first\n')
            (base/'day2/monitor_a.log').write_text('[14:00:00] ERROR module=new\n[14:01:00] ERROR module=new\n')
            documents=[]
            for index,example in ((1,{'errors':{'monitor':0}}),(2,{'total_failures':0})):
                doc=base/f'task_l{index}.md'
                doc.write_text(f'# 日志统计\n读取 `day{index}/` 下的日志文件。\n'
                               '## 提交形式\n```json\n'+json.dumps(example)+'\n```\n')
                documents.append(doc)
            agent=Agent(Config(layout_mode='explicit'))
            payload=task_payload(1,f'请阅读{documents[0]}，获取任务信息')
            discovery=agent.decide(payload)
            payload.update(roundNo=2,lastCmdResult=v2_result(discovery,'/'))
            agent.decide(payload)
            prefix="import json\nfrom pathlib import Path\ndef parse_line(system,line):\n    return (system,line,'ERROR' in line,'')\n"
            first=prefix+"rows=[parse_line('monitor',line) for line in Path('day1/monitor_a.log').read_text().splitlines()]\n" \
                         "print('FINAL_ANSWER')\nprint(json.dumps({'errors':{'monitor':sum(row[2] for row in rows)}}))"
            payload.update(roundNo=3,lastCmdResult='',llmResp='PYTHON\n'+first)
            execution=agent.decide(payload)
            payload.update(roundNo=4,llmResp='',lastCmdResult=v2_result(execution,'/'))
            submitted=agent.decide(payload)
            self.assertEqual(json.loads(submitted['roleCommandMap']['11']['taskAnswer']),{'errors':{'monitor':1}})
            mem=next(iter(agent.sessions.values()))
            self.assertFalse(mem.skills)
            payload.update(roundNo=5,phaseTask='',lastCmdResult='',lastRoundRoleActionResults={'11':True})
            agent.decide(payload)
            self.assertEqual(len(mem.skills),1)
            parser=mem.skills[0]['parser']
            self.assertNotIn('day1',parser)
            self.assertNotIn('first',parser)
            (base/'day1/monitor_a.log').unlink()
            payload.update(roundNo=6,phaseTask=f'请阅读{documents[1]}，获取任务信息')
            discovery=agent.decide(payload)
            payload.update(roundNo=7,lastCmdResult=v2_result(discovery,'/'))
            prompt=agent.decide(payload)['prompt']
            self.assertIn(mem.skills[0]['id'],prompt)
            self.assertIn('parse_line',prompt)
            second=parser+"\nfrom pathlib import Path\nrows=[parse_line('monitor',line) for line in Path('day2/monitor_a.log').read_text().splitlines()]\n" \
                          "print('FINAL_ANSWER')\nprint(json.dumps({'total_failures':sum(row[2] for row in rows)}))"
            payload.update(roundNo=8,lastCmdResult='',llmResp='PYTHON\n'+second)
            execution=agent.decide(payload)
            payload.update(roundNo=9,llmResp='',lastCmdResult=v2_result(execution,'/'))
            submitted=agent.decide(payload)
            self.assertEqual(json.loads(submitted['roleCommandMap']['11']['taskAnswer']),{'total_failures':2})


if __name__ == '__main__':
    unittest.main()
