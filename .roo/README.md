# Roo Code configuration

Use the files in this directory with Roo Code to route model calls through the sanitized LiteLLM proxy.

- Import `.roo/roo-code-settings.json` using Roo Code's settings import/export
	control, or copy its fields into the Roo Code provider settings.
- API provider: OpenAI (custom base URL)
- Base URL: http://localhost:4000/v1
- Model: cloud-sanitized-auto
- API key: sk-dummy

The custom instructions in the room mode and Cline config enforce the same safety rule: do not send raw secrets or PII to the upstream provider.

Start the stack before importing the settings:

```bash
./update_models_and_run.sh
```

Check that the model is available:

```bash
curl -H 'Authorization: Bearer sk-dummy' http://localhost:4000/v1/models
```

If the installed Roo Code version does not expose a JSON import action, use
the same values manually in its OpenAI-compatible provider form. The JSON file
is intentionally limited to provider settings and does not contain secrets.
