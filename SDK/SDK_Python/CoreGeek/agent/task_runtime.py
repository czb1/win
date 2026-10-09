"""Build model execution wrappers; execution stays in the official sandbox."""
import json
import re
from .task_evidence import LOG_AUDIT


def command_output(raw):
    """Remove only the optional platform duration header; keep status/output strict."""
    header, sep, body = raw.partition("\n")
    first, newline, rest = body.partition("\n")
    if re.fullmatch(r"\[durationMs:[0-9]+\]", first):
        body = rest if newline else ""
    return header + sep + body


def runtime_code(code, directory=None, rounding=None, log_task=False, parser=None):
    return (f"TASK_CODE = {code!r}\nTASK_DIRECTORY = {directory!r}\n"
            f"TASK_ROUNDING = {rounding!r}\nTASK_LOG = {log_task!r}\nLEARNED_PARSER = {parser!r}\n"
            + _RUNTIME.replace("# LOG_AUDIT_SETUP", LOG_AUDIT if log_task else ""))


def runtime_result(raw):
    """Remove wrapper metadata before applying the strict final-answer parser."""
    if len(raw.encode('utf-8')) > 65536:
        return raw, {}
    raw = command_output(raw)
    header, _, body = raw.partition("\n")
    first, sep, output = body.partition("\n")
    if not sep or not first.startswith("TASK_RUNTIME "):
        return raw, {}
    try:
        report = json.loads(first[len("TASK_RUNTIME "):])
    except ValueError:
        return raw, {}
    if not isinstance(report, dict):
        return raw, {}
    return header + "\n" + output, report


