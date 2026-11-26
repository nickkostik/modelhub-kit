"""Stage registry packages in a confined macOS process before switching an owned launcher."""
import hashlib
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import tempfile

from ..cache import process_inventory
from ..core import GuardError, atomic_json, http_json, private_directory, protected, protected_runtime_path, read_json, run
from ..mutation import _follow_signals
from .products import _directory_binding, _launcher_snapshot

REGISTRY = 'https://registry.npmjs.org'
RECEIPT = '.modelhub-kit-npm.json'
FORMAT = 'modelhub-kit-macos-npm-v1'
OPERATION = 'operation.json'
# DeepSeek's package alone contains over 100,000 entries. This walk is
# reserved for a newly built generation, recovery, or retirement.
MAX_RELEASE_ENTRIES = 250_000


def package_file(release, definition):
    return release.joinpath('node_modules', *definition['package'].split('/'), 'package.json')


def entrypoint(release, definition):
    from ..updater import npm_native_metadata
    manifest = package_file(release, definition)
    entry = release / 'node_modules/.bin' / definition['command']
    if not entry.is_symlink():
        raise GuardError('managed-npm-bin-missing')
    target = entry.resolve(strict=True)
    if manifest.parent not in target.parents:
        raise GuardError('managed-npm-bin-unverified')
    identity = npm_native_metadata(str(target), definition)
    if not identity or pathlib.Path(identity['metadata']) != manifest:
        raise GuardError('managed-npm-bin-unverified')
    return target, identity['version']


def release_receipt(release, definition):
    if not (release / RECEIPT).is_file():
        return None
    protected(release, True)
    row = read_json(release / RECEIPT, limit=8192)
    if not isinstance(row, dict) or row.get('format') != FORMAT or row.get('package') != definition['package']:
        raise GuardError('managed-npm-release-receipt-unverified')
    entry, version = entrypoint(release, definition)
    if (row.get('version') != version or row.get('entry') != entry.relative_to(release).as_posix()
            or not re.fullmatch(r'[a-f0-9]{64}', row.get('tree', ''))):
        raise GuardError('managed-npm-release-receipt-unverified')
    return row


def installed_receipt(name, definition, home, state, command):
    launcher = home / '.local/bin' / definition['command']
    if not launcher.is_symlink() or command and pathlib.Path(command).absolute() != launcher:
        return None
    root = state / 'npm-releases' / name
    target = launcher.resolve(strict=True)
    if root not in target.parents:
        return None
    _launcher_snapshot(launcher)
    release = root / target.relative_to(root).parts[0]
    row = release_receipt(release, definition)
    if row and target == release / row['entry']:
        return {**row, 'release': release, 'launcher': launcher}
    return None


def tree_identity(release):
    """Compact ownership evidence for deleting an unchanged package tree."""
    digest = hashlib.sha256()
    count = 0
    def unreadable(error):
        raise error
    for base, directories, files in os.walk(release, followlinks=False, onerror=unreadable):
        directories.sort()
        for part in sorted(directories + files):
            file = pathlib.Path(base) / part
            if file == release / RECEIPT:
                continue
            count += 1
            info = file.lstat()
            if count > MAX_RELEASE_ENTRIES:
                raise GuardError('managed-npm-tree-entry-budget')
            if info.st_uid != os.getuid():
                raise GuardError('managed-npm-tree-unverified')
            target = None
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(file)
                if os.path.isabs(target) or release not in file.resolve(strict=True).parents:
                    raise GuardError('managed-npm-tree-link-outside-release')
            elif not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or info.st_mode & 0o022:
                raise GuardError('managed-npm-tree-unverified')
            fields = (file.relative_to(release).as_posix(), info.st_dev, info.st_ino, info.st_mode,
                      info.st_size, info.st_mtime_ns, info.st_ctime_ns, target)
            digest.update(json.dumps(fields).encode() + b'\n')
    return digest.hexdigest()


def idle(definition, *roots):
    observed = process_inventory()
    if observed is None:
        return False
    command = re.compile(r'(?:^|[/\s])' + re.escape(definition['command']) + r'(?:[./\s]|$)', re.I)
    return not any(command.search(line) or definition['package'] in line
                   or any(str(root) in line for root in roots) for line in observed)


