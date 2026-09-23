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
    assert code == 0 and d["written"] == 1 and d["urls"] == {"from_qr": 1, "from_ocr": 1}
    note = next(content.rglob("*.md")).read_text()
    assert "[q.example.com/abc](https://q.example.com/abc) · from QR" in note
    assert "`docs.example.com/form` · read by OCR, unverified" in note
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
    assert _run("status", env=env)[1]["data"]["urls_by_verification"] == {"none": 1, "qr": 1}


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
