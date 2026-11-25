/**
 * ============================================================================
 * Project: LectureScribe - 课堂智能双语速记与 AI 智能讲解工作台
 * Author: Blueberry (@lyang091126-cmd)
 * GitHub: https://github.com/lyang091126-cmd/lecture-scribe
 * Copyright (c) 2026 Blueberry. All rights reserved.
 * 
 * [版权与防剽窃严正声明 / Anti-Plagiarism Notice]
 * 本项目由作者独立原创构思、架构与编写，享有全部著作权。
 * 严禁在未获原作者许可的情况下进行商业倒卖、闭源转售、恶意抄袭或去除作者署名。
 * ============================================================================
 */

// 控制台作者与防剽窃安全水印
(function printAuthorWatermark() {
  const brandStyle = "background: #1f2de6; color: #ffffff; font-size: 13px; font-weight: bold; padding: 4px 10px; border-radius: 4px 0 0 4px;";
  const authorStyle = "background: #0f172a; color: #38bdf8; font-size: 13px; font-weight: bold; padding: 4px 10px; border-radius: 0 4px 4px 0;";
  const warnStyle = "color: #f59e0b; font-size: 11px; font-weight: 600; line-height: 1.6; margin-top: 4px;";
  
  console.log("%cAnti Lecture AI%cAuthor: Blueberry (@lyang091126-cmd)", brandStyle, authorStyle);
  console.log(
    "%c🛡️【版权与防剽窃严正声明】\n" +
    "本项目代码受严格著作权保护。未经作者书面许可，严禁商业转售、恶意抄袭或抹除原作者署名！\n" +
    "开源官方仓库: https://github.com/lyang091126-cmd/lecture-scribe",
    warnStyle
  );
})();


// State
const state = {
  isRecording: false,
  startTime: null,
  elapsedSeconds: 0,
  timerInterval: null,
  audioContext: null,
  analyser: null,
  mediaStream: null,
  recognition: null,
  
  // Buffers for Semantic Chunking
  activeInterimText: '',
  cardedInterim: '',
  thoughtBuffer: [],
  pauseTimer: null,
  pauseThreshold: 1.8,
  
  // Session data
  session: {
    id: 'session_' + Date.now(),
    title: '计算机体系结构 Lecture 01',
    created_at: Date.now() / 1000,
    updated_at: Date.now() / 1000,
    source_lang: 'en',
    target_lang: 'zh',
    duration_seconds: 0,
    items: [],
    summary: null,
    slide_meta: null,
    slide_overview: null,
    slide_summaries: {}
  },
  
  totalTokensEst: 0,
  filterStarredOnly: false,
  searchQuery: '',
  activeQACardId: null,
  
  config: {
    provider: 'gemini',
    apiKey: '',
    modelName: '',
    customEndpoint: '',
    useLlmLiveTranslate: false,
    pauseThreshold: 1.4
  },

  auth: {
    token: localStorage.getItem('lecture_scribe_auth_token_v1') || '',
    user: null,
    mode: 'login' // 'login' | 'register'
  }
};

const AUTH_TOKEN_KEY = 'lecture_scribe_auth_token_v1';

function apiHeaders(extra = {}) {
  const h = { 'X-Client-Id': getClientId(), ...extra };
  if (state.auth && state.auth.token) {
    h['Authorization'] = `Bearer ${state.auth.token}`;
  }
  return h;
}

function getEffectivePlan() {
  return (state.auth && state.auth.user && state.auth.user.plan) ? state.auth.user.plan : 'free';
}

function isPaidUser() {
  const p = getEffectivePlan();
  return p === 'pro' || p === 'ultra';
}

// Per-browser client id sent as X-Client-Id so the server can keep this
// browser's cloud-saved sessions (history list, cross-tab reload) separate
// from every other visitor's, without any real login. Generated once and
// persisted in localStorage; never sent anywhere except this site's own API.
function getClientId() {
  const KEY = 'lecture_scribe_client_id';
  let id = localStorage.getItem(KEY);
  if (id && /^[a-zA-Z0-9_-]{1,64}$/.test(id)) {
    return id;
  }
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    id = window.crypto.randomUUID();
  } else {
    id = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  localStorage.setItem(KEY, id);
  return id;
}

function normalizeSpeech(text) {
  return (text || '').toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, '').replace(/\s+/g, ' ').trim();
}

// The recognizer streams interim text and finalizes it later. A pause can turn
// interim text into a card first, so drop the words a card already contains.
function stripCardedWords(text) {
  const carded = state.cardedInterim;
  const norm = normalizeSpeech(text);
  if (!carded || !norm) return text;
  if (norm === carded || carded.startsWith(norm + ' ')) return '';
  if (!norm.startsWith(carded + ' ')) {
    state.cardedInterim = '';
    return text;
  }
  const cardedWordCount = carded.split(' ').length;
  const tokens = text.split(/\s+/);
  let cut = 0;
  for (let seen = 0; cut < tokens.length && seen < cardedWordCount; cut++) {
    if (normalizeSpeech(tokens[cut])) seen++;
  }
  return tokens.slice(cut).join(' ');
}

// Keyless translation runs in the visitor's own browser: the free Google and
// MyMemory endpoints block Render's shared datacenter IP but accept ordinary
// user IPs (both send Access-Control-Allow-Origin: *).
const TRANSLATION_FAILED_TEXT = '⚠️ 翻译失败：翻译服务暂时不可用';
const TRANSLATE_CHUNK_MAX_CHARS = 450; // MyMemory rejects queries over 500 chars

async function fetchJsonWithTimeout(url, timeoutMs = 6000) {
  const res = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function translateChunkInBrowser(text, sourceLang) {
  try {
    const data = await fetchJsonWithTimeout(
      `https://clients5.google.com/translate_a/t?client=dict-chrome-ex&sl=auto&tl=zh-CN&q=${encodeURIComponent(text)}`
    );
    const first = Array.isArray(data) ? data[0] : null;
    const out = Array.isArray(first) ? first[0] : first;
    if (typeof out === 'string' && out.trim()) return out;
  } catch (e) {}

  try {
    const data = await fetchJsonWithTimeout(
      `https://api.mymemory.translated.net/get?q=${encodeURIComponent(text)}&langpair=${encodeURIComponent(sourceLang + '|zh-CN')}`
    );
    const out = data.responseData && data.responseData.translatedText;
    if (Number(data.responseStatus) === 200 && !data.quotaFinished && out && !/^MYMEMORY WARNING/i.test(out)) {
      return out;
    }
  } catch (e) {}

  return '';
}

function splitForTranslation(text) {
  const chunks = [];
  let curr = '';
  for (const word of text.split(/\s+/).filter(Boolean)) {
    for (let i = 0; i < word.length; i += TRANSLATE_CHUNK_MAX_CHARS) {
      const piece = word.slice(i, i + TRANSLATE_CHUNK_MAX_CHARS);
      if (curr && curr.length + 1 + piece.length > TRANSLATE_CHUNK_MAX_CHARS) {
        chunks.push(curr);
        curr = piece;
      } else {
        curr = curr ? `${curr} ${piece}` : piece;
      }
    }
  }
  if (curr) chunks.push(curr);
  return chunks;
}

// Returns '' if any chunk fails, so the source text is never shown as a translation.
async function translateInBrowser(text, sourceLang) {
  if (!text) return '';
  if (sourceLang === 'zh') return text;
  const parts = await Promise.all(splitForTranslation(text).map(c => translateChunkInBrowser(c, sourceLang)));
  return parts.every(Boolean) ? parts.join('') : '';
}

// Re-rendering a whole lecture after every new card froze the page for ~1s
// once a session passed a thousand cards, so only the newest stay in the DOM.
// Searching or filtering is a deliberate review action, so show more there.
const RENDERED_CARD_LIMIT = 10;
const FILTERED_CARD_LIMIT = 120;

// --- Bilingual subtitles: Dual-mode (1) Draggable In-Page True Frosted Glass
// over the PDF slide stage, AND (2) Compact Always-On-Top Document PiP window
// for viewing other browser tabs or PowerPoint.
const SUBTITLE_CSS = `
  html, body { margin: 0; padding: 0; width: 100%; height: 100%; overflow: hidden; }
  .sub-shell {
    box-sizing: border-box;
    display: flex; flex-direction: column; justify-content: center; gap: 6px;
    font-family: "Inter", "Noto Sans SC", system-ui, -apple-system, "Microsoft YaHei", sans-serif;
    -webkit-font-smoothing: antialiased;
    transition: background-color 0.2s ease, color 0.2s ease, border-color 0.2s ease;
  }
  .sub-shell--inpage {
    position: fixed;
    left: 48px;
    bottom: 204px;
    width: min(680px, calc(63.5vw - 96px));
    min-width: 320px;
    max-width: calc(100vw - 32px);
    padding: 10px 16px 14px;
    border-radius: 14px;
    z-index: 260;
    backdrop-filter: blur(20px) saturate(165%);
    -webkit-backdrop-filter: blur(20px) saturate(165%);
  }
  .sub-shell--pip {
    width: 100%; height: 100%;
    padding: 10px 18px 14px;
    border-radius: 0;
  }
  /* Warm Paper True Frosted Glass (Light Theme) */
  .sub-shell--light {
    background: rgba(245, 242, 236, 0.72);
    color: #171717;
    border: 1px solid rgba(255, 255, 255, 0.78);
    box-shadow: 0 14px 36px -14px rgba(23, 23, 23, 0.28), inset 0 1px 0 rgba(255, 255, 255, 0.85);
  }
  .sub-shell--pip.sub-shell--light {
    background: linear-gradient(135deg, #F7F4EE 0%, #ECE6DB 100%);
    border: none;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.9);
  }
  /* Soft Charcoal Frosted Glass (Dark Theme) */
  .sub-shell--dark {
    background: rgba(22, 22, 20, 0.74);
    color: #F5F2EC;
    border: 1px solid rgba(255, 255, 255, 0.16);
    box-shadow: 0 16px 40px -14px rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.12);
  }
  .sub-shell--pip.sub-shell--dark {
    background: linear-gradient(135deg, #22221F 0%, #161614 100%);
    border: none;
    box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.1);
  }
  .sub-head {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding-bottom: 6px; margin-bottom: 2px;
    border-bottom: 1px solid rgba(23, 23, 23, 0.08);
    user-select: none;
  }
  .sub-shell--dark .sub-head { border-bottom-color: rgba(245, 242, 236, 0.12); }
  .sub-drag {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 11.5px; font-weight: 500; opacity: 0.72;
    cursor: move;
  }
  .sub-shell--pip .sub-drag { cursor: default; }
  .sub-dot {
    width: 6px; height: 6px; border-radius: 50%; background: #315BCE;
    display: inline-block;
  }
  .sub-shell--dark .sub-dot { background: #E8C97A; }
  .sub-tools { display: inline-flex; align-items: center; gap: 5px; }
  .sub-btn {
    border: 1px solid rgba(23, 23, 23, 0.14);
    background: rgba(255, 255, 255, 0.55);
    color: #171717;
    border-radius: 6px;
    height: 22px; padding: 0 8px;
    font-size: 11px; font-weight: 500; cursor: pointer;
    font-family: inherit;
    transition: background-color 0.14s, border-color 0.14s;
  }
  .sub-btn:hover { background: rgba(255, 255, 255, 0.9); border-color: #315BCE; color: #315BCE; }
  .sub-shell--dark .sub-btn {
    border-color: rgba(245, 242, 236, 0.18);
    background: rgba(245, 242, 236, 0.09);
    color: #F5F2EC;
  }
  .sub-shell--dark .sub-btn:hover { background: rgba(245, 242, 236, 0.18); border-color: #E8C97A; color: #E8C97A; }
  .sub-source {
    font-size: 13px; line-height: 1.42; color: #4A4742;
    word-break: break-word;
  }
  .sub-shell--dark .sub-source { color: #CBC5BA; }
  .sub-translation {
    font-size: 18px; line-height: 1.46; font-weight: 600; color: #171717;
    word-break: break-word;
  }
  .sub-shell--dark .sub-translation { color: #F2D78C; text-shadow: 0 1px 2px rgba(0, 0, 0, 0.35); }
`;

let subtitleWindow = null;
let subtitleRoot = null;
let subtitleTheme = localStorage.getItem('lecture_scribe_sub_theme') || 'light'; // 'light' | 'dark'
const subtitleText = { source: '', translation: '' };

function renderSubtitle() {
  if (!subtitleRoot) return;
  const src = subtitleRoot.querySelector('.sub-source');
  const trans = subtitleRoot.querySelector('.sub-translation');
  if (src) src.textContent = subtitleText.source || '等待老师开口...';
  if (trans) trans.textContent = subtitleText.translation || '中文译文会实时显示在这里';
}

function setSubtitle(source, translation) {
  if (typeof source === 'string') subtitleText.source = source;
  if (typeof translation === 'string') subtitleText.translation = translation;
  renderSubtitle();
}

function applySubtitleTheme(shellEl, toggleBtn) {
  if (!shellEl) return;
  shellEl.classList.toggle('sub-shell--light', subtitleTheme === 'light');
  shellEl.classList.toggle('sub-shell--dark', subtitleTheme === 'dark');
  if (toggleBtn) toggleBtn.textContent = subtitleTheme === 'light' ? '深色' : '浅色';
}

function makeSubtitleDraggable(shellEl, handleEl) {
  if (!shellEl || !handleEl) return;
  let dragging = false;
  let startX = 0, startY = 0, origLeft = 0, origTop = 0;

  handleEl.addEventListener('pointerdown', (e) => {
    if (e.target.closest('button')) return;
    dragging = true;
    const rect = shellEl.getBoundingClientRect();
    shellEl.style.left = rect.left + 'px';
    shellEl.style.top = rect.top + 'px';
    shellEl.style.bottom = 'auto';
    startX = e.clientX;
    startY = e.clientY;
    origLeft = rect.left;
    origTop = rect.top;
    handleEl.setPointerCapture?.(e.pointerId);
  });

  handleEl.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const maxL = Math.max(8, window.innerWidth - shellEl.offsetWidth - 8);
    const maxT = Math.max(8, window.innerHeight - shellEl.offsetHeight - 8);
    const nextL = Math.max(8, Math.min(maxL, origLeft + (e.clientX - startX)));
    const nextT = Math.max(8, Math.min(maxT, origTop + (e.clientY - startY)));
    shellEl.style.left = nextL + 'px';
    shellEl.style.top = nextT + 'px';
  });

  const endDrag = () => { dragging = false; };
  handleEl.addEventListener('pointerup', endDrag);
  handleEl.addEventListener('pointercancel', endDrag);
}

// Subtitles can't wait for a card: a lecturer who never pauses can talk for
// tens of seconds before one is cut, which left the previous sentence's
// translation sitting under a long, already-moved-on English line.
const LIVE_SUBTITLE_INTERVAL_MS = 2000;
const INTERIM_FLUSH_CHARS = 150;
let liveSubtitleTimer = null;
let liveSubtitleTurn = 0;
let lastLiveSubtitleText = '';

function queueLiveSubtitleTranslation() {
  if (!subtitleRoot || liveSubtitleTimer) return;
  liveSubtitleTimer = setTimeout(async () => {
    liveSubtitleTimer = null;
    const current = (state.activeInterimText || '').trim();
    if (!subtitleRoot || current.length < 8) return;
    if (current.startsWith(lastLiveSubtitleText) && current.length - lastLiveSubtitleText.length < 10) return;
    lastLiveSubtitleText = current;
    const turn = ++liveSubtitleTurn;
    const translated = await translateInBrowser(current, state.session.source_lang);
    if (translated && turn === liveSubtitleTurn) setSubtitle(undefined, translated);
  }, LIVE_SUBTITLE_INTERVAL_MS);
}

function updateSubtitleButton() {
  if (el.btnSubtitles) el.btnSubtitles.classList.toggle('active', !!subtitleRoot);
}

function closeSubtitles() {
  if (subtitleWindow) {
    try { subtitleWindow.close(); } catch (e) {}
    subtitleWindow = null;
    subtitleRoot = null;
  } else if (subtitleRoot) {
    subtitleRoot.remove();
    subtitleRoot = null;
  }
  updateSubtitleButton();
}

function openInPageSubtitles() {
  closeSubtitles();
  if (!document.getElementById('subtitleStyles')) {
    const style = document.createElement('style');
    style.id = 'subtitleStyles';
    style.textContent = SUBTITLE_CSS;
    document.head.appendChild(style);
  }
  const canPip = !!window.documentPictureInPicture;
  const bar = document.createElement('div');
  bar.id = 'inPageSubtitleBar';
  bar.className = `sub-shell sub-shell--inpage sub-shell--${subtitleTheme}`;
  bar.innerHTML = `
    <div class="sub-head">
      <span class="sub-drag"><span class="sub-dot"></span>实时双语字幕 · 拖动调整位置</span>
      <div class="sub-tools">
        <button type="button" class="sub-btn" data-sub-action="theme">${subtitleTheme === 'light' ? '深色' : '浅色'}</button>
        ${canPip ? '<button type="button" class="sub-btn" data-sub-action="pip" title="弹出独立置顶窗口，看其他网页或 PPT 时也能悬浮在最上方">弹出置顶窗</button>' : ''}
        <button type="button" class="sub-btn" data-sub-action="close" aria-label="关闭字幕">×</button>
      </div>
    </div>
    <div class="sub-source"></div>
    <div class="sub-translation"></div>
  `;
  document.body.appendChild(bar);

  const themeBtn = bar.querySelector('[data-sub-action="theme"]');
  themeBtn?.addEventListener('click', () => {
    subtitleTheme = subtitleTheme === 'light' ? 'dark' : 'light';
    localStorage.setItem('lecture_scribe_sub_theme', subtitleTheme);
    applySubtitleTheme(bar, themeBtn);
  });
  bar.querySelector('[data-sub-action="pip"]')?.addEventListener('click', () => openPipSubtitles());
  bar.querySelector('[data-sub-action="close"]')?.addEventListener('click', () => closeSubtitles());
  makeSubtitleDraggable(bar, bar.querySelector('.sub-head'));

  subtitleRoot = bar;
  renderSubtitle();
  updateSubtitleButton();
  showToast('已开启页内毛玻璃字幕条（可拖动，点右上角可弹出独立置顶窗）');
}

async function openPipSubtitles() {
  if (!window.documentPictureInPicture) {
    showToast('当前浏览器不支持独立置顶窗，已为你保持页内毛玻璃字幕条');
    return;
  }
  closeSubtitles();
  try {
    const win = await documentPictureInPicture.requestWindow({ width: 780, height: 136 });
    const style = win.document.createElement('style');
    style.textContent = SUBTITLE_CSS;
    win.document.head.appendChild(style);
    win.document.body.innerHTML = `
      <div class="sub-shell sub-shell--pip sub-shell--${subtitleTheme}">
        <div class="sub-head">
          <span class="sub-drag"><span class="sub-dot"></span>Anti Lecture 独立置顶字幕</span>
          <div class="sub-tools">
            <button type="button" class="sub-btn" data-sub-action="theme">${subtitleTheme === 'light' ? '深色' : '浅色'}</button>
            <button type="button" class="sub-btn" data-sub-action="inpage">回页内毛玻璃</button>
          </div>
        </div>
        <div class="sub-source"></div>
        <div class="sub-translation"></div>
      </div>
    `;
    const shell = win.document.querySelector('.sub-shell');
    const themeBtn = win.document.querySelector('[data-sub-action="theme"]');
    themeBtn?.addEventListener('click', () => {
      subtitleTheme = subtitleTheme === 'light' ? 'dark' : 'light';
      localStorage.setItem('lecture_scribe_sub_theme', subtitleTheme);
      applySubtitleTheme(shell, themeBtn);
    });
    win.document.querySelector('[data-sub-action="inpage"]')?.addEventListener('click', () => {
      openInPageSubtitles();
    });
    win.addEventListener('pagehide', () => {
      if (subtitleWindow === win) {
        subtitleWindow = null;
        subtitleRoot = null;
        updateSubtitleButton();
      }
    });
    subtitleWindow = win;
    subtitleRoot = shell;
    renderSubtitle();
    updateSubtitleButton();
    showToast('已弹出独立置顶双语字幕窗，切换其他网页或 PPT 时始终悬浮在最上层');
  } catch (e) {
    console.warn('Picture-in-Picture subtitles unavailable:', e);
    openInPageSubtitles();
  }
}

