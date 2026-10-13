# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import json
import sys
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

root = Path(SPECPATH)
mac = sys.platform == "darwin"
generated = root / ".build-generated"
datas = []
# 只枚举运行资源；不扫描根目录，不收集资料库、旧配置、测试或预览数据。
for directory in ("static", "harness/prompts/zh", "harness/prompts/en"):
    for path in (root / directory).rglob("*"):
        if path.is_file() and path.name != ".DS_Store" and "__pycache__" not in path.parts:
            datas.append((str(path), str(path.parent.relative_to(root))))
datas += [(str(root / "THIRD_PARTY_NOTICES.txt"), "."),
          (str(generated / "build-info.json"), ".")]
for relative in json.loads((generated / "license-manifest.json").read_text(encoding="utf-8")):
    datas.append((str(generated / relative), str(Path(relative).parent)))
datas += collect_data_files("webview")
for package in ("openai", "arxiv", "pymupdf", "pywebview", "platformdirs"):
    datas += copy_metadata(package)
hiddenimports = ["uvicorn.logging", "uvicorn.loops.asyncio", "uvicorn.protocols.http.h11_impl",
                 "uvicorn.protocols.websockets.websockets_impl", "uvicorn.lifespan.on"]
for package in ("app", "harness", "knowledge"):
    for source in (root / package).rglob("*.py"):
        parts = list(source.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        hiddenimports.append(".".join(parts))
hiddenimports += (["webview.platforms.cocoa"] if mac else ["webview.platforms.winforms", "webview.platforms.edgechromium"])
a = Analysis([str(root / "desktop.py")], pathex=[str(root)], binaries=[], datas=datas,
             hiddenimports=hiddenimports,
             excludes=["pytest", "unittest", "tkinter", "IPython", "esprima", "bs4",
                       "webview.platforms.qt", "webview.platforms.gtk", "webview.platforms.cef",
                       "webview.platforms.mshtml"], noarchive=False)
pyz = PYZ(a.pure)
icon = str(generated / ("ArxivReader.icns" if mac else "ArxivReader.ico"))
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ArxivReader", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=icon, target_arch=None, codesign_identity=None, entitlements_file=None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ArxivReader")
if mac:
    app = BUNDLE(coll, name="ArxivReader.app", icon=icon,
                 bundle_identifier="org.arxivreader.desktop", version="0.2.0",
                 info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0",
                             "CFBundleShortVersionString": "0.2.0", "NSAppTransportSecurity": {
                                 "NSAllowsLocalNetworking": True,
                                 "NSExceptionDomains": {"127.0.0.1": {"NSExceptionAllowsInsecureHTTPLoads": True}}}})
