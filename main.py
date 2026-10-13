import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(part.strip()) for part in os.getenv("ADMIN_IDS", "").split(",")
    if part.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "vufc.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан. Добавьте его в переменные окружения.")

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA foreign_keys = ON")

def init_db():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        fighter_nick TEXT,
        balance REAL NOT NULL DEFAULT 0,
        joined_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS tournaments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS fights (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
        card_type TEXT NOT NULL DEFAULT 'Main Card',
        fighter_a TEXT NOT NULL,
        fighter_b TEXT NOT NULL,
        odds_a REAL NOT NULL,
        odds_b REAL NOT NULL,
        winner TEXT,
        status TEXT NOT NULL DEFAULT 'open'
    );
    CREATE TABLE IF NOT EXISTS bets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(user_id),
        tournament_id INTEGER,
        stake REAL NOT NULL,
        bet_type TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        total_odds REAL NOT NULL,
        payout REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS bet_legs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bet_id INTEGER NOT NULL REFERENCES bets(id) ON DELETE CASCADE,
        fight_id INTEGER NOT NULL REFERENCES fights(id),
        selection TEXT NOT NULL,
        odds REAL NOT NULL,
        result TEXT NOT NULL DEFAULT 'pending'
    );
    CREATE TABLE IF NOT EXISTS promo_codes (
        code TEXT PRIMARY KEY COLLATE NOCASE,
        reward REAL NOT NULL,
        max_uses INTEGER,
        uses INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS promo_redemptions (
        code TEXT NOT NULL REFERENCES promo_codes(code),
        user_id INTEGER NOT NULL REFERENCES users(user_id),
        redeemed_at TEXT NOT NULL,
        PRIMARY KEY (code, user_id)
    );
    CREATE TABLE IF NOT EXISTS balance_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(user_id),
        amount REAL NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)
    # Добавляем колонку card_type, если база уже существовала ранее
    try:
        db.execute("ALTER TABLE fights ADD COLUMN card_type TEXT NOT NULL DEFAULT 'Main Card'")
    except sqlite3.OperationalError:
        pass
    db.commit()

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def get_user(user_id: int):
    return db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

def ensure_user(user_id: int, username: Optional[str]):
    if not get_user(user_id):
        db.execute(
            "INSERT INTO users(user_id, username, balance, joined_at) VALUES(?,?,0,?)",
            (user_id, username, now())
        )
    else:
        db.execute("UPDATE users SET username=? WHERE user_id=?", (username, user_id))
    db.commit()

def main_menu():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🥊 Кард"), KeyboardButton(text="💰 Мой баланс")],
            [KeyboardButton(text="🎯 Сделать ставку"), KeyboardButton(text="📜 Мои ставки")],
            [KeyboardButton(text="🏆 Таблица лидеров"), KeyboardButton(text="💳 Пополнить")],
            [KeyboardButton(text="🎟️ Промокоды"), KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True
    )

def admin_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎟️ Создать промокод", callback_data="adm:promo_create")],
        [InlineKeyboardButton(text="📊 Промокоды и статистика", callback_data="adm:promo_list")],
        [InlineKeyboardButton(text="⛔ Отключить промокод", callback_data="adm:promo_disable")],
        [InlineKeyboardButton(text="➕ Создать турнир", callback_data="adm:tournament_create")],
        [InlineKeyboardButton(text="🥊 Добавить бой", callback_data="adm:fight_create")],
        [InlineKeyboardButton(text="🏁 Внести результат", callback_data="adm:result")],
        [InlineKeyboardButton(text="🎁 Начислить бонус", callback_data="adm:bonus")],
    ])

class PromoFlow(StatesGroup):
    entering_code = State()

class DepositFlow(StatesGroup):
    details = State()

class AdminFlow(StatesGroup):
    promo_code = State()
    promo_reward = State()
    promo_limit = State()
    promo_expiry = State()
    disable_code = State()
    tournament_title = State()
    fight_tournament = State()
    fight_card_type = State()
    fight_a = State()
    fight_b = State()
    fight_odds = State()
    result_fight = State()
    result_winner = State()
    bonus_user = State()
    bonus_amount = State()

