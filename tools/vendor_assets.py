"""固定版本渲染资源打包；仅维护时联网，运行应用无需 npm 或 CDN。"""
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
    "katex": ("0.16.22", {"dist/katex.min.js": "katex.min.js", "dist/katex.min.css": "katex.min.css", "LICENSE": "LICENSE"}),
}


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
                target = files.get(relative)
                if name == "katex" and relative.startswith("dist/fonts/") and relative.endswith((".woff2", ".woff", ".ttf")):
                    target = "fonts/" + Path(relative).name
                if target and member.isfile():
                    data = tar.extractfile(member).read()
                    path = ROOT / name / target
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    hashes[target] = hashlib.sha256(data).hexdigest()
        if not set(files.values()) <= set(hashes):
            raise RuntimeError("资源或许可证缺失：" + name)
        manifest[name] = {"version": version, "source": info["dist"]["tarball"],
                          "integrity": info["dist"]["integrity"], "sha256": hashes}
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("已校验并打包 {} 个渲染库".format(len(manifest)))


if __name__ == "__main__":
    main()