async function toggleSubtitles() {
  if (subtitleRoot) {
    closeSubtitles();
    return;
  }
  openInPageSubtitles();
}

const ENGLISH_STOP_WORDS = new Set([
  'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and', 'any', 'are', "aren't",
  'as', 'at', 'be', 'because', 'been', 'before', 'being', 'below', 'between', 'both', 'but', 'by',
  'can', "can't", 'cannot', 'could', "couldn't", 'did', "didn't", 'do', 'does', "doesn't", 'doing',
  "don't", 'down', 'during', 'each', 'few', 'for', 'from', 'further', 'had', "hadn't", 'has', "hasn't",
  'have', "haven't", 'having', 'he', "he'd", "he'll", "he's", 'her', 'here', "here's", 'hers', 'herself',
  'him', 'himself', 'his', 'how', "how's", 'i', "i'd", "i'll", "i'm", "i've", 'if', 'in', 'into', 'is',
  "isn't", 'it', "it's", 'its', 'itself', 'let', "let's", 'me', 'more', 'most', "mustn't", 'my', 'myself',
  'no', 'nor', 'not', 'of', 'off', 'on', 'once', 'only', 'or', 'other', 'ought', 'our', 'ours', 'ourselves',
  'out', 'over', 'own', 'same', "shan't", 'she', "she'd", "she'll", "she's", 'should', "shouldn't", 'so',
  'some', 'such', 'than', 'that', "that's", 'the', 'their', 'theirs', 'them', 'themselves', 'then', 'there',
  "there's", 'these', 'they', "they'd", "they'll", "they're", "they've", 'this', 'those', 'through', 'to',
  'too', 'under', 'until', 'up', 'us', 'very', 'was', "wasn't", 'we', "we'd", "we'll", "we're", "we've", 'were',
  "weren't", 'what', "what's", 'when', "when's", 'where', "where's", 'which', 'while', 'who', "who's",
  'whom', 'why', "why's", 'with', "won't", 'would', "wouldn't", 'you', "you'd", "you'll", "you're", "you've",
  'your', 'yours', 'yourself', 'yourselves',
  'ok', 'okay', 'yeah', 'yep', 'nope', 'poor', 'good', 'bad', 'great', 'well', 'just', 'like', 'basically',
  'actually', 'really', 'going', 'get', 'getting', 'got', 'make', 'making', 'made', 'say', 'saying', 'said',
  'look', 'looking', 'see', 'seeing', 'saw', 'think', 'thinking', 'thought', 'know', 'knowing', 'knew',
  'take', 'taking', 'took', 'come', 'coming', 'came', 'go', 'went', 'gone', 'put', 'tell', 'talk', 'talking',
  'use', 'using', 'used', 'work', 'working', 'worked', 'try', 'trying', 'tried', 'start', 'starting', 'started',
  'one', 'two', 'three', 'first', 'second', 'third', 'next', 'now', 'break', 'quick', 'issues', 'issue',
  'problem', 'problems', 'thing', 'things', 'something', 'anything', 'nothing', 'someone', 'anyone', 'everyone',
  'experience', 'experiences', 'outcome', 'outcomes', 'money', 'user', 'users', 'system', 'systems', 'need',
  'needs', 'want', 'wants', 'way', 'ways', 'lot', 'lots', 'mean', 'means', 'meant', 'right', 'sure', 'maybe',
  'kind', 'sort', 'bit', 'point', 'points', 'part', 'parts', 'case', 'cases', 'time', 'times'
]);

// 严格过滤国家代码、日常通用代词、常见口语缩写（绝不作为学术专有名词展示）
const NON_ACADEMIC_TERMS = new Set([
  'us', 'usa', 'uk', 'eu', 'un', 'cn', 'jp', 'de', 'fr', 'ru', 'in', 'au', 'ca', 'nz', 'kr', 'it', 'es', 'ch', 'nl', 'se', 'no', 'sg', 'hk', 'tw', 'mo',
  'america', 'american', 'china', 'chinese', 'europe', 'european', 'japan', 'japanese', 'united states',
  'them', 'him', 'her', 'its', 'our', 'ours', 'their', 'theirs', 'me', 'you',
  'am', 'pm', 'ok', 'okay', 'tv', 'pc', 'vs', 'etc', 'id', 'no', 'yes', 'hi', 'hello', 'by', 'mr', 'ms', 'mrs', 'dr',
  'iq', 'eq', 'vip', 'ceo', 'cfo', 'coo', 'cto', 'hr', 'pr', 'ad', 'bc', 'ps', 'faq', 'fyi', 'asap',
  'diy', 'aka', 'tba', 'tbd', 'eta', 'tgif', 'lol', 'omg', 'na', 'n/a',
  'today', 'tomorrow', 'yesterday', 'now', 'then', 'week', 'month', 'year', 'day', 'hour', 'minute', 'second',
  'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'
]);

// 前端内置高频核心专业术语词典（权威中文命名与通俗背景原理解析）
const BUILTIN_TECH_GLOSSARY = {
  'owasp': {
    term_en: 'OWASP',
    term_zh: '开放式Web应用安全项目',
    desc: '全球权威的应用安全非营利组织，其发布的 OWASP Top 10 是评估 Web 漏洞风险的事实国际标准。'
  },
  'mdm': {
    term_en: 'MDM (Mobile Device Management)',
    term_zh: '企业移动设备管理',
    desc: '企业统一管控办公终端的系统，可远程下发安全策略（如离岗自动锁屏、防截屏、远程数据擦除）。'
  },
  'hipaa': {
    term_en: 'HIPAA',
    term_zh: '健康保险可携性与责任法案',
    desc: '美国针对医疗健康与患者隐私制定的合规法案，对敏感健康信息（PHI）保护有极严苛的防泄密要求。'
  },
  'fda': {
    term_en: 'FDA',
    term_zh: '美国食品药品监督管理局',
    desc: '在医疗健康系统、医用软件及关键嵌入式算法中，必须通过 FDA 极严格的合规审计与流程验证标准。'
  },
  'nist': {
    term_en: 'NIST',
    term_zh: '美国国家标准与技术研究院',
    desc: '制定了全球公认的网络安全框架（CSF）与密码算法标准，是企业架构安全与等保合规的核心基石。'
  },
  'gdpr': {
    term_en: 'GDPR',
    term_zh: '通用数据保护条例',
    desc: '欧盟严苛的数据隐私保护条例，强调用户的知情权与被遗忘权，对用户敏感数据跨境传输实施严格监管。'
  },
  'soc2': {
    term_en: 'SOC 2',
    term_zh: '服务机构控制报告',
    desc: '针对云服务商与 SaaS 企业的独立合规审计报告，评估系统在安全性、可用性及机密性上的控制机制。'
  },
  'xss': {
    term_en: 'XSS (Cross-Site Scripting)',
    term_zh: '跨站脚本攻击',
    desc: '攻击者向网页注入恶意客户端脚本，受害者浏览时在浏览器端静默执行以窃取身份 Cookie 或敏感凭证。'
  },
  'csrf': {
    term_en: 'CSRF',
    term_zh: '跨站请求伪造',
    desc: '诱导用户在已认证的浏览器中，向受信任网站发起未经授权的恶意操作请求（如转账、修改安全邮箱）。'
  },
  'ddos': {
    term_en: 'DDoS',
    term_zh: '分布式拒绝服务攻击',
    desc: '控制大量僵尸网络节点向目标服务器灌注海量虚假流量，耗尽网络带宽或计算资源导致合法服务瘫痪。'
  },
  'jwt': {
    term_en: 'JWT (JSON Web Token)',
    term_zh: 'JSON Web 令牌',
    desc: '基于数字签名的轻量级、自包含跨域认证规范，广泛用于微服务架构与前后端分离的无状态会话鉴权。'
  },
  'oauth': {
    term_en: 'OAuth 2.0',
    term_zh: '开放授权协议',
    desc: '业内标准的授权框架，允许第三方应用在不直接获取用户账号密码的前提下安全获得受限的资源访问令牌。'
  },
  'tcp': {
    term_en: 'TCP',
    term_zh: '传输控制协议',
    desc: '面向连接、高可靠且基于字节流的传输层通信协议，具备三次握手建立连接、丢包重传及拥塞控制机制。'
  },
  'udp': {
    term_en: 'UDP',
    term_zh: '用户数据报协议',
    desc: '无连接、开销低且实时性极高的传输层协议，广泛应用于音视频直播通话、多人联机对战与流媒体传输。'
  },
  'dns': {
    term_en: 'DNS',
    term_zh: '域名系统',
    desc: '互联网核心基础设施，通过分布式树状查询将人类易记的域名解析为计算机底层通信的 IP 地址。'
  },
  'http': {
    term_en: 'HTTP',
    term_zh: '超文本传输协议',
    desc: '万维网数据通信的基础协议，基于请求-响应无状态模型，是 Web 浏览器与服务端交互的基石。'
  },
  'https': {
    term_en: 'HTTPS',
    term_zh: '安全超文本传输协议',
    desc: '在 HTTP 基础上结合 TLS/SSL 实施公钥握手与对称加密传输，保障数据机密性与防篡改完整性。'
  },
  'tls': {
    term_en: 'TLS',
    term_zh: '传输层安全性协议',
    desc: '为互联网通信提供数据保密与完整性保护的现代密码学协议（SSL 的升级版），全面保障端到端加密。'
  },
  'ssl': {
    term_en: 'SSL',
    term_zh: '安全套接字层',
    desc: '网络通信早期的加密协议，现已被安全性更高、算法更现代的 TLS 协议完全继承与替代。'
  },
  'ssh': {
    term_en: 'SSH',
    term_zh: '安全外壳协议',
    desc: '在不安全网络上通过非对称密钥加密为远程计算机提供安全终端交互与文件传输的协议。'
  },
  'cpu': {
    term_en: 'CPU',
    term_zh: '中央处理器',
    desc: '计算机的核心控制中枢与运算单元，负责解释执行程序指令、进行算术逻辑运算及统筹各部件调度。'
  },
  'gpu': {
    term_en: 'GPU',
    term_zh: '图形处理器',
    desc: '拥有海量并行计算核心的高吞吐硬件，现已成为 AI 深度学习大规模矩阵运算与图像处理的主流算力引擎。'
  },
  'ram': {
    term_en: 'RAM',
    term_zh: '随机存取存储器',
    desc: '与 CPU 直接高速交换数据的易失性主存储器，断电后数据即失，承载操作系统及运行中程序的活动数据。'
  },
  'rom': {
    term_en: 'ROM',
    term_zh: '只读存储器',
    desc: '非易失性存储芯片，断电后数据永不丢失，通常用于固化存放计算机开机自检与底层启动固件（BIOS/UEFI）。'
  },
  'dma': {
    term_en: 'DMA (Direct Memory Access)',
    term_zh: '直接内存访问',
    desc: '允许高速外设绕过 CPU 直接读写主内存，大幅卸载 CPU 搬运数据的开销，提升大吞吐数据传输效率。'
  },
  'cnn': {
    term_en: 'CNN',
    term_zh: '卷积神经网络',
    desc: '利用局部感受野卷积核与权重共享机制提取图像网格特征的深度架构，计算机视觉领域的核心支柱。'
  },
  'rnn': {
    term_en: 'RNN',
    term_zh: '循环神经网络',
    desc: '通过隐藏状态时间循环反馈建模时序上下文依赖的神经网络，常用于语音、自然语言等动态序列处理。'
  },
  'llm': {
    term_en: 'LLM',
    term_zh: '大语言模型',
    desc: '基于海量文本自监督预训练的数十亿至万亿级参数深度模型，具备通用的自然语言理解、逻辑推理与生成能力。'
  },
  'rag': {
    term_en: 'RAG (Retrieval-Augmented Generation)',
    term_zh: '检索增强生成',
    desc: '在 LLM 回答前提早从私域知识库检索高相关文档作为上下文，有效解决模型事实幻觉与知识时效性问题。'
  },
  'sql': {
    term_en: 'SQL',
    term_zh: '结构化查询语言',
    desc: '用于在关系型数据库（如 PostgreSQL/MySQL）中定义表结构、执行增删改查及事务管理的核心标准语言。'
  },
  'nosql': {
    term_en: 'NoSQL',
    term_zh: '非关系型数据库',
    desc: '针对海量数据高并发读写与灵活半结构化模式设计的分布式数据库（如 Redis, MongoDB, Cassandra）。'
  },
  'docker': {
    term_en: 'Docker',
    term_zh: '应用容器引擎',
    desc: '基于 Linux Namespace 与 Cgroups 的轻量级虚拟化，将应用及其全部运行依赖打包为高可移植的自给镜像。'
  },
  'k8s': {
    term_en: 'Kubernetes (K8s)',
    term_zh: '容器集群编排系统',
    desc: '自动化容器集群管理平台，负责海量容器的自动化部署调度、弹性横向伸缩、滚动升级与故障自愈。'
  }
};

function isValidKeyword(kw) {
  if (!kw || typeof kw !== 'string') return false;
  kw = kw.trim().replace(/^#+/, '').trim();
  if (kw.length < 2 || kw.length > 45) return false;
  const kwLower = kw.toLowerCase();
  if (ENGLISH_STOP_WORDS.has(kwLower) || NON_ACADEMIC_TERMS.has(kwLower)) return false;
  if (!kw.includes(' ')) {
    if (/^\d+$/.test(kw)) return false;
    // 2-3 字母缩写检查
    if (kw.length < 4) {
      if (!(kw === kw.toUpperCase() && kw.length >= 2)) return false;
      if (NON_ACADEMIC_TERMS.has(kwLower)) return false;
    }
    if (ENGLISH_STOP_WORDS.has(kwLower) || NON_ACADEMIC_TERMS.has(kwLower)) return false;
  } else {
    const parts = kwLower.split(/\s+/);
    if (parts.every(p => ENGLISH_STOP_WORDS.has(p) || NON_ACADEMIC_TERMS.has(p))) return false;
  }
  return true;
}

// DOM Elements
const el = {
  btnToggleCourseDrawer: document.getElementById('btnToggleCourseDrawer'),
  btnCloseCourseDrawer: document.getElementById('btnCloseCourseDrawer'),
  courseDrawer: document.getElementById('courseDrawer'),
  courseSidebarList: document.getElementById('courseSidebarList'),
  courseDrawerScopeLabel: document.getElementById('courseDrawerScopeLabel'),
  btnDrawerUpgrade: document.getElementById('btnDrawerUpgrade'),
  sessionTitleInput: document.getElementById('sessionTitleInput'),
  statusPill: document.getElementById('statusPill'),
  statusText: document.getElementById('statusText'),
  timerText: document.getElementById('timerText'),
  btnExportMenu: document.getElementById('btnExportMenu'),
  exportDropdown: document.getElementById('exportDropdown'),
  exportDocx: document.getElementById('exportDocx'),
  exportMd: document.getElementById('exportMd'),
  exportSrt: document.getElementById('exportSrt'),
  btnAccount: document.getElementById('btnAccount'),
  accountBtnLabel: document.getElementById('accountBtnLabel'),
  accountPlanChip: document.getElementById('accountPlanChip'),
  btnSettings: document.getElementById('btnSettings'),
  btnToggleRecord: document.getElementById('btnToggleRecord'),
  recordIcon: document.getElementById('recordIcon'),
  recordText: document.getElementById('recordText'),
  audioSourceSelect: document.getElementById('audioSourceSelect'),
  sourceLangSelect: document.getElementById('sourceLangSelect'),
  waveformCanvas: document.getElementById('waveformCanvas'),
  searchInput: document.getElementById('searchInput'),
  btnGenerateSummary: document.getElementById('btnGenerateSummary'),
  btnSubtitles: document.getElementById('btnSubtitles'),
  btnClearNotes: document.getElementById('btnClearNotes'),
  cardsContainer: document.getElementById('cardsContainer'),
  cardsList: document.getElementById('cardsList'),
  emptyState: document.getElementById('emptyState'),
  blackboardSidebar: document.getElementById('blackboardSidebar'),
  summaryOverview: document.getElementById('summaryOverview'),
  takeawaysList: document.getElementById('takeawaysList'),
  glossaryList: document.getElementById('glossaryList'),
  btnRefreshSummary: document.getElementById('btnRefreshSummary'),
  activeDock: document.getElementById('activeDock'),
  dockStateLabel: document.getElementById('dockStateLabel'),
  activeSourceText: document.getElementById('activeSourceText'),
  btnForceFlush: document.getElementById('btnForceFlush'),
  apiWarningBanner: document.getElementById('apiWarningBanner'),
  apiWarningText: document.getElementById('apiWarningText'),
  btnFixApiKey: document.getElementById('btnFixApiKey'),

  // Account & Plans Modal
  accountModal: document.getElementById('accountModal'),
  btnCloseAccount: document.getElementById('btnCloseAccount'),
  accountSignedInBox: document.getElementById('accountSignedInBox'),
  acctUsernameDisplay: document.getElementById('acctUsernameDisplay'),
  acctPlanBadge: document.getElementById('acctPlanBadge'),
  acctExpiryText: document.getElementById('acctExpiryText'),
  btnLogout: document.getElementById('btnLogout'),
  ultraQuotaBox: document.getElementById('ultraQuotaBox'),
  qOverviewText: document.getElementById('qOverviewText'),
  qSlideText: document.getElementById('qSlideText'),
  qQaText: document.getElementById('qQaText'),
  authGuestBox: document.getElementById('authGuestBox'),
  authTabLogin: document.getElementById('authTabLogin'),
  authTabRegister: document.getElementById('authTabRegister'),
  authForm: document.getElementById('authForm'),
  authUsername: document.getElementById('authUsername'),
  authPassword: document.getElementById('authPassword'),
  authHintText: document.getElementById('authHintText'),
  btnAuthSubmit: document.getElementById('btnAuthSubmit'),
  authErrorMsg: document.getElementById('authErrorMsg'),
  redeemCodeInput: document.getElementById('redeemCodeInput'),
  btnRedeemCode: document.getElementById('btnRedeemCode'),
  redeemMsg: document.getElementById('redeemMsg'),
  payPlanSwitch: document.getElementById('payPlanSwitch'),
  payRemarkHint: document.getElementById('payRemarkHint'),
  payMethodSwitch: document.getElementById('payMethodSwitch'),
  payReceiptInput: document.getElementById('payReceiptInput'),
  btnSubmitPayClaim: document.getElementById('btnSubmitPayClaim'),
  payClaimMsg: document.getElementById('payClaimMsg'),
  
  // Settings Modal
  settingsModal: document.getElementById('settingsModal'),
  btnCloseSettings: document.getElementById('btnCloseSettings'),
  btnCancelSettings: document.getElementById('btnCancelSettings'),
  btnSaveSettings: document.getElementById('btnSaveSettings'),
  providerPresets: document.getElementById('providerPresets'),
  cfgProvider: document.getElementById('cfgProvider'),
  cfgApiKey: document.getElementById('cfgApiKey'),
  cfgModelName: document.getElementById('cfgModelName'),
  cfgCustomEndpoint: document.getElementById('cfgCustomEndpoint'),
  groupCustomEndpoint: document.getElementById('groupCustomEndpoint'),
  cfgUseLlmLiveTranslate: document.getElementById('cfgUseLlmLiveTranslate'),
  cfgPauseThreshold: document.getElementById('cfgPauseThreshold'),
  valPauseThreshold: document.getElementById('valPauseThreshold'),

  // Slide Viewer & Sync Controls
  mainLayout: document.getElementById('mainLayout'),
  slideStagePane: document.getElementById('slideStagePane'),
  slideFileInput: document.getElementById('slideFileInput'),
  btnUploadSlide: document.getElementById('btnUploadSlide'),
  uploadSlideBtnText: document.getElementById('uploadSlideBtnText'),
  btnDropzoneUpload: document.getElementById('btnDropzoneUpload'),
  btnHideSlidesFromEmpty: document.getElementById('btnHideSlidesFromEmpty'),
  btnToggleSlidePane: document.getElementById('btnToggleSlidePane'),
  toggleSlidePaneText: document.getElementById('toggleSlidePaneText'),
  slideFileName: document.getElementById('slideFileName'),
  slidePageIndicator: document.getElementById('slidePageIndicator'),
  btnAutoFollow: document.getElementById('btnAutoFollow'),
  autoFollowText: document.getElementById('autoFollowText'),
  btnSlidePrev: document.getElementById('btnSlidePrev'),
  btnSlideNext: document.getElementById('btnSlideNext'),
  btnZoomOut: document.getElementById('btnZoomOut'),
  btnZoomFit: document.getElementById('btnZoomFit'),
  btnZoomIn: document.getElementById('btnZoomIn'),
  slideCanvasStage: document.getElementById('slideCanvasStage'),
  slideDropzone: document.getElementById('slideDropzone'),
  slideCanvasWrap: document.getElementById('slideCanvasWrap'),
  slideCanvas: document.getElementById('slideCanvas'),
  slideStrip: document.getElementById('slideStrip'),
  currentSlidePagePill: document.getElementById('currentSlidePagePill'),
  currentSlideSumTitle: document.getElementById('currentSlideSumTitle'),
  currentSlideSpokenCount: document.getElementById('currentSlideSpokenCount'),
  btnSummarizeCurrentSlide: document.getElementById('btnSummarizeCurrentSlide'),
  currentSlideSummaryBody: document.getElementById('currentSlideSummaryBody'),

  // Right Workspace Tabs & Slide Overview / Timeline
  tabBtnLive: document.getElementById('tabBtnLive'),
  tabBtnSlides: document.getElementById('tabBtnSlides'),
  tabBtnGlossary: document.getElementById('tabBtnGlossary'),
  tabLiveCount: document.getElementById('tabLiveCount'),
  tabSlidesCount: document.getElementById('tabSlidesCount'),
  tabGlossaryCount: document.getElementById('tabGlossaryCount'),
  tabPanelLive: document.getElementById('tabPanelLive'),
  tabPanelSlides: document.getElementById('tabPanelSlides'),
  tabPanelGlossary: document.getElementById('tabPanelGlossary'),
  btnFilterAllCards: document.getElementById('btnFilterAllCards'),
  btnFilterCurrentSlide: document.getElementById('btnFilterCurrentSlide'),
  liveSubbarHint: document.getElementById('liveSubbarHint'),
  btnRefreshSlideOverview: document.getElementById('btnRefreshSlideOverview'),
  slideOverviewBody: document.getElementById('slideOverviewBody'),
  timelineSummaryMeta: document.getElementById('timelineSummaryMeta'),
  slideTimelineList: document.getElementById('slideTimelineList')
};

// Slide Sync State (kept separate from serializable state.session so pdfDoc binary never hits localStorage)
const slideState = {
  pdfDoc: null,
  fileName: '',
  totalPages: 0,
  currentPage: 1,
  zoom: 1.0,
  fitMode: true,
  autoFollow: true,
  filterCurrentSlideOnly: false,
  slidesPaneHidden: false,
  pagesData: [],           // [{ page, title, text, tokens: Set<string>, upperAcronyms: Set<string> }]
  renderTask: null,
  candidatePage: null,
  candidateHits: 0,
  summarizingPages: new Set(),
  overviewLoading: false
};

if (window.pdfjsLib) {
  window.pdfjsLib.GlobalWorkerOptions.workerSrc =
    'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
}

const PROVIDER_PRESETS = {
  gemini: { provider: 'gemini', endpoint: '', defaultModelHint: '留空默认 gemini-2.5-flash' },
  deepseek: { provider: 'openai_compatible', endpoint: 'https://api.deepseek.com/v1', defaultModelHint: '留空默认 deepseek-chat' },
  openai: { provider: 'openai_compatible', endpoint: 'https://api.openai.com/v1', defaultModelHint: '留空默认 gpt-4o-mini' },
  siliconflow: { provider: 'openai_compatible', endpoint: 'https://api.siliconflow.cn/v1', defaultModelHint: '留空默认 Qwen/Qwen2.5-72B-Instruct' },
  qwen: { provider: 'openai_compatible', endpoint: 'https://dashscope.aliyuncs.com/compatible-mode/v1', defaultModelHint: '留空默认 qwen-plus' },
  openrouter: { provider: 'openai_compatible', endpoint: 'https://openrouter.ai/api/v1', defaultModelHint: '留空默认 google/gemini-2.5-flash' }
};

// Initialize
async function init() {
  loadConfig();
  loadSavedSession();
  setupEventListeners();
  setupSlideSyncListeners();
  setupAccountListeners();
  updateAccountUI();
  renderSlideOverviewUI();
  renderSlideTimelineUI();
  renderCurrentSlideSummaryUI();
  updateTabBadges();
  lucide.createIcons();
  await refreshAuthUser();
  fetchHistorySessions();
  // Lightweight anonymous visit count for author dashboard
  fetch('/api/track', {
    method: 'POST',
    headers: apiHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ event_type: 'page_app' })
  }).catch(() => {});

  // If navigated from landing page pricing CTA (?upgrade=pro or ?upgrade=ultra)
  const params = new URLSearchParams(window.location.search);
  if (params.get('upgrade') && !isPaidUser()) {
    openAccountModal('登录或 5 秒注册账号后，输入 Pro / Ultra 激活码即可立即解锁全部功能。');
  }
}

