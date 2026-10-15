"""Explicit offline notification-root entry for a controlled package upgrade.

The operator has already stopped and excluded ALL old notification senders and
their subprocesses, and retains that exclusion throughout this call. A fresh
native receipt records that fact; validating its identity is not an independent
OS census. The acquired foreground/common locks additionally exclude supported
cooperating senders. This command never stops or starts a process, discovers an
owner, infers native idle, reads credentials or handles provider/native journals.
"""
from contextlib import ExitStack, contextmanager, nullcontext
import hashlib
import json
import math
from pathlib import Path
import stat
import time

from .notification_receipt_retention import PROVIDERS, RetentionError, _digest, _safe, _scope, _sha, _subject, _time, context
from .notification_root_retention import prepare_notification_root
from .notification_senders import sender_fence
from .storage import FileLock

MAX_INPUT = 2 * 1024 * 1024
PROOF_SECONDS = 30


def notifier_cutover_projection(notifier, *, workspaces, observer_workspace_ids,
                               event_types, current_contexts, current_http_pins):
    """Read-only O(current configuration) adapter, before operator shutdown.

    Workspaces come from the caller's current configured/verified catalog, not
    historical mapping files or receipt enumeration. The caller explicitly
    supplies the current HTTP-effect projection; native/provider tuple pins are
    not reinterpreted as HTTP claims. This does not prove native or sender idle.
    Authority selection is refreshed under its existing guard before choosing
    the actual receipt runtime, which may differ from the authority/controller.
    """
    authority = getattr(notifier, "relay_authority", None)
    with authority.guard() if authority is not None else nullcontext():
        runtime = Path(authority.notification_runtime if authority is not None
                       else getattr(notifier, "relay_runtime", notifier.runtime)).absolute()
        paths = {Path(notifier.runtime).absolute(), runtime}
        if authority is not None:paths.add(Path(authority.runtime).absolute())
        def resource(path):
            _safe(path)
            value = path.stat()
            if not path.is_dir() or not value.st_ino:
                raise RetentionError("notification_cutover_runtime_invalid")
            return [value.st_dev, value.st_ino]
        roots = [dict(path=str(path), identity=resource(path)) for path in sorted(paths,key=str)]
        if len({tuple(row["identity"]) for row in roots}) != len(roots):
            raise RetentionError("notification_cutover_sender_roots_aliased")
        providers = PROVIDERS["dual"] + PROVIDERS["fallback"]
        scopes, names, identities = set(), set(), {}
        from .notification_context import observer_scope
        for workspace in workspaces:
            name, session = workspace.workspace_id, workspace.session_id
            _, scope = observer_scope(name)  # Validate the actual workspace name.
            _scope(session, scope)
            if session.startswith("observer:") or (name in identities and identities[name] != session):
                raise RetentionError("notification_cutover_workspace_ambiguous")
            identities[name] = session;names.add(name)
            scopes.update((session,scope,p) for p in providers)
        for name in observer_workspace_ids:
            subject, scope = observer_scope(name);names.add(name)
            scopes.update((subject,scope,p) for p in providers)
        events = list(event_types)
        if not events or any(not isinstance(value,str) or not value or len(value)>128 for value in events):
            raise RetentionError("notification_cutover_event_types_invalid")
        events = set(events)
        # Same canonical formula as NotificationEvent.dedupe_key, without
        # loading notification history or inventing an event's original birth.
        keys = sorted(hashlib.sha256(json.dumps(dict(workspace_id=name,event_type=kind),
            sort_keys=True,separators=(",", ":")).encode()).hexdigest() for name in names for kind in events)
        request = dict(schema_version=1, notification_runtime=str(runtime),
            notification_runtime_id=resource(runtime), sender_runtimes=roots,
            projection=dict(scopes=[list(row) for row in sorted(scopes)],current_keys=keys,
                            contexts=list(current_contexts),current_pins=[list(row) for row in current_http_pins]))
        _projection(runtime,request)
        return request


def _read(path):
    path = _safe(Path(path).absolute())
    if not path.is_file() or path.stat().st_nlink != 1:
        raise RetentionError("notification_cutover_input_not_regular")
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT + 1)
    if len(raw) > MAX_INPUT:
        raise RetentionError("notification_cutover_input_oversized")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RetentionError("notification_cutover_input_invalid")
            result[key] = value
        return result
    def number(raw):
        value = float(raw)
        if not math.isfinite(value):raise ValueError()
        return value
    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_float=number,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise RetentionError("notification_cutover_input_invalid") from None
    if not isinstance(value, dict):
        raise RetentionError("notification_cutover_input_invalid")
    return value, hashlib.sha256(raw).hexdigest()


def _runtime(path, expected):
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise RetentionError("notification_cutover_runtime_invalid")
    root = _safe(Path(path))
    if (not root.is_dir() or not isinstance(expected, list) or len(expected) != 2
            or any(type(v) is not int or v < 0 for v in expected)):
        raise RetentionError("notification_cutover_runtime_invalid")
    actual = root.stat()
    if not actual.st_ino or [actual.st_dev, actual.st_ino] != expected:
        raise RetentionError("notification_cutover_runtime_changed")
    # Verify the complete lock path before FileLock can create/open anything.
    # A redirected locks directory or hardlinked foreground file is not an
    # exclusion fence for the selected runtime and must never be written.
    _safe(root / "locks")
    for name in ("foreground-run.lock", "notification-senders.lock", "notifications.lock"):
        path = _safe(root / "locks" / name)
        if path.exists():
            value = path.stat()
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise RetentionError("notification_cutover_lock_invalid")
    return root


