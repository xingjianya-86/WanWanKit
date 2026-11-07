"""像素级定位。

uiautomator dump 的坐标在键盘/表情面板打开时不可信：实测 dump 报「发送」在
y=2208，而 screencap 里真实像素在 y≈1245（面板把窗口压矮了约 960px，dump 仍按
全屏布局报）。screencap 的像素永远等于真实屏幕坐标，所以「键盘打开之后」的点击
必须先截屏、按图里的真实像素坐标点。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True)
class Blob:
    """截图里的一个实心色块（按钮）。"""

    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def center(self) -> tuple[int, int]:
        return ((self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2)

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1


def find_color_button(
    png: bytes,
    rgb: tuple[int, int, int],
    *,
    tol: int = 60,
    y_min_ratio: float = 0.4,
    min_run: int = 70,
    max_run: int = 420,
    min_height: int = 30,
    max_height: int = 220,
) -> Blob | None:
    """在截图里找指定颜色的实心按钮，返回面积最大的合格色块。

    思路：按钮是实心圆角矩形，中间若干行会有 >= min_run 的同色长连续段；
    表情、小图标、文字等杂色块宽度都远小于按钮，靠段长就能排掉。
    再把相邻的合格行聚成块，按尺寸过滤后取面积最大者。

    Args:
        png: 截图 PNG 字节。
        rgb: 目标颜色（抖音发送按钮 #FE2C55）。
        tol: 每通道容差。
        y_min_ratio: 只扫图片下方这个比例以下的区域（按钮不会在屏幕上半部）。
        min_run / max_run: 单行同色连续段的像素长度范围。
        min_height / max_height: 色块高度范围。
    """
    try:
        im = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:  # noqa: BLE001 —— 坏图直接当没找到
        return None

    width, height = im.size
    data = im.tobytes()
    cr, cg, cb = rgb
    y_start = int(height * y_min_ratio)

    # 每行收集合格的同色连续段（步长 2：够准且快一倍）
    rows: list[tuple[int, int, int, int]] = []  # (y, x1, x2, 段长)
    for y in range(y_start, height, 2):
        base = y * width * 3
        run_start = -1
        run_len = 0
        for x in range(0, width, 2):
            i = base + x * 3
            hit = abs(data[i] - cr) <= tol and abs(data[i + 1] - cg) <= tol and abs(data[i + 2] - cb) <= tol
            if hit:
                if run_start < 0:
                    run_start = x
                run_len += 2
            elif run_start >= 0:
                if min_run <= run_len <= max_run:
                    rows.append((y, run_start, x, run_len))
                run_start = -1
                run_len = 0
        if run_start >= 0 and min_run <= run_len <= max_run:
            rows.append((y, run_start, width, run_len))

    if not rows:
        return None

    # 相邻行聚块（允许 8px 空隙：圆角/抗锯齿会断行）
    groups: list[list[tuple[int, int, int, int]]] = []
    for row in rows:
        if groups and row[0] - groups[-1][-1][0] <= 8:
            groups[-1].append(row)
        else:
            groups.append([row])

    def _row_fill(y: int, x1: int, x2: int) -> float:
        """该行在 [x1,x2] 内目标色的占比（按钮中间的白字会让占比下降但不会归零）。"""
        base = y * width * 3
        hit = 0
        total = 0
        for x in range(x1, x2, 2):
            i = base + x * 3
            total += 1
            if abs(data[i] - cr) <= tol and abs(data[i + 1] - cg) <= tol and abs(data[i + 2] - cb) <= tol:
                hit += 1
        return hit / total if total else 0.0

    best: Blob | None = None
    best_area = 0
    for group in groups:
        # 长段行只覆盖按钮上下沿（中间被「发送」白字截断），用它们的 x 范围
        # 向上下扩展，直到该范围内没有目标色——中间文字行占比仍高，不会断。
        x1 = min(r[1] for r in group)
        x2 = max(r[2] for r in group)
        y1 = group[0][0]
        y2 = group[-1][0]
        for y in range(y1 - 2, max(y_start, y1 - 160), -2):
            if _row_fill(y, x1, x2) < 0.15:
                break
            y1 = y
        for y in range(y2 + 2, min(height, y2 + 160), 2):
            if _row_fill(y, x1, x2) < 0.15:
                break
            y2 = y

        blob = Blob(x1, y1, x2, y2)
        if not (min_height <= blob.height <= max_height):
            continue
        if not (min_run <= blob.width <= max_run):
            continue
        area = blob.width * blob.height
        if area > best_area:
            best = blob
            best_area = area
    return best
