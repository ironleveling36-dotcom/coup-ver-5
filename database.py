"""
database.py - MongoDB async data layer (Motor) for the Coupon Selling Bot.

Single source of truth for ALL persistent data:
  • users          - profile + wallet balance + referral + notify prefs + fraud
  • admins         - role-based staff (super_admin / admin / support)
  • categories     - coupon categories / products
  • stock          - individual coupon codes (one row per code)
  • orders         - purchase history
  • transactions   - wallet ledger (recharge + purchase + admin + referral)
  • used_txns      - UPI transaction IDs already consumed (anti-replay)
  • referrals      - referral edges (referrer -> referred)
  • settings       - key/value bot settings (UPI id, discounts, maintenance…)
  • counters       - auto-increment ids

Design goals:
  • Wallet balance updates are ATOMIC ($inc + conditional filters) so
    concurrent purchases can never double-spend.
  • Every balance change writes a transactions ledger row -> nothing is lost.
  • Singleton client with pooled connections -> fast + supports many users.
"""

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument, ASCENDING, DESCENDING

import config

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    """Async MongoDB wrapper. Use `await Database.get_instance()`."""

    _instance: Optional["Database"] = None

    def __init__(self):
        self.client: Optional[AsyncIOMotorClient] = None
        self.db = None
        # ── In-memory hot-path caches (singleton-scoped) ─────────────────────
        # Settings & roles are read on almost every update but change rarely.
        # Caching them collapses 5-10 Mongo round-trips per action down to ~0,
        # which is the single biggest latency win under concurrent users.
        self._settings_cache: dict = {}     # key -> (value, expiry_monotonic)
        self._role_cache: dict = {}         # user_id -> (role_or_None, expiry)
        self._SETTINGS_TTL = 30.0           # seconds
        self._ROLE_TTL = 30.0               # seconds

    # ── Singleton / lifecycle ────────────────────────────────────────────────
    @classmethod
    async def get_instance(cls) -> "Database":
        if cls._instance is None:
            inst = cls()
            await inst.connect()
            cls._instance = inst
        return cls._instance

    async def connect(self):
        self.client = AsyncIOMotorClient(
            config.MONGO_URI,
            maxPoolSize=50,
            minPoolSize=5,
            serverSelectionTimeoutMS=10000,
            retryWrites=True,
        )
        self.db = self.client[config.MONGO_DB_NAME]
        await self.client.admin.command("ping")  # fail fast if unreachable
        await self._ensure_indexes()
        logger.info("Connected to MongoDB database '%s'", config.MONGO_DB_NAME)

    async def _ensure_indexes(self):
        await self.db.users.create_index([("user_id", ASCENDING)], unique=True)
        await self.db.users.create_index([("referred_by", ASCENDING)])
        await self.db.admins.create_index([("user_id", ASCENDING)], unique=True)
        await self.db.categories.create_index([("name", ASCENDING)], unique=True)
        await self.db.stock.create_index([("category_id", ASCENDING), ("is_sold", ASCENDING)])
        await self.db.orders.create_index([("order_id", ASCENDING)], unique=True)
        await self.db.orders.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await self.db.transactions.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await self.db.transactions.create_index([("ref", ASCENDING)])
        await self.db.used_txns.create_index([("txn_id", ASCENDING)], unique=True)
        await self.db.referrals.create_index([("referred_id", ASCENDING)], unique=True)
        await self.db.referrals.create_index([("referrer_id", ASCENDING)])
        await self.db.settings.create_index([("key", ASCENDING)], unique=True)
        await self.db.counters.create_index([("_id", ASCENDING)])
        await self.db.categories.create_index([("owner_id", ASCENDING)])
        await self.db.stock.create_index([("category_id", ASCENDING), ("item", ASCENDING)])
        await self.db.orders.create_index([("owner_id", ASCENDING), ("created_at", DESCENDING)])
        await self.db.withdrawals.create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
        await self.db.withdrawals.create_index([("status", ASCENDING), ("created_at", ASCENDING)])
        await self.db.reseller_requests.create_index([("user_id", ASCENDING), ("status", ASCENDING)])
        await self.db.withdrawals.create_index([("withdrawal_id", ASCENDING)], unique=True)

    async def close(self):
        if self.client:
            self.client.close()
            self.client = None
        Database._instance = None

    # ── Counters ──────────────────────────────────────────────────────────────
    async def _next_seq(self, name: str) -> int:
        doc = await self.db.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return doc["seq"]

    # ══════════════════════════════════════════════════════════════════════
    # USERS + WALLET
    # ══════════════════════════════════════════════════════════════════════
    async def upsert_user(self, user_id: int, username: str, full_name: str) -> dict:
        """Create the user if new (wallet starts at 0), else update profile.
        Wallet balance is NEVER reset on update -> survives restarts/updates."""
        await self.db.users.update_one(
            {"user_id": user_id},
            {
                "$set": {"username": username, "full_name": full_name, "last_seen": _now()},
                "$setOnInsert": {
                    "wallet_balance": 0.0,
                    "is_banned": False,
                    "joined_at": _now(),
                    "total_spent": 0.0,
                    "total_recharged": 0.0,
                    "notify": True,
                    "referred_by": None,
                    "ref_earnings": 0.0,
                    "ref_count": 0,
                    "failed_txn_attempts": 0,
                    "flagged": False,
                    "first_recharge_done": False,
                    "favorites": [],
                    "is_reseller": False,
                },
            },
            upsert=True,
        )
        return await self.get_user(user_id)

    async def get_user(self, user_id: int) -> Optional[dict]:
        return await self.db.users.find_one({"user_id": user_id})

    async def get_balance(self, user_id: int) -> float:
        u = await self.db.users.find_one({"user_id": user_id}, {"wallet_balance": 1})
        return float(u["wallet_balance"]) if u else 0.0

    async def is_banned(self, user_id: int) -> bool:
        u = await self.db.users.find_one({"user_id": user_id}, {"is_banned": 1})
        return bool(u and u.get("is_banned"))

    async def set_banned(self, user_id: int, banned: bool):
        await self.db.users.update_one(
            {"user_id": user_id}, {"$set": {"is_banned": banned}}, upsert=True
        )

    async def set_notify(self, user_id: int, enabled: bool):
        await self.db.users.update_one(
            {"user_id": user_id}, {"$set": {"notify": enabled}}, upsert=True
        )

    async def count_users(self) -> int:
        return await self.db.users.count_documents({})

    async def all_user_ids(self, segment: str = "all") -> list[int]:
        """
        Segment options for broadcasts:
          all        - every non-banned user
          notify     - non-banned users who opted into notifications
          buyers     - users with >=1 completed order
          with_balance - users whose wallet balance > 0
          recharged  - users who have recharged at least once
        """
        q = {"is_banned": {"$ne": True}}
        if segment == "notify":
            q["notify"] = {"$ne": False}
        elif segment == "with_balance":
            q["wallet_balance"] = {"$gt": 0}
        elif segment == "recharged":
            q["total_recharged"] = {"$gt": 0}

        if segment == "buyers":
            ids = await self.db.orders.distinct("user_id", {"status": "completed"})
            banned = set(await self.db.users.distinct("user_id", {"is_banned": True}))
            return [i for i in ids if i not in banned]

        cur = self.db.users.find(q, {"user_id": 1})
        return [d["user_id"] async for d in cur]

    async def notify_user_ids(self) -> list[int]:
        return await self.all_user_ids("notify")

    # ── Atomic wallet ops ─────────────────────────────────────────────────────
    async def credit_wallet(
        self, user_id: int, amount: float, *, ttype: str, ref: str = "", note: str = ""
    ) -> float:
        """Add funds atomically and write a ledger row. Returns new balance."""
        amount = round(float(amount), 2)
        inc = {"wallet_balance": amount}
        if ttype == "recharge":
            inc["total_recharged"] = amount
        elif ttype == "referral":
            inc["ref_earnings"] = amount
        doc = await self.db.users.find_one_and_update(
            {"user_id": user_id},
            {"$inc": inc},
            return_document=ReturnDocument.AFTER,
            upsert=True,
        )
        new_balance = round(float(doc["wallet_balance"]), 2)
        await self._log_txn(user_id, ttype, amount, new_balance, ref=ref, note=note, status="success")
        return new_balance

    async def debit_wallet(
        self, user_id: int, amount: float, *, ttype: str = "purchase", ref: str = "", note: str = ""
    ) -> Optional[float]:
        """Deduct funds ONLY if balance is sufficient (atomic). Returns new
        balance, or None if insufficient funds (no change made)."""
        amount = round(float(amount), 2)
        doc = await self.db.users.find_one_and_update(
            {"user_id": user_id, "wallet_balance": {"$gte": amount}},
            {"$inc": {"wallet_balance": -amount, "total_spent": amount if ttype == "purchase" else 0.0}},
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            return None  # insufficient funds
        new_balance = round(float(doc["wallet_balance"]), 2)
        await self._log_txn(user_id, ttype, -amount, new_balance, ref=ref, note=note, status="success")
        return new_balance

    async def admin_adjust_wallet(self, user_id: int, amount: float, note: str = "") -> float:
        """Admin sets wallet up or down by `amount` (can be negative)."""
        amount = round(float(amount), 2)
        doc = await self.db.users.find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"wallet_balance": amount}},
            return_document=ReturnDocument.AFTER,
            upsert=True,
        )
        new_balance = round(float(doc["wallet_balance"]), 2)
        await self._log_txn(user_id, "admin_adjust", amount, new_balance, note=note, status="success")
        return new_balance

    async def _log_txn(self, user_id, ttype, amount, balance_after, *, ref="", note="", status="success"):
        await self.db.transactions.insert_one({
            "user_id": user_id,
            "type": ttype,            # recharge | purchase | admin_adjust | refund | referral
            "amount": round(float(amount), 2),
            "balance_after": round(float(balance_after), 2),
            "ref": ref,
            "note": note,
            "status": status,
            "created_at": _now(),
        })

    async def get_transactions(self, user_id: int, limit: int = 20) -> list[dict]:
        cur = self.db.transactions.find({"user_id": user_id}).sort("created_at", DESCENDING).limit(limit)
        return [d async for d in cur]

    async def get_all_transactions(self, limit: int = 50) -> list[dict]:
        cur = self.db.transactions.find({}).sort("created_at", DESCENDING).limit(limit)
        return [d async for d in cur]

    # ── UPI anti-replay ───────────────────────────────────────────────────────
    async def is_txn_used(self, txn_id: str) -> bool:
        return await self.db.used_txns.find_one({"txn_id": txn_id}) is not None

    async def mark_txn_used(self, txn_id: str, user_id: int, amount: float):
        try:
            await self.db.used_txns.insert_one({
                "txn_id": txn_id,
                "user_id": user_id,
                "amount": round(float(amount), 2),
                "approved_at": _now(),
            })
            return True
        except Exception:
            return False  # duplicate key -> already used

    # ── Fraud / security counters ─────────────────────────────────────────────
    async def record_failed_txn(self, user_id: int) -> int:
        doc = await self.db.users.find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"failed_txn_attempts": 1}},
            return_document=ReturnDocument.AFTER,
            upsert=True,
        )
        return int(doc.get("failed_txn_attempts", 0))

    async def reset_failed_txn(self, user_id: int):
        await self.db.users.update_one(
            {"user_id": user_id}, {"$set": {"failed_txn_attempts": 0}}
        )

    async def flag_user(self, user_id: int, reason: str = ""):
        await self.db.users.update_one(
            {"user_id": user_id},
            {"$set": {"flagged": True, "flag_reason": reason, "flagged_at": _now()}},
            upsert=True,
        )

    async def unflag_user(self, user_id: int):
        await self.db.users.update_one(
            {"user_id": user_id}, {"$set": {"flagged": False, "flag_reason": ""}}
        )

    async def list_flagged(self, limit: int = 25) -> list[dict]:
        cur = self.db.users.find({"flagged": True}).limit(limit)
        return [d async for d in cur]

    # ══════════════════════════════════════════════════════════════════════
    # ROLE-BASED ADMINS
    # ══════════════════════════════════════════════════════════════════════
    async def get_admin_role(self, user_id: int) -> Optional[str]:
        now = time.monotonic()
        hit = self._role_cache.get(user_id)
        if hit and hit[1] > now:
            return hit[0]
        d = await self.db.admins.find_one({"user_id": user_id}, {"role": 1})
        role = d["role"] if d else None
        self._role_cache[user_id] = (role, now + self._ROLE_TTL)
        return role

    def _invalidate_role(self, user_id: int):
        self._role_cache.pop(user_id, None)

    async def effective_role(self, user_id: int) -> str:
        """
        Combine env bootstrap with DB-assigned roles. Highest wins.
        Env SUPER_ADMIN_IDS always resolve to super_admin (can't be locked out).
        Returns one of: super_admin | admin | support | user.
        """
        from utils import env_role, ROLE_USER
        env = env_role(user_id)
        if env == "super_admin":
            return "super_admin"
        db_role = await self.get_admin_role(user_id)
        # Rank the candidates and return the strongest.
        rank = {"super_admin": 4, "admin": 3, "reseller": 2, "support": 1,
                None: 0, ROLE_USER: 0}
        best = max([env, db_role], key=lambda r: rank.get(r, 0))
        return best or ROLE_USER

    async def is_staff(self, user_id: int) -> bool:
        role = await self.effective_role(user_id)
        return role in ("super_admin", "admin", "reseller", "support")

    async def set_admin_role(self, user_id: int, role: str, added_by: int = 0):
        await self.db.admins.update_one(
            {"user_id": user_id},
            {"$set": {"role": role, "added_by": added_by, "updated_at": _now()},
             "$setOnInsert": {"created_at": _now()}},
            upsert=True,
        )
        self._invalidate_role(user_id)

    async def remove_admin(self, user_id: int):
        await self.db.admins.delete_one({"user_id": user_id})
        self._invalidate_role(user_id)

    async def list_admins(self) -> list[dict]:
        cur = self.db.admins.find({}).sort("created_at", ASCENDING)
        return [d async for d in cur]

    # ══════════════════════════════════════════════════════════════════════
    # REFERRALS
    # ══════════════════════════════════════════════════════════════════════
    async def set_referrer(self, referred_id: int, referrer_id: int) -> bool:
        """Link referred_id -> referrer_id once. Returns True if newly linked."""
        if referred_id == referrer_id:
            return False
        u = await self.db.users.find_one({"user_id": referred_id}, {"referred_by": 1})
        if u and u.get("referred_by"):
            return False  # already referred
        try:
            await self.db.referrals.insert_one({
                "referred_id": referred_id,
                "referrer_id": referrer_id,
                "created_at": _now(),
                "rewarded_signup": False,
            })
        except Exception:
            return False
        await self.db.users.update_one(
            {"user_id": referred_id}, {"$set": {"referred_by": referrer_id}}
        )
        await self.db.users.update_one(
            {"user_id": referrer_id}, {"$inc": {"ref_count": 1}}, upsert=True
        )
        return True

    async def get_referrer(self, user_id: int) -> Optional[int]:
        u = await self.db.users.find_one({"user_id": user_id}, {"referred_by": 1})
        return u.get("referred_by") if u else None

    async def mark_signup_rewarded(self, referred_id: int) -> bool:
        res = await self.db.referrals.update_one(
            {"referred_id": referred_id, "rewarded_signup": False},
            {"$set": {"rewarded_signup": True, "rewarded_at": _now()}},
        )
        return res.modified_count > 0

    async def referral_stats(self, user_id: int) -> dict:
        u = await self.db.users.find_one(
            {"user_id": user_id}, {"ref_count": 1, "ref_earnings": 1}
        ) or {}
        count = await self.db.referrals.count_documents({"referrer_id": user_id})
        return {
            "count": max(count, int(u.get("ref_count", 0))),
            "earnings": round(float(u.get("ref_earnings", 0.0)), 2),
        }

    async def referral_leaderboard(self, limit: int = 10) -> list[dict]:
        cur = self.db.users.find(
            {"ref_count": {"$gt": 0}},
            {"user_id": 1, "username": 1, "full_name": 1, "ref_count": 1, "ref_earnings": 1},
        ).sort("ref_earnings", DESCENDING).limit(limit)
        return [d async for d in cur]

    # ══════════════════════════════════════════════════════════════════════
    # CATEGORIES (coupon products)
    # ══════════════════════════════════════════════════════════════════════
    async def add_category(self, name: str, price: float, owner_id: int = 0,
                           terms: str = "") -> int:
        cid = await self._next_seq("category_id")
        await self.db.categories.insert_one({
            "id": cid, "name": name, "price": round(float(price), 2),
            "is_active": True, "created_at": _now(), "low_stock_alerted": False,
            "owner_id": int(owner_id),      # 0 = global / super admin
            "terms": terms or "",
            "expires_at": None,             # optional coupon expiry
            "release_at": None,             # optional scheduled release
        })
        return cid

    async def get_categories(self, active_only: bool = True,
                             owner_id: int | None = None,
                             storefront: bool = False) -> list[dict]:
        """
        owner_id  -> when set, only that owner's categories (admin/reseller view)
        storefront-> hide expired + not-yet-released (customer browse view)
        """
        q = {}
        if active_only:
            q["is_active"] = True
        if owner_id is not None:
            q["owner_id"] = int(owner_id)
        if storefront:
            now = _now()
            q["$and"] = [
                {"$or": [{"expires_at": None}, {"expires_at": {"$gt": now}}]},
                {"$or": [{"release_at": None}, {"release_at": {"$lte": now}}]},
            ]
        cur = self.db.categories.find(q).sort("id", ASCENDING)
        return [d async for d in cur]

    async def category_owned_by(self, cat_id: int, owner_id: int) -> bool:
        c = await self.db.categories.find_one({"id": cat_id}, {"owner_id": 1})
        return bool(c and int(c.get("owner_id", 0)) == int(owner_id))

    async def get_category(self, cat_id: int) -> Optional[dict]:
        return await self.db.categories.find_one({"id": cat_id})

    async def update_category(self, cat_id: int, **fields):
        if fields:
            await self.db.categories.update_one({"id": cat_id}, {"$set": fields})

    async def delete_category(self, cat_id: int):
        await self.db.categories.delete_one({"id": cat_id})
        await self.db.stock.delete_many({"category_id": cat_id})

    # ══════════════════════════════════════════════════════════════════════
    # STOCK (coupon codes)
    # ══════════════════════════════════════════════════════════════════════
    async def add_stock(self, cat_id: int, items: list[str]) -> dict:
        """Insert coupon codes, skipping duplicates (existing in this category
        OR repeated within the batch). Returns {added, skipped}."""
        cleaned, seen = [], set()
        for it in items:
            v = it.strip()
            if not v or v in seen:
                continue
            seen.add(v)
            cleaned.append(v)
        if not cleaned:
            return {"added": 0, "skipped": 0}

        existing = set(await self.db.stock.distinct(
            "item", {"category_id": cat_id, "item": {"$in": cleaned}}))
        fresh = [v for v in cleaned if v not in existing]
        skipped = len(items) - len(fresh)

        if fresh:
            docs = [
                {"category_id": cat_id, "item": v, "is_sold": False,
                 "sold_at": None, "order_id": None, "created_at": _now()}
                for v in fresh
            ]
            await self.db.stock.insert_many(docs)
            await self.db.categories.update_one(
                {"id": cat_id}, {"$set": {"low_stock_alerted": False}})
        return {"added": len(fresh), "skipped": skipped}

    async def export_stock(self, owner_id: int | None = None) -> list[dict]:
        """Return stock rows (with category name) for CSV inventory export."""
        cats = await self.get_categories(active_only=False, owner_id=owner_id)
        name_by_id = {c["id"]: c["name"] for c in cats}
        ids = list(name_by_id.keys())
        q = {} if owner_id is None else {"category_id": {"$in": ids}}
        cur = self.db.stock.find(q).sort("category_id", ASCENDING)
        rows = []
        async for s in cur:
            rows.append({
                "category": name_by_id.get(s["category_id"], str(s["category_id"])),
                "code": s["item"],
                "status": "SOLD" if s.get("is_sold") else "AVAILABLE",
                "order_id": s.get("order_id") or "",
            })
        return rows

    # ── Favorites ─────────────────────────────────────────────────────────────
    async def toggle_favorite(self, user_id: int, cat_id: int) -> bool:
        u = await self.db.users.find_one({"user_id": user_id}, {"favorites": 1})
        favs = (u or {}).get("favorites", []) or []
        if cat_id in favs:
            await self.db.users.update_one(
                {"user_id": user_id}, {"$pull": {"favorites": cat_id}})
            return False
        await self.db.users.update_one(
            {"user_id": user_id}, {"$addToSet": {"favorites": cat_id}}, upsert=True)
        return True

    async def get_favorites(self, user_id: int) -> list[int]:
        u = await self.db.users.find_one({"user_id": user_id}, {"favorites": 1})
        return (u or {}).get("favorites", []) or []

    async def search_categories(self, term: str, storefront: bool = True) -> list[dict]:
        cats = await self.get_categories(active_only=True, storefront=storefront)
        t = (term or "").strip().lower()
        return [c for c in cats if t in c["name"].lower()]

    async def stock_count(self, cat_id: int) -> int:
        return await self.db.stock.count_documents({"category_id": cat_id, "is_sold": False})

    async def reserve_stock(self, cat_id: int, qty: int, order_id: str) -> list[str]:
        """Atomically claim `qty` unsold codes for an order. Returns the codes.
        If fewer than qty are available, claims none and returns []."""
        claimed = []
        for _ in range(qty):
            doc = await self.db.stock.find_one_and_update(
                {"category_id": cat_id, "is_sold": False},
                {"$set": {"is_sold": True, "sold_at": _now(), "order_id": order_id}},
                return_document=ReturnDocument.AFTER,
            )
            if not doc:
                break
            claimed.append(doc["item"])
        if len(claimed) < qty:
            await self.db.stock.update_many(  # rollback partial claim
                {"order_id": order_id},
                {"$set": {"is_sold": False, "sold_at": None, "order_id": None}},
            )
            return []
        return claimed

    async def low_stock_categories(self, threshold: int) -> list[dict]:
        """Active categories at/under threshold that haven't been alerted yet."""
        out = []
        cats = await self.get_categories(active_only=True)
        for c in cats:
            cnt = await self.stock_count(c["id"])
            if cnt <= threshold and not c.get("low_stock_alerted"):
                c["_stock"] = cnt
                out.append(c)
        return out

    async def mark_low_stock_alerted(self, cat_id: int):
        await self.db.categories.update_one(
            {"id": cat_id}, {"$set": {"low_stock_alerted": True}}
        )

    # ══════════════════════════════════════════════════════════════════════
    # ORDERS (purchase history)
    # ══════════════════════════════════════════════════════════════════════
    async def create_order(self, order: dict):
        order["created_at"] = _now()
        order["updated_at"] = _now()
        await self.db.orders.insert_one(order)

    async def get_order(self, order_id: str) -> Optional[dict]:
        return await self.db.orders.find_one({"order_id": order_id})

    async def update_order(self, order_id: str, **fields):
        fields["updated_at"] = _now()
        await self.db.orders.update_one({"order_id": order_id}, {"$set": fields})

    async def get_user_orders(self, user_id: int, limit: int = 20) -> list[dict]:
        cur = self.db.orders.find({"user_id": user_id}).sort("created_at", DESCENDING).limit(limit)
        return [d async for d in cur]

    async def recent_orders(self, limit: int = 20) -> list[dict]:
        cur = self.db.orders.find({}).sort("created_at", DESCENDING).limit(limit)
        return [d async for d in cur]

    # ══════════════════════════════════════════════════════════════════════
    # SETTINGS
    # ══════════════════════════════════════════════════════════════════════
    async def get_setting(self, key: str, default=None):
        now = time.monotonic()
        hit = self._settings_cache.get(key)
        if hit and hit[1] > now:
            return hit[0] if hit[0] is not None else default
        d = await self.db.settings.find_one({"key": key})
        val = d["value"] if d else None
        self._settings_cache[key] = (val, now + self._SETTINGS_TTL)
        return val if val is not None else default

    async def set_setting(self, key: str, value):
        await self.db.settings.update_one(
            {"key": key}, {"$set": {"value": value}}, upsert=True
        )
        # Keep the hot-path cache in sync so changes take effect immediately.
        self._settings_cache[key] = (value, time.monotonic() + self._SETTINGS_TTL)

    async def get_force_channel(self) -> dict:
        return {
            "channel": await self.get_setting("force_channel", config.FORCE_CHANNEL_DEFAULT) or "",
            "url": await self.get_setting("force_channel_url", config.FORCE_CHANNEL_URL_DEFAULT) or "",
        }

    async def get_reseller_config(self) -> dict:
        return {
            "fee": float(await self.get_setting("reseller_fee", config.RESELLER_FEE_DEFAULT)),
            "auto_approve": (await self.get_setting(
                "reseller_auto_approve",
                "true" if config.RESELLER_AUTO_APPROVE_DEFAULT else "false")) == "true",
            "enabled": (await self.get_setting(
                "reseller_enabled",
                "true" if config.RESELLER_ENABLED_DEFAULT else "false")) == "true",
        }

    async def reseller_enabled(self) -> bool:
        """Global master switch for the reseller program."""
        return (await self.get_setting(
            "reseller_enabled",
            "true" if config.RESELLER_ENABLED_DEFAULT else "false")) == "true"

    async def get_withdrawal_commission_pct(self) -> float:
        """Super-admin commission % charged on admin revenue withdrawals."""
        return float(await self.get_setting(
            "withdrawal_commission_pct", config.WITHDRAWAL_COMMISSION_PCT_DEFAULT))

    async def get_discount_tiers(self) -> list[dict]:
        tiers = await self.get_setting("discount_tiers")
        if tiers is None:
            tiers = config.DISCOUNT_TIERS_DEFAULT
        return tiers

    async def set_discount_tiers(self, tiers: list[dict]):
        await self.set_setting("discount_tiers", tiers)

    async def get_referral_config(self) -> dict:
        return {
            "enabled": (await self.get_setting("ref_enabled",
                        "true" if config.REFERRAL_ENABLED_DEFAULT else "false")) == "true",
            "signup_bonus": float(await self.get_setting(
                "ref_signup_bonus", config.REFERRAL_SIGNUP_BONUS_DEFAULT)),
            "commission_pct": float(await self.get_setting(
                "ref_commission_pct", config.REFERRAL_COMMISSION_PCT_DEFAULT)),
            "welcome_bonus": float(await self.get_setting(
                "ref_welcome_bonus", config.REFERRAL_WELCOME_BONUS_DEFAULT)),
        }

    # ══════════════════════════════════════════════════════════════════════
    # ANALYTICS
    # ══════════════════════════════════════════════════════════════════════
    async def analytics(self) -> dict:
        total_users = await self.db.users.count_documents({})
        banned = await self.db.users.count_documents({"is_banned": True})
        flagged = await self.db.users.count_documents({"flagged": True})
        total_orders = await self.db.orders.count_documents({"status": "completed"})

        rev_cur = self.db.orders.aggregate([
            {"$match": {"status": "completed"}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
        ])
        rev = await rev_cur.to_list(1)
        revenue = round(rev[0]["total"], 2) if rev else 0.0

        rc_cur = self.db.transactions.aggregate([
            {"$match": {"type": "recharge"}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
        ])
        rc = await rc_cur.to_list(1)
        recharged = round(rc[0]["total"], 2) if rc else 0.0

        ref_cur = self.db.transactions.aggregate([
            {"$match": {"type": "referral"}},
            {"$group": {"_id": None, "total": {"$sum": "$amount"}}},
        ])
        rf = await ref_cur.to_list(1)
        ref_paid = round(rf[0]["total"], 2) if rf else 0.0

        wl_cur = self.db.users.aggregate([
            {"$group": {"_id": None, "total": {"$sum": "$wallet_balance"}}},
        ])
        wl = await wl_cur.to_list(1)
        wallet_liability = round(wl[0]["total"], 2) if wl else 0.0

        total_stock = await self.db.stock.count_documents({"is_sold": False})

        top_cur = self.db.orders.aggregate([
            {"$match": {"status": "completed"}},
            {"$group": {"_id": "$category_name", "count": {"$sum": "$quantity"},
                        "revenue": {"$sum": "$amount"}}},
            {"$sort": {"revenue": -1}},
            {"$limit": 5},
        ])
        top_categories = [d async for d in top_cur]

        return {
            "total_users": total_users,
            "banned_users": banned,
            "flagged_users": flagged,
            "total_orders": total_orders,
            "revenue": revenue,
            "recharged": recharged,
            "referral_paid": ref_paid,
            "wallet_liability": wallet_liability,
            "available_stock": total_stock,
            "top_categories": top_categories,
        }

    # ══════════════════════════════════════════════════════════════════════
    # TIME-WINDOWED & SCOPED ANALYTICS
    # ══════════════════════════════════════════════════════════════════════
    async def _sum_orders(self, match: dict) -> tuple[int, float]:
        cur = self.db.orders.aggregate([
            {"$match": {**match, "status": "completed"}},
            {"$group": {"_id": None, "count": {"$sum": "$quantity"},
                        "revenue": {"$sum": "$amount"}}},
        ])
        r = await cur.to_list(1)
        if not r:
            return 0, 0.0
        return int(r[0].get("count", 0)), round(float(r[0].get("revenue", 0.0)), 2)

    async def sales_summary(self, owner_id: int | None = None) -> dict:
        """today / week / month / all-time sales + revenue, optionally scoped."""
        now = _now()
        start_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_week = start_today - timedelta(days=start_today.weekday())
        start_month = start_today.replace(day=1)
        base = {} if owner_id is None else {"owner_id": int(owner_id)}

        out = {}
        for label, start in (("today", start_today), ("week", start_week),
                             ("month", start_month)):
            c, rev = await self._sum_orders({**base, "created_at": {"$gte": start}})
            out[label] = {"orders": c, "revenue": rev}
        c, rev = await self._sum_orders(base)
        out["all"] = {"orders": c, "revenue": rev}

        # active (7d) & new (today) users are global metrics
        out["active_users"] = await self.db.users.count_documents(
            {"last_seen": {"$gte": now - timedelta(days=7)}})
        out["new_users"] = await self.db.users.count_documents(
            {"joined_at": {"$gte": start_today}})
        return out

    async def admin_dashboard(self, owner_id: int) -> dict:
        """Scoped dashboard for an admin/reseller (their own categories only)."""
        summ = await self.sales_summary(owner_id=owner_id)
        cats = await self.get_categories(active_only=False, owner_id=owner_id)
        cat_ids = [c["id"] for c in cats]
        stock = await self.db.stock.count_documents(
            {"category_id": {"$in": cat_ids}, "is_sold": False})
        customers = len(await self.db.orders.distinct(
            "user_id", {"owner_id": int(owner_id), "status": "completed"}))
        return {
            "summary": summ, "categories": len(cats),
            "stock": stock, "customers": customers,
        }

    async def top_products(self, owner_id: int | None = None, limit: int = 5) -> list[dict]:
        match = {"status": "completed"}
        if owner_id is not None:
            match["owner_id"] = int(owner_id)
        cur = self.db.orders.aggregate([
            {"$match": match},
            {"$group": {"_id": "$category_name", "sold": {"$sum": "$quantity"},
                        "revenue": {"$sum": "$amount"}}},
            {"$sort": {"sold": -1}}, {"$limit": limit},
        ])
        return [d async for d in cur]

    async def admin_wise_sales(self) -> list[dict]:
        """Revenue grouped by category owner (for super-admin overview)."""
        cur = self.db.orders.aggregate([
            {"$match": {"status": "completed"}},
            {"$group": {"_id": "$owner_id", "orders": {"$sum": "$quantity"},
                        "revenue": {"$sum": "$amount"}}},
            {"$sort": {"revenue": -1}},
        ])
        return [d async for d in cur]

    # ══════════════════════════════════════════════════════════════════════
    # RESELLER REQUESTS
    # ══════════════════════════════════════════════════════════════════════
    async def create_reseller_request(self, user_id: int, fee: float) -> bool:
        existing = await self.db.reseller_requests.find_one(
            {"user_id": user_id, "status": "pending"})
        if existing:
            return False
        await self.db.reseller_requests.insert_one({
            "user_id": user_id, "fee": round(float(fee), 2),
            "status": "pending", "created_at": _now(),
        })
        return True

    async def pending_reseller_requests(self) -> list[dict]:
        cur = self.db.reseller_requests.find({"status": "pending"}).sort("created_at", ASCENDING)
        return [d async for d in cur]

    async def set_reseller_request_status(self, user_id: int, status: str):
        await self.db.reseller_requests.update_one(
            {"user_id": user_id, "status": "pending"},
            {"$set": {"status": status, "resolved_at": _now()}})

    async def activate_reseller(self, user_id: int):
        await self.set_admin_role(user_id, "reseller", added_by=0)
        await self.db.users.update_one(
            {"user_id": user_id}, {"$set": {"is_reseller": True}}, upsert=True)

    # ══════════════════════════════════════════════════════════════════════
    # WITHDRAWALS
    # ══════════════════════════════════════════════════════════════════════
    async def _withdrawal_sum(self, user_id: int, statuses: list[str],
                              field: str = "revenue") -> float:
        cur = self.db.withdrawals.aggregate([
            {"$match": {"user_id": int(user_id), "status": {"$in": statuses}}},
            {"$group": {"_id": None, "total": {"$sum": f"${field}"}}},
        ])
        r = await cur.to_list(1)
        return round(float(r[0]["total"]), 2) if r else 0.0

    async def admin_revenue_overview(self, user_id: int) -> dict:
        """Revenue snapshot used for the dashboard & withdrawal calculations.

        available = lifetime sales revenue − revenue locked in pending
        requests − revenue already approved for payout.
        """
        s = await self.sales_summary(owner_id=user_id)
        total = round(float(s["all"]["revenue"]), 2)
        pending_gross = await self._withdrawal_sum(user_id, ["pending"], "revenue")
        approved_gross = await self._withdrawal_sum(user_id, ["approved"], "revenue")
        pending_net = await self._withdrawal_sum(user_id, ["pending"], "amount")
        withdrawn_net = await self._withdrawal_sum(user_id, ["approved"], "amount")
        available = round(total - pending_gross - approved_gross, 2)
        return {
            "total_revenue": total,
            "available": max(0.0, available),
            "pending_gross": pending_gross,
            "pending_net": pending_net,
            "approved_gross": approved_gross,
            "withdrawn_net": withdrawn_net,
        }

    async def has_withdrawal_today(self, user_id: int) -> bool:
        """True if the admin already submitted a request today (1/day limit)."""
        now = _now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        n = await self.db.withdrawals.count_documents(
            {"user_id": int(user_id), "created_at": {"$gte": start}})
        return n > 0

    async def create_withdrawal(self, user_id: int, *, revenue: float,
                                commission_pct: float, commission: float,
                                amount: float, upi_id: str) -> str:
        """Create a pending revenue-withdrawal request. Revenue is 'locked'
        (excluded from available) until the Super Admin approves or rejects."""
        import uuid
        wid = "WD-" + uuid.uuid4().hex[:8].upper()
        await self.db.withdrawals.insert_one({
            "withdrawal_id": wid, "user_id": int(user_id),
            "revenue": round(float(revenue), 2),
            "commission_pct": round(float(commission_pct), 2),
            "commission": round(float(commission), 2),
            "amount": round(float(amount), 2),   # final withdrawable (paid to admin)
            "upi_id": upi_id, "status": "pending",
            "payment_reference": "", "created_at": _now(),
        })
        return wid

    async def list_withdrawals(self, status: str = "pending") -> list[dict]:
        cur = self.db.withdrawals.find({"status": status}).sort("created_at", ASCENDING)
        return [d async for d in cur]

    async def list_user_withdrawals(self, user_id: int, limit: int = 10) -> list[dict]:
        cur = (self.db.withdrawals.find({"user_id": int(user_id)})
               .sort("created_at", DESCENDING).limit(limit))
        return [d async for d in cur]

    async def get_withdrawal(self, wid: str) -> Optional[dict]:
        return await self.db.withdrawals.find_one({"withdrawal_id": wid})

    async def resolve_withdrawal(self, wid: str, approve: bool,
                                 payment_reference: str = "") -> Optional[dict]:
        """Approve or reject a pending request. On approve the revenue stays
        deducted (payout made off-platform via UPI); on reject the locked
        revenue is released back into the admin's available balance."""
        w = await self.db.withdrawals.find_one(
            {"withdrawal_id": wid, "status": "pending"})
        if not w:
            return None
        status = "approved" if approve else "rejected"
        upd = {"status": status, "resolved_at": _now()}
        if approve and payment_reference:
            upd["payment_reference"] = payment_reference
        await self.db.withdrawals.update_one({"withdrawal_id": wid}, {"$set": upd})
        w["status"] = status
        return w

    # ══════════════════════════════════════════════════════════════════════
    # BACKUP / RESTORE
    # ══════════════════════════════════════════════════════════════════════
    BACKUP_COLLECTIONS = [
        "users", "admins", "categories", "stock", "orders",
        "transactions", "used_txns", "referrals", "settings", "counters",
        "reseller_requests", "withdrawals",
    ]

    async def export_all(self) -> dict:
        """Dump every collection into a JSON-serializable dict."""
        from bson import ObjectId

        def _clean(doc):
            out = {}
            for k, v in doc.items():
                if k == "_id" and isinstance(v, ObjectId):
                    continue  # drop Mongo _id so restore re-inserts cleanly
                if isinstance(v, datetime):
                    out[k] = {"$date": v.astimezone(timezone.utc).isoformat()}
                elif isinstance(v, ObjectId):
                    out[k] = str(v)
                else:
                    out[k] = v
            return out

        data = {"_meta": {"exported_at": _now().isoformat(),
                          "db": config.MONGO_DB_NAME, "version": 2}}
        for name in self.BACKUP_COLLECTIONS:
            cur = self.db[name].find({})
            data[name] = [_clean(d) async for d in cur]
        return data

    async def import_all(self, data: dict, wipe: bool = True) -> dict:
        """Restore collections from an export_all() dict. Returns counts."""
        def _revive(doc):
            out = {}
            for k, v in doc.items():
                if isinstance(v, dict) and "$date" in v:
                    try:
                        out[k] = datetime.fromisoformat(v["$date"])
                    except Exception:
                        out[k] = _now()
                else:
                    out[k] = v
            return out

        counts = {}
        for name in self.BACKUP_COLLECTIONS:
            rows = data.get(name)
            if rows is None:
                continue
            if wipe:
                await self.db[name].delete_many({})
            docs = [_revive(r) for r in rows]
            if docs:
                # insert in chunks to stay well under request limits
                for i in range(0, len(docs), 500):
                    await self.db[name].insert_many(docs[i:i + 500], ordered=False)
            counts[name] = len(docs)
        await self._ensure_indexes()
        return counts
