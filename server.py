"""
=============================================================================
Project: LectureScribe - 课堂智能双语速记与 AI 智能讲解工作台
Author: Blueberry (@lyang091126-cmd)
GitHub: https://github.com/lyang091126-cmd/lecture-scribe
Copyright (c) 2026 Blueberry. All rights reserved.

[版权与防剽窃严正声明 / Anti-Plagiarism Notice]
本项目代码由作者独立设计、架构与编写，享有全部原创著作权。
严禁在未获原作者许可的情况下进行商业倒卖、闭源包装转售、恶意抄袭或抹除原作者署名！
如需二次开发或技术引用，请保留原作者署名与 GitHub 开源仓库地址。
=============================================================================
"""

import os
import re
import json
import time
import io
import uuid
import hmac
import hashlib
import base64
import secrets
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel
import httpx
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

import db

BASE_DIR = Path(__file__).resolve().parent
env_file = BASE_DIR / ".env"
if env_file.exists():
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass

# --- Security: strict session/client id validation ---
SAFE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')

# Lecture notes are a study aid for the current week, not an archive: drop
# older ones so the database and everyone's history list stay small.
SESSION_RETENTION_SECONDS = 7 * 24 * 60 * 60
_last_session_purge = [0.0]

async def maybe_purge_expired_sessions():
    now = time.time()
    if now - _last_session_purge[0] < 3600 or now < _storage_down_until[0]:
        return
    _last_session_purge[0] = now
    try:
        removed = await db.purge_expired(SESSION_RETENTION_SECONDS, now)
        if removed:
            print(f"[Sessions] purged {removed} lecture(s) older than 7 days")
    except Exception as e:
        print(f"[Sessions] purge failed: {e}")

MAX_SESSION_ITEMS = 5000

def get_client_id(request: Request) -> str:
    """Reads a per-browser client id from the X-Client-Id header. This is
    NOT a real auth system, just isolation between anonymous browser
    sessions so users can't list/read/delete each other's lecture notes."""
    cid = request.headers.get("x-client-id", "").strip()
    if not cid or not SAFE_ID_RE.match(cid):
        raise HTTPException(status_code=400, detail="Missing or invalid X-Client-Id header")
    return cid

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # A database that is merely unreachable would otherwise hang startup
        # until the platform gives up and marks the whole deploy failed.
        await asyncio.wait_for(db.init_db(), timeout=20)
        print("[Storage] Postgres" if db.using_postgres
              else "[Storage] local SQLite — set DATABASE_URL for storage that survives restarts")
    except Exception as e:
        # Recording, translation and subtitles don't need the database, so a
        # bad DATABASE_URL must not stop the whole service from starting.
        db.init_error = f"{type(e).__name__}: {e}"
        print(f"[Storage] unavailable ({db.init_error}); lecture history is off, everything else still works")
    yield

app = FastAPI(title="Anti Lecture - 课堂智能双语速记与课件同步工作台", lifespan=lifespan)

# --- Security: CORS. Wildcard origin + credentials is an invalid/unsafe
# combination; we only ever read a custom header (no cookies), so
# allow_credentials is off and origins are wide open for a public API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "X-Client-Id", "Authorization", "X-Admin-Secret"],
)

# --- Lightweight Username + Password Auth & 3-Tier Membership (Free / Pro / Ultra) ---
AUTH_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60
_AUTH_SECRET = (
    os.environ.get("LECTURE_AUTH_SECRET")
    or os.environ.get("DATABASE_URL")
    or f"lecturescribe-local-secret-{BASE_DIR}"
).encode("utf-8")

USERNAME_RE = re.compile(r'^[\w\u4e00-\u9fa5\-]{2,24}$')


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, hex_dk = stored.split("$", 2)
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
        return hmac.compare_digest(dk.hex(), hex_dk)
    except Exception:
        return False


def issue_auth_token(user_id: str) -> str:
    exp = int(time.time()) + AUTH_TOKEN_TTL_SECONDS
    payload = f"{user_id}.{exp}".encode("utf-8")
    sig = hmac.new(_AUTH_SECRET, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=") + "." + base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")


def verify_auth_token(token: str) -> Optional[str]:
    try:
        p_b64, s_b64 = token.strip().split(".", 1)
        p_pad = p_b64 + "=" * (-len(p_b64) % 4)
        s_pad = s_b64 + "=" * (-len(s_b64) % 4)
        payload = base64.urlsafe_b64decode(p_pad.encode("ascii"))
        sig = base64.urlsafe_b64decode(s_pad.encode("ascii"))
        expected = hmac.new(_AUTH_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expected):
            return None
        user_id, exp_str = payload.decode("utf-8").rsplit(".", 1)
        if int(exp_str) < int(time.time()):
            return None
        return user_id
    except Exception:
        return None


async def get_optional_user(request: Request) -> Optional[Dict[str, Any]]:
    auth = request.headers.get("authorization", "").strip()
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    user_id = verify_auth_token(token)
    if not user_id:
        return None
    try:
        row = await db.get_user_by_id(user_id)
        if row is None:
            return None
        return db.user_to_dict(row)
    except Exception:
        return None


async def get_owner_key(request: Request) -> str:
    user = await get_optional_user(request)
    if user:
        return f"user:{user['id']}"
    return get_client_id(request)


def resolve_provider_and_model(
    api_key: str,
    provider: Optional[str],
    model_name: Optional[str],
    custom_endpoint: Optional[str],
) -> tuple[str, str]:
    """Allow blank model_name and auto-detect provider/default model cleanly."""
    key = (api_key or "").strip()
    prov = (provider or "").strip().lower()
    ep = (custom_endpoint or "").strip().lower()
    model = (model_name or "").strip()

    if not prov or prov == "free_google":
        prov = "gemini" if key.startswith("AIza") else "openai_compatible"
    elif prov == "gemini" and key and not key.startswith("AIza") and ep:
        prov = "openai_compatible"
    elif prov in ("openai_compatible", "deepseek") and key.startswith("AIza") and not ep:
        prov = "gemini"

    if not model:
        if prov == "gemini":
            model = "gemini-2.5-flash"
        elif "deepseek" in ep or prov == "deepseek":
            model = "deepseek-chat"
        elif "dashscope" in ep or "aliyuncs" in ep:
            model = "qwen-plus"
        elif "siliconflow" in ep:
            model = "Qwen/Qwen2.5-72B-Instruct"
        elif "moonshot" in ep:
            model = "moonshot-v1-8k"
        elif "openrouter" in ep:
            model = "google/gemini-2.5-flash"
        else:
            model = "gpt-4o-mini"
    return prov, model


