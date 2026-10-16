"""Merge only receipt-owned MCP entries. ACP is exported as a native command manifest."""
import hashlib
import json
import pathlib
import re
import tomllib
import urllib.parse
from .core import GuardError, atomic_json, atomic_text, private_directory, protected, read_json


def servers(config):
    result = {}
    for name, item in config.get('mcp_servers', {}).items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name) or not isinstance(item, dict) or set(item) - {'url', 'bearer_env'}:
            raise GuardError('safe-server-name-required')
        url = item.get('url', '')
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise GuardError('public-https-mcp-url-without-secrets-required')
        env = item.get('bearer_env')
        if env and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', env):
            raise GuardError('credential-environment-name-required')
        result[name] = {'url': url, **({'bearer_env': env} if env else {})}
    return result


def integrate(config, home, state, client, apply=False):
    definitions = servers(config)
    paths = {'codex': '.codex/config.toml', 'claude': '.claude.json',
             'opencode': '.config/opencode/opencode.json', 'cursor': '.cursor/mcp.json'}
    if client not in paths:
        raise GuardError('unsupported-client')
    path = pathlib.Path(home) / paths[client]
    receipt_file = pathlib.Path(state) / ('integration-' + client + '.json')
    receipt = read_json(receipt_file, {'entries': {}})
    before = ''
    if path.exists() or path.is_symlink():
        if protected(path).st_size > 4 * 1024 * 1024:
            raise GuardError('oversized-client-config')
        before = path.read_text(encoding='utf-8')
    if client == 'codex':
        parsed = tomllib.loads(before)
        begin, end = '# modelhub-kit:begin', '# modelhub-kit:end'
        if before.count(begin) != before.count(end) or before.count(begin) > 1:
            raise GuardError('ambiguous-owned-toml-block')
        block = ''
        if begin in before:
            start, stop = before.index(begin), before.index(end) + len(end)
            block = before[start:stop]
            if hashlib.sha256(block.encode()).hexdigest() != receipt.get('blockSha256'):
                raise GuardError('customized-owned-block-preserved')
            outside = before[:start] + before[stop:]
        else:
            outside = before
        outside_parsed = tomllib.loads(outside)
        if set(outside_parsed.get('mcp_servers', {})) & definitions.keys():
            raise GuardError('custom-server-entry-preserved')
        lines = [begin]
        for name, item in definitions.items():
            lines += ['[mcp_servers.' + name + ']', 'url = ' + json.dumps(item['url'])]
            if item.get('bearer_env'):
                lines += ['bearer_token_env_var = ' + json.dumps(item['bearer_env'])]
        lines += [end]
        desired_block = '\n'.join(lines)
        after = outside.rstrip() + '\n\n' + desired_block + '\n'
        tomllib.loads(after)
        new_receipt = {'version': 1, 'blockSha256': hashlib.sha256(desired_block.encode()).hexdigest()}
    else:
        document = json.loads(before) if before.strip() else {}
        if not isinstance(document, dict):
            raise GuardError('object-client-config-required')
        field = 'mcp' if client == 'opencode' else 'mcpServers'
        entries = document.setdefault(field, {})
        if not isinstance(entries, dict):
            raise GuardError('object-mcp-config-required')
        owned = receipt.get('entries', {})
        for name, old in owned.items():
            if name in entries and entries[name] != old:
                raise GuardError('customized-managed-server-preserved')
        rendered = {}
        for name, item in definitions.items():
            value = {'type': 'remote', 'url': item['url'], 'enabled': True} if client == 'opencode' else {'url': item['url']}
            if client == 'claude':
                value['type'] = 'http'
            if item.get('bearer_env'):
                variable = ('{env:' + item['bearer_env'] + '}') if client == 'opencode' else ('${env:' + item['bearer_env'] + '}') if client == 'cursor' else ('${' + item['bearer_env'] + '}')
                value['headers'] = {'Authorization': 'Bearer ' + variable}
                if client == 'opencode':
                    value['oauth'] = False
            if name in entries and name not in owned:
                raise GuardError('custom-server-entry-preserved')
            rendered[name] = value
        for name in owned:
            entries.pop(name, None)
        entries.update(rendered)
        after = json.dumps(document, indent=2) + '\n'
        new_receipt = {'version': 1, 'entries': rendered}
    if not definitions and not receipt.get('entries') and not receipt.get('blockSha256'):
        return {'client': client, 'status': 'no-configured-servers', 'changed': False}
    changed = before != after
    if apply:
        if before != (path.read_text() if path.exists() else ''):
            raise GuardError('client-config-changed-concurrently')
        atomic_text(path, after, expected=before)
        atomic_json(receipt_file, new_receipt)
    return {'client': client, 'status': 'applied' if apply else 'planned', 'changed': changed}


def export_acp(config, state):
    agents = config.get('acp_agents', {})
    for name, agent in agents.items():
        if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', name) or not isinstance(agent, dict):
            raise GuardError('safe-acp-agent-required')
        if set(agent) - {'command', 'args', 'env_names'} or not isinstance(agent.get('command'), str):
            raise GuardError('acp-command-and-environment-references-only')
        if not isinstance(agent.get('args', []), list) or not all(isinstance(a, str) for a in agent.get('args', [])):
            raise GuardError('acp-string-arguments-required')
        if any(not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', v) for v in agent.get('env_names', [])):
            raise GuardError('acp-environment-names-required')
    changed = atomic_json(pathlib.Path(state) / 'acp-agents.json', {'version': 1, 'agents': agents})
    return {'agents': len(agents), 'changed': changed, 'status': 'exported', 'transport': 'stdio', 'agentsStarted': 0}
