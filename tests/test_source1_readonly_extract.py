import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location('s1extract', Path(__file__).parents[1]/'scripts/extract_source1_readonly.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_complete_client_sample_without_bulk_export():
    data = '''proxies:
  - {name: 美国01-客户端, type: vmess, server: example.invalid, port: 443, uuid: test-id}
  - {name: 日本01-客户端, type: trojan, server: example.invalid, port: 443, password: test}
'''.encode() + b'\0binary'
    sample, count = module.one_node_from_buffer(data)
    assert sample['name'] == '美国01-客户端'
    assert count == 2


def test_incomplete_or_local_nodes_are_not_exported():
    for node in ['{name: 客户端, type: vmess, server: example.invalid, port: 443}',
                 '{name: 客户端, type: trojan, server: 127.0.0.1, port: 443, password: test}',
                 '{name: 客户端, type: trojan, server: example.invalid, port: 443, password: test, dialer-proxy: other}']:
        assert module.one_node_from_buffer(('proxies:\n  - '+node+'\n').encode()) is None


def test_embedded_json_with_binary_tail():
    node = {'name': '美国01-客户端', 'type': 'vmess', 'server': 'example.invalid', 'port': 443, 'uuid': 'test-id'}
    data = b'\x01\x02' + json.dumps({'proxies': [node]}, ensure_ascii=False).encode() + b'\xff\x00tail'
    assert module.one_node_from_buffer(data)[0] == node


def test_alternate_sample_does_not_silently_reuse_us(monkeypatch):
    monkeypatch.setattr(module, 'PREFERRED_REGION', '新加坡')
    nodes = [{'name': region+'01-客户端', 'type': 'ss', 'server': 'example.invalid', 'port': 443, 'password': 'test'}
             for region in ['美国', '新加坡']]
    assert module.select_node({'proxies': nodes})[0]['name'] == '新加坡01-客户端'
    assert module.select_node({'proxies': nodes[:1]}) is None


def test_bulk_exports_all_complete_nodes_not_only_client_labels(monkeypatch):
    monkeypatch.setattr(module, 'EXPORT_ALL', True)
    a = {'name': '美国01-客户端', 'type': 'ss', 'server': 'example.invalid', 'port': 443, 'password': 'test'}
    b = {**a, 'name': '台湾01'}
    incomplete = {'name': 'missing', 'type': 'ss'}
    nodes, count = module.select_node({'proxies': [a, b, a, incomplete]})
    assert nodes == [a, b]
    assert count == 3


def test_bulk_rejects_conflicting_duplicate_names(monkeypatch):
    import pytest
    monkeypatch.setattr(module, 'EXPORT_ALL', True)
    a = {'name': '美国01-客户端', 'type': 'ss', 'server': 'example.invalid', 'port': 443, 'password': 'test'}
    with pytest.raises(RuntimeError, match='conflicting duplicate'):
        module.select_node({'proxies': [a, {**a, 'port': 444}]})


def test_atls_frame_uses_runtime_key_and_nonce():
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key, nonce = bytes(range(32)), bytes(range(16))
    node = {'name': '美国01-客户端', 'type': 'trojan', 'server': 'example.invalid', 'port': 443, 'password': 'test'}
    plaintext = json.dumps({'proxies': [node]}, ensure_ascii=False).encode()
    ciphertext = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor().update(plaintext)
    diagnostic = {'encrypted_frame_markers': 0, 'decrypted_proxy_markers': 0}
    found = module.decrypt_frame_candidates(b'prefixATLS' + nonce + ciphertext, key, b'ATLS', diagnostic)
    assert found[0] == node
    assert diagnostic == {'encrypted_frame_markers': 1, 'decrypted_proxy_markers': 1}
