'''Explicit finite notification-root cutover: cold O(H), then hot point reads.

Disposable synthetic runtimes only. nullcontext stands in for the caller's
verified old-sender exclusion; nothing here proves that a live old binary is
quiescent. Native queues, provider journals, grants and credentials appear
only as sentinel bytes that must never change.
'''
from contextlib import closing, contextmanager, nullcontext
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog import notification_root_retention as root
from codex_watchdog import relay_notification_migration as migration
from codex_watchdog.notification_receipts import DualDeliveryReceipts, FallbackDeliveryReceipts
from codex_watchdog.notification_state import CurrentNotificationState
from codex_watchdog.storage import InstructionStore, StoreBusyError
from test_notification_sender_integration import held_elsewhere

SID = '11111111-2222-4333-8444-555555555555'
NOW = 1792008000.0
PROVIDERS = ('slack', 'lark', 'smtp', 'windows_msg')
KEY, OLD, LIVE = 'a' * 64, 'b' * 64, 'e' * 64
CURRENT_FP, OLD_FP, LIVE_FP = 'c' * 64, 'd' * 64, 'f' * 64
PLAN = 'finite-receipts-preparation.json'
SIDECARS = ('-wal', '-shm', '-journal')
SENTINELS = {
    'queue/native-queue.sqlite3': b'native queue sentinel',
    'providers/slack-journal.jsonl': b'provider journal sentinel',
    'grants/bot-access.json': b'grant sentinel',
    'config/watchdog.json': b'config sentinel',
    'secrets/credential.dpapi': b'credential sentinel',
    'notifications/provider-pin-sentinel.bin': b'provider pin sentinel',
}
HOLDER = chr(10).join([
    'import sys',
    'from pathlib import Path',
    'from codex_watchdog.notification_senders import sender_fence',
    'with sender_fence(Path(sys.argv[1])):',
    '    print(sys.argv[2], flush=True)',
    '    sys.stdin.readline()',
])


