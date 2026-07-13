"""
main.py - Entry point for the upgraded Coupon Selling Bot.

Major Upgrades:
  • Role-based access control (Super Admin / Admin / Support)
  • Wallet Recharge via UPI QR Code + Transaction ID verification
  • Bulk Discounts & Referral / Affiliate System
  • Background Jobs: Low-stock alerts, asynchronous broadcasts
  • Security: Rate limiting, Anti-Spam, Auto-fraud flagging
  • Full Database Backup / Restore from Admin panel
  • Inline Telegram Animations for loading/processing states
"""

import logging
import sys
import warnings

from telegram.warnings import PTBUserWarning

warnings.filterwarnings("ignore", message=r".*per_message.*", category=PTBUserWarning)

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes

import config
from database import Database
from notifications import low_stock_job, daily_report_job

from handlers.security import register_security_handlers
from handlers.channel import register_channel_handlers
from handlers.user import register_user_handlers
from handlers.payment import register_payment_handlers
from handlers.admin import register_admin_handlers

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    handlers=[logging.StreamHandler(sys.stdout)],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def _post_init(app: Application):
    await Database.get_instance()
    db = await Database.get_instance()
    
    # Bootstrap settings from env if unset
    if config.UPI_ID and not await db.get_setting("upi_id"):
        await db.set_setting("upi_id", config.UPI_ID)
    if config.PAYEE_NAME and not await db.get_setting("payee_name"):
        await db.set_setting("payee_name", config.PAYEE_NAME)
    if await db.get_setting("maintenance") is None:
        await db.set_setting("maintenance", "true" if config.MAINTENANCE_MODE else "false")
    
    # Bootstrap super admins
    for uid in config.SUPER_ADMIN_IDS:
        await db.set_admin_role(uid, "super_admin", added_by=0)

    await app.bot.set_my_commands([
        BotCommand("start", "Start the bot / main menu"),
        BotCommand("admin", "Admin control panel"),
    ])
    
    # Background jobs
    if app.job_queue:
        app.job_queue.run_repeating(low_stock_job, interval=config.LOW_STOCK_CHECK_INTERVAL, first=10)
        try:
            from datetime import time as dtime
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(config.TIMEZONE)
            app.job_queue.run_daily(
                daily_report_job,
                time=dtime(hour=config.DAILY_REPORT_HOUR, minute=config.DAILY_REPORT_MINUTE, tzinfo=tz),
            )
            logger.info("Daily report scheduled at %02d:%02d %s",
                        config.DAILY_REPORT_HOUR, config.DAILY_REPORT_MINUTE, config.TIMEZONE)
        except Exception:
            logger.exception("Could not schedule daily report (continuing without it)")
    else:
        logger.warning("JobQueue unavailable — install python-telegram-bot[job-queue] "
                       "for low-stock alerts and daily reports.")

    logger.info("Bot initialized and ready.")


async def _post_shutdown(app: Application):
    inst = Database._instance
    if inst:
        await inst.close()
    logger.info("Bot shut down cleanly.")


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error: %s", ctx.error, exc_info=ctx.error)


def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(config.BOT_TOKEN)
        .concurrent_updates(True)        # handle many users in parallel
        # ── HTTP tuning: keep a big connection pool and WAIT for a free slot
        # under bursts instead of failing fast (default pool_timeout is 1s,
        # which drops requests when several users act at once). ──
        .connection_pool_size(256)
        .pool_timeout(20.0)
        .connect_timeout(10.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .get_updates_connection_pool_size(16)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    
    # Order matters: Security/RateLimit FIRST, then features
    register_security_handlers(app)
    register_channel_handlers(app)
    register_user_handlers(app)
    register_payment_handlers(app)
    register_admin_handlers(app)

    # Catch-all in a LATE group: any callback not matched above still gets
    # answered so the button never shows an endless loading spinner.
    from telegram.ext import CallbackQueryHandler
    async def _catch_all_cb(update, ctx):
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    app.add_handler(CallbackQueryHandler(_catch_all_cb), group=9)

    app.add_error_handler(_on_error)
    return app


def main():
    config.validate()
    app = build_app()

    if config.WEBHOOK_URL:
        logger.info("Starting in WEBHOOK mode on port %s", config.PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=config.PORT,
            url_path=config.BOT_TOKEN,
            webhook_url=f"{config.WEBHOOK_URL.rstrip('/')}/{config.BOT_TOKEN}",
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
    else:
        logger.info("Starting in POLLING mode.")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
