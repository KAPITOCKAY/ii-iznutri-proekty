# -*- coding: utf-8 -*-
"""Тесты диалога цветочного бота (dialog.otvet) под точный контракт бэкенда.

Контракт:
    bouquets() -> [{id, name, price, sostav, in_stock}], price — целые рубли
    slots(date) -> [{slot: "10-12", free: N}], date — YYYY-MM-DD
    create_order(d) -> {order_id, token, total} | {error: "код", field, _status}
        d — ровно {bouquet_id, qty, name, phone, address, date, slot, card_text}
        коды: no_bouquet, out_of_stock, slot_full, bad_date, bad_phone, card_too_long

Запуск: python -m unittest -v test_dialog
"""
import datetime as dt
import re
import socket
import unittest
from pathlib import Path

import dialog

TODAY = dt.date(2026, 10, 19)          # календарь в тестах заморожен: подменяем dialog._segodnya
LIMIT_DNEY = 30                        # сервер принимает заказы максимум на 30 дней вперёд
POLYA_ZAKAZA = {"bouquet_id", "qty", "name", "phone", "address", "date", "slot", "card_text"}


def dney(days):
    return (TODAY + dt.timedelta(days=days)).isoformat()


def sled_den(days=1):
    return dney(days)


def bez_probelov(s):
    return str(s).replace(" ", "").replace("\u00a0", "").replace("\u2009", "")


_NE_DANO = object()


class FakeApi:
    """Подставной бэкенд: пишет вызовы, умеет падать и отвечать мусором."""

    def __init__(self, bouquets=None, slots=None, create_result=_NE_DANO,
                 create_error=None, slots_answer=None, bouquets_answer=None):
        self.bouquets_data = bouquets if bouquets is not None else [
            {"id": 1, "name": "Розы", "price": 2500, "sostav": "15 красных роз", "in_stock": True},
            {"id": 2, "name": "Тюльпаны", "price": 1500, "sostav": "9 тюльпанов", "in_stock": True},
            {"id": 3, "name": "Пионы", "price": 3200, "sostav": "7 пионов", "in_stock": True},
            {"id": 4, "name": "Ромашки", "price": 1200, "sostav": "11 ромашек", "in_stock": False},
        ]
        self.slots_data = slots if slots is not None else [
            {"slot": "10-12", "free": 2},
            {"slot": "12-14", "free": 0},
            {"slot": "15-17", "free": 1},
        ]
        self.create_result = {"order_id": 12, "token": "t-12", "total": 5000} \
            if create_result is _NE_DANO else create_result
        self.create_error = create_error
        self.slots_answer = slots_answer
        self.bouquets_answer = bouquets_answer
        self.calls_bouquets = 0
        self.calls_slots = []
        self.calls_create = []

    def bouquets(self):
        self.calls_bouquets += 1
        if self.bouquets_answer is not None:
            if isinstance(self.bouquets_answer, Exception):
                raise self.bouquets_answer
            return self.bouquets_answer
        return list(self.bouquets_data)

    def slots(self, date):
        self.calls_slots.append(date)
        if self.slots_answer is not None:
            if isinstance(self.slots_answer, Exception):
                raise self.slots_answer
            return self.slots_answer
        return list(self.slots_data)

    def create_order(self, data):
        self.calls_create.append(dict(data))
        if self.create_error is not None:
            if isinstance(self.create_error, Exception):
                raise self.create_error
            return self.create_error
        return self.create_result


