"""Extract speech-ready plain text from an uploaded document (Phase 7.11).

Supported inputs are PDF (``pypdf``), EPUB (``ebooklib`` + lxml), plain text
and Markdown. The extractor returns the text in **reading order** (PDF page
order, EPUB spine order) already stripped of markup, so the TTS layer can
chunk it without knowing where it came from.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path

import structlog
from ebooklib import ITEM_DOCUMENT, epub
from lxml import html as lxml_html
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.api.services.tts import markdown_to_speech_text
from src.core.config import get_settings

logger = structlog.get_logger(__name__)

EXTENSION_KINDS = {
    ".pdf": "pdf",
    ".epub": "epub",
    ".txt": "txt",
    ".md": "md",
}
# A PDF whose pages yield less text than this is a scan or a form: handing it to
# the TTS would produce silence plus a misleading "success".
MIN_PDF_CHARS = 40


class DocumentExtractionError(Exception):
    """A permanent failure: unsupported, corrupt or unreadable document."""


@dataclass(frozen=True)
class ExtractedDocument:
    """Speech-ready text extracted from one uploaded document."""

    title: str
    text: str
    kind: str
    char_count: int


def sniff_kind(filename: str) -> str:
    """Map an uploaded filename to a supported document kind.

    Args:
        filename: Original filename of the upload.

    Returns:
        One of 'pdf', 'epub', 'txt', 'md'.

    Raises:
        DocumentExtractionError: When the extension is not supported.
    """
    suffix = Path(filename or "").suffix.lower()
    kind = EXTENSION_KINDS.get(suffix)
    if kind is None:
        supported = ", ".join(sorted(EXTENSION_KINDS))
        raise DocumentExtractionError(
            f"unsupported file type '{suffix or filename}': expected {supported}"
        )
    return kind


def _clean(text: str) -> str:
    """Normalize whitespace without touching the wording.

    Args:
        text: Raw extracted text.

    Returns:
        Text with collapsed blank runs and trimmed lines.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _decode(data: bytes) -> str:
    """Decode a text payload, falling back to latin-1 (which never fails).

    Args:
        data: Raw file bytes.

    Returns:
        The decoded text.
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _title_from_filename(filename: str) -> str:
    """Return a human title derived from the filename.

    Args:
        filename: Original filename of the upload.

    Returns:
        The stem with separators normalized, or 'documento' when empty.
    """
    stem = Path(filename or "").stem
    title = re.sub(r"[_\-]+", " ", stem).strip()
    return title[:120] or "documento"


def _extract_pdf(data: bytes, filename: str) -> ExtractedDocument:
    """Extract text page by page from a PDF.

    Args:
        data: Raw PDF bytes.
        filename: Original filename (title fallback).

    Returns:
        The extracted document.

    Raises:
        DocumentExtractionError: When the file is not a readable PDF or no
            page yields text (scanned document).
    """
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except (PdfReadError, ValueError, OSError) as exc:
        raise DocumentExtractionError(f"unreadable PDF: {exc}") from exc
    text = _clean("\n\n".join(page for page in pages if page))
    if not text:
        raise DocumentExtractionError(
            "PDF has no extractable text (scanned document?): OCR is required first"
        )
    title = _title_from_filename(filename)
    try:
        meta_title = (reader.metadata.title if reader.metadata else None) or ""
    except Exception:  # noqa: BLE001 - broken metadata must not sink the job
        meta_title = ""
    if str(meta_title).strip():
        title = str(meta_title).strip()[:120]
    return ExtractedDocument(title=title, text=text, kind="pdf", char_count=len(text))


def _extract_epub(data: bytes, filename: str) -> ExtractedDocument:
    """Extract text from an EPUB following its spine (reading order).

    The navigation document is skipped: reading its link labels aloud is noise.

    Args:
        data: Raw EPUB bytes.
        filename: Original filename (title fallback).

    Returns:
        The extracted document.

    Raises:
        DocumentExtractionError: When the file is not a readable EPUB or it
            has no text content.
    """
    try:
        book = epub.read_epub(io.BytesIO(data))
    except Exception as exc:  # ebooklib raises a wide range of parse errors
        raise DocumentExtractionError(f"unreadable EPUB: {exc}") from exc

    parts: list[str] = []
    for idref, _linear in book.spine or []:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        if isinstance(item, epub.EpubNav) or item.get_name().endswith("nav.xhtml"):
            continue
        raw = item.get_content()
        try:
            chunk = lxml_html.fromstring(raw).text_content()
        except (lxml_html.ParserError, ValueError):
            chunk = _decode(raw) if isinstance(raw, bytes) else str(raw)
        chunk = _clean(chunk)
        if chunk:
            parts.append(chunk)
    text = _clean("\n\n".join(parts))
    if not text:
        raise DocumentExtractionError("EPUB has no text content")
    title = _title_from_filename(filename)
    try:
        titles = book.get_metadata("DC", "title") or []
    except (AttributeError, KeyError, ValueError):
        titles = []
    book_title = str(titles[0][0]).strip() if titles else ""
    if book_title:
        title = book_title[:120]
    return ExtractedDocument(title=title, text=text, kind="epub", char_count=len(text))


def _extract_plain(data: bytes, filename: str, kind: str) -> ExtractedDocument:
    """Decode a TXT/MD payload; Markdown is converted to speech-friendly text.

    Args:
        data: Raw file bytes.
        filename: Original filename (title fallback).
        kind: 'txt' or 'md'.

    Returns:
        The extracted document.

    Raises:
        DocumentExtractionError: When the decoded text is empty.
    """
    text = _clean(_decode(data))
    if kind == "md":
        text = _clean(markdown_to_speech_text(text))
    if not text:
        raise DocumentExtractionError("document has no text content")
    return ExtractedDocument(
        title=_title_from_filename(filename), text=text, kind=kind, char_count=len(text)
    )


def extract_document(data: bytes, filename: str, kind: str | None = None) -> ExtractedDocument:
    """Extract speech-ready text from an uploaded document.

    Args:
        data: Raw upload bytes.
        filename: Original filename (drives the kind and the fallback title).
        kind: Optional pre-sniffed kind; sniffed from the filename when absent.

    Returns:
        The extracted document, already capped by ``doc_tts_max_source_chars``.

    Raises:
        DocumentExtractionError: On an unsupported type, a corrupt file, a
            document without text or one longer than the configured limit.
    """
    if not data:
        raise DocumentExtractionError("uploaded file is empty")
    detected = kind or sniff_kind(filename)
    if detected == "pdf":
        document = _extract_pdf(data, filename)
    elif detected == "epub":
        document = _extract_epub(data, filename)
    else:
        document = _extract_plain(data, filename, detected)

    limit = get_settings().doc_tts_max_source_chars
    if document.char_count > limit:
        raise DocumentExtractionError(
            f"document too long: {document.char_count} characters "
            f"(doc_tts_max_source_chars={limit}); split it into parts"
        )
    logger.info(
        "document_extracted",
        filename=filename,
        kind=document.kind,
        char_count=document.char_count,
        title=document.title,
    )
    return document
