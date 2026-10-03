-- Context-Intelligence v2 schema (SQLite + sqlite-vec scalar functions + FTS5).
-- ponytail: embeddings live in BLOB columns and are scanned with vec_distance_cosine (exact, filterable);
-- switch to vec0 ANN tables when a domain passes ~100k rows.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    norm        TEXT NOT NULL UNIQUE,          -- lowercased, whitespace-collapsed name
    kind        TEXT,
    aliases     TEXT NOT NULL DEFAULT '[]',    -- JSON array of alternative names
    embedding   BLOB,
    created_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_entities USING fts5(
    name, aliases, content='entities', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS entities_ai AFTER INSERT ON entities BEGIN
    INSERT INTO fts_entities(rowid, name, aliases) VALUES (new.id, new.name, new.aliases);
END;
CREATE TRIGGER IF NOT EXISTS entities_au AFTER UPDATE OF name, aliases ON entities BEGIN
    INSERT INTO fts_entities(fts_entities, rowid, name, aliases) VALUES ('delete', old.id, old.name, old.aliases);
    INSERT INTO fts_entities(rowid, name, aliases) VALUES (new.id, new.name, new.aliases);
END;

-- Facts and free-text notes share one bi-temporal ledger.
CREATE TABLE IF NOT EXISTS memories (
    id               INTEGER PRIMARY KEY,
    uid              TEXT NOT NULL UNIQUE,
    external_id      TEXT UNIQUE,                 -- caller-supplied idempotency key (imports, LifeHub)
    domain           TEXT NOT NULL,
    type             TEXT NOT NULL DEFAULT 'note',
    subject_id       INTEGER REFERENCES entities(id),
    relation         TEXT,
    object_text      TEXT,
    object_entity_id INTEGER REFERENCES entities(id),
    content          TEXT NOT NULL,               -- searchable statement
    raw_text         TEXT,                        -- original wording, never rewritten
    temporal_form    TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (temporal_form IN ('ongoing', 'completed', 'habitual', 'unknown')),
    tags             TEXT NOT NULL DEFAULT '[]',
    importance       REAL NOT NULL DEFAULT 0.5,
    source           TEXT,
    agent            TEXT,
    session          TEXT,
    extra            TEXT NOT NULL DEFAULT '{}',  -- caller fields we don't model (diary entry_id, mood, ...)
    observed_at      TEXT NOT NULL,
    valid_from       TEXT NOT NULL,
    valid_to         TEXT,
    recorded_at      TEXT NOT NULL,
    superseded_by    INTEGER REFERENCES memories(id),
    status           TEXT NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'superseded', 'pending', 'archived', 'deleted')),
    expires_at       TEXT,
    access_count     INTEGER NOT NULL DEFAULT 0,
    last_accessed    TEXT,
    embedding        BLOB
);

-- The invariant: at most one active value per (domain, subject, relation).
CREATE UNIQUE INDEX IF NOT EXISTS one_active
    ON memories(domain, subject_id, relation)
    WHERE status = 'active' AND relation IS NOT NULL;
CREATE INDEX IF NOT EXISTS memories_status_domain ON memories(status, domain);
CREATE INDEX IF NOT EXISTS memories_subject ON memories(subject_id);
CREATE INDEX IF NOT EXISTS memories_object ON memories(object_entity_id);

CREATE VIRTUAL TABLE IF NOT EXISTS fts_memories USING fts5(
    content, tags, content='memories', content_rowid='id', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO fts_memories(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE OF content, tags ON memories BEGIN
    INSERT INTO fts_memories(fts_memories, rowid, content, tags) VALUES ('delete', old.id, old.content, old.tags);
    INSERT INTO fts_memories(rowid, content, tags) VALUES (new.id, new.content, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO fts_memories(fts_memories, rowid, content, tags) VALUES ('delete', old.id, old.content, old.tags);
END;
-- Vocabulary stats for adaptive (IDF-weighted) fusion.
CREATE VIRTUAL TABLE IF NOT EXISTS fts_memories_vocab USING fts5vocab(fts_memories, row);

-- A-MEM style associative links between memories (undirected; stored once with src < dst).
CREATE TABLE IF NOT EXISTS links (
    src        INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    dst        INTEGER NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    weight     REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (src, dst),
    CHECK (src < dst)
);

CREATE TABLE IF NOT EXISTS skills (
    id           INTEGER PRIMARY KEY,
    uid          TEXT NOT NULL UNIQUE,
    external_id  TEXT UNIQUE,
    name         TEXT NOT NULL UNIQUE,
    description  TEXT NOT NULL,
    domain       TEXT NOT NULL DEFAULT 'general',
    trigger_tags TEXT NOT NULL DEFAULT '[]',
    instructions TEXT NOT NULL,
    examples     TEXT NOT NULL DEFAULT '[]',
    version      INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    embedding    BLOB
);

-- Items the server would not auto-resolve: uncertain supersessions, free-text conflicts, merge/alias proposals.
CREATE TABLE IF NOT EXISTS reviews (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('supersede', 'conflict', 'merge', 'alias')),
    memory_id   INTEGER REFERENCES memories(id),   -- the new / proposed-to-win item
    other_id    INTEGER,                           -- the existing memory (or entity, for alias)
    confidence  REAL,
    detail      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'approved', 'rejected')),
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS reviews_open ON reviews(status, created_at);

-- Audit + usage: one row per write, supersession, tool call.
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    op         TEXT NOT NULL,
    tool       TEXT,
    agent      TEXT,
    memory_id  INTEGER,
    latency_ms INTEGER,
    detail     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_op_ts ON events(op, ts);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
