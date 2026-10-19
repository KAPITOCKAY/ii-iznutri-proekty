"""Веб-чат консультанта Сникер-шопа: отдаёт static/, /chat передаёт текст в konsultant.otvet (магазин GPT на :8860, модель — Ollama).
python3 bot_web.py <папка konsultant.py>  → http://127.0.0.1:8861"""
import json, secrets, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).parent))
import konsultant
konsultant.MODEL = "qwen3.5:9b"; konsultant.TAYMAUT = 90.0     # qwen3:4b на Маке «думает вслух» и не укладывается в таймаут
STATIC = Path(__file__).parent / "static"


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(STATIC), **k)

    def _json(self, kod, d):
        b = json.dumps(d, ensure_ascii=False).encode()
        self.send_response(kod); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        if self.path != "/chat": return self._json(404, {"error": "not_found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            if not 0 <= n <= 20000: raise ValueError
            d = json.loads(self.rfile.read(n))
            if not isinstance(d, dict): raise ValueError
        except (ValueError, UnicodeDecodeError):
            self.close_connection = True
            return self._json(400, {"error": "bad_json"})
        sid = str(d.get("sid") or "") or secrets.token_urlsafe(12)
        o = konsultant.otvet(sid, str(d.get("text", ""))[:2000])
        self._json(200, {"sid": sid, "tekst": o["tekst"], "zakaz": o["zakaz"]})

    def log_message(self, *a): pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8861), H).serve_forever()
