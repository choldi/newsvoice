"""Admin collections CRUD routes for the Phase 6 GUI.

These endpoints operate on `collections.toml` via the CollectionsRepository,
keeping the TOML file the single source of truth.
"""

import structlog
from fastapi import APIRouter, HTTPException, status

from src.api.models.admin import (
    CollectionIn,
    CollectionOut,
    CollectionsListOut,
)
from src.api.services.collections_repo import CollectionConflictError, CollectionsRepository

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["admin-collections"])


def _repo() -> CollectionsRepository:
    """Return a CollectionsRepository bound to the configured file.

    Returns:
        A new repository instance for collections.toml.
    """
    return CollectionsRepository()


@router.get("/admin/collections", response_model=CollectionsListOut)
async def list_collections() -> CollectionsListOut:
    """List all collections (enabled and disabled).

    Returns:
        CollectionsListOut with all collection configurations.
    """
    collections = _repo().list()
    return CollectionsListOut(collections=collections)


@router.get("/admin/collections/{name}", response_model=CollectionOut)
async def get_collection_config(name: str) -> CollectionOut:
    """Return a single collection by name.

    Args:
        name: Collection name.

    Returns:
        CollectionOut with the requested collection.

    Raises:
        HTTPException: 404 if the collection does not exist.
    """
    collection = _repo().get(name)
    if collection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection '{name}' not found",
        )
    return CollectionOut(collection=collection)


@router.post(
    "/admin/collections", response_model=CollectionOut, status_code=status.HTTP_201_CREATED
)
async def create_collection(payload: CollectionIn) -> CollectionOut:
    """Create a new collection.

    Args:
        payload: CollectionIn with the collection fields.

    Returns:
        CollectionOut with the persisted collection.

    Raises:
        HTTPException: 409 if the name already exists, 422 on invalid fields.
    """
    try:
        config = payload.to_config()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    try:
        created = _repo().create(config)
    except CollectionConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    return CollectionOut(collection=created)


@router.put("/admin/collections/{name}", response_model=CollectionOut)
async def update_collection(name: str, payload: CollectionIn) -> CollectionOut:
    """Update an existing collection (the TOML name follows the payload name).

    Args:
        name: Current collection name.
        payload: CollectionIn with the new field values.

    Returns:
        CollectionOut with the persisted collection.

    Raises:
        HTTPException: 404 if the collection does not exist.
    """
    repo = _repo()
    existing = repo.get(name)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection '{name}' not found",
        )
    if payload.name.casefold() != name.casefold() and repo.exists(payload.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A collection named '{payload.name}' already exists",
        )
    try:
        config = payload.to_config()
        collections = [c for c in repo.list() if c.name.casefold() != name.casefold()]
        collections.append(config)
        repo.save_all(collections)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return CollectionOut(collection=config)


@router.delete("/admin/collections/{name}", status_code=status.HTTP_200_OK)
async def delete_collection(name: str) -> dict[str, str]:
    """Delete a collection by name.

    Args:
        name: Collection name.

    Returns:
        Dict with status and message.

    Raises:
        HTTPException: 404 if the collection does not exist.
    """
    repo = _repo()
    existing = repo.get(name)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Collection '{name}' not found",
        )
    collections = [c for c in repo.list() if c.name.casefold() != name.casefold()]
    repo.save_all(collections)
    logger.info("collection_deleted", name=name)
    return {"status": "ok", "message": f"Collection '{name}' deleted"}