def sha(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def ctx(index, born=NOW - 1, thread=SID):
    return dict(schema_version=1, fingerprint=sha(index), thread_id=thread, scope_sha256=sha(thread), born=born,
        providers={p: dict(target_sha256=sha(p + 'target'), payload_sha256=sha('payload' + str(index)))
                   for p in PROVIDERS})


def scopes(thread=SID):
    return [(thread, sha(thread), p) for p in PROVIDERS]


CURRENT, OBSOLETE = ctx('current'), ctx('obsolete', born=NOW - 2)
ACTIVE = tuple(ctx('active-' + str(i), born=NOW - 1 - i) for i in range(2))


def notes(runtime):
    return runtime / 'notifications'


def entry(fingerprint):
    return dict(fingerprint=fingerprint, recorded_at='2026-10-01T10:00:00Z')


def write(runtime, name, value):
    InstructionStore._atomic_json(notes(runtime) / name, value)


def dumps(value):
    return json.dumps(value).encode()


def leading(key, value, rest):
    '''The same JSON object with one key repeated first.'''
    return ('{' + json.dumps(key) + ': ' + json.dumps(value) + ', ' + json.dumps(rest)[1:]).encode()


def files(runtime):
    '''Persistent bytes; lock files and transient SQLite sidecars excluded.'''
    found = {}
    for path in runtime.rglob('*'):
        name = path.relative_to(runtime).as_posix()
        if path.is_file() and not name.startswith('locks/') and not name.endswith(SIDECARS):
            found[name] = path.read_bytes()
    return found


def retained(runtime, fingerprint):
    path = notes(runtime) / (finite.NAMES['dual'] + '.sqlite3')
    with closing(sqlite3.connect(path)) as db:
        return sorted((row[1], row[6]) for row in
                      db.execute('SELECT * FROM retention WHERE fingerprint=?', (fingerprint,)))


def build(kind, runtime):
    fp = CURRENT['fingerprint']
    spec = dict(keys=[], contexts=[], pins=[], relay_plan=None, suppressed=None)
    if kind == 'fresh':
        runtime.mkdir(parents=True)
    elif kind == 'legacy':
        write(runtime, 'last-events.json', dict(schema_version=1,
            last_events={KEY: entry(CURRENT_FP), OLD: entry(OLD_FP)}))
        with DualDeliveryReceipts(runtime) as store:
            assert store.claim(fp, 'slack')
            store.confirm_sent(fp, 'slack')
            assert store.claim(fp, 'lark')
            assert store.claim(OBSOLETE['fingerprint'], 'slack')
            store.confirm_sent(OBSOLETE['fingerprint'], 'slack')
        with FallbackDeliveryReceipts(runtime) as store:
            store.claim(fp, 'smtp', **CURRENT['providers']['smtp'])
        spec.update(keys=[KEY], contexts=[CURRENT], pins=[(fp, 'slack'), (fp, 'lark')],
                    suppressed=(KEY, CURRENT_FP))
    else:
        source = runtime.parent / ('old-node-' + runtime.name)
        write(source, 'last-events.json', dict(schema_version=1,
            last_events={KEY: entry(CURRENT_FP), OLD: entry(OLD_FP)}))
        write(source, 'dual-deliveries.json', dict(schema_version=1, events={
            fp: {'slack': 'sent', 'lark': 'uncertain'}, OBSOLETE['fingerprint']: {'slack': 'sent'}}))
        result = migration.apply_notification_migration(runtime, [source])
        assert result['status'] == 'migrated'
        # Compact current suppression written by the post-migration notifier.
        assert CurrentNotificationState(runtime).claim(LIVE, LIVE_FP)
        spec.update(keys=[LIVE], contexts=[CURRENT], pins=[(fp, 'slack'), (fp, 'lark')],
                    relay_plan=result['plan_sha256'], suppressed=(LIVE, LIVE_FP))
    for name, raw in SENTINELS.items():
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return spec


def prepare(runtime, spec, *, now=NOW, fence=nullcontext):
    return root.prepare_notification_root(runtime, scopes=scopes(), current_keys=spec['keys'],
        contexts=spec['contexts'], current_pins=spec['pins'], sender_fence=fence, clock=lambda: now)


@pytest.fixture(autouse=True)
def provider_calls(monkeypatch):
    '''Any socket use would be a provider/network attempt; none is allowed.'''
    attempts = []

    def refuse(*args, **kwargs):
        attempts.append(args)
        raise AssertionError('provider or network call attempted')
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    yield attempts
    assert attempts == []


@pytest.mark.parametrize('kind', ['fresh', 'legacy', 'migrated'])
def test_explicit_cold_preparation_then_read_only_hot_readiness(tmp_path, kind):
    runtime = tmp_path / 'runtime'
    spec = build(kind, runtime)
    directory = notes(runtime)
    if kind == 'migrated':
        assert all((directory / name).exists() for name in root.RETIRED)
    history = (directory / root.RETIRED[1]).read_bytes() if kind == 'migrated' else None
    assert root.read_notification_root(runtime) is None
    before = files(runtime)
    result = prepare(runtime, spec)
    assert result['status'] == 'ready' and result['legacy_history_retired'] is True
    assert result['native_or_provider_state_changed'] is False
    assert result['rollback_policy'] == 'forward_recovery_only_no_old_sender'
    after = files(runtime)
    assert {name: after.get(name) for name in SENTINELS} == SENTINELS
    assert not any((directory / name).exists() for name in root.RETIRED)
    if kind != 'fresh':
        assert not (directory / 'last-events.json').exists()
    # Nothing written or rewritten carries obsolete history, the retired
    # database bytes or an archive/backup name.
    changed = {name: raw for name, raw in after.items() if before.get(name) != raw}
    assert not any(OLD_FP.encode() in raw for raw in changed.values())
    assert not [name for name in set(after) - set(before) if any(word in name.lower()
                for word in ('history', 'backup', 'archive', 'evidence', 'relay-migration'))]
    if kind == 'legacy':
        assert not any(OLD_FP.encode() in raw for raw in after.values())
    if history is not None:
        assert history not in after.values()
    plan = json.loads((directory / PLAN).read_bytes())
    floor = result['floor']
    assert floor == plan['born'] and floor <= NOW
    ready = root.read_notification_root(runtime)
    assert (ready['floor'], ready['plan_sha256']) == (floor, result['plan_sha256'])
    if kind == 'migrated':
        assert ready['relay']['plan_sha256'] == spec['relay_plan']
    else:
        assert ready['relay'] is None
    hot = files(runtime)
    assert root.validate_notification_root(runtime, expected_relay_plan=spec['relay_plan'])['floor'] == floor
    assert root.source_eligible(runtime, floor) is False
    assert root.source_eligible(runtime, floor + 1) is True
    assert files(runtime) == hot
    for later in (NOW + 86400, NOW - 86400):
        again = prepare(runtime, spec, now=later)
        assert again['status'] == 'ready' and again['floor'] == floor
        assert files(runtime) == hot
    if spec['suppressed']:
        assert not CurrentNotificationState(runtime).claim(*spec['suppressed'])


def test_source_floor_is_strict_original_birth_and_not_reborn_by_wall_clock(tmp_path, monkeypatch):
    unprepared = tmp_path / 'never-prepared'
    assert root.read_notification_root(unprepared) is None
    assert root.source_eligible(unprepared, NOW) is None
    assert not unprepared.exists()
    runtime = tmp_path / 'runtime'
    spec = build('fresh', runtime)
    floor = prepare(runtime, spec)['floor']
    ready = (notes(runtime) / root.READY).read_bytes()
    for born in (floor, floor - 1, floor - 86400, None, 'not-a-time', float('nan'), float('inf'), float('-inf')):
        assert root.source_eligible(runtime, born) is False, born
    assert root.source_eligible(runtime, floor + 1) is True
    later = NOW + 10 ** 7
    monkeypatch.setattr(time, 'time', lambda: later)
    assert root.source_eligible(runtime, floor) is False
    assert root.source_eligible(runtime, floor + 1) is True
    assert prepare(runtime, spec, now=later)['floor'] == floor
    assert (notes(runtime) / root.READY).read_bytes() == ready


READY_DAMAGE = {
    'truncated': lambda v: dumps(v)[:20],
    'not_utf8': lambda v: bytes([0x80, 0x81, 0x7b]),
    'not_object': lambda v: dumps([v]),
    'duplicate_key': lambda v: leading('state', 'ready', v),
    'nan_floor': lambda v: dumps(dict(v, floor=float('nan'))),
    'overflow_floor': lambda v: json.dumps(dict(v, floor='OVERFLOW')).replace(json.dumps('OVERFLOW'), '1e400').encode(),
    'text_floor': lambda v: dumps(dict(v, floor='yesterday')),
    'missing_floor': lambda v: dumps({k: x for k, x in v.items() if k != 'floor'}),
    'bool_schema': lambda v: dumps(dict(v, schema_version=True)),
    'future_schema': lambda v: dumps(dict(v, schema_version=2)),
    'unready': lambda v: dumps(dict(v, state='preparing')),
    'storage': lambda v: dumps(dict(v, storage='finite_notification_root_v0')),
    'plan': lambda v: dumps(dict(v, plan_sha256='not-a-digest')),
    'relay': lambda v: dumps(dict(v, relay=dict(plan_sha256=sha('relay')))),
    'oversized': lambda v: dumps(v) + b' ' * (root.MAX_BYTES + 1 - len(dumps(v))),
}


@pytest.mark.parametrize('damage', sorted(READY_DAMAGE))
def test_damaged_ready_marker_refuses_hot_and_cold_without_repair(tmp_path, damage):
    runtime = tmp_path / 'runtime'
    spec = build('fresh', runtime)
    prepare(runtime, spec)
    path = notes(runtime) / root.READY
    path.write_bytes(READY_DAMAGE[damage](json.loads(path.read_bytes())))
    before, connections = files(runtime), []
    connect = sqlite3.connect
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(root.sqlite3, 'connect', lambda *a, **k: connections.append(a) or connect(*a, **k))
        for call in (lambda: root.read_notification_root(runtime),
                     lambda: root.source_eligible(runtime, NOW + 10 ** 6),
                     lambda: root.validate_notification_root(runtime)):
            with pytest.raises(root.RetentionError):
                call()
    assert connections == []
    with pytest.raises(root.RetentionError):
        prepare(runtime, spec, now=NOW + 60)
    assert files(runtime) == before


def test_ready_marker_at_byte_limit_remains_one_bounded_point_read(tmp_path):
    runtime = tmp_path / 'runtime'
    spec = build('fresh', runtime)
    floor = prepare(runtime, spec)['floor']
    path = notes(runtime) / root.READY
    raw = path.read_bytes()
    path.write_bytes(raw + b' ' * (root.MAX_BYTES - len(raw)))
    assert path.stat().st_size == root.MAX_BYTES
    assert root.read_notification_root(runtime)['floor'] == floor
    assert root.validate_notification_root(runtime)['floor'] == floor


@pytest.mark.parametrize('damage', ['marker_missing', 'marker_plan', 'marker_schema', 'marker_retention',
                                    'marker_duplicate', 'database_plan', 'database_missing', 'relay_expected'])
def test_finite_family_mismatch_refuses_without_repair_or_creation(tmp_path, damage):
    runtime = tmp_path / 'runtime'
    spec = build('legacy', runtime)
    prepare(runtime, spec)
    directory = notes(runtime)
    marker, ready = directory / 'dual-deliveries.json', directory / root.READY
    value = json.loads(marker.read_bytes())
    database = directory / (finite.NAMES['fallback'] + '.sqlite3')
    if damage == 'marker_missing':
        marker.unlink()
    elif damage == 'marker_plan':
        marker.write_bytes(dumps(dict(value, plan_sha256=sha('other-plan'))))
    elif damage == 'marker_schema':
        marker.write_bytes(dumps(dict(value, schema_version=2)))
    elif damage == 'marker_retention':
        marker.write_bytes(dumps(dict(value, retention='unbounded')))
    elif damage == 'marker_duplicate':
        marker.write_bytes(leading('retention', 'finite-v1', value))
    elif damage == 'database_plan':
        # Marker and root agree with each other but not with the SQLite family.
        marker.write_bytes(dumps(dict(value, plan_sha256=sha('other-plan'))))
        forged = dict(json.loads(ready.read_bytes()), plan_sha256=sha('other-plan'))
        forged['ready_sha256'] = finite._sha({k:v for k,v in forged.items() if k != 'ready_sha256'})
        ready.write_bytes(dumps(forged))
    elif damage == 'database_missing':
        database.unlink()
    expected = (root.RetentionError, sqlite3.Error) if damage == 'database_missing' else root.RetentionError
    relay = sha('other-relay') if damage == 'relay_expected' else None
    before = files(runtime)
    with pytest.raises(expected) as refused:
        root.validate_notification_root(runtime, expected_relay_plan=relay)
    if damage == 'relay_expected':
        assert 'relay_plan_mismatch' in str(refused.value)
    elif damage not in ('marker_duplicate', 'database_missing'):
        assert 'family_mismatch' in str(refused.value)
    assert files(runtime) == before
    if damage != 'relay_expected':
        with pytest.raises(expected):
            prepare(runtime, spec, now=NOW + 60)
        assert files(runtime) == before
    assert database.exists() is (damage != 'database_missing')


@pytest.mark.parametrize('damage,code', [('missing', 'intent_missing'), ('storage', 'intent_invalid'),
    ('retired_names', 'intent_invalid'), ('corrupt', 'metadata_invalid'), ('relay_collision', 'intent_collision')])
def test_cold_intent_damage_refuses_without_recreation_or_rewrite(tmp_path, damage, code):
    runtime = tmp_path / 'runtime'
    spec = build('migrated', runtime)
    floor = prepare(runtime, spec)['floor']
    directory = notes(runtime)
    intent, ready = directory / root.INTENT, directory / root.READY
    value = json.loads(intent.read_bytes())
    if damage == 'missing':
        intent.unlink()
    elif damage == 'storage':
        intent.write_bytes(dumps(dict(value, storage='notification_root_intent_v0')))
    elif damage == 'retired_names':
        intent.write_bytes(dumps(dict(value, retired={root.RETIRED[0]: None})))
    elif damage == 'corrupt':
        intent.write_bytes(dumps(value)[:15])
    else:
        other = dict(plan_sha256=sha('other-relay'), source_count=1, history_count=1, dual_count=1)
        forged = dict(json.loads(ready.read_bytes()), relay=other)
        forged['ready_sha256'] = finite._sha({k:v for k,v in forged.items() if k != 'ready_sha256'})
        ready.write_bytes(dumps(forged))
    before = files(runtime)
    with pytest.raises(root.RetentionError, match=code):
        prepare(runtime, spec, now=NOW + 60)
    assert files(runtime) == before
    assert root.read_notification_root(runtime)['floor'] == floor


RELAY_DAMAGE = {
    'schema': lambda v: dumps(dict(v, schema_version=2)),
    'state': lambda v: dumps(dict(v, state='preparing')),
    'storage': lambda v: dumps(dict(v, storage='relay_notification_history_v2')),
    'digest': lambda v: dumps(dict(v, plan_sha256='not-a-digest')),
    'count': lambda v: dumps(dict(v, history_count=-1)),
    'duplicate': lambda v: leading('state', 'ready', v),
    'corrupt': lambda v: dumps(v)[:12],
}


@pytest.mark.parametrize('damage', sorted(RELAY_DAMAGE))
def test_unknown_old_relay_metadata_refuses_before_preparation(tmp_path, damage):
    runtime = tmp_path / 'runtime'
    spec = build('migrated', runtime)
    directory = notes(runtime)
    relay = directory / root.RETIRED[0]
    relay.write_bytes(RELAY_DAMAGE[damage](json.loads(relay.read_bytes())))
    before = files(runtime)
    with pytest.raises(root.RetentionError):
        prepare(runtime, spec)
    assert files(runtime) == before
    assert not any((directory / name).exists() for name in (root.INTENT, root.READY, PLAN))
    assert root.read_notification_root(runtime) is None


def test_caller_fence_is_required_and_spans_the_whole_cold_call(tmp_path):
    runtime = tmp_path / 'runtime'
    with pytest.raises(root.RetentionError, match='sender_fence_required'):
        root.prepare_notification_root(runtime, scopes=scopes(), current_keys=[], current_pins=(), sender_fence=None,
                                       clock=lambda: NOW)
    assert not runtime.exists()
    spec = build('migrated', runtime)
    directory, observed = notes(runtime), []

    @contextmanager
    def verified_old_sender_exclusion():
        observed.append(((directory / root.READY).exists(), (directory / root.RETIRED[1]).exists()))
        yield
        observed.append(((directory / root.READY).exists(), (directory / root.RETIRED[1]).exists()))

    prepare(runtime, spec, fence=verified_old_sender_exclusion)
    assert observed == [(False, True), (True, False)]

    @contextmanager
    def old_sender_not_excluded():
        raise RuntimeError('fixture old sender still active')
        yield

    other = tmp_path / 'second'
    spec = build('legacy', other)
    before = files(other)
    with pytest.raises(RuntimeError, match='still active'):
        prepare(other, spec, fence=old_sender_not_excluded)
    assert files(other) == before


def test_common_sender_kernel_contention_refuses_before_any_mutation(tmp_path):
    runtime = tmp_path / 'runtime'
    spec = build('legacy', runtime)
    before = files(runtime)
    with held_elsewhere(runtime):
        with pytest.raises(StoreBusyError):
            prepare(runtime, spec)
    assert files(runtime) == before
    assert not any((notes(runtime) / name).exists() for name in (root.INTENT, root.READY, PLAN))
    assert root.read_notification_root(runtime) is None
    assert prepare(runtime, spec)['status'] == 'ready'


def test_cross_process_os_sender_lock_refuses_and_preserves_old_history(tmp_path):
    runtime = tmp_path / 'runtime'
    spec = build('migrated', runtime)
    before = files(runtime)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(path for path in sys.path if path))
    holder = subprocess.Popen([sys.executable, '-c', HOLDER, str(runtime), 'held'], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path))
    try:
        assert holder.stdout.readline().strip() == 'held'
        with pytest.raises(StoreBusyError):
            prepare(runtime, spec)
        assert files(runtime) == before
    finally:
        _, error = holder.communicate(input=chr(10), timeout=30)
    assert holder.returncode == 0, error
    assert prepare(runtime, spec)['status'] == 'ready'
    assert not any((notes(runtime) / name).exists() for name in root.RETIRED)


