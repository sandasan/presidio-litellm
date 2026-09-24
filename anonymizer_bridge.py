#!/usr/bin/env python3
import http.server
import json
import sys
import re
import urllib.request
import ssl
import http.client

PRESIDIO_URL = "http://127.0.0.1:5001"
OPENCODE_REAL_IP = "104.21.32.140"

class BoundHTTPSConnection(http.client.HTTPSConnection):
    """Класс соединения, совместимый со всеми версиями Python в Docker."""
    def connect(self):
        # Подключаемся к сырому IP Cloudflare
        self.sock = self._create_connection((OPENCODE_REAL_IP, 443), self.timeout, self.source_address)
        if self._tunnel_host:
            self._tunnel()

        # Корректно передаем SNI 'opencode.ai' через TLS контекст без конфликтов в __init__
        context = ssl.create_default_context()
        self.sock = context.wrap_socket(self.sock, server_hostname='opencode.ai')

class BoundHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(BoundHTTPSConnection, req)

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        raw_body = self.rfile.read(content_length)
        final_body = raw_body
        replacements = {}

        try:
            body = json.loads(raw_body.decode('utf-8'))
            if "messages" in body:
                for msg in body["messages"]:
                    if "content" in msg and isinstance(msg["content"], str):
                        orig_text = msg["content"]

                        detected_lang = "en"
                        if re.search(r'[ҐґЄєІіЇї]', orig_text):
                            detected_lang = "uk"
                        elif re.search(r'[а-яА-ЯёЁ]', orig_text):
                            detected_lang = "ru"

                        try:
                            payload = json.dumps({"text": orig_text, "language": detected_lang}).encode('utf-8')
                            req_presidio = urllib.request.Request(
                                PRESIDIO_URL,
                                data=payload,
                                headers={"Content-Type": "application/json", "Host": "127.0.0.1:5001"}
                            )
                            with urllib.request.urlopen(req_presidio, timeout=4) as resp:
                                presidio_res = json.loads(resp.read().decode('utf-8'))
                                anonymized_text = presidio_res.get("text", orig_text)

                                placeholders = re.findall(r'(<[A-Z_]+(?:_\d+)?>)', anonymized_text)
                                if placeholders:
                                    emails_orig = re.findall(r'[\w\.-]+@[\w\.-]+\.\w+', orig_text)
                                    cards_orig = re.findall(r'\d{13,19}', orig_text)
                                    names_orig = re.findall(r'[А-ЯҐЄІЇ][а-яґєії\']+\s+[А-ЯҐЄІЇ][а-яґєії\']+', orig_text)

                                    for p in placeholders:
                                        if "EMAIL" in p and emails_orig:
                                            replacements[p] = emails_orig[0]
                                        elif "CREDIT_CARD" in p and cards_orig:
                                            replacements[p] = cards_orig[0]
                                        elif "PERSON" in p and names_orig:
                                            replacements[p] = names_orig[0]

                                if replacements:
                                    print(f"[Map Created] Собрана карта замен: {replacements}", flush=True)
                                msg["content"] = anonymized_text
                        except Exception as e:
                            print(f"[Presidio Bridge Error] {e}", file=sys.stderr, flush=True)
            final_body = json.dumps(body).encode('utf-8')
        except Exception:
            pass

        upstream_headers = {k: v for k, v in self.headers.items() if k.lower() not in ['host', 'content-length', 'accept-encoding']}
        upstream_headers['Host'] = 'opencode.ai'
        upstream_headers['Accept-Encoding'] = 'identity'
        upstream_headers['Content-Length'] = str(len(final_body))

        url = f"https://opencode.ai{self.path}"
        opener = urllib.request.build_opener(BoundHTTPSHandler)
        req = urllib.request.Request(url, data=final_body, headers=upstream_headers, method="POST")

        try:
            with opener.open(req) as response:
                self.send_response(response.status)
                for k, v in response.getheaders():
                    if k.lower() not in ['content-encoding', 'transfer-encoding', 'content-length']:
                        self.send_header(k, v)
                self.end_headers()

                while True:
                    chunk = response.read(1024)
                    if not chunk:
                        break
                    try:
                        chunk_str = chunk.decode('utf-8', errors='ignore')
                        if replacements:
                            for placeholder, original_value in replacements.items():
                                chunk_str = chunk_str.replace(placeholder, original_value)
                        self.wfile.write(chunk_str.encode('utf-8'))
                    except Exception:
                        self.wfile.write(chunk)
                    self.wfile.flush()

        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            for k, v in e.headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(e.read())
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode('utf-8'))

if __name__ == '__main__':
    server = http.server.HTTPServer(('127.0.0.1', 4001), ProxyHandler)
    print("Финальный автономный SSL-бридж анонимайзера запущен на порту 4001...")
    server.serve_forever()
