"""ArxivReader 桌面入口：本机子进程、受限桥接、单实例与有界退出。"""
import json
import multiprocessing as mp
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

from app.version import VERSION


def _backend(pipe, data_dir, ticket, cookie):
    try:
        from harness.config import set_data_dir
        set_data_dir(data_dir)
        from app import desktop_session
        import uvicorn
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        desktop_session.configure(sock.getsockname()[1], ticket, cookie)
        from app.main import app
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False,
                                              timeout_graceful_shutdown=8, loop="asyncio"))

        def control():
            while not server.started:
                if server.should_exit:
                    return
                time.sleep(.03)
            pipe.send({"ok": True, "port": sock.getsockname()[1]})
            try:
                while pipe.recv() != "stop":
                    pass
            except EOFError:
                pass
            from harness.cancel import stop_all
            from app.database import SessionLocal
            from app.models import FolderReview, RelatedRun
            stop_all()
            with SessionLocal() as db:
                for model in (FolderReview, RelatedRun):
                    db.query(model).filter(model.status.in_(("running", "queued"))).update(
                        {"cancel_requested": True}, synchronize_session=False)
                db.commit()
            # 后台任务先获得保存检查点的机会；随后关闭 HTTP 与流式请求。
            time.sleep(2)
            server.should_exit = True

        threading.Thread(target=control, daemon=True).start()
        server.run(sockets=[sock])
    except Exception:
        try:
            pipe.send({"ok": False, "code": "desktop.failed"})
        except (EOFError, OSError):
            pass
    finally:
        pipe.close()


def _root():
    from platformdirs import user_data_dir
    # 仅供隔离验收；常规双击启动无需任何环境变量。
    override = os.environ.get("ARXIVREADER_DESKTOP_HOME")
    return Path(override) if override else Path(user_data_dir("ArxivReader", appauthor=False))


def _preferences(root):
    path = root / "desktop.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invalid_desktop_preferences")
    return value


def _read_value(data, key):
    path = data / "library.db"
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else None
    except sqlite3.Error:
        return None


def _notice(message, english=False, download=False):
    if sys.platform == "darwin":
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("ArxivReader")
        alert.setInformativeText_(message)
        alert.addButtonWithTitle_("OK" if english else "确定")
        alert.runModal()
    elif sys.platform == "win32":
        import ctypes
        result = ctypes.windll.user32.MessageBoxW(None, message, "ArxivReader", 4 if download else 0)
        if download and result == 6:
            import webbrowser
            webbrowser.open("https://developer.microsoft.com/microsoft-edge/webview2/")


class _Instance:
    def __init__(self, root):
        self.file = open(root / "instance.lock", "a+b")
        if sys.platform != "win32":
            os.chmod(root / "instance.lock", 0o600)
        try:
            if sys.platform == "win32":
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b" "); self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.owned = False
            for _ in range(30):
                try:
                    self.file.seek(0)
                    info = json.loads(self.file.read().decode())
                    with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=1) as client:
                        client.sendall(info["token"].encode())
                    break
                except (OSError, ValueError, KeyError):
                    time.sleep(.1)
            self.file.close()
            return
        self.owned = True
        self.token = secrets.token_hex(32)
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(2)
        self.file.seek(0); self.file.truncate()
        self.file.write(json.dumps({"port": self.listener.getsockname()[1], "token": self.token}).encode())
        self.file.flush()

    def watch(self, callback):
        def wait():
            while self.owned:
                try:
                    client, _ = self.listener.accept()
                    with client:
                        client.settimeout(1)
                        if secrets.compare_digest(client.recv(128).decode(), self.token):
                            callback()
                except (OSError, UnicodeError):
                    pass
        threading.Thread(target=wait, daemon=True).start()

    def close(self):
        if self.owned:
            self.owned = False
            self.listener.close()
            self.file.close()


