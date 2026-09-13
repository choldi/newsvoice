"""Pydantic models for articles, filtering, and news digests."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.api.models.cluster import ClusterItem


class ArticleIn(BaseModel):
    """Input article schema received from n8n RSS node."""

    title: str = Field(..., description="Title of the news article")
    link: str = Field(..., description="URL link to the full article")
    content: str | None = Field(default="", description="Body content or summary of article")
    pub_date: str | None = Field(default=None, description="Publication timestamp/date")
    source: str | None = Field(default=None, description="Source RSS feed name or domain")


class ArticleDB(BaseModel):
    """Database representation of an article record."""

    url: str = Field(..., description="Normalized URL primary key")
    title: str = Field(..., description="Title of the article")
    content: str | None = Field(default=None, description="Body content of the article")
    source: str | None = Field(default=None, description="Source name or domain")
    fetched_at: datetime = Field(..., description="Timestamp when article was fetched")


class FilterNewIn(BaseModel):
    """Input payload to filter new/unseen articles."""

    articles: list[ArticleIn] = Field(..., description="List of raw RSS articles")


class FilterNewOut(BaseModel):
    """Response payload containing only unseen/new articles."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    new_articles: list[ArticleIn] = Field(..., description="Filtered list of unseen articles")
    total_received: int = Field(..., description="Total number of articles in request")
    new_count: int = Field(..., description="Number of new articles found")


class DigestIn(BaseModel):
    """Input payload containing articles or clusters to summarize."""

    articles: list[ArticleIn] = Field(
        default_factory=list, description="List of RSS articles to summarize"
    )
    clusters: list[ClusterItem] | None = Field(
        default=None, description="Optional pre-grouped event clusters to summarize"
    )
    podcast_behavior: str | None = Field(
        default=None,
        description="Optional podcaster behavior instruction injected into the LLM prompt",
    )
    language: str | None = Field(
        default=None,
        description="ISO 639-1 language code for the digest text (e.g. 'es'). "
        "Defaults to settings.",
    )
    prompt_template: str | None = Field(
        default=None,
        description="Name of the per-collection prompt template in src/core/prompts.py. "
        "Defaults to 'default'.",
    )
    collection: str = Field(
        default="default", description="Collection name, used for scoped delivery tracking"
    )


class DigestOut(BaseModel):
    """Output response schema returned to n8n containing the generated digest."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    digest: str = Field(..., description="Generated Markdown digest (5 main stories + 5 headlines)")
    article_count: int = Field(..., description="Total number of articles processed")
