"""Цветочный бот: Python 3.9+, stdlib, loopback-only teaching backend.

Run: python server.py [--port 8840] [--db flowers.sqlite3]
Create seller: python server.py --add-user (interactive login + password twice).
Prices are integer rubles, qty is an integer from 1 through 99.
Identical validated fields reuse a non-cancelled confirmation for 10 minutes,
including after a lost response or a restart. Cancelled/older orders do not
prevent a new confirmation. Started delivery slots cannot accept new orders.
"""
import argparse
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import getpass
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import time
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent
MAX_BODY = 65536
PASSWORD_ITERATIONS = 600000
SESSION_SECONDS = 43200
DEDUP_SECONDS = 600
SLOTS = ("10-12", "12-14", "14-16", "16-18", "18-20")
SLOT_LIMIT = 3
STATUSES = ("new", "sobiraem", "v_puti", "dostavlen", "otmenen")
SEED = [
    ("Розы", 3500, "11 красных роз, упаковка", 1),
    ("Тюльпаны", 2400, "15 тюльпанов, упаковка", 1),
    ("Пионы", 4800, "7 пионов, упаковка", 0),
    ("Цветочный микс", 3200, "Розы, хризантемы, зелень", 1),
    ("Монобукет", 2800, "9 белых хризантем", 1),
    ("На 14 февраля", 4200, "15 роз, лента", 1),
    ("Ромашки", 1600, "Ромашки и зелень", 1),
]


class ApiError(Exception):
    def __init__(self, status, code, field=None):
        self.status = status
        self.payload = {"error": code}
        if field is not None:
            self.payload["field"] = field


def connect(db):
    con = sqlite3.connect(str(db), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def initialize(db):
    with closing(connect(db)) as con, con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS bouquets(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>0),
                sostav TEXT NOT NULL, in_stock INTEGER NOT NULL CHECK(in_stock IN (0,1)));
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, login TEXT UNIQUE NOT NULL,
                pass_hash BLOB NOT NULL, salt BLOB NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(
                id INTEGER PRIMARY KEY AUTOINCREMENT, bouquet_id INTEGER NOT NULL REFERENCES bouquets(id),
                bouquet TEXT NOT NULL, price INTEGER NOT NULL, qty INTEGER NOT NULL,
                name TEXT NOT NULL, phone TEXT NOT NULL, address TEXT NOT NULL,
                date TEXT NOT NULL, slot TEXT NOT NULL, card_text TEXT NOT NULL,
                status TEXT NOT NULL, total INTEGER NOT NULL, created_at TEXT NOT NULL,
                token TEXT UNIQUE NOT NULL, request_hash TEXT UNIQUE NOT NULL);
            CREATE INDEX IF NOT EXISTS orders_slot ON orders(date,slot,status);
        """)
        con.execute("BEGIN IMMEDIATE")
        if not con.execute("SELECT 1 FROM bouquets LIMIT 1").fetchone():
            con.executemany("INSERT INTO bouquets(name,price,sostav,in_stock) VALUES (?,?,?,?)", SEED)


def local_now():
    return datetime.now()


def utc_now():
    return datetime.now(timezone.utc)


def today():
    """Dates use the machine's local calendar, not a user-supplied timezone."""
    return local_now().date()


def slot_started(day, slot, now=None):
    now = local_now() if now is None else now
    start = datetime.combine(date.fromisoformat(day), datetime.min.time()).replace(hour=int(slot.split("-")[0]))
    return now >= start


def valid_date(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError()
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "bad_date", "date")
    current = today()
    if not current <= parsed <= current + timedelta(days=30):
        raise ApiError(400, "bad_date", "date")
    return value


def text(data, key, maximum, empty=False):
    value = data.get(key)
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        if key == "phone":
            raise ApiError(400, "bad_phone", key)
        raise ApiError(400, "card_too_long" if key == "card_text" and isinstance(value, str) and len(value) > maximum else "invalid_" + key, key)
    return value if empty else value.strip()


