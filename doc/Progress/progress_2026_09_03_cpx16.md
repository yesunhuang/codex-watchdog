# Codex WatchDog progress report — Checkpoint 16

Date: 2026-09-03
Scope: public-release preflight audit of the tracked tree and complete reachable
Git history; no publication, history rewrite, credential rotation, packaging,
or product redesign.

## 1. Executive assessment

**NO-GO for making the repository public as it currently exists.**

No active credential or secret/security blocker was detected. The no-go is
instead driven by two unresolved release decisions:

1. the current tree and commit metadata disclose personal/institutional
   identity, machine-local paths, SSH infrastructure, private project names,
   and exact historical Codex thread/turn identifiers; and
2. redistribution provenance for the two PNG assets is undocumented, while the
   workflow manga visibly contains third-party product names and logos.

If the maintainer deliberately accepts the identity/institution disclosures and
can document the image rights, the privacy cleanup can be narrower. Until that
decision is explicit, keep repository visibility unchanged.

## 2. Audit scope and method

The synchronized repository had 69 commits, one local branch, one matching
remote branch, no tags, 590 reachable Git objects, 267 reachable blob/path
records, and 71 tracked files before this report. The worktree was clean.

Read-only checks covered:

- every snapshot reachable from `git rev-list --all`, including files later
  deleted;
- high-confidence patterns for Slack/GitHub/cloud tokens, webhook URLs, private
  keys, JWTs, credentialed URLs, bearer values, literal credentials, DPAPI
  payloads, SSH keys, OAuth identifiers, cookies, and long encoded values;
- historical filenames associated with credentials, runtime state, databases,
  logs, captures, backups, and key/certificate stores;
- privacy patterns for usernames, emails, home/local paths, SSH hosts and
  aliases, project names, Slack identifiers, UUIDs, and private IP addresses;
- commit author/committer metadata and messages;
- reachable object sizes, symlink/submodule modes, ignored runtime paths,
  image metadata, dependency metadata, and source-level dangerous primitives;
  and
- the complete test suite, Python compilation, PowerShell parsing, and the
  active environment's package consistency.

Gitleaks was not locally installed. A pinned official `v8.30.1` Docker run was
attempted with a read-only repository mount and redaction, but Docker Engine was
not running, so the container never started. The final cleanup gate should run
Gitleaks or an equivalent reputable scanner in addition to repeating the
targeted checks. Official usage reference:
https://github.com/gitleaks/gitleaks/blob/master/README.md.

`git fsck` also reported local unreachable objects. They are outside every
audited ref and are not transferred by a normal clone. Prepare any release from
a fresh clone rather than copying this working directory or its `.git` object
store.

## 3. BLOCKER — secret/security

No secret/security blocker was found, and this audit does **not** recommend
rotating a credential at this time.

- No production Slack webhook, Slack token, GitHub token, private key, cloud
  key, JWT, credentialed URL, bearer value, DPAPI payload, SSH key, cookie, or
  encoded credential signature was found in any reachable commit.
- Twenty-four Slack-shaped strings were found only in synthetic fixtures at
  `tests/test_notifications.py:21`, `tests/test_slack_relay.py:73`, and
  `tests/test_watchdog_launcher.py:52` and their historical versions. Every
  value was explicitly marked as test/fake data.
- OAuth-shaped UUIDs at `tests/test_notifications.py:20` and
  `tests/test_outlook_oauth.py:22` are fixed test fixtures, not production app
  registrations.
- No sensitive filename was ever tracked. In particular, no `.env`, private
  key, CLIXML/DPAPI payload, token cache, SQLite/database, log, or runtime-state
  path appeared in reachable history.
- The actual Slack, Outlook, and local runtime stores remain outside Git under
  current-user application data or ignored `.codex-watchdog/` state. Their
  contents were not copied into this report.

This is strong negative evidence, not a mathematical guarantee. A successful
independent Gitleaks scan remains a release gate because the preferred scanner
could not run in the stopped Docker environment.

## 4. PRIVACY — sanitize or make an explicit user decision

### Commit identity

Forty-eight of 69 commits expose an institutional author/committer email and a
personal account name. The affected sequence begins at `3375766ebde8` and runs
through `2a8272a26b91`; the remaining 21 author records use a GitHub noreply
address. Two commit subjects, `702b0674f39f` and `a748bb4c74f0`, also name the
institutional remote environment.

The full legal author name in `LICENSE:3` and the repository owner's account
name in the Git remote are reasonable public attribution if intentional. The
maintainer must explicitly decide whether the institutional email/account name
and affiliation may also be public. A `.mailmap` changes presentation but does
not remove the original commit objects; genuine concealment requires reviewed
history rewriting.

