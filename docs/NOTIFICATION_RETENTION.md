# Offline notification retention during a controlled upgrade

The upgrade preserves existing configuration, destinations, credentials and
exact Codex conversations. Notification history is disposable; current native
work, provider journals, grants, queues and ownership are outside this operation.
This is an operator/packaging interface, not a pairing or routine user setup step.

`notification-retention-prepare` makes the explicit cold root API available in
the packaged executable. It does not discover, stop, restart or take ownership of
any process. Ordinary observe/poll/Stop/send/startup does not invoke this command.
The supported upgrade controller must positively exclude **all actual old
notification senders and their subprocesses** through the complete invocation.
A cooperative lock or a JSON assertion alone does not establish that exclusion.

## Current projection and actual roots

Before stopping the old monitor, build its current configuration projection with
`notification_cutover.notifier_cutover_projection`. Supply the current verified
catalog's workspaces, current discovery/resume observer identifiers, the actual
sender event types, and explicitly verified current HTTP contexts/pins. This
read-only adapter refreshes the notifier's authority selection under its existing
guard and includes its actual controller, authority and selected notification
runtime roots, with their physical directory identities. It reads no historical
mapping, notification, native or provider journal.

The result has schema_version1, notification_runtime/notification_runtime_id,
sender_runtimes (path/identity), and projection (scopes/current_keys/contexts/
current_pins). Identity is [device,inode]. The request is frozen before cutover;
reconcile its current configuration coverage with the operator's native stop
inventory. Keep the original source birth and destination/payload identity in
each supplied context. HTTP pins are exact [fingerprint,provider] pairs. They
are not provider/native [namespace,kind,key] tuples. Zero live HTTP operations
can be established by complete actual sender/subprocess closure; it cannot be
inferred from a native queue, a missing receipt or an unsupported writer probe.
Running Codex jobs can remain running while WatchDog senders are excluded.

## Native sender receipt and invocation

The upgrade controller retains its actual OS/process/service evidence and writes
a fresh schema_version1 receipt with purpose `notification-root-cutover`:

- `projection_sha256`: SHA256 of sorted, compact UTF-8 JSON of the complete
  request, with ensure_ascii=False and allow_nan=False.
- `sender_runtimes`: the exact request list, including physical identities.
- `observed_at`: the verified census time, as finite nonnegative Unix seconds.
- `native_source`: windows-process-census, linux-process-census or
  darwin-process-census, matching the actual collection.
- `native_evidence_sha256`: hash of the retained actual native evidence.
- `all_notification_senders_stopped` and
  `all_notification_subprocesses_stopped`: true only after that evidence proves
  the closure; `old_sender_restart_allowed`: false.

Do not synthesize a successful native receipt from this field list. The operator
keeps old-process exclusion effective throughout the command. Its receipt must
be no more than30 seconds old when cooperative exclusion is acquired. The command
validates structure, freshness, request binding, root identities and unchanged
input bytes; it does not independently perform an OS census. Its result explicitly
states `caller_native_receipt_not_independent_census`.

Invoke the candidate image with the existing selected notification runtime:

```text
codex-watchdog --runtime /selected/notification/runtime notification-retention-prepare --projection /private/current-projection.json --quiescence /private/native-sender-receipt.json
```

The command acquires foreground and common sender locks across the projected
roots. It refuses redirected or multiply linked lock files before opening them.
The existing cold backend owns its notification lock, publishes finite readiness
and the original cutoff before retiring recognized old history. An invalid or
missing input or held lock returns exit1 with a nonsecret refusal receipt; it
does not start messaging setup or request new configuration/authorization.

## Completion and forward recovery

Verify the exact candidate image, returned plan/floor and native evidence, and
the preservation of current configuration/native/provider state before starting
the candidate sender. A successful source fixture or receipt is not compiled,
live-owner, native or fleet acceptance. The normal release/upgrade gates still
apply.

If interrupted, retain the compact preparation/intent/ready metadata and original
inputs. Re-establish actual old-sender exclusion and supply a fresh bound receipt
for an explicit forward-completion call. Preserve the original plan/floor and
uncertain claims. Do not restart the old executable against compacted state or
restore an old receipt archive as live state. Kept executable backups are not
permission for an active downgrade.

Recognized imported notification copies are optionally retired only after ready.
Original source runtimes and provider-authority rollback backups stay untouched.
Unknown, malformed, linked, busy, changed or undeletable copies remain and are
reported by counts. Such an exclusion does not undo readiness or block later
independent observation/notification. No default historical archive is created.
