-- 002_clusters_schema.sql: Event clusters and cluster-article associations

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cluster_articles (
    cluster_id INTEGER NOT NULL,
    article_url TEXT NOT NULL,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (cluster_id, article_url),
    FOREIGN KEY (cluster_id) REFERENCES clusters(id) ON DELETE CASCADE,
    FOREIGN KEY (article_url) REFERENCES articles(url) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_clusters_first_seen ON clusters(first_seen);
CREATE INDEX IF NOT EXISTS idx_clusters_last_seen ON clusters(last_seen);
CREATE INDEX IF NOT EXISTS idx_cluster_articles_url ON cluster_articles(article_url);
