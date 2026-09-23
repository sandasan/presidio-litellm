#!/usr/bin/env python3
"""Keep the Hermes cloud-auto combo limited to currently usable tool models.

Two independent pools are probed and refreshed:

- `cloud-auto` (agent route `cloud-sanitized-auto`) — Hermes needs tool calls and
  a large context, so a target is accepted only when a streaming tool-call probe
  returns a real tool call and a terminal SSE event.
- `cloud-chat` (chat route `cloud-sanitized-chat`) — Open WebUI chat does not
  need tool calls, so a target is accepted on a plain streaming completion
  (non-empty content plus [DONE]). This keeps the chat usable even when agent
  quotas for tool-capable models are exhausted.

Providers without an API key in `.env` are skipped (their targeted models never
run). Cerebras is listed as a candidate: once billing is activated it starts
passing the probe and is added automatically to both combos.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = PROJECT_DIR / ".omniroute-health.json"
AGENT_COMBO = "cloud-auto"
CHAT_COMBO = "cloud-chat"
SUCCESS_TTL = 300
FAILURE_COOLDOWN = 900
PROBE_TIMEOUT = 30

# Агентный пул: обязательны tool-calls и большой контекст.
AGENT_CANDIDATES = [
    {"provider": "mistral", "model": "mistral-small-latest", "weight": 5},
    {"provider": "gemini", "model": "gemini-flash-latest", "weight": 4},
    {"provider": "cerebras", "model": "qwen-3.8-27b", "weight": 5},
]

# Чат-пул: tool-calls не нужны. Здесь шире диапазон провайдеров, включая те,
# что не тянут большие контексты агента (Groq) и нестабильные free-модели
# OpenRouter — они отсеются probe'ом, если не стримят до [DONE].
CHAT_CANDIDATES = [
    {"provider": "mistral", "model": "mistral-small-latest", "weight": 5},
    {"provider": "gemini", "model": "gemini-flash-latest", "weight": 4},
    {"provider": "gemini", "model": "gemini-flash-lite-latest", "weight": 3},
    {"provider": "groq", "model": "openai/gpt-oss-120b", "weight": 4},
    {"provider": "groq", "model": "openai/gpt-oss-20b", "weight": 3},
    {"provider": "groq", "model": "qwen/qwen3.8-27b", "weight": 2},
    {"provider": "mistral", "model": "ministral-8b-latest", "weight": 3},
    {"provider": "cerebras", "model": "openai/gpt-oss-120b", "weight": 3},
    {"provider": "cerebras", "model": "qwen-3.8-27b", "weight": 2},
    {"provider": "openrouter", "model": "qwen/qwen3.8-27b:free", "weight": 1},
    {"provider": "openrouter", "model": "z-ai/glm-5.2:free", "weight": 1},
    {"provider": "openrouter", "model": "nvidia/nemotron-3.5-lightning:free", "weight": 1},
]

POOLS = [
    (AGENT_COMBO, AGENT_CANDIDATES, True),
    (CHAT_COMBO, CHAT_CANDIDATES, False),
]


def load_env() -> None:
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    if not os.getenv("OMNIROUTE_URL"):
        os.environ["OMNIROUTE_URL"] = "http://127.0.0.1:20128"


def log(message: str) -> None:
    print(f"[omniroute-health] {message}", flush=True)


class OmniRoute:
    def __init__(self) -> None:
        self.base_url = os.getenv("OMNIROUTE_URL", "http://127.0.0.1:20128").rstrip("/")
        self.password = os.getenv("OMNIROUTE_INITIAL_PASSWORD", "omniro2026!")
        self.api_key = os.getenv("OMNIROUTE_API_KEY", "sk-omniroute")
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        with self.opener.open(request, timeout=PROBE_TIMEOUT) as response:
            return json.loads(response.read().decode())

    def login(self) -> None:
        result = self.request("POST", "/api/auth/login", {"password": self.password})
        if result.get("success") is not True:
            raise RuntimeError("management API login failed")

    def probe(self, model: str, require_tool_calls: bool) -> tuple[bool, str]:
        if require_tool_calls:
            payload: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": "Use the probe tool exactly once."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "probe",
                            "description": "Health probe",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "probe"}},
                "stream": True,
            }
        else:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                "max_tokens": 40,
                "stream": True,
            }
        request = Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        chunks = []
        try:
            with self.opener.open(request, timeout=PROBE_TIMEOUT) as response:
                while True:
                    chunk = response.read(4096)
                    if not chunk:
                        break
                    chunks.append(chunk.decode(errors="replace"))
                    text = "".join(chunks)
                    if '"error"' in text:
                        return False, "error in stream: " + text.strip()[:160]
                    if "data: [DONE]" in text:
                        if require_tool_calls:
                            if '"tool_calls"' in text and '"finish_reason":"tool_calls"' in text:
                                return True, "tool-call stream completed"
                            return False, "stream completed without tool_calls"
                        if '"content"' in text:
                            return True, "chat stream completed"
                        return False, "stream completed without assistant content"
                    if len(text) > 512_000:
                        return False, "probe response exceeded 512 KiB"
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return False, str(exc)
        text = "".join(chunks)
        if require_tool_calls:
            if '"tool_calls"' in text and '"finish_reason":"tool_calls"' in text:
                return True, "tool-call stream completed without DONE marker"
            return False, "stream ended without [DONE]"
        if '"content"' in text:
            return True, "chat stream completed without DONE marker"
        return False, "stream ended without [DONE] or assistant content"

    def update_combo(self, name: str, models: list[dict[str, Any]]) -> None:
        combos = self.request("GET", "/api/combos").get("combos", [])
        combo = next((item for item in combos if item.get("name") == name), None)
        payload = {"name": name, "strategy": "auto", "models": models}
        if combo:
            self.request("PUT", f"/api/combos/{combo['id']}", payload)
        else:
            self.request("POST", "/api/combos", payload)


def load_state() -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(STATE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(STATE_FILE)


def run_pool(route: OmniRoute, state: dict[str, dict[str, Any]], combo: str,
             candidates: list[dict[str, Any]], require_tool_calls: bool) -> int:
    now = int(time.time())
    active = []
    mode = "agent" if require_tool_calls else "chat"
    for candidate in candidates:
        provider = candidate["provider"]
        if not os.getenv(f"{provider.upper()}_API_KEY"):
            log(f"skip {mode}: {provider} (no API key)")
            continue
        key = f"{mode}/{provider}/{candidate['model']}"
        previous = state.get(key, {})
        checked = int(previous.get("checked", 0))
        ttl = SUCCESS_TTL if previous.get("ok") else FAILURE_COOLDOWN
        if now - checked < ttl:
            if previous.get("ok"):
                active.append(candidate)
                log(f"cached OK ({mode}): {key}")
            else:
                log(f"cooldown ({mode}): {key} ({previous.get('reason', 'failed')})")
            continue

        ok, reason = route.probe(candidate["model"], require_tool_calls)
        state[key] = {"checked": now, "ok": ok, "reason": reason}
        if ok:
            active.append(candidate)
            log(f"OK ({mode}): {key} ({reason})")
        else:
            log(f"FAIL ({mode}): {key} ({reason})")

    if not active:
        log(f"{combo}: no healthy models; keeping the existing combo")
        return 1

    try:
        route.update_combo(combo, active)
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        log(f"{combo} update failed: {exc}")
        return 1
    log(f"{combo} updated ({mode}): " + ", ".join(item["model"] for item in active))
    return 0


def run_once() -> int:
    load_env()
    route = OmniRoute()
    state = load_state()

    try:
        route.login()
    except (HTTPError, URLError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        log(f"login failed: {exc}")
        return 1

    code = 0
    for combo, candidates, require_tool_calls in POOLS:
        code |= run_pool(route, state, combo, candidates, require_tool_calls)

    save_state(state)
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="repeat health checks")
    parser.add_argument("--interval", type=int, default=300, help="loop interval in seconds")
    args = parser.parse_args()
    if not args.loop:
        return run_once()
    while True:
        run_once()
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    sys.exit(main())