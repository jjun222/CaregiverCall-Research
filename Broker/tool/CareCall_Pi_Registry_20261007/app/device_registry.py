"""CareCall device/set registry, phase 1. No MQTT or Telegram activation.

Only three new registry tables are written. Existing event/guardian/notification
tables remain owned by the running, single-button application.
"""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3

VERSION = 1
PHASE = "registry_only"
LEGACY_DEVICE = "button01"
TABLES = ("carecall_registry_meta", "carecall_sets", "carecall_devices")
DDL = (
    """CREATE TABLE carecall_registry_meta (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        version INTEGER NOT NULL CHECK(version=1),
        phase TEXT NOT NULL CHECK(phase='registry_only'),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE carecall_sets (
        set_id TEXT PRIMARY KEY NOT NULL
          CHECK(length(set_id) BETWEEN 1 AND 32
                AND set_id NOT GLOB '*[^A-Za-z0-9_-]*'),
        label TEXT NOT NULL CHECK(length(label) BETWEEN 1 AND 48),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE carecall_devices (
        device_id TEXT PRIMARY KEY NOT NULL
          CHECK(length(device_id) BETWEEN 1 AND 32
                AND device_id NOT GLOB '*[^A-Za-z0-9_-]*'),
        set_id TEXT NOT NULL REFERENCES carecall_sets(set_id),
        mqtt_username TEXT NOT NULL UNIQUE CHECK(mqtt_username=device_id),
        mqtt_client_id TEXT NOT NULL UNIQUE
          CHECK(mqtt_client_id='carecall-' || device_id),
        state TEXT NOT NULL CHECK(state IN ('legacy','staged')),
        created_at TEXT NOT NULL,
        CHECK((device_id='button01' AND state='legacy')
           OR (device_id<>'button01' AND state='staged'))
    ) STRICT""",
)
LEGACY_COLUMNS = {
    "call_events": {"event_id", "device_id", "delivery_count"},
    "notification_recipients": {"device_id", "chat_id", "enabled"},
    "notification_outbox": {"event_id", "device_id", "status"},
    "confirmation_calls": {"event_id", "device_id", "confirmed_at"},
    "confirmation_latest": {"device_id", "event_id"},
    "guardian_invites": {"device_id", "state"},
}


class RegistryError(RuntimeError):
    """Stable error codes; never embed secrets or database row contents."""


def require(condition, code):
    if not condition:
        raise RegistryError(code)


def identifier(value):
    require(isinstance(value, str) and
            re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) is not None,
            "INVALID_IDENTIFIER")
    return value


def label_value(value):
    require(isinstance(value, str) and 1 <= len(value) <= 48
            and value == value.strip() and all(c.isprintable() for c in value),
            "INVALID_SET_LABEL")
    return value


def connect(database, *, readonly=False):
    path = Path(database).expanduser().absolute()
    require(path.is_file() and not path.is_symlink(), "EXISTING_DATABASE_REQUIRED")
    mode = "ro" if readonly else "rw"
    c = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True,
                        timeout=5, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    if readonly:
        c.execute("PRAGMA query_only=ON")
    else:
        c.execute("PRAGMA synchronous=FULL")
    return c


def _sql_normalized(sql):
    return " ".join(sql.strip().rstrip(";").split()).casefold()


def inspect(c):
    """Validate the known single-button baseline and any phase-1 registry."""
    for table, columns in LEGACY_COLUMNS.items():
        actual = {r["name"] for r in c.execute('PRAGMA table_info("' + table + '")')}
        require(columns <= actual, "LEGACY_SCHEMA_MISMATCH:" + table)
        require(c.execute('SELECT 1 FROM "' + table +
                          '" WHERE device_id IS NULL OR device_id<>? LIMIT 1',
                          (LEGACY_DEVICE,)).fetchone() is None,
                "NON_LEGACY_DATA_REQUIRES_MIGRATION:" + table)
    found = {r["name"]: r for r in c.execute(
        "SELECT name,type,sql FROM sqlite_master WHERE name IN (?,?,?)", TABLES)}
    if not found:
        return "not_installed"
    require(set(found) == set(TABLES), "PARTIAL_REGISTRY_SCHEMA")
    for name, sql in zip(TABLES, DDL):
        require(found[name]["type"] == "table" and
                _sql_normalized(found[name]["sql"]) == _sql_normalized(sql),
                "REGISTRY_SCHEMA_VERSION_MISMATCH")
    require(c.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                      "AND tbl_name IN (?,?,?)", TABLES).fetchone() is None,
            "UNEXPECTED_REGISTRY_TRIGGER")
    meta = c.execute("SELECT * FROM carecall_registry_meta").fetchall()
    require(len(meta) == 1 and meta[0]["singleton"] == 1 and
            meta[0]["version"] == VERSION and meta[0]["phase"] == PHASE,
            "REGISTRY_METADATA_MISMATCH")
    legacy = c.execute("SELECT * FROM carecall_devices WHERE device_id=?",
                       (LEGACY_DEVICE,)).fetchone()
    require(legacy is not None and legacy["state"] == "legacy",
            "LEGACY_REGISTRY_ENTRY_MISSING")
    for table in TABLES:
        require(not c.execute('PRAGMA foreign_key_check("' + table + '")').fetchall(),
                "REGISTRY_FOREIGN_KEY_ERROR")
    return "installed"


