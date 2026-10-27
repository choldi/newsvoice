"""Pydantic models for the phonetic-adaptation heuristics (Phase 7.16).

One row per language pair: how to recognise terms of ``language_origin``
inside text read in ``language_target``. The same rows feed the narration
respeller (Phase 7.15) and the lesson phonetic adaptation, so the shape is
shared by both consumers.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, field_validator, model_validator

#: Two-letter ISO 639-1 code, exactly what the lesson form already sends.
_LANGUAGE_RE = re.compile(r"[a-z]{2}")


def _clean_codes(values: list[str]) -> list[str]:
    """Trim, drop blanks and de-duplicate a word list preserving order.

    Args:
        values: Raw list (form lines, JSON array items).

    Returns:
        The cleaned list.
    """
    seen: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in seen:
            seen.append(item)
    return seen


class PhoneticHeuristicsIn(BaseModel):
    """Create/update payload of one language-pair row (admin form).

    Attributes:
        language_origin: ISO 639-1 language the foreign terms come from.
        language_target: ISO 639-1 language the text is read in.
        language_origin_name: Prompt/display name of the origin language.
        language_target_name: Prompt/display name of the read language.
        suffixes: Word endings that flag an origin-shaped token.
        digraphs: Letter pairs that flag an origin-shaped token anywhere.
        tech_terms: Brand/tech words flagged verbatim.
        native_words: Stop list: never offered to the LLM as candidates.
        examples: Few-shot ``{term: respelling}`` pairs for the prompt.
        ascii_only: Only consider ASCII tokens (English has no accents).
        flag_all_foreign: Offer every non-native word as a candidate, letting
            the LLM decide instead of the shape heuristic.
        enabled: Disabled rows make the pair skip respelling entirely.
    """

    language_origin: str = Field(..., description="ISO 639-1 language of the terms")
    language_target: str = Field(..., description="ISO 639-1 language reading them")
    language_origin_name: str = Field(default="", max_length=64)
    language_target_name: str = Field(default="", max_length=64)
    suffixes: list[str] = Field(default_factory=list, max_length=200)
    digraphs: list[str] = Field(default_factory=list, max_length=200)
    tech_terms: list[str] = Field(default_factory=list, max_length=500)
    native_words: list[str] = Field(default_factory=list, max_length=2000)
    examples: dict[str, str] = Field(default_factory=dict, max_length=200)
    ascii_only: bool = True
    flag_all_foreign: bool = False
    enabled: bool = True

    @field_validator("language_origin", "language_target")
    @classmethod
    def _language_is_iso639_1(cls, value: str) -> str:
        """Normalize a language code to a lowercase two-letter ISO 639-1.

        Args:
            value: Raw code from the form.

        Returns:
            The trimmed, lowercase code.

        Raises:
            ValueError: If it is not a two-letter alphabetic code.
        """
        code = (value or "").strip().lower()
        if not _LANGUAGE_RE.fullmatch(code):
            raise ValueError(f"language must be a two-letter ISO 639-1 code: {value!r}")
        return code

    @field_validator("suffixes", "digraphs", "tech_terms", "native_words", mode="before")
    @classmethod
    def _clean_list(cls, value: object) -> list[str]:
        """Accept a JSON array or a raw string of newline/comma-separated words.

        Args:
            value: List, JSON text or plain text from a textarea.

        Returns:
            The cleaned list of trimmed, unique, non-empty strings.

        Raises:
            ValueError: When a JSON-looking value does not parse.
        """
        if value is None:
            return []
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON list: {exc}") from exc
                if not isinstance(parsed, list):
                    raise ValueError("expected a JSON array of strings")
                value = parsed
            else:
                value = [part for part in text.replace(",", "\n").splitlines()]
        if not isinstance(value, list):
            raise ValueError("expected a list of strings")
        return _clean_codes([str(item) for item in value])

    @field_validator("examples", mode="before")
    @classmethod
    def _clean_examples(cls, value: object) -> dict[str, str]:
        """Accept a JSON object or ``term = respelling`` lines from a textarea.

        Args:
            value: Mapping, JSON text or plain text of ``key=value`` lines.

        Returns:
            The cleaned ``{term: respelling}`` map (blank values dropped).

        Raises:
            ValueError: When a JSON-looking value does not parse as an object.
        """
        if value is None:
            return {}
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("{"):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON object: {exc}") from exc
                if not isinstance(parsed, dict):
                    raise ValueError("expected a JSON object of term -> respelling")
                value = parsed
            else:
                pairs: dict[str, str] = {}
                for line in text.splitlines():
                    if "=" not in line:
                        continue
                    key, _, raw = line.partition("=")
                    if key.strip() and raw.strip():
                        pairs[key.strip()] = raw.strip()
                return pairs
        if not isinstance(value, dict):
            raise ValueError("expected a mapping of term -> respelling")
        return {
            str(term).strip(): str(respelling).strip()
            for term, respelling in value.items()
            if str(term).strip() and str(respelling).strip()
        }

    @model_validator(mode="after")
    def _pair_is_sensible(self) -> PhoneticHeuristicsIn:
        """Reject a pair that cannot adapt anything.

        Returns:
            The validated model.

        Raises:
            ValueError: When origin and target are the same language.
        """
        if self.language_origin == self.language_target:
            raise ValueError(
                "language_origin and language_target must differ "
                "(a language does not need adapting to itself)"
            )
        return self


class PhoneticHeuristicsOut(PhoneticHeuristicsIn):
    """One stored heuristics row as the GUI and the respeller read it.

    Attributes:
        id: Primary key of the row.
        created_at: Insertion timestamp (ISO string from SQLite).
        updated_at: Last update timestamp (ISO string from SQLite).
    """

    id: int = Field(..., description="Row id")
    created_at: str = Field(default="", description="Creation timestamp")
    updated_at: str = Field(default="", description="Last update timestamp")

    def prompt_origin_name(self) -> str:
        """Return the origin language name for prompts.

        Returns:
            The configured name, falling back to the ISO code.
        """
        return self.language_origin_name.strip() or self.language_origin

    def prompt_target_name(self) -> str:
        """Return the read-language name for prompts.

        Returns:
            The configured name, falling back to the ISO code.
        """
        return self.language_target_name.strip() or self.language_target
