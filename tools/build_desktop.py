"""使用当前平台的 Python 3.12 构建本地桌面包，不上传或发布。"""
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.1.1"


def resources(output):
    import pymupdf
    iconset = output / "ArxivReader.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    with pymupdf.open(ROOT / "assets/icon.svg") as doc:
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = size * scale
                pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(pixels / doc[0].rect.width, pixels / doc[0].rect.height), alpha=True)
                name = "icon_{}x{}{}.png".format(size, size, "@2x" if scale == 2 else "")
                pix.save(iconset / name)
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(256 / doc[0].rect.width, 256 / doc[0].rect.height), alpha=True)
        image = pix.tobytes("png")
        (output / "ArxivReader.ico").write_bytes(
            struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(image), 22) + image)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/iconutil", "-c", "icns", str(iconset), "-o", str(output / "ArxivReader.icns")], check=True)
    licenses = output / "third_party_licenses"
    licenses.mkdir(exist_ok=True)
    excluded = {"pytest", "iniconfig", "pluggy", "esprima", "beautifulsoup4", "soupsieve", "uv"}
    versions = {}
    license_files = []
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if name.lower() in excluded:
            continue
        versions[name] = distribution.version
        folder = licenses / name
        folder.mkdir(exist_ok=True)
        meta = folder / "METADATA.txt"
        meta.write_text(distribution.read_text("METADATA") or name, encoding="utf-8")
        license_files.append(str(meta.relative_to(output)))
        for file in distribution.files or []:
            lower = file.name.lower()
            if re.fullmatch(r"(?:licen[sc]e|copying|notice|authors)(?:[._-](?:txt|md|rst|html|apache[\w.-]*|mit|bsd[\w.-]*|lesser|gpl[\w.-]*|lgpl[\w.-]*))?", lower):
                source = Path(distribution.locate_file(file))
                if source.is_file():
                    destination = folder / str(file).replace("/", "__").replace("\\", "__")
                    shutil.copy2(source, destination)
                    license_files.append(str(destination.relative_to(output)))
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, licenses / "Python-LICENSE.txt")
        license_files.append("third_party_licenses/Python-LICENSE.txt")
    (output / "license-manifest.json").write_text(json.dumps(license_files), encoding="utf-8")
    info = {"version": VERSION, "platform": sys.platform, "architecture": platform.machine(),
            "python": platform.python_version(), "signed_for_distribution": False, "dependencies": versions}
    (output / "build-info.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")


def main(out=None):
    generated = ROOT / ".build-generated"
    generated.mkdir(exist_ok=True)
    resources(generated)
    dist = Path(out).resolve() if out else ROOT / "dist"
    dist.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(dist),
                    "--workpath", str(ROOT / ".build-work"), str(ROOT / "ArxivReader.spec")], cwd=ROOT, check=True)
    architecture = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    os_name = "macOS" if sys.platform == "darwin" else "Windows"
    stem = "ArxivReader-{}-{}-{}".format(VERSION, os_name, architecture)
    archive = dist / (stem + ".zip")
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(dist / "ArxivReader.app"), str(archive)], check=True)
    else:
        shutil.make_archive(str(dist / stem), "zip", dist, "ArxivReader")
    with zipfile.ZipFile(archive) as packaged:
        forbidden = {"library.db", "config.example.py", "conftest.py"}
        for name in packaged.namelist():
            parts = Path(name).parts
            if any(part in forbidden or part == "tests" or part.startswith("reading-preview-") for part in parts):
                raise RuntimeError("打包内容包含禁止分发的文件：" + name)
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    (dist / (stem + ".sha256")).write_text(digest.hexdigest() + "  " + archive.name + "\n", encoding="utf-8")
    shutil.copy2(generated / "build-info.json", dist / (stem + ".json"))
    print("已生成本机未签名构建：", archive)


if __name__ == "__main__":
    argv = sys.argv[1:]
    out_dir = None
    if "--out" in argv:
        index = argv.index("--out")
        if index + 1 >= len(argv):
            raise SystemExit("--out 需要一个目录参数，例如：--out /path/to/releases")
        out_dir = argv[index + 1]
    elif "-h" in argv or "--help" in argv:
        raise SystemExit("用法：python tools/build_desktop.py [--out 输出目录]\n"
                         "  在目标平台（macOS / Windows）的 Python 3.12 环境运行；\n"
                         "  不带 --out 时输出到项目下的 dist/。")
    main(out_dir)
