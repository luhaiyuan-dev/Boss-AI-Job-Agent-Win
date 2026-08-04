"""用当前断点配置执行一次有界的 Boss Web 端到端仅填充验证。

该工具不连接 MySQL，也不会发送招呼语；它会沿用断点中的求职意向选择，用于在真实已登录页面上验证：
求职意向 -> 卡片初筛 -> 详情审核 -> 消息巡检 -> 返回推荐页 -> 填充招呼语。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boss_assistant.automation import (  # noqa: E402
    AutomationConfig,
    AutomationPolicy,
    BossAutomationRunner,
    CodexCliReviewProvider,
    JobExpectation,
)
from boss_assistant.browser import EdgeBrowser  # noqa: E402
from boss_assistant.resume import process_inbox_resume  # noqa: E402
from boss_assistant.storage import JobStore  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 Boss Web 端到端仅填充验证")
    parser.add_argument("--target-companies", type=int, default=1)
    parser.add_argument("--max-jobs", type=int, default=10)
    parser.add_argument(
        "--checkpoint",
        default="data/automation_runs/boss_checkpoint.json",
    )
    return parser


def _load_policy(path: Path, target_companies: int) -> AutomationPolicy:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("policy")
    if not isinstance(raw, dict):
        raise ValueError(f"断点文件缺少 policy：{path}")
    selected_raw = raw.get("selected_expectation")
    selected_expectation = None
    if isinstance(selected_raw, dict) and str(selected_raw.get("role") or "").strip():
        selected_expectation = JobExpectation(
            city=str(selected_raw.get("city") or "").strip() or None,
            role=str(selected_raw["role"]).strip(),
            salary=str(selected_raw.get("salary") or "").strip() or None,
            keywords=tuple(selected_raw.get("keywords") or ()),
        )
    policy = AutomationPolicy(
        excluded_companies=tuple(raw.get("excluded_companies") or ()),
        allowed_job_keywords=tuple(raw.get("allowed_job_keywords") or ()),
        allowed_locations=tuple(raw.get("allowed_locations") or ()),
        target_companies=int(raw.get("target_companies") or 1),
        excluded_job_directions=tuple(raw.get("excluded_job_directions") or ()),
        minimum_score=int(raw.get("minimum_score") or 50),
        weekend_rest=str(raw.get("weekend_rest") or "不限"),
        experience_requirement=str(raw.get("experience_requirement") or "1-3年"),
        selected_expectation=selected_expectation,
        salary_min_k=(
            int(raw["salary_min_k"])
            if raw.get("salary_min_k") is not None
            else None
        ),
        salary_max_k=(
            int(raw["salary_max_k"])
            if raw.get("salary_max_k") is not None
            else None
        ),
        minimum_company_size=(
            int(raw["minimum_company_size"])
            if raw.get("minimum_company_size") is not None
            else None
        ),
    )
    return replace(policy, target_companies=target_companies)


def main() -> None:
    args = _parser().parse_args()
    if args.target_companies < 1 or args.max_jobs < 1:
        raise ValueError("target-companies 和 max-jobs 必须至少为 1")

    policy = _load_policy(Path(args.checkpoint), args.target_companies)
    resume_result = process_inbox_resume("resume_inbox", "data/resume")
    browser = EdgeBrowser(
        attach_existing_only=True,
        require_boss_page=True,
    )
    runner = BossAutomationRunner(
        browser,
        JobStore("data/smoke_jobs.sqlite3"),
        resume_result.process_result.resume,
        artifact_directory="data/smoke_job_artifacts",
        run_directory="data/smoke_runs",
        config=AutomationConfig(
            max_jobs=args.max_jobs,
            dry_run=False,
            fill_only=True,
        ),
        policy=policy,
        output=lambda text: print(text, flush=True),
        status_callback=lambda step, detail: print(
            f"[状态] {step}：{detail}",
            flush=True,
        ),
        review_provider=CodexCliReviewProvider(
            "data/manual_reviews",
            workspace=".",
            output=lambda text: print(text, flush=True),
        ),
        resume_text=resume_result.process_result.document.text,
        require_logged_in_before_start=True,
    )
    try:
        stats, log_path = runner.run()
        print(
            "[完成] "
            f"检查={stats.inspected} 匹配={stats.matched} "
            f"跳过={stats.skipped} 失败={stats.failed} "
            f"填充目标={args.target_companies} 记录={log_path.resolve()}",
            flush=True,
        )
    finally:
        browser.quit()


if __name__ == "__main__":
    main()
