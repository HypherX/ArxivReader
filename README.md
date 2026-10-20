<div align="center">

# 📚 ArxivReader

在自己的电脑上读论文、提问，并把文献库整理成文件夹和关系图。数据留在本机。

📖 阅读原文 · 💬 继续追问 · 🌳 自动分类 · ☁️ 换电脑同步

[中文](README.md) · [English](README.en.md)

</div>

ArxivReader 是一个本地论文阅读器。你可以把 arXiv 论文或自己的 PDF 放进文库，在阅读器里看原文、划词翻译、全文翻译，也可以让自己配置的模型做总结、分类、精读和关系整理。不需要注册账号，也不需要单独部署服务器。

## 🚀 开始使用

### 🍎 下载桌面应用

到 [Releases](https://github.com/HypherX/ArxivReader/releases) 下载压缩包。目前提供的桌面包是 Apple Silicon（M 系列）Mac：`ArxivReader-macOS-arm64.zip`。

解压后把 `ArxivReader.app` 拖进「应用程序」，双击打开。如果系统提示无法验证开发者，到 **系统设置 → 隐私与安全性** 选择「仍要打开」。构建包没有签名，这个提示是正常的。压缩包附带 `.sha256` 校验文件。

其他系统请用下面的源码方式运行，也可以自行构建桌面应用。

### 💻 从源码运行

需要 Python 3.12 或更新版本。

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

浏览器打开 <http://127.0.0.1:8765>。想要一个桌面窗口时，再安装 `requirements-build.txt` 里的依赖，然后运行 `python desktop.py`。

### ⚙️ 第一次配置模型

首次打开会弹出设置。填三项：

1. **Base URL**：兼容 OpenAI Chat Completions 的接口地址。
2. **API Key**：你的密钥。
3. **模型**：例如 `gpt-4o`、`deepseek-chat`。

点「测试连接」会真实请求一次模型。确认可用后点「保存」，设置窗口会关闭。

不填模型也可以管理文库和阅读 PDF。总结、分类、精读、问答和翻译会在配置模型之后可用。

## ✨ 怎么用

### 📥 加入论文

- 🔗 **arXiv**：粘贴链接或编号，例如 `2401.12345`。应用会下载 PDF 并抽出全文。
- 📤 **本地 PDF**：直接上传。相同文件会按内容识别，避免重复入库。
- 🧲 **相关论文**：阅读当前论文的 related work 和参考文献，按摘要判断相关度。选中的论文只进入「全部论文」，之后再决定要不要总结或分类。
- 📥 **Zotero 导入**：顶栏点「📥 Zotero 导入」，读取本机 Zotero 数据目录（默认 `~/Zotero`，里面有 `zotero.sqlite` 和 `storage/`）。带 PDF 的论文会连同标题、作者、摘要复制进来，收藏夹按原来的层级建成文件夹；一篇论文在多个收藏夹时放进最深的那个。能认出 arXiv 编号的按编号去重，其余按 PDF 内容去重，已经在库里的会跳过，所以可以再导一次。Zotero 里的文件不会被移动或修改。导入前先关掉 Zotero。

新论文默认只出现在「全部论文」里。之后可以自动分类，也可以自己建文件夹再移动。选中某个文件夹时，会连同它下面的子文件夹一起显示。

### 📖 阅读和翻译

阅读器里可以翻页、缩放。选中的文字可以直接复制。

- 🖍 **划词翻译**把译文放在右侧，可以连续翻译并复制。
- 🌐 **全文翻译**把整篇 PDF 译成中文，可以看中英对照或纯译文，公式和图表留在原位。引擎比较大，第一次使用时再安装，说明见下文。
- 🔎 **查找**会高亮命中的文字，并能逐个跳转。

界面可以在中文和英文之间切换。

### 🧠 总结、分类、精读和问答

选中一篇论文后，有四个互相独立的动作。每个都可以单独重跑；重跑会用新结果替换旧结果，不保留上一版。

- 🧾 **快速总结**：较短的阅读笔记。
- 🗂 **自动分类**：需要先有快速总结。分类会把论文放进更贴合主题的文件夹，而不是一律塞进已有的大类；也不会细到一篇论文一个名字。重新分类会把论文移到新的位置。如果某个方向文件夹里已经没有论文，这个文件夹会被删掉。
- 📖 **深度精读**：更完整的阅读笔记。
- 🕸 **论文关系**：需要先完成深度精读和自动分类。抽出的关系画成可以点开的图。单篇论文看它自己的关系，文件夹看这一支里的关系。

💬 快速总结和深度精读的页面可以直接继续追问，问题留在当前页，不会跳进一个新对话。对话页彼此独立：新建一页就是一次新的对话，不带上其他窗口里的内容。对话和深度精读使用 PDF 全文，包括附录。

📄 一个文件夹下的论文还可以汇总成一份综述。

这些功能都使用你自己配置的模型，论文内容会发送到该服务。

## 🌐 全文翻译

全文翻译使用 [PDFMathTranslate](https://github.com/PDFMathTranslate/PDFMathTranslate-next)。它的依赖较大，所以不放进主程序，也不写在 `requirements.txt` 里。

1. 打开一篇论文，点阅读器里的全文翻译。
2. 在弹窗里点「安装引擎」。装一次之后可以继续用。

安装时会优先使用 [uv](https://docs.astral.sh/uv/)。没有 uv 时，源码运行使用当前的 Python 3.12 或更新版本；桌面应用会在常见位置查找 Python 3.10 到 3.13。macOS 自带的 `/usr/bin/python3` 是 3.9，不会使用。国内网络可以在 **设置 → 全文翻译（PDF）** 里打开安装镜像。

翻译模型默认跟随主模型。全文翻译用量较大，可以在同一页单独换成更省的服务。引擎和译文在数据目录的 `pdftranslate/` 里，删掉这个目录就等于卸载引擎。

## 📁 数据放在哪里

| 运行方式 | 数据目录 |
|---|---|
| 🍎 桌面应用（macOS） | `~/Library/Application Support/ArxivReader/data/` |
| 🪟 桌面应用（Windows） | `%LOCALAPPDATA%/ArxivReader/data/` |
| 💻 源码运行 | 项目里的 `data/` |

PDF、数据库和笔记都在这个目录。关闭应用后，复制整个目录就是一份备份。

API Key 明文存在本地数据库里，不要把 `data/` 发给别人。下载 arXiv 论文需要联网；使用模型时，费用和服务商的数据政策以对方为准。

## ☁️ 换电脑：云同步

顶栏的 **云同步** 可以把文库备份到云端，再在另一台电脑上恢复。任选一种：

- 🟢 **Google Drive**：填入你自己的 Google Cloud OAuth Client ID 和 Secret，连接一次。
- 🔌 **WebDAV**：适合坚果云、Nextcloud、ownCloud、群晖等。填写服务器地址、账号和应用密码。

坚果云可以这样配：

1. 在坚果云网页版打开 **账户信息 → 安全选项 → 第三方应用管理**，添加一个应用密码。
2. 服务器填 `https://dav.jianguoyun.com/dav/`，账号填邮箱，密码填应用密码。
3. 备份目录默认是 `ArxivReader`，可以改成例如 `我的坚果云/ArxivReader`。
4. 连接成功后使用上传或恢复。

没有变化的文件不会重复上传。恢复前会保留当前本地数据；校验失败时不会改动本地文件。云端副本不含 API Key 和同步密码。除本机测试外，只接受 HTTPS 地址。

## 📜 许可证

ArxivReader 以 [MIT 许可证](LICENSE) 开源，可以自由使用、修改和分发。

## 🙏 致谢

感谢这些项目：

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
