"""Real log wording and independent offline checker fixtures for task rewards."""
import hashlib
import json
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest

from test_agent import setup_case, unit
from test_evolution import task_payload
from agent.brain import Agent
from agent.config import Config
from agent.economy import pioneer
from agent.intelligence import Intelligence, Memory, parse_task_reply
from agent.model import Turn
from agent.task_sop import answer_contract, answer_error, engineering_code
from agent.task_tools import document_code, document_path


def deployment(directory, name, port=8080):
    task_dir = Path(directory, 'selfEvolutionTask', '1-fixed-step', '2-engineering-fix')
    workspace = task_dir / ('ws_' + name)
    (workspace / 'config').mkdir(parents=True)
    (workspace / 'bin').mkdir()
    (workspace / 'bin/start.sh').write_text('#!/bin/sh\nexit 0\n')
    (workspace / 'bin/start.sh').chmod(0o600)
    (workspace / 'config' / f'{name}.conf').write_text('keep1\nkeep2\nport 0\nkeep4\nkeep5\nname wrong\n')
    (workspace / 'spec.md').write_text(f'''# 应用 {name} 部署规范

## 目录要求
- logs/{name}/ 必须存在，权限为 755

## 配置文件 config/{name}.conf
- 第 3 行：`port {port}`
- 第 6 行：`name {name}-app`

## 脚本要求
- bin/start.sh 必须存在且可执行（权限 755）
''', encoding='utf-8')
    token = hashlib.sha256(f'{name}:{port}'.encode()).hexdigest()[:24]
    # This independent checker validates outcomes; it does not call SOP code.
    checker = f'''from pathlib import Path
import stat
directory = Path('logs/{name}')
lines = Path('config/{name}.conf').read_text().splitlines()
ok = (directory.is_dir() and stat.S_IMODE(directory.stat().st_mode) == 0o755
      and lines == ['keep1', 'keep2', 'port {port}', 'keep4', 'keep5', 'name {name}-app']
      and stat.S_IMODE(Path('bin/start.sh').stat().st_mode) == 0o755)
if not ok:
    print('deployment requirements not satisfied')
    raise SystemExit(1)
print('all checks passed')
print('TOKEN: {token}')
'''
    (workspace / 'check').write_text(f'#!{sys.executable}\n' + checker)
    (workspace / 'check').chmod(0o755)
    document = task_dir / f'task_1_{name}.md'
    document.write_text(f'''# 自进化任务 B-1：修复应用 {name} 部署
应用工作区由组织方准备，请按 spec.md 修复文件系统。
## 任务要求
1. 进入工作区：`cd {workspace}/`
2. 阅读 `spec.md`
3. 修复文件系统中的所有问题
4. 运行 `./check`
5. 全部通过输出 `TOKEN: xxx`
## 提交规则
任务完成以 `./check` 输出 `TOKEN: xxx` 为准，通过 submitAnswer 提交：
```
{{"token": "xxx"}}
```
## 提示
你可以反复运行 `./check` 查看进度。
''', encoding='utf-8')
    return workspace, document, token


def run_sandbox(response, directory):
    # Only repository-authored deterministic tools or fixed test code runs here.
    args = shlex.split(response['executeCmd'])
    assert args[:2] == ['python3', '-c']
    code = args[2].replace("root = '/tmp/selfEvolutionTask'",
                           f"root = {str(Path(directory, 'selfEvolutionTask'))!r}")
    result = subprocess.run([sys.executable, '-c', code], cwd=directory,
                            capture_output=True, text=True, timeout=12)
    return f'[exitCode:{result.returncode}]\n{result.stdout}{result.stderr}'


