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
    EXTRACT_FINGERPRINTS_BATCH_SYSTEM_PROMPT,
    EXTRACT_FINGERPRINTS_BATCH_USER_PROMPT_TEMPLATE,
    MATERIAL_UPDATE_SYSTEM_PROMPT,
    MATERIAL_UPDATE_USER_PROMPT_TEMPLATE,
)
from src.core.tracing import trace_function

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

    async def _extract_fingerprints_batch(
        self, articles: list[ArticleIn]
    ) -> dict[int, Fingerprint]:
        """Extract entity/action fingerprints for all articles in one LLM call.

        Args:
            articles: Articles to fingerprint.

        Returns:
            Mapping of article index (0-based) to Fingerprint. Entries that
            fail to extract (LLM error or malformed JSON) fall back to an
            "Unknown" fingerprint keyed by the article's title hash.
        """
        numbered: list[str] = []
        for i, article in enumerate(articles, start=1):
            snippet = (article.content or "").strip().replace("\n", " ")[:300]
            content_clause = f" | Content: {snippet}" if snippet else ""
            numbered.append(f"{i}. Title: {article.title}{content_clause}")
        user_prompt = EXTRACT_FINGERPRINTS_BATCH_USER_PROMPT_TEMPLATE.format(
            items="\n".join(numbered)
        )
        messages = [
            {"role": "system", "content": EXTRACT_FINGERPRINTS_BATCH_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        try:
            response = await generate_completion(messages=messages, temperature=0.7)
            data = json.loads(response.strip())
            pairs = self._parse_fingerprint_pair_batch(data, len(articles))
        except Exception as exc:
            logger.warning("fingerprints_batch_llm_failed", error=str(exc), total=len(articles))
            pairs = {i: ("Unknown", "Unknown") for i in range(len(articles))}

        return {
            i: Fingerprint(
                entity=entity,
                action=action,
                title_hash=self._compute_title_hash(articles[i].title),
            )
            for i, (entity, action) in pairs.items()
        }

    @staticmethod
    def _parse_fingerprint_pair_batch(data: Any, expected: int) -> dict[int, tuple[str, str]]:
        """Normalize a batch fingerprint LLM response.

        Args:
            data: Parsed JSON from the LLM. Either a list aligned to the input
                order or an object keyed by 1-based item number.
            expected: Number of articles; indices 0..expected-1 must exist.

        Returns:
            Map of article index to (entity, action); missing or malformed
            entries default to ("Unknown", "Unknown").
        """
        raw: dict[int, Any] = {}
        if isinstance(data, list):
            for idx, item in enumerate(data):
                if isinstance(item, dict):
                    raw[idx] = item
        elif isinstance(data, dict):
            for key, item in data.items():
                try:
                    parsed_key = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(item, dict):
                    raw[parsed_key - 1 if parsed_key >= 1 else parsed_key] = item

        pairs: dict[int, tuple[str, str]] = {}
        for idx in range(expected):
            item = raw.get(idx)
            if isinstance(item, dict):
                entity = str(item.get("entity") or "Unknown").strip() or "Unknown"
                action = str(item.get("action") or "Unknown").strip() or "Unknown"
            else:
                entity, action = "Unknown", "Unknown"
            pairs[idx] = (entity, action)
        return pairs

    def _fingerprint_to_key(self, fp: Fingerprint) -> str:
        """Create a deterministic key for fingerprint matching.

        The key is based on entity + action only so that different titles
        covering the same event collapse into one cluster. title_hash is
        kept as diagnostic metadata but does not influence grouping.
        """
        return f"{fp.entity.lower()}|{fp.action.lower()}"

    async def _check_material_update(
        self, cluster_id: int, new_article: ArticleIn, collection: str = "default"
    ) -> bool:
        """Check if new article brings material updates to existing cluster."""
        try:
            existing_articles = await self.repo.get_cluster_articles(
                cluster_id, collection=collection
            )
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

    @trace_function(component="api.services.cluster")
    async def cluster_articles(self, payload: ClusterIn) -> list[ClusterItem]:
        """Cluster articles by event fingerprint and detect material updates."""
        if not payload.articles:
            return []

        collection = payload.collection

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
            ],
            collection=collection,
        )

        # 1. Get recent active clusters from DB (last 3 days)
        recent_clusters = await self.repo.find_recent_clusters(active_days=3, collection=collection)
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

        # 2. Extract fingerprints for every article in a single batched LLM
        #    call, then match or create clusters.
        fingerprints = await self._extract_fingerprints_batch(payload.articles)
        for idx, article in enumerate(payload.articles):
            fp = fingerprints[idx]
            key = self._fingerprint_to_key(fp)

            if key in cluster_map:
                # Existing cluster: add article, check material update
                cluster_map[key]["articles"].append(article)
                if not cluster_map[key]["is_material_update"]:
                    is_update = await self._check_material_update(
                        cluster_map[key]["id"], article, collection
                    )
                    cluster_map[key]["is_material_update"] = is_update
            else:
                # New cluster: create in DB
                cluster_id = await self.repo.create_cluster(fp.model_dump(), collection=collection)
                await self.repo.add_article_to_cluster(
                    cluster_id, article.link, collection=collection
                )
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
