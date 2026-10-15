"""Focused tests for local observer notification source clocks."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_watchdog.observer_notification_sources import ObserverNotificationSources
from codex_watchdog.storage import FileLock, InstructionStore, StoreBusyError

WORKSPACE = "workspace-one"
DISCOVERY = "discovery_attention"
RESUME = "resume_ambiguity"
FP_A = "a" * 64
FP_B = "b" * 64
FP_C = "c" * 64
HORIZON = 72 * 60 * 60
MAX_GENERATION = 2**63 - 2


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path.resolve() / "runtime"
    path.mkdir()
    return path


@pytest.fixture
def sources(runtime):
    helper = ObserverNotificationSources(runtime)
    yield helper
    helper.close()


def _scope(workspace_id):
    return hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()


def _sources_dir(runtime):
    return runtime / "service" / "observer-sources"


def _state(runtime, workspace_id=WORKSPACE):
    return _sources_dir(runtime) / f"{_scope(workspace_id)}.json"


def _document(scope, points, **extra):
    document = {"schema_version": 1, "scope_sha256": scope, "points": points}
    document.update(extra)
    return document


def _point(fingerprint, born, checked_at, generation, **extra):
    point = {
        "fingerprint": fingerprint,
        "born": born,
        "checked_at": checked_at,
        "generation": generation,
    }
    point.update(extra)
    return point


def _write_raw(runtime, workspace_id, data):
    path = _state(runtime, workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _baseline(fingerprint, checked_at):
    return _point(fingerprint, None, float(checked_at), 0)


def _symlink(link, target, *, directory):
    try:
        os.symlink(str(target), str(link), target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unsupported here: {exc}")


class _CountingWriter:
    def __init__(self):
        self.calls = 0

    def __call__(self, path, value):
        self.calls += 1
        InstructionStore._atomic_json(path, value)


def test_first_observation_is_unknown_baseline(runtime, sources):
    point = sources.observe(WORKSPACE, DISCOVERY, FP_A, 10)

    assert point == _baseline(FP_A, 10)
    stored = json.loads(_state(runtime).read_text("utf-8"))
    assert stored == _document(_scope(WORKSPACE), {DISCOVERY: point})
    assert sources.observe("w" * 4096, RESUME, FP_B, 0.5) == _baseline(FP_B, 0.5)


def test_strictly_later_change_records_birth_and_poll_preserves_it(sources):
    sources.observe(WORKSPACE, DISCOVERY, FP_A, 10)

    changed = sources.observe(WORKSPACE, DISCOVERY, FP_B, 20)
    assert changed == _point(FP_B, 20.0, 20.0, 1)

    for moment in (21, 22.5, 30):
        polled = sources.observe(WORKSPACE, DISCOVERY, FP_B, moment)
        assert polled == _point(FP_B, 20.0, float(moment), 1)


def test_restart_reuses_point_without_rebirth(runtime, sources):
    sources.observe(WORKSPACE, DISCOVERY, FP_A, 10)
    sources.observe(WORKSPACE, DISCOVERY, FP_B, 20)

    restarted = ObserverNotificationSources(runtime)
    try:
        assert restarted.observe(WORKSPACE, DISCOVERY, FP_B, 50) == _point(
            FP_B, 20.0, 50.0, 1
        )
    finally:
        restarted.close()


def test_equal_time_same_fingerprint_valid_without_write(runtime):
    writer = _CountingWriter()
    helper = ObserverNotificationSources(runtime, writer=writer)
    helper.observe(WORKSPACE, DISCOVERY, FP_A, 10)
    before = _state(runtime).read_bytes()

    assert helper.observe(WORKSPACE, DISCOVERY, FP_A, 10) == _baseline(FP_A, 10)
    assert writer.calls == 1
    assert _state(runtime).read_bytes() == before


def test_backwards_and_same_time_change_return_none_without_write(runtime):
    writer = _CountingWriter()
    helper = ObserverNotificationSources(runtime, writer=writer)
    helper.observe(WORKSPACE, DISCOVERY, FP_A, 10)
    helper.observe(WORKSPACE, DISCOVERY, FP_B, 20)
    before = _state(runtime).read_bytes()

    assert helper.observe(WORKSPACE, DISCOVERY, FP_B, 19.5) is None
    assert helper.observe(WORKSPACE, DISCOVERY, FP_A, 5) is None
    assert helper.observe(WORKSPACE, DISCOVERY, FP_C, 20) is None
    assert writer.calls == 2
    assert _state(runtime).read_bytes() == before


def test_rollback_to_previous_fingerprint_is_a_new_change(sources):
    sources.observe(WORKSPACE, DISCOVERY, FP_A, 10)
    sources.observe(WORKSPACE, DISCOVERY, FP_B, 20)

    assert sources.observe(WORKSPACE, DISCOVERY, FP_A, 30) == _point(FP_A, 30.0, 30.0, 2)


def test_returned_point_is_a_copy(sources):
    point = sources.observe(WORKSPACE, DISCOVERY, FP_A, 10)
    point["born"] = 1.0
    point["generation"] = 7

    assert sources.observe(WORKSPACE, DISCOVERY, FP_A, 10) == _baseline(FP_A, 10)


def test_slots_are_independent_and_malformed_other_slot_is_preserved(runtime, sources):
    document = _document(_scope(WORKSPACE), {RESUME: "not-a-point"})
    path = _write_raw(runtime, WORKSPACE, json.dumps(document).encode("utf-8"))

    assert sources.observe(WORKSPACE, DISCOVERY, FP_A, 5) == _baseline(FP_A, 5)
    stored = json.loads(path.read_text("utf-8"))
    assert stored["points"][RESUME] == "not-a-point"
    with pytest.raises(ValueError):
        sources.observe(WORKSPACE, RESUME, FP_A, 6)

    sources.observe("other", DISCOVERY, FP_A, 1)
    resume = sources.observe("other", RESUME, FP_B, 2)
    sources.observe("other", DISCOVERY, FP_C, 3)
    stored = json.loads(_state(runtime, "other").read_text("utf-8"))
    assert stored["points"][RESUME] == resume
    assert stored["points"][DISCOVERY] == _point(FP_C, 3.0, 3.0, 1)


def _assert_refused_without_mutation(runtime, sources, data):
    path = _write_raw(runtime, WORKSPACE, data)

    with pytest.raises(ValueError):
        sources.observe(WORKSPACE, DISCOVERY, FP_B, 100)
    assert path.read_bytes() == data
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda s: _document(s, {DISCOVERY: _point(FP_A.upper(), None, 1, 0)}),
            id="uppercase-fingerprint",
        ),
        pytest.param(
            lambda s: _document(s, {DISCOVERY: _point(FP_A, 1.0, 2.0, True)}),
            id="bool-generation",
        ),
        pytest.param(
            lambda s: _document(s, {DISCOVERY: _point(FP_A, 1.0, 2.0, 0)}),
            id="born-at-generation-zero",
        ),
        pytest.param(
            lambda s: _document(s, {DISCOVERY: _point(FP_A, None, 2.0, 1)}),
            id="unborn-later-generation",
        ),
        pytest.param(
            lambda s: _document(s, {DISCOVERY: _point(FP_A, 3.0, 2.0, 1)}),
            id="born-after-check",
        ),
        pytest.param(
            lambda s: _document(s, {DISCOVERY: _point(FP_A, None, -1, 0)}),
            id="negative-checked-at",
        ),
        pytest.param(
            lambda s: _document(s, {DISCOVERY: _point(FP_A, None, "1", 0)}),
            id="string-checked-at",
        ),
        pytest.param(
            lambda s: _document(
                s, {DISCOVERY: {"fingerprint": FP_A, "checked_at": 1, "generation": 0}}
            ),
            id="missing-born",
        ),
        pytest.param(
            lambda s: _document(s, {DISCOVERY: [FP_A]}),
            id="point-not-object",
        ),
        pytest.param(
            lambda s: _document(s, {DISCOVERY: None}),
            id="null-point",
        ),
        pytest.param(
            lambda s: _document(s, {"extra_slot": _point(FP_A, None, 1, 0)}),
            id="extra-slot",
        ),
        pytest.param(lambda s: dict(_document(s, {}), schema_version=2), id="schema-2"),
        pytest.param(
            lambda s: dict(_document(s, {}), schema_version=True), id="schema-bool"
        ),
        pytest.param(lambda s: _document(_scope("another"), {}), id="scope-mismatch"),
        pytest.param(lambda s: _document(s, []), id="points-list"),
        pytest.param(lambda s: [s], id="top-level-list"),
    ],
)
def test_corrupt_selected_state_raises_without_mutation(runtime, sources, build):
    data = json.dumps(build(_scope(WORKSPACE))).encode("utf-8")

    _assert_refused_without_mutation(runtime, sources, data)


def _nan_document(scope):
    document = _document(scope, {DISCOVERY: _point(FP_A, None, "__NAN__", 0)})
    return json.dumps(document).replace('"__NAN__"', "NaN").encode("utf-8")


def _duplicate_key_document(scope):
    return (json.dumps(_document(scope, {}))[:-1] + ', "points": {}}').encode("utf-8")


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda s: b"{not json", id="invalid-json"),
        pytest.param(lambda s: bytes([0xFF, 0xFE, 0x7B, 0x7D]), id="invalid-utf8"),
        pytest.param(_nan_document, id="nan"),
        pytest.param(_duplicate_key_document, id="duplicate-key"),
        pytest.param(lambda s: b"[" * 8000, id="deep-nesting"),
    ],
)
def test_corrupt_state_bytes_raise_without_mutation(runtime, sources, build):
    _assert_refused_without_mutation(runtime, sources, build(_scope(WORKSPACE)))


def test_oversized_state_raises_without_mutation(runtime, sources):
    document = _document(
        _scope(WORKSPACE), {DISCOVERY: _point(FP_A, None, 1, 0)}, pad="x" * 8192
    )

    _assert_refused_without_mutation(runtime, sources, json.dumps(document).encode("utf-8"))


def test_state_at_read_bound_is_accepted_but_not_grown(runtime, sources):
    document = _document(_scope(WORKSPACE), {DISCOVERY: _point(FP_A, None, 1, 0)}, pad="")
    document["pad"] = "x" * (8192 - len(json.dumps(document).encode("utf-8")))
    data = json.dumps(document).encode("utf-8")
    assert len(data) == 8192
    path = _write_raw(runtime, WORKSPACE, data)

    assert sources.observe(WORKSPACE, DISCOVERY, FP_A, 1)["generation"] == 0
    with pytest.raises(ValueError):
        sources.observe(WORKSPACE, DISCOVERY, FP_A, 2)
    assert path.read_bytes() == data


def test_unknown_fields_and_original_times_preserved(runtime, sources):
    document = _document(
        _scope(WORKSPACE),
        {DISCOVERY: _point(FP_A, None, 10, 0, note="kept-point")},
        note="kept-outer",
    )
    path = _write_raw(runtime, WORKSPACE, json.dumps(document).encode("utf-8"))

    changed = sources.observe(WORKSPACE, DISCOVERY, FP_B, 20)
    assert changed == _point(FP_B, 20.0, 20.0, 1, note="kept-point")
    polled = sources.observe(WORKSPACE, DISCOVERY, FP_B, 30)
    assert polled == _point(FP_B, 20.0, 30.0, 1, note="kept-point")

    stored = json.loads(path.read_text("utf-8"))
    assert stored["note"] == "kept-outer"
    assert stored["points"] == {DISCOVERY: polled}


@pytest.mark.parametrize(
    "workspace_id,slot,fingerprint,observed_at",
    [
        ("", DISCOVERY, FP_A, 1),
        ("w" * 4097, DISCOVERY, FP_A, 1),
        (b"workspace", DISCOVERY, FP_A, 1),
        (None, DISCOVERY, FP_A, 1),
        (chr(0xD800), DISCOVERY, FP_A, 1),
        (WORKSPACE, "other_slot", FP_A, 1),
        (WORKSPACE, None, FP_A, 1),
        (WORKSPACE, DISCOVERY, FP_A.upper(), 1),
        (WORKSPACE, DISCOVERY, "a" * 63, 1),
        (WORKSPACE, DISCOVERY, "g" * 64, 1),
        (WORKSPACE, DISCOVERY, None, 1),
        (WORKSPACE, DISCOVERY, FP_A, True),
        (WORKSPACE, DISCOVERY, FP_A, "1"),
        (WORKSPACE, DISCOVERY, FP_A, None),
        (WORKSPACE, DISCOVERY, FP_A, float("nan")),
        (WORKSPACE, DISCOVERY, FP_A, float("inf")),
        (WORKSPACE, DISCOVERY, FP_A, -1),
        (WORKSPACE, DISCOVERY, FP_A, 10**400),
    ],
)
def test_invalid_arguments_rejected_before_any_write(
    runtime, sources, workspace_id, slot, fingerprint, observed_at
):
    with pytest.raises(ValueError):
        sources.observe(workspace_id, slot, fingerprint, observed_at)
    assert list(runtime.iterdir()) == []


def test_write_failure_propagates_without_state(runtime, sources):
    def failing(path, value):
        raise OSError("simulated write failure")

    sources.observe(WORKSPACE, DISCOVERY, FP_A, 10)
    before = _state(runtime).read_bytes()
    broken = ObserverNotificationSources(runtime, writer=failing)

    with pytest.raises(OSError):
        broken.observe(WORKSPACE, DISCOVERY, FP_B, 20)
    with pytest.raises(OSError):
        broken.observe("fresh", DISCOVERY, FP_A, 20)
    assert _state(runtime).read_bytes() == before
    assert not _state(runtime, "fresh").exists()
    assert sources.observe(WORKSPACE, DISCOVERY, FP_B, 20) == _point(FP_B, 20.0, 20.0, 1)


def test_generation_exhaustion_refuses_without_write(runtime, sources):
    document = _document(
        _scope(WORKSPACE), {DISCOVERY: _point(FP_A, 1.0, 2.0, MAX_GENERATION)}
    )
    data = json.dumps(document).encode("utf-8")
    path = _write_raw(runtime, WORKSPACE, data)

    with pytest.raises(ValueError):
        sources.observe(WORKSPACE, DISCOVERY, FP_B, 3)
    assert path.read_bytes() == data
    assert sources.observe(WORKSPACE, DISCOVERY, FP_A, 3)["generation"] == MAX_GENERATION


def test_lock_holder_excludes_observe_and_cleanup(runtime, sources):
    sources.observe(WORKSPACE, DISCOVERY, FP_A, 10)
    before = _state(runtime).read_bytes()

    with FileLock(runtime / "locks" / "observer-sources.lock"):
        with pytest.raises(StoreBusyError):
            sources.observe(WORKSPACE, DISCOVERY, FP_B, 20)
        with pytest.raises(StoreBusyError):
            sources.cleanup(10 + HORIZON)
    assert _state(runtime).read_bytes() == before

    assert sources.observe(WORKSPACE, DISCOVERY, FP_B, 20) == _point(FP_B, 20.0, 20.0, 1)


def test_lock_released_after_in_process_failures(runtime):
    lock_path = runtime / "locks" / "observer-sources.lock"
    with pytest.raises(RuntimeError):
        with FileLock(lock_path):
            raise RuntimeError("simulated holder failure")

    def interrupted(path, value):
        raise KeyboardInterrupt

    helper = ObserverNotificationSources(runtime, writer=interrupted)
    with pytest.raises(KeyboardInterrupt):
        helper.observe(WORKSPACE, DISCOVERY, FP_A, 1)
    with FileLock(lock_path):
        pass
    assert not _state(runtime).exists()


def test_lock_released_after_holder_process_exit(runtime, sources):
    lock_path = runtime / "locks" / "observer-sources.lock"
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), env.get("PYTHONPATH")) if part
    )
    code = (
        "import os, sys; from pathlib import Path; "
        "from codex_watchdog.storage import FileLock; "
        "lock = FileLock(Path(sys.argv[1])); lock.__enter__(); os._exit(0)"
    )

    subprocess.run(
        [sys.executable, "-c", code, str(lock_path)], env=env, check=True, timeout=60
    )

    assert sources.observe(WORKSPACE, DISCOVERY, FP_A, 1) == _baseline(FP_A, 1)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda r: Path("relative-runtime"), id="relative"),
        pytest.param(lambda r: r / ".." / r.name, id="parent-component"),
    ],
)
def test_noncanonical_runtime_spelling_rejected(runtime, build):
    with pytest.raises(ValueError):
        ObserverNotificationSources(build(runtime))


def test_missing_or_file_runtime_rejected_and_not_created(tmp_path):
    missing = tmp_path.resolve() / "missing"
    with pytest.raises(ValueError):
        ObserverNotificationSources(missing).observe(WORKSPACE, DISCOVERY, FP_A, 1)
    assert not missing.exists()

    regular = tmp_path.resolve() / "regular"
    regular.write_bytes(b"")
    with pytest.raises(ValueError):
        ObserverNotificationSources(regular).observe(WORKSPACE, DISCOVERY, FP_A, 1)
    assert regular.read_bytes() == b""


def test_redirected_runtime_rejected(tmp_path):
    real = tmp_path.resolve() / "real-runtime"
    (real / "inner").mkdir(parents=True)
    link = tmp_path.resolve() / "linked-runtime"
    _symlink(link, real, directory=True)

    for candidate in (link, link / "inner"):
        helper = ObserverNotificationSources(candidate)
        with pytest.raises(ValueError):
            helper.observe(WORKSPACE, DISCOVERY, FP_A, 1)
        with pytest.raises(ValueError):
            helper.cleanup(HORIZON)
    assert [entry.name for entry in real.iterdir()] == ["inner"]
    assert list((real / "inner").iterdir()) == []


@pytest.mark.parametrize(
    "parts", [("service",), ("service", "observer-sources"), ("locks",)]
)
def test_redirected_standard_directory_rejected(tmp_path, runtime, sources, parts):
    target = tmp_path.resolve() / "elsewhere"
    target.mkdir()
    link = runtime.joinpath(*parts)
    link.parent.mkdir(parents=True, exist_ok=True)
    _symlink(link, target, directory=True)

    with pytest.raises(ValueError):
        sources.observe(WORKSPACE, DISCOVERY, FP_A, 1)
    with pytest.raises(ValueError):
        sources.cleanup(HORIZON)
    assert list(target.iterdir()) == []


def test_redirected_state_file_rejected(tmp_path, runtime, sources):
    target = tmp_path.resolve() / "outside.json"
    target.write_bytes(json.dumps(_document(_scope(WORKSPACE), {})).encode("utf-8"))
    before = target.read_bytes()
    link = _state(runtime)
    link.parent.mkdir(parents=True)
    _symlink(link, target, directory=False)

    with pytest.raises(ValueError):
        sources.observe(WORKSPACE, DISCOVERY, FP_A, 1)
    assert target.read_bytes() == before
    assert link.is_symlink()


def test_redirected_lock_file_rejected(tmp_path, runtime, sources):
    target = tmp_path.resolve() / "outside.lock"
    target.write_bytes(b"")
    lock_path = runtime / "locks" / "observer-sources.lock"
    lock_path.parent.mkdir()
    _symlink(lock_path, target, directory=False)

    with pytest.raises(ValueError):
        sources.observe(WORKSPACE, DISCOVERY, FP_A, 1)
    with pytest.raises(ValueError):
        sources.cleanup(HORIZON)
    assert target.read_bytes() == b""
    assert not _state(runtime).exists()


@pytest.mark.parametrize(
    "observed_at,budget",
    [(True, 16), ("1", 16), (float("nan"), 16), (-1, 16), (1, 0), (1, 65), (1, True), (1, 1.5)],
)
def test_cleanup_validates_time_and_budget(runtime, sources, observed_at, budget):
    with pytest.raises(ValueError):
        sources.cleanup(observed_at, budget=budget)
    assert list(runtime.iterdir()) == []


def test_cleanup_missing_directory_is_noop(runtime, sources):
    assert sources.cleanup(HORIZON) == {"visited": 0, "removed": 0}
    assert not (runtime / "service").exists()


def test_cleanup_horizon_boundary(runtime, sources):
    sources.observe("old", DISCOVERY, FP_A, 100)
    sources.observe("fresh", DISCOVERY, FP_A, 101)
    sources.observe("mixed", DISCOVERY, FP_A, 50)
    sources.observe("mixed", RESUME, FP_A, 101)

    assert sources.cleanup(100 + HORIZON, budget=64) == {"visited": 3, "removed": 1}
    assert not _state(runtime, "old").exists()
    assert _state(runtime, "fresh").exists()
    assert _state(runtime, "mixed").exists()


def test_cleanup_bounded_fair_progress(runtime, sources):
    for index in range(6):
        sources.observe(f"ws-{index}", DISCOVERY, FP_A, 0)
    junk = _sources_dir(runtime) / "notes.txt"
    junk.write_text("keep", encoding="utf-8")

    results = [sources.cleanup(HORIZON, budget=2) for _ in range(4)]

    assert [result["visited"] for result in results] == [2, 2, 2, 1]
    assert sum(result["removed"] for result in results) == 6
    assert [entry.name for entry in _sources_dir(runtime).iterdir()] == ["notes.txt"]
    assert sources.cleanup(HORIZON, budget=2) == {"visited": 1, "removed": 0}


def test_cleanup_preserves_malformed_unknown_and_oversized(runtime, sources):
    old = _point(FP_A, None, 0.0, 0)
    directory = _sources_dir(runtime)
    directory.mkdir(parents=True)
    files = {
        _scope("malformed") + ".json": b"{not json",
        _scope("version") + ".json": json.dumps(
            dict(_document(_scope("version"), {DISCOVERY: old}), schema_version=2)
        ).encode("utf-8"),
        _scope("oversized") + ".json": json.dumps(
            _document(_scope("oversized"), {DISCOVERY: old}, pad="x" * 8192)
        ).encode("utf-8"),
        _scope("empty") + ".json": json.dumps(_document(_scope("empty"), {})).encode(
            "utf-8"
        ),
        _scope("mismatch") + ".json": json.dumps(
            _document(_scope("elsewhere"), {DISCOVERY: old})
        ).encode("utf-8"),
        _scope("extra") + ".json": json.dumps(
            _document(_scope("extra"), {DISCOVERY: old, "extra_slot": old})
        ).encode("utf-8"),
        _scope("partly") + ".json": json.dumps(
            _document(_scope("partly"), {DISCOVERY: old, RESUME: "bad"})
        ).encode("utf-8"),
        _scope("upper").upper() + ".json": json.dumps(
            _document(_scope("upper").upper(), {DISCOVERY: old})
        ).encode("utf-8"),
        "notes.json": json.dumps(_document("notes", {DISCOVERY: old})).encode("utf-8"),
    }
    for name, data in files.items():
        (directory / name).write_bytes(data)

    assert sources.cleanup(10**7, budget=64) == {"visited": len(files), "removed": 0}
    assert {entry.name: entry.read_bytes() for entry in directory.iterdir()} == files


def test_cleanup_preserves_symlinked_state(tmp_path, runtime, sources):
    target = tmp_path.resolve() / "outside.json"
    data = json.dumps(
        _document(_scope(WORKSPACE), {DISCOVERY: _point(FP_A, None, 0.0, 0)})
    ).encode("utf-8")
    target.write_bytes(data)
    link = _state(runtime)
    link.parent.mkdir(parents=True)
    _symlink(link, target, directory=False)

    assert sources.cleanup(10**7, budget=64) == {"visited": 1, "removed": 0}
    assert link.is_symlink()
    assert target.read_bytes() == data


def test_cleanup_deletion_returns_to_unknown_baseline(runtime, sources):
    sources.observe(WORKSPACE, DISCOVERY, FP_A, 0)
    assert sources.observe(WORKSPACE, DISCOVERY, FP_B, 10)["born"] == 10.0

    assert sources.cleanup(10 + HORIZON, budget=64) == {"visited": 1, "removed": 1}
    assert not _state(runtime).exists()

    reopened = sources.observe(WORKSPACE, DISCOVERY, FP_C, 11 + HORIZON)
    assert reopened == _baseline(FP_C, 11 + HORIZON)
    assert sources.observe(WORKSPACE, DISCOVERY, FP_B, 10) is None
    assert sources.observe(WORKSPACE, DISCOVERY, FP_C, 12 + HORIZON)["born"] is None


def test_cleanup_restarts_after_directory_replacement(runtime, sources):
    for index in range(3):
        sources.observe(f"ws-{index}", DISCOVERY, FP_A, 0)
    assert sources.cleanup(0, budget=1) == {"visited": 1, "removed": 0}

    directory = _sources_dir(runtime)
    replaced = runtime / "service" / "replaced"
    try:
        directory.rename(replaced)
    except OSError as exc:
        sources.close()
        pytest.skip(f"directory rename with an open scan is unsupported here: {exc}")
    sources.observe("fresh", DISCOVERY, FP_A, 0)

    assert sources.cleanup(HORIZON, budget=64) == {"visited": 1, "removed": 1}
    assert len(list(replaced.iterdir())) == 3
