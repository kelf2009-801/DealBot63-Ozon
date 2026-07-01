#!/usr/bin/env python3
"""
Тесты для Ozon Price Monitor бота (DealBot63_bot)
==================================================
Запуск: pytest -v test_bot.py
        pytest -v test_bot.py --asyncio-mode=auto
"""

import os
import sys
import re
import sqlite3
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch, MagicMock, PropertyMock

import pytest
import pytest_asyncio

# ── Подготовка окружения перед импортом бота ──────────────────────
# Патчим всё что вызывает сетевые/внешние вызовы при импорте

# 1. Мокаем cloakbrowser и dotenv до любых импортов
sys.modules["cloakbrowser"] = MagicMock()
sys.modules["dotenv"] = MagicMock()

# 2. Мокаем Bot от aiogram, чтобы не валидировал токен
from unittest.mock import patch as _patch

# Патчим aiogram.client.bot.Bot.__init__ чтобы пропустить валидацию токена
_original_bot_init = None

def _mock_bot_init(self, token, **kwargs):
    """Мок-конструктор Bot — пропускает валидацию токена."""
    self._token = token

# 3. Устанавливаем BOT_TOKEN (любой, просто чтобы был)
os.environ["BOT_TOKEN"] = "1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_test"

# 4. Применяем патч на Bot.__init__ перед импортом main_full
import aiogram.client.bot
aiogram.client.bot.Bot.__init__ = _mock_bot_init

# Также патчим Dispatcher чтобы не стартовал поллинг
import aiogram
_original_dp_init = aiogram.Dispatcher.__init__

# Теперь импортируем модуль бота
sys.path.insert(0, str(Path(__file__).parent))

import main_full as bot_module

# ── Фикстуры ──────────────────────────────────────────────────────

