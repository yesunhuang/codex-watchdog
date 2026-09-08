# Automatic version releases

Change the canonical `[project].version` in `pyproject.toml` and push it to
`main`. The `Publish versioned packages` workflow automatically builds new
Windows x64, macOS ARM64 preview, Linux ARM64, and Linux x64 ZIPs. Changes to other
pyproject settings without a version change do not republish anything.

Publication requires the complete Windows/Ubuntu/macOS Python 3.9 matrix,
the pinned native package builds, a full public-history secret scan, embedded
Windows icon verification, Windows source-free fresh startup and upgrade from
the immediately previous public executable, native Mac source-free package
acceptance, and both Linux executable acceptance jobs. The upgrade test obtains
the previous package and verifies its
published checksum, lets that executable create the saved launcher profile,
then verifies the candidate preserves the profile, hooks, provider stores,
settings, and retained journal bytes.

The reusable `linux-package.yml` workflow builds ARM64 on `ubuntu-22.04-arm`.
The x64 runner uses a digest-pinned Red Hat UBI 8.10 build container with glibc
2.28, then tests the actual package on both Ubuntu 22.04 and UBI 8. Python
3.12.14, pip 26.0.1, and `requirements-linux-package.txt` stay pinned. The UBI
recipe verifies the official Python source checksum and retains vendor RPM
license records. SQLite's public-domain notice is included only when its exact
RPM/library identity and declaration agree. Every bundled ELF dependency is
checked against the architecture's glibc ceiling before packaging. The derived
minimum is recorded in the manifest and acceptance receipt. Each job runs the
full source suite and tests
its actual executable with Python hidden from PATH. Gates cover ELF identity,
all shipped hashes and native dependency licenses, privacy, fresh installation,
source-runtime reuse, replacement and stable hooks, 30-second fixture Stop,
kernel ownership locks, exact-thread fixture queue/restart/release, and read-only
Git observation and credential-free default-CA HTTPS. The disposable UBI
acceptance container receives its own temporary machine ID because the base
image omits that binding prerequisite. Real-machine Ubuntu ARM64 and RHEL 8.10
acceptance remains distinct from general desktop E2E.

The reusable `macos-package.yml` retains the pinned Apple Silicon build and
isolated Keychain/upgrade/hook gates. It additionally requires the installed
executable to verify TLS to Slack's unauthenticated `api.test` endpoint using
the default macOS system CA, without source Python or certificate overrides.
No Slack credential or message is involved. Both native POSIX package workflows
also exercise manual thread rebind migration through the actual executable,
including backup preservation, first/subsequent Stop processing once each,
and unchanged Git state. The Windows v0.2.6 gate upgrades the actual public
v0.2.5 executable's profile/runtime to the candidate.

Only the final publish job has repository write permission. It combines the
tested artifacts, checks all four ZIP hashes and the Linux manifest/receipt
version, architecture, and exact source commit, uploads a draft release, and publishes
that draft after successful asset upload. The release targets the exact tested
commit. Existing tags/releases are refused; they are never overwritten.

If a build or test fails before release creation, fix the cause on `main` and
run this workflow manually to retry the still-unreleased version. If a draft or
tag was partially created, inspect that exact release state first. A published
packaging or migration correction requires a new version.

Update `docs/RELEASE_NOTES.md` with the release's behavior and support limits.
The workflow appends GitHub-generated changes since the selected previous tag.
The Mac asset remains ad-hoc signed and not notarized unless a separately
reviewed signing flow is added. Manual Mac acceptance is reported separately
from hosted package checks.

Each release includes four versioned ZIPs, `SHA256SUMS.txt`, four automated
package-acceptance JSON records, and shared license/notices/provenance files.
Each Linux ZIP includes its source-commit manifest and complete dependency and
native-library license inventory. The public build allowlist excludes runtime,
credential, private-machine, and development-history data.

The shared matrix is called from the same commit using
[GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).
Release creation uses the official [GitHub CLI](https://cli.github.com/manual/gh_release_create).
