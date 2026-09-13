"""Pydantic models for event clustering."""

from pydantic import BaseModel, Field

from src.api.models.article import ArticleIn, DigestIn


class Fingerprint(BaseModel):
    """Event fingerprint representation identifying an event story."""

    entity: str = Field(..., description="Primary entity involved in the event")
    action: str = Field(..., description="Core action or predicate of the event")
    title_hash: str = Field(..., description="Deterministic hash of the normalized headline")


class ClusterItem(BaseModel):
    """A cluster group containing related articles for a single story event."""

    cluster_id: int | None = Field(default=None, description="Database cluster ID")
    fingerprint: Fingerprint = Field(..., description="Event fingerprint identifying this cluster")
    articles: list[ArticleIn] = Field(
        ..., description="List of articles collapsed into this cluster"
    )
    is_material_update: bool = Field(
        default=True,
        description="Whether this cluster is new or contains material update facts",
    )


class ClusterIn(BaseModel):
    """Input payload to cluster a batch of articles."""

    articles: list[ArticleIn] = Field(..., description="Raw or filtered articles to cluster")


class ClusterOut(BaseModel):
    """Response payload containing clustered article groups."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    clusters: list[ClusterItem] = Field(..., description="List of clustered story groups")
    total_articles: int = Field(..., description="Total input articles processed")
    new_clusters: int = Field(..., description="Number of newly created clusters")
    updated_clusters: int = Field(
        ..., description="Number of existing clusters with material updates"
    )


# Rebuild DigestIn with ClusterItem type available
DigestIn.model_rebuild()
