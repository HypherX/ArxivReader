"""生成可直接分发的源码 ZIP：白名单目录、排除数据与密钥、并扫描疑似密钥。"""
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.build_desktop import VERSION

INCLUDE_FILES = (
    "ArxivReader.spec", "README.md", "README.en.md", "THIRD_PARTY_NOTICES.txt",
    "desktop.py", "desktop_native.py", "requirements.txt", "requirements-build.txt",
    ".gitignore",
)
INCLUDE_TREES = (".github", "app", "assets", "harness", "knowledge", "static", "tools")
EXCLUDE_NAMES = {".DS_Store", "config.py", "config.example.py"}
EXCLUDE_PARTS = {"__pycache__", ".git", "data", "dist", "node_modules", ".build-venv"}
SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9]{16,}"),
    re.compile(rb"(?i)api[_-]?key\s*[:=]\s*[\"'][A-Za-z0-9_\-]{20,}"),
)
TEXT_SUFFIXES = {".py", ".js", ".md", ".txt", ".json", ".html", ".css", ".yml", ".svg", ".spec", ".gitignore"}


def iter_entries():
    for name in INCLUDE_FILES:
        path = ROOT / name
        if path.is_file():
            yield path
    for tree in INCLUDE_TREES:
        for path in sorted((ROOT / tree).rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if relative.name in EXCLUDE_NAMES or EXCLUDE_PARTS.intersection(relative.parts):
                continue
            if relative.suffix.lower() in (".pyc", ".pyo") or relative.name.endswith((".orig", ".rej")):
                continue
            yield path


def main():
    files = sorted(set(iter_entries()))
    for path in files:
        relative = path.relative_to(ROOT)
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        if path.stat().st_size > 2_000_000:
            continue
        data = path.read_bytes()
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            raise SystemExit("疑似密钥，已停止打包：" + str(relative))
    dst = ROOT.parent / "交付目录" / "ArxivReader-{}-source.zip".format(VERSION)
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, "paper_reader/" + path.relative_to(ROOT).as_posix())
    with zipfile.ZipFile(dst) as archive:
        names = archive.namelist()
        if len(names) != len(files):
            raise SystemExit("源码 ZIP 条目数不一致")
        for name in names:
            parts = Path(name).parts
            if "data" in parts or parts[-1] in EXCLUDE_NAMES or "__pycache__" in parts:
                raise SystemExit("源码 ZIP 包含禁止分发的路径：" + name)
    print("源码 ZIP：{}（{} 个文件）".format(dst, len(files)))


if __name__ == "__main__":
    main()
