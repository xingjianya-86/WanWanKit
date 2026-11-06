"""抖音工具：刷视频 / 评论。全部走手机 uiautomator，不用 PC 浏览器。

风控基线：评论前随机停顿、同一条不重复评论（去重靠状态文件）、
失败只回一行 ERROR，不抛堆栈给模型。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from pydantic_ai import RunContext

from ..config import load_config
from ..core import adb, phone

_CAP_DOMAIN = "抖音"
_COVERS = [
    "刷抖音 / 看抖音视频",
    "给抖音视频评论",
    "回复抖音评论",
    "发抖音作品",
]

DOUYIN_PKG = "com.ss.android.ugc.aweme"
_STATE_FILE = Path("/gsuid_core/data/wanwan/.douyin_state.json")
_USED_COMMENTS = Path("/gsuid_core/data/wanwan/.douyin_used_comments.json")


@dataclass
class _Video:
    """一条抖音视频的可读摘要。"""

    author: str
    desc: str
    likes: str
    comments: str

    def line(self, idx: int) -> str:
        d = self.desc[:60].replace("\n", " ") if self.desc else "(无描述)"
        return f"{idx}. @{self.author or '?'} | {d} | 赞{self.likes} 评{self.comments}"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_video(nodes: list) -> _Video | None:
    """从控件树里抠出当前视频的作者/描述/赞/评。"""
    author = desc = likes = comments = ""
    for n in nodes:
        t = (n.text or n.desc).strip()
        if not t:
            continue
        if not author:
            m = re.match(r"@(.+)", t)
            if m and n.clickable:
                author = m.group(1).strip()
        if not likes:
            m = re.search(r"喜欢([\d.万亿]+)", t)
            if m:
                likes = m.group(1)
        if not comments:
            m = re.search(r"评论([\d.万亿]+)", t)
            if m:
                comments = m.group(1)
        # 描述：长文本且不含按钮词
        if not desc and len(t) > 12 and "按钮" not in t:
            if not re.match(r"^[\d.万亿]+$", t):
                desc = t
    if not (author or desc):
        return None
    return _Video(author=author, desc=desc, likes=likes or "0", comments=comments or "0")


async def _ensure_douyin(serial: str) -> None:
    """确保抖音在前台；不在则启动并等待。"""
    _, _, app = await phone.screen_state(serial)
    if app != DOUYIN_PKG:
        await phone.launch(serial, DOUYIN_PKG)
        await asyncio.sleep(6)


async def _swipe_next(serial: str) -> None:
    """上滑切下一条。"""
    w, h = await phone.screen_size(serial)
    x = w // 2 + random.randint(-40, 40)
    y1 = int(h * 0.72)
    y2 = int(h * 0.32)
    await phone.swipe(serial, x, y1, x, y2, ms=random.randint(220, 380))
    await asyncio.sleep(random.uniform(1.0, 1.8))


def _used_comments() -> list[str]:
    data = _load_json(_USED_COMMENTS)
    return list(data.get("used", []))


def _mark_comment_used(text: str) -> None:
    used = _used_comments()
    used.append(text)
    # 只留最近 200 条，防止无限增长
    _save_json(_USED_COMMENTS, {"used": used[-200:]})


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=180.0,
)
async def douyin_browse(
    ctx: RunContext[ToolContext],
    limit: int = 5,
) -> str:
    """刷抖音：连看几条视频，返回清单（作者/描述/赞/评）。

    读操作，不点赞不评论。看完后 App 停在最后一条，
    后续 douyin_comment 评论的就是这条。

    Args:
        limit: 看几条，默认 5，最大 15。
    """
    cfg = load_config()
    if cfg is None or not cfg.phone_ready:
        return "⚠️ 手机通道未配置"
    serial = cfg.phone_serial
    cap = max(1, min(limit, 15))

    try:
        async with adb.device(serial):
            await _ensure_douyin(serial)
            await asyncio.sleep(1.5)

            seen: list[_Video] = []
            last_raw = ""
            for i in range(cap):
                # 界面切换/加载需要时间：轮询直到读到完整信息（有描述或赞数）
                v = None
                for _ in range(3):
                    nodes = await phone.ui(serial)
                    cand = _extract_video(nodes)
                    if cand and (cand.desc or cand.likes != "0"):
                        v = cand
                        break
                    await asyncio.sleep(1.2)
                raw = f"{v.author}|{v.desc}" if v else ""
                # 同一条重复读到就跳过（swipe 可能没生效）
                if v and raw != last_raw:
                    seen.append(v)
                    last_raw = raw
                if i < cap - 1:
                    await _swipe_next(serial)

            shot = await phone.screenshot(serial, cfg, name="douyin")
    except adb.AdbError as exc:
        return f"⚠️ {exc}"

    if not seen:
        return "⚠️ 没读到视频内容（可能在开屏广告或登录页），请先手动打开抖音首页"

    lines = [f"看了 {len(seen)} 条："]
    lines += [v.line(i + 1) for i, v in enumerate(seen)]
    lines.append(f"\n当前停在第 {len(seen)} 条。要评论就调 douyin_comment。")
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
async def douyin_comment(
    ctx: RunContext[ToolContext],
    text: str,
) -> str:
    """给**当前正在看的这条**抖音视频发评论。

    先用 douyin_browse 刷到目标视频再调本工具。同一句话不重复发。

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
        async with adb.device(serial):
            await _ensure_douyin(serial)
            await asyncio.sleep(1.0)

            # 找「评论」按钮；遇到直播卡片/广告等无评论按钮的内容，上滑跳过
            btn = await _locate_comment_btn(serial)
            if btn is None:
                return "⚠️ 连刷 5 条都没找到评论按钮（可能一直在直播/广告页）"
            await phone.tap(serial, *btn.center)
            await asyncio.sleep(random.uniform(1.2, 2.0))

            # 点底部输入条展开面板，再点真正的输入框聚焦（否则键盘不上屏）
            box = await _find_comment_input(serial)
            if box is None:
                return "⚠️ 没找到评论输入框"
            await phone.tap(serial, *box.center)
            await asyncio.sleep(random.uniform(0.9, 1.5))

            focused = await _find_comment_input(serial)
            if focused is not None and focused != box:
                await phone.tap(serial, *focused.center)
                await asyncio.sleep(random.uniform(0.6, 1.0))

            if not await phone.type_text(serial, clean):
                return "⚠️ 输入未生效（AdbKeyboard 可能没激活）"

            await asyncio.sleep(random.uniform(0.6, 1.2))
            send = await _find_send_btn(serial)
            if send is None:
                return "⚠️ 没找到发送按钮，评论可能未发出"
            await phone.tap(serial, *send.center)
            await asyncio.sleep(random.uniform(1.0, 1.8))
            shot = await phone.screenshot(serial, cfg, name="douyin_comment")
    except adb.AdbError as exc:
        return f"⚠️ {exc}"

    _mark_comment_used(clean)
    logger.info(f"[wanwan] 抖音评论已发：{clean[:40]}")

    # 把发送后的界面截图给主人，作为「真的发出去了」的凭证
    if ctx.deps.allow_user_outbound and ctx.deps.bot is not None:
        try:
            await ctx.deps.bot.send(MessageSegment.image(shot))
        except Exception:  # noqa: BLE001 —— 截图发不出不影响评论结果
            pass
    return f"✅ 评论已发送：{clean}"


