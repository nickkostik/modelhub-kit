"""Receipt-owned native MCP configuration; no agent launches or credential reads."""
import copy
import hashlib
import json
import os
import pathlib
import re
import tomllib
import urllib.parse
from .core import GuardError, atomic_json, atomic_text, protected, read_json

MCP_CLIENTS = ('codex', 'claude', 'opencode', 'cursor', 'qwen', 'copilot', 'devin', 'muse')
SUPPORTED_CLIENTS = MCP_CLIENTS
SAFE_ID = re.compile(r'[a-z][a-z0-9_-]{0,63}')
ENV_NAME = re.compile(r'[A-Z][A-Z0-9_]{0,127}')


def safe_url(url):
    if not isinstance(url, str):
        raise GuardError('https-url-required')
    p = urllib.parse.urlsplit(url)
    try:
        p.port
    except ValueError:
        raise GuardError('valid-https-port-required') from None
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.query or p.fragment:
        raise GuardError('public-https-url-without-secrets-required')
    return url


def servers(config):
    result = {}
    for name, item in config.get('mcp_servers', {}).items():
        if not SAFE_ID.fullmatch(name) or not isinstance(item, dict):
            raise GuardError('safe-server-name-required')
        if 'url' in item:
            if set(item) - {'url', 'bearer_env'}:
                raise GuardError('remote-mcp-url-and-environment-reference-only')
            safe_url(item.get('url'))
            env = item.get('bearer_env')
            if env and (not isinstance(env, str) or not ENV_NAME.fullmatch(env)):
                raise GuardError('credential-environment-name-required')
            result[name] = {'url': item['url'], **({'bearer_env': env} if env else {})}
        else:
            if set(item) - {'command', 'args', 'env_names'} or not isinstance(item.get('command'), str) or not item['command'] or any(c in item['command'] for c in '\r\n\0'):
                raise GuardError('literal-mcp-command-required')
            if not isinstance(item.get('args', []), list) or not all(isinstance(a, str) and not any(c in a for c in '\r\n\0') for a in item.get('args', [])):
                raise GuardError('literal-mcp-arguments-required')
            if not isinstance(item.get('env_names', []), list) or any(not isinstance(n, str) or not ENV_NAME.fullmatch(n) for n in item.get('env_names', [])):
                raise GuardError('mcp-environment-names-required')
            result[name] = {'command': item['command'], 'args': item.get('args', []), 'env_names': item.get('env_names', [])}
    return result


def native_version(config, home, state, client):
    """Use checked install metadata; never launch a harness just to discover it."""
    override = config.get('client_versions', {}).get(client)
    if override is not None:
        return override if isinstance(override, str) and re.fullmatch(r'v?\d+\.\d+(?:\.\d+)?', override) else None
    try:
        from .updater import describe, tools
        product = {'devin': 'devin-cli', 'cursor': 'cursor-cli'}.get(client, client)
        definition = tools().get(product)
        if not definition:
            return None
        metadata = describe(product, definition, state, home=home)
        verified = ('owned-native-installation', 'owned-npm-installation', 'native-npm-metadata-verified', 'native-metadata-verified')
        version = metadata.get('version') if isinstance(metadata, dict) and metadata.get('present') is True and metadata.get('status') in verified else None
        return version if version_parts(version) is not None else None
    except (ImportError, GuardError, OSError, ValueError, KeyError, TypeError):
        return None


def version_parts(value):
    return tuple(int(x) for x in value.lstrip('v').split('.')) if isinstance(value, str) and re.fullmatch(r'v?\d+\.\d+(?:\.\d+)?', value) else None


def read_client(path):
    path = pathlib.Path(path)
    if not path.exists() and not path.is_symlink():
        return ''
    if protected(path).st_size > 4 * 1024 * 1024:
        raise GuardError('oversized-client-config')
    return path.read_text(encoding='utf-8')