class BetFlow(StatesGroup):
    choose_fight = State()
    choose_side = State()
    amount = State()
    choose_type = State()
    confirm_single = State()

dp = Dispatcher()

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "🥊 <b>VUFC Букмекер</b>\n\n"
        "Добро пожаловать! Здесь используются виртуальные рубли.\n"
        "Твой стартовый баланс: <b>0 ₽</b>.\n\n"
        "Выбери раздел в меню ниже.",
        reply_markup=main_menu()
    )

@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await message.answer("⚙️ <b>Админ-панель VUFC</b>", reply_markup=admin_menu())

@dp.message(Command("cancel"))
@dp.message(F.text == "⬅️ Назад")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Возврат в главное меню.", reply_markup=main_menu())

# --- 1. КАРД ---
@dp.message(F.text == "🥊 Кард")
async def card_view(message: Message):
    tournaments = db.execute("SELECT * FROM tournaments WHERE status='open' ORDER BY id DESC").fetchall()
    if not tournaments:
        return await message.answer("На данный момент нет активных турниров.", reply_markup=main_menu())
    
    text_lines = []
    for t in tournaments:
        text_lines.append(f"🇮🇹 <b>{t['title']}</b>\n")
        
        main_fights = db.execute("SELECT * FROM fights WHERE tournament_id=? AND card_type='Main Card' AND status='open' ORDER BY id", (t["id"],)).fetchall()
        prelim_fights = db.execute("SELECT * FROM fights WHERE tournament_id=? AND card_type='Prelims' AND status='open' ORDER BY id", (t["id"],)).fetchall()
        
        if main_fights:
            text_lines.append("<b>MAIN CARD:</b>")
            for f in main_fights:
                text_lines.append(f"• {f['fighter_a']} vs {f['fighter_b']} — <b>{f['odds_a']:.2f} / {f['odds_b']:.2f}</b>")
            text_lines.append("")
            
        if prelim_fights:
            text_lines.append("<b>PRELIMS:</b>")
            for f in prelim_fights:
                text_lines.append(f"• {f['fighter_a']} vs {f['fighter_b']} — <b>{f['odds_a']:.2f} / {f['odds_b']:.2f}</b>")
            text_lines.append("")
            
        if not main_fights and not prelim_fights:
            text_lines.append("Бои пока не добавлены.\n")
    
    text_lines.append("📊 Коэффициенты → 🎯 <b>Сделать ставку</b> в меню")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Канал лиги", url="https://t.me/UFCvirtualleagueMMA")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    await message.answer("\n".join(text_lines), reply_markup=kb, disable_web_page_preview=True)

# --- 2. МОЙ БАЛАНС ---
@dp.message(F.text == "💰 Мой баланс")
async def balance_view(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    user = get_user(message.from_user.id)
    username = f"@{user['username']}" if user['username'] else "Не указан"
    
    active_bets = db.execute(
        "SELECT COUNT(*) as cnt FROM bets WHERE user_id=? AND status='active'",
        (message.from_user.id,)
    ).fetchone()["cnt"]

    text = (
        f"👤 <b>Ваш ник:</b> {username}\n"
        f"💰 <b>Баланс:</b> {user['balance']:,.2f} ₽\n"
        f"🎯 <b>Активных ставок:</b> {active_bets}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Пополнить", callback_data="nav:deposit")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    await message.answer(text, reply_markup=kb)

# --- 3. СДЕЛАТЬ СТАВКУ ---
@dp.message(F.text == "🎯 Сделать ставку")
async def start_betting(message: Message, state: FSMContext):
    await state.clear()
    fights = db.execute("SELECT f.*, t.title FROM fights f JOIN tournaments t ON t.id=f.tournament_id WHERE f.status='open' ORDER BY f.id DESC").fetchall()
    if not fights:
        return await message.answer("Нет доступных боёв для ставок.")
    
    buttons = [
        [InlineKeyboardButton(text=f"{f['fighter_a']} vs {f['fighter_b']}", callback_data=f"bet:select_fight:{f['id']}")]
        for f in fights
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    
    await message.answer("<b>Выберите бой:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(BetFlow.choose_fight)

@dp.callback_query(F.data.startswith("bet:select_fight:"))
async def bet_select_fight(callback: CallbackQuery, state: FSMContext):
    fid = int(callback.data.split(":")[2])
    fight = db.execute("SELECT * FROM fights WHERE id=? AND status='open'", (fid,)).fetchone()
    if not fight:
        return await callback.answer("Бой недоступен.", show_alert=True)
    
    await state.update_data(current_fight_id=fid)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{fight['fighter_a']} — кэф {fight['odds_a']:.2f}", callback_data="bet:side:a")],
        [InlineKeyboardButton(text=f"{fight['fighter_b']} — кэф {fight['odds_b']:.2f}", callback_data="bet:side:b")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="bet:back_fights")]
    ])
    await callback.message.edit_text(f"Выберите, на кого ставите в бою <b>{fight['fighter_a']} vs {fight['fighter_b']}</b>:", reply_markup=kb)
    await state.set_state(BetFlow.choose_side)
    await callback.answer()