@pytest.fixture
def temp_db():
    """Создаёт временную БД для каждого теста."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db", prefix="test_prices_")
    old_db = bot_module.DB_PATH
    bot_module.DB_PATH = db_path
    yield db_path
    bot_module.DB_PATH = old_db
    os.close(db_fd)
    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def clean_db(temp_db):
    """Инициализирует чистую БД."""
    bot_module.init_db()
    return temp_db


@pytest.fixture
def mock_message():
    """Создаёт мок aiogram Message."""
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = 123456789
    msg.from_user.username = "testuser"
    msg.from_user.full_name = "Test User"
    msg.chat = MagicMock()
    msg.chat.id = 123456789
    msg.text = ""
    msg.answer = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.reply = AsyncMock()
    msg.answer_invoice = AsyncMock()
    return msg


@pytest.fixture
def mock_callback():
    """Создаёт мок aiogram CallbackQuery."""
    cb = MagicMock()
    cb.from_user = MagicMock()
    cb.from_user.id = 123456789
    cb.message = MagicMock()
    cb.message.chat = MagicMock()
    cb.message.chat.id = 123456789
    cb.message.text = ""
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    cb.data = ""
    return cb


@pytest.fixture
def mock_pre_checkout():
    """Создаёт мок PreCheckoutQuery."""
    pcq = MagicMock()
    pcq.from_user = MagicMock()
    pcq.from_user.id = 123456789
    pcq.id = "test_checkout_id"
    pcq.answer = AsyncMock()
    return pcq


# =====================================================================
# 1. ТЕСТЫ БАЗЫ ДАННЫХ
# =====================================================================

class TestDatabase:
    """Тесты для функций работы с БД: init_db, add_product, remove_product,
    get_user_products, get_all_stats."""

    def test_init_db_creates_tables(self, temp_db):
        """init_db должен создать таблицы tracked_products и user_tariffs."""
        bot_module.init_db()

        conn = sqlite3.connect(temp_db)
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        conn.close()

        assert "tracked_products" in table_names
        assert "user_tariffs" in table_names

    def test_init_db_idempotent(self, temp_db):
        """Повторный вызов init_db не должен падать."""
        bot_module.init_db()
        bot_module.init_db()  # Не должно быть исключения

        conn = sqlite3.connect(temp_db)
        count = conn.execute("SELECT COUNT(*) FROM tracked_products").fetchone()[0]
        conn.close()
        assert count == 0

    def test_add_product_success(self, clean_db):
        """Успешное добавление товара."""
        success, msg = bot_module.add_product(
            user_id=111,
            url="https://ozon.ru/product/test-123",
            title="Тестовый товар",
            price=1500.0,
            target=1200.0,
            alert_type="percent",
            alert_value=20,
        )
        assert success is True
        assert "добавлен" in msg.lower()

        # Проверяем, что товар действительно в БД
        conn = sqlite3.connect(clean_db)
        row = conn.execute(
            "SELECT * FROM tracked_products WHERE user_id=?", (111,)
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[2] == "https://ozon.ru/product/test-123"  # url
        assert row[4] == "Тестовый товар"  # title
        assert row[5] == 1500.0  # current_price
        assert row[6] == 1200.0  # target_price

    def test_add_product_duplicate(self, clean_db):
        """Повторное добавление того же URL должно вернуть False."""
        bot_module.add_product(111, "https://ozon.ru/product/test", "T", 100, 80)
        success, msg = bot_module.add_product(111, "https://ozon.ru/product/test", "T", 100, 80)

        assert success is False
        assert "уже отслеживается" in msg.lower()

    def test_add_product_limit_free(self, clean_db):
        """Бесплатный тариф: лимит 3 товара."""
        # Добавляем 3 товара
        for i in range(3):
            success, _ = bot_module.add_product(
                222, f"https://ozon.ru/product/test-{i}", f"T{i}", 100, 80
            )
            assert success is True

        # 4-й товар — отказ
        success, msg = bot_module.add_product(
            222, "https://ozon.ru/product/test-extra", "TExtra", 100, 80
        )
        assert success is False
        assert "лимит" in msg.lower()

    def test_remove_product_success(self, clean_db):
        """Удаление существующего товара."""
        bot_module.add_product(333, "https://ozon.ru/product/to-remove", "T", 100, 80)

        conn = sqlite3.connect(clean_db)
        pid = conn.execute(
            "SELECT id FROM tracked_products WHERE user_id=?", (333,)
        ).fetchone()[0]
        conn.close()

        removed = bot_module.remove_product(333, pid)
        assert removed is True

        # Проверяем, что товара нет
        conn = sqlite3.connect(clean_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM tracked_products WHERE id=?", (pid,)
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_remove_product_wrong_user(self, clean_db):
        """Нельзя удалить чужой товар."""
        bot_module.add_product(444, "https://ozon.ru/product/test", "T", 100, 80)

        conn = sqlite3.connect(clean_db)
        pid = conn.execute(
            "SELECT id FROM tracked_products WHERE user_id=?", (444,)
        ).fetchone()[0]
        conn.close()

        # Другой пользователь пытается удалить
        removed = bot_module.remove_product(555, pid)
        assert removed is False

    def test_remove_product_not_found(self, clean_db):
        """Удаление несуществующего товара."""
        removed = bot_module.remove_product(999, 99999)
        assert removed is False

    def test_get_user_products_empty(self, clean_db):
        """Пустой список для нового пользователя."""
        rows = bot_module.get_user_products(777)
        assert rows == []

    def test_get_user_products_multiple(self, clean_db):
        """Несколько товаров одного пользователя."""
        for i in range(2):
            bot_module.add_product(
                888, f"https://ozon.ru/product/test-{i}", f"Tovar {i}", 100 + i * 10, 80
            )

        rows = bot_module.get_user_products(888)
        assert len(rows) == 2

    def test_get_user_products_isolation(self, clean_db):
        """Товары разных пользователей изолированы."""
        bot_module.add_product(111, "https://ozon.ru/product/a", "A", 100, 80)
        bot_module.add_product(222, "https://ozon.ru/product/b", "B", 200, 150)

        rows_111 = bot_module.get_user_products(111)
        rows_222 = bot_module.get_user_products(222)

        assert len(rows_111) == 1
        assert len(rows_222) == 1
        assert rows_111[0][4] == "A"
        assert rows_222[0][4] == "B"

    def test_get_all_stats_empty(self, clean_db):
        """Статистика на пустой БД."""
        total, users = bot_module.get_all_stats()
        assert total == 0
        assert users == 0

    def test_get_all_stats_with_data(self, clean_db):
        """Статистика с товарами."""
        bot_module.add_product(111, "https://ozon.ru/product/a", "A", 100, 80)
        bot_module.add_product(111, "https://ozon.ru/product/b", "B", 200, 150)
        bot_module.add_product(222, "https://ozon.ru/product/c", "C", 300, 250)

        total, users = bot_module.get_all_stats()
        assert total == 3
        assert users == 2


# =====================================================================
# 2. ТЕСТЫ ПАРСИНГА URL
# =====================================================================

class TestUrlParsing:
    """Проверка извлечения URL Ozon из сообщений."""

    # Используем тот же regex что и в боте
    OZON_URL_RE = re.compile(r"(?:https?://)?(?:www\.)?ozon\.ru[^\s]*")

    def extract_url(self, text: str) -> str | None:
        """Извлекает URL из текста (как в handle_url)."""
        if "ozon.ru" not in text.lower():
            return None
        match = self.OZON_URL_RE.search(text)
        if not match:
            return None
        url = match.group(0)
        if not url.startswith("http"):
            url = "https://" + url
        # Убираем query-параметры (кроме коротких ссылок)
        if "/t/" not in url:
            url = re.sub(r"\?.*", "", url)
        return url

    def test_full_url_with_https(self):
        """Полный URL с https://."""
        url = self.extract_url("https://ozon.ru/product/12345-test/")
        assert url == "https://ozon.ru/product/12345-test/"

    def test_url_without_protocol(self):
        """URL без протокола."""
        url = self.extract_url("ozon.ru/product/abc-def/")
        assert url == "https://ozon.ru/product/abc-def/"

    def test_url_with_www(self):
        """URL с www."""
        url = self.extract_url("https://www.ozon.ru/product/test/")
        assert url == "https://www.ozon.ru/product/test/"

    def test_url_in_text(self):
        """URL внутри текста сообщения."""
        url = self.extract_url(
            "Привет! Посмотри этот товар https://ozon.ru/product/iphone-15/"
        )
        assert url == "https://ozon.ru/product/iphone-15/"

    def test_short_link_t(self):
        """Короткая ссылка /t/ (из приложения Ozon)."""
        url = self.extract_url("https://ozon.ru/t/longhash123")
        # Короткие ссылки сохраняют query params (для разрешения)
        assert url == "https://ozon.ru/t/longhash123"

    def test_url_with_query_params(self):
        """URL с query-параметрами (должны обрезаться)."""
        url = self.extract_url(
            "https://ozon.ru/product/test/?utm_source=telegram&ref=bot"
        )
        assert url == "https://ozon.ru/product/test/"

    def test_no_ozon_url(self):
        """Текст без ссылки на Ozon."""
        url = self.extract_url("Привет! Как дела?")
        assert url is None

    def test_other_site_url(self):
        """Ссылка на другой сайт — не должна извлекаться."""
        url = self.extract_url("https://wildberries.ru/product/test/")
        assert url is None

    def test_multiple_urls_first_ozon(self):
        """Несколько URL, первый — Ozon."""
        url = self.extract_url(
            "ozon.ru/product/a/ и https://wildberries.ru/product/b/"
        )
        assert url == "https://ozon.ru/product/a/"

    def test_url_with_trailing_spaces(self):
        """URL с пробелами после."""
        url = self.extract_url("https://ozon.ru/product/test/   ")
        assert url == "https://ozon.ru/product/test/"

    def test_url_with_russian_text_attached(self):
        """URL, за которым сразу идёт русский текст без пробела."""
        url = self.extract_url("https://ozon.ru/product/test/товар")
        # Регекс [^\s]* захватит всё до пробела
        # Это ожидаемое поведение — URL может содержать кириллицу в пути
        assert url is not None
        assert "ozon.ru" in url


