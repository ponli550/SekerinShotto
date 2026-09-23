"""Phase 1 commands: schema, ingest, status, reindex."""
from __future__ import annotations

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
         args=[Arg("src", "an image file or a folder (searched recursively)"),
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
        row = con.execute("SELECT extractor_version, note_path, source_state FROM items WHERE id=?",
                          (fid,)).fetchone()
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
            prior = con.execute("SELECT note_path FROM items WHERE id=?", (ex.id,)).fetchone()
            rel = prior["note_path"] if prior and prior["note_path"] else None
            _index(con, manifest_record(ex, batch_id, rel), ex.text, ingested)
            fresh.add(ex.id)
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
                   group_size, stored_path, purge_after, hold_reason, attempts, keep, confirmed_by)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET source_path=excluded.source_path,
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
                 1 if rec.get("keep") else 0, rec.get("confirmed_by")))
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
    org = organize(items, rules, content)
    targets = sorted(i for i in items if i in force or changed(items[i], org[i]) or not items[i]["_note_path"])
    moves = []
    for i in targets:
        old = items[i]["_note_path"]
        if old and not old.startswith(f"notes/{org[i]['category']}/"):
            moves.append({"id": i, "from": old, "to": f"notes/{org[i]['category']}/{Path(old).name}"})
    groups: dict[str, list[str]] = {}
    for i, o in org.items():
        if o["group"]:
            groups.setdefault(o["group"], []).append(i)
    prev_groups = {r["_prev"]["group"] for r in items.values() if r["_prev"]["group"]}
    summary = {"rules": rules_src, "notes_to_write": len(targets), "moves": len(moves),
               "by_category": _count(o["category"] for o in org.values()),
               "groups": len(groups), "in_groups": sum(len(v) for v in groups.values()),
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
            new_rel = f"notes/{o['category']}/{Path(old_rel).name}" if old_rel else note_relpath(ex, o["category"])
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
        rer = _rerender(state, con, content, journal, batch_id, set(moved))
        con.commit()
    finally:
        audit.close()
    _write_audit(state, con, content)
    held = con.execute("SELECT COUNT(*) FROM items WHERE source_state='held'").fetchone()[0]
    return {"committed": True, "batch_id": batch_id, "moved": _count(p["outcome"] for p in plan if p["id"] in moved),
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
    waiting = sorted(({"id": r["id"], "purge_after": r["purge_after"], "seconds_left": lc.seconds_left(r, now)}
                      for r in recs if not lc.is_due(r, now)), key=lambda x: x["seconds_left"])
    if not a.commit:
        return Result({"committed": False, "now": now, "due": len(due), "waiting": len(waiting),
                       "next": waiting[:10], "due_items": [{"id": r["id"], "purge_after": r["purge_after"]}
                                                           for r in due[:50]]})
    batch_id = now.replace(":", "-") + "-purge"
    purged, refused = [], []
    with state.lock():
        journal = state.journal(batch_id)
        for r in due:
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
    return Result({"committed": True, "now": now, "purged": len(purged), "refused": refused,
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
            _save(con, r, source_state="present", stored_path=None, quarantined_at=None, purge_after=None)
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
    return Result({"query": a.query or "", "total": total, "returned": len(hits), "offset": a.offset,
                   "redactions": sum(reds), "results": hits})


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
    return Result({"total": total, "returned": len(rows), "offset": a.offset,
                   "results": [_hit(r, r["snip"], reds) for r in rows], "redactions": sum(reds),
                   "categories": _count(r[0] for r in con.execute("SELECT category FROM items"))})


@command("show", "One item in full: text, URLs, QR codes, category, group, image state (redacted)",
         args=[Arg("id", "item id, id prefix (>= 8 hex) or note filename")])
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
    return Result({"id": iid, "note": r["note_path"], "text": text, "redactions": n,
                   "urls": urls, "qr": [redact_qr(q) for q in rec["entities"]["qr"]],
                   "domains": rec["entities"]["domains"],
                   "category": r["category"], "decided_by": r["decided_by"], "why": r["why"],
                   "group": r["group_id"], "rank": r["rank"], "group_size": r["group_size"],
                   "app": r["source_app"], "captured_at": r["captured_at"], "status": r["status"],
                   "source_state": r["source_state"], "purge_after": r["purge_after"],
                   "hold_reason": r["hold_reason"]})


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
