"""Corrupt old values cannot roll back independent pre-attempt claims/pins."""
from contextlib import closing,nullcontext
import sqlite3

import pytest

from codex_watchdog import notification_receipt_retention as finite
from codex_watchdog.notification_context import observer_scope
from codex_watchdog.notifications import EnvironmentNotifier,NotificationConfig
from test_notification_root_integration import prepare,event
from test_notification_root_retention import SID,NOW,sha


def seed(db, family, subject, scope, provider, *, protected=False):
    for n in range(1,257):
        fp=sha(n);born=NOW+n
        if family=='dual':db.execute("INSERT INTO receipts VALUES(?,'uncertain',NULL)",(fp,))
        else:db.execute("INSERT INTO receipts VALUES(?,?,?,?,'uncertain')",(fp,provider,sha('target'),sha('payload')))
        db.execute('INSERT INTO retention VALUES(?,?,?,?,?,?,0,?,?)',(fp,provider,subject,scope,born,born+finite.TTL,sha('target'),sha('payload')))
    db.execute("UPDATE retention SET target_sha256='malformed-preserved' WHERE fingerprint=?",(sha(1),))
    if protected:
        fp=sha('protected')
        if family=='dual':db.execute("INSERT INTO receipts VALUES(?,'uncertain',NULL)",(fp,))
        else:db.execute("INSERT INTO receipts VALUES(?,?,?,?,'uncertain')",(fp,provider,sha('target'),sha('payload')))
        db.execute('INSERT INTO retention VALUES(?,?,?,?,?,?,1,?,?)',(fp,provider,subject,scope,NOW+1,NOW+1+finite.TTL,sha('target'),sha('payload')))
    db.commit()


@pytest.mark.parametrize('family,provider',[('dual','slack'),('fallback','smtp')])
@pytest.mark.parametrize('observer',[False,True])
def test_independent_claims_and_verified_pins_commit_despite_bad_historical_candidate(tmp_path,family,provider,observer):
    subject,scope=observer_scope('observer') if observer else (SID,sha(SID))
    finite.prepare_notification_receipts(tmp_path,scopes=[(subject,scope,provider)],contexts=[],
        sender_fence=nullcontext,clock=lambda:NOW)
    path=tmp_path/'notifications'/(finite.NAMES[family]+'.sqlite3')
    with closing(sqlite3.connect(path,isolation_level=None)) as db:
        seed(db,family,subject,scope,provider,protected=True)
        bad=db.execute('SELECT * FROM retention WHERE fingerprint=?',(sha(1),)).fetchone()
        protected=db.execute('SELECT * FROM retention WHERE fingerprint=?',(sha('protected'),)).fetchone()
        for n in (300,301):
            ctx=dict(schema_version=1,thread_id=SID,scope_sha256=scope,fingerprint=sha(n),born=NOW+n,
                     providers={provider:dict(target_sha256=sha('new-target'),payload_sha256=sha('new-payload'))})
            if observer:
                del ctx['thread_id']
                ctx.update(schema_version=2,subject_kind='observer',observer_scope_sha256=scope)
            receipts=finite.FiniteReceipts(db,family,receipt_context=ctx,clock=lambda:NOW+n)
            assert receipts.claim(sha(n),provider) is None
            assert receipts.claim(sha(n),provider)=='uncertain'
            assert db.execute('SELECT * FROM retention WHERE fingerprint=?',(sha(1),)).fetchone()==bad
        receipts=finite.FiniteReceipts(db,family,clock=lambda:NOW+finite.TTL+1000)
        pins=[(sha(301),provider,True)]
        if observer:
            with pytest.raises(finite.RetentionError,match='observer_pin_invalid'):
                receipts.maintain([(subject,scope,provider)],pins,complete=True)
            pins=[]  # Observer subjects never manufacture a native pin.
        result=receipts.maintain([(subject,scope,provider)],pins,complete=True)
        assert 0<result['removed']<result['examined']<=2*finite.BUDGET
        assert db.execute('SELECT protected FROM retention WHERE fingerprint=?',(sha(301),)).fetchone()==(0 if observer else 1,)
        assert db.execute('SELECT * FROM retention WHERE fingerprint=?',(sha(1),)).fetchone()==bad
        assert db.execute('SELECT * FROM retention WHERE fingerprint=?',(sha('protected'),)).fetchone()==protected


def test_actual_notifier_sends_later_independent_outputs_once_with_corrupt_capacity_history(tmp_path):
    prepare(tmp_path)
    from codex_watchdog.models import sha256_text
    path=tmp_path/'notifications/dual-deliveries.sqlite3'
    with closing(sqlite3.connect(path)) as db:
        seed(db,'dual',SID,sha256_text('fixture'),'slack')
        bad=db.execute('SELECT * FROM retention WHERE fingerprint=?',(sha(1),)).fetchone()
    calls=[];clock=[NOW+300]
    def post(token,method,payload,timeout):
        calls.append(payload);return dict(channel=payload['channel'],ts='1792008000.000001')
    notifier=EnvironmentNotifier(tmp_path,
        NotificationConfig(slack_bot_token='xoxb-fixture',slack_channel_id='C12345678'),
        slack_api_post=post,clock=lambda:clock[0])
    for n in (300,301):
        clock[0]=NOW+n;current=event(born=clock[0],state='independent-'+str(n))
        assert notifier.notify(current).status=='sent'
        assert notifier.notify(current).status=='suppressed'
    assert len(calls)==2
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT * FROM retention WHERE fingerprint=?',(sha(1),)).fetchone()==bad
