/**
 * LectureScribe - 课堂智能双语速记与 AI 智能讲解工作台
 */

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
    summary: null
  },
  
  totalTokensEst: 0,
  filterStarredOnly: false,
  searchQuery: '',
  
  config: {
    provider: 'gemini',
    apiKey: '',
    modelName: 'gemini-3.7-flash',
    customEndpoint: '',
    pauseThreshold: 1.4
  }
};

// DOM Elements
const el = {
  sessionTitleInput: document.getElementById('sessionTitleInput'),
  statusPill: document.getElementById('statusPill'),
  statusText: document.getElementById('statusText'),
  timerText: document.getElementById('timerText'),
  tokenText: document.getElementById('tokenText'),
  btnStarFilter: document.getElementById('btnStarFilter'),
  starCount: document.getElementById('starCount'),
  btnExportMenu: document.getElementById('btnExportMenu'),
  exportDropdown: document.getElementById('exportDropdown'),
  exportDocx: document.getElementById('exportDocx'),
  exportMd: document.getElementById('exportMd'),
  exportSrt: document.getElementById('exportSrt'),
  btnSettings: document.getElementById('btnSettings'),
  btnToggleRecord: document.getElementById('btnToggleRecord'),
  recordIcon: document.getElementById('recordIcon'),
  recordText: document.getElementById('recordText'),
  audioSourceSelect: document.getElementById('audioSourceSelect'),
  sourceLangSelect: document.getElementById('sourceLangSelect'),
  waveformCanvas: document.getElementById('waveformCanvas'),
  searchInput: document.getElementById('searchInput'),
  btnGenerateSummary: document.getElementById('btnGenerateSummary'),
  btnClearNotes: document.getElementById('btnClearNotes'),
  cardsContainer: document.getElementById('cardsContainer'),
  cardsList: document.getElementById('cardsList'),
  emptyState: document.getElementById('emptyState'),
  blackboardSidebar: document.getElementById('blackboardSidebar'),
  summaryOverview: document.getElementById('summaryOverview'),
  takeawaysList: document.getElementById('takeawaysList'),
  glossaryList: document.getElementById('glossaryList'),
  historyList: document.getElementById('historyList'),
  btnRefreshSummary: document.getElementById('btnRefreshSummary'),
  activeDock: document.getElementById('activeDock'),
  activeSourceText: document.getElementById('activeSourceText'),
  btnForceFlush: document.getElementById('btnForceFlush'),
  apiWarningBanner: document.getElementById('apiWarningBanner'),
  apiWarningText: document.getElementById('apiWarningText'),
  btnFixApiKey: document.getElementById('btnFixApiKey'),
  
  // Modal
  settingsModal: document.getElementById('settingsModal'),
  btnCloseSettings: document.getElementById('btnCloseSettings'),
  btnCancelSettings: document.getElementById('btnCancelSettings'),
  btnSaveSettings: document.getElementById('btnSaveSettings'),
  cfgProvider: document.getElementById('cfgProvider'),
  cfgApiKey: document.getElementById('cfgApiKey'),
  cfgModelName: document.getElementById('cfgModelName'),
  cfgCustomEndpoint: document.getElementById('cfgCustomEndpoint'),
  groupCustomEndpoint: document.getElementById('groupCustomEndpoint'),
  cfgPauseThreshold: document.getElementById('cfgPauseThreshold'),
  valPauseThreshold: document.getElementById('valPauseThreshold')
};

// Initialize
function init() {
  loadConfig();
  loadSavedSession();
  setupEventListeners();
  lucide.createIcons();
  fetchHistorySessions();
}

// Load / Save Config
function loadConfig() {
  const saved = localStorage.getItem('lecture_scribe_config');
  if (saved) {
    try {
      state.config = { ...state.config, ...JSON.parse(saved) };
    } catch (e) {}
  }
  if (!state.config.modelName || state.config.modelName.includes('2.0') || state.config.modelName.includes('1.5')) {
    state.config.modelName = 'gemini-3.7-flash';
  }
  el.cfgProvider.value = state.config.provider;
  el.cfgApiKey.value = state.config.apiKey;
  el.cfgModelName.value = state.config.modelName;
  if (!state.config.pauseThreshold || state.config.pauseThreshold > 2.0) {
    state.config.pauseThreshold = 1.4;
  }
  el.cfgPauseThreshold.value = state.config.pauseThreshold;
  el.valPauseThreshold.textContent = state.config.pauseThreshold + ' 秒';
  state.pauseThreshold = parseFloat(state.config.pauseThreshold);
  updateProviderVisibility();
}