async def resolve_ai_credentials(
    request: Request,
    req_api_key: Optional[str],
    req_provider: Optional[str],
    req_model: Optional[str],
    req_endpoint: Optional[str],
    quota_bucket: str,
) -> Dict[str, Any]:
    """Resolve whether to use visitor's own key, Ultra built-in server quota,
    or fall back cleanly. Never spends server keys on anonymous/Free/Pro users."""
    user_key = (req_api_key or "").strip()
    if user_key:
        prov, model = resolve_provider_and_model(user_key, req_provider, req_model, req_endpoint)
        return {
            "api_key": user_key,
            "provider": prov,
            "model_name": model,
            "custom_endpoint": (req_endpoint or "").strip(),
            "via_ultra_quota": False,
            "quota_notice": "",
        }

    # No custom key: ONLY active Ultra members with remaining monthly quota can use built-in model
    user = await get_optional_user(request)
    server_key = (os.environ.get("ULTRA_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()
    if user and user.get("plan") == "ultra" and server_key:
        allowed, _ = await db.consume_ultra_quota(user["id"], quota_bucket)
        if allowed:
            ultra_provider = (os.environ.get("ULTRA_PROVIDER") or ("gemini" if server_key.startswith("AIza") else "openai_compatible")).strip()
            ultra_model = (os.environ.get("ULTRA_MODEL") or ("gemini-2.5-flash-lite" if ultra_provider == "gemini" else "deepseek-chat")).strip()
            ultra_ep = (os.environ.get("ULTRA_ENDPOINT") or "").strip()
            return {
                "api_key": server_key,
                "provider": ultra_provider,
                "model_name": ultra_model,
                "custom_endpoint": ultra_ep,
                "via_ultra_quota": True,
                "quota_notice": "",
            }
        return {
            "api_key": "",
            "provider": "",
            "model_name": "",
            "custom_endpoint": "",
            "via_ultra_quota": False,
            "quota_notice": "本月 Ultra 内置额度已用完，可在设置中填入个人 API Key 继续使用。",
        }

    return {
        "api_key": "",
        "provider": "",
        "model_name": "",
        "custom_endpoint": "",
        "via_ultra_quota": False,
        "quota_notice": "",
    }

# --- Security: small in-memory rate limiter. Not distributed-safe (fine for
# a single Render instance). The budget is per browser (X-Client-Id) because
# a whole class on campus Wi-Fi shares one public IP; the much larger per-IP
# budget only stops a single caller from rotating fake client ids.
_rate_buckets: Dict[str, List[float]] = {}
_rate_bucket_last_sweep = [0.0]
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_PER_CLIENT = 120
RATE_LIMIT_PER_IP = 1200

def _take_rate_token(key: str, limit: int, now: float) -> bool:
    bucket = _rate_buckets.setdefault(key, [])
    bucket[:] = [t for t in bucket if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    return True

def get_client_ip(request: Request) -> str:
    """Best-effort real visitor IP. Render (like most PaaS reverse proxies)
    terminates TLS at its edge and forwards the original client IP via
    X-Forwarded-For; without reading it, request.client.host would be the
    proxy's own address and every visitor would collapse into one shared
    rate-limit bucket."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"

def check_rate_limit(request: Request):
    ip = get_client_ip(request)
    now = time.time()

    # Periodically sweep out IPs with no recent activity so this dict
    # doesn't grow forever as more distinct visitors show up over time.
    if now - _rate_bucket_last_sweep[0] > RATE_LIMIT_WINDOW_SECONDS:
        _rate_bucket_last_sweep[0] = now
        for k in list(_rate_buckets.keys()):
            _rate_buckets[k] = [t for t in _rate_buckets[k] if now - t < RATE_LIMIT_WINDOW_SECONDS]
            if not _rate_buckets[k]:
                del _rate_buckets[k]

    cid = request.headers.get("x-client-id", "").strip()
    client_key = f"cid:{cid}" if SAFE_ID_RE.match(cid) else f"ip-client:{ip}"
    if not (_take_rate_token(f"ip:{ip}", RATE_LIMIT_PER_IP, now)
            and _take_rate_token(client_key, RATE_LIMIT_PER_CLIENT, now)):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试 (Too many requests)")

FILLER_PATTERNS_EN = [
    r'\b(um+h?|uh+h?|er+h?|ah+h?)\b',
    r'\b(you know|like I said|sort of|kind of|basically|actually|literally)\b',
    r'\b(I mean|as it were|you see|if you will)\b',
]
FILLER_PATTERNS_ZH = [
    r'(那个[，\s]*)+',
    r'(就是说[，\s]*)+',
    r'(然后[，\s]*然后[，\s]*)+',
    r'(嗯+[，\s]*)+',
    r'(呃+[，\s]*)+',
    r'(对吧[，\s]*)+',
]

def clean_filler_words(text: str, lang: str = "en") -> str:
    cleaned = text
    if "en" in lang.lower():
        for pat in FILLER_PATTERNS_EN:
            cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE)
    else:
        for pat in FILLER_PATTERNS_ZH:
            cleaned = re.sub(pat, '', cleaned)
    cleaned = re.sub(r'\s{2,}', ' ', cleaned).strip()
    cleaned = re.sub(r'^[,\.;\s]+', '', cleaned)
    return cleaned

def extract_heading_signal(text: str) -> Optional[str]:
    signals = [
        (r'^(now|next|moving on to|let\'s look at|firstly|secondly|finally|another key concept is)\s+([^\.,;\n]{3,40})', 2),
        (r'^(首先|其次|接下来|下面我们看|另外一个重点是|总结一下|关于)\s*([^\n，。]{2,20})', 2),
    ]
    for pattern, group_idx in signals:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            heading = match.group(group_idx).strip().capitalize()
            return heading
    return None

def sanitize_gemini_model(model_name: Optional[str]) -> str:
    """Ensures Gemini model name strictly conforms to current Google API specs (gemini-2.5-flash / 2.0-flash / 1.5-flash)."""
    if not model_name:
        return "gemini-2.5-flash"
    m = model_name.strip()
    m = re.sub(r'^models/', '', m)
    m_lower = m.lower().replace(" ", "-")
    if "2.5-pro" in m_lower:
        return "gemini-2.5-pro"
    elif "2.5" in m_lower:
        return "gemini-2.5-flash"
    elif "2.0-flash-lite" in m_lower or "flash-lite" in m_lower:
        return "gemini-2.0-flash-lite"
    elif "2.0" in m_lower:
        return "gemini-2.0-flash"
    elif "1.5-pro" in m_lower:
        return "gemini-1.5-pro"
    elif "1.5" in m_lower:
        return "gemini-1.5-flash"
    elif "3.8" in m_lower:
        return "gemini-3.8-flash"
    elif "3.7" in m_lower:
        return "gemini-3.7-flash"
    elif "3.6" in m_lower:
        return "gemini-3.6-flash"
    clean_m = re.sub(r'[^a-zA-Z0-9\._-]', '', m)
    return clean_m or "gemini-2.5-flash"

class TranslateRequest(BaseModel):
    text: str
    source_lang: str = "en"
    target_lang: str = "zh"
    context: Optional[str] = ""
    provider: Optional[str] = "gemini"
    api_key: Optional[str] = ""
    custom_endpoint: Optional[str] = ""
    model_name: Optional[str] = "gemini-3.8-flash"

class AskCardRequest(BaseModel):
    card_source: str
    card_translation: str
    question: Optional[str] = ""
    provider: Optional[str] = "gemini"
    api_key: Optional[str] = ""
    custom_endpoint: Optional[str] = ""
    model_name: Optional[str] = "gemini-3.8-flash"

class SummarizeRequest(BaseModel):
    session_title: str
    items: List[Dict[str, Any]]
    provider: Optional[str] = "gemini"
    api_key: Optional[str] = ""
    custom_endpoint: Optional[str] = ""
    model_name: Optional[str] = "gemini-3.8-flash"

class SlideOverviewRequest(BaseModel):
    file_name: Optional[str] = ""
    total_pages: int = 0
    pages: List[Dict[str, Any]] = []
    provider: Optional[str] = "gemini"
    api_key: Optional[str] = ""
    custom_endpoint: Optional[str] = ""
    model_name: Optional[str] = "gemini-2.5-flash"

class SlidePageSummaryRequest(BaseModel):
    page: int
    total_pages: int = 0
    slide_title: Optional[str] = ""
    slide_text: Optional[str] = ""
    spoken_items: List[Dict[str, Any]] = []
    course_title: Optional[str] = ""
    provider: Optional[str] = "gemini"
    api_key: Optional[str] = ""
    custom_endpoint: Optional[str] = ""
    model_name: Optional[str] = "gemini-2.5-flash"

class SessionData(BaseModel):
    id: str
    title: str
    created_at: float
    updated_at: float
    source_lang: str
    target_lang: str
    duration_seconds: int = 0
    items: List[Dict[str, Any]] = []
    summary: Optional[Dict[str, Any]] = None
    slide_meta: Optional[Dict[str, Any]] = None
    slide_overview: Optional[Dict[str, Any]] = None
    slide_summaries: Optional[Dict[str, Any]] = None
    client_id: Optional[str] = None

# --- Security: SSRF guard. custom_endpoint is attacker-controlled input
# (any visitor's browser can set it), and the server would otherwise
# happily POST to whatever URL it's given, with the caller's API key
# attached as a Bearer token. Restrict to a small allowlist of known
# LLM-provider hosts, and always reject private/internal/metadata IP
# ranges outright.
ALLOWED_LLM_HOST_SUFFIXES = (
    "api.openai.com",
    "api.deepseek.com",
    "generativelanguage.googleapis.com",
    "openrouter.ai",
    "api.siliconflow.cn",
    "api.moonshot.cn",
)

def validate_custom_endpoint(raw_endpoint: str) -> str:
    from urllib.parse import urlparse
    import ipaddress
    import socket

    endpoint = (raw_endpoint or "").strip().rstrip("/") or "https://api.openai.com/v1"
    parsed = urlparse(endpoint)

    if parsed.scheme != "https":
        raise HTTPException(status_code=400, detail="自定义接口必须使用 https")

    host = (parsed.hostname or "").lower()
    if not any(host == suf or host.endswith("." + suf) for suf in ALLOWED_LLM_HOST_SUFFIXES):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的自定义接口域名: {host}。出于安全考虑，仅支持: {', '.join(ALLOWED_LLM_HOST_SUFFIXES)}"
        )

    # Belt-and-suspenders: block anything that resolves to a private /
    # loopback / link-local address (e.g. cloud metadata endpoints),
    # even if it somehow matched the suffix list above.
    try:
        infos = socket.getaddrinfo(host, None)
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                raise HTTPException(status_code=400, detail="自定义接口解析到内网地址，已拒绝")
    except HTTPException:
        raise
    except Exception:
        pass  # DNS resolution issues are surfaced naturally by the actual request later

    return endpoint

ENGLISH_STOP_WORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "aren't",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by",
    "can", "can't", "cannot", "could", "couldn't", "did", "didn't", "do", "does", "doesn't", "doing",
    "don't", "down", "during", "each", "few", "for", "from", "further", "had", "hadn't", "has", "hasn't",
    "have", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here", "here's", "hers", "herself",
    "him", "himself", "his", "how", "how's", "i", "i'd", "i'll", "i'm", "i've", "if", "in", "into", "is",
    "isn't", "it", "it's", "its", "itself", "let", "let's", "me", "more", "most", "mustn't", "my", "myself",
    "no", "nor", "not", "of", "off", "on", "once", "only", "or", "other", "ought", "our", "ours", "ourselves",
    "out", "over", "own", "same", "shan't", "she", "she'd", "she'll", "she's", "should", "shouldn't", "so",
    "some", "such", "than", "that", "that's", "the", "their", "theirs", "them", "themselves", "then", "there",
    "there's", "these", "they", "they'd", "they'll", "they're", "they've", "this", "those", "through", "to",
    "too", "under", "until", "up", "us", "very", "was", "wasn't", "we", "we'd", "we'll", "we're", "we've", "were",
    "weren't", "what", "what's", "when", "when's", "where", "where's", "which", "while", "who", "who's",
    "whom", "why", "why's", "with", "won't", "would", "wouldn't", "you", "you'd", "you'll", "you're", "you've",
    "your", "yours", "yourself", "yourselves",
    # Common conversational words, generic verbs, adjectives, nouns
    "ok", "okay", "yeah", "yep", "nope", "poor", "good", "bad", "great", "well", "just", "like", "basically",
    "actually", "really", "going", "get", "getting", "got", "make", "making", "made", "say", "saying", "said",
    "look", "looking", "see", "seeing", "saw", "think", "thinking", "thought", "know", "knowing", "knew",
    "take", "taking", "took", "come", "coming", "came", "go", "went", "gone", "put", "tell", "talk", "talking",
    "use", "using", "used", "work", "working", "worked", "try", "trying", "tried", "start", "starting", "started",
    "one", "two", "three", "first", "second", "third", "next", "now", "break", "quick", "issues", "issue",
    "problem", "problems", "thing", "things", "something", "anything", "nothing", "someone", "anyone", "everyone",
    "experience", "experiences", "outcome", "outcomes", "money", "user", "users", "system", "systems", "need",
    "needs", "want", "wants", "way", "ways", "lot", "lots", "mean", "means", "meant", "right", "sure", "maybe",
    "kind", "sort", "bit", "point", "points", "part", "parts", "case", "cases", "time", "times"
}

# 严格过滤国家、地区代码、日常通用代词或非专业学术缩写（绝不作为专有名词展示）
NON_ACADEMIC_TERMS = {
    # 国家、地区代码及简称
    "us", "usa", "uk", "eu", "un", "cn", "jp", "de", "fr", "ru", "in", "au", "ca", "nz", "kr", "it", "es", "ch", "nl", "se", "no", "sg", "hk", "tw", "mo",
    "america", "american", "china", "chinese", "europe", "european", "japan", "japanese", "united states",
    # 常用人称代词与日常缩写
    "them", "him", "her", "its", "our", "ours", "their", "theirs", "me", "you",
    "am", "pm", "ok", "okay", "tv", "pc", "vs", "etc", "id", "no", "yes", "hi", "hello", "by", "mr", "ms", "mrs", "dr",
    "iq", "eq", "vip", "ceo", "cfo", "coo", "cto", "hr", "pr", "ad", "bc", "ps", "faq", "fyi", "asap",
    "diy", "aka", "tba", "tbd", "eta", "tgif", "lol", "omg", "na", "n/a",
    # 时间日常词
    "today", "tomorrow", "yesterday", "now", "then", "week", "month", "year", "day", "hour", "minute", "second",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
}

# 内置高频计算机、网络安全、系统架构与 AI 学术技术词典（提供权威中文定义与背景原理解析）
BUILTIN_TECH_GLOSSARY = {
    "owasp": {
        "term_en": "OWASP",
        "term_zh": "开放式Web应用安全项目",
        "type": "安全组织与规范",
        "desc": "全球权威的应用安全非营利组织，其发布的 OWASP Top 10 是评估 Web 漏洞风险的事实国际标准。"
    },
    "mdm": {
        "term_en": "MDM (Mobile Device Management)",
        "term_zh": "企业移动设备管理",
        "type": "企业运维与安全",
        "desc": "企业统一管控办公终端的系统，可远程下发安全策略（如离岗自动锁屏、防截屏、远程数据擦除）。"
    },
    "hipaa": {
        "term_en": "HIPAA",
        "term_zh": "健康保险可携性与责任法案",
        "type": "法规标准",
        "desc": "美国针对医疗健康与患者隐私制定的合规法案，对敏感健康信息（PHI）保护有极严苛的防泄密要求。"
    },
    "fda": {
        "term_en": "FDA",
        "term_zh": "美国食品药品监督管理局",
        "type": "行业法规",
        "desc": "在医疗健康系统、医用软件及关键嵌入式算法中，必须通过 FDA 极严格的合规审计与流程验证标准。"
    },
    "nist": {
        "term_en": "NIST",
        "term_zh": "美国国家标准与技术研究院",
        "type": "国家安全标准",
        "desc": "制定了全球公认的网络安全框架（CSF）与密码算法标准，是企业架构安全与等保合规的核心基石。"
    },
    "gdpr": {
        "term_en": "GDPR",
        "term_zh": "通用数据保护条例",
        "type": "数据隐私法案",
        "desc": "欧盟严苛的数据隐私保护条例，强调用户的知情权与被遗忘权，对用户敏感数据跨境传输实施严格监管。"
    },
    "soc2": {
        "term_en": "SOC 2",
        "term_zh": "服务机构控制报告",
        "type": "安全审计",
        "desc": "针对云服务商与 SaaS 企业的独立合规审计报告，评估系统在安全性、可用性及机密性上的控制机制。"
    },
    "xss": {
        "term_en": "XSS (Cross-Site Scripting)",
        "term_zh": "跨站脚本攻击",
        "type": "应用安全漏洞",
        "desc": "攻击者向网页注入恶意客户端脚本，受害者浏览时在浏览器端静默执行以窃取身份 Cookie 或敏感凭证。"
    },
    "csrf": {
        "term_en": "CSRF",
        "term_zh": "跨站请求伪造",
        "type": "应用安全漏洞",
        "desc": "诱导用户在已认证的浏览器中，向受信任网站发起未经授权的恶意操作请求（如转账、修改安全邮箱）。"
    },
    "ddos": {
        "term_en": "DDoS",
        "term_zh": "分布式拒绝服务攻击",
        "type": "网络安全攻击",
        "desc": "控制大量僵尸网络节点向目标服务器灌注海量虚假流量，耗尽网络带宽或计算资源导致合法服务瘫痪。"
    },
    "jwt": {
        "term_en": "JWT (JSON Web Token)",
        "term_zh": "JSON Web 令牌",
        "type": "身份认证与授权",
        "desc": "基于数字签名的轻量级、自包含跨域认证规范，广泛用于微服务架构与前后端分离的无状态会话鉴权。"
    },
    "oauth": {
        "term_en": "OAuth 2.0",
        "term_zh": "开放授权协议",
        "type": "鉴权标准",
        "desc": "业内标准的授权框架，允许第三方应用在不直接获取用户账号密码的前提下安全获得受限的资源访问令牌。"
    },
    "tcp": {
        "term_en": "TCP",
        "term_zh": "传输控制协议",
        "type": "网络传输协议",
        "desc": "面向连接、高可靠且基于字节流的传输层通信协议，具备三次握手建立连接、丢包重传及拥塞控制机制。"
    },
    "udp": {
        "term_en": "UDP",
        "term_zh": "用户数据报协议",
        "type": "网络传输协议",
        "desc": "无连接、开销低且实时性极高的传输层协议，广泛应用于音视频直播通话、多人联机对战与流媒体传输。"
    },
    "dns": {
        "term_en": "DNS",
        "term_zh": "域名系统",
        "type": "网络基础设施",
        "desc": "互联网核心基础设施，通过分布式树状查询将人类易记的域名解析为计算机底层通信的 IP 地址。"
    },
    "http": {
        "term_en": "HTTP",
        "term_zh": "超文本传输协议",
        "type": "应用层协议",
        "desc": "万维网数据通信的基础协议，基于请求-响应无状态模型，是 Web 浏览器与服务端交互的基石。"
    },
    "https": {
        "term_en": "HTTPS",
        "term_zh": "安全超文本传输协议",
        "type": "加密通信协议",
        "desc": "在 HTTP 基础上结合 TLS/SSL 实施公钥握手与对称加密传输，保障数据机密性与防篡改完整性。"
    },
    "tls": {
        "term_en": "TLS",
        "term_zh": "传输层安全性协议",
        "type": "密码学协议",
        "desc": "为互联网通信提供数据保密与完整性保护的现代密码学协议（SSL 的升级版），全面保障端到端加密。"
    },
    "ssl": {
        "term_en": "SSL",
        "term_zh": "安全套接字层",
        "type": "传统加密协议",
        "desc": "网络通信早期的加密协议，现已被安全性更高、算法更现代的 TLS 协议完全继承与替代。"
    },
    "ssh": {
        "term_en": "SSH",
        "term_zh": "安全外壳协议",
        "type": "安全协议",
        "desc": "在不安全网络上通过非对称密钥加密为远程计算机提供安全终端交互与文件传输的协议。"
    },
    "cpu": {
        "term_en": "CPU",
        "term_zh": "中央处理器",
        "type": "计算机硬件架构",
        "desc": "计算机的核心控制中枢与运算单元，负责解释执行程序指令、进行算术逻辑运算及统筹各部件调度。"
    },
    "gpu": {
        "term_en": "GPU",
        "term_zh": "图形处理器",
        "type": "高并发算力硬件",
        "desc": "拥有海量并行计算核心的高吞吐硬件，现已成为 AI 深度学习大规模矩阵运算与图像处理的主流算力引擎。"
    },
    "ram": {
        "term_en": "RAM",
        "term_zh": "随机存取存储器",
        "type": "主内存系统",
        "desc": "与 CPU 直接高速交换数据的易失性主存储器，断电后数据即失，承载操作系统及运行中程序的活动数据。"
    },
    "rom": {
        "term_en": "ROM",
        "term_zh": "只读存储器",
        "type": "固态存储",
        "desc": "非易失性存储芯片，断电后数据永不丢失，通常用于固化存放计算机开机自检与底层启动固件（BIOS/UEFI）。"
    },
    "dma": {
        "term_en": "DMA (Direct Memory Access)",
        "term_zh": "直接内存访问",
        "type": "I/O 系统架构",
        "desc": "允许高速外设绕过 CPU 直接读写主内存，大幅卸载 CPU 搬运数据的开销，提升大吞吐数据传输效率。"
    },
    "cnn": {
        "term_en": "CNN",
        "term_zh": "卷积神经网络",
        "type": "经典深度模型",
        "desc": "利用局部感受野卷积核与权重共享机制提取图像网格特征的深度架构，计算机视觉领域的核心支柱。"
    },
    "rnn": {
        "term_en": "RNN",
        "term_zh": "循环神经网络",
        "type": "时序深度模型",
        "desc": "通过隐藏状态时间循环反馈建模时序上下文依赖的神经网络，常用于语音、自然语言等动态序列处理。"
    },
    "llm": {
        "term_en": "LLM",
        "term_zh": "大语言模型",
        "type": "人工智能前沿",
        "desc": "基于海量文本自监督预训练的数十亿至万亿级参数深度模型，具备通用的自然语言理解、逻辑推理与生成能力。"
    },
    "rag": {
        "term_en": "RAG (Retrieval-Augmented Generation)",
        "term_zh": "检索增强生成",
        "type": "大模型落地架构",
        "desc": "在 LLM 回答前提早从私域知识库检索高相关文档作为上下文，有效解决模型事实幻觉与知识时效性问题。"
    },
    "sql": {
        "term_en": "SQL",
        "term_zh": "结构化查询语言",
        "type": "关系型数据库",
        "desc": "用于在关系型数据库（如 PostgreSQL/MySQL）中定义表结构、执行增删改查及事务管理的核心标准语言。"
    },
    "nosql": {
        "term_en": "NoSQL",
        "term_zh": "非关系型数据库",
        "type": "分布式存储架构",
        "desc": "针对海量数据高并发读写与灵活半结构化模式设计的分布式数据库（如 Redis, MongoDB, Cassandra）。"
    },
    "docker": {
        "term_en": "Docker",
        "term_zh": "应用容器引擎",
        "type": "虚拟化技术",
        "desc": "基于 Linux Namespace 与 Cgroups 的轻量级虚拟化，将应用及其全部运行依赖打包为高可移植的自给镜像。"
    },
    "k8s": {
        "term_en": "Kubernetes (K8s)",
        "term_zh": "容器集群编排系统",
        "type": "云原生编排",
        "desc": "自动化容器集群管理平台，负责海量容器的自动化部署调度、弹性横向伸缩、滚动升级与故障自愈。"
    }
}

def clean_academic_keywords(keywords: List[Any]) -> List[str]:
    cleaned = []
    seen = set()
    for raw in keywords:
        if not raw or not isinstance(raw, str):
            continue
        kw = raw.strip().strip('#').strip()
        kw_lower = kw.lower()
        if not kw or len(kw) < 2 or len(kw) > 45:
            continue
        # 排除英文停用词与非学术日常缩写（如 US, EU, UK 等）
        if kw_lower in ENGLISH_STOP_WORDS or kw_lower in NON_ACADEMIC_TERMS:
            continue
        # 单词检查
        if " " not in kw:
            if kw.isdigit():
                continue
            # 针对 2-3 字母的缩写：必须不是非学术缩写，且若为 2 字符，必须属于已知专业术语或大写
            if len(kw) < 4:
                if not (kw.isupper() and len(kw) >= 2):
                    continue
                if kw_lower in NON_ACADEMIC_TERMS:
                    continue
            if kw_lower in ENGLISH_STOP_WORDS or kw_lower in NON_ACADEMIC_TERMS:
                continue
        else:
            # 多词短语：检查是否全由停用词或日常代词组成
            words = kw_lower.split()
            if all(w in ENGLISH_STOP_WORDS or w in NON_ACADEMIC_TERMS for w in words):
                continue
        
        if kw_lower not in seen:
            seen.add(kw_lower)
            cleaned.append(kw)
    return cleaned[:3]

def heuristic_annotations(text: str) -> List[Dict[str, str]]:
    results = []
    seen_keys = set()

    # 1. 优先扫描内置核心专业词典
    for key, entry in BUILTIN_TECH_GLOSSARY.items():
        if re.search(r'\b' + re.escape(key) + r'\b', text, flags=re.IGNORECASE):
            if key not in seen_keys:
                seen_keys.add(key)
                results.append({
                    "term": entry["term_en"],
                    "type": entry.get("type", "专业术语"),
                    "explanation": entry["desc"]
                })
        if len(results) >= 3:
            return results

    # 2. 启发式场景规则
    extra_patterns = [
        (r'\b(mobile device management|MDM)\b', "Mobile Device Management (MDM)", "企业运维与安全", "企业移动设备管理软件，用于远程配置、安全策略监控（如离开自动锁屏）及设备合规审计。"),
        (r'\b(personal health|HIPAA|PHI)\b', "HIPAA / 个人健康信息安全", "法规标准", "涉及个人敏感健康数据保护，法规对数据防泄密有严格要求，违规未锁屏或泄密将面临重罚。"),
        (r'\b(FDA)\b', "美国食品药品监督管理局 (FDA)", "行业规范", "在涉及医疗健康系统与设备时，必须符合 FDA 极其严格的合规审计与流程验证标准。"),
        (r'\b(NIST)\b', "NIST 美国国家标准与技术研究院", "国家安全标准", "制定了全球顶级的网络安全框架（CSF）与密码学标准，企业合规的重要基准。"),
        (r'\b(lock|auto lock)\b', "离开工位自动锁屏", "安全合规", "办公安全基本守则，防止他人或访客在无人看管的电脑上窥探或窃取机密。"),
        (r'\b(threat)\b', "内部/未授权威胁 (Security Threat)", "安全概念", "在企业网络安全中指可能导致数据泄露、非授权访问或服务中断的安全隐患。"),
        (r'\b(convolutional neural network|CNN)\b', "卷积神经网络 (CNN)", "经典深度模型", "深度学习中处理图像与网格数据的核心架构，通过局部感受野和权重共享提取空间特征。"),
    ]
    for pattern, term, ttype, exp in extra_patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            term_l = term.lower()
            if term_l not in seen_keys:
                seen_keys.add(term_l)
                results.append({"term": term, "type": ttype, "explanation": exp})
        if len(results) >= 3:
            break

    return results[:3]

async def execute_gemini_call(api_key: str, model_name: str, prompt: str, is_json: bool = True) -> str:
    key = (api_key or os.environ.get("GEMINI_API_KEY") or "").strip()
    primary = sanitize_gemini_model(model_name)
    # 多层自适应高可用降级链：用户指定模型 -> 2.5-flash -> 2.0-flash -> 1.5-flash -> 2.0-flash-lite -> 2.5-pro
    candidates = [primary]
    for m in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.0-flash-lite", "gemini-2.5-pro", "gemini-1.5-pro", "gemini-3.7-flash", "gemini-3.6-flash"]:
        if m not in candidates:
            candidates.append(m)

    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if is_json:
        payload["generationConfig"] = {"response_mime_type": "application/json"}

    last_err = None
    async with httpx.AsyncClient(timeout=16.0) as client:
        for model in candidates:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
            try:
                resp = await client.post(endpoint, headers=headers, json=payload)
                if resp.status_code == 200:
                    data = resp.json()
                    candidates_list = data.get("candidates", [])
                    if candidates_list:
                        parts = candidates_list[0].get("content", {}).get("parts", [])
                        if parts and "text" in parts[0]:
                            raw_text = parts[0]["text"]
                            raw_text = re.sub(r'^```json\s*', '', raw_text.strip())
                            raw_text = re.sub(r'\s*```$', '', raw_text.strip())
                            return raw_text
                else:
                    last_err = f"API Error {resp.status_code}: {resp.text}"
                    print(f"[Gemini] Model {model} returned {resp.status_code}, auto-falling back to next candidate...")
                    if resp.status_code in [400, 401, 403, 429]:
                        break
            except Exception as ex:
                last_err = str(ex)
                print(f"[Gemini] Model {model} exception: {ex}, auto-falling back to next candidate...")

        # 备用：OpenAI 兼容接口重试
        for fb_model in ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]:
            try:
                openai_ep = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
                openai_hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
                openai_payload = {
                    "model": fb_model,
                    "messages": [
                        {"role": "system", "content": "You are a world-class academic lecture assistant and translator. Always return valid JSON only." if is_json else "You are a helpful teaching assistant."},
                        {"role": "user", "content": prompt}
                    ]
                }
                if is_json:
                    openai_payload["response_format"] = {"type": "json_object"}
                resp2 = await client.post(openai_ep, headers=openai_hdrs, json=openai_payload)
                if resp2.status_code == 200:
                    raw_text = resp2.json()["choices"][0]["message"]["content"]
                    raw_text = re.sub(r'^```json\s*', '', raw_text.strip())
                    raw_text = re.sub(r'\s*```$', '', raw_text.strip())
                    return raw_text
            except Exception as ex2:
                last_err = str(ex2)

    raise Exception(last_err or "所有 Gemini 模型通道响应异常，请稍后重试")

async def translate_via_llm(text: str, source_lang: str, target_lang: str, req: TranslateRequest) -> Dict[str, Any]:
    prompt = f"""你是一名世界顶级学术同声传译员与名校笔记助教。
请分析以下老师讲课原话，并完成：
1. 过滤口语口头禅（如 um, you know, like, basically 等），输出干净连贯的讲师英文原声。
2. 翻译为流畅、严谨、学术级的中文译文。
3. 【最重要：AI 智能讲解与背景标注 (annotations)】：
   识别本段话中出现的任何：学术概念、专业技术术语、行业法规标准、计算机软硬件架构或核心协议。
   【严禁提取国家/地区或日常普通缩写】：严禁将 US, EU, UK, UN, CN 等国家/地区名、日常人称代词或常见生活词（如 how, and, poor 等）误提取为专业术语！只提炼真正的学术技术概念、架构、协议、法规标准。
   对识别出的 1-3 个重点实体提供通俗透彻的背景讲解：
   - "term": 专业名词名称（如 "Mobile Device Management (MDM)", "HIPAA", "FDA", "NIST", "OWASP" 等）
   - "type": 分类，从 ["专业术语", "法规标准", "核心考点", "安全合规", "系统架构", "经典算法"] 中选一
   - "explanation": 用 1-2 句通俗透彻的中文解释其定义、原理或在当前话境下的背景意义。
4. 提取 1-3 个核心专有名词标签（keywords），严格排除国家地名缩写与日常词汇。
5. 判断是否开启了新章节/小标题，若有则提取（section_heading）。

讲课原话：
"{text}"

请严格按如下合法的 JSON 格式输出（绝不要加任何 markdown 或 ```json 标记）：
{{
  "cleaned_source": "过滤口头禅后的清晰原话",
  "translation": "准确优雅的中文译文",
  "keywords": ["术语1", "术语2"],
  "annotations": [
    {{
      "term": "专业术语名称",
      "type": "专业术语",
      "explanation": "简明透彻的定义或背景讲解"
    }}
  ],
  "section_heading": "新小标题（若无可留空字符串）"
}}"""

    prov, model = resolve_provider_and_model(req.api_key, req.provider, req.model_name, req.custom_endpoint)
    if prov == "gemini":
        api_key = req.api_key.strip()
        raw_json = await execute_gemini_call(api_key, model, prompt, is_json=True)
        return json.loads(raw_json)
    else:
        api_key = req.api_key.strip()
        endpoint = validate_custom_endpoint(req.custom_endpoint) + "/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a world-class academic lecture assistant and translator. Always return valid JSON only."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"}
        }

        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(endpoint, headers=headers, json=payload)
            if resp.status_code == 200:
                raw_text = resp.json()["choices"][0]["message"]["content"]
                raw_text = re.sub(r'^```json\s*', '', raw_text.strip())
                raw_text = re.sub(r'\s*```$', '', raw_text.strip())
                return json.loads(raw_text)
            else:
                raise Exception(f"API Error {resp.status_code}: {resp.text}")

@app.post("/api/translate")
async def handle_translate(req: TranslateRequest, request: Request, _rl=Depends(check_rate_limit)):
    raw_text = req.text.strip()
    if not raw_text:
        return {"cleaned_source": "", "translation": "", "keywords": [], "annotations": [], "section_heading": None}
    log_event_bg("translate_use", request)

    cleaned = clean_filler_words(raw_text, req.source_lang)
    heading = extract_heading_signal(cleaned)
    api_error_msg = None

    if req.api_key and req.provider in ["gemini", "openai_compatible", "deepseek"]:
        try:
            llm_result = await translate_via_llm(raw_text, req.source_lang, req.target_lang, req)
            cleaned_kws = clean_academic_keywords(llm_result.get("keywords", []))
            # 过滤 annotations 中的国家名或非学术缩写
            valid_annotations = [
                a for a in llm_result.get("annotations", [])
                if a.get("term") and a["term"].strip().lower() not in NON_ACADEMIC_TERMS and a["term"].strip().lower() not in ENGLISH_STOP_WORDS
            ]
            return {
                "cleaned_source": llm_result.get("cleaned_source", cleaned),
                "translation": llm_result.get("translation", ""),
                "keywords": cleaned_kws,
                "annotations": valid_annotations,
                "section_heading": llm_result.get("section_heading") or heading,
                "api_status": "ok"
            }
        except Exception as e:
            err_str = str(e)
            print(f"[Translation] LLM fallback error: {err_str}")
            if "API_KEY_INVALID" in err_str or "API key not valid" in err_str:
                api_error_msg = "API Key 校验未通过，请检查您的 Key 是否完整无误。"
            else:
                api_error_msg = None

    # Keyless translation happens in the visitor's browser: the free Google and
    # MyMemory endpoints block Render's shared datacenter IP but accept ordinary
    # user IPs, so here we only clean and tag the text.
    h_annotations = heuristic_annotations(cleaned)

    DOMAIN_TAG_PATTERNS = [
        (r'\b(governance|government|policy|policies)\b', "治理体系与规范"),
        (r'\b(intelligence|ai|model|llm|deep learning)\b', "AI智能与模型架构"),
        (r'\b(security|secure|safe|safety|threat)\b', "系统安全防线"),
        (r'\b(detector|detection|exposed|exposure)\b', "安全检测探针"),
        (r'\b(attack|attacks|exploit|payload|hack)\b', "攻击测试与对抗"),
        (r'\b(system|systems|building|infrastructure)\b', "系统架构与工程"),
        (r'\b(cache|latency|throughput|pipeline|memory)\b', "体系结构与性能"),
        (r'\b(concurrency|thread|threads|lock|deadlock|mutex)\b', "并发与资源调度"),
        (r'\b(network|protocol|protocols|http|tcp|packet)\b', "网络与通信协议"),
        (r'\b(data|database|sql|storage|privacy)\b', "数据存储与隐私"),
        (r'\b(hardware|cpu|gpu|chip|register)\b', "底层硬件与计算"),
        (r'\b(algorithm|complexity|sort|tree|graph)\b', "算法理论与推导"),
    ]

    # Heuristic academic keywords: 严格过滤非学术缩写，优先采纳内置词典、领域模式与启发式实体
    candidate_kws = []
    for a in h_annotations:
        if a.get("term"):
            candidate_kws.append(a["term"])

    cleaned_lower = cleaned.lower()
    for pat, tag_zh in DOMAIN_TAG_PATTERNS:
        if re.search(pat, cleaned_lower):
            candidate_kws.append(tag_zh)

    if "en" in req.source_lang:
        acronyms = [
            w for w in re.findall(r'\b[A-Z]{2,6}\b', cleaned)
            if w.lower() not in NON_ACADEMIC_TERMS and w.lower() not in ENGLISH_STOP_WORDS
        ]
        candidate_kws.extend(acronyms)
        multi_words = [
            w for w in re.findall(r'(?<![.?!;]\s)\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b', cleaned)
            if w.lower() not in NON_ACADEMIC_TERMS
        ]
        candidate_kws.extend(multi_words)

    # 若仍然缺少标签，从中文原文（中文授课时）中抽取学术概念短语兜底
    if not candidate_kws:
        zh_concepts = re.findall(r'[\u4e00-\u9fa5]{2,5}(?:机制|系统|算法|架构|模型|规范|策略|协议|逻辑)', cleaned)
        if zh_concepts:
            candidate_kws.extend(zh_concepts[:2])
        else:
            candidate_kws.append("课堂重点论述")

    keywords = clean_academic_keywords(candidate_kws)

    return {
        "cleaned_source": cleaned,
        "translation": "",
        "keywords": keywords,
        "annotations": h_annotations,
        "section_heading": heading,
        "api_error": api_error_msg
    }

def generate_dynamic_academic_answer(card_src: str, card_tr: str, user_q: str = "") -> str:
    """
    根据卡片英文原声与中文精译内容，动态自适应生成结构化深度学术解析。
    彻底杜绝死板模板，确保不同卡片提取的内容、考点和原理各不相同。
    """
    combined = f"{card_src} {card_tr}".lower()
    
    domain_topic = None
    # 1. 寻找安全检测、对抗演练、探针
    if any(k in combined for k in ["detector", "detection", "attack", "exposed", "exposure", "pattern", "探测器", "攻击", "暴露"]):
        domain_topic = (
            "安全检测与对抗演练 (Attack Detection & Exposure)",
            "通过主动构造或模拟攻击测试（attacks/patterns），针对系统部署的探测探针（detectors）进行动态暴露面验证。",
            "主动防御的核心在于‘以攻促防’。静态代码与配置审查无法覆盖运行时多变交互，因此需建立高覆盖度的检测探针与攻击特征库，闭环验证防护盲区。",
            "1. 探针规则的误报率与漏报率（False Positive vs False Negative）权衡；\n• 2. 攻击载荷（Payload）变异对检测模型的穿透风险；\n• 3. 生产环境实战演练时的业务隔离与熔断机制。"
        )
    # 2. 终端安全、MDM、运维合规
    elif any(k in combined for k in ["mdm", "mobile device", "auto lock", "lock", "移动设备", "锁屏", "合规", "管控"]):
        domain_topic = (
            "终端安全管控与自动化策略 (Device Management & Security Policies)",
            "统一管控办公设备的安全基线，下发包括离岗自动锁屏、加密存储与防截屏等合规策略。",
            "属于零信任（Zero Trust）架构中的终端可信验证层。杜绝物理接触导致的非授权窥探与数据横向渗透。",
            "1. 策略静默下发的权限层级与越权旁路防范；\n• 2. 设备离线脱网状态下的安全策略兜底；\n• 3. 隐私保护与企业审计日志合规性界限。"
        )
    # 3. 隐私保护、法规合规标准
    elif any(k in combined for k in ["hipaa", "phi", "fda", "gdpr", "nist", "health", "privacy", "隐私", "法规", "健康"]):
        domain_topic = (
            "行业法规遵从与敏感数据合规 (Regulatory Compliance & Data Privacy)",
            "在严监管领域（如医疗健康、个人隐私、关键基础设施），技术系统必须遵循严格的防泄密规范与审计流程。",
            "合规性设计属于非功能性架构的核心约束。违规泄露不仅导致技术瘫痪，更会触发巨额法律惩罚与牌照吊销。",
            "1. 敏感数据存储与传输链路端到端加密（TLS/AES-256）；\n• 2. 细粒度最小权限访问控制（RBAC/ABAC）；\n• 3. 审计追踪（Audit Trails）的不可篡改性与归档要求。"
        )
    # 4. 并发、操作系统、多线程
    elif any(k in combined for k in ["thread", "concurrency", "mutex", "deadlock", "process", "memory", "线程", "并发", "死锁", "互斥", "内存"]):
        domain_topic = (
            "系统并发与资源竞争调度 (Concurrency & Resource Scheduling)",
            "多任务或多线程协同工作时，通过同步原语与排队机制协调共享资源的互斥访问。",
            "现代高并发系统的基石。若缺乏原子性保障将引发竞争条件（Race Condition），而过度锁竞争则会导致吞吐量暴跌或死锁。",
            "1. 死锁形成的四大必要条件与破坏策略；\n• 2. 乐观锁与悲观锁在不同读写比例下的性能选择；\n• 3. 无锁编程（Lock-free CAS）与内存可见性（Memory Barrier）。"
        )
    # 5. 计算机架构、流水线、缓存
    elif any(k in combined for k in ["cache", "latency", "throughput", "pipeline", "buffer", "api", "http", "缓存", "时延", "吞吐", "流水线", "缓冲区"]):
        domain_topic = (
            "系统高可用与流水线性能优化 (System Architecture & Pipeline Optimization)",
            "利用多级缓存与流水线并发重叠，消解 I/O 瓶颈，降低响应时延并提升吞吐能力。",
            "计算机体系结构中的核心权衡原则——空间换时间、延迟换吞吐。合理规划局部性原理（Locality）以最大化硬件利用率。",
            "1. 缓存击穿、穿透与雪崩的场景特征与防护方案；\n• 2. 流水线冒险（数据冒险、控制冒险、结构冒险）与分支预测；\n• 3. 接口幂等性设计与背压（Backpressure）流控机制。"
        )

    if domain_topic:
        topic_title, plain_exp, core_principle, exam_points = domain_topic
    else:
        # 启发式提取卡片中的核心中英文短语，针对本卡片精准定制
        words_en = [w for w in re.findall(r'[a-zA-Z]{3,}', card_src) if w.lower() not in ENGLISH_STOP_WORDS and w.lower() not in NON_ACADEMIC_TERMS]
        words_zh = [c for c in re.findall(r'[\u4e00-\u9fa5]{2,6}', card_tr) if c not in ["这个", "那个", "好的", "而且", "所以", "如果", "我们", "你们", "他们", "进行", "可以", "以及"]]
        
        top_focus = f"{words_en[0]} ({words_zh[0]})" if (words_en and words_zh) else (words_zh[0] if words_zh else "课堂核心推导逻辑")
        topic_title = f"本段核心知识点：【{top_focus}】"
        plain_exp = f"讲师在此处通过语境递进，重点剖析了“{card_tr[:50]}...”背后的本质逻辑。建议结合前后上下文因果关系进行系统把握。"
        core_principle = "该论述在学科知识网络中起到了承上启下的枢纽作用，明确了技术落地的边界约束与核心前提假设。"
        exam_points = "1. 熟记该结论成立的核心前置条件与参数取值范围；\n• 2. 注意考核中关于因果倒置或概念偷换的高频考点陷阱。"

    return f"""💡 助教深度拆解（本段精讲）：
老师在此处重点聚焦于【{topic_title}】。

• 大白话理解：
{plain_exp}

• 核心原理与底层逻辑：
{core_principle}

• 复习与常考点：
• {exam_points}

📌 *助教提示：本条已由智能情境引擎生成深度学术解析。若在右上角设置中填入 API Key，助教将由云端大模型（Gemini / GPT）进行长文本全方位推理！*"""

@app.post("/api/ask-card")
async def handle_ask_card(req: AskCardRequest, request: Request, _rl=Depends(check_rate_limit)):
    log_event_bg("ask_ta", request)
    card_src = (req.card_source or "").strip()
    card_tr = (req.card_translation or "").strip()
    user_inquiry = f"【学生具体疑问】: {req.question.strip()}" if req.question and req.question.strip() else "【任务】: 学生点击了一键答疑，请主动全方位深度精讲本段核心知识点。"

    prompt = f"""你是一名世界顶级名校计算机与工程学科的资深学术助教。
请针对以下老师课堂讲授的双语卡片，为学生提供一份结构清晰、生动通俗的【助教深度解析与考点精讲】：

【老师原声】:
{card_src}

【中文精译】:
{card_tr}

{user_inquiry}

请用通俗易懂、切中要害的学术助教语言展开精讲，必须严格按以下结构输出：
💡 助教深度拆解（本段核心精讲）：
一两句话精炼提炼老师这段话的核心论述与实质意义。

• 大白话理解：
用形象生动的比喻或底层生活直觉，解释老师说的本质逻辑，消除理解障碍。

• 核心原理与背景：
该知识点在系统体系、架构设计或行业实践中为什么重要，解决了什么关键痛点。

• 复习与常考点：
在课程考核、期末试卷或技术面试中，这段内容最容易怎么考？有哪些极易混淆的概念陷阱？

字数控制在 160~320 字以内，层次分明，排版规范，让学生一眼看懂！"""

    creds = await resolve_ai_credentials(
        request, req.api_key, req.provider, req.model_name, req.custom_endpoint, "qa"
    )
    api_key = creds["api_key"]
    provider = creds["provider"]
    model = creds["model_name"]
    custom_ep = creds["custom_endpoint"]

    if api_key and provider in ["gemini", "openai_compatible", "deepseek"]:
        try:
            print(f"[AskCard] Calling LLM via {provider}, model={model}, ultra={creds['via_ultra_quota']}...")
            if provider == "gemini":
                ans = await execute_gemini_call(api_key, model, prompt, is_json=False)
                if ans and len(ans.strip()) > 20:
                    return {"answer": ans.strip(), "source": "ultra_llm" if creds["via_ultra_quota"] else "gemini_llm"}
            else:
                endpoint = validate_custom_endpoint(custom_ep) + "/chat/completions"
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are an elite university teaching assistant. Provide direct, highly educational lecture breakdowns."},
                        {"role": "user", "content": prompt}
                    ]
                }
                async with httpx.AsyncClient(timeout=20.0) as client:
                    resp = await client.post(endpoint, headers=headers, json=payload)
                    if resp.status_code == 200:
                        res_json = resp.json()
                        ans = res_json["choices"][0]["message"]["content"]
                        if ans and len(ans.strip()) > 20:
                            return {"answer": ans.strip(), "source": "ultra_llm" if creds["via_ultra_quota"] else "openai_llm"}
        except Exception as e:
            print(f"[AskCard] LLM call failed, smoothly switching to dynamic academic fallback: {e}")

    # 动态上下文自适应语义答疑（绝非千篇一律的死板套话，彻底杜绝所有卡片相同内容）
    ans = generate_dynamic_academic_answer(card_src, card_tr, req.question)
    if creds.get("quota_notice"):
        ans = f"⚠️ {creds['quota_notice']}\n\n" + ans
    return {"answer": ans, "source": "adaptive_engine"}

@app.post("/api/summarize")
async def handle_summarize(req: SummarizeRequest, request: Request, _rl=Depends(check_rate_limit)):
    if not req.items:
        return {"overview": "暂无有效课堂记录", "takeaways": [], "glossary": []}

    transcript_blocks = []
    for item in req.items:
        time_str = item.get("time_str", "")
        src = item.get("cleaned_source") or item.get("source_text", "")
        tr = item.get("translation", "")
        transcript_blocks.append(f"[{time_str}] 原文: {src}\n译文: {tr}")

    full_text = "\n\n".join(transcript_blocks[-50:])

    prompt = f"""请深度分析以下课堂双语实录（课程：{req.session_title}），为同学生成一份结构清晰、高可读性的【课堂精要复习板书】：
要求：
1. 【概览 (overview)】：必须用 2-4 句话（120-220字）提纲挈领地总结本堂课讲解的核心学术主题、技术框架、攻防/推导因果与最终结论。严禁输出“共记录了X个知识意群”等字数统计废话！必须是对讲授内容的实质性专业概括！
2. 【核心要点 (takeaways)】：提炼 3-5 条重点干货（列表形式），突出重点公式/理论/结论/工程合规要求。严禁输出任何“建议对照卡片复习”等空壳模板套话！
3. 【核心术语对照 (glossary)】：提取 3-6 个核心中英文专有名词解释。
   【严格过滤指令】：严禁将 US, EU, UK, UN, CN 等国家/地区缩写，或 ordinary common words（如 how, and, poor, week 等）提取为专有名词！
   每一项专有名词的 desc 字段必须写明其实质定义与底层原理（20-60字），严禁输出“高频学术词汇/概念”等空壳套话！

课堂记录节选：
{full_text}

请严格按如下 JSON 格式输出：
{{
  "overview": "本节课重点聚焦于...",
  "takeaways": ["重点1...", "重点2...", "重点3..."],
  "glossary": [
    {{"term_en": "Mobile Device Management (MDM)", "term_zh": "企业移动设备管理", "desc": "企业对员工终端设备实施统一安全策略监控（如离岗自动锁屏、防截屏、远程擦除）的管理系统。"}}
  ]
}}"""

    def _sanitize_glossary_list(items_list):
        cleaned_list = []
        seen = set()
        for g in items_list:
            if not isinstance(g, dict):
                continue
            t_en = (g.get("term_en") or g.get("term") or "").strip()
            t_zh = (g.get("term_zh") or g.get("type") or "专业术语").strip()
            desc = (g.get("desc") or g.get("explanation") or "").strip()
            t_en_l = t_en.lower()
            if not t_en or t_en_l in seen or t_en_l in ENGLISH_STOP_WORDS or t_en_l in NON_ACADEMIC_TERMS or len(t_en) < 2:
                continue
            # 若描述缺失或含占位符，尝试从内置词典补全
            if (not desc or "高频学术" in desc or "核心概念" in desc or len(desc) < 6) and t_en_l in BUILTIN_TECH_GLOSSARY:
                entry = BUILTIN_TECH_GLOSSARY[t_en_l]
                desc = entry["desc"]
                if t_zh == "专业术语" or not t_zh:
                    t_zh = entry.get("term_zh", t_zh)
            # 杜绝无实质解释的空壳词条
            if desc and len(desc) >= 6 and "高频学术" not in desc:
                seen.add(t_en_l)
                cleaned_list.append({"term_en": t_en, "term_zh": t_zh, "desc": desc})
        return cleaned_list

    creds = await resolve_ai_credentials(
        request, req.api_key, req.provider, req.model_name, req.custom_endpoint, "qa"
    )
    api_key = creds["api_key"]
    provider = creds["provider"]
    model = creds["model_name"]
    custom_ep = creds["custom_endpoint"]

    if api_key and provider in ["gemini", "openai_compatible", "deepseek"]:
        try:
            print(f"[Summarize] Calling LLM via {provider}, model={model}, ultra={creds['via_ultra_quota']}...")
            if provider == "gemini":
                raw_json = await execute_gemini_call(api_key, model, prompt, is_json=True)
                data = json.loads(raw_json)
                if "glossary" in data and isinstance(data["glossary"], list):
                    data["glossary"] = _sanitize_glossary_list(data["glossary"])
                if data.get("overview") and "共记录" not in data.get("overview"):
                    return data
            else:
                endpoint = validate_custom_endpoint(custom_ep) + "/chat/completions"
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are an elite academic assistant. Return valid JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"}
                }
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(endpoint, headers=headers, json=payload)
                    if resp.status_code == 200:
                        raw_text = resp.json()["choices"][0]["message"]["content"]
                        raw_text = re.sub(r'^```json\s*', '', raw_text.strip())
                        raw_text = re.sub(r'\s*```$', '', raw_text.strip())
                        data = json.loads(raw_text)
                        if "glossary" in data and isinstance(data["glossary"], list):
                            data["glossary"] = _sanitize_glossary_list(data["glossary"])
                        if data.get("overview") and "共记录" not in data.get("overview"):
                            return data
        except Exception as e:
            print(f"[Summarize] LLM error, falling back to semantic synthesizer: {e}")

    # Fallback glossary: 仅收录具有真实通俗解析的批注与内置专业词典，杜绝无解释空壳
    all_glossary = []
    seen_terms = set()
    for item in req.items:
        for a in item.get("annotations", []):
            t = (a.get("term") or "").strip()
            t_l = t.lower()
            if t and t_l not in seen_terms and t_l not in ENGLISH_STOP_WORDS and t_l not in NON_ACADEMIC_TERMS:
                exp = (a.get("explanation") or "").strip()
                if (not exp or "高频学术" in exp or "核心概念" in exp or len(exp) < 6) and t_l in BUILTIN_TECH_GLOSSARY:
                    exp = BUILTIN_TECH_GLOSSARY[t_l]["desc"]
                if exp and len(exp) >= 6 and "高频学术" not in exp:
                    seen_terms.add(t_l)
                    all_glossary.append({
                        "term_en": t,
                        "term_zh": a.get("type", "专业术语"),
                        "desc": exp
                    })
        for kw in clean_academic_keywords(item.get("keywords", [])):
            kw_l = kw.lower()
            if kw_l in BUILTIN_TECH_GLOSSARY and kw_l not in seen_terms:
                seen_terms.add(kw_l)
                entry = BUILTIN_TECH_GLOSSARY[kw_l]
                all_glossary.append({
                    "term_en": entry["term_en"],
                    "term_zh": entry["term_zh"],
                    "desc": entry["desc"]
                })

    # 提炼核心章节小标题
    headings = [it.get("section_heading") for it in req.items if it.get("section_heading")]
    unique_headings = []
    for h in headings:
        if h and h not in unique_headings:
            unique_headings.append(h)

    # 提取有实质内容的讲授要点
    sample_points = []
    for it in req.items:
        tr = (it.get("translation") or "").strip()
        if len(tr) >= 16 and not tr.startswith("好的") and not tr.startswith("而且") and "共记录" not in tr:
            clean_tr = tr.rstrip("。，,. ")
            if clean_tr and clean_tr not in sample_points:
                sample_points.append(clean_tr)
        if len(sample_points) >= 4:
            break

    terms_summary = [g["term_zh"] if g.get("term_zh") != "专业术语" else g["term_en"] for g in all_glossary[:5]]
    terms_str = "、".join(terms_summary) if terms_summary else "核心学术机制与技术推导"

    course_name = req.session_title or "课堂专题讲授"
    heading_clause = f"，系统贯穿了【{' / '.join(unique_headings[:3])}】等重要章节" if unique_headings else ""

    # P2: 真正的核心概览（绝不再出现“共记录 427 个知识意群”）
    overview_text = (
        f"本节课围绕【{course_name}】展开深入讲授{heading_clause}。老师重点剖析了关于【{terms_str}】的底层架构与实战机制，"
        f"深入探讨了其在工程实践与合规体系中的实施路径与边界条件。整堂课理论与应用并重，系统梳理了关键技术链路与考核要点。"
    )

    # P1: 核心要点清单（已彻底删除“建议对照下方卡片中的重点时间戳与 AI 批注进行逐段复习。”）
    takeaways = []
    if unique_headings:
        takeaways.append(f"核心授课模块演进：{' ➔ '.join(unique_headings[:4])}")
    if terms_summary:
        takeaways.append(f"重点概念与机制聚焦：{terms_str}")
    if sample_points:
        for p in sample_points[:3]:
            takeaways.append(f"课堂关键推导与结论：{p}")
    else:
        takeaways.append("重点掌握相关技术机制的定义范畴、触发条件与安全防护边界")
        takeaways.append("注意区分不同架构机制的适用场景与性能权衡（Trade-offs）")

    return {
        "overview": overview_text,
        "takeaways": takeaways[:5],
        "glossary": all_glossary[:6]
    }


