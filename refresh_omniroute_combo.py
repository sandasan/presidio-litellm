#!/usr/bin/env python3
"""Keep the Hermes cloud-auto combo limited to currently usable tool models."""

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
COMBO_NAME = "cloud-auto"
SUCCESS_TTL = 300
FAILURE_COOLDOWN = 900
PROBE_TIMEOUT = 30

CANDIDATES = [
    {"provider": "mistral", "model": "mistral-small-latest", "weight": 5},
    {"provider": "gemini", "model": "gemini-flash-latest", "weight": 4},
    {"provider": "cerebras", "model": "qwen-3.8-27b", "weight": 5},
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

    def probe(self, model: str) -> tuple[bool, str]:
        payload = {
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
                    if "data: [DONE]" in text:
                        if '"tool_calls"' in text and '"finish_reason":"tool_calls"' in text:
                            return True, "tool-call stream completed"
                        return False, "stream completed without tool_calls"
                    if len(text) > 512_000:
                        return False, "probe response exceeded 512 KiB"
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return False, str(exc)
        text = "".join(chunks)
        if '"tool_calls"' in text and '"finish_reason":"tool_calls"' in text:
            return True, "tool-call stream completed without DONE marker"
        return False, "stream ended without [DONE]"

    def update_combo(self, models: list[dict[str, Any]]) -> None:
        combos = self.request("GET", "/api/combos").get("combos", [])
        combo = next((item for item in combos if item.get("name") == COMBO_NAME), None)
        payload = {"name": COMBO_NAME, "strategy": "auto", "models": models}
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


def run_once() -> int:
    load_env()
    route = OmniRoute()
    state = load_state()
    now = int(time.time())
    active = []

    try:
        route.login()
    except (HTTPError, URLError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        log(f"login failed: {exc}")
        return 1

    for candidate in CANDIDATES:
        key = f"{candidate['provider']}/{candidate['model']}"
        previous = state.get(key, {})
        checked = int(previous.get("checked", 0))
        ttl = SUCCESS_TTL if previous.get("ok") else FAILURE_COOLDOWN
        if now - checked < ttl:
            if previous.get("ok"):
                active.append(candidate)
                log(f"cached OK: {key}")
            else:
                log(f"cooldown: {key} ({previous.get('reason', 'failed')})")
            continue

        ok, reason = route.probe(candidate["model"])
        state[key] = {"checked": now, "ok": ok, "reason": reason}
        if ok:
            active.append(candidate)
            log(f"OK: {key} ({reason})")
        else:
            log(f"FAIL: {key} ({reason})")

    save_state(state)
    if not active:
        log("no healthy tool-capable models; keeping the existing combo")
        return 1

    try:
        route.update_combo(active)
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        log(f"combo update failed: {exc}")
        return 1
    log("cloud-auto updated: " + ", ".join(item["model"] for item in active))
    return 0


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