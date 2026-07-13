"""
handlers/admin.py - Full admin control dashboard.
Role-aware routing via @requires_role("permission").
"""

import json
import logging
from io import BytesIO
from functools import wraps

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

import config
import keyboards
import messages
from database import Database
from notifications import broadcast, fanout_new_coupon, notify_super_admins
from utils import (
    role_can, role_label, safe_int, safe_float, format_currency, fmt_dt,
)

logger = logging.getLogger(__name__)

# ── Conversation states ───────────────────────────────────────────────────────
(
    ADD_CAT_NAME, ADD_CAT_PRICE,
    EDIT_NAME, EDIT_PRICE,
    ADD_STOCK,
    WALLET_ADD_UID, WALLET_ADD_AMT,
    WALLET_DED_UID, WALLET_DED_AMT,
    WALLET_CHECK_UID,
    BAN_UID, UNBAN_UID,
    BC_MSG,
    SET_UPI, SET_PAYEE, SET_LOW_STOCK,
    REF_SIGNUP, REF_COMMISSION, REF_WELCOME,
    STAFF_ADD_UID, RESTORE_FILE,
    EDIT_TERMS, EDIT_EXPIRY,
    WD_UPI, WD_COMMISSION,
) = range(25)


def requires_role(permission: str):
    """Decorator to enforce role permissions on admin commands."""
    def decorator(func):
        @wraps(func)
        async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            user_id = update.effective_user.id
            db = await Database.get_instance()
            role = await db.effective_role(user_id)
            if not role_can(role, permission):
                if update.callback_query:
                    await update.callback_query.answer("🚫 Permission denied.", show_alert=True)
                else:
                    await update.message.reply_text("🚫 You don't have permission for this.")
                return ConversationHandler.END
            ctx.user_data["_admin_role"] = role
            return await func(update, ctx)
        return wrapper
    return decorator


async def _close(update, ctx):
    if update.callback_query:
        try:
            await update.callback_query.message.delete()
        except TelegramError:
            pass
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# MAIN MENU
# ══════════════════════════════════════════════════════════════════════════
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    role = await db.effective_role(update.effective_user.id)
    if role == "user":
        return
    text = f"🛡️ *Admin Control Panel*\nRole: {role_label(role)}\n\nSelect an option below:"
    kb = keyboards.admin_menu_kb(role)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)


async def cbq_admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_admin(update, ctx)


# ══════════════════════════════════════════════════════════════════════════
# COUPONS
# ══════════════════════════════════════════════════════════════════════════
async def _scoped_owner(ctx) -> int | None:
    """Return the owner_id filter for the current staff member, or None for
    super admin (sees everything)."""
    from utils import is_scoped
    role = ctx.user_data.get("_admin_role", "user")
    return None if not is_scoped(role) else ctx.user_data.get("_uid")


async def _can_touch(ctx, db, cat_id: int) -> bool:
    from utils import is_scoped
    role = ctx.user_data.get("_admin_role", "user")
    if not is_scoped(role):
        return True
    return await db.category_owned_by(cat_id, ctx.user_data.get("_uid", -1))


@requires_role("coupons")
async def cbq_coupons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    ctx.user_data["_uid"] = query.from_user.id
    owner = await _scoped_owner(ctx)
    cats = await db.get_categories(active_only=False, owner_id=owner)
    scope = "your" if owner is not None else "all"
    await query.edit_message_text(
        f"🏷️ *Manage Coupons* ({scope})\n\nSelect a category or add a new one:",
        reply_markup=keyboards.admin_coupons_kb(cats), parse_mode=ParseMode.MARKDOWN)


@requires_role("coupons")
async def cbq_category(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split("adm_cat_")[1])
    ctx.user_data["_uid"] = query.from_user.id
    db = await Database.get_instance()
    if not await _can_touch(ctx, db, cat_id):
        await query.answer("🚫 Not your category.", show_alert=True)
        return
    cat = await db.get_category(cat_id)
    if not cat:
        return
    stock = await db.stock_count(cat_id)
    terms = (cat.get("terms") or "").strip()
    exp = fmt_dt(cat["expires_at"]) if cat.get("expires_at") else "none"
    text = (f"🏷️ *{cat['name']}*\n\n💵 Price: {format_currency(cat['price'])}\n"
            f"📦 Stock: {stock}\n⏳ Expiry: {exp}\n"
            f"📜 Terms: {'set ✅' if terms else 'default'}\n"
            f"Status: {'Active ✅' if cat.get('is_active') else 'Inactive 🚫'}")
    await query.edit_message_text(text, reply_markup=keyboards.admin_category_kb(cat_id, cat.get("is_active")),
                                  parse_mode=ParseMode.MARKDOWN)


@requires_role("coupons")
async def cbq_add_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("➕ *Add Category*\n\nSend the category name:", parse_mode=ParseMode.MARKDOWN)
    return ADD_CAT_NAME


async def add_cat_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["new_cat_name"] = update.message.text.strip()
    await update.message.reply_text("💵 Now send the price (e.g. 50 or 99.99):")
    return ADD_CAT_PRICE


async def add_cat_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    price = safe_float(update.message.text)
    if price is None or price < 0:
        await update.message.reply_text("❌ Invalid price. Send a number:")
        return ADD_CAT_PRICE
    db = await Database.get_instance()
    name = ctx.user_data["new_cat_name"]
    from utils import is_scoped
    role = await db.effective_role(update.effective_user.id)
    owner_id = update.effective_user.id if is_scoped(role) else 0
    try:
        await db.add_category(name, price, owner_id=owner_id)
    except Exception:
        await update.message.reply_text("❌ A category with that name already exists.", reply_markup=keyboards.admin_back_kb())
        return ConversationHandler.END
    await update.message.reply_text(f"✅ Category *{name}* added.\nNow add stock from the category menu.",
                                    reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


@requires_role("coupons")
async def cbq_togglecat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cat_id = int(query.data.split("adm_togglecat_")[1])
    db = await Database.get_instance()
    cat = await db.get_category(cat_id)
    if cat:
        new = not cat.get("is_active", True)
        await db.update_category(cat_id, is_active=new)
        await query.answer(f"Category {'Activated' if new else 'Deactivated'}")
    await cbq_category(update, ctx)


# ── Edit Terms & Conditions (per category) ──
@requires_role("coupons")
async def cbq_edit_terms(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split("adm_editterms_")[1])
    ctx.user_data["_uid"] = query.from_user.id
    db = await Database.get_instance()
    if not await _can_touch(ctx, db, cat_id):
        await query.answer("🚫 Not your category.", show_alert=True)
        return ConversationHandler.END
    ctx.user_data["terms_cat_id"] = cat_id
    await query.edit_message_text(
        "📜 *Edit Terms & Conditions*\n\nSend the full T&C text buyers must accept "
        "before purchasing this coupon.\nSend `-` to reset to the default terms.",
        parse_mode=ParseMode.MARKDOWN)
    return EDIT_TERMS

async def edit_terms_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    cid = ctx.user_data.get("terms_cat_id")
    text = update.message.text.strip()
    await db.update_category(cid, terms="" if text == "-" else text)
    await update.message.reply_text("✅ Terms updated.", reply_markup=keyboards.admin_back_kb())
    return ConversationHandler.END


# ── Set expiry (days from now; 0 = no expiry) ──
@requires_role("coupons")
async def cbq_edit_expiry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split("adm_editexpiry_")[1])
    ctx.user_data["_uid"] = query.from_user.id
    db = await Database.get_instance()
    if not await _can_touch(ctx, db, cat_id):
        await query.answer("🚫 Not your category.", show_alert=True)
        return ConversationHandler.END
    ctx.user_data["expiry_cat_id"] = cat_id
    await query.edit_message_text(
        "⏳ *Set Expiry*\n\nSend the number of *days* until this coupon expires "
        "(e.g. 30). Send `0` to remove any expiry.", parse_mode=ParseMode.MARKDOWN)
    return EDIT_EXPIRY