def _heuristic_slide_overview(req: SlideOverviewRequest) -> Dict[str, Any]:
    pages = (req.pages or [])[:150]
    total = req.total_pages or len(pages)
    clean_name = re.sub(r'\.pdf$', '', (req.file_name or '').strip(), flags=re.IGNORECASE)

    titles = []
    all_text_parts = []
    objectives = []

    for idx, p in enumerate(pages, start=1):
        pg = int(p.get("page") or idx)
        title = (p.get("title") or "").strip()
        text = (p.get("text") or "").strip()
        if not title and text:
            first_line = next((ln.strip() for ln in re.split(r'[\n。\.!?]+', text) if len(ln.strip()) >= 3), "")
            title = first_line[:60]
        titles.append((pg, title or f"第 {pg} 页", text))
        all_text_parts.append(text[:400])

        if re.search(r'(objective|learning outcome|agenda|outline|goals?|教学目标|课程目标|大纲)', (title + " " + text[:200]), re.I):
            for ln in text.splitlines():
                s = re.sub(r'^[\s•\-–\d\.、]+', '', ln).strip()
                if 8 <= len(s) <= 110 and not re.search(r'^(objective|agenda|outline|learning)', s, re.I):
                    if s not in objectives:
                        objectives.append(s)
                if len(objectives) >= 3:
                    break

    course_topic = titles[0][1] if titles and len(titles[0][1]) >= 4 else (clean_name or "本节课程课件")
    if not objectives:
        non_trivial = [t[1] for t in titles[1:] if len(t[1]) >= 4][:3]
        if non_trivial:
            objectives = [f"掌握「{t}」的核心概念与基本思路" for t in non_trivial]
        else:
            objectives = [
                f"理解《{course_topic}》的主线结构与核心概念",
                "结合教授课堂口述补充，理清各页之间的推导与案例联系",
            ]

    # Group slides into 3–5 readable page-range outline blocks
    outline = []
    if titles:
        chunk_count = min(5, max(1, (len(titles) + 4) // 5))
        chunk_size = max(1, (len(titles) + chunk_count - 1) // chunk_count)
        for start in range(0, len(titles), chunk_size):
            grp = titles[start:start + chunk_size]
            p_start, p_end = grp[0][0], grp[-1][0]
            page_label = f"P{p_start}" if p_start == p_end else f"P{p_start}–P{p_end}"
            lead_title = grp[0][1][:42]
            sub_titles = [g[1][:26] for g in grp[1:3] if g[1] and g[1] != grp[0][1]]
            brief = f"涵盖 {' / '.join(sub_titles)}" if sub_titles else (grp[0][2][:60].replace('\n', ' ') or "本段核心概念讲解")
            outline.append({"pages": page_label, "topic": lead_title, "brief": brief})

    # Extract key terms from builtin tech glossary + capitalized acronyms
    combined_lower = "\n".join(all_text_parts).lower()
    key_terms = []
    seen_terms = set()
    for k, entry in BUILTIN_TECH_GLOSSARY.items():
        if re.search(r'\b' + re.escape(k) + r'\b', combined_lower):
            if k not in seen_terms:
                seen_terms.add(k)
                key_terms.append({
                    "term": entry["term_en"],
                    "zh": entry["term_zh"],
                    "note": entry["desc"][:55]
                })
        if len(key_terms) >= 6:
            break

    if len(key_terms) < 4:
        acronyms = re.findall(r'\b[A-Z]{2,6}\b', "\n".join(all_text_parts))
        freq: Dict[str, int] = {}
        for ac in acronyms:
            if ac.lower() not in ENGLISH_STOP_WORDS and ac not in {"PDF", "PPT", "THE", "AND", "FOR", "WITH", "FROM", "THIS", "THAT"}:
                freq[ac] = freq.get(ac, 0) + 1
        for ac, _ in sorted(freq.items(), key=lambda x: -x[1]):
            if ac.lower() not in seen_terms:
                seen_terms.add(ac.lower())
                key_terms.append({"term": ac, "zh": "课件高频缩写", "note": "上课时可重点留意教授对该缩写的定义与场景说明"})
            if len(key_terms) >= 5:
                break

    return {
        "course_topic": course_topic,
        "total_pages": total,
        "objectives": objectives[:3],
        "outline": outline[:5],
        "key_terms": key_terms[:6],
        "source": "heuristic",
    }


@app.post("/api/slide-overview")
async def handle_slide_overview(req: SlideOverviewRequest, request: Request, _rl=Depends(check_rate_limit)):
    log_event_bg("pdf_upload", request)
    pages = (req.pages or [])[:150]
    if not pages:
        raise HTTPException(status_code=400, detail="未收到课件页面文本")

    compact_lines = []
    for idx, p in enumerate(pages, start=1):
        pg = int(p.get("page") or idx)
        title = (p.get("title") or "").strip()[:80]
        text = re.sub(r'\s+', ' ', (p.get("text") or "").strip())[:260]
        compact_lines.append(f"[P{pg}] {title} | {text}")
    slides_digest = "\n".join(compact_lines)[:12000]

    prompt = f"""你是高校课堂学习助教。学生刚上传了一份共 {req.total_pages or len(pages)} 页的课程 Slide（文件名：{req.file_name or '未命名'}）。
请快速给出一份**简短、清晰、20 秒能扫完**的课前课件总览，严格返回如下 JSON（所有解释用简体中文，严禁冗长）：
{{
  "course_topic": "一句话点明本节课件主题（25字以内）",
  "objectives": ["教学目标1（简短一句）", "教学目标2", "教学目标3"],
  "outline": [
    {{"pages": "P1-P4", "topic": "模块小标题", "brief": "一句话说清这几页讲什么（30字以内）"}}
  ],
  "key_terms": [
    {{"term": "英文术语", "zh": "中文名", "note": "一句话大白话说明（30字以内）"}}
  ]
}}
要求：
1. objectives 严格 2-3 条；outline 严格 3-5 段覆盖全部页码；key_terms 严格 3-6 个本课件真正出现的核心专有名词。
2. 直接说干货，不要写客套话或统计页数字数。

课件各页摘录：
{slides_digest}
"""

    creds = await resolve_ai_credentials(
        request, req.api_key, req.provider, req.model_name, req.custom_endpoint, "overview"
    )
    api_key = creds["api_key"]
    provider = creds["provider"]
    model = creds["model_name"]
    custom_ep = creds["custom_endpoint"]

    if api_key and provider in ["gemini", "openai_compatible", "deepseek"]:
        try:
            if provider == "gemini":
                raw_json = await execute_gemini_call(api_key, model, prompt, is_json=True)
                data = json.loads(raw_json)
            else:
                endpoint = validate_custom_endpoint(custom_ep) + "/chat/completions"
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a concise university lecture assistant. Return valid JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"}
                }
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(endpoint, headers=headers, json=payload)
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    raw_text = resp.json()["choices"][0]["message"]["content"]
                    raw_text = re.sub(r'^```json\s*', '', raw_text.strip())
                    raw_text = re.sub(r'\s*```$', '', raw_text.strip())
                    data = json.loads(raw_text)

            if isinstance(data, dict) and data.get("objectives"):
                data["total_pages"] = req.total_pages or len(pages)
                data["source"] = "ultra_llm" if creds["via_ultra_quota"] else "llm"
                return data
        except Exception as e:
            print(f"[SlideOverview] LLM error, falling back to heuristic: {e}")

    out = _heuristic_slide_overview(req)
    if creds.get("quota_notice"):
        out["quota_notice"] = creds["quota_notice"]
    return out


def _heuristic_slide_page_summary(req: SlidePageSummaryRequest) -> Dict[str, Any]:
    title = (req.slide_title or "").strip()
    slide_clean = re.sub(r'\s+', ' ', (req.slide_text or "").strip())
    spoken_zh = [
        (it.get("translation") or "").strip()
        for it in (req.spoken_items or [])
        if (it.get("translation") or "").strip() and "翻译失败" not in (it.get("translation") or "")
    ]
    spoken_en = [
        (it.get("source") or "").strip()
        for it in (req.spoken_items or [])
        if (it.get("source") or "").strip()
    ]

    if title:
        headline = f"本页围绕「{title[:40]}」展开讲解。"
    elif spoken_zh:
        headline = spoken_zh[0][:56].rstrip("，, ") + "。"
    elif slide_clean:
        headline = f"本页核心内容：{slide_clean[:56]}"
    else:
        headline = f"第 {req.page} 页课堂内容已归档。"

    additions = []
    for zh in spoken_zh:
        s = zh.strip()
        if len(s) >= 12 and s not in additions:
            additions.append(s[:85])
        if len(additions) >= 3:
            break
    if not additions:
        additions = ["本页暂无足够教授口述记录，可继续收音或点击「重新总结」补充。"]

    combined = (slide_clean + " " + " ".join(spoken_en)).lower()
    keywords = []
    for k, entry in BUILTIN_TECH_GLOSSARY.items():
        if re.search(r'\b' + re.escape(k) + r'\b', combined):
            keywords.append(entry["term_en"])
        if len(keywords) >= 4:
            break
    if not keywords:
        for ac in re.findall(r'\b[A-Z]{2,6}\b', slide_clean + " " + " ".join(spoken_en)):
            if ac not in keywords and ac.lower() not in ENGLISH_STOP_WORDS:
                keywords.append(ac)
            if len(keywords) >= 4:
                break

    return {
        "page": req.page,
        "headline": headline,
        "professor_additions": additions,
        "keywords": keywords[:4],
        "spoken_count": len(req.spoken_items or []),
        "source": "heuristic",
    }


@app.post("/api/slide-page-summary")
async def handle_slide_page_summary(req: SlidePageSummaryRequest, request: Request, _rl=Depends(check_rate_limit)):
    spoken = (req.spoken_items or [])[:25]
    slide_text = (req.slide_text or "").strip()[:1400]
    spoken_lines = []
    for it in spoken:
        t = it.get("time_str") or ""
        src = (it.get("source") or "").strip()[:220]
        zh = (it.get("translation") or "").strip()[:220]
        if src or zh:
            spoken_lines.append(f"[{t}] 原声: {src} | 译文: {zh}")
    spoken_block = "\n".join(spoken_lines) or "（本页暂无口述转写，请仅依据 Slide 原文提炼）"

    prompt = f"""你是高校课堂双语助教。教授刚刚讲完《{req.course_title or '当前课程'}》的第 {req.page} 页 Slide（共 {req.total_pages or '?'} 页）。
请把「这一页 Slide 原文」和「教授讲这页时的口述内容」融合，输出一份**极简、抓重点**的单页综合总结，严格返回 JSON（简体中文）：
{{
  "headline": "一句话说清这一页的核心结论（45字以内）",
  "professor_additions": [
    "教授口述中额外强调的例子、推导、对比或考点提示1（45字以内）",
    "教授口述补充2（如无额外补充，提炼本页最关键的1条理解要点）"
  ],
  "keywords": ["关键词1", "关键词2", "关键词3"]
}}
要求：不要复述整页原文，重点突出「教授嘴上补充了什么 / 这页考什么」。

【第 {req.page} 页 Slide 原文】：
{slide_text or '（无提取文本，可能是纯图表页）'}

【教授讲这一页时的口述记录】：
{spoken_block}
"""

    creds = await resolve_ai_credentials(
        request, req.api_key, req.provider, req.model_name, req.custom_endpoint, "slide_summary"
    )
    api_key = creds["api_key"]
    provider = creds["provider"]
    model = creds["model_name"]
    custom_ep = creds["custom_endpoint"]

    if api_key and provider in ["gemini", "openai_compatible", "deepseek"]:
        try:
            if provider == "gemini":
                raw_json = await execute_gemini_call(api_key, model, prompt, is_json=True)
                data = json.loads(raw_json)
            else:
                endpoint = validate_custom_endpoint(custom_ep) + "/chat/completions"
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": "You are a concise university lecture assistant. Return valid JSON only."},
                        {"role": "user", "content": prompt}
                    ],
                    "response_format": {"type": "json_object"}
                }
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(endpoint, headers=headers, json=payload)
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    raw_text = resp.json()["choices"][0]["message"]["content"]
                    raw_text = re.sub(r'^```json\s*', '', raw_text.strip())
                    raw_text = re.sub(r'\s*```$', '', raw_text.strip())
                    data = json.loads(raw_text)

            if isinstance(data, dict) and data.get("headline"):
                return {
                    "page": req.page,
                    "headline": str(data.get("headline", "")).strip(),
                    "professor_additions": [str(x).strip() for x in (data.get("professor_additions") or []) if str(x).strip()][:3],
                    "keywords": [str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()][:4],
                    "spoken_count": len(spoken),
                    "source": "ultra_llm" if creds["via_ultra_quota"] else "llm",
                }
        except Exception as e:
            print(f"[SlidePageSummary] LLM error, falling back to heuristic: {e}")

    out = _heuristic_slide_page_summary(req)
    if creds.get("quota_notice"):
        out["quota_notice"] = creds["quota_notice"]
    return out

