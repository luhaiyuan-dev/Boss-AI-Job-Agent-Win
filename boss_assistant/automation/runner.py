"""Boss 推荐页的受控循环（Web 端）：读取意向、逐岗采集、匹配、返回与翻页。

与 Android 端 ``runner.py`` 保持相同的处理逻辑与状态机，只把“设备动作”从 adb 点坐标
换成最小原生 CDP 操作 DOM：
  - 读取求职期望 → 按 GUI 选择只投一条，或按目标公司数平均分配后顺序投递；
  - 逐张卡片：硬门槛初筛 → 大模型卡片初筛 → 进详情（Web 无“查看更多”，直接读全文）
    → 详情匹配与招呼语生成 → 周末休息校验 → 逐字填入/发送招呼语；
  - 每完成一家后只读“消息”旁红色数字；有未读才进入消息页处理；
  - 每次发送/返回后重新点击“职位/求职意向”回到正确的推荐页；
  - 操作间随机等待 1-2 秒，招呼语逐字输入并带自然停顿。
"""

from __future__ import annotations

import json
import random
import re
import time
import unicodedata
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

from boss_assistant.browser import (
    BrowserError,
    EdgeBrowser,
    ElementNotFoundError,
    LoginRequiredError,
)
from boss_assistant.browser.driver import GEEK_JOBS_URL, GEEK_LOGIN_URL
from boss_assistant.page import PageReadError
from boss_assistant.resume import ResumeData
from boss_assistant.storage import JobStore, JobStoreError
from boss_assistant.web import (
    WebSelectionError,
    align_detail_identity,
    click_expectation,
    click_positions_tab,
    expectation_is_active,
    extract_chat_conversations,
    extract_chat_messages,
    extract_chat_system_notes,
    extract_job_cards,
    find_chat_entry,
    find_conversation_open_target,
    find_conversation_operation_element,
    find_greeting_editor,
    find_resume_send_entry,
    find_send_button,
    parse_job_intents,
    read_current_chat_identity,
    read_current_chat_job_info,
    read_communication_quota_notice,
    read_message_unread_count,
    read_job_detail,
    resume_request_accept_button,
    select_job_card_inline,
)
from boss_assistant.web.selectors import find_resume_confirm_button

from .models import (
    AutomationConfig,
    AutomationPolicy,
    AutomationStats,
    ChatConversation,
    ChatJobInfo,
    ChatMessage,
    JobCard,
    JobExpectation,
    JobIntentData,
    MatchDecision,
)
from .control import AutomationControl, AutomationStopRequested
from .policy import card_rejection_reason, card_review_rejection_reason, contains_any
from .requirements import resume_degree_level, salary_meets, weekend_meets
from .review import (
    DEFAULT_RESUME_REPLY,
    JobReviewProvider,
    ReviewError,
    recruiter_requested_resume_card,
    resume_already_sent,
)


CHAT_URL = "https://www.zhipin.com/web/geek/chat"
RECENT_APPLICATION_DAYS = 30
CHAT_ENTRY_TEXTS = ("立即沟通", "继续沟通")
DAILY_COMMUNICATION_LIMIT_REASON = "已达到150沟通上限"
_HR_REJECTION_MARKERS = (
    "不合适",
    "不太合适",
    "不太适合",
    "不匹配",
    "不太匹配",
    "暂不考虑",
    "暂时不考虑",
    "不符合",
    "不完全一致",
    "不完全符合",
    "不完全吻合",
    "不能和您合作",
    "无法进入下一轮",
    "很遗憾",
    "很抱歉",
)
_CONTACT_REQUEST_MARKERS = (
    "交换微信",
    "添加微信",
    "提供微信",
    "发下微信",
    "发一下微信",
    "联系方式",
    "联系电话",
    "手机号",
    "手机号码",
    "电话号码",
)
_PAGE_LABELS = {
    "recommendations": "推荐岗位列表",
    "detail": "岗位详情页",
    "chat": "沟通页面",
    "messages": "消息会话列表",
}


def _hr_has_rejected(
    conversation: ChatConversation,
    messages: tuple[ChatMessage, ...],
) -> bool:
    """只看列表最新文案和我方最后回复后的HR消息，避免拿历史拒绝误判新会话。"""

    unanswered: list[str] = []
    for message in reversed(messages):
        if message.from_me:
            break
        unanswered.append(message.text)
    text = " ".join((conversation.last_message, *reversed(unanswered)))
    return any(marker in text for marker in _HR_REJECTION_MARKERS)


def _contact_request_detected(
    messages: tuple[ChatMessage, ...],
    system_notes: tuple[str, ...],
) -> bool:
    unanswered: list[str] = []
    for message in reversed(messages):
        if message.from_me:
            break
        unanswered.append(message.text)
    text = " ".join((*reversed(unanswered), *system_notes))
    if "已经发送给Boss" in text or "已发送给Boss" in text:
        return False
    return any(marker in text for marker in _CONTACT_REQUEST_MARKERS)


_CHAT_DELIVERY_PREFIX = re.compile(
    r"^(?:\[(?:送达|已读|未读)\]|(?:送达|已读|未读))"
)


def _chat_text_identity(value: str | None) -> str:
    """去掉展示状态与空白，只保留真实聊天正文用于发送确认。"""

    normalized = re.sub(r"\s+", "", value or "")
    return _CHAT_DELIVERY_PREFIX.sub("", normalized)


class BossAutomationError(RuntimeError):
    """自动化状态与预期页面不一致，继续操作可能产生误点击。"""


class AutomationStoppedError(RuntimeError):
    """用户主动停止；携带停止前已保存的统计和运行记录。"""

    def __init__(self, stats: AutomationStats, log_path: Path) -> None:
        self.stats = stats
        self.log_path = log_path
        self.cleanup_warning: str | None = None
        super().__init__("用户已停止本次自动化运行")


class DailyCommunicationLimitReachedError(RuntimeError):
    """Boss 已拒绝继续沟通；用于安全退出当前岗位和整轮任务。"""

    def __init__(self) -> None:
        super().__init__(DAILY_COMMUNICATION_LIMIT_REASON)


class UpdatedTargetReachedError(RuntimeError):
    """暂停后下调目标，且当前累计进度已经达到新目标。"""


class RuntimeConditionsChangedError(RuntimeError):
    """当前岗位处理期间条件改变，必须回到列表后按新条件重新判断。"""


def format_processing_duration(seconds: float) -> str:
    """把单个岗位的处理耗时格式化成 “2min32sec”。"""

    total = max(0, int(round(seconds)))
    minutes, remainder = divmod(total, 60)
    return f"{minutes}min{remainder}sec" if minutes else f"{remainder}sec"


def card_scroll_distance(cards: tuple[JobCard, ...]) -> int:
    """Web 端固定滚动约一张卡片的高度区间，具体距离由页面测量决定。"""

    return 520


def has_job_capacity(max_jobs: int, inspected: int) -> bool:
    """0 表示不限量；达到正数上限后必须在任何后续滑动前停止。"""

    return max_jobs <= 0 or inspected < max_jobs


