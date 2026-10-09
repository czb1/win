"""Opt-in sample-driven transforms. Host builds code; only executeCmd runs it."""
import ast
import json
from pathlib import PurePosixPath
import re


def transform_config(documents, contract, inputs):
    if contract.get('kind') != 'check_token' or contract.get('repair_spec', True):
        return None
    workspace = contract.get('workspace')
    if not workspace or inputs.get('directory') != workspace:
        return None
    samples = [f for f in inputs.get('files', []) if f.get('case_count')]
    if len(samples) != 1:
        return None
    text = '\n'.join(d.get('output', '').partition('\nTASK_INPUTS ')[0] for d in documents)
    outputs = set(re.findall(r'(?:写入|写到|保存到|write\s+to)\s*`([^`]+\.json)`', text, re.I))
    if len(outputs) != 1:
        return None
    output = next(iter(outputs))
    candidates = [f['path'] for f in inputs.get('files', []) if f.get('type') == 'list'
                  and f['path'] != output and not f.get('case_count')]
    if len(candidates) != 1:
        return None
    paths = [samples[0]['path'], candidates[0], output]
    if any(PurePosixPath(p).is_absolute() or '..' in PurePosixPath(p).parts for p in paths) or len(set(paths)) != 3:
        return None
    return dict(workspace=workspace, cases=paths[0], input=paths[1], output=paths[2], checker=contract['checker'])


def transform_method(code):
    """Accept definitions only. No old data, I/O, checker calls or executable driver."""
    tree = ast.parse(code)
    allowed = {'collections', 'itertools', 'functools', 'math', 'decimal', 'datetime', 're', 'json', 'operator', 'copy'}
    names = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            if node.decorator_list or node.args.defaults or any(v is not None for v in node.args.kw_defaults):
                raise ValueError('转换函数不要使用装饰器或默认参数表达式')
            names.append(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or '']
            if any(m.split('.')[0] not in allowed for m in modules) or getattr(node, 'level', 0):
                raise ValueError('转换函数只需纯数据处理，不要导入文件/进程模块')
        elif isinstance(node, ast.Assign):
            if any(not isinstance(t, ast.Name) for t in node.targets):
                raise ValueError('只允许简单常量赋值')
            ast.literal_eval(node.value)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        else:
            raise ValueError('只输出 transform(records) 和必要辅助函数；不要读取、写入文件或调用 checker')
    if names.count('transform') != 1:
        raise ValueError('请提供一个 transform(records)，由框架验证全部样例并生成文件')
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'transform')
    if len(fn.args.posonlyargs + fn.args.args) != 1 or fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
        raise ValueError('transform 只接收一个 records 参数')
    # Quality gate, not a security boundary. The official sandbox remains the boundary.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in {'open', 'eval', 'exec', '__import__', 'globals', 'locals', 'compile', 'input', 'print'}:
            raise ValueError('转换函数只返回数据，不读写文件、打印或动态执行')
        if isinstance(node, ast.Attribute) and node.attr.startswith('__'):
            raise ValueError('转换函数不要访问内部属性')
    return ast.unparse(tree)


def transform_code(code, config):
    return 'CONFIG = ' + repr(config) + '\nMETHOD = ' + repr(transform_method(code)) + '\n' + _RUNNER


# Runs in a separate Python process; model globals cannot replace the checker,
# comparison or token extraction in the parent. This is not a sandbox escape guard.
_WORKER = r'''
import contextlib, io, json, sys
request = json.load(sys.stdin)
scope = {'__name__': 'task_transform'}
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    exec(compile(request['code'], '<transform>', 'exec'), scope)
    values = [scope['transform'](records) for records in request['inputs']]
print(json.dumps(values, ensure_ascii=False, allow_nan=False))
'''

