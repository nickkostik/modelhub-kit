"""Project validated catalog metadata into opt-in native providers and profiles."""
import copy
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import tomllib
import urllib.parse
from .catalog import models
from . import catalog
from .core import GuardError, protected, read_json
from .integration import (ENV_NAME, SAFE_ID, native_version, native_idle, native_json, owned_json, owned_toml,
                          queued, read_client, safe_url, version_parts)

MODEL_CLIENTS = ('codex', 'opencode', 'pi', 'paseo', 't3', 'devin', 'muse', 'claude', 'cursor')
PROTOCOLS = ('openai-responses', 'openai-completions', 'anthropic-messages')


def provider_url(url):
    """Permit unencrypted adapters only on a literal loopback address."""
    if not isinstance(url, str) or re.search(r'[\s\x00-\x1f\x7f]', url):
        raise GuardError('provider-url-without-whitespace-required')
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port
        if parsed.scheme == 'http':
            if (not parsed.hostname or '%' in parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment):
                raise ValueError('nonlocal-http')
            address = ipaddress.ip_address(parsed.hostname)
            if not address.is_loopback or address.version == 6 and address != ipaddress.IPv6Address('::1'):
                raise ValueError('nonlocal-http')
            return url
    except ValueError:
        raise GuardError('https-or-literal-loopback-provider-url-required') from None
    return safe_url(url)


def receipt_names(state, prefix):
    """Discover bounded receipt names, never arbitrary paths supplied by a receipt."""
    state = pathlib.Path(state)
    if not state.exists():
        return set()
    protected(state, True)
    names = set()
    for count, file in enumerate(state.iterdir(), 1):
        if count > 4096:
            raise GuardError('native-receipt-discovery-budget')
        if file.name.startswith(prefix) and file.name.endswith('.json'):
            name = file.name[len(prefix):-5]
            if not name.startswith('kit-') or not SAFE_ID.fullmatch(name[4:]):
                raise GuardError('unrecognized-native-provider-receipt-review-required')
            names.add(name)
            if len(names) > 128:
                raise GuardError('native-provider-retirement-budget')
    return names


def receipt_entries(receipt_file, path, fields):
    raw = read_client(receipt_file)
    if not raw:
        return {}
    receipt = json.loads(raw)
    if not isinstance(receipt, dict) or receipt.get('version') != 1 or receipt.get('path') != str(path) or receipt.get('field') != list(fields) or not isinstance(receipt.get('entries'), dict):
        raise GuardError('native-retirement-receipt-schema-review-required')
    names = receipt['entries']
    if fields in (('provider',), ('providers',), ('agents', 'providers'), ('providerInstances',)):
        if any(not name.startswith('kit-') or not SAFE_ID.fullmatch(name[4:]) for name in names):
            raise GuardError('unrecognized-native-provider-receipt-review-required')
    elif not fields and set(names) - {'models'} or len(fields) == 3 and fields[:2] == ('agents', 'providers') and set(names) - {'additionalModels'}:
        raise GuardError('native-retirement-receipt-schema-review-required')
    return receipt['entries']


def references_route(value, names):
    if isinstance(value, dict):
        return any(references_route(v, names) for v in value.values())
    if isinstance(value, list):
        return any(references_route(v, names) for v in value)
    return isinstance(value, str) and any(value == name or value.startswith(name + '/') or pathlib.PurePath(value).name in (name + '.config.toml', name + '.json') for name in names)


def retirement_guard(document, fields, owned, rendered):
    """Retiring custom/disabled entries must queue, not silently lose ownership."""
    if not isinstance(document, dict) or not isinstance(owned, dict) or not isinstance(rendered, dict):
        raise GuardError('object-native-config-and-receipt-required')
    removed = set(owned) - set(rendered)
    outside = copy.deepcopy(document)
    entries = outside
    for field in fields:
        entries = entries.get(field, {})
        if not isinstance(entries, dict):
            raise GuardError('object-native-entry-map-required')
    for name in removed:
        if name in entries:
            value = entries[name]
            if value != owned[name] or isinstance(value, dict) and (value.get('enabled') is False or value.get('disabled') is True):
                raise GuardError('customized-or-disabled-native-route-review-before-retirement')
            entries.pop(name)
    if removed and references_route(outside, removed):
        raise GuardError('native-settings-reference-retired-route-review-before-retirement')
    return removed


