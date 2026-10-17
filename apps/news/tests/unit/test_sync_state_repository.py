"""Unit tests for SyncStateRepository (collections-config monitor KV store)."""

from src.api.db.database import SyncStateRepository


async def test_get_missing_key_returns_none() -> None:
    """An absent key yields None."""
    value = await SyncStateRepository().get("collections_signature")
    assert value is None


async def test_set_and_get_roundtrip() -> None:
    """A stored value round-trips through the repo."""
    repo = SyncStateRepository()
    await repo.set("collections_signature", "abc123")

    assert await repo.get("collections_signature") == "abc123"


async def test_set_is_idempotent_upsert() -> None:
    """Re-setting the same key replaces the previous value."""
    repo = SyncStateRepository()
    await repo.set("collections_signature", "old")
    await repo.set("collections_signature", "new")

    assert await repo.get("collections_signature") == "new"


async def test_set_does_not_clobber_other_keys() -> None:
    """Different keys are stored independently."""
    repo = SyncStateRepository()
    await repo.set("collections_signature", "a")
    await repo.set("other_key", "b")

    assert await repo.get("collections_signature") == "a"
    assert await repo.get("other_key") == "b"
