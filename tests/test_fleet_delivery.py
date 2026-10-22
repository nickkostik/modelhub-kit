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
    def test_observer_timeout_retains_original_fence_and_prevents_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder); state = root / 'state'; inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'}]})
            responses = [{'exit': 0, 'output': 'modelhub-kit-probe-v1\nlinux\nfixture\nboot-a\n'},
                         {'exit': -15, 'output': '', 'timedOut': True}]
            with patch.object(fleet, 'run', side_effect=responses) as child:
                first = fleet.reconcile(inventory, state, apply=True)
            self.assertFalse(first['hosts'][0]['custodyKnown'])
            fence = json.loads((state / 'fleet-inflight.json').read_text())
            original = fence['operations']['workstation']['id']
            with patch.object(fleet, 'run') as child:
                second = fleet.reconcile(inventory, state, apply=True)
                child.assert_not_called()
            self.assertEqual(second['hosts'][0]['reason'], 'remote-operation-inflight')
            self.assertEqual(json.loads((state / 'fleet-inflight.json').read_text())['operations']['workstation']['id'], original)

    def test_verified_terminal_delivery_alone_clears_fence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder); state = root / 'state'; inventory = root / 'fleet.json'
            self.fixture(root)
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'}]})
            def child(argv, **kwargs):
                if 'input_data' not in kwargs:
                    return {'exit': 0, 'output': 'modelhub-kit-probe-v1\nlinux\nfixture\nboot-a\n'}
                self.assertIn('workstation', json.loads((state / 'fleet-inflight.json').read_text())['operations'])
                operation = json.loads((state / 'fleet-inflight.json').read_text())['operations']['workstation']['id']
                return {'exit': 0, 'output': json.dumps(dict(fleet._delivery_context(operation), status='installed-or-current', cleanupComplete=True, exitCode=0)),
                        'inputSent': len(kwargs['input_data']), 'inputFailed': False, 'timedOut': False}
            with patch.object(fleet, 'run', side_effect=child):
                result = fleet.reconcile(inventory, state, apply=True, source=root, enroll=True)
            self.assertEqual(result['hosts'][0]['status'], 'completed')
            self.assertEqual(json.loads((state / 'fleet-inflight.json').read_text())['operations'], {})

    def test_bound_terminal_queued_pass_clears_fence_without_claiming_success(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder); state = root / 'state'; inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'}]})
            def child(argv, **kwargs):
                if 'fleet-inflight.json' not in {p.name for p in state.iterdir()}:
                    return {'exit': 0, 'output': 'modelhub-kit-probe-v1\nlinux\nfixture\nboot-a\n'}
                operation = json.loads((state / 'fleet-inflight.json').read_text())['operations']['workstation']['id']
                self.assertIn('--operation-id ' + operation, argv[-1])
                receipt = {'format': 'modelhub-kit-maintenance-v1', 'version': 1, 'operationId': operation,
                           'operationTerminal': True, 'action': 'maintain', 'status': 'queued', 'modelCalls': 0,
                           'startedAt': 'start', 'completedAt': 'end', 'tools': [{'status': 'queued'}],
                           'catalogs': [], 'integrations': [], 'cache': {'status': 'absent'}}
                return {'exit': 75, 'output': json.dumps(receipt)}
            with patch.object(fleet, 'run', side_effect=child):
                result = fleet.reconcile(inventory, state, apply=True)
            self.assertEqual(result['hosts'][0]['status'], 'queued')
            self.assertTrue(result['hosts'][0]['custodyKnown'])
            self.assertEqual(json.loads((state / 'fleet-inflight.json').read_text())['operations'], {})

    def test_unbound_malformed_or_wrong_operation_receipt_is_not_terminal(self):
        operation = 'a' * 32
        good = dict(fleet._delivery_context(operation), status='queued', cleanupComplete=True, exitCode=75)
        self.assertTrue(fleet._terminal_observed(good, operation, True, 75))
        for bad in ({}, dict(good, operationId='b' * 32), dict(good, operationTerminal=False),
                    dict(good, cleanupComplete=False), dict(good, status=[]), dict(good, exitCode=True), dict(good, exitCode='75')):
            self.assertFalse(fleet._terminal_observed(bad, operation, True, 75))
        self.assertFalse(fleet._terminal_observed(good, operation, True, 0))

    def test_windows_terminal_installer_failure_clears_only_matching_exit_fence(self):
        for observed_exit in (1, 255):
            with self.subTest(observed_exit=observed_exit), tempfile.TemporaryDirectory() as folder:
                root = pathlib.Path(folder); state = root / 'state'; inventory = root / 'fleet.json'
                self.fixture(root)
                atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'windows'}]})
                def child(argv, **kwargs):
                    if 'input_data' not in kwargs:
                        return {'exit': 0, 'output': json.dumps({'os': 'windows', 'variant': None, 'boot': 'boot-a'})}
                    operation = json.loads((state / 'fleet-inflight.json').read_text())['operations']['workstation']['id']
                    receipt = dict(fleet._delivery_context(operation), status='queued', cleanupComplete=True, exitCode=1)
                    return {'exit': observed_exit, 'output': json.dumps(receipt), 'timedOut': False,
                            'inputSent': len(kwargs['input_data']), 'inputFailed': False}
                with patch.object(fleet, 'run', side_effect=child):
                    result = fleet.reconcile(inventory, state, apply=True, source=root, enroll=True)
                self.assertEqual(result['hosts'][0]['status'], 'queued')
                self.assertEqual(result['hosts'][0]['custodyKnown'], observed_exit == 1)
                operations = json.loads((state / 'fleet-inflight.json').read_text())['operations']
                self.assertEqual('workstation' in operations, observed_exit != 1)

    @unittest.skipUnless(os.name == 'posix', 'Owned Unix shell transport fixture')
    def test_actual_installer_failure_keeps_exit_and_only_safe_diagnostics(self):
        from modelhub_kit.core import run as execute_child
        cases = [(42, 'modelhub-kit-install stage=initialize category=command-failed', 'initialize'),
                 (1, 'modelhub-kit-install stage=schedule category=not-ready', 'schedule'),
                 (1, 'modelhub-kit-install stage=initialize category=command-failed extra=SECRET_PLACEHOLDER', 'installer'),
                 (75, 'modelhub-kit-install stage=install category=operation-error', None),
                 (0, 'modelhub-kit-install stage=install category=operation-error', None)]
        for code, marker, stage in cases:
            with self.subTest(code=code, stage=stage), tempfile.TemporaryDirectory() as directory:
                base = pathlib.Path(directory)
                source, home = base / 'source', base / 'home'
                source.mkdir(); home.mkdir()
                self.fixture(source, "import sys\n"
                    "sys.stderr.write('LOCAL_REMOTE_SECRET_PLACEHOLDER'*12000+'\\n')\n"
                    "sys.stderr.write(" + repr(marker + '\n') + ")\n"
                    "print('LOCAL_STDOUT_SECRET_PLACEHOLDER')\nraise SystemExit(" + str(code) + ")\n")
                inventory = base / 'fleet.json'
                atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': identity()['os']}]})
                captured = []
                def transport(argv, **kwargs):
                    result = execute_child(shlex.split(argv[-1]), env=dict(os.environ, HOME=str(home)), **kwargs)
                    captured.append(result)
                    return result
                with patch.object(fleet, 'run', side_effect=transport):
                    report = fleet.reconcile(inventory, base / 'state', apply=True, source=source, enroll=True)
                row = report['hosts'][0]
                self.assertTrue(row['custodyKnown'])
                self.assertEqual(row['status'], 'completed' if code == 0 else 'queued')
                self.assertEqual(row['exit'], 0 if code == 0 else 75)
                self.assertTrue(captured[-1]['outputTruncated'])
                self.assertLessEqual(len(captured[-1]['output']), 65536)
                if stage:
                    self.assertEqual(row['installerExit'], code)
                    self.assertEqual(row['installerDiagnostic']['stage'], stage)
                    self.assertEqual(row['reason'], 'installer-deferred')
                else:
                    self.assertNotIn('installerExit', row)
                    self.assertNotIn('installerDiagnostic', row)
                    receipt = fleet._json_response(captured[-1]['output'])
                    self.assertNotIn('installerExit', receipt)
                self.assertEqual(json.loads((base / 'state/fleet-inflight.json').read_text())['operations'], {})
                for text in [json.dumps(report), *[p.read_text() for p in (base / 'state').rglob('*.json')]]:
                    self.assertNotIn('SECRET_PLACEHOLDER', text)
                self.assertEqual(list(home.glob('.modelhub-kit-delivery.*')), [])

    def test_only_fixed_diagnostic_pairs_and_bounded_actual_exits_are_retained(self):
        good = 'modelhub-kit-install stage=initialize category=command-failed'
        for text in ('', good + ' secret=LOCAL_SECRET_PLACEHOLDER', 'prefix ' + good,
                     good + '\x00', good + 'X' * 200,
                     'modelhub-kit-install stage=arbitrary category=command-failed',
                     good + '\nmodelhub-kit-install stage=schedule category=command-failed',
                     'LOCAL_SECRET_PLACEHOLDER' * 4000 + '\n' + good):
            with self.subTest(length=len(text)):
                value = fleet._installer_failure({'installerExit': 17,
                    'installerDiagnostic': {'stage': 'LOCAL_SECRET_PLACEHOLDER'}}, text)
                self.assertEqual(value, {'installerExit': 17,
                    'installerDiagnostic': {'stage': 'installer', 'category': 'nonzero-exit'}})
        for code in (None, True, '17', [], 0, 75, 4294967296, -2147483649):
            with self.subTest(code=code):
                self.assertEqual(fleet._installer_failure({'installerExit': code}, good), {})
        for stage, category in fleet.INSTALL_FAILURES:
            self.assertEqual(fleet._installer_failure({'installerExit': -1},
                'modelhub-kit-install stage=' + stage + ' category=' + category),
                {'installerExit': -1, 'installerDiagnostic': {'stage': stage, 'category': category}})

    def test_diagnostics_never_confer_terminal_receipt_or_transport_authority(self):
        cases = [('wrong-operation', {'operationId': 'b' * 32}, {}),
                 ('nonterminal', {'operationTerminal': False}, {}),
                 ('wrong-exit', {'exitCode': 1}, {}),
                 ('timeout', {}, {'timedOut': True}),
                 ('incomplete-input', {}, {'inputFailed': True})]
        for label, edits, transport_edits in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as directory:
                root = pathlib.Path(directory); state = root / 'state'; inventory = root / 'fleet.json'
                self.fixture(root)
                atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'}]})
                def child(argv, **kwargs):
                    if 'input_data' not in kwargs:
                        return {'exit': 0, 'output': 'modelhub-kit-probe-v1\nlinux\nfixture\nboot-a\n'}
                    operation = json.loads((state / 'fleet-inflight.json').read_text())['operations']['workstation']['id']
                    receipt = dict(fleet._delivery_context(operation), status='queued', cleanupComplete=True,
                                   exitCode=75, installerExit=17)
                    receipt.update(edits)
                    result = {'exit': 75, 'output': 'modelhub-kit-install stage=install category=operation-error\n' + json.dumps(receipt),
                              'timedOut': False, 'inputSent': len(kwargs['input_data']), 'inputFailed': False}
                    result.update(transport_edits)
                    return result
                with patch.object(fleet, 'run', side_effect=child):
                    report = fleet.reconcile(inventory, state, apply=True, source=root, enroll=True)
                row = report['hosts'][0]
                self.assertFalse(row['custodyKnown'])
                self.assertNotIn('installerExit', row)
                self.assertNotIn('installerDiagnostic', row)
                self.assertIn('workstation', json.loads((state / 'fleet-inflight.json').read_text())['operations'])

    def test_disconnect_preserves_fence_and_existing_writer_blocks_new_observer(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder); state = root / 'state'; inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'}]})
            with fleet.writer(state / 'fleet-operations'), patch.object(fleet, 'run') as child:
                with self.assertRaisesRegex(GuardError, 'writer-busy'):
                    fleet.reconcile(inventory, state, apply=True)
                child.assert_not_called()
            with patch.object(fleet, 'run', side_effect=[{'exit': 0, 'output': 'modelhub-kit-probe-v1\nlinux\nfixture\nboot-a\n'}, OSError('fixture disconnect')]):
                fleet.reconcile(inventory, state, apply=True)
            self.assertIn('workstation', json.loads((state / 'fleet-inflight.json').read_text())['operations'])

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
            command = fleet._unix_delivery(observed['os'], observed['boot'], metadata, False, False, 'a' * 32)
            child = subprocess.Popen(shlex.split(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=dict(os.environ, HOME=str(home)))
            try:
                child.stdin.write(payload); child.stdin.flush()
                # Deliberately keep stdin open. The receiver must consume its exact length.
                child.wait(timeout=15)
                output, errors = child.stdout.read(), child.stderr.read()
                self.assertEqual(child.returncode, 0, errors.decode())
                self.assertEqual(json.loads(output)['status'], 'installed-or-current')
                self.assertEqual(json.loads(output)['operationId'], 'a' * 32)
                self.assertTrue(json.loads(output)['operationTerminal'])
                self.assertEqual(json.loads(output)['exitCode'], 0)
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
            for index, response in enumerate(cases):
                with self.subTest(response=response), patch.object(fleet, 'run', side_effect=[
                    {'exit': 0, 'output': 'modelhub-kit-probe-v1\nlinux\narch\nboot-a\n'},
                    {'exit': 0, 'output': json.dumps(response)}]):
                    result = fleet.reconcile(inventory, root / ('state-' + str(index)), apply=True)
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
            command, prefix = fleet._windows_delivery('windows', 'boot-a', metadata, True, True, 'a' * 32)
            self.assertLess(len(command), 7500)
            loader = base64.b64decode(command.split()[-1]).decode('utf-16le')
            script = gzip.decompress(prefix).decode('utf-8')
            self.assertIn("$operationId='" + 'a' * 32 + "'", script)
            self.assertIn('operationTerminal=$cleanupOk', script)
            self.assertIn('exitCode=$result', script)
            self.assertIn('$installerExit=$null', script)
            self.assertIn("$ErrorActionPreference='Continue';$LASTEXITCODE=$null", script)
            self.assertIn('if($null -ne $LASTEXITCODE){$result=$LASTEXITCODE;$installerExit=$result}', script)
            self.assertIn('finally {$ErrorActionPreference=$savedErrorAction}', script)
            self.assertIn("if($null -eq $installerExit){throw 'installer-unavailable'}", script)
            self.assertIn("$result=75;$installerExit=$null;$reason='delivery-unavailable'", script)
            self.assertIn('if($null -ne $installerExit -and $installerExit -notin @(0,75))', script)
            invocation = next(line for line in script.splitlines() if '& powershell.exe ' in line)
            self.assertTrue(invocation.endswith(' > $null'))
            self.assertNotIn('*>', invocation)
            self.assertLess(script.index("throw 'installer-unavailable'"), script.index('} catch {'))
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
                operation = json.loads((root / 'state/fleet-inflight.json').read_text())['operations']['workstation']['id']
                bound_command, bound_prefix = fleet._windows_delivery('windows', 'boot-a', metadata, False, False, operation)
                self.assertEqual(argv[-1], bound_command)
                self.assertEqual(kwargs['input_data'], bound_prefix + payload)
                receipt = dict(fleet._delivery_context(operation), status='installed-or-current', cleanupComplete=True, exitCode=0)
                return {'exit': 0, 'output': json.dumps(receipt),
                        'inputSent': len(bound_prefix) + len(payload), 'inputFailed': False}
            with patch.object(fleet, 'run', side_effect=transport):
                result = fleet.reconcile(inventory, root / 'state', apply=True, source=root, enroll=True)
            self.assertEqual(result['hosts'][0]['status'], 'completed')
            self.assertEqual(json.loads((root / 'state/fleet-inflight.json').read_text())['operations'], {})

    def test_default_maintain_remains_compatible_and_binds_observed_boot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            inventory = root / 'fleet.json'
            atomic_json(inventory, {'version': 1, 'hosts': [{'alias': 'workstation', 'os': 'linux'}]})
            def response(argv, **kwargs):
                if '--operation-id' not in argv[-1]:
                    return {'exit': 0, 'timedOut': False, 'output': 'modelhub-kit-probe-v1\nlinux\narch\nboot-a\n'}
                operation = json.loads((root / 'state/fleet-inflight.json').read_text())['operations']['workstation']['id']
                value = {'format': 'modelhub-kit-maintenance-v1', 'action': 'maintain', 'operationId': operation,
                         'operationTerminal': True, 'status': 'completed', 'version': 1, 'modelCalls': 0,
                         'startedAt': 'start', 'completedAt': 'end', 'tools': [], 'catalogs': [], 'integrations': [], 'cache': {'status': 'absent'}}
                return {'exit': 0, 'timedOut': False, 'output': json.dumps(value)}
            with patch.object(fleet, 'run', side_effect=response) as execute:
                result = fleet.reconcile(inventory, root / 'state', apply=True)
            self.assertEqual(result['hosts'][0]['status'], 'completed')
            self.assertIn('--expected-boot boot-a --operation-id ', execute.call_args_list[1].args[0][-1])
            self.assertNotIn('input_data', execute.call_args_list[1].kwargs)


if __name__ == '__main__':
    unittest.main()
