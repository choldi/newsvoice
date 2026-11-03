"""Structural book parsing and delivery profiles for audiobook narration.

The audiobook pipeline used to flatten every document into one string. That
works for reading, but a narrator needs to know what is a title, a copyright
page, a dedication, a table of contents, a chapter heading or a paragraph:
each of those is delivered differently and separated by a different pause.

This module is deliberately independent of the TTS engines. It turns text into
:class:`BookBlock` objects, groups those blocks into chapters and maps every
role to a :class:`DeliveryProfile`. The audiobook service then synthesizes each
block separately and joins the audio with the pauses the profile asks for.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "BookBlock",
    "BookChapter",
    "BookRole",
    "DeliveryProfile",
    "PROFILE_VERSION",
    "blocks_from_markdown",
    "blocks_from_plain_text",
    "chapters_from_blocks",
    "classify_heading",
    "profile_for",
]


class BookRole(StrEnum):
    """Structural role of one speech block."""

    BOOK_TITLE = "book_title"
    SUBTITLE = "subtitle"
    AUTHOR = "author"
    COPYRIGHT = "copyright"
    DEDICATION = "dedication"
    EPIGRAPH = "epigraph"
    TOC = "toc"
    FOREWORD = "foreword"
    PREFACE = "preface"
    INTRODUCTION = "introduction"
    PART = "part"
    CHAPTER = "chapter"
    SUBHEADING = "subheading"
    BODY = "body"
    FOOTNOTE = "footnote"
    AFTERWORD = "afterword"
    APPENDIX = "appendix"
    ACKNOWLEDGMENTS = "acknowledgments"
    ABOUT_AUTHOR = "about_author"
    BACK_MATTER = "back_matter"


@dataclass(frozen=True)
class BookBlock:
    """One speech-ready block with its structural role.

    Attributes:
        role: How the block must be delivered.
        text: Exact text handed to the engine.
        title: Heading text for chapter markers (empty for body paragraphs).
        level: Markdown/HTML heading level, 0 for non-headings.
    """

    role: BookRole
    text: str
    title: str = ""
    level: int = 0

    def to_dict(self) -> dict[str, str | int]:
        """Serialize the block for a Temporal payload."""
        return {
            "role": str(self.role),
            "text": self.text,
            "title": self.title,
            "level": int(self.level),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> BookBlock:
        """Rebuild a block from a serialized Temporal payload."""
        try:
            role = BookRole(str(data.get("role") or BookRole.BODY))
        except ValueError:
            role = BookRole.BODY
        return cls(
            role=role,
            text=str(data.get("text") or ""),
            title=str(data.get("title") or ""),
            level=int(data.get("level") or 0),
        )


@dataclass(frozen=True)
class BookChapter:
    """A group of blocks rendered as one M4B chapter."""

    title: str
    blocks: tuple[BookBlock, ...]
    explicit: bool = False

    @property
    def text(self) -> str:
        """Return the chapter text with headings and paragraphs joined."""
        return "\n\n".join(block.text for block in self.blocks if block.text)


@dataclass(frozen=True)
class DeliveryProfile:
    """How one structural role is spoken."""

    speed: float = 1.0
    pitch_hz: float = 0.0
    emotion: str | None = None
    pause_after_ms: int = 500
    sentence_pause_ms: int = 130


#: Bump when the default profiles change so cached block audio is rebuilt.
PROFILE_VERSION = "1"

#: Profiles are intentionally restrained: a book should sound narrated, not
#: acted. The largest differences are the slower headings and the pauses.
_PROFILES: dict[BookRole, DeliveryProfile] = {
    BookRole.BOOK_TITLE: DeliveryProfile(0.90, -2.0, "calm", 1500, 220),
    BookRole.SUBTITLE: DeliveryProfile(0.92, -1.0, "calm", 1100, 200),
    BookRole.AUTHOR: DeliveryProfile(0.92, 0.0, "calm", 1100, 180),
    BookRole.COPYRIGHT: DeliveryProfile(1.03, -1.0, None, 700, 110),
    BookRole.DEDICATION: DeliveryProfile(0.88, 0.0, "calm", 1400, 220),
    BookRole.EPIGRAPH: DeliveryProfile(0.90, 0.0, "calm", 1400, 200),
    BookRole.TOC: DeliveryProfile(1.00, 0.0, None, 1000, 220),
    BookRole.FOREWORD: DeliveryProfile(0.95, 0.0, "calm", 1000, 160),
    BookRole.PREFACE: DeliveryProfile(0.94, 0.0, "calm", 1000, 160),
    BookRole.INTRODUCTION: DeliveryProfile(0.95, 0.0, "calm", 1000, 150),
    BookRole.PART: DeliveryProfile(0.90, -1.0, "calm", 1500, 220),
    BookRole.CHAPTER: DeliveryProfile(0.92, -1.0, "calm", 1200, 180),
    BookRole.SUBHEADING: DeliveryProfile(0.95, -1.0, None, 700, 150),
    BookRole.BODY: DeliveryProfile(1.00, 0.0, None, 500, 130),
    BookRole.FOOTNOTE: DeliveryProfile(1.04, -1.0, None, 350, 100),
    BookRole.AFTERWORD: DeliveryProfile(0.95, 0.0, "calm", 1000, 160),
    BookRole.APPENDIX: DeliveryProfile(1.00, 0.0, None, 800, 140),
    BookRole.ACKNOWLEDGMENTS: DeliveryProfile(0.98, 0.0, "calm", 800, 150),
    BookRole.ABOUT_AUTHOR: DeliveryProfile(0.98, 0.0, "calm", 800, 150),
    BookRole.BACK_MATTER: DeliveryProfile(1.00, 0.0, None, 700, 130),
}


def profile_for(role: BookRole) -> DeliveryProfile:
    """Return the default delivery profile for a structural role."""
    return _PROFILES.get(role, _PROFILES[BookRole.BODY])


# Heading aliases are checked longest-first so "tabla de contenidos" wins over
# "contenido" and "sobre el autor" wins over a generic "autor" if one is added.
_HEADING_ALIASES: tuple[tuple[BookRole, tuple[str, ...]], ...] = (
    (
        BookRole.TOC,
        (
            "tabla de contenidos",
            "tabla de contenido",
            "indice general",
            "indice de contenidos",
            "table of contents",
            "table of content",
            "contenidos",
            "contenido",
            "indice",
            "contents",
            "sumario",
        ),
    ),
    (
        BookRole.COPYRIGHT,
        (
            "derechos de autor",
            "creditos editoriales",
            "pagina de creditos",
            "nota legal",
            "legal notice",
            "copyright",
            "creditos",
        ),
    ),
    (
        BookRole.ABOUT_AUTHOR,
        (
            "sobre el autor",
            "sobre la autora",
            "biografia del autor",
            "biografia de la autora",
            "about the author",
            "about the authors",
            "sobre los autores",
        ),
    ),
    (
        BookRole.ACKNOWLEDGMENTS,
        (
            "agradecimientos",
            "reconocimientos",
            "acknowledgments",
            "acknowledgements",
        ),
    ),
    (
        BookRole.FOREWORD,
        ("prologo", "prologue", "foreword"),
    ),
    (
        BookRole.PREFACE,
        ("prefacio", "preface"),
    ),
    (
        BookRole.INTRODUCTION,
        ("introduccion", "introduction"),
    ),
    (
        BookRole.AFTERWORD,
        (
            "posfacio",
            "postfacio",
            "epilogo",
            "epilogue",
            "afterword",
        ),
    ),
    (
        BookRole.DEDICATION,
        ("dedicatoria", "dedication"),
    ),
    (
        BookRole.EPIGRAPH,
        ("epigrafe", "epigraph"),
    ),
    (
        BookRole.APPENDIX,
        ("apendice", "apendices", "appendix", "anexo", "anexos"),
    ),
    (
        BookRole.BACK_MATTER,
        (
            "indice analitico",
            "bibliografia",
            "bibliography",
            "glosario",
            "glossary",
            "notas",
            "notes",
        ),
    ),
    (
        BookRole.PART,
        ("parte", "part", "libro", "book"),
    ),
    (
        BookRole.CHAPTER,
        ("capitulo", "chapter"),
    ),
    (
        BookRole.SUBTITLE,
        ("subtitulo", "subtitle"),
    ),
    (
        BookRole.AUTHOR,
        ("autor", "autora", "author"),
    ),
    (
        BookRole.BOOK_TITLE,
        ("titulo", "title"),
    ),
)


def _normalize(text: str) -> str:
    """Fold accents, case and punctuation for heading matching."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _matches_alias(normalized: str, alias: str) -> bool:
    """Return True when a heading starts with the alias on a word boundary."""
    return normalized == alias or normalized.startswith(f"{alias} ")


