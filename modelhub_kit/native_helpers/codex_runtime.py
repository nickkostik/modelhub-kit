"""Keep the installed Codex CLI and its running app-server on one release.

The package update and the long-lived app-server are separate runtimes. A
current npm package therefore does not prove that the server serving native
models and capabilities is current. This adapter reads the daemon's loaded-turn
state over its local control socket and performs only an idle, verified update.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import struct
import subprocess
import time

from ..core import GuardError, protected_runtime_path


class LocalDaemon:
    """Minimal read-only app-server control client; never performs inference."""

    def __init__(self, path: Path, timeout: int = 3):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(timeout)
        self.buffer = b""
        self.request_id = 0
        try:
            self.socket.connect(str(path))
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall((
                "GET / HTTP/1.1\r\nHost: localhost\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode())
            while b"\r\n\r\n" not in self.buffer:
                self.buffer += self._receive()
                if len(self.buffer) > 16384:
                    raise ValueError("invalid-control-handshake")
            head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()).decode()
            headers = {}
            for line in head.decode().split("\r\n")[1:]:
                name, separator, value = line.partition(":")
                if separator:
                    headers[name.lower()] = value.strip()
            if not head.startswith(b"HTTP/1.1 101 ") or headers.get("sec-websocket-accept") != expected:
                raise ValueError("refused-control-handshake")
            self.request("initialize", {
                "clientInfo": {"name": "modelhub-kit", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            self._send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def close(self):
        self.socket.close()

    def _receive(self):
        value = self.socket.recv(65536)
        if not value:
            raise ConnectionError("control-socket-closed")
        return value

    def _exact(self, size):
        while len(self.buffer) < size:
            self.buffer += self._receive()
        value, self.buffer = self.buffer[:size], self.buffer[size:]
        return value

    def _send_frame(self, data, opcode=1):
        mask = os.urandom(4)
        size = len(data)
        header = bytes([128 | opcode, 128 | size]) if size < 126 else (
            bytes([128 | opcode, 254]) + struct.pack("!H", size)
            if size < 65536
            else bytes([128 | opcode, 255]) + struct.pack("!Q", size)
        )
        self.socket.sendall(header + mask + bytes(
            value ^ mask[index % 4] for index, value in enumerate(data)
        ))

    def _send(self, value):
        self._send_frame(json.dumps(value, separators=(",", ":")).encode())

    def _message(self):
        fragments = bytearray()
        while True:
            first, second = self._exact(2)
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self._exact(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._exact(8))[0]
            if size + len(fragments) > 16 * 1024 * 1024:
                raise ValueError("control-message-too-large")
            mask = self._exact(4) if second & 128 else None
            data = self._exact(size)
            if mask:
                data = bytes(value ^ mask[index % 4] for index, value in enumerate(data))
            opcode = first & 15
            if opcode == 8:
                raise ConnectionError("control-socket-closed")
            if opcode == 9:
                self._send_frame(data, 10)
            elif opcode in (0, 1):
                fragments.extend(data)
                if first & 128:
                    return json.loads(fragments)

    def request(self, method, params):
        self.request_id += 1
        request_id = self.request_id
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.socket.gettimeout()
        while time.monotonic() < deadline:
            response = self._message()
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError("local-daemon-rpc-failed")
            return response["result"]
        raise TimeoutError("local-daemon-rpc-timeout")

    def active_threads(self):
        identifiers, cursor = [], None
        while True:
            result = self.request("thread/loaded/list", {
                "limit": 1000,
                "cursor": cursor,
            })
            identifiers.extend(result.get("data", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        active = 0
        for thread_id in identifiers:
            result = self.request("thread/read", {
                "threadId": thread_id,
                "includeTurns": False,
            })
            status = result.get("thread", {}).get("status", {}).get("type")
            if status != "idle":
                active += 1
        return active


def _version(value):
    return isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", value)


def _newer(left, right):
    if not _version(left) or not _version(right):
        return False
    parse = lambda value: tuple(
        int(part) for part in re.split(r"[.+-]", value)[:3]
    )
    return parse(left) > parse(right)


def _daemon_receipt(binary, environment, run):
    result = run(
        [str(binary), "app-server", "daemon", "version"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        universal_newlines=True,
        timeout=15,
    )
    if result.returncode:
        raise ValueError("codex-runtime-version-unavailable")
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise ValueError("codex-runtime-version-unavailable")
    return value


def reconcile_codex_runtime(*, home=None, cli=None, run=subprocess.run):
    """Update one stale app-server only after a fresh idle proof."""
    home = Path(home or os.environ.get("HOME") or Path.home()).resolve(strict=True)
    environment = dict(os.environ, HOME=str(home))
    codex_home = Path(environment.get("CODEX_HOME", home / ".codex")).resolve()
    environment["CODEX_HOME"] = str(codex_home)
    binary = Path(cli or shutil.which("codex") or home / ".local/bin/codex")
    if not binary.exists():
        return {"status": "not-installed"}
    binary = protected_runtime_path(binary)
    control = codex_home / "app-server-control/app-server-control.sock"
    try:
        version = _daemon_receipt(binary, environment, run)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return {"status": "not-running"}
    cli_version = version.get("cliVersion")
    server_version = version.get("appServerVersion")
    managed_version = version.get("managedCodexVersion")
    if not _version(cli_version) or not _version(server_version):
        return {"status": "queued-runtime-version-unverified"}
    if cli_version == server_version:
        return {"status": "current", "cliVersion": cli_version, "appServerVersion": server_version}
    if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
        return {"status": "queued-runtime-idle-check-unavailable",
                "cliVersion": cli_version, "appServerVersion": server_version}
    if not control.exists():
        return {"status": "queued-runtime-control-unavailable",
                "cliVersion": cli_version, "appServerVersion": server_version}
    try:
        daemon = LocalDaemon(control)
    except (OSError, ValueError, ConnectionError):
        return {"status": "queued-runtime-control-unavailable",
                "cliVersion": cli_version, "appServerVersion": server_version}
    try:
        if daemon.active_threads():
            return {"status": "queued-active-turns",
                    "cliVersion": cli_version, "appServerVersion": server_version}
        # Recheck immediately before the process mutation. An approval or new
        # turn can begin while the first RPC response is being decoded.
        if daemon.active_threads():
            return {"status": "queued-active-turns",
                    "cliVersion": cli_version, "appServerVersion": server_version}
        action = "update" if _newer(cli_version, managed_version) else "restart"
        arguments = [action, "--from-cli", "--yes"] if action == "update" else ["restart"]
        result = run(
            [str(binary), "app-server", "daemon", *arguments],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
        if result.returncode:
            return {"status": "queued-runtime-update-failed",
                    "cliVersion": cli_version, "appServerVersion": server_version}
    except (OSError, ValueError, RuntimeError, ConnectionError, subprocess.SubprocessError):
        return {"status": "queued-runtime-unavailable",
                "cliVersion": cli_version, "appServerVersion": server_version}
    finally:
        daemon.close()
    try:
        updated = _daemon_receipt(binary, environment, run)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return {"status": "queued-runtime-verification-unavailable",
                "cliVersion": cli_version, "appServerVersion": server_version}
    if updated.get("appServerVersion") != cli_version or updated.get("cliVersion") != cli_version:
        return {"status": "queued-runtime-version-mismatch",
                "cliVersion": cli_version,
                "appServerVersion": updated.get("appServerVersion")}
    return {"status": "updated", "cliVersion": cli_version, "appServerVersion": cli_version}
