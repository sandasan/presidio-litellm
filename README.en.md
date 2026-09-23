# presidio-litellm

[Русский](README.ru.md) | [Українська](README.uk.md) | **English**

Docker stack for running the Hermes agent with protected access to free AI models:

```
Hermes Agent -> LiteLLM Proxy -> Presidio (PII anonymization) -> OmniRoute -> AI provider
Open WebUI ->
```

All requests sent to cloud models pass through **Presidio**. Personal data such as
email addresses, names, phone numbers, API keys, database connection strings, and
internal IP addresses are masked before the request reaches a provider.

## Quick start

1. Create the environment file and add provider keys:

   ```bash
   cp .env.example .env
   ```

2. Start the complete stack:

   ```bash
   ./update_models_and_run.sh
   ```

   The script starts OmniRoute, provisions the providers, builds the services,
   and launches Hermes through LiteLLM and Presidio.

3. Open WebUI at <http://localhost:3000>, or use Hermes from a terminal:

   ```bash
   docker exec -it hermes-agent hermes-chat chat \
     --provider custom -m cloud-sanitized-auto
   ```

## Run Hermes for a specific project

The container mounts the `PROJECTS_DIR` host directory as `/workspace`
(`PROJECTS_DIR` defaults to `/home/alexander/projects`). Set it in `.env` when
your projects are stored elsewhere.
The value of `HERMES_GRANTS` is the name of a directory directly inside that
host directory.

For a project at `/home/alexander/projects/my-app`, grant access only to that project:

```bash
# Start the stack from this repository
./update_models_and_run.sh

# In another terminal
docker exec -it \
  -e HERMES_GRANTS=my-app \
  hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
```

Inside the container the project is available as `/workspace/my-app`. Multiple
projects can be granted with a comma-separated list:

```bash
docker exec -it \
  -e HERMES_GRANTS=my-app,another-app \
  hermes-agent hermes-chat chat --provider custom -m cloud-sanitized-auto
```

Use the `hermes-chat` wrapper instead of invoking `hermes` directly. It creates
a block list for all non-granted directories, applies ignore files such as
`.gitignore` and `.aiignore`, and enables the `filegate` filesystem guard.
Files outside the granted project are returned as `Permission denied`.

For a non-interactive task, set the grant explicitly:

```bash
docker exec -e HERMES_GRANTS=my-app hermes-agent hermes-chat \
  -z "Inspect the project and summarize the failing tests"
```

If the project is stored elsewhere, add its host directory to the
`hermes-agent` service volumes in `docker-compose.yml`, then use the mounted
name in `HERMES_GRANTS`.

For a project anywhere on the host, mount its root directory directly:

```bash
./hermes-acp.sh "$(basename "$PWD")" "$PWD"
```

In this mode the project is available as `/workspace`, the grant is `.`, and
the wrapper ensures the stack is running without recreating already running
containers. The VS Code task uses this mode automatically through
`${workspaceFolder}`. For regular chat, set
`PROJECTS_DIR=/absolute/path/to/project` in `.env` and use
`HERMES_GRANTS=.`.

## Hermes in VS Code: ACP

Hermes supports the Agent Client Protocol (ACP) for editor integrations.
For `Poppywu124.hermes-chat` and `joaompfp.hermes-ai-agent`, install the
portable wrapper once:

```bash
./install-hermes-vscode.sh
```

The script creates `~/.local/bin/hermes-acp` as a symlink to the repository
wrapper. Add `~/.local/bin` to `PATH` if necessary, then restart VS Code. Both
extensions use the command name `hermes-acp`, so users do not need to edit a
personal absolute path. The repository settings already contain:

```json
{
  "hermes.path": "hermes",
  "hermes-chat.hermesPath": "hermes-acp",
  "hermes-chat.autoApproveTools": false
}
```

When a folder is open, the extensions invoke `hermes-acp acp`. The wrapper uses
the current VS Code workspace, mounts it as `/workspace`, and delegates startup
to `ensure-stack.sh`.

After the stack is running and the ACP dependencies are installed in the
Hermes image, check the installation:

```bash
docker exec hermes-agent hermes acp --check
```

Start the bundled ACP wrapper from the repository root:

```bash
HERMES_GRANTS=my-app ./hermes-acp.sh
```

The wrapper checks the state of every container through `ensure-stack.sh`. If
the stack is already running, no Docker start command is executed, so switching
between extension tabs does not restart Open WebUI or Hermes. Parallel starts
are serialized with a lock. Do not run a second ACP task at the same time: the
extension itself owns the ACP process.

For image updates, use `update_models_and_run.sh` separately. It intentionally
uses `--force-recreate` and can interrupt active sessions.

The wrapper starts Hermes with the protected LiteLLM route and passes
`HERMES_GRANTS` to the container. The same command can be configured as the
stdio command in an ACP-compatible VS Code extension. The repository also
contains the `Hermes ACP` VS Code task.

