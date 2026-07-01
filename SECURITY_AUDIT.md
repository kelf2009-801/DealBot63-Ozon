# 🔐 АУДИТ БЕЗОПАСНОСТИ — Ozon Price Monitor (DealBot63_bot)

**Дата:** 28.06.2026  
**Файл:** `main_full.py` (1231 стр.)  
**Стек:** aiogram 3.x, CloakBrowser, SQLite, Telegram Stars  
**Аудитор:** Деби (хакерша за Бакугана)  

---

## 📊 СВОДКА

| Приоритет | Количество |
|-----------|------------|
| 🔴 CRITICAL | 5 |
| 🟠 HIGH | 5 |
| 🟡 MEDIUM | 5 |
| 🟢 LOW | 5 |
| **Всего** | **20** |

---

## 🔴 CRITICAL (Немедленный фикс)

### C1. Пароль админа в открытом виде (Hardcoded Credentials)

**Файл:** `config.py:6`  
**Проблема:** `ADMIN_PASSWORD = "1434"` — пароль хранится в открытом виде в коде, который копируется в Docker-образ (`COPY config.py .`). Любой, кто получит доступ к репозиторию, логам сборки или Docker-образу, получает полный админский доступ.

**Риск:** Полная компрометация админки: рассылка всем пользователям, изменение тарифов, кража данных.

**Фикс:**
```python
# config.py
import os
import hashlib

# Храним ТОЛЬКО хеш, не пароль
ADMIN_PASSWORD_HASH = os.getenv(
    "ADMIN_PASSWORD_HASH",
    "8b7df143d91c716ecfa5fc1730022f6b421b05cedee8fd52b1fc65a96030ad52"  # sha256("1434")
)
```
```python
# main_full.py — проверка пароля
if hashlib.sha256(message.text.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
    admin_temp_access[user_id] = True
```
**Дополнительно:** Добавить `.env` переменную `ADMIN_PASSWORD_HASH`, убрать пароль из `config.py` и `.gitignore` для `config.py` с секретами.

---

### C2. XSS / HTML-инъекция через названия товаров

**Файл:** `main_full.py:362, 397, 400, 966, 1048, 1104, 1189`  
**Проблема:** Бот использует `ParseMode.HTML` глобально (строка 57). Названия товаров с Ozon и URL вставляются в HTML-сообщения **без экранирования**. Ozon может вернуть заголовок вида `Товар</b><script>alert('xss')</script><b>`, и он будет отрендерен как HTML в Telegram.

**Конкретные места:**
- Строка 362: `f"📦 <b>{title[:60]}</b>"`
- Строка 397: `f"{i}. 📦 {title_short}...\n"`  
- Строка 400: `f"🔗 <a href=\"{url}\">Открыть на Ozon</a>"`
- Строка 966: `f"{data['title'][:50]}"`
- Строка 1048: `f"📦 <b>{title[:60]}</b>"`
- Строка 1104: `f"{title[:50]}"`
- Строка 1189: `f"{title[:50]}"`

**Риск:** Инъекция произвольного HTML в сообщения бота. В Telegram это может привести к фишингу (поддельные ссылки), дезинформации, нарушению UI.

**Фикс:**
```python
import html as html_module

def escape_html(text: str) -> str:
    """Экранирует HTML-спецсимволы для безопасного вывода."""
    return html_module.escape(str(text), quote=True)

# Использовать везде, где вставляются пользовательские данные:
title_safe = escape_html(title[:60])
url_safe = escape_html(url)
text = f"📦 <b>{title_safe}</b>"
text += f"🔗 <a href=\"{url_safe}\">Открыть на Ozon</a>"
```

---

### C3. Отсутствие Rate Limiting (DoS)

**Файл:** `main_full.py:147-273, 978-1056`  
**Проблема:** Ноль ограничений на частоту запросов. Каждый парсинг запускает полноценный Chromium через CloakBrowser — это дорого по CPU/памяти. Злоумышленник может:
- Спамить ссылками — исчерпать память сервера (каждый браузер ~200-500MB RAM)
- Спамить `/parse` — положить сервер
- Отправлять сообщения в бесконечном цикле

**Риск:** DoS, исчерпание ресурсов, огромные счета за хостинг.

