"""Bound the kit's own download cache. Never purge application data or active runtimes."""
import os
import json
import pathlib
import re
import shutil
from .core import GuardError, protected, run


def process_inventory():
    if os.name == 'nt':
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                   '@(Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine) | ConvertTo-Json -Compress']
    else:
        command = ['ps', '-axww', '-o', 'pid=', '-o', 'command=']
    try:
        value = run(command, timeout=15)
        if value['exit'] or value['outputTruncated']:
            return None
        result = []
        if os.name == 'nt':
            rows = json.loads(value['output'])
            for row in rows:
                if row['ProcessId'] in (os.getpid(), os.getppid()):
                    continue
                name, arguments = row.get('Name', ''), row.get('CommandLine')
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
    except OSError:
        return None


def allocated_bytes(root):
    total, seen = 0, set()
    for base, directories, files in os.walk(root, followlinks=False):
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
    before = allocated_bytes(directory)
    result = {'beforeBytes': before, 'afterBytes': before, 'budgetBytes': budget,
              'scope': 'kit-owned-npm-download-cache'}
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
