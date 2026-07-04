#!/usr/bin/env python3
"""Полноценный бот для мониторинга цен Ozon"""
import html as html_module
import asyncio
import hashlib
import os
import sys
import logging
import re
import sqlite3
import time
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse

# Setup
os.environ['PYTHONPATH'] = ''
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s,%(levelname)s %(message)s',
    handlers=[
        logging.FileHandler('bot_full.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Загружаем настройки из config.py — меняй там
from config import ADMIN_PASSWORD, ADMIN_IDS, TARIFFS
admin_temp_access = {}  # user_id: timestamp when access granted
admin_pending_password = {}
ADMIN_SESSION_TTL = 3600  # 1 час

def is_admin(user_id: int) -> bool:
    """Проверяет, имеет ли пользователь доступ к админке."""
    if user_id in ADMIN_IDS:
        return True
    if user_id in admin_temp_access:
        if time.time() - admin_temp_access[user_id] < ADMIN_SESSION_TTL:
            return True
        del admin_temp_access[user_id]  # Просрочен
    return False

# ─── Безопасность ────────────────────────────────────────────────

def escape_html(text: str) -> str:
    """Экранирует HTML-спецсимволы для безопасного вывода."""
    return html_module.escape(str(text or ""), quote=True)

def is_valid_ozon_url(url: str) -> bool:
    """Проверяет что URL действительно ведёт на ozon.ru."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        return host == "ozon.ru" or host.endswith(".ozon.ru")
    except Exception:
        return False

# Rate limiter: не более 5 парсингов в минуту на пользователя
_rate_limits: dict[int, list[float]] = defaultdict(list)

def check_rate_limit(user_id: int, max_calls: int = 5, window: int = 60) -> bool:
    now = time.time()
    window_start = now - window
    _rate_limits[user_id] = [t for t in _rate_limits.get(user_id, []) if t > window_start]
    if len(_rate_limits[user_id]) >= max_calls:
        return False
    _rate_limits[user_id].append(now)
    return True

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, ReplyKeyboardMarkup, KeyboardButton, LabeledPrice, PreCheckoutQuery
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("Нет BOT_TOKEN!")
    sys.exit(1)

DB_PATH = "prices.db"

# Главная клавиатура с важными кнопками
main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📋 Мои товары"), KeyboardButton(text="📊 Статистика")],
        [KeyboardButton(text="💰 Тариф")]
    ],
    resize_keyboard=True,
    one_time_keyboard=False
)

# Initialize bot
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
DB_PATH = "prices.db"

# ─── База данных ───────────────────────────────────────────────

# Глобальный семафор — не более 2 одновременных браузеров
browser_semaphore = asyncio.Semaphore(2)

def init_db():
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracked_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            sku TEXT,
            title TEXT,
            current_price REAL DEFAULT 0,
            target_price REAL DEFAULT 0,
            alert_type TEXT DEFAULT 'percent',
            alert_value REAL DEFAULT 10,
            last_price REAL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notified INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_id ON tracked_products(user_id)")
    # Таблица тарифов пользователей (для подписки через Telegram Stars)
    conn.execute("""CREATE TABLE IF NOT EXISTS user_tariffs (
        user_id INTEGER PRIMARY KEY,
        tariff TEXT DEFAULT 'free',
        expires_at TIMESTAMP
    )""")
    conn.commit()
    conn.close()

def add_product(user_id: int, url: str, title: str, price: float, target: float, alert_type: str = "percent", alert_value: float = 10):
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    safe_title = escape_html(str(title or "Товар с Ozon")[:100])
    safe_url = escape_html(str(url or ""))
    # Проверяем, не отслеживается ли уже
    existing = conn.execute("SELECT id FROM tracked_products WHERE user_id=? AND url=?", 
                           (user_id, url)).fetchone()
    if existing:
        conn.close()
        return False, "Этот товар уже отслеживается!"
    
    # Проверяем лимит по тарифу пользователя
    row = conn.execute("SELECT tariff FROM user_tariffs WHERE user_id=?", (user_id,)).fetchone()
    user_tariff = row[0] if row else "free"
    max_prods = TARIFFS[user_tariff]["max_products"]
    count = conn.execute("SELECT COUNT(*) FROM tracked_products WHERE user_id=?", (user_id,)).fetchone()[0]
    if count >= max_prods:
        conn.close()
        return False, f"Достигнут лимит: максимум {max_prods} товаров. Купи подписку: /buy"
    
    conn.execute("""
        INSERT INTO tracked_products (user_id, url, title, current_price, target_price, alert_type, alert_value, last_price, last_check, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
    """, (user_id, url, title, price, target, alert_type, alert_value, price))
    conn.commit()
    conn.close()
    return True, "Товар добавлен в отслеживание!"

def remove_product(user_id: int, product_id: int):
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("DELETE FROM tracked_products WHERE id=? AND user_id=?", 
                       (product_id, user_id))
    conn.commit()
    removed = cur.rowcount > 0
    conn.close()
    return removed

def get_user_products(user_id: int):
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM tracked_products WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    return rows

def get_all_stats():
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    total = conn.execute("SELECT COUNT(*) FROM tracked_products").fetchone()[0]
    users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM tracked_products").fetchone()[0]
    conn.close()
    return total, users

def get_user_savings(user_id: int) -> dict:
    """Считает экономию пользователя по всем товарам"""
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT title, last_price, current_price FROM tracked_products WHERE user_id=? AND current_price < last_price",
        (user_id,)
    ).fetchall()
    conn.close()
    total_saved = 0.0
    details = []
    for r in rows:
        saved = float(r[1]) - float(r[2])
        total_saved += saved
        details.append({"title": r[0], "saved": saved})
    return {"total": round(total_saved, 2), "count": len(details), "details": details}

# ─── Парсинг ───────────────────────────────────────────────────

async def parse_ozon_price(url: str) -> dict:
    """Парсит цену через CloakBrowser"""
    logger.info(f"[PARSE] Starting parse: {url}")
    
    try:
        from cloakbrowser import launch_async
        async with browser_semaphore:
            browser = await launch_async(headless=True)
            page = await browser.new_page()
            
            # Навигация — ждём полной загрузки
            await page.goto(url, wait_until="networkidle", timeout=45000)
            await asyncio.sleep(2)
            
            # Ищем цену — несколько способов
            price = None
            title = None
            page_content = None
            
            # Способ 1: webPrice
            try:
                await page.wait_for_selector('[data-widget="webPrice"]', timeout=8000)
                el = await page.query_selector('[data-widget="webPrice"] span')
                if el:
                    text = await el.text_content() or ""
                    nums = re.findall(r'[\d]+', text.replace(' ', '').replace('\u2009', ''))
                    if nums:
                        price = float(''.join(nums))
                        logger.info(f"[PARSE] Method 1 (webPrice): {price}")
            except Exception as e:
                logger.warning(f"[PARSE] Method 1 failed: {e}")
            
            # Способ 2: price-классы
            if not price:
                try:
                    for sel in ['[class*="price"]', '[class*="Price"]', '[class*="cost"]', '[class*="Cost"]']:
                        try:
                            el = await page.query_selector(sel)
                            if el:
                                text = await el.text_content() or ""
                                if '₽' in text:
                                    nums = re.findall(r'[\d]+', text.replace(' ', '').replace('\u2009', ''))
                                    if nums:
                                        price = float(''.join(nums))
                                        logger.info(f"[PARSE] Method 2 (class*price): {price}")
                                        break
                        except:
                            continue
                except Exception as e:
                    logger.warning(f"[PARSE] Method 2 failed: {e}")
            
            # Способ 3: любой span/div с ₽
            if not price:
                try:
                    els = await page.query_selector_all('span, div, b, strong')
                    for el in els:
                        try:
                            text = await el.text_content() or ""
                            if '₽' in text and '→' not in text and 'до' not in text.lower() and 'от' not in text.lower():
                                nums = re.findall(r'[\d]+', text.replace(' ', '').replace('\u2009', ''))
                                if nums:
                                    candidate = float(nums[-1])
                                    if 10 < candidate < 500000:
                                        price = candidate
                                        logger.info(f"[PARSE] Method 3 (₽ span): {price}")
                                        break
                        except:
                            continue
                except Exception as e:
                    logger.warning(f"[PARSE] Method 3 failed: {e}")
            
            # Способ 4: HTML regex
            if not price:
                try:
                    page_content = await page.content()
                    for pattern in [
                        r'"price"\s*:\s*(\d+\.?\d*)',
                        r'"priceValue"\s*:\s*(\d+\.?\d*)',
                        r'"priceAmount"\s*:\s*(\d+\.?\d*)',
                        r'₽</span>\s*</span>\s*<span[^>]*>\s*(\d+[\s\d]*\d+)',
                    ]:
                        match = re.search(pattern, page_content)
                        if match:
                            raw = match.group(1).replace(' ', '').replace('\u2009', '')
                            try:
                                p = float(raw)
                                if 10 < p < 500000:
                                    price = p
                                    logger.info(f"[PARSE] Method 4 (regex HTML): {price}")
                                    break
                            except:
                                continue
                except Exception as e:
                    logger.warning(f"[PARSE] Method 4 failed: {e}")
            
            # Название товара
            try:
                title_el = await page.query_selector('[data-widget="webProductHeading"] h1')
                if not title_el:
                    title_el = await page.query_selector('h1')
                if title_el:
                    title = await title_el.text_content()
                    title = title.strip() if title else "Товар с Ozon"
                else:
                    if not title and page_content:
                        match = re.search(r'<h1[^>]*>([^<]+)', page_content)
                        if match:
                            title = match.group(1).strip()
            except:
                title = "Товар с Ozon"
            
            await browser.close()
        
        if price and price > 0:
            logger.info(f"[PARSE] Success: price={price}, title={title[:40] if title else None}")
            return {"price": price, "title": title, "success": True}
        else:
            logger.warning(f"[PARSE] Price not found or invalid: price={price}")
            return {"success": False, "error": "Цена не найдена на странице"}
            
    except Exception as e:
        logger.exception(f"[PARSE] Error: {e}")
        return {"success": False, "error": str(e)}

# ─── Команды ───────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Привет! Я бот для отслеживания цен на Ozon.</b>\n\n"
        "Вот что я умею:\n"
        "• /parse [ссылка] — спарсить товар\n"
        "• /prices — показать отслеживаемые товары\n"
        "• /stats — статистика\n"
        "• /buy — купить подписку\n"
        "• /help — помощь\n\n"
        "Просто скинь ссылку на товар с Ozon — я его спаршу и покажу карточку!",
        reply_markup=main_keyboard
    )

@dp.message(Command("parse"))
async def cmd_parse(message: Message):
    """Парсит товар по ссылке из аргумента команды"""
    text = message.text or ""
    parts = text.split(maxsplit=1)
    
    if len(parts) < 2:
        await message.answer(
            "❌ Укажи ссылку на товар!\n\n"
            "Пример: /parse https://ozon.ru/product/...\n"
            "Или: /parse https://ozon.ru/t/...",
            reply_markup=main_keyboard
        )
        return
    
    url_text = parts[1]
    
    # Извлекаем URL
    url = None
    if "ozon.ru" in url_text.lower():
        match = re.search(r'(?:https?://)?(?:www\.)?ozon\.ru[^\s]*', url_text)
        if match:
            url = match.group(0)
            if not url.startswith('http'):
                url = 'https://' + url
            # Разрешаем короткие ссылки ozon.ru/t/
            if '/t/' in url:
                try:
                    import requests
                    resp = requests.get(url, allow_redirects=True, timeout=10)
                    final = resp.url
                    clean = re.search(r'https://www\.ozon\.ru/product/[^?]+', final)
                    if clean:
                        url = clean.group(0)
                        logger.info(f"[PARSE CMD] Short link resolved: {url}")
                except Exception as e:
                    logger.warning(f"[PARSE CMD] Failed to resolve short link: {e}")
            else:
                url = re.sub(r'\?.*', '', url)
    
    if not url:
        await message.answer(
            "❌ Не вижу ссылку на Ozon. Отправь ссылку вида:\n"
            "https://ozon.ru/product/...\n"
            "или ozon.ru/t/...",
            reply_markup=main_keyboard
        )
        return
    
    # SSRF + Rate limit
    if not is_valid_ozon_url(url):
        await message.answer("❌ Разрешены только ссылки на ozon.ru", reply_markup=main_keyboard)
        return
    if not check_rate_limit(message.from_user.id):
        await message.answer("⏳ Слишком много запросов! Подожди минуту.", reply_markup=main_keyboard)
        return
    
    # Парсим как обычную ссылку
    status_msg = await message.answer("⏳ Парсинг товара...")
    result = await parse_ozon_price(url)
    
    if not result.get("success"):
        await status_msg.edit_text(f"❌ Ошибка парсинга: {result.get('error', 'Неизвестная ошибка')}")
        return
    
    price = result["price"]
    title = escape_html(result["title"] or "Товар с Ozon")
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 -5%", callback_data=f"track:{price}:{price*0.95}:percent:5"),
         InlineKeyboardButton(text="📉 -10%", callback_data=f"track:{price}:{price*0.90}:percent:10"),
         InlineKeyboardButton(text="📉 -15%", callback_data=f"track:{price}:{price*0.85}:percent:15")],
        [InlineKeyboardButton(text="📉 -20%", callback_data=f"track:{price}:{price*0.80}:percent:20"),
         InlineKeyboardButton(text="📉 -25%", callback_data=f"track:{price}:{price*0.75}:percent:25"),
         InlineKeyboardButton(text="📉 -30%", callback_data=f"track:{price}:{price*0.70}:percent:30")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])
    
    await status_msg.edit_text(
        f"📦 <b>{title[:60]}</b>\n\n"
        f"💰 <b>Текущая цена: {price:.0f} ₽</b>\n\n"
        f"🔗 <a href=\"{url}\">Открыть на Ozon</a>\n\n"
        f"Выбери процент или введи сумму в рублях (например: 300)\n"
        f"Бот отследит падение до {price:.0f} - [твоя сумма] ₽",
        reply_markup=keyboard
    )
    
    pending_price_input[message.from_user.id] = {"price": price, "title": title, "url": url}

# Обработка кнопок "Мои товары" и "Статистика"
@dp.message(lambda m: m.text == "📋 Мои товары")
@dp.message(Command("prices"))
async def cmd_prices(message: Message):
    """Показывает все отслеживаемые товары."""
    logger.info(f"[PRICES] called by {message.from_user.id}")
    
    rows = get_user_products(message.from_user.id)
    logger.info(f"[PRICES] found {len(rows)} rows")
    
    if not rows:
        await message.answer(
            "📋 У тебя пока нет отслеживаемых товаров!\n\n"
            "Скинь ссылку на товар с Ozon — я начну отслеживать 🎯",
            reply_markup=main_keyboard
        )
        return
    
    text = "📋 <b>Отслеживаемые товары</b>:\n\n"
    keyboard = []
    for i, r in enumerate(rows, 1):
        title = r[4] or "Товар"  # title в колонке 4
        url = r[2] or ""  # url в колонке 2
        price = float(r[5]) if r[5] else 0  # current_price в колонке 5
        target = float(r[6]) if r[6] else 0  # target_price в колонке 6
        initial = float(r[9]) if r[9] else price  # last_price (оригинал) в колонке 9
        saved = initial - price if price < initial else 0
        title_short = str(title)[:40]
        text += f"{i}. 📦 {title_short}...\n"
        text += f"   💰 {price:.0f} ₽ → 🎯 {target:.0f} ₽\n"
        if saved > 0:
            text += f"   💚 <b>−{saved:.0f} ₽</b> от начальной\n"
        if url:
            text += f"   🔗 <a href=\"{url}\">Открыть на Ozon</a>\n"
        text += "\n"
        # Кнопка удаления + смена цели
        keyboard.append([
            InlineKeyboardButton(text=f"❌ Удалить {i}", callback_data=f"untrack:{r[0]}"),
            InlineKeyboardButton(text=f"🎯 Цель {i}", callback_data=f"change_target:{r[0]}:{target:.0f}")
        ])

    # Кнопка обновить все цены
    keyboard.append([InlineKeyboardButton(text="🔄 Обновить все цены", callback_data="refresh_prices")])

    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.message(lambda m: m.text == "📊 Статистика")
@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    total, users = get_all_stats()
    savings = get_user_savings(message.from_user.id)

    text = f"📊 <b>Статистика</b>\n\n"
    text += f"👥 Всего пользователей: {users}\n"
    text += f"📦 Всего товаров: {total}\n"
    text += f"─────────────────────\n"

    rows = get_user_products(message.from_user.id)
    text += f"📋 Твои товары: {len(rows)}\n"

    if savings['total'] > 0:
        text += f"💰 <b>Экономия: {savings['total']:.0f} ₽</b> 🔥\n"
        text += f"📉 Подешевело: {savings['count']} товаров\n"
    else:
        text += f"💰 Экономия: 0 ₽\n"
        text += f"💡 Цены пока не падали — следи за /prices\n"

    await message.answer(text, reply_markup=main_keyboard)

@dp.message(lambda m: m.text == "💰 Тариф")
@dp.message(Command("tariff"))
async def cmd_tariff(message: Message):
    """Показывает выбор тарифа"""
    user_id = message.from_user.id
    
    # Получаем тариф из БД
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT tariff FROM user_tariffs WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    current = row[0] if row else "free"
    
    text = f"""💰 <b>Выбор тарифа</b>

━━━━━━━━━━━━━━━━━━━━━━━━━

📦 <b>Текущий тариф: {TARIFFS[current]['name']}</b>

• Лимит товаров: {TARIFFS[current]['max_products']}
• Проверка цен: раз в {TARIFFS[current]['check_interval']//60} минут

━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Доступные тарифы:</b>

⭐ <b>Премиум</b> — 100 Stars (~199₽/мес)
• До 20 товаров
• Проверка: раз в 30 минут

👑 <b>VIP</b> — 150 Stars (~299₽/мес)
• До 100 товаров
• Проверка: раз в 10 минут
• Приоритетная поддержка

━━━━━━━━━━━━━━━━━━━━━━━━━

💡 <i>Оплата через Telegram Stars. Купить: /buy</i>"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Купить Премиум", callback_data="buy:premium"),
         InlineKeyboardButton(text="👑 Купить VIP", callback_data="buy:vip")],
        [InlineKeyboardButton(text="📦 Бесплатный (текущий)", callback_data="tariff:free")]
    ])

    await message.answer(text, reply_markup=keyboard)

