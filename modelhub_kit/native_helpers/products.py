"""Owned native terminal bundles. Installer scripts are metadata, never executed."""
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import stat
import struct
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from ..core import GuardError, atomic_json, atomic_text, locations, private_directory, protected, read_json, run

MAX_DOWNLOAD = 512 * 1024**2
MAX_EXPANDED = 1024 * 1024**2
FORMAT = 'modelhub-kit-native-v1'
COMMANDS = {'devin-cli': 'devin', 'cursor-cli': 'cursor-agent', 'muse': 'muse'}
HOSTS = {'devin-cli': {'static.devin.ai'}, 'cursor-cli': {'cursor.com', 'downloads.cursor.com'},
         'muse': {'api.meta.ai', 'lookaside.facebook.com'}}


def version_key(product, value):
    if not isinstance(value, str):
        raise GuardError('stable-native-version-required')
    if product == 'cursor-cli':
        match = re.fullmatch(r'(\d{4})\.(\d{2})\.(\d{2})-[a-f0-9]{7,40}', value)
        if match:
            return tuple(map(int, match.groups()))
    elif product == 'muse':
        match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)-R(\d+)\.(\d+)', value)
        if match:
            return tuple(map(int, match.groups()[3:] + match.groups()[:3]))
    elif re.fullmatch(r'\d+\.\d+\.\d+', value):
        return tuple(map(int, value.split('.')))
    raise GuardError('stable-native-version-required')


def official_url(product, url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS[product] or parsed.username or parsed.password or parsed.fragment or parsed.port not in (None, 443):
        raise GuardError('official-native-origin-required')
    if product == 'muse' and parsed.hostname == 'lookaside.facebook.com' and parsed.path != '/lookaside/muse/download/':
        raise GuardError('official-muse-download-path-required')
    return url


class Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, product):
        self.product = product
    def redirect_request(self, request, fp, code, message, headers, url):
        official_url(self.product, url)
        return super().redirect_request(request, fp, code, message, headers, url)


def fetch_text(product, url, limit=1024**2):
    official_url(product, url)
    request = urllib.request.Request(url, headers={'User-Agent': 'ModelHub-Kit/0.1', 'Accept-Encoding': 'identity'})
    with urllib.request.build_opener(Redirects(product)).open(request, timeout=30) as response:
        official_url(product, response.url)
        value = response.read(limit + 1)
    if len(value) > limit:
        raise GuardError('native-metadata-too-large')
    return value.decode('utf8')


def download(product, url, destination):
    official_url(product, url)
    request = urllib.request.Request(url, headers={'User-Agent': 'ModelHub-Kit/0.1', 'Accept-Encoding': 'identity'})
    digest, size = hashlib.sha256(), 0
    deadline = time.monotonic() + 240
    with urllib.request.build_opener(Redirects(product)).open(request, timeout=90) as response, destination.open('xb') as output:
        official_url(product, response.url)
        while True:
            block = response.read(1024**2)
            if not block:
                break
            size += len(block)
            if time.monotonic() > deadline:
                raise GuardError('native-download-deadline')
            if size > MAX_DOWNLOAD:
                raise GuardError('native-download-too-large')
            digest.update(block); output.write(block)
        output.flush(); os.fsync(output.fileno())
    return digest.hexdigest(), size