class FailReadyPublication:
    @staticmethod
    def _atomic_json(path, value):
        if Path(path).name == root.READY:
            raise OSError('fixture crash before ready publication')
        return InstructionStore._atomic_json(path, value)


def crashed_after_publication(tmp_path):
    runtime = tmp_path / 'runtime'
    spec = build('migrated', runtime)
    unlink = Path.unlink

    def interrupted(self, *args, **kwargs):
        if self.name in root.RETIRED:
            raise OSError('fixture crash after ready publication')
        return unlink(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, 'unlink', interrupted)
        with pytest.raises(OSError, match='after ready publication'):
            prepare(runtime, spec)
    return runtime, spec


def test_crash_before_ready_publication_forward_completes_original_floor(tmp_path):
    runtime = tmp_path / 'runtime'
    spec = build('migrated', runtime)
    directory = notes(runtime)
    retired = {name: (directory / name).read_bytes() for name in root.RETIRED}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(root, 'InstructionStore', FailReadyPublication)
        with pytest.raises(OSError, match='before ready publication'):
            prepare(runtime, spec)
    assert not (directory / root.READY).exists()
    intent = (directory / root.INTENT).read_bytes()
    assert {name: (directory / name).read_bytes() for name in root.RETIRED} == retired
    assert root.read_notification_root(runtime) is None
    assert root.source_eligible(runtime, NOW + 1) is None
    born = json.loads((directory / PLAN).read_bytes())['born']
    result = prepare(runtime, spec, now=NOW + 5000)
    assert result['status'] == 'ready' and result['floor'] == born
    assert (directory / root.INTENT).read_bytes() == intent
    assert not any((directory / name).exists() for name in root.RETIRED)
    assert root.validate_notification_root(runtime, expected_relay_plan=spec['relay_plan'])['floor'] == born
    assert not CurrentNotificationState(runtime).claim(*spec['suppressed'])


