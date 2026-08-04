"""本地 PDF 简历读取、结构化和保存能力。"""

from .models import (
    BasicInfo,
    ContactInfo,
    EducationEntry,
    InternshipExperience,
    ProjectExperience,
    ResumeData,
    SkillSet,
    WorkExperience,
)
from .inbox import (
    DEFAULT_RESUME_INBOX,
    InboxResume,
    ResumeInboxError,
    find_resume_pdf,
    process_inbox_resume,
)
from .parser import parse_resume_text
from .pdf_reader import (
    PdfTextDocument,
    ResumePdfEmptyTextError,
    ResumePdfError,
    ResumePdfNotFoundError,
    normalize_extracted_text,
    read_pdf_text,
)
from .service import ResumeProcessResult, process_resume
from .storage import ResumeSaveError, SavedResumeArtifacts, save_resume_bundle

__all__ = [
    "BasicInfo",
    "ContactInfo",
    "DEFAULT_RESUME_INBOX",
    "EducationEntry",
    "InternshipExperience",
    "InboxResume",
    "PdfTextDocument",
    "ProjectExperience",
    "ResumeData",
    "ResumePdfEmptyTextError",
    "ResumePdfError",
    "ResumePdfNotFoundError",
    "normalize_extracted_text",
    "ResumeInboxError",
    "ResumeProcessResult",
    "ResumeSaveError",
    "SavedResumeArtifacts",
    "SkillSet",
    "WorkExperience",
    "parse_resume_text",
    "find_resume_pdf",
    "process_inbox_resume",
    "process_resume",
    "read_pdf_text",
    "save_resume_bundle",
]
