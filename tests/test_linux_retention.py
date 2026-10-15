"""Linux orchestration respects supported capability and lexical cold fences."""
from contextlib import nullcontext

from codex_watchdog.control_context import current_control, current_effect
from codex_watchdog.linux_retention import prepare_linux_retention
from codex_watchdog.relay_authority_migration import write_authority_snapshot
from test_session_authority_retention import native, snapshot, migrate
from test_linux_node import nodes


def test_current_actor_can_compose_the_actual_cold_owner_lock(native):
    write_authority_snapshot(native["source"],snapshot())
    authority=native["authority"]
    original=native["inventory"];seen=[]
    def reader():
        assert current_control()==(authority.store,authority.token)
        # This is the real lock composition used by the existing Linux actor.
        # Opening a second owner descriptor would fail, not establish proof.
        with current_effect("writer"):
            seen.append(True)
            return original()
    assert migrate(native,inflight_reader=reader)["status"]=="migrated"
    assert len(seen)==2


def test_missing_current_actor_skips_optional_preparation_without_creating_it():
    class Owner:pass
    assert prepare_linux_retention(dict(owner=Owner()))["status"]=="unsupported_current_native_producer"
