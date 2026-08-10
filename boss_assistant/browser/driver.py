"""Edge 浏览器驱动：封装 Selenium，提供 Boss直聘 Web 端所需的最小操作集。

对应 Android 端的 ``AdbClient``：Android 靠 adb 点坐标、发按键、逐字输入；Web 端
靠 Selenium 定位 DOM、点击元素、逐字 send_keys。两者对上层运行器暴露的语义一致：
随机延迟由运行器负责，本层只提供“点一下 / 逐字输入 / 读文本 / 滚动 / 等待”这些原子
动作，并复刻招呼语逐字输入的自然节奏（普通字 0.06-0.22s 三段带、标点 0.28-0.62s）。
"""

from __future__ import annotations

import os
import random
import sys
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webelement import WebElement
import websocket

from boss_assistant.paths import runtime_root


class BrowserError(RuntimeError):
    """浏览器启动、导航或元素操作失败，继续操作可能产生误点击。"""


class ElementNotFoundError(BrowserError):
    """在超时时间内没有等到目标元素。"""


class LoginRequiredError(BrowserError):
    """当前处于未登录状态，需要用户先在浏览器里完成登录。"""


# Boss直聘 Web 端关键地址。
ZHIPIN_HOST = "www.zhipin.com"
GEEK_JOBS_URL = "https://www.zhipin.com/web/geek/jobs"
GEEK_LOGIN_URL = "https://www.zhipin.com/"
# 未登录时 /web/geek/jobs 会被重定向到登录页（根域名或 /web/user/ 开头）。
LOGIN_URL_MARKERS = ("/web/user/", "/login", "login.html")
DEBUG_PORT_FILE = "BossDebugPort"
_REMOTE_DEBUG_PORT_RE = re.compile(r'"?--remote-debugging-port=(\d+)"?')
_USER_DATA_DIR_RE = re.compile(
    r'"--user-data-dir=([^"]+)"|--user-data-dir=(?:"([^"]+)"|([^\s"]+))'
)


@dataclass(frozen=True)
class EdgeDebugTarget:
    """一个当前 Edge 调试端口中打开的页面。"""

    debugger_address: str
    url: str
    title: str
    target_id: str
    source: str


def default_edge_user_data_dir() -> Path:
    """返回当前 Windows 用户的 Edge 用户数据目录。"""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "Microsoft" / "Edge" / "User Data"
    return Path.home() / "AppData" / "Local" / "Microsoft" / "Edge" / "User Data"


def _project_root() -> Path:
    """源码与冻结运行时统一返回外置数据所在根目录。"""

    return runtime_root()


def boss_edge_user_data_dir() -> Path:
    """返回 Boss 自动化专用的账号 Profile 镜像目录。"""

    configured = os.environ.get("BOSS_EDGE_USER_DATA_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return _project_root() / "data" / "edge_profile_boss"


def _read_debugger_address_from_user_data_dir(user_data_dir: Path) -> str | None:
    # open_login_edge.py 使用显式的正整数端口，避免 Chromium 在
    # --remote-debugging-port=0 下把 navigator.webdriver 标记为 true。
    # 兼容旧版本由 Edge 自动写入的 DevToolsActivePort。
    for filename in (DEBUG_PORT_FILE, "DevToolsActivePort"):
        port_file = user_data_dir / filename
        try:
            port = port_file.read_text(encoding="utf-8").splitlines()[0].strip()
        except (IndexError, OSError):
            continue
        if not port.isdigit() or port == "0":
            continue
        address = f"127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(
                f"http://{address}/json/version",
                timeout=1.5,
            ):
                return address
        except OSError:
            continue
    return None


def _running_edge_command_lines() -> list[str]:
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process -Filter \"name = 'msedge.exe'\" "
                    "| ForEach-Object { $_.CommandLine }"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            # GUI/--windowed 进程启动 powershell.exe 时，若不显式禁止创建
            # 控制台，Windows 会短暂显示空终端窗口。该函数会被 GUI 的
            # 求职意向探针周期调用，因此必须从进程创建层彻底隐藏。
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _extract_user_data_dir(command_line: str) -> Path | None:
    match = _USER_DATA_DIR_RE.search(command_line)
    if not match:
        return None
    value = next(group for group in match.groups() if group)
    try:
        return Path(value).expanduser().resolve()
    except OSError:
        return None


