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
import hashlib, json, os, re, shlex, subprocess, sys, tempfile, traceback
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

def unordered(value):
    # Type tags prevent bool/int and nested structure from comparing as equal.
    if isinstance(value, dict):
        return ('dict', tuple((k, unordered(v)) for k, v in sorted(value.items())))
    if isinstance(value, list):
        return ('list', tuple(sorted((unordered(v) for v in value), key=repr)))
    return (type(value).__name__, value)

def classify(expected, actual, evidence):
    if unordered(expected) == unordered(actual):
        return 'order_only'
    if evidence.get('missing_count') or evidence.get('extra_count'):
        return 'record_membership_or_mixed'
    return 'value_or_structure'

def distance(expected, actual):
    # Structural, scalar and ordering differences, independent of business fields.
    if type(expected) is not type(actual):
        return [1, 0, 0]
    if expected == actual:
        return [0, 0, 0]
    if unordered(expected) == unordered(actual):
        return [0, 0, 1]
    if isinstance(expected, dict):
        result = [len(expected.keys() ^ actual.keys()), 0, 0]
        pairs = ((expected[k], actual[k]) for k in expected.keys() & actual.keys())
    elif isinstance(expected, list):
        result = [abs(len(expected) - len(actual)), 0, 0]
        pairs = list(zip(expected, actual))
        # Align only when all candidate unique fields agree on correspondence.
        mappings = []
        if expected and actual and all(isinstance(r, dict) for r in expected + actual):
            for key in sorted(set.intersection(*(set(r) for r in expected + actual))):
                left, right = [r[key] for r in expected], [r[key] for r in actual]
                if (all(type(v) in (str, int) for v in left + right)
                        and len(set(left)) == len(left) and len(set(right)) == len(right)):
                    mapping = [(i, right.index(v)) for i, v in enumerate(left) if v in right]
                    if mapping:
                        mappings.append(mapping)
        if mappings and all(m == mappings[0] for m in mappings):
            mapping = mappings[0]
            order = [j for _, j in mapping]
            result = [len(expected) + len(actual) - 2 * len(mapping), 0,
                      int(order != sorted(order))]
            pairs = [(expected[i], actual[j]) for i, j in mapping]
    else:
        return [0, 1, 0]
    for a, b in pairs:
        result = [x+y for x,y in zip(result, distance(a,b))]
    return result

def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()

def emit_diagnostic(detail):
    # Keep complete, machine-readable evidence accessible even for large cases.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=base,
                                     prefix='.transform-diagnostic-', suffix='.json', delete=False) as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)
        saved = f.name
    view = dict(detail, evidence_path=saved, read_command='READ ' + shlex.quote(saved) + ' 0')
    if detail.get('stage') == 'samples':
        view['failures'] = []
        budget = 6500
        for item in detail['failures'][:2]:
            size = len(json.dumps(item, ensure_ascii=False))
            if size <= budget:
                view['failures'].append(item)
                budget -= size
            else:
                view['failures'].append({k: item[k] for k in ('case', 'difference', 'classification')})
                view['failures'][-1]['detail'] = 'Complete counterexample at evidence_path; not truncated JSON'
    if 'traceback' in view and len(view['traceback']) > 8000:
        view['traceback'] = view['traceback'][-8000:]
        view['traceback_truncated'] = True
    if 'checker_feedback' in view and len(view['checker_feedback']) > 8000:
        view['checker_feedback'] = view['checker_feedback'][-8000:]
        view['checker_feedback_truncated'] = True
    print('TRANSFORM_DIAGNOSTIC ' + json.dumps(view, ensure_ascii=False))

def bounded(value, limit=1800):
    encoded = json.dumps(value, ensure_ascii=False)
    return value if len(encoded) <= limit else {'detail': 'fragment exceeds budget', 'chars': len(encoded)}

