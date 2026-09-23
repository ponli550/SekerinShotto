"""Note files and manifest records (FORMAT.md §2–§3)."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import __version__
from .extract import Extraction

START = "<!-- generated:start — owned by sekerinshotto, rewritten on re-run -->"
END = "<!-- generated:end -->"
USER_TAIL = "\n\n## Notes\n\n"

OWNED_KEYS = ["id", "ingester", "ingester_version", "source_type", "source_app", "captured_at",
              "ingested", "status", "status_reason", "category", "decided_by", "urls", "urls_unverified", "urls_corrected", "domains",
              "qr", "group", "rank", "group_size", "members", "source_state", "purge_after", "decided_evidence", "terms",
              "tags"]
WRITEBACK_KEYS = ("category", "decided_by", "decided_evidence")   # kept when a caller decided them
WRITEBACK_BY = ("llm", "user", "laya")
_SKIP_PKG = {"com", "org", "net", "my", "io", "co", "app", "android"}


class NoteConflict(Exception):
    """The note exists but its generated markers are gone: a human rewrote it. Never overwrite."""


def app_slug(pkg: str | None) -> str:
    if not pkg:
        return "image"
    toks = [t for t in pkg.split(".") if t not in _SKIP_PKG]
    return (toks[0] if toks else pkg.split(".")[-1])[:24]


def note_filename(ex: Extraction) -> str:
    date = (ex.captured_at or "undated")[:10]
    return f"{date}-{app_slug(ex.source_app)}-{ex.id.split(':')[1][:8]}.md"


def note_relpath(ex: Extraction, category: str = "uncategorized") -> str:
    return f"notes/{category}/{note_filename(ex)}"


def set_frontmatter(text: str, updates: dict) -> str:
    """Rewrite (or add) top-level frontmatter keys, leaving every other block untouched."""
    blocks, body = _split(text)
    seen, out = set(), []
    for k, raw in blocks:
        if k in updates:
            out.append(f"{k}: {_y(updates[k])}")
            seen.add(k)
        else:
            out.append(raw)
    out += [f"{k}: {_y(v)}" for k, v in updates.items() if k not in seen]
    return "---\n" + "\n".join(out) + "\n---\n" + body


def read_frontmatter(text: str) -> dict:
    blocks, _ = _split(text)
    return {k: _block_value(raw) for k, raw in blocks}


def _y(v) -> str:
    return json.dumps(v, ensure_ascii=False)          # JSON scalars/arrays are valid YAML


def _split(text: str) -> tuple[list[tuple[str, str]], str]:
    """-> ([(key, raw_block)], body). Blocks keep their exact text so foreign keys survive untouched."""
    if not text.startswith("---\n"):
        return [], text
    end = text.find("\n---\n", 4)
    if end == -1:
        return [], text
    fm, body = text[4:end], text[end + 5:]
    blocks: list[tuple[str, str]] = []
    for line in fm.splitlines():
        m = re.match(r"^([A-Za-z0-9_-]+):", line)
        if m:
            blocks.append((m.group(1), line))
        elif blocks:
            k, raw = blocks[-1]
            blocks[-1] = (k, raw + "\n" + line)
    return blocks, body


def _block_value(raw: str):
    val = raw.split(":", 1)[1].strip()
    try:
        return json.loads(val)
    except (json.JSONDecodeError, ValueError):
        return val.strip("'\"")


def _fence(text: str) -> str:
    f = "~~~~" if "```" in text else "```"
    return f"{f}text\n{text}\n{f}"


def _linkable(u: dict) -> bool:
    return u["verified_by"] in ("qr", "known", "crossref", "allowed") and not u.get("flag")


def generated_body(ex: Extraction, org: dict | None = None) -> str:
    out = [START]
    if ex.attachment:
        out.append(f"![[{ex.attachment}]]")                  # visual: the image is the content
    out += ["## Text", _fence(ex.text) if ex.text.strip() else "_no text read_"]
    if ex.urls:
        out.append("## Links")
        for u in ex.urls:
            label = u["url"].split("://", 1)[1]
            vb, flag = u["verified_by"], u.get("flag")
            if vb == "qr":
                out.append(f"- [{label}]({u['url']}) · from QR")
            elif flag == "truncated":
                out.append(f"- `{u['raw']}…` · cut off on screen — not a link")
            elif flag in ("invalid_tld", "invalid_host"):
                out.append(f"- `{u['raw']}` · not a valid address (likely cut off) — not a link")
            elif vb in ("known", "crossref", "allowed") and u.get("corrected"):
                # corrected to a well-ranked domain; the raw reading stays visible
                out.append(f"- [{label}]({u['url']}) · corrected, read as `{u['raw']}` ({u['reason']})")
            elif vb in ("known", "crossref", "allowed"):
                out.append(f"- [{label}]({u['url']}) · read by OCR, domain {vb} ({u['reason']})")
            else:
                # OCR misreads produce lookalike domains (docs.qoogle.com); never make them clickable.
                out.append(f"- `{u['raw']}` · read by OCR, unverified — not a link")
            if u.get("joined"):
                out[-1] += f" · joined across {u['joined'] + 1} lines"
    if ex.barcodes:
        out.append("## QR")
        for b in ex.barcodes:
            extra = ", ".join(f"{k.replace('_', ' ')}: {b[k]}" for k in ("merchant_name", "merchant_city", "amount") if k in b)
            out.append(f"- **{b['type']}** ({b['symbology']}){' · ' + extra if extra else ''}")
            out.append("  " + _fence(b["payload"]).replace("\n", "\n  "))
    if org and org.get("group"):
        out.append("## Group")
        out.append(f"[[{org['group']}]] · rank {org['rank']} of {org['size']} ({org.get('score_why', '')})")
    out.append("## Source")
    src = [f"- category: **{org['category']}** — {org.get('why') or ''}"] if org else []
    src.append(f"- file: `{ex.path.name}`")
    if ex.source_app:
        src.append(f"- app: `{ex.source_app}`")
    if ex.captured_at:
        src.append(f"- captured: {ex.captured_at}")
    src.append(f"- size: {ex.width}×{ex.height}, {ex.bytes // 1024} KB")
    if ex.status != "ok":
        src.append(f"- **status: {ex.status}** ({ex.status_reason})")
    src.append("- image: " + {
        "present": "at its source, not yet cleaned up",
        "quarantined": f"quarantined, purged after {ex.purge_after} UTC",
        "attached": "kept in the vault (visual or diagram)",
        "held": f"held — {ex.hold_reason}; retried automatically, never auto-deleted",
        "purged": f"purged at {ex.purged_at}; this note is the only record",
    }.get(ex.source_state, ex.source_state))
    if ex.copies:
        states = {}
        for c in ex.copies:
            states[c.get("state", "present")] = states.get(c.get("state", "present"), 0) + 1
        src.append("- identical copies: " + ", ".join(f"{n} {st}" for st, n in sorted(states.items())))
    out.append("\n".join(src))
    out.append(END)
    return "\n\n".join(out)


def render(ex: Extraction, ingested: str, existing: str | None = None, org: dict | None = None) -> str:
    org = org or {"category": "uncategorized", "decided_by": None}
    fm = {
        "id": ex.id, "ingester": "sekerinshotto", "ingester_version": __version__,
        "source_type": "image", "source_app": ex.source_app, "captured_at": ex.captured_at,
        "ingested": ingested, "status": ex.status, "status_reason": ex.status_reason,
        "category": org["category"], "decided_by": org.get("decided_by"),
        "group": org.get("group"), "rank": org.get("rank"), "group_size": org.get("size"),
        "terms": org.get("terms") or [],
        "urls": [u["url"] for u in ex.urls if _linkable(u)],
        # no scheme, so Obsidian's Properties panel does not turn a misread into a link
        "urls_unverified": [u["url"].split("://", 1)[1] for u in ex.urls if not _linkable(u)],
        "urls_corrected": [f"{u['raw']} -> {u['url']}" for u in ex.urls if u.get("corrected")],
        "domains": ex.domains,
        "qr": sorted({b["type"] for b in ex.barcodes}),
        "source_state": ex.source_state, "purge_after": ex.purge_after,
        "tags": ["sekerinshotto"] + ([f"sekerinshotto/{ex.status}"] if ex.status != "ok" else [])
                + ([f"sekerinshotto/{ex.source_state}"] if ex.source_state in ("held", "attached") else []),
    }
    foreign: list[str] = []
    user_part = USER_TAIL
    if existing is not None:
        blocks, body = _split(existing)
        if START not in body or END not in body:
            raise NoteConflict("generated markers missing")
        current = {k: _block_value(raw) for k, raw in blocks}
        if current.get("decided_by") in WRITEBACK_BY:
            for k in WRITEBACK_KEYS:
                fm[k] = current.get(k)
        foreign = [raw for k, raw in blocks if k not in OWNED_KEYS]
        user_part = body.split(END, 1)[1]
    lines = [f"{k}: {_y(v)}" for k, v in fm.items() if v is not None and v != []]
    head = "---\n" + "\n".join(lines + foreign) + "\n---\n\n"
    return head + generated_body(ex, org) + user_part


def text_from_note(text: str) -> str:
    """Recover the OCR text block from a note (used by reindex)."""
    m = re.search(r"## Text\n\n(```|~~~~)text\n(.*?)\n\1", text, re.S)
    return m.group(2) if m else ""


def manifest_record(ex: Extraction, batch_id: str, note_path: str, source_state: str | None = None,
                    org: dict | None = None) -> dict:
    org = org or {"category": "uncategorized", "decided_by": None}
    return {
        "format": "shared-note/0.1", "id": ex.id, "ingester": "sekerinshotto",
        "ingester_version": __version__, "batch_id": batch_id, "source_type": "image",
        "source_path": str(ex.path), "source_state": source_state or ex.source_state, "note_path": note_path,
        "source_app": ex.source_app, "captured_at": ex.captured_at,
        "category": org["category"], "decided_by": org.get("decided_by"), "why": org.get("why"),
        "group": org.get("group"), "rank": org.get("rank"), "group_size": org.get("size"),
        "terms": org.get("terms") or [],
        "status": ex.status, "status_reason": ex.status_reason,
        "entities": {"qr": ex.barcodes, "urls": ex.urls, "domains": ex.domains},
        "text_chars": len(ex.text), "ocr_confidence": ex.ocr_confidence,
        "width": ex.width, "height": ex.height, "bytes": ex.bytes,
        "sig": ex.sig, "toks": ex.toks, "dhash": ex.dhash, "content_tokens": ex.content_tokens,
        "extractor_version": ex.extractor_version,
        "text_coverage": ex.text_coverage, "grays": ex.grays, "edges": ex.edges,
        "hlines": ex.hlines, "vlines": ex.vlines, "saturation": ex.saturation,
        **{k: getattr(ex, k) for k in LIFECYCLE},
    }


LIFECYCLE = ("stored_path", "quarantined_at", "purge_after", "purged_at", "attachment", "hold_reason",
             "attempts", "keep", "confirmed_by", "copies")


def ex_version() -> str:
    from .extract import EXTRACTOR_VERSION
    return EXTRACTOR_VERSION


def extraction_from_record(rec: dict, text: str) -> Extraction:
    """Rebuild what a note needs from a stored record, without the image (it may be purged)."""
    ex = Extraction(id=rec["id"], path=Path(rec["source_path"]), width=rec.get("width") or 0,
                    height=rec.get("height") or 0, bytes=rec.get("bytes") or 0,
                    captured_at=rec.get("captured_at"), source_app=rec.get("source_app"))
    ex.lines = [(t, 1.0) for t in text.splitlines()]
    ex.barcodes = rec["entities"]["qr"]
    ex.urls = rec["entities"]["urls"]
    ex.status, ex.status_reason = rec["status"], rec.get("status_reason")
    ex.ocr_confidence = rec.get("ocr_confidence")
    ex.sig, ex.dhash, ex.content_tokens = rec.get("sig") or [], rec.get("dhash"), rec.get("content_tokens") or 0
    ex.toks = rec.get("toks") or []
    ex.text_coverage, ex.grays, ex.edges = rec.get("text_coverage") or 0.0, rec.get("grays") or 0, rec.get("edges") or 0.0
    ex.hlines, ex.vlines, ex.saturation = rec.get("hlines") or 0, rec.get("vlines") or 0, rec.get("saturation") or 0.0
    ex.source_state = rec.get("source_state") or "present"
    for k in LIFECYCLE:
        if rec.get(k) is not None:
            setattr(ex, k, rec[k])
    ex.extractor_version = rec.get("extractor_version") or ex_version()
    return ex


def render_group(gid: str, members: list[dict], existing: str | None = None) -> str:
    """Hub note for one duplicate group. members: [{stem, rank, score_why, why_grouped}] in rank order."""
    fm = {"id": gid, "ingester": "sekerinshotto", "ingester_version": __version__,
          "source_type": "group", "members": len(members), "tags": ["sekerinshotto", "sekerinshotto/group"]}
    body = [START, f"## Duplicate group · {len(members)} screenshots",
            "Ranked by information content; rank 1 is the most complete copy.", ""]
    body += [f"{m['rank']}. [[{m['stem']}]] · {m['score_why']}" for m in members]
    body.append(END)
    user_part, foreign = USER_TAIL, []
    if existing is not None:
        blocks, old_body = _split(existing)
        if START not in old_body or END not in old_body:
            raise NoteConflict("generated markers missing")
        foreign = [raw for k, raw in blocks if k not in OWNED_KEYS]
        user_part = old_body.split(END, 1)[1]
    lines = [f"{k}: {_y(v)}" for k, v in fm.items()]
    return "---\n" + "\n".join(lines + foreign) + "\n---\n\n" + "\n".join(body) + user_part


def user_part_is_empty(text: str) -> bool:
    _, body = _split(text)
    return END in body and body.split(END, 1)[1].strip() in ("", "## Notes")