function saveConfig() {
  state.config.provider = el.cfgProvider.value;
  state.config.apiKey = el.cfgApiKey.value.trim();
  state.config.modelName = el.cfgModelName.value.trim();
  state.config.customEndpoint = el.cfgCustomEndpoint.value.trim();
  state.config.pauseThreshold = parseFloat(el.cfgPauseThreshold.value);
  state.pauseThreshold = state.config.pauseThreshold;
  localStorage.setItem('lecture_scribe_config', JSON.stringify(state.config));
  el.apiWarningBanner.classList.remove('show');
  showToast('设置已保存');
}

function updateProviderVisibility() {
  const prov = el.cfgProvider.value;
  if (prov === 'openai_compatible') {
    el.groupCustomEndpoint.style.display = 'flex';
  } else {
    el.groupCustomEndpoint.style.display = 'none';
  }
}

// Load / Save Current Session
function loadSavedSession() {
  const saved = localStorage.getItem('lecture_scribe_current_session');
  if (saved) {
    try {
      const data = JSON.parse(saved);
      if (data && data.items && data.items.length > 0) {
        state.session = data;
        el.sessionTitleInput.value = state.session.title || '计算机体系结构 Lecture 01';
        renderCards();
        renderSummaryUI(state.session.summary);
        updateStats();
      }
    } catch (e) {}
  }
}

function persistSession() {
  state.session.title = el.sessionTitleInput.value.trim() || '未命名课程';
  state.session.updated_at = Date.now() / 1000;
  state.session.duration_seconds = state.elapsedSeconds;
  localStorage.setItem('lecture_scribe_current_session', JSON.stringify(state.session));
  fetch('/api/sessions', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(state.session)
  }).catch(() => {});
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

    drawWaveform();
  } catch (err) {
    console.warn('Audio capture warning:', err);
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

    ctx.fillStyle = '#0f172a';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    const barWidth = (canvas.width / bufferLength) * 1.5;
    let x = 0;
    for (let i = 0; i < bufferLength; i++) {
      const barHeight = (dataArray[i] / 255) * canvas.height;
      ctx.fillStyle = barHeight > canvas.height * 0.7 ? '#10b981' : '#6366f1';
      ctx.fillRect(x, canvas.height - barHeight, barWidth, barHeight);
      x += barWidth + 1;
    }
  }
  render();
}

