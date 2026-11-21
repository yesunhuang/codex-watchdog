# Codex WatchDog 2.3.12

Fix automatic discovery of newly created VS Code Codex conversations when the
extension emits fresh-creation metadata instead of a resume-success event.
Current-generation active history attachment and matching typed creation
evidence remain separate from ownership. Selection still requires one exact
live window and its independently verified same-user held native writer.

Existing resume and legacy owner/follower paths are preserved. Bare activity,
ambiguous or malformed identities, follower evidence, stale generations and
released or different writers remain rejected. Saved settings, secure stores,
routes, grants, queues and delivery reservations are preserved without replay.

All affected Windows x64, Linux x64/ARM64 and macOS ARM64 preview packages are
built from the same canonical source. Windows upgrade acceptance uses previous
public version 2.3.11. Existing icon, privacy, package and native gates remain.
