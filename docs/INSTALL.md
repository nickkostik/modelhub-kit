# Install ModelHub Kit

ModelHub Kit uses Python 3.11 or newer and the standard library. It does not
need pip packages, a database, a background server, or model credentials to
install. Node 22 or newer is needed for the npm-based tools you choose to
manage; an existing compatible Node installation is preserved.

From a downloaded or cloned copy of this project:

```sh
sh scripts/install.sh
```

On Windows, use an ordinary PowerShell terminal:

```powershell
& .\scripts\install.ps1
```

If your PowerShell policy disables scripts and Python is already installed,
`py -3 scripts/install.py` runs the same installer without changing that policy.

The installer copies an explicit allowlist of application files and initializes
an empty personal configuration without overwriting an existing one. It excludes
Git history, tests, example inventories, runtime state, and credentials. A
repeat installation with identical files does no work. Modified application
files and custom commands are preserved; the installer stops instead of
overwriting them. No backups are made.

| Platform | Application | Command |
| --- | --- | --- |
| Linux and macOS | `~/.local/share/modelhub-kit/app` | `~/.local/bin/modelhub-kit` |
| Windows | `%LOCALAPPDATA%\modelhub-kit\app` | `%LOCALAPPDATA%\modelhub-kit\bin\modelhub-kit.cmd` |

Add the command directory to your user PATH if it is not already present. The
installer does not alter shell profiles, system PATH, an npm prefix, or an
existing runtime manager. You can also use the command's full path.

## Prerequisites

The wrappers detect compatible Python before making any changes. If Python is
missing, they print installation steps and stop. To explicitly allow native
Python installation:

```sh
sh scripts/install.sh --prerequisites
```

```powershell
& .\scripts\install.ps1 -Prerequisites
```

This opt-in can invoke your native package manager for missing Python and a
missing or older-than-22 Node runtime. It does not upgrade the operating system,
replace a working Python, or upgrade an already compatible Node installation.
Some native package operations require prior administrative authorization;
unavailable authorization produces installation steps rather than a hidden prompt.
Some older distributions do not provide Python 3.11 in their supported
repositories; use a supported interpreter rather than replacing system Python.
The [Python documentation](https://docs.python.org/3/using/) covers official
installation options. On macOS, preserve Apple's system Python and install a
separate supported interpreter.

Without that opt-in, missing or old Node produces an actionable package-manager
command. Repositories advertising older-than-22 Node packages are left unchanged.
An unavailable supported native package is reported as a prerequisite failure;
the installer does not claim that the runtime is ready. Official LTS downloads and checksum-verification instructions
are available from [Node.js](https://nodejs.org/en/download). Node is not
reinstalled when it is already compatible. The installed entry remembers the
selected Node binary directory for maintenance without changing your shell profile.

## First use

```sh
modelhub-kit init
modelhub-kit doctor --json
modelhub-kit update --dry-run
```

Choose tools and catalog sources in your own configuration before enabling
maintenance. Existing installed tools can be updated; installing missing tools
requires the explicit `update --install-missing` option. Native client sign-in
stays with each client. The kit does not copy account credentials.

Select a registered ID rather than a shared command alias:

```sh
modelhub-kit update --tool devin-cli --install-missing
modelhub-kit update --tool cursor-cli --install-missing
modelhub-kit update --tool muse --install-missing
```

Desktop applications have separate IDs: `cursor-desktop`, `devin-desktop` and
`grokbot`. An explicit request still requires a supported verified native
installation channel. [Product support](SUPPORT.md) documents the platform
matrix and guarded paths; an unavailable channel stays queued.

| Platform | Configuration | State |
| --- | --- | --- |
| Linux and macOS | `$XDG_CONFIG_HOME/modelhub-kit/config.json`, default `~/.config/modelhub-kit/config.json` | `$XDG_STATE_HOME/modelhub-kit`, default `~/.local/state/modelhub-kit` |
| Windows | `%LOCALAPPDATA%\modelhub-kit\config.json` | `%LOCALAPPDATA%\modelhub-kit\state` |

`MODELHUB_KIT_HOME` overrides only the state directory. Configuration and state
stay outside the installed code tree. Run `python3 scripts/install.py --dry-run`
to inspect the installer allowlist and destination paths without installing.

## Optional daily scheduling

```sh
modelhub-kit schedule
modelhub-kit schedule --apply
```

The installer can also register the schedule when explicitly requested:

```sh
sh scripts/install.sh --schedule
```

```powershell
& .\scripts\install.ps1 -Schedule
```

Scheduling is per-user and quiet. It does not create terminal windows or
restart an interactive coding session. See [Operations](OPERATIONS.md) for
ownership checks, platform behavior, and removal.

## Fleet installation

The same allowlisted installer can be delivered to trusted SSH aliases after
actual OS and boot discovery:

```sh
modelhub-kit fleet --inventory /path/to/private-inventory.json --source /path/to/reviewed-source --enroll
modelhub-kit fleet --inventory /path/to/private-inventory.json --source /path/to/reviewed-source --enroll --apply
```

Add `--prerequisites` only when you intend to install missing supported runtimes,
and `--schedule` only to register background maintenance. Neither option is
implied by enrollment. A remote administrative authorization gap queues work;
the kit does not transmit passwords or disable strict SSH identity checks.
See [Fleet delivery](FLEET.md) for boot enrollment and bounded transfer details.
