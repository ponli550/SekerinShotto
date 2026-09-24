"""Phase 1 commands: schema, ingest, status, reindex."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__
from .contract import (AGENT_CONTRACT, COMMIT_ARG, COMMON_ARGS, EXIT_CODES, REGISTRY, SCHEMA_VERSION,
                       CONTRACT_VERSION, Arg, Result, ToolError, command)
from .extract import EXTRACTOR_VERSION, domain_of, extract, iter_images, sha256_file
from .notes import (NoteConflict, extraction_from_record, manifest_record, note_relpath, render, render_group,
                    render_sequence,
                    text_from_note, user_part_is_empty)
from .organize import changed, load_items, organize
from .rules import load as load_rules
from .domains import DomainIndex, update as domains_update
from .state import State, now_iso, verify_journal
from .urlfix import fix_urls, load_allowed, qr_domains_of, raw_host_of, resolve


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
         args=[Arg("src", "an image file or a folder (searched recursively); default: <state>/inbox", required=False),
               Arg("--content", "content root the notes are written under (bound to the state on first use)"),
               Arg("--limit", "process at most N images", type=int),
               Arg("--workers", "parallel extraction threads", type=int, default=4),
               Arg("--cleanup", "after committing, run cleanup too (moves the original images)", flag=True)],
         writes=True,
         details="Plan: hashes every image and reports which are new or need re-extraction. "
                 "Commit: extracts them and writes notes, manifest and journal. Images already "
                 "extracted with the current extractor version are skipped. Exit 2 when some "
                 "notes were not overwritten because a human removed their generated markers.")
def cmd_ingest(a, state: State):
    src = Path(a.src).expanduser().resolve() if a.src else state.dir("inbox")
    if not a.src:
        state.ensure()
    if not src.exists():
        raise ToolError(f"source {src} does not exist")
    content = _content_root(state, a.content, required=True)
    con = state.connect()

    t0 = time.perf_counter()
    todo, skipped, dup_in_batch, seen = [], 0, 0, set()
    copies: dict[str, list[Path]] = {}
    for p in (getattr(a, "files", None) or iter_images(src, a.limit)):
        fid = sha256_file(p)
        if fid in seen:
            dup_in_batch += 1
            copies.setdefault(fid, []).append(p)
            continue
        seen.add(fid)
        row = con.execute("SELECT extractor_version, note_path, source_state, record FROM items WHERE id=?",
                          (fid,)).fetchone()
        if row and row["record"]:
            prev = json.loads(row["record"])
            here = prev.get("stored_path") if prev.get("source_state") != "present" else prev.get("source_path")
            if not here or Path(here).resolve() != p.resolve():
                copies.setdefault(fid, []).append(p)      # the same bytes at another path
                if row["source_state"] == "present":
                    skipped += 1
                    continue
        if row and row["source_state"] != "present":
            skipped += 1                      # already routed by cleanup (held/quarantined/attached/purged)
            continue
        if row and row["extractor_version"] == EXTRACTOR_VERSION and row["note_path"] \
                and (content / row["note_path"]).exists():
            skipped += 1
            continue
        todo.append((p, fid, "re-extract" if row else "new"))
    plan = {"source": str(src), "content_root": str(content), "state": str(state.root),
            "planned": len(todo), "skipped_already_extracted": skipped,
            "duplicates_in_batch": dup_in_batch,
            "copies_found": sum(len(v) for v in copies.values()),
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
        dom = DomainIndex(state.dir("domains"))
        qr_known = qr_domains_of([r[0] for r in con.execute(
            "SELECT value FROM entities WHERE kind='url' AND verified_by='qr'")], dom)
        qr_known |= qr_domains_of([u["url"] for ex in results for u in ex.urls if u["verified_by"] == "qr"], dom)
        allowed = load_allowed(state.dir("domains"))
        for ex in results:
            fix_urls(ex, dom, qr_known, allowed)
        ingested = now_iso()
        fresh = set()
        for ex in results:
            prior = con.execute("SELECT note_path, record FROM items WHERE id=?", (ex.id,)).fetchone()
            rel = prior["note_path"] if prior and prior["note_path"] else None
            if prior and prior["record"]:
                ex.copies = json.loads(prior["record"]).get("copies") or []   # survive re-extraction
            _index(con, manifest_record(ex, batch_id, rel), ex.text, ingested)
            fresh.add(ex.id)
        for fid, paths in copies.items():
            row = con.execute("SELECT record FROM items WHERE id=?", (fid,)).fetchone()
            if not row or not row["record"]:
                continue
            rec = json.loads(row["record"])
            known = {c["path"] for c in rec.get("copies") or []} | {rec.get("source_path"), rec.get("stored_path")}
            new_c = [{"path": str(pp), "state": "present"} for pp in paths if str(pp) not in known]
            if new_c:
                _save(con, rec, copies=(rec.get("copies") or []) + new_c)
                fresh.add(fid)
        report = apply_organization(state, con, content, journal, manifest, batch_id, ingested, force=fresh)
        con.commit()
    times = [ex.elapsed_ms for ex in results]
    failed = [{"id": ex.id, "file": ex.path.name, "reason": ex.status_reason} for ex in results if ex.status != "ok"]
    conflicts = report.pop("conflicts")
    data = {**plan, "committed": True, "batch_id": batch_id, "written": report["notes_written"],
            "failed": failed, "conflicts": conflicts, "organize": report,
            "urls": {**_count(u["verified_by"] for ex in results for u in ex.urls),
                     "corrected": sum(1 for ex in results for u in ex.urls if u.get("corrected")),
                     "flagged": _count(u["flag"] for ex in results for u in ex.urls if u.get("flag"))},
            "domain_list": dom.info.get("tranco_list_id") or "missing: run `domains update --commit`",
            "qr_types": _count(b["type"] for ex in results for b in ex.barcodes),
            "timing": {"total_s": round(time.perf_counter() - t0, 1),
                       "per_image_median_ms": int(statistics.median(times)) if times else 0,
                       "per_image_max_ms": max(times) if times else 0}}
    data.pop("items")
    if a.cleanup:
        with state.lock():
            data["cleanup"] = run_cleanup(state, con, content, True)
        conflicts = conflicts + data["cleanup"]["conflicts"]
    else:
        with state.lock():
            _write_audit(state, con, content)
    return Result(data, violation=bool(conflicts) or bool(a.cleanup and data["cleanup"]["held_total"]))


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
                   note_path, batch_id, ingested_at, record, category, decided_by, why, group_id, rank,
                   group_size, stored_path, purge_after, hold_reason, attempts, keep, confirmed_by, added_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET source_path=excluded.source_path,
                   source_app=excluded.source_app, captured_at=excluded.captured_at, width=excluded.width,
                   height=excluded.height, bytes=excluded.bytes,
                   extractor_version=excluded.extractor_version, status=excluded.status,
                   status_reason=excluded.status_reason, ocr_confidence=excluded.ocr_confidence,
                   text_chars=excluded.text_chars, note_path=excluded.note_path,
                   batch_id=excluded.batch_id, ingested_at=excluded.ingested_at, record=excluded.record,
                   category=excluded.category, decided_by=excluded.decided_by, why=excluded.why,
                   group_id=excluded.group_id, rank=excluded.rank, group_size=excluded.group_size,
                   source_state=excluded.source_state, stored_path=excluded.stored_path,
                   purge_after=excluded.purge_after, hold_reason=excluded.hold_reason,
                   attempts=excluded.attempts, keep=excluded.keep, confirmed_by=excluded.confirmed_by""",
                (rec["id"], rec["source_path"], rec["source_app"], rec["captured_at"], rec["width"],
                 rec["height"], rec["bytes"], rec.get("extractor_version", EXTRACTOR_VERSION),
                 rec["source_state"], rec["status"], rec["status_reason"], rec["ocr_confidence"],
                 rec["text_chars"], rec["note_path"], rec["batch_id"], ingested,
                 json.dumps(rec, ensure_ascii=False), rec.get("category"), rec.get("decided_by"), rec.get("why"),
                 rec.get("group"), rec.get("rank"), rec.get("group_size"), rec.get("stored_path"),
                 rec.get("purge_after"), rec.get("hold_reason"), rec.get("attempts") or 0,
                 1 if rec.get("keep") else 0, rec.get("confirmed_by"), ingested))
    # added_at is set once, on first insert (the ON CONFLICT branch never touches it)
    ents = rec["entities"]
    for u in ents["urls"]:
        sub = u.get("flag") or ("corrected" if u.get("corrected") else None)
        con.execute("INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                    (rec["id"], "url", u["url"], u["raw"], sub, u["verified_by"], u.get("confidence")))
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
        "by_category": q("SELECT category, COUNT(*) FROM items GROUP BY category"),
        "groups": con.execute("SELECT COUNT(DISTINCT group_id) FROM items WHERE group_id IS NOT NULL").fetchone()[0],
        "urls_corrected_or_flagged": q("SELECT subtype, COUNT(*) FROM entities WHERE kind='url' AND subtype IS NOT NULL GROUP BY subtype"),
        "domain_list": DomainIndex(state.dir("domains")).info or None,
        "held_bytes": sum(p.stat().st_size for p in state.dir("held").glob("*") if p.is_file()),
        "quarantine_bytes": sum(p.stat().st_size for p in state.dir("quarantine").rglob("*") if p.is_file()),
        "next_purge": con.execute("SELECT MIN(purge_after) FROM items WHERE source_state='quarantined'").fetchone()[0],
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


# ---------------------------------------------------------------- domains
@command("domains update", "Download the reference lists used to check and correct URLs",
         writes=True,
         details="Fetches the latest Tranco top-1M ranking, the Public Suffix List and the IANA TLD "
                 "list into <state>/domains/. This is the only command that uses the network, and it "
                 "fetches reference lists only, never a URL read from a screenshot. Re-run ingest "
                 "afterwards to apply corrections to images already extracted.")
def cmd_domains_update(a, state: State):
    info = DomainIndex(state.dir("domains")).info
    if not a.commit:
        return Result({"committed": False, "current": info or None,
                       "would_download": ["Tranco top-1M (~10 MB)", "Public Suffix List", "IANA TLD list"]})
    with state.lock():
        new = domains_update(state.dir("domains"))
        content = _content_root(state, None, required=False)
        res = reverify(state, state.connect(), content, now_iso().replace(":", "-") + "-domains") \
            if content and state.exists else None
    return Result({"committed": True, "previous": info or None, "current": new, "reverify": res})


# ---------------------------------------------------------------- organize
def apply_organization(state: State, con, content: Path, journal, manifest: Path | None, batch_id: str,
                       ingested: str, force: set[str] = frozenset(), commit: bool = True) -> dict:
    """Classify, group and rank every item; (re)write the notes whose organization changed."""
    rules, rules_src = load_rules(state.root)
    items = load_items(con)
    from .terms import load_stopterms
    org = organize(items, rules, content, load_stopterms(state.root))
    targets = sorted(i for i in items if i in force or changed(items[i], org[i]) or not items[i]["_note_path"])
    moves = []
    for i in targets:
        old = items[i]["_note_path"]
        if old and (not old.startswith(f"notes/{org[i]['category']}/") or Path(old).name.startswith("undated-")):
            moves.append({"id": i, "from": old, "to": f"notes/{org[i]['category']}/{Path(old).name}"})
    groups: dict[str, list[str]] = {}
    for i, o in org.items():
        if o["group"]:
            groups.setdefault(o["group"], []).append(i)
    prev_groups = {r["_prev"]["group"] for r in items.values() if r["_prev"]["group"]}
    summary = {"rules": rules_src, "notes_to_write": len(targets), "moves": len(moves),
               "by_category": _count(o["category"] for o in org.values()),
               "groups": len(groups), "in_groups": sum(len(v) for v in groups.values()),
               "sequences": len({o["sequence"] for o in org.values() if o.get("sequence")}),
               "groups_dissolved": sorted(prev_groups - set(groups))}
    if not commit:
        return {**summary, "move_list": moves[:50], "conflicts": []}

    written, conflicts = 0, []
    mf = open(manifest, "a") if manifest else None
    try:
        for i in targets:
            rec, o = items[i], org[i]
            text = rec.get("_text", "")
            ex = extraction_from_record(rec, text)
            old_rel = rec["_note_path"]
            name = Path(old_rel).name if old_rel else None
            if name and name.startswith("undated-") and ex.captured_at:
                name = None                                  # the one rename: an undated note that gained a date
            new_rel = f"notes/{o['category']}/{name}" if name else note_relpath(ex, o["category"])
            old_path, new_path = (content / old_rel) if old_rel else None, content / new_rel
            existing = old_path.read_text() if old_path and old_path.exists() else None
            try:
                body = render(ex, ingested, existing, o)
            except NoteConflict:
                conflicts.append({"id": i, "note": old_rel})
                continue
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text(body)
            if old_path and old_path.exists() and old_path != new_path:
                old_path.unlink()
                if old_path.parent != content / "notes" and not any(old_path.parent.iterdir()):
                    old_path.parent.rmdir()               # a category folder emptied by the move
                journal.append(op="move", id=i, path=str(new_path), from_path=str(old_path), batch_id=batch_id)
            else:
                journal.append(op="update" if existing is not None else "create", id=i, path=str(new_path),
                               batch_id=batch_id)
            new_rec = manifest_record(ex, batch_id, new_rel, None, o)
            if mf:
                mf.write(json.dumps(new_rec, ensure_ascii=False) + "\n")
            _index(con, new_rec, text, ingested)
            items[i]["_note_path"] = new_rel
            written += 1

        stems = {i: Path(items[i]["_note_path"]).stem for i in items if items[i]["_note_path"]}
        for gid, ids in groups.items():
            ranked = sorted(ids, key=lambda i: org[i]["rank"])
            members = [{"stem": stems.get(i, i), "rank": org[i]["rank"], "score_why": org[i]["score_why"]}
                       for i in ranked]
            path = content / "groups" / f"{gid}.md"
            existing = path.read_text() if path.exists() else None
            try:
                body = render_group(gid, members, existing)
            except NoteConflict:
                conflicts.append({"id": gid, "note": f"groups/{gid}.md"})
                continue
            if body != existing:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                journal.append(op="update" if existing else "create", id=gid, path=str(path), batch_id=batch_id)
        seqs: dict[str, list[str]] = {}
        for i, o in org.items():
            if o.get("sequence"):
                seqs.setdefault(o["sequence"], []).append(i)
        for sid, ids in seqs.items():
            ordered = sorted(ids, key=lambda i: org[i]["seq_part"])
            path = content / "sequences" / f"{sid}.md"
            existing = path.read_text() if path.exists() else None
            try:
                body = render_sequence(sid, [stems.get(i, i) for i in ordered], org[ordered[0]]["seq_text"], existing)
            except NoteConflict:
                conflicts.append({"id": sid, "note": f"sequences/{sid}.md"})
                continue
            if body != existing:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                journal.append(op="update" if existing else "create", id=sid, path=str(path), batch_id=batch_id)
        prev_seqs = {r["_prev"].get("sequence") for r in items.values() if r["_prev"].get("sequence")}
        for sid in sorted(prev_seqs - set(seqs)):
            path = content / "sequences" / f"{sid}.md"
            if path.exists() and user_part_is_empty(path.read_text()):
                path.unlink()
                journal.append(op="delete", id=sid, path=str(path), batch_id=batch_id)
        kept = []
        for gid in summary["groups_dissolved"]:
            path = content / "groups" / f"{gid}.md"
            if path.exists() and user_part_is_empty(path.read_text()):
                path.unlink()
                journal.append(op="delete", id=gid, path=str(path), batch_id=batch_id)
            elif path.exists():
                kept.append(gid)                          # the user wrote in it; leave it
    finally:
        if mf:
            mf.close()
    return {**summary, "notes_written": written, "dissolved_hubs_kept": kept, "conflicts": conflicts}


@command("organize", "Re-apply classification rules, duplicate groups and ranking to every note",
         writes=True,
         details="Plan: reports category counts, groups, and which notes would be written or moved. "
                 "Commit: rewrites only notes whose category, group or rank changed, moving them to "
                 "notes/<category>/. Categories set by an LLM, the user or Laya (decided_by) are never "
                 "overridden. Rules come from <state>/rules.toml if present, else the built-in rules. "
                 "Exit 2 when some notes were skipped because their generated markers are gone.")
def cmd_organize(a, state: State):
    content = _content_root(state, None, required=True)
    con = state.connect()
    if not a.commit:
        return Result({"committed": False, **apply_organization(state, con, content, None, None, "", now_iso(),
                                                                 commit=False)})
    batch_id = now_iso().replace(":", "-") + "-organize"
    with state.lock():
        report = apply_organization(state, con, content, state.journal(batch_id),
                                    state.dir("batches") / f"{batch_id}.jsonl", batch_id, now_iso())
        con.commit()
    conflicts = report.pop("conflicts")
    return Result({"committed": True, "batch_id": batch_id, **report, "conflicts": conflicts},
                  violation=bool(conflicts))


# ---------------------------------------------------------------- cleanup (phase 4)
from . import cleanup as lc  # noqa: E402


def _records(con, where: str = "1=1", params=()) -> list[dict]:
    out = []
    for r in con.execute(f"SELECT record, note_path FROM items WHERE record IS NOT NULL AND {where}", params):
        rec = json.loads(r["record"])
        rec["note_path"] = r["note_path"]
        out.append(rec)
    return out


def _text(con, iid: str) -> str:
    row = con.execute("SELECT text FROM text_fts WHERE id=?", (iid,)).fetchone()
    return row[0] if row else ""


def _save(con, rec: dict, **changes) -> dict:
    rec = {**rec, **changes}
    _index(con, rec, _text(con, rec["id"]), now_iso())
    return rec


def resolve_id(con, token: str) -> str:
    """Full id from a full id, a >= 8-hex prefix, or a note filename stem. Never guesses."""
    t = token.strip()
    if t.endswith(".md"):
        t = t[:-3]
    rows = con.execute("SELECT id FROM items WHERE id=? OR substr(id, 8) LIKE ? OR note_path LIKE ?",
                       (t, (t.split(":")[-1] + "%") if len(t.split(":")[-1]) >= 8 else "\x00", f"%/{t}.md")).fetchall()
    ids = sorted({r[0] for r in rows})
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise ToolError(f"no item matches {token!r}; pass a full id, an id prefix of at least 8 hex chars, "
                        "or a note filename")
    raise ToolError(f"{token!r} is ambiguous: {', '.join(i[:15] for i in ids[:5])}")


def _write_audit(state: State, con, content: Path) -> None:
    content.mkdir(parents=True, exist_ok=True)
    (content / "AUDIT.md").write_text(lc.render_audit(_records(con), now_iso()) + "\n")


def _rerender(state: State, con, content: Path, journal, batch_id: str, ids: set[str]) -> dict:
    if not ids:
        return {"notes_written": 0, "conflicts": []}
    return apply_organization(state, con, content, journal, state.dir("batches") / f"{batch_id}.jsonl",
                              batch_id, now_iso(), force=ids)


def run_cleanup(state: State, con, content: Path, commit: bool, batch_id: str | None = None,
                only: set[str] | None = None) -> dict:
    plan, moved, errors = [], [], []
    for rec in _records(con, "source_state IN ('present', 'held')"):
        if only is not None and rec["id"] not in only:
            continue                                   # confirm/keep touch their target, nothing else
        outcome, reason = lc.decide(rec)
        if rec["source_state"] == "held" and outcome == "hold":
            continue                                   # stays held; nothing to do
        plan.append({"id": rec["id"], "note": rec.get("note_path"), "outcome": outcome, "reason": reason})
    counts = _count(p["outcome"] for p in plan)
    moving = {p["id"] for p in plan}
    pending_copies = sum(1 for rec in _records(con) if only is None or rec["id"] in only
                         for c in rec.get("copies") or []
                         if c.get("state") == "present" and (rec["source_state"] != "present" or rec["id"] in moving))
    if pending_copies:
        counts["copies_to_quarantine"] = pending_copies
    if not commit:
        return {"committed": False, "plan": counts, "items": plan[:200],
                "already_held": con.execute("SELECT COUNT(*) FROM items WHERE source_state='held'").fetchone()[0]}
    batch_id = batch_id or now_iso().replace(":", "-") + "-cleanup"
    journal = state.journal(batch_id)
    audit = open(state.dir("audit") / f"{batch_id}.jsonl", "a")
    try:
        for p in plan:
            rec = json.loads(con.execute("SELECT record FROM items WHERE id=?", (p["id"],)).fetchone()[0])
            src = lc.current_file(rec)
            try:
                fields = lc.route(rec, p["outcome"], p["reason"], state.root, content, batch_id)
            except ToolError as e:
                errors.append({"id": p["id"], "error": str(e)})
                continue
            journal.append(op=p["outcome"], id=p["id"], path=fields["stored_path"], from_path=str(src),
                           batch_id=batch_id)
            _save(con, rec, **fields)
            moved.append(p["id"])
            if p["outcome"] != "quarantine" or any(b["type"] == "wifi" for b in rec["entities"]["qr"]):
                audit.write(json.dumps({"id": p["id"], "source_path": rec["source_path"], "outcome": p["outcome"],
                                        "reason": p["reason"], "ocr_confidence": rec.get("ocr_confidence"),
                                        "text_chars": rec.get("text_chars"), "qr_detected": len(rec["entities"]["qr"]),
                                        "attempts": rec.get("attempts") or 0, "at": now_iso(),
                                        "note_path": rec.get("note_path")}, ensure_ascii=False) + "\n")
        con.commit()
        copied = _quarantine_copies(state, con, journal, batch_id, only)
        con.commit()
        rer = _rerender(state, con, content, journal, batch_id, set(moved) | copied)
        con.commit()
    finally:
        audit.close()
    _write_audit(state, con, content)
    held = con.execute("SELECT COUNT(*) FROM items WHERE source_state='held'").fetchone()[0]
    return {"committed": True, "batch_id": batch_id, "moved": _count(p["outcome"] for p in plan if p["id"] in moved),
            "copies_quarantined": _copies_count(con, "quarantined"),
            "held_total": held, "errors": errors, "notes_rewritten": rer["notes_written"],
            "conflicts": rer["conflicts"]}


@command("cleanup", "Route extracted images: quarantine, keep as attachment, or hold",
         writes=True,
         details="Read well -> <state>/quarantine/, purged exactly 7 days later (604800 s). Visual (photos, "
                 "video frames; little text, rich pixels, no QR) -> <content>/attachments/, embedded in the "
                 "note, kept forever. Failed, or with an unverified OCR URL -> <state>/held/, retried, never "
                 "auto-deleted. Moves the original image files. Writes AUDIT.md. Exit 2 when images are "
                 "held back: a valid answer, not a failure.")
def cmd_cleanup(a, state: State):
    content = _content_root(state, None, required=True)
    con = state.connect()
    if not a.commit:
        return Result(run_cleanup(state, con, content, False))
    with state.lock():
        res = run_cleanup(state, con, content, True)
    return Result(res, violation=res["held_total"] > 0)


@command("purge", "Delete quarantined images whose 7 days are up",
         writes=True,
         details="Plan: every quarantined image with its purge time and seconds remaining. Commit: deletes "
                 "only images with now >= purge_after, and only files that resolve inside "
                 "<state>/quarantine/. The note stays and records the purge; it is then the only record.")
def cmd_purge(a, state: State):
    content = _content_root(state, None, required=True)
    con = state.connect()
    now = now_iso()
    recs = _records(con, "source_state='quarantined'")
    due = [r for r in recs if lc.is_due(r, now)]
    copies_due = [(r, k) for r in _records(con) for k, c in enumerate(r.get("copies") or [])
                  if c.get("state") == "quarantined" and lc.is_due(c, now)]
    waiting = sorted(({"id": r["id"], "purge_after": r["purge_after"], "seconds_left": lc.seconds_left(r, now)}
                      for r in recs if not lc.is_due(r, now)), key=lambda x: x["seconds_left"])
    if not a.commit:
        return Result({"committed": False, "now": now, "due": len(due), "copies_due": len(copies_due),
                       "waiting": len(waiting),
                       "next": waiting[:10], "due_items": [{"id": r["id"], "purge_after": r["purge_after"]}
                                                           for r in due[:50]]})
    batch_id = now.replace(":", "-") + "-purge"
    purged, refused = [], []
    with state.lock():
        journal = state.journal(batch_id)
        for r, k in copies_due:
            r = json.loads(con.execute("SELECT record FROM items WHERE id=?", (r["id"],)).fetchone()[0])
            c = r["copies"][k]
            if lc.purge_file(Path(c["stored_path"]), state.root):
                journal.append(op="delete_copy", id=r["id"], path=c["stored_path"], batch_id=batch_id)
                r["copies"][k] = {**c, "state": "purged", "purged_at": now, "stored_path": None}
                _save(con, r, copies=r["copies"])
                purged.append(r["id"])
            else:
                refused.append({"id": r["id"], "path": c["stored_path"],
                                "reason": "copy is not a file inside the quarantine folder; left untouched"})
        for r in due:
            r = json.loads(con.execute("SELECT record FROM items WHERE id=?", (r["id"],)).fetchone()[0])
            if r.get("stored_path") and lc.purge_file(Path(r["stored_path"]), state.root):
                journal.append(op="delete", id=r["id"], path=r["stored_path"], batch_id=batch_id)
                _save(con, r, source_state="purged", purged_at=now, stored_path=None)
                purged.append(r["id"])
            else:
                refused.append({"id": r["id"], "path": r.get("stored_path"),
                                "reason": "not a file inside the quarantine folder; left untouched"})
        con.commit()
        rer = _rerender(state, con, content, journal, batch_id, set(purged))
        con.commit()
    _write_audit(state, con, content)
    return Result({"committed": True, "now": now, "purged": len(purged), "copies_purged": len(copies_due),
                   "refused": refused,
                   "waiting": len(waiting), "notes_rewritten": rer["notes_written"]}, violation=bool(refused))


@command("restore", "Move quarantined images back to where they came from",
         args=[Arg("target", "an item id / id prefix / note filename, or a quarantine batch id")],
         writes=True,
         details="Only quarantined images can be restored; after purge there is nothing to restore. "
                 "An image whose original path is occupied again is skipped, never overwritten.")
def cmd_restore(a, state: State):
    content = _content_root(state, None, required=True)
    con = state.connect()
    batch = [r for r in _records(con, "source_state='quarantined'")
             if r.get("stored_path") and Path(r["stored_path"]).parent.name == a.target]
    recs = batch or [json.loads(con.execute("SELECT record FROM items WHERE id=?",
                                            (resolve_id(con, a.target),)).fetchone()[0])]
    recs = [r for r in recs if r.get("source_state") == "quarantined"]
    if not recs:
        raise ToolError(f"{a.target!r} has nothing in quarantine (purged images cannot be restored)")
    if not a.commit:
        return Result({"committed": False, "would_restore": [{"id": r["id"], "to": r["source_path"],
                                                              "occupied": Path(r["source_path"]).exists()} for r in recs]})
    batch_id = now_iso().replace(":", "-") + "-restore"
    done, skipped = [], []
    with state.lock():
        journal = state.journal(batch_id)
        for r in recs:
            dst = Path(r["source_path"])
            if dst.exists():
                skipped.append({"id": r["id"], "reason": f"{dst} exists"})
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(r["stored_path"], dst)
            journal.append(op="restore", id=r["id"], path=str(dst), from_path=r["stored_path"], batch_id=batch_id)
            cs = r.get("copies") or []
            for k, c in enumerate(cs):
                if c.get("state") == "quarantined" and c.get("stored_path") and not Path(c["path"]).exists():
                    Path(c["path"]).parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(c["stored_path"], c["path"])
                    journal.append(op="restore_copy", id=r["id"], path=c["path"], from_path=c["stored_path"],
                                   batch_id=batch_id)
                    cs[k] = {"path": c["path"], "state": "present"}
            _save(con, r, source_state="present", stored_path=None, quarantined_at=None, purge_after=None,
                  copies=cs)
            done.append(r["id"])
        con.commit()
        _rerender(state, con, content, journal, batch_id, set(done))
        con.commit()
    _write_audit(state, con, content)
    return Result({"committed": True, "restored": len(done), "skipped": skipped}, violation=bool(skipped))


@command("retry", "Re-extract held images; those that now pass leave the held folder",
         writes=True,
         details="Runs extraction again on every image in <state>/held/ with the current extractor, "
                 "domain list and rules, then routes it like cleanup does. attempts counts the tries.")
def cmd_retry(a, state: State):
    content = _content_root(state, None, required=True)
    con = state.connect()
    held = _records(con, "source_state='held'")
    if not a.commit:
        return Result({"committed": False, "held": len(held),
                       "items": [{"id": r["id"], "reason": r.get("hold_reason"), "attempts": r.get("attempts") or 0}
                                 for r in held[:100]]})
    batch_id = now_iso().replace(":", "-") + "-retry"
    with state.lock():
        dom = DomainIndex(state.dir("domains"))
        qr_known = qr_domains_of([r[0] for r in con.execute(
            "SELECT value FROM entities WHERE kind='url' AND verified_by='qr'")], dom)
        for r in held:
            path = Path(r["stored_path"]) if r.get("stored_path") else None
            if not path or not path.exists():
                continue
            ex = extract(path, r["id"])
            fix_urls(ex, dom, qr_known, load_allowed(state.dir("domains")))
            ex.path = Path(r["source_path"])                 # the note keeps naming the original file
            for k in ("stored_path", "keep", "confirmed_by", "hold_reason"):
                setattr(ex, k, r.get(k))
            ex.copies = r.get("copies") or []
            ex.source_state, ex.attempts = "held", (r.get("attempts") or 0) + 1
            rec = manifest_record(ex, batch_id, r.get("note_path"), None,
                                  {"category": r.get("category") or "uncategorized", "decided_by": r.get("decided_by"),
                                   "why": r.get("why"), "group": r.get("group"), "rank": r.get("rank"),
                                   "size": r.get("group_size")})
            _index(con, rec, ex.text, now_iso())
        con.commit()
        res = run_cleanup(state, con, content, True, batch_id)
    return Result({"retried": len(held), **res}, violation=res["held_total"] > 0)


def _flag(state: State, a, **changes) -> Result:
    content = _content_root(state, None, required=True)
    con = state.connect()
    iid = resolve_id(con, a.id)
    rec = json.loads(con.execute("SELECT record FROM items WHERE id=?", (iid,)).fetchone()[0])
    if rec["source_state"] not in ("present", "held"):
        hint = " Run `restore` first." if rec["source_state"] == "quarantined" else ""
        raise ToolError(f"{iid[:15]} is {rec['source_state']}; only present or held images can be changed.{hint}")
    after = lc.decide({**rec, **changes})
    if not a.commit:
        return Result({"committed": False, "id": iid, "state": rec["source_state"], "set": changes,
                       "cleanup_would": after[0]})
    with state.lock():
        _save(con, rec, **changes)
        con.commit()
        res = run_cleanup(state, con, content, True, only={iid})
    return Result({"committed": True, "id": iid, "set": changes, "cleanup": res})


@command("confirm", "Release a held image: the caller vouches for its extraction",
         args=[Arg("id", "item id, id prefix (>= 8 hex) or note filename"),
               Arg("--by", "who confirms: llm or user", default="llm")],
         writes=True,
         details="Sets confirmed_by, which lifts a hold for failed extraction or unverified URLs, then runs "
                 "cleanup so the image moves on (usually to quarantine).")
def cmd_confirm(a, state: State):
    if a.by not in ("llm", "user"):
        raise ToolError("--by must be 'llm' or 'user'")
    return _flag(state, a, confirmed_by=a.by)


@command("keep", "Keep an image forever as a vault attachment (e.g. a diagram OCR cannot capture)",
         args=[Arg("id", "item id, id prefix (>= 8 hex) or note filename")],
         writes=True,
         details="The visual detector finds photos and video frames, not text-heavy diagrams. keep moves "
                 "the image to <content>/attachments/ and embeds it in its note. Quarantined images can "
                 "be kept only after restore.")
def cmd_keep(a, state: State):
    return _flag(state, a, keep=True)


# ---------------------------------------------------------------- text commands (phase 5)
from .notes import read_frontmatter, set_frontmatter  # noqa: E402
from .redact import redact, redact_qr  # noqa: E402
from .rules import _CATEGORY  # noqa: E402

_FILTER_ARGS = [
    Arg("--category", "only this category"), Arg("--domain", "only notes with a URL on this domain (suffix)"),
    Arg("--app", "only screenshots from this app (package prefix)"),
    Arg("--since", "captured on or after this date (YYYY-MM-DD)"), Arg("--until", "captured on or before (YYYY-MM-DD)"),
    Arg("--group", "only members of this duplicate group"),
    Arg("--source-state", "present | held | attached | quarantined | purged"),
    Arg("--limit", "at most N results", type=int, default=20), Arg("--offset", "skip the first N", type=int, default=0),
]


def _filters(a) -> tuple[str, list]:
    where, params = ["i.record IS NOT NULL"], []
    if getattr(a, "category", None):
        where.append("i.category = ?"); params.append(a.category)
    if getattr(a, "uncategorized", False):
        where.append("i.category = 'uncategorized'")
    if getattr(a, "app", None):
        where.append("i.source_app LIKE ?"); params.append(a.app + "%")
    if getattr(a, "since", None):
        where.append("substr(i.captured_at, 1, 10) >= ?"); params.append(a.since)
    if getattr(a, "until", None):
        where.append("substr(i.captured_at, 1, 10) <= ?"); params.append(a.until)
    if getattr(a, "group", None):
        where.append("i.group_id = ?"); params.append(a.group)
    if getattr(a, "source_state", None):
        where.append("i.source_state = ?"); params.append(a.source_state)
    if getattr(a, "domain", None):
        d = a.domain.lower()
        where.append("EXISTS (SELECT 1 FROM entities e WHERE e.item_id = i.id AND e.kind = 'domain' "
                     "AND (e.value = ? OR e.value LIKE ?))"); params += [d, "%." + d]
    return " AND ".join(where), params


def _fts_query(q: str) -> str:
    """User text -> a safe FTS5 query: every word quoted (no operators), all must match."""
    words = [w for w in re.findall(r"[\w'-]+", q, re.UNICODE) if w]
    return " ".join('"' + w.replace('"', '') + '"' for w in words)


def _human_hits(title: str, hits: list[dict]) -> str:
    """Readable list for a terminal pane: one header line per note, then a one-line excerpt."""
    out = [title, ""]
    for h in hits:
        date = (h.get("captured_at") or "")[:16].replace("T", " ")
        app = (h.get("app") or "").split(".")[-1]
        extra = f"  answer {h['answer']}" if "answer" in h else ""
        out.append(f"{h['id'].split(':')[1][:8]}  {h['category']:<13} {date:<16}  {app:<12} {h['source_state']}{extra}")
        excerpt = " · ".join(x.strip() for x in (h.get("excerpt") or "").splitlines() if x.strip())
        out.append(f"          {excerpt[:150]}")
        out.append(f"          {h.get('note') or ''}")
        out.append("")
    return "\n".join(out) + "\n"


def _hit(r, snippet: str | None, redactions: list) -> dict:
    text, n = redact(snippet or "")
    redactions.append(n)
    return {"id": r["id"], "note": r["note_path"], "category": r["category"], "captured_at": r["captured_at"],
            "app": r["source_app"], "group": r["group_id"], "rank": r["rank"], "source_state": r["source_state"],
            "excerpt": text}


@command("search", "Full-text search over OCR text, with filters; excerpts are redacted",
         args=[Arg("query", "words that must all appear (no operators); may be empty with filters", required=False)]
              + _FILTER_ARGS,
         details="Matches every word of the query in the OCR text (SQLite FTS5, porter-free, case-insensitive), "
                 "combined with the filters. Excerpts pass PII redaction before they are returned.")
def cmd_search(a, state: State):
    con = state.connect()
    where, params = _filters(a)
    q = _fts_query(a.query or "")
    if q:
        sql = (f"SELECT i.*, snippet(text_fts, 1, '«', '»', '…', 16) AS snip, bm25(text_fts) AS score "
               f"FROM text_fts JOIN items i ON i.id = text_fts.id WHERE text_fts MATCH ? AND {where} "
               f"ORDER BY score LIMIT ? OFFSET ?")
        rows = con.execute(sql, [q, *params, a.limit, a.offset]).fetchall()
        total = con.execute(f"SELECT COUNT(*) FROM text_fts JOIN items i ON i.id = text_fts.id "
                            f"WHERE text_fts MATCH ? AND {where}", [q, *params]).fetchone()[0]
    else:
        if where == "i.record IS NOT NULL":
            raise ToolError("give a query or at least one filter (e.g. --category event)")
        rows = con.execute(f"SELECT i.*, substr(f.text, 1, 200) AS snip FROM items i JOIN text_fts f ON f.id = i.id "
                           f"WHERE {where} ORDER BY i.captured_at DESC LIMIT ? OFFSET ?",
                           [*params, a.limit, a.offset]).fetchall()
        total = con.execute(f"SELECT COUNT(*) FROM items i WHERE {where}", params).fetchone()[0]
    reds: list[int] = []
    hits = [_hit(r, r["snip"], reds) for r in rows]
    data = {"query": a.query or "", "total": total, "returned": len(hits), "offset": a.offset,
            "redactions": sum(reds), "results": hits}
    return Result(data, human=_human_hits(f"search {a.query or ''!r}: {total} notes (excerpts redacted)", hits))


@command("list", "List notes by category or state, newest first; excerpts are redacted",
         args=[Arg("--uncategorized", "only notes no rule matched (for the calling LLM to tag)", flag=True)]
              + _FILTER_ARGS)
def cmd_list(a, state: State):
    con = state.connect()
    where, params = _filters(a)
    rows = con.execute(f"SELECT i.*, substr(f.text, 1, 300) AS snip FROM items i JOIN text_fts f ON f.id = i.id "
                       f"WHERE {where} ORDER BY i.captured_at DESC LIMIT ? OFFSET ?",
                       [*params, a.limit, a.offset]).fetchall()
    total = con.execute(f"SELECT COUNT(*) FROM items i WHERE {where}", params).fetchone()[0]
    reds: list[int] = []
    hits = [_hit(r, r["snip"], reds) for r in rows]
    label = a.category or ("uncategorized" if a.uncategorized else "all")
    return Result({"total": total, "returned": len(rows), "offset": a.offset, "results": hits,
                   "redactions": sum(reds),
                   "categories": _count(r[0] for r in con.execute("SELECT category FROM items"))},
                  human=_human_hits(f"{label}: {total} notes, newest first (excerpts redacted)", hits))


@command("show", "One item in full: text, URLs, QR codes, category, group, image state (redacted)",
         args=[Arg("id", "item id, id prefix (>= 8 hex) or note filename"),
               Arg("--view", "open the card in read-only nvim (what the panels use)", flag=True)])
def cmd_show(a, state: State):
    con = state.connect()
    iid = resolve_id(con, a.id)
    r = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    rec = json.loads(r["record"])
    text, n = redact(_text(con, iid))
    urls = [{k: u.get(k) for k in ("raw", "url", "verified_by", "corrected", "reason", "flag", "joined")
             if u.get(k) not in (None, False, 0)} for u in rec["entities"]["urls"]]
    for u in urls:
        u["raw"] = redact(u["raw"])[0]
    from .notes import app_slug
    card = [f"{iid.split(':')[1][:8]} · {r['category']} · {(r['captured_at'] or '')[:16].replace('T', ' ')} · "
            f"{app_slug(r['source_app'])}", f"why: {r['why'] or '-'}",
            f"image: {r['source_state']}" + (f" (purge after {r['purge_after']})" if r["purge_after"] else "")
            + (f" · held: {r['hold_reason']}" if r["hold_reason"] else "")]
    if r["group_id"]:
        card.append(f"group: {r['group_id']} · rank {r['rank']} of {r['group_size']}")
    if rec.get("terms"):
        card.append("terms: " + ", ".join(rec["terms"]))
    if urls:
        card += ["", "links:"] + [f"  {u['url']}  [{u['verified_by']}{', corrected' if u.get('corrected') else ''}"
                                  f"{', ' + u['flag'] if u.get('flag') else ''}]" for u in urls]
    qrs = [redact_qr(q) for q in rec["entities"]["qr"]]
    if qrs:
        card += ["", "qr:"] + [f"  {q['type']}: {q['payload'][:80]}" for q in qrs]
    body, hidden = pv.content_only(rec, text)
    card += ["", f"text (redacted, {n} removed; {hidden} status-bar/noise lines hidden):", ""]
    card += [f"  {l}" for l in body] + ["", f"note: {r['note_path']}"]
    human = "\n".join(card) + "\n"
    if a.view:
        import tempfile
        tmp = Path(tempfile.gettempdir()) / f"sekerinshotto-card-{iid.split(':')[1][:8]}.txt"
        tmp.write_text(human)
        from . import contract as _c3
        os.dup2(_c3.OUT.fileno(), 1)                          # hand nvim the real terminal on fd 1
        os.execvp("nvim", ["nvim", "-R", "-n", "-c",
                           "set nonumber norelativenumber signcolumn=no foldcolumn=0 statuscolumn= wrap linebreak nomodifiable | "
                           "nnoremap <buffer> q :qa!<CR>", str(tmp)])
    return Result({"id": iid, "note": r["note_path"], "text": text, "redactions": n,
                   "urls": urls, "qr": [redact_qr(q) for q in rec["entities"]["qr"]],
                   "domains": rec["entities"]["domains"],
                   "category": r["category"], "decided_by": r["decided_by"], "why": r["why"],
                   "group": r["group_id"], "rank": r["rank"], "group_size": r["group_size"],
                   "app": r["source_app"], "captured_at": r["captured_at"], "status": r["status"],
                   "source_state": r["source_state"], "purge_after": r["purge_after"],
                   "hold_reason": r["hold_reason"]}, human=human)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


@command("tag", "Set a note's category as the calling LLM (or user), grounded by a verbatim quote",
         args=[Arg("id", "item id, id prefix (>= 8 hex) or note filename"),
               Arg("--category", "new category: lowercase letters, digits, hyphens", required=True),
               Arg("--quote", "text copied from the screenshot that justifies the category (required for llm)"),
               Arg("--by", "llm or user", default="llm")],
         writes=True,
         details="An LLM write-back is accepted only if --quote (at least 8 characters) appears verbatim in "
                 "the OCR text, as returned by show/search (redacted form accepted), ignoring case and "
                 "whitespace. Otherwise it is rejected with exit 1: invented reasons never reach the vault. "
                 "The note moves to notes/<category>/ and rules never override it afterwards.")
def cmd_tag(a, state: State):
    if a.by not in ("llm", "user"):
        raise ToolError("--by must be 'llm' or 'user'")
    if not _CATEGORY.match(a.category or ""):
        raise ToolError(f"invalid category {a.category!r}: lowercase letters, digits, hyphens, max 31 chars")
    content = _content_root(state, None, required=True)
    con = state.connect()
    iid = resolve_id(con, a.id)
    raw = _text(con, iid)
    quote = (a.quote or "").strip()
    if a.by == "llm" or quote:
        if len(quote) < 8:
            raise ToolError("an llm tag needs --quote with at least 8 characters copied from the screenshot text")
        if _norm(quote) not in _norm(raw) and _norm(quote) not in _norm(redact(raw)[0]):
            raise ToolError(f"--quote {quote[:60]!r} does not appear in this screenshot's text; "
                            "copy it verbatim from `show`")
    row = con.execute("SELECT note_path, category, decided_by FROM items WHERE id=?", (iid,)).fetchone()
    plan = {"id": iid, "from": {"category": row["category"], "decided_by": row["decided_by"]},
            "to": {"category": a.category, "decided_by": a.by}, "quote_verified": bool(quote),
            "note": row["note_path"]}
    if not a.commit:
        return Result({"committed": False, **plan})
    note = content / row["note_path"]
    if not note.exists():
        raise ToolError(f"note {row['note_path']} is missing; run reindex or ingest")
    batch_id = now_iso().replace(":", "-") + "-tag"
    with state.lock():
        journal = state.journal(batch_id)
        updates = {"category": a.category, "decided_by": a.by}
        if quote:
            updates["decided_evidence"] = quote
        note.write_text(set_frontmatter(note.read_text(), updates))
        journal.append(op="tag", id=iid, path=str(note), category=a.category, by=a.by, quote=quote,
                       batch_id=batch_id)
        rer = _rerender(state, con, content, journal, batch_id, {iid})
        con.commit()
    new = con.execute("SELECT note_path, category FROM items WHERE id=?", (iid,)).fetchone()
    return Result({"committed": True, **plan, "note": new["note_path"], "notes_rewritten": rer["notes_written"],
                   "conflicts": rer["conflicts"]}, violation=bool(rer["conflicts"]))


# ---------------------------------------------------------------- allowlist (phase 6)
def reverify(state: State, con, content: Path, batch_id: str) -> dict:
    """Re-check every stored OCR URL against the current domain list, QR crossref and allowlist.
    No image is read; held images whose URLs now pass are released by a scoped cleanup."""
    dom = DomainIndex(state.dir("domains"))
    allowed = load_allowed(state.dir("domains"))
    qr_known = qr_domains_of([r[0] for r in con.execute(
        "SELECT value FROM entities WHERE kind='url' AND verified_by='qr'")], dom)
    changed_ids = set()
    for rec in _records(con):
        ex = extraction_from_record(rec, "")
        before = json.dumps(rec["entities"]["urls"], sort_keys=True)
        fix_urls(ex, dom, qr_known, allowed)
        if json.dumps(ex.urls, sort_keys=True) != before:
            ents = {**rec["entities"], "urls": ex.urls, "domains": sorted({domain_of(u["url"]) for u in ex.urls})}
            _save(con, rec, entities=ents)
            changed_ids.add(rec["id"])
    con.commit()
    journal = state.journal(batch_id)
    rer = _rerender(state, con, content, journal, batch_id, changed_ids)
    con.commit()
    held = {r[0] for r in con.execute("SELECT id FROM items WHERE source_state='held'")} & changed_ids
    released = run_cleanup(state, con, content, True, batch_id, only=held) if held else {"moved": {}}
    if not held:
        _write_audit(state, con, content)
    return {"urls_changed_in": len(changed_ids), "notes_rewritten": rer["notes_written"],
            "held_rechecked": len(held), "released": released["moved"],
            "held_total": con.execute("SELECT COUNT(*) FROM items WHERE source_state='held'").fetchone()[0]}


def _allow_targets(state: State, raw: str) -> list[str]:
    dom = DomainIndex(state.dir("domains"))
    if not dom.available:
        # Without the Public Suffix List, "27a.onrender.com" would be stored as "onrender.com" and vouch
        # for every app on that host. Refuse rather than guess.
        raise ToolError("the allowlist needs the Public Suffix List: run `domains update --commit` first")
    out = []
    for d in [x.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
              for x in raw.split(",") if x.strip()]:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", d):
            raise ToolError(f"{d!r} is not a domain name")
        if not dom.valid_tld(d):
            raise ToolError(f"{d!r}: .{d.rsplit('.', 1)[-1]} is not a real top-level domain")
        reg = dom.registrable(d)
        if not reg:
            raise ToolError(f"{d!r} is a public suffix (like com.my), not a site")
        out.append(reg)
    if not out:
        raise ToolError("give at least one domain, e.g. hackfest2026.my or a,b,c")
    return sorted(set(out))


def _write_allowed(state: State, domains: set[str]) -> None:
    f = state.dir("domains") / "allow.txt"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# domains you vouch for; one registrable domain per line (managed by `domains allow`)\n"
                 + "".join(d + "\n" for d in sorted(domains)))