def classify_heading(text: str, level: int = 1, book_title: str = "") -> BookRole:
    """Infer the role of a heading.

    Known front/back-matter terms win over the heading level, because a book
    may mark "Dedicatoria" as ``h1`` or ``h2``. An exact match with the book
    metadata title is treated as the title page.

    Args:
        text: Heading text.
        level: HTML/Markdown heading level (1-6).
        book_title: Document metadata title, used to recognise the title page.

    Returns:
        The matching role, or ``CHAPTER`` for an unknown top-level heading and
        ``SUBHEADING`` for a deeper one.
    """
    normalized = _normalize(text)
    if not normalized:
        return BookRole.BODY

    title_key = _normalize(book_title)
    if title_key and (normalized == title_key or _matches_alias(title_key, normalized)):
        return BookRole.BOOK_TITLE

    for role, aliases in _HEADING_ALIASES:
        if any(_matches_alias(normalized, alias) for alias in aliases):
            return role

    return BookRole.CHAPTER if level <= 1 else BookRole.SUBHEADING


def _known_heading(text: str, level: int, book_title: str) -> BookRole | None:
    """Return a role only when the heading is explicitly recognised."""
    role = classify_heading(text, level, book_title)
    if role not in (BookRole.CHAPTER, BookRole.SUBHEADING):
        return role
    normalized = _normalize(text)
    if normalized and len(text) <= 100 and text.upper() == text and any(c.isalpha() for c in text):
        return role
    return None