**Фикс:**
```python
import time
from collections import defaultdict

# Rate limiter: не более 5 парсингов в минуту на пользователя
rate_limits: dict[int, list[float]] = defaultdict(list)
RATE_LIMIT_MAX = 5       # запросов
RATE_LIMIT_WINDOW = 60   # секунд

def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    window = now - RATE_LIMIT_WINDOW
    rate_limits[user_id] = [t for t in rate_limits[user_id] if t > window]
    if len(rate_limits[user_id]) >= RATE_LIMIT_MAX:
        return False
    rate_limits[user_id].append(now)
    return True

# В handle_url и cmd_parse перед парсингом:
if not check_rate_limit(user_id):
    await message.answer("⏳ Слишком много запросов. Подожди минуту.")
    return
```

**Дополнительно:** Ограничить конкурентность браузеров через `asyncio.Semaphore(3)`.

---

### C4. Подделка платежей Telegram Stars (Payment Forgery)

**Файл:** `main_full.py:507-529`  
**Проблема:** Обработчик `successful_payment` **не проверяет**, что тариф в `payload` валиден:
```python
payload = message.successful_payment.invoice_payload
tariff = payload.split(":")[1] if ":" in payload else "premium"
```
Нет проверки `if tariff not in TARIFFS`. Можно подсунуть `payload=tariff:vip` и получить VIP бесплатно. Также нет проверки, что `total_amount` соответствует заявленному тарифу.

**Риск:** Бесплатное получение платных тарифов, обход оплаты.

**Фикс:**
```python
@dp.message(F.successful_payment)
async def success_payment_handler(message: Message):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    total_amount = message.successful_payment.total_amount
    
    # Разбираем payload
    parts = payload.split(":")
    if len(parts) != 2 or parts[0] != "tariff":
        await message.answer("❌ Некорректный платёж.")
        return
    
    tariff = parts[1]
    
    # ВАЛИДАЦИЯ: тариф должен существовать
    if tariff not in TARIFFS:
        logger.warning(f"Invalid tariff in payment: {tariff}")
        await message.answer("❌ Неизвестный тариф.")
        return
    
    # ВАЛИДАЦИЯ: проверяем сумму
    expected_amounts = {"premium": 100, "vip": 150}
    if tariff in expected_amounts and total_amount != expected_amounts[tariff]:
        logger.warning(f"Amount mismatch: got {total_amount}, expected {expected_amounts[tariff]}")
        await message.answer("❌ Неверная сумма платежа.")
        return
    
    # Валидация пройдена — применяем тариф
    import sqlite3, time
    conn = sqlite3.connect(DB_PATH)
    expires = int(time.time()) + 30 * 24 * 60 * 60
    conn.execute(
        "INSERT OR REPLACE INTO user_tariffs (user_id, tariff, expires_at) VALUES (?, ?, ?)",
        (user_id, tariff, expires)
    )
    conn.commit()
    conn.close()
    
    await message.answer(
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"Тариф <b>{TARIFFS[tariff]['name']}</b> активен на 30 дней.\n"
        f"Лимит: {TARIFFS[tariff]['max_products']} товаров.",
        reply_markup=main_keyboard
    )
```

---

### C5. SSRF через небезопасный парсинг URL

**Файл:** `main_full.py:310-329, 997-1018`  
**Проблема:** Regex `(?:https?://)?(?:www\.)?ozon\.ru[^\s]*` пропускает:
- `https://ozon.ru.evil.com/phishing` — содержит `ozon.ru` в поддомене
- `https://ozon.ru@evil.com` — `ozon.ru` трактуется как userinfo
- `https://evil.com/ozon.ru/product/...` — `ozon.ru` в пути

Браузер переходит по этим URL, а `requests.get` делает HTTP-запрос с `allow_redirects=True` — полноценный SSRF.

**Риск:** Фишинг, сканирование внутренней сети, атаки на внутренние сервисы.

**Фикс:**
```python
from urllib.parse import urlparse

def validate_ozon_url(raw_url: str) -> str | None:
    """Валидирует и нормализует URL Ozon. Возвращает None если невалидный."""
    if not raw_url.startswith("http"):
        raw_url = "https://" + raw_url
    
    try:
        parsed = urlparse(raw_url)
    except Exception:
        return None
    
    # Жёсткая проверка домена — только ozon.ru или www.ozon.ru
    hostname = parsed.hostname or ""
    if hostname not in ("ozon.ru", "www.ozon.ru"):
        # Проверяем, не окончание ли это (защита от ozon.ru.evil.com)
        if not hostname.endswith(".ozon.ru"):
            return None
    
    # Допустимые пути
    valid_paths = ["/product/", "/t/"]
    if not any(parsed.path.startswith(p) for p in valid_paths):
        return None
    
    # Собираем чистый URL
    clean_url = f"https://{hostname}{parsed.path}"
    return clean_url
```

---

