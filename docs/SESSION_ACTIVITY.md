# Session activity and reply expiry

Reply tickets have an absolute lifetime of72 hours from their original creation.
WatchDog keeps at most the last four still-valid tickets for each provider and
exact Codex session. Polling, a lease renewal, a restart or reading a notification
does not extend that lifetime. Closed, expired and evicted parents are not polled
or reopened. Messages remain on the provider; their presence does not authorize
new commands. Send new work under a current reply-enabled notification.

An eligible tracked session becomes dormant after72 hours without independently
verified genuine user or Codex activity. A current native model turn or admitted
queue work requiring observation retains monitoring. Dormancy does not terminate
a conversation, release its writer, delete its queue or remove saved routes and
grants. Unknown native evidence is not proof of idle or fresh activity.

On first verified enrollment, or genuine reactivation of the same dormant UUID,
WatchDog durably claims an activation generation before one Hello attempt on each
configured supported route. It creates a fresh reply parent without waking or
replacing Codex. Repeated discovery and restarts do not create another Hello in
the same generation. A failed or uncertain Hello is not automatically retried and
does not prevent later independent observation. An unbound session has no invented
destination or permission.

WatchDog's own notifications, Hello, acknowledgements, retries, cursor updates and
leases do not refresh genuine activity. A stable editor writer alone is not
activity. Native detection uses current supported evidence; where that evidence
is unavailable, use the existing supported human session/re-enable workflow.

Slack polling retains the ordinary ten-second interval, one parent per tick and
provider backoff. This is best-effort delivery, not an immediate-response SLA.
Exact sender grants, one-shot tickets, UUID deduplication and native FIFO remain
required. A queued acknowledgement establishes acceptance, not native execution
or model completion. Bot commands use the documented plain-text instruction
envelope; generated rich-text link blocks are not supported by that parser.

Current snapshots and notification claim cleanup do not delete Codex history or
provider messages. A controlled notification-store transition and its forward
recovery are documented in [retention and upgrades](NOTIFICATION_RETENTION.md).
Other-window coverage and Linux stale-boot recovery limitations remain as
documented in [platform support](PLATFORM_SUPPORT.md).
