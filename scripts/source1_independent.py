"""Source1 exported nodes: isolated GET probes, measured geo, scoped Feishu sync."""
import concurrent.futures
import contextlib
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
import yaml
from source3_compatible import atomic_json, eligible_candidates

ROOT = Path('C:/mihomo/source1-extraction')
LABEL = '源1独立Mihomo'
TARGETS = {'grok': 'https://grok.com/', 'chatgpt': 'https://chatgpt.com/robots.txt',
           'openai': 'https://api.openai.com'}


def accepted(label, result):
    return result.get('exit') == 0 and (result.get('status') == 200 or
                                      label == 'openai' and result.get('status') == 421)


def latency_fields(grok, chatgpt, openai):
    values = [float(value) for value in (grok, chatgpt, openai)
              if value is not None and float(value) > 0]
    return {'存活': bool(values), '延迟ms': int(min(values)) if values else -1,
            'Grok延迟ms': int(float(grok)) if grok is not None and float(grok) > 0 else -1,
            '延迟口径': '成功目标最小GET请求耗时；Grok/GPT/OpenAI分别记录',
            '测速目标': '; '.join(TARGETS.values())}


def ensure_latency_fields(client, table, log, helpers):
    helpers['ensure_fields'](client, table,
        [('Grok延迟ms', helpers['FT_NUMBER'], helpers['NUMBER_PROPERTY']),
         ('延迟口径', helpers['FT_TEXT'], None)], log)


