"""Scroll sequences: screenshots of one long page, stitched in page order.

Conservative on purpose. A link needs: same app, captured <= MAX_GAP_S apart, not duplicates,
a run of >= MIN_RUN identical content lines (>= MIN_CHARS) sitting where a scroll leaves it
(the end of the upper screenshot, within EDGE lines, and the start of the lower one), and the
lower screenshot must add >= MIN_NEW lines. Scrolling up is detected too (the later screenshot
is the upper part). Matching and joining live in stitch.py (fixed interface set aside, fuzzy lines).
"""
from __future__ import annotations

import re
from datetime import datetime

from . import stitch

MAX_GAP_S = 300


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t).strip().lower()


def _key(t: str) -> str:
    return stitch.key(t)


def content_lines(rec: dict, text: str) -> list[str]:
    lines = text.split("\n") if text else []
    top, bottom = rec.get("chrome_top_n") or 0, rec.get("chrome_bottom_n") or 0
    body = lines[top:len(lines) - bottom if bottom else None]
    return [n for n in (_norm(l) for l in body) if len(_key(n)) >= 4]


def link(upper: list[str], lower: list[str]) -> dict | None:
    return stitch.link(upper, lower)


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
        out.append({"members": chain, "text": stitch.join([lines[m] for m in chain])})
    return out
