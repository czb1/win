"""Regression fixtures for the L-3 match failures, independent of hidden answers."""
import json
from pathlib import Path
import shlex
import tempfile
import unittest

from test_agent import setup_case
from test_evolution import task_payload
from test_evolution_v2 import step, v2_result
from agent.intelligence import Memory, Intelligence, sandbox_result
from agent.task_runtime import runtime_code, runtime_result
from agent.task_evidence import computed_answer, evidence_matches, parsing_rules
from agent.task_skills import parser_method, learned_method, compatible


PARSER = '''
from datetime import datetime
import re
FAULTS = ('OOM-killer', 'disk-full', 'net-down')
def parse_monitor_log(path):
    result = []
    with open(path) as source:
        for line in source:
            result.append(('monitor', datetime.strptime(line[1:20], '%Y-%m-%d %H:%M:%S'), '] ERROR ' in line))
    return result

def parse_gateway_log(path):
    result = []
    with open(path) as source:
        for line in source:
            status = int(line.rsplit(' ', 1)[1])
            result.append(('gateway', datetime.strptime(line.split('[')[1].split(' ')[0], '%d/%b/%Y:%H:%M:%S'), 500 <= status < 600))
    return result

def parse_logistics_log(path):
    result = []
    with open(path) as source:
        for line in source:
            result.append(('logistics', datetime.strptime(line[:15], '%b %d %H:%M:%S'), any(signal in line for signal in FAULTS)))
    return result
'''
CONTRACT = {'input_kind':'logs', 'family':'logs/task_l#.md',
            'example': {'total_events':0, 'avg_duration_minutes':0,
                        'longest_event':{'system':'monitor','duration_minutes':0}}}


def execute(code, directory, parser=None):
    raw = v2_result({'executeCmd':'python3 -c '+shlex.quote(runtime_code(code, directory, log_task=True, parser=parser))}, directory)
    clean, report = runtime_result(raw)
    return raw, sandbox_result(clean), report


