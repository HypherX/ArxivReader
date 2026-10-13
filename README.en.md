# ArxivReader
[中文](README.md) | [English](README.en.md)

A paper-reading tool that runs on your own computer: collect arXiv papers or local PDF files into a library, read PDFs right in the app, ask an AI questions about them, generate summaries, and organize a pile of papers into a topic tree and review notes.

All data stays on your own machine. No account, no server to deploy.

## What it does

- **Collect papers**: paste an arXiv link or ID (e.g. `2401.12345`), or upload a PDF file straight from your computer.
- **Read papers**: a built-in PDF reader; ask the AI questions with the full text as context, or generate a quick summary / deep-reading note in one click.
- **Connect the dots**: papers are automatically filed into a topic tree; papers under the same topic get their relationships extracted (improves, contradicts, complements…), and any folder can be turned into a review report.
- **Discover in bulk**: search arXiv by category, keywords and date; the AI pre-screens results into a reading queue, and you decide which ones earn a full download.

## Quick start

### Option 1: use a packaged build (for most people)

There is no official installer download yet. If you have a built ZIP from a source you trust:

- **macOS**: unzip, drag `ArxivReader.app` into Applications, and open it.
  If macOS says the developer cannot be verified, go to System Settings → Privacy & Security and click "Open Anyway" — no need to turn off system protection. Note that Apple Silicon and Intel builds are not interchangeable.
- **Windows x64**: unzip and keep all files together, then double-click `ArxivReader.exe`.
  If it complains about WebView2, follow the prompt to install Microsoft's WebView2 Runtime and reopen the app. No Python or terminal needed.

### Option 2: run from source

You need Python 3.12:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start (either one)
python desktop.py                                              # desktop window
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765    # then open http://127.0.0.1:8765 in a browser
```

### Option 3: build the app yourself

Build on the same operating system you are targeting (build macOS apps on macOS, Windows apps on Windows):

```bash
python3 -m venv .build-venv
.build-venv/bin/pip install -r requirements-build.txt    # on Windows use .build-venv\Scripts\pip.exe
.build-venv/bin/python tools/build_desktop.py
```

Artifacts land in `dist/`, together with a SHA256 file.

## First run: connect an AI model

Summaries, Q&A and reviews are powered by a large language model that *you* provide. On first launch the Settings page opens automatically — fill in three fields:

1. **Base URL**: the API endpoint from your model provider (anything compatible with the OpenAI Chat Completions API works).
2. **API Key**: your key.
3. **Model**: a model name your provider supports, e.g. `gpt-4o` or `deepseek-chat`.

Click "Test connection" to verify (this makes one real model call and may cost a few cents), then "Save".
Without a model configured, the app still works for managing papers and reading PDFs — only the AI features are unavailable.

## Daily use

**Add papers** (two ways, both at the top of the page):

1. Paste an arXiv link or ID → click "Add"; the PDF is downloaded and its full text extracted automatically.
2. Click "📤 Upload PDF" → pick a PDF from your computer. Uploaded files are remembered by content, so uploading the same file twice tells you it is already in the library.

For both ways you can pick a target folder in the dropdown first; otherwise papers are filed automatically (arXiv papers follow your filing rules, local PDFs go to Inbox).

**Read and ask**: click a paper in the list — the PDF is in the middle, the chat panel on the right. Ask things like "what is the core method of this paper?"

**AI features**:

- "🧠 Knowledge network": runs quick summary → auto-classification → deep reading → relation extraction on the current paper.
- "🔗 Related-work search": reads the paper's references and finds strongly related papers on arXiv automatically.
- "🔎 ArXiv search": bulk search by criteria, AI-screened into a reading queue.
- Folder context menu / "Generate review": turns a folder of papers into a review report; all historical versions stay browsable.

**Long tasks can be stopped at any time** — completed parts are kept and can be resumed later.

## Where the data lives

- Packaged app: `~/Library/Application Support/ArxivReader/data/` on macOS, `%LOCALAPPDATA%/ArxivReader/data/` on Windows.
- Running from source: the `data/` folder inside the project.
- PDFs, the database and notes all live in that directory. To back up, close the app and copy the whole folder.

**Two things worth knowing**:

1. The API key is stored in the local database without encryption. Do not share your data folder with others.
2. "Stored locally" does not mean "fully offline": searching and downloading from arXiv needs the network, and AI features send paper content to the model provider you configured and may incur charges. Your provider's data policy applies.

## FAQ

**An uploaded PDF has no authors or abstract?**
Local PDFs carry no arXiv metadata, so the title falls back to the PDF's own document title or the file name — you can rename it in place. AI Q&A and summaries are unaffected because the full text is still extracted.

**Can't ask questions about a scanned PDF?**
Scanned pages have no text layer, so nothing can be extracted. Try "↻ Re-extract text" on the paper toolbar once; if it still fails, run the file through an OCR tool first.

**macOS says the app is damaged / can't be opened?**
That is the standard prompt for unsigned apps — allow it under "Privacy & Security" as described in Quick start.

**Moving to a new computer?**
Close the app and copy the whole data folder to the same location on the new machine.

## Licensing

The project license has not been decided yet. PyMuPDF is dual-licensed under the AGPL or a commercial license; packaging and distribution carry those obligations. See [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
