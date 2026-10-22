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
            {'context_window': 272000, 'contextWindow': 500000},
            {'max_context_window': 872000, 'maxContextWindow': 1000000},
            {'max_output_tokens': 131072, 'maxOutputTokens': 65536},
            {'contextWindow': 272000, 'limits': {'context': 500000}},
            {'maxOutputTokens': 131072, 'limit': {'output': 65536}},
        ]
        bad.extend({alias: value} for alias in catalog.TOKEN_LIMIT_SOURCE_ALIASES.values()
                   for value in (True, 0, -1, 1.5, '10000'))
        for values in bad:
            row = {'id': 'invalid', **values}
            with self.subTest(values=values):
                with self.assertRaises(core.GuardError):
                    catalog.models({'models': [row]})
                with self.assertRaises(core.GuardError):
                    projection.codex_catalog({}, [row])
                with self.assertRaises(core.GuardError):
                    projection.opencode_model(row, 'openai-responses')

    def test_equal_source_aliases_normalize_once_without_changing_source(self):
        source = {'id': 'exact-model', 'context_window': 272000, 'contextWindow': 272000,
                  'max_context_window': 872000, 'maxContextWindow': 872000,
                  'maxOutputTokens': 131072, 'limit': {'context': 272000, 'output': 131072}}
        before = dict(source)
        normalized = catalog.models({'models': [source]})[0]
        self.assertEqual(catalog.model_budgets(normalized), {
            'context_window': 272000, 'max_context_window': 872000, 'max_output_tokens': 131072})
        self.assertEqual(catalog.models({'models': [normalized]}), [normalized])
        self.assertTrue(set(catalog.TOKEN_LIMIT_SOURCE_ALIASES.values()).isdisjoint(normalized))
        self.assertEqual(source, before)

    def test_null_source_aliases_remain_unknown(self):
        source = {'id': 'unknown', 'contextWindow': None, 'maxContextWindow': None,
                  'maxOutputTokens': None}
        self.assertEqual(catalog.model_budgets(source), {})
        normalized = catalog.models({'models': [source]})[0]
        self.assertEqual(catalog.model_budgets(normalized), {})
        self.assertFalse(set(catalog.TOKEN_LIMIT_FIELDS).intersection(normalized))
        known = catalog.models({'models': [dict(source, context_window=272000)]})[0]
        self.assertEqual(catalog.model_budgets(known), {'context_window': 272000})

    def test_invalid_new_metadata_retains_last_valid_catalog(self):
        config = {'catalog_sources': [{'id': 'example', 'url': 'https://catalog.example.invalid/models'}]}
        with tempfile.TemporaryDirectory() as temporary:
            state = pathlib.Path(temporary)
            good = {'models': [{'id': 'exact-model', 'limit': {'context': 131072, 'output': 131072},
                                'reasoning_efforts': ['max', 'verified-custom']}]}
            self.assertEqual(catalog.sync(config, state, lambda *_: (good, {'ETag': 'valid'}))[0]['status'], 'ready')
            file = state / 'catalogs/example.json'
            before = file.read_bytes()
            for bad in (
                {'models': [{'id': 'exact-model', 'context_window': 10000, 'limits': {'context': 5000}}]},
                {'models': [{'id': 'exact-model', 'max_context_window': 872000, 'maxContextWindow': 500000}]},
            ):
                with self.subTest(bad=bad):
                    result = catalog.sync(config, state, lambda *_: (bad, {}))[0]
                    self.assertEqual(result['status'], 'refresh-failed-last-good-retained')
                    self.assertTrue(result['retained'])
                    self.assertEqual(file.read_bytes(), before)
                    self.assertEqual(core.read_json(file)['models'][0]['max_output_tokens'], 131072)


if __name__ == '__main__':
    unittest.main()
