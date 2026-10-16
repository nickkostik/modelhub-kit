# Native integrations

Integrations are opt-in and run under the kit's existing writer lock. Planning
is read-only. Changed native settings wait while the corresponding runtime is active or its
process state is unknown; current/no-op configurations need no process scan.
Applying changes never starts agents, calls models, signs in,
changes the selected model, or reinstalls software. Install only the explicitly
selected adapter or server tools before configuring their commands.

## MCP

Configure each server as one of:

```json
{
  "mcp_servers": {
    "remote-docs": {"url": "https://mcp.example.invalid/mcp", "bearer_env": "EXAMPLE_TOKEN"},
    "local-docs": {"command": "example-mcp", "args": ["--stdio"], "env_names": ["EXAMPLE_TOKEN"]}
  }
}
```

Only credential environment **names** are stored. Values are never read or
copied. Commands and argument elements remain literal; the kit does not execute
them during synchronization.

| Client | Native configuration |
| --- | --- |
| Codex | `.codex/config.toml`, receipt-owned `mcp_servers` block |
| Claude Code | `.claude.json`, `mcpServers` |
| OpenCode | `.config/opencode/opencode.json`, `mcp` local/remote definitions |
| Cursor | `.cursor/mcp.json`, `mcpServers` |
| Qwen Code | `.qwen/settings.json`, `mcpServers`, HTTP `httpUrl` |
| Copilot CLI | `.copilot/mcp-config.json`, local/http definitions |
| Devin CLI | Before v3000.3: user `config.json`; v3000.3+: dedicated `mcp_config.json` |
| Muse Code 1.3 | Existing `mcpServers` or legacy `mcp_servers`; never both |

Devin's user directory is `.config/devin` on Unix and `%APPDATA%\devin` on
Windows. Its header references use `${env:NAME}`. Muse's legacy HTTP transport
is `streamable_http`; canonical entries use `type: streamable-http`. Muse 1.3
interpolates environment references in stdio `env` only: bearer-protected remote
entries queue for native OAuth rather than sending a literal placeholder.
Copilot bearer-header and stdio environment interpolation are not assumed and
queue when credential references are requested. Public
remote endpoints and stdio entries remain configurable for these clients.

Unknown version-dependent schemas, ambiguous roots, custom entries, modified
owned fields and concurrent file edits fail closed. Unrelated settings,
additional entry fields and explicit disablement remain intact. Automatic
version discovery reads verified installation metadata without launching CLI
agents. Existing npm installs can be recognized by their actual resolved command,
exact official package and version metadata; this does not adopt ownership or
authorize an update of that installation. `client_versions` is an explicit operator override for a reviewed
schema, not a request to update a package.

## Providers and reasoning

A provider consumes one configured, validated last-good catalog source:

```json
{
  "catalog_sources": [{"id": "example", "url": "https://catalog.example.invalid/models"}],
  "model_providers": {
    "example": {
      "source": "example",
      "base_url": "https://api.example.invalid/v1",
      "credential_env": "EXAMPLE_ACCESS_TOKEN",
      "protocol": "openai-responses"
    }
  }
}
```

Protocols are `openai-responses`, `openai-completions` and
`anthropic-messages`. The base URL and explicit credential reference belong to
that custom provider. Native subscription credentials are never reused for it.
Catalog identity must still match its configured source URL, credential
reference and current credential scope. Source-scope checks hash a domain-separated
credential value without storing or printing it; a rotated or missing source
credential cannot project the previous account's catalog. Refresh failure retains the last good catalog; it does not erase
native models.

OpenCode receives separate `kit-` providers, exact IDs, supported limits and
all advertised effort variants. Existing wire variant objects remain intact.
Anthropic effort projection requires explicit wire variants instead of guessing
thinking budgets. Pi receives separate providers in `models.json`; an explicit
`thinkingLevelMap` can map native picker levels to different provider strings.
If Pi's native keys cannot represent every advertised effort, the projection
queues without clipping the source or modifying the current provider.

