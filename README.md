# ModelHub Kit

Lightweight maintenance for coding CLIs, model catalogs, and native MCP/ACP configuration across Linux, macOS, and Windows. Python 3.11+, standard library, no runtime dependencies.

## Install

Download or clone this repository, inspect it, and run:

```sh
sh scripts/install.sh
```

Windows PowerShell:

```powershell
& .\scripts\install.ps1
```

The installer creates a per-user command and initializes an empty personal configuration outside the checkout. It preserves existing configuration and compatible runtimes. Use `--prerequisites` on Unix or `-Prerequisites` on Windows to explicitly install missing prerequisites through a supported native package manager. Node 22+ is needed for npm tools; unsupported package-manager versions produce an actionable result. [Installation instructions](docs/INSTALL.md) cover release selection, paths, prerequisites, PATH setup, initialization, optional scheduling, kit upgrades and removal.

```sh
modelhub-kit doctor --json
modelhub-kit update --dry-run
modelhub-kit update --tool codex --install-missing
modelhub-kit schedule --apply
```

## What it does

- Compares official stable versions and updates the owned installation actually used by the selected command. Current versions are left installed, missing tools require an explicit selection, and custom or newer installations are preserved.
- Manages selected npm harnesses and ACP adapters, native Devin CLI, Cursor CLI and Muse Code, plus separate Cursor, Devin and Grok Bot desktop update channels. [Product support](docs/SUPPORT.md) lists ownership, platform and vendor constraints. Current or newer installations do not trigger an artifact download.
- Serializes mutation with a kernel lock. Active tool sessions defer package replacement. Read-only probes have deadlines; supported vendor installs retain custody of their descendants until natural completion. Unsupported generic mutations queue. Native Droid updates bypass the vendor installer's process-kill step.
- Refreshes user-selected HTTPS model endpoints with ETag/Last-Modified validators. Invalid/truncated responses retain the last good catalog. Exact model IDs and all supplied reasoning metadata remain intact; unknown capabilities stay unknown.
- Plans native MCP configuration for Codex, Claude Code, OpenCode, Cursor, Qwen Code, Copilot, Devin CLI and Muse Code. Owned entries preserve unrelated settings and use native credential references. Unsupported header interpolation queues for native authentication.
- Projects validated model metadata into supported native provider configurations and registers ACP agents in Paseo and T3. Exact model IDs and representable reasoning efforts remain intact; an unrepresentable choice queues the projection instead of clipping it. Separate Codex profiles leave the default subscription configuration untouched. [Native integrations](docs/INTEGRATIONS.md) explain each client boundary.
- Exports ACP agents as native stdio command manifests with environment-variable names. MCP HTTP URLs and ACP stdio commands remain distinct. Sync does not start agents or perform inference.
- Caps its own npm download cache at a configurable 2 GiB target when idle. Installed tools, external user caches, live environments, browser profiles, conversations, and authentication stores are outside that cleanup scope. Busy/unknown cache users defer cleanup.
- Maintains or explicitly installs the kit through trusted SSH aliases, with finite checksummed source delivery and actual OS/boot checks before writes. Offline, mismatched and busy hosts remain queued; dual boots are separate enrollments. Private keys are never distributed.
- Offers optional daily background scheduling: Linux user systemd, macOS launchd, and a hidden Windows scheduled task. Interactive harnesses receive no file, thread, CPU, or memory limits from the kit.

## Personal configuration

Edit your generated configuration, starting from [the empty example](examples/config.json). Model endpoints, MCP URLs and fleet aliases are supplied by each user; no shared service, account, credential, machine inventory, or subscription is shipped.

