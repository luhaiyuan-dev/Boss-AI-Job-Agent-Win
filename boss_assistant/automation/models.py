"""Boss 推荐岗位自动化的稳定数据模型（Web 端）。

与 Android 端相比只去掉了像素 ``Bounds``：Web 端靠 DOM 指纹与 job_id 重新定位元素，
不再需要屏幕坐标。其余字段与语义保持一致，使 ``review`` / ``api_provider`` /
``policy`` 等复用模块无需改动。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class JobExpectation:
    city: str | None
    role: str
    salary: str | None
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class JobIntentData:
    expectations: tuple[JobExpectation, ...]
    work_status: str | None = None


@dataclass(frozen=True)
class JobCard:
    job_name: str
    company_name: str | None
    salary: str | None
    location: str | None
    recruiter_activity: str | None
    tags: tuple[str, ...]
    fingerprint: str
    # 卡片“要求信息”里读到的经验/学历（如“1-3年”“本科”），供初筛与设置比对。
    experience: str | None = None
    degree: str | None = None
    # Boss Web 卡片通常带有稳定的 job id（data-jobid / 链接中的 securityId），
    # 供返回列表后重新定位同一张卡片；读不到时回退到指纹匹配。
    job_id: str | None = None
    # 列表在大模型审核期间可能自动刷新；保留卡片原始详情链接后，即使旧 DOM
    # 元素已经消失，仍可直接打开审核的是同一岗位。
    detail_url: str | None = None
    # Boss 卡片可见 DOM 不展示公司规模；卡片 Vue 数据的 brandScaleName 返回
    # “0-20人 / 20-99人 / ... / 10000人以上”，由 selectors 同卡片读取。
    company_scale: str | None = None


@dataclass(frozen=True)
class ChatConversation:
    """“消息”列表中的一条会话；未读数取自会话行上的红色角标。"""

    recruiter_name: str
    company_name: str | None
    position_name: str | None
    last_message: str
    unread_count: int
    last_message_from_me: bool
    fingerprint: str
    # 是否位于列表顶部的置顶分组。
    pinned: bool = False


@dataclass(frozen=True)
class ChatMessage:
    """聊天页中的一条消息气泡。"""

    text: str
    from_me: bool
    top: int  # DOM 顺序，用于稳定排序。


@dataclass(frozen=True)
class ChatJobInfo:
    """当前聊天头部展示的岗位条件，供HR主动会话做安全门禁。"""

    job_name: str | None
    salary: str | None
    location: str | None


@dataclass(frozen=True)
class MatchDecision:
    should_apply: bool
    score: int
    matched_expectation: JobExpectation | None
    matched_skills: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class AutomationConfig:
    max_jobs: int = 20
    dry_run: bool = True
    stagnant_scroll_limit: int = 2
    page_wait_seconds: float = 12.0
    fill_only: bool = False
    action_delay_min_seconds: float = 1.0
    action_delay_max_seconds: float = 2.0
    # 启动任务时先检查一次；每实际完成一家公司的招呼语发送后再检查；连续扫描
    # 岗位期间也按 force_message_check_every_n_jobs 定期检查。只读顶部“消息”
    # 旁的红色数字，有数字才进入消息页。单轮上限避免持续无限停留。
    inspect_unread_messages: bool = True
    max_unread_conversations: int = 10
    # 即使本阶段没有成功投递，也至少每 N 个已扫描岗位核实一次顶部红色数字。
    # “强制”仅指触发核实；没有红色数字时仍不会进入消息页。
    force_message_check_every_n_jobs: int = 10
    # 发附件简历前是否先自己回一句。Boss 确认发送后会自动代发“您好，可以看看
    # 我的简历”，关掉这项可避免连发两句意思相同的话。
    reply_before_resume: bool = True

    def __post_init__(self) -> None:
        if not 1.0 <= self.action_delay_min_seconds <= 2.0:
            raise ValueError("操作延迟下限必须在1-2秒之间")
        if not 1.0 <= self.action_delay_max_seconds <= 2.0:
            raise ValueError("操作延迟上限必须在1-2秒之间")
        if self.action_delay_max_seconds < self.action_delay_min_seconds:
            raise ValueError("操作延迟上限不能小于下限")
        if self.max_unread_conversations < 1:
            raise ValueError("单轮处理的未读会话数必须至少为1")
        if self.force_message_check_every_n_jobs < 1:
            raise ValueError("强制消息核实间隔必须至少为1个岗位")


@dataclass(frozen=True)
class AutomationPolicy:
    excluded_companies: tuple[str, ...]
    allowed_job_keywords: tuple[str, ...]
    allowed_locations: tuple[str, ...]
    target_companies: int
    excluded_job_directions: tuple[str, ...] = ()
    minimum_score: int = 50
    # 周末休息设置：不限/双休/大小周/单休（默认不限）。岗位提供的休息 >= 设置才符合，
    # 且只在详情阶段判断（详情读不到周末休息即视为符合）。
    weekend_rest: str = "不限"
    # 经验要求设置：经验不限/1-3年/3-5年/5-10年（默认1-3年）。岗位要求 <= 设置才符合。
    experience_requirement: str = "1-3年"
    # None 表示按页面顺序平均分配目标公司数；指定值时整次运行只处理这一条
    # Boss Web 求职意向，返回职位列表后也始终重新点击同一条意向。
    selected_expectation: JobExpectation | None = None
    # 薪资以 K/月为单位；设置后岗位薪资的完整上下限都必须落在该闭区间内。
    salary_min_k: int | None = None
    salary_max_k: int | None = None
    # 公司规模档位的下限必须大于等于该值；例如设置20时，0-20人不合格，
    # 20-99人及更大档位合格。None 表示不筛公司规模。
    minimum_company_size: int | None = None


@dataclass
class AutomationStats:
    inspected: int = 0
    matched: int = 0
    skipped: int = 0
    sent: int = 0
    failed: int = 0
    scrolls: int = 0
    resumes_sent: int = 0
    conversations_pinned: int = 0
    # 已经处于置顶、本轮直接跳过的未读会话数（上一轮已交由用户处理）。
    conversations_already_pinned: int = 0
    decisions: list[dict[str, object]] = field(default_factory=list)
    chat_actions: list[dict[str, object]] = field(default_factory=list)
