#!/usr/bin/env python3
"""Discover, health-check and publish a stable Xigua entry-IP provider.

The state file never contains proxy credentials.  Credentials are copied only
from the source provider to the generated provider.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover - deployment guard
    raise SystemExit("PyYAML is required (already used by this repository)") from exc


DEFAULT_DOH = (
    "https://dns.google/resolve?name={name}&type=A",
    "https://223.5.5.5/resolve?name={name}&type=A",
)
FAKE_NET = ipaddress.ip_network("198.18.0.0/15")


class AlreadyRunning(RuntimeError):
    pass


class FileLock:
    def __init__(self, path: Path, stale_seconds: int = 900):
        self.path = path
        self.stale_seconds = stale_seconds
        self.token = os.urandom(16).hex()
        self.held = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            error_access_denied = 5
            error_invalid_parameter = 87
            still_active = 259
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k32.GetExitCodeProcess.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.CloseHandle.restype = wintypes.BOOL
            handle = k32.OpenProcess(0x1000, False, pid)
            if not handle:
                err = ctypes.get_last_error()
                # Only "no such process" proves death. ACCESS_DENIED and any
                # other indeterminate failure must be treated as still alive.
                if err == error_invalid_parameter:
                    return False
                if err == error_access_denied:
                    return True
                return True
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return True
                return code.value == still_active
            finally:
                k32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True

    def _read_meta(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        pid = data.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return None
        token = data.get("token")
        if token is not None and not isinstance(token, str):
            return None
        return data

    def _remove_stale(self, meta) -> bool:
        current = self._read_meta()
        if meta is not None and current != meta:
            return False
        if current is not None and self._pid_alive(current["pid"]):
            return False
        try:
            self.path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                meta = self._read_meta()
                if meta is not None and self._pid_alive(meta["pid"]):
                    raise AlreadyRunning("another collector instance is running")
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age <= self.stale_seconds:
                    raise AlreadyRunning("another collector instance is running")
                if not self._remove_stale(meta):
                    raise AlreadyRunning("another collector instance is running")
                continue
            try:
                os.write(
                    fd,
                    json.dumps(
                        {"pid": os.getpid(), "token": self.token, "created": time.time()}
                    ).encode(),
                )
            except OSError:
                os.close(fd)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                raise
            os.close(fd)
            self.held = True
            return self
        raise AlreadyRunning("another collector instance is running")

    def __exit__(self, *_):
        if self.held:
            meta = self._read_meta()
            if meta is not None and meta.get("token") == self.token:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            self.held = False


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def is_public_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.version == 4 and ip not in FAKE_NET and not (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
    )


def doh_lookup(host: str, endpoint: str, timeout: float = 5.0, opener=None) -> set[str]:
    url = endpoint.format(name=urllib.parse.quote(host, safe=""))
    request = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    open_fn = opener or urllib.request.urlopen
    with open_fn(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    found = set()
    for answer in payload.get("Answer", []):
        if int(answer.get("type", 0)) == 1 and is_public_ip(str(answer.get("data", ""))):
            found.add(str(answer["data"]))
    return found


def discover(hosts, endpoints=DEFAULT_DOH, repetitions=4, timeout=5.0, resolver=doh_lookup):
    result = {host: set() for host in hosts}
    errors = 0
    for host in sorted(hosts):
        for endpoint in endpoints:
            for _ in range(repetitions):
                try:
                    result[host].update(
                        ip for ip in resolver(host, endpoint, timeout) if is_public_ip(ip)
                    )
                except Exception:
                    errors += 1
    return result, errors


def _recv_headers(sock, limit=16384):
    data = bytearray()
    while b"\r\n\r\n" not in data and len(data) < limit:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def probe_connect_tls(ip: str, node: dict, proxy_host="127.0.0.1", proxy_port=7890,
                      timeout=5.0, socket_factory=socket.create_connection) -> tuple[bool, float | None, str]:
    started = time.monotonic()
    port = int(node["port"])
    sni = node.get("sni") or node.get("servername") or node.get("server")
    raw = None
    try:
        raw = socket_factory((proxy_host, int(proxy_port)), timeout=timeout)
        raw.settimeout(timeout)
        target = f"{ip}:{port}"
        raw.sendall((f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n").encode("ascii"))
        head = _recv_headers(raw)
        first = head.split(b"\r\n", 1)[0]
        if b" 200 " not in first:
            return False, None, "connect_rejected"
        context = ssl.create_default_context()
        if node.get("skip-cert-verify", False):
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        with context.wrap_socket(raw, server_hostname=str(sni), do_handshake_on_connect=True):
            pass
        raw = None
        return True, round((time.monotonic() - started) * 1000, 1), "ok"
    except (TimeoutError, socket.timeout):
        return False, None, "timeout"
    except ssl.SSLError as exc:
        # A peer TLS alert or certificate-verification failure proves that the
        # target reached TLS. WRONG_VERSION_NUMBER/plaintext does not.
        detail = str(exc).upper()
        if "ALERT" in detail or "CERTIFICATE" in detail:
            return True, round((time.monotonic() - started) * 1000, 1), "tls_response"
        return False, None, "tls_invalid"
    except OSError:
        return False, None, "network_error"
    finally:
        if raw is not None:
            try:
                raw.close()
            except OSError:
                pass


def load_nodes(path: Path) -> list[dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    nodes = data.get("proxies", []) if isinstance(data, dict) else data
    if not isinstance(nodes, list):
        raise ValueError("source provider must contain a proxies list")
    selected = []
    for node in nodes:
        if not isinstance(node, dict) or str(node.get("type", "")).lower() != "trojan":
            continue
        host = str(node.get("server", "")).lower()
        if "西瓜" in str(node.get("name", "")) or host.endswith(".indxg.com"):
            if node.get("port") and (node.get("sni") or node.get("servername")):
                selected.append(node)
    if not selected:
        raise ValueError("no Xigua trojan nodes found")
    return selected


def empty_state():
    return {"schema": 2, "updated_at": None, "hosts": {}}


def read_state(path: Path):
    if not path.exists():
        return empty_state()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != 2 or not isinstance(data.get("hosts"), dict):
        raise ValueError("unsupported state schema; remove v1 state after preserving it for audit")
    return data


def update_host_state(host_state: dict, discovered: set[str],
                      results: dict[str, tuple[bool, float | None, str]], stamp: str):
    records = host_state.setdefault("ips", {})
    for ip in sorted(discovered | set(records)):
        rec = records.setdefault(ip, {
            "first_seen": stamp, "last_seen": None, "success_total": 0,
            "failure_total": 0, "success_streak": 0, "failure_streak": 0,
            "healthy": False, "latency_ms": None, "last_result": "unprobed",
        })
        if ip in discovered:
            rec["last_seen"] = stamp
        if ip not in results:
            continue
        ok, latency, reason = results[ip]
        rec["last_result"] = reason
        if ok:
            rec["success_total"] += 1
            rec["success_streak"] += 1
            rec["failure_streak"] = 0
            rec["latency_ms"] = latency
            if rec["success_streak"] >= 2:
                rec["healthy"] = True
        else:
            rec["failure_total"] += 1
            rec["failure_streak"] += 1
            rec["success_streak"] = 0
            if rec["failure_streak"] >= 3:
                rec["healthy"] = False
    healthy = [ip for ip, rec in records.items() if rec.get("healthy")]
    host_state["zero_healthy_rounds"] = 0 if healthy else host_state.get("zero_healthy_rounds", 0) + 1
    return sorted(healthy, key=lambda ip: (records[ip].get("latency_ms") is None,
                                            records[ip].get("latency_ms") or 10**9, ip))


def build_provider(nodes: list[dict], healthy_by_host: dict[str, list[str]], max_per_node=3):
    proxies = []
    for node in nodes:
        host = str(node.get("server", ""))
        for ip in healthy_by_host.get(host, [])[:max_per_node]:
            item = copy.deepcopy(node)
            item["server"] = ip
            item["name"] = f"{node.get('name', '西瓜')}@{host}@{ip}"
            proxies.append(item)
    return {"proxies": proxies}


def yaml_bytes(data) -> bytes:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False).encode("utf-8")


def atomic_write_if_changed(path: Path, content: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == content:
        return False
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return True


def validate_candidate(command_json: str, candidate: Path) -> tuple[bool, str]:
    try:
        argv = json.loads(command_json)
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
            return False, "validation command must be a JSON string array"
        if not any("{provider}" in item for item in argv):
            return False, "validation command must reference {provider}"
        argv = [x.replace("{provider}", str(candidate)) for x in argv]
        completed = subprocess.run(argv, shell=False, capture_output=True, timeout=60)
        return completed.returncode == 0, f"validator_exit_{completed.returncode}"
    except Exception as exc:
        return False, f"validator_error_{type(exc).__name__}"


def run(args, resolver=doh_lookup, prober=probe_connect_tls):
    source = Path(args.source_provider).resolve()
    state_path = Path(args.state).resolve()
    output = Path(args.output_provider).resolve()
    with FileLock(Path(args.lock).resolve(), args.lock_stale_seconds):
        nodes = load_nodes(source)
        hosts = {str(n["server"]) for n in nodes}
        found, dns_errors = discover(hosts, args.doh_endpoint, args.doh_repetitions,
                                     args.timeout, resolver)
        state = read_state(state_path)
        nodes_by_host = {}
        for item in nodes:
            nodes_by_host.setdefault(str(item["server"]), []).append(item)
        healthy_by_host = {}
        stamp = now_iso()
        probe_index = 0
        for host in sorted(nodes_by_host):
            host_state = state["hosts"].setdefault(host, {"zero_healthy_rounds": 0, "ips": {}})
            discovered = set(found.get(host, set()))
            candidates = sorted(discovered | set(host_state.get("ips", {})))
            representative = sorted(nodes_by_host[host],
                                    key=lambda n: (int(n["port"]), str(n.get("name", ""))))[0]
            results = {}
            for ip in candidates:
                if probe_index and args.probe_interval > 0:
                    time.sleep(args.probe_interval)
                results[ip] = prober(ip, representative, args.proxy_host, args.proxy_port, args.timeout)
                probe_index += 1
            healthy_by_host[host] = update_host_state(host_state, discovered, results, stamp)
        state["updated_at"] = stamp
        atomic_write_if_changed(state_path, json.dumps(state, ensure_ascii=False, indent=2,
                                                        sort_keys=True).encode("utf-8") + b"\n")
        published = False
        reason = "no_mature_ip_keep_previous"
        healthy_count = sum(len(values) for values in healthy_by_host.values())
        discovered_count = sum(len(values) for values in found.values())
        if healthy_count:
            content = yaml_bytes(build_provider(nodes, healthy_by_host, args.max_ips_per_node))
            if args.mode == "production":
                if not args.validate_command:
                    reason = "production_validation_required"
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    fd, candidate_name = tempfile.mkstemp(prefix="xigua-candidate-", suffix=".yaml",
                                                           dir=str(output.parent))
                    os.close(fd)
                    candidate = Path(candidate_name)
                    try:
                        candidate.write_bytes(content)
                        valid, reason = validate_candidate(args.validate_command, candidate)
                        if valid:
                            published = atomic_write_if_changed(output, content)
                            reason = "published" if published else "unchanged"
                    finally:
                        candidate.unlink(missing_ok=True)
            else:
                published = atomic_write_if_changed(output, content)
                reason = "staged" if published else "unchanged"
        publish_failed = args.mode == "production" and reason not in ("published", "unchanged")
        summary = {
            "status": "error" if publish_failed else ("ok" if healthy_count else "degraded"),
            "mode": args.mode,
            "discovered_ips": discovered_count,
            "discovered_hosts": sum(bool(values) for values in found.values()),
            "healthy_ips": healthy_count,
            "healthy_hosts": sum(bool(values) for values in healthy_by_host.values()),
            "dns_errors": dns_errors,
            "published": published,
            "reason": reason,
            "alert": any(
                host_state.get("zero_healthy_rounds", 0) >= 3
                for host_state in state["hosts"].values()
            ),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 4 if publish_failed else (0 if healthy_count else 2)


def parser():
    p = argparse.ArgumentParser(description="Xigua dynamic entry-IP collector")
    p.add_argument("--source-provider", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--output-provider", required=True)
    p.add_argument("--lock", required=True)
    p.add_argument("--mode", choices=("dry-run", "production"), default="dry-run")
    p.add_argument("--validate-command", help='JSON argv; use "{provider}" for candidate path')
    p.add_argument("--doh-endpoint", action="append", default=[])
    p.add_argument("--doh-repetitions", type=int, default=4)
    p.add_argument("--proxy-host", default="127.0.0.1")
    p.add_argument("--proxy-port", type=int, default=7890)
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--probe-interval", type=float, default=0.1)
    p.add_argument("--max-ips-per-node", type=int, default=3)
    p.add_argument("--lock-stale-seconds", type=int, default=900)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.doh_endpoint:
        args.doh_endpoint = list(DEFAULT_DOH)
    try:
        return run(args)
    except AlreadyRunning as exc:
        print(json.dumps({"status": "busy", "reason": str(exc)}), file=sys.stderr)
        return 3
    except Exception as exc:
        print(json.dumps({"status": "error", "reason": type(exc).__name__}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
