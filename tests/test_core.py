import json
import os
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.request
from unittest.mock import patch

from modelhub_kit import cache, catalog, core, integration, updater


def client_environment(home):
    return {'PATH': os.environ.get('PATH', ''), 'HOME': str(home), 'USERPROFILE': str(home),
            'CODEX_HOME': str(home / '.codex'), 'XDG_CONFIG_HOME': str(home / '.config'),
            'XDG_STATE_HOME': str(home / '.local/state'), 'XDG_CACHE_HOME': str(home / '.cache'),
            'APPDATA': str(home / 'AppData/Roaming'), 'LOCALAPPDATA': str(home / 'AppData/Local'),
            'PI_CODING_AGENT_DIR': str(home / '.pi/agent')}


class CoreTests(unittest.TestCase):
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
        value = {'exit': 0, 'outputTruncated': False, 'output': json.dumps([
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


if __name__ == '__main__':
    unittest.main()
