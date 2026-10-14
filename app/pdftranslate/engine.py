"""pdf2zh_next 引擎调用：生成 config.toml、拼命令、跑子进程并解析进度。

迁移自 zotero-pdf2zh server（translate_pdf_next + execute_with_progress），
只保留 ArxivReader 用得上的路径：
  - 服务凭据写进 config.toml 的 <service>_detail 段；
  - CLI 控制语言 / QPS / 输出组合（mono 纯译文、dual 对照、LR/TB 排版、页范围）；
  - POSIX 用 PTY 跑（rich 进度条按行刷新才能被解析），Windows 用管道 + FORCE_TERMINAL；
  - 取消 = 直接杀进程组，不等当前批次结束。
"""

import glob
import logging
import os
import re
import select
import signal
import subprocess
import sys
from typing import Tuple

from harness import net

from . import env as env_mod
from .settings import SERVICES

logger = logging.getLogger(__name__)

# ------------------------------------------------------------ 进度解析（同 zotero-pdf2zh）
# OSC（超链接/标题，\x1b]... 以 BEL 或 \x1b\\ 收尾）必须先于 CSI 洗掉，
# 否则 rich 日志里的 ]8;;file:// 链接体会漏进日志视图。
ANSI_ESCAPE = re.compile(r"\x1B\][^\x07\x1B]*(?:\x07|\x1B\\)"
                         r"|(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]")
# 块末尾疑似未写完的转义序列（CSI/OSC/单字符）：留给下一块拼全后再清洗，
# 否则 4096 字节切块会把 \x1b[ 与 0m 拆进两条日志，界面看到裸 [ / 0m 碎片。
_INCOMPLETE_ANSI_RE = re.compile(r"\x1B(?:\[[0-?]*[ -/]*|\][^\x07\x1B]*|[(@-_][0-?]*)?$")

def _split_incomplete_ansi(text: str) -> Tuple[str, str]:
    """(可安全清洗的正文, 尾部未完成转义残片)。"""
    match = _INCOMPLETE_ANSI_RE.search(text)
    if not match:
        return text, ""
    return text[:match.start()], text[match.start():]
# 主进度：translate 3/17 —— translate 与分数之间不允许出现字母/括号，
# 避开 "Translate Paragraphs (1/1)" 这类子任务标题
MAIN_PROGRESS_RE = re.compile(r"\btranslate\s+[^a-z()\r\n]*?(\d+)/(\d+)\b", re.IGNORECASE)
# 阶段标题："Parse PDF and Create Intermediate Representation (1/6) ..."
STEP_PROGRESS_RE = re.compile(r"(.+?)\(\d+/\d+\)\s+.*?(\d+)/(\d+)")
# tqdm 原生进度条："| 3/17 ["
TQDM_PROGRESS_RE = re.compile(r"\|\s*(\d+)/(\d+)\s+\[")

PAGES_RE = re.compile(r"^\d+(?:-\d+)?$")


class TranslationCancelled(Exception):
    """用户请求取消：进程已被杀。"""


# ------------------------------------------------------------ 子进程「关闭思考」补丁
# 翻译是高频小调用，思考只拖慢速度、白烧 token。pdf2zh_next 只有写死官方端点的
# DeepSeek 服务暴露 thinking 开关；OpenAICompatible 的 extra_body 是私有字段，
# config.toml 设不进去。引擎 venv 由我们独占，写一份 sitecustomize.py 进去是
# 侵入最小的注入方式（venv 内任何 python 进程启动时自动执行）。
_SITECUSTOMIZE = '''"""ArxivReader 注入：全文翻译默认关闭模型思考。"""


def _arxivr_extra_body(model):
    # 与主程序同一口径：模型名带 deepseek 走 thinking.type，其余走 enable_thinking
    if "deepseek" in (model or "").lower():
        return {"thinking": {"type": "disabled"}}
    return {"enable_thinking": False}


try:
    from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator

    _arxivr_openai_init = OpenAITranslator.__init__

    def _arxivr_openai_patched(self, settings, rate_limiter):
        _arxivr_openai_init(self, settings, rate_limiter)
        if getattr(self, "extra_body", None):
            return  # 引擎自己已设置（如 DeepSeek 服务的 thinking 开关），不覆盖
        extra = _arxivr_extra_body(getattr(self, "model", ""))
        self.extra_body = extra
        self.add_cache_impact_parameters("extra_body", extra)
        self.options["extra_body"] = extra

    OpenAITranslator.__init__ = _arxivr_openai_patched
except Exception:
    pass  # 补丁失败不应影响翻译本体

try:
    from pdf2zh_next.translator.translator_impl.siliconflow import SiliconFlowTranslator

    _arxivr_sf_init = SiliconFlowTranslator.__init__

    def _arxivr_sf_patched(self, settings, rate_limiter):
        _arxivr_sf_init(self, settings, rate_limiter)
        # 硅基流动原生开关：默认不发参数（由服务端决定），强制发 enable_thinking=false
        self.enable_thinking = False
        self.send_enable_thinking_param = True
        self.add_cache_impact_parameters("enable_thinking", False)
        self.add_cache_impact_parameters("send_enable_thinking_param", True)

    SiliconFlowTranslator.__init__ = _arxivr_sf_patched
except Exception:
    pass  # 同上
'''

