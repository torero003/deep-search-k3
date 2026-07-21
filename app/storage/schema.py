SCHEMA = """
-- Search results (cache + dedup)
CREATE TABLE IF NOT EXISTS search_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    content TEXT,
    score REAL,
    fetched_at TEXT DEFAULT (datetime('now')),
    category TEXT
);

-- UNIQUE index created in init_db() after cleaning duplicates
-- CREATE UNIQUE INDEX idx_search_dedup ON search_results(query, source, url);

CREATE INDEX IF NOT EXISTS idx_search_query ON search_results(query);
CREATE INDEX IF NOT EXISTS idx_search_source ON search_results(source);
CREATE INDEX IF NOT EXISTS idx_search_category ON search_results(category);

CREATE VIRTUAL TABLE IF NOT EXISTS search_results_fts USING fts5(
    query, source, url, title, content,
    content='search_results', content_rowid='rowid'
);

-- Trigger to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS search_results_ai AFTER INSERT ON search_results BEGIN
    INSERT INTO search_results_fts(rowid, query, source, url, title, content)
    VALUES (new.id, new.query, new.source, new.url, new.title, new.content);
END;

-- Time-series structured data
CREATE TABLE IF NOT EXISTS timeseries_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    value_text TEXT,
    unit TEXT,
    period TEXT,
    source TEXT NOT NULL,
    source_url TEXT,
    extracted_at TEXT DEFAULT (datetime('now')),
    raw_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_timeseries_cm ON timeseries_data(category, metric);

-- Source health tracking
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    last_success TEXT,
    last_failure TEXT,
    failure_count INTEGER DEFAULT 0,
    avg_response_time_ms INTEGER
);
"""