def test_crash_after_ready_publication_before_unlink_forward_completes(tmp_path):
    runtime, spec = crashed_after_publication(tmp_path)
    directory = notes(runtime)
    assert all((directory / name).exists() for name in root.RETIRED)
    ready = (directory / root.READY).read_bytes()
    floor = root.validate_notification_root(runtime, expected_relay_plan=spec['relay_plan'])['floor']
    assert root.source_eligible(runtime, floor) is False and root.source_eligible(runtime, floor + 1) is True
    result = prepare(runtime, spec, now=NOW - 5000)
    assert result['status'] == 'ready' and result['floor'] == floor
    assert (directory / root.READY).read_bytes() == ready
    assert not any((directory / name).exists() for name in root.RETIRED)


@pytest.mark.parametrize('name', root.RETIRED)
def test_changed_retired_source_is_refused_and_preserved(tmp_path, name):
    runtime, spec = crashed_after_publication(tmp_path)
    directory = notes(runtime)
    originals = {item: (directory / item).read_bytes() for item in root.RETIRED}
    ready = (directory / root.READY).read_bytes()
    changed = b'unrelated bytes from an unknown writer'
    (directory / name).write_bytes(changed)
    for attempt in range(2):
        with pytest.raises(root.RetentionError, match='retired_source_changed'):
            prepare(runtime, spec, now=NOW + 60 + attempt)
        assert (directory / name).read_bytes() == changed
        for other in root.RETIRED:
            if other != name and (directory / other).exists():
                assert (directory / other).read_bytes() == originals[other]
        assert (directory / root.READY).read_bytes() == ready
    assert root.validate_notification_root(runtime, expected_relay_plan=spec['relay_plan'])['state'] == 'ready'


