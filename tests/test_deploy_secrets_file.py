"""deploy.secrets_file: merge keys into the secret file and keep it private."""

import os
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.abspath(os.path.join(HERE, os.pardir, "scripts"))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from deploy import secrets_file  # noqa: E402


class MergeTests(unittest.TestCase):
    def test_new_file_from_nothing(self):
        self.assertEqual(secrets_file.merge_env_text("", {"A": "1", "B": "2"}), "A=1\nB=2\n")
        self.assertEqual(secrets_file.merge_env_text("", {}), "")

    def test_update_in_place_keeps_everything_else_where_it_was(self):
        existing = "# header\nSUBSCRIPTION_URL=https://x\n\nSECRET=old\n# tail note\n"
        merged = secrets_file.merge_env_text(existing, {"SECRET": "new"})
        self.assertEqual(merged, "# header\nSUBSCRIPTION_URL=https://x\n\nSECRET=new\n# tail note\n")

    def test_new_keys_are_appended_in_update_order(self):
        merged = secrets_file.merge_env_text("A=1\n", {"C": "3", "B": "2", "A": "9"})
        self.assertEqual(merged, "A=9\nC=3\nB=2\n")

    def test_duplicate_keys_collapse_into_the_new_value(self):
        merged = secrets_file.merge_env_text("A=old\nB=keep\nA=older\n", {"A": "new"})
        self.assertEqual(merged, "A=new\nB=keep\n")

    def test_quoted_old_value_is_replaced_by_the_raw_value(self):
        merged = secrets_file.merge_env_text('SECRET="old"\n', {"SECRET": "a=b#c"})
        self.assertEqual(merged, "SECRET=a=b#c\n")

    def test_crlf_files_stay_crlf(self):
        merged = secrets_file.merge_env_text("A=1\r\n# c\r\n", {"A": "2", "B": "3"})
        self.assertEqual(merged, "A=2\r\n# c\r\nB=3\r\n")

    def test_bom_is_dropped(self):
        merged = secrets_file.merge_env_text("\ufeffA=1\n", {"B": "2"})
        self.assertFalse(merged.startswith("\ufeff"))
        self.assertEqual(merged, "A=1\nB=2\n")

    def test_exactly_one_final_newline_and_no_leading_blank(self):
        self.assertEqual(secrets_file.merge_env_text("A=1", {}), "A=1\n")
        self.assertEqual(secrets_file.merge_env_text("A=1\n\n\n", {}), "A=1\n")
        self.assertEqual(secrets_file.merge_env_text("\n\nA=1\n", {}), "A=1\n")

    def test_key_with_spaces_around_the_separator_is_matched(self):
        merged = secrets_file.merge_env_text("  A = 1\n", {"A": "2"})
        self.assertEqual(merged, "A=2\n")

    def test_commented_out_key_does_not_count_as_present(self):
        merged = secrets_file.merge_env_text("# A=1\n", {"A": "2"})
        self.assertEqual(merged, "# A=1\nA=2\n")

    def test_multiline_values_and_bad_keys_are_refused_by_key_name(self):
        for key, value in (("A", "x\ny"), ("A", "x\ry"), ("B\nC", "1"), ("", "1"),
                           ("#D", "1"), ("E=F", "1"), (" G", "1"), ("H", 1)):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError) as caught:
                    secrets_file.merge_env_text("", {key: value})
                self.assertNotIn("x\ny", str(caught.exception))


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = self.temp.name
        self.path = os.path.join(self.root, "subscription.env")

    def read(self):
        with open(self.path, encoding="utf-8", newline="") as handle:
            return handle.read()

    def leftovers(self):
        return [name for name in os.listdir(self.root) if name.startswith(".tmp-")]


class PosixWriteTests(TempDirTestCase):
    def write(self, text):
        return secrets_file.write_secret_file(self.path, text, os_name="posix")

    @unittest.skipIf(os.name == "nt", "0600 is a POSIX permission bit")
    def test_file_is_0600_and_holds_the_text(self):
        result = self.write("SECRET=one\n")
        self.assertEqual(result, {"path": self.path, "protection": "0600"})
        self.assertEqual(self.read(), "SECRET=one\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_second_write_stays_private_and_leaves_no_temp_file(self):
        self.write("SECRET=one\n")
        self.write("SECRET=two\r\n")
        self.assertEqual(self.read(), "SECRET=two\r\n")
        self.assertEqual(self.leftovers(), [])

    def test_missing_directory_is_reported_without_the_text(self):
        secret = "SECRET=do-not-leak-value\n"
        with self.assertRaises(secrets_file.SecretFileError) as caught:
            secrets_file.write_secret_file(
                os.path.join(self.root, "missing", "subscription.env"), secret, os_name="posix")
        self.assertEqual(caught.exception.kind, "write_failed")
        self.assertNotIn("do-not-leak-value", str(caught.exception))


class WindowsLayoutTests(TempDirTestCase):
    """The Windows branch, exercised with an injected ``icacls`` runner."""

    SECRET = "FEISHU_APP_SECRET=fake-secret-value-xyz\n"

    def test_icacls_argv_uses_well_known_sids(self):
        self.assertEqual(
            secrets_file.icacls_argv(r"C:\mihomo\subscription.env"),
            ["icacls", r"C:\mihomo\subscription.env", "/inheritance:r", "/grant:r",
             "*S-1-5-18:(F)", "*S-1-5-32-544:(F)"],
        )

    def test_new_file_is_protected_then_written(self):
        seen = []

        def runner(argv):
            seen.append(list(argv))
            with open(self.path, encoding="utf-8") as handle:
                seen.append(handle.read())  # nothing secret yet when icacls runs
            return (0, "processed file", "")

        result = secrets_file.write_secret_file(self.path, self.SECRET, os_name="nt", runner=runner)
        self.assertEqual(result["protection"], "acl")
        self.assertEqual(result["argv"], secrets_file.icacls_argv(self.path))
        self.assertEqual(seen, [secrets_file.icacls_argv(self.path), ""])
        self.assertEqual(self.read(), self.SECRET)

    def test_bare_exit_code_runner_is_accepted(self):
        result = secrets_file.write_secret_file(self.path, self.SECRET, os_name="nt",
                                                runner=lambda argv: 0)
        self.assertEqual(result["protection"], "acl")
        self.assertEqual(self.read(), self.SECRET)

    def test_acl_failure_on_a_new_file_leaves_nothing_behind(self):
        with self.assertRaises(secrets_file.SecretFileError) as caught:
            secrets_file.write_secret_file(self.path, self.SECRET, os_name="nt",
                                           runner=lambda argv: (1, "", "denied"))
        self.assertEqual((caught.exception.kind, caught.exception.detail), ("acl_failed", "1"))
        self.assertFalse(os.path.exists(self.path))
        self.assertNotIn("fake-secret-value", str(caught.exception))

    def test_acl_failure_keeps_the_previous_file_untouched(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("SECRET=previous\n")
        with self.assertRaises(secrets_file.SecretFileError):
            secrets_file.write_secret_file(self.path, self.SECRET, os_name="nt",
                                           runner=lambda argv: 5)
        self.assertEqual(self.read(), "SECRET=previous\n")
        self.assertTrue(os.path.exists(self.path))

    def test_runner_exception_is_an_acl_failure(self):
        def runner(argv):
            raise OSError("icacls not found")
        with self.assertRaises(secrets_file.SecretFileError) as caught:
            secrets_file.write_secret_file(self.path, self.SECRET, os_name="nt", runner=runner)
        self.assertEqual(caught.exception.kind, "acl_failed")
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
