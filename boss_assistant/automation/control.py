"""线程安全的自动化暂停、继续和停止控制。"""

from __future__ import annotations

import threading
from typing import Callable


class AutomationStopRequested(RuntimeError):
    """用户请求在下一个安全控制点停止自动化。"""


class AutomationControl:
    """由 GUI 线程发出控制请求，由自动化工作线程在安全点响应。"""

    def __init__(self) -> None:
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._pause_notified = False
        self._on_pause: Callable[[], None] | None = None
        self._on_resume: Callable[[], None] | None = None
        # 竞态补偿回调：当 pause 与 resume 都落在两个控制点之间时，on_pause/on_resume
        # 都不会触发，但用户暂停期间改过的设置仍需生效——此时只补“应用设置”，不做
        # 断点页面恢复（那种情形下工作线程从未真正停下，仍在自己的页面上继续）。
        self._apply_settings: Callable[[], None] | None = None
        # sticky 标记：发生过一次 pause 但可能还没走完整的恢复通知。resume 不清它，
        # 只有真正应用过（正常恢复或补偿）才清，避免电平事件被 resume 抹平后丢设置。
        self._pause_pending = False
        # 暂停期间用户在界面上改过的设置；继续时由工作线程取出并应用。
        self._pending_settings: dict[str, object] | None = None

    @property
    def paused(self) -> bool:
        return not self._resume_event.is_set() and not self._stop_event.is_set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def set_pause_callbacks(
        self,
        *,
        on_pause: Callable[[], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        apply_settings: Callable[[], None] | None = None,
    ) -> None:
        with self._lock:
            self._on_pause = on_pause
            self._on_resume = on_resume
            self._apply_settings = apply_settings

    def set_pending_settings(self, settings: dict[str, object] | None) -> None:
        """由 GUI 线程写入暂停期间修改后的设置。"""

        with self._lock:
            self._pending_settings = settings

    def take_pending_settings(self) -> dict[str, object] | None:
        """由工作线程取出并清空；同一份设置只会被应用一次。"""

        with self._lock:
            settings = self._pending_settings
            self._pending_settings = None
            return settings

    def pause(self) -> bool:
        if self._stop_event.is_set() or not self._resume_event.is_set():
            return False
        self._resume_event.clear()
        with self._lock:
            # sticky：即使紧接着就 resume，也要留下“发生过暂停”的痕迹，供下一个
            # 控制点补应用设置。resume 不会清它，只有应用过之后才清。
            self._pause_pending = True
        return True

    def resume(self) -> bool:
        if self._stop_event.is_set() or self._resume_event.is_set():
            return False
        self._resume_event.set()
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self._resume_event.set()

    def wait_if_paused(self) -> None:
        while True:
            if self._stop_event.is_set():
                raise AutomationStopRequested("用户已请求停止")
            if self._resume_event.is_set():
                self._apply_missed_settings()
                return

            with self._lock:
                should_notify = not self._pause_notified
                if should_notify:
                    self._pause_notified = True
                on_pause = self._on_pause
            if should_notify:
                if on_pause:
                    on_pause()

            while not self._resume_event.wait(0.1):
                if self._stop_event.is_set():
                    raise AutomationStopRequested("用户已请求停止")
            if self._stop_event.is_set():
                raise AutomationStopRequested("用户已请求停止")

            with self._lock:
                should_notify = self._pause_notified
                if should_notify:
                    self._pause_notified = False
                on_resume = self._on_resume
                # 初始化阶段还没有注册 runner 回调时，不能消费 sticky 暂停标记；
                # 否则 GUI 已提交的新设置会永久留在 pending 中却再也没人应用。
                if should_notify and on_resume:
                    self._pause_pending = False
            if should_notify and on_resume:
                on_resume()
            # 恢复回调可能因页面恢复失败再次暂停，不能放行一次错误页面操作。
            if self._resume_event.is_set():
                return

    def _apply_missed_settings(self) -> None:
        """竞态补偿：仅在确有一次未消费的暂停时，补一次“应用设置”。

        用 take-once 的方式清除 sticky 标记；即便与正常恢复路径叠加，
        settings_applier 内部也会因 pending 设置已被取空而幂等，不会重复应用。
        """

        with self._lock:
            pending = self._pause_pending
            apply_settings = self._apply_settings
            if pending and apply_settings:
                self._pause_pending = False
        if pending and apply_settings:
            apply_settings()