@app.post("/api/export/docx")
async def export_docx(session: SessionData, _rl=Depends(check_rate_limit)):
    if len(session.items) > MAX_SESSION_ITEMS:
        raise HTTPException(status_code=413, detail=f"课堂记录条目过多（上限 {MAX_SESSION_ITEMS} 条）")
    doc = Document()

    title_p = doc.add_paragraph()
    title_run = title_p.add_run(f"📚 {session.title}")
    title_run.font.size = Pt(20)
    title_run.font.bold = True
    title_run.font.color.rgb = RGBColor(30, 41, 59)
    title_p.paragraph_format.space_after = Pt(4)

    meta_p = doc.add_paragraph()
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session.created_at))
    duration_min = session.duration_seconds // 60
    duration_sec = session.duration_seconds % 60
    meta_run = meta_p.add_run(f"记录时间: {time_str} | 课程时长: {duration_min}分{duration_sec}秒 | 语言: {session.source_lang.upper()} ➔ {session.target_lang.upper()} | 知识卡片数: {len(session.items)}")
    meta_run.font.size = Pt(10)
    meta_run.font.color.rgb = RGBColor(100, 116, 139)
    meta_p.paragraph_format.space_after = Pt(18)

    if session.slide_overview:
        doc.add_heading("📑 课件总览（教学目标与内容脉络）", level=1)
        ov = session.slide_overview
        if ov.get("course_topic"):
            tp = doc.add_paragraph()
            tp.add_run("【本节主题】 ").bold = True
            tp.add_run(str(ov.get("course_topic")))
        if ov.get("objectives"):
            doc.add_paragraph("【教学目标】:").bold = True
            for obj in ov.get("objectives", []):
                doc.add_paragraph(f"• {obj}")
        if ov.get("outline"):
            doc.add_paragraph("【主要内容脉络】:").bold = True
            for sec in ov.get("outline", []):
                doc.add_paragraph(f"• {sec.get('pages', '')} {sec.get('topic', '')}：{sec.get('brief', '')}")

    if session.slide_summaries:
        doc.add_heading("📌 逐页 Slide + 教授口述综合总结", level=1)
        def _pg_sort_key(k):
            try:
                return int(k)
            except Exception:
                return 9999
        for pg_key in sorted(session.slide_summaries.keys(), key=_pg_sort_key):
            ps = session.slide_summaries[pg_key]
            if not isinstance(ps, dict):
                continue
            p_h = doc.add_paragraph()
            p_h.add_run(f"Slide P{pg_key}｜").bold = True
            p_h.add_run(ps.get("headline", ""))
            for add in ps.get("professor_additions", []):
                doc.add_paragraph(f"   - {add}")

    if session.summary:
        sum_h = doc.add_heading("🎯 课堂重点精要与板书提炼", level=1)
        sum_h.style.font.color.rgb = RGBColor(31, 45, 230)
        
        ov_p = doc.add_paragraph()
        ov_p.add_run("【核心概览】 ").bold = True
        ov_p.add_run(session.summary.get("overview", ""))
        ov_p.paragraph_format.space_after = Pt(8)

        takeaways = session.summary.get("takeaways", [])
        if takeaways:
            doc.add_paragraph("【关键要点清单】:").bold = True
            for tk in takeaways:
                doc.add_paragraph(f"• {tk}")

        glossary = session.summary.get("glossary", [])
        if glossary:
            doc.add_paragraph("【核心术语对照表】:").bold = True
            t_table = doc.add_table(rows=1, cols=3)
            t_table.alignment = WD_TABLE_ALIGNMENT.CENTER
            hdr_cells = t_table.rows[0].cells
            hdr_cells[0].text = "英文术语"
            hdr_cells[1].text = "中文翻译"
            hdr_cells[2].text = "概念解析"
            for g in glossary:
                row_cells = t_table.add_row().cells
                row_cells[0].text = g.get("term_en", "")
                row_cells[1].text = g.get("term_zh", "")
                row_cells[2].text = g.get("desc", "")
            doc.add_paragraph().paragraph_format.space_after = Pt(14)

    doc.add_heading("📝 结构化双语对照实录与 AI 讲解", level=1)

    table = doc.add_table(rows=1, cols=3)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    hdr = table.rows[0].cells
    hdr[0].width = Inches(1.1)
    hdr[0].text = "时间轴"
    hdr[1].width = Inches(2.7)
    hdr[1].text = "讲师原话 (过滤口语噪声)"
    hdr[2].width = Inches(3.0)
    hdr[2].text = "中文精译与 AI 深度讲解"

    for c in hdr:
        for p in c.paragraphs:
            p.runs[0].font.bold = True
            p.runs[0].font.color.rgb = RGBColor(255, 255, 255)
        tcPr = c._element.get_or_add_tcPr()
        shd = OxmlElement('w:shd')
        shd.set(qn('w:val'), 'clear')
        shd.set(qn('w:color'), 'auto')
        shd.set(qn('w:fill'), '1F2DE6')
        tcPr.append(shd)

    sorted_items = sorted(session.items, key=lambda x: (x.get('time_sec', 0), x.get('seq', 0)))
    for item in sorted_items:
        row = table.add_row()
        c0, c1, c2 = row.cells
        c0.width = Inches(1.1)
        c1.width = Inches(2.7)
        c2.width = Inches(3.0)

        star = " ⭐" if item.get("starred") else ""
        slide_tag = f"\n[P{item['slide_page']}]" if item.get("slide_page") else ""
        c0.text = f"⏱ {item.get('time_str', '00:00')}{star}{slide_tag}"
        c0.paragraphs[0].runs[0].font.size = Pt(9.5)
        c0.paragraphs[0].runs[0].font.color.rgb = RGBColor(100, 116, 139)

        src_text = item.get("cleaned_source") or item.get("source_text", "")
        c1.text = src_text
        c1.paragraphs[0].runs[0].font.size = Pt(10)

        c2.text = item.get("translation", "")
        c2.paragraphs[0].runs[0].font.size = Pt(10)
        c2.paragraphs[0].runs[0].font.color.rgb = RGBColor(30, 41, 59)

        annos = item.get("annotations", [])
        if annos:
            p_anno = c2.add_paragraph()
            r_head = p_anno.add_run("💡 AI 背景讲解:")
            r_head.bold = True
            r_head.font.size = Pt(8.5)
            r_head.font.color.rgb = RGBColor(79, 70, 229)
            for a in annos:
                pa = c2.add_paragraph()
                ra_term = pa.add_run(f"• [{a.get('type', '术语')}] {a.get('term', '')}: ")
                ra_term.bold = True
                ra_term.font.size = Pt(8.5)
                ra_exp = pa.add_run(a.get('explanation', ''))
                ra_exp.font.size = Pt(8.5)
                ra_exp.font.color.rgb = RGBColor(71, 85, 105)

        if item.get("section_heading"):
            p_head = c2.add_paragraph()
            r_head = p_head.add_run(f"📌 主题: {item['section_heading']}")
            r_head.bold = True
            r_head.font.size = Pt(9)
            r_head.font.color.rgb = RGBColor(79, 70, 229)

    # Document Watermark & Anti-Plagiarism Notice
    footer_p = doc.add_paragraph()
    footer_p.paragraph_format.space_before = Pt(24)
    footer_run = footer_p.add_run("──────────────────────────────────────────────────\n📝 本笔记由 Anti Lecture 智能生成 | 开发者: Blueberry (@lyang091126-cmd) | 享有原创版权保护 • 未经许可禁止商业剽窃")
    footer_run.font.size = Pt(8.5)
    footer_run.font.italic = True
    footer_run.font.color.rgb = RGBColor(148, 163, 184)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)

    safe_title = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', session.title)
    encoded_fn = quote(f"{safe_title}.docx")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"}
    )

