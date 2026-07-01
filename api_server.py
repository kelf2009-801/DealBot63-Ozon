#!/usr/bin/env python3
"""
API-сервер для Ozon Price Monitor бота.
Лёгкий JSON API на Flask для чтения статистики, пользователей, товаров и экспорта в CSV.
Авторизация: заголовок X-API-Key со значением grove-street-2024.

Запуск:
    python api_server.py
    (по умолчанию слушает 0.0.0.0:5000)

Эндпоинты:
    GET /api/stats    — общая статистика
    GET /api/users    — список пользователей с товарами
    GET /api/products — все товары
    GET /api/export   — экспорт в CSV
"""

import csv
import io
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import Flask, jsonify, request, Response

# ─── Конфигурация ────────────────────────────────────────────────

API_KEY = "grove-street-2024"
DB_PATH = Path(__file__).parent / "prices.db"

app = Flask(__name__)

# ─── CORS ────────────────────────────────────────────────────────

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-API-Key'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# ─── Авторизация ─────────────────────────────────────────────────

def require_api_key(f):
    """Декоратор: проверяет заголовок X-API-Key."""
    @wraps(f)
    def decorated(*args, **kwargs):
        key = request.headers.get("X-API-Key", "")
        if key != API_KEY:
            return jsonify({"error": "Unauthorized", "message": "Неверный или отсутствующий X-API-Key"}), 401
        return f(*args, **kwargs)
    return decorated


def get_db():
    """Возвращает соединение с БД (read-only для безопасности)."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _format_timestamp(ts):
    """Форматирует timestamp в читаемую строку ISO."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts).isoformat()
    return str(ts)


# ─── Эндпоинты ───────────────────────────────────────────────────

