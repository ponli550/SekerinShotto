"""Joining the text of several screenshots: scroll sequences and episode hub notes share this.

A scroll is found by the lines two screenshots share. Two things broke that on real screenshots:
- apps keep a header and a bottom bar fixed while the page scrolls ("My Network / Post / Jobs"), so the
  shared block sat 5-8 lines from the edge and a 3-line edge test rejected it;
- OCR reads the same line slightly differently in two shots ("of the" / "ofthe", a dropped comma).
  Digits must still match exactly: numbered lines differ on purpose.
So lines that sit identically at the top (or bottom) of BOTH screenshots are treated as fixed interface,
set aside before matching and kept out of the joined text, and lines match when >= 90% similar.
Measured on the 1,049-item trial: close same-app pairs linked went from 2 to 8.
"""
from __future__ import annotations

import difflib
import re

EDGE, MIN_RUN, MIN_CHARS, MIN_NEW = 3, 2, 30, 3
FUZZY, FUZZY_MIN = 0.90, 12            # similarity for two OCR readings of one line, and the length it needs
REPEAT_WINDOW = 30                     # a line seen this recently is a repeated header, not new content


def key(t: str) -> str:
    """Comparison form: OCR spaces the same line differently across shots."""
    return re.sub(r"[^0-9a-z]", "", t.lower())


def same(a: str, b: str) -> bool:
    """a, b in key() form."""
    if a == b:
        return True
    if len(a) < FUZZY_MIN or len(b) < FUZZY_MIN:
        return False
    if re.sub(r"\D", "", a) != re.sub(r"\D", "", b):
        return False                       # "paragraph 00" vs "paragraph 09": numbers differ on purpose
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= FUZZY


def sticky(a: list[str], b: list[str]) -> tuple[int, int]:
    """(top, bottom): how many leading and trailing lines both screenshots share in place — the app's fixed
    header and bottom bar. At least one line of each is always left for matching."""
    ka, kb = [key(x) for x in a], [key(x) for x in b]
    n = min(len(ka), len(kb))
    top = 0
    while top < n - 1 and same(ka[top], kb[top]):
        top += 1
    bot = 0
    while bot < n - top - 1 and same(ka[-1 - bot], kb[-1 - bot]):
        bot += 1
    return top, bot


def longest_run(a: list[str], b: list[str]) -> tuple[int, int, int, int]:
    """(run, chars, i, j): the longest block of consecutive lines a[i:] and b[j:] share (fuzzy)."""
    ka, kb = [key(x) for x in a], [key(x) for x in b]
    best = (0, 0, 0, 0)
    for i in range(len(ka)):
        for j in range(len(kb)):
            if not same(ka[i], kb[j]):
                continue
            k = 1
            while i + k < len(ka) and j + k < len(kb) and same(ka[i + k], kb[j + k]):
                k += 1
            chars = sum(len(x) for x in ka[i:i + k])
            if (k, chars) > best[:2]:
                best = (k, chars, i, j)
    return best


def link(upper: list[str], lower: list[str]) -> dict | None:
    """Does `lower` continue `upper` downwards? With the fixed interface set aside, the shared block must
    end near upper's bottom and start near lower's top, and lower must add lines after it.
    -> {run, chars, top, bottom, lower_from, lower_to}: lower[lower_from:lower_to] is what lower adds."""
    if not upper or not lower:
        return None
    top, bot = sticky(upper, lower)
    u, lo = upper[top:len(upper) - bot], lower[top:len(lower) - bot]
    k, chars, i, j = longest_run(u, lo)
    if k < MIN_RUN or chars < MIN_CHARS:
        return None
    if len(u) - (i + k) > EDGE or j > EDGE or len(lo) - (j + k) < MIN_NEW:
        return None
    return {"run": k, "chars": chars, "top": top, "bottom": bot,
            "lower_from": top + j + k, "lower_to": len(lower) - bot}


# Buttons printed under every post in a feed. A short line made only of these is interface, not the page;
# it is kept out of a joined text (the per-screenshot notes keep everything OCR read).
UI_WORDS = {"like", "liker", "likes", "comment", "comments", "repost", "reposts", "send", "share", "reply",
            "replies", "follow", "following", "view", "activity", "top", "translate", "see", "translation",
            "more", "less", "save", "report", "v", ">", "•", "…"}


def is_ui(line: str) -> bool:
    words = re.findall(r"[^\s]+", line.lower())
    return 0 < len(words) <= 4 and all(w.strip(".,:;!?()[]") in UI_WORDS for w in words)


def join(parts: list[list[str]]) -> list[str]:
    """A scroll chain as one text: the first screenshot without the bottom bar it shares with the next,
    then what each later one adds. A line already among the last REPEAT_WINDOW joined lines is dropped:
    feeds repeat a post's header ("anonouswill > kerja kosong 10h") as you scroll."""
    merged: list[str] = []
    for n, lines in enumerate(parts):
        if n == 0:
            bot = sticky(lines, parts[1])[1] if len(parts) > 1 else 0
            add = lines[:len(lines) - bot]
        else:
            lk = link(parts[n - 1], lines)
            add = lines[lk["lower_from"]:lk["lower_to"]] if lk else ["…"] + lines
        for line in add:
            if is_ui(line):
                continue
            k = key(line)
            if line != "…" and len(k) >= 8 and any(same(k, key(m)) for m in merged[-REPEAT_WINDOW:]):
                continue
            merged.append(line)
    return merged