_RUNTIME = r'''
import ast
import contextlib
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from urllib.parse import urlsplit, parse_qs

report = {'http_successes': 0, 'http_errors': [], 'http_calls': [], 'json_shapes': []}

def task_round(value, ndigits=None):
    places = 0 if ndigits is None else ndigits
    rounded = Decimal(str(value)).quantize(Decimal('1').scaleb(-places), rounding=ROUND_HALF_UP)
    return int(rounded) if ndigits is None else float(rounded)

def track_parser(function):
    # Observe the proposed parser, without supplying guessed formats or records.
    def observed(*args, **kwargs):
        coverage = report.setdefault('parse_lines', {'total': 0, 'matched': 0, 'unmatched': []})
        coverage['total'] += 1
        value = function(*args, **kwargs)
        if value is not None and value is not False:
            coverage['matched'] += 1
        elif len(coverage['unmatched']) < 3:
            line = kwargs.get('line') or next((x for x in reversed(args) if isinstance(x, str)), '')
            coverage['unmatched'].append(line[:240])
        return value
    return observed

def remember_call(method, url, headers=None, params=None):
    address = urlsplit(str(url))
    if address.scheme not in ('http', 'https') or not address.hostname:
        return
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    authorization = headers.get('authorization', '')
    # Only protocol names survive; no credentials, query values or response data.
    auth = ('Bearer' if authorization.lower().startswith('bearer ') else
            'Basic' if authorization.lower().startswith('basic ') else
            'X-API-Key' if 'x-api-key' in headers else 'none')
    keys = set(parse_qs(address.query))
    if isinstance(params, dict):
        keys.update(str(k) for k in params)
    endpoint = address.scheme + '://' + address.hostname
    if address.port:
        endpoint += ':' + str(address.port)
    call = {'method': str(method).upper(), 'endpoint': endpoint + address.path,
            'auth': auth, 'parameters': sorted(keys)}
    if call not in report['http_calls'] and len(report['http_calls']) < 12:
        report['http_calls'].append(call)


def failure(url, detail):
    # Do not repeat headers, credentials, or query strings in diagnostics.
    path = urlsplit(str(url)).path
    if len(report['http_errors']) < 4:
        report['http_errors'].append({'path': path[:240], 'error': str(detail)[:1200]})

def payload_error(url, value):
    if isinstance(value, dict) and (value.get('status') in ('error', 'failed')
                                   or value.get('success') is False):
        failure(url, json.dumps(value, ensure_ascii=False))

def json_shape(value, depth=0):
    """Return bounded structural metadata without scalar response values."""
    if depth >= 4:
        return {'type': type(value).__name__}
    if isinstance(value, dict):
        raw_keys = list(value.keys())[:20]
        shape = {'type': 'dict', 'keys': [str(k)[:120] for k in raw_keys]}
        children = {}
        for key in raw_keys:
            child = value.get(key)
            if isinstance(child, (dict, list)):
                children[str(key)[:120]] = json_shape(child, depth + 1)
        if children:
            shape['children'] = children
        return shape
    if isinstance(value, list):
        shape = {'type': 'list', 'length': len(value)}
        item_types = []
        for item in value[:8]:
            name = type(item).__name__
            if name not in item_types:
                item_types.append(name)
        if item_types:
            shape['item_types'] = item_types
        if value:
            shape['item'] = json_shape(value[0], depth + 1)
        return shape
    return {'type': type(value).__name__}

def remember_json_shape(url, value):
    path = urlsplit(str(url)).path[:240]
    entry = {'path': path, 'shape': json_shape(value)}
    if entry not in report['json_shapes'] and len(report['json_shapes']) < 4:
        report['json_shapes'].append(entry)

original_urlopen = urllib.request.urlopen
def checked_urlopen(url, *args, **kwargs):
    address = getattr(url, 'full_url', url)
    if len(args) < 2:
        timeout = kwargs.get('timeout', 8)
        kwargs['timeout'] = 8 if timeout is None else min(float(timeout), 8)
    try:
        response = original_urlopen(url, *args, **kwargs)
    except urllib.error.HTTPError as error:
        # Leave the response body available to the caller for diagnosis.
        failure(address, 'HTTP ' + str(error.code))
        raise
    except (OSError, urllib.error.URLError) as error:
        failure(address, type(error).__name__)
        raise
    if str(address).startswith(('http://', 'https://')):
        report['http_successes'] += 1
        remember_call(getattr(url, 'get_method', lambda: 'GET')(), address,
                      dict(url.header_items()) if hasattr(url, 'header_items') else {})
    return response
urllib.request.urlopen = checked_urlopen

# requests is present in the official sandbox, but is not a host dependency.
try:
    import requests
except ImportError:
    pass
else:
    original_request = requests.sessions.Session.request
    original_json = requests.models.Response.json

    def checked_request(session, method, url, *args, **kwargs):
        timeout = kwargs.get('timeout', 8)
        if timeout is None:
            timeout = 8
        kwargs['timeout'] = (tuple(min(float(x), 8) for x in timeout)
                             if isinstance(timeout, tuple) else min(float(timeout), 8))
        try:
            response = original_request(session, method, url, *args, **kwargs)
        except requests.exceptions.RequestException as error:
            failure(url, type(error).__name__)
            raise
        if not 200 <= response.status_code < 300:
            failure(url, 'HTTP %s: %s' % (response.status_code, response.text[:1000]))
        else:
            report['http_successes'] += 1
            remember_call(method, url, kwargs.get('headers') or getattr(session, 'headers', {}),
                          kwargs.get('params'))
        return response

    def checked_json(response, *args, **kwargs):
        try:
            value = original_json(response, *args, **kwargs)
        except ValueError:
            failure(response.url, 'invalid JSON response')
            raise
        remember_json_shape(response.url, value)
        payload_error(response.url, value)
        return value

    requests.sessions.Session.request = checked_request
    requests.models.Response.json = checked_json

# LOG_AUDIT_SETUP

def parser_exception():
    error = sys.exc_info()[1]
    if isinstance(error, (ValueError, OSError)):
        log_error("caught_parser_error", type(error).__name__ + ": " + str(error))

def instrument(tree):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            is_parser = node.name == "parse_line" or (node.name.startswith("parse_") and node.name.endswith("_log"))
            if node.name == "parse_line":
                node.decorator_list.append(ast.Name(id="_task_track_parser", ctx=ast.Load()))
            if TASK_LOG and is_parser:
                node.decorator_list.append(ast.Name(id="_task_audit_parser", ctx=ast.Load()))
                for handler in ast.walk(node):
                    if isinstance(handler, ast.ExceptHandler):
                        handler.body.insert(0, ast.Expr(value=ast.Call(func=ast.Name(id="_task_parser_exception", ctx=ast.Load()), args=[], keywords=[])))
            if TASK_LOG and node.name.startswith("merge_") and "event" in node.name:
                node.decorator_list.append(ast.Name(id="_task_audit_merge", ctx=ast.Load()))
    ast.fix_missing_locations(tree)
    return tree

exit_code = 0
with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as output:
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            if TASK_DIRECTORY:
                os.chdir(TASK_DIRECTORY)
            scope = {'__name__': '__main__', '_task_track_parser': track_parser,
                     'task_result': lambda value: print('FINAL_ANSWER\n' + json.dumps(value, ensure_ascii=False))}
            learned_scope = {}
            if TASK_LOG:
                scope.update(_task_audit_parser=audited_function, _task_parser_exception=parser_exception,
                             _task_audit_merge=audit_merge, task_events=task_events, task_parse=task_parse)
                if LEARNED_PARSER:
                    learned_scope = dict(scope)
                    exec(compile(instrument(ast.parse(LEARNED_PARSER)), '<learned-parser>', 'exec'), learned_scope)
            if TASK_ROUNDING == 'half_up':
                scope['round'] = task_round
            tree = ast.parse(TASK_CODE, '<task>', 'exec')
            if TASK_LOG and LEARNED_PARSER:
                names = [n.name for n in ast.parse(LEARNED_PARSER).body if isinstance(n, ast.FunctionDef)]
                scope.update({name: learned_scope[name] for name in names})
                # Explicit same-format tasks reuse verified functions, not model rewrites.
                tree.body = [n for n in tree.body if not (isinstance(n, ast.FunctionDef) and n.name in names)]
                report['reused_parsers'] = names
            tree = instrument(tree)
            exec(compile(tree, '<task>', 'exec'), scope)
        except SystemExit as error:
            exit_code = error.code if isinstance(error.code, int) else (1 if error.code else 0)
        except BaseException:
            traceback.print_exc()
            exit_code = 1
    output.seek(0)
    body = output.read(60001)
print('TASK_RUNTIME ' + json.dumps(report, ensure_ascii=False))
print(body[:60000], end='')
if len(body) > 60000:
    print('\n[TRUNCATED]')
if TASK_LOG and (log_report['errors'] or any(not f['complete'] for f in log_report['files'].values())):
    print('\nTASK_INPUT_FAILED: missing/incomplete input or caught parsing error; repair before submitting.')
    exit_code = 1
if report['http_errors']:
    # Caught HTTP/JSON errors must not turn into successful empty statistics.
    print('\nTASK_QUERY_FAILED: fix the request; no aggregate answer is valid from this run.')
    exit_code = 1
raise SystemExit(exit_code)
'''
