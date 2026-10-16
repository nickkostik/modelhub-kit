"""Inventory is user-local. Strict SSH identity and OS fencing precede remote writes."""
import base64
import json
import pathlib
import re
import shlex
from .core import GuardError, atomic_json, now, read_json, run


def reconcile(inventory, state, apply=False):
    document = read_json(inventory)
    if not isinstance(document, dict) or document.get('version') != 1 or not isinstance(document.get('hosts'), list):
        raise GuardError('version1-fleet-inventory-required')
    outcomes, names = [], set()
    for host in document['hosts']:
        name, expected = host.get('alias', ''), host.get('os', '')
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', name) or name in names or not re.fullmatch(r'(?:linux(?::[a-z0-9_-]+)?|macos|windows)', expected):
            raise GuardError('unique-trusted-ssh-alias-and-os-required')
        names.add(name)
        boot = host.get('boot')
        if boot and not re.fullmatch(r'[a-zA-Z0-9.:+-]{1,100}', boot):
            raise GuardError('safe-boot-identity-required')
        if not apply:
            outcomes.append({'host': name, 'status': 'planned', 'expectedOs': expected})
            continue
        argv = ['--expected-os', expected]
        if boot:
            argv += ['--expected-boot', boot]
        argv += ['maintain']
        if expected == 'windows':
            args = ','.join("'" + a + "'" for a in argv)
            code = "$p=Join-Path $env:LOCALAPPDATA 'modelhub-kit/bin/modelhub-kit.cmd'; & $p " + ' '.join("'" + a + "'" for a in argv) + '; exit $LASTEXITCODE'
            remote = 'powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + base64.b64encode(code.encode('utf-16le')).decode()
        else:
            remote = 'exec "$HOME/.local/bin/modelhub-kit" ' + shlex.join(argv)
        try:
            value = run(['ssh', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=8',
                         '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2', name, remote], timeout=600)
            try:
                response = json.loads(value['output'])
            except ValueError:
                response = None
            outcomes.append({'host': name, 'status': 'completed' if value['exit'] == 0 and isinstance(response, dict) else 'queued',
                             'exit': value['exit'], 'timedOut': value['timedOut'],
                             'reason': response.get('reason') if isinstance(response, dict) and response.get('reason') in
                             ['different-os-boot', 'different-boot-identity', 'writer-busy'] else None})
        except OSError:
            outcomes.append({'host': name, 'status': 'queued', 'reason': 'ssh-unavailable'})
    report = {'version': 1, 'observedAt': now(), 'hosts': outcomes, 'apply': apply}
    if apply:
        atomic_json(pathlib.Path(state) / 'fleet-status.json', report)
    return report
