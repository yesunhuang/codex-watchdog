"""Real opted-in host helper lookup/tail, using only disposable native state."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

import pytest

from test_host_completion import host
from test_linux_auto import scenario, THREAD, ROLLOUT
from codex_watchdog.control_state import ControlError


def terminal(h, name, *, age=60, output='real fallback'):
    from datetime import timedelta
    value=dict(type='event_msg',timestamp=(h.now[0]-timedelta(seconds=age)).isoformat(),
        payload=dict(type='task_complete',thread_id=THREAD,
                     turn_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'host-turn:'+name)),last_agent_message=output))
    path=h.store.codex_home/'sessions'/ROLLOUT
    path.write_text(json.dumps(value)+'\n')
    return value,path


@contextmanager
def measured_cycle(h, monkeypatch):
    """Measure the complete call, including pathlib's cached Python 3.9 accessor."""
    home=h.store.codex_home;sessions=home/'sessions'
    stats=dict(directory_scans=0,directory_entries=0,native_sql=0,native_vm_steps=0,tail_reads=[],index_plans=[])
    connect=sqlite3.connect;scandir=os.scandir;path_open=Path.open
    class Cursor(sqlite3.Cursor):
        def execute(self,sql,*args,**kwargs):
            self.plan=sql.startswith('EXPLAIN QUERY PLAN')
            return super().execute(sql,*args,**kwargs)
        def fetchmany(self,*args):
            rows=super().fetchmany(*args)
            if self.plan:stats['index_plans'].append([row[3] for row in rows])
            return rows
    class Measured(sqlite3.Connection):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs)
            self.native='state_5.sqlite' in str(args[0])
            if self.native:super().set_progress_handler(self.count,1)
        def count(self):
            stats['native_vm_steps']+=1
            return 0
        def set_progress_handler(self,callback,interval):
            ticks=[0]
            def step():
                self.count();ticks[0]+=1
                return callback() if ticks[0]%interval==0 else 0
            super().set_progress_handler(step,1)
        def execute(self,sql,*args,**kwargs):
            if self.native:stats['native_sql']+=1
            return self.cursor(factory=Cursor).execute(sql,*args,**kwargs)
    class Scan:
        def __init__(self,path):
            self.inner=scandir(path)
            self.native=Path(path)==sessions or sessions in Path(path).parents
            if self.native:stats['directory_scans']+=1
        def __enter__(self):return self
        def __exit__(self,*args):self.close()
        def __iter__(self):return self
        def __next__(self):
            item=next(self.inner)
            if self.native:stats['directory_entries']+=1
            return item
        def close(self):self.inner.close()
    class Reader:
        def __init__(self,handle):self.handle=handle
        def __enter__(self):self.handle.__enter__();return self
        def __exit__(self,*args):return self.handle.__exit__(*args)
        def seek(self,*args):return self.handle.seek(*args)
        def read(self,size=-1):
            raw=self.handle.read(size)
            stats['tail_reads'].append(dict(requested=size,returned=len(raw)))
            return raw
        def __getattr__(self,name):return getattr(self.handle,name)
    def opened(path,*args,**kwargs):
        handle=path_open(path,*args,**kwargs)
        return Reader(handle) if path==sessions/ROLLOUT else handle
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3,'connect',lambda *a,**k:connect(*a,factory=Measured,**k))
        patch.setattr(os,'scandir',Scan)
        accessor=getattr(sessions,'_accessor',None)
        if accessor is not None:patch.setattr(accessor,'scandir',Scan)
        patch.setattr(Path,'open',opened)
        yield stats


@pytest.mark.parametrize('host',[True],indirect=True)
@pytest.mark.parametrize('history',[0,1000])
def test_full_host_cycle_uses_index_without_visiting_unrelated_history(host,monkeypatch,history):
    h=host;h.agent.step()
    old=h.store.codex_home/'sessions'/'old';old.mkdir()
    with sqlite3.connect(h.store.codex_home/'state_5.sqlite') as db:
        db.executemany("INSERT INTO threads VALUES (?,?,'cli','user',1,?)",
            [('old-'+str(i),h.store.repo_path,str(old/(str(i)+'.jsonl'))) for i in range(history)])
    for i in range(history):(old/(str(i)+'.jsonl')).write_text('{}\n')
    h.advance();h.publish('real-path-current')
    (h.store.codex_home/'sessions'/ROLLOUT).write_text(json.dumps(dict(type='event_msg',
        payload=dict(type='task_started',thread_id=THREAD,turn_id=str(uuid.uuid4()))))+'\n')
    with measured_cycle(h,monkeypatch) as stats:
        result=h.agent.step()[0]
    print('HOST_FULL_CYCLE_WORK '+json.dumps(dict(history=history,**stats),sort_keys=True))
    assert result['state']=='observing' and len(h.sent)==1,(result,stats)
    assert stats['directory_entries']==stats['directory_scans']==0,stats
    assert 0<stats['native_sql']<=40 and stats['native_vm_steps']<2000,stats
    assert stats['index_plans'] and all(len(plan)==1 and 'SEARCH' in plan[0] and '(id=?)' in plan[0]
        for plan in stats['index_plans']),stats
    assert stats['tail_reads'] and all(0<=r['requested']<=1048576 and r['returned']<=1048576 for r in stats['tail_reads']),stats


