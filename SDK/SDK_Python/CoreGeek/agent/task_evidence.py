"""Host-side answer evidence and sandbox-only log diagnostics."""
import ast
import json
import math
import re
from .task_sop import answer_error


def result_object(text, contract):
    """Exactly one finite JSON object; no diagnostics, duplicate keys or tokens."""
    if not contract.get('example') or contract.get('kind') == 'check_token':
        return None
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs,
                           parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
        def finite(item):
            if isinstance(item, dict):
                return all(finite(v) for v in item.values())
            if isinstance(item, list):
                return all(finite(v) for v in item)
            return not isinstance(item, float) or math.isfinite(item)
        if not isinstance(value, dict) or not finite(value) or answer_error(text, contract):
            return None
        return json.dumps(value, ensure_ascii=False)
    except (ValueError, TypeError):
        return None


def computed_answer(body, contract):
    body = body.strip()
    if body.startswith('FINAL_ANSWER\n'):
        body = body.partition('\n')[2]
    if body.startswith('RESULT '):
        try:
            tree = ast.parse(body[7:], mode='eval')
            if sum(1 for _ in ast.walk(tree)) > 10000:
                return None
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    keys = [ast.literal_eval(k) for k in node.keys]
                    if any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
                        return None
            value = ast.literal_eval(tree)
            # Only JSON-native values: tuples/sets/bytes are not silently converted.
            def native(v):
                return (v is None or type(v) in (str, int, float, bool)
                        or type(v) is list and all(native(x) for x in v)
                        or type(v) is dict and all(type(k) is str and native(x) for k, x in v.items()))
            if not native(value):
                return None
            body = json.dumps(value, allow_nan=False)
        except (SyntaxError, ValueError, TypeError, RecursionError):
            return None
    return result_object(body, contract)


def evidence_matches(answer, output, contract):
    supported = computed_answer(output, contract)
    proposed = result_object(answer, contract)
    if supported is None or proposed is None:
        return False
    # Permit a partial submission only if every supplied leaf has evidence.
    def subset(value, source):
        if isinstance(value, dict):
            return isinstance(source, dict) and all(k in source and subset(v, source[k]) for k, v in value.items())
        return type(value) is type(source) and value == source
    return subset(json.loads(proposed), json.loads(supported))


def parsing_rules(documents):
    """Keep complete rule paragraphs, including adjacent definitions without keywords."""
    blocks, size = [], 0
    for document in documents:
        text = document.get('output', '').partition('\nTASK_INPUTS ')[0]
        text = re.sub(r'```.*?```', '', text, flags=re.S)
        for block in re.split(r'\n\s*\n', text):
            block = '\n'.join(line for line in block.splitlines()
                              if not re.search(r'/tmp/|https?://|\d{4}-\d\d-\d\d', line))
            if not re.search(r'故障|失败|ERROR|5xx|OOM|格式|解析', block):
                continue
            if block in blocks:
                continue
            if size + len(block) > 6000:
                blocks.append('[规则未完整保存；需读取原文，不得猜测缺失定义]')
                break
            blocks.append(block)
            size += len(block)
    return '\n\n'.join(blocks)


def inherited_rules(skills):
    """Keep chronological source evidence; a reference cannot erase definitions."""
    blocks = []
    for skill in skills:
        if skill.get('rules_snapshot'):
            blocks = []
        for block in skill.get('rules', '').split('\n\n'):
            if block and block not in blocks:
                blocks.append(block)
    text = '\n\n'.join(blocks)
    return text if len(text) <= 12000 else text[:12000] + '\n[规则省略；请读取来源文档补全]'


# Injected only for log tasks. No model code or local task file runs on the host.
