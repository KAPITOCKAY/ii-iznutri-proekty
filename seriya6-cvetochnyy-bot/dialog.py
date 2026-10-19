# -*- coding: utf-8 -*-
"""Диалог цветочного бота.

    otvet(sostoyanie: dict, tekst: str, api) -> (novoe_sostoyanie, [сообщения], [кнопки])

Контракт бэкенда:
    bouquets()          -> [{"id", "name", "price", "sostav", "in_stock"}]
                           price — целые рубли; in_stock=false не показываем
    slots(date)         -> [{"slot": "10-12", "free": N}]   date — "YYYY-MM-DD"
    create_order(d)     -> {"order_id", "token", "total"} или {"error": "код", "field", "_status"}
        d — ровно {bouquet_id, qty, name, phone, address, date, slot, card_text},
        суммy считает сервер.
        коды ошибок: no_bouquet, out_of_stock, slot_full, bad_date, bad_phone, card_too_long

Только стандартная библиотека, без сети. Сообщения — чистый текст без HTML-сущностей:
экранирование на стороне фронта (textContent). Шаги состояния:
    bouquet -> qty -> date -> slot -> card -> name -> phone -> address -> summary -> done
"""
import datetime as dt
import re

MESYACY = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
           "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
           "декабря": 12}
NAZAD = {"qty": "bouquet", "date": "qty", "slot": "date", "card": "slot",
         "name": "card", "phone": "name", "address": "phone", "summary": "address",
         "done": "bouquet", "bouquet": "bouquet"}
NET_OTKRYTKI = ("без открытки", "без", "нет", "не надо", "нет открытки", "-", "—")
OTMENA = ("отмена", "cancel", "/cancel", "стоп")
START = ("/start", "start", "начать", "начать заново", "сначала", "заново")

TEKST_OSHIBNY = "Что-то пошло не так, попробуйте ещё раз."
PREDEL_DNEY = 30                    # сервер принимает заказы не дальше, чем на 30 дней вперёд
TEKSTY_KODOV = {
    "no_bouquet": "Этот букет больше недоступен. Выберите, пожалуйста, другой.",
    "out_of_stock": "Такого букета сейчас нет в наличии. Выберите, пожалуйста, другой.",
    "slot_full": "Это время только что заняли. Выберите, пожалуйста, другое.",
    "bad_date": "С датой что-то не так. Назовите, пожалуйста, другую.",
    "bad_phone": "Телефон не подошёл. Напишите, пожалуйста, другой номер.",
    "card_too_long": "Открытка длинновата. Напишите, пожалуйста, короче.",
    "invalid_qty": "Количество не подошло. Напишите, пожалуйста, число от 1 до 99.",
    "invalid_name": "С именем что-то не так. Напишите, пожалуйста, имя ещё раз.",
    "invalid_address": "Адрес не подошёл. Напишите, пожалуйста, адрес подробнее.",
    "invalid_slot": "Это время не подошло. Выберите, пожалуйста, другое.",
    "invalid_card_text": "Текст открытки не подошёл. Напишите, пожалуйста, другой.",
}
KOD_STEP = {"no_bouquet": "bouquet", "out_of_stock": "bouquet", "slot_full": "slot",
            "bad_date": "date", "bad_phone": "phone", "card_too_long": "card",
            "invalid_qty": "qty", "invalid_name": "name", "invalid_address": "address",
            "invalid_slot": "slot", "invalid_card_text": "card"}


def _dengi(cyfry):
    return "{:,}".format(int(cyfry)).replace(",", " ") + " ₽"


def _segodnya():
    """Сегодняшняя дата. Отдельной функцией — чтобы тесты подменяли календарь."""
    return dt.date.today()


def _tekst(znachenie):
    """Текст как есть. Экранирование — на стороне фронта (textContent)."""
    return "" if znachenie is None else str(znachenie)


def _sgladit(tekst):
    """Текст кнопки к сравнимому виду: регистр и пробелы (в том числе неразрывные)."""
    return re.sub(r"\s+", " ", str(tekst).replace("\u00a0", " ")).strip().lower()


def _kod_oshibki(otvet):
    """Код ошибки из ответа бэкенда {error, field, _status} либо None."""
    if not isinstance(otvet, dict) or "error" not in otvet:
        return None
    kod = otvet.get("error")
    return kod if isinstance(kod, str) and kod else "unknown"


# ---------------------------------------------------------------- API