## 🟠 HIGH (Срочный фикс)

### H1. Отсутствие brute-force защиты на админ-пароль

**Файл:** `main_full.py:595-615`  
**Проблема:** Нет ограничений на количество попыток ввода пароля. Нет задержки между попытками. Злоумышленник может перебирать пароль бесконечно.

**Фикс:**
```python
admin_login_attempts: dict[int, list[float]] = defaultdict(list)
MAX_ATTEMPTS = 5
ATTEMPT_WINDOW = 300  # 5 минут
ATTEMPT_COOLDOWN = 30  # пауза между попытками

@dp.message(lambda m: m.from_user.id in admin_pending_password)
async def handle_admin_password(message: Message):
    user_id = message.from_user.id
    now = time.time()
    
    # Очищаем старые попытки
    admin_login_attempts[user_id] = [
        t for t in admin_login_attempts[user_id] 
        if t > now - ATTEMPT_WINDOW
    ]
    
    # Блокировка за слишком много попыток
    if len(admin_login_attempts[user_id]) >= MAX_ATTEMPTS:
        del admin_pending_password[user_id]
        await message.answer("🚫 Слишком много попыток. Попробуй через 5 минут.")
        return
    
    admin_login_attempts[user_id].append(now)
    del admin_pending_password[user_id]
    
    if hashlib.sha256(message.text.encode()).hexdigest() == ADMIN_PASSWORD_HASH:
        admin_temp_access[user_id] = time.time() + 3600  # expires in 1 hour
        await message.answer("✅ <b>Пароль верный!</b>")
        # ... показываем админку
    else:
        await message.answer("❌ <b>Неверный пароль!</b>")
```

---

### H2. Отсутствие контроля конкурентности CloakBrowser

**Файл:** `main_full.py:147-273`  
**Проблема:** Каждый вызов `parse_ozon_price` запускает новый экземпляр Chromium. Нет семафора на количество одновременных браузеров. 10 одновременных парсингов = 10 браузеров × ~300MB = 3GB RAM. Сервер упадёт.

**Фикс:**
```python
# Глобальный семафор — не более 2 одновременных браузеров
browser_semaphore = asyncio.Semaphore(2)

async def parse_ozon_price(url: str) -> dict:
    async with browser_semaphore:
        try:
            from cloakbrowser import launch_async
            browser = await launch_async(headless=True)
            # ... остальной код
        finally:
            await browser.close()
```

---

### H3. Callback data injection — подмена цен

**Файл:** `main_full.py:352-358, 1037-1044, 1060-1096`  
**Проблема:** Callback-данные содержат цену и таргет в открытом виде: `track:{price}:{price*0.95}:percent:5`. Пользователь может вручную вызвать callback с `track:1:0.01:percent:100` и отслеживать несуществующую цену, или обойти лимиты тарифа.

**Фикс:**
```python
import json
import hashlib

# Сохраняем данные парсинга в БД/кеш с ID, в callback передаём только ID
# Не класть чувствительные данные в callback_data!

# Временное хранилище (в продакшене — Redis или БД)
parse_cache: dict[str, dict] = {}

async def handle_url(message: Message):
    # ... парсинг ...
    cache_id = hashlib.md5(f"{user_id}:{url}:{time.time()}".encode()).hexdigest()[:12]
    parse_cache[cache_id] = {
        "price": price, "title": title, "url": url, "user_id": user_id,
        "expires": time.time() + 600  # 10 минут
    }
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 -5%", callback_data=f"track:{cache_id}:5"),
         InlineKeyboardButton(text="📉 -10%", callback_data=f"track:{cache_id}:10"),
         # ...
    ])

@dp.callback_query(lambda c: c.data and c.data.startswith("track:"))
async def callback_track(callback: CallbackQuery):
    _, cache_id, percent = callback.data.split(":")
    cached = parse_cache.get(cache_id)
    if not cached or cached["user_id"] != callback.from_user.id:
        await callback.answer("❌ Сессия истекла. Спарси заново.")
        return
    
    target = cached["price"] * (1 - int(percent) / 100)
    success, msg = add_product(
        cached["user_id"], cached["url"], cached["title"],
        cached["price"], target, "percent", int(percent)
    )
    # ...
```

---

### H4. Broadcast — HTML-инъекция всем пользователям

**Файл:** `main_full.py:916-942`  
**Проблема:** Сообщение админа вставляется в HTML без экранирования: `f"📢 <b>Рассылка:</b>\n\n{text}"`. Если админ-аккаунт скомпрометирован, злоумышленник может разослать HTML-фишинг всем пользователям.

