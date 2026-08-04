"""岗位“薪资 / 公司规模 / 经验 / 学历 / 周末休息”的归一化与匹配判断。

三者都把杂乱的中文文本归一到离散等级后再比较：
- 经验：岗位要求等级 <= 用户设置等级 才符合（要求越低越容易满足）；
- 学历：岗位要求等级 <= 简历学历等级 才符合；
- 周末：岗位提供等级 >= 用户设置等级 才符合（双休>大小周>单休），且详情里
  读不到周末休息时一律视为符合。

用户设置里的“经验不限 / (周末)不限”表示不筛。所有映射集中在本模块，便于单独
测试与后续微调，不影响 selectors / runner。
"""
from __future__ import annotations

import re


# -------------------------------------------------------- 薪资 / 公司规模 ----

_SALARY_RANGE_RE = re.compile(
    r"(?P<low>\d+(?:\.\d+)?)\s*[Kk]?\s*[-—–~～至]\s*"
    r"(?P<high>\d+(?:\.\d+)?)\s*[Kk](?![A-Za-z])"
)
_COMPANY_SIZE_RANGE_RE = re.compile(
    r"(?P<low>\d+)\s*[-—–~～至]\s*(?P<high>\d+)\s*人"
)
_COMPANY_SIZE_ABOVE_RE = re.compile(r"(?P<low>\d+)\s*人\s*(?:以上|及以上|起)")


def salary_range_k(text: str | None) -> tuple[float, float] | None:
    """把 Boss 月薪文本解析成 K 为单位的上下限；日薪/时薪/面议均返回 None。"""

    match = _SALARY_RANGE_RE.search((text or "").replace(",", ""))
    if match is None:
        return None
    low = float(match.group("low"))
    high = float(match.group("high"))
    if low < 0 or high < low:
        return None
    return low, high


def salary_meets(
    job_text: str | None,
    minimum_k: int | None,
    maximum_k: int | None,
) -> bool:
    """岗位完整薪资区间是否包含在用户设置的闭区间内。"""

    if minimum_k is None and maximum_k is None:
        return True
    if minimum_k is None or maximum_k is None or maximum_k < minimum_k:
        return False
    parsed = salary_range_k(job_text)
    if parsed is None:
        return False
    job_minimum, job_maximum = parsed
    return minimum_k <= job_minimum and job_maximum <= maximum_k


def company_size_range(text: str | None) -> tuple[int, int | None] | None:
    """解析 Boss 公司规模档位；“10000人以上”的上限用 None 表示。"""

    value = (text or "").replace(",", "").strip()
    ranged = _COMPANY_SIZE_RANGE_RE.search(value)
    if ranged is not None:
        low = int(ranged.group("low"))
        high = int(ranged.group("high"))
        return (low, high) if high >= low else None
    above = _COMPANY_SIZE_ABOVE_RE.search(value)
    if above is not None:
        return int(above.group("low")), None
    return None


def company_size_meets(job_text: str | None, minimum_size: int | None) -> bool:
    """公司规模档位下限是否大于等于设置值；读不到时不猜测、判为不符合。"""

    if minimum_size is None:
        return True
    parsed = company_size_range(job_text)
    return parsed is not None and parsed[0] >= minimum_size

# ---------------------------------------------------------------- 经验 ----

def _max_year(text: str) -> int | None:
    numbers = [int(n) for n in re.findall(r"\d+", text)]
    return max(numbers) if numbers else None


def _level_for_years(years: int) -> int:
    """把“年数上界”落到与 Boss 经验档一致的等级：1年以内=1、1-3年=2、
    3-5年=3、5-10年=4、10年以上=5。"""

    if years <= 1:
        return 1
    if years <= 3:
        return 2
    if years <= 5:
        return 3
    if years <= 10:
        return 4
    return 5


def experience_level(text: str | None) -> int:
    """经验文本 → 等级（越大要求越高）。不限 / 在校 / 应届 / 无法识别 → 0。"""

    t = (text or "").replace(" ", "")
    if not t or "不限" in t or "应届" in t or "在校" in t:
        return 0
    if "以内" in t:  # “1年以内”
        return 1
    if "以上" in t:  # Boss 经验档里“以上”只用于“10年以上”，直接归最高档
        return 5
    years = _max_year(t)  # “1-3年”→3、“8年”→8
    if years is None:
        return 0
    return _level_for_years(years)


def experience_meets(job_text: str | None, requirement: str | None) -> bool:
    """岗位经验要求是否 <= 用户设置（设置“经验不限 / 空”不筛）。"""

    if not requirement or "不限" in requirement:
        return True
    return experience_level(job_text) <= experience_level(requirement)


# ---------------------------------------------------------------- 学历 ----

# 关键词按从高到低排列，命中即定档；靠前的先匹配（如“研究生”前于“本科”无所谓，
# 因为一个文本通常只含一个学历词）。
_DEGREE_ORDER: tuple[tuple[str, int], ...] = (
    ("博士", 6),
    ("硕士", 5),
    ("研究生", 5),
    ("MBA", 5),
    ("本科", 4),
    ("学士", 4),
    ("大专", 3),
    ("专科", 3),
    ("高中", 2),
    ("中专", 2),
    ("职高", 2),
    ("技校", 2),
    ("初中", 1),
)


def degree_level(text: str | None) -> int:
    """学历文本 → 等级（越大越高）。不限 / 无法识别 → 0（岗位侧即无学历要求）。"""

    t = text or ""
    if "不限" in t:
        return 0
    for keyword, level in _DEGREE_ORDER:
        if keyword in t:
            return level
    return 0


def resume_degree_level(education) -> int:  # type: ignore[no-untyped-def]
    """从简历学历条目取**最高**学历等级；没有可识别学历时按最高（6），避免因
    简历缺学历信息而误杀岗位。"""

    levels = [degree_level(getattr(item, "degree", None)) for item in (education or ())]
    positive = [level for level in levels if level > 0]
    return max(positive) if positive else 6


def degree_meets(job_text: str | None, resume_level: int) -> bool:
    """岗位学历要求是否 <= 简历学历等级（岗位“不限 / 未识别”永远符合）。"""

    return degree_level(job_text) <= resume_level


# ------------------------------------------------------------ 周末休息 ----

_WEEKEND_SETTING_LEVEL = {"双休": 3, "大小周": 2, "单休": 1}


def weekend_level(text: str | None) -> int:
    """文本里的周末休息类型 → 等级（双休3 > 大小周2 > 单休1）；未提及 → 0。"""

    t = text or ""
    if "双休" in t:
        return 3
    if "大小周" in t:
        return 2
    if "单休" in t:
        return 1
    return 0


def weekend_meets(detail_text: str | None, requirement: str | None) -> bool:
    """岗位提供的周末休息是否满足用户设置（岗位等级 >= 设置等级）。

    设置“不限 / 空”不筛；详情里读不到任何周末休息类型（等级 0）时一律视为符合
    ——这是用户明确要求的“详情读不到就不管”。
    """

    if not requirement or "不限" in requirement:
        return True
    required = _WEEKEND_SETTING_LEVEL.get(requirement.strip())
    if required is None:
        return True
    level = weekend_level(detail_text)
    if level == 0:  # 详情没提周末休息 → 合格
        return True
    return level >= required
