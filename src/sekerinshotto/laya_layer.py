"""Laya query layer (FORMAT §7): typed questions over finished notes, answered locally.

Laya is a pure function here: (state text, questions) -> answers. It never sees the DB,
paths or SQL, and nothing it says is written anywhere except the answer cache.
Measured on the 182-screenshot sample against rule labels (53 items):
  typed-decisions 79 % topic accuracy, english 62 %, multilingual 40 % (and over-confident).
  Structured facts first + 600 chars of text: same accuracy, ~49 ms per note.
"""
from __future__ import annotations

import hashlib
import json

from .contract import ToolError

REPO = "convaiinnovations/laya"
DEFAULT_CHECKPOINT = "typed-decisions"
CHECKPOINTS = ("typed-decisions", "english", "multilingual")
STATE_CHARS = 600
INSTALL_HINT = "Laya is not installed: uv tool install --editable '.[laya]'  (or: pip install 'sekerinshotto[laya]')"

_AGENTS: dict[str, object] = {}


def model_rev(checkpoint: str) -> str:
    try:
        from importlib.metadata import version
        v = version("laya-mlx")
    except Exception:  # noqa: BLE001
        v = "unknown"
    return f"{REPO}:{checkpoint}@laya-mlx-{v}"


def load_agent(checkpoint: str):
    """Tests replace this. Real use: laya-mlx on Apple Silicon; the first call downloads the weights."""
    if checkpoint not in CHECKPOINTS:
        raise ToolError(f"unknown checkpoint {checkpoint!r}; valid: {', '.join(CHECKPOINTS)}")
    if checkpoint in _AGENTS:
        return _AGENTS[checkpoint]
    try:
        import laya_mlx
    except ImportError:
        raise ToolError(INSTALL_HINT)
    agent = laya_mlx.load(REPO, subfolder=None if checkpoint == "english" else checkpoint)
    _AGENTS[checkpoint] = agent
    return agent


def build_state(rec: dict, text: str) -> str:
    """Structured facts first, so the model's context limit cuts the text tail, not the facts."""
    head = [f"app: {rec.get('source_app') or 'unknown'}",
            f"category (rules): {rec.get('category') or 'uncategorized'}",
            f"qr: {', '.join(b['type'] for b in rec['entities']['qr']) or 'none'}",
            f"domains: {', '.join(rec['entities']['domains']) or 'none'}",
            f"terms: {', '.join(rec.get('terms') or []) or 'none'}", "---"]
    return "\n".join(head) + "\n" + (text or "")[:STATE_CHARS]


def parse_question(raw: str) -> dict:
    try:
        q = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ToolError(f"--question is not JSON: {e}")
    if not isinstance(q, dict) or q.get("type") not in ("choice", "noul", "score"):
        raise ToolError('--question must be {"type": "choice"|"noul"|"score", "instructions": "...", '
                        '"criteria": [...] or {name: description}} (criteria required for choice/score)')
    if not str(q.get("instructions", "")).strip():
        raise ToolError("--question needs non-empty instructions")
    if q["type"] in ("choice", "score") and not q.get("criteria"):
        raise ToolError(f"a {q['type']} question needs criteria")
    return q


def qhash(q: dict) -> str:
    return hashlib.sha256(json.dumps(q, sort_keys=True).encode()).hexdigest()[:16]


def value_of(answer: dict) -> tuple[float, str | None]:
    """(sort key, choice) from one Laya answer: P(yes) for noul, confidence for choice, score for score."""
    t = answer.get("type")
    if t == "noul":
        return float(answer.get("noul", 0.0)), None
    if t == "choice":
        return float(answer.get("confidence", 0.0)), answer.get("choice")
    return float(answer.get("score", answer.get("confidence", 0.0))), answer.get("choice")
