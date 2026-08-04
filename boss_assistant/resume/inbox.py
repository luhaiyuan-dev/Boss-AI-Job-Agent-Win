"""从项目内的简历收件目录选择当前要使用的 PDF。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .service import ResumeProcessResult, process_resume


DEFAULT_RESUME_INBOX = Path("resume_inbox")


class ResumeInboxError(RuntimeError):
    """简历收件目录不存在可唯一选择的 PDF。"""


@dataclass(frozen=True)
class InboxResume:
    source_path: Path
    process_result: ResumeProcessResult


def find_resume_pdf(directory: str | Path = DEFAULT_RESUME_INBOX) -> Path:
    """扫描目录并选择唯一的 PDF；文件名可以任意。"""

    inbox = Path(directory).expanduser()
    inbox.mkdir(parents=True, exist_ok=True)
    candidates = sorted(
        (path for path in inbox.iterdir() if path.is_file() and path.suffix.lower() == ".pdf"),
        key=lambda path: path.name.casefold(),
    )
    if not candidates:
        raise ResumeInboxError(
            f"简历目录中没有 PDF：{inbox.resolve()}。请放入一份 PDF 简历。"
        )
    if len(candidates) > 1:
        names = "、".join(path.name for path in candidates)
        raise ResumeInboxError(
            f"简历目录中有多份 PDF，无法确定当前简历：{names}。"
            "请只保留本次要使用的一份 PDF；文件名可以任意。"
        )
    return candidates[0].resolve()


def process_inbox_resume(
    directory: str | Path = DEFAULT_RESUME_INBOX,
    output_directory: str | Path = "data/resume",
) -> InboxResume:
    source = find_resume_pdf(directory)
    return InboxResume(source, process_resume(source, output_directory))
