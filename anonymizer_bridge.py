#!/usr/bin/env python3
import http.server
import json
import sys
import re
import codecs
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

PRESIDIO_URL = "http://127.0.0.1:5001"
PRESIDIO_ANALYZE_TIMEOUT_SECONDS = float(
    os.environ.get("PRESIDIO_ANALYZE_TIMEOUT_SECONDS", "60")
)
PRESIDIO_ANALYZE_BATCH_MAX_CHARS = 20000
if PRESIDIO_ANALYZE_TIMEOUT_SECONDS <= 0:
    raise ValueError("Presidio analyze timeout must be positive")
OPENCODE_REAL_IP = "104.21.32.140"
PLACEHOLDER_PATTERN = re.compile(
    r"<[A-Z][A-Z0-9_]*_(?:\d+(?:_[0-9a-f]{32})?|[0-9a-f]{32})>"
)
LEGACY_UUID_PLACEHOLDER_PATTERN = re.compile(
    r"<(?P<entity>[A-Z][A-Z0-9_]*?)_(?:(?P<index>\d+)_)?(?P<uuid>[0-9a-f]{32})>"
)
MEDIA_TYPES = {"image", "image_url", "input_image", "audio", "input_audio", "video", "file"}
MAPPING_DB_PATH = os.environ.get(
    "OPENCODE_MAPPING_DB", "/var/lib/opencode-bridge/mappings.sqlite3"
)
MAPPING_TTL_SECONDS = int(os.environ.get("OPENCODE_MAPPING_TTL_SECONDS", "0"))


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
            canonical_pattern = re.compile(rf"<{re.escape(entity_type)}_\d+>")
            for row in rows:
                if canonical_pattern.fullmatch(row[0]):
                    connection.commit()
                    return row[0]

            counter = connection.execute(
                "SELECT last_value FROM placeholder_counters WHERE entity_type = ?",
                (entity_type,),
            ).fetchone()
            if counter is None:
                numeric_pattern = re.compile(
                    rf"<{re.escape(entity_type)}_(\d+)(?:_[0-9a-f]{{32}})?>"
                )
                existing = connection.execute(
                    "SELECT placeholder FROM mappings WHERE entity_type = ?",
                    (entity_type,),
                ).fetchall()
                last_value = max(
                    (
                        int(match.group(1))
                        for row in existing
                        if (match := numeric_pattern.fullmatch(row[0]))
                    ),
                    default=0,
                )
            else:
                last_value = counter[0]
            next_value = last_value + 1
            connection.execute(
                """INSERT INTO placeholder_counters(entity_type, last_value)
                   VALUES (?, ?)
                   ON CONFLICT(entity_type) DO UPDATE SET
                       last_value = excluded.last_value""",
                (entity_type, next_value),
            )
            placeholder = f"<{entity_type}_{next_value}>"
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
    for placeholder in dict.fromkeys(PLACEHOLDER_PATTERN.findall(text)):
        original_value = mapping_store.resolve(session_id, placeholder)
        legacy_match = LEGACY_UUID_PLACEHOLDER_PATTERN.fullmatch(placeholder)
        if legacy_match is not None:
            entity_type = legacy_match.group("entity")
            if original_value is None:
                original_value = placeholder
            elif LEGACY_UUID_PLACEHOLDER_PATTERN.fullmatch(original_value) is None:
                replacements[placeholder] = original_value

            canonical = mapping_store.get_or_create(
                session_id, entity_type, original_value
            )
            if LEGACY_UUID_PLACEHOLDER_PATTERN.fullmatch(original_value) is None:
                replacements[canonical] = original_value
            text = text.replace(placeholder, canonical)
        elif (
            original_value is not None
            and LEGACY_UUID_PLACEHOLDER_PATTERN.fullmatch(original_value) is None
        ):
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
    for placeholder, original_value in replacements.items():
        text = text.replace(placeholder, original_value)
    return text


def restored_chunks(response, replacements):
    """Restore placeholders even when their bytes cross response chunk boundaries."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    tail_length = max((len(key) for key in replacements), default=1) - 1
    pending = ""
    while True:
        chunk = response.read1(4096)
        if not chunk:
            break
        combined = pending + decoder.decode(chunk)
        split_at = max(0, len(combined) - tail_length)
        for placeholder in replacements:
            search_from = 0
            while True:
                start = combined.find(placeholder, search_from)
                if start < 0:
                    break
                end = start + len(placeholder)
                if start < split_at < end:
                    split_at = start
                search_from = start + 1
        yield restore_text(combined[:split_at], replacements)
        pending = combined[split_at:]
    pending += decoder.decode(b"", final=True)
    yield restore_text(pending, replacements)


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

        try:
            self.server.mapping_store.prepare_session(session_id, parent_session_id)
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

        try:
            with opener.open(req) as response:
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in ['content-encoding', 'transfer-encoding', 'content-length']:
                        self.send_header(k, v)
                self.end_headers()

                for chunk_str in restored_chunks(response, replacements):
                    self.wfile.write(chunk_str.encode('utf-8'))
                    self.wfile.flush()

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
                print(
                    "[Upstream 400 Details] "
                    + json.dumps({
                        "request": request_shape_summary(body),
                        "error_type": safe_diagnostic_label(error_type),
                        "error_code": safe_diagnostic_label(error_code),
                        "error_body_bytes": len(error_body),
                    }, ensure_ascii=True),
                    file=sys.stderr,
                    flush=True,
                )
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