@command("domains allow", "Vouch for domains too small for the Tranco list; re-verifies stored URLs",
         args=[Arg("domains", "one domain or a comma-separated list; stored as the registrable domain")],
         writes=True,
         details="Allowing asserts the domain is real. OCR URLs on it become verified_by: allowed (links in "
                 "notes), it counts as evidence when correcting lookalikes, and held images whose last "
                 "unverified URL it covers are released. No image is re-read.")
def cmd_domains_allow(a, state: State):
    targets = _allow_targets(state, a.domains)
    current = load_allowed(state.dir("domains"))
    new = [d for d in targets if d not in current]
    if not a.commit:
        return Result({"committed": False, "would_add": new, "already": sorted(set(targets) - set(new))})
    content = _content_root(state, None, required=True)
    with state.lock():
        _write_allowed(state, current | set(targets))
        con = state.connect()
        res = reverify(state, con, content, now_iso().replace(":", "-") + "-allow")
    return Result({"committed": True, "added": new, "allowlist_size": len(current | set(targets)), **res})


@command("domains unallow", "Remove domains from the allowlist; re-verifies stored URLs",
         args=[Arg("domains", "one domain or a comma-separated list")],
         writes=True,
         details="URLs verified only by the allowlist go back to verified_by: none and stop being links. "
                 "Images already quarantined or purged are not pulled back.")
