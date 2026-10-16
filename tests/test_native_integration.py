import hashlib
import json
import pathlib
import os
import tempfile
import tomllib
import unittest
from unittest import mock

from modelhub_kit import acp, catalog, core, integration, projection


class NativeIntegration(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = pathlib.Path(self.directory.name) / 'home'
        self.state = pathlib.Path(self.directory.name) / 'state'
        core.private_directory(self.home)
        environment = mock.patch.dict(os.environ, {
            'PATH': os.environ.get('PATH', ''), 'HOME': str(self.home), 'USERPROFILE': str(self.home),
            'CODEX_HOME': str(self.home / '.codex'), 'CLAUDE_CONFIG_DIR': str(self.home / '.claude'),
            'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_STATE_HOME': str(self.home / '.local/state'),
            'XDG_CACHE_HOME': str(self.home / '.cache'), 'APPDATA': str(self.home / 'AppData/Roaming'),
            'LOCALAPPDATA': str(self.home / 'AppData/Local'), 'PI_CODING_AGENT_DIR': str(self.home / '.pi/agent'),
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        original = core.atomic_text
        def fixture_write(path, value, *args, **kwargs):
            if pathlib.Path(self.directory.name).resolve() not in pathlib.Path(path).absolute().resolve().parents:
                raise AssertionError('test attempted native write outside fixture')
            return original(path, value, *args, **kwargs)
        for module in (core, integration):
            patch = mock.patch.object(module, 'atomic_text', side_effect=fixture_write)
            patch.start()
            self.addCleanup(patch.stop)
        self.native_idle_impl = integration.native_idle
        for module in (integration, projection):
            patch = mock.patch.object(module, 'native_idle', return_value=True)
            patch.start()
            self.addCleanup(patch.stop)


    def source(self, levels=None):
        config = {'catalog_sources': [{'id': 'example', 'url': 'https://catalog.example.invalid/models'}],
                  'model_providers': {'example': {'source': 'example', 'base_url': 'https://api.example.invalid/v1',
                    'credential_env': 'EXAMPLE_ACCESS_TOKEN', 'protocol': 'openai-responses'}},
                  'client_versions': {'opencode': '1.2.0', 'pi': '1.2.0', 'codex': '0.162.1', 'paseo': '1.0.0'}}
        row = {'id': 'demo-model', 'name': 'Demo model', 'context_window': 10000,
               'supported_reasoning_levels': levels if levels is not None else ['low', 'xhigh', 'max', 'custom-verified']}
        fingerprint = hashlib.sha256(json.dumps({'url': config['catalog_sources'][0]['url'], 'bearer_env': None}, sort_keys=True).encode()).hexdigest()
        core.atomic_json(self.state / 'catalogs/example.json', {'version': 1, 'sourceFingerprint': fingerprint, 'models': [row]})
        return config

    def test_devin_dedicated_file_and_muse_actual_legacy_schema(self):
        cfg = {'client_versions': {'devin': '3000.3.2', 'muse': '1.3.0'},
               'mcp_servers': {'example': {'url': 'https://mcp.example.invalid/mcp'}}}
        old = (self.home / 'AppData/Roaming/devin' if os.name == 'nt' else self.home / '.config/devin') / 'config.json'
        core.atomic_json(old, {'agent': {'model': 'chosen'}, 'mcpServers': {'custom': {'command': 'custom'}}})
        before = old.read_bytes()
        integration.integrate(cfg, self.home, self.state, 'devin', True)
        self.assertEqual(json.loads((old.parent / 'mcp_config.json').read_text())['mcpServers']['example']['transport'], 'http')
        self.assertEqual(old.read_bytes(), before)
        integration.integrate(cfg, self.home, self.state, 'muse', True)
        muse = json.loads((self.home / '.config/muse/settings.json').read_text())
        self.assertEqual(muse['mcp_servers']['example']['transport'], 'streamable_http')
        self.assertNotIn('mcpServers', muse)

    def test_unknown_version_and_ambiguous_or_unsupported_muse_auth_never_write(self):
        cfg = {'mcp_servers': {'example': {'url': 'https://mcp.example.invalid/mcp'}}}
        self.assertEqual(integration.integrate(cfg, self.home, self.state, 'devin', True)['status'], 'queued')
        self.assertFalse(self.state.exists())
        cfg['client_versions'] = {'muse': '1.3.0'}
        cfg['mcp_servers']['example']['bearer_env'] = 'EXAMPLE_TOKEN'
        self.assertEqual(integration.integrate(cfg, self.home, self.state, 'muse', True)['status'], 'queued')
        path = self.home / '.config/muse/settings.json'
        core.atomic_json(path, {'mcp_servers': {}, 'mcpServers': {}})
        before = path.read_bytes()
        cfg['mcp_servers']['example'].pop('bearer_env')
        self.assertEqual(integration.integrate(cfg, self.home, self.state, 'muse', True)['status'], 'queued')
        self.assertEqual(path.read_bytes(), before)

    def test_stdio_native_shapes_and_disabled_entry_survive_repeat(self):
        cfg = {'mcp_servers': {'example': {'command': 'example-mcp', 'args': ['--stdio'], 'env_names': ['EXAMPLE_TOKEN']}}}
        integration.integrate(cfg, self.home, self.state, 'opencode', True)
        path = self.home / '.config/opencode/opencode.json'
        doc = json.loads(path.read_text())
        value = doc['mcp']['example']
        self.assertEqual(value['command'], ['example-mcp', '--stdio'])
        self.assertEqual(value['environment'], {'EXAMPLE_TOKEN': '{env:EXAMPLE_TOKEN}'})
        value.update(enabled=False, timeout=12345)
        doc['model'] = 'custom/chosen'
        core.atomic_json(path, doc)
        integration.integrate(cfg, self.home, self.state, 'opencode', True)
        after = json.loads(path.read_text())
        self.assertFalse(after['mcp']['example']['enabled'])
        self.assertEqual(after['mcp']['example']['timeout'], 12345)
        self.assertEqual(after['model'], 'custom/chosen')
        integration.integrate(cfg, self.home, self.state, 'codex', True)
        codex = tomllib.loads((self.home / '.codex/config.toml').read_text())
        self.assertEqual(codex['mcp_servers']['example']['env_vars'], ['EXAMPLE_TOKEN'])

    def test_opencode_all_efforts_custom_variants_and_user_default_survive(self):
        cfg = self.source()
        path = self.home / '.config/opencode/opencode.json'
        core.atomic_json(path, {'model': 'custom/chosen', 'provider': {'custom': {'options': {'custom': True}}}})
        result = projection.project(cfg, self.home, self.state, 'opencode', True)
        self.assertEqual(result['status'], 'applied')
        doc = json.loads(path.read_text())
        value = doc['provider']['kit-example']
        self.assertEqual(value['options']['apiKey'], '{env:EXAMPLE_ACCESS_TOKEN}')
        self.assertEqual(list(value['models']['demo-model']['variants']), ['low', 'xhigh', 'max', 'custom-verified'])
        self.assertEqual(doc['model'], 'custom/chosen')
        self.assertTrue(doc['provider']['custom']['options']['custom'])
        self.assertFalse(projection.project(cfg, self.home, self.state, 'opencode', True)['changed'])
        value['models']['demo-model']['variants']['user-custom'] = {'customOption': True}
        core.atomic_json(path, doc)
        before = path.read_bytes()
        self.assertEqual(projection.project(cfg, self.home, self.state, 'opencode', True)['status'], 'queued')
        self.assertEqual(path.read_bytes(), before)

    def test_codex_optional_profile_preserves_subscription_config_and_auth(self):
        cfg = self.source()
        root = self.home / '.codex'
        core.atomic_text(root / 'config.toml', 'model = "chosen-subscription-model"\n')
        core.atomic_json(root / 'auth.json', {'fixture': 'subscription-state-must-stay-local'})
        core.atomic_json(root / 'models_cache.json', {'models': [{'slug': 'native-model', 'context_window': 20000,
                          'base_instructions': 'fixture instructions', 'supported_reasoning_levels': []}]})
        before = {n: (root / n).read_bytes() for n in ['config.toml', 'auth.json', 'models_cache.json']}
        result = projection.project(cfg, self.home, self.state, 'codex', True)
        self.assertEqual(result['status'], 'applied')
        profile = tomllib.loads((root / 'kit-example.config.toml').read_text())
        self.assertFalse(profile['model_providers']['kit-example']['requires_openai_auth'])
        self.assertEqual(profile['model_providers']['kit-example']['env_key'], 'EXAMPLE_ACCESS_TOKEN')
        model = json.loads(pathlib.Path(profile['model_catalog_json']).read_text())['models'][0]
        self.assertEqual([r['effort'] for r in model['supported_reasoning_levels']], ['low', 'xhigh', 'max', 'custom-verified'])
        self.assertEqual({n: (root / n).read_bytes() for n in before}, before)
        self.assertFalse(projection.project(cfg, self.home, self.state, 'codex', True)['changed'])

    def test_pi_preserves_all_representable_efforts_and_queues_lossy_mapping(self):
        cfg = self.source(['none', 'low', 'xhigh', 'max'])
        self.assertEqual(projection.project(cfg, self.home, self.state, 'pi', True)['status'], 'applied')
        path = self.home / '.pi/agent/models.json'
        provider = json.loads(path.read_text())['providers']['kit-example']
        self.assertEqual(provider['apiKey'], '${EXAMPLE_ACCESS_TOKEN}')
        self.assertEqual(provider['models'][0]['thinkingLevelMap']['off'], 'none')
        self.assertEqual(provider['models'][0]['thinkingLevelMap']['max'], 'max')
        before = path.read_bytes()
        cfg = self.source(['low', 'custom-verified'])
        self.assertEqual(projection.project(cfg, self.home, self.state, 'pi', True)['status'], 'queued')
        self.assertEqual(path.read_bytes(), before)

    def test_acp_consumers_register_commands_without_launch_or_auth_selection(self):
        cfg = {'client_versions': {'paseo': '1.0.0', 't3': '0.1.0'},
               'acp_agents': {'example': {'command': 'example-acp', 'args': ['acp'], 'env_names': ['EXAMPLE_TOKEN']}}}
        with mock.patch.object(acp.shutil, 'which', return_value='/example/example-acp'), mock.patch.object(acp, 't3_idle', return_value=True):
            result = acp.register(cfg, self.home, self.state, 'paseo', True)
            self.assertEqual(result['agentsStarted'], 0)
            native = json.loads((self.home / '.paseo/config.json').read_text())['agents']['providers']['kit-example']
            self.assertEqual(native, {'extends': 'acp', 'label': 'example', 'command': ['example-acp', 'acp']})
            path = self.home / '.t3/userdata/settings.json'
            custom = {'driver': 'customDriver', 'enabled': False, 'config': {'custom': 'preserved'}}
            core.atomic_json(path, {'providerInstances': {'personal-id': custom}, 'theme': 'custom'})
            self.assertEqual(acp.register(cfg, self.home, self.state, 't3', True)['status'], 'applied')
            after = json.loads(path.read_text())
            self.assertEqual(after['providerInstances']['personal-id'], custom)
            self.assertEqual(after['providerInstances']['kit-example']['driver'], 'acpRegistry')
            self.assertNotIn('authMethodId', after['providerInstances']['kit-example']['config'])
        self.assertEqual(acp.register(cfg, self.home, self.state, 'devin', True)['status'], 'queued')

    def test_t3_active_runtime_and_custom_instance_queued_without_mutation(self):
        cfg = {'client_versions': {'t3': '0.1.0'}, 'acp_agents': {'example': {'command': 'example-acp'}}}
        path = self.home / '.t3/userdata/settings.json'
        core.atomic_json(path, {'providerInstances': {}})
        before = path.read_bytes()
        with mock.patch.object(acp.shutil, 'which', return_value='/example/example-acp'), mock.patch.object(acp, 't3_idle', return_value=False):
            self.assertEqual(acp.register(cfg, self.home, self.state, 't3', True)['status'], 'queued')
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.state / 'acp-t3.json').exists())

    def test_paseo_model_discovery_is_additive_and_acp_receipt_stays_valid(self):
        cfg = self.source()
        cfg['acp_agents'] = {'example': {'command': 'opencode', 'args': ['acp']}}
        self.assertEqual(projection.project(cfg, self.home, self.state, 'opencode', True)['status'], 'applied')
        with mock.patch.object(acp.shutil, 'which', return_value='/example/opencode'):
            acp.register(cfg, self.home, self.state, 'paseo', True)
            self.assertEqual(projection.project(cfg, self.home, self.state, 'paseo', True)['status'], 'applied')
            self.assertFalse(acp.register(cfg, self.home, self.state, 'paseo', True)['changed'])
        doc = json.loads((self.home / '.paseo/config.json').read_text())
        provider = doc['agents']['providers']['kit-example']
        self.assertNotIn('models', provider)
        self.assertEqual([e['id'] for e in provider['additionalModels']], ['kit-example/demo-model', 'kit-example/demo-model/low', 'kit-example/demo-model/xhigh', 'kit-example/demo-model/max', 'kit-example/demo-model/custom-verified'])

    def test_empty_routes_without_receipts_do_not_discover_or_write_native_clients(self):
        with mock.patch.object(projection, 'native_version', side_effect=AssertionError('empty config')), mock.patch.object(acp, 'native_version', side_effect=AssertionError('empty config')):
            for client in ('opencode', 'pi', 'codex', 'paseo'):
                self.assertEqual(projection.project({}, self.home, self.state, client, True)['status'], 'no-configured-providers')
            for client in ('paseo', 't3'):
                self.assertEqual(acp.register({}, self.home, self.state, client, True)['status'], 'no-configured-agents')
        self.assertFalse(self.state.exists())

    def test_provider_retirement_visits_removed_one_and_last_without_catalog_refresh(self):
        for client, relative, field in [('opencode', '.config/opencode/opencode.json', 'provider'), ('pi', '.pi/agent/models.json', 'providers')]:
            with self.subTest(client=client):
                cfg = self.source(['low', 'max'])
                cfg['model_providers']['other'] = dict(cfg['model_providers']['example'])
                self.assertEqual(projection.project(cfg, self.home, self.state, client, True)['status'], 'applied')
                path = self.home / relative
                document = json.loads(path.read_text())
                document[field]['personal'] = {'custom': True}
                document['selected'] = 'personal/model'
                core.atomic_json(path, document)
                del cfg['model_providers']['example']
                self.assertTrue(projection.project(cfg, self.home, self.state, client, True)['changed'])
                after = json.loads(path.read_text())
                self.assertNotIn('kit-example', after[field])
                self.assertIn('kit-other', after[field])
                cfg['model_providers'] = {}
                cfg['catalog_sources'] = []
                self.assertEqual(projection.project(cfg, self.home, self.state, client, True)['status'], 'applied')
                after = json.loads(path.read_text())
                self.assertEqual(after[field], {'personal': {'custom': True}})
                self.assertEqual(after['selected'], 'personal/model')

    def test_custom_disabled_referenced_and_active_provider_retirement_remains_owned_and_queued(self):
        cfg = self.source()
        projection.project(cfg, self.home, self.state, 'opencode', True)
        path = self.home / '.config/opencode/opencode.json'
        receipt = self.state / 'projection-opencode.json'
        original, proof = path.read_text(), receipt.read_bytes()
        cfg['model_providers'] = {}
        for custom in ({'timeout': 42}, {'disabled': True}):
            document = json.loads(original)
            document['provider']['kit-example'].update(custom)
            core.atomic_json(path, document)
            before = path.read_bytes()
            self.assertEqual(projection.project(cfg, self.home, self.state, 'opencode', True)['status'], 'queued')
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(receipt.read_bytes(), proof)
        core.atomic_text(path, original)
        with mock.patch.object(integration, 'native_idle', return_value=False):
            self.assertEqual(projection.project(cfg, self.home, self.state, 'opencode', True)['status'], 'queued')
        self.assertEqual(path.read_text(), original)
        document = json.loads(original)
        document['model'] = 'kit-example/demo-model'
        core.atomic_json(path, document)
        self.assertIn('reference-retired', projection.project(cfg, self.home, self.state, 'opencode', True)['reason'])
        self.assertEqual(receipt.read_bytes(), proof)

    def test_codex_retirement_checks_separate_catalog_and_profile_receipts(self):
        cfg = self.source()
        cfg['model_providers']['other'] = dict(cfg['model_providers']['example'])
        root = self.home / '.codex'
        core.atomic_text(root / 'config.toml', 'model = "native-model"\n')
        core.atomic_json(root / 'auth.json', {'fixture': 'unchanged'})
        core.atomic_json(root / 'models_cache.json', {'models': [{'slug': 'native-model'}]})
        protected_files = {name: (root / name).read_bytes() for name in ('config.toml', 'auth.json', 'models_cache.json')}
        projection.project(cfg, self.home, self.state, 'codex', True)
        del cfg['model_providers']['example']
        self.assertEqual(projection.project(cfg, self.home, self.state, 'codex', True)['status'], 'applied')
        self.assertFalse((root / 'kit-example.config.toml').exists())
        self.assertFalse((self.state / 'projections/codex/kit-example.json').exists())
        self.assertTrue((root / 'kit-other.config.toml').exists())
        self.assertEqual({name: (root / name).read_bytes() for name in protected_files}, protected_files)
        cfg['model_providers'] = {}
        (root / 'models_cache.json').unlink()  # Retirement needs no native template.
        self.assertEqual(projection.project(cfg, self.home, self.state, 'codex', True)['status'], 'applied')
        for name in ('example', 'other'):
            self.assertFalse((root / ('kit-' + name + '.config.toml')).exists())
            for kind in ('catalog', 'profile'):
                self.assertFalse((self.state / ('projection-codex-' + kind + '-kit-' + name + '.json')).exists())
        self.assertEqual((root / 'config.toml').read_bytes(), protected_files['config.toml'])
        self.assertEqual((root / 'auth.json').read_bytes(), protected_files['auth.json'])

    def test_codex_custom_catalog_or_profile_and_active_runtime_prevent_partial_retirement(self):
        cfg = self.source()
        root = self.home / '.codex'
        core.atomic_json(root / 'models_cache.json', {'models': [{'slug': 'native-model'}]})
        projection.project(cfg, self.home, self.state, 'codex', True)
        cfg['model_providers'] = {}
        catalog_file = self.state / 'projections/codex/kit-example.json'
        profile = root / 'kit-example.config.toml'
        original_catalog, original_profile = catalog_file.read_text(), profile.read_text()
        for target, custom in ((catalog_file, '{"models":[],"custom":true}\n'), (profile, original_profile + '\n[custom]\nkeep = true\n')):
            core.atomic_text(target, custom)
            self.assertEqual(projection.project(cfg, self.home, self.state, 'codex', True)['status'], 'queued')
            self.assertTrue(catalog_file.exists())
            self.assertTrue(profile.exists())
            core.atomic_text(catalog_file, original_catalog)
            core.atomic_text(profile, original_profile)
        with mock.patch.object(projection, 'native_idle', return_value=False):
            self.assertEqual(projection.project(cfg, self.home, self.state, 'codex', True)['status'], 'queued')
        self.assertEqual(catalog_file.read_text(), original_catalog)
        self.assertEqual(profile.read_text(), original_profile)

    def test_codex_concurrent_retirement_edit_is_preserved_and_partial_progress_is_truthful(self):
        cfg = self.source()
        root = self.home / '.codex'
        core.atomic_json(root / 'models_cache.json', {'models': [{'slug': 'native-model'}]})
        projection.project(cfg, self.home, self.state, 'codex', True)
        cfg['model_providers'] = {}
        profile = root / 'kit-example.config.toml'
        catalog_file = self.state / 'projections/codex/kit-example.json'
        original_unlink = pathlib.Path.unlink
        def race(path, *args, **kwargs):
            if path == profile:
                core.atomic_json(catalog_file, {'custom': 'concurrent edit'})
            return original_unlink(path, *args, **kwargs)
        with mock.patch.object(pathlib.Path, 'unlink', new=race):
            result = projection.project(cfg, self.home, self.state, 'codex', True)
        self.assertEqual(result['status'], 'queued')
        self.assertTrue(result['changed'])
        self.assertFalse(profile.exists())
        self.assertEqual(json.loads(catalog_file.read_text()), {'custom': 'concurrent edit'})
        self.assertTrue((self.state / 'projection-codex-catalog-kit-example.json').exists())

    def test_acp_removes_one_and_last_owned_agent_but_preserves_custom_and_active(self):
        for client, relative, field in [('paseo', '.paseo/config.json', ('agents', 'providers')), ('t3', '.t3/userdata/settings.json', ('providerInstances',))]:
            with self.subTest(client=client), mock.patch.object(acp.shutil, 'which', return_value='/example/agent'), mock.patch.object(acp, 't3_idle', return_value=True):
                cfg = {'client_versions': {'paseo': '1.0.0', 't3': '0.1.0'}, 'acp_agents': {'example': {'command': 'agent'}, 'other': {'command': 'agent'}}}
                path = self.home / relative
                if client == 't3':
                    core.atomic_json(path, {'providerInstances': {}})
                acp.register(cfg, self.home, self.state, client, True)
                del cfg['acp_agents']['example']
                self.assertEqual(acp.register(cfg, self.home, self.state, client, True)['status'], 'applied')
                doc = json.loads(path.read_text())
                entries = doc
                for key in field:
                    entries = entries[key]
                self.assertNotIn('kit-example', entries)
                cfg['acp_agents'] = {}
                original = path.read_text()
                with mock.patch.object(integration, 'native_idle', return_value=False), mock.patch.object(acp, 't3_idle', return_value=False):
                    self.assertEqual(acp.register(cfg, self.home, self.state, client, True)['status'], 'queued')
                self.assertEqual(path.read_text(), original)
                entries['kit-other']['disabled'] = True
                core.atomic_json(path, doc)
                self.assertEqual(acp.register(cfg, self.home, self.state, client, True)['status'], 'queued')
                core.atomic_text(path, original)
                with mock.patch.object(acp.shutil, 'which', side_effect=AssertionError('removed command need not be installed')):
                    self.assertEqual(acp.register(cfg, self.home, self.state, client, True)['status'], 'applied')

    def test_paseo_combined_removal_queues_then_retires_models_before_acp_command(self):
        cfg = self.source()
        cfg['acp_agents'] = {'example': {'command': 'opencode', 'args': ['acp']}}
        projection.project(cfg, self.home, self.state, 'opencode', True)
        with mock.patch.object(acp.shutil, 'which', return_value='/example/opencode'):
            acp.register(cfg, self.home, self.state, 'paseo', True)
        projection.project(cfg, self.home, self.state, 'paseo', True)
        path = self.home / '.paseo/config.json'
        before = path.read_bytes()
        receipt = (self.state / 'acp-paseo.json').read_bytes()
        cfg['model_providers'], cfg['acp_agents'] = {}, {}
        blocked = acp.register(cfg, self.home, self.state, 'paseo', True)
        self.assertEqual(blocked['status'], 'queued')
        self.assertIn('model-projection-first', blocked['reason'])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual((self.state / 'acp-paseo.json').read_bytes(), receipt)
        self.assertEqual(projection.project(cfg, self.home, self.state, 'paseo', True)['status'], 'applied')
        provider = json.loads(path.read_text())['agents']['providers']['kit-example']
        self.assertNotIn('additionalModels', provider)
        self.assertEqual(provider['command'], ['opencode', 'acp'])
        self.assertEqual(acp.register(cfg, self.home, self.state, 'paseo', True)['status'], 'applied')
        self.assertNotIn('kit-example', json.loads(path.read_text())['agents']['providers'])
        self.assertFalse(projection.project(cfg, self.home, self.state, 'paseo', True)['changed'])
        self.assertNotIn('kit-example', json.loads(path.read_text())['agents']['providers'])

    def test_paseo_custom_additional_models_do_not_release_either_receipt(self):
        cfg = self.source()
        cfg['acp_agents'] = {'example': {'command': 'opencode', 'args': ['acp']}}
        projection.project(cfg, self.home, self.state, 'opencode', True)
        with mock.patch.object(acp.shutil, 'which', return_value='/example/opencode'):
            acp.register(cfg, self.home, self.state, 'paseo', True)
        projection.project(cfg, self.home, self.state, 'paseo', True)
        path = self.home / '.paseo/config.json'
        doc = json.loads(path.read_text())
        doc['agents']['providers']['kit-example']['additionalModels'][0]['label'] = 'Custom label'
        core.atomic_json(path, doc)
        receipts = [self.state / name for name in ('acp-paseo.json', 'projection-paseo-kit-example.json')]
        before = {p: p.read_bytes() for p in [path, *receipts]}
        cfg['model_providers'], cfg['acp_agents'] = {}, {}
        self.assertEqual(projection.project(cfg, self.home, self.state, 'paseo', True)['status'], 'queued')
        self.assertEqual(acp.register(cfg, self.home, self.state, 'paseo', True)['status'], 'queued')
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_active_or_unknown_native_clients_defer_only_changed_settings(self):
        cfg = {'mcp_servers': {'example': {'url': 'https://mcp.example.invalid/mcp'}}}
        with mock.patch.object(integration, 'native_idle', return_value=False):
            self.assertEqual(integration.integrate(cfg, self.home, self.state, 'cursor', True)['status'], 'queued')
        self.assertFalse((self.home / '.cursor/mcp.json').exists())
        integration.integrate(cfg, self.home, self.state, 'cursor', True)
        with mock.patch.object(integration, 'native_idle', side_effect=AssertionError('noop must not collect processes')):
            self.assertFalse(integration.integrate(cfg, self.home, self.state, 'cursor', True)['changed'])
        with self.assertRaises(AssertionError):
            core.atomic_text(pathlib.Path(self.directory.name).parent / 'outside-fixture-must-not-exist', 'fixture')

    def test_native_runtime_inventory_recognizes_scoped_package_launchers(self):
        from modelhub_kit import cache
        cases = [('paseo', 'node /example/node_modules/@getpaseo/cli/dist/index.js'),
                 ('codex', 'node /example/node_modules/@openai/codex/bin/codex.js'),
                 ('pi', 'node /example/node_modules/@earendil-works/pi-coding-agent/dist/cli.js')]
        for client, process in cases:
            with self.subTest(client=client), mock.patch.object(cache, 'process_inventory', return_value=[process]):
                self.assertFalse(self.native_idle_impl(client))
        with mock.patch.object(cache, 'process_inventory', return_value=None):
            self.assertFalse(self.native_idle_impl('paseo'))
        with mock.patch.object(cache, 'process_inventory', return_value=['/usr/bin/other-app']):
            self.assertTrue(self.native_idle_impl('paseo'))

    def test_receipt_race_and_removed_customized_entry_preserve_user_state(self):
        cfg = {'mcp_servers': {'example': {'url': 'https://mcp.example.invalid/mcp'}}}
        integration.integrate(cfg, self.home, self.state, 'cursor', True)
        receipt = self.state / 'integration-cursor.json'
        path = self.home / '.cursor/mcp.json'
        before = path.read_bytes()
        original_read, calls = integration.read_client, [0]
        def race(target):
            if pathlib.Path(target) == receipt:
                calls[0] += 1
                if calls[0] == 3:
                    doc = json.loads(receipt.read_text())
                    doc['externalEdit'] = True
                    core.atomic_json(receipt, doc)
            return original_read(target)
        cfg['mcp_servers']['example']['url'] = 'https://new.example.invalid/mcp'
        with mock.patch.object(integration, 'read_client', side_effect=race):
            with self.assertRaises(core.GuardError):
                integration.integrate(cfg, self.home, self.state, 'cursor', True)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(json.loads(receipt.read_text())['externalEdit'])
        doc = json.loads(path.read_text())
        doc['mcpServers']['example']['disabled'] = True
        doc['mcpServers']['example']['timeout'] = 42
        core.atomic_json(path, doc)
        integration.integrate({'mcp_servers': {}}, self.home, self.state, 'cursor', True)
        self.assertEqual(json.loads(path.read_text()), doc)
        self.assertEqual(json.loads(receipt.read_text())['entries'], {})

    def test_rotated_or_missing_source_credential_cannot_project_prior_scope(self):
        cfg = self.source()
        source = cfg['catalog_sources'][0]
        source['bearer_env'] = 'SOURCE_ACCESS_TOKEN'
        record_path = self.state / 'catalogs/example.json'
        with mock.patch.dict(os.environ, {'SOURCE_ACCESS_TOKEN': 'fixture-scope-old'}):
            record = json.loads(record_path.read_text())
            record['sourceFingerprint'] = catalog.source_fingerprint(source)
            core.atomic_json(record_path, record)
            self.assertEqual(projection.project(cfg, self.home, self.state, 'opencode', True)['status'], 'applied')
        path = self.home / '.config/opencode/opencode.json'
        before = path.read_bytes()
        with mock.patch.dict(os.environ, {'SOURCE_ACCESS_TOKEN': 'fixture-scope-new'}):
            self.assertEqual(projection.project(cfg, self.home, self.state, 'opencode', True)['status'], 'queued')
        self.assertEqual(projection.project(cfg, self.home, self.state, 'opencode', True)['status'], 'queued')
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn('fixture-scope-old', record_path.read_text())
        self.assertNotIn('fixture-scope-new', record_path.read_text())

    def test_native_version_uses_checked_static_package_metadata_without_launch(self):
        from modelhub_kit import updater
        package = self.home / 'node_modules/@getpaseo/cli'
        entry = package / 'bin/paseo.js'
        core.atomic_json(package / 'package.json', {'name': '@getpaseo/cli', 'version': '1.0.0', 'bin': {'paseo': 'bin/paseo.js'}})
        core.atomic_text(entry, 'throw new Error("must never launch for version discovery");\n')
        entry.chmod(0o700)
        with mock.patch.object(updater.shutil, 'which', return_value=str(entry)), mock.patch.object(updater, 'run', side_effect=AssertionError('must not launch')):
            self.assertEqual(integration.native_version({}, self.home, self.state, 'paseo'), '1.0.0')
        with mock.patch.object(updater, 'describe', side_effect=AssertionError('explicit reviewed override takes precedence')):
            self.assertEqual(integration.native_version({'client_versions': {'paseo': '1.1.0'}}, self.home, self.state, 'paseo'), '1.1.0')
        self.assertFalse((self.state / 'tools/paseo.json').exists())

    def test_concurrent_native_edit_is_not_overwritten(self):
        cfg = {'mcp_servers': {'example': {'url': 'https://mcp.example.invalid/mcp'}}}
        path = self.home / '.cursor/mcp.json'
        original = integration.atomic_text
        def race(target, value, expected=None):
            core.atomic_text(target, '{"custom":true}\n')
            return original(target, value, expected=expected)
        with mock.patch.object(integration, 'atomic_text', side_effect=race):
            with self.assertRaises(core.GuardError):
                integration.integrate(cfg, self.home, self.state, 'cursor', True)
        self.assertEqual(json.loads(path.read_text()), {'custom': True})
        self.assertFalse((self.state / 'integration-cursor.json').exists())


if __name__ == '__main__':
    unittest.main()
