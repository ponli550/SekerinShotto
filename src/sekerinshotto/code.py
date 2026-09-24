"""Code in screenshots: is it code, which language, what does it import, and its layout.

No model: weighted signatures per language. A signature is a regex over the OCR text (multiline,
case-sensitive: keywords are). The best-scoring language wins when it clears MIN_SCORE; TypeScript
beats JavaScript only on TypeScript-only evidence (types, interfaces).
"""
from __future__ import annotations

import re
from statistics import median

M = re.M
_S = lambda rx, w: (re.compile(rx, M), w)                         # noqa: E731

LANGS: dict[str, list[tuple[re.Pattern, int]]] = {
    "python": [_S(r"^\s*def \w+\(.*\)\s*(->\s*[\w\[\], .]+)?:\s*$", 3), _S(r"^\s*from [\w.]+ import [\w*]", 3),
               _S(r"^\s*import \w[\w.]*(\s+as \w+)?\s*$", 2), _S(r"^\s*class \w+(\(.*\))?:\s*$", 3),
               _S(r"^\s*(if|elif|for|while|with|try|except)\b.*:\s*$", 2), _S(r"\bself\.\w", 2),
               _S(r"^\s*@\w[\w.]*(\(.*\))?\s*$", 1), _S(r"\b(None|True|False)\b", 1), _S(r"\bprint\(", 1),
               _S(r"^\s*return\b", 1), _S(r"\bf\"[^\"]*\{", 2), _S(r"__\w+__", 2)],
    "go": [_S(r"^package \w+\s*$", 4), _S(r"\bfunc (\(\w+ \*?\w+\) )?\w+\(", 3), _S(r"\w :?= ", 0),
           _S(r":= ", 2), _S(r"\bif err != nil\b", 4), _S(r"\bfmt\.\w+\(", 3), _S(r"^import \($", 3),
           _S(r"\b(chan|go func|defer)\b", 2), _S(r"\[\]\w+\{", 2), _S(r"\bstruct \{", 2)],
    "typescript": [_S(r"\binterface \w+(<.*>)? \{", 3), _S(r"\w\??: (string|number|boolean|any|void|unknown|never)\b", 3),
                   _S(r"^\s*(export )?type \w+(<.*>)? = ", 3), _S(r"\b(as const|readonly|implements|enum \w+ \{)", 2),
                   _S(r"\):\s*(Promise<|string|number|boolean|void)", 3), _S(r"<\w+(\[\])?>\(", 1)],
    "javascript": [_S(r"^\s*(export )?(const|let|var) \w+ = ", 2), _S(r"=> ?[{(\w]", 2),
                   _S(r"^\s*import .* from ['\"][@\w./-]+['\"];?\s*$", 3), _S(r"\brequire\(['\"]", 3),
                   _S(r"\bconsole\.log\(", 3), _S(r"\bmodule\.exports\b", 3), _S(r"^\s*(async )?function \w+\(", 2),
                   _S(r"\b(document|window)\.\w", 2), _S(r"\bawait \w", 1), _S(r"===", 2)],
    "rust": [_S(r"\bfn \w+(<.*>)?\(", 3), _S(r"\blet mut\b", 3), _S(r"^\s*use \w+(::\w+)+", 3), _S(r"\bimpl\b", 2),
             _S(r"\bprintln!\(", 4), _S(r"-> (Result|Option)<", 3), _S(r"&(mut )?self\b", 3), _S(r"\w::\w", 1)],
    "java": [_S(r"\bpublic (static )?(final )?(class|void|interface)\b", 3), _S(r"\bSystem\.out\.print", 4),
             _S(r"\bprivate (final )?\w+(<.*>)? \w+;", 3), _S(r"@Override\b", 3), _S(r"^import java\.", 4),
             _S(r"\bnew \w+(<.*>)?\(", 1)],
    "kotlin": [_S(r"\bfun \w+\(", 3), _S(r"^\s*val \w+(: \w+)? = ", 2), _S(r"\bdata class\b", 3), _S(r"\bprintln\(", 1)],
    "swift": [_S(r"\bfunc \w+\(.*\) -> ", 3), _S(r"^import (SwiftUI|UIKit|Foundation)\s*$", 4), _S(r"\bguard let\b", 3),
              _S(r"^\s*(var|let) \w+: \w+", 2), _S(r"\bstruct \w+: View\b", 4)],
    "c": [_S(r"^#include [<\"]", 4), _S(r"\bint main\(", 3), _S(r"\bprintf\(", 2), _S(r"\w->\w", 1)],
    "cpp": [_S(r"\bstd::", 4), _S(r"^#include <(iostream|vector|string|map)>", 4), _S(r"\bcout <<", 4),
            _S(r"\btemplate ?<", 3)],
    "csharp": [_S(r"^using System", 4), _S(r"^namespace \w", 3), _S(r"\bConsole\.WriteLine\(", 4),
               _S(r"\bpublic (async )?(Task|void|string|int)\b", 2)],
    "php": [_S(r"<\?php", 5), _S(r"\$\w+ = ", 2), _S(r"\becho \$", 3), _S(r"->\w+\(", 1)],
    "ruby": [_S(r"^\s*def \w+[!?]?(\(.*\))?\s*$", 2), _S(r"^\s*end\s*$", 2), _S(r"\bputs ", 2),
             _S(r"^require ['\"]", 3), _S(r"\bdo \|\w+\|", 3)],
    "sql": [_S(r"(?i)\bselect\b.+\bfrom\b", 3), _S(r"(?i)\binsert into\b", 4), _S(r"(?i)\bcreate table\b", 4),
            _S(r"(?i)\b(inner |left |right )?join\b.+\bon\b", 2), _S(r"(?i)\bwhere\b.+=", 1),
            _S(r"(?i)\b(group|order) by\b", 2)],
    "shell": [_S(r"^#!/(usr/)?bin/(env )?(ba|z)?sh", 5),
              _S(r"^\s*[$%❯] ", 2),
              _S(r"^\s*([$%❯] )?(sudo|brew|apt(-get)?|npm|npx|pnpm|yarn|pip3?|uv|git|curl|wget|docker|kubectl|"
                 r"cd|ls|mkdir|chmod|export|ssh|make|go (run|build|mod)|cargo)\b", 2),
              _S(r" && ", 1), _S(r"\| ?(grep|awk|sed|xargs)\b", 2)],
    "dockerfile": [_S(r"^(FROM|RUN|COPY|CMD|ENTRYPOINT|WORKDIR|EXPOSE|ENV|ARG) ", 3)],
    "yaml": [_S(r"^(apiVersion|kind|services|jobs|steps|on):", 3), _S(r"^\s*- (name|uses|run): ", 3),
             _S(r"^\s*[\w-]+: [\w\"'./-]+\s*$", 1)],
    "json": [_S(r"^\s*\"[\w-]+\": [\"\d\[{tfn]", 2), _S(r"^\s*[{\[]\s*$", 1)],
    "html": [_S(r"<(html|head|body|div|span|script|a|p|ul|li)\b[^>]*>", 2), _S(r"</\w+>", 2), _S(r"<!DOCTYPE", 5)],
    "css": [_S(r"^\s*[.#]?[\w-]+(\s*[,>]\s*[.#]?[\w-]+)*\s*\{\s*$", 2), _S(r"^\s*[\w-]+: [^;]+;\s*$", 2),
            _S(r"@media\b", 3)],
}
# A language with only weak, generic evidence (a colon, a `new`) is not claimed.
MIN_SCORE = 5
# The share of lines that must look like code (ends in a bracket/colon/semicolon, has an operator, is a
# comment or starts with a keyword) for an image to be treated as code at all.
_CODE_LINE = re.compile(r"[{}\[\]();:,]\s*$|^\s*(#|//|/\*|\*|--)|\s(=|==|=>|:=|->|\+=|&&|\|\|)\s|^\s*[$%❯] |"
                        r"^\s*(def|class|func|fn|fun|import|from|package|return|if|for|while|const|let|var|"
                        r"public|private|use|SELECT|FROM|WHERE|RUN|FROM)\b")