class TaskCompletionTests(unittest.TestCase):
    def test_log_protocol_marker_is_rejected_before_sandbox(self):
        for code in ("print('query')\nPYTHON", "if True:\n    PYTHON"):
            p = task_payload(12, "query")
            p['llmResp'] = 'PYTHON\n' + code
            mem = Memory(task_text='query', task_started=10, pending=('task', 11))
            t, cfg, _, _ = setup_case(p)
            mem.observe(t, cfg)
            self.assertIsNone(mem.python)
            self.assertIn('残留协议标记', mem.history[-1]['error'])

    def test_judger_rejection_blocks_reordered_json_but_allows_correction(self):
        p = task_payload(19, 'query')
        p['errors'] = [{'errorCode': 2, 'description': 'world_heritage_count: 数值不符'}]
        mem = Memory(task_text='query', task_started=10, bootstrap_done=True,
                     submitted=(18, '{"city":"北京","world_heritage_count":0}'))
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        mem.answer = '{ "world_heritage_count": 0, "city": "北京" }'
        prompt, cmd = Intelligence(t, cfg, mem).task(ledger)
        self.assertIsNone(mem.answer)
        self.assertIsNone(mem.submitted)
        self.assertIn('禁止原样重交', prompt)
        mem.pending = None
        mem.answer = '{"city":"北京","world_heritage_count":7}'
        Intelligence(t, cfg, mem).task(ledger)
        self.assertIsNotNone(mem.submitted)
        self.assertEqual(mem.submitted[0], 19)
        self.assertIn(':7', mem.submitted[1])

    def test_query_discovery_survives_later_output_and_resets_for_new_task(self):
        mem = Memory(task_text='query', task_started=10, bootstrap_done=True)
        for round_no, output in ((13, '404 /docs'), (15, 'API_SCHEMA_FROM_QUERY'), (17, 'DATA')):
            p = task_payload(round_no, 'query')
            p['lastCmdResult'] = '[exitCode:0]\n' + output
            mem.pending = ('cmd', round_no - 1)
            mem.running_python = "print('fixture')"
            t, cfg, _, ledger = setup_case(p)
            mem.observe(t, cfg)
        prompt, _ = Intelligence(t, cfg, mem).task(ledger)
        self.assertIn('API_SCHEMA_FROM_QUERY', prompt)
        self.assertIn('404 /docs', prompt)
        self.assertIn('不等于空数据', prompt)
        p = task_payload(18, 'new task')
        mem.observe(Turn(p, cfg), cfg)
        self.assertFalse(mem.exploration)
        self.assertFalse(mem.rejected_answers)

    def test_sandbox_final_cannot_resubmit_rejected_answer(self):
        from agent.intelligence import answer_identity
        p = task_payload(22, 'query')
        p['lastCmdResult'] = '[exitCode:0]\nFINAL_ANSWER\n{"count":0}'
        mem = Memory(task_text='query', task_started=10, bootstrap_done=True,
                     pending=('cmd', 21), running_python='print("FINAL_ANSWER")',
                     rejected_answers={answer_identity('{"count":0}')})
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        self.assertIsNotNone(mem.answer)
        Intelligence(t, cfg, mem).task(ledger)
        self.assertIsNone(mem.submitted)
        self.assertIsNone(mem.answer)

    def test_completed_solution_keeps_exploration_for_next_task(self):
        p = task_payload(4)
        p['lastRoundRoleActionResults'] = {'11': True}
        mem = Memory(task_text='query Beijing', task_point=(6, 5), task_started=1,
                     submitted=(3, '42'), submitted_python='print(42)')
        mem.exploration.append({'python': 'read_schema()', 'sandbox': 'API_SCHEMA', 'status': 'ok'})
        mem.observe(Turn(p, Config()), Config())
        self.assertFalse(mem.exploration)
        self.assertEqual(mem.skills[0]['exploration'][0]['sandbox'], 'API_SCHEMA')
        p = task_payload(40, 'query Shanghai')
        mem.task_text = p['phaseTask']
        mem.task_point = (6, 5)
        mem.task_started = 40
        mem.bootstrap_done = True
        t, cfg, _, ledger = setup_case(p)
        prompt, _ = Intelligence(t, cfg, mem).task(ledger)
        self.assertIn('API_SCHEMA', prompt)
        self.assertIn('必须重新查询', prompt)

    def test_actual_log_question_triggers_bootstrap_without_llm(self):
        for question in ('请阅读task_1_alpha.md，获取任务信息', '请阅读task_1_beijing.md，获取任务信息',
                         '文件：task_1_alpha.md', '阅读：`/tmp/current/spec.md`', '请阅读./task_1_alpha.md'):
            with self.subTest(question=question):
                path = document_path(question)
                self.assertIsNotNone(path)
                response = Agent(Config(layout_mode='explicit')).decide(task_payload(2, question))
                self.assertFalse(response['prompt'])
                self.assertEqual(shlex.split(response['executeCmd'])[-1], document_code(path))
        self.assertIsNone(document_path('请阅读https://example.com/spec.md'))
        self.assertIsNone(document_path('请阅读../old/spec.md'))
        self.assertEqual(parse_task_reply('读取文件：READ task_1_alpha.md'),
                         {'read': 'task_1_alpha.md', 'start': 0})

    def test_model_relative_read_uses_current_document_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory, 'current')
            current.mkdir()
            (current / 'values.txt').write_text('CURRENT DATA')
            Path(directory, 'values.txt').write_text('STALE CWD DATA')
            p = task_payload(3, 'query')
            p['llmResp'] = 'READ values.txt'
            mem = Memory(task_text='query', task_started=1, bootstrap_done=True,
                         pending=('task', 2), documents=[{'resolved_path': str(current / 'api.md')}])
            t, cfg, _, ledger = setup_case(p)
            mem.observe(t, cfg)
            _, cmd = Intelligence(t, cfg, mem).task(ledger)
            result = run_sandbox({'executeCmd': cmd}, directory)
            self.assertIn('CURRENT DATA', result)
            self.assertNotIn('STALE CWD DATA', result)

    def test_relative_search_rejects_ambiguous_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('one', 'two'):
                folder = Path(directory, 'selfEvolutionTask', name)
                folder.mkdir(parents=True)
                (folder / 'spec.md').write_text(name)
            result = run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(document_code('spec.md'))}, directory)
            self.assertTrue(result.startswith('[exitCode:1]'))
            self.assertIn('found 2', result)

    def test_search_does_not_claim_uniqueness_after_skipping_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory, 'selfEvolutionTask')
            root.mkdir()
            (root / 'spec.md').write_text('possibly stale')
            for i in range(41):
                (root / str(i)).mkdir()
            result = run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(document_code('spec.md'))}, directory)
            self.assertTrue(result.startswith('[exitCode:1]'))
            self.assertIn('search limit reached', result)

    def complete_deployment(self, agent, directory, name, begin, port=8080):
        workspace, document, token = deployment(directory, name, port)
        p = task_payload(begin)
        p['teamOur']['playerTasks'][0]['timeoutRounds'] = 15
        self.assertEqual(agent.decide(p)['roleCommandMap']['11']['action'], 'acceptTask')
        p.update(roundNo=begin + 1, phaseTask=f'请阅读{document.name}，获取任务信息')
        response = agent.decide(p)
        self.assertFalse(response['prompt'])
        p.update(roundNo=begin + 2, lastCmdResult=run_sandbox(response, directory))
        response = agent.decide(p)
        self.assertFalse(response['prompt'])
        self.assertNotIn('11', response['roleCommandMap'])
        self.assertIn(str(workspace), response['executeCmd'])
        p.update(roundNo=begin + 3, lastCmdResult=run_sandbox(response, directory))
        response = agent.decide(p)
        self.assertFalse(response['prompt'])
        self.assertFalse(response['executeCmd'])
        self.assertEqual(json.loads(response['roleCommandMap']['11']['taskAnswer']), {'token': token})
        self.assertEqual(stat.S_IMODE((workspace / 'bin/start.sh').stat().st_mode), 0o755)
        p.update(roundNo=begin + 4, phaseTask='', lastCmdResult='', lastRoundRoleActionResults={'11': True})
        p['teamOur']['playerTasks'][0].update(isValid=False, coldDownRounds=30)
        p['teamOur']['goldNum'] += 10
        p['teamOur']['totalScore'] = 25
        with self.assertLogs('agent.intelligence', 'INFO') as captured:
            agent.decide(p)
        self.assertIn('task_outcome=', '\n'.join(captured.output))
        return workspace

    def test_accept_repair_check_submit_in_four_rounds_without_llm_and_rebind(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = Agent(Config(layout_mode='explicit'))
            first = self.complete_deployment(agent, directory, 'alpha', 1)
            self.complete_deployment(agent, directory, 'gamma', 36, port=9091)
            mem = next(iter(agent.sessions.values()))
            self.assertEqual(len(mem.skills), 2)
            self.assertEqual(mem.skills[-1]['workflow'], 'check_token')
            self.assertNotIn(str(first), mem.skills[-1]['python'])
            self.assertFalse(mem.skills[-1]['verified'])
            for outcome in mem.task_outcomes:
                self.assertTrue(outcome['completionObserved'])
                self.assertEqual(outcome['llmCalls'], 0)
                self.assertEqual(outcome['sandboxCalls'], 2)
                self.assertEqual(outcome['submissions'], 1)
                self.assertEqual(outcome['teamGoldDelta'], 10)

    def test_unsupported_spec_is_not_partially_repaired_or_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, _, _ = deployment(directory, 'alpha')
            with (workspace / 'spec.md').open('a') as target:
                target.write('- config/alpha.conf 中启用未知开关\n')
            result = run_sandbox({'executeCmd': 'python3 -c ' + shlex.quote(engineering_code(str(workspace)))}, directory)
            self.assertTrue(result.startswith('[exitCode:1]'))
            self.assertIn('unsupported spec requirement', result)
            self.assertFalse((workspace / 'logs').exists())
            self.assertIn('port 0', (workspace / 'config/alpha.conf').read_text())

    def test_checker_failure_duplicate_or_excessive_output_never_yields_token(self):
        for checker in ("print('TOKEN: fake'); raise SystemExit(1)",
                        "print('TOKEN: one\\nTOKEN: two')", "print('TOKEN: fake\\n' + 'x' * 16001)",
                        "print('TOKEN:\\nfake')"):
            with self.subTest(checker=checker), tempfile.TemporaryDirectory() as directory:
                workspace, _, _ = deployment(directory, 'alpha')
                (workspace / 'check').write_text(f'#!{sys.executable}\n' + checker)
                response = {'executeCmd': 'python3 -c ' + shlex.quote(engineering_code(str(workspace)))}
                result = run_sandbox(response, directory)
                self.assertTrue(result.startswith('[exitCode:1]'))
                self.assertNotIn('\nFINAL_ANSWER\n', result)

    def test_model_token_and_printed_token_require_independent_check(self):
        for purpose, reply in (('task', 'ANSWER\n{"token":"invented"}'),
                               ('cmd', '[exitCode:0]\nFINAL_ANSWER\n{"token":"invented"}')):
            with self.subTest(purpose=purpose):
                p = task_payload(4, 'query')
                p['llmResp' if purpose == 'task' else 'lastCmdResult'] = reply
                mem = Memory(task_text='query', task_started=1, bootstrap_done=True, sop_attempted=True,
                             pending=(purpose, 3), contract={'kind': 'check_token', 'workspace': '/tmp/current/ws',
                                                           'example': {'token': 'xxx'}})
                t, cfg, _, ledger = setup_case(p)
                mem.observe(t, cfg)
                self.assertIsNone(mem.answer)
                _, cmd = Intelligence(t, cfg, mem).task(ledger)
                self.assertIn('REPAIR = False', shlex.split(cmd)[2])
                self.assertNotIn('11', ledger.commands)

    def test_submission_shape_rejects_document_or_extra_keys_and_allows_partial_credit(self):
        docs = [{'kind': 'read', 'path': 'task.md', 'output': '''## API 返回示例
```json
{"data": [1,2]}
```
## 提交格式
```json
{"city": "北京", "temperature": 21}
```
'''}]
        contract = answer_contract(docs)
        self.assertEqual(contract['example'], {'city': '北京', 'temperature': 21})
        for answer in ('# 任务规范正文', '{"task":"wrong wrapper"}', '{"city": 123}',
                       '{"city":"北京","temperature":true}', '{}'):
            self.assertTrue(answer_error(answer, contract), answer)
        self.assertFalse(answer_error('{"city":"北京"}', contract))
        self.assertFalse(answer_error('{"city":"北京","temperature":22.5}', contract))

    def test_api_output_format_is_not_the_task_submission_contract(self):
        docs = [{'kind': 'read', 'path': 'api.md', 'output':
                 '# API 文档\n## 输出格式\n```json\n{"data": [1, 2], "status": "ok"}\n```\n'}]
        self.assertFalse(answer_contract(docs))

    def test_partial_answer_is_submitted_immediately_then_refined(self):
        agent = Agent(Config(layout_mode='explicit'))
        p = task_payload(1, '请阅读weather.md，获取任务信息')
        agent.decide(p)
        p.update(roundNo=2, lastCmdResult='[exitCode:0]\nRESOLVED_DOCUMENT /tmp/current/weather.md OFFSET 0\n'
                 '查询北京温度。\n## 提交格式\n```json\n{"city":"北京","temperature":21}\n```\n')
        self.assertIn('最高通过率', agent.decide(p)['prompt'])
        p.update(roundNo=3, lastCmdResult='', llmResp='ANSWER\n{"city":"北京"}')
        answer = agent.decide(p)['roleCommandMap']['11']['taskAnswer']
        self.assertEqual(json.loads(answer), {'city': '北京'})
        p.update(roundNo=4, llmResp='', lastRoundRoleActionResults={'11': True},
                 errors=[{'errorCode': 2, 'description': '$/temperature: 缺少键'}])
        self.assertIn('$/temperature: 缺少键', agent.decide(p)['prompt'])
        p.update(roundNo=5, errors=[], llmResp='ANSWER\n{"city":"北京","temperature":23}')
        answer = agent.decide(p)['roleCommandMap']['11']['taskAnswer']
        self.assertEqual(json.loads(answer), {'city': '北京', 'temperature': 23})

    def test_unknown_spec_can_recover_with_model_repair_and_fixed_checker(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace, document, token = deployment(directory, 'alpha')
            with (workspace / 'spec.md').open('a') as target:
                target.write('- release.txt 内容必须为 ready\n')
            checker = (workspace / 'check').read_text().replace(
                'if not ok:', "ok = ok and Path('release.txt').is_file() and Path('release.txt').read_text() == 'ready'\nif not ok:")
            (workspace / 'check').write_text(checker)
            agent = Agent(Config(layout_mode='explicit'))
            p = task_payload(1, f'请阅读{document.name}，获取任务信息')
            response = agent.decide(p)
            p.update(roundNo=2, lastCmdResult=run_sandbox(response, directory))
            response = agent.decide(p)
            p.update(roundNo=3, lastCmdResult=run_sandbox(response, directory))
            prompt = agent.decide(p)['prompt']
            self.assertIn('release.txt', prompt)
            self.assertIn('提交规则', prompt)
            # A fixed authored stand-in for an LLM repair; never live model code.
            repair = f'''from pathlib import Path
base = Path({str(workspace)!r})
folder = base / 'logs/alpha'
folder.mkdir(parents=True)
folder.chmod(0o755)
(base / 'config/alpha.conf').write_text('keep1\\nkeep2\\nport 8080\\nkeep4\\nkeep5\\nname alpha-app\\n')
(base / 'bin/start.sh').chmod(0o755)
(base / 'release.txt').write_text('ready')
print('repair done')
'''
            p.update(roundNo=4, lastCmdResult='', llmResp='PYTHON\n' + repair)
            response = agent.decide(p)
            p.update(roundNo=5, llmResp='', lastCmdResult=run_sandbox(response, directory))
            response = agent.decide(p)
            self.assertIn('REPAIR = False', response['executeCmd'])
            self.assertNotIn('11', response['roleCommandMap'])
            p.update(roundNo=6, lastCmdResult=run_sandbox(response, directory))
            answer = agent.decide(p)['roleCommandMap']['11']['taskAnswer']
            self.assertEqual(json.loads(answer), {'token': token})

    def test_observed_fast_sop_changes_task_ranking_without_shortening_timeout(self):
        p = task_payload()
        p['teamOur']['playerTasks'][0]['timeoutRounds'] = 100
        p['teamOur']['playerTasks'].append({**p['teamOur']['playerTasks'][0],
            'taskPosition': {'x': 5, 'y': 6}, 'timeoutRounds': 10})
        for learned, expected in ((False, (5, 6)), (True, (6, 5))):
            with self.subTest(learned=learned):
                t, cfg, nav, ledger = setup_case(p)
                mem = Memory(skills=[{'point': (6, 5), 'workflow': 'check_token', 'rounds': 4}] if learned else [])
                pioneer(t, cfg, mem, nav, ledger, t.pioneer)
                self.assertEqual(mem.task_point, expected)
                self.assertEqual(mem.task_timeout, 100 if learned else 10)

    def test_contract_survives_document_page_eviction(self):
        p = task_payload(3, 'query')
        p['lastCmdResult'] = '[exitCode:0]\nDOCUMENT /tmp/current/page4.md OFFSET 0\nmore data'
        mem = Memory(task_text='query', task_started=1, pending=('cmd', 2),
                     contract={'example': {'city': '北京'}, 'source': 'task.md'},
                     running_tool={'kind': 'read', 'path': '/tmp/current/page4.md', 'start': 0})
        mem.observe(Turn(p, Config()), Config())
        self.assertEqual(mem.contract['example'], {'city': '北京'})

    def test_checker_workspace_must_be_explicit_and_within_current_task(self):
        with tempfile.TemporaryDirectory() as directory:
            _, document, _ = deployment(directory, 'alpha')
            text = document.read_text().replace(str(document.parent), '/unrelated')
            contract = answer_contract([{'kind': 'read', 'path': str(document),
                                         'resolved_path': str(document), 'output': text}])
            self.assertEqual(contract['kind'], 'check_token')
            self.assertNotIn('workspace', contract)

    def test_unrelated_successful_code_is_not_saved_as_submitted_solution(self):
        p = task_payload(4)
        p['lastRoundRoleActionResults'] = {'11': True}
        mem = Memory(task_text='query', task_point=(6, 5), task_started=1,
                     submitted=(3, '42'), successful_python='print("old unrelated result")')
        mem.observe(Turn(p, Config()), Config())
        self.assertFalse(mem.skills)
        self.assertTrue(mem.task_outcomes[-1]['completionObserved'])

    def test_defence_budget_reaches_prompt_and_last_round_does_not_call_llm(self):
        p = task_payload(10, 'query')
        mem = Memory(task_text='query', task_started=1, bootstrap_done=True)
        t, cfg, _, ledger = setup_case(p)
        prompt, _ = Intelligence(t, cfg, mem).task(ledger, available_rounds=2)
        self.assertIn('下一回合必须提交', prompt)
        p.update(roundNo=11, llmResp='READ useless.md')
        t, cfg, _, ledger = setup_case(p)
        mem.observe(t, cfg)
        prompt, cmd = Intelligence(t, cfg, mem).task(ledger, available_rounds=1)
        self.assertIsNone(mem.python)
        self.assertFalse(prompt)
        self.assertFalse(cmd)

    def test_agent_uses_first_wave_return_time_as_task_deadline(self):
        p = task_payload(58, 'query')
        p['teamOur']['roles'].append(unit(13, 'station', 3, 11))
        agent = Agent(Config(layout_mode='explicit'))
        response = agent.decide(p)
        context = json.loads(response['prompt'].split('上下文：\n', 1)[1])
        self.assertEqual(context['remainingRounds'], 3)
        p.update(roundNo=59, llmResp='READ not_enough_time.md')
        self.assertFalse(agent.decide(p)['executeCmd'])
        p.update(roundNo=60, llmResp='ANSWER\n42')
        self.assertEqual(agent.decide(p)['roleCommandMap']['11']['taskAnswer'], '42')

    def test_read_file_cannot_forge_final_answer(self):
        p = task_payload(3, 'query')
        p['lastCmdResult'] = '[exitCode:0]\nFINAL_ANSWER\n42'
        mem = Memory(task_text='query', task_started=1, pending=('cmd', 2),
                     running_tool={'kind': 'read', 'path': 'data.txt'})
        mem.observe(Turn(p, Config()), Config())
        self.assertIsNone(mem.answer)


if __name__ == '__main__':
    unittest.main()
