"""Bound the kit's own download cache. Never purge application data or active runtimes."""
import os
import json
import pathlib
import re
import shutil
import sys
from .core import GuardError, protected, run

NPM_INVOCATION = re.compile(r'(?:^|[\s/])(?:npm|npx)(?:\.(?:cmd|exe))?["\']?(?:\s|$)|/(?:npm|npx)-cli\.js["\']?(?:\s|$)', re.I)


class ProcessInventory(list):
    """String-compatible process commands plus bounded parent/lifecycle rows."""

    def __init__(self, commands, rows=None):
        super().__init__(commands)
        self.rows = rows


def _linux_process_inventory():
    """Read bounded process arguments directly when a busy ps output is cut."""
    if not sys.platform.startswith('linux') or not pathlib.Path('/proc').is_dir():
        return None
    output, rows = [], []
    try:
        names = os.listdir('/proc')
        if len(names) > 32768:
            return None
        for name in names:
            if not name.isdigit():
                continue
            pid = int(name)
            if pid in (os.getpid(), os.getppid()):
                continue
            try:
                with open('/proc/' + name + '/cmdline', 'rb') as stream:
                    data = stream.read(1024 * 1024 + 1)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                return None
            if len(data) > 1024 * 1024:
                return None
            value = data.rstrip(b'\0').replace(b'\0', b' ').decode('utf-8', errors='replace')
            if value:
                try:
                    with open('/proc/' + name + '/stat', 'rb') as stream:
                        stat = stream.read(65537)
                    if len(stat) > 65536:
                        return None
                    tail = stat.rsplit(b')', 1)[1].strip().split()
                    ppid = int(tail[1])
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (OSError, IndexError, ValueError):
                    return None
                row = {'pid': pid, 'ppid': ppid, 'command': value}
                if NPM_INVOCATION.search(value):
                    try:
                        with open('/proc/' + name + '/environ', 'rb') as stream:
                            environment = stream.read(1024 * 1024 + 1)
                        if len(environment) > 1024 * 1024:
                            return None
                    except (FileNotFoundError, ProcessLookupError):
                        continue
                    except (OSError, PermissionError):
                        pass
                    else:
                        prefix = b'npm_lifecycle_event='
                        for entry in environment.split(b'\0'):
                            if entry.startswith(prefix):
                                row['lifecycle'] = entry[len(prefix):].decode('utf-8', errors='replace')
                                break
                output.append(value)
                rows.append(row)
        return ProcessInventory(output, rows)
    except (OSError, ValueError):
        return None


def _attach_linux_lifecycles(rows):
    """Read only npm children's lifecycle names without persisting environments."""
    if not sys.platform.startswith('linux') or not pathlib.Path('/proc').is_dir():
        return
    npm_parents = {row['pid'] for row in rows if NPM_INVOCATION.search(row['command'])}
    for row in rows:
        if row.get('ppid') not in npm_parents:
            continue
        path = '/proc/%d/environ' % row['pid']
        try:
            with open(path, 'rb') as stream:
                environment = stream.read(1024 * 1024 + 1)
            if len(environment) > 1024 * 1024:
                continue
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, PermissionError):
            continue
        prefix = b'npm_lifecycle_event='
        for entry in environment.split(b'\0'):
            if entry.startswith(prefix):
                row['lifecycle'] = entry[len(prefix):].decode('utf-8', errors='replace')
                break


