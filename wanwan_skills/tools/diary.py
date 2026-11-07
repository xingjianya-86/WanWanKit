"""日记工具：写本地日记并发布到 sua17.cn 手帐。

写/读是纯函数（不碰 RunContext），便于单测；工具只做参数校验与编排。
"""

from __future__ import annotations

import hashlib
import json
import traceback
from datetime import datetime
from pathlib import Path

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.data_store import get_res_path
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from pydantic_ai import RunContext

from ..config import load_config
from ..core import ark, site

_CAP_DOMAIN = "晚晚日记"
_COVERS = [
    "写日记 / 记录今天",
    "把手帐发布到网站",
    "回顾某天的日记",
    "日记配图上传",
]

_DIARY_DIR = get_res_path(["wanwan", "diary"])
_PUBLISHED_FILE = get_res_path(["wanwan", ".diary_published.json"])


def _content_sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _load_published() -> dict:
    try:
        data = json.loads(_PUBLISHED_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_published(state: dict) -> None:
    try:
        _PUBLISHED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PUBLISHED_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # 状态写不进去不致命，最多下次再发一遍
        logger.warning(f"[wanwan] 发布状态写入失败: {exc}")


def _diary_path(date_str: str, base: Path | None = None) -> Path:
    """日记文件路径：{base}/YYYY-MM-DD.md。"""
    root = base if base is not None else _DIARY_DIR
    return root / f"{date_str}.md"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def write_entry(
    content: str,
    date_str: str,
    *,
    mood: str = "",
    base: Path | None = None,
) -> Path:
    """把一条日记追加到指定日期文件，返回文件路径。content 已由调用方校验非空。"""
    root = base if base is not None else _DIARY_DIR
    root.mkdir(parents=True, exist_ok=True)
    path = _diary_path(date_str, base)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"## {stamp}"
    clean_mood = mood.strip()
    if clean_mood:
        header += f" · {clean_mood}"

    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8").strip()

    entry = f"{header}\n\n{content.strip()}"
    body = f"{existing}\n\n{entry}" if existing else entry
    path.write_text(body, encoding="utf-8")
    return path


def read_entries(
    date_str: str = "",
    limit: int = 1,
    *,
    base: Path | None = None,
) -> str:
    """读日记：date_str 非空读该天，否则按文件名倒序读最近 N 篇。"""
    root = base if base is not None else _DIARY_DIR

    if date_str:
        path = _diary_path(date_str, base)
        if not path.exists():
            return f"⚠️ {date_str} 没有日记"
        return path.read_text(encoding="utf-8")

    if not root.exists():
        return "⚠️ 还没有任何日记"

    files = sorted(root.glob("*.md"), reverse=True)
    if not files:
        return "⚠️ 还没有任何日记"
    cap = max(1, min(limit, 7))

    out: list[str] = []
    for f in files[:cap]:
        out.append(f"# {f.stem}\n\n{f.read_text(encoding='utf-8')}")
    return "\n\n---\n\n".join(out)


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=60.0,
)
async def diary_write(
    ctx: RunContext[ToolContext],
    content: str,
    date: str = "",
    mood: str = "",
) -> str:
    """写一篇日记到本地（data/wanwan/diary/），同一天多次写会追加。

    用第一人称、晚晚的口吻记录今天发生的事、心情和想法。
    写完如果主人要发到网站，再调 diary_publish。

    Args:
        content: 日记正文，markdown 可用。
        date: 日期 YYYY-MM-DD，默认今天。
        mood: 心情标签，如 "开心"、"有点累"、"想主人了"。
    """
    clean = content.strip()
    if not clean:
        return "⚠️ 请提供 content（日记正文）"

    date_str = date.strip() or _today()
    path = write_entry(clean, date_str, mood=mood)
    logger.info(f"[wanwan] 日记写入 {path.name}（{len(clean)} 字）")
    return f"✅ 日记已写入 {path.name}"