def retire(release, receipt, launcher, definition):
    if not idle(definition, release) or tree_identity(release) != receipt['tree']:
        return False
    # Another shell alias into the old package is still a reference.
    for entry in launcher.parent.iterdir():
        if entry.is_symlink() and release in entry.resolve().parents:
            return False
    handles = run(['/usr/sbin/lsof', '-nP', '-a', '-u', str(os.getuid()), '-Fn'],
                  timeout=6, output_limit=4 * 1024**2)
    if handles['exit'] or handles['outputTruncated'] or str(release) in handles['output']:
        return False
    if not idle(definition, release):
        return False
    shutil.rmtree(release)
    return True


def finish_promotion(stage, launcher, definition, *, dry_run):
    operation = read_json(stage / OPERATION, limit=8192)
    answer = {'status': 'queued', 'reason': 'macos-npm-stage-outcome-unverified', 'installed': False}
    if (not isinstance(operation, dict) or operation.get('stage') != _directory_binding(stage)
            or operation.get('status') != 'verified'
            or not re.fullmatch(r'\d+\.\d+\.\d+[A-Za-z0-9.-]{0,100}-[a-f0-9]{12}(?:-repair)?', operation.get('release', ''))):
        return answer
    release = stage.parent / operation['release']
    project = stage / 'package'
    where = release if os.path.lexists(release) else project
    row = release_receipt(where, definition)
    if not row or row != operation.get('candidate') or row['tree'] != tree_identity(where):
        raise GuardError('npm-promotion-package-changed')
    target = release / row['entry']
    observed = _launcher_snapshot(launcher)
    already_selected = where == release and observed and observed.get('target') == str(target)
    if dry_run:
        return dict(answer, reason='npm-promotion-pending')
    if not already_selected:
        if observed != operation['launcher']:
            raise GuardError('npm-launcher-changed')
        if operation['before'] and not idle(definition, where, operation['previous']):
            return dict(answer, reason='active-or-unknown-processes')
        if where == project:
            os.rename(project, release)
        if observed is None:
            os.symlink(str(target), launcher)
        else:
            with tempfile.TemporaryDirectory(prefix='.npm-launch-', dir=launcher.parent) as temporary:
                pending = pathlib.Path(temporary) / 'launcher'
                os.symlink(str(target), pending)
                if _launcher_snapshot(launcher) != observed:
                    raise GuardError('npm-launcher-changed')
                os.replace(pending, launcher)
    if launcher.resolve(strict=True) != target or _directory_binding(stage) != operation['stage']:
        raise GuardError('npm-launcher-readback-failed')
    shutil.rmtree(stage)
    before = operation['before']
    return {'status': 'updated' if before else 'installed', 'before': before,
            'version': row['version'], 'installed': True}


def stage_env(stage, npm):
    return {'HOME': str(stage), 'TMPDIR': str(stage), 'PATH': str(pathlib.Path(npm[0]).parent) + ':/usr/bin:/bin',
            'NPM_CONFIG_GLOBALCONFIG': '/dev/null', 'NODE_USE_SYSTEM_CA': '1', 'GROK_DISABLE_API_KEY_AUTH': '1'}


def unpack_npm(stage, package, version, npm, sandbox):
    project = stage / 'package'
    project.mkdir(mode=0o700)
    dependencies = {package: version}
    if package == '@deepseek-ai/dsh':
        dependencies['yaml'] = '^2.8.1'
    atomic_json(project / 'package.json', {'private': True, 'dependencies': dependencies})
    rule = '(version 1) (allow default) (deny process-fork) (deny file-write* (require-not (subpath '
    rule += json.dumps(str(stage), ensure_ascii=False) + ')))'
    argv = [str(sandbox), '-p', rule, *npm, 'install', '--ignore-scripts', '--engine-strict',
            '--allow-git=none', '--allow-remote=none', '--allow-file=none', '--allow-directory=none',
            '--no-audit', '--no-fund', '--logs-max=0', '--umask=0022',
            '--omit=dev', '--include=optional', '--registry=' + REGISTRY,
            '--fetch-retries=1', '--fetch-timeout=30000', '--cache=' + str(stage / 'cache')]
    # npm only expands registry data. Vendor hooks are off and the system
    # sandbox denies forks and writes outside the unpublished staging area.
    # Follow that same process to natural completion without a timeout kill.
    with _follow_signals():
        with subprocess.Popen(argv, env=stage_env(stage, npm), cwd=project,
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              close_fds=True, start_new_session=True) as child:
            tail = b''
            while block := child.stderr.read(4096):
                tail = (tail + block)[-16384:]
            code = child.wait()
    match = re.search(rb'\bnpm (?:error|ERR!) code ([A-Z0-9_]{2,40})\b', tail)
    diagnostic = '-' + match[1].decode('ascii').lower().replace('_', '-') if match else ''
    return project, code, diagnostic


