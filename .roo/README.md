# Roo Code configuration

Use the files in this directory with Roo Code to route model calls through the sanitized LiteLLM proxy.

- Base URL: http://localhost:4000/v1
- Model: cloud-sanitized-auto
- API key: sk-dummy

The custom instructions in the room mode and Cline config enforce the same safety rule: do not send raw secrets or PII to the upstream provider.