**Фикс:**
```python
async def do_broadcast(message: Message):
    text = message.text or ""
    text_safe = escape_html(text)
    
    # Запрещаем HTML-теги от админа — только plain text
    for (user_id,) in users:
        try:
            await bot.send_message(
                user_id,
                f"📢 <b>Рассылка:</b>\n\n{text_safe}",
                reply_markup=main_keyboard
            )
        except Exception as e:
            logger.error(f"Broadcast error to {user_id}: {e}")
    
    # Добавить задержку между сообщениями чтобы не улететь в rate limit
    await asyncio.sleep(0.05)  # ~20 msg/sec — безопасно для Telegram
```

---

### H5. `.env` с токеном в Docker-образе

**Файл:** `Dockerfile:23`, `docker-compose.yml:17`  
**Проблема:** `COPY .env .` копирует BOT_TOKEN внутрь Docker-образа. Если образ пушится в Docker Hub или любой registry — токен утекает. Плюс `.env` монтируется как volume поверх — но в слоях образа токен всё равно остаётся.

**Фикс:**
```dockerfile
# Dockerfile — УБРАТЬ COPY .env .
COPY main_full.py .
COPY config.py .
# .env монтируется через volume, не копируем в образ!
```
```yaml
# docker-compose.yml — добавить env_file вместо volume
services:
  bot:
    build: .
    env_file:
      - .env
    # .env не нужно монтировать как volume, он читается через env_file
```

---

## 🟡 MEDIUM (Исправить в ближайшее время)

### M1. Утечка данных в ошибках парсинга

**Файл:** `main_full.py:345, 1030`  
**Проблема:** `result.get('error', 'Неизвестная ошибка')` — `error` берётся из `str(e)` в `parse_ozon_price`. Исключение может содержать пути файловой системы, внутренние URL, stack trace.

**Фикс:**
```python
# В parse_ozon_price:
except Exception as e:
    logger.exception(f"[PARSE] Error for {url[:50]}: {e}")
    return {"success": False, "error": "Ошибка парсинга. Попробуй позже."}
# Пользователю — только общее сообщение, детали — в лог.
```

---

### M2. Предсказуемый admin_temp_access без TTL

**Файл:** `main_full.py:27, 602`  
**Проблема:** `admin_temp_access[user_id] = True` — сессия админа живёт вечно, пока бот не перезапустится. Нет таймаута. Если админ залогинился с чужого устройства, сессия останется навсегда.

**Фикс:**
```python
admin_temp_access: dict[int, float] = {}  # user_id -> expiry timestamp

# После успешного входа:
admin_temp_access[user_id] = time.time() + 3600  # 1 час

# При проверке:
if user_id in admin_temp_access and admin_temp_access[user_id] > time.time():
    # доступ разрешён
else:
    # сессия истекла — удаляем ключ
    admin_temp_access.pop(user_id, None)
```

---

### M3. requests.get на непроверенный URL

**Файл:** `main_full.py:319, 1008`  
**Проблема:** `requests.get(url, allow_redirects=True, timeout=10)` — HTTP-запрос на URL, который не прошёл полную валидацию. После редиректов URL может вести куда угодно.

**Фикс:** Использовать `validate_ozon_url()` (см. C5) перед `requests.get`. Также добавить `verify=True` и `headers={'User-Agent': 'OzonBot/1.0'}`.

---

### M4. Нет защиты от повторных платежей (replay)

**Файл:** `main_full.py:507-529`  
**Проблема:** Telegram теоретически гарантирует уникальность `successful_payment`, но нет проверки `telegram_payment_charge_id` на уникальность в БД. Если платёж обработается дважды (race condition), тариф продлится дважды.

**Фикс:**
```python
# Сохраняем telegram_payment_charge_id в БД и проверяем уникальность
charge_id = message.successful_payment.telegram_payment_charge_id

conn = sqlite3.connect(DB_PATH)
existing = conn.execute(
    "SELECT id FROM payment_log WHERE charge_id=?", (charge_id,)
).fetchone()
if existing:
    conn.close()
    return  # Уже обработан
```

---

### M5. SQLite без WAL-режима и бэкапов

**Файл:** `main_full.py:63-91`  
**Проблема:** База данных в стандартном режиме. При одновременных записях (парсинг + действия пользователя) возможны `database is locked`. Нет механизма бэкапов.

**Фикс:**
```python
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    # ... остальное
```

---

## 🟢 LOW (Рекомендации)

### L1. Жёстко заданные интервалы в фоновых задачах

