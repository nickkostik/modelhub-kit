import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from modelhub_kit.native_helpers import codex_runtime


def receipt(cli, server, managed=None):
    return types.SimpleNamespace(
        returncode=0,
        stdout=json.dumps({
            "cliVersion": cli,
            "appServerVersion": server,
            "managedCodexVersion": managed or cli,
        }),
    )


class CodexRuntimeTests(unittest.TestCase):
    def fixture(self, root):
        home = Path(root)
        binary = home / ".local/bin/codex"
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        control = home / ".codex/app-server-control/app-server-control.sock"
        control.parent.mkdir(parents=True)
        control.write_text("fixture")
        return home, binary, control

    def test_current_cli_and_server_do_not_restart(self):
        with tempfile.TemporaryDirectory() as root:
            home, binary, _ = self.fixture(root)
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                return receipt("1.2.3", "1.2.3")
            with patch.object(codex_runtime, "protected_runtime_path", side_effect=Path):
                result = codex_runtime.reconcile_codex_runtime(home=home, cli=binary, run=run)
            self.assertEqual(result["status"], "current")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1:], ["app-server", "daemon", "version"])

    def test_stale_server_updates_only_after_two_idle_checks(self):
        with tempfile.TemporaryDirectory() as root:
            home, binary, control = self.fixture(root)
            responses = iter([
                receipt("2.0.0", "1.0.0", "1.0.0"),
                types.SimpleNamespace(returncode=0),
                receipt("2.0.0", "2.0.0"),
            ])
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                return next(responses)
            class Daemon:
                checks = 0
                instances = []
                def __init__(self, path):
                    self.path = path
                    self.checks = 0
                    self.instances.append(self)
                def active_threads(self):
                    self.checks += 1
                    return 0
                def close(self):
                    self.closed = True
            with patch.object(codex_runtime, "protected_runtime_path", side_effect=Path), \
                    patch.object(codex_runtime, "LocalDaemon", Daemon):
                result = codex_runtime.reconcile_codex_runtime(home=home, cli=binary, run=run)
            self.assertEqual(result, {
                "status": "updated", "cliVersion": "2.0.0", "appServerVersion": "2.0.0",
            })
            self.assertEqual(Daemon.instances[0].checks, 2)
            self.assertEqual(calls[1][1:], ["app-server", "daemon", "update", "--from-cli", "--yes"])

    def test_same_package_restarts_an_older_server(self):
        with tempfile.TemporaryDirectory() as root:
            home, binary, _ = self.fixture(root)
            responses = iter([
                receipt("2.0.0", "1.0.0", "2.0.0"),
                types.SimpleNamespace(returncode=0),
                receipt("2.0.0", "2.0.0", "2.0.0"),
            ])
            calls = []
            def run(argv, **kwargs):
                calls.append(argv)
                return next(responses)
            class Daemon:
                def __init__(self, path): pass
                def active_threads(self): return 0
                def close(self): pass
            with patch.object(codex_runtime, "protected_runtime_path", side_effect=Path), \
                    patch.object(codex_runtime, "LocalDaemon", Daemon):
                result = codex_runtime.reconcile_codex_runtime(home=home, cli=binary, run=run)
            self.assertEqual(result["status"], "updated")
            self.assertEqual(calls[1][1:], ["app-server", "daemon", "restart"])

    def test_active_or_unknown_turns_queue_without_mutation(self):
        for active in (1, RuntimeError("unreadable")):
            with self.subTest(active=active), tempfile.TemporaryDirectory() as root:
                home, binary, _ = self.fixture(root)
                calls = []
                def run(argv, **kwargs):
                    calls.append(argv)
                    return receipt("2.0.0", "1.0.0")
                class Daemon:
                    def __init__(self, path): pass
                    def active_threads(self):
                        if isinstance(active, Exception):
                            raise active
                        return active
                    def close(self): pass
                with patch.object(codex_runtime, "protected_runtime_path", side_effect=Path), \
                        patch.object(codex_runtime, "LocalDaemon", Daemon):
                    result = codex_runtime.reconcile_codex_runtime(home=home, cli=binary, run=run)
                self.assertEqual(result["status"], "queued-active-turns" if active == 1
                                 else "queued-runtime-unavailable")
                self.assertEqual(len(calls), 1)

    def test_lifecycle_failure_or_mismatch_is_queued(self):
        cases = [
            (types.SimpleNamespace(returncode=75), \
             "queued-runtime-update-failed"),
            (types.SimpleNamespace(returncode=0), \
             "queued-runtime-version-mismatch"),
        ]
        for mutation, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as root:
                home, binary, _ = self.fixture(root)
                responses = iter([
                    receipt("2.0.0", "1.0.0"),
                    mutation,
                    receipt("2.0.0", "1.0.0"),
                ])
                def run(argv, **kwargs):
                    return next(responses)
                class Daemon:
                    def __init__(self, path): pass
                    def active_threads(self): return 0
                    def close(self): pass
                with patch.object(codex_runtime, "protected_runtime_path", side_effect=Path), \
                        patch.object(codex_runtime, "LocalDaemon", Daemon):
                    result = codex_runtime.reconcile_codex_runtime(home=home, cli=binary, run=run)
                self.assertEqual(result["status"], expected)


if __name__ == "__main__":
    unittest.main()