async def edit_expiry_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from datetime import datetime, timezone, timedelta
    days = safe_int(update.message.text)
    if days is None or days < 0:
        await update.message.reply_text("❌ Send a whole number of days (0 = none).")
        return EDIT_EXPIRY
    db = await Database.get_instance()
    cid = ctx.user_data.get("expiry_cat_id")
    exp = None if days == 0 else datetime.now(timezone.utc) + timedelta(days=days)
    await db.update_category(cid, expires_at=exp)
    await update.message.reply_text(
        "✅ Expiry cleared." if exp is None else f"✅ Expires in {days} day(s).",
        reply_markup=keyboards.admin_back_kb())
    return ConversationHandler.END


@requires_role("stock")
async def cbq_export_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Preparing CSV…")
    cat_id = int(query.data.split("adm_exportcat_")[1])
    ctx.user_data["_uid"] = query.from_user.id
    db = await Database.get_instance()
    if not await _can_touch(ctx, db, cat_id):
        await query.answer("🚫 Not your category.", show_alert=True)
        return
    cat = await db.get_category(cat_id)
    rows = await db.export_stock()
    rows = [r for r in rows if r["category"] == cat["name"]]
    import csv, io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=["category", "code", "status", "order_id"])
    w.writeheader()
    w.writerows(rows)
    data = io.BytesIO(buf.getvalue().encode())
    data.name = f"stock_{cat['name']}.csv"
    await ctx.bot.send_document(query.message.chat_id, data,
                                caption=f"📤 {cat['name']} — {len(rows)} codes")


@requires_role("stock")
async def cbq_add_stock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split("adm_addstock_")[1])
    ctx.user_data["_uid"] = query.from_user.id
    db = await Database.get_instance()
    if not await _can_touch(ctx, db, cat_id):
        await query.answer("🚫 Not your category.", show_alert=True)
        return ConversationHandler.END
    ctx.user_data["stock_cat_id"] = cat_id
    await query.edit_message_text(
        "➕ *Add Stock (Bulk Upload)*\n\nSend the coupon codes — *one per line*.\n"
        "Duplicates are detected and skipped automatically.", parse_mode=ParseMode.MARKDOWN)
    return ADD_STOCK


async def add_stock_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    cid = ctx.user_data["stock_cat_id"]
    was_empty = await db.stock_count(cid) == 0
    items = [ln for ln in update.message.text.splitlines() if ln.strip()]
    res = await db.add_stock(cid, items)   # {added, skipped}
    dup = f"  ⚠️ {res['skipped']} duplicate(s) skipped." if res["skipped"] else ""
    await update.message.reply_text(
        f"✅ Added *{res['added']}* coupon code(s).{dup}",
        reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    # Only fan out a "new coupons" alert when a previously-empty category is refilled
    if res["added"] > 0 and was_empty:
        cat = await db.get_category(cid)
        if cat and cat.get("is_active"):
            await fanout_new_coupon(ctx.bot, cat["name"], cat["price"])
    return ConversationHandler.END


# ... (del_cat, edit_name, edit_price flow the same but wrapped in @requires_role("coupons"))
@requires_role("coupons")
async def cbq_del_cat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split("adm_delcat_")[1])
    await query.edit_message_text("🗑️ *Delete this category and ALL its stock?*\nThis cannot be undone.",
                                  reply_markup=keyboards.admin_confirm_delete_kb(cat_id), parse_mode=ParseMode.MARKDOWN)

@requires_role("coupons")
async def cbq_del_cat_yes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat_id = int(query.data.split("adm_delcatyes_")[1])
    db = await Database.get_instance()
    await db.delete_category(cat_id)
    await query.edit_message_text("✅ Category deleted.", reply_markup=keyboards.admin_back_kb())

@requires_role("coupons")
async def cbq_edit_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["edit_cat_id"] = int(query.data.split("adm_editname_")[1])
    await query.edit_message_text("✏️ Send the new category name:", parse_mode=ParseMode.MARKDOWN)
    return EDIT_NAME

async def edit_name_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    cid = ctx.user_data.get("edit_cat_id")
    await db.update_category(cid, name=update.message.text.strip())
    await update.message.reply_text("✅ Name updated.", reply_markup=keyboards.admin_back_kb())
    return ConversationHandler.END

@requires_role("coupons")
async def cbq_edit_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ctx.user_data["edit_cat_id"] = int(query.data.split("adm_editprice_")[1])
    await query.edit_message_text("💵 Send the new price:", parse_mode=ParseMode.MARKDOWN)
    return EDIT_PRICE

async def edit_price_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    price = safe_float(update.message.text)
    if price is None or price < 0:
        await update.message.reply_text("❌ Invalid price. Try again:")
        return EDIT_PRICE
    db = await Database.get_instance()
    await db.update_category(ctx.user_data.get("edit_cat_id"), price=round(price, 2))
    await update.message.reply_text("✅ Price updated.", reply_markup=keyboards.admin_back_kb())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# USERS / BAN / FRAUD
# ══════════════════════════════════════════════════════════════════════════
@requires_role("users_view")
async def cbq_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    total = await db.count_users()
    flagged = len(await db.list_flagged(1))
    await query.edit_message_text(f"👥 *Manage Users*\n\nTotal users: *{total}*\nFlagged for review: {flagged}",
                                  reply_markup=keyboards.admin_users_kb(), parse_mode=ParseMode.MARKDOWN)