@app.route("/api/stats", methods=["GET"])
@require_api_key
def api_stats():
    """
    GET /api/stats
    Возвращает общую статистику: количество товаров, пользователей,
    распределение по тарифам и другую сводную информацию.
    """
    conn = get_db()
    try:
        total_products = conn.execute(
            "SELECT COUNT(*) FROM tracked_products"
        ).fetchone()[0]

        total_users = conn.execute(
            "SELECT COUNT(DISTINCT user_id) FROM tracked_products"
        ).fetchone()[0]

        # Товары с уведомлениями (цена упала до цели)
        notified = conn.execute(
            "SELECT COUNT(*) FROM tracked_products WHERE notified = 1"
        ).fetchone()[0]

        # Средняя текущая цена
        avg_price_row = conn.execute(
            "SELECT AVG(current_price) FROM tracked_products WHERE current_price > 0"
        ).fetchone()
        avg_price = round(avg_price_row[0], 2) if avg_price_row[0] else 0

        # Распределение по тарифам
        tariff_rows = conn.execute(
            "SELECT tariff, COUNT(*) FROM user_tariffs GROUP BY tariff"
        ).fetchall()
        tariffs = {row["tariff"]: row["COUNT(*)"] for row in tariff_rows}

        # Товары по дням (последние 7 дней)
        recent_products = conn.execute("""
            SELECT DATE(created_at, 'unixepoch') as day, COUNT(*) as cnt
            FROM tracked_products
            WHERE created_at > unixepoch('now', '-7 days')
            GROUP BY day
            ORDER BY day
        """).fetchall()
        products_by_day = {row["day"]: row["cnt"] for row in recent_products}

        # История цен: всего записей
        total_history = 0
        try:
            total_history = conn.execute(
                "SELECT COUNT(*) FROM price_history"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass  # Таблицы может не быть

        # История цен: последние 30 записей для графика
        price_history = []
        try:
            ph_rows = conn.execute(
                "SELECT price, timestamp FROM price_history ORDER BY timestamp DESC LIMIT 30"
            ).fetchall()
            price_history = [{"price": r["price"], "timestamp": r["timestamp"]} for r in ph_rows]
        except sqlite3.OperationalError:
            pass
        
        # Проверки по дням (из last_check)
        checks_per_day = conn.execute("""
            SELECT DATE(last_check, 'unixepoch') as day, COUNT(*) as checks
            FROM tracked_products
            WHERE last_check IS NOT NULL
            GROUP BY day ORDER BY day
        """).fetchall()
        checks_per_day_list = [{"day": r["day"], "checks": r["checks"]} for r in checks_per_day]
        
        # Распределение пользователей по тарифам
        tariff_stats = conn.execute("""
            SELECT COALESCE(ut.tariff, 'free') as tariff, COUNT(DISTINCT tp.user_id) as user_count
            FROM tracked_products tp
            LEFT JOIN user_tariffs ut ON tp.user_id = ut.user_id
            GROUP BY tariff
        """).fetchall()
        tariff_stats_list = [{"tariff": r["tariff"], "user_count": r["user_count"]} for r in tariff_stats]
        
        # Типы алертов
        alert_types = conn.execute("""
            SELECT alert_type, COUNT(*) as cnt
            FROM tracked_products
            WHERE alert_type IS NOT NULL
            GROUP BY alert_type
        """).fetchall()
        alert_types_list = [{"alert_type": r["alert_type"], "cnt": r["cnt"]} for r in alert_types]

        # История всех цен для большого графика
        all_price_history = []
        try:
            aph_rows = conn.execute(
                "SELECT current_price, last_check FROM tracked_products WHERE last_check IS NOT NULL ORDER BY last_check DESC LIMIT 50"
            ).fetchall()
            all_price_history = [{"price": r["current_price"], "timestamp": r["last_check"]} for r in aph_rows]
        except:
            pass

        return jsonify({
            "total_products": total_products,
            "total_users": total_users,
            "notified_products": notified,
            "average_price": avg_price,
            "tariffs": tariffs,
            "products_by_day_last_7": products_by_day,
            "price_history_entries": total_history,
            "price_history": price_history,
            "checks_per_day": checks_per_day_list,
            "tariff_stats": tariff_stats_list,
            "alert_types": alert_types_list,
            "all_price_history": all_price_history,
        })
    finally:
        conn.close()


@app.route("/api/users", methods=["GET"])
@require_api_key
def api_users():
    """
    GET /api/users
    Возвращает список пользователей с их товарами и тарифами.
    """
    conn = get_db()
    try:
        # Получаем всех уникальных пользователей с их товарами и тарифами
        rows = conn.execute("""
            SELECT
                tp.user_id,
                COUNT(tp.id) AS product_count,
                AVG(tp.current_price) AS avg_price,
                MIN(tp.current_price) AS min_price,
                MAX(tp.current_price) AS max_price,
                COUNT(CASE WHEN tp.notified = 1 THEN 1 END) AS notified_count,
                MAX(tp.created_at) AS last_product_added,
                ut.tariff,
                ut.expires_at
            FROM tracked_products tp
            LEFT JOIN user_tariffs ut ON tp.user_id = ut.user_id
            GROUP BY tp.user_id
            ORDER BY tp.user_id
        """).fetchall()

        users = []
        for row in rows:
            users.append({
                "user_id": row["user_id"],
                "product_count": row["product_count"],
                "avg_price": round(row["avg_price"], 2) if row["avg_price"] else 0,
                "min_price": round(row["min_price"], 2) if row["min_price"] else 0,
                "max_price": round(row["max_price"], 2) if row["max_price"] else 0,
                "notified_count": row["notified_count"],
                "last_product_added": _format_timestamp(row["last_product_added"]),
                "tariff": row["tariff"] or "free",
                "tariff_expires_at": _format_timestamp(row["expires_at"]),
            })

        return jsonify({"users": users, "total": len(users)})
    finally:
        conn.close()


@app.route("/api/products", methods=["GET"])
@require_api_key
def api_products():
    """
    GET /api/products
    Возвращает все товары. Поддерживает query-параметры:
        ?user_id=123       — фильтр по пользователю
        ?limit=50          — лимит записей (по умолчанию 100)
        ?offset=0          — смещение для пагинации
        ?notified=1        — только товары с уведомлением о падении цены
    """
    user_id = request.args.get("user_id", type=int)
    limit = request.args.get("limit", 100, type=int)
    offset = request.args.get("offset", 0, type=int)
    notified = request.args.get("notified", type=int)

    conn = get_db()
    try:
        query = "SELECT * FROM tracked_products WHERE 1=1"
        params = []

        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)

        if notified is not None:
            query += " AND notified = ?"
            params.append(notified)

        # Общее количество (до пагинации)
        count_query = query.replace("SELECT *", "SELECT COUNT(*)")
        total = conn.execute(count_query, params).fetchone()[0]

        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        rows = conn.execute(query, params).fetchall()

        products = []
        for row in rows:
            products.append({
                "id": row["id"],
                "user_id": row["user_id"],
                "url": row["url"],
                "sku": row["sku"],
                "title": row["title"],
                "current_price": row["current_price"],
                "target_price": row["target_price"],
                "alert_type": row["alert_type"],
                "alert_value": row["alert_value"],
                "last_price": row["last_price"],
                "created_at": _format_timestamp(row["created_at"]),
                "last_check": _format_timestamp(row["last_check"]),
                "notified": bool(row["notified"]),
            })

        return jsonify({
            "products": products,
            "total": total,
            "limit": limit,
            "offset": offset,
        })
    finally:
        conn.close()


