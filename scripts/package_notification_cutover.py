"""Native exclusion for this test harness's unique, disposable Linux images.

The single harness owns every launcher, uses spawn for its monitor groups, and
never relaunches an old generation during prepare. This is not a fleet utility
or a general OS launch barrier. No process is signaled by this helper.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import time
import uuid


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def resource(path):
    path = Path(path)
    assert path.is_absolute() and path.resolve(strict=True) == path
    value = path.stat()
    assert value.st_ino
    return [value.st_dev, value.st_ino]


def proc_record(pid):
    directory = Path("/proc") / str(pid)
    uid = directory.stat().st_uid
    if uid != os.getuid():
        return None
    raw = (directory / "stat").read_bytes()
    assert len(raw) <= 16384
    fields = raw[raw.rindex(b")") + 2:].split()
    row = dict(pid=pid, uid=uid, state=fields[0].decode("ascii"),
               ppid=int(fields[1]), group=int(fields[2]), session=int(fields[3]),
               flags=int(fields[6]), start_ticks=int(fields[19]))
    row["nonexecuting"] = row["state"] in ("Z", "X")
    row["kernel_thread"] = bool(row["flags"] & 0x00200000)
    if not row["nonexecuting"] and not row["kernel_thread"]:
        value = (directory / "exe").stat()
        row["image"] = [value.st_dev, value.st_ino]
    return row


def claims(runtime):
    path = runtime / "notifications/recent-notification-claims.sqlite3"
    if not path.exists():
        return []
    assert path.resolve(strict=True) == path and path.stat().st_nlink == 1
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        values = db.execute("SELECT scope,fingerprint,born,expires FROM claims "
                            "ORDER BY scope,fingerprint LIMIT 4097").fetchall()
    assert len(values) <= 4096, "unexpected fixture history; do not omit claims"
    return [list(row) for row in values]


class FixtureSenders:
    def __init__(self, fixture_root, image_paths):
        assert sys.platform == "linux"
        self.root = Path(fixture_root).resolve(strict=True)
        self.images, self.groups, self.processes = {}, set(), []
        self.phase = "initial"
        for path in image_paths:
            self.remember_image(path)

    def owned(self, path):
        path = Path(path)
        assert path.is_absolute() and path.resolve(strict=True) == path
        path.relative_to(self.root)
        return path

    def remember_image(self, path):
        assert self.phase in ("initial", "ready")
        path = self.owned(path)
        value = path.stat()
        assert stat.S_ISREG(value.st_mode) and value.st_nlink == 1
        identity = tuple(resource(path))
        self.images[identity] = dict(path=str(path), identity=list(identity),
                                     sha256=digest(path.read_bytes()))

    def register(self, process):
        assert self.phase in ("initial", "ready"), "fixture launch gate is closed"
        # Popen(start_new_session=True) establishes this group before returning.
        self.groups.add(process.pid)
        try:
            row = proc_record(process.pid)
        except FileNotFoundError:
            assert process.poll() is not None, "unclassified registered process"
            row = dict(pid=process.pid, group=process.pid, exited_before_capture=True)
        if row and not row.get("exited_before_capture"):
            assert row["group"] == row["session"] == process.pid
        assert row is not None
        self.processes.append((process, row))

    def spawn(self, arguments, **kwargs):
        assert self.phase in ("initial", "ready"), "fixture launch gate is closed"
        assert kwargs.get("start_new_session", True) is True
        kwargs["start_new_session"] = True
        process = subprocess.Popen(arguments, **kwargs)
        self.register(process)
        return process

    def census(self, extra_groups=()):
        groups = self.groups | set(extra_groups)
        records, vanished, matching = [], [], []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdecimal():
                continue
            pid = int(entry.name)
            try:
                row = proc_record(pid)
            except FileNotFoundError:
                # A live same-UID process with no classifiable image is unknown.
                if entry.exists():
                    try:
                        check = proc_record(pid)
                    except FileNotFoundError:
                        assert not entry.exists(), "live fixture census image unavailable"
                        check = None
                    if check is not None:
                        row = check
                    else:
                        vanished.append(pid)
                        continue
                else:
                    vanished.append(pid)
                    continue
            if row is None:
                continue
            records.append(row)
            relevant = row["group"] in groups or tuple(row.get("image", ())) in self.images
            if relevant:
                matching.append(row)
        live = [r for r in matching if not r["nonexecuting"] and not r["kernel_thread"]]
        return dict(observed_at=time.time(), uid=os.getuid(), inspected=records,
                    vanished=vanished, matching=matching, live=live,
                    registered=[row for _, row in self.processes],
                    groups=sorted(groups), images=list(self.images.values()))

    def prepare(self, executable, runtime, codex_home, workspace_id, thread_id,
                environment, cwd, evidence_dir, protected_files=()):
        assert self.phase == "initial", "one explicit preparation per fixture"
        executable, runtime = self.owned(executable), self.owned(runtime)
        assert tuple(resource(executable)) in self.images
        assert str(uuid.UUID(thread_id)) == thread_id
        assert workspace_id == "package-fixture"
        evidence_dir = Path(evidence_dir)
        assert evidence_dir.parent.resolve(strict=True).is_relative_to(self.root)
        evidence_dir.mkdir(mode=0o700, exist_ok=False)
        self.phase = "preparing"  # No old generation can be launched by this harness.
        try:
            assert all(p.poll() is not None for p, _ in self.processes), "old parent still live"
            native = self.census()
            assert not native["live"], ("old fixture sender/backend remains", native["live"])
            scope = digest(workspace_id.encode())
            kinds = ("linux_owner_attention", "linux_owner_recovered")
            keys = sorted(digest(canonical(dict(workspace_id=workspace_id, event_type=k))) for k in kinds)
            before_claims = claims(runtime)
            assert {r[0] for r in before_claims} <= set(keys), "unprojected fixture notification"
            protected = {str(self.owned(p)): digest(Path(p).read_bytes()) for p in protected_files}
            identity = resource(runtime)
            request = dict(schema_version=1, notification_runtime=str(runtime),
                notification_runtime_id=identity, sender_runtimes=[dict(path=str(runtime), identity=identity)],
                projection=dict(scopes=sorted([[thread_id, scope, p] for p in
                    ("slack", "lark", "smtp", "windows_msg")]), current_keys=keys, contexts=[], current_pins=[]))
            proof = dict(schema_version=1, purpose="notification-root-cutover",
                projection_sha256=digest(canonical(request)), sender_runtimes=request["sender_runtimes"],
                observed_at=native["observed_at"], native_source="linux-process-census",
                native_evidence_sha256=digest(canonical(native)), all_notification_senders_stopped=True,
                all_notification_subprocesses_stopped=True, old_sender_restart_allowed=False)
            def save(name, value):
                path = evidence_dir / name
                with path.open("xb") as stream:
                    stream.write(canonical(value) + b"\n")
                return path
            save("native-before.json", native)
            projection_path = save("projection.json", request)
            proof_path = save("quiescence.json", proof)
            assert resource(runtime) == identity and 0 <= time.time() - native["observed_at"] < 30
            arguments = [str(executable), "--runtime", str(runtime), "--codex-home", str(codex_home),
                         "notification-retention-prepare", "--projection", str(projection_path),
                         "--quiescence", str(proof_path)]
            # Only this new offline command is permitted through the launch gate.
            cold = subprocess.Popen(arguments, env=environment, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
            stdout, stderr = cold.communicate(timeout=30)
            save("command.json", dict(returncode=cold.returncode, stdout=stdout, stderr=stderr,
                                      pid=cold.pid, group=cold.pid))
            after = self.census(extra_groups=(cold.pid,))
            save("native-after.json", after)
            assert not after["live"], "fixture process survived offline command"
            assert cold.returncode == 0, (stdout, stderr)
            result = json.loads(stdout)
            assert result["status"] == "ready"
            assert result["proof_scope"] == "caller_native_receipt_not_independent_census"
            assert resource(runtime) == identity
            after_claims = claims(runtime)
            assert after_claims == before_claims, "offline preparation changed original claim/birth tuples"
            assert all(digest(Path(p).read_bytes()) == value for p, value in protected.items())
            evidence = dict(native_before=native, native_after=after, projection=request, quiescence=proof,
                            result=result, claims_before=before_claims, claims_after=after_claims,
                            protected_sha256=protected)
            save("complete.json", evidence)
            raw = canonical(evidence)
            assert len(raw) <= 2 * 1024 * 1024
            print("FIXTURE_NOTIFICATION_COLD_EVIDENCE " + raw.decode(), file=sys.stderr, flush=True)
            self.phase = "ready"
            return dict(status="ready", proof_scope=result["proof_scope"],
                        native_evidence_sha256=proof["native_evidence_sha256"],
                        claims_preserved=True, protected_bytes_unchanged=True)
        except BaseException:
            self.phase = "failed"  # Never relaunch an old sender or automatically retry.
            raise
