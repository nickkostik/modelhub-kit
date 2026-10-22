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

Protocols are `openai-responses`, `openai-completions`,
`anthropic-messages` and `google-generative-ai`. The base URL and explicit credential reference belong to
that custom provider. Native subscription credentials are never reused for it.
Catalog identity must still match its configured source URL, credential
reference and current credential scope. Source-scope checks hash a domain-separated
credential value without storing or printing it; a rotated or missing source
credential cannot project the previous account's catalog. Refresh failure retains the last good catalog; it does not erase
native models.

### Direct and local routing

The kit has no hosted gateway or Cloudflare Worker dependency. Configure each
provider's own HTTPS API base URL and keep its key in the named environment
variable on that device. Installing or projecting the kit never uploads those
keys to a central service and never enables a fallback gateway.

The configured protocol must match the endpoint and every model projected from
that catalog. Chat Completions, Responses, Anthropic Messages and Google GenerateContent are separate
wire protocols even when the same vendor serves them. Use separate providers
and matching catalogs when a vendor publishes different model families through
different protocols. Check the vendor's current endpoint documentation; a model
name alone does not establish its supported protocol.

Codex custom profiles require the Responses protocol. A provider that only
offers Chat Completions or Messages needs a separately installed local protocol
adapter. Such an adapter can use `http://127.0.0.1:47836/v1` or
`http://[::1]:47836/v1` as the configured base URL, with
`protocol: "openai-responses"`. HTTP is accepted only for literal loopback
addresses; remote APIs still require HTTPS. Catalog downloads and remote MCP
configuration retain their existing HTTPS requirement. The kit does not install,
start, authenticate or verify the behavior of an adapter, and a local adapter's
upstream routing remains its operator's responsibility.

Catalog limits belong to the exact source model. The kit accepts positive integer
`context_window`, `max_input_tokens` and `max_output_tokens` fields, or their
`context`, `input` and `output` equivalents inside `limit` or `limits`. Matching
declarations are normalized consistently. Missing or null values remain unknown;
conflicting, malformed or nonpositive declarations reject the refresh and retain
the last valid catalog. Input capacity is never inferred by subtracting maximum
output from context capacity.