def order_data(data):
    pid, qty = data.get("bouquet_id"), data.get("qty")
    if type(pid) is not int or not 1 <= pid <= 9223372036854775807:
        raise ApiError(400, "no_bouquet", "bouquet_id")
    if type(qty) is not int or not 1 <= qty <= 99:
        raise ApiError(400, "invalid_qty", "qty")
    name = text(data, "name", 100)
    phone = text(data, "phone", 20)
    digits = "".join(re.findall(r"[0-9]", phone))
    if not re.fullmatch(r"[0-9+ ()-]+", phone) or not 10 <= len(digits) <= 15:
        raise ApiError(400, "bad_phone", "phone")
    address = text(data, "address", 300)
    day = valid_date(data.get("date"))
    slot = data.get("slot")
    if slot not in SLOTS:
        raise ApiError(400, "invalid_slot", "slot")
    card = text(data, "card_text", 200, empty=True)
    fields = {"bouquet_id": pid, "qty": qty, "name": name, "phone": phone,
              "address": address, "date": day, "slot": slot, "card_text": card}
    canonical = dict(fields, phone=digits)
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return fields, digest


def confirmation(row):
    return {"order_id": row["id"], "token": row["token"], "total": row["total"]}


def create_order(con, data):
    fields, digest = order_data(data)
    now = utc_now()
    stamp = now.isoformat(timespec="microseconds")
    cutoff = (now - timedelta(seconds=DEDUP_SECONDS)).isoformat(timespec="microseconds")
    existing = con.execute("""SELECT id,token,total FROM orders WHERE request_hash=?
        AND status!='otmenen' AND created_at>? AND created_at<=?""", (digest, cutoff, stamp)).fetchone()
    if existing:
        return confirmation(existing)
    bouquet = con.execute("SELECT * FROM bouquets WHERE id=?", (fields["bouquet_id"],)).fetchone()
    if bouquet is None:
        raise ApiError(400, "no_bouquet", "bouquet_id")
    if not bouquet["in_stock"]:
        raise ApiError(400, "out_of_stock", "bouquet_id")
    if slot_started(fields["date"], fields["slot"]):
        raise ApiError(400, "slot_full", "slot")
    count = con.execute("SELECT COUNT(*) FROM orders WHERE date=? AND slot=? AND status!='otmenen'", (fields["date"], fields["slot"])).fetchone()[0]
    if count >= SLOT_LIMIT:
        raise ApiError(400, "slot_full", "slot")
    total = bouquet["price"] * fields["qty"]
    if total > 9223372036854775807:
        raise ApiError(400, "invalid_qty", "qty")
    # Older DBs have an inline UNIQUE constraint on request_hash. Keep that
    # schema intact: retire an expired/cancelled fingerprint into a namespace
    # that cannot collide with a 64-character SHA256 digest. Historical orders,
    # tokens and timestamps remain unchanged. BEGIN IMMEDIATE serializes this
    # with both new confirmations and cancellations.
    con.execute("UPDATE orders SET request_hash='history:'||id||':'||request_hash WHERE request_hash=?", (digest,))
    token = secrets.token_urlsafe(16)
    oid = con.execute("""INSERT INTO orders(bouquet_id,bouquet,price,qty,name,phone,address,date,slot,
        card_text,status,total,created_at,token,request_hash) VALUES (?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?)""",
        (fields["bouquet_id"], bouquet["name"], bouquet["price"], fields["qty"], fields["name"], fields["phone"],
         fields["address"], fields["date"], fields["slot"], fields["card_text"], total, stamp, token, digest)).lastrowid
    return {"order_id": oid, "token": token, "total": total}


