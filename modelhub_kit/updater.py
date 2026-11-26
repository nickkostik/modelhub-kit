"""Compare official versions; update one owned installation at a time."""
import json
import os
import pathlib
import re
import shutil
import sys
import hashlib
import platform
import stat
from .core import GuardError, atomic_json, atomic_text, http_json, locations, private_directory, protected, protected_runtime_path, read_json, run, run_mutation

SEMVER = re.compile(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?')
GROK_API_KEY_NAME = re.compile(r'(?:XAI|GROK|GROK_CODE|GROK_CODE_XAI)_API_KEY(?:_\d+)?|SUPER_GROK_PLAN')
UNVERIFIED_NPM_OWNERSHIP = frozenset({
    'unverified-npm-metadata-path', 'unverified-npm-metadata-owner',
    'unverified-npm-metadata-link', 'npm-metadata-changed',
})


def subscription_only_environment(environment=None):
    """Remove every supported Grok API-key fallback before vendor probes."""
    result = dict(os.environ if environment is None else environment)
    for name in list(result):
        if GROK_API_KEY_NAME.fullmatch(name):
            result.pop(name, None)
    result['GROK_DISABLE_API_KEY_AUTH'] = '1'
    proxy = result.get('GROK_CLI_CHAT_PROXY_BASE_URL', '')
    if re.match(r'^https?://api\.x\.ai(?:/|$)', proxy, re.I):
        result.pop('GROK_CLI_CHAT_PROXY_BASE_URL', None)
    return result


def tools():
    return json.loads(pathlib.Path(__file__).with_name('tools.json').read_text())


def npm_native_metadata(command, definition, *, allow_macos_opencode_hardlink=False):
    """Read an exact npm bin's metadata; this never grants update ownership."""
    if not command or (os.name == 'nt' and pathlib.Path(command).suffix.lower() in ('.cmd', '.ps1', '.bat')):
        return None
    native = pathlib.Path(command).resolve(strict=True)
    owners = {0, os.getuid()} if os.name != 'nt' else set()

    def checked(path, directory=False, *, native_command=False):
        value = path.lstat()
        if path.is_symlink() or path.resolve() != path or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)):
            raise GuardError('unverified-npm-metadata-path')
        if os.name != 'nt' and (value.st_uid not in owners or value.st_mode & 0o022):
            raise GuardError('unverified-npm-metadata-owner')
        # The official macOS OpenCode package hard-links its selected binary
        # to its platform package. Permit that only for read-only discovery;
        # mutation callers retain the stricter single-link requirement.
        read_only_opencode_link = (native_command and allow_macos_opencode_hardlink
                                  and platform.system() == 'Darwin'
                                  and definition.get('package') == '@opencode/cli'
                                  and value.st_nlink == 2)
        if not directory and value.st_nlink != 1 and not read_only_opencode_link:
            raise GuardError('unverified-npm-metadata-link')
        return value

    checked(native, native_command=True)
    for root in list(native.parents)[:8]:
        metadata = root / 'package.json'
        if not metadata.exists():
            continue
        checked(root, True)
        before = checked(metadata)
        if before.st_size > 65536:
            continue
        fd = os.open(metadata, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                raise GuardError('npm-metadata-changed')
            data = os.read(fd, 65537)
            after = os.fstat(fd)
            if len(data) != opened.st_size or len(data) > 65536 or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
                raise GuardError('npm-metadata-changed')
        finally:
            os.close(fd)
        value = json.loads(data)
        if not isinstance(value, dict) or value.get('name') != definition['package']:
            continue
        version = value.get('version')
        bins = value.get('bin')
        target = bins.get(definition['command']) if isinstance(bins, dict) else bins if definition['command'] == definition['package'].rsplit('/', 1)[-1] else None
        if not isinstance(version, str) or not SEMVER.fullmatch(version) or not isinstance(target, str):
            return None
        relative = pathlib.PurePosixPath(target)
        if relative.is_absolute() or '..' in relative.parts or '\\' in target:
            return None
        entry = root.joinpath(*relative.parts)
        if entry.resolve(strict=True) != native or entry.is_symlink():
            return None
        for parent in entry.parents:
            checked(parent, True)
            if parent == root:
                break
        return {'version': version, 'metadata': str(metadata)}
    return None


def describe(name, definition, state, home=None):
    """Read native identity/owned receipts without starting an agent or GUI."""
    home = pathlib.Path(home) if home is not None else pathlib.Path.home()
    command = shutil.which(definition['command'])
    manager = definition.get('manager')
    result = {'present': bool(command), 'owned': None, 'kind': 'desktop' if manager == 'native-desktop' else 'cli',
              'platform': platform.system().lower(), 'status': 'unmanaged-or-unverified' if command else 'not-installed'}
    try:
        if manager == 'native-products':
            from .native_helpers.products import installed_receipt
            value = installed_receipt(name, home, state, command)
            if value:
                result.update(present=True, owned=True, version=value['version'], status='owned-native-installation')
        elif manager == 'native-desktop':
            from .native_helpers.desktop import discover
            value = discover(name, home, platform.system(), run)
            result.update(present=bool(value), status='native-metadata-verified' if value else 'not-installed')
            if value:
                result['version'] = value['version']
        elif manager == 'publisher-managed' and command:
            result['status'] = 'publisher-managed'
        elif not manager:
            if platform.system() == 'Darwin':
                from .native_helpers.macos_npm import installed_receipt
                generation = installed_receipt(name, definition, home, pathlib.Path(state), command)
                if generation:
                    return dict(result, present=True, owned=True, version=generation['version'], status='owned-npm-installation')
            receipt = owned_installation(state, name)
            if receipt:
                metadata = package_file(pathlib.Path(receipt['prefix']), definition['package'])
                value = read_json(metadata)
                if isinstance(value, dict) and value.get('name') == definition['package'] and isinstance(value.get('version'), str) and SEMVER.fullmatch(value['version']):
                    result.update(present=True, owned=True, version=value['version'], status='owned-npm-installation')
            if 'version' not in result:
                value = npm_native_metadata(command, definition, allow_macos_opencode_hardlink=True)
                if value:
                    result.update(present=True, owned=None, version=value['version'], status='native-npm-metadata-verified')
    except (GuardError, OSError, ValueError, KeyError, TypeError):
        result.update(status='native-metadata-unavailable', owned=None)
        if manager == 'native-desktop':
            result['present'] = None
    return result


def npm_command():
    npm, node = shutil.which('npm'), shutil.which('node')
    if not npm or not node:
        raise GuardError('node-and-npm-required')
    # Validate before --version: even a metadata probe executes the runtime.
    node = str(protected_runtime_path(node))
    executable = protected_runtime_path(npm)
    node_version = run([node, '--version'], timeout=10)
    match = re.fullmatch(r'v(\d+)\.\d+\.\d+\s*', node_version['output'])
    if node_version['exit'] or not match or int(match[1]) < 22:
        raise GuardError('node22-or-newer-required')
    candidates = [executable.parent / 'node_modules/npm/bin/npm-cli.js',
                  executable.parent.parent / 'lib/node_modules/npm/bin/npm-cli.js', executable]
    for candidate in candidates:
        if candidate.name == 'npm-cli.js' and candidate.is_file():
            return [node, str(protected_runtime_path(candidate))]
    if os.name == 'nt':
        raise GuardError('native-npm-cli-layout-required')
    return [str(executable)]


def package_file(prefix, package):
    root = prefix / ('node_modules' if os.name == 'nt' else 'lib/node_modules')
    return root.joinpath(*package.split('/')) / 'package.json'


def version_tuple(value, *, allow_prerelease=False):
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise GuardError('stable-release-required')
    base, sep, prerelease = value.partition('-')
    if sep and not allow_prerelease:
        raise GuardError('stable-release-required')
    identifiers = []
    for part in prerelease.split('.') if sep else ():
        if not part or (part.isdigit() and len(part) > 1 and part.startswith('0')):
            raise GuardError('npm-package-metadata-invalid')
        identifiers.append((0, int(part)) if part.isdigit() else (1, part))
    return (*map(int, base.split('.')), not sep, tuple(identifiers))


def npm_metadata_version(value, *, allow_prerelease=False):
    """Treat malformed vendor metadata as a per-tool failure, not a crash."""
    if not isinstance(value, dict):
        raise GuardError('npm-package-metadata-invalid')
    version = value.get('version')
    version_tuple(version, allow_prerelease=allow_prerelease)
    return version


def healthy_codex(metadata, expected, env):
    value = json.loads(metadata.read_text())
    bins = value.get('bin') if isinstance(value, dict) else None
    target = bins.get('codex') if isinstance(bins, dict) else None
    if not isinstance(target, str):
        return False
    file = (metadata.parent / target).resolve()
    if metadata.parent.resolve() not in file.parents:
        return False
    node = shutil.which('node')
    if not node:
        return False
    result = run([node, str(file), '--version'], env=env, timeout=15)
    return result['exit'] == 0 and re.search(r'(?<![\d.])' + re.escape(expected) + r'(?![\d.])', result['output']) is not None


def owned_installation(state, name):
    state = pathlib.Path(state)
    receipt = read_json(state / 'tools' / (name + '.json'))
    if not isinstance(receipt, dict) or receipt.get('prefix') not in (str(state / 'npm'), str(state / 'npm-tools' / name)):
        return None
    launcher, native = pathlib.Path(receipt['launcher']), pathlib.Path(receipt['native'])
    if pathlib.Path(receipt['prefix']) not in native.parents:
        return None
    if os.name == 'nt':
        if not launcher.is_file() or not native.is_file() or launcher.is_symlink() or native.is_symlink():
            return None
        if hashlib.sha256(launcher.read_bytes()).hexdigest() != receipt['launcherSha256'] or hashlib.sha256(native.read_bytes()).hexdigest() != receipt['nativeSha256']:
            return None
    elif not launcher.is_symlink() or launcher.resolve() != native.resolve():
        return None
    return receipt


def npm_launcher(definition):
    home, _, _ = locations()
    directory = pathlib.Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / 'modelhub-kit/bin' if os.name == 'nt' else home / '.local/bin'
    return directory / (definition['command'] + ('.cmd' if os.name == 'nt' else ''))


def expose_installation(state, name, definition, prior=None, prefix=None):
    state = pathlib.Path(state)
    prefix = pathlib.Path(prefix) if prefix is not None else state / 'npm'
    native = prefix / (definition['command'] + '.cmd') if os.name == 'nt' else prefix / 'bin' / definition['command']
    if not native.is_file():
        raise GuardError('installed-command-missing')
    launcher = npm_launcher(definition)
    private_directory(launcher.parent)
    receipt = prior or owned_installation(state, name)
    if launcher.exists() or launcher.is_symlink():
        if not receipt or pathlib.Path(receipt['launcher']) != launcher:
            raise GuardError('custom-launcher-preserved')
        if os.name == 'nt' and hashlib.sha256(launcher.read_bytes()).hexdigest() != receipt['launcherSha256']:
            raise GuardError('custom-launcher-preserved')
    if os.name == 'nt':
        if any(c in str(native) for c in '%!"\r\n'):
            raise GuardError('unsafe-command-wrapper-path')
        text = '@"' + str(native) + '" %*\n'
        atomic_text(launcher, text)
    elif not launcher.is_symlink():
        os.symlink(str(native), launcher)
    value = {'version': 1, 'prefix': str(prefix), 'launcher': str(launcher), 'native': str(native)}
    if os.name == 'nt':
        value.update(launcherSha256=hashlib.sha256(launcher.read_bytes()).hexdigest(), nativeSha256=hashlib.sha256(native.read_bytes()).hexdigest())
    atomic_json(state / 'tools' / (name + '.json'), value)
    return launcher


def update(config, state, selected=None, install_missing=False, dry_run=False, repair=False):
    definitions = tools()
    names = selected or config.get('tools', [])
    if not set(names) <= definitions.keys():
        raise GuardError('unknown-tool')
    external = config.get('externally_managed_tools', [])
    if (not isinstance(external, list) or any(not isinstance(name, str) for name in external)
            or len(external) > 32 or len(set(external)) != len(external)):
        raise GuardError('invalid-external-tool-selection')
    if not set(external) <= definitions.keys():
        raise GuardError('unknown-external-tool')
    external = set(external)
    if install_missing and not selected:
        raise GuardError('install-missing-requires-explicit-tool-selection')
    if repair and not selected:
        raise GuardError('repair-requires-explicit-tool-selection')
    env = subscription_only_environment(dict(os.environ, NPM_CONFIG_LOGS_MAX='0',
                                             NPM_CONFIG_AUDIT='false', NPM_CONFIG_FUND='false'))
    # No API-key fallback for subscription tooling. User login files stay untouched.
    results = []
    npm, prefix = None, None
    for name in names:
        if name in external:
            # Another updater owns this tool. Do not inspect or mutate it,
            # and do not claim that the external update has completed.
            results.append({'tool': name, 'status': 'queued', 'reason': 'managed-by-external-updater'})
            continue
        definition = definitions[name]
        command = shutil.which(definition['command'])
        managed = owned_installation(state, name)
        if not command and managed:
            command = managed['launcher']
        if definition.get('manager'):
            manager = definition['manager']
            if manager == 'publisher-managed':
                status = 'queued' if command or install_missing else 'not-installed'
                results.append({'tool': name, 'status': status, 'installed': False,
                                'reason': 'use-official-publisher-channel', 'source': definition['source']})
                continue
            if manager in ('native-products', 'native-desktop'):
                try:
                    if manager == 'native-products':
                        from .native_helpers.products import reconcile
                        result = reconcile(name, home=pathlib.Path.home(), state=state, command=command,
                                           install_missing=install_missing, dry_run=dry_run)
                    else:
                        from .native_helpers.desktop import reconcile
                        result = reconcile(name, home=pathlib.Path.home(), state=state,
                                           install_missing=install_missing, dry_run=dry_run)
                    results.append(result)
                except (GuardError, OSError, ValueError, KeyError, TypeError):
                    results.append({'tool': name, 'status': 'queued-native-metadata-unavailable', 'installed': False})
                continue
            helpers = {'native-omp': 'omp.py', 'native-droid': 'droid.py'}
            if manager not in helpers:
                results.append({'tool': name, 'status': 'unsupported-native-manager', 'installed': False})
                continue
            if not command:
                results.append({'tool': name, 'status': 'not-installed', 'installed': False})
                continue
            if dry_run:
                results.append({'tool': name, 'status': 'planned-native-integrity-check'})
                continue
            if os.name == 'nt':
                results.append({'tool': name, 'status': 'queued-native-windows-manual-update'})
                continue
            if pathlib.Path(command).absolute() != pathlib.Path.home() / '.local/bin' / definition['command']:
                results.append({'tool': name, 'status': 'different-active-installation-preserved'})
                continue
            helper = pathlib.Path(__file__).parent / 'native_helpers' / helpers[manager]
            if manager == 'native-omp':
                args = [sys.executable, str(helper)]
            else:
                discover = run([sys.executable, str(helper), '--discover'], env=env, timeout=40)
                target = discover['output'].strip()
                if discover['exit'] or not SEMVER.fullmatch(target):
                    results.append({'tool': name, 'status': 'metadata-unavailable'})
                    continue
                env['MODELHUB_DISCOVERED_VERSION'] = target
                args = [sys.executable, str(helper)]
            # These Darwin helpers stage and verify owned atomic artifacts;
            # they never invoke an in-place vendor installer. Generic vendor
            # mutations use kernel descendant custody on supported systems.
            result = (run(args, env=env, timeout=310) if platform.system() == 'Darwin'
                      else run_mutation(args, env=env, timeout=310))
            if platform.system() != 'Darwin' and (result.get('treeComplete') is not True or result.get('observationFailed')):
                results.append({'tool': name, 'status': 'queued', 'reason': 'vendor-mutation-custody-unknown'})
                continue
            if result['exit'] != 0:
                results.append({'tool': name, 'status': 'queued' if result['exit'] == 75 else 'update-failed', 'exit': result['exit']})
            else:
                value = json.loads(result['output'])
                results.append({'tool': name, 'status': 'updated' if value.get('changed', value.get('installed')) else 'current'})
            continue
        if platform.system() == 'Darwin':
            from .native_helpers.macos_npm import installed_receipt, reconcile
            try:
                home, _, _ = locations()
                root = pathlib.Path(state)
                pending = os.path.lexists(root / 'npm-releases' / name / '.prepare')
                if pending or managed or installed_receipt(name, definition, home, root, command) or not command and install_missing:
                    results.append(reconcile(name, definition, home, root, command, legacy=managed,
                                             install_missing=install_missing, dry_run=dry_run, repair=repair))
                    continue
            except (GuardError, OSError, ValueError, KeyError, TypeError) as error:
                results.append({'tool': name, 'status': 'queued' if isinstance(error, GuardError) else 'update-failed',
                                'reason': str(error) if isinstance(error, GuardError) else 'macos-package-check-failed',
                                'installed': False})
                continue
        if not command and not install_missing:
            results.append({'tool': name, 'status': 'not-installed', 'installed': False})
            continue
        try:
            if definition.get('platforms') and platform.machine().lower() not in definition['platforms'].get(platform.system(), []):
                results.append({'tool': name, 'status': 'unsupported-vendor-platform', 'installed': False})
                continue
            if npm is None:
                npm = npm_command()
            if command and not managed and prefix is None:
                result = run(npm + ['prefix', '--global'], env=env, timeout=15)
                if result['exit']:
                    raise GuardError('npm-prefix-unavailable')
                prefix = pathlib.Path(result['output'].strip())
            install_prefix = pathlib.Path(state) / 'npm-tools' / name
            legacy_prefix = pathlib.Path(state) / 'npm'
            target_prefix = install_prefix
            if managed:
                target_prefix = pathlib.Path(managed['prefix'])
            elif command:
                target_prefix = prefix
            metadata = package_file(target_prefix, definition['package'])
            if not command:
                if os.path.lexists(npm_launcher(definition)):
                    results.append({'tool': name, 'status': 'unmanaged-installation-preserved'})
                    continue
                if os.path.lexists(package_file(legacy_prefix, definition['package']).parent):
                    results.append({'tool': name, 'status': 'queued', 'reason': 'unowned-legacy-npm-package-present', 'installed': False})
                    continue
                if os.path.lexists(install_prefix):
                    protected(install_prefix, True)
                    if any(install_prefix.iterdir()):
                        results.append({'tool': name, 'status': 'queued', 'reason': 'npm-installation-outcome-unverified', 'installed': False})
                        continue
            # DeepSeek publishes its endorsed release candidates under npm latest.
            allow_prerelease = definition['package'] == '@deepseek-ai/dsh'
            current = None
            if command:
                if not metadata.is_file() or metadata.is_symlink():
                    results.append({'tool': name, 'status': 'unmanaged-installation-preserved'})
                    continue
                protected(target_prefix, True)
                if not managed:
                    identity = npm_native_metadata(command, definition)
                    if not identity or pathlib.Path(identity['metadata']) != metadata.resolve():
                        results.append({'tool': name, 'status': 'unmanaged-installation-preserved'})
                        continue
                actual = pathlib.Path(command).resolve()
                package_root = metadata.parent.resolve()
                # Avoid updating an old prefix while the active command uses another installation.
                if package_root not in actual.parents:
                    # A command filename alone never proves an npm shim's ownership.
                    if os.name != 'nt' or not managed or pathlib.Path(command).absolute() != pathlib.Path(managed['launcher']):
                        results.append({'tool': name, 'status': 'different-active-installation-preserved'})
                        continue
                current = npm_metadata_version(json.loads(metadata.read_text()), allow_prerelease=allow_prerelease)
            value, _ = http_json('https://registry.npmjs.org/' + definition['package'].replace('/', '%2f') + '/latest')
            latest = npm_metadata_version(value, allow_prerelease=allow_prerelease)
            order_is_newer = current is not None and (
                version_tuple(current, allow_prerelease=allow_prerelease) >
                version_tuple(latest, allow_prerelease=allow_prerelease))
            if current and (current == latest or order_is_newer) and not repair:
                if name == 'codex' and not healthy_codex(metadata, current, env):
                    results.append({'tool': name, 'status': 'repair-needed',
                                    'reason': 'native-platform-runtime-unavailable', 'installed': False})
                    continue
                else:
                    results.append({'tool': name, 'status': 'current' if current == latest else 'newer-installation-preserved', 'version': current, 'installed': False})
                    continue
            if order_is_newer:
                results.append({'tool': name, 'status': 'newer-installation-preserved', 'installed': False})
                continue
            if dry_run:
                results.append({'tool': name, 'status': 'planned', 'before': current, 'target': latest})
                continue
            # An existing package can still have lazy imports from its prefix.
            # A new, empty per-tool prefix is independent of other running copies.
            if command:
                from .cache import process_inventory
                processes = process_inventory()
                package_root = str(metadata.parent.resolve()).replace('\\', '/').lower()
                package_name = definition['package'].replace('\\', '/').lower()
                if processes is None or any(package_root in p.replace('\\', '/').lower() or package_name in p.replace('\\', '/').lower()
                                            or re.search(r'(?i)(?:^|[/\\\s])' + re.escape(definition['command']) + r'(?:[.\s/\\]|$)', p) for p in processes):
                    results.append({'tool': name, 'status': 'queued-active-or-unknown-processes'})
                    continue
            if platform.system() == 'Darwin':
                results.append({'tool': name, 'status': 'queued', 'reason': 'vendor-mutation-custody-unavailable', 'installed': False})
                continue
            private_directory(target_prefix)
            cache = private_directory(pathlib.Path(state) / 'cache/npm')
            runtime = run([shutil.which('node'), '-p', 'JSON.stringify({arch:process.arch,platform:process.platform})'], env=env, timeout=10)
            native = json.loads(runtime['output']) if runtime['exit'] == 0 else {}
            if native.get('arch') not in ('x64', 'arm64', 'arm', 'ia32', 'ppc64', 's390x', 'riscv64') or native.get('platform') not in ('darwin', 'linux', 'win32'):
                raise GuardError('native-node-platform-unavailable')
            for key in list(env):
                if key.lower() in ('npm_config_arch', 'npm_config_platform', 'npm_config_cpu', 'npm_config_os'):
                    env.pop(key)
            env.update(npm_config_arch=native['arch'], npm_config_cpu=native['arch'],
                       npm_config_platform=native['platform'], npm_config_os=native['platform'])
            if command and not managed and npm_native_metadata(command, definition) != identity:
                results.append({'tool': name, 'status': 'unmanaged-installation-preserved'})
                continue
            args = npm + ['install', '--global', '--prefix', str(target_prefix), '--include=optional', '--umask=0022',
                          '--omit=dev', '--engine-strict', '--no-audit', '--no-fund', '--logs-max=0', '--cache', str(cache),
                          '--registry=https://registry.npmjs.org']
            package_scripts = {
                'opencode': '@opencode/cli',
                'dsh': '@deepseek-ai/dsh-subprocess-local,koffi,node-pty',
            }
            if name in package_scripts:
                npm_version = run(npm + ['--version'], env=env, timeout=10)
                version = npm_version['output'].strip()
                if npm_version['exit'] or not SEMVER.fullmatch(version):
                    raise GuardError('npm-version-unavailable')
                if int(version.split('.')[0]) >= 11:
                    args.append('--allow-scripts=' + package_scripts[name])
            args.append(definition['package'] + '@' + latest)
            fresh_prefix = protected(install_prefix, True) if not command else None
            if fresh_prefix is not None and (os.path.lexists(npm_launcher(definition)) or any(install_prefix.iterdir())):
                results.append({'tool': name, 'status': 'queued', 'reason': 'npm-installation-outcome-unverified', 'installed': False})
                continue
            result = run_mutation(args, env=env, timeout=300)
            if result.get('treeComplete') is not True or result.get('observationFailed'):
                results.append({'tool': name, 'status': 'queued', 'reason': 'vendor-mutation-custody-unknown', 'installed': False})
                continue
            after = (npm_metadata_version(json.loads(metadata.read_text()), allow_prerelease=allow_prerelease)
                     if metadata.is_file() else None)
            converged = result['exit'] == 0 and after == latest
            runtime_missing = converged and name == 'codex' and not healthy_codex(metadata, latest, env)
            if converged and not runtime_missing and (managed or not command):
                expose_installation(state, name, definition, managed, target_prefix)
            installed = result['exit'] == 0
            if fresh_prefix is not None and (not converged or runtime_missing) and not os.path.lexists(npm_launcher(definition)):
                now = protected(install_prefix, True)
                if (fresh_prefix.st_dev, fresh_prefix.st_ino) == (now.st_dev, now.st_ino):
                    shutil.rmtree(install_prefix)
                    installed = False
            row = {'tool': name, 'status': 'updated' if converged and not runtime_missing else 'update-failed',
                   'before': current, 'version': after, 'exit': result['exit'], 'installed': installed}
            if runtime_missing:
                row['reason'] = 'native-platform-runtime-unavailable'
            results.append(row)
        except (GuardError, OSError, ValueError) as error:
            reason = str(error) if isinstance(error, GuardError) else 'package-check-failed'
            if reason in UNVERIFIED_NPM_OWNERSHIP:
                # No vendor mutation was attempted: an installation whose
                # ownership cannot be proven must remain queued and intact.
                results.append({'tool': name, 'status': 'queued', 'reason': reason, 'installed': False})
            else:
                results.append({'tool': name, 'status': 'update-failed', 'reason': reason})
    return results