@app.post("/api/export/markdown")
async def export_markdown(session: SessionData, _rl=Depends(check_rate_limit)):
    if len(session.items) > MAX_SESSION_ITEMS:
        raise HTTPException(status_code=413, detail=f"课堂记录条目过多（上限 {MAX_SESSION_ITEMS} 条）")
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session.created_at))
    duration_min = session.duration_seconds // 60
    duration_sec = session.duration_seconds % 60

    md = [
        f"# 📚 {session.title}",
        f"> **记录时间**: {time_str} | **课程时长**: {duration_min}分{duration_sec}秒 | **语言**: {session.source_lang.upper()} ➔ {session.target_lang.upper()}\n",
        "---",
    ]

    if session.slide_overview:
        ov = session.slide_overview
        md.append("## 📑 课件总览（教学目标与内容脉络）\n")
        if ov.get("course_topic"):
            md.append(f"**本节主题**：{ov.get('course_topic')}\n")
        if ov.get("objectives"):
            md.append("**教学目标**：")
            for obj in ov.get("objectives", []):
                md.append(f"- {obj}")
            md.append("")
        if ov.get("outline"):
            md.append("**主要内容脉络**：")
            for sec in ov.get("outline", []):
                md.append(f"- **{sec.get('pages', '')} {sec.get('topic', '')}**：{sec.get('brief', '')}")
            md.append("")

    if session.slide_summaries:
        md.append("## 📌 逐页 Slide + 教授口述综合总结\n")
        def _pg_sort_md(k):
            try:
                return int(k)
            except Exception:
                return 9999
        for pg_key in sorted(session.slide_summaries.keys(), key=_pg_sort_md):
            ps = session.slide_summaries[pg_key]
            if not isinstance(ps, dict):
                continue
            md.append(f"### Slide P{pg_key}")
            md.append(f"- **核心结论**：{ps.get('headline', '')}")
            for add in ps.get("professor_additions", []):
                md.append(f"- **口述补充**：{add}")
            md.append("")

    if session.summary:
        md.append("## 🎯 课堂精要提炼与板书\n")
        md.append(f"**核心概览**:\n{session.summary.get('overview', '')}\n")
        takeaways = session.summary.get("takeaways", [])
        if takeaways:
            md.append("**核心要点清单**:")
            for tk in takeaways:
                md.append(f"- {tk}")
            md.append("")
        glossary = session.summary.get("glossary", [])
        if glossary:
            md.append("**专有名词对照表**:\n")
            md.append("| 英文术语 | 中文翻译 | 概念解析 |")
            md.append("| :--- | :--- | :--- |")
            for g in glossary:
                md.append(f"| `{g.get('term_en','')}` | **{g.get('term_zh','')}** | {g.get('desc','')} |")
            md.append("\n---\n")

    md.append("## 📝 课堂双语卡片实录与 AI 智能讲解\n")

    sorted_items = sorted(session.items, key=lambda x: (x.get('time_sec', 0), x.get('seq', 0)))
    for idx, item in enumerate(sorted_items, 1):
        star = " ⭐ (重点复习)" if item.get("starred") else ""
        time_badge = item.get("time_str", "00:00")
        heading = item.get("section_heading")
        if heading:
            md.append(f"\n### 📌 {heading}\n")

        src = item.get("cleaned_source") or item.get("source_text", "")
        tr = item.get("translation", "")
        kws = item.get("keywords", [])
        kw_tags = " ".join([f"`#{kw}`" for kw in kws]) if kws else ""

        md.append(f"#### [{time_badge}] 知识点 #{item.get('seq', idx)}{star}")
        md.append(f"**讲师原声**: {src}\n")
        md.append(f"**中文精译**: {tr}\n")

        annos = item.get("annotations", [])
        if annos:
            md.append("> 💡 **AI 智能讲解与背景批注**:")
            for a in annos:
                md.append(f"> - **`{a.get('term', '')}`** *[{a.get('type', '术语')}]*: {a.get('explanation', '')}")
            md.append("")

        if kw_tags:
            md.append(f"🏷️ **核心术语**: {kw_tags}\n")
        md.append("")

    # Markdown Watermark
    md.append("\n---\n")
    md.append("> 🛡️ **版权与防伪水印**：本课堂双语实录由 [Anti Lecture](https://github.com/lyang091126-cmd/lecture-scribe) 智能生成  \n> **原作者**: Blueberry ([@lyang091126-cmd](https://github.com/lyang091126-cmd)) | 版权所有 © 2026 | 受原创版权法保护，未经许可禁止二次打包转售或商业剽窃")

    content = "\n".join(md)
    safe_title = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', session.title)
    encoded_fn = quote(f"{safe_title}.md")
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"}
    )