For a Models.dev metadata source, set `provider_id` on its `catalog_sources`
entry to the exact provider key from [the provider catalog](https://models.dev/api.json).
Both that provider dictionary and the `providers` dictionary in
`https://models.dev/catalog.json` are supported. A missing provider or disagreeing
model ID rejects the refresh; another provider's similarly named model is never
substituted. Models.dev's nested zero capacity markers become unknown values.
Changing `provider_id` invalidates the old HTTP validators and cached scope.
The selection supplies metadata, not account entitlement or authentication.

After `sync`, run `python3 -m modelhub_kit limits` to inspect each exact model's
declared base, maximum, active context, input, output and compaction settings.
The report reads cached observations without network calls or writes and shows
their observation time. Absent context is reported as unknown, and stale source
scope is queued for refresh. There are no paid capacity probes, default 500k
caps, assumed million-token windows, or deductions from a model family name.
Provider account metadata and official plan documentation take precedence when
they describe a different route from a public API catalog.

For Codex catalogs, the kit clears all limits and compaction settings borrowed
from the native template before applying the selected source model's metadata.
Known context is retained even when output capacity is unknown. Source-supplied
`max_context_window`, `auto_compact_token_limit` and
`effective_context_window_percent` are preserved after validation; percentages
must be integers from 1 through 100. In the separate owned Codex catalog, an
explicit larger `max_context_window` also becomes that model's active
`context_window`: native Codex otherwise prefers the smaller base and leaves
the maximum unused. OpenCode and Pi likewise receive the greater validated base
or maximum as their active context, including models declaring only a maximum.
The canonical source keeps its distinct base and maximum; native subscription
metadata is unchanged. Each managed model retains its own input and output limits.
Missing maxima never borrow another model's limit, and no global context
override is written. Configure trusted sources whose declared maxima are usable
for that provider and credential: the source fingerprint binds configuration and
credential scope, but does not independently prove provider entitlement or
capacity. When the source supplies no explicit auto-compaction limit, it remains null,
so [Codex derives its native default from that model's own context window](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/openai_models.rs).
The kit sets no global context cap or fixed compaction percentage and does not
reserve the entire maximum output on every turn. Native Codex can still apply
its own headroom and compaction safeguards. The provider or local adapter must
budget the actual serialized request, including tools, reasoning and output;
changing catalog metadata cannot repair an oversized request already in flight.
Codex loads `model_catalog_json` at startup. After finishing active work, restart
the Codex process that uses the catalog to load changed context limits; writing
the catalog alone does not update a running process.

OpenCode receives separate owned providers, exact IDs, supported limits and
all advertised effort variants. Existing wire variant objects remain intact.
Known context does not require a known output ceiling: OpenCode's required
output field receives its [native `0` marker for the client's own output default](https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/src/provider/transform.ts#L710).
The source catalog retains its missing or null output, and an input limit is
included only when explicitly supplied. A model declaring input or output
capacity without a known context queues projection instead of losing those
limits or borrowing another model's context. Entirely unknown limits are omitted.
Anthropic effort projection requires explicit wire variants instead of guessing
thinking budgets. Pi receives separate providers in `models.json`; an explicit
`thinkingLevelMap` can map native picker levels to different provider strings.
If Pi's native keys cannot represent every advertised effort, the projection
queues without clipping the source or modifying the current provider.

### Native Google in OpenCode

`google-generative-ai` projects through OpenCode's `@ai-sdk/google` provider,
with the configured native Google API base URL. This path requires a verified
OpenCode 1.x version at least 1.2, whose bundled Google SDK supports the reviewed
options. Older, unknown and unreviewed major versions queue. Other clients queue
this protocol instead of treating a Google endpoint as OpenAI-compatible. The
kit does not add a protocol gateway.

Advertised `minimal`, `low`, `medium` and `high` efforts use
`thinkingConfig: {"thinkingLevel": "<effort>", "includeThoughts": true}`.
A source-declared default effort also becomes that model's default options.
Models requiring thinking budgets, or efforts such as `max` and custom names,
need explicit source `variants` using the model's documented Google options,
for example `{"thinkingConfig": {"thinkingBudget": 8192, "includeThoughts": true}}`.
The kit preserves those exact variants and does not derive a thinking budget
from the model's output ceiling. An unrepresentable effort or invalid default
queues the projection, preserving the previous provider and every advertised
choice. Each model keeps its own context and output limits.

For the exact official Go base URL
`https://opencode.ai/inference/go/google/v1beta`, a new binding receives the owned
ID `opencode-modelhub-kit-<name>`. This opts into the verified native OpenCode
conversation-header branch: OpenCode supplies `x-opencode-session` for the real
conversation. The kit writes no fixed or random session header. It supplies the
verified client version in `User-Agent` and the required Bearer header, using
the same `{env:VARIABLE}` reference as the native Google SDK's `apiKey`. Only
references are written; the SDK resolves the device's credential at runtime.
Other Google endpoints retain the ordinary `kit-<name>` ID and native SDK
authentication, without Go headers.

Existing owned bindings are never renamed to enter or leave the Go namespace;
such a change queues for explicit review, as do collisions and modified owned
entries. Global `model` and `small_model` settings remain untouched. OpenCode
retains its native auxiliary-model selection when `small_model` is unset; that
selection may use a different provider. Choose `small_model` explicitly in the
native configuration when a particular auxiliary route is required.

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
- [Go endpoints and coding-agent headers](https://opencode.ai/v2/docs/console/go/#endpoints), [native Gemini requests](https://opencode.ai/v2/docs/console/api/inference/#gemini), and [Google SDK options](https://ai-sdk.dev/providers/ai-sdk-providers/google)
- [Reviewed OpenCode 1.2 Google SDK dependency](https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/package.json), [native conversation headers](https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/src/session/llm.ts), and [auxiliary-model selection](https://github.com/anomalyco/opencode/blob/v1.2.0/packages/opencode/src/provider/provider.ts)
- [Current Codex adapter](https://github.com/agentclientprotocol/codex-acp) and [Claude adapter](https://github.com/agentclientprotocol/claude-agent-acp)
