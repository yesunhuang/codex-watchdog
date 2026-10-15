"""An explicit local human re-enable intent; no Codex wake or authority edit."""
import math
import json
from pathlib import Path
import uuid

from .storage import FileLock, InstructionStore


def _path(runtime,session_id):
    root=Path(runtime).resolve()
    session=str(uuid.UUID(session_id))
    path=root/'activation-requests'/(session+'.json')
    if path.is_symlink() or path.parent.resolve()!=path.parent:
        raise ValueError('activation_request_path_invalid')
    return path,session


def request_activation(runtime,session_id,*,requested_at):
    path,session=_path(runtime,session_id)
    if type(requested_at) not in (int,float) or not math.isfinite(requested_at) or requested_at<0:
        raise ValueError('activation_request_clock_invalid')
    with FileLock(Path(runtime)/'locks'/('activation-'+session+'.lock')):
        InstructionStore._atomic_json(path,dict(schema_version=1,thread_id=session,requested_at=requested_at))
    return dict(status='requested',thread_id=session,native_wake=False)


def requested_activity(runtime,session_id,*,now):
    path,session=_path(runtime,session_id)
    if not path.exists():return None
    with path.open('rb') as stream: raw=stream.read(4097)
    if len(raw)>4096:return None
    value=json.loads(raw)
    if not isinstance(value,dict):return None
    stamp=value.get('requested_at')
    if (value.get('schema_version')!=1 or value.get('thread_id')!=session
            or type(stamp) not in (int,float) or not math.isfinite(stamp)
            or not 0<=stamp<=now or now-stamp>=72*3600):
        return None
    return stamp  # immutable intent; repeated reads never advance it
