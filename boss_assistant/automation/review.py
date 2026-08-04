"""可替换的大模型审核接口；当前使用文件交换，由 Codex 人工返回结构化判断。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import tomllib
import unicodedata
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Protocol

from opencc import OpenCC

from boss_assistant.page import JobPageData
from boss_assistant.resume import ResumeData

from .models import (
    AutomationPolicy,
    ChatConversation,
    ChatMessage,
    JobCard,
    JobIntentData,
)
from .policy import card_review_rejection_reason


class ReviewError(RuntimeError):
    """大模型审核请求或返回数据不完整，不能继续自动化。"""


class GreetingLengthError(ReviewError):
    """招呼语字符数超出硬范围，供API Provider进入定向压缩流程。"""

    def __init__(self, actual_length: int) -> None:
        self.actual_length = actual_length
        super().__init__(
            f"greeting实际长度为{actual_length}字符，"
            f"必须在{_GREETING_MIN_LENGTH}-{_GREETING_MAX_LENGTH}字符之间"
        )


class GreetingGroundingError(ReviewError):
    """招呼语中的高风险经历、归属或成果陈述没有简历原文依据。"""


class GreetingStyleError(ReviewError):
    """招呼语开场或目标岗位称呼不符合自然沟通规则。"""


class GreetingRepairError(ReviewError):
    """招呼语连续修复仍未通过硬校验；携带未通过的招呼语原文，便于落库排查。

    仍属于处理失败：该岗位不投递、不发送，但把这条招呼语写入结果的 greeting
    字段，供后续分析为何反复通不过硬校验。API 与 Codex 两种审核方式共用。
    """

    def __init__(self, message: str, *, failed_greeting: str) -> None:
        super().__init__(message)
        self.failed_greeting = failed_greeting


# Codex 主导模式固定使用的模型；gpt-5.6 系列当前会被服务端拒绝调用。
CODEX_REQUIRED_MODEL = "gpt-5.5"

_TRADITIONAL_TO_SIMPLIFIED = OpenCC("t2s")
# 前20字符“钩子”只要求出现能力/经历证据，覆盖技术与非技术两类简历；
# 行业措辞由大模型依据简历判断，这里只做“是否亮出证据”的宽校验。
_GREETING_HOOK_EVIDENCE = (
    "我有",
    "我曾",
    "我能",
    "经验",
    "项目",
    "实习",
    "交付",
    "获奖",
    "一等奖",
    "负责",
    "完成",
    "具备",
    "掌握",
    "熟悉",
    "擅长",
    "主导",
    "主管",
    "业绩",
    "成果",
    "服务",
)
_GREETING_COMMUNICATION_INTENT = (
    "希望与您沟通",
    "希望与您进一步沟通",
    "期待与您沟通",
    "期待与您进一步沟通",
)
# 真实职责证据：既含技术动词，也含通用/非技术岗位的职责动词，
# 使市场、运营、销售、财务、行政、教育等行业的真实职责表达也能通过。
# 只放宽“接受面”，不放松事实接地校验（见 _validate_greeting_grounding）。
_GREETING_RESPONSIBILITY_EVIDENCE = (
    "设计",
    "实现",
    "开发",
    "优化",
    "搭建",
    "编写",
    "完成",
    "交付",
    "部署",
    "落地",
    "负责",
    "参与",
    "闭环",
    "验证",
    "上线",
    "运营",
    "策划",
    "组织",
    "协调",
    "统筹",
    "推进",
    "执行",
    "达成",
    "复盘",
    "分析",
    "撰写",
    "主持",
    "主导",
    "对接",
    "拓展",
    "制定",
    "培训",
    "招聘",
    "核算",
    "管理",
    "运维",
    "维护",
    "服务",
)
_GREETING_MIN_LENGTH = 80
_GREETING_MAX_LENGTH = 150
# 结尾校验的基础窗口；实际窗口按目标岗位名长度自适应放大。
_GREETING_CLOSING_WINDOW = 35
_JOB_PROMOTION_MARKERS = (
    "五险",
    "一金",
    "六险",
    "社保",
    "公积金",
    "双休",
    "大小周",
    "周末",
    "急招",
    "高薪",
    "福利",
    "奖金",
    "年终奖",
    "绩效",
    "补贴",
    "餐补",
    "房补",
    "包吃",
    "包住",
    "住宿",
    "带薪",
    "不加班",
)
_SALARY_PROMOTION_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:[-–—~至]\s*\d+(?:\.\d+)?)?\s*(?:[KkWw]|万)(?:薪)?"
)
# 岗位名尾部的无语义装饰符号（如“前后端开发工程师 @%”“售后技术支持 &@”）。
# 只清理 ASCII 符号与空白，保留中英文、数字和成对括号等有语义的内容。
_GREETING_JOB_NAME_NOISE_SUFFIX = re.compile(r"[\s&@%#*~^_+=<>!?.,;:'\"`$\\-]+$")
# 高风险句式按“事实类型”分类，便于分级接地校验：
#   - 实习/任职归属（在某公司实习）：只需公司名可在简历检索到；
#   - 部署/交付/上线等成果状态：必须逐字回指简历，禁止把“启动部署文档”夸大成“已部署”；
#   - 独立/主导等排他性归属：必须逐字回指简历；
#   - 完成/开发某项目/系统/工具：只需项目名核心可在简历检索到（允许自然改写）。
# 实习归属若与项目/成果出现在同一小句（跨区块拼接），则升级为必须逐字回指。
_GREETING_INTERNSHIP_PATTERN = re.compile(r"(?:在|于)(.{2,30}?)(?:实习|工作|任职)")
_GREETING_DEPLOY_STATUS_PATTERN = re.compile(
    r"(?:项目|系统|平台|工具|应用|Demo|追踪器).{0,20}(?:已)?(?:部署|交付|上线|投产)",
    re.IGNORECASE,
)
_GREETING_SOLE_OWNERSHIP_PATTERN = re.compile(r"(?:独立|主导)(?:负责|完成|开发|设计|交付)")
_GREETING_BUILD_ARTIFACT_PATTERN = re.compile(
    r"(?:完成|开发|设计|部署|交付)(.{0,20}?)(项目|系统|平台|工具|应用|Demo|追踪器)",
    re.IGNORECASE,
)
_GREETING_PRONOUN_PREFIX = re.compile(r"^(?:本人)?(?:我有|我曾|我能|我|曾经|曾)")
# 公司名尾部的通用后缀，去掉后再与简历比对，避免“实在智能公司”这类写法误判。
_CORP_GENERIC_SUFFIXES = (
    "有限责任公司",
    "有限公司",
    "科技有限公司",
    "股份有限公司",
    "股份公司",
    "责任公司",
    "研究院",
    "研究所",
    "事业部",
    "分公司",
    "子公司",
    "集团",
    "公司",
    "科技",
    "股份",
    "部门",
    "团队",
    "期间",
)


def _normalize_fact_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


_HARD_EXPERIENCE_YEARS_PATTERN = re.compile(
    r"(?:\d+(?:\s*[-–—~至]\s*\d+)?|[一二三四五六七八九十两]+)\s*年"
)


def _minimum_experience_years(value: str) -> int | None:
    normalized = unicodedata.normalize("NFKC", value)
    range_match = re.search(r"(\d+)\s*[-–—~至]\s*(\d+)\s*年", normalized)
    if range_match:
        return int(range_match.group(1))
    single_match = re.search(r"(\d+)\s*年", normalized)
    if single_match:
        return int(single_match.group(1))
    chinese_digits = {
        "一": 1,
        "二": 2,
        "两": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    chinese_match = re.search(r"([一二三四五六七八九十两]+)\s*年", normalized)
    if chinese_match:
        token = chinese_match.group(1)
        if token in chinese_digits:
            return chinese_digits[token]
    return None


def _validate_detail_rejection_evidence(
    job: JobPageData,
    policy: AutomationPolicy,
    detail_excluded_keywords: tuple[str, ...],
    experience_conflicts: tuple[str, ...],
) -> None:
    """拒投证据必须逐字存在于当前详情，不能只相信模型的布尔结论。"""

    detail_text = job.job_description.value or ""
    normalized_detail = _normalize_fact_text(detail_text)
    # 工作年限是 Boss 独立字段（tv_required_work_exp），常不在职责正文里；
    # 年限证据允许在“职责正文 + 工作年限字段”中逐字回指，排除方向证据仍只查正文。
    experience_field = getattr(job, "experience", None)
    experience_value = experience_field.value if experience_field else None
    normalized_experience_source = _normalize_fact_text(
        f"{detail_text} {experience_value or ''}"
    )
    normalized_directions = tuple(
        _normalize_fact_text(direction) for direction in policy.excluded_job_directions
    )
    for keyword in detail_excluded_keywords:
        normalized_keyword = _normalize_fact_text(keyword)
        if not normalized_keyword or normalized_keyword not in normalized_detail:
            raise ReviewError(f"详情排除方向证据未在岗位详情中出现：{keyword}")
        if not any(
            normalized_keyword in direction or direction in normalized_keyword
            for direction in normalized_directions
        ):
            raise ReviewError(f"详情排除方向证据不属于用户配置的排除方向：{keyword}")
    for evidence in experience_conflicts:
        normalized_evidence = _normalize_fact_text(evidence)
        if not normalized_evidence or normalized_evidence not in normalized_experience_source:
            raise ReviewError(f"硬性工作年限证据未逐字出现在岗位详情中：{evidence}")
        if not _HARD_EXPERIENCE_YEARS_PATTERN.search(evidence):
            raise ReviewError(f"硬性工作年限证据没有明确年数：{evidence}")
        minimum_years = _minimum_experience_years(evidence)
        if minimum_years is None or minimum_years < 3:
            raise ReviewError(
                f"工作年限最低门槛不足3年，不构成详情拒投依据：{evidence}"
            )


def _is_job_promotion(value: str) -> bool:
    compact = re.sub(r"\s+", "", value)
    return any(marker in compact for marker in _JOB_PROMOTION_MARKERS) or bool(
        _SALARY_PROMOTION_PATTERN.search(compact)
    )


def _greeting_job_name(job_name: str) -> str:
    """移除岗位名尾部待遇和招聘宣传，只保留职位及必要方向说明。"""

    original = re.sub(r"\s+", " ", job_name).strip(" -—–|｜·/，,。")
    candidate = original
    while candidate:
        previous = candidate
        bracket = re.search(r"\s*[（(【\[]([^）)】\]]+)[）)】\]]\s*$", candidate)
        if bracket and _is_job_promotion(bracket.group(1)):
            candidate = candidate[: bracket.start()].rstrip(" -—–|｜·/，,")
            continue
        suffix = re.search(r"\s*[-—–|｜·/]\s*([^\-—–|｜·/]+)\s*$", candidate)
        if suffix and _is_job_promotion(suffix.group(1)):
            candidate = candidate[: suffix.start()].rstrip(" -—–|｜·/，,")
            continue
        trimmed = _GREETING_JOB_NAME_NOISE_SUFFIX.sub("", candidate)
        if trimmed != candidate:
            # Boss 卡片上的岗位名常带 “&@”“@%”等装饰符号；它们没有语义，
            # 却会被结尾硬校验要求逐字写进招呼语，必须在这里先清掉。
            candidate = trimmed
            continue
        if candidate == previous:
            break
    return candidate or original


def _greeting_job_label(job_name: str) -> str:
    name = _greeting_job_name(job_name)
    return name if name.endswith("岗位") else name + "岗位"


def _strip_corp_suffix(core: str) -> str:
    """去掉公司名尾部的通用后缀，便于与简历原文做核心比对。"""

    changed = True
    while changed and core:
        changed = False
        for suffix in _CORP_GENERIC_SUFFIXES:
            normalized_suffix = _normalize_fact_text(suffix)
            if len(core) > len(normalized_suffix) and core.endswith(normalized_suffix):
                core = core[: -len(normalized_suffix)]
                changed = True
                break
    return core


def _company_grounded(company: str, normalized_resume: str) -> bool:
    """公司/机构名（或其去后缀核心）可在简历原文检索到即视为有依据。"""

    full = _normalize_fact_text(company)
    if len(full) >= 2 and full in normalized_resume:
        return True
    core = _strip_corp_suffix(full)
    return len(core) >= 2 and core in normalized_resume


# 动词与项目名之间常见的体态助词/量词/指示词，比对前需剥离，
# 避免把“开发过X”“完成了X”“开发一套X”里的“过/了/一套”并入项目名导致误判。
_ARTIFACT_LEADING_NOISE = re.compile(
    r"^(?:过|了|的|一套|一个|一项|一款|一门|整套|该|这|那|本|相关|其|某|出|好)+"
)


def _artifact_grounded(middle: str, type_noun: str, normalized_resume: str) -> bool:
    """项目/系统/工具名可整体或其核心描述（≥4字符）在简历检索到即视为有依据。"""

    middle_core = _ARTIFACT_LEADING_NOISE.sub("", middle.strip())
    artifact = _normalize_fact_text(middle_core + type_noun)
    if artifact and artifact in normalized_resume:
        return True
    core = _normalize_fact_text(middle_core)
    return len(core) >= 4 and core in normalized_resume


def merge_combined_directions(
    user_directions: tuple[str, ...],
    resume_directions: tuple[str, ...],
    model_directions: tuple[str, ...],
) -> tuple[str, ...]:
    """按程序规则完成方向合并，不依赖模型是否记得逐项保留。

    “用户方向 ∪ 简历辅助方向”是纯机械的集合并操作，模型没有裁量空间。此前把
    它当成模型输出校验，模型一旦自作主张归并（把“前端开发”并进“前端”“开发”）
    就会抛错并终止整轮运行。这里改由程序补齐：保留模型给出的顺序和额外表达，
    再补上任何缺失的用户方向与简历方向。
    """

    merged: list[str] = []
    seen: set[str] = set()
    for item in (*model_directions, *user_directions, *resume_directions):
        key = _normalize_fact_text(item)
        if not key or key in seen:
            continue
        seen.add(key)
        merged.append(item.strip())
    return tuple(merged)


def _greeting_grounding_error(clause: str) -> GreetingGroundingError:
    return GreetingGroundingError(
        f"招呼语高风险事实无简历原文逐句依据：“{clause}”；"
        "项目归属、实习归属以及部署/交付/上线等成果不得跨区块推断，"
        "请改用简历中可直接检索的原句或删除该陈述"
    )


def _validate_greeting_grounding(
    greeting: str,
    *,
    should_apply: bool,
    resume_text: str,
) -> None:
    """高风险归属和成果必须能回指到简历原文，拒绝跨区块拼接与夸大成果。

    分级校验，避免把“对简历事实的自然改写”误判为虚构：
      1) 任何高风险小句只要能逐字回指简历，直接通过；
      2) 部署/交付/上线等成果状态、独立/主导等排他归属、以及实习与项目
         出现在同一小句的跨区块拼接，必须逐字回指，否则拒绝；
      3) 仅陈述实习/任职归属时，公司名可在简历检索到即通过；
      4) 仅陈述完成/开发某项目时，项目名核心可在简历检索到即通过。
    """

    if not should_apply:
        return
    normalized_resume = _normalize_fact_text(resume_text)
    if not normalized_resume:
        raise GreetingGroundingError("缺少简历原文，无法核验招呼语事实")
    for raw_clause in re.split(r"[，,；;。！!\n]+", greeting):
        clause = raw_clause.strip()
        if not clause or any(marker in clause for marker in _GREETING_COMMUNICATION_INTENT):
            continue
        internship_matches = list(_GREETING_INTERNSHIP_PATTERN.finditer(clause))
        build_matches = list(_GREETING_BUILD_ARTIFACT_PATTERN.finditer(clause))
        has_internship = bool(internship_matches)
        has_deploy = bool(_GREETING_DEPLOY_STATUS_PATTERN.search(clause))
        has_ownership = bool(_GREETING_SOLE_OWNERSHIP_PATTERN.search(clause))
        has_build = bool(build_matches)
        if not (has_internship or has_deploy or has_ownership or has_build):
            continue

        normalized_claim = _GREETING_PRONOUN_PREFIX.sub("", _normalize_fact_text(clause))
        if normalized_claim and normalized_claim in normalized_resume:
            continue

        # 跨区块拼接：实习归属与项目/成果同处一句，必须逐字回指，否则视为把
        # 个人项目错误安到实习公司名下。
        welded = has_internship and (has_deploy or has_ownership or has_build)
        if has_deploy or has_ownership or welded:
            raise _greeting_grounding_error(clause)

        if has_internship:
            if not all(
                _company_grounded(match.group(1), normalized_resume)
                for match in internship_matches
            ):
                raise _greeting_grounding_error(clause)
            continue

        if has_build:
            if not all(
                _artifact_grounded(match.group(1), match.group(2), normalized_resume)
                for match in build_matches
            ):
                raise _greeting_grounding_error(clause)
            continue


def _validate_chinese_greeting(
    greeting: str,
    *,
    should_apply: bool,
    matched_skills: tuple[str, ...] = (),
    job_name: str = "",
) -> None:
    """校验招呼语的预览钩子、岗位证据、篇幅和主动沟通闭环。"""

    if not should_apply:
        return
    if not any("\u4e00" <= char <= "\u9fff" for char in greeting):
        raise ReviewError("greeting必须使用简体中文")
    if _TRADITIONAL_TO_SIMPLIFIED.convert(greeting) != greeting:
        raise ReviewError("greeting检测到繁体中文，必须改为简体中文")
    if not _GREETING_MIN_LENGTH <= len(greeting) <= _GREETING_MAX_LENGTH:
        raise GreetingLengthError(len(greeting))
    if "本人" in greeting:
        raise GreetingStyleError(
            "greeting不得出现“本人”等生硬书面称呼，请统一使用自然的“我”"
        )
    if not re.match(r"^(?:(?:您好|你好)[，,！!。.]?我|我)", greeting):
        raise GreetingStyleError(
            "greeting必须以“您好，我…”或“我…”等自然第一人称表达开头，"
            "不得直接以项目名、技术名或经历名词起句"
        )
    preview = greeting[:20]
    if not any(marker in preview for marker in _GREETING_HOOK_EVIDENCE):
        raise ReviewError(
            "greeting前20个字符必须先展示项目、经验或能力等候选人匹配证据"
        )
    normalized_greeting = _normalize_fact_text(greeting)
    skill_hits = {
        skill
        for skill in matched_skills
        if skill.strip() and _normalize_fact_text(skill) in normalized_greeting
    }
    required_skill_hits = min(3, len(matched_skills))
    if len(skill_hits) < required_skill_hits:
        raise ReviewError(
            f"greeting必须明确包含至少{required_skill_hits}项已匹配的岗位技能或能力"
        )
    if not any(marker in greeting for marker in _GREETING_RESPONSIBILITY_EVIDENCE):
        raise ReviewError(
            "greeting必须包含负责、完成、参与、开发、运营、策划或管理等真实职责/工作证据"
        )
    target_job_name = _greeting_job_name(job_name)
    target_label = _greeting_job_label(job_name) if target_job_name else ""
    # 结尾窗口必须容得下“希望与您进一步沟通 + 完整岗位名 + 岗位”；固定 35 字符会把
    # 长岗位名（如“RPA开发工程师 / Python自动化开发工程师”）的沟通句挤出窗口，
    # 造成任何招呼语都过不了结尾校验。
    closing_window = max(_GREETING_CLOSING_WINDOW, len(target_label) + 24)
    closing = greeting[-closing_window:]
    if not any(marker in closing for marker in _GREETING_COMMUNICATION_INTENT):
        raise ReviewError("greeting结尾必须主动表达希望或期待与Boss进一步沟通")
    if target_job_name:
        # 两侧必须用同一套归一化：岗位名自带空格（“（偏集成 / 低代码）”）时，
        # 只对招呼语去空格会让比对永远失败。
        normalized_closing = _normalize_fact_text(closing)
        if _normalize_fact_text(f"沟通{target_label}") not in normalized_closing:
            raise GreetingStyleError(
                f"greeting结尾必须只使用清理后的岗位名称“{target_job_name}”，"
                "不得携带五险一金、双休、薪资等待遇或宣传后缀"
            )
    elif "岗位" not in closing:
        raise ReviewError("greeting结尾的沟通请求必须明确指向目标岗位")


def _safely_compact_greeting(greeting: str) -> str:
    """仅删除不改变事实的冗余表达，用于收尾轻微超限的模型结果。"""

    candidate = greeting.strip().replace(" ", "")
    replacements = (
        ("希望与您进一步沟通", "希望与您沟通"),
        ("期待与您进一步沟通", "期待与您沟通"),
        ("并且", "并"),
        ("以及", "及"),
        ("能够", "能"),
        ("相关的", ""),
        ("相关", ""),
        ("实际", ""),
        ("多个", ""),
    )
    if len(candidate) > _GREETING_MAX_LENGTH and candidate.endswith(("。", "！", "!")):
        candidate = candidate[:-1]
    for source, replacement in replacements:
        if len(candidate) <= _GREETING_MAX_LENGTH:
            break
        compacted = candidate.replace(source, replacement, 1)
        if len(compacted) >= _GREETING_MIN_LENGTH:
            candidate = compacted
    return candidate


def _safely_expand_short_greeting(greeting: str, resume_text: str) -> str:
    """只用简历原文事实补足过短候选，不再信任模型生成的原招呼语。"""

    candidate = greeting.strip().replace(" ", "")
    if len(candidate) >= _GREETING_MIN_LENGTH:
        return candidate

    communication_markers = (
        "希望与您进一步沟通",
        "期待与您进一步沟通",
        "希望与您沟通",
        "期待与您沟通",
    )
    closing_index = max(candidate.rfind(marker) for marker in communication_markers)
    if closing_index < 0:
        return candidate
    body = candidate[:closing_index].rstrip("，,；;。！!")
    closing = candidate[closing_index:]

    source_fragments: list[str] = []
    # 事实片段标记覆盖技术与非技术岗位，使两类简历都能从原文补足过短招呼语。
    factual_markers = (
        "负责",
        "完成",
        "参与",
        "实现",
        "开发",
        "设计",
        "掌握",
        "熟悉",
        "技术栈",
        "项目",
        "实习",
        "工作",
        "获奖",
        "运营",
        "策划",
        "组织",
        "统筹",
        "达成",
        "复盘",
        "管理",
        "服务",
        "对接",
        "拓展",
        "培训",
        "撰写",
        "业绩",
        "客户",
    )
    for fragment in re.split(r"[，,。；;！!\n]+", resume_text):
        cleaned = fragment.strip().replace(" ", "")
        if (
            len(cleaned) >= 6
            and cleaned not in candidate
            and not any(marker in cleaned for marker in communication_markers)
            and any(marker in cleaned for marker in factual_markers)
            and not any(
                sensitive in cleaned
                for sensitive in ("姓名：", "电话：", "邮箱：", "微信：")
            )
        ):
            source_fragments.append(cleaned)

    valid_trials = []
    for fragment in source_fragments:
        trial = f"{body}，{fragment}，{closing}"
        if _GREETING_MIN_LENGTH <= len(trial) <= _GREETING_MAX_LENGTH:
            valid_trials.append(trial)
    if valid_trials:
        return min(valid_trials, key=lambda item: abs(len(item) - 95))

    expanded_body = body
    for fragment in sorted(source_fragments, key=len, reverse=True):
        trial = f"{expanded_body}，{fragment}，{closing}"
        if len(trial) <= _GREETING_MAX_LENGTH:
            expanded_body = f"{expanded_body}，{fragment}"
            if len(trial) >= _GREETING_MIN_LENGTH:
                return trial

    # 只表达进一步说明和交流意愿，不新增经历、成果或技能事实。
    neutral_bridges = (
        "也愿意进一步说明相关技能的学习与实践过程",
        "可结合岗位需求进一步交流具体实践",
        "也可进一步介绍对应职责的落实细节",
    )
    for bridge in neutral_bridges:
        trial = f"{expanded_body}，{bridge}，{closing}"
        if len(trial) <= _GREETING_MAX_LENGTH:
            expanded_body = f"{expanded_body}，{bridge}"
            if len(trial) >= _GREETING_MIN_LENGTH:
                return trial
    return f"{expanded_body}，{closing}"


_SYNTH_SENSITIVE_MARKERS = ("姓名", "电话", "邮箱", "微信", "手机", "github", "http", "@")
# 以真实职责动词开头的简历片段读起来更自然，合成时优先选用；
# 同时覆盖技术与非技术岗位的常见职责动词首字。
_SYNTH_VERB_INITIALS = "设实完开负参搭编优验运策组统筹达复管服对拓培撰主执"


def _synthesize_grounded_greeting(
    resume_text: str,
    matched_skills: tuple[str, ...],
    job_name: str,
) -> str | None:
    """模型多次修复仍失败时，用简历原文片段本地合成一条必定通过硬校验的招呼语。

    只使用简历中可逐字检索的职责片段、已匹配技能和固定问候/结尾，从而同时满足：
    自然第一人称开场、前20字符含能力证据、逐字包含≥3项已匹配技能、含真实职责证据、
    结尾主动沟通清理后的目标岗位、长度落在硬范围内。任何一项无法满足时返回 None，
    交由上层继续抛出原始错误，绝不降低事实标准。
    """

    label = _greeting_job_label(job_name)
    required = min(3, len(matched_skills))
    skills = [skill.strip() for skill in matched_skills[:required] if skill.strip()]
    skills_part = ("掌握" + "、".join(skills)) if skills else ""
    # 开场措辞按简历自适应（实习/在职/其它），保持自然；真正的职责证据由下方
    # 从简历检索到的片段承担，因此开场无需强行使用技术动词。
    if "实习" in resume_text:
        opener = "您好，我有相关项目与实习经验"
    elif "工作" in resume_text:
        opener = "您好，我有相关岗位与工作经验"
    else:
        opener = "您好，我有相关项目与实践经验"
    closing = f"希望与您进一步沟通{label}"

    fragments: list[str] = []
    for raw in re.split(r"[，,。；;！!\n、：:]+", resume_text):
        fragment = re.sub(r"^\d+[.、)）]\s*", "", raw.strip().replace(" ", ""))
        if not 8 <= len(fragment) <= 40:
            continue
        lowered = fragment.casefold()
        if any(marker in lowered for marker in _SYNTH_SENSITIVE_MARKERS):
            continue
        if re.search(r"20\d{2}", fragment) or len(re.findall(r"\d", fragment)) >= 4:
            continue  # 跳过时间段、时间轴等日期/数字密集片段
        if any(verb in fragment for verb in _GREETING_RESPONSIBILITY_EVIDENCE):
            fragments.append(fragment)
    fragments.sort(key=lambda item: 0 if item[:1] in _SYNTH_VERB_INITIALS else 1)

    def assemble(body: str) -> str:
        head = f"{opener}，{body}" if body else opener
        if skills_part:
            head = f"{head}，{skills_part}"
        return f"{head}，{closing}。"

    body = ""
    for fragment in fragments:
        trial_body = f"{body}，{fragment}" if body else fragment
        if len(assemble(trial_body)) > _GREETING_MAX_LENGTH:
            continue
        body = trial_body
        if len(assemble(body)) >= _GREETING_MIN_LENGTH:
            return assemble(body)
    candidate = assemble(body)
    if _GREETING_MIN_LENGTH <= len(candidate) <= _GREETING_MAX_LENGTH:
        return candidate
    return None


@dataclass(frozen=True)
class CardReviewResult:
    eligible: bool
    job_direction_match: bool
    location_match: bool
    reason: str
    resume_inferred_directions: tuple[str, ...] = ()
    combined_directions: tuple[str, ...] = ()
    matched_direction_keywords: tuple[str, ...] = ()
    excluded_direction_match: bool = False
    matched_excluded_direction_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class DetailReviewResult:
    should_apply: bool
    score: int
    reasons: tuple[str, ...]
    matched_skills: tuple[str, ...]
    greeting: str
    qualifications_summary: str
    matched_detail_excluded_direction_keywords: tuple[str, ...] = ()
    hard_experience_requirement_conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChatReviewResult:
    """HR 未读消息的意图判定；联系方式请求绝不能复用简历卡片按钮。"""

    resume_requested: bool
    reply: str
    reason: str
    contact_requested: bool = False
    # 明确拒绝、结束沟通或纯状态通知无需用户处理，也绝不能进入通用置顶分支。
    no_action_needed: bool = False


DEFAULT_RESUME_REPLY = "好的，这是我的简历，您看看"
# 聊天区里表示“附件简历已经发出去了”的系统提示。真机上共出现过三种文案：
#   我方点“发简历”：      “附件简历请求已发送”
#   对方同意后：          “对方已同意，您的附件简历已发送给对方”“对方已查看了您的附件简历”
#   同意HR的索要卡片后：  “您的附件简历 示例简历... 已发送给Boss点击查看附件”
# 第三种中间夹着文件名，固定短语匹配不到，因此用“附件简历…已发送”的模式匹配。
# 关键是不能只匹配“附件简历”子串：HR 索要卡片的文案“我想要一份您的附件简历，您
# 是否同意”语义相反，误判成已发送会让模型返回 false 而漏发简历。
_RESUME_SENT_PATTERN = re.compile(
    r"附件简历.{0,40}已发送"
    r"|查看了您的附件简历"
    r"|(?:对方|Boss).{0,16}(?:已查看|查看了?).{0,16}"
    r"(?:你的|您的)?(?:附件)?简历"
)
# HR 通过 Boss 标准功能索要简历时发出的卡片，附带“拒绝”“同意”两个按钮。
# 这是最明确的索要信号，语义与上面的“已发送”完全相反。
RESUME_REQUEST_CARD_MARKERS = (
    "我想要一份您的附件简历",
    "想要一份您的附件简历",
)


def resume_already_sent(notes: Iterable[str]) -> bool:
    """判断聊天区是否已经发出过附件简历。"""

    return any(_RESUME_SENT_PATTERN.search(note) for note in notes)


def recruiter_requested_resume_card(notes: Iterable[str]) -> bool:
    """聊天区是否存在 HR 主动索要附件简历的卡片。"""

    return any(
        marker in note for note in notes for marker in RESUME_REQUEST_CARD_MARKERS
    )
# 回复只允许是一句不含承诺、条件、联系方式和数字的简短中文。
_CHAT_REPLY_MIN_LENGTH = 6
_CHAT_REPLY_MAX_LENGTH = 30
_CHAT_REPLY_FORBIDDEN = (
    "微信",
    "电话",
    "手机",
    "邮箱",
    "http",
    "www.",
    "@",
    "薪资",
    "工资",
    "面试",
    "入职",
    "到岗",
    "一定",
    "保证",
    "承诺",
)


def validate_chat_reply(reply: str) -> str:
    """校验模型给出的简短回复；任何不合规都回退到固定模板，绝不外发不可控内容。"""

    candidate = re.sub(r"\s+", "", reply or "")
    if not _CHAT_REPLY_MIN_LENGTH <= len(candidate) <= _CHAT_REPLY_MAX_LENGTH:
        return DEFAULT_RESUME_REPLY
    if not any("一" <= char <= "鿿" for char in candidate):
        return DEFAULT_RESUME_REPLY
    if _TRADITIONAL_TO_SIMPLIFIED.convert(candidate) != candidate:
        return DEFAULT_RESUME_REPLY
    if re.search(r"\d", candidate):
        return DEFAULT_RESUME_REPLY
    lowered = candidate.casefold()
    if any(marker.casefold() in lowered for marker in _CHAT_REPLY_FORBIDDEN):
        return DEFAULT_RESUME_REPLY
    return candidate


_CHAT_REVIEW_INSTRUCTIONS = (
    "你负责判断HR的最新未读消息是在索要附件简历、索要联系方式，还是其他内容；判断结果决定程序是否自动沟通，必须保守。",
    "resume_requested和contact_requested只依据 payload.unanswered_recruiter_messages（我方最后一次回应之后HR才说的话）作判断。payload.conversation 里更早的请求已经被回应过，绝不能再次触发回复；该数组为空时这两项必须返回false。若system_notes明确是纯状态通知，no_action_needed仍可返回true。",
    "payload.system_notes 是Boss生成的系统提示（如“附件简历请求已发送”“对方已同意，您的附件简历已发送给对方”“对方已查看了您的附件简历”），它们不是HR说的话，只能当作上下文，不得据此判定HR在索要简历。",
    "payload.resume_already_sent 为 true 时说明简历已经发过，HR不可能还在要简历，必须返回 false。注意它只在系统提示明确表示“已发送/已查看”时为 true；“我想要一份您的附件简历，您是否同意”是HR在索要简历，语义相反，不属于已发送。",
    "payload.recruiter_requested_resume_card 为 true 表示HR通过Boss的标准功能发来了索要附件简历的卡片，这是最明确的索要信号，此时只要 resume_already_sent 为 false 就必须返回 true。",
    "resume_requested=true 只允许用于HR明确、直接索要简历附件的情形，例如“方便发份简历吗”“简历发我看看”“把简历发过来”“投个简历”“看下你的简历”。",
    "contact_requested=true 只允许用于HR明确索要电话、手机号、微信或其他联系方式，以及Boss标准联系方式交换卡片。程序不会自动同意交换联系方式，只会在岗位匹配时礼貌回复并发送附件简历。",
    "no_action_needed=true 只允许用于HR明确拒绝候选人、明确结束合作/沟通，或无需候选人回应的纯状态通知；拒绝措辞不限于固定模板。此时 resume_requested和contact_requested必须都为false，程序会直接返回且不置顶。",
    "岗位介绍、询问是否有兴趣、需要候选人回答的问题、约面试、无法判断的消息都不是no_action_needed；这些消息应交由用户处理。",
    "以下情形 resume_requested和contact_requested都必须为false：向候选人提问（是否有某项经验、是否熟悉某工具、期望薪资、能否到岗、是否接受某条件）、介绍岗位或公司、约面试、寒暄、以及任何只是提到“简历”却不是索要附件的说法（如“你的简历我看过了”）。",
    "消息同时包含提问和索要简历时，只要提问需要候选人本人作答，就返回false，交由用户处理。",
    "无法确定时必须返回false。",
    "reply只在resume_requested=true时使用：一句6-30个字的简体中文，礼貌说明随附简历，例如“好的，这是我的简历，您看看”。不得包含数字、联系方式、薪资、面试或入职承诺，也不得回答HR提出的任何问题。",
    "resume_requested和contact_requested都为false时reply填写“不自动回复”。",
    "reason用中文说明判断依据，引用HR消息中的关键表述。",
)


class JobReviewProvider(Protocol):
    def review_card(
        self,
        card: JobCard,
        policy: AutomationPolicy,
        resume: ResumeData,
        resume_text: str,
    ) -> CardReviewResult: ...

    def review_detail(
        self,
        card: JobCard,
        job: JobPageData,
        policy: AutomationPolicy,
        intents: JobIntentData,
        resume: ResumeData,
        resume_text: str,
    ) -> DetailReviewResult: ...

    def review_chat_message(
        self,
        conversation: ChatConversation,
        messages: tuple[ChatMessage, ...],
        *,
        system_notes: tuple[str, ...] = (),
    ) -> ChatReviewResult: ...


class ManualFileReviewProvider:
    """通过 request/response JSON 文件把模型职责交给当前 Codex 会话。"""

    def __init__(
        self,
        directory: str | Path = "data/manual_reviews",
        *,
        timeout_seconds: float = 900.0,
        poll_interval_seconds: float = 0.25,
        output: Callable[[str], None] = print,
        request_id_factory: Callable[[], str] | None = None,
        control_point: Callable[[], None] | None = None,
    ) -> None:
        if timeout_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("人工审核超时和轮询间隔必须大于0")
        self.directory = Path(directory)
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.output = output
        self.request_id_factory = request_id_factory or self._new_request_id
        self.control_point = control_point

    @staticmethod
    def _new_request_id() -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return f"{stamp}_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _card_payload(card: JobCard) -> dict[str, object]:
        return {
            "job_name": card.job_name,
            "company_name": card.company_name,
            "salary": card.salary,
            "company_scale": card.company_scale,
            "location": card.location,
            "recruiter_activity": card.recruiter_activity,
            "tags": list(card.tags),
        }

    @staticmethod
    def _policy_payload(policy: AutomationPolicy) -> dict[str, object]:
        return {
            "excluded_companies": list(policy.excluded_companies),
            "target_job_directions": list(policy.allowed_job_keywords),
            "excluded_job_directions": list(policy.excluded_job_directions),
            "target_cities": list(policy.allowed_locations),
            "minimum_score": policy.minimum_score,
            "salary_min_k": policy.salary_min_k,
            "salary_max_k": policy.salary_max_k,
            "minimum_company_size": policy.minimum_company_size,
        }

    def _exchange(
        self,
        kind: str,
        instructions: tuple[str, ...],
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.directory.mkdir(parents=True, exist_ok=True)
        request_id = self.request_id_factory()
        request_path = self.directory / f"{request_id}.request.json"
        response_path = self.directory / f"{request_id}.response.json"
        request = {
            "schema_version": 1,
            "request_id": request_id,
            "kind": kind,
            "instructions": list(instructions),
            "payload": payload,
        }
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.output(f"等待 Codex 大模型审核：{request_path.resolve()}")
        deadline = time.monotonic() + self.timeout_seconds
        while time.monotonic() < deadline:
            if self.control_point:
                self.control_point()
            if response_path.is_file():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ReviewError(f"无法读取审核返回：{response_path.resolve()}：{exc}") from exc
                if not isinstance(response, dict):
                    raise ReviewError("审核返回必须是JSON对象")
                if response.get("request_id") != request_id or response.get("kind") != kind:
                    raise ReviewError("审核返回的request_id或kind与请求不一致")
                return response
            time.sleep(self.poll_interval_seconds)
        raise ReviewError(f"等待 Codex 审核超时：{request_path.resolve()}")

    @staticmethod
    def _required_bool(response: dict[str, object], key: str) -> bool:
        value = response.get(key)
        if type(value) is not bool:
            raise ReviewError(f"审核返回字段 {key} 必须是布尔值")
        return value

    @staticmethod
    def _required_text(response: dict[str, object], key: str) -> str:
        value = response.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ReviewError(f"审核返回字段 {key} 必须是非空字符串")
        return value.strip()

    @staticmethod
    def _text_tuple(
        response: dict[str, object],
        key: str,
        *,
        allow_empty: bool = False,
    ) -> tuple[str, ...]:
        value = response.get(key)
        if (
            not isinstance(value, list)
            or (not allow_empty and not value)
            or not all(isinstance(item, str) and item.strip() for item in value)
        ):
            requirement = "字符串数组" if allow_empty else "非空字符串数组"
            raise ReviewError(f"审核返回字段 {key} 必须是{requirement}")
        return tuple(item.strip() for item in value)

    def review_card(
        self,
        card: JobCard,
        policy: AutomationPolicy,
        resume: ResumeData,
        resume_text: str,
    ) -> CardReviewResult:
        instructions = (
                "你负责岗位卡片语义初筛，不得依赖代码中的固定岗位关键词表、方向示例或区县表。",
                "岗位方向只能来自两部分：policy.target_job_directions 是用户输入的主要方向；根据结构化简历和简历原文语义推断出的方向是辅助方向。不得用手机求职意向中的职位方向替代这两部分。",
                "先返回 resume_inferred_directions，再将用户主要方向与简历辅助方向去重合并为 combined_directions；用户输入方向必须全部保留。",
                "排除方向具有最高优先级。只根据 card.job_name 判断：如果岗位名在语义上包含 policy.excluded_job_directions 中任一方向的全部或部分有效关键词、简称或近义表达，excluded_direction_match=true，并在 matched_excluded_direction_keywords 中列出命中词。排除方向为空时必须返回false和空数组。",
                "一旦 excluded_direction_match=true，该岗位不得进入详情、不得生成招呼语、不得打招呼。即使目标方向也匹配，排除结果仍优先。",
                "方向初筛只判断 card.job_name：当岗位名在语义上包含 combined_directions 中任一方向的全部或部分有效关键词时，job_direction_match=true并列出 matched_direction_keywords；不得仅凭标签、公司名或招聘者状态判定方向匹配。",
                "方向初筛可以接受标题中的合理简称、近义表达或方向中的部分有效关键词，但matched_direction_keywords必须来自combined_directions并实际出现在card.job_name语义中；HR、文员、人事、行政等词只有在combined_directions明确包含对应方向时才可命中。岗位名未实际命中任何合并方向时，必须job_direction_match=false且matched_direction_keywords为空，不得用高召回放行明显无关岗位。",
                "根据地理常识判断地点；location_match只有在card.location能确认属于policy.target_cities任一目标城市时才为true。地点为空、解析不完整或无法确认归属时，必须返回false，不能进入详情。",
                "招聘者活跃状态只作记录，不能决定卡片是否通过；岗位标签缺失也不能单独作为拒绝理由。",
                "不要返回eligible字段；程序会根据job_direction_match、location_match和excluded_direction_match计算最终卡片资格。",
        )
        payload = {
            "card": self._card_payload(card),
            "policy": self._policy_payload(policy),
            "resume": resume.to_dict(),
            "resume_text": resume_text[:30000],
        }
        validation_error: ReviewError | None = None
        for attempt in range(3):
            attempt_instructions = instructions
            if validation_error is not None:
                attempt_instructions += (
                    "上一次卡片返回未通过程序校验："
                    f"{validation_error}。请重新返回完整JSON并只修正结构和字段一致性；"
                    "不得改变用户方向、排除方向或地点事实。",
                )
            response = self._exchange("card_review", attempt_instructions, payload)
            try:
                return self._parse_card_response(response, card, policy)
            except ReviewError as exc:
                validation_error = exc
        raise ReviewError(f"卡片审核连续3次未通过程序校验：{validation_error}")

    def _parse_card_response(
        self,
        response: dict[str, object],
        card: JobCard,
        policy: AutomationPolicy,
    ) -> CardReviewResult:
        job_match = self._required_bool(response, "job_direction_match")
        location_match = self._required_bool(response, "location_match")
        excluded_match = self._required_bool(response, "excluded_direction_match")
        reason = self._required_text(response, "reason")
        resume_directions = self._text_tuple(
            response, "resume_inferred_directions", allow_empty=True
        )
        combined_directions = merge_combined_directions(
            policy.allowed_job_keywords,
            resume_directions,
            self._text_tuple(response, "combined_directions"),
        )
        matched_keywords = self._text_tuple(
            response, "matched_direction_keywords", allow_empty=True
        )
        matched_excluded_keywords = self._text_tuple(
            response, "matched_excluded_direction_keywords", allow_empty=True
        )
        eligible = job_match and location_match and not excluded_match
        if job_match != bool(matched_keywords):
            raise ReviewError(
                "job_direction_match必须与matched_direction_keywords是否非空一致"
            )
        if excluded_match != bool(matched_excluded_keywords):
            raise ReviewError(
                "excluded_direction_match必须与排除方向命中关键词是否非空一致"
            )
        if not policy.excluded_job_directions and excluded_match:
            raise ReviewError("未配置排除岗位方向时不得返回排除命中")
        result = CardReviewResult(
            eligible,
            job_match,
            location_match,
            reason,
            resume_directions,
            combined_directions,
            matched_keywords,
            excluded_match,
            matched_excluded_keywords,
        )
        hard_rejection = card_review_rejection_reason(card, result)
        if hard_rejection:
            return CardReviewResult(
                False,
                job_match,
                location_match,
                hard_rejection,
                resume_directions,
                combined_directions,
                matched_keywords,
                excluded_match,
                matched_excluded_keywords,
            )
        return result

    @staticmethod
    def _chat_payload(
        conversation: ChatConversation,
        messages: tuple[ChatMessage, ...],
        system_notes: tuple[str, ...] = (),
    ) -> dict[str, object]:
        trailing_recruiter: list[str] = []
        for item in reversed(messages):
            if item.from_me:
                break
            trailing_recruiter.append(item.text)
        trailing_recruiter.reverse()
        return {
            "recruiter_name": conversation.recruiter_name,
            "company_name": conversation.company_name,
            "position_name": conversation.position_name,
            "unread_count": conversation.unread_count,
            "conversation": [
                {"speaker": "candidate" if item.from_me else "recruiter", "text": item.text}
                for item in messages
            ],
            # 只有“我方最后一次回应之后”HR 说的话才需要判断；更早的请求已经处理
            # 过，不能再次触发回复。
            "unanswered_recruiter_messages": trailing_recruiter,
            "system_notes": list(system_notes),
            "resume_already_sent": resume_already_sent(system_notes),
            "recruiter_requested_resume_card": recruiter_requested_resume_card(
                system_notes
            ),
        }

    def review_chat_message(
        self,
        conversation: ChatConversation,
        messages: tuple[ChatMessage, ...],
        *,
        system_notes: tuple[str, ...] = (),
    ) -> ChatReviewResult:
        payload = self._chat_payload(conversation, messages, system_notes)
        validation_error: ReviewError | None = None
        for _ in range(3):
            instructions = _CHAT_REVIEW_INSTRUCTIONS
            if validation_error is not None:
                instructions += (
                    f"上一次返回未通过程序校验：{validation_error}。"
                    "请重新返回完整JSON并只修正结构问题，不得改变判断标准。",
                )
            response = self._exchange("chat_review", instructions, payload)
            try:
                resume_requested = self._required_bool(response, "resume_requested")
                contact_requested = self._required_bool(
                    response, "contact_requested"
                )
                no_action_needed = self._required_bool(
                    response, "no_action_needed"
                )
                reason = self._required_text(response, "reason")
                reply = self._required_text(response, "reply")
                if no_action_needed and (resume_requested or contact_requested):
                    raise ReviewError(
                        "no_action_needed=true时不能同时索要简历或联系方式"
                    )
            except ReviewError as exc:
                validation_error = exc
                continue
            return ChatReviewResult(
                resume_requested=resume_requested,
                contact_requested=contact_requested,
                no_action_needed=no_action_needed,
                reply=(
                    validate_chat_reply(reply)
                    if resume_requested or contact_requested
                    else ""
                ),
                reason=reason,
            )
        raise ReviewError(f"消息意图审核连续3次未通过程序校验：{validation_error}")

    def _detail_instructions(self, card: JobCard) -> tuple[str, ...]:
        greeting_job_label = _greeting_job_label(card.job_name)
        return (

            "你负责完整岗位匹配和招呼语生成，代码不再进行固定打分或套用固定模板。",
            "结合岗位职责、任职要求、求职意向、结构化简历和简历原文判断；不得虚构简历经历。",
            "岗位名称已通过方向初筛。详情页阶段只允许两类拒投依据：一是完整岗位详情明确提及并实际命中policy.excluded_job_directions中的排除方向；二是详情明确提出硬性最低工作年限，且resume与resume_text明确无法满足。除此之外不得判断为不投递。",
            "少量或多数技术栈/工具/技能未覆盖、加分项缺失、学历/专业/行业背景差异、职责经验不足、泛化的‘有经验优先’以及未写明年数的经验要求，都不能单独作为详情页拒投依据。详情出现简历未提及的技术栈、工具或技能时，greeting可以写掌握或熟悉，但绝不能捏造使用该技能的项目、实习、工作、职责、成果或交付经验。",
            "年限只把最低门槛达到3年的明确硬性要求作为候选冲突，例如3-5年或3年以上；1-3年不影响投递。只有简历明确无法满足时才写入hard_experience_requirement_conflicts。没有上述两类明确冲突时，should_apply必须为true且score不得低于minimum_score。",
            "matched_detail_excluded_direction_keywords只列出详情正文逐字出现且命中的用户排除方向词；hard_experience_requirement_conflicts只逐字复制详情中的硬性最低年限原文（工作年限主要来自job_detail.fields.experience字段，也可能出现在职责正文，须原样复制该文本），简历为何不满足写入reasons。两数组均为空时should_apply必须为true；任一非空时才可为false。",
            "score为0-100，并须与should_apply和minimum_score保持一致。",
            "greeting必须使用简体中文并针对该岗位，硬范围80-150个Unicode字符是第一规则，推荐用90-120字符简洁表达；必须以“您好，我…”或“我…”等自然第一人称表达开头，全文不得出现“本人”等生硬书面称呼，不得直接以项目名、技术名或经历名词起句。Boss聊天列表只展示前20个字符，因此简短问候后应立即呈现最强相关证据。",
            "招呼语的行业属性由你依据结构化简历与简历原文自行判断：技术类简历用技术职责与技术栈表达，非技术类（如市场、运营、销售、财务、行政、人力、教育、设计、客服等）用该行业真实的职责、业绩与成果表达；用词贴合简历所属行业，不得为了套用示例把非技术经历硬写成技术经历，也不得反之。",
            f"greeting采用完整闭环：自然第一人称开场→强证据→简历真实项目/工作或职责→明确覆盖3项以上岗位核心技能（技术岗为技术栈，非技术岗为该岗位核心能力；不足3项时全部覆盖）→结尾主动写“希望与您进一步沟通{greeting_job_label}”或同义表达。结尾只能使用greeting_job_name，不得复制card.job_name中的五险一金、双休、薪资、急招等后缀。不得为了满足格式强行加入完成、交付、部署、落地、上线、达成或复盘；这些词只有在简历对同一项目或经历明确陈述时才能使用。",
            "项目经历与实习经历是独立事实区块，不得因为PDF提取后文本相邻，就把个人项目写成实习公司项目。启动部署文档只代表存在文档，测试用例只代表包含测试，不等于项目已经部署、上线或对外交付。",
            "凡是写到‘在某公司实习期间做了某项目’、‘项目已部署/交付/上线’、‘独立完成/主导’或‘在某项目中负责某事项’等高风险事实，整句必须能从resume_text中连续直接检索；不能把原文中由标点分隔的两句话重新拼成新的归属结论。无法逐句引用时改写为不带项目归属的技能和职责表达。",
            "参考的是结构而不是固定模板；专有名词（技术岗的技术名、非技术岗的工具/系统/证书/方法名）必须使用简历或岗位中的正确写法。技术岗不得为了套用示例虚构LLM、RAG、Agent、Vibe Coding等经历；非技术岗同样不得虚构未写明的项目、业绩、客户、渠道或职责。",
            "greeting必须是简体中文，无论最终是仅填充还是实际发送都直接使用它；不得返回英文或拼音版本。",
            "仅在上述两类明确冲突导致不投递时，也必须返回判断理由、任职要求概括，并将greeting填写为不发送说明。",
        )

    def _detail_payload(
        self,
        card: JobCard,
        job: JobPageData,
        policy: AutomationPolicy,
        intents: JobIntentData,
        resume: ResumeData,
        resume_text: str,
    ) -> dict[str, object]:
        return {
            "card": self._card_payload(card),
            "job_detail": job.to_dict(),
            "policy": self._policy_payload(policy),
            "job_intents": asdict(intents),
            "resume": resume.to_dict(),
            "resume_text": resume_text[:30000],
            "greeting_job_name": _greeting_job_name(card.job_name),
        }

    def review_detail(
        self,
        card: JobCard,
        job: JobPageData,
        policy: AutomationPolicy,
        intents: JobIntentData,
        resume: ResumeData,
        resume_text: str,
    ) -> DetailReviewResult:
        """详情审核；与API方式一致地重试、定向修复招呼语并本地兜底。"""

        instructions = self._detail_instructions(card)
        payload = self._detail_payload(card, job, policy, intents, resume, resume_text)
        validation_error: ReviewError | None = None
        for attempt in range(3):
            attempt_instructions = instructions
            if validation_error is not None:
                attempt_instructions += (
                    "上一次返回未通过程序硬校验："
                    f"{validation_error}。请重新生成完整JSON并严格修正该问题，"
                    "不得降低匹配标准或虚构事实。",
                )
            response = self._exchange("detail_review", attempt_instructions, payload)
            try:
                return self._parse_detail_response(
                    response, policy, resume_text, job=job, job_name=card.job_name
                )
            except (
                GreetingLengthError,
                GreetingGroundingError,
                GreetingStyleError,
            ) as exc:
                return self._repair_greeting(
                    response,
                    card=card,
                    job=job,
                    policy=policy,
                    resume_text=resume_text,
                    initial_error=exc,
                )
            except ReviewError as exc:
                validation_error = exc
                if attempt == 2:
                    raise ReviewError(f"详情返回连续三次未通过硬校验：{exc}") from exc
        raise ReviewError("详情审核未返回有效结果")

    def _parse_detail_response(
        self,
        response: dict[str, object],
        policy: AutomationPolicy,
        resume_text: str,
        *,
        job: JobPageData,
        job_name: str,
    ) -> DetailReviewResult:
        should_apply = self._required_bool(response, "should_apply")
        score = response.get("score")
        if type(score) is not int or not 0 <= score <= 100:
            raise ReviewError("审核返回字段 score 必须是0-100整数")
        if should_apply != (score >= policy.minimum_score):
            raise ReviewError("详情审核should_apply必须与minimum_score阈值一致")
        detail_excluded_keywords = (
            self._text_tuple(
                response,
                "matched_detail_excluded_direction_keywords",
                allow_empty=True,
            )
            if "matched_detail_excluded_direction_keywords" in response
            else ()
        )
        experience_conflicts = (
            self._text_tuple(
                response,
                "hard_experience_requirement_conflicts",
                allow_empty=True,
            )
            if "hard_experience_requirement_conflicts" in response
            else ()
        )
        if detail_excluded_keywords and not policy.excluded_job_directions:
            raise ReviewError("未配置排除岗位方向时，详情不得返回排除方向命中")
        policy_should_apply = not detail_excluded_keywords and not experience_conflicts
        if should_apply != policy_should_apply:
            raise ReviewError(
                "详情页只有命中排除岗位方向或存在不满足的硬性工作年限要求时才可不投递"
            )
        _validate_detail_rejection_evidence(
            job,
            policy,
            detail_excluded_keywords,
            experience_conflicts,
        )
        reasons = self._text_tuple(response, "reasons")
        matched_skills_value = response.get("matched_skills")
        if not isinstance(matched_skills_value, list) or not all(
            isinstance(item, str) and item.strip() for item in matched_skills_value
        ):
            raise ReviewError("审核返回字段 matched_skills 必须是字符串数组")
        matched_skills = tuple(item.strip() for item in matched_skills_value)
        greeting = self._required_text(response, "greeting")
        _validate_chinese_greeting(
            greeting,
            should_apply=should_apply,
            matched_skills=matched_skills,
            job_name=job_name,
        )
        _validate_greeting_grounding(
            greeting,
            should_apply=should_apply,
            resume_text=resume_text,
        )
        qualifications = self._required_text(response, "qualifications_summary")
        return DetailReviewResult(
            should_apply,
            score,
            reasons,
            matched_skills,
            greeting,
            qualifications,
            detail_excluded_keywords,
            experience_conflicts,
        )

    def _repair_greeting(
        self,
        detail_response: dict[str, object],
        *,
        card: JobCard,
        job: JobPageData,
        policy: AutomationPolicy,
        resume_text: str,
        initial_error: ReviewError,
    ) -> DetailReviewResult:
        """只修复招呼语格式与事实，保留已经完成的详情匹配决定。"""

        original_greeting = self._required_text(detail_response, "greeting")
        matched_skills = self._text_tuple(
            detail_response, "matched_skills", allow_empty=True
        )
        required_skill_count = min(3, len(matched_skills))
        normalized_original = _normalize_fact_text(original_greeting)
        present = [
            skill
            for skill in matched_skills
            if _normalize_fact_text(skill) in normalized_original
        ]
        absent = [skill for skill in matched_skills if skill not in present]
        locked_skills = tuple((present + absent)[:required_skill_count])
        locked_skills_text = "、".join(f"“{skill}”" for skill in locked_skills)
        label = _greeting_job_label(card.job_name)
        instructions = (
            "只修复既有招呼语的格式、长度和事实依据，不重新判断岗位，不修改评分、理由、匹配技能或其他字段。",
            "一次生成3份简体中文候选，长度分别约95、115、135个Unicode字符；每份都必须满足80-150字符这一第一硬规则，并保持简洁。",
            "原招呼语可能本身含有错误归属或虚构成果，不能把它当作事实来源。所有事实只能来自resume_text；原文超过150字符时删除重复修饰，候选不足80字符时只能补充resume_text中的原句。",
            "项目经历与实习经历必须分开，不得把个人项目写成实习公司项目；启动部署文档不等于项目已部署，测试用例不等于项目已交付。",
            "不得加入resume_text和matched_skills之外的新经历、新成果或新技术。",
            "招呼语的行业属性依据resume_text判断：技术类用技术职责与技术栈表达，非技术类（市场、运营、销售、财务、行政、人力、教育、设计、客服等）用该行业真实职责与成果表达，用词贴合简历。",
            "必须以“您好，我…”或“我…”等自然第一人称表达开头，全文不得出现“本人”等生硬书面称呼，不得直接以项目名、技术名或经历名词起句；问候应简短，前20个字符仍需展示最强项目、经验或能力证据。",
            "为通过前20字符硬校验，每份候选前20个字符必须自然包含“我有”“我曾”“我能”“经验”“项目”“实习”“负责”“完成”“具备”“掌握”“熟悉”“擅长”“主导”“业绩”或“成果”至少一项，且该证据必须来自resume_text。",
            "事实可对resume_text做自然改写，但严禁把实习与具体项目或成果写进同一小句，也严禁在简历未写部署、上线或交付时凭空声称项目已部署、上线或交付。",
            f"必须逐字包含这{required_skill_count}项技能：{locked_skills_text}；不得改成缩写、同义词或合并表达；使用简历可直接支持的职责证据即可，不强制使用部署或交付。",
            f"每份候选必须以“希望与您沟通{label}”或“希望与您进一步沟通{label}”结尾，不得带入原岗位名中的五险一金、双休、薪资、急招等后缀，也不得改写这两种固定句式。",
            "仅返回包含greetings字段的JSON对象，greetings必须是恰好3项的字符串数组；返回前逐份按Unicode字符计数。",
        )
        payload: dict[str, object] = {
            "job_name": card.job_name,
            "greeting_job_name": _greeting_job_name(card.job_name),
            "original_greeting": original_greeting,
            "original_length": len(original_greeting),
            "matched_skills": list(matched_skills),
            "required_verbatim_skills": list(locked_skills),
            "resume_text": resume_text[:30000],
        }
        validation_error: ReviewError = initial_error
        for attempt in range(3):
            attempt_instructions = instructions + (
                "上一版结果未通过程序硬校验："
                f"{validation_error}。低于80字符时必须补充原文事实，"
                "高于150字符时才继续压缩；不得新增事实。",
            )
            rewrite_response = self._exchange(
                "greeting_rewrite", attempt_instructions, payload
            )
            try:
                candidates = self._text_tuple(rewrite_response, "greetings")
            except ReviewError as exc:
                validation_error = exc
                candidates = ()
            if len(candidates) != 3:
                validation_error = ReviewError("greetings必须恰好包含3份候选招呼语")
            candidate_errors: list[str] = []
            for rewritten in candidates if len(candidates) == 3 else ():
                candidate = dict(detail_response)
                candidate["greeting"] = rewritten
                try:
                    return self._parse_detail_response(
                        candidate, policy, resume_text, job=job, job_name=card.job_name
                    )
                except GreetingLengthError as exc:
                    adjusted = dict(candidate)
                    adjusted["greeting"] = (
                        _safely_expand_short_greeting(rewritten, resume_text)
                        if exc.actual_length < _GREETING_MIN_LENGTH
                        else _safely_compact_greeting(rewritten)
                    )
                    try:
                        return self._parse_detail_response(
                            adjusted,
                            policy,
                            resume_text,
                            job=job,
                            job_name=card.job_name,
                        )
                    except ReviewError as inner:
                        candidate_errors.append(str(inner))
                except ReviewError as exc:
                    candidate_errors.append(str(exc))
            if candidate_errors:
                validation_error = ReviewError(
                    "3份候选均未通过：" + "；".join(candidate_errors)
                )
            payload["previous_greetings"] = list(candidates)
            payload["previous_lengths"] = [len(item) for item in candidates]
            if attempt == 2:
                fallback = self._grounded_greeting_fallback(
                    detail_response,
                    card=card,
                    job=job,
                    policy=policy,
                    resume_text=resume_text,
                    matched_skills=matched_skills,
                )
                if fallback is not None:
                    return fallback
                raise GreetingRepairError(
                    f"招呼语修复连续三次未通过硬校验：{validation_error}",
                    failed_greeting=original_greeting,
                ) from validation_error
        raise ReviewError("招呼语修复未返回有效结果")

    def _grounded_greeting_fallback(
        self,
        detail_response: dict[str, object],
        *,
        card: JobCard,
        job: JobPageData,
        policy: AutomationPolicy,
        resume_text: str,
        matched_skills: tuple[str, ...],
    ) -> DetailReviewResult | None:
        """用简历原文本地合成招呼语兜底；仍过不了同一套硬校验时返回 None。"""

        greeting = _synthesize_grounded_greeting(
            resume_text, matched_skills, card.job_name
        )
        if greeting is None:
            return None
        candidate = dict(detail_response)
        candidate["greeting"] = greeting
        try:
            return self._parse_detail_response(
                candidate, policy, resume_text, job=job, job_name=card.job_name
            )
        except ReviewError:
            return None


class CodexCliReviewProvider(ManualFileReviewProvider):
    """通过本机已登录的Codex CLI自动完成文件协议审核，不再等待外部人工响应。"""

    def __init__(
        self,
        directory: str | Path = "data/manual_reviews",
        *,
        workspace: str | Path = ".",
        timeout_seconds: float = 240.0,
        poll_interval_seconds: float = 0.1,
        output: Callable[[str], None] = print,
        request_id_factory: Callable[[], str] | None = None,
        control_point: Callable[[], None] | None = None,
        codex_path: str | Path | None = None,
        model: str | None = None,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ) -> None:
        super().__init__(
            directory,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            output=output,
            request_id_factory=request_id_factory,
            control_point=control_point,
        )
        located = str(codex_path) if codex_path else shutil.which("codex")
        if not located:
            raise ReviewError(
                "Codex主导模式需要本机Codex CLI，但当前未找到codex命令；"
                "请安装并登录Codex CLI，或切换至大模型API"
            )
        self.codex_path = str(Path(located).resolve())
        # 固定使用 gpt-5.5：更新的 gpt-5.6 系列虽然会出现在模型缓存里，但通过 CLI
        # 调用会被服务端以“requires a newer version of Codex”拒绝。若用户 Codex 端
        # 选的不是 gpt-5.5，这里给出明确的切换提示。
        self.model = model or CODEX_REQUIRED_MODEL
        configured = self._configured_model()
        if configured and configured != self.model:
            self.output(
                f"请将codex端的模型切换至{self.model}使用（当前Codex端配置为{configured}）"
            )
        self.workspace = Path(workspace).resolve()
        self.process_factory = process_factory

    @staticmethod
    def _configured_model() -> str | None:
        """读取 $CODEX_HOME/config.toml 中用户当前选用的模型。"""

        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        try:
            with (codex_home / "config.toml").open("rb") as handle:
                value = tomllib.load(handle).get("model")
        except (OSError, tomllib.TOMLDecodeError, ValueError):
            return None
        return value.strip() if isinstance(value, str) and value.strip() else None

    @staticmethod
    def _cached_compatible_model() -> str:
        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
        cache_path = codex_home / "models_cache.json"
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            models = payload.get("models") if isinstance(payload, dict) else None
            if isinstance(models, list):
                for item in models:
                    if (
                        isinstance(item, dict)
                        and isinstance(item.get("slug"), str)
                        and item.get("visibility") == "list"
                    ):
                        return str(item["slug"])
        except (OSError, json.JSONDecodeError):
            pass
        return "gpt-5.5"

    @staticmethod
    def _response_schema(kind: str, request_id: str) -> dict[str, object]:
        string_array = {"type": "array", "items": {"type": "string"}}
        if kind == "card_review":
            properties: dict[str, object] = {
                "request_id": {"type": "string", "const": request_id},
                "kind": {"type": "string", "const": kind},
                "job_direction_match": {"type": "boolean"},
                "location_match": {"type": "boolean"},
                "excluded_direction_match": {"type": "boolean"},
                "resume_inferred_directions": string_array,
                "combined_directions": {**string_array, "minItems": 1},
                "matched_direction_keywords": string_array,
                "matched_excluded_direction_keywords": string_array,
                "reason": {"type": "string", "minLength": 1},
            }
        elif kind == "detail_review":
            properties = {
                "request_id": {"type": "string", "const": request_id},
                "kind": {"type": "string", "const": kind},
                "should_apply": {"type": "boolean"},
                "score": {"type": "integer", "minimum": 0, "maximum": 100},
                "reasons": {**string_array, "minItems": 1},
                "matched_skills": string_array,
                "matched_detail_excluded_direction_keywords": string_array,
                "hard_experience_requirement_conflicts": string_array,
                "greeting": {"type": "string", "minLength": 1},
                "qualifications_summary": {"type": "string", "minLength": 1},
            }
        elif kind == "greeting_rewrite":
            # 招呼语定向修复：只回 3 份候选，详情判断沿用上一轮结果。
            properties = {
                "request_id": {"type": "string", "const": request_id},
                "kind": {"type": "string", "const": kind},
                "greetings": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 3,
                    "maxItems": 3,
                },
            }
        elif kind == "chat_review":
            # HR 未读消息意图：区分简历请求和联系方式请求，避免误点同名按钮。
            properties = {
                "request_id": {"type": "string", "const": request_id},
                "kind": {"type": "string", "const": kind},
                "resume_requested": {"type": "boolean"},
                "contact_requested": {"type": "boolean"},
                "no_action_needed": {"type": "boolean"},
                "reply": {"type": "string", "minLength": 1},
                "reason": {"type": "string", "minLength": 1},
            }
        else:  # pragma: no cover - 仅由公开审核方法调用
            raise ReviewError(f"未知Codex审核类型：{kind}")
        return {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }

    def _exchange(
        self,
        kind: str,
        instructions: tuple[str, ...],
        payload: dict[str, object],
    ) -> dict[str, object]:
        self.directory.mkdir(parents=True, exist_ok=True)
        request_id = self.request_id_factory()
        prefix = self.directory / request_id
        request_path = prefix.with_suffix(".request.json")
        response_path = prefix.with_suffix(".response.json")
        schema_path = prefix.with_suffix(".schema.json")
        log_path = prefix.with_suffix(".codex.log")
        request = {
            "schema_version": 1,
            "request_id": request_id,
            "kind": kind,
            "instructions": list(instructions),
            "payload": payload,
        }
        request_path.write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        schema_path.write_text(
            json.dumps(
                self._response_schema(kind, request_id),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        prompt = (
            "你只负责完成下面的岗位审核请求。严格遵循instructions，仅根据payload判断；"
            "不得读取其他文件、运行命令、修改项目或操作设备。"
            "最终只返回符合所给JSON Schema的JSON对象。\n\n"
            + json.dumps(request, ensure_ascii=False)
        )
        command = [
            self.codex_path,
            "exec",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--model",
            self.model,
            "--config",
            f'model="{self.model}"',
            "--config",
            'model_reasoning_effort="low"',
            "--cd",
            str(self.workspace),
            "--output-schema",
            str(schema_path.resolve()),
            "--output-last-message",
            str(response_path.resolve()),
            "-",
        ]
        card_payload = payload.get("card")
        subject = (
            card_payload.get("job_name", kind)
            if isinstance(card_payload, dict)
            else payload.get("recruiter_name") or payload.get("job_name") or kind
        )
        self.output(f"Codex正在自动审核：{subject}")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with log_path.open("w", encoding="utf-8") as log_file:
            try:
                process = self.process_factory(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    cwd=self.workspace,
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise ReviewError(f"无法启动Codex CLI：{exc}") from exc
            assert process.stdin is not None
            process.stdin.write(prompt)
            process.stdin.close()
            deadline = time.monotonic() + self.timeout_seconds
            while process.poll() is None:
                before = time.monotonic()
                try:
                    if self.control_point:
                        self.control_point()
                except BaseException:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise
                # 暂停期间 control_point 会阻塞在这里等“继续”，这段时长不该计入审核
                # 超时——否则暂停久了一继续就报“Codex自动审核超过N秒”。把明显的阻塞
                # （>0.5s，正常轮询检查耗时可忽略）从 deadline 里顺延掉，相当于暂停时
                # 审核计时也一起暂停。
                paused_for = time.monotonic() - before
                if paused_for > 0.5:
                    deadline += paused_for
                if time.monotonic() >= deadline:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise ReviewError(
                        f"Codex自动审核超过{self.timeout_seconds:g}秒，已终止：{request_path.resolve()}"
                    )
                time.sleep(self.poll_interval_seconds)
            return_code = process.returncode
        if return_code != 0:
            try:
                diagnostic = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
            except OSError:
                diagnostic = ""
            raise ReviewError(
                f"Codex自动审核失败（退出码{return_code}）：{diagnostic or log_path.resolve()}"
            )
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReviewError(f"Codex审核结果无法解析：{response_path.resolve()}：{exc}") from exc
        if not isinstance(response, dict):
            raise ReviewError("Codex审核返回必须是JSON对象")
        if response.get("request_id") != request_id or response.get("kind") != kind:
            raise ReviewError("Codex审核返回的request_id或kind与请求不一致")
        self.output("Codex审核完成")
        return response
