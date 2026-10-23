import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from modelhub_kit import core, updater


@unittest.skipIf(os.name == 'nt', 'Unix runtime custody')
class RuntimeTrustTests(unittest.TestCase):
    def fixture(self):
        directory = tempfile.TemporaryDirectory(prefix='.runtime-fixture-', dir=pathlib.Path.cwd())
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        binary = root / 'runtime/bin/node'
        binary.parent.mkdir(parents=True)
        binary.write_text('fixture only; must never execute')
        binary.chmod(0o755)
        npm = binary.with_name('npm-cli.js')
        npm.write_text('fixture only; must never execute')
        npm.chmod(0o755)
        return root, binary, npm

    def test_protected_selection_symlink_is_resolved_without_a_probe(self):
        root, binary, _ = self.fixture()
        selection = root / 'selected'
        selection.symlink_to(binary)
        self.assertEqual(core.protected_runtime_path(selection), binary)

    def test_unsafe_runtime_or_parent_never_receives_a_version_probe(self):
        root, binary, npm = self.fixture()
        actual = pathlib.Path.lstat
        for unsafe in (binary, binary.parent, binary.parent.parent):
            for failure in ('owner', 'mode'):
                with self.subTest(unsafe=unsafe.name, failure=failure):
                    def observed(path, *args, **kwargs):
                        info = actual(path, *args, **kwargs)
                        if path == unsafe:
                            values = list(info)
                            if failure == 'owner':
                                values[4] = max(os.geteuid(), 1) + 1
                            else:
                                values[0] |= 0o020
                            return os.stat_result(values)
                        return info
                    with patch.object(pathlib.Path, 'lstat', observed), \
                         patch.object(updater.shutil, 'which', side_effect=lambda name: str(binary if name == 'node' else npm)), \
                         patch.object(updater, 'run') as execute:
                        with self.assertRaisesRegex(core.GuardError, 'runtime-custody-unverified'):
                            updater.npm_command()
                        execute.assert_not_called()
        self.assertTrue(binary.exists())

    def test_unsafe_npm_selection_is_checked_before_the_node_probe(self):
        _, binary, npm = self.fixture()
        npm.chmod(0o775)
        with patch.object(updater.shutil, 'which', side_effect=lambda name: str(binary if name == 'node' else npm)), \
             patch.object(updater, 'run') as execute:
            with self.assertRaisesRegex(core.GuardError, 'runtime-custody-unverified'):
                updater.npm_command()
            execute.assert_not_called()

    def test_current_protected_runtime_is_used_without_installing(self):
        _, binary, npm = self.fixture()
        with patch.object(updater.shutil, 'which', side_effect=lambda name: str(binary if name == 'node' else npm)), \
             patch.object(updater, 'run', return_value={'exit': 0, 'output': 'v24.22.1\n'}) as execute:
            self.assertEqual(updater.npm_command(), [str(binary), str(npm)])
            execute.assert_called_once_with([str(binary), '--version'], timeout=10)


if __name__ == '__main__':
    unittest.main()
