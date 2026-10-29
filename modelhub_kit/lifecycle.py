"""Explicit, receipt-bound kit removal; vendor tools and personal data stay put."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

from .core import GuardError, protected, run, writer
from . import cache, schedule

INSTALL_MARKER = 'modelhub-kit-install-v1'
MAINTENANCE_MARKER = 'modelhub-kit-maintenance-v1'
# Fixed application surface, including optional documentation and old releases.
# A receipt cannot grant permission to remove arbitrary names beneath the app.
APPLICATION_FILES = frozenset({
    'modelhub_kit/__init__.py', 'modelhub_kit/__main__.py', 'modelhub_kit/core.py', 'modelhub_kit/mutation.py',
    'modelhub_kit/updater.py', 'modelhub_kit/catalog.py', 'modelhub_kit/integration.py',
    'modelhub_kit/projection.py', 'modelhub_kit/acp.py', 'modelhub_kit/lifecycle.py',
    'modelhub_kit/cache.py', 'modelhub_kit/fleet.py', 'modelhub_kit/schedule.py', 'modelhub_kit/tools.json',
    'modelhub_kit/native_helpers/__init__.py', 'modelhub_kit/native_helpers/omp.py',
    'modelhub_kit/native_helpers/droid.py', 'modelhub_kit/native_helpers/products.py',
    'modelhub_kit/native_helpers/desktop.py', 'modelhub_kit/native_helpers/macos_apps.py',
    'modelhub_kit/native_helpers/codex_runtime.py',
    'launch.py', 'LICENSE', 'AGENTS.md', 'README.md', 'docs/INSTALL.md',
    'docs/OPERATIONS.md', 'docs/SUPPORT.md', 'docs/INTEGRATIONS.md', 'docs/FLEET.md',
})
_HEX = re.compile(r'[a-f0-9]{64}')


def installation_paths(home):
    home = Path(home).absolute()
    base = (Path(os.environ.get('LOCALAPPDATA', home / 'AppData/Local')) / 'modelhub-kit'
            if os.name == 'nt' else home / '.local/share/modelhub-kit')
    app = base / 'app'
    command = base / 'bin/modelhub-kit.cmd' if os.name == 'nt' else home / '.local/bin/modelhub-kit'
    return app, command


def _snapshot(path):
    # Reuse the scheduler's bounded, descriptor/path-fenced ordinary-file reader.
    return schedule._snapshot(Path(path))


def _directory(path):
    info = protected(path, True)
    return info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)


# Emit a summary rather than all command lines: a large unrelated process set
# must not exhaust the existing 64 KiB child-output budget. No inventory scope,
# timeout or visibility requirement is weakened by this Windows-only collector.
_WINDOWS_KIT_SCAN = r"""
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$processCount=0;$skippedCount=0;$activeKit=$false;$unknownKit=$false
$querySeen=0;$callerSeen=0;$complete=$false
try {
  $probeParameters=ConvertFrom-Json ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PARAMETERS__')))
  $appPath=([string]$probeParameters.app).Replace('\','/')
  $commandPath=([string]$probeParameters.command).Replace('\','/')
  Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {
    $processCount++
    if ($null -eq $_.ProcessId -or $_.ProcessId -lt 0 -or $_.ProcessId -gt [uint32]::MaxValue) { throw 'incomplete-process-metadata' }
    $recordId=[long]$_.ProcessId
    if ($recordId -eq $PID) { $querySeen++ }
    if ($recordId -eq $probeParameters.caller) { $callerSeen++ }
    if ($recordId -eq $PID -or $recordId -eq $probeParameters.caller -or $recordId -eq $probeParameters.parent) {
      $skippedCount++
    } else {
      if ($_.Name -isnot [string] -or [string]::IsNullOrWhiteSpace($_.Name)) { throw 'incomplete-process-metadata' }
      $name=[string]$_.Name
      $line=$_.CommandLine
      if ($null -ne $line -and $line -isnot [string]) { throw 'incomplete-process-metadata' }
      if ([string]::IsNullOrWhiteSpace($line)) {
        if ($name -imatch '^(?:node|bun|npm|npx|python(?:\d+(?:\.\d+)*)?|pythonw)(?:\.exe)?$') { $unknownKit=$true }
      } else {
        $normalized=([string]$line).Replace('\','/')
        if ($normalized.IndexOf($appPath,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $normalized.IndexOf($commandPath,[StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $line -imatch '(?:^|[ /\\])modelhub[-_]kit(?:[. /\\]|$)') { $activeKit=$true }
        if ($line.Trim() -imatch '^(?:.*[/\\])?(?:python(?:\d+(?:\.\d+)*)?|pythonw)(?:\.exe)?$') { $unknownKit=$true }
      }
    }
  }
  $complete=($querySeen -eq 1 -and $callerSeen -eq 1)
} catch { $complete=$false }
@{version=1;complete=[bool]$complete;processCount=[long]$processCount;skippedCount=[long]$skippedCount;
  activeKit=[bool]$activeKit;unknownKit=[bool]$unknownKit} | ConvertTo-Json -Compress
"""


def _windows_kit_idle(app, command):
    try:
        parameters = {'app': str(app), 'command': str(command), 'caller': os.getpid(), 'parent': os.getppid()}
        payload = base64.b64encode(json.dumps(parameters, ensure_ascii=False).encode('utf-8')).decode('ascii')
        script = _WINDOWS_KIT_SCAN.replace('__PARAMETERS__', payload)
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        argv = ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
        # CreateProcessW's limit includes its terminating NUL. Encoded text is ASCII,
        # so this length is also the Windows UTF-16 command-line length.
        if len(subprocess.list2cmdline(argv)) >= 32767:
            return False
        value = run(argv, timeout=15)
        if (not isinstance(value, dict) or type(value.get('exit')) is not int or value.get('exit') != 0 or value.get('timedOut') is not False
                or value.get('outputTruncated') is not False or not isinstance(value.get('output'), str)
                or len(value['output']) > 2048):
            return False
        result = json.loads(value['output'])
        fields = {'version', 'complete', 'processCount', 'skippedCount', 'activeKit', 'unknownKit'}
        if (not isinstance(result, dict) or set(result) != fields or type(result['version']) is not int
                or result['version'] != 1 or type(result['complete']) is not bool or not result['complete']
                or any(type(result[k]) is not bool for k in ('activeKit', 'unknownKit'))
                or any(type(result[k]) is not int for k in ('processCount', 'skippedCount'))
                or not 2 <= result['skippedCount'] <= result['processCount'] <= 2**32 - 1):
            return False
        return not result['activeKit'] and not result['unknownKit']
    except (GuardError, OSError, ValueError, KeyError, TypeError):
        return False


def _idle(app, command):
    if os.name == 'nt':
        return _windows_kit_idle(app, command)
    processes = cache.process_inventory()
    if processes is None:
        return False
    paths = [str(p).replace('\\', '/').casefold() for p in (app, command)]
    for value in processes:
        normalized = value.replace('\\', '/').casefold()
        if any(p in normalized for p in paths) or re.search(r'(?i)(?:^|[ /\\])modelhub[-_]kit(?:[. /\\]|$)', value):
            return False
        # An interpreter with unreadable/absent arguments cannot prove idleness.
        if re.fullmatch(r'(?i)(?:.*[/\\])?(?:python(?:\d+(?:\.\d+)*)?|pythonw)(?:\.exe)?', value.strip()):
            return False
    return True


def _inspect(app, command):
    receipt_file = app / '.modelhub-kit-install.json'
    if not app.exists() and not app.is_symlink():
        if command.exists() or command.is_symlink():
            raise GuardError('unowned-launcher-preserved')
        return None
    directories = {app: _directory(app), app.parent: _directory(app.parent), command.parent: _directory(command.parent)}
    receipt_snapshot = _snapshot(receipt_file)
    if receipt_snapshot is None:
        raise GuardError('missing-install-receipt')
    receipt = json.loads(receipt_snapshot[0])
    if (not isinstance(receipt, dict) or set(receipt) != {'marker', 'files', 'launcherSha256'}
            or receipt.get('marker') != INSTALL_MARKER
            or (json.dumps(receipt, indent=2) + '\n').encode() != receipt_snapshot[0]):
        raise GuardError('unowned-install-receipt')
    files, launcher_hash = receipt.get('files'), receipt.get('launcherSha256')
    if (not isinstance(files, dict) or not files or len(files) > len(APPLICATION_FILES)
            or any(name not in APPLICATION_FILES or not isinstance(value, str) or not _HEX.fullmatch(value)
                   for name, value in files.items())
            or not isinstance(launcher_hash, str) or not _HEX.fullmatch(launcher_hash)):
        raise GuardError('unowned-install-receipt')
    snapshots = {receipt_file: receipt_snapshot}
    for relative, digest in files.items():
        path = app / relative
        original = _snapshot(path)
        if original is not None and hashlib.sha256(original[0]).hexdigest() != digest:
            raise GuardError('modified-application-file-preserved')
        snapshots[path] = original
    snapshots[command] = _snapshot(command)
    if snapshots[command] is not None and hashlib.sha256(snapshots[command][0]).hexdigest() != launcher_hash:
        raise GuardError('modified-launcher-preserved')
    # Walk only the fixed, tiny application surface. Unknown files, directories,
    # symlinks and bytecode are data to preserve, never recursive-delete targets.
    allowed = {path for path in snapshots if path != command}
    allowed_directories = {app}
    for path in allowed:
        for parent in path.parents:
            if parent == app:
                break
            allowed_directories.add(parent)
    memberships = {}
    pending = [app]
    while pending:
        parent = pending.pop()
        directories[parent] = _directory(parent)
        names = set()
        with os.scandir(parent) as entries:
            for entry in entries:
                names.add(entry.name)
                if len(names) > len(APPLICATION_FILES) + 1:
                    raise GuardError('unknown-application-entry-preserved')
                path = parent / entry.name
                if entry.is_symlink():
                    raise GuardError('symlinked-application-entry-preserved')
                if entry.is_dir(follow_symlinks=False) and path in allowed_directories:
                    pending.append(path)
                elif not entry.is_file(follow_symlinks=False) or path not in allowed:
                    raise GuardError('unknown-application-entry-preserved')
        memberships[parent] = names
    return {'snapshots': snapshots, 'directories': directories, 'memberships': memberships,
            'receipt': receipt_file, 'command': command}


def _current(plan):
    for path, expected in plan['directories'].items():
        if _directory(path) != expected:
            raise GuardError('installation-directory-changed')
    for path, expected in plan['memberships'].items():
        if set(os.listdir(path)) != expected:
            raise GuardError('application-membership-changed')
    for path, expected in plan['snapshots'].items():
        if _snapshot(path) != expected:
            raise GuardError('installation-file-changed')


def _schedule(launch, config, state, apply):
    argv = [sys.executable, '-B', str(launch), '--config', str(config), 'maintain']
    if os.name == 'nt':
        plan = schedule._windows(argv, state)
    elif sys.platform == 'darwin':
        plan = schedule._mac(argv, state)
    else:
        plan = schedule._linux(argv, state)
    # Registration writes its ownership receipt before registering the native
    # job. With no receipt or generated files there is no owned job to remove.
    evidence = [state / 'schedule/ownership.json', *plan['files']]
    if not any(p.exists() or p.is_symlink() for p in evidence):
        return {'status': 'absent', 'changed': False}
    return schedule.configure(argv, state, apply=apply, remove=True)


def _maintenance_receipt(state):
    path = state / 'last-maintenance.json'
    original = _snapshot(path)
    if original is None:
        return None
    value = json.loads(original[0])
    fields = {'format', 'version', 'startedAt', 'tools', 'catalogs', 'modelCalls',
              'integrations', 'cache', 'status', 'completedAt'}
    if (not isinstance(value, dict) or set(value) != fields or value['format'] != MAINTENANCE_MARKER
            or value['version'] != 1 or value['modelCalls'] != 0
            or not all(isinstance(value[k], list) for k in ('tools', 'catalogs', 'integrations'))
            or not isinstance(value['cache'], dict) or value['status'] not in ('completed', 'queued', 'failed')
            or not all(isinstance(value[k], str) for k in ('startedAt', 'completedAt'))):
        raise GuardError('unmarked-or-custom-state-receipt-preserved')
    return path, original


def uninstall(home, config, state, *, apply=False, clean_state=False):
    """Plan by default; unlink only a complete, unchanged owned application."""
    state = Path(state).absolute()
    app, command = installation_paths(home)
    result = {'status': 'planned', 'apply': apply, 'filesRemoved': 0, 'directoriesRemoved': 0,
              'maintenanceReceiptsRemoved': 0, 'configPreserved': True, 'vendorToolsPreserved': True,
              'nativeIntegrationsPreserved': True, 'appsRestarted': 0, 'backupsCreated': 0, 'modelCalls': 0}
    try:
        plan = _inspect(app, command)
        if plan is None:
            return dict(result, status='absent')
        # Never allow state/config overrides to make code removal delete data.
        for path in (state, Path(config).absolute()):
            if path == app or app in path.parents or path == command:
                raise GuardError('personal-data-overlaps-application')
        if not _idle(app, command):
            return dict(result, status='queued', reason='active-or-unknown-kit-runtime')
        with writer(state) if apply else contextlib.nullcontext():
            _current(plan)
            cleanup = _maintenance_receipt(state) if clean_state else None
            job = _schedule(app / 'launch.py', config, state, False)
            result['schedule'] = job
            if job['status'] not in ('planned', 'removed', 'absent'):
                return dict(result, status='queued', reason='owned-schedule-removal-unavailable')
            result['plannedFiles'] = sum(v is not None for v in plan['snapshots'].values())
            result['cleanStateScope'] = 'marked-last-maintenance-receipt-only' if clean_state else 'preserved'
            if not apply:
                return result
            _current(plan)
            if not _idle(app, command):
                return dict(result, status='queued', reason='active-or-unknown-kit-runtime')
            result['schedule'] = _schedule(app / 'launch.py', config, state, True)
            if result['schedule']['status'] not in ('removed', 'absent'):
                return dict(result, status='queued', reason='owned-schedule-removal-deferred')
            _current(plan)
            if not _idle(app, command):
                return dict(result, status='queued', reason='active-or-unknown-kit-runtime')
            # Check every input before the first unlink. Remove the recovery
            # receipt last; interrupted removal can be retried from reviewed source.
            if cleanup and _snapshot(cleanup[0]) != cleanup[1]:
                raise GuardError('maintenance-receipt-changed')
            order = [command] + [p for p in plan['snapshots'] if p not in (command, plan['receipt'])] + [plan['receipt']]
            for path in order:
                original = plan['snapshots'][path]
                _current(plan)
                if original is not None:
                    path.unlink()
                    result['filesRemoved'] += 1
                    plan['snapshots'][path] = None
                    if path.parent in plan['memberships']:
                        plan['memberships'][path.parent].remove(path.name)
            for directory in sorted(plan['memberships'], key=lambda p: len(p.parts), reverse=True):
                _current(plan)
                if plan['memberships'][directory]:
                    raise GuardError('application-membership-changed')
                directory.rmdir()
                result['directoriesRemoved'] += 1
                del plan['memberships'][directory]
                del plan['directories'][directory]
                if directory.parent in plan['memberships']:
                    plan['memberships'][directory.parent].remove(directory.name)
            if cleanup:
                if _snapshot(cleanup[0]) != cleanup[1]:
                    raise GuardError('maintenance-receipt-changed')
                cleanup[0].unlink()
                result['maintenanceReceiptsRemoved'] = 1
            result['status'] = 'removed'
            return result
    except (GuardError, OSError, ValueError, KeyError, TypeError) as error:
        reason = str(error) if isinstance(error, GuardError) else 'owned-installation-inspection-or-change-failed'
        return dict(result, status='queued', reason=reason)