// Speech Recognition Engine
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
    updateStatus(true);
  };

  let flushedPrefix = '';

  recognition.onresult = (event) => {
    let interim = '';
    for (let i = event.resultIndex; i < event.results.length; ++i) {
      const transcript = event.results[i][0].transcript;
      if (event.results[i].isFinal) {
        flushedPrefix = '';
        handleFinalSentence(transcript.trim());
      } else {
        interim += transcript;
      }
    }
    if (interim) {
      let trimmed = interim.trim();
      if (flushedPrefix && trimmed.startsWith(flushedPrefix)) {
        trimmed = trimmed.slice(flushedPrefix.length).trim();
      }
      if (!trimmed) return;

      const prevTrimmed = (state.activeInterimText || '').trim();
      state.activeInterimText = trimmed;
      el.activeSourceText.textContent = trimmed;

      // 智能意群分段：若临时文本已达到完整语意（>=15个词、>=80字符、或遇到句末标点）立即分段翻译
      const wordCount = trimmed.split(/\s+/).length;
      const hasSentenceEnd = /[.?!。？！]\s*$/.test(trimmed) && trimmed.length >= 20;

      if (wordCount >= 16 || trimmed.length >= 80 || hasSentenceEnd) {
        flushedPrefix = interim.trim();
        flushThoughtBuffer();
      } else if (trimmed !== prevTrimmed) {
        // 只有听到新词时才重置停顿计时器，防止底噪反复重置导致死锁
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
    if (event.error === 'not-allowed') {
      showToast('麦克风权限被拒绝，请在浏览器地址栏允许麦克风权限！');
      toggleRecording(false);
    }
  };

  recognition.onend = () => {
    if (state.isRecording) {
      // 浏览器转写周期结束时，如果缓冲区有未翻译文字立即触发成卡
      if (state.thoughtBuffer.length > 0 || (state.activeInterimText && state.activeInterimText.trim().length >= 3)) {
        flushThoughtBuffer();
      }
      try {
        recognition.start();
      } catch (e) {}
    } else {
      updateStatus(false);
    }
  };

  return recognition;
}

function handleFinalSentence(sentence) {
  if (!sentence) return;
  state.thoughtBuffer.push(sentence);
  el.activeSourceText.textContent = state.thoughtBuffer.join(' ') + ' ...';

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
  state.thoughtBuffer = [];
  state.activeInterimText = '';
  el.activeSourceText.textContent = '等待声音输入中...';

  if (!rawText || rawText.length < 3) return;

  const cardTimeSec = Math.floor(state.elapsedSeconds);
  const timeStr = formatTime(cardTimeSec);

  const now = Date.now();
  const tempId = 'card_' + now;
  const newCard = {
    id: tempId,
    timestamp: now,
    time_sec: cardTimeSec,
    time_str: timeStr,
    source_text: rawText,
    cleaned_source: rawText,
    translation: '正在生成学术译文与 AI 智能讲解...',
    keywords: [],
    annotations: [],
    section_heading: null,
    starred: false,
    loading: true
  };

  // 最新的内容放在最上面，把旧内容挤下去
  state.session.items.unshift(newCard);
  renderCards();
  scrollToTop();

  try {
    const res = await fetch('/api/translate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text: rawText,
        source_lang: state.session.source_lang,
        target_lang: 'zh',
        provider: state.config.provider,
        api_key: state.config.apiKey,
        custom_endpoint: state.config.customEndpoint,
        model_name: state.config.modelName
      })
    });

    if (res.ok) {
      const data = await res.json();
      newCard.cleaned_source = data.cleaned_source || rawText;
      newCard.translation = data.translation || '';
      newCard.keywords = data.keywords || [];
      newCard.annotations = data.annotations || [];
      newCard.section_heading = data.section_heading || null;
      newCard.loading = false;

      // Handle API Key error notice
      if (data.api_error) {
        el.apiWarningText.textContent = data.api_error;
        el.apiWarningBanner.classList.add('show');
      } else {
        el.apiWarningBanner.classList.remove('show');
      }

      const estTokens = Math.floor(rawText.length / 3) + Math.floor((data.translation || '').length * 1.5) + 120;
      state.totalTokensEst += estTokens;
      updateStats();

      updateSidebarOutline();
    }
  } catch (err) {
    console.error('Translation error:', err);
    newCard.translation = rawText;
    newCard.loading = false;
  }

  persistSession();
  renderCards();
  lucide.createIcons();
}

