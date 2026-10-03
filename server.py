import asyncio
from datetime import datetime, timedelta
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "8976214880:AAFjnGXZwAPSEl9c0ndkJlwA2q02a5QSTIg"
SECRET_TOKEN = "Luna0501"  # Секретный ключ (такой же в MMT.lua)
SHARED_CHAT_ID = -1003923967726         # ID группы в TG с вами и подселенцами

# Скорость из скрипта MMT (Arizona RP 2026) для 10 LVL:
BASE_RATE_10_LVL = 1.227625  # BTC/час на одну карту
WARN_BEFORE_HOURS = 2.0      # Предупреждение ровно за 2 часа до фулла

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
                harvest_time TIMESTAMP
            )
        """)
        await db.commit()

# ==================== МАТЕМАТИКА MMT ====================
def calc_house_speed(online_hours: int, architect: bool, custom_pct: float) -> tuple[float, float]:
    """
    Расчёт эффективной скорости одной карты 10 LVL с бонусами дома:
    - Онлайн (0-24ч): до +20%
    - Набор архитектора (для творчества): +30%
    - Кастомный процент дома
    """
    bonus = (20.0 * (min(24, max(0, online_hours)) / 24.0))
    if architect:
        bonus += 30.0
    bonus += max(0.0, custom_pct)
    
    speed = BASE_RATE_10_LVL * (1.0 + bonus / 100.0)
    return speed, bonus

def get_fill_times(speed: float):
    """Точное время заполнения (в часах) для карт 12 и 24 BTC"""
    return 12.0 / speed, 24.0 / speed

def format_duration(hours_float: float) -> str:
    total_seconds = max(0, int(hours_float * 3600))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    return f"{h} ч {m} мин"

# ==================== МЕНЮ TELEGRAM ====================
def get_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статус ферм (Live)", callback_data="status")],
        [InlineKeyboardButton(text="⏳ Точный график сбора", callback_data="next_time")],
        [InlineKeyboardButton(text="📜 История сборов", callback_data="history")],
        [InlineKeyboardButton(text="⚙️ Настройки бонусов домов", callback_data="house_settings")]
    ])

@dp.message(Command("start", "menu"))
async def cmd_start(message: types.Message):
    await message.answer(
        "⚡ **Майнинг-контроллер Arizona RP (Точный расчет MMT)**\n\n"
        f"• Скорость 10 LVL: `{BASE_RATE_10_LVL} BTC/ч`\n"
        f"• Оповещение: **за 2 часа** до переполнения.\n"
        "Синхронизировано с действиями всех подселенцев.",
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
        speed, bonus = calc_house_speed(on_h, bool(arch), cust)
        h12, h24 = get_fill_times(speed)

        elapsed = (now - ltime).total_seconds() / 3600.0
        
        # Расчет намайненного
        cur_12 = min(12.0, elapsed * speed)
        cur_24 = min(24.0, elapsed * speed)
        
        p12 = min(1.0, elapsed / h12)
        p24 = min(1.0, elapsed / h24)

        rem12 = max(0.0, h12 - elapsed)
        rem24 = max(0.0, h24 - elapsed)

        arch_tag = "🏛 Архитектор (+30%)" if arch else "Без архитектора"

        text += (
            f"🏠 **Дом №{hid}** [{arch_tag} | Онлайн: {on_h}ч]\n"
            f"⚡ Скорость: `{round(speed, 3)} BTC/ч` (карта) [бонус: +{int(bonus)}%]\n"
            f"👤 Крайний сборщик: `{nick}`\n\n"
            f"📦 **Карты 12 BTC:**\n"
            f"`[{'█'*int(p12*10)}{'░'*(10-int(p12*10))}]` **{cur_12:.2f}** / 12.00 BTC ({int(p12*100)}%)\n"
            f"⏳ Осталось: **{format_duration(rem12)}**\n\n"
            f"📦 **Карты 24 BTC:**\n"
            f"`[{'█'*int(p24*10)}{'░'*(10-int(p24*10))}]` **{cur_24:.2f}** / 24.00 BTC ({int(p24*100)}%)\n"
            f"⏳ Осталось: **{format_duration(rem24)}**\n"
            f"────────────────────\n"
        )

    await query.message.answer(text, reply_markup=get_main_menu(), parse_mode="Markdown")
    await query.answer()

@dp.callback_query(F.data == "next_time")
async def cb_next_time(query: types.CallbackQuery):
    now = datetime.now()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT house_id, last_harvest_time, online_hours, architect_set, custom_bonus FROM houses") as cur:
            houses = await cur.fetchall()

    text = "⏳ **Точное расписание заполнения полок:**\n\n"
    for hid, ltime_str, on_h, arch, cust in houses:
        ltime = datetime.fromisoformat(ltime_str)
        speed, _ = calc_house_speed(on_h, bool(arch), cust)
        h12, h24 = get_fill_times(speed)

        t12 = ltime + timedelta(hours=h12)
        t24 = ltime + timedelta(hours=h24)

        rem12 = max(0.0, (t12 - now).total_seconds() / 3600.0)
        rem24 = max(0.0, (t24 - now).total_seconds() / 3600.0)

        warn_t12 = t12 - timedelta(hours=WARN_BEFORE_HOURS)

        text += (
            f"🏠 **Дом №{hid}**:\n"
            f"• **12 BTC (фулл)**: {t12.strftime('%d.%m в %H:%M')} (через {format_duration(rem12)})\n"
            f"  └ ⚠️ Пред за 2ч: {warn_t12.strftime('%H:%M')}\n"
            f"• **24 BTC (фулл)**: {t24.strftime('%d.%m в %H:%M')} (через {format_duration(rem24)})\n\n"
        )

    await query.message.answer(text, reply_markup=get_main_menu(), parse_mode="Markdown")
    await query.answer()

@dp.callback_query(F.data == "history")
async def cb_history(query: types.CallbackQuery):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT house_id, player_nick, btc, harvest_time FROM history ORDER BY id DESC LIMIT 10") as cur:
            rows = await cur.fetchall()

    if not rows:
        await query.message.answer("История пуста.")
        await query.answer()
        return

    text = "📜 **История сборов прибыли:**\n\n"
    for hid, nick, btc, t_str in rows:
        t = datetime.fromisoformat(t_str).strftime('%d.%m %H:%M')
        text += f"• `{t}` — Дом **№{hid}** | `{nick}` забрал `{btc:.2f} BTC`\n"

    await query.message.answer(text, reply_markup=get_main_menu(), parse_mode="Markdown")
    await query.answer()

# ==================== НАСТРОЙКИ БОНУСОВ ДОМА ====================
@dp.callback_query(F.data == "house_settings")
async def cb_house_settings(query: types.CallbackQuery):
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT house_id, architect_set, online_hours FROM houses") as cur:
            houses = await cur.fetchall()

    kb = []
    for hid, arch, on_h in houses:
        arch_status = "ВКЛ" if arch else "ВЫКЛ"
        kb.append([InlineKeyboardButton(text=f"Дом №{hid} (Архитектор: {arch_status})", callback_data=f"toggle_arch_{hid}")])
        kb.append([InlineKeyboardButton(text=f"Онлайн дома №{hid}: {on_h} ч (+{int(20*(on_h/24))}%)", callback_data=f"cycle_online_{hid}")])
    
    kb.append([InlineKeyboardButton(text="⬅️ Назад в меню", callback_data="back_main")])

    await query.message.answer("⚙️ **Настройки бонусов майнинга (как в MMT):**\nНажимайте на кнопки для переключения:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await query.answer()

@dp.callback_query(F.data.startswith("toggle_arch_"))
async def cb_toggle_arch(query: types.CallbackQuery):
    hid = int(query.data.split("_")[2])
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE houses SET architect_set = CASE WHEN architect_set = 1 THEN 0 ELSE 1 END WHERE house_id = ?", (hid,))
        await db.commit()
    await cb_house_settings(query)

@dp.callback_query(F.data.startswith("cycle_online_"))
async def cb_cycle_online(query: types.CallbackQuery):
    hid = int(query.data.split("_")[2])
    async with aiosqlite.connect(DB_FILE) as db:
        # Циклическое переключение: 0 -> 8 -> 16 -> 24 -> 0
        await db.execute("UPDATE houses SET online_hours = (online_hours + 8) % 32 WHERE house_id = ?", (hid,))
        await db.commit()
    await cb_house_settings(query)

@dp.callback_query(F.data == "back_main")
async def cb_back_main(query: types.CallbackQuery):
    await query.message.delete()
    await cmd_start(query.message)
    await query.answer()

# ==================== МОНИТОРИНГ И ПУШИ (ПРЕД ЗА 2 ЧАСА) ====================
async def alert_loop():
    while True:
        try:
            now = datetime.now()
            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute(
                    "SELECT house_id, last_harvest_time, online_hours, architect_set, custom_bonus, warned_12, full_12, warned_24, full_24 FROM houses"
                ) as cur:
                    houses = await cur.fetchall()

                for hid, ltime_str, on_h, arch, cust, w12, f12, w24, f24 in houses:
                    ltime = datetime.fromisoformat(ltime_str)
                    speed, _ = calc_house_speed(on_h, bool(arch), cust)
                    h12, h24 = get_fill_times(speed)
                    elapsed = (now - ltime).total_seconds() / 3600.0

                    t12 = ltime + timedelta(hours=h12)
                    t24 = ltime + timedelta(hours=h24)

                    # ----- 12 BTC: ПРЕДУПРЕЖДЕНИЕ ЗА 2 ЧАСА -----
                    if elapsed >= (h12 - WARN_BEFORE_HOURS) and not w12:
                        rem_m = int(max(0, (t12 - now).total_seconds() // 60))
                        await bot.send_message(
                            SHARED_CHAT_ID,
                            f"⚠️ **[Дом №{hid}] Внимание!**\n"
                            f"Стойка **12 BTC** заполнится через **{rem_m // 60} ч {rem_m % 60} мин** (в `{t12.strftime('%H:%M')}`)!\n"
                            f"Кто рядом на сервере — приготовьтесь забрать прибыль."
                        )
                        await db.execute("UPDATE houses SET warned_12 = 1 WHERE house_id = ?", (hid,))

                    # ----- 12 BTC: 100% ЗАПОЛНЕНО -----
                    if elapsed >= h12 and not f12:
                        await bot.send_message(
                            SHARED_CHAT_ID,
                            f"🚨 **[Дом №{hid}] АЛАРМ!**\n"
                            f"Карты на **12 BTC** ПОЛНОСТЬЮ ЗАПОЛНЕНЫ (12.00 / 12.00 BTC)!\n"
                            f"Майнинг на них остановлен. Срочно снимите биткоины!"
                        )
                        await db.execute("UPDATE houses SET full_12 = 1 WHERE house_id = ?", (hid,))

                    # ----- 24 BTC: ПРЕДУПРЕЖДЕНИЕ ЗА 2 ЧАСА -----
                    if elapsed >= (h24 - WARN_BEFORE_HOURS) and not w24:
                        rem_m = int(max(0, (t24 - now).total_seconds() // 60))
                        await bot.send_message(
                            SHARED_CHAT_ID,
                            f"⚠️ **[Дом №{hid}] Внимание!**\n"
                            f"Стойка **24 BTC** заполнится через **{rem_m // 60} ч {rem_m % 60} мин** (в `{t24.strftime('%H:%M')}`)!"
                        )
                        await db.execute("UPDATE houses SET warned_24 = 1 WHERE house_id = ?", (hid,))

                    # ----- 24 BTC: 100% ЗАПОЛНЕНО -----
                    if elapsed >= h24 and not f24:
                        await bot.send_message(
                            SHARED_CHAT_ID,
                            f"🚨 **[Дом №{hid}] АЛАРМ!**\n"
                            f"Карты на **24 BTC** ПОЛНОСТЬЮ ЗАПОЛНЕНЫ (24.00 / 24.00 BTC)!"
                        )
                        await db.execute("UPDATE houses SET full_24 = 1 WHERE house_id = ?", (hid,))

                await db.commit()
        except Exception as e:
            print(f"[Error in alert_loop]: {e}")
        await asyncio.sleep(30)  # Высокая точность проверки (каждые 30 секунд)

# ==================== WEB ENDPOINTS ====================
async def ping_handler(request):
    return web.json_response({"status": "ok", "time": datetime.now().isoformat()})

async def harvest_handler(request):
    if request.query.get("token") != SECRET_TOKEN:
        return web.Response(status=403, text="Forbidden")

    try:
        house_id = int(request.query.get("house", 0))
        nick = request.query.get("nick", "Игрок")
        btc = float(request.query.get("btc", 0.0))
    except ValueError:
        return web.Response(status=400, text="Bad params")

    now = datetime.now()
    now_str = now.isoformat()

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO houses (house_id, last_harvest_time, last_harvest_by, last_btc, warned_12, full_12, warned_24, full_24)
            VALUES (?, ?, ?, ?, 0, 0, 0, 0)
            ON CONFLICT(house_id) DO UPDATE SET
                last_harvest_time = excluded.last_harvest_time,
                last_harvest_by = excluded.last_harvest_by,
                last_btc = excluded.last_btc,
                warned_12 = 0, full_12 = 0, warned_24 = 0, full_24 = 0
        """, (house_id, now_str, nick, btc))

        await db.execute("INSERT INTO history (house_id, player_nick, btc, harvest_time) VALUES (?, ?, ?, ?)",
                         (house_id, nick, btc, now_str))
        await db.commit()

    # Уведомление в общую группу подселенцев
    await bot.send_message(
        SHARED_CHAT_ID,
        f"⚡ **Сбор биткоинов зафиксирован!**\n\n"
        f"🏠 Дом: **№{house_id}**\n"
        f"👤 Снял: `{nick}`\n"
        f"💰 Получено: `{btc:.2f} BTC`\n"
        f"🕒 Время: `{now.strftime('%H:%M:%S')}`\n\n"
        f"Таймеры сброшены. Следующий сбор 12 BTC через ~7–9 часов.",
        parse_mode="Markdown"
    )
    return web.json_response({"status": "success"})

# ==================== ЗАПУСК ====================
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
    print("Сервер запущен (порт 8080). Polling бота активен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
