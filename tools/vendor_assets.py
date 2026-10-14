"""固定版本渲染资源打包；仅维护时联网，运行应用无需 npm 或 CDN。

files 的值有两种写法：
  - "包内相对路径": "目标文件名"          —— 单文件映射；
  - "包内目录/": "目标目录/"              —— 整目录搬运（保留原文件名），
    用于 PDF.js 的 standard_fonts / cmaps 这类按需加载的散件资源。
"""
import base64
import hashlib
import io
import json
from pathlib import Path
import tarfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / "static" / "vendor"
PACKAGES = {
    "markdown-it": ("14.1.0", {"dist/markdown-it.min.js": "markdown-it.min.js", "LICENSE": "LICENSE"}),
    "dompurify": ("3.2.7", {"dist/purify.min.js": "purify.min.js", "LICENSE": "LICENSE"}),
    "katex": ("0.16.22", {"dist/katex.min.js": "katex.min.js", "dist/katex.min.css": "katex.min.css",
                         "dist/fonts/": "fonts/", "LICENSE": "LICENSE"}),
    # legacy 构建：转译过且自带 core-js，兼容桌面壳里较旧的 WebView 内核。
    # 后缀改成 .js 是为了让静态服务给出 text/javascript —— 动态 import 与 module worker
    # 都按 MIME 判定，而部分 Python 版本的 mimetypes 不认识 .mjs。
    "pdfjs-dist": ("4.10.38", {"legacy/build/pdf.min.mjs": "pdf.min.js",
                               "legacy/build/pdf.worker.min.mjs": "pdf.worker.min.js",
                               "standard_fonts/": "standard_fonts/", "cmaps/": "cmaps/",
                               "LICENSE": "LICENSE"}),
}


def _target(relative: str, files: dict) -> str:
    """单个文件的目标名：先查精确映射，再落进"整目录"映射。"""
    if relative in files:
        return files[relative]
    for source, destination in files.items():
        if source.endswith("/") and relative.startswith(source) and destination.endswith("/"):
            return destination + Path(relative).name
    return ""


def main():
    manifest = {}
    for name, (version, files) in PACKAGES.items():
        with urlopen("https://registry.npmjs.org/{}/{}".format(name, version), timeout=60) as response:
            info = json.load(response)
        with urlopen(info["dist"]["tarball"], timeout=60) as response:
            archive = response.read()
        algorithm, expected = info["dist"]["integrity"].split("-", 1)
        actual = base64.b64encode(hashlib.new(algorithm, archive).digest()).decode()
        if actual != expected:
            raise RuntimeError("资源完整性校验失败：" + name)
        hashes = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar.getmembers():
                relative = member.name.removeprefix("package/")
                target = _target(relative, files)
                if target and member.isfile():
                    data = tar.extractfile(member).read()
                    path = ROOT / name / target
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    hashes[target] = hashlib.sha256(data).hexdigest()
        if not {v for v in files.values() if not v.endswith("/")} <= set(hashes):
            raise RuntimeError("资源或许可证缺失：" + name)
        for source, destination in files.items():
            if destination.endswith("/") and not any(key.startswith(destination) for key in hashes):
                raise RuntimeError("目录资源缺失：{} {}".format(name, source))
        manifest[name] = {"version": version, "source": info["dist"]["tarball"],
                          "integrity": info["dist"]["integrity"], "sha256": hashes}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("已校验并打包 {} 个渲染库".format(len(manifest)))


if __name__ == "__main__":
    main()
