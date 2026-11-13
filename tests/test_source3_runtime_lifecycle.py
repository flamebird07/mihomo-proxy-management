import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'source3', Path(__file__).parents[1] / 'scripts/source3_compatible.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeProc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.exited = False

    def poll(self):
        return 1 if self.exited else None


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(('INFO', message))

    def warn(self, message):
        self.lines.append(('WARN', message))

    def error(self, message):
        self.lines.append(('ERROR', message))


class PopenIndependentCoreTests(unittest.TestCase):
    def test_winerror5_falls_back_without_breakaway(self):
        calls = []

        def popen(command, **kwargs):
            calls.append(kwargs.get('creationflags'))
            if len(calls) == 1:
                exc = PermissionError('Access is denied')
                exc.winerror = 5
                raise exc
            return FakeProc(77)

        log = FakeLog()
        proc, meta = module.popen_independent_core(['inet.exe'], popen=popen, log=log)
        self.assertEqual(proc.pid, 77)
        self.assertEqual(meta, {'fallback': True, 'winerror': 5})
        no_window = module._creation_no_window()
        breakaway = module._creation_breakaway()
        self.assertEqual(calls[0], no_window | breakaway)
        self.assertEqual(calls[1], no_window)
        self.assertEqual(calls[1] & breakaway, 0)
        self.assertTrue(any('winerror=5' in line and 'fallback=yes' in line
                            for _, line in log.lines))

    def test_oserror_winerror5_also_falls_back(self):
        calls = []

        def popen(command, **kwargs):
            calls.append(kwargs.get('creationflags'))
            if len(calls) == 1:
                exc = OSError('Access is denied')
                exc.winerror = 5
                raise exc
            return FakeProc()

        _proc, meta = module.popen_independent_core(['inet.exe'], popen=popen)
        self.assertEqual(meta['fallback'], True)
        self.assertEqual(len(calls), 2)

    def test_non5_permissionerror_is_raised(self):
        def popen(command, **kwargs):
            exc = PermissionError('denied')
            exc.winerror = 13
            raise exc

        with self.assertRaises(PermissionError) as ctx:
            module.popen_independent_core(['inet.exe'], popen=popen)
        self.assertEqual(ctx.exception.winerror, 13)

    def test_non5_oserror_is_raised(self):
        def popen(command, **kwargs):
            exc = OSError('not found')
            exc.winerror = 2
            raise exc

        with self.assertRaises(OSError) as ctx:
            module.popen_independent_core(['inet.exe'], popen=popen)
        self.assertEqual(ctx.exception.winerror, 2)
        self.assertFalse(isinstance(ctx.exception, PermissionError))

    def test_first_try_success_has_no_fallback(self):
        def popen(command, **kwargs):
            return FakeProc(9)

        proc, meta = module.popen_independent_core(['inet.exe'], popen=popen)
        self.assertEqual(proc.pid, 9)
        self.assertEqual(meta, {'fallback': False, 'winerror': None})