def _project_edge_user_data_dirs() -> list[Path]:
    data_dir = _project_root() / "data"
    if not data_dir.is_dir():
        return []
    return [
        path.resolve()
        for path in data_dir.glob("edge_profile*")
        if path.is_dir()
    ]


def _candidate_debugger_endpoints() -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(address: str | None, source: str) -> None:
        if not address:
            return
        normalized = address.removeprefix("http://").removeprefix("https://")
        if normalized in seen:
            return
        seen.add(normalized)
        candidates.append((normalized, source))

    add(os.environ.get("BOSS_EDGE_DEBUGGER_ADDRESS"), "BOSS_EDGE_DEBUGGER_ADDRESS")

    user_data_dirs: list[Path] = []
    for command_line in _running_edge_command_lines():
        port_match = _REMOTE_DEBUG_PORT_RE.search(command_line)
        if port_match and port_match.group(1) != "0":
            add(f"127.0.0.1:{port_match.group(1)}", "Edge 进程参数")
        user_data_dir = _extract_user_data_dir(command_line)
        if user_data_dir is not None:
            user_data_dirs.append(user_data_dir)

    user_data_dirs.extend(
        [
            boss_edge_user_data_dir(),
            default_edge_user_data_dir(),
            *_project_edge_user_data_dirs(),
        ]
    )
    seen_dirs: set[Path] = set()
    for user_data_dir in user_data_dirs:
        if user_data_dir in seen_dirs:
            continue
        seen_dirs.add(user_data_dir)
        add(
            _read_debugger_address_from_user_data_dir(user_data_dir),
            f"DevToolsActivePort:{user_data_dir}",
        )
    return candidates


def discover_edge_debug_targets(
    *,
    host_filter: str | None = None,
) -> list[EdgeDebugTarget]:
    """发现当前可接管 Edge 中已经打开的页面。

    普通手动打开、没有远程调试端口的 Edge 无法被 Selenium 接管；这里仅返回
    当前系统中已经暴露 DevTools 调试端口的 Edge 页面。
    """

    targets: list[EdgeDebugTarget] = []
    seen_target_ids: set[tuple[str, str]] = set()
    for address, source in _candidate_debugger_endpoints():
        try:
            with urllib.request.urlopen(
                f"http://{address}/json/list",
                timeout=1.5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict) or item.get("type") != "page":
                continue
            url = str(item.get("url") or "")
            if host_filter and host_filter not in url:
                continue
            target_id = str(item.get("id") or "")
            key = (address, target_id)
            if key in seen_target_ids:
                continue
            seen_target_ids.add(key)
            targets.append(
                EdgeDebugTarget(
                    debugger_address=address,
                    url=url,
                    title=str(item.get("title") or ""),
                    target_id=target_id,
                    source=source,
                )
            )
    return targets


class _CdpClient:
    """最小 CDP 客户端。

    不经过 EdgeDriver，也不启用 DOM/Debugger 等调试域；Boss 页面会主动关闭带
    Selenium/Playwright 注入标记的标签页，而 Runtime/Input 这组最小命令不会改写页面
    的 JavaScript 环境。
    """

    def __init__(self, web_socket_url: str) -> None:
        self._socket = websocket.create_connection(
            web_socket_url,
            timeout=10,
            suppress_origin=True,
        )
        self._next_id = 0
        self._lock = threading.RLock()

    def call(self, method: str, params: dict[str, object] | None = None) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            payload = {
                "id": request_id,
                "method": method,
                "params": params or {},
            }
            try:
                self._socket.send(json.dumps(payload, ensure_ascii=False))
                while True:
                    response = json.loads(self._socket.recv())
                    if response.get("id") != request_id:
                        continue
                    if "error" in response:
                        raise BrowserError(
                            f"CDP {method} 执行失败：{response['error']}"
                        )
                    result = response.get("result")
                    return result if isinstance(result, dict) else {}
            except (OSError, websocket.WebSocketException, json.JSONDecodeError) as exc:
                raise BrowserError(f"Boss 页面连接已断开：{exc}") from exc

    def close(self) -> None:
        try:
            self._socket.close()
        except (OSError, websocket.WebSocketException):
            pass


