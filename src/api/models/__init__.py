"""Models package initialization."""

from src.api.models.article import (
    ArticleDB,
    ArticleIn,
    DigestIn,
    DigestOut,
    FilterNewIn,
    FilterNewOut,
)
from src.api.models.cluster import (
    ClusterIn,
    ClusterItem,
    ClusterOut,
    Fingerprint,
)
from src.api.models.tts import TTSIn, TTSOut

DigestIn.model_rebuild()

__all__ = [
    "ArticleDB",
    "ArticleIn",
    "ClusterIn",
    "ClusterItem",
    "ClusterOut",
    "DigestIn",
    "DigestOut",
    "Fingerprint",
    "FilterNewIn",
    "FilterNewOut",
    "TTSIn",
    "TTSOut",
]