async def _close_comment_panel(serial: str, attempts: int = 3) -> bool:
    """关掉已展开的评论面板，直到确认真的关掉。

    单次点「关闭」可能没生效（焦点/动画），所以循环验证；
    没有关闭按钮时用 back 兜底。
    """
    for _ in range(attempts):
        nodes = await phone.ui(serial)
        close_btn = None
        for n in nodes:
            t = (n.text or n.desc).strip()
            if n.clickable and t in ("关闭", "缩小评论", "收起"):
                close_btn = n
                break
        if close_btn is None:
            return True  # 没有关闭按钮 = 面板已关
        await phone.tap(serial, *close_btn.center)
        await asyncio.sleep(1.6)
    return False


async def _locate_comment_btn(serial: str, retries: int = 5) -> object | None:
    """找视频侧栏的文字评论按钮。

    第一次先关掉可能残留的评论面板；之后上滑跳过直播/广告等无按钮内容。
    """
    for i in range(retries):
        btn = await _find_comment_btn(serial)
        if btn is not None:
            return btn
        if i == 0:
            await _close_comment_panel(serial)
        else:
            await _swipe_next(serial)
    return None


async def _find_comment_btn(serial: str) -> object | None:
    """找视频侧栏的文字评论按钮。

    必须排除「语音评论」（点开会进录音界面）；只认右侧带数字的
    「评论NNN」按钮（如 `评论811，按钮` @x=930,y=1417）。
    """
    nodes = await phone.ui(serial)
    for n in nodes:
        t = (n.text or n.desc).strip()
        if not n.clickable or "评论" not in t:
            continue
        if "语音" in t:
            continue
        if re.search(r"评论\s*[\d.万亿]", t) and n.x1 > 700 and n.y1 > 900:
            return n
    return None


async def _find_comment_input(serial: str) -> object | None:
    """找评论输入框。

    抖音面板底部有明确 placeholder（「爱评论的人…」/「善语…」），
    优先点它所在区域；兜底取底部可点的宽控件（真正的输入容器）。
    不用「评论」这种弱关键字——会误匹配「评论 817」计数标题。
    """
    strong = ("爱评论的人", "善语", "说点什么", "留言")
    nodes = await phone.ui(serial)
    for n in nodes:
        t = (n.text or n.desc).strip()
        if any(k in t for k in strong):
            return n
    # 兜底：底部（y>=2170）可点且宽 >=400 的控件
    # 注意真输入容器宽约 570，别设 600 这种会漏掉的阈值
    for n in nodes:
        if n.clickable and n.y1 >= 2170 and (n.x2 - n.x1) >= 400:
            return n
    return None


async def _find_send_btn(serial: str) -> object | None:
    """找发送按钮。

    抖音把「发送」文字放在不可点的 TextView 上，可点的是旁边空 View，
    所以不能要求 clickable；只需限定底部 y>2100，避免点到评论列表的「回复」。
    """
    nodes = await phone.ui(serial)
    for n in nodes:
        t = (n.text or n.desc).strip()
        if n.y1 > 2100 and ("发送" in t or "发布" in t):
            return n
    return None
