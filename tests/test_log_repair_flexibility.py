"""Generic repair transitions and current-input evidence, without hidden answers."""
import ast
import json
from pathlib import Path
import tempfile
import unittest

from test_log_evidence import execute
from test_evolution_v2 import step
from test_log_parser_repair import BAD, GOOD_FUNCTION
from agent.intelligence import Memory
from agent.task_log_repair import merge_repair
from agent.task_evidence import inherited_rules
from agent.task_skills import learned_method


class RepairFlexibilityTests(unittest.TestCase):
    def test_equivalent_import_subsets_and_split_imports(self):
        original = 'from datetime import datetime, timedelta\nimport re, json\n' + BAD.split('import re\n')[1]
        patch = 'import json\nimport re\nfrom datetime import datetime\n' + GOOD_FUNCTION
        merged = merge_repair(original, patch, {'targets': ['parse_line']})
        imports = [n for n in ast.parse(merged).body if isinstance(n, (ast.Import, ast.ImportFrom))]
        self.assertEqual(len(imports), 2)
        for conflict in ('import math as re\n', 'from calendar import timegm as datetime\n'):
            with self.assertRaisesRegex(ValueError, '原来源=.*新来源='):
                merge_repair(original, conflict + GOOD_FUNCTION, {'targets': ['parse_line']})
        with self.assertRaisesRegex(ValueError, '程序变量'):
            merge_repair(original + '\nre = 7\n', 'import re\n' + GOOD_FUNCTION, {'targets': ['parse_line']})

    def test_type_change_allows_full_program_and_submits_only_after_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'device.log').write_text('10 NORMAL\n20 FAULT\n')
            bad = "def parse_device_log(path):\n with open(path) as f:\n  return [int(line.split()[0]) for line in f]\nrows=parse_device_log('device.log')\ntask_result({'count':len(rows)})"
            raw, _, report = execute(bad, directory)
            self.assertEqual(report['logs']['parsers']['parse_device_log']['invalid_element'], 'int')
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('cmd',1),
                         running_python=bad, contract={'input_kind':'logs','example':{'count':0}},
                         inputs={'directory':directory,'files':[{'path':'device.log'}]})
            prompt, _, ledger = step(mem,2,lastCmdResult=raw)
            self.assertFalse(ledger.commands)
            self.assertEqual(mem.repair_mode, 'full')
            self.assertIn('完整修复程序', prompt)
            good = "def parse_device_log(path):\n with open(path) as f:\n  return [{'system':'device','timestamp':int(l.split()[0]),'is_fault':l.split()[1]=='FAULT'} for l in f]\nrows=parse_device_log('device.log')\ntask_result({'count':sum(r['is_fault'] for r in rows)})"
            _, command, _ = step(mem,3,llmResp='PYTHON\n'+good)
            self.assertTrue(command)
            raw, _, _ = execute(mem.running_python, directory)
            _, _, ledger = step(mem,4,lastCmdResult=raw)
            self.assertEqual(json.loads(ledger.commands['11']['taskAnswer']), {'count':1})

    def test_nonempty_zero_records_and_all_normal_are_distinct(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, 'device.log'); path.write_text('10 NORMAL\n20 NORMAL\n')
            base = "def parse_device_log(path):\n with open(path) as f:\n  lines=list(f)\n return %s\nrows=parse_device_log('device.log')\ntask_result({'count':len(rows)})"
            _, result, report = execute(base % '[]', directory)
            self.assertNotEqual(result[0], 'ok')
            self.assertIn('coverage_gap', report['logs']['parsers']['parse_device_log'])
            self.assertEqual(report['logs']['files'][str(path)]['sample'][0], '10 NORMAL\n')
            rows = "[{'system':'device','timestamp':int(l.split()[0]),'is_fault':False} for l in lines]"
            _, result, report = execute(base % rows, directory)
            self.assertEqual(result[0], 'ok')
            self.assertEqual(report['logs']['parsers']['parse_device_log']['normal'], 2)
            self.assertEqual(report['logs']['parsers']['parse_device_log']['failures'], 0)
            path.write_text('')
            _, result, _ = execute(base % '[]', directory)
            self.assertEqual(result[0], 'ok')

    def test_repeated_local_rejection_changes_prompt_and_acceptance_together(self):
        mem = Memory(task_text='query',task_started=1,bootstrap_done=True,pending=('task',1),
                     contract={'input_kind':'logs','example':{'count':0}}, repair_python=BAD,
                     input_blocked=True,log_diagnostics={'logs':{'parsers':{'parse_line':{'unmatched_count':1}}}})
        patch = 'import math as re\n' + GOOD_FUNCTION
        step(mem,2,llmResp='PYTHON\n'+patch)
        prompt, _, _ = step(mem,3,llmResp='PYTHON\n'+patch)
        self.assertEqual(mem.repair_mode, 'full')
        self.assertIn('完整修复程序', prompt)
        self.assertIsNone(mem.active_repair())
        _, code, _ = step(mem,4,llmResp='PYTHON\n'+BAD.replace("r'(\\d+) (\\w+) module=(\\S+)'", "r'(\\d+)\\s+(\\w+)\\s+module=(\\S+)'").replace("{'count':sum", "{'count':int(0)+sum"))
        self.assertTrue(code)

    def test_rule_reference_keeps_definitions_and_new_snapshot_replaces_them(self):
        contract={'input_kind':'logs','family':'generic/task#.md','example':{'count':0},'source':'first.md'}
        first=learned_method((6,5),contract,rules='故障定义：状态为 BROKEN')
        second=learned_method((6,5),contract,rules='故障信号定义与上一题相同')
        mem=Memory(task_point=(6,5),contract=contract,skills=[first,second],
                   documents=[{'output':'故障信号定义与上一题相同\n\n日志格式与上一题完全相同'}])
        self.assertTrue(mem.same_log_format())
        self.assertIn('BROKEN', inherited_rules(mem.skills))
        self.assertEqual(first['rule_sources'], ['first.md'])
        newer={'rules':'故障定义：状态为 STOPPED','rules_snapshot':True}
        self.assertNotIn('BROKEN', inherited_rules([first, second, newer]))
        mem.documents=[{'output':'日志格式与上一题完全相同\n\n故障定义：状态为 STOPPED'}]
        self.assertFalse(mem.same_log_format())

    def test_deadline_does_not_request_unverified_answer(self):
        mem=Memory(task_text='query',task_started=1,task_timeout=15,bootstrap_done=True,
                   contract={'input_kind':'logs','example':{'count':0}},input_blocked=True)
        prompt,code,ledger=step(mem,14)
        self.assertFalse(prompt);self.assertFalse(code);self.assertFalse(ledger.commands)
