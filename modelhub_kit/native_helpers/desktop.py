"""Conservative native desktop maintenance; call while holding core.writer.

No GUI executable is used as a version probe. Native package receipts must own
the actual application, and busy/unknown processes always defer mutations.
Direct DMG/AppImage/EXE replacement is deliberately not an adapter: a vendor
download alone is not an authenticated installer policy. Existing Arch packages
can retain their native ownership while receiving an authenticated vendor bundle.

References: https://cursor.com/docs/get-started/quickstart
https://docs.devin.ai/desktop/install
https://prod.cursor.com/docs/grok-bot/deployment
https://learn.microsoft.com/windows/package-manager/winget/upgrade
https://jrsoftware.org/ishelp/topic_setupcmdline.htm
https://docs.brew.sh/Manpage
"""
import base64
import datetime
import gzip
import hashlib
import io
import itertools
import json
import os
import pathlib
import platform
import plistlib
import re
import stat
import struct
import tarfile
import tempfile
import time
import urllib.parse
from email.utils import parsedate_to_datetime

from .. import core


PRODUCTS = {
    'cursor-desktop': {
        'names': ('Cursor',), 'packages': ('cursor', 'cursor-bin'),
        'roots': ('/usr/share/cursor', '/opt/cursor'), 'casks': ('cursor',),
        'hosts': ('downloads.cursor.com', 'api2.cursor.sh'),
        'apt': 'https://downloads.cursor.com/aptrepo',
        'rpm': 'https://downloads.cursor.com/yumrepo',
        'winget': 'Anysphere.Cursor',
    },
    'devin-desktop': {
        'names': ('Devin Desktop', 'Devin', 'Windsurf'),
        'packages': ('devin-desktop', 'windsurf'),
        'roots': ('/opt/devin-desktop', '/usr/share/devin-desktop', '/opt/windsurf', '/usr/share/windsurf'),
        'casks': ('devin', 'devin-desktop', 'windsurf'),
        'hosts': ('windsurf-stable.codeiumdata.com', 'update.windsurf.com'),
        'apt': 'https://windsurf-stable.codeiumdata.com/wVxQEIWkwPUEAGf3/apt',
        'rpm': 'https://windsurf-stable.codeiumdata.com/wVxQEIWkwPUEAGf3/yum/repo',
    },
    'grokbot': {
        'names': ('Grok Bot',), 'packages': ('grok-bot',),
        'roots': ('/opt/Grok Bot', '/usr/share/grok-bot'), 'casks': ('grok-bot',),
        'hosts': ('downloads.cursor.com',),
        # Read the exact installed repository URL; deployment docs specify
        # signed source files, not a permanent repository URL template.
        'apt': None, 'rpm': None,
    },
}
MAX_METADATA = 4 * 1024 * 1024
SAFE_INNO = ('/VERYSILENT /SUPPRESSMSGBOXES /SP- /NOCLOSEAPPLICATIONS '
             '/NORESTARTAPPLICATIONS /NORESTART /MERGETASKS=!runcode')


class Deferred(core.GuardError):
    def __init__(self, reason, target):
        super().__init__(reason)
        self.target = target


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+(?:\.\d+){1,3}', value):
        raise core.GuardError('stable-native-version-required')
    return tuple(int(part) for part in value.split('.')) + (0,) * (4 - len(value.split('.')))


def approved_url(product, url):
    if not isinstance(url, str):
        return False
    value = urllib.parse.urlsplit(url)
    return (value.scheme == 'https' and value.hostname in PRODUCTS[product]['hosts']
            and not value.username and not value.password and not value.query and not value.fragment
            and (product != 'grokbot' or value.path.startswith('/grokbot/')))


def _fetch(fetch, url):
    value = fetch(url)
    # core.http_json also returns transport validators; fixture fetchers often
    # return only the document. Never confuse validators with release metadata.
    if isinstance(value, tuple) and len(value) == 2:
        value = value[0]
    if not isinstance(value, dict):
        raise core.GuardError('native-release-metadata-protocol-invalid')
    return value


def _run(command, argv, timeout=30, env=None):
    try:
        result = command(argv, timeout=timeout, **({'env': env} if env is not None else {}))
    except FileNotFoundError:
        return {'exit': 127, 'output': ''}
    if not isinstance(result, dict) or not isinstance(result.get('output'), str):
        raise core.GuardError('native-command-result-invalid')
    if (len(result['output'].encode('utf-8')) > MAX_METADATA or result.get('truncated')
            or result.get('outputTruncated') or result.get('timedOut')):
        raise core.GuardError('native-command-output-bound-exceeded')
    return result


def _checked(command, argv, timeout=30, env=None):
    result = _run(command, argv, timeout, env)
    if result['exit']:
        raise core.GuardError('native-metadata-command-unavailable')
    return result['output'].strip()


def _mutate(command, argv, timeout=300, env=None):
    result = _run(command, argv, timeout, env)
    if result.get('treeComplete') is not True or result.get('observationFailed'):
        raise core.GuardError('vendor-mutation-custody-unknown')
    return result


def _checked_mutation(command, argv, timeout=300, env=None):
    result = _mutate(command, argv, timeout, env)
    if result['exit']:
        raise core.GuardError('native-vendor-mutation-failed')
    return result['output'].strip()


def _file(path, limit=MAX_METADATA):
    path = pathlib.Path(path)
    info = path.lstat()
    if (path.is_symlink() or path.resolve() != path.absolute() or not stat.S_ISREG(info.st_mode)
            or info.st_size > limit or info.st_nlink != 1):
        raise core.GuardError('native-metadata-file-unverified')
    if os.name != 'nt' and (info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022):
        raise core.GuardError('native-metadata-owner-unverified')
    return path


def _json(path):
    return json.loads(_file(path).read_text(encoding='utf-8'))


def _asar(path):
    """Read the bounded top-level package.json, without evaluating Electron."""
    path = _file(path, 512 * 1024 * 1024)
    with path.open('rb') as stream:
        first = stream.read(8)
        if len(first) != 8:
            raise core.GuardError('native-asar-header-invalid')
        pickle_size, size = struct.unpack('<II', first)
        if pickle_size != 4 or not 8 <= size <= MAX_METADATA:
            raise core.GuardError('native-asar-header-invalid')
        header = stream.read(size)
        if len(header) != size:
            raise core.GuardError('native-asar-header-invalid')
        payload, length = struct.unpack('<II', header[:8])
        if payload != size - 4 or not 0 < length <= size - 8:
            raise core.GuardError('native-asar-header-invalid')
        entry = json.loads(header[8:8 + length]).get('files', {}).get('package.json', {})
        count, offset = entry.get('size'), entry.get('offset')
        if (entry.get('unpacked') or any(key in entry for key in ('link', 'files'))
                or type(count) is not int or not 0 < count <= MAX_METADATA
                or not isinstance(offset, str) or not re.fullmatch(r'\d{1,16}', offset)):
            raise core.GuardError('native-asar-package-invalid')
        start = 8 + size + int(offset)
        if start + count > path.stat().st_size:
            raise core.GuardError('native-asar-package-invalid')
        stream.seek(start)
        value = json.loads(stream.read(count))
    if (value.get('name') != 'sand' or value.get('productName') != 'Grok Bot'
            or value.get('main') != 'dist/electron-main/main.cjs'):
        raise core.GuardError('native-product-identity-unverified')
    return value


