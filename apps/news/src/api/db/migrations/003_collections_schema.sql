-- 003_collections_schema.sql: Collection-scoped dedup and delivery history.
--
-- Multiple collections (e.g. "tech", "health", "finance") each produce their own
-- digest. The same article URL may legitimately appear in several collections, so
-- dedup state must be keyed by (collection, url). This migration rewrites the
-- existing tables to be collection-scoped, backfilling rows with 'default'.

PRAGMA foreign_keys=OFF;

-- Articles: composite PK (collection, url) for collection-scoped dedup.
CREATE TABLE articles_new (
    collection TEXT NOT NULL DEFAULT 'default',
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT,
    source TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (collection, url)
);
INSERT INTO articles_new (collection, url, title, content, source, fetched_at)
    SELECT 'default', url, title, content, source, fetched_at FROM articles;
DROP TABLE articles;
ALTER TABLE articles_new RENAME TO articles;

-- Clusters: collection-scoped so events are grouped independently per digest.
CREATE TABLE clusters_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL DEFAULT 'default',
    fingerprint TEXT NOT NULL,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO clusters_new (id, collection, fingerprint, first_seen, last_seen)
    SELECT id, 'default', fingerprint, first_seen, last_seen FROM clusters;
DROP TABLE clusters;
ALTER TABLE clusters_new RENAME TO clusters;

-- Cluster <-> article links: collection-scoped to keep the composite PK valid.
-- clusters.id is globally unique, so the cluster FK references id alone.
CREATE TABLE cluster_articles_new (
    collection TEXT NOT NULL DEFAULT 'default',
    cluster_id INTEGER NOT NULL,
    article_url TEXT NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (collection, cluster_id, article_url),
    FOREIGN KEY (cluster_id) REFERENCES clusters(id) ON DELETE CASCADE,
    FOREIGN KEY (collection, article_url) REFERENCES articles(collection, url) ON DELETE CASCADE
);
INSERT INTO cluster_articles_new (collection, cluster_id, article_url, added_at)
    SELECT 'default', cluster_id, article_url, added_at FROM cluster_articles;
DROP TABLE cluster_articles;
ALTER TABLE cluster_articles_new RENAME TO cluster_articles;

-- Deliveries: collection-scoped so the history of each digest is independent.
CREATE TABLE deliveries_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    collection TEXT NOT NULL DEFAULT 'default',
    article_url TEXT,
    cluster_id INTEGER,
    delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    material_update BOOLEAN DEFAULT 0
);
INSERT INTO deliveries_new (id, collection, article_url, cluster_id, delivered_at, material_update)
    SELECT id, 'default', article_url, cluster_id, delivered_at, material_update FROM deliveries;
DROP TABLE deliveries;
ALTER TABLE deliveries_new RENAME TO deliveries;

CREATE INDEX IF NOT EXISTS idx_articles_fetched_at ON articles(fetched_at);
CREATE INDEX IF NOT EXISTS idx_articles_collection ON articles(collection);
CREATE INDEX IF NOT EXISTS idx_clusters_collection ON clusters(collection);
CREATE INDEX IF NOT EXISTS idx_clusters_first_seen ON clusters(first_seen);
CREATE INDEX IF NOT EXISTS idx_clusters_last_seen ON clusters(last_seen);
CREATE INDEX IF NOT EXISTS idx_deliveries_collection ON deliveries(collection);
CREATE INDEX IF NOT EXISTS idx_deliveries_article_url ON deliveries(article_url);
CREATE INDEX IF NOT EXISTS idx_deliveries_delivered_at ON deliveries(delivered_at);
CREATE INDEX IF NOT EXISTS idx_cluster_articles_url ON cluster_articles(article_url);

PRAGMA foreign_keys=ON;