**Файл:** `main_full.py:1156, 1205`  
**Проблема:** `await asyncio.sleep(3 * 60 * 60)` и `await asyncio.sleep(24 * 60 * 60)` — нельзя изменить без перезапуска.

**Фикс:** Вынести в `config.py`:
```python
PRICE_CHECK_INTERVAL = int(os.getenv("PRICE_CHECK_INTERVAL", "10800"))  # 3 часа
DAILY_REMINDER_INTERVAL = int(os.getenv("DAILY_REMINDER_INTERVAL", "86400"))
```

---

### L2. Нет graceful shutdown

**Файл:** `main_full.py:1230-1231`  
**Проблема:** При `SIGTERM` от Docker бот падает без закрытия браузеров и соединений с БД.

**Фикс:**
```python
import signal

async def shutdown():
    logger.info("Shutting down...")
    # Закрываем все открытые браузеры
    # Закрываем соединения с БД
    await dp.stop_polling()

async def main():
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
    # ...
```

---

### L3. Нет аудит-лога админских действий

**Файл:** `main_full.py:617-874`  
**Проблема:** Смена тарифов, рассылка, просмотр пользователей — нигде не логируется КТО и ЧТО сделал.

**Фикс:** Добавить логгирование всех админских действий с user_id и timestamp.

---

### L4. Нет валидации длины ввода

**Файл:** `main_full.py:978-1056`  
**Проблема:** URL, текст сообщений, названия товаров — нет ограничений по длине перед записью в БД. Можно записать URL на 10KB в SQLite.

**Фикс:**
```python
MAX_URL_LENGTH = 2048
MAX_TITLE_LENGTH = 500

if len(url) > MAX_URL_LENGTH:
    await message.answer("❌ Ссылка слишком длинная.")
    return
if len(title) > MAX_TITLE_LENGTH:
    title = title[:MAX_TITLE_LENGTH]
```

---

### L5. Токен в логах

**Файл:** `main_full.py:39-42`  
**Проблема:** `print("Нет BOT_TOKEN!")` — если по ошибке напечатать `print(BOT_TOKEN)`, токен утечёт в stdout/stderr который пишется в `bot_full.log` смонтированный на хост.

**Фикс:** Использовать `logger.error` вместо `print`. Никогда не логировать BOT_TOKEN.

---

## 🛡️ ОБЩИЕ РЕКОМЕНДАЦИИ

1. **Перевести все сообщения на `parse_mode=None`** и использовать `escape_html()` вручную только там, где реально нужен HTML. Глобальный `ParseMode.HTML` — это бомба.

2. **Добавить middleware для rate limiting** на уровне aiogram — throttling на все хендлеры.

3. **Выпилить все секреты из кода:** пароль, токен, ADMIN_IDS (или хотя бы в `.env`).

4. **Добавить `.gitignore`:**
   ```
   .env
   *.db
   bot_full.log
   ```

5. **Добавить `healthcheck` в Docker:**
   ```yaml
   healthcheck:
     test: ["CMD", "python", "-c", "import sqlite3; sqlite3.connect('/bot/prices.db').close()"]
     interval: 30s
   ```

6. **Обновить CloakBrowser** до актуальной версии — следить за CVE в Chromium.

7. **Включить Telegram-логирование** всех критических действий (админ-логин, смена тарифа, рассылка) в отдельный канал.

---

## 📝 CHECKLIST ИСПРАВЛЕНИЙ

- [ ] C1: Убрать пароль в открытом виде → хеш в `.env`
- [ ] C2: `escape_html()` на все пользовательские данные
- [ ] C3: Rate limiter + семафор на браузеры
- [ ] C4: Валидация `payload` и `total_amount` в платежах
- [ ] C5: `validate_ozon_url()` с проверкой домена
- [ ] H1: Brute-force защита на админ-пароль
- [ ] H2: `asyncio.Semaphore(2)` на CloakBrowser
- [ ] H3: Убрать чувствительные данные из callback_data
- [ ] H4: `escape_html()` в broadcast
- [ ] H5: Убрать `COPY .env .` из Dockerfile
- [ ] M1: Не показывать `str(e)` пользователю
- [ ] M2: TTL на `admin_temp_access`
- [ ] M3: Валидация URL перед `requests.get`
- [ ] M4: Проверка уникальности `charge_id`
- [ ] M5: WAL-режим для SQLite
- [ ] L1-L5: Конфигурируемые интервалы, graceful shutdown, аудит-лог, валидация длины, безопасное логирование

---

**Деби сказала — Деби сделала.** Бакуган в безопасности. 🔥