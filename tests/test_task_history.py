import unittest
from agent.task_history import remember, references
from agent.intelligence import Memory
from agent.task_transform_feedback import prompt
from test_evolution_v2 import step


class TaskHistoryTests(unittest.TestCase):
    def save(self, archive, path='/work/task_a.md', title='任务 Z-27', body='按当前规则排序', start=0):
        remember(archive, {'kind': 'discover', 'path': path, 'start': start,
                          'output': f'DOCUMENT {path} OFFSET {start}\n# {title}\n{body}'})

    def current(self, text='沿用 Z-27 的排序，改变输出格式'):
        return [{'kind': 'discover', 'resolved_path': '/work/current.md', 'output': text}]

    def test_identifier_reference_survives_output_change_and_repair(self):
        mem = Memory(documents=self.current())
        self.save(mem.task_archive)
        refs, missing = references(mem.documents, mem.task_archive)
        self.assertFalse(missing)
        self.assertEqual(refs[0]['source'], '/work/task_a.md')
        self.assertTrue(refs[0]['complete'])
        for attempt in ({}, {'tool': 'transform', 'status': 'failed', 'python': 'def transform(records): return []'}):
            mem.transform_attempt = attempt
            text = prompt(mem, 8)
            self.assertIn('按当前规则排序', text)
            self.assertEqual(text.count('按当前规则排序'), 1)
            self.assertIn('必须通过本题官方checker', text)

    def test_no_reference_no_attachment_and_no_prefix_match(self):
        archive = {}
        self.save(archive)
        for text in ('新的独立题目', '引用 Z-270'):
            self.assertEqual(references(self.current(text), archive), ([], []))

    def test_ambiguous_identifier_never_selects_arbitrarily(self):
        archive = {}
        self.save(archive, '/one/a.md')
        self.save(archive, '/two/a.md')
        refs, _ = references(self.current(), archive)
        self.assertEqual(refs[0]['status'], 'ambiguous')
        self.assertNotIn('text', refs[0])

    def test_current_auxiliary_files_are_not_historical_references(self):
        archive = {}
        remember(archive, {'kind': 'read', 'path': '/work/API_DOCS.md',
                          'output': 'DOCUMENT /work/API_DOCS.md OFFSET 0\n# API documentation\nCurrent credentials'})
        self.assertEqual(references(self.current('请读取 API_DOCS.md 和 spec.md'), archive), ([], []))

    def test_explicit_missing_path_and_partial_pages(self):
        archive = {}
        refs, missing = references(self.current('引用 `task_a.md`'), archive)
        self.assertEqual(missing, ['/work/task_a.md'])
        self.save(archive, body="page one\nNEXT_READ '/work/task_a.md' 6000")
        refs, _ = references(self.current(), archive)
        self.assertFalse(refs[0]['complete'])
        self.assertIn('6000', refs[0]['next_read'])
        self.save(archive, body='page two', start=6000)
        refs, _ = references(self.current(), archive)
        self.assertTrue(refs[0]['complete'])
        self.assertIn('page two', refs[0]['text'])

    def test_long_sources_have_explicit_continuation(self):
        archive = {}
        self.save(archive, body='x' * 14000)
        refs, _ = references(self.current(), archive)
        self.assertFalse(refs[0]['complete'])
        self.assertEqual(len(refs[0]['text']), 12000)
        self.assertTrue(refs[0]['next_read'].endswith('12000'))

    def test_history_read_does_not_replace_current_contract_or_inputs(self):
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                     pending=('cmd', 1), running_tool={'kind': 'reference', 'path': '/work/old.md'},
                     documents=self.current(), contract={'example': {'count': 0}}, inputs={'directory': '/current'})
        raw = '[exitCode:0]\nDOCUMENT /work/old.md OFFSET 0\n# Old\nOld requirements\nTASK_INPUTS {"directory":"/old"}'
        step(mem, 2, lastCmdResult=raw)
        self.assertEqual(mem.inputs['directory'], '/current')
        self.assertEqual(mem.contract, {'example': {'count': 0}})
        self.assertIn('/work/old.md', mem.task_archive)
        self.assertEqual(mem.documents, self.current())
