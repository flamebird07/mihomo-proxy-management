"""Run explicitly from an administrator terminal; read only the source1 core.

Does not inject code, stop a process, change proxies or dump entire memory.
Single-node default; explicit --all saves all complete nodes from the same config.
Files stay outside the repository. No credentials printed.
"""
import argparse
import ctypes
from ctypes import wintypes as w
import json
import hashlib
from pathlib import Path
import re
import time
import yaml

EXPECTED_EXE = Path('C:/Program Files (x86)/wmsxwd/AtlasCore_amd64.exe')
OUTPUT = Path('C:/mihomo/source1-extraction')
PREFERRED_REGION = '美国'
EXPORT_ALL = False


def select_node(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get('proxies'), list):
        return None
    complete = []
    for node in obj['proxies']:
        if not isinstance(node, dict):
            continue
        if not all(node.get(k) for k in ('name', 'type', 'server', 'port')):
            continue
        if not any(node.get(k) for k in ('password', 'uuid', 'auth', 'auth-str', 'private-key')):
            continue
        if str(node['server']).lower() in ('127.0.0.1', 'localhost', '::1') or node.get('dialer-proxy'):
            continue
        complete.append(node)
    client = [n for n in complete if '客户端' in n['name']]
    if client:
        if EXPORT_ALL:
            unique = {}
            for node in complete:
                name = node['name']
                if name in unique and unique[name] != node:
                    raise RuntimeError('conflicting duplicate node names; export refused')
                unique[name] = node
            return list(unique.values()), len(complete)
        if PREFERRED_REGION != '美国':
            preferred = [n for n in client if PREFERRED_REGION in n['name']]
            return (preferred[0], len(complete)) if preferred else None
        return next((n for n in client if '美国' in n['name']), client[0]), len(complete)
    return None


def one_node_from_buffer(data):
    """Recover both JSON proxy arrays and YAML, even inside larger buffers."""
    for match in re.finditer(rb'"proxies"\s*:\s*(?=\[)', data):
        raw = data[match.end():match.end() + 1024 * 1024]
        try:
            # Ignore trailing unrelated heap bytes; raw_decode stops at array end.
            nodes, _ = json.JSONDecoder().raw_decode(raw.decode('utf-8', errors='replace'))
            found = select_node({'proxies': nodes})
            if found:
                return found
        except (ValueError, TypeError):
            pass
    for match in re.finditer(rb'proxies:[ \t]*(?:\r?\n|\[)', data):
        raw = data[match.start():match.start() + 1024 * 1024].split(b'\0', 1)[0]
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError as exc:
            text = raw[:exc.start].decode('utf-8')
        for _ in range(4):
            try:
                obj = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                mark = getattr(exc, 'problem_mark', None)
                if mark is None or mark.line < 3:
                    break
                shorter = '\n'.join(text.splitlines()[:mark.line])
                if shorter == text:
                    break
                text = shorter
                continue
            found = select_node(obj)
            if found:
                return found
            break
    return None


def decrypt_frame_candidates(data, key, magic, diagnostics):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    for match in re.finditer(re.escape(magic), data):
        start = match.start()
        diagnostics['encrypted_frame_markers'] += 1
        if len(data) - start < 52:
            continue
        nonce = data[start + 4:start + 20]
        ciphertext = data[start + 20:start + 20 + 1024 * 1024]
        plaintext = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor().update(ciphertext)
        if b'proxies' not in plaintext:
            continue
        diagnostics['decrypted_proxy_markers'] += 1
        found = one_node_from_buffer(plaintext)
        if found:
            return found
    return None