@app.route("/api/export", methods=["GET"])
@require_api_key
def api_export():
    """
    GET /api/export
    Экспорт всех товаров в CSV. Поддерживает query-параметры:
        ?user_id=123  — фильтр по пользователю
        ?format=json  — вернуть JSON вместо CSV
    """
    user_id = request.args.get("user_id", type=int)
    output_format = request.args.get("format", "csv")

    conn = get_db()
    try:
        query = """
            SELECT
                tp.id, tp.user_id, tp.url, tp.sku, tp.title,
                tp.current_price, tp.target_price, tp.alert_type,
                tp.alert_value, tp.last_price, tp.created_at,
                tp.last_check, tp.notified,
                COALESCE(ut.tariff, 'free') AS tariff
            FROM tracked_products tp
            LEFT JOIN user_tariffs ut ON tp.user_id = ut.user_id
        """
        params = []

        if user_id is not None:
            query += " WHERE tp.user_id = ?"
            params.append(user_id)

        query += " ORDER BY tp.user_id, tp.created_at DESC"
        rows = conn.execute(query, params).fetchall()

        if output_format == "json":
            products = []
            for row in rows:
                products.append({
                    "id": row["id"],
                    "user_id": row["user_id"],
                    "url": row["url"],
                    "sku": row["sku"],
                    "title": row["title"],
                    "current_price": row["current_price"],
                    "target_price": row["target_price"],
                    "alert_type": row["alert_type"],
                    "alert_value": row["alert_value"],
                    "last_price": row["last_price"],
                    "created_at": _format_timestamp(row["created_at"]),
                    "last_check": _format_timestamp(row["last_check"]),
                    "notified": bool(row["notified"]),
                    "tariff": row["tariff"],
                })
            return jsonify({"products": products, "total": len(products)})

        # CSV
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "user_id", "url", "sku", "title",
            "current_price", "target_price", "alert_type", "alert_value",
            "last_price", "created_at", "last_check", "notified", "tariff"
        ])
        for row in rows:
            writer.writerow([
                row["id"], row["user_id"], row["url"], row["sku"], row["title"],
                row["current_price"], row["target_price"], row["alert_type"],
                row["alert_value"], row["last_price"],
                _format_timestamp(row["created_at"]),
                _format_timestamp(row["last_check"]),
                row["notified"], row["tariff"],
            ])

        csv_content = output.getvalue()
        output.close()

        return Response(
            csv_content,
            mimetype="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": "attachment; filename=ozon_products_export.csv"
            }
        )
    finally:
        conn.close()


# ─── Корневой эндпоинт (health check) ────────────────────────────

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Ozon Price Monitor API",
        "version": "1.0.0",
        "endpoints": [
            "GET /api/stats",
            "GET /api/users",
            "GET /api/products",
            "GET /api/export",
            "GET /dashboard",
        ],
        "auth": "X-API-Key header required",
        "docs": "Все эндпоинты (кроме / и /dashboard) требуют заголовок X-API-Key",
    })

# ─── Дашборд (HTML) ─────────────────────────────────────────────

@app.route("/dashboard", methods=["GET"])
def dashboard():
    """Раздаёт HTML-дашборд (Emerald)."""
    dashboard_html = Path(__file__).parent / "dashboard.html"
    if dashboard_html.exists():
        return dashboard_html.read_text(encoding="utf-8")
    return "<h1>dashboard.html not found</h1>", 404

