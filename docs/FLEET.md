# Fleet installation and maintenance

Fleet inventory stays on your computer. Delivery copies a small, explicit public source package over your existing SSH connections. It does not copy Git history, personal configuration, tool selections, authentication, conversations, caches, or SSH keys.

Create a private inventory outside this checkout. Start with an empty list, then add only aliases you already use through your local SSH configuration:

```json
{
  "version": 1,
  "hosts": [
    {"alias": "workstation-arch", "os": "linux:arch"},
    {"alias": "portable-macos", "os": "macos"},
    {"alias": "workstation-windows", "os": "windows"}
  ]
}
```

Linux variants use `/etc/os-release`'s exact `ID`, such as `linux:arch`. A plain `linux` accepts any Linux distribution. An optional `boot` field pins one exact boot identity from `modelhub-kit doctor --json`; it must be updated after a reboot. Even without that field, each applied operation binds the actual observed boot for that invocation. For a machine with multiple operating systems, use a separate inventory entry and trusted SSH alias for each intended OS. Inactive boots remain queued.

Verify SSH host fingerprints independently before enrollment. Fleet commands require `BatchMode=yes` and `StrictHostKeyChecking=yes`; unknown or changed keys, unavailable authentication, offline machines, and another OS or boot stay queued. No password prompts are opened and no passwords are stored. Windows needs its native OpenSSH server, PowerShell, and regular, non-reparse `tar.exe` in the native system directory; Git or other tools earlier in `PATH` cannot replace that extractor; Unix hosts need an SSH shell, `tar`, and `sha256sum` or `shasum`.

## First installation or public source refresh

Run from a reviewed public checkout. These plans make no remote connections or writes:

```sh
modelhub-kit fleet --inventory "$HOME/fleet.json" --enroll --source "$HOME/src/modelhub-kit"
```

The plan includes the exact relative source files, their SHA-256 hashes, and the delivery archive hash. Review it, then apply the same request:

```sh
modelhub-kit fleet --inventory "$HOME/fleet.json" --enroll --source "$HOME/src/modelhub-kit" --apply
```

`--enroll` explicitly authorizes installing or refreshing the kit for every listed alias. It does not enroll SSH keys, copy native sign-ins, choose models, select tools, or install missing coding applications. Existing personal configuration and custom launchers are preserved by the normal installer. A modified or unmanaged installation queues rather than being replaced. Identical installed source is left current.

Python 3.11+ is required to run the kit. Missing or incompatible prerequisites remain queued unless you explicitly allow the existing native installer to obtain them:

```sh
modelhub-kit fleet --inventory "$HOME/fleet.json" --enroll --source "$HOME/src/modelhub-kit" --prerequisites --apply
```

The installer preserves compatible Python and Node installations. Supported native package managers run without password prompts; permission or repository problems leave the host queued. Fleet never saves elevation credentials. Add `--schedule` only if you also want the existing per-user background maintenance schedule installed. These options require `--enroll --source`.

## Routine maintenance

Once installed, the original maintenance command remains available:

```sh
modelhub-kit fleet --inventory "$HOME/fleet.json" --apply
```

It runs each host's installed `maintain` command, using that host's own configuration. Nested queued or failed maintenance work is not reported as completed. Offline, busy, and mismatched boots stay in the single overwritten `fleet-status.json` receipt for the next invocation. This command does not deliver source or reinstall the application. It does not create a retry daemon; invoke it again or schedule it explicitly through your own controller.

## Delivery guards and resource use

Before staging, a read-only SSH probe checks the actual OS and boot. A second, finite SSH session repeats those checks before its first write and again before installation. On Unix, the archive travels through standard input in bounded chunks. On Windows, a small UTF-16LE encoded loader first receives a compressed, length-bound PowerShell script prefix into memory, checks its SHA-256 hash, and verifies its bounded decoded length. The loader reads the native standard-input handle through a file stream, avoiding the Windows PowerShell console-stream SSH blocking bug. It then reads exactly the declared archive length, checks its SHA-256 in memory, and repeats the boot check before invoking the script. The script consumes that buffered archive through a memory stream, avoiding standard-input interference from PowerShell or child processes. Archive buffering uses at most 8 MiB; no script is written to disk. Both receivers stop at their declared byte counts. The Windows loader stays below 7,500 command-line characters so it also fits the default command shell limit. The Windows installer invocation uses a process-scoped execution-policy option; no machine or user execution policy is saved.

Only literal installer-declared Python modules, `tools.json`, and the three installer scripts can travel. Source files must be regular, owned, unchanged files; symlinks and unsafe paths are rejected. The public archive has a fixed size limit and contains no checkout traversal. The remote machine verifies the complete archive and every source-file hash before executing the installer.

The installer uses the same kernel writer as ordinary maintenance. Concurrent work queues; interactive applications are never signaled, restarted, or limited. Delivery uses one private temporary directory and removes only its exact transferred files and empty directories. Archive cleanup rechecks the captured file identity and hash; Windows also fences the native directory identity and owner ACL. Windows PowerShell 5.1 receives a flat directory list so exact cleanup can remove empty source directories. If unknown or changed content appears there, cleanup preserves it and reports a queued result. No backups or growing logs are produced. The local receipt stores outcomes and hashes, without remote command output or source contents.