function toggleRecording(forceState) {
  const shouldStart = forceState !== undefined ? forceState : !state.isRecording;
  if (shouldStart) {
    if (!state.recognition) {
      state.recognition = setupSpeechRecognition();
      if (!state.recognition) return;
    }
    try {
      state.recognition.lang = el.sourceLangSelect.value;
      state.recognition.start();
      state.isRecording = true;
      startTimer();
      startAudioMonitoring();
      updateStatus(true);
      el.recordIcon.setAttribute('data-lucide', 'square');
      el.recordText.textContent = '暂停课堂收音';
      el.btnToggleRecord.classList.add('recording');
      el.statusPill.classList.add('recording');
      lucide.createIcons();
      showToast('已开始课堂听翻，正在收音...');
    } catch (e) {
      console.warn('Start err:', e);
    }
  } else {
    state.isRecording = false;
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
    persistSession();
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
  el.statusText.textContent = isRec ? '正在上课收音中' : '已暂停';
}

function updateStats() {
  const starred = state.session.items.filter(it => it.starred).length;
  el.starCount.textContent = starred;

  const costRmb = (state.totalTokensEst / 1000 * 0.0012).toFixed(3);
  el.tokenText.textContent = `已用: ~${state.totalTokensEst.toLocaleString()} Tokens (~¥${costRmb})`;
}

function getCardTimestamp(c) {
  if (c.timestamp) return c.timestamp;
  if (c.id && typeof c.id === 'string') {
    const num = parseInt(c.id.replace(/\D/g, ''), 10);
    if (!isNaN(num) && num > 1000000000000) return num;
  }
  return (c.time_sec || 0) * 1000;
}

// Render Knowledge Cards with AI Smart Annotations
function renderCards() {
  const items = state.session.items;
  if (!items || items.length === 0) {
    el.emptyState.style.display = 'flex';
    el.cardsList.innerHTML = '';
    return;
  }
  el.emptyState.style.display = 'none';

  // 严格保证：按创建绝对时间倒序排列，最新生成的卡片 100% 绝对在最上面！
  let sortedItems = [...items].sort((a, b) => getCardTimestamp(b) - getCardTimestamp(a));

  let filtered = sortedItems;
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

  let html = '';
  filtered.forEach((card, idx) => {
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
    const keywordsHtml = (card.keywords || []).map(kw => `
      <span class="keyword-pill"><i data-lucide="tag"></i>${escapeHtml(kw)}</span>
    `).join('');

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

      annotationsHtml = `
        <div class="card-annotations-box">
          <div class="anno-header">
            <i data-lucide="sparkles"></i>
            <span>AI 智能讲解与背景批注</span>
          </div>
          ${itemsHtml}
        </div>
      `;
    }

    html += `
      <div class="lecture-card ${starClass}" data-id="${card.id}">
        <div class="card-header-bar">
          <div class="card-meta-left">
            <span class="card-time-pill">⏱ ${card.time_str || '00:00'}</span>
            <span class="card-topic-pill">知识点 #${filtered.length - idx}</span>
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
            <p class="card-trans-text">${escapeHtml(card.translation)}</p>
          </div>

          ${annotationsHtml}
        </div>

        <div class="card-qa-bar">
          <div class="card-keywords-bar">
            ${keywordsHtml}
          </div>
          <button class="btn-ask-ai" data-action="open-qa" data-id="${card.id}">
            <i data-lucide="message-square"></i>
            <span>问问助教 (深度答疑)</span>
          </button>
        </div>

        <!-- QA Drawer -->
        <div class="card-qa-drawer" id="qa_drawer_${card.id}">
          <div class="qa-input-row">
            <input type="text" id="qa_input_${card.id}" placeholder="输入您的疑问，如：用大白话打个比方？或这段怎么考？">
            <button class="btn-sm btn-primary" data-action="submit-qa" data-id="${card.id}">提问</button>
          </div>
          <div class="qa-answer-box" id="qa_ans_${card.id}" style="display:none;"></div>
        </div>
      </div>
    `;
  });

  el.cardsList.innerHTML = html;
  lucide.createIcons();
}

function scrollToTop() {
  el.cardsContainer.scrollTop = 0;
}

// Blackboard Outline
function updateSidebarOutline() {
  const allKws = [];
  state.session.items.forEach(it => {
    if (it.keywords) allKws.push(...it.keywords);
    if (it.annotations) {
      it.annotations.forEach(a => allKws.push(a.term));
    }
  });
  const uniqueKws = [...new Set(allKws)].slice(0, 10);

  if (uniqueKws.length > 0 && !state.session.summary) {
    el.glossaryList.innerHTML = uniqueKws.map(kw => `
      <div class="glossary-item">
        <div class="term-head">
          <span class="term-en">${escapeHtml(kw)}</span>
        </div>
        <div class="term-desc">课堂核心概念 / 术语</div>
      </div>
    `).join('');
  }
}

