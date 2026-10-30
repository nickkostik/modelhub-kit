import contextlib
import io
import json
import os
import pathlib
import subprocess
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

    def test_externally_managed_codex_skips_daemon_reconciliation(self):
        from modelhub_kit import updater, catalog, cache
        from modelhub_kit.native_helpers import codex_runtime
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            settings = core.default_config()
            settings['tools'] = ['codex', 'pi']
            settings['externally_managed_tools'] = ['codex']
            file, state = home / 'config.json', home / 'state'
            core.atomic_json(file, settings)
            rows = [{'tool': 'codex', 'status': 'queued', 'reason': 'managed-by-external-updater'},
                    {'tool': 'pi', 'status': 'current'}]
            with patch.dict(os.environ, {'HOME': str(home)}, clear=True), \
                    patch.object(sys, 'argv', ['modelhub-kit', 'maintain']), \
                    patch.object(cli, 'locations', return_value=(home, file, state)), \
                    patch.object(cli, 'check_identity', return_value={}), \
                    patch.object(updater, 'update', return_value=rows), \
                    patch.object(catalog, 'sync', return_value=[]), \
                    patch.object(cli, 'integration_pass', return_value=[]), \
                    patch.object(cache, 'enforce', return_value={'status': 'within-budget'}), \
                    patch.object(codex_runtime, 'reconcile_codex_runtime', side_effect=AssertionError('external Codex was probed')):
                result = cli.main()
            self.assertEqual(result['status'], 'queued')
            self.assertNotIn('codexRuntime', result)
            self.assertEqual(result['tools'], rows)

    def test_cli_reports_deferred_apply_with_temporary_failure_exit(self):
        for result, expected in (({'status': 'queued'}, 75), ({'status': 'failed'}, 1),
                                 ({'status': 'completed'}, 0), ({'status': 'planned'}, 0),
                                 ({'status': 'over-budget', 'apply': False}, 0)):
            output, diagnostic = io.StringIO(), io.StringIO()
            with self.subTest(result=result), patch.object(cli, 'main', return_value=result), \
                    patch.object(sys, 'argv', ['modelhub-kit', 'maintain']), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
                with self.assertRaises(SystemExit) as stopped:
                    cli.cli()
                self.assertEqual(stopped.exception.code, expected)
                self.assertEqual(json.loads(output.getvalue()), result)
                self.assertEqual(diagnostic.getvalue(),
                                 'modelhub-kit command=maintain category=operation-failed status=failed\n' if expected == 1 else '')

    def test_cli_failure_summaries_never_copy_exception_or_result_data(self):
        private = 'https://service.example.invalid/?token=fixture-private-value\n/fixture/private-config\n' * 1000
        cases = [({'status': 'failed', 'reason': private}, None, 1, 'operation-failed status=failed'),
                 (None, core.GuardError(private), 78, 'guard-refused status=refused'),
                 (None, OSError(private), 1, 'operation-error status=error')]
        for result, error, expected, summary in cases:
            output, diagnostic = io.StringIO(), io.StringIO()
            with self.subTest(summary=summary), patch.object(cli, 'main', return_value=result, side_effect=error), \
                    patch.object(sys, 'argv', ['modelhub-kit', '--config', 'update', '--expected-boot', 'maintain', 'sync']), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
                with self.assertRaises(SystemExit) as stopped:
                    cli.cli()
            self.assertEqual(stopped.exception.code, expected)
            self.assertEqual(diagnostic.getvalue(), 'modelhub-kit command=sync category=' + summary + '\n')
            self.assertLess(len(diagnostic.getvalue()), 128)
            if result is not None:
                self.assertEqual(json.loads(output.getvalue()), result)
            elif isinstance(error, core.GuardError):
                self.assertEqual(json.loads(output.getvalue()), {'status': 'refused', 'reason': private})
            else:
                self.assertEqual(json.loads(output.getvalue()),
                                 {'status': 'error', 'reason': 'operation-failed-current-settings-retained'})

    def test_cli_writer_busy_remains_quiet_and_preserves_exit75(self):
        output, diagnostic = io.StringIO(), io.StringIO()
        with patch.object(cli, 'main', side_effect=core.GuardError('writer-busy')), \
                patch.object(sys, 'argv', ['modelhub-kit', 'maintain']), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostic):
            with self.assertRaises(SystemExit) as stopped:
                cli.cli()
        self.assertEqual(stopped.exception.code, 75)
        self.assertEqual(json.loads(output.getvalue()), {'status': 'queued', 'reason': 'writer-busy'})
        self.assertEqual(diagnostic.getvalue(), '')

    def test_unavailable_diagnostic_stream_preserves_failure_exit(self):
        diagnostic = io.StringIO()
        diagnostic.close()
        with patch.object(cli, 'main', side_effect=core.GuardError('fixture-refusal')), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(diagnostic):
            with self.assertRaises(SystemExit) as stopped:
                cli.cli()
        self.assertEqual(stopped.exception.code, 78)

    def test_real_cli_failures_remain_visible_with_stdout_discarded(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = pathlib.Path(temporary)
            file = home / 'fixture-private-config.json'
            environment = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'USERPROFILE': str(home),
                           'LOCALAPPDATA': str(home / 'AppData/Local'), 'XDG_CONFIG_HOME': str(home / '.config'),
                           'XDG_STATE_HOME': str(home / '.local/state'), 'MODELHUB_KIT_HOME': str(home / 'state')}
            if 'SystemRoot' in os.environ:
                environment['SystemRoot'] = os.environ['SystemRoot']
            failed = core.default_config()
            failed['tools'] = []
            # Rejected before opening a socket; no live catalog or model calls.
            failed['catalog_sources'] = [{'id': 'example', 'url': 'http://127.0.0.1:1/fixture-private-value'}]
            cases = [('malformed', '{"fixture-private-value":', 1, 'operation-error status=error'),
                     ('refused', json.dumps({'version': 0, 'fixture-private-value': 'sentinel'}), 78,
                      'guard-refused status=refused'),
                     ('failed-result', json.dumps(failed), 1, 'operation-failed status=failed')]
            for name, contents, expected, summary in cases:
                with self.subTest(case=name):
                    core.atomic_text(file, contents)
                    result = subprocess.run([sys.executable, '-B', '-m', 'modelhub_kit', '--config', str(file), 'maintain'],
                                            cwd=pathlib.Path(__file__).resolve().parents[1], env=environment,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=10)
                    self.assertEqual(result.returncode, expected)
                    self.assertEqual(result.stderr, 'modelhub-kit command=maintain category=' + summary + '\n')


if __name__ == '__main__':
    unittest.main()
