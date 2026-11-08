"""手机工具：读屏幕 / 执行操作。晚晚通过这两个工具操控真机。

设计：只暴露少量高层动作，不把 adb 细节漏给模型；
截图走 bot 直发（主人能直接看到屏幕），文字控件树进返回值。
"""

from __future__ import annotations

import asyncio

from gsuid_core.ai_core.models import ToolContext
from gsuid_core.ai_core.register import ai_tools
from gsuid_core.logger import logger
from gsuid_core.segment import MessageSegment
from pydantic_ai import RunContext

from ..config import load_config
from ..core import adb, phone

_CAP_DOMAIN = "手机控制"
_COVERS = [
    "看手机屏幕当前在什么界面",
    "操作手机：点击、滑动、输入文字",
    "打开某个 App",
    "刷抖音 / 刷 B 站",
    "解锁 / 锁屏手机",
]

# 有界动作白名单：避免模型构造任意 shell
_ACTIONS = frozenset({"tap", "swipe", "input", "open", "back", "home", "lock", "unlock"})


async def _shot_and_send(ctx: RunContext[ToolContext], serial: str, cfg: object) -> str:
    """截图并尝试发到对话；返回提示文本。"""
    img = await phone.screenshot(serial, cfg, name="look")  # type: ignore[arg-type]
    if ctx.deps.allow_user_outbound and ctx.deps.bot is not None:
        try:
            await ctx.deps.bot.send(MessageSegment.image(img))
            return ""
        except Exception as exc:  # noqa: BLE001 —— 发送失败不该让整个工具失败
            logger.warning(f"[wanwan] 截图发送失败: {exc}")
    return f"（截图已存，{len(img) // 1024} KB）"


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=60.0,
)
async def phone_look(ctx: RunContext[ToolContext]) -> str:
    """读取手机当前屏幕：亮/锁屏、前台 App、可见文字，并把截图发给主人。

    想知道「手机现在什么界面」「App 开着吗」「屏幕上有什么字」时调用。
    返回的控件列表带坐标，配合 phone_do(tap) 可以精确点击。
    """
    cfg = load_config()
    if cfg is None or not cfg.phone_ready:
        return "⚠️ 手机通道未配置（缺 phone_serial）"

    serial = cfg.phone_serial
    try:
        async with adb.device(serial):
            awake, locked, app = await phone.screen_state(serial)
            nodes = await phone.ui(serial)
    except adb.AdbError as exc:
        return f"⚠️ {exc}"

    lines = [
        f"屏幕:{'亮' if awake else '灭'} 锁屏:{'是' if locked else '否'} 前台:{app}",
        f"可见文字（{len(nodes)} 个控件）：",
    ]
    shown = 0
    for n in nodes:
        body = (n.text or n.desc).strip()
        if not body:
            continue
        lines.append(f"  - {body[:50]}  @({n.x1},{n.y1})")
        shown += 1
        if shown >= 25:
            lines.append("  …（其余略）")
            break
    if shown == 0:
        lines.append("  （当前界面无文字控件）")

    note = await _shot_and_send(ctx, serial, cfg)
    if note:
        lines.append(note)
    return "\n".join(lines)


@ai_tools(
    category="common",
    capability_domain=_CAP_DOMAIN,
    covers=_COVERS,
    code_callable=False,
    timeout=90.0,
)
async def phone_do(
    ctx: RunContext[ToolContext],
    action: str,
    x: int = 0,
    y: int = 0,
    x2: int = 0,
    y2: int = 0,
    text: str = "",
    app: str = "",
) -> str:
    """对手机执行一个动作。返回动作结果与执行后的界面文字。

    Args:
        action: 动作名，只能是 tap / swipe / input / open / back / home / lock / unlock。
        x, y: tap 的坐标；swipe 的起点。坐标来自 phone_do 返回的控件坐标。
        x2, y2: swipe 的终点。
        text: input 要输入的文字（中文走 AdbKeyboard）。
        app: open 要启动的包名，如 com.ss.android.ugc.aweme（抖音）。
    """
    cfg = load_config()
    if cfg is None or not cfg.phone_ready:
        return "⚠️ 手机通道未配置（缺 phone_serial）"

    act = action.strip().lower()
    if act not in _ACTIONS:
        return f"⚠️ 未知动作 {action!r}，可选：{', '.join(sorted(_ACTIONS))}"

    serial = cfg.phone_serial
    try:
        async with adb.device(serial):
            result = await _dispatch(serial, act, x, y, x2, y2, text, app)
            if act in ("lock", "open", "home", "back"):
                await asyncio.sleep(1.2)  # 等界面稳定，避免读到过渡帧
                _, _, app_now = await phone.screen_state(serial)
                nodes = await phone.ui(serial)
                visible = [
                    f"  - {(n.text or n.desc).strip()[:50]} @({n.x1},{n.y1})"
                    for n in nodes
                    if (n.text or n.desc).strip()
                ][:15]
                result += f"\n当前前台: {app_now}\n" + ("\n".join(visible) or "  （无文字）")
    except adb.AdbError as exc:
        return f"⚠️ {exc}"

    return result


async def _dispatch(
    serial: str,
    act: str,
    x: int,
    y: int,
    x2: int,
    y2: int,
    text: str,
    app: str,
) -> str:
    """执行单个动作并返回一行结果。"""
    if act == "tap":
        if not (x or y):
            return "⚠️ tap 需要 x/y 坐标"
        await phone.tap(serial, x, y)
        return f"已点击 ({x},{y})"

    if act == "swipe":
        await phone.swipe(serial, x, y, x2, y2)
        return f"已滑动 ({x},{y})->({x2},{y2})"

    if act == "input":
        if not text:
            return "⚠️ input 需要 text"
        ok = await phone.type_text(serial, text)
        return "输入成功" if ok else "⚠️ 输入已发送，但界面未见文字（可能焦点不在输入框）"

    if act == "open":
        if not app:
            return "⚠️ open 需要 app 包名"
        await phone.launch(serial, app)
        return f"已启动 {app}"

    if act == "back":
        await adb.shell(serial, "input", "keyevent", "4", timeout=10)
        return "已返回"

    if act == "home":
        await adb.shell(serial, "input", "keyevent", "3", timeout=10)
        return "已回桌面"

    if act == "lock":
        return await phone.lock(serial)

    if act == "unlock":
        return await phone.unlock(serial)

    return f"⚠️ 未实现的动作 {act}"
