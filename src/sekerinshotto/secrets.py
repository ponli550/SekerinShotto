"""Credentials in screenshots (code, .env files, terminals, dashboards).

Unlike personal data (redacted only at the LLM boundary), secrets are scrubbed at EXTRACTION, before a note,
manifest or index row is written -- the same treatment as Wi-Fi QR passwords. A key in a vault is a key in
every backup and sync of it. The replacement names the kind, so the note still says what was there.
"""
from __future__ import annotations

import re

# (kind, pattern). Specific formats first; the generic key=value rule last.
PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S)),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("openai", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{10,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[0-9A-Z]{16}\b")),
    ("github", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("slack", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google-api", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe", re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("url-password", re.compile(r"(?<=://)[^\s:/@\[\]]+:[^\s@/\[\]]{3,}(?=@)")),
]
# key = "value" / key: value / KEY=value for secret-looking names. The name stays; the value goes.
ASSIGN = re.compile(
    r"(?i)(?<!\[)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|"
    r"client[_-]?secret|private[_-]?key|auth)[A-Za-z0-9_.-]*)(\s*[:=]\s*)([\"']?)([^\s\"',;]{6,})\3")
# Unquoted code on the right-hand side (`token = self.token`, `tokens = tokenize(text)`) is not a secret.
_CODE_VALUE = re.compile(r"[()\[\]{}]|^[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*$")
_PLACEHOLDER = re.compile(r"^(?:\*+|x+|\.+|<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|changeme|your[_-].*|example|null|none|"
                          r"true|false|\[SECRET:[a-z-]+\])$", re.I)


def scrub(text: str) -> tuple[str, list[str]]:
    """(text with secrets replaced, kinds found). Idempotent: already-scrubbed text is unchanged."""
    if not text:
        return text, []
    kinds: list[str] = []
    for kind, rx in PATTERNS:
        def rep(m, k=kind):
            kinds.append(k)
            return f"[SECRET:{k}]"
        text = rx.sub(rep, text)

    def assign(m):
        if _PLACEHOLDER.match(m.group(4)) or (not m.group(3) and _CODE_VALUE.search(m.group(4))):
            return m.group(0)
        kinds.append("assigned")
        return f"{m.group(1)}{m.group(2)}{m.group(3)}[SECRET:assigned]{m.group(3)}"
    text = ASSIGN.sub(assign, text)
    return text, kinds
