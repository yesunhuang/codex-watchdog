"""Small ownership RPCs over the existing bounded SSH helper transport."""

import hashlib
import os
from pathlib import Path
import uuid

from .control_state import ControlError


REMOTE_CONTROL_SOURCE = r'''
_CONTROL_ACTIVE = None


def control_exact_thread(repo, thread):
    rows = read_json_database(
        remote_codex_home() / "state_5.sqlite",
        "SELECT cwd FROM threads WHERE id=? AND archived=0 AND source='vscode' AND thread_source='user'",
        (thread,),
    )
    return len(rows) == 1 and isinstance(rows[0][0], str) and os.path.normpath(rows[0][0]) == os.path.normpath(repo)


def control_writer_kind(store):
    pid = control_kernel_owner(remote_codex_home() / "thread-writer-locks" / (store.thread_id + ".lock"))
    if pid is None:
        return "absent"
    if control_vscode_writer(pid):
        return "vscode"
    if store.path.exists():
        value = store.read()
        if (value["owner"] is not None and value["owner"]["role"] == "remote"
                and value.get("writer_pid") == pid):
            return "remote"
    return "unknown"


def control_resolve(repo, storage_key, expected):
    loaded = set(expected) if expected is not None else window_sessions(storage_key)[0]
    if not loaded:
        raise ControlError("remote_thread_unresolved")
    candidates = {}
    for thread in loaded:
        if not control_exact_thread(repo, thread):
            continue
        owner, view_active = log_session_state(thread)
        store = ControlStore(remote_codex_home(), thread, repo)
        if owner or (store.path.exists() and control_writer_kind(store) == "remote"):
            candidates[thread] = view_active
    active = [thread for thread, view in candidates.items() if view is True]
    if len(active) == 1 and all(view is not None for view in candidates.values()):
        return active[0]
    if len(active) > 1:
        raise ControlError("remote_thread_ambiguous")
    thread, issue = resolve_session(repo, storage_key, expected)
    if thread is None:
        raise ControlError(issue or "remote_thread_unresolved")
    return thread


def control_runtime(store):
    binding = remote_codex_home() / "watchdog-linux" / (store.thread_id + ".json")
    if not binding.exists():
        return str(store.directory / "runtime")
    previous = control_read_json(binding)
    if previous.get("thread_id") != store.thread_id:
        raise ControlError("control_binding_mismatch")
    candidates = [previous.get("runtime_path")]
    agent = remote_codex_home() / "watchdog-control" / "agent.json"
    if agent.exists():
        candidates.append(control_read_json(agent).get("runtime_path"))
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))).expanduser()
    profile_root = Path(os.environ.get("CODEX_WATCHDOG_LINUX_CONFIG_DIR", str(data / "codex-watchdog"))).expanduser()
    if not profile_root.is_absolute():
        raise ControlError("control_previous_runtime_unresolved")
    profile = profile_root / "linux-launcher.json"
    if profile.exists():
        candidates.append(control_read_json(profile).get("runtime"))
    for runtime in candidates:
        if isinstance(runtime, str) and Path(runtime).is_absolute():
            runtime = str(Path(runtime).resolve())
            if hashlib.sha256(runtime.encode("utf-8")).hexdigest() == previous.get("runtime_sha256"):
                return runtime
    raise ControlError("control_previous_runtime_unresolved")


def run_control(request):
    global _CONTROL_ACTIVE
    control = request["control"]
    action = control.get("action")
    token = control.get("token")
    repo = request["repo_path"]
    if action == "acquire":
        thread = control_resolve(repo, request["storage_key"], request.get("expected_session_ids"))
    else:
        thread = canonical_uuid(control.get("thread_id"))
        if thread is None or not control_exact_thread(repo, thread):
            raise ControlError("control_exact_thread_unavailable")
    store = ControlStore(remote_codex_home(), thread, repo)
    if action == "relay":
        if not store.path.exists():
            return {"status": "ok", "legacy": True}
        value = store.read()
        if value["owner"] is None:
            raise ControlError("control_handoff_in_progress")
        token = store._token(value)
        with store.guard(token, purpose="queue"):
            _CONTROL_ACTIVE = (store, token)
            try:
                receipt = store._once_locked(
                    token, "relay", request["instruction_id"], sha(request["prompt"]),
                    lambda: dispatch_wake(request, thread),
                )
            finally:
                _CONTROL_ACTIVE = None
        return {"status": "ok", "control": {"token": token}, "duplicate": receipt["duplicate"],
                "wake": receipt.get("result", {"state": "uncertain"})}
    if action == "acquire":
        runtime = control_runtime(store)
        token = store.attach(control.get("instance"), control.get("locality"),
                             control_writer_kind(store), ttl=control.get("ttl", 900))
        if token is not None:
            with store.guard(token) as value:
                if "runtime_path" not in value:
                    value["runtime_path"] = runtime
                if value["runtime_path"] != runtime:
                    raise ControlError("control_runtime_mismatch")
                target = value.get("remote_target")
                if target is None:
                    value["remote_target"] = {
                        "authority": control["authority"], "repo_path": repo,
                        "storage_key": request["storage_key"],
                    }
                if "remote_state" not in value:
                    initial = control.get("initial_state")
                    if (isinstance(initial, dict) and initial.get("session_id") in (None, thread)
                            and initial.get("repo_path") == repo):
                        value["remote_state"] = dict(initial, session_id=thread)
                    else:
                        value["remote_state"] = None
                store.merge_slack_mappings(value, [entry for entry in control.get("relay_mappings", ())
                                                   if isinstance(entry, dict) and entry.get("thread_id") == thread])
                control_atomic_json(store.path, value)
    elif action == "detach":
        store.detach(token)
    elif action == "save":
        state = control.get("state")
        if (not isinstance(state, dict) or state.get("session_id") != thread
                or state.get("repo_path") != repo or len(json.dumps(state)) > 65536):
            raise ControlError("control_state_invalid")
        with store.guard(token) as value:
            value["remote_state"] = state
            control_atomic_json(store.path, value)
    elif action == "notification_prepare":
        receipt = store.prepare_notification(token, control["event_id"], control["fingerprint"])
        return {"status": "ok", "receipt": receipt}
    elif action == "notification_finish":
        receipt = store.finish_notification(token, control["event_id"], control["operation_id"], control["result"],
                                             control.get("relay_mappings", ()))
        return {"status": "ok", "receipt": receipt}
    elif action not in ("probe", "observe"):
        raise ControlError("control_action_invalid")
    value = store.read()
    result = {"status": "ok", "session_id": thread, "repo_path": repo,
              "control": {"token": token, "owner_state": value["state"], "epoch": value["epoch"],
                          "state": value.get("remote_state"), "target": value.get("remote_target"),
                          "runtime_path": value.get("runtime_path"),
                          "relay_mappings": store.slack_mappings(),
                          "external_effect_pending": value["external_effect"] is not None}}
    if action in ("save", "detach", "observe") or token is None:
        return result
    # Pure Git/rollout reads may overlap another observer. Queue journal writes
    # and courier invocation remain inside the authoritative epoch lock.
    result.update(git=git_observation(repo), completion=rollout_completion(thread))
    with store.guard(token, purpose="queue" if request.get("action") == "wake" else "state"):
        _CONTROL_ACTIVE = (store, token)
        try:
            pending = request.get("pending_instruction_id")
            if action == "acquire" and isinstance(value.get("remote_state"), dict):
                pending = value["remote_state"].get("pending_instruction_id")
            if isinstance(pending, str):
                path = wake_record_path(pending)
                if path.is_file():
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if record.get("thread_id") != thread:
                        raise ControlError("control_wake_thread_mismatch")
                    result["wake"] = observe_wake(record)
            if request.get("action") == "wake":
                result["wake"] = dispatch_wake(request, thread)
        finally:
            _CONTROL_ACTIVE = None
    return result
'''


