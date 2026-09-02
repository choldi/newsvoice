"""Event clustering and fingerprinting service."""

import hashlib
import json
import re
import string

import structlog

from src.api.db.database import ArticleRepository
from src.api.models.article import ArticleIn
from src.api.models.cluster import ClusterItem, EventFingerprint
from src.core.llm import generate_completion
from src.core.prompts import (
    EXTRACT_FINGERPRINT_SYSTEM_PROMPT,
    EXTRACT_FINGERPRINT_USER_PROMPT_TEMPLATE,
    MATERIAL_UPDATE_SYSTEM_PROMPT,
    MATERIAL_UPDATE_USER_PROMPT_TEMPLATE,
)

logger = structlog.get_logger()

# Common English stopwords for title tokenization
STOPWORDS: set[str] = {
    "a",
    "about",
    "after",
    "all",
    "also",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "doing",
    "during",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "having",
    "he",
    "her",
    "here",
    "him",
    "his",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "more",
    "most",
    "new",
    "no",
    "not",
    "now",
    "of",
    "off",
    "on",
    "once",
    "only",
    "or",
    "other",
    "our",
    "out",
    "over",
    "says",
    "should",
    "so",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "to",
    "too",
    "under",
    "until",
    "up",
    "very",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "whom",
    "why",
    "will",
    "with",
    "would",
}


def compute_normalized_title_hash(title: str) -> str:
    """Computes a deterministic hash of normalized headline tokens.

    Args:
        title: The article title string.

    Returns:
        A 12-character hex hash representing normalized title keywords.
    """
    if not title:
        return "0" * 12

    # Remove punctuation and lowercase
    cleaned = title.lower().translate(str.maketrans("", "", string.punctuation))
    tokens = [w for w in cleaned.split() if w and w not in STOPWORDS]
    tokens.sort()

    if not tokens:
        tokens = [title.strip().lower()]

    normalized_str = " ".join(tokens)
    return hashlib.sha256(normalized_str.encode("utf-8")).hexdigest()[:12]


def extract_heuristic_fingerprint(title: str, content: str | None = None) -> tuple[str, str]:
    """Heuristic fallback to extract entity and action from title.

    Args:
        title: Article title.
        content: Optional article content.

    Returns:
        Tuple of (entity, action).
    """
    cleaned = re.sub(r"[^\w\s]", " ", title).strip()
    words = cleaned.split()

    if not words:
        return ("General", "Reported news")

    # Capitalized sequence as entity heuristic
    capitalized = [w for w in title.split() if w and w[0].isupper() and w.lower() not in STOPWORDS]
    entity = " ".join(capitalized[:2]) if capitalized else words[0]

    # Action from remaining words
    action_words = [w for w in words if w.lower() not in STOPWORDS and w != entity]
    action = " ".join(action_words[:3]) if action_words else "updates"

    return (entity[:50], action[:50])