def _bukety(api):
    """(список букетов в наличии, текст ошибки)."""
    try:
        otvet = api.bouquets()
    except Exception:
        return None, "Не получилось получить список букетов."
    if isinstance(otvet, dict):
        return None, "Не получилось получить список букетов."
    if not isinstance(otvet, (list, tuple)):
        return [], None
    zhivye = []
    for b in otvet:
        if not isinstance(b, dict) or b.get("in_stock") is False:
            continue
        try:
            cena = int(b.get("price", 0))
        except (TypeError, ValueError):
            continue
        zhivye.append({"id": b.get("id"), "name": str(b.get("name", "")),
                       "price": cena, "sostav": str(b.get("sostav") or "")})
    return zhivye, None


def _sloty(api, data):
    """(свободные слоты, текст ошибки) или (None, код ошибки)."""
    try:
        otvet = api.slots(data)
    except Exception:
        return None, {"code": "unknown"}
    kod = _kod_oshibki(otvet)
    if kod:
        return None, {"code": kod}
    if not isinstance(otvet, (list, tuple)):
        return [], None
    zhivye = []
    for s in otvet:
        if not isinstance(s, dict):
            continue
        znachenie = s.get("slot")
        if not znachenie:
            continue
        try:
            svobodno = int(s.get("free", 0))
        except (TypeError, ValueError):
            svobodno = 0
        if svobodno > 0:
            zhivye.append(str(znachenie))
    return zhivye, None


def _sozdat(api, zakaz):
    """(успешный ответ, код ошибки). Мусор и исключения — код unknown."""
    try:
        otvet = api.create_order(dict(zakaz))
    except Exception:
        return None, {"code": "unknown"}
    if not isinstance(otvet, dict):
        return None, {"code": "unknown"}
    kod = _kod_oshibki(otvet)
    if kod:
        return None, {"code": kod}
    if otvet.get("order_id") in (None, ""):
        return None, {"code": "unknown"}
    return otvet, None


# ---------------------------------------------------------------- разбор ввода

def _buket_po_vvodu(bukety, vvod):
    nizkiy = vvod.lower()
    if vvod.isdigit():
        for b in bukety:
            if str(b.get("id")) == vvod:
                return b
        if 1 <= int(vvod) <= len(bukety):
            return bukety[int(vvod) - 1]
        return None
    for b in bukety:
        if nizkiy and nizkiy in b["name"].lower():
            return b
    return None


def _data_po_vvodu(vvod):
    segodnya = _segodnya()
    poslednyaya = segodnya + dt.timedelta(days=PREDEL_DNEY)
    t = re.sub(r"\s+", " ", vvod.strip().lower().replace("ё", "е"))
    if t == "завтра":
        return segodnya + dt.timedelta(days=1), None
    if t == "послезавтра":
        return segodnya + dt.timedelta(days=2), None
    if t == "сегодня":
        return segodnya, None
    god = None
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        god, mesyac, den = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?", t)
        if m:
            den, mesyac = int(m.group(1)), int(m.group(2))
            if m.group(3):
                god = int(m.group(3)) + (2000 if int(m.group(3)) < 100 else 0)
        else:
            m = re.fullmatch(r"(\d{1,2}) ([а-я]+)(?: (\d{4}))?", t)
            if not m or m.group(2) not in MESYACY:
                return None, "Не понял дату. Напишите, например, %s." % _primery_daty()
            den, mesyac = int(m.group(1)), MESYACY[m.group(2)]
            if m.group(3):
                god = int(m.group(3))
    if god is None:
        god = segodnya.year
        try:
            if dt.date(god, mesyac, den) <= segodnya:
                god += 1
        except ValueError:
            pass
    try:
        data = dt.date(god, mesyac, den)
    except ValueError:
        return None, "Такой даты не бывает. Проверьте, пожалуйста, день и месяц."
    if data < segodnya:
        return None, "Эта дата уже прошла. Назовите будущую дату."
    if data > poslednyaya:
        return None, ("Принимаем заказы на %d дней вперёд, до %s. Назовите, пожалуйста, дату "
                      "в этом промежутке." % (PREDEL_DNEY, _pisat_datu(poslednyaya.isoformat())))
    return data, None


def _normalnyy_telefon(vvod):
    if not re.fullmatch(r"[0-9+ ()\-]+", vvod):
        return False
    return len(re.sub(r"\D", "", vvod)) >= 10


def _primery_daty():
    """Примеры дат для подсказок: строим от сегодня, чтобы пример точно проходил предел."""
    segodnya = _segodnya()
    return "«завтра», «%s» или «%s»" % ((segodnya + dt.timedelta(days=1)).strftime("%d.%m"),
                                        (segodnya + dt.timedelta(days=7)).isoformat())


