"""Small standard-library primitives. Runtime state never belongs in Git."""
import contextlib
import datetime
import gzip
import hashlib
import io
import json
import os
import pathlib
import platform
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
import urllib.error
import urllib.parse
import urllib.request


class GuardError(Exception):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def locations():
    home = pathlib.Path.home()
    if os.name == 'nt':
        root = pathlib.Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / 'modelhub-kit'
        config, state = root / 'config.json', root / 'state'
    else:
        config = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'modelhub-kit/config.json'
        state = pathlib.Path(os.environ.get('XDG_STATE_HOME', home / '.local/state')) / 'modelhub-kit'
    return home, config, pathlib.Path(os.environ.get('MODELHUB_KIT_HOME', state))


def protected(path, directory=False):
    path = pathlib.Path(path).absolute()
    info = path.lstat()
    if path.is_symlink() or path.resolve() != path or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise GuardError('untrusted-path')
    if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise GuardError('unowned-or-writable-path')
    if not directory and info.st_nlink != 1:
        raise GuardError('linked-state-file')
    return info


def private_directory(path):
    path = pathlib.Path(path).absolute()
    missing = []
    parent = path
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    # The first existing ancestor must be canonical; never follow a state symlink.
    if parent.is_symlink() or parent.resolve() != parent:
        raise GuardError('symlink-state-ancestor')
    for item in reversed(missing):
        item.mkdir(mode=0o700)
    protected(path, True)
    return path


def read_json(path, default=None, limit=8 * 1024 * 1024):
    path = pathlib.Path(path)
    if not path.exists():
        return default
    info = protected(path)
    if info.st_size > limit:
        raise GuardError('oversized-json')
    return json.loads(path.read_text(encoding='utf-8'))


_UNSPECIFIED = object()


def atomic_text(path, text, expected=_UNSPECIFIED):
    path = pathlib.Path(path)
    private_directory(path.parent)
    before = None
    if path.exists() or path.is_symlink():
        info = protected(path)
        before = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        current_text = path.read_text(encoding='utf-8')
        if expected is not _UNSPECIFIED and current_text != expected:
            raise GuardError('concurrent-file-change')
        if current_text == text:
            return False
    elif expected is not _UNSPECIFIED and expected not in ('', None):
        raise GuardError('concurrent-file-change')
    descriptor, temporary = tempfile.mkstemp(prefix='.prepare-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        current = None
        if path.exists() or path.is_symlink():
            info = protected(path)
            current = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
        if current != before:
            raise GuardError('concurrent-file-change')
        os.replace(temporary, path)
        return True
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


def atomic_json(path, value):
    return atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + '\n')


@contextlib.contextmanager
def writer(state):
    root = private_directory(state)
    lock = root / 'writer.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        protected(lock)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'0')
            os.lseek(fd, 0, 0)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise GuardError('writer-busy') from error
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise GuardError('writer-busy') from error
        yield
    finally:
        os.close(fd)


