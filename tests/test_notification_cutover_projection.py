"""Current configuration projection, with no historical/native/provider reads."""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_watchdog.notification_context import observer_scope
from codex_watchdog.notification_cutover import notifier_cutover_projection
from codex_watchdog.notification_receipt_retention import RetentionError
from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig, NotificationEvent
from test_notification_receipt_retention import SID, NOW, ctx

OTHER="22222222-3333-4444-8555-666666666666"


def workspace(name,session=SID):
    return SimpleNamespace(workspace_id=name,session_id=session)


def project(notifier,**kwargs):
    return notifier_cutover_projection(notifier,
        workspaces=kwargs.pop("workspaces",[workspace("fixture")]),
        observer_workspace_ids=kwargs.pop("observers",["resume-prompt"]),
        event_types=("git_attention","codex_parked","session_hello"),
        current_contexts=kwargs.pop("contexts",[]), current_http_pins=kwargs.pop("pins",[]),**kwargs)


def test_current_projection_uses_actual_suppression_keys_and_never_enumerates_history(tmp_path,monkeypatch):
    runtime=tmp_path/"runtime";runtime.mkdir()
    note=runtime/"unrelated-history.json";note.write_bytes(b"protected sentinel")
    notifier=EnvironmentNotifier(runtime,NotificationConfig())
    def forbidden(*a,**k):pytest.fail("projection enumerated history")
    with monkeypatch.context() as patch:
        patch.setattr(Path,"iterdir",forbidden);patch.setattr(Path,"rglob",forbidden)
        patch.setattr(Path,"glob",forbidden);patch.setattr(Path,"open",forbidden)
        value=project(notifier,workspaces=[workspace("fixture"),workspace("新しい作業",OTHER)],observers=["resume-prompt","vscode-discovery"])
    expected={NotificationEvent(name,kind,"state","subject","message").dedupe_key()
              for name in ("fixture","新しい作業","resume-prompt","vscode-discovery")
              for kind in ("git_attention","codex_parked","session_hello")}
    assert set(value["projection"]["current_keys"]) == expected and len(expected)==12
    assert len(value["projection"]["scopes"]) == 16
    assert value["projection"]["current_pins"] == value["projection"]["contexts"] == []
    assert value["sender_runtimes"] == [dict(path=str(runtime),identity=[runtime.stat().st_dev,runtime.stat().st_ino])]
    assert note.read_bytes() == b"protected sentinel" and not (runtime/"notifications").exists()


def test_actual_authority_guard_selects_all_controller_authority_and_notification_roots(tmp_path):
    controller=tmp_path/"controller";authority=tmp_path/"authority";selected=tmp_path/"selected";stale=tmp_path/"stale"
    for path in (controller,authority,selected,stale):path.mkdir()
    notifier=EnvironmentNotifier(controller,NotificationConfig());notifier.relay_runtime=stale
    guarded=[]
    class Authority:
        runtime=stale
        notification_runtime=stale
        @contextmanager
        def guard(self):
            self.runtime=authority;self.notification_runtime=selected;guarded.append("enter")
            try:yield
            finally:guarded.append("exit")
    notifier.relay_authority=Authority()
    value=project(notifier)
    assert guarded == ["enter","exit"] and value["notification_runtime"] == str(selected)
    assert {row["path"] for row in value["sender_runtimes"]} == set(map(str,(controller,authority,selected)))
    assert all(not (path/"notifications").exists() for path in (controller,authority,selected,stale))


def test_ambiguous_workspace_session_refuses_without_mutation(tmp_path):
    notifier=EnvironmentNotifier(tmp_path,NotificationConfig())
    with pytest.raises(RetentionError,match="workspace_ambiguous"):
        project(notifier,workspaces=[workspace("fixture"),workspace("fixture",OTHER)])
    assert list(tmp_path.iterdir()) == []


def test_http_pin_projection_is_explicit_and_unchanged_and_native_tuples_are_not_reinterpreted(tmp_path):
    notifier=EnvironmentNotifier(tmp_path,NotificationConfig())
    original=ctx(1,born=NOW-1)
    # Use the context's exact fixture scope, not a fabricated native pin.
    from codex_watchdog.models import sha256_text
    original["scope_sha256"]=sha256_text("fixture")
    value=project(notifier,contexts=[original],pins=[(original["fingerprint"],"slack")])
    assert value["projection"]["contexts"] == [original]
    assert value["projection"]["current_pins"] == [[original["fingerprint"],"slack"]]
    with pytest.raises(RetentionError,match="projection_invalid"):
        project(notifier,pins=[("namespace","events","native-event-key")])
    assert list(tmp_path.iterdir()) == []