@dp.callback_query(F.data == "bet:back_fights")
async def back_to_fights_list(callback: CallbackQuery, state: FSMContext):
    await bet_select_fight_menu(callback.message, state)
    await callback.answer()

async def bet_select_fight_menu(message: Message, state: FSMContext):
    fights = db.execute("SELECT f.*, t.title FROM fights f JOIN tournaments t ON t.id=f.tournament_id WHERE f.status='open' ORDER BY f.id DESC").fetchall()
    buttons = [
        [InlineKeyboardButton(text=f"{f['fighter_a']} vs {f['fighter_b']}", callback_data=f"bet:select_fight:{f['id']}")]
        for f in fights
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    await message.edit_text("<b>Выберите бой:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(BetFlow.choose_fight)

@dp.callback_query(BetFlow.choose_side, F.data.startswith("bet:side:"))
async def bet_choose_side(callback: CallbackQuery, state: FSMContext):
    side = callback.data.split(":")[-1]
    data = await state.get_data()
    fight = db.execute("SELECT * FROM fights WHERE id=?", (data["current_fight_id"],)).fetchone()
    
    selection = fight["fighter_a"] if side == "a" else fight["fighter_b"]
    odds = fight["odds_a"] if side == "a" else fight["odds_b"]
    
    legs = data.get("legs", [])
    if any(l["fight_id"] == fight["id"] for l in legs):
        return await callback.answer("Вы уже добавили этот бой в купон!", show_alert=True)
    
    legs.append({
        "fight_id": fight["id"],
        "tournament_id": fight["tournament_id"],
        "selection": selection,
        "odds": odds,
        "match": f"{fight['fighter_a']} vs {fight['fighter_b']}"
    })
    
    await state.update_data(legs=legs)
    await state.set_state(BetFlow.amount)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="bet:back_fights")]])
    await callback.message.answer("Введите сумму ставки (мин. 100 ₽):", reply_markup=kb)
    await callback.answer()

@dp.message(BetFlow.amount)
async def bet_enter_amount(message: Message, state: FSMContext):
    try:
        stake = float((message.text or "").replace(",", "."))
        if stake < 100:
            return await message.answer("Минимальная сумма ставки — 100 ₽. Введите сумму ещё раз:")
    except ValueError:
        return await message.answer("Пожалуйста, введите корректную сумму в рублях (мин. 100 ₽):")
    
    user = get_user(message.from_user.id)
    if user["balance"] < stake:
        return await message.answer(f"Недостаточно средств. Ваш баланс: {user['balance']:.2f} ₽.")
    
    await state.update_data(stake=stake)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Одинар", callback_data="bet:type:single")],
        [InlineKeyboardButton(text="Экспресс", callback_data="bet:type:express")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="bet:back_fights")]
    ])
    await message.answer("Выберите тип ставки:", reply_markup=kb)
    await state.set_state(BetFlow.choose_type)

