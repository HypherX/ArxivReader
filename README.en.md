<div align="center">

# 📚 ArxivReader

Read papers, ask questions, and organise a library into folders and a relationship graph. Everything stays on your computer.

📖 Read the PDF · 💬 Ask follow-ups · 🌳 Classify · ☁️ Sync to a new computer

[中文](README.md) · [English](README.en.md)

</div>

ArxivReader is a local paper reader. Add an arXiv paper or your own PDF, read it, translate a selection or the whole file, and use a model you configure for summaries, classification, deep reading, and relationships. There is no account to register and no server to deploy.

## 🚀 Getting started

### 🍎 Desktop app

Download a zip from [Releases](https://github.com/HypherX/ArxivReader/releases). The published desktop build is for Apple Silicon Macs: `ArxivReader-macOS-arm64.zip`.

Unzip it, drag `ArxivReader.app` into Applications, and open it. If macOS says the developer cannot be verified, choose **System Settings → Privacy & Security → Open Anyway**. The build is unsigned, so that prompt is expected. Each zip includes a `.sha256` checksum.

On other systems, run from source as below, or build the desktop app yourself.

### 💻 Run from source

Python 3.12 or newer:

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

Open <http://127.0.0.1:8765>. For a desktop window, install the extra packages in `requirements-build.txt` and run `python desktop.py`.

### ⚙️ Configure a model

The first launch opens Settings. Three fields are enough:

1. **Base URL** — an endpoint compatible with the OpenAI Chat Completions API.
2. **API Key** — your key.
3. **Model** — for example `gpt-4o` or `deepseek-chat`.

**Test connection** makes one real request. **Save** closes the window.

The library and the PDF reader work without a model. Summaries, classification, deep reading, chat, and translation become available after you save one.

## ✨ How to use it

### 📥 Add papers

- 🔗 **arXiv**: paste a link or an id such as `2401.12345`. The PDF is downloaded and the text is extracted.
- 📤 **Local PDF**: upload a file. The same file is recognised by its contents, so it is not added twice.
- 🧲 **Related papers**: the app reads Related Work and the references, then judges relevance from abstracts. A paper you keep is added only to **All papers**. Summary and classification stay separate.
- 📥 **Zotero import**: choose **📥 Import Zotero** in the top bar. The app reads your local Zotero data directory (by default `~/Zotero`, which contains `zotero.sqlite` and `storage/`). Papers that have a PDF are copied in with their title, authors, and abstract. Collections become folders at the same depth; a paper that sits in several collections goes into the deepest one. arXiv ids are recognised and used for deduplication; other PDFs are matched by content. Papers already in the library are skipped, so you can run the import again. Nothing in Zotero is moved or changed. Quit Zotero before importing.

A new paper stays in **All papers** until you classify it or move it into a folder you created. Selecting a folder also shows the papers in its subfolders.

### 📖 Read and translate

Page through the PDF and zoom. Selected text can be copied.

- 🖍 **Selection translation** appears in the panel beside the reader. You can translate several passages and copy any of them.
- 🌐 **Full-text translation** produces a Chinese PDF, either bilingual or translation-only, and keeps formulas and figures in place. The engine is large, so it is installed the first time you use it.
- 🔎 **Find** highlights each match and lets you step through them.

The interface can be switched between Chinese and English.

### 🧠 Summaries, classification, deep reading, and chat

Four actions are independent. Running one again replaces its previous result.

- 🧾 **Quick summary** — a short reading note.
- 🗂 **Classify** — requires a quick summary first. The paper is filed under a topic that matches it, rather than dropped into a broad folder that already exists, and rather than a folder named after one paper. Running it again moves the paper. A topic folder with no papers left is removed.
- 📖 **Deep reading** — a longer reading note.
- 🕸 **Relationships** — requires deep reading and classification first. The result is a graph you can click. One paper shows its own links; a folder shows the links inside that branch.

💬 You can keep asking questions on the quick-summary page and on the deep-reading page. The question stays on that page. Chat pages are separate: a new page is a new conversation and does not include other windows. Chat and deep reading use the full PDF, including appendices.

📄 Papers in one folder can also be gathered into a review.

These actions use the model you configured, and the paper is sent to that service.

## 🌐 Full-text translation

Full-text translation uses [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate-next). Its dependencies are large, so they are not part of the app and not listed in `requirements.txt`.

1. Open a paper and choose full-text translation in the reader.
2. Choose **Install engine**. One install is enough.

The installer prefers [uv](https://docs.astral.sh/uv/). Without uv, a source checkout uses the current Python 3.12 or newer, and the desktop app looks in common locations for Python 3.10 through 3.13. The Python 3.9 shipped at `/usr/bin/python3` on macOS is skipped. On a network in mainland China, turn on the install mirror under **Settings → PDF full-text translation**.

The translation model follows your main model unless you choose another one on that same settings page. The engine and translated PDFs live in `pdftranslate/` inside the data directory. Deleting that folder uninstalls the engine.

## 📁 Where your data lives

| How you run it | Data directory |
|---|---|
| 🍎 Desktop app (macOS) | `~/Library/Application Support/ArxivReader/data/` |
| 🪟 Desktop app (Windows) | `%LOCALAPPDATA%/ArxivReader/data/` |
| 💻 From source | `data/` in the project |

PDFs, the database, and notes are all in that folder. Quit the app, then copy the folder to make a backup.

The API key is stored in the local database in plain text. Do not share `data/`. Downloading from arXiv needs a network connection. Model use is billed by your provider and follows that provider's data policy.

## ☁️ Cloud sync

**Cloud sync** in the top bar backs up the library and restores it on another computer. Pick one backend:

- 🟢 **Google Drive** — paste the OAuth Client ID and Secret from your own Google Cloud project, then connect once.
- 🔌 **WebDAV** — Nutstore, Nextcloud, ownCloud, Synology, and similar servers. Enter the server URL, account, and an app password.

Nutstore:

1. On the Nutstore website, open **Account → Security → Third-party apps** and create an app password.
2. Set the server to `https://dav.jianguoyun.com/dav/`, then enter your email and that app password.
3. The backup folder defaults to `ArxivReader`. You can change it, for example to `我的坚果云/ArxivReader`.
4. After **Connect**, use upload or restore.

Unchanged files are skipped. A restore keeps a copy of the current local data, and a failed check does not replace it. The cloud copy does not include API keys or sync passwords. Only HTTPS addresses are accepted, aside from loopback addresses used for local testing.

## 📜 License

ArxivReader is open source under the [MIT License](LICENSE). You can use, modify, and distribute it freely.

## 🙏 Acknowledgements

Thanks to these projects:

- [FastAPI](https://github.com/fastapi/fastapi)
- [Uvicorn](https://github.com/encode/uvicorn)
- [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy)
- [Pydantic](https://github.com/pydantic/pydantic)
- [arxiv.py](https://github.com/lukasschwab/arxiv.py)
- [PyMuPDF](https://github.com/pymupdf/PyMuPDF)
- [OpenAI Python](https://github.com/openai/openai-python)
- [HTTPX](https://github.com/encode/httpx)
- [python-multipart](https://github.com/Kludex/python-multipart)
- [pywebview](https://github.com/r0x0r/pywebview)
- [platformdirs](https://github.com/platformdirs/platformdirs)
- [PyInstaller](https://github.com/pyinstaller/pyinstaller)
- [PDF.js](https://github.com/mozilla/pdf.js)
- [markdown-it](https://github.com/markdown-it/markdown-it)
- [DOMPurify](https://github.com/cure53/DOMPurify)
- [KaTeX](https://github.com/KaTeX/KaTeX)
- [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate-next)
