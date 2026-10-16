# Operations

Run `modelhub-kit doctor --json` for a read-only view of prerequisites and
configured commands. Runtime receipts belong in the state directory, not in
the project checkout. Keep configuration, inventories, authentication files,
and receipts out of public repositories.

## Updates and catalogs

```sh
modelhub-kit update --dry-run
modelhub-kit update
modelhub-kit sync
modelhub-kit maintain
```

`maintain` serializes a selected-tool update, configured catalog refresh, and
kit-owned cache cleanup under one writer lock. A busy writer is queued. It
does not install every available tool, start every agent, run inference, or
restart your interactive applications.

Updates require proof that the active installation is supported and owned.
Current tools are not routinely reinstalled. Custom launchers and newer
installed versions are preserved. A failed native asset download must not
replace the working executable. A live old executable can remain until it is
safe to retire; it is an active runtime, not disposable cache.

Catalog sources are explicitly configured HTTPS sources. Capability metadata
and reasoning choices come from those sources, with unknown capabilities kept
unknown. A transport failure must not turn a last valid catalog into an empty
one. Metadata checks do not establish that a subscription is signed in; use
the provider's own client login flow.

## Client integration

```sh
modelhub-kit integrate --client codex
modelhub-kit integrate --client codex --apply
modelhub-kit export-acp
```

Supported integration targets are `codex`, `claude`, `opencode`, and `cursor`.
Inspect the default plan before applying it. Unrelated settings, disabled
entries, and custom definitions are preserved. MCP server URLs and ACP stdio
agent commands are separate configuration surfaces. Use environment-variable
names for credentials; do not put credential values in a shared example.

## Cache budget

```sh
modelhub-kit cache
modelhub-kit cache --apply
```

The default budget is 2 GiB for kit-owned disposable downloads. This command
does not claim to cap every third-party application's cache or delete active
environments, model weights, browser profiles, messages, or user files. The
kit writes bounded metadata receipts instead of retaining updater logs and
backup trees.

## Daily background maintenance

`schedule` is a plan. `schedule --apply` registers an owned per-user job.
`schedule --remove` plans removal; `schedule --remove --apply` removes it.

| Platform | Scheduler and policy |
| --- | --- |
| Linux | User systemd timer; daily, persistent catch-up, up to 30 minutes of jitter. The oneshot service uses nice 10, idle I/O, a one-core CPU quota, and 2 GiB memory-high pressure handling. |
| macOS | User LaunchAgent; every 24 hours, Background process type, low-priority I/O, nice 10, no run-at-load. |
| Windows | Interactive-user task every 24 hours, with a hidden `wscript` launcher. No administrator account, saved password, or visible terminal window is required. |

These settings apply only to maintenance. They do not place limits on an
interactive harness. Linux's persistent timer may catch up on missed work
when the user scheduler becomes available. Windows maintenance runs when the
user has an interactive logon and the scheduler can access the application.

Job files and native definitions are tied to an ownership receipt. A modified
or unowned job is preserved. Running maintenance is not stopped to update a
definition: the change stays queued until an idle application of the plan.
Removing a schedule stops only its idle timer or removes its idle registration;
it does not kill an interactive application. An unavailable user scheduler
returns an actionable status rather than enabling a system-wide service.

Inspect native scheduler state when needed:

```sh
systemctl --user status org.modelhub-kit.maintenance.timer
launchctl print gui/$(id -u)/org.modelhub-kit.maintenance
```

Use only the command appropriate to your operating system. On Windows, the
generated task name starts with `ModelHubKit-` and includes a per-state-directory
identifier; the schedule result reports the full name. VBScript and Windows
Script Host must be available for the hidden launcher.

## Fleet work

```sh
modelhub-kit fleet --inventory /path/to/your-inventory.json
modelhub-kit fleet --inventory /path/to/your-inventory.json --apply
```

Start with the empty example inventory and enroll your own trusted SSH aliases.
Keep private keys in your SSH agent or SSH configuration. Host verification
must remain enabled. The actual remote OS and boot identity are checked before
writing, so another OS at the same address is queued rather than treated as
the enrolled boot. Offline and busy nodes remain queued for a later attempt.

No live model requests are needed for these maintenance checks. Report a
failure with its sanitized status and the operation that produced it; do not
attach credentials, raw environments, account databases, or personal logs.

The installer uses the same per-user writer lock as maintenance. A busy writer queues installation with exit code 75; retry when that maintenance run finishes. Existing files are checked again against their original content and filesystem identity immediately before replacement or removal. Concurrent custom changes are preserved. An unavailable Windows Task Scheduler query never counts as proof that a task is absent.
