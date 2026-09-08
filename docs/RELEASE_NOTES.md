Version 0.2.6 fixes Linux x64 startup on RHEL 8.10. The v0.2.5 x64 executable
required glibc 2.35 and could not start on a glibc 2.28 host. The new x64 package
builds Python 3.12.14 and its native dependencies against UBI 8.10. Every bundled
ELF library and the executable bootloader must fit the glibc 2.28 limit, and the
manifest records the measured requirement. No host glibc replacement is needed.

x64 package acceptance runs on both Ubuntu 22.04 and UBI 8. Linux ARM64 retains
its Ubuntu 22.04/glibc 2.35 baseline. All packages include dependency and native
library notices with hashes; the RPM inventory uses the vendor's license files
and the upstream SQLite public-domain notice only when the exact SQLite runtime
RPM declares that license. See the
[Linux package guide](https://github.com/yesunhuang/codex-watchdog/blob/main/docs/LINUX_PACKAGE.md).

This release changes packaging, not persistent runtime schemas or thread
ownership. Compatible profiles, provider settings, credential stores, runtimes,
and trusted stable hook paths continue to be reused. The v0.2.5 Mac certificate
discovery and manual thread-rebind fixes remain included. Windows x64, macOS 15
ARM64 preview, Linux ARM64, and Linux x64 continue to ship as separate executable
ZIPs. The Mac asset remains ad-hoc signed and not notarized; real-user desktop
and detached lifecycle acceptance remain distinct from isolated package tests.

English, Chinese, and Japanese READMEs remain together at the repository root,
with matching Quick Start structure and updated Linux requirements. Automatic
publication on a new version retains the three-OS source matrix, all four native
packages, complete-history secret scan, embedded Windows icon verification, and
upgrade from the actual immediately previous public Windows v0.2.5 executable.
Published v0.2.3, v0.2.4, and v0.2.5 tags and assets remain unchanged.