def discover(product, system=None, architecture=None, fetch=fetch_text):
    system, architecture = system or platform.system(), (architecture or platform.machine()).lower()
    os_id = {'Darwin': 'macos', 'Linux': 'linux', 'Windows': 'windows'}.get(system)
    arch = {'x86_64': 'x86', 'amd64': 'x86', 'x64': 'x86', 'arm64': 'aarch64', 'aarch64': 'aarch64'}.get(architecture)
    if not os_id or not arch:
        raise GuardError('unsupported-vendor-platform')
    if product == 'devin-cli':
        metadata = json.loads(fetch(product, 'https://static.devin.ai/cli/current/manifest.json'))
        target = ('aarch64' if arch == 'aarch64' else 'x86_64') + {'macos': '-apple-darwin', 'linux': '-unknown-linux', 'windows': '-pc-windows'}[os_id]
        row = metadata.get('platforms', {}).get(target)
        if not isinstance(row, dict):
            raise GuardError('unsupported-vendor-platform')
        version = metadata.get('version'); version_key(product, version)
        url = official_url(product, row.get('url', ''))
        suffix = '.zip' if os_id == 'windows' else '.tar.gz'
        if url != f'https://static.devin.ai/cli/{version}/devin-{version}-{target}{suffix}':
            raise GuardError('native-release-version-binding-required')
        result = {'version': version, 'url': url, 'sha256': row.get('sha256'), 'kind': 'zip' if os_id == 'windows' else 'tar', 'entry': 'bin/devin' + ('.exe' if os_id == 'windows' else '')}
    elif product == 'muse':
        channel = json.loads(fetch(product, 'https://api.meta.ai/muse-code/channels/muse-stable'))
        version = channel.get('version'); version_key(product, version)
        if channel.get('channel') != 'muse-stable' or channel.get('state') != 'public':
            raise GuardError('public-stable-muse-channel-required')
        manifest_url = official_url(product, channel.get('manifest_url', ''))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(manifest_url).query)
        if query != {'channel': ['muse'], 'version': [version], 'file': ['manifest.json']}:
            raise GuardError('native-release-version-binding-required')
        metadata = json.loads(fetch(product, manifest_url))
        if metadata.get('version') != version or metadata.get('checksum_algorithm') != 'sha256':
            raise GuardError('native-release-version-binding-required')
        row = metadata.get('artifacts', {}).get(arch + '_' + os_id)
        if not isinstance(row, dict):
            raise GuardError('unsupported-vendor-platform')
        url = official_url(product, row.get('url', ''))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        expected_file = 'muse-' + arch + '-' + os_id + ('.exe' if os_id == 'windows' else '')
        if query != {'channel': ['muse'], 'version': [version], 'file': [expected_file]}:
            raise GuardError('native-release-version-binding-required')
        result = {'version': version, 'url': url, 'sha256': row.get('checksum'), 'size': row.get('size'), 'kind': 'binary', 'entry': 'muse' + ('.exe' if os_id == 'windows' else '')}
    elif product == 'cursor-cli':
        script = fetch(product, 'https://cursor.com/install?win32=true' if os_id == 'windows' else 'https://cursor.com/install')
        matches = re.findall(r'https://downloads\.cursor\.com/lab/(\d{4}\.\d{2}\.\d{2}-[a-f0-9]{7,40})/', script)
        if len(set(matches)) != 1:
            raise GuardError('official-cursor-version-unavailable')
        version = matches[0]; version_key(product, version)
        target = {'macos': 'darwin', 'linux': 'linux', 'windows': 'windows'}[os_id]
        native_arch = 'arm64' if arch == 'aarch64' else 'x64'
        result = {'version': version, 'url': f'https://downloads.cursor.com/lab/{version}/{target}/{native_arch}/agent-cli-package.' + ('zip' if os_id == 'windows' else 'tar.gz'), 'sha256': None, 'kind': 'zip' if os_id == 'windows' else 'tar', 'strip': 'dist-package', 'entry': 'cursor-agent.exe' if os_id == 'windows' else 'cursor-agent'}
    else:
        raise GuardError('unknown-native-product')
    if product != 'cursor-cli' and not re.fullmatch(r'[a-f0-9]{64}', result.get('sha256') or ''):
        raise GuardError('publisher-sha256-required')
    if 'size' in result and (not isinstance(result['size'], int) or isinstance(result['size'], bool) or not 0 < result['size'] <= MAX_DOWNLOAD):
        raise GuardError('native-release-size-invalid')
    result.update(system=system, architecture='arm64' if arch == 'aarch64' else 'x64', integrity='publisher-sha256' if result['sha256'] else 'official-https-sha256-readback')
    return result


