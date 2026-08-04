"""基于 SQLite 的本地岗位存储、去重和查询。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from boss_assistant.page import (
    ArtifactSaveError,
    FieldStatus,
    JobField,
    JobPageData,
    PageSnapshot,
    save_page_artifacts,
)


SCHEMA_VERSION = 1


class JobStoreError(RuntimeError):
    """岗位数据库初始化、保存或查询失败。"""


class SaveAction(str, Enum):
    """一次岗位采集对主岗位记录产生的结果。"""

    INSERTED = "inserted"
    EXISTING = "existing"
    UPDATED = "updated"


@dataclass(frozen=True)
class StoredJob:
    id: int
    fingerprint: str
    job_name: str | None
    company_name: str | None
    salary: str | None
    location: str | None
    job_description: str | None
    source_package: str
    source_activity: str
    created_at: str
    updated_at: str
    last_seen_at: str
    access_count: int
    raw_xml_path: str
    structured_json_path: str


@dataclass(frozen=True)
class SaveResult:
    action: SaveAction
    job: StoredJob
    snapshot_saved: bool


@dataclass(frozen=True)
class JobSearchResult:
    job: StoredJob
    matched_fields: tuple[str, ...]


def _normalize_identity(value: str | None) -> str:
    """统一全半角、大小写和空白，避免显示差异造成重复记录。"""

    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(normalized.split())


def build_job_fingerprint(data: JobPageData) -> str:
    """以公司、职位、地点作为稳定身份；字段缺失时加入描述降低误合并。"""

    company = _normalize_identity(data.company_name.value)
    job_name = _normalize_identity(data.job_name.value)
    location = _normalize_identity(data.location.value)
    identity: dict[str, str] = {
        "company_name": company,
        "job_name": job_name,
        "location": location,
    }
    if not company or not job_name or not location:
        identity["description"] = _normalize_identity(data.job_description.value)
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_content_hash(data: JobPageData) -> str:
    """判断同一岗位的可见内容是否变化，不把采集时间计入内容。"""

    fields = {
        name: {"value": field.value, "status": field.status.value}
        for name, field in (
            ("job_name", data.job_name),
            ("company_name", data.company_name),
            ("salary", data.salary),
            ("location", data.location),
            ("job_description", data.job_description),
        )
    }
    canonical = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class JobStore:
    """单文件岗位库；每个方法使用短连接，适合本地命令行采集。"""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser()
        try:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()
        except (OSError, sqlite3.Error) as exc:
            raise JobStoreError(f"岗位数据库初始化失败：{exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            with connection:
                current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current_version > SCHEMA_VERSION:
                    raise JobStoreError(
                        f"数据库版本 {current_version} 高于程序支持版本 {SCHEMA_VERSION}"
                    )
                connection.executescript(
                    """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    content_hash TEXT NOT NULL,
                    job_name TEXT,
                    company_name TEXT,
                    salary TEXT,
                    location TEXT,
                    job_description TEXT,
                    source_package TEXT NOT NULL,
                    source_activity TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 1 CHECK (access_count >= 1),
                    raw_xml_path TEXT NOT NULL,
                    structured_json_path TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    raw_xml_path TEXT NOT NULL,
                    structured_json_path TEXT NOT NULL,
                    UNIQUE (job_id, content_hash)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_last_seen
                    ON jobs(last_seen_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_name_company
                    ON jobs(job_name, company_name);
                CREATE INDEX IF NOT EXISTS idx_snapshots_job
                    ON job_snapshots(job_id, captured_at DESC);
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @staticmethod
    def _source_package(activity: str) -> str:
        return activity.split("/", 1)[0] if "/" in activity else activity

    def _stored_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.database_path.parent.resolve()).as_posix()
        except ValueError:
            return str(resolved)

    def resolve_stored_path(self, stored_path: str) -> Path:
        """把数据库中的可迁移相对路径还原为当前机器上的完整路径。"""

        path = Path(stored_path)
        if path.is_absolute():
            return path
        return (self.database_path.parent / path).resolve()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> StoredJob:
        return StoredJob(
            id=row["id"],
            fingerprint=row["fingerprint"],
            job_name=row["job_name"],
            company_name=row["company_name"],
            salary=row["salary"],
            location=row["location"],
            job_description=row["job_description"],
            source_package=row["source_package"],
            source_activity=row["source_activity"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_seen_at=row["last_seen_at"],
            access_count=row["access_count"],
            raw_xml_path=row["raw_xml_path"],
            structured_json_path=row["structured_json_path"],
        )

    @staticmethod
    def _latest_value(field: JobField, previous: str | None) -> str | None:
        # 页面本次未显示或识别失败时，保留已经采集到的可靠旧值。
        if field.status is FieldStatus.FOUND and field.value:
            return field.value
        return previous

    def save_snapshot(
        self,
        snapshot: PageSnapshot,
        artifact_directory: str | Path,
    ) -> SaveResult:
        """保存新岗位或更新访问记录；相同内容不会重复生成快照文件。"""

        data = snapshot.job_data
        if not data.is_boss_job_detail_page:
            raise JobStoreError("拒绝保存：结构化数据不是 Boss职位详情页")
        if (
            data.job_name.status is not FieldStatus.FOUND
            or not data.job_name.value
            or data.company_name.status is not FieldStatus.FOUND
            or not data.company_name.value
        ):
            raise JobStoreError("拒绝保存：职位名称和公司名称必须均已可靠识别")

        fingerprint = build_job_fingerprint(data)
        content_hash = build_content_hash(data)
        connection = self._connect()
        created_paths: tuple[Path, Path] | None = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM jobs WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()

            if existing is not None and existing["content_hash"] == content_hash:
                connection.execute(
                    """
                    UPDATE jobs
                    SET last_seen_at = ?, access_count = access_count + 1
                    WHERE id = ?
                    """,
                    (data.captured_at, existing["id"]),
                )
                action = SaveAction.EXISTING
                snapshot_saved = False
            else:
                artifacts = save_page_artifacts(snapshot, artifact_directory)
                created_paths = (artifacts.raw_xml_path, artifacts.structured_json_path)
                raw_xml_path = self._stored_path(artifacts.raw_xml_path)
                structured_json_path = self._stored_path(artifacts.structured_json_path)

                if existing is None:
                    cursor = connection.execute(
                        """
                        INSERT INTO jobs (
                            fingerprint, content_hash, job_name, company_name, salary,
                            location, job_description, source_package, source_activity,
                            created_at, updated_at, last_seen_at, access_count,
                            raw_xml_path, structured_json_path
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                        """,
                        (
                            fingerprint,
                            content_hash,
                            data.job_name.value,
                            data.company_name.value,
                            data.salary.value,
                            data.location.value,
                            data.job_description.value,
                            self._source_package(data.foreground_activity),
                            data.foreground_activity,
                            data.captured_at,
                            data.captured_at,
                            data.captured_at,
                            raw_xml_path,
                            structured_json_path,
                        ),
                    )
                    job_id = int(cursor.lastrowid)
                    action = SaveAction.INSERTED
                else:
                    job_id = int(existing["id"])
                    connection.execute(
                        """
                        UPDATE jobs
                        SET content_hash = ?, job_name = ?, company_name = ?, salary = ?,
                            location = ?, job_description = ?, source_package = ?,
                            source_activity = ?, updated_at = ?, last_seen_at = ?,
                            access_count = access_count + 1, raw_xml_path = ?,
                            structured_json_path = ?
                        WHERE id = ?
                        """,
                        (
                            content_hash,
                            self._latest_value(data.job_name, existing["job_name"]),
                            self._latest_value(data.company_name, existing["company_name"]),
                            self._latest_value(data.salary, existing["salary"]),
                            self._latest_value(data.location, existing["location"]),
                            self._latest_value(
                                data.job_description, existing["job_description"]
                            ),
                            self._source_package(data.foreground_activity),
                            data.foreground_activity,
                            data.captured_at,
                            data.captured_at,
                            raw_xml_path,
                            structured_json_path,
                            job_id,
                        ),
                    )
                    action = SaveAction.UPDATED

                connection.execute(
                    """
                    INSERT INTO job_snapshots (
                        job_id, content_hash, captured_at, raw_xml_path,
                        structured_json_path
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        content_hash,
                        data.captured_at,
                        raw_xml_path,
                        structured_json_path,
                    ),
                )
                snapshot_saved = True

            connection.commit()
            row = connection.execute(
                "SELECT * FROM jobs WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            if row is None:
                raise JobStoreError("岗位保存后无法重新读取记录")
            return SaveResult(action, self._row_to_job(row), snapshot_saved)
        except (sqlite3.Error, ArtifactSaveError, OSError) as exc:
            connection.rollback()
            if created_paths:
                for path in created_paths:
                    path.unlink(missing_ok=True)
            raise JobStoreError(f"岗位保存失败：{exc}") from exc
        finally:
            connection.close()

    def count_jobs(self) -> int:
        try:
            with closing(self._connect()) as connection:
                return int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        except sqlite3.Error as exc:
            raise JobStoreError(f"岗位数量查询失败：{exc}") from exc

    def count_snapshots(self, job_id: int | None = None) -> int:
        try:
            with closing(self._connect()) as connection:
                if job_id is None:
                    row = connection.execute("SELECT COUNT(*) FROM job_snapshots").fetchone()
                else:
                    row = connection.execute(
                        "SELECT COUNT(*) FROM job_snapshots WHERE job_id = ?", (job_id,)
                    ).fetchone()
                return int(row[0])
        except sqlite3.Error as exc:
            raise JobStoreError(f"岗位快照数量查询失败：{exc}") from exc

    def recent_jobs(self, limit: int = 5) -> list[StoredJob]:
        if limit < 1:
            return []
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY last_seen_at DESC, id DESC LIMIT ?", (limit,)
                ).fetchall()
                return [self._row_to_job(row) for row in rows]
        except sqlite3.Error as exc:
            raise JobStoreError(f"最近岗位查询失败：{exc}") from exc

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def search_jobs(self, keyword: str, limit: int = 20) -> list[JobSearchResult]:
        keyword = keyword.strip()
        if not keyword or limit < 1:
            return []
        pattern = f"%{self._escape_like(keyword)}%"
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE job_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR company_name LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR salary LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR location LIKE ? ESCAPE '\\' COLLATE NOCASE
                       OR job_description LIKE ? ESCAPE '\\' COLLATE NOCASE
                    ORDER BY last_seen_at DESC, id DESC
                    LIMIT ?
                    """,
                    (pattern, pattern, pattern, pattern, pattern, limit),
                ).fetchall()
        except sqlite3.Error as exc:
            raise JobStoreError(f"岗位关键词查询失败：{exc}") from exc

        results: list[JobSearchResult] = []
        needle = keyword.casefold()
        labels = (
            ("职位名称", "job_name"),
            ("公司名称", "company_name"),
            ("薪资信息", "salary"),
            ("工作地点", "location"),
            ("岗位描述", "job_description"),
        )
        for row in rows:
            matched = tuple(
                label
                for label, column in labels
                if row[column] is not None and needle in str(row[column]).casefold()
            )
            results.append(JobSearchResult(self._row_to_job(row), matched))
        return results
