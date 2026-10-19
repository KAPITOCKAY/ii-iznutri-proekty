"""Contract-first HTTP tests, standard library only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import http.client
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
server = None
if (ROOT / 'server.py').exists():
    spec = importlib.util.spec_from_file_location('sneaker_server', ROOT / 'server.py')
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)


class SneakerTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(server, 'Красная фаза: server.py ещё отсутствует')
        self.temp = tempfile.TemporaryDirectory(prefix='test_', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'test.sqlite3'
        self.http = server.make_server(port=0, db=self.db, quiet=True)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.http.server_port
        self.catalog = self.req('GET', '/api/katalog')[1]
        self.pid = self.catalog[0]['id']

    def stop(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())

    def req(self, method, path, data=None, raw=None, headers=None):
        body = raw if raw is not None else json.dumps(data, ensure_ascii=False).encode('utf-8') if data is not None else None
        con = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        try:
            con.request(method, path, body, headers if headers is not None else {'Content-Type': 'application/json'})
            r = con.getresponse()
            payload = r.read()
            self.assertIn('application/json', r.getheader('Content-Type', ''))
            self.assertIn('utf-8', r.getheader('Content-Type', '').lower())
            return r.status, json.loads(payload) if method != 'HEAD' else payload
        finally:
            con.close()

    def payload(self, **changes):
        data = {'items': [{'id': self.pid, 'size': 42, 'qty': 1}], 'skidka_pct': 0,
                'name': 'Иван', 'phone': '+7 (999) 123-45-67'}
        data.update(changes)
        return data

    def create(self, **changes):
        code, body = self.req('POST', '/api/zakaz', self.payload(**changes))
        self.assertEqual(code, 201, body)
        self.assertEqual(set(body), {'order_id', 'total', 'skidka_pct', 'status'})
        self.assertEqual(body['status'], 'created')
        return body

    def lookup(self, oid, phone='+7 (999) 123-45-67'):
        return self.req('GET', '/api/zakaz/%s?' % oid + urlencode({'phone': phone}))

    def stock(self, pid=None, size=42):
        pid = self.pid if pid is None else pid
        return next(p for p in self.req('GET', '/api/katalog')[1] if p['id'] == pid)['sizes'][str(size)]

    def price(self, pid=None):
        return next(p['price'] for p in self.catalog if p['id'] == (self.pid if pid is None else pid))

    def error(self, response, status, error):
        self.assertEqual(response, (status, {'error': error}))

    def test_catalog_contract_six_models_all_sizes(self):
        self.assertEqual(self.http.server_address[0], '127.0.0.1')
        self.assertEqual(len(self.catalog), 6)
        for model in self.catalog:
            self.assertEqual(set(model), {'id', 'name', 'price', 'sizes'})
            self.assertTrue(6000 <= model['price'] <= 18000)
            self.assertIs(type(model['price']), int)
            self.assertEqual(set(model['sizes']), {str(n) for n in range(39, 46)})
            self.assertTrue(all(type(q) is int and q >= 0 for q in model['sizes'].values()))

    def test_seed_does_not_reset_sold_inventory(self):
        before = self.stock()
        self.create()
        server.initialize(self.db)
        self.assertEqual(self.stock(), before - 1)
        self.assertEqual(len(self.req('GET', '/api/katalog')[1]), 6)

    def test_order_and_stock_by_size_only(self):
        before, other = self.stock(), self.stock(size=41)
        result = self.create(total=1, price=1, status='paid')
        self.assertEqual(result['total'], self.price())
        self.assertEqual(result['skidka_pct'], 0)
        self.assertEqual(self.stock(), before - 1)
        self.assertEqual(self.stock(size=41), other)

    def test_missing_fields_have_contract_errors(self):
        for field, code in (('items', 'no_item'), ('skidka_pct', 'bad_skidka'), ('name', 'bad_name'), ('phone', 'bad_phone')):
            data = self.payload()
            del data[field]
            self.error(self.req('POST', '/api/zakaz', data), 400, code)

    def test_no_item_invalid_id_and_items_structure(self):
        for items in ([], None, {}, 'x', [None], [{}], [{'id': 999999, 'size': 42, 'qty': 1}],
                      *([{'id': value, 'size': 42, 'qty': 1}] for value in (None, True, 0, -1, 1.5, '1,5', 'abc', 10**100))):
            self.error(self.req('POST', '/api/zakaz', self.payload(items=items)), 400, 'no_item')

    def test_size_string_or_integer_and_invalid_sizes(self):
        self.create(items=[{'id': self.pid, 'size': '42', 'qty': 1}])
        for size in (None, True, 0, -1, 38, 46, '42.0', '1,5', 42.5, 'x'*10000, '🌷', 10**100):
            self.error(self.req('POST', '/api/zakaz', self.payload(items=[{'id': self.pid, 'size': size, 'qty': 1}])), 400, 'no_size')

    def test_bad_quantity_types_and_bounds(self):
        for qty in (None, True, 0, -1, 1.5, '1,5', '1.5', 'abc', '', 10**100):
            self.error(self.req('POST', '/api/zakaz', self.payload(items=[{'id': self.pid, 'size': 42, 'qty': qty}])), 400, 'bad_qty')

    def test_name_unicode_and_invalid_name(self):
        for name in ('', '   ', None, 0, 'x'*10000):
            self.error(self.req('POST', '/api/zakaz', self.payload(name=name)), 400, 'bad_name')
        self.create(name='Алёна 👟')
        self.create(name='я'*100)

    def test_phone_validation_and_russian_normalization(self):
        for phone in ('', ' ', None, 0, 'x'*10000, '++++++++++1', '1'*16, '🌷'):
            self.error(self.req('POST', '/api/zakaz', self.payload(phone=phone)), 400, 'bad_phone')
        order = self.create()
        for phone in ('+7 999 123-45-67', '8 999 123-45-67', '9991234567'):
            self.assertEqual(self.lookup(order['order_id'], phone)[0], 200)

    def test_invalid_discount(self):
        for value in (None, True, -1, -0.1, 100.001, 101, 10**100, '', ' ', '1,5', '1.5', 'abc', [], {}):
            self.error(self.req('POST', '/api/zakaz', self.payload(skidka_pct=value)), 400, 'bad_skidka')

    def test_server_enforces_fifteen_percent_limit_without_stock_changes(self):
        before = self.stock()
        for value in (15.0001, 15.01, 16, 50, 100):
            with self.subTest(discount=value):
                self.error(self.req('POST', '/api/zakaz', self.payload(skidka_pct=value)), 400, 'bad_skidka')
                self.assertEqual(self.stock(), before)
        with closing(server.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)
        for value in (0, 14.999, 15):
            order = self.create(name='Граница %s' % value, skidka_pct=value)
            expected = int((Decimal(self.price()) * (100 - Decimal(str(value))) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            self.assertEqual((order['skidka_pct'], order['total']), (value, expected))

    def test_discount_limit_checked_before_replaying_old_free_order(self):
        data = self.payload(skidka_pct=100)
        canonical = {'items': data['items'], 'skidka_pct': str(Decimal(100).normalize()),
                     'name': data['name'], 'phone': server.phone_number(data['phone'])}
        fingerprint = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
        with closing(server.connect(self.db)) as con, con:
            con.execute("INSERT INTO orders(name,phone,skidka_pct,total,status,created_at,request_hash) VALUES (?,?,?,0,'created',?,?)",
                        (data['name'], canonical['phone'], '100', server.utc_now().isoformat(timespec='microseconds'), fingerprint))
        before = self.stock()
        self.error(self.req('POST', '/api/zakaz', data), 400, 'bad_skidka')
        self.assertEqual(self.stock(), before)

    def test_direct_request_for_expensive_model_cannot_override_policy(self):
        model = self.catalog[-1]
        size = 43
        before = self.stock(model['id'], size)
        data = self.payload(items=[{'id': model['id'], 'size': str(size), 'qty': 1}],
                            skidka_pct=100, name='Хакер', phone='+79990001122',
                            max_discount=100, total=0, price=0)
        self.error(self.req('POST', '/api/zakaz', data, headers={'Content-Type': 'application/x-www-form-urlencoded'}), 400, 'bad_skidka')
        self.assertEqual(self.stock(model['id'], size), before)
        order = self.create(items=[{'id': model['id'], 'size': str(size), 'qty': 1}], skidka_pct=15)
        expected = int((Decimal(model['price']) * Decimal('.85')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        self.assertEqual(order['total'], expected)
        self.assertGreater(order['total'], 0)

    def test_zero_and_fifteen_percent_discounts(self):
        first = self.create(skidka_pct=0)
        self.assertEqual(first['total'], self.price())
        before = self.stock()
        maximum = self.create(name='Максимальная скидка', skidka_pct=15)
        expected = int((Decimal(self.price()) * Decimal('.85')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        self.assertEqual((maximum['total'], maximum['skidka_pct']), (expected, 15))
        self.assertEqual(self.stock(), before - 1)

    def test_fractional_discount_and_integer_ruble_rounding(self):
        for index, discount in enumerate((1.5, 12.345, 14.3333)):
            order = self.create(name='Клиент %s' % index, skidka_pct=discount)
            expected = int((Decimal(self.price()) * (100 - Decimal(str(discount))) / 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
            self.assertEqual(order['total'], expected)
            self.assertIs(type(order['total']), int)
            self.assertEqual(order['skidka_pct'], discount)

    def test_half_ruble_rounds_up_not_to_even(self):
        with closing(server.connect(self.db)) as con, con:
            con.execute('UPDATE products SET price=8005 WHERE id=?', (self.pid,))
        self.assertEqual(self.create(skidka_pct=10)['total'], 7205)

    def test_multiple_items_discount_applies_to_whole_subtotal(self):
        second = self.catalog[1]
        order = self.create(items=[{'id': self.pid, 'size': 42, 'qty': 2}, {'id': second['id'], 'size': 41, 'qty': 1}], skidka_pct=7.5)
        subtotal = 2*self.price() + second['price']
        expected = int((Decimal(subtotal)*Decimal('.925')).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        self.assertEqual(order['total'], expected)
        code, detail = self.lookup(order['order_id'])
        self.assertEqual(code, 200)
        self.assertEqual(sum(line['sum'] for line in detail['items']), subtotal)
        self.assertEqual(detail['total'], expected)

    def test_insufficient_stock_rolls_back_every_line(self):
        first_before = self.stock()
        second = self.catalog[1]
        data = self.payload(items=[{'id': self.pid, 'size': 42, 'qty': 1}, {'id': second['id'], 'size': 42, 'qty': second['sizes']['42']+1}])
        self.error(self.req('POST', '/api/zakaz', data), 400, 'out_of_stock')
        self.assertEqual(self.stock(), first_before)
        self.assertEqual(self.stock(second['id']), second['sizes']['42'])
        with closing(server.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 0)

    def test_duplicate_lines_aggregate_before_stock_check(self):
        stock = self.stock()
        self.error(self.req('POST', '/api/zakaz', self.payload(items=[{'id': self.pid, 'size': 42, 'qty': stock}, {'id': self.pid, 'size': '42', 'qty': 1}])), 400, 'out_of_stock')
        self.assertEqual(self.stock(), stock)
        order = self.create(items=[{'id': self.pid, 'size': 42, 'qty': 1}, {'id': self.pid, 'size': '42', 'qty': 1}])
        self.assertEqual(order['total'], self.price()*2)
        self.assertEqual(len(self.lookup(order['order_id'])[1]['items']), 1)

    def test_sequential_repeat_does_not_reserve_twice(self):
        before = self.stock()
        first = self.create()
        self.assertEqual(self.create(), first)
        self.assertEqual(self.stock(), before - 1)

    def test_normalized_cart_phone_and_discount_deduplicate(self):
        first = self.create(items=[{'id': self.pid, 'size': 42, 'qty': 2}], skidka_pct=10)
        self.assertEqual(self.create(items=[{'id': self.pid, 'size': '42', 'qty': 1}]*2, phone='89991234567', skidka_pct=10.0), first)

    def test_zero_float_and_negative_zero_are_same_discount(self):
        before = self.stock()
        first = self.create(skidka_pct=0)
        self.assertEqual(self.create(skidka_pct=0.0), first)
        self.assertEqual(self.create(skidka_pct=-0.0), first)
        self.assertEqual(self.stock(), before - 1)

    def test_repeat_of_last_pair_keeps_snapshot_even_after_price_change(self):
        size = 45
        qty = self.stock(size=size)
        first = self.create(items=[{'id': self.pid, 'size': size, 'qty': qty}])
        self.assertEqual(self.stock(size=size), 0)
        with closing(server.connect(self.db)) as con, con:
            con.execute('UPDATE products SET price=18000 WHERE id=?', (self.pid,))
        self.assertEqual(self.create(items=[{'id': self.pid, 'size': size, 'qty': qty}]), first)
        detail = self.lookup(first['order_id'])[1]
        self.assertEqual(detail['items'][0]['price'], self.price())
        self.assertEqual(self.stock(size=size), 0)

    def test_foreign_phone_with_country_prefix_not_rewritten(self):
        for phone in ('+81 90 123 4567', '+44 20 712345'):
            order = self.create(name='Иностранный ' + phone, phone=phone)
            self.assertEqual(self.lookup(order['order_id'], phone)[0], 200)
            self.error(self.lookup(order['order_id']), 404, 'not_found')

    def test_parallel_identical_orders_return_same_order(self):
        before = self.stock()
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda _: self.req('POST', '/api/zakaz', self.payload()), range(2)))
        self.assertEqual([code for code, _ in result], [201, 201])
        self.assertEqual(result[0][1], result[1][1])
        self.assertEqual(self.stock(), before - 1)

    def test_two_distinct_buyers_race_for_last_pair(self):
        size = 45
        initial = self.stock(size=size)
        if initial > 1:
            self.create(name='Подготовка', items=[{'id': self.pid, 'size': size, 'qty': initial - 1}])
        self.assertEqual(self.stock(size=size), 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(lambda name: self.req('POST', '/api/zakaz', self.payload(name=name, items=[{'id': self.pid, 'size': size, 'qty': 1}])), ('А', 'Б')))
        self.assertEqual(sorted(code for code, _ in result), [201, 400])
        self.assertEqual(next(body for code, body in result if code == 400), {'error': 'out_of_stock'})
        self.assertEqual(self.stock(size=size), 0)

    def test_lost_reply_after_commit_does_not_duplicate(self):
        done = threading.Event()
        original = server.Handler.reply
        def lost(handler, code, payload):
            if code == 201:
                done.set()
                raise BrokenPipeError('simulated lost reply')
            return original(handler, code, payload)
        with patch.object(server.Handler, 'reply', lost):
            with socket.create_connection(('127.0.0.1', self.port), timeout=3) as client:
                body = json.dumps(self.payload()).encode()
                client.sendall(b'POST /api/zakaz HTTP/1.1\r\nHost: localhost\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
                self.assertEqual(client.recv(1024), b'')
                self.assertTrue(done.wait(2))
        self.create()
        with closing(server.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)

    def test_lookup_requires_matching_phone(self):
        order = self.create()
        for phone in ('', 'abc', '89991234568', "' OR 1=1 --"):
            self.error(self.lookup(order['order_id'], phone), 404, 'not_found')
        self.error(self.req('GET', '/api/zakaz/%s' % order['order_id']), 404, 'not_found')
        self.error(self.lookup(999999), 404, 'not_found')
        self.error(self.lookup('9'*100), 404, 'not_found')

    def test_order_detail_has_no_customer_phone_or_internal_fields(self):
        order = self.create()
        code, body = self.lookup(order['order_id'])
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {'order_id', 'total', 'skidka_pct', 'status', 'items'})
        for key in ('phone', 'name', 'request_hash', 'created_at'):
            self.assertNotIn(key, body)
        for item in body['items']:
            self.assertEqual(set(item), {'id', 'name', 'size', 'qty', 'price', 'sum'})

    def test_sql_and_script_injections_are_not_executed(self):
        order = self.create(name="<script>alert(1)</script> ' OR 1=1 --")
        self.assertEqual(self.lookup(order['order_id'])[0], 200)
        self.error(self.req('POST', '/api/zakaz', self.payload(items=[{'id': "' OR 1=1 --", 'size': 42, 'qty': 1}])), 400, 'no_item')
        for path in ('/../server.py', '/static/%2e%2e/server.py', '/static/..%5cserver.py'):
            self.error(self.req('GET', path), 404, 'not_found')

    def test_invalid_json_huge_numbers_surrogates(self):
        for raw in (b'{', b'', b'[]', b'null', b'\xff', b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\\ud800"}'):
            self.error(self.req('POST', '/api/zakaz', raw=raw), 400, 'invalid_json')
        self.assertEqual(self.req('GET', '/api/katalog')[0], 200)

    def test_body_limit_and_bad_framing(self):
        self.error(self.req('POST', '/api/zakaz', raw=b' '*65537), 413, 'invalid_json')
        for value in ('-1', 'abc'):
            self.error(self.req('POST', '/api/zakaz', raw=b'', headers={'Content-Length': value}), 400, 'invalid_json')

    def test_exact_body_limit_is_accepted(self):
        body = json.dumps(self.payload()).encode()
        self.assertEqual(self.req('POST', '/api/zakaz', raw=body + b' '*(65536 - len(body)))[0], 201)

    def test_unknown_method_and_path_are_json(self):
        self.error(self.req('TRACE', '/api/katalog'), 405, 'not_found')
        self.error(self.req('GET', '/api/missing'), 404, 'not_found')
        self.assertEqual(self.req('GET', '/api/katalog')[0], 200)

    def test_socket_read_timeout(self):
        self.assertEqual(server.Handler.timeout, 10)
        with patch.object(server.Handler, 'timeout', 0.1):
            with socket.create_connection(('127.0.0.1', self.port), timeout=2) as client:
                client.sendall(b'POST /api/zakaz HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n1234567890')
                self.assertEqual(client.recv(1024), b'')

    def test_logs_do_not_record_phone_or_payload(self):
        self.http.quiet = False
        with patch('sys.stderr', new_callable=io.StringIO) as err, patch('sys.stdout', new_callable=io.StringIO) as out:
            order = self.create()
            self.lookup(order['order_id'])
        self.assertNotIn('79991234567', err.getvalue() + out.getvalue())
        self.assertNotIn('Иван', err.getvalue() + out.getvalue())

    def test_restart_preserves_stock_and_dedup(self):
        order = self.create()
        stock = self.stock()
        self.stop()
        self.http = server.make_server(port=0, db=self.db, quiet=True)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.port = self.http.server_port
        self.assertEqual(self.create(), order)
        self.assertEqual(self.stock(), stock)

    def test_expired_dedup_checks_current_stock_again(self):
        start = datetime(2030, 1, 1, 8, tzinfo=timezone.utc)
        with patch.object(server, 'utc_now', return_value=start):
            first = self.create()
        with patch.object(server, 'utc_now', return_value=start + timedelta(minutes=9)):
            self.assertEqual(self.create(), first)
        with patch.object(server, 'utc_now', return_value=start + timedelta(minutes=10)):
            self.assertNotEqual(self.create()['order_id'], first['order_id'])

    def test_cli_startup(self):
        db = self.root / 'cli.sqlite3'
        process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', '-u', str(ROOT / 'server.py'), '--port', '0', '--db', str(db)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        try:
            lines = []
            reader = threading.Thread(target=lambda: lines.append(process.stdout.readline()), daemon=True)
            reader.start()
            reader.join(3)
            self.assertFalse(reader.is_alive())
            self.assertIn('http://127.0.0.1:', lines[0])
            port = int(lines[0].split(':')[-1].strip())
            con = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
            try:
                con.request('GET', '/api/katalog')
                r = con.getresponse()
                self.assertEqual(r.status, 200)
                self.assertEqual(len(json.loads(r.read())), 6)
            finally:
                con.close()
        finally:
            process.terminate()
            process.communicate(timeout=3)


if __name__ == '__main__':
    unittest.main()
