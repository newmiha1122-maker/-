import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional

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
if not ADMIN_IDS:
    print("ВНИМАНИЕ: ADMIN_IDS не задан — админ-панель будет недоступна.")

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
        tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
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
            [KeyboardButton(text="🥊 Турниры"), KeyboardButton(text="💰 Мой баланс")],
            [KeyboardButton(text="🎟️ Промокоды"), KeyboardButton(text="📋 Мои ставки")],
            [KeyboardButton(text="🏆 Топ игроков"), KeyboardButton(text="👤 Профиль")],
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


class AdminFlow(StatesGroup):
    promo_code = State()
    promo_reward = State()
    promo_limit = State()
    promo_expiry = State()
    disable_code = State()
    tournament_title = State()
    fight_tournament = State()
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


dp = Dispatcher()


@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    ensure_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "🥊 <b>VUFC Букмекер</b>\n\n"
        "Добро пожаловать! Здесь используются только виртуальные средства.\n"
        "Твой стартовый баланс: <b>$0</b>.\n\n"
        "Выбери раздел в меню ниже.",
        reply_markup=main_menu()
    )


@dp.message(Command("admin"))
async def admin_command(message: Message):
    if not is_admin(message.from_user.id):
        return await message.answer("⛔ Нет доступа.")
    await message.answer("⚙️ <b>Админ-панель VUFC</b>", reply_markup=admin_menu())


@dp.message(Command("promo"))
async def promo_command(message: Message, state: FSMContext):
    ensure_user(message.from_user.id, message.from_user.username)
    await state.set_state(PromoFlow.entering_code)
    await message.answer("🎟️ Введи промокод. Для отмены отправь /cancel.")


@dp.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_menu())


@dp.message(PromoFlow.entering_code)
async def redeem_promo(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    if not code or len(code) > 40 or not code.replace("_", "").replace("-", "").isalnum():
        return await message.answer("Некорректный код. Попробуй ещё раз или отправь /cancel.")
    uid = message.from_user.id
    ensure_user(uid, message.from_user.username)
    try:
        db.execute("BEGIN IMMEDIATE")
        promo = db.execute("SELECT * FROM promo_codes WHERE code=? COLLATE NOCASE", (code,)).fetchone()
        if not promo or not promo["active"]:
            db.rollback()
            await state.clear()
            return await message.answer("❌ Такой промокод не найден или отключён.", reply_markup=main_menu())
        if promo["expires_at"] and datetime.fromisoformat(promo["expires_at"]) < datetime.now(timezone.utc):
            db.rollback()
            await state.clear()
            return await message.answer("⌛ Срок действия промокода истёк.", reply_markup=main_menu())
        if promo["max_uses"] is not None and promo["uses"] >= promo["max_uses"]:
            db.rollback()
            await state.clear()
            return await message.answer("❌ Лимит активаций этого промокода исчерпан.", reply_markup=main_menu())
        already = db.execute(
            "SELECT 1 FROM promo_redemptions WHERE code=? COLLATE NOCASE AND user_id=?",
            (code, uid)
        ).fetchone()
        if already:
            db.rollback()
            await state.clear()
            return await message.answer("⚠️ Ты уже активировал этот промокод.", reply_markup=main_menu())
        db.execute("INSERT INTO promo_redemptions(code,user_id,redeemed_at) VALUES(?,?,?)", (code, uid, now()))
        db.execute("UPDATE promo_codes SET uses=uses+1 WHERE code=? COLLATE NOCASE", (code,))
        db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (promo["reward"], uid))
        db.execute("INSERT INTO balance_log(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
                   (uid, promo["reward"], f"Промокод {code}", now()))
        db.commit()
        balance = get_user(uid)["balance"]
        await state.clear()
        await message.answer(
            f"✅ Промокод <b>{code}</b> активирован!\n"
            f"Начислено: <b>${promo['reward']:.2f}</b>\n"
            f"Твой баланс: <b>${balance:.2f}</b>",
            reply_markup=main_menu()
        )
    except sqlite3.IntegrityError:
        db.rollback()
        await state.clear()
        await message.answer("⚠️ Этот промокод уже был активирован.", reply_markup=main_menu())
    except Exception:
        db.rollback()
        await message.answer("Не удалось активировать код. Попробуй ещё раз позже.")


