"""手机原语：解锁 / 屏幕状态 / UI 树 / 点击 / 滑动 / 输入 / 启动 App / 截图。

只做「一次原子操作」，不含业务流程（抖音/B站流程在 tools/ 上层）。
所有函数都经 ``adb.device(serial)`` 持锁，避免并发抢 UI。
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

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


async def type_text(serial: str, text: str) -> bool:
    """用 AdbKeyboard 输入中文。返回是否在界面上看到输入结果。"""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")

    # 切到 AdbKeyboard（失败不致命，继续试广播）
    await adb.shell(serial, "ime", "set", ADB_IME, timeout=20)

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
        if any(text[:6] in n.text for n in nodes):
            return True
    return False


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


async def screenshot(serial: str, cfg: WanwanConfig, name: str = "wanwan") -> bytes:
    """截图并落盘，返回 PNG 字节。"""
    data = await _screencap_bytes(serial)
    if not data:
        raise adb.AdbError("截图返回空数据（手机可能已锁屏或离线）")

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
