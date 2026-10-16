"""Register literal local ACP commands in documented native consumer schemas."""
import pathlib
import re
import shutil
from .core import GuardError
from .integration import (ENV_NAME, SAFE_ID, native_version, owned_json, native_json, queued,
                          read_client, version_parts)
from .projection import receipt_entries, retirement_guard
import json

ACP_CLIENTS = ('paseo', 't3', 'devin')


def definitions(config):
    agents = config.get('acp_agents', {})
    if not isinstance(agents, dict):
        raise GuardError('object-acp-agents-required')
    for name, agent in agents.items():
        if not SAFE_ID.fullmatch(name) or not isinstance(agent, dict) or set(agent) - {'command', 'args', 'env_names'}:
            raise GuardError('safe-acp-agent-required')
        if not isinstance(agent.get('command'), str) or not agent['command'] or any(c in agent['command'] for c in '\r\n\0'):
            raise GuardError('literal-acp-command-required')
        if not isinstance(agent.get('args', []), list) or not all(isinstance(a, str) and not any(c in a for c in '\r\n\0') for a in agent.get('args', [])):
            raise GuardError('literal-acp-arguments-required')
        if not isinstance(agent.get('env_names', []), list) or any(not isinstance(v, str) or not ENV_NAME.fullmatch(v) for v in agent.get('env_names', [])):
            raise GuardError('acp-environment-names-required')
    return agents


def t3_idle():
    from .cache import process_inventory
    processes = process_inventory()
    return processes is not None and not any(re.search(r'(?i)(?:@t3tools[/\\]code|t3code|t3-code)', p) for p in processes)


def register(config, home, state, client, apply=False):
    try:
        return _register(config, home, state, client, apply)
    except GuardError as error:
        return queued(client, str(error))
    except (ValueError, TypeError, KeyError):
        return queued(client, 'native-acp-schema-review-required')


def _register(config, home, state, client, apply=False):
    agents = definitions(config)
    if client == 'devin':
        return queued(client, 'devin-is-an-acp-agent-not-a-documented-acp-consumer')
    if client not in ACP_CLIENTS:
        return queued(client, 'unsupported-native-acp-consumer')
    home, state = pathlib.Path(home), pathlib.Path(state)
    receipt = state / ('acp-' + client + '.json')
    if not agents and not read_client(receipt):
        return {'client': client, 'status': 'no-configured-agents', 'changed': False, 'agentsStarted': 0}
    version = version_parts(native_version(config, home, state, client))
    if version is None:
        return queued(client, 'unknown-native-acp-consumer-version')
    missing = [name for name, a in agents.items() if not shutil.which(a['command'])]
    if missing:
        return {**queued(client, 'required-acp-command-not-installed-select-adapter-tool-explicitly'), 'agents': missing}
    if client == 'paseo':
        if any(not re.fullmatch(r'[a-z][a-z0-9-]{0,59}', name) for name in agents):
            return queued(client, 'paseo-provider-id-requires-lowercase-letters-digits-hyphens')
        path = home / '.paseo/config.json'
        existing = json.loads((read_client(path) or '{}').lstrip('\ufeff'))
        if not isinstance(existing, dict) or existing.get('version', 1) != 1:
            return queued(client, 'unknown-paseo-provider-config-schema')
        if not isinstance(existing.get('agents', {}), dict) or not isinstance(existing.get('agents', {}).get('providers', {}), dict):
            return queued(client, 'object-paseo-provider-map-required')
        rendered = {'kit-' + name: {'extends': 'acp', 'label': name, 'command': [a['command'], *a.get('args', [])]} for name, a in agents.items()}
        owned = receipt_entries(receipt, path, ('agents', 'providers'))
        entries = existing.get('agents', {}).get('providers', {})
        if any(isinstance(entries.get(name), dict) and 'additionalModels' in entries[name] for name in set(owned) - set(rendered)):
            return queued(client, 'retire-paseo-model-projection-first-then-retry-acp-retirement')
        retirement_guard(existing, ('agents', 'providers'), owned, rendered)
        result = native_json(client, path, receipt, ('agents', 'providers'), rendered, apply)
    else:
        if version[:2] != (0, 1):
            return queued(client, 'unknown-t3-provider-instance-version')
        path = home / '.t3/userdata/settings.json'
        existing = json.loads((read_client(path) or '{}').lstrip('\ufeff'))
        if not isinstance(existing, dict):
            return queued(client, 'unknown-t3-provider-instance-schema')
        instances = existing.get('providerInstances')
        if not isinstance(instances, dict) or 'providers' in existing or any(not isinstance(v, dict) or not isinstance(v.get('driver'), str) or not isinstance(v.get('config', {}), dict) for v in instances.values()):
            return queued(client, 'unknown-t3-provider-instance-schema')
        rendered = {'kit-' + name: {'driver': 'acpRegistry', 'displayName': name,
                    'config': {'source': 'local', 'commandPath': a['command'], 'commandArgs': a.get('args', [])}}
                    for name, a in agents.items()}
        retirement_guard(existing, ('providerInstances',), receipt_entries(receipt, path, ('providerInstances',)), rendered)
        result = owned_json(path, receipt, ('providerInstances',), rendered, False)
        if result['changed'] and apply and not t3_idle():
            return queued(client, 'active-or-unknown-t3-runtime-preserved')
        if apply:
            result = owned_json(path, receipt, ('providerInstances',), rendered, True)
    return {'client': client, **result, 'agentsStarted': 0,
            'environmentNames': sorted({n for a in agents.values() for n in a.get('env_names', [])})}
