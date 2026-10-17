"""LRU cache for audio embeddings to avoid re-computing from reference files."""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np
from loguru import logger


class EmbeddingCache:
    """Thread-safe LRU cache for audio embeddings.

    Keys are SHA-256 hashes of file contents, so the same audio referenced
    from different paths still hits the cache.
    """

    def __init__(self, maxsize: int = 128) -> None:
        self._maxsize = maxsize
        self._cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _file_hash(audio_path: Path | str) -> str:
        """Compute SHA-256 hash of file contents."""
        h = hashlib.sha256()
        with Path(audio_path).open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    def get(self, audio_path: Path | str) -> np.ndarray | None:
        """Return cached embedding or None on miss."""
        key = self._file_hash(audio_path)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._hits += 1
                logger.debug("EmbeddingCache hit: {}", audio_path)
                return self._cache[key]
            self._misses += 1
            logger.debug("EmbeddingCache miss: {}", audio_path)
            return None

    def put(self, audio_path: Path | str, embedding: np.ndarray) -> None:
        """Store an embedding, evicting the oldest entry if at capacity."""
        key = self._file_hash(audio_path)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                self._cache[key] = embedding
                return
            if len(self._cache) >= self._maxsize:
                _evicted_key, _ = self._cache.popitem(last=False)
                logger.debug("EmbeddingCache evicted entry (size={})", len(self._cache))
            self._cache[key] = embedding

    def invalidate(self, audio_path: Path | str) -> bool:
        """Remove cached entry for the given audio file. Returns True if removed."""
        key = self._file_hash(audio_path)
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                logger.debug("EmbeddingCache invalidated: {}", audio_path)
                return True
            return False

    def clear(self) -> None:
        """Remove all cached entries."""
        with self._lock:
            self._cache.clear()
            logger.debug("EmbeddingCache cleared")

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._cache)

    @property
    def stats(self) -> dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "maxsize": self._maxsize,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / total if total > 0 else 0.0,
            }


# Global singleton
_embedding_cache: EmbeddingCache | None = None


def get_embedding_cache(maxsize: int = 128) -> EmbeddingCache:
    """Get or create the global embedding cache."""
    global _embedding_cache
    if _embedding_cache is None:
        _embedding_cache = EmbeddingCache(maxsize=maxsize)
    return _embedding_cache