def merge_owned(document, field_path, rendered, owned):
    """Copy-on-write; only an exact receipt can authorize replacing an entry."""
    result = copy.deepcopy(document)
    if not isinstance(result, dict) or not isinstance(owned, dict):
        raise GuardError('object-native-config-and-receipt-required')
    entries = result
    for field in field_path:
        entries = entries.setdefault(field, {})
        if not isinstance(entries, dict):
            raise GuardError('object-native-entry-map-required')
    toggles = {}
    for name, old in owned.items():
        if name not in entries:
            if name in rendered:
                raise GuardError('removed-managed-entry-preserved')
            continue
        value = entries[name]
        if not isinstance(value, dict) or not isinstance(old, dict):
            if value != old:
                raise GuardError('customized-managed-entry-preserved')
            continue
        toggle = {k: v for k, v in value.items() if k in ('enabled', 'disabled') and isinstance(v, bool)}
        if any(value.get(k) != v for k, v in old.items() if k not in toggle):
            raise GuardError('customized-managed-entry-preserved')
        toggles[name] = toggle
    for name in rendered:
        if name in entries and name not in owned:
            raise GuardError('custom-native-entry-preserved')
    for name, old in owned.items():
        value = entries.get(name)
        customized = isinstance(value, dict) and isinstance(old, dict) and (set(value) - set(old) or value.get('enabled') is False or value.get('disabled') is True or any(value.get(k) != old.get(k) for k in ('enabled', 'disabled')))
        if name not in rendered and customized:
            continue  # Release ownership without deleting user additions/disablement.
        entries.pop(name, None)
    actual = copy.deepcopy(rendered)
    for name, value in actual.items():
        if isinstance(value, dict):
            value.update(toggles.get(name, {}))
    # Extra user settings are outside the receipt's owned keys.
    for name, value in actual.items():
        previous = document
        for field in field_path:
            previous = previous.get(field, {})
        extra = {k: v for k, v in previous.get(name, {}).items() if k not in owned.get(name, {})} if isinstance(previous.get(name), dict) and isinstance(value, dict) else {}
        entries[name] = {**extra, **value} if isinstance(value, dict) else value
    return result, actual


def owned_json(path, receipt_file, field_path, rendered, apply=False):
    before = read_client(path)
    document = json.loads(before.lstrip('\ufeff')) if before.strip() else {}
    receipt_before = read_client(receipt_file)
    receipt = json.loads(receipt_before) if receipt_before else {'entries': {}}
    if not isinstance(receipt, dict) or receipt.get('version', 1) != 1:
        raise GuardError('unknown-native-receipt-version')
    if receipt.get('path', str(path)) != str(path) or receipt.get('field', list(field_path)) != list(field_path):
        raise GuardError('native-receipt-schema-changed')
    if not rendered and not receipt.get('entries'):
        return {'status': 'no-configured-entries', 'changed': False}
    after_doc, entries = merge_owned(document, field_path, rendered, receipt.get('entries', {}))
    after = json.dumps(after_doc, indent=2, ensure_ascii=False) + '\n'
    if len(after.encode('utf-8')) > 4 * 1024 * 1024:
        raise GuardError('native-config-size-budget-exceeded')
    changed = after_doc != document
    if apply:
        if read_client(receipt_file) != receipt_before:
            raise GuardError('native-receipt-changed-concurrently')
        if changed:
            atomic_text(path, after, expected=before)
        elif read_client(path) != before:
            raise GuardError('client-config-changed-concurrently')
        atomic_text(receipt_file, json.dumps({'version': 1, 'path': str(path), 'field': list(field_path), 'entries': entries}, indent=2, ensure_ascii=False) + '\n', expected=receipt_before)
    return {'status': 'applied' if apply else 'planned', 'changed': changed}


