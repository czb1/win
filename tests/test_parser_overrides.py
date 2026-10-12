"""Current parser fixes must reach all call paths and future learned methods."""
import json
from pathlib import Path
import tempfile
import unittest

from test_log_evidence import execute
from test_evolution_v2 import step
from agent.intelligence import Memory
from agent.task_skills import parser_method

OLD = '''def parse_sensor_line(line):
    return {'system': 'sensor', 'timestamp': 1, 'is_fault': True, 'module': 'old'}
def parse_line(system, line):
    return parse_sensor_line(line)
def parse_sensor_log(path):
    with open(path) as source:
        return [parse_line('sensor', line) for line in source]
'''
FIX = '''def parse_sensor_line(line):
    return {'system': 'sensor', 'timestamp': 1, 'is_fault': True, 'module': line.strip()}
'''


class ParserOverrideTests(unittest.TestCase):
    def test_partial_override_reaches_direct_nested_and_helper_calls_and_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sensor.log').write_text('alpha\nalpha\nbeta\n')
            code = FIX + """
rows = task_parse('sensor', 'sensor.log')
assert parse_line('sensor', 'gamma')['module'] == 'gamma'
assert parse_sensor_line('delta')['module'] == 'delta'
task_result({'labels': [r['module'] for r in rows]})
"""
            raw, result, report = execute(code, directory, OLD)
            self.assertEqual(result[0], 'ok', raw)
            self.assertEqual(json.loads(result[2]), {'labels': ['alpha', 'alpha', 'beta']})
            self.assertEqual(report['overridden_parsers'], ['parse_sensor_line'])
            self.assertEqual(report['parse_lines']['total'], 5)
            self.assertEqual(report['parse_systems']['sensor']['failures'], 5)
            self.assertEqual(report['logs']['parsers']['parse_sensor_line']['calls'], 5)
            contract = {'input_kind': 'logs', 'family': 'sample', 'example': {'labels': []}}
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                         pending=('cmd', 1), running_python=code, running_parser=OLD,
                         contract=contract)
            step(mem, 2, lastCmdResult=raw)
            self.assertTrue(mem.parser_candidate)
            _, later, _ = execute("task_result({'labels': [r['module'] for r in task_parse('sensor','sensor.log')]})", directory, mem.parser_candidate)
            self.assertEqual(later[0], 'ok')
            self.assertEqual(json.loads(later[2]), {'labels': ['alpha', 'alpha', 'beta']})

    def test_line_helper_override_and_runtime_failure_never_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'sensor.log').write_text('omega\n')
            old = parser_method(OLD.split('def parse_sensor_log')[0])
            for replacement, success in ((FIX, True),
                ("def parse_sensor_line(line):\n    raise ValueError('repair needed')\n", False)):
                raw, result, report = execute(replacement + "task_result({'labels': [r['module'] for r in task_parse('sensor','sensor.log')]})", directory, old)
                self.assertEqual(result[0] == 'ok', success, raw)
                if success:
                    self.assertEqual(json.loads(result[2]), {'labels': ['omega']})
                else:
                    self.assertIn('repair needed', raw)
                    self.assertTrue(report['logs']['errors'])

    def test_nested_invalid_record_keeps_function_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            code = "def parse_sensor_line(line):\n    return 1\ndef parse_line(system,line):\n    return parse_sensor_line(line)\nparse_line('sensor','bad')\ntask_result({'count':0})"
            raw, result, report = execute(code, directory)
            self.assertNotEqual(result[0], 'ok', raw)
            self.assertEqual(report['parse_lines']['total'], 1)
            for name in ('parse_line', 'parse_sensor_line'):
                self.assertEqual(report['logs']['parsers'][name]['invalid_return'], 'int')

    def test_feedback_keeps_arbitrary_field_path_and_allows_extraction_repairs(self):
        feedback = '键值比对不通过: $/summary/device_label: 值不符'
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     contract={'input_kind': 'logs', 'example': {'summary': {'device_label': ''}}},
                     submission_feedback=feedback)
        prompt, _, _ = step(mem, 2)
        self.assertIn(feedback, prompt)
        self.assertIn('提取、计算和输出', prompt)
        self.assertIn('同名函数优先', prompt)
        self.assertNotIn('定点修正故障筛选或事件合并', prompt)
