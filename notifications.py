"""
notifications.py - Outbound notifications, broadcasts & background jobs.

  • notify_user()          - safe single-user push (respects notify pref)
  • broadcast()            - send to a segment with a live progress bar
  • new_coupon_alert()     - fan-out when admin adds/refills a category
  • low_stock_job()        - JobQueue callback: alert staff on low stock
  • notify_admins()        - push an internal alert to all staff / ADMIN_CHAT_ID
"""

import asyncio
import logging

from telegram.constants import ParseMode
from telegram.error import Forbidden, TelegramError

import config
import messages
from database import Database
from utils import animations, format_currency

logger = logging.getLogger(__name__)


async def notify_user(bot, user_id: int, text: str, *, respect_pref: bool = True,
                      db: Database | None = None) -> bool:
    """Send a message to one user. Never raises. Returns True on success."""
    try:
        if respect_pref:
            db = db or await Database.get_instance()
            u = await db.get_user(user_id)
            if u and u.get("notify") is False:
                return False
        await bot.send_message(user_id, text, parse_mode=ParseMode.MARKDOWN)
        return True
    except (Forbidden, TelegramError):
        return False
    except Exception:
        logger.exception("notify_user failed for %s", user_id)
        return False


async def notify_admins(bot, text: str):
    """Alert all staff (DB admins + env super admins + ADMIN_CHAT_ID)."""
    targets = set(config.SUPER_ADMIN_IDS) | set(config.SUPPORT_IDS)
    try:
        db = await Database.get_instance()
        for a in await db.list_admins():
            targets.add(a["user_id"])
    except Exception:
        pass
    if config.ADMIN_CHAT_ID:
        try:
            targets.add(int(config.ADMIN_CHAT_ID))
        except (ValueError, TypeError):
            pass
    for uid in targets:
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


async def notify_super_admins(bot, text: str):
    """Alert Super Admins only (env SUPER_ADMIN_IDS + DB super_admin roles)."""
    targets = set(config.SUPER_ADMIN_IDS)
    try:
        db = await Database.get_instance()
        for a in await db.list_admins():
            if a.get("role") == "super_admin":
                targets.add(a["user_id"])
    except Exception:
        pass
    for uid in targets:
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass


async def broadcast(bot, user_ids: list[int], text: str, progress_msg=None) -> dict:
    """
    Send `text` to every id. Updates `progress_msg` with a live progress bar.
    Returns dict(sent, failed, total).
    """
    sent = failed = 0
    total = len(user_ids)
    for i, uid in enumerate(user_ids, 1):
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception:
            failed += 1
        # Telegram-friendly pacing (~25 msgs/sec max recommended).
        if i % 25 == 0:
            await asyncio.sleep(1)
            if progress_msg is not None:
                bar = animations.progress_bar(i, total)
                try:
                    await progress_msg.edit_text(
                        f"📤 *Broadcasting…*\n\n{bar}\n\n✅ {sent}  •  ⚠️ {failed}",
                        parse_mode=ParseMode.MARKDOWN)
                except Exception:
                    pass
    return {"sent": sent, "failed": failed, "total": total}


async def fanout_new_coupon(bot, name: str, price: float):
    """Notify opted-in users that a new/refilled coupon is available."""
    db = await Database.get_instance()
    ids = await db.notify_user_ids()
    text = messages.new_coupon_alert(name, price)
    sent = 0
    for i, uid in enumerate(ids, 1):
        try:
            await bot.send_message(uid, text, parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception:
            pass
        if i % 25 == 0:
            await asyncio.sleep(1)
    logger.info("New-coupon alert sent to %s/%s users", sent, len(ids))
    return sent


async def daily_report_job(ctx):
    """JobQueue callback: send an end-of-day sales report to super admins."""
    try:
        db = await Database.get_instance()
        s = await db.sales_summary()
        top = await db.top_products(limit=5)
        aw = await db.admin_wise_sales()
        a = await db.analytics()

        def cur(v):
            return format_currency(v)

        toptxt = "\n".join(f"  • {t['_id'] or 'N/A'}: {t['sold']} sold ({cur(t['revenue'])})"
                           for t in top) or "  (no sales)"
        awtxt = "\n".join(
            f"  • {'Global' if not r['_id'] else 'Owner '+str(r['_id'])}: "
            f"{r['orders']} • {cur(r['revenue'])}" for r in aw[:8]) or "  (none)"
        low = await db.low_stock_categories(int(await db.get_setting(
            "low_stock_threshold", config.LOW_STOCK_THRESHOLD_DEFAULT)))
        lowtxt = "\n".join(f"  • {c['name']}: {c['_stock']} left" for c in low) or "  (all healthy)"

        text = (
            "🧾 *End-of-Day Report*\n\n"
            f"🗓️ Today: {s['today']['orders']} orders • {cur(s['today']['revenue'])}\n"
            f"📅 Week: {s['week']['orders']} • {cur(s['week']['revenue'])}\n"
            f"🗓️ Month: {s['month']['orders']} • {cur(s['month']['revenue'])}\n\n"
            f"👥 New users today: {s['new_users']} • Active 7d: {s['active_users']}\n"
            f"⬆️ Recharged (all-time): {cur(a['recharged'])}\n"
            f"💰 Wallet liability: {cur(a['wallet_liability'])}\n\n"
            f"*Top Products:*\n{toptxt}\n\n"
            f"*Admin-wise Sales:*\n{awtxt}\n\n"
            f"*Low Stock:*\n{lowtxt}")
        # Report goes to super admins only.
        for uid in set(config.SUPER_ADMIN_IDS):
            try:
                await ctx.bot.send_message(uid, text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
        try:
            db2 = await Database.get_instance()
            for adm in await db2.list_admins():
                if adm.get("role") == "super_admin" and adm["user_id"] not in config.SUPER_ADMIN_IDS:
                    await ctx.bot.send_message(adm["user_id"], text, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            pass
    except Exception:
        logger.exception("daily_report_job failed")


async def low_stock_job(ctx):
    """JobQueue callback: alert staff when categories drop to/under threshold."""
    try:
        db = await Database.get_instance()
        threshold = int(await db.get_setting(
            "low_stock_threshold", config.LOW_STOCK_THRESHOLD_DEFAULT))
        low = await db.low_stock_categories(threshold)
        if not low:
            return
        lines = ["🚨 *Low Stock Alert*\n"]
        for c in low:
            lines.append(f"• *{c['name']}* — only *{c['_stock']}* left "
                         f"({format_currency(c['price'])})")
            await db.mark_low_stock_alerted(c["id"])
        lines.append("\nTop up stock from *Manage Coupons* → category → *Add Stock*.")
        await notify_admins(ctx.bot, "\n".join(lines))
    except Exception:
        logger.exception("low_stock_job failed")
