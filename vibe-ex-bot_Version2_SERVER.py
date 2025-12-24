import os
import random
import sqlite3
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List, Dict

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, ContextTypes, filters
)

# ---------------- CONFIG ----------------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "bot.db")

ADMINS = set(x.strip().lstrip("@").lower() for x in os.getenv("ADMINS", "vladislav_vibe").split(",") if x.strip())
ASSISTANTS = set(x.strip().lstrip("@").lower() for x in os.getenv("ASSISTANTS", "vibe_tatti").split(",") if x.strip())

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in .env")

TZ_UAE = timezone(timedelta(hours=4))

# ---------------- DEAL TYPES (пока AED/RUB) ----------------
DT_SELL_AED_RUB = "SELL_AED_RUB"  # продаём AED, получаем RUB
DT_BUY_AED_RUB = "BUY_AED_RUB"    # покупаем AED, отдаём RUB

DEAL_TYPES = {
    DT_SELL_AED_RUB: "💸 Продажа AED → RUB",
    DT_BUY_AED_RUB: "💱 Покупка AED ← RUB",
}

PRAISE_NORMAL = [
    "✅ Сделка записана. Красиво 😎",
    "✅ Есть. Двигаем дальше 💪",
    "✅ Принято. Работаем 👌",
    "✅ Записал. Хорош 🧠",
]
PRAISE_FIRE = [
    "🔥 Нихуя себе курс! Делай грязь! 😈",
    "🔥 Разъёб! Так и надо.",
    "🔥 Сочно! Курс пушка.",
    "🔥 Топ! Уровень.",
]

# ---------------- DB ----------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for r in cur:
        if r["name"] == column:
            return True
    return False

def init_db():
    with db() as conn:
        conn.executescript("""
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
          tg_id INTEGER PRIMARY KEY,
          username TEXT,
          display_name TEXT,
          role TEXT NOT NULL DEFAULT 'staff',
          base_percent REAL NOT NULL DEFAULT 0.0
        );

        CREATE TABLE IF NOT EXISTS daily_rates (
          day TEXT PRIMARY KEY,
          sell_aed_rub REAL,
          buy_aed_rub REAL,
          set_by_tg INTEGER,
          set_at TEXT
        );

        CREATE TABLE IF NOT EXISTS reserves (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          currency TEXT NOT NULL,
          balance REAL NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS reserve_moves (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          reserve_id INTEGER NOT NULL,
          delta REAL NOT NULL,
          note TEXT,
          by_tg INTEGER,
          FOREIGN KEY(reserve_id) REFERENCES reserves(id)
        );

        CREATE TABLE IF NOT EXISTS deals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          created_at TEXT NOT NULL,
          closed_at TEXT NOT NULL,
          created_by_tg INTEGER NOT NULL,

          deal_type TEXT NOT NULL,
          aed REAL NOT NULL,
          rub REAL NOT NULL,
          rate REAL NOT NULL,

          client TEXT,
          closer TEXT,
          assigner TEXT,

          reserve_id INTEGER,
          referrer_id INTEGER,
          FOREIGN KEY(reserve_id) REFERENCES reserves(id)
        );

        CREATE TABLE IF NOT EXISTS debts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          contact_telegram TEXT,
          contact_whatsapp TEXT,
          phone_uae TEXT,
          phone_rus TEXT,
          amount REAL NOT NULL,
          currency TEXT NOT NULL DEFAULT 'AED',
          since_at TEXT,
          note TEXT,
          created_by_tg INTEGER,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS referrers (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          contact TEXT,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS clients (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          contact TEXT,
          referrer_id INTEGER,
          created_at TEXT NOT NULL,
          FOREIGN KEY(referrer_id) REFERENCES referrers(id)
        );

        CREATE TABLE IF NOT EXISTS goals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_tg INTEGER NOT NULL,
          month TEXT NOT NULL,
          target_aed REAL NOT NULL,
          reward_percent REAL DEFAULT 0,
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS payouts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_tg INTEGER NOT NULL,
          amount REAL NOT NULL,
          currency TEXT NOT NULL DEFAULT 'RUB',
          paid_at TEXT,
          note TEXT
        );
        """)

        # add columns if missing (for migration from older DB)
        if not column_exists(conn, "deals", "referrer_id"):
            try:
                conn.execute("ALTER TABLE deals ADD COLUMN referrer_id INTEGER")
            except Exception:
                pass

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def iso_to_pretty(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ_UAE)
    return dt.strftime("%d.%m.%Y %H:%M")

def month_key_local(dt: datetime) -> str:
    return dt.astimezone(TZ_UAE).strftime("%Y-%m")

def today_ymd() -> str:
    return datetime.now(TZ_UAE).strftime("%Y-%m-%d")

def get_setting(key: str) -> Optional[str]:
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

