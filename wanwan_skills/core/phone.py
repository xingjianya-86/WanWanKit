"""手机原语：解锁 / 屏幕状态 / UI 树 / 点击 / 滑动 / 输入 / 启动 App / 截图。

只做「一次原子操作」，不含业务流程（抖音/B站流程在 tools/ 上层）。
所有函数都经 ``adb.device(serial)`` 持锁，避免并发抢 UI。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from gsuid_core.logger import logger

from ..config import WanwanConfig
from . import adb

# AdbKeyboard：输入中文用（广播 ADB_INPUT_B64）
ADB_IME = "com.android.adbkeyboard/.AdbIME"

_WAKEUP_MS = 800


@dataclass(frozen=True)
class UINode:
    """uiautomator dump 出来的一个控件。"""

    text: str
    desc: str
    cls: str
    clickable: bool
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    @property
    def label(self) -> str:
        """给人看的一行描述。"""
        body = (self.text or self.desc or "").strip().replace("\n", " ")
        if len(body) > 40:
            body = body[:40] + "…"
        mark = " [可点]" if self.clickable else ""
        return f"{body}{mark} @({self.x1},{self.y1})" if body else f"<{self.cls}> @({self.x1},{self.y1})"


def is_input_node(n: UINode) -> bool:
    """dump 里的「输入框文字」节点。

    按**形态**认，不按 y 带认——y 带被真机打过脸：B站回复框底部锚定、
    文字一多就向上撑（实测顶边 1891，比预设带低 9px，漏判导致重播 7 份）。
    三重覆盖：
    - 底部条区域 y≥2170（含 dump 残留副本）
    - cls == EditText（多行输入框撑到哪都算；视频简介/标题是 TextView 排除）
    - 贴左缘宽框且 y<1150（抖音键盘开时的输入框，兜 cls 异常）
    评论列表从 x1≈120 起、视频简介是 TextView，都被排除。
    """
    if n.y1 >= 2170:
        return True
    if n.cls == "EditText":
        return True
    return n.x1 <= 60 and (n.x2 - n.x1) >= 900 and n.y1 < 1150


async def screen_size(serial: str) -> tuple[int, int]:
    """返回 (宽, 高)；读不到用常见默认值。"""
    res = await adb.shell(serial, "wm", "size", timeout=15)
    m = re.search(r"(\d+)x(\d+)", res.out)
    return (int(m.group(1)), int(m.group(2))) if m else (1080, 2340)


async def screen_state(serial: str) -> tuple[bool, bool, str]:
    """返回 (亮屏, 锁屏, 前台包名)。"""
    power = await adb.shell(serial, "dumpsys", "power", timeout=20)
    awake = "mWakefulness=Awake" in power.out

    win = await adb.shell(serial, "dumpsys", "window", timeout=25)
    locked = "isKeyguardShowing=true" in win.out
    m = re.search(r"mCurrentFocus=Window\{[^}]*?([A-Za-z0-9_.]+)/", win.out)
    return awake, locked, m.group(1) if m else "?"


async def _wake_light(serial: str) -> None:
    await adb.shell(serial, "input", "keyevent", "KEYCODE_WAKEUP", timeout=10)


async def unlock(serial: str) -> str:
    """唤醒并解锁（无安全锁屏时）。有安全锁屏时 adb 解不了，如实报错。"""
    await _wake_light(serial)
    await _async_sleep(1.0)

    _, locked, _ = await screen_state(serial)
    if locked:
        w, h = await screen_size(serial)
        await adb.shell(
            serial,
            "input",
            "swipe",
            str(w // 2),
            str(int(h * 0.8)),
            str(w // 2),
            str(int(h * 0.25)),
            "250",
            timeout=15,
        )
        await _async_sleep(1.0)
        _, locked, _ = await screen_state(serial)
        if locked:
            return "ERROR: 手机有安全锁屏，adb 无法解锁（请改用图案/密码或临时关闭）"
    return "已解锁"


async def lock(serial: str) -> str:
    """锁屏/息屏。"""
    await adb.shell(serial, "input", "keyevent", "223", timeout=10)  # KEYCODE_SLEEP
    return "已锁屏"


async def tap(serial: str, x: int, y: int) -> None:
    await adb.shell(serial, "input", "tap", str(int(x)), str(int(y)), timeout=15)


async def swipe(serial: str, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
    await adb.shell(
        serial,
        "input",
        "swipe",
        str(int(x1)),
        str(int(y1)),
        str(int(x2)),
        str(int(y2)),
        str(int(ms)),
        timeout=20,
    )


async def ui(serial: str, *, heal: bool = True, timeout: float = 12.0) -> list[UINode]:
    """dump 当前界面控件树。heal=True 时对 uiautomator 卡住做一次自愈点击。"""
    await _wake_light(serial)
    path = f"/sdcard/_wanwan_ui_{int(time.time() * 1000)}.xml"

    for attempt in range(2):
        res = await adb.shell(serial, "uiautomator", "dump", path, timeout=timeout)
        blob = res.out + res.err
        if "dumped" in blob:
            break
        if "idle" in blob and heal and attempt == 0:
            # 悬浮窗动画会让 uiautomator 报 idle；点一下左上角让它停
            await tap(serial, 120, 205)
            await _async_sleep(0.8)
        else:
            await _async_sleep(1.0)

    raw = await adb.exec_out(serial, "cat", path, timeout=timeout)
    await adb.shell(serial, "rm", path, timeout=10)
    return _parse_ui(raw.out if isinstance(raw.out, str) else "")


def _parse_ui(xml_text: str) -> list[UINode]:
    """解析 uiautomator dump 的 XML。解析失败返回空列表（不抛）。"""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    nodes: list[UINode] = []
    for n in root.iter("node"):
        m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        nodes.append(
            UINode(
                text=n.get("text", "") or "",
                desc=n.get("content-desc", "") or "",
                cls=(n.get("class", "") or "").split(".")[-1],
                clickable=n.get("clickable") == "true",
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
            )
        )
    return nodes


def _text_on_screen(nodes: list, text: str) -> bool:
    """字上屏了吗：先按输入框形态认；认不出但字已在屏上任何地方也算。

    第二层宽松判定是防**重播翻倍**的最后防线：形态漏判一次已经够了，
    绝不能因此再播一遍（真机回复场景因此播过 7 份）。
    """
    if any(text[:6] in n.text and is_input_node(n) for n in nodes):
        return True
    if any(text[:6] in n.text for n in nodes):
        logger.info("[wanwan] type_text: 宽松判定命中（输入框形态未识别），不重播")
        return True
    return False


def _looks_like_hint(text: str) -> bool:
    """是占位提示语而不是真内容（B站「评论千万条…」、抖音「有爱评论…」等）。

    input_text 把 hint 当内容会让 clear_input 对着提示语空转、
    让回复预检误判「有残留」。
    """
    t = text.strip()
    if len(t) > 20:
        return False
    return any(m in t for m in ("评论千万条", "说点什么", "有爱评论", "善语", "爱评论的人", "留言"))


async def input_text(serial: str) -> str:
    """读输入框**真实内容**（排除占位提示语；dump 失败返回空）。"""
    nodes = await ui(serial)
    cands = [n.text for n in nodes if n.cls == "EditText" and n.text and not _looks_like_hint(n.text)]
    if not cands:
        cands = [n.text for n in nodes if is_input_node(n) and n.text and not _looks_like_hint(n.text)]
    return max(cands, key=len) if cands else ""


async def clear_input(serial: str, rounds: int = 5) -> bool:
    """清空输入框：读现有长度 → 批量 DEL（keyevent 67=退格），多轮直到读空。

    光标须在文末（B站/抖音聚焦时默认如此）；单轮上限 80 键，多轮覆盖长残留。
    """
    for _ in range(rounds):
        existing = await input_text(serial)
        if not existing:
            return True
        dels = ["67"] * min(len(existing) + 3, 80)
        await adb.shell(serial, "input", "keyevent", *dels, timeout=30)
        await _async_sleep(0.8)
    return not await input_text(serial)


async def type_text(serial: str, text: str, *, clear: bool = True) -> bool:
    """用 AdbKeyboard 输入中文。返回是否在界面上看到输入结果。

    clear=True（默认）：先清空输入框再广播正文
    （ADB_INPUT_CLEAR_TEXT 在抖音上不生效，不清会把新旧文字拼在一起）。
    clear=False（回复场景）：输入框已预填「回复 @xxx」归属前缀，
    **只能追加不能清**——清掉归属，回复就变成普通评论了。
    """
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")

    # 切到 AdbKeyboard（失败不致命，继续试广播）
    await adb.shell(serial, "ime", "set", ADB_IME, timeout=20)

    if clear:
        await clear_input(serial)

    for _ in range(3):
        await adb.shell(
            serial,
            "am",
            "broadcast",
            "-a",
            "ADB_INPUT_B64",
            "--es",
            "msg",
            payload,
            timeout=20,
        )
        await _async_sleep(1.2)
        nodes = await ui(serial)
        if _text_on_screen(nodes, text):
            return True
        if not nodes:
            # dump 瞬时空 ≠ 没上屏：绝不能因此重播——重播会把同一段文字
            # 播两遍拼进输入框（真机复现过）。等一下重读，读不到就报失败。
            await _async_sleep(1.0)
            nodes = await ui(serial)
            if _text_on_screen(nodes, text):
                return True
            if not nodes:
                logger.warning("[wanwan] type_text: dump 连续读不到，放弃重播（避免文字翻倍）")
                return False
        # dump 正常且屏上哪儿都没见字 → 真没播进去，才允许重播
    return False


def find_reply_button(nodes: list, target: str = ""):
    """评论区里找要回复的那条评论的「回复」按钮。

    target 空 → 回复第一条（热评）；否则用作者名/内容片段锚定那条评论
    （跳过 EditText——输入框里残留的「回复 @xx」前缀不能当锚点），
    取它下方最近的「回复」。找不到返回 None，由调用方如实报告。
    只认整词「回复」，排除「展开N条回复」。
    """
    btns = [n for n in nodes if n.clickable and (n.text or n.desc or "").strip() == "回复" and n.y1 > 900]
    if not btns:
        return None
    if not target:
        return min(btns, key=lambda b: b.y1)

    anchors = [n for n in nodes if target in (n.text or n.desc or "") and n.y1 > 900 and n.cls != "EditText"]
    if not anchors:
        return None
    anchor_y = min(n.y1 for n in anchors)
    below = [b for b in btns if b.y1 >= anchor_y - 20]
    if not below:
        return None
    return min(below, key=lambda b: b.y1)


async def input_cleared(serial: str, text: str) -> bool:
    """输入框是否已清空（发送成功的唯一标志）。

    dump 失败时 ui() 会**静默返回空列表**，那不是「清空了」——
    必须重试；耗尽仍拿不到控件树就保守当作「没清空」，
    宁可多等也绝不能假报「已发送」。
    文字位置用 is_input_node 认（位置随 App/键盘状态变，不是固定 y）；
    评论列表里的同文节点不算输入框（发出去之后它出现在列表里是正常的）。
    """
    for i in range(3):
        nodes = await ui(serial)
        if nodes:
            return not any(text[:6] in n.text and is_input_node(n) for n in nodes)
        await asyncio.sleep(0.6 + i * 0.5)
    logger.warning("[wanwan] uiautomator dump 连续失败，按「未清空」保守处理")
    return False


async def put_away(serial: str) -> None:
    """收手机：锁屏（息屏即收，App 留后台保状态，重开恢复原位）。"""
    try:
        await lock(serial)
        logger.info("[wanwan] 收手机：已锁屏（App 留后台，重开恢复原位）")
    except adb.AdbError as exc:
        logger.warning(f"[wanwan] 收手机失败: {exc}")


@contextlib.asynccontextmanager
async def put_away_on_exit(serial: str):
    """with 块退出时收手机（成功、失败、异常都收）。"""
    try:
        yield
    finally:
        await put_away(serial)


async def launch(serial: str, package: str) -> None:
    """启动 App（monkey 方式，通用）。"""
    await adb.shell(
        serial,
        "monkey",
        "-p",
        package,
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
        timeout=30,
    )
    await _async_sleep(4.0)


async def capture(serial: str) -> bytes:
    """截屏返回 PNG 字节（不落盘）——供像素级定位用。"""
    data = await _screencap_bytes(serial)
    if not data:
        raise adb.AdbError("截图返回空数据（手机可能已锁屏或离线）")
    return data


async def screenshot(serial: str, cfg: WanwanConfig, name: str = "wanwan") -> bytes:
    """截图并落盘，返回 PNG 字节。"""
    data = await capture(serial)

    out_dir = Path(cfg.phone_shot_dir or "/tmp")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}_{time.strftime('%H%M%S')}.png"
    path.write_bytes(data)
    return data


async def _screencap_bytes(serial: str) -> bytes:
    """screencap 输出是 PNG 二进制，必须按字节取，不能走文本解码。"""
    proc = await asyncio.create_subprocess_exec(
        "adb",
        "-s",
        serial,
        "exec-out",
        "screencap",
        "-p",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except TimeoutError:
        proc.kill()
        return b""
    return out


async def _async_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)