_purelib_cache = ""


def _engine_purelib() -> str:
    """引擎 venv 的 site-packages 路径（问 venv 里的解释器，跨平台）。"""
    global _purelib_cache
    if _purelib_cache:
        return _purelib_cache
    try:
        out = subprocess.run(
            [env_mod.venv_python(), "-c",
             "import sysconfig; print(sysconfig.get_path('purelib'))"],
            capture_output=True, text=True, timeout=30)
        lines = (out.stdout or "").strip().splitlines()
        _purelib_cache = lines[-1].strip() if lines else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取引擎 site-packages 路径失败：%s", exc)
    return _purelib_cache


def _ensure_runtime_patch() -> None:
    """把 sitecustomize.py 写进引擎 venv（内容一致则跳过）；失败不阻断翻译。"""
    purelib = _engine_purelib()
    if not purelib:
        return
    target = os.path.join(purelib, "sitecustomize.py")
    try:
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8") as fh:
                if fh.read() == _SITECUSTOMIZE:
                    return
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(_SITECUSTOMIZE)
    except OSError as exc:
        logger.warning("写入引擎补丁失败：%s", exc)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"{}"'.format(text)


def build_config_toml(path: str, service: str, creds: dict, options: dict) -> None:
    """生成 pdf2zh_next 的最小 config.toml（缺失键走 pdf2zh_next 自带默认）。"""
    spec = SERVICES[service]
    lines = [
        "# Generated by ArxivReader; overwritten on every translation run.",
        "report_interval = 0.2",
        "{} = true".format(service),
        "",
        "[translation]",
        "lang_in = " + _toml_value(options["lang_in"]),
        "lang_out = " + _toml_value(options["lang_out"]),
        "qps = " + _toml_value(int(options.get("qps") or 4)),
        "min_text_length = 5",
        "",
        "[pdf]",
        "watermark_output_mode = \"no_watermark\"",
        "no_dual = " + _toml_value(bool(options.get("no_dual"))),
        "no_mono = " + _toml_value(bool(options.get("no_mono"))),
        "dual_translate_first = " + _toml_value(bool(options.get("dual_translate_first"))),
        "translate_table_text = " + _toml_value(bool(options.get("translate_table_text"))),
        "ocr_workaround = " + _toml_value(bool(options.get("ocr_workaround"))),
        "enhance_compatibility = " + _toml_value(bool(options.get("enhance_compatibility"))),
        "use_alternating_pages_dual = " + _toml_value(options.get("dual_mode") == "TB"),
        "",
        "[{}_detail]".format(service),
        "translate_engine_type = " + _toml_value(spec["engine_type"]),
    ]
    if spec.get("model_field") and creds.get("model"):
        lines.append("{} = {}".format(spec["model_field"], _toml_value(creds["model"])))
    if spec.get("base_url_field") and creds.get("base_url"):
        lines.append("{} = {}".format(spec["base_url_field"], _toml_value(creds["base_url"])))
    if spec.get("api_key_field") and creds.get("api_key"):
        lines.append("{} = {}".format(spec["api_key_field"], _toml_value(creds["api_key"])))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def build_command(input_path: str, service: str, config_path: str,
                  out_dir: str, options: dict) -> list:
    """与 zotero-pdf2zh translate_pdf_next 同一组 CLI 参数。"""
    cmd = [
        env_mod.engine_bin(),
        input_path,
        "--" + service,
        "--qps", str(int(options.get("qps") or 4)),
        "--output", out_dir,
        "--lang-in", options["lang_in"],
        "--lang-out", options["lang_out"],
        "--config-file", config_path,
        "--watermark-output-mode", "no_watermark",
    ]
    if options.get("no_dual"):
        cmd.append("--no-dual")
    if options.get("no_mono"):
        cmd.append("--no-mono")
    if options.get("dual_mode") == "TB":
        cmd.append("--use-alternating-pages-dual")
    if options.get("dual_translate_first"):
        cmd.append("--dual-translate-first")
    pages = (options.get("pages") or "").strip()
    if pages and PAGES_RE.match(pages):
        cmd.extend(["--pages", pages])
    if options.get("translate_table_text"):
        cmd.append("--translate-table-text")
    if options.get("ocr_workaround"):
        cmd.append("--ocr-workaround")
    if options.get("enhance_compatibility"):
        cmd.append("--enhance-compatibility")
    return cmd


