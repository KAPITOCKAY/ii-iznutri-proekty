"""Fitness membership demo: Python 3.9+, stdlib, loopback-only.

No PAN, CVC, expiry, merchant key or full phone is persisted. A keyed request
fingerprint distinguishes payment cards without storing them; CVC is excluded
even from that fingerprint. This simulator is NOT a real payment integration.
"""
import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import sqlite3
import sys
from urllib.parse import parse_qs, urlsplit

import oplata

ROOT = Path(__file__).resolve().parent
MAX_BODY = 65536
DEDUP_SECONDS = 600
TARIFY = [(1, 'Месяц', 3000, 30), (2, '3 месяца', 7500, 90), (3, 'Год', 24000, 365)]


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def connect(db):
    con = sqlite3.connect(str(db), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    return con


def initialize(db):
    with closing(connect(db)) as con, con:
        con.executescript('''
            CREATE TABLE IF NOT EXISTS tarify(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>0),
                days INTEGER NOT NULL CHECK(days>0));
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, tarif_id INTEGER NOT NULL REFERENCES tarify(id),
                tarif_name TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL,
                phone_hash TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('paid','declined')),
                total INTEGER NOT NULL, until TEXT NOT NULL, created_at TEXT NOT NULL,
                request_hash TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS order_requests ON orders(request_hash,created_at);
        ''')
        con.execute('BEGIN IMMEDIATE')
        if not con.execute('SELECT 1 FROM tarify LIMIT 1').fetchone():
            con.executemany('INSERT INTO tarify VALUES (?,?,?,?)', TARIFY)


def today():
    return date.today()


def utc_now():
    return datetime.now(timezone.utc)


def keyed_hash(namespace, value):
    return hmac.new(oplata.secret_key(), namespace.encode() + b'\0' + value.encode('utf-8'), hashlib.sha256).hexdigest()


def phone_digits(value):
    if not isinstance(value, str) or len(value) > 20 or not re.fullmatch(r'\+?[0-9 ()-]+', value.strip()):
        raise ApiError(400, 'bad_phone')
    digits = ''.join(re.findall(r'[0-9]', value))
    if not 10 <= len(digits) <= 15:
        raise ApiError(400, 'bad_phone')
    # An explicit +country prefix belongs to the caller: do not rewrite +81,
    # +86, +44, etc. Unprefixed ten digits / Russian trunk 8 are local forms.
    if not value.strip().startswith('+'):
        if len(digits) == 11 and digits.startswith('8'):
            digits = '7' + digits[1:]
        elif len(digits) == 10:
            digits = '7' + digits
    return digits


def valid_email(value):
    if not isinstance(value, str) or len(value) > 254:
        raise ApiError(400, 'bad_email')
    value = value.strip().lower()
    pattern = r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}"
    if not re.fullmatch(pattern, value):
        raise ApiError(400, 'bad_email')
    local = value.split('@')[0]
    if len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in local:
        raise ApiError(400, 'bad_email')
    return value


def valid_card(card, current_day):
    if not isinstance(card, dict):
        raise ApiError(400, 'bad_card')
    raw, cvc = card.get('number'), card.get('cvc')
    if not isinstance(raw, str) or len(raw) > 40 or not re.fullmatch(r'[0-9 -]+', raw):
        raise ApiError(400, 'bad_card')
    number = raw.replace(' ', '').replace('-', '')
    if not 13 <= len(number) <= 19 or len(set(number)) == 1:
        raise ApiError(400, 'bad_card')
    digits = [int(d) for d in number[::-1]]
    checksum = sum((d * 2 - 9 if d * 2 > 9 else d * 2) if i % 2 else d for i, d in enumerate(digits))
    if checksum % 10 or not isinstance(cvc, str) or not re.fullmatch(r'[0-9]{3,4}', cvc):
        raise ApiError(400, 'bad_card')
    exp = card.get('exp')
    if not isinstance(exp, str):
        raise ApiError(400, 'bad_exp')
    match = re.fullmatch(r'(0[1-9]|1[0-2])/([0-9]{2}|[0-9]{4})', exp)
    if not match:
        raise ApiError(400, 'bad_exp')
    month, year = int(match[1]), int(match[2])
    year = year + 2000 if len(match[2]) == 2 else year
    if (year, month) < (current_day.year, current_day.month):
        raise ApiError(400, 'bad_exp')
    return {'number': number, 'exp': '%02d/%04d' % (month, year), 'cvc': cvc}


def validate(data):
    tid = data.get('tarif_id')
    if type(tid) is not int or not 1 <= tid <= 9223372036854775807:
        raise ApiError(400, 'no_tarif')
    name = data.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ApiError(400, 'invalid_json')
    phone = phone_digits(data.get('phone'))
    email = valid_email(data.get('email'))
    current = today()
    card = valid_card(data.get('card'), current)
    fields = {'tarif_id': tid, 'name': name.strip(), 'phone': phone, 'email': email,
              'day': current.isoformat(), 'card_number': card['number'], 'card_exp': card['exp']}
    fingerprint = keyed_hash('request', json.dumps(fields, sort_keys=True, ensure_ascii=False))
    # CVC is never included in persistent hashes or records.
    return tid, name.strip(), email, keyed_hash('phone', phone), card, current, fingerprint


