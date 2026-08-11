"""浅色扁平 Tkinter 控制台：配置、状态、招呼语和滚动结果表。"""

from __future__ import annotations

import ctypes
import queue
import re
import sys
import threading
import tkinter as tk
from dataclasses import asdict, replace
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk

from boss_assistant import __version__
from boss_assistant.automation import (
    AutomationConfig,
    AutomationControl,
    AutomationMySqlStore,
    AutomationPolicy,
    AutomationStats,
    AutomationStopRequested,
    CodexCliReviewProvider,
    JobExpectation,
    JobIntentData,
    MySqlConfig,
    MySqlStoreError,
    ReviewError,
    parse_terms,
)
from boss_assistant.automation.runner import (
    AutomationStoppedError,
    BossAutomationError,
    BossAutomationRunner,
)
from boss_assistant.automation.api_provider import (
    ApiProviderConfig,
    OpenAICompatibleReviewProvider,
)
from boss_assistant.browser import (
    BrowserError,
    EdgeBrowser,
    LoginRequiredError,
)
from boss_assistant.browser.driver import GEEK_JOBS_URL
from boss_assistant.paths import bundled_icon, runtime_root
from boss_assistant.resume import ResumeInboxError, ResumePdfError, process_inbox_resume
from boss_assistant.storage import JobStore, JobStoreError
from boss_assistant.web import parse_job_intents


MODE_FILL_ONLY = "仅填充不发送"
MODE_SEND = "实际发送"
REVIEW_MODE_CODEX = "Codex主导"
REVIEW_MODE_API = "大模型API"
DEFAULT_RUN_MODE = MODE_SEND
DEFAULT_REVIEW_MODE = REVIEW_MODE_API
API_CONFIG_PATH = runtime_root() / "config" / "model_api.local.json"
APP_TITLE = f"Boss 求职助手控制台-Win v{__version__}"
HEADER_TITLE = "Boss 求职助手控制台-Win"
GUI_DEFAULTS_PATH = runtime_root() / "config" / "gui_defaults.txt"
# 周末休息设置：不限/双休/大小周/单休（默认不限）。
WEEKEND_OPTIONS = ("不限", "双休", "大小周", "单休")
# 经验要求设置：经验不限/1-3年/3-5年/5-10年（默认1-3年）。
EXPERIENCE_OPTIONS = ("经验不限", "1-3年", "3-5年", "5-10年")
AVERAGE_EXPECTATION_OPTION = "平均次数"
_EXPECTATION_RETRY_MS = 5000

# 浅色扁平主题配色：整体一套中性蓝灰 + 单一强调蓝，避免默认 ttk 的生硬观感。
_UI_FONT = "Microsoft YaHei UI"
_UI_APP_BG = "#eef2f8"        # 窗口底色（卡片之间的留白）
_UI_CARD_BG = "#ffffff"       # 卡片/输入区底色
_UI_BORDER = "#dbe2ec"        # 细边框
_UI_TEXT = "#1f2937"          # 正文
_UI_HEADING = "#0f172a"       # 标题
_UI_MUTED = "#64748b"         # 次要说明文字
_UI_ACCENT = "#2563eb"        # 强调色（主按钮、卡片标题）
_UI_ACCENT_HOVER = "#1d4ed8"
_UI_ACCENT_SOFT = "#eff4ff"   # 强调色浅底（状态条、幽灵按钮悬停）
_UI_DANGER = "#e11d48"
_UI_DANGER_HOVER = "#be123c"
_UI_TREE_HEAD_BG = "#f1f5f9"
_UI_SEL_BG = "#dbe8ff"        # 结果表选中行
_UI_ROW_ALT = "#f6f8fc"       # 斑马纹交替行
_UI_ROW_FAIL_BG = "#fef2f2"   # 处理/发送失败行
_UI_ROW_FAIL_FG = "#b91c1c"
_UI_ROW_OK_BG = "#f0fdf4"     # 发送成功行
_UI_ROW_OK_FG = "#15803d"
_UI_ROW_CHAT_BG = "#eff6ff"   # 消息巡检行（回复HR / 置顶待处理）
_UI_ROW_CHAT_FG = "#1d4ed8"
# 单元格查看条：判断文字是否被截断时预留的左右内边距，以及查看条的最小宽度。
_CELL_TEXT_PADDING = 12
_CELL_VIEWER_MIN_WIDTH = 380
# “投递情况”列的固定宽度：最长文案“已回复，简历此前已发送”实测 132px，留出内边距。
_RESULT_COLUMN_WIDTH = 148


_GUI_MANUAL_FIELD_DEFAULTS = {
    "不打招呼公司": "无",
    "目标岗位方向": "",
    "排除岗位方向": "无",
    "目标城市": "",
    "薪资下限": "",
    "薪资上限": "",
    "公司规模": "",
    "目标公司数": "50",
    "最低匹配分": "50",
    "MySQL主机": "127.0.0.1",
    "MySQL端口": "3306",
    "MySQL用户名": "",
    "MySQL密码": "",
    "MySQL数据库": "boss_job_assistant",
}
_GUI_EMPTY_FIELD_DEFAULTS = {
    key: "无" if key in {"不打招呼公司", "排除岗位方向"} else ""
    for key in _GUI_MANUAL_FIELD_DEFAULTS
}


def parse_gui_defaults(text: str) -> dict[str, str]:
    """解析GUI手填字段；未知字段和下拉框字段不会进入结果。"""

    values = dict(_GUI_MANUAL_FIELD_DEFAULTS)
    for raw_line in text.splitlines():
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([^:：]+)[:：](.*)$", line)
        if match is None:
            continue
        label = match.group(1).strip()
        if label not in values:
            continue
        cleaned = match.group(2).strip()
        # TXT约定每个字段行以中文句号收尾；句号是格式分隔符，不属于字段值。
        if cleaned.endswith("。"):
            cleaned = cleaned[:-1].rstrip()
        values[label] = cleaned or _GUI_EMPTY_FIELD_DEFAULTS[label]
    return values


def load_gui_defaults(
    path: str | Path = GUI_DEFAULTS_PATH,
) -> dict[str, str]:
    """读取本地TXT默认值；文件缺失或不可读时沿用程序原有默认值。"""

    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return dict(_GUI_MANUAL_FIELD_DEFAULTS)
    return parse_gui_defaults(text)


def format_expectation_option(expectation: JobExpectation) -> str:
    """把页面求职意向格式化为下拉框中稳定、可读的唯一选项。"""

    return f"{expectation.city or '不限城市'} / {expectation.role}"


def fetch_job_intents_for_gui(
    *,
    browser_factory=EdgeBrowser,
    timeout: float = 12.0,
) -> JobIntentData:
    """在正式运行前用独立 CDP 会话读取网页求职意向，不启动投递流程。"""

    browser = browser_factory(
        attach_existing_only=True,
        require_boss_page=True,
    )
    try:
        browser.start()
        if not browser.is_logged_in():
            raise LoginRequiredError("当前登录专用 Edge 尚未登录 Boss直聘")
        if "web/geek/jobs" not in browser.current_url:
            browser.open(GEEK_JOBS_URL)
        browser.wait_for(
            lambda _browser: bool(parse_job_intents(browser).expectations),
            timeout=timeout,
            description="GUI 求职意向下拉选项",
        )
        intents = parse_job_intents(browser)
        if not intents.expectations:
            raise BrowserError("Boss 职位页没有读取到求职意向")
        return intents
    finally:
        browser.quit()


def _primary_work_area(
    fallback_width: int,
    fallback_height: int,
) -> tuple[int, int, int, int]:
    """返回Windows主屏工作区，避免初始窗口落到任务栏后面。"""

    try:
        class Rect(ctypes.Structure):
            _fields_ = (
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            )

        rect = Rect()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except (AttributeError, OSError):
        pass
    return 0, 0, fallback_width, fallback_height


