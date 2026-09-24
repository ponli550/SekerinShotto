"""Deterministic extraction: OCR text, barcodes, URLs, filename metadata.

No model decides anything here. Apple Vision is used as an OS-level sensor
for text and barcodes; everything after that is plain rules.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

EXTRACTOR_VERSION = "12"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".heic", ".webp", ".tif", ".tiff", ".bmp", ".gif"}

FAIL_MIN_CHARS = 3            # fewer chars and no barcode -> failed/no_text
FAIL_MIN_CONFIDENCE = 0.5     # char-weighted mean line confidence below this -> failed/low_confidence


_HEIF_READY = False


def _pil():
    """Pillow with HEIC/HEIF support (iPhone photos). Without it, Vision still reads the text but the image
    fingerprint, visual/diagram metrics and EXIF dates silently came back empty."""
    global _HEIF_READY
    from PIL import Image
    if not _HEIF_READY:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError:
            pass
        _HEIF_READY = True
    return Image


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


# ---- filename metadata (Android: Screenshot_YYYYMMDD_HHMMSS_<package>_<Activity>...) ----
_ANDROID = re.compile(r"^Screenshot_(\d{8})_(\d{6})_(.+)$")


_OTHER_NAMES = [   # (regex, app package or None) -> date groups Y M D h m s
    (re.compile(r"^WhatsApp Image (\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})"), "com.whatsapp"),
    (re.compile(r"^Screenshot (\d{4})-(\d{2})-(\d{2}) at (\d{1,2})\.(\d{2})\.(\d{2})"), "com.apple.macos.screenshot"),
    (re.compile(r"^(?:IMG|VID|PXL|MVIMG)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"), None),
    (re.compile(r"^Screenshot_(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})"), None),
]


def parse_filename(name: str) -> dict:
    stem = Path(name).stem
    for rx, app in _OTHER_NAMES:
        m2 = rx.match(stem)
        if m2:
            y, mo, d, h, mi, sec = (int(x) for x in m2.groups())
            ampm = stem.upper().rstrip()[-2:]                         # macOS: "… at 3.45.12 PM"
            if ampm == "PM" and h < 12:
                h += 12
            elif ampm == "AM" and h == 12:                            # 12.29 AM is just after midnight
                h = 0
            try:
                out = {"captured_at": datetime(y, mo, d, h, mi, sec).strftime("%Y-%m-%dT%H:%M:%S")}
            except ValueError:
                return {}
            return {**out, **({"source_app": app} if app else {})}
    m = _ANDROID.match(stem)
    if not m:
        return {}
    out = {}
    try:
        out["captured_at"] = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        pass
    pkg = []
    for tok in m.group(3).split("_"):
        if not tok or not re.fullmatch(r"[a-z0-9]+", tok):
            break
        pkg.append(tok)
    if len(pkg) >= 2:
        out["source_app"] = ".".join(pkg)
    return out


# iPhone: IMG_1234.HEIC, IMG_E1234.JPG (an edit), "IMG_1234 (1).PNG" (a re-save). The name has no
# date or app, so the source comes from evidence in the file, never from the name alone.
_IPHONE = re.compile(r"^IMG_E?\d{4}(?: \(\d+\))?$", re.I)


def iphone_source(path: Path) -> dict:
    """{"source_app", "camera_model"?} for an iPhone-named file, or {} when the file does not say.
    EXIF Make=Apple -> a camera photo; a PNG with no camera EXIF -> an iOS screenshot (they are PNGs)."""
    if not _IPHONE.match(path.stem):
        return {}
    Image = _pil()
    try:
        with Image.open(path) as im:
            exif, fmt = im.getexif(), (im.format or "").upper()
    except Exception:  # noqa: BLE001
        return {}
    make, model = str(exif.get(271) or "").strip(), str(exif.get(272) or "").strip()
    if make.lower().startswith("apple"):
        return {"source_app": "com.apple.camera", **({"camera_model": model} if model else {})}
    if fmt == "PNG" and not make:
        return {"source_app": "com.apple.ios.screenshot"}
    return {}


# ---- barcodes ----
_EMV_NAMES = {"54": "amount", "59": "merchant_name", "60": "merchant_city"}


def _crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return crc


def parse_emv(payload: str) -> dict | None:
    """EMVCo merchant-presented QR (DuitNow, PayNow, PromptPay, ...).

    Accepted only if the whole string parses as tag-length-value, starts with
    tag 00, ends with tag 63, and the CRC-16/CCITT over everything up to the
    CRC value matches. A prefix check is not enough: real DuitNow codes carry
    format indicator 02, not 01. Returns selected fields, or None.
    """
    tags, i = [], 0
    while i < len(payload):
        tag, ln = payload[i:i + 2], payload[i + 2:i + 4]
        if len(tag) < 2 or not ln.isdigit() or i + 4 + int(ln) > len(payload):
            return None
        tags.append((tag, payload[i + 4:i + 4 + int(ln)], i))
        i += 4 + int(ln)
    if not tags or tags[0][0] != "00" or tags[-1][0] != "63" or len(tags[-1][1]) != 4:
        return None
    crc_at = tags[-1][2] + 4
    if f"{_crc16_ccitt(payload[:crc_at].encode()):04X}" != tags[-1][1].upper():
        return None
    return {_EMV_NAMES[t]: v for t, v, _ in tags if t in _EMV_NAMES}


def classify_qr(payload: str) -> tuple[str, str, dict]:
    """(subtype, payload_to_store, extra). Wi-Fi passwords are redacted here, before anything is written."""
    p = payload.strip()
    low = p.lower()
    if low.startswith(("http://", "https://")):
        return "url", p, {}
    if low.startswith("wifi:"):
        return "wifi", re.sub(r"(P:)((?:\\.|[^;])*)", r"\1<redacted>", p, flags=re.I), {}
    emv = parse_emv(p)
    if emv is not None:
        return "payment", p, emv
    if low.startswith(("begin:vcard", "mecard:")):
        return "contact", p, {}
    for scheme in ("mailto", "tel", "smsto", "geo"):
        if low.startswith(scheme + ":"):
            return scheme, p, {}
    return "text", p, {}


# ---- URLs from OCR text ----
_TLDS = ("com|net|org|io|app|dev|ai|co|me|my|sg|id|uk|us|gov|edu|info|biz|ly|gg|xyz|tv|so|to|"
         "site|online|store|shop|link|page|tech|cloud|live|news|blog|in|jp|cn|de|fr|au|gle|gl|sh|lk|ph|th|vn|tw|kr|hk|nz|ca|eu|es|it|nl|ch|at|be|se|no|dk|fi|pl|ru|br|mx|ar")
_URL_RE = re.compile(
    r"(?:https?://[^\s<>\"'`]+)"
    r"|(?:\bwww\.[^\s<>\"'`]+)"
    rf"|(?:(?<![@\w.-])[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.(?:{_TLDS})\b(?:/[^\s<>\"'`]*)?)",
    re.I,
)
_TRAIL = ".,;:!?)]}>'\"’”…"


def _norm_url(u: str) -> str:
    u = u.strip().rstrip(_TRAIL)
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u
    parts = urlsplit(u)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    q = ("?" + parts.query) if parts.query else ""
    return f"{parts.scheme.lower()}://{host}{path}{q}"


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


_CONT = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")
_DATEISH = re.compile(r"^[\d/.\-:]+$")
_ELLIPSIS = ("…", "...")
_STRAY_END = "<>|"            # OCR sometimes reads a final "/" as "<"


def _continues(url: str, nxt: str, cur_box, nxt_box) -> bool:
    """Is `nxt` the wrapped remainder of a URL that ended at the end of the previous line?"""
    t = nxt.strip().rstrip(_STRAY_END)
    if len(t) < 2 or not _CONT.match(t) or not re.search(r"[A-Za-z]", t) or _DATEISH.match(t):
        return False
    if re.match(r"(?i)https?://|www\.", t):
        return False                                   # a new URL, not a continuation
    if cur_box and nxt_box:
        gap = nxt_box[1] - (cur_box[1] + cur_box[3])
        if gap > 1.5 * cur_box[3] or gap < -0.5 * cur_box[3]:
            return False
    return t[0] in "/-" or url[-1] in "/-=?&_" or "/" in t or "-" in t


def find_urls_in_lines(lines) -> list[dict]:
    """lines: [(text, conf, box|None), ...] in reading order.
    -> [{raw, url, confidence, joined, truncated}] deduplicated by normalized URL."""
    seen, out = set(), []
    for i, line in enumerate(lines):
        text, conf = line[0], line[1]
        box = line[2] if len(line) > 2 else None
        for m in _URL_RE.finditer(text):
            raw = m.group(0)
            if "@" in raw.split("//")[-1].split("/")[0]:
                continue                               # user@host: an email address
            after = text[m.end():m.end() + 3]
            truncated = raw.endswith(_ELLIPSIS) or after.startswith(_ELLIPSIS)
            raw = raw.rstrip(_TRAIL)
            joined = 0
            at_end = text[m.end():].strip() == ""
            j = i
            while at_end and not truncated and j + 1 < len(lines):
                nxt = lines[j + 1]
                if not _continues(raw, nxt[0], lines[j][2] if len(lines[j]) > 2 else None,
                                  nxt[2] if len(nxt) > 2 else None):
                    break
                piece = nxt[0].strip().rstrip(_STRAY_END)
                truncated = piece.endswith(_ELLIPSIS)
                raw = raw + piece.rstrip(_TRAIL)
                conf = min(conf, nxt[1])
                joined += 1
                j += 1
            norm = _norm_url(raw)
            if not domain_of(norm) or norm in seen:
                continue
            seen.add(norm)
            out.append({"raw": raw, "url": norm, "confidence": round(conf, 2),
                        "joined": joined, "truncated": truncated})
    return out


def find_urls(text: str) -> list[tuple[str, str]]:
    """[(raw, normalized)] from plain text (no geometry; lines still joined by text rules)."""
    return [(u["raw"], u["url"]) for u in find_urls_in_lines([(t, 1.0) for t in text.splitlines()])]


def raw_host(raw: str) -> str:
    """Host exactly as OCR read it, case and odd characters preserved (|1nk.dev, Inkd.in)."""
    r = re.sub(r"(?i)^https?://", "", raw)
    return re.split(r"[/?#]", r, maxsplit=1)[0].split(":")[0]


# ---- content fingerprint for duplicate grouping ----
CHROME_TOP, CHROME_BOTTOM = 0.045, 0.96      # status bar / gesture bar strips, as fractions of height
SIG_SIZE = 64
_MERSENNE = (1 << 61) - 1
_PERMS = [((i * 0x9E3779B97F4A7C15 + 1) % _MERSENNE | 1, (i * 0xC2B2AE3D27D4EB4F + 7) % _MERSENNE)
          for i in range(1, SIG_SIZE + 1)]
_TOKEN = re.compile(r"[a-z][a-z0-9]{2,}")
_NUMBER = re.compile(r"\b\d[\d.,:%/]*\d\b|\b\d\b")   # figures distinguish same-template screens (sleep reports)


def content_tokens(lines) -> set[str]:
    """Words from the content area only: the status bar (clock, battery) would make every
    screenshot look alike, so lines in the top/bottom strips are dropped."""
    out = set()
    for line in lines:
        box = line[2] if len(line) > 2 else None
        if box and (box[1] < CHROME_TOP or box[1] > CHROME_BOTTOM):
            continue
        out.update(_TOKEN.findall(line[0].lower()))
        out.update("#" + n for n in _NUMBER.findall(line[0]))
    return out


def token_hashes(tokens: set[str]) -> list[int]:
    return sorted(int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), "big") for t in tokens)


def minhash(hs: list[int]) -> list[int]:
    """64-value MinHash of token hashes; used only to find candidate pairs quickly (LSH)."""
    if not hs:
        return []
    return [min((a * h + b) % _MERSENNE for h in hs) for a, b in _PERMS]


def jaccard_est(a: list[int], b: list[int]) -> float:
    if not a or not b:
        return 0.0
    return sum(x == y for x, y in zip(a, b)) / len(a)


_EDGE_MASK = bytes(1 if i > 60 else 0 for i in range(256))


def _long_runs(mask: bytes, width: int, height: int, min_run: int, vertical: bool) -> int:
    """How many rows (or columns) contain an unbroken edge run of at least min_run pixels."""
    needle = b"\x01" * min_run
    if not vertical:
        return sum(1 for y in range(height) if needle in mask[y * width:(y + 1) * width])
    return sum(1 for x in range(width) if needle in mask[x::width])


def visual_metrics(path: Path, lines) -> dict:
    """Image measurements used by cleanup (FORMAT §6):
    text coverage / gray levels / edges -> photos; long straight lines + low saturation -> diagrams."""
    from PIL import ImageFilter
    Image = _pil()
    content = [l for l in lines if len(l) > 2 and CHROME_TOP <= l[2][1] <= CHROME_BOTTOM]
    out = {"text_coverage": round(sum(l[2][2] * l[2][3] for l in content) / (CHROME_BOTTOM - CHROME_TOP), 4),
           "grays": 0, "edges": 0.0, "hlines": 0, "vlines": 0, "saturation": 0.0}
    try:
        with Image.open(path) as im:
            g = im.crop((0, int(im.height * CHROME_TOP), im.width, int(im.height * CHROME_BOTTOM)))
            small = g.convert("L").resize((300, 600))
            e = small.filter(ImageFilter.FIND_EDGES).tobytes()
            out["edges"] = round(sum(1 for p in e if p > 40) / (300 * 600), 4)
            out["grays"] = len(set(small.resize((60, 120)).tobytes()))
            mask = e.translate(_EDGE_MASK)
            out["hlines"] = _long_runs(mask, 300, 600, 90, vertical=False)      # >= 30 % of the width
            out["vlines"] = _long_runs(mask, 300, 600, 90, vertical=True)       # >= 15 % of the height
            hsv = g.convert("HSV").resize((150, 300)).tobytes()
            out["saturation"] = round(sum(1 for i in range(1, len(hsv), 3) if hsv[i] > 80) / (150 * 300), 4)
    except Exception:  # noqa: BLE001 - measurements are optional
        pass
    return out


def dhash_of(path: Path) -> str | None:
    Image = _pil()
    try:
        with Image.open(path) as im:
            w, h = im.size
            im = im.crop((0, int(h * CHROME_TOP), w, int(h * CHROME_BOTTOM))).convert("L").resize((9, 8))
            px = list(im.tobytes())
    except Exception:  # noqa: BLE001 - a fingerprint is optional
        return None
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return f"{bits:016x}"


def hamming(a: str | None, b: str | None) -> int:
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# ---- the extraction record ----
@dataclass
class Extraction:
    id: str
    path: Path
    width: int = 0
    height: int = 0
    bytes: int = 0
    captured_at: str | None = None
    source_app: str | None = None
    lines: list[tuple[str, float]] = field(default_factory=list)
    barcodes: list[dict] = field(default_factory=list)
    urls: list[dict] = field(default_factory=list)
    status: str = "ok"
    status_reason: str | None = None
    ocr_confidence: float | None = None
    sig: list[int] = field(default_factory=list)      # MinHash of content tokens (status/nav bars excluded)
    toks: list[int] = field(default_factory=list)     # exact token hashes, for exact similarity on candidates
    content_tokens: int = 0
    dhash: str | None = None                          # 64-bit difference hash of the content region
    extractor_version: str = EXTRACTOR_VERSION        # kept from the record when a note is re-rendered
    text_coverage: float = 0.0                        # share of the content area covered by text boxes
    grays: int = 0                                    # distinct gray levels in a 60x120 thumbnail
    edges: float = 0.0                                # share of strong-edge pixels
    hlines: int = 0                                   # rows with a long horizontal edge (tables, boxes)
    vlines: int = 0                                   # columns with a long vertical edge
    saturation: float = 0.0                           # share of strongly coloured pixels (posters are high)
    # lifecycle (FORMAT §6); set by cleanup, carried through re-renders
    source_state: str = "present"
    stored_path: str | None = None
    quarantined_at: str | None = None
    purge_after: str | None = None
    purged_at: str | None = None
    attachment: str | None = None
    hold_reason: str | None = None
    attempts: int = 0
    keep: bool = False
    confirmed_by: str | None = None
    copies: list = field(default_factory=list)        # byte-identical files elsewhere: {path, state, ...}
    secrets: list = field(default_factory=list)       # kinds of credentials scrubbed at extraction
    camera_model: str | None = None                   # EXIF Model of a camera photo ("iPhone 15 Pro")
    code: dict | None = None                          # {lang, score, why, imports, code_lines} when code (code.py)
    chrome_top_n: int = 0                             # OCR lines in the status-bar strip (reading order: first)
    chrome_bottom_n: int = 0                          # OCR lines in the gesture-bar strip (last)
    elapsed_ms: int = 0

    @property
    def text(self) -> str:
        return "\n".join(t[0] for t in self.lines)

    @property
    def domains(self) -> list[str]:
        return sorted({domain_of(u["url"]) for u in self.urls})


_FW = None
_FW_LOCK = __import__("threading").Lock()


def _frameworks():
    """Resolve every PyObjC symbol once, under a lock. PyObjC's lazy constant
    loading is not thread-safe: first-touch from several worker threads races
    and raises KeyError inside objc._lazyimport."""
    global _FW
    with _FW_LOCK:
        if _FW is None:
            import Quartz
            import Vision
            from Foundation import NSURL
            _FW = {
                "url": NSURL.fileURLWithPath_,
                "src": Quartz.CGImageSourceCreateWithURL,
                "img": Quartz.CGImageSourceCreateImageAtIndex,
                "w": Quartz.CGImageGetWidth, "h": Quartz.CGImageGetHeight,
                "TextReq": Vision.VNRecognizeTextRequest,
                "BarReq": Vision.VNDetectBarcodesRequest,
                "Handler": Vision.VNImageRequestHandler,
                "accurate": Vision.VNRequestTextRecognitionLevelAccurate,
            }
    return _FW


def _vision_read(path: Path, correction: bool = True, barcodes: bool = True):
    fw = _frameworks()
    src = fw["src"](fw["url"](str(path)), None)
    if src is None:
        raise ValueError("not a readable image")
    img = fw["img"](src, 0, None)
    if img is None:
        raise ValueError("not a readable image")
    text_req = fw["TextReq"].alloc().init()
    text_req.setRecognitionLevel_(fw["accurate"])
    # Language correction turns `uid` into `aid` and `:=` into `=`: code is re-read without it.
    text_req.setUsesLanguageCorrection_(correction)
    text_req.setRecognitionLanguages_(["en-US", "ms-MY"])
    bar_req = fw["BarReq"].alloc().init()
    handler = fw["Handler"].alloc().initWithCGImage_options_(img, None)
    ok, err = handler.performRequests_error_([text_req, bar_req] if barcodes else [text_req], None)
    if not ok:
        raise RuntimeError(f"Vision failed: {err}")
    lines = []
    for obs in text_req.results() or []:
        cand = obs.topCandidates_(1)
        if cand:
            bb = obs.boundingBox()          # normalized, origin bottom-left
            box = (bb.origin.x, 1 - bb.origin.y - bb.size.height, bb.size.width, bb.size.height)
            lines.append((str(cand[0].string()), float(cand[0].confidence()), box))
    lines.sort(key=lambda t: (round(t[2][1], 3), t[2][0]))      # reading order: top, then left
    bars = []
    for obs in (bar_req.results() or []) if barcodes else []:
        payload = obs.payloadStringValue()
        bars.append({"symbology": str(obs.symbology()).replace("VNBarcodeSymbology", ""),
                     "payload": str(payload) if payload is not None else None})
    return fw["w"](img), fw["h"](img), lines, bars


def _zxing_read(path: Path) -> list[dict]:
    import zxingcpp
    Image = _pil()
    with Image.open(path) as im:
        return [{"symbology": str(b.format).split(".")[-1], "payload": b.text}
                for b in zxingcpp.read_barcodes(im) if b.text]


def extract(path: Path, file_id: str | None = None) -> Extraction:
    """Never raises: an image that breaks extraction comes back as status=failed."""
    try:
        return _extract(path, file_id)
    except Exception as e:  # noqa: BLE001 - one bad image must not kill a 2000-image batch
        ex = Extraction(id=file_id or sha256_file(path), path=path, bytes=path.stat().st_size)
        ex.__dict__.update(parse_filename(path.name))
        ex.status, ex.status_reason = "failed", f"internal: {type(e).__name__}: {e}"[:200]
        return ex


def _extract(path: Path, file_id: str | None = None) -> Extraction:
    import time
    t0 = time.perf_counter()
    ex = Extraction(id=file_id or sha256_file(path), path=path, bytes=path.stat().st_size)
    ex.__dict__.update(parse_filename(path.name))
    if not ex.source_app:
        ex.__dict__.update(iphone_source(path))
    if not ex.captured_at:
        ex.captured_at = fallback_date(path)
    try:
        ex.width, ex.height, ex.lines, bars = _vision_read(path)
    except (ValueError, RuntimeError) as e:
        ex.status, ex.status_reason = "failed", f"unreadable: {e}"
        ex.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return ex
    from .code import detect, rebuild
    if detect(ex.text):
        # Code: re-read without language correction and rebuild each row with its on-screen indentation.
        raw = rebuild(_vision_read(path, correction=False, barcodes=False)[2])
        ex.code = detect("\n".join(t[0] for t in raw))
        if ex.code:
            ex.lines = raw
        else:
            ex.code = detect(ex.text)                  # the uncorrected read lost it: keep the first reading
    undecoded = [b for b in bars if b["payload"] is None]
    decoded = [b for b in bars if b["payload"]]
    if not decoded:
        decoded = _zxing_read(path)            # second decoder only when Vision found nothing usable
    # Credentials are scrubbed HERE, before any note, manifest or index row can contain them.
    from .secrets import scrub
    clean = []
    for line in ex.lines:
        t, k = scrub(line[0])
        ex.secrets += k
        clean.append((t, *line[1:]))
    ex.lines = clean
    for b in decoded:
        b["payload"], k = scrub(b["payload"])
        ex.secrets += k

    qr_urls = set()
    for b in decoded:
        subtype, payload, extra = classify_qr(b["payload"])
        rec = {"symbology": b["symbology"], "type": subtype, "payload": payload, **extra}
        ex.barcodes.append(rec)
        if subtype == "url":
            norm = _norm_url(payload)
            qr_urls.add(norm)
            ex.urls.append({"raw": payload, "url": norm, "verified_by": "qr", "confidence": 1.0})

    for u in find_urls_in_lines(ex.lines):
        if u["url"] in qr_urls:
            continue
        ex.urls.append({**u, "verified_by": "none"})

    ex.toks = token_hashes(content_tokens(ex.lines))
    ex.chrome_top_n = sum(1 for l in ex.lines if len(l) > 2 and l[2][1] < CHROME_TOP)
    ex.chrome_bottom_n = sum(1 for l in ex.lines if len(l) > 2 and l[2][1] > CHROME_BOTTOM)
    ex.content_tokens, ex.sig, ex.dhash = len(ex.toks), minhash(ex.toks), dhash_of(path)
    vm = visual_metrics(path, ex.lines)
    ex.text_coverage, ex.grays, ex.edges = vm["text_coverage"], vm["grays"], vm["edges"]
    ex.hlines, ex.vlines, ex.saturation = vm["hlines"], vm["vlines"], vm["saturation"]

    chars = sum(len(t[0]) for t in ex.lines)
    if chars:
        ex.ocr_confidence = round(sum(len(t[0]) * t[1] for t in ex.lines) / chars, 3)
    if undecoded and not decoded:
        ex.status, ex.status_reason = "failed", "qr_undecodable"
    elif chars < FAIL_MIN_CHARS and not decoded:
        ex.status, ex.status_reason = "failed", "no_text"
    elif ex.ocr_confidence is not None and ex.ocr_confidence < FAIL_MIN_CONFIDENCE and not decoded:
        ex.status, ex.status_reason = "failed", "low_confidence"
    ex.elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return ex


def iter_images(src: Path, limit: int | None = None):
    files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
    n = 0
    for p in files:
        if p.suffix.lower() in IMAGE_EXTS and not p.name.startswith("."):
            yield p
            n += 1
            if limit and n >= limit:
                return


def fallback_date(path: Path) -> str | None:
    """When the filename carries no date: EXIF DateTimeOriginal, else the file's modification time."""
    Image = _pil()
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)       # DateTimeOriginal, DateTime
            if raw:
                return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S").strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:  # noqa: BLE001
        pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%dT%H:%M:%S")
    except OSError:
        return None