class RemoteControlClient:
    def __init__(self, adapter, runtime, *, instance=None, locality=None):
        self.adapter = adapter
        self.instance = instance or str(uuid.uuid4())
        self.locality = locality or hashlib.sha256(
            (os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "desktop"))
             + "\0" + str(Path(runtime).resolve())).encode("utf-8")
        ).hexdigest()

    def request(self, target, action, *, token=None, **fields):
        control = dict(action=action, instance=self.instance, locality=self.locality, **fields)
        if token is not None:
            control.update(token=token, thread_id=token["thread_id"])
        result = self.adapter.probe(target, control=control)
        if result.get("status") != "ok":
            raise ControlError(result.get("reason", "control_transport_unavailable"))
        return result

    def acquire(self, target, state, ttl=900, relay_mappings=()):
        return self.request(target, "acquire", authority=target.authority, initial_state=state, ttl=ttl,
                            relay_mappings=relay_mappings)

    def save(self, target, token, state):
        return self.request(target, "save", token=token, state=state)

    def probe(self, target, token, **options):
        result = self.adapter.probe(target, control=dict(action="probe", token=token,
                                                         thread_id=token["thread_id"]), **options)
        if result.get("status") != "ok":
            raise ControlError(result.get("reason", "control_transport_unavailable"))
        return result

    def notify(self, target, token, event, notifier):
        fingerprint = event.event_fingerprint()
        prepared = self.request(target, "notification_prepare", token=token,
                                event_id=fingerprint, fingerprint=fingerprint)["receipt"]
        if prepared["duplicate"]:
            result = prepared.get("result")
            if isinstance(result, dict):
                return dict(result, status="suppressed", duplicate=True)
            raise ControlError("control_notification_outcome_uncertain")
        result = notifier.notify(event).to_dict()
        mappings = getattr(notifier, "slack_thread_store", None)
        mappings = mappings.notification_mappings(fingerprint) if mappings is not None else ()
        self.request(target, "notification_finish", token=token, event_id=fingerprint,
                     operation_id=prepared["operation_id"], result=result, relay_mappings=mappings)
        return result
