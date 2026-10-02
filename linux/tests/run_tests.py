#!/usr/bin/env python3
"""mihomo-proxy-management test orchestrator (section 11, D12=B).

Runs every mandatory category and records an honest result table:

* ``PASS`` / ``FAIL`` per unittest suite (each suite is mandatory: core unit,
  idempotency, leak scan, supply chain, lifecycle)
* ``PASS`` / ``NOT_RUN`` per *optional* host tool check (shellcheck, ...) - a
  missing tool is always disclosed, never silently skipped
* static audits: ``python -m compileall``, byte-level ``git diff --check``
  style whitespace scan of the delivery tree, UTF-8-no-BOM/LF audit, wrapper
  ``sh -n``

Usage:
    python3 linux/tests/run_tests.py [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
LINUX = os.path.dirname(HERE)
REPO = os.path.dirname(LINUX)

SUITES = [
    ("config-unit", "test_config.py"),
    ("lifecycle-idempotency", "test_lifecycle.py"),
    ("supply-chain-controller-unit", "test_supply_unit.py"),
    ("cli-surface", "test_cli.py"),
    ("leak-scan-gitignore", "test_leak_scan.py"),
]

TEXT_SUFFIXES = {
    ".py", ".sh", ".json", ".md", ".yaml", ".yml", ".service", ".template",
    ".gitignore", ".gitattributes", "",
}


class Recorder:
    def __init__(self) -> None:
        self.results: list[dict[str, object]] = []

    def add(self, name: str, status: str, detail: str = "", command: str = "") -> None:
        self.results.append(
            {"name": name, "status": status, "detail": detail[:2000], "command": command}
        )
        print(f"[{status:7}] {name}" + (f"  ({detail[:120]})" if detail and status != "PASS" else ""))


def run_suite(rec: Recorder, name: str, filename: str) -> bool:
    pattern = filename
    command = f"python3 -m unittest discover -s tests -t tests -p {pattern}"
    started = time.time()
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=HERE, pattern=pattern, top_level_dir=HERE)
    runner = unittest.TextTestRunner(verbosity=1, stream=sys.stdout)
    result = runner.run(suite)
    ok = result.wasSuccessful() and result.testsRun > 0
    skipped = len(result.skipped)
    detail = f"{result.testsRun} tests in {time.time() - started:.1f}s"
    if skipped:
        detail += f", {skipped} skipped (honest NOT_RUN sub-checks)"
    if not ok:
        detail += f"; failures={len(result.failures)} errors={len(result.errors)}"
    rec.add(name, "PASS" if ok else "FAIL", detail, command)
    return ok


def tool_check(rec: Recorder, name: str, tool: str, argv: list[str], paths: list[str]) -> None:
    binary = shutil.which(tool)
    if not binary:
        rec.add(name, "NOT_RUN", f"{tool} is not installed on this host; nothing was faked", " ".join(argv))
        return
    bad = []
    for path in paths:
        result = subprocess.run([*argv, path], capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            bad.append(f"{path}: rc={result.returncode}")
    if bad:
        rec.add(name, "FAIL", "; ".join(bad), " ".join(argv))
    else:
        rec.add(name, "PASS", f"{len(paths)} file(s) clean", " ".join(argv))


def compileall_check(rec: Recorder) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", os.path.join(LINUX, "python"),
         os.path.join(LINUX, "tests"), os.path.join(REPO, "scripts", "convert_sub.py")],
        capture_output=True, text=True,
    )
    ok = result.returncode == 0
    rec.add("python-compileall", "PASS" if ok else "FAIL",
            (result.stdout + result.stderr).strip(), "python3 -m compileall")


def encoding_audit(rec: Recorder) -> None:
    """The Linux delivery tree must be UTF-8, LF, no BOM; baseline Windows
    files keep their BOM (phase 3 does not rewrite them byte-wise)."""
    problems: list[str] = []
    for base in (LINUX, os.path.join(REPO, ".gitignore"), os.path.join(REPO, ".gitattributes")):
        paths = [base] if os.path.isfile(base) else []
        if os.path.isdir(base):
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                paths.extend(os.path.join(dirpath, f) for f in filenames)
        for path in paths:
            if os.path.splitext(path)[1] not in TEXT_SUFFIXES and path not in (
                os.path.join(REPO, ".gitignore"), os.path.join(REPO, ".gitattributes"),
            ):
                continue
            with open(path, "rb") as handle:
                blob = handle.read()
            if blob.startswith(b"\xef\xbb\xbf"):
                problems.append(f"BOM: {path}")
            if b"\r" in blob:
                problems.append(f"CR: {path}")
            try:
                blob.decode("utf-8")
            except UnicodeDecodeError:
                problems.append(f"not-UTF-8: {path}")
    rec.add("encoding-lf-no-bom", "PASS" if not problems else "FAIL", "; ".join(problems[:12]))


def whitespace_audit(rec: Recorder) -> None:
    """Equivalent of ``git diff --check`` for the delivery tree: no trailing
    whitespace, no space-before-tab in indentation, files end with newline."""
    problems: list[str] = []
    for base in (LINUX, os.path.join(REPO, ".gitignore"), os.path.join(REPO, ".gitattributes")):
        paths = [base] if os.path.isfile(base) else []
        if os.path.isdir(base):
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                paths.extend(os.path.join(dirpath, f) for f in filenames)
        for path in paths:
            ext = os.path.splitext(path)[1]
            if ext in {".gz", ".zip", ".dat", ".exe", ".dll", ".pyc"}:
                continue
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except (UnicodeDecodeError, OSError):
                continue
            if text and not text.endswith("\n"):
                problems.append(f"no-final-newline: {path}")
            for lineno, line in enumerate(text.splitlines(), 1):
                if line.rstrip() != line:
                    problems.append(f"trailing-ws: {path}:{lineno}")
                if line.startswith(" \t") or " \t" in line[: len(line) - len(line.lstrip())]:
                    problems.append(f"space-before-tab: {path}:{lineno}")
    rec.add("whitespace-check", "PASS" if not problems else "FAIL",
            "; ".join(problems[:12]), "git diff --check equivalent")


def sh_check(rec: Recorder) -> None:
    wrapper = os.path.join(LINUX, "bin", "mihomo-proxy-management")
    if not shutil.which("sh"):
        rec.add("sh-syntax", "NOT_RUN", "sh not present", "sh -n")
        return
    result = subprocess.run(["sh", "-n", wrapper], capture_output=True, text=True)
    rec.add("sh-syntax", "PASS" if result.returncode == 0 else "FAIL",
            (result.stdout + result.stderr).strip(), f"sh -n {wrapper}")


def systemd_analyze_check(rec: Recorder) -> None:
    """Rendered units are verified by RealSystemdAnalyzeTests inside the
    supply suite; here we surface it as its own line for the report."""
    if not shutil.which("systemd-analyze"):
        rec.add("systemd-analyze-verify", "NOT_RUN",
                "systemd-analyze not installed; suite marks the same check NOT_RUN",
                "systemd-analyze verify")
        return
    loader = unittest.TestLoader()
    suite = loader.discover(start_dir=HERE, pattern="test_supply_unit.py", top_level_dir=HERE)
    subset = unittest.TestSuite()

    def collect(test):
        if isinstance(test, unittest.TestSuite):
            for item in test:
                collect(item)
        elif type(test).__name__ == "RealSystemdAnalyzeTests":
            subset.addTest(test)

    for item in suite:
        collect(item)
    result = unittest.TextTestRunner(verbosity=0, stream=sys.stdout).run(subset)
    ok = result.wasSuccessful() and result.testsRun > 0 and not result.skipped
    rec.add("systemd-analyze-verify", "PASS" if ok else "FAIL",
            f"{result.testsRun} rendered unit(s) verified against real systemd-analyze",
            "systemd-analyze verify <rendered unit>")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", dest="json_out", help="write machine-readable results")
    args = parser.parse_args()

    sys.path.insert(0, HERE)
    rec = Recorder()
    print(f"== mpm test run ({sys.version.split()[0]}, euid={os.geteuid()}) ==")

    all_ok = True
    for name, filename in SUITES:
        all_ok &= run_suite(rec, name, filename)

    compileall_check(rec)
    sh_check(rec)
    encoding_audit(rec)
    whitespace_audit(rec)
    systemd_analyze_check(rec)
    tool_check(
        rec,
        "shellcheck",
        "shellcheck",
        ["shellcheck", "-S", "warning"],
        [os.path.join(LINUX, "bin", "mihomo-proxy-management")],
    )

    failures = [r for r in rec.results if r["status"] == "FAIL"]
    not_run = [r for r in rec.results if r["status"] == "NOT_RUN"]
    print(f"\n{len(rec.results)} checks, {len(failures)} failed, {len(not_run)} NOT_RUN")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump({"results": rec.results}, handle, indent=2, sort_keys=True)
            handle.write("\n")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