The agent must be restricted to the project currently being edited:

```bash
export HERMES_GRANTS=my-app
./hermes-acp.sh
```

## Routing and privacy

LiteLLM exposes protected named routes:

| Route                    | Provider                      | OmniRoute combo |
|--------------------------|-------------------------------|-----------------|
| `cloud-sanitized-auto`   | auto (agent pool, tool-calls) | `cloud-auto`    |
| `cloud-sanitized-chat`   | auto (chat pool, wide free)   | `cloud-chat`    |
| `cloud-sanitized-mistral`| Mistral                       | `cloud-mistral` |
| `cloud-sanitized-gemini` | Gemini                        | `cloud-gemini`  |
| `cloud-sanitized-groq`   | Groq                          | `cloud-groq`    |

```
Hermes / Open WebUI -> LiteLLM (Presidio) -> OmniRoute -> provider
```

OmniRoute selects a live free model and can fail over between providers when a
request receives a rate limit, timeout, or server error. LiteLLM applies the
Presidio guardrail before forwarding the request. Responses are de-anonymized
locally so Hermes can display the original values again.

There are two independent pools with separate failover scopes:

- `cloud-auto` (agent) backs `cloud-sanitized-auto`. It keeps only models that
  pass a streaming **tool-call** probe — Hermes needs tool calls and a large
  context. Candidates: Mistral and Gemini (plus Cerebras once billing is active).
- `cloud-chat` (chat) backs `cloud-sanitized-chat`. It accepts any model that
  streams a plain completion — tool calls are NOT required. It therefore holds a
  wider set of free models, including Groq (short chat contexts are fine) and
  light Gemini/Mistral tiers.

Because the pools are separate, exhausting agent quotas for tool-capable models
does not break Open WebUI: the chat pool falls back to its own set of healthy
free models. `refresh_omniroute_combo.py` probes each pool with its own criteria
and updates the two combos independently. Groq is not part of `cloud-auto`
(its current TPM limits reject normal Hermes contexts) — use the dedicated
`cloud-sanitized-groq` route instead. Unreliable OpenRouter free models are
probed too but usually drop out on SSE stalls, billing 401s, or rate limits.
Provider combos `cloud-mistral`/`cloud-gemini`/`cloud-groq` pin a single provider
while keeping the same Presidio guardrail. Re-run `./provision_omniroute.sh`
after changing a combo definition.

The default model for `update_models_and_run.sh` is set with `DEFAULT_MODEL` in
`.env` (default `cloud-sanitized-auto`); override it by passing `-m <route>` to
`hermes-chat`. The `auto` and `chat` routes always exist; provider routes appear
in LiteLLM only when the matching key is present in `.env` (the route list is
assembled in `docker-compose.yml` from the presence of `{PROVIDER}_API_KEY`; the
keys themselves never reach the LiteLLM container). In Open WebUI, pick any
route from the `/v1/models` dropdown — for ordinary conversation choose
`cloud-sanitized-chat` so agent quota exhaustion does not affect it.

Note: a provider-specific route does not fail over to other providers — when
that provider's free-tier quota is exhausted, the request returns a rate-limit
error (LiteLLM retries the same route). For maximum resilience use
`cloud-sanitized-auto` (agent) and `cloud-sanitized-chat` (chat): each fails
over inside its own pool.

`refresh_omniroute_combo.py` probes targets per pool — the agent pool with a
streaming tool call (requires a tool call plus a terminal SSE event), the chat
pool with a plain streaming completion (requires assistant content; tool calls
not needed). Each combo is updated only with models that passed its criteria.
Results are cached for five minutes after success and fifteen minutes after
failure. If every target fails, the previous combo is kept. Run it once or
continuously:

```bash
python3 refresh_omniroute_combo.py
python3 refresh_omniroute_combo.py --loop --interval 300
```

The agent container has an egress lock. Requests to the internal stack services
are allowed, while arbitrary external HTTP(S) requests from the agent are
blocked. Browser and direct web-surfing features therefore do not work by
design.

Do not treat PII masking as general confidentiality. Code, architecture,
business logic, and internal names are not automatically private unless they
match a configured recognizer or are added to
`litellm-proxy/secrets_map.json`.

## Literal secrets

Values that are not recognized as PII can be listed in
`litellm-proxy/secrets_map.json` (use `secrets_map.example.json` as a template).
They are replaced before the request is sent and restored in the local response.
Keep entries unique and high-entropy; short common words can damage prompts and
responses.

After changing the map, recreate LiteLLM:

```bash
docker compose up -d --force-recreate litellm
```

## Useful checks

```bash
curl http://localhost:5001/health
docker exec hermes-agent hermes acp --check
docker compose ps
```

The complete Russian documentation contains the detailed service reference,
provider notes, file protection details, and troubleshooting guidance:
[README.ru.md](README.ru.md).