def unlink_owned_files(plans, apply):
    """Preflight the whole set before exact-file CAS removal; retain custom files."""
    for path, before in plans:
        if read_client(path) != before:
            raise GuardError('native-retirement-file-changed-concurrently')
    changed = False
    if apply:
        try:
            for path, before in plans:
                if before:
                    if read_client(path) != before:
                        raise GuardError('native-retirement-file-changed-concurrently')
                    protected(path)
                    path.unlink()
                    changed = True
        except (GuardError, OSError) as error:
            deferred = GuardError('native-retirement-file-changed-concurrently')
            deferred.changed = changed
            raise deferred from error
    return changed if apply else any(before for _, before in plans)


def codex_retirement(root, state, names):
    plans = []
    if names:
        default = read_client(root / 'config.toml')
        if references_route(tomllib.loads(default), names):
            raise GuardError('native-default-references-retired-profile-review-before-retirement')
    for name in sorted(names):
        file = state / 'projections/codex' / (name + '.json')
        catalog_receipt = state / ('projection-codex-catalog-' + name + '.json')
        profile = root / (name + '.config.toml')
        profile_receipt = state / ('projection-codex-profile-' + name + '.json')
        before, proof = read_client(file), read_client(catalog_receipt)
        owned = receipt_entries(catalog_receipt, file, ()) if proof else {}
        if before or file.exists():
            if not proof or json.loads(before) != owned:
                raise GuardError('customized-codex-catalog-review-before-retirement')
        profile_before, profile_proof = read_client(profile), read_client(profile_receipt)
        if profile_proof:
            record = json.loads(profile_proof)
            if not isinstance(record, dict) or record.get('version', 1) != 1 or record.get('path') != str(profile):
                raise GuardError('native-retirement-receipt-schema-review-required')
        if profile_before or profile.exists():
            begin, end = '# modelhub-kit:profile:begin', '# modelhub-kit:profile:end'
            if not profile_proof or profile_before.count(begin) != 1 or profile_before.count(end) != 1:
                raise GuardError('customized-codex-profile-review-before-retirement')
            start, stop = profile_before.index(begin), profile_before.index(end) + len(end)
            if stop <= start or (profile_before[:start] + profile_before[stop:]).strip() or hashlib.sha256(profile_before[start:stop].encode()).hexdigest() != record.get('blockSha256'):
                raise GuardError('customized-codex-profile-review-before-retirement')
        # Retire the profile before its catalog; receipts are kept until both
        # native files have gone, so interrupted retirement remains resumable.
        plans += [(profile, profile_before), (file, before), (profile_receipt, profile_proof), (catalog_receipt, proof)]
    return plans


def efforts(row):
    """Preserve exact advertised strings, including new or custom effort names."""
    values = []
    def add(items):
        if isinstance(items, list):
            for item in items:
                value = item if isinstance(item, str) else item.get('effort', item.get('id')) if isinstance(item, dict) else None
                if not isinstance(value, str) or not value or len(value) > 128:
                    raise GuardError('exact-reasoning-effort-strings-required')
                if value not in values:
                    values.append(value)
    for field in ('supported_reasoning_levels', 'supportedReasoningEfforts', 'supported_reasoning_efforts', 'reasoning_efforts', 'reasoningEfforts', 'reasoning_levels'):
        add(row.get(field))
    reasoning = row.get('reasoning')
    if isinstance(reasoning, dict):
        for field in ('levels', 'efforts'):
            add(reasoning.get(field))
        for option in reasoning.get('options', []):
            if isinstance(option, dict) and option.get('type') == 'effort':
                add(option.get('values'))
    if isinstance(row.get('variants'), dict):
        add(list(row['variants']))
    return values


def providers(config, state):
    result = {}
    sources = {s['id']: s for s in config.get('catalog_sources', [])}
    for name, item in config.get('model_providers', {}).items():
        if not SAFE_ID.fullmatch(name) or not isinstance(item, dict) or set(item) - {'source', 'base_url', 'credential_env', 'protocol'}:
            raise GuardError('safe-model-provider-required')
        provider_url(item.get('base_url'))
        if item.get('protocol') not in PROTOCOLS or not isinstance(item.get('credential_env'), str) or not ENV_NAME.fullmatch(item['credential_env']):
            raise GuardError('explicit-provider-protocol-and-credential-environment-required')
        source = sources.get(item.get('source'))
        if source is None or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', source['id']):
            raise GuardError('configured-catalog-source-required')
        record = read_json(pathlib.Path(state) / 'catalogs' / (source['id'] + '.json'))
        expected = catalog.source_fingerprint(source)
        if not isinstance(record, dict) or record.get('sourceFingerprint') != expected:
            raise GuardError('matching-last-good-source-catalog-required')
        rows = models({'models': record.get('models')})
        for row in rows:
            efforts(row)
            if not isinstance(label(row), str) or not label(row) or len(label(row)) > 1024:
                raise GuardError('native-model-label-string-required')
            for field in ('context_window', 'max_output_tokens'):
                if field in row and (type(row[field]) is not int or row[field] <= 0):
                    raise GuardError('positive-native-model-token-metadata-required')
            if 'input_modalities' in row and (not isinstance(row['input_modalities'], list) or not all(isinstance(v, str) for v in row['input_modalities'])):
                raise GuardError('native-model-input-modality-list-required')
        result['kit-' + name] = {**item, 'rows': rows}
    return result


