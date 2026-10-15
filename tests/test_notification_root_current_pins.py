"""Current receipt projection is explicit and not inferred from provider keys."""
from contextlib import nullcontext
import sqlite3

import pytest

from codex_watchdog import notification_root_retention as root
from codex_watchdog.notification_receipts import DualDeliveryReceipts
from test_notification_root_retention import ctx, scopes, NOW


def prepare(runtime, *, contexts=(), pins=(), now=NOW):
    return root.prepare_notification_root(runtime, scopes=scopes(), current_keys=[],
        contexts=contexts, current_pins=pins, sender_fence=nullcontext, clock=lambda:now)


def test_reused_root_installs_verified_current_receipt_pin_and_preserves_one_attempt(tmp_path):
    first=prepare(tmp_path)
    current=ctx("new-current",born=NOW+1)
    fp=current["fingerprint"]
    path=tmp_path/"notifications/dual-deliveries.sqlite3"
    with DualDeliveryReceipts(tmp_path,receipt_context=current,clock=lambda:NOW+1) as receipts:
        assert receipts.claim(fp,"slack")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT protected FROM retention WHERE fingerprint=? AND provider='slack'",(fp,)).fetchone()==(0,)
    second=prepare(tmp_path,contexts=[current],pins=[(fp,"slack")],now=NOW+2)
    assert second["floor"]==first["floor"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT protected FROM retention WHERE fingerprint=? AND provider='slack'",(fp,)).fetchone()==(1,)
    with DualDeliveryReceipts(tmp_path,receipt_context=current,clock=lambda:NOW+3) as receipts:
        assert receipts.get(fp)=={"slack":"uncertain"}
        assert not receipts.claim(fp,"slack")
    saved=(tmp_path/"notifications"/root.READY).read_bytes()
    missing=ctx("missing-current",born=NOW+1)
    with pytest.raises(root.RetentionError,match="current_pin_gap"):
        prepare(tmp_path,contexts=[missing],pins=[(missing["fingerprint"],"slack")],now=NOW+4)
    assert (tmp_path/"notifications"/root.READY).read_bytes()==saved
    with DualDeliveryReceipts(tmp_path,receipt_context=current,clock=lambda:NOW+4) as receipts:
        assert receipts.get(fp)=={"slack":"uncertain"} and not receipts.claim(fp,"slack")