@app.post("/api/export/srt")
async def export_srt(session: SessionData, _rl=Depends(check_rate_limit)):
    if len(session.items) > MAX_SESSION_ITEMS:
        raise HTTPException(status_code=413, detail=f"课堂记录条目过多（上限 {MAX_SESSION_ITEMS} 条）")
    sorted_items = sorted(session.items, key=lambda x: (x.get('time_sec', 0), x.get('seq', 0)))
    srt_lines = []

    def format_srt_time(seconds: float) -> str:
        ms = int((seconds % 1) * 1000)
        s = int(seconds)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    for i, item in enumerate(sorted_items, 1):
        start_t = float(item.get("time_sec", (i - 1) * 5))
        if i < len(sorted_items):
            next_t = float(sorted_items[i].get("time_sec", start_t + 5))
            end_t = min(next_t - 0.2, start_t + 10)
        else:
            end_t = start_t + 5.0
        if end_t <= start_t:
            end_t = start_t + 4.0

        src = item.get("cleaned_source") or item.get("source_text", "")
        tr = item.get("translation", "")

        srt_lines.append(str(i))
        srt_lines.append(f"{format_srt_time(start_t)} --> {format_srt_time(end_t)}")
        srt_lines.append(f"{tr}")
        if src:
            srt_lines.append(f"{src}")
        srt_lines.append("")

    if sorted_items:
        last_end = float(sorted_items[-1].get("time_sec", len(sorted_items) * 5.0)) + 5.0
        srt_lines.append(str(len(sorted_items) + 1))
        srt_lines.append(f"{format_srt_time(last_end)} --> {format_srt_time(last_end + 3.5)}")
        srt_lines.append("【Anti Lecture 智能双语笔记 • Author: Blueberry (@lyang091126-cmd)】")
        srt_lines.append("")

    content = "\n".join(srt_lines)
    safe_title = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', session.title)
    encoded_fn = quote(f"{safe_title}.srt")
    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"}
    )