def set_setting(key: str, value: str):
    with db() as conn:
        conn.execute("""
            INSERT INTO settings(key, value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))

def upsert_user(update: Update):
    u = update.effective_user
    if not u:
        return
    username = (u.username or "").lower()
    display = (u.full_name or "").strip()
    with db() as conn:
        conn.execute("""
            INSERT INTO users(tg_id, username, display_name) VALUES(?,?,?)
            ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, display_name=excluded.display_name
        """, (u.id, username, display))

def is_admin_or_assistant(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    un = (u.username or "").lower()
    return (un in ADMINS) or (un in ASSISTANTS)

def norm_username(txt: str) -> str:
    t = (txt or "").strip()
    if t in ("", "-", "Никто", "никто"):
        return "-"
    if not t.startswith("@"):
        t = "@" + t
    return t

def calc_rate(aed: float, rub: float) -> float:
    return rub / aed if aed else 0.0

# ---------- Daily rates ----------
def get_today_rates() -> Tuple[Optional[float], Optional[float]]:
    day = today_ymd()
    with db() as conn:
        row = conn.execute("SELECT sell_aed_rub, buy_aed_rub FROM daily_rates WHERE day=?", (day,)).fetchone()
        if not row:
            return (None, None)
        return (row["sell_aed_rub"], row["buy_aed_rub"])

def set_today_sell(val: float, by_tg: int):
    day = today_ymd()
    sell, buy = get_today_rates()
    with db() as conn:
        conn.execute("""
        INSERT INTO daily_rates(day, sell_aed_rub, buy_aed_rub, set_by_tg, set_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(day) DO UPDATE SET sell_aed_rub=excluded.sell_aed_rub, buy_aed_rub=excluded.buy_aed_rub, set_by_tg=excluded.set_by_tg, set_at=excluded.set_at
        """, (day, float(val), buy, by_tg, now_iso()))

def set_today_buy(val: float, by_tg: int):
    day = today_ymd()
    sell, buy = get_today_rates()
    with db() as conn:
        conn.execute("""
        INSERT INTO daily_rates(day, sell_aed_rub, buy_aed_rub, set_by_tg, set_at)
        VALUES(?,?,?,?,?)
        ON CONFLICT(day) DO UPDATE SET sell_aed_rub=excluded.sell_aed_rub, buy_aed_rub=excluded.buy_aed_rub, set_by_tg=excluded.set_by_tg, set_at=excluded.set_at
        """, (day, sell, float(val), by_tg, now_iso()))

def deal_has_fire(deal_type: str, rate: float) -> bool:
    sell, buy = get_today_rates()
    if deal_type == DT_SELL_AED_RUB and sell is not None:
        return rate > float(sell)
    if deal_type == DT_BUY_AED_RUB and buy is not None:
        return rate < float(buy)
    return False

# ---------- Date parsing ----------
def parse_flexible_date(text: str) -> Optional[datetime]:
    s = (text or "").strip()
    if not s:
        return None

    if re.fullmatch(r"\d{6}", s):
        dd = int(s[0:2]); mm = int(s[2:4]); yy = int(s[4:6])
        year = 2000 + yy if yy < 70 else 1900 + yy
        try:
            return datetime(year, mm, dd, 12, 0, tzinfo=TZ_UAE)
        except:
            return None

    parts = re.split(r"[.\-/\s]+", s)
    parts = [p for p in parts if p]
    if len(parts) != 3:
        return None

    a, b, c = parts
    if not (a.isdigit() and b.isdigit() and c.isdigit()):
        return None

    x = int(a); y = int(b); z = int(c)

    if z < 100:
        year = 2000 + z if z < 70 else 1900 + z
    else:
        year = z

    dd, mm = x, y

    if x <= 12 and y > 12:
        dd, mm = y, x

    if x > 12:
        dd, mm = x, y

    try:
        return datetime(year, mm, dd, 12, 0, tzinfo=TZ_UAE)
    except:
        return None

def parse_time_or_none(text: str) -> Optional[Tuple[int, int]]:
    t = (text or "").strip()
    if t in ("-", ""):
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m:
        return None
    hh = int(m.group(1)); mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return None
    return (hh, mm)

# ---------- Reserves ----------
def list_reserves(currency: str="AED") -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM reserves WHERE currency=? ORDER BY id ASC", (currency,)).fetchall()

def create_reserve(name: str, currency: str="AED", initial: float=0.0) -> int:
    with db() as conn:
        conn.execute("INSERT INTO reserves(name, currency, balance, created_at) VALUES(?,?,?,?)",
                     (name, currency, float(initial), now_iso()))
        rid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        if initial != 0:
            conn.execute("INSERT INTO reserve_moves(created_at, reserve_id, delta, note) VALUES(?,?,?,?)",
                         (now_iso(), rid, float(initial), "initial"))
        return int(rid)

def apply_reserve_delta(reserve_id: int, delta: float, note: str, by_tg: int):
    with db() as conn:
        conn.execute("UPDATE reserves SET balance = balance + ? WHERE id=?", (float(delta), reserve_id))
        conn.execute("INSERT INTO reserve_moves(created_at, reserve_id, delta, note, by_tg) VALUES(?,?,?,?,?)",
                     (now_iso(), reserve_id, float(delta), note, by_tg))

def get_reserve(reserve_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM reserves WHERE id=?", (reserve_id,)).fetchone()

# ---------- Referrers / clients ----------
def list_referrers() -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM referrers ORDER BY id ASC").fetchall()

def create_referrer(name: str, contact: str="") -> int:
    with db() as conn:
        conn.execute("INSERT INTO referrers(name, contact, created_at) VALUES(?,?,?)", (name, contact, now_iso()))
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

def list_clients() -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM clients ORDER BY id ASC").fetchall()

def create_client(name: str, contact: str="", referrer_id: Optional[int]=None) -> int:
    with db() as conn:
        conn.execute("INSERT INTO clients(name, contact, referrer_id, created_at) VALUES(?,?,?,?)",
                     (name, contact, referrer_id, now_iso()))
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

# ---------------- UI ----------------
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💸 Продажа AED - RUB"), KeyboardButton("💱 Покупка AED - RUB")],
            [KeyboardButton("🏦 Резервы"), KeyboardButton("👤 Мой профиль")],
            [KeyboardButton("📄 Последние сделки"), KeyboardButton("📌 Дневной курс")],
            [KeyboardButton("⚙️ Курс ПРОДАЖИ"), KeyboardButton("⚙️ Курс ПОКУПКИ")],
            [KeyboardButton("🧾 Должники"), KeyboardButton("🤝 Рефералы")],
            [KeyboardButton("👥 Сотрудники")],
            [KeyboardButton("📌 Подробное ЗП")],
            [KeyboardButton("❌ Отмена")],
        ],
        resize_keyboard=True
    )

def back_cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[KeyboardButton("⬅️ Назад"), KeyboardButton("❌ Отмена")]], resize_keyboard=True)

def closed_when_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Сейчас", callback_data="closed:now")],
        [InlineKeyboardButton("🗓 Указать дату/время", callback_data="closed:custom")],
        [InlineKeyboardButton("❌ Отмена", callback_data="closed:cancel")],
    ])

def reserve_pick_kb(reserves: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = []
    for r in reserves:
        buttons.append([InlineKeyboardButton(f"{r['name']} (AED) = {r['balance']:.2f}", callback_data=f"res:{r['id']}")])
    buttons.append([InlineKeyboardButton("➕ Новый резерв", callback_data="res:new")])
    buttons.append([InlineKeyboardButton("👤 Резерв клиента", callback_data="res:client")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="res:cancel")])
    return InlineKeyboardMarkup(buttons)

def referrer_pick_kb(refs: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = []
    for r in refs:
        buttons.append([InlineKeyboardButton(f"{r['name']}", callback_data=f"ref:{r['id']}")])
    buttons.append([InlineKeyboardButton("➕ Новый рефовод", callback_data="ref:new")])
    buttons.append([InlineKeyboardButton("Нет рефовода", callback_data="ref:none")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="ref:cancel")])
    return InlineKeyboardMarkup(buttons)

def format_deal_card(r: sqlite3.Row) -> str:
    # normalize sqlite3.Row -> dict so .get() works
    if r is not None and not hasattr(r, 'get'):
        r = dict(r)
    fire = " 🔥" if deal_has_fire(r["deal_type"], float(r["rate"])) else ""
    rate_str = f"{float(r['rate']):.2f}{fire}"
    closed = iso_to_pretty(r["closed_at"])
    reserve_txt = "-"
    if r["reserve_id"]:
        rr = get_reserve(int(r["reserve_id"]))
        if rr:
            reserve_txt = rr["name"]
    ref_txt = "-"
    if r.get("referrer_id"):
        with db() as conn:
            rr = conn.execute("SELECT name FROM referrers WHERE id=?", (r["referrer_id"],)).fetchone()
            if rr:
                ref_txt = rr["name"]
    return (
        f"🧾 *Сделка #{r['id']}*\n"
        f"*{DEAL_TYPES.get(r['deal_type'], r['deal_type'])}*\n\n"
        f"💰 {float(r['aed']):.0f} AED за {float(r['rub']):.0f} RUB\n"
        f"📈 Курс: *{rate_str}*\n"
        f"👤 Клиент: *{r['client'] or '-'}*\n"
        f"✅ Закрыл: *{r['closer'] or '-'}*\n"
        f"📨 Передал: *{r['assigner'] or '-'}*\n"
        f"🏦 Резерв: *{reserve_txt}*\n"
        f"🤝 Рефовод: *{ref_txt}*\n"
        f"🕒 Закрыта: *{closed}*"
    )

# ---------------- CONVERSATIONS ----------------
# Deal wizard states
W_AED, W_RUB, W_CLIENT, W_CLOSER, W_ASSIGNER, W_REFERRER_PICK, W_RESERVE_PICK, W_RESERVE_NEWNAME, W_RESERVE_CLIENTNAME, W_CLOSED_CHOICE, W_CLOSED_DATE, W_CLOSED_TIME, W_CONFIRM = range(13)
# Rate wizard states
R_SELL, R_BUY = range(2)
# Reserve menu states
RV_MENU, RV_PICK_ACTION, RV_DELTA, RV_NEW_NAME = range(10, 14)
# Staff menu states
ST_MENU, ST_ADD_NAME, ST_ADD_USERNAME, ST_ADD_TGID, ST_ADD_ROLE, ST_ADD_PERCENT, ST_EDIT_NAME, ST_EDIT_USERNAME, ST_EDIT_ROLE, ST_EDIT_PERCENT = range(20, 30)
# Debts states
D_CREATE_NAME, D_CREATE_CONTACTS, D_CREATE_AMOUNT, D_CREATE_SINCE, D_CREATE_NOTE = range(100, 105)
# Referrers states
REF_CREATE_NAME, REF_CREATE_CONTACT = range(200, 202)
# Ref pick states for client creation
CL_CREATE_NAME, CL_CREATE_CONTACT, CL_CREATE_REF = range(300, 303)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    await update.message.reply_text("🚀 Готово. Выбирай действие кнопками 👇", reply_markup=main_menu())

async def cancel_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ок ✅ отменено.", reply_markup=main_menu())
    return ConversationHandler.END

async def back_any(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⬅️ Назад в меню.", reply_markup=main_menu())
    return ConversationHandler.END

# ---------- Rates ----------
async def show_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sell, buy = get_today_rates()
    day = today_ymd()
    txt = (
        f"📌 *Дневной курс* ({day})\n"
        f"💸 Продажа: *{(f'{sell:.2f}' if sell is not None else '-') }*\n"
        f"💱 Покупка: *{(f'{buy:.2f}' if buy is not None else '-') }*"
    )
    await update.message.reply_text(txt, reply_markup=main_menu())

async def ask_sell_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    if not is_admin_or_assistant(update):
        await update.message.reply_text("Нет прав. Курс может ставить только Влад или Таня.", reply_markup=main_menu())
        return ConversationHandler.END
    await update.message.reply_text("⚙️ Введи курс *ПРОДАЖИ* AED→RUB (например 23.00):", reply_markup=back_cancel_kb())
    return R_SELL

async def set_sell_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    try:
        val = float(t.replace(",", "."))
    except:
        await update.message.reply_text("Не понял число. Пример: 23.00", reply_markup=back_cancel_kb())
        return R_SELL
    set_today_sell(val, update.effective_user.id)
    await update.message.reply_text(f"✅ Курс ПРОДАЖИ установлен: *{val:.2f}*", reply_markup=main_menu())
    return ConversationHandler.END

async def ask_buy_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    if not is_admin_or_assistant(update):
        await update.message.reply_text("Нет прав. Курс может ставить только Влад или Таня.", reply_markup=main_menu())
        return ConversationHandler.END
    await update.message.reply_text("⚙️ Введи курс *ПОКУПКИ* AED←RUB (например 21.00):", reply_markup=back_cancel_kb())
    return R_BUY

async def set_buy_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    try:
        val = float(t.replace(",", "."))
    except:
        await update.message.reply_text("Не понял число. Пример: 21.00", reply_markup=back_cancel_kb())
        return R_BUY
    set_today_buy(val, update.effective_user.id)
    await update.message.reply_text(f"✅ Курс ПОКУПКИ установлен: *{val:.2f}*", reply_markup=main_menu())
    return ConversationHandler.END

# ---------- Deals entrypoints (buttons) ----------
async def deal_sell_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    context.user_data["deal"] = {"deal_type": DT_SELL_AED_RUB}
    await update.message.reply_text("💸 *Продажа AED → RUB*\nВведи объём AED:", reply_markup=back_cancel_kb())
    return W_AED

async def deal_buy_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    context.user_data["deal"] = {"deal_type": DT_BUY_AED_RUB}
    await update.message.reply_text("💱 *Покупка AED ← RUB*\nВведи объём AED:", reply_markup=back_cancel_kb())
    return W_AED

async def deal_aed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    try:
        aed = float(t.replace(",", "."))
        if aed <= 0:
            raise ValueError
    except:
        await update.message.reply_text("Нужно число > 0. Пример: 1500", reply_markup=back_cancel_kb())
        return W_AED
    context.user_data["deal"]["aed"] = aed
    await update.message.reply_text("Введи сумму RUB:", reply_markup=back_cancel_kb())
    return W_RUB

async def deal_rub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    try:
        rub = float(t.replace(",", "."))
        if rub <= 0:
            raise ValueError
    except:
        await update.message.reply_text("Нужно число > 0. Пример: 31500", reply_markup=back_cancel_kb())
        return W_RUB
    d = context.user_data["deal"]
    d["rub"] = rub
    d["rate"] = calc_rate(d["aed"], d["rub"])
    await update.message.reply_text("👤 Клиент (или '-' если не нужно):", reply_markup=back_cancel_kb())
    return W_CLIENT

async def deal_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    client = "" if t == "-" else t
    context.user_data["deal"]["client"] = client
    await update.message.reply_text("✅ Кто закрыл? (@username или '-' если ты сам):", reply_markup=back_cancel_kb())
    return W_CLOSER

async def deal_closer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    txt = norm_username(t)
    if txt == "-":
        u = update.effective_user.username or ""
        txt = "@" + u if u else "-"
    context.user_data["deal"]["closer"] = txt
    await update.message.reply_text("📨 Кто передал? (@username или 'Никто'):", reply_markup=back_cancel_kb())
    return W_ASSIGNER

async def deal_assigner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    context.user_data["deal"]["assigner"] = norm_username(t)

    # Сначала — выбрать рефовода (если есть)
    refs = list_referrers()
    await update.message.reply_text("🤝 Кто рефовод? (если нет — выбрать 'Нет')", reply_markup=ReplyKeyboardRemove())
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Выбор рефовода:", reply_markup=referrer_pick_kb(refs))
    return W_REFERRER_PICK

async def deal_referrer_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "ref:cancel":
        await q.edit_message_text("Отмена. Возвращаю в меню.", reply_markup=None)
        return await back_any(update, context)

    if data == "ref:none":
        context.user_data["deal"]["referrer_id"] = None
    elif data == "ref:new":
        await q.edit_message_text("Напиши имя нового рефовода:")
        return REF_CREATE_NAME
    else:
        m = re.fullmatch(r"ref:(\d+)", data)
        if m:
            context.user_data["deal"]["referrer_id"] = int(m.group(1))

    # После рефовода — резерв (куда/откуда AED)
    d = context.user_data["deal"]
    reserves = list_reserves("AED")
    if d["deal_type"] == DT_BUY_AED_RUB:
        await q.edit_message_text("🏦 Куда *добавить* AED? Выбери резерв:", reply_markup=None)
    else:
        await q.edit_message_text("🏦 Откуда *списать* AED? Выбери резерв:", reply_markup=None)

    await context.bot.send_message(chat_id=update.effective_chat.id, text="Выбор резерва:", reply_markup=reserve_pick_kb(reserves))
    return W_RESERVE_PICK

# creating referrer flow
async def ref_create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    context.user_data["_new_ref_name"] = t
    await update.message.reply_text("Контакт рефовода (телефон/телеграм) или '-' если нет:", reply_markup=back_cancel_kb())
    return REF_CREATE_CONTACT

async def ref_create_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    name = context.user_data.pop("_new_ref_name", "Unknown")
    contact = "" if t == "-" else t
    rid = create_referrer(name, contact)
    context.user_data["deal"]["referrer_id"] = rid
    await update.message.reply_text(f"✅ Рефовод создан: {name}", reply_markup=main_menu())
    return ConversationHandler.END

async def deal_reserve_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "res:cancel":
        await q.edit_message_text("Отмена. Возвращаю в меню.", reply_markup=None)
        return await back_any(update, context)

    if data == "res:new":
        await q.edit_message_text("Напиши название нового резерва (например: 'Касса Дима'):")
        return W_RESERVE_NEWNAME

    if data == "res:client":
        await q.edit_message_text("Напиши имя клиента для резерва (например: 'Ксения Сиськи'):")
        return W_RESERVE_CLIENTNAME

    m = re.fullmatch(r"res:(\d+)", data)
    if not m:
        await q.edit_message_text("Не понял выбор резерва. Попробуй ещё раз.")
        return W_RESERVE_PICK

    rid = int(m.group(1))
    context.user_data["deal"]["reserve_id"] = rid
    await q.edit_message_text("🕒 Когда закрыта сделка?", reply_markup=None)
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Выбери вариант:", reply_markup=closed_when_kb())
    return W_CLOSED_CHOICE

async def deal_reserve_newname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    rid = create_reserve(t, "AED", 0.0)
    context.user_data["deal"]["reserve_id"] = rid
    await update.message.reply_text("✅ Резерв создан.", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("🕒 Когда закрыта сделка?", reply_markup=closed_when_kb())
    return W_CLOSED_CHOICE

async def deal_reserve_clientname(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    name = f"Клиент: {t}"
    rid = create_reserve(name, "AED", 0.0)
    context.user_data["deal"]["reserve_id"] = rid
    await update.message.reply_text(f"✅ Создал резерв клиента: {name}", reply_markup=ReplyKeyboardRemove())
    await update.message.reply_text("🕒 Когда закрыта сделка?", reply_markup=closed_when_kb())
    return W_CLOSED_CHOICE

async def deal_closed_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    choice = q.data.split(":", 1)[1]
    if choice == "now":
        context.user_data["deal"]["closed_at"] = now_iso()
        await q.edit_message_text("Ок ✅ считаю, что закрыта сейчас.")
        return await deal_preview(update, context)
    elif choice == "cancel":
        await q.edit_message_text("Отмена. Возвращаю в меню.")
        return await back_any(update, context)
    else:
        await q.edit_message_text("Введи дату (например: 25.12.2025 / 121425 / 12/14/25 / 12 14 25):")
        return W_CLOSED_DATE

async def deal_closed_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    dt_local = parse_flexible_date(t)
    if not dt_local:
        await update.message.reply_text("Не понял дату. Пример: 25.12.2025 или 121425 или 12/14/25", reply_markup=back_cancel_kb())
        return W_CLOSED_DATE
    context.user_data["deal"]["_closed_date_local"] = dt_local
    await update.message.reply_text("Введи время ЧЧ:ММ (например 18:30) или '-' если не нужно:", reply_markup=back_cancel_kb())
    return W_CLOSED_TIME

async def deal_closed_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    base = context.user_data["deal"].get("_closed_date_local")
    if not base:
        await update.message.reply_text("Что-то пошло не так. Начни сделку заново.", reply_markup=main_menu())
        return ConversationHandler.END

    tm = parse_time_or_none(t)
    if tm:
        base = base.replace(hour=tm[0], minute=tm[1])

    dt_utc = base.astimezone(timezone.utc)
    context.user_data["deal"]["closed_at"] = dt_utc.isoformat()
    return await deal_preview(update, context)

async def deal_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = context.user_data["deal"]
    fire = deal_has_fire(d["deal_type"], float(d["rate"]))
    fire_mark = " 🔥" if fire else ""

    reserve_txt = "-"
    if d.get("reserve_id"):
        rr = get_reserve(int(d["reserve_id"]))
        if rr:
            reserve_txt = rr["name"]

    ref_txt = "-"
    if d.get("referrer_id"):
        with db() as conn:
            rr = conn.execute("SELECT name FROM referrers WHERE id=?", (d["referrer_id"],)).fetchone()
            if rr:
                ref_txt = rr["name"]

    msg = (
        "🧾 *Проверь сделку*\n\n"
        f"*{DEAL_TYPES[d['deal_type']]}*\n"
        f"💰 {float(d['aed']):.0f} AED за {float(d['rub']):.0f} RUB\n"
        f"📈 Курс: *{float(d['rate']):.2f}{fire_mark}*\n"
        f"👤 Клиент: *{d['client'] or '-'}*\n"
        f"✅ Закрыл: *{d['closer']}*\n"
        f"📨 Передал: *{d['assigner']}*\n"
        f"🏦 Резерв: *{reserve_txt}*\n"
        f"🤝 Рефовод: *{ref_txt}*\n"
        f"🕒 Закрыта: *{iso_to_pretty(d['closed_at'])}*\n\n"
        "Ответь *да* чтобы сохранить или *нет* чтобы отменить."
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)
    return W_CONFIRM

async def deal_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip().lower()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)

    if t not in ("да", "нет"):
        await update.message.reply_text("Ответь 'да' или 'нет'", reply_markup=back_cancel_kb())
        return W_CONFIRM
    if t == "нет":
        await update.message.reply_text("Ок, отменил.", reply_markup=main_menu())
        return ConversationHandler.END

    d = context.user_data["deal"]
    with db() as conn:
        conn.execute(
            "INSERT INTO deals (created_at, closed_at, created_by_tg, deal_type, aed, rub, rate, client, closer, assigner, reserve_id, referrer_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (now_iso(), d["closed_at"], update.effective_user.id, d["deal_type"], d["aed"], d["rub"], d["rate"],
             d["client"], d["closer"], d["assigner"], d.get("reserve_id"), d.get("referrer_id"))
        )
        deal_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        row = conn.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()

    # Двигаем резервы (AED):
    # - SELL: списать AED (минус)
    # - BUY: добавить AED (плюс)
    rid = row["reserve_id"]
    if rid:
        delta = float(row["aed"])
        if row["deal_type"] == DT_SELL_AED_RUB:
            delta = -delta
        apply_reserve_delta(int(rid), delta, f"deal#{row['id']} {row['deal_type']}", update.effective_user.id)

    fire = deal_has_fire(row["deal_type"], float(row["rate"]))
    praise = random.choice(PRAISE_FIRE if fire else PRAISE_NORMAL)
    await update.message.reply_text(praise, reply_markup=main_menu())

    # Дублирование в чат СДЕЛКИ
    deals_chat_id = get_setting("deals_chat_id")
    if deals_chat_id:
        try:
            await context.bot.send_message(chat_id=int(deals_chat_id), text=format_deal_card(row))
        except Exception:
            pass

    return ConversationHandler.END

# ---------- Deals list ----------
async def deals_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute("SELECT * FROM deals ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        await update.message.reply_text("Пока сделок нет.", reply_markup=main_menu())
        return
    for r in rows:
        await update.message.reply_text(format_deal_card(r), reply_markup=main_menu())

# ---------- Deals chat bind ----------
async def set_deals_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    if not is_admin_or_assistant(update):
        await update.message.reply_text("Нет прав. Привязать чат может только Влад или Таня.", reply_markup=main_menu())
        return
    set_setting("deals_chat_id", str(update.effective_chat.id))
    await update.message.reply_text("✅ Этот чат теперь *СДЕЛКИ*. Я буду дублировать сюда сделки.", reply_markup=main_menu())

# ---------- Reserves menu ----------
def reserves_keyboard(reserves: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = []
    for r in reserves:
        buttons.append([InlineKeyboardButton(f"{r['name']} ({r['balance']:.2f})", callback_data=f"rv:{r['id']}")])
    buttons.append([InlineKeyboardButton("➕ Добавить резерв", callback_data="rv:add")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="rv:back")])
    return InlineKeyboardMarkup(buttons)

def reserve_actions_keyboard(rid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Пополнить", callback_data=f"rvact:{rid}:plus"), InlineKeyboardButton("➖ Списать", callback_data=f"rvact:{rid}:minus")],
        [InlineKeyboardButton("🗑 Удалить резерв", callback_data=f"rvact:{rid}:delete")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="rv:back")],
    ])

async def reserves_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reserves = list_reserves("AED")
    if update.callback_query:
        q = update.callback_query
        await q.answer()
        target = q.message
    else:
        target = update.message

    if not reserves:
        await target.reply_text(
            "🏦 Резервы пустые. Создадим первый?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Добавить резерв", callback_data="rv:add")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="rv:back")],
            ]),
        )
        return RV_MENU

    lines = ["🏦 *Резервы (AED)*:"]
    for r in reserves:
        lines.append(f"• {r['name']} = `{r['balance']:.2f}`")

    await target.reply_text("\n".join(lines), reply_markup=reserves_keyboard(reserves), parse_mode="Markdown")
    return RV_MENU

async def reserves_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "rv:back":
        await q.edit_message_reply_markup(None)
        return await back_any(update, context)

    if data == "rv:add":
        await q.edit_message_text("Название нового резерва:")
        return RV_NEW_NAME

    m = re.fullmatch(r"rv:(\d+)", data)
    if not m:
        await q.edit_message_text("Не понял выбор резерва.")
        return RV_MENU

    rid = int(m.group(1))
    rr = get_reserve(rid)
    if not rr:
        await q.edit_message_text("Резерв не найден.")
        return RV_MENU

    context.user_data["rv_current"] = rid
    txt = f"🏦 *{rr['name']}*\nБаланс: `{rr['balance']:.2f}`"
    await q.edit_message_text(txt, reply_markup=reserve_actions_keyboard(rid), parse_mode="Markdown")
    return RV_PICK_ACTION

async def reserves_new_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    rid = create_reserve(t, "AED", 0.0)
    await update.message.reply_text("✅ Резерв создан.", reply_markup=back_cancel_kb())
    context.user_data["rv_current"] = rid
    return await reserves_menu(update, context)

async def reserves_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "rv:back":
        return await reserves_menu(update, context)

    m = re.fullmatch(r"rvact:(\d+):(plus|minus|delete)", data)
    if not m:
        await q.edit_message_text("Не понял действие.")
        return RV_PICK_ACTION

    rid = int(m.group(1))
    action = m.group(2)
    rr = get_reserve(rid)
    if not rr:
        await q.edit_message_text("Резерв не найден.")
        return RV_MENU

    context.user_data["rv_current"] = rid
    if action == "delete":
        delete_reserve(rid)
        await q.edit_message_text(f"🗑 Резерв {rr['name']} удалён.")
        return await reserves_menu(update, context)

    context.user_data["rv_action"] = action
    await q.edit_message_text("На сколько изменить баланс? Введи число (например 150):")
    return RV_DELTA

async def reserves_change_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    try:
        amt = float(t.replace(",", "."))
    except Exception:
        await update.message.reply_text("Нужно число. Пример: 150", reply_markup=back_cancel_kb())
        return RV_DELTA
    if amt <= 0:
        await update.message.reply_text("Сумма должна быть > 0", reply_markup=back_cancel_kb())
        return RV_DELTA

    rid = context.user_data.get("rv_current")
    action = context.user_data.get("rv_action")
    rr = get_reserve(rid) if rid else None
    if not rr:
        await update.message.reply_text("Резерв не найден.", reply_markup=main_menu())
        return ConversationHandler.END

    delta = amt if action == "plus" else -amt
    apply_reserve_delta(rid, delta, "manual_adjust", update.effective_user.id)
    updated = get_reserve(rid)
    await update.message.reply_text(f"✅ Готово. {updated['name']} = {updated['balance']:.2f}", reply_markup=back_cancel_kb())
    return await reserves_menu(update, context)

# ---------- Staff ----------
def list_staff() -> List[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM users ORDER BY display_name").fetchall()

def get_staff(tg_id: int) -> Optional[sqlite3.Row]:
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()

def staff_inline(rows: List[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = []
    for r in rows:
        buttons.append([InlineKeyboardButton(f"{r['display_name'] or r['username'] or r['tg_id']}", callback_data=f"st:{r['tg_id']}")])
    buttons.append([InlineKeyboardButton("➕ Добавить", callback_data="st:add")])
    buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="st:back")])
    return InlineKeyboardMarkup(buttons)

def staff_edit_kb(tg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Имя", callback_data=f"stedit:{tg_id}:name"), InlineKeyboardButton("✏️ Ник", callback_data=f"stedit:{tg_id}:user")],
        [InlineKeyboardButton("🎭 Роль", callback_data=f"stedit:{tg_id}:role"), InlineKeyboardButton("📌 %", callback_data=f"stedit:{tg_id}:percent")],
        [InlineKeyboardButton("🗑 Удалить", callback_data=f"stedit:{tg_id}:delete")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="st:back")],
    ])

async def staff_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = list_staff()
    text = ["👥 *Сотрудники*:"]
    if rows:
        for r in rows:
            text.append(f"• {r['display_name'] or '-'} ({r['username'] or '-'}) — {r['role']} / {float(r['base_percent']):.1f}%")
    else:
        text.append("Пока никого нет. Нажми 'Добавить'.")

    target = update.callback_query.message if update.callback_query else update.message
    if update.callback_query:
        await update.callback_query.answer()
    await target.reply_text("\n".join(text), reply_markup=staff_inline(rows), parse_mode="Markdown")
    return ST_MENU

async def staff_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "st:back":
        await q.edit_message_reply_markup(None)
        return await back_any(update, context)

    if data == "st:add":
        await q.edit_message_text("Имя сотрудника:")
        return ST_ADD_NAME

    m = re.fullmatch(r"st:(\d+)", data)
    if not m:
        await q.edit_message_text("Не понял выбор сотрудника.")
        return ST_MENU

    tg_id = int(m.group(1))
    row = get_staff(tg_id)
    if not row:
        await q.edit_message_text("Сотрудник не найден.")
        return ST_MENU

    context.user_data["st_current"] = tg_id
    txt = (
        f"👤 *{row['display_name'] or '-'}*\n"
        f"@{row['username'] or '-'}\n"
        f"Роль: `{row['role']}`\n"
        f"Базовый %: `{float(row['base_percent']):.1f}`"
    )
    await q.edit_message_text(txt, reply_markup=staff_edit_kb(tg_id), parse_mode="Markdown")
    return ST_MENU

async def staff_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    context.user_data["_st_new_name"] = t
    await update.message.reply_text("Ник в телеграм (без @ или '-' если нет):", reply_markup=back_cancel_kb())
    return ST_ADD_USERNAME

async def staff_add_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    username = "" if t == "-" else t.lstrip("@")
    context.user_data["_st_new_username"] = username
    await update.message.reply_text("Telegram ID (числом):", reply_markup=back_cancel_kb())
    return ST_ADD_TGID

async def staff_add_tgid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    if not t.isdigit():
        await update.message.reply_text("Нужно число (Telegram ID).", reply_markup=back_cancel_kb())
        return ST_ADD_TGID
    context.user_data["_st_new_tgid"] = int(t)
    await update.message.reply_text("Роль (admin/assistant/staff):", reply_markup=back_cancel_kb())
    return ST_ADD_ROLE

async def staff_add_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip().lower()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    if t not in ("admin", "assistant", "staff"):
        await update.message.reply_text("Варианты: admin / assistant / staff", reply_markup=back_cancel_kb())
        return ST_ADD_ROLE
    context.user_data["_st_new_role"] = t
    await update.message.reply_text("Базовый процент (например 5):", reply_markup=back_cancel_kb())
    return ST_ADD_PERCENT

async def staff_add_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    try:
        pct = float(t.replace(",", "."))
    except Exception:
        await update.message.reply_text("Нужно число. Пример: 7.5", reply_markup=back_cancel_kb())
        return ST_ADD_PERCENT

    name = context.user_data.pop("_st_new_name", "")
    username = context.user_data.pop("_st_new_username", "")
    tg_id = context.user_data.pop("_st_new_tgid", None)
    role = context.user_data.pop("_st_new_role", "staff")
    if tg_id is None:
        await update.message.reply_text("Не нашёл ID. Начни заново.", reply_markup=main_menu())
        return ConversationHandler.END

    with db() as conn:
        conn.execute(
            """
            INSERT INTO users(tg_id, username, display_name, role, base_percent) VALUES(?,?,?,?,?)
            ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username, display_name=excluded.display_name, role=excluded.role, base_percent=excluded.base_percent
            """,
            (tg_id, username, name, role, pct),
        )
    await update.message.reply_text("✅ Сотрудник сохранён.", reply_markup=main_menu())
    return ConversationHandler.END

async def staff_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    m = re.fullmatch(r"stedit:(\d+):(name|user|role|percent|delete)", data)
    if not m:
        await q.edit_message_text("Не понял действие.")
        return ST_MENU

    tg_id = int(m.group(1))
    field = m.group(2)
    context.user_data["st_current"] = tg_id

    if field == "delete":
        with db() as conn:
            conn.execute("DELETE FROM users WHERE tg_id=?", (tg_id,))
        await q.edit_message_text("🗑 Сотрудник удалён.")
        return await staff_menu(update, context)

    prompts = {
        "name": "Новое имя:",
        "user": "Новый ник (без @ или '-' если убрать):",
        "role": "Новая роль (admin/assistant/staff):",
        "percent": "Новый базовый %:",
    }
    context.user_data["st_edit_field"] = field
    await q.edit_message_text(prompts[field])
    return {
        "name": ST_EDIT_NAME,
        "user": ST_EDIT_USERNAME,
        "role": ST_EDIT_ROLE,
        "percent": ST_EDIT_PERCENT,
    }[field]

async def staff_edit_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    tg_id = context.user_data.get("st_current")
    with db() as conn:
        conn.execute("UPDATE users SET display_name=? WHERE tg_id=?", (t, tg_id))
    await update.message.reply_text("✅ Имя обновлено.", reply_markup=main_menu())
    return ConversationHandler.END

async def staff_edit_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    username = "" if t == "-" else t.lstrip("@")
    tg_id = context.user_data.get("st_current")
    with db() as conn:
        conn.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
    await update.message.reply_text("✅ Ник обновлён.", reply_markup=main_menu())
    return ConversationHandler.END

async def staff_edit_role(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip().lower()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    if t not in ("admin", "assistant", "staff"):
        await update.message.reply_text("Варианты: admin / assistant / staff", reply_markup=back_cancel_kb())
        return ST_EDIT_ROLE
    tg_id = context.user_data.get("st_current")
    with db() as conn:
        conn.execute("UPDATE users SET role=? WHERE tg_id=?", (t, tg_id))
    await update.message.reply_text("✅ Роль обновлена.", reply_markup=main_menu())
    return ConversationHandler.END

async def staff_edit_percent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    try:
        pct = float(t.replace(",", "."))
    except Exception:
        await update.message.reply_text("Нужно число. Пример: 5", reply_markup=back_cancel_kb())
        return ST_EDIT_PERCENT
    tg_id = context.user_data.get("st_current")
    with db() as conn:
        conn.execute("UPDATE users SET base_percent=? WHERE tg_id=?", (pct, tg_id))
    await update.message.reply_text("✅ % обновлён.", reply_markup=main_menu())
    return ConversationHandler.END

# ---------- Debts ----------
async def debts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧾 Должники:\n- Добавить: нажми 'Добавить'\n- Список: нажми 'Список'",
        reply_markup=ReplyKeyboardMarkup(
            [[KeyboardButton("Добавить"), KeyboardButton("Список")], [KeyboardButton("❌ Отмена")]], resize_keyboard=True
        ),
    )
    return D_CREATE_NAME

async def debts_create_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "Добавить":
        await update.message.reply_text("Имя должника:", reply_markup=back_cancel_kb())
        return D_CREATE_NAME
    if t == "Список":
        await debts_list_cmd(update, context)
        return D_CREATE_NAME
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    context.user_data["_debt_name"] = t
    await update.message.reply_text("Контакты (телеграм/WhatsApp) или '-' если нет:", reply_markup=back_cancel_kb())
    return D_CREATE_CONTACTS

async def debts_create_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    context.user_data["_debt_contacts"] = t
    await update.message.reply_text("Сколько должен (AED):", reply_markup=back_cancel_kb())
    return D_CREATE_AMOUNT

async def debts_create_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    try:
        amt = float(t.replace(",", "."))
    except:
        await update.message.reply_text("Нужно число. Пример: 12280", reply_markup=back_cancel_kb())
        return D_CREATE_AMOUNT
    context.user_data["_debt_amount"] = amt
    await update.message.reply_text("С какой даты (например 25.12.2025) или '-' если неизвестно:", reply_markup=back_cancel_kb())
    return D_CREATE_SINCE

async def debts_create_since(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    since = "" if t == "-" else t
    context.user_data["_debt_since"] = since
    await update.message.reply_text("Примечание / причина (или '-' если нет):", reply_markup=back_cancel_kb())
    return D_CREATE_NOTE

async def debts_create_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    name = context.user_data.pop("_debt_name", None)
    contacts = context.user_data.pop("_debt_contacts", None)
    amount = context.user_data.pop("_debt_amount", 0.0)
    since = context.user_data.pop("_debt_since", None)
    note = "" if t == "-" else t
    with db() as conn:
        conn.execute("INSERT INTO debts (name, contact_telegram, contact_whatsapp, amount, since_at, note, created_by_tg, created_at) VALUES (?,?,?,?,?,?,?,?)",
                     (name, contacts, contacts, amount, since, note, update.effective_user.id, now_iso()))
    await update.message.reply_text("✅ Должник добавлен.", reply_markup=main_menu())
    return ConversationHandler.END

async def debts_list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as conn:
        rows = conn.execute("SELECT * FROM debts ORDER BY id DESC LIMIT 30").fetchall()
    if not rows:
        await update.message.reply_text("Должников пока нет.", reply_markup=main_menu())
        return
    for r in rows:
        since = r["since_at"] or "-"
        await update.message.reply_text(f"#{r['id']} {r['name']}\nДолжен: {r['amount']} AED\nС какого: {since}\nКонтакты: {r['contact_telegram']}\nПримечание: {r['note']}", reply_markup=main_menu())

# ---------- Referrers menu ----------
async def referrers_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    refs = list_referrers()
    lines = ["🤝 *Рефералы:*"]
    for r in refs:
        lines.append(f"• {r['id']}) {r['name']} ({r['contact'] or '-'})")
    lines.append("\nЧтобы создать рефовода: нажми 'Создать'")
    await update.message.reply_text("\n".join(lines), reply_markup=ReplyKeyboardMarkup([[KeyboardButton("Создать"), KeyboardButton("❌ Отмена")]], resize_keyboard=True))
    return REF_CREATE_NAME

async def ref_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if t == "Создать":
        await update.message.reply_text("Имя рефовода:", reply_markup=back_cancel_kb())
        return REF_CREATE_NAME
    if t in ("⬅️ Назад", "❌ Отмена"):
        return await back_any(update, context)
    return REF_CREATE_NAME

# ---------- Profile ----------
def month_range_utc() -> Tuple[str, str]:
    now_local = datetime.now(TZ_UAE)
    start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_local.month == 12:
        next_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        next_local = start_local.replace(month=start_local.month + 1)
    return (start_local.astimezone(timezone.utc).isoformat(), next_local.astimezone(timezone.utc).isoformat())

def weighted_avg_rate(rows: List[sqlite3.Row]) -> Optional[float]:
    total_aed = sum(float(r["aed"]) for r in rows)
    total_rub = sum(float(r["rub"]) for r in rows)
    if total_aed <= 0:
        return None
    return total_rub / total_aed

async def my_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    upsert_user(update)
    u = update.effective_user
    uname = ("@" + u.username) if u.username else "-"
    start_utc, end_utc = month_range_utc()

    with db() as conn:
        closed = conn.execute(
            "SELECT * FROM deals WHERE closer=? AND closed_at>=? AND closed_at<?",
            (uname, start_utc, end_utc)
        ).fetchall()

        sold = [r for r in closed if r["deal_type"] == DT_SELL_AED_RUB]
        bought = [r for r in closed if r["deal_type"] == DT_BUY_AED_RUB]

        personal = [r for r in closed if (r["assigner"] or "-") == "-"]
        personal_sold = [r for r in personal if r["deal_type"] == DT_SELL_AED_RUB]
        personal_bought = [r for r in personal if r["deal_type"] == DT_BUY_AED_RUB]

        avg_sell = weighted_avg_rate(sold)
        avg_buy = weighted_avg_rate(bought)

        avg_sell_p = weighted_avg_rate(personal_sold)
        avg_buy_p = weighted_avg_rate(personal_bought)

        total_sold_aed = sum(float(r["aed"]) for r in sold)
        total_bought_aed = sum(float(r["aed"]) for r in bought)

        personal_sold_aed = sum(float(r["aed"]) for r in personal_sold)
        personal_bought_aed = sum(float(r["aed"]) for r in personal_bought)

        by_assigner: Dict[str, float] = {}
        for r in closed:
            a = r["assigner"] or "-"
            if a == "-":
                continue
            by_assigner[a] = by_assigner.get(a, 0.0) + float(r["aed"])

        me = conn.execute("SELECT role, base_percent FROM users WHERE tg_id=?", (u.id,)).fetchone()
        role = (me["role"] if me else "staff")
        base = (float(me["base_percent"]) if me else 0.0)

    month_title = datetime.now(TZ_UAE).strftime("%B %Y")
    txt = [
        f"📊 *Профиль*",
        f"👤 *{u.full_name}* ({uname})",
        f"🎭 Роль: *{role}*",
        f"📌 Базовый процент: *{base:.1f}%*",
        "",
        f"🗓 За месяц ({month_title}):",
        f"💸 Продано AED: *{total_sold_aed:.0f}*",
        f"💱 Куплено AED: *{total_bought_aed:.0f}*",
        f"📈 Ср. курс продажи (взвеш): *{(f'{avg_sell:.2f}' if avg_sell else '-') }*",
        f"📉 Ср. курс покупки (взвеш): *{(f'{avg_buy:.2f}' if avg_buy else '-') }*",
        "",
        f"⭐ Личный оборот (assigner = '-'):",
        f"• Продано AED: *{personal_sold_aed:.0f}*",
        f"• Куплено AED: *{personal_bought_aed:.0f}*",
        f"• Ср. курс продажи: *{(f'{avg_sell_p:.2f}' if avg_sell_p else '-') }*",
        f"• Ср. курс покупки: *{(f'{avg_buy_p:.2f}' if avg_buy_p else '-') }*",
    ]

    if by_assigner:
        txt.append("")
        txt.append("🧩 Оборот от кого (AED):")
        for k, v in sorted(by_assigner.items(), key=lambda x: -x[1])[:6]:
            txt.append(f"• {k}: *{v:.0f}*")

    await update.message.reply_text("\n".join(txt), reply_markup=main_menu())

# ---------- Salary / Detailed payroll (базовая реализация) ----------
async def detailed_payroll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Показываем базовую разбивку — личный и общий обороты по месяцу и пример расчёта ЗП.
    u = update.effective_user
    uname = ("@" + u.username) if u.username else "-"
    start_utc, end_utc = month_range_utc()
    with db() as conn:
        closed = conn.execute("SELECT * FROM deals WHERE closed_at>=? AND closed_at<? AND (closer=? OR assigner=?)", (start_utc, end_utc, uname, uname)).fetchall()
        personal = [r for r in closed if (r["assigner"] or "-") == "-"]
        total_aed = sum(float(r["aed"]) for r in closed)
        personal_aed = sum(float(r["aed"]) for r in personal)
        # базовые проценты:
        me = conn.execute("SELECT role, base_percent FROM users WHERE tg_id=?", (u.id,)).fetchone()
        base = float(me["base_percent"]) if me else (40.0 if (me and me["role"]=="courier") else 15.0)
    # Простейшая модель: комиссия = RUB_sum * base_percent/100 (пока демонстрация)
    total_rub = sum(float(r["rub"]) for r in closed)
    personal_rub = sum(float(r["rub"]) for r in personal)
    commission_total = total_rub * (base / 100.0)
    commission_personal = personal_rub * (base / 100.0)

    txt = [
        f"📌 *Подробное ЗП* для {u.full_name} ({uname})",
        f"🎯 Базовый процент: *{base:.1f}%*",
        "",
        f"🔹 Общий оборот (AED): *{total_aed:.0f}*",
        f"🔹 Личный оборот (AED): *{personal_aed:.0f}*",
        "",
        f"💵 Общая сумма в RUB: *{total_rub:.0f}*",
        f"💵 Личная сумма в RUB: *{personal_rub:.0f}*",
        "",
        f"🔸 Примерная ЗП (комиссия):",
        f"• От общего: *{commission_total:.0f} RUB*",
        f"• От личного: *{commission_personal:.0f} RUB*",
        "",
        "Примечание: это базовая модель. Для точных правил деления при выездах/передачах пришлите подробные правила — я автоматически применю их в расчётах."
    ]
    await update.message.reply_text("\n".join(txt), reply_markup=main_menu())

# ---------- Menu router ----------
async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()

    if t == "❌ Отмена":
        return await cancel_any(update, context)

    if t == "💸 Продажа AED - RUB":
        return await deal_sell_entry(update, context)

    if t == "💱 Покупка AED - RUB":
        return await deal_buy_entry(update, context)

    if t == "📄 Последние сделки":
        await deals_list(update, context)
        return ConversationHandler.END

    if t == "📌 Дневной курс":
        await show_rates(update, context)
        return ConversationHandler.END

    if t == "⚙️ Курс ПРОДАЖИ":
        return await ask_sell_rate(update, context)

    if t == "⚙️ Курс ПОКУПКИ":
        return await ask_buy_rate(update, context)

    if t == "🏦 Резервы":
        return await reserves_menu(update, context)

    if t == "👤 Мой профиль":
        await my_profile(update, context)
        return ConversationHandler.END

    if t == "🧾 Должники":
        return await debts_menu(update, context)

    if t == "🤝 Рефералы":
        return await referrers_menu(update, context)

    if t == "👥 Сотрудники":
        return await staff_menu(update, context)

    if t == "📌 Подробное ЗП":
        await detailed_payroll(update, context)
        return ConversationHandler.END

    await update.message.reply_text("Выбери действие кнопками 👇", reply_markup=main_menu())
    return ConversationHandler.END

# ---------------- MAIN ----------------
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel_any))
    app.add_handler(CommandHandler("setdealschat", set_deals_chat))
    app.add_handler(CommandHandler("debts_list", debts_list_cmd))

    # Rates conv
    sell_rate_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^⚙️ Курс ПРОДАЖИ$"), ask_sell_rate)],
        states={R_SELL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_sell_rate)]},
        fallbacks=[MessageHandler(filters.Regex(r"^(⬅️ Назад|❌ Отмена)$"), back_any)],
        allow_reentry=True,
    )
    buy_rate_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^⚙️ Курс ПОКУПКИ$"), ask_buy_rate)],
        states={R_BUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_buy_rate)]},
        fallbacks=[MessageHandler(filters.Regex(r"^(⬅️ Назад|❌ Отмена)$"), back_any)],
        allow_reentry=True,
    )

    # Deals conv
    deal_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^💸 Продажа AED - RUB$"), deal_sell_entry),
            MessageHandler(filters.Regex(r"^💱 Покупка AED - RUB$"), deal_buy_entry),
        ],
        states={
            W_AED: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_aed)],
            W_RUB: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_rub)],
            W_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_client)],
            W_CLOSER: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_closer)],
            W_ASSIGNER: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_assigner)],
            W_REFERRER_PICK: [CallbackQueryHandler(deal_referrer_pick, pattern=r"^ref:") , MessageHandler(filters.TEXT & ~filters.COMMAND, ref_create_name)],
            W_RESERVE_PICK: [CallbackQueryHandler(deal_reserve_pick, pattern=r"^res:")],
            W_RESERVE_NEWNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_reserve_newname)],
            W_RESERVE_CLIENTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_reserve_clientname)],
            W_CLOSED_CHOICE: [CallbackQueryHandler(deal_closed_choice, pattern=r"^closed:")],
            W_CLOSED_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_closed_date)],
            W_CLOSED_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_closed_time)],
            W_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, deal_confirm)],
            REF_CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_create_name)],
            REF_CREATE_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_create_contact)],
        },
        fallbacks=[MessageHandler(filters.Regex(r"^(⬅️ Назад|❌ Отмена)$"), back_any)],
        allow_reentry=True,
    )

    # Reserves conv (buttons)
    reserves_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🏦 Резервы$"), reserves_menu)],
        states={
            RV_MENU: [CallbackQueryHandler(reserves_menu_click, pattern=r"^rv:")],
            RV_PICK_ACTION: [CallbackQueryHandler(reserves_action, pattern=r"^rvact:"), CallbackQueryHandler(reserves_menu_click, pattern=r"^rv:back$")],
            RV_NEW_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reserves_new_name)],
            RV_DELTA: [MessageHandler(filters.TEXT & ~filters.COMMAND, reserves_change_balance)],
        },
        fallbacks=[MessageHandler(filters.Regex(r"^(⬅️ Назад|❌ Отмена)$"), back_any)],
        allow_reentry=True,
    )

    # Debts conv
    debts_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🧾 Должники$"), debts_menu)],
        states={
            D_CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, debts_create_name)],
            D_CREATE_CONTACTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, debts_create_contacts)],
            D_CREATE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, debts_create_amount)],
            D_CREATE_SINCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, debts_create_since)],
            D_CREATE_NOTE: [MessageHandler(filters.TEXT & ~filters.COMMAND, debts_create_note)],
        },
        fallbacks=[MessageHandler(filters.Regex(r"^(⬅️ Назад|❌ Отмена)$"), back_any)],
        allow_reentry=True,
    )

    # Referrers conv
    refs_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🤝 Рефералы$"), referrers_menu)],
        states={
            REF_CREATE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_create_start)],
            # detailed name/contact flow handled inside
            REF_CREATE_CONTACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, ref_create_contact)],
        },
        fallbacks=[MessageHandler(filters.Regex(r"^(⬅️ Назад|❌ Отмена)$"), back_any)],
        allow_reentry=True,
    )

    # Staff conv
    staff_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^👥 Сотрудники$"), staff_menu)],
        states={
            ST_MENU: [CallbackQueryHandler(staff_menu_click, pattern=r"^st:"), CallbackQueryHandler(staff_edit, pattern=r"^stedit:")],
            ST_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_add_name)],
            ST_ADD_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_add_username)],
            ST_ADD_TGID: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_add_tgid)],
            ST_ADD_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_add_role)],
            ST_ADD_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_add_percent)],
            ST_EDIT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_edit_name)],
            ST_EDIT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_edit_username)],
            ST_EDIT_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_edit_role)],
            ST_EDIT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, staff_edit_percent)],
        },
        fallbacks=[MessageHandler(filters.Regex(r"^(⬅️ Назад|❌ Отмена)$"), back_any)],
        allow_reentry=True,
    )

    app.add_handler(sell_rate_conv)
    app.add_handler(buy_rate_conv)
    app.add_handler(deal_conv)
    app.add_handler(reserves_conv)
    app.add_handler(debts_conv)
    app.add_handler(refs_conv)
    app.add_handler(staff_conv)

    # Menu router (последним)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router))

    app.run_polling()

if __name__ == "__main__":
    main()
