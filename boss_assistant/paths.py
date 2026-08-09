"""源码、PyInstaller 与 Nuitka onefile 共用的运行时路径解析。"""

from __future__ import annotations

import sys
from pathlib import Path


def _compiled_containing_dir() -> Path | None:
    """返回 Nuitka 可执行文件所在目录；源码运行时返回 ``None``。"""

    compiled = globals().get("__compiled__")
    containing_dir = getattr(compiled, "containing_dir", None)
    if containing_dir:
        return Path(containing_dir).resolve()
    return None


def is_frozen_runtime() -> bool:
    """同时识别 PyInstaller 与 Nuitka 编译运行时。"""

    return bool(getattr(sys, "frozen", False) or _compiled_containing_dir())


def runtime_root() -> Path:
    """返回外置 ``config/``、``data/`` 与 ``resume_inbox/`` 的根目录。"""

    nuitka_root = _compiled_containing_dir()
    if nuitka_root is not None:
        return nuitka_root
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def bundled_icon(name: str) -> Path:
    """返回随程序打包的窗口 ICO；源码运行时读取正式图标目录。"""

    pyinstaller_root = getattr(sys, "_MEIPASS", None)
    if pyinstaller_root:
        return Path(pyinstaller_root) / "icons" / name
    if _compiled_containing_dir() is not None:
        # Nuitka onefile 的内置数据位于临时解压目录；``__file__`` 指向该目录。
        return Path(__file__).resolve().parent / "_assets" / name
    return runtime_root() / "assets" / "icons" / "official" / name
