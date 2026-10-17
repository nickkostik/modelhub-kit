"""Natural completion of one owned vendor process tree, without app signals."""
from collections import deque
import contextlib
import ctypes
import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from .core import GuardError, WindowsJob


@contextlib.contextmanager
def _follow_signals():
    """Defer observer interruption until the owned operation is terminal."""
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for name in ('SIGINT', 'SIGTERM', 'SIGHUP'):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, lambda *_: None)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _custodian():
    """A separate Linux process adopts only this operation's orphaned children."""
    command = json.loads(sys.stdin.buffer.read(1024 * 1024 + 1))
    if not isinstance(command, list) or not command or any(not isinstance(a, str) for a in command):
        return 125
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER, before spawning.
        return 125
    with _follow_signals():
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
        primary, descendant_failed = None, False
        while True:
            try:
                pid, status = os.waitpid(-1, 0)
            except ChildProcessError:
                break
            except OSError as error:
                if error.errno != errno.EINTR:
                    # Never turn an observation error into permission to leave.
                    time.sleep(0.1)
                continue
            if os.WIFEXITED(status):
                code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                code = -os.WTERMSIG(status)
            else:
                continue
            if pid == child.pid:
                primary = code
                child.returncode = code
            elif code:
                descendant_failed = True
        if primary is None:
            return 125
        return (primary if primary >= 0 else 128 - primary) or int(descendant_failed)


def run(argv, *, env=None, timeout=None):
    """Keep custody until natural tree completion; retain only bounded output."""
    if os.name != 'nt' and not sys.platform.startswith('linux'):
        raise GuardError('vendor-mutation-custody-unavailable')
    if not isinstance(argv, (list, tuple)) or not argv:
        raise GuardError('vendor-command-required')
    command = [str(a) for a in argv]
    payload = json.dumps(command).encode()
    if len(payload) > 1024 * 1024:
        raise GuardError('vendor-command-bound-exceeded')
    job = WindowsJob(kill_on_close=False) if os.name == 'nt' else None
    started = time.monotonic()
    chunks, total, failed = deque(maxlen=16), [0], [False]
    child, reader = None, None
    def drain():
        try:
            while block := child.stdout.read(4096):
                chunks.append(block)
                total[0] += len(block)
        except Exception:
            # No third-party error bodies or thread traceback. Custody remains.
            failed[0] = True

    def close_input():
        if child.stdin is not None and not child.stdin.closed:
            try:
                child.stdin.close()
            except (OSError, ValueError):
                failed[0] = True
    with _follow_signals():
        try:
            if job:
                child = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True,
                    creationflags=subprocess.CREATE_NO_WINDOW | 4)
                try:
                    job.attach_resume(child)
                except Exception:
                    # Assignment failed before the suspended child executed.
                    child.kill()
                    child.wait()
                    raise
            else:
                bootstrap = ('import sys;sys.path.insert(0,sys.argv[1]);'
                             'from modelhub_kit.mutation import _custodian;'
                             'raise SystemExit(_custodian())')
                child = subprocess.Popen([sys.executable, '-I', '-B', '-c', bootstrap,
                    str(Path(__file__).resolve().parents[1])], env=env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    close_fds=True, start_new_session=True)
            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            if not job:
                try:
                    child.stdin.write(payload)
                except (OSError, ValueError):
                    failed[0] = True
                finally:
                    close_input()
            child.wait()  # A parent exit alone is insufficient on Windows.
            if job:
                job.wait_empty()
            reader.join()
            complete = child.returncode >= 0
            return {'exit': child.returncode, 'timedOut': False,
                    'observationExceeded': timeout is not None and time.monotonic() - started > timeout,
                    'treeComplete': complete,
                    'observationFailed': failed[0],
                    'output': b''.join(chunks).decode('utf-8', errors='replace'),
                    'outputTruncated': total[0] > 65536}
        finally:
            if child is not None:
                close_input()  # Even a failed observer write must deliver EOF.
                if reader is None and child.stdout is not None:
                    reader = threading.Thread(target=drain, daemon=True)
                    reader.start()
                child.wait()
                if job:
                    job.wait_empty()
                if reader is not None:
                    reader.join()
                if child.stdout is not None:
                    child.stdout.close()
            if job:
                job.close()
