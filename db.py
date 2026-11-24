"""
=============================================================================
Project: LectureScribe - 课堂智能双语速记与 AI 智能讲解工作台
Author: Blueberry (@lyang091126-cmd)
Copyright (c) 2026 Blueberry. All rights reserved.
=============================================================================

Durable storage for lecture sessions, user accounts (Free / Pro / Ultra),
and Pro / Ultra redemption codes.

Supports Supabase Postgres via DATABASE_URL and automatically falls back to a
local SQLite file so it still runs on a laptop with zero setup.
"""

import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import JSON, Column, Float, Integer, String, Text, delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

TZ_SG = timezone(timedelta(hours=8))

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

Base = declarative_base()

ULTRA_QUOTA_LIMITS = {
    "overview": 40,       # 40 PDF slide overviews / month
    "slide_summary": 1200,  # 1200 per-page summaries / month
    "qa": 300,            # 300 Ask-TA / full lecture summaries / month
}


class LectureSession(Base):
    __tablename__ = "lecture_sessions"

    id = Column(String(64), primary_key=True)
    # Per-browser client id for guests, or "user:<user_id>" once signed in.
    owner_key = Column(String(128), index=True, nullable=False)
    title = Column(Text, default="")
    source_lang = Column(String(16), default="en")
    target_lang = Column(String(16), default="zh")
    created_at = Column(Float, default=0.0)
    updated_at = Column(Float, default=0.0, index=True)
    duration_seconds = Column(Integer, default=0)
    items = Column(JSON, default=list)
    summary = Column(JSON, nullable=True)


class UserAccount(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True)
    username = Column(String(64), nullable=False)
    username_lower = Column(String(64), unique=True, index=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    plan = Column(String(16), default="free", nullable=False)  # free | pro | ultra
    plan_expires_at = Column(Float, default=0.0, nullable=False)
    quota_month = Column(String(16), default="", nullable=False)
    quota_overview_used = Column(Integer, default=0, nullable=False)
    quota_slide_used = Column(Integer, default=0, nullable=False)
    quota_qa_used = Column(Integer, default=0, nullable=False)
    created_at = Column(Float, default=0.0, nullable=False)


class RedemptionCode(Base):
    __tablename__ = "redemption_codes"

    code = Column(String(64), primary_key=True)
    plan = Column(String(16), nullable=False)  # pro | ultra
    duration_days = Column(Integer, default=30, nullable=False)
    used_by = Column(String(64), nullable=True)
    used_at = Column(Float, nullable=True)
    created_at = Column(Float, default=0.0, nullable=False)


class PaymentClaim(Base):
    """Scan-to-pay instant-access claims reviewed by the author."""
    __tablename__ = "payment_claims"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), index=True, nullable=False)
    username = Column(String(64), nullable=False)
    plan = Column(String(16), nullable=False)  # pro | ultra
    amount_rmb = Column(Float, default=4.9, nullable=False)
    pay_method = Column(String(16), default="wechat", nullable=False)  # wechat | alipay
    receipt_note = Column(String(128), default="", nullable=False)
    status = Column(String(16), default="pending", index=True, nullable=False)  # pending | approved | rejected
    temp_granted = Column(Integer, default=0, nullable=False)
    created_at = Column(Float, default=0.0, index=True, nullable=False)
    reviewed_at = Column(Float, nullable=True)


class UsageEvent(Base):
    """Lightweight privacy-friendly visit & feature usage log for author dashboard."""
    __tablename__ = "usage_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(32), index=True, nullable=False)
    client_id = Column(String(64), index=True, default="", nullable=False)
    ip_addr = Column(String(64), index=True, default="", nullable=False)
    user_id = Column(String(64), nullable=True)
    day_str = Column(String(16), index=True, nullable=False)  # YYYY-MM-DD (UTC+8)
    created_at = Column(Float, default=0.0, index=True, nullable=False)


def _normalize_url(raw: str) -> Tuple[str, Dict[str, Any]]:
    url = raw.strip()
    connect_args: Dict[str, Any] = {}

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+psycopg://"):
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        connect_args["prepare_threshold"] = None
        connect_args["connect_timeout"] = 5

    return url, connect_args


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL:
    _url, _connect_args = _normalize_url(DATABASE_URL)
else:
    _url, _connect_args = f"sqlite+aiosqlite:///{(DATA_DIR / 'lecture.db').as_posix()}", {}

using_postgres = _url.startswith("postgresql")

