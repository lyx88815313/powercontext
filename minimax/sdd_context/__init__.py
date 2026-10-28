"""SDD 上下文模块（MiniMax × OceanBase 竞赛作品）。

模块职责：

1. 把规格、代码、测试导入 seekdb（工程资料存储与检索）。
2. 依据开发任务检索适用资料，组装带来源的开发上下文。
3. 用 MiniMax 完成代码修改，并用测试验证。
4. 把任务状态（已完成/待办/验证结果）写入 PowerContext，重启后读回复用。
"""

from .config import Settings, load_settings

__all__ = ["Settings", "load_settings"]
__version__ = "1.0.0"
