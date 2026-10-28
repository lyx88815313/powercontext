"""pytest 引导：把业务包 ``src`` 加入导入路径。

这样测试可以在没有安装业务包的情况下直接运行：
``python -m pytest minimax/demo/tests``
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