# Once the database is unreachable, every request would otherwise sit through
# its own connection timeout. Remember the failure briefly and answer at once.
STORAGE_COOLDOWN_SECONDS = 30
_storage_down_until = [0.0]
STORAGE_DOWN_MESSAGE = "云端课堂历史暂时不可用（不影响本地记录与实时翻译）"

def storage_unavailable(e: Exception) -> HTTPException:
    _storage_down_until[0] = time.time() + STORAGE_COOLDOWN_SECONDS
    print(f"[Sessions] storage error: {type(e).__name__}: {e}")
    return HTTPException(status_code=503, detail=STORAGE_DOWN_MESSAGE)

def ensure_storage_ready():
    if time.time() < _storage_down_until[0]:
        raise HTTPException(status_code=503, detail=STORAGE_DOWN_MESSAGE)

@app.get("/api/health")
async def health():
    """Says which storage backend is actually in use. Without a database the
    lecture history silently disappears on every restart, so this needs to be
    checkable from outside rather than buried in startup logs."""
    storage = "postgres" if db.using_postgres else "ephemeral-sqlite"
    reachable = True
    detail = db.init_error
    try:
        await db.list_sessions("__healthcheck__")
    except Exception as e:
        reachable = False
        detail = f"{type(e).__name__}: {e}"[:300]
    return {
        "status": "ok" if reachable else "degraded",
        "storage": storage,
        "storage_reachable": reachable,
        "storage_error": detail,
        "durable": bool(db.using_postgres and reachable),
        "retention_days": SESSION_RETENTION_SECONDS // 86400,
    }

