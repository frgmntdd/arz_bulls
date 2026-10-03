import asyncio
from datetime import datetime, timedelta
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "ВАШ_ТОКЕН_BOTFATHER"
SECRET_TOKEN = "SUPER_SECRET_KEY_12345"  # Совпадает с TG_CONFIG в Lua!
SHARED_CHAT_ID = -1001234567890         # ID группы в TG с вами и подселенцами

BASE_RATE_10_LVL = 1.227625  # BTC/час на одну карту (Arizona RP)
WARN_BEFORE_HOURS = 2.0      # Пред за 2 часа
HEARTBEAT_TIMEOUT_SEC = 300  # Если нет сигналов 5 минут — игрок считается вышедшим
DASHBOARD_REFRESH_MIN = 10   # Интервал авто-редактирования сообщения

DB_FILE = "mining_data.db"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Кэш онлайна жильцов: {house_id: {player_nick: last_seen_timestamp}}
online_players: dict[int, dict[str, float]] = {}
# ID сообщения дашборда для авто-редактирования
live_dashboard_message_id = None
last_calc_time = datetime.now()

# ==================== БАЗА ДАННЫХ ====================
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS houses (
                house_id INTEGER PRIMARY KEY,
                last_harvest_time TIMESTAMP,
                last_harvest_by TEXT,
                accumulated_btc REAL DEFAULT 0.0,
                architect_set INTEGER DEFAULT 0,
                custom_bonus REAL DEFAULT 0.0,
                warned_12 INTEGER DEFAULT 0,
                full_12 INTEGER DEFAULT 0,
                warned_24 INTEGER DEFAULT 0,
                full_24 INTEGER DEFAULT 0,
                dashboard_msg_id INTEGER DEFAULT 0
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

# ==================== МАТЕМАТИКА ====================
def is_house_online(house_id: int) -> tuple[bool, list[str]]:
    now = datetime.now().timestamp()
    house_users = online_players.get(house_id, {})
    active = [nick for nick, last_seen in house_users.items() if (now - last_seen) < HEARTBEAT_TIMEOUT_SEC]
    return len(active) > 0, active

def get_current_speed(house_id: int, architect: bool, custom_pct: float) -> tuple[float, float, bool, list[str]]:
    is_online, active_nicks = is_house_online(house_id)
    bonus = 0.0
    if architect:
        bonus += 30.0
    if is_online:
        bonus += 20.0  # +20% за нахождение хотя бы 1 жильца в сети
    bonus += max(0.0, custom_pct)

    speed = BASE_RATE_10_LVL * (1.0 + bonus / 100.0)
    return speed, bonus, is_online, active_nicks

def format_duration(hours_float: float) -> str:
    total_seconds = max(0, int(hours_float * 3600))
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    return f"{h} ч {m} мин"

# ==================== ГЕНЕРАТОР ДАШБОРДА ====================
async def render_dashboard_text():
    now = datetime.now()
    async with aiosqlite.connect(DB_FILE) as db:
        async with db.execute("SELECT house_id, last_harvest_time, last_harvest_by, accumulated_btc, architect_set, custom_bonus FROM houses") as cur:
            houses = await cur.fetchall()

    if not houses:
        return "📊 **Фермы ещё не зарегистрированы.**\nСделайте первый сбор в игре или нажмите кнопку ниже.", None

    text = f"📊 **АКТУАЛЬНОЕ СОСТОЯНИЕ ФЕРМ**\n_(Автообновление каждые {DASHBOARD_REFRESH_MIN} мин)_\n\n"
    kb = []

    for hid, ltime_str, nick, acc_btc, arch, cust in houses:
        speed, bonus, is_online, nicks = get_current_speed(hid, bool(arch), cust)
        
        cur_12 = min(12.0, acc_btc)
        cur_24 = min(24.0, acc_btc)
        p12 = cur_12 / 12.0
        p24 = cur_24 / 24.0

        rem12 = max(0.0, (12.0 - cur_12) / speed)
        rem24 = max(0.0, (24.0 - cur_24) / speed)

        online_badge = f"🟢 **ОНЛАЙН (+20% активен!)**\n  └ В игре: `{', '.join(nicks)}`" if is_online else "⚪ **ОФЛАЙН** (базовая скорость)"
        arch_badge = " | 🏛 Архитектор (+30%)" if arch else ""

        text += (
            f"🏠 **Дом №{hid}**{arch_badge}\n"
            f"Статус: {online_badge}\n"
            f"⚡ Скорость карты: `{round(speed, 3)} BTC/ч` (+{int(bonus)}%)\n\n"
            f"📦 **Карты 12 BTC:**\n"
            f"`[{'█'*int(p12*10)}{'░'*(10-int(p12*10))}]` **{cur_12:.2f}** / 12.00 BTC ({int(p12*100)}%)\n"
            f"⏳ До фулла: **{format_duration(rem12)}**\n\n"
            f"📦 **Карты 24 BTC:**\n"
            f"`[{'█'*int(p24*10)}{'░'*(10-int(p24*10))}]` **{cur_24:.2f}** / 24.00 BTC ({int(p24*100)}%)\n"
            f"⏳ До фулла: **{format_duration(rem24)}**\n\n"
            f"👤 Крайний сбор: `{nick}`\n"
            f"🕒 Обновлено: `{now.strftime('%H:%M:%S')}`\n"
            f"────────────────────\n"
        )
        kb.append([InlineKeyboardButton(text=f"🔄 Собрал вручную (Дом №{hid})", callback_data=f"ask_reset_{hid}")])

    kb.append([InlineKeyboardButton(text="⚡ Обновить сейчас", callback_data="manual_refresh")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb)

# ==================== ОБРАБОТЧИКИ TELEGRAM ====================
@dp.message(Command("start", "dashboard"))
async def cmd_dashboard(message: types.Message):
    global live_dashboard_message_id
    text, reply_markup = await render_dashboard_text()
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode="Markdown")
    live_dashboard_message_id = sent.message_id

@dp.callback_query(F.data == "manual_refresh")
async def cb_manual_refresh(query: types.CallbackQuery):
    text, reply_markup = await render_dashboard_text()
    try:
        await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    except TelegramBadRequest:
        pass
    await query.answer("Обновлено!")

# --- ЗАЩИТА ОТ СЛУЧАЙНОГО НАЖАТИЯ (МИСС-КЛИКА) ---
@dp.callback_query(F.data.startswith("ask_reset_"))
async def cb_ask_reset(query: types.CallbackQuery):
    hid = int(query.data.split("_")[2])
    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, сбросить!", callback_data=f"confirm_reset_{hid}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_reset")
        ]
    ])
    await query.message.answer(
        f"⚠️ **Подтверждение сброса таймера!**\n\nВы уверены, что хотите обнулить таймер дома **№{hid}**?\n"
        f"Нажимайте только если вы действительно **забрали все биткоины** в игре.",
        reply_markup=confirm_kb,
        parse_mode="Markdown"
    )
    await query.answer()