def test_busy_retired_history_sidecar_is_refused_not_deleted(tmp_path):
    runtime, spec = crashed_after_publication(tmp_path)
    directory = notes(runtime)
    history = (directory / root.RETIRED[1]).read_bytes()
    sidecar = directory / (root.RETIRED[1] + '-wal')
    sidecar.write_bytes(b'fixture sidecar')
    with pytest.raises(root.RetentionError, match='legacy_source_busy'):
        prepare(runtime, spec, now=NOW + 60)
    assert (directory / root.RETIRED[1]).read_bytes() == history
    assert sidecar.read_bytes() == b'fixture sidecar'


def history_root(base, name, count):
    source, target = base / ('old-node-' + name), base / name
    write(source, 'last-events.json', dict(schema_version=1, last_events={
        sha('history-key-' + str(i)): entry(sha('history-fp-' + str(i))) for i in range(count)}))
    events = {sha('history-dual-' + str(i)): {'slack': 'sent'} for i in range(count)}
    events.update({c['fingerprint']: {'slack': 'uncertain'} for c in ACTIVE})
    write(source, 'dual-deliveries.json', dict(schema_version=1, events=events))
    relay = migration.apply_notification_migration(target, [source])['plan_sha256']
    with closing(sqlite3.connect(notes(target) / root.RETIRED[1])) as db:
        assert db.execute('SELECT COUNT(*) FROM history').fetchone()[0] >= count
    spec = dict(keys=[], contexts=list(ACTIVE), pins=[(c['fingerprint'], 'slack') for c in ACTIVE],
                relay_plan=relay)
    before = set(files(target))
    started = time.perf_counter()
    prepare(target, spec)
    cold = time.perf_counter() - started
    created = sum(len(raw) for item, raw in files(target).items() if item not in before)
    return target, spec, cold, created


