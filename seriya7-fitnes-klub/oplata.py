"""Local bank simulator. No network calls, card storage or logging.

Only public simulator PAN vectors are constants. Actual merchant credentials
are read from the environment, never from request JSON or committed files.
"""
import hashlib
import hmac
import os

SHOP_ID = '104577'
EXPECTED_SECRET_SHA256 = '7eaac9d99eb8e93ae7ab20a0ea52c254caadf7c2c28702f11292a8ac9a351ae9'


class GatewayError(Exception):
    pass


def secret_key():
    key = os.environ.get('FITNES_SECRET_KEY', '')
    shop = os.environ.get('FITNES_SHOP_ID', SHOP_ID)
    if shop != SHOP_ID or not key or not hmac.compare_digest(hashlib.sha256(key.encode('utf-8')).hexdigest(), EXPECTED_SECRET_SHA256):
        raise GatewayError('Настройте FITNES_SECRET_KEY и FITNES_SHOP_ID по данным магазина')
    return key.encode('utf-8')


def charge(amount, card):
    """Return paid/declined; require the configured shop's exact secret key.

    Amount is integer rubles, card is a transient dict. No value is retained.
    Other valid-format PANs are declined by this deliberately limited simulator.
    """
    secret_key()
    if type(amount) is not int or amount <= 0 or not isinstance(card, dict):
        raise GatewayError('Неверный запрос имитатору оплаты')
    number = card.get('number', '')
    if not isinstance(number, str):
        raise GatewayError('Неверный запрос имитатору оплаты')
    number = number.replace(' ', '').replace('-', '')
    return 'paid' if number == '4242424242424242' else 'declined'