@dp.callback_query(F.data == "cancel_reset")
async def cb_cancel_reset(query: types.CallbackQuery):
    await query.message.delete()
    await query.answer("Отменено")

@dp.callback_query(F.data.startswith("confirm_reset_"))
async def cb_confirm_reset(query: types.CallbackQuery):
    hid = int(query.data.split("_")[2])
    await query.message.delete()
    
    user_name = query.from_user.full_name or query.from_user.username or "Жилец"
    now_str = datetime.now().isoformat()

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            UPDATE houses SET
                last_harvest_time = ?,
                last_harvest_by = ?,
                accumulated_btc = 0.0,
                warned_12 = 0, full_12 = 0, warned_24 = 0, full_24 = 0
            WHERE house_id = ?
        """, (now_str, f"{user_name} (ТГ-сброс)", hid))
        
        await db.execute("INSERT INTO history (house_id, player_nick, btc, harvest_time) VALUES (?, ?, ?, ?)",
                         (hid, f"{user_name} (ТГ)", 0.0, now_str))
        await db.commit()

    await bot.send_message(
        SHARED_CHAT_ID,
        f"🔄 **Ручной сброс фермы!**\n\n👤 Пользователь: **{user_name}** сбросил таймеры Дома **№{hid}** через Telegram.\nТаймеры запущены с 0.",
        parse_mode="Markdown"
    )
    await query.answer("Таймер сброшен!")
    await update_live_dashboard()

# ==================== ФОНОВЫЙ ЦИКЛ: ПОДСЧЕТ И АВТООБНОВЛЕНИЕ ====================
async def update_live_dashboard():
    global live_dashboard_message_id
    if not live_dashboard_message_id:
        return
    text, reply_markup = await render_dashboard_text()
    try:
        await bot.edit_message_text(text, chat_id=SHARED_CHAT_ID, message_id=live_dashboard_message_id, reply_markup=reply_markup, parse_mode="Markdown")
    except TelegramBadRequest:
        pass
    except Exception as e:
        print(f"[Dashboard update error]: {e}")

async def main_engine_loop():
    global last_calc_time
    last_dashboard_edit = datetime.now()

    while True:
        try:
            now = datetime.now()
            dt_seconds = (now - last_calc_time).total_seconds()
            last_calc_time = now

            async with aiosqlite.connect(DB_FILE) as db:
                async with db.execute("SELECT house_id, accumulated_btc, architect_set, custom_bonus, warned_12, full_12, warned_24, full_24 FROM houses") as cur:
                    houses = await cur.fetchall()

                for hid, acc_btc, arch, cust, w12, f12, w24, f24 in houses:
                    speed, _, _, _ = get_current_speed(hid, bool(arch), cust)
                    
                    # Прибавляем намайненное за прошедшие секунды
                    new_acc = acc_btc + speed * (dt_seconds / 3600.0)
                    
                    rem12 = max(0.0, (12.0 - new_acc) / speed)
                    rem24 = max(0.0, (24.0 - new_acc) / speed)

                    # --- АЛЕРТЫ 12 BTC ---
                    if rem12 <= WARN_BEFORE_HOURS and not w12 and new_acc < 12.0:
                        rem_m = int(rem12 * 60)
                        await bot.send_message(
                            SHARED_CHAT_ID,
                            f"⚠️ **[Дом №{hid}] Внимание!**\nКарты **12 BTC** заполнятся через **{rem_m // 60} ч {rem_m % 60} мин**!\nКто на сервере — приготовьтесь снять прибыль.",
                            parse_mode="Markdown"
                        )
                        w12 = 1

                    if new_acc >= 12.0 and not f12:
                        await bot.send_message(
                            SHARED_CHAT_ID,
                            f"🚨 **[Дом №{hid}] АЛАРМ!**\nКарты на **12 BTC** ПОЛНОСТЬЮ ЗАПОЛНЕНЫ (12.00 / 12.00 BTC)!\nМайнинг встал, снимите прибыль!",
                            parse_mode="Markdown"
                        )
                        f12 = 1

                    # --- АЛЕРТЫ 24 BTC ---
                    if rem24 <= WARN_BEFORE_HOURS and not w24 and new_acc < 24.0:
                        rem_m = int(rem24 * 60)
                        await bot.send_message(
                            SHARED_CHAT_ID,
                            f"⚠️ **[Дом №{hid}] Внимание!**\nКарты **24 BTC** заполнятся через **{rem_m // 60} ч {rem_m % 60} мин**!",
                            parse_mode="Markdown"
                        )
                        w24 = 1

                    if new_acc >= 24.0 and not f24:
                        await bot.send_message(
                            SHARED_CHAT_ID,
                            f"🚨 **[Дом №{hid}] АЛАРМ!**\nКарты на **24 BTC** ПОЛНОСТЬЮ ЗАПОЛНЕНЫ (24.00 / 24.00 BTC)!",
                            parse_mode="Markdown"
                        )
                        f24 = 1

                    await db.execute("""
                        UPDATE houses SET
                            accumulated_btc = ?,
                            warned_12 = ?, full_12 = ?, warned_24 = ?, full_24 = ?
                        WHERE house_id = ?
                    """, (new_acc, w12, f12, w24, f24, hid))

                await db.commit()

            # Авто-редактирование дашборда раз в 10 минут
            if (now - last_dashboard_edit).total_seconds() >= (DASHBOARD_REFRESH_MIN * 60):
                last_dashboard_edit = now
                await update_live_dashboard()

        except Exception as e:
            print(f"[Error in engine loop]: {e}")

        await asyncio.sleep(20)  # Шаг вычисления 20 секунд

# ==================== ВЕБ-ЭНДПОИНТЫ ====================
async def ping_handler(request):
    return web.json_response({"status": "ok"})

async def heartbeat_handler(request):
    """Принимает сигнал онлайна от любого подселенца"""
    if request.query.get("token") != SECRET_TOKEN:
        return web.Response(status=403, text="Forbidden")
    try:
        hid = int(request.query.get("house", 0))
        nick = request.query.get("nick", "Игрок")
    except ValueError:
        return web.Response(status=400, text="Bad params")

    online_players.setdefault(hid, {})[nick] = datetime.now().timestamp()
    return web.json_response({"status": "online_tracked"})

async def harvest_handler(request):
    """Сбор через MMT"""
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
            INSERT INTO houses (house_id, last_harvest_time, last_harvest_by, accumulated_btc, warned_12, full_12, warned_24, full_24)
            VALUES (?, ?, ?, 0.0, 0, 0, 0, 0)
            ON CONFLICT(house_id) DO UPDATE SET
                last_harvest_time = excluded.last_harvest_time,
                last_harvest_by = excluded.last_harvest_by,
                accumulated_btc = 0.0,
                warned_12 = 0, full_12 = 0, warned_24 = 0, full_24 = 0
        """, (house_id, now_str, nick))

        await db.execute("INSERT INTO history (house_id, player_nick, btc, harvest_time) VALUES (?, ?, ?, ?)",
                         (house_id, nick, btc, now_str))
        await db.commit()

    await bot.send_message(
        SHARED_CHAT_ID,
        f"⚡ **Сбор биткоинов (MMT)!**\n\n🏠 Дом: **№{house_id}**\n👤 Снял: `{nick}`\n💰 Сумма: `{btc:.2f} BTC`\n🕒 Время: `{now.strftime('%H:%M:%S')}`\n\nТаймеры сброшены.",
        parse_mode="Markdown"
    )
    await update_live_dashboard()
    return web.json_response({"status": "success"})

# ==================== СТАРТ СИСТЕМЫ ====================
async def main():
    await init_db()

    app = web.Application()
    app.router.add_get("/", ping_handler)
    app.router.add_get("/ping", ping_handler)
    app.router.add_get("/api/heartbeat", heartbeat_handler)
    app.router.add_get("/api/harvest", harvest_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()

    asyncio.create_task(main_engine_loop())
    print("Сервер запущен (порт 8080). Polling активен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
