# Codex WatchDog development guidelines

These rules are durable project constraints for future implementation work. Treat them as architecture invariants, not optional preferences.

## Core boundaries

- Keep WatchDog lightweight, deterministic, and easy to inspect.
- WatchDog observes, wakes, notifies, and relays; it is not another AI agent.
- WatchDog observes Git; Codex owns Git. WatchDog must not stage, commit, pull, merge, rebase, reset, checkout, or push.
- Preserve ownership of the existing VS Code Codex thread. Do not create a second session merely to simplify integration.
- Prefer the smallest reliable mechanism that solves a real dogfood problem. Avoid speculative orchestration and premature abstraction.
- Preserve fail-closed behavior for ambiguous targeting, delivery, authorization, and remote-state resolution.

## Upgrade and configuration compatibility

**Upgrade must preserve user state by default. Reconfiguration is a failure mode, not a normal upgrade step.**

Every release and packaging change must follow these rules:

1. **Read previous-version state automatically.** A new version must discover and reuse an existing current-user runtime/profile whenever the stored state is compatible.
2. **Version persistent schemas.** Durable configuration/runtime formats that may evolve must carry an explicit schema/version marker or an equivalent unambiguous migration discriminator.
3. **Migrate explicitly and idempotently.** Migrations must be deterministic, safe to run more than once, and must not duplicate, reset, or silently discard user state.
4. **Prefer non-destructive migration.** Do not overwrite or delete the previous usable state until the replacement has been validated. Create an atomic backup/snapshot when a migration changes durable state materially.
5. **Preserve user choices.** Reuse existing Slack, Outlook, Duo/remote, runtime, notification, workspace, and other saved settings whenever their semantics are still supported. Preserve unknown/nonconflicting keys where practical instead of normalizing them away.
6. **Keep secrets in their existing security boundary.** Do not export, decrypt, copy into the release bundle, or downgrade DPAPI/OAuth/credential material merely to perform an upgrade. Reuse the existing current-user secure store in place.
7. **Do not fake reauthorization.** If an external provider or a security boundary genuinely requires fresh consent, login, trust, or Duo interaction, explain exactly what must be repeated and why. Preserve all unrelated configuration.
8. **Rollback must remain possible when practical.** A failed migration must leave the prior version's usable state recoverable. Avoid one-way schema destruction unless there is a documented, necessary reason.
9. **Release acceptance must include upgrade testing.** For every versioned Windows release, test at least the immediately previous public release profile/runtime -> candidate release path. The user must not have to re-enter unchanged configuration.
10. **Fresh install and upgrade are different acceptance paths.** A clean install test does not substitute for migration/upgrade testing.

When a proposed feature or packaging change conflicts with these rules, stop and report the compatibility tradeoff before implementing a breaking migration.

## Release discipline

- `pyproject.toml` is the canonical application version source unless the architecture explicitly changes.
- Do not overwrite an existing release/tag to hide a packaging or migration error; publish a new version.
- Public release artifacts must not contain local runtime state, credentials, private paths, private hostnames, or development-repository history.
- Maintain the private development/control repository separately from the sanitized public product repository.
- Prefer real dogfood evidence and minimal fixes over speculative hardening. Keep "repairing the tire while driving" compatible with the invariants above.
