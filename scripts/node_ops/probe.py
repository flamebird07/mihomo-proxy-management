"""Health, latency and egress-region probing through an isolated mihomo core.

An isolated core is started on free local ports from a temporary runtime
directory.  Nothing touches the production controller (:9090), the source-3
bridge or port 3443.  The temporary runtime directory holds only a throwaway
core config; no node cache is written locally.

Probe targets (the project contract):
  * grok    - https://grok.com/
  * chatgpt - https://chatgpt.com/cdn-cgi/trace
  * openai  - https://api.openai.com

Egress region is measured through the node via ipinfo.io/json (fallbacks:
api.ip.sb, ipapi.co, cloudflare trace), normalized to 中文 国家/地区/城市.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

from .regions import format_cf_trace, format_geo_payload


GROK_TARGET = "https://grok.com/"
CHATGPT_TARGET = "https://chatgpt.com/cdn-cgi/trace"
OPENAI_TARGET = "https://api.openai.com"

DEFAULT_TARGETS: Dict[str, str] = {
    "grok": GROK_TARGET,
    "chatgpt": CHATGPT_TARGET,
    "openai": OPENAI_TARGET,
}

MAX_WORKERS = 4
DEFAULT_TOTAL_BUDGET_S = 600.0
IPINFO_TIMEOUT_S = 5.0
IPINFO_BUDGET_S = 600.0
GEO_ENDPOINTS = (
    "https://api.ip.sb/geoip",
    "https://ipapi.co/json/",
    "https://ipinfo.io/json",
)

CORE_ENV = "NODE_OPS_MIHOMO_CORE"


class ProbeError(Exception):
    def __init__(self, message: str, kind: str = "probe") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class ProbeResult:
    delays: Dict[str, Optional[int]] = field(default_factory=dict)
    chatgpt: Dict[str, Optional[int]] = field(default_factory=dict)
    openai: Dict[str, Optional[int]] = field(default_factory=dict)
    regions: Dict[str, str] = field(default_factory=dict)


class _QuotedString(str):
    pass


_NUMERIC_LIKE_STR = re.compile(
    r"^(?:[-+]?(?:\d[\d_]*(?:\.[\d_]*)?|\.[\d_]+)(?:[eE][-+]?\d+)?"
    r"|0[xXbBoO][0-9a-fA-F_]+)$"
)


def _quote_numeric_like(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _quote_numeric_like(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_quote_numeric_like(v) for v in value]
    if isinstance(value, str) and not isinstance(value, _QuotedString) \
            and _NUMERIC_LIKE_STR.match(value):
        return _QuotedString(value)
    return value


if yaml is not None:
    yaml.add_representer(
        _QuotedString,
        lambda dumper, data: dumper.represent_scalar(
            "tag:yaml.org,2002:str", str(data), style='"'
        ),
        Dumper=yaml.SafeDumper,
    )


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_ports(mixed_hint: int, controller_hint: int) -> tuple[int, int]:
    for base in range(0, 5000, 3):
        mixed = mixed_hint + base
        controller = controller_hint + base
        if mixed < 65536 and controller < 65536 and _port_free(mixed) and _port_free(controller):
            return mixed, controller
    raise ProbeError("no_free_ports")


def request_delay(api: str, name: str, target: str, timeout_ms: int,
                  expected: Optional[str] = None) -> Optional[int]:
    query_values = {"url": target, "timeout": str(timeout_ms)}
    if expected:
        query_values["expected"] = expected
    endpoint = (
        f"{api}/proxies/{urllib.parse.quote(name, safe='')}/delay?"
        f"{urllib.parse.urlencode(query_values)}"
    )
    try:
        with urllib.request.urlopen(endpoint, timeout=(timeout_ms / 1000) + 5) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        value = int(payload.get("delay") or 0)
        return value if value > 0 else None
    except Exception:
        return None


def test_delay_targets(api: str, names: List[str], targets: Dict[str, str],
                       timeout_ms: int, workers: int = MAX_WORKERS,
                       budget_s: float = DEFAULT_TOTAL_BUDGET_S) -> Dict[str, Dict[str, Optional[int]]]:
    safe_workers = max(1, min(int(workers), MAX_WORKERS))
    results = {label: {name: None for name in names} for label in targets}
    if not names or not targets or budget_s <= 0:
        return results
    tasks = [(name, label, target) for name in names for label, target in targets.items()]
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=safe_workers)
    futures = {
        executor.submit(
            request_delay, api, name, target, timeout_ms,
            "200-299/421" if label == "openai" else None,
        ): (name, label)
        for name, label, target in tasks
    }
    reserve_s = (max(1, timeout_ms) / 1000.0) + 6.0
    done, pending = concurrent.futures.wait(futures, timeout=max(0.0, budget_s - reserve_s))
    for future in done:
        name, label = futures[future]
        try:
            results[label][name] = future.result()
        except Exception:
            results[label][name] = None
    for future in pending:
        future.cancel()
    executor.shutdown(wait=True, cancel_futures=True)
    return results


def select_proxy(api: str, group: str, name: str) -> bool:
    body = json.dumps({"name": name}).encode("utf-8")
    request = urllib.request.Request(
        f"{api}/proxies/{urllib.parse.quote(group, safe='')}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            response.read()
        return True
    except Exception:
        return False


@contextlib.contextmanager
def _force_proxy_env():
    keys = ("NO_PROXY", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
            "http_proxy", "https_proxy", "all_proxy")
    saved = {key: os.environ.pop(key, None) for key in keys}
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _mixed_opener(mixed_port: int) -> urllib.request.OpenerDirector:
    proxy = f"http://127.0.0.1:{mixed_port}"
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
    )


def fetch_geo_via_mixed(mixed_port: int, timeout_s: float = IPINFO_TIMEOUT_S) -> str:
    opener = _mixed_opener(mixed_port)
    headers = {"User-Agent": "node-ops/geo", "Accept": "application/json"}
    with _force_proxy_env():
        for url in GEO_ENDPOINTS:
            try:
                request = urllib.request.Request(url, headers=headers)
                with opener.open(request, timeout=timeout_s) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                if isinstance(payload, dict):
                    text = format_geo_payload(payload)
                    if text:
                        return text
            except Exception:
                continue
        try:
            request = urllib.request.Request(
                "https://cloudflare.com/cdn-cgi/trace",
                headers={"User-Agent": "node-ops/geo"},
            )
            with opener.open(request, timeout=timeout_s) as response:
                text = format_cf_trace(response.read().decode("utf-8", "replace"))
            if text:
                return text
        except Exception:
            pass
    return "未知"


def _probe_region_once(api: str, mixed_port: int, name: str,
                       timeout_s: float = IPINFO_TIMEOUT_S) -> str:
    if not select_proxy(api, "TEST", name):
        return "未知"
    try:
        with urllib.request.urlopen(
            f"{api}/proxies/{urllib.parse.quote('TEST', safe='')}", timeout=3
        ) as response:
            now = json.loads(response.read().decode("utf-8", "replace")).get("now")
        if now != name:
            return "未知"
    except Exception:
        return "未知"
    return fetch_geo_via_mixed(mixed_port, timeout_s=timeout_s)


def lookup_regions(api: str, mixed_port: int, names: List[str],
                   timeout_s: float = IPINFO_TIMEOUT_S,
                   budget_s: float = IPINFO_BUDGET_S) -> Dict[str, str]:
    regions: Dict[str, str] = {}
    started = time.monotonic()
    for name in names:
        if time.monotonic() - started > budget_s:
            for rest in names[len(regions):]:
                regions.setdefault(rest, "未知")
            break
        regions[name] = _probe_region_once(api, mixed_port, name, timeout_s)
    for name in names:
        regions.setdefault(name, "未知")
    return regions


class IsolatedProber:
    """Start a throwaway mihomo core and probe all nodes through it."""

    def __init__(self, core: str, timeout_ms: int = 5000,
                 workers: int = MAX_WORKERS,
                 total_budget_s: float = DEFAULT_TOTAL_BUDGET_S,
                 ipinfo_budget_s: float = IPINFO_BUDGET_S,
                 targets: Optional[Dict[str, str]] = None,
                 mixed_hint: int = 17890, controller_hint: int = 17891) -> None:
        self.core = core
        self.timeout_ms = timeout_ms
        self.workers = max(1, min(int(workers), MAX_WORKERS))
        self.total_budget_s = total_budget_s
        self.ipinfo_budget_s = ipinfo_budget_s
        self.targets = dict(targets or DEFAULT_TARGETS)
        self.mixed_hint = mixed_hint
        self.controller_hint = controller_hint

    def probe(self, nodes: List[Dict[str, Any]],
              names: Optional[List[str]] = None) -> ProbeResult:
        if yaml is None:
            raise ProbeError("pyyaml_missing")
        if not Path(self.core).exists():
            raise ProbeError("core_missing")
        names = names or [str(node.get("name")) for node in nodes]
        if len(names) != len(nodes):
            raise ProbeError("name_count_mismatch", "usage")
        mixed, controller = pick_ports(self.mixed_hint, self.controller_hint)
        runtime = Path(tempfile.mkdtemp(prefix="node_ops-"))
        try:
            config = {
                "mixed-port": mixed,
                "allow-lan": False,
                "mode": "rule",
                "log-level": "warning",
                "ipv6": False,
                "external-controller": f"127.0.0.1:{controller}",
                "dns": {"enable": True, "enhanced-mode": "redir-host",
                        "nameserver": ["8.8.8.8", "1.1.1.1"]},
                "tun": {"enable": False},
                "proxies": _quote_numeric_like(nodes),
                "proxy-groups": [{"name": "TEST", "type": "select", "proxies": names}],
                "rules": ["MATCH,TEST"],
            }
            config_path = runtime / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            process = subprocess.Popen(
                [self.core, "-d", str(runtime), "-f", str(config_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            api = f"http://127.0.0.1:{controller}"
            try:
                deadline = time.monotonic() + 20
                ready = False
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise ProbeError("core_exited")
                    try:
                        with urllib.request.urlopen(f"{api}/version", timeout=1):
                            ready = True
                            break
                    except Exception:
                        time.sleep(0.3)
                if not ready:
                    raise ProbeError("core_not_ready")
                values = test_delay_targets(
                    api, names, self.targets, self.timeout_ms,
                    self.workers, self.total_budget_s,
                )
                regions = lookup_regions(
                    api, mixed, names,
                    timeout_s=min(IPINFO_TIMEOUT_S, max(1.0, self.timeout_ms / 1000.0)),
                    budget_s=self.ipinfo_budget_s,
                )
                return ProbeResult(
                    delays=values.get("grok", {}),
                    chatgpt=values.get("chatgpt", {}),
                    openai=values.get("openai", {}),
                    regions=regions,
                )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        finally:
            shutil.rmtree(runtime, ignore_errors=True)


def default_core_path() -> str:
    env = os.environ.get(CORE_ENV, "").strip()
    if env:
        return env
    return r"C:\mihomo\mihomo.exe"
