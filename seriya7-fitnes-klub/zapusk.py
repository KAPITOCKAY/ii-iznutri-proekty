"""Сайт целиком: API GPT (server.py) + страница DeepSeek (static/index.html). Ключ — из .env."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for stroka in (ROOT / '.env').read_text(encoding='utf-8').splitlines():
    if '=' in stroka and not stroka.startswith('#'):
        k, v = stroka.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

import server  # noqa: E402

STRANICA = (ROOT / 'static' / 'index.html').read_bytes()
_dispatch = server.Handler.dispatch


def dispatch(self, method):
    if method == 'GET' and self.path.split('?')[0] in ('/', '/index.html'):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(STRANICA)))
        self.end_headers()
        self.wfile.write(STRANICA)
        return
    _dispatch(self, method)


server.Handler.dispatch = dispatch

if __name__ == '__main__':
    raise SystemExit(server.main())
