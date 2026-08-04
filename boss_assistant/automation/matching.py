"""历史离线回归辅助函数；自动化运行器已不再调用这些固定规则。"""

from __future__ import annotations

import re
import unicodedata

from boss_assistant.page import JobPageData
from boss_assistant.resume import ResumeData

from .models import JobCard, JobExpectation, JobIntentData, MatchDecision


def _normalized(value: str | None) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


def _resume_skills(resume: ResumeData) -> tuple[str, ...]:
    values = list(
        resume.skills.programming_languages
        + resume.skills.technical_stack
        + resume.skills.tools
        + resume.skills.other
    )
    for project in resume.projects:
        values.extend(project.technologies)
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def _role_tokens(role: str) -> tuple[str, ...]:
    lowered = _normalized(role)
    tokens = re.findall(r"[a-z0-9+#.]{2,}|[\u4e00-\u9fff]{2,}", lowered)
    generic = {"工程师", "开发工程师", "技术支持", "岗位", "职位"}
    return tuple(token for token in tokens if token not in generic)


def match_job(
    job: JobPageData,
    intents: JobIntentData,
    resume: ResumeData,
    *,
    threshold: int = 50,
    card: JobCard | None = None,
    preferred_job_keywords: tuple[str, ...] = (),
) -> MatchDecision:
    job_name = job.job_name.value or ""
    location = job.location.value or ""
    description = job.job_description.value or ""
    card_text = "\n".join(card.tags) if card else ""
    card_location = card.location if card else ""
    haystack = _normalized("\n".join((job_name, location, description, card_text)))
    skills = _resume_skills(resume)
    matched_skills = tuple(skill for skill in skills if _normalized(skill) in haystack)

    best: tuple[int, JobExpectation | None, list[str]] = (0, None, [])
    for expectation in intents.expectations:
        score = 0
        reasons: list[str] = []
        role = _normalized(expectation.role)
        role_match = bool(role and (role in _normalized(job_name) or _normalized(job_name) in role))
        if not role_match:
            role_match = any(token in haystack for token in _role_tokens(expectation.role))
        if role_match:
            score += 55
            reasons.append(f"职位方向匹配：{expectation.role}")

        if expectation.city and _normalized(expectation.city) in _normalized(
            location + " " + (card_location or "")
        ):
            score += 20
            reasons.append(f"城市匹配：{expectation.city}")

        if matched_skills:
            skill_score = min(25, len(matched_skills) * 5)
            score += skill_score
            reasons.append("简历技能匹配：" + "、".join(matched_skills[:5]))

        if score > best[0]:
            best = (score, expectation, reasons)

    score, expectation, reasons = best
    preferred_match = next(
        (
            keyword
            for keyword in preferred_job_keywords
            if _normalized(keyword) in _normalized(job_name)
        ),
        None,
    )
    if preferred_match:
        score = min(100, score + 55)
        reasons.append(f"页面岗位关键词匹配：{preferred_match}")
    has_positive_evidence = any(
        reason.startswith(("职位方向匹配", "简历技能匹配", "页面岗位关键词匹配"))
        for reason in reasons
    )
    should_apply = score >= threshold and has_positive_evidence
    if not should_apply:
        reasons.append(f"匹配分 {score} 低于投递阈值 {threshold}")
    return MatchDecision(
        should_apply=should_apply,
        score=score,
        matched_expectation=expectation,
        matched_skills=matched_skills,
        reasons=tuple(reasons),
    )


def generate_greeting(job: JobPageData, decision: MatchDecision) -> str:
    job_name = job.job_name.value or "该岗位"
    skills = "、".join(decision.matched_skills[:3])
    if skills:
        return (
            f"您好，我对{job_name}职位很感兴趣。我的{skills}相关经验与岗位要求较匹配，"
            "希望有机会进一步沟通，谢谢！"
        )
    return f"您好，我对{job_name}职位很感兴趣，希望有机会进一步沟通，谢谢！"


def generate_ascii_test_greeting(decision: MatchDecision) -> str:
    """填充验证专用 ASCII 招呼语，兼容 Android 原生 input text。"""

    skills = [skill for skill in decision.matched_skills[:3] if skill.isascii()]
    if skills:
        return (
            "Hello, I am interested in this role. My experience with "
            + ", ".join(skills)
            + " aligns with the position. I look forward to connecting with you."
        )
    return (
        "Hello, I am interested in this role. My background aligns with the position. "
        "I look forward to connecting with you."
    )
