"""自动化运行与岗位处理结果的 MySQL 持久化。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any


class MySqlStoreError(RuntimeError):
    """MySQL 连接、建库、建表或写入失败。"""


@dataclass(frozen=True)
class MySqlConfig:
    host: str
    port: int
    user: str
    password: str
    database: str = "boss_job_assistant"

    def validate(self) -> None:
        if not self.host or not self.user or not self.password or not self.database:
            raise MySqlStoreError("MySQL 配置字段不允许为空")
        if not 1 <= self.port <= 65535:
            raise MySqlStoreError("MySQL 端口必须在 1-65535 之间")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.database):
            raise MySqlStoreError("MySQL 数据库名只能包含字母、数字和下划线，且必须以字母开头")


class AutomationMySqlStore:
    def __init__(self, config: MySqlConfig, *, connector: Any | None = None) -> None:
        config.validate()
        self.config = config
        if connector is None:
            try:
                import mysql.connector as connector_module
            except ImportError as exc:
                raise MySqlStoreError(
                    "缺少 mysql-connector-python，请执行 python -m pip install -r requirements.txt"
                ) from exc
            connector = connector_module
        self.connector = connector

    def _connect(self, *, with_database: bool) -> Any:
        arguments: dict[str, object] = {
            "host": self.config.host,
            "port": self.config.port,
            "user": self.config.user,
            "password": self.config.password,
            "connection_timeout": 5,
            "charset": "utf8mb4",
            # mysql-connector-python 9.7 的 C 扩展在 Python 3.13 + Nuitka
            # onefile 中连接失败时只会抛出 "Failed raising error."。纯 Python
            # 协议实现已在同一份外置配置和同构 onefile 探针中验证可用。
            "use_pure": True,
        }
        if with_database:
            arguments["database"] = self.config.database
        try:
            return self.connector.connect(**arguments)
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 连接失败：{exc}") from exc

    def initialize(self) -> None:
        database = self.config.database
        connection = self._connect(with_database=False)
        cursor = connection.cursor()
        try:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{database}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
            )
            connection.commit()
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 创建数据库失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()

        connection = self._connect(with_database=True)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS automation_runs (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    started_at DATETIME(6) NOT NULL,
                    completed_at DATETIME(6) NULL,
                    mode VARCHAR(32) NOT NULL,
                    target_companies INT NOT NULL,
                    settings_json JSON NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    inspected_count INT NOT NULL DEFAULT 0,
                    selected_count INT NOT NULL DEFAULT 0,
                    sent_count INT NOT NULL DEFAULT 0,
                    failed_count INT NOT NULL DEFAULT 0
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS application_results (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    run_id BIGINT NOT NULL,
                    created_at DATETIME(6) NOT NULL,
                    processing_duration VARCHAR(32) NULL,
                    company_name VARCHAR(255) NULL,
                    job_name VARCHAR(255) NULL,
                    location VARCHAR(255) NULL,
                    salary VARCHAR(128) NULL,
                    recruiter_activity VARCHAR(128) NULL,
                    qualifications_summary TEXT NULL,
                    match_score INT NOT NULL DEFAULT 0,
                    should_apply BOOLEAN NOT NULL DEFAULT FALSE,
                    delivery_status VARCHAR(64) NOT NULL,
                    greeting TEXT NULL,
                    reasons_json JSON NOT NULL,
                    CONSTRAINT fk_application_results_run
                        FOREIGN KEY (run_id) REFERENCES automation_runs(id)
                        ON DELETE CASCADE,
                    INDEX idx_results_run_created (run_id, created_at),
                    INDEX idx_results_company_job (company_name, job_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 每日投递统计：只保存聚合数字，不落任何公司名称或岗位信息。
            # 数据由 application_results 按自然日实时聚合（公司按名称去重），
            # 因此同一家公司当天被多次处理也只计一次。
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_delivery_stats (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    stat_date DATE NOT NULL,
                    inspected_companies INT NOT NULL DEFAULT 0,
                    matched_companies INT NOT NULL DEFAULT 0,
                    sent_companies INT NOT NULL DEFAULT 0,
                    filled_companies INT NOT NULL DEFAULT 0,
                    skipped_companies INT NOT NULL DEFAULT 0,
                    failed_companies INT NOT NULL DEFAULT 0,
                    processing_failed_companies INT NOT NULL DEFAULT 0,
                    send_failed_companies INT NOT NULL DEFAULT 0,
                    inspected_jobs INT NOT NULL DEFAULT 0,
                    sent_jobs INT NOT NULL DEFAULT 0,
                    run_count INT NOT NULL DEFAULT 0,
                    first_result_at DATETIME(6) NULL,
                    last_result_at DATETIME(6) NULL,
                    updated_at DATETIME(6) NOT NULL,
                    UNIQUE KEY uk_daily_stats_date (stat_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 投递成功明细镜像：字段与 application_results 完全相同，只保留投递成功
            # （发送成功／已填充未发送）的记录，供单独查看成功投递的公司。id 沿用
            # application_results 的同一个 id（同一条记录同一个 id），便于去重导入。
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS successful_applications (
                    id BIGINT PRIMARY KEY AUTO_INCREMENT,
                    run_id BIGINT NOT NULL,
                    created_at DATETIME(6) NOT NULL,
                    processing_duration VARCHAR(32) NULL,
                    company_name VARCHAR(255) NULL,
                    job_name VARCHAR(255) NULL,
                    location VARCHAR(255) NULL,
                    salary VARCHAR(128) NULL,
                    recruiter_activity VARCHAR(128) NULL,
                    qualifications_summary TEXT NULL,
                    match_score INT NOT NULL DEFAULT 0,
                    should_apply BOOLEAN NOT NULL DEFAULT FALSE,
                    delivery_status VARCHAR(64) NOT NULL,
                    greeting TEXT NULL,
                    reasons_json JSON NOT NULL,
                    INDEX idx_success_run_created (run_id, created_at),
                    INDEX idx_success_company_job (company_name, job_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            # 镜像表 id 从 1 自增、与 application_results 的 id 无关。历史成功记录只在
            # 镜像表为空时按原始时间顺序导入一次（避免重复导入），之后每保存一条成功
            # 结果由 save_result 实时补进来。
            cursor.execute("SELECT COUNT(*) FROM successful_applications")
            if (cursor.fetchone() or (0,))[0] == 0:
                cursor.execute(
                    f"INSERT INTO successful_applications ({self._SUCCESS_MIRROR_COLUMNS}) "
                    f"SELECT {self._SUCCESS_MIRROR_COLUMNS} FROM application_results "
                    "WHERE delivery_status IN ('发送成功', '已填充未发送') ORDER BY id"
                )
            # 老库里 application_results 建表时还没有 processing_duration；
            # 用 AFTER created_at 就地补列，保持“时间→耗时→公司”的阅读顺序，
            # 历史行留空。已有该列时跳过，可重复执行。
            cursor.execute(
                """
                SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = 'application_results'
                  AND COLUMN_NAME = 'processing_duration'
                """,
                (database,),
            )
            row = cursor.fetchone()
            if row is not None and int(row[0]) == 0:
                cursor.execute(
                    "ALTER TABLE application_results "
                    "ADD COLUMN processing_duration VARCHAR(32) NULL AFTER created_at"
                )
            connection.commit()
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 创建数据表失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()

    def start_run(self, mode: str, target_companies: int, settings: dict[str, object]) -> int:
        connection = self._connect(with_database=True)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO automation_runs (
                    started_at, mode, target_companies, settings_json, status
                ) VALUES (%s, %s, %s, %s, 'running')
                """,
                (
                    datetime.now(),
                    mode,
                    target_companies,
                    json.dumps(settings, ensure_ascii=False),
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.commit()
            return run_id
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 创建运行记录失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()

    def update_run_settings(
        self,
        run_id: int,
        mode: str,
        target_companies: int,
        settings: dict[str, object],
    ) -> None:
        """暂停修改条件后同步当前运行记录，避免数据库仍保留启动时快照。"""

        connection = self._connect(with_database=True)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE automation_runs
                SET mode = %s, target_companies = %s, settings_json = %s
                WHERE id = %s AND status = 'running'
                """,
                (
                    mode,
                    target_companies,
                    json.dumps(settings, ensure_ascii=False),
                    run_id,
                ),
            )
            connection.commit()
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 同步运行条件失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()

    def save_result(self, run_id: int, result: dict[str, object]) -> None:
        connection = self._connect(with_database=True)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO application_results (
                    run_id, created_at, processing_duration, company_name, job_name,
                    location, salary, recruiter_activity, qualifications_summary,
                    match_score, should_apply, delivery_status, greeting, reasons_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    datetime.now(),
                    result.get("processing_duration"),
                    result.get("company_name"),
                    result.get("job_name"),
                    result.get("location"),
                    result.get("salary"),
                    result.get("recruiter_activity"),
                    result.get("qualifications_summary"),
                    int(result.get("score") or 0),
                    bool(result.get("should_apply")),
                    result.get("delivery_status") or "未知",
                    result.get("greeting"),
                    json.dumps(result.get("reasons") or [], ensure_ascii=False),
                ),
            )
            # 投递成功的记录镜像到 successful_applications（id 自增，不复制原 id）。
            if (result.get("delivery_status") or "未知") in ("发送成功", "已填充未发送"):
                cursor.execute(
                    f"INSERT INTO successful_applications ({self._SUCCESS_MIRROR_COLUMNS}) "
                    f"SELECT {self._SUCCESS_MIRROR_COLUMNS} FROM application_results "
                    "WHERE id = %s",
                    (cursor.lastrowid,),
                )
            connection.commit()
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 保存岗位结果失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()
        # 每保存一条结果就重算当天统计，使统计表在脚本运行过程中保持实时。
        self.refresh_daily_stats()

    # daily_delivery_stats 的 13 个聚合列，顺序必须与下方聚合 SELECT 的表达式一一对应。
    _DAILY_STAT_COLUMNS = (
        "inspected_companies",
        "matched_companies",
        "sent_companies",
        "filled_companies",
        "skipped_companies",
        "failed_companies",
        "processing_failed_companies",
        "send_failed_companies",
        "inspected_jobs",
        "sent_jobs",
        "run_count",
        "first_result_at",
        "last_result_at",
    )

    # successful_applications 的业务列（不含自增 id），导入/补写时按列对齐、不复制原 id。
    _SUCCESS_MIRROR_COLUMNS = (
        "run_id, created_at, processing_duration, company_name, job_name, "
        "location, salary, recruiter_activity, qualifications_summary, "
        "match_score, should_apply, delivery_status, greeting, reasons_json"
    )

    def refresh_daily_stats(self, stat_date: date | None = None) -> None:
        """按自然日重算当天投递统计并写入 daily_delivery_stats（全部检查过的公司）。

        统计口径全部来自 application_results 的聚合：公司维度按 company_name 去重
        （同一家公司当天多次处理只计一次），岗位维度按记录条数计。统计表只保存
        数字、不写入任何公司名称。每保存一条结果后立即刷新，保证运行过程中实时。

        采用“先查后写”而非 INSERT ... ON DUPLICATE KEY UPDATE：后者即便命中重复键
        只做 UPDATE，MySQL InnoDB 也会白白消耗一个 AUTO_INCREMENT 值；本方法每保存
        一条结果就被调一次（一天上百次），会把 id 顶到与天数严重脱节的巨大值。先查
        后写让每天最多消耗一个自增值，id 严格按天递增 1、2、3……

        （注：只含投递成功的公司改用明细镜像表 successful_applications，不再做成一张
        聚合表；本方法只负责全量的 daily_delivery_stats。）
        """

        target = stat_date or date.today()
        connection = self._connect(with_database=True)
        cursor = connection.cursor()
        try:
            updated_at = datetime.now()
            self._write_stats_row(cursor, "daily_delivery_stats", target, updated_at, "")
            connection.commit()
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 刷新每日投递统计失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()

    def _write_stats_row(
        self,
        cursor,
        table: str,
        target: date,
        updated_at: datetime,
        extra_where: str,
    ) -> None:
        """把某一天的聚合结果“先查后写”进指定统计表（存在则 UPDATE、否则 INSERT）。

        extra_where 追加在 ``WHERE DATE(created_at) = %(stat_date)s`` 之后限定口径
        （空串＝全量）。table 与 extra_where 都是模块内固定字面量、不含用户输入，
        无 SQL 注入风险。
        """

        columns = self._DAILY_STAT_COLUMNS
        # 聚合当天统计（无 GROUP BY，恒返回单行）。表达式顺序必须 == columns。
        cursor.execute(
            f"""
            SELECT
                COUNT(DISTINCT company_name),
                COUNT(DISTINCT CASE WHEN delivery_status IN ('发送成功', '已填充未发送')
                    THEN company_name END),
                COUNT(DISTINCT CASE WHEN delivery_status = '发送成功'
                    THEN company_name END),
                COUNT(DISTINCT CASE WHEN delivery_status = '已填充未发送'
                    THEN company_name END),
                COUNT(DISTINCT CASE WHEN delivery_status = '未投递'
                    THEN company_name END),
                COUNT(DISTINCT CASE WHEN delivery_status IN ('处理失败', '发送失败')
                    THEN company_name END),
                COUNT(DISTINCT CASE WHEN delivery_status = '处理失败'
                    THEN company_name END),
                COUNT(DISTINCT CASE WHEN delivery_status = '发送失败'
                    THEN company_name END),
                COUNT(*),
                SUM(CASE WHEN delivery_status = '发送成功' THEN 1 ELSE 0 END),
                COUNT(DISTINCT run_id),
                MIN(created_at),
                MAX(created_at)
            FROM application_results
            WHERE DATE(created_at) = %(stat_date)s {extra_where}
            """,
            {"stat_date": target},
        )
        values = cursor.fetchone()

        # 当天行已存在则 UPDATE（不碰自增），否则 INSERT（正常 +1）。
        cursor.execute(
            f"SELECT id FROM {table} WHERE stat_date = %(stat_date)s",
            {"stat_date": target},
        )
        exists = cursor.fetchone() is not None

        if exists:
            assignments = ", ".join(f"{name} = %s" for name in columns)
            cursor.execute(
                f"UPDATE {table} SET {assignments}, updated_at = %s WHERE stat_date = %s",
                (*values, updated_at, target),
            )
        else:
            column_list = ", ".join(("stat_date", *columns, "updated_at"))
            placeholders = ", ".join(["%s"] * (len(columns) + 2))
            cursor.execute(
                f"INSERT INTO {table} ({column_list}) VALUES ({placeholders})",
                (target, *values, updated_at),
            )

    def recent_sent_companies(
        self,
        *,
        within_days: int = 30,
        now: datetime | None = None,
    ) -> dict[str, datetime]:
        """返回严格短于指定天数、状态为发送成功的公司及最近发送时间。"""

        if within_days <= 0:
            raise ValueError("within_days必须大于0")
        cutoff = (now or datetime.now()) - timedelta(days=within_days)
        connection = self._connect(with_database=True)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT company_name, MAX(created_at) AS last_sent_at
                FROM application_results
                WHERE delivery_status = '发送成功'
                  AND company_name IS NOT NULL
                  AND company_name <> ''
                  AND created_at > %s
                GROUP BY company_name
                """,
                (cutoff,),
            )
            return {
                str(company): sent_at
                for company, sent_at in cursor.fetchall()
                if company and isinstance(sent_at, datetime)
            }
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 查询30天内已投递公司失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()

    def recent_successful_applications(
        self,
        *,
        within_days: int = 30,
        now: datetime | None = None,
    ) -> dict[tuple[str, str], datetime]:
        """返回30天内公司名、岗位名均完整且状态为发送成功的精确组合。"""

        if within_days <= 0:
            raise ValueError("within_days必须大于0")
        cutoff = (now or datetime.now()) - timedelta(days=within_days)
        connection = self._connect(with_database=True)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                SELECT company_name, job_name, MAX(created_at) AS last_sent_at
                FROM application_results
                WHERE delivery_status = '发送成功'
                  AND company_name IS NOT NULL
                  AND company_name <> ''
                  AND job_name IS NOT NULL
                  AND job_name <> ''
                  AND created_at > %s
                GROUP BY company_name, job_name
                """,
                (cutoff,),
            )
            return {
                (str(company), str(job)): sent_at
                for company, job, sent_at in cursor.fetchall()
                if company and job and isinstance(sent_at, datetime)
            }
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 查询30天内发送成功岗位失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()

    def finish_run(self, run_id: int, stats: object, *, status: str) -> None:
        connection = self._connect(with_database=True)
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                UPDATE automation_runs
                SET completed_at = %s, status = %s, inspected_count = %s,
                    selected_count = %s, sent_count = %s, failed_count = %s
                WHERE id = %s
                """,
                (
                    datetime.now(),
                    status,
                    int(getattr(stats, "inspected", 0)),
                    int(getattr(stats, "matched", 0)),
                    int(getattr(stats, "sent", 0)),
                    int(getattr(stats, "failed", 0)),
                    run_id,
                ),
            )
            connection.commit()
        except Exception as exc:
            raise MySqlStoreError(f"MySQL 更新运行结果失败：{exc}") from exc
        finally:
            cursor.close()
            connection.close()