def initialize(database, set_id="set01", label="CareCall set01"):
    identifier(set_id)
    label_value(label)
    with closing(connect(database)) as c, c:
        c.execute("BEGIN IMMEDIATE")
        if inspect(c) == "installed":
            current = c.execute("SELECT set_id FROM carecall_devices WHERE device_id=?",
                                (LEGACY_DEVICE,)).fetchone()[0]
            require(current == set_id, "LEGACY_SET_ALREADY_ASSIGNED")
            return "already_installed"
        for sql in DDL:
            c.execute(sql)
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        c.execute("INSERT INTO carecall_registry_meta VALUES(1,?,?,?)", (VERSION, PHASE, now))
        c.execute("INSERT INTO carecall_sets VALUES(?,?,?)", (set_id, label, now))
        c.execute("INSERT INTO carecall_devices VALUES(?,?,?,?,?,?)",
                  (LEGACY_DEVICE, set_id, LEGACY_DEVICE, "carecall-button01", "legacy", now))
        require(inspect(c) == "installed", "POST_MIGRATION_CHECK_FAILED")
        return "installed"


def add_set(database, set_id, label):
    identifier(set_id)
    label_value(label)
    with closing(connect(database)) as c, c:
        c.execute("BEGIN IMMEDIATE")
        require(inspect(c) == "installed", "REGISTRY_NOT_INSTALLED")
        old = c.execute("SELECT label FROM carecall_sets WHERE set_id=?", (set_id,)).fetchone()
        if old:
            require(old[0] == label, "SET_ALREADY_EXISTS_WITH_DIFFERENT_LABEL")
            return "already_registered"
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        c.execute("INSERT INTO carecall_sets VALUES(?,?,?)", (set_id, label, now))
        return "registered"


def stage_device(database, device_id, set_id):
    identifier(device_id)
    identifier(set_id)
    with closing(connect(database)) as c, c:
        c.execute("BEGIN IMMEDIATE")
        require(inspect(c) == "installed", "REGISTRY_NOT_INSTALLED")
        require(c.execute("SELECT 1 FROM carecall_sets WHERE set_id=?", (set_id,)).fetchone(),
                "SET_NOT_REGISTERED")
        old = c.execute("SELECT set_id FROM carecall_devices WHERE device_id=?",
                        (device_id,)).fetchone()
        if old:
            require(old[0] == set_id, "DEVICE_ALREADY_ASSIGNED_TO_ANOTHER_SET")
            return "already_registered"
        now = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        c.execute("INSERT INTO carecall_devices VALUES(?,?,?,?,?,?)",
                  (device_id, set_id, device_id, "carecall-" + device_id, "staged", now))
        return "staged"


def status(database):
    with closing(connect(database, readonly=True)) as c, c:
        c.execute("BEGIN")
        state = inspect(c)
        result = {"phase": PHASE, "registry": state,
                  "additional_button_calls_enabled": False, "sets": [], "devices": []}
        if state == "installed":
            result["sets"] = [dict(r) for r in c.execute(
                "SELECT set_id,label FROM carecall_sets ORDER BY set_id")]
            result["devices"] = [dict(r) for r in c.execute(
                "SELECT device_id,set_id,state,mqtt_username,mqtt_client_id "
                "FROM carecall_devices ORDER BY device_id")]
        return result