@dp.message(F.text == "🎟️ Промокоды")
async def promo_button(message: Message, state: FSMContext):
    await state.set_state(PromoFlow.entering_code)
    await message.answer("🎟️ Введи промокод. Для отмены отправь /cancel.")


@dp.message(F.text == "💰 Мой баланс")
async def balance(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    user = get_user(message.from_user.id)
    await message.answer(f"💰 Твой баланс: <b>${user['balance']:.2f}</b>\n\nДеньги здесь виртуальные.")


@dp.message(F.text == "👤 Профиль")
async def profile(message: Message):
    ensure_user(message.from_user.id, message.from_user.username)
    user = get_user(message.from_user.id)
    nick = user["fighter_nick"] or "не выбран"
    total = db.execute("SELECT COUNT(*) n FROM bets WHERE user_id=?", (message.from_user.id,)).fetchone()["n"]
    await message.answer(
        f"👤 <b>Профиль</b>\nНик бойца: {nick}\nБаланс: ${user['balance']:.2f}\nСтавок: {total}\nID: <code>{message.from_user.id}</code>"
    )


@dp.message(F.text == "🏆 Топ игроков")
async def leaderboard(message: Message):
    rows = db.execute("SELECT fighter_nick, username, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
    if not rows:
        return await message.answer("Пока игроков нет.")
    lines = ["🏆 <b>Топ игроков по балансу</b>"]
    for i, row in enumerate(rows, 1):
        name = row["fighter_nick"] or (f"@{row['username']}" if row["username"] else "Игрок")
        lines.append(f"{i}. {name} — ${row['balance']:.2f}")
    await message.answer("\n".join(lines))


@dp.message(F.text == "🥊 Турниры")
async def tournaments(message: Message):
    rows = db.execute("SELECT * FROM tournaments ORDER BY id DESC").fetchall()
    if not rows:
        return await message.answer("Пока турниров нет. Следи за обновлениями!")
    for t in rows:
        fights = db.execute("SELECT * FROM fights WHERE tournament_id=? ORDER BY id", (t["id"],)).fetchall()
        lines = [f"🥊 <b>{t['title']}</b> · {t['status'].upper()}"]
        for f in fights:
            status = f" · победитель: {f['winner']}" if f["winner"] else ""
            lines.append(f"{f['id']}. {f['fighter_a']} ({f['odds_a']:.2f}) — {f['fighter_b']} ({f['odds_b']:.2f}){status}")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎯 Сделать ставку", callback_data=f"bet:start:{t['id']}")]
        ]) if t["status"] == "open" and fights else None
        await message.answer("\n".join(lines), reply_markup=kb)


@dp.callback_query(F.data.startswith("bet:start:"))
async def bet_start(callback: CallbackQuery, state: FSMContext):
    tid = int(callback.data.split(":")[2])
    t = db.execute("SELECT * FROM tournaments WHERE id=? AND status='open'", (tid,)).fetchone()
    if not t:
        return await callback.answer("Турнир закрыт.", show_alert=True)
    fights = db.execute("SELECT * FROM fights WHERE tournament_id=? AND status='open' ORDER BY id", (tid,)).fetchall()
    if not fights:
        return await callback.answer("Нет доступных боёв.", show_alert=True)
    buttons = [[InlineKeyboardButton(
        text=f"{f['fighter_a']} vs {f['fighter_b']}",
        callback_data=f"bet:fight:{tid}:{f['id']}"
    )] for f in fights]
    await state.update_data(tournament_id=tid)
    await callback.message.answer("Выбери бой:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data.startswith("bet:fight:"))
async def bet_choose_fight(callback: CallbackQuery, state: FSMContext):
    _, _, tid, fid = callback.data.split(":")
    fight = db.execute("SELECT * FROM fights WHERE id=? AND tournament_id=? AND status='open'", (int(fid), int(tid))).fetchone()
    if not fight:
        return await callback.answer("Бой уже закрыт.", show_alert=True)
    await state.update_data(tournament_id=int(tid), fight_id=int(fid))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"{fight['fighter_a']} · {fight['odds_a']:.2f}", callback_data="bet:side:a"),
        InlineKeyboardButton(text=f"{fight['fighter_b']} · {fight['odds_b']:.2f}", callback_data="bet:side:b")
    ]])
    await callback.message.answer("На кого ставишь?", reply_markup=kb)
    await state.set_state(BetFlow.choose_side)
    await callback.answer()


