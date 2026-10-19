"""Sneaker shop demo: Python 3.9+, stdlib, 127.0.0.1:8860 only.

Discounts are bounded server-side to 0..15 by the merchant's policy. There is
no authentication or trusted AI/coupon authority in this teaching API: do not
publish it as a real shop. No external AI service or secret key is required.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hmac
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import socket
import sqlite3
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
MAX_BODY = 65536
MAX_INTEGER = 9223372036854775807
DEDUP_SECONDS = 600
MAX_DISCOUNT_PCT = Decimal('15')
# Fictional demo models, not claims of availability or real brand pricing.
MODELS = [('Stride City Runner', 6490), ('Nord Pace Knit', 8490),
          ('Urban Court Low', 7999), ('Trailway Ridge', 12990),
          ('Tempo Fast Run', 15990), ('Daily Retro 88', 17990)]


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
            CREATE TABLE IF NOT EXISTS products(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>0));
            CREATE TABLE IF NOT EXISTS inventory(
                product_id INTEGER NOT NULL REFERENCES products(id), size INTEGER NOT NULL,
                stock INTEGER NOT NULL CHECK(stock>=0), PRIMARY KEY(product_id,size));
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL,
                skidka_pct TEXT NOT NULL, total INTEGER NOT NULL CHECK(total>=0),
                status TEXT NOT NULL, created_at TEXT NOT NULL, request_hash TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS requests ON orders(request_hash,created_at);
            CREATE TABLE IF NOT EXISTS order_items(
                id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
                product_id INTEGER NOT NULL REFERENCES products(id), name TEXT NOT NULL,
                size INTEGER NOT NULL, qty INTEGER NOT NULL, price INTEGER NOT NULL);
        ''')
        con.execute('BEGIN IMMEDIATE')
        if not con.execute('SELECT 1 FROM products LIMIT 1').fetchone():
            for name, price in MODELS:
                pid = con.execute('INSERT INTO products(name,price) VALUES (?,?)', (name, price)).lastrowid
                for size, stock in zip(range(39, 46), (2, 3, 4, 5, 3, 2, 1)):
                    con.execute('INSERT INTO inventory VALUES (?,?,?)', (pid, size, stock))


def utc_now():
    return datetime.now(timezone.utc)


def phone_number(raw):
    if not isinstance(raw, str) or len(raw) > 20 or not re.fullmatch(r'\+?[0-9 ()-]+', raw.strip()):
        raise ApiError(400, 'bad_phone')
    digits = ''.join(re.findall(r'[0-9]', raw))
    if not 10 <= len(digits) <= 15:
        raise ApiError(400, 'bad_phone')
    if not raw.strip().startswith('+'):
        if len(digits) == 11 and digits.startswith('8'):
            digits = '7' + digits[1:]
        elif len(digits) == 10:
            digits = '7' + digits
    return digits


def discount(value):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ApiError(400, 'bad_skidka')
    pct = Decimal(str(value))
    if not 0 <= pct <= MAX_DISCOUNT_PCT:
        raise ApiError(400, 'bad_skidka')
    return Decimal(0) if pct == 0 else pct.normalize()


def discount_number(value):
    pct = Decimal(value)
    return int(pct) if pct == pct.to_integral_value() else float(pct)


def validated(data):
    raw_items = data.get('items')
    if not isinstance(raw_items, list) or not raw_items:
        raise ApiError(400, 'no_item')
    combined = {}
    for item in raw_items:
        if not isinstance(item, dict):
            raise ApiError(400, 'no_item')
        pid, size, qty = item.get('id'), item.get('size'), item.get('qty')
        if type(pid) is not int or not 1 <= pid <= MAX_INTEGER:
            raise ApiError(400, 'no_item')
        if isinstance(size, str) and re.fullmatch(r'[0-9]{2}', size):
            size = int(size)
        if type(size) is not int or not 39 <= size <= 45:
            raise ApiError(400, 'no_size')
        if type(qty) is not int or not 1 <= qty <= MAX_INTEGER:
            raise ApiError(400, 'bad_qty')
        key = (pid, size)
        combined[key] = combined.get(key, 0) + qty
        if combined[key] > MAX_INTEGER:
            raise ApiError(400, 'bad_qty')
    items = [{'id': pid, 'size': size, 'qty': qty} for (pid, size), qty in sorted(combined.items())]
    pct = discount(data.get('skidka_pct'))
    name = data.get('name')
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ApiError(400, 'bad_name')
    name = name.strip()
    phone = phone_number(data.get('phone'))
    canonical = {'items': items, 'skidka_pct': str(pct), 'name': name, 'phone': phone}
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return items, pct, name, phone, digest


def summary(row):
    return {'order_id': row['id'], 'total': row['total'], 'skidka_pct': discount_number(row['skidka_pct']), 'status': row['status']}


def create_order(con, data):
    items, pct, name, phone, fingerprint = validated(data)
    now = utc_now()
    stamp = now.isoformat(timespec='microseconds')
    cutoff = (now - timedelta(seconds=DEDUP_SECONDS)).isoformat(timespec='microseconds')
    old = con.execute('''SELECT id,total,skidka_pct,status FROM orders
        WHERE request_hash=? AND created_at>? AND created_at<=? ORDER BY id DESC LIMIT 1''',
        (fingerprint, cutoff, stamp)).fetchone()
    if old is not None:
        return summary(old)
    lines, subtotal = [], 0
    for item in items:
        product = con.execute('SELECT id,name,price FROM products WHERE id=?', (item['id'],)).fetchone()
        if product is None:
            raise ApiError(400, 'no_item')
        row = con.execute('SELECT stock FROM inventory WHERE product_id=? AND size=?', (item['id'], item['size'])).fetchone()
        if row is None:
            raise ApiError(400, 'no_size')
        if row['stock'] < item['qty']:
            raise ApiError(400, 'out_of_stock')
        changed = con.execute('UPDATE inventory SET stock=stock-? WHERE product_id=? AND size=? AND stock>=?',
                             (item['qty'], item['id'], item['size'], item['qty'])).rowcount
        if changed != 1:
            raise ApiError(400, 'out_of_stock')
        subtotal += product['price'] * item['qty']
        if subtotal > MAX_INTEGER:
            raise ApiError(400, 'bad_qty')
        lines.append((item['id'], product['name'], item['size'], item['qty'], product['price']))
    total = int((Decimal(subtotal) * (100 - pct) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    oid = con.execute('''INSERT INTO orders(name,phone,skidka_pct,total,status,created_at,request_hash)
        VALUES (?,?,?,?,'created',?,?)''', (name, phone, str(pct), total, stamp, fingerprint)).lastrowid
    con.executemany('INSERT INTO order_items(order_id,product_id,name,size,qty,price) VALUES (?,?,?,?,?,?)',
                    [(oid,) + line for line in lines])
    return {'order_id': oid, 'total': total, 'skidka_pct': discount_number(str(pct)), 'status': 'created'}


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, socket.timeout, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        # Query strings contain phone numbers: no URL/body/access logging.
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
    def bad_constant(value):
        raise ValueError(value)

    def read_json(self):
        if self.headers.get('Transfer-Encoding'):
            raise ApiError(400, 'invalid_json')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch(r'[0-9]+', lengths[0]):
            raise ApiError(400, 'invalid_json')
        length = lengths[0].lstrip('0') or '0'
        if len(length) > 5 or int(length) > MAX_BODY:
            raise ApiError(413, 'invalid_json')
        try:
            body = self.rfile.read(int(length))
            if len(body) != int(length):
                raise ValueError()
            data = json.loads(body.decode('utf-8'), parse_constant=self.bad_constant)
            if not isinstance(data, dict):
                raise ValueError()
            json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
            return data
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(400, 'invalid_json')

    def discard_small_body(self):
        values = self.headers.get_all('Content-Length', [])
        if len(values) == 1 and re.fullmatch(r'[0-9]{1,5}', values[0]) and int(values[0]) <= MAX_BODY and not self.headers.get('Transfer-Encoding'):
            self.rfile.read(int(values[0]))

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
                    if url.path == '/api/katalog':
                        con.execute('BEGIN')
                        result = []
                        for row in con.execute('SELECT id,name,price FROM products ORDER BY id').fetchall():
                            model = dict(row)
                            model['sizes'] = {str(s['size']): s['stock'] for s in con.execute('SELECT size,stock FROM inventory WHERE product_id=? ORDER BY size', (row['id'],))}
                            result.append(model)
                        return self.reply(200, result)
                    match = re.fullmatch(r'/api/zakaz/([0-9]+)', url.path)
                    if match:
                        number = match[1].lstrip('0') or '0'
                        phones = parse_qs(url.query, keep_blank_values=True).get('phone', [])
                        if len(number) > 19 or int(number) > MAX_INTEGER or len(phones) != 1:
                            raise ApiError(404, 'not_found')
                        try:
                            phone = phone_number(phones[0])
                        except ApiError:
                            raise ApiError(404, 'not_found')
                        con.execute('BEGIN')
                        row = con.execute('SELECT id,total,skidka_pct,status,phone FROM orders WHERE id=?', (int(number),)).fetchone()
                        if row is None or not hmac.compare_digest(row['phone'], phone):
                            raise ApiError(404, 'not_found')
                        detail = summary(row)
                        detail['items'] = [{'id': i['product_id'], 'name': i['name'], 'size': i['size'],
                                            'qty': i['qty'], 'price': i['price'], 'sum': i['qty'] * i['price']}
                                           for i in con.execute('SELECT product_id,name,size,qty,price FROM order_items WHERE order_id=? ORDER BY id', (row['id'],))]
                        return self.reply(200, detail)
                elif url.path == '/api/zakaz':
                    data = self.read_json()
                    con.execute('BEGIN IMMEDIATE')
                    result = create_order(con, data)
                    con.commit()
                    return self.reply(201, result)
                if method == 'POST':
                    self.discard_small_body()
                raise ApiError(404, 'not_found')
        except ApiError as exc:
            self.reply(exc.status, {'error': exc.code})
        except (ConnectionError, socket.timeout, TimeoutError):
            raise
        except (sqlite3.Error, OSError):
            self.reply(500, {'error': 'invalid_json'})


def make_server(port=8860, db=None, quiet=False):
    db = Path(db) if db is not None else ROOT / 'sneakers.sqlite3'
    initialize(db)
    http = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    http.db, http.quiet = db, quiet
    return http


def main(argv=None):
    parser = argparse.ArgumentParser(description='Сникер-шоп: учебный API')
    parser.add_argument('--port', type=int, default=8860)
    parser.add_argument('--db', type=Path, default=ROOT / 'sneakers.sqlite3')
    args = parser.parse_args(argv)
    http = make_server(args.port, args.db)
    print('Сникер-шоп: http://127.0.0.1:%s' % http.server_port, flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()


if __name__ == '__main__':
    main()
