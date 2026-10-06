#!/usr/bin/env python3
import http.server
import json
import sys
import re
import codecs
import copy
import socket
import hashlib
import os
import sqlite3
import time
import urllib.error
import urllib.request
import ssl
import http.client
from bisect import bisect_right
from errno import EPIPE

PRESIDIO_URL = "http://127.0.0.1:5001"
PRESIDIO_ANALYZE_TIMEOUT_SECONDS = float(
    os.environ.get("PRESIDIO_ANALYZE_TIMEOUT_SECONDS", "60")
)
PRESIDIO_ANALYZE_BATCH_MAX_CHARS = 20000
if PRESIDIO_ANALYZE_TIMEOUT_SECONDS <= 0:
    raise ValueError("Presidio analyze timeout must be positive")
OPENCODE_REAL_IP = "104.21.32.140"
PLACEHOLDER_BODY = (
    r"[A-Z][A-Z0-9_]*_(?:S[0-9a-f]{12}_\d+|\d+(?:_[0-9a-f]{32})?|[0-9a-f]{32})"
)
PLACEHOLDER_PATTERN = re.compile(
    r"(?:<|\\u003c|\\u003C|&lt;)"
    rf"{PLACEHOLDER_BODY}"
    r"(?:>|\\u003e|\\u003E|&gt;)"
)
SESSION_PLACEHOLDER_PATTERN = re.compile(
    r"<(?P<entity>[A-Z][A-Z0-9_]*?)_S(?P<session>[0-9a-f]{12})_(?P<index>\d+)>"
)
LEGACY_UUID_PLACEHOLDER_PATTERN = re.compile(
    r"<(?P<entity>[A-Z][A-Z0-9_]*?)_(?:(?P<index>\d+)_)?(?P<uuid>[0-9a-f]{32})>"
)
UNRESOLVED_PLACEHOLDER = "[unresolved anonymization placeholder]"
MEDIA_TYPES = {"image", "image_url", "input_image", "audio", "input_audio", "video", "file"}
MAPPING_DB_PATH = os.environ.get(
    "OPENCODE_MAPPING_DB", "/var/lib/opencode-bridge/mappings.sqlite3"
)
MAPPING_TTL_SECONDS = int(os.environ.get("OPENCODE_MAPPING_TTL_SECONDS", "0"))
MAX_UNRESOLVED_TOOL_RETRIES = int(
    os.environ.get("OPENCODE_UNRESOLVED_TOOL_RETRIES", "2")
)
if MAX_UNRESOLVED_TOOL_RETRIES < 0:
    raise ValueError("Unresolved tool retry count cannot be negative")

# Context compression settings
DEFAULT_MAX_MESSAGES = int(os.environ.get("OPENCODE_MAX_MESSAGES", "100"))
CONTEXT_COMPRESSION_THRESHOLD = float(
    os.environ.get("OPENCODE_COMPRESSION_THRESHOLD", "0.67")
)  # 2/3 by default
if not 0 < CONTEXT_COMPRESSION_THRESHOLD < 1:
    raise ValueError("Compression threshold must be between 0 and 1")
KEEP_RECENT_MESSAGES = int(os.environ.get("OPENCODE_KEEP_RECENT", "30"))  # Keep last 30 messages

# Model context limits (maximum messages, assuming average 100 tokens per message)
# Based on common model context windows and typical message sizes
MODEL_CONTEXT_LIMITS = {
    # GPT-4 models
    "gpt-4": 80,  # 8K context
    "gpt-4-32k": 320,  # 32K context
    "gpt-4-turbo": 128,  # 128K context
    "gpt-4o": 128,  # 128K context
    "gpt-4o-mini": 128,  # 128K context
    # Claude models
    "claude-3-opus": 200,  # 200K context
    "claude-3-sonnet": 200,  # 200K context
    "claude-3-haiku": 200,  # 200K context
    "claude-3.5-sonnet": 200,  # 200K context
    # Gemini models
    "gemini-pro": 128,  # 128K context
    "gemini-1.5-pro": 280,  # 1M context
    "gemini-1.5-flash": 280,  # 1M context
    # Mistral models
    "mistral-large": 32,  # 32K context
    "mistral-medium": 32,  # 32K context
    "mistral-small": 32,  # 32K context
    "mixtral-8x7b": 32,  # 32K context
    "mixtral-8x22b": 64,  # 64K context
    # Llama models
    "llama-3-70b": 8,  # 8K context
    "llama-3-8b": 8,  # 8K context
    "llama-3.1-405b": 128,  # 128K context
    "llama-3.1-70b": 128,  # 128K context
    "llama-3.1-8b": 128,  # 128K context
    # Groq models
    "llama3-70b-8192": 80,  # 8K context
    "llama3-8b-8192": 80,  # 8K context
    "mixtral-8x7b-32768": 320,  # 32K context
    # Other common models
    "deepseek-chat": 128,  # 128K context
    "deepseek-v4-flash": 280,  # 1M context, limited by daily quota (~200 requests/day)
    "qwen-72b-chat": 32,  # 32K context
    "yi-34b-chat": 4,  # 4K context
    # OpenCode Zen free models (high or unlimited context, may have daily quotas)
    "space-bunny": 500,  # Unlimited context, limited time availability
    "longcat": 500,  # Extended context (Preview), limited time availability
    "mimo": 100,  # Standard context, provider session dependent
    "mimo-v2.5": 100,  # Standard context, provider session dependent
    "ling": 100,  # Standard context, provider session dependent
    "ling-3.0": 100,  # Standard context, provider session dependent
    "nemotron": 100,  # Standard context, provider session dependent
    "nemotron-3.5": 100,  # Standard context, provider session dependent
    "nemotron-3.5-lightning": 100,  # Standard context, provider session dependent
}


