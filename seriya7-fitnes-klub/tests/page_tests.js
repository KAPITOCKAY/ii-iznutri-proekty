/* Тесты страницы покупки абонемента (static/index.html).
 *
 * Запуск:  node tests/page_tests.js
 * Playwright берём из кэша npx (NODE_PATH), браузер — установленный Chrome.
 * Тесты сами поднимают server.py на свободном порту и в конце его гасят.
 */
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const net = require('net');

const KOREN = path.resolve(__dirname, '..');
const CHROME = process.env.CHROME_PATH || 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const KARTA_OK = '4242 4242 4242 4242';
const KARTA_OTKAZ = '4000 0000 0000 0002';

let ok = 0, fail = 0;
const bedy = [];
function proverka(imya, uslovie, podrobno = '') {
  if (uslovie) { ok++; console.log('ok   ' + imya); }
  else { fail++; bedy.push(imya + ' :: ' + podrobno); console.log('FAIL ' + imya + (podrobno ? ' :: ' + podrobno : '')); }
}
function razd(t) { console.log('\n--- ' + t + ' ---'); }
const bezprobelov = (s) => String(s || '').replace(/[\s\u00a0\u2009]/g, '');

function svobodnyyPort() {
  return new Promise((gotovo) => {
    const s = net.createServer();
    s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => gotovo(p)); });
  });
}

async function zapustitServer(port) {
  const logf = fs.openSync(path.join(KOREN, 'server.tests.log'), 'w');
  const proc = spawn('python', ['-X', 'utf8', 'server.py', '--port', String(port)],
                     { cwd: KOREN, stdio: ['ignore', logf, logf] });
  const konec = Date.now() + 15000;
  while (Date.now() < konec) {
    try {
      const o = await fetch(`http://127.0.0.1:${port}/api/tarify`);
      if (o.ok) return proc;
    } catch (e) { /* сервер ещё поднимается */ }
    await new Promise((r) => setTimeout(r, 150));
  }
  throw new Error('заглушка не поднялась');
}

async function zapolnit(page, { karta = KARTA_OK, srok = '12/29', cvc = '123',
                                imya = 'Ярослав', telefon = '+7 999 123-45-67',
                                pochta = 'ivan@example.com' } = {}) {
  await page.fill('#imya', imya);
  await page.fill('#telefon', telefon);
  await page.fill('#pochta', pochta);
  await page.fill('#karta', karta);
  await page.fill('#srok', srok);
  await page.fill('#cvc', cvc);
}

async function otkryt(browser, port, viewport = { width: 1280, height: 900 }) {
  const page = await browser.newPage({ viewport });
  const konsol = [], oshibki = [];
  page.on('console', (m) => konsol.push(m.type() + ': ' + m.text()));
  page.on('pageerror', (e) => oshibki.push(String(e)));
  await page.goto(`http://127.0.0.1:${port}/`, { waitUntil: 'load' });
  await page.waitForSelector('.tarif', { timeout: 8000 });
  return { page, konsol, oshibki };
}

