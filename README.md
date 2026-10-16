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

The installer creates a per-user command and initializes an empty personal configuration outside the checkout. It preserves existing configuration and compatible runtimes. Use `--prerequisites` on Unix or `-Prerequisites` on Windows to explicitly install missing prerequisites through a supported native package manager. Node 22+ is needed for npm tools; unsupported package-manager versions produce an actionable result. [Installation instructions](docs/INSTALL.md) cover paths, prerequisites, PATH setup, initialization, and optional scheduling.

```sh
modelhub-kit doctor --json
modelhub-kit update --dry-run
modelhub-kit update --tool codex --install-missing
modelhub-kit schedule --apply
```

## What it does

- Compares official stable versions and updates the owned installation actually used by the selected command. Current versions are left installed, missing tools require an explicit selection, and custom or newer installations are preserved.
- Manages npm installations of Codex, Claude Code, OpenCode, Qwen Code, Pi, Copilot, and Grok. Existing compatible managed standalone macOS OMP and Unix Droid use checksummed native adapters and atomic promotion. Windows native standalone updates remain explicitly queued for their native updater.
- Serializes mutation with a kernel lock. Active tool sessions defer package replacement. Maintenance children are bounded, without app restarts or global process-kill commands. Native Droid updates bypass the vendor installer's process-kill step.
- Refreshes user-selected HTTPS model endpoints with ETag/Last-Modified validators. Invalid/truncated responses retain the last good catalog. Exact model IDs and all supplied reasoning metadata remain intact; unknown capabilities stay unknown.
- Plans native MCP configuration for Codex, Claude Code, OpenCode, and Cursor. Apply changes only through owned entries/receipts, preserving unrelated settings. Each client receives its documented environment-reference syntax for bearer credentials; native OAuth remains available. No credential values are written into client configuration.
- Exports ACP agents as native stdio command manifests with environment-variable names. An MCP HTTP URL is not an ACP transport. Sync does not start agent processes or perform model inference.
- Caps its own npm download cache at a configurable 2 GiB target when idle. Installed tools, external user caches, live environments, browser profiles, conversations, and authentication stores are outside that cleanup scope. Busy/unknown cache users defer cleanup.
- Keeps fleet hosts queued when offline, busy, or booted into another OS. SSH aliases must already be trusted and enrolled. Strict host verification is retained, and actual OS/optional boot guards run before remote maintenance.
- Offers optional daily background scheduling: Linux user systemd, macOS launchd, and a hidden Windows scheduled task. Interactive harnesses receive no file, thread, CPU, or memory limits from the kit.

## Personal configuration

Edit your generated configuration, starting from [the empty example](examples/config.json). Model endpoints, MCP URLs and fleet aliases are supplied by each user; no shared service, account, credential, machine inventory, or subscription is shipped.

Catalog source records use `id`, `url`, and optional `bearer_env`. The URL must be HTTPS; authentication is read at runtime from the named environment variable. Only `/models`-style `data` or `models` arrays are accepted. This refreshes metadata; configure the corresponding provider in your native harness to use its models.

MCP server records map a unique name to `url` and optional `bearer_env`. Keep secrets out of URLs. Inspect a plan before applying:

```sh
modelhub-kit sync
modelhub-kit integrate --client codex
modelhub-kit integrate --client codex --apply
modelhub-kit export-acp
modelhub-kit cache
modelhub-kit maintain
```

For fleets, make your own inventory outside Git from [the empty fleet example](examples/fleet.json). Each host specifies an existing SSH `alias`, expected `os` (`linux`, `linux:arch`, `macos`, or `windows`) and optional `boot` identity reported by `doctor`. Install locally on each OS first; dual/triple boots are separate enrollments. The kit never copies SSH keys or bypasses first-time trust.

```sh
modelhub-kit fleet --inventory /path/to/your-inventory.json
modelhub-kit fleet --inventory /path/to/your-inventory.json --apply
```

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
