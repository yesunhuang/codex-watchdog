"""PID-1 supervisor for disposable CI fixtures, never installed or packaged.

Invoke only inside a newly created PID/mount namespace. Product/test processes
run below PID 1 with ordinary unblocked signal defaults; no host settings change.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys


def main():
    assert sys.platform == "linux" and os.getpid() == 1
    assert len(sys.argv) > 1
    target = Path(sys.argv[1]).resolve(strict=True)
    target.relative_to(Path(__file__).resolve().parents[1])
    assert target.is_file()
    child = None

    def forward(signum, frame):
        if child is not None and child.poll() is None:
            # This is our dedicated child group in this private namespace.
            os.killpg(child.pid, signum)

    inherited_mask = signal.pthread_sigmask(signal.SIG_UNBLOCK,
                                          {signal.SIGINT, signal.SIGTERM})
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, forward)
    probe_code = ("import json,os,signal,sys; "
        "print(json.dumps(dict(pid=os.getpid(),term=int(signal.getsignal(signal.SIGTERM)),"
        "mask=sorted(int(s) for s in signal.pthread_sigmask(signal.SIG_BLOCK,set())))),flush=True); "
        "sys.stdin.read()")
    probe = subprocess.Popen([sys.executable, "-c", probe_code], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    observation = json.loads(probe.stdout.readline())
    assert observation["pid"] != 1 and observation["term"] == int(signal.SIG_DFL)
    assert not ({int(signal.SIGTERM), int(signal.SIGINT)} & set(observation["mask"]))
    probe.terminate()  # Only this owned, idle probe; never a product/native job.
    probe.wait(timeout=5)
    probe.stdin.close()
    probe.stdout.close()
    probe.stderr.close()
    assert probe.returncode == -signal.SIGTERM
    print("FIXTURE_NAMESPACE_INIT " + json.dumps(dict(schema_version=1,
        pid_namespace=os.readlink("/proc/self/ns/pid"), init_pid=os.getpid(),
        inherited_mask=sorted(int(s) for s in inherited_mask), probe=observation,
        probe_exit=probe.returncode, scope="owned disposable CI process domain only")),
        file=sys.stderr, flush=True)
    child = subprocess.Popen([sys.executable, str(target), *sys.argv[2:]],
                             start_new_session=True)
    result = child.wait()
    return result if result >= 0 else 128 - result


if __name__ == "__main__":
    raise SystemExit(main())