(async () => {
  fs.mkdirSync(path.join(KOREN, 'proverka'), { recursive: true });
  const port = await svobodnyyPort();
  const server = await zapustitServer(port);
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  try {
    // ---------------------------------------------------------------- 1. тарифы и выбор
    razd('1. Тарифы, выбор, сумма на кнопке');
    {
      const { page, oshibki } = await otkryt(browser, port);
      proverka('три карточки тарифов из /api/tarify', await page.locator('.tarif').count() === 3);
      proverka('первая карточка выбрана и подсвечена',
               await page.locator('.tarif.vybran').count() === 1 &&
               await page.locator('.tarif').first().evaluate((el) => el.classList.contains('vybran')));
      const cena1 = await page.locator('.tarif.vybran').getAttribute('data-cena');
      proverka('сумма на кнопке = цена выбранного тарифа (3. Деньги)',
               bezprobelov(await page.locator('#knopka').textContent()).includes(bezprobelov(cena1)),
               `кнопка: ${await page.locator('#knopka').textContent()} / тариф: ${cena1}`);
      await page.locator('.tarif').nth(2).click();
      const cena3 = await page.locator('.tarif.vybran').getAttribute('data-cena');
      proverka('клик по карточке переключает тариф',
               await page.locator('.tarif').nth(2).evaluate((el) => el.classList.contains('vybran')) &&
               await page.locator('.tarif.vybran').count() === 1);
      proverka('кнопка пересчитала сумму',
               bezprobelov(await page.locator('#knopka').textContent()).includes(bezprobelov(cena3)));
      proverka('нет ошибок JS на загрузке', oshibki.length === 0, oshibki.join(' | '));
      await page.close();
    }

    // ---------------------------------------------------------------- 2. ввод и формат карты
    razd('2. Ввод: карта группами по 4, буквы, пусто, 10 000 символов, эмодзи, срок');
    {
      const { page, oshibki, konsol } = await otkryt(browser, port);
      let zaprosov = 0;
      page.on('request', (r) => { if (r.url().includes('/api/pokupka')) zaprosov++; });

      await page.fill('#karta', '4242424242424242');
      proverka('номер карты форматируется группами по 4',
               (await page.inputValue('#karta')) === KARTA_OK, await page.inputValue('#karta'));

      await page.fill('#karta', 'abcd-efgh!');
      proverka('буквы в номере карты не попадают в поле',
               !/[a-zA-Zа-яА-Я]/.test(await page.inputValue('#karta')), await page.inputValue('#karta'));

      await page.fill('#imya', '');
      await page.fill('#karta', KARTA_OK);
      await page.click('#knopka');
      await page.waitForTimeout(300);
      proverka('пустая форма не отправляется и объясняет, что не так',
               zaprosov === 0 && /имя|заполн/i.test(await page.locator('#oshibka-imya').textContent() +
                                                     await page.locator('#oshibka-obshchaya').textContent()));

      await page.fill('#imya', 'я'.repeat(10000));
      const dlina = (await page.inputValue('#imya')).length;
      proverka('10 000 символов в имени не ломают страницу', oshibki.length === 0 && dlina <= 200,
               `длина в поле: ${dlina}`);

      await zapolnit(page, { imya: 'Ярослав 🌸', srok: '13/25' });
      zaprosov = 0;
      await page.click('#knopka');
      await page.waitForTimeout(300);
      proverka('срок 13/25 не принимается',
               zaprosov === 0 && /месяц|срок|мм\/гг/i.test(await page.locator('#oshibka-srok').textContent() +
                                                              await page.locator('#oshibka-obshchaya').textContent()),
               `запросов: ${zaprosov}`);

      await page.fill('#srok', '01/20');
      zaprosov = 0;
      await page.click('#knopka');
      await page.waitForTimeout(300);
      proverka('срок в прошлом не принимается',
               zaprosov === 0 && (await page.locator('#oshibka-srok').textContent()).length > 0,
               `запросов: ${zaprosov}`);
      proverka('номер карты не светится в консоли (6. Утечки)',
               !konsol.join(' ').includes('4242'), konsol.join(' | ').slice(0, 200));
      await page.close();
    }

    // ---------------------------------------------------------------- 3. успешная оплата, повторное нажатие, занятость
    razd('3. Оплата: успех, двойное нажатие, блокировка кнопки');
    {
      const { page, oshibki } = await otkryt(browser, port);
      let zaprosov = 0;
      page.on('request', (r) => { if (r.url().includes('/api/pokupka')) zaprosov++; });

      await zapolnit(page);
      await page.click('.tarif >> nth=1');
      const cena = await page.locator('.tarif.vybran').getAttribute('data-cena');
      const nazvanie = await page.locator('.tarif.vybran').getAttribute('data-nazvanie');

      // занятость кнопки: притормаживаем ответ сервера
      await page.route('**/api/pokupka', async (route) => {
        await new Promise((r) => setTimeout(r, 700));
        await route.continue();
      });
      await page.click('#knopka');
      await page.waitForTimeout(250);
      proverka('пока идёт запрос, кнопка неактивна (3)', await page.locator('#knopka').isDisabled());
      await page.waitForSelector('#itog[data-status="paid"]', { timeout: 8000 });
      proverka('успех: номер заказа, дата «до» и название тарифа',
               /оплачен до \d{2}\.\d{2}\.\d{4}/.test(await page.locator('#itog').textContent()) &&
               /заказ №\d+/.test(await page.locator('#itog').textContent()) &&
               (await page.locator('#itog').textContent()).includes(nazvanie),
               await page.locator('#itog').textContent());
      proverka('сумма взята у сервера (3. Деньги)',
               bezprobelov(await page.locator('#itog').textContent()).includes(bezprobelov(cena)),
               await page.locator('#itog').textContent());
      proverka('после успеха кнопка снова доступна', !(await page.locator('#knopka').isDisabled()));
      proverka('нет ошибок JS', oshibki.length === 0, oshibki.join(' | '));
      await page.unroute('**/api/pokupka');

      // двойное нажатие
      zaprosov = 0;
      await page.reload({ waitUntil: 'load' });
      await page.waitForSelector('.tarif');
      await zapolnit(page);
      await page.evaluate(() => {
        const k = document.querySelector('#knopka');
        k.click(); k.click();                      // два нажатия подряд, как нетерпеливый человек
      });
      await page.waitForSelector('#itog[data-status]', { timeout: 8000 });
      await page.waitForTimeout(400);
      proverka('двойное нажатие — один запрос и один заказ (2)',
               zaprosov === 1, `запросов: ${zaprosov}`);
      await page.close();
    }

    // ---------------------------------------------------------------- 4. отказ банка и повтор
    razd('4. Отказ банка и повторная попытка');
    {
      const { page } = await otkryt(browser, port);
      await zapolnit(page, { karta: KARTA_OTKAZ });
      await page.click('#knopka');
      await page.waitForSelector('#itog[data-status="declined"]', { timeout: 8000 });
      const tekst = await page.locator('#itog').textContent();
      proverka('отказ: понятный текст по-русски', /отказ|отклонил|не прошл/i.test(tekst), tekst);
      proverka('отказ: кнопка снова доступна', !(await page.locator('#knopka').isDisabled()));
      await page.fill('#karta', KARTA_OK);
      await page.click('#knopka');
      await page.waitForSelector('#itog[data-status="paid"]', { timeout: 8000 });
      proverka('после отказа можно повторить с другой картой',
               /оплачен до/.test(await page.locator('#itog').textContent()));
      await page.close();
    }

    // ---------------------------------------------------------------- 5. коды ошибок сервера
    razd('5. Коды ошибок сервера — сообщение у нужного поля');
    {
      const karty = [['no_tarif', '#oshibka-tarif'], ['bad_phone', '#oshibka-telefon'],
                     ['bad_email', '#oshibka-pochta'], ['bad_card', '#oshibka-karta'],
                     ['bad_exp', '#oshibka-srok'], ['invalid_json', '#oshibka-obshchaya']];
      for (const [kod, selektor] of karty) {
        const { page } = await otkryt(browser, port);
        await zapolnit(page);
        await page.route('**/api/pokupka', (route) => route.fulfill({
          status: 400, contentType: 'application/json', body: JSON.stringify({ error: kod }),
        }));
        await page.click('#knopka');
        await page.waitForTimeout(400);
        const tekst = (await page.locator(selektor).textContent()) || '';
        proverka(`ошибка ${kod} показана по-русски у нужного поля`,
                 tekst.trim().length > 0 && /[а-яА-Я]/.test(tekst), `«${tekst.trim()}» в ${selektor}`);
        proverka(`после ${kod} кнопка снова доступна`, !(await page.locator('#knopka').isDisabled()));
        await page.close();
      }
    }

    // ---------------------------------------------------------------- 6. сервер молчит / 500
    razd('6. Сервер не ответил и сервер ответил 500 — страница не зависает (8)');
    {
      const { page } = await otkryt(browser, port);
      await zapolnit(page);
      await page.route('**/api/pokupka', (route) => route.abort());
      await page.click('#knopka');
      await page.waitForTimeout(800);
      proverka('обрыв связи: сообщение и кнопка доступна',
               (await page.locator('#itog').textContent()).trim().length > 0 &&
               !(await page.locator('#knopka').isDisabled()), await page.locator('#itog').textContent());
      await page.unroute('**/api/pokupka');
      await page.route('**/api/pokupka', (route) => route.fulfill({ status: 500, body: 'nope' }));
      await page.click('#knopka');
      await page.waitForTimeout(800);
      proverka('500 от сервера: сообщение и кнопка доступна',
               (await page.locator('#itog').textContent()).trim().length > 0 &&
               !(await page.locator('#knopka').isDisabled()));
      await page.close();
    }

    // ---------------------------------------------------------------- 7. утечки и вставка кода
    razd('7. Утечки карты и CVC (6), вставка кода в имя (7)');
    {
      const { page, konsol } = await otkryt(browser, port);
      const zloy = '<img src=x onerror="window.__xss=1">';
      await zapolnit(page, { imya: zloy, cvc: '987' });
      await page.click('#knopka');
      await page.waitForSelector('#itog[data-status="paid"]', { timeout: 8000 });
      const hranilishche = await page.evaluate(() => JSON.stringify([localStorage, sessionStorage]));
      proverka('в localStorage/sessionStorage нет номера карты и CVC',
               !hranilishche.includes('4242') && !hranilishche.includes('987'), hranilishche.slice(0, 200));
      proverka('в консоли нет номера карты и CVC',
               !konsol.join(' ').includes('4242') && !konsol.join(' ').includes('987'),
               konsol.join(' | ').slice(0, 200));
      proverka('вставка кода в имя не исполняется',
               (await page.evaluate(() => window.__xss)) === undefined &&
               await page.locator('img[src="x"]').count() === 0);
      proverka('имя показано как текст',
               (await page.locator('#itog').textContent()).includes(zloy),
               await page.locator('#itog').textContent());
      await page.close();
    }

    // ---------------------------------------------------------------- 8. телефон 390 и компьютер
    razd('8. Телефон 390 px и компьютер');
    {
      const { page } = await otkryt(browser, port, { width: 390, height: 844 });
      const bezProkrutki = await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth + 1);
      proverka('390 px: страница не разъезжается вбок', bezProkrutki,
               await page.evaluate(() => `${document.documentElement.scrollWidth} > ${window.innerWidth}`));
      const boksy = await page.locator('.tarif').evaluateAll((els) => els.map((e) => e.getBoundingClientRect()));
      proverka('390 px: карточки тарифов стоят столбиком',
               boksy[1].top >= boksy[0].bottom - 1 && boksy[2].top >= boksy[1].bottom - 1);
      await page.screenshot({ path: path.join(KOREN, 'proverka', 'telefon-390.png'), fullPage: true });
      await page.close();

      const { page: shirokaya } = await otkryt(browser, port, { width: 1280, height: 900 });
      const boksy2 = await shirokaya.locator('.tarif').evaluateAll((els) => els.map((e) => e.getBoundingClientRect()));
      proverka('компьютер: три карточки в один ряд',
               Math.abs(boksy2[0].top - boksy2[1].top) < 2 && Math.abs(boksy2[1].top - boksy2[2].top) < 2);
      await shirokaya.screenshot({ path: path.join(KOREN, 'proverka', 'kompyuter.png'), fullPage: true });
      await shirokaya.close();
    }
    // ---------------------------------------------------------------- 9. формат даты и устаревшее состояние
    razd('9. Дата в формате ДД.ММ.ГГГГ и защита от смены тарифа во время запроса');
    {
      const { page } = await otkryt(browser, port);
      await zapolnit(page);
      await page.route('**/api/pokupka', (route) => route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ order_id: 7, status: 'paid', total: 2900, until: '2027-01-17' }),
      }));
      await page.click('#knopka');
      await page.waitForSelector('#itog[data-status="paid"]', { timeout: 8000 });
      const tekst = await page.locator('#itog').textContent();
      proverka('дата «до» показана как ДД.ММ.ГГГГ (1)',
               tekst.includes('17.01.2027') && !tekst.includes('2027-01-17'), tekst);
      await page.close();
    }
    {
      const { page } = await otkryt(browser, port);
      let otpravlennyy = null;
      page.on('request', (r) => {
        if (r.url().includes('/api/pokupka')) {
          try { otpravlennyy = r.postDataJSON(); } catch (e) { otpravlennyy = null; }
        }
      });
      await zapolnit(page);
      await page.locator('.tarif').nth(1).click();                    // «Месяц»
      const knopka_do = (await page.locator('#knopka').textContent()).trim();
      await page.route('**/api/pokupka', async (route) => {
        await new Promise((r) => setTimeout(r, 800));
        await route.continue();
      });
      await page.click('#knopka');
      await page.waitForTimeout(200);
      await page.evaluate(() => { document.querySelectorAll('.tarif')[2].click(); });  // «Год»
      proverka('во время запроса смена тарифа невозможна (2)',
               await page.locator('.tarif').nth(2).isDisabled() &&
               await page.locator('.tarif').nth(1).evaluate((el) => el.classList.contains('vybran')),
               `«Год» выбран: ${await page.locator('.tarif').nth(2).evaluate((el) => el.classList.contains('vybran'))}`);
      proverka('кнопка не сменила сумму во время запроса',
               (await page.locator('#knopka').textContent()).trim() === knopka_do,
               `${(await page.locator('#knopka').textContent()).trim()} / было ${knopka_do}`);
      await page.waitForSelector('#itog[data-status="paid"]', { timeout: 8000 });
      const tekst = await page.locator('#itog').textContent();
      proverka('в тексте успеха — тариф из отправленного запроса (2)',
               !!otpravlennyy && otpravlennyy.tarif_id === 2 &&
               tekst.includes('Месяц') && !tekst.includes('Год'),
               `${tekst} | отправлено tarif_id=${otpravlennyy && otpravlennyy.tarif_id}`);
      proverka('после ответа карточки снова доступны',
               !(await page.locator('.tarif').nth(2).isDisabled()));
      await page.close();
    }
  } catch (e) {
    fail++;
    bedy.push('исключение в прогоне :: ' + (e && e.message));
    console.log('FAIL исключение в прогоне :: ' + (e && e.message));
  } finally {
    await browser.close();
    server.kill();
  }

  console.log('\n===== ИТОГ =====');
  console.log(`проверок ок: ${ok}, провалов: ${fail}`);
  bedy.forEach((b) => console.log(' - ' + b));
  process.exit(fail ? 1 : 0);
})();