def native_metadata(product, root, system):
    """Actual installed app identity, independent of any CLI shim or receipt."""
    root = pathlib.Path(root)
    if root.is_symlink() or root.resolve() != root.absolute():
        raise core.GuardError('native-app-root-unverified')
    info = root.stat()
    if os.name != 'nt' and (info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022):
        raise core.GuardError('native-app-root-owner-unverified')
    resources = root / ('Contents/Resources' if system == 'Darwin' else 'resources')
    current = root
    relative = (resources if product == 'grokbot' else resources / 'app').relative_to(root)
    for component in relative.parts:
        current = current / component
        entry = current.lstat()
        if (current.is_symlink() or not stat.S_ISDIR(entry.st_mode) or
                (os.name != 'nt' and (entry.st_uid not in (0, os.getuid()) or entry.st_mode & 0o022))):
            raise core.GuardError('native-resource-directory-unverified')
    if product == 'grokbot':
        data, package = {}, _asar(resources / 'app.asar')
        owned_file = resources / 'app.asar'
    else:
        owned_file = resources / 'app/product.json'
        data, package = _json(owned_file), _json(resources / 'app/package.json')
        names = {str(data.get(key, '')).lower() for key in ('nameShort', 'nameLong', 'applicationName')}
        expected = {name.lower() for name in PRODUCTS[product]['names']}
        expected.update(name.lower().replace(' ', '-') for name in PRODUCTS[product]['names'])
        if not names.intersection(expected) or data.get('quality', 'stable') != 'stable':
            raise core.GuardError('native-product-identity-unverified')
    native_version = None
    if system == 'Darwin':
        bundle = plistlib.loads(_file(root / 'Contents/Info.plist').read_bytes())
        if not isinstance(bundle.get('CFBundleIdentifier'), str) or not bundle.get('CFBundleExecutable'):
            raise core.GuardError('native-bundle-identity-unverified')
        native_version = bundle.get('CFBundleShortVersionString')
    choices = [data.get(key) for key in ('cursorVersion', 'devinVersion', 'windsurfVersion')]
    choices.extend((native_version, package.get('version')))
    version = next((value for value in choices if isinstance(value, str)
                    and re.fullmatch(r'\d+(?:\.\d+){1,3}', value)), None)
    version_tuple(version)
    return {'root': str(root), 'version': version, 'metadata': str(owned_file)}


WINDOWS_AUDIT = r"""
$ErrorActionPreference='Stop';$rows=@();$known=$true
foreach($hive in @('HKCU','HKLM')){
 foreach($suffix in @('\Software\Microsoft\Windows\CurrentVersion\Uninstall','\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')){
  $base=$hive+':'+$suffix;if(-not(Test-Path -LiteralPath $base)){continue}
  foreach($key in Get-ChildItem -LiteralPath $base){
   $v=Get-ItemProperty -LiteralPath $key.PSPath
   if($v.DisplayName -notmatch '^(Cursor|Devin Desktop|Devin|Windsurf|Grok Bot)(?:\s|$)'){continue}
   $root=[string]$v.InstallLocation
   if(-not $root -and $v.DisplayIcon){$root=Split-Path -Parent (([string]$v.DisplayIcon -replace ',\d+$','').Trim('"'))}
   $rows+=@{name=[string]$v.DisplayName;root=$root;version=[string]$v.DisplayVersion;scope=$(if($hive -eq 'HKCU'){'user'}else{'machine'})}
  }
 }
}
$processes=@();foreach($p in Get-CimInstance Win32_Process){
 if($p.Name -match '(?i)(cursor|devin|windsurf|grok|sand)'){
  if(-not $p.ExecutablePath){$known=$false}
  $processes+=@{name=[string]$p.Name;path=[string]$p.ExecutablePath}
 }
}
$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
@{apps=@($rows);processes=@($processes);known=$known;administrator=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)}|ConvertTo-Json -Depth 5 -Compress
"""


def _windows_audit(command):
    encoded = base64.b64encode(WINDOWS_AUDIT.encode('utf-16le')).decode('ascii')
    value = json.loads(_checked(command, ['powershell.exe', '-NoProfile', '-NonInteractive',
                                         '-EncodedCommand', encoded]))
    if not isinstance(value.get('apps'), list) or len(value['apps']) > 64:
        raise core.GuardError('native-registry-audit-invalid')
    return value


def discover(product, home, system, command):
    if system == 'Windows':
        audit = _windows_audit(command)
        candidates = [row for row in audit['apps'] if row.get('name') in PRODUCTS[product]['names']]
        if len(candidates) > 1:
            raise core.GuardError('multiple-native-installations-preserved')
        if not candidates:
            bases = [pathlib.Path(os.environ.get('LOCALAPPDATA', pathlib.Path(home) / 'AppData/Local')) / 'Programs']
            bases += [pathlib.Path(os.environ[key]) for key in ('ProgramFiles', 'ProgramFiles(x86)') if os.environ.get(key)]
            if any((base / name).exists() for base in bases for name in PRODUCTS[product]['names']):
                raise core.GuardError('unregistered-native-installation-preserved')
            return None
        registration = candidates[0]
        app = native_metadata(product, registration['root'], system)
        # Cursor's bundled package version may be VS Code's version: the
        # registry is authoritative, only after actual product identity passed.
        version_tuple(registration['version'])
        app.update(version=registration['version'], registration=registration,
                   administrator=audit.get('administrator') is True)
        return app
    roots = ([pathlib.Path(base) / (name + '.app') for base in
              (pathlib.Path('/Applications'), pathlib.Path(home) / 'Applications')
              for name in PRODUCTS[product]['names']] if system == 'Darwin'
             else [pathlib.Path(root) for root in PRODUCTS[product]['roots']] +
             [pathlib.Path(home) / base / name for base in ('.local/share', 'Applications')
              for name in PRODUCTS[product]['names']])
    candidates = [root for root in roots if root.exists() or root.is_symlink()]
    if len(candidates) > 1:
        raise core.GuardError('multiple-native-installations-preserved')
    return native_metadata(product, candidates[0], system) if candidates else None


def process_state(product, app, system, command, processes=None):
    """Return 'idle', 'busy', or 'unknown'; do not read argument vectors."""
    if processes is not None:
        value = processes(app) if callable(processes) else processes
        if isinstance(value, dict):
            return ('busy' if value.get('active') else 'idle') if value.get('known') is True else 'unknown'
        return value if value in ('idle', 'busy', 'unknown') else 'unknown'
    if system == 'Windows':
        audit = _windows_audit(command)
        if audit.get('known') is not True or not isinstance(audit.get('processes'), list):
            return 'unknown'
        names = ('cursor',) if product == 'cursor-desktop' else (
            ('devin', 'windsurf') if product == 'devin-desktop' else ('grok', 'sand'))
        return 'busy' if any(any(name in row.get('name', '').lower() for name in names)
                             for row in audit['processes']) else 'idle'
    if system == 'Linux':
        # comm can be renamed or truncated. Verify actual executable paths;
        # unreadable live processes leave activity unknown, never assumed idle.
        roots = [app['root']] if app else list(PRODUCTS[product]['roots'])
        unknown = False
        try:
            children = list(itertools.islice(pathlib.Path('/proc').iterdir(), 65537))
            if len(children) > 65536:
                return 'unknown'
            for child in children:
                if not child.name.isdigit():
                    continue
                try:
                    executable = os.readlink(child / 'exe')
                    if any(executable.startswith(root + '/') for root in roots):
                        return 'busy'
                    # A distribution Electron runtime can load any app's ASAR
                    # outside its executable directory. The executable alone
                    # cannot prove that the selected desktop is inactive.
                    native_name = os.path.basename(executable.removesuffix(' (deleted)')).lower()
                    if re.fullmatch(r'electron(?:[-_.]?\d+(?:\.\d+)*)?', native_name):
                        unknown = True
                except FileNotFoundError:
                    continue  # Process exited, or a kernel thread has no exe.
                except OSError:
                    unknown = True
            return 'unknown' if unknown else 'idle'
        except OSError:
            return 'unknown'
    result = _run(command, ['ps', '-A', '-ww', '-o', 'comm='])
    if result['exit']:
        return 'unknown'
    roots = [app['root']] if app else list(PRODUCTS[product]['roots'])
    names = ('cursor',) if product == 'cursor-desktop' else (
        ('devin', 'windsurf') if product == 'devin-desktop' else ('grok', 'sand'))
    for line in result['output'].splitlines():
        executable = line.strip()
        if any(executable.startswith(root + '/') for root in roots):
            return 'busy'
        if any(name in pathlib.Path(executable).name.lower() for name in names):
            # A short/truncated comm cannot establish that it is another app.
            return 'busy'
    return 'idle'