def cmd_domains_unallow(a, state: State):
    targets = _allow_targets(state, a.domains)
    current = load_allowed(state.dir("domains"))
    gone = [d for d in targets if d in current]
    if not a.commit:
        return Result({"committed": False, "would_remove": gone, "not_listed": sorted(set(targets) - set(gone))})
    content = _content_root(state, None, required=True)
    with state.lock():
        _write_allowed(state, current - set(targets))
        con = state.connect()
        res = reverify(state, con, content, now_iso().replace(":", "-") + "-unallow")
    return Result({"committed": True, "removed": gone, **res})


@command("domains list", "The allowlist and the reference-list version")
def cmd_domains_list(a, state: State):
    return Result({"allowed": sorted(load_allowed(state.dir("domains"))),
                   "reference": DomainIndex(state.dir("domains")).info or None})


@command("domains suggest", "Unverified domains read by OCR, ranked by how many held images they would release",
         args=[Arg("--limit", "at most N domains", type=int, default=30)],
         details="Candidates for `domains allow`. Check each one is a real site you trust before allowing it; "
                 "an OCR misread of a lookalike would be vouched for too.")
def cmd_domains_suggest(a, state: State):
    con = state.connect()
    dom = DomainIndex(state.dir("domains"))
    agg: dict[str, dict] = {}
    for rec in _records(con):
        for u in rec["entities"]["urls"]:
            if u["verified_by"] != "none" or u.get("flag"):
                continue
            host = u["url"].split("://", 1)[-1].split("/", 1)[0]
            reg = (dom.registrable(host) if dom.available else ".".join(host.split(".")[-2:])) or host
            e = agg.setdefault(reg, {"domain": reg, "urls": 0, "notes": set(), "held": set(), "examples": []})
            e["urls"] += 1
            e["notes"].add(rec["id"])
            if rec.get("source_state") == "held":
                e["held"].add(rec["id"])
            if len(e["examples"]) < 3 and u["raw"] not in e["examples"]:
                e["examples"].append(redact(u["raw"])[0])
    rows = sorted(agg.values(), key=lambda e: (-len(e["held"]), -len(e["notes"]), e["domain"]))[:a.limit]
    return Result({"suggestions": [{"domain": e["domain"], "urls": e["urls"], "notes": len(e["notes"]),
                                    "held_images": len(e["held"]), "examples": e["examples"]} for e in rows]})