def next_review_mode(current: str) -> str:
    return REVIEW_MODE_API if current == REVIEW_MODE_CODEX else REVIEW_MODE_CODEX


def review_mode_status(current: str) -> str:
    return (
        "当前：大模型API（按本地配置，默认）"
        if current == REVIEW_MODE_API
        else "当前：Codex主导"
    )


def review_mode_button_text(current: str) -> str:
    return "切换至Codex主导" if current == REVIEW_MODE_API else "切换至大模型API"


def result_view_is_at_bottom(
    yview: tuple[float, float] | list[float],
    *,
    tolerance: float = 1e-6,
) -> bool:
    """只有用户当前位于表格底部时，新记录才继续自动跟随。"""

    if len(yview) < 2:
        return True
    return float(yview[1]) >= 1.0 - tolerance


def format_run_completion(
    completion_reason: str | None,
    log_path: object,
    completion_warning: str | None = None,
) -> tuple[str, str]:
    """生成正常完成或Boss硬上限提前结束时的GUI状态。"""

    if completion_reason:
        detail = f"结束理由：{completion_reason}"
        if completion_warning:
            detail += f"；提示：{completion_warning}"
        return "运行结束", f"{detail}；运行记录：{log_path}"
    return "运行完成", f"运行记录：{log_path}"


def _set_window_icon(window: "tk.Tk") -> None:
    """Windows 下把应用图标设为窗口图标；图标缺失时静默跳过。"""

    try:
        candidate = bundled_icon("boss_assistant.ico")
        if candidate.is_file():
            window.iconbitmap(str(candidate))
    except Exception:
        pass


def _enable_windows_dpi_awareness() -> None:
    """在创建 Tk 窗口前启用系统 DPI 感知，避免图标和界面被 DWM 二次放大。"""

    try:
        # PROCESS_SYSTEM_DPI_AWARE。必须早于第一个 HWND；125% 缩放时这会让
        # Tk 直接请求 40×40 大图标，而不是先取 32×32 再由 Windows 放大。
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except (AttributeError, OSError):
        pass
    try:
        # Windows 7 兼容回退。
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


