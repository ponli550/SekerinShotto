"""Scroll sequences: screenshots of one long page, stitched in page order.

Conservative on purpose. A link needs: same app, captured <= MAX_GAP_S apart, not duplicates,
a run of >= MIN_RUN identical content lines (>= MIN_CHARS) sitting where a scroll leaves it
(the end of the upper screenshot, within EDGE lines, and the start of the lower one), and the
lower screenshot must add >= MIN_NEW lines. Scrolling up is detected too (the later screenshot
is the upper part). On the 182-screenshot sample no pair met this bar, so precision on real data
is unmeasured; see FORMAT §6b.
"""
from __future__ import annotations

import re
from datetime import datetime

MAX_GAP_S = 300
MIN_RUN, MIN_CHARS, MIN_NEW, EDGE = 2, 30, 3, 3


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def _key(t: str) -> str:
    """Comparison form: OCR spaces the same line differently across shots ("of the" / "ofthe")."""
    return re.sub(r"[^0-9a-z]", "", t.lower())


def content_lines(rec: dict, text: str) -> list[str]:
    lines = text.split("\n") if text else []
    top, bottom = rec.get("chrome_top_n") or 0, rec.get("chrome_bottom_n") or 0
    body = lines[top:len(lines) - bottom if bottom else None]
    return [n for n in (_norm(l) for l in body) if len(_key(n)) >= 4]


def _longest_run(a: list[str], b: list[str]) -> tuple[int, int, int, int]:
    """(run, chars, i_start, j_start) of the longest common contiguous block of lines, compared by _key."""
    ka, kb = [_key(x) for x in a], [_key(x) for x in b]
    best = (0, 0, 0, 0)
    pos: dict[str, list[int]] = {}
    for j, line in enumerate(kb):
        pos.setdefault(line, []).append(j)
    for i, line in enumerate(ka):
        for j in pos.get(line, []):
            k = 0
            while i + k < len(ka) and j + k < len(kb) and ka[i + k] == kb[j + k]:
                k += 1
            chars = sum(len(x) for x in ka[i:i + k])
            if (k, chars) > best[:2]:
                best = (k, chars, i, j)
    return best


def link(upper: list[str], lower: list[str]) -> dict | None:
    """Does `lower` continue `upper` downwards? The shared run must end near upper's bottom and start
    near lower's top, and lower must add new lines after it."""
    k, chars, i, j = _longest_run(upper, lower)
    if k < MIN_RUN or chars < MIN_CHARS:
        return None
    if len(upper) - (i + k) > EDGE or j > EDGE or len(lower) - (j + k) < MIN_NEW:
        return None
    return {"run": k, "chars": chars, "upper_end": i + k, "lower_from": j + k}


def _ts(rec) -> datetime | None:
    try:
        return datetime.strptime(rec["captured_at"], "%Y-%m-%dT%H:%M:%S")
    except (KeyError, TypeError, ValueError):
        return None


def find(items: dict[str, dict], dup_group: dict[str, str | None]) -> list[dict]:
    """-> [{"members": [ids in page order], "text": stitched lines}] for every chain of >= 2."""
    lines = {i: content_lines(r, r.get("_text", "")) for i, r in items.items()}
    by_app: dict[str, list[str]] = {}
    for i, r in items.items():
        if r.get("source_app") and _ts(r):
            by_app.setdefault(r["source_app"], []).append(i)
    nxt: dict[str, str] = {}            # page order: upper -> lower
    for ids in by_app.values():
        ids.sort(key=lambda i: (items[i]["captured_at"], i))
        for a, b in zip(ids, ids[1:]):
            if (_ts(items[b]) - _ts(items[a])).total_seconds() > MAX_GAP_S:
                continue
            if dup_group.get(a) and dup_group.get(a) == dup_group.get(b):
                continue                                  # duplicates, not a scroll
            if link(lines[a], lines[b]):
                upper, lower = a, b                       # scrolled down
            elif link(lines[b], lines[a]):
                upper, lower = b, a                       # scrolled up
            else:
                continue
            if upper not in nxt and lower not in nxt.values():
                nxt[upper] = lower
    heads = [u for u in nxt if u not in nxt.values()]
    out = []
    for h in sorted(heads):
        chain, seen = [h], {h}
        while chain[-1] in nxt and nxt[chain[-1]] not in seen:
            chain.append(nxt[chain[-1]])
            seen.add(chain[-1])
        merged = list(lines[chain[0]])
        for m in chain[1:]:
            lk = link(merged, lines[m]) or link(lines[chain[chain.index(m) - 1]], lines[m])
            merged += lines[m][lk["lower_from"]:] if lk else ["…"] + lines[m]
        out.append({"members": chain, "text": merged})
    return out