class LogEvidenceTests(unittest.TestCase):
    def test_only_single_clean_finite_object_can_replace_missing_marker(self):
        for text in ('{"total_events":21}', 'FINAL_ANSWER\n{"total_events":21}'):
            self.assertEqual(json.loads(computed_answer(text, CONTRACT)), {'total_events':21})
        for text in ('noise\n{"total_events":21}', '{"total_events":21}\n{"total_events":15}',
                     '{"total_events":NaN}', '{"total_events":1e999}', '{"total_events":21,"total_events":15}',
                     '{"total_events":"21"}', '{"extra":21}'):
            self.assertIsNone(computed_answer(text, CONTRACT))
        self.assertIsNone(computed_answer('{"token":"guess"}', {'kind':'check_token','example':{'token':''}}))
        self.assertFalse(evidence_matches('{"total_events":15}', '{"total_events":21}', CONTRACT))
        self.assertTrue(evidence_matches('{"total_events":21}', '{"total_events":21,"avg_duration_minutes":5}', CONTRACT))

    def test_model_cannot_change_computed_statistics_or_bypass_final_gate(self):
        mem=Memory(task_text='query',task_started=1,bootstrap_done=True,pending=('task',1),
                   contract=CONTRACT.copy(),supported_python='print(21)',supported_output='{"total_events":21}')
        _,_,ledger=step(mem,2,llmResp='ANSWER\n{"total_events":15}')
        self.assertFalse(ledger.commands)
        mem.answer='{"total_events":15}'
        _,_,ledger=step(mem,3)
        self.assertFalse(ledger.commands)

    def test_clean_json_auto_submits_and_failed_or_noisy_output_does_not(self):
        for header, body, accept in [('[exitCode:0]','{"total_events":21}',True),
                                    ('[exitCode:1]','{"total_events":21}',False),
                                    ('[exitCode:0]','debug\n{"total_events":21}',False),
                                    ('[exitCode:0]','{"total_events":21}\n[TRUNCATED]',False)]:
            mem=Memory(task_text='query',task_started=1,bootstrap_done=True,pending=('cmd',1),
                       running_python='print(21)',contract=CONTRACT.copy())
            _,_,ledger=step(mem,2,lastCmdResult=header+'\n[durationMs:12]\n'+body)
            self.assertEqual(bool(ledger.commands),accept)

    def test_verified_file_parsers_reused_on_new_files_and_new_aggregation(self):
        parser=parser_method(PARSER+"\nold_input='/tmp/old-secret.log'\nold_answer=999")
        self.assertTrue(parser)
        self.assertNotIn('old-secret',parser)
        self.assertNotIn('999',parser)
        rules='故障定义：monitor ERROR；gateway 5xx；logistics OOM-killer/disk-full/net-down'
        first={'input_kind':'logs','family':CONTRACT['family'],'example':{'failures':0}}
        skill=learned_method((6,5),first,parser=parser,rules=rules, parser_evidence={name: {'record_contract':'structured-v1'} for name in ('parse_monitor_log','parse_gateway_log','parse_logistics_log')})
        mem=Memory(task_point=(6,5),contract=CONTRACT.copy(),skills=[skill],
                   documents=[{'output':'请使用与上一题完全相同的日志格式和解析方法'}])
        self.assertEqual(mem.reusable_parser(),skill)
        self.assertTrue(compatible(skill,(6,5),CONTRACT,for_hint=True))
        self.assertFalse(compatible(skill,(6,5),CONTRACT))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'monitor.log').write_text('[2025-10-09 00:17:00] ERROR x\n[2025-10-09 00:12:00] ERROR x\n[2025-10-09 00:13:00] ERROR x\n')
            (root/'gateway.log').write_text('ip - - [09/Oct/2025:00:10:00 +0800] "GET / HTTP/1.1" 500\nip - - [09/Oct/2025:00:01:00 +0800] "GET / HTTP/1.1" 404\n')
            (root/'logistics.log').write_text('Oct 09 00:01:00 host daemon: OOM-killer killed\nOct 09 00:06:00 host daemon: disk-full full\nOct 09 00:12:00 host daemon: net-down restarted\nOct 09 00:40:00 host daemon: process restarted successfully\n')
            # Weak model attempts to redefine a verified parser; the current paths still bind fresh data.
            code="def parse_logistics_log(path):\n    return []\nall_events=[]\n"
            code+="for system in ('monitor','gateway','logistics'):\n    ts=[row[1] for row in task_parse(system,system+'.log') if row[2]]\n    for start,end in task_events(system,ts):\n        all_events.append((system,(end-start).total_seconds()/60+1))\n"
            code+="longest=max(all_events,key=lambda x:x[1])\ntask_result({'total_events':len(all_events),'avg_duration_minutes':round(sum(x[1] for x in all_events)/len(all_events)), 'longest_event':{'system':longest[0],'duration_minutes':longest[1]}})"
            _, result, report=execute(code,directory,parser)
            self.assertEqual(result[0],'ok')
            answer=json.loads(result[2])
            self.assertEqual(answer['total_events'],4)
            self.assertEqual(report['logs']['events']['monitor']['samples'][0],['2025-10-09 00:12:00','2025-10-09 00:17:00'])
            self.assertEqual(report['logs']['events']['logistics']['count'],2)
            self.assertEqual(report['logs']['parsers']['parse_logistics_log']['returned_records'],4)
            self.assertEqual(len(report['logs']['files']),3)
        skill['disabled']=True
        self.assertIsNone(mem.reusable_parser())

    def test_missing_file_and_swallowed_timestamp_error_cannot_be_successful(self):
        with tempfile.TemporaryDirectory() as directory:
            for code in ("try:\n    open('missing.log')\nexcept FileNotFoundError:\n    pass\ntask_result({'total_events':0})",
                         "def parse_monitor_log(path):\n    try:\n        int('bad timestamp')\n    except ValueError:\n        pass\n    return []\nparse_monitor_log('file.log')\ntask_result({'total_events':0})"):
                _, result, report=execute(code,directory)
                self.assertNotEqual(result[0],'ok')
                self.assertTrue(report['logs']['errors'])

    def test_partial_read_and_missing_system_are_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'a.log').write_text('one\ntwo\n')
            _,result,_=execute("with open('a.log') as f:\n    f.readline()\ntask_result({'total_events':1})",directory)
            self.assertNotEqual(result[0],'ok')
            code="with open('a.log') as f:\n    f.read()\ntask_result({'total_events':1})"
            raw,_,_=execute(code,directory)
            mem=Memory(task_text='query',task_started=1,bootstrap_done=True,pending=('cmd',1),running_python=code,
                       contract=CONTRACT.copy(),inputs={'directory':directory,'files':[{'path':'a.log'},{'path':'b.log'}]})
            _,_,ledger=step(mem,2,lastCmdResult=raw)
            self.assertFalse(ledger.commands)
            self.assertTrue(mem.input_blocked)

    def test_real_zero_and_per_line_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory,'normal.log').write_text('normal\n')
            code="def parse_line(system,line):\n    return (system,None,False,'')\nwith open('normal.log') as f:\n    rows=[parse_line('monitor',line) for line in f]\ntask_result({'total_events':0})"
            raw,_,report=execute(code,directory)
            self.assertEqual(report['parse_systems']['monitor']['matched'],1)
            mem=Memory(task_text='query',task_started=1,bootstrap_done=True,pending=('cmd',1),running_python=code,contract=CONTRACT.copy())
            _,_,ledger=step(mem,2,lastCmdResult=raw)
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']),{'total_events':0})

    def test_confirmed_file_parser_promotes_then_runs_automatically_on_next_task(self):
        from agent.brain import Agent
        from agent.config import Config
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'one.log').write_text('[2025-10-09 00:12:00] ERROR x\n')
            (root/'two.log').write_text('[2025-10-10 00:12:00] ERROR x\n[2025-10-10 00:13:00] ERROR x\n')
            for number,filename,example in ((1,'one.log',{'failures':0}),(2,'two.log',{'events':0})):
                (root/f'task_l{number}.md').write_text('# 日志分析\n读取 `'+filename+'` 日志文件。完全相同的解析方法。\n故障定义：ERROR级别。\n## 提交形式\n```json\n'+json.dumps(example)+'\n```\n')
            agent=Agent(Config(layout_mode='explicit'))
            payload=task_payload(1, f"请阅读{root/'task_l1.md'}")
            discovery=agent.decide(payload)
            payload.update(roundNo=2,lastCmdResult=v2_result(discovery,'/'))
            agent.decide(payload)
            code=PARSER+"\ntask_result({'failures':len(parse_monitor_log('one.log'))})"
            payload.update(roundNo=3,lastCmdResult='',llmResp='PYTHON\n'+code)
            response=agent.decide(payload)
            payload.update(roundNo=4,llmResp='',lastCmdResult=v2_result(response,'/'))
            response=agent.decide(payload)
            self.assertEqual(json.loads(response['roleCommandMap']['11']['taskAnswer']),{'failures':1})
            payload.update(roundNo=5,phaseTask='',lastCmdResult='',lastRoundRoleActionResults={'11':True})
            agent.decide(payload)
            mem=next(iter(agent.sessions.values()))
            self.assertEqual(len(mem.skills),1)
            self.assertIn('parse_monitor_log',mem.skills[0]['parser'])
            self.assertNotIn('parse_gateway_log',mem.skills[0]['parser'])
            self.assertIn('ERROR',mem.skills[0]['rules'])
            (root/'one.log').unlink()
            payload.update(roundNo=6,phaseTask=f"请阅读{root/'task_l2.md'}")
            response=agent.decide(payload)
            payload.update(roundNo=7,lastCmdResult=v2_result(response,'/'))
            response=agent.decide(payload)
            self.assertIn('verifiedParsingRules',response['prompt'])
            code="def parse_monitor_log(path):\n    return []\ntask_result({'events':len(task_events('monitor',[r[1] for r in parse_monitor_log('two.log') if r[2]]))})"
            payload.update(roundNo=8,lastCmdResult='',llmResp='PYTHON\n'+code)
            response=agent.decide(payload)
            payload.update(roundNo=9,llmResp='',lastCmdResult=v2_result(response,'/'))
            response=agent.decide(payload)
            self.assertEqual(json.loads(response['roleCommandMap']['11']['taskAnswer']),{'events':1})
            self.assertEqual(mem.log_diagnostics['logs']['events']['monitor']['failures'],2)

    def test_rules_preserve_definitions_not_input_records(self):
        rules=parsing_rules([{'output':'故障定义：OOM-killer/disk-full/net-down\n2024-09-18 ERROR private data\n读取 /tmp/old.log'}])
        self.assertIn('OOM-killer',rules)
        self.assertNotIn('private',rules)
        self.assertNotIn('/tmp/',rules)
        self.assertIsNone(parser_method("def parse_monitor_log(path):\n    return open('old.log').read()"))

if __name__=='__main__':
    unittest.main()
