"""deploy.credentials: stdin JSON, env > stdin > prompt, and redaction."""

import io
import os
import secrets
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import credentials as creds  # noqa: E402


def fake(value):
    return f"fake-{value}-" + secrets.token_hex(8)


def json_object(**fields):
    import json
    return io.StringIO(json.dumps(fields))


class StdinTests(unittest.TestCase):
    def test_no_stream_or_blank_input_means_nothing(self):
        self.assertEqual(creds.read_stdin_values(None), {})
        self.assertEqual(creds.read_stdin_values(io.StringIO("")), {})
        self.assertEqual(creds.read_stdin_values(io.StringIO("  \n\t")), {})

    def test_fields_are_kept_stripped_and_unknown_keys_dropped(self):
        stream = json_object(feishu_url="  https://x.feishu.cn/base/tok12345678  ",
                             app_id=" cli_x ", app_secret=" s ", extra="ignored")
        self.assertEqual(creds.read_stdin_values(stream), {
            "feishu_url": "https://x.feishu.cn/base/tok12345678",
            "app_id": "cli_x",
            "app_secret": "s",
        })

    def test_blank_and_null_fields_are_dropped(self):
        stream = json_object(feishu_url="   ", app_id=None, app_secret="kept")
        self.assertEqual(creds.read_stdin_values(stream), {"app_secret": "kept"})

    def test_invalid_input_is_reported_without_echoing_it(self):
        bad_secret = fake("app-secret")
        for text in (f'{{"app_secret": "{bad_secret}",,}}', f'["{bad_secret}"]',
                     f'{{"app_secret": {len(bad_secret)}}}', f'{{"app_secret": "{bad_secret}"'):
            with self.subTest(shape=text[:14]):
                with self.assertRaises(creds.CredentialsError) as caught:
                    creds.read_stdin_values(io.StringIO(text))
                self.assertEqual(caught.exception.kind, "stdin_invalid")
                self.assertNotIn(bad_secret, str(caught.exception))


class ResolveTests(unittest.TestCase):
    def test_environment_beats_stdin_and_stdin_beats_the_prompt(self):
        url, app_id = fake("url"), fake("id")
        answers = {"feishu_url": fake("prompted-url"), "app_id": fake("prompted-id"),
                   "app_secret": fake("prompted-secret")}
        prompted = []

        def prompt(field):
            prompted.append(field)
            return answers[field]

        resolved = creds.resolve_credentials(
            {creds.BASE_URL_KEY: url}, {"feishu_url": fake("stdin"), "app_id": app_id},
            prompt)
        self.assertEqual(resolved, creds.Credentials(url, app_id, answers["app_secret"]))
        self.assertEqual(prompted, ["app_secret"])

    def test_address_may_come_from_the_token_alias(self):
        token = fake("token")
        resolved = creds.resolve_credentials(
            {creds.TOKEN_KEY: token, creds.APP_ID_KEY: "cli_x", creds.APP_SECRET_KEY: "s"}, None, None)
        self.assertEqual(resolved.feishu_url, token)

    def test_base_url_wins_over_the_token_alias(self):
        url, token = fake("url"), fake("token")
        resolved = creds.resolve_credentials(
            {creds.TOKEN_KEY: token, creds.BASE_URL_KEY: url,
             creds.APP_ID_KEY: "cli_x", creds.APP_SECRET_KEY: "s"}, None, None)
        self.assertEqual(resolved.feishu_url, url)

    def test_prompt_is_used_only_for_what_is_missing_at_most_once_per_field(self):
        url = fake("url")
        calls = []

        def prompt(field):
            calls.append(field)
            return fake("prompt-" + field)

        resolved = creds.resolve_credentials(
            {creds.BASE_URL_KEY: url}, {"app_id": "  "}, prompt)
        self.assertEqual(calls, ["app_id", "app_secret"])
        self.assertEqual(resolved.feishu_url, url)

    def test_missing_fields_are_reported_by_key_name_in_field_order(self):
        cases = (
            ({}, (creds.BASE_URL_KEY, creds.APP_ID_KEY, creds.APP_SECRET_KEY)),
            ({creds.APP_ID_KEY: "cli_x"}, (creds.BASE_URL_KEY, creds.APP_SECRET_KEY)),
            ({creds.BASE_URL_KEY: "tok12345678", creds.APP_SECRET_KEY: "s"},
             (creds.APP_ID_KEY,)),
        )
        for environ, missing in cases:
            with self.subTest(missing=missing):
                with self.assertRaises(creds.CredentialsError) as caught:
                    creds.resolve_credentials(environ, None, None)
                self.assertEqual(caught.exception.kind, "missing")
                self.assertEqual(caught.exception.missing, missing)
                self.assertEqual(str(caught.exception),
                                 "missing credentials: " + ", ".join(missing)
                                 + " (set env vars, pipe JSON on stdin, or run interactively)")

    def test_blank_candidates_are_skipped_and_values_are_stripped(self):
        environ = {creds.BASE_URL_KEY: " ", creds.APP_ID_KEY: "  cli_x  ",
                   creds.APP_SECRET_KEY: "\ts\t"}
        resolved = creds.resolve_credentials(
            environ, {"feishu_url": " tok12345678 ", "app_id": ""}, lambda field: "")
        self.assertEqual(resolved, creds.Credentials("tok12345678", "cli_x", "s"))

    def test_none_sources_are_accepted(self):
        resolved = creds.resolve_credentials(
            None, {"feishu_url": "tok12345678", "app_id": "cli_x", "app_secret": "s"}, None)
        self.assertEqual(resolved.app_id, "cli_x")

    def test_prompt_errors_propagate(self):
        for error in (KeyboardInterrupt, EOFError):
            with self.subTest(error=error):
                def prompt(field, error=error):
                    raise error()
                with self.assertRaises(error):
                    creds.resolve_credentials(None, None, prompt)


class SecretHandlingTests(unittest.TestCase):
    def test_secret_never_appears_in_repr_or_str(self):
        secret = fake("app-secret")
        value = creds.Credentials("tok12345678", "cli_x", secret)
        self.assertNotIn(secret, repr(value))
        self.assertNotIn(secret, str(value))
        self.assertIn("cli_x", repr(value))

    def test_mask(self):
        self.assertEqual(creds.mask(""), "")
        self.assertEqual(creds.mask("short"), "<redacted>")
        self.assertEqual(creds.mask("x" * 9), "<redacted>")
        self.assertEqual(creds.mask("abcdefghijklmnop"), "abcd…nop")

    def test_redactor_ignores_short_and_missing_values(self):
        redactor = creds.Redactor()
        redactor.add(None)
        redactor.add("")
        redactor.add("ab")
        self.assertEqual(redactor.text("ab cd ab"), "ab cd ab")

    def test_redactor_scrubs_longest_first_so_overlaps_leave_no_stub(self):
        token = fake("token")
        url = f"https://x.feishu.cn/base/{token}"
        redactor = creds.Redactor([token])
        redactor.add(url)
        redactor.add(url)
        self.assertEqual(redactor.text(f"opening {url} now"), "opening <redacted> now")
        self.assertEqual(redactor.text(f"token {token}!"), "token <redacted>!")


if __name__ == "__main__":
    unittest.main()