### Current tracked content

Real environment-derived identifiers appear in both documentation and tests:

- user/home paths and local absolute paths:
  `doc/Progress/progress_2026_08_30_cpx03.md:51`,
  `doc/Progress/progress_2026_08_31_cpx04.md:155`,
  `doc/Progress/progress_2026_09_01_cpx05.md:132`,
  `doc/Progress/progress_2026_09_02_cpx10.md:18`,
  `doc/Progress/progress_2026_09_03_cpx12.md:59`,
  `doc/Progress/progress_2026_09_03_cpx13.md:106`, `README.md:896`,
  `tests/test_mvp_service.py:1394`, and `tests/test_remote_ssh.py:25`;
- institutional SSH hostnames, a personal SSH alias, and a concrete SSH target:
  `README.md:369`, `README.md:391`, `architecture.md:57`,
  `doc/Progress/progress_2026_09_02_cpx10.md:21`,
  `doc/Progress/progress_2026_09_03_cpx12.md:15`, and
  `doc/Progress/progress_2026_09_03_cpx13.md:17`;
- private project/repository names: `README.md:246`, `README.md:437`,
  `architecture.md:245`, `doc/Progress/progress_2026_08_30_cpx03.md:249`,
  `doc/Progress/progress_2026_09_02_cpx10.md:5`,
  `doc/Progress/progress_2026_09_03_cpx13.md:7`, and several remote-adapter
  tests; and
- exact historical Codex thread/turn UUIDs in progress reports cpx02, cpx03,
  cpx04, cpx05, cpx06, cpx10, and cpx13. These are not authentication secrets,
  but they are unnecessary internal correlation identifiers. The repeated
  `1111...` UUID in README command examples and fixed test UUIDs are visibly
  synthetic and safe.

No private IP address, machine name, production Slack channel/member ID, or
production email address was found in tracked file content. One deleted test
file, `tests/test_git_mutations.py` (`2c7593c7b953` through
`546ba960f0ac`), contained only a synthetic test email. No other historical-only
privacy surface was detected.

Recommended replacements are role-based placeholders such as `<USER>`,
`<LOCAL_REPO>`, `<REMOTE_HOST>`, `<REMOTE_REPO>`, `<PROJECT_A>`, and `<UUID>`.
Do not publish exact replacement values in an issue or progress report.

## 5. Existing progress reports

The reports are valuable technical history and can remain public after targeted
sanitization; wholesale deletion is neither needed nor recommended.

- No identified privacy replacement: cpx01, cpx07, cpx08, cpx14, and cpx15.
- Replace exact UUIDs: cpx02, cpx05, and cpx06.
- Replace usernames/home paths, absolute paths, exact UUIDs, private project
  names, remote URIs/hosts, or aliases as applicable: cpx03, cpx04, cpx09,
  cpx10, cpx11, cpx12, and cpx13.

All reports passed the high-confidence secret scan. Commit hashes, test-only
fingerprints, architecture decisions, failure descriptions, and verification
counts are harmless technical history worth retaining.

Editing only the current report files is insufficient if the same metadata must
be absent from public history. After the maintainer approves an exact replacement
map, use a fresh mirror and a narrowly reviewed `git filter-repo` plan for blob
text, commit messages, and author/committer identities. Do not run it against
the working repository and do not force-push until the rewritten graph has been
reviewed from a separate clone.

## 6. RELEASE HYGIENE — fix before public release

### Assets and branding

- `Shiro.png` was introduced by `d7075921346e`; `principleManga.png` by
  `fcaf374966e3`. Neither PNG contains EXIF or textual metadata, but neither has
  a documented creator, source, generation method, or redistribution license.
- The workflow manga visibly uses or closely depicts ChatGPT/OpenAI, GitHub,
  Slack, and VS Code names/logos. The project name and README also use product
  marks. Add an `ASSETS.md` provenance/license record and an unofficial,
  unaffiliated-project trademark disclaimer. Obtain/confirm permitted use or
  replace third-party logos with generic symbols before release.
- The software has an MIT `LICENSE`, but it is unclear whether the maintainer
  intends that license to cover the PNG artwork. State the asset license
  explicitly.

No third-party source code or binary is vendored in the tracked tree. External
projects cited by the progress reports were used as design references rather
than copied code.

### Dependencies and repository controls

- `pyproject.toml` declares MSAL, MSAL Extensions, and Slack Bolt with broad
  compatible ranges but has no lockfile, SBOM, vulnerability report, or
  third-party notices. Installed metadata identified MIT, Apache-2.0, and
  BSD/Apache-2.0 components; a release build must resolve its own isolated,
  pinned transitive set and preserve every required notice.