def _pisat_datu(iso):
    try:
        data = dt.date.fromisoformat(iso)
    except (TypeError, ValueError):
        return str(iso)
    mesyacy = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
               "августа", "сентября", "октября", "ноября", "декабря"]
    return "%d %s %d" % (data.day, mesyacy[data.month - 1], data.year)


# ---------------------------------------------------------------- показ шагов

def _pokazat_bukety(sost, api, privetstvie=False):
    bukety, beda = _bukety(api)
    sost["step"] = "bouquet"
    soob = []
    if privetstvie:
        soob.append("Здравствуйте! Помогу выбрать букет и оформить доставку 🌸")
        soob.append("В любой момент можно написать «назад» или «отмена».")
    if beda:
        soob.append(beda + " Напишите «/start», попробуем снова.")
        return sost, soob, []
    sost["bukety"] = bukety
    if not bukety:
        soob.append("Букетов сейчас нет в наличии. Загляните, пожалуйста, позже.")
        return sost, soob, []
    soob.append("Какой букет вам нравится? Напишите номер или название.")
    knopki = ["%s — %s" % (b["name"], _dengi(b["price"])) for b in bukety]
    sost["knopki_buketov"] = {_sgladit(kn): i for i, kn in enumerate(knopki)}
    return sost, soob, knopki


def _pokazat_sloty(sost, api):
    sloty, beda = _sloty(api, sost.get("date"))
    if beda:
        kod = beda["code"]
        if kod == "bad_date":
            sost["step"] = "date"
            sost.pop("date", None)
            return sost, [TEKSTY_KODOV["bad_date"]], []
        sost["step"] = "date"
        return sost, [TEKST_OSHIBNY + " Попробуйте, пожалуйста, другую дату."], []
    if not sloty:
        sost["step"] = "date"
        sost.pop("vybor_slotov", None)
        return sost, ["На %s свободного времени нет. Давайте выберем другую дату."
                      % _pisat_datu(sost.get("date"))], []
    sost["vybor_slotov"] = sloty
    sost["step"] = "slot"
    return sost, ["Свободное время на %s. Выберите, пожалуйста:"
                  % _pisat_datu(sost.get("date"))], list(sloty)


def _vopros_shaga(sost, api):
    step = sost.get("step")
    if step == "qty":
        return sost, ["Сколько букетов нужно? Напишите число от 1 до 99."], []
    if step == "date":
        return sost, ["На какую дату доставить? Например, %s." % _primery_daty()], []
    if step == "slot":
        if sost.get("vybor_slotov"):
            return sost, ["Выберите, пожалуйста, другое время:"], list(sost["vybor_slotov"])
        return _pokazat_sloty(sost, api)
    if step == "card":
        return sost, ["Что написать на открытке? Если не нужно — напишите «без открытки»."], \
            ["без открытки"]
    if step == "name":
        return sost, ["Как вас зовут? Напишите имя."], []
    if step == "phone":
        return sost, ["Оставьте, пожалуйста, телефон для связи."], []
    if step == "address":
        return sost, ["Куда привезти? Напишите адрес: улица, дом, квартира."], []
    return _pokazat_bukety(sost, api)


def _itog(sost):
    return ("Проверьте заказ:\n"
            "Букет: %s — %s\n"
            "Количество: %d\n"
            "Дата: %s, время %s\n"
            "Открытка: %s\n"
            "Имя: %s\n"
            "Телефон: %s\n"
            "Адрес: %s\n"
            "Итого: %s"
            % (_tekst(sost.get("bouquet_name")), _dengi(sost.get("price", 0)),
               int(sost.get("qty", 0)), _pisat_datu(sost.get("date")),
               _tekst(sost.get("slot")),
               _tekst(sost.get("card")) or "без открытки",
               _tekst(sost.get("name")), _tekst(sost.get("phone")),
               _tekst(sost.get("address")), _dengi(sost.get("total", 0))))