def collect(pid, max_seconds=180):
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    k.OpenProcess.restype = w.HANDLE
    k.CloseHandle.argtypes = [w.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    k.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

    class MBI(ctypes.Structure):
        _fields_ = [('base', ctypes.c_void_p), ('allocation', ctypes.c_void_p),
                    ('allocation_protect', w.DWORD), ('partition', w.WORD), ('padding', w.WORD),
                    ('size', ctypes.c_size_t), ('state', w.DWORD), ('protect', w.DWORD), ('type', w.DWORD)]
    k.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    k.VirtualQueryEx.restype = ctypes.c_size_t
    handle = k.OpenProcess(0x410, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'source1 core read denied; run explicitly in administrator terminal')
    try:
        path = ctypes.create_unicode_buffer(32768); length = w.DWORD(len(path))
        if not k.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(length)):
            raise RuntimeError('cannot verify target executable')
        if Path(path.value).resolve() != EXPECTED_EXE.resolve():
            raise RuntimeError('target is not source1 AtlasCore; refused')
        diagnostics = {'yaml_markers': 0, 'json_markers': 0, 'client_name_markers': 0,
                       'encrypted_frame_markers': 0, 'decrypted_proxy_markers': 0,
                       'key_ready': False}
        key = None
        layout_path = OUTPUT / 'atlas-read-layout.json'
        if layout_path.exists():
            layout = json.loads(layout_path.read_text(encoding='utf-8'))
            if hashlib.sha256(EXPECTED_EXE.read_bytes()).hexdigest() != layout['exe_sha256']:
                raise RuntimeError('core version changed; static read layout must be rebuilt')
            ps = ctypes.WinDLL('psapi', use_last_error=True)
            ps.EnumProcessModulesEx.argtypes = [w.HANDLE, ctypes.POINTER(ctypes.c_void_p), w.DWORD,
                                               ctypes.POINTER(w.DWORD), w.DWORD]
            modules = (ctypes.c_void_p * 1024)(); needed = w.DWORD()
            if not ps.EnumProcessModulesEx(handle, modules, ctypes.sizeof(modules), ctypes.byref(needed), 3):
                raise OSError(ctypes.get_last_error(), 'cannot locate core module')
            image_base = modules[0]
            def read_exact(address, count):
                buf = ctypes.create_string_buffer(count); done = ctypes.c_size_t()
                if not k.ReadProcessMemory(handle, address, buf, count, ctypes.byref(done)) or done.value != count:
                    raise OSError(ctypes.get_last_error(), 'targeted core read failed')
                return buf.raw
            diagnostics['key_ready'] = read_exact(image_base + layout['ready_rva'], 1) != b'\0'
            if diagnostics['key_ready']:
                key = read_exact(image_base + layout['key_rva'], 32)
            magic = bytes.fromhex(layout['frame_magic_hex'])
        address = 0; scanned = 0; started = time.monotonic()
        while time.monotonic() - started < max_seconds and scanned < 2 * 1024**3:
            mbi = MBI()
            if not k.VirtualQueryEx(handle, address, ctypes.byref(mbi), ctypes.sizeof(mbi)):
                break
            start = mbi.base or 0; end = start + mbi.size
            if end <= address:
                break
            address = end
            if mbi.state != 0x1000 or mbi.type != 0x20000 or mbi.protect & (0x100 | 0x01):
                continue
            overlap = b''
            for offset in range(start, end, 2 * 1024 * 1024):
                if time.monotonic() - started >= max_seconds:
                    break
                size = min(2 * 1024 * 1024, end - offset)
                buffer = ctypes.create_string_buffer(size); read = ctypes.c_size_t()
                if not k.ReadProcessMemory(handle, offset, buffer, size, ctypes.byref(read)):
                    overlap = b''
                    continue
                data = overlap + buffer.raw[:read.value]
                scanned += read.value
                diagnostics['yaml_markers'] += len(re.findall(rb'proxies:\s', data))
                diagnostics['json_markers'] += len(re.findall(rb'"proxies"\s*:', data))
                diagnostics['client_name_markers'] += data.count('客户端'.encode())
                found = one_node_from_buffer(data)
                if not found and key:
                    found = decrypt_frame_candidates(data, key, magic, diagnostics)
                if found:
                    node, count = found
                    OUTPUT.mkdir(parents=True, exist_ok=True)
                    filename = ('all-nodes.private.yaml' if EXPORT_ALL else
                                'single-node.private.yaml' if PREFERRED_REGION == '美国' else
                                f'single-node-{PREFERRED_REGION}.private.yaml')
                    target = OUTPUT / filename
                    exported = node if EXPORT_ALL else [node]
                    with target.open('x', encoding='utf-8') as stream:
                        yaml.safe_dump({'proxies': exported}, stream, allow_unicode=True, sort_keys=False)
                    return {'exported': len(exported), 'observed_complete_nodes': count,
                            'types': sorted({n['type'] for n in exported}),
                            'path': str(target), 'scanned_mb': scanned // 1024**2,
                            'diagnostics': diagnostics}
                overlap = data[-1024 * 1024:]
        return {'exported': 0, 'reason': 'no complete client node recovered from YAML/JSON/ATLS frames',
                'scanned_mb': scanned // 1024**2, 'diagnostics': diagnostics}
    finally:
        k.CloseHandle(handle)


def main():
    global PREFERRED_REGION, EXPORT_ALL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--sample-region', choices=['US', 'SG', 'JP', 'HK'], default='US')
    parser.add_argument('--all', action='store_true', help='Explicitly export all complete nodes, without speed-test gating')
    args = parser.parse_args()
    PREFERRED_REGION = {'US': '美国', 'SG': '新加坡', 'JP': '日本', 'HK': '香港'}[args.sample_region]
    EXPORT_ALL = args.all
    try:
        result = collect(args.pid)
    except Exception as exc:
        result = {'exported': 0, 'error': type(exc).__name__, 'detail': str(exc)}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / 'readonly-extraction-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
