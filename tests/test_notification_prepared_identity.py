"""A well-formed finite family from a different cold plan is still refused."""
from contextlib import nullcontext
import shutil

import pytest

from codex_watchdog import notification_root_retention as root
from codex_watchdog.notification_receipts import DualDeliveryReceipts,FallbackDeliveryReceipts,NotificationReceiptError
from test_notification_root_retention import scopes,ctx,NOW


@pytest.mark.parametrize('family',['dual','fallback'])
def test_native_receipt_entry_binds_the_activated_root_plan(tmp_path,family):
    current,foreign=tmp_path/'current',tmp_path/'foreign'
    def prepare(path,now):
        return root.prepare_notification_root(path,scopes=scopes(),contexts=[],current_keys=[],
            current_pins=(),sender_fence=nullcontext,clock=lambda:now)
    assert prepare(current,NOW)['plan_sha256']!=prepare(foreign,NOW-100)['plan_sha256']
    name=('dual' if family=='dual' else 'fallback')+'-deliveries.sqlite3'
    path=current/'notifications'/name
    shutil.copyfile(foreign/'notifications'/name,path)
    if family=='dual':
        shutil.copyfile(foreign/'notifications/dual-deliveries.json',current/'notifications/dual-deliveries.json')
    before=path.read_bytes()
    receipts=DualDeliveryReceipts if family=='dual' else FallbackDeliveryReceipts
    with pytest.raises(NotificationReceiptError,match='root_plan'):
        with receipts(current,receipt_context=ctx('fresh',born=NOW+1),clock=lambda:NOW+1):
            pass
    assert path.read_bytes()==before
