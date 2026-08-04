"""不依赖大模型的确定性简历文本解析器。"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .models import (
    BasicInfo,
    ContactInfo,
    EducationEntry,
    InternshipExperience,
    ProjectExperience,
    ResumeData,
    SkillSet,
    WorkExperience,
)


_SECTION_ALIASES = {
    "基本信息": "basic",
    "个人信息": "basic",
    "basicinformation": "basic",
    "教育背景": "education",
    "教育经历": "education",
    "education": "education",
    "专业技能": "skills",
    "技能清单": "skills",
    "技能": "skills",
    "skills": "skills",
    "项目经历": "projects",
    "项目经验": "projects",
    "projects": "projects",
    "实习经历": "internships",
    "实习经验": "internships",
    "internships": "internships",
    "工作经历": "work",
    "工作经验": "work",
    "职业经历": "work",
    "工作背景": "work",
    "workexperience": "work",
    "获奖经历": "awards",
    "荣誉奖项": "awards",
    "奖项": "awards",
    "awards": "awards",
    "证书": "certificates",
    "证书资质": "certificates",
    "certificates": "certificates",
    "自我评价": "self_evaluation",
    "个人评价": "self_evaluation",
    "个人总结": "self_evaluation",
    "selfevaluation": "self_evaluation",
}


def _heading_key(line: str) -> str | None:
    candidate = re.sub(r"[\s:：#]+", "", line.strip("-—_*•● ")).casefold()
    return _SECTION_ALIASES.get(candidate)


def _sections(text: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {"preamble": []}
    current = "preamble"
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        heading = _heading_key(line)
        if heading:
            current = heading
            result.setdefault(current, [])
        else:
            result.setdefault(current, []).append(line)
    return result


def _label_value(lines: list[str], labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"^\s*(?:{label_pattern})\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
    for line in lines:
        match = pattern.match(line)
        if match:
            value = match.group(1).strip()
            if value:
                return value
    return None


def _all_label_values(lines: list[str], labels: tuple[str, ...]) -> list[str]:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(rf"^\s*(?:{label_pattern})\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)
    return [match.group(1).strip() for line in lines if (match := pattern.match(line))]


def _split_labeled_blocks(lines: list[str], start_labels: tuple[str, ...]) -> list[str]:
    start_pattern = re.compile(
        rf"^\s*(?:{'|'.join(re.escape(label) for label in start_labels)})\s*[:：]",
        re.IGNORECASE,
    )
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if start_pattern.match(line):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def _block_value(block: str, labels: tuple[str, ...], all_labels: tuple[str, ...]) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(label) for label in all_labels)
    match = re.search(
        rf"(?ms)^\s*(?:{label_pattern})\s*[:：]\s*(.*?)"
        rf"(?=^\s*(?:{stop_pattern})\s*[:：]|\Z)",
        block,
        re.IGNORECASE,
    )
    if not match:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip()
    return value or None


def _unique(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        key = cleaned.casefold()
        if cleaned and key not in seen:
            result.append(cleaned)
            seen.add(key)
    return tuple(result)


def _split_items(values: list[str]) -> tuple[str, ...]:
    items: list[str] = []
    for value in values:
        items.extend(re.split(r"[、,，;；/|]+", value))
    return _unique(items)


def _clean_list_lines(lines: list[str], labels: tuple[str, ...]) -> tuple[str, ...]:
    label_pattern = "|".join(re.escape(label) for label in labels)
    result: list[str] = []
    for line in lines:
        cleaned = re.sub(
            r"^\s*(?:(?:[-—_*•●·]+)|(?:\d+[.)、]))\s*",
            "",
            line,
        ).strip()
        cleaned = re.sub(rf"^(?:{label_pattern})\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
        if cleaned:
            result.append(cleaned)
    return _unique(result)


def _parse_education(lines: list[str]) -> tuple[EducationEntry, ...]:
    all_labels = ("学校", "院校", "专业", "学历", "学位", "时间", "就读时间", "教育时间")
    entries: list[EducationEntry] = []
    for block in _split_labeled_blocks(lines, ("学校", "院校")):
        entry = EducationEntry(
            school=_block_value(block, ("学校", "院校"), all_labels),
            major=_block_value(block, ("专业",), all_labels),
            degree=_block_value(block, ("学历", "学位"), all_labels),
            time=_block_value(block, ("时间", "就读时间", "教育时间"), all_labels),
        )
        if any((entry.school, entry.major, entry.degree, entry.time)):
            entries.append(entry)
    return tuple(entries)


def _parse_projects(lines: list[str]) -> tuple[ProjectExperience, ...]:
    all_labels = (
        "项目名称",
        "项目描述",
        "项目内容",
        "项目职责",
        "使用技术",
        "技术栈",
        "项目时间",
        "时间",
    )
    projects: list[ProjectExperience] = []
    for block in _split_labeled_blocks(lines, ("项目名称",)):
        project = ProjectExperience(
            name=_block_value(block, ("项目名称",), all_labels),
            description=_block_value(
                block, ("项目描述", "项目内容", "项目职责"), all_labels
            ),
            technologies=_split_items(
                [
                    value
                    for value in (
                        _block_value(block, ("使用技术", "技术栈"), all_labels),
                    )
                    if value
                ]
            ),
            time=_block_value(block, ("项目时间", "时间"), all_labels),
        )
        if any((project.name, project.description, project.technologies, project.time)):
            projects.append(project)
    return tuple(projects)


def _parse_internships(lines: list[str]) -> tuple[InternshipExperience, ...]:
    all_labels = (
        "公司",
        "实习公司",
        "岗位",
        "职位",
        "工作内容",
        "工作职责",
        "实习内容",
        "实习时间",
        "时间",
    )
    internships: list[InternshipExperience] = []
    for block in _split_labeled_blocks(lines, ("公司", "实习公司")):
        internship = InternshipExperience(
            company=_block_value(block, ("公司", "实习公司"), all_labels),
            position=_block_value(block, ("岗位", "职位"), all_labels),
            description=_block_value(
                block, ("工作内容", "工作职责", "实习内容"), all_labels
            ),
            time=_block_value(block, ("实习时间", "时间"), all_labels),
        )
        if any((internship.company, internship.position, internship.description, internship.time)):
            internships.append(internship)
    return tuple(internships)


def _parse_work_experience(lines: list[str]) -> tuple[WorkExperience, ...]:
    all_labels = (
        "公司",
        "单位",
        "工作单位",
        "岗位",
        "职位",
        "工作内容",
        "工作职责",
        "主要职责",
        "工作时间",
        "在职时间",
        "任职时间",
        "时间",
    )
    experiences: list[WorkExperience] = []
    for block in _split_labeled_blocks(lines, ("公司", "单位", "工作单位")):
        experience = WorkExperience(
            company=_block_value(block, ("公司", "单位", "工作单位"), all_labels),
            position=_block_value(block, ("岗位", "职位"), all_labels),
            description=_block_value(
                block, ("工作内容", "工作职责", "主要职责"), all_labels
            ),
            time=_block_value(
                block, ("工作时间", "在职时间", "任职时间", "时间"), all_labels
            ),
        )
        if any(
            (
                experience.company,
                experience.position,
                experience.description,
                experience.time,
            )
        ):
            experiences.append(experience)
    return tuple(experiences)


def parse_resume_text(
    text: str,
    source_pdf_path: str | Path,
    *,
    parsed_at: str | None = None,
) -> ResumeData:
    """只解析明确标题、标签和联系方式；不存在的字段保持空值。"""

    sections = _sections(text)
    basic_lines = sections.get("preamble", []) + sections.get("basic", [])
    explicit_phone = _label_value(basic_lines, ("电话", "手机", "联系电话", "Phone"))
    explicit_email = _label_value(basic_lines, ("邮箱", "电子邮箱", "Email"))
    phone_match = re.search(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", text)
    email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text, re.IGNORECASE)

    basic_info = BasicInfo(
        name=_label_value(basic_lines, ("姓名", "Name")),
        contact=ContactInfo(
            phone=explicit_phone or (phone_match.group(0) if phone_match else None),
            email=explicit_email or (email_match.group(0) if email_match else None),
            wechat=_label_value(basic_lines, ("微信", "微信号", "WeChat")),
        ),
        city=_label_value(basic_lines, ("所在城市", "现居城市", "现居住地", "城市", "City")),
    )

    skill_lines = sections.get("skills", [])
    skills = SkillSet(
        programming_languages=_split_items(
            _all_label_values(skill_lines, ("编程语言", "Programming Languages"))
        ),
        technical_stack=_split_items(
            _all_label_values(skill_lines, ("技术栈", "框架", "Technical Stack"))
        ),
        tools=_split_items(_all_label_values(skill_lines, ("工具", "开发工具", "Tools"))),
        other=_split_items(_all_label_values(skill_lines, ("其他技能", "技能", "Other Skills"))),
    )

    self_evaluation_lines = sections.get("self_evaluation", [])
    self_evaluation = " ".join(self_evaluation_lines).strip() or None
    return ResumeData(
        source_pdf_path=str(Path(source_pdf_path).expanduser().resolve()),
        parsed_at=parsed_at or datetime.now(timezone.utc).isoformat(),
        basic_info=basic_info,
        education=_parse_education(sections.get("education", [])),
        skills=skills,
        projects=_parse_projects(sections.get("projects", [])),
        internships=_parse_internships(sections.get("internships", [])),
        work_experience=_parse_work_experience(sections.get("work", [])),
        awards=_clean_list_lines(sections.get("awards", []), ("获奖", "奖项")),
        certificates=_clean_list_lines(sections.get("certificates", []), ("证书",)),
        self_evaluation=self_evaluation,
    )
