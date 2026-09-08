from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shlex

import pytest

from codex_watchdog import linux_package as package
from codex_watchdog import posix_package
from codex_watchdog.storage import FileLock, InstructionStore, StoreBusyError


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def legacy_hooks(root: Path, runtime: Path) -> dict:
    command = shlex.join([str(root / "old Python"), str(root / "tools/codex_watchdog_hook.py"),
                          "--runtime", str(runtime), "hook", "--grace-seconds", "45",
                          "--poll-seconds", "0.25"])
    return {"description": "user description", "future_key": {"retain": True}, "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 80,
                               "statusMessage": "my status", "future_option": "retain"}]}],
        "PermissionRequest": [{"matcher": "", "hooks": [
            {"type": "command", "command": command, "timeout": 10}]}],
        "UnrelatedEvent": [{"hooks": [{"type": "command", "command": "unrelated hook"}]}],
    }}


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2),
    ("platform", "macos"), ("architecture", "arm64"), ("version", "0.0.0"),
    ("files", {"codex-watchdog": "0" * 64}),
])
def test_candidate_manifest_refuses_wrong_identity_or_hash(tmp_path, monkeypatch, field, value):
    from codex_watchdog import __version__

    executable = tmp_path / "codex-watchdog"
    executable.write_bytes(b"\x7fELF\x02\x01" + bytes(12) + (62).to_bytes(2, "little"))
    manifest = {"schema_version": 1, "platform": "linux", "architecture": "x64",
                "version": __version__, "files": {"codex-watchdog": package.file_hash(executable)}}
    monkeypatch.setattr(package, "architecture", lambda: "x64")
    write_json(tmp_path / "package-manifest.json", manifest)
    package.validate_bundle(tmp_path)
    manifest[field] = value
    write_json(tmp_path / "package-manifest.json", manifest)
    with pytest.raises(package.PackageError, match="candidate_manifest_invalid"):
        package.validate_bundle(tmp_path)


def test_help_and_runtime_lookup_leave_fresh_user_state_absent(tmp_path, monkeypatch, capsys):
    config, codex = tmp_path / "config", tmp_path / "codex"
    monkeypatch.setenv("CODEX_WATCHDOG_LINUX_CONFIG_DIR", str(config))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    assert package.select_runtime(config, codex) == config / "runtime"
    assert package.main(["--help"], tmp_path / "codex-watchdog") == 0
    assert package.main([], tmp_path / "codex-watchdog") == 0
    assert not config.exists() and not codex.exists()
    assert "linux-install" in capsys.readouterr().out


def test_invalid_profile_error_has_no_private_path_or_contents(tmp_path, monkeypatch, capsys):
    config = tmp_path / "private profile"
    write_json(config / package.PROFILE_NAME, {"schema_version": 99, "secret": "private-value"})
    monkeypatch.setenv("CODEX_WATCHDOG_LINUX_CONFIG_DIR", str(config))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert package.main(["doctor"], tmp_path / "codex-watchdog") == 1
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err) == {"status": "blocked", "reason": "linux_profile_unsupported"}


@pytest.fixture
def installation(tmp_path: Path, monkeypatch):
    root = tmp_path.resolve()
    bundle, config, codex = root / "download with spaces", root / "Application Support", root / "codex home"
    bundle.mkdir()
    for name in package.PACKAGE_FILES:
        (bundle / name).write_bytes(("package payload " + name).encode())
    runtime = root / "existing source checkout/.codex-watchdog"
    runtime.mkdir(parents=True)
    (runtime / "journal-retain.json").write_text('{"retained":"journal"}', encoding="utf-8")
    write_json(codex / "hooks.json", legacy_hooks(root, runtime))
    (codex / "config.toml").write_text('trusted_hash = "unchanged"\n', encoding="utf-8")
    write_json(config / "slack-relay.json", {"schema_version": 1, "channel_id": "G12345678",
                                            "allowed_user_ids": ["U12345678"], "unknown": "preserve"})
    monkeypatch.setattr(package, "validate_executable", lambda path: None)
    monkeypatch.setattr(package, "validate_bundle", lambda path: None)
    return bundle, config, codex, runtime