@dp.callback_query(BetFlow.choose_type, F.data == "bet:type:single")
async def bet_type_single(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    leg = data["legs"][-1]
    stake = data["stake"]
    payout = stake * leg["odds"]
    
    text = (
        f"<b>Подтверждение ставки (Одинар):</b>\n\n"
        f"Бой: {leg['match']}\n"
        f"Ставка: на <b>{leg['selection']}</b>\n"
        f"Кэф: <b>{leg['odds']:.2f}</b>\n"
        f"Сумма: <b>{stake:.2f} ₽</b>\n"
        f"Возможный выигрыш: <b>{payout:.2f} ₽</b>"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="bet:confirm_single")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(BetFlow.confirm_single)
    await callback.answer()

@dp.callback_query(BetFlow.confirm_single, F.data == "bet:confirm_single")
async def bet_confirm_single(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = callback.from_user.id
    stake = data["stake"]
    leg = data["legs"][-1]
    
    try:
        db.execute("BEGIN IMMEDIATE")
        user = get_user(uid)
        if user["balance"] < stake:
            db.rollback()
            await state.clear()
            return await callback.message.answer("Недостаточно средств на балансе.")
        
        db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (stake, uid))
        cur = db.execute(
            "INSERT INTO bets(user_id, tournament_id, stake, bet_type, status, total_odds, payout, created_at) VALUES(?,?,?,?,?,?,0,?)",
            (uid, leg["tournament_id"], stake, "single", "active", leg["odds"], now())
        )
        bet_id = cur.lastrowid
        db.execute(
            "INSERT INTO bet_legs(bet_id, fight_id, selection, odds) VALUES(?,?,?,?)",
            (bet_id, leg["fight_id"], leg["selection"], leg["odds"])
        )
        db.execute(
            "INSERT INTO balance_log(user_id, amount, reason, created_at) VALUES(?,?,?,?)",
            (uid, -stake, f"Ставка Одинар #{bet_id}", now())
        )
        db.commit()
    except Exception:
        db.rollback()
        await state.clear()
        return await callback.message.answer("Ошибка при принятии ставки.")
    
    await state.clear()
    await callback.message.edit_text(f"✅ Ставка #{bet_id} (Одинар) успешно принята!")
    await callback.answer()

@dp.callback_query(BetFlow.choose_type, F.data == "bet:type:express")
async def bet_type_express(callback: CallbackQuery, state: FSMContext):
    await render_express_coupon(callback.message, state)
    await callback.answer()

async def render_express_coupon(message: Message, state: FSMContext):
    data = await state.get_data()
    legs = data.get("legs", [])
    stake = data.get("stake", 100)
    
    total_odds = 1.0
    lines = [f"<b>Экспресс ({len(legs)} события):</b>"]
    for idx, leg in enumerate(legs, 1):
        total_odds *= leg["odds"]
        lines.append(f"{idx}. {leg['selection']} — {leg['odds']:.2f}")
    
    lines.append(f"\n<b>Итоговый кэф:</b> {total_odds:.2f}")
    lines.append(f"<b>Сумма:</b> {stake:.2f} ₽")
    lines.append(f"<b>Возможный выигрыш:</b> {stake * total_odds:.2f} ₽")
    
    kb_buttons = [[InlineKeyboardButton(text="➕ Добавить событие", callback_data="bet:express_add")]]
    if len(legs) >= 2:
        kb_buttons.append([InlineKeyboardButton(text="✅ Завершить", callback_data="bet:express_finish")])
    kb_buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_menu")])
    
    await message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

@dp.callback_query(F.data == "bet:express_add")
async def express_add_more(callback: CallbackQuery, state: FSMContext):
    fights = db.execute("SELECT f.*, t.title FROM fights f JOIN tournaments t ON t.id=f.tournament_id WHERE f.status='open' ORDER BY f.id DESC").fetchall()
    buttons = [
        [InlineKeyboardButton(text=f"{f['fighter_a']} vs {f['fighter_b']}", callback_data=f"bet:select_fight:{f['id']}")]
        for f in fights
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    await callback.message.edit_text("<b>Выберите дополнительный бой:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(BetFlow.choose_fight)
    await callback.answer()

@dp.callback_query(F.data == "bet:express_finish")
async def express_finish(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    legs = data.get("legs", [])
    stake = data.get("stake", 100)
    uid = callback.from_user.id
    
    if len(legs) < 2:
        return await callback.answer("В экспрессе должно быть минимум 2 события!", show_alert=True)
    
    total_odds = 1.0
    for l in legs:
        total_odds *= l["odds"]
        
    try:
        db.execute("BEGIN IMMEDIATE")
        user = get_user(uid)
        if user["balance"] < stake:
            db.rollback()
            await state.clear()
            return await callback.message.answer("Недостаточно средств.")
            
        db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (stake, uid))
        cur = db.execute(
            "INSERT INTO bets(user_id, tournament_id, stake, bet_type, status, total_odds, payout, created_at) VALUES(?,NULL,?,?,?,?,0,?)",
            (uid, stake, "express", "active", total_odds, now())
        )
        bet_id = cur.lastrowid
        for l in legs:
            db.execute(
                "INSERT INTO bet_legs(bet_id, fight_id, selection, odds) VALUES(?,?,?,?)",
                (bet_id, l["fight_id"], l["selection"], l["odds"])
            )
        db.execute(
            "INSERT INTO balance_log(user_id, amount, reason, created_at) VALUES(?,?,?,?)",
            (uid, -stake, f"Ставка Экспресс #{bet_id}", now())
        )
        db.commit()
    except Exception:
        db.rollback()
        await state.clear()
        return await callback.message.answer("Ошибка сохранения экспресс-ставки.")
        
    await state.clear()
    await callback.message.edit_text(f"✅ Экспресс #{bet_id} принят! Событий: {len(legs)}, Итоговый кэф: {total_odds:.2f}")
    await callback.answer()

# --- 4. МОИ СТАВКИ ---
@dp.message(F.text == "📜 Мои ставки")
async def my_bets_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟡 Активные", callback_data="bets:filter:active"),
            InlineKeyboardButton(text="🟢 Выигранные", callback_data="bets:filter:won"),
            InlineKeyboardButton(text="🔴 Проигранные", callback_data="bets:filter:lost")
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    await message.answer("📜 <b>Мои ставки</b>\nВыберите категорию:", reply_markup=kb)

@dp.callback_query(F.data.startswith("bets:filter:"))
async def my_bets_filtered(callback: CallbackQuery):
    status_filter = callback.data.split(":")[-1]
    rows = db.execute(
        "SELECT * FROM bets WHERE user_id=? AND status=? ORDER BY id DESC LIMIT 10",
        (callback.from_user.id, status_filter)
    ).fetchall()
    
    if not rows:
        await callback.answer("Ставок с таким статусом не найдено.", show_alert=True)
        return
        
    lines = [f"<b>Список ставок ({status_filter.upper()}):</b>\n"]
    for b in rows:
        legs = db.execute(
            "SELECT f.fighter_a, f.fighter_b, bl.selection, bl.result FROM bet_legs bl JOIN fights f ON f.id=bl.fight_id WHERE bl.bet_id=?",
            (b["id"],)
        ).fetchall()
        
        status_emoji = "🟡" if b["status"] == "active" else ("🟢" if b["status"] == "won" else "🔴")
        lines.append(f"{status_emoji} <b>{b['bet_type'].capitalize()} #{b['id']}</b>")
        for l in legs:
            lines.append(f"• {l['fighter_a']} vs {l['fighter_b']} ➔ {l['selection']}")
        lines.append(f"Сумма: {b['stake']:.2f} ₽ | Кэф: {b['total_odds']:.2f}\n")
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]])
    await callback.message.edit_text("\n".join(lines), reply_markup=kb)
    await callback.answer()

