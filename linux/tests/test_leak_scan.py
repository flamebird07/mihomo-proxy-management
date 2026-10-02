"""Section 11 category 4: leak scanning.

Three layers, all offline:

1. **canary sweep** - after running real lifecycle operations against a
   throwaway root, every byte this project could emit (stdout/stderr, status
   JSON, plan/degraded state, backup index, rendered config comments, unit
   file) is scanned for runtime-generated ``example.invalid`` secrets.
2. **static repo scan** - no credential-shaped literal exists anywhere in the
   repository (test fixtures use runtime tokens, docs use example.invalid).
3. **.gitignore behaviour** - the secret-bearing paths the tools *can* create
   are provably ignored via ``git check-ignore``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
import urllib.parse

import support
from mpm import lifecycle

REPO_ROOT = os.path.dirname(support.LINUX_DIR)
CANARY_HOST = "example.invalid"


def walk_text_roots():
    for base in (REPO_ROOT,):
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
            for name in filenames:
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, REPO_ROOT)
                if rel.startswith("linux" + os.sep + "tests" + os.sep):
                    continue  # the scanner's own source
                yield rel, path


class CanarySweepTests(unittest.TestCase):
    """Every artefact a real operation produces must be secret-free."""

    def setUp(self) -> None:
        self.sb = support.Sandbox(cn=True)
        self.addCleanup(self.sb.cleanup)

    def artefacts(self) -> dict[str, str]:
        layout = self.sb.layout
        texts = {
            "stdout": self.sb.out.text,
            "stderr": self.sb.err.text,
            "unit": support.read_text(layout.unit),
            "config-comments": "\n".join(
                line
                for line in support.read_text(layout.config).splitlines()
                if line.startswith("#")
            ),
            "plan.json": support.read_text(layout.plan_file),
            "degraded.json": support.read_text(layout.degraded_file),
        }
        if os.path.isfile(layout.backup_index):
            texts["backup-index.json"] = support.read_text(layout.backup_index)
        return texts

    def assert_no_canary(self, where: str, text: str) -> None:
        for canary in self.sb.canaries():
            self.assertNotIn(canary, text, f"{where} leaked {canary[:12]}...")
        # a full subscription URL (path+query) must never appear anywhere
        self.assertNotIn("/sub?token=", text, where)
        self.assertNotIn("clash=1", text, where)

    def test_install_status_test_update_cycle_is_clean(self) -> None:
        ctx = self.sb.installed()
        self.sb.transport.responses.clear()
        lifecycle.status(ctx)
        lifecycle.run_test(ctx)
        lifecycle.update_subscription(ctx)
        for name, text in self.artefacts().items():
            self.assert_no_canary(name, text)

    def test_failure_paths_are_clean(self) -> None:
        # the binary must exist so ``mihomo -t`` really runs (against the fake)
        ctx = self.sb.installed()
        # changed inputs force a re-render; the fake mihomo -t now fails and
        # its stderr is full of canaries - none of that may reach any stream
        self.sb.forget_canaries()
        self.sb.canary_secret = support.token("ctrl")
        self.sb.write_secret_file()
        self.sb.set_binary_test(
            returncode=1,
            stderr="parse failed for " + self.sb.canary_foreign + " / " + self.sb.canary_secret,
        )
        from mpm.errors import FailClosed

        with self.assertRaises(FailClosed):
            lifecycle.configure(ctx, quiet=True)
        combined = self.sb.err.text + self.sb.out.text
        for canary in (self.sb.canary_foreign, self.sb.canary_secret):
            self.assertNotIn(canary, combined)
        self.assertNotIn("/sub?token=", combined)

    def test_json_status_dump_is_clean(self) -> None:
        ctx = self.sb.installed()
        report = lifecycle.status(ctx)
        blob = json.dumps(report.to_dict(), sort_keys=True)
        self.assert_no_canary("status --json", blob)
        json.loads(blob)  # still valid JSON

    def test_state_files_store_fingerprints_not_values(self) -> None:
        ctx = self.sb.installed()
        lifecycle.configure(ctx, quiet=True)
        plan = json.loads(support.read_text(self.sb.layout.plan_file))
        for key in ("foreign_description", "cn_description"):
            value = str(plan.get(key) or "")
            self.assertIn("#fp", value, key)  # scheme://host#fp<hash>
            self.assert_no_canary(f"plan.{key}", value)

    def test_host_registry_covers_canaries(self) -> None:
        from mpm.sanitize import HOST

        self.sb.installed()
        for canary in self.sb.canaries():
            # the sanitizer, fed the raw canary, must mask it via the registry
            self.assertNotIn(canary, HOST.text("probe " + canary))

    def test_controller_error_is_clean_through_the_cli_streams(self) -> None:
        """D7/D16: a percent-encoded probe URL is invisible to the sanitizer.

        ``delay()`` puts the CN health-check URL into the query string, so the
        controller's non-2xx message must carry the endpoint only.  The message
        is pushed through the real CLI output paths (JSON + stderr warning) and
        scanned for the raw *and* percent-encoded canary values.
        """
        from mpm import cli
        from mpm import controller as controller_mod

        ctx = self.sb.installed()
        self.sb.transport.responses["GET /proxies/CN-EXIT/delay"] = (
            502,
            b'{"error":"probe rejected"}',
        )
        controller = controller_mod.Controller(
            secret=self.sb.canary_secret, transport=self.sb.transport
        )
        with self.assertRaises(controller_mod.ControllerError) as caught:
            controller.delay("CN-EXIT", url=self.sb.canary_health)
        message = str(caught.exception)

        report = lifecycle.Report(command="test", status="FAILED")
        report.add(message)
        report.warn(message)
        cli.emit(ctx, report, as_json=True)
        cli.emit(ctx, report, as_json=False)

        texts = self.artefacts()
        texts["exception"] = message
        for name, text in texts.items():
            self.assert_no_canary(name, text)
            for canary in self.sb.canaries():
                encoded = urllib.parse.quote(canary, safe="")
                self.assertNotIn(encoded, text, f"{name} percent-encoded")
                for part in (canary.split("://", 1)[-1], canary.split("?", 1)[-1]):
                    self.assertNotIn(urllib.parse.quote(part, safe=""), text, name)
        # still debuggable: endpoint and status, nothing else
        self.assertIn("/proxies/CN-EXIT/delay", message)
        self.assertNotIn("?", message)


class StaticRepoScanTests(unittest.TestCase):
    """Nothing credential-shaped is hardcoded anywhere in the repo."""

    PATTERNS = [
        re.compile(r"sub\?token=[A-Za-z0-9-]{8,}"),
        re.compile(r"//[^/\s\"'@]{8,}@"),  # userinfo in a URL
        re.compile(r"(?i)(secret|token|password|passwd|apikey|api_key)\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']"),
        re.compile(r"(?i)sk-[A-Za-z0-9]{20,}"),
        re.compile(r"sha256:[0-9a-f]{64}", re.I),  # ok in lock file only
    ]
    DIGEST_ALLOWED = {
        os.path.join("linux", "supply", "mihomo.lock.json"),
    }

    def test_no_hardcoded_credentials(self) -> None:
        hits = []
        for rel, path in walk_text_roots():
            try:
                with open(path, "r", encoding="utf-8", errors="strict") as handle:
                    text = handle.read()
            except (UnicodeDecodeError, OSError):
                continue
            for pattern in self.PATTERNS:
                if pattern.pattern.startswith("sha256:") and rel in self.DIGEST_ALLOWED:
                    continue
                for match in pattern.finditer(text):
                    value = match.group(0)
                    # placeholders in examples/docs are fine
                    if "your-token" in value or "<redacted>" in value:
                        continue
                    # assignment to an identifier constant (env var *name*) is
                    # not a credential: SECRET = "MPM_CONTROLLER_SECRET"
                    quoted = re.search("""['"]([^'"]*)['"]\\s*$""", value)
                    if quoted and re.fullmatch(r"[A-Z][A-Z0-9_]*", quoted.group(1)):
                        continue
                    if pattern.pattern.startswith("sha256:") and "example" in text.lower():
                        continue
                    hits.append(f"{rel}: {match.group(0)[:48]!r}")
        self.assertFalse(hits, "hardcoded credential-shaped strings:\n" + "\n".join(hits))

    def test_docs_only_use_canary_hosts(self) -> None:
        with open(os.path.join(REPO_ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        # no real subscription-looking URL with a query credential
        self.assertNotRegex(readme, r"token=[A-Za-z0-9]{6,}")

    def test_runtime_canaries_never_appear_in_repo(self) -> None:
        # a live sandbox's canaries are runtime values: none of them may be
        # present anywhere in the repository (fixtures included)
        sb = support.Sandbox()
        self.addCleanup(sb.cleanup)
        canaries = sb.canaries()
        for rel, path in walk_text_roots():
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for canary in canaries:
                self.assertNotIn(canary, text, f"{rel} contains {canary[:12]}...")


class GitIgnoreBehaviourTests(unittest.TestCase):
    """D15: the ignore rules are verified with real ``git check-ignore``."""

    CASES_IGNORED = [
        "subscription.env",
        "install_dir/subscription.env",
        "config/subscription.env",
        "deep/nested/subscription.env",
        "install_dir/config.yaml",
        "config/config.yaml",
        "providers/subscription.yaml",
        "install_dir/providers/subscription.yaml",
        "install_dir/GeoIP.dat",
        "install_dir/logs/mihomo.log",
        "install_dir/cache.db",
        "local.env",
        "linux/python/mpm/__pycache__/cli.cpython-312.pyc",
    ]
    CASES_TRACKED = [
        ".gitignore",
        "config/subscription.env.example",
        "config/config.yaml.template",
        "providers/.gitkeep",
        "linux/supply/mihomo.lock.json",
        "docs/ARCHITECTURE.md",
    ]

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", REPO_ROOT, *args], capture_output=True, text=True
        )

    def setUp(self) -> None:
        probe = self.git("rev-parse", "--git-dir")
        if probe.returncode != 0:
            self.skipTest("git NOT_RUN: repository not available for check-ignore")

    def test_secret_paths_are_ignored(self) -> None:
        for path in self.CASES_IGNORED:
            with self.subTest(path=path):
                result = self.git("check-ignore", "-v", "--no-index", path)
                self.assertEqual(result.returncode, 0, f"{path} NOT ignored: {result.stdout}{result.stderr}")

    def test_template_and_example_files_stay_visible(self) -> None:
        for path in self.CASES_TRACKED:
            with self.subTest(path=path):
                result = self.git("check-ignore", "--no-index", path)
                self.assertNotEqual(result.returncode, 0, f"{path} must not be ignored")

    def test_no_secret_file_is_currently_tracked(self) -> None:
        listed = self.git("ls-files")
        self.assertEqual(listed.returncode, 0)
        tracked = listed.stdout.split()
        for path in tracked:
            base = os.path.basename(path)
            self.assertFalse(base.endswith(".env"), path)
            self.assertFalse(base in ("config.yaml", "GeoIP.dat", "GeoSite.dat"), path)
        ignored = self.git("ls-files", "--ignored", "--exclude-standard")
        self.assertEqual(ignored.stdout.strip(), "", "tracked files must not be ignored")


class ConvertSubRedactionTests(unittest.TestCase):
    """The Windows/shared converter script must not echo credentials (D16-2)."""

    CONVERT = os.path.join(REPO_ROOT, "scripts", "convert_sub.py")

    def run_convert(self, args, *, input_text=None):
        import sys

        return subprocess.run(
            [sys.executable, self.CONVERT, *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def sub_file(self, token: str) -> str:
        import base64
        import tempfile

        payload = (
            f"anytls://uuid-{token}@node1.hk.provider.example.invalid:443"
            f"?type=tcp&fp=chrome#HK-1\n"
            f"hysteria2://pw-{token}@node2.us.provider.example.invalid:8443#US-2\n"
        )
        fd, path = tempfile.mkstemp(prefix="mpm-sub-", suffix=".b64")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(base64.b64encode(payload.encode()).decode())
        self.addCleanup(lambda: os.unlink(path))
        return path

    def test_list_hides_endpoints_by_default(self) -> None:
        token = support.token("cred")
        result = self.run_convert(["-i", self.sub_file(token), "--list", "-q"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertNotIn("node1.hk", result.stdout)
        self.assertIn("HK-1", result.stdout)

    def test_opt_in_endpoints_are_masked(self) -> None:
        token = support.token("cred")
        result = self.run_convert(["-i", self.sub_file(token), "--list", "--show-endpoints", "-q"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertNotIn("node1.hk", result.stdout)
        self.assertIn("***.example.invalid:443", result.stdout)

    def test_fetch_failure_does_not_echo_url_credential(self) -> None:
        token = support.token("cred")
        result = self.run_convert(
            ["-u", f"https://provider.example.invalid/link?token={token}", "--list"]
        )
        self.assertEqual(result.returncode, 1)  # unreachable canary host
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertIn("https://provider.example.invalid", result.stderr)


if __name__ == "__main__":
    unittest.main()
