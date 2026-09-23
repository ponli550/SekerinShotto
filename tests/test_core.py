"""Phase 1 tests. The end-to-end tests run Apple Vision, so they need macOS."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from sekerinshotto.contract import ToolError
from sekerinshotto.extract import (Extraction, _crc16_ccitt, classify_qr, find_urls, parse_emv,
                                   parse_filename)
from sekerinshotto.notes import NoteConflict, render
from sekerinshotto.state import Journal, resolve_state, verify_journal


# ---------------------------------------------------------------- filename metadata
@pytest.mark.parametrize("name,app,when", [
    ("Screenshot_20251227_204228_com_whatsapp_w4b_Conversation.jpg", "com.whatsapp.w4b", "2025-12-27T20:42:28"),
    ("Screenshot_20251208_231441_com_lemon_lvoverseas_EditActivity_edit_5002.jpg", "com.lemon.lvoverseas", "2025-12-08T23:14:41"),
    ("Screenshot_20260106_003340_com_hihonor_android_launcher_UniHomeLauncher.jpg", "com.hihonor.android.launcher", "2026-01-06T00:33:40"),
    ("Screenshot_20251204_125809_my_com_gxbank_app_HomeActivity.jpg", "my.com.gxbank.app", "2025-12-04T12:58:09"),
])
def test_android_filename(name, app, when):
    assert parse_filename(name) == {"source_app": app, "captured_at": when}


def test_non_android_filename_has_no_metadata():
    assert parse_filename("IMG_0412.png") == {}


# ---------------------------------------------------------------- QR classification
def _emv(fields: list[tuple[str, str]]) -> str:
    body = "".join(f"{t}{len(v):02d}{v}" for t, v in fields) + "6304"
    return body + f"{_crc16_ccitt(body.encode()):04X}"


def test_duitnow_payment_with_format_02_and_valid_crc():
    p = _emv([("00", "02"), ("01", "11"), ("59", "TEST MERCHANT"), ("60", "KL")])
    kind, stored, extra = classify_qr(p)
    assert kind == "payment" and stored == p
    assert extra == {"merchant_name": "TEST MERCHANT", "merchant_city": "KL"}


def test_payment_with_bad_crc_is_not_payment():
    p = _emv([("00", "01"), ("59", "X")])
    assert parse_emv(p[:-1] + ("0" if p[-1] != "0" else "1")) is None
    assert classify_qr(p[:-1] + ("0" if p[-1] != "0" else "1"))[0] == "text"


def test_wifi_password_redacted_including_escaped_semicolon():
    kind, stored, _ = classify_qr(r"WIFI:T:WPA;S:home;P:se\;cret;;")
    assert kind == "wifi" and "se" not in stored.split("P:")[1] and "<redacted>" in stored


@pytest.mark.parametrize("payload,kind", [
    ("https://qr.example.net/t3st", "url"), ("mailto:a@b.c", "mailto"),
    ("BEGIN:VCARD\nFN:A\nEND:VCARD", "contact"), ("55501234|ABCDEF0123", "text")])
def test_qr_kinds(payload, kind):
    assert classify_qr(payload)[0] == kind


# ---------------------------------------------------------------- URLs
def test_find_urls_normalizes_dedupes_and_skips_email():
    got = find_urls("see https://Example.com/docs/. and www.example.com/docs, mail me at a@example.com, also forms.gle/x")
    assert [n for _, n in got] == ["https://example.com/docs", "https://www.example.com/docs", "https://forms.gle/x"]


# ---------------------------------------------------------------- notes
def _ex(**kw) -> Extraction:
    ex = Extraction(id="sha256:" + "ab" * 32, path=Path("/x/Screenshot_1.png"), width=10, height=10, bytes=2048)
    ex.lines = [("Hello world", 1.0)]
    ex.urls = [{"raw": "docs.qoogle.com", "url": "https://docs.qoogle.com", "verified_by": "none", "confidence": 1.0},
               {"raw": "https://qr.example.net/x", "url": "https://qr.example.net/x", "verified_by": "qr", "confidence": 1.0}]
    ex.__dict__.update(kw)
    return ex


def test_ocr_urls_are_never_links():
    note = render(_ex(), "2026-01-01T00:00:00Z")
    assert "[qr.example.net/x](https://qr.example.net/x)" in note          # QR url is a link
    assert "](https://docs.qoogle.com)" not in note                    # OCR url is not
    assert 'urls_unverified: ["docs.qoogle.com"]' in note              # and has no scheme in frontmatter
    assert 'urls: ["https://qr.example.net/x"]' in note


def test_rerender_keeps_user_text_foreign_keys_and_llm_category():
    first = render(_ex(), "2026-01-01T00:00:00Z")
    edited = first.replace('category: "uncategorized"', 'category: "event"\ndecided_by: "llm"')
    edited = edited.replace("\n---\n", '\nwrapper_key: "keep me"\n---\n', 1) + "my own thought\n"
    again = render(_ex(lines=[("Changed text", 1.0)]), "2026-01-02T00:00:00Z", edited)
    assert "my own thought" in again and 'wrapper_key: "keep me"' in again
    assert 'category: "event"' in again and 'decided_by: "llm"' in again
    assert "Changed text" in again and "Hello world" not in again


def test_note_without_markers_is_a_conflict():
    with pytest.raises(NoteConflict):
        render(_ex(), "t", "---\nid: x\n---\n\nhand-written, markers gone\n")


# ---------------------------------------------------------------- journal
def test_journal_detects_edit_and_wrong_key(tmp_path):
    j = Journal(tmp_path / "j.jsonl", b"k" * 32)
    for i in range(3):
        j.append(op="create", id=str(i))
    assert verify_journal(j.path, b"k" * 32) == (True, 3)
    assert verify_journal(j.path, b"x" * 32)[0] is False
    lines = j.path.read_text().splitlines()
    lines[1] = lines[1].replace('"create"', '"update"')
    j.path.write_text("\n".join(lines) + "\n")
    assert verify_journal(j.path, b"k" * 32)[0] is False


def test_state_refuses_synced_folder():
    with pytest.raises(ToolError, match="synced"):
        resolve_state("~/Library/Mobile Documents/com~apple~CloudDocs/ss")


# ---------------------------------------------------------------- end to end (Vision)
def _run(*args, env):
    p = subprocess.run([sys.executable, "-m", "sekerinshotto.cli", *args, "--json"],
                       capture_output=True, text=True, env=env)
    return p.returncode, json.loads(p.stdout)


@pytest.fixture
def sample(tmp_path):
    import zxingcpp
    img = Image.new("RGB", (1200, 900), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=48)
    d.text((40, 40), "Register at docs.example.com/form today", fill="black", font=font)
    qr = zxingcpp.create_barcode("https://q.example.com/abc", zxingcpp.BarcodeFormat.QRCode).to_image(scale=10)
    h, w = qr.shape[:2]
    img.paste(Image.frombytes("L", (w, h), bytes(qr)), (40, 200))
    src = tmp_path / "in"
    src.mkdir()
    img.save(src / "Screenshot_20260101_120000_com_android_chrome_ChromeTabbedActivity.png")
    img.save(src / "copy.png")                                       # same bytes -> duplicate in batch
    import os
    env = {**os.environ, "SEKERINSHOTTO_STATE": str(tmp_path / "state")}
    return src, tmp_path / "content", env


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_end_to_end_plan_commit_rerun(sample):
    src, content, env = sample
    code, env_plan = _run("ingest", str(src), "--content", str(content), env=env)
    assert code == 0 and env_plan["ok"] and env_plan["data"]["planned"] == 1
    assert env_plan["data"]["duplicates_in_batch"] == 1 and not content.exists()

    code, res = _run("ingest", str(src), "--content", str(content), "--commit", env=env)
    d = res["data"]
    # the OCR-read docs.example.com is confirmed by the QR code on the same domain
    assert code == 0 and d["written"] == 1 and d["urls"]["qr"] == 1 and d["urls"]["crossref"] == 1
    note = next((content / "notes").rglob("*.md")).read_text()
    assert "[q.example.com/abc](https://q.example.com/abc) · from QR" in note
    assert "[docs.example.com/form](https://docs.example.com/form) · read by OCR, domain crossref" in note
    assert 'source_app: "com.android.chrome"' in note

    code, again = _run("ingest", str(src), "--content", str(content), env=env)
    assert again["data"]["planned"] == 0 and again["data"]["skipped_already_extracted"] == 1

    code, st = _run("status", env=env)
    assert st["data"]["journal"]["intact"] and st["data"]["items"] == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_db_is_disposable(sample):
    src, content, env = sample
    _run("ingest", str(src), "--content", str(content), "--commit", env=env)
    for f in Path(env["SEKERINSHOTTO_STATE"]).glob("index.sqlite*"):
        f.unlink()
    code, res = _run("reindex", "--commit", env=env)
    assert code == 0 and res["data"]["rebuilt"] == 1
    assert _run("status", env=env)[1]["data"]["urls_by_verification"] == {"crossref": 1, "qr": 1}
    assert _run("status", env=env)[1]["data"]["domain_list"] is None


def test_errors_are_envelopes(tmp_path):
    import os
    env = {**os.environ, "SEKERINSHOTTO_STATE": str(tmp_path / "s")}
    code, res = _run("bogus", env=env)
    assert code == 1 and res["ok"] is False and "error" in res and "data" not in res
    code, res = _run("ingest", str(tmp_path / "missing"), env=env)
    assert code == 1 and "does not exist" in res["error"]


def test_schema_matches_registry():
    from sekerinshotto.contract import REGISTRY
    from sekerinshotto.commands import cmd_schema  # noqa: F401
    import argparse
    data = cmd_schema(argparse.Namespace(path=None), None).data
    assert [c["path"] for c in data["commands"]] == list(REGISTRY)
    ingest = next(c for c in data["commands"] if c["path"] == "ingest")
    assert ingest["writes"] and any(a["name"] == "--commit" for a in ingest["args"])


# ---------------------------------------------------------------- URL correction (phase 2)
import sqlite3 as _sq

from sekerinshotto.domains import DomainIndex, PSL
from sekerinshotto.extract import find_urls_in_lines, raw_host
from sekerinshotto.urlfix import candidates, resolve

_RANKS = {"google.com": 1, "paypal.com": 170, "gmail.com": 231, "lnkd.in": 1978, "maybank2u.com.my": 8434,
          "l1nk.dev": 307935, "acesse.one": 319761, "inkd.in": 794020}


@pytest.fixture
def dom(tmp_path):
    f = tmp_path / "domains"
    f.mkdir()
    con = _sq.connect(f / "ranks.sqlite")
    con.execute("CREATE TABLE ranks (domain TEXT PRIMARY KEY, rank INTEGER)")
    con.executemany("INSERT INTO ranks VALUES (?,?)", _RANKS.items())
    con.commit()
    con.close()
    (f / "psl.dat").write_text("com\nin\ndev\none\nmy\ncom.my\ngov.my\n")
    (f / "tlds.txt").write_text("# v\nCOM\nIN\nDEV\nONE\nMY\n")
    return DomainIndex(f)


@pytest.mark.parametrize("raw,host,corrected", [
    ("docs.qoogle.com", "docs.google.com", True),      # q->g; must NOT become d0cs.google.com
    ("Inkd.in", "lnkd.in", True),                      # the raw lookalike is itself ranked (794020)
    ("|1nk.dev", "l1nk.dev", True),                    # invalid char; "i1nk.dev" is unranked
    ("paypaI.com", "paypal.com", True),
    ("rnaybank2u.com.my", "maybank2u.com.my", True),   # rn->m
    ("docs.google.com", "docs.google.com", False),     # popular raw is trusted as-is
    ("acesse.one", "acesse.one", False),               # ranked raw, no likelier lookalike
])
def test_resolve(dom, raw, host, corrected):
    r = resolve(raw, dom, set())
    assert (r["host"], r["corrected"]) == (host, corrected) and r["verified_by"] == "known"


def test_resolve_flags_impossible_tld_and_uses_qr_crossref(dom):
    assert resolve("www.ome", dom, set())["flag"] == "invalid_tld"
    r = resolve("hackfest2O26.my", dom, {"hackfest2026.my"})
    assert r == {**r, "host": "hackfest2026.my", "verified_by": "crossref", "corrected": True}
    assert resolve("unknownsite.my", dom, set())["verified_by"] == "none"


def test_candidates_prefer_fewest_edits():
    c = candidates("docs.qoogle.com")
    assert c["docs.google.com"] == 1 and c["d0cs.google.com"] == 2


def test_psl_registrable():
    psl = PSL("com\nmy\ncom.my\n*.ck\n!www.ck\n")
    assert psl.registrable("a.b.shop.com.my") == "shop.com.my"
    assert psl.registrable("docs.google.com") == "google.com"
    assert psl.registrable("com.my") is None
    assert psl.registrable("a.b.ck") == "a.b.ck" and psl.registrable("www.ck") == "www.ck"


def test_raw_host_keeps_case_and_odd_chars():
    assert raw_host("https://|1nk.dev/YxKdP") == "|1nk.dev" and raw_host("Inkd.in/x") == "Inkd.in"


def _lines(*texts, h=0.017):
    return [(t, 1.0, (0.1, 0.4 + i * 0.027, 0.8, h)) for i, t in enumerate(texts)]


def test_wrapped_url_is_joined_and_stray_end_dropped():
    u = find_urls_in_lines(_lines("details: https://courses.example.com.my/campai",
                                  "gns/cloud-skills-for-your-future-", "with-partners<", "It takes 1-2 weeks"))
    assert u[0]["url"] == "https://courses.example.com.my/campaigns/cloud-skills-for-your-future-with-partners"
    assert u[0]["joined"] == 2


def test_join_refuses_words_dates_and_far_lines():
    assert find_urls_in_lines(_lines("see https://a.com/x", "Location"))[0]["joined"] == 0
    assert find_urls_in_lines(_lines("see https://a.com/x", "11/12/2025"))[0]["joined"] == 0
    far = [("see https://a.com/x", 1.0, (0.1, 0.1, 0.8, 0.017)), ("/more-path", 1.0, (0.1, 0.5, 0.8, 0.017))]
    assert find_urls_in_lines(far)[0]["joined"] == 0


def test_ellipsis_marks_truncated():
    assert find_urls_in_lines(_lines("https://register.gotow…"))[0]["truncated"] is True


# ---------------------------------------------------------------- organize (phase 3)
from sekerinshotto.extract import content_tokens, token_hashes, minhash
from sekerinshotto.organize import group, organize as run_organize, score
from sekerinshotto.rules import _compile, classify, load as load_rules


def _rules():
    return load_rules(Path("/nonexistent"))[0]


@pytest.mark.parametrize("app,qr,domains,text,cat", [
    ("com.whatsapp.w4b", set(), [], "Hackathon registration closes 22 January 2026", "event"),
    ("com.whatsapp.w4b", set(), [], "okay faham nanti tanya", "chat"),
    ("com.instagram.android", {"payment"}, [], "scan to pay", "payment"),
    ("com.hihonor.android.launcher", set(), [], "TNG eWallet Cash In Successful", "payment"),
    ("com.google.android.gm", set(), ["forms.gle"], "please fill in", "form"),
    ("com.google.android.gm", set(), [], "Pendaftaran dibuka sehingga 20 DECEMBER", "event"),
    ("com.linkedin.android", set(), [], "a post about hiring", "social"),
    ("com.unknown.app", set(), [], "nothing useful", "uncategorized"),
])
def test_default_rules(app, qr, domains, text, cat):
    assert classify(_rules(), app, qr, domains, text)[0] == cat


def test_event_needs_a_date():
    assert classify(_rules(), "com.unknown", set(), [], "Join our workshop soon")[0] == "uncategorized"


def test_bad_user_rules_are_errors():
    with pytest.raises(ToolError, match="invalid category"):
        _compile({"rule": [{"category": "Bad Name"}]}, "x")
    with pytest.raises(ToolError, match="bad regex"):
        _compile({"rule": [{"category": "ok", "text": ["("]}]}, "x")


def _rec(iid, words, app="com.x", qr=(), captured="2026-01-01T00:00:00", chars=None, w=1200, h=2640):
    toks = token_hashes(set(words))
    return {"id": iid, "source_app": app, "captured_at": captured, "width": w, "height": h,
            "entities": {"qr": [{"payload": p, "type": "url"} for p in qr], "urls": [], "domains": []},
            "text_chars": chars if chars is not None else 20 * len(words), "ocr_confidence": 1.0,
            "toks": toks, "sig": minhash(toks), "dhash": None, "content_tokens": len(toks),
            "_note_path": None, "_prev": {"category": None, "decided_by": None, "why": None, "group": None,
                                          "rank": None, "size": None}, "_text": " ".join(words)}


_DOC = [f"word{i}" for i in range(40)]


def test_crop_is_grouped_with_its_full_version_and_ranks_second():
    items = {"sha256:aa": _rec("sha256:aa", _DOC + ["fit", "screen", "signature"]),       # viewer
             "sha256:bb": _rec("sha256:bb", _DOC[:30], chars=300)}                       # crop
    comps, _ = group(items)
    assert list(comps.values()) == [["sha256:aa", "sha256:bb"]]
    org = run_organize(items, _rules(), Path("/nonexistent"))
    assert org["sha256:aa"]["rank"] == 1 and org["sha256:bb"]["rank"] == 2


def test_same_template_different_figures_is_not_grouped():
    base = ["sleep", "deep", "light", "rem", "reference", "low", "high", "normal", "time", "bed", "awake", "woke"]
    day1 = content_tokens([(" ".join(base) + " 16% 58% 26% 5h41 20:53 05:29", 1.0, None)])
    day2 = content_tokens([(" ".join(base) + " 15% 62% 23% 8h36 22:10 06:30", 1.0, None)])
    items = {"sha256:aa": _rec("sha256:aa", sorted(day1)), "sha256:bb": _rec("sha256:bb", sorted(day2))}
    assert group(items)[0] == {}


def test_same_qr_payload_groups_even_with_different_text():
    items = {"sha256:aa": _rec("sha256:aa", ["alpha"] * 1, qr=["https://q.example.com/x"]),
             "sha256:bb": _rec("sha256:bb", ["beta"], qr=["https://q.example.com/x"])}
    assert list(group(items)[0].values()) == [["sha256:aa", "sha256:bb"]]


def test_status_bar_lines_do_not_count():
    lines = [("11:58 43% battery", 1.0, (0.0, 0.01, 1, 0.02)), ("real content here", 1.0, (0.0, 0.5, 1, 0.02))]
    assert content_tokens(lines) == {"real", "content", "here"}


def test_group_id_is_kept_when_a_member_joins():
    items = {"sha256:aa": _rec("sha256:aa", _DOC), "sha256:bb": _rec("sha256:bb", _DOC)}
    for i in items.values():
        i["_prev"]["group"] = "grp-keepme00"
    items["sha256:00"] = _rec("sha256:00", _DOC)                   # sorts first, but is new
    org = run_organize(items, _rules(), Path("/nonexistent"))
    assert {o["group"] for o in org.values()} == {"grp-keepme00"}


def test_score_prefers_qr_and_text():
    a, b = _rec("sha256:aa", _DOC, qr=["x"]), _rec("sha256:bb", _DOC)
    assert score(a)[0] > score(b)[0]


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_organize_moves_note_and_keeps_llm_decision(sample, tmp_path):
    src, content, env = sample
    _run("ingest", str(src), "--content", str(content), "--commit", env=env)
    note = next((content / "notes").rglob("*.md"))
    assert note.parent.name == "web"                               # chrome screenshot -> web
    note.write_text(note.read_text() + "keep this line\n")
    rules = Path(env["SEKERINSHOTTO_STATE"]) / "rules.toml"
    rules.write_text('[[rule]]\ncategory = "browsing"\napps = ["com.android.chrome"]\n')
    code, res = _run("organize", "--commit", env=env)
    moved = content / "notes" / "browsing" / note.name
    assert code == 0 and moved.exists() and not note.exists() and not note.parent.exists()
    assert "keep this line" in moved.read_text()
    moved.write_text(moved.read_text().replace('decided_by: "rule"', 'decided_by: "llm"')
                     .replace('category: "browsing"', 'category: "reading"'))
    rules.unlink()
    _run("organize", "--commit", env=env)
    final = content / "notes" / "reading" / note.name              # the llm's category decides the folder
    assert 'category: "reading"' in final.read_text() and "keep this line" in final.read_text()
    assert _run("organize", env=env)[1]["data"]["notes_to_write"] == 0


# ---------------------------------------------------------------- cleanup (phase 4)
from sekerinshotto import cleanup as lc


def test_purge_guard_refuses_anything_outside_quarantine(tmp_path):
    state = tmp_path / "state"
    (state / "quarantine" / "b1").mkdir(parents=True)
    victim = tmp_path / "users-photo.jpg"
    victim.write_bytes(b"x")
    assert lc.purge_file(victim, state) is False                                   # outside
    link = state / "quarantine" / "b1" / "link.jpg"
    link.symlink_to(victim)
    assert lc.purge_file(link, state) is False and victim.exists()                 # symlink escaping out
    assert lc.purge_file(state / "quarantine" / "b1" / ".." / ".." / ".." / victim.name, state) is False
    assert lc.purge_file(state / "quarantine", state) is False                     # the folder itself
    inside = state / "quarantine" / "b2" / "a.jpg"
    inside.parent.mkdir()
    inside.write_bytes(b"x")
    assert lc.purge_file(inside, state) is True and not inside.exists() and not inside.parent.exists()


def test_quarantine_is_exactly_seven_days():
    assert lc.QUARANTINE_SECONDS == 604800
    rec = {"purge_after": "2026-09-30T05:22:53Z"}
    assert not lc.is_due(rec, "2026-09-30T05:22:52Z") and lc.is_due(rec, "2026-09-30T05:22:53Z")
    assert lc.seconds_left(rec, "2026-09-23T05:22:53Z") == 604800


def _crec(**kw):
    base = {"id": "sha256:" + "ab" * 32, "status": "ok", "entities": {"qr": [], "urls": [], "domains": []},
            "text_coverage": 0.3, "text_chars": 800, "grays": 250, "edges": 0.1, "category": "chat"}
    return {**base, **kw}


@pytest.mark.parametrize("rec,outcome", [
    (_crec(), "quarantine"),
    (_crec(status="failed", status_reason="no_text"), "hold"),
    (_crec(entities={"qr": [], "domains": [], "urls": [{"raw": "x.my", "verified_by": "none"}]}), "hold"),
    (_crec(entities={"qr": [], "domains": [], "urls": [{"raw": "www.ome", "verified_by": "none", "flag": "invalid_tld"}]}), "quarantine"),
    (_crec(entities={"qr": [], "domains": [], "urls": [{"raw": "x.my", "verified_by": "none"}]}, confirmed_by="llm"), "quarantine"),
    (_crec(text_coverage=0.02, text_chars=40), "attach"),                                   # a photo
    (_crec(text_coverage=0.02, text_chars=40, grays=48, edges=0.01), "quarantine"),         # blank loading screen
    (_crec(text_coverage=0.02, text_chars=40, entities={"qr": [{"type": "payment", "payload": "p"}], "urls": [], "domains": []}), "quarantine"),
    (_crec(text_coverage=0.02, text_chars=40, category="system"), "quarantine"),            # home screen
    (_crec(keep=True), "attach"),
])
def test_decide(rec, outcome):
    assert lc.decide(rec)[0] == outcome


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_cleanup_purge_restore_round_trip(sample):
    src, content, env = sample
    _run("ingest", str(src), "--content", str(content), "--commit", env=env)
    code, plan = _run("cleanup", env=env)
    assert code == 0 and plan["data"]["plan"] == {"quarantine": 1} and len(list(src.iterdir())) == 2
    code, res = _run("cleanup", "--commit", env={**env, "SEKERINSHOTTO_NOW": "2026-01-01T00:00:00Z"})
    assert code == 0 and res["data"]["moved"] == {"quarantine": 1}
    left = [p.name for p in src.iterdir()]
    assert left == ["copy.png"]                     # the duplicate copy was never ingested, so never moved
    audit = (content / "AUDIT.md").read_text()
    assert "| quarantined (purged 7 days after) | 1 |" in audit
    code, pl = _run("purge", env={**env, "SEKERINSHOTTO_NOW": "2026-01-07T23:59:59Z"})
    assert pl["data"]["due"] == 0 and pl["data"]["next"][0]["seconds_left"] == 1
    note = next((content / "notes").rglob("*.md"))
    iid = note.stem.split("-")[-1]
    code, r = _run("restore", iid, "--commit", env=env)
    assert r["data"]["restored"] == 1 and len(list(src.iterdir())) == 2
    _run("cleanup", "--commit", env={**env, "SEKERINSHOTTO_NOW": "2026-01-01T00:00:00Z"})
    code, pg = _run("purge", "--commit", env={**env, "SEKERINSHOTTO_NOW": "2026-01-08T00:00:00Z"})
    assert code == 0 and pg["data"]["purged"] == 1
    assert "this note is the only record" in note.read_text()
    code, again = _run("ingest", str(src), "--content", str(content), env=env)
    assert again["data"]["planned"] == 0                                     # purged hash is not resurrected
    code, bad = _run("restore", iid, env=env)
    assert code == 1 and "purged images cannot be restored" in bad["error"]
    assert _run("status", env=env)[1]["data"]["journal"]["intact"]


# ---------------------------------------------------------------- redaction + text commands (phase 5)
from sekerinshotto.redact import redact, redact_qr
from sekerinshotto.commands import _fts_query


@pytest.mark.parametrize("text,out", [
    ("+60 12-256 7486 said hi", "[PHONE] said hi"),
    ("call 012-345 6789", "call [PHONE]"),
    ("IC 900101-14-5678 ok", "IC [IC] ok"),
    ("ref 991399-12-3456", "ref 991399-12-3456"),             # not a valid YYMMDD: a reference number
    ("Prepared by Ahmad Bin Ali", "Prepared by [NAME]"),
    ("Dr. Siti Aminah", "[NAME]"),
    ("Muhammad Amirul Aziz bin Hassan paid", "[NAME] paid"),  # bounded: the sentence survives
    ("NUR ALIA BINTI OSMAN", "[NAME]"),
    ("mail a.b@um.edu.my", "mail [EMAIL]"),
    ("Invoice 2024 total RM 50", "Invoice 2024 total RM 50"),
])
def test_redact(text, out):
    assert redact(text)[0] == out


def test_redact_keeps_lines():
    assert redact("a\nDr. X Y\nb")[0] == "a\n[NAME]\nb"


def test_payment_qr_is_replaced_whole():
    q = redact_qr({"type": "payment", "symbology": "QR", "payload": "000202…NAME…6304ABCD",
                   "merchant_name": "SOMEONE", "merchant_city": "KL"})
    assert q["payload"] == "[PAYMENT QR]" and q["merchant_name"] == "[NAME]" and "SOMEONE" not in json.dumps(q)


def test_fts_query_neutralizes_operators():
    assert _fts_query('AND OR "( hack*') == '"AND" "OR" "hack"'


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_search_show_and_grounded_tag(sample):
    src, content, env = sample
    _run("ingest", str(src), "--content", str(content), "--commit", env=env)
    code, s = _run("search", "register", env=env)
    assert code == 0 and s["data"]["total"] == 1 and "«Register»" in s["data"]["results"][0]["excerpt"]
    iid = s["data"]["results"][0]["id"]
    assert _run("show", iid[:15], env=env)[1]["data"]["urls"][0]["verified_by"] == "qr"
    code, bad = _run("tag", iid, "--category", "event", "--quote", "totally made up words", env=env)
    assert code == 1 and "does not appear" in bad["error"]
    code, short = _run("tag", iid, "--category", "event", env=env)
    assert code == 1 and "--quote" in short["error"]
    code, ok = _run("tag", iid, "--category", "signup", "--quote", "REGISTER AT docs.example", "--commit", env=env)
    assert code == 0 and ok["data"]["note"].startswith("notes/signup/")
    _run("organize", "--commit", env=env)
    shown = _run("show", iid, env=env)[1]["data"]
    assert shown["category"] == "signup" and shown["decided_by"] == "llm" and "quoting" in shown["why"]


# ---------------------------------------------------------------- allowlist (phase 6)
def test_allowlist_verifies_and_corrects(dom):
    allowed = {"hackfest2026.my"}
    r = resolve("hackfest2026.my", dom, set(), allowed)
    assert (r["verified_by"], r["corrected"]) == ("allowed", False)
    r = resolve("hackfest2O26.my", dom, set(), allowed)                         # O read for 0
    assert (r["host"], r["verified_by"], r["corrected"]) == ("hackfest2026.my", "allowed", True)
    assert resolve("unknownsite.my", dom, set(), allowed)["verified_by"] == "none"


@pytest.mark.skipif(sys.platform != "darwin", reason="Apple Vision")
def test_allow_releases_held_image(tmp_path):
    import os
    import zxingcpp  # noqa: F401
    img = Image.new("RGB", (1200, 900), "white")
    ImageDraw.Draw(img).text((40, 300), "Join us at smallclub.my this weekend", fill="black",
                             font=ImageFont.load_default(size=48))
    src = tmp_path / "in"
    src.mkdir()
    img.save(src / "Screenshot_20260101_120000_com_whatsapp_Conversation.png")
    env = {**os.environ, "SEKERINSHOTTO_STATE": str(tmp_path / "state")}
    content = tmp_path / "content"
    code, need = _run("domains", "allow", "smallclub.my", env=env)
    assert code == 1 and "domains update" in need["error"]               # never guesses without the PSL
    doms = tmp_path / "state" / "domains"
    doms.mkdir(parents=True)
    con = _sq.connect(doms / "ranks.sqlite")
    con.execute("CREATE TABLE ranks (domain TEXT PRIMARY KEY, rank INTEGER)")
    con.commit()
    con.close()
    (doms / "psl.dat").write_text("com\nmy\ncom.my\nonrender.com\n")
    (doms / "tlds.txt").write_text("COM\nMY\n")
    _run("ingest", str(src), "--content", str(content), "--commit", "--cleanup", env=env)
    st = _run("status", env=env)[1]["data"]
    assert st["by_source_state"] == {"held": 1}
    assert _run("domains", "suggest", env=env)[1]["data"]["suggestions"][0]["domain"] == "smallclub.my"
    code, bad = _run("domains", "allow", "com.my", env=env)
    assert code == 1 and "public suffix" in bad["error"]
    code, ok = _run("domains", "allow", "27a.onrender.com", env=env)
    assert ok["data"]["would_add"] == ["27a.onrender.com"]                # not all of onrender.com
    code, ok = _run("domains", "allow", "smallclub.my", "--commit", env=env)
    assert code == 0 and ok["data"]["released"] == {"quarantine": 1} and ok["data"]["held_total"] == 0
    note = next((content / "notes").rglob("*.md")).read_text()
    assert "[smallclub.my](https://smallclub.my)" in note and "allowlist" in note
