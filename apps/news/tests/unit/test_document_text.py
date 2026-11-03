"""Unit tests for the Phase 7.11 document text extraction."""

from __future__ import annotations

import io

import pytest
from ebooklib import epub

from src.api.services.book_structure import BookRole
from src.api.services.document_text import (
    DocumentExtractionError,
    extract_document,
    sniff_kind,
)


def build_pdf(text: str) -> bytes:
    """Return a minimal valid one-page PDF carrying ``text`` in a Tj operator.

    pypdf needs a real xref table, so the offsets are computed while writing.

    Args:
        text: Text to place on the page (latin-1, no parentheses).

    Returns:
        Raw PDF bytes.
    """
    content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        (
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
            b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>"
        ),
        b"<</Length %d>>stream\n%s\nendstream" % (len(content), content),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref_pos}\n%%EOF"
    ).encode()
    return bytes(out)


def build_epub(chapters: list[tuple[str, str]], title: str = "Libro de prueba") -> bytes:
    """Build an EPUB in memory with the given (title, html-body) chapters.

    Args:
        chapters: (chapter title, body html) pairs, in reading order.
        title: The book title metadata.

    Returns:
        Raw EPUB bytes.
    """
    book = epub.EpubBook()
    book.set_identifier("id-test-1")
    book.set_title(title)
    book.set_language("es")
    items: list[epub.EpubHtml] = []
    for index, (chapter_title, body) in enumerate(chapters, start=1):
        item = epub.EpubHtml(title=chapter_title, file_name=f"ch{index}.xhtml", lang="es")
        item.content = f"<h1>{chapter_title}</h1><p>{body}</p>"
        book.add_item(item)
        items.append(item)
    book.toc = tuple(items)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *items]
    buffer = io.BytesIO()
    epub.write_epub(buffer, book)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# sniff_kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "kind"),
    [("a.pdf", "pdf"), ("a.PDF", "pdf"), ("a.epub", "epub"), ("a.txt", "txt"), ("a.md", "md")],
)
def test_sniff_kind_accepts_supported_types(filename: str, kind: str) -> None:
    """Supported extensions map to their kind, case-insensitively."""
    assert sniff_kind(filename) == kind


@pytest.mark.parametrize("filename", ["a.docx", "a.zip", "a", "a.html", ""])
def test_sniff_kind_rejects_unsupported_types(filename: str) -> None:
    """Anything else is a permanent, explained failure."""
    with pytest.raises(DocumentExtractionError, match="unsupported file type"):
        sniff_kind(filename)


# ---------------------------------------------------------------------------
# extract_document
# ---------------------------------------------------------------------------


def test_extract_txt_decodes_and_titles_from_filename() -> None:
    """A plain text file becomes a document titled after its stem."""
    document = extract_document(b"Primera linea.\n\nSegunda linea.\n", "mi_noticia.txt")

    assert document.kind == "txt"
    assert document.text == "Primera linea.\n\nSegunda linea."
    assert document.title == "mi noticia"
    assert document.char_count == len(document.text)


def test_extract_md_strips_markdown() -> None:
    """Headings, bold and links do not survive into the speech text."""
    markdown = "# Titulo\n\nEsto es **importante** y [un enlace](https://x.com).\n\n- item uno\n"
    document = extract_document(markdown.encode(), "clase.md")

    assert document.kind == "md"
    assert "#" not in document.text
    assert "**" not in document.text
    assert "https://" not in document.text
    assert "Esto es importante y un enlace." in document.text
    assert "item uno" in document.text


def test_extract_md_reports_structural_blocks() -> None:
    """Markdown headings and front matter survive alongside the speech text."""
    markdown = "# Dedicatoria\n\nPara Ana.\n\n# Capítulo 1\n\nHabía una vez.\n"
    document = extract_document(markdown.encode(), "libro.md")

    assert [block.role for block in document.blocks] == [
        BookRole.DEDICATION,
        BookRole.DEDICATION,
        BookRole.CHAPTER,
        BookRole.BODY,
    ]
    assert document.blocks[0].title == "Dedicatoria"
    assert document.text.startswith("Dedicatoria")


