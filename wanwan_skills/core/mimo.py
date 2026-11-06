"""小米 MiMo 音色克隆 TTS 客户端。

契约：POST {base}/chat/completions，Bearer 鉴权，
body = {model, messages, audio:{format, voice}}，voice 为参考音频 data URI。
响应 choices[0].message.audio.data 是 base64 音频。
"""

from __future__ import annotations

import base64
from pathlib import Path

import httpx

from ..config import WanwanConfig


class TtsError(RuntimeError):
    """配音失败。携带对用户可读的原因。"""


def _voice_data_uri(cfg: WanwanConfig) -> str:
    voice_path = Path(cfg.voice_file)
    if not voice_path.exists():
        raise TtsError(f"音色样本不存在：{cfg.voice_file}")
    try:
        raw = voice_path.read_bytes()
    except OSError as exc:
        raise TtsError(f"音色样本读取失败：{exc}") from exc
    if not raw:
        raise TtsError("音色样本为空文件")
    return "data:audio/mpeg;base64," + base64.b64encode(raw).decode("ascii")


async def synthesize(cfg: WanwanConfig, text: str) -> bytes:
    """把 text 配成音色克隆语音，返回音频字节（wav）。"""
    clean = text.strip()
    if not clean:
        raise TtsError("配音文本不能为空")
    if not cfg.mimo_api_key:
        raise TtsError("未配置 mimo_api_key，无法配音")

    payload = {
        "model": cfg.mimo_tts_model,
        "messages": [
            {"role": "user", "content": cfg.voice_style},
            {"role": "assistant", "content": clean},
        ],
        "audio": {"format": "wav", "voice": _voice_data_uri(cfg)},
    }
    headers = {
        "Authorization": f"Bearer {cfg.mimo_api_key}",
        "Content-Type": "application/json",
    }
    url = f"{cfg.mimo_base_url}/chat/completions"

    async with httpx.AsyncClient(timeout=cfg.mimo_timeout, trust_env=False) as client:
        resp = await client.post(url, headers=headers, json=payload)
    if resp.status_code >= 400:
        raise TtsError(f"MiMo TTS {resp.status_code}: {resp.text[:300]}")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise TtsError(f"MiMo 响应不是合法 JSON: {resp.text[:200]}") from exc

    choices = doc.get("choices") if isinstance(doc, dict) else None
    message = (
        choices[0].get("message") if isinstance(choices, list) and choices else None
    )
    audio = message.get("audio") if isinstance(message, dict) else None
    data = audio.get("data") if isinstance(audio, dict) else None
    if not isinstance(data, str) or not data:
        raise TtsError("MiMo 响应缺 audio.data")
    return base64.b64decode(data)