# ─── Платежи (Telegram Stars) ──────────────────────────────────

@dp.message(Command("buy"))
async def cmd_buy(message: Message):
    """Покупка подписки"""
    text = "⭐ <b>Купить подписку</b>\n\nВыбери тариф:"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Премиум — 50 Stars", callback_data="buy:premium")],
        [InlineKeyboardButton(text="👑 VIP — 150 Stars", callback_data="buy:vip")],
    ])
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data and c.data.startswith("buy:"))
async def callback_buy(callback: CallbackQuery):
    """Обработка кнопки покупки"""
    await callback.answer()
    tariff = callback.data.split(":")[1]
    
    prices_map = {
        "premium": [LabeledPrice(label="Премиум на 30 дней", amount=100)],
        "vip": [LabeledPrice(label="VIP на 30 дней", amount=150)]
    }
    
    if tariff not in prices_map:
        await callback.message.edit_text("❌ Неизвестный тариф")
        return
    
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=f"{TARIFFS[tariff]['name']} на месяц",
        description=f"• {TARIFFS[tariff]['max_products']} товаров\n• Проверка раз в {TARIFFS[tariff]['check_interval']//60} минут\n• На 30 дней",
        payload=f"tariff:{tariff}",
        provider_token="",  # Пустой для Telegram Stars
        currency="XTR",
        prices=prices_map[tariff]
    )

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_q: PreCheckoutQuery):
    """Подтверждаем платеж"""
    await pre_checkout_q.answer(ok=True)