@dp.callback_query(BetFlow.choose_side, F.data.startswith("bet:side:"))
async def bet_choose_side(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    fight = db.execute("SELECT * FROM fights WHERE id=?", (data["fight_id"],)).fetchone()
    side = callback.data.split(":")[-1]
    selection = fight["fighter_a"] if side == "a" else fight["fighter_b"]
    odds = fight["odds_a"] if side == "a" else fight["odds_b"]
    await state.update_data(selection=selection, odds=odds)
    await state.set_state(BetFlow.amount)
    await callback.message.answer(f"Выбрано: <b>{selection}</b> (кэф {odds:.2f}).\nВведи сумму ставки. Для отмены /cancel.")
    await callback.answer()


@dp.message(BetFlow.amount)
async def bet_amount(message: Message, state: FSMContext):
    try:
        stake = float((message.text or "").replace(",", "."))
        if stake <= 0 or stake > 1_000_000_000:
            raise ValueError()
    except ValueError:
        return await message.answer("Введи положительную сумму числом.")
    data = await state.get_data()
    uid = message.from_user.id
    ensure_user(uid, message.from_user.username)
    user = get_user(uid)
    if user["balance"] < stake:
        return await message.answer(f"Недостаточно виртуальных средств. Баланс: ${user['balance']:.2f}. Введи другую сумму или /cancel.")
    fight = db.execute("SELECT * FROM fights WHERE id=? AND status='open'", (data["fight_id"],)).fetchone()
    if not fight:
        await state.clear()
        return await message.answer("Бой уже закрыт. Ставка не создана.")
    try:
        db.execute("BEGIN IMMEDIATE")
        current = get_user(uid)
        if current["balance"] < stake:
            db.rollback()
            return await message.answer("Недостаточно средств. Попробуй ещё раз.")
        db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (stake, uid))
        cur = db.execute(
            "INSERT INTO bets(user_id,tournament_id,stake,bet_type,status,total_odds,payout,created_at) VALUES(?,?,?,?,?,?,0,?)",
            (uid, data["tournament_id"], stake, "single", "active", data["odds"], now())
        )
        bet_id = cur.lastrowid
        db.execute("INSERT INTO bet_legs(bet_id,fight_id,selection,odds) VALUES(?,?,?,?)",
                   (bet_id, data["fight_id"], data["selection"], data["odds"]))
        db.execute("INSERT INTO balance_log(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
                   (uid, -stake, f"Ставка #{bet_id}", now()))
        db.commit()
    except Exception:
        db.rollback()
        return await message.answer("Не удалось создать ставку. Попробуй позже.")
    await state.clear()
    await message.answer(
        f"✅ Ставка #{bet_id} принята!\n"
        f"Боец: {data['selection']}\nСумма: ${stake:.2f}\n"
        f"Коэффициент: {data['odds']:.2f}\nВозможный возврат: ${stake * data['odds']:.2f}",
        reply_markup=main_menu()
    )


@dp.message(F.text == "📋 Мои ставки")
async def my_bets(message: Message):
    rows = db.execute("SELECT * FROM bets WHERE user_id=? ORDER BY id DESC LIMIT 20", (message.from_user.id,)).fetchall()
    if not rows:
        return await message.answer("У тебя пока нет ставок.")
    lines = ["📋 <b>Последние ставки</b>"]
    for b in rows:
        legs = db.execute("SELECT f.fighter_a,f.fighter_b,bl.selection,bl.result FROM bet_legs bl JOIN fights f ON f.id=bl.fight_id WHERE bl.bet_id=?", (b["id"],)).fetchall()
        desc = ", ".join(f"{x['selection']} ({x['result']})" for x in legs)
        lines.append(f"#{b['id']} · ${b['stake']:.2f} · кэф {b['total_odds']:.2f} · {b['status']}\n{desc}")
    await message.answer("\n\n".join(lines))


