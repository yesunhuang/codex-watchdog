from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
import pytest
from codex_watchdog.reply_tickets import ReplyTickets


def test_construct_before_switch_cannot_write_old_authority_under_new_lock(tmp_path):
    old=tmp_path/'shared';new=tmp_path/'exact'
    authority=SimpleNamespace(runtime=old)
    @contextmanager
    def lock(provider):
        authority.runtime=new
        yield
    authority.journal_lock=lock
    journal=ReplyTickets(tmp_path/'local/slack/state.json',tmp_path/'local/locks/state.lock','slack',tmp_path/'local',lambda:{},authority=authority)
    with pytest.raises(ValueError,match='binding_stale'):
        with journal.transaction():pass
    assert not old.exists() and not new.exists()