def hot_trace(runtime, relay_plan):
    trace = dict(sql=[], vm=[0], connections=[], opened=[], read=[0])
    connect, path_open = sqlite3.connect, Path.open

    def step():
        trace['vm'][0] += 1
        return 0

    def measured(*args, **kwargs):
        db = connect(*args, **kwargs)
        trace['connections'].append(str(args[0]))
        db.set_trace_callback(trace['sql'].append)
        db.set_progress_handler(step, 1)
        return db

    class Counted:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stream.close()

        def read(self, size=-1):
            data = self.stream.read(size)
            trace['read'][0] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def opened(self, *args, **kwargs):
        stream = path_open(self, *args, **kwargs)
        trace['opened'].append(self.name)
        return Counted(stream)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(root.sqlite3, 'connect', measured)
        patch.setattr(Path, 'open', opened)
        floor = root.read_notification_root(runtime)['floor']
        assert root.source_eligible(runtime, floor) is False
        assert root.source_eligible(runtime, floor + 1) is True
        assert root.validate_notification_root(runtime, expected_relay_plan=relay_plan)['floor'] == floor
    return trace


@pytest.mark.parametrize('history', [0, 1000, 10000, 100000])
def test_hot_root_checks_have_constant_cost_for_imported_history(tmp_path, history):
    base, base_spec, _, base_created = history_root(tmp_path, 'baseline', 0)
    runtime, spec, cold, created = history_root(tmp_path, 'imported', history)
    assert root.read_notification_root(runtime)['relay']['history_count'] == history
    assert not any((notes(runtime) / name).exists() for name in root.RETIRED)
    snapshot = files(runtime)
    traces = dict(baseline=hot_trace(base, base_spec['relay_plan']), imported=hot_trace(runtime, spec['relay_plan']))
    assert files(runtime) == snapshot
    counts = {label: dict(sql=len(t['sql']), vm=t['vm'][0], connections=len(t['connections']),
                          opens=len(t['opened']), read_bytes=t['read'][0]) for label, t in traces.items()}
    print('ROOT_HOT_COUNTS', json.dumps(dict(history=history, cold_seconds=round(cold, 3),
        created_bytes=created, baseline_created_bytes=base_created, **counts), sort_keys=True))
    databases = {name + '.sqlite3' for name in finite.NAMES.values()}
    for t in traces.values():
        assert {uri.split('?')[0].rsplit('/', 1)[-1] for uri in t['connections']} == databases
        assert all('mode=ro' in uri for uri in t['connections'])
        assert set(t['opened']) <= {root.READY, 'dual-deliveries.json'}
        statements = [s.upper() for s in t['sql']]
        assert not any('COUNT(' in s or 'FROM HISTORY' in s for s in statements)
        assert not any(('FROM RECEIPTS' in s or 'FROM RETENTION' in s) and 'WHERE' not in s for s in statements)
        assert t['read'][0] <= 2 * (root.MAX_BYTES + 1)
    small, large = counts['baseline'], counts['imported']
    assert (large['sql'], large['connections'], large['opens']) == (small['sql'], small['connections'], small['opens'])
    # B-tree depth may move VM seek counts slightly; imported H may not.
    assert large['vm'] <= small['vm'] + 16
    assert abs(large['read_bytes'] - small['read_bytes']) <= 64
    assert created <= base_created + 65536