class _Bridge:
    def __init__(self, context):
        self._context = context
        self._window = None

    def perform(self, action, payload):
        """只开放单个白名单分派接口；不接受文件系统路径或命令。"""
        try:
            import webview
            from desktop_native import allowed, copy_text, external
            ctx = self._context
            if not self._window or not allowed(self._window.get_current_url() or "", ctx["origin"]):
                raise ValueError("untrusted_window")
            if not isinstance(payload, dict):
                raise ValueError("invalid_payload")
            if action == "storage_info":
                return {"ok": True, "path": str(ctx["data"])}
            if action == "open_storage":
                _open_file(ctx["data"])
            elif action == "select_storage":
                selection = self._window.create_file_dialog(webview.FileDialog.FOLDER)
                if not selection:
                    return {"ok": True, "selected": False}
                selected = Path(selection[0]).resolve()
                if not (selected / "library.db").is_file():
                    return {"ok": False, "code": "desktop.select_library"}
                with sqlite3.connect((selected / "library.db").as_uri() + "?mode=ro", uri=True) as db:
                    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {"papers", "settings"}.issubset(tables):
                        return {"ok": False, "code": "desktop.select_library"}
                temporary = ctx["root"] / "desktop.json.tmp"
                temporary.write_text(json.dumps({"data_dir": str(selected)}, ensure_ascii=False), encoding="utf-8")
                os.replace(temporary, ctx["root"] / "desktop.json")
                return {"ok": True, "selected": True}
            elif action in ("save_markdown", "copy"):
                text = payload.get("text")
                if not isinstance(text, str) or len(text) > 5_000_000:
                    raise ValueError("invalid_text")
                if action == "copy":
                    copy_text(text)
                else:
                    name = re.sub(r'[^\w .-]', "_", str(payload.get("name", "reading.md")))[:100]
                    if not name.endswith(".md"):
                        name += ".md"
                    selection = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=name,
                                                                file_types=("Markdown (*.md)",))
                    if selection:
                        path = selection if isinstance(selection, str) else selection[0]
                        Path(path).write_text(text, encoding="utf-8")
            elif action == "open_reader":
                path = payload.get("path", "")
                parts = urlsplit(path)
                params = parse_qs(parts.query)
                if parts.path != "/reader.html" or parts.netloc or parts.scheme:
                    raise ValueError("invalid_reader")
                if not set(params).issubset({"paper", "kind", "version", "review", "node"}):
                    raise ValueError("invalid_reader")
                for key, values in params.items():
                    if len(values) != 1 or (key == "kind" and values[0] not in ("summary", "deep_reading")):
                        raise ValueError("invalid_reader")
                    if key != "kind" and not values[0].isdigit():
                        raise ValueError("invalid_reader")
                bridge = _Bridge(ctx)
                bridge._window = webview.create_window("ArxivReader", ctx["origin"] + path, js_api=bridge,
                                                       width=980, height=820, min_size=(640, 480))
            elif action == "focus_main":
                ctx["main"].restore(); ctx["main"].show()
            elif action == "quit_app":
                # 自更新换包脚本已在后台等本进程退出；销毁主窗口即走正常关机流程
                self._window.destroy()
            elif action == "open_external":
                external(str(payload.get("url", "")), ctx["origin"])
            elif action == "save_translation":
                src = _translation_file(ctx, payload)
                name = _pdf_name(payload.get("name"), src.name)
                selection = self._window.create_file_dialog(
                    webview.FileDialog.SAVE, save_filename=name, file_types=("PDF (*.pdf)",))
                if selection:
                    dest = Path(selection if isinstance(selection, str) else selection[0])
                    shutil.copyfile(src, dest)
            elif action == "open_translation":
                _open_file(_translation_file(ctx, payload))
            else:
                raise ValueError("unknown_action")
            return {"ok": True}
        except Exception:
            return {"ok": False, "code": "desktop.failed"}


def _pdf_name(name, fallback):
    cleaned = re.sub(r"[^\w. -]", "_", str(name or fallback or "translation.pdf"))[:120].strip()
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned or "translation.pdf"