// Load / Save Config
function loadConfig() {
  const saved = localStorage.getItem('lecture_scribe_config');
  if (saved) {
    try {
      state.config = { ...state.config, ...JSON.parse(saved) };
    } catch (e) {}
  }
  if (state.config.modelName && (state.config.modelName.includes('3.7') || state.config.modelName.includes('3.6') || state.config.modelName.includes('3.8'))) {
    state.config.modelName = '';
  }
  el.cfgProvider.value = state.config.provider || 'gemini';
  el.cfgApiKey.value = state.config.apiKey || '';
  el.cfgModelName.value = state.config.modelName || '';
  el.cfgCustomEndpoint.value = state.config.customEndpoint || '';
  if (el.cfgUseLlmLiveTranslate) {
    el.cfgUseLlmLiveTranslate.checked = !!state.config.useLlmLiveTranslate;
  }
  if (!state.config.pauseThreshold || state.config.pauseThreshold > 3.5) {
    state.config.pauseThreshold = 1.4;
  }
  el.cfgPauseThreshold.value = state.config.pauseThreshold;
  el.valPauseThreshold.textContent = state.config.pauseThreshold + ' 秒';
  state.pauseThreshold = parseFloat(state.config.pauseThreshold);
  updateProviderVisibility();
  updateLiveSubbarHint();
}

function saveConfig() {
  state.config.provider = el.cfgProvider.value;
  state.config.apiKey = el.cfgApiKey.value.trim();
  state.config.modelName = el.cfgModelName.value.trim(); // empty = auto default model!
  state.config.customEndpoint = el.cfgCustomEndpoint.value.trim();
  state.config.useLlmLiveTranslate = !!(el.cfgUseLlmLiveTranslate && el.cfgUseLlmLiveTranslate.checked);
  state.config.pauseThreshold = parseFloat(el.cfgPauseThreshold.value);
  state.pauseThreshold = state.config.pauseThreshold;
  localStorage.setItem('lecture_scribe_config', JSON.stringify(state.config));
  el.apiWarningBanner.classList.remove('show');
  updateLiveSubbarHint();
  showToast(state.config.modelName ? `设置已保存（模型：${state.config.modelName}）` : '设置已保存（将自动调用默认模型）');
}

function updateLiveSubbarHint() {
  if (!el.liveSubbarHint) return;
  const usingLlmLive = !!(state.config.useLlmLiveTranslate && state.config.apiKey && state.config.provider !== 'free_google');
  el.liveSubbarHint.textContent = usingLlmLive ? '个人 Key 精译模式' : '实时免费直连';
}

function updateProviderVisibility() {
  const prov = el.cfgProvider.value;
  if (prov === 'openai_compatible') {
    el.groupCustomEndpoint.style.display = 'flex';
  } else {
    el.groupCustomEndpoint.style.display = 'none';
  }
}

// Local Multi-Course Archive (ensures creating a new course NEVER loses previous courses)
const COURSES_ARCHIVE_KEY = 'lecture_scribe_courses_v1';
const MAX_LOCAL_ARCHIVED_COURSES = 25;

function hasSessionContent(session) {
  if (!session) return false;
  if (Array.isArray(session.items) && session.items.length > 0) return true;
  if (session.slide_meta && session.slide_meta.file_name) return true;
  if (session.slide_overview) return true;
  if (session.slide_summaries && Object.keys(session.slide_summaries).length > 0) return true;
  if (session.summary && session.summary.overview && session.summary.overview !== '尚未生成概览') return true;
  return false;
}

function loadLocalCoursesMap() {
  try {
    const raw = localStorage.getItem(COURSES_ARCHIVE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    const now = Date.now();
    const cleaned = {};
    let changed = false;
    Object.entries(parsed).forEach(([id, sess]) => {
      if (!sess || !sess.id) { changed = true; return; }
      const tsMs = (sess.updated_at || sess.created_at || 0) * 1000;
      if (tsMs && now - tsMs > SESSION_RETENTION_MS) { changed = true; return; }
      cleaned[id] = sess;
    });
    if (changed) saveLocalCoursesMap(cleaned);
    return cleaned;
  } catch (e) {
    return {};
  }
}

function saveLocalCoursesMap(map) {
  const entries = Object.values(map || {})
    .filter(s => s && s.id)
    .sort((a, b) => (b.updated_at || b.created_at || 0) - (a.updated_at || a.created_at || 0))
    .slice(0, MAX_LOCAL_ARCHIVED_COURSES);
  const pruned = {};
  entries.forEach(s => { pruned[s.id] = s; });

  while (true) {
    try {
      localStorage.setItem(COURSES_ARCHIVE_KEY, JSON.stringify(pruned));
      return true;
    } catch (e) {
      const keys = Object.keys(pruned).filter(k => k !== state.session.id);
      if (keys.length === 0) return false;
      // Drop the oldest non-active course to free space
      delete pruned[keys[keys.length - 1]];
    }
  }
}

function archiveSessionLocally(session) {
  if (!session || !session.id) return;
  const map = loadLocalCoursesMap();
  if (!hasSessionContent(session) && !map[session.id]) return;
  map[session.id] = JSON.parse(JSON.stringify(session));
  saveLocalCoursesMap(map);
}

function removeLocalCourse(sessionId) {
  const map = loadLocalCoursesMap();
  if (map[sessionId]) {
    delete map[sessionId];
    saveLocalCoursesMap(map);
  }
}

// Load / Save Current Session
function loadSavedSession() {
  const saved = localStorage.getItem('lecture_scribe_current_session');
  if (saved) {
    try {
      const data = JSON.parse(saved);
      const savedAtMs = (data.updated_at || data.created_at || 0) * 1000;
      if (savedAtMs && Date.now() - savedAtMs > SESSION_RETENTION_MS) {
        localStorage.removeItem('lecture_scribe_current_session');
        showToast('已自动清理一周前的课堂缓存');
        return;
      }
      if (data && hasSessionContent(data)) {
        if (!Array.isArray(data.items)) data.items = [];
        if (!data.slide_summaries) data.slide_summaries = {};
        // Sanitize legacy items: strip stopwords & non-academic terms (e.g. US, EU, How, And, Poor)
        data.items.forEach(it => {
          if (it.keywords && Array.isArray(it.keywords)) {
            it.keywords = it.keywords.filter(kw => isValidKeyword(kw) && !NON_ACADEMIC_TERMS.has(kw.trim().toLowerCase()));
          }
          if (it.annotations && Array.isArray(it.annotations)) {
            it.annotations = it.annotations.filter(a => {
              const term = (a.term || '').trim().toLowerCase();
              return isValidKeyword(term) && !NON_ACADEMIC_TERMS.has(term);
            });
          }
        });
        if (data.summary && data.summary.glossary && Array.isArray(data.summary.glossary)) {
          data.summary.glossary = data.summary.glossary.filter(g => {
            const term = (g.term_en || g.term_zh || '').trim().toLowerCase();
            return isValidKeyword(term) && !NON_ACADEMIC_TERMS.has(term);
          });
        }
        state.session = data;
        state.elapsedSeconds = data.duration_seconds || 0;
        el.sessionTitleInput.value = state.session.title || '计算机体系结构 Lecture 01';
        el.timerText.textContent = formatTimeFull(state.elapsedSeconds);
        if (data.slide_meta && data.slide_meta.file_name && el.slideFileName) {
          el.slideFileName.textContent = `${data.slide_meta.file_name}（点击重传恢复原图）`;
        }
        archiveSessionLocally(state.session);
        renderCards();
        renderSummaryUI(state.session.summary);
        renderSlideOverviewUI();
        renderSlideTimelineUI();
        renderCurrentSlideSummaryUI();
        updateSidebarOutline();
        updateStats();
        updateTabBadges();
      }
    } catch (e) {}
  }
}

// The cloud copy only feeds the history list, and each upload re-sends the
// whole lecture, so batch uploads instead of sending one after every card.
const CLOUD_SAVE_INTERVAL_MS = 20000;
// Lecture notes are for the current week; older ones aren't restored locally
// and the server drops them too.
const SESSION_RETENTION_MS = 7 * 24 * 60 * 60 * 1000;
let cloudSaveTimer = null;
let cloudSaveTarget = null;

function uploadSession(session) {
  fetch('/api/sessions', {
    method: 'POST',
    headers: apiHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify(session)
  }).catch(() => {});
}

// Serializing a long lecture costs milliseconds per megabyte and a 4000-card
// session runs to several, so don't rewrite it after every single card.
const LOCAL_SAVE_MIN_INTERVAL_MS = 3000;
let localSaveTimer = null;
let lastLocalSaveAt = 0;
let localStorageFullWarned = false;

function writeSessionLocally() {
  localSaveTimer = null;
  lastLocalSaveAt = Date.now();
  try {
    localStorage.setItem('lecture_scribe_current_session', JSON.stringify(state.session));
    if (hasSessionContent(state.session)) {
      archiveSessionLocally(state.session);
    }
    renderCourseSidebarList();
  } catch (e) {
    // Browser storage is full after a very long lecture; the cloud copy still has it.
    console.warn('Local session save failed:', e);
    if (!localStorageFullWarned) {
      localStorageFullWarned = true;
      showToast('本地存储已满，建议在左侧边栏新建一节新课，旧记录仍保存在云端');
    }
  }
}

function persistSession(immediate = false) {
  state.session.title = el.sessionTitleInput.value.trim() || '未命名课程';
  state.session.updated_at = Date.now() / 1000;
  state.session.duration_seconds = state.elapsedSeconds;

  if (immediate) {
    clearTimeout(localSaveTimer);
    writeSessionLocally();
  } else if (!localSaveTimer) {
    localSaveTimer = setTimeout(writeSessionLocally, Math.max(0, LOCAL_SAVE_MIN_INTERVAL_MS - (Date.now() - lastLocalSaveAt)));
  }

  if (cloudSaveTarget && cloudSaveTarget !== state.session) {
    // A different lecture is open now; don't drop the previous one's pending upload.
    uploadSession(cloudSaveTarget);
  }
  cloudSaveTarget = state.session;

  if (immediate) {
    clearTimeout(cloudSaveTimer);
    cloudSaveTimer = null;
    cloudSaveTarget = null;
    uploadSession(state.session);
  } else if (!cloudSaveTimer) {
    cloudSaveTimer = setTimeout(() => {
      const session = cloudSaveTarget;
      cloudSaveTimer = null;
      cloudSaveTarget = null;
      if (session) uploadSession(session);
    }, CLOUD_SAVE_INTERVAL_MS);
  }
}

// Audio Visualizer
async function startAudioMonitoring() {
  try {
    const isSystem = el.audioSourceSelect.value === 'system';
    if (isSystem) {
      state.mediaStream = await navigator.mediaDevices.getDisplayMedia({
        audio: true,
        video: true
      });
    } else {
      state.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });
    }

    state.audioContext = new (window.AudioContext || window.webkitAudioContext)();
    const source = state.audioContext.createMediaStreamSource(state.mediaStream);
    state.analyser = state.audioContext.createAnalyser();
    state.analyser.fftSize = 64;
    source.connect(state.analyser);

    micCaptureError = '';
    drawWaveform();
  } catch (err) {
    console.warn('Audio capture warning:', err);
    micCaptureError = err.name || 'unknown';
    showToast('麦克风提示: ' + (err.message || '请允许麦克风权限以收音'));
  }
}

function stopAudioMonitoring() {
  if (state.mediaStream) {
    state.mediaStream.getTracks().forEach(t => t.stop());
    state.mediaStream = null;
  }
  if (state.audioContext && state.audioContext.state !== 'closed') {
    state.audioContext.close();
  }
}

function drawWaveform() {
  if (!state.isRecording || !state.analyser) return;
  const canvas = el.waveformCanvas;
  const ctx = canvas.getContext('2d');
  const bufferLength = state.analyser.frequencyBinCount;
  const dataArray = new Uint8Array(bufferLength);

  function render() {
    if (!state.isRecording || !state.analyser) {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }
    requestAnimationFrame(render);
    state.analyser.getByteFrequencyData(dataArray);

    ctx.fillStyle = '#EDE8DF';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    const barWidth = (canvas.width / bufferLength) * 1.5;
    let x = 0;
    for (let i = 0; i < bufferLength; i++) {
      const barHeight = (dataArray[i] / 255) * canvas.height;
      ctx.fillStyle = barHeight > canvas.height * 0.7 ? '#1B7A43' : '#315BCE';
      ctx.fillRect(x, canvas.height - barHeight, barWidth, barHeight);
      x += barWidth + 1;
    }
  }
  render();
}

// Speech Recognition Engine
// The browser's recognizer ends itself periodically and also drops out on
// network hiccups. Restarting it once wasn't enough: start() throws while the
// previous run is still tearing down, and that left the session dead with the
// button still showing "recording". It can also stay nominally running while
// quietly delivering nothing, which only a forced restart recovers from.
const RECOGNITION_SILENCE_LIMIT_MS = 12000;
// A silent room explains silent transcription, so only force a restart that
// soon when the mic is actually picking something up; otherwise wait this long.
const RECOGNITION_HARD_LIMIT_MS = 60000;
const AUDIO_ACTIVITY_LEVEL = 6;
let recognitionRestartTimer = null;
let recognitionRestartDelay = 300;
let recognitionWatchdogTimer = null;
let lastRecognitionResultAt = 0;
let lastAudioAt = 0;
let lastRecoveryNoticeAt = 0;
let lastNetworkNoticeAt = 0;
let recognitionNetworkErrors = 0;
let micCaptureError = '';
let lastMicNoticeAt = 0;

// "Recording" with a flat level meter and no transcription means the audio
// never reached the browser at all, which no amount of restarting fixes. Say
// so in the dock, where it can't be missed, instead of looking busy.
function reportMicrophoneProblem() {
  if (!state.isRecording) return false;
  let problem = '';
  if (micCaptureError === 'NotAllowedError') {
    problem = '麦克风权限被拒绝：点地址栏左边的图标，把麦克风改成「允许」后重新开始收音';
  } else if (micCaptureError === 'NotFoundError' || micCaptureError === 'OverconstrainedError') {
    problem = '系统里找不到可用的麦克风：请检查设备是否插好、是否被禁用';
  } else if (micCaptureError) {
    problem = '麦克风打不开（' + micCaptureError + '）：可能被其他软件占用，关掉会议/录音软件后重试';
  } else if (!state.analyser) {
    problem = '还没拿到麦克风，请在浏览器弹窗里允许麦克风权限';
  } else if (Date.now() - lastAudioAt > 15000) {
    problem = '麦克风电平一直是 0：请检查 Windows 默认输入设备选对没有、麦克风是否静音、是否被会议软件占用';
  }
  if (!problem) {
    // Mic recovered: drop a warning left in the dock.
    if (el.activeSourceText.textContent.startsWith('⚠️')) {
      el.activeSourceText.textContent = '等待声音输入中...';
    }
    return false;
  }

  el.activeSourceText.textContent = '⚠️ ' + problem;
  if (Date.now() - lastMicNoticeAt > 60000) {
    lastMicNoticeAt = Date.now();
    showToast('⚠️ ' + problem);
  }
  return true;
}

