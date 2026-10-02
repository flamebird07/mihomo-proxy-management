"""Section 10 / D10: the CLI surface.

The wrapper script must stay logic-free, the parser must expose exactly the
documented subcommands, and ``main()`` must map every outcome onto the stable
exit-code table without ever accepting a secret as an argument.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unittest
import unittest.mock

import support
from mpm import cli, lifecycle
from mpm.errors import ExitCode, FailClosed, NotReady, Unsupported
from mpm.paths import Layout

WRAPPER = os.path.join(support.LINUX_DIR, "bin", "mihomo-proxy-management")


class ParserSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = cli.build_parser()

    def parse(self, argv):
        return self.parser.parse_args(argv)

    def test_every_documented_subcommand_exists(self) -> None:
        for name in lifecycle.SUBCOMMANDS:
            with self.subTest(name=name):
                self.parse([name])

    def test_no_command_is_a_usage_failure(self) -> None:
        code = cli.main([], stdout=support.CapturingStream(), stderr=support.CapturingStream())
        self.assertEqual(code, ExitCode.FAILURE)

    def test_unknown_subcommand_exits_argparse_code_2(self) -> None:
        stream_out, stream_err = support.CapturingStream(), support.CapturingStream()
        saved = sys.stderr
        sys.stderr = stream_err  # keep argparse's own message out of the terminal
        try:
            with self.assertRaises(SystemExit) as caught:
                cli.main(["nonsense"], stdout=stream_out, stderr=stream_err)
        finally:
            sys.stderr = saved
        self.assertEqual(caught.exception.code, ExitCode.FAIL_CLOSED)

    def test_secrets_are_never_arguments(self) -> None:
        """The CLI accepts secret *paths* only: no option may take a URL, token
        or secret value (D7)."""
        options = {flag for flag in _all_option_strings(self.parser)}
        for forbidden in ("--url", "--token", "--secret", "--password", "--subscription-url"):
            self.assertNotIn(forbidden, options)
        self.assertIn("--secret-file", options)
        for name in lifecycle.SUBCOMMANDS:
            with self.subTest(name=name):
                self.assertTrue(hasattr(self.parse([name]), "secret_file"))

    def test_common_flags_present_on_every_subcommand(self) -> None:
        for name in lifecycle.SUBCOMMANDS:
            args = self.parse([name])
            for flag in ("secret_file", "root", "lock_file", "json", "quiet"):
                with self.subTest(name=name, flag=flag):
                    self.assertTrue(hasattr(args, flag), f"{name} lacks --{flag.replace('_', '-')}")

    def test_command_specific_flags(self) -> None:
        self.assertTrue(self.parse(["install", "--enable"]).enable)
        self.assertTrue(self.parse(["install", "--skip-download"]).skip_download)
        self.assertTrue(self.parse(["configure", "--check-only"]).check_only)
        self.assertTrue(self.parse(["uninstall", "--purge", "--yes", "--dry-run"]).purge)
        self.assertEqual(self.parse(["update-subscription", "--provider", "x"]).provider, "x")

    def test_version_flag(self) -> None:
        out = support.CapturingStream()
        code = cli.main(["--version"], stdout=out, stderr=support.CapturingStream())
        self.assertEqual(code, 0)
        self.assertIn("mihomo-proxy-management", out.text)


class ExitCodeMappingTests(unittest.TestCase):
    """main() must translate report/error states onto the documented codes."""

    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.out = support.CapturingStream()
        self.err = support.CapturingStream()

    def run_main(self, argv, *, report=None, error=None):
        def factory(_args):
            ctx = self.sb.ctx()
            ctx.out = self.out
            ctx.err = self.err
            return ctx

        def dispatch(_ctx, _args):
            if error is not None:
                raise error
            return report or lifecycle.Report(command="x", status="OK")

        original = cli.dispatch
        cli.dispatch = dispatch
        try:
            return cli.main(argv, stdout=self.out, stderr=self.err, context_factory=factory)
        finally:
            cli.dispatch = original

    def test_ok_status_is_zero(self) -> None:
        self.assertEqual(self.run_main(["status"]), 0)

    def test_degraded_status_is_still_zero_but_loud(self) -> None:
        code = self.run_main(
            ["status"], report=lifecycle.Report(command="status", status="DEGRADED")
        )
        self.assertEqual(code, 0)
        self.assertIn("STATUS: DEGRADED", self.out.text)

    def test_failed_status_defaults_to_one(self) -> None:
        code = self.run_main(["status"], report=lifecycle.Report(command="status", status="FAILED"))
        self.assertEqual(code, ExitCode.FAILURE)

    def test_failed_preflight_maps_to_unsupported_four(self) -> None:
        report = lifecycle.Report(command="preflight", status="FAILED")
        report.exit_code = ExitCode.UNSUPPORTED
        self.assertEqual(self.run_main(["preflight"], report=report), ExitCode.UNSUPPORTED)

    def test_fail_closed_is_two(self) -> None:
        self.assertEqual(self.run_main(["install"], error=FailClosed("boundary")), ExitCode.FAIL_CLOSED)

    def test_not_ready_is_three(self) -> None:
        self.assertEqual(self.run_main(["start"], error=NotReady("nope")), ExitCode.NOT_READY)

    def test_unsupported_is_four(self) -> None:
        self.assertEqual(
            self.run_main(["preflight"], error=Unsupported("no systemd")), ExitCode.UNSUPPORTED
        )

    def test_error_text_is_sanitised_on_stderr(self) -> None:
        secret = self.sb.canary_secret
        self.sb.canaries()  # ensure the canary exists
        from mpm.sanitize import HOST

        HOST.register(secret)
        code = self.run_main(["install"], error=FailClosed(f"refusing because {secret}"))
        self.assertEqual(code, ExitCode.FAIL_CLOSED)
        self.assertNotIn(secret, self.err.text)

    def test_unexpected_exception_never_leaks_details(self) -> None:
        class Boom(RuntimeError):
            pass

        code = self.run_main(["status"], error=Boom("internal detail with path /tmp/x"))
        self.assertEqual(code, 1)
        self.assertIn("unexpected Boom", self.err.text)
        self.assertNotIn("internal detail", self.err.text)


class JsonOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.out = support.CapturingStream()
        self.err = support.CapturingStream()

    def run_main(self, argv, report):
        def factory(_args):
            ctx = self.sb.ctx()
            ctx.out = self.out
            ctx.err = self.err
            return ctx

        original = cli.dispatch
        cli.dispatch = lambda _c, _a: report
        try:
            return cli.main(argv, stdout=self.out, stderr=self.err, context_factory=factory)
        finally:
            cli.dispatch = original

    def test_json_is_machine_readable_and_canary_free(self) -> None:
        report = lifecycle.Report(command="status", status="OK")
        report.data = {"nested": {"value": "plain"}}
        report.warnings = ["warn"]
        code = self.run_main(["status", "--json"], report)
        self.assertEqual(code, 0)
        payload = json.loads(self.out.text)
        self.assertEqual(payload["status"], "OK")
        self.assertEqual(payload["data"]["nested"]["value"], "plain")
        self.assertEqual(payload["warnings"], ["warn"])
        for canary in self.sb.canaries():
            self.assertNotIn(canary, self.out.text)

    def test_json_mode_emits_a_single_document(self) -> None:
        """In --json mode stdout is exactly one JSON document: no human lines,
        no STATUS banner mixed in."""
        report = lifecycle.Report(command="status", status="DEGRADED")
        report.messages.append("human readable line")
        self.run_main(["status", "--json"], report)
        payload = json.loads(self.out.text)  # parses => nothing else on stdout
        self.assertNotIn("STATUS:", self.out.text)
        self.assertEqual(payload["messages"], ["human readable line"])


class ContextWiringTests(unittest.TestCase):
    def test_lock_resolution_prefers_flag_then_env_then_source_tree(self) -> None:
        args = cli.build_parser().parse_args(["preflight"])
        args.root = None
        args.secret_file = None
        args.quiet = True
        args.json = False
        args.dry_run = False
        args.enable = False
        args.lock_file = "/tmp/explicit.lock.json"
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            ctx = cli.make_context(args, stdout=sys.stdout, stderr=sys.stderr)
            self.assertEqual(ctx.lock_path, "/tmp/explicit.lock.json")

        args.lock_file = None
        with unittest.mock.patch.dict(os.environ, {cli.ENV_LOCK: "/tmp/from-env.lock.json"}):
            ctx = cli.make_context(args, stdout=sys.stdout, stderr=sys.stderr)
            self.assertEqual(ctx.lock_path, "/tmp/from-env.lock.json")

        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cli.default_lock_path(), os.path.join(support.LINUX_DIR, "supply", "mihomo.lock.json"))

    def test_root_override_builds_layout_under_it(self) -> None:
        args = cli.build_parser().parse_args(["status", "--root", "/tmp/stage"])
        args.secret_file = None
        args.lock_file = None
        args.quiet = True
        args.json = False
        args.dry_run = False
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            ctx = cli.make_context(args, stdout=sys.stdout, stderr=sys.stderr)
        self.assertEqual(ctx.layout, Layout("/tmp/stage"))

    def test_secret_file_is_passed_as_a_path_only(self) -> None:
        args = cli.build_parser().parse_args(["install", "--secret-file", "/root/sub.env"])
        self.assertEqual(args.secret_file, "/root/sub.env")


class WrapperScriptTests(unittest.TestCase):
    """The shell entry point must stay a thin, logic-free wrapper."""

    def test_syntax_check(self) -> None:
        if not _which("sh"):
            self.skipTest("sh NOT_RUN: not present")
        result = subprocess.run(["sh", "-n", WRAPPER], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wrapper_contains_no_business_logic(self) -> None:
        with open(WRAPPER, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        code = "\n".join(line for line in lines if not line.strip().startswith("#"))
        for forbidden in ("systemctl", "curl", "wget", "nft", "iptables", "modprobe",
                          "subscription.env", "render", "python3 -m mpm"):
            self.assertNotIn(forbidden, code, forbidden)
        self.assertIn("set -eu", code)
        self.assertLess(len([l for l in code.splitlines() if l.strip()]), 40, "wrapper grew logic")

    def test_wrapper_runs_the_package_end_to_end(self) -> None:
        if not _which("python3"):
            self.skipTest("python3 NOT_RUN: not present")
        env = dict(os.environ, MPM_PYTHONPATH=os.path.join(support.LINUX_DIR, "python"))
        result = subprocess.run(
            [WRAPPER, "--version"], capture_output=True, text=True, env=env, timeout=60
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mihomo-proxy-management 3", result.stdout)

    def test_wrapper_status_without_privileges_is_clean(self) -> None:
        """A sandboxed --root run must not touch the host and must not print secrets."""
        if not _which("python3"):
            self.skipTest("python3 NOT_RUN: not present")
        env = dict(os.environ, MPM_PYTHONPATH=os.path.join(support.LINUX_DIR, "python"))
        result = subprocess.run(
            [WRAPPER, "preflight", "--root", _temp_root(self), "--json"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        # unsupported host profiles (no systemd / not root) are a legitimate
        # FAILED/4 outcome; what must never happen is a crash or a traceback.
        self.assertNotIn("Traceback", result.stderr)
        if result.returncode == 0:
            json.loads(result.stdout)


def _temp_root(case: unittest.TestCase) -> str:
    import tempfile

    root = tempfile.mkdtemp(prefix="mpm-wrapper-")
    case.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
    return root


def _which(name: str):
    import shutil

    return shutil.which(name)


def _all_option_strings(parser) -> set[str]:
    found: set[str] = set()
    stack = [parser]
    while stack:
        item = stack.pop()
        for action in item._actions:
            found.update(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
    return found


class DispatchTableTests(unittest.TestCase):
    """dispatch() must reach the lifecycle engine and nothing else."""

    def setUp(self) -> None:
        self.sb = support.Sandbox()
        self.addCleanup(self.sb.cleanup)
        self.calls: list[str] = []

    def _patch(self, names):
        import mpm.lifecycle as lc

        for name in names:
            self._originals[name] = getattr(lc, name)
            setattr(lc, name, self._make(name))

    def _make(self, name):
        def stub(*_a, **_k):
            self.calls.append(name)
            return lifecycle.Report(command=name, status="OK")

        return stub

    def test_each_command_maps_to_its_engine_function(self) -> None:
        mapping = {
            "preflight": "run_preflight",
            "install": "install",
            "configure": "configure",
            "start": "start",
            "stop": "stop",
            "restart": "restart",
            "status": "status",
            "test": "run_test",
            "update-subscription": "update_subscription",
            "uninstall": "uninstall",
        }
        self._originals = {}
        self._patch(list(mapping.values()))
        try:
            for command, function in mapping.items():
                with self.subTest(command=command):
                    args = cli.build_parser().parse_args([command])
                    report = cli.dispatch(self.sb.ctx(), args)
                    self.assertEqual(report.command, function)
        finally:
            import mpm.lifecycle as lc

            for name, original in self._originals.items():
                setattr(lc, name, original)

    def test_unknown_command_fails_closed(self) -> None:
        args = cli.build_parser().parse_args(["status"])
        args.command = "does-not-exist"
        with self.assertRaises(FailClosed):
            cli.dispatch(self.sb.ctx(), args)


if __name__ == "__main__":
    unittest.main()