MIN_CODE_LINES = 0.4


def detect(text: str) -> dict | None:
    """{"lang", "score", "why", "imports"} when the text reads as code, else None."""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    share = sum(1 for l in lines if _CODE_LINE.search(l)) / len(lines)
    if share < MIN_CODE_LINES:
        return None
    scores, why = {}, {}
    for lang, sigs in LANGS.items():
        s, w = 0, []
        for rx, weight in sigs:
            if weight and (m := rx.search(text)):
                s += weight
                w.append(m.group(0).strip()[:40])
        scores[lang], why[lang] = s, w
    if scores["typescript"] >= 3:                       # TS is a superset: JS evidence counts for it
        scores["typescript"] += scores["javascript"]
    if scores["cpp"]:
        scores["cpp"] += scores["c"]
    lang = max(scores, key=lambda k: scores[k])
    if scores[lang] < MIN_SCORE:
        return None
    return {"lang": lang, "score": scores[lang], "why": why[lang][:4], "code_lines": round(share, 2),
            "imports": imports(text, lang)}


_IMPORTS = {
    "python": [r"^\s*from ([\w]+)[\w.]* import", r"^\s*import ([\w]+)"],
    "go": [r"^\s*(?:import )?\"([\w.-]+(?:/[\w.-]+){0,2})\"\s*$"],
    "typescript": [r"from ['\"]((?:@[\w-]+/)?[\w-]+)", r"require\(['\"]((?:@[\w-]+/)?[\w-]+)"],
    "javascript": [r"from ['\"]((?:@[\w-]+/)?[\w-]+)", r"require\(['\"]((?:@[\w-]+/)?[\w-]+)"],
    "rust": [r"^\s*use (\w+)::", r"^\s*extern crate (\w+)"],
    "java": [r"^import (?:static )?([\w]+\.[\w]+)"],
    "kotlin": [r"^import ([\w]+\.[\w]+)"],
    "swift": [r"^import (\w+)"],
    "c": [r"^#include [<\"]([\w./]+)[>\"]"], "cpp": [r"^#include [<\"]([\w./]+)[>\"]"],
    "csharp": [r"^using ([\w.]+);"], "php": [r"^use ([\w\\]+)"], "ruby": [r"^require ['\"]([\w/-]+)"],
}


