"""Bounded parser repair proposals; host inspection never executes model code."""
import ast
import re
import sys


def repair_context(code, diagnostics):
    """Enable local repair only for named, observed failures with available source."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    parsers = diagnostics.get('logs', {}).get('parsers', {})
    targets = [name for name, evidence in parsers.items()
               if name in functions and (evidence.get('unmatched_count') or evidence.get('error')
                                         or evidence.get('invalid_return'))]
    if not targets:
        return None
    selected = set(targets)
    while True:
        refs = {node.id for name in selected for node in ast.walk(functions[name])
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
        expanded = selected | (refs & functions.keys())
        if expanded == selected:
            break
        selected = expanded
    dependencies = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            dependencies.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in selected:
            dependencies.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            if names & refs:
                # Do not present executable initialization as a reusable constant.
                try:
                    ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    return None
                dependencies.append(node)
    source = '\n\n'.join(ast.get_source_segment(code, node) or ast.unparse(node) for node in dependencies)
    if len(source) > 9000 or len(targets) > 8:
        return None  # Fall back to the existing complete-program workflow.
    return {'targets': targets, 'source': source,
            'failed_parser': diagnostics.get('failed_parser'),
            'parsers': {name: parsers[name] for name in targets},
            'systems': dict(list(diagnostics.get('systems', {}).items())[:8]),
            'errors': diagnostics.get('logs', {}).get('errors', [])[:8]}


def merge_repair(original, replacement, context):
    """Replace only failed function bodies, preserving signatures and all callers."""
    base, patch = ast.parse(original), ast.parse(replacement)
    targets = set(context['targets'])
    functions = {node.name: node for node in base.body if isinstance(node, ast.FunctionDef)}
    updates, imports = {}, []
    # Accept an unchanged full wrapper for older models, but never changed aggregation.
    untouched = [ast.dump(n) for n in base.body
                 if not (isinstance(n, ast.FunctionDef) and n.name in targets)]
    supplied = [ast.dump(n) for n in patch.body
                if not (isinstance(n, ast.FunctionDef) and n.name in targets)]
    full_program = supplied == untouched
    for node in patch.body:
        if isinstance(node, ast.FunctionDef) and node.name in targets:
            old = functions[node.name]
            if (node.name in updates or ast.dump(node.args) != ast.dump(old.args)
                    or ast.dump(node.returns or ast.Constant(None)) != ast.dump(old.returns or ast.Constant(None))
                    or [ast.dump(n) for n in node.decorator_list] != [ast.dump(n) for n in old.decorator_list]):
                raise ValueError('只修失败函数，保留函数签名和装饰器。')
            updates[node.name] = node
        elif full_program:
            continue
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or '']
            if (getattr(node, 'level', 0) or any(m.split('.')[0] not in sys.stdlib_module_names or m == '__future__' for m in modules)
                    or any(n.name == '*' for n in node.names)):
                raise ValueError('修复只允许必要的标准库显式导入。')
            # New aliases must not overwrite original globals used by aggregation.
            aliases = {n.asname or (n.name.split('.')[0] if isinstance(node, ast.Import) else n.name) for n in node.names}
            existing = {n.id for n in ast.walk(base) if isinstance(n, ast.Name)}
            existing.update(n.name for n in base.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)))
            for old in base.body:
                if isinstance(old, (ast.Import, ast.ImportFrom)):
                    existing.update(n.asname or (n.name.split('.')[0] if isinstance(old, ast.Import) else n.name) for n in old.names)
            if ast.dump(node) not in untouched:
                if aliases & existing:
                    raise ValueError('新增导入不能覆盖原程序名称；请复用已有导入。')
                imports.append(node)
        else:
            raise ValueError('只输出失败解析函数的完整定义；不输出主程序、聚合或 task_result。')
    if not updates:
        raise ValueError('缺少失败解析函数定义。')
    base.body = imports + [updates.get(n.name, n) if isinstance(n, ast.FunctionDef) else n for n in base.body]
    return ast.unparse(ast.fix_missing_locations(base))


def validate_literal_regex(code):
    """Compile literal patterns only; dynamic expressions remain sandbox work."""
    tree = ast.parse(code)
    modules, names = set(), set()
    methods = {'compile', 'match', 'fullmatch', 'search', 'findall', 'finditer', 'split', 'sub', 'subn'}
    for node in tree.body:
        if isinstance(node, ast.Import):
            modules.update(n.asname or n.name for n in node.names if n.name == 're')
        elif isinstance(node, ast.ImportFrom) and node.module == 're':
            names.update(n.asname or n.name for n in node.names if n.name in methods)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        known = (isinstance(fn, ast.Name) and fn.id in names or isinstance(fn, ast.Attribute)
                 and isinstance(fn.value, ast.Name) and fn.value.id in modules and fn.attr in methods)
        pattern = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == 'pattern'), None)
        if known and isinstance(pattern, ast.Constant) and isinstance(pattern.value, (str, bytes)) and len(pattern.value) <= 8192:
            # Flags can change validity (e.g. verbose patterns); inspect only calls with no flags.
            method = fn.attr if isinstance(fn, ast.Attribute) else next((n.name for imp in tree.body
                if isinstance(imp, ast.ImportFrom) and imp.module == 're' for n in imp.names
                if (n.asname or n.name) == fn.id), '')
            positional_limit = 1 if method == 'compile' else 3 if method in ('sub', 'subn', 'split') else 2
            if any(k.arg in ('flags', None) for k in node.keywords) or len(node.args) > positional_limit:
                continue
            try:
                re.compile(pattern.value)
            except (re.error, RecursionError, OverflowError) as error:
                raise ValueError('正则表达式无效（第%s行）：%s；直接写Python，不要JSON双重转义。' % (node.lineno, error)) from error
