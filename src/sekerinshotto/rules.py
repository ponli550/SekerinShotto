"""Config-driven, explainable classification. First matching rule wins."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .contract import ToolError

UNCATEGORIZED = "uncategorized"
_CATEGORY = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


@dataclass
class Rule:
    category: str
    mode: str = "any"
    apps: list[str] = field(default_factory=list)
    qr_types: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    text: list[re.Pattern] = field(default_factory=list)
    text_min: int = 1
    also: list[re.Pattern] = field(default_factory=list)
    code_langs: list[str] = field(default_factory=list)   # detected code language; "*" is any

    def match(self, app: str | None, qr_types: set[str], domains: list[str], text: str,
              code: dict | None = None) -> str | None:
        """The reason this rule matched, or None."""
        checks = []
        if self.code_langs:
            lang = (code or {}).get("lang")
            hit = lang and ("*" in self.code_langs or lang in self.code_langs)
            checks.append(f"code {lang} ({', '.join(repr(w) for w in (code.get('why') or [])[:2])})" if hit else None)
        if self.apps:
            hit = next((p for p in self.apps if app and app.startswith(p)), None)
            checks.append(f"app {app}" if hit else None)
        if self.qr_types:
            hit = sorted(qr_types & set(self.qr_types))
            checks.append(f"QR {', '.join(hit)}" if hit else None)
        if self.domains:
            hit = sorted({d for d in domains for s in self.domains if d == s or d.endswith("." + s)})
            checks.append(f"domain {', '.join(hit)}" if hit else None)
        if self.text:
            found = []
            for rx in self.text:
                m = rx.search(text)
                if m:
                    found.append(m.group(0).strip())
            checks.append(f"text {', '.join(repr(f) for f in found[:3])}" if len(found) >= self.text_min else None)
        ok = [c for c in checks if c]
        if not checks or (self.mode == "all" and len(ok) != len(checks)) or not ok:
            return None
        for rx in self.also:
            m = rx.search(text)
            if not m:
                return None
            ok.append(f"and {m.group(0).strip()!r}")
        return "; ".join(ok)


def _compile(raw: dict, src: str) -> list[Rule]:
    rules = []
    for i, r in enumerate(raw.get("rule", []), 1):
        cat = r.get("category", "")
        if not _CATEGORY.match(cat):
            raise ToolError(f"{src}: rule {i} has invalid category {cat!r} (lowercase letters, digits, hyphens)")
        if r.get("mode", "any") not in ("any", "all"):
            raise ToolError(f"{src}: rule {i} mode must be 'any' or 'all'")
        try:
            rules.append(Rule(
                category=cat, mode=r.get("mode", "any"), apps=list(r.get("apps", [])),
                qr_types=list(r.get("qr_types", [])), domains=[d.lower() for d in r.get("domains", [])],
                text=[re.compile(x, re.I) for x in r.get("text", [])], text_min=int(r.get("text_min", 1)),
                also=[re.compile(x, re.I) for x in r.get("also", [])],
                code_langs=list(r.get("code_langs", []))))
        except re.error as e:
            raise ToolError(f"{src}: rule {i} ({cat}) has a bad regex: {e}")
    return rules


def load(state_root: Path) -> tuple[list[Rule], str]:
    user = state_root / "rules.toml"
    if user.exists():
        try:
            return _compile(tomllib.loads(user.read_text()), str(user)), str(user)
        except tomllib.TOMLDecodeError as e:
            raise ToolError(f"{user}: not valid TOML: {e}")
    text = resources.files(__package__).joinpath("rules_default.toml").read_text()
    return _compile(tomllib.loads(text), "built-in rules"), "built-in"


def classify(rules: list[Rule], app: str | None, qr_types: set[str], domains: list[str], text: str,
             code: dict | None = None) -> tuple[str, str]:
    for r in rules:
        why = r.match(app, qr_types, domains, text, code)
        if why:
            return r.category, why
    return UNCATEGORIZED, "no rule matched"
