"""Small standard-library primitives. Runtime state never belongs in Git."""
import contextlib
import datetime
import email.utils
import gzip
import hashlib
import http.client
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
import zlib
from . import __version__


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


def _replace_file(temporary, path, durable=False):
    if durable and os.name == 'nt':
        import ctypes
        move = ctypes.WinDLL('kernel32', use_last_error=True).MoveFileExW
        move.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        move.restype = ctypes.c_int
        if not move(str(temporary), str(path), 0x1 | 0x8):  # REPLACE_EXISTING | WRITE_THROUGH
            raise GuardError('durable-file-publication-failed')
    else:
        os.replace(temporary, path)
        if durable:
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def atomic_text(path, text, expected=_UNSPECIFIED, *, durable=False):
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
        _replace_file(temporary, path, durable)
        return True
    finally:
        pathlib.Path(temporary).unlink(missing_ok=True)


def atomic_json(path, value, *, durable=False):
    return atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False) + '\n', durable=durable)


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
    def __init__(self, *, kill_on_close=True):
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
        limits.basic.flags = 0x2000 if kill_on_close else 0  # No breakaway.
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

    def wait_empty(self):
        """Follow a mutation's descendants naturally; never cancel the Job."""
        counters = (self.c.c_ubyte * 48)()
        while True:
            if not self.api.QueryInformationJobObject(self.handle, 1, counters, 48, None):
                # An unknown count is not terminal authority. Retain custody.
                time.sleep(0.1)
                continue
            if int.from_bytes(bytes(counters)[40:44], 'little') == 0:
                return
            time.sleep(0.05)

    def close(self):
        if getattr(self, 'handle', None):
            self.api.CloseHandle(self.handle)
            self.handle = None