# =====================================================================
# 3. ТЕСТЫ КОМАНД
# =====================================================================

class TestCommands:
    """Тесты для команд: /start, /prices, /stats, /admin, /help."""

    @pytest.mark.asyncio
    async def test_start_command(self, mock_message):
        """Команда /start отправляет приветственное сообщение с клавиатурой."""
        mock_message.text = "/start"

        await bot_module.cmd_start(mock_message)

        mock_message.answer.assert_called_once()
        call_args = mock_message.answer.call_args
        # Первый аргумент — текст
        text = call_args[0][0] if call_args[0] else call_args.kwargs.get("text", "")
        assert "Привет" in text
        assert "Ozon" in text
        # Проверяем что передан reply_markup
        assert "reply_markup" in call_args.kwargs

    @pytest.mark.asyncio
    async def test_help_command(self, mock_message):
        """Команда /help отправляет справку."""
        mock_message.text = "/help"

        await bot_module.cmd_help(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "Справка" in text or "help" in text.lower()
        assert "/start" in text
        assert "/prices" in text

    @pytest.mark.asyncio
    async def test_prices_empty(self, mock_message, clean_db):
        """Команда /prices у пользователя без товаров."""
        mock_message.text = "/prices"
        mock_message.from_user.id = 999

        await bot_module.cmd_prices(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "нет" in text.lower() or "нет отслеживаемых" in text.lower()

    @pytest.mark.asyncio
    async def test_prices_with_products(self, mock_message, clean_db):
        """Команда /prices показывает товары пользователя."""
        bot_module.add_product(
            999, "https://ozon.ru/product/a", "Товар А", 1500, 1200
        )
        bot_module.add_product(
            999, "https://ozon.ru/product/b", "Товар Б", 2500, 2000
        )

        mock_message.text = "/prices"
        mock_message.from_user.id = 999

        await bot_module.cmd_prices(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "Товар А" in text
        assert "Товар Б" in text
        assert "1500" in text
        assert "2500" in text

    @pytest.mark.asyncio
    async def test_stats_command(self, mock_message, clean_db):
        """Команда /stats показывает статистику."""
        bot_module.add_product(111, "https://ozon.ru/product/a", "A", 100, 80)
        bot_module.add_product(222, "https://ozon.ru/product/b", "B", 200, 150)

        mock_message.text = "/stats"
        mock_message.from_user.id = 111

        await bot_module.cmd_stats(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "2" in text  # всего товаров
        assert "Статистика" in text

    @pytest.mark.asyncio
    async def test_admin_command_not_admin(self, mock_message):
        """Не-админ получает запрос пароля."""
        mock_message.text = "/admin"
        mock_message.from_user.id = 999999  # не админ

        await bot_module.cmd_admin(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "парол" in text.lower()
        # Проверяем, что юзер добавлен в pending_password
        assert 999999 in bot_module.admin_pending_password
        # Чистим
        bot_module.admin_pending_password.pop(999999, None)

    @pytest.mark.asyncio
    async def test_admin_command_admin_id(self, mock_message):
        """Админ (по ID) сразу получает панель."""
        mock_message.text = "/admin"
        mock_message.from_user.id = 378061707  # ID из ADMIN_IDS

        await bot_module.cmd_admin(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "АДМИН" in text
        # Проверяем inline keyboard в ответе
        assert "reply_markup" in mock_message.answer.call_args.kwargs

    @pytest.mark.asyncio
    async def test_admin_password_correct(self, mock_message):
        """Правильный пароль даёт доступ к админке."""
        mock_message.text = "1434"  # ADMIN_PASSWORD
        mock_message.from_user.id = 888888
        bot_module.admin_pending_password[888888] = True

        await bot_module.handle_admin_password(mock_message)

        # Проверяем, что доступ предоставлен
        assert 888888 in bot_module.admin_temp_access
        assert 888888 not in bot_module.admin_pending_password

        # Должно быть два ответа: "Пароль верный!" и меню админки
        assert mock_message.answer.call_count == 2
        texts = [c[0][0] for c in mock_message.answer.call_args_list]
        assert any("верный" in t.lower() for t in texts)
        assert any("АДМИН" in t for t in texts)

        # Чистим
        bot_module.admin_temp_access.pop(888888, None)

    @pytest.mark.asyncio
    async def test_admin_password_wrong(self, mock_message):
        """Неправильный пароль — отказ."""
        mock_message.text = "wrong_password"
        mock_message.from_user.id = 888888
        bot_module.admin_pending_password[888888] = True

        await bot_module.handle_admin_password(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "неверный" in text.lower()
        assert 888888 not in bot_module.admin_temp_access
        assert 888888 not in bot_module.admin_pending_password

    @pytest.mark.asyncio
    async def test_prices_button(self, mock_message, clean_db):
        """Кнопка '📋 Мои товары' вызывает ту же логику что и /prices."""
        bot_module.add_product(
            999, "https://ozon.ru/product/x", "Товар X", 500, 400
        )

        mock_message.text = "📋 Мои товары"
        mock_message.from_user.id = 999

        await bot_module.cmd_prices(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "Товар X" in text

    @pytest.mark.asyncio
    async def test_stats_button(self, mock_message, clean_db):
        """Кнопка '📊 Статистика' вызывает ту же логику что и /stats."""
        bot_module.add_product(111, "https://ozon.ru/product/a", "A", 100, 80)

        mock_message.text = "📊 Статистика"
        mock_message.from_user.id = 111

        await bot_module.cmd_stats(mock_message)

        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "1" in text
        assert "Статистика" in text


# =====================================================================
# 4. ТЕСТЫ ТАРИФОВ
# =====================================================================

class TestTariffs:
    """Тесты для тарифов: лимиты, проверка тарифов."""

    def test_tariff_config_structure(self):
        """Проверка структуры конфига тарифов."""
        from config import TARIFFS

        assert "free" in TARIFFS
        assert "premium" in TARIFFS
        assert "vip" in TARIFFS

        for key in TARIFFS:
            t = TARIFFS[key]
            assert "name" in t
            assert "max_products" in t
            assert "check_interval" in t

    def test_free_tariff_defaults(self):
        """Бесплатный тариф: 3 товара, 3 часа."""
        from config import TARIFFS

        assert TARIFFS["free"]["max_products"] == 3
        assert TARIFFS["free"]["check_interval"] == 3 * 60 * 60  # 10800 сек

    def test_premium_tariff(self):
        """Премиум тариф: 20 товаров, 30 минут."""
        from config import TARIFFS

        assert TARIFFS["premium"]["max_products"] == 20
        assert TARIFFS["premium"]["check_interval"] == 30 * 60

    def test_vip_tariff(self):
        """VIP тариф: 100 товаров, 10 минут."""
        from config import TARIFFS

        assert TARIFFS["vip"]["max_products"] == 100
        assert TARIFFS["vip"]["check_interval"] == 10 * 60

    def test_add_product_respects_free_limit(self, clean_db):
        """Лимит бесплатного тарифа (3 товара) соблюдается."""
        # Добавляем 3 товара — ок
        for i in range(3):
            ok, _ = bot_module.add_product(
                111, f"https://ozon.ru/product/test-{i}", f"T{i}", 100, 80
            )
            assert ok is True

        # 4-й — отказ
        ok, msg = bot_module.add_product(
            111, "https://ozon.ru/product/test-extra", "TExtra", 100, 80
        )
        assert ok is False
        assert "лимит" in msg.lower()

    def test_add_product_no_limit_for_premium(self, clean_db):
        """Премиум-пользователь может добавить больше 3 товаров."""
        # Устанавливаем тариф premium
        conn = sqlite3.connect(clean_db)
        conn.execute(
            "INSERT OR REPLACE INTO user_tariffs (user_id, tariff) VALUES (?, ?)",
            (555, "premium"),
        )
        conn.commit()
        conn.close()

        # Добавляем 5 товаров (лимит премиума — 20)
        for i in range(5):
            ok, _ = bot_module.add_product(
                555, f"https://ozon.ru/product/test-{i}", f"T{i}", 100, 80
            )
            assert ok is True, f"Item {i} should be added"

        rows = bot_module.get_user_products(555)
        assert len(rows) == 5

    def test_add_product_no_limit_for_vip(self, clean_db):
        """VIP-пользователь может добавить много товаров."""
        conn = sqlite3.connect(clean_db)
        conn.execute(
            "INSERT OR REPLACE INTO user_tariffs (user_id, tariff) VALUES (?, ?)",
            (777, "vip"),
        )
        conn.commit()
        conn.close()

        # Добавляем 10 товаров (лимит VIP — 100)
        for i in range(10):
            ok, _ = bot_module.add_product(
                777, f"https://ozon.ru/product/test-{i}", f"T{i}", 100, 80
            )
            assert ok is True

        rows = bot_module.get_user_products(777)
        assert len(rows) == 10

    def test_user_tariff_defaults_to_free(self, clean_db):
        """Если тариф не установлен — считается free."""
        # НЕ добавляем запись в user_tariffs
        # Добавляем 3 товара — ок
        for i in range(3):
            ok, _ = bot_module.add_product(
                999, f"https://ozon.ru/product/test-{i}", f"T{i}", 100, 80
            )
            assert ok is True

        # 4-й — отказ
        ok, msg = bot_module.add_product(
            999, "https://ozon.ru/product/test-extra", "TExtra", 100, 80
        )
        assert ok is False
        assert "лимит" in msg.lower()


# =====================================================================
# 5. ТЕСТЫ ПЛАТЕЖЕЙ
# =====================================================================

class TestPayments:
    """Тесты для pre_checkout и successful_payment."""

    @pytest.mark.asyncio
    async def test_pre_checkout_accepts(self, mock_pre_checkout):
        """pre_checkout_handler всегда отвечает ok=True."""
        await bot_module.pre_checkout_handler(mock_pre_checkout)

        mock_pre_checkout.answer.assert_called_once_with(ok=True)

    @pytest.mark.asyncio
    async def test_successful_payment_premium(self, mock_message, clean_db):
        """Успешный платёж — premium тариф."""
        mock_message.from_user.id = 123456789
        mock_message.successful_payment = MagicMock()
        mock_message.successful_payment.invoice_payload = "tariff:premium"

        await bot_module.success_payment_handler(mock_message)

        # Проверяем, что тариф записан в БД
        conn = sqlite3.connect(clean_db)
        row = conn.execute(
            "SELECT tariff, expires_at FROM user_tariffs WHERE user_id=?",
            (123456789,),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "premium"
        assert row[1] is not None  # expires_at установлен

        # Проверяем ответное сообщение
        mock_message.answer.assert_called_once()
        text = mock_message.answer.call_args[0][0]
        assert "Оплата прошла" in text
        assert "Премиум" in text

    @pytest.mark.asyncio
    async def test_successful_payment_vip(self, mock_message, clean_db):
        """Успешный платёж — VIP тариф."""
        mock_message.from_user.id = 987654321
        mock_message.successful_payment = MagicMock()
        mock_message.successful_payment.invoice_payload = "tariff:vip"

        await bot_module.success_payment_handler(mock_message)

        conn = sqlite3.connect(clean_db)
        row = conn.execute(
            "SELECT tariff FROM user_tariffs WHERE user_id=?", (987654321,)
        ).fetchone()
        conn.close()

        assert row[0] == "vip"

        text = mock_message.answer.call_args[0][0]
        assert "VIP" in text

    @pytest.mark.asyncio
    async def test_successful_payment_updates_existing(self, mock_message, clean_db):
        """Повторный платёж обновляет существующий тариф."""
        # Сначала ставим free
        conn = sqlite3.connect(clean_db)
        conn.execute(
            "INSERT INTO user_tariffs (user_id, tariff) VALUES (?, ?)",
            (111, "free"),
        )
        conn.commit()
        conn.close()

        # Теперь платёж за premium
        mock_message.from_user.id = 111
        mock_message.successful_payment = MagicMock()
        mock_message.successful_payment.invoice_payload = "tariff:premium"

        await bot_module.success_payment_handler(mock_message)

        conn = sqlite3.connect(clean_db)
        row = conn.execute(
            "SELECT tariff FROM user_tariffs WHERE user_id=?", (111,)
        ).fetchone()
        conn.close()

        assert row[0] == "premium"  # Обновился с free на premium

    @pytest.mark.asyncio
    async def test_successful_payment_payload_without_colon(self, mock_message, clean_db):
        """Платёж с payload без двоеточия — fallback на premium."""
        mock_message.from_user.id = 222
        mock_message.successful_payment = MagicMock()
        mock_message.successful_payment.invoice_payload = "premium"

        await bot_module.success_payment_handler(mock_message)

        conn = sqlite3.connect(clean_db)
        row = conn.execute(
            "SELECT tariff FROM user_tariffs WHERE user_id=?", (222,)
        ).fetchone()
        conn.close()

        # По коду: tariff = payload.split(":")[1] if ":" in payload else "premium"
        # "premium" без ":" → fallback на "premium"
        assert row is not None
        assert row[0] == "premium"

    @pytest.mark.asyncio
    async def test_successful_payment_expiry_set(self, mock_message, clean_db):
        """Проверяем, что expires_at устанавливается на +30 дней."""
        import time

        mock_message.from_user.id = 333
        mock_message.successful_payment = MagicMock()
        mock_message.successful_payment.invoice_payload = "tariff:premium"

        now = int(time.time())
        await bot_module.success_payment_handler(mock_message)

        conn = sqlite3.connect(clean_db)
        row = conn.execute(
            "SELECT expires_at FROM user_tariffs WHERE user_id=?", (333,)
        ).fetchone()
        conn.close()

        assert row[0] is not None
        # Должен быть примерно now + 30 дней (с погрешностью в пару секунд)
        expected = now + 30 * 24 * 60 * 60
        assert abs(row[0] - expected) < 5  # Погрешность до 5 секунд


# =====================================================================
# 6. ИНТЕГРАЦИОННЫЕ ТЕСТЫ
# =====================================================================

class TestIntegration:
    """Интеграционные тесты: полный цикл добавления-удаления товара."""

    def test_full_lifecycle(self, clean_db):
        """Полный цикл: add → get → stats → remove → verify."""
        user_id = 12345

        # 1. Добавляем товар
        ok, _ = bot_module.add_product(
            user_id, "https://ozon.ru/product/iphone", "iPhone 15", 89990, 75000
        )
        assert ok is True

        # 2. Проверяем через get_user_products
        rows = bot_module.get_user_products(user_id)
        assert len(rows) == 1
        assert rows[0][4] == "iPhone 15"
        assert rows[0][5] == 89990.0
        assert rows[0][6] == 75000.0

        pid = rows[0][0]

        # 3. Статистика
        total, users = bot_module.get_all_stats()
        assert total == 1
        assert users == 1

        # 4. Удаляем
        removed = bot_module.remove_product(user_id, pid)
        assert removed is True

        # 5. Проверяем, что товара нет
        rows = bot_module.get_user_products(user_id)
        assert len(rows) == 0

        # 6. Статистика обнулилась
        total, users = bot_module.get_all_stats()
        assert total == 0
        assert users == 0

    def test_multiple_users_isolation(self, clean_db):
        """Товары разных пользователей полностью изолированы."""
        # User A: 2 товара
        bot_module.add_product(111, "https://ozon.ru/product/a1", "A1", 100, 80)
        bot_module.add_product(111, "https://ozon.ru/product/a2", "A2", 200, 150)

        # User B: 1 товар
        bot_module.add_product(222, "https://ozon.ru/product/b1", "B1", 300, 250)

        # User A видит только свои
        a_rows = bot_module.get_user_products(111)
        assert len(a_rows) == 2
        titles_a = [r[4] for r in a_rows]
        assert "A1" in titles_a
        assert "A2" in titles_a

        # User B видит только свой
        b_rows = bot_module.get_user_products(222)
        assert len(b_rows) == 1
        assert b_rows[0][4] == "B1"

        # User C — пусто
        c_rows = bot_module.get_user_products(333)
        assert len(c_rows) == 0

        # Статистика
        total, users = bot_module.get_all_stats()
        assert total == 3
        assert users == 2

    def test_limit_per_user(self, clean_db):
        """Лимит считается отдельно для каждого пользователя."""
        # User A: 3 товара (лимит free)
        for i in range(3):
            ok, _ = bot_module.add_product(
                111, f"https://ozon.ru/product/a{i}", f"A{i}", 100, 80
            )
            assert ok is True

        # User A: 4-й — отказ
        ok, msg = bot_module.add_product(
            111, "https://ozon.ru/product/a_extra", "AExtra", 100, 80
        )
        assert ok is False

        # User B: может добавить свои 3 товара (его лимит не зависит от A)
        for i in range(3):
            ok, _ = bot_module.add_product(
                222, f"https://ozon.ru/product/b{i}", f"B{i}", 100, 80
            )
            assert ok is True


# =====================================================================
# 7. ТЕСТЫ is_number_text
# =====================================================================

class TestIsNumberText:
    """Тесты для вспомогательной функции is_number_text."""

    def test_positive_integer(self):
        assert bot_module.is_number_text("123") is True

    def test_zero(self):
        assert bot_module.is_number_text("0") is True

    def test_with_spaces(self):
        assert bot_module.is_number_text("  456  ") is True

    def test_negative(self):
        assert bot_module.is_number_text("-123") is False

    def test_float(self):
        assert bot_module.is_number_text("123.45") is False

    def test_text(self):
        assert bot_module.is_number_text("hello") is False

    def test_empty(self):
        assert bot_module.is_number_text("") is False

    def test_mixed(self):
        assert bot_module.is_number_text("123abc") is False


# =====================================================================
# 8. ТЕСТЫ КОНФИГА
# =====================================================================

class TestConfig:
    """Тесты для config.py."""

    def test_admin_password_exists(self):
        from config import ADMIN_PASSWORD
        assert ADMIN_PASSWORD is not None
        assert isinstance(ADMIN_PASSWORD, str)
        assert len(ADMIN_PASSWORD) > 0

    def test_admin_ids_is_list(self):
        from config import ADMIN_IDS
        assert isinstance(ADMIN_IDS, list)
        assert len(ADMIN_IDS) > 0

    def test_tariffs_have_required_keys(self):
        from config import TARIFFS
        required = {"name", "max_products", "check_interval"}
        for tariff_name, tariff_data in TARIFFS.items():
            assert required.issubset(
                tariff_data.keys()
            ), f"Tariff {tariff_name} missing keys: {required - set(tariff_data.keys())}"


# =====================================================================
# Точка входа
# =====================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--asyncio-mode=auto"])