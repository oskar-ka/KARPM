-- KARPM schema. Version is tracked via PRAGMA user_version (see db.py).

CREATE TABLE IF NOT EXISTS searches (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    url          TEXT NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_status  TEXT
);

CREATE TABLE IF NOT EXISTS listings (
    id                TEXT PRIMARY KEY,          -- Kleinanzeigen ad id
    url               TEXT NOT NULL,
    search_name       TEXT,                      -- search that first found it
    title             TEXT,
    description       TEXT,

    price_eur         INTEGER,
    price_kind        TEXT,                      -- fixed | vb (negotiable) | free | unknown

    make              TEXT,
    model             TEXT,
    bike_type         TEXT,                      -- Art: Naked, Sportler, Enduro, ...
    model_year        INTEGER,                   -- "Baujahr" if stated, else derived
    first_reg_date    TEXT,                      -- Erstzulassung, ISO YYYY-MM-01
    first_reg_year    INTEGER,
    km                INTEGER,                   -- Kilometerstand
    hp                INTEGER,                   -- Leistung in PS
    ccm               INTEGER,                   -- Hubraum
    owners            INTEGER,                   -- Anzahl Fahrzeughalter
    inspection_until  TEXT,                      -- HU/TUEV, ISO YYYY-MM-01
    condition         TEXT,                      -- Fahrzeugzustand
    damaged           INTEGER,                   -- 1 = Beschaedigtes Fahrzeug
    full_service_hist INTEGER,                   -- Scheckheftgepflegt

    seller_type       TEXT,                      -- private | commercial | unknown
    seller_name       TEXT,
    seller_id         TEXT,
    location          TEXT,
    postcode          TEXT,

    posted_at         TEXT,                      -- when the ad went up (ISO)
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    is_active         INTEGER NOT NULL DEFAULT 1,
    delisted_at       TEXT,
    view_count        INTEGER,

    attributes_json   TEXT,                      -- every raw label/value pair we found
    parse_warnings    TEXT,                      -- fields the parser could not fill
    content_hash      TEXT                       -- hash of title+description+price
);

CREATE INDEX IF NOT EXISTS idx_listings_active   ON listings(is_active);
CREATE INDEX IF NOT EXISTS idx_listings_price    ON listings(price_eur);
CREATE INDEX IF NOT EXISTS idx_listings_seen     ON listings(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_listings_model    ON listings(make, model);

-- One row per observed change. Lets you reconstruct price history and
-- time-on-market, which is the whole point of tracking over time.
CREATE TABLE IF NOT EXISTS listing_history (
    id          INTEGER PRIMARY KEY,
    listing_id  TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    event       TEXT NOT NULL,                   -- created | price_change | edited | delisted | relisted
    price_eur   INTEGER,
    prev_price_eur INTEGER,
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_listing ON listing_history(listing_id, observed_at);

CREATE TABLE IF NOT EXISTS images (
    id            INTEGER PRIMARY KEY,
    listing_id    TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    position      INTEGER NOT NULL,
    url           TEXT NOT NULL,
    local_path    TEXT,
    sha256        TEXT,
    bytes         INTEGER,
    downloaded_at TEXT,
    UNIQUE (listing_id, url)
);

CREATE INDEX IF NOT EXISTS idx_images_listing ON images(listing_id, position);

CREATE TABLE IF NOT EXISTS scores (
    id             INTEGER PRIMARY KEY,
    listing_id     TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    scored_at      TEXT NOT NULL,
    model          TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    content_hash   TEXT,                         -- listing state this score refers to
    overall        INTEGER NOT NULL,             -- 1-5
    fit            INTEGER,                      -- 1-5 vs. your preferences
    value          INTEGER,                      -- 1-5 value for money
    fair_price_eur INTEGER,                      -- model's estimate of a fair price
    headline       TEXT,
    reasoning      TEXT,
    pros_json      TEXT,
    cons_json      TEXT,
    red_flags_json TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_scores_listing ON scores(listing_id, scored_at);
CREATE INDEX IF NOT EXISTS idx_scores_overall ON scores(overall);

-- Guarantees we never mail the same listing twice for the same reason.
CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY,
    listing_id TEXT REFERENCES listings(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                    -- instant | digest
    sent_at    TEXT NOT NULL,
    provider_id TEXT,
    UNIQUE (listing_id, kind)
);

CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    kind          TEXT NOT NULL,                 -- scrape | score | digest
    ok            INTEGER,
    listings_seen INTEGER DEFAULT 0,
    listings_new  INTEGER DEFAULT 0,
    listings_changed INTEGER DEFAULT 0,
    scored        INTEGER DEFAULT 0,
    error         TEXT
);

-- Current score per listing, for convenient querying. Recreated on every init
-- so the column list always matches this file.
DROP VIEW IF EXISTS listing_current;
CREATE VIEW listing_current AS
SELECT l.*,
       s.overall, s.fit, s.value, s.fair_price_eur, s.headline, s.reasoning,
       s.pros_json, s.cons_json, s.red_flags_json, s.model AS score_model,
       s.scored_at
FROM listings l
LEFT JOIN scores s ON s.id = (
    SELECT id FROM scores WHERE listing_id = l.id ORDER BY scored_at DESC LIMIT 1
);
