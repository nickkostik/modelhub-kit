#!/usr/bin/env python3
"""Update an existing ModelHub OMP binary pointer, without replacing live files.

The native stable release, rather than npm's independently published version,
is authoritative. Metadata and artifact hashes come from the official GitHub
release API, with the build service used by upstream's installer as fallback.
"""
import argparse
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
import time
import urllib.parse
import urllib.request

REPOSITORY = 'can1357/oh-my-pi'
BUILD_ORIGIN = 'https://build.stencil.so'
MARKER = 'modelhub-omp-standalone-v1'
RECEIPT = 'modelhub-artifact.json'
RETIREMENT = 'modelhub-retirement.json'
MAX_BINARY = 512 * 1024 * 1024


def stable_version(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?\d+\.\d+\.\d+', value):
        raise ValueError('official-stable-version-required')
    return value.lstrip('v')


def native_target():
    if platform.system() != 'Darwin':
        raise ValueError('standalone-omp-darwin-required')
    arch = {'x86_64': 'x64', 'arm64': 'arm64'}.get(platform.machine())
    # Match upstream's native hardware selection, including Rosetta shells.
    try:
        if subprocess.check_output(['/usr/sbin/sysctl', '-in', 'hw.optional.arm64'],
                                   timeout=3, stderr=subprocess.DEVNULL).strip() == b'1':
            arch = 'arm64'
    except (OSError, subprocess.SubprocessError):
        pass
    if not arch:
        raise ValueError('unsupported-standalone-omp-architecture')
    return 'omp-darwin-' + arch, 'macos-' + ('x86_64' if arch == 'x64' else 'arm64')


def request_json(url):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'ModelHub-Updater/1', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        if urllib.parse.urlsplit(response.url).scheme != 'https':
            raise ValueError('https-release-metadata-required')
        data = response.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise ValueError('oversized-release-metadata')
    return json.loads(data)


def checked_release(version, asset, size, checksum, url, metadata_url):
    version = stable_version(version)
    if not re.fullmatch(r'omp-darwin-(?:x64|arm64)', asset) or \
            not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_BINARY or \
            not re.fullmatch(r'[a-f0-9]{64}', checksum or ''):
        raise ValueError('verified-native-asset-metadata-required')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('official-https-asset-required')
    if metadata_url.startswith('https://api.github.com/'):
        if url != f'https://github.com/{REPOSITORY}/releases/download/v{version}/{asset}':
            raise ValueError('official-github-release-asset-required')
    elif parsed.hostname != 'build.stencil.so' and not (parsed.hostname or '').endswith('.r2.cloudflarestorage.com'):
        raise ValueError('official-build-download-origin-required')
    return dict(version=version, asset=asset, size=size, sha256=checksum,
                url=url, metadataUrl=metadata_url)


def resolve_release(asset, target):
    api = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
    try:
        value = request_json(api)
        version = stable_version(value.get('tag_name'))
        if value.get('draft') or value.get('prerelease'):
            raise ValueError('stable-published-release-required')
        matches = [row for row in value.get('assets', []) if row.get('name') == asset]
        if len(matches) != 1:
            raise ValueError('one-native-release-asset-required')
        row = matches[0]
        checksum = row.get('digest', '')
        if not isinstance(checksum, str) or not checksum.startswith('sha256:'):
            raise ValueError('native-release-sha256-required')
        return checked_release(version, asset, row.get('size'), checksum[7:],
                               row.get('browser_download_url', ''), api)
    except (OSError, ValueError, KeyError, TypeError):
        # Independent official path: no npm fallback or invented release URL.
        api = f'{BUILD_ORIGIN}/api/products/omp/latest/{target}?channel=stable'
        value = request_json(api)
        build, row = value.get('build', {}), value.get('file', {})
        expected_arch = 'x86_64' if asset.endswith('-x64') else 'arm64'
        if build.get('product') != 'omp' or build.get('channel') != 'stable' or \
                row.get('name') != asset or row.get('platform') != 'macos' or row.get('arch') != expected_arch:
            raise ValueError('official-stable-build-target-required')
        return checked_release(build.get('version'), asset, row.get('size'), row.get('sha256'),
                               value.get('download', ''), api)


def identity(info):
    return {key: getattr(info, 'st_' + key) for key in
            ['dev', 'ino', 'uid', 'gid', 'mode', 'nlink', 'size', 'mtime_ns', 'ctime_ns']}


def owned(path, directory=False):
    info = path.lstat()
    if path.is_symlink() or path.resolve() != path.absolute() or info.st_uid != os.getuid() or \
            info.st_mode & 0o022 or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or \
            (not directory and info.st_nlink != 1):
        raise ValueError('owned-canonical-omp-path-required')
    return info


def protected_ancestry(home, directory):
    if home not in directory.parents and directory != home:
        raise ValueError('omp-home-containment-required')
    owned(home, True)
    current = home
    for part in directory.relative_to(home).parts:
        current = current / part
        owned(current, True)


def digest(file):
    with file.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest() if hasattr(hashlib, 'file_digest') else stream_digest(stream)


def stream_digest(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        value.update(block)
    return value.hexdigest()


def version(binary):
    output = subprocess.check_output([str(binary), '--version'], timeout=20,
                                     stderr=subprocess.DEVNULL).decode().strip()
    match = re.fullmatch(r'(?:omp[/ ]|v)?(\d+\.\d+\.\d+)', output)
    if not match:
        raise ValueError('native-omp-version-probe-failed')
    return match.group(1)


def download(release, destination):
    request = urllib.request.Request(release['url'], headers={'User-Agent': 'ModelHub-Updater/1'})
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=45) as response, destination.open('xb') as output:
        if urllib.parse.urlsplit(response.url).scheme != 'https':
            raise ValueError('https-native-download-required')
        total = 0
        for block in iter(lambda: response.read(1024 * 1024), b''):
            total += len(block)
            if total > release['size'] or time.monotonic() - started > 240:
                raise ValueError('native-artifact-download-bound-exceeded')
            output.write(block)
        output.flush()
        os.fsync(output.fileno())
    if total != release['size'] or digest(destination) != release['sha256']:
        raise ValueError('native-omp-artifact-integrity-failed')
    # A verified raw native executable is expected, never an archive or script.
    with destination.open('rb') as stream:
        header = stream.read(8)
    cpu = 0x01000007 if release['asset'].endswith('-x64') else 0x0100000c
    if header != b'\xcf\xfa\xed\xfe' + cpu.to_bytes(4, 'little'):
        raise ValueError('native-omp-mach-o-target-required')


def receipt_matches(file, binary, release=None):
    try:
        info = owned(file)
        if info.st_mode & 0o077 or info.st_size > 8192:
            return False
        saved = json.loads(file.read_text())
        return saved.get('marker') == MARKER and saved.get('binaryIdentity') == identity(owned(binary)) and \
            saved.get('version') == binary.parent.name and re.fullmatch('[a-f0-9]{64}', saved.get('sha256', '')) and \
            (release is None or all(saved.get(key) == release[key] for key in ['version', 'asset', 'size', 'sha256']))
    except (OSError, ValueError, TypeError):
        return False


def write_json(file, data):
    if file.exists() or file.is_symlink():
        owned(file)
    temporary = file.parent / ('.receipt-' + str(os.getpid()))
    created = False
    try:
        with open(str(temporary), 'x', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            created = True
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
    finally:
        if created and temporary.exists():
            temporary.unlink()


def write_receipt(binary, release):
    data = {key: release[key] for key in ['version', 'asset', 'size', 'sha256', 'metadataUrl']}
    data.update(marker=MARKER, binaryIdentity=identity(owned(binary)))
    write_json(binary.parent / RECEIPT, data)


def legacy_retirement_matches(binary):
    try:
        file = binary.parent / RETIREMENT
        info = owned(file)
        if info.st_mode & 0o077 or info.st_size > 4096:
            return False
        value = json.loads(file.read_text())
        return value.get('marker') == 'modelhub-omp-legacy-retirement-v1' and \
            value.get('version') == binary.parent.name and value.get('binaryIdentity') == identity(owned(binary))
    except (OSError, ValueError, TypeError):
        return False


def active_binaries(root):
    """Executable names only, never argument vectors or credentials."""
    try:
        output = subprocess.check_output(['ps', '-A', '-ww', '-o', 'pid=', '-o', 'comm='],
                                         timeout=10, stderr=subprocess.DEVNULL)
        if len(output) > 8 * 1024 * 1024:
            return None
        active = set()
        for line in output.decode(errors='replace').splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                return None
            command = pathlib.Path(parts[1])
            if command.name == 'omp' and not command.is_absolute():
                return None  # Cannot prove which release a short name uses.
            if root in command.parents:
                active.add(str(command))
        return active
    except (OSError, subprocess.SubprocessError):
        return None


def retire_idle(root, current, legacy=None):
    live = active_binaries(root)
    if live is None:
        return dict(retiredReleases=0, retirementDeferred=True)
    removed = 0
    retained = 0
    for folder in list(root.iterdir())[:64]:
        if folder == current.parent or not re.fullmatch(r'\d+\.\d+\.\d+', folder.name):
            continue
        if tuple(map(int, folder.name.split('.'))) >= tuple(map(int, current.parent.name.split('.'))):
            continue
        binary = folder / 'omp'
        try:
            owned(folder, True)
            before = identity(owned(binary))
            names = {p.name for p in folder.iterdir()}
            proven = receipt_matches(folder / RECEIPT, binary) or legacy_retirement_matches(binary)
            if not proven and not (legacy and folder == legacy['path'].parent and before == legacy['identity']):
                continue
            if not names <= {'omp', RECEIPT, RETIREMENT}:
                continue
            if str(binary) in live:
                retained += 1
                continue
            fresh = active_binaries(root)
            if fresh is None or str(binary) in fresh or identity(owned(binary)) != before:
                retained += 1
                continue
            metadata = [folder / name for name in [RECEIPT, RETIREMENT] if name in names]
            for file in metadata:
                owned(file)
            binary.unlink()
            for file in metadata:
                file.unlink()
            folder.rmdir()
            removed += 1
        except (OSError, ValueError):
            continue  # Unknown or concurrently changed files stay untouched.
    return dict(retiredReleases=removed, retainedLiveReleases=retained, retirementDeferred=False)


def reconcile(home):
    home = pathlib.Path(home).absolute()
    root, link = home / '.local/share/modelhub-omp-releases', home / '.local/bin/omp'
    # Reject custom files, npm links, relative links and unowned parent chains
    # before metadata requests, downloads, or executing a version probe.
    if not link.is_symlink():
        raise ValueError('existing-managed-omp-pointer-required')
    protected_ancestry(home, root)
    protected_ancestry(home, link.parent)
    link_before, old_text = identity(link.lstat()), os.readlink(link)
    old = pathlib.Path(old_text)
    if link_before['uid'] != os.getuid() or not old.is_absolute() or old != link.resolve() or \
            old.name != 'omp' or old.parent.parent != root or not re.fullmatch(r'\d+\.\d+\.\d+', old.parent.name):
        raise ValueError('custom-omp-launcher-preserved')
    owned(old.parent, True)
    old_info = identity(owned(old))
    lock = root / '.update.lock'
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if identity(os.fstat(fd)) != identity(owned(lock)):
            raise ValueError('omp-writer-lock-identity-changed')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return dict(queued=True, reason='existing-omp-writer', changed=False)
        asset, target = native_target()
        release = resolve_release(asset, target)
        before = old.parent.name
        # A current release with a changed file identity must pass its official
        # digest before we execute it again. Older legacy pointers still get a
        # bounded native version check before the verified replacement is used.
        if before != release['version'] and version(old) != before:
            raise ValueError('managed-omp-version-path-mismatch')
        if tuple(map(int, release['version'].split('.'))) < tuple(map(int, before.split('.'))):
            raise ValueError('omp-stable-downgrade-refused')
        folder = root / release['version']
        binary = folder / 'omp'
        downloaded = False
        if folder.exists() or folder.is_symlink():
            owned(folder, True)
            info = owned(binary)
            if not receipt_matches(folder / RECEIPT, binary, release):
                if info.st_size != release['size'] or digest(binary) != release['sha256']:
                    raise ValueError('immutable-omp-release-mismatch-preserved')
                if version(binary) != release['version']:
                    raise ValueError('native-omp-version-mismatch')
                write_receipt(binary, release)
        else:
            with tempfile.TemporaryDirectory(prefix='.omp-prepare-', dir=str(root)) as temporary:
                stage = pathlib.Path(temporary)
                candidate = stage / 'omp'
                download(release, candidate)
                candidate.chmod(0o755)
                if version(candidate) != release['version']:
                    raise ValueError('native-omp-version-mismatch')
                # Immutable release directory rename; running binaries are never
                # overwritten. Temporary downloads disappear on every failure.
                os.rename(stage, folder)
                write_receipt(binary, release)
                downloaded = True
        if version(binary) != release['version']:
            raise ValueError('verified-omp-release-cannot-start')
        protected_ancestry(home, root)
        protected_ancestry(home, link.parent)
        if identity(link.lstat()) != link_before or os.readlink(link) != old_text or identity(owned(old)) != old_info:
            raise ValueError('omp-pointer-changed-concurrently')
        changed = old != binary
        if changed:
            if not receipt_matches(old.parent / RECEIPT, old):
                # Legacy installations predate artifact receipts. This bounded
                # retirement record proves only the old selected pointer's
                # exact file identity, never a fabricated official checksum.
                write_json(old.parent / RETIREMENT, dict(
                    marker='modelhub-omp-legacy-retirement-v1', version=before, binaryIdentity=old_info))
            temporary_link = link.with_name('.omp-modelhub-next-' + str(os.getpid()))
            created = False
            try:
                os.symlink(str(binary), temporary_link)
                created = True
                os.replace(temporary_link, link)
            finally:
                if created and temporary_link.is_symlink():
                    temporary_link.unlink()
        result = dict(before=before, after=release['version'], changed=changed, downloaded=downloaded,
                      verified=True, sha256=release['sha256'], metadataUrl=release['metadataUrl'],
                      appsRestarted=0, backupsCreated=0)
        result.update(retire_idle(root, binary, legacy=dict(path=old, identity=old_info)))
        return result
    finally:
        os.close(fd)


def install_cli_cancel_handlers():
    def cancel(signum, _frame):
        # Unwind only our own staging contexts and lock descriptor. Existing
        # interactive applications never receive a signal from this helper.
        raise SystemExit(128 + signum)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, cancel)


if __name__ == '__main__':
    install_cli_cancel_handlers()
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', default=os.path.expanduser('~'))
    args = parser.parse_args()
    try:
        result = reconcile(args.home)
        print(json.dumps(result))
        raise SystemExit(75 if result.get('queued') else 0)
    except Exception as error:
        # Never expose signed download URLs, headers, or third-party stderr.
        reason = str(error) if re.fullmatch(r'[a-z][a-z0-9-]+', str(error)) else 'standalone-omp-reconciliation-failed'
        print(json.dumps(dict(verified=False, changed=False, oldRuntimePreserved=True, reason=reason)))
        raise SystemExit(1)
