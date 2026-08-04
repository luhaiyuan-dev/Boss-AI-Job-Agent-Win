"""串联 PDF 读取、确定性解析和本地保存。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .models import ResumeData
from .parser import parse_resume_text
from .pdf_reader import PdfTextDocument, read_pdf_text
from .storage import SavedResumeArtifacts, save_resume_bundle


@dataclass(frozen=True)
class ResumeProcessResult:
    document: PdfTextDocument
    resume: ResumeData
    artifacts: SavedResumeArtifacts


def process_resume(
    pdf_path: str | Path,
    output_base_directory: str | Path = "data/resume",
) -> ResumeProcessResult:
    document = read_pdf_text(pdf_path)
    resume = parse_resume_text(document.text, document.source_path)
    artifacts = save_resume_bundle(
        document.source_path,
        document.text,
        resume,
        output_base_directory,
    )
    return ResumeProcessResult(document, resume, artifacts)
