"""adb over TCP 客户端：异步执行、设备串行、可读错误。

只封装「跑 adb 命令」，不含任何业务语义；上层 phone 模块负责语义。
全部用 asyncio 子进程，避免同步 subprocess 阻塞 GsCore 事件循环。
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass


class AdbError(RuntimeError):
    """adb 调用失败，message 对用户可读。"""


@dataclass(frozen=True)
class AdbResult:
    """一次 adb 调用的结果。"""

    code: int
    out: str
    err: str

    @property
    def ok(self) -> bool:
        return self.code == 0


# 进程内串行：手机 UI 是独占资源，并发操作会互相打断
_LOCK = asyncio.Lock()


async def _run(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    """跑 adb（不含 -s），返回 (returncode, stdout, stderr)。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            "adb",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise AdbError("adb 未安装（PATH 里找不到 adb）") from exc

    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError as exc:
        proc.kill()
        raise AdbError(f"adb 超时（{timeout:.0f}s）：adb {' '.join(args[:3])}") from exc
    return proc.returncode or 0, out, err


async def _serial(serial: str, args: list[str], timeout: float) -> AdbResult:
    """跑 `adb -s <serial> <args>`。"""
    code, out, err = await _run(["-s", serial, *args], timeout)
    return AdbResult(code, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


async def ensure_connected(serial: str, timeout: float = 25.0) -> None:
    """确保设备在线；离线则尝试 connect，仍失败抛 AdbError。"""
    code, out, _ = await _run(["devices"], timeout=20.0)
    if code != 0:
        raise AdbError(f"adb devices 失败: {out.decode('utf-8', 'replace')[:200]}")
    if _is_online(serial, out.decode("utf-8", "replace")):
        return

    await _run(["connect", serial], timeout=timeout)
    await asyncio.sleep(1.5)

    _, out, _ = await _run(["devices"], timeout=20.0)
    if not _is_online(serial, out.decode("utf-8", "replace")):
        raise AdbError(f"手机离线（{serial}）。请在手机上确认：ZeroTier 已连接、无线调试已开启。")


def _is_online(serial: str, devices_text: str) -> bool:
    """devices 输出里该 serial 是否为 device 状态（offline/unauthorized 都算离线）。"""
    return bool(re.search(re.escape(serial) + r"\s+device\b", devices_text))


async def shell(serial: str, *args: str, timeout: float = 30.0) -> AdbResult:
    """执行 `adb shell <args>`。"""
    return await _serial(serial, ["shell", *args], timeout)


async def exec_out(serial: str, *args: str, timeout: float = 60.0) -> AdbResult:
    """执行 `adb exec-out <args>`（输出不换行转换，适合二进制）。"""
    return await _serial(serial, ["exec-out", *args], timeout)


async def ensure(serial: str) -> None:
    """仅确保在线（不持锁）。用于诊断类一次性调用。"""
    await ensure_connected(serial)


@asynccontextmanager
async def device(serial: str):
    """一次手机操作的统一切口：持锁 + 确保在线，退出时释放。

    所有手机原语都应 ``async with device(serial):`` 包裹，保证同一时刻
    只有一个操作在跑——手机 UI 是独占资源，并发会互相打断。
    """
    async with _LOCK:
        await ensure_connected(serial)
        yield
