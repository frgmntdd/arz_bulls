import asyncio
from datetime import datetime, timedelta
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "ВАШ_ТОКЕН_ОТ_BOTFATHER"
SECRET_TOKEN = "SUPER_SECRET_KEY_12345"  # Должен совпадать с TG_CONFIG в Lua!
SHARED_CHAT_ID = -1001234567890         # ID вашей общей группы с подселенцами

# Базовая скорость одной видеокарты 10 LVL (BTC в час)
BASE_RATE_10_LVL = 0.25 
WARN_BEFORE_HOURS = 4  # Предупреждать за 4 часа до фулла

DB_FILE = "mining_data.db"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ==================== БАЗА ДАННЫХ ====================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS houses (
                house_id INTEGER PRIMARY KEY,
                last_harvest_time TIMESTAMP,
                last_harvest_by TEXT,
                last_btc REAL DEFAULT 0,
                last_asc REAL DEFAULT 0,
                online_hours INTEGER DEFAULT 0,
                architect_set INTEGER DEFAULT 0,
                custom_bonus REAL DEFAULT 0,
                warned_12 INTEGER DEFAULT 0,
                full_12 INTEGER DEFAULT 0,
                warned_24 INTEGER DEFAULT 0,
                full_24 INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                house_id INTEGER,
                player_nick TEXT,
                btc REAL,
                asc_val REAL,
                harvest_time TIMESTAMP
            )
        """)
        await db.commit()

# ==================== МАТЕМАТИКА MMT ====================
def calc_mining_speed(online_hours: int, architect: bool, custom_pct: float) -> float:
    """Расчет скорости добычи с учетом всех бонусов из MMT"""
    bonus = (20.0 * (min(24, max(0, online_hours)) / 24.0))
    if architect:
        bonus += 30.0
    bonus += max(0.0, custom_pct)
    return BASE_RATE_10_LVL * (1.0 + bonus / 100.0)

def get_fill_hours(speed: float):
    # Время заполнения карт на 12 и 24 BTC
    hours_12 = 12.0 / speed
    hours_24 = 24.0 / speed
    return hours_12, hours_24

# ==================== МЕНЮ TELEGRAM ====================
def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статус ферм", callback_data="status")],
        [InlineKeyboardButton(text="⏳ Когда следующий сбор?", callback_data="next_time")],
        [InlineKeyboardButton(text="📜 История сборов", callback_data="history")]
    ])

@dp.message(Command("start", "menu"))
async def cmd_start(message: types.Message):
    await message.answer(
        "⚡ **Майнинг-контроллер Arizona RP (MMT Core)**\n\n"
        "Бот отслеживает сборы со всех стоек и рассчитывает точное время заполнения для карт 10 LVL.",
        reply_markup=get_main_menu(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "status")
async def cb_status(query: types.CallbackQuery):
    now = datetime.now()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT house_id, last_harvest_time, last_harvest_by, online_hours, architect_set, custom_bonus FROM houses") as cur:
            houses = await cur.fetchall()

    if not houses:
        await query.message.answer("Записей о домах пока нет. Ждём первого сбора из игры!")
        await query.answer()
        return

    text = "📊 **Текущее состояние ферм:**\n\n"
    for hid, ltime_str, nick, on_h, arch, cust in houses:
        ltime = datetime.fromisoformat(ltime_str)
        speed = calc_mining_speed(on_h, bool(arch), cust)
        h12, h24 = get_fill_hours(speed)

        elapsed = (now - ltime).total_seconds() / 3600.0
        p12 = min(1.0, elapsed / h12)
        p24 = min(1.0, elapsed / h24)

        cur_12 = round(p12 * 12.0, 2)
        cur_24 = round(p24 * 24.0, 2)

        arch_mark = " 🏛 Набор архитектора (+30%)" if arch else ""
        text += (
            f"🏠 **Дом №{hid}**{arch_mark}\n"
            f"├ Крайний сбор: `{nick}`\n"
            f"├ Скорость: `~{round(speed, 3)} BTC/ч` (карта)\n"
            f"├ Карты 12 BTC: `[{'█'*int(p12*10)}{'░'*(10-int(p12*10))}]` {cur_12}/12 BTC ({int(p12*100)}%)\n"
            f"└ Карты 24 BTC: `[{'█'*int(p24*10)}{'░'*(10-int(p24*10))}]` {cur_24}/24 BTC ({int(p24*100)}%)\n\n"
        )

    await query.message.answer(text, reply_markup=get_main_menu(), parse_mode="Markdown")
    await query.answer()

@dp.callback_query(F.data == "next_time")
async def cb_next_time(query: types.CallbackQuery):
    now = datetime.now()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT house_id, last_harvest_time, online_hours, architect_set, custom_bonus FROM houses") as cur:
            houses = await cur.fetchall()

    text = "⏳ **График следующего сбора:**\n\n"
    for hid, ltime_str, on_h, arch, cust in houses:
        ltime = datetime.fromisoformat(ltime_str)
        speed = calc_mining_speed(on_h, bool(arch), cust)
        h12, h24 = get_fill_hours(speed)

        t12 = ltime + timedelta(hours=h12)
        t24 = ltime + timedelta(hours=h24)

        rem12 = max(0.0, (t12 - now).total_seconds() / 3600.0)
        rem24 = max(0.0, (t24 - now).total_seconds() / 3600.0)

        text += (
            f"🏠 **Дом №{hid}**:\n"
            f"• **12 BTC**: {t12.strftime('%d.%m в %H:%M')} (осталось ~{int(rem12)} ч {int((rem12%1)*60)} м)\n"
            f"• **24 BTC**: {t24.strftime('%d.%m в %H:%M')} (осталось ~{int(rem24)} ч {int((rem24%1)*60)} м)\n\n"
        )

    await query.message.answer(text, reply_markup=get_main_menu(), parse_mode="Markdown")
    await query.answer()

@dp.callback_query(F.data == "history")
async def cb_history(query: types.CallbackQuery):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT house_id, player_nick, btc, harvest_time FROM history ORDER BY id DESC LIMIT 10") as cur:
            rows = await cur.fetchall()

    if not rows:
        await query.message.answer("История сборов пуста.")
        await query.answer()
        return

    text = "📜 **Последние сборы (MMT Sync):**\n\n"
    for hid, nick, btc, t_str in rows:
        t = datetime.fromisoformat(t_str).strftime('%d.%m %H:%M')
        text += f"• `{t}` — Дом **№{hid}** | `{nick}` снял `{btc:.2f} BTC`\n"

    await query.message.answer(text, reply_markup=get_main_menu(), parse_mode="Markdown")
    await query.answer()

# ==================== ФОНОВЫЕ НАПОМИНАНИЯ ====================
async def alert_loop():
    while True:
        try:
            now = datetime.now()
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT house_id, last_harvest_time, online_hours, architect_set, custom_bonus, warned_12, full_12, warned_24, full_24 FROM houses") as cur:
                    houses = await cur.fetchall()

                for hid, ltime_str, on_h, arch, cust, w12, f12, w24, f24 in houses:
                    ltime = datetime.fromisoformat(ltime_str)
                    speed = calc_mining_speed(on_h, bool(arch), cust)
                    h12, h24 = get_fill_hours(speed)
                    elapsed = (now - ltime).total_seconds() / 3600.0

                    # 12 BTC: Предупреждение за 4 часа
                    if elapsed >= (h12 - WARN_BEFORE_HOURS) and not w12:
                        await bot.send_message(SHARED_CHAT_ID, f"⚠️ **[Дом №{hid}]** Карты **12 BTC** заполнятся через ~{WARN_BEFORE_HOURS} ч! Приготовьтесь снять прибыль.")
                        await db.execute("UPDATE houses SET warned_12 = 1 WHERE house_id = ?", (hid,))

                    # 12 BTC: 100% заполнено
                    if elapsed >= h12 and not f12:
                        await bot.send_message(SHARED_CHAT_ID, f"🚨 **[Дом №{hid}] ВНИМАНИЕ!** Карты на **12 BTC** ПОЛНОСТЬЮ ЗАПОЛНЕНЫ! Майнинг встал, снимите битки!")
                        await db.execute("UPDATE houses SET full_12 = 1 WHERE house_id = ?", (hid,))

                    # 24 BTC: 100% заполнено
                    if elapsed >= h24 and not f24:
                        await bot.send_message(SHARED_CHAT_ID, f"🔥 **[Дом №{hid}] ВНИМАНИЕ!** Карты на **24 BTC** ПОЛНОСТЬЮ ЗАПОЛНЕНЫ!")
                        await db.execute("UPDATE houses SET full_24 = 1 WHERE house_id = ?", (hid,))

                await db.commit()
        except Exception as e:
            print(f"[Error in alert_loop]: {e}")
        await asyncio.sleep(60)

# ==================== ВЕБ-ЭНДПОИНТЫ (AIOHTTP) ====================
async def ping_handler(request):
    """Каждые 15 минут пингуется сервисом UptimeRobot"""
    return web.json_response({"status": "alive", "server_time": datetime.now().isoformat()})

async def harvest_handler(request):
    """Сюда шлёт запрос MMT при сборе"""
    if request.query.get("token") != SECRET_TOKEN:
        return web.Response(status=403, text="Forbidden")

    try:
        house_id = int(request.query.get("house", 0))
        nick = request.query.get("nick", "Игрок")
        btc = float(request.query.get("btc", 0.0))
        asc_val = float(request.query.get("asc", 0.0))
    except ValueError:
        return web.Response(status=400, text="Bad params")

    now = datetime.now()
    now_str = now.isoformat()

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO houses (house_id, last_harvest_time, last_harvest_by, last_btc, last_asc, warned_12, full_12, warned_24, full_24)
            VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0)
            ON CONFLICT(house_id) DO UPDATE SET
                last_harvest_time = excluded.last_harvest_time,
                last_harvest_by = excluded.last_harvest_by,
                last_btc = excluded.last_btc,
                last_asc = excluded.last_asc,
                warned_12 = 0, full_12 = 0, warned_24 = 0, full_24 = 0
        """, (house_id, now_str, nick, btc, asc_val))

        await db.execute("INSERT INTO history (house_id, player_nick, btc, asc_val, harvest_time) VALUES (?, ?, ?, ?, ?)",
                         (house_id, nick, btc, asc_val, now_str))
        await db.commit()

    # Оповещение в общую группу подселенцев
    await bot.send_message(
        SHARED_CHAT_ID,
        f"⚡ **Сбор биткоинов завершён!**\n\n"
        f"🏠 Дом: **№{house_id}**\n"
        f"👤 Собрал: `{nick}`\n"
        f"💰 Снято: `{btc:.2f} BTC`\n"
        f"🕒 Время: `{now.strftime('%H:%M:%S')}`\n\n"
        f"Все таймеры дома сброшены на 0.",
        parse_mode="Markdown"
    )
    return web.json_response({"status": "ok"})

# ==================== ЗАПУСК ПРИЛОЖЕНИЯ ====================
async def main():
    await init_db()

    app = web.Application()
    app.router.add_get("/", ping_handler)
    app.router.add_get("/ping", ping_handler)
    app.router.add_get("/api/harvest", harvest_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    asyncio.create_task(alert_loop())
    print("Web-сервер запущен на порту 8080. Запуск Telegram Polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