# --- 5. ТАБЛИЦА ЛИДЕРОВ ---
@dp.message(F.text == "🏆 Таблица лидеров")
async def leaderboard_view(message: Message):
    rows = db.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
    if not rows:
        return await message.answer("Таблица лидеров пуста.")
    
    lines = ["🏆 <b>Таблица лидеров</b>\n"]
    for idx, row in enumerate(rows, 1):
        uname = f"@{row['username']}" if row['username'] else "Игрок"
        lines.append(f"{idx}. {uname} — <b>{row['balance']:,.2f} ₽</b>")
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]])
    await message.answer("\n".join(lines), reply_markup=kb)

# --- 6. ПОПОЛНИТЬ ---
@dp.message(F.text == "💳 Пополнить")
@dp.callback_query(F.data == "nav:deposit")
async def deposit_menu(event):
    text = (
        "💳 <b>Пополнение баланса</b>\n\n"
        "Отправьте заявку администратору на пополнение счета в рублях.\n"
        "Укажите нужную сумму и реквизиты/способ пополнения."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ Написать админу", callback_data="deposit:request")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=kb)
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "deposit:request")
async def deposit_request_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(DepositFlow.details)
    await callback.message.answer("Укажите сумму в рублях и способ пополнения (например: 1000 рублей, СБП):")
    await callback.answer()