def add_user(db, login, password):
    if not isinstance(login, str) or not re.fullmatch(r"[a-z0-9_]{3,32}", login):
        raise ValueError("Логин: 3–32 символа a-z, 0-9 или _")
    if not isinstance(password, str) or not 8 <= len(password) <= 1000:
        raise ValueError("Пароль: от 8 до 1000 символов")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    with closing(connect(db)) as con, con:
        try:
            con.execute("INSERT INTO users(login,pass_hash,salt,created_at) VALUES (?,?,?,?)",
                (login, digest, salt, datetime.now(timezone.utc).isoformat(timespec="microseconds")))
        except sqlite3.IntegrityError:
            raise ValueError("Логин уже существует")


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, socket.timeout, TimeoutError):
            self.close_connection = True

    def log_message(self, fmt, *args):
        # Request URLs may contain bearer tracking tokens. No access/body/
        # cookie logs; startup only prints the loopback address.
        pass

    def reply(self, status, payload, headers=None):
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if getattr(self, "command", None) != "HEAD":
            self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self.close_connection = True
        self.request_version = "HTTP/1.0"
        self.reply(405 if code == 501 else code, {"error": "invalid_http_request"})

    @staticmethod
    def bad_constant(value):
        raise ValueError(value)

    def discard_small_body(self):
        # Read a bounded, correctly framed rejected body before closing. On
        # Windows an early close with unread bytes can reset the connection
        # and hide the JSON error from the client.
        values = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(values) != 1:
            return
        length = values[0].lstrip("0") or "0"
        if re.fullmatch(r"[0-9]{1,5}", length) and int(length) <= MAX_BODY:
            self.rfile.read(int(length))

    def read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(400, "invalid_content_length", "body")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise ApiError(400, "invalid_content_length", "body")
        length = lengths[0].lstrip("0") or "0"
        if len(length) > 5 or int(length) > MAX_BODY:
            raise ApiError(413, "body_too_large")
        try:
            raw = self.rfile.read(int(length))
            if len(raw) != int(length):
                raise ValueError()
            result = json.loads(raw.decode("utf-8"), parse_constant=self.bad_constant)
            if not isinstance(result, dict):
                raise ValueError()
            json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            return result
        except (ValueError, UnicodeError, RecursionError):
            raise ApiError(400, "invalid_json", "body")

    def session(self, con):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            token = cookies["sid"].value if "sid" in cookies else ""
        except CookieError:
            token = ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            raise ApiError(401, "unauthorized")
        row = con.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?", (hashlib.sha256(token.encode()).hexdigest(), time.time())).fetchone()
        if row is None:
            raise ApiError(401, "unauthorized")
        return row[0]

    def login(self, con, data):
        login, password = data.get("login"), data.get("password")
        if not isinstance(login, str) or not isinstance(password, str) or len(login) > 1000 or len(password) > 1000:
            raise ApiError(400, "invalid_login", "login")
        user = con.execute("SELECT * FROM users WHERE login=?", (login,)).fetchone()
        salt = user["salt"] if user else bytes(16)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
        expected = user["pass_hash"] if user else bytes(32)
        valid = hmac.compare_digest(candidate, expected)
        if not valid or user is None:
            raise ApiError(401, "invalid_credentials")
        token = secrets.token_urlsafe(32)
        con.execute("BEGIN IMMEDIATE")
        con.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
        con.execute("INSERT INTO sessions VALUES (?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user["id"], time.time() + SESSION_SECONDS))
        con.commit()
        return self.reply(200, {"login": user["login"]}, headers={"Set-Cookie": "sid=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200" % token})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        try:
            try:
                url = urlsplit(self.path)
            except ValueError:
                raise ApiError(400, "invalid_path", "path")
            if method == "POST" and (len(self.headers.get_all("Content-Type", [])) != 1 or self.headers.get_content_type() != "application/json"):
                self.discard_small_body()
                raise ApiError(415, "json_required")
            if method == "GET" and not url.path.startswith("/api/"):
                return self.static_response(url.path)
            with closing(connect(self.server.db)) as con, con:
                if method == "GET":
                    if url.path == "/api/bouquets":
                        rows = [dict(row) for row in con.execute("SELECT id,name,price,sostav,in_stock FROM bouquets ORDER BY id")]
                        for row in rows:
                            row["in_stock"] = bool(row["in_stock"])
                        return self.reply(200, rows)
                    if url.path == "/api/slots":
                        values = parse_qs(url.query, keep_blank_values=True).get("date", [])
                        if len(values) != 1:
                            raise ApiError(400, "bad_date", "date")
                        day = valid_date(values[0])
                        used = dict(con.execute("SELECT slot,COUNT(*) FROM orders WHERE date=? AND status!='otmenen' GROUP BY slot", (day,)))
                        now = local_now()
                        return self.reply(200, [{"slot": slot, "free": 0 if slot_started(day, slot, now) else max(0, SLOT_LIMIT - used.get(slot, 0))} for slot in SLOTS])
                    match = re.fullmatch(r"/api/track/([A-Za-z0-9_-]{22})", url.path)
                    if match:
                        row = con.execute("SELECT status,bouquet,date,slot FROM orders WHERE token=?", (match[1],)).fetchone()
                        if row is None:
                            raise ApiError(404, "not_found")
                        return self.reply(200, dict(row))
                    if url.path == "/api/orders":
                        self.session(con)
                        rows = con.execute("""SELECT id,bouquet_id,bouquet,price,qty,name,phone,address,date,slot,
                            card_text,status,total,created_at FROM orders ORDER BY id DESC""").fetchall()
                        return self.reply(200, [dict(row) for row in rows])
                else:
                    if url.path == "/api/login":
                        return self.login(con, self.read_json())
                    match = re.fullmatch(r"/api/orders/([0-9]+)/status", url.path)
                    if url.path != "/api/orders" and not match:
                        self.discard_small_body()
                        raise ApiError(404, "not_found")
                    data = self.read_json()
                    con.execute("BEGIN IMMEDIATE")
                    if url.path == "/api/orders":
                        result = create_order(con, data)
                        con.commit()
                        return self.reply(201, result)
                    self.session(con)
                    number = match[1].lstrip("0") or "0"
                    if len(number) > 19 or int(number) > 9223372036854775807:
                        raise ApiError(404, "not_found")
                    oid = int(number)
                    row = con.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
                    if row is None:
                        raise ApiError(404, "not_found")
                    status = data.get("status")
                    if status not in STATUSES:
                        raise ApiError(400, "invalid_status", "status")
                    old = row[0]
                    allowed = {"new": ("sobiraem", "otmenen"), "sobiraem": ("v_puti", "otmenen"),
                               "v_puti": ("dostavlen", "otmenen"), "dostavlen": (), "otmenen": ()}
                    if status != old and status not in allowed[old]:
                        raise ApiError(400, "invalid_transition", "status")
                    con.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
                    con.commit()
                    return self.reply(200, {"order_id": oid, "status": status})
                raise ApiError(404, "not_found")
        except ApiError as exc:
            self.reply(exc.status, exc.payload)
        except (ConnectionError, socket.timeout, TimeoutError):
            raise
        except (sqlite3.Error, OSError):
            self.reply(500, {"error": "storage_error"})

    def static_response(self, path):
        name = "index.html" if path == "/" else unquote(path[8:]).replace("\\", "/") if path.startswith("/static/") else None
        if name is None:
            raise ApiError(404, "not_found")
        try:
            target = (self.server.static_root / name).resolve()
            target.relative_to(self.server.static_root)
            if not target.is_file():
                raise ValueError()
            raw = target.read_bytes()
        except (ValueError, OSError):
            raise ApiError(404, "not_found")
        self.send_response(200)
        mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_header("Content-Type", mime + ("; charset=utf-8" if mime.startswith("text/") or mime == "application/javascript" else ""))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(raw)


def make_server(port=8840, db=None, static_root=None, quiet=False):
    db = Path(db) if db is not None else ROOT / "flowers.sqlite3"
    initialize(db)
    http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    http.db = db
    http.static_root = (Path(static_root) if static_root is not None else ROOT / "static").resolve()
    http.quiet = quiet
    return http


def main(argv=None):
    parser = argparse.ArgumentParser(description="Цветочный бот — учебный сервер")
    parser.add_argument("--port", type=int, default=8840)
    parser.add_argument("--db", type=Path, default=ROOT / "flowers.sqlite3")
    parser.add_argument("--add-user", action="store_true")
    args = parser.parse_args(argv)
    if args.add_user:
        try:
            initialize(args.db)
            login = input("Логин продавца: ")
            first = getpass.getpass("Пароль (от 8 символов): ")
            second = getpass.getpass("Повторите пароль: ")
            if first != second:
                raise ValueError("Пароли не совпадают")
            add_user(args.db, login, first)
        except (ValueError, EOFError) as exc:
            parser.error(str(exc))
        print("Продавец добавлен", flush=True)
        return
    http = make_server(args.port, args.db)
    print("Цветочный бот: http://127.0.0.1:%s" % http.server_port, flush=True)
    try:
        http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        http.server_close()


if __name__ == "__main__":
    main()
