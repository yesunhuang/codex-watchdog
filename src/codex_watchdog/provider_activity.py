"""Fresh admitted provider commands are activity; polling/ACKs are not."""
from .recent_state import epoch
from .session_activity import SessionActivity


def message_is_current(thread_store, address, created_at, *, resolution=0):
    """Check an immutable provider creation time against its current parent.

    OneBot's integer seconds are compared at that documented resolution. A
    missing timestamp or ancient callback never gets a new local birth time.
    """
    created=epoch(created_at)
    if created is None:return False
    journal=thread_store.journal
    with journal.transaction() as db:
        entry=journal.get(db,'threads',address)
        from .recent_state import now, TTL_SECONDS
        current=now(journal,db)
        parent=epoch(entry.get('created_at')) if entry is not None else None
        return (parent is not None and created+resolution>=parent
                and created<=current and current-created<TTL_SECONDS)


def record_admitted_activity(thread_store, target, created_at):
    """Called only after a new exact-principal journal claim was committed."""
    created=epoch(created_at)
    if created is None:return False
    try:
        journal=thread_store.journal
        authority=journal.authority
        runtime=authority.runtime if authority is not None else thread_store.runtime
        SessionActivity(runtime,clock=journal.clock).observe(target.thread_id,
            genuine_at=created,turn_active=None,queue_active=None,legacy=True)
        return True
    except Exception:
        return False  # optional activity publication never redispatches a command