@dataclass(frozen=True)
class _CdpElement:
    browser: "EdgeBrowser"
    kind: str
    locator: str
    index: int = 0

    @property
    def text(self) -> str:
        return str(
            self.browser._element_value(  # noqa: SLF001
                self,
                "(el) => (el.innerText || el.textContent || '').trim()",
                "",
            )
            or ""
        )

    def is_displayed(self) -> bool:
        return bool(
            self.browser._element_value(  # noqa: SLF001
                self,
                """
                (el) => {
                  const style = getComputedStyle(el);
                  const rect = el.getBoundingClientRect();
                  return style.display !== 'none' && style.visibility !== 'hidden'
                    && Number(style.opacity || 1) !== 0
                    && rect.width > 0 && rect.height > 0;
                }
                """,
                False,
            )
        )

    def is_enabled(self) -> bool:
        """Return whether a control can currently accept a user action."""

        return bool(
            self.browser._element_value(  # noqa: SLF001
                self,
                """
                (el) => {
                  const style = getComputedStyle(el);
                  const classes = String(el.className || '').toLowerCase();
                  return !el.disabled
                    && el.getAttribute('aria-disabled') !== 'true'
                    && !classes.split(/\\s+/).includes('disabled')
                    && !classes.split(/\\s+/).includes('unable')
                    && style.pointerEvents !== 'none';
                }
                """,
                False,
            )
        )

    def get_attribute(self, name: str) -> str | None:
        value = self.browser._element_value(  # noqa: SLF001
            self,
            "(el, name) => el.getAttribute(name)",
            None,
            name,
        )
        return None if value is None else str(value)

    def click(self) -> None:
        self.browser.click(self)

    def clear(self) -> None:
        self.browser.clear_editor(self)

    def send_keys(self, *values: object) -> None:
        text = "".join(str(value) for value in values)
        if text == "\n":
            self.browser.press_enter(self)
            return
        self.browser._focus_element(self)  # noqa: SLF001
        for char in text:
            self.browser._cdp_call(  # noqa: SLF001
                "Input.insertText",
                {"text": char},
            )


class _CdpDriverFacade:
    """保留 runner 对 browser.driver 的少量兼容访问。"""

    def __init__(self, browser: "EdgeBrowser") -> None:
        self.browser = browser

    @property
    def current_url(self) -> str:
        return self.browser.current_url

    @property
    def window_handles(self) -> list[str]:
        return [target.target_id for target in self.browser._page_targets()]  # noqa: SLF001

    def get_cookies(self) -> list[dict]:
        result = self.browser._cdp_call(  # noqa: SLF001
            "Network.getCookies",
            {"urls": [self.browser.current_url]},
        )
        cookies = result.get("cookies")
        return list(cookies) if isinstance(cookies, list) else []


