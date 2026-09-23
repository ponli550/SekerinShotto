"""Phase 1 commands: schema, ingest, status, reindex."""
from __future__ import annotations

import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .contract import (AGENT_CONTRACT, COMMIT_ARG, COMMON_ARGS, EXIT_CODES, REGISTRY, SCHEMA_VERSION,
                       CONTRACT_VERSION, Arg, Result, ToolError, command)
from .extract import EXTRACTOR_VERSION, extract, iter_images, sha256_file
from .notes import NoteConflict, manifest_record, note_relpath, render, text_from_note
from .state import State, now_iso, verify_journal


def _content_root(state: State, arg: str | None, required: bool) -> Path | None:
    """The content root a state folder writes to. The binding lives in <state>/binding.json,
    not in the DB, because the DB is disposable (reindex rebuilds it)."""
    bound = state.bound_content()
    raw = arg or os.environ.get("SEKERINSHOTTO_CONTENT")
    if raw:
        path = Path(raw).expanduser().resolve()
        if bound and bound != path:
            raise ToolError(f"this state folder is bound to content root {bound}; "
                            f"got {path}. Use that path, or a different --state")
        return path
    if bound:
        return bound
    if required:
        raise ToolError("no content root: pass --content PATH (or set SEKERINSHOTTO_CONTENT)")
    return None


# ---------------------------------------------------------------- schema
@command("schema", "Every command, its flags, output shape and exit codes, plus the agent contract",
         args=[Arg("path", "only this command (e.g. ingest)", required=False)],
         details="Generated from the live command registry; it cannot disagree with the CLI.")
def cmd_schema(a, state):
    cmds = []
    for c in REGISTRY.values():
        args = [x.spec() for x in c.args + COMMON_ARGS + ([COMMIT_ARG] if c.writes else [])]
        cmds.append({"path": c.path, "summary": c.summary, "details": c.details,
                     "writes": c.writes, "json": True, "args": args})
    if a.path:
        match = [c for c in cmds if c["path"] == a.path]
        if not match:
            raise ToolError(f"unknown command {a.path!r}; valid: {', '.join(REGISTRY)}")
        return Result({"schema_version": SCHEMA_VERSION, "commands": match})
    return Result({
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "sekerinshotto", "version": __version__, "contract_version": CONTRACT_VERSION,
                 "extractor_version": EXTRACTOR_VERSION},
        "envelope": {"fields": ["command", "ok", "version", "data", "error"],
                     "rule": "exactly one of data (ok true) or error (ok false)"},
        "exit_codes": EXIT_CODES,
        "commands": cmds,
        "agent_contract": AGENT_CONTRACT,
    })


# ---------------------------------------------------------------- ingest
@command("ingest", "Extract images into notes: OCR text, QR payloads, URLs",
         args=[Arg("src", "an image file or a folder (searched recursively)"),
               Arg("--content", "content root the notes are written under (bound to the state on first use)"),
               Arg("--limit", "process at most N images", type=int),
               Arg("--workers", "parallel extraction threads", type=int, default=4)],
         writes=True,
         details="Plan: hashes every image and reports which are new or need re-extraction. "
                 "Commit: extracts them and writes notes, manifest and journal. Images already "
                 "extracted with the current extractor version are skipped. Exit 2 when some "
                 "notes were not overwritten because a human removed their generated markers.")
def cmd_ingest(a, state: State):
    src = Path(a.src).expanduser().resolve()
    if not src.exists():
        raise ToolError(f"source {src} does not exist")
    content = _content_root(state, a.content, required=True)
    con = state.connect()

    t0 = time.perf_counter()
    todo, skipped, dup_in_batch, seen = [], 0, 0, set()
    for p in iter_images(src, a.limit):
        fid = sha256_file(p)
        if fid in seen:
            dup_in_batch += 1
            continue
        seen.add(fid)
        row = con.execute("SELECT extractor_version, note_path FROM items WHERE id=?", (fid,)).fetchone()
        if row and row["extractor_version"] == EXTRACTOR_VERSION and row["note_path"] \
                and (content / row["note_path"]).exists():
            skipped += 1
            continue
        todo.append((p, fid, "re-extract" if row else "new"))
    plan = {"source": str(src), "content_root": str(content), "state": str(state.root),
            "planned": len(todo), "skipped_already_extracted": skipped,
            "duplicates_in_batch": dup_in_batch,
            "items": [{"file": p.name, "id": fid, "action": act} for p, fid, act in todo]}
    if not a.commit:
        plan["committed"] = False
        return Result(plan)

    batch_id = now_iso().replace(":", "-")
    with state.lock():
        state.bind_content(content)
        journal = state.journal(batch_id)
        manifest = state.dir("batches") / f"{batch_id}.jsonl"
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
            results = list(pool.map(lambda t: extract(t[0], t[1]), todo))
        written, failed, conflicts, times = 0, [], [], []
        ingested = now_iso()
        with open(manifest, "a") as mf:
            for ex in results:
                times.append(ex.elapsed_ms)
                rel = note_relpath(ex)
                prior = con.execute("SELECT note_path FROM items WHERE id=?", (ex.id,)).fetchone()
                if prior and prior["note_path"]:
                    rel = prior["note_path"]           # a note keeps its path once written
                path = content / rel
                existing = path.read_text() if path.exists() else None
                try:
                    body = render(ex, ingested, existing)
                except NoteConflict:
                    conflicts.append({"id": ex.id, "note": rel})
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                journal.append(op="update" if existing is not None else "create", id=ex.id,
                               path=str(path), batch_id=batch_id)
                rec = manifest_record(ex, batch_id, rel)
                mf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                _index(con, rec, ex.text, ingested)
                written += 1
                if ex.status != "ok":
                    failed.append({"id": ex.id, "file": ex.path.name, "reason": ex.status_reason})
        con.commit()
    data = {**plan, "committed": True, "batch_id": batch_id, "written": written,
            "failed": failed, "conflicts": conflicts,
            "urls": {"from_qr": sum(1 for ex in results for u in ex.urls if u["verified_by"] == "qr"),
                     "from_ocr": sum(1 for ex in results for u in ex.urls if u["verified_by"] == "none")},
            "qr_types": _count(b["type"] for ex in results for b in ex.barcodes),
            "timing": {"total_s": round(time.perf_counter() - t0, 1),
                       "per_image_median_ms": int(statistics.median(times)) if times else 0,
                       "per_image_max_ms": max(times) if times else 0}}
    data.pop("items")
    return Result(data, violation=bool(conflicts))