@pytest.mark.parametrize('host',[True],indirect=True)
@pytest.mark.parametrize('fault',['missing','relative','wrong_name','symlink','oversized'])
def test_optional_rollout_hint_failure_preserves_actual_probe_compact_stop_and_git(host,fault,monkeypatch):
    h=host;h.agent.step();h.advance()
    original=h.store.codex_home/'sessions'/ROLLOUT
    if fault=='missing':hint=None
    elif fault=='relative':hint=ROLLOUT
    elif fault=='wrong_name':hint=str(original.with_name('wrong.jsonl'))
    elif fault=='oversized':hint='x'*4097
    else:
        alias=original.with_name('rollout-alias-'+THREAD+'.jsonl')
        try:alias.symlink_to(original)
        except OSError:pytest.skip('native symlink unavailable on this platform')
        hint=str(alias)
    with sqlite3.connect(h.store.codex_home/'state_5.sqlite') as db:
        db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(hint,THREAD))
    h.publish('current-with-bad-hint')
    # The observer cycle receives its existing native writer/owner capability.
    # Do not relax the separate native owner's required exact-thread checks.
    with measured_cycle(h,monkeypatch) as stats:
        result=h.agent._cycle(h.agent.controllers[THREAD])
    assert result.status=='completed' and len(h.sent)==1
    assert h.store.read()['remote_state']['connection_status']=='connected'
    assert stats['directory_entries']==0 and stats['tail_reads']==[],stats
    with sqlite3.connect(h.store.codex_home/'state_5.sqlite') as db:
        db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(original),THREAD))
    h.advance(120);terminal(h,'fresh-after-bad-hint');h.agent.step()
    assert len(h.sent)==2


@pytest.mark.parametrize('host',[True],indirect=True)
def test_real_indexed_fallback_baseline_restart_crash_and_same_turn_compact_are_once(host,monkeypatch):
    h=host;terminal(h,'old-real',age=120);h.agent.step();h.restart();h.agent.step();assert h.sent==[]
    h.advance(120);terminal(h,'fresh-real');service=h.services[-1]
    save=service.remote_control.save
    def crash(*args,**kwargs):raise ControlError('synthetic_crash_before_cursor_save')
    monkeypatch.setattr(service.remote_control,'save',crash)
    h.agent.step();assert len(h.sent)==1 and h.sent[0].rstrip().endswith('real fallback')
    monkeypatch.setattr(service.remote_control,'save',save);h.restart();h.agent.step();assert len(h.sent)==1
    h.publish('fresh-real',age=61,output='real fallback');h.agent.step();assert len(h.sent)==1


@pytest.mark.parametrize('host',[True],indirect=True)
def test_real_fallback_read_remains_capped_when_file_grows_after_stat(host,monkeypatch):
    h=host;h.agent.step();h.advance(120);_,path=terminal(h,'growing-real')
    original=Path.open;reads=[];grown=[];fallback_reads=[]
    class Reader:
        def __init__(self,handle,fallback=False):self.handle=handle;self.fallback=fallback
        def __enter__(self):self.handle.__enter__();return self
        def __exit__(self,*args):return self.handle.__exit__(*args)
        def seek(self,*args):return self.handle.seek(*args)
        def read(self,size=-1):
            raw=self.handle.read(size);reads.append((size,len(raw)))
            if self.fallback:fallback_reads.append((size,len(raw)))
            return raw
        def __getattr__(self,name):return getattr(self.handle,name)
    def opened(p,*args,**kwargs):
        if p==path:
            fallback=sys._getframe(1).f_code.co_name=='rollout_completion'
            if fallback and not grown:
                with original(path,'ab') as handle:handle.write(b' '*(2*1048576))
                grown.append(True)
            return Reader(original(p,*args,**kwargs),fallback)
        return original(p,*args,**kwargs)
    monkeypatch.setattr(Path,'open',opened)
    result=h.agent.step()[0]
    assert result['state']=='observing' and h.store.read()['remote_state']['connection_status']=='connected'
    assert grown and len(h.sent)==1 and fallback_reads,fallback_reads
    assert reads and all(0<=size<=1048576 and count<=1048576 for size,count in reads),reads