def owned_toml(path, receipt_file, body, collision_fields, apply=False, marker='mcp'):
    before = read_client(path)
    tomllib.loads(before)
    receipt_before = read_client(receipt_file)
    receipt = json.loads(receipt_before) if receipt_before else {}
    if not isinstance(receipt, dict) or receipt.get('version', 1) != 1 or receipt.get('path', str(path)) != str(path):
        raise GuardError('unknown-native-receipt-version-or-path')
    # Retain the original v0.1 MCP marker so existing receipts remain usable.
    begin, end = ('# modelhub-kit:begin', '# modelhub-kit:end') if marker == 'mcp' else ('# modelhub-kit:' + marker + ':begin', '# modelhub-kit:' + marker + ':end')
    if before.count(begin) != before.count(end) or before.count(begin) > 1:
        raise GuardError('ambiguous-owned-toml-block')
    outside = before
    if begin in before:
        start, stop = before.index(begin), before.index(end) + len(end)
        if hashlib.sha256(before[start:stop].encode()).hexdigest() != receipt.get('blockSha256'):
            raise GuardError('customized-owned-block-preserved')
        outside = before[:start] + before[stop:]
    parsed = tomllib.loads(outside)
    for field, names in collision_fields.items():
        if set(parsed.get(field, {})) & set(names):
            raise GuardError('custom-native-entry-preserved')
    if not body and not receipt:
        return {'status': 'no-configured-entries', 'changed': False}
    block = begin + '\n' + body.rstrip() + '\n' + end
    after = outside.rstrip() + '\n\n' + block + '\n'
    tomllib.loads(after)
    if len(after.encode('utf-8')) > 4 * 1024 * 1024:
        raise GuardError('native-config-size-budget-exceeded')
    changed = before != after
    if apply:
        if read_client(receipt_file) != receipt_before:
            raise GuardError('native-receipt-changed-concurrently')
        atomic_text(path, after, expected=before)
        atomic_text(receipt_file, json.dumps({'version': 1, 'path': str(path), 'blockSha256': hashlib.sha256(block.encode()).hexdigest()}, indent=2) + '\n', expected=receipt_before)
    return {'status': 'applied' if apply else 'planned', 'changed': changed}


def queued(client, reason):
    return {'client': client, 'status': 'queued', 'reason': reason, 'changed': False, 'agentsStarted': 0}


def native_idle(client):
    from .cache import process_inventory
    processes = process_inventory()
    if processes is None:
        return False
    pattern = {
        't3': r'(?:@t3tools[/\\]code|t3code|t3-code)',
        'paseo': r'(?:paseo|@getpaseo[/\\](?:cli|server))',
        'codex': r'(?:codex|@openai[/\\]codex)',
        'claude': r'(?:claude|claude-code|@anthropic-ai[/\\]claude-code)',
        'opencode': r'(?:opencode|opencode-ai)',
        'qwen': r'(?:qwen|@qwen-code[/\\]qwen-code)',
        'pi': r'(?:pi|@earendil-works[/\\]pi-coding-agent)',
        'copilot': r'(?:copilot|@github[/\\]copilot)',
        'devin': r'(?:devin(?:-desktop|-cli)?|windsurf)',
        'cursor': r'(?:cursor|cursor-agent)',
        'muse': r'(?:muse|muse-code)',
    }.get(client, re.escape(client))
    return not any(re.search(r'(?i)(?:^|[/\\\s])' + pattern + r'(?:[.\s/\\]|$)', p) for p in processes)


def native_json(client, path, receipt_file, fields, rendered, apply=False):
    plan = owned_json(path, receipt_file, fields, rendered, False)
    if apply and plan['changed'] and not native_idle(client):
        return queued(client, 'active-or-unknown-native-runtime-preserved')
    return owned_json(path, receipt_file, fields, rendered, True) if apply else plan


