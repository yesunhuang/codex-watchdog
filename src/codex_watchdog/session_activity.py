"""Bounded per-session activity lifecycle and dormant Hello claims.

Each canonical session UUID owns exactly one compact JSON snapshot at
``<runtime>/lifecycle/<uuid>.json``.  Every state change happens under
``<runtime>/locks/lifecycle-<uuid>.lock`` and is published with the shared
atomic JSON writer.  Only point reads are performed: no directory scans,
history logs, global caps, or native session access.

Activity timestamps given to :meth:`SessionActivity.observe` are verified by
the caller.  Checks, Hello claims, and busy flags never refresh
``last_activity_at``; only :meth:`SessionActivity.observe` transitions state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

from .storage import FileLock, InstructionStore

__all__ = [
    "ACTIVE",
    "DORMANT",
    "HELLO_PROVIDERS",
    "MAX_SNAPSHOT_BYTES",
    "SCHEMA_VERSION",
    "TTL_SECONDS",
    "SessionActivity",
    "canonical_session_id",
    "hello_claim_key",
]

SCHEMA_VERSION = 1
TTL_SECONDS = 72 * 60 * 60
MAX_SNAPSHOT_BYTES = 65536
ACTIVE = "ACTIVE"
DORMANT = "DORMANT"
HELLO_PROVIDERS = frozenset({"slack", "lark", "onebot"})

# Leave headroom for the shared writer's formatting so every published
# snapshot still fits the bounded read.
_MAX_COMPACT_BYTES = MAX_SNAPSHOT_BYTES // 2
_HEX64 = re.compile("[0-9a-f]{64}")

Record = Dict[str, Any]


def canonical_session_id(value: object) -> str:
    """Return ``value`` if it is an exact canonical lowercase UUID string."""
    if isinstance(value, str):
        try:
            if str(uuid.UUID(value)) == value:
                return value
        except ValueError:
            pass
    raise ValueError("session id must be a canonical UUID string")


def hello_claim_key(provider: str, scope: str) -> str:
    """Stable Hello claim key for a provider scope, independent of the route."""
    return hashlib.sha256((provider + "\0" + scope).encode("utf-8")).hexdigest()


def _timestamp(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be epoch seconds")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"{name} must be finite epoch seconds") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite nonnegative epoch seconds")
    return number


def _optional_flag(value: object, name: str) -> Optional[bool]:
    if value is None or isinstance(value, bool):
        return value
    raise ValueError(f"{name} must be True, False, or None (unknown)")


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _validate(raw: object, session_id: str) -> Record:
    if not isinstance(raw, dict):
        raise ValueError("lifecycle snapshot must be a JSON object")
    version = raw.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ValueError("unsupported lifecycle snapshot schema")
    if raw.get("session_id") != session_id:
        raise ValueError("lifecycle snapshot belongs to another session")
    status = raw.get("status")
    if status not in (ACTIVE, DORMANT):
        raise ValueError("invalid lifecycle status")
    generation = raw.get("generation")
    if type(generation) is not int or generation < 0:
        raise ValueError("invalid lifecycle generation")
    eligible = raw.get("hello_eligible")
    if type(eligible) is not bool:
        raise ValueError("invalid hello_eligible flag")
    checked_at = _timestamp(raw.get("checked_at"), "checked_at")
    last = raw.get("last_activity_at")
    if last is not None and _timestamp(last, "last_activity_at") > checked_at:
        raise ValueError("last_activity_at is after checked_at")
    if status == DORMANT and last is None:
        raise ValueError("dormant snapshot lacks known activity")
    if eligible and (status != ACTIVE or generation < 1):
        raise ValueError("hello eligibility requires an active generation")
    claims = raw.get("hello_claims")
    if not isinstance(claims, dict):
        raise ValueError("invalid hello_claims")
    for key, claim in claims.items():
        if not _is_hex64(key) or not isinstance(claim, dict):
            raise ValueError("invalid hello claim")
        claim_generation = claim.get("generation")
        if type(claim_generation) is not int or not 1 <= claim_generation <= generation:
            raise ValueError("invalid hello claim generation")
        if not _is_hex64(claim.get("route_fingerprint")):
            raise ValueError("invalid hello claim route fingerprint")
        _timestamp(claim.get("claimed_at"), "claimed_at")
    return raw


def _is_placeholder(record: Record) -> bool:
    """ACTIVE generation 0 with no known activity: nothing has been seeded yet."""
    return (
        record["status"] == ACTIVE
        and record["generation"] == 0
        and record["last_activity_at"] is None
    )


def _discover(
    record: Record, latest: Optional[float], now: float, idle: bool, legacy: bool
) -> Tuple[bool, bool]:
    """Seed the first known lifecycle state without manufacturing activity."""
    record["last_activity_at"] = latest
    record["hello_eligible"] = False
    record["hello_claims"] = {}
    if latest is not None and now - latest < TTL_SECONDS:
        record["status"] = ACTIVE
        record["generation"] = 1
        record["hello_eligible"] = not legacy
    elif latest is not None and idle:
        record["status"] = DORMANT
        record["generation"] = 0
    else:
        # Unknown or busy sessions stay monitored; a migrated legacy session
        # is seeded as an existing generation that never gets Hello.
        record["status"] = ACTIVE
        record["generation"] = 1 if legacy else 0
    return record["hello_eligible"], record["status"] == DORMANT


def _lifecycle_view(record: Record) -> Tuple[Any, ...]:
    return (
        record["status"],
        record["generation"],
        record["hello_eligible"],
        record["last_activity_at"],
        json.dumps(record["hello_claims"], sort_keys=True),
    )


def _result(record: Record, *, changed: bool, activated: bool, dormant: bool) -> Dict[str, Any]:
    return {
        "session_id": record["session_id"],
        "status": record["status"],
        "generation": record["generation"],
        "hello_eligible": record["hello_eligible"],
        "last_activity_at": record["last_activity_at"],
        "checked_at": record["checked_at"],
        "hello_claims": copy.deepcopy(record["hello_claims"]),
        "changed": changed,
        "activated": activated,
        "dormant": dormant,
    }


class SessionActivity:
    """Per-session ACTIVE/DORMANT lifecycle with one Hello claim per generation."""

    def __init__(
        self,
        runtime: Union[str, Path],
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self._runtime = Path(runtime).expanduser().resolve()
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc).timestamp()

    def observe(
        self,
        session_id: str,
        *,
        genuine_at: Optional[float] = None,
        enrollment_at: Optional[float] = None,
        turn_active: Optional[bool] = None,
        queue_active: Optional[bool] = None,
        legacy: bool = False,
    ) -> Dict[str, Any]:
        """Record one check and apply the only permitted lifecycle transitions.

        ``genuine_at`` and ``enrollment_at`` must be independently verified
        activity and authorized enrollment times.  ``None`` busy flags mean
        unknown: they keep the session monitored but never refresh activity
        or allow dormancy.  ``legacy`` only suppresses Hello when a session is
        first seeded.  In the result ``activated`` means a new Hello-eligible
        generation began and ``dormant`` means this check made it dormant.
        """
        sid = canonical_session_id(session_id)
        evidence = [
            _timestamp(value, name)
            for name, value in (("genuine_at", genuine_at), ("enrollment_at", enrollment_at))
            if value is not None
        ]
        turn = _optional_flag(turn_active, "turn_active")
        queue = _optional_flag(queue_active, "queue_active")
        if not isinstance(legacy, bool):
            raise ValueError("legacy must be a bool")
        idle = turn is False and queue is False
        with self._lock(sid):
            prior = self._read(sid)
            now = self._now()
            if prior is not None:
                # Clock rollback must never make expired activity look fresh.
                now = max(now, float(prior["checked_at"]))
            if any(value > now for value in evidence):
                raise ValueError("activity timestamp is in the future")
            latest = max(evidence) if evidence else None
            record: Record = dict(prior) if prior is not None else {}
            record["schema_version"] = SCHEMA_VERSION
            record["session_id"] = sid
            record["checked_at"] = now
            activated = dormant = False
            if prior is None or _is_placeholder(prior):
                activated, dormant = _discover(record, latest, now, idle, legacy)
            elif record["status"] == ACTIVE:
                last = record["last_activity_at"]
                if latest is not None and (last is None or latest > last):
                    record["last_activity_at"] = last = latest
                if idle and last is not None and now - last >= TTL_SECONDS:
                    record["status"] = DORMANT
                    record["hello_eligible"] = False
                    dormant = True
            else:
                last = record["last_activity_at"]
                if latest is not None and latest > last:
                    record["last_activity_at"] = latest
                    if now - latest < TTL_SECONDS:
                        record["status"] = ACTIVE
                        record["generation"] += 1
                        record["hello_eligible"] = True
                        record["hello_claims"] = {}
                        activated = True
            changed = prior is None or _lifecycle_view(prior) != _lifecycle_view(record)
            if record != prior:
                self._publish(sid, record)
        return _result(record, changed=changed, activated=activated, dormant=dormant)

    def claim_hello(
        self, session_id: str, provider: str, scope: str, route_fingerprint: str
    ) -> bool:
        """Claim the current generation's Hello for one provider scope.

        The caller has already validated the exact route and ACL.  The claim
        is persisted before ``True`` is returned and is never retried within
        the same generation, even if delivery fails or the process crashes.
        """
        sid = canonical_session_id(session_id)
        if not isinstance(provider, str) or provider not in HELLO_PROVIDERS:
            raise ValueError("unsupported Hello provider")
        if not isinstance(scope, str) or not scope:
            raise ValueError("Hello scope must be a nonempty string")
        if not _is_hex64(route_fingerprint):
            raise ValueError("route fingerprint must be 64 lowercase hex characters")
        key = hello_claim_key(provider, scope)
        with self._lock(sid):
            record = self._read(sid)
            if (
                record is None
                or record["status"] != ACTIVE
                or not record["hello_eligible"]
                or record["generation"] < 1
            ):
                return False
            generation = record["generation"]
            claims = record["hello_claims"]
            existing = claims.get(key)
            if existing is not None and existing["generation"] == generation:
                return False
            claims[key] = {
                "generation": generation,
                "route_fingerprint": route_fingerprint,
                "claimed_at": max(self._now(), float(record["checked_at"])),
            }
            self._publish(sid, record)
        return True

    def is_active(self, session_id: str) -> bool:
        """Missing state is unknown and therefore active; never mutates."""
        record = self._read(canonical_session_id(session_id))
        return record is None or record["status"] == ACTIVE

    def snapshot(self, session_id: str) -> Optional[Record]:
        """Return the validated snapshot, including unknown keys, or ``None``."""
        return self._read(canonical_session_id(session_id))

    def _state_path(self, session_id: str) -> Path:
        directory = self._runtime / "lifecycle"
        path = directory / f"{session_id}.json"
        if directory.resolve() != directory or path.is_symlink():
            raise ValueError("lifecycle snapshot path is redirected")
        return path

    def _lock(self, session_id: str) -> FileLock:
        path = self._runtime / "locks" / f"lifecycle-{session_id}.lock"
        if path.parent.resolve() != path.parent or path.is_symlink():
            raise ValueError("lifecycle lock path is redirected")
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(path)

    def _now(self) -> float:
        return _timestamp(self._clock(), "clock")

    def _read(self, session_id: str) -> Optional[Record]:
        path = self._state_path(session_id)
        if path.is_dir():
            raise ValueError("lifecycle snapshot path is not a file")
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_SNAPSHOT_BYTES + 1)
        except FileNotFoundError:
            return None
        if len(data) > MAX_SNAPSHOT_BYTES:
            raise ValueError("lifecycle snapshot exceeds its bounded size")
        try:
            raw = json.loads(data.decode("utf-8"))
        except (ValueError, RecursionError):
            raise ValueError("lifecycle snapshot is not valid JSON") from None
        return _validate(raw, session_id)

    def _publish(self, session_id: str, record: Record) -> None:
        encoded = json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(encoded) > _MAX_COMPACT_BYTES:
            raise ValueError("lifecycle snapshot would exceed its bounded size")
        InstructionStore._atomic_json(self._state_path(session_id), record)