class ClusterService:
    """Service to group articles into event clusters and detect material updates."""

    def __init__(self, repository: ArticleRepository | None = None) -> None:
        """Initializes the ClusterService with an ArticleRepository instance.

        Args:
            repository: Optional ArticleRepository instance. Defaults to new instance.
        """
        self.repo = repository or ArticleRepository()

    async def extract_fingerprint(self, article: ArticleIn) -> EventFingerprint:
        """Extracts event fingerprint (entity, action, title hash) for an article.

        Args:
            article: The ArticleIn instance.

        Returns:
            EventFingerprint model.
        """
        title_hash = compute_normalized_title_hash(article.title)

        user_prompt = EXTRACT_FINGERPRINT_USER_PROMPT_TEMPLATE.format(
            title=article.title, content=article.content or ""
        )
        messages = [
            {"role": "system", "content": EXTRACT_FINGERPRINT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            # Temperature 0.7 for clustering judgment as per conventions
            raw_response = await generate_completion(messages=messages, temperature=0.7)
            # Clean possible markdown wrapping
            cleaned = (
                raw_response.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            data = json.loads(cleaned)
            entity = str(data.get("entity", "")).strip() or "General"
            action = str(data.get("action", "")).strip() or "Reported event"
        except Exception as exc:
            logger.warning("llm_fingerprint_extraction_failed_using_heuristics", error=str(exc))
            entity, action = extract_heuristic_fingerprint(article.title, article.content)

        return EventFingerprint(
            entity=entity,
            action=action,
            normalized_title_hash=title_hash,
        )

    def _are_fingerprints_similar(self, fp1: EventFingerprint, fp2: EventFingerprint) -> bool:
        """Evaluates whether two fingerprints belong to the same event cluster.

        Args:
            fp1: First EventFingerprint.
            fp2: Second EventFingerprint.

        Returns:
            True if fingerprints match on hash or entity/action similarity.
        """
        if fp1.normalized_title_hash == fp2.normalized_title_hash:
            return True

        e1, e2 = fp1.entity.lower().strip(), fp2.entity.lower().strip()
        a1, a2 = fp1.action.lower().strip(), fp2.action.lower().strip()

        if e1 == e2 and (a1 in a2 or a2 in a1):
            return True

        # Check entity containment and shared action words
        if (e1 in e2 or e2 in e1) and len(e1) > 2 and len(e2) > 2:
            a1_words = set(a1.split())
            a2_words = set(a2.split())
            if a1_words & a2_words:
                return True

        return False

    async def check_material_update(
        self,
        existing_articles: list[dict[str, str | None]],
        new_article: ArticleIn,
    ) -> bool:
        """Evaluates if a new incoming article contains material new facts for a cluster.

        Args:
            existing_articles: Previously saved articles in the cluster.
            new_article: The new ArticleIn being evaluated.

        Returns:
            True if the new article represents a material update.
        """
        if not existing_articles:
            return True

        existing_text = "\n".join(
            f"- {a.get('title')}: {a.get('content', '')[:150]}" for a in existing_articles
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

        try:
            raw_response = await generate_completion(messages=messages, temperature=0.7)
            cleaned = (
                raw_response.strip()
                .removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
            data = json.loads(cleaned)
            return bool(data.get("is_material_update", True))
        except Exception as exc:
            logger.warning("material_update_detection_fallback", error=str(exc))
            return True

    async def cluster_articles(
        self,
        articles: list[ArticleIn],
        active_days: int = 3,
    ) -> list[ClusterItem]:
        """Groups a list of articles into event clusters and associates them in database.

        Args:
            articles: List of ArticleIn instances to cluster.
            active_days: Lookback window in days for matching active clusters.

        Returns:
            List of ClusterItem objects.
        """
        if not articles:
            return []

        # 1. Extract fingerprints for all incoming articles
        fingerprinted_articles: list[tuple[EventFingerprint, ArticleIn]] = []
        for article in articles:
            fp = await self.extract_fingerprint(article)
            fingerprinted_articles.append((fp, article))

        # 2. Intra-batch grouping
        batch_groups: list[dict] = []
        for fp, article in fingerprinted_articles:
            matched_group = None
            for group in batch_groups:
                if self._are_fingerprints_similar(fp, group["fingerprint"]):
                    matched_group = group
                    break

            if matched_group:
                matched_group["articles"].append(article)
            else:
                batch_groups.append(
                    {
                        "fingerprint": fp,
                        "articles": [article],
                    }
                )

        # 3. Match against recent database clusters & evaluate material updates
        recent_db_clusters = await self.repo.find_recent_clusters(active_days=active_days)
        result_clusters: list[ClusterItem] = []

        for group in batch_groups:
            group_fp: EventFingerprint = group["fingerprint"]
            group_articles: list[ArticleIn] = group["articles"]

            # Check if matching cluster exists in DB
            matched_db_cluster = None
            for db_c in recent_db_clusters:
                db_fp = EventFingerprint(
                    entity=db_c["fingerprint"].get("entity", ""),
                    action=db_c["fingerprint"].get("action", ""),
                    normalized_title_hash=db_c["fingerprint"].get("normalized_title_hash", ""),
                )
                if self._are_fingerprints_similar(group_fp, db_fp):
                    matched_db_cluster = db_c
                    break

            if matched_db_cluster:
                cluster_id = matched_db_cluster["id"]
                is_delivered = await self.repo.is_cluster_delivered(cluster_id)

                if is_delivered:
                    # Check material update against previous articles
                    existing_articles = await self.repo.get_cluster_articles(cluster_id)
                    is_material = await self.check_material_update(
                        existing_articles, group_articles[0]
                    )
                else:
                    is_material = True

                await self.repo.update_cluster_last_seen(cluster_id)
                for art in group_articles:
                    await self.repo.add_article_to_cluster(cluster_id, art.link)

                result_clusters.append(
                    ClusterItem(
                        cluster_id=cluster_id,
                        fingerprint=group_fp,
                        articles=group_articles,
                        is_material_update=is_material,
                    )
                )
            else:
                # Create brand new cluster
                cluster_id = await self.repo.create_cluster(group_fp.model_dump())
                for art in group_articles:
                    await self.repo.add_article_to_cluster(cluster_id, art.link)

                result_clusters.append(
                    ClusterItem(
                        cluster_id=cluster_id,
                        fingerprint=group_fp,
                        articles=group_articles,
                        is_material_update=True,
                    )
                )

        logger.info(
            "clustering_completed",
            input_articles=len(articles),
            clusters_count=len(result_clusters),
        )
        return result_clusters