def verify_native(release, definition, entry, version, home, npm):
    package = definition['package']
    env = stage_env(home, npm)
    if package in ('@deepseek-ai/dsh', '@getpaseo/cli'):
        # The registry archives already contain the platform PTY helper.
        # Restore the executable bit that the official lifecycle would set.
        source = r"""
const fs = require('node:fs'), path = require('node:path'), {createRequire} = require('node:module');
const [manifest, root, name] = process.argv.slice(1);
let from = createRequire(manifest);
if (name === '@deepseek-ai/dsh') {
  if (typeof from('yaml').parseDocument !== 'function') process.exit(1);
  const base = createRequire(from.resolve('@deepseek-ai/dsh-base/package.json'));
  from = createRequire(base.resolve('@deepseek-ai/dsh-subprocess-local/package.json'));
  from('koffi');
} else {
  from = createRequire(from.resolve('@getpaseo/server'));
}
const pty = from.resolve('node-pty');
const helper = path.resolve(path.dirname(pty), '..', 'prebuilds', 'darwin-' + process.arch, 'spawn-helper');
if (!helper.startsWith(root + path.sep) || fs.realpathSync(helper) !== helper || !fs.lstatSync(helper).isFile())
  process.exit(1);
if (!(fs.statSync(helper).mode & 0o100)) fs.chmodSync(helper, 0o755);
fs.accessSync(helper, fs.constants.X_OK);
from('node-pty');
"""
        runtime = run([npm[0], '-e', source, package_file(release, definition), release / 'node_modules', package],
                      env=env, timeout=15)
        if runtime['exit']:
            raise GuardError('native-npm-runtime-unavailable')
    result = run([entry, '--version'], env=env, timeout=15)
    if result['exit'] or not re.search(r'(?<![A-Za-z0-9.-])v?' + re.escape(version) + r'(?![A-Za-z0-9.-])', result['output']):
        raise GuardError('native-npm-version-unverified')


