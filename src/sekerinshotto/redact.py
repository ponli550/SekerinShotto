"""PII redaction at the LLM boundary (FORMAT §7a). Ported from VeriPay backend.redact().

Notes, manifests and the index keep full text: the vault is the user's own memory.
Everything a command returns toward a calling LLM goes through here first.

Name detection is layered and admits its gap: a bare name with no title, cue or
patronymic (e.g. "LIM CHEE KEONG" alone on a line) is NOT caught.
"""
from __future__ import annotations

import os
import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
MY_IC_RE = re.compile(r"\b\d{6}-\d{2}-\d{4}\b")
# VeriPay's pattern plus a separator after the country code ("+60 12-256 7486"), common on WhatsApp.
# Full numbers, and numbers cut off on screen ("+6017-483 56..."): a partial number still identifies someone.
PHONE_RE = re.compile(r"(?<!\d)(?:\+?60[-\s]?|0)1\d[-\s]?\d{3,4}(?:[-\s]?\d{1,4})?(?:\.{2,}|…)?(?!\d)")

# Addresses and locations. Conservative-but-broad: over-redacting an excerpt costs little (the vault keeps
# the full text); leaking a home address does not.
ADDR_CUE_RE = re.compile(r"\b(Alamat|Address|Addr)\s*[:\-]\s*\S.*$", re.IGNORECASE)
STREET_RE = re.compile(
    r"\b(?:No\.?\s*\d+[A-Za-z]?,?\s*)?(?:Jalan|Jln|Lorong|Lrg|Persiaran|Taman|Tmn|Kampung|Kg|Blok|Block|Lebuh|Presint)"
    r"\.?\s+(?:[A-Z0-9][\w/'.-]*)(?:[ ,]+[A-Z0-9][\w/'.-]*)*")
POSTCODE_RE = re.compile(r"\b\d{5},?\s+[A-Z][A-Za-z]+(?:[ ,]+[A-Z][A-Za-z]+)*")
GPS_RE = re.compile(r"\b(?:LAT|LNG|LON|LONG|Latitude|Longitude)\s*[:=]\s*-?\d{1,3}\.\d{3,}"
                    r"|(?<![\d.])-?\d{1,2}\.\d{4,},\s*-?\d{1,3}\.\d{4,}(?![\d.])", re.IGNORECASE)

MY_STATES = ("Johor|Kedah|Kelantan|Melaka|Malacca|Negeri Sembilan|Pahang|Perak|Perlis|Pulau Pinang|Penang|Sabah|"
             "Sarawak|Selangor|Terengganu|Kuala Lumpur|Putrajaya|Labuan|Wilayah Persekutuan")
ADDR_CONT_RE = re.compile(rf"\b\d{{5}}\b|\b(?:{MY_STATES})\b")
ADDR_STOP_RE = re.compile(r"(?i)^\s*(nama|name|tel|phone|no\.?\s*tel|email|total|jumlah)\b|\d{1,2}:\d{2}")
ADDR_MAX_CONT = 3

KNOWN_NAMES = [n.strip() for n in os.environ.get("SEKERINSHOTTO_KNOWN_NAMES", "").split(",") if n.strip()]
_NAME_WORD = r"[A-Z][\w'.@-]*"
HONORIFIC_RE = re.compile(
    r"\b(?:Mr|Ms|Mrs|Dr|Ir|Tun|Tan\s+Sri|Puan\s+Sri|Toh\s+Puan|Datuk\s+Seri|"
    r"Dato'?\s+Sri|Datuk|Dato'?|Datin|Encik|Puan|Cik|Tuan|Haji|Hajah|Prof)\.?\s+"
    rf"{_NAME_WORD}(?:\s+(?:{_NAME_WORD}|bin|binti|a/l|a/p)){{0,4}}")
CUE_RE = re.compile(
    r"\b((?:Prepared|Reviewed|Approved|Signed|Certified|Audited)\s+by[:\s]+)"
    rf"({_NAME_WORD}(?:\s+(?:{_NAME_WORD}|bin|binti|a/l|a/p)){{0,4}})")
# Capitalised (or ALL-CAPS) words around bin/binti/a/l/a/p, at most 4 on each side. VeriPay's version
# was case-insensitive and unbounded, which on a long OCR line swallows the rest of the sentence.
# Cost: an all-lowercase name in a chat ("ahmad bin ali") is not caught.
PATRONYMIC_RE = re.compile(
    r"\b[A-Z][\w'@-]*(?:\s+[A-Z][\w'@-]*){0,3}\s+(?i:bin|binti|a/l|a/p)\s+"
    r"[A-Z][\w'@-]*(?:\s+[A-Z][\w'@-]*){0,3}\b")


# Bare names with no cue, anchored on a common Malaysian given/honorific first name. Narrows (does not close)
# the bare-name gap: "Mohamad Zarif Iman" in a call roster. Up to 3 more capitalised words.
GIVEN = ("Muhammad|Muhamad|Mohammad|Mohamad|Mohammed|Mohd|Muhd|Ahmad|Abdul|Abd|Nur|Nurul|Nor|Noor|Siti|"
         "Wan|Nik|Tengku|Tunku|Syed|Sharifah|Puteri|Megat|Raja|Nurin|Nurfarah|Aisyah|Aishah|Fatimah|Khairul|"
         "Hafiz|Hafizah|Amirul|Aiman|Izzat|Syafiq|Farah|Nabila|Aina|Balqis")
