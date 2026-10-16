import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit import catalog


class CatalogScopeTests(unittest.TestCase):
    def test_rotation_drops_old_validators_but_same_scope_keeps_cache_hits(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            settings = {'catalog_sources': [{'id': 'example', 'url': 'https://catalog.example.invalid/models', 'bearer_env': 'CATALOG_TOKEN'}]}
            requests = []
            def fetch(url, headers):
                requests.append(dict(headers))
                return {'models': [{'id': 'exact-model', 'reasoning_efforts': ['max', 'future-choice']}]}, {'ETag': 'same-validator'}
            with patch.dict(os.environ, {'CATALOG_TOKEN': 'first-fixture-value'}, clear=True):
                catalog.sync(settings, state, fetch)
                def cached(url, headers):
                    requests.append(dict(headers))
                    return None, {}
                self.assertEqual(catalog.sync(settings, state, cached)[0]['status'], 'not-modified')
            self.assertEqual(requests[1].get('If-None-Match'), 'same-validator')
            with patch.dict(os.environ, {'CATALOG_TOKEN': 'second-fixture-value'}, clear=True):
                self.assertEqual(catalog.sync(settings, state, fetch)[0]['status'], 'ready')
            self.assertNotIn('If-None-Match', requests[2])
            saved = (state / 'catalogs/example.json').read_text()
            self.assertNotIn('first-fixture-value', saved)
            self.assertNotIn('second-fixture-value', saved)
            before = saved
            with patch.dict(os.environ, {}, clear=True):
                result = catalog.sync(settings, state, lambda *args: self.fail('missing credential sent a request'))
            self.assertEqual(result[0]['status'], 'missing-credential-reference')
            self.assertEqual((state / 'catalogs/example.json').read_text(), before)


if __name__ == '__main__':
    unittest.main()
