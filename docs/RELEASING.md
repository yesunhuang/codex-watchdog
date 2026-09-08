# Automatic version releases

Change the canonical `[project].version` in `pyproject.toml` and push it to
`main`. The `Publish versioned packages` workflow automatically builds a new
Windows x64 beta and an Apple Silicon developer-preview ZIP. Changes to other
pyproject settings without a version change do not republish anything.

Publication requires the complete Windows/Ubuntu/macOS Python 3.9 matrix,
the pinned Windows and ARM64 builds, a full public-history secret scan, embedded
Windows icon verification, Windows source-free fresh startup and upgrade from
the immediately previous public executable, and native Mac source-free package
acceptance. The upgrade test obtains the previous package and verifies its
published checksum, lets that executable create the saved launcher profile,
then verifies the candidate preserves the profile, hooks, provider stores,
settings, and retained journal bytes.

Only the final publish job has repository write permission. It combines the
tested artifacts, checks both ZIP hashes, uploads a draft release, and publishes
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

The shared matrix is called from the same commit using
[GitHub reusable workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).
Release creation uses the official [GitHub CLI](https://cli.github.com/manual/gh_release_create).
