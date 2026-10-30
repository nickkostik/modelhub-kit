import json
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit import catalog
from modelhub_kit.core import MetadataHTTPError


class CatalogCooldownTests(unittest.TestCase):
    def test_provider_retry_after_survives_scheduled_runs_without_discarding_last_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            config = {'catalog_sources': [{'id': 'fixture', 'url': 'https://example.invalid/models'}]}
            catalog.sync(config, state, lambda *_: ({'models': [{'id': 'model', 'reasoning_efforts': ['max']}]}, {}))
            before = (state / 'catalogs/fixture.json').read_bytes()
            calls = []

            def limited(*_):
                calls.append(1)
                raise MetadataHTTPError(429, 'temporarily-unavailable', 1, 90)

            with patch('modelhub_kit.catalog.time.time', return_value=1000):
                first = catalog.sync(config, state, limited)[0]
                second = catalog.sync(config, state, limited)[0]
            self.assertEqual(first['status'], 'queued-rate-limit')
            self.assertEqual(second['status'], 'queued-rate-limit')
            self.assertEqual(second['retryAfterSeconds'], 90)
            self.assertEqual(len(calls), 1)
            self.assertEqual((state / 'catalogs/fixture.json').read_bytes(), before)
            with patch('modelhub_kit.catalog.time.time', return_value=1091):
                ready = catalog.sync(config, state, lambda *_: ({'models': [{'id': 'model'}]}, {}))[0]
            self.assertEqual(ready['status'], 'ready')
            self.assertEqual(json.loads((state / 'catalog-cooldowns.json').read_text()), {})

    def test_credential_rotation_does_not_inherit_old_account_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'FIXTURE_TOKEN': 'first-secret'}):
            state = pathlib.Path(tmp)
            config = {'catalog_sources': [{'id': 'fixture', 'url': 'https://example.invalid/models',
                                           'bearer_env': 'FIXTURE_TOKEN'}]}
            with patch('modelhub_kit.catalog.time.time', return_value=1000):
                result = catalog.sync(config, state, lambda *_: (_ for _ in ()).throw(
                    MetadataHTTPError(429, 'temporarily-unavailable', 1, 120)))[0]
                self.assertEqual(result['status'], 'queued-rate-limit')
                os.environ['FIXTURE_TOKEN'] = 'second-secret'
                result = catalog.sync(config, state, lambda *_: ({'models': [{'id': 'model'}]}, {}))[0]
            self.assertEqual(result['status'], 'ready')
            self.assertNotIn('secret', (state / 'catalog-cooldowns.json').read_text())

    def test_permanent_rejections_do_not_create_a_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            config = {'catalog_sources': [{'id': 'fixture', 'url': 'https://example.invalid/models'}]}
            result = catalog.sync(config, state, lambda *_: (_ for _ in ()).throw(
                MetadataHTTPError(429, 'credential-or-permission', 1, 60)))[0]
            self.assertEqual(result['status'], 'refresh-failed')
            self.assertFalse((state / 'catalog-cooldowns.json').exists())


if __name__ == '__main__':
    unittest.main()
