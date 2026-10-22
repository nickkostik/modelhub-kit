"""Read metadata only. Preserve supplied model IDs and reasoning choices."""
import hashlib
import json
import os
import pathlib
import re
from .core import GuardError, MetadataHTTPError, atomic_json, http_json, now, read_json


def source_fingerprint(source):
    """Bind HTTP validators and projections to an origin and credential scope."""
    name = source.get('bearer_env')
    scope = {'url': source.get('url'), 'bearer_env': name}
    if name:
        credential = os.environ.get(name)
        if not credential:
            raise GuardError('missing-source-credential-reference')
        scope['authScope'] = hashlib.sha256(b'ModelHub-Kit catalog credential scope\0' + credential.encode()).hexdigest()
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def models(value):
    rows = value.get('data', value.get('models')) if isinstance(value, dict) else None
    if not isinstance(rows, list) or len(rows) > 20000:
        raise GuardError('model-array-required')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise GuardError('invalid-model-record')
        model_id = row.get('id', row.get('slug'))
        if not isinstance(model_id, str) or not model_id or len(model_id) > 512 or model_id in seen:
            raise GuardError('unique-exact-model-ids-required')
        seen.add(model_id)
        # Keep capability values intact; never manufacture or cap reasoning levels.
        fields = ('name', 'display_name', 'context_window', 'max_output_tokens', 'input_modalities',
                  'output_modalities', 'reasoning', 'supported_reasoning_levels', 'supportedReasoningEfforts',
                  'default_reasoning_level', 'defaultReasoningEffort', 'capabilities', 'variants', 'limits', 'parameters')
        keep = set(fields) | {key for key in row if re.search(r'reason|effort|think', key, re.I)}
        result.append({'id': model_id, **{key: row[key] for key in row if key in keep}})
    if not result:
        raise GuardError('empty-catalog-not-activated')
    return result


def sync(config, state, fetch=http_json):
    root = pathlib.Path(state) / 'catalogs'
    results = []
    seen = set()
    for source in config.get('catalog_sources', []):
        name = source.get('id', '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name) or name in seen:
            raise GuardError('unique-safe-source-id-required')
        seen.add(name)
        file = root / (name + '.json')
        try:
            prior = read_json(file)
            if prior is not None:
                if not isinstance(prior, dict) or not isinstance(prior.get('validators'), dict):
                    raise ValueError('invalid-cache')
                models({'models': prior['models']})
                if set(prior['validators']) - {'ETag', 'Last-Modified'} or not all(isinstance(v, str) and len(v) <= 4096 and '\r' not in v and '\n' not in v for v in prior['validators'].values()):
                    raise ValueError('invalid-cache-validators')
        except (GuardError, OSError, ValueError, KeyError):
            prior = None
        headers = dict(prior.get('validators', {})) if prior else {}
        headers = {('If-None-Match' if k == 'ETag' else 'If-Modified-Since'): v for k, v in headers.items()}
        environment_name = source.get('bearer_env')
        if environment_name:
            if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,127}', environment_name):
                raise GuardError('credential-environment-name-required')
            credential = os.environ.get(environment_name)
            if not credential:
                results.append({'source': name, 'status': 'missing-credential-reference', 'retained': prior is not None})
                continue
            headers['Authorization'] = 'Bearer ' + credential
        fingerprint = source_fingerprint(source)
        if prior and prior.get('sourceFingerprint') != fingerprint:
            prior = None
            headers = {k: v for k, v in headers.items() if k == 'Authorization'}
        try:
            value, validators = fetch(source['url'], headers)
            if value is None:
                if not prior:
                    raise GuardError('304-without-valid-catalog')
                models({'models': prior['models']})
                results.append({'source': name, 'status': 'not-modified', 'models': len(prior['models']), 'changed': False})
                continue
            rows = models(value)
            changed = prior is None or prior['models'] != rows
            record = {'version': 1, 'sourceFingerprint': fingerprint, 'models': rows, 'validators': validators}
            # No timestamp-only history churn. Keep the exact last good validated model set.
            if prior and record == {k: v for k, v in prior.items() if k != 'observedAt'}:
                written = False
            else:
                record['observedAt'] = now()
                written = atomic_json(file, record)
            results.append({'source': name, 'status': 'ready', 'models': len(rows), 'changed': changed, 'written': written})
        except (GuardError, OSError, ValueError, KeyError) as error:
            result = {'source': name, 'status': 'refresh-failed-last-good-retained' if prior else 'refresh-failed',
                      'retained': prior is not None}
            if isinstance(error, MetadataHTTPError):
                result['diagnostic'] = error.diagnostic()
            results.append(result)
    return results