# ---------------------------------------------------------------- panels (phase 7)
from . import panels as pv  # noqa: E402


@command("panel", "Render one read-only panvim panel (counts, reasons, names; never OCR text)",
         args=[Arg("view", "home | class | concepts | groups | audit | quarantine | notes | results | set | set-row | inbox | path | image"),
               Arg("id", "for path/image: an item id / prefix / note filename, or a group id", required=False),
               Arg("--category", "notes view: only this category")],
         details="What panvim runs on its timer. Reads the index only, never extraction or Laya. "
                 "`panel set QUERY [--category C]` chooses what the results view shows (a UI setting in "
                 "<state>/results.json, not data). `panel path ID` / `panel image ID` print paths.")
def cmd_panel(a, state: State):
    if not state.exists:
        return Result({"_text": "SekerinShotto — not initialised\n\nRun: sekerinshotto ingest <folder> --content <vault folder> --commit\n"})
    con = state.connect()
    content = _content_root(state, None, required=False)
    if a.view == "inbox":
        state.ensure()
        return Result({"_text": str(state.dir("inbox")) + "\n"})
    if a.view == "set-row":
        if not a.id:
            raise ToolError("panel set-row needs a row value")
        q = pv.set_row(state.root, state.connect(), a.id)
        return Result({"_text": f"results: {q}\n"})
    if a.view == "set":
        q = pv.set_query(state.root, a.id, a.category)
        return Result({"_text": f"results: {q['query'] or '*'}{' in ' + q['category'] if q['category'] else ''}\n"})
    if a.view in ("path", "image"):
        if not a.id:
            raise ToolError(f"panel {a.view} needs an id")
        if a.view == "path" and a.id.startswith("grp-"):
            return Result({"_text": str(content / "groups" / f"{a.id}.md") + "\n"})
        if a.view == "path" and a.id.startswith("seq-"):
            return Result({"_text": str(content / "sequences" / f"{a.id}.md") + "\n"})
        iid = resolve_id(con, a.id)
        r = con.execute("SELECT note_path, record FROM items WHERE id=?", (iid,)).fetchone()
        if a.view == "path":
            return Result({"_text": str(content / r["note_path"]) + "\n"})
        rec = json.loads(r["record"])
        p = rec.get("stored_path") or rec.get("source_path")
        if not p or not Path(p).exists():
            raise ToolError(f"no image on disk for {iid[:15]} ({rec.get('source_state')})")
        return Result({"_text": p + "\n"})
    return Result({"_text": pv.render(a.view, con, content, state.root, a.category)})


