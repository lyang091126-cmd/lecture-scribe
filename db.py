"""
=============================================================================
Project: LectureScribe - 课堂智能双语速记与 AI 智能讲解工作台
Author: Blueberry (@lyang091126-cmd)
Copyright (c) 2026 Blueberry. All rights reserved.
=============================================================================

Durable storage for lecture sessions.

Render's free instances have an ephemeral filesystem: anything written to disk
is wiped on every deploy, restart and 15-minute idle spin-down, so the cloud
history quietly emptied itself. Sessions live in Postgres (Supabase) instead,
configured through DATABASE_URL. Without that variable the app falls back to a
local SQLite file so it still runs on a laptop with nothing to set up.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import JSON, Column, Float, Integer, String, Text, delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

Base = declarative_base()


class LectureSession(Base):
    __tablename__ = "lecture_sessions"

    id = Column(String(64), primary_key=True)
    # Whose lecture this is. Today that's the per-browser client id; once
    # email login lands it becomes the user id, and a browser's sessions are
    # claimed by the account that logs in on it.
    owner_key = Column(String(128), index=True, nullable=False)
    title = Column(Text, default="")
    source_lang = Column(String(16), default="en")
    target_lang = Column(String(16), default="zh")
    created_at = Column(Float, default=0.0)
    updated_at = Column(Float, default=0.0, index=True)
    duration_seconds = Column(Integer, default=0)
    items = Column(JSON, default=list)
    summary = Column(JSON, nullable=True)


def _normalize_url(raw: str) -> Tuple[str, Dict[str, Any]]:
    """Supabase hands out a libpq URL, but SQLAlchemy needs the asyncpg driver
    and asyncpg takes TLS as a connect argument instead of a query parameter."""
    url = raw.strip()
    connect_args: Dict[str, Any] = {}

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if "sslmode=" in url:
        url = re.sub(r"[?&]sslmode=[^&]*", "", url).rstrip("?&")
    if url.startswith("postgresql+asyncpg://"):
        connect_args["ssl"] = True
        # Supabase's direct host is IPv6-only, so Render has to go through the
        # connection pooler -- and pgbouncer rejects the prepared statements
        # asyncpg caches by default.
        if "pooler.supabase.com" in url or ":6543/" in url:
            connect_args["statement_cache_size"] = 0
            connect_args["prepared_statement_cache_size"] = 0

    return url, connect_args


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL:
    _url, _connect_args = _normalize_url(DATABASE_URL)
else:
    _url, _connect_args = f"sqlite+aiosqlite:///{(DATA_DIR / 'lecture.db').as_posix()}", {}

using_postgres = _url.startswith("postgresql")
engine = create_async_engine(_url, pool_pre_ping=True, connect_args=_connect_args)
SessionMaker = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _to_payload(row: LectureSession) -> Dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title or "",
        "created_at": row.created_at or 0.0,
        "updated_at": row.updated_at or 0.0,
        "source_lang": row.source_lang or "en",
        "target_lang": row.target_lang or "zh",
        "duration_seconds": row.duration_seconds or 0,
        "items": row.items or [],
        "summary": row.summary,
    }


async def list_sessions(owner_key: str) -> List[Dict[str, Any]]:
    async with SessionMaker() as db:
        rows = (
            await db.execute(
                select(LectureSession)
                .where(LectureSession.owner_key == owner_key)
                .order_by(LectureSession.created_at.desc())
            )
        ).scalars().all()

    return [
        {
            "id": row.id,
            "title": row.title or "未命名课堂",
            "created_at": row.created_at,
            "item_count": len(row.items or []),
            "duration_seconds": row.duration_seconds or 0,
        }
        for row in rows
    ]


async def get_session(owner_key: str, session_id: str) -> Optional[Dict[str, Any]]:
    async with SessionMaker() as db:
        row = await db.get(LectureSession, session_id)
    if row is None or row.owner_key != owner_key:
        return None
    return _to_payload(row)


async def save_session(owner_key: str, session_id: str, payload: Dict[str, Any]) -> None:
    """Raises PermissionError if the id belongs to a different browser/account."""
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
        row.summary = payload.get("summary")
        await db.commit()


async def delete_session(owner_key: str, session_id: str) -> None:
    async with SessionMaker() as db:
        row = await db.get(LectureSession, session_id)
        if row is None:
            return
        if row.owner_key != owner_key:
            raise PermissionError("not your session")
        await db.delete(row)
        await db.commit()


async def purge_expired(max_age_seconds: int, now: float) -> int:
    cutoff = now - max_age_seconds
    async with SessionMaker() as db:
        result = await db.execute(delete(LectureSession).where(LectureSession.updated_at < cutoff))
        await db.commit()
    return result.rowcount or 0