def test_source_upgrade_reuses_runtime_without_touching_provider_or_trust(installation):
    bundle, config, codex, runtime = installation
    protected = [runtime / "journal-retain.json", config / "slack-relay.json",
                 codex / "hooks.json", codex / "config.toml"]
    before = {path: path.read_bytes() for path in protected}
    assert package.install_package(bundle, config, codex) == {"status": "installed", "runtime_reused": True}
    profile = package.load_profile(config)
    assert profile["runtime"] == str(runtime)
    assert all(path.read_bytes() == raw for path, raw in before.items())
    assert {p.name for p in (config / "bin").iterdir()} == set(package.PACKAGE_FILES)


def test_repeat_install_is_byte_idempotent_and_preserves_unknown_profile_keys(installation):
    bundle, config, codex, _ = installation
    package.install_package(bundle, config, codex)
    profile = package.load_profile(config)
    profile["future_feature"] = {"enabled": False}
    write_json(config / package.PROFILE_NAME, profile)
    before = (config / package.PROFILE_NAME).read_bytes()
    assert package.install_package(bundle, config, codex)["status"] == "unchanged"
    assert (config / package.PROFILE_NAME).read_bytes() == before
    assert not list(config.rglob("*.backup-*"))


def test_package_upgrade_snapshots_previous_files_and_preserves_profile(installation):
    bundle, config, codex, runtime = installation
    package.install_package(bundle, config, codex)
    profile = package.load_profile(config)
    profile["future_feature"] = {"retain": "value"}
    write_json(config / package.PROFILE_NAME, profile)
    before = {name: (config / "bin" / name).read_bytes() for name in package.PACKAGE_FILES}
    (bundle / "codex-watchdog").write_bytes(b"next tested executable")
    package.install_package(bundle, config, codex)
    after = package.load_profile(config)
    assert after["runtime"] == str(runtime) and after["future_feature"] == profile["future_feature"]
    for name, raw in before.items():
        backup = (config / "bin" / name).with_name(name + ".backup-" + profile["files"][name])
        assert backup.read_bytes() == raw
    assert (config / "bin/codex-watchdog").read_bytes() == b"next tested executable"


def test_failed_profile_publication_rolls_back_every_replaced_file(installation, monkeypatch):
    bundle, config, codex, _ = installation
    package.install_package(bundle, config, codex)
    previous = {name: (config / "bin" / name).read_bytes() for name in package.PACKAGE_FILES}
    profile_bytes = (config / package.PROFILE_NAME).read_bytes()
    for name in package.PACKAGE_FILES:
        (bundle / name).write_bytes(b"changed payload")

    def fail_write(path, value):
        raise OSError("publication failed")

    monkeypatch.setattr(InstructionStore, "_atomic_json", fail_write)
    with pytest.raises(OSError):
        package.install_package(bundle, config, codex)
    assert (config / package.PROFILE_NAME).read_bytes() == profile_bytes
    assert all((config / "bin" / name).read_bytes() == raw for name, raw in previous.items())


def test_invalid_candidate_never_replaces_an_install(installation, monkeypatch):
    bundle, config, codex, _ = installation

    def fail_validation(path):
        raise package.PackageError("linux_candidate_validation_failed")

    monkeypatch.setattr(package, "validate_executable", fail_validation)
    with pytest.raises(package.PackageError, match="candidate_validation"):
        package.install_package(bundle, config, codex)
    assert not (config / "bin").exists()
    assert not (config / package.PROFILE_NAME).exists()


def test_interrupted_snapshot_never_publishes_a_partial_backup(tmp_path, monkeypatch):
    path = tmp_path / "profile.json"
    path.write_bytes(b"previous usable profile")

    def interrupted_copy(source, output):
        output.write(b"partial")
        raise OSError("interrupted copy")

    monkeypatch.setattr(posix_package.shutil, "copyfileobj", interrupted_copy)
    with pytest.raises(OSError):
        package.snapshot(path)
    assert path.read_bytes() == b"previous usable profile"
    assert list(tmp_path.iterdir()) == [path]


def test_unknown_existing_file_and_modified_owned_file_are_not_overwritten(installation):
    bundle, config, codex, _ = installation
    destination = config / "bin"
    destination.mkdir()
    (destination / "codex-watchdog").write_bytes(b"not ours")
    with pytest.raises(package.PackageError, match="existing_install_conflict"):
        package.install_package(bundle, config, codex)
    assert (destination / "codex-watchdog").read_bytes() == b"not ours"
    (destination / "codex-watchdog").unlink()
    package.install_package(bundle, config, codex)
    (destination / "codex-watchdog").write_bytes(b"user modified")
    with pytest.raises(package.PackageError, match="existing_install_conflict"):
        package.install_package(bundle, config, codex)
    assert (destination / "codex-watchdog").read_bytes() == b"user modified"


