"""Narrow heritage-query SOP. Only generated sandbox code performs I/O.

The current documents bind every URL, credential and city. Unknown contracts
fall back to the model; historical documents are locators, never cached inputs.
"""
from pathlib import PurePosixPath
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit


def reference_paths(documents, knowledge, point):
    """Recover auxiliary file locations for abbreviated follow-up questions."""
    if not documents:
        return []
    entry = documents[0]
    text = entry.get('output', '')
    source = entry.get('resolved_path', '')
    if not source or not re.search(r'API[^\n]*(?:相同|同一)', text, re.I):
        return []
    root = PurePosixPath(source).parent
    paths = []
    for record in knowledge:
        path = record.get('resolved_path', '')
        if (record.get('point') == point and record.get('kind') == 'read'
                and path and PurePosixPath(path).parent == root
                and re.search(r'https?://', record.get('output', ''))
                and path != source and path not in paths):
            paths.append(path)
    return paths[-2:]


def query_config(documents, contract):
    example = contract.get('example', {})
    if (set(example) != {'city', 'total_count', 'world_heritage_count', 'types', 'oldest_era'}
            or not isinstance(example.get('city'), str) or not example['city']):
        return None
    entry = documents[0].get('output', '') if documents else ''
    if not all(word in entry for word in ('文化遗产', '全部', '世界遗产', '年代最早')):
        return None
    # Accept only an explicit curl example, never execute its shell text.
    for document in documents[1:]:
        text = document.get('output', '')
        if 'NEXT_READ' in text:
            continue
        match = re.search(r'curl\s+-H\s+"(X-API-Key|Authorization):\s*([^"\r\n]+)"\s+"(https?://[^"\s]+)"', text)
        if not match:
            continue
        address = urlsplit(match[3])
        params = parse_qs(address.query)
        if (address.username or address.password or address.fragment
                or len(params) != 1 or any(len(v) != 1 for v in params.values())):
            continue
        parameter = next(iter(params))
        if parameter not in ('city', 'location'):
            continue
        endpoint = urlunsplit((address.scheme, address.netloc, address.path, '', ''))
        # The current question must name the same service as this document.
        origin = urlunsplit((address.scheme, address.netloc, '', '', ''))
        if origin not in entry:
            continue
        return {'endpoint': endpoint, 'header': match[1], 'credential': match[2],
                'parameter': parameter, 'city': example['city']}
    return None


def query_code(config):
    return 'CONFIG = ' + repr(config) + '\n' + _QUERY


_QUERY = r'''
import json
import re
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlencode

deadline = time.monotonic() + 12
headers = {CONFIG['header']: CONFIG['credential']}
parameter = CONFIG['parameter']
negotiated = set()
calls = []

def fetch(offset):
    global parameter, headers
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError('query time budget exhausted')
        params = {parameter: CONFIG['city'], 'offset': offset, 'limit': 100}
        request = Request(CONFIG['endpoint'] + '?' + urlencode(params), headers=headers)
        try:
            with urlopen(request, timeout=min(3, remaining)) as response:
                raw = response.read(1000001)
            if len(raw) > 1000000:
                raise ValueError('response too large')
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get('status') in ('error', 'failed') or value.get('success') is False:
                raise ValueError('invalid or failed response')
            if 'code' in value and value['code'] != 200:
                raise ValueError('API returned non-success code')
            call = {'method': 'GET', 'endpoint': CONFIG['endpoint'],
                    'auth': 'Bearer' if 'Authorization' in headers else 'X-API-Key',
                    'parameters': sorted(params)}
            if call not in calls:
                calls.append(call)
            return value
        except HTTPError as error:
            # Only explicit server corrections to known semantics are allowed.
            # Never guess paths, credentials or unrelated required parameters.
            detail = error.read(4096).decode('utf-8', errors='replace')
            error.close()
            if (error.code == 401 and 'auth' not in negotiated
                    and 'Authorization' in detail and 'Bearer' in detail
                    and 'X-API-Key' in headers):
                headers = {'Authorization': 'Bearer ' + headers['X-API-Key']}
                negotiated.add('auth')
            elif (error.code == 400 and 'parameter' not in negotiated
                    and re.search(r'Missing required parameter:\s*location\b', detail)
                    and parameter == 'city'):
                parameter = 'location'
                negotiated.add('parameter')
            else:
                raise ValueError('HTTP ' + str(error.code) + '; unsupported correction') from None
    raise ValueError('negotiation exhausted')


def era_rank(era):
    # Composite labels use their earliest named period, never lexicographic order.
    periods = ['旧石器时代', '新石器时代', '夏', '商', '西周', '东周', '春秋', '战国',
               '秦', '西汉', '东汉', '三国', '西晋', '东晋', '南北朝', '隋', '唐',
               '五代', '辽', '北宋', '金', '南宋', '元', '明', '清', '民国', '现代']
    ranks = {name: i for i, name in enumerate(periods)}
    ranks.update({'周': ranks['西周'], '汉': ranks['西汉'], '晋': ranks['西晋'],
                  '宋': ranks['北宋'], '六朝': ranks['三国'], '两晋': ranks['西晋']})
    pattern = '|'.join(sorted(ranks, key=len, reverse=True))
    found = re.findall(pattern, era)
    remainder = re.sub(pattern, '', era)
    if not found or remainder.strip(' 、，,/-—至及到时期年代'):
        return None
    return min(ranks[name] for name in found)


try:
    rows, ids, offset, total = [], set(), 0, None
    for page in range(200):
        value = fetch(offset)
        data = value.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('records'), list):
            raise ValueError('expected data.records list')
        records, pagination = data['records'], data.get('pagination')
        if not isinstance(pagination, dict):
            raise ValueError('missing pagination; completeness unproven')
        count, actual_offset, limit = [pagination.get(k) for k in ('total_count', 'offset', 'limit')]
        if (any(type(x) is not int for x in (count, actual_offset, limit))
                or count < 0 or limit <= 0 or actual_offset != offset
                or len(records) > limit or (total is not None and count != total)):
            raise ValueError('inconsistent pagination')
        total = count
        for row in records:
            if not isinstance(row, dict) or any(not isinstance(row.get(k), str) or not row[k]
                    for k in ('id', 'name', 'type', 'era', 'protected_level')):
                raise ValueError('unsupported record fields')
            if row['id'] in ids:
                raise ValueError('duplicate record or repeated page')
            ids.add(row['id'])
        rows.extend(records)
        if len(rows) > total:
            raise ValueError('records exceed total')
        if len(rows) == total:
            break
        if not records:
            raise ValueError('empty page before total reached')
        offset += len(records)
    else:
        raise ValueError('page budget exhausted')
    answer = {'city': CONFIG['city'], 'total_count': len(rows),
              'world_heritage_count': sum(r['protected_level'] == '世界遗产' for r in rows),
              'types': sorted({r['type'] for r in rows})}
    ranks = [era_rank(r['era']) for r in rows]
    if rows and all(rank is not None for rank in ranks):
        answer['oldest_era'] = rows[min(range(len(rows)), key=lambda i: ranks[i])]['name']
    # Unknown chronology yields evidenced fields only, not a fabricated oldest item.
    report = {'http_successes': page + 1, 'http_errors': [], 'http_calls': calls}
    print('TASK_RUNTIME ' + json.dumps(report, ensure_ascii=False))
    print('FINAL_ANSWER')
    print(json.dumps(answer, ensure_ascii=False))
except Exception as error:
    # No response body or credentials in persistent diagnostics.
    print('QUERY_DIAGNOSTIC', type(error).__name__, str(error)[:240])
    raise SystemExit(1)
'''
