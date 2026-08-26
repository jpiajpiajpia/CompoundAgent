-- Compound Signal Harness — ledger schema
-- One SQLite file holds all state. Every table is append-friendly and replayable.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- episodes
CREATE TABLE IF NOT EXISTS episodes (
    id              TEXT PRIMARY KEY,      -- slug: show-YYYY-MM-DD[-n]
    show            TEXT NOT NULL,         -- tcaf | wayt | compound_other | animal_spirits
    feed            TEXT NOT NULL,
    guid            TEXT NOT NULL UNIQUE,  -- dedupe key from the feed
    published_at    TEXT NOT NULL,         -- ISO8601 UTC
    weekday         TEXT NOT NULL,
    title           TEXT NOT NULL,
    subtitle        TEXT,
    audio_url       TEXT,
    duration_sec    INTEGER,
    link            TEXT,
    transcript_path TEXT,
    transcript_kind TEXT,                  -- captions | whisper | none
    state           TEXT NOT NULL DEFAULT 'discovered',
                    -- discovered -> fetched -> transcribed -> extracted
    discovered_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_pub  ON episodes(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_state ON episodes(state);

-- ---------------------------------------------------------------- mentions
-- The contract between the model layer and the trading layer.
CREATE TABLE IF NOT EXISTS mentions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id      TEXT NOT NULL REFERENCES episodes(id) ON DELETE CASCADE,
    speaker         TEXT,
    speaker_role    TEXT,                  -- host | guest | clip
    ticker          TEXT,
    resolved_name   TEXT,
    resolve_conf    REAL,
    t_start         INTEGER,
    t_end           INTEGER,
    quote           TEXT,
    stance          TEXT,                  -- strong_bull|bull|neutral|bear|strong_bear
    action_language TEXT,                  -- buying|owns|watching|sold|shorting|none
    horizon         TEXT,                  -- trade|swing|long_term|unspecified
    is_thesis       INTEGER NOT NULL DEFAULT 0,
    is_news_recap   INTEGER NOT NULL DEFAULT 0,
    is_hypothetical INTEGER NOT NULL DEFAULT 0,
    extracted_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mentions_ticker ON mentions(ticker);
CREATE INDEX IF NOT EXISTS idx_mentions_ep     ON mentions(episode_id);

-- ---------------------------------------------------------------- candidates
CREATE TABLE IF NOT EXISTS candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week            TEXT NOT NULL,         -- ISO week: 2026-W34
    ticker          TEXT NOT NULL,
    signal_s        REAL,
    quality_q       REAL,
    idf             REAL,
    rank_score      REAL,
    gates_passed    TEXT,
    gates_failed    TEXT,
    rank            INTEGER,
    created_at      TEXT NOT NULL,
    UNIQUE(week, ticker)
);

-- ---------------------------------------------------------------- execution
CREATE TABLE IF NOT EXISTS intents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    week            TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    target_dollars  REAL,
    stop_price      REAL,
    status          TEXT NOT NULL DEFAULT 'proposed',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    intent_id       INTEGER REFERENCES intents(id),
    ref_id          TEXT UNIQUE,           -- idempotency key (UUID)
    rh_order_id     TEXT,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    quantity        REAL,
    dollar_amount   REAL,
    price           REAL,
    state           TEXT,
    placed_at       TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    quantity        REAL NOT NULL,
    entry_price     REAL NOT NULL,
    entry_date      TEXT NOT NULL,
    stop_price      REAL NOT NULL,
    high_water_mark REAL NOT NULL,
    time_stop_date  TEXT,
    no_exit_before  TEXT,                  -- same-session round-trip guard
    protection      TEXT NOT NULL,         -- broker | poller
    stop_order_id   TEXT,                  -- RH order id when protection='broker'
    open            INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS exits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER REFERENCES positions(id),
    exit_date       TEXT NOT NULL,
    exit_price      REAL NOT NULL,
    reason          TEXT NOT NULL,         -- stop|gap_through|time_stop|thesis_change|manual
    pnl             REAL,
    pnl_pct         REAL
);

-- ---------------------------------------------------------------- operations
CREATE TABLE IF NOT EXISTS runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger         TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT,
    payload_json    TEXT
);

CREATE TABLE IF NOT EXISTS polls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                 TEXT NOT NULL,
    positions_checked  INTEGER NOT NULL,
    breaches           INTEGER NOT NULL DEFAULT 0,
    ok                 INTEGER NOT NULL DEFAULT 1,
    pinged_deadman     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    tier            TEXT NOT NULL,         -- wake_me | tell_me | log_only
    event           TEXT NOT NULL,
    message         TEXT NOT NULL,
    delivered       INTEGER NOT NULL DEFAULT 0,
    acknowledged_at TEXT
);

-- ---------------------------------------------------------------- youtube
-- Catalog of per-show playlist videos. Populated once, refreshed incrementally.
-- Kept separate from episodes so a bad match can be redone without re-fetching.
CREATE TABLE IF NOT EXISTS youtube_videos (
    video_id     TEXT PRIMARY KEY,
    show         TEXT NOT NULL,
    playlist_id  TEXT NOT NULL,
    title        TEXT NOT NULL,
    upload_date  TEXT,               -- YYYY-MM-DD
    duration_sec INTEGER,
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ytv_show_date ON youtube_videos(show, upload_date);
