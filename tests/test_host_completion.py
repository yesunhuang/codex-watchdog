"""Actual host monitor -> fenced notification regressions, disposable state only."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import uuid

import pytest

from test_linux_auto import scenario, THREAD
from codex_watchdog.control_state import ControlError, control_atomic_json
from codex_watchdog.host_completion import CURSOR, OUTPUT_MAX_BYTES
from codex_watchdog.models import sha256_text
from codex_watchdog.mvp_service import MvpWatchdogService
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
from codex_watchdog.stop_snapshots import StopSnapshots, STOP_MAX_BYTES


@pytest.fixture
def host(scenario, monkeypatch, request):
    if sys.platform == 'win32':
        pytest.skip('host helper requires an absolute POSIX repository path')
    store, local, writer, tick, clients, make_agent = scenario
    now = [datetime.now(timezone.utc)]
    monkeypatch.setattr('codex_watchdog.stop_snapshots.utc_now', lambda: now[0].isoformat())
    sent, rollouts, services = [], [None], []
    agent = make_agent()
    runtime = Path(store.read()['runtime_path'])
    def factory(runtime, **kwargs):
        ns = kwargs['remote_ssh_adapter'].namespace
        ns['ControlStore'] = lambda *args: store
        ns['ControlError'] = ControlError
        if not getattr(request, 'param', False):
            ns['control_exact_thread'] = lambda repo, thread: repo == store.repo_path and thread == THREAD
        ns['git_observation'] = lambda repo: dict(status='observed',topology='equal',blockers=[],head_oid='a'*40,upstream_oid='a'*40)
        # None is exactly what the actual tail selector returns once later work starts.
        if not getattr(request, 'param', False):
            ns['rollout_completion'] = lambda _: rollouts[0]
        def post(url,payload,timeout):
            text=json.loads(payload)['text']
            if 'Outcome:' in text:sent.append(text)
            return 200
        notifier = EnvironmentNotifier(runtime, config=NotificationConfig(slack_webhook_url='https://hooks.slack.invalid/fixture'),
            http_post=post,clock=lambda:now[0].timestamp())
        service = MvpWatchdogService(runtime, notifier=notifier, clock=lambda: now[0], **kwargs)
        services.append(service)
        return service
    agent.service_factory = factory
    def advance(seconds=60):now[0] += timedelta(seconds=seconds)
    def publish(name='fresh', output='exact result', age=0):
        turn=str(uuid.uuid5(uuid.NAMESPACE_URL,'host-turn:'+name));inv=str(uuid.uuid5(uuid.NAMESPACE_URL,'host-invocation:'+name))
        completed=now[0]-timedelta(seconds=age)
        event=dict(schema_version=1,event_type='Stop',outcome='grace_expired_parked',audit_id='audit-'+name,
            invocation_id=inv,session_id=THREAD,turn_id=turn,workspace=store.repo_path,
            hook_started_at=(completed-timedelta(seconds=30)).isoformat(),hook_completed_at=completed.isoformat(),
            recorded_at=completed.isoformat(),last_assistant_message_available=bool(output),last_output_chars=len(output),
            last_output_sha256=sha256_text(output) if output else None)
        StopSnapshots(runtime).publish(event)
        spool=runtime/'transient/stop-output'/(inv+'.json')
        control_atomic_json(spool,dict(schema_version=1,invocation_id=inv,session_id=THREAD,turn_id=turn,
            workspace=store.repo_path,last_assistant_message=output,last_output_chars=len(output),last_output_sha256=event['last_output_sha256']))
        return event,spool
    def restart():
        item=agent.controllers[THREAD];old=item['service']
        item['service']=factory(runtime,codex_home=store.codex_home,registry=old.registry,
            remote_ssh_adapter=old.remote_ssh_adapter,auto_discovery=False)
    h=SimpleNamespace(store=store,writer=writer,clients=clients,agent=agent,runtime=runtime,now=now,
        sent=sent,rollouts=rollouts,services=services,advance=advance,publish=publish,restart=restart)
    yield h
    assert writer[0]==777 and clients==[], 'native writer must never be resumed or replaced'


def test_compact_completion_while_later_turn_active_uses_actual_fenced_monitor_once(host):
    h=host
    assert h.agent.step()[0]['state']=='observing'
    assert h.sent==[]
    before=h.store.read();h.advance();event,spool=h.publish()
    result=h.agent.step()[0]
    assert result['state']=='observing' and len(h.sent)==1, (result,h.store.read()['remote_state'],
        StopSnapshots(h.runtime).read(THREAD,Path(h.store.repo_path)))
    assert h.sent[0].rstrip().endswith('exact result') and 'Outcome: grace_expired_parked' in h.sent[0]
    state=h.store.read()['remote_state']
    assert state['last_completion_turn']==event['turn_id'] and state[CURSOR]['schema_version']==1
    assert h.store.read()['epoch']==before['epoch']
    h.rollouts[0]=dict(turn_id=event['turn_id'],completed_at=event['hook_completed_at'],final_output='exact result',
        final_output_sha256=event['last_output_sha256'],final_output_chars=12)
    h.advance();h.agent.step();h.restart();h.agent.step()
    assert len(h.sent)==1
    assert h.store.read()['remote_state']['last_completion_turn']==event['turn_id']


def test_upgrade_baselines_existing_compact_and_tail_without_backfill_then_next_once(host):
    h=host;old,_=h.publish('old',age=120)
    h.rollouts[0]=dict(turn_id=old['turn_id'],completed_at=old['hook_completed_at'],final_output='exact result',
        final_output_sha256=old['last_output_sha256'],final_output_chars=12)
    protected=h.runtime/'saved-settings.json';protected.parent.mkdir(parents=True,exist_ok=True)
    protected.write_text('{"unknown-preserved":true}')
    h.agent.step();h.restart();h.advance();h.agent.step()
    assert h.sent==[] and protected.read_text()=='{"unknown-preserved":true}'
    h.rollouts[0]=None;h.advance();fresh,_=h.publish('next');h.agent.step();h.agent.step()
    assert len(h.sent)==1 and h.store.read()['remote_state']['last_completion_turn']==fresh['turn_id']


@pytest.mark.parametrize('fault',['hash','count','session','turn','invocation','repo','oversize','symlink','surrogate'])
def test_bad_output_is_consumed_without_attempt_or_retry_and_next_valid_turn_progresses(host,fault):
    h=host;h.agent.step();h.advance();event,path=h.publish('bad')
    value=json.loads(path.read_text())
    if fault=='oversize':path.write_text('x'*(OUTPUT_MAX_BYTES+1))
    elif fault=='symlink':
        other=path.with_suffix('.retained');path.rename(other)
        try:path.symlink_to(other)
        except OSError:pytest.skip('native symlink unavailable on this platform')
    elif fault=='surrogate':
        value['last_assistant_message']='\ud800'*12
        path.write_text(json.dumps(value))
    else:
        key={'hash':'last_output_sha256','count':'last_output_chars','session':'session_id','turn':'turn_id',
             'invocation':'invocation_id','repo':'workspace'}[fault]
        value[key]=999 if fault=='count' else 'wrong';control_atomic_json(path,value)
    h.agent.step();assert h.sent==[]
    if path.is_symlink():path.unlink()
    control_atomic_json(path,dict(schema_version=1,invocation_id=event['invocation_id'],session_id=THREAD,
        turn_id=event['turn_id'],workspace=h.store.repo_path,last_assistant_message='exact result',
        last_output_sha256=event['last_output_sha256'],last_output_chars=12))
    h.rollouts[0]=dict(turn_id=event['turn_id'],completed_at=event['hook_completed_at'],final_output='exact result',
        final_output_sha256=event['last_output_sha256'],final_output_chars=12)
    h.advance();h.agent.step();assert h.sent==[]
    assert h.store.read()['remote_state']['last_seen_at']
    h.rollouts[0]=None;h.advance();h.publish('good');h.agent.step()
    assert len(h.sent)==1


@pytest.mark.parametrize('fault',['session','repo','turn','invocation','future','expired','oversize','malformed','symlink','symlink-loop'])
def test_invalid_current_metadata_does_not_block_git_or_later_current_completion(host,fault):
    h=host;h.agent.step();h.advance();event,_=h.publish('bad-meta')
    snapshots=StopSnapshots(h.runtime);p=snapshots.path(THREAD,Path(h.store.repo_path));raw=json.loads(p.read_text())
    if fault=='oversize':p.write_text('x'*(STOP_MAX_BYTES+1))
    elif fault=='malformed':p.write_text('{')
    elif fault in ('symlink','symlink-loop'):
        other=p.with_suffix('.retained');p.rename(other)
        try:p.symlink_to(p if fault=='symlink-loop' else other)
        except OSError:pytest.skip('native symlink unavailable on this platform')
    else:
        if fault in ('future','expired'):
            bad=h.now[0]+timedelta(hours=1) if fault=='future' else h.now[0]-timedelta(hours=73)
            for k in ('hook_started_at','hook_completed_at','recorded_at'):raw['event'][k]=bad.isoformat()
        elif fault in ('turn','invocation'):raw['event'][fault+'_id']='not-a-uuid'
        elif fault=='session':raw['event']['session_id']=str(uuid.uuid4())
        else:raw['event']['workspace']='/wrong-repo'
        control_atomic_json(p,raw)
    h.agent.step();assert h.sent==[] and h.store.read()['remote_state']['connection_status']=='connected'
    if p.is_symlink():p.unlink()
    h.advance();h.publish('good-meta');h.agent.step()
    assert len(h.sent)==1


def test_malformed_cursor_is_retained_and_process_baseline_allows_later_valid_turn(host):
    h=host;h.agent.step();value=h.store.read();bad=dict(schema_version=99,unknown='retain me')
    value['remote_state'][CURSOR]=deepcopy(bad);control_atomic_json(h.store.path,value)
    h.advance();h.publish('baseline');h.agent.step();assert h.sent==[]
    h.advance();h.publish('after-bad-cursor');h.agent.step()
    assert len(h.sent)==1 and h.store.read()['remote_state'][CURSOR]==bad


def test_external_attempt_survives_crash_before_cursor_publication(host,monkeypatch):
    h=host;h.agent.step();service=h.services[-1];h.advance();h.publish('crash')
    original=service.remote_control.save
    def crash(*args,**kwargs):raise ControlError('synthetic_crash_after_external_attempt')
    monkeypatch.setattr(service.remote_control,'save',crash)
    h.agent.step();assert len(h.sent)==1
    monkeypatch.setattr(service.remote_control,'save',original);h.restart();h.agent.step()
    assert len(h.sent)==1


def test_future_snapshot_and_fallback_never_become_fresh_when_clock_catches_up(host):
    h=host;h.agent.step();baseline=h.now[0]
    h.advance(3600);event,_=h.publish('future-valid')
    h.now[0]=baseline+timedelta(seconds=60);h.agent.step();assert h.sent==[]
    h.advance(3700)
    h.rollouts[0]=dict(turn_id=event['turn_id'],completed_at=event['hook_completed_at'],final_output='exact result',
        final_output_sha256=event['last_output_sha256'],final_output_chars=12)
    h.agent.step();assert h.sent==[]
    h.rollouts[0]=None;h.advance();h.publish('fresh-after-future');h.agent.step();assert len(h.sent)==1


def test_empty_current_output_is_a_real_stop_without_fabricated_message(host):
    h=host;h.agent.step();h.advance();event,_=h.publish('empty',output='');h.agent.step()
    assert len(h.sent)==1 and h.store.read()['remote_state']['last_completion_turn']==event['turn_id']
    assert h.sent[0].rstrip().endswith('Outcome: grace_expired_parked')


@pytest.mark.parametrize('history',[0,1000])
def test_actual_host_monitor_reads_four_current_points_without_history_work(host,monkeypatch,history):
    import codex_watchdog.host_completion as module
    h=host;h.agent.step();audit=h.runtime/'audit';audit.mkdir(exist_ok=True)
    for n in range(history):(audit/(str(n)+'.json')).write_text('{"historical":true}')
    h.advance();h.publish('bounded');reads=[]
    original=module._read;glob=Path.glob;scandir=os.scandir
    def read(path,limit=STOP_MAX_BYTES):
        reads.append((path,limit));return original(path,limit)
    def refuse_glob(path,pattern):
        assert path not in (audit,h.store.codex_home/'sessions');return glob(path,pattern)
    def refuse_scan(path):
        assert Path(path) not in (audit,h.store.codex_home/'sessions');return scandir(path)
    monkeypatch.setattr(module,'_read',read);monkeypatch.setattr(Path,'glob',refuse_glob);monkeypatch.setattr(os,'scandir',refuse_scan)
    h.agent.step();assert len(h.sent)==1 and len(reads)==4
    assert sorted(limit for _,limit in reads)==[STOP_MAX_BYTES]*3+[OUTPUT_MAX_BYTES]


def test_growing_file_refused_by_bounded_reader(tmp_path,monkeypatch):
    import codex_watchdog.host_completion as module
    path=tmp_path/'current.json';path.write_text('{"schema_version":1}')
    original=os.fstat;calls=[]
    def changing(fd):
        s=original(fd);calls.append(True)
        return SimpleNamespace(st_dev=s.st_dev,st_ino=s.st_ino,st_size=s.st_size+(len(calls)>1),
            st_mode=s.st_mode,st_uid=s.st_uid,st_mtime_ns=s.st_mtime_ns,st_ctime_ns=s.st_ctime_ns)
    monkeypatch.setattr(os,'fstat',changing)
    assert module._read(path) is None and len(calls)==2


def test_native_fifo_grants_closed_ticket_and_prior_uncertain_claim_are_preserved(host):
    from test_linux_auto import queue_native_reply
    h=host;queue_native_reply(h.store,'99999999-aaaa-4bbb-8ccc-dddddddddddd')
    protected=[h.runtime/'slack/retained-grant.json',h.runtime/'slack/retained-closed-ticket.json',h.runtime/'wake/retained-uncertain.json']
    for path in protected:path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{"unknown":"retained"}')
    def queue():
        with sqlite3.connect(h.store.codex_home/'queue_1.sqlite') as db:return db.execute('SELECT * FROM queued_items').fetchall()
    before=queue();h.agent.step();h.advance();h.publish('preservation');h.agent.step()
    assert len(h.sent)==1 and queue()==before and all(p.read_text()=='{"unknown":"retained"}' for p in protected)


def test_explicit_host_runtime_mismatch_never_falls_through_to_old_rollout(host):
    h=host;h.agent.step();service=h.services[-1];adapter=service.remote_ssh_adapter
    adapter.current_runtime=h.runtime/'unrelated';h.advance();h.publish('mismatch')
    h.agent.step();assert h.sent==[]
    adapter.current_runtime=h.runtime;h.advance();h.publish('restored');h.agent.step();assert len(h.sent)==1


def test_lost_provider_response_remains_terminal_across_cursor_publication_failure(host,monkeypatch):
    h=host;h.agent.step();service=h.services[-1];attempts=[]
    original_post=service.notifier.http_post
    def lose(url,payload,timeout):
        if 'Outcome:' in json.loads(payload)['text']:
            attempts.append(True)
            raise RuntimeError('synthetic response lost after external attempt')
        return original_post(url,payload,timeout)
    monkeypatch.setattr(service.notifier,'http_post',lose)
    original_save=service.remote_control.save
    def crash(*args,**kwargs):raise ControlError('synthetic_crash_before_cursor_publication')
    monkeypatch.setattr(service.remote_control,'save',crash)
    h.advance();h.publish('uncertain-crash');h.agent.step()
    assert attempts==[True] and h.sent==[]
    monkeypatch.setattr(service.remote_control,'save',original_save)
    h.restart();h.agent.step();h.agent.step()
    assert attempts==[True] and h.sent==[]


def test_future_current_present_at_initial_upgrade_never_requalifies_via_fallback(host):
    h=host;initial=h.now[0]
    h.advance(3600);event,_=h.publish('preexisting-future')
    h.now[0]=initial;h.agent.step();assert h.sent==[]
    h.advance(3700)
    h.rollouts[0]=dict(turn_id=event['turn_id'],completed_at=event['hook_completed_at'],final_output='exact result',
        final_output_sha256=event['last_output_sha256'],final_output_chars=12)
    h.agent.step();assert h.sent==[]
    h.rollouts[0]=None;h.advance();h.publish('fresh-after-upgrade-future');h.agent.step();assert len(h.sent)==1


@pytest.mark.parametrize('first',['compact','rollout'])
def test_same_turn_cross_source_duplicate_after_crash_has_one_external_attempt(host,monkeypatch,first):
    h=host;h.agent.step();service=h.services[-1];h.advance(120)
    turn=str(uuid.uuid5(uuid.NAMESPACE_URL,'host-turn:cross-source'))
    fallback=dict(turn_id=turn,completed_at=(h.now[0]-timedelta(seconds=60)).isoformat(),
        final_output='exact result',final_output_sha256=sha256_text('exact result'),final_output_chars=12)
    if first=='compact':h.publish('cross-source',age=61)
    else:h.rollouts[0]=fallback
    original=service.remote_control.save
    def crash(*args,**kwargs):raise ControlError('synthetic_crash_after_one_source_attempt')
    monkeypatch.setattr(service.remote_control,'save',crash)
    h.agent.step();assert len(h.sent)==1
    monkeypatch.setattr(service.remote_control,'save',original)
    if first=='compact':
        snapshots=StopSnapshots(h.runtime);path=snapshots.path(THREAD,Path(h.store.repo_path))
        path.unlink();path.with_name(path.stem+'.floor.json').unlink()
        h.rollouts[0]=fallback
    else:
        h.publish('cross-source',age=61);h.rollouts[0]=None
    h.restart();h.agent.step();h.agent.step()
    assert len(h.sent)==1 and h.store.read()['remote_state']['connection_status']=='connected'


def test_malformed_fallback_unicode_does_not_block_git_or_later_completion(host):
    h=host;h.agent.step();h.advance(120)
    h.rollouts[0]=dict(turn_id=str(uuid.uuid4()),completed_at=(h.now[0]-timedelta(seconds=60)).isoformat(),
        final_output='\ud800',final_output_sha256='0'*64,final_output_chars=1)
    h.agent.step();assert h.sent==[] and h.store.read()['remote_state']['connection_status']=='connected'
    h.rollouts[0]=None;h.advance();h.publish('after-malformed-fallback');h.agent.step()
    assert len(h.sent)==1


@pytest.mark.skipif(sys.platform=='win32',reason='native POSIX directory symlink race')
def test_bounded_reader_refuses_parent_becoming_symlink_during_read(tmp_path,monkeypatch):
    import codex_watchdog.host_completion as module
    parent=tmp_path/'original';parent.mkdir();path=parent/'current.json';path.write_text('{"schema_version":1}')
    original=os.fstat;calls=[]
    def replace_parent(fd):
        s=original(fd);calls.append(True)
        if len(calls)==1:
            retained=parent.with_name('retained');parent.rename(retained);parent.symlink_to(retained,target_is_directory=True)
        return s
    monkeypatch.setattr(os,'fstat',replace_parent)
    assert module._read(path) is None