def reconcile(name, definition, home, state, command, *, legacy, install_missing, dry_run, repair):
    from ..updater import healthy_codex, npm_command, npm_metadata_version, npm_native_metadata, subscription_only_environment, version_tuple
    launcher = home / '.local/bin' / definition['command']
    root = state / 'npm-releases' / name
    current = installed_receipt(name, definition, home, state, command)
    before = current['version'] if current else None
    result = {'tool': name, 'installed': False}
    stage = root / '.prepare'
    if os.path.lexists(stage):
        protected(stage, True)
        if command and pathlib.Path(command).absolute() != launcher:
            return dict(result, status='unmanaged-installation-preserved')
        return {'tool': name, **finish_promotion(stage, launcher, definition, dry_run=dry_run), 'downloaded': False}
    if legacy and not current:
        if pathlib.Path(legacy['launcher']) != launcher or command and pathlib.Path(command).absolute() != launcher:
            return dict(result, status='unmanaged-installation-preserved')
        identity = npm_native_metadata(str(launcher), definition)
        prefix = pathlib.Path(legacy['prefix'])
        if not identity or prefix not in pathlib.Path(identity['metadata']).parents:
            return dict(result, status='unmanaged-installation-preserved')
        before = identity['version']
    if not before and (command or os.path.lexists(launcher)):
        return dict(result, status='unmanaged-installation-preserved')
    if not before and not install_missing:
        return dict(result, status='not-installed')
    launcher_before = _launcher_snapshot(launcher)
    package = definition['package']
    allow_prerelease = package == '@deepseek-ai/dsh'
    manifest, _ = http_json(REGISTRY + '/' + package.replace('/', '%2f') + '/latest')
    latest = npm_metadata_version(manifest, allow_prerelease=allow_prerelease)
    newer = before and version_tuple(before, allow_prerelease=allow_prerelease) > version_tuple(latest, allow_prerelease=allow_prerelease)
    if name == 'codex' and not repair and (newer or before == latest):
        metadata = package_file(current['release'], definition) if current else pathlib.Path(identity['metadata'])
        npm_command()
        if not healthy_codex(metadata, before, subscription_only_environment()):
            return dict(result, status='repair-needed', reason='native-platform-runtime-unavailable', version=before)
    if newer:
        return dict(result, status='newer-installation-preserved', version=before)
    if dry_run:
        if before == latest and not repair:
            return dict(result, status='current', version=before)
        return dict(result, status='planned', before=before, target=latest)
    if root.exists() and current:
        if protected(root, True).st_mode & 0o077:
            raise GuardError('npm-release-root-not-private')
        for index, old in enumerate(root.iterdir()):
            if index >= 64:
                raise GuardError('npm-release-inventory-budget')
            if old == current['release'] or not old.is_dir() or old.is_symlink():
                continue
            receipt = release_receipt(old, definition)
            if receipt and not retire(old, receipt, launcher, definition):
                if before == latest and not repair:
                    return dict(result, status='current', version=before, retirementQueued=True)
                return dict(result, status='queued', reason='superseded-npm-release-still-referenced', version=before)
    if before == latest and not repair:
        return dict(result, status='current', version=before)
    previous = current['release'] if current else state / 'npm'
    # Creating an empty launcher path has no incumbent runtime to replace.
    # Unrelated copies elsewhere on the machine can keep running.
    if before and not idle(definition, previous):
        return dict(result, status='queued', reason='active-or-unknown-processes')
    dist = manifest.get('dist', {})
    integrity = dist.get('integrity', '')
    url = REGISTRY + '/' + package + '/-/' + package.split('/')[-1] + '-' + latest + '.tgz'
    if (manifest.get('name') != package or dist.get('tarball') != url
            or not re.fullmatch(r'sha512-[A-Za-z0-9+/]{86}==', integrity)):
        raise GuardError('official-npm-integrity-unavailable')
    npm = npm_command()
    if len(npm) != 2 or not pathlib.Path('/usr/bin/sandbox-exec').is_file():
        raise GuardError('vendor-mutation-custody-unavailable')
    sandbox = protected_runtime_path('/usr/bin/sandbox-exec')
    private_directory(root)
    private_directory(launcher.parent)
    key = hashlib.sha256(integrity.encode()).hexdigest()[:12]
    release = root / (latest + '-' + key)
    if current and release == current['release']:
        release = release.with_name(release.name + '-repair')
    if os.path.lexists(release):
        raise GuardError('unselected-npm-release-preserved')
    stage.mkdir(mode=0o700)
    stage_id = _directory_binding(stage)
    operation = {'stage': stage_id, 'status': 'installing', 'release': release.name,
                 'launcher': launcher_before, 'before': before, 'previous': str(previous)}
    atomic_json(stage / OPERATION, operation, durable=True)
    terminal = False
    admitted = False
    try:
        project, code, diagnostic = unpack_npm(stage, package, latest, npm, sandbox)
        terminal = True
        if code:
            raise GuardError('confined-npm-install-failed' + diagnostic)
        lock = read_json(project / 'package-lock.json')
        resolved = lock.get('packages', {}).get('node_modules/' + package, {})
        if resolved.get('version') != latest or resolved.get('integrity') != integrity or resolved.get('resolved') != url:
            raise GuardError('official-npm-integrity-mismatch')
        entry, version = entrypoint(project, definition)
        if version != latest:
            raise GuardError('native-npm-version-unverified')
        verify_native(project, definition, entry, latest, stage, npm)
        relative = entry.relative_to(project)
        data = {'format': FORMAT, 'package': package, 'version': latest,
                'entry': relative.as_posix(), 'integrity': integrity, 'tree': tree_identity(project)}
        atomic_json(project / RECEIPT, data, durable=True)
        operation.update(status='verified', candidate=data)
        # A verified stage survives a late busy state or an interrupted
        # promotion. The next pass continues it without invoking npm again.
        admitted = True
        atomic_json(stage / OPERATION, operation, durable=True)
        return {'tool': name, **finish_promotion(stage, launcher, definition, dry_run=False), 'downloaded': True}
    finally:
        if terminal and not admitted and _directory_binding(stage) == stage_id:
            shutil.rmtree(stage)
