"""
handlers/channel.py - Force-join channel gate.

Users must be a member of the configured channel before using the bot. The gate
is checked on /start, before browsing, before wallet ops and again right before
completing a purchase. Membership is re-verified live (short positive cache) so
if a user leaves the channel they are restricted again immediately.

Design for reliability ("never crash / never lock everyone out"):
  • FAIL-CLOSED only on a definitive "left"/"kicked" status.
  • FAIL-OPEN on API errors (e.g. bot not admin, transient network) so a
    misconfiguration can never brick the whole bot — instead we warn staff.
"""

import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes, CallbackQueryHandler

import config
from database import Database

logger = logging.getLogger(__name__)

# user_id -> expiry timestamp for a positive membership result
_ok_cache: dict[int, float] = {}
_warned = {"missing_perm": 0.0}


def _clear_cache(user_id: int):
    _ok_cache.pop(user_id, None)


async def is_member(bot, channel: str, user_id: int) -> bool:
    """True if the user is in `channel`. Fail-open on API errors."""
    if not channel:
        return True
    now = time.monotonic()
    exp = _ok_cache.get(user_id)
    if exp and exp > now:
        return True
    try:
        m = await bot.get_chat_member(channel, user_id)
        status = getattr(m, "status", "")
        if status in ("creator", "administrator", "member"):
            _ok_cache[user_id] = now + config.MEMBERSHIP_CACHE_TTL
            return True
        if status == "restricted":
            ok = bool(getattr(m, "is_member", False))
            if ok:
                _ok_cache[user_id] = now + config.MEMBERSHIP_CACHE_TTL
            return ok
        # left / kicked -> definitively not a member
        _clear_cache(user_id)
        return False
    except TelegramError as e:
        # Bot likely not admin in the channel, or channel id wrong.
        logger.warning("Force-channel check failed (fail-open): %s", e)
        if time.monotonic() - _warned["missing_perm"] > 3600:
            _warned["missing_perm"] = time.monotonic()
            try:
                from notifications import notify_admins
                await notify_admins(
                    bot,
                    "⚠️ *Force-Channel check failed.* Make sure the bot is an "
                    f"*admin* of `{channel}` and the ID/username is correct. "
                    "Access is temporarily open until this is fixed.")
            except Exception:
                pass
        return True
    except Exception:
        logger.exception("Unexpected force-channel error (fail-open)")
        return True


def _join_kb(url: str) -> InlineKeyboardMarkup:
    rows = []
    if url:
        rows.append([InlineKeyboardButton("📢 Join Channel", url=url)])
    rows.append([InlineKeyboardButton("✅ I've Joined", callback_data="check_join")])
    return InlineKeyboardMarkup(rows)


def _channel_url(cfg: dict) -> str:
    if cfg.get("url"):
        return cfg["url"]
    ch = cfg.get("channel", "")
    if ch.startswith("@"):
        return f"https://t.me/{ch[1:]}"
    return ""


async def ensure_gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Return True if the user may proceed. Otherwise show the join prompt and
    return False. Staff bypass the gate.
    """
    user = update.effective_user
    if not user:
        return True
    db = await Database.get_instance()
    if await db.is_staff(user.id):
        return True
    cfg = await db.get_force_channel()
    channel = cfg["channel"]
    if not channel:
        return True
    if await is_member(ctx.bot, channel, user.id):
        return True

    text = (
        "🔒 *Members Only*\n\n"
        "To use this bot you must join our official channel first.\n\n"
        "1️⃣ Tap *Join Channel*\n"
        "2️⃣ Come back and tap *I've Joined*")
    kb = _join_kb(_channel_url(cfg))
    target = update.callback_query.message if update.callback_query else update.message
    try:
        if update.callback_query:
            await update.callback_query.answer()
            await target.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        else:
            await target.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        try:
            await ctx.bot.send_message(user.id, text, reply_markup=kb,
                                       parse_mode=ParseMode.MARKDOWN)
        except TelegramError:
            pass
    return False


async def cbq_check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db = await Database.get_instance()
    cfg = await db.get_force_channel()
    _clear_cache(query.from_user.id)  # force a fresh check
    if await is_member(ctx.bot, cfg["channel"], query.from_user.id):
        await query.answer("✅ Verified! Welcome.", show_alert=False)
        import messages, keyboards
        bal = await db.get_balance(query.from_user.id)
        await query.edit_message_text(
            messages.welcome(query.from_user.first_name, bal),
            reply_markup=keyboards.main_menu_kb(show_reseller=await db.reseller_enabled()),
            parse_mode=ParseMode.MARKDOWN)
    else:
        await query.answer("❌ You haven't joined yet. Please join and retry.",
                           show_alert=True)


def register_channel_handlers(app):
    app.add_handler(CallbackQueryHandler(cbq_check_join, pattern="^check_join$"))
