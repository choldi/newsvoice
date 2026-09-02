-- 001_initial_schema.sql: Initial database schema for articles and deliveries

CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    content TEXT,
    source TEXT,
    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_url TEXT,
    cluster_id INTEGER,
    delivered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    material_update BOOLEAN DEFAULT 0,
    FOREIGN KEY (article_url) REFERENCES articles(url)
);

CREATE INDEX IF NOT EXISTS idx_articles_fetched_at ON articles(fetched_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_article_url ON deliveries(article_url);
CREATE INDEX IF NOT EXISTS idx_deliveries_delivered_at ON deliveries(delivered_at);
