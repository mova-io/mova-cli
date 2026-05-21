"""Fix registry — each fix is a self-contained :class:`Fix` instance.

The registry pattern keeps the dispatcher trivial (just iterate the
list) and makes adding a new fix a one-class change. Each fix
declares:

* its ``id`` (kebab-case, used in ``--only`` / ``--skip`` flags)
* a human-readable ``label`` and ``description``
* ``check(root)`` → ``True`` if the fix is *needed* (operators want
  to see "would apply" only for things that are actually broken)
* ``apply(root, *, dry_run)`` → :class:`FixResult` describing what
  happened (or would have happened in dry-run)

Fixes that fail mid-apply return ``FixStatus.FAILED`` with a
human-readable reason. The dispatcher continues with the next fix —
one bad permission shouldn't block creating ``.movate/``.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

# Canonical "tight" mode for secrets files. Lifted to a constant so the
# perms-check logic doesn't sprinkle 0o600 magic numbers all over.
_SECRETS_FILE_MODE = 0o600


class FixStatus(StrEnum):
    """Outcome of running one fix."""

    NOT_NEEDED = "not_needed"
    """The fix's check returned False — nothing to do."""

    WOULD_APPLY = "would_apply"
    """Dry-run mode + the fix is needed."""

    APPLIED = "applied"
    """Apply mode + the fix succeeded."""

    FAILED = "failed"
    """Apply attempted but raised — see :attr:`FixResult.message`."""


@dataclass(frozen=True)
class FixResult:
    """Outcome of dispatching one :class:`Fix`."""

    fix_id: str
    status: FixStatus
    message: str = ""
    """Operator-facing context — what was changed, or why it failed."""


@dataclass(frozen=True)
class Fix:
    """One auto-remediation. ``check`` + ``apply`` are pure-enough callables.

    Both callbacks take the project root. ``check`` returns whether the
    fix is needed; ``apply`` performs the change (or returns a no-op
    message in dry-run). Keeping the two separate makes the preview
    table fast even with many fixes — we only call ``apply`` when the
    operator opts in.
    """

    id: str
    label: str
    description: str
    check: Callable[[Path], bool]
    apply_fn: Callable[[Path, bool], FixResult]

    def run(self, project_root: Path, *, dry_run: bool) -> FixResult:
        """Dispatch: check first, short-circuit if not needed, else apply."""
        if not self.check(project_root):
            return FixResult(fix_id=self.id, status=FixStatus.NOT_NEEDED)
        try:
            return self.apply_fn(project_root, dry_run)
        except Exception as exc:
            return FixResult(
                fix_id=self.id,
                status=FixStatus.FAILED,
                message=str(exc),
            )


# ---------------------------------------------------------------------------
# Individual fix implementations
# ---------------------------------------------------------------------------


_GITIGNORE_BODY = """\
# movate runtime state — never commit
.movate/local.db
.movate/local.db-*

# Snapshots are commit-friendly by default; uncomment to opt out:
# .movate/snapshots/

# Python
__pycache__/
*.pyc

# Editor / OS
.vscode/
.idea/
.DS_Store

# Secrets
.env
"""


def _check_movate_dir(root: Path) -> bool:
    """Fix needed when .movate/ doesn't exist yet."""
    return not (root / ".movate").is_dir()


def _apply_movate_dir(root: Path, dry_run: bool) -> FixResult:
    target = root / ".movate"
    if dry_run:
        return FixResult(
            fix_id="ensure-movate-dir",
            status=FixStatus.WOULD_APPLY,
            message=f"would create {target}",
        )
    target.mkdir(parents=True, exist_ok=True)
    return FixResult(
        fix_id="ensure-movate-dir",
        status=FixStatus.APPLIED,
        message=f"created {target}",
    )


def _check_gitignore(root: Path) -> bool:
    """Fix needed when .gitignore is absent. We DON'T overwrite an
    existing one — operators have their own conventions."""
    return not (root / ".gitignore").is_file()


def _apply_gitignore(root: Path, dry_run: bool) -> FixResult:
    target = root / ".gitignore"
    if dry_run:
        return FixResult(
            fix_id="ensure-gitignore",
            status=FixStatus.WOULD_APPLY,
            message=f"would create {target} with movate-aware ignores",
        )
    target.write_text(_GITIGNORE_BODY)
    return FixResult(
        fix_id="ensure-gitignore",
        status=FixStatus.APPLIED,
        message=f"created {target}",
    )


def _check_env_from_example(root: Path) -> bool:
    """Fix needed when .env.example exists but .env doesn't.

    We don't auto-create .env from scratch — without a template it'd
    be empty noise. Operators who don't have .env.example get a hint
    from doctor instead.
    """
    return (root / ".env.example").is_file() and not (root / ".env").is_file()


