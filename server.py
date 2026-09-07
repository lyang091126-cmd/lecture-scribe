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
import asyncio
import time
import io
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
import httpx
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

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

DATA_DIR = BASE_DIR / "data"
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="LectureScribe - 课堂智能双语速记与排版系统")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    """Ensures Gemini model name strictly conforms to current Google API specs (gemini-3.6-flash / 3.8-flash)."""
    if not model_name:
        return "gemini-3.7-flash"
    m = model_name.strip()
    m = re.sub(r'^models/', '', m)
    m_lower = m.lower().replace(" ", "-")
    if "3.8" in m_lower:
        return "gemini-3.8-flash"
    elif "3.7" in m_lower:
        return "gemini-3.7-flash"
    elif "3.6" in m_lower:
        return "gemini-3.6-flash"
    elif "3.5" in m_lower:
        return "gemini-3.5-flash"
    elif "1.5" in m_lower or "2.0" in m_lower or "2.5" in m_lower:
        return "gemini-3.7-flash"
    clean_m = re.sub(r'[^a-zA-Z0-9\._-]', '', m)
    return clean_m or "gemini-3.7-flash"

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

async def translate_single_chunk(text: str, source_lang: str, target_lang: str) -> str:
    # 1. Google Clients5 API (极速高可用，零限流)
    try:
        url = f"https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=zh-CN&q={quote(text)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list) and len(data) > 0 and isinstance(data[0], list):
                    return data[0][0]
                elif isinstance(data, list) and len(data) > 0 and isinstance(data[0], str):
                    return data[0]
    except Exception:
        pass

    # 2. Google Translate GTX
    try:
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": source_lang, "tl": target_lang, "dt": "t", "q": text}
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url, params=params)
            if resp.status_code == 200:
                data = resp.json()
                parts = [seg[0] for seg in data[0] if seg and seg[0]]
                if parts:
                    return "".join(parts)
    except Exception:
        pass

    return text