function noteRecognitionActivity() {
  lastRecognitionResultAt = Date.now();
}

function currentAudioLevel() {
  if (!state.analyser) return 0;
  const data = new Uint8Array(state.analyser.frequencyBinCount);
  state.analyser.getByteFrequencyData(data);
  let sum = 0;
  for (let i = 0; i < data.length; i++) sum += data[i];
  return sum / data.length;
}

async function warnIfNoMicrophone() {
  try {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
    const devices = await navigator.mediaDevices.enumerateDevices();
    if (!devices.some(d => d.kind === 'audioinput')) {
      showToast('系统里没有找到麦克风设备，请插上麦克风或检查声音输入设置');
    }
  } catch (e) {}
}

function scheduleRecognitionRestart() {
  if (!state.isRecording || recognitionRestartTimer) return;
  recognitionRestartTimer = setTimeout(() => {
    recognitionRestartTimer = null;
    if (!state.isRecording || !state.recognition) return;
    try {
      state.recognition.start();
      recognitionRestartDelay = 300;
      noteRecognitionActivity();
    } catch (e) {
      // Still running or still shutting down: back off and try again rather
      // than giving up on the whole lecture.
      recognitionRestartDelay = Math.min(recognitionRestartDelay * 2, 5000);
      scheduleRecognitionRestart();
    }
  }, recognitionRestartDelay);
}

function startRecognitionWatchdog() {
  stopRecognitionWatchdog();
  noteRecognitionActivity();
  recognitionWatchdogTimer = setInterval(() => {
    if (!state.isRecording || !state.recognition) return;
    if (currentAudioLevel() > AUDIO_ACTIVITY_LEVEL) lastAudioAt = Date.now();
    // No audio at all is a device/permission problem; restarting won't help.
    if (reportMicrophoneProblem()) return;
    const quietFor = Date.now() - lastRecognitionResultAt;
    if (quietFor < RECOGNITION_SILENCE_LIMIT_MS) return;
    // Restarting a healthy recognizer swallows the first words spoken after a
    // pause, so only do it early when the mic says someone is talking.
    const soundRecently = Date.now() - lastAudioAt < RECOGNITION_SILENCE_LIMIT_MS;
    if (!soundRecently && quietFor < RECOGNITION_HARD_LIMIT_MS) return;
    noteRecognitionActivity();
    try { state.recognition.stop(); } catch (e) {}
    scheduleRecognitionRestart();
    if (Date.now() - lastRecoveryNoticeAt > 30000) {
      lastRecoveryNoticeAt = Date.now();
      showToast('语音识别已自动重连，继续收音中');
    }
  }, 4000);
}

function stopRecognitionWatchdog() {
  clearInterval(recognitionWatchdogTimer);
  recognitionWatchdogTimer = null;
  clearTimeout(recognitionRestartTimer);
  recognitionRestartTimer = null;
  recognitionRestartDelay = 300;
}

function setupSpeechRecognition() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    alert('当前浏览器不支持内置语音转写，请使用 Google Chrome 或 Microsoft Edge 浏览器！');
    return null;
  }

  const recognition = new SpeechRecognition();
  recognition.continuous = true;
  recognition.interimResults = true;
  recognition.lang = el.sourceLangSelect.value;
  state.session.source_lang = el.sourceLangSelect.value.split('-')[0];

  recognition.onstart = () => {
    noteRecognitionActivity();
    updateStatus(true);
  };

  recognition.onresult = (event) => {
    noteRecognitionActivity();
    recognitionNetworkErrors = 0;
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; ++i) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        handleFinalSentence(transcript.trim());
      } else {
        interim += transcript;
      }
    }
    if (interim) {
      const trimmed = stripCardedWords(interim.trim());
      if (!trimmed) return;

      const prevTrimmed = (state.activeInterimText || '').trim();
      state.activeInterimText = trimmed;
      el.activeSourceText.textContent = trimmed;
      if (el.activeSourceText.parentElement) {
        el.activeSourceText.parentElement.scrollTop = el.activeSourceText.parentElement.scrollHeight;
      }
      setSubtitle(trimmed);
      queueLiveSubtitleTranslation();

      if (trimmed.length >= INTERIM_FLUSH_CHARS) {
        // 老师一直不停顿时，先切一张卡片，避免整段话堆在一起迟迟不翻译
        flushThoughtBuffer();
      } else if (trimmed !== prevTrimmed) {
        // 实时打入停顿计时器：当老师说完停顿超过设定阈值时，才聚合形成完整知识卡片，避免产生零碎断句
        resetPauseTimer();
      }
    }
  };

  recognition.onspeechend = () => {
    // 监测到老师停顿或换气时，立即成卡翻译，拒绝等待
    const hasThought = state.thoughtBuffer && state.thoughtBuffer.length > 0;
    const hasInterim = state.activeInterimText && state.activeInterimText.trim().length >= 3;
    if (hasThought || hasInterim) {
      setTimeout(() => {
        if (state.thoughtBuffer.length > 0 || (state.activeInterimText && state.activeInterimText.trim().length >= 3)) {
          flushThoughtBuffer();
        }
      }, 350);
    }
  };

  recognition.onerror = (event) => {
    console.warn('Speech recognition error:', event.error);
    if (event.error === 'not-allowed' || event.error === 'service-not-allowed') {
      showToast('麦克风权限被拒绝，请在浏览器地址栏允许麦克风权限！');
      toggleRecording(false);
      return;
    }
    if (event.error === 'audio-capture') {
      showToast('没有检测到麦克风输入，请检查系统声音设置或换一个麦克风后重新开始收音');
      toggleRecording(false);
      return;
    }
    if (event.error === 'network') {
      recognitionNetworkErrors++;
      if (recognitionNetworkErrors >= 3 && Date.now() - lastNetworkNoticeAt > 30000) {
        lastNetworkNoticeAt = Date.now();
        showToast('语音识别连不上服务器：Chrome 的语音转写需要访问 Google，请检查网络或 VPN');
      }
    }
    // network / aborted / no-speech: keep the lecture alive.
    scheduleRecognitionRestart();
  };

  recognition.onend = () => {
    if (state.isRecording) {
      // 浏览器转写周期结束时，如果缓冲区有未翻译文字立即触发成卡
      if (state.thoughtBuffer.length > 0 || (state.activeInterimText && state.activeInterimText.trim().length >= 3)) {
        flushThoughtBuffer();
      }
      // A restarted recognizer never finalizes the previous run's interim text.
      state.cardedInterim = '';
      scheduleRecognitionRestart();
    } else {
      updateStatus(false);
    }
  };

  return recognition;
}

function handleFinalSentence(sentence) {
  // This final result supersedes the interim preview of the same utterance.
  state.activeInterimText = '';
  sentence = stripCardedWords(sentence);
  state.cardedInterim = '';
  if (!sentence) return;
  state.thoughtBuffer.push(sentence);
  el.activeSourceText.textContent = state.thoughtBuffer.join(' ') + ' ...';
  if (el.activeSourceText.parentElement) {
    el.activeSourceText.parentElement.scrollTop = el.activeSourceText.parentElement.scrollHeight;
  }

  const joinedText = state.thoughtBuffer.join(' ');
  // 积累 2 个短句或总长度达 55 字符时，立刻输出知识卡片
  if (state.thoughtBuffer.length >= 2 || joinedText.length >= 55) {
    flushThoughtBuffer();
  } else {
    resetPauseTimer();
  }
}

function resetPauseTimer() {
  if (state.pauseTimer) clearTimeout(state.pauseTimer);
  state.pauseTimer = setTimeout(() => {
    // 无论是 thoughtBuffer 还是实时 interim 只要有字，停顿即立刻成卡翻译！
    const hasThought = state.thoughtBuffer && state.thoughtBuffer.length > 0;
    const hasInterim = state.activeInterimText && state.activeInterimText.trim().length >= 3;
    if (hasThought || hasInterim) {
      flushThoughtBuffer();
    }
  }, Math.max(600, (state.pauseThreshold || 1.8) * 1000));
}

// Flush Thought Buffer into a structured Knowledge Card
async function flushThoughtBuffer() {
  if (state.pauseTimer) clearTimeout(state.pauseTimer);
  const hasThought = state.thoughtBuffer && state.thoughtBuffer.length > 0;
  const hasInterim = state.activeInterimText && state.activeInterimText.trim().length >= 3;
  if (!hasThought && !hasInterim) return;

  const rawText = (state.thoughtBuffer.join(' ') + ' ' + state.activeInterimText).trim();
  const flushedInterim = normalizeSpeech(state.activeInterimText);
  state.thoughtBuffer = [];
  state.activeInterimText = '';
  el.activeSourceText.textContent = '等待声音输入中...';

  // Any live subtitle translation still in flight is now stale.
  const subtitleTurn = ++liveSubtitleTurn;
  lastLiveSubtitleText = '';

  if (!rawText || rawText.length < 3) return;
  if (flushedInterim) {
    state.cardedInterim = state.cardedInterim ? `${state.cardedInterim} ${flushedInterim}` : flushedInterim;
  }

  const cardTimeSec = Math.floor(state.elapsedSeconds);
  const timeStr = formatTime(cardTimeSec);

  const now = Date.now();
  const tempId = 'card_' + now;
  const activeSlidePage = slideState.totalPages > 0 ? slideState.currentPage : null;
  const newCard = {
    id: tempId,
    timestamp: now,
    time_sec: cardTimeSec,
    time_str: timeStr,
    slide_page: activeSlidePage,
    source_text: rawText,
    cleaned_source: rawText,
    translation: '正在生成双语译文...',
    keywords: [],
    annotations: [],
    section_heading: null,
    starred: false,
    loading: true
  };

  // 最新的内容放在最上面，把旧内容挤下去
  state.session.items.unshift(newCard);
  updateTabBadges();
  renderCards();
  if (state.activeQACardId) {
    anchorActiveCard();
  } else {
    scrollToTop();
  }

  // Trigger real-time voice -> slide auto-matching as soon as speech arrives
  if (slideState.totalPages > 0) {
    evaluateSlideAutoFollow(rawText, newCard);
    renderCurrentSlideSummaryUI();
  }

  // Start translating right away: default to blazing-fast free browser translation
  // unless the user explicitly enabled "use my API key for live translation" in Settings.
  const useLlmLive = !!(state.config.useLlmLiveTranslate && state.config.apiKey && state.config.apiKey.trim() && state.config.provider !== 'free_google');
  const browserTranslation = useLlmLive ? null : translateInBrowser(rawText, state.session.source_lang);

  let data = null;
  try {
    const res = await fetch('/api/translate', {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        text: rawText,
        source_lang: state.session.source_lang,
        target_lang: 'zh',
        provider: useLlmLive ? state.config.provider : 'free_google',
        api_key: useLlmLive ? state.config.apiKey : '',
        custom_endpoint: useLlmLive ? state.config.customEndpoint : '',
        model_name: useLlmLive ? state.config.modelName : ''
      })
    });
    if (res.ok) {
      data = await res.json();
    } else {
      console.warn('Translate API returned HTTP', res.status);
    }
  } catch (err) {
    console.error('Translate API error:', err);
  }

  if (data) {
    newCard.cleaned_source = data.cleaned_source || rawText;
    newCard.keywords = data.keywords || [];
    newCard.annotations = data.annotations || [];
    newCard.section_heading = data.section_heading || null;

    // Handle API Key error notice
    if (data.api_error) {
      el.apiWarningText.textContent = data.api_error;
      el.apiWarningBanner.classList.add('show');
    } else {
      el.apiWarningBanner.classList.remove('show');
    }
  }

  // The server only translates when an LLM key is configured; otherwise translate here.
  newCard.translation = (data && data.translation)
    || await (browserTranslation || translateInBrowser(newCard.cleaned_source, state.session.source_lang));
  newCard.translation_failed = !newCard.translation;
  newCard.loading = false;
  // Don't overwrite subtitles that have already moved on to the next sentence.
  if (liveSubtitleTurn === subtitleTurn) {
    setSubtitle(newCard.cleaned_source || newCard.source_text, newCard.translation || TRANSLATION_FAILED_TEXT);
  }

  state.totalTokensEst += Math.floor(rawText.length / 3) + Math.floor(newCard.translation.length * 1.5) + 120;
  updateStats();
  scheduleSidebarOutlineUpdate();

  persistSession();

  // 若用户当前正在查看某张卡片的助教解析，直接静默更新新卡片，完全避免重绘整屏打扰用户
  const newCardEl = document.querySelector(`.lecture-card[data-id="${newCard.id}"]`);
  if (state.activeQACardId && newCardEl) {
    const transEl = newCardEl.querySelector('.card-trans-text');
    if (transEl) transEl.textContent = newCard.translation || TRANSLATION_FAILED_TEXT;
    const sourceEl = newCardEl.querySelector('.card-source-text');
    if (sourceEl) sourceEl.textContent = newCard.cleaned_source || newCard.source_text;

    if (newCard.annotations && newCard.annotations.length > 0 && !newCardEl.querySelector('.card-annotations-box')) {
      const cardBody = newCardEl.querySelector('.card-body');
      if (cardBody) {
        const itemsHtml = newCard.annotations.map(a => `
          <div class="annotation-item">
            <div class="anno-term-row">
              <span class="anno-tag ${escapeHtml(a.type || '专业术语')}">${escapeHtml(a.type || '专业术语')}</span>
              <span class="anno-term">${escapeHtml(a.term)}</span>
            </div>
            <div class="anno-exp">${escapeHtml(a.explanation)}</div>
          </div>
        `).join('');
        cardBody.insertAdjacentHTML('beforeend', `
          <div class="card-annotations-box">
            <div class="anno-header">
              <i data-lucide="sparkles"></i>
              <span>AI 智能讲解与背景批注</span>
            </div>
            ${itemsHtml}
          </div>
        `);
      }
    }
    lucide.createIcons();
    anchorActiveCard();
    return;
  }

  renderCards();
  if (state.activeQACardId) {
    anchorActiveCard();
  }
  lucide.createIcons();
}

function toggleRecording(forceState) {
  const shouldStart = forceState !== undefined ? forceState : !state.isRecording;
  if (shouldStart) {
    if (!state.recognition) {
      state.recognition = setupSpeechRecognition();
      if (!state.recognition) return;
    }
    // Enter the recording state first. start() throws while a previous run is
    // still tearing down, and that used to skip everything below it: no timer,
    // no mic monitoring, button still saying "start", nothing retried.
    state.isRecording = true;
    startRecognitionWatchdog();
    startTimer();
    updateStatus(true);
    el.recordIcon.setAttribute('data-lucide', 'square');
    el.recordText.textContent = '暂停课堂收音';
    el.btnToggleRecord.classList.add('recording');
    el.statusPill.classList.add('recording');
    lucide.createIcons();

    try {
      state.recognition.lang = el.sourceLangSelect.value;
      state.recognition.start();
    } catch (e) {
      console.warn('Start err:', e);
      scheduleRecognitionRestart();
    }
    // Let the recognizer open the microphone first: grabbing it with
    // getUserMedia beforehand can stop it from capturing on some systems.
    startAudioMonitoring();
    warnIfNoMicrophone();
    showToast('已开始课堂听翻，正在收音...');
  } else {
    state.isRecording = false;
    stopRecognitionWatchdog();
    micCaptureError = '';
    if (state.recognition) {
      try { state.recognition.stop(); } catch (e) {}
    }
    stopTimer();
    stopAudioMonitoring();
    if (state.thoughtBuffer.length > 0) {
      flushThoughtBuffer();
    }
    updateStatus(false);
    el.recordIcon.setAttribute('data-lucide', 'mic');
    el.recordText.textContent = '继续上课收音';
    el.btnToggleRecord.classList.remove('recording');
    el.statusPill.classList.remove('recording');
    lucide.createIcons();
    persistSession(true);
  }
}

function startTimer() {
  if (!state.timerInterval) {
    state.startTime = Date.now() - (state.elapsedSeconds * 1000);
    state.timerInterval = setInterval(() => {
      state.elapsedSeconds = Math.floor((Date.now() - state.startTime) / 1000);
      el.timerText.textContent = formatTimeFull(state.elapsedSeconds);
    }, 1000);
  }
}

function stopTimer() {
  if (state.timerInterval) {
    clearInterval(state.timerInterval);
    state.timerInterval = null;
  }
}