def _apply_env_from_example(root: Path, dry_run: bool) -> FixResult:
    example = root / ".env.example"
    target = root / ".env"
    if dry_run:
        return FixResult(
            fix_id="ensure-env-from-example",
            status=FixStatus.WOULD_APPLY,
            message=f"would copy {example} → {target} (you'll still need to fill in real values)",
        )
    target.write_text(example.read_text())
    return FixResult(
        fix_id="ensure-env-from-example",
        status=FixStatus.APPLIED,
        message=f"copied {example} → {target} — edit it to add real values",
    )


def _check_secrets_permissions(root: Path) -> bool:
    """Fix needed when ANY secrets file is world/group-readable.

    Walks ``~/.movate/secrets/*.yaml`` (the canonical secrets-store
    location). Returns True if any file is anything other than 0600.
    ``project_root`` is ignored — secrets are operator-wide, not
    project-wide, but we keep the signature consistent.
    """
    _ = root
    secrets_dir = Path(os.path.expanduser("~")) / ".movate" / "secrets"
    if not secrets_dir.is_dir():
        return False
    return any(
        (path.stat().st_mode & 0o777) != _SECRETS_FILE_MODE for path in secrets_dir.glob("*.yaml")
    )


def _apply_secrets_permissions(root: Path, dry_run: bool) -> FixResult:
    _ = root
    secrets_dir = Path(os.path.expanduser("~")) / ".movate" / "secrets"
    touched: list[str] = []
    for path in secrets_dir.glob("*.yaml"):
        mode = path.stat().st_mode & 0o777
        if mode == _SECRETS_FILE_MODE:
            continue
        touched.append(path.name)
        if not dry_run:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    if dry_run:
        return FixResult(
            fix_id="fix-secrets-permissions",
            status=FixStatus.WOULD_APPLY,
            message=f"would chmod 0600 on {len(touched)} file(s): {', '.join(touched)}",
        )
    return FixResult(
        fix_id="fix-secrets-permissions",
        status=FixStatus.APPLIED,
        message=f"chmod 0600 on {len(touched)} file(s): {', '.join(touched)}",
    )


def _check_agents_dir(root: Path) -> bool:
    """Fix needed when agents/ is missing AND the project has movate.yaml.

    We only touch agents/ for actual movate projects — a stray ``mdk fix``
    run in a non-movate directory shouldn't sprinkle agents/ everywhere.
    """
    if not (root / "movate.yaml").is_file():
        return False
    return not (root / "agents").is_dir()


def _apply_agents_dir(root: Path, dry_run: bool) -> FixResult:
    target = root / "agents"
    gitkeep = target / ".gitkeep"
    if dry_run:
        return FixResult(
            fix_id="ensure-agents-dir",
            status=FixStatus.WOULD_APPLY,
            message=f"would create {target} (with .gitkeep)",
        )
    target.mkdir(exist_ok=True)
    gitkeep.write_text("")
    return FixResult(
        fix_id="ensure-agents-dir",
        status=FixStatus.APPLIED,
        message=f"created {target}/.gitkeep",
    )


# ---------------------------------------------------------------------------
# OCR optional-package fix helpers
# ---------------------------------------------------------------------------

def _uv_pip_install(packages: list[str], fix_id: str) -> FixResult:
    """Install ``packages`` into the current Python interpreter via uv pip.

    Works for both ``uv tool install``-ed mdk binaries (isolated venv in
    ``~/.local/share/uv/tools/movate-cli/``) and project-venv installs —
    ``--python sys.executable`` always targets the running interpreter's
    environment, bypassing any outer venv resolution.

    Raises on non-zero returncode so the caller's ``try/except`` in
    :meth:`Fix.run` converts it to ``FixStatus.FAILED``.
    """
    result = subprocess.run(
        ["uv", "pip", "install", "--python", sys.executable, *packages],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or "uv pip install failed").strip()
        )
    return FixResult(
        fix_id=fix_id,
        status=FixStatus.APPLIED,
        message=f"installed {', '.join(packages)}",
    )


def _check_ocr_extra(_root: Path) -> bool:
    """True when pdf2image or pytesseract is not importable."""
    return (
        importlib.util.find_spec("pdf2image") is None
        or importlib.util.find_spec("pytesseract") is None
    )