def test_extract_epub_reports_headings_and_front_matter_prose() -> None:
    """EPUB headings are not flattened into anonymous paragraphs."""
    data = build_epub([("Dedicatoria", "Para ti."), ("Capítulo 1", "Hola mundo.")])
    document = extract_document(data, "libro.epub")

    roles = [block.role for block in document.blocks]
    assert roles[:2] == [BookRole.DEDICATION, BookRole.DEDICATION]
    assert roles[-2:] == [BookRole.CHAPTER, BookRole.BODY]
    assert document.blocks[2].title == "Capítulo 1"


def test_extract_pdf_reads_page_text_and_falls_back_to_stem() -> None:
    """PDF pages are read in order; metadata has no title, so the stem is used."""
    data = build_pdf("El consejo aprobo un crecimiento del 3,5 por ciento")
    document = extract_document(data, "presupuesto_2027.pdf")

    assert document.kind == "pdf"
    assert "3,5" in document.text
    assert document.title == "presupuesto 2027"
    assert document.blocks and document.blocks[0].text in document.text


def test_extract_pdf_without_text_is_rejected() -> None:
    """A PDF with pages but no extractable text (scan) fails explicitly."""
    data = build_pdf("x").replace(b"(x)", b"")
    with pytest.raises(DocumentExtractionError, match="no extractable text"):
        extract_document(data, "scan.pdf")


def test_extract_pdf_rejects_a_corrupt_file() -> None:
    """Garbage bytes are reported as an unreadable PDF, not a crash."""
    with pytest.raises(DocumentExtractionError, match="unreadable PDF"):
        extract_document(b"%PDF-1.4 this is not a pdf", "broken.pdf")


def test_extract_epub_follows_the_spine_and_skips_navigation() -> None:
    """Chapters come in reading order; nav labels are not read aloud."""
    data = build_epub(
        [
            ("Introduccion", "Buenos dias, hoy estudiamos el subjuntivo."),
            ("Ejercicios", "Completa la frase con el verbo correcto."),
        ],
        title="Clase de espanol",
    )
    document = extract_document(data, "apuntes.epub")

    assert document.kind == "epub"
    assert document.title == "Clase de espanol"
    intro = document.text.index("Buenos dias")
    exercises = document.text.index("Completa la frase")
    assert intro < exercises, "chapters must follow the spine order"
    # The navigation document repeats every chapter title as a link label: if
    # it leaked into the text each title would appear twice.
    assert document.text.count("Introduccion") == 1
    assert document.text.count("Ejercicios") == 1


def test_extract_epub_rejects_a_corrupt_file() -> None:
    """Garbage bytes are reported as an unreadable EPUB."""
    with pytest.raises(DocumentExtractionError, match="unreadable EPUB"):
        extract_document(b"PK\x03\x04 not really an epub", "broken.epub")


def test_extract_rejects_empty_bytes() -> None:
    """An empty upload is refused before any parser runs."""
    with pytest.raises(DocumentExtractionError, match="empty"):
        extract_document(b"", "nota.txt")


def test_extract_rejects_a_document_over_the_char_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents beyond doc_tts_max_source_chars fail with the numbers in hand."""
    monkeypatch.setattr("src.core.config.settings.doc_tts_max_source_chars", 10_000)
    payload = ("palabra " * 5000).encode()
    with pytest.raises(DocumentExtractionError, match="too long"):
        extract_document(payload, "gigante.txt")


def test_extract_rejects_a_blank_text_file() -> None:
    """Whitespace-only content has nothing to speak."""
    with pytest.raises(DocumentExtractionError, match="no text content"):
        extract_document(b"   \n\n  \n", "vacio.txt")
