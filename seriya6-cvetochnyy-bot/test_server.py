"""Contract-first HTTP tests; temporary files stay in this project."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import getpass
import hashlib
import http.client
import importlib.util
import io
import json
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
server = None
if (ROOT / "server.py").exists():
    spec = importlib.util.spec_from_file_location("flower_server", ROOT / "server.py")
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = secrets.token_urlsafe(18)
        cls.salt = secrets.token_bytes(16)
        cls.digest = hashlib.pbkdf2_hmac("sha256", cls.password.encode(), cls.salt, 600000)

    def setUp(self):
        self.assertIsNotNone(server, "Красная фаза: server.py отсутствует")
        self.temp = tempfile.TemporaryDirectory(prefix="test_", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "test.sqlite3"
        self.static = self.root / "static"
        self.static.mkdir()
        self.http = server.make_server(port=0, db=self.db, static_root=self.static, quiet=True)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.http.server_port
        self.cookie = ""
        with closing(server.connect(self.db)) as con, con:
            con.execute("INSERT INTO users(login,pass_hash,salt,created_at) VALUES (?,?,?,?)",
                        ("florist", self.digest, self.salt, "2026-01-01T00:00:00+00:00"))
        self.catalog = self.req("GET", "/api/bouquets")[1]
        self.pid = next(p["id"] for p in self.catalog if p["in_stock"])
        self.day = (date.today() + timedelta(days=1)).isoformat()

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def req(self, method, path, data=None, raw=None, cookie=None, headers=None):
        body = raw if raw is not None else json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        headers = dict(headers) if headers is not None else {"Content-Type": "application/json"}
        cookie = self.cookie if cookie is None else cookie
        if cookie:
            headers["Cookie"] = cookie
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        try:
            con.request(method, path, body, headers)
            response = con.getresponse()
            self.last_headers = dict(response.getheaders())
            payload = response.read()
            if response.getheader("Content-Type", "").startswith("application/json"):
                self.assertIn("utf-8", response.getheader("Content-Type").lower())
                if method != "HEAD":
                    payload = json.loads(payload)
            return response.status, payload
        finally:
            con.close()

    def payload(self, **changes):
        data = {"bouquet_id": self.pid, "qty": 1, "name": "Иван", "phone": "+7 (999) 123-45-67",
                "address": "Москва, улица Ленина, 1", "date": self.day, "slot": "10-12", "card_text": "С любовью"}
        data.update(changes)
        return data

    def create(self, **changes):
        code, order = self.req("POST", "/api/orders", self.payload(**changes), cookie="")
        self.assertEqual(code, 201, order)
        self.assertEqual(set(order), {"order_id", "token", "total"})
        return order

    def login(self):
        code, body = self.req("POST", "/api/login", {"login": "florist", "password": self.password}, cookie="")
        self.assertEqual(code, 200, body)
        self.cookie = self.last_headers["Set-Cookie"].split(";", 1)[0]
        return body

    def orders(self):
        if not self.cookie:
            self.login()
        code, rows = self.req("GET", "/api/orders")
        self.assertEqual(code, 200, rows)
        return rows

    def change(self, oid, status):
        if not self.cookie:
            self.login()
        return self.req("POST", "/api/orders/%s/status" % oid, {"status": status})

    def error(self, response, status, field=None):
        code, body = response
        self.assertEqual(code, status, body)
        self.assertIsInstance(body, dict)
        self.assertIsInstance(body.get("error"), str)
        self.assertNotIn("Traceback", json.dumps(body))
        if field is not None:
            self.assertEqual(set(body), {"error", "field"})
            self.assertEqual(body["field"], field)

    def test_catalog_seed_and_loopback(self):
        self.assertEqual(self.http.server_address[0], "127.0.0.1")
        self.assertGreaterEqual(len(self.catalog), 6)
        names = " ".join(p["name"].lower() for p in self.catalog)
        for word in ("роз", "тюльпан", "пион", "микс", "монобукет", "14 февраля"):
            self.assertIn(word, names)
        for bouquet in self.catalog:
            self.assertEqual(set(bouquet), {"id", "name", "price", "sostav", "in_stock"})
            self.assertIs(type(bouquet["price"]), int)
            self.assertGreater(bouquet["price"], 0)
            self.assertIsInstance(bouquet["in_stock"], bool)
        server.initialize(self.db)
        self.assertEqual(self.req("GET", "/api/bouquets")[1], self.catalog)

    def test_five_slots_with_capacity_three(self):
        code, slots = self.req("GET", "/api/slots?date=" + self.day)
        self.assertEqual(code, 200)
        self.assertEqual(slots, [{"slot": s, "free": 3} for s in ("10-12", "12-14", "14-16", "16-18", "18-20")])

    def test_today_started_slots_are_zero_and_reject_new_order(self):
        fixed = datetime(2030, 3, 10, 15, 0)
        with patch.object(server, "local_now", return_value=fixed, create=True), patch.object(server, "today", return_value=fixed.date()):
            code, slots = self.req("GET", "/api/slots?date=2030-03-10")
            self.assertEqual(code, 200)
            self.assertEqual([s["free"] for s in slots], [0, 0, 0, 3, 3])
            for slot in ("10-12", "12-14", "14-16"):
                response = self.req("POST", "/api/orders", self.payload(date="2030-03-10", slot=slot))
                self.assertEqual(response, (400, {"error": "slot_full", "field": "slot"}))
            self.create(date="2030-03-10", slot="16-18")
            self.create(date="2030-03-11", slot="10-12")

    def test_slot_start_boundaries_do_not_use_real_clock(self):
        cases = ((0, 0, 0, [3, 3, 3, 3, 3]), (9, 59, 59, [3, 3, 3, 3, 3]),
                 (10, 0, 0, [0, 3, 3, 3, 3]), (12, 0, 0, [0, 0, 3, 3, 3]),
                 (18, 0, 0, [0, 0, 0, 0, 0]), (23, 59, 59, [0, 0, 0, 0, 0]))
        for hour, minute, second, expected in cases:
            fixed = datetime(2030, 3, 10, hour, minute, second)
            with self.subTest(time=fixed.time()), patch.object(server, "local_now", return_value=fixed, create=True), patch.object(server, "today", return_value=fixed.date()):
                self.assertEqual([s["free"] for s in self.req("GET", "/api/slots?date=2030-03-10")[1]], expected)
                self.assertEqual([s["free"] for s in self.req("GET", "/api/slots?date=2030-03-11")[1]], [3] * 5)

    def test_post_exact_slot_start_rejected_but_one_second_before_allowed(self):
        with patch.object(server, "local_now", return_value=datetime(2030, 3, 10, 9, 59, 59), create=True), patch.object(server, "today", return_value=date(2030, 3, 10)):
            self.create(date="2030-03-10")
        with patch.object(server, "local_now", return_value=datetime(2030, 3, 10, 10, 0), create=True), patch.object(server, "today", return_value=date(2030, 3, 10)):
            self.assertEqual(self.req("POST", "/api/orders", self.payload(date="2030-03-10", name="Новый клиент")),
                             (400, {"error": "slot_full", "field": "slot"}))

    def test_slot_capacity_and_full_error_400(self):
        for name in ("Первый", "Второй", "Третий"):
            self.create(name=name)
        self.error(self.req("POST", "/api/orders", self.payload(name="Четвёртый")), 400, "slot")
        slots = self.req("GET", "/api/slots?date=" + self.day)[1]
        self.assertEqual(slots[0]["free"], 0)
        self.assertEqual(slots[1]["free"], 3)

    def test_february_fourteenth_capacity_is_also_three(self):
        with patch.object(server, "today", return_value=date(2030, 2, 14)), patch.object(server, "local_now", return_value=datetime(2030, 2, 14, 0, 0)):
            for index in range(3):
                self.create(date="2030-02-14", name="Клиент %s" % index)
            self.error(self.req("POST", "/api/orders", self.payload(date="2030-02-14", name="Лишний")), 400, "slot")
            self.assertEqual(self.req("GET", "/api/slots?date=2030-02-14")[1][0]["free"], 0)

    def test_order_response_and_integer_ruble_total(self):
        order = self.create(qty=3, total=1, status="dostavlen", price=1)
        price = next(p["price"] for p in self.catalog if p["id"] == self.pid)
        self.assertEqual(order["total"], price * 3)
        self.assertIs(type(order["total"]), int)
        self.assertRegex(order["token"], r"^[A-Za-z0-9_-]{22}$")
        self.assertEqual(self.req("GET", "/api/slots?date=" + self.day)[1][0]["free"], 2)

    def test_missing_required_fields(self):
        for field in self.payload():
            data = self.payload()
            del data[field]
            self.error(self.req("POST", "/api/orders", data), 400, field)

    def test_invalid_bouquet_and_not_in_stock(self):
        for value in (None, True, "1", 0, -1, 1.5, 10 ** 100, 999999):
            self.error(self.req("POST", "/api/orders", self.payload(bouquet_id=value)), 400, "bouquet_id")
        unavailable = next(p for p in self.catalog if not p["in_stock"])
        self.error(self.req("POST", "/api/orders", self.payload(bouquet_id=unavailable["id"])), 400, "bouquet_id")

    def test_contract_error_codes_are_stable(self):
        unavailable = next(p["id"] for p in self.catalog if not p["in_stock"])
        cases = (({"bouquet_id": 999999}, "no_bouquet", "bouquet_id"),
                 ({"bouquet_id": None}, "no_bouquet", "bouquet_id"),
                 ({"bouquet_id": unavailable}, "out_of_stock", "bouquet_id"),
                 ({"date": (date.today() + timedelta(days=40)).isoformat()}, "bad_date", "date"),
                 ({"date": (date.today() - timedelta(days=1)).isoformat()}, "bad_date", "date"),
                 ({"date": "2030-02-31"}, "bad_date", "date"),
                 ({"date": None}, "bad_date", "date"),
                 ({"phone": "abc"}, "bad_phone", "phone"),
                 ({"phone": ""}, "bad_phone", "phone"),
                 ({"phone": None}, "bad_phone", "phone"),
                 ({"card_text": "x" * 201}, "card_too_long", "card_text"))
        for changes, expected, field in cases:
            with self.subTest(changes=changes):
                response = self.req("POST", "/api/orders", self.payload(**changes))
                self.error(response, 400, field)
                self.assertEqual(response[1]["error"], expected)
        self.assertEqual(self.req("GET", "/api/slots?date=31.02")[1], {"error": "bad_date", "field": "date"})
        for number in range(3):
            self.create(name="Заполнение %s" % number)
        self.assertEqual(self.req("POST", "/api/orders", self.payload(name="Лишний"))[1],
                         {"error": "slot_full", "field": "slot"})

    def test_invalid_quantities(self):
        for qty in (0, -1, 1.5, "1,5", "1.5", "abc", "", True, None, 10 ** 100):
            self.error(self.req("POST", "/api/orders", self.payload(qty=qty)), 400, "qty")

    def test_quantity_boundaries_one_to_ninety_nine(self):
        price = next(p["price"] for p in self.catalog if p["id"] == self.pid)
        for qty in (1, 99):
            self.assertEqual(self.create(qty=qty)["total"], price * qty)
        for qty in (0, -1, 100, 1000, 9_000_000_000_000_000_000, 9223372036854775807):
            with self.subTest(qty=qty):
                response = self.req("POST", "/api/orders", self.payload(qty=qty))
                self.assertEqual(response, (400, {"error": "invalid_qty", "field": "qty"}))

    def test_retry_keeps_original_price_after_catalog_change(self):
        order = self.create(qty=2)
        with closing(server.connect(self.db)) as con, con:
            con.execute("UPDATE bouquets SET price=price+1000,in_stock=0 WHERE id=?", (self.pid,))
        self.assertEqual(self.create(qty=2), order)
        self.assertEqual(self.orders()[0]["total"], order["total"])

    def test_key_order_and_phone_format_do_not_duplicate_confirmation(self):
        first = self.create()
        data = dict(reversed(list(self.payload(phone="79991234567").items())))
        self.assertEqual(self.req("POST", "/api/orders", data), (201, first))
        self.assertEqual(len(self.orders()), 1)

    def test_text_empty_spaces_overlong_and_wrong_type(self):
        for field in ("name", "phone", "address"):
            for value in ("", "   ", "x" * 10000, None, 123):
                self.error(self.req("POST", "/api/orders", self.payload(**{field: value})), 400, field)

    def test_unicode_name_card_and_empty_card(self):
        first = self.create(name="Алёна 🌷", card_text="Ёжик, с днём рождения! 🌹")
        row = self.orders()[0]
        self.assertEqual(row["name"], "Алёна 🌷")
        self.assertEqual(row["card_text"], "Ёжик, с днём рождения! 🌹")
        self.assertEqual(row["id"], first["order_id"])
        self.create(name="Без открытки", card_text="")

    def test_phone_digit_count_and_injections(self):
        for phone in ("++++++++++1", "1" * 9, "1" * 16, "123456789x", "🌷" * 10, "' OR 1=1 --"):
            self.error(self.req("POST", "/api/orders", self.payload(phone=phone)), 400, "phone")
        self.create(phone="1234567890")

    def test_card_text_200_boundary(self):
        self.create(card_text="🌷" * 200)
        for value in ("я" * 201, "x" * 10000, None, 1):
            self.error(self.req("POST", "/api/orders", self.payload(card_text=value)), 400, "card_text")

    def test_date_validation_and_thirty_day_boundary(self):
        with patch.object(server, "today", return_value=date(2030, 1, 20)), patch.object(server, "local_now", return_value=datetime(2030, 1, 20, 0, 0)):
            for value in ("2030-01-19", "2030-02-31", "2030-02-20", "9999-12-31", "2030-1-20", "2030-01-20T00:00:00", "", "   ", None, 0):
                self.error(self.req("POST", "/api/orders", self.payload(date=value)), 400, "date")
            self.create(date="2030-01-20")
            self.create(date="2030-02-19")

    def test_slots_bad_or_missing_date(self):
        for query in ("", "?date=", "?date=31.02", "?date=9999-12-31", "?date=x&date=y"):
            self.error(self.req("GET", "/api/slots" + query), 400, "date")

    def test_midnight_date_rollover(self):
        with patch.object(server, "today", return_value=date(2030, 1, 31)), patch.object(server, "local_now", return_value=datetime(2030, 1, 31, 0, 0)):
            self.create(date="2030-01-31")
        with patch.object(server, "today", return_value=date(2030, 2, 1)), patch.object(server, "local_now", return_value=datetime(2030, 2, 1, 0, 0)):
            self.error(self.req("POST", "/api/orders", self.payload(date="2030-01-31", name="Поздно")), 400, "date")
            self.create(date="2030-02-01")

    def test_unknown_slot(self):
        for value in ("00-02", "10:00", "", None, 12):
            self.error(self.req("POST", "/api/orders", self.payload(slot=value)), 400, "slot")

    def test_repeated_confirmation_is_idempotent_even_when_slot_full(self):
        first = self.create()
        self.create(name="Второй")
        self.create(name="Третий")
        duplicate = self.create()
        self.assertEqual(first, duplicate)
        self.assertEqual(len(self.orders()), 3)

    def test_confirmation_after_cancellation_creates_new_order(self):
        first = self.create()
        self.assertEqual(self.change(first["order_id"], "otmenen")[0], 200)
        second = self.create()
        self.assertNotEqual(first["order_id"], second["order_id"])
        self.assertNotEqual(first["token"], second["token"])
        self.assertEqual(self.req("GET", "/api/track/" + first["token"])[1]["status"], "otmenen")
        self.assertEqual(self.req("GET", "/api/track/" + second["token"])[1]["status"], "new")
        self.assertEqual(len(self.orders()), 2)
        self.assertEqual(self.req("GET", "/api/slots?date=" + self.day)[1][0]["free"], 2)

    def test_dedup_window_just_before_and_at_ten_minutes(self):
        start = datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)
        with patch.object(server, "utc_now", return_value=start, create=True):
            first = self.create()
        with patch.object(server, "utc_now", return_value=start + timedelta(minutes=10, microseconds=-1), create=True):
            self.assertEqual(self.create(), first)
        with patch.object(server, "utc_now", return_value=start + timedelta(minutes=10), create=True):
            second = self.create()
            self.assertNotEqual(second["order_id"], first["order_id"])
        with patch.object(server, "utc_now", return_value=start + timedelta(minutes=10, seconds=1), create=True):
            self.assertEqual(self.create(), second)
        self.assertEqual(len(self.orders()), 2)

    def test_expired_dedup_still_checks_slot_capacity(self):
        start = datetime(2030, 1, 1, 8, 0, tzinfo=timezone.utc)
        with patch.object(server, "utc_now", return_value=start, create=True):
            self.create()
            self.create(name="Второй")
            self.create(name="Третий")
        with patch.object(server, "utc_now", return_value=start + timedelta(minutes=11), create=True):
            self.assertEqual(self.req("POST", "/api/orders", self.payload()),
                             (400, {"error": "slot_full", "field": "slot"}))

    def test_concurrent_retry_after_cancellation_creates_only_one_replacement(self):
        first = self.create()
        self.change(first["order_id"], "otmenen")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.req("POST", "/api/orders", self.payload()), range(2)))
        self.assertEqual([code for code, _ in results], [201, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertNotEqual(results[0][1]["order_id"], first["order_id"])
        self.assertEqual(len(self.orders()), 2)

    def test_expired_order_keeps_token_and_data_in_existing_unique_schema(self):
        first = self.create()
        with closing(server.connect(self.db)) as con, con:
            con.execute("UPDATE orders SET created_at=? WHERE id=?",
                        ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(timespec="microseconds"), first["order_id"]))
            old = dict(con.execute("SELECT * FROM orders WHERE id=?", (first["order_id"],)).fetchone())
        second = self.create()
        self.assertNotEqual(first["order_id"], second["order_id"])
        with closing(server.connect(self.db)) as con:
            saved = dict(con.execute("SELECT * FROM orders WHERE id=?", (first["order_id"],)).fetchone())
        for key in old:
            if key != "request_hash":
                self.assertEqual(saved[key], old[key])
        self.assertEqual(self.req("GET", "/api/track/" + first["token"])[0], 200)

    def test_cancelled_order_can_be_confirmed_again_next_day(self):
        with patch.object(server, "local_now", return_value=datetime(2030, 1, 10, 8)), patch.object(server, "utc_now", return_value=datetime(2030, 1, 10, 8, tzinfo=timezone.utc)):
            first = self.create(date="2030-01-11")
            self.change(first["order_id"], "otmenen")
        with patch.object(server, "local_now", return_value=datetime(2030, 1, 11, 8)), patch.object(server, "utc_now", return_value=datetime(2030, 1, 11, 8, tzinfo=timezone.utc)):
            second = self.create(date="2030-01-11")
        self.assertNotEqual(first["order_id"], second["order_id"])
        self.assertEqual(self.req("GET", "/api/track/" + second["token"])[1]["status"], "new")

    def test_concurrent_expired_confirmation_creates_one_new_order(self):
        start = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
        with patch.object(server, "utc_now", return_value=start):
            first = self.create()
        with patch.object(server, "utc_now", return_value=start + timedelta(minutes=11)):
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.req("POST", "/api/orders", self.payload()), range(2)))
        self.assertEqual([code for code, _ in results], [201, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertNotEqual(first["order_id"], results[0][1]["order_id"])
        self.assertEqual(len(self.orders()), 2)

    def test_concurrent_identical_confirmation_is_one_order(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.req("POST", "/api/orders", self.payload()), range(2)))
        self.assertEqual([code for code, _ in results], [201, 201])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(len(self.orders()), 1)

    def test_network_retry_after_committed_but_lost_reply(self):
        sent = threading.Event()
        original = server.Handler.reply

        def lost(handler, code, body, **kwargs):
            if code == 201:
                sent.set()
                raise BrokenPipeError("simulated lost response")
            return original(handler, code, body, **kwargs)

        with patch.object(server.Handler, "reply", lost):
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as client:
                body = json.dumps(self.payload()).encode()
                client.sendall(b"POST /api/orders HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
                self.assertEqual(client.recv(1024), b"")
                self.assertTrue(sent.wait(2))
        retried = self.create()
        self.assertEqual(self.orders()[0]["id"], retried["order_id"])
        self.assertEqual(len(self.orders()), 1)

    def test_last_slot_concurrent_distinct_requests(self):
        self.create(name="Первый")
        self.create(name="Второй")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda name: self.req("POST", "/api/orders", self.payload(name=name)), ("А", "Б")))
        self.assertEqual(sorted(code for code, _ in results), [201, 400])
        self.assertEqual(len(self.orders()), 3)
        self.assertEqual(self.req("GET", "/api/slots?date=" + self.day)[1][0]["free"], 0)

    def test_tracking_is_minimal_and_status_updates_visible(self):
        order = self.create()
        path = "/api/track/" + order["token"]
        code, result = self.req("GET", path)
        self.assertEqual(code, 200)
        self.assertEqual(set(result), {"status", "bouquet", "date", "slot"})
        self.assertEqual(result["status"], "new")
        self.assertEqual(result["date"], self.day)
        self.assertIsInstance(result["bouquet"], str)
        for status in ("sobiraem", "v_puti", "dostavlen"):
            self.assertEqual(self.change(order["order_id"], status)[0], 200)
            self.assertEqual(self.req("GET", path, cookie="")[1]["status"], status)

    def test_invalid_unknown_tracking_token(self):
        for token in ("", "x", "x" * 22, "x" * 23, "!" * 22, "%00", "../server.py"):
            self.error(self.req("GET", "/api/track/" + token), 404)

    def test_unauthenticated_seller_routes_closed(self):
        order = self.create()
        self.error(self.req("GET", "/api/orders", cookie=""), 401)
        for oid in (order["order_id"], 99999):
            self.error(self.req("POST", "/api/orders/%s/status" % oid, {"status": "sobiraem"}, cookie=""), 401)

    def test_login_cookie_attributes_and_private_responses(self):
        result = self.login()
        self.assertNotIn("pass_hash", result)
        header = self.last_headers["Set-Cookie"]
        for flag in ("sid=", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=43200"):
            self.assertIn(flag, header)
        order = self.create()
        rows = self.orders()
        self.assertNotIn(order["token"], json.dumps(rows))
        for key in ("pass_hash", "salt", "request_hash", "token", "sid"):
            self.assertNotIn(key, rows[0])

    def test_wrong_password_and_unknown_login_same_401(self):
        known = self.req("POST", "/api/login", {"login": "florist", "password": "wrong"})
        unknown = self.req("POST", "/api/login", {"login": "missing", "password": "wrong"})
        self.error(known, 401)
        self.assertEqual(known, unknown)

    def test_login_input_boundaries_and_dummy_hash(self):
        for login, password in (("", ""), ("   ", "   "), ("🌷", "ёж"), ("missing", "1,5")):
            self.error(self.req("POST", "/api/login", {"login": login, "password": password}), 401)
        for data in ({}, {"login": 0, "password": 1.5}, {"login": "x" * 10000, "password": "x"}, {"login": "florist", "password": "x" * 10000}):
            self.error(self.req("POST", "/api/login", data), 400)
        original = server.hashlib.pbkdf2_hmac
        with patch.object(server.hashlib, "pbkdf2_hmac", wraps=original) as derive:
            self.error(self.req("POST", "/api/login", {"login": "missing", "password": "wrong"}), 401)
            self.assertEqual(derive.call_count, 1)
            self.assertEqual(derive.call_args.args[3], 600000)

    def test_expired_and_forged_sessions_rejected(self):
        self.login()
        self.error(self.req("GET", "/api/orders", cookie="sid=" + "x" * 43), 401)
        with closing(server.connect(self.db)) as con, con:
            con.execute("UPDATE sessions SET expires_at=0")
        self.error(self.req("GET", "/api/orders"), 401)

    def test_cli_add_user_and_no_default_users(self):
        db = self.root / "accounts.sqlite3"
        server.initialize(db)
        with closing(server.connect(db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 0)
        with patch("builtins.input", return_value="shopkeeper"), patch.object(getpass, "getpass", side_effect=[self.password, self.password]), patch("sys.stdout", new_callable=io.StringIO) as output:
            server.main(["--db", str(db), "--add-user"])
            self.assertNotIn(self.password, output.getvalue())
        with closing(server.connect(db)) as con:
            row = con.execute("SELECT * FROM users").fetchone()
            self.assertEqual(row["login"], "shopkeeper")
            self.assertEqual(len(row["salt"]), 16)
            self.assertEqual(row["pass_hash"], hashlib.pbkdf2_hmac("sha256", self.password.encode(), row["salt"], 600000))
        self.assertNotIn(self.password.encode(), db.read_bytes())

    def test_cli_rejects_short_or_mismatching_password(self):
        for passwords in (("short", "short"), (self.password, self.password + "x")):
            with patch("builtins.input", return_value="another"), patch.object(getpass, "getpass", side_effect=passwords), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    server.main(["--db", str(self.db), "--add-user"])

    def test_cli_login_validation_and_duplicate_user(self):
        for login in ("", "   ", "x" * 10000, "🌷", "ёж", "' OR 1=1 --", "florist"):
            with patch("builtins.input", return_value=login), patch.object(getpass, "getpass", return_value=self.password), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    server.main(["--db", str(self.db), "--add-user"])
        with closing(server.connect(self.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_unknown_order_and_bad_status(self):
        self.login()
        self.error(self.change(99999, "sobiraem"), 404)
        order = self.create()
        for status in ("oops", "", None, []):
            self.error(self.change(order["order_id"], status), 400, "status")
        self.error(self.change(order["order_id"], "dostavlen"), 400, "status")

    def test_cancel_returns_slot_once_and_status_repeat_idempotent(self):
        order = self.create()
        self.assertEqual(self.change(order["order_id"], "otmenen")[0], 200)
        self.assertEqual(self.change(order["order_id"], "otmenen")[0], 200)
        self.assertEqual(self.req("GET", "/api/slots?date=" + self.day)[1][0]["free"], 3)
        self.error(self.change(order["order_id"], "new"), 400, "status")

    def test_input_code_is_data_not_sql_or_execution(self):
        order = self.create(name="<script>alert(1)</script>", address="../../ ' OR 1=1 --", card_text='<img src=x onerror="alert(1)">')
        rows = self.orders()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "<script>alert(1)</script>")
        self.assertEqual(rows[0]["id"], order["order_id"])
        self.error(self.req("POST", "/api/login", {"login": "' OR 1=1 --", "password": "x"}), 401)

    def test_errors_do_not_echo_secrets_and_access_log_redacts(self):
        self.http.quiet = False
        with patch("sys.stderr", new_callable=io.StringIO) as log:
            order = self.create()
            self.login()
            self.req("GET", "/api/track/" + order["token"])
            self.req("GET", "/api/orders?password=" + self.password)
            text = log.getvalue()
            self.assertNotIn(self.password, text)
            self.assertNotIn(order["token"], text)
            self.assertNotIn(self.cookie.split("=", 1)[1], text)

    def test_bad_json_huge_body_and_server_survival(self):
        for path in ("/api/orders", "/api/login"):
            for raw in (b"{", b"", b"[]", b"null", b"\xff", b'{"x":NaN}', b'{"x":"\\ud800"}'):
                self.error(self.req("POST", path, raw=raw), 400)
            self.error(self.req("POST", path, raw=b" " * 65537), 413)
        self.assertEqual(self.req("GET", "/api/bouquets")[0], 200)

    def test_unknown_paths_methods_and_static_traversal(self):
        for path in ("/api/no", "/static/../server.py", "/static/%2e%2e/server.py", "/static/..%5cserver.py", "/static/%00"):
            self.error(self.req("GET", path), 404)
        self.error(self.req("TRACE", "/api/bouquets"), 405)
        self.assertEqual(self.req("GET", "/api/bouquets")[0], 200)

    def test_json_content_type_required_on_all_posts(self):
        for path in ("/api/orders", "/api/login", "/api/orders/1/status"):
            for headers in ({}, {"Content-Type": "text/plain"}, {"Content-Type": "application/x-www-form-urlencoded"}):
                self.error(self.req("POST", path, {}, headers=headers), 415)

    def test_static_fixture_and_cli_startup(self):
        (self.static / "index.html").write_text("Цветочный чат", encoding="utf-8")
        self.assertEqual(self.req("GET", "/"), (200, "Цветочный чат".encode("utf-8")))
        db = self.root / "cli.sqlite3"
        process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-u", str(ROOT / "server.py"), "--port", "0", "--db", str(db)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
        try:
            lines = []
            reader = threading.Thread(target=lambda: lines.append(process.stdout.readline()), daemon=True)
            reader.start()
            reader.join(3)
            self.assertFalse(reader.is_alive())
            self.assertIn("http://127.0.0.1:", lines[0])
            port = int(lines[0].split(":")[-1].strip())
            con = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            try:
                con.request("GET", "/api/bouquets")
                response = con.getresponse()
                self.assertEqual(response.status, 200)
                self.assertGreaterEqual(len(json.loads(response.read())), 6)
            finally:
                con.close()
        finally:
            process.terminate()  # Only this test-owned child process.
            process.communicate(timeout=3)

    def test_incomplete_body_does_not_hold_worker_forever(self):
        self.assertEqual(server.Handler.timeout, 10)
        with patch.object(server.Handler, "timeout", 0.1):
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as client:
                client.sendall(b"POST /api/orders HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n1234567890")
                self.assertEqual(client.recv(1024), b"")

    def test_restart_preserves_confirmation_and_tracking(self):
        first = self.create()
        self.stop()
        self.http = server.make_server(port=0, db=self.db, static_root=self.static, quiet=True)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.port = self.http.server_port
        self.assertEqual(self.create(), first)
        self.assertEqual(self.req("GET", "/api/track/" + first["token"])[0], 200)


if __name__ == "__main__":
    unittest.main()
