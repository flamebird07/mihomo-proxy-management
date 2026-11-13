import importlib.util
import json
import os
import time
import secrets
import sys
from pathlib import Path
import datetime as dt

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / 'scripts' / 'sync-feishu-bitable.py'
SOURCE1 = ROOT / 'scripts' / 'source1_independent.py'

spec = importlib.util.spec_from_file_location('sync_feishu_bitable', SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class FakeLog:
    def __init__(self):
        self.lines = []

    def info(self, message):
        self.lines.append(('INFO', message))

    def warn(self, message):
        self.lines.append(('WARN', message))

    def error(self, message):
        self.lines.append(('ERROR', message))

    def joined(self):
        return '\n'.join(f'{level} {message}' for level, message in self.lines)


class FakeClient:
    def __init__(self, rows):
        self.rows = [dict(row) for row in rows]
        self.created = []
        self.deleted = []
        self.create_result = None
        self.fail_create = False

    def list_records(self, table_id):
        return [dict(row, fields=dict(row.get('fields') or {})) for row in self.rows]

    def batch_create(self, table_id, records):
        if self.fail_create:
            raise RuntimeError('create_failed')
        if self.create_result == 0:
            return 0
        self.created.extend(records)
        for index, record in enumerate(records):
            self.rows.append({'record_id': f'new{index}', 'fields': dict(record.get('fields') or {})})
        return len(records)

    def batch_delete(self, table_id, ids):
        victim = set(ids)
        self.deleted.extend(list(ids))
        self.rows = [row for row in self.rows if row.get('record_id') not in victim]
        return len(ids)


def _row(rid, path, stamp=None, name=None):
    fields = {'测速路径': path}
    if stamp is not None:
        fields['同步时间'] = stamp
    if name is not None:
        fields['节点名'] = name
    return {'record_id': rid, 'fields': fields}


NOW = 1_762_300_000_000
MINUTE = 60 * 1000


def test_parse_numeric_ms_and_numeric_string():
    parsed, warning = module.parse_sync_ms(NOW - 1000, now_ms=NOW)
    assert warning is None and parsed == NOW - 1000
    parsed, warning = module.parse_sync_ms(str(NOW - 2000), now_ms=NOW)
    assert warning is None and parsed == NOW - 2000


def test_parse_text_utc_plus_eight_matches_naive_shanghai_without_extra_offset():
    aware = '2026-11-05 12:00:00+08:00'
    naive = '2026-11-05 12:00:00'
    zulu = '2026-11-05T04:00:00Z'
    expected = int(dt.datetime(2026, 11, 5, 4, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    now_ms = expected + 60_000
    a, wa = module.parse_sync_ms(aware, now_ms=now_ms)
    n, wn = module.parse_sync_ms(naive, now_ms=now_ms)
    z, wz = module.parse_sync_ms(zulu, now_ms=now_ms)
    assert wa is None and wn is None and wz is None
    assert a == n == z == expected
    # A mistaken extra +8h would shift naive by 8 hours.
    assert n != expected + 8 * 3600 * 1000


def test_parse_empty_future_unreadable_and_ambiguous_are_kept():
    assert module.parse_sync_ms(None, now_ms=NOW)[1] == 'empty'
    assert module.parse_sync_ms('', now_ms=NOW)[1] == 'empty'
    assert module.parse_sync_ms(NOW + 10, now_ms=NOW)[1] == 'future'
    assert module.parse_sync_ms('not-a-date', now_ms=NOW)[1] == 'unreadable'
    assert module.parse_sync_ms(1_762_300_000, now_ms=NOW)[1] == 'ambiguous'
    assert module.parse_sync_ms(True, now_ms=NOW)[1] == 'ambiguous'


@pytest.mark.parametrize('age_min,expect_stale', [(59, False), (60, False), (61, True)])
def test_independent_stale_boundary_minutes(age_min, expect_stale):
    stamp = NOW - age_min * MINUTE
    rows = [_row('s3', '源3独立兼容内核', stamp, name='hidden-should-not-appear')]
    log = FakeLog()
    plan = module.plan_table_deletions(
        rows, [], preserve_ninja=True, table_key='nodes',
        now_ms=NOW, stale_minutes=60, log=log)
    assert bool(plan) is expect_stale
    if expect_stale:
        assert plan == [{'record_id': 's3', 'path': '源3独立兼容内核', 'reason': 'stale_independent'}]
    # Node names never appear in plan summaries or skip logs.
    summary = module.summarize_delete_plan(plan)
    blob = log.joined() + str(summary)
    assert 'hidden-should-not-appear' not in blob


def test_table2_empty_payload_keeps_main_and_fresh_source1_deletes_stale_source3():
    rows = [
        _row('main1', '订阅 Mihomo', NOW - 5 * MINUTE, name='sub-node'),
        _row('s1', '源1独立Mihomo', NOW - 10 * MINUTE, name='fresh-s1'),
        _row('s3', '源3独立兼容内核', NOW - 90 * MINUTE, name='stale-s3'),
        _row('empty_time', 'Ninja Desktop', None, name='no-time'),
    ]
    client = FakeClient(rows)
    log = FakeLog()
    created = module.replace_records(
        client, 'tbl-nodes', [], log,
        preserve_ninja=True, table_key='nodes', stale_minutes=60, now_ms=NOW)
    assert created == 0
    assert client.deleted == ['s3']
    assert client.created == []
    kept = {row['record_id'] for row in client.rows}
    assert kept == {'main1', 's1', 'empty_time'}
    assert any('keep_old=1' in message for level, message in log.lines)
    assert any('sync_time_empty' in message for level, message in log.lines)
    assert 'sub-node' not in log.joined()
    assert 'stale-s3' not in log.joined()


def test_table3_regions_zero_clears_only_main_path():
    rows = [
        _row('main-empty', '', NOW - 5 * MINUTE, name='星辰新加坡1'),
        _row('s1', '源1独立Mihomo', NOW - 8 * MINUTE, name='fresh-s1-region'),
        _row('s3', '源3独立兼容内核', NOW - 3 * 3600 * 1000, name='stale-s3-region'),
    ]
    client = FakeClient(rows)
    log = FakeLog()
    created = module.replace_records(
        client, 'tbl-regions', [], log,
        preserve_ninja=True, table_key='gpt_regions', stale_minutes=60, now_ms=NOW)
    assert created == 0
    assert set(client.deleted) == {'main-empty', 's3'}
    kept = {row['record_id'] for row in client.rows}
    assert kept == {'s1'}
    summary = ' '.join(message for _, message in log.lines)
    assert 'empty_regions_clear_main' in summary
    assert 'stale_independent' in summary
    assert '星辰新加坡1' not in log.joined()
    # No placeholder row was created.
    assert client.created == []
    assert not any((row.get('fields') or {}).get('节点名') == '本轮无合格' for row in client.rows)


def test_nonempty_create_then_delete_keeps_fresh_independent():
    rows = [
        _row('old-main', '订阅 Mihomo', NOW - 40 * MINUTE, name='old-sub'),
        _row('fresh-s1', '源1独立Mihomo', NOW - 5 * MINUTE, name='fresh-s1'),
        _row('stale-s3', '源3独立兼容内核', NOW - 120 * MINUTE, name='stale-s3'),
    ]
    client = FakeClient(rows)
    log = FakeLog()
    new_records = [{'fields': {'测速路径': '订阅 Mihomo', '节点名': 'new-sub', '同步时间': NOW}}]
    created = module.replace_records(
        client, 'tbl-nodes', new_records, log,
        preserve_ninja=True, table_key='nodes', stale_minutes=60, now_ms=NOW)
    assert created == 1
    assert set(client.deleted) == {'old-main', 'stale-s3'}
    kept = {row['record_id'] for row in client.rows}
    assert 'fresh-s1' in kept
    assert 'old-main' not in kept
    assert any(row.get('record_id', '').startswith('new') for row in client.rows)


def test_create_failure_does_not_delete():
    rows = [_row('old-main', '订阅 Mihomo', NOW - 10 * MINUTE)]
    client = FakeClient(rows)
    client.fail_create = True
    log = FakeLog()
    with pytest.raises(RuntimeError, match='create_failed'):
        module.replace_records(
            client, 'tbl-nodes', [{'fields': {'测速路径': '订阅 Mihomo'}}], log,
            preserve_ninja=True, table_key='nodes', now_ms=NOW)
    assert client.deleted == []
    assert [row['record_id'] for row in client.rows] == ['old-main']


def test_create_zero_does_not_delete():
    rows = [_row('old-main', '订阅 Mihomo', NOW - 10 * MINUTE)]
    client = FakeClient(rows)
    client.create_result = 0
    log = FakeLog()
    created = module.replace_records(
        client, 'tbl-nodes', [{'fields': {'测速路径': '订阅 Mihomo'}}], log,
        preserve_ninja=True, table_key='nodes', now_ms=NOW)
    assert created == 0
    assert client.deleted == []
    assert any('create_returned_zero' in message for _, message in log.lines)


def test_dry_run_reports_counts_without_writing():
    rows = [
        _row('s3', '源3独立兼容内核', NOW - 90 * MINUTE, name='secret-node'),
        _row('main', '订阅 Mihomo', NOW - 90 * MINUTE, name='main-node'),
    ]
    client = FakeClient(rows)
    log = FakeLog()
    created = module.replace_records(
        client, 'tbl-nodes', [{'fields': {'测速路径': '订阅 Mihomo'}}], log,
        preserve_ninja=True, table_key='nodes', stale_minutes=60, now_ms=NOW, dry_run=True)
    assert created == 0
    assert client.deleted == []
    assert client.created == []
    assert [row['record_id'] for row in client.rows] == ['s3', 'main']
    text = log.joined()
    assert 'records delete dry-run' in text
    assert 'secret-node' not in text
    assert 'main-node' not in text


def test_writer_mutex_name_is_shared():
    assert module.writer_mutex_name() == module.SYNC_MUTEX_NAME
    assert not module.writer_mutex_name().endswith('.Ninja')
    sync_src = SCRIPT.read_text(encoding='utf-8')
    source1_src = SOURCE1.read_text(encoding='utf-8')
    assert 'SYNC_MUTEX_NAME + ".Ninja"' not in sync_src
    assert "SYNC_MUTEX_NAME + '.Ninja'" not in sync_src
    assert "SYNC_MUTEX_NAME'] + '.Ninja'" not in source1_src
    assert 'writer_mutex_name' in sync_src
    assert 'hold_writer_lock' in sync_src
    assert 'hold_writer_lock' in source1_src
    assert 'writer_mutex_name()' in source1_src


def test_main_and_ninja_mutex_exclude_each_other(tmp_path):
    import threading
    name = r'Local\mihomo-feishu-test-' + secrets.token_hex(8)
    log_a = FakeLog()
    log_b = FakeLog()
    lock_a = module.SyncLock(str(tmp_path / 'main.lock'), log_a, mutex_name=name)
    lock_b = module.SyncLock(str(tmp_path / 'ninja.lock'), log_b, mutex_name=name)
    outcome = []

    def other_writer():
        try:
            lock_b.acquire()
        except module.SyncError as exc:
            outcome.append(('busy', exc.kind))
            return
        outcome.append(('acquired', None))
        lock_b.release()

    lock_a.acquire()
    try:
        worker = threading.Thread(target=other_writer)
        worker.start()
        worker.join(3)
        assert not worker.is_alive()
        assert outcome == [('busy', 'lock_busy')]
    finally:
        lock_a.release()
    lock_b.acquire()
    lock_b.release()


def test_hold_writer_lock_allows_other_writer_outside_section(tmp_path):
    import threading
    name = r'Local\mihomo-feishu-test-' + secrets.token_hex(8)
    log = FakeLog()
    other = module.SyncLock(str(tmp_path / 'ninja.lock'), FakeLog(), mutex_name=name)
    # Probe/speedtest window: the write lock is not held, so Ninja can write.
    other.acquire(timeout_ms=0)
    other.release()

    def try_acquire(bucket):
        try:
            other.acquire(timeout_ms=0)
        except module.SyncError as exc:
            bucket.append(exc.kind)
            return
        other.release()
        bucket.append('acquired')

    # Same-thread mutexes are reentrant on Windows; the Ninja writer must be
    # another thread, matching the real main-vs-Ninja process split.
    outside = []
    worker = threading.Thread(target=try_acquire, args=(outside,))
    worker.start()
    worker.join(3)
    assert outside == ['acquired']

    blocked = []
    with module.hold_writer_lock(log, str(tmp_path / 'main.lock'),
                                 mutex_name=name, timeout_ms=0):
        worker = threading.Thread(target=try_acquire, args=(blocked,))
        worker.start()
        worker.join(3)
        assert not worker.is_alive()
    assert blocked == ['lock_busy']
    other.acquire(timeout_ms=0)
    other.release()


def test_lock_scope_excludes_speedtest_and_independent_probes():
    sync_src = SCRIPT.read_text(encoding='utf-8')
    source1_src = SOURCE1.read_text(encoding='utf-8')
    source3_src = (ROOT / 'scripts' / 'source3_compatible.py').read_text(encoding='utf-8')
    main_src = sync_src.split('def main(')[1].split('if __name__')[0]
    assert 'hold_writer_lock' in main_src
    assert 'lock.acquire()' not in main_src
    assert main_src.find('test_isolated(') != -1
    ninja_fn = sync_src.split('def sync_ninja_nodes(')[1].split('def select_write_keys')[0]
    assert ninja_fn.find('request_delay(') < ninja_fn.find('hold_writer_lock')
    assert ninja_fn.find('client.list_records') > ninja_fn.find('hold_writer_lock')
    assert source3_src.find('state, call = runtime(') < source3_src.find('hold_writer_lock')
    assert source3_src.find("helpers['test_isolated']") < source3_src.find('hold_writer_lock')
    assert source1_src.find('results = measure(') < source1_src.find('hold_writer_lock')


def test_sanitized_exception_log_includes_winerror(tmp_path):
    log = module.Logger(str(tmp_path / 'exc.log'))
    exc = PermissionError('denied')
    exc.winerror = 5
    # Reproduce the main() handler wording without invoking main().
    extra = f" winerror={getattr(exc, 'winerror', None)}"
    log.error(f"sync failed class={type(exc).__name__.lower()}{extra}")
    text = (tmp_path / 'exc.log').read_text(encoding='utf-8')
    assert 'class=permissionerror' in text
    assert 'winerror=5' in text
    assert 'denied' not in text


def test_cleanup_does_not_kill_fresh_temp_kernel(tmp_path, monkeypatch):
    """Fresh mihomo-feishu-* kernels must not be killed or have their dir removed.

    Option B: process killing was removed. A sibling main-sync speedtest may
    be using a fresh isolated kernel after the writer lock was narrowed.
    """
    import time
    import os

    now = time.time()
    fresh = tmp_path / 'mihomo-feishu-fresh'
    stale = tmp_path / 'mihomo-feishu-stale'
    other = tmp_path / 'not-a-corpse'
    fresh.mkdir()
    stale.mkdir()
    other.mkdir()
    os.utime(fresh, (now - 60, now - 60))
    os.utime(stale, (now - 3600, now - 3600))
    os.utime(other, (now - 3600, now - 3600))

    def boom_run(*args, **kwargs):
        raise AssertionError('subprocess.run must not kill processes')

    def boom_kill(*args, **kwargs):
        raise AssertionError('os.kill must not kill processes')

    monkeypatch.setattr(module.subprocess, 'run', boom_run)
    monkeypatch.setattr(module.os, 'kill', boom_kill)

    log = FakeLog()
    killed, removed = module.cleanup_temp_corpses(
        log, temp_roots=[str(tmp_path)], max_age_s=1800)
    assert killed == 0
    assert removed == 0
    assert fresh.exists()
    assert other.exists()
    assert stale.exists()  # no owner.json -> fail-closed, do not delete
    assert not hasattr(module, '_kill_corpse_processes')
    src = SCRIPT.read_text(encoding='utf-8')
    assert 'def _kill_corpse_processes' not in src
    assert 'Stop-Process -Id' not in src
    assert "CommandLine -match 'mihomo-feishu-'" not in src
    body = src.split('def cleanup_temp_corpses')[1]
    body = body.split('return 0, dirs_removed', 1)[0]
    assert 'subprocess.run' not in body
    assert 'os.kill(' not in body


def _age_dir(path, seconds=3600):
    past = time.time() - seconds
    os.utime(path, (past, past))


def test_cleanup_keeps_aged_dir_when_owner_alive(tmp_path):
    aged = tmp_path / 'mihomo-feishu-owned-alive'
    aged.mkdir()
    module.write_temp_dir_owner(aged)
    payload = json.loads((aged / 'owner.json').read_text(encoding='utf-8'))
    assert payload['pid'] == os.getpid()
    assert isinstance(payload['create_time'], (int, float)) and payload['create_time'] > 0
    _age_dir(aged, 3600)
    log = FakeLog()
    killed, removed = module.cleanup_temp_corpses(
        log, temp_roots=[str(tmp_path)], max_age_s=1800)
    assert killed == 0
    assert removed == 0
    assert aged.exists()
    assert any('reason=alive' in message for _, message in log.lines)


def test_cleanup_removes_aged_dir_when_owner_dead(tmp_path):
    aged = tmp_path / 'mihomo-feishu-owned-dead'
    aged.mkdir()
    (aged / 'owner.json').write_text(
        json.dumps({'pid': 1_999_999_999, 'create_time': 1.0}),
        encoding='utf-8')
    _age_dir(aged, 3600)
    log = FakeLog()
    killed, removed = module.cleanup_temp_corpses(
        log, temp_roots=[str(tmp_path)], max_age_s=1800)
    assert killed == 0
    assert removed == 1
    assert not aged.exists()
    assert any('reason=owner_dead' in message for _, message in log.lines)


def test_cleanup_keeps_aged_dir_without_owner_json(tmp_path):
    aged = tmp_path / 'mihomo-feishu-no-owner'
    aged.mkdir()
    _age_dir(aged, 3600)
    log = FakeLog()
    killed, removed = module.cleanup_temp_corpses(
        log, temp_roots=[str(tmp_path)], max_age_s=1800)
    assert killed == 0
    assert removed == 0
    assert aged.exists()
    assert any('reason=missing_owner' in message for _, message in log.lines)


def test_cleanup_removes_aged_dir_when_pid_reused(tmp_path):
    aged = tmp_path / 'mihomo-feishu-pid-reused'
    aged.mkdir()
    (aged / 'owner.json').write_text(
        json.dumps({'pid': os.getpid(), 'create_time': 1.0}),
        encoding='utf-8')
    _age_dir(aged, 3600)
    log = FakeLog()
    killed, removed = module.cleanup_temp_corpses(
        log, temp_roots=[str(tmp_path)], max_age_s=1800)
    assert killed == 0
    assert removed == 1
    assert not aged.exists()


def test_test_isolated_writes_owner_json():
    src = SCRIPT.read_text(encoding='utf-8')
    body = src.split('def test_isolated(')[1].split('def test_live(')[0]
    assert 'write_temp_dir_owner(runtime' in body
