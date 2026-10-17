"""Quiet, per-user daily maintenance; importing this module changes nothing."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET

MARKER = "modelhub-kit-schedule-v1"
LABEL = "org.modelhub-kit.maintenance"


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_UNSPECIFIED = object()


def _identity(info):
    # Windows path stat infers executable bits from the extension; fstat has
    # no filename and cannot reproduce those advisory bits. File identity,
    # ownership, timestamp, size and actual read/write modes still bind CAS.
    mode = info.st_mode & ~0o111 if os.name == "nt" else info.st_mode
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size,
            info.st_uid, mode, info.st_nlink)


def _snapshot(path: Path):
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if path.is_symlink() or path.resolve() != path.absolute() or not path.is_file() or info.st_nlink != 1 or info.st_size > 1024 * 1024:
        raise ValueError("unowned-schedule-file")
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("unprotected-schedule-file")
    with path.open("rb") as stream:
        if _identity(os.fstat(stream.fileno())) != _identity(info):
            raise ValueError("concurrent-schedule-file-change")
        data = stream.read()
        if _identity(os.fstat(stream.fileno())) != _identity(info):
            raise ValueError("concurrent-schedule-file-change")
    if _identity(path.lstat()) != _identity(info):
        raise ValueError("concurrent-schedule-file-change")
    return (data, _identity(info))


def _read(path: Path) -> bytes:
    value = _snapshot(path)
    if value is None:
        raise ValueError("missing-schedule-file")
    return value[0]


def _unchanged(path: Path, expected):
    if _snapshot(path) != expected:
        raise ValueError("concurrent-schedule-file-change")


def _write(path: Path, data: bytes, *, expected=_UNSPECIFIED):
    if expected is _UNSPECIFIED:
        expected = _snapshot(path)
    _unchanged(path, expected)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or path.parent.resolve() != path.parent.absolute():
        raise ValueError("symlinked-schedule-directory")
    info = path.parent.stat()
    if hasattr(os, "getuid") and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise ValueError("unprotected-schedule-directory")
    parent_identity = (info.st_dev, info.st_ino, info.st_uid, info.st_mode)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        temporary.chmod(0o600)
        written = temporary.stat()
        written_identity = _identity(written)
        info = path.parent.stat()
        if path.parent.resolve() != path.parent.absolute() or (info.st_dev, info.st_ino, info.st_uid, info.st_mode) != parent_identity:
            raise ValueError("concurrent-schedule-directory-change")
        _unchanged(path, expected)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    expected_written = (data, written_identity)
    _unchanged(path, expected_written)
    return expected_written


def _remove(path: Path, expected):
    _unchanged(path, expected)
    if expected is not None:
        path.unlink()


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    options = {}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=30, **options)


def _arguments(command: list[str]) -> list[str]:
    if not isinstance(command, list) or not command or any(
            not isinstance(value, str) or not value or any(ord(c) < 32 for c in value) for value in command):
        raise ValueError("invalid-maintenance-command")
    executable = command[0] if Path(command[0]).is_absolute() else shutil.which(command[0])
    if not executable:
        raise ValueError("maintenance-executable-unavailable")
    return [str(Path(executable).absolute()), *command[1:]]


def _systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


def _linux(command: list[str], state: Path) -> dict:
    folder = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
    unit, timer = folder / (LABEL + ".service"), folder / (LABEL + ".timer")
    workdir = str(Path(__file__).resolve().parents[1])
    service = ("[Unit]\nDescription=ModelHub Kit daily maintenance\n\n[Service]\n"
               "# " + MARKER + "\nType=oneshot\nExecStart=" + " ".join(map(_systemd_quote, command)) +
               "\nWorkingDirectory=" + _systemd_quote(workdir) +
               "\nEnvironment=" + _systemd_quote("MODELHUB_KIT_HOME=" + str(state)) +
               "\nNice=10\nIOSchedulingClass=idle\nCPUQuota=100%\nMemoryHigh=2G\n"
               "TimeoutStartSec=infinity\nStandardOutput=null\nStandardError=null\n")
    cadence = ("[Unit]\nDescription=ModelHub Kit daily maintenance schedule\n\n[Timer]\n# " + MARKER +
               "\nOnCalendar=daily\nPersistent=true\nRandomizedDelaySec=30min\nUnit=" + unit.name +
               "\n\n[Install]\nWantedBy=timers.target\n")
    return dict(kind="systemd-user", name=LABEL, files={unit: service.encode(), timer: cadence.encode()},
                primary=unit, timer=timer)


def _mac(command: list[str], state: Path) -> dict:
    file = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    payload = {"Label": LABEL, "ProgramArguments": command,
               "WorkingDirectory": str(Path(__file__).resolve().parents[1]),
               "EnvironmentVariables": {"MODELHUB_KIT_HOME": str(state)},
               "StartInterval": 86400, "RunAtLoad": False, "ProcessType": "Background",
               "LowPriorityIO": True, "Nice": 10,
               "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    return dict(kind="launchd-user", name=LABEL, files={file: plistlib.dumps(payload)}, primary=file)


def _xml_hash(data: bytes) -> str:
    element = ET.fromstring(data)
    def normalize(node):
        return [node.tag.split("}")[-1], sorted(node.attrib.items()), (node.text or "").strip(),
                [normalize(child) for child in node]]
    return _hash(json.dumps(normalize(element), separators=(",", ":")).encode())


def _windows(command: list[str], state: Path) -> dict:
    if any("%" in value for value in command + [str(state)]):
        # WScript.Shell expands %NAME% even without cmd.exe. Never interpret
        # user command text as an environment substitution.
        raise ValueError("percent-in-windows-schedule-path")
    folder = state / "schedule"
    launcher = folder / "maintenance.vbs"
    workdir = str(Path(__file__).resolve().parents[1])
    quoted = subprocess.list2cmdline(command).replace('"', '""')
    vbs = ("' " + MARKER + '\r\nSet shell = CreateObject("WScript.Shell")\r\n' +
           'shell.CurrentDirectory = "' + workdir.replace('"', '""') + '"\r\n' +
           'shell.Environment("Process")("MODELHUB_KIT_HOME") = "' + str(state).replace('"', '""') + '"\r\n' +
           'exitCode = shell.Run("' + quoted + '", 0, True)\r\nWScript.Quit exitCode\r\n')
    name = "ModelHubKit-" + _hash(str(state).casefold().encode())[:12]
    return dict(kind="windows-user-task", name=name, files={launcher: vbs.encode("utf-16")}, primary=launcher)


def _native(plan: dict) -> dict:
    kind, name = plan["kind"], plan["name"]
    if kind == "systemd-user":
        properties = ["LoadState", "ActiveState", "SubState", "FragmentPath", "DropInPaths", "ExecStart",
                      "Nice", "IOSchedulingClass", "CPUQuotaPerSecUSec", "MemoryHigh", "WorkingDirectory"]
        result = _run(["systemctl", "--user", "show", name + ".service",
                       *["--property=" + key for key in properties]])
        fields = dict(line.split("=", 1) for line in result.stdout.decode(errors="replace").splitlines() if "=" in line)
        timer_result = _run(["systemctl", "--user", "show", name + ".timer", *["--property=" + key for key in
                            ["LoadState", "ActiveState", "FragmentPath", "DropInPaths", "Persistent", "RandomizedDelayUSec", "Unit"]]])
        timer_fields = dict(line.split("=", 1) for line in timer_result.stdout.decode(errors="replace").splitlines() if "=" in line)
        if fields.get("LoadState") == "not-found" and timer_fields.get("LoadState") == "not-found":
            return {"exists": False, "available": True, "running": False, "timerExists": False}
        if fields.get("LoadState") not in {"loaded", "not-found"} or timer_fields.get("LoadState") not in {"loaded", "not-found"}:
            return {"available": False}
        service_exists = fields.get("LoadState") == "loaded"
        timer_exists = timer_fields.get("LoadState") == "loaded"
        timer_active = timer_fields.pop("ActiveState", "") == "active"
        running = fields.pop("ActiveState", "") in {"active", "activating", "deactivating"}
        fields.pop("SubState", None)
        # systemd appends the most recent PID and timestamps to ExecStart.
        # Those are run history, not a change to the generated definition.
        fields["ExecStart"] = re.split(r";\s*(?:start_time|stop_time|pid|code|status)=", fields.get("ExecStart", ""), 1)[0]
        matches = ((not service_exists or fields.get("FragmentPath") == str(plan["primary"]) and
                    not fields.get("DropInPaths") and fields.get("Nice") == "10") and
                   (not timer_exists or timer_fields.get("FragmentPath") == str(plan["timer"]) and not timer_fields.get("DropInPaths")))
        return {"exists": True, "available": True, "running": running,
                "ready": service_exists and timer_exists and timer_active, "timerExists": timer_exists,
                "definitionMatches": matches, "fingerprint": _hash(json.dumps([fields, timer_fields], sort_keys=True).encode())}
    if kind == "launchd-user":
        domain = "gui/" + str(os.getuid())
        result = _run(["launchctl", "print", domain + "/" + name])
        if result.returncode:
            domain_available = _run(["launchctl", "print", domain]).returncode == 0
            return {"exists": False, "available": domain_available, "running": False}
        text = result.stdout.decode(errors="replace")
        def field(key):
            match = re.search(r"^\s*" + re.escape(key) + r" = (.+)$", text, re.M)
            return match.group(1) if match else None
        arguments = re.search(r"\n\s*arguments = \{(.*?)\n\s*\}", text, re.S)
        fingerprint = {key: field(key) for key in ["program", "path", "run interval", "spawn type"]}
        fingerprint["arguments"] = [line.strip() for line in arguments.group(1).splitlines() if line.strip()] if arguments else []
        desired = plistlib.loads(plan["files"][plan["primary"]])
        matches = (fingerprint["arguments"] == desired["ProgramArguments"] and
                   fingerprint["path"] == str(plan["primary"]) and fingerprint["run interval"] == "86400 seconds")
        return {"exists": True, "available": True, "running": field("pid") is not None or field("state") in {"running", "active"},
                "definitionMatches": matches, "fingerprint": _hash(json.dumps(fingerprint, sort_keys=True).encode())}
    # A nonzero schtasks result can mean access denied or an unavailable
    # service. Only a successful exact native enumeration proves absence.
    query = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                  "$ErrorActionPreference='Stop';try{$tasks=@(Get-ScheduledTask -ErrorAction Stop);"
                  "$found=@($tasks|Where-Object {$_.TaskName -eq '" + name + "' -and $_.TaskPath -eq '" + chr(92) + "'});"
                  "if($found.Count -gt 1){exit 2};"
                  "@{found=($found.Count -eq 1);state=$(if($found.Count -eq 1){[int]$found[0].State}else{0})}"
                  "|ConvertTo-Json -Compress}catch{exit 1}"])
    if query.returncode:
        return {"available": False}
    try:
        observed = json.loads(query.stdout)
        if not isinstance(observed.get("found"), bool) or type(observed.get("state")) is not int:
            return {"available": False}
    except (ValueError, AttributeError):
        return {"available": False}
    if not observed["found"]:
        return {"exists": False, "available": True, "running": False}
    result = _run(["schtasks.exe", "/Query", "/TN", name, "/XML"])
    if result.returncode:
        return {"available": False}
    document = ET.fromstring(result.stdout)
    def find(relative):
        return document.findtext("/".join("{*}" + part for part in relative.split("/")))
    matches = (find("RegistrationInfo/Description") == MARKER and
               (find("Actions/Exec/Command") or "").casefold() == str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/wscript.exe").casefold() and
               find("Actions/Exec/Arguments") == subprocess.list2cmdline([str(plan["primary"])]) and
               find("Settings/Hidden") == "true" and find("Settings/MultipleInstancesPolicy") == "IgnoreNew")
    return {"exists": True, "available": True, "running": observed["state"] == 4, "definitionMatches": matches,
            "fingerprint": _xml_hash(result.stdout)}


def _windows_xml(plan: dict) -> bytes:
    import csv
    sid_result = _run(["whoami.exe", "/user", "/fo", "csv", "/nh"])
    rows = list(csv.reader(sid_result.stdout.decode(errors="replace").splitlines()))
    if sid_result.returncode or len(rows) != 1 or len(rows[0]) != 2 or not re.fullmatch(r"S-1-[0-9-]+", rows[0][1]):
        raise ValueError("current-windows-user-unavailable")
    task = ET.Element("Task", {"version": "1.2", "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task"})
    def add(parent, name, value=None, **attributes):
        node = ET.SubElement(parent, name, attributes)
        node.text = value
        return node
    add(add(task, "RegistrationInfo"), "Description", MARKER)
    trigger = add(add(task, "Triggers"), "TimeTrigger")
    repetition = add(trigger, "Repetition")
    add(repetition, "Interval", "PT24H")
    add(repetition, "StopAtDurationEnd", "false")
    add(trigger, "StartBoundary", (dt.datetime.now() + dt.timedelta(minutes=5)).replace(microsecond=0).isoformat())
    add(trigger, "Enabled", "true")
    principal = add(add(task, "Principals"), "Principal", id="CurrentUser")
    add(principal, "UserId", rows[0][1])
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    for key, value in [("MultipleInstancesPolicy", "IgnoreNew"), ("StartWhenAvailable", "true"),
                       ("Enabled", "true"), ("Hidden", "true"), ("ExecutionTimeLimit", "PT0S")]:
        add(settings, key, value)
    action = add(add(task, "Actions", Context="CurrentUser"), "Exec")
    add(action, "Command", str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/wscript.exe"))
    add(action, "Arguments", subprocess.list2cmdline([str(plan["primary"])]))
    return ET.tostring(task, encoding="utf-16", xml_declaration=True)


def configure(command: list[str], state: Path, apply: bool = False, remove: bool = False) -> dict:
    """Return a plan unless apply=True; only exact owned idle jobs may change."""
    result = {"status": "planned", "changed": False, "remove": remove, "jobsStarted": 0,
              "appsRestarted": 0, "backupsCreated": 0}
    try:
        state = Path(state).expanduser().absolute()
        command = _arguments(command)
        if os.name == "nt":
            plan = _windows(command, state)
        elif __import__("sys").platform == "darwin":
            plan = _mac(command, state)
        elif __import__("sys").platform.startswith("linux"):
            plan = _linux(command, state)
        else:
            return dict(result, status="unavailable", reason="unsupported-scheduler-platform")
        result.update(scheduler=plan["kind"], name=plan["name"], files=[str(p) for p in plan["files"]])
        record_file = state / "schedule/ownership.json"
        originals = {record_file: _snapshot(record_file)}
        record = json.loads(originals[record_file][0]) if originals[record_file] is not None else None
        if record and (record.get("marker") != MARKER or record.get("name") != plan["name"]):
            return dict(result, status="custom-preserved", reason="unowned-schedule-receipt")
        for file in plan["files"]:
            originals[file] = _snapshot(file)
            if originals[file] is not None:
                if not record or _hash(originals[file][0]) != record.get("files", {}).get(str(file)):
                    return dict(result, status="custom-preserved", reason="modified-or-unowned-job-file")
        try:
            native = _native(plan)
        except (OSError, subprocess.SubprocessError, ValueError):
            native = {"available": False}
        if not native.get("available"):
            return dict(result, status="planned" if not apply else "unavailable", reason="user-scheduler-unavailable")
        recovered = bool(record and record.get("pending") and native.get("definitionMatches") and
                         record.get("files") == {str(file): _hash(data) for file, data in plan["files"].items()})
        if native.get("exists") and (not record or native.get("fingerprint") != record.get("nativeFingerprint")) and not recovered:
            return dict(result, status="custom-preserved", reason="modified-or-unowned-native-job")
        desired = {str(file): _hash(data) for file, data in plan["files"].items()}
        current = record and record.get("files") == desired and native.get("exists") and native.get("ready", True)
        if not remove and current:
            if apply and recovered:
                record.update(nativeFingerprint=native["fingerprint"], pending=False)
                _write(record_file, (json.dumps(record, indent=2) + "\n").encode(), expected=originals[record_file])
            return dict(result, status="current")
        if remove and not record and not native.get("exists"):
            return dict(result, status="absent")
        if not apply:
            return result
        if native.get("running"):
            return dict(result, status="queued", reason="owned-maintenance-job-is-running")
        if record_file.parent.is_symlink() or state.is_symlink():
            raise ValueError("symlinked-schedule-state")
        kind, name = plan["kind"], plan["name"]
        for file, original in originals.items():
            _unchanged(file, original)
        if remove:
            if native.get("exists") and not (kind == "systemd-user" and native.get("timerExists") is False):
                argv = (["systemctl", "--user", "disable", "--now", name + ".timer"] if kind == "systemd-user" else
                        ["launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + name] if kind == "launchd-user" else
                        ["schtasks.exe", "/Delete", "/TN", name, "/F"])
                if _run(argv).returncode:
                    return dict(result, status="queued", reason="owned-job-removal-deferred")
            for file in plan["files"]:
                _remove(file, originals[file])
            _remove(record_file, originals[record_file])
            if kind == "systemd-user":
                _run(["systemctl", "--user", "daemon-reload"])
            return dict(result, status="removed", changed=True)
        # launchd replaces only an idle owned definition. No live process is
        # booted out, restarted, or signaled by this module.
        if kind == "launchd-user" and native.get("exists"):
            if _run(["launchctl", "bootout", "gui/" + str(os.getuid()) + "/" + name]).returncode:
                return dict(result, status="queued", reason="idle-job-replacement-deferred")
        for file, data in plan["files"].items():
            _write(file, data, expected=originals[file])
        # A failed or asynchronous registration remains an owned retry, rather
        # than turning our just-written files into an unowned custom job.
        pending = {"marker": MARKER, "name": name, "files": desired, "pending": True,
                   "nativeFingerprint": native.get("fingerprint")}
        pending_identity = _write(record_file, (json.dumps(pending, indent=2) + "\n").encode(), expected=originals[record_file])
        if kind == "systemd-user":
            operations = [["systemctl", "--user", "daemon-reload"],
                          ["systemctl", "--user", "enable", "--now", name + ".timer"]]
        elif kind == "launchd-user":
            operations = [["launchctl", "bootstrap", "gui/" + str(os.getuid()), str(plan["primary"])]]
        else:
            with tempfile.NamedTemporaryFile(dir=state / "schedule", suffix=".xml", delete=False) as stream:
                stream.write(_windows_xml(plan))
                temporary = Path(stream.name)
            try:
                argv = ["schtasks.exe", "/Create", "/TN", name, "/XML", str(temporary)]
                if native.get("exists"):
                    argv.append("/F")
                operations = [argv]
                if any(_run(operation).returncode for operation in operations):
                    return dict(result, status="queued", reason="task-registration-deferred")
            finally:
                temporary.unlink(missing_ok=True)
            operations = []
        if any(_run(operation).returncode for operation in operations):
            return dict(result, status="queued", reason="job-registration-deferred")
        registered = _native(plan)
        if not registered.get("exists"):
            return dict(result, status="queued", reason="native-registration-not-yet-visible")
        record = {"marker": MARKER, "name": name, "files": desired,
                  "nativeFingerprint": registered["fingerprint"]}
        _write(record_file, (json.dumps(record, indent=2) + "\n").encode(), expected=pending_identity)
        return dict(result, status="applied", changed=True)
    except ValueError as error:
        if str(error).startswith("concurrent-schedule-"):
            return dict(result, status="custom-preserved", reason="concurrent-schedule-change")
        return dict(result, status="unavailable", reason="schedule-inspection-or-registration-failed")
    except (OSError, subprocess.SubprocessError, ET.ParseError):
        return dict(result, status="unavailable", reason="schedule-inspection-or-registration-failed")
