#!/usr/bin/env python3
"""Audit tracked file content and every Git revision without printing matches."""
import argparse
import ipaddress
import pathlib
import re
import subprocess
import sys
import json

RULES = {
    'private-key': re.compile(rb'-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----'),
    'provider-token': re.compile(rb'(?:\bsk-[A-Za-z0-9_-]{20,}|\bgh[pousr]_[A-Za-z0-9]{25,}|\bgithub_pat_[A-Za-z0-9_]{25,}|\bxox[baprs]-[A-Za-z0-9-]{20,})'),
    'credential-url': re.compile(rb'https?://[^\s/\x22\x27]+:[^\s/@\x22\x27]+@'),
    'personal-home': re.compile(rb'(?:/Users/|/home/)[A-Za-z][A-Za-z0-9_.-]+(?:/|\b)|[A-Z]:\\Users\\[A-Za-z][A-Za-z0-9_.-]+\\'),
    'email-in-content': re.compile(rb'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'),
}
FORBIDDEN = re.compile(r'(?i)(?:^|/)(?:\.env(?:\..*)?|auth\.json|credentials.*|.*\.(?:sqlite.*|db|log|bak|backup|tgz|zip|tar\.gz)|known_hosts|id_rsa|id_ed25519)$')


def scan(name, data, denylist=()):
    issues = []
    if FORBIDDEN.search(name):
        issues.append('forbidden-runtime-file')
    if b'\x00' in data:
        issues.append('binary-file')
    for label, rule in RULES.items():
        if rule.search(data):
            issues.append(label)
    for token in denylist:
        if token.lower() in data.lower():
            issues.append('operator-private-denylist')
            break
    for value in re.findall(rb'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])', data):
        try:
            address = ipaddress.ip_address(value.decode())
            if not address.is_loopback and not any(address in ipaddress.ip_network(n) for n in ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')):
                issues.append('non-example-address')
        except ValueError:
            pass
    return sorted(set(issues))


def git(root, args):
    return subprocess.check_output(['git', '-C', str(root)] + args, stderr=subprocess.DEVNULL)


def audit(root, denylist=()):
    findings, seen = [], set()
    try:
        files = git(root, ['ls-files', '-z']).split(b'\0')
        files = [x.decode() for x in files if x]
    except subprocess.CalledProcessError:
        raise ValueError('Initialize and stage the public allowlist before auditing')
    if not files:
        raise ValueError('An empty index is not a successful publication audit')
    for name in files:
        file = root / name
        if file.is_symlink() or not file.is_file() or file.stat().st_size > 4 * 1024 * 1024:
            findings.append({'file': name, 'rules': ['unreviewed-file-type-or-size']})
            continue
        issues = scan(name, file.read_bytes(), denylist)
        if issues:
            findings.append({'file': name, 'rules': issues})
    try:
        commits = git(root, ['rev-list', '--all']).decode().splitlines()
    except subprocess.CalledProcessError:
        commits = []
    for commit in commits:
        message = git(root, ['show', '-s', '--format=%B', commit])
        if re.search(rb'^Co-authored-by:', message, re.I | re.M):
            findings.append({'commit': commit[:12], 'rules': ['co-author-trailer']})
        tree = git(root, ['ls-tree', '-r', '-z', commit])
        for entry in tree.split(b'\0'):
            if not entry:
                continue
            meta, name = entry.split(b'\t', 1)
            mode, kind, oid = meta.split()
            file = name.decode()
            key = file, oid
            if key in seen:
                continue
            seen.add(key)
            if mode not in (b'100644', b'100755') or kind != b'blob':
                findings.append({'file': file, 'rules': ['history-nonregular-file']})
                continue
            data = git(root, ['cat-file', 'blob', oid.decode()])
            issues = scan(file, data, denylist)
            if issues:
                findings.append({'file': file, 'commit': commit[:12], 'rules': issues})
    return {'filesChecked': len(files), 'commitsChecked': len(commits), 'historyBlobsChecked': len(seen),
            'findings': findings, 'passed': not findings,
            'scope': 'Tracked file content and all reachable revisions; chosen public Git author metadata is intentional.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1])
    parser.add_argument('--denylist-file', type=pathlib.Path, help='Optional private operator terms outside the repository')
    args = parser.parse_args()
    denylist = [v.strip().encode() for v in args.denylist_file.read_text().splitlines() if v.strip()] if args.denylist_file else []
    try:
        result = audit(args.root, denylist)
        print(json.dumps(result))
        sys.exit(0 if result['passed'] else 1)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(json.dumps({'passed': False, 'reason': 'audit-could-not-complete'}))
        sys.exit(1)
