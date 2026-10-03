"""Pydantic models for the Phase 7.10 single-URL summary endpoint."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class UrlSummarizeIn(BaseModel):
    """Request payload to summarize one article by URL."""

    url: str = Field(..., max_length=2048, description="Article URL to summarize (http/https)")
    collection: str = Field(
        default="default",
        description="Collection whose prompt and delivery destinations are used",
    )
    store: bool = Field(
        default=False,
        description="Persist the extracted article in `articles` (marks it as seen)",
    )
    deliver: bool = Field(
        default=True,
        description="Deliver the digest to the collection's configured channels",
    )

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, value: str) -> str:
        """Accept only well-formed http(s) URLs.

        Args:
            value: Raw URL from the request body.

        Returns:
            The trimmed URL.

        Raises:
            ValueError: When the URL is blank, too long or not http(s).
        """
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("url must not be empty")
        if len(cleaned) > 2048:
            raise ValueError("url longer than 2048 characters")
        parsed = urlparse(cleaned)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("url scheme must be http or https")
        if not parsed.netloc:
            raise ValueError("url must include a host")
        return cleaned

    @field_validator("collection")
    @classmethod
    def _collection_name(cls, value: str) -> str:
        """Trim the collection name; 'default' is used when blank.

        Args:
            value: Collection name from the request body.

        Returns:
            The trimmed name.
        """
        return value.strip() or "default"


class UrlSummarizeOut(BaseModel):
    """Response payload: the run that will summarize the URL."""

    status: str = Field(default="ok", description="Execution status: ok or error")
    workflow_id: str = Field(..., description="Temporal workflow id of the URL summary run")
    collection: str = Field(..., description="Collection the run belongs to")
    url: str = Field(..., description="URL being summarized")
