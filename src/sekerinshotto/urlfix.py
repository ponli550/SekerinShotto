"""Correct OCR lookalike domains, deterministically and never silently.

A misread like docs.qoogle.com or Inkd.in is corrected only when evidence points
to exactly one candidate:
  - crossref: the candidate's domain was decoded from a QR code somewhere in the index
  - known:    the candidate ranks far higher in the Tranco list than the raw reading
The raw reading is always kept, with the reason. Hosts that cannot be real
(invalid characters, a TLD that does not exist) are flagged, not guessed.
"""
from __future__ import annotations

import re
from itertools import combinations

from .domains import DomainIndex

# OCR confusions seen on screenshots, applied to the raw (case-preserving) host.
CONFUSIONS: dict[str, tuple[str, ...]] = {
    "I": ("l",), "|": ("l", "i"), "1": ("l", "i"), "l": ("1", "i"), "i": ("l",),
    "q": ("g",), "g": ("q",), "0": ("o",), "o": ("0",), "O": ("o", "0"),
    "rn": ("m",), "m": ("rn",), "vv": ("w",), "5": ("s",), "s": ("5",), "cl": ("d",),
}
MAX_EDITS = 2
POPULAR = 100_000          # a raw reading this popular is trusted as-is
MARGIN = 100               # candidate must rank at least this many times better than the raw reading
_HOST_OK = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")


def _sites(host: str) -> list[tuple[int, int, tuple[str, ...]]]:
    out = []
    for i in range(len(host)):
        for key, subs in CONFUSIONS.items():
            if host.startswith(key, i):
                out.append((i, len(key), subs))
    return out


def candidates(raw_host: str) -> dict[str, int]:
    """{host: fewest confusion substitutions needed} within MAX_EDITS of raw_host."""
    sites = _sites(raw_host)
    out: dict[str, int] = {}
    for k in range(1, MAX_EDITS + 1):
        for combo in combinations(sites, k):
            spans = sorted(combo)
            if any(a[0] + a[1] > b[0] for a, b in zip(spans, spans[1:])):
                continue                                  # overlapping edits
            variants = [""]
            pos = 0
            for i, ln, subs in spans:
                variants = [v + raw_host[pos:i] + s for v in variants for s in subs]
                pos = i + ln
            for v in variants:
                h = (v + raw_host[pos:]).lower()
                if h != raw_host.lower() and _HOST_OK.match(h) and h not in out:
                    out[h] = k
    return out


def resolve(raw_host: str, dom: DomainIndex, qr_domains: set[str], allowed: set[str] = frozenset()) -> dict:
    """-> {"host", "verified_by", "corrected", "reason", "flag"} for one OCR-read host."""
    host = raw_host.lower()
    valid_syntax = bool(_HOST_OK.match(host))
    res = {"host": host, "verified_by": "none", "corrected": False, "reason": None, "flag": None}

    def reg(h):
        return dom.registrable(h) if dom.available else ".".join(h.split(".")[-2:])

    if valid_syntax and reg(host) in qr_domains:
        return {**res, "verified_by": "crossref", "reason": "domain also decoded from a QR code"}
    if valid_syntax and reg(host) in allowed:
        return {**res, "verified_by": "allowed", "reason": f"{reg(host)} is on your allowlist"}
    raw_rank = dom.rank(reg(host)) if valid_syntax else None
    if raw_rank is not None and raw_rank <= POPULAR:
        return {**res, "verified_by": "known", "reason": f"{reg(host)} Tranco rank {raw_rank}"}

    scored = []                                   # (rank, edits, host, why): best rank, then fewest edits
    for c, edits in candidates(raw_host).items():
        if dom.available and not dom.valid_tld(c):
            continue
        r = reg(c)
        if r in qr_domains:
            scored.append((0, edits, c, f"{r} decoded from a QR code"))
        elif r in allowed:
            scored.append((0, edits, c, f"{r} is on your allowlist"))
        else:
            rk = dom.rank(r)
            if rk is not None:
                scored.append((rk, edits, c, f"{r} Tranco rank {rk}"))
    scored.sort()
    scored = [(rk, c, why) for rk, _, c, why in scored]
    if scored:
        best_rank, best, why = scored[0]
        unique = len(scored) == 1 or scored[1][0] >= max(best_rank, 1) * 10 or \
            reg(scored[1][1]) == reg(best)
        beats_raw = (not valid_syntax) or raw_rank is None or raw_rank >= max(best_rank, 1) * MARGIN
        if unique and beats_raw:
            vb = ("allowed" if "allowlist" in why else "crossref") if best_rank == 0 else "known"
            raw_part = "not a valid host" if not valid_syntax else (f"rank {raw_rank}" if raw_rank else "unranked")
            return {**res, "host": best, "verified_by": vb, "corrected": True,
                    "reason": f"{why}; raw reading {raw_part}"}
        if not unique:
            res["reason"] = "ambiguous: " + ", ".join(c for _, c, _ in scored[:3])
    if raw_rank is not None and not res["reason"]:
        return {**res, "verified_by": "known", "reason": f"{reg(host)} Tranco rank {raw_rank}, no likelier lookalike"}
    if not valid_syntax:
        res["flag"] = "invalid_host"
    elif dom.available and not dom.valid_tld(host):
        res["flag"] = "invalid_tld"                        # usually cut off on screen: www.ome, register.gotow
    return res


def fix_urls(ex, dom: DomainIndex, qr_domains: set[str], allowed: set[str] = frozenset()) -> None:
    """Resolve every OCR-read URL of one extraction in place. QR-decoded URLs are untouched."""
    from urllib.parse import urlsplit
    from .extract import raw_host
    seen = {u["url"] for u in ex.urls if u["verified_by"] == "qr"}
    kept = []
    for u in ex.urls:
        if u["verified_by"] == "qr":
            kept.append(u)
            continue
        r = resolve(raw_host(u["raw"]), dom, qr_domains, allowed)
        parts = urlsplit(u["url"])
        url = f"{parts.scheme}://{r['host']}{parts.path}" + (f"?{parts.query}" if parts.query else "")
        if url in seen:
            continue                                        # the correction duplicates a QR/earlier URL
        seen.add(url)
        u.update(url=url, verified_by=r["verified_by"], corrected=r["corrected"], reason=r["reason"])
        if r["flag"]:
            u["flag"] = r["flag"]
        if u.get("truncated"):
            u["flag"] = "truncated"
        kept.append(u)
    ex.urls = kept


def qr_domains_of(urls, dom: DomainIndex) -> set[str]:
    out = set()
    for url in urls:
        host = url.split("://", 1)[-1].split("/", 1)[0]
        r = dom.registrable(host) if dom.available else ".".join(host.split(".")[-2:])
        if r:
            out.add(r)
    return out


def load_allowed(folder) -> set[str]:
    f = folder / "allow.txt"
    if not f.exists():
        return set()
    return {ln.strip().lower() for ln in f.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def raw_host_of(raw: str) -> str:
    from .extract import raw_host
    return raw_host(raw)