def get_model_context_limit(model_name):
    """
    Get the maximum number of messages for a given model.
    Returns the limit from MODEL_CONTEXT_LIMITS or DEFAULT_MAX_MESSAGES.
    """
    if not model_name:
        return DEFAULT_MAX_MESSAGES

    # Normalize model name (remove provider prefixes, version suffixes, spaces)
    normalized = model_name.lower().strip()
    # Replace spaces with hyphens for consistent matching
    normalized = normalized.replace(" ", "-")
    # Remove common prefixes
    for prefix in ["openai/", "anthropic/", "google/", "mistralai/", "meta/", "groq/"]:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    # Remove version suffixes (e.g., -latest, -v1, etc.)
    for suffix in ["-latest", "-v1", "-v2", "-v3", "-001", "-002", "-free", "-preview"]:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]

    # Try exact match first
    if normalized in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[normalized]

    # Try partial match (e.g., "gpt-4o-mini" matches "gpt-4o")
    for model_pattern, limit in MODEL_CONTEXT_LIMITS.items():
        if model_pattern in normalized or normalized in model_pattern:
            return limit

    # Default fallback
    return DEFAULT_MAX_MESSAGES


class MappingStore:
    """Persist PII mappings per OpenCode session, including across bridge restarts."""

    def __init__(self, path, ttl_seconds=MAPPING_TTL_SECONDS):
        if ttl_seconds < 0:
            raise ValueError("Mapping TTL cannot be negative")
        self.path = os.path.abspath(path)
        self.ttl_seconds = ttl_seconds
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        os.chmod(os.path.dirname(self.path), 0o700)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self):
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    parent_session_id TEXT,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS mappings (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    placeholder TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    original_value TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY (session_id, placeholder)
                );
                CREATE INDEX IF NOT EXISTS mappings_by_value
                    ON mappings(session_id, entity_type, original_value);
                CREATE TABLE IF NOT EXISTS placeholder_counters (
                    entity_type TEXT PRIMARY KEY,
                    last_value INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_placeholder_counters (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    entity_type TEXT NOT NULL,
                    last_value INTEGER NOT NULL,
                    PRIMARY KEY (session_id, entity_type)
                );
                """
            )
            os.chmod(self.path, 0o600)
        finally:
            connection.close()

    def prepare_session(self, session_id, parent_session_id=None):
        now = int(time.time())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO sessions(session_id, parent_session_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       parent_session_id = COALESCE(excluded.parent_session_id,
                                                    sessions.parent_session_id),
                       updated_at = excluded.updated_at""",
                (session_id, parent_session_id, now),
            )
            if self.ttl_seconds:
                connection.execute(
                    "DELETE FROM sessions WHERE updated_at < ?",
                    (now - self.ttl_seconds,),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_or_create(self, session_id, entity_type, original_value):
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", entity_type):
            raise ValueError("Invalid Presidio entity type")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT placeholder FROM mappings
                   WHERE session_id = ? AND entity_type = ? AND original_value = ?
                   ORDER BY created_at LIMIT 1""",
                (session_id, entity_type, original_value),
            ).fetchall()
            session_namespace = session_id[:12]
            canonical_pattern = re.compile(
                rf"<{re.escape(entity_type)}_S{session_namespace}_\d+>"
            )
            for row in rows:
                if canonical_pattern.fullmatch(row[0]):
                    connection.commit()
                    return row[0]

            counter = connection.execute(
                """SELECT last_value FROM session_placeholder_counters
                   WHERE session_id = ? AND entity_type = ?""",
                (session_id, entity_type),
            ).fetchone()
            if counter is None:
                numeric_pattern = re.compile(rf"<{re.escape(entity_type)}_(\d+)(?:_[0-9a-f]{{32}})?>")
                session_pattern = re.compile(
                    rf"<{re.escape(entity_type)}_S{session_namespace}_(\d+)>"
                )
                existing = connection.execute(
                    "SELECT placeholder FROM mappings WHERE session_id = ? AND entity_type = ?",
                    (session_id, entity_type),
                ).fetchall()
                last_value = max(
                    (
                        int(match.group(1))
                        for row in existing
                        if (match := (
                            session_pattern.fullmatch(row[0])
                            or numeric_pattern.fullmatch(row[0])
                        ))
                    ),
                    default=0,
                )
            else:
                last_value = counter[0]
            next_value = last_value + 1
            connection.execute(
                """INSERT INTO session_placeholder_counters
                   (session_id, entity_type, last_value)
                   VALUES (?, ?, ?)
                   ON CONFLICT(session_id, entity_type) DO UPDATE SET
                       last_value = excluded.last_value""",
                (session_id, entity_type, next_value),
            )
            placeholder = f"<{entity_type}_S{session_namespace}_{next_value}>"
            connection.execute(
                """INSERT INTO mappings
                   (session_id, placeholder, entity_type, original_value, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, placeholder, entity_type, original_value, int(time.time())),
            )
            connection.commit()
            return placeholder
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def resolve(self, session_id, placeholder):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """WITH RECURSIVE lineage(session_id, depth, path) AS (
                       SELECT ?, 0, ',' || ? || ','
                       UNION ALL
                       SELECT sessions.parent_session_id, lineage.depth + 1,
                              lineage.path || sessions.parent_session_id || ','
                       FROM sessions JOIN lineage
                         ON sessions.session_id = lineage.session_id
                       WHERE sessions.parent_session_id IS NOT NULL
                         AND lineage.depth < 32
                         AND instr(lineage.path,
                                   ',' || sessions.parent_session_id || ',') = 0
                   )
                   SELECT mappings.entity_type, mappings.original_value,
                          mappings.session_id, lineage.depth
                   FROM lineage JOIN mappings USING (session_id)
                   WHERE mappings.placeholder = ?
                   ORDER BY lineage.depth LIMIT 1""",
                (session_id, session_id, placeholder),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            entity_type, original_value, _, depth = row
            if depth:
                connection.execute(
                    """INSERT OR IGNORE INTO mappings
                       (session_id, placeholder, entity_type, original_value, created_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (session_id, placeholder, entity_type, original_value, int(time.time())),
                )
            connection.commit()
            return original_value
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_mappings(self, session_id):
        """Все известные плейсхолдеры сессии и родителей — для restore ответа."""
        connection = self._connect()
        try:
            rows = connection.execute(
                """WITH RECURSIVE lineage(session_id, depth, path) AS (
                       SELECT ?, 0, ',' || ? || ','
                       UNION ALL
                       SELECT sessions.parent_session_id, lineage.depth + 1,
                              lineage.path || sessions.parent_session_id || ','
                       FROM sessions JOIN lineage
                         ON sessions.session_id = lineage.session_id
                       WHERE sessions.parent_session_id IS NOT NULL
                         AND lineage.depth < 32
                         AND instr(lineage.path,
                                   ',' || sessions.parent_session_id || ',') = 0
                   )
                   SELECT mappings.placeholder, mappings.original_value, lineage.depth
                   FROM lineage JOIN mappings USING (session_id)
                   ORDER BY lineage.depth""",
                (session_id, session_id),
            ).fetchall()
            replacements = {}
            for placeholder, original_value, _depth in rows:
                replacements.setdefault(placeholder, original_value)
            return replacements
        finally:
            connection.close()


def canonical_placeholder(token):
    return (
        token.replace("\\u003c", "<")
        .replace("\\u003C", "<")
        .replace("\\u003e", ">")
        .replace("\\u003E", ">")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )


def session_key(raw_session_id):
    return hashlib.sha256(raw_session_id.encode("utf-8")).hexdigest()


def request_session_keys(headers):
    raw_session_id = (
        headers.get("x-opencode-session-id")
        or headers.get("x-opencode-session")
        or headers.get("x-session-affinity")
        or headers.get("x-session-id")
    )
    raw_parent_id = (
        headers.get("x-opencode-parent-session-id")
        or headers.get("x-parent-session-id")
    )
    if not raw_session_id or len(raw_session_id) > 256:
        return None, None
    return (
        session_key(raw_session_id),
        session_key(raw_parent_id) if raw_parent_id and len(raw_parent_id) <= 256 else None,
    )


def anonymize_from_results(text, analyzer_results, replacements, mapping_store, session_id):
    """Replace detected values consistently and retain exact values for restoration."""
    spans = []
    for result in analyzer_results:
        if not isinstance(result, dict):
            raise ValueError("Invalid Presidio analyzer result")
        start, end = result.get("start"), result.get("end")
        entity_type = result.get("entity_type")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(entity_type, str)
            or start < 0
            or start >= end
            or end > len(text)
        ):
            raise ValueError("Invalid Presidio analyzer span")
        spans.append({"start": start, "end": end, "entity_type": entity_type})
    spans.sort(key=lambda item: (item["start"], item["end"]))

    merged_spans = []
    for span in spans:
        if merged_spans and span["start"] < merged_spans[-1]["end"]:
            merged_spans[-1]["end"] = max(merged_spans[-1]["end"], span["end"])
        else:
            merged_spans.append(span)

    parts = []
    cursor = 0
    for result in merged_spans:
        start, end = result["start"], result["end"]
        entity_type = result["entity_type"]
        original_value = text[start:end]
        placeholder = mapping_store.get_or_create(session_id, entity_type, original_value)
        parts.extend((text[cursor:start], placeholder))
        replacements[placeholder] = original_value
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)


def detected_language(text):
    detected_lang = "en"
    if re.search(r"[ҐґЄєІіЇї]", text):
        detected_lang = "uk"
    elif re.search(r"[а-яА-ЯёЁ]", text):
        detected_lang = "ru"
    return detected_lang


def analyze_text(text, language):
    payload = json.dumps({"text": text, "language": language}).encode("utf-8")
    req_presidio = urllib.request.Request(
        PRESIDIO_URL + "/analyze",
        data=payload,
        headers={"Content-Type": "application/json", "Host": "127.0.0.1:5001"},
    )
    started_at = time.monotonic()
    try:
        with urllib.request.urlopen(
            req_presidio, timeout=PRESIDIO_ANALYZE_TIMEOUT_SECONDS
        ) as response:
            analyzer_results = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(reason, TimeoutError):
            elapsed = time.monotonic() - started_at
            print(
                "[Presidio Analyzer Timeout] "
                f"limit_seconds={PRESIDIO_ANALYZE_TIMEOUT_SECONDS:g} "
                f"elapsed_seconds={elapsed:.1f} text_chars={len(text)}",
                file=sys.stderr,
                flush=True,
            )
        raise
    if not isinstance(analyzer_results, list):
        raise ValueError("Invalid Presidio analyzer response")
    return analyzer_results


def anonymize_text_batch(texts, replacements, mapping_store, session_id):
    normalized = [
        add_cached_replacements(text, replacements, mapping_store, session_id)
        for text in texts
    ]
    grouped_indices = {}
    for index, text in enumerate(normalized):
        if text:
            grouped_indices.setdefault(detected_language(text), []).append(index)

    results_by_index = [[] for _ in texts]
    for language, indices in grouped_indices.items():
        batches = []
        batch = []
        batch_chars = 0
        for index in indices:
            additional_chars = len(normalized[index]) + (2 if batch else 0)
            if batch and batch_chars + additional_chars > PRESIDIO_ANALYZE_BATCH_MAX_CHARS:
                batches.append(batch)
                batch = []
                batch_chars = 0
                additional_chars = len(normalized[index])
            batch.append(index)
            batch_chars += additional_chars
        if batch:
            batches.append(batch)

        for batch in batches:
            segments = []
            segment_starts = []
            parts = []
            offset = 0
            for index in batch:
                text = normalized[index]
                segment_starts.append(offset)
                segments.append((index, offset, offset + len(text)))
                parts.append(text)
                offset += len(text)
                if index != batch[-1]:
                    parts.append("\n\n")
                    offset += 2

            combined_text = "".join(parts)
            analyzer_results = analyze_text(combined_text, language)
            for result in analyzer_results:
                if not isinstance(result, dict):
                    raise ValueError("Invalid Presidio analyzer result")
                start, end = result.get("start"), result.get("end")
                entity_type = result.get("entity_type")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not isinstance(entity_type, str)
                    or start < 0
                    or start >= end
                    or end > len(combined_text)
                ):
                    raise ValueError("Invalid Presidio analyzer span")
                segment_position = bisect_right(segment_starts, start) - 1
                index, segment_start, segment_end = segments[segment_position]
                if start < segment_start or end > segment_end:
                    continue
                results_by_index[index].append(
                    {
                        "start": start - segment_start,
                        "end": end - segment_start,
                        "entity_type": entity_type,
                    }
                )

    return [
        anonymize_from_results(text, results, replacements, mapping_store, session_id)
        if results
        else text
        for text, results in zip(normalized, results_by_index)
    ]


def anonymize_text(text, replacements, mapping_store, session_id):
    return anonymize_text_batch([text], replacements, mapping_store, session_id)[0]


class TextReference:
    __slots__ = ("index",)

    def __init__(self, index):
        self.index = index


def anonymize_value(value, replacements, mapping_store, session_id):
    texts = []

    def collect(node):
        if isinstance(node, str):
            index = len(texts)
            texts.append(node)
            return TextReference(index)
        if isinstance(node, list):
            return [collect(item) for item in node]
        if isinstance(node, dict):
            media_keys = {"image", "image_url", "input_image", "audio", "input_audio", "video", "file_data"}
            content_type = node.get("type")
            content_types = content_type if isinstance(content_type, list) else [content_type]
            if any(isinstance(item, str) and item in MEDIA_TYPES for item in content_types):
                raise ValueError("Multimodal payload is not supported by the anonymizer")
            if any(key.lower() in media_keys and item for key, item in node.items()):
                raise ValueError("Multimodal payload is not supported by the anonymizer")
            return {
                collect(key): collect(item)
                for key, item in node.items()
            }
        return node

    collected = collect(value)
    anonymized_texts = anonymize_text_batch(
        texts, replacements, mapping_store, session_id
    )

    def rebuild(node):
        if isinstance(node, TextReference):
            return anonymized_texts[node.index]
        if isinstance(node, list):
            return [rebuild(item) for item in node]
        if isinstance(node, dict):
            return {rebuild(key): rebuild(item) for key, item in node.items()}
        return node

    return rebuild(collected)


def add_cached_replacements(text, replacements, mapping_store, session_id):
    for placeholder in dict.fromkeys(
        canonical_placeholder(match.group(0))
        for match in PLACEHOLDER_PATTERN.finditer(text)
    ):
        original_value = mapping_store.resolve(session_id, placeholder)
        legacy_match = LEGACY_UUID_PLACEHOLDER_PATTERN.fullmatch(placeholder)
        if legacy_match is not None:
            entity_type = legacy_match.group("entity")
            if (
                original_value is None
                or LEGACY_UUID_PLACEHOLDER_PATTERN.fullmatch(original_value) is not None
            ):
                text = text.replace(placeholder, UNRESOLVED_PLACEHOLDER)
                continue

            canonical = mapping_store.get_or_create(
                session_id, entity_type, original_value
            )
            replacements[placeholder] = original_value
            replacements[canonical] = original_value
            text = text.replace(placeholder, canonical)
        elif original_value is None:
            text = text.replace(placeholder, UNRESOLVED_PLACEHOLDER)
        elif LEGACY_UUID_PLACEHOLDER_PATTERN.fullmatch(original_value) is None:
            replacements[placeholder] = original_value
    return text


def normalize_model_id(model):
    if isinstance(model, str) and model.startswith("opencode/"):
        return model.removeprefix("opencode/")
    return model


def validate_chat_request(body, path):
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON request object")
    endpoint = path.partition("?")[0]
    if endpoint.endswith("/responses"):
        request_input = body.get("input")
        if not isinstance(request_input, (str, list)) or not request_input:
            raise ValueError("Expected an OpenAI Responses request with input")
        return

    messages = body.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or any(not isinstance(message, dict) for message in messages)
    ):
        raise ValueError("Expected a JSON chat request with messages")


def restore_text(text, replacements):
    def replace_placeholder(match):
        original_value = replacements.get(canonical_placeholder(match.group(0)))
        if original_value is None:
            return UNRESOLVED_PLACEHOLDER
        return original_value

    return PLACEHOLDER_PATTERN.sub(replace_placeholder, text)


def restored_chunks(response, replacements):
    """Restore placeholders even when their bytes cross response chunk boundaries."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    tail_length = max(255, max((len(key) for key in replacements), default=1) - 1)
    pending = ""
    while True:
        chunk = response.read1(4096)
        if not chunk:
            break
        combined = pending + decoder.decode(chunk)
        split_at = max(0, len(combined) - tail_length)
        encoded_tokens = []
        for placeholder in replacements:
            encoded_tokens.append(placeholder)
            encoded_tokens.append(
                placeholder.replace("<", "\\u003c").replace(">", "\\u003e")
            )
            encoded_tokens.append(
                placeholder.replace("<", "&lt;").replace(">", "&gt;")
            )
        for encoded in encoded_tokens:
            search_from = 0
            while True:
                start = combined.find(encoded, search_from)
                if start < 0:
                    break
                end = start + len(encoded)
                if start < split_at < end:
                    split_at = start
                search_from = start + 1
        yield restore_text(combined[:split_at], replacements)
        pending = combined[split_at:]
    pending += decoder.decode(b"", final=True)
    yield restore_text(pending, replacements)


def sse_payload(frame):
    data_lines = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return data
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def is_tool_call_delta(payload):
    if not isinstance(payload, dict):
        return False
    return any(
        isinstance(choice, dict)
        and isinstance(choice.get("delta"), dict)
        and any(key in choice["delta"] for key in ("tool_calls", "function_call"))
        for choice in payload.get("choices", [])
    )


def is_terminal_sse_payload(payload):
    if payload == "[DONE]":
        return True
    if not isinstance(payload, dict):
        return False
    return any(
        isinstance(choice, dict) and choice.get("finish_reason") is not None
        for choice in payload.get("choices", [])
    )


def blocked_tool_call_frame(template_frame, message):
    payload = sse_payload(template_frame)
    base = {
        key: value
        for key, value in (payload.items() if isinstance(payload, dict) else [])
        if key != "choices"
    }
    base.setdefault("object", "chat.completion.chunk")
    base.setdefault("created", int(time.time()))
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    choice_index = choices[0].get("index", 0) if choices and isinstance(choices[0], dict) else 0
    base["choices"] = [
        {
            "index": choice_index,
            "delta": {"content": message},
            "finish_reason": "stop",
        }
    ]
    return "data: " + json.dumps(base, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def process_sse_frame(frame, buffered_tool_frames):
    payload = sse_payload(frame)
    if buffered_tool_frames or is_tool_call_delta(payload):
        buffered = buffered_tool_frames + [frame]
        if not is_terminal_sse_payload(payload):
            return [], buffered, False, None
        if any(UNRESOLVED_PLACEHOLDER in item for item in buffered):
            return [], [], False, buffered
        return buffered, [], payload == "[DONE]", None
    return [frame], [], payload == "[DONE]", None


def build_tool_retry_body(body, assistant_content):
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
        return None
    retry_body = copy.deepcopy(body)
    if assistant_content:
        retry_body["messages"].append(
            {"role": "assistant", "content": assistant_content}
        )
    retry_body["messages"].append(
        {
            "role": "user",
            "content": (
                "A local privacy guard withheld your previous tool call because an argument "
                "contained an unresolved anonymization marker. The tool did not run. "
                "Do not repeat or invent placeholders. Find the real path/value from the "
                "available context using safe local search tools; if it cannot be established, "
                "ask the user. Continue the task and do not repeat completed narration."
            ),
        }
    )
    return retry_body


def frame_assistant_text(frame):
    payload = sse_payload(frame)
    if not isinstance(payload, dict):
        return ""
    texts = []
    for choice in payload.get("choices", []):
        delta = choice.get("delta") if isinstance(choice, dict) else None
        content = delta.get("content") if isinstance(delta, dict) else None
        if isinstance(content, str):
            texts.append(content)
    return "".join(texts)


def tool_rejection_chunks(tool_frames, retries_exhausted=False):
    template = next(
        (item for item in tool_frames if isinstance(sse_payload(item), dict)),
        tool_frames[0],
    )
    message = (
        "\n[Tool call withheld after automatic retries: the required path or value "
        "could not be recovered. Please provide it or ask the agent to search locally.]"
        if retries_exhausted
        else "\n[Tool call withheld: unable to retry safely.]"
    )
    return [blocked_tool_call_frame(template, message), "data: [DONE]\n\n"]


def guarded_stream_chunks(
    response,
    replacements,
    request_body=None,
    retry_upstream=None,
    max_retries=MAX_UNRESOLVED_TOOL_RETRIES,
):
    retry_count = 0
    current_body = request_body

    while True:
        pending = ""
        buffered_tool_frames = []
        assistant_content = []
        retry_frames = None
        done = False

        for chunk in restored_chunks(response, replacements):
            pending += chunk
            while True:
                boundary = re.search(r"\r?\n\r?\n", pending)
                if boundary is None:
                    break
                frame = pending[:boundary.start()] + boundary.group(0)
                pending = pending[boundary.end():]
                output, buffered_tool_frames, frame_done, rejected = process_sse_frame(
                    frame, buffered_tool_frames
                )
                if rejected is not None:
                    retry_frames = rejected
                    break
                for output_frame in output:
                    assistant_content.append(frame_assistant_text(output_frame))
                    yield output_frame
                if frame_done:
                    done = True
                    break
            if retry_frames is not None or done:
                break

        if retry_frames is None and not done and pending:
            output, buffered_tool_frames, frame_done, rejected = process_sse_frame(
                pending, buffered_tool_frames
            )
            if rejected is not None:
                retry_frames = rejected
            else:
                for output_frame in output:
                    assistant_content.append(frame_assistant_text(output_frame))
                    yield output_frame
                done = frame_done

        if done:
            return

        if retry_frames is None and buffered_tool_frames:
            retry_frames = buffered_tool_frames

        if retry_frames is None:
            return

        response.close()
        retry_body = build_tool_retry_body(current_body, "".join(assistant_content))
        if retry_count >= max_retries or retry_body is None or retry_upstream is None:
            yield from tool_rejection_chunks(retry_frames, retries_exhausted=True)
            return

        try:
            response = retry_upstream(retry_body)
        except Exception as error:
            print(
                f"[Tool repair retry failed] {type(error).__name__}",
                file=sys.stderr,
                flush=True,
            )
            yield from tool_rejection_chunks(retry_frames)
            return
        if response is None:
            yield from tool_rejection_chunks(retry_frames)
            return

        current_body = retry_body
        retry_count += 1


def contains_unresolved_placeholder(value, replacements):
    if isinstance(value, str):
        return UNRESOLVED_PLACEHOLDER in value or any(
            canonical_placeholder(placeholder) not in replacements
            for placeholder in PLACEHOLDER_PATTERN.findall(value)
        )
    if isinstance(value, list):
        return any(contains_unresolved_placeholder(item, replacements) for item in value)
    if isinstance(value, dict):
        return any(
            contains_unresolved_placeholder(key, replacements)
            or contains_unresolved_placeholder(item, replacements)
            for key, item in value.items()
        )
    return False


def restore_json_value(value, replacements):
    if isinstance(value, str):
        return PLACEHOLDER_PATTERN.sub(
            lambda match: replacements.get(
                canonical_placeholder(match.group(0)), UNRESOLVED_PLACEHOLDER
            ),
            value,
        )
    if isinstance(value, list):
        return [restore_json_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {
            restore_json_value(key, replacements): restore_json_value(item, replacements)
            for key, item in value.items()
        }
    return value


def restore_nonstream_response(response_body, replacements):
    payload = json.loads(response_body.decode("utf-8"))
    choices = payload.get("choices", []) if isinstance(payload, dict) else []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if not isinstance(message, dict):
            continue
        tool_fields = {
            key: message[key]
            for key in ("tool_calls", "function_call")
            if key in message
        }
        if tool_fields and contains_unresolved_placeholder(tool_fields, replacements):
            message.pop("tool_calls", None)
            message.pop("function_call", None)
            message["content"] = (message.get("content") or "") + (
                "\n[Tool call withheld: an anonymization mapping is unavailable. "
                "Retry after restoring the original value or path.]"
            )
            choice["finish_reason"] = "stop"
    restored = restore_json_value(payload, replacements)
    return json.dumps(restored, ensure_ascii=False).encode("utf-8")


def safe_diagnostic_label(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
        return value
    return "other"


class BoundHTTPSConnection(http.client.HTTPSConnection):
    """Класс соединения, совместимый со всеми версиями Python в Docker."""
    def connect(self):
        self.sock = socket.create_connection((OPENCODE_REAL_IP, 443), self.timeout)

        # Корректно передаем SNI 'opencode.ai' через TLS контекст без конфликтов в __init__
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(self.sock, server_hostname='opencode.ai')

class BoundHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(BoundHTTPSConnection, req)


def request_shape_summary(body):
    messages = body.get("messages")
    message_shapes = []
    allowed_roles = {"system", "developer", "user", "assistant", "tool", "function"}
    allowed_block_types = {"text", "image", "image_url", "input_image", "audio", "input_audio", "video", "file"}
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            content_shape: dict[str, object] = {"type": type(content).__name__}
            if isinstance(content, str):
                content_shape["length"] = len(content)
            elif isinstance(content, list):
                content_shape["length"] = len(content)
                block_types = set()
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    block_type = block.get("type")
                    if isinstance(block_type, str):
                        block_types.add(
                            block_type if block_type in allowed_block_types else "other"
                        )
                content_shape["block_types"] = sorted(block_types)
            tool_calls = message.get("tool_calls")
            message_shapes.append({
                "role": message.get("role") if isinstance(message.get("role"), str) and message.get("role") in allowed_roles else "other",
                "content": content_shape,
                "tool_call_count": len(tool_calls) if isinstance(tool_calls, list) else 0,
            })

    tools = body.get("tools")
    tool_types = sorted({
        "function" if tool.get("type", "function") == "function" else "other"
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("type", "function"), str)
    }) if isinstance(tools, list) else []
    shape_fields = {
        "tools", "tool_choice", "response_format", "parallel_tool_calls",
        "max_tokens", "max_completion_tokens", "temperature", "top_p",
        "stream_options", "functions", "function_call",
    }
    known_fields = shape_fields | {"model", "messages", "metadata", "stream"}
    return {
        "model_present": isinstance(body.get("model"), str),
        "top_level_fields": sorted(key for key in body if key in known_fields),
        "message_count": len(message_shapes),
        "messages": message_shapes,
        "tool_count": len(tools) if isinstance(tools, list) else 0,
        "tool_types": tool_types,
        "optional_field_types": {
            key: type(body[key]).__name__
            for key in sorted(shape_fields & body.keys())
        },
    }


def compress_context(messages, replacements, mapping_store, session_id, model_name=None):
    """
    Compress context by summarizing old messages and keeping recent ones.
    Returns compressed messages and indicates if compression was applied.
    """
    max_messages = get_model_context_limit(model_name)
    threshold = int(max_messages * CONTEXT_COMPRESSION_THRESHOLD)

    if len(messages) < threshold:
        return messages, False

    print(
        f"[Context Compression] Session has {len(messages)} messages, "
        f"model {model_name or 'unknown'} (limit {max_messages}), "
        f"threshold {threshold}. "
        f"Compressing context, keeping last {KEEP_RECENT_MESSAGES} messages.",
        file=sys.stderr,
        flush=True,
    )

    # Split messages: old ones to summarize, recent ones to keep
    if len(messages) > KEEP_RECENT_MESSAGES:
        old_messages = messages[:-KEEP_RECENT_MESSAGES]
        recent_messages = messages[-KEEP_RECENT_MESSAGES:]
    else:
        # Not enough messages to compress meaningfully
        return messages, False

    # Build summary from old messages
    summary_parts = []
    for msg in old_messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str):
            # Truncate long content
            content_preview = content[:200] + "..." if len(content) > 200 else content
            summary_parts.append(f"[{role}]: {content_preview}")
        elif isinstance(content, list):
            summary_parts.append(f"[{role}]: [multimodal content with {len(content)} blocks]")
        else:
            summary_parts.append(f"[{role}]: [content]")

    summary_text = " ".join(summary_parts)
    summary_message = {
        "role": "system",
        "content": (
            f"[CONTEXT SUMMARY: Previous conversation had {len(old_messages)} messages. "
            f"Key points: {summary_text}]"
        ),
    }

    # Create new compressed message list
    compressed_messages = [summary_message] + recent_messages

    print(
        f"[Context Compression] Compressed from {len(messages)} to {len(compressed_messages)} messages.",
        file=sys.stderr,
        flush=True,
    )

    return compressed_messages, True


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status":"healthy"}')

    def log_message(self, format, *args):
        if self.path != "/health":
            super().log_message(format, *args)

    def do_POST(self):
        try:
            content_length = int(self.headers["Content-Length"])
        except (KeyError, ValueError):
            self.send_error(400, "Missing or invalid Content-Length")
            return
        raw_body = self.rfile.read(content_length)
        replacements = {}
        try:
            body = json.loads(raw_body.decode('utf-8'))
            validate_chat_request(body, self.path)
            model_id = normalize_model_id(body.get("model"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as e:
            self.send_error(400, str(e))
            return

        session_id, parent_session_id = request_session_keys(self.headers)
        if session_id is None:
            self.send_error(400, "Missing or invalid OpenCode session identifier")
            return

        # Flag to indicate if context was compressed
        context_compressed = False

        try:
            self.server.mapping_store.prepare_session(session_id, parent_session_id)
            replacements = self.server.mapping_store.list_mappings(session_id)

            # Check if context compression is needed (using model-specific limit)
            messages = body.get("messages", [])
            model_limit = get_model_context_limit(model_id)
            if isinstance(messages, list) and len(messages) >= model_limit * CONTEXT_COMPRESSION_THRESHOLD:
                compressed_messages, was_compressed = compress_context(
                    messages, replacements, self.server.mapping_store, session_id, model_id
                )
                if was_compressed:
                    body["messages"] = compressed_messages
                    context_compressed = True

            anonymized_body = anonymize_value(
                body, replacements, self.server.mapping_store, session_id
            )
            if not isinstance(anonymized_body, dict):
                raise ValueError("Expected an anonymized JSON object")
            anonymized_body["model"] = model_id
            final_body = json.dumps(anonymized_body).encode("utf-8")
        except (
            urllib.error.URLError, OSError, sqlite3.Error,
            http.client.HTTPException, ValueError, TypeError,
        ) as e:
            print(f"[Presidio Bridge Error] {e}", file=sys.stderr, flush=True)
            self.send_error(503, "Presidio anonymization unavailable")
            return

        upstream_headers = {k: v for k, v in self.headers.items() if k.lower() not in ['host', 'content-length', 'accept-encoding']}
        upstream_headers['Host'] = 'opencode.ai'
        upstream_headers['Accept-Encoding'] = 'identity'
        upstream_headers['Content-Length'] = str(len(final_body))

        url = f"https://opencode.ai{self.path}"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), BoundHTTPSHandler)
        req = urllib.request.Request(url, data=final_body, headers=upstream_headers, method="POST")

        def retry_upstream(retry_body):
            retry_replacements = replacements
            anonymized_retry_body = anonymize_value(
                retry_body,
                retry_replacements,
                self.server.mapping_store,
                session_id,
            )
            if not isinstance(anonymized_retry_body, dict):
                raise ValueError("Expected a retried JSON request object")
            anonymized_retry_body["model"] = model_id
            retry_data = json.dumps(anonymized_retry_body).encode("utf-8")
            retry_headers = dict(upstream_headers)
            retry_headers["Content-Length"] = str(len(retry_data))
            retry_request = urllib.request.Request(
                url, data=retry_data, headers=retry_headers, method="POST"
            )
            return opener.open(retry_request)

        try:
            with opener.open(req) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                if "text/event-stream" in content_type:
                    self.send_response(response.status)
                    for k, v in response.getheaders():
                        if k.lower() not in ['content-encoding', 'transfer-encoding', 'content-length']:
                            self.send_header(k, v)
                    if context_compressed:
                        self.send_header("X-OpenCode-Context-Compressed", "true")
                    self.end_headers()

                    for chunk_str in guarded_stream_chunks(
                        response,
                        replacements,
                        request_body=body,
                        retry_upstream=retry_upstream,
                    ):
                        try:
                            self.wfile.write(chunk_str.encode('utf-8'))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            # Client disconnected, silently end the response
                            print("[Stream] Client disconnected (broken pipe)", file=sys.stderr, flush=True)
                            return
                else:
                    restored_body = restore_nonstream_response(
                        response.read(), replacements
                    )
                    self.send_response(response.status)
                    for k, v in response.getheaders():
                        if k.lower() not in ['content-encoding', 'transfer-encoding', 'content-length']:
                            self.send_header(k, v)
                    if context_compressed:
                        self.send_header("X-OpenCode-Context-Compressed", "true")
                    self.send_header("Content-Length", str(len(restored_body)))
                    self.end_headers()
                    self.wfile.write(restored_body)

        except urllib.error.HTTPError as e:
            error_body = e.read()
            print(f"[Upstream Error {e.code}] Ответ от opencode.ai", file=sys.stderr, flush=True)
            if e.code == 400:
                try:
                    upstream_error = json.loads(error_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    upstream_error = {}
                if isinstance(upstream_error, dict):
                    upstream_error = upstream_error.get("error", upstream_error)
                error_type = upstream_error.get("type") if isinstance(upstream_error, dict) else None
                error_code = upstream_error.get("code") if isinstance(upstream_error, dict) else None
                summary = request_shape_summary(body)
                print(
                    "[Upstream 400 Details] "
                    + json.dumps({
                        "request": summary,
                        "error_type": safe_diagnostic_label(error_type),
                        "error_code": safe_diagnostic_label(error_code),
                        "error_body_bytes": len(error_body),
                    }, ensure_ascii=True),
                    file=sys.stderr,
                    flush=True,
                )
                # Provide user-friendly error for large sessions
                if error_type == "invalid_request_error" and summary.get("message_count", 0) > 100:
                    error_body = json.dumps({
                        "error": {
                            "message": (
                                f"Session context is too large ({summary['message_count']} messages, "
                                f"{summary['tool_count']} tools). Please start a new conversation or "
                                "use /clear to reset the context."
                            ),
                            "type": "context_limit_exceeded",
                            "code": "context_too_large"
                        }
                    }).encode("utf-8")
            self.send_response(e.code)
            for k, v in e.headers.items():
                if k.lower() not in ['content-length', 'transfer-encoding']:
                    self.send_header(k, v)
            self.send_header('Content-Length', str(len(error_body)))
            self.end_headers()
            self.wfile.write(error_body)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError, TypeError) as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))

if __name__ == '__main__':
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 4001), ProxyHandler)
    server.mapping_store = MappingStore(MAPPING_DB_PATH)
    print("Финальный автономный SSL-бридж анонимайзера запущен на порту 4001...")
    server.serve_forever()
