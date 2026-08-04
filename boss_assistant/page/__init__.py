"""职位详情页数据模型与快照保存能力（设备无关）。"""

from .reader import (
    AccessibleText,
    ArtifactSaveError,
    FieldStatus,
    JobField,
    JobPageData,
    NodeSample,
    NotBossJobDetailPageError,
    PageReadError,
    PageSnapshot,
    SavedPageArtifacts,
    build_job_page_data,
    make_field,
    save_page_artifacts,
)

__all__ = [
    "AccessibleText",
    "ArtifactSaveError",
    "FieldStatus",
    "JobField",
    "JobPageData",
    "NodeSample",
    "NotBossJobDetailPageError",
    "PageReadError",
    "PageSnapshot",
    "SavedPageArtifacts",
    "build_job_page_data",
    "make_field",
    "save_page_artifacts",
]
