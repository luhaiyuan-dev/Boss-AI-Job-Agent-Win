"""从正式源图生成 Boss 求职助手的透明多尺寸 Windows 图标。

- Boss求职助手.exe：白色圆角底 + 四向 AI 罗盘 + 暖色智能星芒。
- Boss登录浏览器.exe：白色圆角底 + 蓝青连接环 + 红橙连接节点。

源图保存在 ``assets/icons/official/*.png``，产物为同目录下的 ``*.ico``。
用法：python tools/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ICON_DIR = PROJECT_ROOT / "assets" / "icons" / "official"
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


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


def _save(stem: str) -> None:
    source = ICON_DIR / f"{stem}.png"
    target = ICON_DIR / f"{stem}.ico"
    if not source.is_file():
        raise FileNotFoundError(f"缺少图标源图：{source}")

    image = _prepare_icon(source)
    image.save(
        target,
        format="ICO",
        sizes=[(size, size) for size in ICON_SIZES],
    )
    print(f"已生成：{target}（{len(ICON_SIZES)} 个尺寸）")


def main() -> None:
    _save("boss_assistant")
    _save("boss_login")


if __name__ == "__main__":
    main()
