"""GUI 输入规则、卡片安全过滤和资格概括。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .models import AutomationPolicy, JobCard
from .requirements import (
    company_size_meets,
    degree_meets,
    experience_meets,
    salary_meets,
)


def parse_terms(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip()
            for item in re.split(r"[\r\n,，、;；]+", value)
            if item.strip() and item.strip() != "无"
        )
    )


def _normalized(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold().replace(" ", "")


def _direction_key(value: str | None) -> str:
    """用于卡片方向硬校验：保留中英文数字与常见技术符号，去掉装饰字符。"""

    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff+#]+", "", normalized)


def contains_any(value: str | None, terms: tuple[str, ...]) -> bool:
    normalized = _normalized(value)
    return any(_normalized(term) in normalized for term in terms)


def is_eligible_recruiter_activity(value: str | None) -> bool:
    """识别常见近期活跃文案；该信息只展示，不再作为卡片硬过滤条件。"""

    text = (value or "").strip()
    return text in {"昨日活跃", "今日活跃"} or bool(
        re.fullmatch(r"今日回复\d+\+?次", text)
        or re.fullmatch(r"\d+(?:分钟|小时)前回复", text)
    )


def card_rejection_reason(
    card: JobCard,
    policy: AutomationPolicy,
    resume_degree_level: int = 6,
) -> str | None:
    """公司排除名单，以及卡片薪资/规模/经验/学历硬门槛。

    经验只要小于等于设置、学历只要小于等于简历学历即符合，不要求一模一样；卡片读
    不到经验/学历时按无要求处理、不拦截。方向与城市仍进入模型语义初筛和返回后
    的本地硬门禁，不在这里直接判断。
    """

    if contains_any(card.company_name, policy.excluded_companies):
        return "公司命中不打招呼名单"
    if not salary_meets(card.salary, policy.salary_min_k, policy.salary_max_k):
        configured = f"{policy.salary_min_k}-{policy.salary_max_k}K"
        return f"薪资范围不符：岗位“{card.salary or '未识别'}”不在“{configured}”内"
    if not company_size_meets(card.company_scale, policy.minimum_company_size):
        return (
            f"公司规模不符：岗位“{card.company_scale or '未识别'}”，"
            f"设置至少“{policy.minimum_company_size}人”"
        )
    if not experience_meets(card.experience, policy.experience_requirement):
        return (
            f"经验要求高于设置：岗位“{card.experience}”超过“{policy.experience_requirement}”"
        )
    if not degree_meets(card.degree, resume_degree_level):
        return f"学历要求高于简历：岗位要求“{card.degree}”"
    if policy.allowed_locations and not (card.location or "").strip():
        return "工作地点未识别，不能确认符合目标城市"
    return None


def card_review_rejection_reason(
    card: JobCard,
    review: Any,
) -> str | None:
    """对模型卡片初筛结果做本地硬门禁。

    模型负责语义判断，但程序必须保证进入详情前四个条件同时成立：岗位名方向、
    经验、学历、地点。经验/学历由 ``card_rejection_reason`` 先拦；这里再确认
    模型返回的岗位名和地点结论没有自相矛盾。
    """

    if getattr(review, "excluded_direction_match", False):
        keywords = "、".join(
            getattr(review, "matched_excluded_direction_keywords", ()) or ()
        )
        return "大模型命中排除岗位方向" + (f"：{keywords}" if keywords else "")
    if not getattr(review, "job_direction_match", False):
        return "大模型卡片初筛不通过：岗位名未命中目标或简历推断方向：" + str(
            getattr(review, "reason", "")
        )
    if not _direction_evidence_is_grounded(card, review):
        keywords = "、".join(getattr(review, "matched_direction_keywords", ()) or ())
        directions = "、".join(getattr(review, "combined_directions", ()) or ())
        detail = f"模型命中词：{keywords or '无'}；合并方向：{directions or '无'}"
        return "卡片方向硬校验不通过：岗位名未实际命中目标或简历推断方向（" + detail + "）"
    if not getattr(review, "location_match", False):
        return "大模型卡片初筛不通过：工作地点不符合目标城市：" + str(
            getattr(review, "reason", "")
        )
    return None


def _direction_evidence_is_grounded(card: JobCard, review: Any) -> bool:
    title = _direction_key(card.job_name)
    if not title:
        return False
    direction_keys = tuple(
        key
        for key in (
            _direction_key(item)
            for item in (getattr(review, "combined_directions", ()) or ())
        )
        if key
    )
    matched_keys = tuple(
        key
        for key in (
            _direction_key(item)
            for item in (getattr(review, "matched_direction_keywords", ()) or ())
        )
        if key
    )
    if not direction_keys or not matched_keys:
        return False
    return any(
        keyword in title
        and any(
            keyword in direction
            or direction in keyword
            or direction in title
            for direction in direction_keys
        )
        for keyword in matched_keys
    )


def summarize_qualifications(description: str | None, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", description or "").strip()
    if not text:
        return "未识别到任职资格"
    parts = [part.strip() for part in re.split(r"[。；;\n]", text) if part.strip()]
    preferred = [
        part
        for part in parts
        if any(keyword in part for keyword in ("任职", "要求", "经验", "学历", "技能", "熟悉"))
    ]
    selected = preferred[:3] or parts[:2]
    summary = "；".join(selected)
    return summary if len(summary) <= limit else summary[: limit - 1] + "…"
