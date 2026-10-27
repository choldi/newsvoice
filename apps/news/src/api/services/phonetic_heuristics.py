"""Phonetic-adaptation heuristics service (Phase 7.16).

Business rules on top of :class:`PhoneticHeuristicsRepository`: payload
validation lives in :mod:`src.api.models.phonetic`, this module only maps
models to rows and back and reports permanent failures as
:class:`HeuristicsError`, the exception both the admin GUI and the lesson
submit path translate into a 4xx.
"""

from __future__ import annotations

import structlog

from src.api.db.database import PhoneticHeuristicsRepository
from src.api.models.phonetic import PhoneticHeuristicsIn, PhoneticHeuristicsOut

logger = structlog.get_logger(__name__)


class HeuristicsError(Exception):
    """A permanent heuristics failure: an unknown id or an unusable payload."""


def _to_out(row: dict) -> PhoneticHeuristicsOut:
    """Convert a decoded repository row into the output model.

    Args:
        row: Row dict from :class:`PhoneticHeuristicsRepository`.

    Returns:
        The validated output model.

    Raises:
        HeuristicsError: When the stored row no longer matches the schema.
    """
    try:
        return PhoneticHeuristicsOut.model_validate(row)
    except Exception as exc:  # noqa: BLE001 - a corrupted row must not 500
        raise HeuristicsError(f"stored heuristics row {row.get('id')} is invalid: {exc}") from exc


async def list_all() -> list[PhoneticHeuristicsOut]:
    """Return every configured language pair.

    Returns:
        Rows ordered by (origin, target).

    Raises:
        HeuristicsError: When a stored row no longer matches the schema.
    """
    return [_to_out(row) for row in await PhoneticHeuristicsRepository().list_all()]


async def get(row_id: int) -> PhoneticHeuristicsOut | None:
    """Return one row by id, or None.

    Args:
        row_id: Primary key of the row.

    Returns:
        The row, or None when missing.

    Raises:
        HeuristicsError: When the stored row no longer matches the schema.
    """
    row = await PhoneticHeuristicsRepository().get(row_id)
    return _to_out(row) if row else None


async def get_pair(language_origin: str, language_target: str) -> PhoneticHeuristicsOut | None:
    """Return the row of one language pair, or None when unconfigured.

    Args:
        language_origin: ISO 639-1 language of the terms.
        language_target: ISO 639-1 language reading them.

    Returns:
        The row, or None when the pair has never been configured.

    Raises:
        HeuristicsError: When the stored row no longer matches the schema.
    """
    row = await PhoneticHeuristicsRepository().get_pair(language_origin, language_target)
    return _to_out(row) if row else None


async def save(
    payload: PhoneticHeuristicsIn, *, row_id: int | None = None
) -> PhoneticHeuristicsOut:
    """Create a row, or replace an existing one when ``row_id`` is given.

    Args:
        payload: Validated heuristics payload.
        row_id: Id of the row to replace; None creates a new one.

    Returns:
        The stored row.

    Raises:
        HeuristicsError: When the id is unknown or the pair already exists.
    """
    repo = PhoneticHeuristicsRepository()
    data = payload.model_dump()
    try:
        if row_id is None:
            row = await repo.create(data)
        else:
            row = await repo.update(row_id, data)
    except ValueError as exc:
        raise HeuristicsError(str(exc)) from exc
    if row is None:
        raise HeuristicsError(f"heuristics row {row_id} not found")
    result = _to_out(row)
    logger.info(
        "phonetic_heuristics_saved",
        id=result.id,
        origin=result.language_origin,
        target=result.language_target,
        created=row_id is None,
    )
    return result


async def delete(row_id: int) -> None:
    """Delete one row.

    Args:
        row_id: Primary key of the row.

    Raises:
        HeuristicsError: When the row does not exist.
    """
    if not await PhoneticHeuristicsRepository().delete(row_id):
        raise HeuristicsError(f"heuristics row {row_id} not found")
    logger.info("phonetic_heuristics_deleted", id=row_id)


async def require_enabled_pair(language_origin: str, language_target: str) -> PhoneticHeuristicsOut:
    """Return an enabled row for a pair, or explain what is missing.

    Used by the lesson submit path so a class never queues with a pair the
    respeller cannot run.

    Args:
        language_origin: ISO 639-1 language of the terms.
        language_target: ISO 639-1 language reading them.

    Returns:
        The enabled row.

    Raises:
        HeuristicsError: When the pair is unconfigured or disabled.
    """
    row = await get_pair(language_origin, language_target)
    if row is None:
        raise HeuristicsError(
            f"no phonetic heuristics for {language_origin} -> {language_target}; "
            f"create the pair in Ajustes -> Fonética (/ui/phonetics)"
        )
    if not row.enabled:
        raise HeuristicsError(
            f"phonetic heuristics for {language_origin} -> {language_target} are disabled"
        )
    return row