@command("panels install", "Create or refresh SekerinShotto's panvim panels and their key maps",
         writes=True,
         details="Plan: lists the 7 panels (ss visible; ss-class, ss-concepts, ss-groups, ss-audit, "
                 "ss-quarantine, ss-notes hidden, reached from ss). Commit: runs `panvim new` for missing "
                 "panels, (re)writes their keys.tsv and syntax.tsv, then runs `panvim audit`. Needs panvim and "
                 "sekerinshotto on PATH.")
def cmd_panels_install(a, state: State):
    return Result(pv.install(a.commit))


# ---------------------------------------------------------------- Laya query layer (phase 8)
from . import laya_layer as ly  # noqa: E402


def _cache(con):
    con.execute("""CREATE TABLE IF NOT EXISTS laya_cache (id TEXT NOT NULL, qhash TEXT NOT NULL,
                   model_rev TEXT NOT NULL, answer TEXT NOT NULL, at TEXT NOT NULL,
                   PRIMARY KEY (id, qhash, model_rev))""")


@command("ask", "Ask Laya one typed question across notes, locally; returns ranked suggestions (redacted)",
         args=[Arg("query", "words that must appear, to pick candidates (optional)", required=False),
               Arg("--question", 'JSON: {"type":"noul","instructions":"Is this an event I can register for?"}',
                   required=True),
               Arg("--checkpoint", "typed-decisions (default, most accurate) | english | multilingual",
                   default=ly.DEFAULT_CHECKPOINT),
               Arg("--max-candidates", "notes Laya reads at most", type=int, default=300),
               Arg("--top", "results returned", type=int, default=20),
               Arg("--min", "noul: minimum P(yes); choice: minimum confidence", type=float, default=0.5),
               Arg("--choice", "choice questions: only notes whose answer is this option")] + _FILTER_ARGS,
         details="Candidates come from the query and filters (FTS5 + index), never from Laya. Each candidate "
                 "is given to Laya as structured facts first, then 600 chars of its text; answers are cached "
                 "per (note, question, model revision). Read-only: nothing is tagged or moved. Answers are "
                 "suggestions (79 % topic accuracy on the sample); act on them with `tag --quote`.")