@dp.message(F.successful_payment)
async def success_payment_handler(message: Message):
    """Обработка успешного платежа с валидацией"""
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    total_amount = message.successful_payment.total_amount
    currency = message.successful_payment.currency

    # Валидация payload
    parts = payload.split(":")
    if len(parts) != 2 or parts[0] != "tariff":
        logger.warning(f"[PAYMENT] Invalid payload from user {user_id}: {payload}")
        await message.answer("❌ Некорректный платёж. Обратись в поддержку.")
        return

    tariff = parts[1]
    if tariff not in TARIFFS:
        logger.warning(f"[PAYMENT] Invalid tariff from user {user_id}: {tariff}")
        await message.answer("❌ Неизвестный тариф.")
        return

    # Валидация суммы (защита от подделки)
    expected_amounts = {"premium": 100, "vip": 150}
    if tariff in expected_amounts and total_amount != expected_amounts[tariff]:
        logger.warning(f"[PAYMENT] Amount mismatch for user {user_id}: "
                       f"got {total_amount} {currency}, expected {expected_amounts[tariff]} XTR")
        await message.answer("❌ Неверная сумма платежа. Обратись в поддержку.")
        return

    import sqlite3
    import time
    conn = sqlite3.connect(DB_PATH)
    expires = int(time.time()) + 30 * 24 * 60 * 60  # +30 дней
    conn.execute("""INSERT OR REPLACE INTO user_tariffs (user_id, tariff, expires_at)
                    VALUES (?, ?, ?)""", (user_id, tariff, expires))
    conn.commit()
    conn.close()

    await message.answer(
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"Тариф <b>{TARIFFS[tariff]['name']}</b> активен на 30 дней.\n"
        f"Лимит: {TARIFFS[tariff]['max_products']} товаров.\n\n"
        f"Добавляй товары через /prices!",
        reply_markup=main_keyboard
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>Справка по командам:</b>\n\n"
        "• /start — начать работу\n"
        "• /prices — показать товары\n"
        "• /stats — статистика\n"
        "• /export — экспорт товаров\n"
        "• /untrack [номер] — удалить товар\n\n"
        "Просто скинь ссылку на товар Ozon!",
        reply_markup=main_keyboard
    )

