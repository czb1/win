"""Evidence-only transform feedback; never infer business rules or field renames."""
import json


def diagnosis(raw):
    for line in str(raw).splitlines():
        if line.startswith('TRANSFORM_DIAGNOSTIC '):
            try:
                value = json.loads(line.partition(' ')[2])
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
    return None


def schema(value):
    if isinstance(value, dict):
        return {'object': {k: schema(v) for k, v in sorted(value.items())}}
    if isinstance(value, list):
        variants = {json.dumps(schema(v), sort_keys=True) for v in value}
        return {'array': [json.loads(v) for v in sorted(variants)]}
    return type(value).__name__


def metadata(inputs, contract):
    records, outputs = [], []
    for entry in inputs.get('files', []):
        for case in entry.get('case_samples', []):
            if isinstance(case.get('input'), list):
                records.extend(case['input'])
            outputs.append(case.get('expected', case.get('output')))
        if entry.get('type') == 'list' and not entry.get('case_count'):
            try:
                preview = json.loads(entry.get('sample', ''))
                if isinstance(preview, list):
                    records.extend(preview)
            except (ValueError, TypeError):
                pass
    return {'source': contract.get('family'), 'input_schema': schema(records) if records else None,
            'output_schema': schema(outputs) if outputs else None,
            'schema_evidence': 'observed_preview_only'}


def history_methods(skills, current):
    candidates = []
    for skill in skills:
        previous = skill.get('transform_metadata', {})
        if (not skill.get('transform') or skill.get('disabled') or 'rounds' not in skill
                or skill.get('evidence') != 'legal_submission_then_task_disappeared'
                or previous.get('source') != current.get('source')
                or not current.get('input_schema')
                or previous.get('input_schema') != current['input_schema']):
            continue
        exact = previous.get('output_schema') == current.get('output_schema')
        candidates.append((exact, skill.get('successes', 0), {
            'id': skill.get('id'), 'source': previous['source'],
            'compatibility': 'observed_schema_match' if exact else 'output_adaptation_required',
            'evidence': skill.get('evidence'), 'metadata': previous, 'python': skill['transform']}))
    candidates.sort(key=lambda item: item[:2], reverse=True)
    return [item[2] for item in candidates[:2]]


def prompt(mem, remaining):
    attempt = mem.transform_attempt or (mem.last_attempt if mem.last_attempt.get('tool') == 'transform' else {})
    detail = attempt.get('diagnosis')
    context = {'remainingRounds': remaining, 'stage': detail.get('stage') if detail else 'initial',
               'documents': [{**d, 'output': d.get('output', '').partition('\nTASK_INPUTS ')[0]}
                             for d in mem.documents if d.get('kind') != 'read'],
               'inputPreview': {**mem.inputs, 'files': []},
               'lastAttempt': {k: attempt[k] for k in ('python', 'status', 'diagnosis', 'failureRepeatCount') if k in attempt},
               'rejectedCandidate': getattr(mem, 'rejected_transform', None),
               'rejections': [item['error'] for item in list(mem.history)[-3:] if 'error' in item],
               'verifiedMethods': history_methods(mem.skills, metadata(mem.inputs, mem.contract))}
    if attempt and not detail:
        # Transport/protocol failure without our marker: retain the actual evidence.
        context['lastAttempt']['sandbox'] = attempt.get('sandbox', '')
        context['lastAttempt']['runtimeError'] = attempt.get('runtimeError', '')
    failed = {f['case'] for f in (detail or {}).get('failures', []) if 'input' in f}
    for entry in mem.inputs.get('files', []):
        clean = dict(entry)
        if 'case_samples' in clean:
            clean.pop('sample', None)
            clean['case_samples'] = [c for c in clean['case_samples'] if c.get('case') not in failed]
        context['inputPreview']['files'].append(clean)
    # Explicitly requested supplementary evidence is retained once, outside the preview.
    context['additionalReads'] = [d for d in mem.documents if d.get('kind') == 'read']
    return ("第一行 PYTHON，后面只定义 transform(records) 和必要辅助函数，返回题目要求的数据。\n"
            "不要读取/写入文件、打印、运行checker或定义主程序。框架验证全部公开样例，通过后才写产物并运行独立checker；只提交真实token。\n"
            "按题目与完整样例推断过滤、排序、相同键顺序、分组及输出字段；保留排序字段到最后。不得硬编码样例答案、字段映射或特殊回合。\n"
            "order_only表示内容及数量一致，仅顺序不同：核对排序方向、多级排序与稳定性；record_membership_or_mixed表示缺失/多余记录，也可能同时有内容错误；value_or_structure核对字段、类型和数值。不得把混合错误当作单纯排序。\n"
            "execution异常先看traceback；checker失败说明公开样例不能唯一确定规则，结合反馈及边界证据修复，不按差额改答案。无法关联记录时不猜过滤。\n"
            "lastAttempt是最近实际执行；rejectedCandidate是未再次执行的失败AST。不要重发已拒绝代码，在本次回答直接给出有证据的修复，不额外花一回合只做分析。\n"
            "verifiedMethods仅是历史参考；output_adaptation_required需按当前输出适配，所有方法必须重新通过本题样例及checker。未知结构的旧方法未列入。\n"
            "剩余回合>4时可输出 READ 路径 偏移，样例文件偏移为case编号；大反例或长异常使用diagnosis.read_command，继续按NEXT_READ翻页。\n"
            "上下文：" + json.dumps(context, ensure_ascii=False))