@dp.message(DepositFlow.details)
async def deposit_request_submit(message: Message, state: FSMContext):
    details = message.text
    uid = message.from_user.id
    uname = f"@{message.from_user.username}" if message.from_user.username else "Без username"
    
    admin_text = (
        f"📥 <b>Новая заявка на пополнение!</b>\n\n"
        f"Игрок: {uname} (ID: <code>{uid}</code>)\n"
        f"Детали: {details}"
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await message.bot.send_message(admin_id, admin_text)
        except Exception:
            pass
            
    await state.clear()
    await message.answer("✅ Ваша заявка отправлена администратору!", reply_markup=main_menu())

# --- 7. ПОМОЩЬ ---
@dp.message(F.text == "ℹ️ Помощь")
async def help_view(message: Message):
    text = (
        "ℹ️ <b>Справка и правила бота</b>\n\n"
        "• Все средства в боте являются виртуальными рублями.\n"
        "• Ставки принимаются на одиночные бои (Одинар) и цепочки боёв (Экспресс).\n"
        "• Расчёт происходит сразу после внесения официальных результатов турнира админом.\n"
        "• Для пополнения счета используйте раздел 💳 Пополнить.\n\n"
        "Возникли вопросы? Напишите /cancel для выхода в меню или все вопросы к @Iliaatopuria\n\n"
        "https://t.me/UFCvirtualleagueMMA"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📺 Канал лиги", url="https://t.me/UFCvirtualleagueMMA")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ])
    await message.answer(text, reply_markup=kb, disable_web_page_preview=False)

# --- 8. ПРОМОКОДЫ ---
@dp.message(F.text == "🎟️ Промокоды")
async def promo_button(message: Message, state: FSMContext):
    await state.set_state(PromoFlow.entering_code)
    await message.answer("🎟️ Введи промокод. Для отмены отправь /cancel.")

@dp.message(PromoFlow.entering_code)
async def redeem_promo(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    if not code:
        return await message.answer("Некорректный код.")
    uid = message.from_user.id
    ensure_user(uid, message.from_user.username)
    try:
        db.execute("BEGIN IMMEDIATE")
        promo = db.execute("SELECT * FROM promo_codes WHERE code=? COLLATE NOCASE", (code,)).fetchone()
        if not promo or not promo["active"]:
            db.rollback()
            await state.clear()
            return await message.answer("❌ Такой промокод не найден или отключён.", reply_markup=main_menu())
        already = db.execute("SELECT 1 FROM promo_redemptions WHERE code=? COLLATE NOCASE AND user_id=?", (code, uid)).fetchone()
        if already:
            db.rollback()
            await state.clear()
            return await message.answer("⚠️ Ты уже активировал этот промокод.", reply_markup=main_menu())
            
        db.execute("INSERT INTO promo_redemptions(code,user_id,redeemed_at) VALUES(?,?,?)", (code, uid, now()))
        db.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=? COLLATE NOCASE", (code,))
        db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (promo["reward"], uid))
        db.commit()
        await state.clear()
        await message.answer(f"✅ Промокод <b>{code}</b> активирован! Начислено: <b>{promo['reward']} ₽</b>", reply_markup=main_menu())
    except Exception:
        db.rollback()
        await message.answer("Ошибка активации кода.")

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.delete()
    await callback.message.answer("Главное меню", reply_markup=main_menu())
    await callback.answer()

# ---- Admin workflows ----
@dp.callback_query(F.data == "adm:promo_create")
async def admin_promo_create(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminFlow.promo_code)
    await callback.message.answer("Введи код (например VUFC100). Отмена: /cancel")
    await callback.answer()

@dp.message(AdminFlow.promo_code)
async def admin_promo_code(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    await state.update_data(promo_code=code)
    await state.set_state(AdminFlow.promo_reward)
    await message.answer("Размер бонуса в рублях?")

@dp.message(AdminFlow.promo_reward)
async def admin_promo_reward(message: Message, state: FSMContext):
    reward = float(message.text)
    data = await state.get_data()
    db.execute(
        "INSERT INTO promo_codes(code,reward,max_uses,uses,expires_at,active,created_by,created_at) VALUES(?,?,NULL,0,NULL,1,?,?)",
        (data["promo_code"], reward, message.from_user.id, now())
    )
    db.commit()
    await state.clear()
    await message.answer(f"✅ Промокод {data['promo_code']} на {reward} ₽ создан!", reply_markup=admin_menu())

@dp.callback_query(F.data == "adm:tournament_create")
async def admin_tournament_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminFlow.tournament_title)
    await callback.message.answer("Название турнира (например: UFC COLISEUM 10: USMAN VS ADESANYA 3):")
    await callback.answer()

@dp.message(AdminFlow.tournament_title)
async def admin_tournament_create(message: Message, state: FSMContext):
    title = message.text.strip()
    cur = db.execute("INSERT INTO tournaments(title,status,created_at) VALUES(?,'open',?)", (title, now()))
    db.commit()
    await state.clear()
    await message.answer(f"✅ Турнир «{title}» создан.", reply_markup=admin_menu())

@dp.callback_query(F.data == "adm:fight_create")
async def admin_fight_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    rows = db.execute("SELECT id,title FROM tournaments WHERE status='open' ORDER BY id DESC").fetchall()
    if not rows:
        return await callback.message.answer("Сначала создай турнир.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{t['id']} {t['title']}", callback_data=f"adm:ft:{t['id']}")] for t in rows
    ])
    await callback.message.answer("Выбери турнир:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm:ft:"))