# ─── Стили дашбордов ────────────────────────────────────────────

DASHBOARD_STYLES = {
    "aura": "dashboard_aura.html",
    "dala": "dashboard_dala.html",
    "authkit": "dashboard_authkit.html",
}

@app.route("/dashboard/<style>", methods=["GET"])
def dashboard_style(style):
    """Раздаёт дашборд в выбранном стиле: aura, dala, authkit"""
    filename = DASHBOARD_STYLES.get(style)
    if not filename:
        return "<h1>Неизвестный стиль</h1><p>Доступны: aura, dala, authkit, emerald</p>", 404
    html_path = Path(__file__).parent / filename
    if html_path.exists():
        return html_path.read_text(encoding="utf-8")
    return f"<h1>{filename} not found</h1>", 404

# ─── Админ-панель (HTML) ────────────────────────────────────────

@app.route("/admin", methods=["GET"])
def admin_panel():
    """Раздаёт админ-панель."""
    admin_html = Path(__file__).parent / "admin_panel.html"
    if admin_html.exists():
        return admin_html.read_text(encoding="utf-8")
    return "<h1>admin_panel.html not found</h1>", 404

# ─── Эндпоинты для админ-панели ──────────────────────────────────

@app.route("/api/dashboard", methods=["GET"])
@require_api_key
def api_dashboard():
    """Дашборд: сводка + последняя активность + топ пользователей."""
    conn = get_db()
    try:
        total_products = conn.execute("SELECT COUNT(*) FROM tracked_products").fetchone()[0]
        total_users = conn.execute("SELECT COUNT(DISTINCT user_id) FROM tracked_products").fetchone()[0]
        notified = conn.execute("SELECT COUNT(*) FROM tracked_products WHERE notified=1").fetchone()[0]
        avg_price = conn.execute("SELECT AVG(current_price) FROM tracked_products WHERE current_price>0").fetchone()[0] or 0
        total_checks = conn.execute("SELECT COUNT(*) FROM tracked_products WHERE last_check IS NOT NULL").fetchone()[0]
        
        # Последняя активность (10 записей)
        recent = conn.execute("""
            SELECT id, user_id, title, current_price, last_check 
            FROM tracked_products 
            WHERE last_check IS NOT NULL 
            ORDER BY last_check DESC LIMIT 10
        """).fetchall()
        activity = [{
            "id": r["id"], "user_id": r["user_id"], 
            "title": (r["title"] or "Товар")[:50],
            "price": r["current_price"], 
            "last_check": _format_timestamp(r["last_check"])
        } for r in recent]
        
        # Топ пользователей
        top_users = conn.execute("""
            SELECT user_id, COUNT(*) as cnt 
            FROM tracked_products GROUP BY user_id 
            ORDER BY cnt DESC LIMIT 5
        """).fetchall()
        top = [{"user_id": r["user_id"], "product_count": r["cnt"]} for r in top_users]
        
        return jsonify({
            "total_products": total_products, "total_users": total_users,
            "notified": notified, "avg_price": round(avg_price, 2),
            "total_checks": total_checks, "recent_activity": activity,
            "top_users": top
        })
    finally:
        conn.close()

@app.route("/api/user_products", methods=["GET"])
@require_api_key
def api_user_products():
    """Товары конкретного пользователя."""
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM tracked_products WHERE user_id=? ORDER BY created_at DESC",
            (user_id,)
        ).fetchall()
        products = []
        for r in rows:
            products.append({
                "id": r["id"], "user_id": r["user_id"], "url": r["url"],
                "title": r["title"], "current_price": r["current_price"],
                "target_price": r["target_price"], "alert_type": r["alert_type"],
                "alert_value": r["alert_value"], "last_price": r["last_price"],
                "created_at": _format_timestamp(r["created_at"]),
                "last_check": _format_timestamp(r["last_check"]),
                "notified": bool(r["notified"])
            })
        return jsonify({"products": products, "total": len(products)})
    finally:
        conn.close()

