#!/usr/bin/env python3
"""Update an owned Droid binary atomically without running the process-killing installer."""
import argparse
import errno
import fcntl
import hashlib
import json
import os
import pathlib
import platform
import re
import signal
import stat
import subprocess
import tempfile
import urllib.request
import urllib.parse

BASE = 'https://downloads.factory.ai/factory-cli/releases'
VERSION = re.compile(r'\d+\.\d+\.\d+')


def download(url, destination, maximum):
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(url, timeout=90) as response, open(destination, 'xb') as output:
        if urllib.parse.urlsplit(response.url).scheme != 'https':
            raise ValueError('https-native-download-required')
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            total += len(block)
            if total > maximum:
                raise ValueError('official-download-too-large')
            output.write(block)
            digest.update(block)
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


def text(url, maximum=1024 * 1024):
    with urllib.request.urlopen(url, timeout=30) as response:
        if urllib.parse.urlsplit(response.url).scheme != 'https':
            raise ValueError('https-release-metadata-required')
        value = response.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError('official-metadata-too-large')
    return value.decode('utf-8')


def discover(fetch=text):
    installer = fetch('https://app.factory.ai/cli')
    matches = re.findall(r'^VER="(\d+\.\d+\.\d+)"$', installer, re.M)
    if len(matches) != 1 or 'BASE_URL="https://downloads.factory.ai"' not in installer:
        raise ValueError('official-metadata-unrecognized')
    return matches[0]


def identity(file):
    info = file.lstat()
    return tuple(getattr(info, key) for key in ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns'))


def checksum(file):
    digest = hashlib.sha256()
    descriptor = os.open(str(file), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def probe(file):
    result = subprocess.run([str(file), '--version'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            universal_newlines=True, timeout=20)
    value = result.stdout.strip()
    if result.returncode or not VERSION.fullmatch(value):
        raise ValueError('native-version-unavailable')
    return value


def protected_directory(directory):
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or directory.is_symlink()
            or directory.resolve() != directory.absolute()
            or info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError('unprotected-owned-directory')


def reconcile(home, target_version, fetch=text, transfer=download, version_probe=probe,
              system=None, architecture=None):
    home = pathlib.Path(home).absolute()
    system = system or platform.system()
    architecture = architecture or platform.machine()
    native = {'Darwin': 'darwin', 'Linux': 'linux'}.get(system)
    arch = {'x86_64': 'x64-baseline', 'amd64': 'x64-baseline', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(architecture)
    if not native or not arch or not VERSION.fullmatch(target_version):
        raise ValueError('unsupported-native-release')
    target = home / '.local/bin/droid'
    protected_directory(home)
    protected_directory(target.parent)
    info = target.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o022:
        raise ValueError('unrecognized-owned-droid')
    before = identity(target)
    current = version_probe(target)
    actual = checksum(target)
    if identity(target) != before:
        raise ValueError('installed-binary-changed')
    # A native executable and version string alone do not prove ownership.
    # Accept only an exact official artifact, including the publisher's two x64 builds.
    variants = [arch, 'x64'] if arch == 'x64-baseline' else [arch]
    verified = False
    for variant in variants:
        try:
            expected = fetch(f'{BASE}/{current}/{native}/{variant}/droid.sha256', 1024).strip()
            if re.fullmatch(r'[a-f0-9]{64}', expected) and actual == expected:
                verified = True
                break
        except Exception:
            continue
    if not verified:
        raise ValueError('installed-official-checksum-unverified')
    if tuple(map(int, current.split('.'))) >= tuple(map(int, target_version.split('.'))):
        return {'status': 'current', 'version': current, 'installed': False, 'stoppedProcesses': 0}
    url = f'{BASE}/{target_version}/{native}/{arch}/droid'
    expected = fetch(url + '.sha256', 1024).strip()
    if not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('official-checksum-unavailable')
    with tempfile.TemporaryDirectory(prefix='.modelhub-droid-update-', dir=str(target.parent)) as folder:
        candidate = pathlib.Path(folder) / 'droid'
        transferred = transfer(url, candidate, 512 * 1024 * 1024)
        if transferred != expected or checksum(candidate) != expected:
            raise ValueError('official-checksum-mismatch')
        os.chmod(candidate, 0o755)
        if version_probe(candidate) != target_version:
            raise ValueError('candidate-version-mismatch')
        if identity(target) != before or checksum(target) != actual:
            raise ValueError('installed-binary-changed')
        protected_directory(home)
        protected_directory(target.parent)
        # Replacing the directory entry leaves any existing process's mapped
        # executable inode intact. Never modify the running binary in place.
        os.replace(candidate, target)
        descriptor = os.open(str(target.parent), os.O_RDONLY)
        try:
            try:
                os.fsync(descriptor)
            except OSError as error:
                if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(descriptor)
    return {'status': 'updated', 'previous': current, 'version': target_version,
            'sha256': expected, 'installed': True, 'stoppedProcesses': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--discover', action='store_true')
    args = parser.parse_args()
    if args.discover:
        print(discover())
        return 0
    target = os.environ.get('MODELHUB_DISCOVERED_VERSION', '')
    state = pathlib.Path.home() / '.local/state/modelhub-updater'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    protected_directory(state)
    descriptor = os.open(str(state / 'droid-update.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('unprotected-native-update-lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 75
        result = reconcile(pathlib.Path.home(), target)
        print(json.dumps(result))
        return 0
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    def interrupted(signum, frame):
        # Let the private staging context unwind when the bounded updater
        # cancels this helper; do not retain a partial download after timeout.
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        raise SystemExit(main())
    except Exception as error:
        message = str(error)
        print(json.dumps({'status': 'failed', 'reason': message if re.fullmatch(r'[a-z-]+', message) else 'native-droid-update-failed', 'stoppedProcesses': 0}))
        raise SystemExit(1)
