"""pdf2zh_next 运行环境管理（迁移自 zotero-pdf2zh 的 venv 方案）。

pdf2zh_next 依赖很重（onnxruntime / 版面分析模型等），不进主程序依赖，
而是按需装进独立虚拟环境：``<资料目录>/pdftranslate/venv``。

安装方式优先级（与 zotero-pdf2zh 的 env_tool=auto 一致）：
  1. uv（PATH 或常见安装位置里有就用，最快，且能自动准备 Python 3.12）
  2. 当前解释器 python -m venv + pip（源码运行模式）
  3. 桌面打包模式找系统 Python——必须做版本预检：桌面应用 PATH 很干净，
     which("python3") 命中的往往是 /usr/bin/python3（macOS 自带 3.9），
     直接拿去装会在 pip 解析时报一堆看不懂的错。
"""

import glob
import logging
import os
import re
import shutil
import subprocess
import sys
import threading

from harness import config

logger = logging.getLogger(__name__)

PACKAGE_SPEC = "pdf2zh_next>=2.9.0,<3.0.0"
# bitstring 5.0 移除了 babeldoc 仍在用的 BitStream，必须钉在 4.x（与 zotero-pdf2zh 一致）
EXTRA_PACKAGES = ["pypdf", "bitstring<5"]
PYTHON_VERSION = "3.12"
# pdf2zh_next 的 requires-python：>=3.10,<3.14（3.14 太新，/usr/bin/python3 的 3.9 太旧）
MIN_PY = (3, 10)
MAX_PY = (3, 14)
# 国内镜像（pip / uv 通用），可在设置中开启
MIRROR_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"

# 桌面应用从 Finder 启动时 PATH 只有 /usr/bin:/bin 等，Homebrew / 官方安装器
# 装的东西都不在上面，要主动探这些位置。
_EXTRA_BIN_DIRS = [
    "/opt/homebrew/bin", "/usr/local/bin",
    os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.cargo/bin"),
]

_LOCK = threading.Lock()
_STATE = {
    "installing": False,
    "log": [],          # 最近安装日志（尾部，供前端轮询）
    "error": "",
}
_MAX_LOG = 200


def base_dir() -> str:
    path = os.path.join(config.get_base_dir(), "pdftranslate")
    os.makedirs(path, exist_ok=True)
    return path


def venv_dir() -> str:
    return os.path.join(base_dir(), "venv")


def output_dir() -> str:
    path = os.path.join(base_dir(), "output")
    os.makedirs(path, exist_ok=True)
    return path


def _bin_dir(root: str) -> str:
    return os.path.join(root, "Scripts" if sys.platform == "win32" else "bin")


def engine_bin(root: str = None) -> str:
    root = root or venv_dir()
    name = "pdf2zh_next.exe" if sys.platform == "win32" else "pdf2zh_next"
    return os.path.join(_bin_dir(root), name)


def venv_python(root: str = None) -> str:
    root = root or venv_dir()
    name = "python.exe" if sys.platform == "win32" else "python"
    return os.path.join(_bin_dir(root), name)


def _append_log(text: str) -> None:
    with _LOCK:
        for line in str(text).splitlines():
            line = line.rstrip()
            if line:
                _STATE["log"].append(line[-500:])
        del _STATE["log"][:max(0, len(_STATE["log"]) - _MAX_LOG)]


def _set_state(**kw) -> None:
    with _LOCK:
        _STATE.update(kw)


def status() -> dict:
    """引擎状态：是否已安装、版本、是否正在安装、安装日志尾部。"""
    bin_path = engine_bin()
    installed = os.path.isfile(bin_path)
    version = ""
    if installed:
        try:
            out = subprocess.run(
                [venv_python(), "-c",
                 "import importlib.metadata; print(importlib.metadata.version('pdf2zh_next'))"],
                capture_output=True, text=True, timeout=60)
            version = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 pdf2zh_next 版本失败：%s", exc)
    with _LOCK:
        return {
            "installed": installed,
            "version": version,
            "installing": _STATE["installing"],
            "log": list(_STATE["log"][-40:]),
            "error": _STATE["error"],
            "path": venv_dir(),
        }


