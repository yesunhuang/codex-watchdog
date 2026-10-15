"""Current work from a bounded, owner-continuous native rollout stream.

This is observed stream evidence, never a fabricated thread/read response.
First observation/restart or a continuity gap is UNKNOWN. Only a new observed
start, exact same-turn input and matching terminal can prove a full lifecycle.
EOF alone, old terminal history, missing Q and saved receipts cannot do so.
The caller retains real current native-owner/observation/sender fences. No
native client, history walk, queue write, activity renewal or RPC is performed.
"""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat

from .native_inflight import (NativeInflightError, PROOF_SECONDS, _scope, _owner,
                              _number, _uuid, _digest, _json, _sha, _load, _parts)
from .native_rollout import find_native_rollout


BYTE_BUDGET = 256 * 1024
RECORD_BUDGET = 512
ANCHOR_BYTES = 4096
KIND = "native_rollout_stream_work"
PROOF_KIND = "native_rollout_stream"


def _clock():
    from datetime import timezone
    return datetime.now(timezone.utc).timestamp()


def _repository_spelling(repository):
    """Recognize only standard extended Windows drive/UNC anchor spellings."""
    if not isinstance(repository,PureWindowsPath):
        return repository
    if ".." in repository.parts or repository.drive.startswith("\\\\.\\"):
        return None
    if not repository.drive.startswith("\\\\?\\"):
        return repository
    # Python 3.9 recognizes extended UNC anchors only with an uppercase tag.
    # Normalize that tag for parsing; the caller still verifies the original
    # directory with strict resolution and samefile.
    spelling=str(repository)
    if spelling[:8].lower()=="\\\\?\\unc\\":
        repository=PureWindowsPath("\\\\?\\UNC\\"+spelling[8:])
    if repository.root!="\\":
        return None
    drive=re.fullmatch(r"\\\\\?\\([A-Za-z]:)",repository.drive)
    unc=re.fullmatch(r'\\\\\?\\UNC\\([^\\/:<>"|?*\x00-\x1f]+)\\([^\\/:<>"|?*\x00-\x1f]+)',
                     repository.drive,flags=re.IGNORECASE)
    if drive is not None:
        anchor=drive[1]+"\\"
    elif unc is not None and all(part not in (".","..") for part in unc.groups()):
        anchor="\\\\"+unc[1]+"\\"+unc[2]+"\\"
    else:
        return None  # No GLOBALROOT, device or volume-namespace fallback.
    return Path(str(PureWindowsPath(anchor,*repository.parts[1:])))


def stream_exact_thread(home, thread, repo):
    """Indexed exact identity only; missing rollout is not missing native Q.

    This independent identity check permits positive Q registration while the
    optional stream is unknown. It cannot prove a current turn/completion.
    """
    try:
        scope = _scope(home,thread,repo)
        path = Path(home)/"state_5.sqlite"
        before = path.stat()
        if path.is_symlink() or path.resolve()!=path or not stat.S_ISREG(before.st_mode):
            return False
        for suffix in ("-wal","-shm"):
            side=Path(str(path)+suffix)
            if ((side.exists() or side.is_symlink()) and (side.is_symlink() or side.resolve()!=side
                    or not stat.S_ISREG(side.lstat().st_mode))):
                return False
        query=("SELECT CASE WHEN length(CAST(id AS BLOB))=36 THEN id END,"
               "CASE WHEN length(CAST(cwd AS BLOB))<=4096 THEN cwd END,"
               "CASE WHEN length(CAST(source AS BLOB))<=32 THEN source END,"
               "CASE WHEN length(CAST(thread_source AS BLOB))<=32 THEN thread_source END,archived "
               "FROM threads WHERE id=? LIMIT 2")
        with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=0.1)) as db:
            steps=[0]
            def bounded():
                steps[0]+=100
                return steps[0]>10000
            db.set_progress_handler(bounded,100)
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            if db.execute("SELECT type FROM sqlite_master WHERE name='threads'").fetchmany(2)!=[("table",)]:
                return False
            plan=db.execute("EXPLAIN QUERY PLAN "+query,(thread,)).fetchmany(8)
            if len(plan)!=1 or not plan[0][3].startswith(("SEARCH threads ","SEARCH TABLE threads ")) or "(id=?)" not in plan[0][3]:
                return False
            rows=db.execute(query,(thread,)).fetchmany(2)
        after=path.stat()
        if (len(rows)!=1 or rows[0][0]!=thread or not isinstance(rows[0][1],str)
                or rows[0][2:4]!=("vscode","user") or type(rows[0][4]) is not int or rows[0][4]!=0):
            return False
        repository=Path(rows[0][1])
        spelling=_repository_spelling(repository)
        if spelling is None or not spelling.is_absolute() or not repository.is_dir():
            return False
        resolved=repository.resolve(strict=True)
        expected=Path(scope["repo_path"])
        # Native Path equality handles Windows case/separators. The resource
        # check also distinguishes Repo/repo in a case-sensitive directory.
        return (resolved==spelling and resolved==expected and repository.samefile(expected)
                and (before.st_dev,before.st_ino)==(after.st_dev,after.st_ino))
    except (OSError,ValueError,TypeError,RuntimeError,sqlite3.Error):
        return False


