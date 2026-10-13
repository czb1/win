"""Build model execution wrappers; execution stays in the official sandbox."""
import json
import re


def command_output(raw):
    """Remove only the optional platform duration header; keep status/output strict."""
    header, sep, body = raw.partition("\n")
    first, newline, rest = body.partition("\n")
    if re.fullmatch(r"\[durationMs:[0-9]+\]", first):
        body = rest if newline else ""
    return header + sep + body


def runtime_code(code, directory=None, rounding=None, log_task=False, parser=None, contract=None):
    return (f"TASK_CODE = {code!r}\nTASK_DIRECTORY = {directory!r}\n"
            + _RUNTIME)


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
import contextlib
import json
import os
import sys
import tempfile
import traceback
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit, parse_qs, urlencode

report = {'http_successes': 0, 'http_errors': [], 'http_calls': [], 'json_shapes': []}
request_errors = {}

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
    # Full URL is used only as an internal retry identity, never emitted.
    request_errors[str(url)] = {'path': urlsplit(str(url)).path[:240], 'error': str(detail)[:1200]}

def recovered(url):
    request_errors.pop(str(url), None)

def payload_error(url, value):
    # Business payloads can legitimately contain success=false/status=failed.
    # Keep structure as evidence; do not turn domain data into transport errors.
    return

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
        recovered(address)
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
        identity_url = str(url)
        params = kwargs.get('params')
        if params:
            identity_url += ('&' if '?' in identity_url else '?') + (urlencode(params, doseq=True) if not isinstance(params, str) else params)
        try:
            response = original_request(session, method, url, *args, **kwargs)
        except requests.exceptions.RequestException as error:
            failure(identity_url, type(error).__name__)
            raise
        if not 200 <= response.status_code < 300:
            failure(identity_url, 'HTTP %s: %s' % (response.status_code, response.text[:1000]))
        else:
            report['http_successes'] += 1
            recovered(identity_url)
            remember_call(method, url, kwargs.get('headers') or getattr(session, 'headers', {}),
                          kwargs.get('params'))
        response._task_identity_url = identity_url
        return response

    def checked_json(response, *args, **kwargs):
        try:
            value = original_json(response, *args, **kwargs)
        except ValueError:
            failure(getattr(response, '_task_identity_url', response.url), 'invalid JSON response')
            raise
        remember_json_shape(response.url, value)
        payload_error(response.url, value)
        return value

    requests.sessions.Session.request = checked_request
    requests.models.Response.json = checked_json


answer_value = []
def task_result(text):
    if not isinstance(text, str):
        raise TypeError("task_result 需要最终提交文本；JSON 答案请先调用 json.dumps")
    answer_value[:] = [text]

exit_code = 0
with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as output:
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        try:
            if TASK_DIRECTORY:
                os.chdir(TASK_DIRECTORY)
            scope = {'__name__': '__main__', 'task_result': task_result}
            exec(compile(TASK_CODE, '<task>', 'exec'), scope)
        except SystemExit as error:
            exit_code = error.code if isinstance(error.code, int) else (1 if error.code else 0)
        except BaseException:
            traceback.print_exc()
            exit_code = 1
    output.seek(0)
    body = output.read(60001)
report['http_errors'] = list(request_errors.values())[:4]
if answer_value:
    report['stdout'] = body[:6000]
print('TASK_RUNTIME ' + json.dumps(report, ensure_ascii=False))
if answer_value and not exit_code and not report['http_errors'] and len(body) <= 60000:
    print('FINAL_ANSWER')
    print(answer_value[0])
else:
    print(body[:60000], end='')
if len(body) > 60000:
    print('\n[TRUNCATED]')
if report['http_errors']:
    # Caught HTTP/JSON errors must not turn into successful empty statistics.
    print('\nTASK_QUERY_FAILED: fix the request; no aggregate answer is valid from this run.')
    exit_code = 1
raise SystemExit(exit_code)
'''
