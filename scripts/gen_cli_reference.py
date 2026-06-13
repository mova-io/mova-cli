"""Generate the ``mdk`` CLI command reference into ``docs/cli-reference.md``.

Introspects the Typer app via its underlying Click command tree and emits a
markdown doc: the full command tree, then each command's help + options. The
generation logic lives in :func:`movate.cli.docs_cmd.generate_cli_reference`
so this script and the ``mdk docs cli`` subcommand stay in lockstep — this is
a thin CLI wrapper around it.

Usage
-----

::

    # Write docs/cli-reference.md
    python scripts/gen_cli_reference.py

    # Preview without writing
    python scripts/gen_cli_reference.py --print

    # CI freshness gate — exit non-zero if the committed doc is stale
    python scripts/gen_cli_reference.py --check

Why this exists
---------------

The CLI surface is large and grows often; a hand-maintained reference goes
stale immediately. This regenerates it deterministically from the live Typer
app. Wire ``--check`` into CI so a command/option change that isn't reflected
in the committed doc fails the build.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Default output path, relative to the repo root (this file's parent's parent).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_OUTPUT = _REPO_ROOT / "docs" / "cli-reference.md"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the mdk CLI command reference (docs/cli-reference.md).",
    )
    parser.add_argument(
        "output",
        nargs="?",
        type=Path,
        default=_DEFAULT_OUTPUT,
        help=f"Output path (default: {_DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--print",
        dest="print_only",
        action="store_true",
        help="Print to stdout instead of writing the file.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if the output file is stale (or missing). Writes nothing.",
    )
    args = parser.parse_args()

    # Imported lazily so a `--help` invocation doesn't pull the whole CLI in.
    from movate.cli.docs_cmd import generate_cli_reference  # noqa: PLC0415

    markdown = generate_cli_reference()

    if args.print_only:
        sys.stdout.write(markdown)
        return 0

    output: Path = args.output

    if args.check:
        current = output.read_text() if output.exists() else None
        if current == markdown:
            print(f"OK: {output} is up to date")
            return 0
        print(
            f"STALE: {output} differs from the generated reference — "
            "run scripts/gen_cli_reference.py and commit the result",
            file=sys.stderr,
        )
        return 1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(markdown)
    print(f"wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
