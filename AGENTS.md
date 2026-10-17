# Working on ModelHub Kit

Keep this repository generic. Never add device inventories, real hostnames or addresses, account details, chats, conversation history, credentials, authentication files, database snapshots, logs, backups, or recovered projects. Runtime state belongs outside the checkout. Examples must start empty and contain environment-variable names rather than credential values.

Use official current documentation for changing CLI interfaces and package layouts. Preserve exact upstream model IDs, capability metadata, and working reasoning choices. Never infer that a missing selector means a model does not support an effort. Keep unknown capabilities unknown and retain the last valid catalog after transport failures.

Update existing tools only after proving ownership of the active installation. Compare stable versions before downloading. Preserve custom launchers, disabled entries, user settings, and newer installed versions. Missing tools require an explicit selection and installation request. Never set a global npm prefix or bypass optional platform dependencies. A current tool must not be reinstalled on every timer run.

Never terminate interactive sessions or restart an app to make an update appear successful. Queue busy or unknown tool states. Detached applications must not inherit writer locks. Bound read-only probes separately from vendor mutations: keep custody of an install and its descendants until natural completion, without a timeout kill or automatic replay. Queue mutations when that custody is unavailable. Use kernel locks, atomic configuration writes, checksummed official native assets, no retained backups, and overwritten metadata receipts rather than growing logs. Cache accounting is not proof of physical disk reclaimed; never delete active runtime environments.

Before changing a remote machine, verify strict SSH identity and actual OS/boot. One address can boot several operating systems. Offline, mismatched, and busy systems remain queued. Do not distribute private SSH keys or disable host verification. Background scheduling limits apply only to maintenance, never interactive harnesses.

Persist the original operation identity before sending a mutating SSH request. A disconnected observer does not establish remote completion. Resolve that operation only from its matching terminal receipt; preserve unknown outcomes instead of starting another install.

MCP uses the configured server URL and native client authentication. ACP uses the agent's native stdio command. Do not label one protocol as the other, launch every agent during sync, or perform paid inference to check metadata. Native sign-in remains user-owned; do not copy login files or enable API-key fallbacks for subscription tools.

Run focused local checks for the behavior changed. Use small fixtures and no live model calls. Run `python3 scripts/audit-public.py` before committing and before publishing. Audit every tracked revision; do not import private Git history. Never add AI co-author trailers or automatic attribution to commits. Preserve the repository owner's chosen author identity.
