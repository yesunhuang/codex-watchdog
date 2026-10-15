"""Original-source notification identity, without provider calls or discovery.

Birth is supplied by the verified producer. A retry's clock, ticket creation or
file modification time cannot stand in for that source evidence.
"""
from datetime import datetime, timezone
import json
import math
import uuid

from .models import sha256_text

OBSERVER_PREFIX = "observer:"


def observer_scope(workspace_id):
    """A disjoint local receipt subject, never a native thread or grant."""
    if not isinstance(workspace_id, str) or not workspace_id or len(workspace_id) > 4096:
        raise ValueError("notification_observer_scope_invalid")
    scope = sha256_text(workspace_id)
    return OBSERVER_PREFIX + scope, scope


def source_time(value):
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) and value >= 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return source_time(parsed.astimezone(timezone.utc).timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def identity_hash(value):
    return sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False))


def receipt_context(event, identities):
    """Missing original-source evidence leaves finite sending ineligible."""
    born = source_time(event.source_born)
    if getattr(event, "source_observer", False):
        if (born is None or not identities or event.relay_target is not None
                or event.source_thread_id is not None):
            return None
        _, scope = observer_scope(event.workspace_id)
        return dict(schema_version=2, subject_kind="observer", observer_scope_sha256=scope,
                    scope_sha256=scope, fingerprint=event.event_fingerprint(), born=born,
                    providers=identities)
    thread = event.relay_target.thread_id if event.relay_target is not None else event.source_thread_id
    try:
        canonical = str(uuid.UUID(thread))
    except (ValueError, TypeError, AttributeError):
        return None
    if born is None or thread.lower() != canonical or not identities:
        return None
    return dict(schema_version=1, thread_id=canonical,
                scope_sha256=sha256_text(event.workspace_id),
                fingerprint=event.event_fingerprint(), born=born,
                providers=identities)


def verify_identity(expected, target_hash, payload_hash):
    if expected is not None and expected != dict(target_sha256=target_hash,
                                                 payload_sha256=payload_hash):
        raise ValueError("notification_current_destination_or_payload_changed")
