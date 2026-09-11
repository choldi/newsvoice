"""Event clustering service using deterministic fingerprinting."""

import hashlib
import json
import re
from typing import Any

import structlog

from src.api.db.database import ArticleRepository
from src.api.models.article import ArticleIn
from src.api.models.cluster import ClusterIn, ClusterItem, Fingerprint
from src.core.llm import generate_completion
from src.core.prompts import (
    EXTRACT_FINGERPRINT_SYSTEM_PROMPT,
    EXTRACT_FINGERPRINT_USER_PROMPT_TEMPLATE,
    MATERIAL_UPDATE_SYSTEM_PROMPT,
    MATERIAL_UPDATE_USER_PROMPT_TEMPLATE,
)

logger = structlog.get_logger()

# Stopwords for title normalization (English + Spanish common words)
STOPWORDS: set[str] = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "but",
    "in",
    "on",
    "at",
    "to",
    "for",
    "of",
    "with",
    "by",
    "from",
    "as",
    "is",
    "was",
    "are",
    "were",
    "be",
    "been",
    "being",
    "have",
    "has",
    "had",
    "do",
    "does",
    "did",
    "will",
    "would",
    "could",
    "should",
    "may",
    "might",
    "must",
    "can",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "they",
    "them",
    "their",
    "we",
    "us",
    "our",
    "you",
    "your",
    "he",
    "him",
    "his",
    "she",
    "her",
    "hers",
    "i",
    "me",
    "my",
    "mine",
    "new",
    "el",
    "la",
    "los",
    "las",
    "un",
    "una",
    "unos",
    "unas",
    "y",
    "o",
    "pero",
    "en",
    "de",
    "del",
    "al",
    "por",
    "para",
    "con",
    "sin",
    "sobre",
    "entre",
    "hasta",
    "desde",
    "durante",
    "mediante",
    "según",
    "como",
    "es",
    "son",
    "era",
    "eran",
    "está",
    "están",
    "estaba",
    "estaban",
    "ha",
    "han",
    "he",
    "has",
    "había",
    "habían",
    "ser",
    "estar",
    "tener",
    "hacer",
    "ir",
    "venir",
}


