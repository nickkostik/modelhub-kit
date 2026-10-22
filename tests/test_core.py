import json
import ctypes
import io
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.request
from unittest.mock import patch, MagicMock

from modelhub_kit import cache, catalog, core, integration, updater, mutation


def client_environment(home):
    return {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'USERPROFILE': str(home),
            'CODEX_HOME': str(home / '.codex'), 'XDG_CONFIG_HOME': str(home / '.config'),
            'XDG_STATE_HOME': str(home / '.local/state'), 'XDG_CACHE_HOME': str(home / '.cache'),
            'APPDATA': str(home / 'AppData/Roaming'), 'LOCALAPPDATA': str(home / 'AppData/Local'),
            'PI_CODING_AGENT_DIR': str(home / '.pi/agent')}


class CoreTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux isolated subreaper fixture')
    def test_mutation_follows_orphaned_new_session_past_observation_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = pathlib.Path(temporary) / 'finished'
            descendant = 'import pathlib,time;time.sleep(.15);pathlib.Path(' + repr(str(marker)) + ').write_text("done")'
            parent = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(descendant) + '],start_new_session=True)'
            result = core.run_mutation([sys.executable, '-c', parent], timeout=.01)
            self.assertEqual(marker.read_text(), 'done')
            self.assertEqual(result['exit'], 0)
            self.assertTrue(result['treeComplete'])
            self.assertTrue(result['observationExceeded'])
            self.assertFalse(result['timedOut'])

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux isolated subreaper fixture')
    def test_mutation_descendant_failure_is_not_parent_success(self):
        descendant = 'import time;time.sleep(.03);raise SystemExit(9)'
        parent = 'import subprocess,sys;subprocess.Popen([sys.executable,"-c",' + repr(descendant) + '])'
        result = core.run_mutation([sys.executable, '-c', parent], timeout=.01)
        self.assertNotEqual(result['exit'], 0)
        self.assertTrue(result['treeComplete'])

    def test_mutation_unsupported_platform_queues_before_spawning(self):
        with patch.object(mutation.sys, 'platform', 'darwin'), patch.object(mutation.subprocess, 'Popen') as child:
            with self.assertRaisesRegex(core.GuardError, 'custody-unavailable'):
                mutation.run(['vendor', 'install'])
            child.assert_not_called()

    def test_windows_mutation_follows_job_without_destructive_finish(self):
        events = []
        class Job:
            def __init__(self, **kwargs):
                self.assert_flags = kwargs
                events.append(('job', kwargs))
            def attach_resume(self, child): events.append('attached')
            def wait_empty(self): events.append('empty')
            def close(self): events.append('closed')
        class Child:
            stdout = io.BytesIO(b'vendor output')
            stdin = None
            returncode = 0
            def wait(self): events.append('parent-exited'); return 0
            def kill(self): raise AssertionError('running mutation must not be killed')
        with patch.object(mutation, 'os', types.SimpleNamespace(name='nt')), patch.object(mutation, 'WindowsJob', Job), \
                patch.object(mutation.subprocess, 'Popen', return_value=Child()), \
                patch.object(mutation.subprocess, 'CREATE_NO_WINDOW', 0x08000000, create=True):
            result = mutation.run(['vendor', 'install'], timeout=0)
        self.assertEqual(events[0], ('job', {'kill_on_close': False}))
        self.assertLess(events.index('parent-exited'), events.index('empty'))
        self.assertEqual(events[-1], 'closed')
        self.assertTrue(result['treeComplete'])

    def test_windows_natural_job_wait_retries_unknown_and_active_counts(self):
        calls = []
        counts = iter([None, 2, 1, 0])
        class Api:
            def QueryInformationJobObject(self, handle, kind, buffer, size, returned):
                value = next(counts); calls.append(value)
                if value is None: return False
                buffer[40:44] = value.to_bytes(4, 'little')
                return True
            def TerminateJobObject(self, *args): raise AssertionError('destructive Job call')
        job = core.WindowsJob.__new__(core.WindowsJob)
        job.c, job.api, job.handle = ctypes, Api(), 1
        with patch.object(core.time, 'sleep'):
            job.wait_empty()
        self.assertEqual(calls, [None, 2, 1, 0])

    def test_mutation_delivery_and_reader_errors_close_input_and_follow_child(self):
        stream = MagicMock()
        stream.closed = False
        stream.write.side_effect = OSError('fixture write failure')
        def close(): stream.closed = True
        stream.close.side_effect = close
        child = MagicMock(stdin=stream, returncode=1)
        child.stdout.read.side_effect = OSError('fixture output failure')
        child.wait.side_effect = lambda: self.assertTrue(stream.closed)
        with patch.object(mutation.subprocess, 'Popen', return_value=child):
            result = mutation.run(['vendor', 'install'])
        self.assertTrue(result['observationFailed'])
        self.assertTrue(result['treeComplete'])
        self.assertGreaterEqual(child.wait.call_count, 1)
        child.kill.assert_not_called()

    def test_durable_windows_publication_uses_write_through(self):
        move = MagicMock(return_value=1)
        with patch.object(core, 'os', types.SimpleNamespace(name='nt')), \
                patch.object(ctypes, 'WinDLL', return_value=types.SimpleNamespace(MoveFileExW=move), create=True):
            core._replace_file('source', 'target', durable=True)
        move.assert_called_once_with('source', 'target', 0x1 | 0x8)

    def test_npm_install_uses_natural_custody_and_macos_queues(self):
        for system in ('Linux', 'Darwin'):
            with self.subTest(system=system), tempfile.TemporaryDirectory() as folder:
                prefix, state = pathlib.Path(folder) / 'prefix', pathlib.Path(folder) / 'state'
                metadata = updater.package_file(prefix, '@qwen-code/qwen-code')
                data = {'name': '@qwen-code/qwen-code', 'version': '1.0.0', 'bin': {'qwen': 'cli.js'}}
                core.atomic_json(metadata, data)
                entry = metadata.parent / 'cli.js'; core.atomic_text(entry, '// fixture')
                def query(argv, **kwargs):
                    self.assertNotIn('install', argv)
                    return {'exit': 0, 'output': json.dumps({'platform': 'linux', 'arch': 'x64'}) if '-p' in argv else str(prefix)}
                def install(argv, **kwargs):
                    self.assertIn('install', argv)
                    self.assertEqual(argv.count('--umask=0022'), 1)
                    core.atomic_json(metadata, dict(data, version='1.1.0'))
                    return {'exit': 0, 'output': '', 'treeComplete': True}
                with patch.object(updater, 'npm_command', return_value=['npm']), patch.object(updater, 'run', side_effect=query), \
                        patch.object(updater, 'run_mutation', side_effect=install) as mutate, \
                        patch.object(updater.shutil, 'which', return_value=str(entry)), \
                        patch.object(updater.platform, 'system', return_value=system), \
                        patch.object(updater, 'http_json', return_value=({'version': '1.1.0'}, {})), \
                        patch.object(cache, 'process_inventory', return_value=[]):
                    result = updater.update({'tools': ['qwen']}, state)
                self.assertEqual(result[0]['status'], 'updated' if system == 'Linux' else 'queued')
                self.assertEqual(mutate.call_count, int(system == 'Linux'))

    def test_current_codex_health_failure_requires_explicit_selected_repair(self):
        for repair in (False, True):
            with self.subTest(repair=repair), tempfile.TemporaryDirectory() as folder:
                prefix, state = pathlib.Path(folder) / 'prefix', pathlib.Path(folder) / 'state'
                metadata = updater.package_file(prefix, '@openai/codex')
                data = {'name': '@openai/codex', 'version': '1.0.0', 'bin': {'codex': 'cli.js'}}
                core.atomic_json(metadata, data); entry = metadata.parent / 'cli.js'; core.atomic_text(entry, '// fixture')
                def query(argv, **kwargs):
                    self.assertNotIn('install', argv)
                    return {'exit': 0, 'output': json.dumps({'platform': 'linux', 'arch': 'x64'}) if '-p' in argv else str(prefix)}
                with patch.object(updater, 'npm_command', return_value=['npm']), patch.object(updater, 'run', side_effect=query), \
                        patch.object(updater, 'run_mutation', return_value={'exit': 0, 'output': '', 'treeComplete': True}) as mutate, \
                        patch.object(updater.shutil, 'which', return_value=str(entry)), \
                        patch.object(updater, 'healthy_codex', return_value=repair), \
                        patch.object(updater, 'http_json', return_value=({'version': '1.0.0'}, {})), \
                        patch.object(cache, 'process_inventory', return_value=[]):
                    result = updater.update({'tools': ['codex']}, state, selected=['codex'] if repair else None, repair=repair)
                self.assertEqual(result[0]['status'], 'updated' if repair else 'repair-needed')
                self.assertEqual(mutate.call_count, int(repair))
                if repair:
                    self.assertEqual(mutate.call_args.args[0].count('--umask=0022'), 1)
        with self.assertRaisesRegex(core.GuardError, 'repair-requires-explicit-tool-selection'):
            updater.update({'tools': ['codex']}, pathlib.Path('unused-fixture'), repair=True)

    def test_missing_npm_install_uses_scoped_mask_without_rewriting_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            state = pathlib.Path(folder) / 'state'
            metadata = updater.package_file(state / 'npm', '@qwen-code/qwen-code')
            def query(argv, **kwargs):
                self.assertNotIn('--umask=0022', argv)
                return {'exit': 0, 'output': json.dumps({'platform': 'linux', 'arch': 'x64'}) if '-p' in argv else str(state / 'unused-prefix')}
            def install(argv, **kwargs):
                self.assertEqual(argv.count('--umask=0022'), 1)
                self.assertIn('--include=optional', argv)
                self.assertEqual(argv[argv.index('--prefix') + 1], str(state / 'npm'))
                self.assertEqual(argv[-1], '@qwen-code/qwen-code@1.1.0')
                self.assertEqual(kwargs['env']['NPM_CONFIG_UMASK'], '0000')
                core.atomic_json(metadata, {'name': '@qwen-code/qwen-code', 'version': '1.1.0'})
                return {'exit': 0, 'output': '', 'treeComplete': True}
            with patch.dict(os.environ, {'NPM_CONFIG_UMASK': '0000'}), \
                    patch.object(updater, 'npm_command', return_value=['node', 'npm-cli.js']), \
                    patch.object(updater, 'run', side_effect=query), \
                    patch.object(updater, 'run_mutation', side_effect=install) as mutate, \
                    patch.object(updater.shutil, 'which', side_effect=lambda name: '/fixture/node' if name == 'node' else None), \
                    patch.object(updater.platform, 'system', return_value='Linux'), \
                    patch.object(updater, 'http_json', return_value=({'version': '1.1.0'}, {})), \
                    patch.object(updater, 'expose_installation') as expose, \
                    patch.object(cache, 'process_inventory', return_value=[]):
                result = updater.update({}, state, selected=['qwen'], install_missing=True)
                self.assertEqual(os.environ['NPM_CONFIG_UMASK'], '0000')
            self.assertEqual(result[0]['status'], 'updated')
            self.assertEqual(mutate.call_count, 1)
            expose.assert_called_once()

    def test_owned_atomic_helper_has_linux_custody_and_distinct_darwin_staging(self):
        with tempfile.TemporaryDirectory() as folder:
            home, state = pathlib.Path(folder), pathlib.Path(folder) / 'state'
            for system in ('Linux', 'Darwin'):
                with self.subTest(system=system), patch.object(updater, 'tools', return_value={'fixture': {'command': 'fixture', 'manager': 'native-omp'}}), \
                        patch.object(updater.shutil, 'which', return_value=str(home / '.local/bin/fixture')), \
                        patch.object(updater.pathlib.Path, 'home', return_value=home), \
                        patch.object(updater.platform, 'system', return_value=system), \
                        patch.object(updater, 'run', return_value={'exit': 0, 'output': '{"changed":false}'}) as query, \
                        patch.object(updater, 'run_mutation', return_value={'exit': 0, 'output': '{"changed":false}', 'treeComplete': True}) as mutate:
                    result = updater.update({'tools': ['fixture']}, state)
                self.assertEqual(result[0]['status'], 'current')
                self.assertEqual(mutate.call_count, int(system == 'Linux'))
                self.assertEqual(query.call_count, int(system == 'Darwin'))

    def test_expected_text_refuses_concurrent_config_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'config.json'
            core.atomic_text(path, 'user edit')
            with self.assertRaises(core.GuardError):
                core.atomic_text(path, 'managed edit', expected='old text')
            self.assertEqual(path.read_text(), 'user edit')

    def test_bearer_cross_origin_redirect_is_rejected_before_follow(self):
        request = urllib.request.Request('https://catalog.example.invalid/models', headers={'Authorization': 'Bearer demo'})
        with self.assertRaises(core.GuardError):
            core.SameOriginRedirect().redirect_request(request, None, 302, 'redirect', {}, 'https://other.example.invalid/models')

    def test_kernel_writer_excludes_other_writers_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            with core.writer(tmp):
                with self.assertRaises(core.GuardError):
                    with core.writer(tmp):
                        self.fail('second writer acquired')
            with core.writer(tmp):
                pass

    def test_child_timeout_is_bounded_without_global_process_search(self):
        result = core.run([sys.executable, '-c', 'import time; time.sleep(20)'], timeout=0.1)
        self.assertTrue(result['timedOut'])
        self.assertNotEqual(result['exit'], 0)

    @unittest.skipUnless(os.name == 'nt', 'Native Windows own-tree cancellation')
    def test_windows_timeout_cancels_installer_descendant(self):
        code = 'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"]); print(p.pid,flush=True);time.sleep(60)'
        result = core.run([sys.executable, '-c', code], timeout=1)
        self.assertTrue(result['timedOut'])
        pid = int(result['output'].strip())
        check = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                f'if(Get-Process -Id {pid} -ErrorAction SilentlyContinue){{exit 1}}'], capture_output=True)
        self.assertEqual(check.returncode, 0)

    def test_corrupt_cache_refetches_and_keeps_all_reasoning_choices(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp)
            core.atomic_text(state / 'catalogs/provider.json', '[]')
            source = {'id': 'provider', 'url': 'https://catalog.example.invalid/models'}
            settings = {'catalog_sources': [source]}
            choices = ['low', 'high', 'xhigh', 'max', 'ultra', 'custom-effort']
            fetch = lambda url, headers: ({'data': [{'id': 'exact/provider/model', 'reasoning_efforts': choices}]}, {'ETag': 'revision-a'})
            first = catalog.sync(settings, state, fetch)
            self.assertEqual(first[0]['status'], 'ready')
            saved = core.read_json(state / 'catalogs/provider.json')
            self.assertEqual(saved['models'][0]['reasoning_efforts'], choices)
            second = catalog.sync(settings, state, lambda url, headers: (None, {}))
            self.assertEqual(second[0]['status'], 'not-modified')
            prior = (state / 'catalogs/provider.json').read_bytes()
            result = catalog.sync(settings, state, lambda url, headers: ({'data': []}, {}))
            self.assertTrue(result[0]['retained'])
            self.assertEqual((state / 'catalogs/provider.json').read_bytes(), prior)

    def test_client_custom_entry_preserved_and_owned_repeat_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, state = pathlib.Path(tmp), pathlib.Path(tmp) / 'state'
            with patch.dict(os.environ, client_environment(home), clear=True), patch.object(integration, 'native_idle', return_value=True):
                path = home / '.codex/config.toml'
                core.atomic_text(path, 'model = "chosen-model"\n[mcp_servers.personal]\nurl = "https://custom.example.invalid/mcp"\n')
                settings = {'mcp_servers': {'managed': {'url': 'https://server.example.invalid/mcp'}}}
                first = integration.integrate(settings, home, state, 'codex', True)
                self.assertTrue(first['changed'])
                second = integration.integrate(settings, home, state, 'codex', True)
                self.assertFalse(second['changed'])
                self.assertIn('model = "chosen-model"', path.read_text())
                with self.assertRaises(core.GuardError):
                    integration.integrate({'mcp_servers': {'personal': {'url': 'https://other.example.invalid/mcp'}}}, home, state, 'codex', True)

    def test_windows_system_process_without_commandline_does_not_block(self):
        fake_os = types.SimpleNamespace(name='nt', getpid=lambda: 500, getppid=lambda: 400)
        value = {'exit': 0, 'outputTruncated': False, 'timedOut': False, 'output': json.dumps([
            {'ProcessId': 4, 'Name': 'System', 'CommandLine': None},
            {'ProcessId': 20, 'Name': 'svchost.exe', 'CommandLine': None}])}
        with patch.object(cache, 'os', fake_os), patch.object(cache, 'run', return_value=value):
            self.assertEqual(cache.process_inventory(), ['System', 'svchost.exe'])
            value['output'] = json.dumps([{'ProcessId': 40, 'Name': 'node.exe', 'CommandLine': None}])
            self.assertIsNone(cache.process_inventory())

    def test_native_client_bearer_references_never_resolve_secret_values(self):
        references = {'claude': '${MCP_TOKEN}', 'opencode': '{env:MCP_TOKEN}', 'cursor': '${env:MCP_TOKEN}'}
        for client, reference in references.items():
            with self.subTest(client=client), tempfile.TemporaryDirectory() as tmp:
                home, state = pathlib.Path(tmp), pathlib.Path(tmp) / 'state'
                with patch.dict(os.environ, client_environment(home), clear=True), patch.object(integration, 'native_idle', return_value=True):
                    settings = {'mcp_servers': {'example': {'url': 'https://server.example.invalid/mcp', 'bearer_env': 'MCP_TOKEN'}}}
                    integration.integrate(settings, home, state, client, True)
                    path = home / {'claude': '.claude.json', 'opencode': '.config/opencode/opencode.json', 'cursor': '.cursor/mcp.json'}[client]
                    document = json.loads(path.read_text())
                    entry = document['mcp' if client == 'opencode' else 'mcpServers']['example']
                    self.assertEqual(entry['headers']['Authorization'], 'Bearer ' + reference)

    def test_npm_current_tool_is_not_reinstalled_and_active_entrypoint_defers(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix, state = pathlib.Path(tmp) / 'prefix', pathlib.Path(tmp) / 'state'
            metadata = updater.package_file(prefix, '@qwen-code/qwen-code')
            core.atomic_json(metadata, {'name': '@qwen-code/qwen-code', 'version': '1.0.0', 'bin': {'qwen': 'cli.js'}})
            entry = metadata.parent / 'cli.js'
            core.atomic_text(entry, '// local fixture')
            calls = []
            def execute(argv, **kwargs):
                calls.append(argv)
                return {'exit': 0, 'output': str(prefix), 'timedOut': False, 'outputTruncated': False}
            with patch.object(updater, 'npm_command', return_value=['npm']), patch.object(updater, 'run', side_effect=execute), patch.object(updater.shutil, 'which', return_value=str(entry)), patch.object(updater, 'http_json', return_value=({'version': '1.0.0'}, {})):
                result = updater.update({'tools': ['qwen']}, state)
                self.assertEqual(result[0]['status'], 'current')
                self.assertFalse(any('install' in c for c in calls))
            with patch.object(updater, 'npm_command', return_value=['npm']), patch.object(updater, 'run', side_effect=execute), patch.object(updater.shutil, 'which', return_value=str(entry)), patch.object(updater, 'http_json', return_value=({'version': '1.1.0'}, {})), patch.object(cache, 'process_inventory', return_value=['node ' + str(entry)]):
                result = updater.update({'tools': ['qwen']}, state)
                self.assertEqual(result[0]['status'], 'queued-active-or-unknown-processes')
                self.assertFalse(any('install' in c for c in calls))

    @unittest.skipIf(os.name == 'nt', 'Unix symlink launcher')
    def test_kit_installation_exposes_command_and_rejects_custom_inner_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, state = pathlib.Path(tmp), pathlib.Path(tmp) / 'state'
            metadata = updater.package_file(state / 'npm', '@openai/codex')
            core.atomic_json(metadata, {'version': '1.0.0'})
            entry = metadata.parent / 'cli.js'
            core.atomic_text(entry, '// fixture')
            native = state / 'npm/bin/codex'
            core.private_directory(native.parent)
            native.symlink_to(entry)
            with patch.object(updater, 'locations', return_value=(home, None, state)):
                launcher = updater.expose_installation(state, 'codex', {'command': 'codex'})
            self.assertEqual(launcher.resolve(), entry)
            self.assertIsNotNone(updater.owned_installation(state, 'codex'))
            outside = home / 'custom'
            core.atomic_text(outside, '// custom')
            native.unlink()
            native.symlink_to(outside)
            with patch.object(updater, 'npm_command', return_value=['npm']), patch.object(updater, 'run', return_value={'exit': 0, 'output': str(state / 'npm')}), patch.object(updater.shutil, 'which', return_value=str(launcher)):
                result = updater.update({'tools': ['codex']}, state)
            self.assertEqual(result[0]['status'], 'different-active-installation-preserved')
            self.assertEqual(native.resolve(), outside)


    def test_subscription_environment_drops_every_grok_api_key_alias(self):
        from modelhub_kit import updater
        source = {
            'PATH': '/fixture', 'XAI_API_KEY': 'secret', 'XAI_API_KEY_2': 'secret',
            'GROK_API_KEY': 'secret', 'GROK_CODE_API_KEY': 'secret',
            'GROK_CODE_XAI_API_KEY_9': 'secret', 'SUPER_GROK_PLAN': 'secret',
            'GROK_CLI_CHAT_PROXY_BASE_URL': 'https://api.x.ai/v1', 'KEEP': 'value',
        }
        result = updater.subscription_only_environment(source)
        self.assertEqual(result, {'PATH': '/fixture', 'KEEP': 'value', 'GROK_DISABLE_API_KEY_AUTH': '1'})
        self.assertEqual(source['XAI_API_KEY'], 'secret')

if __name__ == '__main__':
    unittest.main()
