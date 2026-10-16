"""Compare official versions; update one owned installation at a time."""
import json
import os
import pathlib
import re
import shutil
import sys
import hashlib
from .core import GuardError, atomic_json, atomic_text, http_json, locations, private_directory, protected, read_json, run

SEMVER = re.compile(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?')


def tools():
    return json.loads(pathlib.Path(__file__).with_name('tools.json').read_text())


def npm_command():
    npm, node = shutil.which('npm'), shutil.which('node')
    if not npm or not node:
        raise GuardError('node-and-npm-required')
    node_version = run([node, '--version'], timeout=10)
    match = re.fullmatch(r'v(\d+)\.\d+\.\d+\s*', node_version['output'])
    if node_version['exit'] or not match or int(match[1]) < 22:
        raise GuardError('node22-or-newer-required')
    executable = pathlib.Path(npm).resolve()
    candidates = [executable.parent / 'node_modules/npm/bin/npm-cli.js',
                  executable.parent.parent / 'lib/node_modules/npm/bin/npm-cli.js', executable]
    for candidate in candidates:
        if candidate.name == 'npm-cli.js' and candidate.is_file():
            return [node, str(candidate)]
    if os.name == 'nt':
        raise GuardError('native-npm-cli-layout-required')
    return [npm]


def package_file(prefix, package):
    root = prefix / ('node_modules' if os.name == 'nt' else 'lib/node_modules')
    return root.joinpath(*package.split('/')) / 'package.json'


def version_tuple(value):
    if not SEMVER.fullmatch(value) or '-' in value:
        raise GuardError('stable-release-required')
    return tuple(int(p) for p in value.split('.'))


def healthy_codex(metadata, expected, env):
    value = json.loads(metadata.read_text())
    target = value.get('bin', {}).get('codex')
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
    receipt = read_json(pathlib.Path(state) / 'tools' / (name + '.json'))
    if not isinstance(receipt, dict) or receipt.get('prefix') != str(pathlib.Path(state) / 'npm'):
        return None
    launcher, native = pathlib.Path(receipt['launcher']), pathlib.Path(receipt['native'])
    if os.name == 'nt':
        if not launcher.is_file() or not native.is_file() or launcher.is_symlink() or native.is_symlink():
            return None
        if hashlib.sha256(launcher.read_bytes()).hexdigest() != receipt['launcherSha256'] or hashlib.sha256(native.read_bytes()).hexdigest() != receipt['nativeSha256']:
            return None
    elif not launcher.is_symlink() or launcher.resolve() != native.resolve():
        return None
    return receipt


def expose_installation(state, name, definition, prior=None):
    state = pathlib.Path(state)
    prefix = state / 'npm'
    native = prefix / (definition['command'] + '.cmd') if os.name == 'nt' else prefix / 'bin' / definition['command']
    if not native.is_file():
        raise GuardError('installed-command-missing')
    home, _, _ = locations()
    directory = pathlib.Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / 'modelhub-kit/bin' if os.name == 'nt' else home / '.local/bin'
    private_directory(directory)
    launcher = directory / (definition['command'] + ('.cmd' if os.name == 'nt' else ''))
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
    if install_missing and not selected:
        raise GuardError('install-missing-requires-explicit-tool-selection')
    env = dict(os.environ, NPM_CONFIG_LOGS_MAX='0', NPM_CONFIG_AUDIT='false', NPM_CONFIG_FUND='false')
    # No API-key fallback for subscription tooling. User login files stay untouched.
    for key in ('XAI_API_KEY', 'GROK_API_KEY'):
        env.pop(key, None)
    results = []
    npm, prefix = None, None
    for name in names:
        definition = definitions[name]
        command = shutil.which(definition['command'])
        managed = owned_installation(state, name)
        if not command and managed:
            command = managed['launcher']
        if definition.get('manager'):
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
            helper = pathlib.Path(__file__).parent / 'native_helpers' / ('omp.py' if name == 'omp' else 'droid.py')
            if name == 'omp':
                args = [sys.executable, str(helper)]
            else:
                discover = run([sys.executable, str(helper), '--discover'], env=env, timeout=40)
                target = discover['output'].strip()
                if discover['exit'] or not SEMVER.fullmatch(target):
                    results.append({'tool': name, 'status': 'metadata-unavailable'})
                    continue
                env['MODELHUB_DISCOVERED_VERSION'] = target
                args = [sys.executable, str(helper)]
            result = run(args, env=env, timeout=310)
            if result['exit'] != 0:
                results.append({'tool': name, 'status': 'queued' if result['exit'] == 75 else 'update-failed', 'exit': result['exit']})
            else:
                value = json.loads(result['output'])
                results.append({'tool': name, 'status': 'updated' if value.get('changed', value.get('installed')) else 'current'})
            continue
        if not command and not install_missing:
            results.append({'tool': name, 'status': 'not-installed', 'installed': False})
            continue
        try:
            if npm is None:
                npm = npm_command()
                result = run(npm + ['prefix', '--global'], env=env, timeout=15)
                if result['exit']:
                    raise GuardError('npm-prefix-unavailable')
                prefix = pathlib.Path(result['output'].strip())
            target_prefix = pathlib.Path(state) / 'npm' if managed or not command else prefix
            metadata = package_file(target_prefix, definition['package'])
            current = None
            if command:
                if not metadata.is_file() or metadata.is_symlink():
                    results.append({'tool': name, 'status': 'unmanaged-installation-preserved'})
                    continue
                protected(target_prefix, True)
                actual = pathlib.Path(command).resolve()
                package_root = metadata.parent.resolve()
                # Avoid updating an old prefix while the active command uses another installation.
                if package_root not in actual.parents:
                    # A command filename alone never proves an npm shim's ownership.
                    if os.name != 'nt' or not managed or pathlib.Path(command).absolute() != pathlib.Path(managed['launcher']):
                        results.append({'tool': name, 'status': 'different-active-installation-preserved'})
                        continue
                current = json.loads(metadata.read_text()).get('version')
                version_tuple(current)
            value, _ = http_json('https://registry.npmjs.org/' + definition['package'].replace('/', '%2f') + '/latest')
            latest = value.get('version', '')
            version_tuple(latest)
            if current and version_tuple(current) >= version_tuple(latest) and not repair:
                if name == 'codex' and not healthy_codex(metadata, current, env):
                    results.append({'tool': name, 'status': 'repair-needed', 'reason': 'native-platform-runtime-unavailable', 'installed': False})
                    continue
                results.append({'tool': name, 'status': 'current', 'version': current, 'installed': False})
                continue
            if current and version_tuple(current) > version_tuple(latest):
                results.append({'tool': name, 'status': 'newer-installation-preserved', 'installed': False})
                continue
            if dry_run:
                results.append({'tool': name, 'status': 'planned', 'before': current, 'target': latest})
                continue
            # Installing/updating package files can invalidate lazy imports. Require a proven idle tool.
            from .cache import process_inventory
            processes = process_inventory()
            package_root = str(metadata.parent.resolve()).replace('\\', '/').lower()
            package_name = definition['package'].replace('\\', '/').lower()
            if processes is None or any(package_root in p.replace('\\', '/').lower() or package_name in p.replace('\\', '/').lower()
                                        or re.search(r'(?i)(?:^|[/\\\s])' + re.escape(definition['command']) + r'(?:[.\s/\\]|$)', p) for p in processes):
                results.append({'tool': name, 'status': 'queued-active-or-unknown-processes'})
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
            args = npm + ['install', '--global', '--prefix', str(target_prefix), '--include=optional',
                          '--omit=dev', '--engine-strict', '--no-audit', '--no-fund', '--logs-max=0', '--cache', str(cache),
                          '--registry=https://registry.npmjs.org',
                          definition['package'] + '@' + latest]
            result = run(args, env=env, timeout=300)
            after = json.loads(metadata.read_text()).get('version') if metadata.is_file() else None
            if result['exit'] == 0 and after == latest and name == 'codex' and not healthy_codex(metadata, latest, env):
                results.append({'tool': name, 'status': 'update-failed', 'reason': 'native-platform-runtime-unavailable', 'installed': True})
                continue
            if result['exit'] == 0 and after == latest and target_prefix == pathlib.Path(state) / 'npm':
                expose_installation(state, name, definition, managed)
            results.append({'tool': name, 'status': 'updated' if result['exit'] == 0 and after == latest else 'update-failed',
                            'before': current, 'version': after, 'exit': result['exit'], 'installed': result['exit'] == 0})
        except (GuardError, OSError, ValueError) as error:
            results.append({'tool': name, 'status': 'update-failed', 'reason': str(error) if isinstance(error, GuardError) else 'package-check-failed'})
    return results