def cmd_ask(a, state: State):
    import time
    q = ly.parse_question(a.question)
    con = state.connect()
    _cache(con)
    where, params = _filters(a)
    fq = _fts_query(a.query or "")
    if fq:
        ids = [r[0] for r in con.execute(f"SELECT i.id FROM text_fts JOIN items i ON i.id=text_fts.id "
                                         f"WHERE text_fts MATCH ? AND {where} ORDER BY bm25(text_fts) LIMIT ?",
                                         [fq, *params, a.max_candidates])]
    else:
        ids = [r[0] for r in con.execute(f"SELECT i.id FROM items i WHERE {where} ORDER BY i.captured_at DESC "
                                         f"LIMIT ?", [*params, a.max_candidates])]
    rev, qh = ly.model_rev(a.checkpoint), ly.qhash(q)
    cached = {r[0]: json.loads(r[1]) for r in con.execute(
        f"SELECT id, answer FROM laya_cache WHERE qhash=? AND model_rev=? AND id IN ({','.join('?' * len(ids))})",
        [qh, rev, *ids])} if ids else {}
    todo = [i for i in ids if i not in cached]
    t0, load_s = time.perf_counter(), 0.0
    if todo:
        agent = ly.load_agent(a.checkpoint)
        load_s = time.perf_counter() - t0
        for iid in todo:
            rec = json.loads(con.execute("SELECT record FROM items WHERE id=?", (iid,)).fetchone()[0])
            ans = agent.predict(ly.build_state(rec, _text(con, iid)), {"q": q})["answers"]["q"]
            cached[iid] = ans
            con.execute("INSERT OR REPLACE INTO laya_cache VALUES (?,?,?,?,?)",
                        (iid, qh, rev, json.dumps(ans), now_iso()))
        con.commit()
    elapsed = time.perf_counter() - t0
    scored = []
    for iid in ids:
        val, choice = ly.value_of(cached[iid])
        if val < a.min or (a.choice and choice != a.choice):
            continue
        scored.append((val, iid, choice))
    scored.sort(key=lambda t: (-t[0], t[1]))
    reds: list[int] = []
    results = []
    for val, iid, choice in scored[:a.top]:
        r = con.execute("SELECT i.*, substr(f.text,1,200) AS snip FROM items i JOIN text_fts f ON f.id=i.id "
                        "WHERE i.id=?", (iid,)).fetchone()
        results.append({**_hit(r, r["snip"], reds), "answer": choice if choice is not None else round(val, 4),
                        "confidence": round(float(cached[iid].get("confidence", val)), 4)})
    return Result({"question": q, "checkpoint": a.checkpoint, "model_rev": rev, "candidates": len(ids),
                   "answered_now": len(todo), "from_cache": len(ids) - len(todo), "matched": len(scored),
                   "results": results, "redactions": sum(reds),
                   "timing": {"load_s": round(load_s, 1), "total_s": round(elapsed, 1)},
                   "note": "suggestions, not decisions: act with `tag <id> --category ... --quote ...`"})



def _copies_count(con, st: str) -> int:
    return sum(1 for rec in _records(con) for c in rec.get("copies") or [] if c.get("state") == st)


def _quarantine_copies(state: State, con, journal, batch_id: str, only: set[str] | None) -> set[str]:
    """Copies of an image that has left its source are redundant: an identical file now sits in
    quarantine, held or attachments. They go to quarantine on the same 7-day clock."""
    changed = set()
    for rec in _records(con, "source_state != 'present'"):
        if only is not None and rec["id"] not in only:
            continue
        cs, touched = rec.get("copies") or [], False
        for k, c in enumerate(cs):
            src = Path(c["path"])
            if c.get("state") != "present" or not src.exists():
                continue
            dst = state.root / "quarantine" / batch_id / f"{rec['id'].split(':')[1][:8]}-copy{k + 1}-{src.name}"
            try:
                lc._move(src, dst)
            except ToolError:
                continue
            now = now_iso()
            cs[k] = {**c, "state": "quarantined", "stored_path": str(dst), "quarantined_at": now,
                     "purge_after": lc.plus_seconds(now, lc.QUARANTINE_SECONDS)}
            journal.append(op="quarantine_copy", id=rec["id"], path=str(dst), from_path=str(src), batch_id=batch_id)
            touched = True
        if touched:
            _save(con, rec, copies=cs)
            changed.add(rec["id"])
    return changed


# ---------------------------------------------------------------- config
from .state import DEFAULT_STATE, config_path, configured_state, resolve_state  # noqa: E402


@command("config show", "Which state folder is active, and why")
def cmd_config_show(a, state: State):
    env = os.environ.get("SEKERINSHOTTO_STATE")
    source = "--state" if a.state else "SEKERINSHOTTO_STATE" if env else "config" if configured_state() else "default"
    return Result({"active_state": str(state.root), "source": source, "config_file": str(config_path()),
                   "configured_state": configured_state(), "default_state": str(Path(DEFAULT_STATE).expanduser()),
                   "initialised": state.exists, "content_root": str(state.bound_content() or "") or None})


@command("config use-state", "Make a state folder the default for every command and panel",
         args=[Arg("path", "the state folder to use (it may not exist yet)")],
         writes=True,
         details="Writes ~/.config/sekerinshotto/config.json. Panels run without --state, so this is how they "
                 "follow a state folder other than the default. --state and $SEKERINSHOTTO_STATE still win.")
def cmd_config_use_state(a, state: State):
    target = resolve_state(a.path)                          # same synced-folder guard as everywhere else
    info = {"path": str(target), "exists": (target / "index.sqlite").exists(), "previous": configured_state()}
    if not a.commit:
        return Result({"committed": False, **info})
    f = config_path()
    f.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(f.read_text()) if f.exists() else {}
    cfg["state"] = str(target)
    f.write_text(json.dumps(cfg, indent=1) + "\n")
    return Result({"committed": True, **info, "config_file": str(f)})



# ---------------------------------------------------------------- add (drag-and-drop into panvim)
from .extract import IMAGE_EXTS  # noqa: E402


def _expand(paths: list[str]) -> tuple[list[Path], list[str]]:
    """Dropped paths (a terminal pastes them shell-escaped; argv already unescaped them) -> image files."""
    files, skipped = [], []
    for raw in paths:
        p = Path(raw.strip().strip("'\"")).expanduser()
        if p.is_dir():
            files += [f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix.lower() in IMAGE_EXTS
                      and not f.name.startswith(".")]
        elif p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            files.append(p)
        else:
            skipped.append(raw)
    return files, skipped


@command("add", "Add photos: copy (or move) images or folders into the inbox, then extract them",
         args=[Arg("paths", "image files or folders; several are fine (drag them onto the prompt)", many=True),
               Arg("--move", "move instead of copy (the originals leave where they were)", flag=True),
               Arg("--content", "content root, if the state folder has no binding yet")],
         writes=True,
         details="Made for the panel's `A` key: dropping files on a terminal pastes their paths, which this "
                 "takes. Plan: lists what would be added. Commit: copies (or --move) them into <state>/inbox, "
                 "keeping the filename (Android names carry the app and capture time), then runs ingest on "
                 "the inbox. Images already known by hash are skipped by ingest as usual.")
def cmd_add(a, state: State):
    paths = list(a.paths)
    files, skipped = _expand(paths)
    if not files:
        raise ToolError(f"no images found in {', '.join(paths)[:200]} (supported: {', '.join(sorted(IMAGE_EXTS))})")
    content = _content_root(state, a.content, required=True)     # before touching any file
    inbox = state.dir("inbox")
    plan = {"files": len(files), "mode": "move" if a.move else "copy", "inbox": str(inbox), "content_root": str(content),
            "skipped_not_images": skipped, "items": [f.name for f in files[:50]]}
    if not a.commit:
        return Result({"committed": False, **plan},
                      human=f"would {plan['mode']} {len(files)} image(s) into the inbox and extract them"
                            + (f"; skipping {len(skipped)} non-image path(s)" if skipped else "") + "\n"
                            + "".join(f"  {n}\n" for n in plan["items"]))
    state.ensure()
    placed, ids = 0, []
    for f in files:
        ids.append(sha256_file(f))
        dst = inbox / f.name
        if dst.exists():
            if sha256_file(dst) == sha256_file(f):
                continue                                  # already in the inbox
            dst = inbox / f"{f.stem}-{sha256_file(f).split(':')[1][:6]}{f.suffix}"
        (shutil.move if a.move else shutil.copy2)(str(f), str(dst))
        placed += 1
    a.src, a.limit, a.cleanup = str(inbox), None, False
    a.workers = getattr(a, "workers", None) or 4
    res = cmd_ingest(a, state)
    d = res.data
    con = state.connect()
    notes = [dict(r) for r in con.execute(
        f"SELECT substr(id,8,8) AS id8, category, note_path, source_state FROM items WHERE id IN "
        f"({','.join('?' * len(ids))})", ids)]
    lines = [f"added {placed} image(s); extracted {d.get('written', 0)} note(s)"
             + (f" · {len(d.get('failed') or [])} failed" if d.get("failed") else ""), f"vault: {content}"]
    lines += [f"  {n['id8']}  {n['category']:<13} {n['note_path']}" for n in notes]
    lines += ["", "the copies wait in the inbox until cleanup (C) routes them"]
    return Result({"committed": True, "placed": placed, **plan, "notes": notes, "ingest": d},
                  violation=res.violation, human="\n".join(lines) + "\n")


# ---------------------------------------------------------------- drop zone
@command("dropzone", "Open the inbox in Finder and auto-extract whatever lands in it, until you type q",
         args=[Arg("--interval", "seconds between inbox checks", type=int, default=2),
               Arg("--no-finder", "do not open the Finder window", flag=True),
               Arg("--ask", "ask first (the panel's A key): yes starts; dropping photos adds them and starts",
                   flag=True),
               Arg("--keep-in-inbox", "do not route images after extraction (leave them for cleanup)", flag=True)],
         writes=True,
         details="Plan: says what it will do. Commit: opens <state>/inbox in Finder (frontmost), then loops: "
                 "new files in the inbox are extracted; a line of paths typed or dropped onto this terminal is "
                 "copied into the inbox and extracted; each new note is printed. `q` + Enter stops. Finder "
                 "MOVES files dragged between folders on the same disk; hold Option to copy.")