function formatTime(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

function formatTimeFull(secs) {
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = Math.floor(secs % 60);
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}

function updateStatus(isRec) {
  if (el.statusText) el.statusText.textContent = isRec ? '正在收音' : '已暂停';
  if (el.activeDock) el.activeDock.classList.toggle('is-live', !!isRec);
  if (el.dockStateLabel) el.dockStateLabel.textContent = isRec ? '听音中' : '待机中';
}

function updateStats() {
  // Token counter intentionally removed for cleaner student reading focus
}

function getCardTimestamp(c) {
  if (c.timestamp) return c.timestamp;
  if (c.id && typeof c.id === 'string') {
    const num = parseInt(c.id.replace(/\D/g, ''), 10);
    if (!isNaN(num) && num > 1000000000000) return num;
  }
  return (c.time_sec || 0) * 1000;
}

function formatMarkdownAnswer(text) {
  if (!text) return '';
  let escaped = escapeHtml(text);
  // 加粗 **重点**
  escaped = escaped.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');
  // 结构化条目解析
  escaped = escaped.replace(/(?:^|\n)([\d+\.]|\-|\•)\s*(【.*?】|.*?)(?=\n|$)/g, '<div class="qa-point"><span class="point-bullet">$1</span><span>$2</span></div>');
  // 换行
  escaped = escaped.replace(/\n\n+/g, '<br><br>').replace(/\n/g, '<br>');
  return escaped;
}

function anchorActiveCard() {
  if (!state.activeQACardId) return;
  const targetCard = document.querySelector(`.lecture-card[data-id="${state.activeQACardId}"]`);
  if (targetCard && el.cardsContainer) {
    const containerRect = el.cardsContainer.getBoundingClientRect();
    const cardRect = targetCard.getBoundingClientRect();
    const offset = cardRect.top - containerRect.top - (containerRect.height / 2) + (cardRect.height / 2);
    el.cardsContainer.scrollBy({ top: offset, behavior: 'smooth' });
  }
}

// Render Knowledge Cards with AI Smart Annotations & Direct 1-Click QA
function renderCards() {
  const items = state.session.items;
  if (!items || items.length === 0) {
    el.emptyState.style.display = 'flex';
    el.cardsList.innerHTML = '';
    return;
  }
  el.emptyState.style.display = 'none';

  const prevScrollTop = el.cardsContainer.scrollTop;

  // 严格保证：按创建绝对时间倒序排列，最新生成的卡片 100% 绝对在最上面！
  let sortedItems = [...items].sort((a, b) => getCardTimestamp(b) - getCardTimestamp(a));

  let filtered = sortedItems;
  if (slideState.filterCurrentSlideOnly && slideState.totalPages > 0) {
    filtered = filtered.filter(it => it.slide_page === slideState.currentPage);
  }
  if (state.filterStarredOnly) {
    filtered = filtered.filter(it => it.starred);
  }
  if (state.searchQuery) {
    const q = state.searchQuery.toLowerCase();
    filtered = filtered.filter(it => 
      (it.cleaned_source || '').toLowerCase().includes(q) ||
      (it.translation || '').toLowerCase().includes(q) ||
      (it.keywords || []).some(k => k.toLowerCase().includes(q)) ||
      (it.annotations || []).some(a => (a.term + a.explanation).toLowerCase().includes(q))
    );
  }

  const isReviewing = !!(state.searchQuery || state.filterStarredOnly || slideState.filterCurrentSlideOnly);
  const limit = isReviewing ? FILTERED_CARD_LIMIT : RENDERED_CARD_LIMIT;
  const shown = filtered.slice(0, limit).map((card, idx) => ({ card, number: filtered.length - idx }));

  // Never let the card whose AI answer is open vanish out from under the reader.
  const activeIdx = state.activeQACardId ? filtered.findIndex(c => c.id === state.activeQACardId) : -1;
  if (activeIdx >= limit) {
    shown.push({ card: filtered[activeIdx], number: filtered.length - activeIdx });
  }

  const hiddenCount = Math.max(0, filtered.length - shown.length);

  let html = '';
  shown.forEach(({ card, number }) => {
    if (card.section_heading) {
      html += `
        <div class="section-divider">
          <div class="section-divider-line"></div>
          <div class="section-divider-pill">
            <i data-lucide="hash"></i>
            <span>${escapeHtml(card.section_heading)}</span>
          </div>
          <div class="section-divider-line"></div>
        </div>
      `;
    }

    const starClass = card.starred ? 'starred' : '';
    const starFill = card.starred ? 'fill="#f59e0b" color="#f59e0b"' : '';
    const isFocusCard = card.qa_open || state.activeQACardId === card.id;
    const focusClass = isFocusCard ? 'qa-active-focus' : '';

    const validKeywords = (card.keywords || []).filter(isValidKeyword);
    const keywordsHtml = validKeywords.map(kw => `
      <span class="keyword-pill"><i data-lucide="tag"></i>${escapeHtml(kw)}</span>
    `).join('');
    const keywordsBarHtml = validKeywords.length > 0 ? `
      <div class="card-keywords-bar">
        ${keywordsHtml}
      </div>
    ` : '';

    // AI Smart Annotations (💡 深度背景讲解与专业词汇批注)
    let annotationsHtml = '';
    if (card.annotations && card.annotations.length > 0) {
      const itemsHtml = card.annotations.map(a => `
        <div class="annotation-item">
          <div class="anno-term-row">
            <span class="anno-tag ${escapeHtml(a.type || '专业术语')}">${escapeHtml(a.type || '专业术语')}</span>
            <span class="anno-term">${escapeHtml(a.term)}</span>
          </div>
          <div class="anno-exp">${escapeHtml(a.explanation)}</div>
        </div>
      `).join('');

      const termPreview = card.annotations.map(a => a.term).filter(Boolean).slice(0, 3).join(' · ');
      annotationsHtml = `
        <details class="card-annotations-box">
          <summary>
            <span>术语批注 (${card.annotations.length})：<span class="anno-summary-terms">${escapeHtml(termPreview)}</span></span>
            <span class="anno-summary-toggle">展开</span>
          </summary>
          ${itemsHtml}
        </details>
      `;
    }

    // QA 抽屉：免用户手动打字，一键直接展示大模型全方位深度解析
    const drawerOpenClass = card.qa_open ? 'open' : '';
    const ansStyle = card.qa_open ? 'display:block;' : 'display:none;';

    let ansContent = '';
    if (card.qa_loading) {
      ansContent = `
        <div class="qa-loading-indicator">
          <span class="pulse-ring"></span>
          <span>🤖 助教正在深度拆解本段核心概念、原理与常考点...</span>
        </div>
      `;
    } else if (card.qa_answer) {
      if (isPaidUser()) {
        ansContent = `
          <div class="qa-ans-header">
            <div class="qa-ans-title">
              <i data-lucide="sparkles"></i>
              <span>AI 助教深度答疑与考点精讲</span>
            </div>
            <span class="qa-ans-badge">实时讲课精析</span>
          </div>
          <div class="qa-ans-content">${formatMarkdownAnswer(card.qa_answer)}</div>
        `;
      } else {
        const lines = String(card.qa_answer).split(/\n+/).filter(Boolean);
        const previewLines = lines.slice(0, 2).join('\n');
        const restLines = lines.slice(2).join('\n') || String(card.qa_answer);
        ansContent = `
          <div class="qa-ans-header">
            <div class="qa-ans-title">
              <i data-lucide="sparkles"></i>
              <span>AI 助教解析（免费预览前两行）</span>
            </div>
            <span class="qa-ans-badge">Pro / Ultra</span>
          </div>
          <div class="qa-ans-content">${formatMarkdownAnswer(previewLines)}</div>
          <div class="pro-gate-wrap">
            <div class="pro-gate-blur">${formatMarkdownAnswer(restLines)}</div>
            <div class="pro-gate-overlay">
              <div class="pro-gate-card">
                <span class="pro-gate-tag">Pro ¥4.9 · Ultra ¥8.9</span>
                <span class="pro-gate-text">升级解锁完整助教原理拆解与高频考点</span>
                <button type="button" class="pro-gate-btn" data-action="open-upgrade">立即解锁</button>
              </div>
            </div>
          </div>
        `;
      }
    }

    const qaBtnText = card.qa_loading 
      ? '助教正在深度解析...' 
      : (card.qa_answer 
          ? (card.qa_open ? '收起助教解析' : '查看助教深度解析') 
          : '问问助教 (一键深度解析)');

    const slideTagHtml = card.slide_page
      ? `<button type="button" class="card-slide-tag" data-action="jump-slide" data-page="${card.slide_page}" title="跳转到 Slide 第 ${card.slide_page} 页">P${card.slide_page}</button>`
      : '';

    html += `
      <div class="lecture-card ${starClass} ${focusClass}" data-id="${card.id}">
        <div class="card-header-bar">
          <div class="card-meta-left">
            <span class="card-time-pill">${card.time_str || '00:00'}</span>
            <span class="card-topic-pill">#${number}</span>
            ${slideTagHtml}
          </div>
          <button class="card-star-btn" data-action="star" data-id="${card.id}" title="${card.starred ? '取消标星' : '标星重点复习'}">
            <i data-lucide="star" ${starFill}></i>
          </button>
        </div>

        <div class="card-body">
          <div class="card-source-box">
            <span class="source-lang-label">讲师原声 (去口头禅)</span>
            <p class="card-source-text">${escapeHtml(card.cleaned_source || card.source_text)}</p>
          </div>

          <div class="card-translation-box">
            <span class="trans-lang-label">中文精译</span>
            <p class="card-trans-text">${card.translation_failed ? TRANSLATION_FAILED_TEXT : escapeHtml(card.translation)}</p>
          </div>

          ${annotationsHtml}
        </div>

        <div class="card-qa-bar">
          ${keywordsBarHtml}
          <button class="btn-ask-ai ${card.qa_open ? 'active' : ''}" data-action="ask-ai-direct" data-id="${card.id}">
            <i data-lucide="${card.qa_open ? 'chevron-up' : 'sparkles'}"></i>
            <span>${qaBtnText}</span>
          </button>
        </div>

        <!-- QA Drawer (免输入，一键直达深度解析，永久视觉锚定) -->
        <div class="card-qa-drawer ${drawerOpenClass}" id="qa_drawer_${card.id}">
          <div class="qa-answer-box" id="qa_ans_${card.id}" style="${ansStyle}">
            ${ansContent}
          </div>
        </div>
      </div>
    `;
  });

  if (hiddenCount > 0) {
    html += `<div class="render-limit-note">${isReviewing
      ? `仅显示前 ${limit} 条匹配结果，另有 ${hiddenCount} 条未列出，可缩小搜索范围或导出笔记查看。`
      : `仅显示最近 ${limit} 条卡片，更早的 ${hiddenCount} 条已完整保存，可用上方搜索查找或导出笔记查看。`}</div>`;
  }

  el.cardsList.innerHTML = html;
  lucide.createIcons();

  // 永久视觉锁定：如果用户正在查看某张卡片的答疑，无论后台生成多少新卡片，自动将该卡片锚定在视野中央，绝不被挤下去！
  if (state.activeQACardId) {
    anchorActiveCard();
  } else if (prevScrollTop > 60) {
    el.cardsContainer.scrollTop = prevScrollTop;
  }
}

function scrollToTop() {
  // 当用户正在查看助教答疑解析或在翻阅笔记时，绝不强制置顶滚动
  if (state.activeQACardId) return;
  const hasOpenDrawer = document.querySelector('.card-qa-drawer.open');
  if (hasOpenDrawer) return;
  if (el.cardsContainer.scrollTop > 60) return;
  el.cardsContainer.scrollTop = 0;
}

// Rebuilding the glossary scans every card of the lecture, so don't redo it
// after each new one either.
const SIDEBAR_OUTLINE_INTERVAL_MS = 3000;
let sidebarOutlineTimer = null;
let lastSidebarOutlineAt = 0;

function scheduleSidebarOutlineUpdate() {
  if (sidebarOutlineTimer) return;
  sidebarOutlineTimer = setTimeout(() => {
    sidebarOutlineTimer = null;
    lastSidebarOutlineAt = Date.now();
    updateSidebarOutline();
  }, Math.max(0, SIDEBAR_OUTLINE_INTERVAL_MS - (Date.now() - lastSidebarOutlineAt)));
}

// Blackboard Outline (专有名词术语表过滤与智能提取：杜绝空壳词条，必须具备真实专业定义)
function updateSidebarOutline() {
  if (state.session.summary && state.session.summary.glossary && state.session.summary.glossary.length > 0) {
    return;
  }

  const termMap = new Map();
  state.session.items.forEach(it => {
    // 1. 来自 AI 详细批注（annotations）
    if (it.annotations && Array.isArray(it.annotations)) {
      it.annotations.forEach(a => {
        if (!a || !a.term) return;
        const termName = a.term.trim();
        const termKey = termName.toLowerCase();
        if (!isValidKeyword(termName) || NON_ACADEMIC_TERMS.has(termKey)) return;

        let explanation = (a.explanation || '').trim();
        let typeName = (a.type || '专业术语').trim();

        // 若解释为模板废话或过短，尝试从内置专业词典补全
        if (!explanation || explanation.includes('高频学术') || explanation.includes('核心概念') || explanation.length < 6) {
          if (BUILTIN_TECH_GLOSSARY[termKey]) {
            explanation = BUILTIN_TECH_GLOSSARY[termKey].desc;
            if (typeName === '专业术语' && BUILTIN_TECH_GLOSSARY[termKey].term_zh) {
              typeName = BUILTIN_TECH_GLOSSARY[termKey].term_zh;
            }
          }
        }

        // 仅收录具有实质背景原理解释的词条，彻底杜绝“无解释/空壳”
        if (explanation && explanation.length >= 6 && !explanation.includes('高频学术')) {
          if (!termMap.has(termKey)) {
            termMap.set(termKey, {
              term_en: termName,
              term_zh: typeName,
              desc: explanation
            });
          }
        }
      });
    }

    // 2. 来自 keywords：仅当命中内置专业学术词典时才收录！坚决不生成空壳“高频学术/专业概念”
    if (it.keywords && Array.isArray(it.keywords)) {
      it.keywords.forEach(kw => {
        if (!kw || typeof kw !== 'string') return;
        const kwClean = kw.trim().replace(/^#+/, '').trim();
        const kwKey = kwClean.toLowerCase();
        if (NON_ACADEMIC_TERMS.has(kwKey) || !isValidKeyword(kwClean)) return;

        if (BUILTIN_TECH_GLOSSARY[kwKey]) {
          if (!termMap.has(kwKey)) {
            const entry = BUILTIN_TECH_GLOSSARY[kwKey];
            termMap.set(kwKey, {
              term_en: entry.term_en,
              term_zh: entry.term_zh,
              desc: entry.desc
            });
          }
        }
      });
    }
  });

  const uniqueTerms = Array.from(termMap.values()).slice(0, 15);

  if (uniqueTerms.length > 0) {
    el.glossaryList.innerHTML = uniqueTerms.map(item => `
      <div class="glossary-item">
        <div class="term-head">
          <span class="term-en">${escapeHtml(item.term_en)}</span>
          <span class="term-zh">${escapeHtml(item.term_zh)}</span>
        </div>
        <div class="term-desc">${escapeHtml(item.desc)}</div>
      </div>
    `).join('');
  } else {
    el.glossaryList.innerHTML = `
      <div class="glossary-empty">
        <p class="placeholder-text" style="text-align: center; padding: 16px 8px; line-height: 1.6;">
          暂未收录专有名词，遇到算法、法规或架构术语时自动汇总。
        </p>
      </div>
    `;
  }
  if (el.tabGlossaryCount) el.tabGlossaryCount.textContent = String(uniqueTerms.length);
}

async function generateSummary() {
  if (!isPaidUser()) {
    openAccountModal('「整课精要提炼」为 Pro（¥4.9/月）与 Ultra（¥8.9/月）会员功能，输入激活码即可立即解锁。');
    return;
  }
  if (state.session.items.length === 0) {
    showToast('当前课堂记录为空，请先录制讲课内容！');
    return;
  }
  showToast('正在调用大模型分析整堂课板书与重点...');
  el.btnGenerateSummary.disabled = true;

  try {
    const res = await fetch('/api/summarize', {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        session_title: el.sessionTitleInput.value.trim(),
        items: state.session.items,
        provider: state.config.provider,
        api_key: state.config.apiKey,
        custom_endpoint: state.config.customEndpoint,
        model_name: state.config.modelName
      })
    });

    if (res.ok) {
      const summary = await res.json();
      state.session.summary = summary;
      persistSession();
      renderSummaryUI(summary);
      refreshAuthUser();
      showToast('课堂精要板书提炼完成！');
    }
  } catch (e) {
    console.error('Summary error:', e);
    showToast('提炼总结失败，请检查网络或 API Key！');
  } finally {
    el.btnGenerateSummary.disabled = false;
  }
}

function renderSummaryUI(summary) {
  if (!summary) return;
  if (summary.overview) {
    el.summaryOverview.textContent = summary.overview;
  }
  if (summary.takeaways && summary.takeaways.length > 0) {
    el.takeawaysList.innerHTML = summary.takeaways.map(t => `<li>${escapeHtml(t)}</li>`).join('');
  }
  if (summary.glossary && summary.glossary.length > 0) {
    const validGlossary = [];
    const seen = new Set();
    summary.glossary.forEach(g => {
      const term = (g.term_en || g.term_zh || '').trim();
      const termKey = term.toLowerCase();
      if (!isValidKeyword(term) || NON_ACADEMIC_TERMS.has(termKey) || seen.has(termKey)) return;

      let desc = (g.desc || '').trim();
      let term_zh = (g.term_zh || '专业术语').trim();

      if ((!desc || desc.includes('高频学术') || desc.includes('核心概念') || desc.length < 6) && BUILTIN_TECH_GLOSSARY[termKey]) {
        desc = BUILTIN_TECH_GLOSSARY[termKey].desc;
        if (term_zh === '专业术语') term_zh = BUILTIN_TECH_GLOSSARY[termKey].term_zh;
      }

      if (desc && desc.length >= 6 && !desc.includes('高频学术')) {
        seen.add(termKey);
        validGlossary.push({
          term_en: g.term_en || term,
          term_zh: term_zh,
          desc: desc
        });
      }
    });

    if (validGlossary.length > 0) {
      el.glossaryList.innerHTML = validGlossary.map(g => `
        <div class="glossary-item">
          <div class="term-head">
            <span class="term-en">${escapeHtml(g.term_en || '')}</span>
            <span class="term-zh">${escapeHtml(g.term_zh || '')}</span>
          </div>
          <div class="term-desc">${escapeHtml(g.desc || '')}</div>
        </div>
      `).join('');
    } else {
      updateSidebarOutline();
    }
  }
}

// Collapsible Course Drawer & Multi-Course Switching
let cloudCoursesCache = [];
let courseDrawerOpen = false;

function setCourseDrawerOpen(open) {
  courseDrawerOpen = !!open;
  el.mainLayout?.classList.toggle('drawer-open', courseDrawerOpen);
  el.btnToggleCourseDrawer?.classList.toggle('active', courseDrawerOpen);
  el.btnToggleCourseDrawer?.setAttribute('aria-expanded', courseDrawerOpen ? 'true' : 'false');
  if (slideState.pdfDoc && slideState.fitMode) {
    setTimeout(() => renderPdfPage(slideState.currentPage), 230);
  }
}

function formatCourseTime(tsSec) {
  if (!tsSec) return '刚刚';
  const d = new Date(tsSec * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mi = String(d.getMinutes()).padStart(2, '0');
  return `${mm}-${dd} ${hh}:${mi}`;
}

function renderCourseSidebarList() {
  if (!el.courseSidebarList) return;
  const localMap = loadLocalCoursesMap();
  const merged = new Map();

  // 1. Cloud sessions summary
  (cloudCoursesCache || []).forEach(c => {
    if (!c || !c.id) return;
    merged.set(c.id, {
      id: c.id,
      title: c.title || '未命名课程',
      updated_at: c.updated_at || c.created_at || 0,
      item_count: c.item_count || 0,
      slide_sum_count: 0,
      has_slide: false
    });
  });

  // 2. Local full archived courses (richer metadata, instant availability)
  Object.values(localMap).forEach(s => {
    if (!s || !s.id) return;
    const prev = merged.get(s.id);
    const ts = Math.max(s.updated_at || s.created_at || 0, prev ? prev.updated_at : 0);
    merged.set(s.id, {
      id: s.id,
      title: s.title || (prev && prev.title) || '未命名课程',
      updated_at: ts,
      item_count: Math.max((s.items || []).length, prev ? prev.item_count : 0),
      slide_sum_count: Object.keys(s.slide_summaries || {}).length,
      has_slide: !!(s.slide_meta && s.slide_meta.file_name)
    });
  });

  // 3. Always include the currently open course at its latest state
  if (state.session && state.session.id) {
    merged.set(state.session.id, {
      id: state.session.id,
      title: (el.sessionTitleInput?.value || state.session.title || '未命名课程').trim() || '未命名课程',
      updated_at: state.session.updated_at || state.session.created_at || (Date.now() / 1000),
      item_count: (state.session.items || []).length,
      slide_sum_count: Object.keys(state.session.slide_summaries || {}).length,
      has_slide: !!(state.session.slide_meta && state.session.slide_meta.file_name)
    });
  }

  const courses = Array.from(merged.values()).sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));

  if (courses.length === 0) {
    el.courseSidebarList.innerHTML = `<span class="placeholder-text">暂无课程记录</span>`;
    return;
  }

  el.courseSidebarList.innerHTML = courses.map(c => {
    const isActive = c.id === state.session.id;
    const metaParts = [formatCourseTime(c.updated_at), `${c.item_count}段`];
    if (c.slide_sum_count > 0) metaParts.push(`${c.slide_sum_count}页总结`);
    else if (c.has_slide) metaParts.push('含课件');
    if (isActive) metaParts.push('当前');

    return `
      <div class="course-item ${isActive ? 'active' : ''}" data-session-id="${escapeHtml(c.id)}" role="button" tabindex="0">
        <div class="course-item-main">
          <div class="course-item-title">${escapeHtml(c.title)}</div>
          <div class="course-item-meta">${escapeHtml(metaParts.join(' · '))}</div>
        </div>
        <button type="button" class="course-item-del" data-action="delete-course" data-session-id="${escapeHtml(c.id)}" title="删除此课程" aria-label="删除课程">
          <i data-lucide="trash-2"></i>
        </button>
      </div>
    `;
  }).join('');
  lucide.createIcons();
}

async function fetchHistorySessions() {
  renderCourseSidebarList();
  try {
    const res = await fetch('/api/sessions', { headers: apiHeaders() });
    if (res.ok) {
      const list = await res.json();
      if (Array.isArray(list)) {
        cloudCoursesCache = list;
        renderCourseSidebarList();
      }
    }
  } catch (e) {}
}

