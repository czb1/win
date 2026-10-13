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


    def test_feedback_keeps_arbitrary_field_path_and_allows_extraction_repairs(self):
        feedback = '键值比对不通过: $/summary/device_label: 值不符'
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     contract={'input_kind': 'logs', 'example': {'summary': {'device_label': ''}}},
                     submission_feedback=feedback)
        prompt, _, _ = step(mem, 2)
        self.assertIn(feedback, prompt)
        self.assertIn('修复完整程序', prompt)
        self.assertIn('允许修改关联函数和调用方', prompt)
        self.assertNotIn('定点修正故障筛选或事件合并', prompt)
