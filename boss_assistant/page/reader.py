"""Boss职位详情页的结构化数据模型与快照保存。

Windows/Web 端不再从 Android uiautomator 的 XML 层级解析，而是由
``boss_assistant.web.page_reader`` 直接从 DOM 读取字段并构造 ``JobPageData``。
本模块只保留与设备无关的数据模型、快照容器和落盘能力，确保 ``storage``、``review``、
``api_provider`` 等复用模块无需改动即可继续依赖同一套 ``JobPageData`` 接口。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


WEB_SOURCE_HOST = "www.zhipin.com"
WEB_JOB_DETAIL_MARKER = "web/geek/job-detail"


class PageReadError(RuntimeError):
    """当前页面无法获取、确认或解析。"""


class NotBossJobDetailPageError(PageReadError):
    """当前不是 Boss直聘 职位详情页。"""

    def __init__(self, current_url: str) -> None:
        self.current_url = current_url
        super().__init__(
            "当前不是 Boss直聘 职位详情页，已停止读取。\n"
            f"当前地址：{current_url}"
        )


class ArtifactSaveError(RuntimeError):
    """页面快照文件无法保存。"""


class FieldStatus(str, Enum):
    """结构化字段的读取状态。"""

    FOUND = "found"
    NOT_DISPLAYED = "not_displayed"
    RECOGNITION_FAILED = "recognition_failed"


@dataclass(frozen=True)
class AccessibleText:
    value: str
    source: str
    resource_id: str
    class_name: str


@dataclass(frozen=True)
class NodeSample:
    """用于诊断展示的代表性节点属性。"""

    node_index: int
    attributes: dict[str, str]


@dataclass(frozen=True)
class JobField:
    """单个职位字段的值、状态及识别依据。"""

    value: str | None
    status: FieldStatus
    reason: str
    source: str | None = None
    resource_id: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "value": self.value,
            "status": self.status.value,
            "reason": self.reason,
            "source": self.source,
            "resource_id": self.resource_id,
        }


_UNREAD_EXPERIENCE = JobField(
    value=None,
    status=FieldStatus.NOT_DISPLAYED,
    reason="field_not_in_current_page",
)


@dataclass(frozen=True)
class JobPageData:
    """Boss职位详情页统一结构化数据模型。"""

    captured_at: str
    foreground_activity: str
    is_boss_job_detail_page: bool
    hierarchy_packages: tuple[str, ...]
    job_name: JobField
    company_name: JobField
    salary: JobField
    location: JobField
    job_description: JobField
    node_count: int
    accessible_text_count: int
    experience: JobField = _UNREAD_EXPERIENCE

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "captured_at": self.captured_at,
            "page": {
                "foreground_activity": self.foreground_activity,
                "is_boss_job_detail_page": self.is_boss_job_detail_page,
                "hierarchy_packages": list(self.hierarchy_packages),
            },
            "fields": {
                "job_name": self.job_name.to_dict(),
                "company_name": self.company_name.to_dict(),
                "salary": self.salary.to_dict(),
                "location": self.location.to_dict(),
                "job_description": self.job_description.to_dict(),
                "experience": self.experience.to_dict(),
            },
            "diagnostics": {
                "node_count": self.node_count,
                "accessible_text_count": self.accessible_text_count,
            },
        }


@dataclass(frozen=True)
class PageSnapshot:
    raw_xml: str  # Web 端存储详情页的可见文本 / HTML 片段，字段名沿用以复用存储层。
    texts: list[AccessibleText]
    node_count: int
    node_samples: list[NodeSample]
    job_data: JobPageData

    @property
    def job_name(self) -> str | None:
        return self.job_data.job_name.value

    @property
    def company_name(self) -> str | None:
        return self.job_data.company_name.value

    @property
    def salary(self) -> str | None:
        return self.job_data.salary.value

    @property
    def location(self) -> str | None:
        return self.job_data.location.value

    @property
    def job_description(self) -> str | None:
        return self.job_data.job_description.value


@dataclass(frozen=True)
class SavedPageArtifacts:
    raw_xml_path: Path
    structured_json_path: Path


def _found(value: str, *, source: str = "dom", reason: str = "matched_dom_selector") -> JobField:
    return JobField(value=value, status=FieldStatus.FOUND, reason=reason, source=source)


def _missing(reason: str = "field_not_in_current_page") -> JobField:
    return JobField(value=None, status=FieldStatus.NOT_DISPLAYED, reason=reason)


def make_field(value: str | None, *, source: str = "dom") -> JobField:
    """根据是否读到值构造 FOUND / NOT_DISPLAYED 字段。"""

    cleaned = re.sub(r"[ \t　]+", " ", value or "").strip()
    if cleaned:
        return _found(cleaned, source=source)
    return _missing()


def build_job_page_data(
    *,
    current_url: str,
    job_name: str | None,
    company_name: str | None,
    salary: str | None,
    location: str | None,
    job_description: str | None,
    experience: str | None,
    is_detail_page: bool,
    node_count: int,
    accessible_text_count: int,
    captured_at: str | None = None,
) -> JobPageData:
    """Web 端从 DOM 读到的字段 → 统一结构化模型。"""

    return JobPageData(
        captured_at=captured_at or datetime.now(timezone.utc).isoformat(),
        foreground_activity=current_url,
        is_boss_job_detail_page=is_detail_page
        and bool(job_name and job_name.strip())
        and bool(company_name and company_name.strip()),
        hierarchy_packages=(WEB_SOURCE_HOST,),
        job_name=make_field(job_name),
        company_name=make_field(company_name),
        salary=make_field(salary),
        location=make_field(location),
        job_description=make_field(job_description),
        experience=make_field(experience),
        node_count=node_count,
        accessible_text_count=accessible_text_count,
    )


def save_page_artifacts(
    snapshot: PageSnapshot,
    output_dir: str | Path,
    *,
    stem: str | None = None,
) -> SavedPageArtifacts:
    """把原始详情文本/HTML 与结构化 JSON 成对保存到本地。"""

    directory = Path(output_dir).expanduser()
    if stem is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        stem = f"boss_job_page_{timestamp}"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", stem):
        raise ArtifactSaveError("保存文件名只能包含字母、数字、点、下划线和连字符。")

    raw_path = directory / f"{stem}.html"
    structured_json_path = directory / f"{stem}.json"
    raw_temp = directory / f".{stem}.html.tmp"
    structured_json_temp = directory / f".{stem}.json.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if raw_path.exists() or structured_json_path.exists():
            raise ArtifactSaveError(f"保存目标已存在，未覆盖：{stem}")
        raw_temp.write_text(snapshot.raw_xml + "\n", encoding="utf-8")
        structured_json_temp.write_text(
            json.dumps(snapshot.job_data.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        raw_temp.replace(raw_path)
        structured_json_temp.replace(structured_json_path)
    except ArtifactSaveError:
        raise
    except OSError as exc:
        raw_path.unlink(missing_ok=True)
        structured_json_path.unlink(missing_ok=True)
        raise ArtifactSaveError(f"页面快照保存失败：{exc}") from exc
    finally:
        raw_temp.unlink(missing_ok=True)
        structured_json_temp.unlink(missing_ok=True)
    return SavedPageArtifacts(raw_path.resolve(), structured_json_path.resolve())
