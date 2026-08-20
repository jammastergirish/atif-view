"""atif-view command line."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from atif_make import corpus
from atif_make.archive import is_archive

from .viewer import serve


def _single_entry(path: Path) -> list[corpus.Entry]:
    """One file as an index entry, so it can be viewed without indexing.

    Built by corpus.describe rather than assembled here: an Entry that is put
    together in two places drifts the moment a field is added, and this one had
    already fallen behind.
    """
    entry = corpus.describe(path)
    return [entry] if entry else []


def cmd_view(args: argparse.Namespace) -> int:
    if args.input:
        path = Path(args.input).expanduser()
        if not path.exists():
            print(f"atif-view: no such path: {path}", file=sys.stderr)
            return 2
        # A directory or archive holds many sessions; a plain file holds one.
        entries = (
            corpus.scan([path])
            if path.is_dir() or is_archive(path)
            else _single_entry(path)
        )
    else:
        entries = corpus.load() or corpus.scan()

    if not entries:
        print("atif-view: nothing to view (try `atif-make index` first)", file=sys.stderr)
        return 1
    serve(
        entries,
        port=args.port or 7433,
        open_browser=not args.no_open,
        explicit_port=args.port is not None,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atif-view",
        description="Browse ATIF trajectories in a local viewer.",
    )
    parser.add_argument("input", nargs="?",
                        help="file, directory or archive (default: the atif-make index)")
    parser.add_argument("--port", type=int, default=None,
                        help="port to listen on (default: 7433, or the next free one)")
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.set_defaults(func=cmd_view)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