def _projection(runtime, request):
    if (type(request.get("schema_version")) is not int or request["schema_version"] != 1
            or request.get("notification_runtime") != str(runtime)
            or not isinstance(request.get("sender_runtimes"), list)
            or not request["sender_runtimes"]):
        raise RetentionError("notification_cutover_projection_invalid")
    _runtime(str(runtime), request.get("notification_runtime_id"))
    roots = []
    for item in request["sender_runtimes"]:
        if not isinstance(item, dict):raise RetentionError("notification_cutover_projection_invalid")
        roots.append(_runtime(item.get("path"), item.get("identity")))
    if len(set(roots)) != len(roots) or runtime not in roots:
        raise RetentionError("notification_cutover_sender_roots_invalid")
    selected = request.get("projection")
    if (not isinstance(selected, dict) or set(selected) != {"scopes", "current_keys", "contexts", "current_pins"}
            or any(not isinstance(v, list) for v in selected.values()) or not selected["scopes"]):
        raise RetentionError("notification_cutover_projection_invalid")
    scopes = set()
    for row in selected["scopes"]:
        if not isinstance(row, list) or len(row) != 3:
            raise RetentionError("notification_cutover_projection_invalid")
        _scope(*row[:2])
        if row[2] not in PROVIDERS["dual"] + PROVIDERS["fallback"]:
            raise RetentionError("notification_cutover_projection_invalid")
        scopes.add(tuple(row))
    if any(not _digest(key) for key in selected["current_keys"]):
        raise RetentionError("notification_cutover_projection_invalid")
    seen = set()
    for raw in selected["contexts"]:
        ctx = context(raw)
        if ctx["fingerprint"] in seen or any((_subject(ctx),ctx["scope_sha256"],provider) not in scopes for provider in ctx["providers"]):
            raise RetentionError("notification_cutover_projection_invalid")
        seen.add(ctx["fingerprint"])
    for row in selected["current_pins"]:
        if (not isinstance(row, list) or len(row) != 2 or not _digest(row[0])
                or row[1] not in PROVIDERS["dual"] + PROVIDERS["fallback"]):
            raise RetentionError("notification_cutover_projection_invalid")
    return selected, sorted(roots, key=str)


def _validate_proof(request, proof, clock):
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or proof.get("purpose") != "notification-root-cutover"
            or proof.get("projection_sha256") != _sha(request)
            or proof.get("sender_runtimes") != request["sender_runtimes"]
            or proof.get("all_notification_senders_stopped") is not True
            or proof.get("all_notification_subprocesses_stopped") is not True
            or proof.get("old_sender_restart_allowed") is not False
            or proof.get("native_source") not in ("windows-process-census", "linux-process-census", "darwin-process-census")
            or not _digest(proof.get("native_evidence_sha256"))
            or not 0 <= _time(clock()) - _time(proof.get("observed_at")) <= PROOF_SECONDS):
        raise RetentionError("notification_cutover_old_sender_receipt_invalid")


def prepare_notification_cutover(runtime, projection_path, quiescence_path, *, clock=time.time):
    """Use the caller's exact current projection and fresh native sender receipt.

    Empty HTTP current_pins is valid only if the operator's actual all-sender
    closure proves no live HTTP operation. It is never inferred from a native
    queue, absent receipt, historical uncertainty or an unsupported writer probe.
    All sender runtime roots (controller, authority and selected notification
    root where different) must be included. No ordinary launch calls this API.
    """
    runtime = _safe(Path(runtime).absolute())
    request, request_digest = _read(projection_path)
    selected, roots = _projection(runtime, request)
    proof, proof_digest = _read(quiescence_path)
    _validate_proof(request,proof,clock)

    @contextmanager
    def excluded():
        # Cooperative exclusion is additional to, not a substitute for, the
        # operator's positive native old-process/subprocess closure.
        with ExitStack() as locks:
            for path in roots:
                locks.enter_context(FileLock(path / "locks/foreground-run.lock"))
            for path in roots:
                locks.enter_context(sender_fence(path))
            _projection(runtime, request)
            _validate_proof(request,proof,clock)
            if (_read(projection_path)[1], _read(quiescence_path)[1]) != (request_digest, proof_digest):
                raise RetentionError("notification_cutover_inputs_changed")
            yield
            _projection(runtime, request)
            if (_read(projection_path)[1], _read(quiescence_path)[1]) != (request_digest, proof_digest):
                raise RetentionError("notification_cutover_inputs_changed")

    result = prepare_notification_root(runtime, **selected, sender_fence=excluded, clock=clock)
    return dict(result, projection_sha256=_sha(request), native_receipt_sha256=proof_digest,
                native_receipt_source=proof["native_source"], proof_scope="caller_native_receipt_not_independent_census")
