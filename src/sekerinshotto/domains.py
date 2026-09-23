"""Reference lists for URL checks: Tranco ranks, Public Suffix List, IANA TLDs.

Downloaded once by `domains update --commit` into <state>/domains/. This is the
tool's only network access, and it fetches reference lists only: it never
fetches a URL read from a screenshot. Everything after the download is offline.
"""
from __future__ import annotations

import io
import json
import sqlite3
import urllib.request
import zipfile
from functools import lru_cache
from pathlib import Path

from .contract import ToolError
from .state import now_iso

TRANCO_LATEST = "https://tranco-list.eu/api/lists/date/latest"
PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
IANA_URL = "https://data.iana.org/TLD/tlds-alpha-by-domain.txt"
_UA = {"User-Agent": "sekerinshotto (reference-list download)"}


def _get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def update(folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    try:
        meta = json.loads(_get(TRANCO_LATEST, 30))
        blob = _get(meta["download"])
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"could not download the Tranco list: {e}")
    if blob[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            blob = z.read(z.namelist()[0])
    tmp = folder / "ranks.sqlite.tmp"
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE ranks (domain TEXT PRIMARY KEY, rank INTEGER NOT NULL) WITHOUT ROWID")
    rows = (line.split(",", 1) for line in blob.decode().splitlines() if "," in line)
    con.executemany("INSERT OR IGNORE INTO ranks VALUES (?, ?)", ((d.strip().lower(), int(r)) for r, d in rows))
    n = con.execute("SELECT COUNT(*) FROM ranks").fetchone()[0]
    con.commit()
    con.close()
    tmp.replace(folder / "ranks.sqlite")
    try:
        (folder / "psl.dat").write_bytes(_get(PSL_URL))
        (folder / "tlds.txt").write_bytes(_get(IANA_URL))
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"could not download the suffix/TLD lists: {e}")
    info = {"tranco_list_id": meta.get("list_id"), "tranco_created": meta.get("created_on"),
            "domains": n, "updated_at": now_iso(),
            "attribution": "Tranco (Le Pochat et al., NDSS 2019), https://tranco-list.eu/; "
                           "Public Suffix List (MPL-2.0); IANA root zone TLD list"}
    (folder / "info.json").write_text(json.dumps(info, indent=1) + "\n")
    return info


class PSL:
    def __init__(self, text: str):
        self.rules, self.wild, self.exc = set(), set(), set()
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            line = line.split()[0].lower()
            if line.startswith("!"):
                self.exc.add(line[1:])
            elif line.startswith("*."):
                self.wild.add(line[2:])
            else:
                self.rules.add(line)

    def registrable(self, host: str) -> str | None:
        """eTLD+1 (e.g. shop.com.my), or None if host is itself a public suffix."""
        labels = host.lower().strip(".").split(".")
        best = 1                                     # default rule "*"
        for i in range(len(labels)):
            cand = ".".join(labels[i:])
            if cand in self.exc:
                best = len(labels) - i - 1
                break
            if cand in self.rules or (i + 1 < len(labels) and ".".join(labels[i + 1:]) in self.wild):
                best = max(best, len(labels) - i)
        if len(labels) <= best:
            return None
        return ".".join(labels[-(best + 1):])


class DomainIndex:
    """Offline lookups. `available` is False until `domains update` has run."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.available = (folder / "ranks.sqlite").exists() and (folder / "psl.dat").exists()
        self.info = json.loads((folder / "info.json").read_text()) if (folder / "info.json").exists() else {}
        if self.available:
            self.psl = PSL((folder / "psl.dat").read_text())
            self.tlds = {t.strip().lower() for t in (folder / "tlds.txt").read_text().splitlines()
                         if t and not t.startswith("#")}
            self._con = sqlite3.connect(f"file:{folder / 'ranks.sqlite'}?mode=ro", uri=True,
                                        check_same_thread=False)

    def valid_tld(self, host: str) -> bool | None:
        if not self.available:
            return None
        return host.lower().rsplit(".", 1)[-1] in self.tlds

    def registrable(self, host: str) -> str | None:
        return self.psl.registrable(host) if self.available else None

    @lru_cache(maxsize=65536)
    def rank(self, domain: str | None) -> int | None:
        if not self.available or not domain:
            return None
        row = self._con.execute("SELECT rank FROM ranks WHERE domain=?", (domain.lower(),)).fetchone()
        return row[0] if row else None
