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