def imports(text: str, lang: str) -> list[str]:
    """Imported packages, in order, deduplicated: the stack the screenshot is about."""
    hits = sorted((m.start(), m.group(1)) for rx in _IMPORTS.get(lang, []) for m in re.finditer(rx, text, M))
    out: list[str] = []
    for _pos, name in hits:
        if name not in out and not name.startswith("."):
            out.append(name)
    return out[:12]


# ---------------------------------------------------------------- layout
# Vision renders operator ligatures and typographic quotes as the glyph it saw; code needs the ASCII.
_GLYPHS = str.maketrans({"→": "->", "⇒": "=>", "≠": "!=", "≤": "<=", "≥": ">=", "≡": "===", "“": '"', "”": '"',
                         "‘": "'", "’": "'", "—": "--", "…": "...", "\u00a0": " "})
# `FastAPI ()` -> `FastAPI()`, but `if (x)`, `return (a)` and `function ()` keep their space.
_CALL_SPACE = re.compile(r"\b(?!(?:if|for|while|switch|return|and|or|not|in|catch|with|elif|typeof|await|yield|"
                         r"function|await|sizeof|case|else|do|of|match|select|from|join|on|where|values)\b)"
                         r"([A-Za-z_]\w*) \((?=[\w)\"'\[{*&-]|$)")


# Vision sometimes keeps a stray half of the ligature: "-→", "→>", "=⇒".
_ARROWS = [(re.compile(r"-?→>?"), "->"), (re.compile(r"=?⇒>?"), "=>")]


def normalize(line: str) -> str:
    for rx, rep in _ARROWS:
        line = rx.sub(rep, line)
    return _CALL_SPACE.sub(r"\1(", line.translate(_GLYPHS))


def _char_width(obs) -> float | None:
    ws = [box[2] / len(t) for t, _c, box in obs if len(t.strip()) >= 4]
    return median(ws) if ws else None


def rebuild(obs: list[tuple]) -> list[tuple]:
    """Vision observations (text, conf, (x, y, w, h)) -> one line per visual row, with the indentation the
    text had on screen. Rows are observations whose vertical centres overlap; within a row, the gap between
    observations becomes spaces. Indentation is the offset from the leftmost row start in character widths,
    snapped to the file's indent unit (2 or 4). An IDE's line-number gutter is dropped."""
    obs = [o for o in obs if len(o) > 2 and o[0].strip()]
    if not obs:
        return obs
    cw = _char_width(obs)
    rows: list[list[tuple]] = []
    for o in sorted(obs, key=lambda o: o[2][1]):
        cy = o[2][1] + o[2][3] / 2
        if rows:
            last = rows[-1]
            top = min(r[2][1] for r in last)
            bot = max(r[2][1] + r[2][3] for r in last)
            if top <= cy <= bot:
                last.append(o)
                continue
        rows.append([o])
    rows = [sorted(r, key=lambda o: o[2][0]) for r in rows]
    # a gutter: most rows start with a separate number-only observation, and the numbers climb
    nums = [int(r[0][0]) for r in rows if len(r) > 1 and r[0][0].strip().isdigit()]
    if len(nums) >= max(3, 0.6 * len(rows)) and nums == sorted(nums):
        rows = [r[1:] if len(r) > 1 and r[0][0].strip().isdigit() else r for r in rows]
    if cw is None or cw <= 0:
        return [(normalize(" ".join(o[0] for o in r)), min(o[1] for o in r), r[0][2]) for r in rows]
    left = min(r[0][2][0] for r in rows)
    cols = [max(0.0, (r[0][2][0] - left) / cw) for r in rows]
    pos = [c for c in cols if c >= 1.5]
    unit = 2
    if pos and sum(1 for c in pos if abs(c / 4 - round(c / 4)) * 4 <= 1) >= 0.7 * len(pos):
        unit = 4
    out = []
    for r, c in zip(rows, cols):
        indent = int(round(c / unit) * unit) if c >= 1.5 else 0
        text = r[0][0].strip()
        for prev, o in zip(r, r[1:]):
            gap = o[2][0] - (prev[2][0] + prev[2][2])
            text += " " * max(1, int(round(gap / cw))) + o[0].strip()
        x0, y0 = r[0][2][0], min(o[2][1] for o in r)
        x1, y1 = max(o[2][0] + o[2][2] for o in r), max(o[2][1] + o[2][3] for o in r)
        out.append((" " * indent + normalize(text), min(o[1] for o in r), (x0, y0, x1 - x0, y1 - y0)))
    return out
