"""Local owned-release fixtures only; no executable, installer or network calls."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit.native_helpers import omp


class OmpRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / 'home'
        self.root = self.home / '.local/share/modelhub-omp-releases'
        self.root.mkdir(parents=True)
        self.current = self.release('9.0.0')
        self.launcher = self.home / '.local/bin/omp'
        self.launcher.parent.mkdir()
        self.launcher.symlink_to(self.current)

    def release(self, version):
        folder = self.root / version
        folder.mkdir()
        binary = folder / 'omp'
        binary.write_bytes(b'native-fixture')
        binary.chmod(0o700)
        omp.write_receipt(binary, dict(
            version=version, asset='omp-darwin-arm64', size=binary.stat().st_size,
            sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            metadataUrl='https://example.invalid/releases'))
        return binary

    def retire(self, live=()):
        with patch.object(omp, 'active_binaries', return_value=set(live)):
            return omp.retire_idle(self.root, self.current, launcher=self.launcher)

    def test_unrelated_entries_do_not_starve_owned_old_releases(self):
        for number in range(65):
            (self.root / ('unrelated-' + str(number))).write_text('preserve')
        old = self.release('1.0.0')
        real_scan = os.scandir

        @contextmanager
        def old_last(path):
            with real_scan(path) as entries:
                rows = list(entries)
            if Path(path) == self.root:
                rows.sort(key=lambda entry: entry.name == old.parent.name)
            yield iter(rows)

        with patch.object(omp.os, 'scandir', side_effect=old_last):
            result = self.retire()
        self.assertEqual(result['retiredReleases'], 1)
        self.assertFalse(old.parent.exists())
        self.assertTrue(self.current.exists())
        self.assertEqual(len(list(self.root.glob('unrelated-*'))), 65)

    def test_live_and_unowned_versions_do_not_consume_retirement_budget(self):
        live = self.release('1.0.0')
        for number in range(65):
            (self.root / ('0.0.' + str(number))).mkdir()
        old = [self.release(version) for version in ('2.0.0', '3.0.0', '4.0.0')]
        newer = self.release('10.0.0')
        with patch.object(omp, 'MAX_RETIREMENT_CHECKS', 2):
            first = self.retire([str(live)])
            self.assertEqual(first['retiredReleases'], 2)
            self.assertTrue(first['retirementDeferred'])
            self.assertEqual(first['retirementReason'], 'native-retirement-release-budget')
            self.assertFalse(old[0].exists())
            self.assertFalse(old[1].exists())
            self.assertTrue(old[2].exists())
            second = self.retire([str(live)])
        self.assertEqual(second['retiredReleases'], 1)
        self.assertFalse(second['retirementDeferred'])
        self.assertTrue(live.exists())
        self.assertTrue(newer.exists())
        self.assertTrue(self.current.exists())
        self.assertEqual(len(list(self.root.glob('0.0.*'))), 65)

    def test_incomplete_enumeration_preserves_every_release_with_bounded_reads(self):
        old = [self.release(version) for version in ('1.0.0', '2.0.0', '3.0.0')]
        real_scan = os.scandir
        visited = []

        @contextmanager
        def counted(path):
            with real_scan(path) as entries:
                def rows():
                    for entry in entries:
                        visited.append(entry.name)
                        yield entry
                yield rows()

        with patch.object(omp, 'MAX_RETIREMENT_ENTRIES', 2), \
                patch.object(omp.os, 'scandir', side_effect=counted):
            result = self.retire()
        self.assertEqual(len(visited), 3)
        self.assertEqual(result['retiredReleases'], 0)
        self.assertTrue(result['retirementDeferred'])
        self.assertEqual(result['retirementReason'], 'native-retirement-enumeration-incomplete')
        self.assertTrue(all(binary.exists() for binary in old))

    def test_enumeration_error_before_completion_cannot_start_deletion(self):
        old = self.release('1.0.0')
        real_scan = os.scandir

        @contextmanager
        def incomplete(path):
            with real_scan(path) as entries:
                row = next(entry for entry in entries if entry.name == old.parent.name)
            def rows():
                yield row
                raise PermissionError('fixture')
            yield rows()

        with patch.object(omp.os, 'scandir', side_effect=incomplete):
            result = self.retire()
        self.assertEqual(result['retiredReleases'], 0)
        self.assertTrue(result['retirementDeferred'])
        self.assertTrue(old.exists())

    def test_fresh_inventory_failure_stops_pass_instead_of_repeated_probes(self):
        old = [self.release(version) for version in ('1.0.0', '2.0.0', '3.0.0')]
        with patch.object(omp, 'active_binaries', side_effect=[set(), None]) as inventory:
            result = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertEqual(inventory.call_count, 2)
        self.assertEqual(result['retiredReleases'], 0)
        self.assertEqual(result['retirementReason'], 'native-retirement-process-inventory-unavailable')
        self.assertTrue(all(binary.exists() for binary in old))

    def test_late_active_candidates_have_bounded_fresh_process_checks(self):
        old = [self.release(version) for version in ('1.0.0', '2.0.0', '3.0.0')]
        calls = []
        def inventory(_root, **_kwargs):
            calls.append(None)
            return {str(binary) for binary in old[:len(calls) - 1]}
        with patch.object(omp, 'MAX_RETIREMENT_CHECKS', 2), \
                patch.object(omp, 'active_binaries', side_effect=inventory):
            result = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['retiredReleases'], 0)
        self.assertTrue(result['retirementDeferred'])
        self.assertTrue(all(binary.exists() for binary in old))

    def test_modified_unknown_linked_and_newly_active_releases_are_preserved(self):
        modified = self.release('1.0.0')
        modified.write_bytes(b'modified-fixture')
        unknown = self.release('2.0.0')
        (unknown.parent / 'custom-data').write_text('preserve')
        linked = self.release('3.0.0')
        os.link(linked, self.root / 'shared-runtime')
        active = self.release('4.0.0')
        with patch.object(omp, 'active_binaries', side_effect=[set(), {str(active)}]):
            result = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertEqual(result['retiredReleases'], 0)
        self.assertEqual(result['retainedLiveReleases'], 1)
        self.assertTrue(all(binary.exists() for binary in (modified, unknown, linked, active)))
        self.assertEqual((unknown.parent / 'custom-data').read_text(), 'preserve')

    def test_default_pass_retires_at_most_eight_and_next_pass_continues(self):
        old = [self.release('1.0.' + str(number)) for number in range(9)]
        with patch.object(omp, 'active_binaries', return_value=set()) as inventory:
            first = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertEqual(first['retiredReleases'], 8)
        self.assertEqual(inventory.call_count, 9)  # Initial view plus eight fresh checks.
        self.assertEqual(first['retirementReason'], 'native-retirement-release-budget')
        self.assertTrue(old[-1].exists())
        second = self.retire()
        self.assertEqual(second['retiredReleases'], 1)
        self.assertFalse(second['retirementDeferred'])

    def test_byte_budget_admits_a_full_candidate_then_queues_without_hashing(self):
        live = self.release('1.0.0')
        unowned = self.root / '2.0.0'
        unowned.mkdir()
        (unowned / 'omp').write_bytes(b'unowned-fixture')
        old = [self.release(version) for version in ('3.0.0', '4.0.0')]
        full_binary_size = old[0].stat().st_size
        self.assertGreaterEqual(omp.MAX_RETIREMENT_BYTES, omp.MAX_BINARY)
        with patch.object(omp, 'MAX_RETIREMENT_BYTES', full_binary_size), \
                patch.object(omp, 'digest', side_effect=AssertionError('Retirement must not hash binaries')), \
                patch.object(omp, 'active_binaries', return_value={str(live)}) as inventory:
            first = omp.retire_idle(self.root, self.current, launcher=self.launcher)
            self.assertEqual(inventory.call_count, 2)
            self.assertEqual(first['retiredReleases'], 1)
            self.assertEqual(first['retirementReason'], 'native-retirement-byte-budget')
            self.assertFalse(old[0].exists())
            self.assertTrue(old[1].exists())
            second = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertEqual(second['retiredReleases'], 1)
        self.assertFalse(second['retirementDeferred'])
        self.assertTrue(live.exists())
        self.assertTrue((unowned / 'omp').exists())

    def test_process_timeout_uses_remaining_wall_time_and_defers_before_deletion(self):
        old = self.release('1.0.0')
        clock = [0]
        timeouts = []
        def inventory(_root, timeout):
            timeouts.append(timeout)
            clock[0] += 8.75 if len(timeouts) == 1 else timeout
            return set()
        with patch.object(omp.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(omp, 'active_binaries', side_effect=inventory):
            result = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertEqual(timeouts, [2, 1.25])
        self.assertEqual(result['retiredReleases'], 0)
        self.assertEqual(result['retirementReason'], 'native-retirement-time-budget')
        self.assertTrue(old.exists())

    def test_wall_time_budget_keeps_completed_progress_for_next_pass(self):
        old = [self.release(version) for version in ('1.0.0', '2.0.0', '3.0.0')]
        clock = [0]
        calls = []
        def inventory(_root, **_kwargs):
            calls.append(None)
            clock[0] = {1: 0, 2: 1, 3: 10}[len(calls)]
            return set()
        with patch.object(omp.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(omp, 'active_binaries', side_effect=inventory):
            result = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['retiredReleases'], 1)
        self.assertEqual(result['retirementReason'], 'native-retirement-time-budget')
        self.assertFalse(old[0].exists())
        self.assertTrue(old[1].exists())
        self.assertTrue(old[2].exists())
        self.assertEqual(self.retire()['retiredReleases'], 2)

    def test_metadata_that_exhausts_wall_budget_cannot_start_fresh_probe(self):
        old = self.release('1.0.0')
        clock = [0]
        matches = omp.receipt_matches
        def slow_metadata(*args, **kwargs):
            proven = matches(*args, **kwargs)
            clock[0] = omp.MAX_RETIREMENT_SECONDS
            return proven
        with patch.object(omp.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(omp, 'receipt_matches', side_effect=slow_metadata), \
                patch.object(omp, 'active_binaries', return_value=set()) as inventory:
            result = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertEqual(inventory.call_count, 1)
        self.assertEqual(result['retirementReason'], 'native-retirement-time-budget')
        self.assertTrue(old.exists())

    def test_launcher_reselected_during_fresh_inventory_preserves_selected_binary(self):
        old = self.release('1.0.0')
        calls = []
        def inventory(_root, **_kwargs):
            calls.append(None)
            if len(calls) == 2:
                self.launcher.unlink()
                self.launcher.symlink_to(old)
            return set()
        with patch.object(omp, 'active_binaries', side_effect=inventory):
            result = omp.retire_idle(self.root, self.current, launcher=self.launcher)
        self.assertTrue(old.exists(), 'The newly selected version must remain usable')
        self.assertEqual(self.launcher.resolve(), old)
        self.assertEqual(result['retiredReleases'], 0)
        self.assertEqual(result['retirementReason'], 'native-retirement-launcher-changed')

    def test_launcher_reselected_after_process_check_preserves_binary_and_receipt(self):
        old = self.release('1.0.0')
        receipt = old.parent / omp.RECEIPT
        original_owned = omp.owned
        checks = []
        def owned(path, directory=False):
            if path == receipt:
                checks.append(None)
                if len(checks) == 2:  # Final metadata check after fresh process visibility.
                    self.launcher.unlink()
                    self.launcher.symlink_to(old)
            return original_owned(path, directory)
        with patch.object(omp, 'owned', side_effect=owned):
            result = self.retire()
        self.assertEqual(len(checks), 2)
        self.assertTrue(old.exists())
        self.assertTrue(receipt.exists())
        self.assertEqual(self.launcher.resolve(), old)
        self.assertEqual(result['retiredReleases'], 0)
        self.assertEqual(result['retirementReason'], 'native-retirement-launcher-changed')

    def test_missing_or_unrecognized_selection_queues_before_process_probes(self):
        old = self.release('1.0.0')
        for target in (None, old, self.home / 'custom-omp'):
            with self.subTest(target=target):
                if self.launcher.is_symlink():
                    self.launcher.unlink()
                if target is not None:
                    self.launcher.symlink_to(target)
                with patch.object(omp, 'active_binaries', side_effect=AssertionError('No selection proof')):
                    result = omp.retire_idle(self.root, self.current, launcher=self.launcher)
                self.assertTrue(old.exists())
                self.assertTrue(self.current.exists())
                self.assertEqual(result['retiredReleases'], 0)
                self.assertEqual(result['retirementReason'], 'native-retirement-launcher-unavailable')


if __name__ == '__main__':
    unittest.main()
