#!/usr/bin/env python3
"""
convert_sub.py - Convert anytls/hysteria2/ss/trojan/tuic/vless subscription nodes to mihomo/Clash.Meta format.

Original anytls2mihomo upgraded for mihomo-proxy-management skill.

anytls 协议本质是 VLESS + TLS + TCP + client-fingerprint (TLS 指纹伪装)。
hysteria2 是 QUIC 协议，直接支持。
mihomo / Clash.Meta 不识别 anytls 协议名，但支持 vless + tls + client-fingerprint 参数。
本脚本将 anytls:// 和 hy2:// 节点转换为 mihomo 兼容的配置格式。

支持输入:
  - 本地订阅文件 (base64 编码或纯文本 URI 列表)
  - HTTP/HTTPS 订阅链接
  - 单个 URI

支持输出:
  - Clash/Mihomo YAML 格式 (proxy-provider 可用)
  - 合并到现有 YAML 配置
  - 列出节点

anytls 协议本质是 VLESS + TLS + TCP + client-fingerprint (TLS 指纹伪装)。
mihomo / Clash.Meta 不识别 anytls 协议名，但支持 vless + tls + client-fingerprint 参数。
本脚本将 anytls:// 节点转换为 mihomo 兼容的 VLESS 配置格式。

支持输入:
  - 本地订阅文件 (base64 编码或纯文本 URI 列表)
  - HTTP/HTTPS 订阅链接
  - 单个 anytls:// URI

支持输出:
  - Clash/Mihomo YAML 格式 (proxy-provider 可用)
  - 纯文本 URI 列表 (可用于其他客户端)
  - 合并到现有 YAML 配置

Usage:
  python anytls2mihomo.py -u "https://example.com/sub?token=xxx" -o output.yaml
  python anytls2mihomo.py -i subscription.txt -o proxies.yaml
  python anytls2mihomo.py -u "https://..." --merge existing.yaml -o merged.yaml
"""

from __future__ import annotations

import argparse
import base64
import sys
import urllib.parse
import urllib.request
from typing import List, Dict, Optional

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def fetch_subscription(url: str, timeout: int = 30) -> str:
    """Fetch subscription content from URL."""
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode('utf-8', errors='replace')