def probe(port, index, runtime, helpers):
    result = {}
    for label, url in TARGETS.items():
        headers = runtime / f'{index}-{label}.headers'
        command = ['curl.exe', '-q', '--silent', '--noproxy', '', '--proxy', f'socks5h://127.0.0.1:{port}',
                   '--max-time', '8', '-o', 'NUL', '-D', str(headers), '-w', '%{http_code} %{time_total}', url]
        try:
            p = subprocess.run(command, capture_output=True, text=True, timeout=10,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            parts = p.stdout.split()
            raw = headers.read_text(errors='replace') if headers.exists() else ''
            result[label] = {'exit': p.returncode, 'status': int(parts[0]) if parts else 0,
                             'ms': max(1, int(float(parts[1])*1000)) if len(parts)>1 else None,
                             'challenge': 'cf-mitigated: challenge' in raw.lower()}
        except (ValueError, subprocess.TimeoutExpired):
            result[label] = {'exit': -1, 'status': 0, 'ms': None}
    result['region'] = helpers['fetch_geo_via_ninja_socks'](port)
    result['completed'] = True
    return result


def measure(nodes, helpers, log):
    directory = Path(tempfile.mkdtemp(prefix='source1-feishu-'))
    held = []
    process = None
    executor = None
    try:
        # Reserve distinct loopback ports until the config is ready to launch.
        ports = []
        for _ in range(len(nodes)+1):
            sock = socket.socket(); sock.bind(('127.0.0.1', 0)); held.append(sock)
            ports.append(sock.getsockname()[1])
        controller = ports.pop()
        config = {'allow-lan': False, 'mode': 'rule', 'log-level': 'warning', 'ipv6': False,
                  'external-controller': f'127.0.0.1:{controller}', 'tun': {'enable': False},
                  'proxies': nodes, 'rules': ['MATCH,REJECT'],
                  'listeners': [{'name': f's1-{i}', 'type': 'mixed', 'listen': '127.0.0.1',
                                 'port': port, 'proxy': node['name']} for i,(node,port) in enumerate(zip(nodes,ports))]}
        path = directory/'config.yaml'
        path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
        for sock in held:
            sock.close()
        held = []
        process = subprocess.Popen(['C:/mihomo/mihomo.exe', '-d', str(directory), '-f', str(path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        for _ in range(60):
            if process.poll() is not None:
                raise RuntimeError('source1_isolated_core_exited')
            try:
                with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                        f'http://127.0.0.1:{controller}/proxies', timeout=1) as response:
                    loaded = json.load(response)['proxies']
                if not all(n['name'] in loaded for n in nodes):
                    raise RuntimeError('source1_nodes_missing')
                break
            except OSError:
                time.sleep(.25)
        else:
            raise RuntimeError('source1_core_not_ready')
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        futures = {executor.submit(probe, port, i, directory, helpers): n['name']
                   for i,(n,port) in enumerate(zip(nodes,ports))}
        done, pending = concurrent.futures.wait(futures, timeout=250)
        results = {futures[f]: f.result() for f in done}
        for f in pending:
            f.cancel()
        log.info(f'source1 probes completed={len(done)} pending={len(pending)}')
        return results
    finally:
        if executor:
            executor.shutdown(wait=True, cancel_futures=True)
        for sock in held:
            sock.close()
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
        shutil.rmtree(directory, ignore_errors=True)


def replace_scope(client, table, records):
    old = [r['record_id'] for r in client.list_records(table)
           if r.get('fields', {}).get('测速路径') == LABEL]
    if records:
        client.batch_create(table, records)
    if old:
        client.batch_delete(table, old)
    saved = [r['fields'] for r in client.list_records(table)
             if r.get('fields', {}).get('测速路径') == LABEL]
    expected = {r['fields']['节点名']: r['fields'] for r in records}
    if len(saved) != len(expected) or {r.get('节点名') for r in saved} != set(expected):
        raise RuntimeError('source1_readback_names_mismatch')
    for row in saved:
        want = expected[row['节点名']]
        for key in ('同步时间', '实际地区', '存活', 'GPT可用', 'Grok可用', '地区组', '测速目标', '地区检测时间'):
            if key in want and row.get(key) != want[key]:
                raise RuntimeError('source1_readback_mismatch:' + key)
        if '连续通过轮数' in want and int(row.get('连续通过轮数', -1)) != want['连续通过轮数']:
            raise RuntimeError('source1_streak_readback_mismatch')
        for key in ('延迟ms', 'Grok延迟ms', 'chatgpt延迟ms', 'openai延迟ms'):
            if key in want and float(row.get(key, -1)) != float(want[key]):
                raise RuntimeError('source1_delay_readback_mismatch:' + key)
    return saved


def sync(client, node_table, region_table, log, helpers):
    nodes = yaml.safe_load((ROOT/'all-nodes.private.yaml').read_text(encoding='utf-8'))['proxies']
    names = [n['name'] for n in nodes]
    if not nodes or len(set(names)) != len(names):
        raise RuntimeError('invalid_source1_export')
    if any(n.get('dialer-proxy') or n.get('server') in ('127.0.0.1','localhost','::1') for n in nodes):
        raise RuntimeError('source1_external_dependency')
    results = measure(nodes, helpers, log)
    stamp = int(time.time()*1000)
    atomic_json(ROOT/'last-probes.json', {'time_ms': stamp, 'results': results})
    delays = {label: {n: results[n][label]['ms'] if accepted(label, results[n][label]) else None
                     for n in results} for label in TARGETS}
    regions = {n: results.get(n, {}).get('region', '未知') for n in names}
    history_path = ROOT/'gpt-history.json'
    history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else {}
    if history.get('probe_version') != 'GET-robots-openai-v1':
        history = {}
    streaks, passed, history = helpers['advance_gpt_history'](names, delays['chatgpt'], delays['openai'], stamp, history)
    history['probe_version'] = 'GET-robots-openai-v1'
    classifier = lambda area: helpers['group_from_actual_area'](area, include_japan=True)
    candidates = eligible_candidates(names, regions, passed, streaks, delays['chatgpt'], classifier, 2)
    pool = [(n,'飞兔') for n in nodes]
    records = helpers['build_node_records'](pool, delays['grok'], TARGETS['grok'], stamp,
                                          regions, delays['chatgpt'], delays['openai'])
    for record in records:
        row = record['fields']; name = row['节点名']; tests = results.get(name, {})
        row['测速路径'] = LABEL
        row.update(latency_fields(delays['grok'].get(name), delays['chatgpt'].get(name), delays['openai'].get(name)))
        row['Grok可用'] = bool(delays['grok'].get(name))
        row['检测说明'] = ('未完成：本轮预算限制' if not tests else
            f"GET Grok={tests['grok']['status']}; ChatGPT={tests['chatgpt']['status']}; OpenAI={tests['openai']['status']}"
            + ('；Grok验证页' if tests['grok'].get('challenge') else ''))
        if regions[name] != '未知':
            row['地区检测时间'] = stamp
    hold = helpers.get('hold_writer_lock') if helpers else None
    lock_ctx = hold(log) if callable(hold) else contextlib.nullcontext()
    with lock_ctx:
        helpers['ensure_fields'](client, node_table,
            [('Grok可用', helpers['FT_CHECKBOX'], None), ('检测说明', helpers['FT_TEXT'], None)], log)
        ensure_latency_fields(client, node_table, log, helpers)
        saved = replace_scope(client, node_table, records)
        helpers['ensure_fields'](client, region_table,
            [('测速路径', helpers['FT_TEXT'], None), ('地区组', helpers['FT_SINGLE_SELECT'],
             {'options': [{'name': n} for n in ('美国','新加坡','日本')]})], log)
        region_records = helpers['build_region_records'](pool, delays['chatgpt'], {}, stamp, regions, passed, streaks, include_japan=True)
        for record in region_records:
            record['fields']['测速路径'] = LABEL
            record['fields']['代理组'] = '源1独立GPT候选'
        region_saved = replace_scope(client, region_table, region_records)
        if {r['节点名'] for r in region_saved if r.get('GPT可用')} != set(candidates):
            raise RuntimeError('source1_candidates_readback_mismatch')
    atomic_json(history_path, history)
    by_name = {n['name']: n for n in nodes}
    candidate_file = ROOT/'gpt-candidates.private.yaml'
    tmp = candidate_file.with_suffix('.tmp')
    tmp.write_text(yaml.safe_dump({'proxies': [by_name[n] for n in candidates]}, allow_unicode=True), encoding='utf-8')
    tmp.replace(candidate_file)
    report = {'path': LABEL, 'nodes': len(saved), 'alive': sum(bool(r['存活']) for r in saved),
              'grok': sum(bool(r['Grok可用']) for r in saved), 'regions': sum(r['实际地区']!='未知' for r in saved),
              'chatgpt': sum(bool(v) for v in delays['chatgpt'].values()), 'openai': sum(bool(v) for v in delays['openai'].values()),
              'candidates': candidates, 'candidate_regions': {n:classifier(regions[n]) for n in candidates},
              'sync_ms':stamp, 'readback_verified':True}
    atomic_json(ROOT/'last-sync.json', report)
    log.info('source1 independent readback ' + json.dumps(report, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    import runpy
    h = runpy.run_path(str(Path(__file__).with_name('sync-feishu-bitable.py')))
    args = h['parse_args']([])
    log = h['Logger']('C:/mihomo/logs/source1-independent.log')
    # Shared writer mutex is hold_writer_lock inside sync(), which uses
    # writer_mutex_name() / SYNC_MUTEX_NAME. Probes run unlocked.
    env = h['read_env_file'](args.env_file)
    client = h['FeishuClient'](env['FEISHU_APP_ID'], env['FEISHU_APP_SECRET'], args.app_token, log)
    tables = h['load_state'](args.state_file)['tables']
    sync(client, tables['nodes']['table_id'], tables['gpt_regions']['table_id'], log, h)
