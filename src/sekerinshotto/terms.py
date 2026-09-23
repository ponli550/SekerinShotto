"""Key terms: plain TF-IDF over the collection. Statistical, deterministic, no model.

Terms are the "concept" handles the calling LLM and the concepts panel start from;
naming and linking concepts stays with the LLM (FORMAT §4).
"""
from __future__ import annotations

import math
import re
from collections import Counter

TOP_N = 5
MIN_DF = 2                 # a term in one note only is not a concept across notes
MAX_DF_SHARE = 0.25        # in more than a quarter of notes: interface noise, not a concept
_WORD = re.compile(r"[a-z][a-z'-]{3,}")

STOP = set("""
about above after again against also among another around because been before being below between
both cannot could does doing down during each even every from further have having here into itself
just like many more most much must never only other ours over same should some still such than that
their them then there these they this those through under until very were what when where which while
whom will with within without would your yours you're we're it's don't can't isn't
adalah akan atau bagi bahawa banyak boleh dalam dari daripada dengan dia hanya ialah itu jika juga
kami kamu kepada kerana lagi lebih mana masih mereka oleh pada para saja sahaja sama sangat satu
sebagai sedang sejak selepas semua sini sudah supaya tetapi untuk yang ingin anda saya kita ini
follow following followers like likes comment comments share shares reply replies send save saved
view views more less show hide open close back next home search menu settings notifications message
messages chat online typing today yesterday tomorrow edit delete copy report post posts story stories
reels live video photo photos image images link links download upload cancel done okay http https
www com net org please times files volume thanks thank monday tuesday wednesday thursday friday saturday sunday january february march april
june july august september october november december
""".split())


def words(text: str) -> list[str]:
    return [w.strip("'-") for w in _WORD.findall(text.lower()) if w.strip("'-") not in STOP and len(w) >= 4]


def load_stopterms(state_root) -> set[str]:
    """<state>/stopterms.txt: words that must never become key terms (names the redactor misses)."""
    f = state_root / "stopterms.txt" if state_root else None
    if not f or not f.exists():
        return set()
    return {w.strip().lower() for w in f.read_text().splitlines() if w.strip() and not w.startswith("#")}


def key_terms(texts: dict[str, str], extra_stop: set[str] = frozenset()) -> dict[str, list[str]]:
    """{id: [top terms]} for every id. Deterministic: ties broken alphabetically.
    A word the PII redactor removes anywhere (a name, an address part) never becomes a term: terms are
    shown on ambient panels and returned to the calling LLM."""
    from .redact import GIVEN, redact
    tfs, pii = {}, {g.lower() for g in GIVEN.split("|")} | set(extra_stop)   # given names are never concepts
    for i, t in texts.items():
        mine = Counter(words(t))
        pii |= set(mine - Counter(words(redact(t)[0])))          # any occurrence redacted -> excluded everywhere
        tfs[i] = mine
    for tf in tfs.values():
        for w in pii & set(tf):
            del tf[w]
    df = Counter()
    for tf in tfs.values():
        df.update(tf.keys())
    n = max(len(texts), 1)
    ok = {w for w, d in df.items() if d >= MIN_DF and d <= max(MIN_DF, MAX_DF_SHARE * n)}
    out = {}
    for i, tf in tfs.items():
        scored = [(-(c * math.log(n / df[w])), w) for w, c in tf.items() if w in ok]
        out[i] = [w for _, w in sorted(scored)[:TOP_N]]
    return out
