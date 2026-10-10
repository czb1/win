"""Generic diagnostic and prompt regressions; no match-specific field rules."""
import json
from pathlib import Path
import tempfile
import unittest

import test_weak_model_evolution as weak
from test_evolution_v2 import step, v2_result
from agent.intelligence import Memory
from agent.task_transform_feedback import diagnosis, metadata, history_methods, prompt, observe, dominates, repair_direction
from agent.task_skills import learned_method


class TransformFeedbackTests(unittest.TestCase):
    fixture = weak.WeakModelTests.fixture
    execute = weak.WeakModelTests.execute

    def test_order_only_requires_identical_content_and_multiplicity(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg, *_ = self.fixture(root)
            records = [{'serial':'x', 'weight':2}, {'serial':'y', 'weight':1}]
            cases = [{'input':records, 'expected':list(reversed(records))}]
            (root/'examples.json').write_text(json.dumps(cases))
            _, body, _ = self.execute('def transform(records): return records', cfg)
            self.assertEqual(diagnosis(body)['failures'][0]['classification'], 'order_only')
            _, body, _ = self.execute("def transform(records): return [dict(r, weight=9) for r in records]", cfg)
            self.assertEqual(diagnosis(body)['failures'][0]['classification'], 'value_or_structure')
            _, body, _ = self.execute('def transform(records): return records[:1]', cfg)
            detail = diagnosis(body)['failures'][0]
            self.assertEqual(detail['classification'], 'record_membership_or_mixed')
            self.assertEqual(detail['record_evidence']['missing_count'], 1)
            # Duplicates cannot establish a unique correspondence.
            records = [{'serial':'x', 'weight':1}, {'serial':'x', 'weight':1}]
            (root/'examples.json').write_text(json.dumps([{'input':records, 'expected':records}]))
            _, body, _ = self.execute('def transform(records): return records[:1]', cfg)
            detail = diagnosis(body)['failures'][0]
            self.assertNotEqual(detail['classification'], 'order_only')
            self.assertIn('unavailable', detail['record_evidence']['alignment'])

    def test_full_exception_and_large_counterexample_are_readable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg, *_ = self.fixture(root)
            _, body, answer = self.execute("def transform(records): raise ValueError('specific root cause')", cfg)
            detail = diagnosis(body)
            self.assertIsNone(answer)
            self.assertEqual(detail['classification'], 'code_exception')
            self.assertIn('ValueError: specific root cause', detail['traceback'])
            self.assertFalse((root/'checked').exists())
            records = ['a'*9000]
            (root/'examples.json').write_text(json.dumps([{'input':records, 'expected':records}]))
            _, body, _ = self.execute('def transform(records): return []', cfg)
            detail = diagnosis(body)
            complete = json.loads(Path(detail['evidence_path']).read_text())
            self.assertEqual(complete['failures'][0]['input'], records)
            self.assertIn('READ ', detail['read_command'])
            self.assertNotIn('input', detail['failures'][0])

    def test_history_requires_observed_schema_and_completion_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, contract, docs, preview = self.fixture(Path(d))
            current = metadata(preview, contract)
            def method(info):
                result = learned_method((1,1), contract, transform='def transform(records): return records', transform_metadata=info)
                result['rounds'] = 4
                return result
            old = method({})
            wrong = method({**current, 'input_schema': {'array':['str']}})
            adapted = method({**current, 'output_schema': {'array':['int']}})
            exact = method(current)
            result = history_methods([exact, adapted, old, wrong], current)
            self.assertEqual([r['compatibility'] for r in result], ['observed_schema_match', 'output_adaptation_required'])
            self.assertEqual(history_methods([exact], {**current, 'input_schema':None}), [])

    def test_retry_prompt_separates_rejected_code_and_deduplicates_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg, contract, docs, preview = self.fixture(root)
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True, pending=('task',2),
                         contract=contract, documents=docs, inputs=preview)
            code = "def transform(records): return [r['key'] for r in records]"
            _, cmd, _ = step(mem,3,llmResp='PYTHON\n'+code)
            raw = v2_result({'executeCmd':cmd}, '/')
            repair, _, _ = step(mem,4,lastCmdResult=raw)
            context = json.loads(repair.split('上下文：')[1])
            self.assertIsInstance(context['lastAttempt']['diagnosis'], dict)
            self.assertNotIn('sandbox', context['lastAttempt'])
            self.assertNotIn('runtimeError', context['lastAttempt'])
            samples = next(f for f in context['inputPreview']['files'] if f.get('case_count'))
            self.assertNotIn('sample', samples)
            self.assertNotIn(0, [c['case'] for c in samples['case_samples']])
            mem.pending = ('task',4)
            retry, cmd, _ = step(mem,5,llmResp='PYTHON\n'+code)
            self.assertFalse(cmd)
            context = json.loads(retry.split('上下文：')[1])
            self.assertFalse(context['rejectedCandidate']['executed'])
            self.assertEqual(context['lastAttempt']['diagnosis']['stage'], 'samples')
            # A supplementary read must not erase the last executed transform.
            mem.last_attempt = {'tool':'read', 'status':'ok'}
            context = json.loads(prompt(mem, 8).split('上下文：')[1])
            self.assertEqual(context['lastAttempt']['diagnosis']['stage'], 'samples')


    def test_result_fingerprint_and_conservative_candidate_selection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg, *_ = self.fixture(root)
            records = [{'serial': 'x', 'rank': 8, 'quantity': 2},
                       {'serial': 'y', 'rank': 8, 'quantity': 7},
                       {'serial': 'z', 'rank': 4, 'quantity': 9}]
            (root/'examples.json').write_text(json.dumps([
                {'input': records, 'expected': records[1:]}]))
            mem = Memory()
            def attempt(code):
                _, body, _ = self.execute(code, cfg)
                item = {'python': code, 'diagnosis': diagnosis(body)}
                observe(mem, item)
                return item
            first = attempt('def transform(records): return records')
            second = attempt('def transform(records): return records[:1]')
            self.assertTrue(dominates(first['diagnosis'], second['diagnosis']))
            self.assertEqual(mem.transform_progress['comparison'], 'regressed')
            self.assertEqual(mem.transform_best['python'], first['python'])
            attempt('def transform(records): return list(records[:1])')
            self.assertTrue(mem.transform_progress['same_sample_outputs'])
            context = json.loads(prompt(mem, 8).split('上下文：')[1])
            # Wiring stores the last attempt; direct helper invocation does not.
            mem.transform_attempt = second
            context = json.loads(prompt(mem, 8).split('上下文：')[1])
            self.assertEqual(context['repairBase']['python'], first['python'])
            self.assertNotIn('python', context['lastAttempt'])
            self.assertNotIn('failures', context['lastAttempt']['diagnosis'])
            evidence = first['diagnosis']['failures'][0]['record_evidence']
            self.assertEqual(evidence['contrasts'][0]['contrast']['serial'], 'y')
            # Same number of failures is not enough; trade-offs remain incomparable.
            a = {'sample_fingerprint':'s', 'quality':[[1, 0, 1, 0, 0]]}
            b = {'sample_fingerprint':'s', 'quality':[[1, 1, 0, 0, 0]]}
            self.assertFalse(dominates(a,b))
            self.assertFalse(dominates(b,a))
            self.assertFalse(dominates(a, {**b, 'sample_fingerprint':'other'}))

    def test_feedback_has_one_direction_and_no_duplicate_candidate(self):
        mem = Memory()
        mem.transform_attempt = {'python':'def transform(records): return records',
                                 'diagnosis': {'stage':'samples', 'failures':[]}}
        mem.rejected_transform = {'python':mem.transform_attempt['python'],
                                  'reason':'failed_ast', 'executed':False}
        mem.history.append({'error':'同类运行时失败；缩小为一个诊断步骤'})
        text = prompt(mem, 3)
        self.assertNotIn('同类运行时失败', text)
        self.assertNotIn('缩小为一个诊断步骤', text)
        self.assertEqual(text.count('def transform(records)'), 1)
        self.assertIn('临近截止', text)
        self.assertIn('traceback', repair_direction({'stage':'execution'}, 8))
        self.assertIn('正式checker', repair_direction({'stage':'checker'}, 8))


if __name__ == '__main__':
    unittest.main()
