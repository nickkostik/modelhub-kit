# Install ModelHub Kit

ModelHub Kit uses Python 3.11 or newer and the standard library. It does not
need pip packages, a database, a background server, or model credentials to
install. Node 22 or newer is needed for the npm-based tools you choose to
manage; an existing compatible Node installation is preserved.

## Choose a release

Use the [published releases](https://github.com/nickkostik/modelhub-kit/releases)
to choose a reviewed version. For this release, either download and extract
[the v0.2.3 source ZIP](https://github.com/nickkostik/modelhub-kit/archive/refs/tags/v0.2.3.zip)
or use Git:

```sh
git clone --branch v0.2.3 --depth 1 https://github.com/nickkostik/modelhub-kit.git modelhub-kit-v0.2.3
cd modelhub-kit-v0.2.3
```

The same Git commands work in PowerShell; use `Set-Location modelhub-kit-v0.2.3`
if preferred. Git is optional when using a downloaded source archive. Inspect
the source before running the installer below. Tags select a version; a version
label alone is not an independent signature verification. The release contains
source, not a preconfigured account or a bundled vendor application.

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

On macOS, install Python 3.11+ yourself when it is missing; the wrapper reports
`queued` and links to the official Python download instead of starting Homebrew.
Generic macOS Node/npm package-manager mutations also queue. Linux and Windows
bootstrap commands before Python is available wait for the direct package
manager; full descendant custody applies to the Python-backed mutation runner.
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


## Upgrade the kit itself

`modelhub-kit update` updates selected coding tools. It does **not** download or
upgrade ModelHub Kit. Choose a newer published release using the steps above,
review that source, and rerun its installer from the newly extracted checkout:

```sh
sh scripts/install.sh
modelhub-kit --version
```

```powershell
& .\scripts\install.ps1
modelhub-kit --version
```

Use the same home, configuration and state environment as the existing
installation. There is no need to request prerequisites or scheduling again.
Existing native tools, sign-ins, configuration and quiet schedules stay in place.
The installer verifies the previous receipt and current code before replacing
only its owned application files. An identical repeat changes zero files.
Changed code queues while another kit reader or writer is active, or process
visibility is unknown; retry after that work finishes. Custom/unknown files and
launchers are preserved instead of overwritten. No backup or background
self-update service is created. An older release is not an automatic rollback:
review compatibility and its installer before deliberately selecting it.

A source checkout that was downloaded separately is not part of the installed
application. It is never removed by upgrade or uninstall.

## Remove ModelHub Kit

First inspect a removal plan, then explicitly apply it:

```sh
modelhub-kit uninstall
modelhub-kit uninstall --apply
```

The same commands work in PowerShell. If the command is not on PATH, use its
full launcher path from the installation table. For an older installation
without `uninstall`, or to resume an interrupted removal, run from reviewed
v0.2.2 or newer source instead:

```sh
python3 -B -m modelhub_kit uninstall
python3 -B -m modelhub_kit uninstall --apply
```

```powershell
py -3 -B -m modelhub_kit uninstall
py -3 -B -m modelhub_kit uninstall --apply
```

Use the same configuration/state environment used for scheduling. Remove any
owned schedules registered under additional state directories first with
`schedule --remove --apply` in each corresponding environment; uninstall does
not search other state directories or users. It removes only the current
state's receipt-owned, idle schedule and the receipt-owned application files
and launcher. There is no recursive application-tree deletion. Unknown files,
symlinks, modified code or schedules, active kit work and unknown process state
queue removal. No session or vendor app is killed to satisfy a guard.

By default, configuration and all remaining state are retained, including
installed vendor tools, their ownership records, model catalogs, native
integration records, authentication and preferences. The state writer lock is
also retained so another invocation cannot lock a different inode. Uninstall
never removes Python, Node, coding tools, projects, native MCP/ACP settings or
the separately downloaded source checkout.

For the optional, narrow state cleanup, select it **before** applying removal:

```sh
modelhub-kit uninstall --clean-state
modelhub-kit uninstall --clean-state --apply
```

`--clean-state` removes only the current bounded `last-maintenance.json` receipt
with the v0.2.2 kit marker and exact known top-level schema. It never purges the
state tree, caches or tool installations. Older unmarked receipts, custom top-level fields
or concurrent receipt changes queue this opt-in; omit it to retain the receipt.
No backups or retained removal logs are created. If an interruption leaves
partial removal, its recovery receipt stays until the checked files have been
unlinked; retry from the reviewed source command above. A queued result reports
any already completed file removals and preserves the remaining unknown data.
## Runtime ownership

On Unix, runtime version checks require protected ownership of the Node/npm
selection, its resolved target, and their parent directories. Root maintenance
does not execute a runtime writable by another account. An unverified runtime
queues work without reinstalling it or changing ownership. Use your operating
system's package manager runtime, or a protected runtime owned by the account
running maintenance.