- The active Anaconda environment passed the project tests but `pip check`
  reported unrelated global-environment conflicts. It must not be treated as a
  reproducible release environment.
- Add a reviewed Gitleaks check to CI/pre-commit and harden `.gitignore` for
  CLIXML, private-key/certificate formats, local Slack/Duo JSON, and editor-local
  configuration. Existing rules already exclude `.env`, logs, databases,
  `.codex-watchdog/`, local Microsoft state, private test material, and build
  outputs.
- Add `license`, `readme`, project URLs/classifiers, and a supported-platform
  statement to package metadata before publishing a Python distribution.
- Consider `SECURITY.md` and a minimal contribution policy before accepting
  public reports or pull requests.

### Source-level security observations

No `shell=True`, dynamic `eval`/`exec`, unsafe pickle/YAML loading, or TLS
verification bypass was found. External commands use argument arrays; SMTP
STARTTLS uses the default verified SSL context. Remote instructions are passed
inside the stdin Python adapter, and Slack input is constrained to an
allowlisted user, configured channel, known WatchDog-created thread, and exact
existing Codex thread. Error persistence is digest/length based.

These controls are suitable for the current architecture. Public documentation
should nevertheless emphasize that Slack replies execute as Codex instructions
with the user's existing permissions, and that SSH/Codex/Git/VS Code are trusted
external prerequisites rather than sandbox boundaries.

## 7. SAFE / intentional public information

- The MIT-licensed Python/PowerShell implementation, tests, architecture, AI
  development declaration, and sanitized dogfooding history are suitable for
  public technical review.
- The full author name in the license, repository name, GitHub owner, and AI
  assistance declaration are safe if the maintainer intends them as public
  attribution.
- Synthetic tokens, UUIDs, hosts, emails, and URLs in tests are clearly fake and
  should remain because they verify validation and privacy behavior.
- Runtime directories, notification state, transient output, caches, logs, and
  credential stores are ignored and have never appeared in reachable history.
- No symlink, submodule, tracked executable, saved credential payload, or hidden
  historical release artifact was found.

Verification at the synchronized tree completed successfully: the full pytest
run reached 100% with one intentional skip, Python compilation passed, and both
PowerShell launchers parsed without errors.

## 8. Minimal cleanup sequence before visibility change

1. Keep the repository private and make no credential/history changes yet.
2. Approve a disclosure policy for the legal name, account name, institutional
   email/affiliation, SSH hosts, private project names, and historical UUIDs.
3. Prepare and review an exact placeholder map. Apply it first to a disposable
   fresh clone, including commit metadata/messages only where the policy
   requires it.
4. Validate the rewritten clone: refs/commit graph, tests, manual targeted
   scans, and a successful redacted full-history Gitleaks scan. Confirm that
   progress reports still read coherently.
5. Document image provenance and licensing; replace or clear third-party marks;
   add trademark and third-party-notice files.
6. Harden ignore/CI controls, complete package metadata, and create a locked,
   isolated dependency set with SBOM, license, and vulnerability review.
7. Review the resulting diff/history and release checklist with the maintainer.
   Only then change visibility or replace remote history.

No cleanup step above was executed in this checkpoint.

## 9. Later Windows executable plan

Do not build an executable until the repository passes the public-release gate.
Then use an isolated Windows build environment and a pinned build recipe. The
minimal distributable layout should be:

```text
codex-watchdog.exe
README.md
LICENSE
THIRD_PARTY_NOTICES.txt
ASSETS.md                 # only if cleared artwork is distributed
SHA256SUMS
```

Generate an SBOM, scan the resolved binary/dependencies, preserve licenses,
test on a clean Windows VM, and code-sign the executable/installer if practical.
Treat Git, Codex CLI, VS Code, OpenSSH, and PuTTY/Plink as separately installed
prerequisites unless their redistribution licenses and update obligations are
reviewed explicitly.

The package must never embed or copy:

- environment variables or `.env` files;
- Slack webhook/bot/app tokens, Outlook access/refresh material, SMTP passwords,
  OAuth caches, DPAPI/CLIXML payloads, or local channel/member mappings;
- `.codex-watchdog/`, `.codex/`, resume prompts, queues, rollout/session state,
  audits, logs, screenshots, debug files, test debris, or Git history;
- SSH private keys, agent state, host/user aliases, Duo responses, saved
  application-data configuration, or machine-specific absolute paths; or
- the maintainer's local Python/Conda environment.

All credentials and machine-specific configuration must be provisioned at first
run into current-user application data, with fail-closed validation and no
secret-bearing diagnostics.