async def _do_publish(
    cfg: object,
    date_str: str,
    title: str = "",
    image_url: str = "",
) -> str:
    """发布核心：读当天日记 → 交给站点。参数名必须与 site.publish_note 对齐。"""
    # TEMP-TRACE：抓「非今天的日期被反复发布」的调用来源（排查完移除）
    if date_str != _today():
        logger.warning(
            f"[wanwan] TEMP-TRACE diary_publish 异常日期 {date_str}"
            f"（今天 {_today()}）调用栈:\n" + "".join(traceback.format_stack()[:-1])
        )

    path = _diary_path(date_str)
    if not path.exists():
        return f"⚠️ {date_str} 没有日记，先写再发"

    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return f"⚠️ {date_str} 日记为空"

    # 同一天 + 同内容只发一次：站点按 title 派生 slug，重复 POST 会堆出
    # 2026-11-06-2/-3… 一堆重复帖（2026-11-07 就被发过两次）
    sha = _content_sha(content)
    state = _load_published()
    prev = state.get(date_str)
    if isinstance(prev, dict) and prev.get("sha") == sha:
        logger.info(f"[wanwan] 日记 {date_str} 已发布过，跳过重复发布")
        return f"✅ 这篇已经发布过了，没有重复发：\n{prev.get('result', '')}"

    # 站点按 title 派生 slug 且剥掉非 ASCII，所以 title 必须带英文前缀，
    # 否则 URL 只剩日期（如 /blog/posts/2026-11-07/）
    clean_title = title.strip() or f"Diary {date_str} 晚晚的日记"
    try:
        # 注意：站点侧参数名是 image，不是 image_url（写错会 TypeError）
        result = await site.publish_note(cfg, clean_title, content, image=image_url)  # type: ignore[arg-type]
    except site.SiteError as exc:
        return f"⚠️ 发布失败：{exc}"

    state[date_str] = {"sha": sha, "result": result, "at": datetime.now().isoformat(timespec="seconds")}
    _save_published(state)
    logger.info(f"[wanwan] 日记发布 {date_str}")
    return f"✅ {result}"


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=180.0,
)
async def diary_illustrate(
    ctx: RunContext[ToolContext],
    scene: str,
    style: str = "",
) -> str:
    """为今天的日记生成一张配图，转存到手帐站点并返回站点 URL。

    发布日记前调用，把返回的 URL 传给 diary_publish 的 image_url。
    图同时会发到当前对话给主人看。画面用晚晚的世界观（海底/沙坑/房间/星空）。

    Args:
        scene: 画面场景，如 "黄昏的海底沙坑"、"窗边的计划本与星星帘子"。
        style: 可选风格补充，如 "暖光"、"俯拍"、"远景"。
    """
    cfg = load_config()
    if cfg is None:
        return "⚠️ wanwan_skills 未配置（缺 plugins_configs/wanwan_skills.json）"
    if not cfg.ark_ready:
        return "⚠️ 未配置 ark_api_key，无法生图"
    if not cfg.site_ready:
        return "⚠️ 站点未配置，配图无法转存"
    if not cfg.ref_image or not Path(cfg.ref_image).exists():
        return f"⚠️ 参考图不存在：{cfg.ref_image}"

    clean_scene = scene.strip()
    if not clean_scene:
        return "⚠️ 请提供 scene（画面场景）"

    try:
        ref_bytes = Path(cfg.ref_image).read_bytes()
    except OSError as exc:
        return f"⚠️ 参考图读取失败：{exc}"

    prompt = f"动漫插画风格，与参考图同一位画师的笔触与配色：深蓝色调、柔和光影。画面：{clean_scene}。"
    if style.strip():
        prompt += f"氛围：{style.strip()}。"

    try:
        img = await ark.generate_image(cfg, prompt, ref_images=[ark.image_to_ref(ref_bytes)])
        url = await site.upload_image_bytes(cfg, img, "image/jpeg")
    except (ark.ArkError, site.SiteError) as exc:
        return f"⚠️ 配图生成失败：{exc}"

    # 同时发到对话，让主人先看到
    if ctx.deps.allow_user_outbound and ctx.deps.bot is not None:
        try:
            await ctx.deps.bot.send(MessageSegment.image(img))
        except Exception as exc:  # noqa: BLE001 —— 配图已上传成功，发送失败不该中断流程
            logger.warning(f"[wanwan] 配图发送失败（已上传站点）: {exc}")

    logger.info(f"[wanwan] 日记配图已生成: {url}")
    return f"✅ 配图已生成并转存：{url}"


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=180.0,
)
async def diary_publish(
    ctx: RunContext[ToolContext],
    date: str = "",
    title: str = "",
    image_url: str = "",
) -> str:
    """把某天的日记发布到 sua17.cn 手帐（公开）。

    发布前确认主人要公开。配图必须先用 diary_upload_image 转存，
    拿到站点 URL 再传 image_url，不要直接用外部图床链接。

    Args:
        date: 要发布的日期 YYYY-MM-DD，默认今天。
        title: 标题，默认「Diary YYYY-MM-DD 晚晚的日记」——
            必须含英文，站点按 title 派生 URL slug 且剥掉中文。
        image_url: 站点配图 URL（可选）。
    """
    cfg = load_config()
    if cfg is None:
        return "⚠️ wanwan_skills 未配置（缺 plugins_configs/wanwan_skills.json）"
    if not cfg.site_ready:
        return "⚠️ 站点未配置（缺 site_username / site_password）"

    return await _do_publish(cfg, date.strip() or _today(), title, image_url)


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=120.0,
)
async def diary_upload_image(
    ctx: RunContext[ToolContext],
    source_url: str,
) -> str:
    """把外链图片转存到手帐站点，返回可用于配图的站点 URL。

    发布日记要配图时先调这个，再把返回的 URL 传给 diary_publish。

    Args:
        source_url: 图片直链（生图工具返回的 URL）。
    """
    cfg = load_config()
    if cfg is None:
        return "⚠️ wanwan_skills 未配置"
    if not cfg.site_ready:
        return "⚠️ 站点未配置"

    try:
        url = await site.upload_image(cfg, source_url)
    except site.SiteError as exc:
        return f"⚠️ 配图上传失败：{exc}"
    return f"✅ 配图已转存：{url}"


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    timeout=30.0,
)
async def diary_read(
    ctx: RunContext[ToolContext],
    date: str = "",
    limit: int = 1,
) -> str:
    """读取本地日记内容。

    Args:
        date: 日期 YYYY-MM-DD，默认最近一篇。
        limit: 读几篇，默认 1，最多 7。
    """
    return read_entries(date.strip(), limit)
