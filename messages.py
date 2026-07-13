"""
messages.py - Static and dynamic message templates.
"""

from config import BOT_NAME, CURRENCY_SYMBOL, SUPPORT_USERNAME


def _cur(amount: float) -> str:
    return f"{CURRENCY_SYMBOL}{amount:,.2f}"


def welcome(first_name: str, balance: float) -> str:
    return (
        f"👋 *Welcome to {BOT_NAME}, {first_name}!*\n\n"
        f"💰 Wallet Balance: *{_cur(balance)}*\n\n"
        "Buy coupons instantly using your wallet. Recharge anytime — "
        "payments are verified *automatically*.\n\n"
        "🎁 Invite friends and earn wallet rewards!\n\n"
        "Choose an option below 👇"
    )


def wallet_overview(balance: float, total_recharged: float, total_spent: float,
                    ref_earnings: float = 0.0) -> str:
    return (
        "💼 *My Wallet*\n\n"
        f"💰 Current Balance: *{_cur(balance)}*\n"
        f"⬆️ Total Recharged: {_cur(total_recharged)}\n"
        f"🛒 Total Spent: {_cur(total_spent)}\n"
        f"🎁 Referral Earnings: {_cur(ref_earnings)}\n\n"
        "Use the buttons below to recharge or view your transactions."
    )


def recharge_instructions(upi_id: str, payee: str) -> str:
    return (
        "➕ *Recharge Wallet*\n\n"
        "1️⃣ *Scan the QR above*, or pay to this UPI ID:\n"
        f"   💳 `{upi_id}`\n"
        f"   👤 {payee}\n\n"
        "2️⃣ Tap *✅ I've Paid* and send your *UPI Transaction ID / UTR*.\n\n"
        "✅ Your wallet is credited *automatically* once the payment is "
        "verified from our bank email — usually within 1–2 minutes.\n\n"
        "🔒 _Each transaction ID can be used only once._"
    )


def enter_txn_prompt() -> str:
    return (
        "🧾 *Enter Transaction ID*\n\n"
        "Please send the *UPI Transaction ID / UTR* (the 12-digit reference "
        "number shown in your UPI app after payment).\n\n"
        "Just paste it here 👇"
    )


def recharge_success(amount: float, balance: float) -> str:
    return (
        "✅ *Recharge Successful!*\n\n"
        f"Added: *{_cur(amount)}*\n"
        f"New Balance: *{_cur(balance)}*\n\n"
        "You can now buy coupons instantly. 🎉"
    )


def category_detail(name: str, price: float, stock: int, balance: float,
                    tiers_text: str = "") -> str:
    base = (
        f"🏷️ *{name}*\n\n"
        f"💵 Price: *{_cur(price)}* each\n"
        f"📦 In stock: *{stock}*\n"
        f"💰 Your balance: *{_cur(balance)}*\n"
    )
    if tiers_text:
        base += f"\n🎉 *Bulk discounts:*\n{tiers_text}\n"
    base += "\nSelect a quantity to buy 👇"
    return base


def coupon_card(name: str, price: float, stock: int, balance: float,
                tiers_text: str, terms: str) -> str:
    """Modern coupon card shown BEFORE purchase, including Terms & Conditions."""
    card = (
        "🎟️ ─────────────────\n"
        f"    *{name}*\n"
        "─────────────────\n\n"
        f"💵 Price: *{_cur(price)}* each\n"
        f"📦 In stock: *{stock}*\n"
        f"💰 Your balance: *{_cur(balance)}*\n"
    )
    if tiers_text and "No bulk" not in tiers_text:
        card += f"\n🎉 *Bulk discounts:*\n{tiers_text}\n"
    card += "\n📜 *Terms & Conditions:*\n"
    card += (terms.strip() if terms and terms.strip() else
             "• Coupons are digital and non-refundable once delivered.\n"
             "• One code per purchase unit.\n"
             "• Use before the coupon's own expiry (if any).")
    card += "\n\n_Tap *I Agree* to accept the terms and continue._"
    return card


def reseller_info(fee: float, balance: float, auto: bool) -> str:
    mode = ("Your account activates *instantly* after payment."
            if auto else "Your request goes to the Super Admin for approval.")
    return (
        "🏪 *Become a Reseller*\n\n"
        "Run your own coupon storefront inside this bot:\n"
        "• Create & manage your *own* categories\n"
        "• Upload your own coupon stock\n"
        "• Set your own prices\n"
        "• Track your own sales & customers\n\n"
        f"💵 One-time activation fee: *{_cur(fee)}*\n"
        f"💰 Your wallet balance: *{_cur(balance)}*\n\n"
        f"{mode}\n\n"
        "The fee is paid from your wallet. Recharge first if needed."
    )


def reseller_requested() -> str:
    return ("✅ *Request Submitted!*\n\nYour reseller fee was received. The Super "
            "Admin will review and activate your account shortly. You'll be "
            "notified here once approved.")


def reseller_activated() -> str:
    return ("🎉 *Reseller Access Activated!*\n\nYou now have your own seller "
            "dashboard. Open it with /admin to create categories, upload stock "
            "and set prices. Happy selling! 🏪")


def reseller_disabled() -> str:
    return ("🏪 *Reseller Program Unavailable*\n\nThe reseller option is "
            "currently turned off by the administrator. Please check back later.")


def withdrawal_breakdown(revenue: float, commission_pct: float,
                         commission: float, final: float, upi_id: str) -> str:
    return (
        "💸 *Confirm Revenue Withdrawal*\n\n"
        f"🧾 Total Revenue: *{_cur(revenue)}*\n"
        f"🏦 Super Admin Commission ({commission_pct:g}%): *−{_cur(commission)}*\n"
        f"✅ Final Withdrawable Amount: *{_cur(final)}*\n\n"
        f"💳 UPI ID: `{upi_id}`\n\n"
        "The request will be sent to the Super Admin for approval. "
        "You can submit *only one* request per day.\n\n"
        "_Tap Confirm to submit._"
    )


