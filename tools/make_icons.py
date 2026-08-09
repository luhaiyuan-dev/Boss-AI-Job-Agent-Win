"""从正式源图生成 Boss 求职助手的透明多尺寸 Windows 图标。

- Boss求职助手.exe：白色圆角底 + 四向 AI 罗盘 + 暖色智能星芒。
- Boss登录浏览器.exe：白色圆角底 + 蓝青连接环 + 红橙连接节点。

源图保存在 ``assets/icons/official/*.png``，产物为同目录下的 ``*.ico``。
用法：python tools/make_icons.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = PROJECT_ROOT / "assets" / "icons" / "official"
# Windows 会按系统 DPI 请求不同的图标资源。例如 125% 缩放下，任务栏
# 大图标为 40×40、小图标为 20×20。只提供传统的 16/32/48 会迫使 Shell
# 临时缩放相邻图层，因此这里覆盖 100%–300% 的常用 DPI 阶梯。
SMALL_ICON_SIZES = frozenset(
    (16, 20, 24, 28, 32, 36, 40, 48, 56, 64, 72, 80, 96)
)
ICON_SIZES = (*sorted(SMALL_ICON_SIZES), 128, 256)
SUPERSAMPLE = 4

BLUE = "#1264F4"
CYAN = "#09BCEB"
RED = "#FF344B"
ORANGE = "#FF9F00"
GOLD = "#FFB000"
TILE_BORDER = "#D7E2EE"


def _prepare_icon(source: Path, *, size: int = 256) -> Image.Image:
    """裁掉透明空白并保留少量安全边距，让小尺寸图标仍清晰可辨。"""

    image = Image.open(source).convert("RGBA")
    bounds = image.getchannel("A").getbbox()
    if bounds is None:
        raise ValueError(f"图标源图完全透明：{source}")

    left, top, right, bottom = bounds
    subject_width = right - left
    subject_height = bottom - top
    padding = max(1, round(max(subject_width, subject_height) * 0.065))
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    crop_size = max(subject_width, subject_height) + 2 * padding
    crop_box = (
        round(center_x - crop_size / 2),
        round(center_y - crop_size / 2),
        round(center_x + crop_size / 2),
        round(center_y + crop_size / 2),
    )
    return image.crop(crop_box).resize(
        (size, size),
        Image.Resampling.LANCZOS,
    )


def _scaled_points(points: list[tuple[float, float]], scale: int) -> list[tuple[int, int]]:
    return [(round(x * scale), round(y * scale)) for x, y in points]


def _rotate_quarter(
    points: list[tuple[float, float]], turns: int
) -> list[tuple[float, float]]:
    """围绕画布中心按顺时针 90 度旋转归一化坐标。"""

    rotated = points
    for _ in range(turns % 4):
        rotated = [(0.5 - (y - 0.5), 0.5 + (x - 0.5)) for x, y in rotated]
    return rotated


def _draw_tile(draw: ImageDraw.ImageDraw, canvas_size: int, icon_size: int) -> None:
    """绘制小尺寸专用的实色圆角底板，避免柔和阴影污染透明边缘。"""

    margin_px = 1 if icon_size <= 24 else max(1.5, icon_size * 0.045)
    radius_px = icon_size * 0.22
    box = tuple(round(value * SUPERSAMPLE) for value in (
        margin_px,
        margin_px,
        icon_size - margin_px,
        icon_size - margin_px,
    ))
    draw.rounded_rectangle(
        box,
        radius=round(radius_px * SUPERSAMPLE),
        fill="white",
        outline=TILE_BORDER,
        width=SUPERSAMPLE,
    )


def _draw_assistant_mark(
    draw: ImageDraw.ImageDraw, canvas_size: int, icon_size: int
) -> None:
    """绘制加粗的四向 AI 罗盘；坐标专为 16–64 像素图层设计。"""

    top_arrow = [
        (0.50, 0.11),
        (0.68, 0.38),
        (0.59, 0.47),
        (0.50, 0.35),
        (0.41, 0.47),
        (0.32, 0.38),
    ]
    for turns, color in ((0, BLUE), (1, ORANGE), (2, CYAN), (3, RED)):
        points = _rotate_quarter(top_arrow, turns)
        draw.polygon(_scaled_points(points, canvas_size), fill=color)

    # 28 像素以下不再绘制八角星的细尖角，改为实心菱形，避免中心只剩
    # 一两个半透明像素。较大图层继续保留正式方案 A 的智能星芒。
    if icon_size <= 28:
        draw.polygon(
            _scaled_points(
                [(0.50, 0.42), (0.58, 0.50), (0.50, 0.58), (0.42, 0.50)],
                canvas_size,
            ),
            fill=GOLD,
        )
        return

    star_points: list[tuple[float, float]] = []
    for index in range(8):
        angle = -math.pi / 2 + index * math.pi / 4
        radius = 0.088 if index % 2 == 0 else 0.030
        star_points.append((
            0.5 + math.cos(angle) * radius,
            0.5 + math.sin(angle) * radius,
        ))
    draw.polygon(_scaled_points(star_points, canvas_size), fill=GOLD)


def _draw_login_mark(
    draw: ImageDraw.ImageDraw, canvas_size: int, icon_size: int
) -> None:
    """绘制粗线连接环，小尺寸省略无助识别的微小装饰。"""

    arc_box = _scaled_points([(0.18, 0.18), (0.82, 0.82)], canvas_size)
    ring_width = max(SUPERSAMPLE * 2, round(icon_size * 0.105 * SUPERSAMPLE))
    draw.arc((*arc_box[0], *arc_box[1]), 35, 180, fill=CYAN, width=ring_width)
    draw.arc((*arc_box[0], *arc_box[1]), 180, 325, fill=BLUE, width=ring_width)
    draw.polygon(
        _scaled_points([(0.75, 0.41), (0.87, 0.50), (0.75, 0.59)], canvas_size),
        fill=CYAN,
    )

    first = (0.37, 0.40)
    second = (0.58, 0.62)
    node_radius = 0.075
    connection_width = max(SUPERSAMPLE, round(icon_size * 0.065 * SUPERSAMPLE))
    draw.line(
        _scaled_points([first, second], canvas_size),
        fill=ORANGE,
        width=connection_width,
    )
    for (center_x, center_y), color in ((first, RED), (second, ORANGE)):
        draw.ellipse(
            (
                round((center_x - node_radius) * canvas_size),
                round((center_y - node_radius) * canvas_size),
                round((center_x + node_radius) * canvas_size),
                round((center_y + node_radius) * canvas_size),
            ),
            fill=color,
        )

    if icon_size >= 32:
        dot_radius = 0.018
        for center_x in (0.66, 0.71, 0.76):
            draw.ellipse(
                (
                    round((center_x - dot_radius) * canvas_size),
                    round((0.50 - dot_radius) * canvas_size),
                    round((center_x + dot_radius) * canvas_size),
                    round((0.50 + dot_radius) * canvas_size),
                ),
                fill=CYAN,
            )


def _render_small_icon(stem: str, size: int) -> Image.Image:
    """按目标像素尺寸渲染，避免把复杂 256 像素图机械缩小。"""

    canvas_size = size * SUPERSAMPLE
    image = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    _draw_tile(draw, canvas_size, size)
    if stem == "boss_assistant":
        _draw_assistant_mark(draw, canvas_size, size)
    elif stem == "boss_login":
        _draw_login_mark(draw, canvas_size, size)
    else:
        raise ValueError(f"未知图标：{stem}")

    # 几何图标采用面积平均缩小；相比 Lanczos，不会在高对比边缘产生额外
    # 的振铃和柔化，同时仍保留一像素级抗锯齿。
    image = image.resize((size, size), Image.Resampling.BOX)
    alpha = image.getchannel("A").point(lambda value: 0 if value < 12 else value)
    image.putalpha(alpha)
    return image


def _icon_frames(stem: str, source: Path) -> list[Image.Image]:
    return [
        _render_small_icon(stem, size)
        if size in SMALL_ICON_SIZES
        else _prepare_icon(source, size=size)
        for size in ICON_SIZES
    ]


def _save(stem: str) -> None:
    source = ICON_DIR / f"{stem}.png"
    target = ICON_DIR / f"{stem}.ico"
    if not source.is_file():
        raise FileNotFoundError(f"缺少图标源图：{source}")

    frames = _icon_frames(stem, source)
    frames[-1].save(
        target,
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES],
        append_images=frames[:-1],
    )
    print(f"已生成：{target}（{len(ICON_SIZES)} 个独立优化尺寸）")


def main() -> None:
    _save("boss_assistant")
    _save("boss_login")


if __name__ == "__main__":
    main()
