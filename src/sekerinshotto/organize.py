"""Classification, duplicate groups and ranking over the whole index (deterministic)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .extract import hamming
from .notes import WRITEBACK_BY, read_frontmatter
from .rules import classify

NEAR_DUP = 0.80          # exact Jaccard of content tokens (words + figures, status/nav bars excluded)
CONTAINED = 0.85         # |A∩B| / min(|A|,|B|): one screenshot's content sits inside the other (crop, viewer)
MIN_TOKENS = 10          # below this, text is too thin to call two screenshots duplicates
DHASH_MAX, DHASH_JACCARD = 6, 0.50
BANDS, ROWS = 32, 2      # LSH over the 64-value MinHash; lenient so containment pairs become candidates
# Measured on the 182-screenshot sample: true duplicates 0.80-0.97 containment; same-template sleep
# reports from different days <= 0.70; a document shown inside a larger document 0.83 (not grouped).


def load_items(con) -> dict[str, dict]:
    items = {}
    for r in con.execute("SELECT id, record, note_path, category, decided_by, why, group_id, rank, group_size "
                         "FROM items WHERE record IS NOT NULL"):
        rec = json.loads(r["record"])
        rec["_note_path"], rec["_prev"] = r["note_path"], {
            "category": r["category"], "decided_by": r["decided_by"], "why": r["why"], "group": r["group_id"],
            "rank": r["rank"], "size": r["group_size"]}
        items[r["id"]] = rec
    for r in con.execute("SELECT id, text FROM text_fts"):
        if r["id"] in items:
            items[r["id"]]["_text"] = r["text"]
    return items


class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b, why, edges):
        ra, rb = self.find(a), self.find(b)
        edges.append((a, b, why))
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def group(items: dict[str, dict]) -> tuple[dict[str, list[str]], list[tuple]]:
    uf, edges = _UF(), []
    by_qr = defaultdict(list)
    for iid, rec in items.items():
        for b in rec["entities"]["qr"]:
            by_qr[b["payload"]].append(iid)
    for ids in by_qr.values():
        for other in ids[1:]:
            uf.union(ids[0], other, "same QR payload", edges)

    buckets = defaultdict(list)
    for iid, rec in items.items():
        sig = rec.get("sig") or []
        if len(sig) == BANDS * ROWS and rec.get("content_tokens", 0) >= MIN_TOKENS:
            for b in range(BANDS):
                buckets[(b, tuple(sig[b * ROWS:(b + 1) * ROWS]))].append(iid)
    seen = set()
    for ids in buckets.values():
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                a, b = sorted((ids[i], ids[j]))
                if (a, b) in seen:
                    continue
                seen.add((a, b))
                ta, tb = set(items[a].get("toks") or []), set(items[b].get("toks") or [])
                if not ta or not tb:
                    continue
                inter = len(ta & tb)
                jac, cont = inter / len(ta | tb), inter / min(len(ta), len(tb))
                if jac >= NEAR_DUP:
                    uf.union(a, b, f"text {jac:.2f} similar", edges)
                elif cont >= CONTAINED:
                    uf.union(a, b, f"content {cont:.2f} contained", edges)
                elif jac >= DHASH_JACCARD and hamming(items[a].get("dhash"), items[b].get("dhash")) <= DHASH_MAX:
                    uf.union(a, b, f"text {jac:.2f} + image hash", edges)
    comps = defaultdict(list)
    for iid in items:
        comps[uf.find(iid)].append(iid)
    return {root: sorted(ids) for root, ids in comps.items() if len(ids) > 1}, edges


def score(rec: dict) -> tuple[float, str]:
    qr = len(rec["entities"]["qr"])
    links = sum(1 for u in rec["entities"]["urls"] if u["verified_by"] in ("qr", "known", "crossref") and not u.get("flag"))
    chars = rec.get("text_chars") or 0
    conf = rec.get("ocr_confidence") or 0.0
    px = (rec.get("width") or 0) * (rec.get("height") or 0)
    s = 3 * qr + 2 * links + min(chars / 300, 4) + 2 * conf + min(px / 3.2e6, 1)
    return round(s, 3), f"QR {qr}, links {links}, {chars} chars, confidence {conf:.2f}, {px / 1e6:.1f} MP"


def organize(items: dict[str, dict], rules, content: Path) -> dict[str, dict]:
    """-> {id: {category, decided_by, why, group, rank, size, score, score_why}}"""
    out = {}
    for iid, rec in items.items():
        cat, why, by = None, None, "rule"
        note = content / rec["_note_path"] if rec.get("_note_path") else None
        if note and note.exists():
            fm = read_frontmatter(note.read_text())
            if fm.get("decided_by") in WRITEBACK_BY:              # a caller decided; never override
                cat, by, why = fm.get("category"), fm["decided_by"], f"set by {fm['decided_by']}"
        if cat is None:
            qr_types = {b["type"] for b in rec["entities"]["qr"]}
            cat, why = classify(rules, rec.get("source_app"), qr_types, rec["entities"]["domains"], rec.get("_text", ""))
            by = "rule" if cat != "uncategorized" else None
        sc, sc_why = score(rec)
        out[iid] = {"category": cat, "decided_by": by, "why": why, "group": None, "rank": None,
                    "size": None, "score": sc, "score_why": sc_why}

    comps, _ = group(items)
    taken = set()
    for ids in sorted(comps.values(), key=lambda v: v[0]):
        old = Counter(items[i]["_prev"]["group"] for i in ids if items[i]["_prev"]["group"])
        gid = next((g for g, _ in sorted(old.items(), key=lambda kv: (-kv[1], kv[0])) if g not in taken), None)
        gid = gid or "grp-" + ids[0].split(":")[1][:8]
        taken.add(gid)
        # best score first; ties go to the newer capture, then to the id (stable, deterministic)
        ranked = sorted(ids)
        ranked.sort(key=lambda i: items[i].get("captured_at") or "", reverse=True)
        ranked.sort(key=lambda i: out[i]["score"], reverse=True)
        for rank, iid in enumerate(ranked, 1):
            out[iid].update(group=gid, rank=rank, size=len(ids))
    return out


def changed(rec: dict, org: dict) -> bool:
    p = rec["_prev"]
    return (p["category"], p["decided_by"], p.get("why"), p["group"], p["rank"], p["size"]) != \
        (org["category"], org["decided_by"], org["why"], org["group"], org["rank"], org["size"])
