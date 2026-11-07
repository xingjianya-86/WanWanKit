"""抖音工具：刷视频 / 评论。全部走手机 uiautomator，不用 PC 浏览器。

风控基线：评论前随机停顿、同一条不重复评论（去重靠状态文件）、
失败只回一行 ERROR，不抛堆栈给模型。
"""

from __future__ import annotations

import asyncio
import contextlib
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
from ..core import adb, phone, vision

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


def _junk_reason(nodes: list) -> str:
    """识别直播卡片 / 广告，返回原因（空串=正常内容）。

    抖音广告卡片带独立「广告」标签；直播卡片有「进入直播间」按钮。
    """
    texts = [(n.text or n.desc).strip() for n in nodes]
    blob = " ".join(texts)
    if "进入直播间" in blob:
        return "直播"
    # 独立「广告」标签（不误伤描述里顺带提到广告的正常视频）
    for t in texts:
        if t in ("广告", "广告 sponsor", "赞助"):
            return "广告"
    return ""


async def _ensure_douyin(serial: str) -> None:
    """确保抖音在前台（含亮屏/解锁），且没有残留的评论/语音面板。

    面板开着时 swipe 会滑到面板上而不是切视频——browse 必须先关。
    上一轮用完可能已收手机（结束 App + 锁屏），所以先唤醒解锁再判断前台。
    """
    awake, locked, app = await phone.screen_state(serial)
    if not awake or locked:
        res = await phone.unlock(serial)
        if res.startswith("ERROR"):
            logger.warning(f"[wanwan] 解锁失败: {res}")
        await asyncio.sleep(1.0)
        _, locked, app = await phone.screen_state(serial)
    if app != DOUYIN_PKG:
        await phone.launch(serial, DOUYIN_PKG)
        await asyncio.sleep(6)
        return
    await _close_comment_panel(serial)


async def _phone_put_away(serial: str) -> None:
    """收手机：后台结束抖音 + 锁屏（用完就收，别亮着屏耗电）。"""
    try:
        await adb.shell(serial, "am", "force-stop", DOUYIN_PKG, timeout=15)
        await phone.lock(serial)
        logger.info("[wanwan] 收手机：已结束抖音并锁屏")
    except adb.AdbError as exc:
        logger.warning(f"[wanwan] 收手机失败: {exc}")


