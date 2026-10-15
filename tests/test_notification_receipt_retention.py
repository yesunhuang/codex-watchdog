"""Finite claims keep one-attempt semantics without native/provider side effects."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from codex_watchdog.notification_receipts import DualDeliveryReceipts, FallbackDeliveryReceipts, NotificationReceiptError
from codex_watchdog import notification_receipt_retention as finite

SID='11111111-2222-4333-8444-555555555555'
OTHER='aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
NOW=1792008000.0
PROVIDERS=('slack','lark','smtp','windows_msg')
COUNTS={}

def sha(value):return hashlib.sha256(str(value).encode()).hexdigest()
def ctx(index=0,born=NOW+1,thread=SID):
    return dict(schema_version=1,fingerprint=sha(index),thread_id=thread,scope_sha256=sha(thread),born=born,
        providers={p:dict(target_sha256=sha(p+'target'),payload_sha256=sha('payload'+str(index))) for p in PROVIDERS})
def scopes(thread=SID):return [(thread,sha(thread),p) for p in PROVIDERS]
def prepare(path,contexts=(),pins=(),now=NOW):
    return finite.prepare_notification_receipts(path,scopes=scopes(),contexts=contexts,current_pins=pins,sender_fence=nullcontext,clock=lambda:now)
def maintenance(path,*,now=NOW,pins=(),complete=True):
    return finite.maintain_notification_receipts(path,scopes=scopes(),pin_changes=pins,complete=complete,sender_fence=nullcontext,clock=lambda:now)
@contextmanager
def opened(path,provider,context=None,now=NOW+1):
    cls=DualDeliveryReceipts if provider in ('slack','lark') else FallbackDeliveryReceipts
    with cls(path,receipt_context=context,clock=lambda:now) as store:yield store
def claim(store,provider,c):
    if provider in ('slack','lark'):return store.claim(c['fingerprint'],provider)
    return store.claim(c['fingerprint'],provider,**c['providers'][provider])
def metadata(path,provider):
    family='dual' if provider in ('slack','lark') else 'fallback'
    with sqlite3.connect(path/'notifications'/(finite.NAMES[family]+'.sqlite3')) as db:
        return db.execute('SELECT * FROM retention ORDER BY fingerprint,provider').fetchall()

def seed_legacy(path,contexts):
    with DualDeliveryReceipts(path) as db:
        for c in contexts:
            db.claim(c['fingerprint'],'slack')
    with FallbackDeliveryReceipts(path) as db:
        for c in contexts:
            db.claim(c['fingerprint'],'smtp',**c['providers']['smtp'])


@pytest.mark.parametrize('provider',PROVIDERS)
def test_committed_claim_restart_duplicate_and_immutable_identity(tmp_path,provider):
    prepare(tmp_path);c=ctx()
    with opened(tmp_path,provider,c) as store:
        assert claim(store,provider,c) in (True,None)
    original=metadata(tmp_path,provider)
    with opened(tmp_path,provider,c,now=NOW+2) as store:
        assert claim(store,provider,c) == (False if provider in ('slack','lark') else 'uncertain')
        store.confirm_sent(c['fingerprint'],provider)
    assert metadata(tmp_path,provider)==original
    bad=deepcopy(c);bad['providers'][provider]['payload_sha256']=sha('edited')
    with opened(tmp_path,provider,bad) as store:
        with pytest.raises(finite.RetentionError,match='collision'):claim(store,provider,bad)
    assert metadata(tmp_path,provider)==original


@pytest.mark.parametrize('provider',PROVIDERS)
def test_identityless_call_and_old_export_never_send(tmp_path,provider):
    prepare(tmp_path)
    with opened(tmp_path,provider) as store:
        with pytest.raises(finite.RetentionError,match='context_required'):claim(store,provider,ctx())
        if provider in ('slack','lark'):
            with pytest.raises(NotificationReceiptError,match='forward_recovery'):store.export()


@pytest.mark.parametrize('provider',PROVIDERS)
def test_expired_quiet_claim_floor_blocks_original_even_after_clock_rewind(tmp_path,provider):
    prepare(tmp_path);c=ctx()
    with opened(tmp_path,provider,c) as store:claim(store,provider,c)
    result=maintenance(tmp_path,now=NOW+finite.TTL+2)
    assert sum(v['removed'] for v in result['families'].values())==1
    assert metadata(tmp_path,provider)==[]
    with opened(tmp_path,provider,c,now=NOW+2) as store:
        with pytest.raises(finite.RetentionError,match='retired'):claim(store,provider,c)
    newer=ctx(2,born=NOW+finite.TTL+3)
    with opened(tmp_path,provider,newer,now=newer['born']) as store:
        assert claim(store,provider,newer) in (True,None)


def test_explicit_current_pin_survives_ttl_unknown_does_not_clear_then_complete_retires(tmp_path):
    prepare(tmp_path);c=ctx()
    with opened(tmp_path,'slack',c) as store:claim(store,'slack',c)
    maintenance(tmp_path,now=NOW+2,pins=[(c['fingerprint'],'slack',True)])
    before=metadata(tmp_path,'slack')
    maintenance(tmp_path,now=NOW+finite.TTL+3,complete=False,pins=[(c['fingerprint'],'slack',False)])
    assert metadata(tmp_path,'slack')==before
    maintenance(tmp_path,now=NOW+finite.TTL+3)
    assert metadata(tmp_path,'slack')==before
    maintenance(tmp_path,now=NOW+finite.TTL+3,pins=[(c['fingerprint'],'slack',False)])
    assert metadata(tmp_path,'slack')==[]


def test_cap_retains_256_and_old_callback_cannot_refresh_birth(tmp_path):
    prepare(tmp_path)
    for i in range(finite.LIMIT+5):
        c=ctx(i,born=NOW+i+1)
        with opened(tmp_path,'slack',c,now=c['born']) as store:assert claim(store,'slack',c)
    assert len(metadata(tmp_path,'slack'))==finite.LIMIT
    old=ctx(0)
    with opened(tmp_path,'slack',old,now=NOW+finite.LIMIT+10) as store:
        with pytest.raises(finite.RetentionError,match='retired'):claim(store,'slack',old)


def test_delayed_original_birth_keeps_exact_new_claim_through_confirmation(tmp_path):
    prepare(tmp_path)
    for i in range(finite.LIMIT):
        c=ctx(i,born=NOW+100+i)
        with opened(tmp_path,'slack',c,now=c['born']) as store:assert claim(store,'slack',c)
    delayed=ctx('delayed',born=NOW+1)
    with opened(tmp_path,'slack',delayed,now=NOW+1000) as store:
        assert claim(store,'slack',delayed)
        row=store.finite._row(delayed['fingerprint'],'slack')
        assert row[2]==delayed['born'] and row[4]==0
        assert row[5:]==tuple(delayed['providers']['slack'][k] for k in ('target_sha256','payload_sha256'))
        assert len(metadata(tmp_path,'slack'))==finite.LIMIT
        store.confirm_sent(delayed['fingerprint'],'slack')
        assert store.get(delayed['fingerprint'])=={'slack':'sent'}


def test_cold_legacy_projection_is_compact_preserves_current_and_fences_untimestamped_history(tmp_path):
    old=ctx(1,born=NOW-finite.TTL-1);recent=ctx(2,born=NOW-1)
    seed_legacy(tmp_path,[old,recent,ctx(3,born=NOW-5)])
    result=prepare(tmp_path,[old,recent],[(old['fingerprint'],'slack')])
    assert result['retained']=={'dual':2,'fallback':1}
    assert len(metadata(tmp_path,'slack'))==2
    assert len(metadata(tmp_path,'smtp'))==1
    assert list((tmp_path/'notifications').glob('*.backup-*'))==[]
    plan=json.loads((tmp_path/'notifications/finite-receipts-preparation.json').read_text())
    assert sum(map(len,plan['retained'].values()))==3
    assert prepare(tmp_path,[old,recent],[(old['fingerprint'],'slack')])==result
    with opened(tmp_path,'slack',old) as store:assert not claim(store,'slack',old)
    omitted=ctx(3,born=NOW-5)
    with opened(tmp_path,'slack',omitted) as store:
        with pytest.raises(finite.RetentionError,match='retired'):claim(store,'slack',omitted)


@pytest.mark.parametrize('stage',['plan','dual','fallback','marker'])
def test_preparation_crash_resumes_exact_compact_plan_without_old_sender_or_reclaim(tmp_path,monkeypatch,stage):
    c=ctx(born=NOW-1);seed_legacy(tmp_path,[c])
    atomic,publish=finite._atomic,finite._publish
    hit=[]
    def write(path,value):
        atomic(path,value)
        if not hit and ((stage=='plan' and path.name=='finite-receipts-preparation.json') or (stage=='marker' and path.name=='dual-deliveries.json')):
            hit.append(True);raise OSError('fixture crash')
    def install(directory,family,plan):
        publish(directory,family,plan)
        if not hit and stage==family:hit.append(True);raise OSError('fixture crash')
    monkeypatch.setattr(finite,'_atomic',write);monkeypatch.setattr(finite,'_publish',install)
    with pytest.raises(OSError):prepare(tmp_path,[c])
    assert hit
    monkeypatch.setattr(finite,'_atomic',atomic);monkeypatch.setattr(finite,'_publish',publish)
    prepare(tmp_path,[c])
    with opened(tmp_path,'slack',c) as store:assert not claim(store,'slack',c)
    with opened(tmp_path,'smtp',c) as store:assert claim(store,'smtp',c)=='uncertain'


def test_preparation_request_changed_refuses_without_reset(tmp_path):
    prepare(tmp_path);before=(tmp_path/'notifications/finite-receipts-preparation.json').read_bytes()
    assert prepare(tmp_path,[ctx(2,born=NOW-1)])['maintenance_required']
    assert (tmp_path/'notifications/finite-receipts-preparation.json').read_bytes()==before


def test_failed_claim_transaction_cannot_become_external_permission(tmp_path):
    prepare(tmp_path);c=ctx()
    with opened(tmp_path,'slack',c) as store:
        store.db.execute("CREATE TRIGGER fail_claim BEFORE INSERT ON retention BEGIN SELECT RAISE(ABORT,'fixture'); END")
        with pytest.raises(sqlite3.IntegrityError):claim(store,'slack',c)
        assert store.db.execute('SELECT * FROM receipts').fetchall()==[]
        store.db.execute('DROP TRIGGER fail_claim')
        assert claim(store,'slack',c)


@pytest.mark.parametrize('history',[0,1000,10000,100000])
@pytest.mark.parametrize('current',[False,True])
def test_ordinary_and_quiet_operation_counts_are_independent_of_unrelated_history(tmp_path,history,current):
    prepare(tmp_path);c=ctx()
    with opened(tmp_path,'slack',c) as store:
        db=store.db
        # Supported indexed rows in another exact scope must not be scanned or
        # collected by maintenance of this scope. H is never native history.
        db.execute('BEGIN IMMEDIATE')
        db.executemany('INSERT INTO receipts VALUES(?,NULL,NULL)',((sha('history'+str(i)),) for i in range(history)))
        db.executemany('INSERT INTO retention VALUES(?,?,?,?,?,?,0,?,?)',((sha('history'+str(i)),'slack',OTHER,sha(OTHER),NOW-1,NOW+finite.TTL-1,sha('target'),sha('body')) for i in range(history)))
        db.commit()
        statements=[];vm=[0]
        db.set_trace_callback(statements.append)
        db.set_progress_handler(lambda:(vm.__setitem__(0,vm[0]+1) or 0),1)
        assert store.get(c['fingerprint'])=={}
        if current:assert claim(store,'slack',c)
        result=store.finite.maintain(scopes()[:2],[],complete=True)
        db.set_trace_callback(None);db.set_progress_handler(None,0)
        assert result['removed']==0
        assert db.execute('SELECT COUNT(*) FROM retention').fetchone()[0]==history+int(current)
        print('FINITE_RECEIPT_COUNTS',json.dumps(dict(history=history,current=current,sql=len(statements),vm=vm[0]),sort_keys=True))
        # B-tree depth can change VM seek internals; SQL shape and result work
        # stay fixed. No SELECT of all receipts/retention rows is permitted.
        assert not any('FROM retention' in s and 'WHERE' not in s for s in statements)
        observed=(len(statements),vm[0])
        assert observed==COUNTS.setdefault((current,bool(history)),observed)
        base=COUNTS[(current,False)]
        assert observed[0]==base[0] and observed[1]<=base[1]+16


def test_quiet_unpin_progressively_restores_recent_cap_without_new_claim(tmp_path):
    entries=[ctx(i,born=NOW-100+i/1000) for i in range(finite.LIMIT+40)]
    seed_legacy(tmp_path,entries)
    pins=[(c['fingerprint'],'slack') for c in entries]
    prepare(tmp_path,entries,pins)
    result=maintenance(tmp_path,now=NOW+1,pins=[(fp,p,False) for fp,p in pins])
    assert result['families']['dual']['removed']==finite.BUDGET
    assert len(metadata(tmp_path,'slack'))==finite.LIMIT+8
    assert maintenance(tmp_path,now=NOW+2)['families']['dual']['removed']==8
    assert len(metadata(tmp_path,'slack'))==finite.LIMIT


def test_corrupt_dual_maintenance_does_not_starve_independent_fallback(tmp_path):
    prepare(tmp_path);c=ctx()
    with opened(tmp_path,'smtp',c) as store:claim(store,'smtp',c)
    path=tmp_path/'notifications/dual-deliveries.sqlite3'
    path.write_bytes(b'retained corrupt fixture')
    result=maintenance(tmp_path,now=NOW+finite.TTL+2)
    assert result['status']=='partial' and result['families']['dual']['status']=='unavailable'
    assert result['families']['fallback']['removed']==1
    assert path.read_bytes()==b'retained corrupt fixture'


def test_provider_subset_does_not_read_or_poison_other_provider_identity(tmp_path):
    prepare(tmp_path);c=ctx()
    with opened(tmp_path,'lark',c) as store:claim(store,'lark',c)
    prior=metadata(tmp_path,'lark')
    only=deepcopy(c);only['providers']={'slack':c['providers']['slack']}
    with opened(tmp_path,'slack',only) as store:
        assert store.get(c['fingerprint'])=={}
        assert claim(store,'slack',only)
        store.confirm_sent(c['fingerprint'],'slack')
    assert [row for row in metadata(tmp_path,'lark') if row[1]=='lark']==prior


def test_missing_metadata_pk_refuses_before_scanning_metadata_history(tmp_path):
    prepare(tmp_path)
    path=tmp_path/'notifications/dual-deliveries.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE finite_metadata RENAME TO old_meta')
        db.execute('CREATE TABLE finite_metadata(id INTEGER,value TEXT NOT NULL)')
        db.execute('INSERT INTO finite_metadata SELECT * FROM old_meta')
        db.execute('DROP TABLE old_meta')
        statements=[];db.set_trace_callback(statements.append)
        with pytest.raises(finite.RetentionError,match='schema_invalid'):
            finite.FiniteReceipts(db,'dual',receipt_context=ctx())
        assert not any('FROM finite_metadata' in s for s in statements)


def test_partial_activation_changed_current_projection_finishes_forward_then_fresh_gc(tmp_path,monkeypatch):
    old=ctx('old',born=NOW-finite.TTL-1);seed_legacy(tmp_path,[old])
    publish=finite._publish;hit=[]
    def crash(directory,family,plan):
        publish(directory,family,plan)
        if family=='dual' and not hit:hit.append(True);raise OSError('after first family')
    monkeypatch.setattr(finite,'_publish',crash)
    with pytest.raises(OSError):prepare(tmp_path,[old],[(old['fingerprint'],'slack')])
    original=(tmp_path/'notifications/finite-receipts-preparation.json').read_bytes()
    monkeypatch.setattr(finite,'_publish',publish)
    result=prepare(tmp_path,[],[],now=NOW+10)
    assert result['status']=='forward_completed' and result['maintenance_required']
    assert (tmp_path/'notifications/finite-receipts-preparation.json').read_bytes()==original
    assert metadata(tmp_path,'slack')[0][6]==1
    # Today's genuine absence proof removes only this old pin; no receipt is
    # labeled completed and no old sender/provider action is involved.
    maintenance(tmp_path,now=NOW+11,pins=[(old['fingerprint'],'slack',False)])
    assert metadata(tmp_path,'slack')==[]
    newer=ctx('new',born=NOW+12)
    with opened(tmp_path,'slack',newer,now=NOW+12) as store:assert claim(store,'slack',newer)


def test_prepared_only_replan_is_explicit_and_retains_floor_without_history_archive(tmp_path,monkeypatch):
    c=ctx(born=NOW-1);seed_legacy(tmp_path,[c]);atomic=finite._atomic
    def crash(path,value):
        atomic(path,value)
        if path.name=='finite-receipts-preparation.json':raise OSError('prepared only')
    monkeypatch.setattr(finite,'_atomic',crash)
    with pytest.raises(OSError):prepare(tmp_path,[c])
    path=tmp_path/'notifications/finite-receipts-preparation.json';old=json.loads(path.read_text())
    monkeypatch.setattr(finite,'_atomic',atomic)
    with pytest.raises(finite.RetentionError,match='replan_required'):prepare(tmp_path,[],now=NOW-10)
    result=finite.prepare_notification_receipts(tmp_path,scopes=scopes(),contexts=[],sender_fence=nullcontext,
        clock=lambda:NOW-10,replan_prepared=True)
    new=json.loads(path.read_text())
    assert result['status']=='prepared' and new['born']==old['born']
    assert new['prior_prepared_sha256']==old['plan_sha256']
    assert sum(map(len,new['retained'].values()))==0
    assert len(list(path.parent.glob('finite-receipts-preparation*')))==1


def test_partial_conversion_refuses_changed_unconverted_source_preserving_new_claim(tmp_path,monkeypatch):
    c=ctx(born=NOW-1);seed_legacy(tmp_path,[c]);publish=finite._publish;hit=[]
    def crash(directory,family,plan):
        publish(directory,family,plan)
        if family=='dual' and not hit:hit.append(True);raise OSError('after first family')
    monkeypatch.setattr(finite,'_publish',crash)
    with pytest.raises(OSError):prepare(tmp_path,[c])
    newer=ctx('new',born=NOW)
    with FallbackDeliveryReceipts(tmp_path) as store:claim(store,'smtp',newer)
    raw=(tmp_path/'notifications/fallback-deliveries.sqlite3').read_bytes()
    monkeypatch.setattr(finite,'_publish',publish)
    with pytest.raises(finite.RetentionError,match='source_changed'):prepare(tmp_path,[],now=NOW+1)
    assert (tmp_path/'notifications/fallback-deliveries.sqlite3').read_bytes()==raw


def test_partial_recovery_reports_omitted_current_pin_without_blocking_newer_claim(tmp_path,monkeypatch):
    kept=ctx('kept',born=NOW-1);omitted=ctx('omitted',born=NOW-2)
    seed_legacy(tmp_path,[kept,omitted]);publish=finite._publish;hit=[]
    def crash(directory,family,plan):
        publish(directory,family,plan)
        if family=='dual' and not hit:hit.append(True);raise OSError('first family')
    monkeypatch.setattr(finite,'_publish',crash)
    with pytest.raises(OSError):prepare(tmp_path,[kept])
    monkeypatch.setattr(finite,'_publish',publish)
    result=prepare(tmp_path,[omitted],[(omitted['fingerprint'],'slack')],now=NOW+1)
    assert result['status']=='forward_completed_with_pin_gaps'
    assert result['pin_gaps']==[dict(fingerprint=omitted['fingerprint'],provider='slack',reason='current_pin_not_in_compact_recovery')]
    new=ctx('independent',born=NOW+2)
    with opened(tmp_path,'slack',new,now=NOW+2) as store:assert claim(store,'slack',new)


@pytest.mark.parametrize('field',['target_sha256','payload_sha256','thread_id','scope','born'])
def test_oversized_malformed_point_metadata_is_refused_without_new_claim(tmp_path,field):
    prepare(tmp_path);c=ctx()
    with opened(tmp_path,'slack',c) as store:
        assert claim(store,'slack',c)
        store.db.execute('UPDATE retention SET '+field+'=?',('x'*1000000,))
        with pytest.raises(finite.RetentionError):claim(store,'slack',c)
        assert store.db.execute('SELECT slack FROM receipts').fetchone()==('uncertain',)


@pytest.mark.parametrize('operation',['claim_cap','quiet_cap','quiet_expiry'])
def test_malformed_large_victim_key_is_bounded_and_refuses_only_candidate(tmp_path,operation):
    prepare(tmp_path);c=ctx('new',born=NOW+1000)
    now=NOW+finite.TTL+1000 if operation=='quiet_expiry' else c['born']
    with opened(tmp_path,'slack',c,now=now) as store:
        db=store.db
        count=1 if operation=='quiet_expiry' else finite.LIMIT+1
        for i in range(count):
            fp='x'*1000000 if i==0 else sha('victim'+str(i))
            db.execute("INSERT INTO receipts VALUES(?,'uncertain',NULL)",(fp,))
            db.execute('INSERT INTO retention VALUES(?,?,?,?,?,?,0,?,?)',
                (fp,'slack',SID,sha(SID),NOW+i+1,NOW+i+1+finite.TTL,sha('target'),sha('payload')))
        malformed='x'*1000000
        before=db.execute('SELECT provider,born,expires,protected,target_sha256,payload_sha256 FROM retention WHERE fingerprint=?',(malformed,)).fetchone()
        observed=[]
        def bounded_rows(cursor,row):
            if len(row)==2:
                observed.append(row)
                assert all(not isinstance(v,str) or len(v)<=64 for v in row)
            return row
        db.row_factory=bounded_rows
        if operation=='claim_cap':
            assert claim(store,'slack',c)
            assert not claim(store,'slack',c)
        else:
            result=store.finite.maintain(scopes()[:2],[],complete=True)
            assert result['status']=='maintained' and result['removed']==0
        db.row_factory=None
        assert (None,'slack') in observed
        assert db.execute('SELECT COUNT(*) FROM retention').fetchone()[0]==count
        assert db.execute('SELECT provider,born,expires,protected,target_sha256,payload_sha256 FROM retention WHERE fingerprint=?',(malformed,)).fetchone()==before
        assert (db.execute('SELECT 1 FROM receipts WHERE fingerprint=?',(c['fingerprint'],)).fetchone() is not None)==(operation=='claim_cap')


@pytest.mark.parametrize('family,alteration',[
    ('dual','CREATE TABLE sqliteX_future(value TEXT)'),
    ('fallback','CREATE TABLE sqliteX_future(value TEXT)'),
    ('dual','ALTER TABLE receipts ADD COLUMN future_field TEXT'),
    ('fallback','ALTER TABLE receipts ADD COLUMN future_field TEXT'),
    ('dual','ALTER TABLE metadata ADD COLUMN future_field TEXT'),
    ('dual','CREATE VIEW sqliteX_future AS SELECT fingerprint FROM receipts'),
    ('dual',"CREATE TRIGGER future_hook AFTER INSERT ON receipts BEGIN SELECT 1; END"),
])
def test_cold_unknown_persistent_schema_refuses_preserving_original_bytes(tmp_path,family,alteration):
    c=ctx(born=NOW-1);seed_legacy(tmp_path,[c])
    path=tmp_path/'notifications'/(finite.NAMES[family]+'.sqlite3')
    with sqlite3.connect(path) as db:db.execute(alteration)
    original={p:p.read_bytes() for p in (tmp_path/'notifications').iterdir() if p.is_file()}
    with pytest.raises(finite.RetentionError,match='source_invalid'):prepare(tmp_path,[c])
    assert all(p.read_bytes()==value for p,value in original.items())
    assert not (tmp_path/'notifications/finite-receipts-preparation.json').exists()


def exported_fixture(path,c,*,sent=False):
    seed_legacy(path,[c])
    marker=path/'notifications/dual-deliveries.json'
    value=json.loads(marker.read_text());value['future_settings']={'preserve':True}
    marker.write_text(json.dumps(value))
    with DualDeliveryReceipts(path) as store:
        if sent:store.confirm_sent(c['fingerprint'],'slack')
        store.export()
    return marker


def test_cold_exported_old_sender_append_confirm_and_marker_metadata_are_retained(tmp_path):
    old=ctx('old',born=NOW-2);new=ctx('new',born=NOW-1)
    marker=exported_fixture(tmp_path,old)
    exported=json.loads(marker.read_text())
    exported['events'][old['fingerprint']]['slack']='sent'
    exported['events'][new['fingerprint']]={'slack':'uncertain','lark':'sent'}
    marker.write_text(json.dumps(exported))
    result=prepare(tmp_path,[old,new],[(new['fingerprint'],'slack')])
    assert result['retained']['dual']==3
    with opened(tmp_path,'slack',old) as store:assert store.get(old['fingerprint'])=={'slack':'sent'}
    with opened(tmp_path,'slack',new) as store:
        assert store.get(new['fingerprint'])=={'slack':'uncertain','lark':'sent'}
        assert not claim(store,'slack',new)
        assert store.finite._row(new['fingerprint'],'slack')[4]==1
    assert json.loads(marker.read_text())['future_settings']=={'preserve':True}


@pytest.mark.parametrize('loss',['event','provider','sent_downgrade'])
def test_cold_exported_old_sender_loss_refuses_without_overwrite(tmp_path,loss):
    c=ctx(born=NOW-1);marker=exported_fixture(tmp_path,c,sent=True)
    exported=json.loads(marker.read_text())
    if loss=='event':exported['events'].pop(c['fingerprint'])
    elif loss=='provider':exported['events'][c['fingerprint']].pop('slack')
    else:exported['events'][c['fingerprint']]['slack']='uncertain'
    marker.write_text(json.dumps(exported))
    original={p:p.read_bytes() for p in marker.parent.iterdir() if p.is_file()}
    with pytest.raises(finite.RetentionError,match='reconcile_loss'):prepare(tmp_path,[c])
    assert all(p.read_bytes()==raw for p,raw in original.items())
    assert not (marker.parent/'finite-receipts-preparation.json').exists()


def test_cold_conflicting_saved_marker_fields_refuse_without_overwrite(tmp_path):
    c=ctx(born=NOW-1);seed_legacy(tmp_path,[c])
    marker=tmp_path/'notifications/dual-deliveries.json';value=json.loads(marker.read_text())
    value['future_settings']='current';marker.write_text(json.dumps(value))
    path=marker.with_suffix('.sqlite3')
    with sqlite3.connect(path) as db:
        prior=json.loads(db.execute('SELECT marker_value FROM metadata WHERE id=1').fetchone()[0])
        prior['future_settings']='conflicting';db.execute('UPDATE metadata SET marker_value=?',(json.dumps(prior),))
    original={p:p.read_bytes() for p in marker.parent.iterdir() if p.is_file()}
    with pytest.raises(finite.RetentionError,match='marker_collision'):prepare(tmp_path,[c])
    assert all(p.read_bytes()==raw for p,raw in original.items())


@pytest.mark.parametrize('changed',['exported_rows','saved_marker'])
def test_cold_source_digest_binds_exported_rows_and_saved_metadata(tmp_path,monkeypatch,changed):
    c=ctx(born=NOW-1);marker=exported_fixture(tmp_path,c);atomic=finite._atomic;observed=[]
    def mutate(path,value):
        atomic(path,value)
        if path.name=='finite-receipts-preparation.json':
            if changed=='exported_rows':
                incoming=json.loads(marker.read_text());incoming['events'][c['fingerprint']]['slack']='sent'
                marker.write_text(json.dumps(incoming))
            else:
                with sqlite3.connect(marker.with_suffix('.sqlite3')) as db:
                    saved=json.loads(db.execute('SELECT marker_value FROM metadata').fetchone()[0])
                    saved['future_settings']['later']=True
                    db.execute('UPDATE metadata SET marker_value=?',(json.dumps(saved),))
            observed.append({p:p.read_bytes() for p in marker.parent.iterdir() if p.is_file()})
    monkeypatch.setattr(finite,'_atomic',mutate)
    with pytest.raises(finite.RetentionError,match='source_changed'):prepare(tmp_path,[c])
    assert observed and all(p.read_bytes()==raw for p,raw in observed[0].items())
