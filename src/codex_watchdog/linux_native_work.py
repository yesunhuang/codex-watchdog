"""Current-work proof from the Linux owner's existing native client only.

No client, session, turn, history reader or sender is created here. The caller
retains its admitted observation and sender fences. The existing control
capability and writer fence are checked again for each correlated native read.
"""
from copy import deepcopy
import math
import time
import uuid

from .control_context import current_control, current_effect
from .control_state import control_node_name
from .native_inflight import NativeInflightError


class LinuxNativeWorkError(NativeInflightError):
    pass


def _turn_id(value):
    try:
        return isinstance(value,str) and str(uuid.UUID(value))==value
    except (ValueError,AttributeError):
        return False


class LinuxNativeWork:
    """Bounded, process-local turn metadata; content stays in its real source.

    ``input_reader`` is optional: it returns the already-owned bounded stream
    producer's tagged proof. NativeInflight validates that proof and correlates
    its exact owner/scope/turn with the actual RPC. Missing input remains unknown.
    """
    def __init__(self, owner, *, clock=time.time):
        self.owner,self.clock=owner,clock
        self._client=None
        self._generation=None
        self._turn=None

    @staticmethod
    def _recovery(client):
        snapshot=getattr(client,"recovery_snapshot",None)
        value=snapshot() if callable(snapshot) else None
        if (not isinstance(value,tuple) or len(value)!=2 or type(value[0]) is not int
                or value[0]<0 or value[1] is not None and not isinstance(value[1],str)):
            raise LinuxNativeWorkError("linux_native_work_observation_unavailable")
        return value

    def _sync(self,client,generation,reason):
        if client is not self._client or generation!=self._generation or reason is not None:
            self._turn=None
        self._client,self._generation=client,generation

    def observe(self,message):
        """Retain only existing reliable same-thread turn IDs, never input text."""
        if not isinstance(message,dict):return
        params=message.get("params")
        if not isinstance(params,dict) or params.get("threadId")!=self.owner.thread:return
        client=self.owner.client
        try:
            generation,reason=self._recovery(client)
        except (ValueError,AttributeError,TypeError):
            self._turn=None
            return
        self._sync(client,generation,reason)
        if reason is not None:return
        method=message.get("method")
        if method=="turn/started":
            turn=params.get("turn")
            self._turn=(dict(id=turn["id"],status=turn["status"])
                if isinstance(turn,dict) and _turn_id(turn.get("id"))
                and turn.get("status")=="inProgress" else None)
        elif method=="turn/completed":
            # A terminal event is not native idle. Another queued turn may be
            # starting; only a correlated live thread/read can prove idle.
            self._turn=None
        elif method=="thread/status/changed":
            status=params.get("status")
            if not isinstance(status,dict) or status.get("type")!="active":
                self._turn=None

    def _owned(self):
        selected=current_control()
        if selected is None:
            raise LinuxNativeWorkError("linux_native_work_owner_capability_required")
        store,token=selected
        binding=self.owner.binding
        workspace=binding.workspace(binding.load())
        if (store.codex_home!=binding.codex_home or store.thread_id!=workspace.session_id
                or store.repo_path!=str(workspace.repo_root) or self.owner.thread!=store.thread_id):
            raise LinuxNativeWorkError("linux_native_work_exact_owner_mismatch")
        value=store.read()
        store._check(value,token)
        client=self.owner.client
        if client is None or self.owner.resumed is not True:
            raise LinuxNativeWorkError("linux_native_work_owned_client_required")
        process=client.process
        poll=getattr(process,"poll",None)
        if (type(process.pid) is not int or process.pid<=0 or not callable(poll)
                or poll() is not None):
            raise LinuxNativeWorkError("linux_native_work_client_not_live")
        from .linux_owner import writer_pid
        if writer_pid(binding.codex_home,store.thread_id)!=process.pid:
            raise LinuxNativeWorkError("linux_native_work_writer_changed")
        if value["owner"].get("boot_id")!=store.boot_id:
            raise LinuxNativeWorkError("linux_native_work_owner_boot_changed")
        stamp=self.clock()
        if type(stamp) not in (int,float) or not math.isfinite(stamp) or stamp<0:
            raise LinuxNativeWorkError("linux_native_work_clock_invalid")
        base=dict(codex_home=str(store.codex_home),thread_id=store.thread_id,
            repo_path=store.repo_path,owner=dict(node=control_node_name(),boot_id=store.boot_id,
                epoch=token["epoch"],instance=token["instance"]),observed_at=stamp)
        return client,workspace,base

    def current_owner(self):
        """Exact currently fenced owner envelope for the existing stream source."""
        if current_control() is None:
            raise LinuxNativeWorkError("linux_native_work_owner_capability_required")
        with current_effect("writer"):
            return self._owned()[2]

    def current_native(self, *, input_reader=None):
        """Read only includeTurns=False on the existing owned client.

        The unmodified RPC response is in-process evidence, not a diagnostic
        export. No current input is manufactured from Q or prior receipts.
        """
        if input_reader is not None and not callable(input_reader):
            raise LinuxNativeWorkError("linux_native_work_input_reader_invalid")
        if current_control() is None:
            raise LinuxNativeWorkError("linux_native_work_owner_capability_required")
        with current_effect("writer"):
            client,workspace,before=self._owned()
            generation,reason=self._recovery(client)
            self._sync(client,generation,reason)
            response=client.request("thread/read",dict(threadId=before["thread_id"],includeTurns=False),timeout=5)
            self.owner._check_thread(response,workspace)
            after_client,_,after=self._owned()
            latest,latest_reason=self._recovery(client)
            if (after_client is not client or after["owner"]!=before["owner"]
                    or latest!=generation):
                self._turn=None
                raise LinuxNativeWorkError("linux_native_work_observation_changed")
            self._sync(client,latest,latest_reason)
            status=response["thread"].get("status")
            idle=isinstance(status,dict) and status.get("type")=="idle"
            if idle:self._turn=None
            proof=dict(after,proof_kind="app_server_rpc",thread_read=response,
                       current_turn=deepcopy(self._turn),current_input=None)
            if not idle and self._turn is not None and input_reader is not None:
                try:proof["active_input_proof"]=input_reader()
                except Exception:
                    proof["input_reason"]="linux_native_work_stream_unavailable"
            # The optional producer ran inside this same owner/writer fence.
            # Reject any replacement rather than exporting its stale token.
            final_client,_,final=self._owned()
            final_generation,_=self._recovery(client)
            if (final_client is not client or final["owner"]!=after["owner"]
                    or final_generation!=latest or self._turn!=proof["current_turn"]):
                self._turn=None
                raise LinuxNativeWorkError("linux_native_work_observation_changed")
            proof["observed_at"]=final["observed_at"]
            return proof

    def collect(self, *, fence, input_reader=None, queue_limit=1024):
        """Compose the actual native-Q collector; no queue writes or dispatch."""
        from .native_inflight import collect_native_work
        binding=self.owner.binding
        workspace=binding.workspace(binding.load())
        return collect_native_work(binding.codex_home,workspace.session_id,workspace.repo_root,
            current_native=lambda:self.current_native(input_reader=input_reader),
            fence=fence,clock=self.clock,queue_limit=queue_limit)
