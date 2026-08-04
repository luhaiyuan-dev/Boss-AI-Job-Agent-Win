"""Boss 推荐岗位受控导航、匹配与执行能力（Web 端）。"""

from .matching import generate_ascii_test_greeting, generate_greeting, match_job
from .control import AutomationControl, AutomationStopRequested
# 不急切导入 runner：runner 依赖 boss_assistant.web，而 web 又需要
# automation.models。下面通过 __getattr__ 延迟暴露运行器符号，兼顾无循环导入和
# 旧调用方的 ``from boss_assistant.automation import BossAutomationRunner``。
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
from .policy import (
    card_rejection_reason,
    card_review_rejection_reason,
    contains_any,
    is_eligible_recruiter_activity,
    parse_terms,
    summarize_qualifications,
)
from .mysql_store import AutomationMySqlStore, MySqlConfig, MySqlStoreError
from .review import (
    DEFAULT_RESUME_REPLY,
    CardReviewResult,
    ChatReviewResult,
    CodexCliReviewProvider,
    DetailReviewResult,
    GreetingGroundingError,
    JobReviewProvider,
    ManualFileReviewProvider,
    ReviewError,
    merge_combined_directions,
    recruiter_requested_resume_card,
    resume_already_sent,
    validate_chat_reply,
)

__all__ = [
    "AutomationConfig",
    "AutomationControl",
    "AutomationPolicy",
    "AutomationMySqlStore",
    "AutomationStats",
    "AutomationStopRequested",
    "AutomationStoppedError",
    "BossAutomationError",
    "BossAutomationRunner",
    "ChatConversation",
    "ChatJobInfo",
    "ChatMessage",
    "ChatReviewResult",
    "DEFAULT_RESUME_REPLY",
    "merge_combined_directions",
    "recruiter_requested_resume_card",
    "resume_already_sent",
    "validate_chat_reply",
    "has_job_capacity",
    "load_recent_sent_companies",
    "load_recent_successful_applications",
    "find_recent_company_application",
    "find_recent_successful_application",
    "JobCard",
    "JobExpectation",
    "JobIntentData",
    "MatchDecision",
    "MySqlConfig",
    "MySqlStoreError",
    "CardReviewResult",
    "CodexCliReviewProvider",
    "DetailReviewResult",
    "GreetingGroundingError",
    "JobReviewProvider",
    "ManualFileReviewProvider",
    "ReviewError",
    "card_rejection_reason",
    "card_review_rejection_reason",
    "contains_any",
    "is_eligible_recruiter_activity",
    "parse_terms",
    "summarize_qualifications",
    "generate_ascii_test_greeting",
    "generate_greeting",
    "match_job",
]


_RUNNER_EXPORTS = {
    "AutomationStoppedError",
    "BossAutomationError",
    "BossAutomationRunner",
    "find_recent_company_application",
    "find_recent_successful_application",
    "has_job_capacity",
    "load_recent_sent_companies",
    "load_recent_successful_applications",
}


def __getattr__(name: str):
    if name in _RUNNER_EXPORTS:
        from . import runner

        return getattr(runner, name)
    raise AttributeError(name)