_INLINE_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_BOLD = re.compile(r"(\*\*|__)(.+?)\1")
_ITALIC = re.compile(r"(\*|_)(.+?)\1")
_CODE = re.compile(r"`([^`]+)`")
_LIST_MARKER = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_BLOCKQUOTE = re.compile(r"^\s*>\s?")
_FOOTNOTE_START = re.compile(r"^\s*\[\d+\]\s+")


def _clean_inline(text: str) -> str:
    """Strip inline Markdown while keeping the spoken wording."""
    text = _INLINE_LINK.sub(r"\1", text)
    text = _BOLD.sub(r"\2", text)
    text = _ITALIC.sub(r"\2", text)
    text = _CODE.sub(r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_block_line(line: str) -> str:
    """Strip list/quote markers from one Markdown line."""
    line = _BLOCKQUOTE.sub("", line)
    return _LIST_MARKER.sub("", line).strip()


def _paragraph_block(paragraph: str, role: BookRole = BookRole.BODY) -> BookBlock:
    """Build a paragraph block, keeping the enclosing section's delivery role."""
    text = _clean_inline(" ".join(_clean_block_line(line) for line in paragraph.splitlines()))
    if _FOOTNOTE_START.match(text):
        role = BookRole.FOOTNOTE
    return BookBlock(role=role, text=text)


def _section_body_role(role: BookRole) -> BookRole:
    """Return the role to use for prose that follows a section heading.

    Front and back matter keep their own delivery profile for the whole
    section; ordinary chapters switch back to the body profile.
    """
    if role in _FRONT_OR_BACK_MATTER:
        return role
    return BookRole.BODY


_FRONT_OR_BACK_MATTER = frozenset(
    {
        BookRole.COPYRIGHT,
        BookRole.DEDICATION,
        BookRole.EPIGRAPH,
        BookRole.TOC,
        BookRole.FOREWORD,
        BookRole.PREFACE,
        BookRole.INTRODUCTION,
        BookRole.AFTERWORD,
        BookRole.APPENDIX,
        BookRole.ACKNOWLEDGMENTS,
        BookRole.ABOUT_AUTHOR,
        BookRole.BACK_MATTER,
    }
)


def blocks_from_markdown(markdown: str, book_title: str = "") -> list[BookBlock]:
    """Parse Markdown into structural blocks.

    Args:
        markdown: Original Markdown, before speech stripping.
        book_title: Document title, used to recognise the title page.

    Returns:
        Blocks in reading order. Empty for blank input.
    """
    text = (markdown or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[BookBlock] = []
    paragraph: list[str] = []
    section_role = BookRole.BODY

    def flush() -> None:
        if paragraph:
            block = _paragraph_block("\n".join(paragraph), section_role)
            if block.text:
                blocks.append(block)
            paragraph.clear()

    for line in text.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if heading:
            flush()
            title = _clean_inline(heading.group(2))
            if title:
                level = len(heading.group(1))
                role = classify_heading(title, level, book_title)
                section_role = _section_body_role(role)
                blocks.append(
                    BookBlock(
                        role=role,
                        text=title,
                        title=title,
                        level=level,
                    )
                )
            continue
        if not line.strip():
            flush()
            continue
        paragraph.append(line)
    flush()
    return blocks


def _looks_like_plain_heading(paragraph: str, book_title: str) -> BookRole | None:
    """Recognise a heading in plain text without treating every line as one."""
    stripped = paragraph.strip()
    if not stripped or "\n" in stripped or len(stripped) > 100:
        return None
    if stripped[-1] in ".?!,;:":
        return None
    role = _known_heading(stripped, 1, book_title)
    if role is not None:
        return role
    if stripped.isupper() and any(character.isalpha() for character in stripped):
        return BookRole.CHAPTER
    return None


def blocks_from_plain_text(text: str, book_title: str = "") -> list[BookBlock]:
    """Parse plain text/PDF text into paragraphs and recognisable headings.

    Args:
        text: Extracted plain text.
        book_title: Document title, used to recognise the title page.

    Returns:
        Blocks in reading order. Empty for blank input.
    """
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
    blocks: list[BookBlock] = []
    section_role = BookRole.BODY
    for paragraph in paragraphs:
        buffer: list[str] = []

        def flush_buffer() -> None:
            if not buffer:
                return
            block = _paragraph_block("\n".join(buffer), section_role)
            if block.text:
                blocks.append(block)
            buffer.clear()

        for line in paragraph.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            role = _looks_like_plain_heading(stripped, book_title)
            if role is None:
                buffer.append(stripped)
                continue
            flush_buffer()
            title = re.sub(r"\s+", " ", stripped).strip()
            section_role = _section_body_role(role)
            blocks.append(BookBlock(role=role, text=title, title=title, level=1))
        flush_buffer()
    return blocks


#: Roles that begin a new M4B chapter. Subheadings stay inside the chapter.
_SECTION_ROLES = frozenset(
    {
        BookRole.BOOK_TITLE,
        BookRole.COPYRIGHT,
        BookRole.DEDICATION,
        BookRole.EPIGRAPH,
        BookRole.TOC,
        BookRole.FOREWORD,
        BookRole.PREFACE,
        BookRole.INTRODUCTION,
        BookRole.PART,
        BookRole.CHAPTER,
        BookRole.AFTERWORD,
        BookRole.APPENDIX,
        BookRole.ACKNOWLEDGMENTS,
        BookRole.ABOUT_AUTHOR,
        BookRole.BACK_MATTER,
    }
)


def chapters_from_blocks(
    blocks: list[BookBlock],
    *,
    max_chars: int = 8000,
    default_title: str = "Capítulo",
) -> list[BookChapter]:
    """Group blocks into chapters, splitting only unstructured long text.

    Args:
        blocks: Blocks in reading order.
        max_chars: Maximum size of an implicit (heading-less) chapter.
        default_title: Title prefix for chapters without a heading.

    Returns:
        Chapters in reading order. Empty when there are no non-empty blocks.
    """
    chapters: list[BookChapter] = []
    current: list[BookBlock] = []
    current_title = ""
    explicit = False
    current_chars = 0

    def flush() -> None:
        nonlocal current, current_title, explicit, current_chars
        if current:
            chapters.append(
                BookChapter(
                    title=current_title or f"{default_title} {len(chapters) + 1}",
                    blocks=tuple(current),
                    explicit=explicit,
                )
            )
        current = []
        current_title = ""
        explicit = False
        current_chars = 0

    for block in blocks:
        if not block.text.strip():
            continue
        if block.role in _SECTION_ROLES and block.title:
            flush()
            current_title = block.title or block.text
            explicit = True
            current.append(block)
            current_chars += len(block.text)
            continue

        if not current:
            current_title = ""
            explicit = False
        elif not explicit and current_chars + len(block.text) > max_chars:
            flush()
        current.append(block)
        current_chars += len(block.text)

    flush()
    return chapters
