"""Image lifecycle after extraction (FORMAT §6): quarantine, attachments, held, purge, restore.

Nothing here runs without --commit. The only deletion is purge, and it only ever
deletes a file whose resolved path is inside <state>/quarantine/.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .contract import ToolError
from .state import now_iso, parse_iso, plus_seconds

QUARANTINE_SECONDS = 7 * 24 * 60 * 60          # exactly 604800 s after quarantined_at
VISUAL_MAX_COVERAGE, VISUAL_MAX_CHARS = 0.08, 300
VISUAL_MIN_GRAYS, VISUAL_MIN_EDGES = 180, 0.03
NOT_VISUAL_CATEGORIES = {"system"}             # home screens: icons, not knowledge
DIAGRAM_MIN_HLINES, DIAGRAM_MIN_VLINES = 14, 8
DIAGRAM_MAX_SATURATION = 0.30                  # colourful posters are above; tables and documents below
NOT_DIAGRAM_CATEGORIES = {"system", "game"}    # home screens and game HUDs are full of straight lines


def is_visual(rec: dict) -> bool:
    """Little text, rich pixels, and no decoded QR explaining the picture. Measured on the sample:
    catches photos, video frames and camera feeds; text-heavy diagrams are NOT caught (use `keep`)."""
    return (rec.get("text_coverage", 1.0) < VISUAL_MAX_COVERAGE and (rec.get("text_chars") or 0) < VISUAL_MAX_CHARS
            and (rec.get("grays") or 0) >= VISUAL_MIN_GRAYS and (rec.get("edges") or 0) >= VISUAL_MIN_EDGES
            and not rec["entities"]["qr"] and rec.get("category") not in NOT_VISUAL_CATEGORIES)


def is_diagram(rec: dict) -> bool:
    """Tables, timetables, formulas, slides: layout carries meaning OCR flattens. Measured on the sample:
    16 of 17 selected were real diagrams (the miss: a delivery app price list); a colourful infographic
    and posters are not caught (posters' text is captured anyway)."""
    return ((rec.get("hlines") or 0) >= DIAGRAM_MIN_HLINES or (rec.get("vlines") or 0) >= DIAGRAM_MIN_VLINES) \
        and not rec["entities"]["qr"] and (rec.get("saturation") or 0.0) <= DIAGRAM_MAX_SATURATION \
        and rec.get("category") not in NOT_DIAGRAM_CATEGORIES


def decide(rec: dict) -> tuple[str, str]:
    """-> (outcome, reason); outcome is quarantine | attach | hold."""
    if rec.get("keep"):
        return "attach", "kept on request"
    if rec["status"] != "ok" and not rec.get("confirmed_by"):
        return "hold", rec.get("status_reason") or "failed"
    unverified = [u["raw"] for u in rec["entities"]["urls"] if u["verified_by"] == "none" and not u.get("flag")]
    if unverified and not rec.get("confirmed_by"):
        return "hold", "unverified URL: " + ", ".join(unverified[:3])
    if is_visual(rec):
        return "attach", f"visual: text covers {rec.get('text_coverage', 0):.0%} of the screen"
    if is_diagram(rec):
        return "attach", f"diagram: {rec.get('hlines')} horizontal / {rec.get('vlines')} vertical lines"
    return "quarantine", "content captured in the note"


def current_file(rec: dict) -> Path | None:
    p = Path(rec["stored_path"]) if rec.get("stored_path") else Path(rec["source_path"])
    return p if p.exists() else None


def _dest_name(rec: dict) -> str:
    return f"{rec['id'].split(':')[1][:8]}-{Path(rec['source_path']).name}"


def _move(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise ToolError(f"refusing to overwrite {dst}")
    shutil.move(str(src), str(dst))


def route(rec: dict, outcome: str, reason: str, state_root: Path, content: Path, batch_id: str) -> dict:
    """Move one image and return the lifecycle fields to store. Raises ToolError on a conflict."""
    src = current_file(rec)
    if src is None:
        raise ToolError(f"image for {rec['id'][:15]} is missing: {rec.get('stored_path') or rec['source_path']}")
    now = now_iso()
    if outcome == "quarantine":
        dst = state_root / "quarantine" / batch_id / _dest_name(rec)
        _move(src, dst)
        return {"source_state": "quarantined", "stored_path": str(dst), "quarantined_at": now,
                "purge_after": plus_seconds(now, QUARANTINE_SECONDS), "hold_reason": None, "attachment": None}
    if outcome == "attach":
        rel = f"attachments/{_dest_name(rec)}"
        _move(src, content / rel)
        return {"source_state": "attached", "stored_path": str(content / rel), "attachment": rel,
                "hold_reason": None, "quarantined_at": None, "purge_after": None}
    dst = state_root / "held" / _dest_name(rec)
    if src.resolve() != dst.resolve():
        _move(src, dst)
    return {"source_state": "held", "stored_path": str(dst), "hold_reason": reason,
            "quarantined_at": None, "purge_after": None}


def purge_file(path: Path, state_root: Path) -> bool:
    """Delete one quarantined file, but ONLY if it really lives inside <state>/quarantine/
    (symlinks resolved). Anything else is refused — this must never be able to delete a
    file the user did not hand to quarantine."""
    root = (state_root / "quarantine").resolve()
    real = path.resolve()
    if not (str(real).startswith(str(root) + os.sep) and real.is_file()):
        return False
    real.unlink()
    parent = real.parent
    if parent != root and not any(parent.iterdir()):
        parent.rmdir()
    return True


def is_due(rec: dict, now: str) -> bool:
    return bool(rec.get("purge_after")) and parse_iso(now) >= parse_iso(rec["purge_after"])


def seconds_left(rec: dict, now: str) -> int:
    return int((parse_iso(rec["purge_after"]) - parse_iso(now)).total_seconds())


def render_audit(items: list[dict], now: str) -> str:
    """AUDIT.md: what the user reads instead of reviewing images."""
    held = [r for r in items if r.get("source_state") == "held"]
    attached = [r for r in items if r.get("source_state") == "attached"]
    quarantined = [r for r in items if r.get("source_state") == "quarantined"]
    purged = [r for r in items if r.get("source_state") == "purged"]
    redacted = [r for r in items if any(b["type"] == "wifi" for b in r["entities"]["qr"])]
    counts = {}
    for r in held:
        key = (r.get("hold_reason") or "").split(":")[0]
        counts[key] = counts.get(key, 0) + 1
    stem = lambda r: Path(r["note_path"]).stem if r.get("note_path") else r["id"][:15]
    out = ["---", 'source_type: "audit"', f'generated: "{now}"', 'tags: ["sekerinshotto", "sekerinshotto/audit"]',
           "---", "", "# SekerinShotto audit", "",
           f"Generated {now}. Regenerated on every ingest, cleanup, purge and retry; edits here are overwritten.", "",
           "| State | Images |", "|---|---|",
           f"| held (kept, retried, never auto-deleted) | {len(held)} |",
           f"| attached (kept forever in the vault) | {len(attached)} |",
           f"| quarantined (purged 7 days after) | {len(quarantined)} |",
           f"| purged | {len(purged)} |", ""]
    if quarantined:
        nxt = min(quarantined, key=lambda r: r["purge_after"])
        out += [f"Next purge: {nxt['purge_after']} ({max(0, seconds_left(nxt, now))} s from now).", ""]
    if held:
        out += ["## Held", "", "| Reason | Images |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]
        out += ["", "| Note | Reason | Attempts | Image |", "|---|---|---|---|"]
        for r in sorted(held, key=lambda r: r.get("hold_reason") or ""):
            img = f"[open](file://{r['stored_path']})" if r.get("stored_path") else "missing"
            reason = (r.get("hold_reason") or "").replace("|", "\\|")
            out.append(f"| [[{stem(r)}]] | {reason} | {r.get('attempts') or 0} | {img} |")
        out.append("")
    if attached:
        out += ["## Attached (visual)", ""] + [f"- [[{stem(r)}]]" for r in attached] + [""]
    if redacted:
        out += ["## Redacted", "", "Wi-Fi QR passwords were removed before anything was written:", ""]
        out += [f"- [[{stem(r)}]]" for r in redacted] + [""]
    return "\n".join(out)