async def translate_via_free_engine(text: str, source_lang: str, target_lang: str) -> str:
    if len(text) <= 300:
        return await translate_single_chunk(text, source_lang, target_lang)

    sentences = re.split(r'([.?!;\n]+)', text)
    chunks = []
    curr = ""
    for s in sentences:
        if len(curr) + len(s) < 280:
            curr += s
        else:
            if curr.strip():
                chunks.append(curr.strip())
            curr = s
    if curr.strip():
        chunks.append(curr.strip())

    if not chunks:
        chunks = [text[:280]]

    translated_chunks = await asyncio.gather(
        *[translate_single_chunk(c, source_lang, target_lang) for c in chunks]
    )
    return " ".join(translated_chunks)

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
    primary = sanitize_gemini_model(model_name)
    # 多层自适应高可用降级链：用户指定模型 -> 3.7-flash -> 3.6-flash -> 3.5-flash
    candidates = [primary]
    for m in ["gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"]:
        if m not in candidates:
            candidates.append(m)

    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    if is_json:
        payload["generationConfig"] = {"response_mime_type": "application/json"}

    last_err = None
    async with httpx.AsyncClient(timeout=14.0) as client:
        for model in candidates:
            endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
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
                    print(f"[Gemini] Model {model} returned {resp.status_code}, auto-falling back to next model...")
            except Exception as ex:
                last_err = str(ex)
                print(f"[Gemini] Model {model} exception: {ex}, auto-falling back to next model...")

        # 备用：OpenAI 兼容接口重试
        for fb_model in ["gemini-3.7-flash", "gemini-3.6-flash"]:
            try:
                openai_ep = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
                openai_hdrs = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
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

    if req.provider == "gemini":
        api_key = req.api_key.strip()
        raw_json = await execute_gemini_call(api_key, req.model_name, prompt, is_json=True)
        return json.loads(raw_json)
    else:
        api_key = req.api_key.strip()
        endpoint = (req.custom_endpoint.strip() or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        model = req.model_name.strip() or "gpt-4o-mini"
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
async def handle_translate(req: TranslateRequest):
    raw_text = req.text.strip()
    if not raw_text:
        return {"cleaned_source": "", "translation": "", "keywords": [], "annotations": [], "section_heading": None}

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

    translated = await translate_via_free_engine(cleaned, req.source_lang, req.target_lang)
    h_annotations = heuristic_annotations(cleaned)

    # Heuristic academic keywords: 严格过滤非学术缩写，优先采纳内置词典与启发式实体
    candidate_kws = []
    for a in h_annotations:
        if a.get("term"):
            candidate_kws.append(a["term"])
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

    keywords = clean_academic_keywords(candidate_kws)

    return {
        "cleaned_source": cleaned,
        "translation": translated,
        "keywords": keywords,
        "annotations": h_annotations,
        "section_heading": heading,
        "api_error": api_error_msg
    }

@app.post("/api/ask-card")
async def ask_card(req: AskCardRequest):
    q_str = (req.question or "").strip()
    if q_str:
        user_inquiry = f"【同学提问】：{q_str}"
    else:
        user_inquiry = "【要求】：无需学生手动输入提问，请助教直接对老师这段讲授进行深度通俗精讲与答疑拆解。"

    prompt = f"""你是一名世界顶级名校计算机与工程学科的资深助教。
请针对以下老师课堂讲授的这段核心内容，为学生提供一份结构清晰、生动通俗的【助教深度解析与考点精讲】：

【老师原声】:
{req.card_source}

【中文精译】:
{req.card_translation}

{user_inquiry}

请用通俗易懂、切中要害的学术助教语言展开精讲，重点包含：
1. 【通俗大白话拆解】：用最形象的生活比喻或底层逻辑，解释老师这段话的核心概念究竟是什么。
2. 【核心原理与背景】：该知识点在技术体系或行业实践中为什么重要，解决了什么关键痛点。
3. 【常考点与避坑指南】：在考试考核或技术面试中，这段内容最容易怎么考，有哪些极易混淆的概念陷阱。

控制在 160~320 字以内，层次分明，让学生一眼看懂！"""

    if req.api_key and req.provider in ["gemini", "openai_compatible", "deepseek"]:
        try:
            if req.provider == "gemini":
                api_key = req.api_key.strip()
                ans = await execute_gemini_call(api_key, req.model_name, prompt, is_json=False)
                return {"answer": ans.strip()}
            else:
                api_key = req.api_key.strip()
                endpoint = (req.custom_endpoint.strip() or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                model = req.model_name.strip() or "gpt-4o-mini"
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
                        return {"answer": ans.strip()}
        except Exception as e:
            print(f"[AskCard] LLM error: {e}")

    # 智能启发式后备答疑：根据卡片内容匹配内置知识库，直接输出结构化解析
    src_tr = f"{req.card_source} {req.card_translation}".lower()
    matched = [v for k, v in BUILTIN_TECH_GLOSSARY.items() if k in src_tr]
    if matched:
        top_t = matched[0]
        fb_ans = f"💡 助教深度拆解：老师这段话的核心在于【{top_t['term_en']} ({top_t['term_zh']})】。\n\n• 大白话理解：{top_t['desc']}\n• 核心原理：在实际系统架构与合规落地中，该机制是不可或缺的防范与管控枢纽。\n• 考点提示：期末或面试常考其工作流程、适用场景及安全边界。"
    else:
        fb_ans = "💡 助教深度拆解：老师这段话聚焦于核心学术推导与技术规范的实际落地。\n\n• 大白话理解：建议结合前后语境把握因果逻辑与应用场景。\n• 复习与考点：注意老师在此处提及的专业术语与执行前提，是考核中的高频要点。"

    return {"answer": fb_ans}

@app.post("/api/summarize")
async def handle_summarize(req: SummarizeRequest):
    if not req.items:
        return {"overview": "暂无有效课堂记录", "takeaways": [], "glossary": []}

    transcript_blocks = []
    for item in req.items:
        time_str = item.get("time_str", "")
        src = item.get("cleaned_source") or item.get("source_text", "")
        tr = item.get("translation", "")
        transcript_blocks.append(f"[{time_str}] 原文: {src}\n译文: {tr}")

    full_text = "\n\n".join(transcript_blocks[-50:])

    prompt = f"""请分析以下课堂双语实录（课程：{req.session_title}），为同学生成一份结构清晰、高可读性的【课堂精要复习板书】：
要求：
1. 【概览 (overview)】：用 2-3 句话总结这堂课讲了什么核心课题。
2. 【核心要点 (takeaways)】：提炼 4-6 条重点干货（列表形式），突出重点公式/理论/结论/合规要求。
3. 【核心术语对照 (glossary)】：提取 3-6 个核心中英文专有名词解释。
   【严格过滤指令】：严禁将 US, EU, UK, UN, CN 等国家/地区缩写，或 ordinary common words（如 how, and, poor 等）提取为专有名词！
   每一项专有名词的 desc 字段必须写明其实质定义、底层原理或现实背景，字数在 20-50 字，严禁输出“高频学术词汇/概念”等空壳套话！

课堂记录节选：
{full_text}

请严格按如下 JSON 格式输出：
{{
  "overview": "...",
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

    if req.api_key and req.provider in ["gemini", "openai_compatible", "deepseek"]:
        try:
            if req.provider == "gemini":
                api_key = req.api_key.strip()
                raw_json = await execute_gemini_call(api_key, req.model_name, prompt, is_json=True)
                data = json.loads(raw_json)
                if "glossary" in data and isinstance(data["glossary"], list):
                    data["glossary"] = _sanitize_glossary_list(data["glossary"])
                return data
            else:
                api_key = req.api_key.strip()
                endpoint = (req.custom_endpoint.strip() or "https://api.openai.com/v1").rstrip("/") + "/chat/completions"
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
                model = req.model_name.strip() or "gpt-4o-mini"
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
                        return data
        except Exception as e:
            print(f"[Summarize] LLM error: {e}")

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

    takeaway_terms = [g["term_en"] for g in all_glossary[:4]]
    return {
        "overview": f"本节课共记录 {len(req.items)} 个知识意群，内容包含老师重点阐述的概念与推导。",
        "takeaways": [
            f"知识点探讨涉及：{', '.join(takeaway_terms) if takeaway_terms else '课堂核心推导与讲解'}",
            f"共记录约 {sum(len(it.get('cleaned_source', '')) for it in req.items)} 词讲授内容",
            "建议对照下方卡片中的重点时间戳与 AI 批注进行逐段复习。"
        ],
        "glossary": all_glossary[:6]
    }

@app.post("/api/export/docx")
async def export_docx(session: SessionData):
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

    if session.summary:
        sum_h = doc.add_heading("🎯 课堂重点精要与板书提炼", level=1)
        sum_h.style.font.color.rgb = RGBColor(79, 70, 229)
        
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
        shd.set(qn('w:fill'), '4F46E5')
        tcPr.append(shd)

    sorted_items = sorted(session.items, key=lambda x: (x.get('time_sec', 0), x.get('seq', 0)))
    for item in sorted_items:
        row = table.add_row()
        c0, c1, c2 = row.cells
        c0.width = Inches(1.1)
        c1.width = Inches(2.7)
        c2.width = Inches(3.0)

        star = " ⭐" if item.get("starred") else ""
        c0.text = f"⏱ {item.get('time_str', '00:00')}{star}"
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
    footer_run = footer_p.add_run("──────────────────────────────────────────────────\n📝 本笔记由 LectureScribe 智能生成 | 开发者: Blueberry (@lyang091126-cmd) | 享有原创版权保护 • 未经许可禁止商业剽窃")
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
async def export_markdown(session: SessionData):
    time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(session.created_at))
    duration_min = session.duration_seconds // 60
    duration_sec = session.duration_seconds % 60

    md = [
        f"# 📚 {session.title}",
        f"> **记录时间**: {time_str} | **课程时长**: {duration_min}分{duration_sec}秒 | **语言**: {session.source_lang.upper()} ➔ {session.target_lang.upper()}\n",
        "---",
    ]

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
    md.append("> 🛡️ **版权与防伪水印**：本课堂双语实录由 [LectureScribe](https://github.com/lyang091126-cmd/lecture-scribe) 智能生成  \n> **原作者**: Blueberry ([@lyang091126-cmd](https://github.com/lyang091126-cmd)) | 版权所有 © 2026 | 受原创版权法保护，未经许可禁止二次打包转售或商业剽窃")

    content = "\n".join(md)
    safe_title = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', session.title)
    encoded_fn = quote(f"{safe_title}.md")
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"}
    )

@app.post("/api/export/srt")
async def export_srt(session: SessionData):
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
        srt_lines.append("【LectureScribe 智能双语笔记 • Author: Blueberry (@lyang091126-cmd)】")
        srt_lines.append("")

    content = "\n".join(srt_lines)
    safe_title = re.sub(r'[^a-zA-Z0-9_\u4e00-\u9fa5]', '_', session.title)
    encoded_fn = quote(f"{safe_title}.srt")
    return Response(
        content=content.encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_fn}"}
    )

@app.get("/api/sessions")
def list_sessions():
    sessions = []
    for file in SESSIONS_DIR.glob("*.json"):
        try:
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
                sessions.append({
                    "id": data.get("id"),
                    "title": data.get("title", "未命名课堂"),
                    "created_at": data.get("created_at"),
                    "item_count": len(data.get("items", [])),
                    "duration_seconds": data.get("duration_seconds", 0)
                })
        except Exception:
            continue
    sessions.sort(key=lambda x: x.get("created_at", 0), reverse=True)
    return sessions

@app.get("/api/sessions/{session_id}")
def get_session(session_id: str):
    file = SESSIONS_DIR / f"{session_id}.json"
    if not file.exists():
        raise HTTPException(status_code=404, detail="Session not found")
    with open(file, "r", encoding="utf-8") as f:
        return json.load(f)

@app.post("/api/sessions")
def save_session(session: SessionData):
    file = SESSIONS_DIR / f"{session.id}.json"
    with open(file, "w", encoding="utf-8") as f:
        json.dump(session.dict(), f, ensure_ascii=False, indent=2)
    return {"status": "ok", "id": session.id}

@app.delete("/api/sessions/{session_id}")
def delete_session(session_id: str):
    file = SESSIONS_DIR / f"{session_id}.json"
    if file.exists():
        file.unlink()
    return {"status": "ok"}

app.mount("/", StaticFiles(directory=str(BASE_DIR / "static"), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    host = os.getenv("HOST", "0.0.0.0")
    print(f"Starting LectureScribe server on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
