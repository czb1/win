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
                     'not JSON', '[NaN]'):
            self.assertIsNone(computed_answer(text, CONTRACT))
        self.assertIsNone(computed_answer('{"token":"guess"}', {'kind':'check_token','example':{'token':''}}))
        self.assertFalse(evidence_matches('{"total_events":15}', '{"total_events":21}', CONTRACT))
        self.assertTrue(evidence_matches('{"total_events":21}', '{"total_events":21,"avg_duration_minutes":5}', CONTRACT))


    def test_rules_preserve_definitions_not_input_records(self):
        rules=parsing_rules([{'output':'故障定义：OOM-killer/disk-full/net-down\n2024-09-18 ERROR private data\n读取 /tmp/old.log'}])
        self.assertIn('OOM-killer',rules)
        self.assertNotIn('private',rules)
        self.assertNotIn('/tmp/',rules)
        self.assertIsNone(parser_method("def parse_monitor_log(path):\n    return open('old.log').read()"))

if __name__=='__main__':
    unittest.main()