def objects(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from objects(item)

def record_evidence(records, expected, actual):
    # Infer identity only when a unique input field survives in both projections.
    if not isinstance(records, list) or not records or not all(isinstance(r, dict) for r in records):
        return {'alignment': 'unavailable: input is not a nonempty record array'}
    candidates = []
    for key in sorted(set.intersection(*(set(r) for r in records))):
        ids = [r[key] for r in records]
        if not all(type(v) in (str, int) for v in ids) or len(set(ids)) != len(ids):
            continue
        projected = []
        for output in (expected, actual):
            items = [o[key] for o in objects(output) if key in o]
            if (not items or not all(type(v) in (str, int) for v in items)
                    or len(set(items)) != len(items) or not set(items) <= set(ids)):
                break
            projected.append(set(items))
        if len(projected) == 2:
            kept = tuple(i for i, v in enumerate(ids) if v in projected[0])
            produced = tuple(i for i, v in enumerate(ids) if v in projected[1])
            candidates.append((key, kept, produced))
    if not candidates or len({(c[1], c[2]) for c in candidates}) != 1:
        return {'alignment': 'unavailable: no unambiguous preserved unique field; do not infer a filter'}
    key, kept, produced = candidates[0]
    missing, extra = set(kept)-set(produced), set(produced)-set(kept)
    contrasts = []
    for index in sorted(missing | extra)[:2]:
        record = records[index]
        opposite = [i for i in range(len(records)) if (i in kept) != (index in kept)]
        if opposite:
            other = max(opposite, key=lambda i: sum(
                k != key and k in records[i] and records[i][k] == v for k, v in record.items()))
            contrasts.append({'record': record, 'expected_kept': index in kept,
                              'contrast': records[other], 'contrast_expected_kept': other in kept})
    return {'alignment': 'unique input field preserved in output', 'identity_field': key,
            'contrasts': bounded(contrasts),
            'missing_count': len(missing), 'extra_count': len(extra),
            'input_order': bounded([r[key] for r in records]),
            'expected_order': bounded([o[key] for o in objects(expected) if key in o]),
            'actual_order': bounded([o[key] for o in objects(actual) if key in o]),
            'missing_records': bounded([records[i] for i in sorted(missing)[:4]]),
            'extra_records': bounded([records[i] for i in sorted(extra)[:4]]),
            'expected_kept': numeric_summary([records[i] for i in kept]),
            'expected_dropped': numeric_summary([r for i,r in enumerate(records) if i not in kept])}

def numeric_summary(records):
    result = {'count': len(records), 'numeric_fields': {}}
    if not records or not all(isinstance(r, dict) for r in records):
        return result
    for key in sorted(set.intersection(*(set(r) for r in records)))[:24]:
        values = [r[key] for r in records]
        if all(type(v) in (int, float) for v in values):
            values = sorted(set(values))
            result['numeric_fields'][key] = {'min': values[0], 'max': values[-1],
                                             'lowest': values[:4], 'highest': values[-4:]}
    return bounded(result, 2200)

def checker_fragment(value, text):
    match = re.search(r'\$(?:(?:/[^\s/\[\]：:,]+)|(?:\[\d+\]))+', text)
    if not match:
        return {'path': None, 'output': bounded(value)}
    location = match.group(0)
    current, parent = value, value
    try:
        for key, index in re.findall(r'/([^/\[\]]+)|\[(\d+)\]', location[1:]):
            parent = current
            current = current[key] if key else current[int(index)]
        return {'path': location, 'actual': bounded(current), 'parent': bounded(parent)}
    except (KeyError, IndexError, TypeError, ValueError):
        return {'path': location, 'detail': 'checker path cannot be resolved; do not guess'}


stage = 'input'
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
    stage = 'execution'
    with tempfile.TemporaryFile() as out, tempfile.NamedTemporaryFile(
            dir=base, prefix='.transform-stderr-', suffix='.txt', delete=False) as err:
        run = subprocess.run([sys.executable, '-I', '-c', WORKER], input=request.encode(), stdout=out, stderr=err, timeout=6)
        out.seek(0); raw = out.read(60001)
        error_size = err.tell()
        err.seek(max(0, error_size - 16000)); error = err.read(16000).decode(errors='replace')
    if run.returncode or len(raw) > 60000:
        emit_diagnostic({'stage': stage, 'classification': 'code_exception' if run.returncode else 'output_limit',
                         'exit_code': run.returncode, 'traceback': error, 'output_limit': len(raw) > 60000,
                         'traceback_truncated': error_size > 16000, 'stderr_path': err.name,
                         'stderr_read_command': 'READ ' + shlex.quote(err.name) + ' 0'})
        raise SystemExit(1)
    Path(err.name).unlink()
    values = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))
    if not isinstance(values, list) or len(values) != len(cases) + 1:
        raise ValueError('invalid transform output')
    stage = 'samples'
    failures, quality = [], []
    for i, case in enumerate(cases):
        expected = case['expected'] if 'expected' in case else case['output']
        mismatch = diff(expected, values[i])
        evidence = record_evidence(case['input'], expected, values[i]) if mismatch else {}
        quality.append([int(bool(mismatch)), *distance(expected, values[i]),
                        evidence.get('missing_count', 0) + evidence.get('extra_count', 0)])
        if mismatch:
            failures.append({'case': i, 'difference': mismatch, 'input': case['input'], 'expected': expected, 'actual': values[i],
                             'classification': classify(expected, values[i], evidence), 'record_evidence': evidence})
    if failures:
        emit_diagnostic({'stage': 'samples', 'case_count': len(cases),
                         'failed_count': len(failures), 'failures': failures,
                         'sample_fingerprint': fingerprint(cases),
                         'output_fingerprint': fingerprint(values[:-1]), 'quality': quality})
        raise SystemExit(1)
    stage = 'output'
    encoded = json.dumps(values[-1], ensure_ascii=False, allow_nan=False)
    destination = path(CONFIG['output'])
    # Never overwrite input, samples or checker, including aliases.
    if destination.resolve() in {path(n).resolve() for n in (CONFIG['input'], CONFIG['cases'], CONFIG['checker'])}:
        raise ValueError('output aliases protected input')
    destination.write_text(encoded, encoding='utf-8')
    stage = 'checker'
    with tempfile.NamedTemporaryFile(dir=base, prefix='.transform-checker-', suffix='.txt', delete=False) as out:
        checked = subprocess.run([str(path(CONFIG['checker']))], cwd=base, stdout=out, stderr=subprocess.STDOUT, timeout=6)
        checker_size = out.tell()
        out.seek(max(0, checker_size - 16000)); raw = out.read(16000)
    text = raw.decode(errors='replace')
    tokens = re.findall(r'^TOKEN:[ \t]*(\S+)[ \t]*$', text, re.M)
    if checked.returncode != 0 or checker_size > 16000 or len(tokens) != 1 or re.search(r'\[FAIL\]', text):
        emit_diagnostic({
            'stage': 'checker', 'classification': 'checker_failure', 'exit_code': checked.returncode,
            'samples_passed': len(cases), 'checker_feedback': text,
            'checker_feedback_truncated': checker_size > 16000, 'checker_output_path': out.name,
            'checker_read_command': 'READ ' + shlex.quote(out.name) + ' 0',
            'output_fragment': checker_fragment(values[-1], text),
            'input_summary': numeric_summary(records),
            'sample_evidence': [record_evidence(c['input'], c.get('expected', c.get('output')), values[i])
                                for i,c in enumerate(cases[:3])],
            'next_action': 'Public samples may admit multiple rules. Compare kept/dropped boundaries and current input; change one supported rule, never patch the answer.'
        })
        raise SystemExit(1)
    Path(out.name).unlink()
    print('FINAL_ANSWER')
    print(json.dumps({'token': tokens[0]}))
except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
    emit_diagnostic({'stage': stage, 'classification': 'timeout' if isinstance(error, subprocess.TimeoutExpired) else 'exception',
                     'error_type': type(error).__name__, 'message': str(error), 'traceback': traceback.format_exc()})
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