def _obrabotat_oshibku(sost, api, oshibka):
    """Ошибка бэкенда: человеческий текст и возврат на нужный шаг."""
    kod = oshibka.get("code")
    tekst = TEKSTY_KODOV.get(kod, TEKST_OSHIBNY)
    shag = KOD_STEP.get(kod)
    if shag is None:                                  # неизвестный код или мусор
        sost["step"] = "summary"
        return sost, [TEKST_OSHIBNY], ["Подтвердить", "Изменить"]
    if shag == "bouquet":
        for klyuch in ("bouquet_id", "bouquet_name", "price", "total"):
            sost.pop(klyuch, None)
        sost["step"] = "bouquet"
        sost, soob, knopki = _pokazat_bukety(sost, api)
        return sost, [tekst] + soob, knopki
    if shag == "slot":
        sost.pop("slot", None)
        sost["step"] = "slot"
        sost["posle_oshibki"] = True                  # данные уже собраны — вернёмся к итогу
        sost, soob, knopki = _pokazat_sloty(sost, api)
        return sost, [tekst] + soob, knopki
    # сюда попадают date, qty, name, phone, address и card: правим одно поле и возвращаемся к итогу
    sost["step"] = shag
    sost["posle_oshibki"] = True
    if shag == "date":
        for klyuch in ("date", "slot", "vybor_slotov"):
            sost.pop(klyuch, None)
        return sost, [tekst], []
    if shag == "qty":
        for klyuch in ("qty", "total"):
            sost.pop(klyuch, None)
        return sost, [tekst], []
    sost.pop({"phone": "phone", "name": "name", "address": "address", "card": "card"}[shag], None)
    knopki = ["без открытки"] if shag == "card" else []
    return sost, [tekst], knopki


# ---------------------------------------------------------------- диалог

