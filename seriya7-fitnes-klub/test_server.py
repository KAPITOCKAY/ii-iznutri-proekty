"""Contract-first tests; test PANs below are public simulator vectors, not users' cards."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import http.client
import io
import json
import os
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
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
server = oplata = None
if (ROOT / 'server.py').exists() and (ROOT / 'oplata.py').exists():
    import server
    import oplata


class FitnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_key = secrets.token_urlsafe(32)

    def setUp(self):
        self.assertIsNotNone(server, 'Красная фаза: server.py и oplata.py ещё не написаны')
        env = patch.dict(os.environ, {'FITNES_SECRET_KEY': self.test_key, 'FITNES_SHOP_ID': '104577'})
        env.start()
        self.addCleanup(env.stop)
        expected = patch.object(oplata, 'EXPECTED_SECRET_SHA256', hashlib.sha256(self.test_key.encode()).hexdigest())
        expected.start()
        self.addCleanup(expected.stop)
        clock = patch.object(server, 'today', return_value=date(2030, 1, 15))
        clock.start()
        self.addCleanup(clock.stop)
        self.temp = tempfile.TemporaryDirectory(prefix='test_', dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'test.sqlite3'
        self.http = server.make_server(port=0, db=self.db, quiet=True)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.addCleanup(self.stop)
        self.port = self.http.server_port

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
            self.last_headers = dict(r.getheaders())
            payload = r.read()
            self.assertIn('application/json', r.getheader('Content-Type', ''))
            self.assertIn('utf-8', r.getheader('Content-Type', '').lower())
            return r.status, json.loads(payload) if method != 'HEAD' else payload
        finally:
            con.close()

    def payload(self, **changes):
        data = {'tarif_id': 1, 'name': 'Иван', 'phone': '+7 (999) 123-45-67', 'email': 'ivan@example.com',
                'card': {'number': '4242 4242 4242 4242', 'exp': '12/40', 'cvc': '123'}}
        data.update(changes)
        return data

    def buy(self, **changes):
        code, body = self.req('POST', '/api/pokupka', self.payload(**changes))
        self.assertEqual(code, 201, body)
        self.assertEqual(set(body), {'order_id', 'status', 'total', 'until'})
        return body

    def lookup(self, oid, phone='+7 (999) 123-45-67'):
        return self.req('GET', '/api/zakaz/%s?' % oid + urlencode({'phone': phone}))

    def error(self, response, status, error):
        self.assertEqual(response, (status, {'error': error}))

    def test_tarify_exact_contract_and_loopback(self):
        self.assertEqual(self.http.server_address[0], '127.0.0.1')
        self.assertEqual(self.req('GET', '/api/tarify'), (200, [
            {'id': 1, 'name': 'Месяц', 'price': 3000, 'days': 30},
            {'id': 2, 'name': '3 месяца', 'price': 7500, 'days': 90},
            {'id': 3, 'name': 'Год', 'price': 24000, 'days': 365}]))
        server.initialize(self.db)
        self.assertEqual(len(self.req('GET', '/api/tarify')[1]), 3)

    def test_paid_and_declined_cards_have_expected_status(self):
        paid = self.buy()
        declined = self.buy(card={'number': '4000 0000 0000 0002', 'exp': '12/40', 'cvc': '123'})
        self.assertEqual(paid['status'], 'paid')
        self.assertEqual(declined['status'], 'declined')
        self.assertNotEqual(paid['order_id'], declined['order_id'])
        self.assertEqual(paid['total'], 3000)
        self.assertEqual(paid['until'], '2030-02-14')
        self.assertEqual(declined['until'], '2030-02-14')

    def test_each_tariff_amount_and_duration(self):
        with patch.object(oplata, 'charge', wraps=oplata.charge) as charge:
            for pid, amount, days in ((1, 3000, 30), (2, 7500, 90), (3, 24000, 365)):
                order = self.buy(tarif_id=pid, total=1, price=1, until='9999-12-31', status='paid')
                self.assertEqual(order['total'], amount)
                self.assertIs(type(order['total']), int)
                self.assertEqual(order['until'], (date(2030, 1, 15) + timedelta(days=days)).isoformat())
                self.assertEqual(charge.call_args.args[0], amount)

    def test_unknown_and_bad_tariff_id(self):
        for value in (None, 0, -1, True, 1.5, '1,5', '1.5', 'abc', '1', 10**100, 999):
            self.error(self.req('POST', '/api/pokupka', self.payload(tarif_id=value)), 400, 'no_tarif')

    def test_phone_validation(self):
        for phone in ('', '  ', None, 123, '1'*10000, '++++++++++1', '1'*9, '1'*16, '123456789x', 'ёж', '🌷'):
            self.error(self.req('POST', '/api/pokupka', self.payload(phone=phone)), 400, 'bad_phone')

    def test_email_validation(self):
        for email in ('', '  ', None, 123, 'x'*10000, 'a@', '@example.com', 'a@example', 'a b@example.com', 'a\n@example.com', '🌷@example.com'):
            self.error(self.req('POST', '/api/pokupka', self.payload(email=email)), 400, 'bad_email')

    def test_name_boundaries_and_unicode(self):
        for name in ('', ' ', 'x'*10000, None, 1):
            self.error(self.req('POST', '/api/pokupka', self.payload(name=name)), 400, 'invalid_json')
        self.assertEqual(self.buy(name='Алёна 💪')['status'], 'paid')
        self.assertEqual(self.buy(name='Я'*100, email='second@example.com')['status'], 'paid')

    def test_card_structure_and_number_validation(self):
        for card in (None, [], 'x', {}, {'number': '4242424242424242'},
                     *({'number': number, 'exp': '12/40', 'cvc': '123'} for number in ('', ' ', 'x'*10000, '4242424242424241', '0000000000000000', '1', -1, 0, 1.5, None, True, '🌷', 'Ё'*16))):
            self.error(self.req('POST', '/api/pokupka', self.payload(card=card)), 400, 'bad_card')

    def test_cvc_not_validated_as_number(self):
        for value in ('', ' ', None, 123, '12', '12345', 'abc', '🌷', 'x'*10000):
            self.error(self.req('POST', '/api/pokupka', self.payload(card={'number': '4242424242424242', 'exp': '12/40', 'cvc': value})), 400, 'bad_card')

    def test_expiry_validation(self):
        for exp in ('', ' ', None, 0, 1.5, 'x'*10000, '31.02', '13/40', '00/40', '12/29', '1/40', 'ёж', '🌷'):
            self.error(self.req('POST', '/api/pokupka', self.payload(card={'number': '4242424242424242', 'exp': exp, 'cvc': '123'})), 400, 'bad_exp')

    def test_card_current_month_valid_and_midnight_rollover(self):
        with patch.object(server, 'today', return_value=date(2030, 1, 31)):
            order = self.buy(card={'number': '4242424242424242', 'exp': '01/30', 'cvc': '123'})
            self.assertEqual(order['until'], '2030-03-02')
        with patch.object(server, 'today', return_value=date(2030, 2, 1)):
            self.error(self.req('POST', '/api/pokupka', self.payload(card={'number': '4242424242424242', 'exp': '01/30', 'cvc': '123'})), 400, 'bad_exp')

    def test_leap_year_duration_is_days_not_calendar_year(self):
        with patch.object(server, 'today', return_value=date(2032, 2, 29)):
            self.assertEqual(self.buy(tarif_id=3)['until'], '2033-02-28')

    def test_missing_fields(self):
        codes = {'tarif_id': 'no_tarif', 'name': 'invalid_json', 'phone': 'bad_phone', 'email': 'bad_email', 'card': 'bad_card'}
        for key, error in codes.items():
            data = self.payload()
            del data[key]
            self.error(self.req('POST', '/api/pokupka', data), 400, error)

    def test_repeated_payment_is_one_order_and_one_charge(self):
        with patch.object(oplata, 'charge', wraps=oplata.charge) as charge:
            first, second = self.buy(), self.buy()
            self.assertEqual(first, second)
            self.assertEqual(charge.call_count, 1)
        with closing(server.connect(self.db)) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM orders').fetchone()[0], 1)

    def test_dedup_ten_minute_boundary(self):
        fixed = datetime(2030, 1, 15, 8, tzinfo=timezone.utc)
        with patch.object(server, 'utc_now', return_value=fixed):
            first = self.buy()
        with patch.object(server, 'utc_now', return_value=fixed + timedelta(minutes=10, microseconds=-1)):
            self.assertEqual(self.buy(), first)
        with patch.object(server, 'utc_now', return_value=fixed + timedelta(minutes=10)):
            self.assertNotEqual(self.buy()['order_id'], first['order_id'])

    def test_cvc_is_not_part_of_persistent_fingerprint(self):
        with patch.object(oplata, 'charge', wraps=oplata.charge) as charge:
            first = self.buy()
            self.assertEqual(self.buy(card={'number': '4242424242424242', 'exp': '12/40', 'cvc': '987'}), first)
            self.assertEqual(charge.call_count, 1)

    def test_failed_validation_never_calls_gateway(self):
        with patch.object(oplata, 'charge', wraps=oplata.charge) as charge:
            self.error(self.req('POST', '/api/pokupka', self.payload(phone='abc')), 400, 'bad_phone')
            self.error(self.req('POST', '/api/pokupka', self.payload(tarif_id=999)), 400, 'no_tarif')
            charge.assert_not_called()

    def test_declined_repeat_deduplicates_but_different_card_can_pay(self):
        changes = {'card': {'number': '4000000000000002', 'exp': '12/40', 'cvc': '123'}}
        first = self.buy(**changes)
        self.assertEqual(self.buy(**changes), first)
        second = self.buy()
        self.assertNotEqual(first['order_id'], second['order_id'])
        self.assertEqual(second['status'], 'paid')

    def test_parallel_confirmations_do_not_charge_twice(self):
        with patch.object(oplata, 'charge', wraps=oplata.charge) as charge:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: self.req('POST', '/api/pokupka', self.payload()), range(2)))
            self.assertEqual([code for code, _ in results], [201, 201])
            self.assertEqual(results[0][1], results[1][1])
            self.assertEqual(charge.call_count, 1)

    def test_response_lost_after_commit_does_not_repeat_charge(self):
        event = threading.Event()
        original = server.Handler.reply
        def lost(handler, status, payload):
            if status == 201:
                event.set()
                raise BrokenPipeError('test lost response')
            return original(handler, status, payload)
        with patch.object(oplata, 'charge', wraps=oplata.charge) as charge:
            with patch.object(server.Handler, 'reply', lost):
                with socket.create_connection(('127.0.0.1', self.port), timeout=3) as client:
                    body = json.dumps(self.payload()).encode()
                    client.sendall(b'POST /api/pokupka HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
                    self.assertEqual(client.recv(1024), b'')
                    self.assertTrue(event.wait(2))
            order = self.buy()
            self.assertEqual(charge.call_count, 1)
            self.assertEqual(self.lookup(order['order_id']), (200, order))

    def test_phone_required_for_lookup_and_no_enumeration(self):
        order = self.buy()
        self.assertEqual(self.lookup(order['order_id']), (200, order))
        self.assertEqual(self.lookup(order['order_id'], '79991234567'), (200, order))
        for phone in ('', 'abc', '89991234568', '1234567890', "' OR 1=1 --"):
            self.error(self.lookup(order['order_id'], phone), 404, 'not_found')
        self.error(self.req('GET', '/api/zakaz/%s' % order['order_id']), 404, 'not_found')
        self.error(self.lookup(999999), 404, 'not_found')
        self.error(self.lookup('9'*100), 404, 'not_found')

    def test_russian_phone_formats_match_at_purchase_and_lookup(self):
        formats = ('+7 999 123-45-67', '8 999 123-45-67', '9991234567')
        for index, purchase_phone in enumerate(formats):
            with self.subTest(purchase_phone=purchase_phone):
                order = self.buy(name='Клиент %s' % index, phone=purchase_phone)
                for search_phone in formats:
                    self.assertEqual(self.lookup(order['order_id'], search_phone), (200, order))
                self.error(self.lookup(order['order_id'], '8 999 123-45-68'), 404, 'not_found')

    def test_russian_phone_formats_deduplicate_same_confirmation(self):
        with patch.object(oplata, 'charge', wraps=oplata.charge) as charge:
            first = self.buy(phone='+7 999 123-45-67')
            self.assertEqual(self.buy(phone='8 999 123-45-67'), first)
            self.assertEqual(self.buy(phone='9991234567'), first)
            self.assertEqual(charge.call_count, 1)

    def test_explicit_foreign_country_codes_are_not_rewritten(self):
        for phone, digits in (('+81 90 1234 5678', '819012345678'),
                              ('+81 90 123 4567', '81901234567'),
                              ('+86 10 12345678', '861012345678'),
                              ('+44 20 712345', '4420712345'),
                              ('+1 202 555 0123', '12025550123')):
            with self.subTest(phone=phone):
                self.assertEqual(server.phone_digits(phone), digits)
                order = self.buy(name='Иностранный ' + digits, phone=phone)
                self.assertEqual(self.lookup(order['order_id'], phone), (200, order))
                self.error(self.lookup(order['order_id'], '+7 999 123-45-67'), 404, 'not_found')

    def test_response_exact_keys_no_personal_or_card_data(self):
        order = self.buy()
        _, found = self.lookup(order['order_id'])
        self.assertEqual(set(found), {'order_id', 'status', 'total', 'until'})
        text = json.dumps(found)
        for word in ('phone', 'email', 'name', 'card', 'number', 'cvc', 'pass', self.test_key):
            self.assertNotIn(word, text)

    def test_database_does_not_store_pan_cvc_or_expiry(self):
        self.buy()
        with closing(server.connect(self.db)) as con:
            columns = [r[1] for r in con.execute('PRAGMA table_info(orders)')]
            for column in ('card', 'number', 'pan', 'cvc', 'cvv', 'exp', 'card_number'):
                self.assertNotIn(column, columns)
            dump = '\n'.join(con.iterdump())
        for secret in ('4242424242424242', '4242 4242 4242 4242', "'123'", '12/40', self.test_key):
            self.assertNotIn(secret, dump)
            if len(secret) > 5:
                self.assertNotIn(secret.encode(), self.db.read_bytes())

    def test_logs_do_not_contain_card_cvc_key_or_phone(self):
        self.http.quiet = False
        with patch('sys.stderr', new_callable=io.StringIO) as log, patch('sys.stdout', new_callable=io.StringIO) as out:
            order = self.buy()
            self.lookup(order['order_id'])
        text = log.getvalue() + out.getvalue()
        for value in (self.test_key, '4242424242424242', '4242 4242 4242 4242', '123', '79991234567', 'ivan@example.com'):
            self.assertNotIn(value, text)

    def test_gateway_authenticates_shop_key_and_unknown_shop(self):
        card = self.payload()['card']
        with patch.dict(os.environ, {'FITNES_SECRET_KEY': 'wrong'}):
            with self.assertRaises(oplata.GatewayError):
                oplata.charge(3000, card)
        with patch.dict(os.environ, {'FITNES_SHOP_ID': '0'}):
            with self.assertRaises(oplata.GatewayError):
                oplata.charge(3000, card)
        self.assertEqual(oplata.charge(3000, card), 'paid')

    def test_server_refuses_startup_without_correct_key(self):
        with patch.dict(os.environ, {'FITNES_SECRET_KEY': ''}):
            with self.assertRaises(oplata.GatewayError):
                server.make_server(port=0, db=self.root / 'unconfigured.sqlite3')

    def test_cli_without_key_fails_cleanly_and_creates_no_database(self):
        db = self.root / 'missing-config.sqlite3'
        env = dict(os.environ, FITNES_SECRET_KEY='')
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(ROOT / 'server.py'), '--db', str(db), '--port', '0'],
                                capture_output=True, encoding='utf-8', env=env, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn(self.test_key, result.stderr)
        self.assertFalse(db.exists())

    def test_money_server_owned_and_stored_price_snapshot(self):
        first = self.buy(total=1, price=1, until='9999-12-31')
        self.assertEqual(first['total'], 3000)
        with closing(server.connect(self.db)) as con, con:
            con.execute('UPDATE tarify SET price=1 WHERE id=1')
        self.assertEqual(self.buy(), first)

    def test_key_order_whitespace_phone_do_not_bypass_dedup(self):
        first = self.buy()
        data = dict(reversed(list(self.payload(name=' Иван ', phone='79991234567').items())))
        data['card']['number'] = '4242424242424242'
        self.assertEqual(self.req('POST', '/api/pokupka', data), (201, first))

    def test_sql_and_html_injection_remain_data(self):
        order = self.buy(name="<script>alert(1)</script> ' OR 1=1 --")
        self.assertEqual(self.lookup(order['order_id'])[0], 200)
        for path in ('/../server.py', '/static/../server.py', '/static/%2e%2e/server.py', '/static/..%5cserver.py'):
            self.error(self.req('GET', path), 404, 'not_found')

    def test_invalid_json_and_surrogate(self):
        for raw in (b'{', b'', b'[]', b'null', b'\xff', b'{"x":NaN}', b'{"x":1e999}', b'{"x":"\\ud800"}'):
            self.error(self.req('POST', '/api/pokupka', raw=raw), 400, 'invalid_json')
        self.assertEqual(self.req('GET', '/api/tarify')[0], 200)

    def test_body_limit_and_bad_length(self):
        self.error(self.req('POST', '/api/pokupka', raw=b' ' * 65537), 413, 'invalid_json')
        for length in ('-1', 'x'):
            self.error(self.req('POST', '/api/pokupka', raw=b'', headers={'Content-Length': length}), 400, 'invalid_json')

    def test_socket_timeout_and_short_body(self):
        self.assertEqual(server.Handler.timeout, 10)
        with patch.object(server.Handler, 'timeout', 0.1):
            with socket.create_connection(('127.0.0.1', self.port), timeout=2) as client:
                client.sendall(b'POST /api/pokupka HTTP/1.1\r\nHost: localhost\r\nContent-Length: 100\r\n\r\n1234567890')
                self.assertEqual(client.recv(1024), b'')

    def test_unknown_path_and_method_are_json_server_survives(self):
        self.error(self.req('GET', '/api/missing'), 404, 'not_found')
        self.error(self.req('TRACE', '/api/tarify'), 405, 'not_found')
        self.assertEqual(self.req('GET', '/api/tarify')[0], 200)

    def test_restart_keeps_order_and_dedup(self):
        first = self.buy()
        self.stop()
        self.http = server.make_server(port=0, db=self.db, quiet=True)
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': 0.01})
        self.thread.start()
        self.port = self.http.server_port
        self.assertEqual(self.buy(), first)
        self.assertEqual(self.lookup(first['order_id']), (200, first))


if __name__ == '__main__':
    unittest.main()
