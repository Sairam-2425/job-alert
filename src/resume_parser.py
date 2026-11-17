"""Extract plain text from PDF / DOCX resumes."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SUPPORTED = {".pdf", ".docx"}
MIN_TEXT_CHARS = 200


class ResumeError(Exception):
    pass


@dataclass
class ParsedResume:
    path: str
    text: str
    sha256: str  # hash of the *file bytes* - used to detect resume changes


def _pdf_text(path: Path) -> str:
    import fitz  # PyMuPDF

    parts = []
    with fitz.open(path) as doc:
        for page in doc:
            parts.append(page.get_text("text", sort=True))
    return "\n".join(parts)


def _docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:  # many resumes use tables for layout
        for row in table.rows:
            seen = set()
            for cell in row.cells:
                if cell._tc in seen:  # merged cells repeat
                    continue
                seen.add(cell._tc)
                if cell.text.strip():
                    parts.append(cell.text.strip())
    return "\n".join(parts)


def find_resume(directory: Path) -> Optional[Path]:
    """Return the most recently modified PDF/DOCX in `directory`, or None."""
    if not directory.is_dir():
        return None
    files = [p for p in directory.iterdir() if p.suffix.lower() in SUPPORTED and not p.name.startswith("~$")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def parse_resume(path: Path) -> ParsedResume:
    path = Path(path)
    ext = path.suffix.lower()
    if ext not in SUPPORTED:
        raise ResumeError(f"Unsupported resume type '{ext}'. Use PDF or DOCX.")
    if not path.is_file():
        raise ResumeError(f"Resume not found: {path}")
    try:
        text = _pdf_text(path) if ext == ".pdf" else _docx_text(path)
    except Exception as exc:  # corrupt/encrypted files raise various library errors
        raise ResumeError(f"Could not read {path.name}: {exc}") from exc
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise ResumeError(
            f"Only {len(text)} characters extracted from {path.name}. "
            "Scanned/image-only PDFs are not supported - export a text-based PDF or DOCX."
        )
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    log.info("Parsed resume", extra={"ctx": {"file": path.name, "chars": len(text)}})
    return ParsedResume(path=str(path), text=text, sha256=digest)
