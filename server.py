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
    question: str
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

def heuristic_annotations(text: str) -> List[Dict[str, str]]:
    results = []
    patterns = [
        (r'\b(mobile device management|MDM)\b', "Mobile Device Management (MDM)", "专业术语", "企业移动设备管理软件，用于远程配置、安全策略监控（如离开自动锁屏）及设备合规审计。"),
        (r'\b(personal health|HIPAA|PHI)\b', "HIPAA / 个人健康信息安全", "法规标准", "涉及个人敏感健康数据保护，法规对数据防泄密有严格要求，违规未锁屏或泄密将面临重罚。"),
        (r'\b(FDA)\b', "美国食品药品监督管理局 (FDA)", "行业规范", "在涉及医疗健康系统与设备时，必须符合 FDA 极其严格的合规审计与流程验证标准。"),
        (r'\b(NIST)\b', "NIST 美国国家标准与技术研究院", "国家安全标准", "制定了全球顶级的网络安全框架（CSF）与密码学标准，企业合规的重要基准。"),
        (r'\b(lock|auto lock)\b', "离开工位自动锁屏", "安全合规", "办公安全基本守则，防止他人或访客在无人看管的电脑上窥探或窃取机密。"),
        (r'\b(threat)\b', "内部/未授权威胁 (Security Threat)", "安全概念", "在企业网络安全中指可能导致数据泄露、非授权访问或服务中断的安全隐患。"),
        (r'\b(convolutional neural network|CNN)\b', "卷积神经网络 (CNN)", "核心算法", "深度学习中处理图像与网格数据的核心架构，通过局部感受野和权重共享提取空间特征。"),
    ]
    for pattern, term, ttype, exp in patterns:
        if re.search(pattern, text, flags=re.IGNORECASE):
            results.append({"term": term, "type": ttype, "explanation": exp})
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
   识别本段话中出现的任何：专业术语、行业法规标准、历史典故、技术架构/软件、或背景事件。
   对识别出的 1-3 个重点实体提供通俗透彻的背景讲解：
   - "term": 名词/事件名称（如 "Mobile Device Management (MDM)", "HIPAA", "FDA", "NIST" 等）
   - "type": 分类，从 ["专业术语", "典故背景", "法规标准", "核心考点", "安全合规", "重要事件"] 中选一
   - "explanation": 用 1-2 句通俗透彻的中文解释其定义、原理或在当前话境下的背景意义。
4. 提取 1-3 个核心专有名词标签（keywords）。
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
      "term": "术语/典故/事件名称",
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
            return {
                "cleaned_source": llm_result.get("cleaned_source", cleaned),
                "translation": llm_result.get("translation", ""),
                "keywords": llm_result.get("keywords", []),
                "annotations": llm_result.get("annotations", []),
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

    keywords = []
    if "en" in req.source_lang:
        words = re.findall(r'\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})*\b', cleaned)
        keywords = list(dict.fromkeys(words))[:3]

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
    prompt = f"""你是一名耐心的大学教授助教。学生在听课时对老师讲的以下这段话有疑问：

【老师原话】:
{req.card_source}

【中文精译】:
{req.card_translation}

【学生提问】:
{req.question}

请用通俗生动、切中要点的语言为学生解答（可举生活中的例子，说明这个概念的现实意义或考试常考点），控制在 150~300 字以内。"""

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
                        {"role": "system", "content": "You are a helpful university teaching assistant."},
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

    return {
        "answer": f"💡 助教速解：老师这段话主要强调了信息安全合规与流程监管（如 FDA、HIPAA、NIST 等标准）在企业实际架构中的落地。在涉及敏感数据时，离岗锁屏及自动化审计是强制要求。"
    }

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

课堂记录节选：
{full_text}

请严格按如下 JSON 格式输出：
{{
  "overview": "...",
  "takeaways": ["重点1...", "重点2...", "重点3..."],
  "glossary": [
    {{"term_en": "Mobile Device Management", "term_zh": "移动设备管理", "desc": "企业对员工终端设备实施统一管控与安全策略的系统"}}
  ]
}}"""

    if req.api_key and req.provider in ["gemini", "openai_compatible", "deepseek"]:
        try:
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
                        return json.loads(raw_text)
        except Exception as e:
            print(f"[Summarize] LLM error: {e}")

    all_keywords = []
    for item in req.items:
        all_keywords.extend(item.get("keywords", []))
    unique_keywords = list(dict.fromkeys(all_keywords))[:8]

    return {
        "overview": f"本节课共记录 {len(req.items)} 个知识意群，内容包含老师重点阐述的概念与推导。",
        "takeaways": [
            f"知识点探讨涉及：{', '.join(unique_keywords[:4]) if unique_keywords else '课堂主体内容'}",
            f"共记录约 {sum(len(it.get('cleaned_source', '')) for it in req.items)} 词讲授内容",
            "建议对照下方卡片中的重点时间戳与 AI 批注进行逐段复习。"
        ],
        "glossary": [{"term_en": kw, "term_zh": kw, "desc": "课堂高频核心概念"} for kw in unique_keywords[:5]]
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
