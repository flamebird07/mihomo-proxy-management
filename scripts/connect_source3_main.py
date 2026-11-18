"""Add the standalone bridge without changing current group selections."""
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
import yaml

ROOT = Path('C:/mihomo/source3-compatible')
CONFIG = Path('C:/mihomo/config.yaml')
BRIDGE = '源3独立GPT'


def main():
    config = yaml.safe_load(CONFIG.read_text(encoding='utf-8-sig'))
    api = 'http://' + config['external-controller']
    def call(path, body=None):
        request = urllib.request.Request(api + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + config.get('secret', ''), 'Content-Type': 'application/json'},
            method='PUT' if body is not None else 'GET')
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=30) as r:
            return json.loads(r.read() or b'{}')
    before = call('/proxies')['proxies']
    selected = {n:p['now'] for n,p in before.items()
                if p.get('type') in ('Selector', 'Fallback') and p.get('now')}
    if BRIDGE in before:
        raise RuntimeError('already_loaded: inspect rather than reloading again')
    if not config.get('profile', {}).get('store-selected'):
        raise RuntimeError('selection_persistence_required')
    config.setdefault('proxies', []).append({'name': BRIDGE, 'type': 'socks5',
        'server': '127.0.0.1', 'port': 17993, 'udp': False})
    group = next(g for g in config['proxy-groups'] if g['name'] == 'GPT')
    group['proxies'].append(BRIDGE)
    # A separate inbound in the SAME main core allows explicit verification,
    # while regular requests still use their existing rules and selections.
    config.setdefault('listeners', []).append({'name': 'source3-main-verification',
        'type': 'mixed', 'listen': '127.0.0.1', 'port': 17995, 'proxy': BRIDGE})
    candidate = CONFIG.with_name('config.source3-next.yaml')
    candidate.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
    test = subprocess.run(['C:/mihomo/mihomo.exe', '-t', '-d', 'C:/mihomo', '-f', str(candidate)],
        capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=40)
    if test.returncode:
        raise RuntimeError('main_config_validation_failed')
    backup = ROOT / ('main-config-before-' + time.strftime('%Y%m%d-%H%M%S') + '.yaml')
    shutil.copy2(CONFIG, backup)
    # Seed the existing persistent selection with the SAME node, before reload.
    for name, current in selected.items():
        call('/proxies/' + urllib.parse.quote(name, safe=''), {'name': current})
    check = call('/proxies')['proxies']
    if any(check[n].get('now') != v for n,v in selected.items()):
        raise RuntimeError('preload_selection_changed: reload_aborted')
    # Hold an actual connection through the main inbound across the reload.
    server = socket.socket(); server.bind(('127.0.0.1', 0)); server.listen()
    def echo():
        conn,_ = server.accept()
        with conn:
            while True:
                data = conn.recv(1024)
                if not data: break
                conn.sendall(data)
    threading.Thread(target=echo, daemon=True).start()
    held = socket.create_connection(('127.0.0.1', config['mixed-port']), timeout=15)
    port = server.getsockname()[1]
    held.sendall(f'CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n'.encode())
    response = b''
    while b'\r\n\r\n' not in response:
        chunk = held.recv(4096)
        if not chunk: raise RuntimeError('hold_connection_failed')
        response += chunk
    if b'200' not in response.split(b'\r\n')[0]:
        raise RuntimeError('hold_connection_rejected')
    held.sendall(b'before'); assert held.recv(6) == b'before'
    before_ids = {c['id'] for c in call('/connections').get('connections', [])}
    shutil.copy2(candidate, CONFIG)
    try:
        call('/configs?force=false', {'path': str(CONFIG)})
        after = call('/proxies')['proxies']
        if any(after[n].get('now') != v for n,v in selected.items()):
            raise RuntimeError('postload_selection_mismatch')
        assert BRIDGE in after['GPT']['all']
        held.sendall(b'after'); assert held.recv(5) == b'after'
        after_ids = {c['id'] for c in call('/connections').get('connections', [])}
        report = {'bridge_in_main_gpt': True, 'selections_unchanged': True,
            'held_connection_survived': True, 'connections_before': len(before_ids),
            'same_connections_after': len(before_ids & after_ids), 'main_gpt': after['GPT']['now'],
            'verification_port': 17995, 'backup': str(backup)}
        (ROOT / 'main-bridge-install.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    except Exception:
        shutil.copy2(backup, CONFIG)
        call('/configs?force=false', {'path': str(CONFIG)})
        raise
    finally:
        held.close(); server.close()


if __name__ == '__main__':
    main()
