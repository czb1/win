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


    def test_deadline_does_not_request_unverified_answer(self):
        mem=Memory(task_text='query',task_started=1,task_timeout=15,bootstrap_done=True,
                   contract={'input_kind':'logs','example':{'count':0}},input_blocked=True)
        prompt,code,ledger=step(mem,14)
        self.assertFalse(prompt);self.assertFalse(code);self.assertFalse(ledger.commands)