class WindowsJob:
    """Own only the maintenance child tree, attached before its first instruction."""
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.api = c.WinDLL('kernel32', use_last_error=True)
        class Basic(c.Structure):
            _fields_ = [('user', c.c_longlong), ('job', c.c_longlong), ('flags', w.DWORD),
                        ('minimum', c.c_size_t), ('maximum', c.c_size_t), ('active', w.DWORD),
                        ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Limits(c.Structure):
            _fields_ = [('basic', Basic), ('io', c.c_ulonglong * 6), ('memory', c.c_size_t),
                        ('jobMemory', c.c_size_t), ('peak', c.c_size_t), ('jobPeak', c.c_size_t)]
        class Thread(c.Structure):
            _fields_ = [('size', w.DWORD), ('usage', w.DWORD), ('id', w.DWORD), ('owner', w.DWORD),
                        ('priority', w.LONG), ('delta', w.LONG), ('flags', w.DWORD)]
        self.Thread = Thread
        signatures = {
            'CreateJobObjectW': ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'QueryInformationJobObject': ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.c_void_p], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
            'CreateToolhelp32Snapshot': ([w.DWORD, w.DWORD], w.HANDLE),
            'Thread32First': ([w.HANDLE, c.POINTER(Thread)], w.BOOL),
            'Thread32Next': ([w.HANDLE, c.POINTER(Thread)], w.BOOL),
            'OpenThread': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'ResumeThread': ([w.HANDLE], w.DWORD),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.api.CreateJobObjectW(None, None)
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.handle or not self.api.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
            self.close()
            raise GuardError('maintenance-job-unavailable')

    def attach_resume(self, child):
        c, api = self.c, self.api
        if not api.AssignProcessToJobObject(self.handle, int(child._handle)):
            raise GuardError('maintenance-child-job-assignment-failed')
        snapshot = api.CreateToolhelp32Snapshot(4, 0)
        if snapshot == c.c_void_p(-1).value:
            raise GuardError('maintenance-thread-unavailable')
        try:
            entry = self.Thread()
            entry.size = c.sizeof(entry)
            found = api.Thread32First(snapshot, c.byref(entry))
            while found:
                if entry.owner == child.pid:
                    thread = api.OpenThread(2, False, entry.id)
                    if not thread:
                        raise GuardError('maintenance-thread-unavailable')
                    try:
                        if api.ResumeThread(thread) == 0xffffffff:
                            raise GuardError('maintenance-thread-resume-failed')
                        return
                    finally:
                        api.CloseHandle(thread)
                found = api.Thread32Next(snapshot, c.byref(entry))
            raise GuardError('maintenance-thread-unavailable')
        finally:
            api.CloseHandle(snapshot)

    def finish(self):
        if self.handle:
            self.api.TerminateJobObject(self.handle, 1)
            # Four LARGE_INTEGER time counters followed by four DWORD counters.
            counters = (self.c.c_ubyte * 48)()
            for _ in range(100):
                if not self.api.QueryInformationJobObject(self.handle, 1, counters, 48, None):
                    raise GuardError('maintenance-job-state-unavailable')
                if int.from_bytes(bytes(counters)[40:44], 'little') == 0:
                    return
                time.sleep(0.05)
            raise GuardError('maintenance-child-tree-still-exiting')

    def close(self):
        if getattr(self, 'handle', None):
            self.api.CloseHandle(self.handle)
            self.handle = None


def run(argv, *, env=None, timeout=60):
    """Bound our own child; never search for or terminate other applications."""
    chunks = deque(maxlen=16)
    size = [0]
    job = WindowsJob() if os.name == 'nt' else None
    try:
        child = subprocess.Popen([str(a) for a in argv], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=os.name != 'nt',
                                 creationflags=subprocess.CREATE_NO_WINDOW | 4 if os.name == 'nt' else 0)
        if job:
            try:
                job.attach_resume(child)
            except Exception:
                child.kill()
                child.wait()
                raise
    except Exception:
        if job:
            job.close()
        raise
    def drain():
        try:
            while block := child.stdout.read(4096):
                chunks.append(block)
                size[0] += len(block)
        except (OSError, ValueError):
            pass
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        timed_out = False
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name != 'nt':
                os.killpg(child.pid, signal.SIGTERM)
            else:
                job.finish()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    os.killpg(child.pid, signal.SIGKILL)
                else:
                    job.finish()
                child.wait()
        if job:
            job.finish()
        reader.join(timeout=2)
        return {'exit': child.returncode, 'timedOut': timed_out,
                'output': b''.join(tuple(chunks)).decode('utf-8', errors='replace'), 'outputTruncated': size[0] > 65536}
    finally:
        if job:
            job.close()
        if not reader.is_alive():
            child.stdout.close()


def identity():
    system = {'Darwin': 'macos', 'Windows': 'windows', 'Linux': 'linux'}.get(platform.system(), 'unknown')
    result = {'os': system, 'architecture': platform.machine().lower(), 'variant': None, 'boot': None}
    if system == 'linux':
        for line in pathlib.Path('/etc/os-release').read_text().splitlines():
            if line.startswith('ID='):
                result['variant'] = line[3:].strip('"\'')
        result['boot'] = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    elif system == 'macos':
        value = run(['/usr/sbin/sysctl', '-n', 'kern.bootsessionuuid'], timeout=10)
        if value['exit'] == 0 and re.fullmatch(r'[A-Fa-f0-9-]{36}\s*', value['output']):
            result['boot'] = value['output'].strip()
    elif system == 'windows':
        value = run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                     '(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString("o")'], timeout=15)
        if value['exit'] == 0:
            result['boot'] = value['output'].strip()
    return result