_RUNNER = r'''
import json, os, re, subprocess, sys, tempfile
from pathlib import Path
base = Path(CONFIG['workspace']).resolve(strict=True)
os.chdir(base)

def path(name):
    p = base / name
    if p.is_symlink() or not p.resolve().is_relative_to(base):
        raise ValueError('path outside current workspace')
    return p

def read(name):
    with path(name).open(encoding='utf-8') as f:
        text = f.read(262145)
    if len(text) > 262144:
        raise ValueError('input exceeds bounded transform size')
    return json.loads(text)

def diff(a, b, location='$'):
    if type(a) is not type(b):
        return {'path': location, 'expected_type': type(a).__name__, 'actual_type': type(b).__name__}
    if isinstance(a, dict):
        if a.keys() != b.keys():
            return {'path': location, 'expected_keys': sorted(a), 'actual_keys': sorted(b)}
        for key in a:
            mismatch = diff(a[key], b[key], location + '/' + str(key))
            if mismatch: return mismatch
    elif isinstance(a, list):
        if len(a) != len(b):
            return {'path': location, 'expected_length': len(a), 'actual_length': len(b)}
        for i, (x, y) in enumerate(zip(a, b)):
            mismatch = diff(x, y, location + '[' + str(i) + ']')
            if mismatch: return mismatch
    elif a != b:
        return {'path': location, 'expected': a, 'actual': b}
    return None

try:
    data = read(CONFIG['cases'])
    cases = data.get('cases') if isinstance(data, dict) else data
    if not isinstance(cases, list) or not 1 <= len(cases) <= 100:
        raise ValueError('expected bounded nonempty case list')
    if any(not isinstance(c, dict) or 'input' not in c or len(set(c) & {'expected', 'output'}) != 1 for c in cases):
        raise ValueError('ambiguous input/expected pairs')
    records = read(CONFIG['input'])
    if not isinstance(records, list):
        raise ValueError('input is not a record array')
    request = json.dumps({'code': METHOD, 'inputs': [c['input'] for c in cases] + [records]}, ensure_ascii=False)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        run = subprocess.run([sys.executable, '-I', '-c', WORKER], input=request.encode(), stdout=out, stderr=err, timeout=6)
        out.seek(0); raw = out.read(60001)
        err.seek(0); error = err.read(2000).decode(errors='replace')
    if run.returncode or len(raw) > 60000:
        raise ValueError('transform execution failed: ' + error)
    values = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    if not isinstance(values, list) or len(values) != len(cases) + 1:
        raise ValueError('invalid transform output')
    failures = []
    for i, case in enumerate(cases):
        expected = case['expected'] if 'expected' in case else case['output']
        mismatch = diff(expected, values[i])
        if mismatch and len(failures) < 3:
            failures.append({'case': i, 'difference': mismatch, 'input': case['input'], 'expected': expected, 'actual': values[i]})
    if failures:
        # Complete first mismatch pair; never dump all cases or splice JSON mid-record.
        detail = {'case_count': len(cases), 'failures': []}
        for index, item in enumerate(failures):
            if index > 0 or len(json.dumps(item, ensure_ascii=False)) > 3500:
                item = {'case': item['case'], 'difference': item['difference'], 'detail': 'large case; inspect this case separately'}
            detail['failures'].append(item)
        print('TRANSFORM_DIAGNOSTIC ' + json.dumps(detail, ensure_ascii=False))
        raise SystemExit(1)
    encoded = json.dumps(values[-1], ensure_ascii=False, allow_nan=False)
    destination = path(CONFIG['output'])
    # Never overwrite input, samples or checker, including aliases.
    if destination.resolve() in {path(n).resolve() for n in (CONFIG['input'], CONFIG['cases'], CONFIG['checker'])}:
        raise ValueError('output aliases protected input')
    destination.write_text(encoded, encoding='utf-8')
    with tempfile.TemporaryFile() as out:
        checked = subprocess.run([str(path(CONFIG['checker']))], cwd=base, stdout=out, stderr=subprocess.STDOUT, timeout=6)
        out.seek(0); raw = out.read(16001)
    text = raw.decode(errors='replace')
    tokens = re.findall(r'^TOKEN:[ \t]*(\S+)[ \t]*$', text, re.M)
    if checked.returncode != 0 or len(raw) > 16000 or len(tokens) != 1 or re.search(r'\[FAIL\]', text):
        print('TRANSFORM_DIAGNOSTIC samples passed; checker rejected:\n' + text[:8000])
        raise SystemExit(1)
    print('FINAL_ANSWER')
    print(json.dumps({'token': tokens[0]}))
except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
    print('TRANSFORM_DIAGNOSTIC ' + str(error)[:2000])
    raise SystemExit(1)
'''
_RUNNER = 'WORKER = ' + repr(_WORKER) + '\n' + _RUNNER


def case_page_code(config, start=0):
    """Case offsets are case indices, never character cuts through paired JSON."""
    return 'CONFIG = ' + repr(config) + '\nSTART = ' + repr(start) + '\n' + r'''
import json
from pathlib import Path
path = Path(CONFIG['workspace']) / CONFIG['cases']
with path.open(encoding='utf-8') as source:
    raw = source.read(262145)
if len(raw) > 262144:
    raise ValueError('sample file too large')
data = json.loads(raw)
cases = data.get('cases') if isinstance(data, dict) else data
if not isinstance(cases, list) or not 0 <= START < len(cases):
    raise ValueError('invalid case index')
page, size = [], 0
for index in range(START, len(cases)):
    pair = {'case': index, **cases[index]}
    count = len(json.dumps(pair, ensure_ascii=False))
    if size + count > 5500:
        if not page:
            raise ValueError('single sample exceeds page budget; needs targeted inspection')
        break
    page.append(pair); size += count
print('DOCUMENT', path, 'OFFSET', START)
print(json.dumps({'case_samples': page, 'case_count': len(cases)}, ensure_ascii=False))
if START + len(page) < len(cases):
    print('NEXT_CASE', repr(str(path)), START + len(page))
'''