def test_live_foreground_owner_blocks_replacement(installation):
    bundle, config, codex, runtime = installation
    with FileLock(runtime / "locks/foreground-run.lock"):
        with pytest.raises(StoreBusyError):
            package.install_package(bundle, config, codex)
    assert not (config / "bin").exists()


@pytest.mark.parametrize("version", [True, 0, 2, "1"])
def test_unsupported_profile_cannot_be_reset_by_install(installation, version):
    bundle, config, codex, runtime = installation
    write_json(config / package.PROFILE_NAME, {"schema_version": version, "runtime": str(runtime)})
    before = (config / package.PROFILE_NAME).read_bytes()
    with pytest.raises(package.PackageError, match="profile_unsupported"):
        package.install_package(bundle, config, codex, runtime=runtime)
    assert (config / package.PROFILE_NAME).read_bytes() == before


def test_ambiguous_legacy_runtime_and_duplicate_watchdog_hook_fail_closed(installation):
    _, config, codex, runtime = installation
    document = package.read_json(codex / "hooks.json")
    definition = document["hooks"]["Stop"][0]["hooks"][0]
    definition["command"] = definition["command"].replace(str(runtime), str(runtime / "other"))
    write_json(codex / "hooks.json", document)
    with pytest.raises(package.PackageError, match="legacy_runtime_ambiguous"):
        package.select_runtime(config, codex)
    document["hooks"]["Stop"][0]["hooks"].append(copy.deepcopy(definition))
    write_json(codex / "hooks.json", document)
    with pytest.raises(package.PackageError, match="hooks_ambiguous"):
        package.select_runtime(config, codex)


def test_hook_move_preserves_other_hooks_grace_unknown_keys_and_manual_trust(installation):
    bundle, config, codex, runtime = installation
    package.install_package(bundle, config, codex)
    old_bytes = (codex / "hooks.json").read_bytes()
    old = json.loads(old_bytes)
    trust_bytes = (codex / "config.toml").read_bytes()
    rendered = package.packaged_hooks(config, codex)
    assert (codex / "hooks.json").read_bytes() == old_bytes
    assert rendered["future_key"] == old["future_key"]
    assert rendered["hooks"]["UnrelatedEvent"] == old["hooks"]["UnrelatedEvent"]
    definition = rendered["hooks"]["Stop"][0]["hooks"][0]
    assert shlex.split(definition["command"]) == [str(config / "bin/codex-watchdog"), "--runtime", str(runtime),
                                                 "hook", "--grace-seconds", "45", "--poll-seconds", "0.25"]
    assert definition["timeout"] == 80 and definition["future_option"] == "retain"
    assert package.install_packaged_hooks(config, codex)["trust"] == "review_exact_definitions_in_codex"
    assert next(codex.glob("hooks.json.backup-*")).read_bytes() == old_bytes
    assert (codex / "config.toml").read_bytes() == trust_bytes
    stable_bytes = (codex / "hooks.json").read_bytes()
    (bundle / "codex-watchdog").write_bytes(b"upgraded executable")
    package.install_package(bundle, config, codex)
    assert package.install_packaged_hooks(config, codex)["status"] == "unchanged"
    assert (codex / "hooks.json").read_bytes() == stable_bytes
    assert len(list(codex.glob("hooks.json.backup-*"))) == 1


def test_fresh_render_adds_hooks_without_replacing_third_party_configuration(installation):
    bundle, config, codex, _ = installation
    existing = {"unknown": "keep", "hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "third-party-program"}]}]}}
    write_json(codex / "hooks.json", existing)
    package.install_package(bundle, config, codex)
    rendered = package.packaged_hooks(config, codex)
    assert rendered["unknown"] == "keep"
    assert rendered["hooks"]["Stop"][0] == existing["hooks"]["Stop"][0]
    assert len(rendered["hooks"]["Stop"]) == 2






@pytest.mark.skipif(os.name == "nt", reason="native POSIX symlink semantics")
def test_hook_symlink_is_preserved(installation):
    bundle, config, codex, _ = installation
    package.install_package(bundle, config, codex)
    original = codex / "original.json"
    (codex / "hooks.json").rename(original)
    (codex / "hooks.json").symlink_to(original)
    before = original.read_bytes()
    with pytest.raises(package.PackageError, match="symlink_write_refused"):
        package.install_packaged_hooks(config, codex)
    assert original.read_bytes() == before and (codex / "hooks.json").is_symlink()
