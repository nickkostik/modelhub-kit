"""Use the checked catalog runtime under the existing maintenance writer."""
import contextlib
import hashlib
import json
import os
import pathlib
import re
import subprocess

from .core import GuardError, atomic_json, atomic_text, protected, read_json
from .schedule import _snapshot

EXECUTABLE_NAMES = ('native/modelhub-kit-catalog', 'native/modelhub-kit-catalog.exe')
RELEASE_FILE = EXECUTABLE_NAMES[os.name == 'nt']
MAX_EXECUTABLE = 32 * 1024 * 1024
MAX_CACHE = 8 * 1024 * 1024
MAX_CONTROL = 64 * 1024
MAX_REPLY = 16 * 1024 * 1024


def checked_catalog_runtime():
    application = pathlib.Path(__file__).resolve().parents[1]
    receipt = read_json(application / '.modelhub-kit-install.json', limit=65536)
    if not isinstance(receipt, dict) or receipt.get('marker') != 'modelhub-kit-install-v1':
        return None
    files = receipt.get('files')
    expected = files.get(RELEASE_FILE) if isinstance(files, dict) else None
    if expected is None:
        return None
    binary = application / RELEASE_FILE
    if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise GuardError('native-catalog-release-receipt-invalid')
    protected(binary.parent, directory=True)
    before = protected(binary)
    if not 0 < before.st_size <= MAX_EXECUTABLE or os.name != 'nt' and not before.st_mode & 0o100:
        raise GuardError('native-catalog-executable-invalid')
    identity = lambda row: (row.st_dev, row.st_ino, row.st_ctime_ns, row.st_mtime_ns, row.st_size)
    with os.fdopen(os.open(binary, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)), 'rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise GuardError('native-catalog-executable-changed')
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise GuardError('native-catalog-executable-changed')
    if actual != expected or identity(protected(binary)) != identity(before):
        raise GuardError('native-catalog-executable-unverified')
    return binary


def reconcile_catalogs(config, state, operation):
    binary = checked_catalog_runtime()
    if binary is None:
        return None
    sources = config.get('catalog_sources', [])
    ids = [source.get('id', '') for source in sources]
    if (any(not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name) for name in ids)
            or len(set(ids)) != len(ids)):
        raise GuardError('unique-safe-source-id-required')
    state = pathlib.Path(state)
    cooldown_file = state / 'catalog-cooldowns.json'
    original = read_json(cooldown_file, default={}) if operation == 'sync' else {}
    if not isinstance(original, dict):
        raise GuardError('invalid-catalog-cooldown-state')
    cooldowns = {}
    results = []
    if not sources:
        if original:
            atomic_json(cooldown_file, {})
        return results
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    try:
        process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError as error:
        raise GuardError('native-catalog-runtime-unavailable') from error
    try:
        for source, name in zip(sources, ids):
            file = state / 'catalogs' / (name + '.json')
            try:
                before = _snapshot(file, limit=MAX_CACHE)
            except (GuardError, OSError, ValueError):
                results.append({'source': name, 'provider_id': source.get('provider_id'), 'models': [],
                                'status': 'queued-metadata-unavailable'} if operation == 'limits'
                               else {'source': name, 'status': 'refresh-failed', 'retained': False})
                if name in original:
                    cooldowns[name] = original[name]
                continue
            prior = before[0] if before else b''
            request = {'operation': operation, 'source': source, 'prior_bytes': len(prior), 'cooldown': original.get(name)}
            body = json.dumps(request, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
            if len(body) > MAX_CONTROL:
                raise GuardError('native-catalog-input-too-large')
            process.stdin.write(body)
            process.stdin.write(prior)
            process.stdin.flush()
            line = process.stdout.readline(MAX_REPLY + 1)
            if not line.endswith(b'\n') or len(line) > MAX_REPLY:
                raise GuardError('native-catalog-reply-unavailable')
            reply = json.loads(line)
            result = reply.get('result') if isinstance(reply, dict) else None
            if not isinstance(result, dict) or result.get('source') != name:
                raise GuardError('native-catalog-reply-invalid')
            if operation == 'sync':
                record = reply.get('record')
                if record is not None:
                    text = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
                    try:
                        if len(text.encode()) > MAX_CACHE:
                            raise GuardError('native-catalog-cache-too-large')
                        expected = prior.decode().replace('\r\n', '\n').replace('\r', '\n') if before else None
                        result['written'] = atomic_text(file, text, expected=expected)
                    except (GuardError, OSError, ValueError):
                        result = {'source': name, 'status': 'catalog-write-deferred'}
                if reply.get('cooldown') is not None:
                    cooldowns[name] = reply['cooldown']
            results.append(result)
        process.stdin.close()
        if process.wait(timeout=10):
            raise GuardError('native-catalog-runtime-failed')
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise GuardError('native-catalog-runtime-failed') from error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        with contextlib.suppress(OSError):
            process.stdin.close()
    if operation == 'sync' and cooldowns != original:
        atomic_json(cooldown_file, cooldowns)
    return results
