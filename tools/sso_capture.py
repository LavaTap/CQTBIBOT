"""SSO 抓包工具快捷入口（从 tools/ 根目录启动）。

用法:
    cd tools
    python sso_capture.py

等价于:
    cd ..
    python -m sso.sso_tool
"""
from __future__ import annotations

import os
import sys

# 确保项目根目录在 PYTHONPATH 中，以便导入 sso 包
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from sso.sso_tool import main  # noqa: E402

if __name__ == "__main__":
    main()