def withdrawal_requested(wid: str, final: float) -> str:
    return (
        "✅ *Withdrawal Request Submitted!*\n\n"
        f"Reference: `{wid}`\n"
        f"You will receive *{_cur(final)}* once the Super Admin approves it.\n\n"
        "Track it under *Withdrawal History*."
    )


def withdrawal_none() -> str:
    return ("💸 *Withdraw Revenue*\n\nYou have no revenue available to withdraw "
            "right now. Make some sales first! 📈")


def withdrawal_already_today() -> str:
    return ("⏳ *Daily Limit Reached*\n\nYou can submit only *one* withdrawal "
            "request per day. Please try again tomorrow.")


def withdrawal_approved_user(wid: str, final: float, ref: str = "") -> str:
    extra = f"\nPayment Ref: `{ref}`" if ref else ""
    return (
        "🎉 *Withdrawal Approved!*\n\n"
        f"Reference: `{wid}`\n"
        f"Amount paid: *{_cur(final)}* to your UPI.{extra}\n\n"
        "Thank you!"
    )


def withdrawal_rejected_user(wid: str) -> str:
    return (
        "❌ *Withdrawal Rejected*\n\n"
        f"Reference: `{wid}`\n"
        "Your revenue has been released back to your available balance. "
        "Contact support if you have questions."
    )


def out_of_stock_msg(name: str) -> str:
    return (
        f"😔 *{name}* is currently *out of stock.*\n\n"
        "Please check back later or browse other categories."
    )


def insufficient_balance(needed: float, balance: float) -> str:
    return (
        "⚠️ *Insufficient Wallet Balance*\n\n"
        f"Order total: *{_cur(needed)}*\n"
        f"Your balance: *{_cur(balance)}*\n"
        f"Short by: *{_cur(needed - balance)}*\n\n"
        "Please recharge your wallet to continue."
    )


def purchase_success(name: str, qty: int, amount: float, balance: float,
                     discount: float = 0.0) -> str:
    txt = (
        "✅ *Purchase Successful!*\n\n"
        f"Item: {name}\n"
        f"Quantity: {qty}\n"
    )
    if discount > 0:
        txt += f"Discount: *−{_cur(discount)}* 🎉\n"
    txt += (
        f"Paid: *{_cur(amount)}* (from wallet)\n"
        f"Remaining Balance: *{_cur(balance)}*\n"
    )
    return txt


def referral_home(ref_link: str, count: int, earnings: float, cfg: dict) -> str:
    lines = [
        "🎁 *Referral & Affiliate Program*\n",
        f"👥 Friends invited: *{count}*",
        f"💰 Total earned: *{_cur(earnings)}*\n",
        "*How it works:*",
    ]
    if cfg.get("signup_bonus", 0) > 0:
        lines.append(f"• Earn *{_cur(cfg['signup_bonus'])}* when a friend makes "
                     "their first recharge.")
    if cfg.get("commission_pct", 0) > 0:
        lines.append(f"• Earn *{cfg['commission_pct']:g}%* of every recharge they "
                     "ever make.")
    if cfg.get("welcome_bonus", 0) > 0:
        lines.append(f"• Your friend also gets *{_cur(cfg['welcome_bonus'])}* to start!")
    lines += [
        "\n*Your invite link:*",
        f"`{ref_link}`",
        "\nShare it anywhere. Rewards are credited to your wallet automatically.",
    ]
    return "\n".join(lines)


def referral_reward(amount: float, kind: str, balance: float) -> str:
    label = "signup bonus" if kind == "signup" else "referral commission"
    return (
        f"🎉 *Referral Reward!*\n\n"
        f"You earned *{_cur(amount)}* ({label}).\n"
        f"New wallet balance: *{_cur(balance)}*\n\n"
        "Keep inviting to earn more! 🚀"
    )


def help_msg() -> str:
    contact = f"\n\nNeed help? Contact @{SUPPORT_USERNAME}" if SUPPORT_USERNAME else \
              "\n\nNeed help? Contact the admin."
    return (
        "ℹ️ *Help & Support*\n\n"
        "• *Browse Categories* — see available coupons\n"
        "• *My Wallet* — recharge & check balance\n"
        "• *My Orders* — view past purchases & codes\n"
        "• *Refer & Earn* — invite friends for wallet rewards\n\n"
        "*How to buy:*\n"
        "1. Recharge your wallet (scan QR / pay UPI)\n"
        "2. Pick a category & quantity (bulk = discount!)\n"
        "3. Coupons are delivered instantly!" + contact
    )


def maintenance() -> str:
    return (
        "🛠️ *Bot Under Maintenance*\n\n"
        "We'll be back shortly. Your wallet balance is safe. Thanks for your patience!"
    )


def banned() -> str:
    return "🚫 *Access Denied*\n\nYour account has been suspended. Contact support."


def rate_limited() -> str:
    return (
        "🐢 *Slow down a little!*\n\n"
        "You're tapping too fast. Please wait a few seconds and try again."
    )


def no_categories() -> str:
    return "🗓 No categories available yet. Please check back soon!"


def new_coupon_alert(name: str, price: float) -> str:
    return (
        "🆕 *New Coupons Available!*\n\n"
        f"🏷️ *{name}* is now in stock at *{_cur(price)}* each.\n\n"
        "Open the bot and grab yours before they're gone! 🔥"
    )
