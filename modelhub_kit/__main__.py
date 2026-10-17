import argparse
import json
import os
import pathlib
import shutil
import sys
from . import __version__
from .core import GuardError, atomic_json, check_identity, config, default_config, locations, now, writer


SETTLED = frozenset({'completed', 'current', 'updated', 'installed', 'ready', 'not-modified',
                     'absent', 'within-budget', 'cleaned', 'applied', 'not-installed', 'missing',
                     'newer-installation-preserved', 'removed', 'no-configured-entries', 'no-configured-servers',
                     'no-configured-providers', 'no-configured-agents'})


def completion(value):
    """Classify operation receipts; an unfamiliar or deferred result stays queued."""
    statuses = []
    def collect(item):
        if isinstance(item, dict):
            if 'status' in item:
                statuses.append(item['status'])
            for child in item.values():
                collect(child)
        elif isinstance(item, list):
            for child in item:
                collect(child)
    collect(value)
    if any(isinstance(s, str) and (s in {'error', 'failed', 'repair-needed', 'refused'}
           or s.startswith(('update-failed', 'refresh-failed'))) for s in statuses):
        return 'failed'
    return 'completed' if all(isinstance(s, str) and s in SETTLED for s in statuses) else 'queued'


def integration_pass(settings, home, state):
    from .integration import MCP_CLIENTS, integrate
    from .projection import MODEL_CLIENTS, project
    from .acp import ACP_CLIENTS, register
    adapters = {'mcp': (MCP_CLIENTS, integrate), 'models': (MODEL_CLIENTS, project), 'acp': (ACP_CLIENTS, register)}
    results = []
    selected = settings.get('integrations', {})
    # Provider additions can depend on an already registered ACP agent and
    # an owned OpenCode provider binding. Do those writes in dependency order.
    for surface in ('mcp', 'acp', 'models'):
        for client in sorted(selected, key=lambda name: (name != 'opencode', name)):
            if not selected[client].get(surface, False):
                continue
            clients, operation = adapters[surface]
            if client not in clients:
                result = {'status': 'queued', 'reason': 'unsupported-native-integration'}
            else:
                try:
                    result = operation(settings, home, state, client, apply=True)
                except (GuardError, OSError, ValueError, KeyError, TypeError):
                    result = {'status': 'queued', 'reason': 'native-configuration-preserved'}
            results.append({'client': client, 'surface': surface, **result})
    return results