@dp.message(Command("export"))
async def cmd_export(message: Message):
    """Экспорт списка товаров"""
    user_id = message.from_user.id
    rows = get_user_products(user_id)
    
    if not rows:
        await message.answer("📋 Нет товаров для экспорта!", reply_markup=main_keyboard)
        return
    
    text = "📤 <b>Список отслеживаемых товаров:</b>\n\n"
    for i, r in enumerate(rows, 1):
        title = r[4] or "Товар"
        price = float(r[5]) if r[5] else 0
        target = float(r[6]) if r[6] else 0
        url = r[2] or ""
        text += f"{i}. {title[:50]}\n"
        text += f"   Цена: {price:.0f}₽ → Цель: {target:.0f}₽\n"
        text += f"   {url}\n\n"
    
    await message.answer(text, reply_markup=main_keyboard)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    """Админ-панель"""
    user_id = message.from_user.id
    
    if not is_admin(user_id):
        # Просим пароль
        await message.answer(
            "🔑 <b>Введи пароль админа</b>\n\n"
            "Отправь пароль одним сообщением:"
        )
        # Запоминаем что юзер в режиме ввода пароля
        admin_pending_password[user_id] = True
        return
    
    text = "⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\n"
    text += "Выбери раздел:"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="💰 Тарифы", callback_data="admin:tariffs"),
         InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:settings")],
    ])
    
    await message.answer(text, reply_markup=keyboard)

# Защита от перебора пароля админки
admin_login_attempts: dict[int, list[float]] = defaultdict(list)
MAX_ADMIN_ATTEMPTS = 5
ADMIN_ATTEMPT_WINDOW = 300  # 5 минут

# Обработка ввода пароля для доступа к админке
@dp.message(lambda m: m.from_user.id in admin_pending_password)
async def handle_admin_password(message: Message):
    """Проверяет пароль админа с защитой от перебора"""
    user_id = message.from_user.id
    now = time.time()

    # Очищаем старые попытки
    admin_login_attempts[user_id] = [
        t for t in admin_login_attempts[user_id]
        if t > now - ADMIN_ATTEMPT_WINDOW
    ]

    # Блокировка при превышении лимита
    if len(admin_login_attempts[user_id]) >= MAX_ADMIN_ATTEMPTS:
        del admin_pending_password[user_id]
        remaining = int(ADMIN_ATTEMPT_WINDOW - (now - admin_login_attempts[user_id][0]))
        await message.answer(
            f"🚫 <b>Слишком много попыток.</b>\n"
            f"Попробуй снова через {remaining // 60} мин."
        )
        return

    admin_login_attempts[user_id].append(now)
    del admin_pending_password[user_id]  # Чистим стейт

    if hashlib.sha256((message.text or "").encode()).hexdigest() == hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest():
        admin_temp_access[user_id] = time.time()
        await message.answer("✅ <b>Пароль верный!</b> Добро пожаловать в админ-панель!")
        # Показываем админку
        text = "⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел:"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"),
             InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="💰 Тарифы", callback_data="admin:tariffs"),
             InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:settings")],
        ])
        await message.answer(text, reply_markup=keyboard)
    else:
        await message.answer("❌ <b>Неверный пароль!</b> Попробуй ещё раз: /admin")