@requires_role("coupons")  # piggyback fraud onto coupons role for simplicity
async def cbq_fraud(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    flagged = await db.list_flagged(10)
    if not flagged:
        await query.edit_message_text("✅ No flagged users.", reply_markup=keyboards.admin_back_kb())
        return
    lines = ["🚨 *Flagged Users (Fraud / Spam)*\n"]
    for u in flagged:
        lines.append(f"• `{u['user_id']}` (@{u.get('username','')}) — {u.get('flag_reason','')}")
    lines.append("\n_Use Check User Balance to view details or Ban._")
    await query.edit_message_text("\n".join(lines), reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)


@requires_role("users_ban")
async def cbq_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🚫 Send the *user ID* to ban:")
    return BAN_UID

async def ban_uid_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = safe_int(update.message.text)
    if not uid:
        return BAN_UID
    db = await Database.get_instance()
    await db.set_banned(uid, True)
    await update.message.reply_text(f"🚫 User `{uid}` banned.", reply_markup=keyboards.admin_back_kb())
    return ConversationHandler.END


@requires_role("users_ban")
async def cbq_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("✅ Send the *user ID* to unban:")
    return UNBAN_UID

async def unban_uid_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = safe_int(update.message.text)
    if not uid:
        return UNBAN_UID
    db = await Database.get_instance()
    await db.set_banned(uid, False)
    await db.unflag_user(uid)  # also clear fraud flag
    await update.message.reply_text(f"✅ User `{uid}` unbanned and unflagged.", reply_markup=keyboards.admin_back_kb())
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# WALLET
# ══════════════════════════════════════════════════════════════════════════
@requires_role("wallet_check")
async def cbq_wallet_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("💰 *Wallet Control*", reply_markup=keyboards.admin_wallet_kb(), parse_mode=ParseMode.MARKDOWN)


@requires_role("wallet_control")
async def cbq_wallet_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("➕ Send the *user ID* to credit:")
    return WALLET_ADD_UID

async def wallet_add_uid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["w_uid"] = safe_int(update.message.text)
    await update.message.reply_text("💵 Send the amount to ADD:")
    return WALLET_ADD_AMT

async def wallet_add_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    amt = safe_float(update.message.text)
    if not amt: return WALLET_ADD_AMT
    db = await Database.get_instance()
    uid = ctx.user_data["w_uid"]
    new_bal = await db.admin_adjust_wallet(uid, amt, note="Admin credit")
    await update.message.reply_text(f"✅ Added {format_currency(amt)} to `{uid}`.\nNew balance: {format_currency(new_bal)}",
                                    reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    try:
        await ctx.bot.send_message(uid, f"💰 Your wallet was credited {format_currency(amt)} by admin.")
    except TelegramError: pass
    return ConversationHandler.END


@requires_role("wallet_control")
async def cbq_wallet_deduct(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("➖ Send the *user ID* to deduct from:")
    return WALLET_DED_UID

async def wallet_ded_uid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["w_uid"] = safe_int(update.message.text)
    await update.message.reply_text("💵 Send the amount to DEDUCT:")
    return WALLET_DED_AMT

async def wallet_ded_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    amt = safe_float(update.message.text)
    if not amt: return WALLET_DED_AMT
    db = await Database.get_instance()
    uid = ctx.user_data["w_uid"]
    new_bal = await db.admin_adjust_wallet(uid, -amt, note="Admin deduction")
    await update.message.reply_text(f"✅ Deducted {format_currency(amt)} from `{uid}`.\nNew balance: {format_currency(new_bal)}",
                                    reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


@requires_role("wallet_check")
async def cbq_wallet_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🔍 Send the *user ID* to check:")
    return WALLET_CHECK_UID

async def wallet_check_uid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = safe_int(update.message.text)
    if not uid: return WALLET_CHECK_UID
    db = await Database.get_instance()
    u = await db.get_user(uid)
    if not u:
        await update.message.reply_text("User not found.", reply_markup=keyboards.admin_back_kb())
        return ConversationHandler.END
    txns = await db.get_transactions(uid, limit=5)
    hist = "\n".join(f"  {'+' if t['amount']>=0 else '−'}{format_currency(abs(t['amount']))} • {t['type']} • {fmt_dt(t['created_at'])}"
                     for t in txns) or "  (no transactions)"
    await update.message.reply_text(
        f"👤 *User* `{uid}`\nName: {u.get('full_name','N/A')} (@{u.get('username','')})\n"
        f"💰 Balance: *{format_currency(u.get('wallet_balance',0))}*\n"
        f"Banned: {'Yes' if u.get('is_banned') else 'No'}\n"
        f"Flagged: {'Yes' if u.get('flagged') else 'No'}\n\n*Recent:* \n{hist}",
        reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════
# TRANSACTIONS / ANALYTICS
# ══════════════════════════════════════════════════════════════════════════
@requires_role("transactions")
async def cbq_txns(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    txns = await db.get_all_transactions(limit=20)
    if not txns:
        await query.edit_message_text("📜 No transactions yet.", reply_markup=keyboards.admin_back_kb())
        return
    icons = {"recharge": "⬆️", "purchase": "🛒", "admin_adjust": "🛠️", "refund": "↩️", "referral": "🎁"}
    lines = ["📜 *Recent Transactions*\n"]
    for t in txns:
        lines.append(f"{icons.get(t['type'],'•')} `{t['user_id']}` "
                     f"{'+' if t['amount']>=0 else '−'}{format_currency(abs(t['amount']))} • {fmt_dt(t['created_at'])}")
    await query.edit_message_text("\n".join(lines), reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)


@requires_role("analytics")
async def cbq_analytics(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    from utils import is_scoped
    role = ctx.user_data.get("_admin_role", "user")
    uid = query.from_user.id

    def cur(v): return format_currency(v)

    if is_scoped(role):
        # Admin / Reseller: own scope only
        d = await db.admin_dashboard(uid)
        s = d["summary"]
        ov = await db.admin_revenue_overview(uid)
        top = await db.top_products(owner_id=uid, limit=5)
        toptxt = "\n".join(f"  • {t['_id']}: {t['sold']} sold ({cur(t['revenue'])})"
                           for t in top) or "  (no sales yet)"
        text = (
            f"📊 *Your Seller Dashboard*\n\n"
            f"🗓️ Today: {s['today']['orders']} orders • {cur(s['today']['revenue'])}\n"
            f"📅 Week: {s['week']['orders']} • {cur(s['week']['revenue'])}\n"
            f"🗓️ Month: {s['month']['orders']} • {cur(s['month']['revenue'])}\n"
            f"🧮 All-time: {s['all']['orders']} orders • {cur(s['all']['revenue'])}\n\n"
            f"💰 *Revenue*\n"
            f"  • Total earned: {cur(ov['total_revenue'])}\n"
            f"  • Available to withdraw: *{cur(ov['available'])}*\n"
            f"  • Pending withdrawal: {cur(ov['pending_net'])}\n"
            f"  • Withdrawn (paid): {cur(ov['withdrawn_net'])}\n\n"
            f"🏷️ Categories: *{d['categories']}*\n"
            f"📦 Stock left: *{d['stock']}*\n"
            f"👥 Customers: *{d['customers']}*\n\n"
            f"*Top Products:*\n{toptxt}")
    else:
        # Super Admin: full business analytics
        a = await db.analytics()
        s = await db.sales_summary()
        top = await db.top_products(limit=5)
        toptxt = "\n".join(f"  • {t['_id'] or 'N/A'}: {t['sold']} sold ({cur(t['revenue'])})"
                           for t in top) or "  (no sales yet)"
        aw = await db.admin_wise_sales()
        awtxt = "\n".join(
            f"  • {'Global' if not r['_id'] else 'Owner '+str(r['_id'])}: "
            f"{r['orders']} • {cur(r['revenue'])}" for r in aw[:8]) or "  (none)"
        text = (
            f"📊 *Business Analytics*\n\n"
            f"🗓️ Today: {s['today']['orders']} • {cur(s['today']['revenue'])}\n"
            f"📅 Week: {s['week']['orders']} • {cur(s['week']['revenue'])}\n"
            f"🗓️ Month: {s['month']['orders']} • {cur(s['month']['revenue'])}\n"
            f"🧮 All-time revenue: *{cur(a['revenue'])}*\n\n"
            f"👥 Users: *{a['total_users']}* (active 7d: {s['active_users']}, "
            f"new today: {s['new_users']})\n"
            f"⬆️ Recharged: {cur(a['recharged'])}\n"
            f"🎁 Ref Paid: {cur(a.get('referral_paid',0))}\n"
            f"💰 Wallet Liability: {cur(a['wallet_liability'])}\n"
            f"📦 Stock: *{a['available_stock']}*\n"
            f"🚩 Flagged: {a.get('flagged_users',0)}\n\n"
            f"*Top Products:*\n{toptxt}\n\n"
            f"*Admin-wise Sales:*\n{awtxt}")
    await query.edit_message_text(text, reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)


# ══════════════════════════════════════════════════════════════════════════
# BROADCAST
# ══════════════════════════════════════════════════════════════════════════
@requires_role("announce")
async def cbq_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("📢 *Broadcast*\n\nSelect audience segment:",
                                                  reply_markup=keyboards.admin_broadcast_kb(), parse_mode=ParseMode.MARKDOWN)

@requires_role("announce")
async def cbq_bc_segment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    seg = query.data.split("adm_bc_")[1]
    ctx.user_data["bc_segment"] = seg
    await query.edit_message_text(f"📢 *Broadcast ({seg})*\n\nSend the message (Markdown supported):", parse_mode=ParseMode.MARKDOWN)
    return BC_MSG

async def broadcast_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    seg = ctx.user_data.get("bc_segment", "all")
    db = await Database.get_instance()
    uids = await db.all_user_ids(seg)
    if not uids:
        await update.message.reply_text("No users found in that segment.", reply_markup=keyboards.admin_back_kb())
        return ConversationHandler.END
    msg = await update.message.reply_text(f"📤 Preparing to broadcast to {len(uids)} users…")
    # Run as a tracked application task so it is NOT garbage-collected mid-send
    # (this was the cause of the "broadcast stuck / not working" bug).
    ctx.application.create_task(_run_broadcast(ctx, uids, text, msg))
    return ConversationHandler.END


async def _run_broadcast(ctx, uids, text, msg):
    try:
        res = await broadcast(ctx.bot, uids, text, progress_msg=msg)
        await msg.edit_text(
            f"✅ *Broadcast complete.*\n\nSent: {res['sent']} • Failed: {res['failed']} "
            f"• Total: {res['total']}",
            reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        logger.exception("Broadcast task failed")
        try:
            await msg.edit_text("⚠️ Broadcast finished with errors. Check logs.",
                                reply_markup=keyboards.admin_back_kb())
        except TelegramError:
            pass


# ══════════════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════════════
@requires_role("settings")
async def cbq_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    maint = await db.get_setting("maintenance", "false") == "true"
    await query.edit_message_text("⚙️ *Settings*", reply_markup=keyboards.admin_settings_kb(maint), parse_mode=ParseMode.MARKDOWN)

@requires_role("settings")
async def cbq_toggle_maint(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db = await Database.get_instance()
    cur = await db.get_setting("maintenance", "false")
    new = "false" if cur == "true" else "true"
    await db.set_setting("maintenance", new)
    await query.answer(f"Maintenance {'ON' if new=='true' else 'OFF'}")
    await cbq_settings(update, ctx)


# ── Force-channel settings (super admin) ──
@requires_role("settings")
async def cbq_channel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    cfg = await db.get_force_channel()
    ch = cfg["channel"] or "(disabled)"
    await query.edit_message_text(
        f"📢 *Force-Join Channel*\n\nCurrent: `{ch}`\nJoin URL: `{cfg['url'] or 'auto'}`\n\n"
        "Users must join this channel before using the bot. The bot must be an "
        "*admin* of the channel for membership checks to work.",
        reply_markup=keyboards.admin_channel_kb(bool(cfg["channel"])),
        parse_mode=ParseMode.MARKDOWN)

@requires_role("settings")
async def cbq_clearchannel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    await db.set_setting("force_channel", "")
    await db.set_setting("force_channel_url", "")
    await update.callback_query.answer("Force-join disabled.")
    await cbq_channel(update, ctx)


# ── Reseller settings (super admin) ──
@requires_role("settings")
async def cbq_reseller_cfg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    cfg = await db.get_reseller_config()
    pending = len(await db.pending_reseller_requests())
    await query.edit_message_text(
        f"🏪 *Reseller Settings*\n\n"
        f"Program: {'🟢 ENABLED' if cfg['enabled'] else '🔴 DISABLED'}\n"
        f"Fee: {format_currency(cfg['fee'])}\n"
        f"Auto-approve: {'ON' if cfg['auto_approve'] else 'OFF'}\n"
        f"Pending requests: {pending}\n\n"
        "_When disabled, the reseller option is hidden from all users._",
        reply_markup=keyboards.admin_reseller_cfg_kb(cfg["auto_approve"], cfg["enabled"]),
        parse_mode=ParseMode.MARKDOWN)

@requires_role("settings")
async def cbq_toggle_reseller_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    cfg = await db.get_reseller_config()
    await db.set_setting("reseller_auto_approve", "false" if cfg["auto_approve"] else "true")
    await update.callback_query.answer("Toggled.")
    await cbq_reseller_cfg(update, ctx)

@requires_role("settings")
async def cbq_toggle_reseller_enabled(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    cfg = await db.get_reseller_config()
    new_on = not cfg["enabled"]
    await db.set_setting("reseller_enabled", "true" if new_on else "false")
    await update.callback_query.answer(
        "Reseller program ENABLED for all users." if new_on
        else "Reseller program DISABLED and hidden from users.")
    await cbq_reseller_cfg(update, ctx)

@requires_role("settings")
async def cbq_reseller_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    reqs = await db.pending_reseller_requests()
    if not reqs:
        await query.edit_message_text("✅ No pending reseller requests.",
                                      reply_markup=keyboards.admin_back_kb())
        return
    lines = ["🏪 *Pending Reseller Requests*\n"]
    for r in reqs:
        lines.append(f"• `{r['user_id']}` — fee {format_currency(r['fee'])} • {fmt_dt(r['created_at'])}")
    await query.edit_message_text("\n".join(lines),
                                  reply_markup=keyboards.admin_reseller_pending_kb(reqs),
                                  parse_mode=ParseMode.MARKDOWN)

@requires_role("settings")
async def cbq_res_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = int(query.data.split("adm_resapprove_")[1])
    db = await Database.get_instance()
    await db.activate_reseller(uid)
    await db.set_reseller_request_status(uid, "approved")
    await query.answer("Approved ✅")
    try:
        import messages
        await ctx.bot.send_message(uid, messages.reseller_activated(), parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass
    await cbq_reseller_pending(update, ctx)

@requires_role("settings")
async def cbq_res_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = int(query.data.split("adm_resreject_")[1])
    db = await Database.get_instance()
    # refund the fee that was paid from wallet
    cfg = await db.get_reseller_config()
    await db.credit_wallet(uid, cfg["fee"], ttype="refund", note="Reseller request rejected refund")
    await db.set_reseller_request_status(uid, "rejected")
    await query.answer("Rejected & refunded.")
    try:
        await ctx.bot.send_message(uid,
            "❌ Your reseller request was declined. The fee has been refunded to your wallet.")
    except TelegramError:
        pass
    await cbq_reseller_pending(update, ctx)


# ═══════════════════════════════════════════════════════════════════════════
# WITHDRAWAL COMMISSION (super admin)
# ═══════════════════════════════════════════════════════════════════════════
@requires_role("settings")
async def cbq_set_wd_commission(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    pct = await db.get_withdrawal_commission_pct()
    await query.edit_message_text(
        f"📉 *Withdrawal Commission*\n\nCurrent rate: *{pct:g}%*\n\n"
        "This is the percentage the Super Admin keeps on every admin revenue "
        "withdrawal. Send the new percentage (e.g. `10` for 10%):",
        parse_mode=ParseMode.MARKDOWN)
    return WD_COMMISSION

async def wd_commission_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    val = safe_float(update.message.text)
    if val is None or val < 0 or val >= 100:
        await update.message.reply_text(
            "❌ Please send a valid percentage between 0 and 99 (e.g. 10).")
        return WD_COMMISSION
    db = await Database.get_instance()
    await db.set_setting("withdrawal_commission_pct", round(val, 2))
    await update.message.reply_text(
        f"✅ Withdrawal commission set to *{val:g}%*.",
        reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════════════════════
# ADMIN REVENUE WITHDRAWALS (admin / reseller)
# ═══════════════════════════════════════════════════════════════════════════
def _wd_status_label(status: str) -> str:
    return {"pending": "⏳ Pending", "approved": "✅ Approved",
            "rejected": "❌ Rejected"}.get(status, status)


@requires_role("analytics")
async def cbq_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Entry point for an admin/reseller to request a revenue withdrawal."""
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    uid = query.from_user.id

    if await db.has_withdrawal_today(uid):
        await query.edit_message_text(
            messages.withdrawal_already_today(),
            reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    ov = await db.admin_revenue_overview(uid)
    if ov["available"] <= 0:
        await query.edit_message_text(
            messages.withdrawal_none(),
            reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    ctx.user_data["wd_available"] = ov["available"]
    await query.edit_message_text(
        f"💸 *Withdraw Revenue*\n\nAvailable revenue: *{format_currency(ov['available'])}*\n\n"
        "Please send your *UPI ID* (e.g. `name@bank`) to receive the payout:",
        parse_mode=ParseMode.MARKDOWN)
    return WD_UPI

async def withdraw_upi_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    upi = (update.message.text or "").strip()
    # Light UPI VPA validation: something@handle, no spaces.
    if "@" not in upi or " " in upi or len(upi) < 3 or upi.startswith("@") or upi.endswith("@"):
        await update.message.reply_text(
            "❌ That doesn't look like a valid UPI ID. Please send it as "
            "`name@bank` (e.g. `john@okhdfcbank`).", parse_mode=ParseMode.MARKDOWN)
        return WD_UPI

    db = await Database.get_instance()
    uid = update.effective_user.id
    # Re-check availability at this point (guards against staleness).
    ov = await db.admin_revenue_overview(uid)
    revenue = ov["available"]
    if revenue <= 0:
        await update.message.reply_text(
            messages.withdrawal_none(), reply_markup=keyboards.admin_back_kb(),
            parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    pct = await db.get_withdrawal_commission_pct()
    commission = round(revenue * pct / 100.0, 2)
    final = round(revenue - commission, 2)

    ctx.user_data["wd_pending"] = {
        "revenue": revenue, "commission_pct": pct,
        "commission": commission, "final": final, "upi": upi,
    }
    await update.message.reply_text(
        messages.withdrawal_breakdown(revenue, pct, commission, final, upi),
        reply_markup=keyboards.withdraw_confirm_kb(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@requires_role("analytics")
async def cbq_withdraw_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db = await Database.get_instance()
    uid = query.from_user.id
    data = ctx.user_data.pop("wd_pending", None)
    if not data:
        await query.answer("Session expired. Please start again.", show_alert=True)
        await query.edit_message_text("Session expired.",
                                      reply_markup=keyboards.admin_back_kb())
        return

    # Final guards: daily limit + still-available revenue.
    if await db.has_withdrawal_today(uid):
        await query.answer()
        await query.edit_message_text(
            messages.withdrawal_already_today(),
            reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
        return
    ov = await db.admin_revenue_overview(uid)
    if ov["available"] + 0.001 < data["revenue"]:
        # Available dropped since the breakdown was shown; recompute.
        await query.answer("Your available revenue changed. Please retry.",
                           show_alert=True)
        await query.edit_message_text(
            "⚠️ Your available revenue changed. Please open *Withdraw Revenue* again.",
            reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
        return

    wid = await db.create_withdrawal(
        uid, revenue=data["revenue"], commission_pct=data["commission_pct"],
        commission=data["commission"], amount=data["final"], upi_id=data["upi"])
    await query.answer("Request submitted ✅")
    await query.edit_message_text(
        messages.withdrawal_requested(wid, data["final"]),
        reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)

    # Instant notification to the Super Admin(s) with full details.
    uname = query.from_user.username
    who = f"@{uname}" if uname else str(uid)
    await notify_super_admins(
        ctx.bot,
        "💸 *New Withdrawal Request*\n\n"
        f"👤 Admin: {who} (`{uid}`)\n"
        f"🆔 Ref: `{wid}`\n"
        f"🧾 Total Revenue: {format_currency(data['revenue'])}\n"
        f"🏦 Commission ({data['commission_pct']:g}%): "
        f"{format_currency(data['commission'])}\n"
        f"✅ Payable: *{format_currency(data['final'])}*\n"
        f"💳 UPI ID: `{data['upi']}`\n\n"
        "Review under *Admin → 💸 Withdrawal Requests*.")


@requires_role("analytics")
async def cbq_wd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    rows = await db.list_user_withdrawals(query.from_user.id, limit=10)
    if not rows:
        await query.edit_message_text(
            "🧾 *Withdrawal History*\n\nNo withdrawal requests yet.",
            reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
        return
    lines = ["🧾 *Withdrawal History*\n"]
    for w in rows:
        ref = w.get("payment_reference") or ""
        line = (
            f"• `{w['withdrawal_id']}` — {_wd_status_label(w['status'])}\n"
            f"  🗓️ {fmt_dt(w['created_at'])}\n"
            f"  🧾 Revenue {format_currency(w.get('revenue', 0))} • "
            f"Payable {format_currency(w.get('amount', 0))}\n"
            f"  💳 {w.get('upi_id', 'N/A')}")
        if ref:
            line += f"\n  🔖 Ref: `{ref}`"
        lines.append(line)
    await query.edit_message_text(
        "\n".join(lines), reply_markup=keyboards.admin_back_kb(),
        parse_mode=ParseMode.MARKDOWN)


# ── Super admin: review withdrawal requests ──
@requires_role("settings")
async def cbq_wd_requests(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    reqs = await db.list_withdrawals("pending")
    if not reqs:
        await query.edit_message_text(
            "✅ No pending withdrawal requests.",
            reply_markup=keyboards.admin_back_kb())
        return
    lines = ["💸 *Pending Withdrawal Requests*\n"]
    for w in reqs:
        lines.append(
            f"• `{w['withdrawal_id']}` — user `{w['user_id']}`\n"
            f"  🧾 Revenue {format_currency(w.get('revenue', 0))} • "
            f"Commission ({w.get('commission_pct', 0):g}%) "
            f"{format_currency(w.get('commission', 0))}\n"
            f"  ✅ Payable *{format_currency(w.get('amount', 0))}* • "
            f"💳 `{w.get('upi_id', 'N/A')}`\n"
            f"  🗓️ {fmt_dt(w['created_at'])}")
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=keyboards.admin_withdrawal_requests_kb(reqs),
        parse_mode=ParseMode.MARKDOWN)

@requires_role("settings")
async def cbq_wd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    wid = query.data.split("adm_wdapprove_")[1]
    db = await Database.get_instance()
    w = await db.resolve_withdrawal(wid, approve=True)
    if not w:
        await query.answer("Already resolved or not found.", show_alert=True)
        await cbq_wd_requests(update, ctx)
        return
    await query.answer("Approved ✅")
    try:
        await ctx.bot.send_message(
            w["user_id"],
            messages.withdrawal_approved_user(wid, w.get("amount", 0)),
            parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass
    await cbq_wd_requests(update, ctx)

@requires_role("settings")
async def cbq_wd_reject(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    wid = query.data.split("adm_wdreject_")[1]
    db = await Database.get_instance()
    w = await db.resolve_withdrawal(wid, approve=False)
    if not w:
        await query.answer("Already resolved or not found.", show_alert=True)
        await cbq_wd_requests(update, ctx)
        return
    await query.answer("Rejected & revenue released.")
    try:
        await ctx.bot.send_message(
            w["user_id"], messages.withdrawal_rejected_user(wid),
            parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass
    await cbq_wd_requests(update, ctx)


def _setup_setting_conv(app, entry_pattern, prompt, state, db_key):
    async def entry(update, ctx):
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(prompt, parse_mode=ParseMode.MARKDOWN)
        return state
    async def finish(update, ctx):
        db = await Database.get_instance()
        await db.set_setting(db_key, update.message.text.strip())
        await update.message.reply_text("✅ Updated.", reply_markup=keyboards.admin_back_kb())
        return ConversationHandler.END
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(requires_role("settings")(entry), pattern=entry_pattern)],
        states={state: [MessageHandler(filters.TEXT & ~filters.COMMAND, requires_role("settings")(finish))]},
        fallbacks=[CommandHandler("admin", cmd_admin), CallbackQueryHandler(cbq_admin_menu, pattern="^adm_menu$")],
    ))


# ══════════════════════════════════════════════════════════════════════════
# REFERRAL & DISCOUNTS
# ══════════════════════════════════════════════════════════════════════════
@requires_role("referral")
async def cbq_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    cfg = await db.get_referral_config()
    await query.edit_message_text(
        f"🎁 *Referral Config*\n\nSignup Bonus: {format_currency(cfg['signup_bonus'])}\n"
        f"Commission: {cfg['commission_pct']}%\nWelcome Bonus: {format_currency(cfg['welcome_bonus'])}",
        reply_markup=keyboards.admin_referral_kb(cfg["enabled"]), parse_mode=ParseMode.MARKDOWN)

@requires_role("referral")
async def cbq_ref_toggle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    cfg = await db.get_referral_config()
    await db.set_setting("ref_enabled", "false" if cfg["enabled"] else "true")
    await cbq_referral(update, ctx)

@requires_role("settings")
async def cbq_discounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    tiers = await db.get_discount_tiers()
    from utils import tiers_summary
    text = f"🎉 *Bulk Discounts*\n\n{tiers_summary(tiers)}\n\n_To update, you can edit them via DB directly or clear them here._"
    await query.edit_message_text(text, reply_markup=keyboards.admin_discounts_kb(), parse_mode=ParseMode.MARKDOWN)

@requires_role("settings")
async def cbq_cleardiscounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    db = await Database.get_instance()
    await db.set_setting("discount_tiers", [])
    await update.callback_query.answer("Discounts cleared.")
    await cbq_discounts(update, ctx)


# ══════════════════════════════════════════════════════════════════════════
# SUPER ADMIN (Staff & Backup)
# ══════════════════════════════════════════════════════════════════════════
@requires_role("staff")
async def cbq_staff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db = await Database.get_instance()
    admins = await db.list_admins()
    await query.edit_message_text("🧑‍✈️ *Manage Staff*\n\nDatabase-assigned roles:",
                                  reply_markup=keyboards.admin_staff_kb(admins), parse_mode=ParseMode.MARKDOWN)

@requires_role("staff")
async def cbq_staffadd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("🧑‍✈️ Send the *user ID* to add/update:")
    return STAFF_ADD_UID

async def staff_add_uid(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = safe_int(update.message.text)
    if not uid: return STAFF_ADD_UID
    ctx.user_data["s_uid"] = uid
    await update.message.reply_text("Select role:", reply_markup=keyboards.admin_staff_role_kb(uid))
    return ConversationHandler.END

@requires_role("staff")
async def cbq_setrole(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    # data = adm_setrole_<uid>_<role> ; role may itself contain underscores
    # (e.g. super_admin), so parse positionally instead of a fixed unpack.
    parts = query.data.split("_")
    uid_s = parts[2]
    role = "_".join(parts[3:])
    db = await Database.get_instance()
    await db.set_admin_role(int(uid_s), role, added_by=query.from_user.id)
    if role == "reseller":
        await db.activate_reseller(int(uid_s))
    from utils import role_label
    await query.edit_message_text(
        f"✅ User `{uid_s}` is now {role_label(role)}.",
        reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    try:
        await ctx.bot.send_message(int(uid_s),
            f"🎖️ You have been granted *{role_label(role)}* access. Open /admin.",
            parse_mode=ParseMode.MARKDOWN)
    except TelegramError:
        pass

@requires_role("staff")
async def cbq_staffdel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = int(query.data.split("adm_staffdel_")[1])
    db = await Database.get_instance()
    await db.remove_admin(uid)
    await query.edit_message_text(f"🗑️ Removed {uid} from DB staff list.", reply_markup=keyboards.admin_back_kb())


@requires_role("backup")
async def cbq_backup_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("💾 *Backup & Restore*\n\nDownload a full JSON dump or restore from one.",
                                  reply_markup=keyboards.admin_backup_kb(), parse_mode=ParseMode.MARKDOWN)

@requires_role("backup")
async def cbq_dobackup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.edit_message_text("⏳ Generating backup...")
    db = await Database.get_instance()
    data = await db.export_all()
    j = json.dumps(data, separators=(',', ':'))
    buf = BytesIO(j.encode("utf-8"))
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    buf.name = f"backup_{config.BOT_NAME}_{stamp}.json"
    await ctx.bot.send_document(query.message.chat_id, buf, caption="✅ Database Export")
    await query.message.delete()

@requires_role("backup")
async def cbq_dorestore(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("⬆️ *Restore*\n\nSend a previously exported `.json` file now.", parse_mode=ParseMode.MARKDOWN)
    return RESTORE_FILE

async def restore_file_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.document or not update.message.document.file_name.endswith(".json"):
        await update.message.reply_text("❌ Please send a valid .json backup file.")
        return RESTORE_FILE
    f = await update.message.document.get_file()
    jdata = await f.download_as_bytearray()
    try:
        data = json.loads(jdata)
        if "_meta" not in data: raise ValueError("Not a valid backup file")
        ctx.user_data["restore_data"] = data
        await update.message.reply_text("⚠️ *DANGER*\n\nThis will WIPE all current data and replace it with the backup. Continue?",
                                        reply_markup=keyboards.admin_restore_confirm_kb(), parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    except Exception:
        await update.message.reply_text("❌ Failed to parse backup file.", reply_markup=keyboards.admin_back_kb())
        return ConversationHandler.END

@requires_role("backup")
async def cbq_restoreyes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = ctx.user_data.pop("restore_data", None)
    if not data:
        await query.edit_message_text("Session expired.", reply_markup=keyboards.admin_back_kb())
        return
    await query.edit_message_text("⏳ Restoring...")
    db = await Database.get_instance()
    try:
        counts = await db.import_all(data)
        summary = "\n".join(f"{k}: {v}" for k,v in counts.items())
        await query.edit_message_text(f"✅ *Restore Complete*\n\n{summary}", reply_markup=keyboards.admin_back_kb(), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        await query.edit_message_text(f"❌ Restore failed: {e}", reply_markup=keyboards.admin_back_kb())


def _conv(app, pattern, entry, state, func, permission=""):
    d = requires_role(permission)(entry) if permission else entry
    f = requires_role(permission)(func) if permission else func
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(d, pattern=pattern)],
        states={state: [MessageHandler(filters.TEXT & ~filters.COMMAND, f)]},
        fallbacks=[CommandHandler("admin", cmd_admin), CallbackQueryHandler(cbq_admin_menu, pattern="^adm_menu$")],
        per_chat=True, per_user=True,
    ))


def register_admin_handlers(app):
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(cbq_admin_menu, pattern="^adm_menu$"))
    app.add_handler(CallbackQueryHandler(_close, pattern="^adm_close$"))

    # Basic UI
    app.add_handler(CallbackQueryHandler(cbq_coupons, pattern="^adm_coupons$"))
    app.add_handler(CallbackQueryHandler(cbq_category, pattern=r"^adm_cat_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_del_cat, pattern=r"^adm_delcat_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_del_cat_yes, pattern=r"^adm_delcatyes_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_togglecat, pattern=r"^adm_togglecat_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_wallet_menu, pattern="^adm_wallet$"))
    app.add_handler(CallbackQueryHandler(cbq_users, pattern="^adm_users$"))
    app.add_handler(CallbackQueryHandler(cbq_fraud, pattern="^adm_fraud$"))
    app.add_handler(CallbackQueryHandler(cbq_txns, pattern="^adm_txns$"))
    app.add_handler(CallbackQueryHandler(cbq_analytics, pattern="^adm_analytics$"))
    app.add_handler(CallbackQueryHandler(cbq_settings, pattern="^adm_settings$"))
    app.add_handler(CallbackQueryHandler(cbq_toggle_maint, pattern="^adm_togglemaint$"))

    # Convs
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(requires_role("coupons")(cbq_add_cat), pattern="^adm_addcat$")],
        states={ADD_CAT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cat_name)],
                ADD_CAT_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cat_price)]},
        fallbacks=[CommandHandler("admin", cmd_admin)], per_chat=True, per_user=True))
    _conv(app, r"^adm_editname_\d+$", cbq_edit_name, EDIT_NAME, edit_name_input, "coupons")
    _conv(app, r"^adm_editprice_\d+$", cbq_edit_price, EDIT_PRICE, edit_price_input, "coupons")
    _conv(app, r"^adm_addstock_\d+$", cbq_add_stock, ADD_STOCK, add_stock_input, "stock")
    # entry callbacks already carry @requires_role, so register without re-wrap
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cbq_edit_terms, pattern=r"^adm_editterms_\d+$")],
        states={EDIT_TERMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_terms_input)]},
        fallbacks=[CommandHandler("admin", cmd_admin)], per_chat=True, per_user=True))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cbq_edit_expiry, pattern=r"^adm_editexpiry_\d+$")],
        states={EDIT_EXPIRY: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_expiry_input)]},
        fallbacks=[CommandHandler("admin", cmd_admin)], per_chat=True, per_user=True))
    app.add_handler(CallbackQueryHandler(cbq_export_cat, pattern=r"^adm_exportcat_\d+$"))

    # Force-channel & reseller settings (super admin)
    app.add_handler(CallbackQueryHandler(cbq_channel, pattern="^adm_channel$"))
    app.add_handler(CallbackQueryHandler(cbq_clearchannel, pattern="^adm_clearchannel$"))
    _setup_setting_conv(app, "^adm_setchannel$", "📢 Send the channel `@username` or `-100…` id:", RESTORE_FILE, "force_channel")
    _setup_setting_conv(app, "^adm_setchannelurl$", "🔗 Send the public join URL (https://t.me/…):", REF_WELCOME, "force_channel_url")
    app.add_handler(CallbackQueryHandler(cbq_reseller_cfg, pattern="^adm_resellercfg$"))
    app.add_handler(CallbackQueryHandler(cbq_toggle_reseller_auto, pattern="^adm_toggleresellerauto$"))
    app.add_handler(CallbackQueryHandler(cbq_toggle_reseller_enabled, pattern="^adm_toggleresellerenabled$"))
    app.add_handler(CallbackQueryHandler(cbq_reseller_pending, pattern="^adm_resellerpending$"))
    app.add_handler(CallbackQueryHandler(cbq_res_approve, pattern=r"^adm_resapprove_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_res_reject, pattern=r"^adm_resreject_\d+$"))
    _setup_setting_conv(app, "^adm_setresellerfee$", "💵 Send the reseller activation fee:", BC_MSG, "reseller_fee")

    # ── Admin revenue withdrawals ──
    # Withdrawal request (admin/reseller): asks for UPI, then shows breakdown.
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cbq_withdraw, pattern="^adm_withdraw$")],
        states={WD_UPI: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_upi_input)]},
        fallbacks=[CommandHandler("admin", cmd_admin),
                   CallbackQueryHandler(cbq_admin_menu, pattern="^adm_menu$")],
        per_chat=True, per_user=True))
    app.add_handler(CallbackQueryHandler(cbq_withdraw_confirm, pattern="^adm_withdraw_confirm$"))
    app.add_handler(CallbackQueryHandler(cbq_wd_history, pattern="^adm_wdhistory$"))
    # Super admin review + approve/reject.
    app.add_handler(CallbackQueryHandler(cbq_wd_requests, pattern="^adm_wdrequests$"))
    app.add_handler(CallbackQueryHandler(cbq_wd_approve, pattern=r"^adm_wdapprove_WD-\w+$"))
    app.add_handler(CallbackQueryHandler(cbq_wd_reject, pattern=r"^adm_wdreject_WD-\w+$"))
    # Withdrawal commission setter (super admin).
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(cbq_set_wd_commission, pattern="^adm_setwdcommission$")],
        states={WD_COMMISSION: [MessageHandler(filters.TEXT & ~filters.COMMAND, wd_commission_input)]},
        fallbacks=[CommandHandler("admin", cmd_admin),
                   CallbackQueryHandler(cbq_admin_menu, pattern="^adm_menu$")],
        per_chat=True, per_user=True))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(requires_role("wallet_control")(cbq_wallet_add), pattern="^adm_walletadd$")],
        states={WALLET_ADD_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_add_uid)],
                WALLET_ADD_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_add_amt)]},
        fallbacks=[CommandHandler("admin", cmd_admin)], per_chat=True, per_user=True))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(requires_role("wallet_control")(cbq_wallet_deduct), pattern="^adm_walletdeduct$")],
        states={WALLET_DED_UID: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_ded_uid)],
                WALLET_DED_AMT: [MessageHandler(filters.TEXT & ~filters.COMMAND, wallet_ded_amt)]},
        fallbacks=[CommandHandler("admin", cmd_admin)], per_chat=True, per_user=True))
    _conv(app, "^adm_walletcheck$", cbq_wallet_check, WALLET_CHECK_UID, wallet_check_uid, "wallet_check")
    _conv(app, "^adm_ban$", cbq_ban, BAN_UID, ban_uid_input, "users_ban")
    _conv(app, "^adm_unban$", cbq_unban, UNBAN_UID, unban_uid_input, "users_ban")
    _conv(app, r"^adm_bc_(all|notify|buyers|with_balance|recharged)$", cbq_bc_segment, BC_MSG, broadcast_input, "announce")
    app.add_handler(CallbackQueryHandler(requires_role("announce")(cbq_broadcast), pattern="^adm_broadcast$"))

    # Config Setters
    _setup_setting_conv(app, "^adm_setupi$", "💳 Send the new UPI ID:", SET_UPI, "upi_id")
    _setup_setting_conv(app, "^adm_setpayee$", "👤 Send the new Payee Name:", SET_PAYEE, "payee_name")
    _setup_setting_conv(app, "^adm_setlowstock$", "📦 Send the new low-stock threshold (e.g. 5):", SET_LOW_STOCK, "low_stock_threshold")

    # Referral & Discounts
    app.add_handler(CallbackQueryHandler(cbq_referral, pattern="^adm_referral$"))
    app.add_handler(CallbackQueryHandler(cbq_ref_toggle, pattern="^adm_ref_toggle$"))
    _setup_setting_conv(app, "^adm_ref_signup$", "💵 Send Signup Bonus amount:", REF_SIGNUP, "ref_signup_bonus")
    _setup_setting_conv(app, "^adm_ref_commission$", "📈 Send Commission %:", REF_COMMISSION, "ref_commission_pct")
    _setup_setting_conv(app, "^adm_ref_welcome$", "🎉 Send Welcome Bonus amount:", REF_WELCOME, "ref_welcome_bonus")
    app.add_handler(CallbackQueryHandler(cbq_discounts, pattern="^adm_discounts$"))
    app.add_handler(CallbackQueryHandler(cbq_cleardiscounts, pattern="^adm_cleardiscounts$"))

    # Super Admin Staff & Backup
    app.add_handler(CallbackQueryHandler(cbq_staff, pattern="^adm_staff$"))
    app.add_handler(CallbackQueryHandler(cbq_staffdel, pattern=r"^adm_staffdel_\d+$"))
    app.add_handler(CallbackQueryHandler(cbq_setrole, pattern=r"^adm_setrole_\d+_\w+$"))
    _conv(app, "^adm_staffadd$", cbq_staffadd, STAFF_ADD_UID, staff_add_uid, "staff")

    app.add_handler(CallbackQueryHandler(cbq_backup_menu, pattern="^adm_backup$"))
    app.add_handler(CallbackQueryHandler(cbq_dobackup, pattern="^adm_dobackup$"))
    app.add_handler(CallbackQueryHandler(cbq_restoreyes, pattern="^adm_restoreyes$"))
    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(requires_role("backup")(cbq_dorestore), pattern="^adm_dorestore$")],
        states={RESTORE_FILE: [MessageHandler(filters.Document.ALL, restore_file_input)]},
        fallbacks=[CommandHandler("admin", cmd_admin), CallbackQueryHandler(cbq_backup_menu, pattern="^adm_backup$")],
        per_chat=True, per_user=True))