def integrate(config, home, state, client, apply=False):
    if client not in MCP_CLIENTS:
        return queued(client, 'unsupported-native-mcp-schema')
    definitions = servers(config)
    home, state = pathlib.Path(home), pathlib.Path(state)
    receipt_file = state / ('integration-' + client + '.json')
    if not definitions and not receipt_file.exists() and not receipt_file.is_symlink():
        return {'client': client, 'status': 'no-configured-servers', 'changed': False, 'agentsStarted': 0}
    paths = {'codex': '.codex/config.toml', 'claude': '.claude.json', 'opencode': '.config/opencode/opencode.json',
             'cursor': '.cursor/mcp.json', 'qwen': '.qwen/settings.json', 'copilot': '.copilot/mcp-config.json',
             'muse': '.config/muse/settings.json'}
    if client in ('qwen', 'copilot'):
        version = version_parts(native_version(config, home, state, client))
        if not version or version[0] not in (0, 1):
            return queued(client, 'unknown-native-mcp-schema-version')
    if client == 'devin':
        version = version_parts(native_version(config, home, state, client))
        if not version or version[0] != 3000:
            return queued(client, 'unknown-devin-config-version')
        filename = 'mcp_config.json' if version >= (3000, 3) else 'config.json'
        path = (pathlib.Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'devin' if os.name == 'nt' else home / '.config/devin') / filename
    else:
        path = home / paths[client]
        if client == 'codex':
            path = pathlib.Path(os.environ.get('CODEX_HOME', home / '.codex')) / 'config.toml'
        elif client == 'claude' and os.environ.get('CLAUDE_CONFIG_DIR'):
            path = pathlib.Path(os.environ['CLAUDE_CONFIG_DIR']) / '.claude.json'
        elif client in ('opencode', 'muse'):
            path = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / client / path.name
    if client == 'codex':
        lines = []
        for name, item in definitions.items():
            lines += ['[mcp_servers.' + name + ']']
            if 'url' in item:
                lines += ['url = ' + json.dumps(item['url'])]
            else:
                lines += ['command = ' + json.dumps(item['command']), 'args = ' + json.dumps(item['args']), 'env_vars = ' + json.dumps(item['env_names'])]
            if item.get('bearer_env'):
                lines += ['bearer_token_env_var = ' + json.dumps(item['bearer_env'])]
        result = owned_toml(path, receipt_file, '\n'.join(lines), {'mcp_servers': definitions}, False)
        if apply and result['changed'] and not native_idle(client):
            return queued(client, 'active-or-unknown-native-runtime-preserved')
        if apply:
            result = owned_toml(path, receipt_file, '\n'.join(lines), {'mcp_servers': definitions}, True)
    else:
        field = 'mcp' if client == 'opencode' else 'mcpServers'
        if client == 'muse':
            version = version_parts(native_version(config, home, state, client))
            if not version or version[:2] != (1, 3):
                return queued(client, 'unknown-muse-config-version')
            doc = json.loads((read_client(path) or '{}').lstrip('\ufeff'))
            if not isinstance(doc, dict) or ('mcp_servers' in doc and 'mcpServers' in doc):
                return queued(client, 'ambiguous-muse-mcp-schema')
            field = 'mcpServers' if 'mcpServers' in doc else 'mcp_servers'
            if any(v.get('bearer_env') for v in definitions.values()):
                return queued(client, 'muse-http-header-environment-interpolation-unsupported-use-native-oauth')
        rendered = {}
        for name, item in definitions.items():
            if 'command' in item:
                if client == 'copilot' and item['env_names']:
                    return queued(client, 'copilot-stdio-environment-interpolation-unverified')
                if client == 'opencode':
                    value = {'type': 'local', 'command': [item['command'], *item['args']], 'enabled': True, 'environment': {n: '{env:' + n + '}' for n in item['env_names']}}
                else:
                    value = {'command': item['command'], 'args': item['args']}
                    value['env'] = {n: '${env:' + n + '}' if client in ('devin', 'cursor') else '${' + n + '}' for n in item['env_names']}
                    if client == 'copilot':
                        value.update(type='local', tools=['*'])
                    elif client == 'muse':
                        value['type' if field == 'mcpServers' else 'transport'] = 'stdio'
                rendered[name] = value
                continue
            value = {'url': item['url']}
            if client == 'opencode':
                value.update(type='remote', enabled=True)
            elif client in ('claude', 'copilot'):
                value['type'] = 'http'
                if client == 'copilot':
                    value['tools'] = ['*']
            elif client == 'devin':
                value['transport'] = 'http'
            elif client == 'qwen':
                value = {'httpUrl': item['url']}
            elif client == 'muse':
                value['type' if field == 'mcpServers' else 'transport'] = 'streamable-http' if field == 'mcpServers' else 'streamable_http'
            if item.get('bearer_env'):
                if client == 'copilot':
                    return queued(client, 'copilot-header-environment-interpolation-unverified')
                env = item['bearer_env']
                reference = ('{env:' + env + '}') if client == 'opencode' else ('${env:' + env + '}') if client in ('cursor', 'devin') else ('${' + env + '}')
                value['headers'] = {'Authorization': 'Bearer ' + reference}
                if client == 'opencode':
                    value['oauth'] = False
            rendered[name] = value
        result = native_json(client, path, receipt_file, (field,), rendered, apply)
    if result['status'] == 'no-configured-entries':
        result['status'] = 'no-configured-servers'
    return {'client': client, **result, 'agentsStarted': 0}


def export_acp(config, state):
    from .acp import definitions
    agents = definitions(config)
    changed = atomic_json(pathlib.Path(state) / 'acp-agents.json', {'version': 1, 'agents': agents})
    return {'agents': len(agents), 'changed': changed, 'status': 'exported', 'transport': 'stdio', 'agentsStarted': 0}
