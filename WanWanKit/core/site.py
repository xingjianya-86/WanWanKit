"""sua17.cn 手帐站点客户端（日记发布）。

契约：
  POST /api/auth/login {username, password} -> Set-Cookie: auth_token
  POST /api/phantasi/notes {title, content_md, topic, image} -> {success, id, link}
  POST /api/media (multipart) -> {url}
带 Bearer=auth_token，401 时清缓存重登一次。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from ..config import WanwanConfig

_TITLE_MAX = 80
_CONTENT_MAX = 20000
_TOKEN_FILE = Path("/gsuid_core/data/wanwan/.site_token")


class SiteError(RuntimeError):
    """站点调用失败。携带对用户可读的原因。"""


def _load_token() -> str:
    if not _TOKEN_FILE.exists():
        return ""
    return _TOKEN_FILE.read_text(encoding="utf-8").strip()


def _save_token(token: str) -> None:
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        _TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def _extract_auth_token(headers: httpx.Headers) -> str:
    """从 Set-Cookie 里取 auth_token。"""
    for raw in headers.get_list("set-cookie"):
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == "auth_token" and value:
                return value
    return ""


async def _login(cfg: WanwanConfig, client: httpx.AsyncClient) -> str:
    if not cfg.site_username or not cfg.site_password:
        raise SiteError("站点未配置用户名/口令")
    resp = await client.post(
        f"{cfg.site_base_url}/api/auth/login",
        json={"username": cfg.site_username, "password": cfg.site_password},
    )
    if resp.status_code >= 400:
        raise SiteError(f"登录失败 {resp.status_code}: {resp.text[:200]}")
    token = _extract_auth_token(resp.headers)
    if not token:
        raise SiteError("登录成功但没有 auth_token cookie")
    _save_token(token)
    return token


async def _ensure_client(cfg: WanwanConfig) -> tuple[httpx.AsyncClient, str]:
    """建客户端并保证 token 有效（无则登录）。"""
    client = httpx.AsyncClient(timeout=cfg.site_timeout, trust_env=False)
    token = _load_token()
    if not token:
        token = await _login(cfg, client)
    return client, token


async def publish_note(
    cfg: WanwanConfig,
    title: str,
    content_md: str,
    *,
    topic: str = "",
    image: str = "",
) -> str:
    """发布一篇公开手帐，返回「已发布」说明文本（含链接）。"""
    clean_title = title.strip()
    if not clean_title:
        raise SiteError("标题不能为空")
    if len(clean_title) > _TITLE_MAX:
        raise SiteError(f"标题太长（{len(clean_title)} 字），上限 {_TITLE_MAX}")
    if len(content_md) > _CONTENT_MAX:
        raise SiteError(f"正文太长（{len(content_md)} 字），上限 {_CONTENT_MAX}")

    payload: dict[str, str] = {"title": clean_title, "content_md": content_md}
    if topic.strip():
        payload["topic"] = topic.strip()
    if image.strip():
        payload["image"] = image.strip()

    client, token = await _ensure_client(cfg)
    try:
        doc = await _post_notes(cfg, client, token, payload)
        if doc is None:
            # 401：清缓存重登再试一次
            token = await _login(cfg, client)
            doc = await _post_notes(cfg, client, token, payload)
        if doc is None:
            raise SiteError("发布失败（未授权，重登后仍失败）")
    finally:
        await client.aclose()

    if not doc.get("success"):
        raise SiteError(f"站点拒绝了这次发布：{json.dumps(doc, ensure_ascii=False)[:200]}")
    note_id = doc.get("id")
    link = doc.get("link") or f"/journal/articles/{note_id}"
    absolute = link if str(link).startswith("http") else cfg.site_base_url + str(link)
    return f"已发布：《{clean_title}》\nid：{note_id}\n链接：{absolute}"


async def _post_notes(
    cfg: WanwanConfig,
    client: httpx.AsyncClient,
    token: str,
    payload: dict[str, str],
) -> dict[str, Any] | None:
    """发一篇笔记。401 返回 None 让上层重登，其余情况返回解析后的 dict。"""
    resp = await client.post(
        f"{cfg.site_base_url}/api/phantasi/notes",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code == 401:
        return None
    if resp.status_code >= 400:
        raise SiteError(f"发布失败 {resp.status_code}: {resp.text[:200]}")
    try:
        doc = resp.json()
    except ValueError as exc:
        raise SiteError(f"站点返回非 JSON：{resp.text[:200]}") from exc
    if not isinstance(doc, dict):
        raise SiteError("站点响应结构异常")
    return doc


async def upload_image_bytes(cfg: WanwanConfig, data: bytes, mime: str = "image/jpeg") -> str:
    """把图片字节上传到站点，返回可直接用于正文配图的绝对 URL。"""
    if not data:
        raise SiteError("图片数据为空")

    site_client, token = await _ensure_client(cfg)
    try:
        files = {"file": ("wanwan.jpg", data, mime)}
        up = await site_client.post(
            f"{cfg.site_base_url}/api/media",
            files=files,
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        await site_client.aclose()

    if up.status_code >= 400:
        raise SiteError(f"上传配图失败 {up.status_code}: {up.text[:200]}")
    try:
        doc = up.json()
    except ValueError as exc:
        raise SiteError(f"上传响应非 JSON：{up.text[:200]}") from exc
    if not isinstance(doc, dict):
        raise SiteError("上传响应结构异常")

    # 站点返回 {"item": {"url"|"public_path": ...}}，个别情况直接给 url
    item = doc.get("item") if isinstance(doc.get("item"), dict) else {}
    raw_url = item.get("url") or item.get("public_path") or doc.get("url") or ""
    if not isinstance(raw_url, str) or not raw_url:
        raise SiteError(f"上传成功但响应缺图片地址：{up.text[:200]}")
    return raw_url if raw_url.startswith("http") else cfg.site_base_url + raw_url


async def upload_image(cfg: WanwanConfig, source_url: str) -> str:
    """把外链图片转存到站点，返回站点 URL。"""
    clean = source_url.strip()
    if not clean:
        raise SiteError("图片 URL 不能为空")

    async with httpx.AsyncClient(timeout=cfg.site_timeout, follow_redirects=True, trust_env=False) as client:
        img_resp = await client.get(clean)
        if img_resp.status_code >= 400:
            raise SiteError(f"下载配图失败 {img_resp.status_code}")
        data = img_resp.content
        mime = img_resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()

    return await upload_image_bytes(cfg, data, mime)