function makeFreshBlankSession() {
  const nowSec = Date.now() / 1000;
  const d = new Date();
  const dateLabel = `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  return {
    id: 'session_' + Date.now(),
    title: `新课程 ${dateLabel}`,
    created_at: nowSec,
    updated_at: nowSec,
    source_lang: (el.sourceLangSelect?.value || 'en-US').split('-')[0],
    target_lang: 'zh',
    duration_seconds: 0,
    items: [],
    summary: null,
    slide_meta: null,
    slide_overview: null,
    slide_summaries: {}
  };
}

function applySessionToUI(sessionData) {
  if (!Array.isArray(sessionData.items)) sessionData.items = [];
  if (!sessionData.slide_summaries) sessionData.slide_summaries = {};
  state.session = sessionData;
  state.elapsedSeconds = sessionData.duration_seconds || 0;
  state.totalTokensEst = 0;
  state.activeQACardId = null;

  el.sessionTitleInput.value = sessionData.title || '未命名课程';
  el.timerText.textContent = formatTimeFull(state.elapsedSeconds);

  resetSlideViewer();
  if (sessionData.slide_meta && sessionData.slide_meta.file_name && el.slideFileName) {
    el.slideFileName.textContent = `${sessionData.slide_meta.file_name}（点击重传恢复原图）`;
  }

  renderCards();
  if (sessionData.summary) {
    renderSummaryUI(sessionData.summary);
  } else {
    el.summaryOverview.textContent = '尚未生成概览';
    el.takeawaysList.innerHTML = `<li class="placeholder-text">等待提炼核心要点...</li>`;
    updateSidebarOutline();
  }
  renderSlideOverviewUI();
  renderSlideTimelineUI();
  renderCurrentSlideSummaryUI();
  updateStats();
  updateTabBadges();
  renderCourseSidebarList();
}

function createNewCourse() {
  if (state.isRecording) {
    toggleRecording(false);
  }
  const prevTitle = (el.sessionTitleInput.value || state.session.title || '当前课程').trim();
  const prevHasContent = hasSessionContent(state.session);

  if (prevHasContent) {
    state.session.title = prevTitle || '未命名课程';
    state.session.updated_at = Date.now() / 1000;
    state.session.duration_seconds = state.elapsedSeconds;
    archiveSessionLocally(state.session);
    uploadSession(state.session);
  } else {
    // Current course is already blank: focus title instead of creating duplicate empty courses
    el.sessionTitleInput?.focus();
    el.sessionTitleInput?.select();
    showToast('当前已是空白课程，可直接上传 PDF 或开始收音');
    return;
  }

  const fresh = makeFreshBlankSession();
  applySessionToUI(fresh);
  try {
    localStorage.setItem('lecture_scribe_current_session', JSON.stringify(state.session));
  } catch (e) {}
  showToast(`已保存「${prevTitle}」，并新建空白课程`);
  fetchHistorySessions();
}

async function switchToCourse(sessionId) {
  if (!sessionId || sessionId === state.session.id) return;
  if (state.isRecording) {
    toggleRecording(false);
  }

  // Always archive current course before leaving it
  if (hasSessionContent(state.session)) {
    state.session.title = el.sessionTitleInput.value.trim() || '未命名课程';
    state.session.updated_at = Date.now() / 1000;
    state.session.duration_seconds = state.elapsedSeconds;
    archiveSessionLocally(state.session);
    uploadSession(state.session);
  }

  // Prefer instant local archive, fall back to cloud fetch
  const localMap = loadLocalCoursesMap();
  let targetData = localMap[sessionId] ? JSON.parse(JSON.stringify(localMap[sessionId])) : null;

  if (!targetData) {
    try {
      const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
        headers: apiHeaders()
      });
      if (res.ok) {
        targetData = await res.json();
        archiveSessionLocally(targetData);
      }
    } catch (e) {}
  }

  if (!targetData) {
    showToast('未能加载该课程记录，请稍后重试');
    return;
  }

  applySessionToUI(targetData);
  try {
    localStorage.setItem('lecture_scribe_current_session', JSON.stringify(state.session));
  } catch (e) {}
  showToast(`已切换至课程：${targetData.title}`);
}

async function deleteCourse(sessionId) {
  if (!sessionId) return;
  if (!confirm('确定永久删除该课程记录吗？删除后无法恢复。')) return;

  removeLocalCourse(sessionId);
  cloudCoursesCache = (cloudCoursesCache || []).filter(c => c.id !== sessionId);
  fetch(`/api/sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
    headers: apiHeaders()
  }).catch(() => {});

  if (sessionId === state.session.id) {
    const remaining = Object.values(loadLocalCoursesMap()).sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    if (remaining.length > 0) {
      applySessionToUI(JSON.parse(JSON.stringify(remaining[0])));
    } else {
      applySessionToUI(makeFreshBlankSession());
    }
    try {
      localStorage.setItem('lecture_scribe_current_session', JSON.stringify(state.session));
    } catch (e) {}
  } else {
    renderCourseSidebarList();
  }
  showToast('已删除课程记录');
}

async function exportFile(type) {
  if (!isPaidUser()) {
    openAccountModal('导出 Word / Markdown / 双语 SRT 字幕为 Pro（¥4.9/月）与 Ultra（¥8.9/月）会员功能，激活后即可无限导出。');
    return;
  }
  if (state.session.items.length === 0) {
    showToast('暂无内容可导出！');
    return;
  }
  persistSession();
  const endpoint = `/api/export/${type}`;
  showToast(`正在生成 ${type.toUpperCase()} 文件...`);

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(state.session)
    });

    if (res.ok) {
      const blob = await res.blob();
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      const safeTitle = (state.session.title || 'lecture').replace(/[^a-zA-Z0-9_\u4e00-\u9fa5]/g, '_');
      a.download = `${safeTitle}.${type}`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      window.URL.revokeObjectURL(url);
      showToast(`已成功导出 ${a.download}`);
    } else {
      showToast('导出失败，请重试！');
    }
  } catch (e) {
    console.error('Export error:', e);
    showToast('导出出错: ' + e.message);
  }
}

function showToast(msg) {
  let toast = document.querySelector('.toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.innerHTML = `<i data-lucide="info"></i> <span>${escapeHtml(msg)}</span>`;
  lucide.createIcons();
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 3000);
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function setupEventListeners() {
  el.btnToggleRecord.addEventListener('click', () => toggleRecording());

  el.btnForceFlush.addEventListener('click', () => {
    if (state.thoughtBuffer.length > 0 || state.activeInterimText) {
      flushThoughtBuffer();
    }
  });

  // Collapsible Course Drawer (点一下展开，再点一下收起)
  el.btnToggleCourseDrawer?.addEventListener('click', () => setCourseDrawerOpen(!courseDrawerOpen));
  el.btnCloseCourseDrawer?.addEventListener('click', () => setCourseDrawerOpen(false));
  el.courseSidebarList?.addEventListener('click', (e) => {
    const delBtn = e.target.closest('[data-action="delete-course"]');
    if (delBtn) {
      e.stopPropagation();
      deleteCourse(delBtn.dataset.sessionId);
      return;
    }
    const item = e.target.closest('.course-item[data-session-id]');
    if (item) {
      switchToCourse(item.dataset.sessionId);
    }
  });

  el.searchInput.addEventListener('input', (e) => {
    state.searchQuery = e.target.value.trim();
    renderCards();
  });

  if (el.btnSubtitles) el.btnSubtitles.addEventListener('click', toggleSubtitles);
  el.btnGenerateSummary.addEventListener('click', generateSummary);
  el.btnRefreshSummary.addEventListener('click', generateSummary);

  el.btnClearNotes?.addEventListener('click', createNewCourse);

  // Card click actions
  el.cardsList.addEventListener('click', async (e) => {
    // Jump to linked Slide page
    const slideBtn = e.target.closest('[data-action="jump-slide"]');
    if (slideBtn) {
      const pg = parseInt(slideBtn.dataset.page, 10);
      if (pg >= 1 && pg <= slideState.totalPages) {
        if (slideState.slidesPaneHidden) setSlidesPaneHidden(false);
        goToSlidePage(pg, { fromAuto: false });
      }
      return;
    }

    // Star toggle
    const starBtn = e.target.closest('[data-action="star"]');
    if (starBtn) {
      const cardId = starBtn.dataset.id;
      const card = state.session.items.find(it => it.id === cardId);
      if (card) {
        card.starred = !card.starred;
        renderCards();
        updateStats();
        persistSession();
      }
      return;
    }

    // 问问助教（免手动打字，一键直接唤起大模型深度答疑精讲）
    const askBtn = e.target.closest('[data-action="ask-ai-direct"]');
    if (askBtn) {
      const cardId = askBtn.dataset.id;
      const card = state.session.items.find(it => it.id === cardId);
      if (!card) return;

      card.qa_open = !card.qa_open;
      if (card.qa_open) {
        state.activeQACardId = cardId;
      } else if (state.activeQACardId === cardId) {
        state.activeQACardId = null;
      }

      // 若展开且尚未生成解答，立即后台请求大模型深度解析
      if (card.qa_open && !card.qa_answer && !card.qa_loading) {
        card.qa_loading = true;
        renderCards();
        anchorActiveCard();

        fetch('/api/ask-card', {
          method: 'POST',
          headers: apiHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({
            card_source: card.cleaned_source || card.source_text,
            card_translation: card.translation,
            question: '',
            provider: state.config.provider,
            api_key: state.config.apiKey,
            custom_endpoint: state.config.customEndpoint,
            model_name: state.config.modelName
          })
        })
        .then(res => res.json())
        .then(data => {
          card.qa_loading = false;
          card.qa_answer = data.answer;
          persistSession();
          renderCards();
          anchorActiveCard();
          refreshAuthUser();
        })
        .catch(err => {
          card.qa_loading = false;
          card.qa_answer = '助教解析生成失败，请检查网络或 API Key！';
          renderCards();
          anchorActiveCard();
        });
      } else {
        renderCards();
        if (card.qa_open) {
          anchorActiveCard();
        }
      }
      return;
    }
  });

  // Export Dropdown (确保置于最高层级，防止意外滚动错位)
  el.btnExportMenu.addEventListener('click', (e) => {
    e.stopPropagation();
    if (window.scrollY > 0) {
      window.scrollTo(0, 0);
    }
    el.exportDropdown.parentElement.classList.toggle('open');
  });

  document.addEventListener('click', () => {
    el.exportDropdown.parentElement.classList.remove('open');
  });

  el.exportDocx.addEventListener('click', () => exportFile('docx'));
  el.exportMd.addEventListener('click', () => exportFile('markdown'));
  el.exportSrt.addEventListener('click', () => exportFile('srt'));

  // Warning Banner click
  el.btnFixApiKey.addEventListener('click', () => el.settingsModal.classList.add('open'));

  // Settings Modal
  el.btnSettings.addEventListener('click', () => el.settingsModal.classList.add('open'));
  el.btnCloseSettings.addEventListener('click', () => el.settingsModal.classList.remove('open'));
  el.btnCancelSettings.addEventListener('click', () => el.settingsModal.classList.remove('open'));
  el.btnSaveSettings.addEventListener('click', () => {
    saveConfig();
    el.settingsModal.classList.remove('open');
  });

  el.cfgProvider.addEventListener('change', updateProviderVisibility);
  el.cfgPauseThreshold.addEventListener('input', (e) => {
    el.valPauseThreshold.textContent = e.target.value + ' 秒';
  });

  el.sessionTitleInput.addEventListener('change', () => {
    persistSession(true);
    renderCourseSidebarList();
  });
}

// ============================================================================
// Slide PDF Viewer, Pre-Class Overview, Voice Auto-Follow & Per-Page Summary
// ============================================================================

function switchRightTab(tabName) {
  const tabs = [
    { name: 'live', btn: el.tabBtnLive, panel: el.tabPanelLive },
    { name: 'slides', btn: el.tabBtnSlides, panel: el.tabPanelSlides },
    { name: 'glossary', btn: el.tabBtnGlossary, panel: el.tabPanelGlossary }
  ];
  tabs.forEach(t => {
    const active = t.name === tabName;
    t.btn?.classList.toggle('active', active);
    t.btn?.setAttribute('aria-selected', active ? 'true' : 'false');
    t.panel?.classList.toggle('active', active);
  });
  lucide.createIcons();
}

function updateTabBadges() {
  if (el.tabLiveCount) el.tabLiveCount.textContent = String((state.session.items || []).length);
  const sumCount = Object.keys(state.session.slide_summaries || {}).length;
  if (el.tabSlidesCount) el.tabSlidesCount.textContent = String(sumCount);
  if (el.timelineSummaryMeta) el.timelineSummaryMeta.textContent = `${sumCount} 页已总结`;
}

function setSlidesPaneHidden(hidden) {
  slideState.slidesPaneHidden = hidden;
  el.mainLayout?.classList.toggle('slides-hidden', hidden);
  const label = hidden ? '展开课件' : '收起课件';
  if (el.btnToggleSlidePane) {
    el.btnToggleSlidePane.title = label;
    el.btnToggleSlidePane.setAttribute('aria-label', label);
    el.btnToggleSlidePane.setAttribute('aria-expanded', String(!hidden));
  }
  const chev = document.getElementById('toggleSlidePaneChevron');
  if (chev) chev.textContent = hidden ? '›' : '‹';
}

function resetSlideViewer() {
  slideState.pdfDoc = null;
  slideState.fileName = '';
  slideState.totalPages = 0;
  slideState.currentPage = 1;
  slideState.pagesData = [];
  slideState.candidatePage = null;
  slideState.candidateHits = 0;
  slideState.filterCurrentSlideOnly = false;
  slideState.summarizingPages.clear();
  if (el.slideFileName) el.slideFileName.textContent = '未上传 PDF 课件';
  if (el.slidePageIndicator) el.slidePageIndicator.textContent = '0 / 0';
  if (el.uploadSlideBtnText) el.uploadSlideBtnText.textContent = '上传 PDF 课件';
  if (el.slideDropzone) el.slideDropzone.hidden = false;
  if (el.slideCanvasWrap) el.slideCanvasWrap.hidden = true;
  if (el.slideStrip) { el.slideStrip.hidden = true; el.slideStrip.innerHTML = ''; }
  updateSlideToolbarControls();
}

function updateSlideToolbarControls() {
  const loaded = slideState.totalPages > 0;
  if (el.btnAutoFollow) {
    el.btnAutoFollow.disabled = !loaded;
    el.btnAutoFollow.classList.toggle('is-on', loaded && slideState.autoFollow);
  }
  if (el.autoFollowText) {
    el.autoFollowText.textContent = slideState.autoFollow ? '自动跟页' : '已锁页';
  }
  if (el.btnSlidePrev) el.btnSlidePrev.disabled = !loaded || slideState.currentPage <= 1;
  if (el.btnSlideNext) el.btnSlideNext.disabled = !loaded || slideState.currentPage >= slideState.totalPages;
  if (el.btnZoomOut) el.btnZoomOut.disabled = !loaded;
  if (el.btnZoomFit) el.btnZoomFit.disabled = !loaded;
  if (el.btnZoomIn) el.btnZoomIn.disabled = !loaded;
  if (el.btnSummarizeCurrentSlide) el.btnSummarizeCurrentSlide.disabled = !loaded;
  if (el.btnRefreshSlideOverview) el.btnRefreshSlideOverview.disabled = !loaded || slideState.overviewLoading;
  if (el.btnFilterCurrentSlide) {
    el.btnFilterCurrentSlide.disabled = !loaded;
    el.btnFilterCurrentSlide.textContent = loaded ? `仅看当前页 P${slideState.currentPage}` : '仅看当前页';
  }
}

function tokenizeSlideText(text) {
  const tokens = new Set();
  const upperAcronyms = new Set();
  if (!text) return { tokens, upperAcronyms };

  const enWords = text.match(/[A-Za-z][A-Za-z0-9\-]{1,}/g) || [];
  enWords.forEach(w => {
    if (w.length >= 2 && w.length <= 6 && w === w.toUpperCase() && !ENGLISH_STOP_WORDS.has(w.toLowerCase())) {
      upperAcronyms.add(w.toLowerCase());
      tokens.add(w.toLowerCase());
    } else {
      const lw = w.toLowerCase();
      if (lw.length >= 3 && !ENGLISH_STOP_WORDS.has(lw) && !NON_ACADEMIC_TERMS.has(lw)) {
        tokens.add(lw);
      }
    }
  });

  // Chinese 2-char bigrams for Chinese or bilingual slides
  const zhChunks = text.match(/[\u4e00-\u9fa5]{2,}/g) || [];
  zhChunks.forEach(chunk => {
    for (let i = 0; i < chunk.length - 1; i++) {
      tokens.add(chunk.slice(i, i + 2));
    }
  });
  return { tokens, upperAcronyms };
}

async function handlePdfFileUpload(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') {
    showToast('当前仅支持 PDF 课件（PPTX 可在 PowerPoint/WPS 中另存为 PDF）');
    return;
  }
  if (!window.pdfjsLib) {
    showToast('PDF 渲染引擎尚未加载完成，请稍后重试或刷新页面');
    return;
  }

  if (slideState.slidesPaneHidden) setSlidesPaneHidden(false);
  showToast(`正在解析课件：${file.name}...`);

  try {
    const buffer = await file.arrayBuffer();
    const loadingTask = window.pdfjsLib.getDocument({ data: buffer });
    const pdfDoc = await loadingTask.promise;

    slideState.pdfDoc = pdfDoc;
    slideState.fileName = file.name;
    slideState.totalPages = pdfDoc.numPages;
    slideState.currentPage = 1;
    slideState.zoom = 1.0;
    slideState.fitMode = true;
    slideState.autoFollow = isPaidUser();
    slideState.candidatePage = null;
    slideState.candidateHits = 0;
    slideState.pagesData = [];

    if (el.slideFileName) el.slideFileName.textContent = file.name;
    if (el.uploadSlideBtnText) el.uploadSlideBtnText.textContent = '更换 PDF';
    if (el.slideDropzone) el.slideDropzone.hidden = true;
    if (el.slideCanvasWrap) el.slideCanvasWrap.hidden = false;
    if (el.slideStrip) el.slideStrip.hidden = false;

    // Auto-fill course title from filename if still using default title
    const cleanTitle = file.name.replace(/\.pdf$/i, '').trim();
    const curTitleVal = el.sessionTitleInput.value.trim();
    if (cleanTitle && (!curTitleVal || curTitleVal.includes('计算机体系结构 Lecture 01') || curTitleVal.startsWith('新课程 '))) {
      el.sessionTitleInput.value = cleanTitle;
      state.session.title = cleanTitle;
    }

    // Render page 1 immediately so the user sees the slide right away
    renderSlideStrip();
    await goToSlidePage(1, { fromAuto: false, skipSummaryCheck: true });

    // Extract text from all pages locally in browser for fast auto-matching & overview
    const pagesData = [];
    for (let p = 1; p <= pdfDoc.numPages; p++) {
      const pageObj = await pdfDoc.getPage(p);
      const tc = await pageObj.getTextContent();
      const rawItems = (tc.items || []).map(it => (it.str || '').trim()).filter(Boolean);
      const fullText = rawItems.join(' ');
      const title = (rawItems[0] || '').slice(0, 72);
      const { tokens, upperAcronyms } = tokenizeSlideText(fullText);
      pagesData.push({ page: p, title, text: fullText, tokens, upperAcronyms });
    }
    slideState.pagesData = pagesData;

    state.session.slide_meta = { file_name: file.name, total_pages: pdfDoc.numPages };
    if (!state.session.slide_summaries) state.session.slide_summaries = {};
    persistSession(true);

    showToast(`课件已加载（共 ${pdfDoc.numPages} 页），正在生成课前总览...`);
    generateSlideOverview();
  } catch (err) {
    console.error('[Slide] PDF parse error:', err);
    showToast('PDF 解析失败，请确认文件未加密且格式正常');
  }
}

function renderSlideStrip() {
  if (!el.slideStrip) return;
  if (slideState.totalPages <= 0) {
    el.slideStrip.hidden = true;
    el.slideStrip.innerHTML = '';
    return;
  }
  const summaries = state.session.slide_summaries || {};
  let html = '';
  for (let p = 1; p <= slideState.totalPages; p++) {
    const isActive = p === slideState.currentPage ? 'active' : '';
    const hasSum = summaries[String(p)] ? 'has-summary' : '';
    html += `<button type="button" class="slide-chip ${isActive} ${hasSum}" data-slide-jump="${p}">P${p}</button>`;
  }
  el.slideStrip.innerHTML = html;
  const activeChip = el.slideStrip.querySelector('.slide-chip.active');
  if (activeChip) activeChip.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
}

