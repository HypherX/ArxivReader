"""应用版本号单一来源。

desktop.py（桌面入口）、app/main.py（后端）与 tools/build_desktop.py（打包）
都从这里取版本，避免发版时多处手工同步漏改。
"""

VERSION = "1.1.4"