def digest(file):
    value = hashlib.sha256()
    with file.open('rb') as source:
        for block in iter(lambda: source.read(1024**2), b''):
            value.update(block)
    return value.hexdigest()


def file_identity(file):
    s = protected(file)
    return (s.st_dev, s.st_ino, s.st_mode, s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def launcher_path(product, home, system):
    if system == 'Windows':
        base = pathlib.Path(os.environ.get('LOCALAPPDATA', pathlib.Path(home) / 'AppData/Local')) / 'modelhub-kit/bin'
        return base / (COMMANDS[product] + '.cmd')
    return pathlib.Path(home) / '.local/bin' / COMMANDS[product]


def installed_receipt(product, home, state, command=None, system=None):
    """Read-only ownership/version lookup; never starts an executable."""
    system = system or platform.system()
    receipt = read_json(pathlib.Path(state) / 'tools' / (product + '.json'))
    if not isinstance(receipt, dict) or receipt.get('format') != FORMAT or receipt.get('product') != product:
        return None
    version_key(product, receipt.get('version'))
    expected = launcher_path(product, home, system)
    release = pathlib.Path(receipt.get('release', ''))
    if receipt.get('launcher') != str(expected) or release.parent != pathlib.Path(state) / 'native' / product or release.name != receipt['version']:
        return None
    if command and pathlib.Path(command).absolute() != expected.absolute():
        return None
    if receipt.get('launcherSha256'):
        protected(expected)
        if digest(expected) != receipt['launcherSha256']:
            return None
    elif system == 'Windows' or not expected.is_symlink() or expected.resolve() != (release / receipt['entry']).resolve():
        return None
    protected(release, True)
    if not isinstance(receipt.get('files'), dict) or not receipt['files'] or len(receipt['files']) > 20000:
        return None
    if receipt.get('entry') not in receipt['files'] or not isinstance(receipt.get('arguments', []), list) or any(argument not in receipt['files'] for argument in receipt.get('arguments', [])):
        return None
    for relative, row in receipt['files'].items():
        parts = pathlib.PurePosixPath(relative).parts
        if not parts or pathlib.PurePosixPath(relative).is_absolute() or '..' in parts or '\\' in relative or ':' in relative:
            return None
        if not isinstance(row, dict) or not re.fullmatch(r'[a-f0-9]{64}', row.get('sha256', '')) or type(row.get('size')) is not int or row['size'] < 0 or type(row.get('executable')) is not bool:
            return None
        info = protected(release.joinpath(*parts))
        if info.st_size != row['size'] or bool(info.st_mode & 0o111) != row['executable']:
            return None
    return receipt


def unpack(archive, destination, metadata):
    """Only ordinary directories/files; no links, traversal, devices or overwrites."""
    files, total, members = {}, 0, 0
    def member(name, size, mode, source, directory=False):
        nonlocal total, members
        members += 1
        if members > 20000:
            raise GuardError('native-bundle-budget')
        parts = pathlib.PurePosixPath(name).parts
        if not parts or name.startswith('/') or any(p in ('..', '.') or ':' in p or '\\' in p or any(ord(c) < 32 for c in p) for p in parts):
            raise GuardError('unsafe-native-archive-member')
        if metadata.get('system') == 'Windows' and any(p.endswith((' ', '.')) or re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', p, re.I) or any(c in p for c in '<>\"|?*') for p in parts):
            raise GuardError('unsafe-windows-native-member')
        if metadata.get('strip'):
            if parts[0] != metadata['strip']:
                raise GuardError('native-bundle-layout-changed')
            parts = parts[1:]
        if not parts:
            if directory:
                return
            raise GuardError('native-bundle-layout-changed')
        relative = '/'.join(parts); file = destination.joinpath(*parts)
        if directory:
            file.mkdir(parents=True, exist_ok=True); return
        total += size
        if total > MAX_EXPANDED or len(files) >= 20000 or relative in files:
            raise GuardError('native-bundle-budget')
        file.parent.mkdir(parents=True, exist_ok=True)
        with file.open('xb') as output:
            remaining = size; value = hashlib.sha256()
            while remaining:
                block = source.read(min(1024**2, remaining))
                if not block:
                    raise GuardError('truncated-native-member')
                remaining -= len(block); output.write(block); value.update(block)
        executable = bool(mode & 0o111)
        file.chmod(0o755 if executable else 0o644)
        files[relative] = {'sha256': value.hexdigest(), 'size': size, 'executable': executable}
    if metadata['kind'] == 'binary':
        entry = destination / metadata['entry']; shutil.copyfile(archive, entry); entry.chmod(0o755)
        files[metadata['entry']] = {'sha256': digest(entry), 'size': entry.stat().st_size, 'executable': True}
    elif metadata['kind'] == 'tar':
        with tarfile.open(archive, 'r:gz') as bundle:
            for row in bundle:
                if row.isdir():
                    member(row.name, 0, row.mode, None, True)
                elif row.isfile():
                    with bundle.extractfile(row) as stream:
                        member(row.name, row.size, row.mode, stream)
                else:
                    raise GuardError('native-bundle-links-or-special-files')
    else:
        with zipfile.ZipFile(archive) as bundle:
            for row in bundle.infolist():
                mode = row.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise GuardError('native-bundle-links-or-special-files')
                if row.is_dir():
                    member(row.filename, 0, mode, None, True)
                else:
                    with bundle.open(row) as stream:
                        member(row.filename, row.file_size, mode, stream)
    return files


def native_architecture(file, system, architecture):
    with file.open('rb') as stream:
        data = stream.read(4096)
    if data[:4] == b'\x7fELF' and len(data) >= 20:
        endian = '<' if data[5] == 1 else '>' if data[5] == 2 else None
        return system == 'Linux' and endian is not None and struct.unpack(endian + 'H', data[18:20])[0] == {'x64': 62, 'arm64': 183}[architecture]
    if data[:2] == b'MZ' and len(data) >= 64:
        offset = int.from_bytes(data[60:64], 'little')
        return system == 'Windows' and offset + 6 <= len(data) and data[offset:offset+4] == b'PE\0\0' and int.from_bytes(data[offset+4:offset+6], 'little') == {'x64': 0x8664, 'arm64': 0xaa64}[architecture]
    if data[:4] in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe') and len(data) >= 8:
        return system == 'Darwin' and int.from_bytes(data[4:8], 'little') == {'x64': 0x1000007, 'arm64': 0x100000c}[architecture]
    return False


def tree_matches(release, files, allow_missing=False):
    protected(release, True)
    expected = set(files); seen = set()
    expected_directories = {str(parent) for name in files for parent in pathlib.PurePosixPath(name).parents if str(parent) != '.'}
    for root, directories, names in os.walk(release, followlinks=False):
        for name in directories:
            directory = pathlib.Path(root) / name
            if directory.relative_to(release).as_posix() not in expected_directories:
                return False
            protected(directory, True)
        for name in names:
            file = pathlib.Path(root) / name; relative = file.relative_to(release).as_posix()
            if relative not in expected:
                return False
            info = protected(file); row = files[relative]
            if info.st_size != row['size'] or bool(info.st_mode & 0o111) != row['executable'] or digest(file) != row['sha256']:
                return False
            seen.add(relative)
    return seen <= expected if allow_missing else seen == expected


def retire(release, files):
    if not release.exists() and not release.is_symlink():
        protected(release.parent, True)
        return
    if not tree_matches(release, files, allow_missing=True):
        raise GuardError('custom-native-files-preserved')
    directories = []
    for root, children, names in os.walk(release):
        directories.append(pathlib.Path(root))
        for name in names:
            (pathlib.Path(root) / name).unlink()
    for directory in reversed(directories):
        directory.rmdir()


def idle(product, release, processes):
    if processes is None:
        return False
    names = [COMMANDS[product]] + (['agent'] if product == 'cursor-cli' else [])
    for value in processes:
        normalized = value.replace('\\', '/').lower()
        if str(release).replace('\\', '/').lower() in normalized or any(re.search(r'(?:^|[/\s])' + re.escape(name) + r'(?:\.exe|\.cmd)?(?:\s|$)', normalized) for name in names):
            return False
    return True


def reconcile(product, *, home, state, command=None, install_missing=False, dry_run=False,
              system=None, architecture=None, fetch=fetch_text, transfer=download, runner=run,
              processes=None, inventory=None):
    """Caller holds the kit writer. Preserve unowned installations and all live sessions."""
    system = system or platform.system(); home, state = pathlib.Path(home), pathlib.Path(state)
    result = {'tool': product, 'installed': False}
    try:
        current = installed_receipt(product, home, state, command, system)
        launcher = launcher_path(product, home, system)
        if not current and (command or launcher.exists() or launcher.is_symlink()):
            return dict(result, status='unmanaged-installation-preserved')
        if not current and not install_missing:
            return dict(result, status='not-installed')
        if inventory is None:
            from ..cache import process_inventory
            inventory = process_inventory
        native_root = state / 'native' / product
        if current and current.get('retirement'):
            old = current['retirement']; old_root = pathlib.Path(old['release'])
            if dry_run or old_root.parent != native_root or old_root == pathlib.Path(current['release']) or not idle(product, old_root, inventory()):
                return dict(result, status='queued-native-retirement', version=current['version'])
            version_key(product, old_root.name)
            retire(old_root, old['files']); current.pop('retirement')
            atomic_json(state / 'tools' / (product + '.json'), current)
        metadata = discover(product, system, architecture, fetch)
        before = current['version'] if current else None
        if current and version_key(product, before) >= version_key(product, metadata['version']):
            status = ('current' if before == metadata['version'] else
                      'newer-installation-preserved' if version_key(product, before) > version_key(product, metadata['version']) else
                      'queued-native-build-order-unknown')
            return dict(result, status=status, version=before, target=metadata['version'])
        if dry_run:
            return dict(result, status='planned-native-update' if current else 'planned-native-install', before=before, target=metadata['version'], integrity=metadata['integrity'])
        snapshot = inventory() if processes is None else processes
        if not idle(product, native_root, snapshot):
            return dict(result, status='queued-active-or-unknown-processes')
        private_directory(native_root); private_directory(launcher.parent)
        if current and not tree_matches(pathlib.Path(current['release']), current['files']):
            return dict(result, status='custom-native-files-preserved')
        launcher_before = ('symlink', os.readlink(launcher)) if launcher.is_symlink() else ('file', digest(launcher)) if launcher.exists() else None
        destination = native_root / metadata['version']
        if destination.exists() or destination.is_symlink():
            return dict(result, status='unrecognized-native-release-preserved')
        with tempfile.TemporaryDirectory(prefix='.prepare-', dir=native_root) as folder:
            stage = pathlib.Path(folder); archive = stage / 'download'; prepared = stage / 'release'; prepared.mkdir()
            actual, size = transfer(product, metadata['url'], archive)
            if digest(archive) != actual or metadata['sha256'] and actual != metadata['sha256'] or metadata.get('size') and size != metadata['size']:
                raise GuardError('native-artifact-checksum-mismatch')
            files = unpack(archive, prepared, metadata)
            entry = metadata['entry']; arguments = []
            if product == 'cursor-cli':
                node = 'node.exe' if system == 'Windows' else 'node'
                if (prepared / node).is_file() and (prepared / 'index.js').is_file():
                    entry, arguments = node, ['index.js']
                elif system == 'Windows' and not (prepared / entry).is_file():
                    raise GuardError('native-cursor-entry-layout-changed')
            if not (prepared / entry).is_file():
                raise GuardError('native-entry-missing')
            if not native_architecture(prepared / entry, metadata['system'], metadata['architecture']):
                raise GuardError('native-executable-architecture-mismatch')
            env = dict(os.environ, MUSE_NO_AUTO_UPDATE='1', NO_COLOR='1')
            for name in ('XAI_API_KEY', 'GROK_API_KEY', 'NODE_OPTIONS', 'NODE_PATH'):
                env.pop(name, None)
            probe = runner([str(prepared / entry), *[str(prepared / a) for a in arguments], '--version'], env=env, timeout=20)
            if probe['exit'] or probe.get('outputTruncated') or not re.search(r'(?<![A-Za-z0-9.])' + re.escape(metadata['version']) + r'(?![A-Za-z0-9.])', probe['output']):
                raise GuardError('native-product-version-mismatch')
            for relative in files:
                file = prepared / relative
                if file.stat().st_size != files[relative]['size'] or digest(file) != files[relative]['sha256']:
                    raise GuardError('prepared-native-content-changed')
            if not idle(product, native_root, inventory()):
                return dict(result, status='queued-active-or-unknown-processes')
            observed = ('symlink', os.readlink(launcher)) if launcher.is_symlink() else ('file', digest(launcher)) if launcher.exists() else None
            if observed != launcher_before:
                raise GuardError('native-launcher-changed')
            # Cursor's bundled node is launched via a tiny owned wrapper; no
            # shared `agent` alias or vendor updater/startup hook is modified.
            wrapper = None
            if arguments:
                if system == 'Windows':
                    wrapper = '@"' + str(destination / entry) + '" "' + str(destination / arguments[0]) + '" %*\n'
                else:
                    wrapper = '#!/bin/sh\nexec ' + _shell_quote(str(destination / entry)) + ' ' + _shell_quote(str(destination / arguments[0])) + ' "$@"\n'
            if system == 'Windows' and any(c in str(destination) for c in '%!\"\r\n'):
                raise GuardError('unsafe-native-wrapper-path')
            os.replace(prepared, destination)
            if system == 'Windows':
                text = wrapper or '@"' + str(destination / entry) + '" %*\n'
                if any(c in str(destination) for c in '%!"\r\n'):
                    raise GuardError('unsafe-native-wrapper-path')
                atomic_text(launcher, text)
            elif wrapper:
                # One checked wrapper is the current pointer, never an app.
                atomic_text(launcher, wrapper); launcher.chmod(0o755)
            else:
                temporary = launcher.with_name('.' + launcher.name + '.prepare')
                if temporary.exists() or temporary.is_symlink():
                    raise GuardError('native-pointer-stage-busy')
                try:
                    temporary.symlink_to(destination / entry); os.replace(temporary, launcher)
                finally:
                    temporary.unlink(missing_ok=True)
            receipt = {'format': FORMAT, 'product': product, 'version': metadata['version'], 'launcher': str(launcher), 'release': str(destination), 'entry': entry, 'arguments': arguments, 'files': files, 'artifactSha256': actual, 'integrity': metadata['integrity']}
            if system == 'Windows' or wrapper:
                receipt['launcherSha256'] = digest(launcher)
            if current:
                receipt['retirement'] = {'release': current['release'], 'files': current['files']}
            atomic_json(state / 'tools' / (product + '.json'), receipt)
        if current and idle(product, pathlib.Path(current['release']), inventory()):
            retire(pathlib.Path(current['release']), current['files']); receipt.pop('retirement'); atomic_json(state / 'tools' / (product + '.json'), receipt)
        return dict(result, status='updated' if current else 'installed', before=before, version=metadata['version'], installed=True, integrity=metadata['integrity'])
    except (GuardError, OSError, ValueError, KeyError, TypeError, tarfile.TarError, zipfile.BadZipFile) as error:
        return dict(result, status='queued' if isinstance(error, GuardError) else 'metadata-or-artifact-unavailable', reason=str(error) if isinstance(error, GuardError) else 'native-metadata-or-artifact-check-failed')


def _shell_quote(value):
    return "'" + value.replace("'", "'\\''") + "'"