class BossControlPanel(tk.Tk):
    def __init__(self) -> None:
        _enable_windows_dpi_awareness()
        super().__init__()
        self.title(APP_TITLE)
        _set_window_icon(self)
        screen_width = self.winfo_screenwidth()
        screen_height = self.winfo_screenheight()
        work_left, work_top, work_right, work_bottom = _primary_work_area(
            screen_width,
            screen_height,
        )
        work_width = work_right - work_left
        work_height = work_bottom - work_top
        initial_width = min(1560, max(1320, work_width - 40))
        initial_height = min(920, max(720, work_height - 40))
        initial_x = work_left + max(0, (work_width - initial_width) // 2)
        initial_y = work_top + max(0, (work_height - initial_height) // 2)
        self.geometry(
            f"{initial_width}x{initial_height}+{initial_x}+{initial_y}"
        )
        self.minsize(min(1560, work_width), 700)
        self.configure(bg=_UI_APP_BG)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.running = False
        self.control: AutomationControl | None = None
        self._worker_thread: threading.Thread | None = None
        self._intent_probe_thread: threading.Thread | None = None
        self._intent_probe_running = False
        self._expectation_by_option: dict[str, JobExpectation] = {}
        self._close_pending = False
        # 表格里每一行对应的原始结果，供“选中行完整内容”展开全文。
        self._row_records: dict[str, dict[str, object]] = {}
        # 自动跟随是用户滚动意图，而不是每次插入前的一次 yview 快照。Tk 在连续
        # insert/see 时可能尚未完成滚动范围重算，临时返回“未到底部”；若直接拿这个
        # 值做下一条记录的开关，跟随几条后就会被错误地永久关闭。
        self._follow_latest_results = True
        # TXT只在本GUI实例初次构造时读取一次。界面建立后，开始运行及暂停修改
        # 均只读取StringVar当前值，绝不重新读取TXT覆盖用户在界面中的改动。
        self._startup_defaults = load_gui_defaults()
        self._build_style()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)
        # GUI 建好后异步读取网页，不初始化简历/MySQL/审核器，也不进入岗位循环。
        self.after(200, self._start_job_intent_probe)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        base = (_UI_FONT, 10)

        # 容器：根/普通 Frame 用中性底，卡片内的 Frame 用白底以免标签背景不一致。
        style.configure("TFrame", background=_UI_APP_BG)
        style.configure("Card.TFrame", background=_UI_CARD_BG)

        # 卡片（LabelFrame）：白底、细边框、彩色粗标题。
        style.configure(
            "TLabelframe",
            background=_UI_CARD_BG,
            bordercolor=_UI_BORDER,
            lightcolor=_UI_BORDER,
            darkcolor=_UI_BORDER,
            relief="solid",
            borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=_UI_CARD_BG,
            foreground=_UI_ACCENT,
            font=(_UI_FONT, 11, "bold"),
        )

        # 文本层级。
        style.configure("TLabel", background=_UI_CARD_BG, foreground=_UI_TEXT, font=base)
        style.configure("Field.TLabel", background=_UI_CARD_BG, foreground=_UI_MUTED, font=(_UI_FONT, 9))
        style.configure("Sub.TLabel", background=_UI_CARD_BG, foreground=_UI_MUTED, font=(_UI_FONT, 9))
        style.configure("Header.TLabel", background=_UI_APP_BG, foreground=_UI_HEADING, font=(_UI_FONT, 19, "bold"))
        style.configure("HeaderSub.TLabel", background=_UI_APP_BG, foreground=_UI_MUTED, font=(_UI_FONT, 9))
        style.configure(
            "Status.TLabel",
            background=_UI_ACCENT_SOFT,
            foreground=_UI_ACCENT_HOVER,
            font=(_UI_FONT, 11, "bold"),
            padding=(10, 5),
        )

        # 输入控件：扁平细边框，聚焦时描边变强调色。
        style.configure(
            "TEntry",
            fieldbackground=_UI_CARD_BG,
            foreground=_UI_TEXT,
            bordercolor=_UI_BORDER,
            lightcolor=_UI_BORDER,
            darkcolor=_UI_BORDER,
            insertcolor=_UI_ACCENT,
            padding=(6, 6),
            relief="flat",
        )
        style.map(
            "TEntry",
            bordercolor=[("focus", _UI_ACCENT)],
            lightcolor=[("focus", _UI_ACCENT)],
            darkcolor=[("focus", _UI_ACCENT)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=_UI_CARD_BG,
            background=_UI_CARD_BG,
            foreground=_UI_TEXT,
            bordercolor=_UI_BORDER,
            lightcolor=_UI_BORDER,
            darkcolor=_UI_BORDER,
            arrowcolor=_UI_MUTED,
            padding=(9, 6),
            relief="flat",
        )
        style.map(
            "TCombobox",
            bordercolor=[("focus", _UI_ACCENT)],
            fieldbackground=[("readonly", _UI_CARD_BG)],
            foreground=[("readonly", _UI_TEXT)],
        )

        # 主按钮 / 危险按钮 / 次级幽灵按钮。
        style.configure("Accent.TButton", background=_UI_ACCENT, foreground="#ffffff", font=(_UI_FONT, 10, "bold"), padding=(24, 10), borderwidth=0, relief="flat")
        style.map("Accent.TButton", background=[("active", _UI_ACCENT_HOVER), ("disabled", "#b6c2d6")], foreground=[("disabled", "#eef2f8")])
        style.configure("Danger.TButton", background=_UI_DANGER, foreground="#ffffff", font=(_UI_FONT, 10, "bold"), padding=(18, 10), borderwidth=0, relief="flat")
        style.map("Danger.TButton", background=[("active", _UI_DANGER_HOVER), ("disabled", "#e7bcc6")])
        style.configure("Ghost.TButton", background=_UI_CARD_BG, foreground=_UI_ACCENT, font=(_UI_FONT, 10, "bold"), padding=(16, 9), borderwidth=1, bordercolor=_UI_BORDER, lightcolor=_UI_BORDER, darkcolor=_UI_BORDER, relief="solid")
        style.map(
            "Ghost.TButton",
            background=[("active", _UI_ACCENT_SOFT), ("disabled", _UI_CARD_BG)],
            foreground=[("disabled", "#aeb8c7")],
            bordercolor=[("active", _UI_ACCENT), ("focus", _UI_ACCENT)],
        )

        # 结果表：无外框、加高行、表头浅底、选中行强调蓝。
        style.configure("Treeview", background=_UI_CARD_BG, fieldbackground=_UI_CARD_BG, foreground=_UI_TEXT, rowheight=32, borderwidth=0, relief="flat", font=(_UI_FONT, 9))
        style.map("Treeview", background=[("selected", _UI_SEL_BG)], foreground=[("selected", _UI_HEADING)])
        style.configure("Treeview.Heading", background=_UI_TREE_HEAD_BG, foreground="#334155", font=(_UI_FONT, 9, "bold"), relief="flat", padding=(8, 9))
        style.map("Treeview.Heading", background=[("active", "#e2e8f0")])

        # 滚动条：细、无箭头描边。
        style.configure("TScrollbar", background="#c7d0de", troughcolor=_UI_APP_BG, bordercolor=_UI_APP_BG, arrowcolor=_UI_MUTED, relief="flat")
        style.map("TScrollbar", background=[("active", _UI_MUTED)])

    def _build_layout(self) -> None:
        defaults = self._startup_defaults
        root = ttk.Frame(self, padding=(14, 6))
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        # 结果表所在行占据全部剩余空间，并保证至少约5行可见。
        root.rowconfigure(4, weight=1, minsize=290)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        ttk.Label(header, text=HEADER_TITLE, style="Header.TLabel").pack(side="left")
        ttk.Label(
            header,
            text=(
                "默认实际发送 · 自动查看未读消息 · 达到目标公司数立即停止"
            ),
            style="HeaderSub.TLabel",
        ).pack(side="right", pady=(10, 0))

        config = ttk.LabelFrame(
            root,
            text=" 运行条件（多个值用逗号分隔；岗位方向与城市由模型语义判断） ",
            padding=(14, 7),
        )
        config.grid(row=1, column=0, sticky="ew", pady=(5, 4))
        # 两行都保持“字段名在左、输入控件在右”。第一行四项严格按 2/2/2/2
        # 等分；第二行用 48 列细分，给双输入框薪资与完整下拉文案保留足够宽度。
        for column in range(48):
            config.columnconfigure(column, weight=1, uniform="run-condition")

        self.excluded_var = tk.StringVar(value=defaults["不打招呼公司"])
        self.jobs_var = tk.StringVar(value=defaults["目标岗位方向"])
        self.excluded_jobs_var = tk.StringVar(value=defaults["排除岗位方向"])
        self.locations_var = tk.StringVar(value=defaults["目标城市"])
        self.salary_min_var = tk.StringVar(value=defaults["薪资下限"])
        self.salary_max_var = tk.StringVar(value=defaults["薪资上限"])
        self.company_size_var = tk.StringVar(value=defaults["公司规模"])
        self.target_var = tk.StringVar(value=defaults["目标公司数"])
        self.score_var = tk.StringVar(value=defaults["最低匹配分"])
        self.mode_var = tk.StringVar(value=DEFAULT_RUN_MODE)
        self.weekend_var = tk.StringVar(value=WEEKEND_OPTIONS[0])
        self.experience_var = tk.StringVar(value="1-3年")
        self.expectation_var = tk.StringVar(value=AVERAGE_EXPECTATION_OPTION)
        self.browser_status_var = tk.StringVar(value="等待读取求职意向")

        self._inline_field(
            config, "不打招呼公司", self.excluded_var, 0, 0, 12, width=32
        )
        self._inline_field(
            config, "目标岗位方向", self.jobs_var, 0, 12, 12, width=32
        )
        self._inline_field(
            config,
            "排除岗位方向",
            self.excluded_jobs_var,
            0,
            24,
            12,
            width=32,
        )
        self._inline_field(
            config, "目标城市", self.locations_var, 0, 36, 12, width=32
        )
        self.expectation_combo = self._inline_combo(
            config,
            "求职意向",
            self.expectation_var,
            (AVERAGE_EXPECTATION_OPTION,),
            1,
            0,
            8,
            width=16,
        )
        self._inline_salary_range(config, 1, 8, 8)
        self._inline_field_with_suffix(
            config,
            "公司规模",
            self.company_size_var,
            "人",
            1,
            16,
            5,
            width=5,
        )
        self._inline_field(
            config, "目标公司数", self.target_var, 1, 21, 5, width=4
        )
        self._inline_field(
            config, "最低匹配分", self.score_var, 1, 26, 5, width=4
        )
        self._inline_combo(
            config,
            "运行模式",
            self.mode_var,
            (MODE_FILL_ONLY, MODE_SEND),
            1,
            31,
            6,
            width=11,
        )
        self._inline_combo(
            config,
            "周末休息",
            self.weekend_var,
            WEEKEND_OPTIONS,
            1,
            37,
            5,
            width=6,
        )
        self._inline_combo(
            config,
            "经验要求",
            self.experience_var,
            EXPERIENCE_OPTIONS,
            1,
            42,
            6,
            width=8,
        )

        mysql = ttk.LabelFrame(root, text=" MySQL（点击开始后自动创建 boss_job_assistant） ", padding=(14, 7))
        mysql.grid(row=2, column=0, sticky="ew", pady=(0, 5))
        for column in range(6):
            mysql.columnconfigure(column, weight=1)
        self.mysql_host_var = tk.StringVar(value=defaults["MySQL主机"])
        self.mysql_port_var = tk.StringVar(value=defaults["MySQL端口"])
        self.mysql_user_var = tk.StringVar(value=defaults["MySQL用户名"])
        self.mysql_password_var = tk.StringVar(value=defaults["MySQL密码"])
        self.mysql_database_var = tk.StringVar(value=defaults["MySQL数据库"])
        self._inline_field(mysql, "主机", self.mysql_host_var, 0, 0)
        self._inline_field(mysql, "端口", self.mysql_port_var, 0, 1)
        self._inline_field(mysql, "用户名", self.mysql_user_var, 0, 2)
        self._inline_field(mysql, "密码", self.mysql_password_var, 0, 3, show="*")
        self._inline_field(mysql, "数据库", self.mysql_database_var, 0, 4)
        self.review_mode_var = tk.StringVar(value=DEFAULT_REVIEW_MODE)
        review_field = ttk.Frame(mysql, style="Card.TFrame")
        review_field.grid(row=0, column=5, sticky="ew", padx=6, pady=5)
        ttk.Label(review_field, text="审核方式", style="Field.TLabel").pack(side="left", padx=(0, 8))
        self.review_mode_combo = ttk.Combobox(
            review_field,
            textvariable=self.review_mode_var,
            values=(REVIEW_MODE_CODEX, REVIEW_MODE_API),
            state="readonly",
        )
        self.review_mode_combo.pack(side="left", fill="x", expand=True)
        self.review_mode_combo.bind(
            "<<ComboboxSelected>>", self._on_review_mode_selected
        )

        live = ttk.Frame(root)
        live.grid(row=3, column=0, sticky="ew", pady=(0, 5))
        live.columnconfigure(0, weight=2)
        live.columnconfigure(1, weight=3)

        status_card = ttk.LabelFrame(live, text=" 当前状态 ", padding=(12, 6))
        status_card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.status_var = tk.StringVar(value="等待开始")
        self.status_detail_var = tk.StringVar(value="请填写所有必填字段")
        ttk.Label(status_card, textvariable=self.status_var, style="Status.TLabel").pack(fill="x")
        ttk.Label(status_card, textvariable=self.status_detail_var, style="Sub.TLabel", wraplength=700).pack(anchor="w", pady=(4, 2))
        self.progress_var = tk.StringVar(value="已检查 0 · 已选择 0 · 已发送 0 · 失败 0")
        status_meta = ttk.Frame(status_card, style="Card.TFrame")
        status_meta.pack(fill="x")
        ttk.Label(status_meta, textvariable=self.progress_var).pack(side="left")
        ttk.Label(status_meta, text="浏览器：", style="Sub.TLabel").pack(
            side="left",
            padx=(14, 0),
        )
        ttk.Label(
            status_meta, textvariable=self.browser_status_var, style="Sub.TLabel"
        ).pack(
            side="left",
            fill="x",
            expand=True,
        )

        greeting_card = ttk.LabelFrame(live, text=" 生成的个性化招呼语 ", padding=(12, 6))
        greeting_card.grid(row=0, column=1, sticky="nsew")
        self.greeting_text = tk.Text(
            greeting_card,
            height=2,
            wrap="word",
            bg=_UI_CARD_BG,
            fg=_UI_TEXT,
            relief="flat",
            highlightthickness=1,
            highlightbackground=_UI_BORDER,
            highlightcolor=_UI_ACCENT,
            padx=12,
            pady=9,
            spacing1=2,
            spacing3=3,
            font=(_UI_FONT, 11),
        )
        self.greeting_text.pack(fill="both", expand=True)
        self.greeting_text.insert("1.0", "尚未生成")
        self.greeting_text.configure(state="disabled")

        table_card = ttk.LabelFrame(root, text=" 岗位处理结果 ", padding=(14, 7))
        table_card.grid(row=4, column=0, sticky="nsew")
        table_card.rowconfigure(1, weight=1, minsize=200)
        table_card.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(table_card, style="Card.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.start_button = ttk.Button(toolbar, text="开始", style="Accent.TButton", command=self._start)
        self.start_button.pack(side="right")
        self.stop_button = ttk.Button(
            toolbar,
            text="停止",
            style="Danger.TButton",
            command=self._stop,
            state="disabled",
        )
        self.stop_button.pack(side="right", padx=(0, 8))
        self.pause_button = ttk.Button(
            toolbar,
            text="暂停",
            style="Ghost.TButton",
            command=self._toggle_pause,
            state="disabled",
        )
        self.pause_button.pack(side="right", padx=(0, 8))

        columns = (
            "index",
            "time",
            "duration",
            "company",
            "job",
            "location",
            "salary",
            "activity",
            "score",
            "qualifications",
            "result",
        )
        self.tree = ttk.Treeview(table_card, columns=columns, show="headings", height=12)
        headings = {
            "index": "序号",
            "time": "时间",
            "duration": "处理耗时",
            "company": "公司",
            "job": "岗位",
            "location": "地点",
            "salary": "待遇",
            "activity": "招聘者活跃",
            "score": "匹配分",
            "qualifications": "任职资格 / 处理说明",
            "result": "投递情况",
        }
        widths = {
            "index": 50,
            "time": 78,
            "duration": 84,
            "company": 120,
            "job": 138,
            "location": 96,
            "salary": 82,
            "activity": 92,
            "score": 58,
            "qualifications": 246,
            "result": _RESULT_COLUMN_WIDTH,
        }
        # 表头与单元格文字统一水平居中。“投递情况”固定宽度且不参与拉伸：最长的
        # “已回复，简历此前已发送”实测 132px，必须一眼看全，不该靠底部滚动条左右
        # 拖动。其余列继续按剩余空间自适应，也相应收窄以腾出这段固定宽度。
        for column in columns:
            self.tree.heading(column, text=headings[column], anchor="center")
            fixed = column == "result"
            self.tree.column(
                column,
                width=widths[column],
                minwidth=_RESULT_COLUMN_WIDTH if fixed else 50,
                stretch=not fixed,
                anchor="center",
            )
        # 斑马纹 + 语义色：失败行淡红、发送成功行淡绿，其余按奇偶交替。
        self.tree.tag_configure("odd", background=_UI_CARD_BG)
        self.tree.tag_configure("even", background=_UI_ROW_ALT)
        self.tree.tag_configure("failed", background=_UI_ROW_FAIL_BG, foreground=_UI_ROW_FAIL_FG)
        self.tree.tag_configure("sent", background=_UI_ROW_OK_BG, foreground=_UI_ROW_OK_FG)
        self.tree.tag_configure("chat", background=_UI_ROW_CHAT_BG, foreground=_UI_ROW_CHAT_FG)
        y_scroll = ttk.Scrollbar(
            table_card,
            orient="vertical",
            command=self._on_result_scroll,
        )
        x_scroll = ttk.Scrollbar(table_card, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        y_scroll.grid(row=1, column=1, sticky="ns")
        x_scroll.grid(row=2, column=0, sticky="ew")

        # 列宽固定、文字居中，超出部分照旧硬截断。想看全文时点一下那个单元格，
        # 会在它正下方浮出一条带横向滚动条的查看条，左右滑动即可读完整内容。
        # Treeview 本身无法在单元格内部滚动，因此这条查看条是浮动在表格之上的
        # 独立控件，只在文字确实被截断时出现，不改变表格布局。
        self._cell_viewer = ttk.Frame(self.tree, style="Card.TFrame")
        self._cell_viewer_var = tk.StringVar()
        self._cell_viewer_entry = ttk.Entry(
            self._cell_viewer,
            textvariable=self._cell_viewer_var,
            state="readonly",
            font=(_UI_FONT, 9),
        )
        self._cell_viewer_scroll = ttk.Scrollbar(
            self._cell_viewer,
            orient="horizontal",
            command=self._cell_viewer_entry.xview,
        )
        self._cell_viewer_entry.configure(
            xscrollcommand=self._cell_viewer_scroll.set
        )
        self._cell_viewer_entry.pack(fill="x")
        self._cell_viewer_scroll.pack(fill="x")
        self._cell_viewer_cell: tuple[str, str] | None = None
        self._cell_font = tkfont.nametofont("TkDefaultFont")

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<MouseWheel>", self._on_result_user_scroll)
        self.tree.bind("<Button-4>", self._on_result_user_scroll)
        self.tree.bind("<Button-5>", self._on_result_user_scroll)
        self.tree.bind("<KeyRelease>", self._on_result_user_scroll)
        self.tree.bind("<Escape>", lambda _event: self._hide_cell_viewer())
        self.tree.bind("<Configure>", lambda _event: self._hide_cell_viewer())

    def _sync_result_follow_state(self) -> None:
        """在用户滚动动作完成后，以最终视口决定是否恢复自动跟随。"""

        self._follow_latest_results = result_view_is_at_bottom(self.tree.yview())

    def _schedule_result_follow_sync(self) -> None:
        # 鼠标滚轮和键盘的 Treeview 类绑定晚于控件绑定执行；等到空闲阶段读取，
        # 才能拿到本次用户动作完成后的最终 yview。
        self.after_idle(self._sync_result_follow_state)

    def _on_result_scroll(self, *args: str) -> None:
        """代理竖向滚动条命令，并同步用户是否已重新回到最新记录。"""

        self._hide_cell_viewer()
        self.tree.yview(*args)
        self._schedule_result_follow_sync()

    def _on_result_user_scroll(self, _event: tk.Event | None = None) -> None:
        """记录滚轮、触控板和键盘滚动后的跟随状态。"""

        self._hide_cell_viewer()
        self._schedule_result_follow_sync()

    def _field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        columnspan: int = 1,
        *,
        show: str | None = None,
        state: str = "normal",
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=column, columnspan=columnspan, sticky="w", padx=5)
        entry = ttk.Entry(parent, textvariable=variable, show=show or "", state=state)
        entry.grid(row=row + 1, column=column, columnspan=columnspan, sticky="ew", padx=5, pady=(2, 5))
        self.input_widgets = getattr(self, "input_widgets", [])
        self.input_widgets.append(entry)

    def _inline_field(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        row: int,
        column: int,
        columnspan: int = 1,
        *,
        show: str | None = None,
        state: str = "normal",
        width: int | None = None,
    ) -> None:
        field = ttk.Frame(parent, style="Card.TFrame")
        field.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="ew",
            padx=6,
            pady=5,
        )
        ttk.Label(field, text=label, style="Field.TLabel").pack(
            side="left", padx=(0, 6)
        )
        entry = ttk.Entry(
            field,
            textvariable=variable,
            show=show or "",
            state=state,
            width=width,
        )
        entry.pack(side="left", fill="x", expand=True)
        self.input_widgets = getattr(self, "input_widgets", [])
        self.input_widgets.append(entry)

    def _inline_field_with_suffix(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        suffix: str,
        row: int,
        column: int,
        columnspan: int = 1,
        *,
        width: int | None = None,
    ) -> None:
        field = ttk.Frame(parent, style="Card.TFrame")
        field.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="ew",
            padx=6,
            pady=5,
        )
        ttk.Label(field, text=label, style="Field.TLabel").pack(
            side="left", padx=(0, 6)
        )
        entry = ttk.Entry(field, textvariable=variable, width=width)
        entry.pack(side="left", fill="x", expand=True)
        ttk.Label(field, text=suffix, style="Field.TLabel").pack(
            side="left", padx=(5, 0)
        )
        self.input_widgets = getattr(self, "input_widgets", [])
        self.input_widgets.append(entry)

    def _inline_salary_range(
        self,
        parent: ttk.Frame,
        row: int,
        column: int,
        columnspan: int,
    ) -> None:
        field = ttk.Frame(parent, style="Card.TFrame")
        field.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="ew",
            padx=6,
            pady=5,
        )
        ttk.Label(field, text="薪资范围", style="Field.TLabel").pack(
            side="left", padx=(0, 6)
        )
        low = ttk.Entry(field, textvariable=self.salary_min_var, width=4)
        low.pack(side="left", fill="x", expand=True)
        ttk.Label(field, text="K -", style="Field.TLabel").pack(
            side="left", padx=4
        )
        high = ttk.Entry(field, textvariable=self.salary_max_var, width=4)
        high.pack(side="left", fill="x", expand=True)
        ttk.Label(field, text="K", style="Field.TLabel").pack(
            side="left", padx=(4, 0)
        )
        self.input_widgets = getattr(self, "input_widgets", [])
        self.input_widgets.extend((low, high))

    def _inline_combo(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        values: tuple[str, ...],
        row: int,
        column: int,
        columnspan: int = 1,
        *,
        width: int | None = None,
    ) -> ttk.Combobox:
        field = ttk.Frame(parent, style="Card.TFrame")
        field.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="ew",
            padx=6,
            pady=5,
        )
        ttk.Label(field, text=label, style="Field.TLabel").pack(
            side="left", padx=(0, 6)
        )
        combo = ttk.Combobox(
            field,
            textvariable=variable,
            values=values,
            state="readonly",
            width=width,
        )
        combo.pack(side="left", fill="x", expand=True)
        self.input_widgets = getattr(self, "input_widgets", [])
        self.input_widgets.append(combo)
        return combo

    def _start_job_intent_probe(self) -> None:
        """后台预读网页选项；失败时保留平均次数并按间隔重试。"""

        if self.running or self._close_pending or self._intent_probe_running:
            return
        self._intent_probe_running = True
        self.browser_status_var.set("正在读取网页求职意向…")
        self._intent_probe_thread = threading.Thread(
            target=self._job_intent_probe_worker,
            daemon=True,
        )
        self._intent_probe_thread.start()

    def _job_intent_probe_worker(self) -> None:
        try:
            intents = fetch_job_intents_for_gui()
        except (BrowserError, LoginRequiredError, OSError) as exc:
            self.events.put(("job_intents_error", str(exc)))
        else:
            self.events.put(("job_intents", intents))

    def _apply_job_intents(self, intents: JobIntentData) -> None:
        current = self.expectation_var.get()
        mapping = {
            format_expectation_option(expectation): expectation
            for expectation in intents.expectations
        }
        self._expectation_by_option = mapping
        values = (AVERAGE_EXPECTATION_OPTION, *mapping.keys())
        self.expectation_combo.configure(values=values)
        self.expectation_var.set(
            current if current in values else AVERAGE_EXPECTATION_OPTION
        )
        self.browser_status_var.set(f"已读取 {len(mapping)} 条求职意向")

    def _required(self, label: str, value: str) -> str:
        if not value.strip():
            messagebox.showwarning(label, "此字段不允许为空", parent=self)
            raise ValueError(label)
        return value.strip()

    def _on_review_mode_selected(self, _event: object = None) -> None:
        # 下拉切换审核方式；选大模型API时先校验本地配置，失败则回退Codex主导。
        if self.running:
            return
        if self.review_mode_var.get() == REVIEW_MODE_API:
            try:
                config = replace(
                    ApiProviderConfig.from_json(API_CONFIG_PATH),
                    enabled=True,
                )
                config.validate_for_request()
            except ReviewError as exc:
                messagebox.showwarning("大模型API配置", str(exc), parent=self)
                self.review_mode_var.set(REVIEW_MODE_CODEX)
        self.focus_set()

    def _collect_settings(self) -> dict[str, object]:
        """收集当前GUI值；这是启动后运行及暂停恢复的唯一有效设置来源。"""

        excluded = self.excluded_var.get().strip() or "无"
        jobs = self._required("允许打招呼岗位", self.jobs_var.get())
        excluded_jobs = self.excluded_jobs_var.get().strip() or "无"
        locations = self._required("允许工作地点", self.locations_var.get())
        salary_min_text = self.salary_min_var.get().strip()
        salary_max_text = self.salary_max_var.get().strip()
        company_size_text = self.company_size_var.get().strip()
        target_text = self._required("目标公司数量", self.target_var.get())
        score_text = self._required("最低匹配分", self.score_var.get())
        host = self._required("MySQL主机", self.mysql_host_var.get())
        port_text = self._required("MySQL端口", self.mysql_port_var.get())
        user = self._required("MySQL用户名", self.mysql_user_var.get())
        password = self._required("MySQL密码", self.mysql_password_var.get())
        database = self._required("MySQL数据库", self.mysql_database_var.get())
        try:
            target = int(target_text)
            score = int(score_text)
            port = int(port_text)
            salary_min = int(salary_min_text) if salary_min_text else None
            salary_max = int(salary_max_text) if salary_max_text else None
            company_size = int(company_size_text) if company_size_text else None
        except ValueError as exc:
            messagebox.showwarning(
                "字段格式",
                "数量、分数、端口、薪资范围和公司规模必须是整数",
                parent=self,
            )
            raise ValueError("字段格式") from exc
        if (salary_min is None) != (salary_max is None):
            raise ValueError("薪资范围的下限和上限必须同时填写，或同时留空")
        if salary_min is not None and (salary_min < 0 or salary_max < salary_min):
            raise ValueError("薪资范围必须满足 0 ≤ 下限 ≤ 上限")
        if company_size is not None and company_size < 0:
            raise ValueError("公司规模不能小于0人")
        if not 1 <= target <= 150:
            raise ValueError("目标公司数量必须在1-150之间")
        if not 0 <= score <= 100:
            raise ValueError("最低匹配分必须在0-100之间")
        mode = self.mode_var.get()
        selected_expectation = self._expectation_by_option.get(
            self.expectation_var.get()
        )
        return {
            "policy": AutomationPolicy(
                excluded_companies=parse_terms(excluded),
                allowed_job_keywords=parse_terms(jobs),
                allowed_locations=parse_terms(locations),
                target_companies=target,
                excluded_job_directions=parse_terms(excluded_jobs),
                minimum_score=score,
                weekend_rest=self.weekend_var.get(),
                experience_requirement=self.experience_var.get(),
                selected_expectation=selected_expectation,
                salary_min_k=salary_min,
                salary_max_k=salary_max,
                minimum_company_size=company_size,
            ),
            "mysql": MySqlConfig(host, port, user, password, database),
            "mode": mode,
            "review_mode": self.review_mode_var.get(),
        }

    def _start(self) -> None:
        if self.running:
            return
        try:
            settings = self._collect_settings()
        except ValueError as exc:
            if str(exc) and str(exc) not in {
                "不打招呼公司",
                "允许打招呼岗位",
                "允许工作地点",
                "目标公司数量",
                "最低匹配分",
                "MySQL主机",
                "MySQL端口",
                "MySQL用户名",
                "MySQL密码",
                "MySQL数据库",
                "字段格式",
            }:
                messagebox.showwarning("配置错误", str(exc), parent=self)
            return
        self.running = True
        self.control = AutomationControl()
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="normal", text="暂停")
        self.stop_button.configure(state="normal")
        self._set_inputs_enabled(False)
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._row_records.clear()
        self._follow_latest_results = True
        self._hide_cell_viewer()
        self.status_var.set("初始化")
        self.status_detail_var.set("正在验证简历、浏览器和MySQL")
        # 非daemon线程配合窗口关闭拦截，确保浏览器关闭的finally有机会执行。
        self._worker_thread = threading.Thread(
            target=self._worker,
            args=(settings,),
            daemon=False,
        )
        self._worker_thread.start()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        """运行中锁定全部设置控件；暂停时解锁，允许改完再继续。"""

        state = "normal" if enabled else "disabled"
        for widget in getattr(self, "input_widgets", []):
            if enabled and self.running and widget is self.expectation_combo:
                # 求职意向决定了本轮配额数组和断点索引，运行开始后（包括暂停）
                # 保持锁定；其它筛选条件仍可按原逻辑在暂停时修改。
                widget.configure(state="disabled")
                continue
            # 下拉框解锁后仍应是只读，避免手输非法值。
            if isinstance(widget, ttk.Combobox):
                widget.configure(state="readonly" if enabled else "disabled")
            else:
                widget.configure(state=state)
        self.review_mode_combo.configure(state="readonly" if enabled else "disabled")

    def _toggle_pause(self) -> None:
        if not self.running or self.control is None:
            return
        if self.control.paused:
            try:
                # 暂停期间只采集用户此刻在GUI里修改后的值，不重新读取启动TXT。
                updated = self._collect_settings()
            except ValueError:
                # 校验失败时保持暂停状态，让用户把设置改对再继续。
                self.status_var.set("已暂停")
                self.status_detail_var.set("设置有误，请修正后再点继续")
                return
            self.control.set_pending_settings(updated)
            if self.control.resume():
                self._set_inputs_enabled(False)
                self.pause_button.configure(text="暂停")
                self.status_var.set("继续请求已提交")
                self.status_detail_var.set("将按当前界面设置从断点继续投递")
        elif self.control.pause():
            self._set_inputs_enabled(True)
            self.pause_button.configure(text="继续")
            self.status_var.set("正在暂停")
            self.status_detail_var.set(
                "将在下一个安全控制点保存断点；除本轮求职意向外可修改设置，点“继续”后生效"
            )

    def _stop(self) -> None:
        if not self.running or self.control is None:
            return
        self.control.stop()
        self.pause_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止")
        self.status_detail_var.set("将在下一个安全控制点结束并保存当前进度")

    def _on_close(self) -> None:
        if not self.running:
            self.destroy()
            return
        if self._close_pending:
            return
        self._close_pending = True
        if self.control is not None:
            self.control.stop()
        self.start_button.configure(state="disabled")
        self.pause_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在安全退出")
        self.status_detail_var.set("正在停止任务并关闭浏览器，请稍候")

    def _worker(self, settings: dict[str, object]) -> None:
        mysql_store: AutomationMySqlStore | None = None
        run_id: int | None = None
        browser: EdgeBrowser | None = None
        stats = AutomationStats()
        control = self.control
        assert control is not None
        try:
            policy = settings["policy"]
            mysql_config = settings["mysql"]
            assert isinstance(policy, AutomationPolicy)
            assert isinstance(mysql_config, MySqlConfig)
            mysql_store = AutomationMySqlStore(mysql_config)
            mysql_store.initialize()
            recent_sent_companies = mysql_store.recent_sent_companies()
            recent_successful_applications = (
                mysql_store.recent_successful_applications()
            )
            control.wait_if_paused()
            mode = str(settings["mode"])
            review_mode = str(settings["review_mode"])
            public_settings = {
                "excluded_companies": list(policy.excluded_companies),
                "allowed_job_keywords": list(policy.allowed_job_keywords),
                "excluded_job_directions": list(policy.excluded_job_directions),
                "allowed_locations": list(policy.allowed_locations),
                "minimum_score": policy.minimum_score,
                "salary_min_k": policy.salary_min_k,
                "salary_max_k": policy.salary_max_k,
                "minimum_company_size": policy.minimum_company_size,
                "selected_expectation": (
                    asdict(policy.selected_expectation)
                    if policy.selected_expectation is not None
                    else AVERAGE_EXPECTATION_OPTION
                ),
                "action_delay_range_seconds": [
                    AutomationConfig.action_delay_min_seconds,
                    AutomationConfig.action_delay_max_seconds,
                ],
                "review_mode": review_mode,
            }
            run_id = mysql_store.start_run(mode, policy.target_companies, public_settings)

            resume_result = process_inbox_resume("resume_inbox", "data/resume")
            control.wait_if_paused()
            # 启动按钮可能恰好与 GUI 预读重叠；正式 runner 接管 Edge 前先等独立
            # 预读会话自然结束，避免两个 CDP 会话同时导航同一标签页。
            intent_probe = self._intent_probe_thread
            if intent_probe is not None and intent_probe.is_alive():
                intent_probe.join()
            control.wait_if_paused()
            browser = EdgeBrowser(
                attach_existing_only=True,
                require_boss_page=True,
            )
            self.events.put(
                ("browser", "Edge · 当前已打开的 Boss 页面")
            )
            control.wait_if_paused()
            store = JobStore("data/jobs.sqlite3")

            fill_only = mode == MODE_FILL_ONLY

            def build_review_provider(chosen: str) -> object:
                if chosen == REVIEW_MODE_API:
                    api_config = replace(
                        ApiProviderConfig.from_json(API_CONFIG_PATH),
                        enabled=True,
                    )
                    api_config.validate_for_request()
                    provider = OpenAICompatibleReviewProvider(
                        api_config,
                        control_point=control.wait_if_paused,
                        audit_directory="data/model_api_reviews",
                    )
                    self.events.put(
                        (
                            "log",
                            f"审核方式：大模型API（模型：{api_config.model}，本次运行临时启用）",
                        )
                    )
                    return provider
                provider = CodexCliReviewProvider(
                    "data/manual_reviews",
                    workspace=".",
                    output=lambda text: self.events.put(("log", text)),
                    control_point=control.wait_if_paused,
                )
                self.events.put(("log", "审核方式：本机Codex CLI自动审核"))
                return provider

            review_provider = build_review_provider(review_mode)

            def on_result(result: dict[str, object]) -> None:
                assert mysql_store is not None and run_id is not None
                # 消息巡检产生的回复/置顶不是投递记录，只展示不写投递统计表。
                if result.get("record_type") != "chat_action":
                    mysql_store.save_result(run_id, result)
                self.events.put(("result", result))

            def apply_paused_settings(updated: dict[str, object]) -> None:
                """把暂停期间改过的界面设置应用到正在运行的任务上。

                逐项对比再改：审核方式与 MySQL 连接的重建代价高，只在真的变了
                才做。任何一项失败都会抛出，由 runner 捕获后沿用原设置继续跑。
                """

                nonlocal mysql_store, run_id, mode, review_mode, mysql_config, policy

                new_policy = updated["policy"]
                assert isinstance(new_policy, AutomationPolicy)
                new_mysql = updated["mysql"]
                assert isinstance(new_mysql, MySqlConfig)
                new_mode = str(updated["mode"])
                new_review_mode = str(updated["review_mode"])
                changed: list[str] = []

                if new_policy != policy:
                    runner.policy = new_policy
                    policy = new_policy
                    changed.append("筛选条件")
                if new_mode != mode:
                    runner.config = replace(
                        runner.config,
                        fill_only=new_mode == MODE_FILL_ONLY,
                        max_jobs=max(20, new_policy.target_companies * 10),
                    )
                    mode = new_mode
                    changed.append(f"运行模式→{new_mode}")
                elif new_policy.target_companies != runner.config.max_jobs // 10:
                    runner.config = replace(
                        runner.config,
                        max_jobs=max(20, new_policy.target_companies * 10),
                    )
                if new_review_mode != review_mode:
                    runner.review_provider = build_review_provider(new_review_mode)
                    review_mode = new_review_mode
                    changed.append(f"审核方式→{new_review_mode}")
                if new_mysql != mysql_config:
                    # 换库意味着换一条运行记录：先把旧库这条运行收尾，再在新库开一条。
                    new_store = AutomationMySqlStore(new_mysql)
                    new_store.initialize()
                    new_run_id = new_store.start_run(
                        mode, new_policy.target_companies, public_settings
                    )
                    if mysql_store is not None and run_id is not None:
                        try:
                            mysql_store.finish_run(run_id, stats, status="completed")
                        except MySqlStoreError:
                            pass
                    mysql_store = new_store
                    run_id = new_run_id
                    mysql_config = new_mysql
                    changed.append(f"数据库→{new_mysql.database}")

                if changed:
                    self.events.put(
                        ("log", "已应用暂停期间的设置改动：" + "；".join(changed))
                    )
                else:
                    self.events.put(("log", "暂停期间设置未变化，按原设置继续"))

            runner = BossAutomationRunner(
                browser,
                store,
                resume_result.process_result.resume,
                artifact_directory="data/job_artifacts",
                run_directory="data/automation_runs",
                config=AutomationConfig(
                    max_jobs=max(20, policy.target_companies * 10),
                    dry_run=False,
                    fill_only=fill_only,
                ),
                policy=policy,
                settings_applier=apply_paused_settings,
                output=lambda text: self.events.put(("log", text)),
                status_callback=lambda step, detail: self.events.put(
                    ("status", (step, detail))
                ),
                result_callback=on_result,
                greeting_callback=lambda text: self.events.put(("greeting", text)),
                review_provider=review_provider,
                resume_text=resume_result.process_result.document.text,
                control=control,
                stats=stats,
                recent_sent_companies=recent_sent_companies,
                recent_successful_applications=recent_successful_applications,
                login_required_callback=lambda message: self.events.put(
                    ("login_required", message)
                ),
                login_wait_seconds=0.0,
                require_logged_in_before_start=True,
            )
            stats, log_path = runner.run()
            mysql_store.finish_run(run_id, stats, status="completed")
            self.events.put(
                (
                    "done",
                    (
                        stats,
                        log_path,
                        runner.completion_reason,
                        runner.completion_warning,
                    ),
                )
            )
        except LoginRequiredError:
            if mysql_store is not None and run_id is not None:
                try:
                    mysql_store.finish_run(run_id, stats, status="login_required")
                except MySqlStoreError:
                    pass
            self.events.put(("login_blocked", None))
        except AutomationStoppedError as exc:
            stats = exc.stats
            if mysql_store is not None and run_id is not None:
                try:
                    mysql_store.finish_run(run_id, stats, status="stopped")
                except MySqlStoreError:
                    pass
            self.events.put(
                ("stopped", (stats, exc.log_path, exc.cleanup_warning))
            )
        except AutomationStopRequested:
            if mysql_store is not None and run_id is not None:
                try:
                    mysql_store.finish_run(run_id, stats, status="stopped")
                except MySqlStoreError:
                    pass
            self.events.put(("stopped", (stats, None)))
        except (
            MySqlStoreError,
            ResumeInboxError,
            ResumePdfError,
            BrowserError,
            JobStoreError,
            BossAutomationError,
            ReviewError,
            OSError,
        ) as exc:
            if mysql_store is not None and run_id is not None:
                try:
                    mysql_store.finish_run(run_id, stats, status="failed")
                except MySqlStoreError:
                    pass
            self.events.put(("error", str(exc)))
        finally:
            # 运行结束/停止/失败后关闭本次 WebDriver 会话；账号登录态由 Edge
            # Default Profile 自身持久化。
            if browser is not None:
                browser.quit()

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "status":
                    step, detail = payload  # type: ignore[misc]
                    self.status_var.set(str(step))
                    self.status_detail_var.set(str(detail))
                elif event == "log":
                    self.status_detail_var.set(str(payload))
                elif event == "greeting":
                    self.greeting_text.configure(state="normal")
                    self.greeting_text.delete("1.0", "end")
                    self.greeting_text.insert("1.0", str(payload))
                    self.greeting_text.configure(state="disabled")
                elif event == "result":
                    self._add_result(payload)  # type: ignore[arg-type]
                elif event == "browser":
                    self.browser_status_var.set(str(payload))
                elif event == "job_intents":
                    self._intent_probe_running = False
                    self._apply_job_intents(payload)  # type: ignore[arg-type]
                elif event == "job_intents_error":
                    self._intent_probe_running = False
                    self.browser_status_var.set(
                        "求职意向待读取：请保持登录专用 Edge 已打开并登录"
                    )
                    if not self.running and not self._close_pending:
                        self.after(_EXPECTATION_RETRY_MS, self._start_job_intent_probe)
                elif event == "login_required":
                    self.status_var.set("等待登录")
                    self.status_detail_var.set(str(payload).replace("\n", " "))
                    if not self._close_pending:
                        messagebox.showinfo(
                            "请登录 Boss直聘 Web",
                            str(payload),
                            parent=self,
                        )
                elif event == "login_blocked":
                    self._finish_ui()
                    self.status_var.set("等待登录")
                    self.status_detail_var.set(
                        "请在当前 Edge 打开 Boss 页面并登录后重新启动脚本"
                    )
                elif event == "done":
                    if len(payload) == 4:  # type: ignore[arg-type]
                        (
                            stats,
                            log_path,
                            completion_reason,
                            completion_warning,
                        ) = payload  # type: ignore[misc]
                    elif len(payload) == 3:  # type: ignore[arg-type]
                        stats, log_path, completion_reason = payload  # type: ignore[misc]
                        completion_warning = None
                    else:
                        stats, log_path = payload  # type: ignore[misc]
                        completion_reason = None
                        completion_warning = None
                    self._finish_ui()
                    status, detail = format_run_completion(
                        completion_reason,
                        log_path,
                        completion_warning,
                    )
                    self.status_var.set(status)
                    self.status_detail_var.set(detail)
                    self._set_progress(stats)
                elif event == "stopped":
                    if len(payload) == 3:  # type: ignore[arg-type]
                        stats, log_path, cleanup_warning = payload  # type: ignore[misc]
                    else:
                        stats, log_path = payload  # type: ignore[misc]
                        cleanup_warning = None
                    self._finish_ui()
                    self.status_var.set("已停止")
                    if cleanup_warning:
                        self.status_detail_var.set(str(cleanup_warning))
                    else:
                        self.status_detail_var.set(
                            f"已保存当前进度；运行记录：{log_path}"
                            if log_path
                            else "已在初始化阶段停止"
                        )
                    self._set_progress(stats)
                elif event == "error":
                    self._finish_ui()
                    self.status_var.set("运行失败")
                    self.status_detail_var.set(str(payload))
                    if not self._close_pending:
                        messagebox.showerror("运行失败", str(payload), parent=self)
        except queue.Empty:
            pass
        if self._close_pending and not self.running:
            self.destroy()
            return
        self.after(100, self._drain_events)

    def _row_counts(self) -> tuple[int, int]:
        """分别统计岗位行与消息行；两者共用一张表但序号各自独立编号。"""

        job_rows = 0
        chat_rows = 0
        for row in self.tree.get_children():
            if "chat_action" in self.tree.item(row, "tags"):
                chat_rows += 1
            else:
                job_rows += 1
        return job_rows, chat_rows

    def _add_result(self, result: dict[str, object]) -> None:
        created = str(result.get("created_at") or "")
        time_text = created[11:19] if len(created) >= 19 else created
        job_rows, chat_rows = self._row_counts()
        if result.get("record_type") == "chat_action":
            # 消息行用 M1、M2… 独立编号，避免占用岗位序号，让最大岗位序号始终
            # 等于状态区的“已检查”。
            self._add_chat_action(result, f"M{chat_rows + 1}", time_text)
            return
        seq = job_rows + 1
        status = result.get("delivery_status") or "—"
        if status in {"处理失败", "发送失败"}:
            row_tag = "failed"
        elif status == "发送成功":
            row_tag = "sent"
        else:
            row_tag = "even" if seq % 2 == 0 else "odd"
        item = self.tree.insert(
            "",
            "end",
            values=(
                seq,
                time_text,
                result.get("processing_duration") or "—",
                result.get("company_name") or "—",
                result.get("job_name") or "—",
                result.get("location") or "—",
                result.get("salary") or "—",
                result.get("recruiter_activity") or "—",
                result.get("score") or 0,
                result.get("qualifications_summary") or "—",
                status,
            ),
            tags=(row_tag, "job"),
        )
        self._row_records[item] = result
        if self._follow_latest_results:
            self.tree.see(item)
        self._refresh_progress_from_table()

    def _add_chat_action(
        self,
        result: dict[str, object],
        seq: str,
        time_text: str,
    ) -> None:
        """消息巡检结果复用同一张表，用独立底色与“消息”标记区分投递记录。"""

        action = str(result.get("action") or "—")
        row_tag = "failed" if action == "处理失败" else "chat"
        recruiter = result.get("recruiter_name") or "—"
        position = result.get("position_name") or "—"
        reply = result.get("reply")
        detail = str(result.get("reason") or "—")
        if reply:
            detail = f"回复“{reply}”；{detail}"
        item = self.tree.insert(
            "",
            "end",
            values=(
                seq,
                time_text,
                result.get("processing_duration") or "—",
                result.get("company_name") or "—",
                f"[消息] {recruiter} · {position}",
                "—",
                "—",
                f"未读 {result.get('unread_count') or 0}",
                "—",
                detail,
                action,
            ),
            tags=(row_tag, "chat_action"),
        )
        self._row_records[item] = result
        if self._follow_latest_results:
            self.tree.see(item)
        self._refresh_progress_from_table()

    # ------------------------------------------------------------------
    # 单元格查看条：表格保持定宽硬截断，点击被截断的单元格时在它正下方浮出
    # 一条带横向滚动条的只读输入框，左右滑动即可读完整内容。
    # ------------------------------------------------------------------

    def _column_name(self, column_id: str) -> str | None:
        """把 identify_column 返回的 '#3' 转成列名。"""

        if not column_id.startswith("#"):
            return None
        try:
            index = int(column_id[1:]) - 1
        except ValueError:
            return None
        columns = self.tree["columns"]
        if 0 <= index < len(columns):
            return str(columns[index])
        return None

    def _cell_is_truncated(self, column: str, text: str) -> bool:
        """按当前字体实测文字宽度，只有真的放不下才弹查看条。"""

        if not text:
            return False
        available = int(self.tree.column(column, "width")) - _CELL_TEXT_PADDING
        return self._cell_font.measure(text) > available

    def _hide_cell_viewer(self) -> None:
        self._cell_viewer.place_forget()
        self._cell_viewer_cell = None

    def _on_tree_click(self, event: tk.Event) -> None:
        """把点击位置解析成单元格；点表头或空白处一律收起查看条。"""

        if self.tree.identify_region(event.x, event.y) != "cell":
            self._hide_cell_viewer()
            return
        row = self.tree.identify_row(event.y)
        column_id = self.tree.identify_column(event.x)
        if not row or self._column_name(column_id) is None:
            self._hide_cell_viewer()
            return
        self._toggle_cell_viewer(row, column_id)

    def _toggle_cell_viewer(self, row: str, column_id: str) -> None:
        """在指定单元格正下方显示查看条；再次点同一个单元格则收起。

        只有文字确实被截断时才显示，避免点短字段也弹一条出来。
        """

        column = self._column_name(column_id)
        if column is None:
            self._hide_cell_viewer()
            return
        if self._cell_viewer_cell == (row, column):
            self._hide_cell_viewer()
            return
        text = str(self.tree.set(row, column))
        if not self._cell_is_truncated(column, text):
            self._hide_cell_viewer()
            return
        bbox = self.tree.bbox(row, column_id)
        if not bbox:
            self._hide_cell_viewer()
            return
        cell_x, cell_y, cell_width, cell_height = bbox
        self._cell_viewer_var.set(text)
        self._cell_viewer_entry.xview_moveto(0)
        # 窄列（序号、待遇等）单独看太挤，给查看条一个可读的最小宽度，
        # 同时不越出表格右边界。
        width = max(cell_width, _CELL_VIEWER_MIN_WIDTH)
        tree_width = self.tree.winfo_width()
        left = min(cell_x, max(0, tree_width - width))
        self._cell_viewer.place(x=left, y=cell_y + cell_height, width=width)
        self._cell_viewer.lift()
        self._cell_viewer_cell = (row, column)

    def _refresh_progress_from_table(self) -> None:
        job_rows = [
            row
            for row in self.tree.get_children()
            if "chat_action" not in self.tree.item(row, "tags")
        ]
        chat_rows = [
            row
            for row in self.tree.get_children()
            if "chat_action" in self.tree.item(row, "tags")
        ]
        selected = sum(
            1
            for row in job_rows
            if self.tree.set(row, "result") in {"已填充未发送", "发送成功"}
        )
        failed = sum(
            1
            for row in self.tree.get_children()
            if self.tree.set(row, "result") in {"处理失败", "发送失败"}
        )
        resumes = sum(
            1
            for row in chat_rows
            if self.tree.set(row, "result") in {"已回复并发送简历", "已发送简历"}
        )
        pinned = sum(
            1 for row in chat_rows if self.tree.set(row, "result") == "已置顶待处理"
        )
        text = f"已检查 {len(job_rows)} · 已选择 {selected} · 失败 {failed}"
        if resumes or pinned:
            text += f" · 已发简历 {resumes} · 已置顶 {pinned}"
        self.progress_var.set(text)

    def _set_progress(self, stats: AutomationStats) -> None:
        text = (
            f"已检查 {stats.inspected} · 已选择 {stats.matched} · "
            f"已发送 {stats.sent} · 失败 {stats.failed}"
        )
        if (
            stats.resumes_sent
            or stats.conversations_pinned
            or stats.conversations_already_pinned
        ):
            text += (
                f" · 已发简历 {stats.resumes_sent} · "
                f"已置顶 {stats.conversations_pinned}"
            )
            if stats.conversations_already_pinned:
                text += f" · 已置顶跳过 {stats.conversations_already_pinned}"
        self.progress_var.set(text)

    def _finish_ui(self) -> None:
        self.running = False
        self.control = None
        self.start_button.configure(state="normal")
        self.pause_button.configure(state="disabled", text="暂停")
        self.stop_button.configure(state="disabled")
        self._set_inputs_enabled(True)
        self.after(200, self._start_job_intent_probe)


def main() -> None:
    app = BossControlPanel()
    app.mainloop()
