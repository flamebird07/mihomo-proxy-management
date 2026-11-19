#!/usr/bin/env python3
"""Run the Xigua IP pool workflow on macOS without PowerShell."""

from __future__ import annotations

import argparse
import fcntl
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit


def project_core_owns_port(port: int, binary: Path | None) -> bool:
    if binary is None:
        return False
    try:
        service = subprocess.run(
            ["/bin/launchctl", "print", "system/com.mihomo-proxy-management.core"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        pid = re.search(r"(?m)^\s*pid = (\d+)\s*$", service.stdout)
        if not pid:
            return False
        listeners = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        owners = set(listeners.stdout.splitlines())
        if len(owners) != 1:
            return False
        owner = re.fullmatch(r"p(\d+)", owners.pop())
        if not owner:
            return False
        process = subprocess.run(
            ["/bin/ps", "-ww", "-p", owner[1], "-o", "ppid=", "-o", "comm="],
            capture_output=True, text=True, check=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    command = process.stdout.strip().split(maxsplit=1)
    return (
        len(command) == 2
        and (command[0] == pid[1] or owner[1] == pid[1])
        and Path(command[1]).is_absolute()
        and Path(command[1]).resolve() == binary.resolve()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-provider", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--final-provider", type=Path)
    parser.add_argument("--core", type=Path)
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--env-file", type=Path, help="Enable Feishu sync using this credentials file")
    parser.add_argument("--proxy-port", type=int, default=7890)
    parser.add_argument("--require-project-core", action="store_true",
                        help="Require the macOS project service to own the proxy port")
    parser.add_argument("--mixed-port", type=int, default=17891)
    parser.add_argument("--controller-port", type=int, default=19091)
    args = parser.parse_args(argv)

    root = args.runtime_root.resolve()
    source = args.source_provider.resolve()
    stage1 = root / "xigua-ippool.stage1.yaml"
    state = root / "ippool.json"
    final = args.final_provider.resolve() if args.final_provider else None
    if not source.is_file():
        parser.error("source provider file is missing")
    runtime_files = {
        (root / name).resolve()
        for name in ("xigua-ippool.stage1.yaml", "ippool.json", "round.lock",
                     "collector.lock", "feishu-sync-state.json", "feishu-sync.lock", "feishu-sync.log")
    }
    credentials = args.env_file.resolve() if args.env_file else None
    if source in runtime_files or credentials in runtime_files or (
        final and final in runtime_files | {source, credentials}
    ):
        parser.error("runtime files, inputs and final provider must be distinct")
    if args.env_file and not args.env_file.is_file():
        parser.error("Feishu credentials file is missing")
    if args.require_project_core and not project_core_owns_port(args.proxy_port, args.core):
        print("ROUND_RESULT exit=4 reason=project_core_not_listening", file=sys.stderr)
        return 4
    if final:
        if not args.core or not args.core.is_file():
            parser.error("final publication requires an explicit Mihomo binary")
        try:
            targets = {url for url in args.target if urlsplit(url).scheme == "https"
                       and urlsplit(url).hostname}
        except ValueError:
            parser.error("invalid health target")
        if len(targets) != len(set(args.target)) or len(targets) < 2:
            parser.error("final publication requires at least two distinct HTTPS targets")
        ports = (args.proxy_port, args.mixed_port, args.controller_port)
        if len(set(ports)) != len(ports) or any(not 0 < port < 65536 for port in ports):
            parser.error("collector and health-check ports must be valid and distinct")

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    scripts = Path(__file__).resolve().parent
    with (root / "round.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ROUND_RESULT exit=3 reason=busy", file=sys.stderr)
            return 3

        collector = subprocess.call([
            sys.executable, str(scripts / "ippool-collector.py"),
            "--source-provider", str(source), "--state", str(state),
            "--output-provider", str(stage1), "--lock", str(root / "collector.lock"),
            "--proxy-port", str(args.proxy_port), "--mode", "dry-run",
        ])
        pool = collector
        if collector == 0 and final:
            health = [
                sys.executable, str(scripts / "health-check-provider.py"),
                "--candidate", str(stage1), "--output", str(final), "--core", str(args.core.resolve()),
                "--required-name-regex", "西瓜", "--minimum-matching", "1",
                "--mixed-port", str(args.mixed_port), "--controller-port", str(args.controller_port),
            ]
            for target in dict.fromkeys(args.target):
                health.extend(["--target", target])
            pool = subprocess.call(health)
        print(f"POOL_RESULT collector={collector} final={pool}", flush=True)

        feishu = 0
        if args.env_file:
            sync = [
                sys.executable, str(scripts / "sync-xigua-ippool-feishu.py"),
                "--ippool-state", str(state), "--env-file", str(args.env_file.resolve()),
                "--final-provider", str(final or root / "xigua-ippool.unpublished.yaml"),
                "--cursor", str(root / "feishu-sync-state.json"),
                "--lock", str(root / "feishu-sync.lock"), "--log-file", str(root / "feishu-sync.log"),
            ]
            feishu = subprocess.call(sync)
            print(f"FEISHU_RESULT exit={feishu}", flush=True)
        result = pool or feishu
        print(f"ROUND_RESULT exit={result}", flush=True)
        return result


if __name__ == "__main__":
    raise SystemExit(main())