def run(argv, *, env=None, timeout=60, input_data=None):
    """Bound our own child; never search for or terminate other applications."""
    if input_data is not None and (not isinstance(input_data, bytes) or len(input_data) > 32 * 1024**2):
        raise GuardError('bounded-byte-input-required')
    chunks = deque(maxlen=16)
    size = [0]
    sent = [0]
    input_failed = [False]
    job = WindowsJob() if os.name == 'nt' else None
    try:
        child = subprocess.Popen([str(a) for a in argv], env=env,
                                 stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
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
    def feed():
        try:
            view = memoryview(input_data)
            while sent[0] < len(view):
                count = child.stdin.write(view[sent[0]:sent[0] + 65536])
                if not count:
                    raise BrokenPipeError()
                sent[0] += count
            child.stdin.flush()
        except (OSError, ValueError):
            input_failed[0] = True
        finally:
            try:
                child.stdin.close()
            except (OSError, ValueError):
                input_failed[0] = True
    feeder = threading.Thread(target=feed, daemon=True) if input_data is not None else None
    if feeder:
        feeder.start()
    def stop_owned(force=False):
        if job:
            job.finish()
        else:
            try:
                os.killpg(child.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass
    try:
        timed_out = False
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_owned()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stop_owned(True)
                child.wait()
        if job:
            job.finish()
        if feeder:
            feeder.join(timeout=1)
            if feeder.is_alive():
                # A child must not leave an inherited pipe holding our finite
                # delivery open after its own exit. Only our process group/job
                # is eligible for cancellation.
                input_failed[0] = True
                stop_owned()
                feeder.join(timeout=1)
                if feeder.is_alive():
                    stop_owned(True)
                    feeder.join(timeout=1)
                if feeder.is_alive():
                    raise GuardError('maintenance-input-still-exiting')
        reader.join(timeout=2)
        result = {'exit': child.returncode, 'timedOut': timed_out,
                  'output': b''.join(tuple(chunks)).decode('utf-8', errors='replace'), 'outputTruncated': size[0] > 65536}
        if input_data is not None:
            result.update(inputSent=sent[0], inputFailed=input_failed[0])
        return result
    finally:
        if job:
            job.close()
        if not reader.is_alive():
            child.stdout.close()


def run_mutation(argv, *, env=None, timeout=None):
    """Follow vendor mutation custody; timeout is observation only."""
    from .mutation import run
    return run(argv, env=env, timeout=timeout)


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


class MetadataHTTPError(GuardError):
    """Safe metadata diagnostics; never retain an upstream body or credential."""
    def __init__(self, status, category, attempts, retry_after=None):
        super().__init__(f'metadata-http-{status}-{category}')
        self.status, self.category, self.attempts = status, category, attempts
        self.retry_after = retry_after

    def diagnostic(self):
        result = {'httpStatus': self.status, 'category': self.category, 'attempts': self.attempts}
        if self.retry_after is not None:
            result['retryAfterSeconds'] = self.retry_after
        return result


_METADATA_RETRY_DELAYS = (0.25, 0.5)
_METADATA_RETRY_WAIT_LIMIT = 5
_METADATA_ERROR_LIMIT = 16 * 1024
_CREDENTIAL_ERROR_CODES = frozenset({
    'invalid_api_key', 'invalid_token', 'invalid_credential', 'invalid_credentials',
    'authentication_error', 'authentication_failed', 'unauthorized', 'permission_denied',
    'permission_error', 'access_denied', 'insufficient_quota', 'billing_error',
})
_TEMPORARY_ERROR_CODES = frozenset({
    'rate_limit_exceeded', 'rate_limit_error', 'overloaded_error', 'server_overloaded',
    'temporarily_unavailable', 'service_unavailable',
})
_REQUEST_ERROR_CODES = frozenset({'invalid_request_error', 'invalid_request', 'bad_request'})
_PARAMETER_ERROR_CODES = frozenset({'unsupported_parameter', 'unknown_parameter', 'invalid_parameter'})


def _metadata_rejection(error):
    """Classify a completed error using constants, not provider-supplied text."""
    codes, complete = set(), False
    try:
        body = error.read(_METADATA_ERROR_LIMIT + 1)
        complete = len(body) <= _METADATA_ERROR_LIMIT
        length = error.headers.get('Content-Length') if error.headers else None
        if length is not None:
            complete = complete and bool(re.fullmatch(r'[0-9]{1,12}', length)) and int(length) == len(body)
        if complete and body:
            value = json.loads(body)
            if isinstance(value, dict):
                detail = value.get('error', value)
                if isinstance(detail, dict):
                    codes = {detail.get(field) for field in ('code', 'type') if isinstance(detail.get(field), str)}
    except (OSError, EOFError, http.client.HTTPException, ValueError):
        # A disconnected or undecodable error is not evidence for replay.
        complete = False
    if error.code in (401, 403) or codes & _CREDENTIAL_ERROR_CODES:
        return 'credential-or-permission', False
    if codes & _PARAMETER_ERROR_CODES:
        return 'unsupported-parameter', False
    if error.code in (400, 429, 503) and codes & _TEMPORARY_ERROR_CODES:
        return 'temporarily-unavailable', complete
    if codes & _REQUEST_ERROR_CODES:
        return 'invalid-request', False
    if error.code in (429, 503) and not codes:
        return 'temporarily-unavailable', complete
    return ('invalid-request' if error.code == 400 else 'request-rejected'), False


def _metadata_retry_after(headers):
    value = headers.get('Retry-After') if headers else None
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 128:
        return False
    value = value.strip()
    if re.fullmatch(r'[0-9]{1,10}', value):
        return int(value)
    try:
        date = email.utils.parsedate_to_datetime(value)
        if date.tzinfo is not None:
            return max(0, date.timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        pass
    return False


def http_json(url, headers=None, limit=8 * 1024 * 1024):
    """Fetch metadata GETs only, with at most two bounded rejection retries."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise GuardError('https-without-embedded-credentials-required')
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity',
                                                  'User-Agent': 'ModelHub-Kit/' + __version__, **(headers or {})})
    opener = urllib.request.build_opener(SameOriginRedirect())
    for attempt in range(len(_METADATA_RETRY_DELAYS) + 1):
        try:
            response = opener.open(request, timeout=30)
            break
        except urllib.error.HTTPError as error:
            try:
                if error.code == 304:
                    return None, {}
                category, transient = _metadata_rejection(error)
                retry_after = _metadata_retry_after(error.headers)
                failure = MetadataHTTPError(error.code, category, attempt + 1,
                                            retry_after if retry_after is not False else None)
            finally:
                error.close()
            if (not transient or attempt == len(_METADATA_RETRY_DELAYS) or retry_after is False
                    or retry_after is not None and retry_after > _METADATA_RETRY_WAIT_LIMIT):
                raise failure from None
            time.sleep(max(_METADATA_RETRY_DELAYS[attempt], retry_after or 0))
    try:
        with response:
            if urllib.parse.urlsplit(response.url).scheme != 'https':
                raise GuardError('insecure-metadata-redirect')
            body = response.read(limit + 1)
            if len(body) > limit:
                raise GuardError('oversized-metadata')
            length = response.headers.get('Content-Length')
            if length is not None and (not re.fullmatch(r'[0-9]+', length) or int(length) != len(body)):
                raise GuardError('incomplete-metadata-body')
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
            validators = {k: response.headers[k] for k in ('ETag', 'Last-Modified')
                          if response.headers.get(k) and len(response.headers[k]) <= 4096
                          and not any(c in response.headers[k] for c in '\r\n\0')}
            return value, validators
    except (EOFError, http.client.HTTPException, zlib.error) as error:
        raise GuardError('metadata-response-body-incomplete') from None


def default_config():
    return {'version': 1, 'tools': ['codex', 'claude', 'opencode', 'qwen', 'pi'],
            'cache_budget_bytes': 2 * 1024**3, 'catalog_sources': [], 'mcp_servers': {}, 'acp_agents': {},
            'model_providers': {}, 'integrations': {}, 'client_versions': {}}


def config(path):
    value = read_json(path)
    if not isinstance(value, dict) or value.get('version') != 1:
        raise GuardError('run-init-or-provide-version1-config')
    if not isinstance(value.get('tools', []), list) or len(value.get('tools', [])) > 32:
        raise GuardError('invalid-tool-selection')
    if not isinstance(value.get('catalog_sources', []), list) or len(value.get('catalog_sources', [])) > 16:
        raise GuardError('invalid-catalog-sources')
    if not all(isinstance(s, dict) and set(s) <= {'id', 'url', 'bearer_env', 'provider_id'} for s in value.get('catalog_sources', [])):
        raise GuardError('catalog-url-and-environment-references-only')
    if any('provider_id' in s and (not isinstance(s['provider_id'], str)
           or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,127}', s['provider_id'])) for s in value.get('catalog_sources', [])):
        raise GuardError('exact-catalog-provider-id-required')
    if not isinstance(value.get('mcp_servers', {}), dict) or not isinstance(value.get('acp_agents', {}), dict):
        raise GuardError('object-integration-config-required')
    providers = value.get('model_providers', {})
    versions = value.get('client_versions', {})
    integrations = value.get('integrations', {})
    safe_id = lambda name: isinstance(name, str) and re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name)
    if not isinstance(providers, dict) or len(providers) > 32:
        raise GuardError('bounded-model-providers-required')
    source_ids = {s.get('id') for s in value.get('catalog_sources', []) if isinstance(s.get('id'), str)}
    for name, provider in providers.items():
        if (not safe_id(name) or not isinstance(provider, dict)
                or set(provider) - {'source', 'base_url', 'credential_env', 'protocol'}
                or provider.get('source') not in source_ids):
            raise GuardError('explicit-catalog-provider-reference-required')
        url = provider.get('base_url')
        if not isinstance(url, str) or len(url) > 4096 or any(c in url for c in '\r\n\0'):
            raise GuardError('safe-provider-url-required')
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.fragment or any(re.search(r'key|token|password|secret|credential|authorization', k, re.I)
                                         for k, _ in urllib.parse.parse_qsl(parsed.query))):
            raise GuardError('provider-https-without-embedded-credentials-required')
        credential_name = provider.get('credential_env')
        if not isinstance(credential_name, str) or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', credential_name):
            raise GuardError('credential-environment-name-required')
        if provider.get('protocol') not in ('openai-completions', 'openai-responses', 'anthropic-messages', 'google-generative-ai'):
            raise GuardError('explicit-supported-provider-protocol-required')
    if (not isinstance(versions, dict) or len(versions) > 64
            or any(not safe_id(k) or not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9.+_-]{1,128}', v)
                   for k, v in versions.items())):
        raise GuardError('native-client-version-map-required')
    if not isinstance(integrations, dict) or len(integrations) > 64:
        raise GuardError('bounded-client-integration-map-required')
    for name, surfaces in integrations.items():
        if (not safe_id(name) or not isinstance(surfaces, dict) or set(surfaces) - {'mcp', 'models', 'acp'}
                or any(not isinstance(v, bool) for v in surfaces.values())):
            raise GuardError('explicit-client-integration-surfaces-required')
    if not isinstance(value.get('cache_budget_bytes', 0), int) or not 64 * 1024**2 <= value['cache_budget_bytes'] <= 8 * 1024**3:
        raise GuardError('invalid-cache-budget')
    return value
