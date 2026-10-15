"""Finite observer receipts have no native UUID, route, grant or reply parent."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
import json
import sqlite3

import pytest

from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog.notification_context import observer_scope, receipt_context
from codex_watchdog.notification_receipts import DualDeliveryReceipts, FallbackDeliveryReceipts
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.lark_transport import LarkConfig
from codex_watchdog.relay import RelayTarget
from codex_watchdog.session_routes import SessionRoutes
from test_notification_receipt_retention import ctx, sha, NOW, SID, PROVIDERS

WORKSPACE = 'fixture-discovery-observer'


def event(index=0, born=NOW+1):
    return NotificationEvent(WORKSPACE, 'workspace_discovery_attention', str(index),
        'Fixture attention', 'Verified current observer condition', source_born=born, source_observer=True)


def observer(index=0, born=NOW+1):
    return receipt_context(event(index,born), {p:dict(target_sha256=sha(p+'target'),
        payload_sha256=sha('payload'+str(index))) for p in PROVIDERS})


def scopes():
    subject, scope = observer_scope(WORKSPACE)
    return [(subject,scope,p) for p in PROVIDERS]


def prepare(runtime, contexts=()):
    return finite.prepare_notification_receipts(runtime, scopes=scopes(), contexts=contexts,
        sender_fence=nullcontext, clock=lambda: NOW)


def open_store(runtime, provider, value, clock=NOW+1):
    cls = DualDeliveryReceipts if provider in ('slack','lark') else FallbackDeliveryReceipts
    return cls(runtime, receipt_context=value, clock=lambda:clock)


def claim(store, provider, value):
    if provider in ('slack','lark'):
        return store.claim(value['fingerprint'],provider)
    return store.claim(value['fingerprint'],provider,**value['providers'][provider])


@pytest.mark.parametrize('provider',PROVIDERS)
def test_disjoint_observer_claim_is_once_and_has_original_birth(tmp_path,provider):
    prepare(tmp_path)
    value=observer()
    assert value['schema_version']==2 and value['subject_kind']=='observer' and 'thread_id' not in value
    with open_store(tmp_path,provider,value) as store:
        assert claim(store,provider,value) in (True,None)
    with open_store(tmp_path,provider,value) as store:
        assert claim(store,provider,value)==(False if provider in ('slack','lark') else 'uncertain')
        row=store.finite._row(value['fingerprint'],provider)
        assert row[:2]==observer_scope(WORKSPACE)
        assert row[2:4]==(NOW+1,NOW+1+finite.TTL)


@pytest.mark.parametrize('provider',PROVIDERS)
def test_original_callback_cannot_rebirth_after_gc_and_clock_rollback(tmp_path,provider):
    prepare(tmp_path);value=observer()
    with open_store(tmp_path,provider,value) as store:claim(store,provider,value)
    finite.maintain_notification_receipts(tmp_path,scopes=scopes(),complete=True,
        sender_fence=nullcontext,clock=lambda:NOW+finite.TTL+2)
    with open_store(tmp_path,provider,value,clock=NOW+2) as store:
        assert store.finite._row(value['fingerprint'],provider) is None
        with pytest.raises(finite.RetentionError,match='retired'):
            claim(store,provider,value)


@pytest.mark.parametrize('fault',['native_id','wrong_scope','wrong_kind','bool_version','observer_in_native'])
def test_subject_protocols_cannot_be_confused(fault):
    value=observer()
    if fault=='native_id':value['thread_id']=SID
    elif fault=='wrong_scope':value['observer_scope_sha256']=sha('different')
    elif fault=='wrong_kind':value['subject_kind']='native'
    elif fault=='bool_version':value['schema_version']=True
    else:value=dict(ctx(),thread_id=observer_scope(WORKSPACE)[0])
    with pytest.raises(finite.RetentionError):finite.context(value)


def test_observer_event_cannot_target_native_or_activation():
    with pytest.raises(ValueError,match='observer'):
        replace(event(),relay_target=RelayTarget(WORKSPACE,SID,'process_local'))
    with pytest.raises(ValueError,match='observer'):
        replace(event(),source_thread_id=SID)
    with pytest.raises(ValueError,match='observer'):
        replace(event(),provider_filter=('slack',))
    assert receipt_context(event(born=None),observer()['providers']) is None


def test_native_and_observer_receipts_coexist_without_cross_scope_retirement(tmp_path):
    native=ctx()
    all_scopes=scopes()+[(SID,sha(SID),p) for p in PROVIDERS]
    finite.prepare_notification_receipts(tmp_path,scopes=all_scopes,contexts=(),
        sender_fence=nullcontext,clock=lambda:NOW)
    for value in (native,observer()):
        with open_store(tmp_path,'slack',value) as store:assert claim(store,'slack',value)
    finite.maintain_notification_receipts(tmp_path,scopes=scopes(),complete=True,
        sender_fence=nullcontext,clock=lambda:NOW+finite.TTL+2)
    with open_store(tmp_path,'slack',native) as store:
        assert store.finite._row(native['fingerprint'],'slack') is not None
    with open_store(tmp_path,'slack',observer()) as store:
        assert store.finite._row(observer()['fingerprint'],'slack') is None


def test_observer_cannot_be_pinned_as_native_work(tmp_path):
    value=observer(born=NOW-1)
    with pytest.raises(finite.RetentionError,match='observer_pin_invalid'):
        finite.prepare_notification_receipts(tmp_path,scopes=scopes(),contexts=[value],
            current_pins=[(value['fingerprint'],'slack')],sender_fence=nullcontext,clock=lambda:NOW)
    assert not (tmp_path/'notifications').exists()
    prepare(tmp_path);value=observer()
    with open_store(tmp_path,'slack',value) as store:assert claim(store,'slack',value)
    result=finite.maintain_notification_receipts(tmp_path,scopes=scopes(),complete=True,
        pin_changes=[(value['fingerprint'],'slack',True)],sender_fence=nullcontext,clock=lambda:NOW+2)
    assert result['status']=='partial' and result['families']['dual']['status']=='unavailable'
    assert result['families']['fallback']['status']=='maintained'
    with open_store(tmp_path,'slack',value) as store:
        assert store.finite._row(value['fingerprint'],'slack')[4]==0


def test_explicit_cold_projection_keeps_a_verified_observer_claim_and_native_protocol(tmp_path):
    value=observer(born=NOW-1)
    with DualDeliveryReceipts(tmp_path) as store:
        assert store.claim(value['fingerprint'],'slack')
    result=prepare(tmp_path,contexts=[value])
    assert result['retained']['dual']==1
    with open_store(tmp_path,'slack',value) as store:
        assert not claim(store,'slack',value)
    assert finite.context(ctx())['thread_id']==SID


def notifier(runtime,*,prepare_finite=True):
    calls=[];clock=[NOW+2]
    config=NotificationConfig(slack_bot_token='xoxb-fixture',slack_channel_id='C12345678',
        interactive_transport='both',lark=LarkConfig('cli_'+'a'*20,'fixture-secret',
            'oc_fixture000000001',('ou_fixture0000001',)))
    def post(token,method,payload,timeout):
        calls.append(('slack',dict(payload)))
        return dict(channel=payload['channel'],ts=str(int(clock[0]))+'.000001')
    class Api:
        def send(self,text,fingerprint,**kwargs):
            calls.append(('lark',text))
            return dict(chat_id=config.lark.chat_id,message_id='om_fixture00000001')
    result=EnvironmentNotifier(runtime,config,slack_api_post=post,clock=lambda:clock[0])
    result.lark_api=Api()
    if prepare_finite:prepare(runtime)
    return result,calls,clock


def test_actual_notifier_dual_send_has_no_native_lookup_or_second_lark_ledger(tmp_path,monkeypatch):
    sender,calls,clock=notifier(tmp_path)
    monkeypatch.setattr(SessionRoutes,'destination',lambda *a:pytest.fail('observer attempted native route'))
    monkeypatch.setattr(sender.lark_thread_store,'prepare_notification',lambda *a,**k:pytest.fail('unscoped secondary history'))
    monkeypatch.setattr(sender.lark_thread_store,'finish_notification',lambda *a,**k:pytest.fail('observer created a reply parent'))
    original=event()
    assert sender.notify(original).provider_outcomes=={'slack':'sent','lark':'sent'}
    assert len(calls)==2
    assert sender.notify(original).duplicate and len(calls)==2
    assert not (tmp_path/'slack/reply-tickets.sqlite3').exists()
    assert not (tmp_path/'lark/reply-tickets.sqlite3').exists()
    with sqlite3.connect(tmp_path/'notifications/dual-deliveries.sqlite3') as db:
        assert db.execute('SELECT thread_id,born FROM retention ORDER BY provider').fetchall()==[
            (observer_scope(WORKSPACE)[0],NOW+1)]*2


def test_unprepared_observer_never_uses_legacy_send_path(tmp_path):
    # Existing unprepared state must refuse; a genuinely absent namespace now
    # has its separately tested first-install initialization path.
    (tmp_path/'notifications').mkdir()
    sender,calls,_=notifier(tmp_path,prepare_finite=False)
    result=sender.notify(event())
    assert result.attempted_channels==() and not calls


@pytest.mark.parametrize('history',[0,1000,10000])
@pytest.mark.parametrize('born',[None,NOW+1])
def test_observer_refuses_before_legacy_history_work_or_conversion(tmp_path,monkeypatch,history,born):
    from pathlib import Path
    sender,calls,_=notifier(tmp_path,prepare_finite=False)
    path=tmp_path/'notifications/dual-deliveries.json';path.parent.mkdir(exist_ok=True)
    original=json.dumps(dict(schema_version=1,events={sha('past-'+str(n)):dict(slack='uncertain')
        for n in range(history)})).encode();path.write_bytes(original)
    read,connect=Path.read_bytes,sqlite3.connect
    def no_history_read(candidate):
        if candidate==path:pytest.fail('observer tried reading/importing legacy history')
        return read(candidate)
    def no_legacy_connect(database,*args,**kwargs):
        if 'dual-deliveries.sqlite3' in str(database):pytest.fail('unprepared observer opened legacy SQL')
        return connect(database,*args,**kwargs)
    monkeypatch.setattr(Path,'read_bytes',no_history_read)
    monkeypatch.setattr(sqlite3,'connect',no_legacy_connect)
    result=sender.notify(event(born=born))
    assert result.attempted_channels==() and calls==[] and read(path)==original
    # The legitimate outer notification claim is retained; only legacy family
    # import/creation is forbidden by this boundary.
    assert {p.name for p in path.parent.glob('*.sqlite3')}=={'recent-notification-claims.sqlite3'}
    assert not list(path.parent.glob('*.backup-*'))


def test_typed_observer_context_cannot_implicitly_create_either_legacy_adapter(tmp_path):
    from codex_watchdog.notification_receipts import NotificationReceiptError
    for cls in (DualDeliveryReceipts,FallbackDeliveryReceipts):
        with pytest.raises(NotificationReceiptError,match='explicit_preparation_required'):
            with cls(tmp_path,receipt_context=observer(),clock=lambda:NOW+2):
                pytest.fail('unprepared typed observer became legacy')
    assert not (tmp_path/'notifications').exists()


@pytest.mark.parametrize('provider',['smtp','windows_msg'])
def test_missing_observer_context_does_not_create_fallback_schema(tmp_path,monkeypatch,provider):
    config=(NotificationConfig(smtp_host='mail.fixture.invalid',smtp_sender='from@fixture.invalid',
        smtp_recipients=('to@fixture.invalid',)) if provider=='smtp' else
        NotificationConfig(windows_message_enabled=True,windows_message_target='fixture'))
    sender=EnvironmentNotifier(tmp_path,config,clock=lambda:NOW+2)
    method='_send_smtp' if provider=='smtp' else '_send_windows_message'
    monkeypatch.setattr(sender,method,lambda value:pytest.fail('unprepared fallback attempted provider'))
    result=sender.notify(event(born=None))
    assert result.attempted_channels==()
    assert not (tmp_path/'notifications/fallback-deliveries.sqlite3').exists()


def test_unprepared_dual_refusal_preserves_independent_prepared_fallback(tmp_path,monkeypatch):
    sender,calls,_=notifier(tmp_path)
    sender.config=replace(sender.config,smtp_host='mail.fixture.invalid',
        smtp_sender='from@fixture.invalid',smtp_recipients=('to@fixture.invalid',))
    path=tmp_path/'notifications/dual-deliveries.json'
    original=json.dumps(dict(schema_version=1,events={})).encode();path.write_bytes(original)
    fallback=[]
    monkeypatch.setattr(sender,'_send_smtp',lambda value:fallback.append(value.event_fingerprint()))
    result=sender.notify(event())
    assert result.status=='sent_fallback' and result.channel=='smtp'
    assert calls==[] and len(fallback)==1 and path.read_bytes()==original


def test_observer_route_change_after_claim_is_uncertain_without_provider_attempt(tmp_path,monkeypatch):
    sender,calls,_=notifier(tmp_path)
    send=sender._send_slack
    def changed(value,*,expected_identity):
        sender.config=replace(sender.config,slack_channel_id='C87654321')
        return send(value,expected_identity=expected_identity)
    monkeypatch.setattr(sender,'_send_slack',changed)
    result=sender.notify(event())
    assert result.provider_outcomes=={'slack':'uncertain','lark':'sent'}
    assert [provider for provider,_ in calls]==['lark']


@pytest.mark.parametrize('provider',['smtp','windows_msg'])
def test_actual_fallback_notifier_uses_disjoint_claim_without_native_context(tmp_path,monkeypatch,provider):
    prepare(tmp_path)
    config=(NotificationConfig(smtp_host='mail.fixture.invalid',smtp_sender='watchdog@fixture.invalid',
                smtp_recipients=('owner@fixture.invalid',)) if provider=='smtp'
            else NotificationConfig(windows_message_enabled=True,windows_message_target='fixture'))
    sender=EnvironmentNotifier(tmp_path,config,clock=lambda:NOW+2)
    calls=[]
    method='_send_smtp' if provider=='smtp' else '_send_windows_message'
    monkeypatch.setattr(sender,method,lambda value:calls.append(value.event_fingerprint()))
    result=sender.notify(event())
    assert result.status=='sent' and result.channel==provider and len(calls)==1
    assert sender.notify(event()).duplicate and len(calls)==1
    with sqlite3.connect(tmp_path/'notifications/fallback-deliveries.sqlite3') as db:
        assert db.execute('SELECT thread_id,born,protected FROM retention').fetchall()==[
            (observer_scope(WORKSPACE)[0],NOW+1,0)]


@pytest.mark.parametrize('history',[0,1000,10000,100000])
def test_observer_claim_work_is_bounded_under_same_scope_history_growth(tmp_path,history,capsys):
    prepare(tmp_path)
    subject,scope=observer_scope(WORKSPACE)
    database=tmp_path/'notifications/dual-deliveries.sqlite3'
    with sqlite3.connect(database) as db:
        db.executemany("INSERT INTO receipts VALUES(?,'uncertain',NULL)",
                       ((sha('old-'+str(n)),) for n in range(history)))
        db.executemany('INSERT INTO retention VALUES(?,?,?,?,?,?,0,?,?)',
            ((sha('old-'+str(n)),'slack',subject,scope,NOW-1,NOW-1+finite.TTL,sha('old-target'),sha('old-payload'))
             for n in range(history)))
    value=observer('fresh')
    with open_store(tmp_path,'slack',value) as store:
        operations=[0];statements=[]
        store.db.set_progress_handler(lambda:operations.__setitem__(0,operations[0]+1) or 0,1)
        store.db.set_trace_callback(statements.append)
        assert store.claim(value['fingerprint'],'slack')
        store.db.set_progress_handler(None,0);store.db.set_trace_callback(None)
        assert len(statements)<400 and operations[0]<25000
        with capsys.disabled():
            print(json.dumps(dict(observer_claim=dict(history=history,sql=len(statements),vm=operations[0]))))
