"""wanwan_skills 配置：读 data/plugins_configs/wanwan_skills.json。

密钥与登录态只落 data/（不进仓库）。文件缺失时返回 None，
由工具侧回一条可读的未配置提示，不在 import 期抛错。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from gsuid_core.data_store import get_res_path

CONFIG_PATH: Path = get_res_path("plugins_configs") / "wanwan_skills.json"

_DEFAULTS: dict[str, object] = {
    # 目标会话（日记播报对象）。留空 = 任务跳过，便于他人开箱即用
    "ws_bot_id": "",
    "bot_id": "",
    "bot_self_id": "",
    "user_id": "",
    "ark_base_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
    "ark_model": "doubao-seedream-5-0-flash-260915",
    "ark_timeout": 120.0,
    "ark_max_bytes": 150000,
    "mimo_base_url": "https://token-plan-cn.xiaomimimo.com/v1",
    "mimo_tts_model": "mimo-v2.5-tts-voiceclone",
    "mimo_timeout": 180.0,
    "site_base_url": "https://sua17.cn",
    "site_username": "wanwan",
    "site_timeout": 60.0,
    "voice_file": "/gsuid_core/data/voice_src/xilian_30s.mp3",
    "ref_image": "/gsuid_core/data/wanwan/wanwan_ref.jpg",
    "voice_style": (
        "低沉圆钝、慢半拍，嘴硬心软，像泡在深海里说话，"
        "尾音轻轻收住，偶尔带一点嗷呜的语气。"
    ),
}


@dataclass(frozen=True)
class WanwanConfig:
    """插件全部配置项。secret 字段为 API key / 登录口令。"""

    ark_api_key: str
    ark_base_url: str
    ark_model: str
    ark_timeout: float
    ark_max_bytes: int

    mimo_api_key: str
    mimo_base_url: str
    mimo_tts_model: str
    mimo_timeout: float
    voice_file: str
    voice_style: str

    site_base_url: str
    site_username: str
    site_password: str
    site_timeout: float

    ref_image: str

    # 目标会话（日记播报发给谁）
    ws_bot_id: str
    bot_id: str
    bot_self_id: str
    user_id: str

    @property
    def session_id(self) -> str:
        """与 Event.session_id 同格式：{ws}:{bot}:{bot_self}:private:{user}。"""
        ws = self.ws_bot_id or self.bot_id or "0"
        bid = self.bot_id or "0"
        sid = self.bot_self_id or "0"
        uid = self.user_id or "0"
        return f"{ws}:{bid}:{sid}:private:{uid}"

    @property
    def session_ready(self) -> bool:
        """会话标识是否配齐（缺任一则日记任务无法播报）。"""
        return bool(
            self.ws_bot_id and self.bot_id and self.bot_self_id and self.user_id
        )

    @property
    def ark_ready(self) -> bool:
        """生图是否可用。"""
        return bool(self.ark_api_key)

    @property
    def mimo_ready(self) -> bool:
        """配音是否可用（key 与音色样本都在）。"""
        return bool(self.mimo_api_key) and Path(self.voice_file).exists()

    @property
    def site_ready(self) -> bool:
        """日记站点是否可用。"""
        return (
            bool(self.site_base_url)
            and bool(self.site_username)
            and bool(self.site_password)
        )


def _read_password_file(path: str) -> str:
    """读口令文件；路径为空或不存在都返回空串。

    空串必须挡掉：Path("") 会解析成 "."（当前目录），read_text 会炸。
    """
    if not path.strip():
        return ""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    return p.read_text(encoding="utf-8").strip()


def load_config() -> WanwanConfig | None:
    """读配置文件并合并默认值；文件不存在返回 None。"""
    if not CONFIG_PATH.exists():
        return None
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None

    merged: dict[str, object] = dict(_DEFAULTS)
    merged.update({k: v for k, v in raw.items() if isinstance(k, str)})

    def _s(key: str) -> str:
        val = merged.get(key)
        return val if isinstance(val, str) else ""

    def _f(key: str) -> float:
        val = merged.get(key)
        return float(val) if isinstance(val, (int, float)) else 0.0

    def _i(key: str) -> int:
        val = merged.get(key)
        return int(val) if isinstance(val, (int, float)) else 0

    password_file = _s("site_password_file")
    password = _s("site_password") or _read_password_file(password_file)

    return WanwanConfig(
        ark_api_key=_s("ark_api_key"),
        ark_base_url=_s("ark_base_url").rstrip("/"),
        ark_model=_s("ark_model"),
        ark_timeout=_f("ark_timeout") or 120.0,
        ark_max_bytes=_i("ark_max_bytes") or 150000,
        mimo_api_key=_s("mimo_api_key"),
        mimo_base_url=_s("mimo_base_url").rstrip("/"),
        mimo_tts_model=_s("mimo_tts_model"),
        mimo_timeout=_f("mimo_timeout") or 180.0,
        voice_file=_s("voice_file"),
        voice_style=_s("voice_style"),
        site_base_url=_s("site_base_url").rstrip("/"),
        site_username=_s("site_username"),
        site_password=password,
        site_timeout=_f("site_timeout") or 60.0,
        ref_image=_s("ref_image"),
        ws_bot_id=_s("ws_bot_id"),
        bot_id=_s("bot_id"),
        bot_self_id=_s("bot_self_id"),
        user_id=_s("user_id"),
    )
