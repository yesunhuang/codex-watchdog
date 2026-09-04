# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability-reporting or security-advisory feature
for this repository. Do not post credentials, tokens, private paths, session
identifiers, or exploit details in a public issue.

## Operational security boundary

WatchDog runs with the current user's permissions and deliberately interacts
with existing Codex, Git, VS Code, SSH, Slack, and email configuration. A Slack
reply accepted by the relay becomes an instruction to an already-existing exact
Codex thread; it is not a sandbox or a separate authorization boundary.

Keep these materials outside the repository and outside packaged artifacts:

- Slack webhook, bot, and app tokens;
- SMTP passwords and Outlook OAuth caches;
- DPAPI/CLIXML credential payloads and local channel/member mappings;
- SSH keys, agent state, host-specific configuration, and MFA responses; and
- `.codex-watchdog/`, `.codex/`, queues, rollouts, resume prompts, audit files,
  logs, screenshots, and local debug output.

The default runtime and common local credential formats are gitignored, but
ignore rules are not a substitute for reviewing every commit before pushing.
