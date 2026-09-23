"""panvim panels: read-only text renderers and the installer.

Renderers read the index only (never OCR, never Laya) because panvim re-runs them on a
timer, unattended. They show counts, categories, terms, reasons and note names; never
OCR text or QR payloads, because a panel stays on screen (screen shares included).
Row lines start with two spaces and an id, matched by ROW_PATTERN.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from .contract import ToolError
from .state import now_iso, parse_iso

ROW_PATTERN = "^  (%S+)"
VIEWS = ("home", "class", "concepts", "groups", "audit", "quarantine", "notes")


def countdown(seconds: int) -> str:
    if seconds <= 0:
        return "due now"
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    return f"{d}d {h:02d}h {m:02d}m {s:02d}s"


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
        groups = con.execute("SELECT COUNT(DISTINCT group_id) FROM items WHERE group_id IS NOT NULL").fetchone()[0]
        out += [f"SekerinShotto · {total} notes · {groups} groups", ""]
        out.append("images")
        for st, n in _q(con, "SELECT source_state, COUNT(*) FROM items GROUP BY 1 ORDER BY 2 DESC"):
            out.append(f"  {st:<12} {n:>5}")
        nxt = con.execute("SELECT MIN(purge_after) FROM items WHERE source_state='quarantined'").fetchone()[0]
        if nxt:
            out += ["", f"next purge   {nxt}  ({countdown(int((parse_iso(nxt) - parse_iso(now)).total_seconds()))})"]
        out += ["", "categories"]
        for cat, n in _q(con, "SELECT category, COUNT(*) FROM items GROUP BY 1 ORDER BY 2 DESC, 1"):
            out.append(f"  {cat:<14} {n:>5}")
        uncat = con.execute("SELECT COUNT(*) FROM items WHERE category='uncategorized'").fetchone()[0]
        held = con.execute("SELECT COUNT(*) FROM items WHERE source_state='held'").fetchone()[0]
        out += ["", f"to do        {uncat} uncategorized (LLM can tag) · {held} held (see audit)"]
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
    elif view == "audit":
        out += ["held and kept images · reasons, never content", ""]
        for iid, st, reason, att, note in _q(con, """SELECT substr(id,8,8), source_state, hold_reason,
                attempts, note_path FROM items WHERE source_state IN ('held','attached')
                ORDER BY source_state DESC, hold_reason"""):
            why = (reason or "visual, kept in the vault")[:48]
            out.append(f"  {iid}  {st:<8} {why:<48} tries {att or 0}  {Path(note).stem if note else ''}")
    elif view == "quarantine":
        out += [f"quarantine · purged exactly 7 days after cleanup · now {now}", ""]
        for iid, after, note in _q(con, """SELECT substr(id,8,8), purge_after, note_path FROM items
                WHERE source_state='quarantined' ORDER BY purge_after, id"""):
            left = int((parse_iso(after) - parse_iso(now)).total_seconds())
            out.append(f"  {iid}  {countdown(left):<18} {after}  {Path(note).stem if note else ''}")
    elif view == "notes":
        where, params = ("WHERE category=?", (category,)) if category else ("", ())
        out += [f"notes{' · ' + category if category else ''} · newest first", ""]
        for iid, cat, cap, app, grp, st in _q(con, f"""SELECT substr(id,8,8), category, substr(captured_at,1,16),
                source_app, group_id, source_state FROM items {where} ORDER BY captured_at DESC LIMIT 300""", params):
            out.append(f"  {iid}  {cat:<13} {cap or '':<16}  {(app or '').split('.')[-1][:14]:<14} "
                       f"{st:<11} {grp or ''}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- installer
CONFIRM = ("sh -c 'sekerinshotto {cmd}; printf \"\\ntype {word} to commit, anything else cancels: \"; "
           "read a; [ \"$a\" = {word} ] && sekerinshotto {cmd} --commit || echo cancelled'")

SPECS = {
    "ss": ("SekerinShotto", "home", 10, False, "86x40"),
    "ss-class": ("ss · categories", "class", 30, True, "70x34"),
    "ss-concepts": ("ss · concepts", "concepts", 60, True, "70x40"),
    "ss-groups": ("ss · groups", "groups", 30, True, "96x34"),
    "ss-audit": ("ss · audit", "audit", 15, True, "120x36"),
    "ss-quarantine": ("ss · quarantine", "quarantine", 1, True, "110x36"),
    "ss-notes": ("ss · notes", "notes", 30, True, "110x40"),
}


def _nav() -> list[str]:
    return ["h\thome\tpopup\tss", "c\tcategories\tpopup\tss-class", "k\tconcepts (key terms)\tpopup\tss-concepts",
            "g\tduplicate groups\tpopup\tss-groups", "a\taudit: held + kept\tpopup\tss-audit",
            "p\tquarantine countdown\tpopup\tss-quarantine", "n\tall notes\tpopup\tss-notes"]


def _note_open() -> str:
    return "o\topen the note (read-only)\tterm-side\tnvim -R \"$(sekerinshotto panel path {row})\""


def keys_for(name: str) -> str:
    g = ["# GENERATED by `sekerinshotto panels install` — do not hand-edit; re-run it instead.",
         "# lowercase = read-only. UPPERCASE runs the plan, then asks you to type a word before committing;",
         "# no key passes --commit on its own.", "[global]"] + _nav()
    g += ["s\tsearch (excerpts are redacted)\tinput:search: |term-hold|sekerinshotto search {input} --limit 30",
          "w\twhere things stand (status)\tterm-hold\tsekerinshotto status",
          "C\tCLEANUP: route images (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="cleanup", word="yes"),
          "O\tORGANIZE: re-apply rules (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="organize", word="yes"),
          "q\tquit\tquit"]
    rows: list[str] = []
    if name == "ss-class":
        rows = ["l\tlist this category (redacted)\tterm-side\tsekerinshotto list --category {row} --limit 100"]
    elif name == "ss-concepts":
        rows = ["l\tnotes with this term (redacted)\tterm-side\tsekerinshotto search {row} --limit 100"]
    elif name == "ss-groups":
        rows = ["o\topen the group or sequence hub note\tterm-side\tnvim -R \"$(sekerinshotto panel path {row})\""]
    elif name in ("ss-audit", "ss-notes"):
        rows = [_note_open(),
                "i\topen the image\tterm\topen \"$(sekerinshotto panel image {row})\"",
                "v\tshow the item (redacted)\tterm-side\tsekerinshotto show {row}",
                "R\tRETRY held images (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="retry", word="yes"),
                "V\tCONFIRM this image: vouch for it (plan, then confirm)\tterm-hold\t"
                + CONFIRM.format(cmd="confirm {row} --by user", word="yes"),
                "K\tKEEP this image forever (plan, then confirm)\tterm-hold\t"
                + CONFIRM.format(cmd="keep {row}", word="yes")]
    elif name == "ss-quarantine":
        rows = [_note_open(),
                "i\topen the image\tterm\topen \"$(sekerinshotto panel image {row})\"",
                "U\tRESTORE this image (plan, then confirm)\tterm-hold\t" + CONFIRM.format(cmd="restore {row}", word="yes"),
                "P\tPURGE images that are due (plan, then type purge)\tterm-hold\t"
                + CONFIRM.format(cmd="purge", word="purge")]
    return "\n".join(g + (["[row]"] + rows if rows else [])) + "\n"


SYNTAX = """# GENERATED by `sekerinshotto panels install`. Roles from ~/.config/panvim/theme.conf.
# No "/" in any regex: the engine uses it as the :syntax delimiter.
head\tsection,bold\t^\\S.*$
held\twarn,bold\t\\<held\\>
due\tbad,bold\tdue now
purged\tdim\t\\<purged\\>
count\tvalue\t\\<[0-9]\\+\\>
"""


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
    audit = subprocess.run(["panvim", "audit"], capture_output=True, text=True)
    return {"committed": True, "created": created, "rewrote_keys": list(SPECS), "failures": failures,
            "panvim_audit": {"exit": audit.returncode, "output": (audit.stdout + audit.stderr)[-1500:]}}
