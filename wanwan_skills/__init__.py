"""wanwan_skills 插件入口：声明插件并导出工具模块。"""

from gsuid_core.sv import Plugins

Plugins(
    name="wanwan_skills",
    allow_empty_prefix=True,
)

# 导入即注册 @ai_tools / @on_core_start。放在 Plugins() 之后是有意为之：
# 先声明插件元数据，再触发各模块的注册副作用，勿移动到文件顶部。
from . import tasks as _tasks  # noqa: E402, F401
from .tools import diary as _diary  # noqa: E402, F401
from .tools import media as _media  # noqa: E402, F401
from .tools import phone as _phone  # noqa: E402, F401
