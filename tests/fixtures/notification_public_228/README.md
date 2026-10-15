# Previous-public notification readers

`notification_receipts.py`, `storage.py` and `models.py` are unchanged source
bytes from public Codex WatchDog **2.2.8**, commit
`657a6a28a3a97e467ed0a3f22fee52e46687d756`:
<https://github.com/yesunhuang/codex-watchdog>.

The retained source archive has SHA256
`ad51c973a58b7d502371b434f062c71ed12a662eb101a09f680f6470c1cdaa6b`
and its Git PAX comment names that exact commit. The canonical archived
`pyproject.toml` says `2.2.8`. `manifest.json` records individual byte hashes;
`LICENSE` is the original complete MIT license from the same archive.

The test creates a unique package alias and loads these three modules with
their original relative imports. The fixture-only `__init__.py` omits the
original version import, so it neither requires an extra application module nor
uses ambient installed metadata. The old storage and model implementations are
used, rather than the current package's classes. No reader logic is changed.

This fixture tests refusal by the actual old receipt readers before a claim or
possible provider attempt. It is not the complete old notifier/import closure,
a compiled previous-version executable, or native upgrade acceptance. In
particular, old notifier provider fallback is independent: both the dual marker
and fallback database must retain their incompatible format discriminators.
