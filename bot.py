import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8624414124:AAG4tSFcVp3C0dG8vX4UTJk7jXOd6Hd9x6U")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@overlordXlooters")  # channel user must join
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "6546821383").split(",") if x.strip().isdigit()]

REQUIRED_REFERRALS = int(os.getenv("REQUIRED_REFERRALS", "4"))
DB_PATH = os.getenv("DB_PATH", "bot.db")
# ==================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("referral-bot")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                referrer_id INTEGER,
                joined_verified INTEGER DEFAULT 0,
                points INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                credited INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                used_by INTEGER,
                used_at TEXT,
                added_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                status TEXT DEFAULT 'pending',
                code TEXT,
                created_at TEXT
            )
        """)


def now():
    return datetime.utcnow().isoformat(timespec="seconds")


def upsert_user(user, referrer_id: Optional[int] = None):
    with db() as conn:
        existing = conn.execute("SELECT * FROM users WHERE user_id=?", (user.id,)).fetchone()

        if existing:
            conn.execute(
                "UPDATE users SET username=?, first_name=? WHERE user_id=?",
                (user.username, user.first_name, user.id),
            )
            return

        clean_ref = None
        if referrer_id and referrer_id != user.id:
            ref_user = conn.execute("SELECT user_id FROM users WHERE user_id=?", (referrer_id,)).fetchone()
            if ref_user:
                clean_ref = referrer_id

        conn.execute(
            """INSERT INTO users(user_id, username, first_name, referrer_id, created_at)
               VALUES(?,?,?,?,?)""",
            (user.id, user.username, user.first_name, clean_ref, now()),
        )

        if clean_ref:
            conn.execute(
                """INSERT OR IGNORE INTO referrals(referrer_id, referred_id, created_at)
                   VALUES(?,?,?)""",
                (clean_ref, user.id, now()),
            )


async def is_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("Join check failed: %s", e)
        return False


def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎁 Refer & Earn", callback_data="refer"),
            InlineKeyboardButton("💰 Balance", callback_data="balance"),
        ],
        [
            InlineKeyboardButton("🎟 Withdraw", callback_data="withdraw"),
            InlineKeyboardButton("🏆 Leaderboard", callback_data="leaderboard"),
        ],
        [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
    ])


def join_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("✅ I Joined / Verify", callback_data="verify_join")],
    ])


async def send_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Welcome to OverLord X Referral Bot</b>\n\n"
        "Earn BigBasket coupon by inviting friends.\n"
        f"Your friend must join {CHANNEL_USERNAME} using your referral link.\n\n"
        f"✅ 1 valid referral = 1 point\n"
        f"🎟 Withdraw BigBasket code at {REQUIRED_REFERRALS} points\n\n"
        "<i>Use the buttons below to get started.</i>"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)
    else:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu(), parse_mode=ParseMode.HTML)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    referrer_id = None
    if context.args:
        try:
            referrer_id = int(context.args[0])
        except ValueError:
            referrer_id = None

    upsert_user(update.effective_user, referrer_id)

    if not await is_member(context, update.effective_user.id):
        await update.message.reply_text(
            "📢 <b>Join our Telegram channel first</b>\n\n"
            f"Channel: {CHANNEL_USERNAME}\n\n"
            "After joining, click <b>I Joined / Verify</b>.",
            reply_markup=join_menu(),
            parse_mode=ParseMode.HTML,
        )
        return

    await verify_and_credit(update, context, silent=True)
    await send_home(update, context)


async def verify_and_credit(update: Update, context: ContextTypes.DEFAULT_TYPE, silent: bool = False):
    user = update.effective_user
    upsert_user(user)

    if not await is_member(context, user.id):
        msg = "❌ You have not joined the channel yet. Please join first, then verify."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        elif not silent:
            await update.message.reply_text(msg)
        return False

    credited_now = False

    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE user_id=?", (user.id,)).fetchone()
        conn.execute("UPDATE users SET joined_verified=1 WHERE user_id=?", (user.id,))

        if u and u["referrer_id"]:
            ref = conn.execute(
                "SELECT * FROM referrals WHERE referrer_id=? AND referred_id=?",
                (u["referrer_id"], user.id),
            ).fetchone()

            if ref and ref["credited"] == 0:
                conn.execute("UPDATE referrals SET credited=1 WHERE id=?", (ref["id"],))
                conn.execute("UPDATE users SET points=points+1 WHERE user_id=?", (u["referrer_id"],))
                credited_now = True
                referrer_id = u["referrer_id"]
            else:
                referrer_id = None
        else:
            referrer_id = None

    if credited_now and referrer_id:
        name = user.first_name or user.username or str(user.id)
        try:
            await context.bot.send_message(
                chat_id=referrer_id,
                text=(
                    "✅ <b>New Referral Joined!</b>\n\n"
                    f"👤 User: {name}\n"
                    "🎉 You got <b>+1 point</b>.\n\n"
                    "Use 💰 Balance to check your points."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.warning("Could not notify referrer: %s", e)

    if update.callback_query:
        await update.callback_query.answer("✅ Verified successfully!")
        await send_home(update, context)
    return True


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = update.effective_user.id

    if data == "verify_join":
        await verify_and_credit(update, context)
        return

    if data == "refer":
        bot_user = await context.bot.get_me()
        link = f"https://t.me/{bot_user.username}?start={user_id}"
        await q.edit_message_text(
            "🎁 <b>Refer & Earn</b>\n\n"
            "Share this link with friends:\n"
            f"<code>{link}</code>\n\n"
            f"✅ Friend must join {CHANNEL_USERNAME}\n"
            "✅ After verification you get 1 point\n"
            f"🎟 {REQUIRED_REFERRALS} points = BigBasket code",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode=ParseMode.HTML,
        )

    elif data == "balance":
        with db() as conn:
            u = conn.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
            total_refs = conn.execute(
                "SELECT COUNT(*) AS c FROM referrals WHERE referrer_id=? AND credited=1",
                (user_id,),
            ).fetchone()["c"]
        points = u["points"] if u else 0
        await q.edit_message_text(
            "💰 <b>Your Balance</b>\n\n"
            f"✅ Valid referrals: <b>{total_refs}</b>\n"
            f"⭐ Points: <b>{points}</b>\n\n"
            f"Need <b>{REQUIRED_REFERRALS}</b> points to withdraw BigBasket code.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode=ParseMode.HTML,
        )

    elif data == "withdraw":
        with db() as conn:
            u = conn.execute("SELECT points FROM users WHERE user_id=?", (user_id,)).fetchone()
            points = u["points"] if u else 0
            available_code = conn.execute("SELECT * FROM codes WHERE used_by IS NULL ORDER BY id LIMIT 1").fetchone()

            if points < REQUIRED_REFERRALS:
                await q.edit_message_text(
                    "❌ <b>Not enough points</b>\n\n"
                    f"You have <b>{points}</b> points.\n"
                    f"You need <b>{REQUIRED_REFERRALS}</b> points to withdraw.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
                    parse_mode=ParseMode.HTML,
                )
                return

            if not available_code:
                await q.edit_message_text(
                    "⚠️ <b>BigBasket codes are currently out of stock.</b>\n\n"
                    "Please try again later.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
                    parse_mode=ParseMode.HTML,
                )
                return

            code = available_code["code"]
            conn.execute("UPDATE users SET points=points-? WHERE user_id=?", (REQUIRED_REFERRALS, user_id))
            conn.execute("UPDATE codes SET used_by=?, used_at=? WHERE id=?", (user_id, now(), available_code["id"]))
            conn.execute(
                "INSERT INTO withdrawals(user_id, status, code, created_at) VALUES(?,?,?,?)",
                (user_id, "approved", code, now()),
            )

        await q.edit_message_text(
            "🎉 <b>Withdraw Successful!</b>\n\n"
            "Here is your BigBasket code:\n"
            f"<code>{code}</code>\n\n"
            "Use it before expiry.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode=ParseMode.HTML,
        )

        for admin in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin,
                    f"🎟 Withdrawal done\nUser: {user_id}\nCode: {code}",
                )
            except Exception:
                pass

    elif data == "leaderboard":
        with db() as conn:
            rows = conn.execute(
                "SELECT user_id, username, first_name, points FROM users ORDER BY points DESC LIMIT 10"
            ).fetchall()

        lines = ["🏆 <b>Top Referrers</b>\n"]
        for i, r in enumerate(rows, 1):
            name = r["username"] or r["first_name"] or str(r["user_id"])
            lines.append(f"{i}. {name} — {r['points']} points")
        await q.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode=ParseMode.HTML,
        )

    elif data == "help":
        await q.edit_message_text(
            "ℹ️ <b>How this bot works</b>\n\n"
            "1️⃣ Join the channel\n"
            "2️⃣ Share your referral link\n"
            "3️⃣ Friend joins using your link\n"
            "4️⃣ You get 1 point after verification\n"
            f"5️⃣ Withdraw BigBasket code after {REQUIRED_REFERRALS} points\n\n"
            "Support: @Codes_StoreSupportBot",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="back")]]),
            parse_mode=ParseMode.HTML,
        )

    elif data == "back":
        await send_home(update, context)


async def addcode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    if not context.args:
        await update.message.reply_text(
            "Use:\n/addcode CODE1\n\nOr multiple codes:\n/addcode CODE1 | CODE2 | CODE3"
        )
        return

    raw = " ".join(context.args)
    codes = [c.strip() for c in raw.split("|") if c.strip()]

    with db() as conn:
        for c in codes:
            conn.execute("INSERT INTO codes(code, added_at) VALUES(?,?)", (c, now()))

    await update.message.reply_text(f"✅ Added {len(codes)} BigBasket code(s).")


async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    with db() as conn:
        available = conn.execute("SELECT COUNT(*) AS c FROM codes WHERE used_by IS NULL").fetchone()["c"]
        used = conn.execute("SELECT COUNT(*) AS c FROM codes WHERE used_by IS NOT NULL").fetchone()["c"]

    await update.message.reply_text(f"📦 Stock\nAvailable: {available}\nUsed: {used}")


async def users_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return

    with db() as conn:
        users = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
        verified = conn.execute("SELECT COUNT(*) AS c FROM users WHERE joined_verified=1").fetchone()["c"]
        refs = conn.execute("SELECT COUNT(*) AS c FROM referrals WHERE credited=1").fetchone()["c"]

    await update.message.reply_text(
        f"👥 Users: {users}\n✅ Verified joined: {verified}\n🎁 Valid referrals: {refs}"
    )


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Please set BOT_TOKEN in environment or paste it in bot.py")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addcode", addcode))
    app.add_handler(CommandHandler("stock", stock))
    app.add_handler(CommandHandler("users", users_count))
    app.add_handler(CallbackQueryHandler(on_button))

    log.info("Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
