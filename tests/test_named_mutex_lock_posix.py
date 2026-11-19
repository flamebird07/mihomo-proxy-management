"""POSIX flock named-mutex for the GPT stability switch (macOS port slice).

Runs only on POSIX hosts: on Windows the kernel32 mutex path is the real
implementation and flock does not exist.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "named_mutex_lock.py"

sys.path.insert(0, str(SCRIPT.parent))
import named_mutex_lock as nml  # noqa: E402


@unittest.skipUnless(os.name == "posix", "POSIX flock branch only")
class PosixMutexTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"NAMED_MUTEX_LOCK_DIR": self.tmp.name}
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_factory_prefers_flock_on_posix(self):
        if os.name == "nt":  # pragma: no cover
            self.skipTest("posix only")
        lock = nml.named_mutex_lock_factory(r"Local\\mihomo-gpt-stability-switch")
        self.assertIsInstance(lock, nml._PosixFileMutex)

    def test_lock_name_maps_to_a_sanitized_path(self):
        path = nml.posix_lock_path(r"Local\\mihomo-gpt-stability-switch")
        self.assertEqual(os.path.dirname(path), self.tmp.name)
        self.assertNotIn("\\", path)
        self.assertTrue(os.path.basename(path).endswith(".lock"))

    def test_second_acquire_in_same_process_is_busy(self):
        lock = nml._PosixFileMutex("busy-name", timeout_ms=150)
        with lock:
            other = nml._PosixFileMutex("busy-name", timeout_ms=150)
            with self.assertRaises(nml.LockBusy):
                with other:
                    pass  # pragma: no cover

    def test_cross_process_contention(self):
        holder = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys, time\n"
                    f"sys.path.insert(0, {str(SCRIPT.parent)!r})\n"
                    "import os\n"
                    f"os.environ['NAMED_MUTEX_LOCK_DIR'] = {self.tmp.name!r}\n"
                    "import named_mutex_lock as nml\n"
                    "lock = nml._PosixFileMutex('cross-proc', timeout_ms=5000)\n"
                    "with lock:\n"
                    "    print('held', flush=True)\n"
                    "    time.sleep(1.5)\n"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(holder.returncode, 0, holder.stderr)
        # the holder has exited: the kernel released the lock, we take over
        lock = nml._PosixFileMutex("cross-proc", timeout_ms=3000)
        with lock:
            pass  # acquired immediately - no abandoned-mutex deadlock


if __name__ == "__main__":
    unittest.main()