@contextlib.asynccontextmanager
async def _put_away_on_exit(serial: str):
    """with 块退出时收手机（成功、失败、异常都收）。"""
    try:
        yield
    finally:
        await _phone_put_away(serial)


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

    读操作，不点赞不评论。**刷完自动收手机**（后台结束抖音 + 锁屏）；
    之后再调 douyin_comment 会重新打开抖音（一般恢复到刚看的这条附近）。

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
            skipped = 0
            last_raw = ""
            attempts = 0
            max_attempts = cap * 4  # 防死循环：直播/广告连播时有上限
            while len(seen) < cap and attempts < max_attempts:
                attempts += 1
                # 界面切换/加载需要时间：轮询直到读到完整信息（有描述或赞数）
                v = None
                nodes: list = []
                for _ in range(3):
                    nodes = await phone.ui(serial)
                    cand = _extract_video(nodes)
                    if cand and (cand.desc or cand.likes != "0"):
                        v = cand
                        break
                    await asyncio.sleep(1.2)

                reason = _junk_reason(nodes)
                if reason:
                    skipped += 1
                    await _swipe_next(serial)
                    continue

                raw = f"{v.author}|{v.desc}" if v else ""
                # 同一条重复读到就跳过（swipe 可能没生效）
                if v and raw != last_raw:
                    seen.append(v)
                    last_raw = raw
                if len(seen) < cap:
                    await _swipe_next(serial)

            shot = await phone.screenshot(serial, cfg, name="douyin")
            # 刷完收手机：后台结束抖音 + 锁屏
            await _phone_put_away(serial)
    except adb.AdbError as exc:
        return f"⚠️ {exc}"

    if not seen:
        return "⚠️ 没读到视频内容（可能在开屏广告或登录页），请先手动打开抖音首页"

    lines = [f"看了 {len(seen)} 条："]
    lines += [v.line(i + 1) for i, v in enumerate(seen)]
    if skipped:
        lines.append(f"\n（已跳过 {skipped} 条直播/广告）")
    lines.append(f"当前停在第 {len(seen)} 条。要评论就调 douyin_comment。")
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
        async with adb.device(serial), _put_away_on_exit(serial):
            await _ensure_douyin(serial)
            await asyncio.sleep(1.0)

            # 找「评论」按钮；遇到直播卡片/广告等无评论按钮的内容，上滑跳过
            btn = await _locate_comment_btn(serial)
            if btn is None:
                return "⚠️ 连刷 5 条都没找到评论按钮（可能一直在直播/广告页）"
            if not await _open_comment_panel(serial, btn):
                return "⚠️ 评论面板没打开（可能点偏或界面异常）"

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

            # 等表情面板/键盘动画走完：动画中截屏会拿到中间态位置，随后点必点空
            await asyncio.sleep(random.uniform(1.6, 2.4))
            send = await _locate_send_stable(serial)
            if send is None:
                await asyncio.sleep(random.uniform(0.8, 1.2))
                send = await _locate_send_stable(serial)
            if send is None:
                await _abort_panel(serial)
                nodes = await phone.ui(serial)
                bottom = [f"y={n.y1},{(n.text or n.desc).strip()[:12]!r}" for n in nodes if n.y1 >= 2100][:12]
                logger.warning(f"[wanwan] 发送按钮未找到，底部控件: {bottom}")
                return "⚠️ 没找到发送按钮，评论未发出"
            await phone.tap(serial, *send)
            await asyncio.sleep(random.uniform(1.2, 1.8))

            # 验证：发送成功会清空输入框；没清空就**重新定位**（按钮可能又漂了）再点一次
            if not await _input_cleared(serial, clean):
                fresh = await _locate_send_stable(serial, tries=2)
                await phone.tap(serial, *(fresh or send))
                await asyncio.sleep(random.uniform(1.2, 1.8))
            if not await _input_cleared(serial, clean):
                await _abort_panel(serial)
                logger.warning("[wanwan] 发送按钮点击两次仍无效，已放弃")
                return "⚠️ 点击发送无效，评论未发出（文字仍在输入框）"

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


async def _input_cleared(serial: str, text: str) -> bool:
    """输入框是否已清空（发送成功的唯一标志）。

    关键：dump 失败时 ui() 会**静默返回空列表**，那不是「清空了」——
    必须重试；耗尽仍拿不到控件树就保守当作「没清空」，
    宁可多点一次也绝不能假报「已发送」（10:28 就是这么误判的）。
    只认底部输入条区域（y>=2170）：评论列表里的同文节点位置更高（y<=2160）。
    """
    for i in range(3):
        nodes = await phone.ui(serial)
        if nodes:
            return not any(text[:6] in n.text and n.y1 >= 2170 for n in nodes)
        await asyncio.sleep(0.6 + i * 0.5)
    logger.warning("[wanwan] uiautomator dump 连续失败，按「未清空」保守处理")
    return False


async def _locate_send_stable(serial: str, tries: int = 4) -> tuple[int, int] | None:
    """定位发送按钮，要求**连续两次结果一致**才返回。

    表情面板展开动画中按钮位置会漂移，截到中间态随后点击必落空
    （10:28 实测：动画中截屏点空、静止后同一坐标一点就中）。
    """
    prev: tuple[int, int] | None = None
    for _ in range(tries):
        pt = await _find_send_btn(serial)
        if pt is None:
            await asyncio.sleep(0.5)
            continue
        if pt == prev:
            return pt
        prev = pt
        await asyncio.sleep(0.45)
    return prev


async def _abort_panel(serial: str) -> None:
    """失败时关掉评论面板，别让手机卡在界面上。"""
    await _close_comment_panel(serial)


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


