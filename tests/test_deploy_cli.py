"""deploy.__main__: the CLI wiring, fully offline with fake factory and runner."""

import io
import json
import os
import secrets
import sys
import unittest
from dataclasses import dataclass, field
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, os.pardir))
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import bootstrap  # noqa: E402
from deploy.__main__ import main  # noqa: E402
from deploy.credentials import Credentials  # noqa: E402

URL = "https://example.feishu.cn/base/cliFakeToken1234567890"
TOKEN = "cliFakeToken1234567890"
APP_ID = "cli-fake-app-id"
SECRET = "cli-fake-secret-" + secrets.token_hex(8)


@dataclass
class FakeAccess:
    read_ok: bool = True
    write_ok: bool = True
    problems: tuple = ()
    extra: str = field(default="")

    def hints(self):
        return ["hint: run --dry-run first"]


class FakeFactory:
    def __init__(self):
        self.calls = []

    def __call__(self, name, repo_root, environ):
        self.calls.append((name, repo_root, environ))
        return object()  # the runner never touches it


class FakeRunner:
    def __init__(self, exit_code=0, problems=(), access=None, error=None):
        self.exit_code = exit_code
        self.problems = problems
        self.access = access
        self.error = error
        self.calls = []

    def __call__(self, credentials, platform, **kwargs):
        self.calls.append((credentials, platform, kwargs))
        if self.error is not None:
            raise self.error
        return bootstrap.BootstrapResult(exit_code=self.exit_code,
                                         problems=self.problems,
                                         access=self.access,
                                         dry_run=kwargs.get("dry_run", False))


class CliTestCase(unittest.TestCase):
    def setUp(self):
        clean = {key: value for key, value in os.environ.items()
                 if not key.startswith("MPM_FEISHU")}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.factory = FakeFactory()
        self.runner = FakeRunner()
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_main(self, argv, stdin_text=""):
        stdin = self.fake_tty() if stdin_text is None else io.StringIO(stdin_text)
        code = main(argv, stdin=stdin, stdout=self.out, stderr=self.err,
                    platform_factory=self.factory, runner=self.runner)
        out, err = self.out.getvalue(), self.err.getvalue()
        self.assertNoLeaks(out, err)
        return code, out, err

    def fake_tty(self):
        stream = io.StringIO()
        stream.isatty = lambda: True
        return stream

    def assertNoLeaks(self, out, err):
        for text in (out, err):
            self.assertNotIn(SECRET, text)
            self.assertNotIn(TOKEN, text)
            self.assertNotIn(URL, text)

    def stdin_json(self, **overrides):
        payload = {"feishu_url": URL, "app_id": APP_ID, "app_secret": SECRET}
        payload.update(overrides)
        return json.dumps(payload)

    def happy_argv(self, *extra):
        return ["--platform", "macos"] + list(extra)


class CredentialSourceTests(CliTestCase):
    def test_stdin_json_is_used(self):
        code, _, _ = self.run_main(self.happy_argv(), self.stdin_json())
        self.assertEqual(code, 0)
        creds, _, _ = self.runner.calls[0]
        self.assertEqual(creds.feishu_url, URL)
        self.assertEqual(creds.app_id, APP_ID)
        self.assertEqual(creds.app_secret, SECRET)

    def test_environment_beats_stdin(self):
        os.environ["MPM_FEISHU_BASE_URL"] = URL
        os.environ["MPM_FEISHU_APP_ID"] = "env-app-id"
        os.environ["MPM_FEISHU_APP_SECRET"] = SECRET
        code, _, _ = self.run_main(self.happy_argv(), self.stdin_json(app_id="stdin-id"))
        self.assertEqual(code, 0)
        creds, _, _ = self.runner.calls[0]
        self.assertEqual(creds.app_id, "env-app-id")

    def test_no_source_at_all_fails_before_the_runner(self):
        code, out, err = self.run_main(self.happy_argv())
        self.assertEqual(code, 2)
        self.assertIn("MPM_FEISHU_BASE_URL", err)
        self.assertIn("MPM_FEISHU_APP_SECRET", err)
        self.assertEqual(self.runner.calls, [])

    def test_invalid_stdin_json_is_not_echoed(self):
        text = "not json at all " + SECRET
        code, _, err = self.run_main(self.happy_argv(), text)
        self.assertEqual(code, 2)
        self.assertNotIn(SECRET, err)
        self.assertNotIn("not json at all", err)

    def test_non_feishu_address_is_rejected(self):
        bad = "https://example.com/base/xxxxxxxxxxxx"
        payload = json.dumps({"feishu_url": bad, "app_id": APP_ID,
                              "app_secret": SECRET})
        code, _, err = self.run_main(self.happy_argv(), payload)
        self.assertEqual(code, 2)
        self.assertIn("not_feishu_host", err)