def public_order(row):
    return {'order_id': row['id'], 'status': row['status'], 'total': row['total'], 'until': row['until']}


def purchase(con, data):
    tid, name, email, phone_hash, card, current, fingerprint = validate(data)
    now = utc_now()
    stamp = now.isoformat(timespec='microseconds')
    cutoff = (now - timedelta(seconds=DEDUP_SECONDS)).isoformat(timespec='microseconds')
    previous = con.execute('''SELECT id,status,total,until FROM orders
        WHERE request_hash=? AND created_at>? AND created_at<=? ORDER BY id DESC LIMIT 1''',
        (fingerprint, cutoff, stamp)).fetchone()
    if previous is not None:
        return public_order(previous)
    tarif = con.execute('SELECT * FROM tarify WHERE id=?', (tid,)).fetchone()
    if tarif is None:
        raise ApiError(400, 'no_tarif')
    until = (current + timedelta(days=tarif['days'])).isoformat()
    status = oplata.charge(tarif['price'], card)
    if status not in ('paid', 'declined'):
        raise oplata.GatewayError('Неожиданный ответ имитатора оплаты')
    oid = con.execute('''INSERT INTO orders(tarif_id,tarif_name,name,email,phone_hash,status,total,until,created_at,request_hash)
        VALUES (?,?,?,?,?,?,?,?,?,?)''', (tid, tarif['name'], name, email, phone_hash, status, tarif['price'], until, stamp, fingerprint)).lastrowid
    return {'order_id': oid, 'status': status, 'total': tarif['price'], 'until': until}


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, socket.timeout, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        # URLs contain phone query parameters; neither URLs nor bodies are logged.
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        if getattr(self, 'command', None) != 'HEAD':
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self.request_version = 'HTTP/1.0'
        self.reply(405 if code == 501 else code, {'error': 'not_found' if code in (405, 501) else 'invalid_json'})

    @staticmethod
    def reject_constant(value):
        raise ValueError(value)

    def read_json(self):
        if self.headers.get('Transfer-Encoding'):
            raise ApiError(400, 'invalid_json')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]):
            raise ApiError(400, 'invalid_json')
        value = lengths[0].lstrip('0') or '0'
        if len(value) > 5 or int(value) > MAX_BODY:
            raise ApiError(413, 'invalid_json')
        try:
            body = self.rfile.read(int(value))
            if len(body) != int(value):
                raise ValueError()
            data = json.loads(body.decode('utf-8'), parse_constant=self.reject_constant)
            if not isinstance(data, dict):
                raise ValueError()
            json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            return data
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(400, 'invalid_json')

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def dispatch(self, method):
        try:
            try:
                url = urlsplit(self.path)
            except ValueError:
                raise ApiError(400, 'invalid_json')
            with closing(connect(self.server.db)) as con, con:
                if method == 'GET':
                    if url.path == '/api/tarify':
                        return self.reply(200, [dict(row) for row in con.execute('SELECT id,name,price,days FROM tarify ORDER BY id')])
                    match = re.fullmatch(r'/api/zakaz/([0-9]+)', url.path)
                    if match:
                        number = match[1].lstrip('0') or '0'
                        values = parse_qs(url.query, keep_blank_values=True).get('phone', [])
                        if len(number) > 19 or int(number) > 9223372036854775807 or len(values) != 1:
                            raise ApiError(404, 'not_found')
                        try:
                            digest = keyed_hash('phone', phone_digits(values[0]))
                        except ApiError:
                            raise ApiError(404, 'not_found')
                        row = con.execute('SELECT id,status,total,until,phone_hash FROM orders WHERE id=?', (int(number),)).fetchone()
                        if row is None or not hmac.compare_digest(row['phone_hash'], digest):
                            raise ApiError(404, 'not_found')
                        return self.reply(200, public_order(row))
                elif url.path == '/api/pokupka':
                    data = self.read_json()
                    con.execute('BEGIN IMMEDIATE')
                    result = purchase(con, data)
                    con.commit()
                    return self.reply(201, result)
                raise ApiError(404, 'not_found')
        except ApiError as exc:
            self.reply(exc.status, {'error': exc.code})
        except (ConnectionError, socket.timeout, TimeoutError):
            raise
        except oplata.GatewayError:
            self.reply(503, {'error': 'invalid_json'})
        except (sqlite3.Error, OSError):
            self.reply(500, {'error': 'invalid_json'})


def make_server(port=8850, db=None, quiet=False):
    oplata.secret_key()  # Fail closed before opening a listening socket or DB.
    db = Path(db) if db is not None else ROOT / 'fitnes.sqlite3'
    initialize(db)
    http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    http.db, http.quiet = db, quiet
    return http


def main(argv=None):
    parser = argparse.ArgumentParser(description='Фитнес-клуб: локальная имитация покупки')
    parser.add_argument('--port', type=int, default=8850)
    parser.add_argument('--db', type=Path, default=ROOT / 'fitnes.sqlite3')
    args = parser.parse_args(argv)
    try:
        http = make_server(args.port, args.db)
    except (oplata.GatewayError, OSError, sqlite3.Error):
        print('Ошибка запуска: проверьте путь базы, порт и переменные FITNES_SHOP_ID/FITNES_SECRET_KEY', file=sys.stderr)
        return 1
    print('Фитнес-клуб: http://127.0.0.1:%s' % http.server_port, flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
