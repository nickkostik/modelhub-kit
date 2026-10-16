import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit import __main__ as cli, core


class CommandTests(unittest.TestCase):
    def test_completion_does_not_hide_nested_failures_or_unfinished_work(self):
        self.assertEqual(cli.completion({'tools': [{'status': 'current'}], 'cache': {'status': 'absent'}}), 'completed')
        self.assertEqual(cli.completion({'status': 'completed', 'catalogs': [{'status': 'refresh-failed-last-good-retained'}]}), 'failed')
        for status in ('queued-active-or-unknown-processes', 'unknown-status', 'newer-or-unordered-installation-preserved', []):
            self.assertEqual(cli.completion({'status': 'completed', 'tools': [{'status': status}]}), 'queued')

    def test_maintain_only_applies_selected_surfaces_and_still_cleans_after_queue(self):
        from modelhub_kit import integration, projection, acp, updater, catalog, cache
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            settings = core.default_config()
            settings['integrations'] = {'codex': {'mcp': True, 'models': False}, 'paseo': {'acp': True}}
            file, state = home / 'config.json', home / 'state'
            core.atomic_json(file, settings)
            calls = []
            def operation(name, result):
                def execute(*args, **kwargs):
                    calls.append(name)
                    if name in ('mcp', 'acp'):
                        self.assertIs(kwargs.get('apply'), True)
                    return result
                return execute
            with patch.dict(os.environ, {'HOME': str(home), 'CODEX_HOME': str(home / '.codex')}, clear=True), \
                    patch.object(sys, 'argv', ['modelhub-kit', 'maintain']), \
                    patch.object(cli, 'locations', return_value=(home, file, state)), \
                    patch.object(cli, 'check_identity', return_value={}), \
                    patch.object(updater, 'update', side_effect=operation('update', [])), \
                    patch.object(catalog, 'sync', side_effect=operation('catalog', [])), \
                    patch.object(integration, 'integrate', side_effect=operation('mcp', {'status': 'queued', 'reason': 'active-client'})), \
                    patch.object(projection, 'project', side_effect=AssertionError('disabled projection ran')), \
                    patch.object(acp, 'register', side_effect=operation('acp', {'status': 'applied'})), \
                    patch.object(cache, 'enforce', side_effect=operation('cache', {'status': 'absent'})):
                result = cli.main()
            self.assertEqual(calls, ['update', 'catalog', 'mcp', 'acp', 'cache'])
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(result['modelCalls'], 0)
            self.assertEqual(json.loads((state / 'last-maintenance.json').read_text())['status'], 'queued')

    def test_cli_reports_deferred_apply_with_temporary_failure_exit(self):
        for result, expected in (({'status': 'queued'}, 75), ({'status': 'failed'}, 1),
                                 ({'status': 'planned'}, 0), ({'status': 'over-budget', 'apply': False}, 0)):
            with self.subTest(result=result), patch.object(cli, 'main', return_value=result), patch('builtins.print'):
                with self.assertRaises(SystemExit) as stopped:
                    cli.cli()
                self.assertEqual(stopped.exception.code, expected)


if __name__ == '__main__':
    unittest.main()
