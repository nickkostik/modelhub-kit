#!/usr/bin/env python3
"""Install the small per-user application from an explicit source allowlist."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

FILES = (
    "modelhub_kit/__init__.py", "modelhub_kit/__main__.py", "modelhub_kit/core.py",
    "modelhub_kit/updater.py", "modelhub_kit/catalog.py", "modelhub_kit/integration.py",
    "modelhub_kit/projection.py", "modelhub_kit/acp.py", "modelhub_kit/lifecycle.py",
    "modelhub_kit/cache.py", "modelhub_kit/fleet.py", "modelhub_kit/schedule.py", "modelhub_kit/tools.json",
    "modelhub_kit/native_helpers/__init__.py", "modelhub_kit/native_helpers/omp.py",
    "modelhub_kit/native_helpers/droid.py", "modelhub_kit/native_helpers/products.py",
    "modelhub_kit/native_helpers/desktop.py", "modelhub_kit/native_helpers/macos_apps.py",
)
OPTIONAL_FILES = (
    "LICENSE", "AGENTS.md", "README.md", "docs/INSTALL.md", "docs/OPERATIONS.md",
    "docs/SUPPORT.md", "docs/INTEGRATIONS.md", "docs/FLEET.md",
)
MARKER = "modelhub-kit-install-v1"
LAUNCH = '''#!/usr/bin/env python3
"""Installed application entry; configuration and state live outside this tree."""
import pathlib
import runpy
import sys
import os
sys.dont_write_bytecode = True
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
node_bin = __NODE_BIN__
if node_bin:
    os.environ["PATH"] = node_bin + os.pathsep + os.environ.get("PATH", "")
runpy.run_module("modelhub_kit", run_name="__main__", alter_sys=True)
'''


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_UNSPECIFIED = object()


def _identity(info):
    # Windows path stat infers executable bits from the extension; fstat has
    # no filename and cannot reproduce those advisory bits. File identity,
    # ownership, timestamp, size and actual read/write modes still bind CAS.
    mode = info.st_mode & ~0o111 if os.name == "nt" else info.st_mode
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size,
            info.st_uid, mode, info.st_nlink)


def snapshot(path: Path):
    """Bind ownership evidence to both original bytes and the actual file."""
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file() or info.st_nlink != 1:
        raise ValueError("An existing installation file is not a regular owned file")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("An existing installation file belongs to another user")
    with path.open("rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(info):
            raise ValueError("An installation file changed during inspection")
        data = stream.read()
        if _identity(os.fstat(stream.fileno())) != _identity(info):
            raise ValueError("An installation file changed during inspection")
    if _identity(path.lstat()) != _identity(info):
        raise ValueError("An installation file changed during inspection")
    return (data, _identity(info))


def own_file(path: Path) -> bytes:
    value = snapshot(path)
    if value is None:
        raise ValueError("An installation file disappeared during inspection")
    return value[0]


def unchanged(path: Path, expected):
    if snapshot(path) != expected:
        raise ValueError("A concurrently changed installation file was preserved")


def _check_install_directory(path: Path):
    from modelhub_kit.core import GuardError
    info = path.lstat()
    if (path.is_symlink() or path.resolve() != path.absolute() or not path.is_dir()
            or hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022)):
        raise GuardError("unsafe-installation-directory-preserved")


def _private_install_directory(path: Path):
    # mkdir(parents=True, mode=0700) does not apply that mode to intermediates.
    # Validate the existing ancestor, then create/check each missing component.
    missing = []
    ancestor = path
    while not ancestor.exists() and not ancestor.is_symlink():
        missing.append(ancestor)
        ancestor = ancestor.parent
    _check_install_directory(ancestor)
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        _check_install_directory(directory)


def atomic_write(path: Path, data: bytes, mode: int = 0o600, *, expected=_UNSPECIFIED) -> bool:
    if expected is _UNSPECIFIED:
        expected = snapshot(path)
    unchanged(path, expected)
    if expected is not None and expected[0] == data:
        if os.name != "nt" and expected[1][5] & 0o777 != mode:
            with path.open("rb") as stream:
                info = os.fstat(stream.fileno())
                if (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size,
                    info.st_uid, info.st_mode, info.st_nlink) != expected[1]:
                    raise ValueError("A concurrently changed installation file was preserved")
                unchanged(path, expected)
                os.fchmod(stream.fileno(), mode)
            return True
        return False
    _private_install_directory(path.parent)
    if path.parent.is_symlink() or path.parent.resolve() != path.parent.absolute():
        raise ValueError("A symlinked installation directory was preserved")
    info = path.parent.stat()
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("An unowned or shared writable installation directory was preserved")
    parent_identity = (info.st_dev, info.st_ino, info.st_mode, info.st_uid)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(mode)
        info = path.parent.stat()
        if path.parent.resolve() != path.parent.absolute() or (info.st_dev, info.st_ino, info.st_mode, info.st_uid) != parent_identity:
            raise ValueError("An installation directory changed during preparation")
        unchanged(path, expected)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def remove_owned(path: Path, expected) -> bool:
    unchanged(path, expected)
    if expected is None:
        return False
    path.unlink()
    return True


def _quiet_child_options() -> dict:
    """Hide our own Windows helper children; Unix process behavior is unchanged."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _node_version(executable: str | None) -> str | None:
    version = None
    if executable:
        try:
            value = subprocess.check_output([executable, "--version"], timeout=5, stdin=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, **_quiet_child_options()).decode().strip()
            if re.fullmatch(r"v\d+\.\d+\.\d+", value):
                version = value[1:]
        except (OSError, subprocess.SubprocessError):
            pass
    return version