def read_file(path: str) -> str:
    """Read subscription file."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.read()


def decode_subscription(content: str) -> List[str]:
    """Decode subscription content (base64 or plain text) into URI list."""
    content = content.strip()
    if not content:
        return []
    
    # Try base64 decode first (standard subscription format)
    try:
        # Add padding if needed
        padding = 4 - len(content) % 4
        if padding != 4:
            content_padded = content + '=' * padding
        else:
            content_padded = content
        decoded = base64.b64decode(content_padded).decode('utf-8', errors='replace')
        lines = [l.strip() for l in decoded.split('\n') if l.strip()]
        # If decoded lines look like URIs, use them
        if lines and any('://' in l for l in lines[:5]):
            return lines
    except Exception:
        pass
    
    # Try plain text
    lines = [l.strip() for l in content.split('\n') if l.strip()]
    if any('://' in l for l in lines[:5]):
        return lines
    
    return []


def parse_anytls_uri(uri: str) -> Optional[Dict]:
    """Parse a single anytls:// URI into a mihomo proxy config dict.
    
    anytls://uuid@server:port/path?type=tcp&insecure=1&fp=chrome&sni=example.com#name
    
    Returns dict with mihomo-compatible keys, or None if parsing fails.
    """
    if not uri.startswith('anytls://'):
        return None
    
    # Extract name
    name = ''
    if '#' in uri:
        url_part, name_encoded = uri.rsplit('#', 1)
        name = urllib.parse.unquote(name_encoded)
    else:
        url_part = uri
    
    body = url_part.replace('anytls://', '')
    
    # Split auth and host
    if '@' in body:
        uuid, host_part = body.split('@', 1)
    else:
        return None  # UUID is required
    
    # Extract query params
    query = ''
    if '?' in host_part:
        host_part, query = host_part.split('?', 1)
    
    # Extract path (some providers include path in host)
    path = '/'
    if '/' in host_part and not host_part.startswith('/'):
        # Only if there's a path after port
        idx = host_part.find('/')
        path = host_part[idx:]
        host_part = host_part[:idx]
    
    # Parse host:port
    if ':' not in host_part:
        return None
    server, port_str = host_part.rsplit(':', 1)
    try:
        port = int(port_str)
    except ValueError:
        return None
    
    # Parse query params
    params = urllib.parse.parse_qs(query) if query else {}
    
    # Extract parameters
    net_type = params.get('type', ['tcp'])[0]
    insecure = params.get('insecure', ['0'])[0] == '1'
    fp = params.get('fp', ['chrome'])[0]  # TLS client fingerprint
    sni = params.get('sni', [server])[0]
    alpn = params.get('alpn', [''])
    if isinstance(alpn, list):
        alpn = alpn[0]
    alpn_list = [a.strip() for a in alpn.split(',') if a.strip()] if alpn else []
    
    # Build mihomo VLESS config
    proxy = {
        'name': name or f'anytls-{server}:{port}',
        'type': 'vless',
        'server': server,
        'port': port,
        'uuid': uuid,
        'network': net_type,
        'tls': True,
        'udp': True,
        'sni': sni,
        'skip-cert-verify': insecure,
        'client-fingerprint': fp,
    }
    
    # ALPN
    if alpn_list:
        proxy['alpn'] = alpn_list
    
    # WS transport
    if net_type == 'ws':
        ws_path = params.get('path', ['/'])[0]
        ws_host = params.get('host', [server])[0]
        proxy['ws-opts'] = {
            'path': ws_path if path == '/' else path,
            'headers': {'Host': ws_host}
        }
        if path != '/':
            proxy['ws-opts']['path'] = path
    
    # Reality
    pbk = params.get('pbk', [''])[0]
    sid = params.get('sid', [''])[0]
    if pbk or sid:
        proxy['reality-opts'] = {
            'public-key': pbk,
            'short-id': sid,
        }
    
    return proxy


def parse_all_uris(uris: List[str]) -> List[Dict]:
    """Parse all URIs, return list of mihomo proxy dicts.
    
    Supports anytls, ss, trojan, tuic, vless, hysteria2 protocols.
    """
    proxies = []
    
    for uri in uris:
        if uri.startswith('anytls://'):
            proxy = parse_anytls_uri(uri)
            if proxy:
                proxies.append(proxy)
        elif uri.startswith('hysteria2://') or uri.startswith('hy2://'):
            # Parse hysteria2 URI
            # Format: hysteria2://auth@server:port?insecure=1&sni=example.com#name
            import urllib.parse
            if '#' in uri:
                url_part, name_encoded = uri.rsplit('#', 1)
                name = urllib.parse.unquote(name_encoded)
            else:
                url_part = uri
                name = ''
            
            scheme_end = url_part.find('://')
            body = url_part[scheme_end+3:] if scheme_end != -1 else url_part
            
            if '@' in body:
                auth, host_part = body.split('@', 1)
            else:
                auth, host_part = '', body
            
            query = ''
            if '?' in host_part:
                host_part, query = host_part.split('?', 1)
            
            if '/' in host_part:
                host_part = host_part.split('/', 1)[0]
            
            if ':' in host_part:
                server, port_str = host_part.rsplit(':', 1)
            else:
                server, port_str = host_part, '443'
            
            try:
                port = int(port_str)
            except ValueError:
                continue
            
            params = urllib.parse.parse_qs(query) if query else {}
            
            proxy = {
                'name': name if name else f'hysteria2-{server}:{port}',
                'type': 'hysteria2',
                'server': server,
                'port': port,
                'password': auth,
                'udp': True,
            }
            
            sni = params.get('sni', [server])[0]
            proxy['sni'] = sni
            
            if params.get('insecure', ['0'])[0] == '1' or params.get('allowInsecure', ['0'])[0] == '1':
                proxy['skip-cert-verify'] = True
            
            alpn = params.get('alpn', [''])[0]
            if alpn:
                proxy['alpn'] = [a.strip() for a in alpn.split(',') if a.strip()]
            
            obfs = params.get('obfs', [''])[0]
            obfs_password = params.get('obfs-password', [''])[0]
            if obfs:
                proxy['obfs'] = obfs
                proxy['obfs-password'] = obfs_password
            
            fast_open = params.get('fast-open', [''])[0]
            if fast_open == '1' or fast_open.lower() == 'true':
                proxy['fast-open'] = True
            
            up_mbps = params.get('up', [''])[0]
            down_mbps = params.get('down', [''])[0]
            if up_mbps and down_mbps:
                proxy['up'] = int(up_mbps)
                proxy['down'] = int(down_mbps)
            
            proxies.append(proxy)
        # TODO: add ss/trojan/tuic/vless parsing support
        # For now, only convert anytls and hysteria2
    
    return proxies


def to_yaml(proxies: List[Dict], as_proxy_provider: bool = True) -> str:
    """Convert proxy list to YAML string.
    
    If as_proxy_provider=True, wraps in {'proxies': [...]} format
    compatible with mihomo proxy-provider.
    """
    if not HAS_YAML:
        # Fallback: simple YAML-like output
        lines = ['proxies:'] if as_proxy_provider else []
        for p in proxies:
            lines.append(f"  - name: {p['name']}")
            lines.append(f"    type: {p['type']}")
            lines.append(f"    server: {p['server']}")
            lines.append(f"    port: {p['port']}")
            for k, v in p.items():
                if k in ('name', 'type', 'server', 'port'):
                    continue
                if isinstance(v, bool):
                    lines.append(f"    {k}: {str(v).lower()}")
                elif isinstance(v, list):
                    lines.append(f"    {k}:")
                    for item in v:
                        lines.append(f"      - {item}")
                elif isinstance(v, dict):
                    lines.append(f"    {k}:")
                    for dk, dv in v.items():
                        if isinstance(dv, dict):
                            lines.append(f"      {dk}:")
                            for ddk, ddv in dv.items():
                                lines.append(f"          {ddk}: {ddv}")
                        else:
                            lines.append(f"      {dk}: {dv}")
                else:
                    lines.append(f"    {k}: {v}")
        return '\n'.join(lines) + '\n'
    
    data = {'proxies': proxies} if as_proxy_provider else proxies
    return yaml.dump(data, allow_unicode=True, sort_keys=False)


def merge_into_yaml(proxies: List[Dict], existing_path: str) -> str:
    """Merge converted proxies into an existing YAML config file.
    
    Adds proxies to the existing proxies list (dedup by name).
    """
    if not HAS_YAML:
        raise ImportError("PyYAML is required for merge mode. Install with: pip install pyyaml")
    
    with open(existing_path, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f)
    
    existing_proxies = data.get('proxies', [])
    existing_names = {p['name'] for p in existing_proxies if isinstance(p, dict)}
    
    added = 0
    for p in proxies:
        if p['name'] not in existing_names:
            existing_proxies.append(p)
            existing_names.add(p['name'])
            added += 1
    
    data['proxies'] = existing_proxies
    print(f"Merged {added} new proxies (total {len(existing_proxies)})", file=sys.stderr)
    
    return yaml.dump(data, allow_unicode=True, sort_keys=False)


def main():
    parser = argparse.ArgumentParser(
        description='Convert anytls:// subscription nodes to mihomo/Clash.Meta VLESS+TLS format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # From URL, output YAML
  python convert_sub.py -u "https://example.com/sub?token=xxx" -o proxies.yaml
  
  # From local file
  python convert_sub.py -i subscription.txt -o proxies.yaml
  
  # Merge into existing config
  python convert_sub.py -u URL --merge config.yaml -o merged.yaml
  
  # Show parsed nodes (no output file)
  python convert_sub.py -u URL --list
  
  # Parse hysteria2 nodes
  python convert_sub.py --uri "hy2://auth@server:port#name" --list

        """
    )
    
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument('-u', '--url', help='Subscription URL')
    input_group.add_argument('-i', '--input', help='Input subscription file')
    input_group.add_argument('--uri', help='Single anytls:// URI')
    
    parser.add_argument('-o', '--output', help='Output YAML file path')
    parser.add_argument('--merge', help='Merge into existing YAML config file')
    parser.add_argument('--list', action='store_true', help='List parsed node names only')
    parser.add_argument('--suffix', default='', 
                        help='Suffix to append to converted node names (default: none, use original names)')
    parser.add_argument('--timeout', type=int, default=30, help='HTTP timeout in seconds (default: 30)')
    parser.add_argument('-q', '--quiet', action='store_true', help='Quiet mode')
    
    args = parser.parse_args()
    
    # Get subscription content
    if args.url:
        if not args.quiet:
            print(f"Fetching subscription from: {args.url}", file=sys.stderr)
        content = fetch_subscription(args.url, args.timeout)
    elif args.input:
        if not args.quiet:
            print(f"Reading from file: {args.input}", file=sys.stderr)
        content = read_file(args.input)
    else:  # --uri
        content = args.uri
    
    # Decode URIs
    uris = decode_subscription(content)
    if not uris and args.uri:
        uris = [args.uri]
    
    if not args.quiet:
        print(f"Parsed {len(uris)} URIs from subscription", file=sys.stderr)
    
    # Parse anytls nodes
    proxies = parse_all_uris(uris)
    
    if not proxies:
        print("No anytls:// nodes found in subscription.", file=sys.stderr)
        print("Supported protocols for conversion: anytls", file=sys.stderr)
        sys.exit(1)
    
    # Apply name suffix if requested
    if args.suffix:
        for p in proxies:
            p['name'] = p['name'] + args.suffix
    
    if not args.quiet:
        print(f"Converted {len(proxies)} anytls nodes to VLESS+TLS format", file=sys.stderr)
        if not args.list:
            for p in proxies[:10]:
                print(f"  - {p['name']} ({p['server']}:{p['port']})", file=sys.stderr)
            if len(proxies) > 10:
                print(f"  ... and {len(proxies)-10} more", file=sys.stderr)
    
    if args.list:
        for p in proxies:
            print(f"[{p['type']}] {p['name']}  {p['server']}:{p['port']}  fp={p.get('client-fingerprint','?')}")
        return
    
    # Generate output
    if args.merge:
        output = merge_into_yaml(proxies, args.merge)
    else:
        output = to_yaml(proxies, as_proxy_provider=True)
    
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        if not args.quiet:
            print(f"\nOutput written to: {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == '__main__':
    main()
