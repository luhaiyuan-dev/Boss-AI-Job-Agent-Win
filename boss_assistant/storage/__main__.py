"""岗位历史查询命令行入口。"""

from __future__ import annotations

import argparse
import sys

from .repository import JobSearchResult, JobStore, JobStoreError, StoredJob


def _configure_utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查询本地 Boss 岗位数据库")
    parser.add_argument(
        "--database",
        default="data/jobs.sqlite3",
        help="SQLite 岗位数据库路径（默认：data/jobs.sqlite3）",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("count", help="查看岗位总数")

    recent = commands.add_parser("recent", help="查看最近采集的岗位")
    recent.add_argument("--limit", type=int, default=5, help="返回数量（默认：5）")

    search = commands.add_parser("search", help="按关键词搜索岗位")
    search.add_argument("keyword", help="要搜索的关键词")
    search.add_argument("--limit", type=int, default=20, help="返回数量（默认：20）")
    return parser


def _date(value: str) -> str:
    return value.split("T", 1)[0]


def _print_job(job: StoredJob) -> None:
    print(f"职位：{job.job_name or '—'}")
    print(f"公司：{job.company_name or '—'}")
    print(f"薪资：{job.salary or '—'}")
    print(f"地点：{job.location or '—'}")
    print(f"最近采集：{_date(job.last_seen_at)}")
    print(f"访问次数：{job.access_count}")


def _print_search_result(result: JobSearchResult) -> None:
    _print_job(result.job)
    print(f"匹配字段：{'、'.join(result.matched_fields) or '—'}")


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_console()
    args = _parser().parse_args(argv)
    try:
        store = JobStore(args.database)
        if args.command == "count":
            print("当前岗位数量：")
            print(store.count_jobs())
            return 0

        if args.command == "recent":
            jobs = store.recent_jobs(args.limit)
            print("最近采集：")
            if not jobs:
                print("（暂无岗位）")
            for index, job in enumerate(jobs, start=1):
                if index > 1:
                    print()
                _print_job(job)
            return 0

        results = store.search_jobs(args.keyword, args.limit)
        print(f"关键词：{args.keyword}")
        print(f"匹配岗位数：{len(results)}")
        for index, result in enumerate(results, start=1):
            print()
            print(f"结果 {index}：")
            _print_search_result(result)
        return 0
    except JobStoreError as exc:
        print(f"[失败] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
