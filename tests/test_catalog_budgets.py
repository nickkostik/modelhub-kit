import pathlib
import tempfile
import unittest

from modelhub_kit import catalog, core, projection


class CatalogBudgetTests(unittest.TestCase):
    def test_unknown_is_not_another_models_limit(self):
        rows = catalog.models({'models': [
            {'id': 'known', 'context_window': 1000000, 'max_output_tokens': 131072},
            {'id': 'missing'},
            {'id': 'null', 'context_window': None, 'limit': None, 'limits': {'output': None}},
            {'id': 'mixed', 'context_window': None, 'limits': {'context': 131072, 'input': 100000}},
        ]})
        self.assertEqual(catalog.model_budgets(rows[1]), {})
        self.assertEqual(catalog.model_budgets(rows[2]), {})
        self.assertIsNone(rows[2]['context_window'])
        self.assertEqual(catalog.model_budgets(rows[3]), {'context_window': 131072, 'max_input_tokens': 100000})
        self.assertNotIn('max_output_tokens', rows[3])

    def test_invalid_or_conflicting_metadata_fails_before_projection(self):
        bad = [
            {'context_window': True}, {'max_output_tokens': 0}, {'max_input_tokens': -1},
            {'auto_compact_token_limit': '10000'}, {'max_context_window': 1.5},
            {'limit': []}, {'limits': 'unknown'}, {'limit': {'input': False}},
            {'limits': {'context': 0}}, {'limit': {'output': '8192'}},
            {'context_window': 10000, 'limits': {'context': 20000}},
            {'limit': {'output': 8000}, 'limits': {'output': 4000}},
            {'effective_context_window_percent': 0}, {'effective_context_window_percent': 101},
            {'effective_context_window_percent': True},
        ]
        for values in bad:
            row = {'id': 'invalid', **values}
            with self.subTest(values=values):
                with self.assertRaises(core.GuardError):
                    catalog.models({'models': [row]})
                with self.assertRaises(core.GuardError):
                    projection.codex_catalog({}, [row])
                with self.assertRaises(core.GuardError):
                    projection.opencode_model(row, 'openai-responses')

    def test_invalid_new_metadata_retains_last_valid_catalog(self):
        config = {'catalog_sources': [{'id': 'example', 'url': 'https://catalog.example.invalid/models'}]}
        with tempfile.TemporaryDirectory() as temporary:
            state = pathlib.Path(temporary)
            good = {'models': [{'id': 'exact-model', 'limit': {'context': 131072, 'output': 131072},
                                'reasoning_efforts': ['max', 'verified-custom']}]}
            self.assertEqual(catalog.sync(config, state, lambda *_: (good, {'ETag': 'valid'}))[0]['status'], 'ready')
            file = state / 'catalogs/example.json'
            before = file.read_bytes()
            bad = {'models': [{'id': 'exact-model', 'context_window': 10000, 'limits': {'context': 5000}}]}
            result = catalog.sync(config, state, lambda *_: (bad, {}))[0]
            self.assertEqual(result['status'], 'refresh-failed-last-good-retained')
            self.assertTrue(result['retained'])
            self.assertEqual(file.read_bytes(), before)
            self.assertEqual(core.read_json(file)['models'][0]['max_output_tokens'], 131072)


if __name__ == '__main__':
    unittest.main()