def _apply_ocr_extra(_root: Path, dry_run: bool) -> FixResult:
    packages = ["pdf2image", "pytesseract"]
    if dry_run:
        return FixResult(
            fix_id="install-ocr-extra",
            status=FixStatus.WOULD_APPLY,
            message=f"would install: {', '.join(packages)} (enables scanned PDF OCR via Tesseract)",
        )
    return _uv_pip_install(packages, "install-ocr-extra")


def _check_easyocr_extra(_root: Path) -> bool:
    """True when easyocr is not importable."""
    return importlib.util.find_spec("easyocr") is None


def _apply_easyocr_extra(_root: Path, dry_run: bool) -> FixResult:
    packages = ["easyocr"]
    if dry_run:
        return FixResult(
            fix_id="install-easyocr-extra",
            status=FixStatus.WOULD_APPLY,
            message=(
                "would install: easyocr (~300 MB, includes torch-cpu) — "
                "enables MOVATE_OCR_BACKEND=easyocr for noisy / low-quality scans"
            ),
        )
    return _uv_pip_install(packages, "install-easyocr-extra")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def available_fixes() -> list[Fix]:
    """The canonical fix list. Order = preview-display order.

    Ordering rationale: filesystem-only fixes first (movate dir, gitignore,
    env, agents dir), security-related fixes last (secrets perms) so
    they're visible / not buried.
    """
    return [
        Fix(
            id="ensure-movate-dir",
            label="Create .movate/",
            description=(
                "Create the .movate/ runtime directory. Houses local.db, "
                "snapshots/, promotions.yaml. Created lazily by most "
                "commands; this fix is for when ops want it eagerly."
            ),
            check=_check_movate_dir,
            apply_fn=_apply_movate_dir,
        ),
        Fix(
            id="ensure-gitignore",
            label="Create .gitignore",
            description=(
                "Create a movate-aware .gitignore with the standard "
                "ignores (.movate/local.db, .env, __pycache__, etc.). "
                "Does NOT overwrite an existing .gitignore."
            ),
            check=_check_gitignore,
            apply_fn=_apply_gitignore,
        ),
        Fix(
            id="ensure-env-from-example",
            label="Create .env from .env.example",
            description=(
                "Copy .env.example to .env. You'll still need to fill "
                "in real API keys — this just establishes the template "
                "so dotenv loading works."
            ),
            check=_check_env_from_example,
            apply_fn=_apply_env_from_example,
        ),
        Fix(
            id="ensure-agents-dir",
            label="Create agents/ with .gitkeep",
            description=(
                "Create the empty agents/ directory if movate.yaml is "
                "present but agents/ isn't. Only fires in real movate "
                "projects — won't pollute non-movate directories."
            ),
            check=_check_agents_dir,
            apply_fn=_apply_agents_dir,
        ),
        Fix(
            id="fix-secrets-permissions",
            label="chmod 0600 on secrets files",
            description=(
                "Tighten ~/.movate/secrets/*.yaml to 0600 (user-only). "
                "These files contain plaintext API keys — wrong perms "
                "are a security incident waiting to happen."
            ),
            check=_check_secrets_permissions,
            apply_fn=_apply_secrets_permissions,
        ),
        Fix(
            id="install-ocr-extra",
            label="Install OCR deps (pdf2image + pytesseract)",
            description=(
                "Install pdf2image and pytesseract into the active Python "
                "environment. Required for mdk kb ingest to OCR scanned / "
                "mixed PDFs via the default Tesseract backend. The Tesseract "
                "system binary (brew install tesseract) must also be on PATH."
            ),
            check=_check_ocr_extra,
            apply_fn=_apply_ocr_extra,
        ),
        Fix(
            id="install-easyocr-extra",
            label="Install EasyOCR (~300 MB)",
            description=(
                "Install easyocr (pure-Python, no system binary required). "
                "Enables MOVATE_OCR_BACKEND=easyocr — better accuracy on "
                "noisy or low-quality scans. Downloads ~300 MB of torch-cpu "
                "and model weights on first use. Skipped if already installed."
            ),
            check=_check_easyocr_extra,
            apply_fn=_apply_easyocr_extra,
        ),
    ]


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def diagnose_and_fix(
    project_root: Path,
    *,
    dry_run: bool = True,
    only: tuple[str, ...] = (),
    skip: tuple[str, ...] = (),
) -> list[FixResult]:
    """Run every applicable fix, returning per-fix results.

    ``only`` and ``skip`` filter the registry by fix id. ``only`` wins
    when both are supplied (operator's intent is the union of the
    declared subset, not the cross-product).
    """
    results: list[FixResult] = []
    for fix in available_fixes():
        if only and fix.id not in only:
            continue
        if skip and fix.id in skip:
            continue
        results.append(fix.run(project_root, dry_run=dry_run))
    return results