def cmd_dropzone(a, state: State):
    import select
    import shlex
    import subprocess
    import sys
    import time
    content = _content_root(state, None, required=True)
    inbox = state.dir("inbox")
    state.ensure()
    if not a.no_finder:
        # Opening a folder changes nothing, so the plan step does it right away: the window is there
        # before the "type yes" question, and drops made now simply wait in the inbox.
        subprocess.run(["open", str(inbox)], check=False)             # Finder comes to the front
    first_drop: list[Path] = []
    if a.ask and not a.commit:
        # The panel's A key. The first answer is the consent: `yes`, or photos dropped onto this pane
        # (a terminal pastes their paths) -- dropping a photo here is the natural first move, and it
        # used to land in a yes/no prompt as a "no".
        from . import contract as _c
        _c.OUT.write(f"Finder is open on the inbox: {inbox}\n\n"
                         "Drop photos onto this pane (then Enter) to add them and start the drop zone,\n"
                         "or type yes + Enter to start it empty. Anything else cancels. If typing does\n"
                         "nothing, press i in this pane first.\n\n> ")
        _c.OUT.flush()
        answer = sys.stdin.readline().strip()
        if answer.lower() != "yes":
            try:
                first_drop, skipped = _expand(shlex.split(answer)) if answer else ([], [])
            except ValueError:
                first_drop, skipped = [], [answer]
            if not first_drop:
                _c.OUT.write("cancelled" + (f" (no images in: {answer[:80]})" if answer else "") + "\n")
                _c.OUT.flush()
                return Result({"committed": False, "cancelled": True}, human="")
        a.commit = True
    if not a.commit:
        return Result({"committed": False, "inbox": str(inbox), "content_root": str(content)},
                      human=f"Finder is open on the inbox: {inbox}\n"
                            "Type yes + Enter to start auto-extracting whatever lands there or is dragged onto\n"
                            "this pane (q stops it). Photos dropped before that wait in the inbox; I extracts them.\n"
                            "If typing does nothing, press i in this pane first.\n")
    from . import contract as _c2
    say = lambda m: (_c2.OUT.write(m + "\n"), _c2.OUT.flush())
    say(f"DROP ZONE · {inbox}")
    say("  drop photos into the Finder window, or drag them onto this pane (then Enter)")
    say("  Finder MOVES files between folders on the same disk: hold Option while dropping to copy")
    say("  q + Enter stops\n")
    added_total, seen = 0, set()

    def extract(label: str) -> None:
        nonlocal added_total
        ns = argparse.Namespace(src=str(inbox), content=None, limit=None, workers=4, cleanup=False, commit=True,
                                json=False, state=None)
        con = state.connect()
        before = {r[0] for r in con.execute("SELECT id FROM items")}
        res = cmd_ingest(ns, state)
        new = [dict(r) for r in state.connect().execute(
            "SELECT substr(id,8,8) AS id8, category, note_path, id FROM items")]
        fresh = [n for n in new if n["id"] not in before]
        added_total += len(fresh)
        routed = {}
        if fresh and not a.keep_in_inbox:
            with state.lock():
                r = run_cleanup(state, state.connect(), content, True, only={n["id"] for n in fresh})
            routed = {row[0]: row[1] for row in state.connect().execute(
                f"SELECT id, source_state FROM items WHERE id IN ({','.join('?' * len(fresh))})",
                [n["id"] for n in fresh])}
        for n in fresh:
            where = {"quarantined": "→ quarantine (7 days)", "attached": "→ kept (visual)", "held": "→ held",
                     "present": ""}.get(routed.get(n["id"], "present"), "")
            say(f"  + {n['id8']}  {n['category']:<13} {n['note_path']}  {where}")
        if not fresh and res.data.get("planned"):
            say(f"  {label}: re-extracted {res.data['planned']}")

    def pending() -> list[Path]:
        return [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")
                and p.suffix.lower() in IMAGE_EXTS and (p.name, p.stat().st_mtime) not in seen]

    for p in pending():                                        # what is already there counts as seen
        seen.add((p.name, p.stat().st_mtime))
    for f in first_drop:                                       # photos dropped at the question
        if not (inbox / f.name).exists():
            shutil.copy2(f, inbox / f.name)
    try:
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], max(1, a.interval))
            if ready:
                line = sys.stdin.readline()
                if not line or line.strip().lower() in ("q", "quit", "exit"):
                    break
                try:
                    files, skipped = _expand(shlex.split(line))
                except ValueError:
                    files, skipped = [], [line.strip()]
                for f in files:
                    dst = inbox / f.name
                    if not dst.exists():
                        shutil.copy2(f, dst)
                if skipped:
                    say(f"  skipped (not images): {', '.join(skipped)[:120]}")
            new = pending()
            if new:
                time.sleep(0.5)                                # let Finder finish writing
                for p in new:
                    seen.add((p.name, p.stat().st_mtime))
                say(f"  {len(new)} new file(s) — extracting…")
                extract("inbox")
    except KeyboardInterrupt:
        pass
    say(f"\ndrop zone closed · {added_total} new note(s)")
    return Result({"committed": True, "added": added_total}, human="")


# ---------------------------------------------------------------- schedule (LaunchAgents)
import plistlib  # noqa: E402

# Overridable so tests never read or touch the real LaunchAgents of the machine they run on.
LAUNCH_DIR = Path(os.environ.get("SEKERINSHOTTO_LAUNCH_DIR") or Path.home() / "Library" / "LaunchAgents")
JOBS = {
    "watch": {"label": "com.sekerinshotto.watch", "summary": "autoadd photos from a folder when it changes (opt-in)",
              "args": ["autoadd"], "when": {}, "opt_in": True},
    "purge": {"label": "com.sekerinshotto.purge", "summary": "purge due quarantined images daily at 03:15",
              "args": ["purge", "--commit", "--json"], "when": {"StartCalendarInterval": {"Hour": 3, "Minute": 15}}},
}


def _bin() -> str:
    found = shutil.which("sekerinshotto")
    if not found:
        raise ToolError("sekerinshotto is not on PATH; install it first: uv tool install --editable <repo>")
    return str(Path(found).resolve())


def _plist(job: str, state: State, extra: dict | None = None, folder: str | None = None) -> dict:
    spec = JOBS[job]
    logs = state.dir("logs")
    args = [*spec["args"]]
    if job == "watch":
        args += [folder, "--commit", "--json"]
        extra = {**(extra or {}), "WatchPaths": [folder], "ThrottleInterval": 30}
    return {"Label": spec["label"], "ProgramArguments": [_bin(), *args],
            "StandardOutPath": str(logs / f"{job}.log"), "StandardErrorPath": str(logs / f"{job}.err"),
            "RunAtLoad": False, **spec["when"], **(extra or {})}