async def admin_fight_tournament(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[2])
    await state.update_data(fight_tournament=tid)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="MAIN CARD", callback_data="adm:cardtype:Main Card")],
        [InlineKeyboardButton(text="PRELIMS", callback_data="adm:cardtype:Prelims")]
    ])
    await callback.message.answer("Выберите категорию боя:", reply_markup=kb)
    await state.set_state(AdminFlow.fight_card_type)
    await callback.answer()

@dp.callback_query(AdminFlow.fight_card_type, F.data.startswith("adm:cardtype:"))
async def admin_fight_cardtype(callback: CallbackQuery, state: FSMContext):
    card_type = callback.data.split(":")[-1]
    await state.update_data(fight_card_type=card_type)
    await state.set_state(AdminFlow.fight_a)
    await callback.message.answer(f"Категория: {card_type}\nВведи имя первого бойца:")
    await callback.answer()

@dp.message(AdminFlow.fight_a)
async def admin_fight_a(message: Message, state: FSMContext):
    await state.update_data(fighter_a=message.text.strip())
    await state.set_state(AdminFlow.fight_b)
    await message.answer("Имя второго бойца:")

@dp.message(AdminFlow.fight_b)
async def admin_fight_b(message: Message, state: FSMContext):
    await state.update_data(fighter_b=message.text.strip())
    await state.set_state(AdminFlow.fight_odds)
    await message.answer("Введи коэффициенты через пробел (например: 1.45 2.70):")