# ---- Admin workflows ----

@dp.callback_query(F.data == "adm:promo_create")
async def admin_promo_create(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFlow.promo_code)
    await callback.message.answer("Введи код (например VUFC100). Отмена: /cancel")
    await callback.answer()


@dp.message(AdminFlow.promo_code)
async def admin_promo_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    code = (message.text or "").strip().upper()
    if not code or len(code) > 40 or not code.replace("_", "").replace("-", "").isalnum():
        return await message.answer("Код должен содержать буквы/цифры, можно _ и -. Введи другой код.")
    if db.execute("SELECT 1 FROM promo_codes WHERE code=? COLLATE NOCASE", (code,)).fetchone():
        return await message.answer("Такой код уже существует. Введи другой.")
    await state.update_data(promo_code=code)
    await state.set_state(AdminFlow.promo_reward)
    await message.answer("Размер бонуса в виртуальных долларах? Например: 100")


@dp.message(AdminFlow.promo_reward)
async def admin_promo_reward(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        reward = float((message.text or "").replace(",", "."))
        if reward <= 0 or reward > 1_000_000:
            raise ValueError()
    except ValueError:
        return await message.answer("Укажи положительную сумму до 1 000 000.")
    await state.update_data(promo_reward=reward)
    await state.set_state(AdminFlow.promo_limit)
    await message.answer("Лимит активаций? Введи целое число или 0 для безлимита.")


@dp.message(AdminFlow.promo_limit)
async def admin_promo_limit(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        n = int((message.text or "").strip())
        if n < 0:
            raise ValueError()
    except ValueError:
        return await message.answer("Введи целое число: 0 — без лимита.")
    await state.update_data(promo_limit=n if n else None)
    await state.set_state(AdminFlow.promo_expiry)
    await message.answer("Срок действия: введи дату и время UTC в формате YYYY-MM-DD HH:MM или отправь 0 без срока.")


@dp.message(AdminFlow.promo_expiry)
async def admin_promo_expiry(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    expiry = None
    if raw != "0":
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            expiry = dt.isoformat(timespec="minutes")
        except ValueError:
            return await message.answer("Неверный формат. Пример: 2026-12-31 23:59 или 0.")
    data = await state.get_data()
    try:
        db.execute(
            "INSERT INTO promo_codes(code,reward,max_uses,uses,expires_at,active,created_by,created_at) VALUES(?,?,?,0,?,1,?,?)",
            (data["promo_code"], data["promo_reward"], data["promo_limit"], expiry, message.from_user.id, now())
        )
        db.commit()
    except sqlite3.IntegrityError:
        return await message.answer("Не удалось создать код: он уже существует.")
    await state.clear()
    limit = "без лимита" if data["promo_limit"] is None else str(data["promo_limit"])
    expiry_text = expiry or "без срока"
    await message.answer(
        f"✅ <b>Промокод создан</b>\nКод: <code>{data['promo_code']}</code>\n"
        f"Награда: ${data['promo_reward']:.2f}\nЛимит: {limit}\nСрок UTC: {expiry_text}",
        reply_markup=admin_menu()
    )


@dp.callback_query(F.data == "adm:promo_list")
async def admin_promo_list(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    rows = db.execute("SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT 30").fetchall()
    if not rows:
        await callback.message.answer("Промокодов пока нет.")
    else:
        lines = ["🎟️ <b>Промокоды</b>"]
        for p in rows:
            limit = "∞" if p["max_uses"] is None else str(p["max_uses"])
            status = "активен" if p["active"] else "отключён"
            lines.append(f"<code>{p['code']}</code> · ${p['reward']:.2f} · {p['uses']}/{limit} · {status}")
        await callback.message.answer("\n".join(lines))
    await callback.answer()


@dp.callback_query(F.data == "adm:promo_disable")
async def admin_promo_disable_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFlow.disable_code)
    await callback.message.answer("Введи код, который нужно отключить.")
    await callback.answer()


@dp.message(AdminFlow.disable_code)
async def admin_promo_disable(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    code = (message.text or "").strip().upper()
    cur = db.execute("UPDATE promo_codes SET active=0 WHERE code=? COLLATE NOCASE", (code,))
    db.commit()
    await state.clear()
    await message.answer("✅ Промокод отключён." if cur.rowcount else "Промокод не найден.", reply_markup=admin_menu())


@dp.callback_query(F.data == "adm:tournament_create")
async def admin_tournament_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFlow.tournament_title)
    await callback.message.answer("Название турнира, например UFC 133:")
    await callback.answer()


@dp.message(AdminFlow.tournament_title)
async def admin_tournament_create(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    title = (message.text or "").strip()
    if not title or len(title) > 100:
        return await message.answer("Название должно быть от 1 до 100 символов.")
    cur = db.execute("INSERT INTO tournaments(title,status,created_at) VALUES(?,'open',?)", (title, now()))
    db.commit()
    tid = cur.lastrowid
    await state.clear()
    await message.answer(f"✅ Турнир «{title}» создан. ID: {tid}\nТеперь добавь бой через /admin.", reply_markup=admin_menu())


@dp.callback_query(F.data == "adm:fight_create")
async def admin_fight_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    rows = db.execute("SELECT id,title FROM tournaments WHERE status='open' ORDER BY id DESC").fetchall()
    if not rows:
        await callback.message.answer("Сначала создай турнир.")
        return await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{t['id']} {t['title']}", callback_data=f"adm:ft:{t['id']}")] for t in rows
    ])
    await callback.message.answer("Выбери турнир:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:ft:"))
async def admin_fight_tournament(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    tid = int(callback.data.split(":")[2])
    await state.update_data(fight_tournament=tid)
    await state.set_state(AdminFlow.fight_a)
    await callback.message.answer("Имя первого бойца:")
    await callback.answer()


@dp.message(AdminFlow.fight_a)
async def admin_fight_a(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    a = (message.text or "").strip()
    if not a or len(a) > 80:
        return await message.answer("Введи имя до 80 символов.")
    await state.update_data(fighter_a=a)
    await state.set_state(AdminFlow.fight_b)
    await message.answer("Имя второго бойца:")


@dp.message(AdminFlow.fight_b)
async def admin_fight_b(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    b = (message.text or "").strip()
    if not b or len(b) > 80:
        return await message.answer("Введи имя до 80 символов.")
    await state.update_data(fighter_b=b)
    await state.set_state(AdminFlow.fight_odds)
    await message.answer("Введи коэффициенты через пробел, например: 1.45 2.70")


@dp.message(AdminFlow.fight_odds)
async def admin_fight_odds(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        parts = (message.text or "").replace(",", ".").split()
        oa, ob = float(parts[0]), float(parts[1])
        if len(parts) != 2 or oa <= 1 or ob <= 1 or oa > 1000 or ob > 1000:
            raise ValueError()
    except (ValueError, IndexError):
        return await message.answer("Введи два коэффициента больше 1, например: 1.45 2.70")
    data = await state.get_data()
    db.execute(
        "INSERT INTO fights(tournament_id,fighter_a,fighter_b,odds_a,odds_b) VALUES(?,?,?,?,?)",
        (data["fight_tournament"], data["fighter_a"], data["fighter_b"], oa, ob)
    )
    db.commit()
    await state.clear()
    await message.answer("✅ Бой добавлен.", reply_markup=admin_menu())


@dp.callback_query(F.data == "adm:result")
async def admin_result_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    rows = db.execute("SELECT f.*,t.title FROM fights f JOIN tournaments t ON t.id=f.tournament_id WHERE f.status='open' ORDER BY f.id DESC").fetchall()
    if not rows:
        await callback.message.answer("Нет боёв, ожидающих результата.")
        return await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"#{f['id']} {f['fighter_a']} vs {f['fighter_b']}", callback_data=f"adm:rf:{f['id']}")]
        for f in rows
    ])
    await callback.message.answer("Выбери бой для внесения результата:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("adm:rf:"))
async def admin_result_fight(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    fid = int(callback.data.split(":")[2])
    fight = db.execute("SELECT * FROM fights WHERE id=? AND status='open'", (fid,)).fetchone()
    if not fight:
        return await callback.answer("Бой не найден/уже рассчитан.", show_alert=True)
    await state.update_data(result_fight=fid)
    await state.set_state(AdminFlow.result_winner)
    await callback.message.answer(
        f"Введи точное имя победителя:\n1) {fight['fighter_a']}\n2) {fight['fighter_b']}\n"
        "Для отмены /cancel"
    )
    await callback.answer()


@dp.message(AdminFlow.result_winner)
async def admin_result_winner(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    fight = db.execute("SELECT * FROM fights WHERE id=? AND status='open'", (data["result_fight"],)).fetchone()
    if not fight:
        await state.clear()
        return await message.answer("Бой уже закрыт.")
    winner = (message.text or "").strip()
    valid = {fight["fighter_a"].casefold(): fight["fighter_a"], fight["fighter_b"].casefold(): fight["fighter_b"]}
    if winner.casefold() not in valid:
        return await message.answer("Имя не совпадает. Введи одно из имён точно, как показано выше.")
    winner = valid[winner.casefold()]
    db.execute("UPDATE fights SET winner=?,status='settled' WHERE id=?", (winner, fight["id"]))
    # settle every active single bet tied to this fight; express legs are supported by schema,
    # but this starter intentionally accepts only single bets in the public flow.
    bet_rows = db.execute("""
        SELECT DISTINCT b.* FROM bets b JOIN bet_legs l ON l.bet_id=b.id
        WHERE l.fight_id=? AND b.status='active'
    """, (fight["id"],)).fetchall()
    settled = 0
    for bet in bet_rows:
        legs = db.execute("SELECT * FROM bet_legs WHERE bet_id=?", (bet["id"],)).fetchall()
        if len(legs) != 1:
            continue
        leg = legs[0]
        if leg["selection"] == winner:
            payout = round(bet["stake"] * bet["total_odds"], 2)
            db.execute("UPDATE bets SET status='won',payout=? WHERE id=?", (payout, bet["id"]))
            db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (payout, bet["user_id"]))
            db.execute("INSERT INTO balance_log(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
                       (bet["user_id"], payout, f"Выигрыш ставки #{bet['id']}", now()))
            db.execute("UPDATE bet_legs SET result='won' WHERE id=?", (leg["id"],))
        else:
            db.execute("UPDATE bets SET status='lost',payout=0 WHERE id=?", (bet["id"],))
            db.execute("UPDATE bet_legs SET result='lost' WHERE id=?", (leg["id"],))
        settled += 1
    db.commit()
    await state.clear()
    await message.answer(f"🏁 Результат сохранён: победитель — <b>{winner}</b>.\nРассчитано ставок: {settled}.", reply_markup=admin_menu())


@dp.callback_query(F.data == "adm:bonus")
async def admin_bonus_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminFlow.bonus_user)
    await callback.message.answer("Введи Telegram ID игрока:")
    await callback.answer()


@dp.message(AdminFlow.bonus_user)
async def admin_bonus_user(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        uid = int((message.text or "").strip())
        if uid <= 0:
            raise ValueError()
    except ValueError:
        return await message.answer("Нужен числовой Telegram ID.")
    if not get_user(uid):
        return await message.answer("Игрок не зарегистрирован в боте.")
    await state.update_data(bonus_user=uid)
    await state.set_state(AdminFlow.bonus_amount)
    await message.answer("Сумма бонуса в виртуальных долларах:")


@dp.message(AdminFlow.bonus_amount)
async def admin_bonus_amount(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        amount = float((message.text or "").replace(",", "."))
        if amount <= 0 or amount > 1_000_000:
            raise ValueError()
    except ValueError:
        return await message.answer("Укажи сумму больше 0 и не более 1 000 000.")
    data = await state.get_data()
    uid = data["bonus_user"]
    db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, uid))
    db.execute("INSERT INTO balance_log(user_id,amount,reason,created_at) VALUES(?,?,?,?)",
               (uid, amount, f"Админский бонус от {message.from_user.id}", now()))
    db.commit()
    await state.clear()
    await message.answer(f"✅ Начислено ${amount:.2f} игроку {uid}.", reply_markup=admin_menu())
    try:
        await message.bot.send_message(uid, f"🎁 Тебе начислен бонус ${amount:.2f} виртуальных средств!")
    except Exception:
        pass


async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
