"""pytest 根配置：确保项目根目录在 sys.path 上，测试可直接 import app 包。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
