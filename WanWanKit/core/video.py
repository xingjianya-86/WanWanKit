"""短视频合成（ffmpeg）：N 张场景图 + 一段配音 → 720p mp4。

移植自 legacy `video_maker_mcp.py`（make_short_video 的合成段，原样保留
时长均分算法与滤镜链）。前两段管线由 core.ark（Seedream 生图）与
core.mimo（MiMo 音色克隆配音）提供。

子进程为阻塞调用——调用方须 `asyncio.to_thread(...)` 包住，别卡 GsCore
事件循环。合成失败保留 work 目录现场便于排查；成功后由调用方清理。
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path

from .look import WANWAN_LOOK  # noqa: F401 —— re-export，保持 video.WANWAN_LOOK 可用

W, H, FPS = 1280, 720, 30
FF_TIMEOUT = 600.0  # 合成超时秒（长配音 + 8 画面也够）
MAX_SCENES = 8
MAX_NARRATION = 1000


class VideoError(RuntimeError):
    """合成失败。携带对用户可读的原因。"""


def pad_cmd(src: Path, dst: Path, lead_seconds: float) -> list[str]:
    """音频前补静音的 ffmpeg 命令（纯函数，供单测）。"""
    return [
        "ffmpeg",
        "-hide_banner",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-t",
        f"{lead_seconds:.3f}",
        "-i",
        "anullsrc=r=24000:cl=mono",
        "-i",
        str(src),
        "-filter_complex",
        "[0:a][1:a]concat=n=2:v=0:a=1",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(dst),
    ]


def pad_audio(src: Path, dst: Path, lead_seconds: float) -> None:
    """在音频前补 lead_seconds 秒静音（24k Hz 单声道，匹配 TTS 输出）。"""
    proc = subprocess.run(
        pad_cmd(src, dst, lead_seconds),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise VideoError(f"补静音失败: {(proc.stderr or '')[-300:]}")


def _parse_duration(text: str) -> float:
    """ffprobe duration 输出 → 秒；无法解析按 0 处理（上层报「时长异常」）。"""
    try:
        return float((text or "").strip() or 0)
    except ValueError:
        return 0.0


def duration(path: Path) -> float:
    """ffprobe 探测媒体时长（秒）。"""
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return _parse_duration(out.stdout or "")


def _split_durations(total: float, n: int) -> list[float]:
    """把总时长均分给 n 个画面（纯函数，供单测）。

    每段向上取 0.1s；末段吃掉全部余量且兜底 0.5s——宁长勿短，防截掉配音尾巴。
    """
    base = math.ceil(total / n * 10) / 10
    durs = [base] * (n - 1)
    durs.append(max(0.5, round(total - base * (n - 1), 2)))
    return durs


def assemble_cmd(images: list[Path], audio: Path, total: float, out: Path) -> list[str]:
    """逐画面均分时长、缩放补边 1280x720@30fps、concat 后混入配音（纯函数，供单测）。"""
    n = len(images)
    durs = _split_durations(total, n)

    cmd: list[str] = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    for p, d in zip(images, durs):
        cmd += ["-loop", "1", "-t", str(d), "-i", str(p)]
    cmd += ["-i", str(audio)]

    fc = []
    for i in range(n):
        fc.append(
            f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={FPS},format=yuv420p[v{i}]"
        )
    fc.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vout]")
    fc.append(f"[{n}:a]aresample=44100[aout]")
    cmd += [
        "-filter_complex",
        ";".join(fc),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out),
    ]
    return cmd


def assemble(
    images: list[Path],
    audio: Path,
    total: float,
    out: Path,
    *,
    timeout: float = FF_TIMEOUT,
) -> None:
    """执行合成（timeout 可由配置 ffmpeg_timeout 覆盖）。失败抛 VideoError。"""
    if not images:
        raise VideoError("没有场景图，无法合成")
    proc = subprocess.run(
        assemble_cmd(images, audio, total, out),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise VideoError(f"ffmpeg 失败: {(proc.stderr or '')[-500:]}")
