"""panvim panels: read-only text renderers and the installer.

Renderers read the index only (never OCR, never Laya) because panvim re-runs them on a
timer, unattended. They show counts, categories, terms, reasons and note names; never
OCR text or QR payloads, because a panel stays on screen (screen shares included).
Row lines start with two spaces and an id, matched by ROW_PATTERN.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from .contract import ToolError
from .state import now_iso, parse_iso

ROW_PATTERN = "^  (%S+)"
# panvim's term-side closes its pane when the command exits (it is meant for editors), so any command
# that just prints must be paged, or its output flashes and vanishes.
PAGER = " | less -R"
VIEWS = ("home", "class", "concepts", "groups", "audit", "quarantine", "notes", "results", "jobs")

_UI_NOISE = re.compile(r"(?i)^(follow|following|reply|replies|like|likes|share|send( message)?|more|see more|view|"
                       r"comment|comments|save|saved|details|print|back|next|done|ok|cancel|search|home|menu|"
                       r"type a message|ketik pesan|\W+)$")
_CLOCKISH = re.compile(r"^[\d:.%/\s-]+(am|pm)?$|^\d{1,2}:\d{2}")


def query_file(state_root: Path) -> Path:
    """Per state folder: a trial and a real state never share what the results board shows."""
    return state_root / "results.json"


def set_query(state_root: Path, query: str | None, category: str | None, state: str | None = None) -> dict:
    q = {"query": (query or "").strip(), "category": category or None, "state": state or None, "at": now_iso()}
    f = query_file(state_root)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(q) + "\n")
    return q


def content_only(rec: dict, text: str) -> tuple[list[str], int]:
    """Lines of the content area, without status/gesture-bar lines, clocks, and bare symbols."""
    lines = text.split("\n") if text else []
    top, bottom = rec.get("chrome_top_n") or 0, rec.get("chrome_bottom_n") or 0
    body = lines[top:len(lines) - bottom if bottom else None]
    keep = [l for l in body if l.strip() and not _CLOCKISH.match(l.strip()) and re.search(r"[A-Za-z]{2}", l)]
    return keep, len(lines) - len(keep)


def headline(rec: dict, text: str, words: list[str]) -> str:
    """The line worth reading: the first content line containing a query word, else the longest
    informative line among the first 15. Status bar, clocks and UI words are skipped. Redacted."""
    from .redact import redact
    lines = text.split("\n") if text else []
    top, bottom = rec.get("chrome_top_n") or 0, rec.get("chrome_bottom_n") or 0
    body = [l.strip() for l in lines[top:len(lines) - bottom if bottom else None]]
    good = [l for l in body if len(l) >= 12 and not _UI_NOISE.match(l) and not _CLOCKISH.match(l)]
    pick = next((l for l in good if any(w.lower() in l.lower() for w in words)), None) if words else None
    if pick is None and rec.get("terms"):
        pick = next((l for l in good if any(t in l.lower() for t in rec["terms"])), None)
    if pick is None:
        pick = max(good[:15], key=len, default=(body[0] if body else ""))
    return " ".join(redact(pick)[0].split())


STATES = ("present", "held", "attached", "quarantined", "purged")
LEFT_W = 46


def _bar(n: int, top: int, width: int = 14) -> str:
    return "▇" * max(1, round(width * n / top)) if n and top else ""


def _local(ts_utc: str) -> str:
    from datetime import timezone
    return parse_iso(ts_utc).replace(tzinfo=timezone.utc).astimezone().strftime("%a %d %b %H:%M")


def _home(con, content, state_root, now) -> list[str]:
    total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    groups = con.execute("SELECT COUNT(DISTINCT group_id) FROM items WHERE group_id IS NOT NULL").fetchone()[0]
    left = ["images · Enter lists them"]
    st = _q(con, "SELECT source_state, COUNT(*) FROM items GROUP BY 1 ORDER BY 2 DESC")
    top = max((n for _, n in st), default=0)
    left += [f"  {k:<12} {n:>4}  {_bar(n, top)}" for k, n in st]
    nxt = con.execute("SELECT MIN(purge_after) FROM items WHERE source_state='quarantined'").fetchone()[0]
    if nxt:
        left += ["", "next purge", f"  {countdown(int((parse_iso(nxt) - parse_iso(now)).total_seconds()))}",
                 f"  {_local(nxt)} local"]
    left += ["", "categories · Enter lists them"]
    cats = _q(con, "SELECT category, COUNT(*) FROM items GROUP BY 1 ORDER BY 2 DESC, 1")
    top = max((n for _, n in cats), default=0)
    left += [f"  {k:<14} {n:>3}  {_bar(n, top, 12)}" for k, n in cats]

    right = ["to do"]
    uncat = con.execute("SELECT COUNT(*) FROM items WHERE category='uncategorized'").fetchone()[0]
    held = con.execute("SELECT COUNT(*) FROM items WHERE source_state='held'").fetchone()[0]
    present = con.execute("SELECT COUNT(*) FROM items WHERE source_state='present'").fetchone()[0]
    todo = [(held, "held", "images held back · a = audit"),
            (uncat, "uncategorized", "notes no rule matched · the LLM can tag them"),
            (present, "present", "images not cleaned up yet · C = cleanup")]
    inbox = sum(1 for p in (state_root / "inbox").glob("*") if p.is_file() and not p.name.startswith("."))
    waiting = max(0, inbox - con.execute("SELECT COUNT(*) FROM items WHERE source_path LIKE ?",
                                         (str(state_root / "inbox") + "/%",)).fetchone()[0])
    todo.insert(0, (waiting, "inbox", "photos waiting · I = extract, A = drop zone"))
    right += [f"  {k:<14} {n:>3}  {why}" for n, k, why in todo if n] or ["  nothing to do · A = add photos"]
    right += ["", "recently added · n = all notes"]
    # ambient panel: app and key terms only, never OCR text (headlines live on the on-demand results board)
    for iid, cat, added, app, rec in _q(con, """SELECT substr(id,8,8), category, added_at, source_app, record
            FROM items ORDER BY added_at DESC, captured_at DESC LIMIT 8"""):
        terms = ", ".join((json.loads(rec).get("terms") or [])[:3])
        when = _local(added)[4:10] if added else ""
        right.append(f"  {iid} {when:<6} {cat[:9]:<9} {(app or '').split('.')[-1][:10]:<10} {terms}")
    c = Counter()
    for (rec,) in _q(con, "SELECT record FROM items WHERE record IS NOT NULL"):
        c.update(json.loads(rec).get("terms") or [])
    if c:
        right += ["", "top concepts · k = concepts"]
        terms = [t for t, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:12]]
        right += [f"  {terms[i]:<18}  {terms[i + 1] if i + 1 < len(terms) else ''}" for i in range(0, len(terms), 2)]
    seqs = sum(1 for (r,) in _q(con, "SELECT record FROM items") if json.loads(r).get("seq_part") == 1)
    right += ["", f"duplicate groups {groups} · scroll sequences {seqs} · g = groups"]

    where = f"state {_short(state_root)} · vault {_short(content) if content else '-'}"
    head = [f"SekerinShotto · {total} notes · {where}", ""]
    rows = max(len(left), len(right))
    left += [""] * (rows - len(left))
    right += [""] * (rows - len(right))
    return head + [f"{l:<{LEFT_W}}{r}".rstrip() for l, r in zip(left, right)]


def _short(p) -> str:
    try:
        return "~/" + str(Path(p).relative_to(Path.home()))
    except ValueError:
        return str(p)


def countdown(seconds: int) -> str:
    if seconds <= 0:
        return "due now"
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d {h:02d}h {m:02d}m {s:02d}s"


def _short_reason(reason: str) -> str:
    """'unverified URL: https://a.my/long/path, b.com' -> 'unverified URL: a.my, b.com' (the domain is what
    `domains allow` needs; a cut-off path is noise)."""
    head, _, rest = reason.partition(": ")
    if head != "unverified URL" or not rest:
        return reason
    hosts = [u.split("://", 1)[-1].split("/", 1)[0].lower() for u in rest.split(", ")]
    return f"{head}: {', '.join(dict.fromkeys(hosts))}"


def _q(con, sql, params=()):
    return con.execute(sql, params).fetchall()


def render(view: str, con, content: Path | None, state_root: Path, category: str | None = None) -> str:
    if view not in VIEWS:
        raise ToolError(f"unknown panel view {view!r}; valid: {', '.join(VIEWS)}")
    now = now_iso()
    total = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    if not total:
        return "SekerinShotto — no screenshots yet\n\nRun: sekerinshotto ingest <folder> --commit\n"
    out = []
    if view == "home":
        out = _home(con, content, state_root, now)
    elif view == "class":
        out += ["categories · rule vs caller decisions", ""]
        for cat, n, rules, callers in _q(con, """SELECT category, COUNT(*), SUM(decided_by='rule'),
                SUM(decided_by IN ('llm','user','laya')) FROM items GROUP BY 1 ORDER BY 2 DESC, 1"""):
            out.append(f"  {cat:<14} {n:>5}   rule {rules or 0:>4}   caller {callers or 0:>3}")
    elif view == "concepts":
        c = Counter()
        for (rec,) in _q(con, "SELECT record FROM items WHERE record IS NOT NULL"):
            c.update(json.loads(rec).get("terms") or [])
        out += ["key terms across notes (TF-IDF, in ≥ 2 notes)", ""]
        for term, n in sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[:80]:
            out.append(f"  {term:<22} {n:>3} notes")
    elif view == "groups":
        out += ["duplicate groups · rank 1 is the most complete copy", ""]
        for gid, n, cat in _q(con, """SELECT group_id, COUNT(*), MAX(CASE WHEN rank=1 THEN category END)
                FROM items WHERE group_id IS NOT NULL GROUP BY 1 ORDER BY 2 DESC, 1"""):
            best = con.execute("SELECT note_path FROM items WHERE group_id=? AND rank=1", (gid,)).fetchone()[0]
            out.append(f"  {gid:<13} {n:>2} copies  {cat:<12} best: {Path(best).stem if best else '-'}")
        seqs = Counter()
        for (rec,) in _q(con, "SELECT record FROM items WHERE record IS NOT NULL"):
            sid = json.loads(rec).get("sequence")
            if sid:
                seqs[sid] += 1
        if seqs:
            out += ["", "scroll sequences · stitched in page order"]
            out += [f"  {sid:<13} {n:>2} parts" for sid, n in sorted(seqs.items())]
        eps: dict[str, list[dict]] = {}
        for (rec,) in _q(con, "SELECT record FROM items WHERE record IS NOT NULL"):
            r = json.loads(rec)
            if r.get("episode"):
                eps.setdefault(r["episode"], []).append(r)
        if eps:
            out += ["", "episodes · taken together (a talk, a training) · name one: episode label <id> <name>"]
            for eid, rs in sorted(eps.items(), key=lambda kv: min(r.get("captured_at") or "" for r in kv[1])):
                label = rs[0].get("ep_label") or "-"
                when = min(r.get("captured_at") or "" for r in rs)[:16].replace("T", " ")
                out.append(f"  {eid:<13} {len(rs):>2} photos  {when}  {label}")
    elif view == "audit":
        out += ["held and kept images · reasons, never content", ""]
        for iid, st, reason, att, note in _q(con, """SELECT substr(id,8,8), source_state, hold_reason,
                attempts, note_path FROM items WHERE source_state IN ('held','attached')
                ORDER BY source_state DESC, hold_reason"""):
            why = _short_reason(reason) if reason else "visual or diagram, kept in the vault"
            why = why[:48]
            out.append(f"  {iid}  {st:<8} {why:<48} tries {att or 0}  {Path(note).stem if note else ''}")
    elif view == "quarantine":
        out += [f"quarantine · purged exactly 7 days after cleanup · now {now}", ""]
        for iid, after, note in _q(con, """SELECT substr(id,8,8), purge_after, note_path FROM items
                WHERE source_state='quarantined' ORDER BY purge_after, id"""):
            left = int((parse_iso(after) - parse_iso(now)).total_seconds())
            out.append(f"  {iid}  {countdown(left):<18} {after}  {Path(note).stem if note else ''}")
    elif view == "results":
        try:
            q = json.loads(query_file(state_root).read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            q = {"query": "", "category": None}
        words = re.findall(r"[\w'-]+", q.get("query") or "")
        where, params = ["i.record IS NOT NULL"], []
        if q.get("category"):
            where.append("i.category = ?"); params.append(q["category"])
        if q.get("state"):
            where.append("i.source_state = ?"); params.append(q["state"])
        if words:
            fts = " ".join('"' + w.replace('"', "") + '"' for w in words)
            rows = _q(con, f"""SELECT i.id, i.record, i.category, i.captured_at, f.text FROM text_fts f
                    JOIN items i ON i.id = f.id WHERE text_fts MATCH ? AND {' AND '.join(where)}
                    ORDER BY bm25(text_fts) LIMIT 200""", [fts, *params])
        else:
            rows = _q(con, f"""SELECT i.id, i.record, i.category, i.captured_at, f.text FROM items i
                    JOIN text_fts f ON f.id = i.id WHERE {' AND '.join(where)}
                    ORDER BY i.captured_at DESC LIMIT 200""", params)
        label = " · ".join(x for x in (f"'{q.get('query')}'" if words else "", q.get("category") or "",
                                        f"images {q['state']}" if q.get("state") else "") if x)
        out += [f"{len(rows)} notes · {label or 'all'}  (o note · v card · i image)", ""]
        from .notes import app_slug
        for iid, rec, cat, cap, text in rows:
            r = json.loads(rec)
            out.append(f"  {iid.split(':')[1][:8]} {(cap or '')[5:10]} {cat[:9]:<9} "
                       f"{app_slug(r.get('source_app'))[:10]:<10} {headline(r, text, words)}")
    elif view == "jobs":
        from .commands import _job_runs, job_status
        from .state import State as _S
        st_obj = _S(state_root)
        out += ["scheduled jobs · Enter = recent runs · R run now · P pause · U resume", ""]
        for job in ("watch", "purge"):
            s_ = job_status(st_obj, job)
            last = (_job_runs(st_obj, job, 1) or [{}])[0]
            where = f"watching {_short(s_['watching'])}" if s_["watching"] else "hourly at :15"
            state_word = "on " if s_["on"] else ("off" if s_["installed"] else "not installed")
            when = _local(last["at"])[4:] if last.get("at") else "-"
            out.append(f"  {job:<6} {state_word:<13} {where:<26} last {when:<13} {last.get('what', 'no runs yet')}")
        nxt = con.execute("SELECT MIN(purge_after) FROM items WHERE source_state='quarantined'").fetchone()[0]
        if nxt:
            out += ["", f"next image due for purge: {_local(nxt)} local (deleted at the first purge run after that)"]
    elif view == "notes":
        where, params = ("WHERE category=?", (category,)) if category else ("", ())
        out += [f"notes{' · ' + category if category else ''} · newest first", ""]
        for iid, cat, cap, app, grp, st in _q(con, f"""SELECT substr(id,8,8), category, substr(captured_at,1,16),
                source_app, group_id, source_state FROM items {where} ORDER BY captured_at DESC LIMIT 300""", params):
            out.append(f"  {iid}  {cat:<13} {cap or '':<16}  {(app or '').split('.')[-1][:14]:<14} "
                       f"{st:<11} {grp or ''}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- installer
# panvim runs a key's argument with `bash -c`, so no extra quoting layer: dropped paths (which the terminal
# pastes shell-escaped) reach sekerinshotto intact, even with spaces or quotes in them.
CONFIRM = ("sekerinshotto {cmd}; printf '\\ntype {word} + Enter to commit, anything else cancels: '; "
           "read a; [ \"$a\" = {word} ] && sekerinshotto {cmd} --commit || echo cancelled")

SPECS = {
    "ss": ("SekerinShotto", "home", 10, False, "95%x90%"),
    "ss-class": ("ss-categories", "class", 30, True, "95%x90%"),
    "ss-concepts": ("ss-concepts", "concepts", 60, True, "95%x90%"),
    "ss-groups": ("ss-groups", "groups", 30, True, "95%x90%"),
    "ss-audit": ("ss-audit", "audit", 15, True, "95%x90%"),
    "ss-quarantine": ("ss-quarantine", "quarantine", 1, True, "95%x90%"),
    "ss-notes": ("ss-notes", "notes", 30, True, "95%x90%"),
    "ss-results": ("ss-results", "results", 2, True, "95%x90%"),
    "ss-jobs": ("ss-jobs", "jobs", 5, True, "95%x90%"),
}


def _nav() -> list[str]:
    return ["h\thome\tpopup\tss", "c\tcategories\tpopup\tss-class", "k\tconcepts (key terms)\tpopup\tss-concepts",
            "g\tduplicate groups\tpopup\tss-groups", "a\taudit: held + kept\tpopup\tss-audit",
            "p\tquarantine countdown\tpopup\tss-quarantine", "n\tall notes\tpopup\tss-notes",
            "j\tjobs: watcher + purge schedule\tpopup\tss-jobs"]


def _note_open() -> str:
    return "o\topen the note (read-only)\tterm-side\tnvim -R \"$(sekerinshotto panel path {row})\""


def _image_keys() -> list[str]:
    # i: text art beside the board (panvim image-side; a tmux popup passes no graphics escapes, so real
    # pixels cannot reach Ghostty from a panel, and text art cannot make body text readable).
    # e: the real pixels in mpv, pinned on top of the panel like the watch panel's player (q closes).
    # `panel viewer` starts it in its own session so the closing split cannot kill it, or fails with
    # the reason (purged, missing), shown here until a key is pressed.
    return ["i\tshow the image beside the board (text art)\timage-side\tsekerinshotto panel image {row}",
            "e\tview the image full size, on top (q closes)\tterm\t"
            "sekerinshotto panel viewer {row} || read -rsn1 -p 'press a key'"]


def keys_for(name: str) -> str:
    g = ["# GENERATED by `sekerinshotto panels install` — do not hand-edit; re-run it instead.",
         "# lowercase = read-only. UPPERCASE runs the plan, then asks you to type a word before committing;",
         "# no key passes --commit on its own.", "[global]"] + _nav()
    g += ["s\tsearch → results board (on top)\tinput:search: |term|sekerinshotto panel set '{input}' && panvim popup ss-results",
          "A\tADD photos: drop zone — Finder inbox + auto-extract until q (yes or a drop starts it)\t"
          "term-side\tsekerinshotto dropzone --ask",
          "I\tINGEST the inbox (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="ingest", word="yes"),
          "C\tCLEANUP: route images (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="cleanup", word="yes"),
          "O\tORGANIZE: re-apply rules (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="organize", word="yes"),
          "q\tquit\tquit"]
    if name == "ss":
        g = [l for l in g if not l.startswith("h\t")]            # home: `h` would go nowhere
    rows: list[str] = []
    direct = {"ss": LIST_ENTER, "ss-class": LIST_ENTER, "ss-concepts": LIST_ENTER,
              "ss-audit": CARD_ENTER, "ss-notes": CARD_ENTER, "ss-results": CARD_ENTER,
              "ss-quarantine": CARD_ENTER}.get(name)
    if name == "ss-class":
        rows = ["l\tnotes in this category → results board\tterm\tsekerinshotto panel set --category {row} && panvim popup ss-results"]
    elif name == "ss-concepts":
        rows = ["l\tnotes with this term → results board\tterm\tsekerinshotto panel set {row} && panvim popup ss-results",
                "X\tHIDE this term: a name the redactor missed (plan, then confirm)\tterm-hold\t"
                + CONFIRM.format(cmd="terms hide {row}", word="yes")]
    elif name == "ss-groups":
        rows = ["o\topen the group or sequence hub note\tterm-side\tnvim -R \"$(sekerinshotto panel path {row})\""]
    elif name in ("ss-audit", "ss-notes", "ss-results"):
        rows = [_note_open(),
                *_image_keys(),
                "v\tshow the item card (redacted)\tout-side\tsekerinshotto show {row}",
                "R\tRETRY held images (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="retry", word="yes"),
                "V\tCONFIRM this image: vouch for it (plan, then confirm)\tterm-hold\t"
                + CONFIRM.format(cmd="confirm {row} --by user", word="yes"),
                "K\tKEEP this image forever (plan, then confirm)\tterm-hold\t"
                + CONFIRM.format(cmd="keep {row}", word="yes"),
                "F\tFORGET this image and its note: to the Trash (plan, then type forget)\tterm-hold\t"
                + CONFIRM.format(cmd="forget {row}", word="forget"),
                "D\tALLOW this image's unverified domains: make its URLs links (plan, then confirm)\tterm-hold\t"
                + CONFIRM.format(cmd="domains allow-item {row}", word="yes")]
    elif name == "ss-quarantine":
        rows = [_note_open(),
                *_image_keys(),
                "U\tRESTORE this image (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="restore {row}", word="yes"),
                "F\tFORGET this image and its note: to the Trash (plan, then type forget)\tterm-hold\t"
                + CONFIRM.format(cmd="forget {row}", word="forget"),
                "P\tPURGE images that are due (plan, then type purge)\tterm-hold\t"
                + CONFIRM.format(cmd="purge", word="purge")]
    if name == "ss-jobs":
        rows = ["R\tRUN this job now (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="schedule run --job {row}", word="yes"),
                "P\tPAUSE this job (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="schedule pause --job {row}", word="yes"),
                "U\tRESUME this job (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="schedule resume --job {row}", word="yes")]
        direct = "<CR>\tEnter: recent runs of this job\tout-side\tsekerinshotto schedule log --job {row}"
    if name == "ss":
        rows = ["l\tlist this category or image state\tterm\tsekerinshotto panel set-row {row} && panvim popup ss-results"]
    return "\n".join(g + (["[row]"] + rows if rows else []) + (["[direct]", direct] if direct else [])) + "\n"


# Colour names panvim's view.lua resolves in a syntax.tsv (its `palette` table). A theme ROLE such as `mark`
# is not in it: nvim receives the literal name and every panel fails at startup (E5113).
SYNTAX_COLOURS = {"head", "key", "section", "value", "dim", "cmd", "accent", "ok", "warn", "bad", "info", "text",
                  "band", "mauve", "grey", "green", "yellow", "red", "blue", "teal", "peach"}

# Vibrant scan colours on the theme's dark ground; structure (headings, counts) keeps theme roles. Retune here.
VIBRANT = {
    "pink": "#ff6ac1", "orange": "#ffb86c", "yellow": "#f1fa8c", "green": "#50fa7b", "cyan": "#8be9fd",
    "purple": "#bd93f9", "red": "#ff5555", "blue": "#6ea8fe", "teal": "#2de2c4", "lime": "#b6f36b",
    "gold": "#ffd866", "coral": "#ff9e64", "sky": "#7dcfff", "violet": "#c3a6ff",
}
CATEGORY_COLOURS = {
    "event": "orange", "payment": "green", "chat": "cyan", "social": "pink", "web": "blue", "learning": "purple",
    "health": "red", "shopping": "yellow", "form": "teal", "document": "lime", "email": "gold", "game": "coral",
    "travel": "sky", "security": "red", "code": "violet",
}


def _syntax() -> str:
    V = VIBRANT
    rows = [
        "# GENERATED by `sekerinshotto panels install`. Theme roles from ~/.config/panvim/theme.conf; scan colours",
        "# are the VIBRANT table in sekerinshotto/panels.py. Later rules win where they overlap.",
        "# No slash in any regex: the engine uses it as the :syntax delimiter.",
        "head\tsection,bold\t^\\S.*$",
        f"title\t{V['pink']},bold\t^SekerinShotto.*$",
        "count\tvalue\t\\<[0-9]\\+\\>",
        f"bar\t{V['pink']}\t▇\\+",
        f"id\t{V['violet']}\t^  \\zs[0-9a-f]\\{{8}}\\>",
        f"quarantined\t{V['orange']},bold\t\\<quarantined\\>",
        f"attached\t{V['green']},bold\t\\<attached\\>",
        f"held\t{V['red']},bold\t\\<held\\>",
        f"present\t{V['cyan']},bold\t\\<present\\>",
        "purged\tdim\t\\<purged\\>",
        "uncat\tdim\t\\<uncategorized\\>",
        "system\tdim\t\\<system\\>",
    ]
    rows += [f"cat_{c}\t{V[col]}\t\\<{c}\\>" for c, col in CATEGORY_COLOURS.items()]
    rows += [
        f"countdown\t{V['yellow']},bold\t\\d\\+d \\d\\dh \\d\\dm \\d\\ds",
        f"due\t{V['red']},bold\tdue now",
        f"on\t{V['green']},bold\t^  \\S\\+\\s\\+\\zson\\>",
        f"off\t{V['red']},bold\t\\<off\\>\\|not installed",
        f"verified\t{V['teal']}\t\\<verified\\>",
        f"unverified\t{V['orange']}\tunverified URL",
        f"group\t{V['sky']}\t\\<grp-[0-9a-f]\\{{8}}\\>\\|\\<seq-[0-9a-f]\\{{8}}\\>",
    ]
    return "\n".join(rows) + "\n"


SYNTAX = _syntax()


def install(commit: bool) -> dict:
    if not shutil.which("panvim"):
        raise ToolError("panvim is not on PATH")
    if not shutil.which("sekerinshotto"):
        raise ToolError("sekerinshotto is not on PATH; install it first: uv tool install --editable <repo>")
    have = {p["name"] for p in json.loads(subprocess.run(["panvim", "popups", "--all", "--json"],
                                                          capture_output=True, text=True).stdout or "[]")}
    cfg = Path.home() / ".config" / "panvim"
    plan = []
    for name, (title, view, interval, hidden, size) in SPECS.items():
        plan.append({"panel": name, "view": view, "exists": name in have, "hidden": hidden,
                     "keys": str(cfg / name / "keys.tsv")})
    if not commit:
        return {"committed": False, "panels": plan}
    created, failures = [], []
    for name, (title, view, interval, hidden, size) in SPECS.items():
        if name not in have:
            cmd = ["panvim", "new", name, "--render", f'sekerinshotto panel {view} > "$PANVIM_OUT"',
                   "--title", title, "--interval", str(interval), "--row", ROW_PATTERN, "--size", size, "--commit"]
            if hidden:
                cmd.insert(-1, "--hidden")
            p = subprocess.run(cmd, capture_output=True, text=True)
            if p.returncode not in (0, 1):                  # new runs audit afterwards; 1 = findings
                failures.append({"panel": name, "error": (p.stderr or p.stdout)[-300:]})
                continue
            created.append(name)
        (cfg / name).mkdir(parents=True, exist_ok=True)
        (cfg / name / "keys.tsv").write_text(keys_for(name))
        (cfg / name / "syntax.tsv").write_text(SYNTAX)
    repaired = _repair_wrappers() + _sync_registry_titles(cfg / "popups.conf")
    audit = subprocess.run(["panvim", "audit"], capture_output=True, text=True)
    return {"committed": True, "created": created, "rewrote_keys": list(SPECS), "repaired_wrappers": repaired,
            "failures": failures,
            "panvim_audit": {"exit": audit.returncode, "output": (audit.stdout + audit.stderr)[-1500:]}}


def _repair_wrappers() -> list[str]:
    """`panvim new` writes `--title %s` unquoted, so a title with a space breaks the wrapper (the shell
    splits it and `panvim view` exits 1). Rewrite only that line, only in our own ss* wrappers."""
    import re
    import shlex
    fixed = []
    bindir = Path.home() / ".local" / "bin"
    for name, (title, *_rest) in SPECS.items():
        w = bindir / f"{name}-popup"
        if not w.exists():
            continue
        text = w.read_text()
        new = re.sub(r"(exec panvim view --title )(.*?)( \\\n)", lambda m: m.group(1) + shlex.quote(title) + m.group(3),
                     text, count=1)
        if new != text:
            w.write_text(new)
            fixed.append(name)
    return fixed


def _sync_registry_titles(registry: Path) -> list[str]:
    """Keep the title and size columns of our own ss* rows in popups.conf equal to SPECS (the title so
    `panvim audit` can match state dirs; the size so the side-pane results board has room).
    Other rows are never touched."""
    if not registry.exists():
        return []
    fixed, out = [], []
    for line in registry.read_text().splitlines(keepends=True):
        cols = line.rstrip("\n").split("\t")
        if not line.startswith("#") and len(cols) >= 5 and cols[0] in SPECS:
            title, _view, _iv, _hidden, size = SPECS[cols[0]]
            w, h = size.split("x")
            if (cols[1], cols[2], cols[3]) != (w, h, title):
                cols[1], cols[2], cols[3] = w, h, title
                line = "\t".join(cols) + "\n"
                fixed.append(f"{cols[0]} (registry title/size)")
        out.append(line)
    if fixed:
        registry.write_text("".join(out))
    return fixed


def set_row(state_root: Path, con, row: str) -> dict:
    """What Enter on a board row means: an image state or a category -> list; anything else -> search."""
    if row in STATES:
        return set_query(state_root, None, None, row)
    if con.execute("SELECT 1 FROM items WHERE category=? LIMIT 1", (row,)).fetchone():
        return set_query(state_root, None, row)
    return set_query(state_root, row, None)


# Drill-down opens ss-results as its own popup on top (full size), instead of squeezing it into this
# panel's sidePan; the card then gets results' sidePan. `panvim popup` runs the panel directly outside tmux.
LIST_ENTER = "<CR>\tEnter: open as a results board (on top)\tterm\tsekerinshotto panel set-row {row} && panvim popup ss-results"
# Cards open in read-only nvim reading stdin: top-aligned, wrapped, `/` search, q closes. (less inside
# panvim's terminal pane rendered bottom-aligned.)
VIEWER = " --view"          # `show --view` opens the card in read-only nvim itself (clean sidePan label)
# panvim's out-side shows a command's output in sidePan (read-only, top-aligned, q closes).
CARD_ENTER = "<CR>\tEnter: the item card (redacted)\tout-side\tsekerinshotto show {row}"
