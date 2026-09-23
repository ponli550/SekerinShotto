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
              "ingested", "status", "status_reason", "category", "decided_by", "urls", "urls_unverified", "domains",
              "qr", "tags"]
WRITEBACK_KEYS = ("category", "decided_by")      # kept when a caller (llm/user/laya) decided them
_SKIP_PKG = {"com", "org", "net", "my", "io", "co", "app", "android"}


class NoteConflict(Exception):
    """The note exists but its generated markers are gone: a human rewrote it. Never overwrite."""


def app_slug(pkg: str | None) -> str:
    if not pkg:
        return "image"
    toks = [t for t in pkg.split(".") if t not in _SKIP_PKG]
    return (toks[0] if toks else pkg.split(".")[-1])[:24]


def note_relpath(ex: Extraction, category: str = "uncategorized") -> str:
    date = (ex.captured_at or "undated")[:10]
    return f"notes/{category}/{date}-{app_slug(ex.source_app)}-{ex.id.split(':')[1][:8]}.md"


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


def generated_body(ex: Extraction) -> str:
    out = [START, "## Text", _fence(ex.text) if ex.text.strip() else "_no text read_"]
    if ex.urls:
        out.append("## Links")
        for u in ex.urls:
            label = u["url"].split("://", 1)[1]
            if u["verified_by"] == "qr":
                out.append(f"- [{label}]({u['url']}) · from QR")
            else:
                # OCR misreads produce lookalike domains (docs.qoogle.com); never make them clickable.
                out.append(f"- `{u['raw']}` · read by OCR, unverified — not a link")
    if ex.barcodes:
        out.append("## QR")
        for b in ex.barcodes:
            extra = ", ".join(f"{k.replace('_', ' ')}: {b[k]}" for k in ("merchant_name", "merchant_city", "amount") if k in b)
            out.append(f"- **{b['type']}** ({b['symbology']}){' · ' + extra if extra else ''}")
            out.append("  " + _fence(b["payload"]).replace("\n", "\n  "))
    out.append("## Source")
    src = [f"- file: `{ex.path.name}`"]
    if ex.source_app:
        src.append(f"- app: `{ex.source_app}`")
    if ex.captured_at:
        src.append(f"- captured: {ex.captured_at}")
    src.append(f"- size: {ex.width}×{ex.height}, {ex.bytes // 1024} KB")
    if ex.status != "ok":
        src.append(f"- **status: {ex.status}** ({ex.status_reason})")
    out.extend(src)
    out.append(END)
    return "\n\n".join(out)


def render(ex: Extraction, ingested: str, existing: str | None = None) -> str:
    fm = {
        "id": ex.id, "ingester": "sekerinshotto", "ingester_version": __version__,
        "source_type": "image", "source_app": ex.source_app, "captured_at": ex.captured_at,
        "ingested": ingested, "status": ex.status, "status_reason": ex.status_reason,
        "category": "uncategorized", "decided_by": None,
        "urls": [u["url"] for u in ex.urls if u["verified_by"] == "qr"],
        # no scheme, so Obsidian's Properties panel does not turn a misread into a link
        "urls_unverified": [u["url"].split("://", 1)[1] for u in ex.urls if u["verified_by"] != "qr"],
        "domains": ex.domains,
        "qr": sorted({b["type"] for b in ex.barcodes}),
        "tags": ["sekerinshotto"] + ([f"sekerinshotto/{ex.status}"] if ex.status != "ok" else []),
    }
    foreign: list[str] = []
    user_part = USER_TAIL
    if existing is not None:
        blocks, body = _split(existing)
        if START not in body or END not in body:
            raise NoteConflict("generated markers missing")
        current = {k: _block_value(raw) for k, raw in blocks}
        if current.get("decided_by") in ("llm", "user", "laya"):
            for k in WRITEBACK_KEYS:
                fm[k] = current.get(k)
        foreign = [raw for k, raw in blocks if k not in OWNED_KEYS]
        user_part = body.split(END, 1)[1]
    lines = [f"{k}: {_y(v)}" for k, v in fm.items() if v is not None and v != []]
    head = "---\n" + "\n".join(lines + foreign) + "\n---\n\n"
    return head + generated_body(ex) + user_part


def text_from_note(text: str) -> str:
    """Recover the OCR text block from a note (used by reindex)."""
    m = re.search(r"## Text\n\n(```|~~~~)text\n(.*?)\n\1", text, re.S)
    return m.group(2) if m else ""


def manifest_record(ex: Extraction, batch_id: str, note_path: str, source_state: str = "present") -> dict:
    return {
        "format": "shared-note/0.1", "id": ex.id, "ingester": "sekerinshotto",
        "ingester_version": __version__, "batch_id": batch_id, "source_type": "image",
        "source_path": str(ex.path), "source_state": source_state, "note_path": note_path,
        "source_app": ex.source_app, "captured_at": ex.captured_at,
        "category": "uncategorized", "decided_by": None,
        "status": ex.status, "status_reason": ex.status_reason,
        "entities": {"qr": ex.barcodes, "urls": ex.urls, "domains": ex.domains},
        "text_chars": len(ex.text), "ocr_confidence": ex.ocr_confidence,
        "width": ex.width, "height": ex.height, "bytes": ex.bytes,
    }
