"""Classification, duplicate groups and ranking over the whole index (deterministic)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .extract import hamming
from .notes import WRITEBACK_BY, read_frontmatter
from .rules import classify
from .terms import key_terms
from . import sequences as seqmod

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
            "rank": r["rank"], "size": r["group_size"], "terms": rec.get("terms", []),
            "sequence": rec.get("sequence"), "seq_part": rec.get("seq_part"), "seq_size": rec.get("seq_size")}
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
    links = sum(1 for u in rec["entities"]["urls"] if u["verified_by"] in ("qr", "known", "crossref", "allowed") and not u.get("flag"))
    chars = rec.get("text_chars") or 0
    conf = rec.get("ocr_confidence") or 0.0
    px = (rec.get("width") or 0) * (rec.get("height") or 0)
    s = 3 * qr + 2 * links + min(chars / 300, 4) + 2 * conf + min(px / 3.2e6, 1)
    return round(s, 3), f"QR {qr}, links {links}, {chars} chars, confidence {conf:.2f}, {px / 1e6:.1f} MP"


# ---------------------------------------------------------------- sessions
# A talk, a training or a trip is photographed minutes apart, and most of its slides never name the topic
# ("Same foundations. New actors."). No keyword rule can reach those; their neighbours can.
SESSION_GAP_S = 10 * 60       # a new session starts after 10 minutes with no photo from the same source
SESSION_MIN = 3               # categorized members needed before a majority means anything
SESSION_SHARE = 0.60          # ...and the share of them that must agree


def _t(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:19])


def sessions(items: dict[str, dict]) -> list[list[str]]:
    """Runs of items from the same source (app, or no app for camera photos) whose consecutive capture
    times are at most SESSION_GAP_S apart. Only runs of two or more."""
    by_src: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for iid, rec in items.items():
        if rec.get("captured_at"):
            by_src[rec.get("source_app") or ""].append((rec["captured_at"][:19], iid))
    runs = []
    for lst in by_src.values():
        lst.sort()
        cur = [lst[0]]
        for prev, nxt in zip(lst, lst[1:]):
            if (_t(nxt[0]) - _t(prev[0])).total_seconds() > SESSION_GAP_S:
                runs.append(cur)
                cur = []
            cur.append(nxt)
        runs.append(cur)
    return [[i for _, i in r] for r in runs if len(r) >= 2]


def inherit_sessions(items: dict[str, dict], out: dict[str, dict]) -> None:
    """Uncategorized members of a session take the category a caller (user, LLM, Laya) set on any member,
    when those agree; otherwise the majority of categorized members. decided_by `session` is not a
    write-back: it is recomputed on every organize, so re-tagging the seed moves the whole session."""
    for members in sessions(items):
        unc = [i for i in members if out[i]["category"] == "uncategorized" and out[i]["decided_by"] is None]
        if not unc:
            continue
        times = sorted(items[i]["captured_at"][:16].replace("T", " ") for i in members)
        span = f"{times[0]}–{times[-1][11:]}" if times[0][:10] == times[-1][:10] else f"{times[0]}–{times[-1]}"
        seeds = [i for i in members if out[i]["decided_by"] in WRITEBACK_BY]
        cats = Counter(out[i]["category"] for i in seeds)
        if len(cats) == 1:
            cat = next(iter(cats))
            by = Counter(out[i]["decided_by"] for i in seeds).most_common(1)[0][0]
            why = f"session: set by {by} on {len(seeds)} of {len(members)} photos, {span}"
        else:
            labelled = Counter(out[i]["category"] for i in members if out[i]["category"] != "uncategorized")
            total = sum(labelled.values())
            if total < SESSION_MIN:
                continue
            cat, n = labelled.most_common(1)[0]
            if n / total < SESSION_SHARE:
                continue
            why = f"session: {n} of {total} categorized photos, {span}, are {cat}"
        for i in unc:
            out[i].update(category=cat, decided_by="session", why=why)


def organize(items: dict[str, dict], rules, content: Path, stopterms: set[str] = frozenset()) -> dict[str, dict]:
    """-> {id: {category, decided_by, why, group, rank, size, score, score_why}}"""
    out = {}
    for iid, rec in items.items():
        cat, why, by = None, None, "rule"
        note = content / rec["_note_path"] if rec.get("_note_path") else None
        if note and note.exists():
            fm = read_frontmatter(note.read_text())
            if fm.get("decided_by") in WRITEBACK_BY:              # a caller decided; never override
                ev = fm.get("decided_evidence")
                cat, by = fm.get("category"), fm["decided_by"]
                why = f"set by {by}" + (f", quoting {ev!r}" if ev else "")
        if cat is None:
            qr_types = {b["type"] for b in rec["entities"]["qr"]}
            cat, why = classify(rules, rec.get("source_app"), qr_types, rec["entities"]["domains"], rec.get("_text", ""),
                                 rec.get("code"))
            by = "rule" if cat != "uncategorized" else None
        sc, sc_why = score(rec)
        out[iid] = {"category": cat, "decided_by": by, "why": why, "group": None, "rank": None,
                    "size": None, "score": sc, "score_why": sc_why}

    inherit_sessions(items, out)

    terms = key_terms({i: r.get("_text", "") for i, r in items.items()}, stopterms)
    for i in out:
        out[i]["terms"] = terms.get(i, [])

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

    for o in out.values():
        o.update(sequence=None, seq_part=None, seq_size=None)
    taken_seq = set()
    for sq in seqmod.find(items, {i: o["group"] for i, o in out.items()}):
        ids = sq["members"]
        old = Counter(items[i]["_prev"].get("sequence") for i in ids if items[i]["_prev"].get("sequence"))
        sid = next((g for g, _ in sorted(old.items(), key=lambda kv: (-kv[1], kv[0])) if g not in taken_seq), None)
        sid = sid or "seq-" + sorted(ids)[0].split(":")[1][:8]
        taken_seq.add(sid)
        for part, iid in enumerate(ids, 1):
            out[iid].update(sequence=sid, seq_part=part, seq_size=len(ids), seq_text=sq["text"])
    return out


def changed(rec: dict, org: dict) -> bool:
    p = rec["_prev"]
    keys = ("category", "decided_by", "why", "group", "rank", "size", "terms", "sequence", "seq_part", "seq_size")
    return tuple(p.get(k) for k in keys) != tuple(org.get(k) for k in keys)
