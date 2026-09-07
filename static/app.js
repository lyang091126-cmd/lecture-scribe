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
  const brandStyle = "background: linear-gradient(135deg, #4f46e5, #06b6d4); color: #ffffff; font-size: 13px; font-weight: bold; padding: 4px 10px; border-radius: 4px 0 0 4px;";
  const authorStyle = "background: #0f172a; color: #38bdf8; font-size: 13px; font-weight: bold; padding: 4px 10px; border-radius: 0 4px 4px 0;";
  const warnStyle = "color: #f59e0b; font-size: 11px; font-weight: 600; line-height: 1.6; margin-top: 4px;";
  
  console.log("%cLectureScribe AI%cAuthor: Blueberry (@lyang091126-cmd)", brandStyle, authorStyle);
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
  valPauseThreshold: document.getElementById('valPauseThreshold'),

  // Sponsor / Donate Modal
  sponsorModal: document.getElementById('sponsorModal'),
  btnSponsor: document.getElementById('btnSponsor'),
  btnSidebarSponsor: document.getElementById('btnSidebarSponsor'),
  btnCloseSponsor: document.getElementById('btnCloseSponsor'),
  btnDismissSponsor: document.getElementById('btnDismissSponsor')
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
        el.sessionTitleInput.value = state.session.title || '计算机体系结构 Lecture 01';
        renderCards();
        renderSummaryUI(state.session.summary);
        updateSidebarOutline();
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
  el.statusText.textContent = isRec ? '正在收音' : '已暂停';
}

function updateStats() {
  const starred = state.session.items.filter(it => it.starred).length;
  el.starCount.textContent = starred;

  const costRmb = (state.totalTokensEst / 1000 * 0.0012).toFixed(3);
  el.tokenText.textContent = `~${state.totalTokensEst.toLocaleString()} Tokens (~¥${costRmb})`;
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
          ${keywordsBarHtml}
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
        <p style="font-size: 0.8rem; color: var(--text-muted); text-align: center; padding: 22px 8px; line-height: 1.6;">
          暂未收录复杂专有名词<br>
          <span style="font-size: 0.74rem; opacity: 0.75;">遇到算法、协议、网络安全或架构专有名词时将自动解析</span>
        </p>
      </div>
    `;
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

  // Sponsor / Donate Modal Listeners
  const openSponsor = () => {
    el.sponsorModal?.classList.add('open');
    lucide.createIcons();
  };
  const closeSponsor = () => {
    el.sponsorModal?.classList.remove('open');
  };

  el.btnSponsor?.addEventListener('click', openSponsor);
  el.btnSidebarSponsor?.addEventListener('click', openSponsor);
  el.btnCloseSponsor?.addEventListener('click', closeSponsor);
  el.btnDismissSponsor?.addEventListener('click', closeSponsor);
  el.sponsorModal?.addEventListener('click', (e) => {
    if (e.target === el.sponsorModal) closeSponsor();
  });
}

window.addEventListener('DOMContentLoaded', init);