@dp.callback_query(lambda c: c.data and c.data.startswith("admin:"))
async def admin_callback(callback: CallbackQuery):
    """Обработка callback админки"""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён!")
        return
    
    data = callback.data
    
    if data == "admin:users":
        # Список пользователей
        conn = sqlite3.connect(DB_PATH)
        users = conn.execute("""
            SELECT user_id, COUNT(*) as cnt, MAX(created_at) 
            FROM tracked_products GROUP BY user_id
        """).fetchall()
        conn.close()
        
        text = "👥 <b>Пользователи:</b>\n\n"
        for u in users[:20]:  # Первые 20
            text += f"ID: {u[0]} — {u[1]} товаров\n"
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]
        ]))
    
    elif data == "admin:stats":
        # Общая статистика
        conn = sqlite3.connect(DB_PATH)
        total_products = conn.execute("SELECT COUNT(*) FROM tracked_products").fetchone()[0]
        total_users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM tracked_products").fetchone()[0]
        conn.close()
        
        text = f"""📊 <b>Статистика:</b>

Всего товаров: {total_products}
Всего пользователей: {total_users}

Тарифы:
• Бесплатный: до 3 товаров
• Премиум: до 20 товаров
• VIP: до 100 товаров"""
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]
        ]))
    
    elif data == "admin:tariffs":
        text = """💰 <b>Тарифы:</b>

<b>Бесплатный</b> — 0₽
• До 3 товаров
• Проверка раз в 3 часа

<b>Премиум</b> — 99₽/мес
• До 20 товаров
• Проверка раз в 30 минут

<b>VIP</b> — 299₽/мес
• До 100 товаров
• Проверка раз в 10 минут"""

        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]
        ]))
    
    elif data == "admin:settings":
        text = """⚙️ <b>Настройки:</b>

Интервал проверки цен: 3 часа
Время напоминания: 09:00
Твой ID: {}""".format(callback.from_user.id)
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]
        ]))
    
    elif data.startswith("admin:settariff:"):
        # Установить тариф пользователю
        target_user = int(data.split(":")[2])
        
        text = f"💳 <b>Установить тариф для User {target_user}</b>\n\n"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Бесплатный", callback_data=f"admin:set:{target_user}:free")],
            [InlineKeyboardButton(text="⭐ Премиум", callback_data=f"admin:set:{target_user}:premium")],
            [InlineKeyboardButton(text="👑 VIP", callback_data=f"admin:set:{target_user}:vip")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:tariffs")],
        ])
        
        await callback.message.edit_text(text, reply_markup=keyboard)
    
    elif data.startswith("admin:set:"):
        # Подтверждение смены тарифа
        parts = data.split(":")
        target_user = int(parts[1])
        new_tariff = parts[2]
        
        text = f"✅ <b>Тариф изменён!</b>\n\n"
        text += f"User: {target_user}\n"
        text += f"Новый тариф: {TARIFFS[new_tariff]['name']}\n"
        text += f"Лимит товаров: {TARIFFS[new_tariff]['max_products']}\n"
        text += f"Интервал проверки: {TARIFFS[new_tariff]['check_interval']//60} мин"
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 К пользователям", callback_data="admin:tariffs")]
        ]))
        
        # Уведомляем пользователя
        try:
            await bot.send_message(
                target_user,
                f"🎉 <b>Тариф обновлён!</b>\n\n"
                f"Новый тариф: {TARIFFS[new_tariff]['name']}\n"
                f"Доступно товаров: {TARIFFS[new_tariff]['max_products']}",
                reply_markup=main_keyboard
            )
        except:
            pass
    
    # Обработка выбора тарифа пользователем
    elif data.startswith("tariff:"):
        tariff = data.split(":")[1]
        
        if tariff == "back":
            # Возврат к списку тарифов
            current = "free"
            text = f"""💰 <b>Выбор тарифа</b>

━━━━━━━━━━━━━━━━━━━━━━━━━

📦 <b>Текущий тариф: {TARIFFS[current]['name']}</b>

• Лимит товаров: {TARIFFS[current]['max_products']}
• Проверка цен: раз в {TARIFFS[current]['check_interval']//60} минут

━━━━━━━━━━━━━━━━━━━━━━━━━

<b>Доступные тарифы:</b>

⭐ <b>Премиум</b> — 99₽/мес
• До 20 товаров
• Проверка: раз в 30 минут

👑 <b>VIP</b> — 299₽/мес
• До 100 товаров
• Проверка: раз в 10 минут

━━━━━━━━━━━━━━━━━━━━━━━━━

💡 <i>Для смены тарифа нажми кнопку ниже</i>"""

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Бесплатный (текущий)", callback_data="tariff:free")],
                [InlineKeyboardButton(text="⭐ Премиум (99₽/мес)", callback_data="tariff:premium")],
                [InlineKeyboardButton(text="👑 VIP (299₽/мес)", callback_data="tariff:vip")],
            ])
            
            await callback.message.edit_text(text, reply_markup=keyboard)
            await callback.answer()
            return
        
        if tariff == "free":
            text = "📦 <b>Бесплатный тариф</b>\n\n"
            text += "• Лимит товаров: 3\n"
            text += "• Проверка цен: раз в 3 часа (180 мин)\n"
            text += "• Напоминания: 1 раз в день\n\n"
            text += "💡 <i>Это твой текущий тариф!</i>"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад к тарифам", callback_data="tariff:back")]
            ])
        elif tariff == "premium":
            text = "⭐ <b>Премиум тариф</b>\n\n"
            text += "• Лимит товаров: 20\n"
            text += "• Проверка цен: раз в 30 минут\n"
            text += "• Напоминания: каждые 4 часа\n"
            text += "• Приоритетная поддержка\n\n"
            text += "💰 <i>Стоимость: 99₽/мес</i>"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⭐ Подключить Премиум", callback_data="tariff:buy:premium")],
                [InlineKeyboardButton(text="🔙 Назад к тарифам", callback_data="tariff:back")]
            ])
        elif tariff == "vip":
            text = "👑 <b>VIP тариф</b>\n\n"
            text += "• Лимит товаров: 100\n"
            text += "• Проверка цен: раз в 10 минут\n"
            text += "• Напоминания: каждые 2 часа\n"
            text += "• Приоритетная поддержка\n"
            text += "• Ранний доступ к функциям\n\n"
            text += "💰 <i>Стоимость: 299₽/мес</i>"
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👑 Подключить VIP", callback_data="tariff:buy:vip")],
                [InlineKeyboardButton(text="🔙 Назад к тарифам", callback_data="tariff:back")]
            ])
        
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
    
    # Обработка нажатия "Подключить"
    elif data.startswith("tariff:buy:"):
        tariff = data.split(":")[2]
        
        if tariff == "premium":
            text = """⭐ <b>Премиум тариф</b>

Стоимость: 99₽/мес

Для оплаты свяжись с администратором — нажми кнопку ниже"""

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Написать администратору", url="https://t.me/DealBot63_bot")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="tariff:back")]
            ])
        elif tariff == "vip":
            text = """👑 <b>VIP тариф</b>

Стоимость: 299₽/мес

Для оплаты свяжись с администратором — нажми кнопку ниже"""

            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📱 Написать администратору", url="https://t.me/DealBot63_bot")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="tariff:back")]
            ])
        
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
    
    elif data == "admin:broadcast":
        text = """📢 <b>Рассылка</b>

Напиши сообщение для всех пользователей.

Внимание: сообщение будет отправлено ВСЕМ пользователям бота!"""
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]
        ]))
        
        # Временно сохраняем состояние ожидания сообщения
        pending_broadcast[callback.from_user.id] = True
        await callback.answer("Напиши сообщение для рассылки")
    
    elif data == "admin:back":
        text = "⚙️ <b>АДМИН-ПАНЕЛЬ</b>\n\nВыбери раздел:"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"),
             InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="💰 Тарифы", callback_data="admin:tariffs"),
             InlineKeyboardButton(text="📢 Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="admin:settings")],
        ])
        await callback.message.edit_text(text, reply_markup=keyboard)
    
    await callback.answer()