class InteractiveTests(CliTestCase):
    def test_prompts_use_getpass_for_the_secret(self):
        answers = iter([URL, APP_ID])
        calls = []
        with mock.patch("builtins.input",
                        lambda prompt: (calls.append(prompt), next(answers))[1]):
            with mock.patch("getpass.getpass", return_value=SECRET) as getpass_mock:
                code, _, _ = self.run_main(self.happy_argv(), stdin_text=None)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        getpass_mock.assert_called_once()
        creds, _, _ = self.runner.calls[0]
        self.assertEqual(creds.app_secret, SECRET)

    def test_interrupted_prompt_returns_130(self):
        with mock.patch("builtins.input", lambda _: URL):
            with mock.patch("getpass.getpass",
                            side_effect=KeyboardInterrupt):
                code, _, err = self.run_main(self.happy_argv(), stdin_text=None)
        self.assertEqual(code, 130)
        self.assertIn("error: interrupted", err)


class FlagPassthroughTests(CliTestCase):
    def test_defaults(self):
        code, _, err = self.run_main(self.happy_argv(), self.stdin_json())
        self.assertEqual(code, 0)
        _, _, kwargs = self.runner.calls[0]
        self.assertFalse(kwargs["dry_run"])
        self.assertTrue(kwargs["start"])
        self.assertFalse(kwargs["skip_supply"])
        self.assertFalse(kwargs["skip_install"])
        self.assertEqual(kwargs["min_nodes"], 1)
        name, repo_root, _ = self.factory.calls[0]
        self.assertEqual(name, "macos")
        self.assertEqual(os.path.abspath(repo_root), REPO_ROOT)

    def test_flags_reach_the_runner(self):
        argv = self.happy_argv("--dry-run", "--no-start", "--skip-supply",
                               "--skip-install", "--min-nodes", "7")
        code, _, _ = self.run_main(argv, self.stdin_json())
        self.assertEqual(code, 0)
        _, _, kwargs = self.runner.calls[0]
        self.assertTrue(kwargs["dry_run"])
        self.assertFalse(kwargs["start"])
        self.assertTrue(kwargs["skip_supply"])
        self.assertTrue(kwargs["skip_install"])
        self.assertEqual(kwargs["min_nodes"], 7)

    def test_windows_platform_name_reaches_the_factory(self):
        code, _, _ = self.run_main(["--platform", "windows"], self.stdin_json())
        self.assertEqual(code, 0)
        self.assertEqual(self.factory.calls[0][0], "windows")


class ArgparseGuardTests(CliTestCase):
    def assert_usage_error(self, argv):
        with self.assertRaises(SystemExit) as ctx:
            main(argv, stdin=io.StringIO(self.stdin_json()), stdout=io.StringIO(),
                 stderr=io.StringIO(), platform_factory=self.factory,
                 runner=self.runner)
        self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(self.runner.calls, [])

    def test_negative_min_nodes(self):
        self.assert_usage_error(self.happy_argv("--min-nodes", "-1"))

    def test_missing_platform(self):
        self.assert_usage_error([])

    def test_unknown_platform_choice(self):
        self.assert_usage_error(["--platform", "linux"])


class ResultReportingTests(CliTestCase):
    def test_success_json_goes_to_stdout_and_ok_to_stderr(self):
        argv = self.happy_argv("--json")
        code, out, err = self.run_main(argv, self.stdin_json())
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["exit_code"], 0)
        self.assertEqual(err, "")

    def test_success_without_json_ends_with_ok_on_stderr(self):
        code, out, err = self.run_main(self.happy_argv(), self.stdin_json())
        self.assertEqual(code, 0)
        self.assertEqual(out, "")
        self.assertEqual(err.strip().splitlines()[-1], "bootstrap: OK")

    def test_failure_reports_problems_and_exit(self):
        self.runner = FakeRunner(exit_code=4, problems=("supply:download_failed",),
                                 access=FakeAccess(problems=("supply:download_failed",)))
        self.factory = FakeFactory()
        code, out, err = self.run_main(self.happy_argv(), self.stdin_json())
        self.assertEqual(code, 4)
        self.assertIn("error: supply:download_failed", err)
        self.assertIn("hint: run --dry-run first", err)
        self.assertIn("bootstrap: FAILED exit=4", err)
        self.assertEqual(out, "")

    def test_runner_crash_names_only_the_class(self):
        self.runner = FakeRunner(error=RuntimeError(f"boom {SECRET}"))
        code, _, err = self.run_main(self.happy_argv(), self.stdin_json())
        self.assertEqual(code, 2)
        self.assertIn("unexpected RuntimeError", err)
        self.assertNotIn("boom", err)
        self.assertNotIn(SECRET, err)


if __name__ == "__main__":
    unittest.main()