def _launchctl(*args) -> tuple[int, str]:
    import subprocess
    p = subprocess.run(["launchctl", *args], capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _loaded(label: str) -> bool:
    return _launchctl("print", f"gui/{os.getuid()}/{label}")[0] == 0


@command("schedule show", "Scheduled jobs (macOS LaunchAgents): installed? loaded? last log line")
def cmd_schedule_show(a, state: State):
    out = {}
    for job, spec in JOBS.items():
        f = LAUNCH_DIR / f"{spec['label']}.plist"
        log = state.dir("logs") / f"{job}.log"
        last = log.read_text().strip().splitlines()[-1][:200] if log.exists() and log.read_text().strip() else None
        out[job] = {"label": spec["label"], "summary": spec["summary"], "installed": f.exists(),
                    "loaded": _loaded(spec["label"]), "plist": str(f), "last_log": last}
    return Result({"jobs": out, "state": str(state.root)})


@command("schedule install", "Install the scheduled jobs as LaunchAgents (purge daily at 03:15)",
         args=[Arg("--job", "only this job", default=None),
               Arg("--folder", "watch job: the folder to autoadd from (e.g. ~/Downloads)")],
         writes=True,
         details="Writes ~/Library/LaunchAgents/com.sekerinshotto.<job>.plist and loads it with launchctl. "
                 "Jobs run without --state, so they follow `config use-state`. Output goes to <state>/logs/. "
                 "purge only deletes quarantined images whose 7 days are up, inside <state>/quarantine/.")
def cmd_schedule_install(a, state: State):
    jobs = [a.job] if a.job else [j for j, sp in JOBS.items() if not sp.get("opt_in")]
    for j in jobs:
        if j not in JOBS:
            raise ToolError(f"unknown job {j!r}; valid: {', '.join(JOBS)}")
    folder = str(Path(a.folder).expanduser().resolve()) if a.folder else None
    if "watch" in jobs and not folder:
        raise ToolError("the watch job needs --folder (the folder photos arrive in, e.g. ~/Downloads)")
    plan = [{"job": j, "plist": str(LAUNCH_DIR / f"{JOBS[j]['label']}.plist"),
             "runs": " ".join(_plist(j, state, folder=folder)["ProgramArguments"]),
             "when": JOBS[j]["when"] or {"WatchPaths": [folder]}} for j in jobs]
    if not a.commit:
        return Result({"committed": False, "would_install": plan})
    LAUNCH_DIR.mkdir(parents=True, exist_ok=True)
    state.dir("logs").mkdir(parents=True, exist_ok=True)
    done = []
    for j in jobs:
        label, f = JOBS[j]["label"], LAUNCH_DIR / f"{JOBS[j]['label']}.plist"
        if _loaded(label):
            _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        f.write_bytes(plistlib.dumps(_plist(j, state, folder=folder)))
        code, msg = _launchctl("bootstrap", f"gui/{os.getuid()}", str(f))
        done.append({"job": j, "loaded": _loaded(label), "launchctl": msg or "ok"})
    return Result({"committed": True, "installed": done}, violation=not all(d["loaded"] for d in done))


@command("schedule remove", "Unload and delete the scheduled jobs", args=[Arg("--job", "only this job")],
         writes=True)
def cmd_schedule_remove(a, state: State):
    jobs = [a.job] if a.job else list(JOBS)
    present = [j for j in jobs if (LAUNCH_DIR / f"{JOBS[j]['label']}.plist").exists() or _loaded(JOBS[j]["label"])]
    if not a.commit:
        return Result({"committed": False, "would_remove": present})
    for j in present:
        label = JOBS[j]["label"]
        _launchctl("bootout", f"gui/{os.getuid()}/{label}")
        (LAUNCH_DIR / f"{label}.plist").unlink(missing_ok=True)
    return Result({"committed": True, "removed": present})



# ---------------------------------------------------------------- autoadd (phone -> Mac)
def _download_tag(p: Path) -> str | None:
    """The app macOS recorded as having downloaded the file (com.apple.quarantine), e.g. 'Safari'.
    Browsers tag what they save from the web; Taildrop (Tailscale) and local copies do not."""
    import subprocess
    r = subprocess.run(["xattr", "-p", "com.apple.quarantine", str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    parts = r.stdout.strip().split(";")
    return parts[2] if len(parts) > 2 and parts[2] else "unknown"


def _watch_since(state: State) -> float | None:
    """When the watch job was installed: untagged images older than this are not the watcher's business."""
    f = LAUNCH_DIR / f"{JOBS['watch']['label']}.plist"
    return f.stat().st_mtime if f.exists() else None


def _photo_named(p: Path) -> bool:
    """Only files named the way phones and Macs name photos/screenshots: a random downloaded image
    (a logo, an avatar) is never swept up."""
    from .extract import parse_filename
    return bool(parse_filename(p.name))


@command("autoadd", "Extract photos/screenshots that arrived in a folder, and route the originals",
         args=[Arg("folder", "where photos arrive (e.g. ~/Downloads, where Taildrop saves)"),
               Arg("--settle", "ignore files modified in the last N seconds (still being written)", type=int,
                   default=5),
               Arg("--since", "also take untagged images newer than this Unix time (default: watch install time)",
                   type=float)],
         writes=True,
         details="Plan: lists the files it would take. Commit: extracts them in place, then routes each original "
                 "like cleanup (quarantine 7 days, attachments, or held) -- the originals leave the folder. Only "
                 "files named like photos/screenshots are touched (Android Screenshot_, WhatsApp Image, macOS "
                 "Screenshot … at …, IMG_/PXL_/VID_/MVIMG_), plus images with a generic name (a phone share's "
                 "'image.png') that carry no browser download tag (com.apple.quarantine) and arrived after the "
                 "watch job was installed. Images already known by hash are skipped. The watch "
                 "LaunchAgent runs this when the folder changes.")
def cmd_autoadd(a, state: State):
    import time
    folder = Path(a.folder).expanduser().resolve()
    if not folder.is_dir():
        raise ToolError(f"{folder} is not a folder")
    content = _content_root(state, None, required=True)
    since = a.since if getattr(a, "since", None) is not None else _watch_since(state)

    def wanted(p: Path) -> bool:
        if _photo_named(p):
            return True
        # Generic names ("image.png") from a phone share: take them only if no browser tagged them as a web
        # download AND they arrived after the watcher was switched on, so old files are never swept up.
        return since is not None and p.stat().st_mtime >= since and _download_tag(p) is None

    def photo_files():
        return [p for p in sorted(folder.iterdir()) if p.is_file() and p.suffix.lower() in IMAGE_EXTS
                and not p.name.startswith(".") and wanted(p)]

    # A Taildrop/AirDrop batch keeps writing for a moment after the watch trigger fires. Files still
    # settling are waited out (up to 30 s) instead of skipped, or the last photos of a batch would sit in
    # the folder until the next unrelated change there.
    deadline = time.time() + (30 if a.commit else 0)
    while True:
        now = time.time()
        files = photo_files()
        settling = [p for p in files if now - p.stat().st_mtime < a.settle]
        if not settling or now >= deadline:
            break
        time.sleep(min(a.settle, max(0.5, deadline - now)))
    cands = [p for p in files if time.time() - p.stat().st_mtime >= a.settle]
    con = state.connect()
    known = {r[0] for r in con.execute("SELECT id FROM items")}
    new = [p for p in cands if sha256_file(p) not in known]
    plan = {"folder": str(folder), "photo_named": len(cands), "new": len(new), "already_known": len(cands) - len(new),
            "items": [p.name for p in new[:50]]}
    if not a.commit:
        return Result({"committed": False, **plan},
                      human=f"{folder}: {len(new)} new photo(s) would be extracted and their originals routed "
                            f"(quarantine 7 days / kept / held); {len(cands) - len(new)} already known\n")
    if not new:
        return Result({"committed": True, **plan, "written": 0})
    ns = argparse.Namespace(src=str(folder), files=new, content=None, limit=None, workers=4, cleanup=False,
                            commit=True, json=False, state=None)
    res = cmd_ingest(ns, state)
    ids = {sha256_file(p) for p in new if p.exists()}
    with state.lock():
        routed = run_cleanup(state, state.connect(), content, True, only=ids)
    return Result({"committed": True, **plan, "written": res.data.get("written"), "routed": routed["moved"]},
                  violation=res.violation)


# ---------------------------------------------------------------- panel quick fixes
@command("domains allow-item", "Allow the domains holding one image back (its unverified OCR URLs)",
         args=[Arg("id", "item id, id prefix (>= 8 hex) or note filename")],
         writes=True,
         details="The audit panel's D key. Finds the item's URLs still verified_by: none (and not flagged as cut "
                 "off), allows their registrable domains, and re-verifies, which releases the image if nothing "
                 "else holds it. Same rules and refusals as `domains allow`.")
def cmd_domains_allow_item(a, state: State):
    con = state.connect()
    iid = resolve_id(con, a.id)
    rec = json.loads(con.execute("SELECT record FROM items WHERE id=?", (iid,)).fetchone()[0])
    hosts = sorted({u["url"].split("://", 1)[-1].split("/", 1)[0] for u in rec["entities"]["urls"]
                    if u["verified_by"] == "none" and not u.get("flag")})
    if not hosts:
        raise ToolError(f"{iid[:15]} has no unverified URL to allow (held for: {rec.get('hold_reason') or 'nothing'})")
    a.domains = ",".join(hosts)
    return cmd_domains_allow(a, state)


def _stopterms_file(state: State) -> Path:
    return state.root / "stopterms.txt"


@command("terms list", "Words kept out of key terms (<state>/stopterms.txt)")
def cmd_terms_list(a, state: State):
    from .terms import load_stopterms
    return Result({"hidden": sorted(load_stopterms(state.root)), "file": str(_stopterms_file(state))})


def _terms_edit(a, state: State, add: bool) -> Result:
    from .terms import load_stopterms
    word = a.term.strip().lower()
    if not re.fullmatch(r"[a-z][a-z'-]{2,}", word):
        raise ToolError(f"{a.term!r} is not a single word")
    current = load_stopterms(state.root)
    changed = (word not in current) if add else (word in current)
    if not a.commit:
        return Result({"committed": False, "term": word, "would_change": changed,
                       "action": "hide" if add else "unhide"})
    if changed:
        new = (current | {word}) if add else (current - {word})
        _stopterms_file(state).write_text("# words that must never become key terms (names the redactor misses)\n"
                                          + "".join(w + "\n" for w in sorted(new)))
    content = _content_root(state, None, required=True)
    batch_id = now_iso().replace(":", "-") + ("-hide" if add else "-unhide")
    with state.lock():
        rep = apply_organization(state, state.connect(), content, state.journal(batch_id),
                                 state.dir("batches") / f"{batch_id}.jsonl", batch_id, now_iso())
        state.connect().commit()
    return Result({"committed": True, "term": word, "changed": changed, "notes_rewritten": rep["notes_written"]})


@command("terms hide", "Keep a word out of key terms everywhere (a name the redactor missed)",
         args=[Arg("term", "the word")], writes=True,
         details="Adds the word to <state>/stopterms.txt and re-organizes, so it leaves every note's terms and the "
                 "concepts panel. The concepts panel's X key.")
def cmd_terms_hide(a, state: State):
    return _terms_edit(a, state, True)


@command("terms unhide", "Allow a hidden word back into key terms", args=[Arg("term", "the word")], writes=True)
def cmd_terms_unhide(a, state: State):
    return _terms_edit(a, state, False)


# ---------------------------------------------------------------- rule suggestions
def _rule_suggestions(state: State, con, min_tags: int) -> list[dict]:
    from collections import Counter, defaultdict
    from .rules import classify, load as load_rules
    rules, _ = load_rules(state.root)
    rows = [(json.loads(r["record"]), r["category"], r["decided_by"]) for r in con.execute(
        "SELECT record, category, decided_by FROM items WHERE record IS NOT NULL")]
    tagged = [(rec, cat) for rec, cat, by in rows if by in ("llm", "user", "laya")]
    by_domain, by_app = defaultdict(Counter), defaultdict(Counter)
    for rec, cat in tagged:
        for d in rec["entities"]["domains"]:
            by_domain[d][cat] += 1
        if rec.get("source_app"):
            by_app[rec["source_app"]][cat] += 1
    app_total = Counter(rec.get("source_app") for rec, _, _ in rows)
    out = []
    for dom, cats in sorted(by_domain.items()):
        (cat, n), = cats.most_common(1)
        if n < min_tags or len(cats) > 1:
            continue                                     # too few tags, or callers disagree
        if classify(rules, None, set(), [dom], "")[0] == cat:
            continue                                     # the rules already say so
        out.append({"kind": "domain", "value": dom, "category": cat, "tags": n,
                    "reason": f"{n} notes on {dom} tagged {cat}, none tagged otherwise"})
    for app, cats in sorted(by_app.items()):
        (cat, n), = cats.most_common(1)
        share = n / max(app_total[app], 1)
        if n < max(min_tags, 5) or len(cats) > 1 or share < 0.6:
            continue                                     # an app rule captures every note from that app:
                                                         # >= 5 consistent tags AND >= 60% of the app's notes
        if classify(rules, app, set(), [], "")[0] == cat:
            continue
        out.append({"kind": "app", "value": app, "category": cat, "tags": n,
                    "reason": f"{n} of {app_total[app]} notes from {app} tagged {cat} ({share:.0%})"})
    for i, sug in enumerate(out, 1):
        key = "domains" if sug["kind"] == "domain" else "apps"
        sug["n"] = i
        sug["toml"] = f'[[rule]]\ncategory = "{sug["category"]}"\nmode = "any"\n{key} = ["{sug["value"]}"]\n'
    return out


@command("rules suggest", "Suggest rules from categories the LLM or user set, so similar notes sort themselves",
         args=[Arg("--min", "minimum consistent tags", type=int, default=2),
               Arg("--apply", "comma-separated suggestion numbers to add to <state>/rules.toml")],
         writes=True,
         details="Domain rules need >= --min tags that all agree; app rules need >= 5 that agree AND cover >= 60% "
                 "of that app's notes, because an app rule captures every note from it. Plan: lists suggestions with "
                 "their TOML. Commit with --apply: inserts them at the top of <state>/rules.toml (created from the "
                 "built-in rules if absent) and re-organizes. Caller-set categories are never overridden.")
def cmd_rules_suggest(a, state: State):
    from importlib import resources
    con = state.connect()
    sug = _rule_suggestions(state, con, a.min)
    if not a.apply:
        human = "no suggestions yet: tag notes with `tag` (the LLM) first\n" if not sug else "".join(
            f"{s['n']}. {s['kind']} {s['value']} → {s['category']}  ({s['reason']})\n" for s in sug)
        return Result({"committed": False, "suggestions": sug}, human=human)
    try:
        pick = {int(x) for x in a.apply.split(",") if x.strip()}
    except ValueError:
        raise ToolError("--apply takes suggestion numbers, e.g. --apply 1,3")
    chosen = [s for s in sug if s["n"] in pick]
    if len(chosen) != len(pick):
        raise ToolError(f"unknown suggestion number(s); valid: {', '.join(str(s['n']) for s in sug) or 'none'}")
    f = state.root / "rules.toml"
    base = f.read_text() if f.exists() else resources.files(__package__).joinpath("rules_default.toml").read_text()
    added = "# added by `rules suggest` " + now_iso() + "\n" + "\n".join(s["toml"] for s in chosen) + "\n"
    if not a.commit:
        return Result({"committed": False, "would_add": added, "file": str(f)}, human=added)
    head, sep, rest = base.partition("[[rule]]")
    f.write_text(head + added + sep + rest)
    from .rules import load as load_rules
    load_rules(state.root)                                   # refuses a broken file (ToolError)
    content = _content_root(state, None, required=True)
    batch_id = now_iso().replace(":", "-") + "-rules"
    with state.lock():
        rep = apply_organization(state, con, content, state.journal(batch_id),
                                 state.dir("batches") / f"{batch_id}.jsonl", batch_id, now_iso())
        con.commit()
    return Result({"committed": True, "added": [s["toml"] for s in chosen], "file": str(f),
                   "notes_rewritten": rep["notes_written"], "by_category": rep["by_category"]})