async function renderPdfPage(pageNum) {
  if (!slideState.pdfDoc || !el.slideCanvas || !el.slideCanvasStage) return;
  try {
    if (slideState.renderTask) {
      try { slideState.renderTask.cancel(); } catch (e) {}
      slideState.renderTask = null;
    }
    const page = await slideState.pdfDoc.getPage(pageNum);
    const baseViewport = page.getViewport({ scale: 1.0 });

    const stageRect = el.slideCanvasStage.getBoundingClientRect();
    const availW = Math.max(260, stageRect.width - 28);
    const availH = Math.max(180, stageRect.height - 28);
    const fitScale = Math.min(availW / baseViewport.width, availH / baseViewport.height);
    const cssScale = slideState.fitMode ? fitScale : fitScale * slideState.zoom;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    const viewport = page.getViewport({ scale: cssScale * dpr });
    const canvas = el.slideCanvas;
    const ctx = canvas.getContext('2d');
    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);
    canvas.style.width = `${Math.floor(viewport.width / dpr)}px`;
    canvas.style.height = `${Math.floor(viewport.height / dpr)}px`;

    const renderTask = page.render({ canvasContext: ctx, viewport });
    slideState.renderTask = renderTask;
    await renderTask.promise;
    slideState.renderTask = null;
  } catch (err) {
    if (err && err.name !== 'RenderingCancelledException') {
      console.error('[Slide] Render page error:', err);
    }
  }
}

async function goToSlidePage(pageNum, opts = {}) {
  if (slideState.totalPages <= 0) return;
  const target = Math.max(1, Math.min(slideState.totalPages, pageNum));
  const prevPage = slideState.currentPage;

  slideState.currentPage = target;
  if (el.slidePageIndicator) {
    el.slidePageIndicator.textContent = `第 ${target} / ${slideState.totalPages} 页`;
  }
  updateSlideToolbarControls();
  renderSlideStrip();
  await renderPdfPage(target);
  renderCurrentSlideSummaryUI();
  renderSlideTimelineUI();
  if (slideState.filterCurrentSlideOnly) renderCards();

  // When leaving a page that accumulated professor speech, auto-generate its synthesis!
  if (!opts.skipSummaryCheck && prevPage !== target && prevPage >= 1) {
    const prevSpoken = getSpokenItemsForPage(prevPage);
    const existing = (state.session.slide_summaries || {})[String(prevPage)];
    const minNeeded = opts.fromAuto ? 1 : 2;
    if (prevSpoken.length >= minNeeded && (!existing || (existing.spoken_count || 0) < prevSpoken.length)) {
      summarizeSlidePage(prevPage, { silent: true });
    }
  }
}

function getSpokenItemsForPage(pageNum) {
  return (state.session.items || [])
    .filter(it => it.slide_page === pageNum)
    .sort((a, b) => getCardTimestamp(a) - getCardTimestamp(b));
}

// Local real-time voice -> slide matcher (fast, 0-cost, sequential lecture bias)
function evaluateSlideAutoFollow(spokenText, newCard) {
  if (!slideState.autoFollow || slideState.pagesData.length < 2) return;
  const { tokens, upperAcronyms } = tokenizeSlideText(spokenText);
  if (tokens.size < 2) return;

  const cur = slideState.currentPage;
  let bestPage = cur;
  let bestScore = 0;
  let curRawScore = 0;

  for (const p of slideState.pagesData) {
    let raw = 0;
    tokens.forEach(tk => {
      if (p.tokens.has(tk)) {
        raw += p.upperAcronyms.has(tk) ? 2.2 : (tk.length >= 6 ? 1.4 : 1.0);
      }
    });
    if (p.page === cur) curRawScore = raw;

    // Sequential lecture bias: professors mostly stay on current or advance +1 / +2
    const delta = p.page - cur;
    let adjusted = raw;
    if (delta === 0) adjusted += 0.6;
    else if (delta === 1) adjusted += 0.55;
    else if (delta === 2) adjusted += 0.25;
    else if (delta > 2 && delta <= 5) adjusted -= 0.6;
    else if (delta < 0 || delta > 5) adjusted -= 1.5;

    if (adjusted > bestScore) {
      bestScore = adjusted;
      bestPage = p.page;
    }
  }

  // Require clear signal over current page so random chatter doesn't cause jitter
  if (bestPage === cur || bestScore < 2.2 || bestScore < curRawScore + 0.8) {
    slideState.candidatePage = null;
    slideState.candidateHits = 0;
    return;
  }

  const delta = bestPage - cur;
  const immediateForward = (delta === 1 || delta === 2) && bestScore >= 2.8;
  const strongJump = bestScore >= 4.8;

  if (immediateForward || strongJump) {
    if (newCard) newCard.slide_page = bestPage;
    slideState.candidatePage = null;
    slideState.candidateHits = 0;
    goToSlidePage(bestPage, { fromAuto: true });
    showToast(`已自动跟页至 Slide P${bestPage}`);
  } else {
    if (slideState.candidatePage === bestPage) {
      slideState.candidateHits += 1;
    } else {
      slideState.candidatePage = bestPage;
      slideState.candidateHits = 1;
    }
    if (slideState.candidateHits >= 2) {
      if (newCard) newCard.slide_page = bestPage;
      slideState.candidatePage = null;
      slideState.candidateHits = 0;
      goToSlidePage(bestPage, { fromAuto: true });
      showToast(`已自动跟页至 Slide P${bestPage}`);
    }
  }
}

async function generateSlideOverview() {
  if (slideState.pagesData.length === 0 || slideState.overviewLoading) return;
  slideState.overviewLoading = true;
  updateSlideToolbarControls();
  if (el.slideOverviewBody) {
    el.slideOverviewBody.innerHTML = `<p class="placeholder-text">正在梳理共 ${slideState.totalPages} 页课件的教学目标与内容脉络...</p>`;
  }

  try {
    const res = await fetch('/api/slide-overview', {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        file_name: slideState.fileName,
        total_pages: slideState.totalPages,
        pages: slideState.pagesData.map(p => ({ page: p.page, title: p.title, text: p.text.slice(0, 450) })),
        provider: state.config.provider,
        api_key: state.config.apiKey,
        custom_endpoint: state.config.customEndpoint,
        model_name: state.config.modelName
      })
    });
    if (res.ok) {
      const overview = await res.json();
      state.session.slide_overview = overview;
      persistSession();
      renderSlideOverviewUI();
      refreshAuthUser();
      showToast('本节课件总览已生成（可在右侧「总览与逐页」查看）');
    } else {
      throw new Error(`HTTP ${res.status}`);
    }
  } catch (err) {
    console.error('[SlideOverview] error:', err);
    if (el.slideOverviewBody) {
      el.slideOverviewBody.innerHTML = `<p class="placeholder-text">课件总览生成失败，请点击右上角「重新生成」重试。</p>`;
    }
  } finally {
    slideState.overviewLoading = false;
    updateSlideToolbarControls();
  }
}

function renderSlideOverviewUI() {
  if (!el.slideOverviewBody) return;
  const ov = state.session.slide_overview;
  if (!ov) {
    el.slideOverviewBody.innerHTML = `<p class="placeholder-text">上传教授的 PDF 课件后，自动整理本节课的「教学目标」（免费预览）以及「主要内容脉络」与「核心术语预告」（Pro / Ultra 解锁）。</p>`;
    return;
  }

  const objsHtml = (ov.objectives || []).map(o => `<li>${escapeHtml(o)}</li>`).join('');
  const outlineHtml = (ov.outline || []).map(sec => {
    const m = String(sec.pages || '').match(/\d+/);
    const startPage = m ? parseInt(m[0], 10) : 1;
    return `
      <button type="button" class="ov-outline-row" data-jump-page="${startPage}">
        <span class="ov-outline-pages">${escapeHtml(sec.pages || '')}</span>
        <span class="ov-outline-text">
          <b>${escapeHtml(sec.topic || '')}</b>
          <span>${escapeHtml(sec.brief || '')}</span>
        </span>
      </button>
    `;
  }).join('');
  const termsHtml = (ov.key_terms || []).map(t => `
    <span class="ov-term-chip" title="${escapeHtml(t.note || '')}">
      <strong>${escapeHtml(t.term || '')}</strong>${t.zh ? ` · ${escapeHtml(t.zh)}` : ''}
    </span>
  `).join('');

  const sourceHint = ov.source === 'ultra_llm'
    ? 'Ultra 官方高速模型梳理'
    : (ov.source === 'llm' ? '个人 Key AI 深度梳理' : '本地免费规则提炼（升级 Pro / Ultra 解锁完整 AI 梳理）');

  const gatedPartHtml = `
    ${outlineHtml ? `<div class="ov-block"><div class="ov-label">主要内容脉络（点击跳转到对应页）</div><div class="ov-outline-list">${outlineHtml}</div></div>` : ''}
    ${termsHtml ? `<div class="ov-block"><div class="ov-label">核心术语预告</div><div class="ov-terms-wrap">${termsHtml}</div></div>` : ''}
  `;

  if (isPaidUser()) {
    el.slideOverviewBody.innerHTML = `
      <div class="ov-topic">${escapeHtml(ov.course_topic || '本节课件总览')}</div>
      ${objsHtml ? `<div class="ov-block"><div class="ov-label">教学目标</div><ul class="ov-list">${objsHtml}</ul></div>` : ''}
      ${gatedPartHtml}
      <div class="sum-source-note">${sourceHint}</div>
    `;
  } else {
    el.slideOverviewBody.innerHTML = `
      <div class="ov-topic">${escapeHtml(ov.course_topic || '本节课件总览')}</div>
      ${objsHtml ? `<div class="ov-block"><div class="ov-label">教学目标（免费预览）</div><ul class="ov-list">${objsHtml}</ul></div>` : ''}
      ${gatedPartHtml ? `
        <div class="pro-gate-wrap">
          <div class="pro-gate-blur">${gatedPartHtml}</div>
          <div class="pro-gate-overlay">
            <div class="pro-gate-card">
              <span class="pro-gate-tag">Pro ¥4.9 · Ultra ¥8.9</span>
              <span class="pro-gate-text">升级解锁完整页码脉络跳转与核心术语预告</span>
              <button type="button" class="pro-gate-btn" data-action="open-upgrade">立即解锁</button>
            </div>
          </div>
        </div>
      ` : ''}
    `;
  }
}

async function summarizeSlidePage(pageNum, opts = {}) {
  if (slideState.totalPages <= 0 || pageNum < 1 || pageNum > slideState.totalPages) return;
  if (slideState.summarizingPages.has(pageNum)) return;
  slideState.summarizingPages.add(pageNum);

  if (pageNum === slideState.currentPage) {
    renderCurrentSlideSummaryUI();
  }

  const pData = slideState.pagesData.find(p => p.page === pageNum) || { title: '', text: '' };
  const spoken = getSpokenItemsForPage(pageNum).map(it => ({
    time_str: it.time_str || '',
    source: it.cleaned_source || it.source_text || '',
    translation: it.translation || ''
  }));

  try {
    const res = await fetch('/api/slide-page-summary', {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        page: pageNum,
        total_pages: slideState.totalPages,
        slide_title: pData.title || '',
        slide_text: pData.text || '',
        spoken_items: spoken,
        course_title: el.sessionTitleInput.value.trim(),
        provider: state.config.provider,
        api_key: state.config.apiKey,
        custom_endpoint: state.config.customEndpoint,
        model_name: state.config.modelName
      })
    });
    if (res.ok) {
      const sum = await res.json();
      if (!state.session.slide_summaries) state.session.slide_summaries = {};
      state.session.slide_summaries[String(pageNum)] = sum;
      persistSession();
      renderSlideStrip();
      renderCurrentSlideSummaryUI();
      renderSlideTimelineUI();
      updateTabBadges();
      refreshAuthUser();
      if (!opts.silent) showToast(`第 ${pageNum} 页综合总结已更新`);
    }
  } catch (err) {
    console.error('[SlidePageSummary] error:', err);
    if (!opts.silent) showToast(`第 ${pageNum} 页总结失败，请重试`);
  } finally {
    slideState.summarizingPages.delete(pageNum);
    if (pageNum === slideState.currentPage) {
      renderCurrentSlideSummaryUI();
    }
  }
}

function renderCurrentSlideSummaryUI() {
  if (!el.currentSlideSummaryBody) return;
  if (slideState.totalPages <= 0) {
    const savedPages = Object.keys(state.session.slide_summaries || {})
      .map(k => parseInt(k, 10))
      .filter(n => !Number.isNaN(n))
      .sort((a, b) => a - b);
    if (savedPages.length > 0) {
      const latestPg = savedPages[savedPages.length - 1];
      const sum = state.session.slide_summaries[String(latestPg)];
      const addsHtml = (sum.professor_additions || []).map(a => `<li>${escapeHtml(a)}</li>`).join('');
      const kwHtml = (sum.keywords || []).map(k => `<span class="keyword-pill">${escapeHtml(k)}</span>`).join('');
      if (el.currentSlidePagePill) el.currentSlidePagePill.textContent = `P${latestPg}`;
      if (el.currentSlideSumTitle) el.currentSlideSumTitle.textContent = `已保存第 ${latestPg} 页总结（共 ${savedPages.length} 页）`;
      if (el.currentSlideSpokenCount) el.currentSlideSpokenCount.textContent = '重传原 PDF 可同步翻页';
      const innerHtml = `
        <div class="sum-headline">${escapeHtml(sum.headline || '')}</div>
        ${addsHtml ? `<ul class="sum-additions">${addsHtml}</ul>` : ''}
        <div class="sum-meta-row">${kwHtml}<span class="sum-source-note">右侧「总览与逐页」可查看全部 ${savedPages.length} 页</span></div>
      `;
      if (isPaidUser() || latestPg === 1) {
        el.currentSlideSummaryBody.innerHTML = innerHtml;
      } else {
        el.currentSlideSummaryBody.innerHTML = `
          <div class="pro-gate-wrap">
            <div class="pro-gate-blur">${innerHtml}</div>
            <div class="pro-gate-overlay">
              <div class="pro-gate-inline">
                <div class="pro-gate-inline-copy">
                  <strong>第 ${latestPg} 页总结已就绪 · Pro / Ultra 解锁</strong>
                  <span>第 1 页免费预览，升级查看全课件逐页结论</span>
                </div>
                <button type="button" class="pro-gate-btn" data-action="open-upgrade">立即解锁</button>
              </div>
            </div>
          </div>
        `;
      }
      return;
    }
    if (el.currentSlidePagePill) el.currentSlidePagePill.textContent = 'P-';
    if (el.currentSlideSumTitle) el.currentSlideSumTitle.textContent = '当前页 · Slide + 教授口述综合总结';
    if (el.currentSlideSpokenCount) el.currentSlideSpokenCount.textContent = '等待上传课件';
    el.currentSlideSummaryBody.innerHTML = `<div class="sum-placeholder">上传 PDF 课件并开始收音后，每讲完一页自动在这里沉淀「该页 Slide + 教授口述补充」的简短结论；第 1 页免费预览，第 2 页起升级 Pro / Ultra 完整解锁。</div>`;
    return;
  }

  const pg = slideState.currentPage;
  const spoken = getSpokenItemsForPage(pg);
  if (el.currentSlidePagePill) el.currentSlidePagePill.textContent = `P${pg}`;
  if (el.currentSlideSumTitle) el.currentSlideSumTitle.textContent = `第 ${pg} 页 · Slide + 教授口述综合总结`;
  if (el.currentSlideSpokenCount) el.currentSlideSpokenCount.textContent = `已收录 ${spoken.length} 段口述`;

  if (slideState.summarizingPages.has(pg)) {
    el.currentSlideSummaryBody.innerHTML = `<div class="sum-placeholder">正在融合第 ${pg} 页 Slide 原文与教授口述内容，生成简短综合总结...</div>`;
    return;
  }

  const sum = (state.session.slide_summaries || {})[String(pg)];
  if (!sum) {
    el.currentSlideSummaryBody.innerHTML = `<div class="sum-placeholder">当前停留在第 ${pg} 页（已收录 ${spoken.length} 段口述）。教授翻页后将自动总结本页；也可以点右上角 <strong>「总结本页」</strong> 立即生成${pg === 1 && !isPaidUser() ? '（第 1 页免费预览）' : ''}。</div>`;
    return;
  }

  const addsHtml = (sum.professor_additions || []).map(a => `<li>${escapeHtml(a)}</li>`).join('');
  const kwHtml = (sum.keywords || []).map(k => `<span class="keyword-pill">${escapeHtml(k)}</span>`).join('');
  const srcNote = sum.source === 'ultra_llm'
    ? 'Ultra 官方高速模型'
    : (sum.source === 'llm' ? '个人 Key AI 融合' : (pg === 1 && !isPaidUser() ? '第 1 页免费预览' : '本地规则摘要'));

  const innerHtml = `
    <div class="sum-headline">${escapeHtml(sum.headline || '')}</div>
    ${addsHtml ? `<ul class="sum-additions">${addsHtml}</ul>` : ''}
    <div class="sum-meta-row">
      ${kwHtml}
      <span class="sum-source-note">${srcNote}</span>
    </div>
  `;

  // Free Preview Policy: Page 1 is always clearly visible; Page 2+ gets frosted glass for Free users
  if (isPaidUser() || pg === 1) {
    el.currentSlideSummaryBody.innerHTML = innerHtml;
  } else {
    el.currentSlideSummaryBody.innerHTML = `
      <div class="pro-gate-wrap">
        <div class="pro-gate-blur">${innerHtml}</div>
        <div class="pro-gate-overlay">
          <div class="pro-gate-inline">
            <div class="pro-gate-inline-copy">
              <strong>第 ${pg} 页综合总结已生成 · Pro / Ultra 解锁</strong>
              <span>免费版可完整预览第 1 页，Pro ¥4.9 / Ultra ¥8.9 解锁全课件</span>
            </div>
            <button type="button" class="pro-gate-btn" data-action="open-upgrade">立即解锁</button>
          </div>
        </div>
      </div>
    `;
  }
}

function renderSlideTimelineUI() {
  if (!el.slideTimelineList) return;
  const map = state.session.slide_summaries || {};
  const pages = Object.keys(map).map(k => parseInt(k, 10)).filter(n => !Number.isNaN(n)).sort((a, b) => a - b);
  updateTabBadges();

  if (pages.length === 0) {
    el.slideTimelineList.innerHTML = `<p class="placeholder-text">每讲完一张 Slide（翻页自动触发或手动点「总结本页」），综合结论会按页码顺序沉淀在这里。</p>`;
    return;
  }

  const renderCard = (pg) => {
    const s = map[String(pg)];
    const active = pg === slideState.currentPage ? 'active' : '';
    const adds = (s.professor_additions || []).slice(0, 2).map(a => `<li>• ${escapeHtml(a)}</li>`).join('');
    return `
      <div class="timeline-card ${active}" data-jump-page="${pg}">
        <div class="timeline-card-head">
          <span class="timeline-page-tag">Slide P${pg}${pg === 1 && !isPaidUser() ? ' · 免费预览' : ''}</span>
          <span class="timeline-spoken-meta">${s.spoken_count || 0} 段口述</span>
        </div>
        <div class="timeline-headline">${escapeHtml(s.headline || '')}</div>
        ${adds ? `<ul class="timeline-additions">${adds}</ul>` : ''}
      </div>
    `;
  };

  if (isPaidUser()) {
    el.slideTimelineList.innerHTML = pages.map(renderCard).join('');
    return;
  }

  const freePages = pages.filter(p => p === 1);
  const lockedPages = pages.filter(p => p > 1);
  let html = freePages.map(renderCard).join('');
  if (lockedPages.length > 0) {
    const pageRangeLabel = lockedPages.length === 1
      ? `第 P${lockedPages[0]} 页及后续`
      : `P${lockedPages[0]}–P${lockedPages[lockedPages.length - 1]}`;
    html += `
      <div class="pro-gate-wrap">
        <div class="pro-gate-blur">${lockedPages.map(renderCard).join('')}</div>
        <div class="pro-gate-overlay">
          <div class="pro-gate-card">
            <span class="pro-gate-tag">已生成 ${lockedPages.length} 页后续总结</span>
            <span class="pro-gate-text">升级 Pro（¥4.9）或 Ultra（¥8.9）解锁${pageRangeLabel}完整总结</span>
            <button type="button" class="pro-gate-btn" data-action="open-upgrade">立即解锁</button>
          </div>
        </div>
      </div>
    `;
  }
  el.slideTimelineList.innerHTML = html;
}

