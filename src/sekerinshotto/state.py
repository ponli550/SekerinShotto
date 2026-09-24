"""<state> folder: layout, lock, SQLite index, signed journal (FORMAT.md §5)."""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .contract import ToolError

DB_SCHEMA_VERSION = 4
SUBDIRS = ("inbox", "held", "quarantine", "batches", "journal", "audit", "logs")

# Sync engines copy index.sqlite, -wal and -shm separately and corrupt it.
_SYNCED_MARKERS = ("/Library/Mobile Documents/", "/Library/CloudStorage/",
                   "/Dropbox/", "/Google Drive/", "/OneDrive")


def now_iso() -> str:
    # SEKERINSHOTTO_NOW pins the clock (tests of the 7-day quarantine); never set it in normal use
    pinned = os.environ.get("SEKERINSHOTTO_NOW")
    if pinned:
        return pinned
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def plus_seconds(ts: str, seconds: int) -> str:
    from datetime import timedelta
    return (parse_iso(ts) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


DEFAULT_STATE = "~/.local/share/sekerinshotto"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "sekerinshotto" / "config.json"


def configured_state() -> str | None:
    """The state folder chosen with `config use-state`; panels have no --state, so they rely on it."""
    try:
        return json.loads(config_path().read_text()).get("state")
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def resolve_state(arg: str | None) -> Path:
    # --state > $SEKERINSHOTTO_STATE > config use-state > default
    raw = arg or os.environ.get("SEKERINSHOTTO_STATE") or configured_state() or DEFAULT_STATE
    path = Path(raw).expanduser().resolve()
    probe = str(path) + "/"
    for marker in _SYNCED_MARKERS:
        if marker in probe:
            raise ToolError(f"state folder {path} is inside a synced location ({marker.strip('/')}); "
                            "sync corrupts SQLite. Pass --state with a local, unsynced path")
    return path


class State:
    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "index.sqlite"
        self._lock_fh = None

    def dir(self, name: str) -> Path:
        return self.root / name

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for d in SUBDIRS:
            (self.root / d).mkdir(exist_ok=True)

    @property
    def exists(self) -> bool:
        return self.db_path.exists()

    def bound_content(self) -> Path | None:
        f = self.root / "binding.json"
        return Path(json.loads(f.read_text())["content_root"]) if f.exists() else None

    def bind_content(self, content: Path) -> None:
        f = self.root / "binding.json"
        if not f.exists():
            f.write_text(json.dumps({"content_root": str(content), "bound_at": now_iso()}) + "\n")

    # one writer at a time
    @contextmanager
    def lock(self):
        self.ensure()
        fh = open(self.root / "lock", "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            raise ToolError(f"another sekerinshotto process is writing to {self.root}; try again when it finishes")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()

    def connect(self) -> sqlite3.Connection:
        self.ensure()
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA secure_delete=ON")      # deleted text (a scrubbed secret) is zeroed, not left in free pages
        _migrate(con)
        if con.execute("SELECT 1 FROM items WHERE added_at IS NULL LIMIT 1").fetchone():
            self._backfill_added(con)
        return con

    def _backfill_added(self, con) -> None:
        """added_at for rows from before it existed: the earliest batch whose manifest names the item
        (batch ids are UTC timestamps), else the recorded ingest time."""
        first: dict[str, str] = {}
        for mf in sorted(self.dir("batches").glob("*.jsonl")):
            stamp = mf.stem[:20]                                    # 2026-09-23T07-18-40Z
            ts = stamp[:11] + stamp[11:].replace("-", ":")
            for line in mf.read_text().splitlines():
                iid = json.loads(line).get("id")
                if iid and iid not in first:
                    first[iid] = ts
        for (iid,) in con.execute("SELECT id FROM items WHERE added_at IS NULL").fetchall():
            con.execute("UPDATE items SET added_at = COALESCE(?, ingested_at) WHERE id = ?", (first.get(iid), iid))
        con.commit()

    # ---- signed journal (hash chain + HMAC, adapted from VeriPay) ----
    def _key(self) -> bytes:
        kp = self.root / "journal.key"
        if not kp.exists():
            kp.write_bytes(secrets.token_bytes(32))
            os.chmod(kp, 0o600)
        return kp.read_bytes()

    def journal(self, batch_id: str) -> "Journal":
        return Journal(self.dir("journal") / f"{batch_id}.jsonl", self._key())


def _canonical(row: dict) -> str:
    body = {k: v for k, v in row.items() if k not in ("prev_hash", "row_hash", "sig")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Journal:
    """Append-only JSONL. row_hash = sha256(prev_hash + canonical(row)); sig = HMAC(key, row_hash)."""

    def __init__(self, path: Path, key: bytes):
        self.path, self.key = path, key
        self.prev = ""
        if path.exists():
            lines = path.read_text().splitlines()
            if lines:
                self.prev = json.loads(lines[-1])["row_hash"]

    def append(self, **row) -> None:
        row.setdefault("at", now_iso())
        row_hash = hashlib.sha256((self.prev + _canonical(row)).encode()).hexdigest()
        row.update(prev_hash=self.prev, row_hash=row_hash,
                   sig=hmac.new(self.key, row_hash.encode(), hashlib.sha256).hexdigest())
        with open(self.path, "a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.prev = row_hash


def verify_journal(path: Path, key: bytes) -> tuple[bool, int]:
    """(intact, rows). Any edit, reorder, deletion in the middle, or wrong key fails."""
    prev, n = "", 0
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row.get("prev_hash") != prev:
            return False, n
        expected = hashlib.sha256((prev + _canonical(row)).encode()).hexdigest()
        if row.get("row_hash") != expected or not hmac.compare_digest(
                row.get("sig", ""), hmac.new(key, expected.encode(), hashlib.sha256).hexdigest()):
            return False, n
        prev, n = expected, n + 1
    return True, n


def _migrate(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS items (
        id TEXT PRIMARY KEY,               -- sha256:<hex> of the source bytes
        source_path TEXT NOT NULL,
        source_app TEXT,
        captured_at TEXT,
        width INTEGER, height INTEGER, bytes INTEGER,
        extractor_version TEXT NOT NULL,
        source_state TEXT NOT NULL,        -- present | held | attached | quarantined | purged
        status TEXT NOT NULL,              -- ok | failed
        status_reason TEXT,
        ocr_confidence REAL,
        text_chars INTEGER,
        note_path TEXT,
        batch_id TEXT NOT NULL,
        ingested_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS entities (
        item_id TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,                -- url | qr | domain
        value TEXT NOT NULL,
        raw TEXT, subtype TEXT, verified_by TEXT, confidence REAL
    );
    CREATE INDEX IF NOT EXISTS entities_item ON entities(item_id);
    CREATE INDEX IF NOT EXISTS entities_kind_value ON entities(kind, value);
    CREATE VIRTUAL TABLE IF NOT EXISTS text_fts USING fts5(id UNINDEXED, text);
    """)
    have = {r[1] for r in con.execute("PRAGMA table_info(items)")}
    for col, typ in (("record", "TEXT"), ("category", "TEXT"), ("decided_by", "TEXT"), ("why", "TEXT"),
                     ("group_id", "TEXT"), ("rank", "INTEGER"), ("group_size", "INTEGER"),
                     ("stored_path", "TEXT"), ("purge_after", "TEXT"), ("hold_reason", "TEXT"),
                     ("attempts", "INTEGER"), ("keep", "INTEGER"), ("confirmed_by", "TEXT"), ("added_at", "TEXT")):
        if col not in have:                                    # additive migration, v1 -> v2
            con.execute(f"ALTER TABLE items ADD COLUMN {col} {typ}")
    con.execute("CREATE INDEX IF NOT EXISTS items_group ON items(group_id)")
    con.execute("INSERT OR REPLACE INTO meta VALUES ('db_schema_version', ?)", (str(DB_SCHEMA_VERSION),))
    con.commit()