class ClusterService:
    """Service to cluster articles by event fingerprint and detect material updates."""

    def __init__(self, repository: ArticleRepository | None = None) -> None:
        """Initialize with ArticleRepository."""
        self.repo = repository or ArticleRepository()

    def _normalize_title(self, title: str) -> str:
        """Normalize title for hashing: lowercase, remove stopwords, punctuation, numbers."""
        if not title:
            return ""
        words: list[str] = []
        for raw_token in title.lower().split():
            token = re.sub(r"[^\w]", "", raw_token)
            if not token or any(c.isdigit() for c in token):
                continue
            if token not in STOPWORDS and len(token) > 2:
                words.append(token)
        return " ".join(words)

    def _compute_title_hash(self, title: str) -> str:
        """Compute short hash of normalized title."""
        normalized = self._normalize_title(title)
        return hashlib.sha256(normalized.encode()).hexdigest()[:12]

    async def _extract_fingerprint_llm(self, title: str, content: str | None) -> Fingerprint:
        """Extract entity and action using LLM."""
        user_prompt = EXTRACT_FINGERPRINT_USER_PROMPT_TEMPLATE.format(
            title=title, content=content or ""
        )
        messages = [
            {"role": "system", "content": EXTRACT_FINGERPRINT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            response = await generate_completion(messages=messages, temperature=0.7)
            data = json.loads(response.strip())
            entity = data.get("entity", "Unknown").strip()
            action = data.get("action", "Unknown").strip()
            title_hash = self._compute_title_hash(title)
            return Fingerprint(entity=entity, action=action, title_hash=title_hash)
        except Exception as exc:
            logger.warning("fingerprint_llm_failed", error=str(exc), title=title[:50])
            # Fallback: simple heuristic
            title_hash = self._compute_title_hash(title)
            return Fingerprint(entity="Unknown", action="Unknown", title_hash=title_hash)

    def _fingerprint_to_key(self, fp: Fingerprint) -> str:
        """Create a deterministic key for fingerprint matching.

        The key is based on entity + action only so that different titles
        covering the same event collapse into one cluster. title_hash is
        kept as diagnostic metadata but does not influence grouping.
        """
        return f"{fp.entity.lower()}|{fp.action.lower()}"

    async def _check_material_update(self, cluster_id: int, new_article: ArticleIn) -> bool:
        """Check if new article brings material updates to existing cluster."""
        try:
            existing_articles = await self.repo.get_cluster_articles(cluster_id)
            if not existing_articles:
                return True  # First article in cluster is always material

            existing_text = "\n\n".join(
                f"Title: {a['title']}\nContent: {a['content'] or ''}" for a in existing_articles
            )
            user_prompt = MATERIAL_UPDATE_USER_PROMPT_TEMPLATE.format(
                existing_text=existing_text,
                new_title=new_article.title,
                new_content=new_article.content or "",
            )
            messages = [
                {"role": "system", "content": MATERIAL_UPDATE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            response = await generate_completion(messages=messages, temperature=0.3)
            data = json.loads(response.strip())
            return bool(data.get("is_material_update", False))
        except Exception as exc:
            logger.warning("material_update_check_failed", error=str(exc))
            return False  # Conservative: assume no material update on error

    async def cluster_articles(self, payload: ClusterIn) -> list[ClusterItem]:
        """Cluster articles by event fingerprint and detect material updates."""
        if not payload.articles:
            return []

        # Ensure every article exists in the articles table so the
        # cluster_articles FK reference does not fail.
        await self.repo.save_articles(
            [
                {
                    "url": a.link,
                    "title": a.title,
                    "content": a.content or "",
                    "source": a.source,
                }
                for a in payload.articles
            ]
        )

        # 1. Get recent active clusters from DB (last 3 days)
        recent_clusters = await self.repo.find_recent_clusters(active_days=3)
        cluster_map: dict[str, dict[str, Any]] = {}
        for cl in recent_clusters:
            fp = cl["fingerprint"]
            key = self._fingerprint_to_key(Fingerprint(**fp))
            cluster_map[key] = {
                "id": cl["id"],
                "fingerprint": fp,
                "articles": [],
                "is_material_update": False,
            }

        # 2. Process each article: extract fingerprint, match or create cluster
        for article in payload.articles:
            fp = await self._extract_fingerprint_llm(article.title, article.content)
            key = self._fingerprint_to_key(fp)

            if key in cluster_map:
                # Existing cluster: add article, check material update
                cluster_map[key]["articles"].append(article)
                if not cluster_map[key]["is_material_update"]:
                    is_update = await self._check_material_update(cluster_map[key]["id"], article)
                    cluster_map[key]["is_material_update"] = is_update
            else:
                # New cluster: create in DB
                cluster_id = await self.repo.create_cluster(fp.model_dump())
                await self.repo.add_article_to_cluster(cluster_id, article.link)
                cluster_map[key] = {
                    "id": cluster_id,
                    "fingerprint": fp.model_dump(),
                    "articles": [article],
                    "is_material_update": True,  # New cluster = material
                }

        # 3. Update last_seen for all touched clusters
        for cluster_data in cluster_map.values():
            await self.repo.update_cluster_last_seen(cluster_data["id"])

        # 4. Build output ClusterItems
        result: list[ClusterItem] = []
        for cluster_data in cluster_map.values():
            fp = Fingerprint(**cluster_data["fingerprint"])
            result.append(
                ClusterItem(
                    cluster_id=cluster_data["id"],
                    fingerprint=fp,
                    articles=cluster_data["articles"],
                    is_material_update=cluster_data["is_material_update"],
                )
            )

        logger.info(
            "clustering_completed",
            total_articles=len(payload.articles),
            clusters=len(result),
            new_clusters=sum(1 for c in result if c.is_material_update and len(c.articles) == 1),
            updated_clusters=sum(1 for c in result if c.is_material_update and len(c.articles) > 1),
        )
        return result
