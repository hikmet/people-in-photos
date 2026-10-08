-- The whole database. app/db.py runs this at every start of the app and the worker; on an empty
-- file it creates everything, on an existing one it changes nothing.
--
-- The tables mirror app/models.py, which tests/test_schema.py checks. Defaults live in the models,
-- not here. Timestamps are stored as UTC text (see `UTCDateTime` in the models).

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS photo_sets (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    name VARCHAR(150) NOT NULL,
    location VARCHAR(150) NOT NULL,
    description TEXT NOT NULL,
    archived BOOLEAN NOT NULL,
    request_consent BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS card_batches (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    label VARCHAR(150) NOT NULL,
    quantity INTEGER NOT NULL,
    base_url TEXT NOT NULL,
    card_text TEXT NOT NULL,
    downloaded_at DATETIME
);

CREATE TABLE IF NOT EXISTS profile (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    public_name VARCHAR(100) NOT NULL,
    links JSON NOT NULL,
    bio TEXT NOT NULL,
    card_text TEXT NOT NULL,
    logo_updated_at DATETIME
);

CREATE TABLE IF NOT EXISTS releases (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    version INTEGER NOT NULL,
    text TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    UNIQUE (version)
);

CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    type VARCHAR(40) NOT NULL,
    payload JSON NOT NULL,
    status VARCHAR(20) NOT NULL,
    attempts INTEGER NOT NULL,
    run_after DATETIME NOT NULL,
    locked_at DATETIME,
    error TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs (status);

CREATE TABLE IF NOT EXISTS cards (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    code VARCHAR(16) NOT NULL,
    photo_set_id VARCHAR(36) REFERENCES photo_sets (id),
    batch_id VARCHAR(36) REFERENCES card_batches (id),
    sequence INTEGER,
    kind VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL,
    notes TEXT NOT NULL,
    handed_out_at DATETIME,
    first_opened_at DATETIME,
    delivered_at DATETIME,
    revision INTEGER NOT NULL,
    UNIQUE (code)
);

CREATE TABLE IF NOT EXISTS photos (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    photo_set_id VARCHAR(36) NOT NULL REFERENCES photo_sets (id),
    upload_id VARCHAR(36) NOT NULL,
    grouping_mode VARCHAR(20) NOT NULL,
    marker_card_id VARCHAR(36) REFERENCES cards (id),
    filename VARCHAR(200) NOT NULL,
    original_sha256 VARCHAR(64) NOT NULL,
    display_sha256 VARCHAR(64) NOT NULL,
    size INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    captured_at VARCHAR(40) NOT NULL,
    exif JSON NOT NULL,
    status VARCHAR(20) NOT NULL,
    error TEXT NOT NULL,
    UNIQUE (original_sha256)
);
CREATE INDEX IF NOT EXISTS ix_photos_upload_id ON photos (upload_id);

CREATE TABLE IF NOT EXISTS card_photos (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    card_id VARCHAR(36) NOT NULL REFERENCES cards (id),
    photo_id VARCHAR(36) NOT NULL REFERENCES photos (id),
    delivered BOOLEAN NOT NULL,
    UNIQUE (photo_id)
);
CREATE INDEX IF NOT EXISTS ix_card_photos_card_id ON card_photos (card_id);

CREATE TABLE IF NOT EXISTS consents (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    card_id VARCHAR(36) NOT NULL REFERENCES cards (id),
    release_id VARCHAR(36) NOT NULL REFERENCES releases (id),
    supersedes_id VARCHAR(36) REFERENCES consents (id),
    kind VARCHAR(20) NOT NULL,
    locale VARCHAR(2) NOT NULL,
    signer_name VARCHAR(150) NOT NULL,
    signer_email VARCHAR(254) NOT NULL,
    attribution VARCHAR(20) NOT NULL,
    credit_name VARCHAR(150) NOT NULL,
    note TEXT NOT NULL,
    ip VARCHAR(64) NOT NULL,
    user_agent VARCHAR(300) NOT NULL,
    snapshot JSON NOT NULL,
    UNIQUE (supersedes_id)
);
CREATE INDEX IF NOT EXISTS ix_consents_card_id ON consents (card_id);

CREATE TABLE IF NOT EXISTS events (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    created_at DATETIME NOT NULL,
    card_id VARCHAR(36) REFERENCES cards (id),
    type VARCHAR(60) NOT NULL,
    actor VARCHAR(20) NOT NULL,
    payload JSON NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_card_id ON events (card_id);

-- Signed decisions, the release texts they were made under, and the activity log are evidence:
-- append-only, refused by trigger whoever asks.
CREATE TRIGGER IF NOT EXISTS immutable_consents_update BEFORE UPDATE ON consents
    BEGIN SELECT RAISE(ABORT, 'Evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_consents_delete BEFORE DELETE ON consents
    BEGIN SELECT RAISE(ABORT, 'Evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_releases_update BEFORE UPDATE ON releases
    BEGIN SELECT RAISE(ABORT, 'Evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_releases_delete BEFORE DELETE ON releases
    BEGIN SELECT RAISE(ABORT, 'Evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'Evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'Evidence is append-only'); END;

COMMIT;
