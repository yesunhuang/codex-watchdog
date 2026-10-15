"""No redirected lock writes; freshness rechecked after cooperative exclusion."""
from pathlib import Path
import os

import pytest

from codex_watchdog.notification_receipt_retention import RetentionError
from codex_watchdog.notification_cutover import prepare_notification_cutover
from test_notification_cutover import Rig, NOW


def test_hardlinked_notification_lock_is_refused_without_touching_external_bytes(tmp_path):
    rig=Rig(tmp_path);rig.write()
    outside=tmp_path/"user-file";outside.write_bytes(b"preserved user sentinel")
    locks=rig.runtime/"locks";locks.mkdir()
    os.link(outside,locks/"notifications.lock")
    with pytest.raises(RetentionError,match="lock_invalid"):
        rig.call()
    assert outside.read_bytes() == b"preserved user sentinel"
    assert (locks/"notifications.lock").stat().st_nlink == 2
    assert not (rig.runtime/"notifications").exists()
    assert not (locks/"foreground-run.lock").exists()


def test_redirected_lock_directory_refuses_before_creating_any_external_lock(tmp_path):
    rig=Rig(tmp_path);rig.write()
    outside=tmp_path/"unrelated";outside.mkdir()
    sentinel=outside/"user-file";sentinel.write_bytes(b"untouched")
    try:(rig.runtime/"locks").symlink_to(outside,target_is_directory=True)
    except OSError:pytest.skip("filesystem does not permit directory symlink")
    with pytest.raises(RetentionError,match="path_invalid"):
        rig.call()
    assert sorted(path.name for path in outside.iterdir()) == ["user-file"]
    assert sentinel.read_bytes() == b"untouched" and not (rig.runtime/"notifications").exists()


def test_native_receipt_freshness_is_rechecked_inside_exclusion_before_preparation(tmp_path):
    rig=Rig(tmp_path);rig.write(observed=NOW)
    samples=iter((NOW,NOW+31))
    with pytest.raises(RetentionError,match="old_sender_receipt_invalid"):
        prepare_notification_cutover(rig.runtime,rig.projection_path,rig.proof_path,clock=lambda:next(samples))
    assert not (rig.runtime/"notifications").exists()
