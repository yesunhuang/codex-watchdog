"""Actual notifier/relay claim-send-finish paths share real kernel exclusion."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest

from codex_watchdog.notification_senders import sender_fence
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from codex_watchdog.slack_bot_receipts import BotCommandReceipts
from codex_watchdog.storage import FileLock, StoreBusyError
from test_slack_bot_acknowledgements import (ReceiptRig, instruction_event,
                                            external_effects_are_forbidden)
from test_destination_binding import setup, begin, finish


def busy(runtime):
    with pytest.raises(StoreBusyError):
        with FileLock(runtime/'locks/notification-senders.lock'):
            pytest.fail('physical sender exclusion absent')


@contextmanager
def held_elsewhere(runtime):
    entered, release = Event(), Event()
    def worker():
        with sender_fence(runtime):
            entered.set()
            assert release.wait(15), 'fixture release timed out'
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(worker)
        try:
            assert entered.wait(5)
            yield
        finally:
            release.set()
            future.result(timeout=5)


def test_actual_notifier_claim_and_provider_result_stay_inside_common_fence(tmp_path,monkeypatch):
    from codex_watchdog.notification_state import CurrentNotificationState
    calls=[]
    config=NotificationConfig(slack_bot_token='xoxb-fixture',slack_channel_id='C12345678')
    def post(token,method,payload,timeout):
        busy(tmp_path);calls.append('send')
        return dict(channel=payload['channel'],ts='1792008000.000001')
    notifier=EnvironmentNotifier(tmp_path,config,slack_api_post=post)
    claim=CurrentNotificationState.claim
    def checked(self,*args,**kwargs):
        busy(tmp_path);calls.append('claim')
        return claim(self,*args,**kwargs)
    monkeypatch.setattr(CurrentNotificationState,'claim',checked)
    event=NotificationEvent('fixture','git_attention','current-source','Subject','Output')
    with held_elsewhere(tmp_path):
        with pytest.raises(StoreBusyError):notifier.notify(event)
    assert calls==[]
    assert notifier.notify(event).status=='sent'
    assert calls==['claim','send']
    with sender_fence(tmp_path):pass


@pytest.mark.parametrize('mode',['socket','poll'])
def test_actual_bot_ack_claim_send_finish_is_fenced(tmp_path,monkeypatch,mode):
    rig=ReceiptRig(tmp_path,mode);phases=[]
    claim,finish_receipt=BotCommandReceipts.claim,BotCommandReceipts.finish
    def checked_claim(self,*args,**kwargs):
        busy(tmp_path);phases.append('claim');return claim(self,*args,**kwargs)
    def checked_finish(self,*args,**kwargs):
        busy(tmp_path);phases.append('finish');return finish_receipt(self,*args,**kwargs)
    monkeypatch.setattr(BotCommandReceipts,'claim',checked_claim)
    monkeypatch.setattr(BotCommandReceipts,'finish',checked_finish)
    post=rig.post
    def checked_post(**params):
        busy(tmp_path);phases.append('send');return post(**params)
    rig.post=checked_post
    result=rig.deliver(instruction_event(rig,rig.grant()))
    assert result[0]['status']=='bot_queued' and len(rig.calls)==1
    assert phases==['claim','send','finish'] and len(rig.acks)==1


@pytest.mark.parametrize('mode',['socket','poll'])
def test_busy_ack_does_not_undo_or_repeat_native_admission(tmp_path,mode):
    rig=ReceiptRig(tmp_path,mode);event=instruction_event(rig,rig.grant())
    with held_elsewhere(tmp_path):result=rig.deliver(event)
    assert result[0]['status']=='bot_queued' and len(rig.calls)==1 and rig.acks==[]
    assert rig.state(event['thread_ts'])['active'] is False


@pytest.mark.parametrize('kind',['lark','onebot'])
def test_binding_control_and_hello_are_fenced_from_original_claim(tmp_path,monkeypatch,kind):
    env=setup(tmp_path,kind);phases=[]
    original=env.api.send
    def send(*args,**kwargs):
        busy(tmp_path);phases.append('send');return original(*args,**kwargs)
    monkeypatch.setattr(env.api,'send',send)
    challenge=begin(env)
    result=finish(env,challenge)
    assert result.status=='route_bound' and phases==['send','send'] and not env.queued


def test_authority_selection_precedes_common_root_selection(tmp_path):
    from contextlib import contextmanager
    local=tmp_path/'local';selected=tmp_path/'selected'
    class Authority:
        runtime=local
        @contextmanager
        def guard(self):
            self.runtime=selected
            yield
    notifier=EnvironmentNotifier(local,NotificationConfig())
    notifier.relay_authority=Authority()
    def body(event):busy(selected);return 'fixture-completed'
    notifier._guarded_notify=body
    assert notifier.notify(NotificationEvent('fixture','attention','source','Subject','Output'))=='fixture-completed'


def test_explicit_receipt_preparation_and_maintenance_share_sender_exclusion(tmp_path):
    from contextlib import nullcontext
    from codex_watchdog.notification_receipt_retention import (
        prepare_notification_receipts,maintain_notification_receipts)
    from test_observer_notification_context import scopes,NOW
    with held_elsewhere(tmp_path):
        with pytest.raises(StoreBusyError):
            prepare_notification_receipts(tmp_path,scopes=scopes(),contexts=(),
                sender_fence=nullcontext,clock=lambda:NOW)
        with pytest.raises(StoreBusyError):
            maintain_notification_receipts(tmp_path,scopes=scopes(),complete=True,
                sender_fence=nullcontext,clock=lambda:NOW)
    assert not (tmp_path/'notifications').exists()


def test_slack_binding_hello_and_ack_keep_common_exclusion(tmp_path):
    from types import SimpleNamespace
    from codex_watchdog.relay import ReplyResult
    from codex_watchdog.session_routes import SessionRoutes
    from test_session_access_relays import slack_relay,CHANNEL
    from test_session_routes import record_ticket,thread_ts,USER,DEST,TS_100
    relay=slack_relay(tmp_path);record_ticket(relay.thread_store,1)
    routes=SessionRoutes(relay.thread_store,scope=CHANNEL)
    key='event:sender-binding'
    assert routes.apply(key,CHANNEL,thread_ts(1),USER,'bind '+DEST,DEST,
                        command_ts=TS_100)['status']=='bound'
    sends=[]
    def post(**params):
        busy(tmp_path);sends.append(params)
        return dict(ok=True,channel=params['channel'],ts='1792008000.%06d'%len(sends))
    relay.acknowledge(dict(channel=CHANNEL,thread_ts=thread_ts(1)),
        ReplyResult('route_bound','workspace-1',key),SimpleNamespace(chat_postMessage=post))
    assert [item['channel'] for item in sends]==[DEST,CHANNEL]


@pytest.mark.parametrize('provider',['slack','lark','onebot'])
def test_access_confirmation_is_fenced_for_each_provider(tmp_path,provider):
    import test_session_access_relays as f
    sends=[]
    if provider=='slack':
        def send(text,operation,destination):
            busy(tmp_path);sends.append(operation)
            return dict(chat_id=destination,message_id=f.NEW_CONF_TS)
        relay=f.slack_relay(tmp_path,access_api=f.slack_access_api(f.DELEGATE_USER),access_sender=send)
        f.record_slack_notification(relay.thread_store)
        result=relay.handle_message(f.slack_reply_event())
    else:
        api=f.LarkApi() if provider=='lark' else f.OneBotApi()
        original=api.send
        def send(*args,**kwargs):
            busy(tmp_path);sends.append(args[1]);return original(*args,**kwargs)
        api.send=send
        if provider=='lark':
            relay=f.lark_relay(tmp_path,api=api);f.record_lark_notification(relay.thread_store)
            result=relay.handle_event(f.lark_access_event())
        else:
            relay=f.onebot_relay(tmp_path,api=api);f.record_ob_notification(relay.thread_store)
            result=relay.handle_event(f.ob_access_event())
    assert result.status=='access_applied' and len(sends)==1


@pytest.mark.parametrize('mode',['socket','poll'])
def test_bot_grant_control_defers_before_any_claim_or_mutation(tmp_path,mode):
    from test_slack_bot_relay import Rig,ADMIN,BOT_USER,journal_rows
    rig=Rig(tmp_path,mode);parent=rig.record()
    command=rig.event(parent,text='bot add <@'+BOT_USER+'>',user=ADMIN,bot=False)
    before=journal_rows(rig)
    with held_elsewhere(tmp_path):result=rig.deliver(command)
    assert result[0]['status']=='deferred'
    assert rig.calls==[] and rig.remote_calls==[] and rig.sent==[]
    assert journal_rows(rig)==before and rig.state(parent)['active']


@pytest.mark.parametrize('mode',['socket','poll'])
def test_bot_grant_control_claim_send_finish_are_all_fenced(tmp_path,monkeypatch,mode):
    from test_slack_bot_relay import Rig,ADMIN,BOT_USER
    from codex_watchdog.slack_bot_acl import BotSessionAccess
    rig=Rig(tmp_path,mode);parent=rig.record();phases=[]
    for name in ('control','claim_confirmation','finish_confirmation'):
        original=getattr(BotSessionAccess,name)
        def checked(self,*args,_name=name,_original=original,**kwargs):
            busy(tmp_path);phases.append(_name);return _original(self,*args,**kwargs)
        monkeypatch.setattr(BotSessionAccess,name,checked)
    send=rig.relay._access_sender
    def checked_send(*args,**kwargs):
        busy(tmp_path);phases.append('send');return send(*args,**kwargs)
    rig.relay._access_sender=checked_send
    result=rig.deliver(rig.event(parent,text='bot add <@'+BOT_USER+'>',user=ADMIN,bot=False))
    assert result[0]['status']=='bot_access_applied'
    assert phases==['control','claim_confirmation','send','finish_confirmation']
    assert len(rig.sent)==1 and rig.calls==[] and rig.remote_calls==[]