Codex receives separate `kit-<provider>.config.toml` profiles and owned JSON
catalogs, using a local native model-metadata template. Select such a profile
explicitly when wanted. The main config, subscription model cache and auth files
are not edited. Custom providers set `requires_openai_auth = false` and use only
the explicitly configured `env_key`. Missing native metadata queues safely.

Paseo adds `additionalModels` only for a verified `opencode acp` agent bound to
the matching receipt-owned OpenCode provider. Exact native
`provider/model/variant` IDs retain each working effort while live discovery
remains enabled. Other agents retain their own ACP discovery; undocumented
model-ID or credential binding is queued instead of guessed. T3 gets model and
reasoning options from its ACP agent. The kit does not invent undocumented
provider formats for Devin, Muse, Claude Code or Cursor; these surfaces retain
native/ACP discovery and return a specific queued result for unsupported custom
provider projection.

## ACP consumers and adapters

```json
{
  "tools": ["codex-acp"],
  "acp_agents": {
    "example": {"command": "codex-acp", "args": [], "env_names": []}
  }
}
```

`codex-acp` and `claude-acp` are separately selected updater tools. Their binaries
are `codex-acp` and `claude-agent-acp`. Registration does not install or run them.
Paseo registrations use `agents.providers.kit-<id>` with `extends: acp` and a
literal command array. T3's reviewed provider-instance schema uses driver
`acpRegistry`, source `local`, `commandPath` and `commandArgs`; custom instance IDs,
preferences and disabled instances remain intact. A changed T3 configuration
queues while any T3 runtime is active or its process state is unknown. Already
current configuration can be verified without a handoff or restart.

Removing a provider or ACP agent from the kit configuration also visits its
existing ownership receipts, including when the last entry is removed. Unchanged
owned routes retire while idle. Customized or disabled routes and native settings
that still reference them queue for review; their receipts stay intact. Codex
retirement checks the separate owned catalog and profile before removing either,
and preserves the main config, subscription cache and sign-in files.

When removing both a Paseo ACP agent and its projected models, retire the models
first with `project --client paseo --apply`, then run
`register-acp --client paseo --apply`. Otherwise ACP registration explicitly queues
until `additionalModels` has been retired. Maintenance performs additions in ACP
dependency order, so this coordinated removal can require a second maintenance
pass. Keep the relevant integration enabled until retirement completes; disabling
an integration stops its configuration management.

Devin supplies an ACP agent with `devin acp`; it is not a documented ACP consumer.
Configure that literal command as an agent for Paseo or T3. Credentials remain
with the agent; no authentication method is automatically selected. Listed
`env_names` document required inherited environment variables, not stored values.

## Primary references

- [Devin MCP configuration and the v3000.3 migration](https://docs.devin.ai/cli/extensibility/mcp/configuration)
- [Devin's ACP agent role](https://docs.devin.ai/cli/acp/zed)
- [Muse native and legacy MCP spellings and environment behavior](https://meta-models.github.io/muse-code-sdk/next/guides/extend/mcp-servers/)
- [Paseo custom providers, additive models and thinking options](https://github.com/jlovec/paseo/blob/main/docs/custom-providers.md)
- [T3 ACP provider settings](https://github.com/pingdotgg/t3code/blob/main/packages/provider-acp-registry/src/settings.ts) and [provider-instance schema](https://github.com/pingdotgg/t3code/blob/main/packages/contracts/src/providerInstance.ts)
- [Codex profiles, catalogs and credential environment references](https://developers.openai.com/codex/config-reference)
- [OpenCode providers](https://opencode.ai/docs/providers/), [ACP model/variant IDs](https://github.com/anomalyco/opencode/blob/dev/packages/opencode/src/acp/config-option.ts), and [Pi model configuration](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md)
- [Current Codex adapter](https://github.com/agentclientprotocol/codex-acp) and [Claude adapter](https://github.com/agentclientprotocol/claude-agent-acp)
