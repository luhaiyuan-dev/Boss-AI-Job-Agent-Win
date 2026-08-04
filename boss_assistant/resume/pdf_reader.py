"""使用 pypdf 读取文本型 PDF 简历。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


class ResumePdfError(RuntimeError):
    """PDF 路径、格式、加密状态或文本提取异常。"""


class ResumePdfNotFoundError(ResumePdfError):
    """指定的简历 PDF 不存在。"""


class ResumePdfEmptyTextError(ResumePdfError):
    """PDF 可以打开，但没有可提取文本。"""


@dataclass(frozen=True)
class PdfTextDocument:
    source_path: Path
    page_count: int
    text: str


def normalize_extracted_text(text: str) -> str:
    """修复部分 PDF 将每个字形之间插入空格的问题，不影响普通文本。"""

    normalized = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    visible_count = sum(not character.isspace() for character in normalized)
    glyph_spaces = len(re.findall(r"(?<=\w)[ \t]+(?=\w)", normalized))
    glyph_spaced = glyph_spaces >= 30 and glyph_spaces / max(visible_count, 1) >= 0.12
    if not glyph_spaced:
        return normalized

    punctuation = r":：,，。；;、/@._+\-~～"
    repaired_lines: list[str] = []
    for line in normalized.splitlines():
        line = re.sub(r"(?<=\w)[ \t]+(?=\w)", "", line)
        line = re.sub(rf"(?<=\w)[ \t]+(?=[{punctuation}])", "", line)
        line = re.sub(rf"(?<=[{punctuation}])[ \t]+(?=\w)", "", line)
        repaired_lines.append(re.sub(r"[ \t]{2,}", " ", line).strip())
    return "\n".join(repaired_lines)


def read_pdf_text(pdf_path: str | Path) -> PdfTextDocument:
    """读取 PDF 每页文本；扫描件或纯图片 PDF 明确报空，不执行 OCR。"""

    path = Path(pdf_path).expanduser()
    if not path.exists():
        raise ResumePdfNotFoundError(f"简历 PDF 不存在：{path}")
    if not path.is_file():
        raise ResumePdfError(f"简历路径不是文件：{path}")
    if path.suffix.lower() != ".pdf":
        raise ResumePdfError(f"简历文件扩展名不是 .pdf：{path.name}")

    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise ResumePdfError(
            "缺少 pypdf。请在项目根目录执行：python -m pip install -r requirements.txt"
        ) from exc

    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise ResumePdfError("PDF 已加密，当前版本不读取需要密码的简历")
        page_texts: list[str] = []
        for page_number, page in enumerate(reader.pages, start=1):
            try:
                extracted = page.extract_text() or ""
            except Exception as exc:  # pypdf 的页面内容异常类型随 PDF 结构而异
                raise ResumePdfError(f"PDF 第 {page_number} 页文本提取失败：{exc}") from exc
            page_texts.append(normalize_extracted_text(extracted))
    except ResumePdfError:
        raise
    except (OSError, PdfReadError, ValueError) as exc:
        raise ResumePdfError(f"PDF 无法读取或文件结构损坏：{exc}") from exc

    text = "\n\n".join(page_text.strip() for page_text in page_texts).strip()
    if not text:
        raise ResumePdfEmptyTextError(
            "PDF 没有可提取文本。文件可能是扫描件或纯图片；本阶段不执行 OCR。"
        )
    return PdfTextDocument(path.resolve(), len(reader.pages), text)
