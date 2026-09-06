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

## comment — development handoff

Please also create a **self-contained development handoff document** because this Codex thread may be replaced soon. The goal is that a fresh Codex thread can continue development safely without depending on any conversational context from this thread.

Please put the handoff in a durable repository document (for example `doc/DEVELOPMENT_HANDOFF.md`; choose a better existing location/name if the repository already has a convention). Keep it concise enough to be usable, but complete enough that a new thread can resume work without reconstructing the project history from all progress reports.

The handoff should include at least:

1. **Project purpose and architectural model**
   - what WatchDog does and intentionally does not do;
   - the lightweight control-plane philosophy;
   - the boundary between WatchDog, Codex workers, Git, Slack, SSH, and remote execution;
   - important architectural invariants such as fail-closed behavior, no domain reasoning/LLM dependency, and avoiding automatic Git actions that belong to Codex.

2. **Current repository state**
   - current release/version and relevant branches if applicable;
   - the major implemented capabilities;
   - important recent changes, especially thread discovery/wake behavior, Windows packaging/launcher behavior, remote support, and the new macOS/Linux work requested above;
   - what is considered stable/reference behavior versus experimental/beta behavior.

3. **Code map**
   - the important modules/files and their responsibilities;
   - where routing, dispatch/persistence, workspace/thread discovery, Git observation, Slack handling, queue wake, SSH/remote logic, launchers, tests, CI, and release tooling live;
   - any duplicated or fragile local/remote protocol logic that a new thread should be careful about.

4. **State and safety semantics**
   - instruction IDs / idempotency expectations;
   - delivered vs uncertain/rejected/dispatching semantics as currently implemented;
   - restart/recovery behavior;
   - thread/workspace ownership assumptions;
   - privacy/security boundaries and credential-storage rules.

5. **Known fragile assumptions / technical debt**
   - dependencies on VS Code/Codex internal state, logs, SQLite schemas, paths, or CLI output;
   - platform-specific assumptions;
   - any known bugs or architecture smells still open;
   - explicitly mention anything that must not be "simplified" without understanding why it exists.

6. **Testing and verification**
   - how to run the core tests;
   - Windows regression checks and package/release checks;
   - current CI behavior;
   - how future Linux/macOS verification should be interpreted (CI verified vs native probe verified vs E2E verified);
   - any manual tests that cannot yet be reproduced in CI.

7. **Development/release workflow**
   - how progress reports and `## comment` instructions are used;
   - how to make incremental changes without unrelated refactors;
   - version/release procedure and important publication/security gates;
   - where a new Codex thread should write its next progress report.

8. **Immediate next actions**
   - the cross-platform/macOS/Linux task from the previous comment;
   - `doctor` / privacy-safe diagnostic export;
   - normal PR/push cross-platform CI;
   - any higher-priority unresolved correctness/reliability issue you identify during the handoff audit.

9. **Resume checklist for a new Codex thread**
   Give a short ordered checklist such as: read the handoff, inspect latest progress report/comments, verify repo/branch/status, run baseline tests, verify current release assumptions, then continue only the highest-priority open task.

Please derive the document from the **actual current code and repository state**, not only from old progress reports. Do not include secrets, personal machine details, private hosts, conversation contents, or stale information. If an old progress report conflicts with current code, document the current code as authoritative and note the discrepancy only if it matters.

After creating/updating the handoff document, mention its exact path in the next progress report so a replacement thread can be pointed to it immediately.
