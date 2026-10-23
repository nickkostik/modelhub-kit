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

`maintain` serializes a selected-tool update, configured catalog refresh,
explicitly enabled native integrations, and kit-owned cache cleanup under one
writer lock. A busy writer is queued. It
does not install every available tool, start every agent, run inference, or
restart your interactive applications.

Updates require proof that the active installation is supported and owned.
Current tools are not routinely reinstalled. Custom launchers and newer
installed versions are preserved. A failed native asset download must not
replace the working executable. A live old executable can remain until it is
safe to retire; it is an active runtime, not disposable cache.

Version and metadata probes have finite deadlines. Supported Linux and Windows
vendor mutations wait for their owned process trees to finish naturally;
scheduler deadlines do not interrupt them. Generic macOS package-manager
mutations queue when descendant custody is unavailable. The separate verified
atomic application paths retain their own staging guards. A current Codex
installation with a failed health check reports `repair-needed`; use an
explicit `update --tool codex --repair` after reviewing the failure.

Catalog sources are explicitly configured HTTPS sources. Capability metadata
and reasoning choices come from those sources, with unknown capabilities kept
unknown. A transport failure must not turn a last valid catalog into an empty
one. Metadata checks do not establish that a subscription is signed in; use
the provider's own client login flow.

Catalog HTTP failures report only the status, a fixed diagnostic category and
attempt count. Upstream response bodies, URLs and credentials are never copied
into diagnostics. Metadata GETs retry an explicit temporary rejection at most
twice, on the same URL with unchanged authentication and cache validators.
Only HTTP 429/503 or a recognized temporary error code on HTTP 400 qualify;
generic HTTP 400, parameter errors, credential/billing errors, connection errors
and incomplete responses do not retry. `Retry-After` seconds and dates are
honored; waits longer than five seconds or invalid headers defer to a future
maintenance run. This policy applies to metadata discovery only. Model requests
use native harness transports and are not intercepted or replayed by this kit.

Maintenance reports `completed` only when every requested operation is settled.
Deferred or unknown work produces `queued` and exit 75; failed updates or catalog
refreshes produce `failed` and exit 1. A retained last-good catalog keeps native
settings available but does not turn a failed refresh into a successful one.
Plans do not apply pending changes. Routine state is one overwritten receipt,
with no appended execution log or versioned backups.

### Installed source and a running model adapter

The kit projects native provider configuration; it does not run an inference
proxy. If you separately use a local protocol adapter, a successful source
installation does not prove that its running process loaded that source.
Check the adapter's authenticated local health metadata for the process ID,
loaded source revision, handler checksum and configuration identity. Compare
those with the reviewed installed files. A configured service command or a
version on disk cannot establish the identity of an already running handler.

A `501` transport error during compaction can occur before any provider request
when that adapter's active catalog reader lacks the selected route's protocol.
Check the route's declared transport and the loaded reader together. Keep
working reasoning choices intact. Updating model limits, increasing output
tokens or retrying the unchanged request does not repair a missing transport.
Any adapter refresh must use its verified native handoff and preserve active
streams; unsupported or unverified handoffs remain queued.

For permission errors, check access as the adapter's actual service account.
A valid user credential can be unreachable when a separate root-only default
is tried first. Fix optional-path selection in the owning adapter while
preserving errors from explicitly configured credentials. Keep system
credentials private and do not copy them into shared configuration or reports.

## Client integration

```sh
modelhub-kit integrate --client codex
modelhub-kit integrate --client codex --apply
modelhub-kit project --client opencode
modelhub-kit project --client opencode --apply
modelhub-kit register-acp --client paseo
modelhub-kit register-acp --client paseo --apply
modelhub-kit export-acp
```

MCP also supports Qwen Code, Copilot, Devin CLI and Muse Code. Model projection
and ACP consumer support are documented separately in [Native integrations](INTEGRATIONS.md).
Inspect the default plan before applying it. Unrelated settings, disabled
entries, and custom definitions are preserved. MCP server URLs and ACP stdio
agent commands are separate configuration surfaces. Use environment-variable
names for credentials; do not put credential values in a shared example.

Set individual `mcp`, `models` or `acp` booleans in the configuration's
`integrations` map to include those surfaces in maintenance. The empty default
does not modify native harness configuration. Changed shared client settings
wait until the client is idle; an unknown process inventory also queues changes.
Native schema discovery reads metadata rather than launching GUI applications.

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

An unreadable cache subtree or a file disappearing during measurement queues
cleanup with unknown byte counts. A partial scan never counts as within budget,
and the kit leaves the remaining cache untouched until it can measure it.

### Investigating retained installations

Measure installed packages, native version directories and downloaded staging
files separately from caches. An old directory name does not prove an unused
installation: the selected launcher, a service or an active session may still
refer to it. Preserve those references and personal data during cleanup.

Bounded cleanup must advance through its backlog. When a separate root observer
maintains user-owned releases, write its scheduling cursor as the enrolled user
and restore the observer's identity before checking native locks. Do not change
ownership of the user's installation to make a progress write succeed. The
cursor schedules fresh checks; it never authorizes deletion. Keep it as one
overwritten metadata file and verify that successive runs reach later entries.
Budget deferrals alone are not evidence that a directory is unsafe or removed.

For a desktop slowdown, identify the stalled process and its local resource
controls before clearing caches. [Desktop indexing and memory pressure](DESKTOP-PRESSURE.md)
describes optional KDE/Baloo checks and native exclusions for generated state.

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
First-time kit delivery is explicit through `--source <reviewed-source> --enroll`;
it is separate from routine maintenance. [Fleet delivery](FLEET.md) describes
strict trust, finite input, checksums, prerequisites and staging cleanup.

No live model requests are needed for these maintenance checks. Report a
failure with its sanitized status and the operation that produced it; do not
attach credentials, raw environments, account databases, or personal logs.

The installer uses the same per-user writer lock as maintenance. A busy writer queues installation with exit code 75; retry when that maintenance run finishes. Existing files are checked again against their original content and filesystem identity immediately before replacement or removal. Concurrent custom changes are preserved. An unavailable Windows Task Scheduler query never counts as proof that a task is absent.


## Kit upgrades and removal

Tool maintenance does not update ModelHub Kit itself. Select and review a
published source release, then rerun that release's installer. Identical
installations are a no-op; a changed installation defers while another kit
reader/writer is active or cannot be inspected. The kit has no self-update
daemon. See [release selection and upgrades](INSTALL.md#upgrade-the-kit-itself).

`modelhub-kit uninstall` plans receipt-bound removal;
`modelhub-kit uninstall --apply` removes the idle owned schedule, launcher and
checked application files. It preserves personal configuration, installed tools,
native integrations and state. Modified/unknown code or jobs queue; they are
never forcibly removed. Use the same state environment as the owned schedule,
and remove schedules for any additional state directories before uninstalling.
`--clean-state` is a separate opt-in limited to the explicitly marked current
maintenance receipt; it never purges the state directory or a vendor cache.
[Complete removal instructions](INSTALL.md#remove-modelhub-kit) include older
installations, Windows and recovery after an interrupted operation.