def label(row):
    return row.get('name', row.get('display_name', row['id']))


def opencode_model(row, protocol):
    value = {'id': row['id'], 'name': label(row)}
    limits = row.get('limits', {})
    context, output = row.get('context_window', limits.get('context')), row.get('max_output_tokens', limits.get('output'))
    if context and output:
        if type(context) is not int or type(output) is not int or context <= 0 or output <= 0:
            raise GuardError('positive-native-model-limits-required')
        value['limit'] = {'context': context, 'output': output}
    variants = copy.deepcopy(row.get('variants', {}))
    if not isinstance(variants, dict) or not all(isinstance(v, dict) for v in variants.values()):
        raise GuardError('native-reasoning-variant-options-required')
    for effort in efforts(row):
        if effort not in variants:
            if protocol == 'anthropic-messages':
                raise GuardError('anthropic-effort-wire-variants-required')
            variants[effort] = {'reasoningEffort': effort}
    if variants:
        value['variants'] = variants
    return value


def pi_model(row):
    levels = efforts(row)
    reasoning = row.get('reasoning', {})
    wire = row.get('thinkingLevelMap', reasoning.get('thinkingLevelMap') if isinstance(reasoning, dict) else None)
    native = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
    if wire is None:
        wire = {('off' if e == 'none' else e): e for e in levels}
    if not isinstance(wire, dict) or any(v is not None and not isinstance(v, str) for v in wire.values()) or set(wire) - set(native) or not set(levels) <= {v for v in wire.values() if isinstance(v, str)}:
        raise GuardError('pi-native-thinking-level-map-cannot-represent-all-efforts')
    value = {'id': row['id'], 'name': label(row), 'reasoning': bool(levels)}
    if levels:
        value['thinkingLevelMap'] = {n: wire.get(n) for n in native}
    if row.get('context_window'):
        value['contextWindow'] = row['context_window']
    if row.get('max_output_tokens'):
        value['maxTokens'] = row['max_output_tokens']
    if row.get('input_modalities'):
        value['input'] = copy.deepcopy(row['input_modalities'])
    return value


def codex_catalog(template, rows):
    result = []
    for index, row in enumerate(rows):
        value = copy.deepcopy(template)
        values = efforts(row)
        default = row.get('default_reasoning_level', row.get('defaultReasoningEffort'))
        if default is not None and default not in values:
            raise GuardError('source-default-effort-not-in-advertised-levels')
        value.update(slug=row['id'], display_name=label(row), priority=index, visibility='list', supported_in_api=True,
                     supported_reasoning_levels=[{'effort': e, 'description': e} for e in values], default_reasoning_level=default)
        for field in ('context_window', 'max_output_tokens', 'input_modalities'):
            if field in row:
                value[field] = copy.deepcopy(row[field])
        if isinstance(row.get('context_window'), int) and row['context_window'] > 0:
            context = row['context_window']
            value['max_context_window'] = context
            threshold = context * 85 // 100
            output = row.get('max_output_tokens')
            if type(output) is int and output > 0:
                threshold = min(threshold, context - output)
            value['auto_compact_token_limit'] = max(1, threshold)
        value.pop('tool_mode', None)
        result.append(value)
    return {'models': result}


