"""``mdk migrate-state`` — rename a project's legacy ``.movate/`` dir to ``.mdk/``.

ADR 011: the project-level runtime-state directory is now ``.mdk/``. Existing
projects keep working (the resolver reads a legacy ``.movate/``); this command
is the explicit, opt-in migration — it never runs automatically and never moves
data behind your back.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import typer

from movate.cli import _console
from movate.core.paths import LEGACY_STATE_DIR_NAME, STATE_DIR_NAME


def migrate_state(
    path: Path = typer.Argument(
        Path("."),
        help="Project root to migrate (default: current directory).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would change; write nothing.",
    ),
) -> None:
    """Rename this project's legacy ``.movate/`` runtime-state dir to ``.mdk/`` (ADR 011).

    Moves the directory (via ``git mv`` when inside a repo, so history + tracked
    eval baselines are preserved) and rewrites the project's ``.gitignore`` so
    ``.mdk/`` is ignored with the same committed-baseline exception. No-op when
    there's no legacy ``.movate/``; refuses to clobber a non-empty ``.mdk/``.
    """
    root = path.resolve()
    legacy = root / LEGACY_STATE_DIR_NAME
    target = root / STATE_DIR_NAME

    if not legacy.is_dir():
        _console.success(f"nothing to migrate — no {LEGACY_STATE_DIR_NAME}/ under {root}")
        return
    if target.exists() and any(target.iterdir()):
        _console.error(
            f"{STATE_DIR_NAME}/ already exists and is non-empty under {root}",
            context=f"merge or remove {target} first — refusing to clobber it.",
        )
        raise typer.Exit(code=2)

    if dry_run:
        _console.hint(f"[dim][dry-run] would move {legacy} → {target} and update .gitignore[/dim]")
        return

    _move_state_dir(root, legacy, target)
    rewrote = _rewrite_gitignore(root)
    extra = " (.gitignore updated)" if rewrote else ""
    _console.success(f"migrated {LEGACY_STATE_DIR_NAME}/ → {STATE_DIR_NAME}/ under {root}{extra}")


def _in_git_repo(root: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _move_state_dir(root: Path, legacy: Path, target: Path) -> None:
    """``git mv`` when tracked (preserves history); plain move otherwise."""
    if _in_git_repo(root):
        result = subprocess.run(
            ["git", "-C", str(root), "mv", str(legacy), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        # git mv fails if the dir isn't tracked — fall back to a plain move.
    shutil.move(str(legacy), str(target))


def _rewrite_gitignore(root: Path) -> bool:
    """Swap project-level ``.movate/`` ignore patterns to ``.mdk/``.

    Leaves the machine-global ``~/.movate/`` line untouched. Returns True if the
    file changed.
    """
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return False
    changed = False
    out: list[str] = []
    for line in gitignore.read_text().splitlines():
        if "~/.movate" in line:  # machine-global config — not ours to rename
            out.append(line)
        elif ".movate/" in line:
            out.append(line.replace(".movate/", ".mdk/"))
            changed = True
        else:
            out.append(line)
    if changed:
        gitignore.write_text("\n".join(out) + "\n")
    return changed


__all__ = ["migrate_state"]