def _translation_file(ctx, payload):
    """只允许打开资料库 pdftranslate/output 里、数据库记下来的那一份译文。"""
    kind = payload.get("kind")
    raw_id = payload.get("task_id")
    if kind not in ("mono", "dual") or isinstance(raw_id, bool) or not isinstance(raw_id, (int, float)):
        raise ValueError("invalid_payload")
    task_id = int(raw_id)
    if task_id < 1 or task_id != raw_id:
        raise ValueError("invalid_payload")
    column = "mono_path" if kind == "mono" else "dual_path"
    db_path = Path(ctx["data"]) / "library.db"
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT " + column + " FROM pdf_translation_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row or not row[0]:
        raise ValueError("missing_file")
    path = Path(row[0]).resolve()
    root = (Path(ctx["data"]) / "pdftranslate" / "output").resolve()
    if root != path and root not in path.parents:
        raise ValueError("missing_file")
    if not path.is_file():
        raise ValueError("missing_file")
    return path


def _open_file(path):
    if sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.Popen(["/usr/bin/open", str(path)])


def main():
    instance = None
    process = None
    parent = None
    english = False
    try:
        root = _root().resolve()
        root.mkdir(parents=True, exist_ok=True)
        instance = _Instance(root)
        if not instance.owned:
            return
        prefs = _preferences(root)
        default = root / "data" if getattr(sys, "frozen", False) or os.environ.get("ARXIVREADER_DESKTOP_HOME") else Path(__file__).parent / "data"
        data = Path(prefs.get("data_dir", default)).resolve()
        if prefs.get("data_dir") and not (data / "library.db").is_file():
            raise ValueError("missing_selected_library")
        data.mkdir(parents=True, exist_ok=True)
        english = _read_value(data, "output_language") == "en"
        ctx_mp = mp.get_context("spawn")
        parent, child = ctx_mp.Pipe()
        ticket, cookie = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        process = ctx_mp.Process(target=_backend, args=(child, str(data), ticket, cookie))
        process.start(); child.close()
        if not parent.poll(45):
            raise RuntimeError("startup_timeout")
        ready = parent.recv()
        if not ready.get("ok"):
            raise RuntimeError("startup_failed")
        origin = "http://127.0.0.1:{}".format(ready["port"])
        import webview
        from desktop_native import install_navigation_guard
        webview.settings.update(ALLOW_FILE_URLS=False, ALLOW_DOWNLOADS=False,
                                OPEN_DEVTOOLS_IN_DEBUG=False, SHOW_DEFAULT_MENUS=False)
        gui = install_navigation_guard(origin)
        context = {"root": root, "data": data, "origin": origin}
        bridge = _Bridge(context)
        window = webview.create_window("ArxivReader", origin + "/desktop-start?ticket=" + ticket,
                                       js_api=bridge, width=1380, height=900, min_size=(900, 640))
        context["main"] = bridge._window = window
        shutting_down = threading.Event()

        def shutdown():
            if shutting_down.is_set():
                return
            shutting_down.set()
            try:
                parent.send("stop")
            except (EOFError, OSError):
                pass
            for child_window in list(webview.windows):
                if child_window is not window:
                    child_window.destroy()

        window.events.closing += shutdown
        instance.watch(lambda: (window.restore(), window.show()))
        webview.start(gui=gui, debug=False, private_mode=False, storage_path=str(root / "webview"))
    except Exception as exc:
        missing = str(exc) == "webview2_missing"
        if missing:
            message = ("WebView2 Runtime is required. Open the official download page? Install it, then restart ArxivReader."
                       if english else "需要 WebView2 Runtime。是否打开官方下载页面？安装后请重新启动 ArxivReader。")
        else:
            message = ("ArxivReader could not start. Check library access permissions and available disk space, then reopen the app."
                       if english else "ArxivReader 无法启动。请检查资料库访问权限、资料库位置和可用磁盘空间，然后重新打开应用。")
        _notice(message, english, missing)
    finally:
        if process is not None:
            if parent:
                try:
                    parent.send("stop")
                except (EOFError, OSError):
                    pass
            process.join(12)
            if process.is_alive():
                process.terminate(); process.join(3)
            if process.is_alive():
                process.kill(); process.join(2)
        if parent:
            parent.close()
        if instance is not None:
            instance.close()


if __name__ == "__main__":
    mp.freeze_support()
    main()
