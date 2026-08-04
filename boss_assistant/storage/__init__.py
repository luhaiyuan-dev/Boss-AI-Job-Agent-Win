"""本地岗位数据管理接口。"""

from .repository import (
    JobSearchResult,
    JobStore,
    JobStoreError,
    SaveAction,
    SaveResult,
    StoredJob,
    build_content_hash,
    build_job_fingerprint,
)

__all__ = [
    "JobSearchResult",
    "JobStore",
    "JobStoreError",
    "SaveAction",
    "SaveResult",
    "StoredJob",
    "build_content_hash",
    "build_job_fingerprint",
]
