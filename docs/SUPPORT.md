# Supported products and update channels

A registered tool is not automatically installed. Select a tool explicitly with
`update --tool <id> --install-missing` to install it. Ordinary maintenance updates
only configured, recognized installations. Custom launchers, newer versions,
unknown ownership and active sessions are preserved. A queued result describes a
specific unmet guard; it does not mean that the vendor lacks that operating system.

## Terminal tools

| IDs | Supported installation/channel |
| --- | --- |
| `codex`, `claude`, `opencode`, `qwen`, `pi`, `copilot`, `grok` | Owned npm installations; official package stable `latest` metadata. Codex verifies its optional native runtime. |
| `paseo`, `t3`, `kilo`, `dsh`, `cline`, `kimi`, `crush`, `openclaw`, `glm-acp-agent` | Owned npm installations using each project's published package and command. Installing a server package does not start its server. |
| `codex-acp`, `claude-acp` | Official `@agentclientprotocol` npm adapters; explicit installation only. MCP/ACP configuration never installs or starts them. |
| `devin-cli` | Native macOS/Linux/Windows x64 and arm64 bundles from Devin's stable manifest, with publisher SHA-256. |
| `cursor-cli` | Native macOS/Linux/Windows x64 and arm64 bundles from the versioned URL in Cursor's official installer metadata. The installer script is never executed. |
| `muse` | Native macOS/Linux/Windows x64 and arm64 binaries from Meta's public stable channel and version-bound manifest, with publisher SHA-256 and size. |
| `omp` | Existing recognized standalone macOS installation, with official GitHub/build-service SHA-256 metadata. |
| `droid` | Existing recognized Unix native installation, with Factory's official SHA-256. The vendor process-killing installer is never run. |

Generic npm mutations use descendant custody on Linux and Windows. On macOS,
discovery remains available, but those mutations queue until a suitable native
backend exists. The separate verified atomic native adapters remain available.

T3's upstream CLI does not publish an Intel macOS binary. Its npm adapter reports
`unsupported-vendor-platform` there; the separate upstream Electron desktop app
has a different platform matrix. Native Windows OMP/Droid maintenance remains a
queued native-channel task rather than being misrouted through another product.
Hermes is not registered or reinstalled.

The three portable native terminal adapters use kit-owned version directories and
one current ownership receipt. They preserve an unrelated existing installation;
a version string or command filename alone does not authorize replacing it.
The command names remain `devin`, `cursor-agent`, and `muse`. The kit never takes
ownership of the shared `agent` alias or copies subscription login files.

Native downloads are bounded and staged on the same filesystem as their managed
release. Paths, ordinary file types, expansion size, architecture and the exact
candidate version are checked before promotion. Current versions require no
artifact download. A failed candidate leaves the working launcher intact.
Superseded kit-owned files are retired only after exact manifest and fresh idle
checks. Unknown additions are preserved. A deferred retirement remains in the
single receipt and blocks further replacements until resolved, avoiding a growing
backup/version collection.

A protected pending promotion records the verified candidate manifest, exact
staging directory identity and previous launcher/receipt before publishing a
version directory. The next update resolves that same transaction before any
new discovery or download, including interruptions between pointer publication
and the ownership receipt. Only the matching idle candidate can finish; active,
unknown, replaced or modified files remain queued. A late busy state keeps the
same recorded preparation for the next idle attempt. Its download staging is
removed after verified completion, and the one pending record is then cleared.
Legacy unrecorded installation directories are never adopted or swept.

Devin and Muse provide publisher checksums. Cursor's CLI installer currently
publishes an exact version-bound HTTPS URL without a publisher checksum. Its
receipt therefore says `official-https-sha256-readback`: the kit verifies the
vendor origin, hashes the received bytes and verifies the staged readback. This
is not described as an independently published vendor digest.

## Desktop applications

These are separate products, not aliases for their terminal agents:

| ID | Native update policy |
| --- | --- |
| `cursor-desktop` | Discover actual Cursor product metadata; preserve CLI-only installations. Use a verified native package/cask or the exact Windows Inno manifest. |
| `devin-desktop` | Discover Devin Desktop/Windsurf identity and its own version; the vendor's transitional package/repository names are accepted only through an owning native channel. |
| `grokbot` | Discover Grok Bot's actual Electron package identity. Its platform feeds and installers are distinct from the Grok terminal package. |

Linux signed apt/dnf installations, recognized owned Arch packages, official
Homebrew casks and Windows Cursor/Devin Inno packages have native adapters. Package metadata must identify
the actual installation and vendor channel. Desktop version discovery reads
static package/bundle/registry metadata; it never opens the GUI with `--version`.
Generic Homebrew mutations currently queue when descendant custody is unavailable.
Busy or unknown applications defer before metadata-refresh mutations and are
checked again immediately before the native transaction. Applications are not
closed, restarted, or opened to verify an update.