def _count(it) -> dict:
    out: dict = {}
    for x in it:
        out[x] = out.get(x, 0) + 1
    return out


def _index(con, rec: dict, text: str, ingested: str) -> None:
    con.execute("DELETE FROM entities WHERE item_id=?", (rec["id"],))
    con.execute("DELETE FROM text_fts WHERE id=?", (rec["id"],))
    con.execute("""INSERT INTO items (id, source_path, source_app, captured_at, width, height, bytes,
                   extractor_version, source_state, status, status_reason, ocr_confidence, text_chars,
                   note_path, batch_id, ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET source_path=excluded.source_path,
                   extractor_version=excluded.extractor_version, status=excluded.status,
                   status_reason=excluded.status_reason, ocr_confidence=excluded.ocr_confidence,
                   text_chars=excluded.text_chars, note_path=excluded.note_path,
                   batch_id=excluded.batch_id, ingested_at=excluded.ingested_at""",
                (rec["id"], rec["source_path"], rec["source_app"], rec["captured_at"], rec["width"],
                 rec["height"], rec["bytes"], rec.get("extractor_version", EXTRACTOR_VERSION),
                 rec["source_state"], rec["status"], rec["status_reason"], rec["ocr_confidence"],
                 rec["text_chars"], rec["note_path"], rec["batch_id"], ingested))
    ents = rec["entities"]
    for u in ents["urls"]:
        con.execute("INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                    (rec["id"], "url", u["url"], u["raw"], None, u["verified_by"], u["confidence"]))
    for b in ents["qr"]:
        con.execute("INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                    (rec["id"], "qr", b["payload"], None, b["type"], "qr", 1.0))
    for d in ents["domains"]:
        con.execute("INSERT INTO entities VALUES (?,?,?,?,?,?,?)", (rec["id"], "domain", d, None, None, None, None))
    con.execute("INSERT INTO text_fts VALUES (?,?)", (rec["id"], text))


# ---------------------------------------------------------------- status
@command("status", "Counts, content root, and journal integrity",
         details="Read-only. Journal integrity recomputes every row's hash chain and signature.")
def cmd_status(a, state: State):
    if not state.exists:
        return Result({"state": str(state.root), "initialized": False})
    con = state.connect()
    q = lambda sql: {r[0]: r[1] for r in con.execute(sql)}
    journals = sorted(state.dir("journal").glob("*.jsonl"))
    key = (state.root / "journal.key").read_bytes() if (state.root / "journal.key").exists() else b""
    broken = [p.name for p in journals if not verify_journal(p, key)[0]]
    root = state.bound_content()
    return Result({
        "state": str(state.root), "initialized": True,
        "content_root": str(root) if root else None,
        "items": con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "by_status": q("SELECT status, COUNT(*) FROM items GROUP BY status"),
        "by_source_state": q("SELECT source_state, COUNT(*) FROM items GROUP BY source_state"),
        "failed_by_reason": q("SELECT status_reason, COUNT(*) FROM items WHERE status!='ok' GROUP BY status_reason"),
        "urls_by_verification": q("SELECT verified_by, COUNT(*) FROM entities WHERE kind='url' GROUP BY verified_by"),
        "qr_by_type": q("SELECT subtype, COUNT(*) FROM entities WHERE kind='qr' GROUP BY subtype"),
        "top_apps": q("SELECT source_app, COUNT(*) c FROM items GROUP BY source_app ORDER BY c DESC LIMIT 10"),
        "journal": {"files": len(journals), "intact": not broken, "broken": broken},
    })


# ---------------------------------------------------------------- reindex
@command("reindex", "Rebuild index.sqlite from manifests and notes",
         args=[Arg("--content", "content root, if the state folder has no binding yet")],
         writes=True,
         details="The DB is a derived index (FORMAT.md §5). Plan: counts what would be rebuilt. "
                 "Commit: drops items, entities and full-text rows and rebuilds them; the latest "
                 "manifest record per id wins, text is read back from the note.")
def cmd_reindex(a, state: State):
    content = _content_root(state, a.content, required=True)
    con = state.connect()
    latest: dict[str, dict] = {}
    for mf in sorted(state.dir("batches").glob("*.jsonl")):
        for line in mf.read_text().splitlines():
            rec = json.loads(line)
            latest[rec["id"]] = rec
    missing = [r["note_path"] for r in latest.values() if not (content / r["note_path"]).exists()]
    plan = {"manifest_records": len(latest), "notes_missing": len(missing), "missing": missing[:20]}
    if not a.commit:
        return Result({**plan, "committed": False})
    with state.lock():
        con.executescript("DELETE FROM entities; DELETE FROM text_fts; DELETE FROM items;")
        for rec in latest.values():
            p = content / rec["note_path"]
            text = text_from_note(p.read_text()) if p.exists() else ""
            _index(con, rec, text, rec.get("ingested", now_iso()))
        con.commit()
    return Result({**plan, "committed": True, "rebuilt": len(latest)})
