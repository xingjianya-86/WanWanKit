"""火山方舟 Seedream 文生图客户端。

契约：POST {base}/images/generations，Bearer 鉴权，
body = {model, prompt, size, response_format, watermark, image?}。
响应 data[0] 给 url 或 b64_json。
"""

from __future__ import annotations

import base64
import io
from typing import Any

import httpx

from ..config import WanwanConfig

_MAX_SIDE = 1280
_QUALITY_STEPS = (82, 76, 70, 64, 58, 52, 46, 40, 34, 28)


class ArkError(RuntimeError):
    """方舟调用失败。携带对用户可读的原因。"""


def _headers(cfg: WanwanConfig) -> dict[str, str]:
    if not cfg.ark_api_key:
        raise ArkError("未配置 ark_api_key，无法调用火山方舟")
    return {
        "Authorization": f"Bearer {cfg.ark_api_key}",
        "Content-Type": "application/json",
    }


def _compress_jpeg(data: bytes, max_bytes: int) -> bytes:
    """重编码成 JPEG（剥 EXIF/C2PA 元数据）并压到 max_bytes 内。

    先按长边缩到 1280，再逐级降 quality；仍超标则继续缩尺寸。
    打不开原图时原样返回，交给上层决定。
    """
    from PIL import Image as PILImage

    try:
        img = PILImage.open(io.BytesIO(data))
        img.load()
    except OSError:
        return data

    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    if max(img.size) > _MAX_SIDE:
        ratio = _MAX_SIDE / float(max(img.size))
        size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
        img = img.resize(size, PILImage.LANCZOS)

    def _encode(im: Any, quality: int) -> bytes:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True, progressive=True)
        return buf.getvalue()

    out = _encode(img, _QUALITY_STEPS[0])
    if len(out) <= max_bytes:
        return out

    for quality in _QUALITY_STEPS[1:]:
        out = _encode(img, quality)
        if len(out) <= max_bytes:
            return out

    cur = img
    for _ in range(4):
        if max(cur.size) <= 512:
            break
        cur = cur.resize(
            (max(1, cur.width * 3 // 4), max(1, cur.height * 3 // 4)),
            PILImage.LANCZOS,
        )
        for quality in (70, 55, 40):
            out = _encode(cur, quality)
            if len(out) <= max_bytes:
                return out
    return out


async def _post_generations(
    cfg: WanwanConfig, payload: dict[str, Any]
) -> dict[str, Any]:
    url = f"{cfg.ark_base_url}/images/generations"
    # 国内服务直连，不吃容器 HTTP_PROXY
    async with httpx.AsyncClient(timeout=cfg.ark_timeout, trust_env=False) as client:
        resp = await client.post(url, headers=_headers(cfg), json=payload)
    if resp.status_code >= 400:
        raise ArkError(f"方舟返回 {resp.status_code}: {resp.text[:300]}")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise ArkError(f"方舟响应不是合法 JSON: {resp.text[:200]}") from exc
    if not isinstance(doc, dict):
        raise ArkError("方舟响应结构异常")
    return doc


async def _download(url: str, timeout: float) -> tuple[bytes, str]:
    if url.startswith("data:"):
        head, _, b64 = url.partition(",")
        mime = head[5:].split(";", 1)[0] or "image/jpeg"
        return base64.b64decode(b64), mime
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=True, trust_env=False
    ) as client:
        resp = await client.get(url)
    if resp.status_code >= 400:
        raise ArkError(f"下载生成图片失败 {resp.status_code}")
    mime = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return resp.content, mime


async def generate_image(
    cfg: WanwanConfig,
    prompt: str,
    *,
    size: str = "1K",
    watermark: bool = False,
    ref_images: list[str] | None = None,
) -> bytes:
    """文生图（可带参考图），返回压缩后的 JPEG 字节。"""
    clean = prompt.strip()
    if not clean:
        raise ArkError("prompt 不能为空")

    payload: dict[str, Any] = {
        "model": cfg.ark_model,
        "prompt": clean,
        "size": size or "1K",
        "response_format": "url",
        "watermark": bool(watermark),
    }
    if ref_images:
        payload["image"] = list(ref_images)[:10]

    doc = await _post_generations(cfg, payload)
    items = doc.get("data")
    if not isinstance(items, list) or not items:
        raise ArkError("方舟响应里没有图片")

    first = items[0]
    if not isinstance(first, dict):
        raise ArkError("方舟响应条目结构异常")
    img_url = first.get("url")
    if isinstance(img_url, str) and img_url:
        raw, _mime = await _download(img_url, cfg.ark_timeout)
    else:
        b64 = first.get("b64_json")
        if not isinstance(b64, str) or not b64:
            raise ArkError("方舟响应缺少图片 URL 与 base64")
        raw = base64.b64decode(b64)

    return _compress_jpeg(raw, cfg.ark_max_bytes)


def image_to_ref(image_bytes: bytes) -> str:
    """把参考图字节转成方舟可接受的 data URI。"""
    return "data:image/jpeg;base64," + base64.b64encode(image_bytes).decode("ascii")