def find_outputs(out_dir: str) -> dict:
    """在输出目录里找 mono / dual 产物（pdf2zh_next 命名带语言与水印标记）。"""
    result = {"mono_path": "", "dual_path": ""}
    for pattern, key in (("*.mono.pdf", "mono_path"), ("*.dual.pdf", "dual_path")):
        candidates = sorted(glob.glob(os.path.join(out_dir, "**", pattern), recursive=True),
                            key=os.path.getmtime, reverse=True)
        if candidates:
            result[key] = candidates[0]
    return result


def _sanitize_no_proxy(value: str) -> str:
    """去掉 no_proxy 里的 IPv6 条目（[::1] / ::1）。

    babeldoc 的资源下载器按逗号切分后把每项当 host:port 解析，
    带冒号的条目会直接报 "Invalid port: ':1]'" 让 warmup 崩溃。
    """
    kept = [item.strip() for item in (value or "").split(",")
            if item.strip() and ":" not in item.strip()]
    return ",".join(kept)


def _private_host_bypass_hosts(base_url: str) -> list:
    """翻译端点是内网/本机地址时代理必然到不了，返回需要加进 no_proxy 的主机名。

    公网地址或解析失败返回空列表（不改变现有代理行为）。判定逻辑见 harness.net。
    """
    host = net.host_of(base_url)
    if host and net.is_private_base_url(base_url):
        return [host]
    return []


def _child_env(extra_no_proxy=None) -> dict:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    # 让 rich/tqdm 在非 tty 下也按终端模式输出，进度行才可解析
    env["FORCE_COLOR"] = "1"
    env["FORCE_TERMINAL"] = "1"
    env.pop("NO_COLOR", None)
    env["TERM"] = "xterm-256color"
    # 宽终端：rich 进度行不截断，"63/100" 这类数字完整输出才可解析；
    # 太窄会显示成 "63/…"，太宽又可能让 rich 折行重复打印。
    env["COLUMNS"] = "180"
    env["LINES"] = "24"
    for key in ("no_proxy", "NO_PROXY"):
        if env.get(key):
            cleaned = _sanitize_no_proxy(env[key])
            if cleaned != env[key]:
                logger.info("子进程 %s 已剔除 IPv6 条目（babeldoc 解析限制）", key)
            env[key] = cleaned
    if extra_no_proxy:
        for key in ("no_proxy", "NO_PROXY"):
            hosts = [h.strip() for h in env.get(key, "").split(",") if h.strip()]
            added = [h for h in extra_no_proxy if h not in hosts]
            if added:
                env[key] = ",".join(hosts + added)
                logger.info("翻译端点 %s 为内网/本机地址，子进程 %s 已加入 no_proxy 绕过代理",
                            added, key)
    return env


def _parse_chunk(text: str, progress_cb, log_cb) -> None:
    clean = ANSI_ESCAPE.sub("", text)
    for line in clean.splitlines():
        if line.strip():
            log_cb(line.rstrip()[-500:])
    # rich 的 Live 显示每帧整屏重绘，一个块里含有全部阶段行：
    # 一律取最后一个匹配（最新状态），而不是 search 到的第一行。
    matches = MAIN_PROGRESS_RE.findall(clean)
    if matches:
        curr, total = int(matches[-1][0]), int(matches[-1][1])
        if total > 0:
            progress_cb(min(99, int(curr / total * 100)),
                        "translate {}/{}".format(curr, total))
        return
    matches = TQDM_PROGRESS_RE.findall(clean)
    if matches:
        curr, total = int(matches[-1][0]), int(matches[-1][1])
        if total > 0:
            progress_cb(min(99, int(curr / total * 100)),
                        "translate {}/{}".format(curr, total))
        return
    # 未开始的阶段显示 0/0，过滤掉，取最后一个真正有进度的阶段名
    steps = [m for m in STEP_PROGRESS_RE.findall(clean) if int(m[2]) > 0]
    if steps:
        progress_cb(None, steps[-1][0].strip()[:120])


