"""Independent source 3 runtime; never calls a Ninja Desktop API."""
import contextlib
import json
import os
from pathlib import Path
import secrets
import hashlib
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import yaml
import ctypes
from ctypes import wintypes

ROOT = Path('C:/mihomo/source3-compatible')
LABEL = '源3独立兼容内核'
REGION_PRIORITY = {'美国': 0, '新加坡': 1, '日本': 2}


def import_ninja():
    """Explicit one-time import only. Scheduled runs do not read the desktop files."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    base = Path.home() / 'AppData/Local/NinjaDesktop'
    fingerprint = hashlib.sha256(b'windows\0amd64\0' + socket.gethostname().encode() + b'\0').hexdigest().encode()
    kek = HKDF(algorithm=hashes.SHA256(), length=32, salt=b'NinjaDesktop-KEK-v1',
               info=b'ninjadesktop-kek').derive(fingerprint)
    wrapped = (base / 'vault.key.enc').read_bytes()
    key = AESGCM(kek).decrypt(wrapped[:12], wrapped[12:], None)
    encrypted = (base / 'subscription/default/config.enc').read_bytes()
    if encrypted[:5] != b'NJVT\x01':
        raise ValueError('unsupported_vault_format')
    nodes = yaml.safe_load(AESGCM(key).decrypt(encrypted[5:17], encrypted[17:], None))['proxies']
    if not nodes or any(n.get('type') != 'ninja' for n in nodes):
        raise ValueError('unexpected_node_type')
    ROOT.mkdir(parents=True, exist_ok=True)
    if (ROOT / 'nodes.yaml').exists():
        raise FileExistsError('existing_export_kept: import into a fresh runtime only')
    shutil.copy2(base / 'inet.exe', ROOT / 'inet.exe')
    (ROOT / 'nodes.yaml').write_text(yaml.safe_dump({'proxies': nodes}, allow_unicode=True), encoding='utf-8')
    atomic_json(ROOT / 'import-manifest.json', {'count': len(nodes), 'time_ms': int(time.time()*1000),
        'core_sha256': hashlib.sha256((ROOT / 'inet.exe').read_bytes()).hexdigest()})
    print(json.dumps({'imported': len(nodes), 'directory': str(ROOT)}))


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def eligible_candidates(names, regions, passed, streaks, delays, group_from_area, required):
    eligible = [n for n in names if passed.get(n) and streaks.get(n, 0) >= required
                and group_from_area(regions.get(n, '')) in REGION_PRIORITY]
    return sorted(eligible, key=lambda n: (REGION_PRIORITY[group_from_area(regions[n])],
                                          delays.get(n) or 10**9, n))


def choose_candidate(candidates, current, regions, group_from_area):
    if not candidates:
        return 'REJECT'
    # Keep a healthy current node in the highest available tier to avoid churn.
    if current in candidates and group_from_area(regions[current]) == group_from_area(regions[candidates[0]]):
        return current
    return candidates[0]


OWN_INET_NAME = 'inet.exe'


def _creation_no_window():
    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _creation_breakaway():
    return getattr(subprocess, 'CREATE_BREAKAWAY_FROM_JOB', 0)


def _emit(log, level, message):
    writer = getattr(log, level, None) if log is not None else None
    if callable(writer):
        writer(message)


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001


def _bind_kernel32():
    """Load kernel32 once with complete OpenProcess/CloseHandle signatures.

    WinDLL defaults restype to c_int. On 64-bit Python a HANDLE is
    pointer-width, so an undeclared OpenProcess can truncate the value
    and make the same-handle terminate path unreliable. inspect and
    terminate share this bound DLL.
    """
    if sys.platform != 'win32':
        return None
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    return k32


kernel32 = _bind_kernel32()


def process_image_path(pid, *, resolver=None):
    """Return the executable path for *pid*, or None if it cannot be verified.

    On Windows this uses QueryFullProcessImageNameW with
    PROCESS_QUERY_LIMITED_INFORMATION (0x1000). A non-elevated caller can
    read the image path of a same-user process this way. Win32_Process
    CIM ExecutablePath is intentionally not used: it is often empty
    without administrator rights and would fail-closed on every reuse.
    OpenProcess/CloseHandle signatures come from the module-level kernel32.
    """
    if resolver is not None:
        return resolver(pid)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if sys.platform != 'win32':
        return None
    k32 = kernel32
    if k32 is None:
        return None
    handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return None
        return buf.value
    finally:
        k32.CloseHandle(handle)


def image_path_is_own(raw, own_exe=None):
    r"""True when *raw* is this path's inet.exe after normalization.

    The required equality is case-insensitive (normcase) and exact after
    normpath, against C:\mihomo\source3-compatible\inet.exe. resolve()
    is tried first so equivalent spellings of the same file also match.
    """
    if not raw:
        return False
    own_exe = Path(own_exe or (ROOT / OWN_INET_NAME))
    try:
        return Path(raw).resolve() == own_exe.resolve()
    except OSError:
        return os.path.normcase(os.path.normpath(raw)) == os.path.normcase(
            os.path.normpath(str(own_exe)))


def is_own_inet_pid(pid, *, own_exe=None, resolver=None):
    own_exe = Path(own_exe or (ROOT / OWN_INET_NAME))
    return image_path_is_own(process_image_path(pid, resolver=resolver), own_exe)


def pids_listening_on_port(port, *, inspector=None):
    if inspector is not None:
        return set(inspector(port))
    pids = set()
    flags = _creation_no_window()
    try:
        kwargs = {'text': True, 'errors': 'replace'}
        if flags:
            kwargs['creationflags'] = flags
        output = subprocess.check_output(['netstat', '-ano', '-p', 'tcp'], **kwargs)
    except (OSError, subprocess.CalledProcessError):
        return pids
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != 'TCP':
            continue
        if parts[3].upper() not in ('LISTENING', 'LISTEN'):
            continue
        _host, _sep, local_port = parts[1].rpartition(':')
        if local_port != str(port):
            continue
        try:
            pids.add(int(parts[-1]))
        except ValueError:
            continue
    return pids


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def text_sha256(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def fingerprints_match(stored, desired):
    """True only when both core and config hashes are present and equal."""
    if not isinstance(stored, dict) or not isinstance(desired, dict):
        return False
    core = stored.get('core_sha256')
    config = stored.get('config_sha256')
    if not core or not config:
        return False
    return core == desired.get('core_sha256') and config == desired.get('config_sha256')


def _as_int_pid(value):
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return 0
    return pid if pid > 0 else 0


def controller_identity_match(ports, stored_pid, *, pids_on_port=None,
                              resolver=None, own_exe=None):
    """Verify live process identity before reusing a controller.

    All of the following must hold; any mismatch or unreadable value
    returns (False, reason) so the caller fail-closes instead of reusing:

      1. The PID listening on the controller port (ports[1], 19993)
         equals the pid stored in runtime.json.
      2. The mixed port (ports[0], 17993) and the controller port are
         held by that same PID (each listener set is exactly {stored_pid}).
      3. That PID's executable path equals this path's inet.exe after
         case-insensitive path normalization. The path is read with
         QueryFullProcessImageNameW + PROCESS_QUERY_LIMITED_INFORMATION,
         never CIM ExecutablePath.
    """
    stored = _as_int_pid(stored_pid)
    if stored <= 0:
        return False, 'missing_stored_pid'
    if not ports or len(list(ports)) < 2:
        return False, 'invalid_ports'
    ports = list(ports)
    mixed_port, controller_port = ports[0], ports[1]
    finder = pids_on_port or (lambda port: pids_listening_on_port(port))
    try:
        mixed_pids = {_as_int_pid(pid) for pid in finder(mixed_port)}
        ctrl_pids = {_as_int_pid(pid) for pid in finder(controller_port)}
    except Exception:
        return False, 'listener_lookup_failed'
    mixed_pids.discard(0)
    ctrl_pids.discard(0)
    if stored not in ctrl_pids:
        return False, 'controller_pid_mismatch'
    if ctrl_pids != {stored}:
        return False, 'controller_port_shared'
    if mixed_pids != {stored}:
        return False, 'mixed_controller_pid_mismatch'
    if not is_own_inet_pid(stored, own_exe=own_exe, resolver=resolver):
        return False, 'image_path_unproven'
    return True, 'identity_match'


def runtime_config_payload(state, nodes):
    return {
        'mixed-port': state['port'], 'allow-lan': False, 'bind-address': '127.0.0.1',
        'mode': 'rule', 'log-level': 'warning', 'ipv6': False,
        'external-controller': f"127.0.0.1:{state['controller']}", 'secret': state['secret'],
        'tun': {'enable': False}, 'proxies': nodes,
        'proxy-groups': [{'name': 'GPT', 'type': 'select',
                          'proxies': ['REJECT'] + [n['name'] for n in nodes]}],
        'rules': ['MATCH,GPT'],
    }


def inspect_controller(call, ports, stored_pid=None, *, pids_on_port=None,
                       resolver=None, own_exe=None,
                       stored_fingerprints=None, desired_fingerprints=None):
    """Inspect the independent controller.

    Returns:
      ('reuse', None) when /version succeeds AND core+config fingerprints
        match AND live process identity matches all three checks:
          * PID listening on 19993 == runtime.json pid
          * 17993 and 19993 are held by that same PID
          * that PID's image path is this path's inet.exe
            (QueryFullProcessImageNameW + PROCESS_QUERY_LIMITED_INFORMATION;
             CIM ExecutablePath is not used)
        Any identity mismatch or unreadable path is not reused.
      ('blocked', [{'port', 'pid'}, ...]) when a process we cannot prove is
        ours occupies a port (including fingerprint or identity mismatch
        when the image path is unreadable);
      ('reap', [own_pids]) when we must (re)start: controller down, or
        fingerprints/identity mismatch, and every occupant is a proven
        own inet.exe. Only PIDs proven to belong to this path are returned.
    """
    controller_up = False
    try:
        call('/version')
        controller_up = True
    except Exception:
        pass
    finder = pids_on_port or (lambda port: pids_listening_on_port(port))
    if controller_up and fingerprints_match(stored_fingerprints, desired_fingerprints):
        ok, _reason = controller_identity_match(
            ports, stored_pid, pids_on_port=finder, resolver=resolver,
            own_exe=own_exe)
        if ok:
            return 'reuse', None
        # Fingerprints matched but live PID/port/path identity did not;
        # fall through and fail-closed or reap only proven own PIDs.
    unknown = []
    own = set()
    seen = set()
    for port in ports:
        for pid in finder(port):
            if pid in seen:
                continue
            seen.add(pid)
            if is_own_inet_pid(pid, own_exe=own_exe, resolver=resolver):
                own.add(int(pid))
            else:
                unknown.append({'port': int(port), 'pid': int(pid)})
    try:
        stored_pid = int(stored_pid) if stored_pid else 0
    except (TypeError, ValueError):
        stored_pid = 0
    if stored_pid and is_own_inet_pid(stored_pid, own_exe=own_exe, resolver=resolver):
        own.add(stored_pid)
    if unknown:
        return 'blocked', unknown
    if controller_up and not own:
        return 'blocked', [{'port': int(port), 'pid': stored_pid} for port in ports]
    return 'reap', sorted(own)


def _win32_terminate_if_own_image(pid, own_exe, *, kernel32_dll=None):
    r"""Open a PID once, prove its image path, terminate on that handle.

    Sequence:
      OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE)
      QueryFullProcessImageNameW on that same handle
      TerminateProcess on that same handle only if the path is this
      path's inet.exe (normcase-equal to C:\mihomo\source3-compatible\inet.exe)
      CloseHandle
    Open failure, an unreadable path, or a path mismatch returns False
    and does not terminate (fail-closed). CIM ExecutablePath is not used.

    Uses the module-level kernel32 (complete OpenProcess/CloseHandle
    ctypes signatures). kernel32_dll is a test hook with the same
    surface; production callers leave it unset.
    """
    k32 = kernel32 if kernel32_dll is None else kernel32_dll
    if k32 is None:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    access = _PROCESS_QUERY_LIMITED_INFORMATION | _PROCESS_TERMINATE
    handle = k32.OpenProcess(access, False, pid)
    if not handle:
        return False
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return False
        if not image_path_is_own(buf.value, own_exe):
            return False
        if not k32.TerminateProcess(handle, 1):
            return False
        return True
    finally:
        k32.CloseHandle(handle)


def terminate_verified_pids(pids, *, killer=None, resolver=None, own_exe=None):
    """Re-prove the image path at terminate time; never os.kill a bare PID.

    Windows production path (no resolver hook):
      OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE)
      QueryFullProcessImageNameW on that same handle
      if the path is this path's inet.exe:
          TerminateProcess on that same handle
      then CloseHandle
    Open failure, unreadable path, or path mismatch: skip the PID
    (fail-closed). Never looks up processes by image name.

    Tests may pass resolver= for the terminate-time image path and
    killer= as a stand-in invoked only after that path still matches.
    A changed or unreadable path must not call killer.
    """
    own_exe = Path(own_exe or (ROOT / OWN_INET_NAME))
    ended = []
    for raw_pid in pids:
        pid = _as_int_pid(raw_pid)
        if pid <= 0:
            continue
        try:
            if resolver is not None:
                if not image_path_is_own(resolver(pid), own_exe):
                    continue
                if killer is None:
                    # Matched via the hook but no killer: do not fall
                    # back to os.kill. Use the Windows same-handle path
                    # only when it is actually available.
                    if sys.platform != 'win32':
                        continue
                    if not _win32_terminate_if_own_image(pid, own_exe):
                        continue
                else:
                    killer(pid, getattr(signal, 'SIGTERM', 15))
            else:
                # Production: ignore any killer stand-in and never
                # os.kill a previously inspected bare PID.
                if sys.platform != 'win32':
                    continue
                if not _win32_terminate_if_own_image(pid, own_exe):
                    continue
            ended.append(pid)
        except OSError:
            continue
    return ended


def popen_independent_core(command, *, popen=None, log=None):
    """Start inet.exe, falling back only on PermissionError/OSError winerror==5.

    Preferred flags: CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB.
    On winerror=5, retry with CREATE_NO_WINDOW only (no breakaway). That
    child is intended to stay inside the parent Job, but in practice it may
    still outlive the scheduled task (cross-round reuse is allowed). Later
    rounds reuse that PID only when all of these hold: the authenticated
    controller is up, stored core+config fingerprints match the current
    inet.exe and generated runtime.yaml, the PID listening on 19993 equals
    the pid in runtime.json, 17993 and 19993 are that same PID, and that
    PID's image path is this path's inet.exe (read with
    QueryFullProcessImageNameW + PROCESS_QUERY_LIMITED_INFORMATION, not
    CIM ExecutablePath). Otherwise this path restarts only a PID proven
    to be our inet.exe, or fail-closes if ownership cannot be proven
    (for example the image path cannot be read).
    """
    launcher = popen or subprocess.Popen
    no_window = _creation_no_window()
    breakaway = _creation_breakaway()
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc = launcher(command, creationflags=(no_window | breakaway), **kwargs)
        return proc, {'fallback': False, 'winerror': None}
    except (PermissionError, OSError) as exc:
        winerror = getattr(exc, 'winerror', None)
        if winerror == 5:
            _emit(log, 'warn',
                  'independent core popen fallback winerror=5 fallback=yes '
                  'flags=CREATE_NO_WINDOW (job-bound; may exit when the '
                  'scheduled task or job stops)')
            proc = launcher(command, creationflags=no_window, **kwargs)
            return proc, {'fallback': True, 'winerror': 5}
        raise


def runtime(nodes, log=None, *,
                 popen=None, inspector=None, resolver=None, killer=None):
    """Only our authenticated loopback controller; no system proxy or TUN."""
    settings = ROOT / 'runtime.json'
    if settings.exists():
        state = json.loads(settings.read_text(encoding='utf-8'))
    else:
        state = {'secret': secrets.token_hex(32), 'port': 17993, 'controller': 19993}
        atomic_json(settings, state)
    api = f"http://127.0.0.1:{state['controller']}"
    def call(path, body=None):
        request = urllib.request.Request(api + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer ' + state['secret'], 'Content-Type': 'application/json'},
            method='PUT' if body is not None else 'GET')
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as r:
            return json.loads(r.read() or b'{}')
    own_exe = ROOT / OWN_INET_NAME
    config = runtime_config_payload(state, nodes)
    config_text = yaml.safe_dump(config, allow_unicode=True)
    desired = {
        'core_sha256': file_sha256(own_exe),
        'config_sha256': text_sha256(config_text),
    }
    stored_fp = {
        'core_sha256': state.get('core_sha256'),
        'config_sha256': state.get('config_sha256'),
    }
    matched = fingerprints_match(stored_fp, desired)
    status, detail = inspect_controller(
        call, (state.get('port', 17993), state.get('controller', 19993)), state.get('pid'),
        pids_on_port=inspector, resolver=resolver, own_exe=own_exe,
        stored_fingerprints=stored_fp, desired_fingerprints=desired)
    if status == 'blocked':
        ports = sorted({item['port'] for item in detail})
        if not matched:
            _emit(log, 'warn',
                  'independent fingerprint mismatch fail-closed '
                  f'core={desired["core_sha256"][:16]} '
                  f'config={desired["config_sha256"][:16]} '
                  f'stored_core={str(stored_fp.get("core_sha256") or "none")[:16]} '
                  f'stored_config={str(stored_fp.get("config_sha256") or "none")[:16]} '
                  f'unproven_ports={ports}')
        else:
            _emit(log, 'warn',
                  'independent identity fail-closed fingerprint=match '
                  f'pid={state.get("pid")} unproven_ports={ports}')
        raise RuntimeError('independent_ports_occupied_unknown')
    if status == 'reuse':
        _emit(log, 'info',
              f'independent core reused pid={state.get("pid")} '
              f'fingerprint=match identity=match '
              f'listen=17993+19993 exe={own_exe} '
              f'core={desired["core_sha256"][:16]} '
              f'config={desired["config_sha256"][:16]}')
    if status == 'reap':
        if detail:
            ended = terminate_verified_pids(
                detail, killer=killer, resolver=resolver, own_exe=own_exe)
            if stored_fp.get('core_sha256') and not matched:
                _emit(log, 'warn',
                      f'independent fingerprint mismatch restart count={len(ended)} '
                      f'core={desired["core_sha256"][:16]} '
                      f'config={desired["config_sha256"][:16]}')
            elif matched:
                _emit(log, 'warn',
                      f'independent identity mismatch restart count={len(ended)} '
                      f'pid={state.get("pid")} fingerprint=match '
                      f'core={desired["core_sha256"][:16]} '
                      f'config={desired["config_sha256"][:16]}')
            else:
                _emit(log, 'warn', f'independent stale own core reaped count={len(ended)}')
            for _ in range(20):
                busy = False
                for port in (state.get('port', 17993), state.get('controller', 19993)):
                    if pids_listening_on_port(port, inspector=inspector):
                        busy = True
                        break
                if not busy:
                    break
                time.sleep(0.1)
        config_path = ROOT / 'runtime.yaml'
        config_path.write_text(config_text, encoding='utf-8')
        command = [str(own_exe), '-d', str(ROOT), '-f', str(config_path)]
        process, meta = popen_independent_core(command, popen=popen, log=log)
        state['pid'] = process.pid
        state['popen_fallback'] = bool(meta.get('fallback'))
        if meta.get('winerror') is not None:
            state['popen_winerror'] = meta['winerror']
        state['core_sha256'] = desired['core_sha256']
        state['config_sha256'] = desired['config_sha256']
        atomic_json(settings, state)
        _emit(log, 'info',
              f'independent core started pid={process.pid} '
              f'fallback={str(bool(meta.get("fallback"))).lower()} '
              f'core={desired["core_sha256"][:16]} '
              f'config={desired["config_sha256"][:16]}')
        for _ in range(40):
            if process.poll() is not None:
                raise RuntimeError('independent_core_exited')
            try:
                call('/version')
                break
            except Exception:
                time.sleep(.25)
        else:
            raise RuntimeError('independent_core_not_ready')
    actual = call('/proxies')['proxies']
    if set(n['name'] for n in nodes) - set(actual):
        raise RuntimeError('independent_runtime_node_mismatch')
    return state, call


def sync(client, table_id, log, helpers, region_table_id=None):
    nodes = yaml.safe_load((ROOT / 'nodes.yaml').read_text(encoding='utf-8'))['proxies']
    if not nodes or len({n['name'] for n in nodes}) != len(nodes):
        raise RuntimeError('invalid_independent_nodes')
    for n in nodes:
        if n.get('dialer-proxy') or str(n.get('server', '')).lower() in ('localhost', '127.0.0.1', '::1'):
            raise RuntimeError('desktop_dependency_rejected')
    names = [n['name'] for n in nodes]
    state, call = runtime(nodes, log=log)
    delays, regions, chatgpt, openai = helpers['test_isolated'](
        str(ROOT / 'inet.exe'), names, nodes, 17994, 19994,
        helpers['DEFAULT_TARGET'], 8000, 4, log,
        include_reachability=True, total_budget_s=100, ipinfo_budget_s=230)
    stamp = int(time.time() * 1000)
    history_path = ROOT / 'gpt-history.json'
    history = json.loads(history_path.read_text(encoding='utf-8')) if history_path.exists() else {}
    streaks, passed, history = helpers['advance_gpt_history'](names, chatgpt, openai, stamp, history)
    group_from_area = lambda area: helpers['group_from_actual_area'](area, include_japan=True)
    candidates = eligible_candidates(names, regions, passed, streaks, chatgpt,
        group_from_area, helpers['GPT_REQUIRED_CONSECUTIVE'])
    # This selector belongs only to our new endpoint; existing connections are untouched.
    current = call('/proxies/GPT').get('now')
    selected = choose_candidate(candidates, current, regions, group_from_area)
    if selected != current:
        call('/proxies/GPT', {'name': selected})
    if call('/proxies/GPT').get('now') != selected:
        raise RuntimeError('independent_gpt_selection_mismatch')
    # Standard Mihomo can consume this SOCKS bridge without understanding ninja.
    bridge = {'proxies': [{'name': '源3独立GPT', 'type': 'socks5',
                          'server': '127.0.0.1', 'port': state['port'], 'udp': False}]}
    (ROOT / 'mihomo-bridge.yaml').write_text(yaml.safe_dump(bridge, allow_unicode=True), encoding='utf-8')
    records = helpers['build_node_records']([(n, 'CoffeeCloud') for n in nodes], delays,
        helpers['DEFAULT_TARGET'], stamp, regions, chatgpt, openai)
    for r in records:
        r['fields']['测速路径'] = LABEL
        if r['fields']['实际地区'] != '未知':
            r['fields']['地区检测时间'] = stamp
    hold = helpers.get('hold_writer_lock') if helpers else None
    lock_ctx = hold(log) if callable(hold) else contextlib.nullcontext()
    with lock_ctx:
        existing = client.list_records(table_id)
        client.batch_create(table_id, records)
        old = [r['record_id'] for r in existing if r.get('fields', {}).get('测速路径') in (LABEL, 'Ninja Desktop')]
        if old:
            client.batch_delete(table_id, old)
        saved = [r['fields'] for r in client.list_records(table_id) if r.get('fields', {}).get('测速路径') == LABEL]
        expected = {r['fields']['节点名']: r['fields'] for r in records}
        if len(saved) != len(nodes) or {r.get('节点名') for r in saved} != set(names):
            raise RuntimeError('independent_feishu_count_mismatch')
        for row in saved:
            want = expected[row['节点名']]
            for key in ('实际地区', '同步时间', '测速目标', '存活', '地区检测时间'):
                if row.get(key) != want.get(key):
                    raise RuntimeError('independent_feishu_field_mismatch:' + key)
        if region_table_id:
            helpers['ensure_fields'](client, region_table_id,
                [('测速路径', helpers['FT_TEXT'], None),
                 ('地区组', helpers['FT_SINGLE_SELECT'],
                  {'options': [{'name': name} for name in REGION_PRIORITY]})], log)
            region_records = helpers['build_region_records']([(n, 'CoffeeCloud') for n in nodes],
                chatgpt, {}, stamp, regions, passed, streaks, include_japan=True)
            for record in region_records:
                record['fields']['测速路径'] = LABEL
                record['fields']['代理组'] = '源3独立GPT'
            previous = [r['record_id'] for r in client.list_records(region_table_id)
                        if r.get('fields', {}).get('测速路径') == LABEL]
            if region_records:
                client.batch_create(region_table_id, region_records)
            if previous:
                client.batch_delete(region_table_id, previous)
            region_saved = [r['fields'] for r in client.list_records(region_table_id)
                            if r.get('fields', {}).get('测速路径') == LABEL]
            if ({r['节点名'] for r in region_saved if r.get('GPT可用')} != set(candidates)
                    or any(r.get('同步时间') != stamp for r in region_saved)):
                raise RuntimeError('independent_gpt_readback_mismatch')
    atomic_json(history_path, history)
    report = {'path': LABEL, 'nodes': len(saved), 'alive': sum(bool(r['存活']) for r in saved),
        'regions': sum(r['实际地区'] != '未知' for r in saved), 'chatgpt': sum(bool(v) for v in chatgpt.values()),
        'openai': sum(bool(v) for v in openai.values()), 'sync_ms': stamp,
        'candidates': candidates, 'selected': selected, 'port': state['port'],
        'selected_region': group_from_area(regions.get(selected, '')),
        'selected_streak': streaks.get(selected, 0), 'readback_verified': True}
    atomic_json(ROOT / 'last-result.json', report)
    log.info('source3 independent readback ' + json.dumps(report, ensure_ascii=False))
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--import-ninja', action='store_true')
    args = parser.parse_args()
    if args.import_ninja:
        import_ninja()
    else:
        parser.error('use sync-feishu-bitable.py --ninja-only for scheduled standalone sync')