@dp.message(AdminFlow.fight_odds)
async def admin_fight_odds(message: Message, state: FSMContext):
    parts = message.text.replace(",", ".").split()
    oa, ob = float(parts[0]), float(parts[1])
    data = await state.get_data()
    db.execute(
        "INSERT INTO fights(tournament_id,card_type,fighter_a,fighter_b,odds_a,odds_b) VALUES(?,?,?,?,?,?)",
        (data["fight_tournament"], data.get("fight_card_type", "Main Card"), data["fighter_a"], data["fighter_b"], oa, ob)
    )
    db.commit()
    await state.clear()
    await message.answer("✅ Бой успешно добавлен.", reply_markup=admin_menu())

@dp.callback_query(F.data == "adm:result")
async def admin_result_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    rows = db.execute("SELECT f.*, t.title FROM fights f JOIN tournaments t ON t.id=f.tournament_id WHERE f.status='open' ORDER BY f.id DESC").fetchall()
    if not rows:
        return await callback.message.answer("Нет открытых боёв.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{f['id']} {f['fighter_a']} vs {f['fighter_b']}", callback_data=f"adm:rf:{f['id']}")]
        for f in rows
    ])
    await callback.message.answer("Выбери бой для расчета:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("adm:rf:"))
async def admin_result_fight(callback: CallbackQuery, state: FSMContext):
    fid = int(callback.data.split(":")[2])
    fight = db.execute("SELECT * FROM fights WHERE id=?", (fid,)).fetchone()
    await state.update_data(result_fight=fid)
    await state.set_state(AdminFlow.result_winner)
    await callback.message.answer(f"Введи точное имя победителя:\n1) {fight['fighter_a']}\n2) {fight['fighter_b']}")
    await callback.answer()

@dp.message(AdminFlow.result_winner)
async def admin_result_winner(message: Message, state: FSMContext):
    winner = message.text.strip()
    data = await state.get_data()
    fid = data["result_fight"]
    
    db.execute("UPDATE fights SET winner=?, status='settled' WHERE id=?", (winner, fid))
    db.execute("UPDATE bet_legs SET result='won' WHERE fight_id=? AND selection=?", (fid, winner))
    db.execute("UPDATE bet_legs SET result='lost' WHERE fight_id=? AND selection!=?", (fid, winner))
    
    active_bets = db.execute("SELECT * FROM bets WHERE status='active'").fetchall()
    for bet in active_bets:
        legs = db.execute("SELECT * FROM bet_legs WHERE bet_id=?", (bet["id"],)).fetchall()
        if all(l["result"] == "won" for l in legs):
            payout = round(bet["stake"] * bet["total_odds"], 2)
            db.execute("UPDATE bets SET status='won', payout=? WHERE id=?", (payout, bet["id"]))
            db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (payout, bet["user_id"]))
        elif any(l["result"] == "lost" for l in legs):
            db.execute("UPDATE bets SET status='lost', payout=0 WHERE id=?", (bet["id"],))
            
    db.commit()
    await state.clear()
    await message.answer(f"🏁 Результат сохранён: {winner}. Все ставки рассчитаны!", reply_markup=admin_menu())

@dp.callback_query(F.data == "adm:bonus")
async def admin_bonus_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id): return
    await state.set_state(AdminFlow.bonus_user)
    await callback.message.answer("Введи Telegram ID игрока:")
    await callback.answer()

@dp.message(AdminFlow.bonus_user)
async def admin_bonus_user(message: Message, state: FSMContext):
    await state.update_data(bonus_user=int(message.text.strip()))
    await state.set_state(AdminFlow.bonus_amount)
    await message.answer("Сумма бонуса в рублях:")

@dp.message(AdminFlow.bonus_amount)
async def admin_bonus_amount(message: Message, state: FSMContext):
    amount = float(message.text.replace(",", "."))
    data = await state.get_data()
    uid = data["bonus_user"]
    
    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid))
    db.commit()
    await state.clear()
    await message.answer(f"✅ Начислено {amount} ₽ пользователю {uid}.", reply_markup=admin_menu())

async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
