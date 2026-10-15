"""Fixture helper tests; mock CLI success is not packaged/native acceptance."""
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/package_notification_cutover.py"
spec = importlib.util.spec_from_file_location("package_notification_cutover", SOURCE)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
linux = pytest.mark.skipif(sys.platform != "linux", reason="actual Linux /proc fixture proof")
SID = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def native_census_available():
    tracker = helper.FixtureSenders.__new__(helper.FixtureSenders)
    tracker.groups, tracker.images, tracker.processes = set(), {}, []
    try:
        tracker.census()
    except PermissionError as error:
        # A shared development host can hide another same-UID process image.
        # Never interpret that gap as exclusion. CI and the actual package
        # fixture must supply complete native evidence and may not skip it.
        if os.environ.get("CI"):
            raise
        pytest.skip("native census capability unavailable: %s; actual packaged proof remains required" % error)


def test_canonical_json_matches_notification_protocol():
    from codex_watchdog.notification_receipt_retention import _sha
    value = {"z": [1, "非秘密"], "a": True}
    assert helper.digest(helper.canonical(value)) == _sha(value)


def test_unreadable_census_model_refuses(monkeypatch):
    tracker = helper.FixtureSenders.__new__(helper.FixtureSenders)
    tracker.groups, tracker.images, tracker.processes = set(), {}, []
    entry = SimpleNamespace(name="12345")
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: iter([entry]) if str(path) == str(Path("/proc")) else original(path))
    def unavailable(pid):
        raise PermissionError("modeled unreadable live same-UID process")
    monkeypatch.setattr(helper, "proc_record", unavailable)
    monkeypatch.setattr(os, "getuid", lambda: 1000, raising=False)
    with pytest.raises(PermissionError, match="modeled unreadable"):
        tracker.census()


def sleep_image(tmp_path):
    image = tmp_path / "unique-fixture-sleep"
    shutil.copyfile(shutil.which("sleep"), image)
    image.chmod(0o700)
    return image


@linux
def test_native_census_requires_backend_and_detached_image_closure(tmp_path, native_census_available):
    image = sleep_image(tmp_path)
    tracker = helper.FixtureSenders(tmp_path, [image])
    ready, release = tmp_path / "ready", tmp_path / "release"
    # Owned controller exits on release; its finite-lived backend stays in the
    # original group. No test process is killed or signaled by this test.
    code = ("import pathlib,subprocess,sys,time; "
            "subprocess.Popen([sys.argv[1],'3']); pathlib.Path(sys.argv[2]).write_text('ready'); "
            "deadline=time.monotonic()+5\n"
            "while not pathlib.Path(sys.argv[3]).exists() and time.monotonic()<deadline: time.sleep(.01)\n")
    parent = tracker.spawn([sys.executable, "-c", code, str(image), str(ready), str(release)])
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists() and tracker.census()["live"]
        release.touch()
        parent.wait(timeout=3)
        remaining = tracker.census()["live"]
        assert any(r["group"] == parent.pid for r in remaining), "parent exit is not backend closure"
        deadline = time.monotonic() + 5
        while tracker.census()["live"] and time.monotonic() < deadline:
            time.sleep(.03)
        assert not tracker.census()["live"]
        detached = subprocess.Popen([str(image), "1"], start_new_session=True)
        try:
            live = tracker.census()["live"]
            assert any(r["pid"] == detached.pid and r["group"] not in tracker.groups for r in live)
        finally:
            detached.wait(timeout=3)
        assert not tracker.census()["live"]
    finally:
        release.touch(exist_ok=True)
        parent.wait(timeout=7)


def setup_mock(tmp_path):
    runtime, home, repo = [tmp_path / name for name in ("runtime", "codex", "repo")]
    for path in (runtime, home, repo):
        path.mkdir()
    script = tmp_path / "mock-cold-cli.py"
    script.write_text("import json,sys\n"
        "assert 'notification-retention-prepare' in sys.argv\n"
        "print(json.dumps({'status':'ready','proof_scope':'caller_native_receipt_not_independent_census'}))\n")
    image = tmp_path / "mock-cold-cli"
    image.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(script)]) + ' "$@"\n')
    image.chmod(0o700)
    return runtime, home, repo, image


@linux
def test_mock_cli_projection_matches_real_adapter_and_preserves_claims(tmp_path, native_census_available):
    from codex_watchdog.notifications import EnvironmentNotifier, NotificationConfig
    from codex_watchdog.notification_cutover import notifier_cutover_projection
    runtime, home, repo, image = setup_mock(tmp_path)
    directory = runtime / "notifications"
    directory.mkdir()
    keys = [helper.digest(helper.canonical(dict(workspace_id="package-fixture", event_type=kind)))
            for kind in ("linux_owner_attention", "linux_owner_recovered")]
    with closing(sqlite3.connect(directory / "recent-notification-claims.sqlite3")) as db:
        db.execute("CREATE TABLE claims(scope,fingerprint,born,expires)")
        db.execute("INSERT INTO claims VALUES(?,?,?,?)", (keys[0], "a" * 64, 10, 259210))
        db.commit()
    protected = home / "opaque-fixture.bin"
    protected.write_bytes(b"synthetic test bytes")
    tracker = helper.FixtureSenders(tmp_path, [image])
    evidence = tmp_path / "evidence"
    result = tracker.prepare(image, runtime, home, "package-fixture", SID,
        dict(os.environ), tmp_path, evidence, [protected])
    request = json.loads((evidence / "projection.json").read_bytes())
    expected = notifier_cutover_projection(EnvironmentNotifier(runtime, NotificationConfig()),
        workspaces=[SimpleNamespace(workspace_id="package-fixture", session_id=SID)],
        observer_workspace_ids=[],event_types=("linux_owner_attention", "linux_owner_recovered"),
        current_contexts=[],current_http_pins=[])
    assert request == expected
    proof = json.loads((evidence / "quiescence.json").read_bytes())
    native = json.loads((evidence / "native-before.json").read_bytes())
    assert proof["projection_sha256"] == helper.digest(helper.canonical(request))
    assert proof["native_evidence_sha256"] == helper.digest(helper.canonical(native))
    assert native["live"] == [] and result["claims_preserved"] and result["protected_bytes_unchanged"]
    assert helper.claims(runtime) == [[keys[0], "a" * 64, 10, 259210]]
    with pytest.raises(AssertionError, match="one explicit"):
        tracker.prepare(image, runtime, home, "package-fixture", SID, {}, tmp_path, tmp_path / "again")


@linux
def test_unprojected_claim_refuses_without_launch_and_seals_gate(tmp_path, native_census_available):
    runtime, home, repo, image = setup_mock(tmp_path)
    (runtime / "notifications").mkdir()
    with closing(sqlite3.connect(runtime / "notifications/recent-notification-claims.sqlite3")) as db:
        db.execute("CREATE TABLE claims(scope,fingerprint,born,expires)")
        db.execute("INSERT INTO claims VALUES('unknown','old',1,2)")
        db.commit()
    tracker = helper.FixtureSenders(tmp_path, [image])
    with pytest.raises(AssertionError, match="unprojected"):
        tracker.prepare(image, runtime, home, "package-fixture", SID, {}, tmp_path, tmp_path / "evidence")
    assert tracker.phase == "failed" and helper.claims(runtime) == [["unknown", "old", 1, 2]]
    with pytest.raises(AssertionError, match="launch gate"):
        tracker.spawn([str(image)])
