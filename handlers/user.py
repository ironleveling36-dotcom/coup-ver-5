"""
handlers/user.py - User-facing handlers: start (+referral capture), browse,
wallet view, transaction history, my orders, referral home, notifications.

Every callback is wrapped so a failure can never crash the bot ("error-free").
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    ConversationHandler, MessageHandler, filters,
)

import config
import keyboards
import messages
from database import Database
from handlers.channel import ensure_gate
from utils import (
    format_currency, fmt_dt, generate_ref_code, tiers_summary,
)

logger = logging.getLogger(__name__)


# ── safety wrapper ────────────────────────────────────────────────────────────
def safe(func):
    """Wrap a handler so any exception is caught and shown gently."""
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            return await func(update, ctx)
        except TelegramError as e:
            logger.warning("Telegram error in %s: %s", func.__name__, e)
        except Exception:
            logger.exception("Handler %s crashed", func.__name__)
            try:
                if update.callback_query:
                    await update.callback_query.answer(
                        "⚠️ Something went wrong. Please try again.", show_alert=True)
                elif update.message:
                    await update.message.reply_text(
                        "⚠️ Something went wrong. Please try /start again.")
            except Exception:
                pass
    return wrapper


async def _guard(update: Update, db: Database, ctx=None) -> bool:
    """Return True if the user is allowed to proceed (ban + maintenance +
    force-channel membership). Staff bypass everything."""
    user = update.effective_user
    if await db.is_staff(user.id):
        return True
    target = update.callback_query.message if update.callback_query else update.message
    if await db.is_banned(user.id):
        await target.reply_text(messages.banned(), parse_mode=ParseMode.MARKDOWN)
        return False
    if await db.get_setting("maintenance") == "true":
        await target.reply_text(messages.maintenance(), parse_mode=ParseMode.MARKDOWN)
        return False
    if ctx is not None and not await ensure_gate(update, ctx):
        return False
    return True


def _bot_username(ctx) -> str:
    return config.BOT_USERNAME or (ctx.bot.username if ctx.bot and ctx.bot.username else "")


# ══════════════════════════════════════════════════════════════════════════
# START (+ referral capture)
# ══════════════════════════════════════════════════════════════════════════
@safe
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db = await Database.get_instance()
    rec = await db.upsert_user(user.id, user.username or "", user.full_name or "")

    # Referral deep-link: /start ref_<code>
    if ctx.args:
        await _try_capture_referral(update, ctx, db, ctx.args[0])

    if not await _guard(update, db, ctx):
        return

    await update.message.reply_text(
        messages.welcome(user.first_name, rec.get("wallet_balance", 0.0)),
        reply_markup=keyboards.main_menu_kb(show_reseller=await db.reseller_enabled()),
        parse_mode=ParseMode.MARKDOWN,
    )


async def _try_capture_referral(update, ctx, db: Database, payload: str):
    cfg = await db.get_referral_config()
    if not cfg["enabled"]:
        return
    if not payload.startswith("ref_"):
        return
    code = payload[4:]
    # Resolve code -> referrer user id. We use base36 of the user id as code.
    referrer_id = None
    try:
        referrer_id = int(code, 36)
    except ValueError:
        return
    me = update.effective_user.id
    if referrer_id == me:
        return
    referrer = await db.get_user(referrer_id)
    if not referrer:
        return  # unknown referrer
    linked = await db.set_referrer(me, referrer_id)
    if not linked:
        return
    # Optional welcome bonus for the new user.
    if cfg["welcome_bonus"] > 0:
        bal = await db.credit_wallet(me, cfg["welcome_bonus"], ttype="referral",
                                     ref=f"welcome:{referrer_id}",
                                     note="Referral welcome bonus")
        try:
            await update.message.reply_text(
                messages.referral_reward(cfg["welcome_bonus"], "welcome", bal),
                parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════════
# MAIN MENU / WALLET
# ══════════════════════════════════════════════════════════════════════════
@safe
async def cbq_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    balance = await db.get_balance(query.from_user.id)
    await query.edit_message_text(
        messages.welcome(query.from_user.first_name, balance),
        reply_markup=keyboards.main_menu_kb(show_reseller=await db.reseller_enabled()),
        parse_mode=ParseMode.MARKDOWN,
    )


@safe
async def cbq_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    if not await _guard(update, db, ctx):
        return
    u = await db.get_user(query.from_user.id) or await db.upsert_user(
        query.from_user.id, query.from_user.username or "", query.from_user.full_name or "")
    await query.edit_message_text(
        messages.wallet_overview(
            u.get("wallet_balance", 0.0),
            u.get("total_recharged", 0.0),
            u.get("total_spent", 0.0),
            u.get("ref_earnings", 0.0),
        ),
        reply_markup=keyboards.wallet_kb(),
        parse_mode=ParseMode.MARKDOWN,
    )


@safe
async def cbq_txn_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    txns = await db.get_transactions(query.from_user.id, limit=15)

    if not txns:
        await query.edit_message_text(
            "📜 *Transaction History*\n\nNo transactions yet.",
            reply_markup=keyboards.wallet_kb(), parse_mode=ParseMode.MARKDOWN)
        return

    lines = ["📜 *Transaction History*\n"]
    icons = {"recharge": "⬆️", "purchase": "🛒", "admin_adjust": "🛠️",
             "refund": "↩️", "referral": "🎁"}
    for t in txns:
        sign = "+" if t["amount"] >= 0 else "−"
        icon = icons.get(t["type"], "•")
        lines.append(
            f"{icon} {sign}{format_currency(abs(t['amount']))} • "
            f"{t['type']} • {fmt_dt(t['created_at'])}")
    await query.edit_message_text(
        "\n".join(lines), reply_markup=keyboards.wallet_kb(),
        parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════
# BROWSE
# ══════════════════════════════════════════════════════════════════════════
@safe
async def cbq_browse(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    if not await _guard(update, db, ctx):
        return
    await _render_browse(query, ctx, db, page=0)


async def _render_browse(query, ctx, db, page: int):
    from utils import paginate
    categories = await db.get_categories(active_only=True, storefront=True)
    if not categories:
        await query.edit_message_text(
            messages.no_categories(), reply_markup=keyboards.back_to_main_kb(),
            parse_mode=ParseMode.MARKDOWN)
        return
    page_items, page, total_pages = paginate(categories, page, config.PAGE_SIZE)
    stock_map = {c["id"]: await db.stock_count(c["id"]) for c in page_items}
    await query.edit_message_text(
        f"🛍️ *Available Coupons*  (page {page+1}/{total_pages})\n\n"
        "Select a category to continue:",
        reply_markup=keyboards.categories_kb(page_items, stock_map, page, total_pages),
        parse_mode=ParseMode.MARKDOWN)


async def cbq_browse_page(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    page = int(query.data.split("browse_page_")[1])
    await _render_browse(query, ctx, db, page)


@safe
async def cbq_select_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show the coupon card + Terms & Conditions. User must tap I Agree."""
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split("_")[1])
    db = await Database.get_instance()
    if not await _guard(update, db, ctx):
        return

    cat = await db.get_category(cat_id)
    if not cat or not cat.get("is_active", True):
        await query.answer("Category not available!", show_alert=True)
        return

    stock = await db.stock_count(cat_id)
    balance = await db.get_balance(query.from_user.id)
    if stock == 0:
        await query.edit_message_text(
            messages.out_of_stock_msg(cat["name"]),
            reply_markup=keyboards.back_to_main_kb(), parse_mode=ParseMode.MARKDOWN)
        return

    favs = await db.get_favorites(query.from_user.id)
    tiers = await db.get_discount_tiers()
    text = messages.coupon_card(cat["name"], cat["price"], stock, balance,
                                tiers_summary(tiers), cat.get("terms", ""))
    await query.edit_message_text(
        text, reply_markup=keyboards.category_card_kb(cat_id, cat_id in favs),
        parse_mode=ParseMode.MARKDOWN)


