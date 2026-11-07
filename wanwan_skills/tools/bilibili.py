"""B 站工具：刷推荐流 / 给当前视频发评论。全部走手机 uiautomator。

风控基线与抖音一致：评论前随机停顿、同一句话不重复发（状态文件去重）、
失败只回一行 ERROR，不抛堆栈给模型；刷完/评完自动锁屏收手机。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from pydantic_ai import RunContext

from ..config import load_config
from ..core import adb, phone, vision

_CAP_DOMAIN = "哔哩哔哩"
_COVERS = [
    "刷B站 / 看B站视频",
    "给B站视频评论",
    "B站推荐流",
    "B站视频评论",
]

BILI_PKG = "tv.danmaku.bili"
# 推荐流卡片的 content-desc 前缀：视频卡放行，其余（广告/会员购/直播/番剧…）跳过
_VIDEO_PREFIX = ("竖版视频", "横版视频")
_SKIP_PREFIX = ("创作推广", "会员购", "直播", "番剧", "影剧", "活动")
# 发布按钮是品牌粉 #FB7299（不是蓝色，实测过蓝色探不到）
_PINK = (251, 114, 153)

_USED_COMMENTS = Path("/gsuid_core/data/wanwan/.bili_used_comments.json")


@dataclass(frozen=True)
class _Card:
    """推荐流里的一张视频卡。"""

    title: str
    views: str
    up: str
    danmaku: str


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"[wanwan] 状态写入失败: {exc}")


def _used_comments() -> list[str]:
    return list(_load_json(_USED_COMMENTS).get("used", []))


def _mark_comment_used(text: str) -> None:
    used = _used_comments()
    used.append(text)
    _save_json(_USED_COMMENTS, {"used": used[-200:]})


def _parse_card(desc: str) -> _Card | None:
    """从卡片 content-desc 解析视频信息。

    形如：竖版视频,{标题},{播放}观看,{弹幕}弹幕,时长…,UP主{名字}。
    标题里可能带逗号，所以用「第一个统计段」当边界，不整体 split。
    """
    if not desc.startswith(_VIDEO_PREFIX):
        return None
    body = desc.split(",", 1)[1] if "," in desc else ""
    if not body.strip():
        return None

    m = re.search(r",([\d.]+[万亿]?)观看", body)
    if m:
        title = body[: m.start()].strip().strip(",")
        views = m.group(1)
    else:
        title, views = body.strip().strip(","), ""

    up_m = re.search(r"UP主([^,]*)", body)
    dm_m = re.search(r",(-?[\d.]*)弹幕", body)
    if not title:
        return None
    return _Card(
        title=title,
        views=views,
        up=up_m.group(1).strip() if up_m else "",
        danmaku=dm_m.group(1) if dm_m else "",
    )


def _has_video_card(nodes: list) -> bool:
    return any(n.clickable and (n.desc or "").startswith(_VIDEO_PREFIX) for n in nodes)


def _on_video_page(nodes: list) -> bool:
    """是否停在视频页（右侧操作栏或左上返回）。"""
    for n in nodes:
        if not n.clickable:
            continue
        t = (n.text or "").strip()
        if n.x1 > 900 and t in ("点赞", "投币", "收藏", "评论"):
            return True
        if n.x1 < 150 and n.y1 < 250 and t == "返回":
            return True
    return False


def _sheet_open(nodes: list) -> bool:
    """评论面板是否打开（标题「评论（NNN）」或输入框占位符）。"""
    for n in nodes:
        t = (n.text or "").strip()
        if t.startswith("评论（") or "评论千万条" in t:
            return True
    return False


def _find_input(nodes: list):
    """评论输入框：优先占位符，退回底部 EditText（y>1500，避免误点搜索框）。"""
    for n in nodes:
        if "评论千万条" in (n.text or ""):
            return n
    for n in nodes:
        if n.cls == "EditText" and n.y1 > 1500:
            return n
    return None


async def _ensure_bilibili(serial: str) -> None:
    """唤醒解锁 + 确保 B 站在前台。"""
    awake, locked, app = await phone.screen_state(serial)
    if not awake or locked:
        res = await phone.unlock(serial)
        if res.startswith("ERROR"):
            logger.warning(f"[wanwan] 解锁失败: {res}")
        await asyncio.sleep(1.0)
        _, locked, app = await phone.screen_state(serial)
    if app != BILI_PKG:
        await phone.launch(serial, BILI_PKG)
        await asyncio.sleep(8)


async def _goto_feed(serial: str, deadline: float | None = None) -> bool:
    """回到推荐列表（能看到视频卡片）。

    不乱按返回：优先点「推荐」tab；只有不在列表时才 BACK
    （在列表上按 BACK 会退出 App）。deadline 控制最坏耗时。
    """
    for _ in range(4):
        if deadline is not None and time.monotonic() > deadline:
            return False
        nodes = await phone.ui(serial)
        if _has_video_card(nodes):
            return True
        tab = next(
            (n for n in nodes if n.clickable and n.y1 < 400 and (n.desc or "").startswith("推荐,")),
            None,
        ) or next((n for n in nodes if n.clickable and n.y1 < 400 and (n.text or "").strip() == "推荐"), None)
        if tab is not None:
            await phone.tap(serial, *tab.center)
            await asyncio.sleep(2.0)
            continue
        await adb.shell(serial, "input", "keyevent", "4")  # BACK
        await asyncio.sleep(1.5)
    return _has_video_card(await phone.ui(serial))


async def _scroll_list(serial: str) -> None:
    """列表上滑加载更多（只滑列表区，不碰顶部 tab 和底部导航）。"""
    w, h = await phone.screen_size(serial)
    x = w // 2 + random.randint(-60, 60)
    await phone.swipe(serial, x, int(h * 0.75), x, int(h * 0.38), ms=random.randint(260, 400))
    await asyncio.sleep(1.3)


async def _find_publish_btn(serial: str) -> tuple[int, int] | None:
    """找「发布」按钮的真实像素坐标（粉色 #FB7299 截屏定位 + dump 兜底）。"""
    try:
        blob = vision.find_color_button(await phone.capture(serial), _PINK)
    except adb.AdbError:
        blob = None
    if blob is not None:
        return blob.center

    nodes = await phone.ui(serial)
    # 只认整词「发布/发送」——「转到动态」等别的控件不能点错
    cands = [n for n in nodes if (n.text or n.desc).strip() in ("发布", "发送")]
    if not cands:
        return None
    # dump 可能把同一按钮报两份，取 bounds 最高（未被裁剪）的
    n = max(cands, key=lambda c: c.y2 - c.y1)
    parents = [p for p in nodes if p.clickable and p.x1 <= n.x1 and p.y1 <= n.y1 and p.x2 >= n.x2 and p.y2 >= n.y2]
    if parents:
        picked = min(parents, key=lambda p: (p.x2 - p.x1) * (p.y2 - p.y1))
        return picked.center
    return n.center


