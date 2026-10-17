"""Tests unitarios para el caché de embeddings LRU."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from tts_api.core.embedding_cache import EmbeddingCache


@pytest.fixture
def cache() -> EmbeddingCache:
    """Caché de embeddings con capacidad pequeña para tests de evicción."""
    return EmbeddingCache(maxsize=3)


@pytest.fixture
def sample_audio(tmp_path: Path) -> Path:
    """Archivo de audio de prueba."""
    audio_file = tmp_path / "test.wav"
    audio_file.write_bytes(b"fake wav data " * 100)
    return audio_file


@pytest.fixture
def sample_embedding() -> np.ndarray:
    """Embedding de prueba."""
    return np.random.randn(192).astype(np.float32)


def test_cache_miss_on_empty(cache: EmbeddingCache, sample_audio: Path) -> None:
    """Test de miss en caché vacío."""
    result = cache.get(sample_audio)
    assert result is None
    assert cache.size == 0


def test_cache_hit(cache: EmbeddingCache, sample_audio: Path, sample_embedding: np.ndarray) -> None:
    """Test de hit en caché."""
    cache.put(sample_audio, sample_embedding)
    result = cache.get(sample_audio)
    assert result is not None
    np.testing.assert_array_equal(result, sample_embedding)
    assert cache.size == 1


def test_cache_miss_different_file(
    cache: EmbeddingCache, sample_audio: Path, sample_embedding: np.ndarray, tmp_path: Path
) -> None:
    """Test de miss con archivo diferente."""
    cache.put(sample_audio, sample_embedding)
    other_audio = tmp_path / "other.wav"
    other_audio.write_bytes(b"different data " * 100)
    result = cache.get(other_audio)
    assert result is None


def test_cache_eviction(cache: EmbeddingCache, tmp_path: Path) -> None:
    """Test de evicción LRU cuando se alcanza la capacidad."""
    embeddings = [np.array([i], dtype=np.float32) for i in range(4)]

    for i in range(4):
        audio = tmp_path / f"audio_{i}.wav"
        audio.write_bytes(f"data_{i}".encode() * 50)
        cache.put(audio, embeddings[i])

    # La primera entrada (audio_0) debería haber sido evictada
    assert cache.size == 3

    audio_0 = tmp_path / "audio_0.wav"
    assert cache.get(audio_0) is None

    # Las otras deberían seguir
    for i in range(1, 4):
        audio = tmp_path / f"audio_{i}.wav"
        assert cache.get(audio) is not None


def test_cache_invalidation(
    cache: EmbeddingCache, sample_audio: Path, sample_embedding: np.ndarray
) -> None:
    """Test de invalidación de entrada."""
    cache.put(sample_audio, sample_embedding)
    assert cache.size == 1

    removed = cache.invalidate(sample_audio)
    assert removed is True
    assert cache.size == 0
    assert cache.get(sample_audio) is None


def test_cache_invalidation_nonexistent(cache: EmbeddingCache, tmp_path: Path) -> None:
    """Test de invalidación de entrada inexistente."""
    audio = tmp_path / "nonexistent.wav"
    audio.write_bytes(b"data" * 50)
    removed = cache.invalidate(audio)
    assert removed is False


def test_cache_clear(
    cache: EmbeddingCache, sample_audio: Path, sample_embedding: np.ndarray
) -> None:
    """Test de limpieza completa del caché."""
    cache.put(sample_audio, sample_embedding)
    assert cache.size == 1

    cache.clear()
    assert cache.size == 0
    assert cache.get(sample_audio) is None


def test_cache_update_same_file(cache: EmbeddingCache, sample_audio: Path) -> None:
    """Test de actualización de embedding para el mismo archivo."""
    emb1 = np.array([1.0], dtype=np.float32)
    emb2 = np.array([2.0], dtype=np.float32)

    cache.put(sample_audio, emb1)
    cache.put(sample_audio, emb2)

    assert cache.size == 1
    result = cache.get(sample_audio)
    np.testing.assert_array_equal(result, emb2)


def test_cache_lru_order(cache: EmbeddingCache, tmp_path: Path) -> None:
    """Test de que el acceso a una entrada la mueve al final (LRU)."""
    for i in range(3):
        audio = tmp_path / f"audio_{i}.wav"
        audio.write_bytes(f"data_{i}".encode() * 50)
        cache.put(audio, np.array([i], dtype=np.float32))

    # Acceder a audio_0 para moverlo al final
    audio_0 = tmp_path / "audio_0.wav"
    cache.get(audio_0)

    # Agregar una cuarta entrada — debería evictar audio_1 (el menos reciente)
    audio_3 = tmp_path / "audio_3.wav"
    audio_3.write_bytes(b"data_3" * 50)
    cache.put(audio_3, np.array([3], dtype=np.float32))

    assert cache.get(audio_0) is not None  # Todavía en caché
    audio_1 = tmp_path / "audio_1.wav"
    assert cache.get(audio_1) is None  # Evictado


def test_cache_stats(
    cache: EmbeddingCache, sample_audio: Path, sample_embedding: np.ndarray
) -> None:
    """Test de estadísticas del caché."""
    stats = cache.stats
    assert stats["size"] == 0
    assert stats["hits"] == 0
    assert stats["misses"] == 0
    assert stats["hit_rate"] == 0.0

    cache.get(sample_audio)  # miss
    cache.put(sample_audio, sample_embedding)
    cache.get(sample_audio)  # hit
    cache.get(sample_audio)  # hit

    stats = cache.stats
    assert stats["size"] == 1
    assert stats["hits"] == 2
    assert stats["misses"] == 1
    assert stats["hit_rate"] == pytest.approx(2 / 3)


def test_cache_same_content_different_path(tmp_path: Path) -> None:
    """Test de que archivos con el mismo contenido comparten caché."""
    cache = EmbeddingCache(maxsize=10)
    content = b"identical content " * 100

    path_a = tmp_path / "a.wav"
    path_b = tmp_path / "b.wav"
    path_a.write_bytes(content)
    path_b.write_bytes(content)

    embedding = np.array([42.0], dtype=np.float32)
    cache.put(path_a, embedding)

    # path_b tiene el mismo contenido → debería hit
    result = cache.get(path_b)
    assert result is not None
    np.testing.assert_array_equal(result, embedding)
