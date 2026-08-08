"""启动一个不会随探针退出而关闭的 Boss 登录专用 Edge。"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

if getattr(sys, "frozen", False):
    # PyInstaller 冻结运行时：exe 位于项目根目录，模块已内嵌，无需路径注入。
    WORKSPACE = Path(sys.executable).resolve().parent
else:
    WORKSPACE = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(WORKSPACE))

from boss_assistant.browser.driver import (
    DEBUG_PORT_FILE,
    GEEK_LOGIN_URL,
    ZHIPIN_HOST,
    _read_debugger_address_from_user_data_dir,
    discover_edge_debug_targets,
)


DEFAULT_PROFILE_DIR = WORKSPACE / "data" / "edge_profile_boss"
EDGE_CANDIDATES = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def locate_edge() -> Path:
    for candidate in EDGE_CANDIDATES:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("未找到 Microsoft Edge 可执行文件")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="打开 Boss 登录专用 Edge")
    parser.add_argument(
        "--profile-dir",
        default=os.environ.get("BOSS_EDGE_LOGIN_PROFILE_DIR", str(DEFAULT_PROFILE_DIR)),
        help="Edge 用户资料目录；默认使用本机自动生成的 data/edge_profile_boss",
    )
    parser.add_argument(
        "--force-new",
        action="store_true",
        help="即使检测到其它 Boss Edge 页面，也强制打开指定资料目录的新窗口",
    )
    return parser


def _targets_for_profile(profile_dir: Path) -> list[dict[str, str]]:
    address = _read_debugger_address_from_user_data_dir(profile_dir)
    if not address:
        return []
    try:
        with urllib.request.urlopen(f"http://{address}/json/list", timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError):
        return []
    targets: list[dict[str, str]] = []
    for item in payload if isinstance(payload, list) else []:
        if not isinstance(item, dict) or item.get("type") != "page":
            continue
        url = str(item.get("url") or "")
        if ZHIPIN_HOST not in url:
            continue
        targets.append(
            {
                "address": address,
                "url": url,
                "title": str(item.get("title") or ""),
            }
        )
    return targets


def _configured_debug_port() -> int | None:
    value = os.environ.get("BOSS_EDGE_DEBUG_PORT", "").strip()
    if not value:
        return None
    if not value.isdigit() or not 1024 <= int(value) <= 65535:
        raise ValueError("BOSS_EDGE_DEBUG_PORT 必须是 1024-65535 之间的整数")
    return int(value)


def _available_debug_port() -> int:
    configured = _configured_debug_port()
    if configured is not None:
        return configured
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_debugger(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=0.5,
            ):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def _open_boss_in_existing_edge(profile_dir: Path) -> bool:
    """待 Edge 主进程就绪后，向同一资料目录发送一次普通 URL 打开请求。"""

    try:
        completed = subprocess.run(
            [
                str(locate_edge()),
                f"--user-data-dir={profile_dir}",
                GEEK_LOGIN_URL,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=5.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _wait_for_boss_target(profile_dir: Path, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _targets_for_profile(profile_dir):
            return True
        time.sleep(0.2)
    return False


def _notify(title: str, message: str) -> None:
    """成功信息：命令行模式打印；窗口模式静默（双击启动不弹窗）。"""

    print(message)


def _notify_error(message: str) -> None:
    """错误提示：命令行打印；窗口模式弹消息框（启动失败必须让用户知道）。"""

    print(f"[失败] {message}")
    if getattr(sys, "frozen", False):
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        try:
            messagebox.showerror("Boss登录浏览器", f"[失败] {message}", parent=root)
        finally:
            root.destroy()


def main() -> None:
    if getattr(sys, "frozen", False):
        # 双击 exe 启动时把工作目录固定到 exe 所在目录（项目根）。
        os.chdir(WORKSPACE)
    try:
        _run_main()
    except RuntimeError as exc:
        _notify_error(str(exc))
        raise SystemExit(1)


def _run_main() -> None:
    args = _parser().parse_args()
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    existing_targets = _targets_for_profile(profile_dir)
    if existing_targets:
        target = existing_targets[0]
        _notify(
            "Boss登录浏览器",
            "已检测到可接管的 Boss Edge 页面，无需重复打开新窗口。\n"
            f"当前页面：{target['url']}\n"
            f"调试地址：{target['address']}\n"
            f"资料目录：{profile_dir}",
        )
        return
    existing_address = _read_debugger_address_from_user_data_dir(profile_dir)
    if existing_address:
        if _open_boss_in_existing_edge(profile_dir) and _wait_for_boss_target(
            profile_dir
        ):
            _notify(
                "Boss登录浏览器",
                "已在当前 Boss 专用 Edge 中打开登录页面。\n"
                f"当前页面：{GEEK_LOGIN_URL}\n"
                f"调试地址：{existing_address}\n"
                f"资料目录：{profile_dir}",
            )
            return
        raise RuntimeError(
            "已找到 Boss 专用 Edge，但无法在其中打开 Boss 页面。"
            "请关闭该专用 Edge 后重试。"
        )

    other_targets = discover_edge_debug_targets(host_filter=ZHIPIN_HOST)
    if other_targets and not args.force_new:
        target = other_targets[0]
        _notify(
            "Boss登录浏览器",
            "已检测到其它 Edge 资料目录中的 Boss 页面，未重复打开新窗口。\n"
            f"当前页面：{target.url}\n"
            f"调试地址：{target.debugger_address}\n"
            "如需切换到 Boss 专用资料目录，请先关闭该 Boss Edge 窗口后重试。\n"
            "确需并行打开时可加参数：--force-new",
        )
        return
    debug_port = _available_debug_port()
    # Chromium 会在 remote-debugging-port=0 时主动暴露自动化标记；Boss 的安全
    # 检查可能据此反复跳转。选择一个明确的本地端口，并写入资料目录供 GUI 发现。
    (profile_dir / DEBUG_PORT_FILE).write_text(
        f"{debug_port}\n",
        encoding="utf-8",
    )
    command = [
        str(locate_edge()),
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        f"--remote-debugging-port={debug_port}",
        "--remote-debugging-address=127.0.0.1",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-mode",
        "--start-maximized",
        "--new-window",
        "about:blank",
    ]
    creation_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    subprocess.Popen(
        command,
        creationflags=creation_flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_debugger(debug_port):
        raise RuntimeError(
            f"Edge 已启动，但调试端口 127.0.0.1:{debug_port} 未就绪。"
            "请关闭该专用 Edge 后重试。"
        )
    debugger_address = f"127.0.0.1:{debug_port}"
    if not _wait_for_boss_target(profile_dir, timeout=1.0):
        if not _open_boss_in_existing_edge(
            profile_dir
        ) or not _wait_for_boss_target(
            profile_dir
        ):
            raise RuntimeError(
                "Edge 已启动，但 Boss 页面未成功打开。"
                "请在当前专用 Edge 中手动打开 https://www.zhipin.com/ 后重试。"
            )
    _notify(
        "Boss登录浏览器",
        f"已启动 Boss 登录专用 Edge：{GEEK_LOGIN_URL}\n"
        f"调试地址：{debugger_address}\n"
        f"资料目录：{profile_dir}",
    )


if __name__ == "__main__":
    main()