def process_inventory():
    if os.name == 'nt':
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                   '@(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CommandLine) | ConvertTo-Json -Compress']
    else:
        command = ['ps', '-axww', '-o', 'pid=', '-o', 'ppid=', '-o', 'command=']
    try:
        value = run(command, timeout=15)
        if (isinstance(value, dict) and os.name != 'nt' and sys.platform.startswith('linux')
                and value.get('exit') == 0 and value.get('outputTruncated') is True
                and value.get('timedOut') is False):
            return _linux_process_inventory()
        if (not isinstance(value, dict) or type(value.get('exit')) is not int or value['exit'] != 0
                or value.get('outputTruncated') is not False or value.get('timedOut') is not False
                or not isinstance(value.get('output'), str) or not value['output'].strip()):
            return None
        result, rows = [], []
        if os.name == 'nt':
            source_rows = json.loads(value['output'])
            if isinstance(source_rows, dict):
                source_rows = [source_rows]
            if not isinstance(source_rows, list) or not source_rows:
                return None
            for row in source_rows:
                if not isinstance(row, dict) or type(row.get('ProcessId')) is not int or row['ProcessId'] < 0:
                    return None
                name, arguments = row.get('Name'), row.get('CommandLine')
                if (not isinstance(name, str) or not name.strip()
                        or arguments is not None and not isinstance(arguments, str)):
                    return None
                if row['ProcessId'] in (os.getpid(), os.getppid()):
                    continue
                if arguments is not None and not arguments.strip():
                    arguments = None
                if not arguments and name.lower() in ('node.exe', 'bun.exe', 'npm.exe', 'npx.exe'):
                    return None
                result.append(arguments or name)
                rows.append({'pid': row['ProcessId'], 'ppid': row.get('ParentProcessId'),
                             'command': arguments or name})
            return ProcessInventory(result, rows)
        for line in value['output'].splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                pid, ppid, command = int(fields[0]), int(fields[1]), fields[2]
            elif len(fields) in (2, 3) and fields[0].isdigit():
                pid, ppid = int(fields[0]), None
                command = fields[1] if len(fields) == 2 else fields[1] + ' ' + fields[2]
            else:
                return None
            if pid in (os.getpid(), os.getppid()):
                continue
            result.append(command)
            rows.append({'pid': pid, 'ppid': ppid, 'command': command})
        _attach_linux_lifecycles(rows)
        return ProcessInventory(result, rows)
    except (GuardError, OSError, ValueError, KeyError, TypeError):
        return None


def allocated_bytes(root):
    def unavailable(error):
        raise error

    total, seen = 0, set()
    for base, directories, files in os.walk(root, followlinks=False, onerror=unavailable):
        directories[:] = [n for n in directories if not (pathlib.Path(base) / n).is_symlink()]
        for name in files:
            path = pathlib.Path(base) / name
            info = path.lstat()
            if path.is_symlink():
                continue
            key = info.st_dev, info.st_ino
            if key in seen:
                continue
            seen.add(key)
            total += getattr(info, 'st_blocks', (info.st_size + 511) // 512) * 512
    return total


def npm_cache_busy(directory, processes):
    """Allow a handed-off npm script child; protect installers and cache readers."""
    if not isinstance(processes, (list, tuple)):
        return True
    normalized_directory = str(directory).replace('\\', '/').lower().rstrip('/') + '/'
    rows = getattr(processes, 'rows', None)
    by_pid = {row['pid']: row for row in rows} if isinstance(rows, list) else {}
    child_lifecycles = {}
    if isinstance(rows, list):
        for row in rows:
            if row.get('ppid') is not None and row.get('lifecycle'):
                child_lifecycles.setdefault(row['ppid'], []).append(row['lifecycle'])
    for index, process in enumerate(processes):
        value = str(process)
        normalized = value.replace('\\', '/').lower()
        if normalized_directory in normalized or re.search(r'(?:^|[\s/])node-gyp(?:\s|$)', normalized):
            return True
        if not NPM_INVOCATION.search(normalized):
            continue
        row = rows[index] if isinstance(rows, list) and index < len(rows) else by_pid.get(index)
        lifecycles = child_lifecycles.get(row.get('pid'), []) if isinstance(row, dict) else []
        if (re.search(r'(?:^|\s)(?:run(?:-script)?\s+\S+|start|dev)(?:\s|$)', normalized)
                and lifecycles and all(not re.fullmatch(r'(?:pre|post)?install', name, re.I)
                                       for name in lifecycles)):
            continue
        return True
    return False


def enforce(config, state, apply=False):
    directory = pathlib.Path(state) / 'cache/npm'
    budget = config['cache_budget_bytes']
    if not directory.exists():
        return {'status': 'absent', 'beforeBytes': 0, 'afterBytes': 0, 'budgetBytes': budget}
    protected(directory, True)
    result = {'beforeBytes': None, 'afterBytes': None, 'budgetBytes': budget,
              'scope': 'kit-owned-npm-download-cache'}
    try:
        before = allocated_bytes(directory)
    except OSError:
        return dict(result, status='queued-cache-accounting-unavailable')
    result.update(beforeBytes=before, afterBytes=before)
    if before <= budget:
        return dict(result, status='within-budget')
    if not apply:
        return dict(result, status='over-budget')
    processes = process_inventory()
    if processes is None or npm_cache_busy(directory, processes):
        return dict(result, status='queued-active-or-unknown-runtime')
    # Only this protected private download cache; no user ~/.npm, uv, installed tools, profiles, or logs.
    for child in directory.iterdir():
        if child.is_symlink():
            raise GuardError('cache-symlink-preserved')
    shutil.rmtree(directory)
    return dict(result, status='cleaned', afterBytes=0)