@app.route("/api/products/delete", methods=["POST", "OPTIONS"])
@require_api_key
def api_products_delete():
    """Удаление товара."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    product_id = data.get("product_id")
    if not product_id:
        return jsonify({"error": "product_id required"}), 400
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM tracked_products WHERE id=?", (product_id,))
        conn.commit()
        return jsonify({"success": True, "deleted": product_id})
    finally:
        conn.close()

@app.route("/api/price_history", methods=["GET"])
@require_api_key
def api_price_history():
    """История цен товара."""
    product_id = request.args.get("product_id", type=int)
    if not product_id:
        return jsonify({"error": "product_id required"}), 400
    conn = get_db()
    try:
        # Берём сам товар + его last_price/current_price как историю
        row = conn.execute(
            "SELECT * FROM tracked_products WHERE id=?", (product_id,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        
        history = []
        if row["last_price"] and row["last_price"] > 0:
            history.append({"price": row["last_price"], "date": _format_timestamp(row["created_at"])})
        history.append({"price": row["current_price"], "date": _format_timestamp(row["last_check"] or row["created_at"])})
        
        return jsonify({
            "product_id": product_id,
            "title": row["title"],
            "history": history
        })
    finally:
        conn.close()

@app.route("/api/tariffs", methods=["GET"])
@require_api_key
def api_tariffs_list():
    """Список тарифов пользователей + определения тарифов."""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT ut.user_id, ut.tariff, ut.expires_at,
                   COUNT(tp.id) as product_count
            FROM user_tariffs ut
            LEFT JOIN tracked_products tp ON ut.user_id = tp.user_id
            GROUP BY ut.user_id
            ORDER BY ut.user_id
        """).fetchall()
        tariffs = [{
            "user_id": r["user_id"], "tariff": r["tariff"],
            "expires_at": _format_timestamp(r["expires_at"]),
            "product_count": r["product_count"]
        } for r in rows]
        return jsonify({
            "tariffs": tariffs,
            "definitions": {
                "free": {"name": "Бесплатный", "max_products": 3, "check_interval": 10800},
                "premium": {"name": "Премиум", "max_products": 20, "check_interval": 1800},
                "vip": {"name": "VIP", "max_products": 100, "check_interval": 600}
            }
        })
    finally:
        conn.close()

@app.route("/api/tariffs/update", methods=["POST", "OPTIONS"])
@require_api_key
def api_tariffs_update():
    """Обновление тарифа пользователя."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    tariff = data.get("tariff")
    if not user_id or tariff not in ("free", "premium", "vip"):
        return jsonify({"error": "user_id and valid tariff required"}), 400
    conn = sqlite3.connect(str(DB_PATH))
    try:
        import time
        expires = int(time.time()) + 30 * 24 * 60 * 60
        conn.execute("""
            INSERT OR REPLACE INTO user_tariffs (user_id, tariff, expires_at)
            VALUES (?, ?, ?)
        """, (user_id, tariff, expires))
        conn.commit()
        return jsonify({"success": True, "user_id": user_id, "tariff": tariff})
    finally:
        conn.close()

@app.route("/api/tariffs/delete", methods=["POST", "OPTIONS"])
@require_api_key
def api_tariffs_delete():
    """Удаление тарифа пользователя (сброс на free)."""
    if request.method == "OPTIONS":
        return "", 204
    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "user_id required"}), 400
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute("DELETE FROM user_tariffs WHERE user_id=?", (user_id,))
        conn.commit()
        return jsonify({"success": True, "user_id": user_id})
    finally:
        conn.close()


# ─── Запуск ──────────────────────────────────────────────────────

if __name__ == "__main__":
    # Убедимся что БД существует
    if not DB_PATH.exists():
        print(f"⚠️  База данных не найдена: {DB_PATH}")
        print("   API запустится, но все запросы будут возвращать пустые данные.")
    else:
        print(f"✅ База данных: {DB_PATH}")
        print(f"   Размер: {DB_PATH.stat().st_size / 1024:.1f} KB")

    print(f"\n🔑 API Key: {API_KEY}")
    print(f"🌐 API: http://0.0.0.0:5000")
    print(f"📊 Admin Panel: http://localhost:5000/admin")
    print(f"   curl -H 'X-API-Key: *** http://localhost:5000/api/stats\n")

    app.run(host="0.0.0.0", port=5000, debug=True)