class EdgeBrowser:
    """受控的 Edge 浏览器会话。

    使用独立的用户数据目录持久化登录态：用户扫码登录一次后，下次启动无需重复登录，
    也不干扰用户自己的 Edge 个人资料。
    """

    def __init__(
        self,
        *,
        user_data_dir: str | Path | None = None,
        profile_directory: str = "Default",
        driver_path: str | None = None,
        page_load_timeout: float = 40.0,
        headless: bool = False,
        attach_existing_only: bool = False,
        require_boss_page: bool = False,
    ) -> None:
        self.user_data_dir = Path(
            user_data_dir or boss_edge_user_data_dir()
        ).resolve()
        self.profile_directory = profile_directory
        self.driver_path = driver_path
        self.page_load_timeout = page_load_timeout
        self.headless = headless
        self.attach_existing_only = attach_existing_only
        self.require_boss_page = require_boss_page
        self.driver: _CdpDriverFacade | object | None = None
        self._attached_to_existing = False
        self._boss_main_handle: str | None = None
        self._debugger_address: str | None = None
        self._target_id: str | None = None
        self._client: _CdpClient | None = None

    # ------------------------------------------------------------------ 生命周期
    def _existing_debugger_address(self) -> str | None:
        """返回该专用 Profile 当前可连接的 Edge 调试地址。"""

        return _read_debugger_address_from_user_data_dir(self.user_data_dir)

    def _current_boss_target(self) -> EdgeDebugTarget | None:
        targets = discover_edge_debug_targets(host_filter=ZHIPIN_HOST)
        if not targets:
            return None
        # 优先接管真正的 Boss Web 页面；如果只有登录页，也返回它用于后续登录判定。
        for target in targets:
            if "/web/geek/" in target.url:
                return target
        return targets[0]

    def _target_payloads(self, address: str | None = None) -> list[dict]:
        endpoint = address or self._debugger_address
        if not endpoint:
            return []
        try:
            with urllib.request.urlopen(
                f"http://{endpoint}/json/list",
                timeout=1.5,
            ) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [
            item
            for item in payload
            if isinstance(item, dict) and item.get("type") == "page"
        ]

    def _page_targets(self) -> list[EdgeDebugTarget]:
        targets: list[EdgeDebugTarget] = []
        for item in self._target_payloads():
            targets.append(
                EdgeDebugTarget(
                    debugger_address=self._debugger_address or "",
                    url=str(item.get("url") or ""),
                    title=str(item.get("title") or ""),
                    target_id=str(item.get("id") or ""),
                    source="当前 CDP 会话",
                )
            )
        return targets

    def _connect_target(self, target: EdgeDebugTarget) -> None:
        payload = next(
            (
                item
                for item in self._target_payloads(target.debugger_address)
                if str(item.get("id") or "") == target.target_id
            ),
            None,
        )
        if not payload:
            raise BrowserError(f"Boss 标签页已关闭：{target.url}")
        web_socket_url = str(payload.get("webSocketDebuggerUrl") or "")
        if not web_socket_url:
            raise BrowserError("Boss 标签页没有可用的 CDP 连接地址")
        if self._client is not None:
            self._client.close()
        self._client = _CdpClient(web_socket_url)
        try:
            # Boss 的“立即沟通/继续沟通”会在页面被其它窗口遮住时读取
            # document.visibilityState。仅发送 CDP 鼠标事件并不会改变 hidden
            # 状态，因而可能出现按钮已收到点击、页面却不跳转。焦点仿真只改变
            # 当前 CDP 页面会话的前台语义，不会把 Edge 窗口抢到控制台前面。
            self._client.call(
                "Emulation.setFocusEmulationEnabled",
                {"enabled": True},
            )
        except BrowserError:
            self._client.close()
            self._client = None
            raise
        self._debugger_address = target.debugger_address
        self._target_id = target.target_id

    def _cdp_call(
        self,
        method: str,
        params: dict[str, object] | None = None,
    ) -> dict:
        if self._client is None:
            raise BrowserError("浏览器尚未启动")
        return self._client.call(method, params)

    def _evaluate(self, expression: str) -> object:
        result = self._cdp_call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        if result.get("exceptionDetails"):
            description = (
                result.get("exceptionDetails", {})
                .get("exception", {})
                .get("description", "未知 JavaScript 错误")
            )
            raise BrowserError(f"执行页面脚本失败：{description}")
        remote = result.get("result")
        if not isinstance(remote, dict):
            return None
        return remote.get("value")

    def start(self) -> None:
        if self.driver is not None:
            return
        target = self._current_boss_target()
        if target is None:
            raise LoginRequiredError(
                "未找到当前可接管的 Edge Boss 页面。\n\n"
                "请先用带远程调试端口的 Edge 打开 Boss直聘 页面并完成登录，"
                "然后再启动脚本。普通手动打开、没有调试端口的 Edge 无法被脚本接管。"
            )
        try:
            self._connect_target(target)
            self.driver = _CdpDriverFacade(self)
            self._attached_to_existing = True
            self._boss_main_handle = target.target_id
        except (BrowserError, OSError, websocket.WebSocketException) as exc:
            self._attached_to_existing = False
            raise BrowserError(
                "无法接管当前 Boss Edge 页面。请保持登录专用 Edge 打开后重试。\n"
                f"底层错误：{exc}"
            ) from exc
        if self.require_boss_page and not self.switch_to_boss_page():
            self.quit()
            raise LoginRequiredError(
                "当前可接管的 Edge 中没有打开 Boss直聘 页面。\n\n"
                "请先在当前 Edge 打开 Boss直聘 页面并完成登录，然后再启动脚本。"
            )

    def quit(self) -> None:
        if self._client is not None:
            self._client.close()
        self._client = None
        self.driver = None
        self._attached_to_existing = False
        self._boss_main_handle = None
        self._debugger_address = None
        self._target_id = None

    def _require(self):
        if self.driver is None:
            raise BrowserError("浏览器尚未启动")
        return self.driver

    # ------------------------------------------------------------------ 导航
    @property
    def current_url(self) -> str:
        if self._client is None:
            try:
                return str(self._require().current_url or "")
            except (AttributeError, WebDriverException):
                return ""
        target = next(
            (
                item
                for item in self._target_payloads()
                if str(item.get("id") or "") == self._target_id
            ),
            None,
        )
        return str(target.get("url") or "") if target else ""

    def open(self, url: str) -> None:
        self._require()
        try:
            self._cdp_call("Page.navigate", {"url": url})
        except BrowserError as exc:
            raise BrowserError(f"打开页面失败：{url}：{exc}") from exc

    def consolidate_windows(self) -> None:
        """Boss 有时会新开标签页展示详情/沟通；把焦点切到最新的标签页。"""

        self._require()
        targets = [
            target
            for target in self._page_targets()
            if ZHIPIN_HOST in target.url
        ]
        if not targets:
            return
        preferred = next(
            (target for target in targets if target.target_id != self._target_id),
            targets[0],
        )
        if preferred.target_id != self._target_id:
            self._connect_target(preferred)

    def switch_to_boss_page(self) -> bool:
        """切换到当前 Edge 中已经打开的 Boss 页面。"""

        self._require()
        targets = [
            target
            for target in self._page_targets()
            if ZHIPIN_HOST in target.url
        ]
        if not targets:
            return False
        target = next(
            (item for item in targets if item.target_id == self._target_id),
            None,
        )
        target = target or next(
            (item for item in targets if "/web/geek/" in item.url),
            targets[0],
        )
        if target.target_id != self._target_id:
            try:
                self._connect_target(target)
            except BrowserError:
                return False
        if self._boss_main_handle not in {
            item.target_id for item in targets
        }:
            self._boss_main_handle = target.target_id
        return True

    def close_extra_windows(self) -> None:
        """只关闭自动化过程中多开的 Boss 标签页，保留主 Boss 页和其它标签页。"""

        self._require()
        targets = self._page_targets()
        if not targets:
            return
        main_handle = (
            self._boss_main_handle
            if self._boss_main_handle in {target.target_id for target in targets}
            else None
        )
        if main_handle is None:
            main_handle = next(
                (
                    target.target_id
                    for target in targets
                    if ZHIPIN_HOST in target.url
                ),
                None,
            )
        if main_handle is None:
            return
        self._boss_main_handle = main_handle
        for target in targets:
            if target.target_id == main_handle or ZHIPIN_HOST not in target.url:
                continue
            try:
                with urllib.request.urlopen(
                    f"http://{self._debugger_address}/json/close/"
                    f"{urllib.parse.quote(target.target_id, safe='')}",
                    timeout=1.5,
                ):
                    pass
            except OSError:
                pass
        main_target = next(
            (target for target in targets if target.target_id == main_handle),
            None,
        )
        if main_target and main_target.target_id != self._target_id:
            self._connect_target(main_target)

    # ------------------------------------------------------------------ 登录判定
    def is_on_login_page(self) -> bool:
        url = self.current_url
        if any(marker in url for marker in LOGIN_URL_MARKERS):
            return True
        # Boss 已登录首页也会在 DOM 中预埋两个隐藏的手机号登录框。只有控件真实
        # 可见时才能据此判定未登录，否则会把已显示用户名的首页误判成登录页。
        phone_inputs = self.find_all_css("input[type='tel'][placeholder*='手机']")
        if any(_is_displayed(element) for element in phone_inputs):
            return True
        # 避免使用 [class*='login'] a：Boss 首页的外层容器类名会让几乎所有链接
        # 命中。这里仅检查明确的登录入口，并要求它当前可见。
        login_selectors = (
            "a[ka='header-login']",
            "a[href*='/web/user/'][ka*='login']",
            "a[href*='login'][ka*='login']",
        )
        for selector in login_selectors:
            for login_entry in self.find_all_css(selector):
                if _is_displayed(login_entry) and "登录" in self.text_of(login_entry):
                    return True
        visible_login_entry = self.find_clickable_by_text(
            ("登录/注册", "登录"),
            tags=("a", "button"),
        )
        if visible_login_entry is not None:
            return True
        return False

    def is_logged_in(self) -> bool:
        """判断 Boss 登录态；兼容登录后仍停留在官网首页的情况。"""

        if self.require_boss_page:
            self.switch_to_boss_page()
        url = self.current_url
        if not url or ZHIPIN_HOST not in url:
            return False
        if self.is_on_login_page():
            return False
        try:
            cookie_names = {
                item.get("name", "") for item in self._require().get_cookies()
            }
        except (AttributeError, WebDriverException):
            cookie_names = set()
        if {"zp_at", "wt2"}.issubset(cookie_names):
            return True
        if "/web/geek/" not in url:
            return False
        return not self.is_on_login_page()

    # ------------------------------------------------------------------ 查找
    def _locator_nodes_expression(self, kind: str, locator: str) -> str:
        encoded = json.dumps(locator, ensure_ascii=False)
        if kind == "xpath":
            return (
                "(()=>{const out=[];"
                f"const snap=document.evaluate({encoded},document,null,"
                "XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);"
                "for(let i=0;i<snap.snapshotLength;i++)out.push(snap.snapshotItem(i));"
                "return out;})()"
            )
        return f"Array.from(document.querySelectorAll({encoded}))"

    def _element_value(
        self,
        element: _CdpElement,
        function_source: str,
        default: object = None,
        *args: object,
    ) -> object:
        nodes = self._locator_nodes_expression(element.kind, element.locator)
        encoded_args = json.dumps(args, ensure_ascii=False)
        expression = (
            "(()=>{"
            f"const nodes={nodes};const el=nodes[{element.index}];"
            f"if(!el)return {json.dumps(default, ensure_ascii=False)};"
            f"return ({function_source})(el,...{encoded_args});"
            "})()"
        )
        try:
            return self._evaluate(expression)
        except BrowserError:
            return default

    def _locator_count(self, kind: str, locator: str) -> int:
        nodes = self._locator_nodes_expression(kind, locator)
        try:
            return int(self._evaluate(f"({nodes}).length") or 0)
        except (BrowserError, TypeError, ValueError):
            return 0

    def find_css(self, css: str) -> _CdpElement | None:
        self._require()
        return _CdpElement(self, "css", css) if self._locator_count("css", css) else None

    def find_all_css(self, css: str) -> list[_CdpElement]:
        self._require()
        return [
            _CdpElement(self, "css", css, index)
            for index in range(self._locator_count("css", css))
        ]

    def find_first_css(self, selectors: Sequence[str]) -> _CdpElement | None:
        """按候选顺序返回第一个命中的元素，容忍布局差异。"""

        for css in selectors:
            element = self.find_css(css)
            if element is not None:
                return element
        return None

    def find_all_first_css(self, selectors: Sequence[str]) -> list[_CdpElement]:
        """返回第一个能命中非空结果的候选选择器的全部元素。"""

        for css in selectors:
            elements = self.find_all_css(css)
            if elements:
                return elements
        return []

    def find_by_xpath(self, xpath: str) -> _CdpElement | None:
        self._require()
        return (
            _CdpElement(self, "xpath", xpath)
            if self._locator_count("xpath", xpath)
            else None
        )

    def find_all_by_xpath(self, xpath: str) -> list[_CdpElement]:
        self._require()
        return [
            _CdpElement(self, "xpath", xpath, index)
            for index in range(self._locator_count("xpath", xpath))
        ]

    def find_clickable_by_text(
        self, texts: Sequence[str], *, tags: Sequence[str] = ("a", "button", "span", "div", "li")
    ) -> _CdpElement | None:
        """按可见文本精确匹配一个可点击元素（用于“职位”“立即沟通”“发送”等入口）。"""

        tag_union = " or ".join(f"self::{tag}" for tag in tags)
        for text in texts:
            xpath = (
                f"//*[({tag_union}) and normalize-space(text())={_xpath_literal(text)}]"
            )
            candidates = self.find_all_by_xpath(xpath)
            for element in candidates:
                if _is_displayed(element):
                    return element
        return None

    # ------------------------------------------------------------------ 等待
    def wait_for(
        self,
        predicate: Callable[["EdgeBrowser"], WebElement | bool | None],
        *,
        timeout: float,
        description: str,
        control_point: Callable[[], None] | None = None,
        poll: float = 0.4,
    ):
        """轮询 predicate，直到返回真值或超时；暂停期间不消耗超时。"""

        deadline = time.monotonic() + timeout
        last_exc: Exception | None = None
        while time.monotonic() < deadline:
            started = time.monotonic()
            if control_point:
                control_point()
            deadline += time.monotonic() - started
            try:
                result = predicate(self)
                if result:
                    return result
            except (
                BrowserError,
                StaleElementReferenceException,
                WebDriverException,
            ) as exc:
                last_exc = exc
            time.sleep(poll)
        suffix = f"；最后错误：{last_exc}" if last_exc else ""
        raise ElementNotFoundError(f"等待{description}超时{suffix}")

    # ------------------------------------------------------------------ 交互
    def _focus_element(self, element: _CdpElement) -> None:
        self._element_value(
            element,
            "(el) => {el.scrollIntoView({block:'center'});el.focus();return true;}",
            False,
        )

    def _element_rect(self, element: _CdpElement) -> dict | None:
        value = self._element_value(
            element,
            """
            (el) => {
              el.scrollIntoView({block:'center'});
              const r=el.getBoundingClientRect();
              if(r.width<=0||r.height<=0)return null;
              return {x:r.left+r.width/2,y:r.top+r.height/2};
            }
            """,
            None,
        )
        return value if isinstance(value, dict) else None

    def click(self, element: _CdpElement, *, description: str = "") -> None:
        self._require()
        rect = self._element_rect(element)
        if not rect:
            raise BrowserError(
                f"点击元素失败{('：' + description) if description else ''}：元素不可见"
            )
        coordinates = {"x": float(rect["x"]), "y": float(rect["y"])}
        try:
            self._cdp_call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", **coordinates},
            )
            self._cdp_call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "button": "left",
                    "clickCount": 1,
                    **coordinates,
                },
            )
            self._cdp_call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "button": "left",
                    "clickCount": 1,
                    **coordinates,
                },
            )
        except BrowserError as exc:
            raise BrowserError(
                f"点击元素失败{('：' + description) if description else ''}：{exc}"
            ) from exc

    def hover(self, element: _CdpElement, *, description: str = "") -> None:
        """把真实鼠标移动到 DOM 元素中心，用于显示仅在 ``:hover`` 时出现的控件。"""

        self._require()
        rect = self._element_rect(element)
        if not rect:
            raise BrowserError(
                f"悬停元素失败{('：' + description) if description else ''}：元素不可见"
            )
        try:
            self._cdp_call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": float(rect["x"]),
                    "y": float(rect["y"]),
                },
            )
        except BrowserError as exc:
            raise BrowserError(
                f"悬停元素失败{('：' + description) if description else ''}：{exc}"
            ) from exc

    def context_click(self, element: _CdpElement, *, description: str = "") -> None:
        """在 DOM 元素中心触发右键菜单；坐标每次由实时元素边界计算。"""

        self._require()
        rect = self._element_rect(element)
        if not rect:
            raise BrowserError(
                f"右键元素失败{('：' + description) if description else ''}：元素不可见"
            )
        coordinates = {"x": float(rect["x"]), "y": float(rect["y"])}
        try:
            self._cdp_call(
                "Input.dispatchMouseEvent",
                {"type": "mouseMoved", **coordinates},
            )
            self._cdp_call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "button": "right",
                    "clickCount": 1,
                    **coordinates,
                },
            )
            self._cdp_call(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "button": "right",
                    "clickCount": 1,
                    **coordinates,
                },
            )
        except BrowserError as exc:
            raise BrowserError(
                f"右键元素失败{('：' + description) if description else ''}：{exc}"
            ) from exc

    def type_stream(
        self,
        element: _CdpElement,
        text: str,
        *,
        control_point: Callable[[], None] | None = None,
        char_delay_min: float = 0.06,
        char_delay_max: float = 0.22,
        punct_delay_min: float = 0.28,
        punct_delay_max: float = 0.62,
    ) -> None:
        """逐字符输入，模拟真人打字节奏；标点处停顿更长。

        复刻 Android ``input_text_stream``：普通字符在三段互不重叠的区间里随机取值，
        并避免与上一次落在同一段，让相邻字符节奏有明显区别；标点使用更长的停顿。
        """

        if not text:
            return
        cdp_element = isinstance(element, _CdpElement)
        if cdp_element:
            self._focus_element(element)
        span = char_delay_max - char_delay_min
        bands = (
            (char_delay_min, char_delay_min + span * 0.25),
            (char_delay_min + span * 0.40, char_delay_min + span * 0.65),
            (char_delay_min + span * 0.80, char_delay_max),
        )
        previous_band = -1
        punctuation = set("，。！？；：、,.!?;:… ")

        def controlled_sleep(seconds: float) -> None:
            deadline = time.monotonic() + seconds
            while True:
                if control_point:
                    control_point()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                time.sleep(min(0.05, remaining))

        for index, char in enumerate(text):
            if control_point:
                control_point()
            try:
                if cdp_element:
                    self._cdp_call("Input.insertText", {"text": char})
                else:
                    element.send_keys(char)
            except (BrowserError, StaleElementReferenceException, WebDriverException) as exc:
                raise BrowserError(f"逐字输入失败：{exc}") from exc
            if index >= len(text) - 1:
                break
            if char in punctuation:
                delay = random.uniform(punct_delay_min, punct_delay_max)
            else:
                choices = [i for i in range(len(bands)) if i != previous_band]
                band_index = random.choice(choices)
                previous_band = band_index
                delay = random.uniform(*bands[band_index])
            controlled_sleep(delay)

    def set_value(self, element: _CdpElement, text: str) -> None:
        """整段写入并触发 input 事件；用于非逐字场景（一般不用于招呼语）。"""

        changed = self._element_value(
            element,
            """
            (el, value) => {
              el.focus();
              if(el.isContentEditable){el.textContent=value;}else{el.value=value;}
              el.dispatchEvent(new Event('input',{bubbles:true}));
              return true;
            }
            """,
            False,
            text,
        )
        if not changed:
            raise BrowserError("整段写入失败：输入框已失效")

    def editor_value(self, element: _CdpElement) -> str:
        """Read the live value of an input, textarea, or contenteditable editor."""

        value = self._element_value(
            element,
            """
            (el) => el.isContentEditable
              ? (el.innerText || el.textContent || '')
              : (('value' in el ? el.value : el.textContent) || '')
            """,
            "",
        )
        return str(value or "")

    def clear_editor(self, element: _CdpElement) -> None:
        """清空输入框/contenteditable。"""

        self._element_value(
            element,
            """
            (el) => {
              el.focus();
              if (el.isContentEditable) {
                el.innerHTML = '';
              } else if ('value' in el) {
                el.value = '';
              }
              el.dispatchEvent(new Event('input', {bubbles: true}));
              return true;
            }
            """,
            False,
        )

    def press_enter(self, element: _CdpElement) -> None:
        self._focus_element(element)
        self._cdp_call(
            "Input.dispatchKeyEvent",
            {
                "type": "rawKeyDown",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            },
        )
        self._cdp_call(
            "Input.dispatchKeyEvent",
            {
                "type": "char",
                "text": "\r",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
            },
        )
        self._cdp_call(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": "Enter",
                "code": "Enter",
                "windowsVirtualKeyCode": 13,
                "nativeVirtualKeyCode": 13,
            },
        )

    def text_of(self, element: _CdpElement) -> str:
        try:
            return (element.text or "").strip()
        except BrowserError:
            return ""

    def scroll_by(self, dx: int, dy: int) -> None:
        try:
            self.js("window.scrollBy(arguments[0], arguments[1]);", dx, dy)
        except BrowserError:
            pass

    def scroll_into_view(self, element: _CdpElement) -> None:
        self._element_value(
            element,
            "(el) => {el.scrollIntoView({block:'center'});return true;}",
            False,
        )

    def scroll_container(self, element: _CdpElement, dy: int) -> None:
        """在指定滚动容器内部滚动（岗位列表通常是内部滚动区，而非 window）。"""

        self._element_value(
            element,
            "(el, amount) => {el.scrollTop += amount;return true;}",
            False,
            dy,
        )

    def js(self, script: str, *args: object) -> object:
        self._require()
        encoded_args = json.dumps(args, ensure_ascii=False)
        expression = (
            "(()=>{"
            f"const __args={encoded_args};"
            f"return (function(){{{script}\n}}).apply(null,__args);"
            "})()"
        )
        return self._evaluate(expression)

    def page_text(self) -> str:
        try:
            return str(self.js("return document.body ? document.body.innerText : '';") or "")
        except BrowserError:
            return ""

    def outer_html(self) -> str:
        try:
            return str(
                self.js("return document.documentElement.outerHTML;") or ""
            )
        except BrowserError:
            return ""


def _xpath_literal(value: str) -> str:
    """把任意字符串安全地嵌入 XPath（处理其中的引号）。"""

    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ", '\"', ".join(f'"{part}"' for part in parts) + ")"


def _is_displayed(element: WebElement) -> bool:
    try:
        return element.is_displayed()
    except (StaleElementReferenceException, WebDriverException):
        return False
