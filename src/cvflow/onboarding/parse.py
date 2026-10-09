"""Extract plain text from an uploaded résumé (PDF, DOCX, TXT/MD)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import docx
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

MAX_BYTES = 5 * 1024 * 1024
_MIN_PDF_CHARS = 50


class ResumeParseError(Exception):
    """Raised when a résumé cannot be read into text."""


@dataclass(frozen=True)
class ParsedResume:
    text: str
    kind: str
    warnings: list[str]


def _pdf_text(path: Path) -> str:
    reader = PdfReader(path)
    text = "\n".join(p.extract_text(extraction_mode="layout") for p in reader.pages)
    if len(text.strip()) < _MIN_PDF_CHARS:
        text = "\n".join(p.extract_text() for p in reader.pages)
    if not text.strip():
        raise ResumeParseError("no extractable text — is this a scanned PDF?")
    return text


def _docx_text(path: Path) -> str:
    document: DocxDocument = docx.Document(str(path))
    lines: list[str] = []
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            lines.append(Paragraph(child, document).text)
        elif child.tag.endswith("}tbl"):
            table = Table(child, document)
            for row in table.rows:
                lines.append(" | ".join(cell.text for cell in row.cells))
    return "\n".join(lines)


def parse_resume(path: str | Path) -> ParsedResume:
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in (".pdf", ".docx", ".txt", ".md"):
        raise ResumeParseError(f"unsupported résumé file type: {ext or p.name!r}")
    if p.stat().st_size > MAX_BYTES:
        raise ResumeParseError(f"{p.name} exceeds the {MAX_BYTES // (1024 * 1024)} MB limit")
    if ext == ".pdf":
        return ParsedResume(_pdf_text(p), "pdf", [])
    if ext == ".docx":
        return ParsedResume(_docx_text(p), "docx", [])
    text = p.read_text()
    if not text.strip():
        raise ResumeParseError(f"{p.name} is empty")
    return ParsedResume(text, "text", [])


def parse_resumes(paths: list[str | Path]) -> ParsedResume:
    parts: list[str] = []
    warnings: list[str] = []
    for path in paths:
        parsed = parse_resume(path)
        parts.append(f"\n\n=== FILE: {Path(path).name} ===\n\n{parsed.text}")
        warnings.extend(parsed.warnings)
    return ParsedResume("".join(parts), "mixed", warnings)
