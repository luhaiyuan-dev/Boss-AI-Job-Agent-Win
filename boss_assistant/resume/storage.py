"""原始简历 PDF、提取文本和 ResumeData 的本地成套保存。"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import ResumeData


class ResumeSaveError(RuntimeError):
    """简历文件包无法完整保存。"""


@dataclass(frozen=True)
class SavedResumeArtifacts:
    bundle_directory: Path
    pdf_path: Path
    text_path: Path
    json_path: Path


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return (cleaned[:40] or "resume")


def _create_bundle_directory(base_directory: Path, source_stem: str) -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base_name = f"{_safe_stem(source_stem)}_{timestamp}"
    for suffix in range(100):
        name = base_name if suffix == 0 else f"{base_name}_{suffix}"
        candidate = base_directory / name
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            continue
    raise ResumeSaveError("无法创建唯一的简历保存目录")


def save_resume_bundle(
    source_pdf_path: str | Path,
    extracted_text: str,
    resume_data: ResumeData,
    output_base_directory: str | Path,
) -> SavedResumeArtifacts:
    """创建独立目录并原子提交 PDF、TXT、JSON，避免覆盖已有简历。"""

    source = Path(source_pdf_path).expanduser().resolve()
    base_directory = Path(output_base_directory).expanduser()
    bundle_directory: Path | None = None
    paths: list[Path] = []
    try:
        base_directory.mkdir(parents=True, exist_ok=True)
        bundle_directory = _create_bundle_directory(base_directory, source.stem)
        pdf_path = bundle_directory / "resume.pdf"
        text_path = bundle_directory / "resume_text.txt"
        json_path = bundle_directory / "resume.json"
        temp_pdf = bundle_directory / ".resume.pdf.tmp"
        temp_text = bundle_directory / ".resume_text.txt.tmp"
        temp_json = bundle_directory / ".resume.json.tmp"
        paths = [pdf_path, text_path, json_path, temp_pdf, temp_text, temp_json]

        shutil.copy2(source, temp_pdf)
        temp_text.write_text(extracted_text.rstrip() + "\n", encoding="utf-8")
        payload = resume_data.to_dict()
        payload["artifacts"] = {
            "saved_pdf": "resume.pdf",
            "extracted_text": "resume_text.txt",
            "structured_json": "resume.json",
        }
        temp_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp_pdf.replace(pdf_path)
        temp_text.replace(text_path)
        temp_json.replace(json_path)
        return SavedResumeArtifacts(
            bundle_directory.resolve(),
            pdf_path.resolve(),
            text_path.resolve(),
            json_path.resolve(),
        )
    except (OSError, ResumeSaveError) as exc:
        for path in paths:
            path.unlink(missing_ok=True)
        if bundle_directory and bundle_directory.exists():
            try:
                bundle_directory.rmdir()
            except OSError:
                pass
        if isinstance(exc, ResumeSaveError):
            raise
        raise ResumeSaveError(f"简历文件保存失败：{exc}") from exc