def otvet(sostoyanie, tekst, api):
    sost = dict(sostoyanie) if isinstance(sostoyanie, dict) else {}
    vvod = (tekst or "").strip()
    nizkiy = re.sub(r"\s+", " ", vvod.lower().replace("ё", "е"))
    step = sost.get("step") or "bouquet"

    # --- команды: работают на любом шаге
    if nizkiy in START:
        return _pokazat_bukety({}, api, privetstvie=True)
    if nizkiy in OTMENA:
        sost, soob, knopki = _pokazat_bukety({}, api)
        return sost, ["Хорошо, начнём заново."] + soob, knopki
    if nizkiy in ("назад", "/back"):
        nazad = NAZAD.get(step, "bouquet")
        if nazad in ("date", "qty"):
            sost.pop("slot", None)
            sost.pop("vybor_slotov", None)
        if nazad == "qty":
            sost.pop("date", None)
        sost["step"] = nazad
        return _vopros_shaga(sost, api)

    # --- заказ уже создан: повторное «Подтвердить» второй заказ не делает
    if sost.get("zakaz_sozdan"):
        return sost, ["Заказ №%s уже принят. Мы позвоним, чтобы подтвердить доставку ✅"
                      % _tekst(sost.get("order"))], []

    if step == "bouquet":
        bukety = sost.get("bukety")
        if bukety is None:
            sost, soob, knopki = _pokazat_bukety(sost, api)
            if not vvod:
                return sost, soob, knopki
            bukety = sost.get("bukety") or []
        vybran = None
        knopki_buketov = sost.get("knopki_buketov") or {}
        nomer = knopki_buketov.get(_sgladit(vvod))
        if nomer is not None and 0 <= nomer < len(bukety):
            vybran = bukety[nomer]                     # нажали кнопку букета
        else:
            vybran = _buket_po_vvodu(bukety, vvod)     # ввели номер или название
        if not vybran:
            sost["step"] = "bouquet"
            knopki = ["%s — %s" % (b["name"], _dengi(b["price"])) for b in bukety]
            sost["knopki_buketov"] = {_sgladit(kn): i for i, kn in enumerate(knopki)}
            return sost, ["Не нашёл такой букет. Выберите, пожалуйста, из списка."], knopki
        sost["bouquet_id"] = vybran["id"]
        sost["bouquet_name"] = vybran["name"]
        sost["price"] = vybran["price"]
        sost["step"] = "qty"
        hvost = " (%s)" % _tekst(vybran["sostav"]) if vybran.get("sostav") else ""
        return sost, ["«%s», %s%s. Сколько букетов нужно?"
                      % (_tekst(vybran["name"]), _dengi(vybran["price"]), hvost)], []

    if step == "qty":
        if not re.fullmatch(r"\d{1,2}", vvod) or int(vvod) < 1:
            sost["step"] = "qty"
            return sost, ["Нужно целое число от 1 до 99. Сколько букетов?"], []
        sost["qty"] = int(vvod)
        sost["total"] = sost["price"] * sost["qty"]
        if sost.pop("posle_oshibki", False):
            sost["step"] = "summary"
            return sost, [_itog(sost)], ["Подтвердить", "Изменить"]
        sost["step"] = "date"
        return sost, ["На какую дату доставить? Например, %s." % _primery_daty()], []

    if step == "date":
        data, beda = _data_po_vvodu(vvod)
        if beda:
            sost["step"] = "date"
            return sost, [beda], []
        sost["date"] = data.isoformat()
        for klyuch in ("slot", "vybor_slotov"):
            sost.pop(klyuch, None)
        return _pokazat_sloty(sost, api)

    if step == "slot":
        vybor = sost.get("vybor_slotov") or []
        vybran = None
        for znachenie in vybor:
            if znachenie == vvod or (len(vvod) >= 2 and znachenie.startswith(vvod)):
                vybran = znachenie
                break
        if not vybran:
            sost["step"] = "slot"
            return sost, ["Выберите, пожалуйста, время из кнопок."], list(vybor)
        sost["slot"] = vybran
        if sost.pop("posle_oshibki", False):
            sost["step"] = "summary"
            return sost, [_itog(sost)], ["Подтвердить", "Изменить"]
        sost["step"] = "card"
        return sost, ["Что написать на открытке? Если не нужно — напишите «без открытки»."], \
            ["без открытки"]

    if step == "card":
        if not vvod:
            sost["step"] = "card"
            return sost, ["Напишите текст открытки или «без открытки»."], ["без открытки"]
        sost["card"] = "" if nizkiy in NET_OTKRYTKI else vvod
        if sost.pop("posle_oshibki", False):
            sost["step"] = "summary"
            return sost, ["Открытку записал." if sost["card"] else "Открытку не пишем.",
                          _itog(sost)], ["Подтвердить", "Изменить"]
        sost["step"] = "name"
        return sost, ["Открытку записал. Как вас зовут?" if sost["card"]
                      else "Открытку не пишем. Как вас зовут?"], []

    if step == "name":
        if not 1 <= len(vvod) <= 100:
            sost["step"] = "name"
            return sost, ["Напишите, пожалуйста, имя — от 1 до 100 символов."], []
        sost["name"] = vvod
        if sost.pop("posle_oshibki", False):
            sost["step"] = "summary"
            return sost, [_itog(sost)], ["Подтвердить", "Изменить"]
        sost["step"] = "phone"
        return sost, ["Приятно познакомиться! Оставьте телефон для связи."], []

    if step == "phone":
        if not _normalnyy_telefon(vvod):
            sost["step"] = "phone"
            return sost, ["Проверьте телефон: цифры, «+», «-», скобки и пробелы, "
                          "не меньше 10 цифр."], []
        sost["phone"] = vvod
        if sost.pop("posle_oshibki", False):
            sost["step"] = "summary"
            return sost, ["Телефон записал.", _itog(sost)], ["Подтвердить", "Изменить"]
        sost["step"] = "address"
        return sost, ["Куда привезти? Напишите адрес: улица, дом, квартира."], []

    if step == "address":
        if not 5 <= len(vvod) <= 200:
            sost["step"] = "address"
            return sost, ["Напишите адрес подробнее: улица, дом, квартира (от 5 символов)."], []
        sost["address"] = vvod
        sost.pop("posle_oshibki", None)
        sost["step"] = "summary"
        return sost, [_itog(sost)], ["Подтвердить", "Изменить"]

    if step == "summary":
        if nizkiy in ("изменить", "/change"):
            sost["step"] = "bouquet"
            return _pokazat_bukety(sost, api)
        if nizkiy not in ("подтвердить", "/confirm", "ок", "да", "готово"):
            sost["step"] = "summary"
            return sost, [_itog(sost) + "\n\nЕсли всё верно — нажмите «Подтвердить»."], \
                ["Подтвердить", "Изменить"]
        zakaz = {"bouquet_id": sost.get("bouquet_id"), "qty": sost.get("qty"),
                 "name": sost.get("name"), "phone": sost.get("phone"),
                 "address": sost.get("address"), "date": sost.get("date"),
                 "slot": sost.get("slot"), "card_text": sost.get("card", "")}
        uspekh, oshibka = _sozdat(api, zakaz)
        if oshibka:
            return _obrabotat_oshibku(sost, api, oshibka)
        sost["order"] = uspekh.get("order_id")
        sost["token"] = uspekh.get("token")
        sost["zakaz_sozdan"] = True
        sost["step"] = "done"
        summa = uspekh.get("total")
        hvost = " на %s" % _dengi(summa) if isinstance(summa, int) and not isinstance(summa, bool) \
            else ""
        return sost, ["Заказ №%s%s принят ✅ Мы позвоним, чтобы подтвердить доставку."
                      % (_tekst(sost["order"]), hvost)], []

    # --- испорченное состояние: показываем начало
    return _pokazat_bukety({}, api, privetstvie=True)
