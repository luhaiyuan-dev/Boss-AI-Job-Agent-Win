"""岗位处理结果的纯视图筛选与相关度排序。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

ALL_RESULT_FILTER_OPTION = "全部"
DELIVERY_STATUS_OPTIONS = (
    ALL_RESULT_FILTER_OPTION,
    "发送成功",
    "已填充未发送",
    "未投递",
    "处理失败",
    "发送失败",
    "已置顶待处理",
    "已发送简历",
    "HR已拒绝，已忽略",
)


@dataclass(frozen=True)
class ResultFilter:
    job_query: str = ""
    location: str = ALL_RESULT_FILTER_OPTION
    delivery_status: str = ALL_RESULT_FILTER_OPTION


@dataclass(frozen=True)
class ResultViewRecord:
    sequence: str
    ordinal: int
    record: dict[str, object]


def _job_title_units(value: object) -> tuple[str, ...]:
    """把岗位名拆成可比较单元：英文/数字词组 + 单个汉字。"""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return tuple(re.findall(r"[a-z0-9+#]+|[\u4e00-\u9fff]", normalized))


def job_title_relevance(
    query: str,
    title: object,
) -> tuple[int, int, int, int, int, int] | None:
    """返回岗位名相关度；无有效关键词交集时返回 ``None``。

    完全相同最高，其次是完整查询被岗位名包含；其余按用户关键词覆盖率、
    岗位名覆盖率和最长连续命中排序。这样既支持完整岗位名，也支持“AI”、
    “AI应用”“工程师”等局部关键词，并能识别“AI 开发 工程师”这类非连续命中。
    """

    query_units = _job_title_units(query)
    title_units = _job_title_units(title)
    if not query_units or not title_units:
        return None
    query_key = "".join(query_units)
    title_key = "".join(title_units)
    matcher = SequenceMatcher(None, query_units, title_units, autojunk=False)
    blocks = matcher.get_matching_blocks()
    matched_units = sum(block.size for block in blocks)
    longest_block = max((block.size for block in blocks), default=0)
    exact = query_key == title_key
    query_contained = query_key in title_key
    title_contained = title_key in query_key
    ascii_query_terms = {
        unit for unit in query_units if unit.isascii() and len(unit) >= 2
    }
    ascii_title_terms = set(title_units)
    meaningful_overlap = (
        exact
        or query_contained
        or title_contained
        or longest_block >= 2
        or bool(ascii_query_terms & ascii_title_terms)
        or (len(query_units) == 1 and query_units[0] in title_units)
    )
    if not meaningful_overlap:
        return None
    scale = 10_000
    return (
        int(exact),
        int(query_contained),
        matched_units * scale // len(query_units),
        int(title_contained),
        matched_units * scale // len(title_units),
        longest_block,
    )


def result_status(record: dict[str, object]) -> str:
    if record.get("record_type") == "chat_action":
        return str(record.get("action") or "—")
    return str(record.get("delivery_status") or "—")


def _status_matches(selected: str, record: dict[str, object]) -> bool:
    """把消息巡检的细分动作归并到用户可筛选的状态。"""

    if selected == "已发送简历" and record.get("record_type") == "chat_action":
        return record.get("resume_sent") is True or record.get("action") in {
            "已发送简历",
            "已回复并发送简历",
        }
    return result_status(record) == selected


def _result_job_name(record: dict[str, object]) -> object:
    if record.get("record_type") == "chat_action":
        return record.get("position_name")
    return record.get("job_name")


def _location_matches(selected: str, actual: object) -> bool:
    selected_key = "".join(_job_title_units(selected))
    actual_key = "".join(_job_title_units(actual))
    return bool(selected_key and actual_key and selected_key in actual_key)


def filter_result_records(
    records: list[ResultViewRecord],
    result_filter: ResultFilter,
) -> list[ResultViewRecord]:
    """按岗位、地点、投递情况取交集；岗位筛选结果再按相关度降序排列。"""

    matched: list[
        tuple[tuple[int, int, int, int, int, int] | None, ResultViewRecord]
    ] = []
    for view_record in records:
        record = view_record.record
        relevance = None
        if result_filter.job_query.strip():
            relevance = job_title_relevance(
                result_filter.job_query,
                _result_job_name(record),
            )
            if relevance is None:
                continue
        if (
            result_filter.location != ALL_RESULT_FILTER_OPTION
            and not _location_matches(result_filter.location, record.get("location"))
        ):
            continue
        if (
            result_filter.delivery_status != ALL_RESULT_FILTER_OPTION
            and not _status_matches(result_filter.delivery_status, record)
        ):
            continue
        matched.append((relevance, view_record))
    if result_filter.job_query.strip():
        matched.sort(
            key=lambda item: (
                tuple(-part for part in (item[0] or ())),
                item[1].ordinal,
            )
        )
    return [view_record for _relevance, view_record in matched]