init_error = ""
engine = None
SessionMaker = None

try:
    engine = create_async_engine(_url, pool_pre_ping=True, connect_args=_connect_args)
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False)
except Exception as e:
    init_error = f"{type(e).__name__}: {e}"


def _require_db():
    if SessionMaker is None:
        raise RuntimeError(init_error or "database is not configured")


async def init_db() -> None:
    _require_db()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _current_month_str(now: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    return dt.strftime("%Y-%m")


def user_to_dict(row: UserAccount, now: Optional[float] = None) -> Dict[str, Any]:
    t = now if now is not None else time.time()
    effective_plan = row.plan or "free"
    expires_at = float(row.plan_expires_at or 0.0)
    if effective_plan in ("pro", "ultra") and expires_at > 0 and expires_at < t:
        effective_plan = "free"

    month = _current_month_str(t)
    same_month = (row.quota_month == month)
    return {
        "id": row.id,
        "username": row.username,
        "plan": effective_plan,
        "raw_plan": row.plan or "free",
        "plan_expires_at": expires_at if effective_plan != "free" else 0.0,
        "quota_month": month,
        "quotas": {
            "overview_used": row.quota_overview_used if same_month else 0,
            "overview_limit": ULTRA_QUOTA_LIMITS["overview"],
            "slide_used": row.quota_slide_used if same_month else 0,
            "slide_limit": ULTRA_QUOTA_LIMITS["slide_summary"],
            "qa_used": row.quota_qa_used if same_month else 0,
            "qa_limit": ULTRA_QUOTA_LIMITS["qa"],
        },
        "created_at": row.created_at or 0.0,
    }


def _pack_summary(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw_summary = payload.get("summary")
    slide_meta = payload.get("slide_meta")
    slide_overview = payload.get("slide_overview")
    slide_summaries = payload.get("slide_summaries")

    has_slides = bool(slide_meta or slide_overview or slide_summaries)
    if not raw_summary and not has_slides:
        return None

    packed: Dict[str, Any] = dict(raw_summary) if isinstance(raw_summary, dict) else {}
    if has_slides:
        packed["_slide_bundle"] = {
            "slide_meta": slide_meta,
            "slide_overview": slide_overview,
            "slide_summaries": slide_summaries or {},
        }
    return packed


def _unpack_summary(stored: Any) -> Tuple[Optional[Dict[str, Any]], Any, Any, Dict[str, Any]]:
    if not isinstance(stored, dict):
        return None, None, None, {}
    copy_s = dict(stored)
    bundle = copy_s.pop("_slide_bundle", None) or {}
    clean_summary = copy_s if copy_s else None
    return (
        clean_summary,
        bundle.get("slide_meta"),
        bundle.get("slide_overview"),
        bundle.get("slide_summaries") or {},
    )


def _to_payload(row: LectureSession) -> Dict[str, Any]:
    clean_summary, slide_meta, slide_overview, slide_summaries = _unpack_summary(row.summary)
    return {
        "id": row.id,
        "title": row.title or "",
        "created_at": row.created_at or 0.0,
        "updated_at": row.updated_at or 0.0,
        "source_lang": row.source_lang or "en",
        "target_lang": row.target_lang or "zh",
        "duration_seconds": row.duration_seconds or 0,
        "items": row.items or [],
        "summary": clean_summary,
        "slide_meta": slide_meta,
        "slide_overview": slide_overview,
        "slide_summaries": slide_summaries,
    }


async def list_sessions(owner_key: str) -> List[Dict[str, Any]]:
    _require_db()
    async with SessionMaker() as db:
        rows = (
            await db.execute(
                select(LectureSession)
                .where(LectureSession.owner_key == owner_key)
                .order_by(LectureSession.updated_at.desc())
            )
        ).scalars().all()

    return [
        {
            "id": row.id,
            "title": row.title or "未命名课堂",
            "created_at": row.created_at,
            "updated_at": row.updated_at or row.created_at,
            "item_count": len(row.items or []),
            "duration_seconds": row.duration_seconds or 0,
        }
        for row in rows
    ]


async def get_session(owner_key: str, session_id: str) -> Optional[Dict[str, Any]]:
    _require_db()
    async with SessionMaker() as db:
        row = await db.get(LectureSession, session_id)
    if row is None or row.owner_key != owner_key:
        return None
    return _to_payload(row)


async def save_session(owner_key: str, session_id: str, payload: Dict[str, Any]) -> None:
    """Raises PermissionError if the id belongs to a different browser/account."""
    _require_db()
    async with SessionMaker() as db:
        row = await db.get(LectureSession, session_id)
        if row is None:
            row = LectureSession(id=session_id, owner_key=owner_key)
            db.add(row)
        elif row.owner_key != owner_key:
            raise PermissionError("not your session")

        row.title = payload.get("title") or ""
        row.source_lang = payload.get("source_lang") or "en"
        row.target_lang = payload.get("target_lang") or "zh"
        row.created_at = float(payload.get("created_at") or 0.0)
        row.updated_at = float(payload.get("updated_at") or 0.0)
        row.duration_seconds = int(payload.get("duration_seconds") or 0)
        row.items = payload.get("items") or []
        row.summary = _pack_summary(payload)
        await db.commit()


async def delete_session(owner_key: str, session_id: str) -> None:
    _require_db()
    async with SessionMaker() as db:
        row = await db.get(LectureSession, session_id)
        if row is None:
            return
        if row.owner_key != owner_key:
            raise PermissionError("not your session")
        await db.delete(row)
        await db.commit()


async def claim_browser_sessions(browser_owner_key: str, user_owner_key: str) -> int:
    """Move guest sessions from this browser under the signed-in account."""
    if not browser_owner_key or not user_owner_key or browser_owner_key == user_owner_key:
        return 0
    _require_db()
    async with SessionMaker() as db:
        res = await db.execute(
            update(LectureSession)
            .where(LectureSession.owner_key == browser_owner_key)
            .values(owner_key=user_owner_key)
        )
        await db.commit()
    return res.rowcount or 0


async def purge_expired(max_age_seconds: int, now: float) -> int:
    _require_db()
    cutoff = now - max_age_seconds
    async with SessionMaker() as db:
        result = await db.execute(delete(LectureSession).where(LectureSession.updated_at < cutoff))
        await db.commit()
    return result.rowcount or 0


# --- User Account & Membership Operations ---

async def create_user(username: str, password_hash: str) -> Dict[str, Any]:
    _require_db()
    clean = username.strip()
    lower = clean.lower()
    now = time.time()
    async with SessionMaker() as db:
        existing = (
            await db.execute(select(UserAccount).where(UserAccount.username_lower == lower))
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError("USERNAME_EXISTS")
        row = UserAccount(
            id="u_" + uuid.uuid4().hex[:16],
            username=clean,
            username_lower=lower,
            password_hash=password_hash,
            plan="free",
            plan_expires_at=0.0,
            quota_month=_current_month_str(now),
            quota_overview_used=0,
            quota_slide_used=0,
            quota_qa_used=0,
            created_at=now,
        )
        db.add(row)
        await db.commit()
        return user_to_dict(row, now)


async def get_user_by_username(username: str) -> Optional[UserAccount]:
    _require_db()
    lower = username.strip().lower()
    async with SessionMaker() as db:
        return (
            await db.execute(select(UserAccount).where(UserAccount.username_lower == lower))
        ).scalar_one_or_none()


async def get_user_by_id(user_id: str) -> Optional[UserAccount]:
    _require_db()
    async with SessionMaker() as db:
        return await db.get(UserAccount, user_id)


async def create_redemption_codes(plan: str, count: int = 1, duration_days: int = 30) -> List[str]:
    _require_db()
    plan_clean = plan.strip().lower()
    if plan_clean not in ("pro", "ultra"):
        raise ValueError("plan must be pro or ultra")
    prefix = "LSPRO" if plan_clean == "pro" else "LSULT"
    now = time.time()
    created: List[str] = []
    async with SessionMaker() as db:
        for _ in range(max(1, min(count, 100))):
            token = secrets.token_hex(4).upper()
            code = f"{prefix}-{token[:4]}-{token[4:]}"
            db.add(
                RedemptionCode(
                    code=code,
                    plan=plan_clean,
                    duration_days=max(1, int(duration_days)),
                    created_at=now,
                )
            )
            created.append(code)
        await db.commit()
    return created


async def redeem_code(user_id: str, raw_code: str) -> Tuple[Dict[str, Any], str, int]:
    """Redeem a Pro or Ultra activation code for a user."""
    _require_db()
    code_norm = raw_code.strip().upper()
    now = time.time()
    async with SessionMaker() as db:
        code_row = await db.get(RedemptionCode, code_norm)
        if code_row is None:
            raise LookupError("激活码不存在，请检查是否输入有误")
        if code_row.used_by:
            raise ValueError("该激活码已被使用")
        user = await db.get(UserAccount, user_id)
        if user is None:
            raise LookupError("账号不存在")

        target_plan = code_row.plan
        days = int(code_row.duration_days or 30)
        base_time = now
        # Stack remaining time when renewing the same tier or upgrading
        if user.plan_expires_at and user.plan_expires_at > now:
            if user.plan == target_plan or (user.plan == "ultra" and target_plan == "ultra"):
                base_time = user.plan_expires_at

        user.plan = target_plan
        user.plan_expires_at = base_time + days * 86400
        code_row.used_by = user.id
        code_row.used_at = now
        await db.commit()
        return user_to_dict(user, now), target_plan, days


async def consume_ultra_quota(user_id: str, bucket: str) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Check and increment an Ultra user's monthly built-in AI quota."""
    _require_db()
    limit = ULTRA_QUOTA_LIMITS.get(bucket, 0)
    now = time.time()
    month = _current_month_str(now)
    async with SessionMaker() as db:
        user = await db.get(UserAccount, user_id)
        if user is None:
            return False, None
        info = user_to_dict(user, now)
        if info["plan"] != "ultra":
            return False, info

        if user.quota_month != month:
            user.quota_month = month
            user.quota_overview_used = 0
            user.quota_slide_used = 0
            user.quota_qa_used = 0

        col_map = {
            "overview": "quota_overview_used",
            "slide_summary": "quota_slide_used",
            "qa": "quota_qa_used",
        }
        col = col_map.get(bucket)
        if not col:
            return False, user_to_dict(user, now)

        used = getattr(user, col)
        if used >= limit:
            return False, user_to_dict(user, now)

        setattr(user, col, used + 1)
        await db.commit()
        return True, user_to_dict(user, now)


# --- Payment Claims (Instant 48h Access + Author Review) ---

def _day_str_sg(ts: Optional[float] = None) -> str:
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=TZ_SG)
    return dt.strftime("%Y-%m-%d")


async def submit_payment_claim(
    user_id: str,
    plan: str,
    pay_method: str,
    receipt_note: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
    """Record a user's WeChat/Alipay payment claim. Grants immediate 48h access
    for clean accounts (preventing repeated unapproved trial farming)."""
    _require_db()
    plan_clean = plan.strip().lower()
    if plan_clean not in ("pro", "ultra"):
        raise ValueError("套餐类型无效")
    method_clean = pay_method.strip().lower()
    if method_clean not in ("wechat", "alipay"):
        method_clean = "wechat"
    note_clean = (receipt_note or "").strip()[:120]
    amount = 8.9 if plan_clean == "ultra" else 4.9
    now = time.time()

    async with SessionMaker() as db:
        user = await db.get(UserAccount, user_id)
        if user is None:
            raise LookupError("账号不存在")

        prev_claims = (
            await db.execute(
                select(PaymentClaim)
                .where(PaymentClaim.user_id == user_id)
                .order_by(PaymentClaim.created_at.desc())
            )
        ).scalars().all()

        if any(c.status == "pending" for c in prev_claims):
            raise ValueError("你已有一笔待核对的付款申请，无需重复提交")

        # Anti-abuse: grant instant 48h access unless user previously had a rejected claim
        # or already used a temp grant that wasn't approved.
        has_bad_history = any(c.status == "rejected" for c in prev_claims)
        grant_temp = not has_bad_history

        claim = PaymentClaim(
            id="pay_" + uuid.uuid4().hex[:14],
            user_id=user.id,
            username=user.username,
            plan=plan_clean,
            amount_rmb=amount,
            pay_method=method_clean,
            receipt_note=note_clean,
            status="pending",
            temp_granted=1 if grant_temp else 0,
            created_at=now,
        )
        db.add(claim)

        if grant_temp:
            # Give immediate 48h access (don't shorten if they already have longer validity)
            temp_exp = now + 48 * 3600
            if user.plan == "free" or not user.plan_expires_at or user.plan_expires_at < temp_exp:
                user.plan = plan_clean
                user.plan_expires_at = temp_exp
            elif user.plan == "pro" and plan_clean == "ultra":
                user.plan = "ultra"
                user.plan_expires_at = max(user.plan_expires_at, temp_exp)

        await db.commit()
        claim_dict = {
            "id": claim.id,
            "user_id": claim.user_id,
            "username": claim.username,
            "plan": claim.plan,
            "amount_rmb": claim.amount_rmb,
            "pay_method": claim.pay_method,
            "receipt_note": claim.receipt_note,
            "status": claim.status,
            "temp_granted": bool(claim.temp_granted),
            "created_at": claim.created_at,
        }
        return claim_dict, user_to_dict(user, now), grant_temp


async def review_payment_claim(claim_id: str, action: str, duration_days: int = 30) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Approve (upgrade to full 30 days) or Reject (revoke temp access) a payment claim."""
    _require_db()
    act = action.strip().lower()
    if act not in ("approve", "reject"):
        raise ValueError("action must be approve or reject")
    now = time.time()

    async with SessionMaker() as db:
        claim = await db.get(PaymentClaim, claim_id)
        if claim is None:
            raise LookupError("付款记录不存在")
        user = await db.get(UserAccount, claim.user_id)

        claim.reviewed_at = now
        if act == "approve":
            claim.status = "approved"
            if user is not None:
                # Base from now (replacing the 48h temp window cleanly with full 30 days)
                base_time = now
                if user.plan_expires_at and user.plan_expires_at > now + 49 * 3600 and user.plan == claim.plan:
                    base_time = user.plan_expires_at
                user.plan = claim.plan
                user.plan_expires_at = base_time + max(1, int(duration_days)) * 86400
        else:
            claim.status = "rejected"
            if user is not None and claim.temp_granted:
                # Revoke temporary access if it wasn't extended by a paid renewal
                if user.plan_expires_at and user.plan_expires_at <= claim.created_at + 49 * 3600:
                    user.plan = "free"
                    user.plan_expires_at = 0.0

        await db.commit()
        return (
            {
                "id": claim.id,
                "status": claim.status,
                "plan": claim.plan,
                "username": claim.username,
                "reviewed_at": claim.reviewed_at,
            },
            user_to_dict(user, now) if user is not None else None,
        )


async def set_user_plan_manual(user_id: str, plan: str, duration_days: int = 30) -> Dict[str, Any]:
    """Allow author to manually grant/adjust any user's plan (e.g., gifting friends)."""
    _require_db()
    plan_clean = plan.strip().lower()
    if plan_clean not in ("free", "pro", "ultra"):
        raise ValueError("plan must be free, pro, or ultra")
    now = time.time()
    async with SessionMaker() as db:
        user = await db.get(UserAccount, user_id)
        if user is None:
            raise LookupError("用户不存在")
        user.plan = plan_clean
        user.plan_expires_at = 0.0 if plan_clean == "free" else (now + max(1, int(duration_days)) * 86400)
        await db.commit()
        return user_to_dict(user, now)


# --- Lightweight Usage & Visit Analytics ---

async def record_usage_event(
    event_type: str,
    client_id: str = "",
    ip_addr: str = "",
    user_id: Optional[str] = None,
) -> None:
    if SessionMaker is None:
        return
    now = time.time()
    try:
        async with SessionMaker() as db:
            db.add(
                UsageEvent(
                    event_type=(event_type or "unknown")[:32],
                    client_id=(client_id or "")[:64],
                    ip_addr=(ip_addr or "")[:64],
                    user_id=user_id,
                    day_str=_day_str_sg(now),
                    created_at=now,
                )
            )
            await db.commit()
    except Exception:
        pass


async def get_admin_overview() -> Dict[str, Any]:
    """Aggregate real IP visits, active tool users, registrations, paid members,
    7-day daily trends, pending payment claims, recent users, and unused codes."""
    _require_db()
    now = time.time()
    today_str = _day_str_sg(now)
    days_7 = [_day_str_sg(now - i * 86400) for i in range(6, -1, -1)]
    cutoff_7d = now - 7 * 86400
    today_dt = datetime.fromtimestamp(now, tz=TZ_SG).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_ts = today_dt.timestamp()

    async with SessionMaker() as db:
        events = (await db.execute(select(UsageEvent))).scalars().all()
        users = (await db.execute(select(UserAccount).order_by(UserAccount.created_at.desc()))).scalars().all()
        claims = (await db.execute(select(PaymentClaim).order_by(PaymentClaim.created_at.desc()))).scalars().all()
        codes = (
            await db.execute(
                select(RedemptionCode)
                .where(RedemptionCode.used_by.is_(None))
                .order_by(RedemptionCode.created_at.desc())
                .limit(30)
            )
        ).scalars().all()

    active_event_types = {"translate_use", "pdf_upload", "ask_ta", "export_use"}

    # Unique IP & active usage counters
    ips_all, ips_7d, ips_today = set(), set(), set()
    active_all, active_7d, active_today = set(), set(), set()
    pv_landing_all = pv_app_all = pv_today = 0
    pdf_uploads_all = translates_all = 0

    daily_map: Dict[str, Dict[str, Any]] = {
        d: {"day": d, "ips": set(), "active": set(), "pv": 0, "registrations": 0}
        for d in days_7
    }

    for ev in events:
        ip = ev.ip_addr or ev.client_id
        actor = ev.client_id or ev.ip_addr
        is_today = (ev.day_str == today_str)
        is_7d = (ev.created_at >= cutoff_7d)

        if ip:
            ips_all.add(ip)
            if is_7d:
                ips_7d.add(ip)
            if is_today:
                ips_today.add(ip)

        if ev.event_type == "page_landing":
            pv_landing_all += 1
            if is_today:
                pv_today += 1
        elif ev.event_type == "page_app":
            pv_app_all += 1
            if is_today:
                pv_today += 1
        elif ev.event_type == "pdf_upload":
            pdf_uploads_all += 1
        elif ev.event_type == "translate_use":
            translates_all += 1

        if ev.event_type in active_event_types and actor:
            active_all.add(actor)
            if is_7d:
                active_7d.add(actor)
            if is_today:
                active_today.add(actor)

        if ev.day_str in daily_map:
            bucket = daily_map[ev.day_str]
            if ip:
                bucket["ips"].add(ip)
            if ev.event_type in ("page_landing", "page_app"):
                bucket["pv"] += 1
            if ev.event_type in active_event_types and actor:
                bucket["active"].add(actor)

    # Users & membership stats
    pro_active = 0
    ultra_active = 0
    reg_today = 0
    user_list = []
    for u in users:
        info = user_to_dict(u, now)
        if info["plan"] == "pro":
            pro_active += 1
        elif info["plan"] == "ultra":
            ultra_active += 1
        if (u.created_at or 0) >= today_start_ts:
            reg_today += 1
        u_day = _day_str_sg(u.created_at) if u.created_at else ""
        if u_day in daily_map:
            daily_map[u_day]["registrations"] += 1
        user_list.append(info)

    # Claims & revenue
    pending_claims = []
    recent_claims = []
    confirmed_revenue = 0.0
    for c in claims:
        item = {
            "id": c.id,
            "user_id": c.user_id,
            "username": c.username,
            "plan": c.plan,
            "amount_rmb": c.amount_rmb,
            "pay_method": c.pay_method,
            "receipt_note": c.receipt_note,
            "status": c.status,
            "temp_granted": bool(c.temp_granted),
            "created_at": c.created_at,
            "reviewed_at": c.reviewed_at,
        }
        if c.status == "pending":
            pending_claims.append(item)
        if c.status == "approved":
            confirmed_revenue += float(c.amount_rmb or 0.0)
        if len(recent_claims) < 40:
            recent_claims.append(item)

    daily_trend = [
        {
            "day": d,
            "label": d[5:],
            "unique_ips": len(daily_map[d]["ips"]),
            "active_users": len(daily_map[d]["active"]),
            "page_views": daily_map[d]["pv"],
            "registrations": daily_map[d]["registrations"],
        }
        for d in days_7
    ]

    return {
        "generated_at": now,
        "today": today_str,
        "kpis": {
            "unique_ips_today": len(ips_today),
            "unique_ips_7d": len(ips_7d),
            "unique_ips_total": len(ips_all),
            "page_views_today": pv_today,
            "page_views_landing": pv_landing_all,
            "page_views_app": pv_app_all,
            "active_users_today": len(active_today),
            "active_users_7d": len(active_7d),
            "active_users_total": len(active_all),
            "pdf_uploads_total": pdf_uploads_all,
            "translates_total": translates_all,
            "registered_total": len(users),
            "registered_today": reg_today,
            "pro_active": pro_active,
            "ultra_active": ultra_active,
            "pending_claims_count": len(pending_claims),
            "confirmed_revenue_rmb": round(confirmed_revenue, 2),
        },
        "daily_trend": daily_trend,
        "pending_claims": pending_claims,
        "recent_claims": recent_claims,
        "recent_users": user_list[:50],
        "unused_codes": [
            {"code": r.code, "plan": r.plan, "duration_days": r.duration_days, "created_at": r.created_at}
            for r in codes
        ],
    }