def parse_winget_list(text, name):
    rows = [re.split(r'\s{2,}', line.strip()) for line in text.splitlines()]
    matches = [row for row in rows if len(row) >= 3 and row[0] == name
               and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]+', row[1])
               and re.fullmatch(r'\d+(?:\.\d+){1,3}', row[2])]
    return {'id': matches[0][1], 'version': matches[0][2]} if len(matches) == 1 else None


def parse_winget_show(text, product):
    def field(name):
        found = re.findall(r'^\s*' + re.escape(name) + r':\s*(.+)$', text, re.M | re.I)
        return found[0].strip() if len(found) == 1 else ''
    version, kind, url, digest = (field(name) for name in
                                  ('Version', 'Installer Type', 'Installer Url', 'Installer SHA256'))
    version_tuple(version)
    if kind.lower() != 'inno' or not approved_url(product, url) or not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
        raise core.GuardError('native-winget-inno-checksum-or-origin-unverified')
    return {'target': version, 'sha256': digest.lower(), 'url': url}


def _windows_plan(product, app, architecture, command, refresh=False, mutation=core.run_mutation):
    if product == 'grokbot':
        raise core.GuardError('grokbot-safe-unattended-windows-installer-unverified')
    if app:
        registration = app['registration']
        result = _checked(command, ['winget', 'list', '--name', registration['name'], '--exact',
                                   '--source', 'winget', '--accept-source-agreements', '--disable-interactivity'])
        owned = parse_winget_list(result, registration['name'])
        if not owned or owned['version'] != app['version']:
            raise core.GuardError('native-winget-registry-ownership-unverified')
        identifier, scope = owned['id'], registration['scope']
    else:
        identifier, scope = PRODUCTS[product].get('winget'), 'user'
        if not identifier:
            raise core.GuardError('explicit-native-install-channel-unavailable')
    if scope not in ('machine', 'user'):
        raise core.GuardError('native-registry-scope-unverified')
    args = ['--id', identifier, '--exact', '--source', 'winget', '--scope', scope,
            '--architecture', architecture]
    if refresh:
        _checked_mutation(mutation, ['winget', 'source', 'update', '--name', 'winget', '--disable-interactivity'], timeout=120)
    manifest = parse_winget_show(_checked(command, ['winget', 'show', *args, '--locale', 'en-US',
                                '--accept-source-agreements', '--disable-interactivity']), product)
    if (app and scope == 'machine' and not app['administrator']
            and version_tuple(manifest['target']) > version_tuple(app['version'])):
        raise Deferred('noninteractive-machine-update-permission-unavailable', manifest['target'])
    manifest.update(channel='winget', argv=['winget', 'upgrade' if app else 'install', *args,
                    '--version', manifest['target'], '--silent', '--disable-interactivity',
                    '--accept-source-agreements', '--accept-package-agreements',
                    '--skip-dependencies', '--override', SAFE_INNO])
    return manifest


def approved_apt_source(text, origin):
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    if any(line.startswith('Types:') for line in lines):
        fields = {}
        for line in lines:
            if ':' not in line:
                return False
            key, value = line.split(':', 1)
            if key in fields or key not in ('Types', 'URIs', 'Suites', 'Components', 'Signed-By', 'Architectures', 'Enabled'):
                return False
            fields[key] = value.strip()
        return (fields.get('Types') == 'deb' and fields.get('URIs', '').rstrip('/') == origin.rstrip('/')
                and fields.get('Suites') == 'stable' and fields.get('Components') == 'main'
                and fields.get('Enabled', 'yes') == 'yes'
                and bool(re.fullmatch(r'/[\w./-]+', fields.get('Signed-By', ''))))
    if len(lines) != 1:
        return False
    found = re.fullmatch(r'deb \[([^\]]+)\] (\S+) stable main', lines[0])
    if not found or found[2].rstrip('/') != origin.rstrip('/'):
        return False
    options = found[1].split()
    return (len([v for v in options if v.startswith('signed-by=')]) == 1 and
            all(re.fullmatch(r'(?:signed-by=/[\w./-]+|arch=(?:amd64|arm64)(?:,(?:amd64|arm64))?)', v) for v in options))


def parse_apt_policy(text, origin):
    match = re.search(r'^\s*Candidate:\s*(\S+)', text, re.M)
    candidate, row = match[1] if match else None, None
    if candidate in (None, '(none)'):
        raise core.GuardError('stable-package-candidate-unavailable')
    for line in text.splitlines():
        found = re.fullmatch(r'\s+(?:\*\*\*\s+)?(\S+)\s+\d+\s*', line)
        if found:
            row = found[1]
        if row == candidate and origin.rstrip('/') + ' ' in line and re.search(r'\bstable/', line):
            return candidate
    raise core.GuardError('stable-package-origin-unverified')


def _elevated(argv):
    return argv if hasattr(os, 'geteuid') and os.geteuid() == 0 else ['sudo', '-n', *argv]


def _package_stable_version(value):
    match = re.fullmatch(r'(?:\d+:)?(\d+(?:\.\d+){1,3})(?:[-+][A-Za-z0-9.+_-]+)?', value)
    if not match:
        raise core.GuardError('stable-package-version-required')
    return match[1]


