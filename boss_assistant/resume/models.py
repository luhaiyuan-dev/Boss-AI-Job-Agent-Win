"""本地简历结构化数据模型。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContactInfo:
    phone: str | None = None
    email: str | None = None
    wechat: str | None = None


@dataclass(frozen=True)
class BasicInfo:
    name: str | None
    contact: ContactInfo
    city: str | None


@dataclass(frozen=True)
class EducationEntry:
    school: str | None
    major: str | None
    degree: str | None
    time: str | None


@dataclass(frozen=True)
class SkillSet:
    programming_languages: tuple[str, ...] = ()
    technical_stack: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    other: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectExperience:
    name: str | None
    description: str | None
    technologies: tuple[str, ...]
    time: str | None = None


@dataclass(frozen=True)
class InternshipExperience:
    company: str | None
    position: str | None
    description: str | None
    time: str | None = None


@dataclass(frozen=True)
class WorkExperience:
    """社招/在职经历；与实习分开，供非应届简历结构化使用。"""

    company: str | None
    position: str | None
    description: str | None
    time: str | None = None


@dataclass(frozen=True)
class ResumeData:
    """供后续本地岗位匹配使用的确定性简历结构。"""

    source_pdf_path: str
    parsed_at: str
    basic_info: BasicInfo
    education: tuple[EducationEntry, ...]
    skills: SkillSet
    projects: tuple[ProjectExperience, ...]
    internships: tuple[InternshipExperience, ...]
    awards: tuple[str, ...]
    certificates: tuple[str, ...]
    self_evaluation: str | None
    work_experience: tuple[WorkExperience, ...] = ()
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_pdf_path": self.source_pdf_path,
            "parsed_at": self.parsed_at,
            "basic_info": {
                "name": self.basic_info.name,
                "contact": {
                    "phone": self.basic_info.contact.phone,
                    "email": self.basic_info.contact.email,
                    "wechat": self.basic_info.contact.wechat,
                },
                "city": self.basic_info.city,
            },
            "education": [
                {
                    "school": item.school,
                    "major": item.major,
                    "degree": item.degree,
                    "time": item.time,
                }
                for item in self.education
            ],
            "skills": {
                "programming_languages": list(self.skills.programming_languages),
                "technical_stack": list(self.skills.technical_stack),
                "tools": list(self.skills.tools),
                "other": list(self.skills.other),
            },
            "projects": [
                {
                    "name": item.name,
                    "description": item.description,
                    "technologies": list(item.technologies),
                    "time": item.time,
                }
                for item in self.projects
            ],
            "internships": [
                {
                    "company": item.company,
                    "position": item.position,
                    "description": item.description,
                    "time": item.time,
                }
                for item in self.internships
            ],
            "work_experience": [
                {
                    "company": item.company,
                    "position": item.position,
                    "description": item.description,
                    "time": item.time,
                }
                for item in self.work_experience
            ],
            "awards": list(self.awards),
            "certificates": list(self.certificates),
            "self_evaluation": self.self_evaluation,
        }
