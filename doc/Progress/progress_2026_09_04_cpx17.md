# Codex WatchDog progress report — Checkpoint 17

Date: 2026-09-04
Scope: construct a separate public-release staging repository with fresh Git
history and generalized environment fixtures.

## 1. Fresh-history boundary

The publishing tree was exported from the reviewed private source tree without
its `.git` directory. It will receive one new initial commit under the generic
`Codex WatchDog Contributors` identity. Old author/committer metadata, deleted
files, unreachable objects, and private-repository commit history are not part
of this repository.

The private development repository remains unchanged and is not configured as a
remote of the publishing repository.

## 2. Sanitization completed

The staging tree consistently replaces environment-derived material with
public fixtures:

- Windows and Unix usernames use `operator`;
- local repositories use a generic `D:\projects\...` root;
- remote repositories use `/home/operator/...`;
- private repositories use `ProjectAlpha` and `ProjectBeta`;
- the MFA-protected host uses `hpc-login.example.edu`;
- the key-authenticated remote example uses `gpu-lab-personal`; and
- exact live thread/turn UUIDs and workspace-state identifiers were removed
  from progress and probe reports.

The software copyright holder is the non-personal `Codex WatchDog
contributors` collective. Synthetic credentials, addresses, UUIDs, hosts, and
URLs remain in tests where they are needed to verify validation behavior.

## 3. Release hardening

The staging tree adds:

- an independent/unaffiliated-project disclaimer in `README.md`;
- `SECURITY.md` with private reporting and runtime-secret boundaries;
- `THIRD_PARTY_NOTICES.md` with direct dependency licensing and executable
  packaging obligations;
- `ASSETS.md` with explicit provenance and publication status; and
- ignore rules for local editor state, CLIXML, key/certificate formats, and
  operator-local JSON.

Python project metadata now declares its README, MIT license, supported Python
baseline, and Windows platform classification.

## 4. Verification

The sanitized tree reported:

- zero occurrences of the known personal names, usernames, original host and
  project names, institutional domain, channel identifier, or workspace name;
- zero high-confidence production secret signatures;
- zero private IP addresses;
- zero exact UUID or 32-hex state identifiers in progress, architecture, and
  probe reports; and
- four Slack-shaped values, all intentionally synthetic test fixtures.

The complete pytest suite passed with one intentional skip. Python compilation,
Black formatting, PowerShell parsing, and `pyproject.toml` parsing also passed.

## 5. Publication gate

The new GitHub repository must remain private staging until both conditions are
confirmed:

1. the maintainer confirms redistribution rights and an explicit license for
   `Shiro.png` and `principleManga.png`, including the workflow comic's
   third-party product/logo references; and
2. a reputable independent full-history secret scan succeeds on the new
   one-commit repository.

Once those gates pass, a final clean clone should be rescanned and tested before
changing visibility. No executable or release artifact is built in this
checkpoint.

## comment

Please work on adding **macOS and Linux support to Codex WatchDog**, while preserving the current lightweight architecture and existing Windows behavior.

The goal is not to blindly port every Windows-specific implementation. First audit the current platform-dependent surface and determine the cleanest minimal architecture for cross-platform support.

Main requirements:

1. Keep the core platform-independent. Routing, dispatch state, Git observation, persistence, Slack/event logic, etc. should remain shared. Isolate genuinely platform-specific behavior behind small adapters/interfaces rather than scattering OS conditionals throughout the core.

2. Audit and isolate the main platform-specific areas, especially VS Code user-data/workspace discovery, `code --status` and live-window discovery, Codex executable/extension discovery, filesystem locking/atomic persistence, credential storage, launcher/background-service behavior, and packaging/install paths. Reuse existing POSIX support instead of rewriting working code.

3. Add normal PR/push cross-platform CI independent of the release workflow, at least on `windows-latest`, `ubuntu-latest`, and `macos-latest`. Run the shared test suite, compile checks, CLI/version smoke tests, and platform-adapter tests on all applicable platforms. Ordinary code changes should be covered; do not rely only on the Windows release workflow.

4. For Linux, separate if useful: (a) Linux as a remote execution target and (b) Linux as a local desktop host. We have access to real remote Linux machines, so remote/CLI behavior can later be validated there. Do not block useful Linux support merely because Linux-desktop GUI E2E testing is initially unavailable.

5. For macOS, use GitHub-hosted macOS runners for native OS testing. Initial macOS support may remain preview/beta until live VS Code/Codex behavior is validated on a real Mac. Prefer modern Apple Silicon support first if packaging architecture choices are needed; do not add legacy Intel complexity unless cheap.

6. Implement a cross-platform `codex-watchdog doctor` diagnostic capability. It should be read-only and report platform/architecture, VS Code CLI and user-data discovery, Codex extension/executable and home/state availability, workspace-storage readability, live-window discovery where supported, current-thread resolution where supported, queue/wake capability, and a useful PASS/PARTIAL/FAIL result with reasons.

Also consider `codex-watchdog doctor --export`, producing a small privacy-safe JSON that a macOS tester can send back. The export must not include conversation contents, tokens/secrets, Slack credentials, usernames, raw home paths, SSH hosts, repository contents, or Codex message contents. Normalize/redact paths and identifiers as needed.

7. Be conservative about support claims. Distinguish CI-verified, native-probe-verified, and full-E2E-verified support. Do not claim full macOS support merely because unit tests pass on `macos-latest`.

8. Preserve Windows as the reference implementation. Cross-platform refactoring must not regress workspace/thread discovery, wake delivery, persistence/idempotency, Slack behavior, or current Windows packaging/launcher behavior.

Architecture preference is conceptually: a shared core for routing/dispatch/storage/events, thin platform adapters for Windows/Linux/macOS, and integrations for VS Code/Codex/Git/Slack/SSH. This is guidance, not a request for a large rewrite. Prefer the smallest refactor that establishes clean platform boundaries.

Important constraints: keep WatchDog lightweight; no Postgres/Redis/message broker/container stack; no LLM dependency; do not turn the remote helper into a second independent WatchDog; avoid unrelated refactors; prefer incremental test-backed changes; and when upstream VS Code/Codex behavior is uncertain, fail closed and expose a useful diagnostic instead of guessing.

Please first audit the current codebase against these requirements, then implement the highest-value cross-platform path incrementally. Add tests for every new platform abstraction and document what is genuinely verified versus what still requires real-machine E2E validation.

At the end, write a new progress report summarizing architecture changes, files changed, CI coverage, Windows regression status, Linux status, macOS status, remaining real-machine validation needed, and any fragile VS Code/Codex assumptions.
