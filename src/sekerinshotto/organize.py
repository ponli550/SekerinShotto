"""Classification, duplicate groups and ranking over the whole index (deterministic)."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .extract import hamming
from .notes import WRITEBACK_BY, read_frontmatter
from .rules import KINDS, UNCATEGORIZED, classify, topics_of
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
            "sequence": rec.get("sequence"), "seq_part": rec.get("seq_part"), "seq_size": rec.get("seq_size"),
            "episode": rec.get("episode"), "ep_part": rec.get("ep_part"), "ep_size": rec.get("ep_size"),
            "ep_label": rec.get("ep_label"), "topics": rec.get("topics") or []}
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
SESSION_GAP_S = 10 * 60       # a new session starts after 10 minutes with no screenshot from the same app
CAMERA_GAP_S = 30 * 60        # camera photos (no app) are taken on purpose at a talk: a break is not an end.
                              # Measured: the ODC training split in two at a 17-minute break. Bridging by shared
                              # words failed: unrelated Threads sessions shared more rare words (5-8) than the
                              # two halves of the training (2).
EPISODE_MIN_CAMERA = 3        # a session gets an episode hub note from this many camera photos...
EPISODE_MIN_APP = 5           # ...or this many screenshots from one app
SESSION_MIN = 3               # categorized members needed before a majority means anything
SESSION_SHARE = 0.60          # ...and the share of them that must agree


def _t(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:19])


def sessions(items: dict[str, dict]) -> list[list[str]]:
    """Runs of items from the same source (app, or no app for camera photos) whose consecutive capture
    times are at most SESSION_GAP_S (CAMERA_GAP_S for camera photos) apart. Only runs of two or more.
    Members are in capture order."""
    by_src: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for iid, rec in items.items():
        if rec.get("captured_at"):
            by_src[rec.get("source_app") or ""].append((rec["captured_at"][:19], iid))
    runs = []
    for src, lst in by_src.items():
        lst.sort()
        gap = SESSION_GAP_S if src else CAMERA_GAP_S
        cur = [lst[0]]
        for prev, nxt in zip(lst, lst[1:]):
            if (_t(nxt[0]) - _t(prev[0])).total_seconds() > gap:
                runs.append(cur)
                cur = []
            cur.append(nxt)
        runs.append(cur)
    return [[i for _, i in r] for r in runs if len(r) >= 2]


def label_slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(label).lower()).strip("-")[:40]


CAMERA_NAME = re.compile(r"^(IMG|PXL|MVIMG)_\d{8}_\d{6}", re.I)     # Android camera; screenshots say Screenshot_


def is_camera(rec: dict) -> bool:
    if rec.get("source_app") == "com.apple.camera":
        return True
    return not rec.get("source_app") and bool(CAMERA_NAME.match(Path(rec.get("source_path") or "").name))


def decide_kind(rec: dict, app_kind: str, why: str) -> tuple[str, str]:
    """Kind = what the image is. Code and receipts first (they are what they show), then a visual image is a
    photo, a camera photo with text is a slide, then the app's kind; anything else is a screenshot."""
    from .cleanup import is_visual
    if app_kind in ("code", "receipt"):
        return app_kind, why
    if is_visual({**rec, "category": app_kind}):
        return "photo", f"visual: text covers {rec.get('text_coverage', 0):.0%} of the image"
    if is_camera(rec):
        return "slide", "camera photo with text"
    if app_kind != UNCATEGORIZED:
        return app_kind, why
    return "screenshot", "no kind rule matched"


def assign_episodes(items: dict[str, dict], out: dict[str, dict], content: Path) -> None:
    """Big enough sessions become episodes: an id (kept from the previous run when most members had it),
    their order, and the label the user wrote in the hub note (`label:`), which reaches every member."""
    for o in out.values():
        o.update(episode=None, ep_part=None, ep_size=None, ep_label=None)
    taken = set()
    for members in sorted(sessions(items), key=lambda m: m[0]):
        camera = not items[members[0]].get("source_app")
        if len(members) < (EPISODE_MIN_CAMERA if camera else EPISODE_MIN_APP):
            continue
        old = Counter(items[i]["_prev"].get("episode") for i in members if items[i]["_prev"].get("episode"))
        eid = next((e for e, _ in sorted(old.items(), key=lambda kv: (-kv[1], kv[0])) if e not in taken), None)
        eid = eid or "ep-" + members[0].split(":")[1][:8]
        taken.add(eid)
        hub = content / "episodes" / f"{eid}.md"
        label = read_frontmatter(hub.read_text()).get("label") if hub.exists() else None
        label = str(label).strip() if label not in (None, "") else None
        for part, iid in enumerate(members, 1):
            out[iid].update(episode=eid, ep_part=part, ep_size=len(members), ep_label=label)