def _company_identity(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    normalized = re.sub(r"[^0-9a-z一-鿿]+", "", normalized)
    for suffix in ("有限责任公司", "股份有限公司", "有限公司"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def _same_company(left: str | None, right: str | None) -> bool:
    left_key = _company_identity(left)
    right_key = _company_identity(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted((left_key, right_key), key=len)
    return len(shorter) >= 4 and shorter in longer


def _job_identity(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^0-9a-z一-鿿+#]+", "", normalized)


def _same_job_name(left: str | None, right: str | None) -> bool:
    left_key = _job_identity(left)
    right_key = _job_identity(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    shorter, longer = sorted((left_key, right_key), key=len)
    return len(shorter) >= 4 and shorter in longer


def recommendation_cards_ready(
    cards: tuple[JobCard, ...],
    target_city: str | None,
) -> bool:
    """确认求职意向切换后的卡片已属于目标城市，避免读取刷新前的旧列表。"""

    if not cards:
        return False
    city = _company_identity(target_city).removesuffix("市")
    if not city:
        return True
    return any(city in _company_identity(card.location) for card in cards)


def load_recent_sent_companies(
    run_directory: str | Path,
    *,
    now: datetime | None = None,
    within_days: int = RECENT_APPLICATION_DAYS,
) -> dict[str, datetime]:
    """从本地运行记录读取严格短于指定天数的真实发送公司。"""

    reference = now or datetime.now().astimezone()
    cutoff = reference - timedelta(days=within_days)
    recent: dict[str, datetime] = {}
    for path in Path(run_directory).glob("boss_run_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fallback_created_at = payload.get("created_at")
        for decision in payload.get("decisions") or ():
            if not isinstance(decision, dict):
                continue
            if not (
                decision.get("message_sent") is True
                or decision.get("delivery_status") == "发送成功"
            ):
                continue
            company = decision.get("company_name")
            timestamp = decision.get("created_at") or fallback_created_at
            if not isinstance(company, str) or not company.strip() or not isinstance(timestamp, str):
                continue
            try:
                sent_at = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=reference.tzinfo)
            if not cutoff < sent_at <= reference:
                continue
            previous = recent.get(company)
            if previous is None or sent_at > previous:
                recent[company] = sent_at
    return recent


def find_recent_company_application(
    company_name: str | None,
    recent_companies: Mapping[str, datetime],
) -> tuple[str, datetime] | None:
    matches = (
        (stored_name, sent_at)
        for stored_name, sent_at in recent_companies.items()
        if _same_company(company_name, stored_name)
    )
    return max(matches, key=lambda item: item[1], default=None)


def load_recent_successful_applications(
    run_directory: str | Path,
    *,
    now: datetime | None = None,
    within_days: int = RECENT_APPLICATION_DAYS,
) -> dict[tuple[str, str], datetime]:
    """读取30天内公司名、岗位名和状态都精确匹配的本地发送成功记录。"""

    reference = now or datetime.now().astimezone()
    cutoff = reference - timedelta(days=within_days)
    recent: dict[tuple[str, str], datetime] = {}
    for path in Path(run_directory).glob("boss_run_*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        fallback_created_at = payload.get("created_at")
        for decision in payload.get("decisions") or ():
            if not isinstance(decision, dict):
                continue
            if decision.get("delivery_status") != "发送成功":
                continue
            company = decision.get("company_name")
            job = decision.get("job_name")
            timestamp = decision.get("created_at") or fallback_created_at
            if not all(
                isinstance(value, str) and value
                for value in (company, job, timestamp)
            ):
                continue
            try:
                sent_at = datetime.fromisoformat(timestamp)
            except ValueError:
                continue
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=reference.tzinfo)
            if not cutoff < sent_at <= reference:
                continue
            key = (company, job)
            previous = recent.get(key)
            if previous is None or sent_at > previous:
                recent[key] = sent_at
    return recent


def find_recent_successful_application(
    company_name: str | None,
    job_name: str | None,
    recent_applications: Mapping[tuple[str, str], datetime],
) -> datetime | None:
    """只接受完全相同的公司名与岗位名成功记录。"""

    if not company_name or not job_name:
        return None
    return recent_applications.get((company_name, job_name))


def allocate_expectation_quotas(
    intents: JobIntentData,
    target_companies: int,
) -> tuple[int, ...]:
    """按页面顺序平均分配目标公司数，余数依次给更靠前的求职期望。"""

    count = len(intents.expectations)
    if count == 0:
        return ()
    if count == 1:
        return (target_companies,)
    base, remainder = divmod(target_companies, count)
    return tuple(base + (1 if index < remainder else 0) for index in range(count))


def select_policy_job_intents(
    intents: JobIntentData,
    selected: JobExpectation | None,
) -> JobIntentData:
    """按 GUI 选择收窄本次运行的求职意向；None 保留原平均分配语义。"""

    if selected is None:
        return intents

    selected_city = _company_identity(selected.city)
    selected_role = _company_identity(selected.role)
    for expectation in intents.expectations:
        if (
            _company_identity(expectation.city) == selected_city
            and _company_identity(expectation.role) == selected_role
        ):
            return JobIntentData((expectation,), intents.work_status)

    label = f"{selected.city or '不限城市'} / {selected.role}"
    raise BossAutomationError(
        f"GUI 中选择的求职意向“{label}”已不在当前 Boss 页面中。"
        "请等待下拉框重新读取后再选择并启动。"
    )


class BossAutomationRunner:
    def __init__(
        self,
        browser: EdgeBrowser,
        store: JobStore,
        resume: ResumeData,
        *,
        artifact_directory: str | Path = "data/job_artifacts",
        run_directory: str | Path = "data/automation_runs",
        config: AutomationConfig | None = None,
        policy: AutomationPolicy | None = None,
        output: Callable[[str], None] = print,
        status_callback: Callable[[str, str], None] | None = None,
        result_callback: Callable[[dict[str, object]], None] | None = None,
        greeting_callback: Callable[[str], None] | None = None,
        review_provider: JobReviewProvider | None = None,
        resume_text: str = "",
        control: AutomationControl | None = None,
        stats: AutomationStats | None = None,
        recent_sent_companies: Mapping[str, datetime] | None = None,
        recent_successful_applications: (
            Mapping[tuple[str, str], datetime] | None
        ) = None,
        settings_applier: Callable[[dict[str, object]], None] | None = None,
        login_required_callback: Callable[[str], None] | None = None,
        login_wait_seconds: float = 300.0,
        require_logged_in_before_start: bool = False,
    ) -> None:
        self.browser = browser
        self.store = store
        self.resume = resume
        self._resume_degree_level = resume_degree_level(
            getattr(resume, "education", None)
        )
        self.artifact_directory = Path(artifact_directory)
        self.run_directory = Path(run_directory)
        self.config = config or AutomationConfig()
        self.policy = policy
        self.output = output
        self.status_callback = status_callback
        self.result_callback = result_callback
        self.greeting_callback = greeting_callback
        self.review_provider = review_provider
        self.resume_text = resume_text
        self.control = control
        # GUI 与 runner 必须持有同一对象；这样运行中途抛出普通异常时，MySQL
        # 收尾仍能写入异常前已经累计的真实进度。
        self.stats = stats if stats is not None else AutomationStats()
        self.settings_applier = settings_applier
        self.recent_sent_companies = dict(recent_sent_companies or {})
        self.recent_successful_applications = dict(
            recent_successful_applications or {}
        )
        self.login_required_callback = login_required_callback
        self.login_wait_seconds = login_wait_seconds
        self.require_logged_in_before_start = require_logged_in_before_start
        self._current_step = "尚未开始"
        self._current_job: str | None = None
        self._checkpoint_writer: Callable[[str], None] | None = None
        self._handled_conversations: set[str] = set()
        self._jobs_since_message_check = 0
        self._last_message_check_inspected = 0
        self._item_started_at: float | None = None
        self._breakpoint_page: str | None = None
        # 当前使用的求职意向（城市/角色），返回推荐列表时据此重新点击。
        self._selected_city: str | None = None
        self._selected_role: str | None = None
        self._selected_expectation_index: int | None = None
        self._selected_expectation_quota: int | None = None
        self._expectation_quotas: tuple[int, ...] = ()
        self._expectation_completed: list[int] = []
        self._job_intents = JobIntentData(())
        self._updated_target_reached = False
        self._runtime_conditions_changed = False
        self._current_send_started = False
        # None 表示按目标正常完成；达到 Boss 当日硬上限时写入固定、可展示的
        # 正常结束原因，并由主循环立即停止。
        self.completion_reason: str | None = None
        self.completion_warning: str | None = None

    # ------------------------------------------------------------------ 基础工具
    def _status(self, step: str, detail: str = "") -> None:
        self._current_step = step
        if self.status_callback:
            self.status_callback(step, detail)

    def _control_point(self) -> None:
        if self.control:
            self.control.wait_if_paused()
        if self._updated_target_reached and not self._current_send_started:
            self._updated_target_reached = False
            raise UpdatedTargetReachedError(
                f"已完成 {self.stats.matched} 家，已达到暂停后修改的新目标 "
                f"{self.policy.target_companies if self.policy else 0} 家"
            )
        if self._runtime_conditions_changed:
            self._runtime_conditions_changed = False
            raise RuntimeConditionsChangedError("运行条件已修改")

    def _sleep(self, seconds: float) -> None:
        if not self.control:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while True:
            self._control_point()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _pause_before(self, description: str) -> float:
        """在一次页面交互前重新生成随机延迟，避免固定节奏操作。"""

        delay = random.uniform(
            self.config.action_delay_min_seconds,
            self.config.action_delay_max_seconds,
        )
        self._status("随机等待", f"{delay:.2f} 秒后{description}")
        self.output(f"操作前随机等待 {delay:.2f} 秒：{description}")
        started_at = time.monotonic()
        self._sleep(delay)
        elapsed = time.monotonic() - started_at
        self.output(f"随机等待完成（实际 {elapsed:.2f} 秒），开始{description}")
        return delay

    def _wait(self, predicate, description: str, timeout: float | None = None):
        def resilient_predicate(browser):
            try:
                return predicate(browser)
            except (PageReadError, WebSelectionError) as exc:
                # 页面异步渲染期间字段缺失属于可恢复状态；统一交给底层有界轮询，
                # 超时后仍会带最后错误报出，不在第一次短暂空白时立即中断。
                raise BrowserError(str(exc)) from exc

        return self.browser.wait_for(
            resilient_predicate,
            timeout=timeout or self.config.page_wait_seconds,
            description=description,
            control_point=self._control_point if self.control else None,
        )

    def _click(self, element, description: str) -> None:
        self._pause_before(description)
        self.browser.click(element, description=description)

    def _wait_for_displayed(
        self,
        finder: Callable[[], object | None],
        description: str,
        *,
        timeout: float | None = None,
    ):
        """等待元素真正可见；CDP 页面重绘和短时网络空白都在边界内重试。"""

        def displayed(_browser):
            element = finder()
            if element is None:
                return None
            is_displayed = getattr(element, "is_displayed", None)
            if callable(is_displayed) and not is_displayed():
                return None
            return element

        return self._wait(displayed, description, timeout=timeout)

    def _click_when_ready(
        self,
        finder: Callable[[], object | None],
        description: str,
        *,
        timeout: float | None = None,
        before_click: Callable[[], None] | None = None,
    ):
        """先等待再按稳定语义重新定位，避免随机延迟期间旧 DOM 标记失效。"""

        self._pause_before(description)
        element = self._wait_for_displayed(
            finder,
            f"{description}对应的可见元素",
            timeout=timeout,
        )
        if before_click:
            before_click()
        self.browser.click(element, description=description)
        return element

    # ------------------------------------------------------------------ 页面判定
    def _current_page(self) -> str:
        try:
            if find_greeting_editor(self.browser) is not None and (
                "chat" in self.browser.current_url
                or extract_chat_messages(self.browser)
            ):
                return "chat"
            if self._extract_job_cards():
                return "recommendations"
            if find_chat_entry(self.browser) is not None:
                return "detail"
            if extract_chat_conversations(self.browser):
                return "messages"
        except (BrowserError, WebSelectionError):
            return "other"
        return "other"

    def _extract_job_cards(self) -> tuple[JobCard, ...]:
        """按当前策略读取卡片；仅启用规模筛选时补查官方列表接口。"""

        if self.policy and self.policy.minimum_company_size is not None:
            return extract_job_cards(self.browser, include_company_scale=True)
        return extract_job_cards(self.browser)

    # ------------------------------------------------------------------ 暂停/继续
    def apply_runtime_policy(self, policy: AutomationPolicy) -> None:
        """应用暂停期间修改的运行条件，并同步所有运行期派生状态。

        筛选条件本身由主循环每次读取 ``self.policy``，替换后即可生效；目标公司数
        还派生出了每条求职期望的配额，因此必须按已经完成的真实进度重新分配剩余额度。
        目标被下调到当前累计数以下时设置硬门禁，由下一个 runner 控制点在任何网页
        操作前正常结束，避免继续当前岗位后再判断。
        """

        previous = self.policy
        if (
            previous is not None
            and previous.selected_expectation != policy.selected_expectation
        ):
            raise BossAutomationError("本轮求职意向不能在暂停期间修改")

        self.policy = policy
        if previous != policy:
            self.mark_runtime_conditions_changed()
        target_changed = (
            previous is not None
            and previous.target_companies != policy.target_companies
        )
        if target_changed and self._job_intents.expectations:
            self._reallocate_remaining_expectation_quotas(policy.target_companies)
        if target_changed and self.stats.matched >= policy.target_companies:
            self._updated_target_reached = True

    def mark_runtime_conditions_changed(self) -> None:
        """使正在处理但尚未完成的岗位失效，避免沿用暂停前的审核或发送决定。"""

        if self._current_job is not None and not self._current_send_started:
            self._runtime_conditions_changed = True

    def _reallocate_remaining_expectation_quotas(
        self,
        target_companies: int,
    ) -> None:
        """从当前求职期望开始，把新目标的剩余数量重新分配到尚可执行的期望。"""

        count = len(self._job_intents.expectations)
        if count == 0:
            self._expectation_quotas = ()
            self._selected_expectation_quota = None
            return

        completed = list(self._expectation_completed[:count])
        completed.extend(0 for _ in range(count - len(completed)))
        start = self._selected_expectation_index or 0
        start = min(max(start, 0), count - 1)
        remaining = max(0, target_companies - sum(completed))
        active_count = count - start
        base, remainder = divmod(remaining, active_count)
        quotas = completed[:]
        for offset, index in enumerate(range(start, count)):
            quotas[index] = completed[index] + base + (1 if offset < remainder else 0)
        self._expectation_quotas = tuple(quotas)
        self._selected_expectation_quota = self._expectation_quotas[start]
        self.output(
            "暂停后已按新目标重算剩余配额："
            + "；".join(
                f"{item.city or '不限城市'}/{item.role}="
                f"{completed[index]}/{self._expectation_quotas[index]}家"
                for index, item in enumerate(self._job_intents.expectations)
            )
        )

    def _on_paused(self) -> None:
        try:
            self._breakpoint_page = self._current_page()
        except (BrowserError, WebSelectionError) as exc:
            self._breakpoint_page = None
            self.output(f"[提示] 暂停时未能记录当前页面：{exc}")
        if self._checkpoint_writer:
            self._checkpoint_writer("paused")

    def _apply_pending_settings(self) -> None:
        if self.control is None or self.settings_applier is None:
            return
        settings = self.control.take_pending_settings()
        if not settings:
            return
        try:
            self.settings_applier(settings)
        except Exception as exc:  # noqa: BLE001 - 应用失败不能中断已在跑的任务
            self.output(f"[警告] 暂停期间修改的设置未能全部生效，将沿用原设置：{exc}")

    def _on_resumed(self) -> None:
        self._apply_pending_settings()
        # 只在两卡之间（推荐/消息列表）暂停时主动恢复导航；详情/沟通页内暂停时
        # 保持不动，让逐字输入等进行中的动作继续，避免半途导航破坏状态。
        try:
            if self._breakpoint_page == "recommendations":
                self._return_to_recommendations()
            elif self._breakpoint_page == "messages":
                self._return_to_message_list()
        except (BrowserError, WebSelectionError, BossAutomationError) as exc:
            self.output(f"[提示] 恢复断点页面失败，将按当前页面继续：{exc}")
        if self._checkpoint_writer:
            self._checkpoint_writer("running")

    # ------------------------------------------------------------------ 结果输出
    def _emit_result(self, stats: AutomationStats, record: dict[str, object]) -> None:
        record.setdefault("expectation_index", self._selected_expectation_index)
        record.setdefault("expectation_city", self._selected_city)
        record.setdefault("expectation_role", self._selected_role)
        record.setdefault("expectation_quota", self._selected_expectation_quota)
        record = self._with_processing_duration(record)
        stats.decisions.append(record)
        if self.result_callback:
            self.result_callback(record)

    def _with_processing_duration(self, record: dict[str, object]) -> dict[str, object]:
        if self._item_started_at is None or "processing_duration" in record:
            return record
        duration = format_processing_duration(time.monotonic() - self._item_started_at)
        ordered: dict[str, object] = {}
        for key, value in record.items():
            ordered[key] = value
            if key == "created_at":
                ordered["processing_duration"] = duration
        ordered.setdefault("processing_duration", duration)
        return ordered

    def _emit_chat_action(self, stats: AutomationStats, record: dict[str, object]) -> None:
        record = self._with_processing_duration(record)
        stats.chat_actions.append(record)
        self.output(
            f"[消息] {record['recruiter_name']} / "
            f"{record.get('company_name') or '未知公司'}：{record['action']}"
        )
        if self.result_callback:
            self.result_callback({"record_type": "chat_action", **record})

    # ------------------------------------------------------------------ 登录与导航
    def ensure_ready(self) -> None:
        """启动浏览器、打开职位页，并在未登录时等待用户完成登录。"""

        self._status("检测当前 Edge", "正在查找当前已打开的 Boss直聘 页面")
        try:
            self.browser.start()
        except LoginRequiredError as exc:
            message = str(exc)
            self._status("等待登录", message.replace("\n", " "))
            if self.login_required_callback:
                self.login_required_callback(message)
            raise
        switch_to_boss_page = getattr(self.browser, "switch_to_boss_page", None)
        if callable(switch_to_boss_page):
            self._pause_before("切换到当前 Boss 页面")
            switch_to_boss_page()
        if self.require_logged_in_before_start:
            if self.browser.is_logged_in():
                return
            message = (
                "当前打开的 Edge Boss 页面未登录或登录已失效。\n\n"
                "请先在 Edge 中打开 Boss直聘 页面并完成登录，然后重新启动脚本。"
            )
            self._status("等待登录", message.replace("\n", " "))
            self.output("检测到当前 Boss 页面未登录：请登录后重新启动脚本")
            if self.login_required_callback:
                self.login_required_callback(message)
            raise LoginRequiredError(message)
        self._status("打开 Boss直聘", GEEK_LOGIN_URL)
        self._pause_before("打开 Boss直聘")
        self.browser.open(GEEK_LOGIN_URL)
        self._sleep(2.0)
        self._pause_before("切换到 Boss直聘 页面")
        self.browser.consolidate_windows()
        if self.browser.is_logged_in():
            return
        self._status("打开登录页", GEEK_LOGIN_URL)
        self._pause_before("打开 Boss直聘 登录页")
        self.browser.open(GEEK_LOGIN_URL)
        self._sleep(1.0)
        self._status(
            "等待登录",
            "请在打开的 Edge 浏览器中登录 Boss直聘（扫码或验证码），登录后将自动继续",
        )
        self.output("检测到未登录：请在 Edge 中完成 Boss直聘 登录（扫码/验证码）")
        if self.login_required_callback:
            self.login_required_callback(
                "已打开 Boss直聘 Web 登录页。\n\n"
                "请切换到 Edge，使用扫码或验证码完成登录；登录成功后程序会自动继续。"
            )
        deadline = time.monotonic() + self.login_wait_seconds
        while time.monotonic() < deadline:
            self._control_point()
            self._pause_before("检查新打开的 Boss 页面")
            self.browser.consolidate_windows()
            if self.browser.is_logged_in():
                self.output("已检测到登录成功，继续运行")
                self._sleep(1.5)
                return
            self._sleep(2.0)
        raise BossAutomationError(
            "等待登录超时。请先在 Edge 中登录 Boss直聘后再启动脚本。"
        )

    def read_job_intents(self) -> JobIntentData:
        self._status("读取求职意向", "正在读取求职意向")
        self._go_to_jobs_page()
        self._wait(
            lambda _b: bool(parse_job_intents(self.browser).expectations),
            "求职意向",
        )
        intents = parse_job_intents(self.browser)
        if not intents.expectations:
            raise BossAutomationError(
                "未读取到任何求职意向。请先在 Boss直聘 中添加求职意向后再运行。"
            )
        self.output(f"读取到 {len(intents.expectations)} 条求职意向")
        for item in intents.expectations:
            self.output(
                f"  - {item.city or '不限城市'} / {item.role} / {item.salary or '薪资不限'}"
            )
        self._job_intents = intents
        return intents

    def _target_city(self) -> str | None:
        if self.policy and self.policy.allowed_locations:
            return self.policy.allowed_locations[0]
        return None

    def _go_to_jobs_page(self, *, immediate: bool = False) -> None:
        # immediate 仅为旧调用兼容保留；所有实际页面操作都必须执行前置随机等待。
        _ = immediate
        if not self.browser.is_logged_in():
            raise BossAutomationError("Boss直聘 未登录或已退出登录，请重新登录后继续。")
        if (
            "web/geek/jobs" in self.browser.current_url
            and "加载中，请稍候" not in self.browser.page_text()[:120]
        ):
            return
        clicked_positions = click_positions_tab(
            self.browser,
            before_click=self._pause_before,
        )
        if clicked_positions:
            try:
                self._wait(
                    lambda _b: "web/geek/jobs" in self.browser.current_url
                    and "加载中，请稍候" not in self.browser.page_text()[:120],
                    "职位页面",
                    timeout=min(3.0, self.config.page_wait_seconds),
                )
                return
            except ElementNotFoundError:
                self.output("[提示] 点击“职位”后页面未切换，改用职位页直达链接")

        # 当前 Boss 消息页并不总是渲染顶部“职位”入口，也可能吞掉一次点击。
        # 已确认登录后可直接导航到职位页，再重新选择原求职意向。
        if not clicked_positions or "web/geek/jobs" not in self.browser.current_url:
            self._pause_before("打开职位页面")
            self.browser.open(GEEK_JOBS_URL)
            self._sleep(1.5)
            try:
                self._wait(
                    lambda _b: "web/geek/jobs" in self.browser.current_url
                    and "加载中，请稍候" not in self.browser.page_text()[:120],
                    "职位页面",
                )
            except ElementNotFoundError as exc:
                raise BossAutomationError(
                    "无法从当前页面恢复到 Boss直聘 职位页；"
                    "请确认登录状态后手动打开职位页再继续。"
                ) from exc

    def open_recommendations(
        self,
        intents: JobIntentData | None = None,
        *,
        expectation: JobExpectation | None = None,
        immediate_return: bool = False,
    ) -> None:
        """进入职位页并点击当前求职期望，返回对应的推荐岗位列表。"""

        self._status("推荐首页", "正在选择求职意向并读取推荐岗位")
        # 关闭详情/沟通遗留的多余标签页，回到职位页。
        # immediate_return 仅为旧调用兼容保留；发送完成后的返回动作同样等待。
        _ = immediate_return
        self._pause_before("整理 Boss 标签页")
        self.browser.close_extra_windows()
        self._go_to_jobs_page(immediate=immediate_return)
        available_intents = intents or self._job_intents
        selected = expectation
        if selected is None and self._selected_expectation_index is not None:
            if self._selected_expectation_index < len(available_intents.expectations):
                selected = available_intents.expectations[
                    self._selected_expectation_index
                ]
        if selected is None and available_intents.expectations:
            # 首次运行严格从页面中已添加的第一条求职期望开始。
            selected = available_intents.expectations[0]
        target_city = selected.city if selected else self._target_city()
        target_role = selected.role if selected else self._selected_role
        self._pause_before(f"点击求职意向（{target_city or '默认'}）")
        try:
            clicked_expectation = bool(
                self._wait(
                    lambda _b: click_expectation(
                        self.browser,
                        target_city,
                        role=target_role,
                    ),
                    f"求职意向（{target_city or '默认'} / {target_role or '默认'}）",
                )
            )
        except ElementNotFoundError as exc:
            if target_city:
                raise BossAutomationError(
                    f"等待 {self.config.page_wait_seconds:g} 秒后仍未找到与目标城市"
                    f"“{target_city}”匹配的求职意向。请确认已在 Boss直聘 添加该城市"
                    "的求职意向，或调整目标城市设置。"
                ) from exc
            raise
        if clicked_expectation:
            self._selected_city = target_city
            self._selected_role = target_role
        self._wait(
            lambda _b: expectation_is_active(
                self.browser,
                target_city,
                role=target_role,
            ),
            f"已激活的求职意向（{target_city or '默认'} / {target_role or '默认'}）",
        )
        self._wait(
            lambda _b: recommendation_cards_ready(
                self._extract_job_cards(),
                target_city,
            ),
            "推荐岗位列表",
        )

    # ------------------------------------------------------------------ 岗位卡片
    def _open_card(self, card: JobCard) -> None:
        # 直接坐标点卡片会命中内部 a[href*=/job_detail/]，把列表页导航成完整
        # 详情页。事件必须发给 li 卡片容器本身，让 Boss 只刷新右侧固定面板。
        self._pause_before(f"在列表右侧查看岗位“{card.job_name}”")
        try:
            self._wait(
                lambda _b: select_job_card_inline(self.browser, card),
                f"推荐列表中的岗位“{card.job_name}”",
            )
        except ElementNotFoundError as exc:
            raise BossAutomationError(
                f"推荐列表异步刷新；等待 {self.config.page_wait_seconds:g} 秒后"
                f"仍无法按岗位ID、详情链接或指纹重新定位岗位：{card.job_name}"
            ) from exc
        self._sleep(1.0)
        if "web/geek/jobs" not in self.browser.current_url:
            raise BossAutomationError(
                f"岗位卡片意外离开推荐列表，已停止读取：{card.job_name}"
            )

        def expected_detail_is_ready(_browser) -> bool:
            # 这里必须核对页面原始岗位名。若先调用 align_detail_identity()，它会
            # 按设计把岗位名覆盖成卡片名，导致旧详情也永远“匹配”当前卡片。
            job = read_job_detail(self.browser).job_data
            return bool(
                job.is_boss_job_detail_page
                and job.job_description.value
                and _same_job_name(card.job_name, job.job_name.value)
            )

        self._wait(
            expected_detail_is_ready,
            f"岗位详情页：{card.job_name}",
        )

    def _return_to_recommendations(self, *, immediate: bool = False) -> None:
        """返回并重新点击“职位/求职意向”，回到正确的推荐岗位页。"""

        self._status("返回推荐", "正在返回推荐岗位列表")
        self.open_recommendations(immediate_return=immediate)

    # ------------------------------------------------------------------ 消息巡检
    def _return_to_message_list(self) -> None:
        if "web/geek/chat" not in self.browser.current_url:
            self._pause_before("打开消息会话列表")
            self.browser.open(CHAT_URL)
            self._sleep(1.5)
        self._wait(
            lambda _b: bool(extract_chat_conversations(self.browser)),
            "消息会话列表",
        )

    def _find_conversation(self, fingerprint: str) -> ChatConversation | None:
        return next(
            (
                item
                for item in extract_chat_conversations(self.browser)
                if item.fingerprint == fingerprint
            ),
            None,
        )

    def _pin_conversation(self, conversation: ChatConversation) -> bool:
        """把需要用户亲自处理的会话置顶。

        点击会话行右侧的实际操作图标后再点击菜单里的“置顶”，不依赖固定屏幕位置；
        找不到入口时抛错，避免记录成“已置顶”但页面实际上没有执行。
        """

        # 当前 Boss Web 的 .user-operation 默认 display:none，只有真实鼠标悬停
        # 会话行后才显示。直接等待操作图标不会触发 :hover，会必然超时。
        row = self._wait_for_displayed(
            lambda: find_conversation_open_target(
                self.browser, conversation.fingerprint
            ),
            f"会话“{conversation.recruiter_name}”的可见行",
        )
        self._pause_before(f"显示会话“{conversation.recruiter_name}”的操作菜单")
        self.browser.hover(
            row,
            description=f"会话“{conversation.recruiter_name}”",
        )
        self._click_when_ready(
            lambda: find_conversation_operation_element(
                self.browser, conversation.fingerprint
            ),
            f"打开会话“{conversation.recruiter_name}”的操作菜单",
        )
        self._pause_before(f"确认会话“{conversation.recruiter_name}”的置顶状态")

        def pin_menu_state(_browser):
            pin = self.browser.find_clickable_by_text(
                ["置顶", "置顶会话"],
                tags=("a", "button", "span", "li", "div"),
            )
            if pin is not None:
                return "pin", pin
            unpin = self.browser.find_clickable_by_text(
                ["取消置顶", "取消置顶会话"],
                tags=("a", "button", "span", "li", "div"),
            )
            if unpin is not None:
                return "already_pinned", unpin
            return None

        state, menu_item = self._wait(
            pin_menu_state,
            f"会话“{conversation.recruiter_name}”的置顶或取消置顶菜单项",
        )
        if state == "already_pinned":
            self.output(
                f"会话已存在“取消置顶”选项，确认此前已经置顶："
                f"{conversation.recruiter_name}"
            )
            return False
        self.browser.click(
            menu_item,
            description=f"置顶会话“{conversation.recruiter_name}”",
        )
        return True

    def _count_pin_result(
        self,
        stats: AutomationStats,
        newly_pinned: bool | None,
    ) -> None:
        """兼容旧测试替身的 None；只有明确 False 才表示原本已经置顶。"""

        if newly_pinned is False:
            stats.conversations_already_pinned += 1
        else:
            stats.conversations_pinned += 1

    def _resume_already_sent(self) -> bool:
        return resume_already_sent(extract_chat_system_notes(self.browser))

    def _accept_resume_request(self, conversation: ChatConversation) -> bool:
        self._status("发送简历", f"正在同意{conversation.recruiter_name}的简历请求")
        if self._resume_already_sent():
            self.output(f"该会话已发送过附件简历，不重复发送：{conversation.recruiter_name}")
            return False
        try:
            self._click_when_ready(
                lambda: resume_request_accept_button(self.browser),
                "同意发送附件简历",
            )
        except ElementNotFoundError as exc:
            raise BossAutomationError(
                f"等待 {self.config.page_wait_seconds:g} 秒后简历请求卡片仍未出现，"
                "未发送简历"
            ) from exc
        self._wait(lambda _b: self._resume_already_sent(), "附件简历发送结果确认")
        return True

    def _send_resume_attachment(self, conversation: ChatConversation) -> bool:
        self._status("发送简历", f"正在向{conversation.recruiter_name}发送附件简历")
        if self._resume_already_sent():
            self.output(f"该会话已发送过附件简历，不重复发送：{conversation.recruiter_name}")
            return False
        try:
            self._click_when_ready(
                lambda: find_resume_send_entry(self.browser),
                "点击“发简历”",
            )
            self._click_when_ready(
                lambda: find_resume_confirm_button(self.browser),
                "确认发送简历",
            )
        except ElementNotFoundError as exc:
            raise BossAutomationError(
                f"等待 {self.config.page_wait_seconds:g} 秒后仍未出现可用的发简历入口"
                "或确认按钮，未发送简历"
            ) from exc
        self._wait(lambda _b: self._resume_already_sent(), "附件简历发送结果确认")
        return True

    def _open_conversation(self, conversation: ChatConversation) -> bool:
        try:
            self._click_when_ready(
                lambda: find_conversation_open_target(
                    self.browser, conversation.fingerprint
                ),
                f"打开会话“{conversation.recruiter_name}”",
            )
        except ElementNotFoundError:
            return False

        def target_chat_content_ready(_browser: EdgeBrowser) -> bool:
            recruiter_name, company_name = read_current_chat_identity(self.browser)
            if _company_identity(recruiter_name) != _company_identity(
                conversation.recruiter_name
            ):
                return False
            if (
                conversation.company_name
                and company_name
                and not _same_company(company_name, conversation.company_name)
            ):
                return False

            messages = extract_chat_messages(self.browser)
            trailing_recruiter_count = 0
            for message in reversed(messages):
                if message.from_me:
                    break
                trailing_recruiter_count += 1

            # 未读角标可能对应普通消息和标准卡片。普通消息必须至少读到角标
            # 声明的数量；标准简历卡片一旦出现便已足以安全判断意图。
            return (
                trailing_recruiter_count >= max(1, conversation.unread_count)
                or resume_request_accept_button(self.browser) is not None
                or bool(extract_chat_system_notes(self.browser))
            )

        self._wait(
            target_chat_content_ready,
            f"会话页面：{conversation.recruiter_name}",
        )
        return True

    def _review_unsolicited_chat_job(
        self,
        conversation: ChatConversation,
        job: ChatJobInfo,
    ) -> tuple[bool, str]:
        """按会话头部真实岗位名、地点和薪资判断HR主动会话是否可回复。"""

        if self.review_provider is None or not hasattr(
            self.review_provider, "review_card"
        ):
            return False, "当前审核器无法判断会话岗位方向"
        if not job.job_name:
            return False, "会话对应岗位名未识别"
        if self.policy.allowed_locations and not job.location:
            return False, "会话对应岗位地点未识别"
        if (
            self.policy.salary_min_k is not None
            or self.policy.salary_max_k is not None
        ) and not job.salary:
            return False, "会话对应岗位薪资未识别"
        if not salary_meets(
            job.salary,
            self.policy.salary_min_k,
            self.policy.salary_max_k,
        ):
            configured = f"{self.policy.salary_min_k}-{self.policy.salary_max_k}K"
            return False, f"岗位薪资“{job.salary or '未识别'}”不符合“{configured}”"
        synthetic = JobCard(
            job_name=job.job_name,
            company_name=conversation.company_name,
            salary=job.salary,
            location=job.location,
            recruiter_activity=None,
            tags=(),
            fingerprint=f"chat:{conversation.fingerprint}:{job.job_name}",
        )
        review = self.review_provider.review_card(
            synthetic,
            self.policy,
            self.resume,
            self.resume_text,
        )
        rejection = card_review_rejection_reason(synthetic, review)
        return rejection is None, rejection or review.reason

    def _handle_resume_request_card(
        self,
        conversation: ChatConversation,
        stats: AutomationStats,
        record: dict[str, object],
    ) -> None:
        if self.config.dry_run or self.config.fill_only:
            self._count_pin_result(stats, self._pin_conversation(conversation))
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "已置顶待处理",
                    "reason": "HR发来索要附件简历的请求卡片；当前为非发送模式，未自动同意",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return
        reply = DEFAULT_RESUME_REPLY if self.config.reply_before_resume else ""
        replied = bool(reply)
        if replied:
            self._fill_or_send_greeting(reply, send=True, open_chat=False)
        sent = self._accept_resume_request(conversation)
        if sent:
            stats.resumes_sent += 1
            action = "已回复并发送简历" if replied else "已发送简历"
        else:
            action = "已回复，简历此前已发送" if replied else "简历此前已发送"
        self._emit_chat_action(
            stats,
            {
                **record,
                "action": action,
                "reason": "HR发来索要附件简历的请求卡片，已点击“同意”发送",
                "reply": reply if replied else None,
                "resume_sent": sent,
            },
        )

    def _handle_unread_conversation(
        self,
        conversation: ChatConversation,
        stats: AutomationStats,
    ) -> None:
        """处理单个未读会话；只有明确索要简历的HR才会收到回复和附件简历。"""

        self._item_started_at = time.monotonic()
        self._status(
            "查看未读消息",
            f"{conversation.recruiter_name} / {conversation.company_name or '未知公司'}",
        )
        self._return_to_message_list()
        try:
            current = self._wait(
                lambda _b: self._find_conversation(conversation.fingerprint),
                f"未读会话“{conversation.recruiter_name}”",
            )
        except ElementNotFoundError:
            current = None
        if current is None or current.unread_count <= 0:
            self.output(f"会话已无未读消息，跳过：{conversation.recruiter_name}")
            return
        conversation = current
        record: dict[str, object] = {
            "created_at": datetime.now().astimezone().isoformat(),
            "recruiter_name": conversation.recruiter_name,
            "company_name": conversation.company_name,
            "position_name": conversation.position_name,
            "unread_count": conversation.unread_count,
            "last_message": conversation.last_message,
        }

        # 列表预览本身已经足以确认的终态必须在打开会话、模型审核和置顶之前短路。
        # 这既避免把“简历已发送/已查看”误置顶，也避免明确拒绝会话因虚拟列表
        # 重绘而产生无意义的打开与置顶失败。
        if resume_already_sent((conversation.last_message,)):
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "无需处理",
                    "reason": "列表消息已确认附件简历发送成功或已被HR查看，不回复且不置顶",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return
        if _hr_has_rejected(conversation, ()):
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "无需处理",
                    "reason": "列表最新消息已明确表示岗位不合适，不回复且不置顶",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return

        if not self._open_conversation(conversation):
            self.output(f"消息列表已刷新，跳过会话：{conversation.recruiter_name}")
            return
        messages = extract_chat_messages(self.browser)
        system_notes = extract_chat_system_notes(self.browser)

        if _hr_has_rejected(conversation, messages):
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "无需处理",
                    "reason": "HR已明确表示岗位不合适，不回复且不置顶",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return

        already_sent = resume_already_sent(
            (conversation.last_message, *system_notes)
        )
        if already_sent:
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "无需处理",
                    "reason": "附件简历已经发送或被HR查看，不重复回复且不置顶",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return

        started_by_me = any(message.from_me for message in messages)
        resume_card = resume_request_accept_button(self.browser)
        if messages and messages[-1].from_me and resume_card is None:
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "无需处理",
                    "reason": "最新聊天气泡由我方发出，未读属于纯状态通知，不回复且不置顶",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return

        if self.policy and contains_any(
            conversation.company_name,
            self.policy.excluded_companies,
        ):
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "无需处理",
                    "reason": "会话公司命中不打招呼名单，不回复且不置顶",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return

        assert self.review_provider is not None
        review = self.review_provider.review_chat_message(
            conversation, messages, system_notes=system_notes
        )
        resume_requested = review.resume_requested or resume_card is not None
        contact_requested = (
            bool(getattr(review, "contact_requested", False))
            or _contact_request_detected(messages, system_notes)
        )
        request_reason = (
            review.reason
            if review.resume_requested
            or bool(getattr(review, "contact_requested", False))
            else (
                "检测到Boss标准附件简历请求卡片"
                if resume_card is not None
                else review.reason
            )
        )

        if (
            bool(getattr(review, "no_action_needed", False))
            and not resume_requested
            and not contact_requested
        ):
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "无需处理",
                    "reason": f"HR消息属于明确拒绝、结束沟通或纯状态通知：{review.reason}",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return

        if not resume_requested and not contact_requested:
            self._count_pin_result(stats, self._pin_conversation(conversation))
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "已置顶待处理",
                    "reason": f"无法自动回复该消息：{review.reason}",
                    "reply": None,
                    "resume_sent": False,
                },
            )
            return

        if not started_by_me:
            def chat_job_info_ready(_browser):
                info = read_current_chat_job_info(self.browser)
                if not info.job_name:
                    return None
                if self.policy.allowed_locations and not info.location:
                    return None
                if (
                    self.policy.salary_min_k is not None
                    or self.policy.salary_max_k is not None
                ) and not info.salary:
                    return None
                return info

            try:
                chat_job = self._wait(
                    chat_job_info_ready,
                    f"会话“{conversation.recruiter_name}”对应的岗位名、地点和薪资",
                )
            except ElementNotFoundError:
                self._count_pin_result(stats, self._pin_conversation(conversation))
                self._emit_chat_action(
                    stats,
                    {
                        **record,
                        "action": "已置顶待处理",
                        "reason": "HR主动发起且无法可靠读取会话对应岗位名、地点或薪资，未自动回复",
                        "reply": None,
                        "resume_sent": False,
                    },
                )
                return
            job_matches, match_reason = self._review_unsolicited_chat_job(
                conversation, chat_job
            )
            record["chat_job_name"] = chat_job.job_name
            record["chat_job_location"] = chat_job.location
            record["chat_job_salary"] = chat_job.salary
            if not job_matches:
                self._emit_chat_action(
                    stats,
                    {
                        **record,
                        "action": "无需处理",
                        "reason": (
                            f"HR主动会话的岗位“{chat_job.job_name} / "
                            f"{chat_job.location} / {chat_job.salary}”与当前条件不匹配："
                            f"{match_reason}"
                        ),
                        "reply": None,
                        "resume_sent": False,
                    },
                )
                return

        if self.config.dry_run or self.config.fill_only:
            self._count_pin_result(stats, self._pin_conversation(conversation))
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "已置顶待处理",
                    "reason": f"当前为非发送模式，未自动发送简历：{request_reason}",
                    "reply": review.reply,
                    "resume_sent": False,
                },
            )
            return

        reply = review.reply or DEFAULT_RESUME_REPLY
        replied = self.config.reply_before_resume and bool(reply)
        try:
            if replied:
                self._fill_or_send_greeting(reply, send=True, open_chat=False)
            # 只有已经严格确认属于“附件简历”的请求卡片才点击同意。
            # 联系方式请求永远不点其“同意”，而是从聊天工具栏发送附件简历。
            sent = (
                self._accept_resume_request(conversation)
                if resume_card is not None
                else self._send_resume_attachment(conversation)
            )
        except (
            PageReadError,
            WebSelectionError,
            BrowserError,
            BossAutomationError,
        ) as exc:
            self._count_pin_result(stats, self._pin_conversation(conversation))
            self._emit_chat_action(
                stats,
                {
                    **record,
                    "action": "已置顶待处理",
                    "reason": f"自动回复或发送简历失败，已交由用户处理：{exc}",
                    "reply": reply if replied else None,
                    "resume_sent": False,
                },
            )
            return
        if sent:
            stats.resumes_sent += 1
            action = "已回复并发送简历" if replied else "已发送简历"
        else:
            action = "已回复，简历此前已发送" if replied else "简历此前已发送"
        self._emit_chat_action(
            stats,
            {
                **record,
                "action": action,
                "reason": request_reason,
                "reply": reply if replied else None,
                "resume_sent": sent,
            },
        )

    def _inspect_messages_between_jobs(self, stats: AutomationStats) -> bool:
        if not self.config.inspect_unread_messages:
            return False
        if self.control and self.control.stop_requested:
            return False
        self._last_message_check_inspected = stats.inspected
        self._jobs_since_message_check = 0
        return self.process_unread_messages(stats)

    def _inspect_messages_if_due(self, stats: AutomationStats) -> bool:
        """连续未成功投递时，按已扫描岗位数定期核实顶部未读数字。"""

        if not self.config.inspect_unread_messages:
            return False
        self._jobs_since_message_check = max(
            0, stats.inspected - self._last_message_check_inspected
        )
        if (
            self._jobs_since_message_check
            < self.config.force_message_check_every_n_jobs
        ):
            return False
        self._last_message_check_inspected = stats.inspected
        self._jobs_since_message_check = 0
        return self.process_unread_messages(stats, force=True)

    def process_unread_messages(
        self,
        stats: AutomationStats,
        *,
        force: bool = False,
    ) -> bool:
        if self.review_provider is None or not hasattr(
            self.review_provider, "review_chat_message"
        ):
            return False
        try:
            unread_count = read_message_unread_count(self.browser)
        except (BrowserError, WebSelectionError):
            unread_count = 0
        if unread_count <= 0:
            self.output("消息角标无红色数字，不进入消息页")
            return False
        self.output(f"检测到“消息”旁红色数字 {unread_count}，开始处理未读会话")
        entered = False
        try:
            self._pause_before("打开消息会话列表")
            self.browser.open(CHAT_URL)
            # 只要导航已经发出，后续无论是否发现未读消息，都必须恢复推荐页。
            # 原逻辑在“没有未读”时提前 return，entered 仍为 False，导致浏览器
            # 留在消息页，而主循环继续拿进入消息页前缓存的岗位卡片进行定位。
            entered = True
            self._sleep(1.5)
            # 会话行会先出现，notice-badge 往往再晚约 1 秒异步挂载。不能只靠
            # 固定 sleep 后立即解析，否则顶部已有红色数字时仍会得到全部 unread=0。
            self._wait(
                lambda _b: any(
                    item.unread_count > 0
                    for item in extract_chat_conversations(self.browser)
                ),
                "含未读角标的消息会话列表",
            )
            self._jobs_since_message_check = 0
            self._status(
                "消息巡检",
                f"检测到红色数字 {unread_count}，正在查看未读会话",
            )
            for _ in range(self.config.max_unread_conversations):
                self._control_point()
                self._return_to_message_list()
                pending = next(
                    (
                        item
                        for item in extract_chat_conversations(self.browser)
                        if item.unread_count > 0
                        and item.fingerprint not in self._handled_conversations
                    ),
                    None,
                )
                if pending is None:
                    break
                self._handled_conversations.add(pending.fingerprint)
                if pending.pinned:
                    stats.conversations_already_pinned += 1
                    self.output(
                        f"[消息] 会话已置顶，交由用户处理，跳过：{pending.recruiter_name}"
                    )
                    continue
                try:
                    self._handle_unread_conversation(pending, stats)
                except (
                    PageReadError,
                    WebSelectionError,
                    BrowserError,
                    BossAutomationError,
                    ReviewError,
                ) as exc:
                    try:
                        self._return_to_message_list()
                        newly_pinned = self._pin_conversation(pending)
                    except (
                        PageReadError,
                        WebSelectionError,
                        BrowserError,
                        BossAutomationError,
                    ) as pin_exc:
                        stats.failed += 1
                        self.output(
                            f"[失败] 未读消息处理失败且无法置顶：{exc}；{pin_exc}"
                        )
                        action = "处理失败"
                        reason = f"{exc}；置顶失败：{pin_exc}"
                    else:
                        self._count_pin_result(stats, newly_pinned)
                        action = "已置顶待处理"
                        reason = f"自动处理失败，已交由用户处理：{exc}"
                    self._emit_chat_action(
                        stats,
                        {
                            "created_at": datetime.now().astimezone().isoformat(),
                            "recruiter_name": pending.recruiter_name,
                            "company_name": pending.company_name,
                            "position_name": pending.position_name,
                            "unread_count": pending.unread_count,
                            "last_message": pending.last_message,
                            "action": action,
                            "reason": reason,
                            "reply": None,
                            "resume_sent": False,
                        },
                    )
        except AutomationStopRequested:
            raise
        except (
            PageReadError,
            WebSelectionError,
            BrowserError,
            BossAutomationError,
            ReviewError,
        ) as exc:
            self.output(f"[失败] 消息巡检失败，已跳过本轮：{exc}")
        finally:
            if entered and not (self.control and self.control.stop_requested):
                self._return_to_recommendations()
        return entered

    # ------------------------------------------------------------------ 招呼语
    def _fill_or_send_greeting(
        self,
        greeting: str,
        *,
        send: bool,
        open_chat: bool = True,
        expected_card: JobCard | None = None,
    ) -> None:
        if open_chat:
            self._status("沟通页面", "正在点击立即沟通")
            try:
                self._open_chat_with_quota_notice_retry(expected_card=expected_card)
            except ElementNotFoundError as exc:
                raise BossAutomationError(
                    f"等待 {self.config.page_wait_seconds:g} 秒后详情页仍未出现可靠的"
                    "“立即沟通”或“继续沟通”按钮"
                ) from exc
        else:
            self._status("沟通页面", "正在回复HR")

        editor = self._click_when_ready(
            lambda: find_greeting_editor(self.browser),
            "点击沟通页面输入框",
        )
        self._pause_before("清空沟通页面输入框")
        self.browser.clear_editor(editor)
        self._status("招呼语填充", "正在逐字填入招呼语")
        self._pause_before("开始逐字填入招呼语")
        self.browser.type_stream(
            editor,
            greeting,
            control_point=self._control_point if self.control else None,
        )

        # Input.insertText 逐字符写入 Boss 的响应式 contenteditable 时，实测曾把
        # TypeScript 写成 Typecript，并把丢失的 S 挪到整句末尾。发送前必须回读；
        # 若有任何差异，只修复输入框一次，尚未触发发送，不存在重复沟通风险。
        actual_editor_text = self.browser.editor_value(editor)
        if _chat_text_identity(actual_editor_text) != _chat_text_identity(greeting):
            self.output("[提示] 招呼语逐字输入结果与目标不一致，发送前正在安全校正")
            self._pause_before("校正沟通页面输入框")
            editor = self._wait_for_displayed(
                lambda: find_greeting_editor(self.browser),
                "校正前的沟通页面输入框",
            )
            self.browser.set_value(editor, greeting)
            self._wait(
                lambda _b: _chat_text_identity(self.browser.editor_value(editor))
                == _chat_text_identity(greeting),
                "招呼语输入框内容校验",
                timeout=min(3.0, self.config.page_wait_seconds),
            )
        self._status("填充完成", "招呼语已填入输入框，尚未发送")

        if not send:
            return

        self._status("等待发送", "正在确认输入框和发送按钮")
        self._control_point()
        if not self._greeting_in_messages(greeting):
            def mark_send_started() -> None:
                # 从这里开始点击可能已经到达 Boss；暂停修改只能影响下一岗位，
                # 不能中断确认并把一次真实发送误当成未发送。
                self._current_send_started = True

            try:
                self._click_when_ready(
                    lambda: find_send_button(self.browser),
                    "发送招呼语",
                    before_click=mark_send_started,
                )
            except ElementNotFoundError as exc:
                raise BossAutomationError(
                    "招呼语已填入并校验，但发送按钮在等待后仍未启用；"
                    "未执行发送动作，已停止处理该岗位"
                ) from exc
            try:
                self._wait(
                    lambda _b: self._greeting_in_messages(greeting),
                    "已发送招呼语出现在沟通记录",
                )
            except Exception as exc:  # noqa: BLE001
                # 发送动作只允许执行一次。验证失败时不能再次点击，否则第一次已成功但
                # DOM 尚未识别到消息时会造成重复招呼。
                raise BossAutomationError(
                    "已执行一次发送动作，但未能在聊天记录中确认招呼语；"
                    "为避免重复发送，已停止处理该岗位"
                ) from exc
        self._status("发送完成", "招呼语已点击发送")

    def _detail_chat_entry(self, card: JobCard) -> object | None:
        snapshot = read_job_detail(self.browser)
        job = snapshot.job_data
        if not (
            job.is_boss_job_detail_page
            and job.job_description.value
            and _same_job_name(card.job_name, job.job_name.value)
        ):
            return None
        return find_chat_entry(self.browser)

    def _open_chat_with_quota_notice_retry(
        self, *, expected_card: JobCard | None = None
    ) -> None:
        """打开沟通页；若命中当日次数提醒，关闭后重放被拦截的原点击。"""

        def chat_is_ready() -> bool:
            return bool(
                expected_card is not None
                and "chat" in self.browser.current_url
                and find_greeting_editor(self.browser) is not None
            )

        if chat_is_ready():
            return

        def chat_entry():
            if expected_card is None:
                return find_chat_entry(self.browser)
            return self._detail_chat_entry(expected_card)

        recovered_detail = False
        retry_reason: str | None = None

        for attempt in range(2):
            # 额度弹窗关闭后的自动跳转可能刚好发生在上一轮有界等待之后、
            # 下一轮重定位之前；此时直接继续输入，不能再去详情页找旧按钮。
            if chat_is_ready():
                return
            if attempt == 0:
                description = "点击“立即沟通”"
            elif retry_reason == "stalled_transition":
                description = "首次点击未切换后重新点击沟通入口"
            else:
                description = "关闭沟通提醒后重新点击“立即沟通”"
            try:
                self._click_when_ready(chat_entry, description)
            except ElementNotFoundError:
                if chat_is_ready():
                    return
                if expected_card is None or recovered_detail or attempt > 0:
                    raise
                # 模型审核期间推荐列表可能异步重绘右侧面板。重新按岗位稳定身份
                # 选择一次并重新核对原始详情，绝不在身份不明的详情上点击沟通。
                recovered_detail = True
                self.output(
                    f"[提示] 岗位详情在审核期间已变化，正在重新定位：{expected_card.job_name}"
                )
                self._wait(
                    lambda _b: select_job_card_inline(self.browser, expected_card),
                    f"重新定位岗位“{expected_card.job_name}”",
                )
                self._sleep(1.0)
                self._click_when_ready(
                    chat_entry,
                    f"重新定位后{description}",
                )
            self._sleep(1.0)
            if self.browser.driver and len(self.browser.driver.window_handles) > 1:
                self._pause_before("切换到沟通页面")
                self.browser.consolidate_windows()

            try:
                state = self._wait(
                    lambda _b: (
                        "quota_notice"
                        if read_communication_quota_notice(self.browser) is not None
                        else "editor"
                        if find_greeting_editor(self.browser) is not None
                        else None
                    ),
                    "沟通页面或当日沟通次数提醒",
                )
            except ElementNotFoundError as exc:
                if chat_is_ready():
                    return
                # 旧实现把这里的“点击后未切换”也包装成“详情按钮未出现”，
                # 因而日志无法区分按钮缺失和点击被页面吞掉。若仍明确停留在同一
                # 岗位详情且入口仍可见，可安全重放一次；自定义招呼语尚未发送。
                if (
                    attempt == 0
                    and expected_card is not None
                    and self._detail_chat_entry(expected_card) is not None
                ):
                    retry_reason = "stalled_transition"
                    self.output(
                        "[提示] 已点击沟通入口但页面未切换，仍停留在同一岗位详情；"
                        "正在安全重试一次"
                    )
                    continue
                raise BossAutomationError(
                    "已点击一次岗位沟通入口，但等待后仍未进入聊天页，也未出现"
                    "可识别的沟通次数提醒；未填入或发送招呼语"
                ) from exc
            if state == "editor":
                return
            if self._dismiss_communication_quota_notice_if_present():
                # 真实页面中点击“好”通常会继续第一次“立即沟通”请求并自动
                # 跳转到聊天页。给该跳转一个有界等待；只有确认没有自动进入
                # 聊天时，下一轮才重放原点击。
                try:
                    self._wait(
                        lambda _b: find_greeting_editor(self.browser),
                        "关闭沟通提醒后自动进入沟通页面",
                        timeout=min(4.0, self.config.page_wait_seconds),
                    )
                    return
                except ElementNotFoundError:
                    pass
                retry_reason = "quota_notice"
                continue

        raise BossAutomationError(
            "重新点击岗位沟通入口后仍未进入沟通页面；未填入或发送招呼语"
        )

    def _dismiss_communication_quota_notice_if_present(self) -> bool:
        notice = read_communication_quota_notice(self.browser)
        if notice is None:
            return False
        if notice.limit_reached:
            # 首次确认硬上限即锁定终止状态。后续“确定”点击或关闭校验即使失败，
            # 也只能带告警结束，绝不能落入普通岗位失败分支后继续投递。
            self.completion_reason = DAILY_COMMUNICATION_LIMIT_REASON
            self.completion_warning = None
            self._status("沟通上限", "检测到今日已沟通150位BOSS，正在点击确定并结束")
            self.output(
                "检测到 Boss 当日沟通硬上限：已与 150 位 BOSS 沟通；"
                "点击“确定”后自动结束本次运行"
            )
            try:
                self._pause_before("关闭150位沟通上限弹窗")
                current = read_communication_quota_notice(self.browser)
                if current is None or not current.limit_reached:
                    # 弹窗被用户同时关闭或替换也不代表额度恢复；已经确认过硬
                    # 上限语义，仍应结束且不能重放“立即沟通”。
                    raise DailyCommunicationLimitReachedError
                self.browser.click(
                    current.confirm_button,
                    description="关闭150位沟通上限弹窗",
                )
                self._wait(
                    lambda _b: read_communication_quota_notice(self.browser) is None,
                    "150位沟通上限弹窗关闭",
                )
                self.output("150位沟通上限弹窗已关闭，自动结束本次运行")
            except AutomationStopRequested:
                raise
            except DailyCommunicationLimitReachedError:
                raise
            except Exception as exc:  # noqa: BLE001 - 已确认硬上限后必须安全终止
                self.completion_warning = (
                    "已确认150位沟通上限，但“确定”按钮关闭或关闭校验失败："
                    f"{exc}"
                )
                self.output(f"[警告] {self.completion_warning}；仍将自动结束本次运行")
                raise DailyCommunicationLimitReachedError from exc
            raise DailyCommunicationLimitReachedError
        self._status(
            "沟通提醒",
            f"今日已沟通 {notice.contacted_count} 位，剩余 {notice.remaining_count} 次；正在关闭",
        )
        self.output(
            "检测到当日沟通次数提醒："
            f"已与 {notice.contacted_count} 位 BOSS 沟通，"
            f"还剩 {notice.remaining_count} 次；关闭后继续当前岗位"
        )
        self._pause_before("关闭当日沟通次数提醒")
        current = read_communication_quota_notice(self.browser)
        if current is None:
            return True
        self.browser.click(
            current.confirm_button,
            description="关闭当日沟通次数提醒",
        )
        self._wait(
            lambda _b: read_communication_quota_notice(self.browser) is None,
            "当日沟通次数提醒关闭",
        )
        self.output("当日沟通次数提醒已关闭，继续弹窗出现前的当前岗位")
        return True

    def _greeting_in_messages(self, greeting: str) -> bool:
        target = _chat_text_identity(greeting)
        for message in extract_chat_messages(self.browser):
            if message.from_me and _chat_text_identity(message.text) == target:
                return True
        return False

    def _conversation_for_sent_greeting(
        self, greeting: str
    ) -> ChatConversation | None:
        """用刚发送的完整招呼语反查左侧会话身份，校验最终公司名。"""

        target = _chat_text_identity(greeting)
        if not target:
            return None
        for conversation in extract_chat_conversations(self.browser):
            last = _chat_text_identity(conversation.last_message)
            if target in last or last in target:
                return conversation
        return None

    # ------------------------------------------------------------------ 记录落盘
    def _save_run_log(self, stats: AutomationStats, intents: JobIntentData) -> Path:
        self.run_directory.mkdir(parents=True, exist_ok=True)
        path = self.run_directory / f"boss_run_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        payload = {
            "schema_version": 1,
            "created_at": datetime.now().astimezone().isoformat(),
            "mode": (
                "dry_run"
                if self.config.dry_run
                else "fill_only"
                if self.config.fill_only
                else "execute"
            ),
            "intents": asdict(intents),
            "expectation_quotas": list(self._expectation_quotas),
            "expectation_completed": list(self._expectation_completed),
            "completion_reason": self.completion_reason,
            "completion_warning": self.completion_warning,
            "stats": {
                "inspected": stats.inspected,
                "matched": stats.matched,
                "skipped": stats.skipped,
                "sent": stats.sent,
                "failed": stats.failed,
                "scrolls": stats.scrolls,
                "resumes_sent": stats.resumes_sent,
                "conversations_pinned": stats.conversations_pinned,
                "conversations_already_pinned": stats.conversations_already_pinned,
            },
            "decisions": stats.decisions,
            "chat_actions": stats.chat_actions,
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path.resolve()

    def _save_checkpoint(
        self,
        status: str,
        stats: AutomationStats,
        intents: JobIntentData,
        visited: set[str],
        selected_companies: set[str],
    ) -> Path:
        self.run_directory.mkdir(parents=True, exist_ok=True)
        path = self.run_directory / "boss_checkpoint.json"
        temporary = path.with_suffix(".json.tmp")
        payload = {
            "schema_version": 1,
            "updated_at": datetime.now().astimezone().isoformat(),
            "status": status,
            "completion_reason": self.completion_reason,
            "completion_warning": self.completion_warning,
            "stage": self._current_step,
            "current_job": self._current_job,
            "mode": (
                "dry_run"
                if self.config.dry_run
                else "fill_only"
                if self.config.fill_only
                else "execute"
            ),
            "policy": asdict(self.policy) if self.policy is not None else None,
            "intents": asdict(intents),
            "expectation_quotas": list(self._expectation_quotas),
            "expectation_completed": list(self._expectation_completed),
            "selected_expectation": {
                "index": self._selected_expectation_index,
                "city": self._selected_city,
                "role": self._selected_role,
                "quota": self._selected_expectation_quota,
            },
            "visited_fingerprints": sorted(visited),
            "selected_companies": sorted(selected_companies),
            "handled_conversations": sorted(self._handled_conversations),
            "stats": {
                "inspected": stats.inspected,
                "matched": stats.matched,
                "skipped": stats.skipped,
                "sent": stats.sent,
                "failed": stats.failed,
                "scrolls": stats.scrolls,
                "resumes_sent": stats.resumes_sent,
                "conversations_pinned": stats.conversations_pinned,
                "conversations_already_pinned": stats.conversations_already_pinned,
            },
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        return path.resolve()

    def _scroll_one_card(self, cards: tuple[JobCard, ...]) -> None:
        """滚动岗位列表加载更多卡片。"""

        self._pause_before("滑动一张岗位卡片")
        self.browser.js(
            """
            const cards = document.querySelectorAll('[data-bossidx]');
            let dy = arguments[0];
            let scroller = null;
            if (cards.length) {
              let el = cards[0];
              while (el && el !== document.body) {
                const oy = getComputedStyle(el).overflowY;
                if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 20) { scroller = el; break; }
                el = el.parentElement;
              }
              dy = Math.max(dy, Math.round(cards[0].getBoundingClientRect().height * 1.2));
            }
            if (scroller) { scroller.scrollTop += dy; } else { window.scrollBy(0, dy); }
            """,
            card_scroll_distance(cards),
        )
        self._sleep(1.2)

    # ------------------------------------------------------------------ 主循环
    def run(self) -> tuple[AutomationStats, Path]:
        if self.policy is None or self.review_provider is None:
            raise BossAutomationError(
                "自动化运行必须同时配置筛选目标和大模型审核器；已停止以避免固定规则误判"
            )
        stats = self.stats
        self.completion_reason = None
        self.completion_warning = None
        intents = JobIntentData(())
        # checkpoint 中保留“求职期望序号:岗位指纹”；运行判重则按求职期望隔离，
        # 避免同一岗位出现在多个推荐列表时让后一个列表误以为已经扫描完。
        visited: set[str] = set()
        visited_by_expectation: list[set[str]] = []
        selected_companies: set[str] = set()
        recent_sent_companies = load_recent_sent_companies(self.run_directory)
        for company, sent_at in self.recent_sent_companies.items():
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=datetime.now().astimezone().tzinfo)
            previous = recent_sent_companies.get(company)
            if previous is None or sent_at > previous:
                recent_sent_companies[company] = sent_at
        recent_successful_applications = load_recent_successful_applications(
            self.run_directory
        )
        for key, sent_at in self.recent_successful_applications.items():
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=datetime.now().astimezone().tzinfo)
            previous = recent_successful_applications.get(key)
            if previous is None or sent_at > previous:
                recent_successful_applications[key] = sent_at
        stagnant_scrolls = 0
        expectation_index = 0

        def write_checkpoint(status: str) -> None:
            path = self._save_checkpoint(
                status, stats, intents, visited, selected_companies
            )
            if status == "paused":
                self._status("已暂停", f"断点已保存：{path}")
            elif status == "running":
                self._status("继续运行", self._current_job or "正在从断点继续")

        self._checkpoint_writer = write_checkpoint
        if self.control:
            self.control.set_pause_callbacks(
                on_pause=self._on_paused,
                on_resume=self._on_resumed,
                apply_settings=self._apply_pending_settings,
            )

        try:
            self._control_point()
            self.ensure_ready()
            intents = self.read_job_intents()
            intents = select_policy_job_intents(
                intents,
                self.policy.selected_expectation if self.policy else None,
            )
            # 后续从消息页/详情页恢复时 open_recommendations() 不再收到显式
            # intents 参数，必须让默认来源也使用 GUI 收窄后的列表。否则 index=0
            # 会重新落到原始网页列表第一条（例如深圳/C/C++）。
            self._job_intents = intents
            self._expectation_quotas = allocate_expectation_quotas(
                intents,
                self.policy.target_companies,
            )
            self._expectation_completed = [0 for _ in intents.expectations]
            visited_by_expectation = [
                set() for _expectation in intents.expectations
            ]
            if self.policy.selected_expectation is not None:
                selected = intents.expectations[0]
                self.output(
                    "已按 GUI 选择锁定求职意向："
                    f"{selected.city or '不限城市'} / {selected.role}；"
                    f"全部 {self.policy.target_companies} 家只由该意向投递"
                )
            elif len(intents.expectations) == 1:
                self.output(
                    f"仅有 1 条求职期望，全部 {self.policy.target_companies} 家由该期望投递"
                )
            else:
                self.output(
                    "求职期望投递配额（按页面顺序）："
                    + "；".join(
                        f"{item.city or '不限城市'}/{item.role}={quota}家"
                        for item, quota in zip(
                            intents.expectations,
                            self._expectation_quotas,
                        )
                    )
                )

            def activate_expectation(index: int) -> None:
                expectation = intents.expectations[index]
                self._selected_expectation_index = index
                self._selected_expectation_quota = self._expectation_quotas[index]
                self._selected_city = expectation.city
                self._selected_role = expectation.role
                self.output(
                    f"开始第 {index + 1} 条求职期望："
                    f"{expectation.city or '不限城市'} / {expectation.role}，"
                    f"本条配额 {self._selected_expectation_quota} 家"
                )
                self.open_recommendations(
                    intents,
                    expectation=expectation,
                )

            def advance_expectation(*, exhausted: bool = False) -> bool:
                nonlocal expectation_index, stagnant_scrolls
                current = intents.expectations[expectation_index]
                quota = self._expectation_quotas[expectation_index]
                expectation_completed = self._expectation_completed[expectation_index]
                if exhausted and expectation_completed < quota:
                    self.output(
                        f"第 {expectation_index + 1} 条求职期望已无新岗位："
                        f"{current.city or '不限城市'} / {current.role}，"
                        f"完成 {expectation_completed}/{quota} 家；不挪用其它期望配额"
                    )
                else:
                    self.output(
                        f"第 {expectation_index + 1} 条求职期望已达到配额："
                        f"{expectation_completed}/{quota} 家"
                    )
                expectation_index += 1
                stagnant_scrolls = 0
                while (
                    expectation_index < len(intents.expectations)
                    and self._expectation_quotas[expectation_index] == 0
                ):
                    skipped = intents.expectations[expectation_index]
                    self.output(
                        f"跳过第 {expectation_index + 1} 条求职期望："
                        f"{skipped.city or '不限城市'} / {skipped.role}，分配配额为 0"
                    )
                    expectation_index += 1
                if expectation_index >= len(intents.expectations):
                    return False
                activate_expectation(expectation_index)
                return True

            while (
                expectation_index < len(intents.expectations)
                and self._expectation_quotas[expectation_index] == 0
            ):
                expectation_index += 1
            if expectation_index >= len(intents.expectations):
                raise BossAutomationError("目标公司数没有分配到任何求职期望")
            activate_expectation(expectation_index)
            if self.config.inspect_unread_messages:
                self._status("消息巡检", "启动任务时正在核实现有未读消息")
                self.process_unread_messages(stats, force=True)
                self._last_message_check_inspected = stats.inspected
                self._jobs_since_message_check = 0

            def target_available() -> bool:
                return (
                    expectation_index < len(intents.expectations)
                    and stats.matched < self.policy.target_companies
                )

            while has_job_capacity(
                self.config.max_jobs, stats.inspected
            ) and target_available():
                self._control_point()
                cards = self._extract_job_cards()
                current_visited = visited_by_expectation[expectation_index]
                pending_by_fingerprint: dict[str, JobCard] = {}
                for card in cards:
                    if card.fingerprint not in current_visited:
                        pending_by_fingerprint.setdefault(card.fingerprint, card)
                pending = list(pending_by_fingerprint.values())
                if not pending:
                    stagnant_scrolls += 1
                    if stagnant_scrolls >= self.config.stagnant_scroll_limit:
                        if advance_expectation(exhausted=True):
                            continue
                        break
                    self._scroll_one_card(cards)
                    stats.scrolls += 1
                    continue

                stagnant_scrolls = 0
                expectation_switched = False
                # 每轮只消费最新 DOM 快照中的一张卡。进入详情后的 finally 会重新
                # 点击求职意向，Boss 可能随即刷新整份推荐列表；若继续遍历本轮缓存
                # 的 pending，后续卡片对象就已经过期，会成批误报“无法定位岗位”。
                # 本地跳过/API 初筛失败同样在下一轮重读 DOM，统一保证状态新鲜。
                for card in pending[:1]:
                    self._control_point()
                    # 卡片初筛失败/本地硬门禁跳过时也会累计 inspected；在处理下一张
                    # 卡片前按间隔核实，避免长时间没有成功投递时完全不看新消息。
                    if self._inspect_messages_if_due(stats):
                        break
                    self._item_started_at = time.monotonic()
                    self._current_job = (
                        f"{card.company_name or '未知公司'} / {card.job_name}"
                    )
                    if not has_job_capacity(self.config.max_jobs, stats.inspected):
                        break
                    current_visited.add(card.fingerprint)
                    visited.add(f"{expectation_index}:{card.fingerprint}")
                    card_review = None
                    rejection = card_rejection_reason(
                        card, self.policy, self._resume_degree_level
                    )
                    if not rejection:
                        self._status(
                            "大模型卡片初筛",
                            f"{card.location or '地点未知'} / {card.job_name}",
                        )
                        try:
                            card_review = self.review_provider.review_card(
                                card, self.policy, self.resume, self.resume_text
                            )
                        except ReviewError as exc:
                            stats.inspected += 1
                            stats.failed += 1
                            self.output(f"[失败] 卡片初筛失败，跳过该岗位：{exc}")
                            self._emit_result(stats, self._card_failure_record(card, exc))
                            continue
                        review_rejection = card_review_rejection_reason(
                            card, card_review
                        )
                        if review_rejection:
                            rejection = review_rejection
                    exact_success = find_recent_successful_application(
                        card.company_name,
                        card.job_name,
                        recent_successful_applications,
                    )
                    if (
                        not rejection
                        and not self.config.dry_run
                        and not self.config.fill_only
                        and exact_success is not None
                    ):
                        rejection = (
                            "同公司同岗位30天内已发送成功"
                            f"（{exact_success.astimezone():%Y-%m-%d}）"
                        )
                    company_key = (card.company_name or "").casefold().replace(" ", "")
                    if not rejection and company_key and company_key in selected_companies:
                        rejection = "本轮已选择过该公司"
                    recent_application = find_recent_company_application(
                        card.company_name, recent_sent_companies
                    )
                    needs_contact_check = (
                        not rejection
                        and not self.config.dry_run
                        and not self.config.fill_only
                        and recent_application is not None
                    )

                    direction_trace = self._direction_trace(card_review)
                    if rejection:
                        stats.inspected += 1
                        stats.skipped += 1
                        record = {
                            "created_at": datetime.now().astimezone().isoformat(),
                            "company_name": card.company_name,
                            "job_name": card.job_name,
                            "location": card.location,
                            "salary": card.salary,
                            "company_scale": card.company_scale,
                            "recruiter_activity": card.recruiter_activity,
                            "qualifications_summary": f"未进入详情页：{rejection}",
                            "score": 0,
                            "should_apply": False,
                            "reasons": [rejection],
                            "matched_skills": [],
                            **direction_trace,
                            "greeting": None,
                            "delivery_status": "未投递",
                            "message_sent": False,
                        }
                        self.output(f"跳过岗位：{card.job_name} / {rejection}")
                        self._emit_result(stats, record)
                        continue

                    counted = False
                    sending_started = False
                    self._current_send_started = False
                    application_completed = False
                    sent_successfully = False
                    try:
                        stats.inspected += 1
                        counted = True
                        self.output(
                            f"\n读取岗位：{card.job_name} / {card.company_name or '未知公司'}"
                        )
                        self._status(
                            "岗位详情",
                            f"{card.company_name or '未知公司'} / {card.job_name}",
                        )
                        self._open_card(card)
                        if needs_contact_check and self.recruiter_already_contacted(card):
                            previous_company, sent_at = recent_application
                            reason = (
                                f"该岗位招聘者已沟通过（本公司30天内投递记录："
                                f"{sent_at.astimezone():%Y-%m-%d}，历史公司名：{previous_company}）"
                            )
                            stats.skipped += 1
                            self.output(f"跳过岗位：{card.job_name} / {reason}")
                            self._emit_result(
                                stats,
                                {
                                    "created_at": datetime.now().astimezone().isoformat(),
                                    "company_name": card.company_name,
                                    "job_name": card.job_name,
                                    "location": card.location,
                                    "salary": card.salary,
                                    "company_scale": card.company_scale,
                                    "recruiter_activity": card.recruiter_activity,
                                    "qualifications_summary": reason,
                                    "score": 0,
                                    "should_apply": False,
                                    "reasons": [reason],
                                    "matched_skills": [],
                                    **direction_trace,
                                    "greeting": None,
                                    "delivery_status": "未投递",
                                    "message_sent": False,
                                },
                            )
                            continue
                        snapshot = align_detail_identity(
                            read_job_detail(self.browser), card
                        )
                        if not snapshot.job_data.is_boss_job_detail_page:
                            raise BossAutomationError(
                                f"未能读取到完整岗位详情：{card.job_name}"
                            )
                        save_result = self.store.save_snapshot(
                            snapshot, self.artifact_directory
                        )
                        self._status("详情匹配", "正在结合完整岗位信息和简历进行判断")
                        reviewed = self.review_provider.review_detail(
                            card,
                            snapshot.job_data,
                            self.policy,
                            intents,
                            self.resume,
                            self.resume_text,
                        )
                        weekend_text = " ".join(
                            part
                            for part in (
                                card.job_name,
                                getattr(snapshot.job_data.job_name, "value", None),
                                getattr(snapshot.job_data.job_description, "value", None),
                            )
                            if part
                        )
                        weekend_ok = weekend_meets(weekend_text, self.policy.weekend_rest)
                        decision = MatchDecision(
                            should_apply=reviewed.should_apply and weekend_ok,
                            score=reviewed.score,
                            matched_expectation=None,
                            matched_skills=reviewed.matched_skills,
                            reasons=reviewed.reasons,
                        )
                        greeting = reviewed.greeting
                        qualifications_summary = reviewed.qualifications_summary
                        if not decision.should_apply:
                            causes: list[str] = []
                            if not weekend_ok:
                                causes.append(
                                    f"周末休息不符（设置：{self.policy.weekend_rest}）"
                                )
                            if reviewed.hard_experience_requirement_conflicts:
                                causes.append(
                                    "工作年限不符："
                                    + "、".join(
                                        reviewed.hard_experience_requirement_conflicts
                                    )
                                )
                            if reviewed.matched_detail_excluded_direction_keywords:
                                causes.append(
                                    "命中排除方向："
                                    + "、".join(
                                        reviewed.matched_detail_excluded_direction_keywords
                                    )
                                )
                            if not causes:
                                causes = list(reviewed.reasons[:2])
                            qualifications_summary = "未投递原因：" + "；".join(causes)
                        self._status("生成完毕", greeting)
                        if self.greeting_callback:
                            self.greeting_callback(greeting)
                        if not decision.should_apply:
                            stats.skipped += 1
                        self.output(
                            f"判断：{'投递' if decision.should_apply else '不投递'} / "
                            f"匹配分 {decision.score} / 数据库 {save_result.action.value}"
                        )
                        for reason in decision.reasons:
                            self.output(f"  {reason}")
                        delivery_status = "未投递"
                        record_company = card.company_name
                        record_job_name = card.job_name
                        if decision.should_apply:
                            self.output(f"招呼语：{greeting}")
                            if self.config.dry_run:
                                delivery_status = "演练未发送"
                            elif self.config.fill_only:
                                self._fill_or_send_greeting(
                                    greeting,
                                    send=False,
                                    expected_card=card,
                                )
                                delivery_status = "已填充未发送"
                            else:
                                sending_started = True
                                self._fill_or_send_greeting(
                                    greeting,
                                    send=True,
                                    expected_card=card,
                                )
                                stats.sent += 1
                                delivery_status = "发送成功"
                                sent_successfully = True
                                sent_conversation = (
                                    self._conversation_for_sent_greeting(greeting)
                                )
                                if (
                                    sent_conversation is not None
                                    and sent_conversation.company_name
                                ):
                                    record_company = sent_conversation.company_name
                                sent_company = record_company
                                if sent_company:
                                    recent_sent_companies[sent_company] = (
                                        datetime.now().astimezone()
                                    )
                                    recent_successful_applications[
                                        (sent_company, record_job_name)
                                    ] = datetime.now().astimezone()
                                # 卡片名和聊天列表名都是可靠身份；若二者因品牌别名不同，
                                # 两个名字都加入本轮去重，但最终记录以真实聊天会话为准。
                                if card.company_name and card.company_name != sent_company:
                                    recent_sent_companies[card.company_name] = (
                                        datetime.now().astimezone()
                                    )
                                    recent_successful_applications[
                                        (card.company_name, record_job_name)
                                    ] = datetime.now().astimezone()
                            stats.matched += 1
                            self._expectation_completed[expectation_index] += 1
                            application_completed = delivery_status == "发送成功"
                            if company_key:
                                selected_companies.add(company_key)
                        self._emit_result(
                            stats,
                            {
                                "created_at": datetime.now().astimezone().isoformat(),
                                "job_name": record_job_name,
                                "company_name": record_company,
                                "location": snapshot.location or card.location,
                                "salary": snapshot.salary or card.salary,
                                "recruiter_activity": card.recruiter_activity,
                                "qualifications_summary": qualifications_summary,
                                "score": decision.score,
                                "should_apply": decision.should_apply,
                                "reasons": list(decision.reasons),
                                "matched_skills": list(decision.matched_skills),
                                "matched_detail_excluded_direction_keywords": list(
                                    reviewed.matched_detail_excluded_direction_keywords
                                ),
                                "hard_experience_requirement_conflicts": list(
                                    reviewed.hard_experience_requirement_conflicts
                                ),
                                **direction_trace,
                                "greeting": greeting if decision.should_apply else None,
                                "delivery_status": delivery_status,
                                "message_sent": delivery_status == "发送成功",
                            },
                        )
                    except RuntimeConditionsChangedError:
                        if counted:
                            stats.inspected = max(0, stats.inspected - 1)
                        current_visited.discard(card.fingerprint)
                        visited.discard(f"{expectation_index}:{card.fingerprint}")
                        self.output(
                            "暂停期间运行条件已修改：当前未完成岗位不再沿用旧判断，"
                            "返回列表后将按新条件重新读取"
                        )
                        continue
                    except (AutomationStopRequested, DailyCommunicationLimitReachedError):
                        raise
                    except (
                        PageReadError,
                        JobStoreError,
                        WebSelectionError,
                        BrowserError,
                        BossAutomationError,
                        ReviewError,
                    ) as exc:
                        stats.failed += 1
                        self.output(f"[失败] 岗位处理失败：{exc}")
                        if not counted:
                            stats.inspected += 1
                        failed_greeting = getattr(exc, "failed_greeting", None)
                        if failed_greeting and self.greeting_callback:
                            self.greeting_callback(f"[未通过硬校验·未发送] {failed_greeting}")
                        self._emit_result(
                            stats,
                            {
                                "created_at": datetime.now().astimezone().isoformat(),
                                "job_name": card.job_name,
                                "company_name": card.company_name,
                                "location": card.location,
                                "salary": card.salary,
                                "company_scale": card.company_scale,
                                "recruiter_activity": card.recruiter_activity,
                                "qualifications_summary": str(exc),
                                "score": 0,
                                "should_apply": False,
                                "reasons": [str(exc)],
                                "matched_skills": [],
                                **direction_trace,
                                "greeting": failed_greeting,
                                "delivery_status": (
                                    "发送失败" if sending_started else "处理失败"
                                ),
                                "message_sent": False,
                            },
                        )
                    finally:
                        try:
                            if (
                                self.completion_reason is None
                                and not (self.control and self.control.stop_requested)
                            ):
                                self._return_to_recommendations(
                                    immediate=sent_successfully
                                )
                        finally:
                            self._current_job = None
                            self._current_send_started = False

                    entered_messages = (
                        self._inspect_messages_between_jobs(stats)
                        if application_completed
                        else False
                    )
                    quota = self._expectation_quotas[expectation_index]
                    if self._expectation_completed[expectation_index] >= quota:
                        expectation_switched = advance_expectation()
                        break
                    if entered_messages:
                        break
                    if not target_available():
                        break

                if not target_available() or not has_job_capacity(
                    self.config.max_jobs, stats.inspected
                ):
                    break
                if expectation_switched:
                    continue
                # 详情返回或消息巡检都会重新加载推荐列表。不能继续依赖本轮开头
                # 缓存的 cards；先读取当前 DOM，有尚未处理的卡片就直接开始下一轮，
                # 只有当前页面确实没有新卡片时才滚动加载更多。
                current_cards = self._extract_job_cards()
                current_visited = visited_by_expectation[expectation_index]
                if any(
                    card.fingerprint not in current_visited
                    for card in current_cards
                ):
                    continue
                self._scroll_one_card(current_cards)
                stats.scrolls += 1

            self._current_job = None
            # 最后一个岗位的 finally / 消息巡检通常已经恢复了推荐页；避免任务
            # 收尾时再次点击“职位”和求职意向，造成无意义的第三次页面闪动。
            if self._current_page() != "recommendations":
                self._return_to_recommendations()
            log_path = self._save_run_log(stats, intents)
            write_checkpoint("completed")
            return stats, log_path
        except UpdatedTargetReachedError as exc:
            self._current_job = None
            self._status("运行结束", str(exc))
            self.output(f"运行条件已更新：{exc}，本次任务直接结束")
            log_path = self._save_run_log(stats, intents)
            write_checkpoint("completed")
            return stats, log_path
        except DailyCommunicationLimitReachedError:
            self._current_job = None
            detail = DAILY_COMMUNICATION_LIMIT_REASON
            if self.completion_warning:
                detail += f"；{self.completion_warning}"
            self._status("运行结束", detail)
            log_path = self._save_run_log(stats, intents)
            write_checkpoint("completed")
            return stats, log_path
        except AutomationStopRequested as exc:
            self._status("已停止", "已保存停止前的投递进度")
            log_path = self._save_run_log(stats, intents)
            write_checkpoint("stopped")
            raise AutomationStoppedError(stats, log_path) from exc
        except Exception:
            try:
                log_path = self._save_run_log(stats, intents)
                self.output(f"运行异常中断，已保存中断前的投递记录：{log_path}")
                write_checkpoint("failed")
            except Exception as save_error:  # pragma: no cover
                self.output(f"[警告] 中断记录保存失败：{save_error}")
            raise
        finally:
            if self.control:
                self.control.set_pause_callbacks()

    # ------------------------------------------------------------------ 辅助
    def recruiter_already_contacted(self, card: JobCard) -> bool:
        """进入该岗位会话页核对是否已发过我方消息；“继续沟通”不代表发过消息。"""

        try:
            entry = self._wait_for_displayed(
                lambda: find_chat_entry(self.browser),
                f"岗位“{card.job_name}”的沟通入口",
            )
        except ElementNotFoundError as exc:
            raise BossAutomationError(
                f"等待 {self.config.page_wait_seconds:g} 秒后仍未出现岗位沟通入口，"
                "无法安全核对历史沟通记录"
            ) from exc
        if self.browser.text_of(entry) == CHAT_ENTRY_TEXTS[0]:
            return False  # “立即沟通”表示从未沟通过
        self._status("核对沟通记录", f"确认是否已与{card.company_name or '该公司'}沟通过")
        self._click_when_ready(
            lambda: find_chat_entry(self.browser),
            "进入会话页核对沟通记录",
        )
        self._sleep(1.5)
        if self.browser.driver and len(self.browser.driver.window_handles) > 1:
            self._pause_before("切换到沟通记录页面")
            self.browser.consolidate_windows()
        try:
            contacted = any(
                message.from_me for message in extract_chat_messages(self.browser)
            )
        except (BrowserError, WebSelectionError):
            contacted = False
        return contacted

    @staticmethod
    def _direction_trace(card_review) -> dict[str, object]:
        return {
            "resume_inferred_directions": list(
                card_review.resume_inferred_directions if card_review else ()
            ),
            "combined_directions": list(
                card_review.combined_directions if card_review else ()
            ),
            "matched_direction_keywords": list(
                card_review.matched_direction_keywords if card_review else ()
            ),
            "excluded_direction_match": bool(
                card_review.excluded_direction_match if card_review else False
            ),
            "matched_excluded_direction_keywords": list(
                card_review.matched_excluded_direction_keywords if card_review else ()
            ),
        }

    @staticmethod
    def _card_failure_record(card: JobCard, exc: Exception) -> dict[str, object]:
        return {
            "created_at": datetime.now().astimezone().isoformat(),
            "company_name": card.company_name,
            "job_name": card.job_name,
            "location": card.location,
            "salary": card.salary,
            "company_scale": card.company_scale,
            "recruiter_activity": card.recruiter_activity,
            "qualifications_summary": f"卡片初筛失败：{exc}",
            "score": 0,
            "should_apply": False,
            "reasons": [str(exc)],
            "matched_skills": [],
            "resume_inferred_directions": [],
            "combined_directions": [],
            "matched_direction_keywords": [],
            "excluded_direction_match": False,
            "matched_excluded_direction_keywords": [],
            "greeting": None,
            "delivery_status": "处理失败",
            "message_sent": False,
        }