class ControllerLifecycleTests(unittest.TestCase):
    OWN = Path(r'C:/mihomo/source3-compatible/inet.exe')

    def test_reuse_when_controller_responds(self):
        stored = {'core_sha256': 'a' * 64, 'config_sha256': 'b' * 64}

        def call(path, body=None):
            self.assertEqual(path, '/version')
            return {'hello': 'ok'}

        def pids_on_port(port):
            return {12}

        def resolver(pid):
            return str(self.OWN) if pid == 12 else None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 12,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN,
            stored_fingerprints=stored, desired_fingerprints=stored)
        self.assertEqual(status, 'reuse')
        self.assertIsNone(detail)

    def test_fingerprint_match_pid_mismatch_not_reused(self):
        stored = {'core_sha256': 'a' * 64, 'config_sha256': 'b' * 64}

        def call(path, body=None):
            return {'hello': 'ok'}

        def pids_on_port(port):
            return {1001}

        def resolver(pid):
            return str(self.OWN) if pid == 1001 else None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 12,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN,
            stored_fingerprints=stored, desired_fingerprints=stored)
        self.assertNotEqual(status, 'reuse')
        self.assertEqual(status, 'reap')
        self.assertEqual(detail, [1001])

    def test_fingerprint_match_controller_port_stolen_not_reused(self):
        stored = {'core_sha256': 'a' * 64, 'config_sha256': 'b' * 64}

        def call(path, body=None):
            return {'hello': 'ok'}

        def pids_on_port(port):
            if port == 19993:
                return {555}
            return {1001}

        def resolver(pid):
            if pid == 1001:
                return str(self.OWN)
            if pid == 555:
                return r'C:\Windows\System32\svchost.exe'
            return None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 1001,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN,
            stored_fingerprints=stored, desired_fingerprints=stored)
        self.assertNotEqual(status, 'reuse')
        self.assertEqual(status, 'blocked')
        self.assertEqual(detail, [{'port': 19993, 'pid': 555}])

    def test_fingerprint_match_unreadable_path_not_reused(self):
        stored = {'core_sha256': 'a' * 64, 'config_sha256': 'b' * 64}

        def call(path, body=None):
            return {'hello': 'ok'}

        def pids_on_port(port):
            return {12}

        def resolver(pid):
            return None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 12,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN,
            stored_fingerprints=stored, desired_fingerprints=stored)
        self.assertNotEqual(status, 'reuse')
        self.assertEqual(status, 'blocked')
        self.assertTrue(detail)

    def test_fingerprint_mismatch_proven_own_pid_reaps(self):
        stored = {'core_sha256': 'old-core', 'config_sha256': 'old-cfg'}
        desired = {'core_sha256': 'new-core', 'config_sha256': 'new-cfg'}

        def call(path, body=None):
            return {'hello': 'ok'}

        def pids_on_port(port):
            return {1001}

        def resolver(pid):
            return str(self.OWN) if pid == 1001 else None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 1001,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN,
            stored_fingerprints=stored, desired_fingerprints=desired)
        self.assertEqual(status, 'reap')
        self.assertEqual(detail, [1001])

    def test_fingerprint_mismatch_unreadable_path_fail_closed(self):
        stored = {'core_sha256': 'old-core', 'config_sha256': 'old-cfg'}
        desired = {'core_sha256': 'new-core', 'config_sha256': 'new-cfg'}

        def call(path, body=None):
            return {'hello': 'ok'}

        def pids_on_port(port):
            return {1001}

        def resolver(pid):
            return None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 1001,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN,
            stored_fingerprints=stored, desired_fingerprints=desired)
        self.assertEqual(status, 'blocked')
        self.assertTrue(detail)

    def test_missing_fingerprints_unreadable_pid_fail_closed(self):
        desired = {'core_sha256': 'new-core', 'config_sha256': 'new-cfg'}

        def call(path, body=None):
            return {'hello': 'ok'}

        def pids_on_port(port):
            return {12}

        def resolver(pid):
            return None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 12,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN,
            stored_fingerprints=None, desired_fingerprints=desired)
        self.assertEqual(status, 'blocked')

    def test_fingerprints_match_helper(self):
        self.assertTrue(module.fingerprints_match(
            {'core_sha256': 'a', 'config_sha256': 'b'},
            {'core_sha256': 'a', 'config_sha256': 'b'}))
        self.assertFalse(module.fingerprints_match(
            {'core_sha256': 'a', 'config_sha256': 'b'},
            {'core_sha256': 'a', 'config_sha256': 'c'}))
        self.assertFalse(module.fingerprints_match(
            None, {'core_sha256': 'a', 'config_sha256': 'b'}))
        self.assertFalse(module.fingerprints_match(
            {'core_sha256': '', 'config_sha256': 'b'},
            {'core_sha256': '', 'config_sha256': 'b'}))

    def test_unknown_process_on_port_fail_closed(self):
        def call(path, body=None):
            raise RuntimeError('down')

        def pids_on_port(port):
            return {555} if port == 19993 else set()

        def resolver(pid):
            return r'C:\Windows\System32\svchost.exe'

        status, detail = module.inspect_controller(
            call, (17993, 19993), None,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN)
        self.assertEqual(status, 'blocked')
        self.assertEqual(detail, [{'port': 19993, 'pid': 555}])

    def test_reap_only_verified_own_inet_pid(self):
        def call(path, body=None):
            raise RuntimeError('down')

        def pids_on_port(port):
            return {1001} if port in (17993, 19993) else set()

        def resolver(pid):
            return str(self.OWN) if pid == 1001 else None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 1001,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN)
        self.assertEqual(status, 'reap')
        self.assertEqual(detail, [1001])
        killed = []

        def killer(pid, sig):
            killed.append((pid, sig))

        ended = module.terminate_verified_pids(
            detail, killer=killer, resolver=resolver, own_exe=self.OWN)
        self.assertEqual(ended, [1001])
        self.assertEqual([pid for pid, _ in killed], [1001])

    def test_terminate_skips_when_path_changes_after_inspect(self):
        def call(path, body=None):
            raise RuntimeError('down')

        def pids_on_port(port):
            return {1001} if port in (17993, 19993) else set()

        def resolver_inspect(pid):
            return str(self.OWN) if pid == 1001 else None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 1001,
            pids_on_port=pids_on_port, resolver=resolver_inspect,
            own_exe=self.OWN)
        self.assertEqual(status, 'reap')
        self.assertEqual(detail, [1001])
        killed = []

        def killer(pid, sig):
            killed.append((pid, sig))

        def resolver_terminate(pid):
            return r'C:\Windows\System32\notepad.exe'

        ended = module.terminate_verified_pids(
            detail, killer=killer, resolver=resolver_terminate,
            own_exe=self.OWN)
        self.assertEqual(ended, [])
        self.assertEqual(killed, [])

    def test_terminate_skips_when_path_unreadable_after_inspect(self):
        def call(path, body=None):
            raise RuntimeError('down')

        def pids_on_port(port):
            return {1001} if port in (17993, 19993) else set()

        def resolver_inspect(pid):
            return str(self.OWN) if pid == 1001 else None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 1001,
            pids_on_port=pids_on_port, resolver=resolver_inspect,
            own_exe=self.OWN)
        self.assertEqual(status, 'reap')
        self.assertEqual(detail, [1001])
        killed = []

        def killer(pid, sig):
            killed.append((pid, sig))

        def resolver_terminate(pid):
            return None

        ended = module.terminate_verified_pids(
            detail, killer=killer, resolver=resolver_terminate,
            own_exe=self.OWN)
        self.assertEqual(ended, [])
        self.assertEqual(killed, [])

    def test_two_own_pids_on_split_ports_not_reused(self):
        stored = {'core_sha256': 'a' * 64, 'config_sha256': 'b' * 64}

        def call(path, body=None):
            return {'hello': 'ok'}

        def pids_on_port(port):
            if port == 17993:
                return {1001}
            if port == 19993:
                return {1002}
            return set()

        def resolver(pid):
            return str(self.OWN) if pid in (1001, 1002) else None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 1002,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN,
            stored_fingerprints=stored, desired_fingerprints=stored)
        self.assertNotEqual(status, 'reuse')
        self.assertEqual(status, 'reap')
        self.assertEqual(detail, [1001, 1002])

    def test_terminate_without_resolver_does_not_call_killer(self):
        killed = []

        def killer(pid, sig):
            killed.append((pid, sig))

        # No resolver: production path must not os.kill / call killer on a
        # bare PID. A nonexistent PID fail-closes (OpenProcess fails).
        ended = module.terminate_verified_pids(
            [999999999], killer=killer, own_exe=self.OWN)
        self.assertEqual(ended, [])
        self.assertEqual(killed, [])

    def test_stored_own_pid_is_reaped_even_if_ports_empty(self):
        def call(path, body=None):
            raise RuntimeError('down')

        def pids_on_port(port):
            return set()

        def resolver(pid):
            return str(self.OWN) if pid == 33508 else None

        status, detail = module.inspect_controller(
            call, (17993, 19993), 33508,
            pids_on_port=pids_on_port, resolver=resolver, own_exe=self.OWN)
        self.assertEqual(status, 'reap')
        self.assertEqual(detail, [33508])

    def test_fallback_child_is_job_bound(self):
        # Fallback omits CREATE_BREAKAWAY_FROM_JOB (CREATE_NO_WINDOW only).
        # That is intended to keep the child in the parent Job, but a fallback
        # child may still outlive the scheduled task; later rounds reuse it
        # only when core+config fingerprints match AND live process identity
        # (19993 PID == runtime.json pid, same PID on 17993, proven inet.exe
        # image path via QueryFullProcessImageNameW) also matches.
        calls = []

        def popen(command, **kwargs):
            calls.append(kwargs.get('creationflags', 0))
            if len(calls) == 1:
                exc = PermissionError('denied')
                exc.winerror = 5
                raise exc
            return FakeProc()

        module.popen_independent_core(['inet.exe'], popen=popen)
        breakaway = module._creation_breakaway()
        if breakaway:
            self.assertTrue(calls[0] & breakaway)
        self.assertEqual(calls[1] & breakaway, 0)