@dp.message(Command("untrack"))
async def cmd_untrack(message: Message):
    """Удаляет товар по номеру"""
    user_id = message.from_user.id
    text = message.text or ""
    parts = text.split()
    
    if len(parts) < 2:
        await message.answer(
            "📝 Использование: /untrack [номер]\n\n"
            "Сначала /prices чтобы увидеть номера",
            reply_markup=main_keyboard
        )
        return
    
    try:
        idx = int(parts[1])
    except ValueError:
        await message.answer("Номер должен быть числом!")
        return
    
    rows = get_user_products(user_id)
    if idx < 1 or idx > len(rows):
        await message.answer("Нет такого номера. Напиши /prices")
        return
    
    product = rows[idx - 1]
    removed = remove_product(user_id, product[0])
    
    if removed:
        await message.answer(f"✅ Товар #{idx} удалён из отслеживания")
    else:
        await message.answer("❌ Не удалось удалить")

# ─── Обработка URL ────────────────────────────────────────────

# Хранилище ожидающих ввода пользователей
pending_price_input = {}  # user_id: {"price": float, "title": str, "url": str}
pending_broadcast = {}  # user_id: True — админ в режиме рассылки

async def do_broadcast(message: Message):
    """Рассылает сообщение всем пользователям"""
    text = message.text or ""
    
    if not text:
        await message.answer("❌ Пустое сообщение!", reply_markup=main_keyboard)
        return
    
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT DISTINCT user_id FROM tracked_products").fetchall()
    conn.close()
    
    sent = 0
    failed = 0
    
    for (user_id,) in users:
        try:
            await bot.send_message(user_id, f"📢 <b>Рассылка:</b>\n\n{text}", reply_markup=main_keyboard)
            sent += 1
        except Exception as e:
            logger.error(f"Broadcast error to {user_id}: {e}")
            failed += 1
    
    await message.answer(
        f"✅ Рассылка завершена!\n\nОтправлено: {sent}\nОшибок: {failed}",
        reply_markup=main_keyboard
    )

def is_number_text(text: str) -> bool:
    """Проверяет, что текст - только цифры"""
    return text.strip().isdigit()

@dp.message(F.text.func(is_number_text))
async def handle_price_input(message: Message):
    """Обрабатывает ввод суммы в рублях"""
    user_id = message.from_user.id
    
    if user_id not in pending_price_input:
        return  # Не ждём ввод от этого пользователя
    
    try:
        rub = int(message.text)
        data = pending_price_input[user_id]
        price = data["price"]
        target = price - rub  # Падение на указанную сумму
        
        success, msg = add_product(user_id, data["url"], data["title"], price, target, "rub", rub)
        
        if success:
            await message.answer(
                f"✅ <b>Товар добавлен!</b>\n\n"
                f"{data['title'][:50]}\n"
                f"💰 {price:.0f} ₽ → 🎯 {target:.0f} ₽ (-{rub}₽)"
            )
        else:
            await message.answer(f"⚠️ {msg}")
        
        del pending_price_input[user_id]
    except Exception as e:
        logger.error(f"[handle_price_input] Error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз или выбери процент.")

# ─── Обработка ввода новой цели ──────────────────────────────────
@dp.message(F.text)
async def handle_target_change_input(message: Message):
    """Обрабатывает ввод новой цели для товара"""
    user_id = message.from_user.id
    if user_id not in pending_target_change:
        return  # Не ждём — пробрасываем дальше

    text = message.text.strip()
    data = pending_target_change[user_id]
    product_id = data["product_id"]
    title = data["title"]
    current_price = data["current_price"]

    try:
        if text.endswith("%"):
            # Процент
            percent = float(text.rstrip("%"))
            new_target = current_price * (1 - percent / 100)
            alert_type = "percent"
            alert_value = percent
        else:
            # Сумма в рублях
            rub = float(text)
            new_target = current_price - rub
            alert_type = "rub"
            alert_value = rub

        new_target = round(max(new_target, 1), 2)  # Минимум 1₽

        # Обновляем в БД
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "UPDATE tracked_products SET target_price=?, alert_type=?, alert_value=? WHERE id=? AND user_id=?",
            (new_target, alert_type, alert_value, product_id, user_id)
        )
        conn.commit()
        conn.close()

        del pending_target_change[user_id]

        await message.answer(
            f"✅ <b>Цель обновлена!</b>\n\n"
            f"📦 {title[:50]}\n"
            f"💰 {current_price:.0f} ₽ → 🎯 {new_target:.0f} ₽\n\n"
            f"Бот уведомит когда цена упадёт! 🔔",
            reply_markup=main_keyboard
        )
    except ValueError:
        await message.answer(
            "❌ Не понял. Напиши сумму в рублях (50000) или процент со знаком % (15%)",
        )
    except Exception as e:
        logger.error(f"[handle_target_change] Error: {e}")
        await message.answer("❌ Ошибка. Попробуй ещё раз.")
        if user_id in pending_target_change:
            del pending_target_change[user_id]

