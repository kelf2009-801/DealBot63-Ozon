# ============================================================
# ⚙️ НАСТРОЙКИ БОТА — меняй смело, перезапусти контейнер
# ============================================================

# --- Пароль для доступа к админ-панели ---
ADMIN_PASSWORD = "1434"

# --- Твой Telegram ID (админ) ---
ADMIN_IDS = [378061707]

# --- Тарифы ---
# max_products   — сколько товаров можно отслеживать
# check_interval — как часто проверять цены (в секундах)
TARIFFS = {
    "free": {
        "name": "Бесплатный",
        "max_products": 3,
        "check_interval": 3 * 60 * 60  # 3 часа
    },
    "premium": {
        "name": "Премиум",
        "max_products": 20,
        "check_interval": 30 * 60  # 30 минут
    },
    "vip": {
        "name": "VIP",
        "max_products": 100,
        "check_interval": 10 * 60  # 10 минут
    }
}

# ============================================================
# 🐳 ШПАРГАЛКА ПО DOCKER
# ============================================================
#
# --- Управление ботом ---
# docker ps                    — список контейнеров
# docker logs ozon-price-bot   — логи бота
# docker restart ozon-price-bot — перезапустить
# docker stop ozon-price-bot   — остановить
# docker start ozon-price-bot  — запустить
#
# --- После изменения config.py или main_full.py ---
# cd D:\\bot_ozon
# docker compose build
# docker compose up -d --force-recreate
#
# --- Если бот упал и не стартует ---
# docker logs ozon-price-bot --tail 20  — посмотреть ошибку
#
# --- Полезно ---
# docker exec -it ozon-price-bot bash   — зайти внутрь контейнера
