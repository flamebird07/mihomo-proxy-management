"""Read-only isolated check of one exported source1 node; no live proxy changes."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile
import time
import urllib.request
import yaml


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node-file', default='single-node.private.yaml')
    args = parser.parse_args()
    root = Path('C:/mihomo/source1-extraction')
    node_path = root/args.node_file
    nodes = yaml.safe_load(node_path.read_text(encoding='utf-8'))['proxies']
    if len(nodes) != 1:
        raise RuntimeError('single node required')
    helper = runpy.run_path(str(Path(__file__).with_name('sync-feishu-bitable.py')))
    mixed, controller = helper['pick_ports'](17996, 19996)
    runtime = Path(tempfile.mkdtemp(prefix='source1-isolated-'))
    config = {'mixed-port': mixed, 'allow-lan': False, 'bind-address': '127.0.0.1',
              'mode': 'rule', 'log-level': 'warning', 'ipv6': False,
              'external-controller': f'127.0.0.1:{controller}', 'tun': {'enable': False},
              'proxies': nodes, 'rules': ['MATCH,' + nodes[0]['name']]}
    config_path = runtime/'config.yaml'
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
    process = subprocess.Popen(['C:/mihomo/mihomo.exe', '-d', str(runtime), '-f', str(config_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    result = {'sample': nodes[0]['name'], 'core': 'standard Mihomo', 'tests': {}}
    try:
        for _ in range(40):
            if process.poll() is not None:
                raise RuntimeError('isolated core exited')
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{controller}/version', timeout=1) as response:
                    result['version'] = json.load(response)
                break
            except Exception:
                time.sleep(.25)
        else:
            raise RuntimeError('isolated core not ready')
        for label, url, head in [('grok', 'https://grok.com/', False),
                                 ('chatgpt', 'https://chatgpt.com/cdn-cgi/trace', False),
                                 ('gpt_health', 'https://chatgpt.com/robots.txt', True)]:
            header_path = runtime/(label + '.headers')
            args = ['curl.exe', '-q', '--silent', '--noproxy', '', '--proxy', f'socks5h://127.0.0.1:{mixed}',
                    '--max-time', '20', '-o', 'NUL', '-D', str(header_path), '-w', '%{http_code}']
            if head:
                args.append('--head')
            p = subprocess.run(args+[url], capture_output=True, text=True)
            headers = header_path.read_text(errors='replace') if header_path.exists() else ''
            result['tests'][label] = {'exit': p.returncode, 'http': p.stdout.strip(),
                'cloudflare_challenge': 'cf-mitigated: challenge' in headers.lower()}
        result['region'] = helper['fetch_geo_via_ninja_socks'](mixed)
        # Clean browser context, with an explicit isolated proxy and no account login.
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, proxy={'server': f'http://127.0.0.1:{mixed}'})
            try:
                page = browser.new_page()
                response = page.goto('https://grok.com/', wait_until='domcontentloaded', timeout=45000)
                page.wait_for_timeout(3000)
                result['browser_grok'] = {'http': response.status if response else None,
                    'title': page.title(), 'challenge': bool(page.get_by_text('Verify you are human', exact=False).count())}
            except Exception as exc:
                result['browser_grok'] = {'error': type(exc).__name__}
            finally:
                browser.close()
        result['passed'] = (result['tests']['chatgpt']['http'] == '200'
            and result['tests']['gpt_health']['http'] == '200' and result['region'] != '未知'
            and (result['tests']['grok']['http'] == '200' or
                 (result.get('browser_grok', {}).get('http') == 200 and not result['browser_grok'].get('challenge'))))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        shutil.rmtree(runtime)
        result['finished_ms'] = int(time.time()*1000)
        report_name = node_path.name.replace('.private.yaml', '-validation-browser.json')
        (root/report_name).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