@dp.message(F.text)
async def handle_url(message: Message):
    """Автоматически парсит ссылку на Ozon"""
    text = message.text or ""
    user_id = message.from_user.id
    
    # Проверяем режим рассылки для админа
    if user_id in ADMIN_IDS and pending_broadcast.get(user_id):
        del pending_broadcast[user_id]
        await do_broadcast(message)
        return
    
    # Если пользователь ожидает ввода цены — чистим старый стейт,
    # чтобы он мог добавить новый товар
    if user_id in pending_price_input:
        del pending_price_input[user_id]
    
    # Ищем URL Ozon
    url = None
    if "ozon.ru" in text.lower():
        # Извлекаем URL — поддерживаем ozon.ru/t/ (короткие ссылки из приложения)
        match = re.search(r'(?:https?://)?(?:www\.)?ozon\.ru[^\s]*', text)
        if match:
            url = match.group(0)
            if not url.startswith('http'):
                url = 'https://' + url
            # Если это короткая ссылка — разрешаем
            if '/t/' in url:
                try:
                    import requests
                    resp = requests.get(url, allow_redirects=True, timeout=10)
                    final = resp.url
                    # Берём чистый URL товара
                    clean = re.search(r'https://www\.ozon\.ru/product/[^?]+', final)
                    if clean:
                        url = clean.group(0)
                        logger.info(f"[HANDLE_URL] Short link resolved: {url}")
                except Exception as e:
                    logger.warning(f"[HANDLE_URL] Failed to resolve short link: {e}")
            else:
                url = re.sub(r'\?.*', '', url)
    
    if not url:
        return  # Не URL — игнорируем
    
    # Проверка SSRF: только ozon.ru
    if not is_valid_ozon_url(url):
        logger.warning(f"[SECURITY] SSRF blocked: {url}")
        return
    
    # Rate limit
    if not check_rate_limit(user_id):
        await message.answer("⏳ Слишком много запросов! Подожди минуту.", reply_markup=main_keyboard)
        return
    
    logger.info(f"[HANDLE_URL] user={user_id}, url={url[:50]}...")
    
    # Парсим
    status_msg = await message.answer("⏳ Парсинг товара...")
    result = await parse_ozon_price(url)
    
    if not result.get("success"):
        await status_msg.edit_text(f"❌ Ошибка парсинга: {result.get('error', 'Неизвестная ошибка')}")
        return
    
    price = result["price"]
    title = escape_html(result["title"] or "Товар с Ozon")
    
    # Показываем карточку с выбором процента
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📉 -5%", callback_data=f"track:{price}:{price*0.95}:percent:5"),
         InlineKeyboardButton(text="📉 -10%", callback_data=f"track:{price}:{price*0.90}:percent:10"),
         InlineKeyboardButton(text="📉 -15%", callback_data=f"track:{price}:{price*0.85}:percent:15")],
        [InlineKeyboardButton(text="📉 -20%", callback_data=f"track:{price}:{price*0.80}:percent:20"),
         InlineKeyboardButton(text="📉 -25%", callback_data=f"track:{price}:{price*0.75}:percent:25"),
         InlineKeyboardButton(text="📉 -30%", callback_data=f"track:{price}:{price*0.70}:percent:30")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])
    
    await status_msg.edit_text(
        f"📦 <b>{title[:60]}</b>\n\n"
        f"💰 <b>Текущая цена: {price:.0f} ₽</b>\n\n"
        f"🔗 <a href=\"{url}\">Открыть на Ozon</a>\n\n"
        f"Выбери процент или введи сумму в рублях (например: 300)\n"
        f"Бот отследит падение до {price:.0f} - [твоя сумма] ₽",
        reply_markup=keyboard
    )
    
    # Сохраняем данные для обработки ввода
    pending_price_input[user_id] = {"price": price, "title": title, "url": url}

# ─── Callback обработка ────────────────────────────────────────

@dp.callback_query(lambda c: c.data and c.data.startswith("track:"))
async def callback_track(callback: CallbackQuery):
    """Обработка кнопки отслеживания"""
    await callback.answer()
    
    try:
        data = callback.data.split(":")
        if len(data) < 5:
            await callback.message.edit_text("❌ Ошибка данных кнопки")
            return
        
        current_price = float(data[1])
        target = float(data[2])
        alert_type = data[3]  # percent или rub
        alert_value = float(data[4])
    except (ValueError, IndexError) as e:
        logger.error(f"[callback_track] Error parsing: {e}, data={callback.data}")
        await callback.message.edit_text("❌ Ошибка обработки")
        return
    
    user_id = callback.from_user.id
    message = callback.message
    
    # Берём URL из pending_price_input (там он сохранён после парсинга)
    pending = pending_price_input.get(user_id, {})
    url = pending.get("url", "https://ozon.ru/product/unknown/")
    title = pending.get("title", "Товар с Ozon")
    if not url or url == "https://ozon.ru/product/unknown/":
        # Fallback: пытаемся извлечь из сообщения
        text = callback.message.text or ""
        url_match = re.search(r'https?://[^\s]+ozon\.ru[^\s]*', text)
        if url_match:
            url = url_match.group(0)
        title_match = re.search(r'📦 <b>([^<]+)</b>', text)
        title = title_match.group(1) if title_match else "Товар с Ozon"
    
    success, msg = add_product(user_id, url, title, current_price, target, alert_type, alert_value)
    
    # Чистим стейт — пользователь может добавлять новый товар
    if user_id in pending_price_input:
        del pending_price_input[user_id]
    
    if success:
        await message.edit_text(
            f"✅ <b>Товар добавлен!</b>\n\n"
            f"{title[:50]}\n"
            f"💰 {current_price:.0f} ₽ → 🎯 {target:.0f} ₽\n\n"
            f"🔗 <a href=\"{url}\">Перейти к товару</a>"
        )
    else:
        await message.edit_text(f"⚠️ {msg}")

@dp.callback_query(lambda c: c.data and c.data.startswith("untrack:"))
async def callback_untrack(callback: CallbackQuery):
    """Удаление через кнопку"""
    await callback.answer()
    
    product_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    removed = remove_product(user_id, product_id)
    
    if removed:
        await callback.message.edit_text("✅ Товар удалён из отслеживания")
    else:
        await callback.message.edit_text("❌ Не удалось удалить")

@dp.callback_query(lambda c: c.data == "cancel")
async def callback_cancel(callback: CallbackQuery):
    await callback.answer()
    await callback.message.edit_text("Отменено ❌")

# ─── Смена цели ──────────────────────────────────────────────────
pending_target_change = {}  # user_id: {"product_id": int, "title": str, "current_price": float}