class AuthRequest(BaseModel):
    username: str
    password: str


class RedeemRequest(BaseModel):
    code: str


class AdminCreateCodesRequest(BaseModel):
    plan: str = "pro"  # pro | ultra
    count: int = 5
    duration_days: int = 30


class PaymentClaimRequest(BaseModel):
    plan: str = "pro"  # pro | ultra
    pay_method: str = "wechat"  # wechat | alipay
    receipt_note: str = ""


class ClaimReviewRequest(BaseModel):
    action: str  # approve | reject
    duration_days: int = 30


class AdminSetPlanRequest(BaseModel):
    plan: str  # free | pro | ultra
    duration_days: int = 30


class TrackEventRequest(BaseModel):
    event_type: str


ALLOWED_CLIENT_TRACK_EVENTS = {"page_landing", "page_app", "pdf_upload", "export_use"}


def log_event_bg(event_type: str, request: Request, user_id: Optional[str] = None) -> None:
    try:
        cid = request.headers.get("x-client-id", "").strip()[:64]
        ip = get_client_ip(request)[:64]
        asyncio.create_task(db.record_usage_event(event_type, cid, ip, user_id))
    except Exception:
        pass


def require_admin(request: Request) -> None:
    env_secret = os.environ.get("ADMIN_SECRET", "").strip()
    is_render = bool(os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL"))
    effective_secret = env_secret or ("" if is_render else "antilecture-admin")
    provided = request.headers.get("x-admin-secret", "").strip()
    if not effective_secret or not hmac.compare_digest(provided, effective_secret):
        raise HTTPException(status_code=403, detail="管理密钥错误或未配置 ADMIN_SECRET")


@app.post("/api/track")
async def track_event(req: TrackEventRequest, request: Request, _rl=Depends(check_rate_limit)):
    ev = (req.event_type or "").strip()
    if ev not in ALLOWED_CLIENT_TRACK_EVENTS:
        return {"status": "ignored"}
    user = await get_optional_user(request)
    log_event_bg(ev, request, user["id"] if user else None)
    return {"status": "ok"}


@app.post("/api/billing/claim")
async def billing_claim(req: PaymentClaimRequest, request: Request, _rl=Depends(check_rate_limit)):
    """Scan-to-pay instant access: user submits WeChat/Alipay receipt note, gets
    immediate 48h Pro/Ultra access on clean accounts, and author confirms to 30 days."""
    ensure_storage_ready()
    user = await get_optional_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先注册或登录账号后再提交付款申请")
    note = (req.receipt_note or "").strip() or f"已备注用户名：{user['username']}"
    try:
        claim, updated_user, temp_granted = await db.submit_payment_claim(
            user["id"], req.plan, req.pay_method, note
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise storage_unavailable(e)

    plan_label = "Ultra" if claim["plan"] == "ultra" else "Pro"
    msg = (
        f"已收到你的付款确认，{plan_label} 会员权限已为你开启（核验到账后自动生效完整 30 天）。"
        if temp_granted
        else f"已收到你的 {plan_label} 付款提交，核验到账后将为你开通 30 天会员。"
    )
    return {
        "status": "ok",
        "claim": claim,
        "user": updated_user,
        "temp_granted": temp_granted,
        "message": msg,
    }


@app.get("/api/admin/overview")
async def admin_overview(request: Request):
    require_admin(request)
    ensure_storage_ready()
    try:
        return await db.get_admin_overview()
    except Exception as e:
        raise storage_unavailable(e)


@app.post("/api/admin/claims/{claim_id}/review")
async def admin_review_claim(claim_id: str, req: ClaimReviewRequest, request: Request):
    require_admin(request)
    ensure_storage_ready()
    try:
        claim, updated_user = await db.review_payment_claim(claim_id, req.action, req.duration_days)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise storage_unavailable(e)
    return {"status": "ok", "claim": claim, "user": updated_user}


@app.post("/api/admin/users/{user_id}/plan")
async def admin_set_user_plan(user_id: str, req: AdminSetPlanRequest, request: Request):
    require_admin(request)
    ensure_storage_ready()
    try:
        updated_user = await db.set_user_plan_manual(user_id, req.plan, req.duration_days)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise storage_unavailable(e)
    return {"status": "ok", "user": updated_user}


@app.post("/api/auth/register")
async def auth_register(req: AuthRequest, request: Request, _rl=Depends(check_rate_limit)):
    ensure_storage_ready()
    username = (req.username or "").strip()
    password = req.password or ""
    if not USERNAME_RE.match(username):
        raise HTTPException(status_code=400, detail="用户名需为 2~24 位中英文、数字或下划线")
    if len(password) < 6 or len(password) > 128:
        raise HTTPException(status_code=400, detail="密码长度需为 6~128 位")

    # Tight per-IP registration throttle (max 10 accounts/min per IP)
    ip = get_client_ip(request)
    if not _take_rate_token(f"reg:{ip}", 10, time.time()):
        raise HTTPException(status_code=429, detail="注册过于频繁，请稍后再试")

    try:
        user = await db.create_user(username, hash_password(password))
    except ValueError as ve:
        if str(ve) == "USERNAME_EXISTS":
            raise HTTPException(status_code=409, detail="该用户名已被注册，请换一个或直接登录")
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        raise storage_unavailable(e)

    # Claim any guest sessions from this browser under the new account
    cid = request.headers.get("x-client-id", "").strip()
    if cid and SAFE_ID_RE.match(cid):
        try:
            await db.claim_browser_sessions(cid, f"user:{user['id']}")
        except Exception:
            pass

    token = issue_auth_token(user["id"])
    return {"token": token, "user": user}


@app.post("/api/auth/login")
async def auth_login(req: AuthRequest, request: Request, _rl=Depends(check_rate_limit)):
    ensure_storage_ready()
    username = (req.username or "").strip()
    password = req.password or ""
    if not username or not password:
        raise HTTPException(status_code=400, detail="请输入用户名和密码")

    try:
        row = await db.get_user_by_username(username)
    except Exception as e:
        raise storage_unavailable(e)

    if row is None or not verify_password(password, row.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码不正确")

    user = db.user_to_dict(row)
    cid = request.headers.get("x-client-id", "").strip()
    if cid and SAFE_ID_RE.match(cid):
        try:
            await db.claim_browser_sessions(cid, f"user:{user['id']}")
        except Exception:
            pass

    token = issue_auth_token(user["id"])
    return {"token": token, "user": user}


@app.get("/api/auth/me")
async def auth_me(request: Request, _rl=Depends(check_rate_limit)):
    user = await get_optional_user(request)
    return {"user": user}


@app.post("/api/billing/redeem")
async def billing_redeem(req: RedeemRequest, request: Request, _rl=Depends(check_rate_limit)):
    ensure_storage_ready()
    user = await get_optional_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="请先注册或登录账号后再激活会员")
    code = (req.code or "").strip()
    if not code or len(code) < 6:
        raise HTTPException(status_code=400, detail="请输入有效的 Pro / Ultra 激活码")
    try:
        updated_user, target_plan, days = await db.redeem_code(user["id"], code)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise storage_unavailable(e)
    return {
        "status": "ok",
        "plan": target_plan,
        "duration_days": days,
        "user": updated_user,
    }


@app.post("/api/admin/codes")
async def admin_create_codes(req: AdminCreateCodesRequest, request: Request):
    require_admin(request)
    codes = await db.create_redemption_codes(req.plan, req.count, req.duration_days)
    return {"plan": req.plan, "duration_days": req.duration_days, "codes": codes}


@app.get("/api/sessions")
async def list_sessions(owner_key: str = Depends(get_owner_key), _rl=Depends(check_rate_limit)):
    ensure_storage_ready()
    await maybe_purge_expired_sessions()
    try:
        return await db.list_sessions(owner_key)
    except Exception as e:
        raise storage_unavailable(e)

@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str, owner_key: str = Depends(get_owner_key), _rl=Depends(check_rate_limit)):
    ensure_storage_ready()
    if not SAFE_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    try:
        data = await db.get_session(owner_key, session_id)
    except Exception as e:
        raise storage_unavailable(e)
    if data is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return data

@app.post("/api/sessions")
async def save_session(session: SessionData, owner_key: str = Depends(get_owner_key), _rl=Depends(check_rate_limit)):
    ensure_storage_ready()
    if len(session.items) > MAX_SESSION_ITEMS:
        raise HTTPException(status_code=413, detail=f"课堂记录条目过多（上限 {MAX_SESSION_ITEMS} 条）")

    session_id = session.id if session.id and SAFE_ID_RE.match(session.id) else uuid.uuid4().hex
    payload = session.dict() if hasattr(session, "dict") else session.model_dump()
    payload["id"] = session_id

    try:
        await db.save_session(owner_key, session_id, payload)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not your session")
    except Exception as e:
        raise storage_unavailable(e)

    return {"status": "ok", "id": session_id}

@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str, owner_key: str = Depends(get_owner_key), _rl=Depends(check_rate_limit)):
    ensure_storage_ready()
    if not SAFE_ID_RE.match(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")
    try:
        await db.delete_session(owner_key, session_id)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Not your session")
    except Exception as e:
        raise storage_unavailable(e)
    return {"status": "ok"}

@app.get("/admin")
async def admin_redirect():
    return RedirectResponse(url="/admin/", status_code=307)

@app.get("/app")
async def app_redirect():
    return RedirectResponse(url="/app/", status_code=307)

(BASE_DIR / "static" / "admin").mkdir(parents=True, exist_ok=True)
app.mount("/admin", StaticFiles(directory=str(BASE_DIR / "static" / "admin"), html=True), name="admin")
app.mount("/app", StaticFiles(directory=str(BASE_DIR / "static" / "app"), html=True), name="workbench")
app.mount("/", StaticFiles(directory=str(BASE_DIR / "static" / "landing"), html=True), name="landing")

if __name__ == "__main__":
    import asyncio
    import sys
    import uvicorn

    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"Starting Anti Lecture server on {host}:{port}")

    if sys.platform == "win32":
        # psycopg's async mode refuses to run on Windows' default Proactor
        # loop, and uvicorn.run() installs one whatever the policy says, so
        # drive the server from a selector loop of our own instead.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(uvicorn.Server(uvicorn.Config(app, host=host, port=port)).serve())
    else:
        uvicorn.run(app, host=host, port=port)