def _owner_proof(value, scope, current):
    if (not isinstance(value,dict) or any(value.get(k)!=v for k,v in scope.items())
            or not _number(value.get("observed_at")) or not 0<=current-value["observed_at"]<=PROOF_SECONDS):
        raise NativeInflightError("native_stream_owner_unverified")
    return _owner(value.get("owner"))


def _stamp(value):
    if not isinstance(value,str):
        return None
    try:
        value=re.sub(r"(\.\d{6})\d+(?=Z|[+-]\d\d:\d\d$)",r"\1",value)
        parsed=datetime.fromisoformat(value.replace("Z","+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError,OverflowError):
        return None


def _unknown(reason):
    return dict(status="unknown",turn_id=None,instruction_id=None,prompt_sha256=None,
                input_sha256=None,evidence_kind=PROOF_KIND,
                reason="native_inflight_current_turn_unknown",stream_reason=reason)


def _anchor(handle, offset):
    handle.seek(max(0,offset-ANCHOR_BYTES))
    return hashlib.sha256(handle.read(min(offset,ANCHOR_BYTES))).hexdigest()


def _event(state, event, baseline, current):
    """Reduce only newly read complete records; one current lifecycle at most."""
    if not isinstance(event,dict):
        return None,"malformed_record"
    if event.get("type")!="event_msg":
        return state,None
    payload=event.get("payload")
    if not isinstance(payload,dict):
        return None,"malformed_record"
    kind=payload.get("type")
    if kind not in ("task_started","task_complete","turn_aborted","item_completed","user_message"):
        return state,None
    if kind=="item_completed" and (not isinstance(payload.get("item"),dict) or payload["item"].get("type")!="UserMessage"):
        return state,None
    # Native display-style user_message may omit turn_id. It cannot establish
    # an input identity and must not erase a later explicit same-turn item.
    if kind=="user_message" and not _uuid(payload.get("turn_id")):
        return state,None
    stamp,turn=_stamp(event.get("timestamp")),payload.get("turn_id")
    if not _number(stamp) or not baseline<=stamp<=current or not _uuid(turn):
        return None,"unobserved_or_invalid_lifecycle"
    if kind=="task_started":
        if state is not None and state["turn_id"]==turn:
            if state["started_at"]==stamp and state.get("completed_at") is None:
                return state,None
            return None,"lifecycle_order_unknown"
        if state is not None and state.get("completed_at") is not None and stamp<state["completed_at"]:
            return None,"lifecycle_order_unknown"
        gap=state is not None and state.get("completed_at") is None
        return dict(turn_id=turn,started_at=stamp,input_at=None,input=None,completed_at=None,
                    terminal=None,tainted=gap),"lifecycle_order_unknown" if gap else None
    if state is None or state["turn_id"]!=turn or stamp<state["started_at"]:
        return None,"unmatched_lifecycle"
    if kind in ("task_complete","turn_aborted"):
        if state.get("input") is None or state.get("input_at") is None or stamp<state["input_at"]:
            return None,"unmatched_terminal"
        if state.get("completed_at") is not None:
            return (state,None) if state["completed_at"]==stamp and state["terminal"]==kind else (None,"lifecycle_order_unknown")
        return dict(state,completed_at=stamp,terminal=kind),None
    if state.get("completed_at") is not None:
        return None,"input_after_terminal"
    try:
        if kind=="item_completed":
            identity=_parts(payload["item"].get("content"),terminal_lf=True)
        else:
            identity=_parts([dict(type="text",text=payload.get("message"))],terminal_lf=True)
    except NativeInflightError:
        return None,"input_unverified"
    if state.get("input") is not None and state["input"]!=identity:
        return None,"input_changed"
    return dict(state,input=identity,input_at=stamp if state.get("input_at") is None else state["input_at"]),None


class NativeStreamWork:
    """Process-local current cursor; restart cannot bootstrap historical idle.

    ``current_owner`` supplies actual fresh exact owner metadata under ``fence``.
    When used inside collect_native_work's already-held nonreentrant guard,
    pass an explicit held-fence/nullcontext adapter, not a second owner lock.
    ``forget`` retires only this optional cursor, never native/receipt state.
    """
    _kind = KIND
    _proof_kind = PROOF_KIND
    _owner_proof = staticmethod(_owner_proof)

    def _unknown(self, reason):
        return dict(_unknown(reason), evidence_kind=self._proof_kind)

    def __init__(self,codex_home,*,clock=None):
        self.home=Path(codex_home).resolve()
        self.clock=_clock if clock is None else clock
        self._slots={}

    def forget(self,thread_id):
        self._slots.pop(thread_id,None)

    def observe(self,thread_id,repo_path,*,current_owner,fence):
        scope=_scope(self.home,thread_id,Path(repo_path))
        now=self.clock()
        if not _number(now) or not callable(current_owner) or not callable(fence):
            raise NativeInflightError("native_stream_arguments_invalid")
        result=dict(schema_version=1,kind=self._kind,proof_kind=self._proof_kind,**scope,
                    observed_at=now,owner=None,native=self._unknown("not_observed"),stream=None)
        prior=self._slots.get(thread_id)
        try:
            with fence():
                owner_raw=current_owner()
                now=self.clock()
                owner=self._owner_proof(owner_raw,scope,now)
                result["owner"]=owner
                if not stream_exact_thread(self.home,thread_id,Path(repo_path)):
                    raise NativeInflightError("native_stream_exact_thread_unavailable")
                path=find_native_rollout(self.home,thread_id,Path(repo_path))
                if path is None:
                    raise NativeInflightError("native_stream_path_unavailable")
                info=path.stat()
                key=(str(path),info.st_dev,info.st_ino)
                with path.open("rb") as handle:
                    if prior is not None and now<prior["checked_at"]:
                        raise NativeInflightError("native_stream_clock_regressed")
                    continuous=(prior is not None and prior["key"]==key and prior["owner"]==owner
                        and prior["repo_path"]==scope["repo_path"] and 0<=info.st_size-prior["offset"]<=BYTE_BUDGET
                        and info.st_size>=prior["size"] and (info.st_size!=prior["size"] or info.st_mtime_ns==prior["mtime_ns"])
                        and _anchor(handle,prior["offset"])==prior["boundary_sha256"])
                    if not continuous:
                        offset=info.st_size
                        slot=dict(key=key,owner=owner,repo_path=scope["repo_path"],offset=offset,
                                  boundary_sha256=_anchor(handle,offset),baseline_at=now,checked_at=now,lifecycle=None)
                        reason="baseline_unknown"
                        complete=False
                    else:
                        offset=prior["offset"]
                        handle.seek(offset)
                        raw=handle.read(info.st_size-offset)
                        lines=raw.splitlines(keepends=True)
                        if len(lines)>RECORD_BUDGET:
                            slot=dict(prior,offset=info.st_size,boundary_sha256=_anchor(handle,info.st_size),
                                      baseline_at=now,checked_at=now,lifecycle=None)
                            reason,complete="record_budget_unknown",False
                        else:
                            complete=not lines or lines[-1].endswith(b"\n")
                            if lines and not complete:
                                lines.pop()
                            lifecycle=deepcopy(prior["lifecycle"])
                            reason=None
                            for line in lines:
                                try:
                                    event=_load(line)
                                    if event.get("type")=="session_meta":
                                        meta=event.get("payload")
                                        if not isinstance(meta,dict) or meta.get("id")!=thread_id or meta.get("cwd")!=scope["repo_path"]:
                                            raise NativeInflightError("native_stream_scope_mismatch")
                                    payload=event.get("payload")
                                    if isinstance(payload,dict) and "thread_id" in payload and payload["thread_id"]!=thread_id:
                                        raise NativeInflightError("native_stream_scope_mismatch")
                                    lifecycle,error=_event(lifecycle,event,prior["baseline_at"],self.clock())
                                    if error is not None:
                                        reason=error
                                except (NativeInflightError,ValueError,TypeError,AttributeError) as error:
                                    if isinstance(error,NativeInflightError) and str(error)=="native_stream_scope_mismatch":
                                        raise
                                    lifecycle,reason=None,"malformed_or_foreign_record"
                                offset+=len(line)
                            slot=dict(prior,offset=offset,boundary_sha256=_anchor(handle,offset),checked_at=now,lifecycle=lifecycle)
                            complete=complete and offset==info.st_size
                    slot.update(size=info.st_size,mtime_ns=info.st_mtime_ns)
                    after=path.stat()
                    if ((after.st_dev,after.st_ino)!=(info.st_dev,info.st_ino) or after.st_size!=info.st_size
                            or after.st_mtime_ns!=info.st_mtime_ns or _anchor(handle,slot["offset"])!=slot["boundary_sha256"]):
                        complete=False
                        reason="stream_changed_during_read"
                    read_at=self.clock()
                owner_after=self._owner_proof(current_owner(),scope,self.clock())
                if (owner_after!=owner or not stream_exact_thread(self.home,thread_id,Path(repo_path))
                        or find_native_rollout(self.home,thread_id,Path(repo_path))!=path):
                    raise NativeInflightError("native_stream_owner_changed")
                self._slots[thread_id]=slot
                result["observed_at"]=read_at
                result["stream"]=dict(path=key[0],device=key[1],inode=key[2],offset=slot["offset"],
                    boundary_sha256=slot["boundary_sha256"],baseline_at=slot["baseline_at"],
                    continuous=continuous,complete=complete,bytes_read=0 if not continuous else info.st_size-prior["offset"])
                lifecycle=slot["lifecycle"]
                if complete and continuous and lifecycle is not None and lifecycle["input"] is not None and not lifecycle["tainted"]:
                    native=dict(status="idle" if lifecycle["completed_at"] is not None else "active",
                        turn_id=None if lifecycle["completed_at"] is not None else lifecycle["turn_id"],
                        instruction_id=None,prompt_sha256=None,input_sha256=None,evidence_kind=self._proof_kind,
                        lifecycle=dict(turn_id=lifecycle["turn_id"],started_at=lifecycle["started_at"],
                            input_at=lifecycle["input_at"],completed_at=lifecycle["completed_at"],terminal=lifecycle["terminal"],
                            input=deepcopy(lifecycle["input"])))
                    if native["status"]=="active":
                        native.update(lifecycle["input"])
                    result["native"]=native
                else:
                    result["native"]=self._unknown(reason or "lifecycle_unobserved")
        except (OSError,ValueError,TypeError,sqlite3.Error) as error:
            self._slots.pop(thread_id,None)
            result["native"]=self._unknown(str(error) if isinstance(error,NativeInflightError) else "stream_unavailable")
        result["proof_sha256"]=_sha(_json(result))
        return result


def validate_stream_work(proof,scope,current):
    """Interpret the tagged fresh observed lifecycle, not a thread/read RPC."""
    return _validate_stream_work(proof, scope, current, kind=KIND,
                                proof_kind=PROOF_KIND, owner_validator=_owner)


def _validate_stream_work(proof,scope,current,*,kind,proof_kind,owner_validator):
    """Shared lifecycle checks; each public proof type fixes its own subject."""
    if (not isinstance(proof,dict) or type(proof.get("schema_version")) is not int or proof["schema_version"]!=1
            or proof.get("kind")!=kind or proof.get("proof_kind")!=proof_kind
            or "thread_read" in proof or "current_turn" in proof
            or any(proof.get(k)!=v for k,v in scope.items()) or not _number(proof.get("observed_at"))
            or not 0<=current-proof["observed_at"]<=PROOF_SECONDS or len(_json(proof).encode())>16384
            or not _digest(proof.get("proof_sha256"))
            or proof["proof_sha256"]!=_sha(_json({k:v for k,v in proof.items() if k!="proof_sha256"}))):
        raise NativeInflightError("native_stream_proof_invalid")
    owner=owner_validator(proof.get("owner"))
    native=proof.get("native")
    if not isinstance(native,dict) or native.get("evidence_kind")!=proof_kind or native.get("status") not in ("unknown","active","idle"):
        raise NativeInflightError("native_stream_proof_invalid")
    if native["status"]=="unknown":
        if native.get("reason")!="native_inflight_current_turn_unknown" or any(native.get(k) is not None for k in ("turn_id","instruction_id","prompt_sha256","input_sha256")):
            raise NativeInflightError("native_stream_proof_invalid")
        value=deepcopy(native)
        value.pop("stream_reason",None)  # Diagnostics are not native work identity.
        return owner,value
    stream,lifecycle=proof.get("stream"),native.get("lifecycle")
    if (not isinstance(stream,dict) or stream.get("continuous") is not True or stream.get("complete") is not True
            or not _number(stream.get("baseline_at")) or stream["baseline_at"]>current
            or not isinstance(stream.get("path"),str) or len(stream["path"])>4096
            or not Path(stream["path"]).is_absolute() or type(stream.get("offset")) is not int or stream["offset"]<0
            or type(stream.get("device")) is not int or type(stream.get("inode")) is not int
            or not _digest(stream.get("boundary_sha256")) or not isinstance(lifecycle,dict)
            or not _uuid(lifecycle.get("turn_id")) or not _number(lifecycle.get("started_at"))
            or not _number(lifecycle.get("input_at")) or not stream["baseline_at"]<=lifecycle["started_at"]<=lifecycle["input_at"]<=proof["observed_at"]):
        raise NativeInflightError("native_stream_proof_invalid")
    identity=lifecycle.get("input")
    if (not isinstance(identity,dict) or not _digest(identity.get("input_sha256"))
            or (identity.get("instruction_id") is None)!=(identity.get("prompt_sha256") is None)
            or identity.get("instruction_id") is not None and (re.fullmatch(r"[A-Za-z0-9._:/+-]{1,512}",str(identity["instruction_id"])) is None or not _digest(identity["prompt_sha256"]))):
        raise NativeInflightError("native_stream_proof_invalid")
    if native["status"]=="active":
        if (native.get("turn_id")!=lifecycle["turn_id"] or lifecycle.get("completed_at") is not None or lifecycle.get("terminal") is not None
                or any(native.get(k)!=v for k,v in identity.items())):
            raise NativeInflightError("native_stream_proof_invalid")
    elif (not _number(lifecycle.get("completed_at")) or not lifecycle["input_at"]<=lifecycle["completed_at"]<=proof["observed_at"]
            or lifecycle.get("terminal") not in ("task_complete","turn_aborted")
            or any(native.get(k) is not None for k in ("turn_id","instruction_id","prompt_sha256","input_sha256"))):
        raise NativeInflightError("native_stream_proof_invalid")
    return owner,deepcopy(native)
