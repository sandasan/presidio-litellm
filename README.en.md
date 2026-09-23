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

The container mounts `/home/alexander/projects` on the host as `/workspace`.
The value of `HERMES_GRANTS` is the name of a directory directly inside that
host directory.

For `/home/alexander/projects/my-app`, grant access only to that project:

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

## Hermes in VS Code: ACP

Hermes supports the Agent Client Protocol (ACP) for editor integrations.
After the stack is running and the ACP dependencies are installed in the
Hermes image, check the installation:

```bash
docker exec hermes-agent hermes acp --check
```

Start the bundled ACP wrapper from the repository root:

```bash
HERMES_GRANTS=my-app ./hermes-acp.sh
```

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

The model route is `cloud-sanitized-auto`:

```
Hermes / Open WebUI -> LiteLLM (Presidio) -> OmniRoute -> provider
```

OmniRoute selects a live free model and can fail over between providers when a
request receives a rate limit, timeout, or server error. LiteLLM applies the
Presidio guardrail before forwarding the request. Responses are de-anonymized
locally so Hermes can display the original values again.

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