def main():
    parser = argparse.ArgumentParser(description='Conservative coding-tool maintenance; no model inference.')
    parser.add_argument('--version', action='version', version=__version__)
    parser.add_argument('--config', type=pathlib.Path)
    parser.add_argument('--expected-os', help='Reject another OS/variant before writes, e.g. linux:arch')
    parser.add_argument('--expected-boot')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('init', help='Create empty personal configuration without overwriting existing settings')
    doctor = sub.add_parser('doctor', help='Read-only runtime, command and configuration discovery')
    doctor.add_argument('--json', action='store_true')
    update = sub.add_parser('update', help='Update only selected, owned, installed CLIs')
    update.add_argument('--tool', action='append')
    update.add_argument('--install-missing', action='store_true')
    update.add_argument('--dry-run', action='store_true')
    update.add_argument('--repair', action='store_true', help='Explicit one-off reinstall of selected same-version npm tools')
    sub.add_parser('sync', help='Refresh configured official model metadata; no inference')
    integrate = sub.add_parser('integrate', help='Plan receipt-owned client MCP entries; preserve unrelated settings')
    from .integration import MCP_CLIENTS
    from .projection import MODEL_CLIENTS
    from .acp import ACP_CLIENTS
    integrate.add_argument('--client', choices=MCP_CLIENTS, required=True)
    integrate.add_argument('--apply', action='store_true')
    project = sub.add_parser('project', help='Plan native model providers from validated catalog metadata')
    project.add_argument('--client', choices=MODEL_CLIENTS, required=True)
    project.add_argument('--apply', action='store_true')
    acp = sub.add_parser('register-acp', help='Plan native ACP agent registration in a supported consumer')
    acp.add_argument('--client', choices=ACP_CLIENTS, required=True)
    acp.add_argument('--apply', action='store_true')
    sub.add_parser('export-acp', help='Write a stdio agent manifest with environment names, never credentials')
    cache = sub.add_parser('cache', help='Assess or clean the kit-owned download cache only')
    cache.add_argument('--apply', action='store_true')
    sub.add_parser('maintain', help='Serialize updates, catalogs, enabled integrations and idle cache cleanup')
    fleet = sub.add_parser('fleet', help='Maintain or explicitly enroll trusted SSH aliases')
    fleet.add_argument('--inventory', type=pathlib.Path, required=True)
    fleet.add_argument('--apply', action='store_true')
    fleet.add_argument('--source', type=pathlib.Path, help='Reviewed kit source directory to deliver')
    fleet.add_argument('--enroll', action='store_true')
    fleet.add_argument('--prerequisites', action='store_true')
    fleet.add_argument('--schedule', action='store_true')
    schedule = sub.add_parser('schedule', help='Plan or apply owned background maintenance scheduling')
    schedule.add_argument('--apply', action='store_true')
    schedule.add_argument('--remove', action='store_true')
    uninstall = sub.add_parser('uninstall', help='Plan or remove only the idle receipt-owned kit application and schedule')
    uninstall.add_argument('--apply', action='store_true')
    uninstall.add_argument('--clean-state', action='store_true', help='Also remove only a marked kit maintenance receipt; retain all other state')
    args = parser.parse_args()
    home, file, state = locations()
    file = args.config or file
    observed = check_identity(args.expected_os, args.expected_boot)
    if args.command == 'init':
        with writer(state):
            changed = False if file.exists() else atomic_json(file, default_config())
        return {'status': 'initialized' if changed else 'existing-config-preserved', 'changed': changed}
    if args.command == 'uninstall':
        from .lifecycle import uninstall
        return uninstall(home, file, state, apply=args.apply, clean_state=args.clean_state)
    settings = config(file)
    if args.command == 'doctor':
        from .updater import tools, describe
        return {'version': __version__, 'identity': observed, 'configured': True, 'readOnly': True,
                'commands': {name: describe(name, d, state, home=home)
                             for name, d in tools().items()},
                'prerequisites': {'python': '.'.join(map(str, sys.version_info[:3])),
                                  'node': shutil.which('node') is not None, 'npm': shutil.which('npm') is not None},
                'catalogSources': len(settings.get('catalog_sources', [])), 'modelCalls': 0}
    if args.command == 'schedule':
        from .schedule import configure
        launch = pathlib.Path(__file__).resolve().parents[1] / 'launch.py'
        command = ([sys.executable, '-B', str(launch)] if launch.is_file() and not launch.is_symlink()
                   else [sys.executable, '-m', 'modelhub_kit']) + ['--config', str(file), 'maintain']
        with writer(state) if args.apply else __import__('contextlib').nullcontext():
            return {**configure(command, state, apply=args.apply, remove=args.remove), 'apply': args.apply}
    if args.command == 'fleet':
        from .fleet import reconcile
        with writer(state) if args.apply else __import__('contextlib').nullcontext():
            return reconcile(args.inventory, state, apply=args.apply, source=args.source, enroll=args.enroll,
                             prerequisites=args.prerequisites, schedule=args.schedule)
    # Plans stay read-only. The same kernel writer serializes all mutation surfaces.
    read_only = (args.command in ('integrate', 'project', 'register-acp') and not args.apply or args.command == 'cache' and not args.apply
                 or args.command == 'update' and args.dry_run)
    with __import__('contextlib').nullcontext() if read_only else writer(state):
        if args.command == 'update':
            if args.repair and not args.tool:
                raise GuardError('repair-requires-explicit-tool-selection')
            from .updater import update
            result = {'tools': update(settings, state, args.tool, args.install_missing, args.dry_run, args.repair)}
            result['status'] = 'planned' if args.dry_run else completion(result)
            return result
        if args.command == 'sync':
            from .catalog import sync
            result = {'catalogs': sync(settings, state)}
            result['status'] = completion(result)
            return result
        if args.command == 'integrate':
            from .integration import integrate
            return integrate(settings, home, state, args.client, args.apply)
        if args.command == 'export-acp':
            from .integration import export_acp
            return export_acp(settings, state)
        if args.command == 'project':
            from .projection import project
            return project(settings, home, state, args.client, args.apply)
        if args.command == 'register-acp':
            from .acp import register
            return register(settings, home, state, args.client, args.apply)
        if args.command == 'cache':
            from .cache import enforce
            return {**enforce(settings, state, args.apply), 'apply': args.apply}
        if args.command == 'maintain':
            from .updater import update
            from .catalog import sync
            from .cache import enforce
            result = {'format': 'modelhub-kit-maintenance-v1', 'version': 1, 'startedAt': now(), 'tools': update(settings, state),
                      'catalogs': sync(settings, state), 'modelCalls': 0}
            result['integrations'] = integration_pass(settings, home, state)
            result['cache'] = enforce(settings, state, True)
            result['status'] = completion(result)
            result['completedAt'] = now()
            atomic_json(state / 'last-maintenance.json', result)
            return result


def failed(result):
    return completion(result) == 'failed'


def cli():
    try:
        result = main()
        print(json.dumps(result, ensure_ascii=False))
        state = completion(result)
        # Plans and discovery have no pending application requirement.
        planned = isinstance(result, dict) and result.get('status') in {'planned', 'initialized', 'existing-config-preserved', 'exported'}
        readonly = isinstance(result, dict) and (result.get('readOnly') is True or result.get('apply') is False)
        sys.exit(1 if state == 'failed' else 75 if state == 'queued' and not (planned or readonly) else 0)
    except GuardError as error:
        print(json.dumps({'status': 'queued' if str(error) == 'writer-busy' else 'refused', 'reason': str(error)}))
        sys.exit(75 if str(error) == 'writer-busy' else 78)
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'error', 'reason': 'operation-failed-current-settings-retained'}))
        sys.exit(1)


if __name__ == '__main__':
    cli()
