"""wanwan_skills 插件入口：声明插件并导出工具模块。"""

from gsuid_core.sv import Plugins

Plugins(
    name="wanwan_skills",
    allow_empty_prefix=True,
)

# 导入即注册 @ai_tools；顺序按工具域分组，便于阅读
# 导入即登记 @on_core_start（每日日记 cron）
from . import tasks as _tasks  # noqa: F401
from .tools import diary as _diary  # noqa: F401
from .tools import media as _media  # noqa: F401