On Linux, unreadable live process executable metadata leaves activity unknown,
including permission failures for another account's processes. Maintenance queues
rather than assuming those processes are unrelated. A generic system Electron
process also leaves activity unknown because its executable can load the selected
app from a separate ASAR directory. System-package maintenance requires verified
elevated process visibility through an administrator-managed, root-owned kit
installation with separate root-owned configuration and state; unresolved
shared-runtime activity still queues. Do not elevate a user-writable kit wrapper
or disable process checks to bypass this guard.

Arch updates authenticate the publisher key, signed release and package hashes
for Cursor/Devin, or the exact official Grok Bot version/commit feed and downloaded
payload digest. Vendor Debian payloads are extracted without executing maintainer
scripts and repackaged through pacman. Existing package metadata, launchers,
checksums and unrelated retained files must match; database locks, boot identity,
live processes and retained files are checked again before the transaction.
Grok Bot uses the complete vendor Electron bundle. Static ELF/ASAR/fuse checks
prevent a Node-mode probe from accidentally starting its GUI. System Electron
adaptations require the existing launcher/dependency and matching native ABI.
No adapter adds `--no-sandbox` or weakens the operating system. A missing Arch
application currently requires an existing verified installation channel; the
kit does not invent an AUR package or overwrite unowned files.

Signed drag-installed macOS applications have a separate DMG adapter when no
Homebrew receipt owns the bundle. It uses official version-bound release metadata,
checks the disk image and candidate Developer ID signature, and requires signer
and bundle identity continuity with the installed app. The bundle is copied into
a private directory on the same volume and promoted with a native atomic exchange.
Explicit missing installations target the user Applications directory. Read-only,
non-browsing mounts are detached in cleanup; low free space, unsigned/custom apps,
signer changes, missing permissions and busy applications queue safely. It does
not convert an existing Homebrew-owned application to a direct installation.

Windows Cursor/Devin requires a matching registered installation, an exact winget
manifest, vendor HTTPS origin, SHA-256, Inno installer type and explicit flags
that disable application closing, restarting and post-install launch. Grok Bot's
published Windows installer does not establish those same unattended guarantees;
that route remains an explicitly reported native-app update task rather than
assuming its `/S` flag has Inno semantics. Native Windows terminal products above
use their checked bundles and do not inherit this desktop limitation.

A missing desktop requires explicit selection and a verified installation channel.
The kit does not silently add system repositories, bypass package holds, disable
sandboxing, or replace arbitrary unowned AppImage/DMG trees. Unrecognized portable app
locations remain queued until their ownership and safe replacement contract are
established. See the result's `reason` and use the vendor's native UI when needed.

## Metadata and integration are separate

`doctor` and integration can read owned tool receipts through
`updater.describe(name, definition, state)`, without starting agents or GUI apps.
Existing npm commands can also supply a version through their exact resolved
official package and declared bin metadata. This grants no update ownership.
Custom launchers and unverified Windows command shims remain unknown; native
configuration migrations must not guess a schema from a similarly named command.

Catalog metadata preserves supplied model IDs and working reasoning choices.
Native provider projection and MCP/ACP client adapters have their own supported
client list. Being an updater target does not imply that every protocol or model
configuration surface is interchangeable.

## Official references

- [Devin CLI](https://docs.devin.ai/cli) and [Devin Desktop](https://docs.devin.ai/desktop/install).
- [Cursor CLI installation](https://docs.cursor.com/en/cli/installation), [current CLI overview](https://cursor.com/docs/cli/overview), and [desktop installation](https://cursor.com/docs/get-started/quickstart).
- [Muse Code](https://dev.meta.ai/docs/muse-code).
- [Grok Bot deployment feeds and platforms](https://prod.cursor.com/docs/grok-bot/deployment).
- [T3 release/platform contracts](https://github.com/pingdotgg/t3code/blob/main/docs/operations/release.md).
- [Codex ACP](https://github.com/agentclientprotocol/codex-acp) and [Claude Agent ACP](https://github.com/agentclientprotocol/claude-agent-acp).
- [winget upgrades](https://learn.microsoft.com/windows/package-manager/winget/upgrade), [Inno Setup flags](https://jrsoftware.org/ishelp/topic_setupcmdline.htm), and [Homebrew commands](https://docs.brew.sh/Manpage).

Every registry entry also links its official package/project source. Native
adapter tests use small local fixtures, not paid inference or live installers.
