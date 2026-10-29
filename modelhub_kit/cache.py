"""Bound the kit's own download cache. Never purge application data or active runtimes."""
import os
import json
import pathlib
import re
import shutil
import sys
from .core import GuardError, protected, run

def _linux_process_inventory():
    """Read bounded process arguments directly when a busy ps output is cut."""
    if not sys.platform.startswith('linux') or not pathlib.Path('/proc').is_dir():
        return None
    output = []
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
                output.append(value)
        return output
    except (OSError, ValueError):
        return None

def process_inventory():
    if os.name == 'nt':
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                   '@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine) | ConvertTo-Json -Compress']
    else:
        command = ['ps', '-axww', '-o', 'pid=', '-o', 'command=']
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
        result = []
        if os.name == 'nt':
            rows = json.loads(value['output'])
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, list) or not rows:
                return None
            for row in rows:
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
            return result
        for line in value['output'].splitlines():
            row = line.strip().split(None, 1)
            if len(row) != 2 or not row[0].isdigit():
                return None
            if int(row[0]) not in (os.getpid(), os.getppid()):
                result.append(row[1])
        return result
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
    normalized = str(directory).replace('\\', '/').lower()
    if processes is None or any(normalized in p.replace('\\', '/').lower() or re.search(r'(?:^|[ /\\])(?:npm|npx)(?:[. /\\]|$)|npm-cli\.js|npx-cli\.js', p) for p in processes):
        return dict(result, status='queued-active-or-unknown-runtime')
    # Only this protected private download cache; no user ~/.npm, uv, installed tools, profiles, or logs.
    for child in directory.iterdir():
        if child.is_symlink():
            raise GuardError('cache-symlink-preserved')
    shutil.rmtree(directory)
    return dict(result, status='cleaned', afterBytes=0)
