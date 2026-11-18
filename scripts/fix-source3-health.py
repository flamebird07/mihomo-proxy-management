"""Change only main GPT's HEAD health target, preserving active connections."""
import json
from pathlib import Path
import runpy
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import yaml

def main():
    root = Path('C:/mihomo/source3-compatible')
    path = Path('C:/mihomo/config.yaml')
    module = runpy.run_path(str(Path(__file__).with_name('proxy-auto-switch.py')))
    env = module['read_env_file']('C:/mihomo-proxy-management/config/subscription.env')
    def call(method, route, body=None):
        return module['api_json'](method, 'http://127.0.0.1:9090' + route, body, env.get('SECRET'), timeout=30)
    config = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    before = call('GET', '/proxies')['proxies']
    selected = {n:p['now'] for n,p in before.items() if p.get('type') in ('Selector','Fallback') and p.get('now')}
    group = next(g for g in config['proxy-groups'] if g['name'] == 'GPT')
    group['url'] = module['DEFAULT_TARGET']
    group['expected-status'] = 200
    candidate = root / 'main-health-next.yaml'
    candidate.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding='utf-8')
    result = subprocess.run(['C:/mihomo/mihomo.exe', '-t', '-d', 'C:/mihomo', '-f', str(candidate)],
        capture_output=True, timeout=40, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeError('config_validation_failed')
    backup = root / ('main-health-before-' + time.strftime('%Y%m%d-%H%M%S') + '.yaml')
    shutil.copy2(path, backup)
    for name, current in selected.items():
        call('PUT', '/proxies/' + urllib.parse.quote(name, safe=''), {'name': current})
    server = socket.socket(); server.bind(('127.0.0.1',0)); server.listen()
    def echo():
        c,_ = server.accept()
        with c:
            while True:
                d=c.recv(1024)
                if not d: break
                c.sendall(d)
    threading.Thread(target=echo, daemon=True).start()
    held=socket.create_connection(('127.0.0.1',7890),timeout=15)
    held.sendall(f'CONNECT 127.0.0.1:{server.getsockname()[1]} HTTP/1.1\r\n\r\n'.encode())
    response=b''
    while b'\r\n\r\n' not in response:
        data=held.recv(4096)
        if not data: raise RuntimeError('hold_failed')
        response+=data
    if b'200' not in response.split(b'\r\n')[0]: raise RuntimeError('hold_rejected')
    held.sendall(b'before'); assert held.recv(6)==b'before'
    old_ids={c['id'] for c in call('GET','/connections').get('connections',[])}
    shutil.copy2(candidate,path)
    try:
        call('PUT','/configs?force=false',{'path':str(path)})
        after=call('GET','/proxies')['proxies']
        if any(after[n].get('now')!=v for n,v in selected.items()): raise RuntimeError('selection_changed')
        held.sendall(b'after'); assert held.recv(5)==b'after'
        new_ids={c['id'] for c in call('GET','/connections').get('connections',[])}
        report={'target':group['url'],'interval':group['interval'],'selection_unchanged':True,
            'held_connection_survived':True,'connections_before':len(old_ids),
            'retained':len(old_ids & new_ids),'backup':str(backup),'applied_ms':int(time.time()*1000)}
        (root/'health-fix-apply.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))
    except Exception:
        shutil.copy2(backup,path); call('PUT','/configs?force=false',{'path':str(path)})
        raise
    finally:
        held.close();server.close()

if __name__=='__main__': main()
