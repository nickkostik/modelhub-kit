import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit import core


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('diagnostic_fixture_installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerDiagnosticTests(unittest.TestCase):
    def test_actual_initialization_and_schedule_failures_emit_only_fixed_markers(self):
        cases = [
            ([], 23, 0, 'applied', 'initialize', 'command-failed'),
            (['--schedule'], 0, 29, 'error', 'schedule', 'command-failed'),
            (['--schedule'], 0, 0, 'queued', 'schedule', 'not-ready'),
            (['--prerequisites'], 0, 0, 'applied', 'prerequisite', 'not-ready'),
        ]
        for arguments, init_exit, schedule_exit, schedule_status, stage, category in cases:
            with self.subTest(stage=stage, category=category), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                app = root / 'app'
                app.mkdir()
                (app / 'launch.py').write_text(
                    "import json,sys\n"
                    "sys.stderr.write('LOCAL_CHILD_SECRET_PLACEHOLDER\\n')\n"
                    "if sys.argv[1]=='init':\n"
                    " print('LOCAL_CHILD_SECRET_PLACEHOLDER')\n"
                    " raise SystemExit(" + str(init_exit) + ")\n"
                    "print(json.dumps({'status':" + repr(schedule_status) + "}))\n"
                    "raise SystemExit(" + str(schedule_exit) + ")\n")
                prepared = {'status': 'current', 'application': str(app), 'filesChanged': 0,
                            'node': {'ready': '--prerequisites' not in arguments}}
                output, errors = io.StringIO(), io.StringIO()
                with patch.object(sys, 'argv', ['install.py', '--source', str(ROOT), *arguments]), \
                        patch.object(installer, '_install_files', return_value=prepared), \
                        patch.object(core, 'locations', return_value=(root, root / 'config.json', root / 'state')), \
                        contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    code = installer.main()
                self.assertEqual(code, 1)
                self.assertEqual(errors.getvalue(), 'modelhub-kit-install stage=' + stage + ' category=' + category + '\n')
                self.assertNotIn('LOCAL_CHILD_SECRET_PLACEHOLDER', output.getvalue())
                value = json.loads(output.getvalue())
                self.assertEqual(value['initExit'], init_exit)
                if '--schedule' in arguments:
                    self.assertEqual(value['scheduleExit'], schedule_exit)

    def test_exception_text_is_never_in_diagnostics(self):
        for error in (ValueError('LOCAL_EXCEPTION_SECRET_PLACEHOLDER'),
                      subprocess.TimeoutExpired(['LOCAL_EXCEPTION_SECRET_PLACEHOLDER'], 30)):
            with self.subTest(error=type(error).__name__), \
                    patch.object(sys, 'argv', ['install.py']), \
                    patch.object(installer, 'install', side_effect=error):
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(installer.main(), 1)
                self.assertEqual(output.getvalue(), '')
                self.assertTrue(errors.getvalue().endswith('modelhub-kit-install stage=install category=operation-error\n'))
                self.assertNotIn('LOCAL_EXCEPTION_SECRET_PLACEHOLDER', errors.getvalue())

    def test_success_and_expected_deferred_outputs_are_unchanged(self):
        for value, expected in (({'status': 'current', 'initExit': 0}, 0),
                                ({'status': 'queued', 'reason': 'writer-busy'}, 75),
                                ({'status': 'queued', 'reason': 'active-or-unknown-kit-runtime'}, 75)):
            with self.subTest(status=value), patch.object(sys, 'argv', ['install.py']), \
                    patch.object(installer, 'install', return_value=value):
                output, errors = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                    self.assertEqual(installer.main(), expected)
                self.assertEqual(output.getvalue(), json.dumps(value, indent=2) + '\n')
                self.assertEqual(errors.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