def check_identity(expected_os=None, expected_boot=None):
    observed = identity()
    parts = expected_os.split(':', 1) if expected_os else []
    if parts and (observed['os'] != parts[0] or len(parts) == 2 and observed['variant'] != parts[1]):
        raise GuardError('different-os-boot')
    if expected_boot and observed['boot'] != expected_boot:
        raise GuardError('different-boot-identity')
    return observed


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        before, after = urllib.parse.urlsplit(request.full_url), urllib.parse.urlsplit(newurl)
        if (before.scheme, before.hostname, before.port or 443) != (after.scheme, after.hostname, after.port or 443) or after.username or after.password:
            raise GuardError('cross-origin-metadata-redirect-refused')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def http_json(url, headers=None, limit=8 * 1024 * 1024):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise GuardError('https-without-embedded-credentials-required')
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity',
                                                  'User-Agent': 'ModelHub-Kit/0.1', **(headers or {})})
    try:
        response = urllib.request.build_opener(SameOriginRedirect()).open(request, timeout=30)
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return None, {}
        raise GuardError('metadata-http-error') from None
    with response:
        if urllib.parse.urlsplit(response.url).scheme != 'https':
            raise GuardError('insecure-metadata-redirect')
        body = response.read(limit + 1)
        if len(body) > limit:
            raise GuardError('oversized-metadata')
        encoding = response.headers.get('Content-Encoding', 'identity').lower()
        if encoding == 'gzip':
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                body = compressed.read(limit + 1)
        elif encoding not in ('identity', ''):
            raise GuardError('unsupported-content-encoding')
        if len(body) > limit:
            raise GuardError('oversized-decoded-metadata')
        content_type = response.headers.get_content_type()
        if content_type != 'application/json' and not content_type.endswith('+json'):
            raise GuardError('json-content-type-required')
        value = json.loads(body)
        return value, {k: response.headers[k] for k in ('ETag', 'Last-Modified') if response.headers.get(k)}


def default_config():
    return {'version': 1, 'tools': ['codex', 'claude', 'opencode', 'qwen', 'pi'],
            'cache_budget_bytes': 2 * 1024**3, 'catalog_sources': [], 'mcp_servers': {}, 'acp_agents': {}}


def config(path):
    value = read_json(path)
    if not isinstance(value, dict) or value.get('version') != 1:
        raise GuardError('run-init-or-provide-version1-config')
    if not isinstance(value.get('tools', []), list) or len(value.get('tools', [])) > 32:
        raise GuardError('invalid-tool-selection')
    if not isinstance(value.get('catalog_sources', []), list) or len(value.get('catalog_sources', [])) > 16:
        raise GuardError('invalid-catalog-sources')
    if not all(isinstance(s, dict) and set(s) <= {'id', 'url', 'bearer_env'} for s in value.get('catalog_sources', [])):
        raise GuardError('catalog-url-and-environment-references-only')
    if not isinstance(value.get('mcp_servers', {}), dict) or not isinstance(value.get('acp_agents', {}), dict):
        raise GuardError('object-integration-config-required')
    if not isinstance(value.get('cache_budget_bytes', 0), int) or not 64 * 1024**2 <= value['cache_budget_bytes'] <= 8 * 1024**3:
        raise GuardError('invalid-cache-budget')
    return value
