"""登录后运行的 DOM 探针：核对 Web 选择器是否命中真实页面结构。

用途：Boss直聘 Web 前端类名会随版本变化。首次在本机使用、或脚本读不到岗位卡片/
求职意向时，运行本工具核对当前页面能提取到什么。它会：
  1. 接管当前已打开且带远程调试端口的 Edge Boss 页面；
  2. 若未登录则提示你登录后重新运行；
  3. 点击“职位”，列出识别到的求职意向；
  4. 提取当前岗位卡片并打印样例；
  5. 把整页 HTML 存到 data/probe/ 供进一步核对。

如果卡片数或求职意向数为 0，说明选择器需要按真实 DOM 调整：把 data/probe/ 下的
HTML 里对应容器的类名，填进 config/web_selectors.local.json 覆盖同名选择器组即可，
无需改动业务代码。

用法：
    python tools/probe_dom.py
    python tools/probe_dom.py --target-city 广州 --target-role Python
"""

from __future__ import annotations

import argparse
import sys
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox

# 允许从项目根直接运行。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from boss_assistant.browser import EdgeBrowser  # noqa: E402
from boss_assistant.web import (  # noqa: E402
    click_expectation,
    click_positions_tab,
    extract_job_cards,
    parse_job_intents,
    read_job_detail,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="探测 Boss直聘 Web 当前 DOM")
    parser.add_argument(
        "--target-city",
        help="点击与该城市匹配的求职意向；可与 --target-role 组合精确定位",
    )
    parser.add_argument(
        "--target-role",
        help="点击与该岗位角色匹配的求职意向；城市相同时用于区分不同意向",
    )
    parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="保存列表页 HTML 后直接退出，不等待人工点开详情",
    )
    return parser


def _check_login(browser: EdgeBrowser) -> bool:
    browser.switch_to_boss_page()
    if browser.is_logged_in():
        return True
    print("检测到当前 Edge 的 Boss 页面未登录：请登录后重新运行探针。")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        messagebox.showinfo(
            "请登录 Boss直聘 Web",
            "当前 Edge 的 Boss 页面未登录或登录已失效。\n\n"
            "请先在 Edge 中完成登录，然后重新运行探针。",
            parent=root,
        )
    finally:
        root.destroy()
    return False


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    browser = EdgeBrowser(
        attach_existing_only=True,
        require_boss_page=True,
    )
    browser.start()
    try:
        if not _check_login(browser):
            return

        print("\n==== 点击“职位” ====")
        clicked = click_positions_tab(browser)
        print(f"是否找到“职位”入口：{clicked}")
        time.sleep(1.5)

        print("\n==== 求职意向 ====")
        intents = parse_job_intents(browser)
        print(f"识别到 {len(intents.expectations)} 条求职意向：")
        for item in intents.expectations:
            print(f"  - 城市={item.city!r}  角色={item.role!r}")
        if intents.expectations:
            def matches_target(item) -> bool:
                city_matches = not args.target_city or args.target_city in (item.city or "")
                role_matches = not args.target_role or args.target_role in item.role
                return city_matches and role_matches

            preferred = next(
                (item for item in intents.expectations if matches_target(item)),
                intents.expectations[0],
            )
            clicked_expectation = click_expectation(
                browser,
                args.target_city or preferred.city,
                role=args.target_role or preferred.role,
            )
            if clicked_expectation:
                time.sleep(2.0)

        print("\n==== 岗位卡片 ====")
        cards = extract_job_cards(browser, include_company_scale=True)
        print(f"识别到 {len(cards)} 张岗位卡片：")
        for card in cards[:8]:
            print(
                f"  - {card.job_name} | {card.company_name} | {card.salary} | "
                f"{card.location} | 经验={card.experience} 学历={card.degree} | "
                f"公司规模={card.company_scale or '页面接口未返回'} | "
                f"招聘者活跃={card.recruiter_activity or '页面未显示'} | "
                f"job_id={card.job_id} | tags={card.tags}"
            )

        probe_dir = Path("data/probe")
        probe_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = probe_dir / f"jobs_page_{stamp}.html"
        html_path.write_text(browser.outer_html(), encoding="utf-8")
        print(f"\n整页 HTML 已保存：{html_path.resolve()}")

        if args.snapshot_only:
            return
        print(
            "\n如需核对详情页：在浏览器中点开任意一个岗位使其显示详情，然后回到此处按回车。"
        )
        try:
            input("按回车读取当前详情页（或直接回车跳过）……")
        except EOFError:
            return
        snapshot = read_job_detail(browser)
        job = snapshot.job_data
        print("\n==== 详情读取结果 ====")
        print(f"  是否识别为详情页：{job.is_boss_job_detail_page}")
        print(f"  职位：{job.job_name.value}")
        print(f"  公司：{job.company_name.value}")
        print(f"  薪资：{job.salary.value}")
        print(f"  地点：{job.location.value}")
        print(f"  经验：{job.experience.value}")
        desc = job.job_description.value or ""
        print(f"  描述前 200 字：{desc[:200]}")
        detail_path = probe_dir / f"detail_page_{stamp}.html"
        detail_path.write_text(browser.outer_html(), encoding="utf-8")
        print(f"详情页 HTML 已保存：{detail_path.resolve()}")
    finally:
        try:
            input("\n探测结束，按回车关闭浏览器……")
        except EOFError:
            pass
        browser.quit()


if __name__ == "__main__":
    main()