@dp.callback_query(lambda c: c.data and c.data.startswith("change_target:"))
async def callback_change_target(callback: CallbackQuery):
    """Запускает диалог смены цели для товара"""
    await callback.answer()
    try:
        parts = callback.data.split(":")
        product_id = int(parts[1])
        current_target = float(parts[2]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        await callback.message.edit_text("❌ Ошибка данных")
        return

    user_id = callback.from_user.id

    # Получаем инфу о товаре
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT title, current_price FROM tracked_products WHERE id=? AND user_id=?",
                       (product_id, user_id)).fetchone()
    conn.close()

    if not row:
        await callback.message.edit_text("❌ Товар не найден")
        return

    title = row[0] or "Товар"
    current_price = float(row[1]) if row[1] else 0

    # Сохраняем стейт
    pending_target_change[user_id] = {
        "product_id": product_id,
        "title": title,
        "current_price": current_price
    }

    await callback.message.edit_text(
        f"🎯 <b>Новая цель для:</b>\n"
        f"📦 {title[:50]}\n\n"
        f"💰 Текущая цена: {current_price:.0f} ₽\n"
        f"📉 Текущая цель: {current_target:.0f} ₽\n\n"
        f"Напиши <b>сумму в рублях</b> (например: 50000)\n"
        f"или <b>процент</b> со знаком % (например: 15%)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ])
    )

# ─── Обновить все цены ───────────────────────────────────────────
@dp.callback_query(lambda c: c.data == "refresh_prices")
async def callback_refresh_prices(callback: CallbackQuery):
    """Обновляет цены на все товары пользователя"""
    await callback.answer()
    user_id = callback.from_user.id
    msg = await callback.message.edit_text("🔄 Обновляю цены...")

    rows = get_user_products(user_id)
    if not rows:
        await msg.edit_text("📋 Нет товаров для обновления")
        return

    updated = 0
    failed = 0
    for r in rows:
        product_id, url = r[0], r[2]
        try:
            result = await parse_ozon_price(url)
            if result.get("success"):
                new_price = result["price"]
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE tracked_products SET current_price=?, last_check=CURRENT_TIMESTAMP WHERE id=?",
                            (new_price, product_id))
                conn.commit()
                conn.close()
                updated += 1
            else:
                failed += 1
        except Exception as e:
            logger.error(f"[REFRESH] Error {product_id}: {e}")
            failed += 1

    await msg.edit_text(
        f"✅ <b>Обновление завершено!</b>\n\n"
        f"📦 Проверено: {len(rows)}\n"
        f"✅ Обновлено: {updated}\n"
        f"❌ Ошибок: {failed}\n\n"
        f"Напиши /prices чтобы увидеть новые цены 🔄",
        reply_markup=main_keyboard
    )

# ─── Меню ──────────────────────────────────────────────────────

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Мои товары", callback_data="menu:prices")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="menu:stats")]
    ])

# ─── Запуск ───────────────────────────────────────────────────

async def main():
    logger.info("Bot starting...")
    init_db()
    logger.info("Database initialized")
    
    # Запускаем фоновые задачи
    asyncio.create_task(price_checker())
    asyncio.create_task(daily_reminder())
    
    # Graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (2, 15):  # SIGINT=2, SIGTERM=15
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))
        except NotImplementedError:
            pass  # Windows не поддерживает add_signal_handler
    
    await dp.start_polling(bot)

async def shutdown():
    """Graceful shutdown — закрываем все ресурсы"""
    logger.info("Shutting down...")
    await dp.stop_polling()
    # Закрываем bot-сессию
    await bot.session.close()
    logger.info("Shutdown complete.")

# ─── Автопроверка цен ────────────────────────────────────────────
async def price_checker():
    """Проверяет цены каждые 3 часа"""
    while True:
        await asyncio.sleep(3 * 60 * 60)  # 3 часа
        try:
            await check_all_prices()
        except Exception as e:
            logger.error(f"Price check error: {e}")

async def check_all_prices():
    """Проверяет цены всех товаров"""
    
    conn = sqlite3.connect(DB_PATH)
    products = conn.execute("SELECT id, user_id, url, title, target_price FROM tracked_products").fetchall()
    conn.close()
    
    for p in products:
        product_id, user_id, url, title, target = p
        try:
            result = await parse_ozon_price(url)
            if not result.get("success"):
                logger.warning(f"[CHECK] Parse failed for product {product_id}: {result.get('error')}")
                continue
            
            current_price = result["price"]
            
            # Обновляем last_check и current_price в БД
            conn2 = sqlite3.connect(DB_PATH)
            conn2.execute("UPDATE tracked_products SET last_check=CURRENT_TIMESTAMP, current_price=? WHERE id=?", 
                         (current_price, product_id))
            conn2.commit()
            conn2.close()
            
            if current_price <= target:
                await bot.send_message(
                    user_id,
                    f"🔔 <b>Цена упала!</b>\n\n{title[:50]}\n"
                    f"Теперь: {current_price:.0f}₽\nЦель: {target:.0f}₽\n\n"
                    f"🔗 <a href=\"{url}\">Перейти к товару</a>",
                    reply_markup=main_keyboard
                )
                # Отмечаем что уведомили
                conn3 = sqlite3.connect(DB_PATH)
                conn3.execute("UPDATE tracked_products SET notified=1 WHERE id=?", (product_id,))
                conn3.commit()
                conn3.close()
        except Exception as e:
            logger.error(f"Check price error for product {product_id}: {e}")

# ─── Ежедневное напоминание ───────────────────────────────────────
async def daily_reminder():
    """Напоминает о товарах раз в день"""
    while True:
        await asyncio.sleep(24 * 60 * 60)  # 24 часа
        try:
            await send_daily_reminders()
        except Exception as e:
            logger.error(f"Daily reminder error: {e}")

async def send_daily_reminders():
    """Отправляет напоминания всем пользователям"""
    conn = sqlite3.connect(DB_PATH)
    users = conn.execute("SELECT DISTINCT user_id FROM tracked_products").fetchall()
    conn.close()
    
    for (user_id,) in users:
        try:
            rows = get_user_products(user_id)
            if rows:
                await bot.send_message(
                    user_id,
                    f"📋 <b>Напоминание:</b> у тебя {len(rows)} отслеживаемых товаров!\n\n"
                    "Проверь /prices",
                    reply_markup=main_keyboard
                )
        except Exception as e:
            logger.error(f"Reminder error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
