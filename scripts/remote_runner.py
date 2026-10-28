#!/usr/bin/env python3
"""Remote runner with atomic status snapshots and independent process-group timeout."""
import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time


class Cancelled(BaseException):
    pass


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(root, state):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=root, prefix='.status-', delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            json.dump(state, handle); handle.write('\n'); handle.flush()
        # Atomic replacement gives readers complete JSON. Shared filesystems may make
        # fsync costly; remote snapshots intentionally do not promise power-loss durability.
        os.replace(temporary, root / 'status.json')
    finally:
        if temporary is not None: temporary.unlink(missing_ok=True)


def terminate_group(child):
    if child is None: return
    try: os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    try: child.wait(timeout=5)
    except subprocess.TimeoutExpired: pass
    # Descendants can outlive the immediate child, so kill the entire group as well.
    try: os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    child.wait()


def main(directory):
    os.umask(0o077)
    root = Path(directory).resolve()
    spec = json.loads((root / 'job.json').read_text())
    with (root / 'runner.lock').open('a+') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 126
        if (root / 'status.json').exists(): return 126
        state = {'state':'STARTING', 'started_at':utcnow(), 'runner_pid':os.getpid()}
        child = None
        watchdog = None
        timed_out = threading.Event()
        cleanup_lock = threading.Lock()
        def cleanup():
            with cleanup_lock:
                terminate_group(child)
        def cancel(signum, _frame):
            # Ignore subsequent termination signals while process-group cleanup completes.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            raise Cancelled(signum)
        signal.signal(signal.SIGTERM, cancel)
        signal.signal(signal.SIGINT, cancel)
        try:
            save(root,state)
            with (root/'stdout.log').open('xb') as out, (root/'stderr.log').open('xb') as err:
                child = subprocess.Popen(spec['argv'],cwd=root,stdout=out,stderr=err,start_new_session=True)
                deadline = time.monotonic()+spec['max_seconds']
                def expire():
                    with cleanup_lock:
                        if child.poll() is None:
                            timed_out.set()
                            terminate_group(child)
                # Storage writes may stall independently of the compute process. The watchdog
                # owns the execution deadline from Popen, never from the next status write.
                watchdog = threading.Timer(spec['max_seconds'],expire)
                watchdog.daemon = True
                watchdog.start()
                state.update(state='RUNNING',pid=child.pid); save(root,state)
                try:
                    code = child.wait(timeout=max(0,deadline-time.monotonic()))
                    if timed_out.is_set():
                        state.update(state='TIMED_OUT',exit_code=124)
                    else:
                        state.update(state='SUCCEEDED' if code == 0 else 'FAILED',exit_code=code)
                except subprocess.TimeoutExpired:
                    timed_out.set(); cleanup()
                    state.update(state='TIMED_OUT',exit_code=124)
        except Cancelled as error:
            cleanup()
            state.update(state='CANCELLED',exit_code=128+error.args[0])
        except BaseException as error:
            cleanup()
            state.update(state='FAILED',error=type(error).__name__,exit_code=125)
        finally:
            if watchdog is not None: watchdog.cancel()
            # A successful parent process must not leave detached descendants consuming resources.
            cleanup()
            state['ended_at']=utcnow(); save(root,state)
        return 0 if state['state']=='SUCCEEDED' else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
