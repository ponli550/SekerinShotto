"""Entry point. argparse is generated from the command registry."""
from __future__ import annotations

import argparse
import sys

from . import commands  # noqa: F401  (registers commands)
from .contract import (COMMIT_ARG, COMMON_ARGS, EXIT_ERROR, EXIT_OK, EXIT_VIOLATION, REGISTRY, ToolError,
                       emit, envelope)
from .state import State, resolve_state


def _add(p: argparse.ArgumentParser, arg) -> None:
    if arg.flag:
        p.add_argument(arg.name, action="store_true", help=arg.help)
    elif arg.name.startswith("--"):
        p.add_argument(arg.name, type=arg.type, default=arg.default, help=arg.help)
    else:
        p.add_argument(arg.name, type=arg.type, nargs=None if arg.required else "?", help=arg.help)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError(f"usage: {message}")


def build() -> argparse.ArgumentParser:
    root = _Parser(prog="sekerinshotto", description="Deterministic screenshot extractor")
    sub = root.add_subparsers(dest="cmd", parser_class=_Parser)
    groups: dict[str, argparse._SubParsersAction] = {}
    for c in REGISTRY.values():
        head, _, tail = c.path.partition(" ")
        if tail:                                       # nested: "domains update"
            if head not in groups:
                g = sub.add_parser(head, help=f"{head} commands")
                groups[head] = g.add_subparsers(dest="sub", parser_class=_Parser)
            p = groups[head].add_parser(tail, help=c.summary, description=c.details or c.summary)
        else:
            p = sub.add_parser(c.path, help=c.summary, description=c.details or c.summary)
        for arg in c.args + COMMON_ARGS + ([COMMIT_ARG] if c.writes else []):
            _add(p, arg)
    return root


def _path_of(argv: list[str]) -> str:
    words = [w for w in argv if not w.startswith("-")]
    for n in (2, 1):
        cand = " ".join(words[:n])
        if cand in REGISTRY:
            return cand
    return "sekerinshotto"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    path = _path_of(argv)
    try:
        a = build().parse_args(argv)
        if not a.cmd:
            raise ToolError(f"no command given; valid: {', '.join(REGISTRY)}")
        name = f"{a.cmd} {a.sub}" if getattr(a, "sub", None) else a.cmd
        if name not in REGISTRY:
            raise ToolError(f"unknown command {name!r}; valid: {', '.join(REGISTRY)}")
        state = State(resolve_state(a.state))
        res = REGISTRY[name].handler(a, state)
    except ToolError as e:
        emit(envelope(path, error=str(e)), as_json)
        return EXIT_ERROR
    except Exception as e:  # noqa: BLE001 - the contract promises an envelope even on a bug
        emit(envelope(path, error=f"internal error: {type(e).__name__}: {e}"), as_json)
        return EXIT_ERROR
    emit(envelope(name, data=res.data), as_json)
    return EXIT_VIOLATION if res.violation else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