def _is_comment_panel_open(nodes: list) -> bool:
    """评论面板是否真的打开。

    面板特征：顶部有「关闭」按钮 或 「评论 N」标题（y<1000）。
    只靠「底部有可点宽控件」判断会误命中首页导航栏。
    """
    for n in nodes:
        t = (n.text or n.desc).strip()
        if n.y1 < 1000 and t in ("关闭", "缩小评论"):
            return True
        if n.y1 < 1000 and t.startswith("评论 ") and "，" not in t:
            return True
    return False


async def _open_comment_panel(serial: str, btn: object) -> bool:
    """点评论按钮并**验证面板真的打开**（没开就重试）。

    只点不验证会让后续步骤在首页导航栏上乱点。
    """
    for attempt in range(3):
        await phone.tap(serial, *btn.center)
        await asyncio.sleep(2.0 + attempt * 0.8)
        nodes = await phone.ui(serial)
        if _is_comment_panel_open(nodes):
            return True
        # 没开：可能点偏或界面在切换，重新定位按钮
        btn2 = await _locate_comment_btn(serial)
        if btn2 is not None:
            btn = btn2
    return False


async def _find_comment_input(serial: str) -> object | None:
    """找评论输入框。

    抖音面板底部有明确 placeholder（「爱评论的人…」/「善语…」），
    优先点它所在区域；兜底取底部可点的宽控件（真正的输入容器）。
    不用「评论」这种弱关键字——会误匹配「评论 817」计数标题。
    """
    strong = ("爱评论的人", "善语", "说点什么", "留言")
    nodes = await phone.ui(serial)

    # 面板没开就别找——否则兜底会命中首页底部导航栏
    if not _is_comment_panel_open(nodes):
        return None

    for n in nodes:
        t = (n.text or n.desc).strip()
        if any(k in t for k in strong):
            return n
    # 兜底：底部（y>=2170）可点且宽 >=400 的控件。
    # 不能按 x1 过滤——真输入框 x1=30（左侧起始），加了会误伤；
    # 面板已由 _is_comment_panel_open 确认打开，此时底部没有导航栏。
    for n in nodes:
        if n.clickable and n.y1 >= 2170 and (n.x2 - n.x1) >= 400:
            return n
    return None


async def _find_send_btn(serial: str) -> tuple[int, int] | None:
    """找「发送」按钮的**真实像素坐标**。

    键盘/表情面板打开后 dump 坐标不可信（残留节点报 y=2208、真实 y≈1245），
    所以优先截屏找抖音红 (#FE2C55) 实心按钮；截屏失败才退回 dump——
    dump 里同一按钮有两份，取 bounds 最高（未被裁剪）的那份，
    再回溯面积最小的可点父节点（直接用文字节点会因 2px 残缺 bounds 点偏）。
    """
    try:
        blob = vision.find_color_button(await phone.capture(serial), (254, 44, 85))
    except adb.AdbError:
        blob = None
    if blob is not None:
        return blob.center

    nodes = await phone.ui(serial)
    # 只认整词「发送/发布」——「同时发布为作品」「去发布作品」是别的控件，点错会切开关
    cands = [n for n in nodes if (n.text or n.desc).strip() in ("发送", "发布")]
    if not cands:
        return None
    # 同一按钮会被 dump 报两份：真实位置（完整 bounds）+ 被裁剪的残留（仅 2px 高），
    # 按残留那份点必点空，取 bounds 最高（未被裁剪）的
    n = max(cands, key=lambda c: c.y2 - c.y1)
    # 回溯包含它的可点父节点，取面积最小的（直接返回会命中根布局 (0,0)-(1080,2210)）
    parents = [p for p in nodes if p.clickable and p.x1 <= n.x1 and p.y1 <= n.y1 and p.x2 >= n.x2 and p.y2 >= n.y2]
    if parents:
        picked = min(parents, key=lambda p: (p.x2 - p.x1) * (p.y2 - p.y1))
        return picked.center
    return n.center