@pytest.mark.parametrize('kind', ['legacy', 'migrated'])
def test_protected_receipts_and_current_pins_survive_without_manufactured_proof(tmp_path, kind):
    runtime = tmp_path / 'runtime'
    spec = build(kind, runtime)
    prepare(runtime, spec)
    fp = CURRENT['fingerprint']
    with DualDeliveryReceipts(runtime, receipt_context=CURRENT, clock=lambda: NOW + 1) as store:
        assert store.get(fp) == {'slack': 'sent', 'lark': 'uncertain'}
        assert not store.claim(fp, 'slack') and not store.claim(fp, 'lark')
        assert store.get(fp) == {'slack': 'sent', 'lark': 'uncertain'}
    assert retained(runtime, fp) == [('lark', 1), ('slack', 1)]
    if kind == 'legacy':
        with FallbackDeliveryReceipts(runtime, receipt_context=CURRENT, clock=lambda: NOW + 1) as store:
            assert store.claim(fp, 'smtp', **CURRENT['providers']['smtp']) == 'uncertain'
    with DualDeliveryReceipts(runtime, receipt_context=OBSOLETE, clock=lambda: NOW + 1) as store:
        with pytest.raises(root.RetentionError, match='retired'):
            store.claim(OBSOLETE['fingerprint'], 'slack')
    finite.maintain_notification_receipts(runtime, scopes=scopes(), pin_changes=[], complete=True,
        sender_fence=nullcontext, clock=lambda: NOW + finite.TTL + 10)
    assert retained(runtime, fp) == [('lark', 1), ('slack', 1)]