def node_prerequisite(allow_install: bool = False) -> dict:
    executable = shutil.which("node")
    version = _node_version(executable)
    if version and int(version.split(".")[0]) >= 22:
        return {"ready": True, "version": version, "changed": False, "executable": executable}
    manager = None
    if os.name == "nt":
        manager = ["winget", "install", "--id", "OpenJS.NodeJS.LTS", "--exact", "--scope", "user",
                   "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    elif sys.platform == "darwin":
        manager = ["brew", "install", "node@24"]
    elif shutil.which("pacman"):
        manager = ["pacman", "-S", "--needed", "--noconfirm", "nodejs-lts-krypton", "npm"]
    elif shutil.which("dnf"):
        manager = ["dnf", "install", "-y", "nodejs", "npm"]
    elif shutil.which("apt-get"):
        manager = ["apt-get", "install", "-y", "nodejs", "npm"]
    if manager and manager[0] not in {"brew", "winget"} and getattr(os, "geteuid", lambda: 1)() != 0:
        manager = ["sudo", "-n", *manager]
    result = {"ready": False, "version": version, "changed": False, "requiredMajor": 22,
              "installCommand": subprocess.list2cmdline(manager) if manager else None,
              "officialDownload": "https://nodejs.org/en/download",
              "note": "Choose a supported Node 22+ package; the application itself needs only Python."}
    if not allow_install or not manager or not shutil.which(manager[0]):
        return result
    try:
        package_manager = next((name for name in ("apt-get", "dnf") if name in manager), None)
        if package_manager:
            query = (["apt-cache", "policy", "nodejs"] if package_manager == "apt-get" else
                     ["dnf", "-q", "repoquery", "--latest-limit", "1", "--qf", "%{version}", "nodejs"])
            offered = subprocess.check_output(query, timeout=30, stdin=subprocess.DEVNULL,
                                              stderr=subprocess.DEVNULL, env=dict(os.environ, LC_ALL="C")).decode()
            found = (re.search(r"Candidate:\s*(?:\d+:)?(\d+)\.", offered) if package_manager == "apt-get" else
                     re.search(r"^\s*(\d+)\.", offered, re.M))
            if not found or int(found.group(1)) < 22:
                result["note"] = "The configured repository does not advertise Node 22+. Use an official Node LTS download; no older Node package was installed."
                return result
        options = _quiet_child_options()
        operation = subprocess.run(manager, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=600,
                                   env=dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1"), **options)
        result["prerequisiteInstallExit"] = operation.returncode
        candidates = [shutil.which("node")]
        if sys.platform == "darwin":
            prefix = subprocess.check_output(["brew", "--prefix", "node@24"], timeout=10, stderr=subprocess.DEVNULL).decode().strip()
            candidates.insert(0, str(Path(prefix) / "bin/node"))
        elif os.name == "nt":
            candidates.extend(str(Path(base) / suffix) for base, suffix in [
                (os.environ.get("LOCALAPPDATA", ""), "Programs/nodejs/node.exe"),
                (os.environ.get("LOCALAPPDATA", ""), "Microsoft/WinGet/Links/node.exe"),
                (os.environ.get("ProgramFiles", ""), "nodejs/node.exe")] if base)
        for candidate in candidates:
            installed = _node_version(candidate)
            if installed and int(installed.split(".")[0]) >= 22:
                return {"ready": True, "version": installed, "changed": True, "executable": candidate}
    except (OSError, subprocess.SubprocessError):
        result["prerequisiteInstallStatus"] = "unavailable-or-permission-required"
    result["note"] = "A compatible native package was not verified. Install an official Node LTS runtime and run the installer again."
    return result


def _install_files(source: Path, schedule: bool = False, dry_run: bool = False, prerequisites: bool = False) -> dict:
    if sys.version_info < (3, 11):
        raise ValueError("Python 3.11 or newer is required")
    source = source.resolve()
    home = Path.home()
    base = (Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local")) / "modelhub-kit"
            if os.name == "nt" else home / ".local/share/modelhub-kit")
    app = base / "app"
    command = (base / "bin/modelhub-kit.cmd" if os.name == "nt" else home / ".local/bin/modelhub-kit")
    for directory in (app, app.parent, command.parent):
        if directory.exists() or directory.is_symlink():
            _check_install_directory(directory)
    receipt_file = app / ".modelhub-kit-install.json"
    bodies = {}
    for relative in FILES + OPTIONAL_FILES:
        file = source / relative
        if not file.exists() and relative in OPTIONAL_FILES:
            continue
        if file.is_symlink() or not file.is_file() or file.resolve() != file.absolute():
            raise ValueError("The source allowlist is incomplete or contains a symlink: " + relative)
        bodies[relative] = file.read_bytes()
    node = node_prerequisite()
    node_bin = str(Path(node["executable"]).parent) if node.get("ready") else None
    bodies["launch.py"] = LAUNCH.replace("__NODE_BIN__", repr(node_bin)).encode()
    originals = {receipt_file: snapshot(receipt_file)}
    previous = None
    if originals[receipt_file] is not None:
        previous = json.loads(originals[receipt_file][0])
        if previous.get("marker") != MARKER:
            raise ValueError("An existing application directory is not managed by this installer")
    elif app.exists() and any(app.iterdir()):
        raise ValueError("A nonempty unmanaged application directory was preserved")
    if app.is_symlink() or command.is_symlink():
        raise ValueError("An existing symlinked installation was preserved")
    # Keep a stable interpreter symlink when the package manager provides one;
    # resolving it into a versioned package cellar would break after cleanup.
    python = str(Path(sys.executable).absolute())
    if os.name == "nt":
        if any(character in python + str(app) for character in '\r\n"'):
            raise ValueError("Unsupported characters in the Windows installation path")
        launcher = ('@echo off\r\nrem modelhub-kit:launcher:v1\r\n"' + python.replace("%", "%%") +
                    '" -B "' + str(app / "launch.py").replace("%", "%%") + '" %*\r\nexit /b %errorlevel%\r\n').encode()
    else:
        launcher = ("#!/bin/sh\n# modelhub-kit:launcher:v1\nexec " + shlex.quote(python) + " -B " +
                    shlex.quote(str(app / "launch.py")) + ' "$@"\n').encode()
    for relative in bodies:
        target = app / relative
        originals[target] = snapshot(target)
        if originals[target] is not None:
            actual = originals[target][0]
            expected = (previous or {}).get("files", {}).get(relative)
            if not expected or digest(actual) != expected:
                raise ValueError("A modified or unmanaged application file was preserved: " + relative)
    originals[command] = snapshot(command)
    if originals[command] is not None:
        expected = (previous or {}).get("launcherSha256")
        if not expected or digest(originals[command][0]) != expected:
            raise ValueError("An existing custom command was preserved")
    obsolete = []
    for relative, expected in (previous or {}).get("files", {}).items():
        if relative not in bodies:
            if relative not in FILES + OPTIONAL_FILES:
                raise ValueError("An unexpected previous install entry was preserved")
            target = app / relative
            originals[target] = snapshot(target)
            if originals[target] is not None and digest(originals[target][0]) != expected:
                raise ValueError("A modified retired application file was preserved")
            obsolete.append(target)
    # Inspect every existing target parent (or nearest existing ancestor) before
    # any application write. Nested shared-writable folders must not fail only
    # after an earlier module was already replaced. This also stays read-only
    # for dry-run and never adopts/chmods an unsafe partial installation.
    for target in [receipt_file, command, *[app / relative for relative in bodies], *obsolete]:
        ancestor = target.parent
        while not ancestor.exists() and not ancestor.is_symlink():
            ancestor = ancestor.parent
        _check_install_directory(ancestor)
    result = {"status": "planned" if dry_run else "current", "application": str(app), "command": str(command),
              "python": platform.python_version(), "node": node, "filesChanged": 0,
              "scheduleRequested": schedule, "backupsCreated": 0}
    if dry_run:
        result["allowlistedFiles"] = list(bodies)
        return result
    if prerequisites and not node.get("ready"):
        node = node_prerequisite(True)
        result["node"] = node
        node_bin = str(Path(node["executable"]).parent) if node.get("ready") else None
        bodies["launch.py"] = LAUNCH.replace("__NODE_BIN__", repr(node_bin)).encode()
    changing = (any(originals[app / relative] is None or originals[app / relative][0] != data
                    for relative, data in bodies.items())
                or originals[command] is None or originals[command][0] != launcher
                or any(originals[path] is not None for path in obsolete))
    if previous and changing:
        from modelhub_kit.lifecycle import _idle
        if not _idle(app, command):
            from modelhub_kit.core import GuardError
            raise GuardError('active-or-unknown-kit-runtime')
    for path, expected in originals.items():
        unchanged(path, expected)
    from modelhub_kit.core import GuardError
    try:
        for relative, data in bodies.items():
            target = app / relative
            result["filesChanged"] += atomic_write(target, data, expected=originals[target])
        for target in obsolete:
            result["filesChanged"] += remove_owned(target, originals[target])
        result["filesChanged"] += atomic_write(command, launcher, 0o755, expected=originals[command])
        receipt = {"marker": MARKER, "files": {name: digest(data) for name, data in bodies.items()},
                   "launcherSha256": digest(launcher)}
        atomic_write(receipt_file, (json.dumps(receipt, indent=2) + "\n").encode(), expected=originals[receipt_file])
    except GuardError as error:
        if str(error) != "unsafe-installation-directory-preserved":
            raise
        # A concurrent directory change can still follow the full preflight.
        # Keep the real count; do not initialize/relaunch a partial candidate.
        result.update(status="queued", reason=str(error))
        return result
    result["status"] = "installed" if result["filesChanged"] else "current"
    return result


def install(source: Path, schedule: bool = False, dry_run: bool = False, prerequisites: bool = False) -> dict:
    # Import only the user-selected source package, never an unverified installed tree.
    source = source.resolve()
    sys.path.insert(0, str(source))
    from modelhub_kit.core import GuardError, locations, writer
    try:
        with contextlib.nullcontext() if dry_run else writer(locations()[2]):
            result = _install_files(source, schedule, dry_run, prerequisites)
    except GuardError as error:
        if str(error) in {"writer-busy", "active-or-unknown-kit-runtime", "unsafe-installation-directory-preserved"}:
            return {"status": "queued", "reason": str(error), "filesChanged": 0, "backupsCreated": 0}
        raise ValueError("The shared maintenance state was preserved") from error
    if dry_run or result.get("status") == "queued":
        return result
    app = Path(result["application"])
    python = str(Path(sys.executable).absolute())
    initialized = subprocess.run([python, "-B", str(app / "launch.py"), "init"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 timeout=30, **_quiet_child_options())
    result["initExit"] = initialized.returncode
    if initialized.returncode:
        result["status"] = "configuration-not-initialized"
        return result
    if schedule:
        completed = subprocess.run([python, "-B", str(app / "launch.py"), "schedule", "--apply"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=60, **_quiet_child_options())
        result["scheduleExit"] = completed.returncode
        try:
            scheduled = json.loads(completed.stdout)
            result["scheduleStatus"] = scheduled.get("status", "unavailable")
        except (ValueError, TypeError):
            result["scheduleStatus"] = "unavailable"
        result["scheduleConfigured"] = completed.returncode == 0 and result["scheduleStatus"] in {"current", "applied"}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--schedule", action="store_true", help="explicitly register quiet daily per-user maintenance")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prerequisites", action="store_true", help="wrapper option: allow native Python bootstrap")
    args = parser.parse_args()
    try:
        result = install(args.source, args.schedule, args.dry_run, args.prerequisites)
        print(json.dumps(result, indent=2))
        if result.get("status") == "queued":
            return 75
        return 1 if (result.get("initExit", 0) or args.prerequisites and not args.dry_run and not result["node"]["ready"] or
                     args.schedule and not args.dry_run and not result.get("scheduleConfigured")) else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Installation stopped; an unavailable prerequisite or an existing custom/unowned path was preserved.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
