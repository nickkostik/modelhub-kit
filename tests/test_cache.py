import contextlib
import io
import json
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from modelhub_kit import __main__ as cli, cache, core


class CacheTests(unittest.TestCase):
    windows = types.SimpleNamespace(name='nt', getpid=lambda: 500, getppid=lambda: 400)
    unix = types.SimpleNamespace(name='posix', getpid=lambda: 500, getppid=lambda: 400)

    @staticmethod
    def observed(output):
        return {'exit': 0, 'outputTruncated': False, 'timedOut': False, 'output': output}

    def test_windows_singleton_and_array_have_the_same_process_identity(self):
        process = {'ProcessId': 123, 'Name': 'node.exe', 'CommandLine': 'node.exe fixture.js'}
        for rows in (process, [process]):
            with self.subTest(rows=rows), patch.object(cache, 'os', self.windows), \
                    patch.object(cache, 'run', return_value=self.observed(json.dumps(rows))):
                self.assertEqual(cache.process_inventory(), ['node.exe fixture.js'])

    def test_malformed_windows_inventory_is_unknown(self):
        invalid_rows = [None, [], 0, {}, {'ProcessId': 123},
                        {'ProcessId': True, 'Name': 'node.exe', 'CommandLine': 'node.exe fixture.js'},
                        {'ProcessId': -1, 'Name': 'node.exe', 'CommandLine': 'node.exe fixture.js'},
                        {'ProcessId': '123', 'Name': 'node.exe', 'CommandLine': 'node.exe fixture.js'},
                        {'ProcessId': 123, 'Name': None, 'CommandLine': None},
                        {'ProcessId': 123, 'Name': '', 'CommandLine': 'fixture'},
                        {'ProcessId': 123, 'Name': 'node.exe', 'CommandLine': 1},
                        {'ProcessId': 123, 'Name': 'node.exe', 'CommandLine': []}]
        invalid = ['', '{', 'null', 'false', '1', '"process"', '[]']
        invalid.extend(json.dumps([row]) for row in invalid_rows)
        for output in invalid:
            with self.subTest(output=output), patch.object(cache, 'os', self.windows), \
                    patch.object(cache, 'run', return_value=self.observed(output)):
                self.assertIsNone(cache.process_inventory())

    def test_incomplete_probe_is_unknown(self):
        good = self.observed(json.dumps([{'ProcessId': 123, 'Name': 'System', 'CommandLine': None}]))
        invalid = [None, [], {}, dict(good, exit=1), dict(good, exit=True), dict(good, exit='0'),
                   dict(good, timedOut=True), dict(good, outputTruncated=True), dict(good, output=None),
                   {key: value for key, value in good.items() if key != 'timedOut'}]
        for observed in invalid:
            with self.subTest(observed=observed), patch.object(cache, 'os', self.windows), \
                    patch.object(cache, 'run', return_value=observed):
                self.assertIsNone(cache.process_inventory())
        for error in (OSError('fixture-unavailable'), core.GuardError('fixture-probe-refused')):
            with self.subTest(error=type(error).__name__), patch.object(cache, 'os', self.windows), \
                    patch.object(cache, 'run', side_effect=error):
                self.assertIsNone(cache.process_inventory())

    def test_unix_empty_or_partial_inventory_is_unknown_and_owned_pids_are_skipped(self):
        for output in ('', '  ', 'bad process', '123', '123 node fixture.js\npartial'):
            with self.subTest(output=output), patch.object(cache, 'os', self.unix), \
                    patch.object(cache, 'run', return_value=self.observed(output)):
                self.assertIsNone(cache.process_inventory())
        with patch.object(cache, 'os', self.unix), patch.object(cache, 'run', return_value=self.observed(
                '500 kit maintenance\n400 kit parent\n123 node fixture.js\n')):
            self.assertEqual(cache.process_inventory(), ['node fixture.js'])

    def test_unknown_or_active_inventory_preserves_cache_and_unrelated_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = pathlib.Path(temporary) / 'state'
            directory = core.private_directory(state / 'cache/npm')
            active = directory / 'runtime.js'
            active.write_bytes(b'active cache fixture')
            unrelated = pathlib.Path(temporary) / 'unrelated-cache'
            unrelated.write_bytes(b'unrelated fixture')
            outputs = ['{', json.dumps({'ProcessId': 123, 'Name': 'node.exe',
                                        'CommandLine': 'node.exe ' + str(active)}),
                       json.dumps([{'ProcessId': 123, 'Name': 'node.exe', 'CommandLine': None}])]
            for output in outputs:
                with self.subTest(output=output), patch.object(cache, 'os', self.windows), \
                        patch.object(cache, 'run', return_value=self.observed(output)), \
                        patch.object(cache, 'allocated_bytes', return_value=100):
                    result = cache.enforce({'cache_budget_bytes': 1}, state, apply=True)
                    self.assertEqual(result['status'], 'queued-active-or-unknown-runtime')
                    self.assertEqual(result['afterBytes'], 100)
                    self.assertEqual(active.read_bytes(), b'active cache fixture')
                    self.assertEqual(unrelated.read_bytes(), b'unrelated fixture')

    def test_verified_idle_cleanup_removes_only_the_owned_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = pathlib.Path(temporary) / 'state'
            directory = core.private_directory(state / 'cache/npm')
            (directory / 'download').write_bytes(b'disposable fixture')
            unrelated = pathlib.Path(temporary) / 'unrelated-cache'
            unrelated.write_bytes(b'unrelated fixture')
            processes = [{'ProcessId': 500, 'Name': 'python.exe', 'CommandLine': 'kit maintenance'},
                         {'ProcessId': 123, 'Name': 'System', 'CommandLine': None}]
            with patch.object(cache, 'os', self.windows), \
                    patch.object(cache, 'run', return_value=self.observed(json.dumps(processes))), \
                    patch.object(cache, 'allocated_bytes', return_value=100):
                result = cache.enforce({'cache_budget_bytes': 1}, state, apply=True)
            self.assertEqual(result['status'], 'cleaned')
            self.assertEqual(result['afterBytes'], 0)
            self.assertFalse(directory.exists())
            self.assertEqual(unrelated.read_bytes(), b'unrelated fixture')

    def test_maintenance_unknown_inventory_persists_a_queued_receipt_and_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = pathlib.Path(temporary)
            state = home / 'state'
            directory = core.private_directory(state / 'cache/npm')
            active = directory / 'runtime.js'
            active.write_bytes(b'active cache fixture')
            settings = core.default_config()
            operation = 'a' * 32
            output, diagnostic = io.StringIO(), io.StringIO()
            with patch.object(sys, 'argv', ['modelhub-kit', '--operation-id', operation, 'maintain']), \
                    patch.object(cli, 'locations', return_value=(home, home / 'config.json', state)), \
                    patch.object(cli, 'config', return_value=settings), patch.object(cli, 'check_identity', return_value={}), \
                    patch('modelhub_kit.updater.update', return_value=[{'status': 'current'}]) as update, \
                    patch('modelhub_kit.catalog.sync', return_value=[{'status': 'ready'}]) as catalog, \
                    patch.object(cli, 'integration_pass', return_value=[]) as integrate, \
                    patch.object(cache, 'allocated_bytes', return_value=settings['cache_budget_bytes'] + 1), \
                    patch.object(cache, 'os', self.windows), \
                    patch.object(cache, 'run', return_value=self.observed('{')), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
                with self.assertRaises(SystemExit) as stopped:
                    cli.cli()
            self.assertEqual(stopped.exception.code, 75)
            self.assertEqual(diagnostic.getvalue(), '')
            result = json.loads(output.getvalue())
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(result['cache']['status'], 'queued-active-or-unknown-runtime')
            self.assertEqual(result['operationId'], operation)
            self.assertTrue(result['operationTerminal'])
            receipt = core.read_json(state / 'last-maintenance.json')
            self.assertEqual(receipt['format'], 'modelhub-kit-maintenance-v1')
            self.assertEqual(receipt['status'], 'queued')
            self.assertEqual(receipt['tools'], [{'status': 'current'}])
            self.assertEqual(receipt['catalogs'], [{'status': 'ready'}])
            self.assertEqual(receipt['modelCalls'], 0)
            self.assertEqual(active.read_bytes(), b'active cache fixture')
            update.assert_called_once()
            catalog.assert_called_once()
            integrate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
