"""Weak-model regressions: generic samples, result transport, and round budget."""
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest

from test_evolution_v2 import step, v2_result
from agent.intelligence import Memory, parse_task_reply, sandbox_result
from agent.task_runtime import runtime_code, runtime_result
from agent.task_evidence import computed_answer
from agent.task_inputs import preview_code, input_context
from agent.task_transform import transform_config, transform_code, transform_method, case_page_code
from agent.task_skills import learned_method


class WeakModelTests(unittest.TestCase):
    def fixture(self, root):
        cases = [{'input': [{'key':'z','rank':2,'value':8}, {'key':'a','rank':5,'value':3}],
                  'expected': ['a','z']}, {'input': [], 'expected': []}]
        (root/'examples.json').write_text(json.dumps({'cases':cases}))
        (root/'data.json').write_text('[{"key":"fresh","rank":7,"value":1}]')
        checker=root/'check_custom'
        checker.write_text(f'#!{sys.executable}\nimport json\nfrom pathlib import Path\n'
                           "Path('checked').write_text('yes')\n"
                           "if json.loads(Path('report.json').read_text()) == ['fresh']:\n print('TOKEN: live-token')\n"
                           "else:\n print('[FAIL] wrong data')\n")
        checker.chmod(0o755)
        doc=root/'task_9.md'
        doc.write_text('查看 `examples.json`，读取 `data.json`，写入 `report.json`，运行 `./check_custom`。')
        raw=v2_result({'executeCmd':'python3 -c '+shlex.quote(preview_code(repr(str(doc)),repr(doc.read_text())))},str(root))
        preview=input_context(raw)
        contract={'kind':'check_token','repair_spec':False,'checker':'check_custom','workspace':str(root),'example':{'token':''},'family':'convert/task#.md'}
        documents=[{'kind':'discover','path':str(doc),'resolved_path':str(doc),'output':doc.read_text()}]
        config=transform_config(documents,contract,preview)
        self.assertIsNotNone(config)
        return config,contract,documents,preview

    def execute(self, code, config):
        raw=v2_result({'executeCmd':'python3 -c '+shlex.quote(transform_code(code,config))},'/')
        return sandbox_result(raw)

    def test_complete_sample_preview_and_narrow_detection(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); cfg,contract,docs,preview=self.fixture(root)
            entry=next(x for x in preview['files'] if x.get('case_count'))
            self.assertTrue(entry['complete'])
            self.assertEqual(entry['content'], (root/'examples.json').read_text())
            self.assertEqual(json.loads(entry['content'])['cases'][0]['expected'],['a','z'])
            self.assertNotIn('case_samples',entry)
            self.assertNotIn('sample',entry)
            self.assertIsNone(transform_config(docs,{**contract,'repair_spec':True},preview))
            self.assertIsNone(transform_config([{'output':'write unknown'}],contract,preview))
            docs[0]['output']+=' 写入 `other.json`'
            self.assertIsNone(transform_config(docs,contract,preview))

    def test_all_cases_gate_artifact_and_checker_with_precise_difference(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cfg,*_=self.fixture(root)
            (root/'report.json').write_text('old output')
            bad="def transform(records):\n return [r['key'] for r in records]"
            status,body,answer=self.execute(bad,cfg)
            self.assertNotEqual(status,'ok');self.assertIsNone(answer)
            self.assertIn('"case": 0',body);self.assertIn('$[0]',body)
            self.assertFalse((root/'checked').exists())
            self.assertEqual((root/'report.json').read_text(),'old output')
            # First case correct is not enough: second case must also pass.
            status,body,_=self.execute("def transform(records):\n return ['a','z']",cfg)
            self.assertIn('"case": 1',body)
            self.assertFalse((root/'checked').exists())

    def test_same_command_validates_writes_checks_and_only_accepts_real_token(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cfg,*_=self.fixture(root)
            good="def transform(records):\n return [r['key'] for r in sorted(records,key=lambda r:-r['rank'])]"
            status,body,answer=self.execute(good,cfg)
            self.assertEqual(status,'ok');self.assertEqual(json.loads(answer),{'token':'live-token'})
            self.assertEqual(json.loads((root/'report.json').read_text()),['fresh'])
            (root/'check_custom').write_text(f'#!{sys.executable}\nprint("[FAIL] no")\nprint("TOKEN: fake")\n')
            status,body,answer=self.execute(good,cfg)
            self.assertNotEqual(status,'ok');self.assertIsNone(answer)
            for bad in ("def transform(records):\n print('TOKEN: fake')\n return []",
                        "def transform(records):\n return []\nopen('report.json','w').write('x')"):
                with self.assertRaises(ValueError):transform_method(bad)


    def test_case_pagination_preserves_whole_pairs_and_next_index(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);cfg,*_=self.fixture(root)
            cases=[{'input':[str(i)*800], 'expected':str(i)*800} for i in range(6)]
            (root/'examples.json').write_text(json.dumps({'cases':cases}))
            raw=v2_result({'executeCmd':'python3 -c '+shlex.quote(case_page_code(cfg,0))},'/')
            lines=raw.splitlines()
            page=json.loads(next(line for line in lines if line.startswith('{')))
            self.assertEqual(page['case_samples'][0]['input'],cases[0]['input'])
            self.assertLess(len(page['case_samples']),len(cases))
            next_index=int(next(line for line in lines if line.startswith('NEXT_CASE')).rsplit(' ',1)[1])
            self.assertEqual(next_index,len(page['case_samples']))
            raw=v2_result({'executeCmd':'python3 -c '+shlex.quote(case_page_code(cfg,next_index))},'/')
            page=json.loads(next(line for line in raw.splitlines() if line.startswith('{')))
            self.assertEqual(page['case_samples'][0]['case'],next_index)


    def test_generic_budget_gate_distinguishes_wait_from_no_time(self):
        mem=Memory(task_text='query',task_started=1,task_timeout=15,bootstrap_done=True,
                   task_failures=5)
        # Rejection count does not block a request with enough time.
        prompt,cmd,_=step(mem,13)
        self.assertTrue(prompt)
        self.assertFalse(cmd)
        mem.pending=None
        # The question's token wording no longer adds a special budget gate.
        mem.task_text = '运行验证，提交 TOKEN'
        prompt, cmd, ledger = step(mem, 14)
        self.assertTrue(prompt)
        self.assertFalse(cmd or ledger.commands)
        self.assertIn('"allowedActions": ["ANSWER"]', prompt)
        mem.pending = None
        # Unresolved execution errors still need a model/execute/submit cycle.
        mem.input_blocked = True
        for turn, remaining, required in [(14,2,3),(15,1,2)]:
            with self.assertLogs('agent.intelligence', 'INFO') as logs:
                prompt,cmd,ledger=step(mem,turn)
            self.assertFalse(prompt or cmd or ledger.commands)
            gate=next(r.data for r in logs.records if getattr(r,'event',None)=='task_budget_gate')
            self.assertEqual(gate['remaining_rounds'],remaining)
            self.assertEqual(gate['required_rounds'],required)
        # Waiting for an in-flight reply is not reported as an idle budget gate.
        mem.pending=('cmd',15)
        with self.assertLogs('agent.intelligence', 'INFO') as logs:
            import logging
            logging.getLogger('agent.intelligence').info('capture start')
            step(mem,15)
        self.assertFalse(any(getattr(r,'event',None)=='task_budget_gate' for r in logs.records))

    def test_raw_code_and_comment_compaction_without_accepting_prose(self):
        code="import json\nresult={'x':3}\nprint(json.dumps(result))"
        self.assertEqual(parse_task_reply(code),{'python':code})
        self.assertIsNone(parse_task_reply('Here is code\nimport json\nprint(3)'))
        mem=Memory(task_text='query',task_started=1,bootstrap_done=True,pending=('task',1))
        _,cmd,_=step(mem,2,llmResp='PYTHON\n'+('# explanatory text\n'*900)+'print(7)')
        self.assertTrue(cmd)
        self.assertEqual(mem.running_python.strip(),'print (7 )')

    def test_result_helper_is_protected_and_legacy_output_is_strict(self):
        contract={'input_kind':'logs','example':{'count':0}}
        good="RESULT {'count': 4}"
        self.assertEqual(json.loads(computed_answer(good,contract)),{'count':4})
        for text in ("RESULT {'count':1,'count':2}","RESULT {'count': __import__('os').getpid()}",
                     "noise\n"+good,good+'\n'+good,"RESULT {'count': (4,)}"):
            self.assertIsNone(computed_answer(text,contract))
        self.assertIsNone(computed_answer("RESULT {'token':'fake'}",{'kind':'check_token','example':{'token':''}}))
        with tempfile.TemporaryDirectory() as d:
            code="def task_result(value):\n print('RESULT',value)\ntask_result({'count':4})"
            raw=v2_result({'executeCmd':'python3 -c '+shlex.quote(runtime_code(code,d,log_task=True))},'/')
            self.assertIsNone(sandbox_result(runtime_result(raw)[0])[2])  # No silent rewriting of model functions.
        mem=Memory(task_text='query',task_started=1,bootstrap_done=True,pending=('cmd',1),contract=contract,
                   running_python='print(result)')
        _,_,ledger=step(mem,2,lastCmdResult='[exitCode:0]\n'+good)
        self.assertFalse(ledger.commands)  # Unmarked output needs an explicit ANSWER.


    def test_deadline_prompt_does_not_override_answer_only(self):
        mem=Memory(task_text='query',task_started=1,task_timeout=15,bootstrap_done=True,
                   contract={'input_kind':'logs','example':{'count':0}},
                   supported_output='{"count":0}', supported_python='task_result({"count":0})')
        prompt,cmd,_=step(mem,14)
        self.assertIn('只允许：第一行 ANSWER',prompt)
        self.assertNotIn('第一行必须写 PYTHON',prompt)

if __name__=='__main__':unittest.main()
