"""Parser repairs preserve evidence, whitespace and unrelated computation."""
import ast
import json
from pathlib import Path
import tempfile
import unittest

from test_log_evidence import execute
from test_evolution_v2 import step
from agent.intelligence import Memory
from agent.task_log_repair import repair_context, merge_repair, validate_literal_regex


BAD = r'''import re
def parse_line(line, system):
    match = re.match(r'(\d+) (\w+) module=(\S+)', line)
    if not match:
        return None
    return {'system':system, 'timestamp':int(match[1]), 'is_fault':match[2]=='BAD', 'module':match[3]}
with open('sensor.log') as source:
    rows = [parse_line(line, 'sensor') for line in source]
task_result(__import__('json').dumps({'count':sum(row['is_fault'] for row in rows if row is not None)}))
'''
GOOD_FUNCTION = r'''def parse_line(line, system):
    match = re.match(r'(\d+)\s+(\w+)\s+module=(\S+)', line)
    if not match:
        return None
    return {'system':system, 'timestamp':int(match[1]), 'is_fault':match[2]=='BAD', 'module':match[3]}
'''


class LogParserRepairTests(unittest.TestCase):


    def test_patch_cannot_change_signature_or_aggregation(self):
        context = {'targets':['parse_line']}
        for patch in (GOOD_FUNCTION.replace('(line, system)', '(system, line)'),
                      GOOD_FUNCTION+'\ntask_result({"count":999})',
                      GOOD_FUNCTION+'\ndef other():\n return 1',
                      'import json as re\n'+GOOD_FUNCTION):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                merge_repair(BAD, patch, context)
        self.assertEqual(ast.dump(ast.parse(merge_repair(BAD, BAD, context))), ast.dump(ast.parse(BAD)))

    def test_literal_regex_validation_does_not_execute_or_rewrite(self):
        with self.assertRaisesRegex(ValueError, '正则表达式无效'):
            validate_literal_regex("import re\ndef parse_line(line):\n return re.match(r'[z-a]', line)")
        validate_literal_regex("import re\nraise RuntimeError('must not execute')\nre.match(dynamic(), line)")
        validate_literal_regex(r"import re; re.match(r'\\d+', line)")  # Valid literal backslash is not auto-fixed.
        validate_literal_regex("import re\nre.compile('a # [', flags=re.X)")