GIVEN_RE = re.compile(rf"\b(?:{GIVEN})\b(?:\s+[A-Z][\w'.-]*){{1,3}}")


def _valid_ic_date(m: re.Match) -> bool:
    """First six NRIC digits must be a plausible YYMMDD, so reference numbers shaped like
    123456-78-9012 are not redacted."""
    d = m.group(0)
    return 1 <= int(d[2:4]) <= 12 and 1 <= int(d[4:6]) <= 31


# Profile author lines (LinkedIn, Instagram, Teams): "Norhasliza Yusof" alone on a line, followed within two
# lines by a connection marker or a job title. The name has no cue of its own; the context gives it away.
NAME_LINE_RE = re.compile(r"^\s*(?:[A-Z][a-z'.-]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z'.-]+|[A-Z]{2,}|bin|binti|a/[lp])){1,3}\s*[•·]?\s*$")
PROFILE_NEXT_RE = re.compile(
    r"^\s*[•·]?\s*(?:1st|2nd|3rd\+?)\b|\b(?:Lecturer|Professor|Engineer|Manager|Director|Student|Intern|Developer|"
    r"Analyst|Officer|Executive|Founder|CEO|CTO|COO|Specialist|Consultant|Researcher|Scientist|Designer|Head of|"
    r"Pensyarah|Pengurus|Pelajar)\b|\s@\s|^\s*(?:Follow|Connect|Author)\s*$", re.IGNORECASE)


NOT_A_NAME_RE = re.compile(
    r"(?i)\b(program|programme|express|stack|services?|sdn|bhd|team|group|university|universiti|academy|club|"
    r"centre|center|department|jabatan|workshop|company|studio|labs?|school|college|kolej|official|news|"
    r"project|system|systems|solutions|digital|media|global|network|community|society|foundation)\b")


def _profile_names(lines: list[str]) -> set[int]:
    """Indexes of lines that are a person's name by context: next non-empty lines carry a profile marker."""
    hits = set()
    for i, line in enumerate(lines):
        if not NAME_LINE_RE.match(line) or len(line.split()) > 5 or NOT_A_NAME_RE.search(line):
            continue
        nxt = [l for l in lines[i + 1:i + 4] if l.strip() and l.strip() not in ("•", "·")][:1]
        if nxt and PROFILE_NEXT_RE.search(nxt[0]):          # the very next meaningful line, not two ahead
            hits.add(i)
    return hits


def redact(text: str) -> tuple[str, int]:
    """(clean_text, items_redacted). Line structure is preserved."""
    if not text:
        return text, 0
    out, count = [], 0
    cont_left, prev_comma = 0, False
    raw_lines = text.split("\n")
    names = _profile_names(raw_lines)
    for idx, line in enumerate(raw_lines):
        red, n = _redact_line(line)
        if idx in names and "[NAME]" not in red:
            red, n = "[NAME]", n + 1
        if "[ADDRESS]" in red:
            cont_left, prev_comma = ADDR_MAX_CONT, red.rstrip().endswith(",")
        elif cont_left and line.strip() and not ADDR_STOP_RE.search(line) and (
                ADDR_CONT_RE.search(line) or prev_comma):
            # an address that wraps onto the next lines ("3/4, Bandar Bertam Putra, 13200" / "Kepala Batas, ...")
            red, n, cont_left = "[ADDRESS]", n + 1, cont_left - 1
            prev_comma = line.rstrip().endswith(",")
        else:
            cont_left = 0
        out.append(red)
        count += n
    return "\n".join(out), count


def _redact_line(text: str) -> tuple[str, int]:
    count = 0
    for name in KNOWN_NAMES:
        text, n = re.subn(re.escape(name), "[NAME]", text, flags=re.IGNORECASE)
        count += n
    text, n = CUE_RE.subn(lambda m: m.group(1) + "[NAME]", text)
    count += n
    text, n = HONORIFIC_RE.subn("[NAME]", text)
    count += n
    text, n = PATRONYMIC_RE.subn("[NAME]", text)
    count += n
    text, n = GIVEN_RE.subn("[NAME]", text)
    count += n
    hits = 0

    def _ic(m):
        nonlocal hits
        if _valid_ic_date(m):
            hits += 1
            return "[IC]"
        return m.group(0)
    text = MY_IC_RE.sub(_ic, text)
    count += hits
    for pattern, token in ((EMAIL_RE, "[EMAIL]"), (PHONE_RE, "[PHONE]"), (GPS_RE, "[LOCATION]")):
        text, n = pattern.subn(token, text)
        count += n
    text, n = ADDR_CUE_RE.subn(lambda m: m.group(1) + ": [ADDRESS]", text)
    count += n
    for pattern in (STREET_RE, POSTCODE_RE):
        text, n = pattern.subn("[ADDRESS]", text)
        count += n
    return text, count


def redact_qr(qr: dict) -> dict:
    """A QR entry safe to hand out. Payment payloads carry names and account ids: replaced whole."""
    if qr.get("type") == "payment":
        city = qr.get("merchant_city")
        return {"type": "payment", "symbology": qr.get("symbology"),
                "payload": "[PAYMENT QR]", "merchant_name": "[NAME]" if qr.get("merchant_name") else None,
                **({"merchant_city": city} if city else {}), **({"amount": qr["amount"]} if qr.get("amount") else {})}
    if qr.get("type") in ("contact", "tel", "smsto", "mailto"):
        return {**qr, "payload": redact(qr.get("payload", ""))[0]}
    return qr