def project(config, home, state, client, apply=False):
    if client not in MODEL_CLIENTS:
        return queued(client, 'unsupported-native-model-projection')
    home, state = pathlib.Path(home), pathlib.Path(state)
    receipt = state / ('projection-' + client + '.json')
    try:
        previous = (receipt_names(state, 'projection-codex-catalog-') | receipt_names(state, 'projection-codex-profile-')) if client == 'codex' else receipt_names(state, 'projection-paseo-') if client == 'paseo' else set()
        if not config.get('model_providers') and not previous and not read_client(receipt):
            return {'client': client, 'status': 'no-configured-providers', 'changed': False}
    except GuardError as error:
        return queued(client, str(error))
    if client in ('t3', 'devin', 'muse', 'claude', 'cursor'):
        return queued(client, 'native-models-and-efforts-use-agent-discovery-no-documented-custom-provider-schema')
    try:
        selected = providers(config, state)
    except GuardError as error:
        return queued(client, str(error))
    version = version_parts(native_version(config, home, state, client))
    if version is None or client != 'codex' and version[0] not in (0, 1):
        return queued(client, 'unknown-native-model-provider-version')
    changed_before_error = False
    try:
        if client == 'opencode':
            packages = {'openai-responses': '@ai-sdk/openai', 'openai-completions': '@ai-sdk/openai-compatible', 'anthropic-messages': '@ai-sdk/anthropic'}
            rendered = {name: {'name': name, 'npm': packages[p['protocol']],
                        'options': {'baseURL': p['base_url'], 'apiKey': '{env:' + p['credential_env'] + '}'},
                        'models': {r['id']: opencode_model(r, p['protocol']) for r in p['rows']}} for name, p in selected.items()}
            path = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'opencode/opencode.json'
            retirement_guard(json.loads((read_client(path) or '{}').lstrip('\ufeff')), ('provider',), receipt_entries(receipt, path, ('provider',)), rendered)
            result = native_json(client, path, receipt, ('provider',), rendered, apply)
        elif client == 'pi':
            rendered = {name: {'baseUrl': p['base_url'].rstrip('/')[:-3] if p['protocol'] == 'anthropic-messages' and p['base_url'].rstrip('/').endswith('/v1') else p['base_url'], 'api': p['protocol'], 'apiKey': '${' + p['credential_env'] + '}',
                        'models': [pi_model(r) for r in p['rows']]} for name, p in selected.items()}
            path = pathlib.Path(os.environ.get('PI_CODING_AGENT_DIR', home / '.pi/agent')) / 'models.json'
            removed = retirement_guard(json.loads((read_client(path) or '{}').lstrip('\ufeff')), ('providers',), receipt_entries(receipt, path, ('providers',)), rendered)
            if references_route(json.loads((read_client(path.parent / 'settings.json') or '{}').lstrip('\ufeff')), removed):
                return queued(client, 'native-default-references-retired-provider-review-before-retirement')
            result = native_json(client, path, receipt, ('providers',), rendered, apply)
        elif client == 'paseo':
            path = home / '.paseo/config.json'
            current = json.loads((read_client(path) or '{}').lstrip('\ufeff'))
            if not isinstance(current, dict) or not isinstance(current.get('agents', {}), dict) or not isinstance(current.get('agents', {}).get('providers', {}), dict):
                raise GuardError('object-paseo-provider-map-required')
            proof = read_json(state / 'acp-paseo.json', {'entries': {}}).get('entries', {})
            providers_now = current.get('agents', {}).get('providers', {})
            if any(name not in proof or providers_now.get(name, {}).get('command') != proof[name].get('command') for name in selected):
                return queued(client, 'register-matching-owned-acp-provider-first')
            native_file = pathlib.Path(os.environ.get('XDG_CONFIG_HOME', home / '.config')) / 'opencode/opencode.json'
            native_doc = json.loads((read_client(native_file) or '{}').lstrip('\ufeff'))
            native_proof = read_json(state / 'projection-opencode.json', {'entries': {}}).get('entries', {})
            plans = []
            for name in sorted(previous - set(selected)):
                file = state / ('projection-paseo-' + name + '.json')
                owned = receipt_entries(file, path, ('agents', 'providers', name))
                if owned and name not in providers_now:
                    return queued(client, 'paseo-provider-missing-review-model-receipt-before-retirement')
                retirement_guard(current, ('agents', 'providers', name), owned, {})
                plans.append((file, name, {}))
                owned_json(path, file, ('agents', 'providers', name), {}, False)
            for name, provider in selected.items():
                agent = config.get('acp_agents', {}).get(name.removeprefix('kit-'), {})
                binary = pathlib.Path(agent.get('command', '')).name.lower().removesuffix('.exe').removesuffix('.cmd')
                native_provider = native_doc.get('provider', {}).get(name, {})
                expected_options = {'baseURL': provider['base_url'], 'apiKey': '{env:' + provider['credential_env'] + '}'}
                if binary != 'opencode' or agent.get('args', []) != ['acp'] or name not in native_proof or native_provider.get('options') != expected_options:
                    return queued(client, 'verified-native-acp-provider-model-binding-required')
                models_out = []
                for row in provider['rows']:
                    native_model = native_provider.get('models', {}).get(row['id'])
                    if native_model != native_proof[name].get('models', {}).get(row['id']) or not isinstance(native_model, dict):
                        return queued(client, 'verified-native-acp-provider-model-binding-required')
                    prefix = name + '/' + row['id']
                    models_out.append({'id': prefix, 'label': label(row)})
                    for effort in efforts(row):
                        if '/' in effort or row['id'] + '/' + effort in native_provider.get('models', {}):
                            return queued(client, 'ambiguous-native-acp-model-variant-id')
                        if effort not in native_model.get('variants', {}):
                            return queued(client, 'native-acp-effort-variant-missing')
                        # OpenCode ACP parses provider/model/variant exactly.
                        # An opaque generic agent's model IDs must never be guessed.
                        models_out.append({'id': prefix + '/' + effort, 'label': label(row) + ' (' + effort + ')'})
                file = state / ('projection-paseo-' + name + '.json')
                plans.append((file, name, {'additionalModels': models_out}))
                owned_json(path, file, ('agents', 'providers', name), plans[-1][2], False)
            if apply and any(owned_json(path, file, ('agents', 'providers', name), value, False)['changed'] for file, name, value in plans) and not native_idle(client):
                return queued(client, 'active-or-unknown-native-runtime-preserved')
            changes = [owned_json(path, file, ('agents', 'providers', name), value, apply) for file, name, value in plans]
            result = {'status': 'applied' if apply else 'planned', 'changed': any(v['changed'] for v in changes)}
        else:
            if version[0] != 0 or version < (0, 162):
                return queued(client, 'unknown-codex-profile-catalog-version')
            if any(p['protocol'] != 'openai-responses' for p in selected.values()):
                return queued(client, 'codex-custom-provider-requires-responses-protocol')
            root = pathlib.Path(os.environ.get('CODEX_HOME', home / '.codex'))
            retire_plans = codex_retirement(root, state, previous - set(selected))
            cache = json.loads(read_client(root / 'models_cache.json') or '{}') if selected else {}
            templates = cache.get('models')
            if selected and (not isinstance(templates, list) or not templates or not isinstance(templates[0], dict) or 'slug' not in templates[0]):
                return queued(client, 'native-codex-model-metadata-template-required')
            plans = []
            for name, provider in selected.items():
                file = state / 'projections/codex' / (name + '.json')
                catalog_receipt = state / ('projection-codex-catalog-' + name + '.json')
                value = codex_catalog(templates[0], provider['rows'])
                profile = root / (name + '.config.toml')
                profile_receipt = state / ('projection-codex-profile-' + name + '.json')
                body = '\n'.join(['model_provider = ' + json.dumps(name), 'model_catalog_json = ' + json.dumps(str(file)),
                        '[model_providers.' + name + ']', 'name = ' + json.dumps(name), 'base_url = ' + json.dumps(provider['base_url']),
                        'wire_api = "responses"', 'requires_openai_auth = false', 'env_key = ' + json.dumps(provider['credential_env'])])
                catalog_plan = owned_json(file, catalog_receipt, (), value, False)
                profile_plan = owned_toml(profile, profile_receipt, body, {'model_providers': [name]}, False, marker='profile')
                if apply and profile.exists() and (catalog_plan['changed'] or profile_plan['changed']) and not native_idle(client):
                    return queued(client, 'active-or-unknown-native-runtime-preserved')
                plans.append((file, catalog_receipt, value, profile, profile_receipt, body, name))
            changes = []
            if apply and (any(before for _, before in retire_plans)) and not native_idle(client):
                return queued(client, 'active-or-unknown-native-runtime-preserved')
            changed_before_error = unlink_owned_files(retire_plans, apply)
            for file, catalog_receipt, value, profile, profile_receipt, body, name in plans:
                changes += [owned_json(file, catalog_receipt, (), value, apply), owned_toml(profile, profile_receipt, body, {'model_providers': [name]}, apply, marker='profile')]
            result = {'status': 'applied' if apply else 'planned', 'changed': changed_before_error or any(v['changed'] for v in changes),
                      'profiles': list(selected), 'defaultSelectionChanged': False}
    except GuardError as error:
        return {**queued(client, str(error)), 'changed': (changed_before_error or getattr(error, 'changed', False)) if apply else False}
    except (ValueError, TypeError, KeyError):
        return {**queued(client, 'native-projection-schema-review-required'), 'changed': changed_before_error if apply else False}
    return {'client': client, **result, 'providers': len(selected), 'models': sum(len(p['rows']) for p in selected.values()),
            'credentialValuesPersisted': 0, 'agentsStarted': 0, 'modelCalls': 0}
