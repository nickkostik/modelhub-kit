import argparse
import json
import os
import pathlib
import shutil
import sys
from . import __version__
from .core import GuardError, atomic_json, check_identity, config, default_config, locations, now, writer


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
    integrate.add_argument('--client', choices=['codex', 'claude', 'opencode', 'cursor'], required=True)
    integrate.add_argument('--apply', action='store_true')
    sub.add_parser('export-acp', help='Write a stdio agent manifest with environment names, never credentials')
    cache = sub.add_parser('cache', help='Assess or clean the kit-owned download cache only')
    cache.add_argument('--apply', action='store_true')
    sub.add_parser('maintain', help='One serialized update, catalog refresh and idle cache pass')
    fleet = sub.add_parser('fleet', help='Plan or run trusted pre-enrolled SSH aliases')
    fleet.add_argument('--inventory', type=pathlib.Path, required=True)
    fleet.add_argument('--apply', action='store_true')
    schedule = sub.add_parser('schedule', help='Plan or apply owned background maintenance scheduling')
    schedule.add_argument('--apply', action='store_true')
    schedule.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    home, file, state = locations()
    file = args.config or file
    observed = check_identity(args.expected_os, args.expected_boot)
    if args.command == 'init':
        with writer(state):
            changed = False if file.exists() else atomic_json(file, default_config())
        return {'status': 'initialized' if changed else 'existing-config-preserved', 'changed': changed}
    settings = config(file)
    if args.command == 'doctor':
        from .updater import tools
        return {'version': __version__, 'identity': observed, 'configured': True, 'readOnly': True,
                'commands': {name: {'present': shutil.which(d['command']) is not None}
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
            return configure(command, state, apply=args.apply, remove=args.remove)
    if args.command == 'fleet':
        from .fleet import reconcile
        with writer(state) if args.apply else __import__('contextlib').nullcontext():
            return reconcile(args.inventory, state, apply=args.apply)
    # Plans stay read-only. The same kernel writer serializes all mutation surfaces.
    read_only = (args.command == 'integrate' and not args.apply or args.command == 'cache' and not args.apply
                 or args.command == 'update' and args.dry_run)
    with __import__('contextlib').nullcontext() if read_only else writer(state):
        if args.command == 'update':
            if args.repair and not args.tool:
                raise GuardError('repair-requires-explicit-tool-selection')
            from .updater import update
            return {'tools': update(settings, state, args.tool, args.install_missing, args.dry_run, args.repair)}
        if args.command == 'sync':
            from .catalog import sync
            return {'catalogs': sync(settings, state)}
        if args.command == 'integrate':
            from .integration import integrate
            return integrate(settings, home, state, args.client, args.apply)
        if args.command == 'export-acp':
            from .integration import export_acp
            return export_acp(settings, state)
        if args.command == 'cache':
            from .cache import enforce
            return enforce(settings, state, args.apply)
        if args.command == 'maintain':
            from .updater import update
            from .catalog import sync
            from .cache import enforce
            result = {'version': 1, 'startedAt': now(), 'tools': update(settings, state),
                      'catalogs': sync(settings, state), 'cache': enforce(settings, state, True), 'modelCalls': 0}
            result['completedAt'] = now()
            atomic_json(state / 'last-maintenance.json', result)
            return result


def failed(result):
    if isinstance(result, dict):
        if result.get('status') in ['update-failed', 'refresh-failed', 'metadata-unavailable', 'repair-needed']:
            return True
        return any(failed(v) for v in result.values())
    return isinstance(result, list) and any(failed(v) for v in result)


def cli():
    try:
        result = main()
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(1 if failed(result) else 0)
    except GuardError as error:
        print(json.dumps({'status': 'queued' if str(error) == 'writer-busy' else 'refused', 'reason': str(error)}))
        sys.exit(75 if str(error) == 'writer-busy' else 78)
    except (OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'status': 'error', 'reason': 'operation-failed-current-settings-retained'}))
        sys.exit(1)


if __name__ == '__main__':
    cli()
