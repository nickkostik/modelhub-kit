import errno
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('atomic_write_fixture_installer', ROOT / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerAtomicWriteTests(unittest.TestCase):
    def test_write_flush_and_fsync_enospc_preserve_original_and_remove_temporary(self):
        real_temporary = installer.tempfile.NamedTemporaryFile
        for phase in ('write', 'flush', 'fsync'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                target = root / 'module.py'
                target.write_bytes(b'original code')
                before = installer.snapshot(target)
                failure = OSError(errno.ENOSPC, 'injected storage failure')
                created = []

                def temporary(*args, **kwargs):
                    stream = real_temporary(*args, **kwargs)
                    created.append(stream)
                    if phase == 'write':
                        write = stream.write
                        def partial_write(data):
                            write(data[:3])
                            raise failure
                        stream.write = partial_write
                    elif phase == 'flush':
                        stream.flush = lambda: (_ for _ in ()).throw(failure)
                    return stream

                with patch.object(installer.tempfile, 'NamedTemporaryFile', side_effect=temporary), \
                        patch.object(installer.os, 'fsync', side_effect=failure if phase == 'fsync' else None):
                    with self.assertRaises(OSError) as observed:
                        installer.atomic_write(target, b'new code', expected=before)
                self.assertIs(observed.exception, failure)
                self.assertEqual(observed.exception.errno, errno.ENOSPC)
                self.assertEqual(installer.snapshot(target), before)
                self.assertEqual(len(created), 1)
                self.assertTrue(created[0].file.closed)
                self.assertFalse(Path(created[0].name).exists())
                self.assertEqual(list(root.iterdir()), [target])

    def test_failed_temporary_creation_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'module.py'
            target.write_bytes(b'original code')
            before = installer.snapshot(target)
            failure = OSError(errno.ENOSPC, 'injected creation failure')
            with patch.object(installer.tempfile, 'NamedTemporaryFile', side_effect=failure):
                with self.assertRaises(OSError) as observed:
                    installer.atomic_write(target, b'new code')
            self.assertIs(observed.exception, failure)
            self.assertEqual(installer.snapshot(target), before)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_cleanup_failure_does_not_mask_original_storage_error(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'module.py'
            target.write_bytes(b'original code')
            before = installer.snapshot(target)
            failure = OSError(errno.ENOSPC, 'injected sync failure')
            cleanup = PermissionError(errno.EACCES, 'injected cleanup failure')
            with patch.object(installer.os, 'fsync', side_effect=failure), \
                    patch.object(Path, 'unlink', side_effect=cleanup) as unlink:
                with self.assertRaises(OSError) as observed:
                    installer.atomic_write(target, b'new code')
            self.assertIs(observed.exception, failure)
            self.assertEqual(installer.snapshot(target), before)
            self.assertEqual(unlink.call_count, 1)
            # Cleanup was genuinely denied; remove only this fixture's residue.
            for temporary in root.iterdir():
                if temporary != target:
                    temporary.unlink()

    def test_standalone_cleanup_failure_is_not_suppressed(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'module.py'
            target.write_bytes(b'original code')
            cleanup = PermissionError(errno.EACCES, 'injected cleanup failure')
            with patch.object(Path, 'unlink', side_effect=cleanup):
                with self.assertRaises(PermissionError) as observed:
                    installer.atomic_write(target, b'new code')
            self.assertIs(observed.exception, cleanup)
            self.assertEqual(target.read_bytes(), b'new code')

    def test_promotion_is_idempotent_and_retains_requested_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'module.py'
            target.write_bytes(b'original code')
            self.assertTrue(installer.atomic_write(target, b'new code', 0o600))
            after = installer.snapshot(target)
            self.assertEqual(target.read_bytes(), b'new code')
            if os.name != 'nt':
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertFalse(installer.atomic_write(target, b'new code', 0o600))
            self.assertEqual(installer.snapshot(target), after)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_concurrent_target_change_still_blocks_promotion_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'module.py'
            target.write_bytes(b'original code')
            sync = installer.os.fsync
            def changed(fd):
                sync(fd)
                target.write_bytes(b'concurrent custom code')
            with patch.object(installer.os, 'fsync', side_effect=changed):
                with self.assertRaisesRegex(ValueError, 'concurrently changed'):
                    installer.atomic_write(target, b'new code')
            self.assertEqual(target.read_bytes(), b'concurrent custom code')
            self.assertEqual(list(target.parent.iterdir()), [target])


if __name__ == '__main__':
    unittest.main()