async function generateSummary() {
  if (state.session.items.length === 0) {
    showToast('当前课堂记录为空，请先录制讲课内容！');
    return;
  }
  showToast('正在调用大模型分析整堂课板书与重点...');
  el.btnGenerateSummary.disabled = true;

  try {
    const res = await fetch('/api/summarize', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
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
    el.glossaryList.innerHTML = summary.glossary.map(g => `
      <div class="glossary-item">
        <div class="term-head">
          <span class="term-en">${escapeHtml(g.term_en || '')}</span>
          <span class="term-zh">${escapeHtml(g.term_zh || '')}</span>
        </div>
        <div class="term-desc">${escapeHtml(g.desc || '')}</div>
      </div>
    `).join('');
  }
}

async function fetchHistorySessions() {
  try {
    const res = await fetch('/api/sessions');
    if (res.ok) {
      const list = await res.json();
      if (list.length > 0) {
        el.historyList.innerHTML = list.slice(0, 5).map(s => {
          const dateStr = new Date(s.created_at * 1000).toLocaleDateString();
          return `
            <div class="history-item" data-session-id="${s.id}">
              <span class="history-item-title">${escapeHtml(s.title)}</span>
              <span class="history-item-meta">${dateStr} (${s.item_count}条)</span>
            </div>
          `;
        }).join('');

        el.historyList.querySelectorAll('.history-item').forEach(item => {
          item.addEventListener('click', () => loadHistoricalSession(item.dataset.sessionId));
        });
      }
    }
  } catch (e) {}
}

async function loadHistoricalSession(id) {
  try {
    const res = await fetch(`/api/sessions/${id}`);
    if (res.ok) {
      const data = await res.json();
      state.session = data;
      el.sessionTitleInput.value = data.title;
      state.elapsedSeconds = data.duration_seconds || 0;
      el.timerText.textContent = formatTimeFull(state.elapsedSeconds);
      renderCards();
      renderSummaryUI(data.summary);
      updateStats();
      showToast('已加载历史课堂记录: ' + data.title);
    }
  } catch (e) {}
}

async function exportFile(type) {
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
      headers: { 'Content-Type': 'application/json' },
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

  el.btnStarFilter.addEventListener('click', () => {
    state.filterStarredOnly = !state.filterStarredOnly;
    el.btnStarFilter.classList.toggle('active', state.filterStarredOnly);
    renderCards();
  });

  el.searchInput.addEventListener('input', (e) => {
    state.searchQuery = e.target.value.trim();
    renderCards();
  });

  el.btnGenerateSummary.addEventListener('click', generateSummary);
  el.btnRefreshSummary.addEventListener('click', generateSummary);

  el.btnClearNotes.addEventListener('click', () => {
    if (confirm('确定清空当前内容并新建一堂课的记录吗？（历史课件已安全保存在本地，随时可恢复）')) {
      state.session = {
        id: 'session_' + Date.now(),
        title: '新课堂 ' + new Date().toLocaleDateString(),
        created_at: Date.now() / 1000,
        updated_at: Date.now() / 1000,
        source_lang: el.sourceLangSelect.value.split('-')[0],
        target_lang: 'zh',
        duration_seconds: 0,
        items: [],
        summary: null
      };
      state.elapsedSeconds = 0;
      state.totalTokensEst = 0;
      el.sessionTitleInput.value = state.session.title;
      el.timerText.textContent = '00:00:00';
      renderCards();
      renderSummaryUI({ overview: '尚未生成概览', takeaways: [], glossary: [] });
      updateStats();
      persistSession();
      fetchHistorySessions();
    }
  });

  // Card click actions
  el.cardsList.addEventListener('click', async (e) => {
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

    // Open QA Drawer
    const qaBtn = e.target.closest('[data-action="open-qa"]');
    if (qaBtn) {
      const cardId = qaBtn.dataset.id;
      const drawer = document.getElementById(`qa_drawer_${cardId}`);
      if (drawer) {
        drawer.classList.toggle('open');
        const input = document.getElementById(`qa_input_${cardId}`);
        if (input && drawer.classList.contains('open')) input.focus();
      }
      return;
    }

    // Submit QA
    const submitQaBtn = e.target.closest('[data-action="submit-qa"]');
    if (submitQaBtn) {
      const cardId = submitQaBtn.dataset.id;
      const card = state.session.items.find(it => it.id === cardId);
      const input = document.getElementById(`qa_input_${cardId}`);
      const ansBox = document.getElementById(`qa_ans_${cardId}`);
      if (!card || !input || !ansBox) return;

      const q = input.value.trim() || '这段话的核心概念是什么？老师在强调什么？';
      ansBox.style.display = 'block';
      ansBox.innerHTML = '<span class="placeholder-text">🤖 助教正在思考并组织解答...</span>';

      try {
        const res = await fetch('/api/ask-card', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            card_source: card.cleaned_source || card.source_text,
            card_translation: card.translation,
            question: q,
            provider: state.config.provider,
            api_key: state.config.apiKey,
            custom_endpoint: state.config.customEndpoint,
            model_name: state.config.modelName
          })
        });
        if (res.ok) {
          const data = await res.json();
          ansBox.innerHTML = `<strong>💡 助教解答:</strong> ${escapeHtml(data.answer)}`;
        }
      } catch (err) {
        ansBox.textContent = '解答请求失败，请检查网络！';
      }
    }
  });

  // Export Dropdown
  el.btnExportMenu.addEventListener('click', (e) => {
    e.stopPropagation();
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

  el.sessionTitleInput.addEventListener('change', persistSession);
}

window.addEventListener('DOMContentLoaded', init);