def _names(v) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[,\s]+", v) if x]
    return [str(x).strip().lower() for x in (v or []) if str(x).strip()]


def assign_topics(items: dict[str, dict], out: dict[str, dict], topic_rules, fms: dict[str, dict]) -> None:
    """Topics, several per note: every matching topic rule; a category a caller set; the note's own
    `topics_added` / `topics_removed` (the user's, kept on re-render); then, for members of a session with
    none, the session's topics (added by the user on any member, else those on >= 60% of >= 3 members)."""
    added: dict[str, list[str]] = {}
    for iid, rec in items.items():
        fm = fms.get(iid, {})
        found: dict[str, str] = {}
        if topic_rules:
            found = topics_of(topic_rules, rec.get("source_app"), {b["type"] for b in rec["entities"]["qr"]},
                              rec["entities"]["domains"], rec.get("_text", ""), rec.get("code"))
        o = out[iid]
        added[iid] = _names(fm.get("topics_added"))
        for t in added[iid]:
            found[t] = "added by user"
        if fm.get("_legacy_topic"):                         # a caller's category from before kinds: the user's
            found[fm["_legacy_topic"]] = f"set by {fm.get('decided_by')} (was its category)"
            added[iid] = sorted(set(added[iid]) | {fm["_legacy_topic"]})
        for t in _names(fm.get("topics_removed")):
            found.pop(t, None)
        o["topics"], o["topic_why"] = sorted(found), found
    for members in sessions(items):
        bare = [i for i in members if not out[i]["topics"] and not _names(fms.get(i, {}).get("topics_removed"))]
        if not bare:
            continue
        seeded = sorted({t for i in members for t in added[i]})
        if seeded:
            inherit = {t: "session: added by user on a member" for t in seeded}
        else:
            having = [i for i in members if out[i]["topics"]]
            if len(having) < SESSION_MIN:
                continue
            counts = Counter(t for i in having for t in out[i]["topics"])
            inherit = {t: f"session: on {n} of {len(having)} photos" for t, n in counts.items()
                       if n / len(having) >= SESSION_SHARE}
        for i in bare:
            out[i]["topics"], out[i]["topic_why"] = sorted(inherit), dict(inherit)


def organize(items: dict[str, dict], rules, content: Path, stopterms: set[str] = frozenset(),
             topic_rules=None) -> dict[str, dict]:
    """-> {id: {category, decided_by, why, group, rank, size, score, score_why, topics, episode, ...}}"""
    out, fms = {}, {}
    for iid, rec in items.items():
        cat, why, by = None, None, "rule"
        note = content / rec["_note_path"] if rec.get("_note_path") else None
        if note and note.exists():
            fm = fms[iid] = read_frontmatter(note.read_text())
            if fm.get("decided_by") in WRITEBACK_BY:              # a caller decided; never override
                k = fm.get("kind") or fm.get("category")
                if k in KINDS:
                    ev = fm.get("decided_evidence")
                    cat, by = k, fm["decided_by"]
                    why = f"set by {by}" + (f", quoting {ev!r}" if ev else "")
                elif k and k != UNCATEGORIZED:
                    fm["_legacy_topic"] = k                        # a caller's category from before kinds
        if cat is None:
            qr_types = {b["type"] for b in rec["entities"]["qr"]}
            k, why = classify(rules, rec.get("source_app"), qr_types, rec["entities"]["domains"], rec.get("_text", ""),
                              rec.get("code"))
            cat, why = decide_kind(rec, k, why)
            by = "rule"
        sc, sc_why = score(rec)
        out[iid] = {"category": cat, "decided_by": by, "why": why, "group": None, "rank": None,
                    "size": None, "score": sc, "score_why": sc_why}

    assign_episodes(items, out, content)
    assign_topics(items, out, topic_rules, fms)

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
    keys = ("category", "decided_by", "why", "group", "rank", "size", "terms", "sequence", "seq_part", "seq_size",
            "episode", "ep_part", "ep_size", "ep_label", "topics")
    return tuple(p.get(k) for k in keys) != tuple(org.get(k) for k in keys)