def _terminate(proc: subprocess.Popen) -> None:
    """取消：整组杀掉，避免 pdf2zh_next 内部 worker 残留。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run_pty(cmd: list, cancel_event, progress_cb, log_cb, child_env: dict) -> int:
    """POSIX：PTY 里跑，逐块读输出解析进度；cancel_event 置位即杀。"""
    import fcntl
    import pty
    import struct
    import termios

    master_fd, slave_fd = pty.openpty()
    try:
        winsize = struct.pack("HHHH", 24, 180, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
    except Exception:  # noqa: BLE001
        pass
    proc = subprocess.Popen(cmd, stdout=slave_fd, stderr=slave_fd,
                            env=child_env, bufsize=0, close_fds=True,
                            start_new_session=True)
    os.close(slave_fd)
    leftover = b""
    ansi_tail = ""
    try:
        while True:
            if cancel_event.is_set():
                _terminate(proc)
                raise TranslationCancelled()
            readable, _, _ = select.select([master_fd], [], [], 0.2)
            if master_fd in readable:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                buf = leftover + data
                try:
                    text, leftover = buf.decode("utf-8"), b""
                except UnicodeDecodeError as exc:
                    text = buf[:exc.start].decode("utf-8", errors="replace")
                    leftover = buf[exc.start:]
                text, ansi_tail = _split_incomplete_ansi(ansi_tail + text)
                if text:
                    _parse_chunk(text, progress_cb, log_cb)
            if proc.poll() is not None:
                # 收尾：把残余输出读完再退出
                try:
                    fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
                    fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
                    while True:
                        data = os.read(master_fd, 4096)
                        if not data:
                            break
                        text, ansi_tail = _split_incomplete_ansi(
                            ansi_tail + data.decode("utf-8", errors="replace"))
                        if text:
                            _parse_chunk(text, progress_cb, log_cb)
                    if ansi_tail:
                        _parse_chunk(ansi_tail, progress_cb, log_cb)
                except Exception:  # noqa: BLE001
                    pass
                break
        return proc.wait()
    finally:
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            _terminate(proc)


def _run_pipe(cmd: list, cancel_event, progress_cb, log_cb, child_env: dict) -> int:
    """Windows：管道 + FORCE_TERMINAL；逐行读，取消时整棵树杀掉。"""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=child_env, text=True, encoding="utf-8",
                            errors="replace", bufsize=1,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.stdout is not None
    try:
        while True:
            if cancel_event.is_set():
                _terminate(proc)
                raise TranslationCancelled()
            line = proc.stdout.readline()
            if line:
                _parse_chunk(line, progress_cb, log_cb)
            elif proc.poll() is not None:
                break
            else:
                cancel_event.wait(0.2)
        rest = proc.stdout.read() or ""
        if rest:
            _parse_chunk(rest, progress_cb, log_cb)
        return proc.wait()
    finally:
        if proc.poll() is None:
            _terminate(proc)


def run_translation(input_path: str, service: str, creds: dict, options: dict,
                    work_dir: str, cancel_event, progress_cb, log_cb) -> dict:
    """执行一次翻译；返回 {"mono_path": ..., "dual_path": ...}（不存在的项为空串）。

    progress_cb(percent:int|None, message:str)：percent 为 None 表示只更新阶段文案。
    失败抛 RuntimeError（带日志尾部），取消抛 TranslationCancelled。
    """
    if service not in SERVICES:
        raise ValueError("不支持的翻译服务：{}".format(service))
    if not os.path.isfile(env_mod.engine_bin()):
        raise RuntimeError("pdf2zh_next 尚未安装，请先在设置或翻译弹窗中安装翻译引擎")
    _ensure_runtime_patch()
    os.makedirs(work_dir, exist_ok=True)
    config_path = os.path.join(work_dir, "config.toml")
    build_config_toml(config_path, service, creds, options)
    cmd = build_command(input_path, service, config_path, work_dir, options)
    # 日志里脱敏：命令本身不含 key（key 在 config.toml 里）
    logger.info("pdf2zh_next 启动：%s", " ".join(cmd))
    log_cb("$ pdf2zh_next " + os.path.basename(input_path))
    runner = _run_pipe if sys.platform == "win32" else _run_pty
    child_env = _child_env(_private_host_bypass_hosts(creds.get("base_url", "")))
    code = runner(cmd, cancel_event, progress_cb, log_cb, child_env)
    if code != 0:
        raise RuntimeError("pdf2zh_next 执行失败（退出码 {}）".format(code))
    outputs = find_outputs(work_dir)
    if not outputs["mono_path"] and not outputs["dual_path"]:
        raise RuntimeError("翻译结束但未生成任何 PDF，请查看日志")
    progress_cb(100, "done")
    return outputs
