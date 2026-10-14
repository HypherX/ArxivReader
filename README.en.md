<div align="center">

# 📚 ArxivReader

**A paper reader that runs on your own machine** — collect arXiv or local PDFs, read the original, translate selections, ask questions, and grow a topic tree with review notes.

![Release](https://img.shields.io/badge/download-Releases-4a57e6?style=flat-square&logo=github&logoColor=white)
![Platform](https://img.shields.io/badge/macOS%20%C2%B7%20Windows-supported-0f8f4d?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.12-3776ab?style=flat-square)
![Local](https://img.shields.io/badge/all_data-stays_local-78829a?style=flat-square)

[中文](README.md) · [English](README.en.md)

</div>

---

## ✨ Features

### 📥 Collecting papers

| | |
|---|---|
| 🔗 **arXiv in one click** | Paste a link or an ID such as `2401.12345`; the PDF is downloaded and the full body extracted. |
| 📤 **Local PDF upload** | Drop PDFs from your disk. Files are fingerprinted, so uploading the same PDF twice is detected. |
| 🧲 **Citation mining** | Read the paper's Related Work and References, fuzzy-search every title mentioned there on arXiv, drop titles whose match score is below 0.7, judge relevance against the current paper from each abstract (no full-text summarizing), and keep only candidates scoring above 0.6 for you to review; strong matches can be promoted into full reading. Nothing is capped — as many citations as the paper lists. |

### 📖 Reading and translation

| | |
|---|---|
| 🖨 **Built-in PDF reader** | Rendered locally with PDF.js on a real text layer: wheel and two-finger trackpad scrolling, pinch or ⌘/Ctrl + wheel zoom anchored at the cursor, plus fit width, exact zoom, jump to page and rotate. |
| 🖍 **Selection translation** | Select a word or a sentence, choose Translate, and the result lands in the translation panel beside the reader — translate several passages in a row, scroll back through them, copy any of them. |
| 🔎 **Find in document** | Search a keyword across the whole PDF, see the hit count and step through each match. |
| ⧉ **Select and copy** | The text layer is real text, so symbols, terms and paragraphs copy out cleanly. |
| 📏 **Whole text, no truncation** | Extraction has no character ceiling, so the end of long papers, appendices and references stay answerable and page-citable. |
| 🌐 **Bilingual UI** | Switch the whole interface between 中文 and English; translations follow the app's output-language setting. |

### 🧠 Understanding and organising

| | |
|---|---|
| 💬 **Ask the paper** | Full text is used as context and answers cite page numbers. Long runs can be stopped; generated content is kept. |
| 🧩 **Four independent AI actions** | Quick summary / classify / deep reading / relationships — each triggered and re-run on its own, each button showing what that step already produced. No need to run the whole chain. |
| 📝 **Reading notes** | Summaries and deep-reading notes keep version history, can be copied, exported as Markdown, or opened in a distraction-free window. |
| 🌳 **Topic tree** | Let the model file a paper under a single topic, which grows the folder hierarchy for you — or build subfolders yourself and move papers by hand. |
| 🕸 **Relationship graph** | A flow-chart-style directed graph: layered nodes, arrows labelled with the relation and its evidence strength, hover to highlight the incident edges, click a node to open that paper. Any paper shows its own neighbourhood; a topic folder shows the whole subgraph. |
| 📄 **Folder reviews** | Summarise every paper in a folder into one report — evidence is gathered in batches, interrupted runs resume, history stays readable. |

> Every AI feature runs on **a model you configure** — anything compatible with the OpenAI Chat Completions API works. No account, no server deployment.

---

## 🚀 Getting started

### Option 1: Download the desktop app (recommended)

Grab the archive for your platform from **[Releases](https://github.com/HypherX/ArxivReader/releases)**:

| Platform | File |
|---|---|
| 🍎 Apple Silicon | `ArxivReader-x.y.z-macOS-arm64.zip` |
| 🍎 Intel Mac | `ArxivReader-x.y.z-macOS-x64.zip` |
| 🪟 Windows x64 | `ArxivReader-x.y.z-Windows-x64.zip` |

- **macOS**: unzip, drag `ArxivReader.app` into Applications, open it. If you see "cannot verify developer", use **System Settings → Privacy & Security → Open Anyway** — you do not need to disable security features.
- **Windows**: unzip, keep all files in the folder, run `ArxivReader.exe`. If it asks for WebView2, install the [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) and start again. **Python is not required.**

> Builds are unsigned and not notarised, so the first-launch warning is expected. Every archive ships with a `.sha256` checksum.

### Option 2: Run from source (open in a browser)

Python 3.12 or newer:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the server, then open http://127.0.0.1:8765 in a browser
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# Or: open a desktop window (needs pywebview, see requirements-build.txt)
python desktop.py
```

---

## ⚙️ Configure a model on first run

First launch opens **Settings**; three fields are enough:

1. **Base URL** — your provider's API endpoint (OpenAI-compatible).
2. **API Key** — your key.
3. **Model** — e.g. `gpt-4o`, `deepseek-chat`.

Use **Test connection** to verify (it makes one real request), then **Save**.
**The app works without a model**: library management, PDF reading and text selection all keep working — only the AI features stay unavailable.

---

## 🗂 Where papers go

A newly added paper belongs only to **All papers** — it is never pushed into a subfolder automatically. Afterwards you can:

- press **🗂 Classify** and let the model file it under a topic in the tree (creating the matching folder);
- or make your own subfolders and move papers with **📁 Move** — including back to All papers.

All papers always lists every entry in the library; selecting a folder counts it together with all of its descendants.

---

## 📁 Where your data lives

| Run mode | Data directory |
|---|---|
| Desktop app (macOS) | `~/Library/Application Support/ArxivReader/data/` |
| Desktop app (Windows) | `%LOCALAPPDATA%/ArxivReader/data/` |
| From source | `data/` inside the project |

PDFs, the database and all notes live in that one folder, so **backing up = copying the folder** (quit the app first). Moving to another machine works the same way.

Two things worth knowing:

- The API key is stored unencrypted in the local database — do not share the whole `data/` folder.
- "Local" does not mean "offline": downloading papers from arXiv needs network access, and AI features send paper content to the provider you configure, which costs money and is governed by that provider's policies.

---

## ❓ FAQ

<details>
<summary><b>Uploaded PDFs have no authors or abstract?</b></summary>
Local PDFs carry no arXiv metadata, so the title comes from the PDF or the filename and can be renamed. Questions and summaries are unaffected — the body is extracted.
</details>

<details>
<summary><b>Scanned PDFs yield no text?</b></summary>
Scans have no text layer, so no text can be extracted; you need an OCR tool first. Such PDFs still read fine, the AI features just have no text to work with.
</details>

<details>
<summary><b>Do repeated translations cost money?</b></summary>
No. Translations are cached in your local library by a hash of the source text, so opening the same passage again costs nothing.
</details>

<details>
<summary><b>Can I stop a long run halfway?</b></summary>
Every AI task can be stopped at any time; whatever was already generated is kept and saved, so nothing is spent for nothing.
</details>

---

Third-party licences, versions and checksums ship with the app — see [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
