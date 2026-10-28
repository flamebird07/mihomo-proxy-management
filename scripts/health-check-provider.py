#!/usr/bin/env python3
"""Build an atomically published provider from nodes healthy on every target."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import yaml


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--core", type=Path, default=Path(r"C:\mihomo\mihomo.exe"))
    parser.add_argument("--target", action="append", required=True)
    parser.add_argument("--required-name-regex", default=r"(?i)(美国|美國|\bUS\b|United States)")
    parser.add_argument("--minimum-matching", type=int, default=1)
    parser.add_argument("--mixed-port", type=int, default=17890)
    parser.add_argument("--controller-port", type=int, default=19090)
    parser.add_argument("--timeout-ms", type=int, default=7000)
    parser.add_argument("--workers", type=int, default=12)
    return parser.parse_args()


def port_is_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def request_delay(api: str, name: str, target: str, timeout_ms: int) -> int | None:
    query = urllib.parse.urlencode({"url": target, "timeout": str(timeout_ms)})
    endpoint = f"{api}/proxies/{urllib.parse.quote(name, safe='')}/delay?{query}"
    try:
        with urllib.request.urlopen(endpoint, timeout=(timeout_ms / 1000) + 3) as response:
            value = int(json.loads(response.read().decode("utf-8", "replace")).get("delay", 0))
        return value if value > 0 else None
    except Exception:
        return None


def wait_ready(api: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("isolated Mihomo exited before becoming ready")
        try:
            with urllib.request.urlopen(f"{api}/version", timeout=1):
                return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("isolated Mihomo controller did not become ready")


def atomic_write_yaml(path: Path, document: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False, width=120)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.replace(temporary, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    args = parse_args()
    if not port_is_free(args.mixed_port) or not port_is_free(args.controller_port):
        raise SystemExit("isolated Mihomo ports are already in use")
    document = yaml.safe_load(args.candidate.read_text(encoding="utf-8"))
    proxies = document.get("proxies", []) if isinstance(document, dict) else []
    proxies = [item for item in proxies if isinstance(item, dict) and item.get("name")]
    if not proxies:
        raise SystemExit("candidate contains no proxies")

    names = [item["name"] for item in proxies]
    api = f"http://127.0.0.1:{args.controller_port}"
    with tempfile.TemporaryDirectory(prefix="mihomo-health-") as runtime_name:
        runtime = Path(runtime_name)
        config_path = runtime / "config.yaml"
        config = {
            "mixed-port": args.mixed_port,
            "allow-lan": False,
            "mode": "global",
            "log-level": "warning",
            "ipv6": False,
            "external-controller": f"127.0.0.1:{args.controller_port}",
            "dns": {"enable": False},
            "tun": {"enable": False},
            "proxies": proxies,
            "proxy-groups": [{"name": "TEST", "type": "select", "proxies": names}],
            "rules": ["MATCH,TEST"],
        }
        config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [str(args.core), "-d", str(runtime), "-f", str(config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        try:
            wait_ready(api, process)
            survivors = set(names)
            target_counts: dict[str, int] = {}
            for target in args.target:
                with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                    delays = dict(
                        zip(
                            names,
                            pool.map(
                                lambda name: request_delay(api, name, target, args.timeout_ms),
                                names,
                            ),
                        )
                    )
                passed = {name for name, delay in delays.items() if delay is not None}
                target_counts[urllib.parse.urlsplit(target).hostname or "target"] = len(passed)
                survivors &= passed
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    healthy = [item for item in proxies if item["name"] in survivors]
    required = re.compile(args.required_name_regex)
    matching = [item for item in healthy if required.search(item["name"])]
    if len(matching) < args.minimum_matching:
        raise SystemExit(
            f"publish blocked: matching healthy nodes {len(matching)} < {args.minimum_matching}"
        )
    digest = atomic_write_yaml(args.output, {"proxies": healthy})
    print(
        json.dumps(
            {
                "tested": len(proxies),
                "healthy_all_targets": len(healthy),
                "matching_pool": len(matching),
                "targets": target_counts,
                "output_sha256": digest,
                "isolated_mihomo_stopped": process.poll() is not None,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
