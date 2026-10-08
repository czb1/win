"""Session-local learned methods: no historical inputs, outputs or credentials.

Recipes are proposals, not trusted host code. They are executed and checked in
exactly the same official sandbox as ordinary model code, before promotion.
"""
import ast
import builtins
import hashlib
import json
import re
import symtable


TYPES = {'str': str, 'int': int, 'float': float, 'bool': bool}


def shape(value):
    if isinstance(value, dict):
        return {key: shape(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return 'list'
    return type(value).__name__


def compatible(skill, point, contract, for_hint=False):
    return (skill.get('point') == point and not skill.get('disabled')
            and (skill.get('shape') == shape(contract.get('example'))
                 or (for_hint and skill.get('parser') and skill.get('family')
                     and skill['family'] == contract.get('family'))))


def parser_method(code):
    """Retain only self-contained parsing functions, never top-level task data."""
    try:
        tree = ast.parse(code)
        definitions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        if 'parse_line' not in definitions:
            return None
        needed, pending = {}, ['parse_line']
        while pending:
            name = pending.pop()
            if name in needed:
                continue
            needed[name] = definitions[name]
            pending.extend(n.id for n in ast.walk(definitions[name])
                           if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                           and n.id in definitions and n.id not in needed)
        functions = list(needed.values())
        imports = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
                   and all(name.split('.')[0] in {'re', 'datetime', 'collections', 'math', 'json'}
                           for name in ([a.name for a in n.names] if isinstance(n, ast.Import)
                                        else [n.module or '']))]
        nodes = imports + functions
        text = ast.unparse(ast.Module(body=nodes, type_ignores=[]))
        if len(text) > 6000 or any(isinstance(n, ast.Constant) and isinstance(n.value, str)
                and any(marker in n.value for marker in ('/tmp/', 'Bearer ', 'http://', 'https://'))
                for node in nodes for n in ast.walk(node)):
            return None
        allowed = set(dir(builtins)) - {'open', 'eval', 'exec', '__import__'}
        allowed.update(n.name for n in functions)
        for node in imports:
            allowed.update(a.asname or (a.name.split('.')[0] if isinstance(node, ast.Import) else a.name)
                           for a in node.names)
        tables = list(symtable.symtable(text, '<parser>', 'exec').get_children())
        while tables:
            table = tables.pop()
            if any(s.is_global() and s.is_referenced() and s.get_name() not in allowed
                   for s in table.get_symbols()):
                return None
            tables.extend(table.get_children())
        return text
    except (SyntaxError, ValueError, TypeError):
        return None


def recipe_proposal(recipe, inputs, max_chars):
    """Check explicit parameter binding; never infer values from old tasks."""
    if not isinstance(recipe, dict) or set(recipe) != {'parameters', 'python'}:
        raise ValueError('skill 必须仅含 parameters 和 python')
    parameters, code = recipe['parameters'], recipe['python']
    if (not isinstance(parameters, dict) or not 1 <= len(parameters) <= 16
            or any(not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', key) or kind not in TYPES
                   for key, kind in parameters.items())):
        raise ValueError('parameters 必须是参数名到 str/int/float/bool 的映射')
    if not isinstance(inputs, dict) or inputs.keys() != parameters.keys():
        raise ValueError('inputs 必须重新提供全部参数，不能沿用旧值')
    if any(type(inputs[key]) is not TYPES[kind] for key, kind in parameters.items()):
        raise ValueError('inputs 参数类型不符')
    if len(json.dumps(inputs, ensure_ascii=False)) > 12000:
        raise ValueError('inputs 过长')
    if not isinstance(code, str) or len(code) > max_chars:
        raise ValueError('skill 代码过长或不是字符串')
    tree = ast.parse(code)
    compile(tree, '<skill>', 'exec')
    refs = {node.slice.value for node in ast.walk(tree)
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
            and node.value.id == 'PARAMS' and isinstance(node.slice, ast.Constant)}
    if refs != parameters.keys():
        raise ValueError('代码必须通过 PARAMS[参数名] 使用全部声明参数')
    # Reject embedded current values, workspace paths and credential literals.
    # This is a quality gate, not a security boundary for arbitrary Python.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if '/tmp/' in value or re.search(r'Bearer\s+\S+', value):
                raise ValueError('工作区和密钥必须从本题 inputs 绑定')
            for key, item in inputs.items():
                if isinstance(item, str) and item and value != key and (
                        value == item or len(item) >= 4 and item in value):
                    raise ValueError('skill 含本题具体值；请改用 PARAMS')
    return {'parameters': dict(parameters), 'python': ast.unparse(tree)}


def bind_recipe(recipe, inputs, max_chars):
    recipe = recipe_proposal(recipe, inputs, max_chars)
    return 'PARAMS = ' + repr(inputs) + '\n' + recipe['python']


def output_supports(answer, output):
    """Bind a model restatement only to an exact observed JSON/scalar value."""
    def identity(text):
        try:
            return json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
        except (ValueError, TypeError):
            return str(text).strip()
    wanted = identity(answer)
    return bool(wanted) and any(identity(part) == wanted for part in
                               [output.strip(), output.partition('FINAL_ANSWER\n')[2]]
                               + output.splitlines())


def learned_method(point, contract, recipe=None, calls=(), parser=None):
    if recipe:
        recipe = {'parameters': dict(recipe['parameters']),
                  'python': ast.unparse(ast.parse(recipe['python']))}
    # Call metadata is generated by the runtime and contains names, never values.
    method = {'point': point, 'shape': shape(contract.get('example')),
              'workflow': contract.get('kind', 'query'), 'recipe': recipe,
              'interfaces': list(calls)[-12:],
              'steps': (['read_current_spec', 'repair_current_workspace', 'run_checker', 'submit_checked_token']
                        if contract.get('kind') == 'check_token' else
                        ['read_current_task_and_credentials', 'bind_current_inputs',
                         'query_all_pages', 'validate_response', 'compute_current_answer']),
              'evidence': 'legal_submission_then_task_disappeared',
              'verified': False, 'successes': 1, 'failures': 0, 'disabled': False}
    if parser:
        method.update(parser=parser, family=contract.get('family'))
    if contract.get('kind') == 'check_token' and not contract.get('repair_spec', True):
        method['steps'] = ['inspect_current_cases_and_input', 'transform_current_records',
                           'write_current_result', 'run_current_checker', 'submit_checked_token']
    elif contract.get('input_kind') == 'logs':
        method['steps'] = ['inspect_current_log_samples', 'parse_each_system',
                           'check_parse_coverage', 'aggregate_current_requirements']
    signature = json.dumps({k: method.get(k) for k in
                           ('point', 'shape', 'workflow', 'recipe', 'interfaces', 'parser', 'family')},
                           sort_keys=True, ensure_ascii=False)
    method['id'] = hashlib.sha256(signature.encode()).hexdigest()[:16]
    return method


def promote(skills, method):
    old = next((s for s in skills if s.get('id') == method['id']), None)
    if 'rounds' in method:
        durations = (old.get('durations', []) if old else []) + [method['rounds']]
        method['durations'] = durations[-3:]
        method['rounds'] = max(method['durations'])
    if old:
        method['successes'] += old.get('successes', 0)
        skills.remove(old)
    skills.append(method)
    del skills[:-8]