Catalog source records use `id`, `url`, and optional `bearer_env`. The URL must be HTTPS; authentication is read at runtime from the named environment variable. Only `/models`-style `data` or `models` arrays are accepted. Conditional requests are bound to both the source and credential scope. Configure `model_providers` to project a source into supported native harnesses; these records require `source`, `base_url`, `protocol` and an explicit `credential_env` name, preventing accidental use of another account's credentials. Inference uses your configured provider directly, without a hosted ModelHub gateway. A separately installed local protocol adapter may use a literal loopback HTTP base URL; see [direct and local routing](docs/INTEGRATIONS.md#direct-and-local-routing).

MCP server records use an HTTPS `url` and optional `bearer_env`, or a literal stdio `command`, `args` and `env_names`. Keep credential values out of configuration and URLs. Inspect a plan before applying:

```sh
modelhub-kit sync
modelhub-kit integrate --client codex
modelhub-kit integrate --client codex --apply
modelhub-kit project --client opencode
modelhub-kit project --client opencode --apply
modelhub-kit register-acp --client paseo
modelhub-kit register-acp --client paseo --apply
modelhub-kit export-acp
modelhub-kit cache
modelhub-kit maintain
```

Automatic native configuration is opt-in through `integrations`, for example `{"opencode":{"mcp":true,"models":true},"paseo":{"acp":true}}`. Daily maintenance refreshes catalogs before applying those surfaces, preserving custom changes and queueing active clients. The default integration map is empty.

For fleets, make your own inventory outside Git from [the empty fleet example](examples/fleet.json). Each host specifies an existing trusted SSH `alias`, expected `os` (`linux`, `linux:arch`, `macos`, or `windows`) and optional `boot` identity reported by `doctor`. Use an OS-specific SSH alias for each boot. The kit never copies SSH keys or bypasses first-time trust.

```sh
modelhub-kit fleet --inventory /path/to/your-inventory.json
modelhub-kit fleet --inventory /path/to/your-inventory.json --apply
modelhub-kit fleet --inventory /path/to/your-inventory.json --source /path/to/reviewed-source --enroll
modelhub-kit fleet --inventory /path/to/your-inventory.json --source /path/to/reviewed-source --enroll --apply
```

[Fleet installation](docs/FLEET.md) explains optional prerequisite installation and scheduling. Source delivery includes only the reviewed application allowlist; it excludes local configuration, state and repository history.

To upgrade ModelHub Kit itself, review a newer published source release and rerun its installer; `update` manages selected tools. `modelhub-kit uninstall` plans removal, and `uninstall --apply` removes only the idle receipt-owned kit application/launcher/schedule. Configuration, vendor tools and native integrations stay in place. [Upgrade and removal instructions](docs/INSTALL.md#upgrade-the-kit-itself) include Windows and the narrow optional receipt cleanup.

[Operations](docs/OPERATIONS.md) explains background behavior, preservation, repairs and queued work. [Agent instructions](AGENTS.md) define the same rules for contributors and automated agents.

## Development and publication

```sh
python3 -m unittest discover -s tests -v
python3 scripts/audit-public.py
```

Tests use local fixtures and owned test children, never a paid model. The publication audit scans tracked content and every reachable Git revision for credential patterns, personal paths/addresses, runtime artifacts, and co-author trailers. An optional private denylist can add operator-specific terms without embedding them in this repository. Review is still required: pattern matching alone cannot prove absence of every possible secret.

This repository begins with fresh history. It contains generic source, installation/operation instructions, empty examples and focused local checks. Runtime state and private deployment history are excluded. The chosen Git author and repository owner remain visible as normal public Git metadata.

Official references: [npm global installation](https://docs.npmjs.com/cli/commands/npm-install), [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports), [Codex MCP](https://developers.openai.com/codex/mcp), [Claude Code MCP](https://code.claude.com/docs/en/mcp), [OpenCode MCP](https://opencode.ai/docs/mcp-servers/), [Cursor MCP](https://cursor.com/docs/mcp), [ACP](https://agentclientprotocol.com), [Windows maintenance process trees](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects). Per-tool source links are in `modelhub_kit/tools.json`.

MIT licensed.
