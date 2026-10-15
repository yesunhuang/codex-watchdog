from types import SimpleNamespace
import pytest
from codex_watchdog.activation_request import request_activation,requested_activity
from codex_watchdog.session_activity import SessionActivity
from codex_watchdog.session_lifecycle import SessionLifecycle

THREAD='11111111-1111-4111-8111-111111111111'


def test_explicit_same_session_intent_is_immutable_and_expires(tmp_path):
    request_activation(tmp_path,THREAD,requested_at=100)
    assert requested_activity(tmp_path,THREAD,now=101)==100
    assert requested_activity(tmp_path,THREAD,now=100+72*3600) is None
    assert requested_activity(tmp_path,'22222222-2222-4222-8222-222222222222',now=101) is None


def test_only_verified_owner_applies_intent_and_restart_does_not_repeat(tmp_path):
    now=[1000000.0]
    store=SessionActivity(tmp_path,clock=lambda:now[0])
    store.observe(THREAD,genuine_at=now[0]-72*3600,turn_active=False,queue_active=False)
    request_activation(tmp_path,THREAD,requested_at=now[0])
    notifier=SimpleNamespace(config=SimpleNamespace(interactive_providers=()))
    bridge=SessionLifecycle(tmp_path,notifier,clock=lambda:now[0])
    workspace=SimpleNamespace(session_id=THREAD)
    signals=dict(exact=True,genuine_at=None,turn_active=None,queue_active=None)
    assert bridge.observe(workspace,signals)['status']=='UNKNOWN'
    assert not store.is_active(THREAD)
    first=bridge.observe(workspace,signals,verified_owner=True)
    assert first['activated'] and first['generation']==1
    now[0]+=1
    second=SessionLifecycle(tmp_path,notifier,clock=lambda:now[0]).observe(workspace,signals,verified_owner=True)
    assert not second['activated'] and second['generation']==1 and second['last_activity_at']==1000000


@pytest.mark.parametrize('stamp',[-1,True,float('nan')])
def test_invalid_request_never_publishes(tmp_path,stamp):
    with pytest.raises(ValueError):request_activation(tmp_path,THREAD,requested_at=stamp)
    assert not (tmp_path/'activation-requests').exists()
