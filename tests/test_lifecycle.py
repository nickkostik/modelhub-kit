import base64
import contextlib
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit import __main__ as cli, core, lifecycle, schedule

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('kit_fixture_installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class LifecycleTests(unittest.TestCase):
    def test_node_prerequisite_mutation_uses_custody_and_macos_queues(self):
        import types
        for system, native_os in (('linux', 'posix'), ('win32', 'nt'), ('darwin', 'posix')):
            with self.subTest(system=system), patch.object(installer, 'os', types.SimpleNamespace(name=native_os, environ={}, geteuid=lambda: 0)), \
                    patch.object(installer.sys, 'platform', system), \
                    patch.object(installer.shutil, 'which', side_effect=lambda name: '/fixture/' + name), \
                    patch.object(installer, '_official_node_lts', return_value={'version': '24.0.0', 'major': 24, 'codename': 'Krypton'}), \
                    patch.object(installer, '_node_version', side_effect=[None, '24.0.0']), \
                    patch.object(installer.subprocess, 'run', side_effect=AssertionError('vendor timeout runner used')), \
                    patch.object(core, 'run_mutation', return_value={'exit': 0, 'treeComplete': True}) as mutate:
                result = installer.node_prerequisite(True)
            self.assertEqual(result['ready'], system != 'darwin')
            self.assertEqual(mutate.call_count, int(system != 'darwin'))

    def test_node_prerequisite_uses_current_official_lts_without_reinstalling_current_node(self):
        lts = {'version': '26.13.0', 'major': 26, 'codename': 'Lithium'}
        with patch.object(installer, '_node_version', return_value='26.13.0'), \
                patch.object(installer, '_official_node_lts') as discover:
            current = installer.node_prerequisite(True)
        self.assertTrue(current['ready'])
        discover.assert_not_called()
        with patch.object(installer, '_node_version', side_effect=[None, '26.13.0']), \
                patch.object(installer, '_official_node_lts', return_value=lts), \
                patch.object(installer.os, 'name', 'posix'), \
                patch.object(installer.sys, 'platform', 'linux'), \
                patch.object(installer.shutil, 'which', side_effect=lambda name: '/fixture/' + name), \
                patch.object(core, 'run_mutation', return_value={'exit': 0, 'treeComplete': True}) as mutate:
            upgraded = installer.node_prerequisite(True)
        self.assertTrue(upgraded['ready'])
        self.assertEqual(upgraded['latestLts'], '26.13.0')
        self.assertIn('nodejs-lts-lithium', mutate.call_args.args[0])

    def test_maintenance_receipt_binds_terminal_queued_operation(self):
        with tempfile.TemporaryDirectory() as folder:
            home, state = Path(folder), Path(folder) / 'state'
            settings = core.default_config(); operation = 'a' * 32
            with patch.object(sys, 'argv', ['modelhub-kit', '--operation-id', operation, 'maintain']), \
                    patch.object(cli, 'locations', return_value=(home, home / 'config.json', state)), \
                    patch.object(cli, 'config', return_value=settings), patch.object(cli, 'check_identity', return_value={}), \
                    patch('modelhub_kit.updater.update', return_value=[{'status': 'queued'}]), \
                    patch('modelhub_kit.catalog.sync', return_value=[]), \
                    patch('modelhub_kit.cache.enforce', return_value={'status': 'absent'}), \
                    patch.object(cli, 'integration_pass', return_value=[]):
                value = cli.main()
            self.assertEqual(value['status'], 'queued')
            self.assertEqual(value['operationId'], operation)
            self.assertTrue(value['operationTerminal'])
            self.assertNotIn('operationId', json.loads((state / 'last-maintenance.json').read_text()))
    def test_schedule_follows_maintenance_without_execution_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            linux = schedule._linux([sys.executable, '-m', 'modelhub_kit', 'maintain'], state)
            text = linux['files'][linux['primary']].decode()
            self.assertIn('TimeoutStartSec=infinity', text)
            self.assertIn('StandardOutput=null\nStandardError=journal\n', text)
            for value in ('CPUQuota=100%', 'MemoryHigh=2G', 'IOSchedulingClass=idle'):
                self.assertIn(value, text)
            windows = schedule._windows(['python.exe', '-m', 'modelhub_kit', 'maintain'], state)
            self.assertIn(', 0, True', windows['files'][windows['primary']].decode('utf-16'))
            self.assertIn('WScript.Quit exitCode', windows['files'][windows['primary']].decode('utf-16'))
            with patch.object(schedule, '_run', return_value=subprocess.CompletedProcess([], 0, b'"User","S-1-5-21-123"\n', b'')):
                payload = schedule._windows_xml(windows)
            self.assertIn('PT0S', payload.decode('utf-16'))

    @unittest.skipUnless(sys.platform.startswith('linux') and shutil.which('systemd-analyze'),
                         'requires the real Linux systemd unit parser')
    def test_systemd_schedule_parses_literal_workdir_and_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            workdir = root / 'space %n $HOME "quoted" \\cache'
            module = workdir / 'modelhub_kit/schedule.py'
            module.parent.mkdir(parents=True)
            module.touch()
            state = root / 'state %n $HOME'
            with patch.object(schedule, '__file__', str(module)):
                plan = schedule._linux([sys.executable, '-m', 'modelhub_kit', 'maintain'], state)
            unit = root / plan['primary'].name
            unit.write_bytes(plan['files'][plan['primary']])
            environment = dict(os.environ, SYSTEMD_UNIT_PATH=str(root) + ':/usr/lib/systemd/system',
                               SYSTEMD_LOG_LEVEL='debug')
            result = subprocess.run(['systemd-analyze', '--generators=no', '--man=no', 'verify', str(unit)],
                                    env=environment, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = result.stdout + result.stderr
            self.assertIn('WorkingDirectory: ' + str(workdir) + '\n', parsed)
            self.assertIn('Environment: MODELHUB_KIT_HOME=' + str(state) + '\n', parsed)
            broken = unit.read_text().replace('WorkingDirectory=' + schedule._systemd_path(str(workdir)),
                                              'WorkingDirectory=' + schedule._systemd_quote(str(workdir)))
            unit.write_text(broken)
            rejected = subprocess.run(['systemd-analyze', '--generators=no', '--man=no', 'verify', str(unit)],
                                      env=environment, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('WorkingDirectory= path is not absolute', rejected.stderr)

    def test_systemd_path_rejects_unrepresentable_directive_values(self):
        for value in ('relative', '/tmp/trailing ', '/tmp/trailing\\', '/tmp/line\nbreak', '/tmp/tab\tpath'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                schedule._systemd_path(value)

    @staticmethod
    def compact_response(*, active=False, unknown=False):
        return {'exit': 0, 'timedOut': False, 'outputTruncated': False,
                'output': json.dumps({'version': 1, 'complete': True, 'processCount': 277,
                                      'skippedCount': 3, 'activeKit': active, 'unknownKit': unknown})}

    @contextlib.contextmanager
    def fixture(self, install=True):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home, source = root / 'home', root / 'source'
            home.mkdir(mode=0o700)
            source.mkdir(mode=0o700)
            for relative in installer.FILES + installer.OPTIONAL_FILES:
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            env = {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'USERPROFILE': str(home),
                   'XDG_CONFIG_HOME': str(home / '.config'), 'XDG_STATE_HOME': str(home / '.local/state'),
                   'XDG_CACHE_HOME': str(home / '.cache'), 'LOCALAPPDATA': str(home / 'AppData/Local'),
                   'APPDATA': str(home / 'AppData/Roaming'), 'CODEX_HOME': str(home / '.codex'),
                   'CLAUDE_CONFIG_DIR': str(home / '.claude'), 'PI_CODING_AGENT_DIR': str(home / '.pi/agent'),
                   'PYTHONDONTWRITEBYTECODE': '1'}
            native_write = installer.atomic_write
            native_unlink, native_rmdir = Path.unlink, Path.rmdir
            def inside(path):
                self.assertIn(root, Path(path).absolute().parents, 'fixture escaped its owned temporary root')
            def write(path, *args, **kwargs):
                inside(path)
                return native_write(path, *args, **kwargs)
            def unlink(path, *args, **kwargs):
                inside(path)
                return native_unlink(path, *args, **kwargs)
            def rmdir(path, *args, **kwargs):
                inside(path)
                return native_rmdir(path, *args, **kwargs)
            def compact_child(argv, **kwargs):
                self.assertEqual(argv[:4], ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand'])
                self.assertEqual(kwargs, {'timeout': 15})
                # Exercise the production parser without querying this machine.
                base64.b64decode(argv[4]).decode('utf-16le')
                return self.compact_response()
            def installed_child(argv, **kwargs):
                expected_app, _ = lifecycle.installation_paths(home)
                self.assertEqual(argv[1:], ['-B', str(expected_app / 'launch.py'), 'init'])
                # Run the real init logic under the fixture environment, without
                # a child identity query or any host process/application call.
                inside(core.locations()[1])
                with patch.object(sys, 'argv', ['modelhub-kit', 'init']):
                    value = cli.main()
                return subprocess.CompletedProcess(argv, 0, json.dumps(value).encode(), b'')
            old_path = sys.path[:]
            try:
                with patch.dict(os.environ, env, clear=True), \
                        patch.object(installer, 'node_prerequisite', return_value={'ready': False, 'changed': False}), \
                        patch.object(lifecycle.cache, 'process_inventory', return_value=[]), \
                        patch.object(lifecycle, 'run', side_effect=compact_child), \
                        patch.object(core, 'run', side_effect=AssertionError('unexpected native process from fixture')), \
                        patch.object(installer.subprocess, 'run', side_effect=installed_child), \
                        patch.object(cli, 'check_identity', return_value={}), \
                        patch.object(schedule, '_run', side_effect=AssertionError('unexpected native scheduler mutation')), \
                        patch.object(schedule, '_native', side_effect=AssertionError('unexpected native scheduler query')), \
                        patch.object(installer, 'atomic_write', side_effect=write), \
                        patch.object(Path, 'unlink', unlink), patch.object(Path, 'rmdir', rmdir):
                    if install:
                        result = installer.install(source)
                        self.assertEqual(result['status'], 'installed')
                    _, config, state = core.locations()
                    app, command = lifecycle.installation_paths(home)
                    yield home, source, app, command, config, state
            finally:
                sys.path[:] = old_path

    def test_fixture_windows_dispatch_uses_closed_summaries_without_real_query(self):
        with self.fixture() as (home, source, app, command, config, state):
            with patch.object(lifecycle.os, 'name', 'nt'):
                self.assertTrue(lifecycle._idle(app, command))
            self.assertEqual(lifecycle.run.call_count, 1)
            lifecycle.cache.process_inventory.assert_not_called()

    def test_windows_compact_idle_summary_is_safely_encoded_and_closed(self):
        app, command = r"C:\Example\quote' space\kit\app", r'C:\Example\kit\modelhub-kit.cmd'
        summary = {'version': 1, 'complete': True, 'processCount': 277, 'skippedCount': 3,
                   'activeKit': False, 'unknownKit': False}
        response = {'exit': 0, 'timedOut': False, 'outputTruncated': False, 'output': json.dumps(summary)}
        with patch.object(lifecycle, 'run', return_value=response) as run:
            self.assertTrue(lifecycle._windows_kit_idle(app, command))
        argv = run.call_args.args[0]
        self.assertEqual(run.call_args.kwargs['timeout'], 15)
        self.assertEqual(argv[:4], ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand'])
        self.assertNotIn(app, subprocess.list2cmdline(argv))
        self.assertLess(len(subprocess.list2cmdline(argv)), 32767)
        script = base64.b64decode(argv[4]).decode('utf-16le')
        payload = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script)[1]
        parameters = json.loads(base64.b64decode(payload))
        self.assertEqual(parameters, {'app': app, 'command': command, 'caller': os.getpid(), 'parent': os.getppid()})
        self.assertIn('Get-CimInstance Win32_Process -ErrorAction Stop', script)
        self.assertNotIn('Select-Object', script)

    def test_windows_compact_inventory_rejects_incomplete_or_ambiguous_results(self):
        summary = {'version': 1, 'complete': True, 'processCount': 277, 'skippedCount': 3,
                   'activeKit': False, 'unknownKit': False}
        response = {'exit': 0, 'timedOut': False, 'outputTruncated': False, 'output': json.dumps(summary)}
        invalid = [dict(response, exit=1), dict(response, exit=False), dict(response, timedOut=True), dict(response, outputTruncated=True),
                   dict(response, output='invalid'), dict(response, output=' ' * 2049), {}, None]
        for overrides in ({'complete': False}, {'complete': 1}, {'version': True}, {'processCount': True},
                          {'processCount': -1}, {'skippedCount': 278}, {'skippedCount': 1}, {'activeKit': 0},
                          {'unknownKit': 'false'}, {'activeKit': True}, {'unknownKit': True},
                          {'CommandLine': 'must not be accepted'}):
            invalid.append(dict(response, output=json.dumps(dict(summary, **overrides))))
        for result in invalid:
            with self.subTest(result=result), patch.object(lifecycle, 'run', return_value=result):
                self.assertFalse(lifecycle._windows_kit_idle('fixture-app', 'fixture-command'))
        with patch.object(lifecycle, 'run', side_effect=core.GuardError('maintenance-child-error')):
            self.assertFalse(lifecycle._windows_kit_idle('fixture-app', 'fixture-command'))

    def test_windows_compact_command_budget_and_platform_boundary(self):
        with patch.object(lifecycle, 'run', side_effect=AssertionError('oversized command was launched')):
            self.assertFalse(lifecycle._windows_kit_idle('x' * 16000, 'fixture-command'))
        with patch.object(lifecycle.os, 'name', 'nt'), \
                patch.object(lifecycle, '_windows_kit_idle', return_value=True) as compact, \
                patch.object(lifecycle.cache, 'process_inventory', side_effect=AssertionError('general Windows inventory used')):
            self.assertTrue(lifecycle._idle('fixture-app', 'fixture-command'))
            compact.assert_called_once_with('fixture-app', 'fixture-command')
        with patch.object(lifecycle.os, 'name', 'posix'), \
                patch.object(lifecycle, '_windows_kit_idle', side_effect=AssertionError('POSIX invoked Windows')), \
                patch.object(lifecycle.cache, 'process_inventory', return_value=['unrelated process']):
            self.assertTrue(lifecycle._idle('fixture-app', 'fixture-command'))

    @unittest.skipUnless(os.name == 'nt', 'Native PowerShell script with owned synthetic CIM records')
    def test_windows_compact_actual_script_handles_large_fake_inventory_and_unknowns(self):
        # Execute the real collector, replacing only CIM with a fixture producer.
        # The producer streams >110 KiB of unrelated arguments; none may be emitted.
        native_run = core.run
        for mode, expected in [('idle', True), ('active', False), ('active-path', False), ('node', False),
                               ('python', False), ('bare-python', False), ('error', False)]:
            with self.subTest(mode=mode):
                def execute(argv, **kwargs):
                    script = base64.b64decode(argv[4]).decode('utf-16le')
                    fixture = r"""
function Get-CimInstance {
  [CmdletBinding()]param([string]$ClassName)
  [pscustomobject]@{ProcessId=$PID;Name='powershell.exe';CommandLine='modelhub-kit query'}
  [pscustomobject]@{ProcessId=[long]$probeParameters.caller;Name='python.exe';CommandLine='modelhub-kit caller'}
  [pscustomobject]@{ProcessId=[long]$probeParameters.parent;Name='python.exe';CommandLine=$null}
  for($fixtureIndex=0;$fixtureIndex -lt 277;$fixtureIndex++) {
    [pscustomobject]@{ProcessId=[long](4000000000+$fixtureIndex);Name='unrelated.exe';CommandLine=('x'*400)}
  }
  if('__MODE__' -eq 'active') {[pscustomobject]@{ProcessId=[long]4000000300;Name='python.exe';CommandLine='python.exe -m MODELHUB_KIT maintain'}}
  if('__MODE__' -eq 'active-path') {[pscustomobject]@{ProcessId=[long]4000000300;Name='python.exe';CommandLine='C:\EXAMPLE\KIT\APP\launch.py'}}
  if('__MODE__' -eq 'node') {[pscustomobject]@{ProcessId=[long]4000000300;Name='NODE.EXE';CommandLine=$null}}
  if('__MODE__' -eq 'python') {[pscustomobject]@{ProcessId=[long]4000000300;Name='pythonw.exe';CommandLine=$null}}
  if('__MODE__' -eq 'bare-python') {[pscustomobject]@{ProcessId=[long]4000000300;Name='python.exe';CommandLine='python.exe'}}
  if('__MODE__' -eq 'error') {throw 'owned-fixture-enumeration-error'}
}
""".replace('__MODE__', mode)
                    code = base64.b64encode((fixture + script).encode('utf-16le')).decode('ascii')
                    result = native_run([*argv[:4], code], **kwargs)
                    self.assertFalse(result['outputTruncated'])
                    self.assertLess(len(result['output']), 2048)
                    return result
                with patch.object(lifecycle, 'run', side_effect=execute):
                    self.assertEqual(lifecycle._windows_kit_idle(r'C:\Example\Kit\app', r'C:\Example\Kit\command.cmd'), expected)

    def test_windows_helper_children_receive_hidden_flags_and_closed_stdin(self):
        # Do not start Windows tools or monkeypatch pathlib's platform selection.
        flag = 0x08000000
        with patch.object(installer.os, 'name', 'nt'), \
                patch.object(installer.subprocess, 'CREATE_NO_WINDOW', flag, create=True):
            self.assertEqual(installer._quiet_child_options(), {'creationflags': flag})
        with patch.object(installer.os, 'name', 'posix'):
            self.assertEqual(installer._quiet_child_options(), {})
        with patch.object(installer, '_quiet_child_options', return_value={'creationflags': flag}), \
                patch.object(installer.subprocess, 'check_output', return_value=b'v22.0.0\n') as probe:
            self.assertEqual(installer._node_version('fixture-node'), '22.0.0')
        self.assertEqual(probe.call_args.kwargs['creationflags'], flag)
        self.assertIs(probe.call_args.kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(probe.call_args.kwargs['timeout'], 5)
        with self.fixture() as (home, source, app, command, config, state):
            with patch.object(installer, '_quiet_child_options', return_value={'creationflags': flag}), \
                    patch.object(installer.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        [], 0, b'{"status":"current"}', b'')) as child:
                result = installer.install(source, schedule=True)
            self.assertTrue(result['scheduleConfigured'])
            self.assertEqual(child.call_count, 2)
            self.assertEqual([c.kwargs['timeout'] for c in child.call_args_list], [30, 60])
            for call in child.call_args_list:
                self.assertEqual(call.kwargs['creationflags'], flag)
                self.assertIs(call.kwargs['stdin'], subprocess.DEVNULL)

    @unittest.skipIf(os.name == 'nt', 'POSIX umask and directory mode regression')
    def test_group_writable_umask_creates_private_intermediates_without_global_change(self):
        previous = os.umask(0o002)
        try:
            with self.fixture() as (home, source, app, command, config, state):
                for directory in (home / '.local', home / '.local/share', app.parent, app,
                                  app / 'modelhub_kit', app / 'modelhub_kit/native_helpers',
                                  command.parent):
                    self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                # Production installation did not silently replace the caller's umask.
                current = os.umask(0o002)
                self.assertEqual(current, 0o002)
                self.assertEqual(installer.install(source)['filesChanged'], 0)
        finally:
            os.umask(previous)

    @unittest.skipIf(os.name == 'nt', 'POSIX shared-writable directory regression')
    def test_unsafe_partial_install_is_queued_without_adoption_or_chmod(self):
        with self.fixture(install=False) as (home, source, app, command, config, state):
            app.mkdir(parents=True, mode=0o775)
            app.chmod(0o775)
            partial = app / 'custom-existing-file.txt'
            partial.write_text('preserve partial or custom data')
            before = (app.stat().st_ino, app.stat().st_mode, partial.read_bytes())
            result = installer.install(source)
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(result['reason'], 'unsafe-installation-directory-preserved')
            self.assertEqual(result['filesChanged'], 0)
            self.assertEqual((app.stat().st_ino, app.stat().st_mode, partial.read_bytes()), before)
            self.assertEqual(list(app.iterdir()), [partial])
            self.assertFalse(command.exists())
            self.assertFalse((app / '.modelhub-kit-install.json').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX nested directory permissions')
    def test_nested_unsafe_owned_parent_is_preflighted_before_any_code_write(self):
        for relative in ('docs', 'modelhub_kit/native_helpers'):
            with self.subTest(parent=relative), self.fixture() as (home, source, app, command, config, state):
                candidate = source / 'modelhub_kit/__init__.py'
                candidate.write_text(candidate.read_text() + '\n# reviewed fixture update\n')
                nested = app / relative
                nested.chmod(0o775)
                before = {p: p.read_bytes() for p in app.rglob('*') if p.is_file()}
                before[command] = command.read_bytes()
                with patch.object(installer, 'atomic_write', side_effect=AssertionError('preflight wrote code')):
                    for dry_run in (True, False):
                        result = installer.install(source, dry_run=dry_run)
                        self.assertEqual(result['status'], 'queued')
                        self.assertEqual(result['reason'], 'unsafe-installation-directory-preserved')
                        self.assertEqual(result['filesChanged'], 0)
                self.assertEqual(nested.stat().st_mode & 0o777, 0o775)
                self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))

    @unittest.skipIf(os.name == 'nt', 'POSIX concurrent directory mode change')
    def test_late_unsafe_directory_reports_real_partial_changes_without_init(self):
        with self.fixture() as (home, source, app, command, config, state):
            for name in ('modelhub_kit/__init__.py', 'docs/INSTALL.md'):
                path = source / name
                path.write_text(path.read_text() + '\n# reviewed fixture update\n')
            receipt = (app / '.modelhub-kit-install.json').read_bytes()
            checked_write = installer.atomic_write
            def race(path, *args, **kwargs):
                changed = checked_write(path, *args, **kwargs)
                if path == app / 'modelhub_kit/__init__.py':
                    (app / 'docs').chmod(0o775)
                return changed
            with patch.object(installer, 'atomic_write', side_effect=race), \
                    patch.object(installer.subprocess, 'run', side_effect=AssertionError('initialized partial code')):
                result = installer.install(source)
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(result['reason'], 'unsafe-installation-directory-preserved')
            self.assertEqual(result['filesChanged'], 1)
            self.assertEqual((app / '.modelhub-kit-install.json').read_bytes(), receipt)
            self.assertEqual((app / 'docs').stat().st_mode & 0o777, 0o775)

    def test_installer_allowlist_and_uninstaller_agree(self):
        shell = (ROOT / 'scripts/install.sh').read_text()
        darwin = shell.split('if [ "$(uname -s)" = Darwin ]; then', 1)[1].split('\n    else', 1)[0]
        self.assertIn('python-prerequisite-custody-unavailable', darwin)
        self.assertIn('https://www.python.org/downloads/', darwin)
        self.assertIn('exit 75', darwin)
        self.assertNotIn('brew install', darwin)
        self.assertEqual(set(installer.FILES + installer.OPTIONAL_FILES + ('launch.py',)),
                         set(lifecycle.APPLICATION_FILES))

    def test_plan_repeat_and_removal_keep_config_vendor_payloads_and_state(self):
        with self.fixture() as (home, source, app, command, config, state):
            config.write_text('a custom config that is deliberately not JSON')
            core.atomic_text(state / 'npm/vendor-package.txt', 'keep installed vendor tool')
            core.atomic_text(state / 'tools/vendor.json', 'keep updater ownership')
            core.atomic_text(state / 'last-maintenance.json', 'keep old or custom receipt')
            before = config.read_bytes()
            with patch.object(lifecycle, '_idle', side_effect=AssertionError('current install scanned processes')):
                result = installer.install(source)
            self.assertEqual(result['filesChanged'], 0)
            plan = lifecycle.uninstall(home, config, state)
            self.assertEqual(plan['status'], 'planned')
            self.assertTrue(app.is_dir())
            result = lifecycle.uninstall(home, config, state, apply=True)
            self.assertEqual(result['status'], 'removed')
            self.assertFalse(app.exists())
            self.assertFalse(command.exists())
            self.assertEqual(config.read_bytes(), before)
            self.assertEqual((state / 'npm/vendor-package.txt').read_text(), 'keep installed vendor tool')
            self.assertTrue((state / 'tools/vendor.json').is_file())
            self.assertEqual((state / 'last-maintenance.json').read_text(), 'keep old or custom receipt')
            self.assertTrue((state / 'writer.lock').exists())
            self.assertEqual(lifecycle.uninstall(home, config, state, apply=True)['status'], 'absent')

    def test_upgrade_queues_active_or_unknown_readers_then_updates_once(self):
        with self.fixture() as (home, source, app, command, config, state):
            file = source / 'modelhub_kit/__init__.py'
            file.write_text(file.read_text() + '\n# reviewed fixture upgrade\n')
            before = (app / 'modelhub_kit/__init__.py').read_bytes()
            for inventory in (None, ['python ' + str(app / 'launch.py')]):
                with patch.object(lifecycle.cache, 'process_inventory', return_value=inventory), \
                        patch.object(lifecycle, 'run', return_value=self.compact_response(
                            active=inventory is not None and inventory != ['python.exe'],
                            unknown=inventory is None or inventory == ['python.exe'])):
                    result = installer.install(source)
                self.assertEqual(result['status'], 'queued')
                self.assertEqual(result['filesChanged'], 0)
                self.assertEqual((app / 'modelhub_kit/__init__.py').read_bytes(), before)
            self.assertEqual(installer.install(source)['filesChanged'], 1)
            self.assertEqual(installer.install(source)['filesChanged'], 0)

    def test_reviewed_source_can_remove_an_older_receipt_without_lifecycle_module(self):
        with self.fixture() as (home, source, app, command, config, state):
            # Older installers used the same receipt format before shipping this module.
            (app / 'modelhub_kit/lifecycle.py').unlink()
            receipt = app / '.modelhub-kit-install.json'
            value = json.loads(receipt.read_text())
            del value['files']['modelhub_kit/lifecycle.py']
            receipt.write_text(json.dumps(value, indent=2) + '\n')
            result = lifecycle.uninstall(home, config, state, apply=True)
            self.assertEqual(result['status'], 'removed')
            self.assertFalse(app.exists())
            self.assertTrue(config.exists())

    def test_modified_or_unknown_application_data_stops_before_any_unlink(self):
        for change in ('modified', 'unknown', 'bytecode', 'launcher', 'forged-receipt', 'receipt-extension'):
            with self.subTest(change=change), self.fixture() as (home, source, app, command, config, state):
                if change == 'modified':
                    (app / 'README.md').write_text('user customization')
                elif change == 'unknown':
                    (app / 'custom-notes.txt').write_text('unique data')
                elif change == 'bytecode':
                    (app / 'modelhub_kit/__pycache__').mkdir()
                elif change == 'launcher':
                    command.write_text('custom command')
                else:
                    path = app / '.modelhub-kit-install.json'
                    value = json.loads(path.read_text())
                    if change == 'receipt-extension':
                        value['userNotes'] = 'preserve custom receipt data'
                    else:
                        value['files']['../user-data.txt'] = hashlib.sha256(b'private data').hexdigest()
                    path.write_text(json.dumps(value, indent=2) + '\n')
                result = lifecycle.uninstall(home, config, state, apply=True)
                self.assertEqual(result['status'], 'queued')
                self.assertEqual(result['filesRemoved'], 0)
                self.assertTrue(command.exists())
                self.assertTrue((app / '.modelhub-kit-install.json').is_file())

    def test_symlink_and_hardlink_never_grant_removal_authority(self):
        for kind in ('symlink', 'hardlink', 'application-directory-symlink'):
            with self.subTest(kind=kind), self.fixture() as (home, source, app, command, config, state):
                outside = home / 'unique-data.txt'
                outside.write_text('preserve')
                if kind == 'application-directory-symlink':
                    saved = app.with_name('custom-app')
                    app.rename(saved)
                    app.symlink_to(saved, target_is_directory=True)
                else:
                    path = app / 'README.md'
                    path.unlink()
                    if kind == 'symlink':
                        path.symlink_to(outside)
                    else:
                        os.link(outside, path)
                result = lifecycle.uninstall(home, config, state, apply=True)
                self.assertEqual(result['status'], 'queued')
                self.assertEqual(result['filesRemoved'], 0)
                self.assertEqual(outside.read_text(), 'preserve')
                self.assertTrue(command.exists())

    def test_writer_and_kit_reader_guards_preserve_application(self):
        with self.fixture() as (home, source, app, command, config, state):
            with core.writer(state):
                result = lifecycle.uninstall(home, config, state, apply=True)
            self.assertEqual(result['reason'], 'writer-busy')
            for inventory in (None, ['python ' + str(app / 'launch.py')], ['python.exe'], ['modelhub-kit maintain']):
                with patch.object(lifecycle.cache, 'process_inventory', return_value=inventory), \
                        patch.object(lifecycle, 'run', return_value=self.compact_response(
                            active=inventory is not None and inventory != ['python.exe'],
                            unknown=inventory is None or inventory == ['python.exe'])):
                    result = lifecycle.uninstall(home, config, state, apply=True)
                self.assertEqual(result['reason'], 'active-or-unknown-kit-runtime')
                self.assertEqual(result['filesRemoved'], 0)
            self.assertTrue(command.exists())

    def test_schedule_running_or_custom_deferral_prevents_code_removal(self):
        with self.fixture() as (home, source, app, command, config, state):
            core.atomic_json(state / 'schedule/ownership.json', {'marker': schedule.MARKER})
            for status in ('custom-preserved', 'queued', 'unavailable'):
                with patch.object(schedule, 'configure', return_value={'status': status}):
                    result = lifecycle.uninstall(home, config, state, apply=True)
                self.assertEqual(result['status'], 'queued')
                self.assertEqual(result['filesRemoved'], 0)
                self.assertTrue(command.exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux user systemd receipt fixture')
    def test_owned_idle_schedule_is_removed_using_existing_native_receipt_fence(self):
        with self.fixture() as (home, source, app, command, config, state):
            native = {'available': True, 'exists': True, 'running': False, 'fingerprint': 'fixture-native'}
            with patch.object(schedule, '_native', side_effect=[{'available': True, 'exists': False}, native]), \
                    patch.object(schedule, '_run', return_value=subprocess.CompletedProcess([], 0, b'', b'')):
                installed = schedule.configure([sys.executable, '-B', str(app / 'launch.py'), '--config', str(config), 'maintain'], state, apply=True)
            self.assertEqual(installed['status'], 'applied')
            with patch.object(schedule, '_native', return_value=dict(native, running=True)):
                result = lifecycle.uninstall(home, config, state, apply=True)
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(result['filesRemoved'], 0)
            with patch.object(schedule, '_native', return_value=native), \
                    patch.object(schedule, '_run', return_value=subprocess.CompletedProcess([], 0, b'', b'')) as run:
                result = lifecycle.uninstall(home, config, state, apply=True)
            self.assertEqual(result['status'], 'removed')
            self.assertEqual(result['schedule']['status'], 'removed')
            self.assertFalse((state / 'schedule/ownership.json').exists())
            self.assertTrue(any('disable' in call.args[0] for call in run.call_args_list))
            self.assertFalse(any('kill' in call.args[0] for call in run.call_args_list))

    def test_fresh_custom_change_after_schedule_step_is_preserved(self):
        with self.fixture() as (home, source, app, command, config, state):
            def schedule_step(*args):
                if args[-1]:
                    (app / 'README.md').write_text('concurrent user edit')
                return {'status': 'absent'}
            with patch.object(lifecycle, '_schedule', side_effect=schedule_step):
                result = lifecycle.uninstall(home, config, state, apply=True)
            self.assertEqual(result['status'], 'queued')
            self.assertEqual(result['filesRemoved'], 0)
            self.assertEqual((app / 'README.md').read_text(), 'concurrent user edit')
            self.assertTrue(command.exists())

    def test_interrupted_unlinks_keep_recovery_receipt_and_report_actual_progress(self):
        with self.fixture() as (home, source, app, command, config, state):
            native_unlink = Path.unlink
            def interrupted(path, *args, **kwargs):
                if path == app / 'modelhub_kit/core.py':
                    raise PermissionError('fixture interruption')
                return native_unlink(path, *args, **kwargs)
            with patch.object(Path, 'unlink', interrupted):
                result = lifecycle.uninstall(home, config, state, apply=True)
            self.assertEqual(result['status'], 'queued')
            self.assertGreater(result['filesRemoved'], 0)
            self.assertTrue((app / '.modelhub-kit-install.json').is_file())
            self.assertTrue(config.exists())
            self.assertEqual(lifecycle.uninstall(home, config, state, apply=True)['status'], 'removed')

    def test_opt_in_state_cleanup_removes_only_marked_current_receipt(self):
        with self.fixture() as (home, source, app, command, config, state):
            settings = core.read_json(config)
            settings['tools'] = []
            core.atomic_json(config, settings)
            with patch.object(sys, 'argv', ['modelhub-kit', 'maintain']), \
                    patch.object(cli, 'check_identity', return_value={}):
                value = cli.main()
            self.assertEqual(value['format'], lifecycle.MAINTENANCE_MARKER)
            self.assertEqual(value['modelCalls'], 0)
            core.atomic_text(state / 'user-notes.txt', 'preserve')
            result = lifecycle.uninstall(home, config, state, apply=True, clean_state=True)
            self.assertEqual(result['status'], 'removed')
            self.assertEqual(result['maintenanceReceiptsRemoved'], 1)
            self.assertFalse((state / 'last-maintenance.json').exists())
            self.assertEqual((state / 'user-notes.txt').read_text(), 'preserve')
            self.assertTrue((state / 'writer.lock').exists())

    def test_custom_state_and_personal_data_overlap_are_preserved(self):
        for scenario in ('old-receipt', 'extended-receipt', 'config-overlap', 'state-overlap'):
            with self.subTest(scenario=scenario), self.fixture() as (home, source, app, command, config, state):
                if scenario == 'old-receipt':
                    core.atomic_json(state / 'last-maintenance.json', {'version': 1, 'user': 'custom'})
                elif scenario == 'extended-receipt':
                    core.atomic_json(state / 'last-maintenance.json', {'format': lifecycle.MAINTENANCE_MARKER,
                                                                     'personalNotes': 'preserve'})
                elif scenario == 'config-overlap':
                    config = app / 'README.md'
                else:
                    state = app / 'modelhub_kit'
                result = lifecycle.uninstall(home, config, state, apply=True, clean_state=True)
                self.assertEqual(result['status'], 'queued')
                self.assertEqual(result['filesRemoved'], 0)
                self.assertTrue(command.exists())

    def test_cli_uninstall_plan_does_not_need_valid_personal_configuration(self):
        with self.fixture() as (home, source, app, command, config, state):
            config.write_text('custom invalid JSON must remain unchanged')
            with patch.object(sys, 'argv', ['modelhub-kit', 'uninstall']), \
                    patch.object(cli, 'check_identity', return_value={}):
                result = cli.main()
            self.assertEqual(result['status'], 'planned')
            self.assertTrue(app.is_dir())
            self.assertEqual(config.read_text(), 'custom invalid JSON must remain unchanged')


if __name__ == '__main__':
    unittest.main()