function setupSlideSyncListeners() {
  // Right tabs switching
  el.tabBtnLive?.addEventListener('click', () => switchRightTab('live'));
  el.tabBtnSlides?.addEventListener('click', () => switchRightTab('slides'));
  el.tabBtnGlossary?.addEventListener('click', () => switchRightTab('glossary'));

  // Filter live cards: all vs current slide
  el.btnFilterAllCards?.addEventListener('click', () => {
    slideState.filterCurrentSlideOnly = false;
    el.btnFilterAllCards.classList.add('active');
    el.btnFilterCurrentSlide?.classList.remove('active');
    renderCards();
  });
  el.btnFilterCurrentSlide?.addEventListener('click', () => {
    if (slideState.totalPages <= 0) return;
    slideState.filterCurrentSlideOnly = true;
    el.btnFilterCurrentSlide.classList.add('active');
    el.btnFilterAllCards?.classList.remove('active');
    renderCards();
  });

  // Upload triggers
  const triggerUpload = () => el.slideFileInput?.click();
  el.btnUploadSlide?.addEventListener('click', triggerUpload);
  el.btnDropzoneUpload?.addEventListener('click', (e) => { e.stopPropagation(); triggerUpload(); });
  el.slideDropzone?.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;
    triggerUpload();
  });
  el.slideFileInput?.addEventListener('change', (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) handlePdfFileUpload(file);
    e.target.value = '';
  });

  // Drag & drop PDF directly onto left stage
  if (el.slideCanvasStage) {
    el.slideCanvasStage.addEventListener('dragover', (e) => {
      e.preventDefault();
      el.slideCanvasStage.classList.add('drag-over');
    });
    el.slideCanvasStage.addEventListener('dragleave', () => {
      el.slideCanvasStage.classList.remove('drag-over');
    });
    el.slideCanvasStage.addEventListener('drop', (e) => {
      e.preventDefault();
      el.slideCanvasStage.classList.remove('drag-over');
      const file = e.dataTransfer?.files?.[0];
      if (file) handlePdfFileUpload(file);
    });
  }

  // Collapse / expand slide pane
  el.btnToggleSlidePane?.addEventListener('click', () => setSlidesPaneHidden(!slideState.slidesPaneHidden));
  el.btnHideSlidesFromEmpty?.addEventListener('click', (e) => {
    e.stopPropagation();
    setSlidesPaneHidden(true);
  });

  // Page navigation & zoom
  el.btnSlidePrev?.addEventListener('click', () => goToSlidePage(slideState.currentPage - 1, { fromAuto: false }));
  el.btnSlideNext?.addEventListener('click', () => goToSlidePage(slideState.currentPage + 1, { fromAuto: false }));
  el.btnAutoFollow?.addEventListener('click', () => {
    if (!isPaidUser()) {
      openAccountModal('「语音自动跟页」为 Pro（¥4.9/月）与 Ultra（¥8.9/月）会员功能；免费版可随时手动翻页。');
      return;
    }
    slideState.autoFollow = !slideState.autoFollow;
    updateSlideToolbarControls();
    showToast(slideState.autoFollow ? '已开启：随教授声音自动翻页' : '已锁定当前页（手动翻页模式）');
  });
  el.btnZoomIn?.addEventListener('click', () => {
    slideState.fitMode = false;
    slideState.zoom = Math.min(2.2, +(slideState.zoom + 0.15).toFixed(2));
    renderPdfPage(slideState.currentPage);
  });
  el.btnZoomOut?.addEventListener('click', () => {
    slideState.fitMode = false;
    slideState.zoom = Math.max(0.6, +(slideState.zoom - 0.15).toFixed(2));
    renderPdfPage(slideState.currentPage);
  });
  el.btnZoomFit?.addEventListener('click', () => {
    slideState.fitMode = true;
    slideState.zoom = 1.0;
    renderPdfPage(slideState.currentPage);
  });

  // Quick page strip clicks
  el.slideStrip?.addEventListener('click', (e) => {
    const chip = e.target.closest('[data-slide-jump]');
    if (!chip) return;
    const pg = parseInt(chip.dataset.slideJump, 10);
    if (pg) goToSlidePage(pg, { fromAuto: false });
  });

  // Outline & timeline jump-to-page clicks
  const onJumpClick = (e) => {
    const node = e.target.closest('[data-jump-page]');
    if (!node) return;
    const pg = parseInt(node.dataset.jumpPage, 10);
    if (pg >= 1 && pg <= slideState.totalPages) {
      if (slideState.slidesPaneHidden) setSlidesPaneHidden(false);
      goToSlidePage(pg, { fromAuto: false });
    }
  };
  el.slideOverviewBody?.addEventListener('click', onJumpClick);
  el.slideTimelineList?.addEventListener('click', onJumpClick);

  // Manual summary & overview refresh buttons
  el.btnSummarizeCurrentSlide?.addEventListener('click', () => summarizeSlidePage(slideState.currentPage, { silent: false }));
  el.btnRefreshSlideOverview?.addEventListener('click', () => generateSlideOverview());

  // Re-render canvas smoothly on window resize
  let resizeTimer = null;
  window.addEventListener('resize', () => {
    if (!slideState.pdfDoc || !slideState.fitMode) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => renderPdfPage(slideState.currentPage), 160);
  });
}

// ============================================================================
// Account Auth (Username + Password), 3-Tier Plans (Free / Pro / Ultra) & Settings Presets
// ============================================================================

function openAccountModal(hintMsg = '') {
  if (!el.accountModal) return;
  if (el.authUsername) el.authUsername.value = '';
  if (el.authPassword) el.authPassword.value = '';
  if (el.authErrorMsg) el.authErrorMsg.hidden = true;
  if (hintMsg && el.redeemMsg) {
    el.redeemMsg.textContent = hintMsg;
  }
  el.accountModal.classList.add('open');
  lucide.createIcons();
  // Always pull latest membership plan/quotas from server when opening modal
  if (state.auth.token) {
    refreshAuthUser();
  }
}

function closeAccountModal() {
  el.accountModal?.classList.remove('open');
}

function setAuthMode(mode) {
  state.auth.mode = mode === 'register' ? 'register' : 'login';
  const isReg = state.auth.mode === 'register';
  el.authTabLogin?.classList.toggle('active', !isReg);
  el.authTabRegister?.classList.toggle('active', isReg);
  if (el.btnAuthSubmit) el.btnAuthSubmit.textContent = isReg ? '立即注册并登录' : '登录账号';
  if (el.authHintText) {
    el.authHintText.textContent = isReg
      ? '无需邮箱或手机号验证，5 秒完成注册；请牢记你的密码。'
      : '登录后历史课程将同步绑定到你的账号，并可随时激活 Pro / Ultra。';
  }
  if (el.authErrorMsg) el.authErrorMsg.hidden = true;
}

function updateAccountUI() {
  const user = state.auth.user;
  const plan = getEffectivePlan();
  const planLabel = plan === 'ultra' ? 'Ultra' : (plan === 'pro' ? 'Pro' : 'Free');
  const chipClass = `plan-chip plan-chip--${plan}`;

  document.body.classList.toggle('has-paid-plan', isPaidUser());

  if (el.accountBtnLabel) {
    el.accountBtnLabel.textContent = user ? user.username : '登录 / 升级';
  }
  if (el.accountPlanChip) {
    el.accountPlanChip.textContent = planLabel;
    el.accountPlanChip.className = chipClass;
  }
  if (el.courseDrawerScopeLabel) {
    el.courseDrawerScopeLabel.textContent = user
      ? `账号「${user.username}」的课程（点击切回）`
      : '最近 7 天课程（点击随时切回）';
  }
  if (el.btnDrawerUpgrade) {
    el.btnDrawerUpgrade.textContent = plan === 'ultra'
      ? 'Ultra 会员生效中 · 查看额度'
      : (plan === 'pro' ? 'Pro 会员生效中 · 管理方案' : 'Free 免费 · Pro ¥4.9 · Ultra ¥8.9');
  }

  if (user) {
    if (el.accountSignedInBox) el.accountSignedInBox.hidden = false;
    if (el.authGuestBox) el.authGuestBox.hidden = true;
    if (el.payRemarkHint) {
      el.payRemarkHint.innerHTML = `扫码付款时请在「添加备注」填写你的用户名：<strong>${escapeHtml(user.username)}</strong>，付款后点击下方按钮即可开通。`;
    }
    if (el.acctUsernameDisplay) el.acctUsernameDisplay.textContent = user.username;
    if (el.acctPlanBadge) {
      el.acctPlanBadge.textContent = planLabel;
      el.acctPlanBadge.className = chipClass;
    }
    if (el.acctExpiryText) {
      if (plan === 'pro' || plan === 'ultra') {
        const expDate = user.plan_expires_at ? new Date(user.plan_expires_at * 1000).toLocaleDateString() : '长期';
        el.acctExpiryText.textContent = `当前为 ${planLabel} 会员（有效期至 ${expDate}），全套课件跟页、逐页总结与导出已解锁。`;
      } else {
        el.acctExpiryText.textContent = '当前为 Free 免费版；可在下方选择 Pro 或 Ultra 扫码开通，或输入激活码升级。';
      }
    }
    if (el.ultraQuotaBox) {
      el.ultraQuotaBox.hidden = (plan !== 'ultra');
      if (plan === 'ultra' && user.quotas) {
        if (el.qOverviewText) el.qOverviewText.textContent = `${user.quotas.overview_used} / ${user.quotas.overview_limit}`;
        if (el.qSlideText) el.qSlideText.textContent = `${user.quotas.slide_used} / ${user.quotas.slide_limit}`;
        if (el.qQaText) el.qQaText.textContent = `${user.quotas.qa_used} / ${user.quotas.qa_limit}`;
      }
    }
  } else {
    if (el.accountSignedInBox) el.accountSignedInBox.hidden = true;
    if (el.authGuestBox) el.authGuestBox.hidden = false;
    if (el.ultraQuotaBox) el.ultraQuotaBox.hidden = true;
    if (el.payRemarkHint) {
      el.payRemarkHint.textContent = '先在上方登录或 5 秒注册，扫码付款时在「添加备注」填用户名，付款后点击下方按钮即可开通。';
    }
  }

  // Re-render gated surfaces immediately so unlocking removes frosted glass live
  if (slideState.totalPages > 0 && isPaidUser() && !slideState.autoFollow) {
    slideState.autoFollow = true;
    updateSlideToolbarControls();
  }
  renderSlideOverviewUI();
  renderCurrentSlideSummaryUI();
  renderSlideTimelineUI();
  renderCards();
}

async function refreshAuthUser() {
  if (!state.auth.token) {
    state.auth.user = null;
    updateAccountUI();
    return;
  }
  try {
    const res = await fetch('/api/auth/me', { headers: apiHeaders() });
    if (res.ok) {
      const data = await res.json();
      state.auth.user = data.user || null;
      if (!state.auth.user) {
        state.auth.token = '';
        localStorage.removeItem(AUTH_TOKEN_KEY);
      }
      updateAccountUI();
    }
  } catch (e) {}
}

async function handleAuthSubmit(e) {
  e.preventDefault();
  const username = (el.authUsername?.value || '').trim();
  const password = el.authPassword?.value || '';
  if (el.authErrorMsg) el.authErrorMsg.hidden = true;

  const endpoint = state.auth.mode === 'register' ? '/api/auth/register' : '/api/auth/login';
  if (el.btnAuthSubmit) el.btnAuthSubmit.disabled = true;

  try {
    const res = await fetch(endpoint, {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ username, password })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (el.authErrorMsg) {
        if (res.status === 405) {
          el.authErrorMsg.textContent = '当前后台服务仍是旧版进程（未加载登录注册接口），请先重启 python server.py 后刷新再试。';
        } else {
          el.authErrorMsg.textContent = data.detail || '操作失败，请检查用户名与密码';
        }
        el.authErrorMsg.hidden = false;
      }
      return;
    }

    state.auth.token = data.token || '';
    state.auth.user = data.user || null;
    localStorage.setItem(AUTH_TOKEN_KEY, state.auth.token);
    if (el.authPassword) el.authPassword.value = '';
    updateAccountUI();
    fetchHistorySessions();
    showToast(state.auth.mode === 'register' ? `欢迎加入，${state.auth.user.username}！` : `已登录：${state.auth.user.username}`);
  } catch (err) {
    if (el.authErrorMsg) {
      el.authErrorMsg.textContent = '网络请求失败，请稍后重试';
      el.authErrorMsg.hidden = false;
    }
  } finally {
    if (el.btnAuthSubmit) el.btnAuthSubmit.disabled = false;
  }
}

async function handleRedeemCode() {
  const code = (el.redeemCodeInput?.value || '').trim();
  if (!state.auth.user) {
    if (el.authErrorMsg) {
      el.authErrorMsg.textContent = '请先在上方登录或 5 秒注册账号，再激活会员。';
      el.authErrorMsg.hidden = false;
    }
    el.authUsername?.focus();
    return;
  }
  if (!code) {
    showToast('请先输入 Pro / Ultra 激活码');
    el.redeemCodeInput?.focus();
    return;
  }
  if (el.btnRedeemCode) el.btnRedeemCode.disabled = true;
  try {
    const res = await fetch('/api/billing/redeem', {
      method: 'POST',
      headers: apiHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ code })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (el.redeemMsg) el.redeemMsg.textContent = '⚠️ ' + (data.detail || '激活码无效');
      showToast(data.detail || '激活码无效');
      return;
    }
    state.auth.user = data.user;
    if (el.redeemCodeInput) el.redeemCodeInput.value = '';
    const planName = data.plan === 'ultra' ? 'Ultra' : 'Pro';
    if (el.redeemMsg) el.redeemMsg.textContent = `✓ 已成功激活 ${planName} 会员（+${data.duration_days} 天），全部功能已解锁！`;
    updateAccountUI();
    showToast(`恭喜！已升级为 ${planName} 会员，所有页面总结与导出已解锁`);
  } catch (e) {
    showToast('激活请求失败，请检查网络');
  } finally {
    if (el.btnRedeemCode) el.btnRedeemCode.disabled = false;
  }
}

function handleLogout() {
  state.auth.token = '';
  state.auth.user = null;
  localStorage.removeItem(AUTH_TOKEN_KEY);
  updateAccountUI();
  fetchHistorySessions();
  showToast('已退出登录');
}

function setupAccountListeners() {
  el.btnAccount?.addEventListener('click', () => openAccountModal());
  el.btnDrawerUpgrade?.addEventListener('click', () => openAccountModal());

  // Automatically sync latest plan when returning to this browser tab
  let lastAuthSyncAt = 0;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || !state.auth.token) return;
    const now = Date.now();
    if (now - lastAuthSyncAt < 10000) return;
    lastAuthSyncAt = now;
    refreshAuthUser();
  });
  el.btnCloseAccount?.addEventListener('click', closeAccountModal);
  el.accountModal?.addEventListener('click', (e) => {
    if (e.target === el.accountModal) closeAccountModal();
  });

  el.authTabLogin?.addEventListener('click', () => setAuthMode('login'));
  el.authTabRegister?.addEventListener('click', () => setAuthMode('register'));
  el.authForm?.addEventListener('submit', handleAuthSubmit);
  el.btnRedeemCode?.addEventListener('click', handleRedeemCode);
  el.btnLogout?.addEventListener('click', handleLogout);

  // Payment plan selection via clickable cards + payment method switcher + claim submission
  let selectedPayPlan = 'pro';
  let selectedPayMethod = 'wechat';

  const applyPayPlanSelection = (plan) => {
    selectedPayPlan = plan === 'ultra' ? 'ultra' : 'pro';
    const amtText = selectedPayPlan === 'ultra' ? '¥8.9' : '¥4.9';
    const tagText = selectedPayPlan === 'ultra' ? 'Ultra · ¥8.9/月' : 'Pro · ¥4.9/月';
    const tagEl = document.getElementById('paySelectedTag');
    if (tagEl) tagEl.textContent = tagText;
    document.querySelectorAll('.pay-amt-text').forEach(n => { n.textContent = amtText; });
    document.querySelectorAll('[data-select-plan]').forEach(card => {
      card.classList.toggle('is-selected', card.dataset.selectPlan === selectedPayPlan);
    });
  };

  document.getElementById('modalPlansGrid')?.addEventListener('click', (e) => {
    const card = e.target.closest('[data-select-plan]');
    if (!card) return;
    applyPayPlanSelection(card.dataset.selectPlan);
  });
  document.getElementById('modalPlansGrid')?.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const card = e.target.closest('[data-select-plan]');
    if (!card) return;
    e.preventDefault();
    applyPayPlanSelection(card.dataset.selectPlan);
  });

  el.payMethodSwitch?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-pay-method]');
    if (!btn) return;
    selectedPayMethod = btn.dataset.payMethod === 'alipay' ? 'alipay' : 'wechat';
    el.payMethodSwitch.querySelectorAll('.pay-method-btn').forEach(b => b.classList.toggle('active', b === btn));
  });

  el.btnSubmitPayClaim?.addEventListener('click', async () => {
    const setStatus = (msg, cls = '') => {
      if (!el.payClaimMsg) return;
      el.payClaimMsg.textContent = msg;
      el.payClaimMsg.className = 'pay-claim-status' + (cls ? ' ' + cls : '');
    };
    if (!state.auth.user) {
      setStatus('请先在上方登录或 5 秒注册账号，再点「我已完成付款」。', 'is-err');
      el.authUsername?.focus();
      return;
    }
    const rawNote = (el.payReceiptInput?.value || '').trim();
    const note = rawNote || `已备注用户名：${state.auth.user.username}`;
    el.btnSubmitPayClaim.disabled = true;
    try {
      const res = await fetch('/api/billing/claim', {
        method: 'POST',
        headers: apiHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({
          plan: selectedPayPlan,
          pay_method: selectedPayMethod,
          receipt_note: note
        })
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        const errMsg = res.status === 405
          ? '后端服务尚未重启到最新版，请先重启 python server.py。'
          : (data.detail || '提交失败，请稍后重试');
        setStatus(errMsg, 'is-err');
        showToast(errMsg);
        return;
      }
      state.auth.user = data.user;
      if (el.payReceiptInput) el.payReceiptInput.value = '';
      setStatus('✓ ' + (data.message || '已收到付款确认，会员权限已开启！'), 'is-ok');
      updateAccountUI();
      showToast(data.message || '会员权限已开启！');
    } catch (err) {
      setStatus('网络请求失败，请稍后重试', 'is-err');
    } finally {
      el.btnSubmitPayClaim.disabled = false;
    }
  });

  // Global delegation for any frosted-glass "立即解锁" button
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-action="open-upgrade"]')) {
      openAccountModal('登录或 5 秒注册账号后，输入 Pro（¥4.9）或 Ultra（¥8.9）激活码即可立即解锁。');
    }
  });

  // Settings quick provider presets + API key auto-detection
  el.providerPresets?.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-preset]');
    if (!btn) return;
    const key = btn.dataset.preset;
    const preset = PROVIDER_PRESETS[key];
    if (!preset) return;
    el.providerPresets.querySelectorAll('.preset-chip').forEach(b => b.classList.toggle('active', b === btn));
    el.cfgProvider.value = preset.provider;
    el.cfgCustomEndpoint.value = preset.endpoint;
    el.cfgModelName.placeholder = preset.defaultModelHint;
    updateProviderVisibility();
  });

  el.cfgApiKey?.addEventListener('input', () => {
    const v = el.cfgApiKey.value.trim();
    if (v.startsWith('AIza')) {
      el.cfgProvider.value = 'gemini';
      updateProviderVisibility();
    } else if (v.startsWith('sk-') && el.cfgProvider.value === 'free_google') {
      el.cfgProvider.value = 'openai_compatible';
      updateProviderVisibility();
    }
  });
}

window.addEventListener('DOMContentLoaded', init);
