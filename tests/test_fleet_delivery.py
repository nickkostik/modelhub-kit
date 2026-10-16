import base64
import gzip
import io
import json
import os
import pathlib
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit import fleet
from modelhub_kit.core import GuardError, atomic_json, identity


class FleetDeliveryTests(unittest.TestCase):
    def fixture(self, root, extra=''):
        (root / 'scripts').mkdir()
        (root / 'modelhub_kit').mkdir()
        (root / 'modelhub_kit/__init__.py').write_text('# public source fixture\n')
        (root / 'scripts/install.py').write_text(
            "FILES=('modelhub_kit/__init__.py',)\nOPTIONAL_FILES=()\n"
            "import pathlib\npathlib.Path.home().joinpath('installed-fixture').write_text('installed')\n" + extra)
        (root / 'scripts/install.sh').write_text(
            '#!/bin/sh\nexec python3 -B "$(dirname "$0")/install.py" "$@"\n')
        (root / 'scripts/install.ps1').write_text('# local test fixture\n')
        return fleet.delivery_package(root)

    def test_only_literal_source_allowlist_travels_and_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, metadata = self.fixture(root)
            (root / '.git').mkdir()
            (root / '.git/private-history').write_text('must never travel')
            (root / 'auth.json').write_text('must never travel')
            second, _ = fleet.delivery_package(root)
            self.assertEqual(payload, second)
            with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
                self.assertEqual(set(archive.getnames()), set(metadata['files']))
                self.assertTrue(all(member.isfile() and member.uid == 0 and member.mtime == 0 for member in archive))
            file = root / 'modelhub_kit/__init__.py'
            file.unlink()
            file.symlink_to(root / 'auth.json')
            with self.assertRaises(GuardError):
                fleet.delivery_package(root)

    def test_unsafe_declared_path_and_invalid_later_host_prevent_all_remote_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            self.fixture(root)
            (root / 'scripts/install.py').write_text("FILES=('../auth.json',)\n")
            with self.assertRaisesRegex(GuardError, 'allowlist'):
                fleet.delivery_package(root)
            inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'},
                                                          {'alias': '-oProxyCommand=anything', 'os': 'linux'}]})
            with patch.object(fleet, 'run') as execute, self.assertRaises(GuardError):
                fleet.reconcile(inventory, root / 'state', apply=True)
            execute.assert_not_called()

    def test_other_boot_and_offline_nodes_queue_without_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, metadata = self.fixture(root)
            inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux:arch'},
                                                          {'alias': 'portable', 'os': 'windows'}]})
            responses = [{'exit': 0, 'timedOut': False, 'output': 'modelhub-kit-probe-v1\nlinux\nother\nboot-a\n'},
                         {'exit': 255, 'timedOut': False, 'output': ''}]
            with patch.object(fleet, 'run', side_effect=responses) as execute:
                result = fleet.reconcile(inventory, root / 'state', apply=True, source=root, enroll=True)
            self.assertEqual([row['status'] for row in result['hosts']], ['queued', 'queued'])
            self.assertEqual(result['hosts'][0]['reason'], 'different-os-boot')
            self.assertEqual(execute.call_count, 2)
            for call in execute.call_args_list:
                self.assertIn('StrictHostKeyChecking=yes', call.args[0])
                self.assertIn('BatchMode=yes', call.args[0])
                self.assertNotIn('input_data', call.kwargs)

    @unittest.skipUnless(os.name == 'posix', 'Owned Unix shell fixture')
    def test_exact_payload_installs_without_waiting_for_stdin_eof_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source, home = base / 'source', base / 'home with spaces'
            source.mkdir(); home.mkdir()
            payload, metadata = self.fixture(source)
            observed = identity()
            command = fleet._unix_delivery(observed['os'], observed['boot'], metadata, False, False)
            child = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=dict(os.environ, HOME=str(home)))
            try:
                child.stdin.write(payload); child.stdin.flush()
                # Deliberately keep stdin open. The receiver must consume its exact length.
                child.wait(timeout=15)
                output, errors = child.stdout.read(), child.stderr.read()
                self.assertEqual(child.returncode, 0, errors.decode())
                self.assertEqual(json.loads(output)['status'], 'installed-or-current')
            finally:
                if child.poll() is None:
                    child.kill(); child.wait()
                child.stdin.close(); child.stdout.close(); child.stderr.close()
            self.assertEqual((home / 'installed-fixture').read_text(), 'installed')
            self.assertEqual(list(home.glob('.modelhub-kit-delivery.*')), [])

    @unittest.skipUnless(os.name == 'posix', 'Owned Unix shell fixture')
    def test_changed_boot_writes_nothing_and_bad_payload_never_executes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source, home = base / 'source', base / 'home'
            source.mkdir(); home.mkdir()
            payload, metadata = self.fixture(source)
            observed = identity()
            for boot, data, expected in [('different-boot', payload, 78),
                                         (observed['boot'], b'X' + payload[1:], 75)]:
                command = fleet._unix_delivery(observed['os'], boot, metadata, False, False)
                result = subprocess.run(shlex.split(command), input=data, capture_output=True, timeout=15,
                                        env=dict(os.environ, HOME=str(home)))
                self.assertEqual(result.returncode, expected)
                self.assertFalse((home / 'installed-fixture').exists())
                self.assertEqual(list(home.iterdir()), [])

    @unittest.skipUnless(os.name == 'posix', 'Owned Unix shell fixture')
    def test_unknown_file_in_owned_delivery_temp_is_preserved_and_queued(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source, home = base / 'source', base / 'home'
            source.mkdir(); home.mkdir()
            payload, metadata = self.fixture(source,
                "pathlib.Path(__file__).resolve().parents[1].joinpath('custom-file').write_text('keep')\n")
            observed = identity()
            result = subprocess.run(shlex.split(fleet._unix_delivery(observed['os'], observed['boot'], metadata, False, False)),
                                    input=payload, capture_output=True, timeout=15, env=dict(os.environ, HOME=str(home)))
            self.assertEqual(result.returncode, 75)
            self.assertEqual(json.loads(result.stdout)['reason'], 'delivery-cleanup-incomplete')
            remaining = list(home.glob('.modelhub-kit-delivery.*'))
            self.assertEqual(len(remaining), 1)
            self.assertEqual((remaining[0] / 'custom-file').read_text(), 'keep')
            self.assertEqual(list(remaining[0].iterdir()), [remaining[0] / 'custom-file'])

    @unittest.skipUnless(os.name == 'posix', 'Owned Unix SSH transport fixture')
    def test_full_reconcile_uses_real_bounded_binary_transport_and_requires_complete_input(self):
        from modelhub_kit.core import run as execute_child
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source, home = base / 'source', base / 'home'
            source.mkdir(); home.mkdir()
            self.fixture(source)
            (source / 'modelhub_kit/__init__.py').write_text('# fixture padding\n' * 12000)
            inventory = base / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': identity()['os']}]})
            calls = []
            def transport(argv, **kwargs):
                calls.append(kwargs)
                return execute_child(shlex.split(argv[-1]), env=dict(os.environ, HOME=str(home)), **kwargs)
            with patch.object(fleet, 'run', side_effect=transport):
                result = fleet.reconcile(inventory, base / 'state', apply=True, source=source, enroll=True)
            self.assertEqual(result['hosts'][0]['status'], 'completed')
            self.assertGreater(len(calls[1]['input_data']), 65536)
            self.assertEqual(list(home.glob('.modelhub-kit-delivery.*')), [])
            responses = [{'exit': 0, 'output': 'modelhub-kit-probe-v1\nlinux\narch\nboot-a\n'},
                         {'exit': 0, 'output': '{"status":"installed-or-current","cleanupComplete":true}',
                          'inputSent': 0, 'inputFailed': True}]
            with patch.object(fleet, 'run', side_effect=responses):
                result = fleet.reconcile(inventory, base / 'state', apply=True, source=source, enroll=True)
            self.assertEqual(result['hosts'][0]['reason'], 'delivery-input-incomplete')
            self.assertEqual(result['hosts'][0]['status'], 'queued')

    @unittest.skipUnless(os.name == 'posix', 'Owned Unix archive replacement fixture')
    def test_cleanup_preserves_replaced_payload_even_when_its_hash_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            source, home = base / 'source', base / 'home'
            source.mkdir(); home.mkdir()
            payload, metadata = self.fixture(source,
                "archive=pathlib.Path(__file__).resolve().parents[1]/'payload.tar'\n"
                "body=archive.read_bytes(); held=archive.open('rb'); archive.unlink(); archive.write_bytes(body); held.close()\n")
            observed = identity()
            result = subprocess.run(shlex.split(fleet._unix_delivery(observed['os'], observed['boot'], metadata, False, False)),
                                    input=payload, capture_output=True, timeout=15, env=dict(os.environ, HOME=str(home)))
            self.assertEqual(result.returncode, 75)
            self.assertEqual(json.loads(result.stdout)['reason'], 'delivery-cleanup-incomplete')
            remaining = list(home.glob('.modelhub-kit-delivery.*'))
            self.assertEqual(len(remaining), 1)
            self.assertEqual((remaining[0] / 'payload.tar').read_bytes(), payload)
            self.assertEqual(list(remaining[0].iterdir()), [remaining[0] / 'payload.tar'])

    def test_nested_queued_maintenance_and_arbitrary_json_never_count_as_complete(self):
        legacy = {'version': 1, 'modelCalls': 0, 'startedAt': 'start', 'completedAt': 'end',
                  'tools': [], 'catalogs': [], 'cache': {'status': 'absent'}}
        self.assertTrue(fleet._maintenance_complete(legacy))
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'}]})
            cases = [{}, {'modelCalls': 0}, dict(legacy, tools=[{'status': 'queued-active-or-unknown-processes'}]),
                     dict(legacy, catalogs=[{'status': 'refresh-failed-last-good-retained'}]),
                     dict(legacy, status='completed', cache={'status': 'queued-active-or-unknown-runtime'})]
            for response in cases:
                with self.subTest(response=response), patch.object(fleet, 'run', side_effect=[
                    {'exit': 0, 'output': 'modelhub-kit-probe-v1\nlinux\narch\nboot-a\n'},
                    {'exit': 0, 'output': json.dumps(response)}]):
                    result = fleet.reconcile(inventory, root / 'state', apply=True)
                self.assertEqual(result['hosts'][0]['status'], 'queued')
                self.assertEqual(result['hosts'][0]['reason'], 'maintenance-incomplete')

    def test_explicit_completed_benign_opt_outs_pass_but_ownership_conflicts_queue(self):
        benign = ('installed', 'applied', 'exported', 'not-installed', 'missing', 'no-configured-entries',
                  'no-configured-servers', 'no-configured-providers', 'no-configured-agents')
        for status in benign:
            with self.subTest(status=status):
                self.assertTrue(fleet._maintenance_complete({'status': 'completed', 'results': [{'status': status}]}))
        conflicts = ('newer-or-unordered-installation-preserved', 'unmanaged-installation-preserved', 'different-active-installation-preserved',
                     'custom-native-files-preserved', 'unrecognized-native-release-preserved',
                     'native-metadata-unavailable', 'future-unreviewed-status', 'queued', 'update-failed', [], {})
        for status in conflicts:
            with self.subTest(status=status):
                self.assertFalse(fleet._maintenance_complete({'status': 'completed', 'results': [{'status': status}]}))

    def test_windows_command_is_finite_encoded_and_checks_boot_before_first_write(self):
        with tempfile.TemporaryDirectory() as directory:
            payload, metadata = self.fixture(pathlib.Path(directory))
            command, prefix = fleet._windows_delivery('windows', 'boot-a', metadata, True, True)
            self.assertLess(len(command), 7500)
            loader = base64.b64decode(command.split()[-1]).decode('utf-16le')
            script = gzip.decompress(prefix).decode('utf-8')
            self.assertTrue(loader.startswith("$ProgressPreference='SilentlyContinue'"))
            self.assertIn('$modelhubWire.Read($zip,$offset', loader)
            self.assertNotIn('[Console]::OpenStandardInput()', loader)
            self.assertIn("GetProperty('ConsoleInputHandle'", loader)
            self.assertIn('[IO.FileStream]::new($handle,[IO.FileAccess]::Read,65536,$false)', loader)
            self.assertIn("if(-not $prop){throw 'native-stdin-unavailable'}", loader)
            self.assertIn(__import__('hashlib').sha256(prefix).hexdigest(), loader)
            self.assertIn('$inputStream=$modelhubWire', script)
            self.assertIn('$archive=New-Object byte[] ' + str(len(payload)), loader)
            self.assertIn(metadata['sha256'], loader)
            self.assertLess(loader.index('$modelhubWire.Read($archive,$offset'), loader.index('[ScriptBlock]::Create'))
            self.assertLess(loader.index('[IO.MemoryStream]::new($archive,$false)'), loader.index('[ScriptBlock]::Create'))
            self.assertEqual(loader.count("if($actualBoot -ne 'boot-a')"), 2)
            self.assertNotIn('ReadToEnd', loader)
            self.assertLess(script.index("if($actualBoot -ne 'boot-a')"), script.index('New-Item'))
            self.assertEqual(script.count("if($actualBoot -ne 'boot-a')"), 2)
            self.assertIn('$remaining=' + str(len(payload)), script)
            self.assertIn('$remaining-=$n', script)
            self.assertIn('-Prerequisites -Schedule', script)
            self.assertNotIn('ReadToEnd', script)
            self.assertNotIn('-Recurse', script)
            self.assertIn('GetFileInformationByHandle', script)
            self.assertIn('GetOwner([Security.Principal.SecurityIdentifier])', script)
            self.assertIn('Stage-Acl) -ne $stageAcl', script)
            self.assertIn('[ModelHubDeliveryId]::Path($p,$false) -ne $payloadId', script)
            self.assertNotIn('Get-Command tar.exe', script)
            self.assertIn("$tar=Join-Path ([Environment]::SystemDirectory) 'tar.exe'", script)
            self.assertIn('Test-Path -LiteralPath $tar -PathType Leaf', script)
            self.assertIn('(Get-Item -LiteralPath $tar).Attributes -band [IO.FileAttributes]::ReparsePoint', script)
            self.assertIn(' & $tar -xf ', script)
            self.assertIn('$directories=[Text.Encoding]::UTF8.GetString(', script)
            self.assertNotIn('$directories=@(', script)

    def test_windows_loader_rejects_unbounded_archive_and_malformed_hash(self):
        for size in (-1, fleet.MAX_DELIVERY + 1, True, 1.5, '1'):
            with self.subTest(size=size), self.assertRaises(fleet.GuardError):
                fleet._windows_loader('exit 0', 'boot-a', size)
        for digest in ('bad', 'A' * 64, ['0' * 64]):
            with self.subTest(digest=digest), self.assertRaises(fleet.GuardError):
                fleet._windows_loader('exit 0', 'boot-a', 1, digest)

    def test_windows_reconcile_sends_deterministic_script_prefix_then_exact_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            payload, metadata = self.fixture(root)
            inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'windows'}]})
            command, prefix = fleet._windows_delivery('windows', 'boot-a', metadata, False, False)
            self.assertEqual((command, prefix), fleet._windows_delivery('windows', 'boot-a', metadata, False, False))
            self.assertEqual(gzip.decompress(prefix)[:len(b"$ErrorActionPreference")], b"$ErrorActionPreference")
            def transport(argv, **kwargs):
                if 'input_data' not in kwargs:
                    return {'exit': 0, 'output': '{"os":"windows","variant":null,"boot":"boot-a"}'}
                self.assertEqual(argv[-1], command)
                self.assertEqual(kwargs['input_data'], prefix + payload)
                return {'exit': 0, 'output': '{"status":"installed-or-current","cleanupComplete":true}',
                        'inputSent': len(prefix) + len(payload), 'inputFailed': False}
            with patch.object(fleet, 'run', side_effect=transport):
                result = fleet.reconcile(inventory, root / 'state', apply=True, source=root, enroll=True)
            self.assertEqual(result['hosts'][0]['status'], 'completed')

    def test_default_maintain_remains_compatible_and_binds_observed_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'}]})
            responses = [{'exit': 0, 'timedOut': False, 'output': 'modelhub-kit-probe-v1\nlinux\narch\nboot-a\n'},
                         {'exit': 0, 'timedOut': False, 'output': json.dumps({'version': 1, 'modelCalls': 0, 'startedAt': 'start', 'completedAt': 'end', 'tools': [], 'catalogs': [], 'cache': {'status': 'absent'}})}]
            with patch.object(fleet, 'run', side_effect=responses) as execute:
                result = fleet.reconcile(inventory, root / 'state', apply=True)
            self.assertEqual(result['hosts'][0]['status'], 'completed')
            self.assertIn('--expected-boot boot-a maintain', execute.call_args_list[1].args[0][-1])
            self.assertNotIn('input_data', execute.call_args_list[1].kwargs)


if __name__ == '__main__':
    unittest.main()
