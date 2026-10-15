"""Explicit offline notification-root cutover through the real cold root API.

Fixture boundary: every JSON native receipt below is a trusted synthetic record
under disposable roots. It stands in for the operator's positive native closure
of all old notification senders/subprocesses; it is not native acceptance, an
independently verified OS census, an owner capability or a permission.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import hashlib
import json
import socket
import sqlite3
import subprocess
import time
from threading import Event

import pytest

from codex_watchdog import cli
from codex_watchdog import notification_cutover as cutover
from codex_watchdog import notification_root_retention as root
from codex_watchdog.models import sha256_text
from codex_watchdog.notification_receipt_retention import RetentionError, _safe, _sha
from codex_watchdog.notification_receipts import DualDeliveryReceipts
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.storage import FileLock, StoreBusyError
import test_notification_receipt_retention as receipts
from test_notification_receipt_retention import SID, NOW, PROVIDERS
from test_notification_sender_integration import busy, held_elsewhere

SCOPES = [[SID, sha256_text('fixture'), p] for p in PROVIDERS]
MISSING = object()
SOURCE = 'windows-process-census'
OBSERVED = b'1792007999.0'


@pytest.fixture(autouse=True)
def no_process_or_network(monkeypatch):
    # The cutover never stops/starts a process or contacts a provider.
    def refuse(*args, **kwargs):
        pytest.fail('cutover started a process or network operation')
    monkeypatch.setattr(subprocess, 'Popen', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(socket, 'getaddrinfo', refuse)


def dump(value):
    return json.dumps(value, sort_keys=True).encode('utf-8')


def roundtrip(value):
    return json.loads(dump(value))


def event(born, state='new'):
    return NotificationEvent('fixture', 'git_attention', state, 'Subject', 'Message',
                             source_thread_id=SID, source_born=born)


def http_context(name='http-current', born=NOW + 1):
    # A caller-verified live HTTP receipt identity inside the projected scope.
    value = receipts.ctx(name, born=born)
    value['scope_sha256'] = sha256_text('fixture')
    return roundtrip(value)


def notifier(runtime, calls, now):
    def post(token, method, payload, timeout):
        busy(runtime)
        calls.append(payload)
        return dict(channel=payload['channel'], ts='1792008000.000001')
    return EnvironmentNotifier(runtime, NotificationConfig(slack_bot_token='xoxb-fixture',
        slack_channel_id='C12345678'), slack_api_post=post, clock=lambda: now)


def foreground_busy(runtime):
    with pytest.raises(StoreBusyError):
        with FileLock(runtime / 'locks/foreground-run.lock'):
            pytest.fail('foreground exclusion absent')


@contextmanager
def foreground_held_elsewhere(runtime):
    # Same protocol as held_elsewhere, for the real foreground-run kernel lock.
    entered, release = Event(), Event()
    def worker():
        with FileLock(runtime / 'locks/foreground-run.lock'):
            entered.set()
            assert release.wait(15), 'fixture release timed out'
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        try:
            assert entered.wait(5)
            yield
        finally:
            release.set()
            future.result(timeout=5)


def snapshot(base):
    state = {}
    for path in sorted(base.rglob('*')):
        relative = path.relative_to(base)
        if 'locks' not in relative.parts:
            state[relative.as_posix()] = path.read_bytes() if path.is_file() else None
    return state


class Rig:
    def __init__(self, tmp_path):
        self.base = tmp_path
        self.runtime = self.root('notification-root')
        self.controller = self.root('controller')
        self.authority = self.root('authority')
        self.roots = (self.runtime, self.controller, self.authority)
        self.sentinels = [tmp_path / 'source.sentinel']
        for path in self.roots:
            self.sentinels += [path / 'config.sentinel.json', path / 'provider-journal.sentinel',
                               path / 'native-journal.sentinel']
        for path in self.sentinels:
            path.write_bytes(b'sentinel:' + path.name.encode() + b':' + path.parent.name.encode())
        inputs = tmp_path / 'inputs'
        inputs.mkdir()
        self.projection_path = inputs / 'projection.json'
        self.proof_path = inputs / 'quiescence.json'
        self.plan = self.runtime / 'notifications/finite-receipts-preparation.json'

    def root(self, name):
        path = self.base / name
        path.mkdir()
        return _safe(path.absolute())

    @staticmethod
    def identity(path):
        stat = path.stat()
        return [stat.st_dev, stat.st_ino]

    def request(self, contexts=(), pins=()):
        return dict(schema_version=1, notification_runtime=str(self.runtime),
            notification_runtime_id=self.identity(self.runtime),
            sender_runtimes=[dict(path=str(p), identity=self.identity(p)) for p in self.roots],
            projection=dict(scopes=SCOPES, current_keys=[], contexts=list(contexts),
                            current_pins=[list(pin) for pin in pins]))

    def proof(self, request, observed=NOW - 1):
        # Trusted synthetic fixture receipt; not native acceptance.
        return dict(schema_version=1, purpose='notification-root-cutover',
            projection_sha256=_sha(request), sender_runtimes=request['sender_runtimes'],
            all_notification_senders_stopped=True, all_notification_subprocesses_stopped=True,
            old_sender_restart_allowed=False, native_source=SOURCE,
            native_evidence_sha256=hashlib.sha256(b'synthetic fixture census').hexdigest(),
            observed_at=observed)

    def write(self, request=None, proof=None, observed=NOW - 1):
        request = roundtrip(request if request is not None else self.request())
        proof = proof if proof is not None else self.proof(request, observed)
        self.projection_path.write_bytes(dump(request))
        self.proof_path.write_bytes(dump(proof))
        return request

    def call(self, now=NOW):
        return cutover.prepare_notification_cutover(str(self.runtime), str(self.projection_path),
            str(self.proof_path), clock=lambda: now)

    def cli_args(self):
        return ['--runtime', str(self.runtime), 'notification-retention-prepare',
                '--projection', str(self.projection_path), '--quiescence', str(self.proof_path)]

    def protected(self, fingerprint):
        with closing(sqlite3.connect(self.runtime / 'notifications/dual-deliveries.sqlite3')) as db:
            return db.execute("SELECT protected FROM retention WHERE fingerprint=? AND provider='slack'",
                              (fingerprint,)).fetchone()

    def intact(self):
        return {path: path.read_bytes() for path in self.sentinels}


def test_valid_cold_entry_binds_receipt_keeps_floor_plan_and_current_http_pin(tmp_path):
    rig = Rig(tmp_path)
    before = rig.intact()
    request = rig.write()
    first = rig.call()
    assert first['status'] == 'ready'
    assert first['projection_sha256'] == _sha(request)
    assert first['native_receipt_sha256'] == hashlib.sha256(rig.proof_path.read_bytes()).hexdigest()
    assert first['native_receipt_source'] == SOURCE
    assert first['proof_scope'] == 'caller_native_receipt_not_independent_census'
    assert root.validate_notification_root(rig.runtime) is not None
    plan = rig.plan.read_bytes()

    current = http_context()
    fp = current['fingerprint']
    with DualDeliveryReceipts(rig.runtime, receipt_context=current, clock=lambda: NOW + 1) as store:
        assert store.claim(fp, 'slack')
    assert rig.protected(fp) == (0,)
    # Explicit caller-verified live HTTP pin, distinct from provider/native journal pins.
    for now in (NOW + 2, NOW + 3):
        rig.write(rig.request(contexts=[current], pins=[(fp, 'slack')]), observed=now - 1)
        repeated = rig.call(now)
        assert repeated['floor'] == first['floor'] and rig.plan.read_bytes() == plan
        assert rig.protected(fp) == (1,)
    with DualDeliveryReceipts(rig.runtime, receipt_context=current, clock=lambda: NOW + 3) as store:
        assert store.get(fp) == {'slack': 'uncertain'}
        assert not store.claim(fp, 'slack')

    calls = []
    later = notifier(rig.runtime, calls, NOW + 4)
    old = later.notify(event(NOW - 1, 'before-floor'))
    assert old.status == 'suppressed' and old.attempted_channels == () and calls == []
    assert later.notify(event(NOW + 4)).status == 'sent' and len(calls) == 1
    assert later.notify(event(NOW + 4)).status == 'suppressed' and len(calls) == 1
    assert rig.intact() == before


def proof_change(**changes):
    def apply(rig):
        request = rig.write()
        proof = rig.proof(request)
        for key, value in changes.items():
            if value is MISSING:
                del proof[key]
            else:
                proof[key] = value(request) if callable(value) else value
        rig.write(request, proof)
    return apply


def projection_change(edit):
    def apply(rig):
        request = rig.request()
        edit(rig, request)
        rig.write(request)
    return apply


def raw_change(name, edit):
    def apply(rig):
        rig.write()
        path = getattr(rig, name)
        path.write_bytes(edit(path.read_bytes()))
    return apply


def missing_proof(rig):
    rig.write()
    rig.proof_path.unlink()


def redirected_root(rig):
    # Same path, different physical directory after the projection was bound.
    rig.write()
    rig.runtime.rename(rig.base / 'detached-notification-root')
    rig.runtime.mkdir()


RECEIPT = 'old_sender_receipt_invalid'
REFUSALS = {
    'proof_purpose': (proof_change(purpose='notification-root-entry'), RECEIPT),
    'proof_schema': (proof_change(schema_version=2), RECEIPT),
    'proof_schema_bool': (proof_change(schema_version=True), RECEIPT),
    'proof_projection_digest': (proof_change(projection_sha256='0' * 64), RECEIPT),
    'proof_root_subset': (proof_change(sender_runtimes=lambda q: q['sender_runtimes'][1:]), RECEIPT),
    'proof_root_order': (proof_change(sender_runtimes=lambda q: q['sender_runtimes'][::-1]), RECEIPT),
    'proof_evidence_digest': (proof_change(native_evidence_sha256='synthetic'), RECEIPT),
    'proof_senders_running': (proof_change(all_notification_senders_stopped=False), RECEIPT),
    'proof_subprocess_flag_missing': (proof_change(all_notification_subprocesses_stopped=MISSING), RECEIPT),
    'proof_restart_allowed': (proof_change(old_sender_restart_allowed=True), RECEIPT),
    'proof_restart_unstated': (proof_change(old_sender_restart_allowed=MISSING), RECEIPT),
    'proof_source': (proof_change(native_source='operator-assertion'), RECEIPT),
    'proof_stale': (proof_change(observed_at=NOW - cutover.PROOF_SECONDS - 1), RECEIPT),
    'proof_future': (proof_change(observed_at=NOW + 1), RECEIPT),
    'proof_duplicate_key': (raw_change('proof_path',
        lambda raw: raw[:-1] + b', "purpose": "notification-root-cutover"}'), 'input_invalid'),
    'proof_nan': (raw_change('proof_path', lambda raw: raw.replace(OBSERVED, b'NaN')), 'input_invalid'),
    'proof_infinite': (raw_change('proof_path', lambda raw: raw.replace(OBSERVED, b'1e999')), 'input_invalid'),
    'proof_missing': (missing_proof, 'input_not_regular'),
    'projection_schema': (projection_change(lambda rig, q: q.update(schema_version=2)), 'projection_invalid'),
    'projection_runtime': (projection_change(
        lambda rig, q: q.update(notification_runtime=str(rig.controller))), 'projection_invalid'),
    'projection_runtime_identity': (projection_change(
        lambda rig, q: q.update(notification_runtime_id=rig.identity(rig.controller))), 'runtime_changed'),
    'projection_selected_root_omitted': (projection_change(
        lambda rig, q: q.update(sender_runtimes=q['sender_runtimes'][1:])), 'sender_roots_invalid'),
    'projection_duplicate_root': (projection_change(
        lambda rig, q: q['sender_runtimes'].append(dict(q['sender_runtimes'][1]))), 'sender_roots_invalid'),
    'projection_root_identity': (projection_change(
        lambda rig, q: q['sender_runtimes'][2].update(identity=rig.identity(rig.controller))), 'runtime_changed'),
    'projection_root_relative': (projection_change(
        lambda rig, q: q['sender_runtimes'][1].update(path='controller')), 'runtime_invalid'),
    'projection_extra_field': (projection_change(
        lambda rig, q: q['projection'].update(native_queue=[])), 'projection_invalid'),
    'projection_empty_scopes': (projection_change(
        lambda rig, q: q['projection'].update(scopes=[])), 'projection_invalid'),
    'projection_context_schema': (projection_change(
        lambda rig, q: q['projection']['contexts'].append(dict(http_context(), schema_version=2))), None),
    'projection_context_schema_missing': (projection_change(
        lambda rig, q: q['projection']['contexts'].append(
            {k: v for k, v in http_context().items() if k != 'schema_version'})), None),
    'projection_context_unscoped': (projection_change(
        lambda rig, q: q['projection']['contexts'].append(roundtrip(receipts.ctx('unscoped')))), 'projection_invalid'),
    'projection_pin_provider': (projection_change(
        lambda rig, q: q['projection']['current_pins'].append([http_context()['fingerprint'], 'email'])),
        'projection_invalid'),
    'projection_key_digest': (projection_change(
        lambda rig, q: q['projection']['current_keys'].append('not-a-digest')), 'projection_invalid'),
    'projection_duplicate_key': (raw_change('projection_path',
        lambda raw: raw[:-1] + b', "schema_version": 1}'), 'input_invalid'),
    'projection_not_object': (raw_change('projection_path', lambda raw: b'[]'), 'input_invalid'),
    'redirected_physical_root': (redirected_root, 'runtime_changed'),
}


@pytest.mark.parametrize('case', sorted(REFUSALS))
def test_invalid_receipt_or_projection_refuses_before_state_or_history_import(tmp_path, monkeypatch, case):
    rig = Rig(tmp_path)
    receipts.seed_legacy(rig.runtime, [receipts.ctx('legacy', born=NOW - 1)])
    monkeypatch.setattr(root, 'prepare_notification_receipts',
                        lambda *args, **kwargs: pytest.fail('history import after refused input'))
    mutate, match = REFUSALS[case]
    mutate(rig)
    before = snapshot(tmp_path)
    with pytest.raises(RetentionError, match=match):
        rig.call()
    assert snapshot(tmp_path) == before
    assert not rig.plan.exists()


@pytest.mark.parametrize('which', ['runtime', 'controller', 'authority'])
@pytest.mark.parametrize('lock', ['foreground', 'sender'])
def test_real_lock_contention_on_any_sender_root_refuses(tmp_path, monkeypatch, lock, which):
    rig = Rig(tmp_path)
    rig.write()
    real_prepare, entered = root.prepare_notification_receipts, []
    def prepare(*args, **kwargs):
        entered.append(True)
        return real_prepare(*args, **kwargs)
    monkeypatch.setattr(root, 'prepare_notification_receipts', prepare)
    hold = foreground_held_elsewhere if lock == 'foreground' else held_elsewhere
    before = snapshot(tmp_path)
    with hold(getattr(rig, which)):
        with pytest.raises(StoreBusyError):
            rig.call()
    assert entered == [] and snapshot(tmp_path) == before
    assert rig.call()['status'] == 'ready'


@pytest.mark.parametrize('stage', ['before_backend', 'during_backend'])
@pytest.mark.parametrize('target', ['proof_path', 'projection_path'])
def test_inputs_changed_under_exclusion_abort_then_only_explicit_forward_completion(
        tmp_path, monkeypatch, stage, target):
    rig = Rig(tmp_path)
    rig.write()
    changed_path = getattr(rig, target)
    captured = changed_path.read_bytes()
    # Same JSON value, different local bytes: the call binds exactly what it
    # validated, which is distinct from the caller's live OS proof.
    changed = json.dumps(json.loads(captured), indent=2, sort_keys=True).encode('utf-8')
    assert changed != captured and json.loads(changed) == json.loads(captured)
    real_prepare, real_fence = root.prepare_notification_receipts, cutover.sender_fence
    mutated, backend = [], []

    def mutate():
        if not mutated:
            changed_path.write_bytes(changed)
            mutated.append(stage)

    @contextmanager
    def fence(path):
        with real_fence(path):
            if stage == 'before_backend':
                mutate()
            yield

    def prepare(*args, **kwargs):
        for path in rig.roots:
            busy(path)
            foreground_busy(path)
        backend.append(True)
        result = real_prepare(*args, **kwargs)
        if stage == 'during_backend':
            mutate()
        return result

    monkeypatch.setattr(cutover, 'sender_fence', fence)
    monkeypatch.setattr(root, 'prepare_notification_receipts', prepare)
    with pytest.raises(RetentionError, match='inputs_changed'):
        rig.call()
    assert mutated == [stage]
    if stage == 'before_backend':
        assert backend == [] and not (rig.runtime / 'notifications').exists()
    else:
        assert backend == [True] and rig.plan.exists()
    plan = rig.plan.read_bytes() if rig.plan.exists() else None
    # Nothing restarted or replayed by itself; recovery is a later explicit
    # call with a fresh trusted synthetic receipt and must not replan.
    rig.write(observed=NOW + 4)
    recovered = rig.call(NOW + 5)
    if plan is not None:
        assert rig.plan.read_bytes() == plan
    assert root.validate_notification_root(rig.runtime) is not None
    rig.write(observed=NOW + 5)
    assert rig.call(NOW + 6)['floor'] == recovered['floor']
    calls = []
    later = notifier(rig.runtime, calls, NOW + 7)
    assert later.notify(event(NOW + 7)).status == 'sent' and len(calls) == 1
    assert later.notify(event(NOW + 7)).status == 'suppressed' and len(calls) == 1


def run_cli(rig, capsys):
    try:
        code = cli.main(rig.cli_args())
    except SystemExit as stop:
        code = stop.code
    return code, capsys.readouterr()


def isolate_messaging(monkeypatch):
    for name in ('SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN', 'CODEX_WATCHDOG_SLACK_BOT_TOKEN'):
        monkeypatch.setenv(name, 'xoxb-must-not-load')
    monkeypatch.setattr(EnvironmentNotifier, '__init__',
                        lambda *args, **kwargs: pytest.fail('messaging setup loaded'))


def secret_keys(value):
    if isinstance(value, dict):
        own = [key for key in value
               if any(word in str(key).lower() for word in ('token', 'secret', 'password', 'credential'))]
        return own + [key for item in value.values() for key in secret_keys(item)]
    if isinstance(value, list):
        return [key for item in value for key in secret_keys(item)]
    return []


def test_cli_prepares_and_prints_nonsecret_schema(tmp_path, monkeypatch, capsys):
    rig = Rig(tmp_path)
    before = rig.intact()
    request = rig.write(observed=time.time() - 1)
    isolate_messaging(monkeypatch)
    code, out = run_cli(rig, capsys)
    assert code in (0, None), out.err
    result = json.loads(out.out)
    assert result['status'] == 'ready'
    assert result['projection_sha256'] == _sha(request)
    assert result['native_receipt_sha256'] == hashlib.sha256(rig.proof_path.read_bytes()).hexdigest()
    assert result['native_receipt_source'] == SOURCE
    assert result['proof_scope'] == 'caller_native_receipt_not_independent_census'
    assert secret_keys(result) == [] and 'xoxb' not in out.out + out.err
    assert root.validate_notification_root(rig.runtime) is not None
    assert rig.intact() == before


def test_cli_refusal_exits_one_without_messaging_or_notification_state(tmp_path, monkeypatch, capsys):
    rig = Rig(tmp_path)
    before = rig.intact()
    request = rig.write()
    proof = rig.proof(request, observed=time.time() - 1)
    proof['old_sender_restart_allowed'] = True
    rig.write(request, proof)
    isolate_messaging(monkeypatch)
    monkeypatch.setattr(root, 'prepare_notification_receipts',
                        lambda *args, **kwargs: pytest.fail('history import after refused input'))
    code, out = run_cli(rig, capsys)
    assert code == 1
    assert not (rig.runtime / 'notifications').exists()
    assert 'xoxb' not in out.out + out.err
    assert rig.intact() == before
