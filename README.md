# LectureScribe 📚✨

> **AI 驱动的课堂智能实时双语速记、深度学术批注与结构化板书工作台**  
> 专为大学课程、学术讲座与国际学术会议打造，告别“密密麻麻一坨字”的传统字幕体验。

[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688.svg)](https://fastapi.tiangolo.com/)
[![Google Gemini](https://img.shields.io/badge/AI-Google%20Gemini%203%20Flash-4285F4.svg)](https://ai.google.dev/)
[![License](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## 🌟 核心特性

### 1. 意群自适应聚合切分（去碎片化）
- **告别传统字幕一个单词一跳或一整坨密密麻麻的痛点**：内置高灵敏语音活动检测（VAD）与停顿阈值分析，智能判断讲师语意完备性，自动将一小段完整表述聚合为一个独立的“知识卡片”。
- **学术级去口头禅**：自动过滤口语噪声（如 *um*, *uh*, *you know*, *like*, *basically* 等），还原干净、连贯、高可读性的讲师英文原声。

### 2. 💡 AI 智能背景讲解与专业词汇批注
- **自动实体识别**：大模型实时识别讲课中提及的**专业术语、行业法规标准（如 HIPAA/FDA/NIST）、算法架构（如 CNN/Transformer）、历史典故或关键事件**。
- **背景透彻讲解**：在双语对照下方自动生成 1~3 条通俗易懂的背景知识卡，帮助跨学科或非母语同学在上课当下即刻理解。
- **问问助教 (HITL 深度答疑)**：每张知识卡片支持直接向助教提问（如“请用大白话打个比方？”、“这个概念考试一般怎么考？”）。

### 3. 📌 P2 控制栏全视口绝对锁定置顶
- 顶部的控制栏（收音开关、音源切换、麦克风电平监视器、搜索栏、提炼板书等）采用物理级 Flex 锁定，**上下滑动翻看长篇卡片时，控制栏 100% 纹丝不动**。

### 4. ⏱️ 毫秒级绝对时间戳倒序卡片流
- **视线永远聚焦最新**：新讲到的知识点（如知识点 #165）永远呈现在屏幕最上方视线焦点，以前讲过的旧卡片自然向下沉淀沉底。

### 5. 🚀 Google Gemini 3 系列高可用降级链
- 深度适配 Google 2026 最新官方模型架构（`gemini-3.7-flash` / `gemini-3.6-flash` / `gemini-3.8-flash`）。
- **全自动透明故障转移**：遇到高峰期拥堵（503）或频次限制（429）时，后端毫秒级自动回退至高速可用通道，听课记录零中断。

### 6. 📤 多格式课后精排一键导出
- **Word 文档 (`.docx`)**：自动排版为专业双语三栏对照表，内置 AI 重点提炼大纲与词汇表。
- **Markdown (`.md`)**：完美兼容 Notion、Obsidian、Logseq，带完整时间戳与高亮引用块。
- **双语字幕 (`.srt`)**：课后复习录播视频时可直接挂载。

---

## 🛠️ 快速启动指南

### 1. 克隆代码库
```bash
git clone https://github.com/lyang091126-cmd/lecture-scribe.git
cd lecture-scribe
```

### 2. 安装 Python 依赖
```bash
pip install -r requirements.txt
```

### 3. 配置 API Key（两种方式任选其一）
- **方式一（推荐）**：复制 `.env.example` 为 `.env`，填入您的 Google Gemini API Key：
  ```bash
  copy .env.example .env
  # 编辑 .env 文件：GEMINI_API_KEY=your_key_here
  ```
- **方式二**：直接启动后，在网页右上角 **设置 ⚙️** 弹窗中填入并保存至本地浏览器。

### 4. 一键启动
- **Windows 用户**：直接双击根目录下的 **`run.bat`**。
- **命令行用户**：
  ```bash
  python server.py
  ```
启动后在浏览器打开：👉 **`http://localhost:8000`**

---

## 🖥️ 技术架构

- **后端**：Python 3.10+, FastAPI, Uvicorn, HTTPX (异步并发), python-docx
- **前端**：Vanilla JavaScript (ES6+), HTML5 Web Audio API, Web Speech API, CSS3 Flexbox/Grid
- **模型支持**：Google Gemini 3 系列原生 REST API 与 OpenAI 兼容格式接口

---

## 📄 开源许可证
本项目采用 [MIT 许可证](LICENSE)。