def _run_step(cmd: list, env: dict = None) -> None:
    """跑一步安装命令，输出并入日志；失败抛 RuntimeError 带日志尾部。"""
    _append_log("$ " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env=env or os.environ.copy(),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        _append_log(line)
    code = proc.wait()
    if code != 0:
        tail = "\n".join(_STATE["log"][-8:])
        raise RuntimeError("安装命令失败（退出码 {}）：\n{}".format(code, tail))


def _install_with_uv(uv: str, root: str, mirror: bool) -> None:
    env = os.environ.copy()
    if mirror:
        env["UV_INDEX_URL"] = MIRROR_INDEX
    _run_step([uv, "venv", "--python", PYTHON_VERSION, root], env)
    _run_step([uv, "pip", "install", "--python", venv_python(root),
               PACKAGE_SPEC] + EXTRA_PACKAGES, env)


def _install_with_python(executable: str, root: str, mirror: bool) -> None:
    _run_step([executable, "-m", "venv", root])
    pip_cmd = [venv_python(root), "-m", "pip", "install"]
    if mirror:
        pip_cmd += ["-i", MIRROR_INDEX]
    _run_step(pip_cmd + [PACKAGE_SPEC] + EXTRA_PACKAGES)


def _which_extended(name: str):
    """PATH 找不到时，再探桌面应用 PATH 之外的常见安装位置。"""
    found = shutil.which(name)
    if found:
        return found
    for directory in _EXTRA_BIN_DIRS:
        path = os.path.join(directory, name)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


def _python_version(executable: str):
    """读候选解释器的 (major, minor)；跑不起来返回 None。"""
    try:
        out = subprocess.run(
            [executable, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=15)
        parts = (out.stdout or "").strip().split(".")
        return (int(parts[0]), int(parts[1]))
    except Exception:  # noqa: BLE001
        return None


def _find_system_python() -> str:
    """找版本合格的系统 Python：版本名优先，裸 python3 必须过版本检查。"""
    candidates = []
    for name in ("python3.12", "python3.13", "python3.11", "python3.10", "python3"):
        path = _which_extended(name)
        if path:
            candidates.append(path)
    # python.org 官方安装器的框架目录（Finder 启动时同样不在 PATH 上）
    candidates.extend(sorted(
        glob.glob("/Library/Frameworks/Python.framework/Versions/3.*/bin/python3"),
        reverse=True))
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        version = _python_version(path)
        if version and MIN_PY <= version < MAX_PY:
            return path
        _append_log("跳过 {}（版本 {}，需要 >=3.10 且 <3.14）".format(
            path, "{}.{}".format(*version) if version else "未知"))
    return ""


def _pick_python() -> str:
    """没 uv 时选建 venv 的解释器：源码模式优先当前解释器，否则找系统 Python。"""
    if not getattr(sys, "frozen", False) and MIN_PY <= sys.version_info[:2] < MAX_PY:
        return sys.executable
    found = _find_system_python()
    if not found:
        raise RuntimeError(
            "未找到可用的 Python（pdf2zh_next 需要 >=3.10 且 <3.14）：请安装 uv "
            "（https://docs.astral.sh/uv/）或 Python 3.12（如 brew install python@3.12）后重试")
    return found


def install(use_mirror: bool = False) -> None:
    """同步安装（调用方负责放后台线程）；幂等：装好了就直接返回。"""
    with _LOCK:
        if _STATE["installing"]:
            raise RuntimeError("安装正在进行中")
        _STATE.update(installing=True, log=[], error="")
    try:
        root = venv_dir()
        if os.path.isfile(engine_bin(root)):
            _append_log("pdf2zh_next 已安装，跳过。")
            return
        if os.path.isdir(root):
            shutil.rmtree(root, ignore_errors=True)
        uv = _which_extended("uv")
        if uv:
            _append_log("使用 uv 创建环境…")
            _install_with_uv(uv, root, use_mirror)
        else:
            executable = _pick_python()
            _append_log("使用 {} 创建环境…".format(executable))
            _install_with_python(executable, root, use_mirror)
        if not os.path.isfile(engine_bin(root)):
            raise RuntimeError("安装完成但未找到 pdf2zh_next 可执行文件")
        _append_log("✓ pdf2zh_next 安装完成")
        logger.info("pdf2zh_next 环境就绪：%s", root)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pdf2zh_next 安装失败：%s", exc)
        _set_state(error=str(exc)[:500])
        raise
    finally:
        _set_state(installing=False)


def install_background(use_mirror: bool = False) -> bool:
    """后台安装；已在安装则返回 False。"""
    with _LOCK:
        if _STATE["installing"]:
            return False
    thread = threading.Thread(target=_install_safe, args=(use_mirror,),
                              name="pdftr-install", daemon=True)
    thread.start()
    return True


def _install_safe(use_mirror: bool) -> None:
    try:
        install(use_mirror=use_mirror)
    except Exception:  # noqa: BLE001  错误已进 _STATE["error"]
        pass


def version_of(path: str) -> str:
    """从 --version 输出里提取版本号（宽松匹配）。"""
    match = re.search(r"(\d+\.\d+(?:\.\d+)*)", path or "")
    return match.group(1) if match else ""