class Base(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self._segodnya = dialog._segodnya          # «сегодня» берём не из календаря
        dialog._segodnya = lambda: TODAY

    def tearDown(self):
        dialog._segodnya = self._segodnya

    def govori(self, sostoyanie, tekst, api=None):
        novoe, soobshcheniya, knopki = dialog.otvet(sostoyanie, tekst, api or self.api)
        self.assertIsInstance(novoe, dict, "состояние должно быть словарём")
        self.assertIsInstance(soobshcheniya, list, "сообщения — список строк")
        self.assertTrue(soobshcheniya, "бот должен что-то ответить")
        for s in soobshcheniya:
            self.assertIsInstance(s, str)
            self.assertLessEqual(sum(s.count(e) for e in "🙂🌸🌷💐📝✅❌🚚📅🕒"), 1,
                                 "не больше одной эмодзи в сообщении: %r" % s)
        self.assertIsInstance(knopki, list, "кнопки — список")
        return novoe, soobshcheniya, knopki

    def do_shaga(self, tekst, api=None):
        """Дойти до нужного шага от приветствия."""
        sost, soob, knopki = self.govori({}, "/start", api)
        for t in tekst:
            sost, soob, knopki = self.govori(sost, t, api)
        return sost, soob, knopki

    def do_itoga(self, api=None, buket="1", kolvo="2", data=None, slot="10-12",
                 otkrytka="С любовью", imya="Ярослав", telefon="+7 999 123-45-67",
                 adres="Ленина 1"):
        sost, soob, knopki = self.do_shaga([buket, kolvo, data or "завтра", slot,
                                            otkrytka, imya, telefon, adres], api)
        self.assertEqual(sost["step"], "summary")
        return sost, soob, knopki

    # ---------------------------------------------------------- приветствие и букеты
    def test_privetstvie_knopki_s_cenami(self):
        sost, soob, knopki = self.govori({}, "/start")
        self.assertEqual(sost["step"], "bouquet")
        self.assertEqual(len(knopki), 3, "четвёртый букет (in_stock=false) не показываем")
        self.assertTrue(any("Розы" in k for k in knopki))
        self.assertTrue(any("2500" in bez_probelov(k) for k in knopki))
        self.assertTrue(any("₽" in k for k in knopki))

    def test_net_v_nalichii_ne_pokazyvaem(self):
        sost, soob, knopki = self.govori({}, "/start")
        self.assertFalse(any("Ромашки" in k for k in knopki), "in_stock=false скрыт")
        sost, soob, knopki = self.govori(sost, "Ромашки")
        self.assertEqual(sost["step"], "bouquet", "недоступный букет не выбирается")

    def test_sostav_pokazyvaem(self):
        sost, soob, knopki = self.do_shaga(["1"])
        self.assertIn("15 красных роз", " ".join(soob))

    def test_vybor_po_nomeru_i_nazvaniyu(self):
        sost, _, _ = self.do_shaga(["1"])
        self.assertEqual((sost["step"], sost["bouquet_id"], sost["price"]), ("qty", 1, 2500))
        sost, _, _ = self.do_shaga(["Тюльпаны"])
        self.assertEqual((sost["step"], sost["bouquet_id"]), ("qty", 2))

    def test_vybor_buketa_musor(self):
        sost, soob, knopki = self.govori({}, "/start")
        sost, soob, knopki = self.govori(sost, "абракадабра")
        self.assertEqual(sost["step"], "bouquet")
        self.assertTrue(knopki)

    # ---------------------------------------------------------- 1. кнопки принимаются обратно
    def test_knopka_buketa_rabotaet(self):
        sost, soob, knopki = self.govori({}, "/start")
        self.assertTrue(knopki)
        for knopka in knopki:
            with self.subTest(knopka=knopka):
                s2, soob2, _ = self.govori(dict(sost), knopka)
                self.assertEqual(s2["step"], "qty",
                                 "кнопка «%s» должна выбирать букет, а не ругаться" % knopka)
                self.assertTrue(s2.get("bouquet_id"))

    def test_knopka_bez_otkrytki(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12"])
        self.assertEqual(sost["step"], "card")
        sost, soob, _ = self.govori(sost, "без открытки")
        self.assertEqual((sost["step"], sost["card"]), ("name", ""))

    def test_knopki_slotov_rabotayut(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра"])
        sost, soob, knopki = self.govori(sost, "завтра")
        self.assertTrue(knopki)
        for knopka in knopki:
            with self.subTest(knopka=knopka):
                s2, soob2, _ = self.govori(dict(sost), knopka)
                self.assertEqual(s2["step"], "card")
                self.assertEqual(s2["slot"], knopka)

    def test_knopki_itoga_rabotayut(self):
        sost, _, _ = self.do_itoga()
        sost, soob, knopki = self.govori(sost, "Изменить")
        self.assertEqual(sost["step"], "bouquet")
        sost, _, _ = self.do_itoga()
        sost, soob, _ = self.govori(sost, "Подтвердить")
        self.assertEqual(sost["step"], "done")

    # ---------------------------------------------------------- количество
    def test_kolichestvo_krivoe(self):
        for vvod in ("0", "-1", "-5", "1,5", "1.5", "сто", "1000", "99+", "", "   "):
            with self.subTest(vvod=vvod):
                sost, _, _ = self.do_shaga(["1"])
                sost, soob, knopki = self.govori(sost, vvod)
                self.assertEqual(sost["step"], "qty", "«%s» не должно проходить" % vvod)

    def test_kolichestvo_ok(self):
        for vvod, zhdat in (("2", 2), (" 3 ", 3), ("99", 99)):
            with self.subTest(vvod=vvod):
                sost, _, _ = self.do_shaga(["1", vvod])
                self.assertEqual(sost["step"], "date")
                self.assertEqual(sost["qty"], zhdat)

    # ---------------------------------------------------------- дата
    def test_data_zavtra(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра"])
        self.assertEqual((sost["step"], sost["date"]), ("slot", sled_den()))

    def test_data_formats(self):
        for vvod, zhdat in (("1 ноября", dney(13)),
                            ("01.11", dney(13)),
                            ("01.11.2026", dney(13)),
                            ("2026-11-01", dney(13))):
            with self.subTest(vvod=vvod):
                sost, _, _ = self.do_shaga(["1", "2", vvod])
                self.assertEqual(sost["date"], zhdat)

    def test_data_14_fevralya_parsitsya(self):
        dialog._segodnya = lambda: dt.date(2027, 1, 20)      # до 14 февраля 25 дней
        sost, _, _ = self.do_shaga(["1", "2", "14 февраля"])
        self.assertEqual(sost["date"], "2027-02-14")
        sost, _, _ = self.do_shaga(["1", "2", "14.02"])
        self.assertEqual(sost["date"], "2027-02-14")

    # ---------------------------------------------------------- 3. не дальше 30 дней
    def test_31_den_otklonyaem_srazu_na_shage_daty(self):
        sost, soob, _ = self.do_shaga(["1", "2", dney(31)])
        self.assertEqual(sost["step"], "date", "на 31 день вперёд заказ не принимаем")
        self.assertNotIn("date", sost)
        vse = " ".join(soob)
        self.assertIn("30 дней", vse)
        self.assertIn("18 ноября 2026", vse, "сообщаем последнюю доступную дату")

    def test_30_dney_prohodit(self):
        sost, _, _ = self.do_shaga(["1", "2", dney(30)])
        self.assertEqual(sost["step"], "slot")
        self.assertEqual(sost["date"], dney(30))

    def test_dalekaya_dataslovesnami_otklonyaetsya(self):
        sost, soob, _ = self.do_shaga(["1", "2", "14 февраля"])
        self.assertEqual(sost["step"], "date", "февраль дальше предела — сразу отказ")
        self.assertIn("30 дней", " ".join(soob))

    def test_primery_v_podskazkah_priemlyutsya(self):
        """Что бот показал как пример — то и принимает (иначе пример врёт)."""
        sost, soob, _ = self.do_shaga(["1", "2"])
        primery = [p for p in re.findall(r"«([^»]+)»", " ".join(soob))
                   if p not in ("назад", "отмена")]
        self.assertTrue(primery, "в подсказке должны быть примеры дат: %r" % soob)
        for primer in primery:
            with self.subTest(podskazka="шаг даты", primer=primer):
                s2, soob2, _ = self.govori(dict(sost), primer)
                self.assertEqual(s2["step"], "slot",
                                 "пример «%s» из подсказки не приняли: %r" % (primer, soob2))

    def test_primery_v_podskazke_ob_oshibke_priemlyutsya(self):
        sost, soob, _ = self.do_shaga(["1", "2"])
        sost, soob, _ = self.govori(sost, "когда-нибудь в марте")
        self.assertEqual(sost["step"], "date")
        primery = [p for p in re.findall(r"«([^»]+)»", " ".join(soob))
                   if p not in ("назад", "отмена")]
        self.assertTrue(primery, "в подсказке об ошибке должны быть примеры дат: %r" % soob)
        for primer in primery:
            with self.subTest(podskazka="ошибка даты", primer=primer):
                s2, soob2, _ = self.govori(dict(sost), primer)
                self.assertEqual(s2["step"], "slot",
                                 "пример «%s» из подсказки об ошибке не приняли: %r"
                                 % (primer, soob2))

    def test_data_plohaya(self):
        for vvod in ("31.02", "30.02.2027", "вчера", "2020-01-01", "прошлый вторник", "", "  ",
                     "14.13", "32.01"):
            with self.subTest(vvod=vvod):
                sost, soob, knopki = self.do_shaga(["1", "2", vvod])
                self.assertEqual(sost["step"], "date", "«%s» — дата не принимается" % vvod)

    # ---------------------------------------------------------- слоты
    def test_slot_knopki_tolko_svobodnye(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра"])
        sost, soob, knopki = self.govori(sost, "завтра")
        self.assertEqual(sost["step"], "slot")
        vse = bez_probelov(" ".join(knopki))
        self.assertIn("10-12", vse)
        self.assertIn("15-17", vse)
        self.assertNotIn("12-14", vse, "занятый слот (free=0) в кнопках не появляется")
        self.assertEqual(self.api.calls_slots[-1], sled_den(), "date уходит в API как YYYY-MM-DD")

    def test_slot_vybor(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "15-17"])
        self.assertEqual((sost["step"], sost["slot"]), ("card", "15-17"))

    def test_slot_zanyat_i_nesushchestvuyushchiy(self):
        for vvod in ("12-14", "23:59"):
            with self.subTest(vvod=vvod):
                sost, _, _ = self.do_shaga(["1", "2", "завтра"])
                sost, soob, knopki = self.govori(sost, vvod)
                self.assertEqual(sost["step"], "slot")
                self.assertTrue(soob)

    # ---------------------------------------------------------- открытка
    def test_otkrytka(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12", "С днём рождения"])
        self.assertEqual((sost["step"], sost["card"]), ("name", "С днём рождения"))
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12", "без открытки"])
        self.assertEqual((sost["step"], sost["card"]), ("name", ""))

    def test_otkrytka_pustaya(self):
        sost, soob, _ = self.do_shaga(["1", "2", "завтра", "10-12", "   "])
        self.assertEqual(sost["step"], "card")
        self.assertTrue(soob)

    # ---------------------------------------------------------- имя, телефон, адрес
    def test_imya(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12", "нет", "Ярослав"])
        self.assertEqual((sost["step"], sost["name"]), ("phone", "Ярослав"))
        for vvod in ("", "   ", "я" * 10000):
            with self.subTest(vvod=vvod[:12]):
                s2, _, _ = self.govori(dict(sost), vvod)
                self.assertIn(s2["step"], ("name", "phone"))

    def test_telefon(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12", "нет", "Ярослав",
                                    "+7 999 123-45-67"])
        self.assertEqual(sost["step"], "address")
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12", "нет", "Ярослав"])
        self.assertEqual(sost["step"], "phone")
        for vvod in ("", "   ", "спросите в телеге", "12345", "8 (999) 12"):
            with self.subTest(vvod=vvod):
                s2, _, _ = self.govori(dict(sost), vvod)
                self.assertEqual(s2["step"], "phone")

    def test_adres(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12", "нет", "Ярослав",
                                    "89991234567"])
        self.assertEqual(sost["step"], "address")
        s2, _, _ = self.govori(sost, "   ")
        self.assertEqual(s2["step"], "address")

    # ---------------------------------------------------------- итог и деньги
    def test_itog_i_knopki(self):
        sost, soob, knopki = self.do_itoga(kolvo="3")
        self.assertEqual(sost["total"], 7500, "предварительный итог = цена × количество")
        self.assertIsInstance(sost["total"], int)
        self.assertIn("Подтвердить", knopki)
        self.assertIn("Изменить", knopki)
        text = " ".join(soob)
        self.assertIn("7500", bez_probelov(text))
        self.assertIn("Розы", text)

    def test_itog_soderzhit_vse_dannye(self):
        sost, soob, _ = self.do_itoga(otkrytka="С любовью", imya="Ярослав",
                                      telefon="+7 999 123-45-67", adres="Ленина 1")
        text = " ".join(soob)
        for ozhidayu in ("Розы", "Ярослав", "Ленина 1", "С любовью", "10-12"):
            self.assertIn(ozhidayu, text)

    def test_izmenit(self):
        sost, _, _ = self.do_itoga()
        sost, _, knopki = self.govori(sost, "Изменить")
        self.assertEqual(sost["step"], "bouquet")
        self.assertTrue(knopki)

    # ---------------------------------------------------------- 4. поля заказа и ответ сервера
    def test_polya_zakaza_rovno_po_kontraktu(self):
        sost, _, _ = self.do_itoga(buket="2", kolvo="2", otkrytka="Привет",
                                   imya="Ярослав", telefon="+7 999 123-45-67", adres="Ленина 1")
        sost, _, _ = self.govori(sost, "Подтвердить")
        self.assertEqual(len(self.api.calls_create), 1)
        zakaz = self.api.calls_create[0]
        self.assertEqual(set(zakaz), POLYA_ZAKAZA, "ровно поля контракта, без total")
        self.assertEqual(zakaz["bouquet_id"], 2)
        self.assertEqual(zakaz["qty"], 2)
        self.assertEqual(zakaz["name"], "Ярослав")
        self.assertEqual(zakaz["phone"], "+7 999 123-45-67")
        self.assertEqual(zakaz["address"], "Ленина 1")
        self.assertEqual(zakaz["date"], sled_den())
        self.assertEqual(zakaz["slot"], "10-12")
        self.assertEqual(zakaz["card_text"], "Привет")

    def test_bez_otkrytki_card_text_pustoy(self):
        sost, _, _ = self.do_itoga(otkrytka="без открытки")
        sost, _, _ = self.govori(sost, "Подтвердить")
        self.assertEqual(self.api.calls_create[0]["card_text"], "")

    def test_pokazyvaem_nomer_i_summu_servera(self):
        api = FakeApi(create_result={"order_id": 77, "token": "t-77", "total": 9999})
        sost, _, _ = self.do_itoga(api)
        sost, soob, knopki = self.govori(sost, "Подтвердить", api)
        text = " ".join(soob)
        self.assertEqual(sost["step"], "done")
        self.assertEqual(str(sost["order"]), "77")
        self.assertIn("77", text)
        self.assertIn("9999", bez_probelov(text), "итог берём из ответа сервера")
        self.assertIn("₽", text)
        self.assertEqual(sost.get("token"), "t-77", "токен сохраняем для фронта")

    def test_summa_servera_a_ne_svoya(self):
        api = FakeApi(create_result={"order_id": 1, "token": "t", "total": 1})
        sost, _, _ = self.do_itoga(api, kolvo="3")           # своя оценка 7500
        sost, soob, _ = self.govori(sost, "Подтвердить", api)
        text = bez_probelov(" ".join(soob))
        self.assertIn("1₽", text)
        self.assertNotIn("7500", text)

    def test_povtornoe_podtverdit_odin_zakaz(self):
        sost, _, _ = self.do_itoga()
        sost, soob1, _ = self.govori(sost, "Подтвердить")
        sost, soob2, _ = self.govori(sost, "Подтвердить")
        sost, soob3, _ = self.govori(sost, "подтвердить")
        self.assertEqual(len(self.api.calls_create), 1, "второй заказ не создаётся")
        self.assertIn("12", " ".join(soob2))
        self.assertIn("12", " ".join(soob3))

    # ---------------------------------------------------------- 5. коды ошибок
    def _oshibka(self, kod, pole=""):
        api = FakeApi(create_result={"error": kod, "field": pole, "_status": 400})
        sost, _, _ = self.do_itoga(api)
        return api, self.govori(sost, "Подтвердить", api)

    def test_oshibka_slot_full(self):
        api, (sost, soob, knopki) = self._oshibka("slot_full", "slot")
        self.assertEqual(sost["step"], "slot")
        self.assertNotIn("slot", sost)
        self.assertTrue(knopki, "снова показываем свободные слоты")
        self.assertIn("заняли", " ".join(soob))

    def test_oshibka_no_bouquet(self):
        api, (sost, soob, knopki) = self._oshibka("no_bouquet", "bouquet_id")
        self.assertEqual(sost["step"], "bouquet")
        self.assertNotIn("bouquet_id", sost)
        self.assertTrue(knopki)

    def test_oshibka_out_of_stock(self):
        api, (sost, soob, knopki) = self._oshibka("out_of_stock", "bouquet_id")
        self.assertEqual(sost["step"], "bouquet")
        self.assertTrue(knopki)

    def test_oshibka_bad_date(self):
        api, (sost, soob, knopki) = self._oshibka("bad_date", "date")
        self.assertEqual(sost["step"], "date")
        self.assertNotIn("date", sost)

    def test_oshibka_bad_phone(self):
        api, (sost, soob, knopki) = self._oshibka("bad_phone", "phone")
        self.assertEqual(sost["step"], "phone")
        self.assertNotIn("phone", sost)

    def test_oshibka_card_too_long(self):
        api, (sost, soob, knopki) = self._oshibka("card_too_long", "card_text")
        self.assertEqual(sost["step"], "card")
        self.assertNotIn("card", sost)

    def _oshibka_kod(self, kod):
        api = FakeApi(create_result={"error": kod, "field": "", "_status": 400})
        sost, _, _ = self.do_itoga(api)
        return api, self.govori(sost, "Подтвердить", api)

    def test_oshibka_invalid_qty(self):
        api, (sost, soob, knopki) = self._oshibka_kod("invalid_qty")
        self.assertEqual(sost["step"], "qty", "возврат к количеству")
        self.assertNotIn("qty", sost)
        sost, soob, knopki = self.govori(sost, "5", api)
        self.assertEqual(sost["step"], "summary", "после правки — сразу к итогу")
        self.assertEqual(sost["qty"], 5)
        self.assertEqual(sost["total"], 12500, "2500 × 5")
        self.assertIn("Подтвердить", knopki)

    def test_oshibka_invalid_name(self):
        api, (sost, soob, knopki) = self._oshibka_kod("invalid_name")
        self.assertEqual(sost["step"], "name")
        self.assertNotIn("name", sost)
        sost, soob, knopki = self.govori(sost, "Пётр", api)
        self.assertEqual(sost["step"], "summary")
        self.assertEqual(sost["name"], "Пётр")
        self.assertIn("Пётр", " ".join(soob))

    def test_oshibka_invalid_address(self):
        api, (sost, soob, knopki) = self._oshibka_kod("invalid_address")
        self.assertEqual(sost["step"], "address")
        self.assertNotIn("address", sost)
        sost, soob, knopki = self.govori(sost, "Ленина 1, кв. 5", api)
        self.assertEqual(sost["step"], "summary")
        self.assertEqual(sost["address"], "Ленина 1, кв. 5")

    def test_oshibka_invalid_slot(self):
        api, (sost, soob, knopki) = self._oshibka_kod("invalid_slot")
        self.assertEqual(sost["step"], "slot")
        self.assertNotIn("slot", sost)
        self.assertTrue(knopki, "показываем свободное время заново")
        sost, soob, knopki = self.govori(sost, "15-17", api)
        self.assertEqual(sost["step"], "summary")
        self.assertEqual(sost["slot"], "15-17")

    def test_oshibka_invalid_card_text(self):
        api, (sost, soob, knopki) = self._oshibka_kod("invalid_card_text")
        self.assertEqual(sost["step"], "card")
        self.assertNotIn("card", sost)
        sost, soob, knopki = self.govori(sost, "С юбилеем", api)
        self.assertEqual(sost["step"], "summary")
        self.assertEqual(sost["card"], "С юбилеем")

    def test_neizvestnyy_kod_ostayomsya_na_itoge(self):
        api, (sost, soob, knopki) = self._oshibka("chto_to_strannoe", "field")
        self.assertEqual(sost["step"], "summary")
        self.assertIn("Что-то пошло не так", " ".join(soob))
        self.assertIn("Подтвердить", knopki)
        self.assertNotIn("order", sost)

    def test_posle_oshibki_slot_full_mozhno_zavershit(self):
        api = FakeApi(create_result={"error": "slot_full", "field": "slot", "_status": 400})
        sost, _, _ = self.do_itoga(api)
        sost, soob, knopki = self.govori(sost, "Подтвердить", api)
        self.assertEqual(sost["step"], "slot")
        api.create_result = {"order_id": 5, "token": "t5", "total": 5000}
        sost, soob, _ = self.govori(sost, "15-17", api)
        sost, soob, _ = self.govori(sost, "Подтвердить", api)
        self.assertEqual(sost["step"], "done")
        self.assertEqual(str(sost["order"]), "5")
        self.assertEqual(len(api.calls_create), 2)

    # ---------------------------------------------------------- 6. мусор и исключения
    def test_otvet_ne_togo_tipa_ne_ronaet(self):
        for rezultat in (["ерунда"], "строка", None, {}, {"token": "t"}, {"order_id": None}):
            with self.subTest(rezultat=rezultat):
                api = FakeApi(create_result=rezultat)
                sost, _, _ = self.do_itoga(api)
                sost, soob, knopki = self.govori(sost, "Подтвердить", api)
                self.assertEqual(sost["step"], "summary", "не тот тип — остаёмся на итоге")
                self.assertIn("Что-то пошло не так", " ".join(soob))
                self.assertNotIn("order", sost)

    def test_isklyuchenie_pri_sozdanii_ne_ronaet(self):
        api = FakeApi(create_error=RuntimeError("сеть отвалилась"))
        sost, _, _ = self.do_itoga(api)
        sost, soob, knopki = self.govori(sost, "Подтвердить", api)
        self.assertEqual(sost["step"], "summary")
        self.assertIn("Что-то пошло не так", " ".join(soob))
        self.assertNotIn("order", sost)

    def test_isklyuchenie_bouquets_i_slots(self):
        api = FakeApi(bouquets_answer=RuntimeError("нет связи"))
        sost, soob, knopki = self.govori({}, "/start", api)
        self.assertTrue(soob)
        self.assertNotEqual(sost.get("step"), "done")
        api = FakeApi(slots_answer=RuntimeError("нет связи"))
        sost, _, _ = self.do_shaga(["1", "2", "завтра"], api)
        self.assertEqual(sost["step"], "date", "при ошибке слотов возвращаемся к дате")
        self.assertTrue(soob)

    def test_musornye_otvety_api(self):
        for answer in ("строка", None, 5, {"error": "x"}):
            with self.subTest(answer=answer):
                api = FakeApi(bouquets_answer=answer)
                sost, soob, knopki = self.govori({}, "/start", api)
                self.assertTrue(soob)
        api = FakeApi(slots_answer="строка")
        sost, soob, knopki = self.do_shaga(["1", "2", "завтра"], api)
        self.assertEqual(sost["step"], "date")
        api = FakeApi(bouquets=[{"id": 1}, "мусор", {"name": "Без цены"}])
        sost, soob, knopki = self.govori({}, "/start", api)
        self.assertTrue(soob)

    def test_api_bez_metodov(self):
        sost, soob, knopki = self.govori({}, "/start", object())
        self.assertTrue(soob)
        sost, soob, knopki = self.govori(sost, "1", object())
        self.assertTrue(soob)

    # ---------------------------------------------------------- назад / отмена / start
    def test_otmena(self):
        sost, _, _ = self.do_itoga()
        sost, soob, knopki = self.govori(sost, "отмена")
        self.assertEqual(sost["step"], "bouquet")
        self.assertNotIn("bouquet_id", sost)
        self.assertNotIn("total", sost)

    def test_start_sbros(self):
        sost, _, _ = self.do_itoga()
        sost, soob, knopki = self.govori(sost, "/start")
        self.assertEqual(sost["step"], "bouquet")
        self.assertNotIn("total", sost)

    def test_nazad_po_shagam(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12", "нет", "Ярослав",
                                    "89991234567", "Ленина 1"])
        self.assertEqual(sost["step"], "summary")
        for ozhidaemyy in ("address", "phone", "name", "card"):
            sost, _, _ = self.govori(sost, "назад")
            self.assertEqual(sost["step"], ozhidaemyy)

    def test_nazad_iz_pervogo_shaga(self):
        sost, _, _ = self.govori({}, "/start")
        sost, soob, knopki = self.govori(sost, "назад")
        self.assertEqual(sost["step"], "bouquet")

    # ---------------------------------------------------------- непонятный и пустой ввод
    def test_neponyatnyy_vvod(self):
        sost, _, _ = self.govori({}, "/start")
        sost, soob, knopki = self.govori(sost, "㊙️")
        self.assertEqual(sost["step"], "bouquet")
        sost, _, _ = self.govori(sost, "1")
        sost, soob, _ = self.govori(sost, "хм")
        self.assertEqual(sost["step"], "qty")
        sost, soob, _ = self.govori(sost, "2")
        sost, soob, _ = self.govori(sost, "когда-нибудь")
        self.assertEqual(sost["step"], "date")

    def test_pustoy_vvod_na_kazhdom_shage(self):
        put = ["1", "2", "завтра", "10-12", "нет", "Ярослав", "89991234567"]
        for glubina in range(0, len(put) + 1):
            with self.subTest(glubina=glubina):
                sost, _, _ = self.do_shaga(put[:glubina])
                s2, soob, _ = self.govori(sost, "")
                self.assertEqual(s2["step"], sost["step"])
                self.assertTrue(soob)

    # ---------------------------------------------------------- 7. текст отдаём как есть
    def test_tekst_ne_ekraniruetsya_dvoyno(self):
        """Экранирует фронт (textContent), поэтому из dialog текст выходит как введён."""
        imya = "Аня <3 & Ко"
        sost, soob, _ = self.do_shaga(["1", "2", "завтра", "10-12", "нет", imya,
                                       "89991234567", "Ленина 1"])
        vse = " ".join(soob)
        self.assertIn(imya, vse, "имя показываем ровно как ввёл человек")
        for artefakt in ("&amp;", "&lt;", "&gt;"):
            self.assertNotIn(artefakt, vse, "никаких HTML-сущностей")
        sost, soob, _ = self.govori(sost, "Подтвердить")
        self.assertNotIn("&amp;", " ".join(soob))
        self.assertEqual(self.api.calls_create[0]["name"], imya)

    def test_script_v_otkrytke_ostayotsya_tekstom(self):
        zloy = "<script>alert(1)</script>"
        sost, soob, _ = self.do_shaga(["1", "2", "завтра", "10-12", zloy, "Иван",
                                       "89991234567", "Ленина 1"])
        self.assertEqual(sost["card"], zloy, "хранится как есть")
        self.assertIn(zloy, " ".join(soob), "отдаём как текст, экранирование — на фронте")
        sost, soob, _ = self.govori(sost, "Подтвердить")
        self.assertEqual(self.api.calls_create[0]["card_text"], zloy)

    def test_yolochka_i_emoji(self):
        sost, _, _ = self.do_shaga(["1", "2", "завтра", "10-12", "С днём рождения! Ёлка 🎄",
                                    "Ярослав", "89991234567", "Ленина 1"])
        self.assertIn("Ёлка", sost["card"])

    # ---------------------------------------------------------- 9. без сети и зависимостей
    def test_net_setevyh_vyzovov(self):
        original = socket.socket.connect

        def zapret(*a, **k):
            raise AssertionError("диалог полез в сеть")

        socket.socket.connect = zapret
        try:
            sost, _, _ = self.do_itoga()
            sost, soob, _ = self.govori(sost, "Подтвердить")
        finally:
            socket.socket.connect = original
        self.assertEqual(len(self.api.calls_create), 1)

    def test_tolko_standartnaya_biblioteka(self):
        istochnik = Path(dialog.__file__).read_text(encoding="utf-8")
        for modul in ("requests", "urllib", "httpx", "aiohttp", "telegram", "flask", "socket",
                      "html"):
            self.assertNotIn("import " + modul, istochnik,
                             "dialog.py не должен тянуть %s" % modul)


if __name__ == "__main__":
    unittest.main(verbosity=2)
