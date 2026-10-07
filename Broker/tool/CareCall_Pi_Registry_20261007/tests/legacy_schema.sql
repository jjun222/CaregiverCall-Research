-- From event_database.py

CREATE TABLE IF NOT EXISTS call_events (
    event_id TEXT PRIMARY KEY NOT NULL,
    schema_version INTEGER NOT NULL
        CHECK (schema_version = 1),
    device_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    sequence INTEGER NOT NULL
        CHECK (sequence >= 1),
    uptime_ms INTEGER NOT NULL
        CHECK (uptime_ms >= 0),
    topic TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    last_received_at TEXT NOT NULL,
    delivery_count INTEGER NOT NULL DEFAULT 1
        CHECK (delivery_count >= 1)
) STRICT;

CREATE INDEX IF NOT EXISTS
    idx_call_events_device_received_at
ON call_events (device_id, received_at);

-- From notification_store.py

CREATE TABLE IF NOT EXISTS notification_recipients (
    device_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL CHECK (chat_id > 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    PRIMARY KEY (device_id, chat_id)
) STRICT;
CREATE TABLE IF NOT EXISTS notification_outbox (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES call_events(event_id),
    device_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL CHECK (chat_id > 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    created_at TEXT NOT NULL,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_attempt_at TEXT,
    sent_at TEXT,
    telegram_message_id INTEGER,
    last_error TEXT,
    UNIQUE (event_id, chat_id),
    FOREIGN KEY (device_id, chat_id)
        REFERENCES notification_recipients(device_id, chat_id)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_notification_due
ON notification_outbox(status, next_attempt_at, notification_id);

-- From confirmation_store.py

CREATE TABLE IF NOT EXISTS confirmation_calls (
 event_id TEXT PRIMARY KEY REFERENCES call_events(event_id) ON DELETE CASCADE,
 device_id TEXT NOT NULL, boot_order TEXT NOT NULL, sequence INTEGER NOT NULL,
 confirmed_at TEXT, device_result TEXT CHECK(device_result IN ('applied','stale')),
 next_attempt_at REAL NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX IF NOT EXISTS confirmation_order
 ON confirmation_calls(device_id,boot_order,sequence);
CREATE TABLE IF NOT EXISTS confirmation_latest (
 device_id TEXT PRIMARY KEY,
 event_id TEXT NOT NULL REFERENCES confirmation_calls(event_id) ON DELETE CASCADE
) STRICT;
CREATE TABLE IF NOT EXISTS confirmation_messages (
 notification_id INTEGER PRIMARY KEY
   REFERENCES notification_outbox(notification_id) ON DELETE CASCADE,
 token TEXT NOT NULL UNIQUE,
 applied TEXT NOT NULL DEFAULT 'empty' CHECK(applied IN ('empty','button','unknown','gone')),
 next_attempt_at REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0
) STRICT;

-- From guardian_store.py

CREATE TABLE IF NOT EXISTS guardian_invites (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 token_hash TEXT UNIQUE NOT NULL,
 device_id TEXT NOT NULL CHECK(device_id='button01'),
 label TEXT NOT NULL,
 created_at REAL NOT NULL,
 expires_at REAL NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('issued','pending','approved','revoked')),
 chat_id INTEGER,
 check_code TEXT,
 approved_at REAL
) STRICT;
CREATE TABLE IF NOT EXISTS guardian_cursor (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 last_update_id INTEGER NOT NULL,
 last_update_at REAL NOT NULL DEFAULT 0
) STRICT;
INSERT OR IGNORE INTO guardian_cursor(singleton,last_update_id) VALUES(1,-1);
CREATE TABLE IF NOT EXISTS guardian_replies (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 dedupe_key TEXT UNIQUE NOT NULL,
 invite_id INTEGER REFERENCES guardian_invites(id),
 chat_id INTEGER NOT NULL,
 text TEXT NOT NULL,
 created_at REAL NOT NULL,
 expires_at REAL NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','sending','sent','failed','cancelled')),
 attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0,
 message_id INTEGER,
 error_code TEXT
) STRICT;
CREATE INDEX IF NOT EXISTS guardian_replies_due ON guardian_replies(status,next_at,id);
CREATE INDEX IF NOT EXISTS guardian_replies_chat ON guardian_replies(chat_id,created_at);
CREATE TABLE IF NOT EXISTS guardian_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 action TEXT NOT NULL,
 invite_id INTEGER,
 recipient_id INTEGER,
 created_at TEXT NOT NULL
) STRICT;

-- OperatorStore.migrate column additions before its indexes/triggers.
ALTER TABLE notification_recipients ADD COLUMN management_key TEXT;
ALTER TABLE guardian_invites ADD COLUMN approval_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE guardian_replies ADD COLUMN reply_markup TEXT;
ALTER TABLE guardian_replies ADD COLUMN admin_reply INTEGER NOT NULL DEFAULT 0;

-- From operator_store.py

CREATE TABLE IF NOT EXISTS operator_config (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 chat_id INTEGER NOT NULL CHECK(chat_id>0),
 generation TEXT NOT NULL, created_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS operator_bootstrap (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 token_hash TEXT NOT NULL, expires_at REAL NOT NULL,
 chat_id INTEGER, code_hash TEXT, salt TEXT,
 attempts INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE TABLE IF NOT EXISTS operator_actions (
 nonce TEXT PRIMARY KEY NOT NULL, kind TEXT NOT NULL,
 target TEXT NOT NULL, expires_at REAL NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS operator_state (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 kind TEXT NOT NULL, target TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS recipient_management_key
 ON notification_recipients(management_key);
CREATE TRIGGER IF NOT EXISTS recipient_management_key_insert
 AFTER INSERT ON notification_recipients WHEN NEW.management_key IS NULL
 BEGIN
 UPDATE notification_recipients SET management_key=lower(hex(randomblob(16)))
 WHERE device_id=NEW.device_id AND chat_id=NEW.chat_id;
 END;

-- From operator_handover.py

CREATE TABLE IF NOT EXISTS operator_handover (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 request_id TEXT NOT NULL, owner_generation TEXT NOT NULL,
 token_hash TEXT NOT NULL, label TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL,
 candidate_chat_id INTEGER CHECK(candidate_chat_id>0),
 salt TEXT, code_hash TEXT,
 attempts INTEGER NOT NULL DEFAULT 0,
 verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1))
) STRICT;
