# Manual testing and releases

GitHub Actions is disabled for this project. Do not dispatch or re-enable a
workflow. The workflow files remain as historical descriptions of acceptance
checks; they are not the release mechanism.

Use `pyproject.toml` as the version source. On an approved machine, install the
appropriate pinned release dependencies and run `python -m pytest` and
`python -m compileall -q src tools tests packaging scripts`. Native process tests
need native OS permissions; record any platform skips and verify them on their
own platform.

## Release dependency scope

If source or dependencies bundled into multiple supported executables change,
**rebuild, test and release every affected platform from one canonical source
revision**. A platform-only release is allowed only when the changed dependency
closure is demonstrably platform-exclusive. Record that analysis in the release
receipt. Different native acceptance depths must be disclosed; they do not
justify shipping another platform's stale binary.

For shared runtime changes, the current artifact set is Windows x64, Linux ARM64,
Linux x64 and macOS ARM64. Keep a release draft until all required packages and
their acceptance receipts are present. A missing native build host is a release
blocker, not permission to relabel an older package. In particular, the v0.2.21
local-discovery change was reproduced first on Windows but changed shared source.
The next release must rebuild all four targets without replacing v0.2.21.

## Native build and acceptance

When a Linux build host is newer than the supported glibc baseline, the build
can reuse system libraries from a checksum-verified previous package of the same
architecture: add `--native-runtime-package /path/to/previous-package` to the
Linux build command. The recipe verifies the previous executable, each reused
library and its license texts, preserves their actual versions and provenance,
and still checks every bundled ELF against the original glibc limit. It does not
replace the pinned build Python or application dependencies. Record the previous
release/archive checksum alongside acceptance evidence.

Build and validate on the corresponding platform:

| Platform | Build | Acceptance |
| --- | --- | --- |
| Windows x64 | `scripts/build_windows_release.ps1` | `tools/verify_windows_executable_icon.py` and `scripts/test_windows_package.ps1` |
| Linux ARM64 / x64 | `scripts/build_linux_package.py` | `scripts/test_linux_package.py --package <package-directory>` |
| macOS ARM64 | `scripts/build_macos_package.py` | `scripts/test_macos_package.py --package <package-directory>` |

For Windows, supply the immediately previous public package and its version to
the acceptance script. A fresh install does not replace the profile/runtime
upgrade test. Test the x64 Linux package on the oldest supported glibc platform.
Do not stop production processes to run package tests; use disposable profiles.

Before publishing, verify archive hashes, version and source revision, embedded
Windows icon, preserved upgrade state, and the absence of runtime data,
credentials, private paths and development history. Use
`scripts/verify_linux_release_assets.py` for Linux archive/acceptance receipts.
Keep platform acceptance JSON files with the release assets. Already completed
artifacts may be reused only after their source revision, hashes and acceptance
receipts have been verified; downloading them does not run Actions.

Create a new tag/release manually with `gh release create`, specifying the tested
source commit, release notes and verified assets. A draft may be used while
checking the uploaded hashes; publish it with `gh release edit --draft=false`
once complete. Never replace an existing published version to hide an error.
Record manual test evidence and deployment results in the development progress
report. Do not reconfigure unrelated user state during installation.
