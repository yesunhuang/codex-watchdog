"""Four current observer-source points, never a history or retry-time oracle.

The caller supplies a fresh verified observation of its current source and
commits the containing workspace state before attempting a notification.
Existing unknown source baselines remain untimestamped until a real change.
"""
import math
import re
import uuid

from .notification_context import source_time

SLOTS = frozenset(("git_attention", "wake_status", "remote_outage", "remote_recovery"))


def observe_notification_source(state, slot, session_id, fingerprint, observed_at, *, allow_initial=False):
    if slot not in SLOTS or type(allow_initial) is not bool:
        raise ValueError("notification_observer_source_invalid")
    try:
        if str(uuid.UUID(session_id)) != session_id:
            raise ValueError()
    except (ValueError, AttributeError, TypeError):
        raise ValueError("notification_observer_session_invalid") from None
    stamp = source_time(observed_at)
    if stamp is None or not isinstance(fingerprint, str) or re.fullmatch("[0-9a-f]{64}", fingerprint) is None:
        raise ValueError("notification_observer_source_invalid")
    points = state.get("notification_births")
    if points is None:
        points = dict(schema_version=1, points={})
    if (not isinstance(points, dict) or type(points.get("schema_version")) is not int
            or points["schema_version"] != 1 or not isinstance(points.get("points"), dict)
            or not set(points["points"]).issubset(SLOTS)):
        raise ValueError("notification_observer_state_invalid")
    old = points["points"].get(slot)
    if old is not None:
        if (not isinstance(old, dict) or old.get("session_id") != session_id
                or not isinstance(old.get("fingerprint"), str)
                or re.fullmatch("[0-9a-f]{64}", old["fingerprint"]) is None
                or type(old.get("generation")) is not int or not 0 <= old["generation"] <= 2**63-2
                or type(old.get("checked_at")) not in (int, float)
                or not math.isfinite(old["checked_at"]) or old["checked_at"] < 0
                or old.get("born") is not None and (type(old["born"]) not in (int, float)
                    or source_time(old["born"]) is None or old["born"] > old["checked_at"])):
            raise ValueError("notification_observer_point_invalid")
        if stamp < old["checked_at"]:
            return None  # Late callback/clock rollback cannot replace current proof.
        if old["fingerprint"] == fingerprint:
            born, generation = old["born"], old["generation"]
        elif stamp == old["checked_at"]:
            return None  # No positive temporal order for a different source.
        else:
            born, generation = stamp, old["generation"]+1
    else:
        born, generation = (stamp, 1) if allow_initial else (None, 0)
    # Copy-on-publish preserves malformed or unrelated evidence on refusal.
    points = dict(points, points=dict(points["points"]))
    points["points"][slot] = dict(session_id=session_id, fingerprint=fingerprint,
        born=born, checked_at=stamp, generation=generation)
    state["notification_births"] = points
    return born