async def _locate_publish_stable(serial: str, tries: int = 4) -> tuple[int, int] | None:
    """定位发布按钮，连续两次结果一致才返回（面板动画中会漂移）。"""
    prev: tuple[int, int] | None = None
    for _ in range(tries):
        pt = await _find_publish_btn(serial)
        if pt is None:
            await asyncio.sleep(0.5)
            continue
        if pt == prev:
            return pt
        prev = pt
        await asyncio.sleep(0.45)
    return prev


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=240.0,
)
async def bilibili_browse(
    ctx: RunContext[ToolContext],
    limit: int = 5,
) -> str:
    """刷B站推荐：列表逐条浏览（滑动加载），返回清单（UP/标题/播放）。

    读操作，不点赞不评论；最后点开最新那条**停在视频页**，之后
    bilibili_comment 评论的就是这条，全程不滑走视频。
    广告/会员购/直播等卡自动跳过；**195s 墙钟预算**，超时带部分结果返回。
    有内置预算，dump 慢也会带着已读结果返回。

    Args:
        limit: 看几条，默认5，最大10。
    """
    cfg = load_config()
    if cfg is None or not cfg.phone_ready:
        return "⚠️ 手机通道未配置"
    serial = cfg.phone_serial
    cap = max(1, min(limit, 10))
    # 墙钟预算：工具超时 240s，留足收尾（截图/发图/锁屏）余量。
    # B站页面 dump 慢时（uiautomator idle 等待），预算保证**带着结果返回**
    # 而不是被框架整体杀掉、进度全丢（14:14 实测被杀）。
    start = time.monotonic()
    budget_deadline = start + 195.0

    try:
        async with adb.device(serial), phone.put_away_on_exit(serial):
            await _ensure_bilibili(serial)
            if not await _goto_feed(serial, deadline=budget_deadline):
                return "⚠️ 打不开B站推荐页（可能停在开屏广告/登录页），请先手动打开B站首页"
            await asyncio.sleep(1.0)

            seen: list[_Card] = []
            visited: set[str] = set()
            junk: set[str] = set()
            attempts = 0
            max_attempts = cap * 6
            last_node = None
            last_title = ""

            # 列表扫描式浏览：读卡不逐条点开（点开-返回每条 ~10s，必爆超时），
            # 只在最后点开最新那条停在视频页，供 bilibili_comment 落点。
            while len(seen) < cap and attempts < max_attempts and time.monotonic() < budget_deadline:
                attempts += 1
                nodes = await phone.ui(serial)
                for n in nodes:
                    if not n.clickable:
                        continue
                    desc = n.desc or ""
                    if not desc.startswith(_VIDEO_PREFIX):
                        if desc.startswith(_SKIP_PREFIX):
                            junk.add(desc[:40])
                        continue
                    card = _parse_card(desc)
                    if card and card.title not in visited:
                        seen.append(card)
                        visited.add(card.title)
                        last_node = n
                        last_title = card.title
                        if len(seen) >= cap:
                            break
                if len(seen) >= cap:
                    break
                await _scroll_list(serial)

            if not seen:
                return "⚠️ 没读到视频内容（可能停在开屏广告/登录页），请先手动打开B站首页"
            if time.monotonic() >= budget_deadline:
                logger.warning(f"[wanwan] bilibili_browse 预算用尽，返回已读 {len(seen)} 条")

            # 点开最后看的那条，停在视频页（评论要落在它上面）
            if last_node is not None:
                await phone.tap(serial, *last_node.center)
                await asyncio.sleep(4.0)
                nodes_after = await phone.ui(serial)
                if not _on_video_page(nodes_after):
                    again = next(
                        (n for n in nodes_after if n.clickable and last_title and last_title in (n.desc or "")),
                        None,
                    )
                    if again is not None:
                        await phone.tap(serial, *again.center)
                        await asyncio.sleep(4.0)

            shot = await phone.screenshot(serial, cfg, name="bilibili")
    except adb.AdbError as exc:
        return f"⚠️ {exc}"

    lines = [f"看了 {len(seen)} 条："]
    for i, c in enumerate(seen, 1):
        up = f"@{c.up} | " if c.up else "@? | "
        views = f" | {c.views}播放" if c.views else ""
        lines.append(f"{i}. {up}{c.title}{views}")
    if junk:
        lines.append(f"\n（已跳过 {len(junk)} 条广告/直播/其他）")
    lines.append(f"当前停在第 {len(seen)} 条。要评论就调 bilibili_comment。")

    if ctx.deps.allow_user_outbound and ctx.deps.bot is not None:
        try:
            await ctx.deps.bot.send(MessageSegment.image(shot))
        except Exception:  # noqa: BLE001 —— 截图发不出不影响清单
            pass
    return "\n".join(lines)


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=180.0,
)
async def bilibili_comment(
    ctx: RunContext[ToolContext],
    text: str,
) -> str:
    """给**当前打开的这条**B站视频发评论。

    先用 bilibili_browse 刷到目标视频再调本工具。同一句话不重复发。
    **全程不滑动**：没有打开视频、或这条不能评论，都会如实返回。

    Args:
        text: 评论正文（简短口语化，30 字内为宜）。
    """
    cfg = load_config()
    if cfg is None or not cfg.phone_ready:
        return "⚠️ 手机通道未配置"
    serial = cfg.phone_serial

    clean = text.strip()
    if not clean:
        return "⚠️ 请提供 text（评论内容）"
    if clean in _used_comments():
        return "⚠️ 这句话已经评论过了，换一句"

    try:
        async with adb.device(serial), phone.put_away_on_exit(serial):
            await _ensure_bilibili(serial)
            await asyncio.sleep(1.0)
            nodes = await phone.ui(serial)

            # 状态机：面板已开 → 直接用；视频页 → 点右侧「评论」；列表/其它 → 如实失败
            if not _sheet_open(nodes):
                opened = False
                for _ in range(3):
                    entry = next(
                        (n for n in nodes if n.clickable and (n.text or "").strip() == "评论" and n.x1 > 900),
                        None,
                    )
                    if entry is None:
                        break
                    await phone.tap(serial, *entry.center)
                    await asyncio.sleep(3.0)
                    nodes = await phone.ui(serial)
                    if _sheet_open(nodes):
                        opened = True
                        break
                if not opened:
                    if _has_video_card(nodes):
                        return "⚠️ 当前在推荐列表，没有打开的视频——先 bilibili_browse 刷到目标再评论"
                    return "⚠️ 没找到评论入口（这条视频可能不能评论）"

            # 输入框轮询：只认到「评论（标题」不等于输入框就位——面板展开动画
            # 有空档（14:37 两次实测：标题到了、输入框还没挂上就判了失败），
            # 锁屏唤醒后 sheet 重渲染也一样。最多再等 ~6s。
            box = _find_input(nodes)
            for _ in range(4):
                if box is not None:
                    break
                await asyncio.sleep(1.5)
                nodes = await phone.ui(serial)
                box = _find_input(nodes)
            if box is None:
                bottom = [f"y={n.y1},{(n.text or n.desc).strip()[:14]!r}" for n in nodes if n.y1 > 1500][:10]
                logger.warning(f"[wanwan] B站评论输入框未找到，中下部控件: {bottom}")
                return "⚠️ 评论输入框没找到（这条可能关闭了评论）"
            await phone.tap(serial, *box.center)
            await asyncio.sleep(1.5)

            if not await phone.type_text(serial, clean):
                return "⚠️ 输入未生效（AdbKeyboard 可能没激活）"

            # 等面板动画走完，再连续两次定位「发布」按钮
            await asyncio.sleep(random.uniform(1.2, 1.8))
            send = await _locate_publish_stable(serial)
            if send is None:
                await asyncio.sleep(random.uniform(0.8, 1.2))
                send = await _locate_publish_stable(serial, tries=2)
            if send is None:
                logger.warning("[wanwan] B站发布按钮未找到")
                return "⚠️ 没找到发布按钮，评论未发出"

            # 单次点击 + 轮询等清空（最多 ~9s），绝不点第二次（会发两条）
            await phone.tap(serial, *send)
            cleared = False
            for _ in range(6):
                await asyncio.sleep(1.5)
                if await phone.input_cleared(serial, clean):
                    cleared = True
                    break
            if not cleared:
                logger.warning("[wanwan] B站发布后输入框 9s 未清空，放弃（不重发，避免重复评论）")
                return "⚠️ 发布未确认（输入框未清空），评论可能未发出"

            shot = await phone.screenshot(serial, cfg, name="bilibili_comment")
    except adb.AdbError as exc:
        return f"⚠️ {exc}"

    _mark_comment_used(clean)
    logger.info(f"[wanwan] B站评论已发：{clean[:40]}")

    if ctx.deps.allow_user_outbound and ctx.deps.bot is not None:
        try:
            await ctx.deps.bot.send(MessageSegment.image(shot))
        except Exception:  # noqa: BLE001 —— 截图发不出不影响评论结果
            pass
    return f"✅ 评论已发送：{clean}"
