"""WanWanKit 配置：读 data/plugins_configs/wanwan_skills.json（文件名保持不变，与插件名解耦）。

密钥与登录态只落 data/（不进仓库、不进 git、不进文档示例）：
- 优先读独立密钥文件（ark_api_key_file / mimo_api_key_file /
  site_password_file / bili_token_path），配置里只放**路径**
- 配置 JSON 里若还有明文密钥，首次读取会**自动迁移到独立文件**并清掉
  明文（迁移后配置可安全分享）
- 平台固定端点（api.bilibili.com / creator.douyin.com 等）不开放自定义
  （协议与风控绑定）；服务型地址（ark/mimo/site 的 base_url）全部可自定义

文件缺失时返回 None，由工具侧回一条可读的未配置提示，不在 import 期抛错。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from gsuid_core.data_store import get_res_path
from gsuid_core.logger import logger

CONFIG_PATH: Path = get_res_path("plugins_configs") / "wanwan_skills.json"

_DEFAULTS: dict[str, object] = {
    # 目标会话（日记播报对象）。留空 = 任务跳过，便于他人开箱即用
    "ws_bot_id": "",
    "bot_id": "",
    "bot_self_id": "",
    "user_id": "",
    # 手机通道：adb over TCP（ZeroTier）。留空则手机类工具返回未配置
    "phone_serial": "",
    "phone_ime": "com.sohu.inputmethod.sogou.xiaomi/.SogouIME",
    "phone_shot_dir": "/gsuid_core/data/wanwan/phone_shots",
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
    # B站登录态文件（浏览器 Cookie 一行：SESSDATA=..; bili_jct=..）。发动态/投稿用
    "bili_token_path": "/gsuid_core/data/wanwan/.bili_token",
    "voice_style": ("低沉圆钝、慢半拍，嘴硬心软，像泡在深海里说话，尾音轻轻收住，偶尔带一点嗷呜的语气。"),
    # 密钥独立文件（一行一个 key，600 权限；配置里只放路径，明文自动迁移）
    "ark_api_key_file": "/gsuid_core/data/wanwan/.ark_api_key",
    "mimo_api_key_file": "/gsuid_core/data/wanwan/.mimo_api_key",
    "site_password_file": "/gsuid_core/data/wanwan/.site_password",
    # 环境路径与超时（可自定义；留空则回退内置默认）
    "dy_python_path": "/gsuid_core/data/douyin_venv/bin/python",
    "dy_driver_path": "/gsuid_core/data/mcp/dy_driver.py",
    "video_out_dir": "/gsuid_core/data/video_out",
    "ffmpeg_timeout": 600.0,
    "http_timeout": 15.0,
    "http_max_body": 1500,
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

    # 手机通道（adb over TCP）
    phone_serial: str
    phone_ime: str
    phone_shot_dir: str

    # B站 API 登录态文件（给默认值：手工构造 WanwanConfig 的旧测试/调用不破坏）
    bili_token_path: str = ""

    # 密钥独立文件路径（配置里只存路径；值在 load_config 时已解析进上列字段）
    ark_api_key_file: str = ""
    mimo_api_key_file: str = ""
    site_password_file: str = ""

    # 环境路径与超时（可自定义）
    dy_python_path: str = ""
    dy_driver_path: str = ""
    video_out_dir: str = ""
    ffmpeg_timeout: float = 600.0
    http_timeout: float = 15.0
    http_max_body: int = 1500

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
        return bool(self.ws_bot_id and self.bot_id and self.bot_self_id and self.user_id)

    @property
    def phone_ready(self) -> bool:
        """手机通道是否已配置 serial。"""
        return bool(self.phone_serial)

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
        return bool(self.site_base_url) and bool(self.site_username) and bool(self.site_password)

    @property
    def bili_ready(self) -> bool:
        """B站 API 登录态文件是否就位。"""
        try:
            p = Path(self.bili_token_path)
            return bool(self.bili_token_path) and p.is_file() and p.stat().st_size > 0
        except OSError:
            return False


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


def _resolve_secret(inline: str, file_path: str, inline_key: str = "") -> str:
    """密钥解析：独立文件优先，回退配置内联值。

    发现内联明文且独立文件缺失时，**自动迁移**到独立文件（600 权限）——
    迁移成功后由 load_config 把配置里的明文清掉，配置从此可安全分享。
    inline_key 为该密钥在配置 JSON 里的键名（用于迁移后清除明文）。
    """
    file_val = _read_password_file(file_path)
    if file_val:
        return file_val
    if not inline.strip() or not file_path.strip():
        return inline
    try:
        p = Path(file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(inline.strip(), encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
        if inline_key:
            _MIGRATED_KEYS.append(inline_key)
        logger.warning(f"[wanwan] 密钥已迁移到独立文件 {file_path}（配置内明文将被清除）")
    except OSError as exc:
        logger.warning(f"[wanwan] 密钥迁移失败（本次仍用配置内值）: {exc}")
    return inline


# 本次 load 中发生迁移的内联键名——load_config 末尾统一从配置文件清除
_MIGRATED_KEYS: list[str] = []


def _flush_migrations(raw: dict) -> None:
    """把已迁移到独立文件的内联密钥从配置 JSON 里清空（尽力而为）。"""
    if not _MIGRATED_KEYS:
        return
    for key in _MIGRATED_KEYS:
        if key in raw:
            raw[key] = ""
    _MIGRATED_KEYS.clear()
    try:
        CONFIG_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("[wanwan] 配置内明文密钥已清除（值已迁入独立文件）")
    except OSError as exc:
        logger.warning(f"[wanwan] 配置回写失败（明文暂留，下次再迁）: {exc}")


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

    ark_key = _resolve_secret(_s("ark_api_key"), _s("ark_api_key_file"), "ark_api_key")
    mimo_key = _resolve_secret(_s("mimo_api_key"), _s("mimo_api_key_file"), "mimo_api_key")
    password = _resolve_secret(_s("site_password"), _s("site_password_file"), "site_password")
    _flush_migrations(raw)

    return WanwanConfig(
        ark_api_key=ark_key,
        ark_base_url=_s("ark_base_url").rstrip("/"),
        ark_model=_s("ark_model"),
        ark_timeout=_f("ark_timeout") or 120.0,
        ark_max_bytes=_i("ark_max_bytes") or 150000,
        mimo_api_key=mimo_key,
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
        phone_serial=_s("phone_serial"),
        phone_ime=_s("phone_ime"),
        phone_shot_dir=_s("phone_shot_dir"),
        bili_token_path=_s("bili_token_path"),
        ark_api_key_file=_s("ark_api_key_file"),
        mimo_api_key_file=_s("mimo_api_key_file"),
        site_password_file=_s("site_password_file"),
        dy_python_path=_s("dy_python_path"),
        dy_driver_path=_s("dy_driver_path"),
        video_out_dir=_s("video_out_dir"),
        ffmpeg_timeout=_f("ffmpeg_timeout") or 600.0,
        http_timeout=_f("http_timeout") or 15.0,
        http_max_body=_i("http_max_body") or 1500,
    )