@safe
async def cbq_agree(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User accepted the T&C -> show quantity selection."""
    query = update.callback_query
    await query.answer("Terms accepted ✅")
    cat_id = int(query.data.split("agree_")[1])
    db = await Database.get_instance()
    if not await _guard(update, db, ctx):
        return
    ctx.user_data.setdefault("agreed_cats", set())
    ctx.user_data["agreed_cats"].add(cat_id)
    cat = await db.get_category(cat_id)
    if not cat:
        await query.answer("Category not found!", show_alert=True)
        return
    stock = await db.stock_count(cat_id)
    balance = await db.get_balance(query.from_user.id)
    tiers = await db.get_discount_tiers()
    await query.edit_message_text(
        messages.category_detail(cat["name"], cat["price"], stock, balance,
                                 tiers_summary(tiers)),
        reply_markup=keyboards.quantity_kb(cat_id), parse_mode=ParseMode.MARKDOWN)


@safe
async def cbq_fav(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cat_id = int(query.data.split("fav_")[1])
    db = await Database.get_instance()
    added = await db.toggle_favorite(query.from_user.id, cat_id)
    await query.answer("⭐ Added to favorites" if added else "💔 Removed")
    cat = await db.get_category(cat_id)
    if not cat:
        return
    stock = await db.stock_count(cat_id)
    balance = await db.get_balance(query.from_user.id)
    tiers = await db.get_discount_tiers()
    text = messages.coupon_card(cat["name"], cat["price"], stock, balance,
                                tiers_summary(tiers), cat.get("terms", ""))
    await query.edit_message_text(
        text, reply_markup=keyboards.category_card_kb(cat_id, added),
        parse_mode=ParseMode.MARKDOWN)


@safe
async def cbq_favorites(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    if not await _guard(update, db, ctx):
        return
    fav_ids = await db.get_favorites(query.from_user.id)
    cats = [c for c in await db.get_categories(active_only=True, storefront=True)
            if c["id"] in fav_ids]
    if not cats:
        await query.edit_message_text(
            "⭐ *Favorites*\n\nYou haven't saved any coupons yet.\n"
            "Open a coupon and tap *Add Favorite*.",
            reply_markup=keyboards.back_to_main_kb(), parse_mode=ParseMode.MARKDOWN)
        return
    stock_map = {c["id"]: await db.stock_count(c["id"]) for c in cats}
    await query.edit_message_text(
        "⭐ *Your Favorite Coupons*", reply_markup=keyboards.favorites_kb(cats, stock_map),
        parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════
# REFERRAL
# ══════════════════════════════════════════════════════════════════════════
@safe
async def cbq_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    cfg = await db.get_referral_config()
    uid = query.from_user.id
    stats = await db.referral_stats(uid)

    if not cfg["enabled"]:
        await query.edit_message_text(
            "🎁 *Referral Program*\n\nThe referral program is currently disabled. "
            "Check back soon!", reply_markup=keyboards.back_to_main_kb(),
            parse_mode=ParseMode.MARKDOWN)
        return

    username = _bot_username(ctx)
    code = generate_ref_code(uid)
    ref_link = f"https://t.me/{username}?start=ref_{code}" if username else \
               f"Your code: ref_{code}"
    from urllib.parse import quote
    share_text = quote(f"Join {config.BOT_NAME} and get instant coupons! {ref_link}")
    share_url = f"https://t.me/share/url?url={quote(ref_link)}&text={share_text}"

    await query.edit_message_text(
        messages.referral_home(ref_link, stats["count"], stats["earnings"], cfg),
        reply_markup=keyboards.referral_kb(share_url),
        parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


@safe
async def cbq_ref_leaderboard(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    top = await db.referral_leaderboard(limit=10)
    lines = ["🏆 *Top Referrers*\n"]
    medals = ["🥇", "🥈", "🥉"]
    if not top:
        lines.append("No referrals yet. Be the first! 🚀")
    for i, u in enumerate(top):
        badge = medals[i] if i < 3 else f"{i+1}."
        name = u.get("full_name") or (f"@{u['username']}" if u.get("username") else f"User {u['user_id']}")
        lines.append(f"{badge} {name} — {u.get('ref_count',0)} invites • "
                     f"{format_currency(u.get('ref_earnings',0))}")
    await query.edit_message_text(
        "\n".join(lines), reply_markup=keyboards.back_to_main_kb(),
        parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════
@safe
async def cbq_notify_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    u = await db.get_user(query.from_user.id) or {}
    enabled = u.get("notify", True)
    await query.edit_message_text(
        "🔔 *Notification Settings*\n\n"
        "Get alerts for new coupons, special offers, wallet credits and order "
        "updates.\n\nWallet & order alerts are always delivered.",
        reply_markup=keyboards.notify_kb(enabled), parse_mode=ParseMode.MARKDOWN)


@safe
async def cbq_notify_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db = await Database.get_instance()
    u = await db.get_user(query.from_user.id) or {}
    new = not u.get("notify", True)
    await db.set_notify(query.from_user.id, new)
    await query.answer(f"Notifications {'ON' if new else 'OFF'}")
    await query.edit_message_text(
        "🔔 *Notification Settings*\n\n"
        "Get alerts for new coupons, special offers, wallet credits and order "
        "updates.\n\nWallet & order alerts are always delivered.",
        reply_markup=keyboards.notify_kb(new), parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════
# HELP / ORDERS
# ══════════════════════════════════════════════════════════════════════════
@safe
async def cbq_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        messages.help_msg(), reply_markup=keyboards.back_to_main_kb(),
        parse_mode=ParseMode.MARKDOWN)


@safe
async def cbq_my_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    orders = await db.get_user_orders(query.from_user.id, limit=15)
    if not orders:
        await query.edit_message_text(
            "📦 *No orders found.*\n\nYou haven't purchased anything yet!",
            reply_markup=keyboards.back_to_main_kb(), parse_mode=ParseMode.MARKDOWN)
        return
    await query.edit_message_text(
        "📦 *Your Recent Orders:*\n\nSelect an order to view its coupon codes.",
        reply_markup=keyboards.my_orders_kb(orders), parse_mode=ParseMode.MARKDOWN)


@safe
async def cbq_view_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = query.data.split("vieworder_")[1]
    db = await Database.get_instance()
    order = await db.get_order(order_id)
    if not order or order["user_id"] != query.from_user.id:
        await query.answer("Order not found!", show_alert=True)
        return
    items = order.get("items", [])
    codes = "\n".join(f"{i}. `{c}`" for i, c in enumerate(items, 1)) or "_No codes stored_"
    text = (
        f"📋 *Order Details*\n\n"
        f"Order ID: `{order['order_id']}`\n"
        f"Category: {order.get('category_name', 'N/A')}\n"
        f"Quantity: {order['quantity']}\n"
        f"Amount: {format_currency(order['amount'])}\n"
        f"Status: {order['status'].upper()}\n"
        f"Date: {fmt_dt(order.get('created_at'))}\n\n"
        f"🎁 *Coupon Codes:*\n{codes}")
    await query.edit_message_text(
        text, reply_markup=keyboards.back_to_main_kb(), parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════
# SEARCH (conversation)
# ══════════════════════════════════════════════════════════════════════════
SEARCH_TERM = 100


@safe
async def cbq_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    if not await _guard(update, db, ctx):
        return ConversationHandler.END
    await query.edit_message_text(
        "🔎 *Search Coupons*\n\nType a keyword to find a coupon category:",
        reply_markup=keyboards.back_to_main_kb(), parse_mode=ParseMode.MARKDOWN)
    return SEARCH_TERM


@safe
async def search_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    results = await db.search_categories(update.message.text)
    if not results:
        await update.message.reply_text(
            "😔 No coupons matched that search.",
            reply_markup=keyboards.back_to_main_kb(), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    stock_map = {c["id"]: await db.stock_count(c["id"]) for c in results[:config.PAGE_SIZE]}
    await update.message.reply_text(
        f"🔎 *Results ({len(results)})*", parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboards.categories_kb(results[:config.PAGE_SIZE], stock_map))
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# RESELLER
# ══════════════════════════════════════════════════════════════════════════
@safe
async def cbq_reseller(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    if not await db.reseller_enabled():
        await query.edit_message_text(
            messages.reseller_disabled(), reply_markup=keyboards.back_to_main_kb(),
            parse_mode=ParseMode.MARKDOWN)
        return
    role = await db.effective_role(query.from_user.id)
    if role in ("reseller", "admin", "super_admin"):
        await query.edit_message_text(
            "🏪 *Reseller*\n\nYou already have seller access! Use /admin to open "
            "your seller dashboard.", reply_markup=keyboards.back_to_main_kb(),
            parse_mode=ParseMode.MARKDOWN)
        return
    cfg = await db.get_reseller_config()
    bal = await db.get_balance(query.from_user.id)
    await query.edit_message_text(
        messages.reseller_info(cfg["fee"], bal, cfg["auto_approve"]),
        reply_markup=keyboards.reseller_kb(), parse_mode=ParseMode.MARKDOWN)


@safe
async def cbq_reseller_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db = await Database.get_instance()
    if not await db.reseller_enabled():
        await query.answer("Reseller program is currently unavailable.", show_alert=True)
        await query.edit_message_text(
            messages.reseller_disabled(), reply_markup=keyboards.back_to_main_kb(),
            parse_mode=ParseMode.MARKDOWN)
        return
    cfg = await db.get_reseller_config()
    fee = cfg["fee"]
    uid = query.from_user.id

    new_bal = await db.debit_wallet(uid, fee, ttype="purchase",
                                    note="Reseller activation fee")
    if new_bal is None:
        await query.answer("Insufficient balance — please recharge.", show_alert=True)
        bal = await db.get_balance(uid)
        await query.edit_message_text(
            messages.insufficient_balance(fee, bal),
            reply_markup=keyboards.wallet_kb(), parse_mode=ParseMode.MARKDOWN)
        return
    await query.answer("Payment received ✅")
    await db.create_reseller_request(uid, fee)

    from notifications import notify_admins
    if cfg["auto_approve"]:
        await db.activate_reseller(uid)
        await db.set_reseller_request_status(uid, "approved")
        await query.edit_message_text(
            messages.reseller_activated(), reply_markup=keyboards.back_to_main_kb(),
            parse_mode=ParseMode.MARKDOWN)
        await notify_admins(ctx.bot,
            f"🏪 New reseller auto-activated: @{query.from_user.username or uid}")
    else:
        await query.edit_message_text(
            messages.reseller_requested(), reply_markup=keyboards.back_to_main_kb(),
            parse_mode=ParseMode.MARKDOWN)
        await notify_admins(ctx.bot,
            f"🏪 *Reseller request* from @{query.from_user.username or uid} "
            f"(`{uid}`) — fee {format_currency(fee)} paid. Approve in Admin → Staff.")


async def cbq_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


def register_user_handlers(app):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cbq_main_menu, pattern="^main_menu$"))
    app.add_handler(CallbackQueryHandler(cbq_wallet, pattern="^wallet$"))
    app.add_handler(CallbackQueryHandler(cbq_txn_history, pattern="^txn_history$"))
    app.add_handler(CallbackQueryHandler(cbq_browse, pattern="^browse$"))
    app.add_handler(CallbackQueryHandler(cbq_browse_page, pattern=r"^browse_page_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_select_category, pattern=r"^cat_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_agree, pattern=r"^agree_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_fav, pattern=r"^fav_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_favorites, pattern="^favorites$"))
    app.add_handler(CallbackQueryHandler(cbq_referral, pattern="^referral$"))
    app.add_handler(CallbackQueryHandler(cbq_ref_leaderboard, pattern="^ref_leaderboard$"))
    app.add_handler(CallbackQueryHandler(cbq_notify_menu, pattern="^notify_menu$"))
    app.add_handler(CallbackQueryHandler(cbq_notify_toggle, pattern="^notify_toggle$"))
    app.add_handler(CallbackQueryHandler(cbq_help, pattern="^help$"))
    app.add_handler(CallbackQueryHandler(cbq_my_orders, pattern="^my_orders$"))
    app.add_handler(CallbackQueryHandler(cbq_view_order, pattern=r"^vieworder_ORD-\w+-\w+$"))
    app.add_handler(CallbackQueryHandler(cbq_reseller, pattern="^reseller$"))
    app.add_handler(CallbackQueryHandler(cbq_reseller_pay, pattern="^reseller_pay$"))
    app.add_handler(CallbackQueryHandler(cbq_noop, pattern="^noop$"))

    # Search conversation
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cbq_search, pattern="^search$")],
        states={SEARCH_TERM: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_input)]},
        fallbacks=[CallbackQueryHandler(cbq_main_menu, pattern="^main_menu$")],
        per_chat=True, per_user=True,
    ))