class RecordingKernel32:
    """Stand-in kernel32 for _win32_terminate_if_own_image unit tests."""

    def __init__(self, *, handle, image_path=None, open_ok=True, query_ok=True,
                 terminate_ok=True):
        self.handle = handle
        self.image_path = image_path
        self.open_ok = open_ok
        self.query_ok = query_ok
        self.terminate_ok = terminate_ok
        self.calls = []

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(('OpenProcess', (access, inherit, pid)))
        return self.handle if self.open_ok else None

    def QueryFullProcessImageNameW(self, handle, flags, buf, size_ptr):
        self.calls.append(('QueryFullProcessImageNameW', (handle, flags)))
        if not self.query_ok:
            return 0
        path = self.image_path or ''
        buf.value = path
        try:
            size_ptr._obj.value = len(path)
        except AttributeError:
            pass
        return 1

    def TerminateProcess(self, handle, code):
        self.calls.append(('TerminateProcess', (handle, code)))
        return 1 if self.terminate_ok else 0

    def CloseHandle(self, handle):
        self.calls.append(('CloseHandle', (handle,)))
        return 1


class Win32TerminateIfOwnImageTests(unittest.TestCase):
    OWN = Path(r'C:/mihomo/source3-compatible/inet.exe')

    def _names(self, k32):
        return [name for name, _args in k32.calls]

    def _handles(self, k32, name):
        return [args[0] for call_name, args in k32.calls if call_name == name]

    def test_module_kernel32_ctypes_signatures(self):
        k32 = module.kernel32
        self.assertIsNotNone(k32)
        from ctypes import wintypes
        self.assertEqual(list(k32.OpenProcess.argtypes),
                         [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
        self.assertIs(k32.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(list(k32.CloseHandle.argtypes), [wintypes.HANDLE])
        self.assertIs(k32.CloseHandle.restype, wintypes.BOOL)

    def test_same_handle_and_call_order_on_match(self):
        handle = object()
        k32 = RecordingKernel32(handle=handle, image_path=str(self.OWN))
        ok = module._win32_terminate_if_own_image(
            4242, self.OWN, kernel32_dll=k32)
        self.assertTrue(ok)
        self.assertEqual(self._names(k32), [
            'OpenProcess', 'QueryFullProcessImageNameW',
            'TerminateProcess', 'CloseHandle'])
        access, inherit, pid = k32.calls[0][1]
        self.assertEqual(
            access,
            module._PROCESS_QUERY_LIMITED_INFORMATION | module._PROCESS_TERMINATE)
        self.assertFalse(inherit)
        self.assertEqual(pid, 4242)
        self.assertIs(k32.calls[1][1][0], handle)
        self.assertIs(k32.calls[2][1][0], handle)
        self.assertEqual(k32.calls[2][1][1], 1)
        self.assertEqual(self._handles(k32, 'CloseHandle'), [handle])
        self.assertIs(self._handles(k32, 'CloseHandle')[0], handle)

    def test_query_failure_does_not_terminate_and_closes_once(self):
        handle = object()
        k32 = RecordingKernel32(handle=handle, query_ok=False)
        ok = module._win32_terminate_if_own_image(
            4242, self.OWN, kernel32_dll=k32)
        self.assertFalse(ok)
        self.assertEqual(self._names(k32), [
            'OpenProcess', 'QueryFullProcessImageNameW', 'CloseHandle'])
        self.assertEqual(self._handles(k32, 'TerminateProcess'), [])
        self.assertEqual(self._handles(k32, 'CloseHandle'), [handle])

    def test_path_mismatch_does_not_terminate_and_closes_once(self):
        handle = object()
        k32 = RecordingKernel32(
            handle=handle, image_path=r'C:\Windows\System32\notepad.exe')
        ok = module._win32_terminate_if_own_image(
            4242, self.OWN, kernel32_dll=k32)
        self.assertFalse(ok)
        self.assertEqual(self._names(k32), [
            'OpenProcess', 'QueryFullProcessImageNameW', 'CloseHandle'])
        self.assertEqual(self._handles(k32, 'TerminateProcess'), [])
        self.assertEqual(self._handles(k32, 'CloseHandle'), [handle])

    def test_open_process_failure_does_not_close_or_terminate(self):
        k32 = RecordingKernel32(handle=object(), open_ok=False)
        ok = module._win32_terminate_if_own_image(
            4242, self.OWN, kernel32_dll=k32)
        self.assertFalse(ok)
        self.assertEqual(self._names(k32), ['OpenProcess'])
        self.assertEqual(self._handles(k32, 'CloseHandle'), [])
        self.assertEqual(self._handles(k32, 'TerminateProcess'), [])
        self.assertEqual(self._handles(k32, 'QueryFullProcessImageNameW'), [])

    def test_terminate_failure_still_closes_once(self):
        handle = object()
        k32 = RecordingKernel32(
            handle=handle, image_path=str(self.OWN), terminate_ok=False)
        ok = module._win32_terminate_if_own_image(
            4242, self.OWN, kernel32_dll=k32)
        self.assertFalse(ok)
        self.assertEqual(self._names(k32), [
            'OpenProcess', 'QueryFullProcessImageNameW',
            'TerminateProcess', 'CloseHandle'])
        self.assertEqual(self._handles(k32, 'CloseHandle'), [handle])


if __name__ == '__main__':
    unittest.main()
