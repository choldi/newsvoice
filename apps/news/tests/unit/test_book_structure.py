"""Unit tests for structural book parsing and delivery profiles."""

from __future__ import annotations

from src.api.services.book_structure import (
    BookBlock,
    BookRole,
    blocks_from_markdown,
    blocks_from_plain_text,
    chapters_from_blocks,
    classify_heading,
    profile_for,
)


def test_classify_heading_recognises_spanish_and_english_front_matter() -> None:
    """Known section names win over the heading level."""
    assert classify_heading("Dedicatoria") is BookRole.DEDICATION
    assert classify_heading("Tabla de contenidos") is BookRole.TOC
    assert classify_heading("Table of Contents") is BookRole.TOC
    assert classify_heading("Copyright") is BookRole.COPYRIGHT
    assert classify_heading("Prólogo") is BookRole.FOREWORD
    assert classify_heading("Introducción") is BookRole.INTRODUCTION
    assert classify_heading("Epílogo") is BookRole.AFTERWORD
    assert classify_heading("Capítulo 7") is BookRole.CHAPTER


def test_markdown_paragraphs_inherit_the_front_matter_role() -> None:
    """A dedication's prose is delivered as dedication, not generic body."""
    blocks = blocks_from_markdown("# Dedicatoria\n\nPara Ana.\n\n# Capítulo 1\n\nHabía una vez.")

    assert [block.role for block in blocks] == [
        BookRole.DEDICATION,
        BookRole.DEDICATION,
        BookRole.CHAPTER,
        BookRole.BODY,
    ]


def test_markdown_title_page_uses_document_metadata() -> None:
    """The document title is recognised as the title page heading."""
    blocks = blocks_from_markdown(
        "# La sombra del viento\n\nTexto.", book_title="La sombra del viento"
    )

    assert blocks[0].role is BookRole.BOOK_TITLE
    assert blocks[0].text == "La sombra del viento"
    assert blocks[1].role is BookRole.BODY


def test_markdown_strips_formatting_but_keeps_speech_text() -> None:
    """Links, emphasis and list markers do not reach the engine."""
    blocks = blocks_from_markdown(
        "# Capítulo 1\n\nEsto es **importante** y [un enlace](https://x.com).\n\n- Uno\n- Dos"
    )

    assert blocks[0].title == "Capítulo 1"
    assert blocks[1].text == "Esto es importante y un enlace."
    assert blocks[2].text == "Uno Dos"


def test_numbered_lists_are_not_mistaken_for_footnotes() -> None:
    """Ordered lists keep the body profile; only bracketed markers are notes."""
    blocks = blocks_from_markdown("# Capítulo 1\n\n1. Primero\n2. Segundo")

    assert [block.role for block in blocks] == [BookRole.CHAPTER, BookRole.BODY]
    assert blocks[1].text == "Primero Segundo"


def test_plain_text_recognises_headings_without_markdown() -> None:
    """Uppercase and known section headings are detected in PDF-style text."""
    blocks = blocks_from_plain_text("DEDICATORIA\n\nPara ti.\n\nCAPÍTULO UNO\n\nHabía una vez.")

    assert [(block.role, block.text) for block in blocks] == [
        (BookRole.DEDICATION, "DEDICATORIA"),
        (BookRole.DEDICATION, "Para ti."),
        (BookRole.CHAPTER, "CAPÍTULO UNO"),
        (BookRole.BODY, "Había una vez."),
    ]


def test_plain_text_detects_heading_lines_without_blank_lines() -> None:
    """PDF-style pages often separate the heading from prose with one newline."""
    blocks = blocks_from_plain_text("DEDICATORIA\nPara ti.\n\nCAPÍTULO UNO\nHabía una vez.")

    assert [(block.role, block.text) for block in blocks] == [
        (BookRole.DEDICATION, "DEDICATORIA"),
        (BookRole.DEDICATION, "Para ti."),
        (BookRole.CHAPTER, "CAPÍTULO UNO"),
        (BookRole.BODY, "Había una vez."),
    ]


def test_chapters_group_by_headings_instead_of_splitting_every_paragraph() -> None:
    """Body paragraphs stay in one chapter; only headings start a new one."""
    blocks = blocks_from_markdown("# Uno\n\nA\n\nB\n\n# Dos\n\nC")
    chapters = chapters_from_blocks(blocks)

    assert [chapter.title for chapter in chapters] == ["Uno", "Dos"]
    assert [len(chapter.blocks) for chapter in chapters] == [3, 2]


def test_title_page_gets_its_own_chapter() -> None:
    """The title page opens a chapter named after the book, not "Capítulo 1"."""
    blocks = blocks_from_markdown(
        "# La sombra del viento\n\n## Un misterio\n\nAutor\n\n# Copyright\n\nTexto.",
        book_title="La sombra del viento",
    )
    chapters = chapters_from_blocks(blocks)

    assert [chapter.title for chapter in chapters] == ["La sombra del viento", "Copyright"]
    assert [len(chapter.blocks) for chapter in chapters] == [3, 2]


def test_chapters_split_headingless_text_by_size() -> None:
    """A long unstructured document still becomes several bounded chapters."""
    blocks = [
        BookBlock(role=BookRole.BODY, text="A" * 60),
        BookBlock(role=BookRole.BODY, text="B" * 60),
    ]
    chapters = chapters_from_blocks(blocks, max_chars=80, default_title="Parte")

    assert len(chapters) == 2
    assert chapters[0].title == "Parte 1"


def test_profiles_vary_by_role_but_stay_restrained() -> None:
    """Headings are slower and pause longer than body prose."""
    title = profile_for(BookRole.BOOK_TITLE)
    body = profile_for(BookRole.BODY)

    assert title.speed < body.speed
    assert title.pause_after_ms > body.pause_after_ms
    assert title.emotion == "calm"
    assert body.emotion is None


def test_block_serialization_round_trips_through_a_temporal_payload() -> None:
    """Unknown roles degrade to body instead of crashing an activity."""
    block = BookBlock(role=BookRole.PREFACE, text="Hola", title="Prefacio", level=1)

    assert BookBlock.from_dict(block.to_dict()) == block
    assert BookBlock.from_dict({"role": "not-a-role", "text": "Hola"}).role is BookRole.BODY