def _linux_plan(product, app, command, refresh=False, mutation=core.run_mutation):
    spec = PRODUCTS[product]
    package = spec['packages'][0]
    if app:
        owner = _run(command, ['dpkg-query', '-S', app['metadata']])
        if owner['exit']:
            rpm_owner = _run(command, ['rpm', '-qf', '--qf', '%{NAME}', app['metadata']])
            if not rpm_owner['exit']:
                return _rpm_plan(product, app, rpm_owner['output'].strip(), command, refresh=refresh, mutation=mutation)
            arch_owner = _run(command, ['pacman', '-Qoq', app['metadata']])
            if not arch_owner['exit'] and arch_owner['output'].strip() in spec['packages']:
                return _arch_plan(product, app, arch_owner['output'].strip(), command)
            raise core.GuardError('native-package-owner-unverified')
        matches = re.findall(r'^([^\s:]+)(?::(?:amd64|arm64))?: ' + re.escape(app['metadata']) + r'$', owner['output'], re.M)
        if len(matches) != 1 or matches[0] not in spec['packages']:
            raise core.GuardError('native-package-owner-unverified')
        package = matches[0]
    sources = [pathlib.Path('/etc/apt/sources.list')]
    folder = pathlib.Path('/etc/apt/sources.list.d')
    if folder.is_dir():
        sources += [path for path in itertools.islice(folder.iterdir(), 128) if path.suffix in ('.list', '.sources')]
    source, origin = None, spec['apt']
    for path in sources:
        try:
            text = _file(path).read_text()
            candidate_origin = origin
            if product == 'grokbot':
                origins = re.findall(r'(https://[^\s]+)', text)
                origins = [url for url in origins if approved_url(product, url)]
                candidate_origin = origins[0] if len(origins) == 1 else None
            if candidate_origin and approved_apt_source(text, candidate_origin):
                source = path
                origin = candidate_origin
                break
        except (OSError, ValueError, core.GuardError):
            continue
    if not source:
        raise core.GuardError('existing-signed-stable-repository-unverified')
    if refresh:
        _checked_mutation(mutation, _elevated(['apt-get', 'update',
                    '-o', 'Dir::Etc::sourcelist=' + str(source), '-o', 'Dir::Etc::sourceparts=-',
                    '-o', 'APT::Get::List-Cleanup=0', '-o', 'Acquire::AllowInsecureRepositories=false',
                    '-o', 'Acquire::AllowDowngradeToInsecureRepositories=false',
                    '-o', 'APT::Update::Error-Mode=any']), timeout=120)
    held = _checked(command, ['apt-mark', 'showhold']).splitlines()
    if package in held:
        raise core.GuardError('native-package-held')
    policy = _checked(command, ['apt-cache', 'policy', package])
    candidate = parse_apt_policy(policy, origin)
    version = _package_stable_version(candidate)
    if app:
        current = _checked(command, ['dpkg-query', '-W', '-f=${Version}', package])
        # Package revision changes alone do not reinstall an already current GUI.
        if version_tuple(_package_stable_version(current)) != version_tuple(app['version']):
            raise core.GuardError('native-package-version-identity-mismatch')
    argv = ['apt-get', 'install', '--no-install-recommends', '--assume-yes', '--no-remove',
            '-o', 'APT::Get::AllowUnauthenticated=false', '-o', 'Acquire::AllowInsecureRepositories=false']
    if app:
        argv.append('--only-upgrade')
    argv.append(package + '=' + candidate)
    return {'target': version, 'packageVersion': candidate, 'channel': 'apt',
            'argv': _elevated(argv), 'env': dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')}


ARCH_PUBLISHERS = {
    'cursor-desktop': ('https://downloads.cursor.com/keys/anysphere.asc',
                       '380FF4BCDC34A4BD92A3565342A1772E62E492D6'),
    'devin-desktop': ('https://windsurf-stable.codeiumdata.com/wVxQEIWkwPUEAGf3/windsurf.gpg',
                      'EE2C46CC44E1578C23135B0E9E608A1B9FD62A3F'),
}
MAX_ARTIFACT = 1024 * 1024 * 1024
MAX_UNPACKED = 1800 * 1024 * 1024


def _digest(file):
    value = hashlib.sha256()
    with pathlib.Path(file).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _download(url, destination, limit, command):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment:
        raise core.GuardError('official-native-https-required')
    # No redirects: every actual payload origin must remain the approved URL.
    _checked(command, ['curl', '--fail', '--silent', '--show-error', '--proto', '=https',
                      '--max-redirs', '0', '--max-time', '180', '--max-filesize', str(limit),
                      '--output', str(destination), url], timeout=190)
    file = _file(destination, limit)
    return _digest(file), file.stat().st_size


def _fields(text):
    value = {}
    for line in text.splitlines():
        if line[:1].isspace() or ': ' not in line:
            continue
        key, entry = line.split(': ', 1)
        if key in value:
            raise core.GuardError('native-package-fields-duplicated')
        value[key] = entry
    return value


def arch_release(product, architecture, command):
    """Authenticate stable package metadata without downloading the app.

    Cursor/Devin use the pinned publisher -> InRelease -> Packages -> SHA256
    chain. Grok's documented feed has no detached digest: preserve that HTTPS
    trust distinction and verify its exact commit URL and payload afterwards.
    """
    deb_arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(architecture)
    if not deb_arch:
        raise core.GuardError('native-arch-package-architecture-unverified')
    with tempfile.TemporaryDirectory(prefix='modelhub-desktop-metadata-') as temporary:
        stage = pathlib.Path(temporary)
        if product == 'grokbot':
            target = 'x64' if deb_arch == 'amd64' else 'arm64'
            feed = 'https://api2.cursor.sh/updates/api/download/stable/linux-' + target + '/sand'
            file = stage / 'release.json'
            _download(feed, file, 65536, command)
            value = _json(file)
            version_tuple(value.get('version'))
            commit = value.get('commitSha', '')
            if not re.fullmatch(r'[a-f0-9]{40}', commit):
                raise core.GuardError('official-grokbot-commit-unverified')
            expected = ('https://downloads.cursor.com/grokbot/stable/' + commit + '/linux/' + target +
                        '/grok-bot_' + value['version'] + '_' + deb_arch + '.deb')
            if value.get('debUrl') != expected:
                raise core.GuardError('official-grokbot-package-url-unverified')
            return {'target': value['version'], 'url': expected, 'sha256': None, 'size': None,
                    'architecture': architecture, 'integrity': 'official-https-feed-commit-payload-sha256'}
        key_url, fingerprint = ARCH_PUBLISHERS[product]
        origin = PRODUCTS[product]['apt']
        key, signed, release, packages = [stage / name for name in ('publisher.key', 'InRelease', 'Release', 'Packages')]
        _download(key_url, key, 128 * 1024, command)
        gpg = stage / 'gnupg'
        gpg.mkdir(mode=0o700)
        common = ['gpg', '--batch', '--no-options', '--no-autostart', '--homedir', str(gpg)]
        shown = _checked(command, [*common, '--with-colons', '--show-keys', str(key)])
        fingerprints = [row.split(':')[9] for row in shown.splitlines() if row.startswith('fpr:')]
        if not fingerprints or fingerprints[0] != fingerprint:
            raise core.GuardError('official-publisher-key-fingerprint-mismatch')
        _checked(command, [*common, '--import', str(key)])
        _download(origin + '/dists/stable/InRelease', signed, 128 * 1024, command)
        verified = _checked(command, [*common, '--no-auto-key-retrieve', '--status-fd', '1',
                                     '--output', str(release), '--decrypt', str(signed)])
        signatures = [row.split()[2:] for row in verified.splitlines() if row.startswith('[GNUPG:] VALIDSIG ')]
        if not any(fingerprint in row for row in signatures):
            raise core.GuardError('official-release-signature-unverified')
        text = _file(release).read_text()
        fields = _fields(text)
        if fields.get('Codename') != 'stable':
            raise core.GuardError('official-stable-release-required')
        published = parsedate_to_datetime(fields['Date'])
        age = (datetime.datetime.now(datetime.timezone.utc) - published).total_seconds()
        if not -3600 <= age <= 7 * 86400:
            raise core.GuardError('official-release-freshness-unverified')
        match = re.search(r'^SHA256:\n((?:[ \t].*\n?)+)', text, re.M)
        name = 'main/binary-' + deb_arch + '/Packages'
        entries = [row.split() for row in match[1].splitlines() if row.strip().endswith(' ' + name)] if match else []
        if (len(entries) != 1 or len(entries[0]) != 3 or not re.fullmatch(r'[a-f0-9]{64}', entries[0][0])
                or not entries[0][1].isdigit() or not 0 < int(entries[0][1]) <= MAX_METADATA):
            raise core.GuardError('official-packages-checksum-unverified')
        digest, size, _ = entries[0]
        actual, count = _download(origin + '/dists/stable/' + name, packages, int(size), command)
        if actual != digest or count != int(size):
            raise core.GuardError('official-packages-checksum-mismatch')
        wanted = PRODUCTS[product]['packages'][0]
        entries = [_fields(block) for block in packages.read_text().split('\n\n')]
        selected = [row for row in entries if row.get('Package') == wanted and row.get('Architecture') == deb_arch]
        if len(selected) != 1:
            raise core.GuardError('official-stable-package-ambiguous')
        row = selected[0]
        version = _package_stable_version(row.get('Version', ''))
        filename = row.get('Filename', '')
        if (not re.fullmatch(r'pool/[A-Za-z0-9_./+-]+\.deb', filename) or '..' in pathlib.PurePosixPath(filename).parts
                or not re.fullmatch(r'[a-f0-9]{64}', row.get('SHA256', ''))
                or not row.get('Size', '').isdigit() or not 1024 <= int(row['Size']) <= MAX_ARTIFACT):
            raise core.GuardError('official-native-package-integrity-unverified')
        return {'target': version, 'url': origin + '/' + filename, 'sha256': row['SHA256'],
                'size': int(row['Size']), 'architecture': architecture,
                'integrity': 'pinned-publisher-inrelease-packages-sha256'}


def _pacman_sections(text):
    result = {}
    for block in text.strip().split('\n\n'):
        rows = block.splitlines()
        if not rows or not re.fullmatch(r'%[A-Z_]+%', rows[0]) or rows[0].strip('%') in result:
            raise core.GuardError('native-pacman-metadata-invalid')
        result[rows[0].strip('%')] = rows[1:]
    return result


def _arch_receipt(package, command):
    receipt = _checked(command, ['pacman', '-Q', package]).split()
    if len(receipt) != 2 or receipt[0] != package or not re.fullmatch(r'[A-Za-z0-9_.+:-]+', receipt[1]):
        raise core.GuardError('native-pacman-receipt-unverified')
    database = pathlib.Path('/var/lib/pacman/local') / (package + '-' + receipt[1])
    info = database.lstat()
    if database.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise core.GuardError('native-pacman-database-unprotected')
    desc = _pacman_sections(_file(database / 'desc').read_text())
    if desc.get('NAME') != [package] or desc.get('VERSION') != [receipt[1]] or desc.get('ARCH') not in (['x86_64'], ['aarch64']):
        raise core.GuardError('native-pacman-package-identity-mismatch')
    return {'package': package, 'packageVersion': receipt[1], 'database': str(database), 'desc': desc}


def _arch_plan(product, app, package, command):
    receipt = _arch_receipt(package, command)
    if version_tuple(_package_stable_version(receipt['packageVersion'])) != version_tuple(app['version']):
        raise core.GuardError('native-package-version-identity-mismatch')
    release = arch_release(product, receipt['desc']['ARCH'][0], command)
    return {'target': release['target'], 'channel': 'pacman-vendor', 'integrity': release['integrity'],
            '_arch': dict(receipt, release=release)}


def arch_extract(artifact, destination, product, command):
    """Unwrap only data.tar; never execute Debian maintainer scripts."""
    members = _checked(command, ['bsdtar', '-tf', str(artifact)]).splitlines()
    payloads = [name for name in members if re.fullmatch(r'data\.tar\.(?:xz|gz|zst)', name)]
    if len(payloads) != 1:
        raise core.GuardError('official-deb-payload-layout-unverified')
    payload = artifact.parent / payloads[0]
    _checked(command, ['bsdtar', '-xf', str(artifact), '-C', str(artifact.parent), payloads[0]], timeout=120)
    _file(payload, MAX_ARTIFACT)
    # Python 3.14 also understands zstd tar files. On older interpreters a
    # missing decoder queues safely rather than invoking a shell or GUI.
    total, count, roots = 0, 0, set()
    with tarfile.open(payload, mode='r:*') as archive:
        for member in archive:
            count += 1
            name = member.name.removeprefix('./')
            if count > 100000 or member.size < 0:
                raise core.GuardError('official-payload-entry-bound-exceeded')
            pure = pathlib.PurePosixPath(name)
            if pure.is_absolute() or '..' in pure.parts:
                raise core.GuardError('official-payload-path-escape')
            matched = [base.lstrip('/') for base in PRODUCTS[product]['roots']
                       if name == base.lstrip('/') or name.startswith(base.lstrip('/') + '/')]
            if not matched:
                continue
            if len(matched) != 1:
                raise core.GuardError('official-payload-root-ambiguous')
            roots.add(matched[0])
            relative = pure.relative_to(matched[0])
            target = destination.joinpath(*relative.parts)
            if not target.parent.resolve().is_relative_to(destination.resolve()) and target != destination:
                raise core.GuardError('official-payload-link-escape')
            total += member.size
            if total > MAX_UNPACKED or member.islnk() or not (member.isdir() or member.isfile() or member.issym()):
                raise core.GuardError('official-payload-size-or-type-unverified')
            sandbox = member.isfile() and str(relative) == 'chrome-sandbox' and member.mode == 0o4755
            if member.mode & 0o7000 and not sandbox:
                raise core.GuardError('official-payload-privileged-mode-unverified')
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.issym():
                if (pathlib.PurePosixPath(member.linkname).is_absolute()
                        or not (target.parent / member.linkname).resolve().is_relative_to(destination.resolve())):
                    raise core.GuardError('official-payload-link-escape')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    raise core.GuardError('official-payload-duplicate-path')
                source = archive.extractfile(member)
                if source is None:
                    raise core.GuardError('official-payload-body-unavailable')
                with source, target.open('xb') as output:
                    remaining = member.size
                    while remaining:
                        block = source.read(min(1024 * 1024, remaining))
                        if not block:
                            raise core.GuardError('official-payload-body-truncated')
                        output.write(block)
                        remaining -= len(block)
                target.chmod(0o4755 if sandbox else member.mode & 0o777)
    if len(roots) != 1:
        raise core.GuardError('official-payload-root-ambiguous')


def _elf_fuse(binary, architecture):
    file = _file(binary, 512 * 1024 * 1024)
    sentinel = b'dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX'
    wires, tail = {}, b''
    with file.open('rb') as stream:
        header = stream.read(64)
        machine = {'x86_64': 62, 'aarch64': 183}[architecture]
        if len(header) != 64 or header[:7] != b'\x7fELF\x02\x01\x01' or struct.unpack('<H', header[18:20])[0] != machine:
            raise core.GuardError('official-native-elf-architecture-unverified')
        stream.seek(0)
        while block := stream.read(1024 * 1024):
            data = tail + block
            offset = 0
            while (found := data.find(sentinel, offset)) >= 0:
                wire = data[found + len(sentinel):found + len(sentinel) + 34]
                if len(wire) >= 2 and wire[0] == 1 and 1 <= wire[1] <= 32 and len(wire) >= 2 + wire[1]:
                    states = wire[2:2 + wire[1]]
                    if all(state in (0x30, 0x31, 0x72, 0x90) for state in states):
                        wires[stream.tell() - len(data) + found] = states
                offset = found + len(sentinel)
            if len(wires) > 1:
                raise core.GuardError('official-electron-fuse-ambiguous')
            tail = data[-(len(sentinel) + 33):]
    if len(wires) != 1:
        raise core.GuardError('official-electron-fuse-unverified')
    return next(iter(wires.values()))[0] == 0x31


def arch_runtime(product, vendor, architecture, system_electron, command):
    names = ('grok-bot',) if product == 'grokbot' else (('cursor',) if product == 'cursor-desktop' else ('devin-desktop', 'windsurf'))
    binaries = [vendor / name for name in names if (vendor / name).is_file()]
    required = ('resources.pak', 'icudtl.dat', 'libffmpeg.so', 'locales/en-US.pak')
    if len(binaries) != 1 or any(_file(vendor / name, MAX_ARTIFACT).stat().st_size == 0 for name in required):
        raise core.GuardError('official-electron-bundle-incomplete')
    enabled = _elf_fuse(binaries[0], architecture)
    if product == 'grokbot':
        if system_electron:
            raise core.GuardError('native-grokbot-system-runtime-unsupported')
        _asar(vendor / 'resources/app.asar')
        return {'runAsNodeEnabled': enabled, 'binaryExecuted': False}
    if system_electron:
        # Never execute an Electron binary until its static fuse proves that
        # ELECTRON_RUN_AS_NODE cannot open the application.
        runtime = pathlib.Path(system_electron)
        if not enabled or not _elf_fuse(runtime, architecture):
            raise core.GuardError('safe-electron-node-probe-unavailable')
        env = dict(os.environ, ELECTRON_RUN_AS_NODE='1')
        probe = 'JSON.stringify({electron:process.versions.electron,modules:process.versions.modules})'
        official = json.loads(_checked(command, [str(binaries[0]), '-p', probe], env=env))
        native = json.loads(_checked(command, [str(runtime), '-p', probe], env=env))
        if (not re.fullmatch(r'\d+\.\d+\.\d+', official.get('electron', ''))
                or not re.fullmatch(r'\d+', official.get('modules', ''))
                or official.get('modules') != native.get('modules')
                or official.get('electron', '').split('.')[0] != native.get('electron', '').split('.')[0]):
            raise core.GuardError('native-system-electron-abi-upgrade-required')
    return {'runAsNodeEnabled': enabled, 'binaryExecuted': bool(system_electron)}


def _retained_identity(path, concurrent=False):
    info = path.lstat()
    value = {'mode': stat.S_IMODE(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid}
    if stat.S_ISREG(info.st_mode):
        value.update(kind='file', sha256=_digest(path))
    elif stat.S_ISLNK(info.st_mode):
        value.update(kind='link', target=os.readlink(path))
    elif stat.S_ISDIR(info.st_mode):
        value.update(kind='directory')
    else:
        raise core.GuardError('native-retained-file-type-unverified')
    if concurrent and not stat.S_ISDIR(info.st_mode):
        value.update(device=info.st_dev, inode=info.st_ino, ctime=info.st_ctime_ns)
    return value


def _arch_launcher(product, app, owner, command):
    name = {'cursor-desktop': 'cursor', 'devin-desktop': 'devin-desktop', 'grokbot': 'grok-bot'}[product]
    launcher = pathlib.Path('/usr/bin') / name
    target = launcher.resolve(strict=True)
    if _checked(command, ['pacman', '-Qoq', str(launcher)]) != owner:
        raise core.GuardError('native-launcher-owner-unverified')
    root = pathlib.Path(app['root'])
    # Packaged Grok uses a flags-preserving /usr/bin shell wrapper.
    if product == 'grokbot' and target == launcher:
        body = _file(target, 128 * 1024).read_bytes()
        if (not body.startswith(b'#!/bin/bash\n') or b'GROK_BOT_BIN="/opt/Grok Bot/grok-bot"' not in body
                or b'exec "$GROK_BOT_BIN"' not in body or b'grok-bot-flags.conf' not in body
                or b'--no-sandbox' in body):
            raise core.GuardError('native-launcher-layout-unverified')
        return False
    if not target.is_relative_to(root) or _checked(command, ['pacman', '-Qoq', str(target)]) != owner:
        raise core.GuardError('native-launcher-target-owner-unverified')
    with _file(target, 512 * 1024 * 1024).open('rb') as stream:
        body = stream.read(128 * 1024)
    if b'--no-sandbox' in body:
        raise core.GuardError('native-launcher-sandbox-policy-unverified')
    if body.startswith(b'\x7fELF'):
        return False
    if (body.startswith(b'#!') and b'ELECTRON_RUN_AS_NODE=1' in body
            and any(name in body for name in (b'cursor-flags.conf', b'devin-desktop-flags.conf'))):
        majors = set(re.findall(rb'(?<![A-Za-z0-9_-])(?:/usr/bin/|/usr/lib/)?electron(\d+)(?:/electron)?(?=[\s"\'])', body))
        if len(majors) != 1:
            raise core.GuardError('native-system-electron-launcher-ambiguous')
        major = next(iter(majors)).decode('ascii')
        return pathlib.Path('/usr/lib/electron' + major + '/electron')
    if target.parent == root / 'bin' and body.startswith(b'#!/') and b'ELECTRON_RUN_AS_NODE' in body:
        return False
    raise core.GuardError('native-launcher-layout-unverified')


def arch_package(product, app, receipt, release, vendor, output, command):
    owner, desc = receipt['package'], receipt['desc']
    files_db = _pacman_sections(_file(pathlib.Path(receipt['database']) / 'files').read_text())
    paths = ['/' + name for name in files_db.get('FILES', [])]
    if (not paths or len(paths) > 100000 or any(not name.startswith(('/usr/', '/opt/', '/etc/'))
            or '..' in pathlib.PurePosixPath(name).parts for name in paths)):
        raise core.GuardError('native-package-file-layout-unverified')
    checked = _checked(command, ['pacman', '-Qkk', owner], timeout=120)
    if not re.search(r'\b0 altered files\b', checked):
        raise core.GuardError('native-package-custom-modification-preserved')
    system_electron = _arch_launcher(product, app, owner, command)
    if system_electron:
        runtime_package = pathlib.Path(system_electron).parent.name
        dependencies = [re.split(r'[<>=]', value, maxsplit=1)[0] for value in desc.get('DEPENDS', [])]
        if runtime_package not in dependencies:
            raise core.GuardError('native-system-electron-dependency-unverified')
    root = pathlib.Path(app['root'])
    scope = root / 'resources/app' if system_electron else root
    payload = vendor / 'resources/app' if system_electron else vendor
    arch_runtime(product, vendor, desc['ARCH'][0], system_electron, command)
    retained = [pathlib.Path(name) for name in paths if pathlib.Path(name) != scope and not pathlib.Path(name).is_relative_to(scope)]
    if system_electron and product == 'cursor-desktop':
        shim = root / 'resources/app/cursor.mjs'
        body = _file(shim, 128 * 1024).read_bytes()
        if (not body.startswith(('#!' + str(system_electron) + '\n').encode())
                or b'await import(appPath + "/out/main.js");' not in body or (payload / 'cursor.mjs').exists()):
            raise core.GuardError('native-cursor-entry-shim-unverified')
        retained.append(shim)
    if not system_electron:
        retained += [root / 'bin' / name for name in ('cursor', 'devin-desktop', 'windsurf') if str(root / 'bin' / name) in paths]
    retained = list(dict.fromkeys(retained))
    snapshots = {str(path): {'content': _retained_identity(path), 'concurrent': _retained_identity(path, True)} for path in retained}
    stamp = int(time.time())
    info = ['pkgname = ' + owner, 'pkgbase = ' + owner, 'pkgver = ' + release['target'] + '-1',
            'pkgdesc = ' + ' '.join(desc.get('DESC', [owner])), 'arch = ' + desc['ARCH'][0],
            'builddate = ' + str(stamp), 'packager = ModelHub (official vendor payload)']
    if ':' in receipt['packageVersion']:
        info.append('epoch = ' + receipt['packageVersion'].split(':', 1)[0])
    for section, key in [('LICENSE', 'license'), ('DEPENDS', 'depend'), ('OPTDEPENDS', 'optdepend'),
                         ('PROVIDES', 'provides'), ('CONFLICTS', 'conflict'), ('REPLACES', 'replaces')]:
        info += [key + ' = ' + value for value in desc.get(section, [])]
    info += ['backup = ' + value.split('\t', 1)[0] for value in files_db.get('BACKUP', [])]
    files = [(path, pathlib.Path(str(path).lstrip('/'))) for path in retained]
    replaced = set(retained)
    for current, folders, names in os.walk(payload, followlinks=False):
        if len(files) + len(folders) + len(names) > 100000:
            raise core.GuardError('native-package-file-bound-exceeded')
        for name in [*folders, *names]:
            file = pathlib.Path(current) / name
            target = scope / file.relative_to(payload)
            if target not in replaced:
                files.append((file, pathlib.Path(str(target).lstrip('/'))))
    files.append((payload, pathlib.Path(str(scope).lstrip('/'))))
    size = sum(file.stat().st_size for file, _ in files if file.is_file() and not file.is_symlink())
    if size > MAX_UNPACKED:
        raise core.GuardError('native-package-unpacked-bound-exceeded')
    info.append('size = ' + str(size))
    mtree = ['#mtree']
    def escaped(value):
        return ''.join('\\%03o' % ord(char) if char.isspace() or char in '\\#=' else char for char in value)
    with tarfile.open(output, mode='w') as archive:
        def body(name, content):
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(content), 0o644, stamp
            archive.addfile(member, io.BytesIO(content))
        body('.PKGINFO', ('\n'.join(info) + '\n').encode())
        for file, destination in files:
            archive.inodes.clear()
            member = archive.gettarinfo(str(file), arcname=str(destination))
            if file.is_relative_to(vendor):
                member.uid = member.gid = 0
                member.uname = member.gname = 'root'
            values = ['./' + escaped(member.name), 'uid=' + str(member.uid), 'gid=' + str(member.gid),
                      'mode=' + format(member.mode, 'o'), 'time=' + str(int(member.mtime))]
            if member.isdir():
                values.append('type=dir')
            elif member.issym():
                values += ['type=link', 'link=' + escaped(member.linkname)]
            elif member.isfile():
                values += ['type=file', 'size=' + str(member.size), 'sha256digest=' + _digest(file)]
            else:
                raise core.GuardError('native-package-file-type-unverified')
            mtree.append(' '.join(values))
            if member.isfile():
                with file.open('rb') as stream:
                    archive.addfile(member, stream)
            else:
                archive.addfile(member)
        body('.MTREE', gzip.compress(('\n'.join(mtree) + '\n').encode(), compresslevel=1, mtime=0))
    return snapshots


def arch_apply(product, app, plan, command, processes=None, mutation=core.run_mutation):
    receipt, release = plan['_arch'], plan['_arch']['release']
    lock = pathlib.Path('/var/lib/pacman/db.lck')
    boot = pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if lock.exists():
        raise core.GuardError('native-pacman-writer-active')
    with tempfile.TemporaryDirectory(prefix='modelhub-desktop-package-') as temporary:
        stage = pathlib.Path(temporary)
        artifact, vendor, package = stage / 'vendor.deb', stage / 'vendor', stage / 'native.pkg.tar'
        digest, size = _download(release['url'], artifact, release.get('size') or MAX_ARTIFACT, command)
        if ((release.get('sha256') and digest != release['sha256'])
                or (release.get('size') and size != release['size'])):
            raise core.GuardError('official-native-artifact-checksum-mismatch')
        vendor.mkdir()
        arch_extract(artifact, vendor, product, command)
        native = native_metadata(product, vendor, 'Linux')
        if native['version'] != release['target']:
            raise core.GuardError('official-vendor-payload-version-mismatch')
        retained = arch_package(product, app, receipt, release, vendor, package, command)
        _checked(command, ['pacman', '-Qip', str(package)])
        # All checks happen again after the potentially lengthy extraction.
        if (pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() != boot
                or _arch_receipt(receipt['package'], command) != {key: value for key, value in receipt.items() if key != 'release'}
                or native_metadata(product, app['root'], 'Linux') != app
                or process_state(product, app, 'Linux', command, processes) != 'idle' or lock.exists()
                or any(_retained_identity(pathlib.Path(name), True) != snapshot['concurrent']
                       for name, snapshot in retained.items())):
            raise core.GuardError('native-arch-pretransaction-state-changed')
        result = _mutate(mutation, _elevated(['pacman', '-U', '--needed', '--noconfirm', str(package)]), timeout=240)
        if result['exit']:
            return result
        if any(_retained_identity(pathlib.Path(name)) != snapshot['content'] for name, snapshot in retained.items()):
            raise core.GuardError('native-retained-file-postverification-failed')
        return dict(result, artifactSha256=digest)


def _rpm_plan(product, app, package, command, refresh=False, mutation=core.run_mutation):
    if package not in PRODUCTS[product]['packages']:
        raise core.GuardError('native-package-owner-unverified')
    # The exact enabled owning repository must retain vendor GPG verification.
    rows = (_checked_mutation if refresh else _checked)(mutation if refresh else command, ['dnf', '-q', *(['--refresh'] if refresh else ['--cacheonly']),
                             'repoquery', '--latest-limit=1', '--qf',
                             '%{name}|%{version}|%{release}|%{repoid}', package]).splitlines()
    parsed = [row.split('|') for row in rows if row.startswith(package + '|')]
    if len(parsed) != 1 or len(parsed[0]) != 4:
        raise core.GuardError('stable-rpm-candidate-unverified')
    _, version, release, repository = parsed[0]
    version_tuple(version)
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', repository) or not re.fullmatch(r'[A-Za-z0-9_.+]+', release):
        raise core.GuardError('stable-rpm-candidate-unverified')
    text = _checked(command, ['dnf', '-q', 'config-manager', '--dump', repository])
    fields = dict(re.findall(r'^\s*([a-z_]+)\s*=\s*(.*?)\s*$', text, re.M))
    origin = fields.get('baseurl', '').rstrip('/')
    approved_origin = (approved_url(product, origin) if product == 'grokbot'
                       else origin == PRODUCTS[product]['rpm'])
    if (fields.get('enabled') not in ('1', 'True', 'true') or fields.get('gpgcheck') not in ('1', 'True', 'true')
            or not approved_origin):
        raise core.GuardError('existing-signed-rpm-repository-unverified')
    current = _checked(command, ['rpm', '-q', '--qf', '%{VERSION}', package])
    if version_tuple(current) != version_tuple(app['version']):
        raise core.GuardError('native-package-version-identity-mismatch')
    return {'target': version, 'channel': 'dnf', 'argv': _elevated(
        ['dnf', '-y', '--disablerepo=*', '--enablerepo=' + repository,
         '--setopt=gpgcheck=1', 'upgrade', package + '-' + version + '-' + release])}


def _brew_plan(product, app, command, fetch, refresh=False, mutation=core.run_mutation):
    env = dict(os.environ, HOMEBREW_NO_AUTO_UPDATE='1', HOMEBREW_NO_INSTALL_CLEANUP='1',
               HOMEBREW_NO_UPGRADE_QUIT_CASKS='1')
    listed = _checked(command, ['brew', 'list', '--cask', '--versions'], env=env)
    tokens = [row.split()[0] for row in listed.splitlines() if row.split() and row.split()[0] in PRODUCTS[product]['casks']]
    if not app:
        if tokens:
            raise core.GuardError('native-homebrew-receipt-with-missing-app-preserved')
        return _brew_install_plan(product, command, fetch, env, refresh=refresh, mutation=mutation)
    for token in tokens:
        info = json.loads(_checked(command, ['brew', 'info', '--json=v2', '--cask', token], env=env))
        casks = info.get('casks', [])
        if len(casks) != 1:
            continue
        cask = casks[0]
        if cask.get('tap') != 'homebrew/cask':
            continue
        artifacts = [name for artifact in cask.get('artifacts', []) for name in artifact.get('app', []) if isinstance(name, str)]
        if pathlib.Path(app['root']).name not in artifacts:
            continue
        caskroom = pathlib.Path(_checked(command, ['brew', '--caskroom', token], env=env))
        installed = cask.get('installed')
        versions = installed if isinstance(installed, list) else [installed] if isinstance(installed, str) else []
        owned = any((caskroom / version / pathlib.Path(app['root']).name).is_symlink()
                    and (caskroom / version / pathlib.Path(app['root']).name).resolve() == pathlib.Path(app['root'])
                    for version in versions if isinstance(version, str) and '/' not in version)
        if not owned:
            continue
        latest = _fetch(fetch, 'https://formulae.brew.sh/api/cask/' + token + '.json')
        version_tuple(latest.get('version'))
        if not approved_url(product, latest.get('url')) or latest.get('tap') != 'homebrew/cask':
            raise core.GuardError('native-homebrew-origin-unverified')
        plan = {'target': latest['version'], 'channel': 'homebrew-cask'}
        if version_tuple(latest['version']) <= version_tuple(app['version']):
            return plan
        if latest.get('sha256') == 'no_check':
            raise Deferred('native-homebrew-checksum-unavailable', latest['version'])
        if not re.fullmatch(r'[a-fA-F0-9]{64}', latest.get('sha256', '')):
            raise core.GuardError('native-homebrew-checksum-unverified')
        if any(cask.get(key) != latest.get(key) for key in ('version', 'url', 'sha256')):
            if not refresh:
                raise Deferred('native-homebrew-metadata-refresh-required', latest['version'])
            _checked_mutation(mutation, ['brew', 'update', '--quiet'], timeout=120, env=env)
            info = json.loads(_checked(command, ['brew', 'info', '--json=v2', '--cask', token], env=env))
            rows = info.get('casks', [])
            if (len(rows) != 1 or rows[0].get('tap') != 'homebrew/cask'
                    or any(rows[0].get(key) != latest.get(key) for key in ('version', 'url', 'sha256'))
                    or not any(pathlib.Path(app['root']).name in artifact.get('app', [])
                               for artifact in rows[0].get('artifacts', []))):
                raise core.GuardError('native-homebrew-refreshed-metadata-unverified')
        plan.update(sha256=latest['sha256'], argv=['brew', 'upgrade', '--cask', '--greedy',
                    '--no-quit', '--require-sha', '--skip-cask-deps', token], env=env)
        return plan
    raise core.GuardError('native-homebrew-receipt-does-not-own-bundle')


def _brew_install_plan(product, command, fetch, env, refresh=False, mutation=core.run_mutation):
    # Only an explicitly selected missing product reaches here. Grok Bot has
    # no verified cask adapter: never infer one from its display name.
    if product == 'grokbot':
        raise core.GuardError('explicit-native-install-channel-unavailable')
    for token in PRODUCTS[product]['casks']:
        try:
            latest = _fetch(fetch, 'https://formulae.brew.sh/api/cask/' + token + '.json')
        except (OSError, ValueError):
            continue
        if not isinstance(latest, dict) or latest.get('token') != token:
            continue
        if (latest.get('tap') != 'homebrew/cask' or not approved_url(product, latest.get('url'))
                or latest.get('disabled') or latest.get('deprecated')):
            continue
        version_tuple(latest.get('version'))
        if latest.get('sha256') == 'no_check':
            raise Deferred('native-homebrew-checksum-unavailable', latest['version'])
        if not re.fullmatch(r'[a-fA-F0-9]{64}', latest.get('sha256', '')):
            raise core.GuardError('native-homebrew-checksum-unverified')
        artifacts = latest.get('artifacts', [])
        # app/binary artifacts move an app and expose its CLI. Recipe hooks,
        # scripts, and installer packages cannot establish the no-start policy.
        if (not isinstance(artifacts, list) or not artifacts
                or any(not isinstance(row, dict) or not set(row) <= {'app', 'binary', 'zap'} for row in artifacts)):
            raise core.GuardError('native-homebrew-install-artifacts-unverified')
        apps = [name for row in artifacts for name in row.get('app', []) if isinstance(name, str)]
        if len(apps) != 1 or apps[0] not in tuple(name + '.app' for name in PRODUCTS[product]['names']):
            raise core.GuardError('native-homebrew-install-app-identity-unverified')
        # Exact matching local official metadata is required for execution;
        # dry runs never refresh Homebrew or change installed receipts.
        selected = 'homebrew/cask/' + token
        info = json.loads(_checked(command, ['brew', 'info', '--json=v2', '--cask', selected], env=env))
        rows = info.get('casks', [])
        keys = ('version', 'url', 'sha256', 'artifacts')
        matches = (len(rows) == 1 and rows[0].get('tap') == 'homebrew/cask'
                   and all(rows[0].get(key) == latest.get(key) for key in keys))
        if not matches:
            if not refresh:
                raise Deferred('native-homebrew-metadata-refresh-required', latest['version'])
            _checked_mutation(mutation, ['brew', 'update', '--quiet'], timeout=120, env=env)
            info = json.loads(_checked(command, ['brew', 'info', '--json=v2', '--cask', selected], env=env))
            rows = info.get('casks', [])
            if (len(rows) != 1 or rows[0].get('tap') != 'homebrew/cask'
                    or any(rows[0].get(key) != latest.get(key) for key in keys)):
                raise core.GuardError('native-homebrew-refreshed-metadata-unverified')
        if rows[0].get('installed'):
            raise core.GuardError('native-homebrew-installation-changed')
        return {'target': latest['version'], 'channel': 'homebrew-cask', 'sha256': latest['sha256'],
                'argv': ['brew', 'install', '--cask', '--require-sha', '--skip-cask-deps', selected], 'env': env}
    raise core.GuardError('explicit-native-install-channel-unavailable')


def grokbot_release(system, architecture, fetch):
    native = {'Darwin': 'darwin', 'Windows': 'win32', 'Linux': 'linux'}[system]
    target = native + '-' + architecture + ('-user' if system == 'Windows' else '')
    value = _fetch(fetch, 'https://api2.cursor.sh/updates/api/download/stable/' + target + '/sand')
    version_tuple(value.get('version'))
    if not approved_url('grokbot', value.get('downloadUrl')):
        raise core.GuardError('official-grokbot-release-url-unverified')
    return value['version']


def reconcile(product, *, home, state, install_missing=False, dry_run=False,
              system=None, architecture=None, command=core.run, fetch=core.http_json, processes=None,
              mutation=core.run_mutation):
    """Reconcile one selected desktop. Caller owns the global writer lock.

    ``processes`` is an optional 'idle'/'busy'/'unknown' value or callable(app)
    returning that value or {known: bool, active: bool}; useful for native tests.
    A single overwritten receipt contains only portable outcome metadata.
    """
    result = {'tool': product, 'status': 'queued', 'installed': False,
              'appsStartedStopped': False}
    def finish(value):
        if not dry_run:
            core.atomic_json(pathlib.Path(state) / 'native-desktop' / (product + '.json'), value)
        return value
    if product not in PRODUCTS:
        raise core.GuardError('unknown-native-desktop-product')
    system = system or platform.system()
    if system == 'Darwin':
        def mutation(*args, **kwargs):
            raise core.GuardError('vendor-mutation-custody-unavailable')
    architecture = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64'}.get(
        (architecture or platform.machine()).lower(), (architecture or platform.machine()).lower())
    if system not in ('Linux', 'Darwin', 'Windows'):
        return finish(dict(result, status='unsupported', reason='vendor-desktop-os-unsupported'))
    if architecture not in ('x64', 'arm64'):
        return finish(dict(result, status='unsupported', reason='vendor-desktop-architecture-unsupported'))
    try:
        if system == 'Darwin' and not dry_run:
            from . import macos_apps
            # Resolve the one exceptional prior transaction even when the app
            # is already current or the first explicit installation failed.
            macos_apps.cleanup_pending(product, home=home, state=state,
                                       command=command, processes=processes)
        app = discover(product, home, system, command)
        result['installed'] = app is not None
        if app:
            result['version'] = app['version']
        elif not install_missing:
            return finish(dict(result, status='missing', reason='explicit-install-selection-required'))
        if not dry_run:
            activity = process_state(product, app, system, command, processes)
            if activity != 'idle':
                raise core.GuardError('application-active' if activity == 'busy' else 'process-state-unknown')
        if product == 'grokbot':
            result['target'] = grokbot_release(system, architecture, fetch)
        if system == 'Darwin':
            try:
                plan = _brew_plan(product, app, command, fetch, refresh=not dry_run, mutation=mutation)
            except (FileNotFoundError, core.GuardError) as error:
                if isinstance(error, core.GuardError) and str(error) not in (
                        'native-homebrew-receipt-does-not-own-bundle',
                        'explicit-native-install-channel-unavailable'):
                    raise
                # Drag-installed signed apps do not become Homebrew installs.
                # Their own valid signature and exact native location remain
                # the ownership fence for an atomic same-volume DMG update.
                from . import macos_apps
                plan = macos_apps.plan(product, app, home=home, state=state,
                                        command=command, fetch=fetch, architecture=architecture)
        else:
            plan = (_windows_plan(product, app, architecture, command, refresh=not dry_run, mutation=mutation) if system == 'Windows'
                    else _linux_plan(product, app, command, refresh=not dry_run, mutation=mutation))
        result.update({key: value for key, value in plan.items() if key not in ('argv', 'env', 'url', '_arch', '_dmg')})
        if app and version_tuple(plan['target']) <= version_tuple(app['version']):
            result['status'] = 'current' if version_tuple(plan['target']) == version_tuple(app['version']) else 'newer-installation-preserved'
        elif dry_run:
            result['status'] = 'update-available' if app else 'install-available'
        else:
            for attempt in range(2):
                activity = process_state(product, app, system, command, processes)
                if activity != 'idle':
                    raise core.GuardError('application-active' if activity == 'busy' else 'process-state-unknown')
                fresh = discover(product, home, system, command)
                if fresh != app:
                    raise core.GuardError('native-installation-changed')
            if 'argv' not in plan and '_arch' not in plan and '_dmg' not in plan:
                raise core.GuardError('safe-native-transaction-unavailable')
            if '_dmg' in plan:
                from . import macos_apps
                updated = macos_apps.apply(product, app, plan, home=home, state=state,
                                           command=command, fetch=fetch, processes=processes)
            else:
                updated = (arch_apply(product, app, plan, command, processes, mutation=mutation) if '_arch' in plan else
                           _mutate(mutation, plan['argv'], timeout=300, env=plan.get('env')))
            for key in ('artifactSha256', 'sha256', 'digestSource'):
                if key in updated:
                    result[key] = updated[key]
            if updated['exit']:
                result.update(status='error', reason='native-quiet-update-failed', exit=updated['exit'])
            else:
                after = discover(product, home, system, command)
                if not after or version_tuple(after['version']) < version_tuple(plan['target']):
                    result.update(status='error', reason='native-installed-version-verification-failed')
                else:
                    result.update(status='updated' if app else 'installed', installed=True, version=after['version'])
    except core.GuardError as error:
        result.update(status='queued', reason=str(error))
        if isinstance(error, Deferred):
            result['target'] = error.target
    except (OSError, ValueError, KeyError, TypeError, AttributeError, tarfile.TarError):
        result.update(status='queued', reason='native-metadata-unavailable')
    return finish(result)
