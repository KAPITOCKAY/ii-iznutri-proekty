"""Веб-чат бота: отдаёт static/, /chat передаёт текст в dialog.otvet, dialog ходит в API server.py.
python3 bot_web.py  → http://127.0.0.1:8841  (server.py должен работать на :8840)"""
import json, secrets, urllib.request, urllib.error
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import dialog

API = "http://127.0.0.1:8840"
STATIC = Path(__file__).parent / "static"
SESSII = {}                                   # sid → состояние диалога


class Api:
    """Клиент API server.py для dialog.otvet."""
    def _zapros(self, put, telo=None):
        req = urllib.request.Request(API + put, data=json.dumps(telo).encode() if telo is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return json.loads(e.read() or b"{}") | {"_status": e.code}

    def bouquets(self): return self._zapros("/api/bouquets")
    def slots(self, date): return self._zapros(f"/api/slots?date={date}")
    def create_order(self, d): return self._zapros("/api/orders", d)


API_KL = Api()


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
        except ValueError:
            n = -1
        if not 0 <= n <= 20000:                 # тело не читаем — соединение закрываем после ответа
            self.close_connection = True
            return self._json(413, {"error": "too_big"})
        try:
            d = json.loads(self.rfile.read(n))
            if not isinstance(d, dict): raise ValueError
            sid, tekst = str(d.get("sid") or ""), str(d.get("text", ""))[:2000]
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "bad_json"})
        if sid not in SESSII:
            if len(SESSII) >= 5000: SESSII.pop(next(iter(SESSII)))   # память не растёт бесконечно: старейшая сессия уходит
            sid = secrets.token_urlsafe(12); SESSII[sid] = {}
        try:
            SESSII[sid], soobsh, knopki = dialog.otvet(SESSII[sid], tekst, API_KL)
        except Exception:
            return self._json(500, {"error": "internal"})
        self._json(200, {"sid": sid, "messages": soobsh, "buttons": knopki})

    def log_message(self, *a): pass


if __name__ == "__main__":
    print("http://127.0.0.1:8841")
    ThreadingHTTPServer(("127.0.0.1", 8841), H).serve_forever()
