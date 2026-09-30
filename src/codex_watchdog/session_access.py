"""Provider-independent exact-session access control coordinator.

Wraps SessionACL to give each relay a single injection point
for apply/claim_confirmation/finish_confirmation/make_admission.  The caller
supplies admin_ids; ACL authority for delegates is resolved at admission time
inside the existing journal transaction.
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Tuple


class SessionAccess:
    """Coordinate SessionACL apply/confirmation with provider-agnostic logic."""

    def __init__(
        self,
        thread_store: Any,
        provider: str,
        scope: str,
        *,
        admin_ids: Tuple[str, ...],
        acl: Any = None,
    ) -> None:
        self.thread_store = thread_store
        self.provider = provider
        self.scope = scope
        self.admin_ids: frozenset = frozenset(admin_ids)
        self._acl_override = acl

    @property
    def _acl(self) -> Any:
        if self._acl_override is not None:
            return self._acl_override
        from .session_acl import SessionACL
        return SessionACL(self.thread_store, self.provider, self.scope)

    def apply(
        self,
        *,
        event_key: str,
        message_key: str,
        source_key: str,
        user_id: str,
        operation: str,
        delegate_id: Optional[str],
        payload_sha256: str,
        expected_target: Any,
    ) -> dict:
        return self._acl.apply(
            event_key=event_key,
            message_key=message_key,
            source_key=source_key,
            user_id=user_id,
            operation=operation,
            delegate_id=delegate_id,
            payload_sha256=payload_sha256,
            admin_ids=self.admin_ids,
            expected_target=expected_target,
        )

    def claim_confirmation(self, operation_key: str) -> Optional[dict]:
        return self._acl.claim_confirmation(operation_key)

    def finish_confirmation(self, operation_key: str, status: str) -> None:
        self._acl.finish_confirmation(operation_key, status)

    def make_admission(
        self,
        source_key: str,
        user_id: str,
        expected_target: Any,
        *, event_key: Optional[str] = None, message_key: Optional[str] = None,
    ) -> Callable[[Any], bool]:
        acl = self._acl
        admins = self.admin_ids

        def admission(db: Any) -> bool:
            return acl.admit(db, source_key, user_id, admins, expected_target,
                             event_key=event_key, message_key=message_key)

        return admission

    def preflight(self, source_key: str, user_id: str, expected_target: Any) -> Optional[List[str]]:
        """Validate an admin control and read its current ACL before provider lookup."""
        if user_id not in self.admin_ids:
            return None
        acl = self._acl
        with self.thread_store.journal.transaction() as db:
            if acl.admit(db, source_key, user_id, self.admin_ids, expected_target) is not True:
                return None
            return acl.delegates(db, expected_target.thread_id)


def access_confirmation_text_slack(
    operation: str,
    status: str,
    delegate_id: Optional[str],
    delegates: List[str],
    target: Any,
) -> str:
    """Concise Slack-format confirmation using <@ID> mention syntax."""
    session_label = f"session {target.thread_id[:8]}…"
    if status == "already_admin":
        headline = (
            f"That user (<@{delegate_id}>) is already an admin; "
            "admin authority is independent and cannot be changed here."
        )
    elif status == "added":
        headline = f"Access granted for <@{delegate_id}>."
    elif status == "removed":
        headline = f"Access revoked for <@{delegate_id}>."
    elif status == "already_present":
        headline = f"<@{delegate_id}> is already a delegate (no change)."
    elif status == "not_present":
        headline = f"<@{delegate_id}> is not a delegate (no change)."
    elif status == "access":
        headline = "Session access status:"
    else:
        headline = f"Access operation '{status}' applied."

    if delegates:
        dl = ", ".join(f"<@{d}>" for d in sorted(delegates))
        headline += f" Active delegates: {dl}."
    else:
        headline += " No active delegates."

    return (
        f"{headline} Admins retain independent access. "
        f"Reply in THIS new thread to send messages to {session_label}."
    )


def access_confirmation_text_lark(
    operation: str,
    status: str,
    delegate_id: Optional[str],
    delegates: List[str],
    target: Any,
) -> str:
    session_label = f"session {target.thread_id[:8]}…"
    if status == "already_admin":
        headline = (
            f"That user ({delegate_id}) is already an admin; "
            "admin authority is independent and cannot be changed here."
        )
    elif status == "added":
        headline = f"Access granted for {delegate_id}."
    elif status == "removed":
        headline = f"Access revoked for {delegate_id}."
    elif status == "already_present":
        headline = f"{delegate_id} is already a delegate (no change)."
    elif status == "not_present":
        headline = f"{delegate_id} is not a delegate (no change)."
    elif status == "access":
        headline = "Session access status:"
    else:
        headline = f"Access operation '{status}' applied."

    if delegates:
        dl = ", ".join(sorted(delegates))
        headline += f" Active delegates: {dl}."
    else:
        headline += " No active delegates."

    return (
        f"{headline} Admins retain independent access. "
        f"Reply in THIS new thread to send messages to {session_label}."
    )


def access_confirmation_text_onebot(
    operation: str,
    status: str,
    delegate_id: Optional[str],
    delegates: List[str],
    target: Any,
) -> str:
    session_label = f"session {target.thread_id[:8]}..."
    if status == "already_admin":
        headline = (
            f"That user ({delegate_id}) is already an admin; "
            "admin authority is independent."
        )
    elif status == "added":
        headline = f"Access granted for {delegate_id}."
    elif status == "removed":
        headline = f"Access revoked for {delegate_id}."
    elif status == "already_present":
        headline = f"{delegate_id} is already a delegate (no change)."
    elif status == "not_present":
        headline = f"{delegate_id} is not a delegate (no change)."
    elif status == "access":
        headline = "Session access status:"
    else:
        headline = f"Access operation '{status}' applied."

    if delegates:
        dl = ", ".join(sorted(delegates))
        headline += f" Delegates: {dl}."
    else:
        headline += " No active delegates."

    return (
        f"WatchDog: {headline} Admins retain independent access. "
        f"Reply in THIS new thread to interact with {session_label}."
    )
