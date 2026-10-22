import copy
import pathlib
import tempfile
import unittest

from modelhub_kit import catalog, core, projection


def feed():
    return {
        'example-plan': {'id': 'example-plan', 'models': {
            'small': {'id': 'small', 'limit': {'context': 65536, 'output': 8192}},
            'large': {'id': 'large', 'contextWindow': 272000, 'maxContextWindow': 872000,
                      'maxInputTokens': 800000, 'maxOutputTokens': 131072,
                      'reasoning_efforts': ['high', 'max', 'provider-depth']},
            'unknown': {'id': 'unknown', 'limit': {'context': 0, 'output': 0}},
        }},
        'other-plan': {'id': 'other-plan', 'models': {
            'unknown': {'id': 'unknown', 'limit': {'context': 1048576}},
        }},
    }


class LimitDiscoveryTests(unittest.TestCase):
    def test_exact_provider_discovery_keeps_heterogeneous_and_unknown_limits(self):
        source = {'provider_id': 'example-plan'}
        value = feed()
        before = copy.deepcopy(value)
        rows = catalog.source_models(value, source)
        self.assertEqual([row['id'] for row in rows], ['small', 'large', 'unknown'])
        self.assertEqual([projection.pi_model(rows[index]).get('contextWindow') for index in (0, 2)],
                         [65536, None])
        # Pi cannot represent an arbitrary provider effort. Keep that source
        # ladder intact and queue its projection rather than hide the effort.
        with self.assertRaises(core.GuardError):
            projection.pi_model(rows[1])
        self.assertEqual(projection.opencode_model(rows[1], 'openai-completions')['limit'],
                         {'context': 872000, 'input': 800000, 'output': 131072})
        self.assertEqual(rows[1]['reasoning_efforts'], ['high', 'max', 'provider-depth'])
        self.assertEqual(catalog.model_budgets(rows[2]), {})
        self.assertEqual(catalog.source_models({'providers': value, 'models': {}}, source), rows)
        self.assertEqual(value, before)
        with self.assertRaises(core.GuardError):
            catalog.models({'models': [{'id': 'generic-invalid', 'limit': {'context': 0}}]})

    def test_provider_and_model_mismatch_cannot_supply_metadata(self):
        for value in ({}, {'example-plan': {'id': 'other', 'models': {}}},
                      {'example-plan': {'models': {'exact': {'id': 'different'}}}}):
            with self.subTest(value=value), self.assertRaises(core.GuardError):
                catalog.source_models(value, {'provider_id': 'example-plan'})

    def test_provider_change_cannot_reuse_another_scope_or_304(self):
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory)
            source = {'id': 'example', 'url': 'https://catalog.example.invalid/api.json',
                      'provider_id': 'example-plan'}
            settings = {'catalog_sources': [source]}
            catalog.sync(settings, state, lambda *_: (feed(), {'ETag': 'old-scope'}))
            saved = (state / 'catalogs/example.json').read_bytes()
            source['provider_id'] = 'other-plan'
            requests = []
            def stale_304(url, headers):
                requests.append(headers)
                return None, {}
            self.assertEqual(catalog.sync(settings, state, stale_304)[0]['status'], 'refresh-failed')
            self.assertNotIn('If-None-Match', requests[0])
            self.assertEqual((state / 'catalogs/example.json').read_bytes(), saved)
            self.assertEqual(catalog.limit_report(settings, state)['status'], 'queued')
            catalog.sync(settings, state, lambda *_: (feed(), {}))
            rows = catalog.limit_report(settings, state)['sources'][0]['models']
            self.assertEqual(rows, [{'id': 'unknown', 'status': 'declared',
                                     'context_window': 1048576, 'active_context_window': 1048576}])

    def test_limits_report_is_metadata_only_and_does_not_change_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            state = pathlib.Path(directory)
            settings = {'catalog_sources': [{'id': 'example', 'url': 'https://catalog.example.invalid/api.json',
                                              'provider_id': 'example-plan'}]}
            catalog.sync(settings, state, lambda *_: (feed(), {}))
            file = state / 'catalogs/example.json'
            before, inode = file.read_bytes(), file.stat().st_ino
            report = catalog.limit_report(settings, state)
            self.assertTrue(report['readOnly'])
            self.assertEqual(report['modelCalls'], 0)
            self.assertFalse(report['capacityProbed'])
            self.assertEqual(report['sources'][0]['models'][2]['status'], 'unknown')
            self.assertEqual(report['sources'][0]['models'][1]['active_context_window'], 872000)
            self.assertTrue(report['sources'][0]['observedAt'])
            self.assertEqual((file.read_bytes(), file.stat().st_ino), (before, inode))

    def test_configuration_validates_explicit_provider_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            file = pathlib.Path(directory) / 'settings.json'
            settings = core.default_config()
            settings['catalog_sources'] = [{'id': 'example', 'url': 'https://catalog.example.invalid/api.json',
                                            'provider_id': 'example-plan'}]
            core.atomic_json(file, settings)
            self.assertEqual(core.config(file)['catalog_sources'][0]['provider_id'], 'example-plan')
            for invalid in (None, False, '', '../other', 'example/other'):
                settings['catalog_sources'][0]['provider_id'] = invalid
                core.atomic_json(file, settings)
                with self.subTest(invalid=invalid), self.assertRaises(core.GuardError):
                    core.config(file)


if __name__ == '__main__':
    unittest.main()
