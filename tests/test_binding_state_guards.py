import pytest

from codex_watchdog.relay import RelayTarget
from test_destination_binding import setup, record, THREAD


def initiate(env, number=1, parent=None):
    return env.relay.binding.store.begin("control-%d" % number, env.default, parent or env.parent,
        env.user, "bind", message_id=("om_control%010d" % number if env.kind=="lark" else str(1000+number)),
        created_at=env.clock[0])


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_storage_does_not_normalize_confirmation(tmp_path,kind):
    env=setup(tmp_path,kind)
    result=initiate(env)
    env.clock[0]+=2
    store=env.relay.binding.store
    assert store.lookup(result["challenge"]+" ") is None
    with pytest.raises(ValueError):
        store.complete(result["challenge"]+" ",env.user,env.dest,
            message_id="om_confirm000001" if kind=="lark" else "2000",created_at=env.clock[0])


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_superseded_generation_cannot_emit_old_hello(tmp_path,kind):
    env=setup(tmp_path,kind)
    store=env.relay.binding.store
    result=initiate(env)
    env.clock[0]+=2
    bound=store.complete(result["challenge"],env.user,env.dest,
        message_id="om_confirm000001" if kind=="lark" else "2000",created_at=env.clock[0])
    parent="om_parent0000002" if kind=="lark" else "102"
    record(env.relay,env.default,parent,env.target,"b"*64)
    initiate(env,2,parent)
    assert store.claim_hello(bound["operation_key"]) is None


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_malformed_existing_route_cannot_be_silently_replaced(tmp_path,kind):
    env=setup(tmp_path,kind)
    store=env.relay.binding.store
    journal=env.relay.thread_store.journal
    with journal.transaction() as db:
        journal.put(db,"session_routes",store.routes._route_key(THREAD),dict(schema_version=99,destination="bad"))
    with pytest.raises(ValueError):initiate(env)
    with journal.transaction() as db:
        assert len(journal.mappings(db,active=True))==1


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_hello_rejects_source_target_identity_change(tmp_path,kind):
    env=setup(tmp_path,kind)
    store=env.relay.binding.store
    result=initiate(env)
    env.clock[0]+=2
    bound=store.complete(result["challenge"],env.user,env.dest,
        message_id="om_confirm000001" if kind=="lark" else "2000",created_at=env.clock[0])
    journal=env.relay.thread_store.journal
    key=env.relay.thread_store._address(env.default,env.parent)
    with journal.transaction() as db:
        row=journal.get(db,"threads",key)
        row["target"]=RelayTarget("changed-workspace",THREAD,"process_local").to_dict()
        journal.put(db,"threads",key,row)
    assert store.claim_hello(bound["operation_key"]) is None


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_confirmation_rejects_changed_source_before_route_commit(tmp_path, kind):
    env = setup(tmp_path, kind)
    store = env.relay.binding.store
    result = initiate(env)
    journal = env.relay.thread_store.journal
    key = env.relay.thread_store._address(env.default, env.parent)
    with journal.transaction() as db:
        row = journal.get(db, "threads", key)
        row["target"] = RelayTarget("changed-workspace", THREAD, "process_local").to_dict()
        journal.put(db, "threads", key, row)
    env.clock[0] += 2
    with pytest.raises(ValueError, match="source_identity_mismatch"):
        store.complete(result["challenge"], env.user, env.dest,
            message_id="om_confirm000001" if kind == "lark" else "2000", created_at=env.clock[0])
    assert store.routes.destination(THREAD) is None
    assert len(store.pending()) == 1


@pytest.mark.parametrize("kind", ["lark", "onebot"])
def test_disabling_replies_does_not_silently_redirect_bound_notifications(tmp_path,kind):
    from dataclasses import replace
    from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
    env=setup(tmp_path,kind)
    request=initiate(env)
    env.clock[0]+=2
    env.relay.binding.store.complete(request["challenge"],env.user,env.dest,
        message_id="om_confirm000001" if kind=="lark" else "2000",created_at=env.clock[0])
    config=replace(env.relay.config,allowed_user_ids=())
    notifier=EnvironmentNotifier(tmp_path,NotificationConfig(**{kind:config}),**{kind+"_api":env.api})
    result=notifier.notify(NotificationEvent("workspace","waiting","later-output","Done","Output",env.target))
    assert result.status=="sent"
    assert env.api.calls[-1]["destination"]==env.dest
