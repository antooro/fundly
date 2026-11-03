import os
import sqlite3
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from .currency import major
from .ids import split


def default_cache_dir() -> Path:
    """$FUNDLY_CACHE_DIR, else the platform's cache folder."""
    if os.environ.get("FUNDLY_CACHE_DIR"):
        return Path(os.environ["FUNDLY_CACHE_DIR"]).expanduser()
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "fundly" / "Cache"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "fundly"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "fundly"


# Letters that NFKD does not decompose into a base letter + accent (applied after lowercasing).
_FOLD = str.maketrans({"đ": "d", "ł": "l", "ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "ı": "i", "þ": "th", "ð": "d"})


def normalize(text: str | None) -> str:
    """Lowercase, accents stripped, punctuation to spaces: 'Multigestión' and 'multigestion' match."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", text.lower().translate(_FOLD))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower().translate(_FOLD)
    return " ".join("".join(c if c.isalnum() else " " for c in t).split())


INSTRUMENT_COLS = ("id", "id_type", "name", "type", "currency", "domicile", "provider", "category",
                   "ter", "size_eur", "ticker", "morningstar_id", "finect_id")


BUSY_TIMEOUT = 30  # seconds to wait for another process holding the write lock


class Cache:
    """Local SQLite store: instruments seen in searches plus cached quotes and price series."""

    def __init__(self, directory: Path | None):
        if directory is None:
            target = ":memory:"
        else:
            directory.mkdir(parents=True, exist_ok=True)
            target = str(directory / "fundly.db")
        # Autocommit mode: transactions are opened explicitly in tx(). Other processes may share the file,
        # so a busy database is waited for instead of failing.
        self._conn = sqlite3.connect(target, check_same_thread=False, isolation_level=None, timeout=BUSY_TIMEOUT)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        if directory is not None:
            self._enable_wal()
        with self.tx() as c:
            if c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                # It's only a cache: on a schema change, start over instead of migrating.
                for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
                    c.execute(f'DROP TABLE IF EXISTS "{table}"')
                c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    c.execute(statement)

    def _enable_wal(self):
        """Switching to WAL needs an exclusive lock and SQLite doesn't wait for it, so when several processes
        open a new cache at once, the losers get "database is locked" at once. Retry up to BUSY_TIMEOUT."""
        deadline, delay = time.monotonic() + BUSY_TIMEOUT, 0.005
        while True:
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                return
            except sqlite3.OperationalError as e:
                if "locked" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.25)

    @contextmanager
    def tx(self):
        # BEGIN IMMEDIATE takes the write lock up front. A deferred transaction that reads first and writes
        # later fails at once ("database is locked") if another process wrote in between, instead of waiting.
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def upsert_instruments(self, items: list[dict]):
        """Insert new instruments; for known ones only fill fields that are still empty."""
        cols = (*INSTRUMENT_COLS, "search_text")
        rows, aliases = [], []
        for it in items:
            it = {k: " ".join(v.split()) if isinstance(v, str) else v for k, v in it.items()}
            it["id_type"] = split(it["id"])[0]
            it["currency"] = major(it.get("currency"))
            names = " ".join(filter(None, [it.get("name"), it.get("alt_name"), it["id"]]))
            rows.append((*tuple(it.get(c) for c in INSTRUMENT_COLS), normalize(names)))
            # Other codes for the same instrument, so a later lookup by any of them lands on this id.
            for alias in [*it.get("aliases", []),
                          *([f"ticker:{it['ticker'].upper()}"] if it.get("ticker") else []),
                          *([f"ms:{it['morningstar_id'].upper()}"] if it.get("morningstar_id") else [])]:
                if alias != it["id"]:
                    aliases.append((alias, it["id"]))
        with self.tx() as c:
            c.executemany(
                f"INSERT INTO instruments ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                "ON CONFLICT(id) DO UPDATE SET "
                + ", ".join(f"{k} = COALESCE(instruments.{k}, excluded.{k})" for k in INSTRUMENT_COLS[1:])
                + ", search_text = CASE WHEN instr(instruments.search_text, excluded.search_text) > 0 "
                  "THEN instruments.search_text ELSE instruments.search_text || ' ' || excluded.search_text END"
                + ", updated_at = datetime('now')",
                rows,
            )
            c.executemany("INSERT OR IGNORE INTO aliases (alias, id) VALUES (?, ?)", aliases)

    def close(self):
        with self._lock:
            self._conn.close()


SCHEMA_VERSION = 5
SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    id              TEXT PRIMARY KEY,         -- canonical id: ISIN, DGS code, or scheme:code (see fundly.ids)
    id_type         TEXT NOT NULL,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL,            -- etf | fund | plan
    currency        TEXT,
    domicile        TEXT,
    provider        TEXT,
    category        TEXT,
    ter             REAL,
    size_eur        INTEGER,
    ticker          TEXT,
    morningstar_id  TEXT,
    finect_id       TEXT,
    search_text     TEXT NOT NULL DEFAULT '',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_instruments_ticker ON instruments(ticker);
CREATE INDEX IF NOT EXISTS idx_instruments_ms ON instruments(morningstar_id);

CREATE TABLE IF NOT EXISTS aliases (
    alias           TEXT PRIMARY KEY,         -- another canonical id for the same instrument (ticker:VTSAX, in-amfi:118989)
    id              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_log (
    q               TEXT PRIMARY KEY,
    ids             TEXT NOT NULL,            -- JSON list, relevance order from the live sources
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quote_cache (
    id              TEXT NOT NULL,
    source          TEXT NOT NULL,
    price           REAL NOT NULL,
    price_date      TEXT NOT NULL,
    currency        TEXT NOT NULL,
    price_type      TEXT NOT NULL,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id, source)
);

CREATE TABLE IF NOT EXISTS price_cache (
    id              TEXT NOT NULL,
    source          TEXT NOT NULL,
    date            TEXT NOT NULL,
    price           REAL NOT NULL,
    PRIMARY KEY (id, source, date)
);

CREATE TABLE IF NOT EXISTS history_fetches (
    id              TEXT NOT NULL,
    source          TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    currency        TEXT NOT NULL,
    price_type      TEXT NOT NULL,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_fetches ON history_fetches(id, source, fetched_at DESC);

CREATE TABLE IF NOT EXISTS misses (
    id              TEXT NOT NULL,
    source          TEXT NOT NULL,
    kind            TEXT NOT NULL,            -- "quote", or "history:<start>:<end>"
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (id, source, kind)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    currency        TEXT NOT NULL,
    source          TEXT NOT NULL,
    date            TEXT NOT NULL,
    per_eur         REAL NOT NULL,            -- units of `currency` per euro
    PRIMARY KEY (currency, source, date)
);

CREATE TABLE IF NOT EXISTS fx_fetches (
    currency        TEXT NOT NULL,
    source          TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    fetched_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""
